"""Checkpoint reports of the forward paper session E05-P1 (PRE-7).

Turns the criteria of ``config/experiments/E05-P1-paper-forward-v1.json`` into numbers and an
outcome, so the decision is computed rather than read from prose:

* ``week12``: operational criteria (timeliness, no day lost by the runner, fidelity to the engine,
  drawdown) and the stop rule; outcome ``OPERATIONAL_PASS`` / ``OPERATIONAL_FAIL`` / ``FAILED`` /
  ``NOT_DUE`` (fewer than 12 full weeks);
* ``edge``: the edge criteria on the net result of the CLOSED trades (the open position and its
  unrealised result are reported next to it, review R16), the equity drawdown, costs against gross
  on closed trades and the comparison with buy and hold on the session's recorded bars; outcome
  ``CONFIRMED`` / ``NOT_CONFIRMED`` once the session has at least 8 closed trades, ``INCONCLUSIVE``
  after 52 weeks without them, ``NOT_DUE`` otherwise.

The edge is evaluated once: the command writes ``p1-edge-report.json`` (write-once) as soon as it
reaches a decision and refuses to produce a second one.  The trigger depends on the number of closed
trades and the elapsed weeks only, never on the result.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pandas as pd

from . import studio_analytics as analytics
from .execution_contract import HOUR
from .paper import PaperSession
from .session_fidelity import check, run_engine

DAY = pd.Timedelta(days=1)
WEEK = pd.Timedelta(weeks=1)
WEEK12 = 12
EDGE_MIN_CLOSED_TRADES = 8
EDGE_DEADLINE_WEEKS = 52
DRAWDOWN_MIN_PCT = -20.0
TIMELY_HOURS = 2
TIMELY_SHARE_MIN = 0.95
COSTS_SHARE_MAX = 0.5
BUY_AND_HOLD_MARGIN_PP = 10.0


def _utc(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _criterion(text: str, observed: Any, threshold: Any, passed: bool) -> dict:
    return {"criterion": text, "observed": observed, "threshold": threshold, "passed": bool(passed)}


def _elapsed_weeks(session: PaperSession, as_of: pd.Timestamp) -> float:
    return float((as_of - _utc(session.meta["start_at"])) / WEEK)


# ---------------------------------------------------------------- week 12
def timeliness(session: PaperSession, as_of: pd.Timestamp) -> dict:
    """Daily 00:00 UTC decisions committed within :data:`TIMELY_HOURS` hours, from ``status_log``."""
    start = _utc(session.meta["start_at"])
    committed: dict[pd.Timestamp, pd.Timestamp] = {}
    for row in session.status_log():
        detail = row["detail"] or ""
        if detail.startswith("last event "):
            bar = _utc(detail[len("last event "):])
            committed.setdefault(bar, _utc(row["at"]))  # wall clock of the commit
    processed = {_utc(ts) for (ts,) in session.connection.execute("SELECT timestamp FROM events")}
    gaps = [(_utc(r["timestamp"]), _utc(r["resumed_at"])) for r in session.ledger() if r["kind"] == "gap_acknowledged"]
    limit = pd.Timedelta(hours=TIMELY_HOURS)
    expected = [ts for ts in pd.date_range(start.ceil("D"), as_of.floor("D"), freq="1D") if ts + limit <= as_of]
    late, missing, unexplained, delays = [], [], [], []
    for ts in expected:
        if ts not in processed:
            missing.append(str(ts))
            if not any(a <= ts < b for a, b in gaps):
                unexplained.append(str(ts))
            continue
        delay = committed.get(ts)
        hours = float((delay - ts) / HOUR) if delay is not None else None
        delays.append(hours)
        if hours is None or hours > TIMELY_HOURS:
            late.append(str(ts))
    on_time = len(expected) - len(late) - len(missing)
    return {"days_expected": len(expected), "on_time_days": on_time,
            "on_time_share": on_time / len(expected) if expected else None,
            "late_days": late, "missing_days": missing, "unexplained_missing_days": unexplained,
            "max_delay_hours": max((d for d in delays if d is not None), default=None)}


def week12(session_dir: str | Path, *, repo_root: str | Path = ".", as_of=None) -> dict:
    clock = _utc(as_of) if as_of is not None else pd.Timestamp.now(tz="UTC")
    session = PaperSession(Path(session_dir), repo_root=repo_root)
    try:
        weeks = _elapsed_weeks(session, clock)
        punctuality = timeliness(session, clock)
        summary = analytics.summarize(session.ledger(), float(session.meta["config"]["initial_cash"]))
    finally:
        session.close()
    fidelity = check(session_dir, repo_root=repo_root)
    share = punctuality["on_time_share"]
    drawdown = summary["max_drawdown_mtm_pct"]
    criteria = [
        _criterion(f"daily decisions committed within {TIMELY_HOURS} h on at least {TIMELY_SHARE_MIN:.0%} of the days",
                   share, TIMELY_SHARE_MIN, share is not None and share >= TIMELY_SHARE_MIN),
        _criterion("no day without decision because of the runner", len(punctuality["unexplained_missing_days"]), 0,
                   not punctuality["unexplained_missing_days"]),
        _criterion("fidelity to the engine on the session's recorded bars", fidelity["match"], True, fidelity["match"]),
        _criterion(f"max_drawdown_mtm_pct >= {DRAWDOWN_MIN_PCT}", drawdown, DRAWDOWN_MIN_PCT,
                   drawdown >= DRAWDOWN_MIN_PCT),
    ]
    if drawdown < DRAWDOWN_MIN_PCT:
        outcome = "FAILED"  # stop rule, valid at any time
    elif weeks < WEEK12:
        outcome = "NOT_DUE"
    else:
        outcome = "OPERATIONAL_PASS" if all(c["passed"] for c in criteria) else "OPERATIONAL_FAIL"
    return {"checkpoint": "P1.week12", "session_id": session.meta["session_id"], "as_of": str(clock),
            "elapsed_weeks": round(weeks, 2), "outcome": outcome, "criteria": criteria,
            "timeliness": punctuality, "fidelity": {k: fidelity[k] for k in ("match", "events", "first_difference")}}


# ---------------------------------------------------------------- edge
def edge_figures(session: PaperSession) -> dict:
    """Equity and realised figures side by side, the open position, and the buy and hold."""
    initial = float(session.meta["config"]["initial_cash"])
    ledger = session.ledger()
    summary = analytics.summarize(ledger, initial)
    trades = analytics.trades(ledger)
    closed = trades[trades["status"] == "CLOSED"] if not trades.empty else trades
    opened = trades[trades["status"] == "OPEN"] if not trades.empty else trades
    realized = float(closed["pnl"].sum()) if not closed.empty else 0.0
    costs = float(closed["costs"].sum()) if not closed.empty else 0.0
    gross = realized + costs
    hold = copy.deepcopy(session.meta["config"])
    hold["strategy"] = {"name": "constant", "parameters": {"exposure": "LONG"}}
    hold_ledger, _ = run_engine(session, hold, with_features=False)
    hold_summary = analytics.summarize(hold_ledger, initial)
    return {
        "closed_trades": int(len(closed)),
        "equity_net_return_pct": summary["net_return_pct"],
        "realized_net_return_pct": 100.0 * realized / initial,
        "realized_net_pnl": realized, "realized_costs": costs, "realized_gross_pnl": gross,
        "realized_costs_share_of_gross": costs / gross if gross > 0 else None,
        "max_drawdown_mtm_pct": summary["max_drawdown_mtm_pct"],
        "open_position": ({"entry_at": str(opened.iloc[0]["entry_at"]),
                           "unrealized_pnl": float(opened.iloc[0]["pnl"])} if not opened.empty else None),
        "buy_and_hold_net_return_pct": hold_summary["net_return_pct"],
        "last_event": summary["last_event"],
    }


def edge(session_dir: str | Path, *, repo_root: str | Path = ".", as_of=None) -> dict:
    clock = _utc(as_of) if as_of is not None else pd.Timestamp.now(tz="UTC")
    session = PaperSession(Path(session_dir), repo_root=repo_root)
    try:
        weeks = _elapsed_weeks(session, clock)
        figures = edge_figures(session)
        session_id = session.meta["session_id"]
    finally:
        session.close()
    report: dict[str, Any] = {"checkpoint": "P1.edge", "session_id": session_id, "as_of": str(clock),
                              "elapsed_weeks": round(weeks, 2), "figures": figures}
    if figures["closed_trades"] >= EDGE_MIN_CLOSED_TRADES:
        share = figures["realized_costs_share_of_gross"]
        floor = figures["buy_and_hold_net_return_pct"] - BUY_AND_HOLD_MARGIN_PP
        criteria = [
            _criterion("realized_net_return_pct > 0", figures["realized_net_return_pct"], 0,
                       figures["realized_net_return_pct"] > 0),
            _criterion(f"max_drawdown_mtm_pct >= {DRAWDOWN_MIN_PCT} (equity)", figures["max_drawdown_mtm_pct"],
                       DRAWDOWN_MIN_PCT, figures["max_drawdown_mtm_pct"] >= DRAWDOWN_MIN_PCT),
            _criterion(f"realized costs <= {COSTS_SHARE_MAX:.0%} of the realized gross result", share, COSTS_SHARE_MAX,
                       share is not None and share <= COSTS_SHARE_MAX),
            _criterion(f"equity net return >= buy and hold - {BUY_AND_HOLD_MARGIN_PP:g} pp",
                       figures["equity_net_return_pct"], floor, figures["equity_net_return_pct"] >= floor),
        ]
        report.update(criteria=criteria,
                      outcome="CONFIRMED" if all(c["passed"] for c in criteria) else "NOT_CONFIRMED")
    elif weeks >= EDGE_DEADLINE_WEEKS:
        report.update(criteria=[], outcome="INCONCLUSIVE",
                      reason=f"fewer than {EDGE_MIN_CLOSED_TRADES} closed trades after {EDGE_DEADLINE_WEEKS} weeks")
    else:
        report.update(criteria=[], outcome="NOT_DUE",
                      reason=f"{figures['closed_trades']} of {EDGE_MIN_CLOSED_TRADES} closed trades, "
                             f"{weeks:.1f} of {EDGE_DEADLINE_WEEKS} weeks")
    return report


# ---------------------------------------------------------------- CLI
def _write_once(path: Path, report: dict) -> bool:
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(report, indent=2, default=str) + "\n")
        return True
    except FileExistsError:
        return False


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="E05-P1 checkpoint reports")
    parser.add_argument("checkpoint", choices=("week12", "edge"))
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--as-of", help="UTC instant of the evaluation (default: now)")
    args = parser.parse_args(argv)
    directory = Path(args.session_dir)
    target = directory / ("p1-week12-report.json" if args.checkpoint == "week12" else "p1-edge-report.json")
    if args.checkpoint == "edge" and target.exists():
        print(json.dumps({"error": "the edge was already evaluated for this session", "report": str(target)}))
        return 2  # evaluated exactly once
    run = week12 if args.checkpoint == "week12" else edge
    report = run(directory, repo_root=args.repo_root, as_of=args.as_of)
    if report["outcome"] != "NOT_DUE" and not _write_once(target, report):
        report["note"] = f"{target.name} already exists: not overwritten"  # week 12 only; the edge returned above
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["outcome"] in ("NOT_DUE", "OPERATIONAL_PASS", "CONFIRMED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
