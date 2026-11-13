"""S02 tests: shared execution contract (cutoff, latency, horizon, costs).

CP02.3 evidence: a fixture with known prices and times reconstructs the
target with the same economic outcome as the S03 engine primitive, including
latency and fees; at zero latency the outcome is bit-identical to the legacy
CP07 ``decision_contract`` formulas (reconciliation required by the PRD).
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab.decision_contract import (
    ACTION_LONG,
    CostModel,
    execution_prices,
    gross_return,
    net_return,
)
from crypto_decision_lab.execution_contract import (
    ExecutionContractError,
    ExecutionHypothesis,
    RoundTrip,
    resolve_round_trip,
)

FEE = 0.001
SLIP = 0.0005
COSTS = CostModel(fee_rate=FEE, slippage=SLIP)


def make_market(start="2024-01-01", hours=200, base=100.0, drift=0.001) -> pd.DataFrame:
    """Deterministic hourly OHLCV fixture with known prices at every bar."""

    index = pd.date_range(start, periods=hours, freq="1h", tz="UTC")
    open_ = base * (1.0 + drift) ** np.arange(hours, dtype=float)
    frame = pd.DataFrame(
        {
            "open": open_,
            "high": open_ * 1.01,
            "low": open_ * 0.99,
            "close": open_ * 1.005,
            "volume": np.full(hours, 12.0),
        },
        index=index,
    )
    return frame


class TestHypothesisValidation:
    def test_rejects_negative_latency(self):
        with pytest.raises(ExecutionContractError):
            ExecutionHypothesis(latency_hours=-1)

    def test_rejects_zero_horizon(self):
        with pytest.raises(ExecutionContractError):
            ExecutionHypothesis(horizon_hours=0)

    def test_rejects_out_of_range_costs(self):
        with pytest.raises(ExecutionContractError):
            ExecutionHypothesis(fee_rate=0.5)

    def test_declaration_declares_all_parameters(self):
        declaration = ExecutionHypothesis(latency_hours=2, horizon_hours=12).declaration()
        assert declaration["latency_hours"] == 2
        assert declaration["horizon_hours"] == 12
        assert declaration["fee_rate_per_side"] == FEE
        assert declaration["slippage_per_side"] == SLIP
        assert "cutoff" in declaration["latency"]


class TestTimestamps:
    def test_zero_latency_matches_legacy_cp07_layout(self):
        hypothesis = ExecutionHypothesis()
        cutoff = pd.Timestamp("2024-01-08T00:00:00Z")
        assert hypothesis.entry_timestamp(cutoff) == cutoff
        assert hypothesis.exit_timestamp(cutoff) == cutoff + pd.Timedelta(hours=24)

    def test_latency_shifts_entry_and_exit(self):
        hypothesis = ExecutionHypothesis(latency_hours=2, horizon_hours=24)
        cutoff = pd.Timestamp("2024-01-08T00:00:00Z")
        assert hypothesis.entry_timestamp(cutoff) == cutoff + pd.Timedelta(hours=2)
        assert hypothesis.exit_timestamp(cutoff) == cutoff + pd.Timedelta(hours=26)
        span = hypothesis.required_span(cutoff)
        assert span[0] == cutoff + pd.Timedelta(hours=2)
        assert span[-1] == cutoff + pd.Timedelta(hours=26)
        assert len(span) == 25


class TestRoundTrip:
    def test_zero_latency_is_bit_identical_to_legacy_cp07(self):
        market = make_market()
        cutoff = market.index[100]
        trip = resolve_round_trip(market, cutoff, ExecutionHypothesis())
        entry_open = float(market.loc[cutoff, "open"])
        exit_open = float(market.loc[cutoff + pd.Timedelta(hours=24), "open"])
        entry_exec, exit_exec = execution_prices(entry_open, exit_open, ACTION_LONG, COSTS)
        assert trip.entry_open == entry_open
        assert trip.exit_open == exit_open
        assert trip.entry_exec_price == entry_exec
        assert trip.exit_exec_price == exit_exec
        assert trip.gross_return_pct == 100.0 * gross_return(entry_open, exit_open, ACTION_LONG)
        assert trip.net_return_pct == 100.0 * net_return(entry_open, exit_open, ACTION_LONG, COSTS)

    def test_known_fixture_reconstructs_outcome_with_latency_and_fees(self):
        market = make_market(base=100.0, drift=0.0)
        # Constant prices: open == 100 everywhere. With latency 1 and any
        # horizon, entry and exit opens are both 100.
        hypothesis = ExecutionHypothesis(latency_hours=1, horizon_hours=24, fee_rate=FEE, slippage=SLIP)
        cutoff = market.index[50]
        trip = resolve_round_trip(market, cutoff, hypothesis)
        entry_exec = 100.0 * (1.0 + SLIP)
        exit_exec = 100.0 * (1.0 - SLIP)
        net = (exit_exec * (1.0 - FEE)) / (entry_exec * (1.0 + FEE)) - 1.0
        assert trip.entry_timestamp == cutoff + pd.Timedelta(hours=1)
        assert trip.exit_timestamp == cutoff + pd.Timedelta(hours=25)
        assert trip.entry_exec_price == pytest.approx(entry_exec)
        assert trip.exit_exec_price == pytest.approx(exit_exec)
        assert trip.gross_return_pct == pytest.approx(0.0)
        assert trip.net_return_pct == pytest.approx(100.0 * net)
        # The round trip loses the full cost when prices do not move.
        assert trip.round_trip_cost_pct == pytest.approx(-100.0 * net)
        assert trip.round_trip_cost_pct == pytest.approx(trip.gross_return_pct - trip.net_return_pct)

    def test_latency_changes_the_outcome_when_prices_move(self):
        market = make_market(drift=0.01)
        cutoff = market.index[50]
        fast = resolve_round_trip(market, cutoff, ExecutionHypothesis(latency_hours=0))
        slow = resolve_round_trip(market, cutoff, ExecutionHypothesis(latency_hours=6))
        assert fast.entry_open != slow.entry_open
        assert slow.entry_open == float(market.loc[cutoff + pd.Timedelta(hours=6), "open"])

    def test_missing_entry_bar_is_rejected(self):
        market = make_market()
        hypothesis = ExecutionHypothesis(latency_hours=3)
        cutoff = market.index[10]  # entry bar 3h later exists, but remove it
        reduced = market.drop(index=[cutoff + pd.Timedelta(hours=3)])
        with pytest.raises(ExecutionContractError, match="entry bar missing"):
            resolve_round_trip(reduced, cutoff, hypothesis)

    def test_missing_exit_bar_is_rejected(self):
        market = make_market()
        cutoff = market.index[10]
        reduced = market.drop(index=[cutoff + pd.Timedelta(hours=24)])
        with pytest.raises(ExecutionContractError, match="exit bar missing"):
            resolve_round_trip(reduced, cutoff, ExecutionHypothesis())

    def test_gap_inside_holding_period_is_rejected(self):
        market = make_market()
        cutoff = market.index[10]
        reduced = market.drop(index=[cutoff + pd.Timedelta(hours=5), cutoff + pd.Timedelta(hours=18)])
        with pytest.raises(ExecutionContractError, match="contiguous"):
            resolve_round_trip(reduced, cutoff, ExecutionHypothesis())

    def test_cutoff_beyond_data_is_rejected(self):
        market = make_market(hours=30)
        cutoff = market.index[10]
        with pytest.raises(ExecutionContractError):
            resolve_round_trip(market, cutoff, ExecutionHypothesis())

    def test_requires_utc_datetime_index(self):
        frame = make_market()
        naive = frame.tz_localize(None)
        with pytest.raises(ExecutionContractError):
            resolve_round_trip(naive, naive.index[10], ExecutionHypothesis())

    def test_round_trip_record_roundtrips(self):
        market = make_market()
        trip = resolve_round_trip(market, market.index[20], ExecutionHypothesis(latency_hours=1))
        record = trip.to_record()
        assert isinstance(record["cutoff"], str)
        restored = RoundTrip(**{**record, "cutoff": pd.Timestamp(record["cutoff"]), "entry_timestamp": pd.Timestamp(record["entry_timestamp"]), "exit_timestamp": pd.Timestamp(record["exit_timestamp"])})
        assert restored == trip
