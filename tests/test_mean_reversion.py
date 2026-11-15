"""E04: mean-reversion features, strategy and the shared two-phase protocol (synthetic data only)."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab import experiment_protocol as protocol
from crypto_decision_lab import reversion_experiment as e04
from crypto_decision_lab import turnover_experiment as e01
from crypto_decision_lab.dataset_catalog import Catalog, load_market
from crypto_decision_lab.mean_reversion import (
    ZSCORE,
    ZSCORE_PREV,
    MeanReversionStrategy,
    reversion_features,
    zscore_features,
)
from crypto_decision_lab.pattern_formation_experiment import ExperimentError
from crypto_decision_lab.regime_momentum import TREND_FEATURE
from crypto_decision_lab.simulation import Portfolio, select_strategy

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "config" / "experiments" / "E04-mean-reversion-v1.json"
RESEARCH_ID = "BTCUSDT-1h-raw-v1"


def _frame(hours=400, seed=4):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, hours)))
    index = pd.date_range("2024-01-01", periods=hours, freq="1h", tz="UTC")
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
                         "volume": 1.0}, index=index)


# ------------------------------------------------------------- feature causality (CP-E04.0)
def test_zscore_uses_only_closed_bars_before_the_event():
    frame = _frame()
    features = zscore_features(frame, 24)
    t = frame.index[100]
    window = frame["close"].iloc[76:100]
    assert features[t][ZSCORE] == pytest.approx((window.iloc[-1] - window.mean()) / window.std(ddof=0))
    assert features[t][ZSCORE_PREV] == pytest.approx(features[frame.index[99]][ZSCORE])
    changed = frame.copy()
    changed.iloc[100, changed.columns.get_loc("close")] *= 2  # the event's own bar
    assert zscore_features(changed, 24)[t] == features[t]
    later = frame.copy()
    later.iloc[150:, later.columns.get_loc("close")] *= 3  # future bars
    assert zscore_features(later, 24)[t] == features[t]


def test_zscore_prefix_invariance_warmup_and_flat_window():
    frame = _frame()
    full = zscore_features(frame, 48)
    for cut in (60, 200, 333):
        prefix = zscore_features(frame.iloc[:cut], 48)
        assert all(prefix[ts] == full[ts] for ts in prefix)
    assert full[frame.index[47]] == {} and set(full[frame.index[48]]) == {ZSCORE}
    assert set(full[frame.index[49]]) == {ZSCORE, ZSCORE_PREV}
    flat = frame.copy()
    flat["close"] = 100.0
    assert all(value == {} for value in zscore_features(flat, 24).values())  # zero deviation: undefined


def test_zscore_undefined_until_the_window_refills_after_a_gap():
    frame = _frame()
    frame = frame.drop(index=frame.index[200])
    features = zscore_features(frame, 24)
    after = [ts for ts in frame.index if ts > frame.index[199]]
    assert all(ZSCORE not in features[ts] for ts in after[:24])
    assert ZSCORE in features[after[25]]
    merged = reversion_features(frame, 24, 48)
    assert TREND_FEATURE in merged[frame.index[150]] and ZSCORE in merged[frame.index[150]]


# ------------------------------------------------------------- strategy rules
def _decide(strategy, z, previous, trend, quantity=0.0):
    features = {k: v for k, v in ((ZSCORE, z), (ZSCORE_PREV, previous), (TREND_FEATURE, trend)) if v is not None}
    return strategy.decide(None, features, Portfolio(500.0, quantity))


def test_entry_on_the_crossing_and_exit_at_the_mean():
    s = MeanReversionStrategy(entry_z=2.0, exit_z=0.0)
    assert _decide(s, -2.1, -1.9, 0.02).exposure == "LONG"            # crossing below -2 in an uptrend
    assert _decide(s, -2.5, -2.1, 0.02).exposure == "FLAT"            # already oversold: no re-trigger
    assert _decide(s, -1.9, -1.0, 0.02).exposure == "FLAT"            # not oversold
    assert _decide(s, -2.1, -1.9, 0.0).exposure == "FLAT"             # regime off
    assert _decide(s, -2.1, -1.9, -0.03).exposure == "FLAT"
    assert _decide(s, -3.0, -2.5, -0.05, quantity=1.0).exposure == "LONG"   # hold: regime not checked while long
    assert _decide(s, -0.01, -0.5, 0.02, quantity=1.0).exposure == "LONG"
    assert _decide(s, 0.0, -0.2, 0.02, quantity=1.0).exposure == "FLAT"     # reverted (inclusive)
    assert _decide(s, 0.0, -0.2, 0.02, quantity=1.0).reason.startswith("exit: reverted")
    assert _decide(s, None, -1.0, 0.02, quantity=1.0).exposure == "FLAT"    # fail closed
    assert _decide(s, -2.1, None, 0.02).exposure == "FLAT"
    assert _decide(s, -2.1, -1.9, None).exposure == "FLAT"
    assert _decide(s, -2.1, None, None, quantity=1.0).exposure == "LONG"    # holding needs only the z-score
    for bad in ({"entry_z": 0.0}, {"entry_z": 2.0, "exit_z": -2.0}, {"entry_z": float("nan")}):
        with pytest.raises(ValueError):
            MeanReversionStrategy(**bad)


def test_strategy_is_registered_in_the_engine():
    strategy = select_strategy({"name": "mean_reversion", "parameters": {"entry_z": 2.5, "exit_z": 0.0}})
    assert isinstance(strategy, MeanReversionStrategy) and strategy.entry_z == 2.5


# ------------------------------------------------------------- spec
def test_repository_spec_grid_and_status():
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    assert spec["schema_version"] == e04.ADAPTER.schema
    if spec["status"] != "PREREGISTERED":
        with pytest.raises(ExperimentError, match="PREREGISTERED"):
            e04.load_spec(SPEC)
    configurations = e04.grid_configurations(spec)
    assert [c["config_id"] for c in configurations] == [f"g{i:02d}" for i in range(12)]
    assert configurations[0]["grid"] == {"entry_z": 1.5, "window_hours": 24, "horizon_hours": 48}
    first = configurations[0]["run_config"]
    assert first["strategy"] == {"name": "mean_reversion",
                                 "parameters": {"exit_z": 0.0, "regime_min": 0.0, "entry_z": 1.5}}
    assert first["hypothesis"]["horizon_hours"] == 48 and configurations[0]["feature_key"] == 24
    assert configurations[-1]["grid"] == {"entry_z": 2.5, "window_hours": 72, "horizon_hours": 120}
    assert set(protocol.baseline_configurations(spec)) == {"momentum_v1", "buy_and_hold"}


# ------------------------------------------------------------- protocol on a synthetic catalog
WINDOWS = {
    "development": {"start": "2024-02-05 00:00", "end": "2024-04-01 00:00", "use": "selection only"},
    "validation": {"start": "2024-04-01 00:00", "end": "2024-05-01 00:00", "use": "one evaluation"},
    "confirmation": {"start": "2024-05-01 00:00", "end": "2024-05-29 00:00", "use": "one evaluation"},
}


def _write_market(root: Path) -> None:
    rng = np.random.default_rng(21)
    hours = 150 * 24
    returns = rng.normal(0.0004, 0.006, hours) + 0.01 * (rng.random(hours) < 0.01) * rng.choice([-3, -2], hours)
    opens = 100.0 * np.exp(np.cumsum(returns))
    closes = np.append(opens[1:], opens[-1] * 1.001)
    index = pd.date_range("2024-01-01", periods=hours, freq="1h", tz="UTC")
    frame = pd.DataFrame({"timestamp": index, "open": opens, "high": np.maximum(opens, closes) * 1.002,
                          "low": np.minimum(opens, closes) * 0.998, "close": closes,
                          "volume": rng.uniform(5, 15, hours)}).drop(index=[1500])
    chunk = root / "data" / "chunks" / "BTCUSDT" / "research.parquet"
    chunk.parent.mkdir(parents=True)
    frame.to_parquet(chunk, index=False)
    snapshot = root / "data" / "snapshots" / RESEARCH_ID
    snapshot.mkdir(parents=True)
    (snapshot / "raw-manifest.json").write_text(json.dumps({
        "dataset_id": RESEARCH_ID, "source_manifest_sha256": hashlib.sha256(b"e04-fixture").hexdigest(), "chunks": [
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
    spec["status"] = "PREREGISTERED"
    spec["inputs"]["windows"] = copy.deepcopy(WINDOWS)
    spec["inputs"]["source_manifest_sha256"] = source_sha
    spec["inputs"]["fixed"]["regime_sma_hours"] = 240  # the synthetic series is short
    spec.update(overrides)
    path = root / name
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    root = tmp_path_factory.mktemp("e04") / "repo"
    root.mkdir()
    _write_market(root)
    market = load_market(Catalog.load(root / "config" / "dataset-catalog.json"), RESEARCH_ID, repo_root=root)
    spec = _spec(root, market.source_manifest_sha256)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(e01, "SELECTION_MIN_TRADES", 1)  # synthetic windows are short
        selection = e04.develop(spec, repo_root=root, output_dir="var/run-a")
        report = e04.evaluate(spec, repo_root=root, output_dir="var/run-a")
        e04.develop(spec, repo_root=root, output_dir="var/run-b")
        second = e04.evaluate(spec, repo_root=root, output_dir="var/run-b")
    return {"root": root, "spec": spec, "selection": selection, "report": report, "second": second,
            "out": root / "var" / "run-a"}


def test_protocol_locks_selection_and_is_reproducible(lab):
    out, report, selection = lab["out"], lab["report"], lab["selection"]
    assert selection["selected"] is not None and "validation" in report  # the full path is exercised
    locked = json.loads((out / "selection.json").read_text(encoding="utf-8"))
    assert locked["locked_utc"] <= report["evaluated_utc"]
    assert report["selection"]["selection_sha256"] == protocol._file_sha256(out / "selection.json")
    assert set(locked["development"]) == {f"g{i:02d}" for i in range(12)}
    assert report["outcome"] in protocol.OUTCOMES
    digests = protocol.ledger_digests(report)
    assert digests == protocol.ledger_digests(lab["second"]) and len(digests) == 12 + 2 + 2 * (1 + 2)
    assert [c["criterion"] for c in report["validation"]["criteria"]][3].startswith("closed_trades_per_year")
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["files"].items():
        assert protocol._file_sha256(out / name) == digest
    preregistration = json.loads((out / "preregistration.json").read_text(encoding="utf-8"))
    assert preregistration["spec_sha256"] == report["spec_sha256"] == locked["spec_sha256"]


def test_trades_happen_and_exits_are_classified(lab):
    development = lab["selection"]["development"]
    assert sum(r["closed_trades"] for r in development.values()) > 0
    reasons = {cause for result in development.values() for cause in result["exit_reasons"]}
    assert reasons and reasons <= {"reverted", "horizon", "missing_feature", "window"}
    # a tighter entry never trades more than a looser one on the same window and horizon
    ids = lab["selection"]["configurations"]
    by = {(g["window_hours"], g["horizon_hours"], g["entry_z"]): development[k]["closed_trades"]
          for k, g in ids.items()}
    for window in (24, 72):
        for horizon in (48, 120):
            assert by[(window, horizon, 2.5)] <= by[(window, horizon, 1.5)]


def test_protocol_refusals(lab):
    with pytest.raises(FileExistsError):
        e04.develop(lab["spec"], repo_root=lab["root"], output_dir="var/run-a")
    with pytest.raises(FileExistsError):
        e04.evaluate(lab["spec"], repo_root=lab["root"], output_dir="var/run-a")
    with pytest.raises(ExperimentError, match="phase-1"):
        e04.evaluate(lab["spec"], repo_root=lab["root"], output_dir="var/never-developed")
    wrong = _spec(lab["root"], "f" * 64, name="wrong.json")
    with pytest.raises(ExperimentError, match="source manifest"):
        e04.develop(wrong, repo_root=lab["root"], output_dir="var/run-wrong")
    assert (lab["root"] / "var" / "run-wrong" / "preregistration.json").is_file()  # lock precedes data access
    with pytest.raises(Exception, match="forward"):
        e04.develop(lab["spec"], repo_root=lab["root"], output_dir="data/forward/BTCUSDT-1h-forward-v1/x")
    draft = _spec(lab["root"], "0" * 64, name="draft.json", status="DRAFT")
    with pytest.raises(ExperimentError, match="PREREGISTERED"):
        e04.develop(draft, repo_root=lab["root"], output_dir="var/run-draft")
    assert not (lab["root"] / "var" / "run-draft").exists()


def test_selection_digest_and_engine_are_checked_before_evaluation(lab, monkeypatch):
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(e01, "SELECTION_MIN_TRADES", 1)
        e04.develop(lab["spec"], repo_root=lab["root"], output_dir="var/run-c")
    monkeypatch.setattr(protocol, "engine_digests", lambda: ("0" * 64, {}))
    with pytest.raises(ExperimentError, match="engine changed"):
        e04.evaluate(lab["spec"], repo_root=lab["root"], output_dir="var/run-c")
