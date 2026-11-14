"""S03 deterministic event engine. Input events contain an open and closed-bar features.

The open is observable at timestamp; features are separately available at cutoff.
Strategies never receive the current execution price. Gaps halt the run, duplicates
are idempotent, conflicting duplicates and reordered events are rejected.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
from typing import Callable, Mapping, Protocol
from pathlib import Path

import pandas as pd

from .decision_contract import ACTION_LONG, execution_prices, utc
from .execution_contract import ACTION_FLAT, ExecutionHypothesis, HOUR

ENGINE_VERSION = "cdl.s03.engine.v1"
# Modules whose code determines the economics of a run: event engine, execution
# hypothesis (latency/horizon) and CP07 prices/fees.
ENGINE_MODULES = ("decision_contract.py", "execution_contract.py", "simulation.py")


def engine_digests() -> tuple[str, dict[str, str]]:
    """Checkout-independent identity of the engine revision.

    Each module is hashed with line endings normalized to LF, so a Windows
    (CRLF) and a Linux (LF) checkout of the same commit give the same digest.
    The combined digest covers every module in :data:`ENGINE_MODULES`.
    """
    package = Path(__file__).resolve().parent
    files = {}
    combined = hashlib.sha256()
    for name in ENGINE_MODULES:
        content = (package / name).read_bytes().replace(b"\r\n", b"\n")
        files[name] = hashlib.sha256(content).hexdigest()
        combined.update(f"{name}\0{files[name]}\n".encode())
    return combined.hexdigest(), files


@dataclass(frozen=True)
class MarketEvent:
    timestamp: pd.Timestamp
    open: float
    features: Mapping[str, float] = field(default_factory=dict)
    feature_available_at: pd.Timestamp | None = None

    def record(self):
        timestamp = utc(self.timestamp)
        available = utc(self.feature_available_at) if self.feature_available_at is not None else timestamp
        if pd.isna(timestamp) or pd.isna(available):
            raise ValueError("timestamps must not be missing")
        if available > timestamp:
            raise ValueError("features available after cutoff")
        if not math.isfinite(self.open) or self.open <= 0:
            raise ValueError("open must be finite and positive")
        if any(not math.isfinite(v) for v in self.features.values()):
            raise ValueError("features must be finite")
        return dict(timestamp=str(timestamp), open=float(self.open),
                    features=dict(self.features), feature_available_at=str(available))


@dataclass(frozen=True)
class Portfolio:
    cash: float
    quantity: float = 0.0


@dataclass(frozen=True)
class Proposal:
    exposure: str
    reason: str


class Strategy(Protocol):
    def decide(self, cutoff: pd.Timestamp, features: Mapping[str, float], portfolio: Portfolio) -> Proposal: ...


@dataclass(frozen=True)
class ConstantStrategy:
    exposure: str = ACTION_LONG

    def decide(self, cutoff, features, portfolio):
        return Proposal(self.exposure, f"constant {self.exposure}")


@dataclass(frozen=True)
class MomentumStrategy:
    threshold: float = 0.05
    feature: str = "momentum_24h"

    def __post_init__(self):
        if not math.isfinite(self.threshold):
            raise ValueError("threshold must be finite")

    def decide(self, cutoff, features, portfolio):
        value = features.get(self.feature)
        exposure = ACTION_LONG if value is not None and value > self.threshold else ACTION_FLAT
        return Proposal(exposure, f"{self.feature}={value}; threshold={self.threshold}")


def _scheduled_momentum(**parameters) -> Strategy:
    from .decision_model import ScheduledMomentumStrategy

    return ScheduledMomentumStrategy(**parameters)


def _model_filtered_momentum(**parameters) -> Strategy:
    from .decision_model import ModelFilteredMomentumStrategy

    return ModelFilteredMomentumStrategy(**parameters)


def _pattern(**parameters) -> Strategy:
    from .pattern_detector import PatternStrategy

    return PatternStrategy(**parameters)


STRATEGIES: dict[str, Callable[..., Strategy]] = {
    "constant": ConstantStrategy, "momentum": MomentumStrategy,
    # S05: decide only at decision points (S02 cutoffs); lazy to avoid import cycles.
    "scheduled_momentum": _scheduled_momentum,
    "model_filtered_momentum": _model_filtered_momentum,
    # E02 (draft): patterns in formation; features from pattern_detector.pattern_market_events.
    "pattern": _pattern,
}


def select_strategy(config: dict) -> Strategy:
    try:
        factory = STRATEGIES[config["name"]]
    except KeyError as exc:
        raise ValueError(f"unknown strategy: {config.get('name')}") from exc
    return factory(**config.get("parameters", {}))


@dataclass(frozen=True)
class RiskLimits:
    max_exposure: float = 1.0
    min_notional: float = 1.0
    allow_open: bool = True

    def __post_init__(self):
        if not 0 <= self.max_exposure <= 1 or not math.isfinite(self.min_notional) or self.min_notional < 0:
            raise ValueError("invalid risk limits")


class RiskEngine:
    def __init__(self, limits: RiskLimits, fee_rate: float = 0.0):
        self.limits = limits
        self.fee_rate = fee_rate

    def assess(self, proposal: Proposal, portfolio: Portfolio):
        if proposal.exposure not in (ACTION_LONG, ACTION_FLAT) or not proposal.reason:
            raise ValueError("strategy must propose LONG/FLAT with a reason")
        if proposal.exposure == ACTION_FLAT:
            return True, "exit allowed", 0.0
        budget = portfolio.cash * self.limits.max_exposure
        if not self.limits.allow_open or budget <= 0 or budget / (1 + self.fee_rate) < self.limits.min_notional:
            return False, "opening disabled or insufficient risk budget", 0.0
        return True, "within exposure limit", budget


class SimulatedBroker:
    def __init__(self, hypothesis: ExecutionHypothesis):
        self.hypothesis = hypothesis

    def fill(self, portfolio: Portfolio, side: str, price: float, budget: float):
        buy, sell = execution_prices(price, price, ACTION_LONG, self.hypothesis.cost_model)
        execution = buy if side == "BUY" else sell
        quantity = budget / (execution * (1 + self.hypothesis.fee_rate)) if side == "BUY" else portfolio.quantity
        notional = quantity * execution
        fee = notional * self.hypothesis.fee_rate
        cash = portfolio.cash - budget if side == "BUY" else portfolio.cash + notional - fee
        state = Portfolio(cash, quantity if side == "BUY" else 0.0)
        return state, dict(side=side, quantity=quantity, price=execution, fee=fee,
                           slippage_cost=quantity * abs(execution - price))


@dataclass(frozen=True)
class RunConfig:
    strategy: dict = field(default_factory=lambda: {"name": "constant"})
    initial_cash: float = 10000.0
    hypothesis: ExecutionHypothesis = field(default_factory=ExecutionHypothesis)
    risk: RiskLimits = field(default_factory=RiskLimits)

    def __post_init__(self):
        if not math.isfinite(self.initial_cash) or self.initial_cash <= 0:
            raise ValueError("initial_cash must be finite and positive")
        select_strategy(self.strategy)


@dataclass
class RunResult:
    manifest: dict
    ledger: list[dict]
    portfolio: Portfolio
    status: str

    def canonical_ledger(self) -> str:
        return json.dumps(self.ledger, sort_keys=True, separators=(",", ":"), allow_nan=False)


INPUT_CHAIN_SEED = hashlib.sha256(b"cdl.s03.inputs.v2").hexdigest()


class Engine:
    """Event-by-event transition system shared by backtest, replay and paper sessions.

    ``step`` processes one event and returns the ledger records it produced;
    ``finish`` cancels unfilled orders and writes the ``end`` record. The full
    mutable state is exposed by :meth:`state` as JSON-compatible data, and an
    engine rebuilt from that state continues with exactly the same records as
    an uninterrupted one. ``seen`` maps processed timestamps to their event
    record (duplicate detection); persistent sessions inject a database-backed
    mapping. ``input_digest`` is a resumable hash chain over processed events.
    """

    def __init__(self, config: RunConfig, *, state: dict | None = None, seen=None):
        self.config = config
        self.strategy = select_strategy(config.strategy)
        self.risk = RiskEngine(config.risk, config.hypothesis.fee_rate)
        self.broker = SimulatedBroker(config.hypothesis)
        self.seen = {} if seen is None else seen
        state = state or {}
        # No numeric coercion: the canonical ledger keeps the configured representation.
        self.portfolio = Portfolio(state.get("cash", config.initial_cash), state.get("quantity", 0.0))
        self.previous = utc(state["previous"]) if state.get("previous") else None
        self.clock = utc(state["clock"]) if state.get("clock") else None
        self.pending = dict(state["pending"]) if state.get("pending") else None
        self.exit_at = utc(state["exit_at"]) if state.get("exit_at") else None
        self.last_price = state.get("last_price")
        self.status = state.get("status", "COMPLETED")
        self.input_digest = state.get("input_digest", INPUT_CHAIN_SEED)
        self.sequence = int(state.get("sequence", 0))
        self.finished = bool(state.get("finished", False))
        self._new: list[dict] = []

    def state(self) -> dict:
        return dict(
            cash=self.portfolio.cash, quantity=self.portfolio.quantity,
            previous=str(self.previous) if self.previous is not None else None,
            clock=str(self.clock) if self.clock is not None else None,
            pending=dict(self.pending) if self.pending else None,
            exit_at=str(self.exit_at) if self.exit_at is not None else None,
            last_price=self.last_price, status=self.status, input_digest=self.input_digest,
            sequence=self.sequence, finished=self.finished,
        )

    def _log(self, kind, timestamp, **values):
        self._new.append(dict(sequence=self.sequence, kind=kind, timestamp=str(timestamp), **values))
        self.sequence += 1

    def _drain(self) -> list[dict]:
        records, self._new = self._new, []
        return records

    def _fill(self, order, timestamp, price):
        self.portfolio, fill = self.broker.fill(self.portfolio, order["side"], price, order["budget"])
        self._log("execution", timestamp, order_id=order["order_id"], eligible_at=order["eligible_at"], **fill)
        self.exit_at = (timestamp + int(self.config.hypothesis.horizon_hours) * HOUR
                        if order["side"] == "BUY" else None)

    def step(self, event: MarketEvent) -> list[dict]:
        if self.finished:
            raise ValueError("engine already finished")
        if self.status == "HALTED_GAP":
            raise ValueError("engine halted at a gap")
        record = event.record()
        timestamp = utc(event.timestamp)
        if timestamp in self.seen:
            if self.seen[timestamp] != record:
                raise ValueError("conflicting duplicate event")
            return []
        if self.previous is not None and timestamp < self.previous:
            raise ValueError("events out of order")
        if self.previous is not None and timestamp != self.previous + HOUR:
            self.clock = timestamp
            self._log("gap", timestamp, expected=str(self.previous + HOUR))
            self.status = "HALTED_GAP"
            return self._drain()
        self.seen[timestamp] = record
        self.input_digest = hashlib.sha256(
            (self.input_digest + json.dumps(record, sort_keys=True, allow_nan=False)).encode()
        ).hexdigest()
        self.previous = timestamp
        self.clock = timestamp
        self.last_price = event.open
        self._log("market", timestamp, **{k: v for k, v in record.items() if k != "timestamp"})
        if self.pending and timestamp >= utc(self.pending["eligible_at"]):
            self._fill(self.pending, timestamp, event.open)
            self.pending = None
        closed_by_horizon = False
        if self.portfolio.quantity and self.exit_at is not None and timestamp >= self.exit_at:
            if self.pending is not None:
                self._log("order_cancelled", timestamp, order_id=self.pending["order_id"],
                          reason="superseded by horizon exit")
                self.pending = None
            self._log("decision", timestamp, exposure=ACTION_FLAT, reason="holding horizon reached")
            self._log("risk", timestamp, accepted=True, reason="scheduled exit", budget=0.0)
            order = dict(order_id=f"order-{self.sequence}", side="SELL", budget=0.0, eligible_at=str(timestamp))
            self._log("order", timestamp, **order)
            self._fill(order, timestamp, event.open)
            closed_by_horizon = True
        proposal = self.strategy.decide(timestamp, dict(event.features), self.portfolio)
        self._log("decision", timestamp, exposure=proposal.exposure, reason=proposal.reason)
        accepted, reason, budget = self.risk.assess(proposal, self.portfolio)
        changed = (proposal.exposure == ACTION_LONG) != bool(self.portfolio.quantity)
        if closed_by_horizon:
            accepted, reason = False, "horizon exit: no same-event reopening"
        elif self.pending is not None:
            accepted, reason = False, "order already pending"
        elif not changed:
            accepted, reason = False, "exposure unchanged"
        self._log("risk", timestamp, accepted=accepted, reason=reason, budget=budget)
        if accepted:
            self.pending = dict(order_id=f"order-{self.sequence}",
                                side="BUY" if proposal.exposure == ACTION_LONG else "SELL",
                                budget=budget, eligible_at=str(self.config.hypothesis.entry_timestamp(timestamp)))
            self._log("order", timestamp, **self.pending)
            if utc(self.pending["eligible_at"]) <= timestamp:
                self._fill(self.pending, timestamp, event.open)
                self.pending = None
        self._log("balance", timestamp, **asdict(self.portfolio),
                  equity=self.portfolio.cash + self.portfolio.quantity * event.open)
        return self._drain()

    def acknowledge_gap(self, resume_at, note: str = "") -> list[dict]:
        """Operator-controlled recovery: continue after missing hours (sessions only).

        A pending order is cancelled (its eligibility fell inside the gap); a
        position stays open and is valued only at prices observed after the gap.
        """
        resume_at = utc(resume_at)
        if self.previous is None or resume_at <= self.previous + HOUR:
            raise ValueError("no gap to acknowledge before the resume timestamp")
        expected = self.previous + HOUR
        if self.pending is not None:
            self._log("order_cancelled", expected, order_id=self.pending["order_id"],
                      reason="gap acknowledged before fill")
            self.pending = None
        self._log("gap_acknowledged", expected, resumed_at=str(resume_at),
                  missing_hours=int((resume_at - expected) / HOUR), note=note)
        self.previous = resume_at - HOUR
        self.status = "COMPLETED"
        return self._drain()

    def finish(self) -> list[dict]:
        if self.finished:
            raise ValueError("engine already finished")
        if self.clock is None:
            raise ValueError("run requires at least one market event")
        if self.pending:
            self._log("order_cancelled", self.clock, order_id=self.pending["order_id"], reason="run ended before fill")
            self.pending = None
        self._log("end", self.clock, status=self.status, valuation_timestamp=str(self.previous),
                  **asdict(self.portfolio),
                  equity=self.portfolio.cash + self.portfolio.quantity * (self.last_price or 0),
                  position_open=bool(self.portfolio.quantity))
        self.finished = True
        return self._drain()

    def manifest(self, *, mode: str, source_id: str) -> dict:
        config_record = asdict(self.config)
        encoded = json.dumps(config_record, sort_keys=True, allow_nan=False)
        engine_sha256, engine_files = engine_digests()
        return dict(engine_version=ENGINE_VERSION,
                    engine_sha256=engine_sha256, engine_files=engine_files,
                    mode=mode, source_id=source_id,
                    input_digest=self.input_digest, config=config_record,
                    config_digest=hashlib.sha256(encoded.encode()).hexdigest())


RUN_MODES = ("backtest", "paper-replay", "paper")


def run(events, config: RunConfig, *, mode="backtest", source_id="in-memory-fixture") -> RunResult:
    """Backtest and paper replay drive the same :class:`Engine` transition path.

    Horizon exits are scheduled at fill+horizon (no second latency). A strategy
    FLAT exit has decision latency. No forced terminal fill or invented prices.
    Input events must come from an admitted source; CLI enforces the catalog.
    """
    if mode not in ("backtest", "paper-replay"):
        raise ValueError("mode must be backtest or paper-replay")
    engine = Engine(config)
    ledger: list[dict] = []
    for event in events:
        ledger.extend(engine.step(event))
        if engine.status == "HALTED_GAP":
            break
    ledger.extend(engine.finish())
    return RunResult(engine.manifest(mode=mode, source_id=source_id), ledger, engine.portfolio, engine.status)

def market_events(frame: pd.DataFrame, start=None, end_exclusive=None) -> list[MarketEvent]:
    """Events for a normalized hourly OHLCV frame, optionally restricted to a period.

    ``momentum_24h`` uses only closed bars before each event: close(t-1h) versus
    close(t-25h) on the hourly calendar, so a missing hour yields no feature
    instead of comparing bars across a gap. Bars before ``start`` act as warmup
    only. Shared by the CLI and Strategy Studio (S04).
    """
    close = frame["close"].astype(float)
    hourly = close.asfreq("1h") if len(close) else close
    momentum = hourly.pct_change(24, fill_method=None).shift(1).reindex(frame.index)
    selected = frame
    if start is not None:
        selected = selected[selected.index >= utc(start)]
    if end_exclusive is not None:
        selected = selected[selected.index < utc(end_exclusive)]
    return [MarketEvent(ts, float(row["open"]),
                        {"momentum_24h": float(momentum.loc[ts])} if pd.notna(momentum.loc[ts]) else {})
            for ts, row in selected.iterrows()]


def main(argv=None):
    import argparse
    from pathlib import Path
    from .dataset_catalog import Catalog, load_market
    from .forward_isolation import guard_research_output

    parser = argparse.ArgumentParser(description="S03 event simulation on admitted research snapshots")
    parser.add_argument("--catalog", default="config/dataset-catalog.json")
    parser.add_argument("--source", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--mode", choices=("backtest", "paper-replay"), default="backtest")
    args = parser.parse_args(argv)
    output = Path(args.output_dir)
    guard_research_output(output, purpose="simulation output")
    if output.exists():
        raise FileExistsError(output)
    raw = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    config = RunConfig(**{**raw, "hypothesis": ExecutionHypothesis(**raw.get("hypothesis", {})),
                          "risk": RiskLimits(**raw.get("risk", {}))})
    market = load_market(Catalog.load(args.catalog), args.source, repo_root=args.repo_root)
    # Only previous closed bars supply the momentum signal (fraction).
    events = market_events(market.frame)
    result = run(events, config, mode=args.mode, source_id=args.source)
    result.manifest["source_manifest_sha256"] = market.source_manifest_sha256
    result.manifest["loaded_dataset_id"] = market.dataset_id
    output.mkdir(parents=True, exist_ok=False)
    (output / "manifest.json").write_text(json.dumps(result.manifest, indent=2) + "\n", encoding="utf-8")
    (output / "ledger.json").write_text(result.canonical_ledger() + "\n", encoding="utf-8")
    print(json.dumps(dict(status=result.status, **asdict(result.portfolio))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
