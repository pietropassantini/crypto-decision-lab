from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab.canonical import HORIZON_HOURS, TARGET_OFFSET, h24_record
from crypto_decision_lab.regime_analysis import (
    RegimeError,
    assign_regimes,
    assign_trend_regime,
    assign_vol_regime,
    build_errors,
    compute_features,
    fit_tercile_thresholds,
    moving_block_bootstrap_ci,
    reconcile_with_benchmark,
    regime_statistics,
)


def _canonical_frame(rows: int = 6, start: str = "2024-01-01") -> pd.DataFrame:
    cutoffs = pd.date_range(start, periods=rows, freq="5D", tz="UTC")
    records = []
    generator = np.random.default_rng(7)
    for index, cutoff in enumerate(cutoffs):
        anchor = 100.0 + index
        actual = anchor * (1.0 + 0.02 * generator.normal())
        p50 = anchor * (1.0 + 0.01 * generator.normal())
        p10, p90 = p50 * 0.98, p50 * 1.02
        records.append(
            h24_record(
                window_index=index,
                cutoff=cutoff,
                anchor_price=anchor,
                actual_price=actual,
                timestral_p10=p10,
                timestral_p50=p50,
                timestral_p90=p90,
                naive_price=anchor,
                source="test",
                model_version="test@rev",
                forecast_sha256="x",
                snapshot_id="snap",
            )
        )
    return pd.DataFrame(records)


def _market_frame(bars: int = 400, start: str = "2023-01-01") -> pd.DataFrame:
    index = pd.date_range(start, periods=bars, freq="1h", tz="UTC", name="timestamp")
    steps = np.arange(bars, dtype=float)
    close = 100.0 + 0.05 * steps + 2.0 * np.sin(steps / 9.0)
    frame = pd.DataFrame(
        {
            "open": np.concatenate([[close[0]], close[:-1]]),
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1000.0 + 5.0 * steps,
        },
        index=index,
    )
    return frame


def test_build_errors_hand_computed_values():
    canonical = pd.DataFrame(
        [
            h24_record(
                window_index=1,
                cutoff=pd.Timestamp("2024-01-01", tz="UTC"),
                anchor_price=100.0,
                actual_price=110.0,
                timestral_p10=95.0,
                timestral_p50=120.0,
                timestral_p90=130.0,
                naive_price=100.0,
                source="t",
                model_version="t",
                forecast_sha256=None,
                snapshot_id=None,
            )
        ]
    )
    errors = build_errors(canonical)
    row = errors.iloc[0]
    assert row["abs_error_timestral"] == pytest.approx(10.0)
    assert row["abs_error_naive"] == pytest.approx(10.0)
    assert row["signed_error_timestral"] == pytest.approx(10.0)
    assert row["signed_error_naive"] == pytest.approx(-10.0)
    assert row["delta_mae"] == pytest.approx(0.0)
    assert row["winner"] == "TIE"
    assert row["predicted_direction"] == 1.0
    assert row["actual_direction"] == 1.0
    assert row["direction_correct"] == 1.0


def test_build_errors_winner_and_direction_labels():
    canonical = pd.DataFrame(
        [
            h24_record(
                window_index=i,
                cutoff=pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(days=5 * i),
                anchor_price=100.0,
                actual_price=actual,
                timestral_p10=90.0,
                timestral_p50=p50,
                timestral_p90=120.0,
                naive_price=100.0,
                source="t",
                model_version="t",
                forecast_sha256=None,
                snapshot_id=None,
            )
            for i, (actual, p50) in enumerate([(105.0, 103.0), (95.0, 89.0), (100.0, 101.0)])
        ]
    )
    errors = build_errors(canonical)
    assert errors.iloc[0]["winner"] == "TIMESTRAL"  # |3| < |5|
    assert errors.iloc[1]["winner"] == "NAIVE"  # |89-95|=6 > |100-95|=5
    assert errors.iloc[1]["predicted_direction"] == -1.0
    assert errors.iloc[1]["actual_direction"] == -1.0
    assert errors.iloc[2]["actual_direction"] == 0.0
    assert np.isnan(errors.iloc[2]["direction_correct"])


def test_reconcile_with_benchmark_passes_and_fails(tmp_path):
    canonical = _canonical_frame()
    errors = build_errors(canonical)
    summary = {
        "windows": len(errors),
        "timestral_vs_naive": {
            "timestral": {
                "mae_usdt": float(errors["abs_error_timestral"].mean()),
                "rmse_usdt": float(np.sqrt((errors["signed_error_timestral"] ** 2).mean())),
                "bias_usdt": float(errors["signed_error_timestral"].mean()),
                "directional_accuracy": float(errors["direction_correct"].mean()),
                "direction_evaluable": int(errors["direction_correct"].notna().sum()),
            },
            "naive": {
                "mae_usdt": float(errors["abs_error_naive"].mean()),
                "rmse_usdt": float(np.sqrt((errors["signed_error_naive"] ** 2).mean())),
                "bias_usdt": float(errors["signed_error_naive"].mean()),
                "directional_accuracy": 0.0,
                "direction_evaluable": 0,
            },
        },
    }
    (tmp_path / "benchmark_summary.json").write_text(json.dumps(summary))
    checks = reconcile_with_benchmark(errors, tmp_path)
    assert all(check["passed"] for check in checks)
    summary["timestral_vs_naive"]["timestral"]["mae_usdt"] += 1.0
    (tmp_path / "benchmark_summary.json").write_text(json.dumps(summary))
    failing = [check for check in reconcile_with_benchmark(errors, tmp_path) if not check["passed"]]
    assert any("timestral MAE" in check["check"] for check in failing)


def test_compute_features_are_causal_against_future_perturbation():
    market = _market_frame(400)
    cutoff = market.index[250] + pd.Timedelta(hours=1)  # feature bar = market.index[250]
    windows = pd.DataFrame({"window_index": [1], "cutoff": [cutoff]})
    baseline = compute_features(market, windows)
    perturbed = market.copy()
    future = perturbed.index >= cutoff
    perturbed.loc[future, "close"] *= 7.0
    perturbed.loc[future, "open"] *= 7.0
    perturbed.loc[future, "high"] *= 7.0
    perturbed.loc[future, "low"] *= 7.0
    perturbed.loc[future, "volume"] *= 7.0
    after = compute_features(perturbed, windows)
    pd.testing.assert_frame_equal(baseline, after)


def test_compute_features_values_and_history_guard():
    market = _market_frame(400)
    cutoff = market.index[250] + pd.Timedelta(hours=1)
    windows = pd.DataFrame({"window_index": [1], "cutoff": [cutoff]})
    features = compute_features(market, windows).iloc[0]
    history = market["close"].iloc[: 250 + 1]
    assert features["feature_bar"] == market.index[250]
    assert features["close_at_cutoff_bar"] == pytest.approx(float(history.iloc[-1]))
    assert features["sma20"] == pytest.approx(float(history.iloc[-20:].mean()))
    assert features["sma50"] == pytest.approx(float(history.iloc[-50:].mean()))
    assert features["momentum_24h"] == pytest.approx(
        float(history.iloc[-1] / history.iloc[-25] - 1.0)
    )
    assert features["momentum_168h"] == pytest.approx(
        float(history.iloc[-1] / history.iloc[-168] - 1.0)
    )
    expected_vol = float(
        np.std(np.diff(np.log(history.iloc[-24:].to_numpy(dtype=float))), ddof=1)
    )
    assert features["volatility_24h"] == pytest.approx(expected_vol)
    with pytest.raises(RegimeError, match="Insufficient history"):
        compute_features(market, pd.DataFrame({"window_index": [2], "cutoff": [market.index[100]]}))


def test_thresholds_are_train_only_and_regimes_use_them():
    train = pd.Series(np.linspace(-0.10, 0.10, 300))
    thresholds = fit_tercile_thresholds(train.to_numpy())
    changed = train.copy()
    changed.iloc[-50:] = 5.0  # perturb rows that must not influence training thresholds
    assert fit_tercile_thresholds(changed.to_numpy()) == thresholds
    labelled = assign_trend_regime(train, thresholds)
    assert set(labelled.unique()) <= {"TREND_DOWN", "SIDEWAYS", "TREND_UP"}
    assert (labelled == "TREND_DOWN").sum() == 100
    assert (labelled == "TREND_UP").sum() == 100
    vol = pd.Series(np.linspace(0.001, 0.02, 300))
    vol_labels = assign_vol_regime(vol, fit_tercile_thresholds(vol.to_numpy()))
    assert set(vol_labels.unique()) <= {"VOL_LOW", "VOL_MEDIUM", "VOL_HIGH"}


def test_assign_regimes_keeps_explicit_thresholds():
    frame = pd.DataFrame({"momentum_168h": [-0.05, 0.0, 0.05], "volatility_168h": [0.001, 0.005, 0.02]})
    regimes = assign_regimes(frame, trend_thresholds=(-0.02, 0.02), vol_thresholds=(0.003, 0.01))
    assert regimes["trend_regime"].tolist() == ["TREND_DOWN", "SIDEWAYS", "TREND_UP"]
    assert regimes["vol_regime"].tolist() == ["VOL_LOW", "VOL_MEDIUM", "VOL_HIGH"]
    assert regimes["combined_regime"].tolist() == [
        "TREND_DOWN/VOL_LOW",
        "SIDEWAYS/VOL_MEDIUM",
        "TREND_UP/VOL_HIGH",
    ]


def test_block_bootstrap_ci_is_deterministic_and_covers_point():
    values = np.concatenate([np.linspace(-50, 50, 120), np.full(30, 10.0)])
    first = moving_block_bootstrap_ci(values)
    second = moving_block_bootstrap_ci(values)
    assert first == second
    assert first["point"] == pytest.approx(float(values.mean()))
    assert first["ci_low"] <= first["point"] <= first["ci_high"]
    assert first["draws"] == 1000


def test_regime_statistics_flags_small_groups():
    frame = pd.DataFrame(
        {
            "regime": ["A"] * 40 + ["B"] * 5,
            "abs_error_timestral": np.full(45, 10.0),
            "abs_error_naive": np.full(45, 12.0),
            "signed_error_timestral": np.full(45, 1.0),
            "signed_error_naive": np.full(45, -1.0),
            "delta_mae": np.full(45, -2.0),
            "winner": ["TIMESTRAL"] * 45,
        }
    )
    stats = regime_statistics(frame, "regime", min_obs=30)
    by_regime = stats.set_index("regime")
    assert by_regime.loc["A", "reliable"] is True or by_regime.loc["A", "reliable"] == 1.0
    assert by_regime.loc["B", "reliable"] is False or by_regime.loc["B", "reliable"] == 0.0
    assert by_regime.loc["A", "delta_mae_usdt"] == pytest.approx(-2.0)
    assert by_regime.loc["A", "timestral_win_rate"] == pytest.approx(1.0)
    assert by_regime.loc["A", "delta_mae_ci_low"] <= -2.0 <= by_regime.loc["A", "delta_mae_ci_high"]
