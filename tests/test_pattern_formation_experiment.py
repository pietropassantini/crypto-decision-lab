"""E02: preregistered pattern-in-formation experiment runner (synthetic catalog only)."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab import pattern_formation_experiment as pfe
from crypto_decision_lab.dataset_catalog import Catalog, load_market

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "config" / "experiments" / "E02-pattern-formation-v1.json"
RESEARCH_ID = "BTCUSDT-1h-raw-v1"
DAYS = 150
WINDOWS = {
    "development": {"start": "2024-01-05 00:00", "end": "2024-03-15 00:00"},
    "validation": {"start": "2024-03-15 00:00", "end": "2024-04-20 00:00"},
    "confirmation": {"start": "2024-04-20 00:00", "end": "2024-05-29 00:00"},
}


def _write_market(root: Path) -> None:
    rng = np.random.default_rng(11)
    hours = DAYS * 24
    returns = rng.normal(0.0001, 0.007, hours) + 0.003 * np.sin(np.arange(hours) / 37.0)
    opens = 100.0 * np.exp(np.cumsum(returns))
    closes = np.append(opens[1:], opens[-1] * 1.001)
    index = pd.date_range("2024-01-01", periods=hours, freq="1h", tz="UTC")
    frame = pd.DataFrame({"timestamp": index, "open": opens, "high": np.maximum(opens, closes) * 1.002,
                          "low": np.minimum(opens, closes) * 0.998, "close": closes,
                          "volume": rng.uniform(5, 15, hours)})
    frame = frame.drop(index=[1000])  # one missing hour inside development: acknowledged gap, detector reset
    chunk = root / "data" / "chunks" / "BTCUSDT" / "research.parquet"
    chunk.parent.mkdir(parents=True)
    frame.to_parquet(chunk, index=False)
    snapshot = root / "data" / "snapshots" / RESEARCH_ID
    snapshot.mkdir(parents=True)
    (snapshot / "raw-manifest.json").write_text(json.dumps({
        "dataset_id": RESEARCH_ID, "source_manifest_sha256": hashlib.sha256(b"e02-fixture").hexdigest(), "chunks": [
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
    spec["selection"]["min_closed_trades"] = 1
    spec.update(overrides)
    path = root / name
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    root = tmp_path_factory.mktemp("e02") / "repo"
    root.mkdir()
    _write_market(root)
    market = load_market(Catalog.load(root / "config" / "dataset-catalog.json"), RESEARCH_ID, repo_root=root)
    spec = _spec(root, market.source_manifest_sha256)
    assert market.source_manifest_sha256
    report = pfe.run_experiment(spec, repo_root=root, output_dir="var/run-a")
    return {"root": root, "spec": spec, "report": report, "out": root / "var" / "run-a",
            "source_sha": market.source_manifest_sha256}


# ------------------------------------------------------------- spec
def test_repository_spec_is_preregistered_and_valid():
    spec, sha = pfe.load_spec(SPEC)
    assert spec["status"] == "PREREGISTERED" and len(sha) == 64
    configurations = pfe.grid_configurations(spec)
    assert [c["config_id"] for c in configurations] == [f"g{i:02d}" for i in range(8)]
    assert len({c["detector_sha256"] for c in configurations}) == 2  # pivot_bars 3 and 5
    assert set(pfe.baseline_configurations(spec)) == {"momentum", "buy_and_hold"}


def test_spec_sha_is_line_ending_independent(tmp_path):
    lf = tmp_path / "lf.json"
    crlf = tmp_path / "crlf.json"
    text = SPEC.read_bytes().replace(b"\r\n", b"\n")
    lf.write_bytes(text)
    crlf.write_bytes(text.replace(b"\n", b"\r\n"))
    assert pfe.load_spec(lf)[1] == pfe.load_spec(crlf)[1]


@pytest.mark.parametrize("change", [
    {"status": "DRAFT"},
    {"schema_version": "cdl.s09.experiment.v1"},
])
def test_spec_refused(tmp_path, change):
    path = _spec(tmp_path, "0" * 64, **change)
    with pytest.raises(pfe.ExperimentError):
        pfe.load_spec(path)


def test_overlapping_windows_refused(tmp_path):
    spec = json.loads(_spec(tmp_path, "0" * 64).read_text(encoding="utf-8"))
    spec["inputs"]["windows"]["validation"]["start"] = "2024-03-01 00:00"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(pfe.ExperimentError, match="overlapping"):
        pfe.load_spec(path)


# ------------------------------------------------------------- run
def test_run_produces_locked_artefacts(lab):
    out, report = lab["out"], lab["report"]
    prereg = json.loads((out / "preregistration.json").read_text(encoding="utf-8"))
    selection = json.loads((out / "selection.json").read_text(encoding="utf-8"))
    assert prereg["spec_sha256"] == selection["spec_sha256"] == report["spec_sha256"]
    assert set(selection["development"]) == {f"g{i:02d}" for i in range(8)}
    assert report["outcome"] in pfe.OUTCOMES
    assert report["selection"]["selected"] == selection["selected"]
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["files"].items():
        assert pfe._file_sha256(out / name) == digest
    assert any(r["gaps_acknowledged"] for r in report["development"]["grid"].values())


def test_selection_uses_development_only(lab):
    report = lab["report"]
    grid = report["development"]["grid"]
    best, _ = pfe.select(json.loads(lab["spec"].read_text(encoding="utf-8")), grid, sorted(grid))
    assert best == report["selection"]["selected"]
    if best is not None:
        for window in ("validation", "confirmation"):
            assert {c["metric"] for c in report[window]["criteria"]}
            assert set(report[window]["baselines"]) == {"momentum", "buy_and_hold"}


def test_run_is_reproducible(lab):
    second = pfe.run_experiment(lab["spec"], repo_root=lab["root"], output_dir="var/run-b")
    first = lab["report"]

    def digests(report):
        out = {k: v["ledger_sha256"] for k, v in report["development"]["grid"].items()}
        out.update({f"dev-{k}": v["ledger_sha256"] for k, v in report["development"]["baselines"].items()})
        for window in ("validation", "confirmation"):
            if window in report:
                out[window] = report[window]["selected"]["ledger_sha256"]
        return out

    assert digests(first) == digests(second)
    assert first["outcome"] == second["outcome"]


def test_existing_output_and_wrong_source_refused(lab):
    with pytest.raises(FileExistsError):
        pfe.run_experiment(lab["spec"], repo_root=lab["root"], output_dir="var/run-a")
    wrong = _spec(lab["root"], "f" * 64, name="wrong.json")
    with pytest.raises(pfe.ExperimentError, match="source manifest"):
        pfe.run_experiment(wrong, repo_root=lab["root"], output_dir="var/run-wrong")
    assert (lab["root"] / "var" / "run-wrong" / "preregistration.json").is_file()  # lock precedes data access


def test_forward_output_refused(lab):
    with pytest.raises(Exception, match="forward"):
        pfe.run_experiment(lab["spec"], repo_root=lab["root"], output_dir="data/forward/BTCUSDT-1h-forward-v1/x")


# ------------------------------------------------------------- rules
def test_criteria_and_outcome_rules():
    criteria = [{"metric": "net_return_pct", "op": ">", "value": 0},
                {"metric": "costs_share_of_gross", "op": "<=", "value": 0.5}]
    passed = pfe.evaluate_criteria(criteria, {"net_return_pct": 1.0, "costs_share_of_gross": 0.2})
    assert all(c["passed"] for c in passed)
    missing = pfe.evaluate_criteria(criteria, {"net_return_pct": 1.0, "costs_share_of_gross": None})
    assert [c["passed"] for c in missing] == [True, False]  # gross <= 0: cost share undefined, fails
    acceptance = {"min_closed_trades": 15}
    ok = {"selected": {"closed_trades": 20}, "criteria": passed}
    bad = {"selected": {"closed_trades": 20}, "criteria": missing}
    few = {"selected": {"closed_trades": 3}, "criteria": passed}
    assert pfe.decide(acceptance, ok, ok)[0] == "CANDIDATE"
    assert pfe.decide(acceptance, bad, ok)[0] == "REJECTED"
    assert pfe.decide(acceptance, ok, bad)[0] == "INCONCLUSIVE"
    assert pfe.decide(acceptance, few, ok)[0] == "INCONCLUSIVE"


def test_selection_rule_ties_and_minimum():
    spec = {"selection": {"min_closed_trades": 5}}
    dev = {"a": {"closed_trades": 10, "net_return_pct": 2.0}, "b": {"closed_trades": 6, "net_return_pct": 2.0},
           "c": {"closed_trades": 4, "net_return_pct": 9.0}}
    assert pfe.select(spec, dev, ["a", "b", "c"])[0] == "b"
    assert pfe.select({"selection": {"min_closed_trades": 50}}, dev, ["a", "b", "c"])[0] is None


def test_exit_causes_from_ledger():
    ledger = [
        {"kind": "decision", "reason": "exit: no configured pattern alive (dbot target)"},
        {"kind": "order", "side": "SELL"},
        {"kind": "decision", "reason": "holding horizon reached"},
        {"kind": "order", "side": "SELL"},
        {"kind": "decision", "reason": "exit: no configured pattern alive (flag invalidation)"},
        {"kind": "order", "side": "SELL"},
        {"kind": "decision", "reason": "entry dbot: ..."},
        {"kind": "order", "side": "BUY"},
    ]
    assert pfe.exit_causes(ledger) == {"horizon": 1, "invalidation": 1, "target": 1}
