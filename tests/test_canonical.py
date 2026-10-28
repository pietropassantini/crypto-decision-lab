from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab.canonical import (
    HORIZON_HOURS,
    TARGET_OFFSET,
    CanonicalValidationError,
    h24_record,
    residual_corrected_price,
    residual_corrected_return,
    target_timestamp,
    timestral_price_from_return,
    validate_h24_frame,
)


def _canonical_frame() -> pd.DataFrame:
    cutoff = pd.Timestamp("2024-01-01 00:00", tz="UTC")
    actual = 105.0
    anchor = 100.0
    timestral = 102.0
    return pd.DataFrame(
        [
            {
                "schema_version": "cdl.canonical.v1",
                "window_index": 1,
                "cutoff": cutoff,
                "target": cutoff + TARGET_OFFSET,
                "horizon_hours": HORIZON_HOURS,
                "anchor_price": anchor,
                "actual_price_24h": actual,
                "timestral_price_24h": timestral,
                "naive_price_24h": anchor,
                "timestral_return_24h": timestral / anchor - 1.0,
                "timestral_return_pct_24h": 100.0 * (timestral / anchor - 1.0),
            }
        ]
    )


def test_target_h24_is_cutoff_plus_23h():
    cutoff = pd.Timestamp("2024-01-01 00:00", tz="UTC")
    assert target_timestamp(cutoff, 1) == cutoff
    assert target_timestamp(cutoff, 24) == cutoff + pd.Timedelta(hours=23)
    assert target_timestamp(cutoff, 24) == cutoff + TARGET_OFFSET


def test_canonical_price_and_residual_formulas():
    assert timestral_price_from_return(100.0, 0.10) == pytest.approx(110.0)
    assert residual_corrected_return(0.10, 2.0) == pytest.approx(0.12)
    assert residual_corrected_price(100.0, 0.10, 2.0) == pytest.approx(112.0)


def test_h24_record_is_canonical():
    cutoff = pd.Timestamp("2024-01-01 00:00", tz="UTC")
    record = h24_record(
        window_index=7,
        cutoff=cutoff,
        anchor_price=100.0,
        actual_price=105.0,
        timestral_p10=99.0,
        timestral_p50=102.0,
        timestral_p90=106.0,
        naive_price=100.0,
        source="test",
        model_version="test@rev",
        forecast_sha256="abc",
        snapshot_id="snap",
    )
    frame = validate_h24_frame(pd.DataFrame([record]))
    assert frame.iloc[0]["target"] == cutoff + TARGET_OFFSET
    assert frame.iloc[0]["timestral_return_pct_24h"] == pytest.approx(2.0)


def test_validate_rejects_wrong_target_and_bad_units():
    frame = _canonical_frame()
    validate_h24_frame(frame)
    wrong = frame.copy()
    wrong.loc[0, "target"] = wrong.loc[0, "cutoff"] + pd.Timedelta(hours=24)
    with pytest.raises(CanonicalValidationError, match="cutoff \\+ 23h"):
        validate_h24_frame(wrong)
    bad = frame.copy()
    bad.loc[0, "timestral_price_24h"] = 100.2
    with pytest.raises(CanonicalValidationError, match="canonical return formula"):
        validate_h24_frame(bad)
    bad_percent = frame.copy()
    bad_percent.loc[0, "timestral_return_pct_24h"] = 0.02
    with pytest.raises(CanonicalValidationError, match="100 \\* decimal"):
        validate_h24_frame(bad_percent)


def test_validate_rejects_duplicates():
    frame = pd.concat([_canonical_frame(), _canonical_frame()], ignore_index=True)
    with pytest.raises(CanonicalValidationError, match="Duplicate"):
        validate_h24_frame(frame)


def test_validate_requires_finite_prices():
    frame = _canonical_frame()
    frame.loc[0, "actual_price_24h"] = np.nan
    with pytest.raises(CanonicalValidationError, match="Non-finite"):
        validate_h24_frame(frame)
