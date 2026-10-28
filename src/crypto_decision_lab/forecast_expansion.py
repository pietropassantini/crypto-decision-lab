"""S02-CP06A: resumable historical Timestral expansion.

This module produces an additive archive of 500 forecast windows:

* 100 windows are reused from S02-CP02 without new inference;
* 400 new windows are inferred from the frozen snapshot;
* every window keeps a CP02-compatible hourly forecast plus canonical h24 data.

It never modifies CP02, CP04, CP05, CP05R or the snapshot.  Re-running a stage
validates existing windows and skips them; an existing invalid window is never
overwritten.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import time
from typing import Any

import numpy as np
import pandas as pd

from .canonical import (
    CANONICAL_SCHEMA,
    HORIZON_HOURS,
    MODELS,
    TARGET_OFFSET,
    h24_record,
    validate_h24_frame,
)
from .forecast_smoke import evaluate, load_window
from .sync import digest

EXPANSION_SCHEMA = "cdl.cp06a.expansion.v1"
FORECAST_SCHEMA = "cdl.cp06a.forecast.v1"
DEFAULT_TARGET = 500
SMOKE_SIZE = 5
VERIFICATION_SIZE = 50
FORECAST_COLUMNS = ["timestamp", "actual", "timestral_p10", "timestral_p50", "timestral_p90", "naive"]
STAGES = ("smoke", "verification", "expand")


@dataclass(frozen=True)
class ExpansionPaths:
    data_root: Path = Path("data")
    snapshot_dir: Path = Path("data/snapshots/BTCUSDT-1h-raw-v1")
    cp02_dir: Path = Path("data/experiments/S02-CP02-batch-100")
    cp05r_dir: Path = Path("data/experiments/S02-CP05R-corrected-benchmark-v1")
    output_dir: Path = Path("data/experiments/S02-CP06A-historical-expansion-v1")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _utc(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _source_hashes(paths: ExpansionPaths) -> dict[str, str]:
    files = {
        "snapshot_raw_manifest": paths.snapshot_dir / "raw-manifest.json",
        "snapshot_report": paths.snapshot_dir / "report.json",
        "snapshot_windows": paths.snapshot_dir / "forecast-windows.parquet",
        "cp02_config": paths.cp02_dir / "config.json",
        "cp02_summary": paths.cp02_dir / "summary.json",
        "cp05r_predictions": paths.cp05r_dir / "predictions.parquet",
        "cp05r_summary": paths.cp05r_dir / "summary.json",
    }
    return {name: digest(path) for name, path in files.items() if path.is_file()}


def load_snapshot_windows(paths: ExpansionPaths) -> pd.DataFrame:
    report = _read_json(paths.snapshot_dir / "report.json")
    windows_path = paths.snapshot_dir / "forecast-windows.parquet"
    if digest(windows_path) != report.get("windows_parquet_sha256"):
        raise ValueError("Frozen forecast-windows checksum mismatch")
    windows = pd.read_parquet(windows_path)
    required = {"context_start_utc", "forecast_start_utc", "target_end_exclusive_utc"}
    if not required.issubset(windows.columns):
        raise ValueError("Snapshot windows missing required timestamp columns")
    windows = windows.copy()
    for column in required:
        windows[column] = pd.to_datetime(windows[column], utc=True)
    if windows["forecast_start_utc"].duplicated().any():
        raise ValueError("Duplicate snapshot forecast_start_utc values")
    return windows.reset_index(drop=True)


def load_cp02_indices(paths: ExpansionPaths) -> list[int]:
    config = _read_json(paths.cp02_dir / "config.json")
    indices = [int(value) for value in config.get("window_indices", [])]
    if not indices or len(indices) != len(set(indices)):
        raise ValueError("CP02 config has missing or duplicate window indices")
    for index in indices:
        folder = paths.cp02_dir / f"window-{index:04d}"
        if not (folder / "forecast.parquet").is_file() or not (folder / "metrics.json").is_file():
            raise ValueError(f"Incomplete CP02 window: {folder}")
    return indices


def select_new_indices(total_windows: int, cp02_indices: list[int], target: int) -> list[int]:
    """Select deterministic, evenly spaced new windows excluding CP02 indices."""

    if target < len(cp02_indices):
        raise ValueError("Target is smaller than the reusable CP02 set")
    cp02_set = {int(index) for index in cp02_indices}
    if any(index < 0 or index >= total_windows for index in cp02_set):
        raise ValueError("CP02 index outside snapshot window range")
    candidates = [index for index in range(total_windows) if index not in cp02_set]
    needed = int(target) - len(cp02_set)
    if needed < 0 or needed > len(candidates):
        raise ValueError(f"Cannot select {needed} new windows from {len(candidates)} candidates")
    if needed == 0:
        return []
    positions = np.linspace(0, len(candidates) - 1, needed, dtype=int)
    selected = [candidates[int(position)] for position in positions]
    if len(selected) != len(set(selected)):
        raise ValueError("Selection generated duplicates")
    if cp02_set.intersection(selected):
        raise ValueError("Selection overlaps CP02")
    return selected


def stage_plan(new_indices: list[int]) -> dict[str, list[int]]:
    smoke = new_indices[:SMOKE_SIZE]
    verification = new_indices[len(smoke):len(smoke) + VERIFICATION_SIZE]
    expansion = new_indices[len(smoke) + len(verification):]
    return {"smoke": smoke, "verification": verification, "expand": expansion}


def _window_row(windows: pd.DataFrame, index: int) -> pd.Series:
    row = windows.iloc[int(index)]
    return row


def _forecast_frame(
    timestamps: pd.DatetimeIndex,
    actual: np.ndarray,
    p10: np.ndarray,
    p50: np.ndarray,
    p90: np.ndarray,
    naive: np.ndarray,
) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "actual": np.asarray(actual, dtype=float),
            "timestral_p10": np.asarray(p10, dtype=float),
            "timestral_p50": np.asarray(p50, dtype=float),
            "timestral_p90": np.asarray(p90, dtype=float),
            "naive": np.asarray(naive, dtype=float),
        }
    )


def _validate_forecast_frame(frame: pd.DataFrame, *, expected_cutoff: pd.Timestamp, source: str) -> pd.DataFrame:
    missing = set(FORECAST_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(f"Forecast columns missing in {source}: {sorted(missing)}")
    data = frame.copy()
    data["timestamp"] = pd.to_datetime(data["timestamp"], utc=True, errors="raise")
    data = data.sort_values("timestamp").reset_index(drop=True)
    if len(data) != HORIZON_HOURS:
        raise ValueError(f"Expected {HORIZON_HOURS} forecast rows in {source}, found {len(data)}")
    if data["timestamp"].iloc[0] != expected_cutoff:
        raise ValueError(f"Forecast start mismatch in {source}")
    if not data["timestamp"].diff().dropna().eq(pd.Timedelta(hours=1)).all():
        raise ValueError(f"Forecast is not hourly in {source}")
    values = data[FORECAST_COLUMNS[1:]].to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError(f"Non-finite forecast values in {source}")
    if (data["actual"] <= 0).any() or (data["timestral_p50"] <= 0).any():
        raise ValueError(f"Non-positive prices in {source}")
    if not ((data["timestral_p10"] <= data["timestral_p50"]) & (data["timestral_p50"] <= data["timestral_p90"])).all():
        raise ValueError(f"Invalid quantile ordering in {source}")
    return data


def _window_metrics(frame: pd.DataFrame, anchor: float) -> dict[str, Any]:
    actual = frame["actual"].to_numpy(dtype=float)
    p50 = frame["timestral_p50"].to_numpy(dtype=float)
    naive = frame["naive"].to_numpy(dtype=float)
    return {
        "timestral": evaluate(actual, p50, anchor),
        "naive": evaluate(actual, naive, anchor),
        "quantile_10_90_empirical_coverage": float(
            np.mean((actual >= frame["timestral_p10"].to_numpy(dtype=float)) & (actual <= frame["timestral_p90"].to_numpy(dtype=float)))
        ),
    }


def _write_window(
    paths: ExpansionPaths,
    index: int,
    frame: pd.DataFrame,
    metrics: dict[str, Any],
) -> dict[str, Any]:
    folder = paths.output_dir / f"window-{index:04d}"
    if folder.exists():
        raise FileExistsError(f"Window already exists; refusing to overwrite: {folder}")
    folder.mkdir(parents=True)
    try:
        forecast_path = folder / "forecast.parquet"
        frame.to_parquet(forecast_path, index=False)
        metrics = dict(metrics)
        metrics["forecast_sha256"] = digest(forecast_path)
        _write_json_atomic(folder / "metrics.json", metrics)
        return metrics
    except BaseException:
        shutil.rmtree(folder, ignore_errors=True)
        raise


def _validate_existing_window(paths: ExpansionPaths, index: int) -> dict[str, Any]:
    folder = paths.output_dir / f"window-{index:04d}"
    metrics_path = folder / "metrics.json"
    forecast_path = folder / "forecast.parquet"
    if not metrics_path.is_file() or not forecast_path.is_file():
        raise ValueError(f"Incomplete existing expansion window: {folder}")
    metrics = _read_json(metrics_path)
    if int(metrics.get("window_index", -1)) != int(index):
        raise ValueError(f"Window index mismatch: {folder}")
    if metrics.get("forecast_sha256") != digest(forecast_path):
        raise ValueError(f"Forecast checksum mismatch: {folder}")
    cutoff = _utc((metrics.get("provenance") or {}).get("forecast_start_utc"))
    _validate_forecast_frame(pd.read_parquet(forecast_path), expected_cutoff=cutoff, source=str(folder))
    return metrics


def _copy_cp02_window(paths: ExpansionPaths, index: int, row: pd.Series) -> dict[str, Any]:
    source_folder = paths.cp02_dir / f"window-{index:04d}"
    source_metrics = _read_json(source_folder / "metrics.json")
    source_path = source_folder / "forecast.parquet"
    expected_sha = source_metrics.get("forecast_sha256")
    if expected_sha and digest(source_path) != expected_sha:
        raise ValueError(f"CP02 source checksum mismatch: {source_folder}")
    frame = _validate_forecast_frame(
        pd.read_parquet(source_path),
        expected_cutoff=_utc(row["forecast_start_utc"]),
        source=str(source_path),
    )
    provenance = dict(source_metrics.get("provenance") or {})
    metrics = {
        "schema_version": FORECAST_SCHEMA,
        "experiment": "S02-CP06A",
        "source": "S02-CP02-reused",
        "reused": True,
        "original_experiment": source_metrics.get("experiment", "S02-CP02"),
        "model": source_metrics.get("model", "mistralai/Timestral-209M"),
        "revision_requested": source_metrics.get("revision_requested"),
        "device": source_metrics.get("device", "cpu"),
        "window_index": int(index),
        "lookback": int(source_metrics.get("lookback", 168)),
        "horizon": HORIZON_HOURS,
        "provenance": provenance,
        "last_context_close": float(source_metrics.get("last_context_close", frame["naive"].iloc[-1])),
        "metrics": source_metrics.get("metrics", _window_metrics(frame, float(frame["naive"].iloc[-1]))),
        "source_forecast_sha256": expected_sha,
    }
    return _write_window(paths, index, frame, metrics)


def _ensure_reused(paths: ExpansionPaths, windows: pd.DataFrame, cp02_indices: list[int]) -> None:
    for index in cp02_indices:
        folder = paths.output_dir / f"window-{index:04d}"
        if folder.exists():
            _validate_existing_window(paths, index)
            continue
        _copy_cp02_window(paths, index, _window_row(windows, index))


def _config_payload(
    paths: ExpansionPaths,
    windows: pd.DataFrame,
    cp02_indices: list[int],
    new_indices: list[int],
    target: int,
    revision: str | None,
    lookback: int,
) -> dict[str, Any]:
    stages = stage_plan(new_indices)
    selected = sorted([*cp02_indices, *new_indices])
    report = _read_json(paths.snapshot_dir / "report.json")
    windows_sha = report.get("windows_parquet_sha256")
    return {
        "schema_version": EXPANSION_SCHEMA,
        "experiment": "S02-CP06A",
        "dataset_id": report.get("dataset_id"),
        "model": "mistralai/Timestral-209M",
        "revision": revision,
        "device": "cpu",
        "target_windows": int(target),
        "lookback": int(lookback),
        "horizon": HORIZON_HOURS,
        "source_manifest_sha256": report.get("source_manifest_sha256"),
        "windows_sha256": windows_sha,
        "cp02_dir": str(paths.cp02_dir),
        "cp02_indices": cp02_indices,
        "new_indices": new_indices,
        "selected_indices": selected,
        "window_indices": selected,
        "stages": stages,
        "stage_policy": {
            "smoke": f"first {SMOKE_SIZE} new windows",
            "verification": f"next {VERIFICATION_SIZE} new windows",
            "expand": "all remaining new windows",
        },
        "selection_method": "evenly spaced in chronological snapshot index, excluding CP02 indices",
        "created_utc": _now(),
    }


def _resolve_revision(model_name: str) -> str | None:
    try:
        from huggingface_hub import HfApi

        return str(HfApi().model_info(model_name).sha)
    except Exception:
        return None


def prepare(
    paths: ExpansionPaths = ExpansionPaths(),
    *,
    target: int = DEFAULT_TARGET,
    lookback: int = 168,
    revision: str | None = None,
) -> dict[str, Any]:
    windows = load_snapshot_windows(paths)
    cp02_indices = load_cp02_indices(paths)
    output = paths.output_dir
    config_path = output / "config.json"
    if config_path.is_file():
        config = _read_json(config_path)
        if int(config.get("target_windows", -1)) != int(target):
            raise ValueError("Existing expansion target differs")
        if config.get("cp02_indices") != cp02_indices:
            raise ValueError("Existing expansion CP02 set differs")
        if config.get("windows_sha256") != _read_json(paths.snapshot_dir / "report.json").get("windows_parquet_sha256"):
            raise ValueError("Existing expansion snapshot differs")
        _ensure_reused(paths, windows, cp02_indices)
        return config
    if output.exists() and any(output.iterdir()):
        raise ValueError("Nonempty expansion directory without config; refusing to mix runs")
    output.mkdir(parents=True, exist_ok=True)
    new_indices = select_new_indices(len(windows), cp02_indices, target)
    if revision is None:
        revision = _resolve_revision("mistralai/Timestral-209M")
    config = _config_payload(paths, windows, cp02_indices, new_indices, target, revision, lookback)
    _write_json_atomic(config_path, config)
    selected = config["selected_indices"]
    records = []
    for index in selected:
        row = _window_row(windows, index)
        records.append(
            {
                "window_index": int(index),
                "context_start_utc": row["context_start_utc"],
                "forecast_start_utc": row["forecast_start_utc"],
                "target_end_exclusive_utc": row["target_end_exclusive_utc"],
                "source": "S02-CP02-reused" if index in set(cp02_indices) else "new-inference",
                "stage": next(
                    (name for name, members in config["stages"].items() if index in members),
                    "reused",
                ),
            }
        )
    pd.DataFrame(records).to_parquet(output / "windows.parquet", index=False)
    _ensure_reused(paths, windows, cp02_indices)
    _write_json_atomic(
        output / "manifest.json",
        {
            "schema_version": EXPANSION_SCHEMA,
            "created_utc": _now(),
            "source_hashes_before": _source_hashes(paths),
            "config_sha256": digest(config_path),
            "windows_sha256": digest(output / "windows.parquet"),
            "original_artifacts_modified": False,
        },
    )
    _write_progress(paths, config, stage=None)
    return config


def _progress(paths: ExpansionPaths, config: dict[str, Any], *, validate: bool = False) -> dict[str, Any]:
    new_indices = [int(index) for index in config["new_indices"]]
    cp02_indices = [int(index) for index in config["cp02_indices"]]
    completed_new = []
    invalid = []
    for index in new_indices:
        folder = paths.output_dir / f"window-{index:04d}"
        if folder.exists():
            if validate:
                try:
                    _validate_existing_window(paths, index)
                    completed_new.append(index)
                except Exception as exc:
                    invalid.append({"window_index": index, "error": str(exc)})
            else:
                completed_new.append(index)
    return {
        "target_windows": len(cp02_indices) + len(new_indices),
        "reused_cp02": len(cp02_indices),
        "new_planned": len(new_indices),
        "new_completed": len(completed_new),
        "new_pending": len(new_indices) - len(completed_new) - len(invalid),
        "invalid_windows": invalid,
        "completed_new_indices": completed_new,
        "updated_utc": _now(),
    }


def _write_progress(
    paths: ExpansionPaths,
    config: dict[str, Any],
    *,
    stage: str | None,
    validate: bool = False,
) -> dict[str, Any]:
    payload = _progress(paths, config, validate=validate)
    payload["last_stage"] = stage
    _write_json_atomic(paths.output_dir / "progress.json", payload)
    return payload


def _flatten_prediction(value: Any, expected: int, label: str) -> np.ndarray:
    if hasattr(value, "detach"):
        value = value.detach().cpu().numpy()
    array = np.asarray(value, dtype=np.float64).reshape(-1)
    if len(array) != expected or not np.isfinite(array).all():
        raise ValueError(f"Invalid Timestral {label}: expected {expected} finite values")
    return array


def _infer_window(
    paths: ExpansionPaths,
    model: Any,
    config: dict[str, Any],
    index: int,
    windows: pd.DataFrame,
) -> dict[str, Any]:
    import torch

    row = _window_row(windows, index)
    context, actual, provenance = load_window(paths.data_root, paths.snapshot_dir, index)
    if _utc(provenance["forecast_start_utc"]) != _utc(row["forecast_start_utc"]):
        raise ValueError(f"Snapshot window cutoff mismatch: {index}")
    with torch.inference_mode():
        prediction = model.forecast(context, prediction_length=len(actual))
    p50 = _flatten_prediction(prediction.median, len(actual), "median")
    p10 = _flatten_prediction(prediction.quantile(0.1), len(actual), "p10")
    p90 = _flatten_prediction(prediction.quantile(0.9), len(actual), "p90")
    if np.any(p10 > p50) or np.any(p50 > p90):
        raise ValueError(f"Quantiles out of order for window {index}")
    anchor = float(context[-1])
    naive = np.full(len(actual), anchor)
    timestamps = pd.date_range(provenance["forecast_start_utc"], periods=len(actual), freq="h", tz="UTC")
    frame = _forecast_frame(timestamps, actual, p10, p50, p90, naive)
    metrics = {
        "schema_version": FORECAST_SCHEMA,
        "experiment": "S02-CP06A",
        "source": "new-inference",
        "reused": False,
        "model": config["model"],
        "revision_requested": config.get("revision"),
        "device": config.get("device", "cpu"),
        "window_index": int(index),
        "lookback": len(context),
        "horizon": len(actual),
        "provenance": provenance,
        "last_context_close": anchor,
        "metrics": _window_metrics(frame, anchor),
    }
    return _write_window(paths, index, frame, metrics)


def run_stage(
    paths: ExpansionPaths = ExpansionPaths(),
    *,
    stage: str,
    target: int = DEFAULT_TARGET,
    lookback: int = 168,
    revision: str | None = None,
) -> dict[str, Any]:
    if stage not in STAGES:
        raise ValueError(f"Unknown stage: {stage}")
    config = prepare(paths, target=target, lookback=lookback, revision=revision)
    windows = load_snapshot_windows(paths)
    indices = config["stages"]["expand"] if stage == "expand" else config["stages"][stage]
    pending = []
    for index in indices:
        folder = paths.output_dir / f"window-{index:04d}"
        if folder.exists():
            _validate_existing_window(paths, index)
        else:
            pending.append(int(index))
    result = {
        "stage": stage,
        "planned": len(indices),
        "pending": len(pending),
        "completed": 0,
        "skipped": len(indices) - len(pending),
        "indices": [int(index) for index in indices],
        "model_revision": config.get("revision"),
    }
    if not pending:
        result["progress"] = _write_progress(paths, config, stage=stage)
        _write_stage_checkpoint(paths, stage, result)
        return result
    import torch
    from timestral import TimestralForecaster

    torch.set_num_threads(min(8, max(1, torch.get_num_threads())))
    kwargs = {"device": config.get("device", "cpu")}
    if config.get("revision"):
        kwargs["revision"] = config["revision"]
    started = time.time()
    model = TimestralForecaster.from_pretrained(config["model"], **kwargs)
    for position, index in enumerate(pending, start=1):
        _infer_window(paths, model, config, index, windows)
        result["completed"] += 1
        progress = _write_progress(paths, config, stage=stage)
        print(
            f"[{stage} {position}/{len(pending)}] window {index} complete; "
            f"new {progress['new_completed']}/{progress['new_planned']}",
            flush=True,
        )
    result["elapsed_seconds"] = time.time() - started
    result["progress"] = _write_progress(paths, config, stage=stage)
    _write_stage_checkpoint(paths, stage, result)
    return result


def _write_stage_checkpoint(paths: ExpansionPaths, stage: str, result: dict[str, Any]) -> None:
    _write_json_atomic(
        paths.output_dir / "stages" / f"{stage}.json",
        {"stage": stage, "finished_utc": _now(), **result},
    )


def _aggregate_metrics(actual: np.ndarray, predicted: np.ndarray, anchor: np.ndarray) -> dict[str, Any]:
    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    anchor = np.asarray(anchor, dtype=float)
    error = predicted - actual
    direction_mask = np.isfinite(actual) & np.isfinite(predicted) & (actual != anchor) & (predicted != anchor)
    direction = float(
        np.mean(np.sign(predicted[direction_mask] - anchor[direction_mask]) == np.sign(actual[direction_mask] - anchor[direction_mask]))
    ) if direction_mask.any() else None
    return {
        "samples": int(len(actual)),
        "mae_usdt": float(np.mean(np.abs(error))),
        "rmse_usdt": float(np.sqrt(np.mean(error**2))),
        "bias_usdt": float(np.mean(error)),
        "mape_pct": float(np.mean(np.abs(error) / actual) * 100.0),
        "directional_accuracy": direction,
        "direction_evaluable": int(direction_mask.sum()),
    }


def build_canonical_predictions(paths: ExpansionPaths) -> pd.DataFrame:
    config = _read_json(paths.output_dir / "config.json")
    windows = pd.read_parquet(paths.output_dir / "windows.parquet")
    records = []
    for row in windows.itertuples(index=False):
        index = int(row.window_index)
        metrics = _validate_existing_window(paths, index)
        frame = pd.read_parquet(paths.output_dir / f"window-{index:04d}" / "forecast.parquet")
        last = frame.iloc[HORIZON_HOURS - 1]
        anchor = float(metrics["last_context_close"])
        record = h24_record(
            window_index=index,
            cutoff=_utc(metrics["provenance"]["forecast_start_utc"]),
            anchor_price=anchor,
            actual_price=float(last["actual"]),
            timestral_p10=float(last["timestral_p10"]),
            timestral_p50=float(last["timestral_p50"]),
            timestral_p90=float(last["timestral_p90"]),
            naive_price=float(last["naive"]),
            source=str(metrics.get("source")),
            model_version=str(metrics.get("model")) + (f"@{metrics['revision_requested']}" if metrics.get("revision_requested") else ""),
            forecast_sha256=metrics.get("forecast_sha256"),
            snapshot_id=config.get("dataset_id"),
        )
        record["reused_cp02"] = bool(metrics.get("reused"))
        records.append(record)
    canonical = pd.DataFrame(records).sort_values("window_index").reset_index(drop=True)
    return validate_h24_frame(canonical)


def benchmark(paths: ExpansionPaths = ExpansionPaths()) -> dict[str, Any]:
    config = _read_json(paths.output_dir / "config.json")
    canonical = build_canonical_predictions(paths)
    canonical.to_parquet(paths.output_dir / "canonical_predictions.parquet", index=False)
    actual = canonical["actual_price_24h"].to_numpy(dtype=float)
    anchor = canonical["anchor_price"].to_numpy(dtype=float)
    timestral = canonical["timestral_price_24h"].to_numpy(dtype=float)
    naive = canonical["naive_price_24h"].to_numpy(dtype=float)
    summary: dict[str, Any] = {
        "schema_version": EXPANSION_SCHEMA,
        "canonical_schema": CANONICAL_SCHEMA,
        "windows": len(canonical),
        "new_inference_windows": int((~canonical["reused_cp02"]).sum()),
        "reused_cp02_windows": int(canonical["reused_cp02"].sum()),
        "target_rows": len(canonical),
        "hourly_target_rows": len(canonical) * HORIZON_HOURS,
        "canonical_predictions_sha256": digest(paths.output_dir / "canonical_predictions.parquet"),
        "timestral_vs_naive": {
            "timestral": _aggregate_metrics(actual, timestral, anchor),
            "naive": _aggregate_metrics(actual, naive, anchor),
        },
        "by_source": {},
        "by_year": [],
    }
    for source, group in canonical.groupby("source"):
        summary["by_source"][source] = {
            "windows": len(group),
            "timestral": _aggregate_metrics(
                group["actual_price_24h"].to_numpy(float),
                group["timestral_price_24h"].to_numpy(float),
                group["anchor_price"].to_numpy(float),
            ),
            "naive": _aggregate_metrics(
                group["actual_price_24h"].to_numpy(float),
                group["naive_price_24h"].to_numpy(float),
                group["anchor_price"].to_numpy(float),
            ),
        }
    for year, group in canonical.groupby(canonical["cutoff"].dt.year):
        summary["by_year"].append(
            {
                "year": int(year),
                "windows": len(group),
                "timestral_mae_usdt": _aggregate_metrics(
                    group["actual_price_24h"].to_numpy(float),
                    group["timestral_price_24h"].to_numpy(float),
                    group["anchor_price"].to_numpy(float),
                )["mae_usdt"],
                "naive_mae_usdt": _aggregate_metrics(
                    group["actual_price_24h"].to_numpy(float),
                    group["naive_price_24h"].to_numpy(float),
                    group["anchor_price"].to_numpy(float),
                )["mae_usdt"],
            }
        )
    cp05r_path = paths.cp05r_dir / "predictions.parquet"
    if cp05r_path.is_file():
        cp05r = pd.read_parquet(cp05r_path)
        common = canonical.merge(
            cp05r[["window_index", "ridge_price_24h", "histgb_price_24h"]],
            on="window_index",
            how="inner",
            validate="one_to_one",
        )
        summary["cp05r_overlap"] = {
            "windows": len(common),
            "timestral": _aggregate_metrics(common["actual_price_24h"].to_numpy(float), common["timestral_price_24h"].to_numpy(float), common["anchor_price"].to_numpy(float)),
            "naive": _aggregate_metrics(common["actual_price_24h"].to_numpy(float), common["naive_price_24h"].to_numpy(float), common["anchor_price"].to_numpy(float)),
            "ridge": _aggregate_metrics(common["actual_price_24h"].to_numpy(float), common["ridge_price_24h"].to_numpy(float), common["anchor_price"].to_numpy(float)),
            "histgb": _aggregate_metrics(common["actual_price_24h"].to_numpy(float), common["histgb_price_24h"].to_numpy(float), common["anchor_price"].to_numpy(float)),
        }
    summary["canonical_checks"] = {
        "unique_windows": bool(canonical["window_index"].is_unique),
        "target_equals_cutoff_plus_23h": bool((canonical["target"] == canonical["cutoff"] + TARGET_OFFSET).all()),
        "all_target_windows": len(canonical) == int(config.get("target_windows", -1)),
        "all_quantiles_ordered": bool(
            (canonical["timestral_p10"] <= canonical["timestral_p50"]).all()
            and (canonical["timestral_p50"] <= canonical["timestral_p90"]).all()
        ),
    }
    _write_json_atomic(paths.output_dir / "benchmark_summary.json", summary)
    progress = _write_progress(paths, config, stage="benchmark")
    summary["progress"] = progress
    _write_json_atomic(paths.output_dir / "summary.json", summary)
    manifest_path = paths.output_dir / "manifest.json"
    previous = _read_json(manifest_path) if manifest_path.is_file() else {}
    before = previous.get("source_hashes_before") or _source_hashes(paths)
    after = _source_hashes(paths)
    _write_json_atomic(
        manifest_path,
        {
            "schema_version": EXPANSION_SCHEMA,
            "created_utc": previous.get("created_utc", _now()),
            "updated_utc": _now(),
            "source_hashes_before": before,
            "source_hashes_after": after,
            "source_hashes_unchanged": before == after,
            "config_sha256": digest(paths.output_dir / "config.json"),
            "windows_index_sha256": digest(paths.output_dir / "windows.parquet"),
            "canonical_predictions_sha256": digest(paths.output_dir / "canonical_predictions.parquet"),
            "benchmark_summary_sha256": digest(paths.output_dir / "benchmark_summary.json"),
            "summary_sha256": digest(paths.output_dir / "summary.json"),
            "model_revision": config.get("revision"),
            "original_artifacts_modified": before != after,
        },
    )
    if before != after:
        raise RuntimeError("Source artifacts changed during expansion benchmark")
    return summary


def status(paths: ExpansionPaths = ExpansionPaths()) -> dict[str, Any]:
    config = _read_json(paths.output_dir / "config.json")
    progress = _progress(paths, config, validate=True)
    return {
        "config": config,
        "progress": progress,
        "stages": {
            name: {
                "planned": len(indices),
                "present": sum(
                    1
                    for index in indices
                    if (paths.output_dir / f"window-{int(index):04d}").exists()
                ),
            }
            for name, indices in config["stages"].items()
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S02-CP06A historical Timestral expansion")
    parser.add_argument("command", choices=("prepare", "run", "status", "benchmark"))
    parser.add_argument("--stage", choices=STAGES)
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET)
    parser.add_argument("--lookback", type=int, default=168)
    parser.add_argument("--revision", default=None)
    parser.add_argument("--data-root", default=str(ExpansionPaths.data_root))
    parser.add_argument("--snapshot-dir", default=str(ExpansionPaths.snapshot_dir))
    parser.add_argument("--cp02-dir", default=str(ExpansionPaths.cp02_dir))
    parser.add_argument("--cp05r-dir", default=str(ExpansionPaths.cp05r_dir))
    parser.add_argument("--output-dir", default=str(ExpansionPaths.output_dir))
    args = parser.parse_args(argv)
    paths = ExpansionPaths(
        Path(args.data_root),
        Path(args.snapshot_dir),
        Path(args.cp02_dir),
        Path(args.cp05r_dir),
        Path(args.output_dir),
    )
    if args.command == "prepare":
        result = prepare(paths, target=args.target, lookback=args.lookback, revision=args.revision)
    elif args.command == "run":
        if not args.stage:
            parser.error("--stage is required with run")
        result = run_stage(paths, stage=args.stage, target=args.target, lookback=args.lookback, revision=args.revision)
    elif args.command == "status":
        result = status(paths)
    else:
        result = benchmark(paths)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
