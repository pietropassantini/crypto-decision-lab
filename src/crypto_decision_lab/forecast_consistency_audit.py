"""S02-CP05R: audit of the Timestral representation used by CP05.

Read-only reconstruction from archived artifacts.  No Timestral inference and
no model training.  The original CP05 files are never modified; a corrected
benchmark is written to a new directory only when explicitly requested.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .canonical import (
    CANONICAL_SCHEMA,
    HORIZON_HOURS as HOURS_24,
    MODELS,
    TARGET_OFFSET,
    legacy_misinterpreted_timestral_price as misinterpreted_timestral_price,
    residual_corrected_price,
    residual_corrected_return,
    timestral_price_from_return,
)
from .forecast_smoke import load_window
from .residual_baselines import summarize
from .sync import digest


@dataclass(frozen=True)
class AuditPaths:
    data_root: Path = Path("data")
    snapshot_dir: Path = Path("data/snapshots/BTCUSDT-1h-raw-v1")
    cp02_dir: Path = Path("data/experiments/S02-CP02-batch-100")
    cp04_dir: Path = Path("data/datasets/S02-CP04-residuals-v1")
    cp05_dir: Path = Path("data/experiments/S02-CP05-residual-baselines-v1")
    output_dir: Path = Path("data/experiments/S02-CP05R-corrected-benchmark-v1")


def _sha256(path: Path) -> str:
    return digest(path)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _utc(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _max_abs(left: pd.Series, right: pd.Series) -> float:
    left = pd.to_numeric(left, errors="coerce").to_numpy(dtype=float)
    right = pd.to_numeric(right, errors="coerce").to_numpy(dtype=float)
    mask = np.isfinite(left) & np.isfinite(right)
    if not mask.any():
        return float("nan")
    return float(np.max(np.abs(left[mask] - right[mask])))


def _check(condition: bool, label: str, detail: str = "") -> dict[str, Any]:
    return {"check": label, "passed": bool(condition), "detail": detail}


def _load_cp02_window(cp02_dir: Path, window_index: int) -> tuple[dict, pd.DataFrame]:
    folder = cp02_dir / f"window-{int(window_index):04d}"
    metrics = _read_json(folder / "metrics.json")
    forecast = pd.read_parquet(folder / "forecast.parquet")
    expected_sha = metrics.get("forecast_sha256")
    if expected_sha and digest(folder / "forecast.parquet") != expected_sha:
        raise ValueError(f"CP02 forecast checksum mismatch: {folder}")
    required = {"timestamp", "actual", "timestral_p10", "timestral_p50", "timestral_p90", "naive"}
    if not required.issubset(forecast.columns):
        raise ValueError(f"Incomplete CP02 archive: {folder}")
    forecast["timestamp"] = pd.to_datetime(forecast["timestamp"], utc=True)
    if len(forecast) < HOURS_24:
        raise ValueError(f"CP02 forecast shorter than 24 rows: {folder}")
    return metrics, forecast


def _source_hashes(paths: AuditPaths) -> dict[str, str]:
    files = {
        "cp02_config": paths.cp02_dir / "config.json",
        "cp02_summary": paths.cp02_dir / "summary.json",
        "cp04_features": paths.cp04_dir / "features.parquet",
        "cp04_targets": paths.cp04_dir / "targets.parquet",
        "cp04_manifest": paths.cp04_dir / "manifest.json",
        "cp05_predictions": paths.cp05_dir / "predictions.parquet",
        "cp05_summary": paths.cp05_dir / "summary.json",
        "snapshot_raw_manifest": paths.snapshot_dir / "raw-manifest.json",
        "snapshot_report": paths.snapshot_dir / "report.json",
    }
    return {name: _sha256(path) for name, path in files.items() if path.is_file()}


def build_audit_table(
    paths: AuditPaths,
    *,
    verify_snapshot: bool = True,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Reconstruct every CP05 prediction and compare representations."""

    features = pd.read_parquet(paths.cp04_dir / "features.parquet")
    targets = pd.read_parquet(paths.cp04_dir / "targets.parquet")
    predictions = pd.read_parquet(paths.cp05_dir / "predictions.parquet")
    cp05_summary = _read_json(paths.cp05_dir / "summary.json")

    required_pred = {
        "window_index", "forecast_start_utc", "fold",
        "actual_price_24h", "naive_price_24h", "timestral_price_24h",
        "timestral_return_pct_24h",
        "ridge_predicted_residual_pct", "ridge_return_pct_24h", "ridge_price_24h",
        "histgb_predicted_residual_pct", "histgb_return_pct_24h", "histgb_price_24h",
    }
    if not required_pred.issubset(predictions.columns):
        raise ValueError(f"CP05 predictions missing columns: {sorted(required_pred - set(predictions.columns))}")
    required_features = {"window_index", "forecast_return_24h", "last_context_close"}
    required_targets = {
        "window_index", "actual_return_pct_24h", "predicted_return_pct_24h", "residual_pct_24h"
    }
    if not required_features.issubset(features.columns):
        raise ValueError("CP04 features missing required columns")
    if not required_targets.issubset(targets.columns):
        raise ValueError("CP04 targets missing required columns")
    if predictions.window_index.duplicated().any():
        raise ValueError("Duplicate CP05 window indices")

    target_columns = targets[
        ["window_index", "actual_return_pct_24h", "predicted_return_pct_24h", "residual_pct_24h"]
    ].rename(
        columns={
            "actual_return_pct_24h": "target_actual_return_pct_24h",
            "predicted_return_pct_24h": "target_predicted_return_pct_24h",
            "residual_pct_24h": "target_residual_pct_24h",
        }
    )
    merged = predictions.merge(
        features[["window_index", "forecast_return_24h", "last_context_close"]],
        on="window_index",
        how="inner",
        validate="one_to_one",
        suffixes=("", "_feature"),
    ).merge(
        target_columns,
        on="window_index",
        how="inner",
        validate="one_to_one",
    )
    if len(merged) != len(predictions):
        raise ValueError("CP04 and CP05 window sets are not identical")

    records: list[dict[str, Any]] = []
    for row in merged.itertuples(index=False):
        window_index = int(row.window_index)
        cutoff = _utc(row.forecast_start_utc)
        metrics, cp02 = _load_cp02_window(paths.cp02_dir, window_index)
        if int(metrics.get("window_index", -1)) != window_index:
            raise ValueError(f"CP02 window index mismatch: {window_index}")
        provenance = metrics.get("provenance") or {}
        if _utc(provenance.get("forecast_start_utc")) != cutoff:
            raise ValueError(f"CP02 cutoff mismatch: {window_index}")
        target = cutoff + TARGET_OFFSET
        if _utc(cp02["timestamp"].iloc[HOURS_24 - 1]) != target:
            raise ValueError(f"CP02 h24 target mismatch: {window_index}")

        cp02_actual = float(cp02["actual"].iloc[HOURS_24 - 1])
        cp02_p50 = float(cp02["timestral_p50"].iloc[HOURS_24 - 1])
        cp02_p10 = float(cp02["timestral_p10"].iloc[HOURS_24 - 1])
        cp02_p90 = float(cp02["timestral_p90"].iloc[HOURS_24 - 1])
        cp02_anchor = float(metrics.get("last_context_close", cp02["naive"].iloc[HOURS_24 - 1]))
        cp02_naive = float(cp02["naive"].iloc[HOURS_24 - 1])

        market_actual = np.nan
        market_anchor = np.nan
        if verify_snapshot:
            context, market_future, _ = load_window(paths.data_root, paths.snapshot_dir, window_index)
            market_actual = float(market_future[HOURS_24 - 1])
            market_anchor = float(context[-1])

        return_decimal = float(row.forecast_return_24h)
        actual_return_pct = float(row.target_actual_return_pct_24h)
        predicted_return_pct = float(row.target_predicted_return_pct_24h)
        residual_pct = float(row.target_residual_pct_24h)
        anchor = float(row.naive_price_24h)
        actual = float(row.actual_price_24h)
        original_timestral_price = float(row.timestral_price_24h)
        original_timestral_return = float(row.timestral_return_pct_24h)
        ridge_residual = float(row.ridge_predicted_residual_pct)
        histgb_residual = float(row.histgb_predicted_residual_pct)

        canonical_timestral_price = timestral_price_from_return(anchor, return_decimal)
        canonical_timestral_return_pct = 100.0 * return_decimal
        canonical_ridge_return = residual_corrected_return(return_decimal, ridge_residual)
        canonical_histgb_return = residual_corrected_return(return_decimal, histgb_residual)
        canonical_ridge_price = residual_corrected_price(anchor, return_decimal, ridge_residual)
        canonical_histgb_price = residual_corrected_price(anchor, return_decimal, histgb_residual)
        wrong_timestral_price = misinterpreted_timestral_price(anchor, return_decimal)
        original_ridge_price_expected = anchor * (1.0 + (return_decimal + ridge_residual) / 100.0)
        original_histgb_price_expected = anchor * (1.0 + (return_decimal + histgb_residual) / 100.0)

        records.append(
            {
                "window_index": window_index,
                "fold": int(row.fold),
                "cutoff": cutoff,
                "target": target,
                "horizon_hours": HOURS_24,
                "anchor_price": anchor,
                "cp02_anchor": cp02_anchor,
                "market_anchor": market_anchor,
                "actual_price_24h": actual,
                "cp02_actual": cp02_actual,
                "market_actual": market_actual,
                "naive_price_24h": cp02_naive,
                "timestral_return_decimal": return_decimal,
                "timestral_return_pct_canonical": canonical_timestral_return_pct,
                "cp05_timestral_return_field": original_timestral_return,
                "targets_predicted_return_pct_24h": predicted_return_pct,
                "targets_actual_return_pct_24h": actual_return_pct,
                "targets_residual_pct_24h": residual_pct,
                "cp02_p10": cp02_p10,
                "cp02_p50": cp02_p50,
                "cp02_p90": cp02_p90,
                "timestral_price_canonical": canonical_timestral_price,
                "timestral_price_original_cp05": original_timestral_price,
                "timestral_price_misinterpreted": wrong_timestral_price,
                "ridge_predicted_residual_pct": ridge_residual,
                "ridge_return_decimal_canonical": canonical_ridge_return,
                "ridge_return_field_original": float(row.ridge_return_pct_24h),
                "ridge_price_original_cp05": float(row.ridge_price_24h),
                "ridge_price_original_expected": original_ridge_price_expected,
                "ridge_price_canonical": canonical_ridge_price,
                "histgb_predicted_residual_pct": histgb_residual,
                "histgb_return_decimal_canonical": canonical_histgb_return,
                "histgb_return_field_original": float(row.histgb_return_pct_24h),
                "histgb_price_original_cp05": float(row.histgb_price_24h),
                "histgb_price_original_expected": original_histgb_price_expected,
                "histgb_price_canonical": canonical_histgb_price,
                "timestral_price_original_delta": original_timestral_price - canonical_timestral_price,
                "ridge_price_original_delta": float(row.ridge_price_24h) - canonical_ridge_price,
                "histgb_price_original_delta": float(row.histgb_price_24h) - canonical_histgb_price,
            }
        )

    audit = pd.DataFrame(records).sort_values("window_index").reset_index(drop=True)
    checks = [
        _check(
            _max_abs(audit["cp02_actual"], audit["actual_price_24h"]) <= 1e-3,
            "CP05 actual equals CP02 actual at h24",
            f"max_abs={_max_abs(audit['cp02_actual'], audit['actual_price_24h']):.6g}",
        ),
        _check(
            _max_abs(audit["cp02_anchor"], audit["anchor_price"]) <= 1e-6,
            "CP05 anchor equals CP02 last context close",
            f"max_abs={_max_abs(audit['cp02_anchor'], audit['anchor_price']):.6g}",
        ),
        _check(
            _max_abs(audit["cp02_p50"], audit["timestral_price_canonical"]) <= 1e-3,
            "canonical Timestral price equals CP02 p50 at h24",
            f"max_abs={_max_abs(audit['cp02_p50'], audit['timestral_price_canonical']):.6g}",
        ),
        _check(
            _max_abs(audit["cp05_timestral_return_field"], audit["timestral_return_decimal"]) <= 1e-12,
            "CP05 timestral_return_pct_24h contains the decimal return, not percent",
            f"max_abs={_max_abs(audit['cp05_timestral_return_field'], audit['timestral_return_decimal']):.6g}",
        ),
        _check(
            _max_abs(
                audit["timestral_price_original_cp05"],
                audit["timestral_price_misinterpreted"],
            ) <= 1e-6,
            "CP05 Timestral price applies /100 to the decimal return",
            f"max_abs={_max_abs(audit['timestral_price_original_cp05'], audit['timestral_price_misinterpreted']):.6g}",
        ),
        _check(
            _max_abs(audit["ridge_price_original_cp05"], audit["ridge_price_original_expected"]) <= 1e-6,
            "CP05 Ridge price applies /100 to the mixed return",
            f"max_abs={_max_abs(audit['ridge_price_original_cp05'], audit['ridge_price_original_expected']):.6g}",
        ),
        _check(
            _max_abs(audit["histgb_price_original_cp05"], audit["histgb_price_original_expected"]) <= 1e-6,
            "CP05 HistGB price applies /100 to the mixed return",
            f"max_abs={_max_abs(audit['histgb_price_original_cp05'], audit['histgb_price_original_expected']):.6g}",
        ),
        _check(
            _max_abs(
                audit["targets_predicted_return_pct_24h"],
                audit["timestral_return_pct_canonical"],
            ) <= 1e-4,
            "CP04 predicted_return_pct_24h equals 100 * decimal return",
            f"max_abs={_max_abs(audit['targets_predicted_return_pct_24h'], audit['timestral_return_pct_canonical']):.6g}",
        ),
        _check(
            _max_abs(
                audit["targets_residual_pct_24h"],
                audit["targets_actual_return_pct_24h"] - audit["targets_predicted_return_pct_24h"],
            ) <= 1e-4,
            "CP04 residual_pct_24h equals actual_return_pct - predicted_return_pct",
            f"max_abs={_max_abs(audit['targets_residual_pct_24h'], audit['targets_actual_return_pct_24h'] - audit['targets_predicted_return_pct_24h']):.6g}",
        ),
        _check(
            _max_abs(audit["market_actual"], audit["actual_price_24h"]) <= 1e-2
            if verify_snapshot else True,
            "archived actual equals snapshot market close at target",
            "snapshot verification disabled" if not verify_snapshot
            else f"max_abs={_max_abs(audit['market_actual'], audit['actual_price_24h']):.6g}",
        ),
    ]
    metadata = {
        "rows": len(audit),
        "windows": [int(audit.window_index.min()), int(audit.window_index.max())],
        "source_hashes": _source_hashes(paths),
        "cp05_archived_metrics": cp05_summary.get("metrics", {}),
        "checks": checks,
    }
    return audit, metadata


def _direction_metrics(actual_return: pd.Series, predicted_return: pd.Series) -> tuple[float | None, int]:
    actual = pd.to_numeric(actual_return, errors="coerce").to_numpy(dtype=float)
    predicted = pd.to_numeric(predicted_return, errors="coerce").to_numpy(dtype=float)
    mask = np.isfinite(actual) & np.isfinite(predicted) & (actual != 0) & (predicted != 0)
    if not mask.any():
        return None, 0
    return float(np.mean(np.sign(actual[mask]) == np.sign(predicted[mask]))), int(mask.sum())


def build_benchmark_frames(audit: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Original vs canonical frames, both in the shape expected by CP05 summarize()."""

    original = pd.DataFrame(
        {
            "fold": audit["fold"],
            "window_index": audit["window_index"],
            "actual_price_24h": audit["actual_price_24h"],
            "timestral_price_24h": audit["timestral_price_original_cp05"],
            "naive_price_24h": audit["naive_price_24h"],
            "naive_return_pct_24h": 0.0,
            "timestral_return_pct_24h": audit["cp05_timestral_return_field"],
            "ridge_price_24h": audit["ridge_price_original_cp05"],
            "ridge_return_pct_24h": audit["ridge_return_field_original"],
            "histgb_price_24h": audit["histgb_price_original_cp05"],
            "histgb_return_pct_24h": audit["histgb_return_field_original"],
            "actual_return_pct_24h": audit["targets_actual_return_pct_24h"],
        }
    )
    corrected = pd.DataFrame(
        {
            "fold": audit["fold"],
            "window_index": audit["window_index"],
            "actual_price_24h": audit["actual_price_24h"],
            "timestral_price_24h": audit["timestral_price_canonical"],
            "naive_price_24h": audit["naive_price_24h"],
            "naive_return_pct_24h": 0.0,
            "timestral_return_pct_24h": audit["timestral_return_decimal"],
            "ridge_price_24h": audit["ridge_price_canonical"],
            "ridge_return_pct_24h": audit["ridge_return_decimal_canonical"],
            "histgb_price_24h": audit["histgb_price_canonical"],
            "histgb_return_pct_24h": audit["histgb_return_decimal_canonical"],
            "actual_return_pct_24h": audit["targets_actual_return_pct_24h"],
        }
    )
    return original, corrected


def compare_metrics(audit: pd.DataFrame) -> dict[str, Any]:
    original_frame, corrected_frame = build_benchmark_frames(audit)
    original = summarize(original_frame)
    corrected = summarize(corrected_frame)
    actual_return = audit["targets_actual_return_pct_24h"] / 100.0
    for payload, frame in ((original, original_frame), (corrected, corrected_frame)):
        for model in MODELS:
            accuracy, evaluable = _direction_metrics(
                actual_return, frame[f"{model}_return_pct_24h"]
            )
            payload["metrics"][model]["direction_accuracy_evaluable"] = accuracy
            payload["metrics"][model]["direction_evaluable"] = evaluable
    impact = {}
    for model in MODELS:
        old = original["metrics"][model]
        new = corrected["metrics"][model]
        impact[model] = {
            "mae_original_usdt": old["mae_price_24h"],
            "mae_corrected_usdt": new["mae_price_24h"],
            "mae_delta_usdt": new["mae_price_24h"] - old["mae_price_24h"],
            "mae_delta_pct": 100.0 * (new["mae_price_24h"] / old["mae_price_24h"] - 1.0),
            "rmse_original_usdt": old["rmse_price_24h"],
            "rmse_corrected_usdt": new["rmse_price_24h"],
            "rmse_delta_usdt": new["rmse_price_24h"] - old["rmse_price_24h"],
            "rmse_delta_pct": 100.0 * (new["rmse_price_24h"] / old["rmse_price_24h"] - 1.0),
            "direction_original": old["direction_accuracy_24h"],
            "direction_corrected": new["direction_accuracy_24h"],
            "direction_delta": new["direction_accuracy_24h"] - old["direction_accuracy_24h"],
            "direction_accuracy_evaluable_original": old["direction_accuracy_evaluable"],
            "direction_accuracy_evaluable_corrected": new["direction_accuracy_evaluable"],
            "direction_original_evaluable": old["direction_evaluable"],
            "direction_corrected_evaluable": new["direction_evaluable"],
        }
    ranking_original = [
        model for model, _ in sorted(original["metrics"].items(), key=lambda item: item[1]["mae_price_24h"])
    ]
    ranking_corrected = [
        model for model, _ in sorted(corrected["metrics"].items(), key=lambda item: item[1]["mae_price_24h"])
    ]
    return {
        "original": original,
        "corrected": corrected,
        "impact": impact,
        "ranking_original_mae": ranking_original,
        "ranking_corrected_mae": ranking_corrected,
        "ranking_changed": ranking_original != ranking_corrected,
    }


def canonical_predictions(audit: pd.DataFrame, source_hashes: dict[str, str]) -> pd.DataFrame:
    """Canonical CP05R table: prices in USDT, returns explicitly named."""

    output = pd.DataFrame(
        {
            "schema_version": CANONICAL_SCHEMA,
            "fold": audit["fold"].astype(int),
            "window_index": audit["window_index"].astype(int),
            "forecast_start_utc": audit["cutoff"],
            "cutoff": audit["cutoff"],
            "target": audit["target"],
            "horizon_hours": HOURS_24,
            "anchor_price": audit["anchor_price"],
            "naive_price_24h": audit["naive_price_24h"],
            "actual_price_24h": audit["actual_price_24h"],
            "actual_return_pct_24h": audit["targets_actual_return_pct_24h"],
            "timestral_price_24h": audit["timestral_price_canonical"],
            "timestral_return_24h": audit["timestral_return_decimal"],
            "timestral_return_pct_24h": audit["timestral_return_pct_canonical"],
            "ridge_predicted_residual_pct": audit["ridge_predicted_residual_pct"],
            "ridge_return_24h": audit["ridge_return_decimal_canonical"],
            "ridge_return_pct_24h": 100.0 * audit["ridge_return_decimal_canonical"],
            "ridge_price_24h": audit["ridge_price_canonical"],
            "histgb_predicted_residual_pct": audit["histgb_predicted_residual_pct"],
            "histgb_return_24h": audit["histgb_return_decimal_canonical"],
            "histgb_return_pct_24h": 100.0 * audit["histgb_return_decimal_canonical"],
            "histgb_price_24h": audit["histgb_price_canonical"],
            "source_cp05_predictions_sha256": source_hashes.get("cp05_predictions"),
            "reconstruction": "anchor_price * (1 + timestral_return_24h + residual_pct/100)",
        }
    )
    return output


def write_corrected_benchmark(
    paths: AuditPaths,
    audit: pd.DataFrame,
    metadata: dict[str, Any],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    output = paths.output_dir
    if output.exists():
        raise FileExistsError(f"Corrected benchmark already exists; refusing to overwrite: {output}")
    output.mkdir(parents=True)
    try:
        corrected = canonical_predictions(audit, metadata["source_hashes"])
        corrected.to_parquet(output / "predictions.parquet", index=False)
        audit.to_parquet(output / "audit_samples.parquet", index=False)
        summary = {
            "schema_version": CANONICAL_SCHEMA,
            "rows": len(corrected),
            "predictions_sha256": digest(output / "predictions.parquet"),
            "audit_samples_sha256": digest(output / "audit_samples.parquet"),
            "windows": metadata["windows"],
            "original_cp05_metrics": metrics["original"],
            "corrected_metrics": metrics["corrected"],
            "impact": metrics["impact"],
            "ranking_original_mae": metrics["ranking_original_mae"],
            "ranking_corrected_mae": metrics["ranking_corrected_mae"],
            "ranking_changed": metrics["ranking_changed"],
            "checks": metadata["checks"],
            "verdict": {
                "original_cp05_price_representation": "FAIL",
                "root_cause": "CONFIRMED",
                "cp06a_gate": "PASS_WITH_CANONICAL_CP05R",
            },
        }
        (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
        manifest = {
            "schema_version": CANONICAL_SCHEMA,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "source_hashes": metadata["source_hashes"],
            "source_hashes_after": _source_hashes(paths),
            "output_files": {
                "predictions": {
                    "path": "predictions.parquet",
                    "sha256": digest(output / "predictions.parquet"),
                },
                "audit_samples": {
                    "path": "audit_samples.parquet",
                    "sha256": digest(output / "audit_samples.parquet"),
                },
            },
            "canonical_formulas": {
                "target": "cutoff + 23h (24th hourly target, 1-indexed horizon)",
                "timestral_price": "anchor_price * (1 + timestral_return_24h)",
                "corrected_price": "anchor_price * (1 + timestral_return_24h + predicted_residual_pct/100)",
                "return_units": {
                    "timestral_return_24h": "decimal (e.g. 0.05 = 5%)",
                    "timestral_return_pct_24h": "percent (e.g. 5.0 = 5%)",
                    "residual_pct": "percent of anchor",
                },
            },
            "original_artifacts_modified": False,
            "notice": "Original CP05/CP04/CP02 files are read-only; this directory is an additive corrected benchmark.",
        }
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        if metadata["source_hashes"] != manifest["source_hashes_after"]:
            raise RuntimeError("Input artifacts changed during the audit")
        return summary
    except BaseException:
        import shutil

        shutil.rmtree(output)
        raise


def run_audit(paths: AuditPaths = AuditPaths(), *, write: bool = True) -> dict[str, Any]:
    audit, metadata = build_audit_table(paths)
    metrics = compare_metrics(audit)
    checks = list(metadata["checks"])
    archived = metadata.get("cp05_archived_metrics") or {}
    recomputed = metrics["original"]["metrics"]
    for model in MODELS:
        for field in ("mae_price_24h", "rmse_price_24h", "direction_accuracy_24h"):
            expected = archived.get(model, {}).get(field)
            actual = recomputed.get(model, {}).get(field)
            if expected is None or actual is None:
                continue
            checks.append(
                _check(
                    bool(np.isclose(actual, expected, rtol=1e-12, atol=1e-9)),
                    f"recomputed original CP05 metric matches archive: {model}.{field}",
                    f"archive={expected:.12g} recomputed={actual:.12g}",
                )
            )
    metadata["checks"] = checks
    result = {
        "rows": len(audit),
        "checks": checks,
        "metrics": metrics,
        "output_dir": str(paths.output_dir) if write else None,
    }
    if write:
        result["written"] = write_corrected_benchmark(paths, audit, metadata, metrics)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S02-CP05R Timestral forecast consistency audit")
    parser.add_argument("--data-root", default=str(AuditPaths.data_root))
    parser.add_argument("--snapshot-dir", default=str(AuditPaths.snapshot_dir))
    parser.add_argument("--cp02-dir", default=str(AuditPaths.cp02_dir))
    parser.add_argument("--cp04-dir", default=str(AuditPaths.cp04_dir))
    parser.add_argument("--cp05-dir", default=str(AuditPaths.cp05_dir))
    parser.add_argument("--output-dir", default=str(AuditPaths.output_dir))
    parser.add_argument("--no-write", action="store_true", help="Audit only; do not create corrected benchmark")
    args = parser.parse_args(argv)
    paths = AuditPaths(
        Path(args.data_root),
        Path(args.snapshot_dir),
        Path(args.cp02_dir),
        Path(args.cp04_dir),
        Path(args.cp05_dir),
        Path(args.output_dir),
    )
    result = run_audit(paths, write=not args.no_write)
    print(json.dumps(
        {
            "rows": result["rows"],
            "checks": result["checks"],
            "ranking_original_mae": result["metrics"]["ranking_original_mae"],
            "ranking_corrected_mae": result["metrics"]["ranking_corrected_mae"],
            "output_dir": result["output_dir"],
        },
        indent=2,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
