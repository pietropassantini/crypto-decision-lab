"""Read-only market data adapters and OHLCV validation.

The dashboard never writes to the source dataset.  It reads the frozen
snapshot manifest first, because that manifest is also the provenance record
used by the forecasting experiments.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import hashlib
import hmac
import json
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]
SUPPORTED_TIMEFRAMES = ("1h", "4h", "1D")


class DataValidationError(ValueError):
    """Raised when a dataset cannot be displayed safely."""


@dataclass
class MarketData:
    frame: pd.DataFrame
    dataset_id: str
    snapshot_dir: Path
    data_root: Path
    source_manifest_sha256: str | None
    source_manifest_path: Path | None
    source_files: int
    source_anomalies: list[dict] = field(default_factory=list)
    missing_hours: list[pd.Timestamp] = field(default_factory=list)
    quarantined_files: int = 0
    issues: list[str] = field(default_factory=list)

    @property
    def latest_timestamp(self) -> pd.Timestamp:
        return self.frame.index.max()

    @property
    def first_timestamp(self) -> pd.Timestamp:
        return self.frame.index.min()

    @property
    def latest_close(self) -> float:
        return float(self.frame["close"].iloc[-1])

    @property
    def rows(self) -> int:
        return len(self.frame)

    @property
    def gap_count(self) -> int:
        return len(self.missing_hours)


def digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_table(path: Path, columns: Iterable[str] | None = None) -> pd.DataFrame:
    """Read a Parquet/CSV table with a clear error for unsupported formats."""

    suffix = path.suffix.lower()
    if suffix == ".parquet":
        return pd.read_parquet(path, columns=list(columns) if columns else None)
    if suffix == ".csv":
        return pd.read_csv(path, usecols=list(columns) if columns else None)
    raise DataValidationError(f"Unsupported table format: {path.suffix} ({path})")


def normalize_ohlcv(frame: pd.DataFrame, *, source: str = "dataset") -> pd.DataFrame:
    """Validate and index OHLCV data without filling, interpolating or deduping."""

    if frame is None or frame.empty:
        raise DataValidationError(f"Empty OHLCV dataset: {source}")
    data = frame.copy()
    if "timestamp" in data.columns:
        timestamp = pd.to_datetime(data.pop("timestamp"), utc=True, errors="raise")
        data.index = pd.DatetimeIndex(timestamp, name="timestamp")
    elif isinstance(data.index, pd.DatetimeIndex):
        if data.index.tz is None:
            raise DataValidationError(f"Naive timestamps in {source}; UTC is required")
        data.index = pd.DatetimeIndex(data.index, name="timestamp").tz_convert("UTC")
    else:
        raise DataValidationError(f"Missing timestamp column/index in {source}")

    missing = [column for column in OHLCV_COLUMNS if column not in data.columns]
    if missing:
        raise DataValidationError(f"Missing OHLCV columns in {source}: {missing}")
    data = data[OHLCV_COLUMNS].sort_index(kind="stable")
    if data.index.has_duplicates:
        duplicated = data.index[data.index.duplicated()].unique()
        sample = ", ".join(str(value) for value in duplicated[:3])
        raise DataValidationError(
            f"Duplicate timestamps in {source} ({len(duplicated)} found; first: {sample}); "
            "refusing to deduplicate silently"
        )
    if data.index.isna().any():
        raise DataValidationError(f"Invalid NaT timestamps in {source}")
    numeric = data[OHLCV_COLUMNS].to_numpy(dtype=float)
    if not np.isfinite(numeric).all():
        raise DataValidationError(f"Non-finite OHLCV values in {source}")
    if (data[list(OHLCV_COLUMNS)] < 0).any().any():
        raise DataValidationError(f"Negative prices/volumes in {source}")
    if (data[["open", "high", "low", "close"]] <= 0).any().any():
        raise DataValidationError(f"Non-positive prices in {source}")
    if (data["high"] < data["low"]).any():
        raise DataValidationError(f"high < low in {source}")
    if (data["high"] < data[["open", "close"]].max(axis=1)).any():
        raise DataValidationError(f"high < open/close in {source}")
    if (data["low"] > data[["open", "close"]].min(axis=1)).any():
        raise DataValidationError(f"low > open/close in {source}")
    return data


def _snapshot_manifest(snapshot_dir: Path) -> tuple[dict | None, Path | None]:
    manifest_path = snapshot_dir / "raw-manifest.json"
    if not manifest_path.is_file():
        return None, None
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    report_path = snapshot_dir / "report.json"
    if report_path.is_file():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if (
            raw.get("source_manifest_sha256")
            and report.get("source_manifest_sha256")
            and raw["source_manifest_sha256"] != report["source_manifest_sha256"]
        ):
            raise DataValidationError("Snapshot and report provenance mismatch")
    return raw, manifest_path


def _safe_chunk_path(data_root: Path, relative: str | Path) -> Path:
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise DataValidationError(f"Unsafe chunk path in snapshot manifest: {relative}")
    path = (data_root / relative_path).resolve()
    try:
        path.relative_to(data_root.resolve())
    except ValueError as exc:
        raise DataValidationError(f"Chunk escapes data root: {relative}") from exc
    return path


def _load_from_manifest(
    data_root: Path,
    snapshot_dir: Path,
    raw: dict,
    *,
    verify_checksums: bool,
) -> tuple[pd.DataFrame, list[str], list[dict], int]:
    chunks = raw.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise DataValidationError("Snapshot manifest contains no chunks")
    frames: list[pd.DataFrame] = []
    issues: list[str] = []
    quarantined = 0
    for chunk in chunks:
        relative = chunk.get("file") if isinstance(chunk, dict) else None
        if not relative:
            raise DataValidationError("Malformed chunk entry in snapshot manifest")
        path = _safe_chunk_path(data_root, relative)
        if not path.is_file():
            raise DataValidationError(f"Referenced chunk does not exist: {path}")
        if verify_checksums:
            expected = chunk.get("sha256")
            actual = digest_file(path)
            if expected and not hmac.compare_digest(str(actual), str(expected)):
                raise DataValidationError(f"Checksum changed: {path}")
        if "quarantine" in Path(relative).parts:
            quarantined += 1
        try:
            table = read_table(path, columns=["timestamp", *OHLCV_COLUMNS])
        except Exception as exc:  # pragma: no cover - defensive adapter boundary
            raise DataValidationError(f"Cannot read chunk {path}: {exc}") from exc
        frames.append(table)
    if not frames:
        raise DataValidationError("No readable chunks in snapshot manifest")
    combined = pd.concat(frames, ignore_index=True)
    return combined, issues, list(raw.get("anomalies") or []), quarantined


def _load_from_glob(data_root: Path) -> tuple[pd.DataFrame, list[str]]:
    files = sorted((data_root / "chunks").glob("**/*.parquet"))
    if not files:
        raise DataValidationError(
            "No market data found. Expected a snapshot raw-manifest.json or data/chunks/**/*.parquet"
        )
    frames = [read_table(path, columns=["timestamp", *OHLCV_COLUMNS]) for path in files]
    return pd.concat(frames, ignore_index=True), [f"Manifest not found; read {len(files)} Parquet chunks from data/chunks"]


def _missing_hours(index: pd.DatetimeIndex) -> list[pd.Timestamp]:
    if len(index) < 2:
        return []
    expected = pd.date_range(index.min(), index.max(), freq="1h", tz="UTC")
    return [timestamp for timestamp in expected if timestamp not in index]


@lru_cache(maxsize=8)
def _load_market_data_cached(
    data_root_value: str,
    snapshot_dir_value: str,
    verify_checksums: bool,
) -> MarketData:
    data_root = Path(data_root_value).resolve()
    snapshot_dir = Path(snapshot_dir_value).resolve()
    raw, manifest_path = _snapshot_manifest(snapshot_dir)
    if raw is not None:
        combined, issues, anomalies, quarantined = _load_from_manifest(
            data_root, snapshot_dir, raw, verify_checksums=verify_checksums
        )
        dataset_id = str(raw.get("dataset_id") or snapshot_dir.name)
        source_hash = raw.get("source_manifest_sha256")
        source_files = len(raw.get("chunks") or [])
    else:
        combined, issues = _load_from_glob(data_root)
        anomalies = []
        quarantined = 0
        dataset_id = snapshot_dir.name if snapshot_dir.name else "local-chunks"
        source_hash = None
        source_files = len(list((data_root / "chunks").glob("**/*.parquet")))
        manifest_path = None
        issues.append("Frozen snapshot provenance unavailable")
    frame = normalize_ohlcv(combined, source=dataset_id)
    missing = _missing_hours(frame.index)
    if missing:
        issues.append(f"{len(missing)} missing hourly bars (not filled)")
    if anomalies:
        issues.append(f"{len(anomalies)} source anomalies recorded in snapshot")
    if quarantined:
        issues.append(f"{quarantined} quarantined chunks included read-only")
    return MarketData(
        frame=frame,
        dataset_id=dataset_id,
        snapshot_dir=snapshot_dir,
        data_root=data_root,
        source_manifest_sha256=source_hash,
        source_manifest_path=manifest_path,
        source_files=source_files,
        source_anomalies=anomalies,
        missing_hours=missing,
        quarantined_files=quarantined,
        issues=issues,
    )


def load_market_data(
    data_root: str | Path,
    snapshot_dir: str | Path,
    *,
    verify_checksums: bool = True,
) -> MarketData:
    """Load historical candles from a frozen snapshot (read-only)."""

    return _load_market_data_cached(
        str(Path(data_root).expanduser().resolve()),
        str(Path(snapshot_dir).expanduser().resolve()),
        bool(verify_checksums),
    )


def clear_market_cache() -> None:
    _load_market_data_cached.cache_clear()


def resample_ohlcv(frame: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Aggregate OHLCV to a requested timeframe.

    ``1h`` returns the validated input unchanged (no synthetic gaps).
    Aggregation uses open=first, high=max, low=min, close=last, volume=sum,
    left-closed/left-labelled UTC bins, matching the source's hourly bars.
    """

    if rule not in SUPPORTED_TIMEFRAMES:
        raise ValueError(f"Unsupported timeframe: {rule}; use {SUPPORTED_TIMEFRAMES}")
    data = normalize_ohlcv(frame)
    if rule == "1h":
        return data
    grouped = data.resample(rule, closed="left", label="left")
    output = pd.DataFrame(
        {
            "open": grouped["open"].first(),
            "high": grouped["high"].max(),
            "low": grouped["low"].min(),
            "close": grouped["close"].last(),
            "volume": grouped["volume"].sum(min_count=1),
        }
    )
    output = output.dropna(subset=["open", "high", "low", "close"])
    if output.empty:
        raise DataValidationError(f"Resampling to {rule} produced no rows")
    return normalize_ohlcv(output, source=f"resampled-{rule}")


def display_frame_with_gaps(frame: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Return a display copy with explicit NaN rows for missing bins.

    This function must never be used to feed indicators: it exists only to make
    gaps visible in a line chart.  No value is filled.
    """

    data = normalize_ohlcv(frame)
    if rule == "1h":
        frequency = "1h"
    else:
        frequency = rule
    expected = pd.date_range(data.index.min(), data.index.max(), freq=frequency, tz="UTC")
    display = data.reindex(expected)
    display.index.name = "timestamp"
    return display


def filter_range(
    frame: pd.DataFrame, start: pd.Timestamp | str | None, end_exclusive: pd.Timestamp | str | None
) -> pd.DataFrame:
    """Filter ``[start, end_exclusive)`` with timezone-aware validation."""

    data = normalize_ohlcv(frame) if not isinstance(frame.index, pd.DatetimeIndex) else frame
    if start is not None:
        start_ts = pd.Timestamp(start)
        if start_ts.tzinfo is None:
            start_ts = start_ts.tz_localize("UTC")
        data = data.loc[data.index >= start_ts]
    if end_exclusive is not None:
        end_ts = pd.Timestamp(end_exclusive)
        if end_ts.tzinfo is None:
            end_ts = end_ts.tz_localize("UTC")
        data = data.loc[data.index < end_ts]
    if data.empty:
        raise DataValidationError("Selected time range contains no candles")
    return data


def latest_candle(frame: pd.DataFrame) -> pd.Series:
    if frame.empty:
        raise DataValidationError("Cannot select latest candle from an empty frame")
    return frame.iloc[-1]


def gap_report(frame: pd.DataFrame) -> dict:
    """Summarize hourly gaps without modifying the data."""

    if not isinstance(frame.index, pd.DatetimeIndex):
        raise DataValidationError("gap_report requires a DatetimeIndex")
    if frame.empty:
        return {"count": 0, "timestamps": []}
    missing = _missing_hours(frame.index)
    return {
        "count": len(missing),
        "timestamps": [timestamp.isoformat() for timestamp in missing],
    }
