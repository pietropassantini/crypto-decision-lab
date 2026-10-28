from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab.forecast_consistency_audit import (
    compare_metrics,
    misinterpreted_timestral_price,
    residual_corrected_price,
    residual_corrected_return,
    timestral_price_from_return,
)


def test_decimal_timestral_return_is_not_scaled_twice():
    anchor = 100.0
    decimal_return = 0.10
    assert timestral_price_from_return(anchor, decimal_return) == pytest.approx(110.0)
    assert misinterpreted_timestral_price(anchor, decimal_return) == pytest.approx(100.1)


def test_residual_percent_is_converted_once():
    anchor = 100.0
    timestral_return = 0.10
    residual_pct = 2.0
    corrected_return = residual_corrected_return(timestral_return, residual_pct)
    assert corrected_return == pytest.approx(0.12)
    assert residual_corrected_price(anchor, timestral_return, residual_pct) == pytest.approx(112.0)
    old_mixed = anchor * (1.0 + (timestral_return + residual_pct) / 100.0)
    assert old_mixed == pytest.approx(102.1)
    assert corrected_return != pytest.approx(old_mixed / anchor - 1.0)


def _synthetic_audit() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "fold": [0, 0],
            "window_index": [1, 2],
            "actual_price_24h": [105.0, 95.0],
            "timestral_price_original_cp05": [100.2, 99.8],
            "naive_price_24h": [100.0, 100.0],
            "cp05_timestral_return_field": [0.20, -0.20],
            "ridge_price_original_cp05": [100.2, 99.8],
            "ridge_return_field_original": [0.20, -0.20],
            "histgb_price_original_cp05": [100.2, 99.8],
            "histgb_return_field_original": [0.20, -0.20],
            "targets_actual_return_pct_24h": [5.0, -5.0],
            "timestral_price_canonical": [120.0, 80.0],
            "timestral_return_decimal": [0.20, -0.20],
            "timestral_return_pct_canonical": [20.0, -20.0],
            "ridge_return_decimal_canonical": [0.20, -0.20],
            "ridge_price_canonical": [120.0, 80.0],
            "histgb_return_decimal_canonical": [0.20, -0.20],
            "histgb_price_canonical": [120.0, 80.0],
        }
    )


def test_metrics_ranking_changes_after_correct_reconstruction():
    result = compare_metrics(_synthetic_audit())
    assert result["ranking_original_mae"][0] == "timestral"
    assert result["ranking_corrected_mae"][0] == "naive"
    assert result["ranking_changed"] is True
    impact = result["impact"]["timestral"]
    assert impact["mae_original_usdt"] == pytest.approx(4.8)
    assert impact["mae_corrected_usdt"] == pytest.approx(15.0)
    assert impact["mae_delta_usdt"] == pytest.approx(10.2)


def test_naive_direction_is_not_reported_as_zero_accuracy():
    result = compare_metrics(_synthetic_audit())
    naive = result["corrected"]["metrics"]["naive"]
    assert naive["direction_accuracy_evaluable"] is None
    assert naive["direction_evaluable"] == 0
    # The legacy sign(0) convention is retained for backwards comparison.
    assert naive["direction_accuracy_24h"] == pytest.approx(0.0)


def test_metrics_are_finite_and_sample_count_is_preserved():
    result = compare_metrics(_synthetic_audit())
    for section in ("original", "corrected"):
        payload = result[section]
        assert payload["evaluation_rows"] == 2
        for model in ("timestral", "naive", "ridge", "histgb"):
            metrics = payload["metrics"][model]
            assert np.isfinite(metrics["mae_price_24h"])
            assert np.isfinite(metrics["rmse_price_24h"])
