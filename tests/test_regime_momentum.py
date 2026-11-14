"""E03: regime-momentum strategy, trend feature causality and preregistered runner (synthetic data only)."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab import regime_experiment as e03
from crypto_decision_lab import turnover_experiment as e01
from crypto_decision_lab.dataset_catalog import Catalog, load_market
from crypto_decision_lab.pattern_formation_experiment import ExperimentError
from crypto_decision_lab.regime_momentum import TREND_FEATURE, RegimeMomentumStrategy, trend_features
from crypto_decision_lab.simulation import Portfolio, select_strategy

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "config" / "experiments" / "E03-regime-momentum-v1.json"
RESEARCH_ID = "BTCUSDT-1h-raw-v1"
H = pd.Timedelta(hours=1)


def _frame(hours=400, seed=3, start="2024-01-01"):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, hours)))
    index = pd.date_range(start, periods=hours, freq="1h", tz="UTC")
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
                         "volume": 1.0}, index=index)


# ------------------------------------------------------------- feature causality (CP-E03.0)
def test_trend_ratio_uses_only_closed_bars_before_the_event():
    frame = _frame()
    features = trend_features(frame, 24)
    t = frame.index[100]
    expected = frame["close"].iloc[99] / frame["close"].iloc[76:100].mean() - 1
    assert features[t][TREND_FEATURE] == pytest.approx(expected)
    changed = frame.copy()
    changed.iloc[100, changed.columns.get_loc("close")] *= 2  # the event's own bar
    assert trend_features(changed, 24)[t] == features[t]
    later = frame.copy()
    later.iloc[150:, later.columns.get_loc("close")] *= 3  # future bars
    assert trend_features(later, 24)[t] == features[t]


def test_trend_ratio_prefix_invariance_and_warmup():
    frame = _frame()
    full = trend_features(frame, 48)
    for cut in (60, 200, 333):
        prefix = trend_features(frame.iloc[:cut], 48)
        assert all(prefix[ts] == full[ts] for ts in prefix)
    # event 48 is the first with 48 closed bars (0..47) behind it
    assert full[frame.index[47]] == {} and TREND_FEATURE in full[frame.index[48]]


def test_trend_ratio_undefined_until_the_window_refills_after_a_gap():
    frame = _frame().drop(index=_frame().index[200])
    features = trend_features(frame, 24)
    after = [ts for ts in frame.index if ts > pd.Timestamp("2024-01-01", tz="UTC") + 200 * H]
    assert all(features[ts] == {} for ts in after[:24])  # the SMA window contains the missing hour
    assert TREND_FEATURE in features[after[25]]


# ------------------------------------------------------------- strategy rules
def _decide(strategy, momentum, trend, quantity=0.0):
    features = {}
    if momentum is not None:
        features["momentum_24h"] = momentum
    if trend is not None:
        features[TREND_FEATURE] = trend
    return strategy.decide(None, features, Portfolio(500.0, quantity))


def test_hysteresis_and_regime_rules():
    s = RegimeMomentumStrategy(entry_threshold=0.03, exit_threshold=0.0, regime_min=0.0)
    assert _decide(s, 0.031, 0.01).exposure == "LONG"                    # entry
    assert _decide(s, 0.02, 0.01).exposure == "FLAT"                     # below entry, flat
    assert _decide(s, 0.02, 0.01, quantity=1.0).exposure == "LONG"       # hysteresis: hold above exit
    assert _decide(s, 0.0, 0.01, quantity=1.0).exposure == "LONG"        # exit threshold inclusive
    assert _decide(s, -0.001, 0.01, quantity=1.0).exposure == "FLAT"     # exit
    assert _decide(s, 0.10, 0.0).exposure == "FLAT"                      # regime off: no entry
    assert _decide(s, 0.10, -0.01, quantity=1.0).exposure == "FLAT"      # regime off: exit
    assert _decide(s, None, 0.01, quantity=1.0).exposure == "FLAT"       # fail closed
    assert _decide(s, 0.05, None).exposure == "FLAT"
    assert _decide(s, -0.001, 0.01, quantity=1.0).reason.startswith("exit:")
    with pytest.raises(ValueError):
        RegimeMomentumStrategy(entry_threshold=0.01, exit_threshold=0.02)
    with pytest.raises(ValueError):
        RegimeMomentumStrategy(entry_threshold=float("nan"))


def test_strategy_is_registered_in_the_engine():
    strategy = select_strategy({"name": "regime_momentum", "parameters": {"entry_threshold": 0.05,
                                                                           "exit_threshold": -0.02}})
    assert isinstance(strategy, RegimeMomentumStrategy) and strategy.exit_threshold == -0.02


# ------------------------------------------------------------- spec
def test_repository_spec_is_a_draft_until_registered():
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    assert spec["schema_version"] == e03.EXPERIMENT_SCHEMA
    if spec["status"] != "PREREGISTERED":
        with pytest.raises(ExperimentError, match="PREREGISTERED"):
            e03.load_spec(SPEC)
    configurations = e03.grid_configurations(spec)
    assert [c["config_id"] for c in configurations] == [f"g{i:02d}" for i in range(12)]
    assert configurations[0]["grid"] == {"entry_threshold": 0.02, "exit_threshold": 0.0, "sma_hours": 168}
    assert set(e03.baseline_configurations(spec)) == {"momentum_v1", "e01_selected", "buy_and_hold"}
    e01_baseline = e03.baseline_configurations(spec)["e01_selected"]
    assert e01_baseline["strategy"]["parameters"] == {"threshold": 0.03}
    assert e01_baseline["hypothesis"]["horizon_hours"] == 48


# ------------------------------------------------------------- runner on a synthetic catalog
WINDOWS = {
    "development": {"start": "2024-01-10 00:00", "end": "2024-03-15 00:00", "use": "selection only"},
    "validation": {"start": "2024-03-15 00:00", "end": "2024-04-20 00:00", "use": "one evaluation"},
    "confirmation": {"start": "2024-04-20 00:00", "end": "2024-05-29 00:00", "use": "one evaluation"},
}


def _write_market(root: Path) -> None:
    rng = np.random.default_rng(9)
    hours = 150 * 24
    returns = rng.normal(0.0002, 0.008, hours) + 0.004 * np.sin(np.arange(hours) / 61.0)
    opens = 100.0 * np.exp(np.cumsum(returns))
    closes = np.append(opens[1:], opens[-1] * 1.001)
    index = pd.date_range("2024-01-01", periods=hours, freq="1h", tz="UTC")
    frame = pd.DataFrame({"timestamp": index, "open": opens, "high": np.maximum(opens, closes) * 1.002,
                          "low": np.minimum(opens, closes) * 0.998, "close": closes,
                          "volume": rng.uniform(5, 15, hours)}).drop(index=[1000])
    chunk = root / "data" / "chunks" / "BTCUSDT" / "research.parquet"
    chunk.parent.mkdir(parents=True)
    frame.to_parquet(chunk, index=False)
    snapshot = root / "data" / "snapshots" / RESEARCH_ID
    snapshot.mkdir(parents=True)
    (snapshot / "raw-manifest.json").write_text(json.dumps({
        "dataset_id": RESEARCH_ID, "source_manifest_sha256": hashlib.sha256(b"e03-fixture").hexdigest(), "chunks": [
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
    spec.update(overrides)
    path = root / name
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    root = tmp_path_factory.mktemp("e03") / "repo"
    root.mkdir()
    _write_market(root)
    market = load_market(Catalog.load(root / "config" / "dataset-catalog.json"), RESEARCH_ID, repo_root=root)
    spec = _spec(root, market.source_manifest_sha256)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(e01, "SELECTION_MIN_TRADES", 1)  # synthetic windows are short
        selection = e03.develop(spec, repo_root=root, output_dir="var/run-a")
        report = e03.evaluate(spec, repo_root=root, output_dir="var/run-a")
        e03.develop(spec, repo_root=root, output_dir="var/run-b")
        second = e03.evaluate(spec, repo_root=root, output_dir="var/run-b")
    return {"root": root, "spec": spec, "selection": selection, "report": report, "second": second,
            "out": root / "var" / "run-a"}


def test_selection_locked_before_validation_and_reproducible(lab):
    out, report = lab["out"], lab["report"]
    locked = json.loads((out / "selection.json").read_text(encoding="utf-8"))
    assert locked["locked_utc"] <= report["evaluated_utc"]
    assert report["selection"]["selection_sha256"] == e03._file_sha256(out / "selection.json")
    assert set(locked["development"]) == {f"g{i:02d}" for i in range(12)}
    assert report["outcome"] in e03.OUTCOMES
    assert e03.ledger_digests(report) == e03.ledger_digests(lab["second"])
    if "validation" in report:
        assert len(e03.ledger_digests(report)) == 12 + 3 + 2 * (1 + 3)
        assert set(report["validation"]["baselines"]) == {"momentum_v1", "e01_selected", "buy_and_hold"}
        assert [c["criterion"] for c in report["validation"]["criteria"]][3].startswith("closed_trades_per_year")
    assert any(r["gaps_acknowledged"] for r in locked["development"].values())
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["files"].items():
        assert e03._file_sha256(out / name) == digest


def test_regime_actually_filters_entries(lab):
    reasons = {}
    for result in lab["selection"]["development"].values():
        for cause, count in result["exit_reasons"].items():
            reasons[cause] = reasons.get(cause, 0) + count
    assert reasons  # exits happened and are classified
    assert set(reasons) <= {"exit_threshold", "regime_off", "missing_feature", "horizon", "window", "other"}
    assert "other" not in reasons


def test_runner_refusals(lab):
    with pytest.raises(FileExistsError):
        e03.develop(lab["spec"], repo_root=lab["root"], output_dir="var/run-a")
    with pytest.raises(FileExistsError):
        e03.evaluate(lab["spec"], repo_root=lab["root"], output_dir="var/run-a")
    with pytest.raises(ExperimentError, match="phase-1"):
        e03.evaluate(lab["spec"], repo_root=lab["root"], output_dir="var/never-developed")
    wrong = _spec(lab["root"], "f" * 64, name="wrong.json")
    with pytest.raises(ExperimentError, match="source manifest"):
        e03.develop(wrong, repo_root=lab["root"], output_dir="var/run-wrong")
    assert (lab["root"] / "var" / "run-wrong" / "preregistration.json").is_file()
    with pytest.raises(Exception, match="forward"):
        e03.develop(lab["spec"], repo_root=lab["root"], output_dir="data/forward/BTCUSDT-1h-forward-v1/x")
