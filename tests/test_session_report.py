"""E05-P1 checkpoint reports (PRE-7): timeliness, week 12, edge on closed trades, stop rules, recorded decisions."""
from __future__ import annotations

import copy
import json
import shutil

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

from crypto_decision_lab import paper, session_report
from crypto_decision_lab import studio_analytics as analytics
from crypto_decision_lab.paper import PaperSession
from crypto_decision_lab.studio import config_digest, normalize_config

REAL_SPEC = session_report.DEFAULT_SPEC
TIMELY = "daily decisions committed within 2 h on at least 95% of the counted days"
NO_LOSS = "no day without decision because of the runner"


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


def _spec(features=FEATURES, source_kind="snapshot"):
    """The E05-P1 spec with the test candidate (fast features, replay source) pinned by its digest."""
    spec = json.loads(REAL_SPEC.read_text(encoding="utf-8"))
    block = spec["candidate"]
    block.update(strategy=CONFIG["strategy"], initial_cash=CONFIG["initial_cash"], hypothesis=CONFIG["hypothesis"],
                 risk=CONFIG["risk"], features=dict(features))
    block["config_sha256"] = config_digest(normalize_config(CONFIG))
    spec["data"]["source_kind"] = source_kind
    return spec


def _spec_file(tmp_path, **kwargs):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(_spec(**kwargs)), encoding="utf-8")
    return path


def _session(repo, days, name="p1"):
    session = PaperSession.create(name, CONFIG, {"kind": "snapshot", "dataset_id": RESEARCH_ID},
                                  start_at=str(T0.tz_localize(None)), end_at=str((T0 + days * DAY).tz_localize(None)),
                                  root="var/paper", repo_root=repo, features=FEATURES)
    session.poll()
    directory = session.directory
    session.close()
    return directory


def _failed(report):
    return [c["criterion"] for c in report["criteria"] if not c["passed"]]


# ---------------------------------------------------------------- the spec pins the candidate
def test_the_real_spec_pins_the_candidate_and_is_checked_against_itself(tmp_path):
    spec = session_report.load_spec(repo_root=".")
    digest, features = session_report.candidate_pin(spec)
    assert digest.startswith("7775ccd4") and features == {"momentum_days": 28, "sma_days": 100}
    assert spec["data"]["source_kind"] == "public_klines"
    tampered = copy.deepcopy(spec)
    tampered["candidate"]["hypothesis"]["fee_rate"] = 0.0
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(session_report.ReportError, match="does not match its own candidate block"):
        session_report.load_spec(path)


# ---------------------------------------------------------------- timeliness and week 12
def test_timeliness_counts_late_and_missing_days(repo, monkeypatch):
    _clock(monkeypatch, late_days={3, 10})
    session = PaperSession(_session(repo, 30), repo_root=repo)
    result = session_report.timeliness(session, T0 + 29 * DAY + pd.Timedelta(hours=3))
    assert result["days_expected"] == 30 and result["on_time_days"] == 28 and result["days_excused_by_gap"] == 0
    assert result["late_days"] == [str(T0 + 3 * DAY), str(T0 + 10 * DAY)] and result["missing_days"] == []
    assert result["max_delay_hours"] == pytest.approx(5.0) and result["on_time_share"] == pytest.approx(28 / 30)
    beyond = session_report.timeliness(session, T0 + 33 * DAY)  # the session ended on day 29: days 30-32 never processed
    assert [m[:10] for m in beyond["unexplained_missing_days"]] == ["2024-07-01", "2024-07-02", "2024-07-03"]
    session.close()


def test_a_market_data_gap_excuses_its_day_instead_of_failing_the_timeliness(tmp_path, monkeypatch, market_frame):
    root = tmp_path / "gap-repo"
    root.mkdir()
    lost = T0 + 10 * DAY  # the 00:00 bar of day 10 never existed
    _write_catalog(root, market_frame.drop(index=[lost]))
    _clock(monkeypatch)
    session = PaperSession.create("gap", CONFIG, {"kind": "snapshot", "dataset_id": RESEARCH_ID},
                                  start_at=str(T0.tz_localize(None)), end_at=str((T0 + 30 * DAY).tz_localize(None)),
                                  root="var/paper", repo_root=root, features=FEATURES)
    assert session.poll()["status"] == "PAUSED_GAP"
    session.acknowledge_gap("bar lost by the exchange")
    assert session.poll()["status"] == "COMPLETED"
    result = session_report.timeliness(session, T0 + 29 * DAY + pd.Timedelta(hours=3))
    assert result["missing_days"] == result["excused_days"] == [str(lost)] and result["unexplained_missing_days"] == []
    assert result["days_due"] == 30 and result["days_excused_by_gap"] == 1 and result["days_expected"] == 29
    assert result["on_time_share"] == 1.0  # nothing late: the gap day is not counted against the runner
    session.close()


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
    live = session_report.week12(directory, repo_root=repo, spec=_spec(source_kind="public_klines"))
    assert live["outcome"] == "INVALID" and "not a live paper session on public_klines" in live["invalid_reasons"][0]
    assert live["dry_run"] is False


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


def test_dry_runs_write_nothing_and_production_decides_once(repo, monkeypatch, long_session, tmp_path, capsys):
    spec = _spec_file(tmp_path)
    base = ["--session-dir", str(long_session), "--repo-root", str(repo), "--spec", str(spec)]
    dry = session_report.main(["edge", *base, "--as-of", str((T0 + 240 * DAY).tz_localize(None))])
    out = json.loads(capsys.readouterr().out)
    assert dry in (0, 1) and out["dry_run"] is True and out["outcome"] in ("CONFIRMED", "NOT_CONFIRMED")
    assert not (long_session / "p1-edge-report.json").exists()
    session = PaperSession(long_session, repo_root=repo)
    assert session.connection.execute(  # not even the table of recorded decisions
        "SELECT name FROM sqlite_master WHERE name = 'p1_decisions'").fetchone() is None
    session.close()

    monkeypatch.setattr(session_report, "_now", lambda: T0 + 240 * DAY)
    first = session_report.main(["edge", *base])
    out = json.loads(capsys.readouterr().out)
    assert first in (0, 1) and out["dry_run"] is False and out["outcome"] in ("CONFIRMED", "NOT_CONFIRMED")
    written = json.loads((long_session / "p1-edge-report.json").read_text(encoding="utf-8"))
    assert written["outcome"] == out["outcome"] and written["figures"] == out["figures"]
    assert session_report.main(["edge", *base]) == 2
    assert "already evaluated" in json.loads(capsys.readouterr().out)["error"]
    (long_session / "p1-edge-report.json").unlink()  # deleting the file does not reopen the evaluation
    assert session_report.main(["edge", *base]) == 2
    capsys.readouterr()
    copy_dir = tmp_path / "copy"
    shutil.copytree(long_session, copy_dir)  # nor does copying the session folder: the record travels with it
    assert session_report.main(["edge", "--session-dir", str(copy_dir), "--repo-root", str(repo),
                                "--spec", str(spec)]) == 2


def test_edge_not_due_and_inconclusive_after_52_weeks(repo, monkeypatch, tmp_path, capsys):
    _clock(monkeypatch)
    quiet = _session(repo, 20, "quiet")  # fewer than 8 closed trades
    spec = _spec_file(tmp_path)
    base = ["--session-dir", str(quiet), "--repo-root", str(repo), "--spec", str(spec)]
    monkeypatch.setattr(session_report, "_now", lambda: T0 + 21 * DAY)
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
    monkeypatch.setattr(session_report, "_now", lambda: T0 + 84 * DAY + pd.Timedelta(hours=1))
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
