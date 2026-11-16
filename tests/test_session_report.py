"""E05-P1 checkpoint reports (PRE-7): timeliness, week 12, edge on closed trades, stop rules, admission,
readiness, deadline, one atomic decision, read-only snapshot."""
from __future__ import annotations

import copy
import dataclasses
import json
import shutil
import sqlite3

import pandas as pd
import pytest
from test_paper_daily import (  # noqa: F401  (fixtures and helpers of the sibling module)
    CONFIG,
    DAY,
    FEATURES,
    RESEARCH_ID,
    T0,
    _write_catalog,
    market_frame,
    repo,
)

from crypto_decision_lab import paper, session_fidelity, session_report
from crypto_decision_lab import studio_analytics as analytics
from crypto_decision_lab.paper import PaperSession
from crypto_decision_lab.session_snapshot import read_snapshot
from crypto_decision_lab.studio import config_digest, normalize_config

REAL_SPEC = session_report.DEFAULT_SPEC
TIMELY = "daily decisions committed within 2 h on at least 95% of the counted days"
NO_LOSS = "no day without decision because of the runner"
BASE_URL = "https://data-api.binance.vision"


def _clock(monkeypatch, late_days=()):
    """Wall clock of the commits = bar time + 5 minutes, or + 5 hours on the days listed as late."""
    current: dict = {}
    original = PaperSession._commit_step

    def wrapped(self, *args):
        current["bar"] = pd.Timestamp(args[-1]) if args[-1] is not None else None
        return original(self, *args)

    def fake_now():
        bar = current.get("bar")
        if bar is None:
            return "2024-05-01T00:00:00Z"
        late = bar.hour == 0 and int((bar - T0) / DAY) in late_days
        return (bar + pd.Timedelta(hours=5 if late else 0.08)).strftime("%Y-%m-%dT%H:%M:%SZ")

    monkeypatch.setattr(PaperSession, "_commit_step", wrapped)
    monkeypatch.setattr(paper, "_now", fake_now)


def _spec(features=FEATURES, source_kind="snapshot", status="PREREGISTERED", registered="2024-01-01"):
    """The E05-P1 spec with the test candidate (fast features, replay source) pinned by its digest."""
    spec = json.loads(REAL_SPEC.read_text(encoding="utf-8"))
    block = spec["candidate"]
    block.update(strategy=CONFIG["strategy"], initial_cash=CONFIG["initial_cash"], hypothesis=CONFIG["hypothesis"],
                 risk=CONFIG["risk"], features=dict(features))
    block["config_sha256"] = config_digest(normalize_config(CONFIG))
    spec["data"]["source_kind"] = source_kind
    spec["status"], spec["registered"] = status, registered
    return spec


def _spec_file(tmp_path, **kwargs):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(_spec(**kwargs)), encoding="utf-8")
    return path


def _production(monkeypatch, now):
    """Production reports on a replay session: the declared live source and mode become the replay ones."""
    monkeypatch.setattr(session_report, "LIVE_SOURCE_KIND", "snapshot")
    monkeypatch.setattr(session_report, "LIVE_MODE", "paper-replay")
    monkeypatch.setattr(session_report, "_now", lambda: now)


def _session(repo, days, name="p1", features=FEATURES):
    session = PaperSession.create(name, CONFIG, {"kind": "snapshot", "dataset_id": RESEARCH_ID},
                                  start_at=str(T0.tz_localize(None)), end_at=str((T0 + days * DAY).tz_localize(None)),
                                  root="var/paper", repo_root=repo, features=features)
    session.poll()
    directory = session.directory
    session.close()
    return directory


def _failed(report):
    return [c["criterion"] for c in report["criteria"] if not c["passed"]]


def _tables(directory):
    """Every table of the session database, to compare before and after a report."""
    connection = sqlite3.connect(directory / "session.sqlite")
    try:
        names = [n for (n,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY 1")]
        return {name: list(connection.execute(f"SELECT * FROM {name}")) for name in names}
    finally:
        connection.close()


# ---------------------------------------------------------------- the spec pins the candidate
def test_the_real_spec_pins_the_candidate_and_is_checked_against_itself(tmp_path):
    spec = session_report.load_spec(repo_root=".")
    digest, features = session_report.candidate_pin(spec)
    assert digest.startswith("7775ccd4") and features == {"momentum_days": 28, "sma_days": 100}
    assert spec["data"]["source_kind"] == "public_klines" and spec["data"]["symbol"] == "BTCUSDT"
    assert spec["data"]["base_url"] == BASE_URL and spec["data"]["min_history_hours"] == 2400
    tampered = copy.deepcopy(spec)
    tampered["candidate"]["hypothesis"]["fee_rate"] = 0.0
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(session_report.ReportError, match="does not match its own candidate block"):
        session_report.load_spec(path)


# ---------------------------------------------------------------- admission (R35)
def test_production_admission_checks_the_whole_perimeter(repo, monkeypatch):
    _clock(monkeypatch)
    snapshot = read_snapshot(_session(repo, 30, "adm"))
    live = dataclasses.replace(snapshot, meta={**snapshot.meta, "mode": "paper", "source": {
        "kind": "public_klines", "symbol": "BTCUSDT", "history_hours": 2600,
        "identity": {"kind": "public_klines", "symbol": "BTCUSDT", "history_hours": 2600, "base_url": BASE_URL}}})
    real = json.loads(REAL_SPEC.read_text(encoding="utf-8"))
    real.update(status="PREREGISTERED", registered="2024-01-01")
    real["candidate"].update(strategy=CONFIG["strategy"], features=dict(FEATURES), hypothesis=CONFIG["hypothesis"],
                             risk=CONFIG["risk"], initial_cash=CONFIG["initial_cash"],
                             config_sha256=config_digest(normalize_config(CONFIG)))
    assert session_report.invalid_reasons(live, real, production=True) == []  # a regular live session passes

    def reasons(snap=live, spec=real):
        return session_report.invalid_reasons(snap, spec, production=True)

    other_symbol = dataclasses.replace(live, meta={**live.meta, "source": {**live.meta["source"], "symbol": "ETHUSDT",
                                                                           "identity": {**live.meta["source"]["identity"],
                                                                                        "symbol": "ETHUSDT"}}})
    assert any("symbol ETHUSDT is not BTCUSDT" in r for r in reasons(other_symbol))
    other_host = dataclasses.replace(live, meta={**live.meta, "source": {**live.meta["source"], "identity": {
        **live.meta["source"]["identity"], "base_url": "https://example.test"}}})
    assert any("data host" in r for r in reasons(other_host))
    shallow = dataclasses.replace(live, meta={**live.meta, "source": {**live.meta["source"], "history_hours": 600}})
    assert any("warmup of 600 hours" in r for r in reasons(shallow))
    replay_mode = dataclasses.replace(live, meta={**live.meta, "mode": "paper-replay"})
    assert any("not a live paper session" in r for r in reasons(replay_mode))
    assert any("not preregistered" in r for r in reasons(live, {**real, "status": "DRAFT"}))
    assert any("not preregistered" in r for r in reasons(live, {**real, "registered": None}))
    assert any("before the preregistration" in r for r in reasons(live, {**real, "registered": "2024-07-01"}))
    assert any("not public_klines" in r for r in reasons(live, {**real, "data": {**real["data"], "source_kind": "lab_feed"}}))
    forged = dataclasses.replace(live, meta={**live.meta, "config": {**live.meta["config"], "risk": {
        **live.meta["config"]["risk"], "max_exposure": 1.0}}})  # configuration altered, stored digest left as it was
    assert any("effective configuration" in r for r in reasons(forged))
    swapped = dataclasses.replace(forged, meta={**forged.meta, "config_sha256": config_digest(forged.meta["config"])})
    assert any("effective configuration" in r for r in reasons(swapped))  # also when the stored digest is made to agree
    # outside production only the candidate itself is checked
    assert session_report.invalid_reasons(other_symbol, real, production=False) == []


# ---------------------------------------------------------------- the report reads, it does not touch (R38, R42)
def test_reports_read_a_snapshot_and_never_open_or_write_the_session(repo, monkeypatch):
    _clock(monkeypatch)
    directory = _session(repo, 90, "ro")
    session = PaperSession(directory, repo_root=repo)
    session.connection.execute("UPDATE meta SET value = '\"0000000000000000\"' WHERE key = 'engine_sha256'")
    session.close()  # a pinned engine that no longer matches: PaperSession would change the status when opened
    before = _tables(directory)
    monkeypatch.setattr(PaperSession, "__init__", lambda *a, **k: (_ for _ in ()).throw(AssertionError("opened")))
    spec = _spec()
    week = session_report.week12(directory, repo_root=repo, as_of=T0 + 84 * DAY, spec=spec)
    edge = session_report.edge(directory, repo_root=repo, as_of=T0 + 90 * DAY, spec=spec)
    assert week["outcome"] in ("OPERATIONAL_PASS", "OPERATIONAL_FAIL") and edge["outcome"] != "INVALID"
    assert session_fidelity.check(directory)["match"]
    assert _tables(directory) == before  # not a row changed, not even the status of the session
    assert not list(directory.glob("p1-*.json"))


# ---------------------------------------------------------------- timeliness and week 12
def test_timeliness_counts_late_and_missing_days(repo, monkeypatch):
    _clock(monkeypatch, late_days={3, 10})
    snapshot = read_snapshot(_session(repo, 30))
    result = session_report.timeliness(snapshot, T0 + 29 * DAY + pd.Timedelta(hours=3))
    assert result["days_expected"] == 30 and result["on_time_days"] == 28 and result["days_excused_by_gap"] == 0
    assert result["late_days"] == [str(T0 + 3 * DAY), str(T0 + 10 * DAY)] and result["missing_days"] == []
    assert result["max_delay_hours"] == pytest.approx(5.0) and result["on_time_share"] == pytest.approx(28 / 30)
    beyond = session_report.timeliness(snapshot, T0 + 33 * DAY)  # the session ended on day 29: days 30-32 never processed
    assert [m[:10] for m in beyond["unexplained_missing_days"]] == ["2024-07-01", "2024-07-02", "2024-07-03"]


def test_a_market_data_gap_excuses_its_day_and_the_fidelity_replays_it(tmp_path, monkeypatch, market_frame):
    root = tmp_path / "gap-repo"
    root.mkdir()
    lost = T0 + 10 * DAY  # the 00:00 bar of day 10 never existed
    longer = [T0 + 20 * DAY + pd.Timedelta(hours=7), T0 + 20 * DAY + pd.Timedelta(hours=8)]
    _write_catalog(root, market_frame.drop(index=[lost, *longer]))
    _clock(monkeypatch)
    session = PaperSession.create("gap", CONFIG, {"kind": "snapshot", "dataset_id": RESEARCH_ID},
                                  start_at=str(T0.tz_localize(None)), end_at=str((T0 + 30 * DAY).tz_localize(None)),
                                  root="var/paper", repo_root=root, features=FEATURES)
    while (status := session.poll()["status"]) == "PAUSED_GAP":
        session.acknowledge_gap("bar lost by the exchange")
    assert status == "COMPLETED"
    directory = session.directory
    session.close()
    snapshot = read_snapshot(directory)
    result = session_report.timeliness(snapshot, T0 + 29 * DAY + pd.Timedelta(hours=3))
    assert result["missing_days"] == result["excused_days"] == [str(lost)] and result["unexplained_missing_days"] == []
    assert result["days_due"] == 30 and result["days_excused_by_gap"] == 1 and result["days_expected"] == 29
    assert result["on_time_share"] == 1.0  # nothing late: the gap day is not counted against the runner
    # R41: a session with acknowledged gaps (one of 1 hour, one of 2) is faithful to the engine
    check = session_fidelity.check(snapshot=snapshot)
    assert check["match"] and check["first_difference"] is None, check
    cut = snapshot.until(lost)  # a cut inside the first gap drops the records of the resumption it cannot replay
    assert cut.ledger[-1]["kind"] != "gap_acknowledged" and session_fidelity.check(snapshot=cut)["match"]


def test_week12_pass_late_days_missing_days_and_not_due(repo, monkeypatch):
    _clock(monkeypatch)
    spec = _spec()
    due = T0 + 84 * DAY + pd.Timedelta(hours=1)
    ok_dir = _session(repo, 90, "ok")
    report = session_report.week12(ok_dir, repo_root=repo, as_of=due, spec=spec)
    assert report["outcome"] == "OPERATIONAL_PASS", report["criteria"]
    assert report["elapsed_weeks"] == pytest.approx(12.0, abs=0.01) and report["timeliness"]["on_time_share"] == 1.0
    assert report["fidelity"]["match"] is True and report["dry_run"] is True and report["invalid_reasons"] == []
    early = session_report.week12(ok_dir, repo_root=repo, as_of=T0 + 20 * DAY, spec=spec)
    assert early["outcome"] == "NOT_DUE" and early["elapsed_weeks"] < 12

    _clock(monkeypatch, late_days={3, 10, 20, 30, 40, 50})  # 6 of 84 days late: 92.9% < 95%
    late = session_report.week12(_session(repo, 90, "late"), repo_root=repo, as_of=due, spec=spec)
    assert late["outcome"] == "OPERATIONAL_FAIL" and _failed(late) == [TIMELY]
    _clock(monkeypatch, late_days={3, 10, 20})  # 3 of 84 days late: 96.4%, still passes
    few = session_report.week12(_session(repo, 90, "few"), repo_root=repo, as_of=due, spec=spec)
    assert few["outcome"] == "OPERATIONAL_PASS"

    _clock(monkeypatch)
    short = session_report.week12(_session(repo, 70, "short"), repo_root=repo, as_of=due, spec=spec)  # stopped day 70
    assert short["outcome"] == "OPERATIONAL_FAIL"
    assert _failed(short) == [TIMELY, NO_LOSS]  # a day the runner never processed is also not on time
    assert short["timeliness"]["on_time_share"] == pytest.approx(70 / 84)
    assert len(short["timeliness"]["unexplained_missing_days"]) == 14


# ---------------------------------------------------------------- stop rules: INVALID and FAILED
def test_a_session_that_is_not_the_frozen_candidate_is_invalid(repo, monkeypatch):
    _clock(monkeypatch)
    directory = _session(repo, 90, "inv")
    other = _spec(features={"momentum_days": 7, "sma_days": 21})
    week = session_report.week12(directory, repo_root=repo, as_of=T0 + 84 * DAY, spec=other)
    assert week["outcome"] == "INVALID" and "feature parameters" in week["invalid_reasons"][0]
    edge = session_report.edge(directory, repo_root=repo, as_of=T0 + 90 * DAY, spec=other)
    assert edge["outcome"] == "INVALID" and edge["criteria"] == []
    monkeypatch.setattr(session_report, "_now", lambda: T0 + 84 * DAY)  # production: a replay is not a live session
    live = session_report.week12(directory, repo_root=repo, spec=_spec())
    assert live["outcome"] == "INVALID" and any("not a live paper session on public_klines" in r for r in live["invalid_reasons"])
    assert live["dry_run"] is False
    draft = session_report.week12(directory, repo_root=repo, spec=_spec(status="DRAFT", registered=None))
    assert draft["outcome"] == "INVALID" and any("not preregistered" in r for r in draft["invalid_reasons"])


def test_the_edge_report_is_invalid_when_the_session_is_not_what_the_engine_produces(repo, monkeypatch):
    _clock(monkeypatch)
    directory = _session(repo, 30, "tamper")
    session = PaperSession(directory, repo_root=repo)
    session.connection.execute("UPDATE ledger SET record = replace(record, '\"side\":\"BUY\"', '\"side\":\"SELL\"') "
                               "WHERE rowid = (SELECT MIN(rowid) FROM ledger WHERE kind = 'order')")
    session.close()
    report = session_report.edge(directory, repo_root=repo, as_of=T0 + 30 * DAY, spec=_spec())
    assert report["outcome"] == "INVALID" and "not what the engine produces" in report["invalid_reasons"][-1]


def test_the_drawdown_stop_rule_fails_both_reports(repo, monkeypatch):
    _clock(monkeypatch)
    directory = _session(repo, 90, "dd")
    monkeypatch.setattr(session_report, "DRAWDOWN_MIN_PCT", -0.5)  # the synthetic run draws down more than 0.5%
    week = session_report.week12(directory, repo_root=repo, as_of=T0 + 10 * DAY, spec=_spec())
    assert week["outcome"] == "FAILED"  # at any time, also before week 12
    edge = session_report.edge(directory, repo_root=repo, as_of=T0 + 10 * DAY, spec=_spec())
    assert edge["outcome"] == "FAILED" and edge["reason"] == "stop rule"


# ---------------------------------------------------------------- edge
@pytest.fixture
def long_session(repo, monkeypatch):
    _clock(monkeypatch)
    return _session(repo, 240, "edge")


def test_the_edge_is_computed_up_to_the_trigger_monday_whenever_it_is_run(repo, long_session):
    spec = _spec()
    session = PaperSession(long_session, repo_root=repo)
    ledger = session.ledger()
    initial = float(session.meta["config"]["initial_cash"])
    session.close()
    fired = session_report.trigger(ledger)
    due = fired["due_at"]
    exit_at = pd.Timestamp(fired["eighth_trade_closed_at"])
    assert due.weekday() == 0 and due.hour == 0 and exit_at <= due < exit_at + pd.Timedelta(days=7)

    soon = session_report.edge(long_session, repo_root=repo, as_of=due - pd.Timedelta(hours=1), spec=spec)
    assert soon["outcome"] == "NOT_DUE" and "due at the Monday" in soon["reason"]
    on_time = session_report.edge(long_session, repo_root=repo, as_of=due, spec=spec)
    later = session_report.edge(long_session, repo_root=repo, as_of=T0 + 240 * DAY, spec=spec)
    assert on_time["figures"] == later["figures"] and on_time["criteria"] == later["criteria"]  # waiting changes nothing
    assert on_time["trigger"]["delay_days"] == 0.0 and later["trigger"]["delay_days"] > 0
    assert later["trigger"]["evaluated_as_of"] == str(due)
    assert later["fidelity_to_the_cutoff"] is True and later["fidelity_to_date"] is True

    figures = later["figures"]
    cut = [r for r in ledger if pd.Timestamp(r["timestamp"]) <= due]
    trades = analytics.trades(cut)
    closed = trades[trades["status"] == "CLOSED"]
    assert figures["closed_trades"] == len(closed) >= 8
    assert figures["realized_net_pnl"] == pytest.approx(closed["pnl"].sum())
    assert figures["realized_net_return_pct"] == pytest.approx(100 * closed["pnl"].sum() / initial)
    assert figures["realized_costs"] == pytest.approx(closed["fees"].sum() + closed["slippage"].sum())
    assert figures["realized_gross_pnl"] == pytest.approx(figures["realized_net_pnl"] + figures["realized_costs"])
    assert figures["equity_net_return_pct"] == pytest.approx(analytics.summarize(cut, initial)["net_return_pct"])
    assert figures["last_event"] == str(due)
    unrealized = figures["open_position"]["unrealized_pnl"] if figures["open_position"] else 0.0
    assert (figures["equity_net_return_pct"] - figures["realized_net_return_pct"]) * initial / 100 == pytest.approx(unrealized)

    criteria = later["criteria"]
    assert len(criteria) == 4
    share = figures["realized_costs_share_of_gross"]
    assert criteria[0]["passed"] == (figures["realized_net_return_pct"] > 0)
    assert criteria[2]["passed"] == (share is not None and share <= 0.5)  # an undefined share fails
    assert criteria[3]["threshold"] == pytest.approx(figures["buy_and_hold_net_return_pct"] - 10.0)
    assert later["outcome"] == ("CONFIRMED" if all(c["passed"] for c in criteria) else "NOT_CONFIRMED")


def test_a_dry_run_for_a_past_instant_shows_nothing_from_after_it(repo, long_session):
    spec = _spec()
    snapshot = read_snapshot(long_session)
    fired = session_report.trigger(snapshot.ledger)
    before = fired["due_at"] - 5 * DAY
    report = session_report.edge(long_session, repo_root=repo, as_of=before, spec=spec)
    assert report["outcome"] == "NOT_DUE" and report["dry_run"] is True
    assert pd.Timestamp(report["figures"]["last_event"]) <= before  # the figures stop at the requested instant
    cut = [r for r in snapshot.ledger if pd.Timestamp(r["timestamp"]) <= before]
    trades = analytics.trades(cut)
    assert report["figures"]["closed_trades"] == int((trades["status"] == "CLOSED").sum()) < 8
    week = session_report.week12(long_session, repo_root=repo, as_of=before, spec=spec)
    assert week["fidelity"]["events"] == len([1 for ts, _ in snapshot.events if pd.Timestamp(ts) <= before])


def test_nothing_is_decided_before_the_bar_of_the_trigger_monday_is_committed(tmp_path, repo, monkeypatch, long_session):
    spec = _spec()
    fired = session_report.trigger(read_snapshot(long_session).ledger)
    due = fired["due_at"]
    monkeypatch.undo()  # the real wall clock again: a second session ending the hour before the Monday
    _clock(monkeypatch)
    early = _session(repo, int((due - T0) / DAY), "early")  # events up to the Sunday 23:00, none at the Monday 00:00
    snapshot = read_snapshot(early)
    assert due not in {pd.Timestamp(ts) for ts, _ in snapshot.events}
    waiting = session_report.edge(early, repo_root=repo, as_of=due, spec=spec)
    assert waiting["outcome"] == "NOT_DUE" and "not committed yet" in waiting["reason"]
    assert waiting["trigger"]["evaluated_as_of"] is None and waiting["criteria"] == []
    _production(monkeypatch, due)
    base = ["edge", "--session-dir", str(early), "--repo-root", str(repo), "--spec", str(_spec_file(tmp_path))]
    assert session_report.main(base) == 0 and not (early / "p1-edge-report.json").exists()  # NOT_DUE writes nothing
    decided = session_report.edge(long_session, repo_root=repo, as_of=due, spec=spec)  # the bar is there
    assert decided["outcome"] in ("CONFIRMED", "NOT_CONFIRMED") and decided["figures"]["last_event"] == str(due)


def test_the_52_week_deadline_applies_to_the_trigger_not_to_the_day_the_report_runs(repo, long_session, monkeypatch):
    spec = _spec()
    fired = session_report.trigger(read_snapshot(long_session).ledger)
    due, exit_at = fired["due_at"], pd.Timestamp(fired["eighth_trade_closed_at"])
    if due - exit_at < DAY:
        pytest.skip("the 8th trade closes on the trigger Monday itself: there is no window between the two")
    # the deadline falls between the close of the 8th trade and the Monday of the trigger
    monkeypatch.setattr(session_report, "EDGE_DEADLINE_WEEKS", float(((exit_at + (due - exit_at) / 2) - T0) / pd.Timedelta(weeks=1)))
    waiting = session_report.edge(long_session, repo_root=repo, as_of=exit_at + pd.Timedelta(hours=1), spec=spec)
    assert waiting["outcome"] == "NOT_DUE" and "after the deadline" in waiting["reason"]
    assert waiting["trigger"]["within_deadline"] is False
    late = session_report.edge(long_session, repo_root=repo, as_of=T0 + 240 * DAY, spec=spec)  # run much later
    assert late["outcome"] == "INCONCLUSIVE" and "is after the" in late["reason"]
    assert late["trigger"]["within_deadline"] is False and late["criteria"] == []
    monkeypatch.setattr(session_report, "EDGE_DEADLINE_WEEKS", 52)  # the trigger Monday is within the deadline
    assert session_report.edge(long_session, repo_root=repo, as_of=T0 + 240 * DAY, spec=spec)["outcome"] in (
        "CONFIRMED", "NOT_CONFIRMED")


def test_the_stop_rule_sees_a_breach_after_the_trigger_that_the_edge_figures_do_not(repo, long_session):
    spec = _spec()
    due = session_report.trigger(read_snapshot(long_session).ledger)["due_at"]
    base = session_report.edge(long_session, repo_root=repo, as_of=T0 + 240 * DAY, spec=spec)
    assert base["outcome"] in ("CONFIRMED", "NOT_CONFIRMED") and base["stop_drawdown_pct"] >= -20.0
    connection = sqlite3.connect(long_session / "session.sqlite")  # a crash of the equity well after the trigger
    sequence, record = list(connection.execute(
        "SELECT sequence, record FROM ledger WHERE kind = 'balance' ORDER BY sequence DESC LIMIT 1"))[0]
    crashed = json.loads(record)
    assert pd.Timestamp(crashed["timestamp"]) > due
    crashed["equity"] = 100.0
    connection.execute("UPDATE ledger SET record = ? WHERE sequence = ?", (json.dumps(crashed, sort_keys=True), sequence))
    connection.commit()
    connection.close()
    report = session_report.edge(long_session, repo_root=repo, as_of=T0 + 240 * DAY, spec=spec)
    assert report["outcome"] == "FAILED" and report["reason"] == "stop rule" and report["invalid_reasons"] == []
    assert report["stop_drawdown_pct"] < -70 and report["figures"] == base["figures"]  # the figures stay those of the trigger
    assert report["fidelity_to_the_cutoff"] is True and report["fidelity_to_date"] is False


# ---------------------------------------------------------------- one decision, atomically recorded
def test_dry_runs_write_nothing_and_production_decides_once(repo, monkeypatch, long_session, tmp_path, capsys):
    spec = _spec_file(tmp_path)
    base = ["--session-dir", str(long_session), "--repo-root", str(repo), "--spec", str(spec)]
    before = _tables(long_session)
    dry = session_report.main(["edge", *base, "--as-of", str((T0 + 240 * DAY).tz_localize(None))])
    out = json.loads(capsys.readouterr().out)
    assert dry in (0, 1) and out["dry_run"] is True and out["outcome"] in ("CONFIRMED", "NOT_CONFIRMED")
    assert not (long_session / "p1-edge-report.json").exists() and _tables(long_session) == before

    _production(monkeypatch, T0 + 240 * DAY)
    first = session_report.main(["edge", *base])
    out = json.loads(capsys.readouterr().out)
    assert first in (0, 1) and out["dry_run"] is False and out["outcome"] in ("CONFIRMED", "NOT_CONFIRMED")
    assert len(out["spec_sha256"]) == 64 and out["invalid_reasons"] == []
    written = json.loads((long_session / "p1-edge-report.json").read_text(encoding="utf-8"))
    assert written["outcome"] == out["outcome"] and written["figures"] == out["figures"]
    assert session_report.main(["edge", *base]) == 2
    assert "already evaluated" in json.loads(capsys.readouterr().out)["error"]
    (long_session / "p1-edge-report.json").unlink()  # deleting the file does not reopen the evaluation...
    assert session_report.main(["edge", *base]) == 2
    capsys.readouterr()
    assert json.loads((long_session / "p1-edge-report.json").read_text(encoding="utf-8")) == written  # ...and restores it
    copy_dir = tmp_path / "copy"
    shutil.copytree(long_session, copy_dir)  # nor does copying the session folder: the record travels with it
    assert session_report.main(["edge", "--session-dir", str(copy_dir), "--repo-root", str(repo),
                                "--spec", str(spec)]) == 2


def test_two_different_edge_decisions_cannot_both_be_recorded(repo, long_session):
    directory = long_session
    confirmed = {"outcome": "CONFIRMED", "checkpoint": "P1.edge"}
    failed = {"outcome": "FAILED", "checkpoint": "P1.edge"}
    assert session_report.record_decision(directory, "edge", confirmed, directory / "p1-edge-report.json") is True
    assert session_report.record_decision(directory, "edge", failed, directory / "other.json") is False  # whatever the outcome
    assert not (directory / "other.json").exists()
    connection = sqlite3.connect(directory / "session.sqlite")
    assert list(connection.execute("SELECT outcome FROM p1_decisions WHERE checkpoint = 'edge'")) == [("CONFIRMED",)]
    with pytest.raises(sqlite3.IntegrityError):  # the database itself refuses a second edge row
        connection.execute("INSERT INTO p1_decisions VALUES ('edge', 'NOT_CONFIRMED', 'x', '{}')")
    connection.close()
    assert session_report.edge_decided(directory)


def test_edge_not_due_and_inconclusive_after_52_weeks(repo, monkeypatch, tmp_path, capsys):
    _clock(monkeypatch)
    quiet = _session(repo, 20, "quiet")  # fewer than 8 closed trades
    spec = _spec_file(tmp_path)
    base = ["--session-dir", str(quiet), "--repo-root", str(repo), "--spec", str(spec)]
    _production(monkeypatch, T0 + 21 * DAY)
    assert session_report.main(["edge", *base]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["outcome"] == "NOT_DUE" and "closed trades" in out["reason"]
    assert not (quiet / "p1-edge-report.json").exists()  # nothing recorded
    monkeypatch.setattr(session_report, "_now", lambda: T0 + 53 * 7 * DAY)
    assert session_report.main(["edge", *base]) == 1  # 52 weeks without 8 closed trades
    assert json.loads(capsys.readouterr().out)["outcome"] == "INCONCLUSIVE"
    assert (quiet / "p1-edge-report.json").exists() and session_report.main(["edge", *base]) == 2


def test_week12_outcomes_are_recorded_one_each(repo, monkeypatch, tmp_path, capsys):
    _clock(monkeypatch)
    directory = _session(repo, 90, "w12")
    spec = _spec_file(tmp_path)
    base = ["week12", "--session-dir", str(directory), "--repo-root", str(repo), "--spec", str(spec)]
    _production(monkeypatch, T0 + 84 * DAY + pd.Timedelta(hours=1))
    assert session_report.main(base) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "OPERATIONAL_PASS"
    assert (directory / "p1-week12-OPERATIONAL_PASS.json").exists()
    assert session_report.main(base) == 0
    assert "already recorded" in json.loads(capsys.readouterr().out)["note"]
    monkeypatch.setattr(session_report, "DRAWDOWN_MIN_PCT", -0.5)  # a later breach is recorded next to the pass
    assert session_report.main(base) == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "FAILED"
    assert (directory / "p1-week12-FAILED.json").exists() and (directory / "p1-week12-OPERATIONAL_PASS.json").exists()
    monkeypatch.setattr(session_report, "_now", lambda: T0 + 5 * DAY)
    monkeypatch.setattr(session_report, "DRAWDOWN_MIN_PCT", -20.0)
    assert session_report.main(base) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "NOT_DUE"
    assert sorted(p.name for p in directory.glob("p1-week12-*.json")) == [
        "p1-week12-FAILED.json", "p1-week12-OPERATIONAL_PASS.json"]
