"""S06 persistent paper trading: CP06.1 resume, CP06.2 lab feed lifecycle, CP06.3 pinned versions."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab import paper, studio_analytics as analytics
from crypto_decision_lab.dataset_catalog import Catalog, CatalogAdmissionError, load_market
from crypto_decision_lab.forward_isolation import ForwardIsolationViolation
from crypto_decision_lab.lab_feed import FEED_SCHEMA, LabFeed
from crypto_decision_lab.paper import PaperSession, SessionError
from crypto_decision_lab.simulation import market_events, run
from crypto_decision_lab.studio import build_run_config

SOURCE = "BTCUSDT-1h-raw-v1"
SRC = Path(__file__).resolve().parents[1] / "src"
MOMENTUM = {"strategy": {"name": "momentum", "parameters": {"threshold": 0.0}}, "initial_cash": 10000,
            "hypothesis": {"latency_hours": 1, "horizon_hours": 6, "fee_rate": 0.001, "slippage": 0.0005},
            "risk": {"max_exposure": 0.8, "min_notional": 10, "allow_open": True}}
CONSTANT = {**MOMENTUM, "strategy": {"name": "constant", "parameters": {"exposure": "LONG"}}}
START, END = "2024-01-31 02:00", "2024-02-02 20:00"
SCENARIO = {
    "schema_version": FEED_SCHEMA, "feed_id": "lab-fixture", "source_dataset_id": SOURCE,
    "start": "2024-01-30 00:00", "end": "2024-02-03 00:00",
    "faults": [
        {"type": "late", "at": "2024-01-31 10:00", "delay_hours": 3},
        {"type": "invalid", "at": "2024-01-31 20:00", "correct_after_hours": 2},
        {"type": "outage", "from": "2024-02-01 06:00", "to": "2024-02-01 12:00"},
        {"type": "gap", "at": "2024-02-02 03:00"},
    ],
}


@pytest.fixture(autouse=True)
def _no_crash_hook():
    paper.CRASH_HOOK = None
    yield
    paper.CRASH_HOOK = None


def replay(repo, name="replay", root="var/paper", config=MOMENTUM):
    return PaperSession.create(name, config, {"kind": "snapshot", "dataset_id": SOURCE}, start_at=START,
                               end_at=END, root=root, repo_root=repo)


def reference_ledger(repo, config=MOMENTUM):
    market = load_market(Catalog.load(repo / "config/dataset-catalog.json"), SOURCE, repo_root=repo)
    return run(market_events(market.frame, START, END), build_run_config(config)).ledger


# ------------------------------------------------------------------ CP03.2 on the persistent path
def test_replay_session_matches_backtest_ledger(studio_repo):
    session = replay(studio_repo)
    snapshot = session.poll()
    assert snapshot["status"] == "COMPLETED"
    ledger = session.ledger()
    assert ledger == reference_ledger(studio_repo)
    assert any(r["kind"] == "execution" for r in ledger)
    assert [r["sequence"] for r in ledger] == list(range(len(ledger)))


# ------------------------------------------------------------------ CP06.1
class Crash(Exception):
    pass


@pytest.mark.parametrize("stage", ["before_commit", "after_commit"])
@pytest.mark.parametrize("at_step", [1, 7, 30])
def test_cp061_interrupted_session_resumes_identically(studio_repo, stage, at_step):
    expected = reference_ledger(studio_repo)
    session = replay(studio_repo, name=f"crash-{stage}-{at_step}")
    calls = {"n": 0}

    def hook(point, timestamp):
        if point == stage:
            calls["n"] += 1
            if calls["n"] == at_step:
                raise Crash(f"{point} at {timestamp}")

    paper.CRASH_HOOK = hook
    with pytest.raises(Crash):
        session.poll()
    session.close()
    paper.CRASH_HOOK = None
    resumed = PaperSession(session.directory, repo_root=studio_repo)
    persisted = resumed.snapshot()["events"]
    assert persisted == (at_step - 1 if stage == "before_commit" else at_step)
    assert resumed.poll()["status"] == "COMPLETED"
    ledger = resumed.ledger()
    assert ledger == expected
    assert [r["sequence"] for r in ledger] == list(range(len(ledger)))
    markets = [r["timestamp"] for r in ledger if r["kind"] == "market"]
    assert len(markets) == len(set(markets))
    end = [r for r in ledger if r["kind"] == "end"][0]
    assert end["cash"] == [r for r in expected if r["kind"] == "end"][0]["cash"]


@pytest.mark.parametrize("stage", ["before_commit", "after_commit"])
def test_cp061_hard_kill_of_runner_process(studio_repo, stage):
    session = replay(studio_repo, name=f"kill-{stage}")
    session.close()
    code = (
        "import sys; sys.path.insert(0, r'%s')\n"
        "from pathlib import Path\n"
        "from crypto_decision_lab.paper import PaperSession\n"
        "PaperSession(Path(r'%s'), repo_root=r'%s').poll()\n"
    ) % (SRC, session.directory, studio_repo)
    env = {**os.environ, "CDL_PAPER_CRASH": f"{stage}:12"}
    result = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=300)
    assert result.returncode == 97, result.stderr
    resumed = PaperSession(session.directory, repo_root=studio_repo)
    assert resumed.snapshot()["events"] == (11 if stage == "before_commit" else 12)
    resumed.poll()
    assert resumed.ledger() == reference_ledger(studio_repo)


def test_stale_version_is_refused(studio_repo):
    session = replay(studio_repo, name="concurrency")
    other = PaperSession(session.directory, repo_root=studio_repo)
    version, state_json, _, _ = session._row()
    other.poll(max_events=1)
    engine = session._engine(state_json)
    session.connection.execute("BEGIN IMMEDIATE")
    with pytest.raises(paper.ConcurrentUpdate):
        session._commit_step(version, engine, [], None, "RUNNING", "stale", None, None, None)
    session.connection.execute("ROLLBACK")


# ------------------------------------------------------------------ CP06.2
def test_cp062_lab_feed_is_reproducible(studio_repo):
    a = LabFeed.create("var/lab-feeds/a", SCENARIO, repo_root=studio_repo)
    b = LabFeed.create("var/lab-feeds/b", SCENARIO, repo_root=studio_repo)
    a.advance(until="2024-02-03 00:00")
    for _ in range(80):
        b.advance(1)
    b.advance(until="2024-02-03 00:00")
    assert (a.directory / "bars.jsonl").read_bytes() == (b.directory / "bars.jsonl").read_bytes()
    assert a.identity() == b.identity()


def test_cp062_lab_feed_lifecycle_with_explicit_states(studio_repo):
    feed = LabFeed.create("var/lab-feeds/life", SCENARIO, repo_root=studio_repo)
    session = PaperSession.create("life", MOMENTUM, {"kind": "lab_feed", "path": "var/lab-feeds/life"},
                                  start_at=START, stale_after_hours=2, repo_root=studio_repo)
    assert session.snapshot()["status"] == "CREATED"

    def step(until):
        feed.advance(until=until)
        return session.poll()

    s = step("2024-01-31 05:00")
    assert (s["status"], s["last_event"]) == ("WAITING", "2024-01-31 05:00:00+00:00")
    s = step("2024-01-31 11:00")  # 10:00 is late: 11:00 already published
    assert s["status"] == "PAUSED_GAP" and "2024-01-31 10:00" in s["detail"]
    s = step("2024-01-31 13:00")  # late bar arrives: automatic recovery
    assert (s["status"], s["last_event"]) == ("WAITING", "2024-01-31 13:00:00+00:00")
    s = step("2024-01-31 21:00")  # 20:00 published without open
    assert s["status"] == "DATA_ERROR" and "open missing" in s["detail"]
    s = step("2024-01-31 22:00")  # correction published
    assert (s["status"], s["last_event"]) == ("WAITING", "2024-01-31 22:00:00+00:00")
    s = step("2024-02-01 09:00")  # outage: nothing after 05:00 for 3h of feed clock
    assert s["status"] == "STALE" and s["last_event"] == "2024-02-01 05:00:00+00:00"
    s = step("2024-02-01 12:00")  # backfill
    assert (s["status"], s["last_event"]) == ("WAITING", "2024-02-01 12:00:00+00:00")
    s = step("2024-02-02 06:00")  # 03:00 is never published
    assert s["status"] == "PAUSED_GAP" and s["last_event"] == "2024-02-02 02:00:00+00:00"
    assert step("2024-02-02 08:00")["status"] == "PAUSED_GAP"
    with pytest.raises(SessionError):
        session.resume()
    s = session.acknowledge_gap("bar lost by the lab exchange")
    assert s["status"] == "RUNNING"
    s = session.poll()
    assert (s["status"], s["last_event"]) == ("WAITING", "2024-02-02 08:00:00+00:00")
    assert session.suspend("maintenance")["status"] == "SUSPENDED"
    assert step("2024-02-02 10:00")["last_event"] == "2024-02-02 08:00:00+00:00"  # no progress while suspended
    session.resume()
    assert session.poll()["last_event"] == "2024-02-02 10:00:00+00:00"
    assert session.finish("end of test")["status"] == "COMPLETED"

    ledger = session.ledger()
    gap = [r for r in ledger if r["kind"] == "gap_acknowledged"]
    assert len(gap) == 1 and gap[0]["timestamp"] == "2024-02-02 03:00:00+00:00"
    assert ledger[-1]["kind"] == "end"
    assert all(check["passed"] for check in analytics.reconcile(ledger, 10000))
    statuses = [row["status"] for row in session.status_log()]
    for expected in ("CREATED", "WAITING", "PAUSED_GAP", "DATA_ERROR", "STALE", "RUNNING", "SUSPENDED", "COMPLETED"):
        assert expected in statuses
    # The bars a session received are exactly the published, valid ones: no future data, no duplicates.
    markets = [r["timestamp"] for r in ledger if r["kind"] == "market"]
    assert len(markets) == len(set(markets)) and "2024-02-02 03:00:00+00:00" not in markets


def test_revised_or_conflicting_bar_is_a_data_error(studio_repo):
    feed = LabFeed.create("var/lab-feeds/rev", SCENARIO, repo_root=studio_repo)
    session = PaperSession.create("rev", MOMENTUM, {"kind": "lab_feed", "path": "var/lab-feeds/rev"},
                                  start_at=START, repo_root=studio_repo)
    feed.advance(until="2024-01-31 05:00")
    session.poll()
    line = {"timestamp": "2024-01-31 03:00:00+00:00", "open": 1.0, "prev_close": 1.0, "revision": 5,
            "published_at": "2024-01-31 05:00:00+00:00"}
    with (feed.directory / "bars.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(line) + "\n")
    before = session.snapshot()["events"]
    s = session.poll()
    assert s["status"] == "DATA_ERROR" and "conflicting" in s["detail"]
    assert s["events"] == before


def test_lab_feed_and_sessions_stay_outside_cp08(studio_repo):
    with pytest.raises(CatalogAdmissionError):
        LabFeed.create("var/lab-feeds/fw", {**SCENARIO, "source_dataset_id": "BTCUSDT-1h-forward-v1"},
                       repo_root=studio_repo)
    with pytest.raises(ForwardIsolationViolation):
        LabFeed.create("data/forward/x/feed", SCENARIO, repo_root=studio_repo)
    with pytest.raises(ForwardIsolationViolation):
        replay(studio_repo, root="data/forward/x/paper")


# ------------------------------------------------------------------ CP06.3
def _model(path: Path, seed: int) -> dict:
    from crypto_decision_lab.decision_model import build_artefact, train_logistic

    rng = np.random.default_rng(seed)
    X = rng.normal(size=(100, 1))
    y = (X[:, 0] > 0).astype(int)
    document = build_artefact(train_logistic(X, y, ["momentum_24h"], seed=seed), score_threshold=0.5,
                              provenance={"seed": seed})
    path.write_text(json.dumps(document), encoding="utf-8")
    return document


def test_cp063_pinned_config_model_and_independent_sessions(studio_repo):
    LabFeed.create("var/lab-feeds/two", SCENARIO, repo_root=studio_repo).advance(until="2024-01-31 23:00")
    source = {"kind": "lab_feed", "path": "var/lab-feeds/two"}
    first = PaperSession.create("v1", MOMENTUM, source, start_at=START, repo_root=studio_repo)
    second = PaperSession.create("v2", CONSTANT, source, start_at=START, repo_root=studio_repo)
    assert first.directory != second.directory
    first.poll()
    assert second.snapshot()["events"] == 0  # state is independent
    second.poll()
    assert first.ledger() != second.ledger()
    reopened = PaperSession(first.directory, repo_root=studio_repo)
    assert reopened.meta["config"]["strategy"]["name"] == "momentum"
    assert reopened.ledger() == first.ledger()

    model_path = studio_repo / "model.json"
    original = _model(model_path, seed=1)
    pinned = {**MOMENTUM, "strategy": {"name": "model_filtered_momentum", "parameters": {
        "model_path": str(model_path), "model_sha256": original["model_sha256"], "threshold": 0.0}}}
    with_model = PaperSession.create("model", pinned, source, start_at=START, repo_root=studio_repo)
    with_model.poll()
    events = with_model.snapshot()["events"]
    original_bytes = model_path.read_bytes()
    _model(model_path, seed=2)  # a different model replaces the file
    swapped = PaperSession(with_model.directory, repo_root=studio_repo)
    assert swapped.snapshot()["status"] == "MODEL_ERROR"
    assert swapped.poll()["events"] == events  # never runs with another model
    model_path.write_bytes(original_bytes)
    restored = PaperSession(with_model.directory, repo_root=studio_repo)
    assert restored.snapshot()["status"] == "RUNNING"
    assert restored.meta["config"]["strategy"]["parameters"]["model_sha256"] == original["model_sha256"]


def test_cp063_engine_revision_is_pinned(studio_repo, monkeypatch):
    session = replay(studio_repo, name="engine")
    session.poll(max_events=3)
    monkeypatch.setattr(paper, "engine_digests", lambda: ("f" * 64, {}))
    changed = PaperSession(session.directory, repo_root=studio_repo)
    assert changed.snapshot()["status"] == "ENGINE_MISMATCH"
    assert changed.poll()["events"] == 3
    assert changed.accept_engine_revision("reviewed change")["status"] == "RUNNING"
    assert changed.poll()["status"] == "COMPLETED"
