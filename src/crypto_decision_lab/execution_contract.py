"""S02: execution hypothesis shared by the Dataset Builder and the S03 engine.

Temporal semantics (hourly bars, index = bar open time, UTC)
-----------------------------------------------------------
```
bar(cutoff - 1h)     closes at cutoff          <- anchor bar: the LAST bar whose
                                                  data is available at cutoff
cutoff               decision instant          <- features are available at this
                                                  time (feature_available_at)
bar(cutoff + L)      entry bar                 <- the order becomes executable L
                                                  hours after the decision and
                                                  fills at this bar's open
bar(cutoff + L + H)  exit bar                  <- the position closes H hours
                                                  after the fill, at the open
```

``L`` is ``latency_hours`` and ``H`` is ``horizon_hours``.  At zero latency
this contract reduces *exactly* to the legacy CP07/CP09 contract
(:mod:`crypto_decision_lab.decision_contract`): entry at the open of the
cutoff bar, exit at the open of the bar ``cutoff + 24h``.  Prices and
economics reuse the CP07 formulas (``execution_prices`` / ``net_return``) so
dataset targets, backtests and paper replays share one outcome definition.

S03 agreement: the event engine must either call :func:`resolve_round_trip`
or produce bit-identical outcomes.  The engine keeps its own separation of
event availability (anchor bar closes at cutoff), decision (strategy runs at
cutoff), order eligibility (entry bar exists and the order is still eligible
at ``cutoff + L``) and execution (open of the entry bar adjusted by
slippage, fees on both sides).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
import json
from typing import Any

import pandas as pd

from .decision_contract import (
    ACTION_LONG,
    DEFAULT_FEE_RATE,
    DEFAULT_SLIPPAGE,
    CostModel,
    DecisionContractError,
    execution_prices,
    gross_return,
    net_return,
    now_iso,
    utc,
)

EXECUTION_SCHEMA = "cdl.s02.execution.v1"
HOUR = pd.Timedelta(hours=1)
ACTION_FLAT = "FLAT"


class ExecutionContractError(ValueError):
    """Raised when a round trip violates the execution contract."""


@dataclass(frozen=True)
class ExecutionHypothesis:
    """How cutoff, latency, horizon and costs determine an execution.

    * ``latency_hours``: hours between the decision and the first bar whose
      open can fill the order (0 = legacy CP07 entry at the cutoff bar open).
    * ``horizon_hours``: holding period measured from the fill; the exit is
      the open of the bar ``entry + horizon_hours``.
    * ``fee_rate`` / ``slippage``: per side, fraction of notional (same
      defaults and validation ranges as the CP07 ``CostModel``).
    """

    latency_hours: int = 0
    horizon_hours: int = 24
    fee_rate: float = DEFAULT_FEE_RATE
    slippage: float = DEFAULT_SLIPPAGE

    def __post_init__(self) -> None:
        if int(self.latency_hours) != self.latency_hours or self.latency_hours < 0:
            raise ExecutionContractError(f"latency_hours must be a non-negative integer: {self.latency_hours}")
        if int(self.horizon_hours) != self.horizon_hours or self.horizon_hours < 1:
            raise ExecutionContractError(f"horizon_hours must be a positive integer: {self.horizon_hours}")
        try:
            CostModel(fee_rate=self.fee_rate, slippage=self.slippage)
        except DecisionContractError as exc:
            raise ExecutionContractError(str(exc)) from exc

    @property
    def cost_model(self) -> CostModel:
        return CostModel(fee_rate=self.fee_rate, slippage=self.slippage)

    def entry_timestamp(self, cutoff: pd.Timestamp) -> pd.Timestamp:
        return utc(cutoff) + int(self.latency_hours) * HOUR

    def exit_timestamp(self, cutoff: pd.Timestamp) -> pd.Timestamp:
        return self.entry_timestamp(cutoff) + int(self.horizon_hours) * HOUR

    def required_span(self, cutoff: pd.Timestamp) -> pd.DatetimeIndex:
        """Hourly bars that must exist for a window starting at ``cutoff``."""

        return pd.date_range(
            self.entry_timestamp(cutoff), self.exit_timestamp(cutoff), freq="1h", tz="UTC"
        )

    def declaration(self) -> dict[str, Any]:
        """Human- and machine-readable declaration of the target semantics."""

        return {
            "schema_version": EXECUTION_SCHEMA,
            "decision": "features and decision are available at the cutoff; the anchor bar cutoff - 1h is the last input",
            "latency_hours": int(self.latency_hours),
            "latency": f"the order fills at the open of the bar cutoff + {int(self.latency_hours)}h",
            "horizon_hours": int(self.horizon_hours),
            "horizon": f"the exit is the open of the bar entry + {int(self.horizon_hours)}h",
            "fee_rate_per_side": float(self.fee_rate),
            "slippage_per_side": float(self.slippage),
            "economics": "CP07 formulas: LONG net = exit_exec*(1-f) / (entry_exec*(1+f)) - 1, slippage moves against the position on both sides",
            "zero_latency_reconciliation": "identical to decision_contract (CP07/CP09): entry open at cutoff, exit open at cutoff + horizon",
        }

    def params_digest(self) -> str:
        import hashlib

        payload = json.dumps(
            {
                "latency_hours": int(self.latency_hours),
                "horizon_hours": int(self.horizon_hours),
                "fee_rate": float(self.fee_rate),
                "slippage": float(self.slippage),
                "schema": EXECUTION_SCHEMA,
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RoundTrip:
    """One resolved LONG round trip with prices, costs and returns."""

    cutoff: pd.Timestamp
    entry_timestamp: pd.Timestamp
    exit_timestamp: pd.Timestamp
    entry_open: float
    exit_open: float
    entry_exec_price: float
    exit_exec_price: float
    fee_rate: float
    slippage: float
    gross_return_pct: float
    net_return_pct: float
    round_trip_cost_pct: float
    latency_hours: int
    horizon_hours: int

    def to_record(self) -> dict[str, Any]:
        record = asdict(self)
        for key in ("cutoff", "entry_timestamp", "exit_timestamp"):
            record[key] = str(record[key])
        return record


def resolve_round_trip(
    market: pd.DataFrame, cutoff: pd.Timestamp, hypothesis: ExecutionHypothesis
) -> RoundTrip:
    """Resolve the economic outcome of a LONG position opened at ``cutoff``.

    ``market`` is an OHLCV frame indexed by UTC hourly bar-open timestamps
    (the normalized :class:`~crypto_decision_lab.dashboard.data_loader.MarketData`
    frame).  The function refuses windows that lack required data: a missing
    entry bar, a missing exit bar or an hourly gap inside the holding period
    raise :class:`ExecutionContractError` instead of producing a target from
    incomplete prices.
    """

    cutoff = utc(cutoff)
    entry_ts = hypothesis.entry_timestamp(cutoff)
    exit_ts = hypothesis.exit_timestamp(cutoff)
    if not isinstance(market.index, pd.DatetimeIndex):
        raise ExecutionContractError("market must be indexed by bar-open timestamps")
    if market.index.tz is None:
        raise ExecutionContractError("market index must be timezone-aware UTC")
    expected = int(hypothesis.horizon_hours) + 1
    if entry_ts not in market.index:
        raise ExecutionContractError(
            f"entry bar missing for cutoff {cutoff}: expected bar at {entry_ts}"
        )
    if exit_ts not in market.index:
        raise ExecutionContractError(
            f"exit bar missing for cutoff {cutoff}: expected bar at {exit_ts}"
        )
    span = market.loc[entry_ts:exit_ts]
    gaps = hypothesis.required_span(cutoff).difference(span.index)
    if len(span) != expected or len(gaps):
        raise ExecutionContractError(
            f"holding period is not {expected} contiguous hourly bars for cutoff {cutoff} "
            f"(got {len(span)} bars, {len(gaps)} missing)"
        )
    entry_open = float(span.loc[entry_ts, "open"])
    exit_open = float(span.loc[exit_ts, "open"])
    costs = hypothesis.cost_model
    entry_exec, exit_exec = execution_prices(entry_open, exit_open, ACTION_LONG, costs)
    gross_pct = 100.0 * gross_return(entry_open, exit_open, ACTION_LONG)
    net_pct = 100.0 * net_return(entry_open, exit_open, ACTION_LONG, costs)
    return RoundTrip(
        cutoff=cutoff,
        entry_timestamp=entry_ts,
        exit_timestamp=exit_ts,
        entry_open=entry_open,
        exit_open=exit_open,
        entry_exec_price=entry_exec,
        exit_exec_price=exit_exec,
        fee_rate=costs.fee_rate,
        slippage=costs.slippage,
        gross_return_pct=gross_pct,
        net_return_pct=net_pct,
        round_trip_cost_pct=gross_pct - net_pct,
        latency_hours=int(hypothesis.latency_hours),
        horizon_hours=int(hypothesis.horizon_hours),
    )


__all__ = [
    "ACTION_FLAT",
    "EXECUTION_SCHEMA",
    "ExecutionContractError",
    "ExecutionHypothesis",
    "RoundTrip",
    "resolve_round_trip",
    "now_iso",
]
