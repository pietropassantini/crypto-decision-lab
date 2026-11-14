"""S09 operational report: observed execution versus simulation hypotheses.

Input is a known register: a live session (``live.sqlite``, S07) or a paper
session (``session.sqlite``, S06).  The report links every order to the
version (config, code/engine digest, release activation), the bar and the
decision that produced it, and its executions, then compares with the
simulation hypotheses of the same configuration:

* **costs**     — fees observed versus ``fee_rate`` × notional;
* **slippage**  — fill VWAP versus the reference open of the decision bar,
  signed so that a positive value is adverse; versus ``slippage``;
* **latency**   — decision bar → order intent → first fill; versus
  ``latency_hours``;
* **fills**     — requested versus executed quantity; rejected, not placed,
  expired and cancelled remainders ("fill mancanti");
* **signals**   — the S03 engine replayed on the same market records with the
  same configuration: agreement of strategy signals and of position state;
* **outcome**   — observed net P&L versus the simulated one, with the cost gap.

Every total is reconciled with the register (``reconciliation``).  The
content is deterministic for a given register: ``content_sha256`` excludes
the generation time.  :func:`hypotheses` turns deviations above the
thresholds below into research hypotheses with a metric and a checkpoint.

When no live session is active the report is exercised on paper sessions and
on **labelled** fixture live sessions (fixture exchange + lab feed):
``data_label`` states it on every report.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import statistics
from typing import Any

import pandas as pd

REPORT_SCHEMA = "cdl.s09.ops-report.v1"
# Deviation thresholds that open a research hypothesis.
SLIPPAGE_RATIO = 1.5          # observed mean adverse slippage > 1.5 × assumed
FILL_RATIO_MIN = 0.99         # executed / requested
LATENCY_MARGIN_S = 300.0      # median decision->intent beyond assumed latency + 5 min
SIGNAL_AGREEMENT_MIN = 0.99
COST_SHARE_MAX = 0.5          # costs above half of the gross result


def _d(value: Any) -> Decimal:
    return Decimal(str(value)) if not isinstance(value, Decimal) else value


def _utc(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": None, "median": None, "max": None, "min": None}
    return {"n": len(values), "mean": statistics.fmean(values), "median": statistics.median(values),
            "max": max(values), "min": min(values)}


def _rows(connection, query: str) -> list[dict]:
    cursor = connection.execute(query)
    names = [c[0] for c in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


# ====================================================================== loading
def load_register(session_dir: str | Path) -> dict:
    directory = Path(session_dir)
    live_db, paper_db = directory / "live.sqlite", directory / "session.sqlite"
    path = live_db if live_db.is_file() else paper_db if paper_db.is_file() else None
    if path is None:
        raise FileNotFoundError(f"no live.sqlite or session.sqlite in {directory}")
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        meta = {row["key"]: json.loads(row["value"]) for row in _rows(connection, "SELECT key, value FROM meta")}
        ledger = [json.loads(r["record"]) for r in _rows(connection, "SELECT record FROM ledger ORDER BY sequence")]
        register = {"kind": "live" if path == live_db else "paper", "meta": meta, "ledger": ledger,
                    "register_sha256": hashlib.sha256(_canonical(ledger).encode()).hexdigest()}
        if register["kind"] == "live":
            register["orders"] = _rows(connection, "SELECT * FROM orders ORDER BY seq")
            register["fills"] = _rows(connection, "SELECT * FROM fills ORDER BY rowid")
            status = _rows(connection, "SELECT status, detail FROM state WHERE id = 1")[0]
        else:
            status = _rows(connection, "SELECT status, detail FROM state WHERE id = 1")[0]
        register["status"] = status
    finally:
        connection.close()
    return register


# ====================================================================== simulation replay (signals)
def _replay(run_config: dict, market: list[dict]) -> list[dict]:
    from .simulation import Engine, MarketEvent
    from .studio import build_run_config

    engine = Engine(build_run_config(run_config))
    ledger: list[dict] = []
    for record in market:
        event = MarketEvent(_utc(record["timestamp"]), float(record["open"]), dict(record.get("features") or {}))
        produced = engine.step(event)
        if engine.status == "HALTED_GAP":
            ledger.extend(produced)
            ledger.extend(engine.acknowledge_gap(event.timestamp, note="report replay"))
            produced = engine.step(event)
        ledger.extend(produced)
    if engine.clock is not None:
        ledger.extend(engine.finish())
    return ledger


def _signals(ledger: list[dict]) -> dict[str, str]:
    """Strategy signal per bar (horizon exits are a separate mechanism)."""
    out = {}
    for record in ledger:
        if record["kind"] == "decision" and record.get("reason") != "holding horizon reached":
            out.setdefault(record["timestamp"], record["exposure"])
    return out


def _positions(ledger: list[dict]) -> dict[str, bool]:
    return {r["timestamp"]: float(r["quantity"]) > 1e-9 for r in ledger if r["kind"] == "balance"}


def _live_positions(register: dict, market: list[dict]) -> dict[str, bool]:
    """Position held after each bar, from executions of orders decided at or before the bar.

    The live ``balance`` record of a bar is written with the order intent,
    before the order is sent, so it lags the fill; the simulation's balance is
    written after the fill.  Dust below ``minQty`` counts as flat.
    """
    rules = register["meta"]["symbol_rules"]
    min_qty = float(rules["min_qty"])
    bar_of = {o["client_order_id"]: _utc(o["bar_ts"]) for o in register["orders"]}
    deltas = []
    for fill in register["fills"]:
        quantity = float(fill["quantity"]) * (1 if fill["side"] == "BUY" else -1)
        if fill["commission_asset"] == rules["base_asset"]:
            quantity -= float(fill["commission"])
        deltas.append((bar_of[fill["client_order_id"]], quantity))
    out = {}
    for record in market:
        stamp = _utc(record["timestamp"])
        out[record["timestamp"]] = sum(q for bar, q in deltas if bar <= stamp) >= min_qty
    return out


# ====================================================================== live report
def _live_orders(register: dict, hypothesis: dict) -> tuple[list[dict], dict]:
    meta = register["meta"]
    rules = meta["symbol_rules"]
    base, quote_asset = rules["base_asset"], rules["quote_asset"]
    opens = {r["timestamp"]: float(r["open"]) for r in register["ledger"] if r["kind"] == "market"}
    open_times = sorted(_utc(t) for t in opens)
    decisions = {}
    for record in register["ledger"]:
        if record["kind"] == "decision":
            decisions[record["timestamp"]] = record  # last decision of the bar
    fills_by_order: dict[str, list[dict]] = {}
    for fill in register["fills"]:
        fills_by_order.setdefault(fill["client_order_id"], []).append(fill)
    orders = []
    for order in register["orders"]:
        request = json.loads(order["request"])
        bar = _utc(order["bar_ts"])
        reference_ts = max((t for t in open_times if t <= bar), default=None)
        reference = opens.get(str(reference_ts)) if reference_ts is not None else None
        fills = fills_by_order.get(order["client_order_id"], [])
        quantity = sum((_d(f["quantity"]) for f in fills), Decimal(0))
        notional = sum((_d(f["price"]) * _d(f["quantity"]) for f in fills), Decimal(0))
        fees = Decimal(0)
        for fill in fills:
            commission = _d(fill["commission"])
            fees += commission if fill["commission_asset"] == quote_asset else \
                commission * _d(fill["price"]) if fill["commission_asset"] == base else Decimal(0)
        vwap = notional / quantity if quantity else None
        sign = 1 if order["side"] == "BUY" else -1
        slippage_bps = float(sign * (vwap / _d(reference) - 1) * 10000) if vwap and reference else None
        if request.get("quantity") is not None:
            requested = _d(request["quantity"])
        elif request.get("quote_quantity") is not None and reference:
            requested = _d(request["quote_quantity"]) / _d(reference)
        else:
            requested = None
        first_fill = min((_utc(f["recorded_at"]) for f in fills), default=None)
        decision = decisions.get(str(bar), {})
        orders.append({
            "client_order_id": order["client_order_id"], "side": order["side"], "purpose": order["purpose"],
            "order_type": request.get("order_type"), "status": order["status"], "bar_ts": str(bar),
            "decision_reason": decision.get("reason"), "reference_open": reference,
            "requested_quantity": float(requested) if requested is not None else None,
            "executed_quantity": float(quantity), "fill_ratio": float(quantity / requested) if requested else None,
            "fills": len(fills), "vwap": float(vwap) if vwap else None, "notional": float(notional),
            "fees_quote": float(fees), "fee_bps": float(fees / notional * 10000) if notional else None,
            "slippage_bps": slippage_bps,
            "slippage_cost_quote": float(sign * (vwap - _d(reference)) * quantity) if vwap and reference else 0.0,
            "decision_latency_s": (_utc(order["created_at"]) - bar).total_seconds(),
            "fill_latency_s": (first_fill - _utc(order["created_at"])).total_seconds() if first_fill else None,
            "detail": order["detail"],
        })
    return orders, opens


def _live_report(register: dict) -> dict:
    meta = register["meta"]
    config = meta["config"]
    run_config = config["run_config"]
    hypothesis = run_config["hypothesis"]
    orders, opens = _live_orders(register, hypothesis)
    ledger = register["ledger"]
    executed = [o for o in orders if o["executed_quantity"] > 0]
    by_status: dict[str, int] = {}
    for order in orders:
        by_status[order["status"]] = by_status.get(order["status"], 0) + 1
    missing = [o for o in orders if o["status"] in ("REJECTED", "NOT_PLACED", "EXPIRED", "CANCELED", "UNKNOWN",
                                                    "INTENT")
               or (o["fill_ratio"] is not None and o["fill_ratio"] < FILL_RATIO_MIN)]
    fees = sum(o["fees_quote"] for o in orders)
    slippage_cost = sum(o["slippage_cost_quote"] for o in orders)
    notional = sum(o["notional"] for o in orders)
    requested_total = sum(o["requested_quantity"] or 0 for o in orders)
    executed_total = sum(o["executed_quantity"] for o in orders)
    # outcome: cash and position from the register's executions (+ operator adjustments)
    initial = float(run_config["initial_cash"])
    cash, position = initial, 0.0
    for record in ledger:
        if record["kind"] == "execution":
            sign = 1 if record["side"] == "BUY" else -1
            cash -= sign * record["quote"]
            position += sign * record["quantity"]
            if record["commission_asset"] == meta["symbol_rules"]["base_asset"]:
                position -= float(record["commission"])
            elif record["commission_asset"] == meta["symbol_rules"]["quote_asset"]:
                cash -= float(record["commission"])
    for asset, amount in (meta.get("adjustments") or {}).items():
        if asset == meta["symbol_rules"]["base_asset"]:
            position += float(amount)
        elif asset == meta["symbol_rules"]["quote_asset"]:
            cash += float(amount)
    market = [r for r in ledger if r["kind"] == "market"]
    last_open = market[-1]["open"] if market else 0.0
    observed_pnl = cash + position * last_open - initial
    simulated = _replay(run_config, market) if market else []
    sim_end = next((r for r in reversed(simulated) if r["kind"] == "end"), None)
    sim_costs = sum(r.get("fee", 0) + r.get("slippage_cost", 0) for r in simulated if r["kind"] == "execution")
    sim_pnl = (sim_end["equity"] - initial) if sim_end else 0.0
    live_signals, sim_signals = _signals(ledger), _signals(simulated)
    common = sorted(set(live_signals) & set(sim_signals))
    disagreements = [{"timestamp": t, "live": live_signals[t], "simulated": sim_signals[t]}
                     for t in common if live_signals[t] != sim_signals[t]]
    live_pos, sim_pos = _live_positions(register, market), _positions(simulated)
    pos_common = sorted(set(live_pos) & set(sim_pos))
    pos_diff = [t for t in pos_common if live_pos[t] != sim_pos[t]]
    guards: dict[str, int] = {}
    for record in ledger:
        if record["kind"] == "guard":
            guards[record["guard"]] = guards.get(record["guard"], 0) + 1
    observed_costs = fees + slippage_cost
    return {
        "orders": orders,
        "fills": {"orders": len(orders), "by_status": by_status, "executed_orders": len(executed),
                  "requested_quantity": requested_total, "executed_quantity": executed_total,
                  "fill_ratio": executed_total / requested_total if requested_total else None,
                  "missing": [{k: o[k] for k in ("client_order_id", "side", "status", "requested_quantity",
                                                 "executed_quantity", "detail")} for o in missing]},
        "costs": {"fees_quote": fees, "notional": notional,
                  "observed_fee_bps": fees / notional * 10000 if notional else None,
                  "assumed_fee_bps": hypothesis["fee_rate"] * 10000},
        "slippage": {"cost_quote": slippage_cost, "bps": _stats([o["slippage_bps"] for o in orders
                                                                   if o["slippage_bps"] is not None]),
                     "assumed_bps": hypothesis["slippage"] * 10000},
        "latency": {"decision_to_intent_s": _stats([o["decision_latency_s"] for o in orders]),
                    "intent_to_first_fill_s": _stats([o["fill_latency_s"] for o in orders
                                                       if o["fill_latency_s"] is not None]),
                    "assumed_s": hypothesis["latency_hours"] * 3600.0},
        "signals": {"bars_compared": len(common), "agreement": 1 - len(disagreements) / len(common) if common else None,
                    "disagreements": disagreements[:20], "position_bars_compared": len(pos_common),
                    "position_agreement": 1 - len(pos_diff) / len(pos_common) if pos_common else None,
                    "position_differences": pos_diff[:20], "guards": guards,
                    "simulated_orders": sum(1 for r in simulated if r["kind"] == "order"),
                    "live_orders": len(orders)},
        "outcome": {"observed_net_pnl": observed_pnl, "simulated_net_pnl": sim_pnl,
                    "difference": observed_pnl - sim_pnl, "observed_costs": observed_costs,
                    "simulated_costs": sim_costs, "observed_gross_pnl": observed_pnl + observed_costs,
                    "position_open": position > 1e-9, "valuation_open": last_open},
        "reconciliation": _reconcile_live(register, orders, fees),
    }


def _reconcile_live(register: dict, orders: list[dict], fees: float) -> list[dict]:
    ledger = register["ledger"]
    executions = [r for r in ledger if r["kind"] == "execution"]
    checks = []
    for order in register["orders"]:
        local = sum((_d(f["quantity"]) for f in register["fills"] if f["client_order_id"] == order["client_order_id"]),
                    Decimal(0))
        checks.append({"check": f"fills = executedQty ({order['client_order_id']})",
                       "ok": local == _d(order["executed_quantity"]) or order["status"] in ("REJECTED", "NOT_PLACED"),
                       "left": str(local), "right": order["executed_quantity"]})
    checks.append({"check": "execution records = fills", "ok": len(executions) == len(register["fills"]),
                   "left": len(executions), "right": len(register["fills"])})
    ledger_quote = sum(r["quote"] for r in executions)
    report_quote = sum(o["notional"] for o in orders)
    checks.append({"check": "notional: report = register", "ok": abs(ledger_quote - report_quote) < 1e-6,
                   "left": report_quote, "right": ledger_quote})
    ledger_fees = sum(r["fee"] for r in executions)
    checks.append({"check": "fees: report = register", "ok": abs(ledger_fees - fees) < 1e-6,
                   "left": fees, "right": ledger_fees})
    trade_ids = [f["trade_id"] for f in register["fills"]]
    checks.append({"check": "trade ids unique", "ok": len(set(trade_ids)) == len(trade_ids),
                   "left": len(set(trade_ids)), "right": len(trade_ids)})
    return checks


# ====================================================================== paper report
def _paper_report(register: dict) -> dict:
    meta, ledger = register["meta"], register["ledger"]
    run_config = meta["config"]
    hypothesis = run_config["hypothesis"]
    opens = {r["timestamp"]: float(r["open"]) for r in ledger if r["kind"] == "market"}
    orders = {r["order_id"]: r for r in ledger if r["kind"] == "order"}
    executions = [r for r in ledger if r["kind"] == "execution"]
    rows, latencies = [], []
    for execution in executions:
        order = orders.get(execution["order_id"], {})
        reference = opens.get(execution["timestamp"])
        sign = 1 if execution["side"] == "BUY" else -1
        rows.append({"order_id": execution["order_id"], "side": execution["side"],
                     "slippage_bps": sign * (execution["price"] / reference - 1) * 10000 if reference else None,
                     "fee": execution["fee"], "notional": execution["quantity"] * execution["price"]})
        if order:
            latencies.append((_utc(execution["timestamp"]) - _utc(order["timestamp"])).total_seconds())
    fees = sum(r["fee"] for r in rows)
    notional = sum(r["notional"] for r in rows)
    slippage_cost = sum(r.get("slippage_cost", 0) for r in executions)
    end = next((r for r in reversed(ledger) if r["kind"] == "end"), None)
    last_balance = next((r for r in reversed(ledger) if r["kind"] in ("end", "balance")), None)
    initial = float(run_config["initial_cash"])
    pnl = (last_balance["equity"] - initial) if last_balance else 0.0
    cancelled = [r for r in ledger if r["kind"] == "order_cancelled"]
    return {
        "orders": rows,
        "fills": {"orders": len(orders), "executed_orders": len(executions),
                  "missing": [{"order_id": r["order_id"], "reason": r["reason"]} for r in cancelled],
                  "fill_ratio": len(executions) / len(orders) if orders else None},
        "costs": {"fees_quote": fees, "notional": notional,
                  "observed_fee_bps": fees / notional * 10000 if notional else None,
                  "assumed_fee_bps": hypothesis["fee_rate"] * 10000},
        "slippage": {"cost_quote": slippage_cost, "bps": _stats([r["slippage_bps"] for r in rows
                                                                   if r["slippage_bps"] is not None]),
                     "assumed_bps": hypothesis["slippage"] * 10000},
        "latency": {"order_to_fill_s": _stats(latencies), "assumed_s": hypothesis["latency_hours"] * 3600.0},
        "signals": {"note": "paper executes the simulation itself: signals agree by construction"},
        "outcome": {"observed_net_pnl": pnl, "observed_costs": fees + slippage_cost,
                    "observed_gross_pnl": pnl + fees + slippage_cost, "status": end["status"] if end else None},
        "reconciliation": [
            {"check": "executions = orders filled or cancelled", "ok": len(executions) + len(cancelled) >= len(orders),
             "left": len(executions) + len(cancelled), "right": len(orders)},
            {"check": "fees: report = register", "ok": abs(fees - sum(r["fee"] for r in executions)) < 1e-9,
             "left": fees, "right": sum(r["fee"] for r in executions)},
        ],
    }


# ====================================================================== report
def build_report(session_dir: str | Path, *, label: str) -> dict:
    """Report of one register.  ``label`` states what the data is (live, paper, fixture)."""
    register = load_register(session_dir)
    meta = register["meta"]
    market = [r for r in register["ledger"] if r["kind"] == "market"]
    if register["kind"] == "live":
        body = _live_report(register)
        version = {"config_sha256": meta["config_sha256"], "run_config_sha256": meta["run_config_sha256"],
                   "code_sha256": meta["code_sha256"], "activation": meta.get("activation"),
                   "exchange": meta["exchange_identity"]}
        feed = meta["source"]["identity"]
        mode = "live" if (meta.get("exchange") or {}).get("name") != "fake" else "live-fixture"
    else:
        body = _paper_report(register)
        version = {"config_sha256": meta["config_sha256"], "engine_sha256": meta["engine_sha256"]}
        feed = meta["source"]["identity"]
        mode = meta["mode"]
    content = {
        "schema_version": REPORT_SCHEMA, "data_label": label, "session_id": meta["session_id"],
        "register": {"kind": register["kind"], "sha256": register["register_sha256"],
                     "records": len(register["ledger"]), "status": register["status"]},
        "mode": mode, "feed": feed, "version": version,
        "period": {"first_bar": market[0]["timestamp"] if market else None,
                   "last_bar": market[-1]["timestamp"] if market else None, "bars": len(market)},
        **body,
    }
    content["hypotheses"] = hypotheses(content)
    report = {"content": content, "content_sha256": hashlib.sha256(_canonical(content).encode()).hexdigest(),
              "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    return report


def hypotheses(content: dict) -> list[dict]:
    """Deviations above the thresholds become research hypotheses with a metric and a checkpoint."""
    found = []
    slip = content["slippage"]
    if slip["bps"]["n"] and slip["bps"]["mean"] is not None and \
            slip["bps"]["mean"] > max(slip["assumed_bps"] * SLIPPAGE_RATIO, 0.5):
        found.append({"id": "H-slippage", "observation":
                      f"mean adverse slippage {slip['bps']['mean']:.2f} bps vs assumed {slip['assumed_bps']:.2f} bps",
                      "hypothesis": "the simulation underestimates execution cost; a slippage model calibrated on "
                                    "observed fills changes the ranking of candidates",
                      "metric": "net_return_pct with calibrated slippage vs assumed slippage",
                      "checkpoint": "re-run the candidate backtest with slippage = observed p75; accept the model "
                                    "if it reproduces observed live/paper cost within 10%"})
    fills = content["fills"]
    live = content["register"]["kind"] == "live"  # paper cancellations are engine semantics, not missing fills
    if live and ((fills.get("fill_ratio") is not None and fills["fill_ratio"] < FILL_RATIO_MIN) or fills.get("missing")):
        found.append({"id": "H-fills", "observation": f"{len(fills.get('missing', []))} orders with missing "
                                                      f"quantity; fill ratio {fills.get('fill_ratio')}",
                      "hypothesis": "unfilled or rejected orders bias the outcome against the simulation, which "
                                    "fills every eligible order",
                      "metric": "share of decisions executed in full; P&L difference attributable to missing fills",
                      "checkpoint": "classify every missing fill by cause (reject, timeout, expiry, cancel) and "
                                    "add the dominant cause to the simulated broker"})
    latency = content["latency"]
    observed = (latency.get("decision_to_intent_s") or latency.get("order_to_fill_s") or {}).get("median")
    if observed is not None and observed > latency["assumed_s"] + LATENCY_MARGIN_S:
        found.append({"id": "H-latency", "observation": f"median decision latency {observed:.0f}s vs assumed "
                                                        f"{latency['assumed_s']:.0f}s",
                      "hypothesis": "signals act later than the simulation assumes; returns at the observed "
                                    "latency differ from the assumed one",
                      "metric": "net_return_pct at latency_hours = ceil(observed) vs assumed",
                      "checkpoint": "backtest at the observed latency; reject candidates whose net return "
                                    "drops below the acceptance criterion"})
    signals = content["signals"]
    if signals.get("agreement") is not None and signals["agreement"] < SIGNAL_AGREEMENT_MIN:
        found.append({"id": "H-signals", "observation": f"signal agreement {signals['agreement']:.3f} over "
                                                        f"{signals['bars_compared']} bars",
                      "hypothesis": "live features differ from the research features (feed, warmup, gaps)",
                      "metric": "signal agreement live vs replay",
                      "checkpoint": "diff the feature values of disagreeing bars and fix the feed or warmup"})
    outcome = content["outcome"]
    gross, costs = outcome.get("observed_gross_pnl"), outcome.get("observed_costs")
    if gross is not None and costs and (gross <= 0 < costs or costs > COST_SHARE_MAX * abs(gross)):
        found.append({"id": "H-turnover", "observation": f"costs {costs:.2f} vs gross result {gross:.2f}",
                      "hypothesis": "turnover consumes the gross edge; fewer, longer trades keep a positive "
                                    "net result",
                      "metric": "net_return_pct and costs / gross on the same window",
                      "checkpoint": "preregistered turnover experiment (S09-E01)"})
    return found


def render(report: dict) -> str:
    c = report["content"]
    lines = [f"# Report operativo {c['session_id']}", "",
             f"**Dati: {c['data_label']}** · modalità `{c['mode']}` · registro `{c['register']['sha256'][:12]}` "
             f"({c['register']['records']} record) · contenuto `{report['content_sha256'][:12]}`", "",
             "## Versione e feed", "", f"- Versione: `{json.dumps(c['version'], default=str)}`",
             f"- Feed: `{json.dumps(c['feed'], default=str)}`",
             f"- Periodo: {c['period']['first_bar']} → {c['period']['last_bar']} ({c['period']['bars']} barre)", "",
             "## Osservato e ipotesi", "", "| Voce | Osservato | Ipotesi |", "|---|---|---|"]
    costs, slip, lat = c["costs"], c["slippage"], c["latency"]
    fmt = lambda v, p=2: "n/d" if v is None else f"{v:.{p}f}"  # noqa: E731
    lines.append(f"| Commissioni (bps del nozionale) | {fmt(costs['observed_fee_bps'])} | {fmt(costs['assumed_fee_bps'])} |")
    lines.append(f"| Slippage medio (bps, + = avverso) | {fmt(slip['bps']['mean'])} | {fmt(slip['assumed_bps'])} |")
    key = "decision_to_intent_s" if "decision_to_intent_s" in lat else "order_to_fill_s"
    lines.append(f"| Latenza mediana (s) | {fmt(lat[key]['median'], 0)} | {fmt(lat['assumed_s'], 0)} |")
    lines.append(f"| Fill ratio | {fmt(c['fills'].get('fill_ratio'), 3)} | 1.000 |")
    if c["signals"].get("agreement") is not None:
        lines.append(f"| Accordo segnali | {fmt(c['signals']['agreement'], 3)} | 1.000 |")
        lines.append(f"| Accordo posizione | {fmt(c['signals']['position_agreement'], 3)} | 1.000 |")
    out = c["outcome"]
    lines += ["", "## Esito", "",
              f"- P&L netto osservato {fmt(out['observed_net_pnl'])}; costi osservati {fmt(out['observed_costs'])}; "
              f"lordo {fmt(out['observed_gross_pnl'])}"]
    if "simulated_net_pnl" in out:
        lines.append(f"- P&L simulato sugli stessi eventi {fmt(out['simulated_net_pnl'])} (costi "
                     f"{fmt(out['simulated_costs'])}); differenza {fmt(out['difference'])}")
    if c["fills"].get("missing"):
        lines += ["", "## Fill mancanti", ""] + [f"- `{json.dumps(m, default=str)}`" for m in c["fills"]["missing"]]
    lines += ["", "## Riconciliazione", ""] + [f"- {'OK' if r['ok'] else 'KO'} {r['check']}: {r['left']} / {r['right']}"
                                               for r in c["reconciliation"]]
    lines += ["", "## Ipotesi dagli scostamenti", ""]
    lines += [f"- **{h['id']}**: {h['observation']}. {h['hypothesis']}. Metrica: {h['metric']}. "
              f"Checkpoint: {h['checkpoint']}" for h in c["hypotheses"]] or ["- Nessuno scostamento oltre le soglie"]
    return "\n".join(lines) + "\n"


# ====================================================================== labelled fixture
def build_fixture_session(repo_root: str | Path, workdir: str | Path, *, live_config: dict,
                          dataset_id: str = "BTCUSDT-1h-raw-v1", feed_start: str = "2024-03-01 00:00",
                          feed_end: str = "2024-03-04 00:00", adverse_bps: str = "10") -> Path:
    """Labelled fixture live session with known deviations.

    Fixture exchange with adverse MARKET slippage, one rejected order, one
    timeout reconciled later and one partially expired order; lab feed with
    one late bar.  Used when no live session exists (PRD S09).
    """
    from .exchange import FakeExchange
    from .lab_feed import FEED_SCHEMA, LabFeed
    from .live import LiveSession

    repo, work = Path(repo_root).resolve(), Path(workdir).resolve()
    start = _utc(feed_start) + pd.Timedelta(hours=26)
    feed = LabFeed.create(work / "feed", {
        "schema_version": FEED_SCHEMA, "feed_id": "s09-fixture", "source_dataset_id": dataset_id,
        "start": feed_start, "end": feed_end,
        "faults": [{"type": "late", "at": str(start + pd.Timedelta(hours=5)), "delay_hours": 2}]}, repo_root=repo)
    feed.advance(until=start - pd.Timedelta(hours=1))
    fake = FakeExchange(work / "exchange.json", balances={"BTC": "0", "USDT": "5000"}, price=1)
    fake.set_adverse_bps(adverse_bps)
    fake.push({"reject": -2010, "msg": "Account has insufficient balance for requested action."},
              {"timeout": "after"}, {}, {"partial": "0.6"})
    session = LiveSession.create("s09-fixture", live_config, exchange={"name": "fake", "state_path": str(fake.path)},
                                 source={"kind": "lab_feed", "path": str(feed.directory)}, start_at=start,
                                 root=work / "live", repo_root=repo, adapter=fake, feed=feed)
    end = _utc(feed_end) - pd.Timedelta(hours=1)
    while feed.now() < end:
        feed.advance(1)
        session.poll()
    session.cancel_open_orders("fixture end")
    directory = session.directory
    session.close()
    return directory


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="S09 operational report")
    parser.add_argument("--repo-root", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    report = sub.add_parser("report")
    report.add_argument("--session-dir", required=True)
    report.add_argument("--label", required=True, help="what the data is: live, paper, fixture-live ...")
    report.add_argument("--out", required=True)
    fixture = sub.add_parser("fixture")
    fixture.add_argument("--config", required=True)
    fixture.add_argument("--workdir", required=True)
    args = parser.parse_args(argv)
    repo = Path(args.repo_root).resolve()
    if args.command == "fixture":
        config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
        print(build_fixture_session(repo, args.workdir, live_config=config))
        return 0
    result = build_report(args.session_dir, label=args.label)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(result, indent=2, sort_keys=True, default=str) + "\n",
                                     encoding="utf-8")
    (out / "report.md").write_text(render(result), encoding="utf-8")
    c = result["content"]
    print(json.dumps({"content_sha256": result["content_sha256"], "hypotheses": [h["id"] for h in c["hypotheses"]],
                      "reconciled": all(r["ok"] for r in c["reconciliation"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
