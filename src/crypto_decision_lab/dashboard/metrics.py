"""Leakage-safe forecast metrics and regime diagnostics."""
from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd

from .forecast_adapter import ALIGNMENT_KEYS, align_models


def _direction(value: float, anchor: float) -> float:
    if not np.isfinite(value) or not np.isfinite(anchor) or value == anchor:
        return np.nan
    return 1.0 if value > anchor else -1.0


def add_error_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Add signed errors, percentage errors and directions.

    ``error = predicted - actual``.  Direction is measured from the price at
    the cutoff, which is observable when the forecast is issued.
    """

    data = frame.copy()
    if data.empty:
        for column in (
            "error",
            "abs_error",
            "pct_error",
            "signed_pct_error",
            "predicted_direction",
            "actual_direction",
            "direction_correct",
        ):
            data[column] = pd.Series(dtype="float64")
        return data
    data["predicted"] = pd.to_numeric(data["predicted"], errors="coerce")
    data["actual"] = pd.to_numeric(data["actual"], errors="coerce")
    if "anchor_price" not in data.columns:
        data["anchor_price"] = np.nan
    data["anchor_price"] = pd.to_numeric(data["anchor_price"], errors="coerce")
    data["error"] = data["predicted"] - data["actual"]
    data["abs_error"] = data["error"].abs()
    data["pct_error"] = np.where(data["actual"].abs() > 0, data["abs_error"] / data["actual"].abs() * 100.0, np.nan)
    data["signed_pct_error"] = np.where(
        data["actual"].abs() > 0, data["error"] / data["actual"].abs() * 100.0, np.nan
    )
    data["predicted_direction"] = [
        _direction(predicted, anchor)
        for predicted, anchor in zip(data["predicted"], data["anchor_price"])
    ]
    data["actual_direction"] = [
        _direction(actual, anchor)
        for actual, anchor in zip(data["actual"], data["anchor_price"])
    ]
    data["direction_correct"] = [
        1.0 if pd.notna(predicted_dir) and pd.notna(actual_dir) and predicted_dir == actual_dir else
        0.0 if pd.notna(predicted_dir) and pd.notna(actual_dir) else np.nan
        for predicted_dir, actual_dir in zip(data["predicted_direction"], data["actual_direction"])
    ]
    return data


def compute_metrics(frame: pd.DataFrame) -> dict:
    """Compute MAE/RMSE/bias/MAPE/direction metrics on supplied rows only."""

    data = add_error_columns(frame)
    valid = data[np.isfinite(data["predicted"]) & np.isfinite(data["actual"])].copy()
    if valid.empty:
        return {
            "samples": 0,
            "mae": np.nan,
            "rmse": np.nan,
            "bias": np.nan,
            "mape": np.nan,
            "directional_accuracy": np.nan,
            "direction_evaluable": 0,
            "residual_std": np.nan,
            "residual_q05": np.nan,
            "residual_q50": np.nan,
            "residual_q95": np.nan,
        }
    error = valid["error"].to_numpy(dtype=float)
    direction = valid["direction_correct"].dropna()
    return {
        "samples": int(len(valid)),
        "mae": float(np.mean(np.abs(error))),
        "rmse": float(np.sqrt(np.mean(error**2))),
        "bias": float(np.mean(error)),
        "mape": float(valid["pct_error"].mean()),
        "directional_accuracy": float(direction.mean()) if len(direction) else np.nan,
        "direction_evaluable": int(len(direction)),
        "residual_std": float(np.std(error, ddof=1)) if len(error) > 1 else np.nan,
        "residual_q05": float(np.quantile(error, 0.05)),
        "residual_q50": float(np.quantile(error, 0.50)),
        "residual_q95": float(np.quantile(error, 0.95)),
    }


def metrics_table(
    points: pd.DataFrame,
    models: Iterable[str],
    *,
    horizon: int | None = None,
    naive_model: str = "naive",
) -> pd.DataFrame:
    """Return metrics for models aligned on identical cutoff/target samples."""

    requested = list(dict.fromkeys(models))
    aligned = align_models(points, requested, horizon=horizon)
    if aligned.empty:
        return pd.DataFrame(
            columns=[
                "model",
                "model_label",
                "samples",
                "mae",
                "rmse",
                "bias",
                "mape",
                "directional_accuracy",
                "direction_evaluable",
                "naive_mae",
                "improvement_vs_naive_pct",
            ]
        )
    baseline = points[points["model"] == naive_model][
        [*ALIGNMENT_KEYS, "predicted"]
    ].rename(columns={"predicted": "naive_predicted"})
    baseline = baseline.drop_duplicates(ALIGNMENT_KEYS)
    merged = aligned.merge(baseline, on=ALIGNMENT_KEYS, how="left", validate="many_to_one")
    rows = []
    for model in requested:
        subset = merged[merged["model"] == model]
        metrics = compute_metrics(subset)
        naive_error = (subset["naive_predicted"] - subset["actual"]).abs().dropna()
        naive_mae = float(naive_error.mean()) if len(naive_error) else np.nan
        improvement = (
            100.0 * (1.0 - metrics["mae"] / naive_mae)
            if pd.notna(metrics["mae"]) and pd.notna(naive_mae) and naive_mae != 0
            else np.nan
        )
        info = subset["model_label"].iloc[0] if not subset.empty else model
        rows.append(
            {
                "model": model,
                "model_label": info,
                **metrics,
                "naive_mae": naive_mae,
                "improvement_vs_naive_pct": improvement,
            }
        )
    return pd.DataFrame(rows)


def common_sample_count(points: pd.DataFrame, models: Iterable[str], *, horizon: int | None = None) -> int:
    aligned = align_models(points, models, horizon=horizon)
    if aligned.empty:
        return 0
    return int(len(aligned.drop_duplicates(ALIGNMENT_KEYS)))


def residuals_long(
    points: pd.DataFrame, models: Iterable[str], *, horizon: int | None = None
) -> pd.DataFrame:
    aligned = align_models(points, models, horizon=horizon)
    return add_error_columns(aligned)


def _market_feature_at_cutoff(frame: pd.DataFrame, cutoff: pd.Timestamp, window: int) -> tuple[float, float]:
    """Past-only volatility/trend observed before the cutoff close."""

    history = frame.loc[frame.index < cutoff, "close"].astype(float)
    if len(history) < max(24, window // 2):
        return np.nan, np.nan
    recent = history.iloc[-(int(window) + 1):]
    if len(recent) < 2:
        return np.nan, np.nan
    returns = np.diff(np.log(recent.to_numpy(dtype=float)))
    volatility = float(np.std(returns, ddof=1)) if len(returns) > 1 else np.nan
    trend = float(recent.iloc[-1] / recent.iloc[0] - 1.0)
    return volatility, trend


def _regime_labels(value: float, low: float, high: float, labels: tuple[str, str, str]) -> str:
    if not np.isfinite(value):
        return "Unknown"
    if value < low:
        return labels[0]
    if value < high:
        return labels[1]
    return labels[2]


def assign_regimes(
    points: pd.DataFrame,
    market_frame: pd.DataFrame,
    *,
    vol_window: int = 168,
    vol_bins: tuple[float, float] = (0.005, 0.008),
    trend_bins: tuple[float, float] = (-0.05, 0.05),
) -> pd.DataFrame:
    """Attach past-only volatility/trend regimes to each forecast window.

    Features are computed from closes strictly before the cutoff.  They are
    never taken from the target horizon.
    """

    if points.empty:
        data = points.copy()
        data["volatility_168h"] = pd.Series(dtype=float)
        data["trend_168h"] = pd.Series(dtype=float)
        data["volatility_regime"] = pd.Series(dtype=object)
        data["trend_regime"] = pd.Series(dtype=object)
        return data
    unique = (
        points[["window_index", "cutoff"]]
        .drop_duplicates()
        .sort_values(["cutoff", "window_index"])
    )
    regimes = []
    for row in unique.itertuples(index=False):
        volatility, trend = _market_feature_at_cutoff(market_frame, pd.Timestamp(row.cutoff), vol_window)
        regimes.append(
            {
                "window_index": int(row.window_index),
                "cutoff": pd.Timestamp(row.cutoff),
                "volatility_168h": volatility,
                "trend_168h": trend,
                "volatility_regime": _regime_labels(
                    volatility, float(vol_bins[0]), float(vol_bins[1]), ("Low", "Mid", "High")
                ),
                "trend_regime": _regime_labels(
                    trend, float(trend_bins[0]), float(trend_bins[1]), ("Down", "Sideways", "Up")
                ),
            }
        )
    regime_frame = pd.DataFrame(regimes)
    base = points.drop(
        columns=["volatility_168h", "trend_168h", "volatility_regime", "trend_regime"],
        errors="ignore",
    )
    return base.merge(
        regime_frame,
        on=["window_index", "cutoff"],
        how="left",
        validate="many_to_one",
    )


def regime_metrics(
    frame: pd.DataFrame,
    regime_column: str,
    models: Iterable[str] | None = None,
) -> pd.DataFrame:
    """MAE/RMSE/bias grouped by a precomputed regime label."""

    data = add_error_columns(frame)
    if models is not None:
        data = data[data["model"].isin(list(models))]
    data = data[np.isfinite(data["predicted"]) & np.isfinite(data["actual"])]
    if data.empty:
        return pd.DataFrame(columns=[regime_column, "model", "samples", "mae", "rmse", "bias"])
    rows = []
    for (regime, model), group in data.groupby([regime_column, "model"], dropna=False):
        metrics = compute_metrics(group)
        rows.append(
            {
                regime_column: regime,
                "model": model,
                "model_label": group["model_label"].iloc[0],
                "samples": metrics["samples"],
                "mae": metrics["mae"],
                "rmse": metrics["rmse"],
                "bias": metrics["bias"],
            }
        )
    return pd.DataFrame(rows)


def model_label_map(points: pd.DataFrame) -> dict[str, str]:
    if points.empty:
        return {}
    return points.drop_duplicates("model").set_index("model")["model_label"].to_dict()
