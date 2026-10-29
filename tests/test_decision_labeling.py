from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab.decision_labeling import build_labels, label_summary
from crypto_decision_lab.decision_contract import (
    ACTION_LONG,
    ACTION_NO_TRADE,
    ACTION_SHORT,
    CostModel,
)


def _canonical(rows: int = 12, *, start: str = "2024-01-01") -> pd.DataFrame:
    cutoffs = pd.date_range(start, periods=rows, freq="5D", tz="UTC")
    return pd.DataFrame(
        {
            "window_index": np.arange(rows),
            "cutoff": cutoffs,
            "anchor_price": np.full(rows, 100.0),
            "actual_price_24h": 100.0 + np.arange(rows),
        }
    )


def _opens(rows: int, *, base: float = 100.0) -> pd.Series:
    index = pd.date_range("2024-01-01", periods=rows * 5 * 24 + 48, freq="1h", tz="UTC")
    return pd.Series(base + np.linspace(0.0, 1.0, len(index)), index=index)


def test_labels_pick_best_action_after_costs():
    canonical = _canonical(rows=3)
    opens = pd.Series(
        [100.0, 90.0, 110.0, 105.0, 95.0, 101.0],
        index=pd.DatetimeIndex(
            [
                "2024-01-01",  # entry window 0
                "2024-01-02",  # exit window 0
                "2024-01-06",  # entry window 1
                "2024-01-07",  # exit window 1
                "2024-01-11",  # entry window 2
                "2024-01-12",  # exit window 2
            ],
            dtype="datetime64[ns]",
        ).tz_localize("UTC"),
    )
    costs = CostModel(fee_rate=0.0, slippage=0.0)
    labels = build_labels(canonical, opens, costs=costs, ambiguity_margin=0.0)
    # Window 0: 100 -> 90, SHORT wins; window 1: 110 -> 105, SHORT wins; window 2: 95 -> 101, LONG wins.
    assert labels.iloc[0]["best_action"] == ACTION_SHORT
    assert labels.iloc[1]["best_action"] == ACTION_SHORT
    assert labels.iloc[2]["best_action"] == ACTION_LONG
    assert labels.iloc[0]["short_net_pct"] == pytest.approx(100.0 / 90.0 * 100.0 - 100.0)
    assert labels.iloc[0]["long_net_pct"] == pytest.approx(-10.0)
    assert labels.iloc[2]["long_net_pct"] == pytest.approx(101.0 / 95.0 * 100.0 - 100.0)


def test_no_trade_label_when_both_directions_lose_after_costs():
    canonical = _canonical(rows=1)
    index = pd.DatetimeIndex(["2024-01-01", "2024-01-02"], dtype="datetime64[ns]").tz_localize("UTC")
    opens = pd.Series([100.0, 100.5], index=index)
    costs = CostModel(fee_rate=0.01, slippage=0.01)
    labels = build_labels(canonical, opens, costs=costs, ambiguity_margin=0.0)
    row = labels.iloc[0]
    # After heavy costs both directions are negative: NO_TRADE is best.
    assert row["long_net_pct"] < 0.0
    assert row["short_net_pct"] < 0.0
    assert row["best_action"] == ACTION_NO_TRADE
    assert row["best_net_pct"] == 0.0


def test_ambiguity_flag_and_margin():
    canonical = _canonical(rows=1)
    index = pd.DatetimeIndex(["2024-01-01", "2024-01-02"], dtype="datetime64[ns]").tz_localize("UTC")
    opens = pd.Series([100.0, 100.1], index=index)  # tiny gross move
    costs = CostModel(fee_rate=0.0, slippage=0.0)
    labels = build_labels(canonical, opens, costs=costs, ambiguity_margin=0.005)
    row = labels.iloc[0]
    # LONG = +0.1%, SHORT = -0.1%, NO_TRADE = 0 -> best LONG, runner-up NO_TRADE.
    assert row["best_action"] == ACTION_LONG
    assert row["margin_vs_second_pct"] == pytest.approx(0.1)
    assert bool(row["ambiguous"]) is True  # 0.1% < 0.5% margin threshold


def test_costs_reduce_returns_monotonically():
    canonical = _canonical(rows=2)
    opens = _opens(rows=2)
    free = build_labels(canonical, opens, costs=CostModel(fee_rate=0.0, slippage=0.0))["long_net_pct"].to_numpy()
    costly = build_labels(canonical, opens, costs=CostModel(fee_rate=0.01, slippage=0.01))["long_net_pct"].to_numpy()
    assert (costly < free).all()


def test_label_summary_counts_and_warning():
    canonical = _canonical(rows=6)
    labels = build_labels(canonical, _opens(rows=6), costs=CostModel(fee_rate=0.001, slippage=0.0005))
    summary = label_summary(labels)
    assert summary["windows"] == 6
    assert sum(summary["best_action_counts"].values()) == 6
    assert "never use best_action" in summary["warning"]
    assert len(summary["by_year"]) >= 1
