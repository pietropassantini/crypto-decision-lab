from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab.dashboard.indicators import (
    IndicatorRequest,
    IndicatorSpec,
    compute_indicator,
    compute_indicators,
    indicator_specs,
    register_indicator,
)


def _close_frame(values) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=len(values), freq="1h", tz="UTC")
    close = pd.Series(values, index=index, dtype=float)
    return pd.DataFrame(
        {
            "open": close.shift(1).fillna(close.iloc[0]),
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": np.linspace(10, 20, len(values)),
        },
        index=index,
    )


def test_sma_and_ema_known_values():
    frame = _close_frame([1, 2, 3, 4, 5])
    sma = compute_indicator(frame, "sma", {"window": 3})["sma"]
    assert np.isnan(sma.iloc[0]) and np.isnan(sma.iloc[1])
    assert sma.iloc[2] == pytest.approx(2.0)
    assert sma.iloc[4] == pytest.approx(4.0)
    ema = compute_indicator(frame, "ema", {"window": 3})["ema"]
    assert np.isnan(ema.iloc[0]) and np.isnan(ema.iloc[1])
    assert ema.iloc[2] == pytest.approx(2.25)
    assert ema.iloc[4] == pytest.approx(4.0625)


def test_rsi_and_macd_are_causal_and_bounded():
    rising = _close_frame(np.arange(1, 60, dtype=float))
    rsi_up = compute_indicator(rising, "rsi", {"period": 14})["rsi"]
    assert rsi_up.iloc[-1] == pytest.approx(100.0)
    falling = _close_frame(np.arange(60, 1, -1, dtype=float))
    rsi_down = compute_indicator(falling, "rsi", {"period": 14})["rsi"]
    assert rsi_down.iloc[-1] == pytest.approx(0.0, abs=1e-8)

    macd = compute_indicator(rising, "macd", {"fast": 12, "slow": 26, "signal": 9})
    assert set(macd.columns) == {"macd", "macd_signal", "macd_hist"}
    assert np.allclose(macd["macd_hist"], macd["macd"] - macd["macd_signal"], equal_nan=True)
    assert macd["macd"].iloc[-1] > 0


def test_bollinger_atr_adx_stochastic_and_volatility():
    frame = _close_frame(100 + 5 * np.sin(np.arange(150) / 5.0))
    bands = compute_indicator(frame, "bollinger", {"window": 20, "num_std": 2.0})
    valid = bands.dropna()
    assert (valid["bb_upper"] > valid["bb_mid"]).all()
    assert (valid["bb_mid"] > valid["bb_lower"]).all()
    atr = compute_indicator(frame, "atr", {"period": 14})["atr"].dropna()
    assert (atr >= 0).all()
    adx = compute_indicator(frame, "adx", {"period": 14}).dropna()
    assert adx["adx"].between(0, 100).all()
    stochastic = compute_indicator(frame, "stochastic", {}).dropna()
    assert stochastic["stoch_k"].between(0, 100).all()
    assert stochastic["stoch_d"].between(0, 100).all()
    raw = compute_indicator(
        frame, "rolling_volatility", {"window": 24, "annualize": False, "periods_per_year": 0}
    )["rolling_volatility"].dropna()
    annual = compute_indicator(
        frame, "rolling_volatility", {"window": 24, "annualize": True, "periods_per_year": 0}
    )["rolling_volatility"].dropna()
    assert (annual > raw).all()


def test_no_lookahead_for_every_registered_indicator(synthetic_market):
    cutoff = 150
    original = synthetic_market.iloc[:220].copy()
    perturbed = original.copy()
    future = perturbed.index > original.index[cutoff]
    perturbed.loc[future, "close"] *= 5.0
    perturbed.loc[future, "open"] *= 5.0
    perturbed.loc[future, "high"] *= 5.0
    perturbed.loc[future, "low"] *= 5.0
    perturbed.loc[future, "volume"] *= 5.0
    for key, spec in indicator_specs().items():
        before = compute_indicator(original, key, spec.default_params).iloc[: cutoff + 1]
        after = compute_indicator(perturbed, key, spec.default_params).iloc[: cutoff + 1]
        np.testing.assert_allclose(
            before.to_numpy(dtype=float),
            after.to_numpy(dtype=float),
            equal_nan=True,
            rtol=1e-12,
            atol=1e-12,
            err_msg=f"look-ahead detected for {key}",
        )


def test_indicator_registry_extension():
    def double_close(frame: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame({"double_close": frame["close"] * 2.0}, index=frame.index)

    register_indicator(
        IndicatorSpec("test_double_close", "Double Close", "overlay", {}, ("double_close",)),
        double_close,
    )
    frame = _close_frame([1, 2, 3])
    result = compute_indicator(frame, "test_double_close", {})
    assert result["double_close"].tolist() == [2.0, 4.0, 6.0]


def test_compute_indicators_preserves_duplicate_periods(synthetic_market):
    frame = synthetic_market.iloc[:100]
    results = compute_indicators(
        frame,
        [
            IndicatorRequest("sma", {"window": 7}),
            IndicatorRequest("sma", {"window": 20}),
            IndicatorRequest("ema", {"window": 9}),
        ],
    )
    assert [result.request.params["window"] for result in results] == [7, 20, 9]
    assert all(len(result.data) == len(frame) for result in results)
