"""S02: versioned research Dataset Builder with verified temporal semantics.

The builder produces one dataset directory::

    data/datasets/research/<dataset_id>-<version>/
        features.parquet   # cutoff-only information, one row per window
        targets.parquet    # realized outcomes of the execution hypothesis
        manifest.json      # provenance, spec, digests, build metadata
        summary.json       # build statistics

Contracts
---------
* **Inputs are identified by manifest and hash.**  Sources come from the S02
  catalog (:mod:`.dataset_catalog`) and are admitted only after the role,
  identity, provenance and alias guards pass.  The manifest records the
  snapshot dataset id, the source manifest SHA-256 and the raw-manifest
  digest.
* **Feature availability is recorded.**  Every feature row carries
  ``feature_available_at`` (the instant the anchor bar closes = the cutoff)
  and ``feature_bar`` (the last bar used, ``cutoff - 1h``).  Frames whose
  availability exceeds the cutoff are rejected.
* **Targets and features are separate files.**  Targets hold the realized
  outcome of the declared :class:`~.execution_contract.ExecutionHypothesis`
  (latency, horizon, fees, slippage) and are never joined to inference
  inputs.
* **Determinism.**  Feature and target *contents* are fingerprinted by a
  canonical digest (sorted rows, fixed columns, explicit float formatting).
  Two builds with the same inputs and parameters produce the same digests;
  ``created_utc`` is build metadata and is excluded from the digests.
* **Windows must be complete.**  A window whose warmup, entry or exit bars
  are missing, or whose holding period contains an hourly gap, is rejected
  with :class:`WindowDataError` — never silently filled.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

import numpy as np
import pandas as pd

from . import dataset_catalog
from .decision_contract import now_iso
from .execution_contract import (
    ExecutionContractError,
    ExecutionHypothesis,
    RoundTrip,
    resolve_round_trip,
    utc,
)
from .forward_isolation import (
    ForwardIsolationViolation,
    guard_research_output,
    guard_research_path,
    is_forward_path,
)

DATASET_SCHEMA = "cdl.s02.dataset.v1"
FEATURES_SCHEMA = "cdl.s02.features.v1"
TARGETS_SCHEMA = "cdl.s02.targets.v1"

# Stationary, cutoff-only indicators (the non-price-level subset of the
# S02-CP06B regime features, reused through regime_analysis.compute_features).
STATIONARY_FEATURES = (
    "rsi14",
    "atr_pct",
    "volatility_24h",
    "volatility_168h",
    "momentum_24h",
    "momentum_168h",
    "dist_sma50_pct",
    "trend_strength_pct",
    "volume_ratio",
)
FEATURE_COLUMNS = STATIONARY_FEATURES
TARGET_COLUMNS = (
    "entry_timestamp",
    "exit_timestamp",
    "entry_open",
    "exit_open",
    "entry_exec_price",
    "exit_exec_price",
    "round_trip_cost_pct",
    "long_gross_return_pct",
    "long_net_return_pct",
    "long_trade_worth",
)
HOUR = pd.Timedelta(hours=1)


class DatasetBuilderError(ValueError):
    """Raised when the dataset build would violate the S02 contracts."""


class WindowDataError(DatasetBuilderError):
    """Raised when a window lacks the bars required by the spec."""


class FeatureAvailabilityError(DatasetBuilderError):
    """Raised when features would use information beyond their cutoff."""


@dataclass(frozen=True)
class BuildSpec:
    """Parameters of one dataset build (fully declared in the manifest)."""

    dataset_id: str = "BTCUSDT-1h-research"
    version: str = "v1"
    stride_hours: int = 24
    warmup_hours: int = 168
    hypothesis: ExecutionHypothesis = field(default_factory=ExecutionHypothesis)

    def __post_init__(self) -> None:
        if not self.dataset_id or not self.version:
            raise DatasetBuilderError("dataset_id and version are required")
        for name, value in (("stride_hours", self.stride_hours), ("warmup_hours", self.warmup_hours)):
            if int(value) != value or value < 1:
                raise DatasetBuilderError(f"{name} must be a positive integer: {value}")

    def to_params(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "version": self.version,
            "stride_hours": int(self.stride_hours),
            "warmup_hours": int(self.warmup_hours),
            "execution": self.hypothesis.declaration(),
        }

    def params_digest(self) -> str:
        payload = json.dumps(self.to_params(), sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Canonical content digests (CP02.1)
# --------------------------------------------------------------------------- #

def _canonical_cell(value: Any) -> str:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return "null"
    if isinstance(value, (bool, np.bool_)):
        return "true" if bool(value) else "false"
    if isinstance(value, pd.Timestamp):
        return pd.Timestamp(value).isoformat()
    if isinstance(value, (int, np.integer)):
        return str(int(value))
    if isinstance(value, (float, np.floating)):
        return f"{float(value):.17g}"
    return str(value)


def canonical_frame_digest(frame: pd.DataFrame, columns: Sequence[str]) -> str:
    """Deterministic content digest: sorted rows, fixed columns, no metadata."""

    missing = set(columns) - set(frame.columns)
    if missing:
        raise DatasetBuilderError(f"columns missing from frame: {sorted(missing)}")
    rows = sorted(
        ",".join(_canonical_cell(value) for value in record)
        for record in frame[list(columns)].itertuples(index=False, name=None)
    )
    payload = "\n".join(rows) + "\n" if rows else "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Window enumeration and validation (CP02.2)
# --------------------------------------------------------------------------- #

def validate_feature_window(
    market: pd.DataFrame, cutoff: pd.Timestamp, spec: BuildSpec
) -> pd.DataFrame:
    """Return the warmup bars of a window or raise :class:`WindowDataError`."""

    cutoff = utc(cutoff)
    start = cutoff - int(spec.warmup_hours) * HOUR
    end = cutoff - HOUR  # last usable bar open: closes exactly at cutoff
    bars = market.loc[start:end]
    if len(bars) != int(spec.warmup_hours):
        raise WindowDataError(
            f"warmup incomplete for cutoff {cutoff}: expected {int(spec.warmup_hours)} bars, got {len(bars)}"
        )
    expected = pd.date_range(start, end, freq="1h", tz="UTC")
    missing = expected.difference(bars.index)
    if len(missing):
        raise WindowDataError(
            f"warmup has {len(missing)} missing hourly bar(s) for cutoff {cutoff} "
            f"(first missing: {missing[0]})"
        )
    validate_feature_inputs(bars, cutoff)
    return bars


def validate_feature_inputs(bars: pd.DataFrame, cutoff: pd.Timestamp) -> None:
    """Refuse feature inputs that contain bars at or beyond the cutoff."""

    cutoff = utc(cutoff)
    beyond = bars.index[bars.index >= cutoff]
    if len(beyond):
        raise FeatureAvailabilityError(
            f"feature inputs use {len(beyond)} bar(s) at or beyond the cutoff {cutoff} "
            f"(first: {beyond[0]}); features may only use bars opening before the cutoff"
        )


def validate_execution_window(
    market: pd.DataFrame, cutoff: pd.Timestamp, hypothesis: ExecutionHypothesis
) -> RoundTrip:
    """Resolve the execution span or raise for windows lacking required data."""

    try:
        return resolve_round_trip(market, cutoff, hypothesis)
    except ExecutionContractError as exc:
        raise WindowDataError(str(exc)) from exc


def eligible_cutoffs(market: pd.DataFrame, spec: BuildSpec) -> list[pd.Timestamp]:
    """Cutoffs whose warmup AND execution windows are complete.

    Candidates step by ``stride_hours`` from the first cutoff with a full
    warmup; windows straddling data holes or the end of the snapshot are
    skipped (deterministic in the input frame and the spec).
    """

    first = market.index.min()
    last = market.index.max()
    cursor = utc(first) + int(spec.warmup_hours) * HOUR
    limit = last - (int(spec.hypothesis.latency_hours) + int(spec.hypothesis.horizon_hours)) * HOUR
    cutoffs: list[pd.Timestamp] = []
    while cursor <= limit:
        try:
            validate_feature_window(market, cursor, spec)
            validate_execution_window(market, cursor, spec.hypothesis)
        except WindowDataError:
            pass
        else:
            cutoffs.append(cursor)
        cursor = cursor + int(spec.stride_hours) * HOUR
    return cutoffs


# --------------------------------------------------------------------------- #
# Features and targets
# --------------------------------------------------------------------------- #

def build_features(
    market: pd.DataFrame, cutoffs: Sequence[pd.Timestamp], spec: BuildSpec
) -> pd.DataFrame:
    """Cutoff-only stationary features, reusing the S02-CP06B computation."""

    from .regime_analysis import REQUIRED_HISTORY_BARS, RegimeError, compute_features

    if not len(cutoffs):
        raise DatasetBuilderError("no cutoffs provided: nothing to build")
    windows = pd.DataFrame(
        {"window_index": np.arange(len(cutoffs), dtype=int), "cutoff": pd.DatetimeIndex(cutoffs)}
    )
    # compute_features reads every indicator at the last bar strictly before
    # the cutoff and requires REQUIRED_HISTORY_BARS of history.
    if int(spec.warmup_hours) < REQUIRED_HISTORY_BARS:
        raise DatasetBuilderError(
            f"warmup_hours={spec.warmup_hours} is below the reusable CP06B requirement of {REQUIRED_HISTORY_BARS}"
        )
    try:
        regime = compute_features(market, windows)
    except RegimeError as exc:
        raise WindowDataError(f"warmup incomplete for the requested cutoffs: {exc}") from exc
    regime["cutoff"] = pd.to_datetime(regime["cutoff"], utc=True)
    regime["feature_bar"] = pd.to_datetime(regime["feature_bar"], utc=True)
    availability = regime["feature_bar"] + HOUR
    if not (availability == regime["cutoff"]).all():
        raise FeatureAvailabilityError(
            "anchor bar does not close at the cutoff: feature availability is inconsistent"
        )
    features = pd.DataFrame(
        {
            "window_index": regime["window_index"].astype(int),
            "cutoff": regime["cutoff"],
            "feature_available_at": availability,
            "feature_bar": regime["feature_bar"],
        }
    )
    for name in FEATURE_COLUMNS:
        features[name] = regime[name].astype(float)
    validate_feature_frame(features)
    return features.reset_index(drop=True)


def validate_feature_frame(features: pd.DataFrame) -> None:
    """Reject feature frames whose availability exceeds the cutoff (CP02.2)."""

    required = {"window_index", "cutoff", "feature_available_at", "feature_bar", *FEATURE_COLUMNS}
    missing = required - set(features.columns)
    if missing:
        raise FeatureAvailabilityError(f"feature columns missing: {sorted(missing)}")
    cutoffs = pd.to_datetime(features["cutoff"], utc=True)
    available = pd.to_datetime(features["feature_available_at"], utc=True)
    bars = pd.to_datetime(features["feature_bar"], utc=True)
    late = available > cutoffs
    if bool(late.any()):
        first = features.loc[late.idxmax()]
        raise FeatureAvailabilityError(
            f"features available beyond the cutoff: window {int(first['window_index'])} "
            f"available_at {first['feature_available_at']} > cutoff {first['cutoff']}"
        )
    leaked = bars >= cutoffs
    if bool(leaked.any()):
        raise FeatureAvailabilityError(
            "feature_bar at or beyond the cutoff: features must only use bars opening before the cutoff"
        )
    values = features[list(FEATURE_COLUMNS)].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise FeatureAvailabilityError("non-finite feature values")


def build_targets(
    market: pd.DataFrame, cutoffs: Sequence[pd.Timestamp], spec: BuildSpec
) -> pd.DataFrame:
    """Realized outcomes of the declared execution hypothesis (future data)."""

    if not len(cutoffs):
        raise DatasetBuilderError("no cutoffs provided: nothing to build")
    records: list[dict[str, Any]] = []
    for index, cutoff in enumerate(cutoffs):
        trip = validate_execution_window(market, cutoff, spec.hypothesis)
        records.append(
            {
                "window_index": index,
                "cutoff": trip.cutoff,
                "entry_timestamp": trip.entry_timestamp,
                "exit_timestamp": trip.exit_timestamp,
                "entry_open": trip.entry_open,
                "exit_open": trip.exit_open,
                "entry_exec_price": trip.entry_exec_price,
                "exit_exec_price": trip.exit_exec_price,
                "round_trip_cost_pct": trip.round_trip_cost_pct,
                "long_gross_return_pct": trip.gross_return_pct,
                "long_net_return_pct": trip.net_return_pct,
                "long_trade_worth": bool(trip.net_return_pct > 0.0),
            }
        )
    targets = pd.DataFrame(records)[["window_index", "cutoff", *TARGET_COLUMNS]]
    expected_hold = pd.to_timedelta(int(spec.hypothesis.horizon_hours), unit="h")
    if not (pd.to_datetime(targets["exit_timestamp"], utc=True)
            - pd.to_datetime(targets["entry_timestamp"], utc=True) == expected_hold).all():
        raise DatasetBuilderError("target holding periods do not match the declared horizon")
    return targets


# --------------------------------------------------------------------------- #
# Build, write, verify
# --------------------------------------------------------------------------- #

def build_dataset(
    market,
    spec: BuildSpec,
    *,
    output_dir: str | Path | None = None,
    source: dataset_catalog.CatalogEntry | None = None,
    write: bool = True,
) -> dict[str, Any]:
    """Build one versioned dataset from an admitted market source.

    ``market`` is a :class:`~crypto_decision_lab.dashboard.data_loader.MarketData`
    (frame + provenance).  The identity guard is re-run here so the builder
    refuses forward sources even when called directly, bypassing the catalog.
    """

    frame = market.frame
    snapshot_dir = Path(getattr(market, "snapshot_dir", Path()))
    if is_forward_path(snapshot_dir):
        raise ForwardIsolationViolation(
            f"dataset build cannot read the forward archive: {snapshot_dir} is independent "
            "evidence reserved for the frozen validation protocol"
        )
    cutoffs = eligible_cutoffs(frame, spec)
    if not cutoffs:
        raise DatasetBuilderError(
            "no eligible cutoffs: the market frame has no window satisfying warmup and execution requirements"
        )
    features = build_features(frame, cutoffs, spec)
    targets = build_targets(frame, cutoffs, spec)
    features_digest = canonical_frame_digest(features, ["window_index", "cutoff", "feature_available_at", "feature_bar", *FEATURE_COLUMNS])
    targets_digest = canonical_frame_digest(targets, ["window_index", "cutoff", *TARGET_COLUMNS])
    gaps = pd.Series(cutoffs).diff().dt.total_seconds().div(3600.0).dropna()
    result: dict[str, Any] = {
        "schema_version": DATASET_SCHEMA,
        "dataset_id": spec.dataset_id,
        "version": spec.version,
        "created_utc": now_iso(),
        "rows": int(len(features)),
        "first_cutoff": str(pd.Timestamp(cutoffs[0])),
        "last_cutoff": str(pd.Timestamp(cutoffs[-1])),
        "min_cutoff_gap_hours": float(gaps.min()) if len(gaps) else None,
        "features_schema": FEATURES_SCHEMA,
        "targets_schema": TARGETS_SCHEMA,
        "feature_columns": list(FEATURE_COLUMNS),
        "target_columns": list(TARGET_COLUMNS),
        "features_content_sha256": features_digest,
        "targets_content_sha256": targets_digest,
        "spec": spec.to_params(),
        "spec_params_sha256": spec.params_digest(),
        "execution_declaration": spec.hypothesis.declaration(),
        "warning": "targets.parquet contains realized outcomes; never join it to inference inputs",
        "purge_note": (
            f"targets of cutoff T are known at T + {int(spec.hypothesis.latency_hours)}h + "
            f"{int(spec.hypothesis.horizon_hours)}h: purge at least "
            f"{int(spec.hypothesis.latency_hours) + int(spec.hypothesis.horizon_hours)}h "
            "between train and test windows"
        ),
    }
    if write:
        if output_dir is None:
            raise DatasetBuilderError("output_dir is required when write=True")
        _write_dataset(
            Path(output_dir), market, spec, features, targets, result, source=source
        )
    return result


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_dataset(
    output_dir: Path,
    market,
    spec: BuildSpec,
    features: pd.DataFrame,
    targets: pd.DataFrame,
    result: dict[str, Any],
    *,
    source: dataset_catalog.CatalogEntry | None,
) -> None:
    output_dir = guard_research_output(output_dir, purpose="dataset build output")
    if output_dir.exists():
        raise FileExistsError(f"dataset already exists; refusing to overwrite: {output_dir}")
    provenance: dict[str, Any] = {
        "market_dataset_id": market.dataset_id,
        "snapshot_dir": str(market.snapshot_dir),
        "source_manifest_sha256": market.source_manifest_sha256,
        "source_files": int(market.source_files),
        "missing_hours": len(market.missing_hours),
        "source_anomalies": len(market.source_anomalies),
        "quarantined_chunks": int(market.quarantined_files),
    }
    if source is not None:
        provenance["catalog_dataset_id"] = source.dataset_id
        provenance["catalog_role"] = source.role
    raw_manifest = Path(market.snapshot_dir) / "raw-manifest.json"
    if raw_manifest.is_file():
        provenance["raw_manifest_sha256"] = _file_digest(raw_manifest)
    output_dir.mkdir(parents=True)
    try:
        features_parquet = output_dir / "features.parquet"
        targets_parquet = output_dir / "targets.parquet"
        features.to_parquet(features_parquet, index=False)
        targets.to_parquet(targets_parquet, index=False)
        manifest = {
            **{key: value for key, value in result.items() if key not in ("features_content_sha256", "targets_content_sha256")},
            "provenance": provenance,
            "outputs": {
                "features": {
                    "path": "features.parquet",
                    "rows": int(len(features)),
                    "content_sha256": result["features_content_sha256"],
                    "file_sha256": _file_digest(features_parquet),
                },
                "targets": {
                    "path": "targets.parquet",
                    "rows": int(len(targets)),
                    "content_sha256": result["targets_content_sha256"],
                    "file_sha256": _file_digest(targets_parquet),
                },
            },
            "feature_availability": (
                "feature_available_at = cutoff: the anchor bar cutoff - 1h closes exactly at the cutoff; "
                "feature_bar is the last bar opening before the cutoff"
            ),
        }
        (output_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8"
        )
        (output_dir / "summary.json").write_text(
            json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8"
        )
    except BaseException:
        import shutil

        shutil.rmtree(output_dir, ignore_errors=True)
        raise


def verify_dataset(dataset_dir: str | Path) -> dict[str, Any]:
    """Recompute digests and compare them with the recorded manifest."""

    dataset_dir = Path(dataset_dir)
    manifest_path = dataset_dir / "manifest.json"
    if not manifest_path.is_file():
        raise DatasetBuilderError(f"manifest missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    checks: dict[str, Any] = {"dataset_dir": str(dataset_dir), "ok": True, "mismatches": []}
    for name in ("features", "targets"):
        recorded = manifest["outputs"][name]
        path = dataset_dir / recorded["path"]
        if not path.is_file():
            checks["mismatches"].append(f"{name}: file missing ({recorded['path']})")
            continue
        file_digest = _file_digest(path)
        if file_digest != recorded["file_sha256"]:
            checks["mismatches"].append(f"{name}: file sha256 {file_digest} != {recorded['file_sha256']}")
        frame = pd.read_parquet(path)
        columns = (
            ["window_index", "cutoff", "feature_available_at", "feature_bar", *FEATURE_COLUMNS]
            if name == "features"
            else ["window_index", "cutoff", *TARGET_COLUMNS]
        )
        content_digest = canonical_frame_digest(frame, columns)
        if content_digest != recorded["content_sha256"]:
            checks["mismatches"].append(f"{name}: content sha256 {content_digest} != {recorded['content_sha256']}")
        if int(len(frame)) != int(recorded["rows"]):
            checks["mismatches"].append(f"{name}: rows {len(frame)} != {recorded['rows']}")
    checks["ok"] = not checks["mismatches"]
    return checks


def main(argv: list[str] | None = None) -> int:
    """CLI: build or verify a research dataset from the catalog."""

    import argparse

    parser = argparse.ArgumentParser(description="S02 research Dataset Builder (catalog-admitted sources only)")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="build a versioned dataset from a catalog source")
    build.add_argument("--catalog", default=str(dataset_catalog.DEFAULT_CATALOG_PATH))
    build.add_argument("--source", required=True, help="admitted catalog dataset_id")
    build.add_argument("--repo-root", default=".")
    build.add_argument("--data-root", default=None, help="defaults to <repo-root>/data")
    build.add_argument("--output-dir", default=None, help="defaults to data/datasets/research/<dataset_id>-<version>")
    build.add_argument("--dataset-id", default="BTCUSDT-1h-research")
    build.add_argument("--version", default="v1")
    build.add_argument("--stride-hours", type=int, default=24)
    build.add_argument("--warmup-hours", type=int, default=168)
    build.add_argument("--latency-hours", type=int, default=0)
    build.add_argument("--horizon-hours", type=int, default=24)
    build.add_argument("--fee-rate", type=float, default=0.001)
    build.add_argument("--slippage", type=float, default=0.0005)
    build.add_argument("--no-write", action="store_true")
    verify = sub.add_parser("verify", help="recompute digests and compare with the manifest")
    verify.add_argument("--dataset-dir", required=True)
    args = parser.parse_args(argv)
    if args.command == "verify":
        report = verify_dataset(args.dataset_dir)
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    catalog = dataset_catalog.Catalog.load(args.catalog)
    repo_root = Path(args.repo_root).resolve()
    data_root = Path(args.data_root) if args.data_root else (repo_root / "data" if (repo_root / "data").is_dir() else repo_root)
    entry = dataset_catalog.assert_research_admissible(catalog, args.source, repo_root=repo_root)
    from .dashboard.data_loader import load_market_data

    market = load_market_data(
        data_root, guard_research_path(repo_root / entry.path, purpose=f"research source {args.source}"), verify_checksums=True
    )
    spec = BuildSpec(
        dataset_id=args.dataset_id,
        version=args.version,
        stride_hours=args.stride_hours,
        warmup_hours=args.warmup_hours,
        hypothesis=ExecutionHypothesis(
            latency_hours=args.latency_hours,
            horizon_hours=args.horizon_hours,
            fee_rate=args.fee_rate,
            slippage=args.slippage,
        ),
    )
    output_dir = (
        Path(args.output_dir)
        if args.output_dir
        else data_root / "datasets" / "research" / f"{spec.dataset_id}-{spec.version}"
    )
    result = build_dataset(market, spec, output_dir=output_dir, source=entry, write=not args.no_write)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
