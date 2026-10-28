"""Canonical forecast representation shared by CP05R, CP06A and the dashboard.

The canonical unit convention is explicit:

* returns whose name ends in ``_return_*h`` are decimal (0.05 = 5%);
* returns whose name ends in ``_return_pct_*h`` are percent (5.0 = 5%);
* residual values ``*_predicted_residual_pct`` are percent of the anchor;
* prices are USDT;
* horizon 24 is the 24th hourly target, i.e. ``cutoff + 23h``.

Do not add implicit fills, rescaling or future information in this module.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

CANONICAL_SCHEMA = "cdl.canonical.v1"
HORIZON_HOURS = 24
TARGET_OFFSET_HOURS = HORIZON_HOURS - 1
TARGET_OFFSET = pd.Timedelta(hours=TARGET_OFFSET_HOURS)
MODELS = ("timestral", "naive", "ridge", "histgb")


class CanonicalValidationError(ValueError):
    """Raised when a frame violates the canonical forecast contract."""


def target_timestamp(cutoff: pd.Timestamp, horizon_hours: int = HORIZON_HOURS) -> pd.Timestamp:
    """Return the target timestamp for a 1-indexed hourly horizon."""

    if int(horizon_hours) < 1:
        raise ValueError("horizon_hours must be >= 1")
    timestamp = pd.Timestamp(cutoff)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return timestamp + pd.Timedelta(hours=int(horizon_hours) - 1)


def timestral_price_from_return(anchor_price: float, timestral_return_decimal: float) -> float:
    """Canonical Timestral price: decimal return, no extra division by 100."""

    return float(anchor_price) * (1.0 + float(timestral_return_decimal))


def decimal_return_from_prices(price: float, anchor_price: float) -> float:
    if float(anchor_price) <= 0:
        raise ValueError("anchor_price must be positive")
    return float(price) / float(anchor_price) - 1.0


def percent_return(return_decimal: float) -> float:
    return 100.0 * float(return_decimal)


def residual_corrected_return(
    timestral_return_decimal: float, predicted_residual_pct: float
) -> float:
    """Add a percent residual to a decimal Timestral return exactly once."""

    return float(timestral_return_decimal) + float(predicted_residual_pct) / 100.0


def residual_corrected_price(
    anchor_price: float,
    timestral_return_decimal: float,
    predicted_residual_pct: float,
) -> float:
    return float(anchor_price) * (
        1.0 + residual_corrected_return(timestral_return_decimal, predicted_residual_pct)
    )


def residual_pct(actual_price: float, predicted_price: float, anchor_price: float) -> float:
    if float(anchor_price) <= 0:
        raise ValueError("anchor_price must be positive")
    return 100.0 * (float(actual_price) - float(predicted_price)) / float(anchor_price)


def legacy_misinterpreted_timestral_price(
    anchor_price: float, timestral_return_decimal: float
) -> float:
    """Reproduce the invalid CP05 formula for audit only. Never use in production."""

    return float(anchor_price) * (1.0 + float(timestral_return_decimal) / 100.0)


def validate_h24_frame(frame: pd.DataFrame, *, require_actual: bool = True) -> pd.DataFrame:
    """Validate canonical h24 predictions without modifying them."""

    required = {
        "window_index",
        "cutoff",
        "target",
        "horizon_hours",
        "anchor_price",
        "timestral_price_24h",
        "naive_price_24h",
        "timestral_return_24h",
        "timestral_return_pct_24h",
    }
    if require_actual:
        required.add("actual_price_24h")
    missing = required - set(frame.columns)
    if missing:
        raise CanonicalValidationError(f"Canonical columns missing: {sorted(missing)}")
    if frame.empty:
        raise CanonicalValidationError("Canonical frame is empty")
    data = frame.copy()
    data["cutoff"] = pd.to_datetime(data["cutoff"], utc=True, errors="raise")
    data["target"] = pd.to_datetime(data["target"], utc=True, errors="raise")
    if data[["window_index", "target"]].duplicated().any():
        raise CanonicalValidationError("Duplicate window_index/target pairs")
    if (data["horizon_hours"].astype(int) != HORIZON_HOURS).any():
        raise CanonicalValidationError("Canonical h24 frame contains a different horizon")
    expected_target = data["cutoff"] + TARGET_OFFSET
    if not (data["target"] == expected_target).all():
        raise CanonicalValidationError("Canonical target is not cutoff + 23h")
    numeric = data[
        [
            "anchor_price",
            "timestral_price_24h",
            "naive_price_24h",
            "timestral_return_24h",
            "timestral_return_pct_24h",
            *(["actual_price_24h"] if require_actual else []),
        ]
    ].to_numpy(dtype=float)
    if not np.isfinite(numeric).all():
        raise CanonicalValidationError("Non-finite canonical values")
    if (data[["anchor_price", "timestral_price_24h", "naive_price_24h"]] <= 0).any().any():
        raise CanonicalValidationError("Non-positive canonical prices")
    expected_price = data["anchor_price"] * (1.0 + data["timestral_return_24h"])
    if not np.allclose(data["timestral_price_24h"], expected_price, rtol=1e-9, atol=1e-6):
        raise CanonicalValidationError("timestral_price_24h does not match canonical return formula")
    if not np.allclose(
        data["timestral_return_pct_24h"],
        100.0 * data["timestral_return_24h"],
        rtol=1e-9,
        atol=1e-8,
    ):
        raise CanonicalValidationError("timestral_return_pct_24h is not 100 * decimal return")
    if require_actual and (data["actual_price_24h"] <= 0).any():
        raise CanonicalValidationError("Non-positive actual prices")
    return data


def h24_record(
    *,
    window_index: int,
    cutoff: pd.Timestamp,
    anchor_price: float,
    actual_price: float,
    timestral_p10: float,
    timestral_p50: float,
    timestral_p90: float,
    naive_price: float,
    source: str,
    model_version: str,
    forecast_sha256: str | None,
    snapshot_id: str | None,
) -> dict:
    """Build one canonical h24 row from a 24-hour forecast path."""

    cutoff = pd.Timestamp(cutoff)
    if cutoff.tzinfo is None:
        cutoff = cutoff.tz_localize("UTC")
    else:
        cutoff = cutoff.tz_convert("UTC")
    anchor = float(anchor_price)
    return_decimal = decimal_return_from_prices(float(timestral_p50), anchor)
    return {
        "schema_version": CANONICAL_SCHEMA,
        "window_index": int(window_index),
        "cutoff": cutoff,
        "target": target_timestamp(cutoff, HORIZON_HOURS),
        "horizon_hours": HORIZON_HOURS,
        "anchor_price": anchor,
        "actual_price_24h": float(actual_price),
        "timestral_p10": float(timestral_p10),
        "timestral_p50": float(timestral_p50),
        "timestral_p90": float(timestral_p90),
        "timestral_price_24h": float(timestral_p50),
        "naive_price_24h": float(naive_price),
        "timestral_return_24h": return_decimal,
        "timestral_return_pct_24h": percent_return(return_decimal),
        "source": source,
        "model_version": model_version,
        "forecast_sha256": forecast_sha256,
        "snapshot_id": snapshot_id,
    }
