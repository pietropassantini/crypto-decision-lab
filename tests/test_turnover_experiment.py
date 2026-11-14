"""E01 (S09): preregistered turnover-reduction runner (synthetic catalog only)."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab import turnover_experiment as e01
from crypto_decision_lab.dataset_catalog import Catalog, load_market
from crypto_decision_lab.pattern_formation_experiment import ExperimentError

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "config" / "experiments" / "S09-E01-turnover-reduction-v1.json"
RESEARCH_ID = "BTCUSDT-1h-raw-v1"
DAYS = 150
WINDOWS = {
    "development": {"start": "2024-01-05 00:00", "end": "2024-03-15 00:00", "use": "selection only"},
    "validation": {"start": "2024-03-15 00:00", "end": "2024-04-20 00:00", "use": "one evaluation"},
    "confirmation": {"start": "2024-04-20 00:00", "end": "2024-05-29 00:00", "use": "one evaluation"},
}


def _write_market(root: Path) -> None:
    rng = np.random.default_rng(5)
    hours = DAYS * 24
    returns = rng.normal(0.0002, 0.008, hours) + 0.004 * np.sin(np.arange(hours) / 53.0)
    opens = 100.0 * np.exp(np.cumsum(returns))
    closes = np.append(opens[1:], opens[-1] * 1.001)
    index = pd.date_range("2024-01-01", periods=hours, freq="1h", tz="UTC")
    frame = pd.DataFrame({"timestamp": index, "open": opens, "high": np.maximum(opens, closes) * 1.002,
                          "low": np.minimum(opens, closes) * 0.998, "close": closes,
                          "volume": rng.uniform(5, 15, hours)})
    frame = frame.drop(index=[1000])  # one missing hour inside development: acknowledged gap
    chunk = root / "data" / "chunks" / "BTCUSDT" / "research.parquet"
    chunk.parent.mkdir(parents=True)
    frame.to_parquet(chunk, index=False)
    snapshot = root / "data" / "snapshots" / RESEARCH_ID
    snapshot.mkdir(parents=True)
    (snapshot / "raw-manifest.json").write_text(json.dumps({
        "dataset_id": RESEARCH_ID, "source_manifest_sha256": hashlib.sha256(b"e01-fixture").hexdigest(), "chunks": [
            {"file": "chunks/BTCUSDT/research.parquet", "sha256": hashlib.sha256(chunk.read_bytes()).hexdigest()}]}),
        encoding="utf-8")
    (root / "config").mkdir()
    (root / "config" / "dataset-catalog.json").write_text(json.dumps({
        "schema_version": "cdl.s02.catalog.v1",
        "sources": [{"dataset_id": RESEARCH_ID, "path": f"data/snapshots/{RESEARCH_ID}", "role": "research"},
                    {"dataset_id": "BTCUSDT-1h-forward-v1", "path": "data/forward/BTCUSDT-1h-forward-v1",
                     "role": "reserved-forward"}],
        "reserved_dataset_ids": ["BTCUSDT-1h-forward-v1"]}), encoding="utf-8")


def _spec(root: Path, source_sha: str, name="spec.json", **overrides) -> Path:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    spec["inputs"]["windows"] = copy.deepcopy(WINDOWS)
    spec["inputs"]["source_manifest_sha256"] = source_sha
    spec.update(overrides)
    path = root / name
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    root = tmp_path_factory.mktemp("e01") / "repo"
    root.mkdir()
    _write_market(root)
    market = load_market(Catalog.load(root / "config" / "dataset-catalog.json"), RESEARCH_ID, repo_root=root)
    spec = _spec(root, market.source_manifest_sha256)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(e01, "SELECTION_MIN_TRADES", 1)  # synthetic windows are short
        selection = e01.develop(spec, repo_root=root, output_dir="var/run-a")
        report = e01.evaluate(spec, repo_root=root, output_dir="var/run-a")
        second = e01.develop(spec, repo_root=root, output_dir="var/run-b")
        second_report = e01.evaluate(spec, repo_root=root, output_dir="var/run-b")
    return {"root": root, "spec": spec, "selection": selection, "report": report, "second": second_report,
            "out": root / "var" / "run-a"}


# ------------------------------------------------------------- spec
def test_repository_spec_is_preregistered_and_matches_the_backlog_digest():
    spec, sha = e01.load_spec(SPEC)
    assert spec["status"] == "PREREGISTERED"
    assert sha == "d5527a6a9ab79a2acc22ae19358b8dcd66d1c4aa0cc32c0d63048f9d01ecc47f"
    configurations = e01.grid_configurations(spec)
    assert [c["config_id"] for c in configurations] == [f"g{i:02d}" for i in range(12)]
    assert configurations[0]["grid"] == {"threshold": 0.005, "horizon_hours": 12}
    assert configurations[-1]["grid"] == {"threshold": 0.03, "horizon_hours": 48}
    for configuration in configurations:
        config = configuration["run_config"]
        assert config["strategy"]["name"] == "momentum" and config["initial_cash"] == 500
        assert config["hypothesis"]["fee_rate"] == 0.001 and config["hypothesis"]["slippage"] == 0.0005
    baseline = e01.baseline_configuration(spec)
    assert baseline["strategy"]["parameters"] == {"threshold": 0.0} and baseline["hypothesis"]["horizon_hours"] == 6


@pytest.mark.parametrize("change", [{"status": "DRAFT"}, {"schema_version": "cdl.e02.experiment.v1"}])
def test_spec_refused(tmp_path, change):
    with pytest.raises(ExperimentError):
        e01.load_spec(_spec(tmp_path, "0" * 64, **change))


# ------------------------------------------------------------- run
def test_selection_is_locked_before_validation_and_uses_development_only(lab):
    out, selection, report = lab["out"], lab["selection"], lab["report"]
    locked = json.loads((out / "selection.json").read_text(encoding="utf-8"))
    assert locked["locked_utc"] <= report["evaluated_utc"]
    assert set(locked["development"]) == {f"g{i:02d}" for i in range(12)}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(e01, "SELECTION_MIN_TRADES", 1)
        assert e01.select(locked["development"], sorted(locked["development"]))[0] == selection["selected"]
    assert report["selection"]["selected"] == selection["selected"]
    assert report["selection"]["selection_sha256"] == e01._file_sha256(out / "selection.json")
    assert report["outcome"] in e01.OUTCOMES
    assert any(r["gaps_acknowledged"] for r in locked["development"].values())
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["files"].items():
        assert e01._file_sha256(out / name) == digest


def test_run_is_reproducible(lab):
    assert e01.ledger_digests(lab["report"]) == e01.ledger_digests(lab["second"])
    assert len(e01.ledger_digests(lab["report"])) == 17  # 12 grid + baseline on dev, 2 + 2 on val/conf
    assert lab["report"]["outcome"] == lab["second"]["outcome"]


def test_phases_are_write_once_and_ordered(lab):
    with pytest.raises(FileExistsError):
        e01.develop(lab["spec"], repo_root=lab["root"], output_dir="var/run-a")
    with pytest.raises(FileExistsError):
        e01.evaluate(lab["spec"], repo_root=lab["root"], output_dir="var/run-a")
    with pytest.raises(ExperimentError, match="phase-1"):
        e01.evaluate(lab["spec"], repo_root=lab["root"], output_dir="var/never-developed")


def test_wrong_source_refused_after_the_lock(lab):
    wrong = _spec(lab["root"], "f" * 64, name="wrong.json")
    with pytest.raises(ExperimentError, match="source manifest"):
        e01.develop(wrong, repo_root=lab["root"], output_dir="var/run-wrong")
    assert (lab["root"] / "var" / "run-wrong" / "preregistration.json").is_file()


def test_forward_output_refused(lab):
    with pytest.raises(Exception, match="forward"):
        e01.develop(lab["spec"], repo_root=lab["root"], output_dir="data/forward/BTCUSDT-1h-forward-v1/x")


def test_default_minimum_without_eligible_configuration_is_inconclusive(lab):
    selection = e01.develop(lab["spec"], repo_root=lab["root"], output_dir="var/run-strict")
    if selection["selected"] is None:  # synthetic development may have fewer than 30 trades per configuration
        report = e01.evaluate(lab["spec"], repo_root=lab["root"], output_dir="var/run-strict")
        assert report["outcome"] == "INCONCLUSIVE" and "validation" not in report


# ------------------------------------------------------------- rules
def _result(**values):
    base = {"net_return_pct": 5.0, "max_drawdown_mtm_pct": -10.0, "closed_trades": 20,
            "closed_trades_per_year": 40.0, "costs": 10.0, "gross_pnl": 40.0}
    return {**base, **values}


def test_validation_criteria_follow_the_spec_text():
    baseline = _result(closed_trades_per_year=200.0)
    assert all(c["passed"] for c in e01.validation_criteria(_result(), baseline))
    cases = {
        "net_return_pct > 0": _result(net_return_pct=0.0),
        "max_drawdown_mtm_pct >= -25": _result(max_drawdown_mtm_pct=-25.01),
        "closed_trades >= 15": _result(closed_trades=14),
        "closed_trades_per_year <= 30% of the baseline on the same window": _result(closed_trades_per_year=60.01),
        "costs <= 50% of the gross result (net + costs)": _result(costs=20.01),
    }
    for failing, result in cases.items():
        failed = [c["criterion"] for c in e01.validation_criteria(result, baseline) if not c["passed"]]
        assert failed == [failing]
    # gross result <= 0: costs can never be at most half of it
    loss = [c for c in e01.validation_criteria(_result(gross_pnl=-1.0), baseline) if not c["passed"]]
    assert [c["criterion"] for c in loss] == ["costs <= 50% of the gross result (net + costs)"]


def test_outcome_rules():
    baseline = _result(closed_trades_per_year=200.0)
    ok = {"selected": _result(), "criteria": e01.validation_criteria(_result(), baseline)}
    conf_ok = {"criteria": e01.confirmation_criteria(_result())}
    conf_bad = {"criteria": e01.confirmation_criteria(_result(net_return_pct=-1.0))}
    bad = {"selected": _result(net_return_pct=-1.0),
           "criteria": e01.validation_criteria(_result(net_return_pct=-1.0), baseline)}
    few = {"selected": _result(closed_trades=3), "criteria": e01.validation_criteria(_result(closed_trades=3), baseline)}
    assert e01.decide(ok, conf_ok)[0] == "CANDIDATE"
    assert e01.decide(bad, conf_ok)[0] == "REJECTED"
    assert e01.decide(ok, conf_bad)[0] == "INCONCLUSIVE"
    assert e01.decide(few, conf_ok)[0] == "INCONCLUSIVE"


def test_selection_rule_minimum_and_ties():
    dev = {"a": {"closed_trades": 40, "net_return_pct": 2.0}, "b": {"closed_trades": 31, "net_return_pct": 2.0},
           "c": {"closed_trades": 29, "net_return_pct": 9.0}}
    assert e01.select(dev, ["a", "b", "c"])[0] == "b"  # c below 30 trades; tie a/b -> fewer trades
    assert e01.select({"c": dev["c"]}, ["c"])[0] is None
