"""S09 operational report: CP09.1 reproducible, reconciled report; deviations become hypotheses."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pandas as pd
import pytest

from crypto_decision_lab import ops_report
from crypto_decision_lab.exchange import FakeExchange
from crypto_decision_lab.lab_feed import FEED_SCHEMA, LabFeed
from crypto_decision_lab.live import LiveSession
from crypto_decision_lab.paper import PaperSession

SRC = Path(__file__).resolve().parents[1] / "src"
SOURCE = "BTCUSDT-1h-raw-v1"
CONSTANT = {
    "symbol": "BTCUSDT",
    "run_config": {"strategy": {"name": "constant", "parameters": {"exposure": "LONG"}}, "initial_cash": 1000,
                   "hypothesis": {"latency_hours": 0, "horizon_hours": 3, "fee_rate": 0.001, "slippage": 0.0005},
                   "risk": {"max_exposure": 0.5, "min_notional": 10, "allow_open": True}},
    "limits": {"max_order_notional": 400, "max_position_notional": 600},
}
FEED = dict(feed_start="2024-01-30 00:00", feed_end="2024-02-02 00:00")


@pytest.fixture
def fixture_session(studio_repo, tmp_path):
    return ops_report.build_fixture_session(studio_repo, tmp_path / "fixture", live_config=CONSTANT,
                                            dataset_id=SOURCE, **FEED)


def test_cp091_fixture_report_is_reproducible_and_reconciled(fixture_session):
    first = ops_report.build_report(fixture_session, label="fixture-live (FakeExchange, lab feed)")
    second = ops_report.build_report(fixture_session, label="fixture-live (FakeExchange, lab feed)")
    assert first["content_sha256"] == second["content_sha256"]
    content = first["content"]
    assert content["mode"] == "live-fixture" and content["data_label"].startswith("fixture-live")
    assert content["feed"]["feed_id"] == "s09-fixture" and content["version"]["code_sha256"]
    assert all(check["ok"] for check in content["reconciliation"]), content["reconciliation"]
    statuses = content["fills"]["by_status"]
    assert statuses.get("REJECTED") == 1 and statuses.get("EXPIRED") == 1
    assert {m["status"] for m in content["fills"]["missing"]} >= {"REJECTED", "EXPIRED"}
    # the timed-out order was reconciled, not lost
    assert all(o["status"] not in ("UNKNOWN", "INTENT") for o in content["orders"])
    assert content["slippage"]["bps"]["mean"] == pytest.approx(10, abs=0.1)
    assert content["slippage"]["assumed_bps"] == pytest.approx(5)
    assert content["costs"]["observed_fee_bps"] == pytest.approx(10, abs=0.05)
    ids = {h["id"] for h in content["hypotheses"]}
    assert {"H-slippage", "H-fills"} <= ids
    for hypothesis in content["hypotheses"]:
        assert hypothesis["metric"] and hypothesis["checkpoint"]
    assert content["latency"]["decision_to_intent_s"]["max"] == max(o["decision_latency_s"] for o in content["orders"])
    assert content["signals"]["agreement"] == 1.0  # same features, same strategy
    assert content["signals"]["position_agreement"] < 1.0  # rejected / partial orders change the position path
    outcome = content["outcome"]
    assert outcome["observed_gross_pnl"] == pytest.approx(outcome["observed_net_pnl"] + outcome["observed_costs"])
    markdown = ops_report.render(first)
    assert "fixture-live" in markdown and "H-slippage" in markdown and "KO" not in markdown


def test_report_totals_match_the_register(fixture_session):
    report = ops_report.build_report(fixture_session, label="fixture")["content"]
    register = ops_report.load_register(fixture_session)
    executions = [r for r in register["ledger"] if r["kind"] == "execution"]
    assert report["costs"]["fees_quote"] == pytest.approx(sum(r["fee"] for r in executions))
    assert report["costs"]["notional"] == pytest.approx(sum(r["quote"] for r in executions))
    assert report["fills"]["executed_quantity"] == pytest.approx(sum(float(f["quantity"]) for f in register["fills"]))
    assert sum(report["fills"]["by_status"].values()) == len(register["orders"])


def _register(request: dict, fills: list[dict], status: str = "FILLED") -> dict:
    return {"meta": {"symbol_rules": {"base_asset": "BTC", "quote_asset": "USDT"}},
            "ledger": [{"kind": "market", "timestamp": "2024-01-31 00:00:00+00:00", "open": 100.0}],
            "fills": [{"client_order_id": "c1", "commission": "0", "commission_asset": "BNB",
                       "recorded_at": "2024-01-31T00:00:05Z", **fill} for fill in fills],
            "orders": [{"client_order_id": "c1", "side": "BUY", "purpose": "open", "status": status,
                        "bar_ts": "2024-01-31T00:00:00Z", "created_at": "2024-01-31T00:00:02Z", "detail": "",
                        "request": json.dumps({"order_type": "MARKET", **request})}]}


def test_f5_quote_sized_market_fill_ratio_is_not_read_from_slippage():
    # 400 USDT spent at 2 % above the reference open: 3.92 BTC instead of the 4 BTC estimated from the open
    spent = [{"trade_id": 1, "price": "102", "quantity": "3.92156863"}]
    orders, _ = ops_report._live_orders(_register({"quote_quantity": "400"}, spent), {})
    assert orders[0]["fill_ratio"] == pytest.approx(1.0, abs=1e-6) and orders[0]["requested_quote"] == 400.0
    assert orders[0]["slippage_bps"] == pytest.approx(200, abs=0.1)
    # only half of the quote amount was spent: the shortfall is still reported
    half = [{"trade_id": 1, "price": "102", "quantity": "1.96078431"}]
    orders, _ = ops_report._live_orders(_register({"quote_quantity": "400"}, half, "PARTIALLY_FILLED"), {})
    assert orders[0]["fill_ratio"] == pytest.approx(0.5, abs=1e-6)
    # quantity-sized orders keep the quantity ratio
    sized = [{"trade_id": 1, "price": "100", "quantity": "0.75"}]
    orders, _ = ops_report._live_orders(_register({"quantity": "1"}, sized, "PARTIALLY_FILLED"), {})
    assert orders[0]["fill_ratio"] == pytest.approx(0.75) and orders[0]["requested_quote"] is None


def test_clean_live_session_has_no_execution_hypotheses(studio_repo, tmp_path):
    config = {**CONSTANT, "limits": {"max_order_notional": 1e9, "max_position_notional": 1e9},
              "run_config": {**CONSTANT["run_config"],
                             "hypothesis": {**CONSTANT["run_config"]["hypothesis"], "slippage": 0.0}}}
    feed = LabFeed.create(tmp_path / "feed", {"schema_version": FEED_SCHEMA, "feed_id": "clean",
                                              "source_dataset_id": SOURCE, "start": FEED["feed_start"],
                                              "end": FEED["feed_end"], "faults": []}, repo_root=studio_repo)
    start = pd.Timestamp(FEED["feed_start"], tz="UTC") + pd.Timedelta(hours=26)
    feed.advance(until=start - pd.Timedelta(hours=1))
    fake = FakeExchange(tmp_path / "exchange.json", balances={"BTC": "0", "USDT": "5000"}, price=1)
    session = LiveSession.create("clean", config, exchange={"name": "fake", "state_path": str(fake.path)},
                                 source={"kind": "lab_feed", "path": str(feed.directory)}, start_at=start,
                                 root=tmp_path / "live", repo_root=studio_repo, adapter=fake, feed=feed)
    for _ in range(20):
        feed.advance(1)
        session.poll()
    content = ops_report.build_report(session.directory, label="fixture-clean")["content"]
    ids = {h["id"] for h in content["hypotheses"]}
    assert not ids & {"H-slippage", "H-fills", "H-latency", "H-signals"}
    assert content["slippage"]["bps"]["mean"] == pytest.approx(0, abs=1e-9)
    assert content["signals"]["agreement"] == 1.0 and content["signals"]["position_agreement"] == 1.0
    # live (fee in base asset, full quote budget) and simulation (fee in quote) differ only by fee mechanics
    assert abs(content["outcome"]["difference"]) < 0.01 * 1000


def test_paper_session_report(studio_repo):
    session = PaperSession.create("report", CONSTANT["run_config"], {"kind": "snapshot", "dataset_id": SOURCE},
                                  start_at="2024-01-31 02:00", end_at="2024-02-02 20:00", repo_root=studio_repo)
    session.poll()
    report = ops_report.build_report(session.directory, label="paper-replay")["content"]
    assert report["mode"] == "paper-replay" and report["register"]["kind"] == "paper"
    assert all(check["ok"] for check in report["reconciliation"])
    assert report["slippage"]["bps"]["mean"] == pytest.approx(5, abs=1e-6)  # assumed by construction
    assert report["costs"]["observed_fee_bps"] == pytest.approx(10, rel=1e-6)
    assert "H-fills" not in {h["id"] for h in report["hypotheses"]}


def test_cp092_preregistered_experiment_is_complete_and_resumable():
    spec_path = SRC.parent / "config/experiments/S09-E01-turnover-reduction-v1.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    for key in ("hypothesis", "inputs", "baseline", "metrics", "selection_rule", "acceptance", "checkpoints",
                "prompt"):
        assert spec[key], key
    inputs = spec["inputs"]
    assert inputs["dataset_id"] != "BTCUSDT-1h-forward-v1" and "forward" not in inputs["dataset_id"]
    windows = [inputs["windows"][k] for k in ("development", "validation", "confirmation")]
    for earlier, later in zip(windows, windows[1:]):
        assert pd.Timestamp(earlier["end"]) <= pd.Timestamp(later["start"])  # chronological, no overlap
    assert len(inputs["grid"]["threshold"]) * len(inputs["grid"]["horizon_hours"]) == 12
    assert set(spec["acceptance"]["outcomes"]) == {"CANDIDATE", "INCONCLUSIVE", "REJECTED"}
    assert {c["id"] for c in spec["checkpoints"]} == {"CP-E01.1", "CP-E01.2", "CP-E01.3"}
    assert "S09-E01" in spec["prompt"] and "data/forward" in spec["prompt"]
    prd = (SRC.parent / "Crypto_Decision_Lab_PRD.md").read_text(encoding="utf-8")
    assert "| E01 |" in prd and "S09-E01-turnover-reduction-v1" in prd


def test_cli_writes_json_and_markdown(fixture_session, tmp_path):
    out = tmp_path / "report"
    result = subprocess.run([sys.executable, "-m", "crypto_decision_lab.ops_report", "report", "--session-dir",
                             str(fixture_session), "--label", "fixture-live", "--out", str(out)],
                            capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(SRC)})
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["reconciled"] and "H-slippage" in summary["hypotheses"]
    saved = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert saved["content_sha256"] == summary["content_sha256"]
    assert (out / "report.md").read_text(encoding="utf-8").startswith("# Report operativo")
