from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab.decision_dataset import (
    FEATURE_COLUMNS,
    P10_P90_SIGMAS,
    REGIME_FEATURES,
    TARGET_COLUMNS,
    DecisionDatasetError,
    build_features,
    build_targets,
)

CUTOFFS = pd.date_range("2024-01-08", periods=3, freq="2D", tz="UTC")
ANCHOR = 100.0
VOL = 0.01  # hourly; 24h sigma = 0.01 * sqrt(24) ~ 4.9%


def _regime() -> pd.DataFrame:
    frame = pd.DataFrame({"window_index": [0, 1, 2], "cutoff": CUTOFFS, "close_at_cutoff_bar": ANCHOR})
    for name in REGIME_FEATURES:
        frame[name] = 0.5
    frame["volatility_168h"] = VOL
    return frame


def _path(cutoff: pd.Timestamp, *, drift: float = 0.001, half_width: float = 2.0) -> pd.DataFrame:
    steps = np.arange(1, 25)
    p50 = ANCHOR * (1.0 + drift * steps)
    spread = half_width * steps / 24.0
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(cutoff, periods=24, freq="1h", tz="UTC"),
            "timestral_p10": p50 - spread,
            "timestral_p50": p50,
            "timestral_p90": p50 + spread,
        }
    )


def _forecasts() -> dict[int, pd.DataFrame]:
    return {i: _path(c) for i, c in enumerate(CUTOFFS)}


def _market(closes_by_window: dict[int, np.ndarray] | None = None) -> pd.DataFrame:
    index = pd.date_range(CUTOFFS[0], CUTOFFS[-1] + pd.Timedelta(hours=30), freq="1h", tz="UTC")
    frame = pd.DataFrame({"open": ANCHOR, "high": ANCHOR, "low": ANCHOR, "close": ANCHOR, "volume": 1.0}, index=index)
    for window, (high, low) in (closes_by_window or {}).items():
        bars = pd.date_range(CUTOFFS[window], periods=24, freq="1h", tz="UTC")
        frame.loc[bars, "high"] = high
        frame.loc[bars, "low"] = low
    return frame


def _labels(gross: tuple[float, float, float] = (3.0, -3.0, 0.1)) -> pd.DataFrame:
    gross_array = np.array(gross)
    return pd.DataFrame(
        {
            "window_index": [0, 1, 2],
            "cutoff": CUTOFFS,
            "entry_open": ANCHOR,
            "exit_open": ANCHOR * (1.0 + gross_array / 100.0),
            "long_gross_pct": gross_array,
            "long_net_pct": gross_array - 0.3,
            "short_net_pct": -gross_array - 0.3,
            "best_action": ["LONG", "SHORT", "NO_TRADE"],
            "fee_rate": 0.001,
            "slippage": 0.0005,
        }
    )


def test_features_formulas_and_columns():
    features = build_features(_regime(), _forecasts())
    assert list(features.columns) == ["window_index", "cutoff", *FEATURE_COLUMNS]
    row = features.iloc[0]
    assert row["ts_return_pct_24h"] == pytest.approx(2.4)
    assert row["ts_return_pct_4h"] == pytest.approx(0.4)
    assert row["ts_width_pct_24h"] == pytest.approx(4.0)
    assert row["ts_width_growth"] == pytest.approx(6.0)
    assert row["ts_skew_24h"] == pytest.approx(0.0)
    assert row["ts_return_z_24h"] == pytest.approx(0.024 / (0.04 / P10_P90_SIGMAS))
    assert row["ts_width_vs_realized"] == pytest.approx((0.04 / P10_P90_SIGMAS) / (VOL * np.sqrt(24)))
    assert row["ts_path_monotonicity"] == pytest.approx(1.0)
    assert row["ts_path_max_pct"] == pytest.approx(2.4)
    # 2024-01-08 is a Monday.
    assert row["dow_sin"] == pytest.approx(0.0)
    assert row["dow_cos"] == pytest.approx(1.0)


def test_features_exclude_price_levels():
    assert not {"sma20", "sma50", "ema20", "ema50", "atr14", "close_at_cutoff_bar"} & set(FEATURE_COLUMNS)
    assert not set(FEATURE_COLUMNS) & set(TARGET_COLUMNS)


def test_features_reject_realized_prices():
    forecasts = _forecasts()
    forecasts[1] = forecasts[1].assign(actual=101.0)
    with pytest.raises(DecisionDatasetError, match="realized prices"):
        build_features(_regime(), forecasts)


def test_features_reject_misaligned_forecast_start():
    forecasts = _forecasts()
    forecasts[2] = _path(CUTOFFS[2] + pd.Timedelta(hours=1))
    with pytest.raises(DecisionDatasetError, match="not the cutoff"):
        build_features(_regime(), forecasts)


def test_features_reject_window_set_mismatch():
    forecasts = _forecasts()
    del forecasts[0]
    with pytest.raises(DecisionDatasetError, match="window sets differ"):
        build_features(_regime(), forecasts)


def test_targets_triple_barrier_and_scaling():
    barrier = VOL * np.sqrt(24)  # ~4.9%
    up = (np.full(24, ANCHOR), np.full(24, ANCHOR))
    up[0][5] = ANCHOR * (1.0 + barrier) + 0.01  # upper touched at hour 6
    down_then_up = (np.full(24, ANCHOR), np.full(24, ANCHOR))
    down_then_up[1][2] = ANCHOR * (1.0 - barrier) - 0.01  # lower first (hour 3)
    down_then_up[0][10] = ANCHOR * (1.0 + barrier) + 0.01
    market = _market({0: up, 1: down_then_up})
    vol = pd.Series(VOL, index=[0, 1, 2])
    targets = build_targets(_labels(), market, vol)
    assert list(targets.columns) == ["window_index", "cutoff", *TARGET_COLUMNS]
    assert targets["triple_barrier"].tolist() == [1, -1, 0]
    assert targets["triple_barrier_hit_hour"].tolist()[:2] == [6, 3]
    assert pd.isna(targets["triple_barrier_hit_hour"].iloc[2])
    assert targets["barrier_pct"].iloc[0] == pytest.approx(100.0 * barrier)
    assert targets["vol_scaled_return_24h"].iloc[0] == pytest.approx(0.03 / barrier)
    assert targets["trade_worth"].tolist() == [True, True, False]
    assert targets["sample_weight"].tolist() == pytest.approx([2.7, 2.7, 0.0])
    assert targets["max_favorable_excursion_pct"].iloc[0] == pytest.approx(100.0 * barrier + 0.01)


def test_targets_same_bar_touch_is_neutral():
    barrier = VOL * np.sqrt(24)
    both = (np.full(24, ANCHOR), np.full(24, ANCHOR))
    both[0][4] = ANCHOR * (1.0 + barrier) + 1.0
    both[1][4] = ANCHOR * (1.0 - barrier) - 1.0
    targets = build_targets(_labels(), _market({0: both}), pd.Series(VOL, index=[0, 1, 2]))
    assert targets["triple_barrier"].iloc[0] == 0
    assert bool(targets["triple_barrier_same_bar"].iloc[0]) is True


def test_targets_reject_incomplete_holding_period():
    market = _market().drop(index=CUTOFFS[1] + pd.Timedelta(hours=7))
    with pytest.raises(DecisionDatasetError, match="contiguous"):
        build_targets(_labels(), market, pd.Series(VOL, index=[0, 1, 2]))


def test_targets_reject_entry_mismatch():
    market = _market()
    market.loc[CUTOFFS[0], "open"] = ANCHOR * 1.01
    with pytest.raises(DecisionDatasetError, match="Entry open"):
        build_targets(_labels(), market, pd.Series(VOL, index=[0, 1, 2]))
