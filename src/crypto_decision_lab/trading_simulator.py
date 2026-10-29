"""S02-CP07 STEP 2: deterministic self-financing offline trading simulator.

Model
-----
* One position at a time, processed in chronological order.
* Sizing: 100% of current equity per trade (no leverage, no partial fills).
* Each trade is a full round trip over ``horizon_hours`` hourly bars
  (open of the entry bar to open of the exit bar).
* A signal whose entry timestamp precedes the previous trade's exit is skipped
  (never re-entered retroactively); skipped signals are counted, not invented.
* NO_TRADE keeps the equity flat: zero return, zero costs.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .decision_contract import (
    ACTION_NO_TRADE,
    ACTION_LONG,
    ACTION_SHORT,
    CostModel,
    Decision,
    DecisionContractError,
    validate_decisions,
)

DEFAULT_INITIAL_CAPITAL = 10_000.0


@dataclass(frozen=True)
class SimulationMetrics:
    trades: int
    skipped_signals: int
    no_trade: int
    total_return_pct: float
    final_equity: float
    max_drawdown_pct: float
    win_rate_pct: float
    profit_factor: float | None
    exposure_pct: float
    avg_net_return_pct: float
    best_trade_pct: float
    worst_trade_pct: float


def simulate_sequence(
    decisions: Iterable[Decision],
    *,
    initial_capital: float = DEFAULT_INITIAL_CAPITAL,
) -> tuple[pd.DataFrame, SimulationMetrics]:
    """Apply decisions chronologically and return (ledger, metrics)."""

    ordered = sorted(decisions, key=lambda item: item.entry_timestamp)
    equity = float(initial_capital)
    if equity <= 0:
        raise DecisionContractError("initial_capital must be positive")
    ledger_rows: list[dict[str, Any]] = []
    skipped = 0
    no_trade = 0
    trades = 0
    last_exit: pd.Timestamp | None = None
    exposure_hours = 0.0
    for decision in ordered:
        entry = pd.Timestamp(decision.entry_timestamp)
        exit_ = pd.Timestamp(decision.exit_timestamp)
        if last_exit is not None and entry < last_exit:
            skipped += 1
            ledger_rows.append(_ledger_row(decision, equity, executed=False))
            continue
        net = float(decision.net_return_pct) / 100.0
        equity *= 1.0 + net
        last_exit = exit_
        if decision.action == ACTION_NO_TRADE:
            no_trade += 1
        else:
            trades += 1
            exposure_hours += float(decision.horizon_hours)
        ledger_rows.append(_ledger_row(decision, equity, executed=True))
    ledger = pd.DataFrame(ledger_rows)
    metrics = compute_metrics(
        ledger,
        initial_capital=initial_capital,
        trades=trades,
        skipped_signals=skipped,
        no_trade=no_trade,
        exposure_hours=exposure_hours,
    )
    return ledger, metrics


def _ledger_row(decision: Decision, equity_after: float, *, executed: bool) -> dict[str, Any]:
    return {
        "decision_id": decision.decision_id,
        "window_index": int(decision.decision_id.split("window-")[1].split(":")[0]),
        "policy_version": decision.policy_version,
        "action": decision.action,
        "cutoff_timestamp": decision.cutoff_timestamp,
        "entry_timestamp": decision.entry_timestamp,
        "exit_timestamp": decision.exit_timestamp,
        "reference_price": decision.reference_price,
        "execution_price": decision.execution_price,
        "exit_price": decision.exit_price,
        "fee_rate": decision.fee_rate,
        "slippage": decision.slippage,
        "gross_return_pct": decision.gross_return_pct,
        "net_return_pct": decision.net_return_pct if executed else 0.0,
        "executed": executed,
        "equity_after": equity_after,
        "fold": decision.fold,
        "regime": decision.regime,
    }


def equity_curve(ledger: pd.DataFrame, *, initial_capital: float) -> pd.Series:
    """Equity after each executed trade, prefixed with the initial capital."""

    if ledger.empty:
        return pd.Series(dtype=float)
    executed = ledger[ledger["executed"]]
    if executed.empty:
        return pd.Series([initial_capital], index=pd.DatetimeIndex([], name="exit_timestamp"), dtype=float)
    curve = pd.Series(
        executed["equity_after"].to_numpy(dtype=float),
        index=pd.DatetimeIndex(pd.to_datetime(executed["exit_timestamp"], utc=True), name="exit_timestamp"),
    )
    return pd.concat(
        [pd.Series([initial_capital], index=[curve.index[0] - pd.Timedelta(hours=1)]), curve]
    )


def compute_metrics(
    ledger: pd.DataFrame,
    *,
    initial_capital: float,
    trades: int,
    skipped_signals: int,
    no_trade: int,
    exposure_hours: float,
) -> SimulationMetrics:
    executed = ledger[ledger["executed"]]
    net = executed["net_return_pct"].to_numpy(dtype=float)
    final_equity = float(executed["equity_after"].iloc[-1]) if len(executed) else float(initial_capital)
    curve = executed["equity_after"].to_numpy(dtype=float)
    peak = np.maximum.accumulate(np.concatenate([[initial_capital], curve]))
    drawdown = (np.concatenate([[initial_capital], curve]) - peak) / peak
    max_drawdown_pct = float(drawdown.min() * 100.0)
    wins = net[net > 0]
    losses = net[net < 0]
    profit_factor = (
        float(wins.sum() / abs(losses.sum())) if len(losses) and losses.sum() != 0 else (float("inf") if len(wins) else None)
    )
    if len(executed):
        first_entry = pd.Timestamp(executed["entry_timestamp"].iloc[0])
        last_exit = pd.Timestamp(executed["exit_timestamp"].iloc[-1])
        span_hours = max((last_exit - first_entry).total_seconds() / 3600.0, 1.0)
    else:
        span_hours = 1.0
    return SimulationMetrics(
        trades=int(trades),
        skipped_signals=int(skipped_signals),
        no_trade=int(no_trade),
        total_return_pct=100.0 * (final_equity / initial_capital - 1.0),
        final_equity=final_equity,
        max_drawdown_pct=max_drawdown_pct,
        win_rate_pct=100.0 * float((net > 0).mean()) if len(net) else 0.0,
        profit_factor=profit_factor,
        exposure_pct=100.0 * min(1.0, exposure_hours / span_hours),
        avg_net_return_pct=float(net.mean()) if len(net) else 0.0,
        best_trade_pct=float(net.max()) if len(net) else 0.0,
        worst_trade_pct=float(net.min()) if len(net) else 0.0,
    )


def simulate_from_frame(
    frame: pd.DataFrame,
    *,
    initial_capital: float = DEFAULT_INITIAL_CAPITAL,
) -> tuple[pd.DataFrame, SimulationMetrics]:
    """Validate a decisions frame and simulate it (used by tests and CLI)."""

    validated = validate_decisions(frame)
    decisions = [
        Decision(
            decision_id=row["decision_id"],
            sample_id=str(row.get("decision_id", "")),
            cutoff_timestamp=pd.Timestamp(row["cutoff_timestamp"]),
            decision_timestamp=pd.Timestamp(row["decision_timestamp"]),
            entry_timestamp=pd.Timestamp(row["entry_timestamp"]),
            exit_timestamp=pd.Timestamp(row["exit_timestamp"]),
            horizon_hours=int(row["horizon_hours"]),
            action=row["action"],
            reference_price=float(row["reference_price"]),
            execution_price=row.get("execution_price"),
            exit_price=row.get("exit_price"),
            fee_rate=float(row.get("fee_rate", 0.0)),
            slippage=float(row.get("slippage", 0.0)),
            gross_return_pct=float(row.get("gross_return_pct", 0.0)),
            net_return_pct=float(row["net_return_pct"]),
            data_version=str(row.get("data_version", "")),
            policy_version=str(row["policy_version"]),
            fold=row.get("fold"),
            regime=row.get("regime"),
        )
        for _, row in validated.iterrows()
    ]
    return simulate_sequence(decisions, initial_capital=initial_capital)
