"""Checkpoint reports of the forward paper session E05-P1 (PRE-7).

Turns the criteria of ``config/experiments/E05-P1-paper-forward-v1.json`` into numbers and an
outcome, so the decision is computed rather than read from prose:

* ``week12``: operational criteria (timeliness, no day lost by the runner, fidelity to the engine,
  drawdown); outcome ``OPERATIONAL_PASS`` / ``OPERATIONAL_FAIL`` / ``NOT_DUE`` (fewer than 12 full weeks);
* ``edge``: the edge criteria on the net result of the CLOSED trades (the open position and its
  unrealised result are reported next to it), the equity drawdown, costs against gross on closed
  trades and the comparison with buy and hold on the session's recorded bars; outcome ``CONFIRMED`` /
  ``NOT_CONFIRMED`` once the trigger is reached, ``INCONCLUSIVE`` when the 52-week deadline passes
  without a trigger, ``NOT_DUE`` otherwise.

Reading.  Both reports read the session through a read-only, consistent snapshot
(:mod:`session_snapshot`): they never write to the session database (except to record a production
decision), never open the data source and never ask it for the clock.  ``--as-of`` cuts every piece of
the session at that instant, so a past instant never shows later data.

Stop rules, checked by both reports at any time: ``INVALID`` when the session is not the frozen
candidate of a preregistered spec on the live source (effective configuration digest, features, source
kind, symbol, host, warmup depth, mode, start after the preregistration) or when, at the edge, the
fidelity check fails up to the cutoff; ``FAILED`` when the equity drawdown over everything seen so far
goes under the limit.

Trigger.  The edge triggers on the first Monday 00:00 UTC at or after the close of the 8th closed trade
and is computed on the session up to that Monday (included), whenever the tool is run.  It is not
decided until the bar of that Monday is committed (or a gap covering it is acknowledged).  The trigger
must fall within 52 weeks of the session start: a later trigger, or no trigger by the deadline, is
``INCONCLUSIVE``.

Once.  ``--as-of`` is a dry run that writes nothing.  A production edge decision is recorded atomically in
the session database (one edge row, enforced by a unique index) and in a file; a second evaluation is
refused (exit code 2), a missing file is re-created from the database.  Week-12 outcomes are recorded once
each.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any
import uuid

import pandas as pd

from . import studio_analytics as analytics
from .execution_contract import HOUR
from .session_fidelity import check, run_engine
from .session_snapshot import SessionSnapshot, read_snapshot
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
LIVE_SOURCE_KIND = "public_klines"   # the only source a production session may use (tests replace it)
LIVE_MODE = "paper"                  # and the only session mode
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
def read_spec(path: str | Path) -> tuple[dict, str]:
    """The spec and its sha256 from ONE read of the file (line endings normalised to LF)."""
    raw = Path(path).read_bytes().replace(b"\r\n", b"\n")
    spec = json.loads(raw.decode("utf-8-sig"))
    candidate_pin(spec)  # a spec that contradicts its own candidate block is refused
    return spec, hashlib.sha256(raw).hexdigest()


def spec_sha256(path: str | Path) -> str:
    return read_spec(path)[1]


def load_spec(path: str | Path | None = None, *, repo_root: str | Path = ".") -> dict:
    target = Path(path) if path is not None else Path(repo_root) / DEFAULT_SPEC
    return read_spec(target)[0]


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


def invalid_reasons(snapshot: SessionSnapshot, spec: dict, *, production: bool) -> list[str]:
    """Why this session is not the frozen candidate of the spec on the declared source (stop rule INVALID)."""
    digest, features = candidate_pin(spec)
    meta, reasons = snapshot.meta, []
    effective = config_digest(meta["config"])  # recomputed from the stored configuration, not trusted
    if effective != digest:
        reasons.append(f"effective configuration {effective[:12]} differs from the frozen candidate {digest[:12]}")
    elif meta["config_sha256"] != effective:
        reasons.append(f"stored digest {meta['config_sha256'][:12]} does not match the stored configuration")
    if meta.get("features") != features:
        reasons.append(f"feature parameters {meta.get('features')} differ from the frozen {features}")
    if not production:
        return reasons
    data = spec.get("data", {})
    registered = pd.to_datetime(spec.get("registered"), utc=True, errors="coerce") if spec.get("registered") else pd.NaT
    if spec.get("status") != "PREREGISTERED" or pd.isna(registered):
        reasons.append(f"the spec is not preregistered with a valid date (status {spec.get('status')}, "
                       f"registered {spec.get('registered')})")
    elif _utc(meta["start_at"]) < registered:
        reasons.append(f"the session starts {meta['start_at']}, before the preregistration of the spec "
                       f"({spec['registered']})")
    if data.get("source_kind") != LIVE_SOURCE_KIND:
        reasons.append(f"the spec declares source kind {data.get('source_kind')}, not {LIVE_SOURCE_KIND}")
    source = meta["source"]
    identity = source.get("identity", {})  # what the data source object reported: the values in effect
    kind = source.get("kind")
    if kind != LIVE_SOURCE_KIND or identity.get("kind", kind) != kind or meta["mode"] != LIVE_MODE:
        reasons.append(f"not a live paper session on {LIVE_SOURCE_KIND} (source {kind}, identity "
                       f"{identity.get('kind')}, mode {meta['mode']})")
    elif kind == "public_klines":
        for field in ("symbol", "history_hours"):  # a value written in the source must agree with the one in effect
            if field in source and source[field] != identity.get(field):
                reasons.append(f"source {field} {source[field]} disagrees with the data source in effect "
                               f"({identity.get(field)})")
        if identity.get("symbol") != data.get("symbol"):
            reasons.append(f"symbol {identity.get('symbol')} is not {data.get('symbol')}")
        if data.get("base_url") and identity.get("base_url") != data["base_url"]:
            reasons.append(f"data host {identity.get('base_url')} is not {data['base_url']}")
        hours = identity.get("history_hours")
        if hours is None or int(hours) < int(data.get("min_history_hours", 0)):
            reasons.append(f"warmup of {hours} hours, fewer than {data.get('min_history_hours')}")
    return reasons


# ---------------------------------------------------------------- decisions recorded in the session
_TABLE = ("CREATE TABLE IF NOT EXISTS p1_decisions(checkpoint TEXT NOT NULL, outcome TEXT NOT NULL, "
          "written_at TEXT NOT NULL, report TEXT NOT NULL, PRIMARY KEY (checkpoint, outcome))")
_EDGE_ONCE = "CREATE UNIQUE INDEX IF NOT EXISTS p1_edge_once ON p1_decisions(checkpoint) WHERE checkpoint = 'edge'"


def _connect(directory: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(directory / "session.sqlite", timeout=30, isolation_level=None)
    connection.execute(_TABLE)
    connection.execute(_EDGE_ONCE)
    return connection


def _edge_file(directory: Path) -> Path:
    return directory / "p1-edge-report.json"


def _same(file: Path, text: str) -> bool:
    try:
        return file.read_text(encoding="utf-8") == text
    except OSError:
        return False


def _publish(file: Path, text: str) -> None:
    """Write ``file`` atomically: a complete temporary file, then a rename; concurrent exporters are harmless."""
    if _same(file, text):
        return
    temporary = file.with_name(f".{file.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    for _ in range(40):
        try:
            os.replace(temporary, file)
            return
        except PermissionError:  # Windows: another exporter is replacing the same file right now
            if _same(file, text):
                temporary.unlink(missing_ok=True)
                return
            time.sleep(0.05)
    temporary.unlink(missing_ok=True)
    raise ReportError(f"could not publish {file}")


def export_decision(directory: Path, checkpoint: str, outcome: str | None, file: Path) -> bool:
    """Re-create ``file`` from the decision stored in the session database (missing, truncated or different)."""
    database = directory / "session.sqlite"
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'p1_decisions'").fetchone() is None:
            return False
        query = "SELECT report FROM p1_decisions WHERE checkpoint = ?" + (" AND outcome = ?" if outcome else "")
        row = connection.execute(query, (checkpoint, outcome) if outcome else (checkpoint,)).fetchone()
    finally:
        connection.close()
    if row is None:
        return False
    _publish(file, json.dumps(json.loads(row[0]), indent=2, default=str) + "\n")
    return True


def edge_decided(directory: Path) -> bool:
    """Whether the edge was already decided here (a row in the session database, the only record that counts).

    Its file is re-created from that row when needed; a file without a row is not a decision and is
    overwritten when the decision is recorded (review R52).
    """
    return export_decision(directory, "edge", None, _edge_file(directory))


def record_decision(directory: Path, checkpoint: str, report: dict, file: Path) -> bool:
    """Record a decision atomically in the session database, then export ``file`` from it; False if it existed."""
    connection = _connect(directory)
    try:
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("INSERT INTO p1_decisions(checkpoint, outcome, written_at, report) VALUES (?, ?, ?, ?)",
                               (checkpoint, report["outcome"], str(_now()), json.dumps(report, default=str)))
            connection.execute("COMMIT")
            inserted = True
        except sqlite3.IntegrityError:
            connection.execute("ROLLBACK")
            inserted = False
    finally:
        connection.close()
    # the file always mirrors the stored decision (also when this call lost a race or the file was damaged)
    export_decision(directory, checkpoint, None if checkpoint == "edge" else report["outcome"], file)
    return inserted


# ---------------------------------------------------------------- shared helpers
def _gaps(snapshot: SessionSnapshot) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    return [(_utc(r["timestamp"]), _utc(r["resumed_at"])) for r in snapshot.ledger if r["kind"] == "gap_acknowledged"]


def _initial(snapshot: SessionSnapshot) -> float:
    return float(snapshot.meta["config"]["initial_cash"])


def _stop_outcome(invalid: list[str], drawdown: float) -> str | None:
    if invalid:
        return "INVALID"
    if drawdown < DRAWDOWN_MIN_PCT:
        return "FAILED"
    return None


# ---------------------------------------------------------------- week 12
def timeliness(snapshot: SessionSnapshot, as_of: pd.Timestamp) -> dict:
    """Daily 00:00 UTC decisions committed within :data:`TIMELY_HOURS` hours, from ``status_log``.

    A day whose 00:00 bar is missing because of an acknowledged market-data gap is excused: it is not
    in the count (the runner did nothing wrong).  A day the runner never processed counts as not on time.
    """
    start = _utc(snapshot.meta["start_at"])
    committed: dict[pd.Timestamp, pd.Timestamp] = {}
    for row in snapshot.status_log:
        detail = row["detail"] or ""
        if detail.startswith("last event "):
            committed.setdefault(_utc(detail[len("last event "):]), _utc(row["at"]))  # wall clock of the commit
    processed = {_utc(ts) for ts, _ in snapshot.events}
    gaps = _gaps(snapshot)
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


def week12(session_dir: str | Path, *, repo_root: str | Path = ".", as_of=None, spec: dict | None = None) -> dict:
    production = as_of is None
    clock = _now() if production else _utc(as_of)
    spec = spec or load_spec(repo_root=repo_root)
    snapshot = read_snapshot(session_dir, as_of=None if production else clock)
    weeks = float((clock - _utc(snapshot.meta["start_at"])) / WEEK)
    punctuality = timeliness(snapshot, clock)
    drawdown = analytics.summarize(snapshot.ledger, _initial(snapshot))["max_drawdown_mtm_pct"]
    invalid = invalid_reasons(snapshot, spec, production=production)
    fidelity = check(snapshot=snapshot)
    share = punctuality["on_time_share"]
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
    return {"checkpoint": "P1.week12", "session_id": snapshot.meta["session_id"], "mode": snapshot.meta["mode"],
            "as_of": str(clock), "dry_run": not production, "elapsed_weeks": round(weeks, 2), "outcome": outcome,
            "invalid_reasons": invalid, "criteria": criteria, "timeliness": punctuality,
            "fidelity": {k: fidelity[k] for k in ("match", "events", "first_difference")}}


# ---------------------------------------------------------------- edge
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


def edge_figures(snapshot: SessionSnapshot) -> dict:
    """Equity and realised figures side by side, the open position, and the buy and hold, on ``snapshot``."""
    initial = _initial(snapshot)
    summary = analytics.summarize(snapshot.ledger, initial)
    trades = analytics.trades(snapshot.ledger)
    closed = trades[trades["status"] == "CLOSED"] if not trades.empty else trades
    opened = trades[trades["status"] == "OPEN"] if not trades.empty else trades
    realized = float(closed["pnl"].sum()) if not closed.empty else 0.0
    costs = float(closed["costs"].sum()) if not closed.empty else 0.0
    gross = realized + costs
    hold = copy.deepcopy(snapshot.meta["config"])
    hold["strategy"] = {"name": "constant", "parameters": {"exposure": "LONG"}}
    hold_ledger, _ = run_engine(snapshot, hold, with_features=False)
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
    snapshot = read_snapshot(session_dir, as_of=None if production else clock)
    start = _utc(snapshot.meta["start_at"])
    weeks = float((clock - start) / WEEK)
    deadline = start + EDGE_DEADLINE_WEEKS * WEEK
    invalid = invalid_reasons(snapshot, spec, production=production)
    fired = trigger(snapshot.ledger)
    due = fired["due_at"] if fired is not None else None
    late_trigger = due is not None and due > deadline
    processed = {_utc(ts) for ts, _ in snapshot.events}
    ready = (due is not None and not late_trigger and due <= clock
             and (due in processed or any(a <= due < b for a, b in _gaps(snapshot))))
    cut = snapshot.until(due) if ready else snapshot  # the evaluation is the session as of the trigger Monday
    figures = edge_figures(cut)
    whole_drawdown = analytics.summarize(snapshot.ledger, _initial(snapshot))["max_drawdown_mtm_pct"]
    fidelity = check(snapshot=cut)
    if not fidelity["match"]:
        invalid.append("the session ledger is not what the engine produces on the session's own recorded bars "
                       f"up to the cutoff (first difference at record {fidelity['first_difference']})")
    to_date = check(snapshot=snapshot) if ready else fidelity
    report: dict[str, Any] = {
        "checkpoint": "P1.edge", "session_id": snapshot.meta["session_id"], "mode": snapshot.meta["mode"],
        "as_of": str(clock), "dry_run": not production, "elapsed_weeks": round(weeks, 2),
        "deadline": str(deadline), "figures": figures, "stop_drawdown_pct": whole_drawdown,
        "fidelity_to_the_cutoff": fidelity["match"], "fidelity_to_date": to_date["match"],
        "invalid_reasons": invalid, "criteria": []}
    if fired is not None:
        report["trigger"] = {"eighth_trade_closed_at": fired["eighth_trade_closed_at"], "due_at": str(due),
                             "evaluated_as_of": str(due) if ready else None,
                             "delay_days": round(float((clock - due) / DAY), 2), "within_deadline": not late_trigger}
    stop = _stop_outcome(invalid, whole_drawdown)
    if stop is not None:
        report.update(outcome=stop, reason="stop rule")
    elif (fired is None or late_trigger) and clock >= deadline:
        report.update(outcome="INCONCLUSIVE",
                      reason=(f"the trigger Monday {due} is after the {EDGE_DEADLINE_WEEKS}-week deadline {deadline}"
                              if late_trigger else
                              f"fewer than {EDGE_MIN_CLOSED_TRADES} closed trades after {EDGE_DEADLINE_WEEKS} weeks"))
    elif ready:
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
    elif fired is not None and not late_trigger and due <= clock:
        report.update(outcome="NOT_DUE", reason=f"8 closed trades reached at {fired['eighth_trade_closed_at']}; the "
                                                f"bar of the Monday {due} is not committed yet, nothing is decided")
    elif fired is not None and not late_trigger:
        report.update(outcome="NOT_DUE", reason=f"8 closed trades reached at {fired['eighth_trade_closed_at']}; the "
                                                f"evaluation is due at the Monday {due}")
    elif late_trigger:
        report.update(outcome="NOT_DUE", reason=f"the trigger Monday {due} is after the deadline {deadline}: "
                                                "INCONCLUSIVE when the deadline is reached")
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
    spec_path = Path(args.spec) if args.spec else Path(args.repo_root) / DEFAULT_SPEC
    spec, spec_digest = read_spec(spec_path)  # the hash is of the very bytes the report uses
    dry = args.as_of is not None
    already = {"error": "the edge was already evaluated for this session"}
    if args.checkpoint == "edge" and not dry and edge_decided(directory):
        print(json.dumps(already))
        return 2  # evaluated exactly once, recorded in the session as well as in the file
    run = week12 if args.checkpoint == "week12" else edge
    report = run(directory, repo_root=args.repo_root, as_of=args.as_of, spec=spec)
    report["spec_sha256"] = spec_digest  # to be compared with the official register
    if not dry and report["outcome"] != "NOT_DUE":
        name = "p1-edge-report.json" if args.checkpoint == "edge" else f"p1-week12-{report['outcome']}.json"
        if not record_decision(directory, args.checkpoint, report, directory / name):
            if args.checkpoint == "edge":
                print(json.dumps(already))
                return 2  # lost the race to another evaluation
            report["note"] = f"{args.checkpoint} {report['outcome']} already recorded: not overwritten"
    print(json.dumps(report, indent=2, default=str))
    return 0 if report["outcome"] in ("NOT_DUE", "OPERATIONAL_PASS", "CONFIRMED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
