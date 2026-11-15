"""Checkpoint reports of the forward paper session E05-P1 (PRE-7).

Turns the criteria of ``config/experiments/E05-P1-paper-forward-v1.json`` into numbers and an
outcome, so the decision is computed rather than read from prose:

* ``week12``: operational criteria (timeliness, no day lost by the runner, fidelity to the engine,
  drawdown); outcome ``OPERATIONAL_PASS`` / ``OPERATIONAL_FAIL`` / ``NOT_DUE`` (fewer than 12 full weeks);
* ``edge``: the edge criteria on the net result of the CLOSED trades (the open position and its
  unrealised result are reported next to it), the equity drawdown, costs against gross on closed
  trades and the comparison with buy and hold on the session's recorded bars; outcome ``CONFIRMED`` /
  ``NOT_CONFIRMED`` once the session has at least 8 closed trades, ``INCONCLUSIVE`` after 52 weeks
  without them, ``NOT_DUE`` otherwise.

Both reports also apply the stop rules at any time: ``INVALID`` when the session is not the frozen
candidate of the spec (digest, features and, in production, a live ``public_klines`` session) and
``FAILED`` when the equity drawdown goes under the limit.

Guarantees against waiting for a better figure and against a second look:

* the edge is computed on the session **up to the trigger Monday 00:00 UTC** (the first Monday at or
  after the close of the 8th closed trade), whenever the tool is run;
* ``--as-of`` is a dry run: it never writes anything;
* a production decision is recorded in the session database and in a file; a second edge decision
  is refused (exit code 2) even if the file is deleted; the week-12 outcomes are recorded once each.
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
from .studio import config_digest, normalize_config

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
DEFAULT_SPEC = Path("config/experiments/E05-P1-paper-forward-v1.json")


class ReportError(RuntimeError):
    """The spec or the session cannot be reported on."""


def _utc(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC")


def _criterion(text: str, observed: Any, threshold: Any, passed: bool) -> dict:
    return {"criterion": text, "observed": observed, "threshold": threshold, "passed": bool(passed)}


# ---------------------------------------------------------------- spec and candidate pin
def load_spec(path: str | Path | None = None, *, repo_root: str | Path = ".") -> dict:
    target = Path(path) if path is not None else Path(repo_root) / DEFAULT_SPEC
    spec = json.loads(target.read_text(encoding="utf-8-sig"))
    candidate_pin(spec)  # a spec that contradicts its own candidate block is refused
    return spec


def candidate_pin(spec: dict) -> tuple[str, dict]:
    """The frozen candidate: its configuration digest and feature parameters, checked against the spec."""
    block = spec["candidate"]
    config = {"strategy": block["strategy"], "initial_cash": block["initial_cash"],
              "hypothesis": block["hypothesis"], "risk": block["risk"]}
    digest = config_digest(normalize_config(config))
    if block.get("config_sha256") != digest:
        raise ReportError(f"spec candidate.config_sha256 {block.get('config_sha256')} does not match its own "
                          f"candidate block ({digest})")
    return digest, dict(block["features"])


def invalid_reasons(session: PaperSession, spec: dict, *, production: bool) -> list[str]:
    """Why this session is not the frozen candidate of the spec (stop rule INVALID)."""
    digest, features = candidate_pin(spec)
    reasons = []
    if session.meta["config_sha256"] != digest:
        reasons.append(f"configuration {session.meta['config_sha256'][:12]} differs from the frozen candidate {digest[:12]}")
    if session.meta.get("features") != features:
        reasons.append(f"feature parameters {session.meta.get('features')} differ from the frozen {features}")
    if production:
        kind = session.meta["source"].get("kind")
        wanted = spec.get("data", {}).get("source_kind", "public_klines")
        if kind != wanted:
            reasons.append(f"not a live paper session on {wanted} (source {kind}, mode {session.meta['mode']})")
    return reasons


# ---------------------------------------------------------------- decisions recorded in the session
_TABLE = ("CREATE TABLE IF NOT EXISTS p1_decisions(checkpoint TEXT NOT NULL, outcome TEXT NOT NULL, "
          "written_at TEXT NOT NULL, report TEXT NOT NULL, PRIMARY KEY (checkpoint, outcome))")


def _decided(directory: Path, checkpoint: str, repo_root) -> bool:
    session = PaperSession(directory, repo_root=repo_root)
    try:
        session.connection.execute(_TABLE)
        return session.connection.execute("SELECT 1 FROM p1_decisions WHERE checkpoint = ?",
                                          (checkpoint,)).fetchone() is not None
    finally:
        session.close()


def _record(directory: Path, checkpoint: str, report: dict, file: Path, repo_root) -> bool:
    """Record a decision in the session database and in ``file``; False if this outcome is already recorded."""
    session = PaperSession(directory, repo_root=repo_root)
    try:
        session.connection.execute(_TABLE)
        inserted = session.connection.execute(
            "INSERT OR IGNORE INTO p1_decisions(checkpoint, outcome, written_at, report) VALUES (?, ?, ?, ?)",
            (checkpoint, report["outcome"], str(_now()), json.dumps(report, default=str))).rowcount == 1
    finally:
        session.close()
    if inserted and not file.exists():
        with file.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(report, indent=2, default=str) + "\n")
    return inserted


# ---------------------------------------------------------------- week 12
def timeliness(session: PaperSession, as_of: pd.Timestamp) -> dict:
    """Daily 00:00 UTC decisions committed within :data:`TIMELY_HOURS` hours, from ``status_log``.

    A day whose 00:00 bar is missing because of an acknowledged market-data gap is excused: it is not
    in the count (the runner did nothing wrong).  A day the runner never processed counts as not on time.
    """
    start = _utc(session.meta["start_at"])
    committed: dict[pd.Timestamp, pd.Timestamp] = {}
    for row in session.status_log():
        detail = row["detail"] or ""
        if detail.startswith("last event "):
            committed.setdefault(_utc(detail[len("last event "):]), _utc(row["at"]))  # wall clock of the commit
    processed = {_utc(ts) for (ts,) in session.connection.execute("SELECT timestamp FROM events")}
    gaps = [(_utc(r["timestamp"]), _utc(r["resumed_at"])) for r in session.ledger() if r["kind"] == "gap_acknowledged"]
    limit = pd.Timedelta(hours=TIMELY_HOURS)
    due = [ts for ts in pd.date_range(start.ceil("D"), as_of.floor("D"), freq="1D") if ts + limit <= as_of]
    late, missing, unexplained, excused, delays = [], [], [], [], []
    for ts in due:
        if ts not in processed:
            missing.append(str(ts))
            (excused if any(a <= ts < b for a, b in gaps) else unexplained).append(str(ts))
            continue
        moment = committed.get(ts)
        hours = float((moment - ts) / HOUR) if moment is not None else None
        delays.append(hours)
        if hours is None or hours > TIMELY_HOURS:
            late.append(str(ts))
    counted = len(due) - len(excused)
    on_time = counted - len(late) - len(unexplained)
    return {"days_due": len(due), "days_excused_by_gap": len(excused), "days_expected": counted,
            "on_time_days": on_time, "on_time_share": on_time / counted if counted else None,
            "late_days": late, "missing_days": missing, "unexplained_missing_days": unexplained,
            "excused_days": excused, "max_delay_hours": max((d for d in delays if d is not None), default=None)}


def _stop_outcome(invalid: list[str], drawdown: float) -> str | None:
    if invalid:
        return "INVALID"
    if drawdown < DRAWDOWN_MIN_PCT:
        return "FAILED"
    return None


def week12(session_dir: str | Path, *, repo_root: str | Path = ".", as_of=None, spec: dict | None = None) -> dict:
    production = as_of is None
    clock = _now() if production else _utc(as_of)
    spec = spec or load_spec(repo_root=repo_root)
    session = PaperSession(Path(session_dir), repo_root=repo_root)
    try:
        weeks = float((clock - _utc(session.meta["start_at"])) / WEEK)
        punctuality = timeliness(session, clock)
        summary = analytics.summarize(session.ledger(), float(session.meta["config"]["initial_cash"]))
        invalid = invalid_reasons(session, spec, production=production)
        session_id, mode = session.meta["session_id"], session.meta["mode"]
    finally:
        session.close()
    fidelity = check(session_dir, repo_root=repo_root)
    share, drawdown = punctuality["on_time_share"], summary["max_drawdown_mtm_pct"]
    criteria = [
        _criterion(f"daily decisions committed within {TIMELY_HOURS} h on at least {TIMELY_SHARE_MIN:.0%} of the "
                   "counted days", share, TIMELY_SHARE_MIN, share is not None and share >= TIMELY_SHARE_MIN),
        _criterion("no day without decision because of the runner", len(punctuality["unexplained_missing_days"]), 0,
                   not punctuality["unexplained_missing_days"]),
        _criterion("fidelity to the engine on the session's recorded bars", fidelity["match"], True, fidelity["match"]),
        _criterion(f"max_drawdown_mtm_pct >= {DRAWDOWN_MIN_PCT}", drawdown, DRAWDOWN_MIN_PCT,
                   drawdown >= DRAWDOWN_MIN_PCT),
    ]
    outcome = _stop_outcome(invalid, drawdown)
    if outcome is None:
        if weeks < WEEK12:
            outcome = "NOT_DUE"
        else:
            outcome = "OPERATIONAL_PASS" if all(c["passed"] for c in criteria) else "OPERATIONAL_FAIL"
    return {"checkpoint": "P1.week12", "session_id": session_id, "mode": mode, "as_of": str(clock),
            "dry_run": not production, "elapsed_weeks": round(weeks, 2), "outcome": outcome,
            "invalid_reasons": invalid, "criteria": criteria, "timeliness": punctuality,
            "fidelity": {k: fidelity[k] for k in ("match", "events", "first_difference")}}


# ---------------------------------------------------------------- edge
def _until(ledger: list[dict], instant: pd.Timestamp | None) -> list[dict]:
    if instant is None:
        return ledger
    return [r for r in ledger if _utc(r["timestamp"]) <= instant]


def first_monday_at_or_after(moment: pd.Timestamp) -> pd.Timestamp:
    """First Monday 00:00 UTC at or after ``moment``."""
    day = moment.normalize()
    day = day + ((0 - day.weekday()) % 7) * DAY
    return day if day >= moment else day + WEEK


def trigger(ledger: list[dict]) -> dict | None:
    """The edge trigger: the close of the 8th closed trade and the first Monday 00:00 UTC at or after it."""
    trades = analytics.trades(ledger)
    closed = trades[trades["status"] == "CLOSED"] if not trades.empty else trades
    if len(closed) < EDGE_MIN_CLOSED_TRADES:
        return None
    exit_at = pd.Timestamp(closed.sort_values("exit_at").iloc[EDGE_MIN_CLOSED_TRADES - 1]["exit_at"])
    return {"eighth_trade_closed_at": str(exit_at), "due_at": first_monday_at_or_after(exit_at)}


def edge_figures(session: PaperSession, until: pd.Timestamp | None = None) -> dict:
    """Equity and realised figures side by side, the open position, and the buy and hold (up to ``until``)."""
    initial = float(session.meta["config"]["initial_cash"])
    ledger = _until(session.ledger(), until)
    summary = analytics.summarize(ledger, initial)
    trades = analytics.trades(ledger)
    closed = trades[trades["status"] == "CLOSED"] if not trades.empty else trades
    opened = trades[trades["status"] == "OPEN"] if not trades.empty else trades
    realized = float(closed["pnl"].sum()) if not closed.empty else 0.0
    costs = float(closed["costs"].sum()) if not closed.empty else 0.0
    gross = realized + costs
    hold = copy.deepcopy(session.meta["config"])
    hold["strategy"] = {"name": "constant", "parameters": {"exposure": "LONG"}}
    hold_ledger, _ = run_engine(session, hold, with_features=False, until=until)
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


def edge(session_dir: str | Path, *, repo_root: str | Path = ".", as_of=None, spec: dict | None = None) -> dict:
    production = as_of is None
    clock = _now() if production else _utc(as_of)
    spec = spec or load_spec(repo_root=repo_root)
    session = PaperSession(Path(session_dir), repo_root=repo_root)
    try:
        weeks = float((clock - _utc(session.meta["start_at"])) / WEEK)
        invalid = invalid_reasons(session, spec, production=production)
        fired = trigger(session.ledger())
        until = fired["due_at"] if fired is not None and fired["due_at"] <= clock else None
        figures = edge_figures(session, until)
        session_id, mode = session.meta["session_id"], session.meta["mode"]
    finally:
        session.close()
    fidelity = check(session_dir, repo_root=repo_root)
    if not fidelity["match"]:
        invalid.append("the session ledger is not what the engine produces on the session's own recorded bars "
                       f"(first difference at record {fidelity['first_difference']})")
    report: dict[str, Any] = {"checkpoint": "P1.edge", "session_id": session_id, "mode": mode, "as_of": str(clock),
                              "dry_run": not production, "elapsed_weeks": round(weeks, 2), "figures": figures,
                              "invalid_reasons": invalid, "criteria": []}
    if fired is not None:
        report["trigger"] = {"eighth_trade_closed_at": fired["eighth_trade_closed_at"], "due_at": str(fired["due_at"]),
                             "evaluated_as_of": str(until) if until is not None else None,
                             "delay_days": round(float((clock - fired["due_at"]) / DAY), 2)}
    stop = _stop_outcome(invalid, figures["max_drawdown_mtm_pct"])
    if stop is not None:
        report.update(outcome=stop, reason="stop rule")
    elif fired is not None and until is not None:
        share = figures["realized_costs_share_of_gross"]
        floor = figures["buy_and_hold_net_return_pct"] - BUY_AND_HOLD_MARGIN_PP
        criteria = [
            _criterion("realized_net_return_pct > 0", figures["realized_net_return_pct"], 0,
                       figures["realized_net_return_pct"] > 0),
            _criterion(f"max_drawdown_mtm_pct >= {DRAWDOWN_MIN_PCT} (equity)", figures["max_drawdown_mtm_pct"],
                       DRAWDOWN_MIN_PCT, figures["max_drawdown_mtm_pct"] >= DRAWDOWN_MIN_PCT),
            _criterion(f"realized costs <= {COSTS_SHARE_MAX:.0%} of the realized gross result (undefined share fails)",
                       share, COSTS_SHARE_MAX, share is not None and share <= COSTS_SHARE_MAX),
            _criterion(f"equity net return >= buy and hold - {BUY_AND_HOLD_MARGIN_PP:g} pp",
                       figures["equity_net_return_pct"], floor, figures["equity_net_return_pct"] >= floor),
        ]
        report.update(criteria=criteria,
                      outcome="CONFIRMED" if all(c["passed"] for c in criteria) else "NOT_CONFIRMED")
    elif fired is not None:
        report.update(outcome="NOT_DUE", reason=f"8 closed trades reached at {fired['eighth_trade_closed_at']}; "
                                                f"the evaluation is due at the Monday {fired['due_at']}")
    elif weeks >= EDGE_DEADLINE_WEEKS:
        report.update(outcome="INCONCLUSIVE",
                      reason=f"fewer than {EDGE_MIN_CLOSED_TRADES} closed trades after {EDGE_DEADLINE_WEEKS} weeks")
    else:
        report.update(outcome="NOT_DUE", reason=f"{figures['closed_trades']} of {EDGE_MIN_CLOSED_TRADES} closed "
                                                f"trades, {weeks:.1f} of {EDGE_DEADLINE_WEEKS} weeks")
    return report


# ---------------------------------------------------------------- CLI
def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="E05-P1 checkpoint reports")
    parser.add_argument("checkpoint", choices=("week12", "edge"))
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--spec", help=f"specification file (default: {DEFAULT_SPEC})")
    parser.add_argument("--as-of", help="DRY RUN at this UTC instant: nothing is written")
    args = parser.parse_args(argv)
    directory = Path(args.session_dir)
    spec = load_spec(args.spec, repo_root=args.repo_root)
    dry = args.as_of is not None
    if args.checkpoint == "edge" and not dry and (_decided(directory, "edge", args.repo_root)
                                                  or (directory / "p1-edge-report.json").exists()):
        print(json.dumps({"error": "the edge was already evaluated for this session"}))
        return 2  # evaluated exactly once, recorded in the session as well as in the file
    run = week12 if args.checkpoint == "week12" else edge
    report = run(directory, repo_root=args.repo_root, as_of=args.as_of, spec=spec)
    if not dry and report["outcome"] != "NOT_DUE":
        name = "p1-edge-report.json" if args.checkpoint == "edge" else f"p1-week12-{report['outcome']}.json"
        if not _record(directory, args.checkpoint, report, directory / name, args.repo_root):
            report["note"] = f"{args.checkpoint} {report['outcome']} already recorded: not overwritten"
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["outcome"] in ("NOT_DUE", "OPERATIONAL_PASS", "CONFIRMED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
