"""E05-P1 checkpoint reports (PRE-7): timeliness, week 12, edge on closed trades, evaluated once."""
from __future__ import annotations

import json

import pandas as pd
import pytest
from test_paper_daily import (  # noqa: F401  (fixtures and helpers of the sibling module)
    CONFIG,
    DAY,
    FEATURES,
    RESEARCH_ID,
    T0,
    market_frame,
    repo,
)

from crypto_decision_lab import paper, session_report
from crypto_decision_lab import studio_analytics as analytics
from crypto_decision_lab.paper import PaperSession


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


# ---------------------------------------------------------------- timeliness and week 12
def test_timeliness_counts_late_and_missing_days(repo, monkeypatch):
    _clock(monkeypatch, late_days={3, 10})
    session = PaperSession(_session(repo, 30), repo_root=repo)
    result = session_report.timeliness(session, T0 + 29 * DAY + pd.Timedelta(hours=3))
    assert result["days_expected"] == 30 and result["on_time_days"] == 28
    assert result["late_days"] == [str(T0 + 3 * DAY), str(T0 + 10 * DAY)] and result["missing_days"] == []
    assert result["max_delay_hours"] == pytest.approx(5.0) and result["on_time_share"] == pytest.approx(28 / 30)
    beyond = session_report.timeliness(session, T0 + 33 * DAY)  # the session ended on day 29: days 30-32 never processed
    assert [m[:10] for m in beyond["unexplained_missing_days"]] == ["2024-07-01", "2024-07-02", "2024-07-03"]
    session.close()


def test_week12_pass_late_days_missing_days_and_not_due(repo, monkeypatch):
    _clock(monkeypatch)
    due = T0 + 84 * DAY + pd.Timedelta(hours=1)
    report = session_report.week12(_session(repo, 90, "ok"), repo_root=repo, as_of=due)
    assert report["outcome"] == "OPERATIONAL_PASS", report["criteria"]
    assert report["elapsed_weeks"] == pytest.approx(12.0, abs=0.01) and report["timeliness"]["on_time_share"] == 1.0
    assert report["fidelity"]["match"] is True
    ok_dir = repo / "var" / "paper" / "sessions"
    early = session_report.week12(next(ok_dir.iterdir()), repo_root=repo, as_of=T0 + 20 * DAY)
    assert early["outcome"] == "NOT_DUE" and early["elapsed_weeks"] < 12

    _clock(monkeypatch, late_days={3, 10, 20, 30, 40, 50})  # 6 of 84 days late: 92.9% < 95%
    late = session_report.week12(_session(repo, 90, "late"), repo_root=repo, as_of=due)
    assert late["outcome"] == "OPERATIONAL_FAIL"
    assert _failed(late) == ["daily decisions committed within 2 h on at least 95% of the days"]
    _clock(monkeypatch, late_days={3, 10, 20})  # 3 of 84 days late: 96.4%, still passes
    few = session_report.week12(_session(repo, 90, "few"), repo_root=repo, as_of=due)
    assert few["outcome"] == "OPERATIONAL_PASS"

    _clock(monkeypatch)
    short = session_report.week12(_session(repo, 70, "short"), repo_root=repo, as_of=due)  # runner stopped on day 70
    assert short["outcome"] == "OPERATIONAL_FAIL"
    # a day the runner never processed is also a day that was not on time: both criteria fail
    assert _failed(short) == ["daily decisions committed within 2 h on at least 95% of the days",
                              "no day without decision because of the runner"]
    assert short["timeliness"]["on_time_share"] == pytest.approx(70 / 84)
    assert len(short["timeliness"]["unexplained_missing_days"]) == 14


# ---------------------------------------------------------------- edge
@pytest.fixture
def long_session(repo, monkeypatch):
    _clock(monkeypatch)
    return _session(repo, 240, "edge")


def test_edge_uses_the_realized_net_and_reports_the_open_position(repo, long_session):
    report = session_report.edge(long_session, repo_root=repo, as_of=T0 + 240 * DAY)
    figures = report["figures"]
    session = PaperSession(long_session, repo_root=repo)
    ledger = session.ledger()
    initial = float(session.meta["config"]["initial_cash"])
    session.close()
    trades = analytics.trades(ledger)
    closed = trades[trades["status"] == "CLOSED"]
    assert figures["closed_trades"] == len(closed) >= 8
    assert figures["realized_net_pnl"] == pytest.approx(closed["pnl"].sum())
    assert figures["realized_net_return_pct"] == pytest.approx(100 * closed["pnl"].sum() / initial)
    assert figures["realized_costs"] == pytest.approx(closed["fees"].sum() + closed["slippage"].sum())
    assert figures["realized_gross_pnl"] == pytest.approx(figures["realized_net_pnl"] + figures["realized_costs"])
    assert figures["equity_net_return_pct"] == pytest.approx(analytics.summarize(ledger, initial)["net_return_pct"])
    unrealized = figures["open_position"]["unrealized_pnl"] if figures["open_position"] else 0.0
    # equity result = realized result + the open position, side by side (review R16)
    assert (figures["equity_net_return_pct"] - figures["realized_net_return_pct"]) * initial / 100 == pytest.approx(unrealized)
    assert abs(figures["buy_and_hold_net_return_pct"]) < 100

    criteria = report["criteria"]
    assert len(criteria) == 4
    share = figures["realized_costs_share_of_gross"]
    assert criteria[0]["passed"] == (figures["realized_net_return_pct"] > 0)
    assert criteria[1]["passed"] == (figures["max_drawdown_mtm_pct"] >= -20)
    assert criteria[2]["passed"] == (share is not None and share <= 0.5)
    assert criteria[3]["threshold"] == pytest.approx(figures["buy_and_hold_net_return_pct"] - 10.0)
    assert criteria[3]["passed"] == (figures["equity_net_return_pct"] >= criteria[3]["threshold"])
    assert report["outcome"] == ("CONFIRMED" if all(c["passed"] for c in criteria) else "NOT_CONFIRMED")


def test_edge_is_evaluated_once_and_only_when_due(repo, monkeypatch, long_session, capsys):
    as_of = str((T0 + 240 * DAY).tz_localize(None))
    args = ["edge", "--session-dir", str(long_session), "--repo-root", str(repo), "--as-of", as_of]
    first = session_report.main(args)
    out = json.loads(capsys.readouterr().out)
    assert first in (0, 1) and out["outcome"] in ("CONFIRMED", "NOT_CONFIRMED")
    written = json.loads((long_session / "p1-edge-report.json").read_text(encoding="utf-8"))
    assert written["outcome"] == out["outcome"] and written["figures"] == out["figures"]
    assert session_report.main(args) == 2  # a second evaluation is refused, whatever the figures
    assert "already evaluated" in json.loads(capsys.readouterr().out)["error"]

    _clock(monkeypatch)
    quiet = _session(repo, 20, "quiet")  # fewer than 8 closed trades
    soon = ["edge", "--session-dir", str(quiet), "--repo-root", str(repo),
            "--as-of", str((T0 + 21 * DAY).tz_localize(None))]
    assert session_report.main(soon) == 0
    due = json.loads(capsys.readouterr().out)
    assert due["outcome"] == "NOT_DUE" and "closed trades" in due["reason"]
    assert not (quiet / "p1-edge-report.json").exists()
    late = ["edge", "--session-dir", str(quiet), "--repo-root", str(repo),
            "--as-of", str((T0 + 53 * 7 * DAY).tz_localize(None))]
    assert session_report.main(late) == 1  # after 52 weeks without 8 closed trades
    assert json.loads(capsys.readouterr().out)["outcome"] == "INCONCLUSIVE"
    assert (quiet / "p1-edge-report.json").exists()


def test_week12_report_is_written_once(repo, monkeypatch, capsys):
    _clock(monkeypatch)
    directory = _session(repo, 90, "w12")
    args = ["week12", "--session-dir", str(directory), "--repo-root", str(repo),
            "--as-of", str((T0 + 84 * DAY + pd.Timedelta(hours=1)).tz_localize(None))]
    assert session_report.main(args) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "OPERATIONAL_PASS"
    assert (directory / "p1-week12-report.json").exists()
    assert session_report.main(args) == 0
    assert "not overwritten" in json.loads(capsys.readouterr().out)["note"]
    not_due = ["week12", "--session-dir", str(directory), "--repo-root", str(repo),
               "--as-of", str((T0 + 5 * DAY).tz_localize(None))]
    assert session_report.main(not_due) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "NOT_DUE"
