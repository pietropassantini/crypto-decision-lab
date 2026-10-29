"""S02-CP07 STEP 1: decision contract for the offline trading simulator.

Temporal semantics (hourly bars, index = bar open time, UTC)
-----------------------------------------------------------
```
bar(cutoff - 1h)   closes at cutoff        <- its CLOSE is the anchor/reference
                                            price and the ONLY price used to
                                            generate the decision
bar(cutoff)        opens at cutoff         <- ENTRY bar: its open is the first
                                            price printed after the decision
bar(cutoff + 24h)  opens at cutoff + 24h   <- EXIT bar: entry open -> exit open
                                            spans exactly 24 hourly bars
```

Rules enforced by this module:

* the entry bar is never the bar whose close generated the decision
  (``entry_timestamp > anchor_bar_timestamp``);
* ``decision_timestamp <= entry_timestamp < exit_timestamp`` with
  ``exit_timestamp - entry_timestamp == horizon_hours``;
* NO_TRADE carries no execution prices and no costs;
* returns follow the canonical unit conventions: percentages are percent
  (5.0 = 5%), schema version is explicit in every record.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

DECISION_SCHEMA = "cdl.cp07.decision.v1"
LABEL_SCHEMA = "cdl.cp07.label.v1"
ACTION_LONG = "LONG"
ACTION_SHORT = "SHORT"
ACTION_NO_TRADE = "NO_TRADE"
ACTIONS = (ACTION_LONG, ACTION_SHORT, ACTION_NO_TRADE)
HORIZON_HOURS = 24
HOUR = pd.Timedelta(hours=1)

# Execution fee/slippage defaults (per side, fraction of notional).
DEFAULT_FEE_RATE = 0.001
DEFAULT_SLIPPAGE = 0.0005
# A label whose best action beats the runner-up by less than this margin is
# economically ambiguous (below typical round-trip cost noise).
DEFAULT_AMBIGUITY_MARGIN = 0.001


class DecisionContractError(ValueError):
    """Raised when a decision violates the execution contract."""


def utc(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CostModel:
    """Round-trip execution costs. Fees are charged on both sides."""

    fee_rate: float = DEFAULT_FEE_RATE
    slippage: float = DEFAULT_SLIPPAGE

    def __post_init__(self) -> None:
        if not 0.0 <= self.fee_rate < 0.05:
            raise DecisionContractError(f"fee_rate out of range: {self.fee_rate}")
        if not 0.0 <= self.slippage < 0.05:
            raise DecisionContractError(f"slippage out of range: {self.slippage}")

    @property
    def label(self) -> str:
        return f"fee={self.fee_rate:g}_slip={self.slippage:g}"


@dataclass(frozen=True)
class Decision:
    """One simulated trading decision on one canonical forecast window."""

    decision_id: str
    sample_id: str
    cutoff_timestamp: pd.Timestamp
    decision_timestamp: pd.Timestamp
    entry_timestamp: pd.Timestamp
    exit_timestamp: pd.Timestamp
    horizon_hours: int
    action: str
    reference_price: float
    execution_price: float | None
    exit_price: float | None
    fee_rate: float
    slippage: float
    gross_return_pct: float
    net_return_pct: float
    data_version: str
    policy_version: str
    fold: int | None = None
    regime: str | None = field(default=None)

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        for key in ("cutoff_timestamp", "decision_timestamp", "entry_timestamp", "exit_timestamp"):
            record[key] = str(record[key])
        return record


def execution_prices(
    entry_open: float, exit_open: float, action: str, costs: CostModel
) -> tuple[float, float]:
    """Direction-aware executable prices including slippage."""

    entry_open = float(entry_open)
    exit_open = float(exit_open)
    if entry_open <= 0 or exit_open <= 0:
        raise DecisionContractError("Execution prices must be positive")
    if action == ACTION_LONG:
        return entry_open * (1.0 + costs.slippage), exit_open * (1.0 - costs.slippage)
    if action == ACTION_SHORT:
        return entry_open * (1.0 - costs.slippage), exit_open * (1.0 + costs.slippage)
    raise DecisionContractError(f"Action {action} has no execution prices")


def net_return(entry_open: float, exit_open: float, action: str, costs: CostModel) -> float:
    """Self-financing fractional return of one round trip (no leverage).

    LONG : net = [exit_exec * (1 - f)] / [entry_exec * (1 + f)] - 1
    SHORT: net = [entry_exec * (1 - f)] / [exit_exec * (1 + f)] - 1
    """

    if action == ACTION_NO_TRADE:
        return 0.0
    entry_exec, exit_exec = execution_prices(entry_open, exit_open, action, costs)
    if action == ACTION_LONG:
        return (exit_exec * (1.0 - costs.fee_rate)) / (entry_exec * (1.0 + costs.fee_rate)) - 1.0
    return (entry_exec * (1.0 - costs.fee_rate)) / (exit_exec * (1.0 + costs.fee_rate)) - 1.0


def gross_return(entry_open: float, exit_open: float, action: str) -> float:
    """Gross fractional return before fees and slippage."""

    if action == ACTION_NO_TRADE:
        return 0.0
    entry_open = float(entry_open)
    exit_open = float(exit_open)
    if action == ACTION_LONG:
        return exit_open / entry_open - 1.0
    return entry_open / exit_open - 1.0


def build_decision(
    *,
    window_index: int,
    cutoff: pd.Timestamp,
    entry_open: float,
    exit_open: float,
    action: str,
    costs: CostModel,
    reference_price: float,
    data_version: str,
    policy_version: str,
    horizon_hours: int = HORIZON_HOURS,
    fold: int | None = None,
    regime: str | None = None,
) -> Decision:
    """Create and validate one decision following the execution contract."""

    if action not in ACTIONS:
        raise DecisionContractError(f"Unknown action: {action}")
    cutoff = utc(cutoff)
    entry_timestamp = cutoff
    exit_timestamp = cutoff + pd.Timedelta(hours=int(horizon_hours))
    anchor_bar = cutoff - HOUR
    if not entry_timestamp > anchor_bar:
        raise DecisionContractError("Entry bar must be after the bar whose close generated the decision")
    if not entry_open > 0 or not exit_open > 0 or not reference_price > 0:
        raise DecisionContractError("Prices must be positive")
    if action == ACTION_NO_TRADE:
        execution_price = None
        exit_price = None
        gross = 0.0
        net = 0.0
    else:
        entry_exec, exit_exec = execution_prices(entry_open, exit_open, action, costs)
        execution_price = entry_exec
        exit_price = exit_exec
        gross = gross_return(entry_open, exit_open, action)
        net = net_return(entry_open, exit_open, action, costs)
    return Decision(
        decision_id=f"CP07:window-{int(window_index):04d}:{policy_version}",
        sample_id=f"S02-CP06A:window-{int(window_index):04d}",
        cutoff_timestamp=cutoff,
        decision_timestamp=cutoff,
        entry_timestamp=entry_timestamp,
        exit_timestamp=exit_timestamp,
        horizon_hours=int(horizon_hours),
        action=action,
        reference_price=float(reference_price),
        execution_price=execution_price,
        exit_price=exit_price,
        fee_rate=costs.fee_rate,
        slippage=costs.slippage,
        gross_return_pct=100.0 * gross,
        net_return_pct=100.0 * net,
        data_version=data_version,
        policy_version=policy_version,
        fold=fold,
        regime=regime,
    )


def validate_decisions(frame: pd.DataFrame) -> pd.DataFrame:
    """Validate a decisions frame without modifying it."""

    required = {
        "decision_id",
        "cutoff_timestamp",
        "decision_timestamp",
        "entry_timestamp",
        "exit_timestamp",
        "horizon_hours",
        "action",
        "reference_price",
        "net_return_pct",
        "policy_version",
    }
    missing = required - set(frame.columns)
    if missing:
        raise DecisionContractError(f"Decision columns missing: {sorted(missing)}")
    if frame.empty:
        raise DecisionContractError("Decisions frame is empty")
    data = frame.copy()
    for column in ("cutoff_timestamp", "decision_timestamp", "entry_timestamp", "exit_timestamp"):
        data[column] = pd.to_datetime(data[column], utc=True, errors="raise")
    if set(data["action"]) - set(ACTIONS):
        raise DecisionContractError("Unknown action values present")
    if (data["horizon_hours"].astype(int) != HORIZON_HOURS).any():
        raise DecisionContractError("Unexpected horizon")
    if not (data["exit_timestamp"] - data["entry_timestamp"] == pd.to_timedelta(data["horizon_hours"], unit="h")).all():
        raise DecisionContractError("exit_timestamp must equal entry_timestamp + horizon_hours")
    anchor_bar = data["cutoff_timestamp"] - HOUR
    if not (data["entry_timestamp"] > anchor_bar).all():
        raise DecisionContractError("Entry bar is not strictly after the anchor bar")
    if not (data["decision_timestamp"] == data["cutoff_timestamp"]).all():
        raise DecisionContractError("decision_timestamp must equal cutoff_timestamp")
    no_trade = data["action"] == ACTION_NO_TRADE
    if not (data.loc[no_trade, "net_return_pct"] == 0.0).all():
        raise DecisionContractError("NO_TRADE rows must have zero net return")
    traded = data.loc[~no_trade]
    if not np.isfinite(traded["net_return_pct"].to_numpy(dtype=float)).all():
        raise DecisionContractError("Non-finite net returns")
    if (traded["net_return_pct"] <= -100.0).any():
        raise DecisionContractError("Net return wiped out the account: leverage or bad prices")
    if data["decision_id"].duplicated().any():
        raise DecisionContractError("Duplicate decision_id values")
    return data
