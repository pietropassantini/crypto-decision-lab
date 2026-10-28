from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab.dashboard.forecast_adapter import (
    ALIGNMENT_KEYS,
    align_models,
    common_targets,
    validate_forecast_actuals,
)
from crypto_decision_lab.dashboard.metrics import (
    add_error_columns,
    assign_regimes,
    compute_metrics,
    metrics_table,
    regime_metrics,
)


def test_forecast_targets_are_aligned_to_horizon_not_generation(archive):
    points = archive.forecasts.points
    timestral = points[points["model"] == "timestral"]
    for window_index, expected in archive.paths["expected"].items():
        rows = timestral[timestral["window_index"] == window_index].sort_values("horizon_hours")
        assert rows["cutoff"].nunique() == 1
        assert rows["cutoff"].iloc[0] == expected["cutoff"]
        assert rows["target"].iloc[0] == expected["cutoff"]
        assert rows["target"].iloc[-1] == expected["target_24"]
        assert rows["target"].iloc[-1] == expected["cutoff"] + pd.Timedelta(hours=23)
        assert rows["horizon_hours"].tolist() == list(range(1, 25))
    ridge = points[points["model"] == "ridge"]
    expected = archive.paths["expected"][0]
    assert ridge.iloc[0]["target"] == expected["target_24"]
    assert ridge.iloc[0]["horizon_hours"] == 24


def test_common_timestamp_alignment_uses_all_models(archive):
    points = archive.forecasts.points
    models = ["timestral", "naive", "ridge", "histgb"]
    targets = common_targets(points, models, horizon=24)
    assert len(targets) == 2
    aligned = align_models(points, models, horizon=24)
    assert len(aligned.drop_duplicates(ALIGNMENT_KEYS)) == 2
    for _, group in aligned.groupby(ALIGNMENT_KEYS):
        assert set(group["model"]) == set(models)
        assert group["target"].nunique() == 1


def test_missing_model_target_is_not_invented(archive):
    points = archive.forecasts.points.copy()
    drop_mask = (
        (points["model"] == "ridge")
        & (points["window_index"] == 0)
        & (points["horizon_hours"] == 24)
    )
    points = points[~drop_mask]
    aligned = align_models(points, ["timestral", "ridge"], horizon=24)
    assert len(aligned.drop_duplicates(ALIGNMENT_KEYS)) == 1
    assert aligned["window_index"].unique().tolist() == [1]


def test_actuals_cross_check_against_market(archive):
    issues = validate_forecast_actuals(archive.forecasts.points, archive.market)
    assert issues == []


def test_metrics_known_values_and_improvement():
    target = pd.Timestamp("2024-01-01 23:00", tz="UTC")
    cutoff = pd.Timestamp("2024-01-01 00:00", tz="UTC")
    base = {
        "model": "m",
        "model_label": "Model",
        "window_index": 0,
        "cutoff": cutoff,
        "target": target,
        "horizon_hours": 24,
        "anchor_price": 100.0,
        "actual": 110.0,
    }
    points = pd.DataFrame(
        [
            {**base, "model": "timestral", "model_label": "Timestral", "predicted": 120.0},
            {**base, "model": "naive", "model_label": "Naive", "predicted": 100.0},
        ]
    )
    table = metrics_table(points, ["timestral", "naive"], horizon=24)
    timestral = table[table["model"] == "timestral"].iloc[0]
    assert timestral["samples"] == 1
    assert timestral["mae"] == pytest.approx(10.0)
    assert timestral["rmse"] == pytest.approx(10.0)
    assert timestral["bias"] == pytest.approx(10.0)
    assert timestral["mape"] == pytest.approx(10.0 / 110.0 * 100.0)
    assert timestral["directional_accuracy"] == pytest.approx(1.0)
    assert timestral["improvement_vs_naive_pct"] == pytest.approx(0.0)
    naive = table[table["model"] == "naive"].iloc[0]
    assert np.isnan(naive["directional_accuracy"])
    assert naive["direction_evaluable"] == 0


def test_metrics_direction_and_empty():
    frame = pd.DataFrame(
        {
            "predicted": [90.0, 120.0],
            "actual": [95.0, 110.0],
            "anchor_price": [100.0, 100.0],
        }
    )
    enriched = add_error_columns(frame)
    assert enriched["predicted_direction"].tolist() == [-1.0, 1.0]
    assert enriched["actual_direction"].tolist() == [-1.0, 1.0]
    assert enriched["direction_correct"].tolist() == [1.0, 1.0]
    metrics = compute_metrics(frame)
    assert metrics["samples"] == 2
    assert metrics["bias"] == pytest.approx(2.5)
    assert metrics["directional_accuracy"] == pytest.approx(1.0)
    empty = compute_metrics(frame.iloc[0:0])
    assert empty["samples"] == 0 and np.isnan(empty["mae"])


def test_regimes_use_only_past_closes(archive):
    points = archive.forecasts.points[
        (archive.forecasts.points["model"] == "timestral")
        & (archive.forecasts.points["horizon_hours"] == 24)
        & (archive.forecasts.points["window_index"] == 0)
    ]
    original = assign_regimes(
        points,
        archive.market,
        vol_bins=(0.005, 0.01),
        trend_bins=(-0.02, 0.02),
    )
    future = archive.market.copy()
    cutoff = archive.paths["expected"][0]["cutoff"]
    after = future.index >= cutoff
    future.loc[after, "close"] *= 10.0
    changed = assign_regimes(
        points,
        future,
        vol_bins=(0.005, 0.01),
        trend_bins=(-0.02, 0.02),
    )
    cols = ["cutoff", "volatility_168h", "trend_168h", "volatility_regime", "trend_regime"]
    pd.testing.assert_frame_equal(
        original[cols].reset_index(drop=True),
        changed[cols].reset_index(drop=True),
        check_dtype=False,
    )
    table = regime_metrics(original, "trend_regime", ["timestral"])
    assert not table.empty
    assert set(table.columns) >= {"trend_regime", "model", "samples", "mae", "rmse", "bias"}


def test_missing_forecast_archive_degrades_to_empty(tmp_path):
    from crypto_decision_lab.dashboard.forecast_adapter import load_forecasts

    dataset = load_forecasts(tmp_path / "missing-batch", None, None)
    assert dataset.empty
    assert dataset.available_models() == []
    assert any("not found" in issue.lower() for issue in dataset.issues)
