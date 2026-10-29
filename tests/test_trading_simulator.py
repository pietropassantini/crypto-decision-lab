from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab.decision_contract import (
    ACTION_LONG,
    ACTION_NO_TRADE,
    ACTION_SHORT,
    CostModel,
    DecisionContractError,
    build_decision,
    execution_prices,
    gross_return,
    net_return,
    validate_decisions,
)
from crypto_decision_lab.trading_simulator import (
    DEFAULT_INITIAL_CAPITAL,
    equity_curve,
    simulate_from_frame,
    simulate_sequence,
)


def _decision(**overrides) -> object:
    defaults = dict(
        window_index=1,
        cutoff=pd.Timestamp("2024-01-01", tz="UTC"),
        entry_open=100.0,
        exit_open=110.0,
        action=ACTION_LONG,
        costs=CostModel(fee_rate=0.001, slippage=0.0),
        reference_price=100.0,
        data_version="test",
        policy_version="test-policy",
    )
    defaults.update(overrides)
    return build_decision(**defaults)


def test_long_net_return_formula():
    costs = CostModel(fee_rate=0.001, slippage=0.0)
    # LONG 100 -> 110 with 0.1% fees per side
    expected = (110.0 * 0.999) / (100.0 * 1.001) - 1.0
    assert net_return(100.0, 110.0, ACTION_LONG, costs) == pytest.approx(expected)


def test_short_net_return_formula():
    costs = CostModel(fee_rate=0.001, slippage=0.0)
    # SHORT 100 -> 90: sell high, buy back low
    expected = (100.0 * 0.999) / (90.0 * 1.001) - 1.0
    assert net_return(100.0, 90.0, ACTION_SHORT, costs) == pytest.approx(expected)


def test_no_trade_has_zero_cost_and_return():
    costs = CostModel(fee_rate=0.01, slippage=0.01)
    assert net_return(100.0, 110.0, ACTION_NO_TRADE, costs) == 0.0
    assert gross_return(100.0, 110.0, ACTION_NO_TRADE) == 0.0
    decision = _decision(action=ACTION_NO_TRADE, costs=costs)
    assert decision.execution_price is None
    assert decision.exit_price is None
    assert decision.net_return_pct == 0.0


def test_slippage_worsens_both_sides():
    costs = CostModel(fee_rate=0.0, slippage=0.01)
    long_entry, long_exit = execution_prices(100.0, 100.0, ACTION_LONG, costs)
    assert long_entry == pytest.approx(101.0)
    assert long_exit == pytest.approx(99.0)
    short_entry, short_exit = execution_prices(100.0, 100.0, ACTION_SHORT, costs)
    assert short_entry == pytest.approx(99.0)
    assert short_exit == pytest.approx(101.0)
    assert net_return(100.0, 100.0, ACTION_LONG, costs) < 0.0
    assert net_return(100.0, 100.0, ACTION_SHORT, costs) < 0.0


def test_entry_bar_is_never_the_anchor_bar():
    # Entry on the anchor bar (cutoff - 1h) violates the execution contract.
    decision = _decision(window_index=1)
    record = decision.to_record()
    cutoff = pd.Timestamp(record["cutoff_timestamp"])
    record["entry_timestamp"] = str(cutoff - pd.Timedelta(hours=1))
    record["exit_timestamp"] = str(cutoff + pd.Timedelta(hours=23))
    with pytest.raises(DecisionContractError, match="anchor bar"):
        validate_decisions(pd.DataFrame([record]))
    # Unknown actions are rejected by the constructor as well.
    with pytest.raises(DecisionContractError):
        build_decision(
            window_index=1,
            cutoff=pd.Timestamp("2024-01-01", tz="UTC"),
            entry_open=100.0,
            exit_open=110.0,
            action="INVALID",
            costs=CostModel(),
            reference_price=100.0,
            data_version="t",
            policy_version="p",
        )


def test_decision_contract_fields_and_ids():
    decision = _decision(window_index=42, policy_version="mom")
    assert decision.decision_id == "CP07:window-0042:mom"
    assert decision.sample_id == "S02-CP06A:window-0042"
    assert decision.entry_timestamp == pd.Timestamp("2024-01-01", tz="UTC")
    assert decision.exit_timestamp == pd.Timestamp("2024-01-02", tz="UTC")
    assert decision.horizon_hours == 24
    assert decision.gross_return_pct == pytest.approx(10.0)


def test_validate_decisions_rejects_contract_violations():
    good = _decision(window_index=1)
    frame = pd.DataFrame([good.to_record()])
    validate_decisions(frame)
    bad = good.to_record()
    bad["exit_timestamp"] = str(pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(hours=12))
    with pytest.raises(DecisionContractError, match="horizon"):
        validate_decisions(pd.DataFrame([bad]))
    sneaky = good.to_record()
    sneaky["entry_timestamp"] = str(pd.Timestamp("2023-12-31", tz="UTC") + pd.Timedelta(hours=23))
    sneaky["decision_timestamp"] = sneaky["entry_timestamp"]
    sneaky["cutoff_timestamp"] = sneaky["entry_timestamp"]
    with pytest.raises(DecisionContractError):
        validate_decisions(pd.DataFrame([sneaky]))


def test_simulation_is_self_financing_and_deterministic():
    first = _decision(
        window_index=1,
        cutoff=pd.Timestamp("2024-01-01", tz="UTC"),
        entry_open=100.0,
        exit_open=110.0,
        costs=CostModel(fee_rate=0.0, slippage=0.0),
        policy_version="p",
    )
    second = _decision(
        window_index=2,
        cutoff=pd.Timestamp("2024-01-02", tz="UTC"),
        entry_open=110.0,
        exit_open=99.0,
        costs=CostModel(fee_rate=0.0, slippage=0.0),
        policy_version="p",
    )
    ledger, metrics = simulate_sequence([first, second], initial_capital=1000.0)
    assert metrics.trades == 2
    assert metrics.final_equity == pytest.approx(1000.0 * 1.10 * 0.9)
    assert metrics.total_return_pct == pytest.approx(-1.0)
    again, metrics_again = simulate_sequence([second, first], initial_capital=1000.0)
    assert metrics_again.final_equity == pytest.approx(metrics.final_equity)
    assert again["equity_after"].tolist() == ledger["equity_after"].tolist()


def test_overlapping_signal_is_skipped_not_invented():
    first = _decision(
        window_index=1,
        cutoff=pd.Timestamp("2024-01-01", tz="UTC"),
        policy_version="p",
    )
    overlapping = _decision(
        window_index=2,
        cutoff=pd.Timestamp("2024-01-01 12:00", tz="UTC"),
        policy_version="p",
    )
    ledger, metrics = simulate_sequence([first, overlapping], initial_capital=1000.0)
    assert metrics.trades == 1
    assert metrics.skipped_signals == 1
    skipped_row = ledger[~ledger["executed"]].iloc[0]
    assert skipped_row["net_return_pct"] == 0.0


def test_metrics_win_rate_drawdown_profit_factor():
    decisions = [
        _decision(
            window_index=index,
            cutoff=pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(days=index),
            entry_open=100.0,
            exit_open=110.0 if index % 2 == 0 else 95.0,
            costs=CostModel(fee_rate=0.0, slippage=0.0),
            policy_version="p",
        )
        for index in range(4)
    ]
    ledger, metrics = simulate_sequence(decisions, initial_capital=1000.0)
    assert metrics.trades == 4
    assert metrics.win_rate_pct == pytest.approx(50.0)
    assert metrics.max_drawdown_pct < 0.0
    gains = 0.10 + 0.10
    losses = abs(0.05) + abs(0.05)
    assert metrics.profit_factor == pytest.approx(gains / losses)
    curve = equity_curve(ledger, initial_capital=1000.0)
    assert curve.iloc[0] == pytest.approx(1000.0)
    assert len(curve) == 5


def test_exposure_time_and_no_trade_handling():
    decisions = [
        _decision(
            window_index=1,
            cutoff=pd.Timestamp("2024-01-01", tz="UTC"),
            policy_version="p",
        ),
        _decision(
            window_index=2,
            cutoff=pd.Timestamp("2024-01-02", tz="UTC"),
            action=ACTION_NO_TRADE,
            policy_version="p",
        ),
    ]
    ledger, metrics = simulate_sequence(decisions, initial_capital=1000.0)
    assert metrics.trades == 1
    assert metrics.no_trade == 1
    assert metrics.exposure_pct > 0.0
    # Two days span, one 24h position -> roughly half the time in market.
    assert metrics.exposure_pct == pytest.approx(50.0, rel=0.2)


def test_simulate_from_frame_validates_first():
    row = _decision(window_index=1).to_record()
    frame = pd.DataFrame([row])
    ledger, metrics = simulate_from_frame(frame, initial_capital=100.0)
    assert metrics.trades == 1
    tampered = frame.copy()
    tampered.loc[0, "net_return_pct"] = -150.0
    with pytest.raises(DecisionContractError):
        simulate_from_frame(tampered)
