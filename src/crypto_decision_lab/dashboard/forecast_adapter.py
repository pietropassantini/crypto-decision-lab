"""Adapters that turn archived forecast experiments into aligned target points.

Important time semantics
------------------------
For a window, ``cutoff`` is ``provenance.forecast_start_utc``.  The first
forecast row is the target one hour after the last context close, the 24th
row is labelled horizon 24 and falls at ``cutoff + 23h``.  CP05's
``actual_price_24h`` follows the same convention.  Forecasts from different
windows are independent and must never be concatenated into a continuous
trajectory.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import glob
import json
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from .data_loader import DataValidationError, digest_file, read_table

HOUR = pd.Timedelta(hours=1)
CP_HORIZON_HOURS = 24
ALIGNMENT_KEYS = ["window_index", "target", "horizon_hours"]

POINT_COLUMNS = [
    "model",
    "model_label",
    "model_version",
    "experiment",
    "window_index",
    "cutoff",
    "target",
    "horizon_hours",
    "predicted",
    "actual",
    "anchor_price",
    "lower",
    "upper",
    "sample_id",
    "source",
    "forecast_sha256",
    "snapshot_id",
]


@dataclass
class ModelInfo:
    key: str
    label: str
    description: str
    horizons: str
    available: bool = False
    source: str | None = None


MODEL_CATALOG: dict[str, ModelInfo] = {
    "timestral": ModelInfo(
        "timestral",
        "Timestral-209M",
        "Forecast p50 archived in S02-CP02; hourly targets 1..24.",
        "1h–24h",
    ),
    "naive": ModelInfo(
        "naive",
        "Naive (persistence)",
        "Last context close repeated at every target timestamp.",
        "1h–24h",
    ),
    "ridge": ModelInfo(
        "ridge",
        "Ridge residual",
        "Residual correction baseline from S02-CP05; +24h target only.",
        "24h only",
    ),
    "histgb": ModelInfo(
        "histgb",
        "HistGradientBoosting residual",
        "Residual correction baseline from S02-CP05; +24h target only.",
        "24h only",
    ),
    "decision": ModelInfo(
        "decision",
        "Decision Model (future)",
        "Optional S03 predictions; unavailable if no compatible archive exists.",
        "per archive",
    ),
}


@dataclass
class ForecastDataset:
    points: pd.DataFrame
    windows: pd.DataFrame
    models: dict[str, ModelInfo]
    issues: list[str] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)
    snapshot_id: str | None = None
    snapshot_sha256: str | None = None
    batch_dir: Path | None = None
    residual_baselines_dir: Path | None = None

    @property
    def empty(self) -> bool:
        return self.points.empty

    def available_models(self) -> list[str]:
        return [key for key, info in self.models.items() if info.available]

    def points_for(
        self,
        models: Iterable[str] | None = None,
        *,
        horizon: int | None = None,
        window_index: int | None = None,
    ) -> pd.DataFrame:
        data = self.points
        if data.empty:
            return data.copy()
        if models is not None:
            data = data[data["model"].isin(list(models))]
        if horizon is not None:
            data = data[data["horizon_hours"] == int(horizon)]
        if window_index is not None:
            data = data[data["window_index"] == int(window_index)]
        return data.copy()

    def window_choices(self) -> pd.DataFrame:
        if self.windows.empty:
            return self.windows.copy()
        return self.windows.sort_values("cutoff", ascending=False).reset_index(drop=True)


def _empty_points() -> pd.DataFrame:
    frame = pd.DataFrame({column: pd.Series(dtype="object") for column in POINT_COLUMNS})
    frame["cutoff"] = pd.to_datetime(frame["cutoff"], utc=True)
    frame["target"] = pd.to_datetime(frame["target"], utc=True)
    for column in ("window_index", "horizon_hours"):
        frame[column] = frame[column].astype("int64")
    for column in ("predicted", "actual", "anchor_price", "lower", "upper"):
        frame[column] = frame[column].astype("float64")
    return frame[list(POINT_COLUMNS)]


def _utc(value) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise DataValidationError(f"Cannot read JSON {path}: {exc}") from exc


def _validate_forecast_frame(
    frame: pd.DataFrame, *, expected_cutoff: pd.Timestamp, source: str
) -> pd.DataFrame:
    required = {"timestamp", "actual", "timestral_p10", "timestral_p50", "timestral_p90", "naive"}
    missing = required - set(frame.columns)
    if missing:
        raise DataValidationError(f"Missing forecast columns in {source}: {sorted(missing)}")
    data = frame.copy()
    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True, errors="raise")
    data = data.sort_values("timestamp").reset_index(drop=True)
    if data["timestamp"].duplicated().any():
        raise DataValidationError(f"Duplicate forecast timestamps in {source}")
    if len(data) < 1:
        raise DataValidationError(f"Empty forecast in {source}")
    if data["timestamp"].iloc[0] != expected_cutoff:
        raise DataValidationError(
            f"Forecast starts at {data['timestamp'].iloc[0]} but cutoff is {expected_cutoff}: {source}"
        )
    if not data["timestamp"].diff().dropna().eq(HOUR).all():
        raise DataValidationError(f"Forecast timestamps are not hourly in {source}")
    values = data[["actual", "timestral_p10", "timestral_p50", "timestral_p90", "naive"]].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise DataValidationError(f"Non-finite forecast values in {source}")
    if not (
        (data["timestral_p10"] <= data["timestral_p50"])
        & (data["timestral_p50"] <= data["timestral_p90"])
    ).all():
        raise DataValidationError(f"Invalid Timestral quantiles in {source}")
    if (data["actual"] <= 0).any() or (data["timestral_p50"] <= 0).any():
        raise DataValidationError(f"Non-positive forecast prices in {source}")
    return data


def _load_batch(
    batch_dir: Path,
    snapshot_id: str | None,
) -> tuple[list[dict], pd.DataFrame, list[dict], list[str]]:
    issues: list[str] = []
    records: list[dict] = []
    windows: list[dict] = []
    runs: list[dict] = []
    if not batch_dir.is_dir():
        return records, pd.DataFrame(), runs, [f"Forecast batch directory not found: {batch_dir}"]
    config_path = batch_dir / "config.json"
    if not config_path.is_file():
        return records, pd.DataFrame(), runs, [f"Forecast batch config missing: {config_path}"]
    config = _read_json(config_path)
    indices = list(config.get("window_indices") or [])
    if not indices:
        return records, pd.DataFrame(), runs, ["Forecast batch config contains no windows"]
    runs.append(
        {
            "experiment": config.get("experiment", "S02-CP02"),
            "model": config.get("model", "mistralai/Timestral-209M"),
            "revision": config.get("revision"),
            "windows": len(indices),
            "source_manifest_sha256": config.get("source_manifest_sha256"),
            "windows_sha256": config.get("windows_sha256"),
            "path": str(batch_dir),
        }
    )
    window_records: dict[int, dict] = {}
    for index in indices:
        try:
            idx = int(index)
            folder = batch_dir / f"window-{idx:04d}"
            metrics_path = folder / "metrics.json"
            forecast_path = folder / "forecast.parquet"
            if not metrics_path.is_file() or not forecast_path.is_file():
                raise DataValidationError(f"Incomplete archived window: {folder}")
            metrics = _read_json(metrics_path)
            expected_sha = metrics.get("forecast_sha256")
            if expected_sha and digest_file(forecast_path) != expected_sha:
                raise DataValidationError(f"Forecast checksum mismatch: {forecast_path}")
            if int(metrics.get("window_index", -1)) != idx:
                raise DataValidationError(f"Window index mismatch in {metrics_path}")
            provenance = metrics.get("provenance") or {}
            cutoff = _utc(provenance.get("forecast_start_utc"))
            frame = _validate_forecast_frame(
                read_table(forecast_path), expected_cutoff=cutoff, source=str(forecast_path)
            )
            anchor = float(metrics.get("last_context_close", frame["naive"].iloc[0]))
            if not np.isfinite(anchor) or anchor <= 0:
                raise DataValidationError(f"Invalid anchor price in {metrics_path}")
            if not np.allclose(frame["naive"].to_numpy(float), anchor, rtol=0, atol=1e-6):
                raise DataValidationError(f"Naive column is not anchored to last context close: {forecast_path}")
            model_version = str(metrics.get("model") or config.get("model") or "Timestral-209M")
            revision = metrics.get("revision_requested") or config.get("revision")
            if revision:
                model_version = f"{model_version}@{revision}"
            for horizon, row in enumerate(frame.itertuples(index=False), start=1):
                target = _utc(row.timestamp)
                common = {
                    "window_index": idx,
                    "cutoff": cutoff,
                    "target": target,
                    "horizon_hours": horizon,
                    "actual": float(row.actual),
                    "anchor_price": anchor,
                    "source": str(forecast_path.relative_to(batch_dir)),
                    "forecast_sha256": expected_sha,
                    "snapshot_id": snapshot_id,
                    "experiment": metrics.get("experiment", config.get("experiment", "S02-CP02")),
                }
                records.append(
                    {
                        **common,
                        "model": "timestral",
                        "model_label": "Timestral-209M",
                        "model_version": model_version,
                        "predicted": float(row.timestral_p50),
                        "lower": float(row.timestral_p10),
                        "upper": float(row.timestral_p90),
                        "sample_id": f"S02-CP02:timestral:window-{idx:04d}:h{horizon:02d}",
                    }
                )
                records.append(
                    {
                        **common,
                        "model": "naive",
                        "model_label": "Naive (persistence)",
                        "model_version": "last-context-close",
                        "predicted": float(row.naive),
                        "lower": np.nan,
                        "upper": np.nan,
                        "sample_id": f"S02-CP02:naive:window-{idx:04d}:h{horizon:02d}",
                    }
                )
            window_records[idx] = {
                "window_index": idx,
                "cutoff": cutoff,
                "target_end_exclusive": _utc(provenance.get("target_end_exclusive_utc")),
                "anchor_price": anchor,
                "forecast_rows": len(frame),
                "last_target": _utc(frame["timestamp"].iloc[-1]),
            }
        except Exception as exc:
            issues.append(f"Window {index} skipped: {exc}")
    if window_records:
        windows = pd.DataFrame(sorted(window_records.values(), key=lambda row: row["window_index"]))
    return records, windows, runs, issues


def _cp05_target(cutoff: pd.Timestamp) -> pd.Timestamp:
    # The archived 24h label is the 24th hourly target including the cutoff bar.
    return cutoff + (CP_HORIZON_HOURS - 1) * HOUR


def _load_cp05(
    residual_dir: Path | None,
    batch_records: list[dict],
    snapshot_id: str | None,
    *,
    corrected: bool = False,
) -> tuple[list[dict], list[dict], list[str]]:
    issues: list[str] = []
    records: list[dict] = []
    runs: list[dict] = []
    if residual_dir is None or not residual_dir.is_dir():
        return records, runs, [f"Residual baseline directory not found: {residual_dir}"]
    predictions_path = residual_dir / "predictions.parquet"
    if not predictions_path.is_file():
        return records, runs, [f"Residual baseline predictions missing: {predictions_path}"]
    try:
        frame = read_table(predictions_path)
    except Exception as exc:
        return records, runs, [f"Cannot read residual baseline predictions: {exc}"]
    required = {
        "window_index",
        "forecast_start_utc",
        "actual_price_24h",
        "naive_price_24h",
        "ridge_price_24h",
        "histgb_price_24h",
    }
    missing = required - set(frame.columns)
    if missing:
        return records, runs, [f"Residual baseline columns missing: {sorted(missing)}"]
    summary_path = residual_dir / "summary.json"
    summary = _read_json(summary_path) if summary_path.is_file() else {}
    predictions_sha = summary.get("predictions_sha256") or digest_file(predictions_path)
    runs.append(
        {
            "experiment": "S02-CP05R corrected benchmark" if corrected else "S02-CP05 residual baselines",
            "windows": len(frame),
            "path": str(residual_dir),
            "predictions_sha256": predictions_sha,
            "materialized_rows": summary.get("evaluation_rows"),
            "corrected": corrected,
        }
    )
    by_window: dict[int, dict] = {}
    for record in batch_records:
        if record["model"] == "timestral":
            by_window[(int(record["window_index"]), int(record["horizon_hours"]))] = record
    version = "S02-CP05R-corrected-benchmark-v1" if corrected else "S02-CP05-residual-baselines-v1"
    experiment = "S02-CP05R" if corrected else "S02-CP05"
    timestral_reference_checked = False
    timestral_reference_mismatch = False
    for row in frame.itertuples(index=False):
        try:
            idx = int(row.window_index)
            cutoff = _utc(row.forecast_start_utc)
            target = _cp05_target(cutoff)
            anchor = float(row.naive_price_24h)
            actual = float(row.actual_price_24h)
            if not all(np.isfinite([anchor, actual, float(row.ridge_price_24h), float(row.histgb_price_24h)])):
                raise DataValidationError(f"Non-finite CP05 row for window {idx}")
            reference = by_window.get((idx, CP_HORIZON_HOURS))
            if reference is not None:
                if reference["target"] != target:
                    raise DataValidationError(
                        f"CP05 target {target} differs from CP02 target {reference['target']} for window {idx}"
                    )
                if not np.isclose(actual, reference["actual"], rtol=1e-6, atol=0.05):
                    raise DataValidationError(f"CP05 actual price mismatch for window {idx}")
                if not np.isclose(anchor, reference["anchor_price"], rtol=1e-6, atol=0.05):
                    raise DataValidationError(f"CP05 anchor mismatch for window {idx}")
                timestral_reference_checked = True
                if not np.isclose(
                    float(row.timestral_price_24h), reference["predicted"], rtol=1e-6, atol=0.05
                ):
                    # Do not rewrite archived outputs.  Report the discrepancy once
                    # and keep displaying CP05's own archived values unchanged.
                    timestral_reference_mismatch = True
            for model, predicted in (
                ("ridge", float(row.ridge_price_24h)),
                ("histgb", float(row.histgb_price_24h)),
            ):
                records.append(
                    {
                        "model": model,
                        "model_label": MODEL_CATALOG[model].label,
                        "model_version": version,
                        "experiment": experiment,
                        "window_index": idx,
                        "cutoff": cutoff,
                        "target": target,
                        "horizon_hours": CP_HORIZON_HOURS,
                        "predicted": predicted,
                        "actual": actual,
                        "anchor_price": anchor,
                        "lower": np.nan,
                        "upper": np.nan,
                        "sample_id": f"{experiment}:{model}:window-{idx:04d}:h{CP_HORIZON_HOURS:02d}",
                        "source": str(predictions_path.relative_to(residual_dir)),
                        "forecast_sha256": predictions_sha,
                        "snapshot_id": snapshot_id,
                    }
                )
        except Exception as exc:
            issues.append(f"CP05 row skipped: {exc}")
    if timestral_reference_checked and timestral_reference_mismatch:
        issues.append(
            "CP05 timestral_price_24h differs from CP02 p50 at h24; archived CP05 values "
            "are displayed unchanged and CP05's Timestral reference is not used for metrics"
        )
    if not corrected:
        issues.append(
            "Original S02-CP05 price fields are affected by the S02-CP05R unit error; "
            "Ridge/HistGB prices shown from this source are legacy and must not be used "
            "for CP06A. Prefer data/experiments/S02-CP05R-corrected-benchmark-v1."
        )
    return records, runs, issues


def _pick_column(frame: pd.DataFrame, names: Iterable[str]) -> str | None:
    for name in names:
        if name in frame.columns:
            return name
    return None


def _load_decision(
    pattern: str | None, snapshot_id: str | None
) -> tuple[list[dict], list[dict], list[str]]:
    """Best-effort adapter for a future S03 decision archive.

    Expected columns: cutoff, target, predicted; optional actual/anchor/window.
    Incompatible archives are reported, never invented.
    """

    records: list[dict] = []
    runs: list[dict] = []
    issues: list[str] = []
    if not pattern:
        return records, runs, issues
    for folder_value in sorted(glob.glob(pattern)):
        folder = Path(folder_value)
        if not folder.is_dir():
            continue
        predictions = folder / "predictions.parquet"
        if not predictions.is_file():
            continue
        try:
            frame = read_table(predictions)
            cutoff_col = _pick_column(frame, ("cutoff_utc", "cutoff", "forecast_start_utc", "generated_at_utc"))
            target_col = _pick_column(frame, ("target_utc", "target", "timestamp", "target_timestamp_utc"))
            pred_col = _pick_column(frame, ("predicted", "prediction", "predicted_price", "decision_price"))
            actual_col = _pick_column(frame, ("actual", "actual_price", "target_price"))
            anchor_col = _pick_column(frame, ("anchor_price", "last_context_close"))
            if not cutoff_col or not target_col or not pred_col:
                raise DataValidationError(f"Decision archive lacks cutoff/target/predicted columns: {folder}")
            config = _read_json(folder / "config.json") if (folder / "config.json").is_file() else {}
            version = str(config.get("model", folder.name))
            if config.get("revision"):
                version = f"{version}@{config['revision']}"
            for ordinal, row in enumerate(frame.itertuples(index=False), start=1):
                cutoff = _utc(getattr(row, cutoff_col))
                target = _utc(getattr(row, target_col))
                if target < cutoff:
                    raise DataValidationError(f"Decision target precedes cutoff in {folder}")
                horizon = int(round((target - cutoff).total_seconds() / 3600.0)) + 1
                window = int(getattr(row, "window_index")) if "window_index" in frame.columns and not pd.isna(getattr(row, "window_index")) else ordinal
                actual = float(getattr(row, actual_col)) if actual_col and not pd.isna(getattr(row, actual_col)) else np.nan
                anchor = float(getattr(row, anchor_col)) if anchor_col and not pd.isna(getattr(row, anchor_col)) else np.nan
                records.append(
                    {
                        "model": "decision",
                        "model_label": MODEL_CATALOG["decision"].label,
                        "model_version": version,
                        "experiment": folder.name,
                        "window_index": window,
                        "cutoff": cutoff,
                        "target": target,
                        "horizon_hours": max(1, horizon),
                        "predicted": float(getattr(row, pred_col)),
                        "actual": actual,
                        "anchor_price": anchor,
                        "lower": np.nan,
                        "upper": np.nan,
                        "sample_id": f"{folder.name}:decision:window-{window:04d}:h{max(1, horizon):02d}",
                        "source": str(predictions),
                        "forecast_sha256": digest_file(predictions),
                        "snapshot_id": snapshot_id,
                    }
                )
            runs.append({"experiment": folder.name, "model": version, "path": str(folder)})
        except Exception as exc:
            issues.append(f"Decision archive skipped ({folder}): {exc}")
    return records, runs, issues


def _deduplicate_points(frame: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    issues: list[str] = []
    if frame.empty:
        return frame, issues
    keys = ["model", "window_index", "target", "horizon_hours"]
    duplicated = frame.duplicated(keys, keep=False)
    if duplicated.any():
        groups = frame[duplicated].groupby(keys, dropna=False)
        conflicts = 0
        keep_indices: list[int] = []
        for _, group in groups:
            if group["predicted"].round(8).nunique(dropna=False) > 1:
                conflicts += 1
                issues.append(
                    f"Conflicting duplicate forecast for {group.iloc[0]['model']} "
                    f"window {group.iloc[0]['window_index']} target {group.iloc[0]['target']}; keeping latest source"
                )
            keep_indices.append(group.index[-1])
        drop = frame.index.difference(pd.Index(keep_indices))
        frame = frame.drop(index=drop)
        if conflicts:
            issues.append(f"{conflicts} forecast conflicts resolved by keeping the last archive row")
    return frame.sort_values(["target", "model", "cutoff"]).reset_index(drop=True), issues


@lru_cache(maxsize=8)
def _load_forecasts_cached(
    batch_dir_value: str,
    residual_dir_value: str,
    corrected_dir_value: str,
    snapshot_dir_value: str,
    decision_pattern: str | None,
) -> ForecastDataset:
    batch_dir = Path(batch_dir_value)
    residual_dir = Path(residual_dir_value) if residual_dir_value else None
    corrected_dir = Path(corrected_dir_value) if corrected_dir_value else None
    snapshot_dir = Path(snapshot_dir_value)
    snapshot_id = None
    snapshot_sha = None
    report_path = snapshot_dir / "report.json"
    if report_path.is_file():
        report = _read_json(report_path)
        snapshot_id = report.get("dataset_id") or snapshot_dir.name
        snapshot_sha = report.get("source_manifest_sha256")
    records, windows, runs, issues = _load_batch(batch_dir, snapshot_id)
    use_corrected = bool(
        corrected_dir and (corrected_dir / "predictions.parquet").is_file()
    )
    cp05_dir = corrected_dir if use_corrected else residual_dir
    if corrected_dir and not use_corrected:
        issues.append(
            f"Corrected CP05R benchmark not found at {corrected_dir}; "
            "falling back to original CP05 price fields, which are legacy and invalid for CP06A"
        )
    cp05_records, cp05_runs, cp05_issues = _load_cp05(
        cp05_dir, records, snapshot_id, corrected=use_corrected
    )
    decision_records, decision_runs, decision_issues = _load_decision(decision_pattern, snapshot_id)
    issues.extend(cp05_issues)
    issues.extend(decision_issues)
    runs.extend(cp05_runs)
    runs.extend(decision_runs)
    points = pd.DataFrame(records + cp05_records + decision_records)
    if points.empty:
        points = _empty_points()
    else:
        points = points[POINT_COLUMNS].copy()
        points["cutoff"] = pd.to_datetime(points["cutoff"], utc=True)
        points["target"] = pd.to_datetime(points["target"], utc=True)
        points["window_index"] = points["window_index"].astype(int)
        points["horizon_hours"] = points["horizon_hours"].astype(int)
        for column in ("predicted", "actual", "anchor_price", "lower", "upper"):
            points[column] = pd.to_numeric(points[column], errors="coerce")
        points, dedupe_issues = _deduplicate_points(points)
        issues.extend(dedupe_issues)
    if windows.empty and not points.empty:
        windows = (
            points[["window_index", "cutoff", "target", "anchor_price"]]
            .groupby(["window_index", "cutoff"], as_index=False)
            .agg(target_end_exclusive=("target", "max"), anchor_price=("anchor_price", "first"))
            .sort_values("cutoff")
        )
    models = {key: ModelInfo(**vars(info)) for key, info in MODEL_CATALOG.items()}
    available = set(points["model"].unique()) if not points.empty else set()
    for key, info in models.items():
        info.available = key in available
        if info.available:
            info.source = str(batch_dir if key in {"timestral", "naive"} else cp05_dir if key in {"ridge", "histgb"} else "decision archive")
    if not points.empty:
        counts = points.groupby("model")["window_index"].nunique().to_dict()
        for key, info in models.items():
            if info.available:
                info.description = f"{info.description} Windows: {counts.get(key, 0)}."
    return ForecastDataset(
        points=points,
        windows=windows,
        models=models,
        issues=list(dict.fromkeys(issues)),
        runs=runs,
        snapshot_id=snapshot_id,
        snapshot_sha256=snapshot_sha,
        batch_dir=batch_dir,
        residual_baselines_dir=cp05_dir,
    )


def load_forecasts(
    batch_dir: str | Path,
    residual_baselines_dir: str | Path | None = None,
    snapshot_dir: str | Path | None = None,
    decision_glob: str | None = None,
    corrected_residual_dir: str | Path | None = None,
) -> ForecastDataset:
    return _load_forecasts_cached(
        str(Path(batch_dir).expanduser().resolve()),
        str(Path(residual_baselines_dir).expanduser().resolve()) if residual_baselines_dir else "",
        str(Path(corrected_residual_dir).expanduser().resolve()) if corrected_residual_dir else "",
        str(Path(snapshot_dir).expanduser().resolve()) if snapshot_dir else "",
        decision_glob,
    )


def clear_forecast_cache() -> None:
    _load_forecasts_cached.cache_clear()


def available_selected(dataset: ForecastDataset, models: Iterable[str]) -> list[str]:
    return [model for model in models if model in dataset.models and dataset.models[model].available]


def align_models(
    points: pd.DataFrame,
    models: Iterable[str],
    *,
    horizon: int | None = None,
) -> pd.DataFrame:
    """Return only samples present for *all* selected models.

    Alignment uses cutoff + target + horizon, not only the display timestamp,
    so two independent forecast windows can never be mixed.
    """

    requested = list(dict.fromkeys(models))
    if not requested:
        return points.iloc[0:0].copy()
    data = points[points["model"].isin(requested)].copy()
    if horizon is not None:
        data = data[data["horizon_hours"] == int(horizon)]
    data = data[np.isfinite(data["predicted"]) & np.isfinite(data["actual"])]
    if data.empty:
        return data
    counts = (
        data.groupby(ALIGNMENT_KEYS)["model"]
        .nunique()
        .rename("model_count")
        .reset_index()
    )
    common = counts[counts["model_count"] == len(requested)][ALIGNMENT_KEYS]
    if common.empty:
        return data.iloc[0:0].copy()
    aligned = data.merge(common, on=ALIGNMENT_KEYS, how="inner")
    return aligned.sort_values([*ALIGNMENT_KEYS, "model"]).reset_index(drop=True)


def common_targets(
    points: pd.DataFrame,
    models: Iterable[str],
    *,
    horizon: int | None = None,
) -> pd.DatetimeIndex:
    aligned = align_models(points, models, horizon=horizon)
    if aligned.empty:
        return pd.DatetimeIndex([], tz="UTC")
    values = pd.to_datetime(aligned["target"], utc=True).drop_duplicates().sort_values()
    return pd.DatetimeIndex(values, name="target")


def validate_forecast_actuals(
    points: pd.DataFrame, market_frame: pd.DataFrame, *, atol: float = 0.05, rtol: float = 1e-6
) -> list[str]:
    """Cross-check archived actual prices against market closes at targets."""

    issues: list[str] = []
    if points.empty:
        return ["No forecast points to validate"]
    subset = points[points["model"].isin(["timestral", "ridge", "histgb"])]
    checked = 0
    for row in subset.drop_duplicates(["target", "actual"]).itertuples(index=False):
        if not np.isfinite(row.actual):
            continue
        target = _utc(row.target)
        if target not in market_frame.index:
            issues.append(f"Target timestamp absent from market data: {target}")
            continue
        market_close = float(market_frame.at[target, "close"])
        if not np.isclose(float(row.actual), market_close, rtol=rtol, atol=atol):
            issues.append(
                f"Actual price mismatch at {target}: archive={row.actual:.4f} market={market_close:.4f}"
            )
        checked += 1
    if checked == 0:
        issues.append("No archived actual prices could be cross-checked")
    return list(dict.fromkeys(issues))
