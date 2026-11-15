"""E05: daily-close features, daily_trend strategy and the protocol with lowered trade minimums (synthetic only)."""
from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab import daily_experiment as e05
from crypto_decision_lab import experiment_protocol as protocol
from crypto_decision_lab.daily_trend import (
    DECISION,
    MOMENTUM,
    TREND,
    DailyTrendStrategy,
    daily_closes,
    daily_features,
)
from crypto_decision_lab.dataset_catalog import Catalog, load_market
from crypto_decision_lab.pattern_formation_experiment import ExperimentError
from crypto_decision_lab.simulation import Portfolio, select_strategy

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "config" / "experiments" / "E05-daily-trend-v1.json"
RESEARCH_ID = "BTCUSDT-1h-raw-v1"
DAY = pd.Timedelta(days=1)


def _frame(days=60, seed=6):
    rng = np.random.default_rng(seed)
    hours = days * 24
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.005, hours)))
    index = pd.date_range("2024-01-01", periods=hours, freq="1h", tz="UTC")
    return pd.DataFrame({"open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
                         "volume": 1.0}, index=index)


# ------------------------------------------------------------- feature causality (CP-E05.0)
def test_daily_close_is_the_last_bar_closed_before_the_decision():
    frame = _frame()
    daily = daily_closes(frame)
    decision = pd.Timestamp("2024-01-11 00:00", tz="UTC")
    assert daily.loc[decision] == frame["close"].loc[decision - pd.Timedelta(hours=1)]  # the 23:00 bar of Jan 10
    features = daily_features(frame, 3, 5)
    assert features[decision][DECISION] == 1.0
    assert features[decision][MOMENTUM] == pytest.approx(daily.loc[decision] / daily.loc[decision - 3 * DAY] - 1)
    window = [daily.loc[decision - k * DAY] for k in range(5)]
    assert features[decision][TREND] == pytest.approx(daily.loc[decision] / np.mean(window) - 1)
    assert all(features[ts] == {} for ts in frame.index if ts.hour != 0)  # only the 00:00 event decides


def test_daily_features_ignore_the_decision_day_and_the_future():
    frame = _frame()
    decision = pd.Timestamp("2024-01-20 00:00", tz="UTC")
    reference = daily_features(frame, 3, 5)[decision]
    changed = frame.copy()
    changed.loc[changed.index >= decision, "close"] *= 2  # the decision bar itself and everything after
    assert daily_features(changed, 3, 5)[decision] == reference
    for cut_days in (25, 40):
        prefix = daily_features(frame.iloc[:cut_days * 24], 3, 5)
        full = daily_features(frame, 3, 5)
        assert all(prefix[ts] == full[ts] for ts in prefix)
    first = daily_features(frame, 3, 5)
    assert MOMENTUM not in first[pd.Timestamp("2024-01-04 00:00", tz="UTC")]  # needs closes of 4 days
    assert MOMENTUM in first[pd.Timestamp("2024-01-05 00:00", tz="UTC")]


def test_missing_daily_close_makes_the_windows_undefined():
    frame = _frame()
    lost = pd.Timestamp("2024-01-15 23:00", tz="UTC")  # the bar that closes Jan 15
    frame = frame.drop(index=lost)
    features = daily_features(frame, 3, 5)
    jan16 = pd.Timestamp("2024-01-16 00:00", tz="UTC")
    assert features[jan16] == {DECISION: 1.0}  # decision bar without values: the strategy fails closed
    assert MOMENTUM not in features[jan16 + 3 * DAY]  # the 3-day return still needs the lost close
    assert MOMENTUM in features[jan16 + 4 * DAY] and TREND not in features[jan16 + 4 * DAY]
    assert TREND in features[jan16 + 5 * DAY]
    other = _frame().drop(index=pd.Timestamp("2024-01-15 10:00", tz="UTC"))  # a mid-day hour does not matter
    assert daily_features(other, 3, 5)[jan16] == daily_features(_frame(), 3, 5)[jan16]


# ------------------------------------------------------------- strategy rules
def _decide(strategy, momentum, trend, quantity=0.0, decision=True):
    features = {DECISION: 1.0} if decision else {}
    if momentum is not None:
        features[MOMENTUM] = momentum
    if trend is not None:
        features[TREND] = trend
    return strategy.decide(None, features, Portfolio(500.0, quantity))


def test_daily_rules_and_keep_between_decisions():
    s = DailyTrendStrategy(entry_threshold=0.05, exit_threshold=0.0)
    assert _decide(s, None, None, decision=False).exposure == "FLAT"                 # keep flat
    assert _decide(s, None, None, quantity=1.0, decision=False).exposure == "LONG"   # keep long
    assert _decide(s, None, None, quantity=1.0, decision=False).reason == "between daily decisions: keep"
    assert _decide(s, 0.06, 0.02).exposure == "LONG"                    # entry
    assert _decide(s, 0.05, 0.02).exposure == "FLAT"                    # not above entry
    assert _decide(s, 0.01, 0.02, quantity=1.0).exposure == "LONG"      # hysteresis
    assert _decide(s, -0.001, 0.02, quantity=1.0).exposure == "FLAT"    # exit
    assert _decide(s, 0.20, 0.0).exposure == "FLAT"                     # regime off
    assert _decide(s, 0.20, -0.01, quantity=1.0).exposure == "FLAT"
    assert _decide(s, None, 0.02, quantity=1.0).exposure == "FLAT"      # decision bar, missing feature
    assert _decide(s, 0.20, None).exposure == "FLAT"
    with pytest.raises(ValueError):
        DailyTrendStrategy(entry_threshold=0.0, exit_threshold=0.01)


def test_strategy_is_registered_in_the_engine():
    strategy = select_strategy({"name": "daily_trend", "parameters": {"entry_threshold": 0.05}})
    assert isinstance(strategy, DailyTrendStrategy)


# ------------------------------------------------------------- spec
def test_repository_spec_grid_minimums_and_status():
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    assert spec["schema_version"] == e05.ADAPTER.schema
    if spec["status"] != "PREREGISTERED":
        with pytest.raises(ExperimentError, match="PREREGISTERED"):
            e05.load_spec(SPEC)
    assert (spec["selection"]["min_closed_trades"], spec["acceptance"]["min_closed_trades"]) == (12, 8)
    assert (e05.ADAPTER.selection_min_trades, e05.ADAPTER.acceptance_min_trades) == (12, 8)
    assert "closed_trades >= 8" in spec["acceptance"]["validation"]
    configurations = e05.grid_configurations(spec)
    assert [c["config_id"] for c in configurations] == [f"g{i:02d}" for i in range(12)]
    assert configurations[0]["grid"] == {"momentum_days": 7, "sma_days": 50, "entry_threshold": 0.0}
    assert configurations[0]["feature_key"] == (7, 50)
    assert configurations[0]["run_config"]["hypothesis"]["horizon_hours"] == 1000000
    assert set(protocol.baseline_configurations(spec)) == {"momentum_v1", "buy_and_hold"}


def test_spec_with_other_minimums_is_refused(tmp_path):
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    spec["status"] = "PREREGISTERED"
    spec["acceptance"]["min_closed_trades"] = 3
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec), encoding="utf-8")
    with pytest.raises(ExperimentError, match="minimum trades"):
        e05.load_spec(path)


# ------------------------------------------------------------- protocol minimums
def _result(**values):
    base = {"net_return_pct": 5.0, "max_drawdown_mtm_pct": -10.0, "closed_trades": 9,
            "closed_trades_per_year": 6.0, "costs": 5.0, "gross_pnl": 30.0}
    return {**base, **values}


def test_lowered_minimums_drive_selection_criteria_and_outcome():
    adapter = e05.ADAPTER
    dev = {"a": {"closed_trades": 12, "net_return_pct": 1.0}, "b": {"closed_trades": 11, "net_return_pct": 9.0}}
    assert protocol._select(adapter, dev, ["a", "b"])[0] == "a"       # b has fewer than 12 trades
    assert protocol._select(adapter, {"b": dev["b"]}, ["b"])[0] is None
    baseline = _result(closed_trades_per_year=900.0)
    criteria = protocol._validation_criteria(adapter, _result(), baseline)
    assert [c["criterion"] for c in criteria][2] == "closed_trades >= 8" and all(c["passed"] for c in criteria)
    ok = {"selected": _result(), "criteria": criteria}
    confirmation = {"criteria": protocol.confirmation_criteria(_result())}
    assert protocol._decide(adapter, ok, confirmation)[0] == "CANDIDATE"  # 9 trades: enough for E05, not for E01
    few = {"selected": _result(closed_trades=7),
           "criteria": protocol._validation_criteria(adapter, _result(closed_trades=7), baseline)}
    assert protocol._decide(adapter, few, confirmation)[0] == "INCONCLUSIVE"
    default = dataclasses.replace(adapter, selection_min_trades=None, acceptance_min_trades=None)
    assert protocol._decide(default, ok, confirmation)[0] == "INCONCLUSIVE"  # E01 minimum of 15 still the default


# ------------------------------------------------------------- protocol on a synthetic catalog
WINDOWS = {
    "development": {"start": "2024-04-20 00:00", "end": "2024-10-01 00:00", "use": "selection only"},
    "validation": {"start": "2024-10-01 00:00", "end": "2024-12-15 00:00", "use": "one evaluation"},
    "confirmation": {"start": "2024-12-15 00:00", "end": "2025-02-01 00:00", "use": "one evaluation"},
}


def _write_market(root: Path) -> None:
    rng = np.random.default_rng(33)
    hours = 400 * 24
    drift = 0.0006 * np.sin(np.arange(hours) / (24 * 9.0))  # multi-day swings: daily trends exist
    opens = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.004, hours) + drift))
    closes = np.append(opens[1:], opens[-1] * 1.001)
    index = pd.date_range("2024-01-01", periods=hours, freq="1h", tz="UTC")
    frame = pd.DataFrame({"timestamp": index, "open": opens, "high": np.maximum(opens, closes) * 1.002,
                          "low": np.minimum(opens, closes) * 0.998, "close": closes,
                          "volume": rng.uniform(5, 15, hours)}).drop(index=[5000])
    chunk = root / "data" / "chunks" / "BTCUSDT" / "research.parquet"
    chunk.parent.mkdir(parents=True)
    frame.to_parquet(chunk, index=False)
    snapshot = root / "data" / "snapshots" / RESEARCH_ID
    snapshot.mkdir(parents=True)
    (snapshot / "raw-manifest.json").write_text(json.dumps({
        "dataset_id": RESEARCH_ID, "source_manifest_sha256": hashlib.sha256(b"e05-fixture").hexdigest(), "chunks": [
            {"file": "chunks/BTCUSDT/research.parquet", "sha256": hashlib.sha256(chunk.read_bytes()).hexdigest()}]}),
        encoding="utf-8")
    (root / "config").mkdir()
    (root / "config" / "dataset-catalog.json").write_text(json.dumps({
        "schema_version": "cdl.s02.catalog.v1",
        "sources": [{"dataset_id": RESEARCH_ID, "path": f"data/snapshots/{RESEARCH_ID}", "role": "research"},
                    {"dataset_id": "BTCUSDT-1h-forward-v1", "path": "data/forward/BTCUSDT-1h-forward-v1",
                     "role": "reserved-forward"}],
        "reserved_dataset_ids": ["BTCUSDT-1h-forward-v1"]}), encoding="utf-8")


def _spec(root: Path, source_sha: str, name="spec.json") -> Path:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    spec["status"] = "PREREGISTERED"
    spec["inputs"]["windows"] = copy.deepcopy(WINDOWS)
    spec["inputs"]["source_manifest_sha256"] = source_sha
    path = root / name
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    root = tmp_path_factory.mktemp("e05") / "repo"
    root.mkdir()
    _write_market(root)
    market = load_market(Catalog.load(root / "config" / "dataset-catalog.json"), RESEARCH_ID, repo_root=root)
    spec = _spec(root, market.source_manifest_sha256)
    loose = dataclasses.replace(e05.ADAPTER, selection_min_trades=1, acceptance_min_trades=1)  # short windows
    selection = protocol.develop(loose, spec, repo_root=root, output_dir="var/run-a")
    report = protocol.evaluate(loose, spec, repo_root=root, output_dir="var/run-a")
    protocol.develop(loose, spec, repo_root=root, output_dir="var/run-b")
    second = protocol.evaluate(loose, spec, repo_root=root, output_dir="var/run-b")
    return {"root": root, "spec": spec, "selection": selection, "report": report, "second": second,
            "out": root / "var" / "run-a"}


def test_daily_run_is_locked_and_reproducible(lab):
    report, selection = lab["report"], lab["selection"]
    assert selection["selected"] is not None and "validation" in report
    digests = protocol.ledger_digests(report)
    assert digests == protocol.ledger_digests(lab["second"]) and len(digests) == 12 + 2 + 2 * (1 + 2)
    assert report["selection"]["selection_sha256"] == protocol._file_sha256(lab["out"] / "selection.json")
    assert [c["criterion"] for c in report["validation"]["criteria"]][2] == "closed_trades >= 1"


def test_orders_are_placed_only_on_daily_decision_bars(lab):
    ledgers = sorted((lab["out"] / "ledgers").glob("development-g*.json"))
    orders = 0
    for path in ledgers:
        for record in json.loads(path.read_text(encoding="utf-8")):
            if record["kind"] == "order":
                orders += 1
                assert pd.Timestamp(record["timestamp"]).hour == 0, (path.name, record)
    assert orders > 0
    development = lab["selection"]["development"]
    reasons = {cause for result in development.values() for cause in result["exit_reasons"]}
    assert reasons <= {"exit_threshold", "regime_off", "missing_feature", "window"}
    # daily decisions trade far less than the hourly baseline
    baseline = lab["selection"]["baselines"]["momentum_v1"]["closed_trades"]
    assert max(r["closed_trades"] for r in development.values()) * 5 < baseline


def test_default_adapter_requires_twelve_trades(lab):
    selection = e05.develop(lab["spec"], repo_root=lab["root"], output_dir="var/run-strict")
    eligible = [k for k, r in selection["development"].items() if r["closed_trades"] >= 12]
    assert (selection["selected"] is None) == (not eligible)
    with pytest.raises(FileExistsError):
        e05.develop(lab["spec"], repo_root=lab["root"], output_dir="var/run-strict")
