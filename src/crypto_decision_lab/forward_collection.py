"""S02-CP08-05 · Forward data collection for independent validation.

This module acquires, validates, quarantines and archives BTCUSDT 1h candles
that are *strictly later* than the frozen boundary of the independent validation
protocol (``2026-10-23T16:00:00Z``), and nothing else.

Hard invariants (all fail closed):

* the boundary, lookback, horizon, latency, purge and sample requirements are
  read from the frozen protocol YAML, never from local configuration: a
  divergence is a hard error;
* only fully closed candles are accepted (``open_time + 1h <= now`` and
  ``close_time == open_time + 3599999``), no partial candle, no interpolation,
  no gap filling, no candle that straddles the boundary;
* chunks are content addressed and write-once: an existing object is reused if
  the bytes are identical, never overwritten; a different payload for the same
  interval is a *conflict* and is blocked from promotion;
* every gap and every anomaly is registered in an append-only JSONL registry;
* no label, decision, equity, PnL, threshold selection or verdict is produced:
  the collection pipeline cannot evaluate the strategy.  Final evaluation is
  (and remains) the exclusive competence of
  :mod:`crypto_decision_lab.independent_validation`, on a registered dataset.

Archive layout::

    data/forward/BTCUSDT-1h-forward-v1/
        chunks/        # promoted, immutable, hash-verified candles
        quarantine/    # identified anomalies, preserved and never promoted
        manifests/     # immutable dataset manifests + append-only "latest" pointer
        registry/      # append-only events.jsonl (runs, gaps, conflicts)

CLI::

    python -m crypto_decision_lab.forward_collection status     # offline
    python -m crypto_decision_lab.forward_collection sync       # network, incremental
    python -m crypto_decision_lab.forward_collection audit      # offline
    python -m crypto_decision_lab.forward_collection readiness   # offline
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import errno
import hashlib
import json

# Import del progetto
from .protocol_integrity import (
    ARCHIVE_VERIFIED,
    PROTOCOL_VERIFIED,
    verify_archive,
    verify_protocol_integrity,
)

import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

import pandas as pd

try:  # POSIX (Sirio/Linux)
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows development hosts
    _fcntl = None
try:  # Windows fallback so the module imports everywhere
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - POSIX hosts
    _msvcrt = None

from . import independent_validation as iv
from .forward_isolation import (
    FORBIDDEN_ARTEFACT_STEMS,
    FORWARD_ROLE,
    ForwardIsolationViolation,
    guard_archive_layout,
    guard_artefact_names,
    is_forward_path,
)
from .market import HOUR_MS, fetch_klines, utc_ms
from .quality import check_quality
from .sync import digest

DATASET_ID = "BTCUSDT-1h-forward-v1"
DATASET_SCHEMA = "cdl.cp08.forward_dataset.v1"
CHUNK_SCHEMA = "cdl.cp08.forward_chunk.v1"
MANIFEST_SCHEMA = "cdl.cp08.forward_manifest.v1"
EVENT_SCHEMA = "cdl.cp08.forward_event.v1"
READINESS_SCHEMA = "cdl.cp08.forward_readiness.v1"
STATUS_SCHEMA = "cdl.cp08.forward_status.v1"
AUDIT_SCHEMA = "cdl.cp08.forward_audit.v1"

STATUS_VALID = "VALID"
STATUS_REQUIRES_REVIEW = "REQUIRES_REVIEW"
STATUS_QUARANTINED = "QUARANTINED"
STATUS_INVALID = "INVALID"
PROMOTABLE = (STATUS_VALID,)

READINESS_NO_DATA = "NO_DATA"
READINESS_NOT_READY = "NOT_READY"
READINESS_DATA_READY = "DATA_READY_EVALUATION_PENDING"

SOURCE_ID = "binance-spot"
DATA_SOURCE = "binance-spot:data-api.binance.vision"
SOURCE_ADAPTER = "crypto_decision_lab.market.fetch_klines"

BASE_COLUMNS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_buy_base",
    "taker_buy_quote",
]
PROVENANCE_COLUMNS = ["symbol", "schema_version", "source_id", "data_source", "record_checksum"]
CHUNK_COLUMNS = ["timestamp", *BASE_COLUMNS, *PROVENANCE_COLUMNS]
NUMERIC_COLUMNS = [
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "taker_buy_base",
    "taker_buy_quote",
]

DEFAULT_CONFIG_PATH = Path("config/forward-collection.yaml")
EXAMPLE_CONFIG_PATH = Path("config/forward-collection.example.yaml")
DEFAULT_ARCHIVE_ROOT = Path("data/forward")
DEFAULT_DATA_ROOT = Path("data")
DEFAULT_SNAPSHOT_MANIFEST = Path("data/snapshots/BTCUSDT-1h-raw-v1/raw-manifest.json")
DEFAULT_PROTOCOL_ARCHIVE = Path("data/experiments/S02-CP08-independent-validation-protocol-v1/manifest.json")
DEFAULT_REGISTRY_DIR = Path("data/validation_registry")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_BLOCKED = 75  # EX_TEMPFAIL: another sync holds the lock / no data available


class ForwardConfigError(ValueError):
    """Invalid configuration or configuration that diverges from the protocol."""


class ForwardDataError(RuntimeError):
    """Unusable source payload (schema, parse, request range)."""


class ForwardConflictError(RuntimeError):
    """An immutable object already exists with different content."""


class ForwardLockedError(RuntimeError):
    """Another collection run holds the exclusive lock."""


# ---------------------------------------------------------------------------
# generic helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(ms: int | None) -> str | None:
    if ms is None:
        return None
    return pd.Timestamp(int(ms), unit="ms", tz="UTC").isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _hash_obj(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        with temp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _atomic_write_once(path: Path, payload: dict[str, Any]) -> bool:
    """Write ``payload`` atomically unless the file already exists.

    Returns ``True`` when the file was created, ``False`` when an identical file
    already existed.  A different payload for the same path raises
    :class:`ForwardConflictError`: immutable objects are never replaced.
    """

    body = (_canonical_json(payload) + "\n").encode("utf-8")
    if path.exists():
        if path.read_bytes() == body:
            return False
        raise ForwardConflictError(f"Immutable object already exists with different content: {path}")
    _atomic_write_bytes(path, body)
    return True


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_write_bytes(path, (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _floor_hour_ms(value: datetime) -> int:
    stamp = pd.Timestamp(value).tz_convert("UTC")
    return int(stamp.floor("h").timestamp() * 1000)


def _range_list(times_ms: Iterable[int]) -> list[list[int]]:
    """Compress sorted millisecond timestamps into ``[start, end_exclusive]`` ranges."""

    ordered = sorted(set(int(value) for value in times_ms))
    ranges: list[list[int]] = []
    for value in ordered:
        if ranges and value == ranges[-1][1]:
            ranges[-1][1] = value + HOUR_MS
        else:
            ranges.append([value, value + HOUR_MS])
    return ranges


def _ranges_payload(times_ms: Iterable[int]) -> list[dict[str, Any]]:
    return [
        {
            "start_utc": _iso(start),
            "end_exclusive_utc": _iso(end),
            "hours": int((end - start) // HOUR_MS),
        }
        for start, end in _range_list(times_ms)
    ]


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ForwardPaths:
    """Filesystem layout, resolved against ``repo_root``."""

    repo_root: Path = Path(".")
    config_path: Path = DEFAULT_CONFIG_PATH
    archive_root: Path = DEFAULT_ARCHIVE_ROOT
    data_root: Path = DEFAULT_DATA_ROOT
    snapshot_manifest: Path = DEFAULT_SNAPSHOT_MANIFEST
    protocol_archive_manifest: Path = DEFAULT_PROTOCOL_ARCHIVE
    protocol_config_path: Path = iv.DEFAULT_CONFIG_PATH
    engine_config_path: Path = iv.DEFAULT_ENGINE_CONFIG_PATH
    registry_dir: Path = DEFAULT_REGISTRY_DIR
    dataset_id: str = DATASET_ID

    def resolve(self, path: str | Path | None) -> Path:
        if path is None:
            return Path()
        candidate = Path(path).expanduser()
        return candidate if candidate.is_absolute() else (self.repo_root / candidate)

    @property
    def config_file(self) -> Path:
        return self.resolve(self.config_path)

    @property
    def dataset_dir(self) -> Path:
        return self.resolve(self.archive_root) / self.dataset_id

    def validation_paths(self) -> iv.ValidationPaths:
        return iv.ValidationPaths(
            repo_root=self.repo_root,
            config_path=self.protocol_config_path,
            engine_config_path=self.engine_config_path,
            registry_dir=self.registry_dir,
        )


@dataclass(frozen=True)
class ForwardConfig:
    """Validated configuration.  Protocol-derived values are authoritative."""

    paths: ForwardPaths
    dataset_id: str
    symbol: str
    interval: str
    boundary_ms: int
    chunk_hours: int
    archive_dir: Path
    data_root: Path
    snapshot_manifest: Path
    protocol_archive_manifest: Path
    source: Mapping[str, Any]
    quality: Mapping[str, Any]
    pause_seconds: float = 0.0
    max_attempts: int = 3
    backoff_seconds: tuple[float, ...] = (2.0, 4.0, 8.0)
    readiness: Mapping[str, Any] = field(default_factory=dict)
    automation: Mapping[str, Any] = field(default_factory=dict)
    protocol: Mapping[str, Any] = field(default_factory=dict)
    protocol_meta: Mapping[str, Any] = field(default_factory=dict)
    used_example_config: bool = False

    @property
    def boundary_utc(self) -> str:
        return _iso(self.boundary_ms) or ""

    @property
    def chunk_ms(self) -> int:
        return int(self.chunk_hours) * HOUR_MS


def _section(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = raw.get(name) or {}
    if not isinstance(value, Mapping):
        raise ForwardConfigError(f"Config section '{name}' must be a mapping")
    return value


def _resolve_config_path(
    path: str | Path | None, paths: ForwardPaths
) -> tuple[Path, bool]:
    selected = path or os.environ.get("CDL_FORWARD_CONFIG")
    if selected:
        candidate = Path(selected).expanduser()
        if not candidate.is_absolute():
            candidate = paths.repo_root / candidate
        if not candidate.is_file():
            raise ForwardConfigError(f"Forward collection config not found: {candidate}")
        return candidate, False
    default = paths.repo_root / DEFAULT_CONFIG_PATH
    if default.is_file():
        return default, False
    example = paths.repo_root / EXAMPLE_CONFIG_PATH
    if example.is_file():
        return example, True
    raise ForwardConfigError(
        f"Forward collection config not found: {default} (copy {EXAMPLE_CONFIG_PATH})"
    )

def protocol_authority(paths: ForwardPaths) -> dict[str, Any]:
    """Use CP08-05R as the single protocol integrity authority."""

    validation_paths = paths.validation_paths()

    loaded = iv.load_protocol(validation_paths)
    protocol = loaded["config"]
    iv.validate_protocol(protocol)

    config_file = validation_paths.config_file
    archive_manifest = paths.resolve(paths.protocol_archive_manifest)

    protocol_result = verify_protocol_integrity(config_file)
    archive_result = verify_archive(archive_manifest.parent)

    protocol_ok = protocol_result["status"] == PROTOCOL_VERIFIED
    archive_ok = archive_result["status"] == ARCHIVE_VERIFIED

    meta = {
        "protocol_id": protocol["protocol_id"],
        "protocol_version": protocol["protocol_version"],
        "status": protocol["status"],
        "config_path": loaded["config_path"],
        "config_sha256": loaded["config_sha256"],
        "canonical_sha256": loaded["canonical_sha256"],
        "engine_config_path": loaded["engine_config_path"],
        "engine_config_sha256": loaded["engine_config_sha256"],
        "sidecar_path": str(config_file) + ".sha256.v2.json",
        "sidecar_match": protocol_ok,
        "protocol_integrity_status": protocol_result["status"],
        "archive_manifest": str(archive_manifest),
        "archive_manifest_match": archive_ok,
        "archive_status": archive_result["status"],
        "hash_ok": protocol_ok and archive_ok,
    }

    return {
        "config": protocol,
        "meta": meta,
        "loaded": loaded,
    }

def protocol_requirements(protocol: Mapping[str, Any]) -> dict[str, Any]:
    """Values the forward pipeline must respect, read from the frozen protocol."""

    independent = protocol["independent_validation"]
    walk = protocol["walk_forward"]
    primary = protocol["primary_scenario"]
    sample = protocol["go_nogo"]["sample_requirements"]
    benchmark = protocol["frozen_benchmark"]
    return {
        "boundary_ms": utc_ms(independent["start_utc"]),
        "boundary_utc": independent["start_utc"],
        "lookback_hours": int(independent["feature_warmup_hours"]),
        "horizon_hours": int(benchmark["horizon_hours"]),
        "latency_hours": int(primary["latency_hours"]),
        "purge_hours": int(walk["purge_hours"]),
        "test_windows_per_fold": int(walk["test_windows_per_fold"]),
        "folds_min_for_conclusive": int(walk["folds_min_for_conclusive"]),
        "folds_max": int(walk["folds_max"]),
        "min_train_windows": int(walk["min_train_windows"]),
        "min_oos_windows": int(independent["min_oos_windows"]),
        "min_duration_days": int(independent["min_duration_days"]),
        "min_trades": int(sample["min_trades"]),
        "min_folds": int(sample["min_folds"]),
        "min_independent_days": int(sample["min_independent_days"]),
        "allowed_sources": list(independent["allowed_sources"]),
        "forbidden_sources": list(independent["forbidden_sources"]),
    }


def forward_config_schema() -> dict[str, Any]:
    """Documented schema of ``config/forward-collection.example.yaml``."""

    return {
        "dataset": ["dataset_id", "symbol", "interval", "chunk_hours"],
        "archive": ["root"],
        "source": ["provider", "base_url", "endpoint", "source_id", "request_limit"],
        "acquisition": ["pause_seconds", "max_attempts", "backoff_seconds", "lock_timeout_seconds"],
        "quality": ["zero_volume_requires_review", "flat_ohlc_requires_review"],
        "windows": ["stride_hours"],
        "readiness": ["min_windows", "min_folds", "min_days", "min_trades"],
    }


def load_config(
    path: str | Path | None = None,
    *,
    paths: ForwardPaths = ForwardPaths(),
    now: datetime | None = None,
) -> ForwardConfig:
    """Load, validate and normalize the collection configuration.

    The YAML only carries *operational* choices (chunk size, cadence, retries).
    Protocol-derived values (boundary, lookback, horizon, latency, purge, sample
    requirements) are always taken from the frozen protocol; if the YAML tries to
    declare a different value, loading fails.
    """

    del now  # reserved for future clock-dependent validation
    import yaml

    config_path, used_example = _resolve_config_path(path, paths)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ForwardConfigError("Forward collection config root must be a mapping")

    authority = protocol_authority(paths)
    protocol = authority["config"]
    requirements = protocol_requirements(protocol)

    dataset = _section(raw, "dataset")
    archive = _section(raw, "archive")
    source = _section(raw, "source")
    acquisition = _section(raw, "acquisition")
    quality = _section(raw, "quality")
    windows = _section(raw, "windows")
    readiness = _section(raw, "readiness")
    automation = _section(raw, "automation")

    dataset_id = str(dataset.get("dataset_id", paths.dataset_id))
    if dataset_id != paths.dataset_id:
        raise ForwardConfigError(
            f"dataset_id {dataset_id!r} does not match the archive dataset {paths.dataset_id!r}"
        )
    symbol = str(dataset.get("symbol", "BTCUSDT"))
    if not symbol.isalnum() or symbol.upper() != symbol:
        raise ForwardConfigError("symbol must be uppercase alphanumeric")
    interval = str(dataset.get("interval", "1h"))
    if interval != "1h":
        raise ForwardConfigError("only the frozen 1h interval is supported")

    chunk_hours = int(dataset.get("chunk_hours", 24))
    if chunk_hours <= 0 or chunk_hours > 24 * 31:
        raise ForwardConfigError("dataset.chunk_hours must be in [1, 744]")

    # Optional declarations must agree with the protocol (no silent overrides).
    declared = {
        "boundary_utc": dataset.get("boundary_utc"),
        "lookback_hours": windows.get("lookback_hours"),
        "horizon_hours": windows.get("horizon_hours"),
        "latency_hours": windows.get("latency_hours"),
        "min_windows": readiness.get("min_windows"),
        "min_folds": readiness.get("min_folds"),
        "min_days": readiness.get("min_days"),
        "min_trades": readiness.get("min_trades"),
    }
    expected = {
        "boundary_utc": requirements["boundary_utc"],
        "lookback_hours": requirements["lookback_hours"],
        "horizon_hours": requirements["horizon_hours"],
        "latency_hours": requirements["latency_hours"],
        "min_windows": requirements["min_oos_windows"],
        "min_folds": requirements["min_folds"],
        "min_days": requirements["min_independent_days"],
        "min_trades": requirements["min_trades"],
    }
    mismatches = []
    for key, value in declared.items():
        if value is None:
            continue
        if key == "boundary_utc":
            same = utc_ms(str(value)) == requirements["boundary_ms"]
        else:
            same = int(value) == int(expected[key])
        if not same:
            mismatches.append(f"{key}: config={value!r} protocol={expected[key]!r}")
    if mismatches:
        raise ForwardConfigError(
            "Configuration diverges from the frozen protocol: " + "; ".join(mismatches)
        )

    stride_hours = int(windows.get("stride_hours", 24))
    if stride_hours <= 0 or stride_hours % 1:
        raise ForwardConfigError("windows.stride_hours must be a positive integer")

    archive_dir = paths.resolve(archive.get("root", paths.archive_root)) / dataset_id
    guard_archive_layout(archive_dir, repo_root=paths.repo_root)

    source_id = str(source.get("source_id", SOURCE_ID))
    provider = str(source.get("provider", "binance-spot"))
    if provider != "binance-spot":
        raise ForwardConfigError("only the reusable binance-spot adapter is supported")
    if source.get("api_key") or source.get("secret") or source.get("token"):
        raise ForwardConfigError(
            "Credentials must never be stored in the configuration or the manifests: "
            "the public Binance data endpoint needs none"
        )

    backoff = acquisition.get("backoff_seconds", [2, 4, 8])
    if not isinstance(backoff, (list, tuple)) or not backoff:
        raise ForwardConfigError("acquisition.backoff_seconds must be a non-empty list")

    data_root = paths.resolve(raw.get("data_root", paths.data_root))
    snapshot_manifest = paths.resolve(raw.get("snapshot_manifest", paths.snapshot_manifest))
    archive_manifest = paths.resolve(
        raw.get("protocol_archive_manifest", paths.protocol_archive_manifest)
    )

    return ForwardConfig(
        paths=paths,
        dataset_id=dataset_id,
        symbol=symbol,
        interval=interval,
        boundary_ms=requirements["boundary_ms"],
        chunk_hours=chunk_hours,
        archive_dir=archive_dir,
        data_root=data_root,
        snapshot_manifest=snapshot_manifest,
        protocol_archive_manifest=archive_manifest,
        source={
            **dict(source),
            "provider": provider,
            "source_id": source_id,
            "adapter": SOURCE_ADAPTER,
            "data_source": str(source.get("data_source", DATA_SOURCE)),
        },
        quality=dict(quality),
        pause_seconds=float(acquisition.get("pause_seconds", 0.0) or 0.0),
        max_attempts=int(acquisition.get("max_attempts", 3) or 3),
        backoff_seconds=tuple(float(value) for value in backoff),
        readiness={
            **dict(readiness),
            **{key: expected[key] for key in ("min_windows", "min_folds", "min_days", "min_trades")},
            "stride_hours": stride_hours,
        },
        automation=dict(automation),
        protocol=protocol,
        protocol_meta=authority["meta"],
        used_example_config=used_example,
    )


# ---------------------------------------------------------------------------
# canonical frames, provenance, quality gate
# ---------------------------------------------------------------------------


def _require_columns(frame: pd.DataFrame) -> None:
    missing = [name for name in ("open_time", "close_time", *NUMERIC_COLUMNS) if name not in frame.columns]
    if missing:
        raise ForwardDataError(f"Source frame missing columns: {missing}")


def canonical_frame(
    frame: pd.DataFrame,
    *,
    symbol: str,
    source_id: str = SOURCE_ID,
    data_source: str = DATA_SOURCE,
) -> pd.DataFrame:
    """Deterministic, provenance-tagged frame used for the content hash.

    ``ingestion_timestamp`` is intentionally *not* a column: it lives in the
    chunk metadata.  Keeping it out of the payload makes the bytes (and the
    SHA-256) reproducible, so re-ingesting identical data is a no-op.
    """

    if frame is None or len(frame) == 0:
        raise ForwardDataError("Empty source payload")
    _require_columns(frame)
    out = frame.copy()
    for column in NUMERIC_COLUMNS:
        out[column] = pd.to_numeric(out[column], errors="raise").astype("float64")
    for column in ("open_time", "close_time"):
        out[column] = pd.to_numeric(out[column], errors="raise").astype("int64")
    out = out.sort_values("open_time", kind="stable").reset_index(drop=True)
    out["timestamp"] = pd.to_datetime(out["open_time"], unit="ms", utc=True)
    out["symbol"] = symbol
    out["schema_version"] = DATASET_SCHEMA
    out["source_id"] = source_id
    out["data_source"] = data_source
    base = out[BASE_COLUMNS].to_dict(orient="records")
    out["record_checksum"] = [
        digest_bytes(_canonical_json({"schema_version": DATASET_SCHEMA, "row": row}).encode("utf-8"))
        for row in base
    ]
    return out[CHUNK_COLUMNS].reset_index(drop=True)


def data_sha256(frame: pd.DataFrame) -> str:
    """Content hash of the OHLCV payload (provenance columns excluded)."""

    records = frame[BASE_COLUMNS].to_dict(orient="records")
    return _hash_obj({"schema_version": DATASET_SCHEMA, "rows": records})


def record_chain_sha256(frame: pd.DataFrame) -> str:
    return _hash_obj({"records": frame["record_checksum"].tolist()})


@dataclass(frozen=True)
class ChunkAssessment:
    status: str
    issues: tuple[dict[str, Any], ...]
    gaps: tuple[int, ...]
    rows: int
    expected_rows: int

    @property
    def promotable(self) -> bool:
        return self.status in PROMOTABLE

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "issues": list(self.issues),
            "gaps": [_iso(value) for value in self.gaps],
            "rows": int(self.rows),
            "expected_rows": int(self.expected_rows),
        }


def assess_chunk(
    frame: pd.DataFrame,
    *,
    start_ms: int,
    end_ms: int,
    boundary_ms: int,
    closed_boundary_ms: int,
    symbol: str = "BTCUSDT",
    quality: Mapping[str, Any] | None = None,
) -> ChunkAssessment:
    """Quality gate.  Classifies VALID / REQUIRES_REVIEW / QUARANTINED / INVALID."""

    quality = dict(quality or {})
    expected_rows = int((end_ms - start_ms) // HOUR_MS)
    expected_times = list(range(start_ms, end_ms, HOUR_MS))

    if start_ms < boundary_ms or end_ms <= start_ms:
        return ChunkAssessment(
            STATUS_INVALID,
            (
                {
                    "code": "BOUNDARY_VIOLATION",
                    "detail": f"interval [{_iso(start_ms)}, {_iso(end_ms)}) touches or precedes the boundary {_iso(boundary_ms)}",
                },
            ),
            (),
            0 if frame is None else int(len(frame)),
            expected_rows,
        )
    if end_ms > closed_boundary_ms:
        return ChunkAssessment(
            STATUS_INVALID,
            (
                {
                    "code": "UNCLOSED_INTERVAL",
                    "detail": f"interval ends at {_iso(end_ms)} after the last closed hour {_iso(closed_boundary_ms)}",
                },
            ),
            (),
            0 if frame is None else int(len(frame)),
            expected_rows,
        )
    if frame is None or len(frame) == 0:
        return ChunkAssessment(
            STATUS_INVALID,
            ({"code": "EMPTY_PAYLOAD", "detail": "provider returned no rows"},),
            tuple(expected_times),
            0,
            expected_rows,
        )
    try:
        _require_columns(frame)
        frame = canonical_frame(frame, symbol=symbol)
    except (ForwardDataError, ValueError, TypeError) as exc:
        return ChunkAssessment(
            STATUS_INVALID,
            ({"code": "SCHEMA_ERROR", "detail": str(exc)},),
            (),
            int(len(frame)),
            expected_rows,
        )

    issues: list[dict[str, Any]] = []
    times = frame["open_time"].astype("int64")
    actual = set(times.tolist())

    if times.duplicated().any():
        duplicates = sorted(times[times.duplicated()].unique().tolist())
        issues.append(
            {
                "code": "DUPLICATE_TIMESTAMPS",
                "detail": f"{len(duplicates)} duplicated open_time values",
                "timestamps": [_iso(value) for value in duplicates],
            }
        )
    if (times % HOUR_MS != 0).any():
        issues.append({"code": "UNALIGNED_TIMESTAMPS", "detail": "timestamps are not hourly aligned"})
    if not times.is_monotonic_increasing:
        issues.append({"code": "UNSORTED_TIMESTAMPS", "detail": "timestamps are not sorted"})
    out_of_range = sorted(actual - set(expected_times))
    if out_of_range:
        issues.append(
            {
                "code": "OUT_OF_RANGE",
                "detail": f"{len(out_of_range)} rows outside [{_iso(start_ms)}, {_iso(end_ms)})",
                "timestamps": [_iso(value) for value in out_of_range],
            }
        )
    if (times < boundary_ms).any():
        early = sorted(value for value in actual if value < boundary_ms)
        issues.append(
            {
                "code": "BOUNDARY_VIOLATION",
                "detail": "payload contains bars before the frozen boundary (research reuse)",
                "timestamps": [_iso(value) for value in early],
            }
        )
    missing = sorted(set(expected_times) - actual)
    gaps = tuple(missing)
    if missing:
        issues.append(
            {
                "code": "MISSING_HOURS",
                "detail": f"{len(missing)} hourly candles missing",
                "timestamps": [_iso(value) for value in missing],
            }
        )
    partial = frame.loc[frame["close_time"].astype("int64") != times + HOUR_MS - 1]
    if len(partial):
        issues.append(
            {
                "code": "PARTIAL_CANDLE",
                "detail": f"{len(partial)} candles are not fully closed",
                "timestamps": [_iso(int(value)) for value in partial["open_time"].tolist()],
            }
        )
    if frame.isna().any().any():
        issues.append({"code": "MISSING_VALUES", "detail": "NaN/None cell values present"})
    if (frame[["open", "high", "low", "close"]] <= 0).any().any():
        issues.append({"code": "NON_POSITIVE_PRICE", "detail": "non-positive OHLC value"})
    if (frame["high"] < frame[["open", "close", "low"]].max(axis=1)).any():
        issues.append({"code": "HIGH_INCONSISTENT", "detail": "high below open/close/low"})
    if (frame["low"] > frame[["open", "close", "high"]].min(axis=1)).any():
        issues.append({"code": "LOW_INCONSISTENT", "detail": "low above open/close/high"})
    for column in ("volume", "quote_volume", "taker_buy_base", "taker_buy_quote"):
        if (frame[column] < 0).any():
            issues.append({"code": "NEGATIVE_VOLUME", "detail": f"negative {column}"})
    if (frame["taker_buy_base"] > frame["volume"] + 1e-9).any():
        issues.append({"code": "TAKER_VOLUME_EXCEEDS", "detail": "taker base volume exceeds volume"})

    hard_codes = {
        "DUPLICATE_TIMESTAMPS",
        "UNALIGNED_TIMESTAMPS",
        "UNSORTED_TIMESTAMPS",
        "OUT_OF_RANGE",
        "BOUNDARY_VIOLATION",
        "MISSING_HOURS",
        "PARTIAL_CANDLE",
        "MISSING_VALUES",
        "NON_POSITIVE_PRICE",
        "HIGH_INCONSISTENT",
        "LOW_INCONSISTENT",
        "NEGATIVE_VOLUME",
        "TAKER_VOLUME_EXCEEDS",
    }
    hard = [item for item in issues if item["code"] in hard_codes]
    if hard:
        return ChunkAssessment(STATUS_QUARANTINED, tuple(issues), gaps, int(len(frame)), expected_rows)

    review: list[dict[str, Any]] = []
    if bool(quality.get("zero_volume_requires_review", True)) and float(frame["volume"].sum()) == 0.0:
        review.append({"code": "ZERO_TOTAL_VOLUME", "detail": "every candle has zero volume"})
    if bool(quality.get("flat_ohlc_requires_review", True)) and len(frame) > 1:
        flat = ((frame["open"] == frame["high"]) & (frame["high"] == frame["low"]) & (frame["low"] == frame["close"]))
        if bool(flat.all()):
            review.append({"code": "FLAT_OHLC", "detail": "constant price across the whole chunk"})
    if review:
        return ChunkAssessment(STATUS_REQUIRES_REVIEW, tuple(issues + review), gaps, int(len(frame)), expected_rows)
    if issues:  # pragma: no cover - defensive: unknown soft issue
        return ChunkAssessment(STATUS_REQUIRES_REVIEW, tuple(issues), gaps, int(len(frame)), expected_rows)
    return ChunkAssessment(STATUS_VALID, (), (), int(len(frame)), expected_rows)


# ---------------------------------------------------------------------------
# immutable archive
# ---------------------------------------------------------------------------


def _try_lock(handle) -> bool:
    """Non-blocking exclusive lock on an open file (POSIX and Windows)."""

    if _fcntl is not None:
        try:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            return True
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                return False
            raise
    if _msvcrt is not None:  # pragma: no cover - exercised on Windows hosts
        try:
            handle.seek(0)
            _msvcrt.locking(handle.fileno(), _msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EDEADLOCK, errno.EAGAIN):
                return False
            raise
    raise OSError(errno.ENOTSUP, "no file locking primitive available")


def _unlock(handle) -> None:
    if _fcntl is not None:
        _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
    elif _msvcrt is not None:  # pragma: no cover - exercised on Windows hosts
        try:
            handle.seek(0)
            _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


class ForwardArchive:
    """Append-only forward archive.  Never overwrites a promoted object."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    # -- layout ------------------------------------------------------------
    @property
    def chunks_dir(self) -> Path:
        return self.root / "chunks"

    @property
    def quarantine_dir(self) -> Path:
        return self.root / "quarantine"

    @property
    def manifests_dir(self) -> Path:
        return self.root / "manifests"

    @property
    def registry_dir(self) -> Path:
        return self.root / "registry"

    @property
    def events_path(self) -> Path:
        return self.registry_dir / "events.jsonl"

    @property
    def lock_path(self) -> Path:
        return self.registry_dir / "sync.lock"

    def exists(self) -> bool:
        return self.root.is_dir()

    def ensure(self) -> None:
        for directory in (self.chunks_dir, self.quarantine_dir, self.manifests_dir, self.registry_dir):
            directory.mkdir(parents=True, exist_ok=True)

    # -- chunks ------------------------------------------------------------
    def chunk_paths(self, chunk_id: str) -> tuple[Path, Path]:
        return self.chunks_dir / f"{chunk_id}.parquet", self.chunks_dir / f"{chunk_id}.json"

    def quarantine_paths(self, chunk_id: str) -> tuple[Path, Path]:
        return self.quarantine_dir / f"{chunk_id}.parquet", self.quarantine_dir / f"{chunk_id}.json"

    def metas(self, *, quarantine: bool = False) -> list[dict[str, Any]]:
        directory = self.quarantine_dir if quarantine else self.chunks_dir
        if not directory.is_dir():
            return []
        rows = []
        for path in sorted(directory.glob("*.json")):
            try:
                rows.append(_read_json(path))
            except (OSError, ValueError):
                rows.append({"chunk_id": path.stem, "status": STATUS_INVALID, "broken_meta": True, "path": str(path)})
        return rows

    def events(self) -> list[dict[str, Any]]:
        if not self.events_path.is_file():
            return []
        rows = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def append_event(self, event: str, payload: Mapping[str, Any], *, run_id: str | None = None) -> dict[str, Any]:
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "schema_version": EVENT_SCHEMA,
            "event": event,
            "at_utc": _now().isoformat(),
            "run_id": run_id,
            "dataset_id": self.root.name,
            **dict(payload),
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(_canonical_json(record) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return record

    def _event_seen(self, event: str, key: str, value: Any) -> bool:
        return any(row.get("event") == event and row.get(key) == value for row in self.events())

    # -- promotion ---------------------------------------------------------
    def promote(
        self,
        frame: pd.DataFrame,
        meta: Mapping[str, Any],
    ) -> tuple[Path, bool]:
        """Write a chunk once.  Returns ``(path, created)``."""

        chunk_id = str(meta["chunk_id"])
        parquet_path, meta_path = self.chunk_paths(chunk_id)
        if parquet_path.exists() or meta_path.exists():
            existing = _read_json(meta_path) if meta_path.is_file() else {}
            if not parquet_path.is_file():
                raise ForwardConflictError(f"Chunk metadata without payload: {chunk_id}")
            if digest(parquet_path) != existing.get("sha256"):
                raise ForwardConflictError(f"Chunk payload hash mismatch: {chunk_id}")
            if existing.get("data_sha256") != meta.get("data_sha256"):
                raise ForwardConflictError(f"Chunk {chunk_id} already promoted with different data")
            return parquet_path, False
        self.chunks_dir.mkdir(parents=True, exist_ok=True)
        temp = parquet_path.with_name(parquet_path.name + ".tmp")
        try:
            frame.to_parquet(temp, index=False, engine="pyarrow", compression="zstd")
            sha = digest(temp)
            os.replace(temp, parquet_path)
        finally:
            temp.unlink(missing_ok=True)
        payload = {**dict(meta), "sha256": sha}
        try:
            _atomic_write_once(meta_path, payload)
        except ForwardConflictError:
            parquet_path.unlink(missing_ok=True)
            raise
        return parquet_path, True

    def quarantine(self, frame: pd.DataFrame, meta: Mapping[str, Any]) -> tuple[Path, bool]:
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        chunk_id = str(meta["chunk_id"])
        parquet_path, meta_path = self.quarantine_paths(chunk_id)
        if parquet_path.exists() and meta_path.exists():
            if digest(parquet_path) != _read_json(meta_path).get("sha256"):
                raise ForwardConflictError(f"Quarantine payload hash mismatch: {chunk_id}")
            return parquet_path, False
        temp = parquet_path.with_name(parquet_path.name + ".tmp")
        try:
            frame.to_parquet(temp, index=False, engine="pyarrow", compression="zstd")
            sha = digest(temp)
            os.replace(temp, parquet_path)
        finally:
            temp.unlink(missing_ok=True)
        _atomic_write_once(meta_path, {**dict(meta), "sha256": sha})
        return parquet_path, True

    def read_chunk(self, meta: Mapping[str, Any]) -> pd.DataFrame:
        path = self.chunks_dir / f"{meta['chunk_id']}.parquet"
        frame = pd.read_parquet(path)
        if not set(CHUNK_COLUMNS).issubset(frame.columns):
            raise ForwardDataError(f"Chunk {meta['chunk_id']} has an invalid schema")
        return frame

    # -- manifests ---------------------------------------------------------
    def manifest_versions(self) -> list[Path]:
        if not self.manifests_dir.is_dir():
            return []
        return sorted(self.manifests_dir.glob("dataset-manifest-*.json"))

    def latest_manifest(self) -> dict[str, Any] | None:
        pointer = self.manifests_dir / "latest.json"
        if not pointer.is_file():
            return None
        payload = _read_json(pointer)
        name = payload.get("manifest")
        if not name:
            return None
        path = self.manifests_dir / str(name)
        if not path.is_file():
            return None
        if digest(path) != payload.get("sha256"):
            raise ForwardConflictError(f"Latest manifest hash mismatch: {path}")
        return _read_json(path)

    def write_manifest(self, manifest: Mapping[str, Any]) -> tuple[Path, bool]:
        self.manifests_dir.mkdir(parents=True, exist_ok=True)
        version = len(self.manifest_versions()) + 1
        path = self.manifests_dir / f"dataset-manifest-{version:04d}.json"
        payload = {**dict(manifest), "manifest_version": version}
        created = _atomic_write_once(path, payload)
        body = (_canonical_json(payload) + "\n").encode("utf-8")
        _atomic_write_json(
            self.manifests_dir / "latest.json",
            {"manifest": path.name, "sha256": digest_bytes(body), "written_utc": _now().isoformat()},
        )
        return path, created

    # -- locking -----------------------------------------------------------
    def lock(self, *, timeout: float = 0.0):
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+")
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            if _try_lock(handle):
                handle.seek(0)
                handle.truncate()
                handle.write(_canonical_json({"pid": os.getpid(), "at_utc": _now().isoformat()}))
                handle.flush()
                return handle
            if time.monotonic() >= deadline:
                handle.close()
                raise ForwardLockedError(
                    f"Another forward collection run holds the lock: {self.lock_path}"
                )
            time.sleep(min(0.5, max(0.05, deadline - time.monotonic())))


# ---------------------------------------------------------------------------
# chunk planning / resolution
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cell:
    start_ms: int
    end_ms: int
    kind: str  # "grid" | "tail"

    @property
    def chunk_id(self) -> str:
        tag = "chunk" if self.kind == "grid" else "tail"
        return f"{tag}-{_stamp(self.start_ms)}_{_stamp(self.end_ms)}"

    @property
    def hours(self) -> int:
        return int((self.end_ms - self.start_ms) // HOUR_MS)


def _stamp(ms: int) -> str:
    return pd.Timestamp(int(ms), unit="ms", tz="UTC").strftime("%Y%m%dT%H%M")


def grid_cells(boundary_ms: int, closed_boundary_ms: int, chunk_ms: int) -> list[Cell]:
    """Aligned grid cells plus at most one trailing (shorter) cell."""

    if chunk_ms % HOUR_MS:
        raise ValueError("chunk size must be a whole number of hours")
    if boundary_ms % HOUR_MS or closed_boundary_ms % HOUR_MS:
        raise ValueError("hour boundaries required")
    cells: list[Cell] = []
    cursor = boundary_ms
    while cursor < closed_boundary_ms:
        end = min(cursor + chunk_ms, closed_boundary_ms)
        cells.append(Cell(cursor, end, "grid" if end - cursor == chunk_ms else "tail"))
        cursor = end
    return cells


@dataclass
class ArchiveView:
    """Consistent read-only view of the archive (no writes, no network)."""

    archive: ForwardArchive
    chunks: list[dict[str, Any]] = field(default_factory=list)
    quarantined: list[dict[str, Any]] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    @property
    def valid_chunks(self) -> list[dict[str, Any]]:
        return [row for row in self.chunks if row.get("status") == STATUS_VALID and row.get("integrity_ok")]

    def coverage_end_ms(self) -> int | None:
        ends = [int(row["end_ms"]) for row in self.valid_chunks]
        return max(ends) if ends else None

    def coverage_start_ms(self) -> int | None:
        starts = [int(row["start_ms"]) for row in self.valid_chunks]
        return min(starts) if starts else None

    def selected(self) -> list[dict[str, Any]]:
        """One chunk per grid cell: prefer the complete grid chunk, else the longest tail."""

        by_cell: dict[int, list[dict[str, Any]]] = {}
        for row in self.valid_chunks:
            by_cell.setdefault(int(row["cell_start_ms"]), []).append(row)
        selected = []
        for cell_start in sorted(by_cell):
            candidates = by_cell[cell_start]
            grid = [row for row in candidates if row.get("kind") == "grid"]
            chosen = max(grid, key=lambda row: int(row["end_ms"])) if grid else max(
                candidates, key=lambda row: int(row["end_ms"])
            )
            selected.append(chosen)
        return selected

    def times(self) -> set[int]:
        times: set[int] = set()
        for row in self.selected():
            frame = self.archive.read_chunk(row)
            times.update(int(value) for value in frame["open_time"].tolist())
        return times

    def frames(self) -> pd.DataFrame:
        parts = [self.archive.read_chunk(row) for row in self.selected()]
        if not parts:
            return pd.DataFrame(columns=CHUNK_COLUMNS)
        return pd.concat(parts, ignore_index=True).sort_values("open_time").reset_index(drop=True)


def load_archive_view(archive: ForwardArchive) -> ArchiveView:
    """Verify every minted object by hash and build the active view."""

    view = ArchiveView(archive=archive)
    if not archive.exists():
        return view
    for meta in archive.metas():
        chunk_id = str(meta.get("chunk_id", "?"))
        parquet_path, meta_path = archive.chunk_paths(chunk_id)
        if meta.get("broken_meta") or not meta_path.is_file() or not parquet_path.is_file():
            view.issues.append({"code": "CHUNK_META_BROKEN", "chunk_id": chunk_id})
            continue
        actual = digest(parquet_path)
        integrity = actual == meta.get("sha256")
        row = {
            **meta,
            "integrity_ok": integrity,
            "actual_sha256": actual,
            "path": str(parquet_path),
        }
        if integrity:
            view.chunks.append(row)
        else:
            view.issues.append(
                {
                    "code": "CHUNK_HASH_MISMATCH",
                    "chunk_id": chunk_id,
                    "expected": meta.get("sha256"),
                    "actual": actual,
                }
            )
            view.skipped.append(row)
    for meta in archive.metas(quarantine=True):
        chunk_id = str(meta.get("chunk_id", "?"))
        parquet_path, meta_path = archive.quarantine_paths(chunk_id)
        if not parquet_path.is_file() or not meta_path.is_file():
            view.issues.append({"code": "QUARANTINE_META_BROKEN", "chunk_id": chunk_id})
            continue
        actual = digest(parquet_path)
        if actual != meta.get("sha256"):
            view.issues.append(
                {"code": "QUARANTINE_HASH_MISMATCH", "chunk_id": chunk_id, "actual": actual}
            )
            continue
        view.quarantined.append({**meta, "path": str(parquet_path)})
    view.chunks.sort(key=lambda row: (int(row.get("cell_start_ms", 0)), int(row.get("end_ms", 0))))
    view.quarantined.sort(key=lambda row: int(row.get("start_ms", 0)))
    return view


def _overlap_rows(frame: pd.DataFrame, start_ms: int, end_ms: int) -> pd.DataFrame:
    times = frame["open_time"].astype("int64")
    return frame.loc[(times >= start_ms) & (times < end_ms)].reset_index(drop=True)


def _prefix_compatible(existing: pd.DataFrame, new: pd.DataFrame, *, start_ms: int, end_ms: int) -> tuple[bool, str | None]:
    left = _overlap_rows(existing, start_ms, end_ms)
    right = _overlap_rows(new, start_ms, end_ms)
    if left.empty and right.empty:
        return True, None
    if len(left) != len(right):
        return False, f"overlap row count differs ({len(left)} vs {len(right)})"
    if left["open_time"].tolist() != right["open_time"].tolist():
        return False, "overlap timestamps differ"
    for column in BASE_COLUMNS:
        if not (left[column].astype("float64") == right[column].astype("float64")).all():
            return False, f"overlap {column} differs"
    return True, None


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


@dataclass
class SyncResult:
    status: str
    run_id: str
    closed_boundary_ms: int
    cells: list[dict[str, Any]]
    promoted: int = 0
    reused: int = 0
    quarantined: int = 0
    conflicts: int = 0
    fetched: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)
    dataset_sha256: str | None = None
    manifest: str | None = None
    last_acquired_ms: int | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": STATUS_SCHEMA,
            "dataset_id": DATASET_ID,
            "status": self.status,
            "run_id": self.run_id,
            "closed_boundary_utc": _iso(self.closed_boundary_ms),
            "cells_total": len(self.cells),
            "cells_promoted": self.promoted,
            "cells_reused": self.reused,
            "cells_quarantined": self.quarantined,
            "cells_conflict": self.conflicts,
            "cells_fetched": self.fetched,
            "dataset_sha256": self.dataset_sha256,
            "last_acquired_utc": _iso(self.last_acquired_ms),
            "manifest": self.manifest,
            "error": self.error,
            "evaluation_executed": False,
            "strategic_gate": iv.VERDICT_NO_GO,
            "verdict": iv.VERDICT_NOT_EVALUATED,
        }


def _new_run_id() -> str:
    return f"sync-{_now().strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}"


def sync(
    config: ForwardConfig,
    *,
    archive: ForwardArchive | None = None,
    downloader: Callable[..., pd.DataFrame] = fetch_klines,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
    recheck_source: bool = False,
    run_id: str | None = None,
) -> SyncResult:
    """Incremental, idempotent, resumable collection of closed candles."""

    archive = archive or ForwardArchive(config.archive_dir)
    archive.ensure()
    # The collector is the legitimate writer of this directory, but the archive
    # must never be placed inside a research data directory.
    guard_archive_layout(archive.root, repo_root=config.paths.repo_root)
    if not is_forward_path(archive.root, repo_root=config.paths.repo_root):
        raise ForwardIsolationViolation(
            f"Forward archive must live under data/forward: {archive.root}"
        )
    run_id = run_id or _new_run_id()
    current = (now or _now()).astimezone(timezone.utc)
    closed_boundary_ms = _floor_hour_ms(current)
    result = SyncResult(status="SYNCED", run_id=run_id, closed_boundary_ms=closed_boundary_ms, cells=[])

    cells = grid_cells(config.boundary_ms, closed_boundary_ms, config.chunk_ms)
    if not cells:
        result.status = READINESS_NO_DATA
        archive.append_event(
            "SYNC_NO_DATA",
            {
                "boundary_utc": config.boundary_utc,
                "closed_boundary_utc": _iso(closed_boundary_ms),
                "reason": "the source has no closed candle after the frozen boundary yet",
            },
            run_id=run_id,
        )
        result.events.append({"event": "SYNC_NO_DATA"})
        return result

    view = load_archive_view(archive)
    resolved: dict[int, dict[str, Any]] = {}
    for row in view.selected():
        resolved[int(row["cell_start_ms"])] = row

    try:
        for cell in cells:
            existing = resolved.get(cell.start_ms)
            covered = bool(existing and int(existing["end_ms"]) >= cell.end_ms)
            if covered and not recheck_source:
                result.reused += 1
                result.cells.append(
                    {
                        "cell": cell.chunk_id,
                        "action": "REUSED",
                        "start_utc": _iso(cell.start_ms),
                        "end_utc": _iso(cell.end_ms),
                        "chunk_id": existing["chunk_id"],
                    }
                )
                result.last_acquired_ms = max(result.last_acquired_ms or 0, int(existing["end_ms"]))
                continue

            frame = _fetch_with_retry(config, cell, downloader=downloader, sleep=sleep)
            result.fetched += 1
            assessment = assess_chunk(
                frame,
                start_ms=cell.start_ms,
                end_ms=cell.end_ms,
                boundary_ms=config.boundary_ms,
                closed_boundary_ms=closed_boundary_ms,
                symbol=config.symbol,
                quality=config.quality,
            )
            normalized = None
            if frame is not None and len(frame):
                try:
                    normalized = canonical_frame(
                        frame,
                        symbol=config.symbol,
                        source_id=str(config.source["source_id"]),
                        data_source=str(config.source["data_source"]),
                    )
                except (ForwardDataError, ValueError, TypeError):
                    normalized = None

            action = _apply_cell(
                archive,
                config,
                cell,
                normalized,
                assessment,
                existing=existing,
                run_id=run_id,
                recheck_source=recheck_source,
                result=result,
            )
            result.cells.append(
                {
                    "cell": cell.chunk_id,
                    "action": action,
                    "start_utc": _iso(cell.start_ms),
                    "end_utc": _iso(cell.end_ms),
                    "quality": assessment.status,
                    "issues": [item["code"] for item in assessment.issues],
                }
            )
            if action in ("PROMOTED", "PROMOTED_TAIL"):
                result.last_acquired_ms = max(result.last_acquired_ms or 0, cell.end_ms)
            elif action == "CONFLICT" and existing is not None:
                result.last_acquired_ms = max(result.last_acquired_ms or 0, int(existing["end_ms"]))

            if config.pause_seconds > 0:
                sleep(config.pause_seconds)
    except (ForwardLockedError, ForwardIsolationViolation):
        raise
    except Exception as exc:  # noqa: BLE001 - explicit interruption record, then abort
        archive.append_event(
            "SYNC_INTERRUPTED",
            {
                "run_id": run_id,
                "closed_boundary_utc": _iso(closed_boundary_ms),
                "error": f"{type(exc).__name__}: {exc}",
                "cells_done": len(result.cells),
            },
            run_id=run_id,
        )
        result.status = "INTERRUPTED"
        result.error = f"{type(exc).__name__}: {exc}"
        result.dataset_sha256 = dataset_sha256(archive)
        return result

    manifest, manifest_path = build_manifest(archive, config, run_id=run_id)
    result.dataset_sha256 = manifest["dataset_sha256"]
    result.manifest = str(manifest_path)
    if result.last_acquired_ms is None:
        result.last_acquired_ms = manifest.get("coverage_end_ms")
    archive.append_event(
        "SYNC_COMPLETED",
        {
            "closed_boundary_utc": _iso(closed_boundary_ms),
            "cells_total": len(cells),
            "promoted": result.promoted,
            "reused": result.reused,
            "quarantined": result.quarantined,
            "conflicts": result.conflicts,
            "dataset_sha256": result.dataset_sha256,
            "manifest": manifest_path.name,
        },
        run_id=run_id,
    )
    result.events.append({"event": "SYNC_COMPLETED"})
    if result.promoted == 0 and result.reused == len(cells) and result.quarantined == 0 and result.conflicts == 0:
        result.status = "UP_TO_DATE"
    return result


def _fetch_with_retry(
    config: ForwardConfig,
    cell: Cell,
    *,
    downloader: Callable[..., pd.DataFrame],
    sleep: Callable[[float], None],
) -> pd.DataFrame:
    last_error: Exception | None = None
    attempts = max(1, int(config.max_attempts))
    for attempt in range(attempts):
        try:
            return downloader(cell.start_ms, cell.end_ms, symbol=config.symbol)
        except ForwardDataError:
            raise
        except Exception as exc:  # noqa: BLE001 - network/rate-limit surfaces are heterogeneous
            last_error = exc
            if attempt == attempts - 1:
                break
            backoff = config.backoff_seconds[min(attempt, len(config.backoff_seconds) - 1)]
            sleep(backoff)
    raise RuntimeError(
        f"source failed after {attempts} attempts for [{_iso(cell.start_ms)}, {_iso(cell.end_ms)}): "
        f"{type(last_error).__name__}: {last_error}"
    ) from last_error


def _apply_cell(
    archive: ForwardArchive,
    config: ForwardConfig,
    cell: Cell,
    frame: pd.DataFrame | None,
    assessment: ChunkAssessment,
    *,
    existing: Mapping[str, Any] | None,
    run_id: str,
    recheck_source: bool,
    result: SyncResult,
) -> str:
    """Persist one cell according to its quality and the immutability rules."""

    common = {
        "cell_start_ms": cell.start_ms,
        "cell_end_ms": cell.end_ms,
        "start_ms": cell.start_ms,
        "end_ms": cell.end_ms,
        "start_utc": _iso(cell.start_ms),
        "end_utc": _iso(cell.end_ms),
        "kind": cell.kind,
        "grid_complete": bool(cell.kind == "grid" and cell.end_ms - cell.start_ms == config.chunk_ms),
        "dataset_id": config.dataset_id,
        "symbol": config.symbol,
        "interval": config.interval,
        "dataset_schema_version": DATASET_SCHEMA,
        "boundary_utc": config.boundary_utc,
        "source": {
            "source_id": config.source["source_id"],
            "provider": config.source["provider"],
            "adapter": config.source["adapter"],
            "data_source": config.source["data_source"],
            "endpoint": config.source.get("endpoint"),
        },
        "protocol": {
            "protocol_id": config.protocol_meta.get("protocol_id"),
            "protocol_version": config.protocol_meta.get("protocol_version"),
            "config_sha256": config.protocol_meta.get("config_sha256"),
            "canonical_sha256": config.protocol_meta.get("canonical_sha256"),
        },
        "quality": assessment.as_dict(),
        "ingested_utc": _now().isoformat(),
    }

    if not assessment.promotable:
        status = assessment.status
        if frame is None or status == STATUS_INVALID:
            archive.append_event(
                "CHUNK_INVALID",
                {
                    "chunk_id": cell.chunk_id,
                    "status": status,
                    "issues": [item["code"] for item in assessment.issues],
                },
                run_id=run_id,
            )
            result.events.append({"event": "CHUNK_INVALID", "chunk_id": cell.chunk_id})
            return "INVALID"

        chunk_id = f"q-{cell.chunk_id}-{data_sha256(frame)[:12]}"
        meta = {
            "schema_version": CHUNK_SCHEMA,
            "chunk_id": chunk_id,
            "status": status,
            "rows": int(len(frame)),
            "expected_rows": assessment.expected_rows,
            "data_sha256": data_sha256(frame),
            "record_chain_sha256": record_chain_sha256(frame),
            "gaps": assessment.as_dict()["gaps"],
            **common,
        }
        path, created = archive.quarantine(frame, meta)
        if created:
            result.quarantined += 1
            archive.append_event(
                "CHUNK_QUARANTINED",
                {
                    "chunk_id": chunk_id,
                    "cell": cell.chunk_id,
                    "status": status,
                    "issues": [item["code"] for item in assessment.issues],
                    "gaps": len(assessment.gaps),
                    "path": str(path),
                },
                run_id=run_id,
            )
            result.events.append({"event": "CHUNK_QUARANTINED", "chunk_id": chunk_id})
        else:
            result.events.append({"event": "QUARANTINE_REUSED", "chunk_id": chunk_id})
        return status

    assert frame is not None
    new_data_hash = data_sha256(frame)
    if existing is not None:
        previous = archive.read_chunk(existing)
        compatible, reason = _prefix_compatible(
            previous, frame, start_ms=int(existing["start_ms"]), end_ms=int(existing["end_ms"])
        )
        if not compatible:
            result.conflicts += 1
            if not recheck_source or not archive._event_seen("SOURCE_DISCREPANCY", "chunk_id", cell.chunk_id):
                archive.append_event(
                    "SOURCE_DISCREPANCY",
                    {
                        "chunk_id": cell.chunk_id,
                        "existing_chunk_id": existing["chunk_id"],
                        "existing_data_sha256": existing.get("data_sha256"),
                        "incoming_data_sha256": new_data_hash,
                        "reason": reason,
                        "promotion_blocked": True,
                    },
                    run_id=run_id,
                )
            result.events.append({"event": "SOURCE_DISCREPANCY", "chunk_id": cell.chunk_id})
            return "CONFLICT"

    meta = {
        "schema_version": CHUNK_SCHEMA,
        "chunk_id": cell.chunk_id,
        "status": STATUS_VALID,
        "rows": int(len(frame)),
        "expected_rows": assessment.expected_rows,
        "data_sha256": new_data_hash,
        "record_chain_sha256": record_chain_sha256(frame),
        "gaps": [],
        **common,
    }
    path, created = archive.promote(frame, meta)
    if created:
        result.promoted += 1
        archive.append_event(
            "CHUNK_PROMOTED",
            {
                "chunk_id": cell.chunk_id,
                "kind": cell.kind,
                "rows": int(len(frame)),
                "data_sha256": new_data_hash,
                "sha256": digest(path),
                "start_utc": _iso(cell.start_ms),
                "end_utc": _iso(cell.end_ms),
            },
            run_id=run_id,
        )
        result.events.append({"event": "CHUNK_PROMOTED", "chunk_id": cell.chunk_id})
        _resolve_quarantine_for_cell(archive, cell, run_id=run_id, result=result)
        return "PROMOTED" if cell.kind == "grid" else "PROMOTED_TAIL"
    result.reused += 1
    return "REUSED"


def _resolve_quarantine_for_cell(
    archive: ForwardArchive, cell: Cell, *, run_id: str, result: SyncResult
) -> None:
    """Mark an existing quarantine object as resolved (historical record, never deleted)."""

    for meta in archive.metas(quarantine=True):
        if int(meta.get("cell_start_ms", -1)) != cell.start_ms:
            continue
        if archive._event_seen("QUARANTINE_RESOLVED", "quarantine_chunk_id", meta["chunk_id"]):
            continue
        archive.append_event(
            "QUARANTINE_RESOLVED",
            {
                "quarantine_chunk_id": meta["chunk_id"],
                "cell": cell.chunk_id,
                "resolved_by": cell.chunk_id,
            },
            run_id=run_id,
        )
        result.events.append({"event": "QUARANTINE_RESOLVED", "chunk_id": meta["chunk_id"]})


# ---------------------------------------------------------------------------
# dataset hash, manifest
# ---------------------------------------------------------------------------


def dataset_sha256(archive: ForwardArchive) -> str | None:
    """Stable dataset hash over the active chunk chain (timestamps excluded)."""

    view = load_archive_view(archive)
    selected = view.selected()
    if not selected:
        return None
    return _hash_obj(
        {
            "dataset_id": archive.root.name,
            "schema_version": DATASET_SCHEMA,
            "chunks": [[row["chunk_id"], row["data_sha256"]] for row in selected],
        }
    )


def build_manifest(
    archive: ForwardArchive, config: ForwardConfig, *, run_id: str | None = None
) -> tuple[dict[str, Any], Path]:
    view = load_archive_view(archive)
    selected = view.selected()
    times: list[int] = []
    for row in selected:
        frame = archive.read_chunk(row)
        times.extend(int(value) for value in frame["open_time"].tolist())
    times_sorted = sorted(times)
    expected: list[int] = []
    if times_sorted:
        expected = list(range(times_sorted[0], times_sorted[-1] + HOUR_MS, HOUR_MS))
    missing = sorted(set(expected) - set(times_sorted))
    duplicates = len(times_sorted) - len(set(times_sorted))
    dataset_hash = (
        _hash_obj(
            {
                "dataset_id": config.dataset_id,
                "schema_version": DATASET_SCHEMA,
                "chunks": [[row["chunk_id"], row["data_sha256"]] for row in selected],
            }
        )
        if selected
        else None
    )
    manifest = {
        "schema_version": MANIFEST_SCHEMA,
        "dataset_id": config.dataset_id,
        "dataset_role": FORWARD_ROLE,
        "created_utc": _now().isoformat(),
        "run_id": run_id,
        "boundary_utc": config.boundary_utc,
        "symbol": config.symbol,
        "interval": config.interval,
        "chunk_hours": config.chunk_hours,
        "dataset_sha256": dataset_hash,
        "chunk_chain_sha256": _hash_obj({"chain": [row["data_sha256"] for row in selected]}),
        "coverage": {
            "start_utc": _iso(times_sorted[0]) if times_sorted else None,
            "end_utc": _iso(times_sorted[-1]) if times_sorted else None,
            "end_exclusive_utc": _iso(times_sorted[-1] + HOUR_MS) if times_sorted else None,
            "hours": len(times_sorted),
            "rows": len(times_sorted),
        },
        "coverage_start_ms": times_sorted[0] if times_sorted else None,
        "coverage_end_ms": times_sorted[-1] + HOUR_MS if times_sorted else None,
        "gaps": _ranges_payload(missing),
        "missing_hours": len(missing),
        "duplicate_timestamps": int(duplicates),
        "chunks": [
            {
                "chunk_id": row["chunk_id"],
                "kind": row.get("kind"),
                "start_utc": row.get("start_utc"),
                "end_utc": row.get("end_utc"),
                "rows": int(row.get("rows", 0)),
                "sha256": row.get("sha256"),
                "data_sha256": row.get("data_sha256"),
                "ingested_utc": row.get("ingested_utc"),
                "source_id": (row.get("source") or {}).get("source_id"),
                "status": row.get("status"),
                "schema_version": row.get("schema_version"),
            }
            for row in selected
        ],
        "quarantine": [
            {
                "chunk_id": row["chunk_id"],
                "start_utc": row.get("start_utc"),
                "end_utc": row.get("end_utc"),
                "status": row.get("status"),
                "issues": [item.get("code") for item in (row.get("quality") or {}).get("issues", [])],
                "sha256": row.get("sha256"),
            }
            for row in view.quarantined
        ],
        "conflicts": [
            {
                "chunk_id": row.get("chunk_id"),
                "event": row.get("event"),
                "reason": row.get("reason"),
                "at_utc": row.get("at_utc"),
            }
            for row in archive.events()
            if row.get("event") in ("SOURCE_DISCREPANCY", "CHUNK_HASH_MISMATCH")
        ],
        "protocol": dict(config.protocol_meta),
        "requirements": {
            key: config.readiness.get(key)
            for key in ("min_windows", "min_folds", "min_days", "min_trades", "stride_hours")
        },
        "research_use_forbidden": True,
        "strategy_metrics_present": False,
        "evaluation_executed": False,
        "final_evaluation_gate": "NOT_ACTIVATED",
        "strategic_gate": iv.VERDICT_NO_GO,
        "verdict": iv.VERDICT_NOT_EVALUATED,
    }
    latest = None
    try:
        latest = archive.latest_manifest()
    except ForwardConflictError:
        latest = None
    if (
        latest
        and latest.get("dataset_sha256") == manifest["dataset_sha256"]
        and latest.get("chunk_chain_sha256") == manifest["chunk_chain_sha256"]
    ):
        # Nothing changed: keep the existing immutable manifest (no churn).
        return latest, archive.manifests_dir / f"dataset-manifest-{int(latest['manifest_version']):04d}.json"
    path, _created = archive.write_manifest(manifest)
    return manifest, path


def registration_candidate(
    config: ForwardConfig, archive: ForwardArchive | None = None
) -> dict[str, Any]:
    """The immutable record that a *future* validation may register.

    Nothing is written here: registering a partial dataset would pin its hash and
    block the final one.  The record is exposed so that registration is a single,
    auditable act once the sample is complete.
    """

    archive = archive or ForwardArchive(config.archive_dir)
    view = load_archive_view(archive)
    selected = view.selected()
    dataset_hash = dataset_sha256(archive)
    first = _iso(int(selected[0]["start_ms"])) if selected else None
    last = _iso(int(selected[-1]["end_ms"])) if selected else None
    return {
        "dataset_id": config.dataset_id,
        "role": "independent",
        "sha256": dataset_hash,
        "schema_version": DATASET_SCHEMA,
        "chunks": len(selected),
        "first_bar_utc": first,
        "last_bar_end_utc": last,
        "boundary_utc": config.boundary_utc,
        "protocol": {
            "protocol_id": config.protocol_meta.get("protocol_id"),
            "protocol_version": config.protocol_meta.get("protocol_version"),
            "canonical_sha256": config.protocol_meta.get("canonical_sha256"),
        },
        "registered": False,
        "registration_deferred_reason": (
            "registration happens once, with the final dataset: pinning a partial "
            "hash would prevent the complete dataset from being registered"
        ),
        "registration_api": "crypto_decision_lab.independent_validation.ValidationRegistry.register_dataset",
    }


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def status(
    config: ForwardConfig,
    *,
    archive: ForwardArchive | None = None,
    now: datetime | None = None,
    paths: ForwardPaths | None = None,
) -> dict[str, Any]:
    """Offline archive status.  Never touches the network and never writes."""

    archive = archive or ForwardArchive(config.archive_dir)
    view = load_archive_view(archive)
    current = (now or _now()).astimezone(timezone.utc)
    closed_boundary_ms = _floor_hour_ms(current)
    last_acquired = view.coverage_end_ms()
    expected_hours = 0
    if closed_boundary_ms > config.boundary_ms:
        expected_hours = int((closed_boundary_ms - config.boundary_ms) // HOUR_MS)
    observed = view.times()
    acquired_hours = len(observed)
    missing = [
        value
        for value in range(config.boundary_ms, max(config.boundary_ms, last_acquired or config.boundary_ms), HOUR_MS)
        if value not in observed
    ]
    pending_cells = grid_cells(config.boundary_ms, closed_boundary_ms, config.chunk_ms)
    pending = [
        cell
        for cell in pending_cells
        if last_acquired is None or cell.end_ms > last_acquired
    ]
    events = archive.events()
    last_event = events[-1] if events else None
    latest = None
    try:
        latest = archive.latest_manifest()
    except ForwardConflictError as exc:
        view.issues.append({"code": "MANIFEST_HASH_MISMATCH", "detail": str(exc)})

    ready = readiness(
        config,
        archive=archive,
        now=current,
        paths=paths or config.paths,
        view=view,
        closed_boundary_ms=closed_boundary_ms,
    )
    if not view.selected():
        acquisition_status = "EMPTY"
    elif view.issues or view.quarantined or missing or ready["status"] == READINESS_DATA_READY:
        acquisition_status = "DEGRADED" if view.issues or view.quarantined or missing else "OK"
    else:
        acquisition_status = "OK"
    payload = {
        "schema_version": STATUS_SCHEMA,
        "dataset_id": config.dataset_id,
        "dataset_role": FORWARD_ROLE,
        "status": acquisition_status,
        "generated_utc": _now().isoformat(),
        "archive_root": str(archive.root),
        "archive_present": archive.exists(),
        "boundary_utc": config.boundary_utc,
        "last_closed_bar_utc": _iso(closed_boundary_ms - HOUR_MS),
        "last_acquired_bar_utc": _iso(last_acquired - HOUR_MS) if last_acquired else None,
        "last_acquired_end_utc": _iso(last_acquired),
        "hours_available": acquired_hours,
        "hours_expected_to_date": expected_hours,
        "coverage_pct": round(100.0 * acquired_hours / expected_hours, 4) if expected_hours else 0.0,
        "hours_not_yet_acquired": max(0, expected_hours - acquired_hours - len(missing)),
        "valid_chunks": len(view.selected()),
        "valid_intervals": len(view.valid_chunks),
        "quarantined_chunks": len(view.quarantined),
        "conflict_events": sum(1 for row in events if row.get("event") == "SOURCE_DISCREPANCY"),
        "gaps": _ranges_payload(missing),
        "gap_hours": len(missing),
        "pending_cells": [
            {"cell": cell.chunk_id, "start_utc": _iso(cell.start_ms), "end_utc": _iso(cell.end_ms)}
            for cell in pending
        ],
        "pending_hours": int(sum(cell.hours for cell in pending)),
        "quarantine": [
            {
                "chunk_id": row["chunk_id"],
                "status": row.get("status"),
                "start_utc": row.get("start_utc"),
                "end_utc": row.get("end_utc"),
                "issues": [item.get("code") for item in (row.get("quality") or {}).get("issues", [])],
            }
            for row in view.quarantined
        ],
        "integrity_issues": list(view.issues),
        "dataset_sha256": latest.get("dataset_sha256") if latest else None,
        "manifest": {
            "version": latest.get("manifest_version") if latest else None,
            "created_utc": latest.get("created_utc") if latest else None,
            "latest_path": str(archive.manifests_dir / "latest.json") if latest else None,
        },
        "last_run": last_event,
        "readiness_status": ready["status"],
        "protocol": dict(config.protocol_meta),
        "isolation": ready["isolation"],
        "evaluation_executed": False,
        "final_evaluation_gate": "NOT_ACTIVATED",
        "strategic_gate": iv.VERDICT_NO_GO,
        "verdict": iv.VERDICT_NOT_EVALUATED,
    }
    return payload


# ---------------------------------------------------------------------------
# readiness
# ---------------------------------------------------------------------------


def _snapshot_warmup_times(
    config: ForwardConfig, *, lookback_hours: int, issues: list[dict[str, Any]]
) -> set[int]:
    """Bar open_times from the frozen snapshot inside the warm-up window.

    The protocol allows warm-up bars to belong to the frozen snapshot; they are
    never written into the forward archive and never counted as independent
    evidence.  Only the snapshot chunk files intersecting the window are read.
    """

    window_start = config.boundary_ms - lookback_hours * HOUR_MS
    manifest_path = config.snapshot_manifest
    if not manifest_path.is_file():
        issues.append({"code": "SNAPSHOT_MANIFEST_MISSING", "path": str(manifest_path)})
        return set()
    manifest = _read_json(manifest_path)
    times: set[int] = set()
    for entry in manifest.get("chunks", []):
        start_ms = int(entry.get("start_ms", 0))
        end_ms = int(entry.get("end_ms", 0))
        if end_ms <= window_start or start_ms >= config.boundary_ms:
            continue
        path = config.data_root / entry["file"]
        if not path.is_file():
            issues.append({"code": "SNAPSHOT_CHUNK_MISSING", "file": entry["file"]})
            continue
        if digest(path) != entry.get("sha256"):
            issues.append({"code": "SNAPSHOT_CHUNK_HASH_MISMATCH", "file": entry["file"]})
            continue
        frame = pd.read_parquet(path)
        open_times = frame["open_time"].astype("int64")
        for value in open_times.tolist():
            if window_start <= value < config.boundary_ms:
                times.add(int(value))
    return times


def eligible_windows(
    available_times: Iterable[int],
    *,
    boundary_ms: int,
    last_bar_open_ms: int | None,
    stride_hours: int,
    lookback_hours: int,
    latency_hours: int,
    horizon_hours: int,
) -> list[dict[str, Any]]:
    """Pre-registered CP07 window contract, computed on available bars only.

    A window is eligible only when *every* bar it needs is present and
    contiguous: ``[cutoff - lookback, cutoff + latency + horizon]``.  Gaps and
    quarantined intervals therefore break eligibility instead of being bridged.
    """

    times = set(int(value) for value in available_times)
    if last_bar_open_ms is None:
        return []
    stride_ms = int(stride_hours) * HOUR_MS
    required_end = int(latency_hours) + int(horizon_hours)
    windows: list[dict[str, Any]] = []
    cutoff = boundary_ms
    while cutoff + required_end * HOUR_MS <= last_bar_open_ms:
        first = cutoff - int(lookback_hours) * HOUR_MS
        needed = range(first, cutoff + (required_end + 1) * HOUR_MS, HOUR_MS)
        if all(value in times for value in needed):
            windows.append(
                {
                    "cutoff_utc": _iso(cutoff),
                    "entry_utc": _iso(cutoff + int(latency_hours) * HOUR_MS),
                    "exit_utc": _iso(cutoff + required_end * HOUR_MS),
                    "feature_start_utc": _iso(first),
                }
            )
        cutoff += stride_ms
    return windows


def readiness(
    config: ForwardConfig,
    *,
    archive: ForwardArchive | None = None,
    now: datetime | None = None,
    paths: ForwardPaths | None = None,
    view: ArchiveView | None = None,
    closed_boundary_ms: int | None = None,
    include_windows: bool = True,
) -> dict[str, Any]:
    """Data availability only.  No MAE, no P&L, no signal, no verdict.

    The function is offline, read-only and structurally unable to evaluate the
    strategy: it imports no metric code and never calls
    :func:`crypto_decision_lab.independent_validation.evaluate_independent`.
    """

    paths = paths or config.paths
    archive = archive or ForwardArchive(config.archive_dir)
    view = view or load_archive_view(archive)
    current = (now or _now()).astimezone(timezone.utc)
    closed_boundary_ms = closed_boundary_ms or _floor_hour_ms(current)
    last_bar_open = closed_boundary_ms - HOUR_MS if closed_boundary_ms > config.boundary_ms else None

    issues: list[dict[str, Any]] = []
    requirement = config.readiness
    lookback = int(config.protocol["independent_validation"]["feature_warmup_hours"])
    latency = int(config.protocol["primary_scenario"]["latency_hours"])
    horizon = int(config.protocol["frozen_benchmark"]["horizon_hours"])
    stride = int(requirement.get("stride_hours", 24))
    min_windows = int(requirement.get("min_windows", 300))
    min_folds = int(requirement.get("min_folds", 5))
    min_days = int(requirement.get("min_days", 180))
    min_trades = int(requirement.get("min_trades", 100))

    forward_times = view.times()
    warmup_times = _snapshot_warmup_times(config, lookback_hours=lookback, issues=issues)
    available = forward_times | warmup_times

    windows = (
        eligible_windows(
            available,
            boundary_ms=config.boundary_ms,
            last_bar_open_ms=last_bar_open,
            stride_hours=stride,
            lookback_hours=lookback,
            latency_hours=latency,
            horizon_hours=horizon,
        )
        if include_windows
        else []
    )
    possible_folds = min(
        int(config.protocol["walk_forward"]["folds_max"]),
        len(windows) // int(config.protocol["walk_forward"]["test_windows_per_fold"]),
    )
    first_entry = windows[0]["entry_utc"] if windows else None
    last_exit = windows[-1]["exit_utc"] if windows else None
    days = 0.0
    if first_entry and last_exit:
        days = (
            pd.Timestamp(last_exit) - pd.Timestamp(first_entry)
        ).total_seconds() / 86400.0

    observed = sorted(forward_times)
    gaps = []
    if observed:
        expected = range(observed[0], observed[-1] + HOUR_MS, HOUR_MS)
        gaps = [value for value in expected if value not in forward_times]

    protocol = dict(config.protocol_meta)
    research_last = pd.Timestamp(config.protocol["research_phase"]["last_used_target_end_utc"])
    isolation = {
        "forward_role": FORWARD_ROLE,
        "research_use": "FORBIDDEN",
        "forward_archive_path": str(archive.root),
        "forward_separated_from_research_dirs": not is_forward_path(
            config.data_root, repo_root=paths.repo_root
        ),
        "boundary_after_research_target_end": bool(
            pd.Timestamp(config.boundary_utc) > research_last
        ),
        "protocol_hash_ok": bool(protocol.get("hash_ok")),
        "protocol_archive_match": protocol.get("archive_manifest_match"),
        "final_evaluation_consumed": False,
        "dataset_registered": False,
        "threshold_selection_on_forward": False,
        "strategy_metrics_present": False,
    }
    isolation["all_ok"] = bool(
        isolation["forward_separated_from_research_dirs"]
        and isolation["boundary_after_research_target_end"]
        and isolation["protocol_hash_ok"]
        and isolation["protocol_archive_match"] is not False
        and not isolation["final_evaluation_consumed"]
        and not isolation["threshold_selection_on_forward"]
    )
    try:
        registry = iv.ValidationRegistry(paths.resolve(paths.registry_dir))
        registry_state = registry.state(protocol.get("canonical_sha256") or "")
        isolation["validation_registry"] = registry_state
        isolation["final_evaluation_consumed"] = bool(registry_state["final_consumed"])
        isolation["all_ok"] = bool(isolation["all_ok"] and not registry_state["final_consumed"])
    except Exception as exc:  # noqa: BLE001 - a broken registry must be visible
        isolation["validation_registry_error"] = f"{type(exc).__name__}: {exc}"
        isolation["all_ok"] = False

    checks = [
        {
            "id": "windows",
            "label": "Finestre indipendenti eleggibili (lookback 168h, hold 24h, latenza 1h)",
            "required": min_windows,
            "observed": len(windows),
            "met": len(windows) >= min_windows,
        },
        {
            "id": "folds",
            "label": "Fold walk-forward possibili (blocchi da 60 finestre)",
            "required": min_folds,
            "observed": possible_folds,
            "met": possible_folds >= min_folds,
        },
        {
            "id": "days",
            "label": "Giorni indipendenti coperti",
            "required": min_days,
            "observed": round(days, 4),
            "met": days >= min_days,
        },
        {
            "id": "trades",
            "label": "Trade eseguiti (misurabile solo con la valutazione finale)",
            "required": min_trades,
            "observed": None,
            "met": False,
            "note": "not measurable before the frozen protocol evaluation",
        },
        {
            "id": "protocol",
            "label": "Hash del protocollo congelato e archivio CP08-04",
            "required": True,
            "observed": bool(protocol.get("hash_ok")),
            "met": bool(protocol.get("hash_ok")),
        },
    ]
    data_ready = all(item["met"] for item in checks if item["id"] in ("windows", "folds", "days", "protocol"))
    if not observed:
        state = READINESS_NO_DATA
    elif data_ready:
        state = READINESS_DATA_READY
    else:
        state = READINESS_NOT_READY

    candidate = registration_candidate(config, archive)
    missing_requirements = [
        {
            "id": item["id"],
            "label": item["label"],
            "required": item["required"],
            "observed": item["observed"],
            "missing": (
                None
                if item["met"] or item["observed"] is None
                else item["required"] - item["observed"]
            ),
        }
        for item in checks
        if not item["met"]
    ]

    return {
        "schema_version": READINESS_SCHEMA,
        "dataset_id": config.dataset_id,
        "dataset_role": FORWARD_ROLE,
        "generated_utc": _now().isoformat(),
        "status": state,
        "boundary_utc": config.boundary_utc,
        "last_closed_bar_utc": _iso(last_bar_open),
        "last_acquired_bar_utc": _iso(observed[-1]) if observed else None,
        "hours_available": len(observed),
        "hours_expected_to_date": (
            int((closed_boundary_ms - config.boundary_ms) // HOUR_MS)
            if closed_boundary_ms > config.boundary_ms
            else 0
        ),
        "valid_intervals": len(view.selected()),
        "quarantined_chunks": len(view.quarantined),
        "gaps": _ranges_payload(gaps),
        "gap_hours": len(gaps),
        "coverage": {
            "start_utc": _iso(observed[0]) if observed else None,
            "end_exclusive_utc": _iso(observed[-1] + HOUR_MS) if observed else None,
        },
        "window_contract": {
            "method": "cp07_contract_hourly_contiguous",
            "cutoff_grid_anchor_utc": config.boundary_utc,
            "stride_hours": stride,
            "lookback_hours": lookback,
            "latency_hours": latency,
            "horizon_hours": horizon,
            "purge_hours": int(config.protocol["walk_forward"]["purge_hours"]),
            "warmup_from_frozen_snapshot": bool(warmup_times),
            "gap_crossing_allowed": False,
        },
        "eligible_windows": {
            "count": len(windows),
            "first": windows[0] if windows else None,
            "last": windows[-1] if windows else None,
        },
        "folds": {
            "possible": possible_folds,
            "required": min_folds,
            "test_windows_per_fold": int(config.protocol["walk_forward"]["test_windows_per_fold"]),
            "folds_max": int(config.protocol["walk_forward"]["folds_max"]),
            "min_train_windows": int(config.protocol["walk_forward"]["min_train_windows"]),
            "research_history_windows": int(config.protocol["research_phase"]["windows"]),
        },
        "independent_days": {"available": round(days, 4), "required": min_days},
        "sample_requirements": checks,
        "missing_requirements": missing_requirements,
        "dataset_sha256": candidate["sha256"],
        "registration_candidate": candidate,
        "data_issues": issues,
        "protocol": protocol,
        "isolation": isolation,
        "evaluation_executed": False,
        "final_evaluation_gate": "NOT_ACTIVATED",
        "dataset_registered": False,
        "strategic_gate": iv.VERDICT_NO_GO,
        "independent_validation": iv.VERDICT_NOT_EVALUATED,
        "verdict": iv.VERDICT_NOT_EVALUATED,
        "disclaimer": "Solo disponibilità dei dati: nessuna metrica di strategia è calcolata.",
    }


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------


def _audit_check(
    check_id: str,
    label: str,
    passed: bool,
    detail: str,
    *,
    status: str | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "label": label,
        "status": status or (iv.STATUS_PASS if passed else iv.STATUS_FAIL),
        "passed": bool(passed),
        "detail": detail,
        "evidence": dict(evidence or {}),
    }


def audit(
    config: ForwardConfig,
    *,
    archive: ForwardArchive | None = None,
    now: datetime | None = None,
    paths: ForwardPaths | None = None,
) -> dict[str, Any]:
    """Offline verification of hashes, boundary, quality, provenance and isolation."""

    paths = paths or config.paths
    archive = archive or ForwardArchive(config.archive_dir)
    view = load_archive_view(archive)
    current = (now or _now()).astimezone(timezone.utc)
    closed_boundary_ms = _floor_hour_ms(current)
    events = archive.events()
    frames = view.frames()
    checks: list[dict[str, Any]] = []

    hashes_ok = all(row.get("integrity_ok") for row in view.chunks) and not any(
        row.get("code") in ("CHUNK_HASH_MISMATCH", "QUARANTINE_HASH_MISMATCH") for row in view.issues
    )
    checks.append(
        _audit_check(
            "FWD.1",
            "Ogni chunk promosso corrisponde al proprio SHA-256",
            hashes_ok,
            "Ricalcolo dei digest su tutti i chunk e gli oggetti in quarantena.",
            evidence={
                "chunks": len(view.chunks),
                "quarantined": len(view.quarantined),
                "integrity_issues": view.issues,
            },
        )
    )

    schema_ok = True
    schema_detail: list[str] = []
    for row in view.selected():
        frame = archive.read_chunk(row)
        if list(frame.columns) != CHUNK_COLUMNS:
            schema_ok = False
            schema_detail.append(f"{row['chunk_id']}: columns differ")
        elif list(frame["symbol"].unique()) != [config.symbol]:
            schema_ok = False
            schema_detail.append(f"{row['chunk_id']}: unexpected symbol")
        elif (frame["timestamp"] != pd.to_datetime(frame["open_time"], unit="ms", utc=True)).any():
            schema_ok = False
            schema_detail.append(f"{row['chunk_id']}: timestamp/open_time mismatch")
    checks.append(
        _audit_check(
            "FWD.2",
            "Schema, provenienza e checksum di riga coerenti",
            schema_ok,
            "Colonne canoniche, simbolo dichiarato, timestamp derivato e checksum per riga.",
            evidence={"issues": schema_detail, "columns": CHUNK_COLUMNS},
        )
    )

    times = sorted(int(value) for value in frames["open_time"].tolist()) if len(frames) else []
    boundary_ok = all(value >= config.boundary_ms for value in times)
    checks.append(
        _audit_check(
            "FWD.3",
            "Nessuna candela precedente al boundary congelato",
            boundary_ok,
            f"Prima barra archiviata: {_iso(times[0]) if times else 'nessuna'} · boundary {config.boundary_utc}",
            evidence={
                "first_bar_utc": _iso(times[0]) if times else None,
                "boundary_utc": config.boundary_utc,
                "bars": len(times),
            },
        )
    )

    partial = (
        int((frames["close_time"].astype("int64") != frames["open_time"].astype("int64") + HOUR_MS - 1).sum())
        if len(frames)
        else 0
    )
    duplicates = len(times) - len(set(times)) if times else 0
    aligned = all(value % HOUR_MS == 0 for value in times)
    checks.append(
        _audit_check(
            "FWD.4",
            "Candele complete, orarie, senza duplicati e senza interpolazioni",
            bool(partial == 0 and duplicates == 0 and aligned),
            "Verifica di close_time, allineamento orario e unicità dei timestamp.",
            evidence={"partial_candles": partial, "duplicates": duplicates, "aligned": aligned},
        )
    )

    gap_hours = 0
    if times:
        expected_hours = list(range(times[0], times[-1] + HOUR_MS, HOUR_MS))
        gap_hours = len(expected_hours) - len(set(times))
    gap_events = [row for row in events if row.get("event") in ("CHUNK_QUARANTINED", "CHUNK_INVALID")]
    gaps_registered = gap_hours == 0 or bool(gap_events)
    checks.append(
        _audit_check(
            "FWD.5",
            "Ogni gap è tracciato nel registro append-only",
            gaps_registered,
            "Un buco nella copertura può esistere solo con un evento di quarantena/registrazione.",
            evidence={
                "gap_hours": gap_hours,
                "quarantine_events": len(gap_events),
            },
        )
    )

    quarantine_state = []
    for row in view.quarantined:
        issues = [item.get("code") for item in (row.get("quality") or {}).get("issues", [])]
        quarantine_state.append({"chunk_id": row["chunk_id"], "status": row.get("status"), "issues": issues})
        if row.get("status") not in (STATUS_QUARANTINED, STATUS_REQUIRES_REVIEW):
            quarantine_state[-1]["unexpected_status"] = True
    checks.append(
        _audit_check(
            "FWD.6",
            "Quarantena classificata, conservata e mai promossa",
            all("unexpected_status" not in item for item in quarantine_state),
            "Ogni oggetto in quarantena è hash-verificato, classificato e assente dai chunk promossi.",
            evidence={"quarantined": quarantine_state},
        )
    )

    protocol = dict(config.protocol_meta)
    checks.append(
        _audit_check(
            "FWD.7",
            "Hash del protocollo congelato e allineamento con l'archivio CP08-04",
            bool(protocol.get("sidecar_match") and protocol.get("archive_manifest_match") is not False),
            "Il sidecar .sha256 e il manifest CP08-04 confermano la stessa versione di protocollo.",
            evidence=protocol,
        )
    )

    strategy_artifacts = guard_artefact_names(
        [
            path
            for directory in (archive.chunks_dir, archive.quarantine_dir, archive.manifests_dir, archive.registry_dir)
            if directory.is_dir()
            for path in directory.iterdir()
        ]
    )
    forbidden_events = [
        row for row in events if row.get("event") in ("EVALUATION", "THRESHOLD_SELECTION", "STRATEGY_METRICS")
    ]
    checks.append(
        _audit_check(
            "FWD.8",
            "Nessuna metrica di strategia o selezione di soglia nell'archivio forward",
            bool(not strategy_artifacts and not forbidden_events),
            "L'archivio contiene solo barre OHLCV, metadati, manifest e registry operativo.",
            evidence={"strategy_artifacts": strategy_artifacts, "forbidden_events": len(forbidden_events)},
        )
    )

    research_violations: list[str] = []
    research_missing: list[str] = []
    research_last = pd.Timestamp(config.protocol["research_phase"]["last_used_target_end_utc"])
    frozen = config.protocol.get("frozen_artifacts", [])
    for item in frozen:
        path = paths.resolve(item["path"])
        if is_forward_path(path, repo_root=paths.repo_root):
            research_violations.append(f"frozen artifact inside forward archive: {item['path']}")
            continue
        if not path.is_file():
            research_missing.append(item["path"])
            continue
        if digest(path) != item["sha256"]:
            research_violations.append(f"frozen artifact hash changed: {item['path']}")
    research_status = (
        iv.STATUS_FAIL
        if research_violations
        else (iv.STATUS_WARN if research_missing else iv.STATUS_PASS)
    )
    checks.append(
        _audit_check(
            "FWD.9",
            "Artefatti di ricerca congelati invariati e disgiunti dall'archivio forward",
            not research_violations,
            "Hash degli artefatti CP07/CP08 ricalcolati quando presenti; nessuno vive in data/forward.",
            status=research_status,
            evidence={
                "artifacts": len(frozen),
                "verified": len(frozen) - len(research_missing) - len(research_violations),
                "missing_in_this_repository": research_missing,
                "violations": research_violations,
                "research_last_used_target_end_utc": research_last.isoformat(),
            },
        )
    )

    isolation_ok, registry_state = _registry_isolation(paths, protocol)
    checks.append(
        _audit_check(
            "FWD.10",
            "Registro di validazione: nessuna valutazione finale e nessun dataset registrato",
            isolation_ok,
            "Il gate di valutazione finale resta NON attivato: una sola valutazione, mai anticipata.",
            evidence=registry_state,
        )
    )

    closed_state = {
        "dataset": config.dataset_id,
        "rows": len(times),
        "dataset_sha256": dataset_sha256(archive),
        "last_acquired_end_utc": _iso(times[-1] + HOUR_MS) if times else None,
        "closed_boundary_utc": _iso(closed_boundary_ms),
        "pending_hours": (
            int((closed_boundary_ms - (times[-1] + HOUR_MS)) // HOUR_MS) if times else 0
        ),
    }
    checks.append(
        _audit_check(
            "FWD.11",
            "Ultima candela completa coerente con l'orologio di sistema",
            all(value + HOUR_MS <= closed_boundary_ms for value in times),
            "Nessuna barra è stata archiviata prima della chiusura della candela.",
            evidence=closed_state,
        )
    )

    failed = [item["id"] for item in checks if item["status"] == iv.STATUS_FAIL]
    return {
        "schema_version": AUDIT_SCHEMA,
        "dataset_id": config.dataset_id,
        "generated_utc": _now().isoformat(),
        "archive_root": str(archive.root),
        "checks": checks,
        "checks_failed": failed,
        "technical_gate": iv.STATUS_PASS if not failed else iv.STATUS_FAIL,
        "dataset": closed_state,
        "quarantine": quarantine_state,
        "protocol": protocol,
        "readiness_status": readiness(config, archive=archive, now=current, paths=paths)["status"],
        "evaluation_executed": False,
        "final_evaluation_gate": "NOT_ACTIVATED",
        "strategic_gate": iv.VERDICT_NO_GO,
        "verdict": iv.VERDICT_NOT_EVALUATED,
    }


def _registry_isolation(
    paths: ForwardPaths, protocol: Mapping[str, Any]
) -> tuple[bool, dict[str, Any]]:
    try:
        registry = iv.ValidationRegistry(paths.resolve(paths.registry_dir))
        state = registry.state(str(protocol.get("canonical_sha256") or ""))
        datasets = registry.datasets()
    except Exception as exc:  # noqa: BLE001 - broken registry is a failure, not a warning
        return False, {"error": f"{type(exc).__name__}: {exc}"}
    forward_datasets = [row.get("dataset_id") for row in datasets if str(row.get("dataset_id", "")).startswith("btcusdt-1h-forward")]
    ok = bool(
        state["final_evaluations"] == 0
        and state["evaluations"] == 0
        and not forward_datasets
    )
    return ok, {
        "registry_dir": str(paths.resolve(paths.registry_dir)),
        "state": state,
        "datasets": len(datasets),
        "forward_datasets_registered": forward_datasets,
    }


# ---------------------------------------------------------------------------
# automation helpers
# ---------------------------------------------------------------------------


def structured_log(event: str, payload: Mapping[str, Any], *, stream=None) -> None:
    """One JSON object per line, suitable for journald or a log file."""

    record = {"at_utc": _now().isoformat(), "event": event, **dict(payload)}
    print(_canonical_json(record), file=stream or sys.stderr, flush=True)


def _print(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m crypto_decision_lab.forward_collection",
        description=(
            "S02-CP08-05: raccolta incrementale di BTCUSDT 1h successivi al boundary "
            "congelato. Nessuna valutazione, nessun training, nessun trading."
        ),
    )
    parser.add_argument("command", choices=("status", "sync", "audit", "readiness"))
    parser.add_argument("--config", default=None, help="YAML di configurazione (default: config/forward-collection.yaml)")
    parser.add_argument("--repo-root", default=".", help="radice del repository")
    parser.add_argument("--now", default=None, help="ISO UTC di riferimento (solo per test/diagnostica)")
    parser.add_argument("--recheck-source", action="store_true", help="sync: ricontrolla la sorgente sui chunk esistenti")
    parser.add_argument("--lock-timeout", type=float, default=None, help="sync: attesa massima per il lock")
    parser.add_argument("--log-file", default=None, help="accoda eventi strutturati anche su file")
    return parser


def _log_target(args) -> Any:
    if not args.log_file:
        return None
    path = Path(args.log_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("a", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    paths = ForwardPaths(repo_root=Path(args.repo_root).expanduser().resolve())
    handle = None
    log_stream = None

    def emit(event: str, payload: Mapping[str, Any]) -> None:
        structured_log(event, payload)
        if log_stream is not None:
            structured_log(event, payload, stream=log_stream)

    try:
        now = None
        if args.now:
            parsed = datetime.fromisoformat(str(args.now).replace("Z", "+00:00"))
            now = parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        config = load_config(args.config, paths=paths, now=now)
        archive = ForwardArchive(config.archive_dir)
        log_stream = _log_target(args)

        if args.command in ("status", "readiness", "audit"):
            payload = {
                "status": lambda: status(config, archive=archive, now=now, paths=paths),
                "readiness": lambda: readiness(config, archive=archive, now=now, paths=paths),
                "audit": lambda: audit(config, archive=archive, now=now, paths=paths),
            }[args.command]()
            _print(payload)
            emit(f"{args.command.upper()}_OK", {"dataset_id": config.dataset_id})
            return EXIT_OK

        lock_timeout = (
            float(args.lock_timeout)
            if args.lock_timeout is not None
            else float(config.automation.get("lock_timeout_seconds", 0) or 0)
        )
        handle = archive.lock(timeout=lock_timeout)
        result = sync(
            config,
            archive=archive,
            now=now,
            recheck_source=bool(args.recheck_source),
        )
        _print(result.as_dict())
        emit(
            "SYNC_RESULT",
            {
                "dataset_id": config.dataset_id,
                "status": result.status,
                "run_id": result.run_id,
                "promoted": result.promoted,
                "quarantined": result.quarantined,
                "conflicts": result.conflicts,
                "dataset_sha256": result.dataset_sha256,
            },
        )
        if result.status == "INTERRUPTED":
            emit("SYNC_ERROR", {"run_id": result.run_id, "error": result.error})
            return EXIT_ERROR
        return EXIT_OK
    except ForwardLockedError as exc:
        emit("SYNC_BLOCKED", {"error": str(exc)})
        return EXIT_BLOCKED
    except (ForwardConfigError, ForwardIsolationViolation) as exc:
        emit("CONFIG_ERROR", {"error": f"{type(exc).__name__}: {exc}"})
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        emit("SYNC_ERROR", {"error": f"{type(exc).__name__}: {exc}"})
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        if handle is not None:
            try:
                _unlock(handle)
            finally:
                handle.close()
        if log_stream is not None:  # pragma: no cover - CLI only
            log_stream.close()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
