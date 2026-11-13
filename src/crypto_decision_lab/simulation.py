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


STRATEGIES: dict[str, Callable[..., Strategy]] = {
    "constant": ConstantStrategy, "momentum": MomentumStrategy,
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


def run(events, config: RunConfig, *, mode="backtest", source_id="in-memory-fixture") -> RunResult:
    """Backtest and paper replay use exactly the same clock and transition path.

    Horizon exits are scheduled at fill+horizon (no second latency). A strategy
    FLAT exit has decision latency. No forced terminal fill or invented prices.
    Input events must come from an admitted source; CLI enforces the catalog.
    """
    if mode not in ("backtest", "paper-replay"):
        raise ValueError("mode must be backtest or paper-replay")
    strategy = select_strategy(config.strategy)
    risk, broker = RiskEngine(config.risk, config.hypothesis.fee_rate), SimulatedBroker(config.hypothesis)
    portfolio = Portfolio(config.initial_cash)
    ledger, seen = [], {}
    previous = None
    clock = None
    pending = None
    exit_at = None
    last_price = None
    status = "COMPLETED"
    inputs = hashlib.sha256()

    def log(kind, timestamp, **values):
        ledger.append(dict(sequence=len(ledger), kind=kind, timestamp=str(timestamp), **values))

    for event in events:
        record = event.record()
        timestamp = utc(event.timestamp)
        if timestamp in seen:
            if seen[timestamp] != record:
                raise ValueError("conflicting duplicate event")
            continue
        if previous is not None and timestamp < previous:
            raise ValueError("events out of order")
        if previous is not None and timestamp != previous + HOUR:
            clock = timestamp
            log("gap", timestamp, expected=str(previous + HOUR))
            status = "HALTED_GAP"
            break
        seen[timestamp] = record
        inputs.update(json.dumps(record, sort_keys=True, allow_nan=False).encode())
        previous = timestamp
        clock = timestamp
        last_price = event.open
        log("market", timestamp, **{k: v for k, v in record.items() if k != "timestamp"})

        def fill_order(order):
            nonlocal portfolio, exit_at
            portfolio, fill = broker.fill(portfolio, order["side"], event.open, order["budget"])
            log("execution", timestamp, order_id=order["order_id"], eligible_at=order["eligible_at"], **fill)
            exit_at = timestamp + int(config.hypothesis.horizon_hours) * HOUR if order["side"] == "BUY" else None

        if pending and timestamp >= utc(pending["eligible_at"]):
            fill_order(pending)
            pending = None
        closed_by_horizon = False
        if portfolio.quantity and exit_at is not None and timestamp >= exit_at:
            if pending is not None:
                log("order_cancelled", timestamp, order_id=pending["order_id"], reason="superseded by horizon exit")
                pending = None
            log("decision", timestamp, exposure=ACTION_FLAT, reason="holding horizon reached")
            log("risk", timestamp, accepted=True, reason="scheduled exit", budget=0.0)
            order = dict(order_id=f"order-{len(ledger)}", side="SELL", budget=0.0, eligible_at=str(timestamp))
            log("order", timestamp, **order)
            fill_order(order)
            closed_by_horizon = True
        proposal = strategy.decide(timestamp, dict(event.features), portfolio)
        log("decision", timestamp, exposure=proposal.exposure, reason=proposal.reason)
        accepted, reason, budget = risk.assess(proposal, portfolio)
        changed = (proposal.exposure == ACTION_LONG) != bool(portfolio.quantity)
        if closed_by_horizon:
            accepted, reason = False, "horizon exit: no same-event reopening"
        elif pending is not None:
            accepted, reason = False, "order already pending"
        elif not changed:
            accepted, reason = False, "exposure unchanged"
        log("risk", timestamp, accepted=accepted, reason=reason, budget=budget)
        if accepted:
            pending = dict(order_id=f"order-{len(ledger)}", side="BUY" if proposal.exposure == ACTION_LONG else "SELL",
                           budget=budget, eligible_at=str(config.hypothesis.entry_timestamp(timestamp)))
            log("order", timestamp, **pending)
            if utc(pending["eligible_at"]) <= timestamp:
                fill_order(pending)
                pending = None
        log("balance", timestamp, **asdict(portfolio), equity=portfolio.cash + portfolio.quantity * event.open)
    if clock is None:
        raise ValueError("run requires at least one market event")
    if pending:
        log("order_cancelled", clock, order_id=pending["order_id"], reason="run ended before fill")
    log("end", clock, status=status, valuation_timestamp=str(previous), **asdict(portfolio),
        equity=portfolio.cash + portfolio.quantity * (last_price or 0), position_open=bool(portfolio.quantity))
    config_record = asdict(config)
    encoded = json.dumps(config_record, sort_keys=True, allow_nan=False)
    engine_sha256, engine_files = engine_digests()
    manifest = dict(engine_version=ENGINE_VERSION,
                    engine_sha256=engine_sha256, engine_files=engine_files,
                    mode=mode, source_id=source_id,
                    input_digest=inputs.hexdigest(), config=config_record,
                    config_digest=hashlib.sha256(encoded.encode()).hexdigest())
    return RunResult(manifest, ledger, portfolio, status)


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
