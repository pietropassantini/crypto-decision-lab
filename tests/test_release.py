"""S08 release: CP08.1 runbook drill, CP08.2 release card, CP08.3 activation decision and mainnet gate."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path
import subprocess
import sys

import pytest

from crypto_decision_lab import release
from crypto_decision_lab.exchange import FakeExchange
from crypto_decision_lab.lab_feed import FEED_SCHEMA, LabFeed
from crypto_decision_lab.live import LiveError, LiveSession
from crypto_decision_lab.release import ActivationError, ReleaseError

SRC = Path(__file__).resolve().parents[1] / "src"
SOURCE = "BTCUSDT-1h-raw-v1"
LIVE_CONFIG = {
    "symbol": "BTCUSDT", "order_type": "MARKET",
    "run_config": {"strategy": {"name": "momentum", "parameters": {"threshold": 0.0}}, "initial_cash": 500,
                   "hypothesis": {"latency_hours": 0, "horizon_hours": 6, "fee_rate": 0.001, "slippage": 0.0005},
                   "risk": {"max_exposure": 0.5, "min_notional": 10, "allow_open": True}},
    "limits": {"max_order_notional": 100, "max_position_notional": 200},
}
STEPS = ["S00A", "S01", "S02", "S03", "S04", "S05", "S06", "S07"]


def criteria(permissive: bool = True) -> dict:
    loose = {"op": ">", "threshold": -1000} if permissive else {"op": ">", "threshold": 1000}
    return {
        "schema_version": release.CRITERIA_SCHEMA, "candidate": "fixture-momentum",
        "candidate_config": "config/live/candidate.json",
        "windows": {"backtest": {"dataset_id": SOURCE, "start": "2024-01-31 00:00", "end": "2024-02-01 12:00"},
                    "paper": {"dataset_id": SOURCE, "start": "2024-02-01 12:00", "end": "2024-02-03 00:00"}},
        "backtest": [{"id": "net_return", "metric": "net_return_pct", **loose}],
        "paper": [{"id": "net_return", "metric": "net_return_pct", **loose},
                  {"id": "trades", "metric": "closed_trades", "op": ">=", "threshold": 1}],
        "operational": {"exchange": "binance-spot", "environment": "mainnet", "max_capital": 500,
                        "max_order_notional": 100, "max_position_notional": 200,
                        "required_steps_verified": STEPS, "testnet_evidence": ["evidence/smoke-*.json"]},
    }


def prepare(repo: Path, *, permissive=True, verified=True, testnet=True, commit=True) -> Path:
    (repo / "config/live").mkdir(parents=True, exist_ok=True)
    (repo / "config/live/candidate.json").write_text(json.dumps(LIVE_CONFIG), encoding="utf-8")
    (repo / "pyproject.toml").write_text('[project]\nname = "fixture"\nversion = "9.9.9"\n', encoding="utf-8")
    status = "VERIFIED" if verified else "IN_REVIEW"
    (repo / "PRD.md").write_text("\n".join(f"| {s} | {status} | x | y | z |" for s in STEPS) + "\n", encoding="utf-8")
    if testnet:
        (repo / "evidence").mkdir(exist_ok=True)
        (repo / "evidence/smoke-1.json").write_text(json.dumps(
            {"started_utc": "2026-11-13T10:00:00Z", "identity": {"environment": "testnet"},
             "steps": [{"step": "market_buy", "ok": True}]}), encoding="utf-8")
    path = repo / "criteria.json"
    path.write_text(json.dumps(criteria(permissive)), encoding="utf-8")
    release.register_criteria(path, repo_root=repo)
    if commit:
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run([*git, "commit", "-q", "-m", "fixture"], cwd=repo, check=True)
    return repo / "releases" / "fixture-momentum"


def card(repo: Path, drill=True):
    return release.build_card("fixture-momentum", repo_root=repo, prd="PRD.md",
                              drill={"run_utc": "now", "package_location": "x", "passed": True} if drill else None)


# ====================================================================== CP08.2
def test_cp082_card_is_ready_only_when_every_check_passes(studio_repo):
    prepare(studio_repo)
    path = card(studio_repo)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["decision"] == "READY", data["reasons"]
    version = data["version"]
    assert version["git_commit"] and version["git_dirty_paths"] == [] and version["package_version"] == "9.9.9"
    assert len(version["code_sha256"]) == 64 and "live.py" in version["code_files"]
    assert data["criteria"]["registered_utc"] <= data["created_utc"]
    paper = data["evidence"]["paper"]
    assert paper["matches_backtest"] and paper["session_status"] == "COMPLETED"
    assert data["evidence"]["backtest"]["window"]["end"] == "2024-02-01 12:00"
    assert data["configuration"]["limits"] == LIVE_CONFIG["limits"]
    assert (path.parent / "release-card.md").read_text(encoding="utf-8").startswith("# Scheda di rilascio")
    with pytest.raises(FileExistsError):  # write-once per code revision
        card(studio_repo)


def test_cp082_failed_checks_block_with_reasons(studio_repo):
    prepare(studio_repo, permissive=False, verified=False, testnet=False, commit=False)
    (studio_repo / "config/live/candidate.json").write_text(
        json.dumps({**LIVE_CONFIG, "limits": {"max_order_notional": 150, "max_position_notional": 200}}),
        encoding="utf-8")
    data = json.loads(card(studio_repo, drill=False).read_text(encoding="utf-8"))
    assert data["decision"] == "BLOCKED"
    failed = {c["id"] for c in data["checks"] if not c["passed"]}
    assert {"backtest.net_return", "paper.net_return", "ops.candidate_config_unchanged", "ops.git_commit",
            "ops.testnet_collaudo", "ops.runbook_drill", "ops.step_S07_verified"} <= failed
    assert "ops.max_order_notional" not in failed  # the registered config (100) is evaluated, not the edited one
    assert len(data["reasons"]) == len(failed)


def test_criteria_are_write_once_and_required_before_the_card(studio_repo):
    with pytest.raises(ReleaseError):
        release.build_card("fixture-momentum", repo_root=studio_repo, prd="PRD.md")
    prepare(studio_repo, commit=False)
    with pytest.raises(FileExistsError):
        release.register_criteria(studio_repo / "criteria.json", repo_root=studio_repo)


def test_backtest_acknowledges_gaps_and_covers_the_window(studio_repo, monkeypatch):
    from crypto_decision_lab import dataset_catalog

    original = dataset_catalog.load_market

    def with_gap(*args, **kwargs):
        market = original(*args, **kwargs)
        frame = market.frame.drop(market.frame.index[40:43])  # three missing hours inside the window
        if dataclasses.is_dataclass(market):
            return dataclasses.replace(market, frame=frame)
        market.frame = frame
        return market

    monkeypatch.setattr(dataset_catalog, "load_market", with_gap)
    window = {"dataset_id": SOURCE, "start": "2024-01-31 00:00", "end": "2024-02-02 00:00"}
    evidence, ledger = release.evaluate_backtest(studio_repo, LIVE_CONFIG["run_config"], window)
    assert evidence["status"] == "COMPLETED" and evidence["gaps_acknowledged"] == 1
    assert evidence["missing_hours"] == 3 and evidence["last_event"] == "2024-02-01 23:00:00+00:00"
    assert [r["kind"] for r in ledger].count("gap_acknowledged") == 1


# ====================================================================== activation gate
def approved(repo: Path) -> Path:
    registry = prepare(repo)
    path = card(repo)
    return release.approve(path, operator="operator", note="first activation with minimal capital")


def test_approval_requires_ready_card(studio_repo):
    prepare(studio_repo, permissive=False, commit=False)
    path = card(studio_repo)
    with pytest.raises(ReleaseError):
        release.approve(path, operator="operator", note="n")


def test_verify_activation_matches_config_exchange_and_code(studio_repo):
    approval = approved(studio_repo)
    from crypto_decision_lab.live import live_digests

    code = live_digests()[0]
    mainnet = {"name": "binance-spot", "environment": "mainnet"}
    assert release.verify_activation(approval, live_config=LIVE_CONFIG, exchange=mainnet, code_sha256=code)
    bigger = {**LIVE_CONFIG, "limits": {"max_order_notional": 1000, "max_position_notional": 200}}
    for kwargs, message in [
        (dict(live_config=bigger, exchange=mainnet, code_sha256=code), "live configuration"),
        (dict(live_config=LIVE_CONFIG, exchange={**mainnet, "environment": "testnet"}, code_sha256=code), "exchange"),
        (dict(live_config=LIVE_CONFIG, exchange=mainnet, code_sha256="0" * 64), "code revision"),
        (dict(live_config={**LIVE_CONFIG, "symbol": "ETHUSDT"}, exchange=mainnet, code_sha256=code), "symbol"),
    ]:
        with pytest.raises(ActivationError, match=message):
            release.verify_activation(approval, **kwargs)
    card_path = approval.parent / "release-card.json"
    card_path.write_text(card_path.read_text(encoding="utf-8").replace('"READY"', '"READY" '), encoding="utf-8")
    with pytest.raises(ActivationError, match="changed after approval"):
        release.verify_activation(approval, live_config=LIVE_CONFIG, exchange=mainnet, code_sha256=code)


def _forge_approval(card_path: Path, code: str) -> Path:
    """Hand-written approval that matches the session and the (possibly forged) card digest."""
    path = card_path.parent / "approval.json"
    path.write_text(json.dumps({
        "schema_version": release.APPROVAL_SCHEMA, "release_id": "fixture-momentum-forged",
        "card_sha256": release.file_sha256(card_path), "code_sha256": code,
        "target": {"exchange": "binance-spot", "environment": "mainnet", "symbol": "BTCUSDT"},
        "live_config_sha256": release.live_config_sha256(LIVE_CONFIG)}), encoding="utf-8")
    return path


def _rewrite(card_path: Path, change) -> None:
    data = json.loads(card_path.read_text(encoding="utf-8"))
    change(data)
    card_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def test_hand_written_minimal_card_is_refused(studio_repo):
    from crypto_decision_lab.live import live_digests

    code = live_digests()[0]
    prepare(studio_repo, commit=False)
    card_path = studio_repo / "releases" / "fixture-momentum" / code[:8] / "release-card.json"
    card_path.parent.mkdir(parents=True)
    card_path.write_text(json.dumps({"release_id": "fixture-momentum-forged", "decision": "READY"}), encoding="utf-8")
    with pytest.raises(ReleaseError, match="unsupported schema"):
        release.approve(card_path, operator="operator", note="n")
    approval = _forge_approval(card_path, code)
    with pytest.raises(ActivationError, match="release card"):
        release.verify_activation(approval, live_config=LIVE_CONFIG, exchange={"name": "binance-spot",
                                  "environment": "mainnet"}, code_sha256=code)


def _flip_to_ready(data):
    for check in data["checks"]:
        check["passed"] = True
    data["decision"], data["reasons"] = "READY", []


def _drop_failed_checks(data):
    data["checks"] = [c for c in data["checks"] if c["passed"]]
    data["decision"], data["reasons"] = "READY", []


def _weaken_thresholds(data):
    for check in data["checks"]:
        if not check["passed"] and check["op"] == ">":
            check["threshold"], check["passed"] = -1e9, True


def _other_criteria(data):
    data["criteria"]["sha256"] = "0" * 64


@pytest.mark.parametrize("tamper, message", [
    (_flip_to_ready, "passed flag inconsistent"),
    (_drop_failed_checks, "check set differs"),
    (_weaken_thresholds, "operator or threshold"),
    (_other_criteria, "criteria digest"),
])
def test_tampered_blocked_card_cannot_be_approved_or_activated(studio_repo, tamper, message):
    from crypto_decision_lab.live import live_digests

    code = live_digests()[0]
    prepare(studio_repo, permissive=False, commit=False)
    card_path = card(studio_repo)
    assert release.validate_card(card_path)["decision"] == "BLOCKED"  # genuine BLOCKED card is valid
    _rewrite(card_path, tamper)
    with pytest.raises(ReleaseError, match=message):
        release.approve(card_path, operator="operator", note="n")
    approval = _forge_approval(card_path, code)
    with pytest.raises(ActivationError, match=message):
        release.verify_activation(approval, live_config=LIVE_CONFIG, exchange={"name": "binance-spot",
                                  "environment": "mainnet"}, code_sha256=code)
    with pytest.raises(ReleaseError, match=message):
        release.record_decision(card_path.parent, decision="ACTIVATED", decided_by="op", reason="r",
                                live_session="x")


def test_approval_must_match_card_fields(studio_repo):
    from crypto_decision_lab.live import live_digests

    code = live_digests()[0]
    approval = approved(studio_repo)
    _rewrite(approval, lambda data: data.update(capital=50_000))
    with pytest.raises(ActivationError, match="approval fields differ"):
        release.verify_activation(approval, live_config=LIVE_CONFIG, exchange={"name": "binance-spot",
                                  "environment": "mainnet"}, code_sha256=code)


def test_mainnet_live_session_requires_matching_approval(studio_repo):
    approval = approved(studio_repo)
    feed = LabFeed.create(studio_repo / "var/lab/mainnet", {
        "schema_version": FEED_SCHEMA, "feed_id": "lab-mainnet", "source_dataset_id": SOURCE,
        "start": "2024-01-30 00:00", "end": "2024-02-03 00:00", "faults": []}, repo_root=studio_repo)
    fake = FakeExchange(studio_repo / "var/mainnet-exchange.json", balances={"BTC": "0", "USDT": "1000"}, price=100)
    spec = {"name": "binance-spot", "environment": "mainnet", "allow_mainnet": True}
    source = {"kind": "lab_feed", "path": str(feed.directory)}
    with pytest.raises(LiveError, match="approved READY release"):
        LiveSession.create("m", LIVE_CONFIG, exchange=spec, source=source, start_at="2024-01-31",
                           repo_root=studio_repo, adapter=fake, feed=feed)
    bigger = {**LIVE_CONFIG, "run_config": {**LIVE_CONFIG["run_config"], "initial_cash": 900}}
    with pytest.raises(LiveError, match="activation refused"):
        LiveSession.create("m", bigger, exchange={**spec, "approval": str(approval)}, source=source,
                           start_at="2024-01-31", repo_root=studio_repo, adapter=fake, feed=feed)
    session = LiveSession.create("m", LIVE_CONFIG, exchange={**spec, "approval": str(approval)}, source=source,
                                 start_at="2024-01-31", repo_root=studio_repo, adapter=fake, feed=feed)
    assert session.meta["activation"]["release_id"].startswith("fixture-momentum-")


# ====================================================================== CP08.3
def test_cp083_decisions_are_recorded_and_gated(studio_repo):
    prepare(studio_repo, permissive=False, commit=False)
    directory = card(studio_repo).parent
    with pytest.raises(ReleaseError):
        release.record_decision(directory, decision="ACTIVATED", decided_by="op", reason="r", live_session="x")
    with pytest.raises(ReleaseError):
        release.record_decision(directory, decision="NOT_ACTIVATED", decided_by="op", reason="r")
    entry = release.record_decision(directory, decision="NOT_ACTIVATED", decided_by="op", reason="card BLOCKED",
                                    open_requirements=["testnet collaudo"], operating_mode="paper")
    assert entry["card_decision"] == "BLOCKED" and release.decisions(directory) == [entry]


def test_cp083_activation_requires_approval_and_live_session(studio_repo):
    approval = approved(studio_repo)
    directory = approval.parent
    with pytest.raises(ReleaseError):
        release.record_decision(directory, decision="ACTIVATED", decided_by="op", reason="go")
    entry = release.record_decision(directory, decision="ACTIVATED", decided_by="op", reason="go",
                                    live_session="var/live/sessions/x")
    assert entry["approval_sha256"] == release.file_sha256(approval)


# ====================================================================== CP08.1
def test_cp081_runbook_drill_through_the_cli(studio_repo, tmp_path):
    result = release.run_drill(studio_repo, tmp_path / "drill", live_config=LIVE_CONFIG,
                               feed_start="2024-01-30 00:00", feed_end="2024-02-03 00:00", candidate_hours=4,
                               env={"PYTHONPATH": str(SRC)})
    failed = [s for s in result["steps"] if s["result"] != "PASS"]
    assert not failed, json.dumps(failed, indent=2)
    assert {s["id"] for s in result["steps"]} >= {"install", "a.crash_after_send", "a.resume_reconcile",
                                                  "a.suspension", "a.balance_mismatch", "a.rebaseline",
                                                  "a.flatten_finish", "b.restart_with_open_order",
                                                  "b.reconcile_partial", "b.cancel_and_close", "c.candidate_run"}
    assert Path(result["package_location"]).is_relative_to(SRC)
