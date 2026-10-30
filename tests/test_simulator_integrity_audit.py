from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab.decision_contract import (
    ACTION_LONG,
    ACTION_NO_TRADE,
    ACTION_SHORT,
    CostModel,
    build_decision,
)
from crypto_decision_lab.simulator_integrity_audit import (
    AuditPaths,
    bootstrap_profile,
    calendar_equity_curves,
    concentration_profile,
    contract_frame,
    dollar_metrics,
    execution_delay_stress,
    max_drawdown,
    reconstruct_returns,
    threshold_robustness,
)
from crypto_decision_lab.trading_simulator import simulate_sequence


# ---------------------------------------------------------------------------
# synthetic fixtures
# ---------------------------------------------------------------------------


def _decision(window: int, cutoff: pd.Timestamp, action: str, entry: float, exit_: float, cost: CostModel):
    return build_decision(
        window_index=window,
        cutoff=cutoff,
        entry_open=entry,
        exit_open=exit_,
        action=action,
        costs=cost,
        reference_price=entry,
        data_version="test",
        policy_version="momentum_threshold",
    )


def _market_index(start: pd.Timestamp, hours: int) -> pd.DatetimeIndex:
    return pd.date_range(start, periods=hours, freq="h", tz="UTC")


def _archived_policy_frame() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Small CP07-shaped archive: 4 windows of a +10% / -10% pattern."""

    cost = CostModel(fee_rate=0.001, slippage=0.0005)
    cutoffs = [pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(days=day) for day in range(4)]
    rows = []
    label_rows = []
    equity = 10_000.0
    prices = [(100.0, 110.0), (110.0, 99.0), (99.0, 108.9), (108.9, 98.01)]
    for index, (cutoff, (entry, exit_)) in enumerate(zip(cutoffs, prices)):
        action = ACTION_LONG if index % 2 == 0 else ACTION_SHORT
        decision = _decision(index, cutoff, action, entry, exit_, cost)
        equity *= 1.0 + decision.net_return_pct / 100.0
        rows.append(
            {
                "fold": 0,
                "policy": "momentum_threshold",
                "decision_id": decision.decision_id,
                "window_index": index,
                "cutoff": cutoff,
                "entry_timestamp": cutoff,
                "exit_timestamp": cutoff + pd.Timedelta(hours=24),
                "action": action,
                "reference_price": entry,
                "execution_price": decision.execution_price,
                "exit_price": decision.exit_price,
                "fee_rate": cost.fee_rate,
                "slippage": cost.slippage,
                "gross_return_pct": decision.gross_return_pct,
                "net_return_pct": decision.net_return_pct,
                "regime": "TREND_UP",
                "uses_target": False,
                "threshold": 0.05,
            }
        )
        label_rows.append(
            {
                "window_index": index,
                "cutoff": cutoff,
                "entry_timestamp": cutoff,
                "exit_timestamp": cutoff + pd.Timedelta(hours=24),
                "entry_open": entry,
                "exit_open": exit_,
            }
        )
    archived = pd.DataFrame(rows)
    labels = pd.DataFrame(label_rows)
    # Market covering the four windows with a flat 100 until the first cutoff.
    index = _market_index(pd.Timestamp("2023-12-31", tz="UTC"), 8 * 24 + 24)
    market = pd.DataFrame({"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1.0}, index=index)
    for label in label_rows:
        market.loc[label["cutoff"], "open"] = label["entry_open"]
        market.loc[label["exit_timestamp"], "open"] = label["exit_open"]
        window = market.loc[label["cutoff"]: label["exit_timestamp"]]
        market.loc[window.index, "close"] = label["entry_open"]
    return archived, labels, market


def test_contract_frame_reconstructs_missing_contract_columns():
    archived, labels, _ = _archived_policy_frame()
    frame, diagnostics = contract_frame(archived, labels)
    assert len(frame) == len(archived)
    assert set(diagnostics["missing_contract_columns"]) == {
        "decision_timestamp",
        "horizon_hours",
        "policy_version",
    }
    assert diagnostics["label_timestamp_mismatches"] == 0
    assert (frame["policy_version"] == "momentum_threshold").all()
    assert (frame["horizon_hours"] == 24).all()
    assert (pd.to_datetime(frame["decision_timestamp"], utc=True) == pd.to_datetime(frame["cutoff_timestamp"], utc=True)).all()


def test_reconstruct_returns_matches_archived_decisions():
    archived, labels, _ = _archived_policy_frame()
    frame, _ = contract_frame(archived, labels)
    rebuilt, diagnostics = reconstruct_returns(frame, labels)
    assert diagnostics["max_abs_net_error_pct"] == pytest.approx(0.0, abs=1e-12)
    assert diagnostics["max_rel_entry_price_error"] == pytest.approx(0.0, abs=1e-12)
    assert diagnostics["max_rel_exit_price_error"] == pytest.approx(0.0, abs=1e-12)
    assert diagnostics["no_trade_rows_with_prices"] == 0
    assert rebuilt["rebuilt_net_pct"].notna().all()


def test_dollar_profit_factor_differs_from_percent_sums_under_compounding():
    cost = CostModel(fee_rate=0.0, slippage=0.0)
    decisions = [
        _decision(1, pd.Timestamp("2024-01-01", tz="UTC"), ACTION_LONG, 100.0, 200.0, cost),
        _decision(2, pd.Timestamp("2024-01-02", tz="UTC"), ACTION_LONG, 100.0, 75.0, cost),
    ]
    ledger, _ = simulate_sequence(decisions, initial_capital=1_000.0)
    metrics = dollar_metrics(ledger, initial_capital=1_000.0)
    assert metrics["profit_factor_pct"] == pytest.approx(100.0 / 25.0)
    # Monetary PF is equity-weighted: the -25% loss is taken on a 2x larger book.
    assert metrics["profit_factor_usdt"] == pytest.approx(1000.0 / 500.0)
    assert metrics["profit_factor_usdt"] != pytest.approx(metrics["profit_factor_pct"])


def test_no_trade_never_blocks_a_later_signal():
    cost = CostModel(fee_rate=0.0, slippage=0.0)
    flat = _decision(1, pd.Timestamp("2024-01-01", tz="UTC"), ACTION_NO_TRADE, 100.0, 110.0, cost)
    traded = _decision(2, pd.Timestamp("2024-01-01 12:00", tz="UTC"), ACTION_LONG, 100.0, 110.0, cost)
    ledger, metrics = simulate_sequence([flat, traded], initial_capital=1_000.0)
    assert metrics.no_trade == 1
    assert metrics.skipped_signals == 0
    assert metrics.trades == 1
    assert metrics.total_return_pct == pytest.approx(10.0)
    assert bool(ledger.iloc[0]["executed"]) is True


def test_calendar_equity_curves_are_hourly_and_match_ledger():
    archived, labels, market = _archived_policy_frame()
    frame, _ = contract_frame(archived, labels)
    frame, _ = reconstruct_returns(frame, labels)
    ledger, _ = simulate_sequence(
        [
            _decision(
                index,
                pd.Timestamp(row.cutoff_timestamp),
                row.action,
                row.entry_open,
                row.exit_open,
                CostModel(fee_rate=row.fee_rate, slippage=row.slippage),
            )
            for index, row in enumerate(frame.itertuples(index=False))
        ],
        initial_capital=10_000.0,
    )
    step, mark = calendar_equity_curves(ledger, market, initial_capital=10_000.0)
    assert len(step) > 0
    assert (step.index.to_series().diff().dropna() == pd.Timedelta(hours=1)).all()
    executed = ledger[ledger["executed"]]
    exits = pd.to_datetime(executed["exit_timestamp"], utc=True)
    assert np.allclose(step.reindex(exits).to_numpy(dtype=float), executed["equity_after"].to_numpy(dtype=float))
    # A flat market marks to a negative value inside a LONG hold (entry fee/slippage).
    assert mark.min() < 10_000.0


def test_mark_to_market_drawdown_exceeds_exit_marked_drawdown():
    cost = CostModel(fee_rate=0.0, slippage=0.0)
    start = pd.Timestamp("2024-01-01", tz="UTC")
    market = pd.DataFrame(
        {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1.0},
        index=_market_index(start, 48),
    )
    # 24h hold: price craters to 50 and recovers to 110 at the exit open.
    market.loc[start + pd.Timedelta(hours=12), "close"] = 50.0
    market.loc[start + pd.Timedelta(hours=24), "open"] = 110.0
    decision = _decision(1, start, ACTION_LONG, 100.0, 110.0, cost)
    ledger, _ = simulate_sequence([decision], initial_capital=1_000.0)
    step, mark = calendar_equity_curves(ledger, market, initial_capital=1_000.0)
    exit_dd = max_drawdown(step.to_numpy(dtype=float)[1:], initial_capital=1_000.0)
    mtm_dd = max_drawdown(mark.to_numpy(dtype=float)[1:], initial_capital=1_000.0)
    assert exit_dd == pytest.approx(0.0)
    assert mtm_dd == pytest.approx(-50.0)


def test_execution_delay_stress_penalises_late_entry():
    cost = CostModel(fee_rate=0.0, slippage=0.0)
    start = pd.Timestamp("2024-01-01", tz="UTC")
    market = pd.DataFrame(
        {"open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0, "volume": 1.0},
        index=_market_index(start, 72),
    )
    market.loc[start + pd.Timedelta(hours=24), "open"] = 110.0
    market.loc[start + pd.Timedelta(hours=25), "open"] = 120.0
    market.loc[start + pd.Timedelta(hours=48), "open"] = 110.0
    decision = _decision(1, start, ACTION_LONG, 100.0, 110.0, cost)
    ledger, _ = simulate_sequence([decision], initial_capital=1_000.0)
    stress = execution_delay_stress(ledger, market, initial_capital=1_000.0, delays=(0, 1))
    assert stress["0"]["total_return_pct"] == pytest.approx(10.0)
    assert stress["1"]["total_return_pct"] == pytest.approx(20.0)
    assert stress["1"]["missing_bars"] == 0


def test_concentration_profile_removes_best_trades():
    cost = CostModel(fee_rate=0.0, slippage=0.0)
    decisions = [
        _decision(1, pd.Timestamp("2024-01-01", tz="UTC"), ACTION_LONG, 100.0, 200.0, cost),
        _decision(2, pd.Timestamp("2024-01-02", tz="UTC"), ACTION_LONG, 100.0, 101.0, cost),
        _decision(3, pd.Timestamp("2024-01-03", tz="UTC"), ACTION_LONG, 100.0, 101.0, cost),
    ]
    ledger, _ = simulate_sequence(decisions, initial_capital=1_000.0)
    profile = concentration_profile(ledger, initial_capital=1_000.0)["leave_out"]
    assert profile["0"] == pytest.approx((2.0 * 1.01 * 1.01 - 1.0) * 100.0)
    assert profile["1"] == pytest.approx((1.01 * 1.01 - 1.0) * 100.0)


def test_bootstrap_profile_is_deterministic_and_reports_uncertainty():
    values = np.array([1.0, -0.5, 0.7, -0.2, 1.2, -1.0] * 10)
    first = bootstrap_profile(values, draws=200, seed=7)
    second = bootstrap_profile(values, draws=200, seed=7)
    assert first == second
    assert first["total_return_pct"]["ci_low"] <= first["total_return_pct"]["point"]
    assert first["profit_factor_pct"]["ci_low"] <= first["profit_factor_pct"]["ci_high"]


def test_threshold_robustness_reports_grid_boundary(tmp_path):
    """A threshold selected at the last grid point is flagged as boundary."""

    from crypto_decision_lab.policy_evaluation import make_folds
    from crypto_decision_lab.trading_policy import THRESHOLD_GRID, load_inputs

    # Reuse the frozen CP07 datasets: this is a read-only diagnostic.
    root = Path(__file__).resolve().parents[1]
    label_dir = root / "data/datasets/S02-CP07-decision-labels-v1"
    regime_dir = root / "data/datasets/S02-CP06B-regime-features-v1"
    if not (label_dir / "labels.parquet").exists():
        pytest.skip("CP07 frozen datasets not available")
    data = load_inputs(label_dir, regime_dir)
    folds = make_folds(data, n_folds=5, min_train=200, test_size=60)
    fold_metrics = pd.DataFrame(
        [
            {
                "fold": fold["fold"],
                "policy": "momentum_threshold",
                "state": {"threshold": float(max(THRESHOLD_GRID))},
            }
            for fold in folds
        ]
    )
    paths = AuditPaths(label_dir=label_dir, regime_dir=regime_dir)
    result = threshold_robustness(paths, fold_metrics)
    assert result["boundary_folds"] == 5
    assert len(result["folds"]) == 5
    assert all(row["grid_boundary"] for row in result["folds"])
    assert all(row["refit_matches_archive"] for row in result["folds"])


def test_max_drawdown_includes_initial_capital():
    assert max_drawdown(np.array([900.0, 950.0]), initial_capital=1_000.0) == pytest.approx(-10.0)
    assert max_drawdown(np.array([1_100.0, 1_050.0]), initial_capital=1_000.0) == pytest.approx(-4.5454545, rel=1e-6)


def test_cost_sensitivity_confound_detection():
    from crypto_decision_lab.simulator_integrity_audit import _cost_sensitivity_confound_check

    def context(scenarios):
        return {
            "sensitivity": {
                "scenarios": [
                    {
                        "fee_rate": fee,
                        "slippage": slippage,
                        "present": True,
                        "momentum_total_return_pct": value,
                    }
                    for fee, slippage, value in scenarios
                ]
            }
        }

    monotone = _cost_sensitivity_confound_check(
        context([(0.0, 0.0, 100.0), (0.0005, 0.0005, 40.0), (0.001, 0.001, 10.0)])
    )
    assert monotone["status"] == "PASS"
    confounded = _cost_sensitivity_confound_check(
        context([(0.0, 0.0, 100.0), (0.0005, 0.0005, 10.0), (0.001, 0.001, 40.0)])
    )
    assert confounded["status"] == "WARN"
    # Round-trip cost is fee + slippage: the two 0.10% total scenarios collapse
    # onto the same key, which is exactly why the grid cannot separate them.
    collapsed = _cost_sensitivity_confound_check(
        context([(0.001, 0.0, 40.0), (0.0, 0.001, 40.0)])
    )
    assert list(collapsed["evidence"]["total_return_by_total_cost"].keys()) == ["0.001"]
