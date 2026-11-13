"""S05: preregistered Decision Model experiment (CP05.1, CP05.2, CP05.3)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("sklearn")

from crypto_decision_lab import pattern_experiment as pe  # noqa: E402
from crypto_decision_lab.dataset_builder import BuildSpec, TARGET_COLUMNS, build_dataset  # noqa: E402
from crypto_decision_lab.dataset_catalog import Catalog, load_market  # noqa: E402
from crypto_decision_lab.decision_model import (  # noqa: E402
    CLOSE_ONLY, DECISION_POINT, DecisionModelAdapter, DecisionModelError, build_artefact, train_logistic,
)
from crypto_decision_lab.simulation import Portfolio, RunConfig, select_strategy  # noqa: E402

from test_studio import STUDIO_RESEARCH_ID  # noqa: E402

DAYS = 420
FEATURES = ["rsi14", "atr_pct", "volatility_24h", "volatility_168h", "momentum_24h", "momentum_168h",
            "dist_sma50_pct", "trend_strength_pct", "volume_ratio"]


def _write_market(root: Path) -> None:
    rng = np.random.default_rng(7)
    hours = DAYS * 24
    returns = rng.normal(0.0001, 0.006, hours) + 0.002 * np.sin(np.arange(hours) / 97.0)
    opens = 100.0 * np.exp(np.cumsum(returns))
    closes = np.append(opens[1:], opens[-1] * 1.001)
    index = pd.date_range("2024-01-01", periods=hours, freq="1h", tz="UTC")
    frame = pd.DataFrame({"timestamp": index, "open": opens, "high": np.maximum(opens, closes) * 1.002,
                          "low": np.minimum(opens, closes) * 0.998, "close": closes,
                          "volume": rng.uniform(5, 15, hours)})
    chunk = root / "data" / "chunks" / "BTCUSDT" / "research.parquet"
    chunk.parent.mkdir(parents=True)
    frame.to_parquet(chunk, index=False)
    snapshot = root / "data" / "snapshots" / STUDIO_RESEARCH_ID
    snapshot.mkdir(parents=True)
    (snapshot / "raw-manifest.json").write_text(json.dumps({"dataset_id": STUDIO_RESEARCH_ID, "chunks": [
        {"file": "chunks/BTCUSDT/research.parquet", "sha256": hashlib.sha256(chunk.read_bytes()).hexdigest()}]}),
        encoding="utf-8")
    (root / "config").mkdir()
    (root / "config" / "dataset-catalog.json").write_text(json.dumps({
        "schema_version": "cdl.s02.catalog.v1",
        "sources": [{"dataset_id": STUDIO_RESEARCH_ID, "path": f"data/snapshots/{STUDIO_RESEARCH_ID}", "role": "research"},
                    {"dataset_id": "BTCUSDT-1h-forward-v1", "path": "data/forward/BTCUSDT-1h-forward-v1",
                     "role": "reserved-forward"}],
        "reserved_dataset_ids": ["BTCUSDT-1h-forward-v1"]}), encoding="utf-8")


def make_spec(root: Path, dataset: dict, **overrides) -> Path:
    spec = {
        "schema_version": pe.EXPERIMENT_SCHEMA,
        "experiment_id": "fixture-filter",
        "hypothesis": "fixture",
        "dataset": {"path": "data/datasets/research/fixture-v1",
                    "features_content_sha256": dataset["features_content_sha256"],
                    "targets_content_sha256": dataset["targets_content_sha256"]},
        "target": {"column": "long_trade_worth"},
        "baseline": {"feature": "momentum_24h", "momentum_threshold": 0.0},
        "model": {"features": FEATURES, "C": 1.0, "seed": 11},
        "folds": [
            {"name": "F1", "dev_start": "2024-05-01", "test_start": "2024-08-01", "test_end": "2024-11-01"},
            {"name": "F2", "dev_start": "2024-08-01", "test_start": "2024-11-01", "test_end": "2025-02-20"},
        ],
        "threshold_grid": [0.0, 0.4, 0.45, 0.5, 0.55, 0.6],
        "threshold_selection": {"min_kept": 5, "min_kept_fraction": 0.25},
        "evaluation": {"initial_cash": 10000, "max_exposure": 1.0, "min_notional": 10, "max_holding_hours": 720},
        "criteria": {"min_filtered_trades_per_fold": 3, "min_winning_folds": 1, "min_total_improvement_pct": 0.0,
                     "max_drawdown_tolerance_pct": 2.0},
        "calibration": {"bins": 5},
    }
    spec.update(overrides)
    path = root / "config" / "experiments" / f"{spec['experiment_id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec, indent=2), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def lab(tmp_path_factory):
    root = tmp_path_factory.mktemp("s05") / "repo"
    root.mkdir()
    _write_market(root)
    catalog = Catalog.load(root / "config" / "dataset-catalog.json")
    market = load_market(catalog, STUDIO_RESEARCH_ID, repo_root=root)
    dataset = build_dataset(market, BuildSpec(dataset_id="fixture", version="v1"),
                            output_dir=root / "data" / "datasets" / "research" / "fixture-v1",
                            source=catalog.entry(STUDIO_RESEARCH_ID))
    spec = make_spec(root, dataset)
    report = pe.run_experiment(spec, repo_root=root, output_dir="var/exp-a")
    return {"root": root, "dataset": dataset, "spec": spec, "report": report, "out": root / "var" / "exp-a"}


# ------------------------------------------------------------------ CP05.1
class TestCP051Splits:
    def test_purge_unit(self):
        cutoffs = pd.Series(pd.date_range("2024-01-01", periods=60, freq="D", tz="UTC"))
        fold = pe.Fold("x", pd.Timestamp("2024-01-20", tz="UTC"), pd.Timestamp("2024-02-10", tz="UTC"),
                       pd.Timestamp("2024-03-01", tz="UTC"))
        masks = pe.split_fold(cutoffs, fold, pd.Timedelta(hours=24))
        assert cutoffs[masks["train"]].max() == pd.Timestamp("2024-01-19", tz="UTC")  # matures exactly at dev_start
        assert cutoffs[masks["dev"]].max() == pd.Timestamp("2024-02-09", tz="UTC")
        longer = pe.split_fold(cutoffs, fold, pd.Timedelta(hours=48))
        assert cutoffs[longer["train"]].max() == pd.Timestamp("2024-01-18", tz="UTC")
        assert not (masks["train"] & masks["dev"]).any() and not (masks["dev"] & masks["test"]).any()

    def test_report_boundaries_respect_maturity(self, lab):
        for fold in lab["report"]["folds"]:
            b = fold["boundaries"]
            assert b["purge_ok"]
            assert pd.Timestamp(b["train_last_target_matures"]) <= pd.Timestamp(b["dev_start"])
            assert pd.Timestamp(b["dev_last_target_matures"]) <= pd.Timestamp(b["test_start"])
            assert pd.Timestamp(b["test_first_cutoff"]) >= pd.Timestamp(b["test_start"])

    def test_inference_features_exclude_targets(self, lab):
        for fold in lab["report"]["folds"]:
            assert not set(fold["model"]["feature_schema"]) & set(TARGET_COLUMNS)
        with pytest.raises(DecisionModelError, match="target"):
            train_logistic(np.zeros((4, 1)), np.array([0, 1, 0, 1]), ["long_net_return_pct"])
        with pytest.raises(pe.ExperimentError):
            pe.validate_spec({**json.loads(lab["spec"].read_text()), "model": {"features": ["exit_open"], "seed": 1}})

    def test_test_targets_do_not_influence_model_or_scores(self, lab):
        out = lab["out"]
        features = pd.read_parquet(lab["root"] / "data/datasets/research/fixture-v1/features.parquet")
        targets = pd.read_parquet(lab["root"] / "data/datasets/research/fixture-v1/targets.parquet")
        features["cutoff"] = pd.to_datetime(features["cutoff"], utc=True)
        fold = pe.parse_folds(json.loads(lab["spec"].read_text()))[0]
        masks = pe.split_fold(features["cutoff"], fold, pd.Timedelta(hours=24))
        signal = (features["momentum_24h"] > 0).to_numpy()
        y = targets["long_trade_worth"].astype(int).to_numpy()
        corrupted = y.copy()
        corrupted[masks["test"] | masks["dev"]] = 1 - corrupted[masks["test"] | masks["dev"]]
        X = features[FEATURES].to_numpy()
        a = train_logistic(X[masks["train"] & signal], y[masks["train"] & signal], FEATURES, seed=11)
        b = train_logistic(X[masks["train"] & signal], corrupted[masks["train"] & signal], FEATURES, seed=11)
        assert a == b
        saved = json.loads((out / "models" / "F1.json").read_text())
        assert saved["coef"] == a["coef"]
        # Stored test scores are a function of the features only.
        predictions = pd.read_parquet(out / "predictions.parquet")
        adapter = DecisionModelAdapter.load(out / "models" / "F1.json")
        rows = features.set_index("cutoff").loc[pd.to_datetime(predictions[predictions.fold == "F1"].cutoff, utc=True)]
        assert np.allclose(adapter.score_matrix(rows[FEATURES].to_numpy()), predictions[predictions.fold == "F1"].score)

    def test_rerun_is_reproducible_within_tolerance(self, lab):
        again = pe.run_experiment(lab["spec"], repo_root=lab["root"], output_dir="var/exp-b")
        assert again == lab["report"]
        first = json.loads((lab["out"] / "models" / "F2.json").read_text())
        second = json.loads((lab["root"] / "var/exp-b/models/F2.json").read_text())
        assert np.allclose(first["coef"], second["coef"], atol=1e-9, rtol=0)
        assert first["model_sha256"] == second["model_sha256"]


# ------------------------------------------------------------------ CP05.2
class TestCP052Report:
    def test_counts_engine_and_variability(self, lab):
        for fold in lab["report"]["folds"]:
            counts = fold["counts"]
            assert counts["test_kept"] + counts["test_filtered_out"] == counts["test_signals"]
            for variant in ("baseline", "filtered"):
                engine = fold["engine"][variant]
                assert engine["reconciled"]
                assert engine["open_trades"] == 0  # close_only decision closes every position
                for key in ("net_return_pct", "max_drawdown_mtm_pct", "costs", "monthly_return_std_pct",
                            "trade_return_std_pct", "ledger_sha256"):
                    assert key in engine
            assert fold["improvement_pct"] == pytest.approx(
                fold["engine"]["filtered"]["net_return_pct"] - fold["engine"]["baseline"]["net_return_pct"])
        assert lab["report"]["aggregate"]["all_ledgers_reconciled"]

    def test_calibration_has_counts(self, lab):
        for fold in lab["report"]["folds"]:
            calib = fold["calibration"]
            assert calib["n"] == fold["counts"]["test_signals"]
            assert sum(row["count"] for row in calib["bins"]) == calib["n"]
            assert calib["brier"] is not None and 0 <= calib["brier"] <= 1
        pooled = lab["report"]["aggregate"]["pooled_calibration"]
        assert pooled["n"] == sum(f["counts"]["test_signals"] for f in lab["report"]["folds"])

    def test_engine_entries_use_target_prices_at_kept_cutoffs(self, lab):
        predictions = pd.read_parquet(lab["out"] / "predictions.parquet")
        targets = pd.read_parquet(lab["root"] / "data/datasets/research/fixture-v1/targets.parquet")
        targets["cutoff"] = pd.to_datetime(targets["cutoff"], utc=True)
        entry_price = dict(zip(targets["cutoff"], targets["entry_exec_price"]))
        for fold in ("F1", "F2"):
            kept = set(pd.to_datetime(predictions[(predictions.fold == fold) & predictions.kept].cutoff, utc=True))
            ledger = json.loads((lab["out"] / "ledgers" / f"{fold}-filtered.json").read_text())
            buys = [r for r in ledger if r["kind"] == "execution" and r["side"] == "BUY"]
            assert buys
            for fill in buys:
                stamp = pd.Timestamp(fill["timestamp"])
                assert stamp in kept
                assert fill["price"] == pytest.approx(entry_price[stamp], rel=1e-12)


# ------------------------------------------------------------------ CP05.3
class TestCP053Identity:
    def test_artefacts_identifiable(self, lab):
        out, report = lab["out"], lab["report"]
        prereg = json.loads((out / "preregistration.json").read_text())
        assert prereg["spec_sha256"] == report["spec_sha256"]
        assert prereg["spec_sha256"] == hashlib.sha256(lab["spec"].read_text().replace("\r\n", "\n").encode()).hexdigest()
        candidate = json.loads((out / "candidate.json").read_text())
        model = json.loads((out / candidate["model_file"]).read_text())
        assert candidate["model_sha256"] == model["model_sha256"]
        assert candidate["feature_schema"] == model["feature_schema"] == FEATURES
        assert candidate["score_threshold"] == model["score_threshold"]
        assert candidate["dataset"]["features_content_sha256"] == lab["dataset"]["features_content_sha256"]
        assert model["provenance"]["spec_sha256"] == report["spec_sha256"]
        manifest = json.loads((out / "manifest.json").read_text())
        for name, digest in manifest["files"].items():
            assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest
        assert report["outcome"] in pe.OUTCOMES
        assert candidate["usable_by_strategy_layer"] == (report["outcome"] == "CANDIDATE")
        assert len(report["criteria"]) == 4 and all("observed" in c for c in report["criteria"])

    def test_dataset_digest_mismatch_and_overwrite_refused(self, lab):
        bad = make_spec(lab["root"], {**lab["dataset"], "features_content_sha256": "0" * 64}, experiment_id="bad")
        with pytest.raises(pe.ExperimentError, match="preregistered"):
            pe.run_experiment(bad, repo_root=lab["root"], output_dir="var/exp-bad")
        with pytest.raises(FileExistsError):
            pe.run_experiment(lab["spec"], repo_root=lab["root"], output_dir="var/exp-a")

    def test_forward_output_refused(self, lab):
        from crypto_decision_lab.forward_isolation import ForwardIsolationViolation
        with pytest.raises(ForwardIsolationViolation):
            pe.run_experiment(lab["spec"], repo_root=lab["root"], output_dir="data/forward/x/exp")


def _engine(net, dd, trades):
    return {"net_return_pct": net, "max_drawdown_mtm_pct": dd, "closed_trades": trades}


def _fold(base, filt, base_dd=-10.0, filt_dd=-10.0, trades=30):
    return {"engine": {"baseline": _engine(base, base_dd, 50), "filtered": _engine(filt, filt_dd, trades)},
            "improvement_pct": filt - base}


CRITERIA = {"min_filtered_trades_per_fold": 20, "min_winning_folds": 2, "min_total_improvement_pct": 0.0,
            "max_drawdown_tolerance_pct": 2.0}


@pytest.mark.parametrize("folds,expected", [
    ([_fold(1, 3), _fold(2, 4), _fold(0, -1)], "CANDIDATE"),
    ([_fold(1, 0), _fold(2, 1), _fold(0, 1)], "REJECTED"),
    ([_fold(1, 3), _fold(2, 4, trades=5)], "INCONCLUSIVE"),
    ([_fold(1, 9), _fold(2, 1), _fold(0, -1)], "INCONCLUSIVE"),
    ([_fold(1, 3), _fold(2, 4, filt_dd=-15.0)], "INCONCLUSIVE"),
])
def test_cp053_outcome_rules(folds, expected):
    outcome, reasons = pe.decide_outcome(folds, CRITERIA)
    assert outcome == expected
    assert all(r["passed"] for r in reasons) == (expected == "CANDIDATE")


# ------------------------------------------------------------------ adapter contract
class TestAdapterContract:
    @pytest.fixture()
    def artefact(self, tmp_path):
        rng = np.random.default_rng(3)
        X = rng.normal(size=(200, 3))
        y = (X[:, 0] + rng.normal(scale=0.5, size=200) > 0).astype(int)
        params = train_logistic(X, y, ["a", "b", "c"], seed=5)
        document = build_artefact(params, score_threshold=0.5, provenance={"test": True})
        path = tmp_path / "model.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path, document, X, y

    def test_matches_sklearn_and_is_bounded(self, artefact):
        from sklearn.linear_model import LogisticRegression
        path, document, X, y = artefact
        adapter = DecisionModelAdapter.load(path, expected_sha256=document["model_sha256"])
        mean, scale = np.asarray(document["mean"]), np.asarray(document["scale"])
        reference = LogisticRegression(max_iter=5000, random_state=5).fit((X - mean) / scale, y)
        expected = reference.predict_proba((X - mean) / scale)[:, 1]
        scores = adapter.score_matrix(X)
        assert np.allclose(scores, expected, atol=1e-12)
        assert ((scores >= 0) & (scores <= 1)).all()
        assert adapter.score({"a": X[0, 0], "b": X[0, 1], "c": X[0, 2]}) == pytest.approx(scores[0])

    def test_missing_or_non_finite_feature_gives_none(self, artefact):
        adapter = DecisionModelAdapter.load(artefact[0])
        assert adapter.score({"a": 1.0, "b": 2.0}) is None
        assert adapter.score({"a": 1.0, "b": 2.0, "c": float("nan")}) is None

    def test_tampered_or_unexpected_artefact_refused(self, artefact, tmp_path):
        path, document, _, _ = artefact
        with pytest.raises(DecisionModelError, match="expects"):
            DecisionModelAdapter.load(path, expected_sha256="f" * 64)
        tampered = dict(document, intercept=document["intercept"] + 1)
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps(tampered), encoding="utf-8")
        with pytest.raises(DecisionModelError, match="digest"):
            DecisionModelAdapter.load(bad)

    def test_strategy_layer_consumes_score(self, artefact):
        path, document, X, _ = artefact
        params = {"model_path": str(path), "model_sha256": document["model_sha256"], "threshold": 0.0, "feature": "a"}
        RunConfig(strategy={"name": "model_filtered_momentum", "parameters": params})  # selectable from config
        strategy = select_strategy({"name": "model_filtered_momentum", "parameters": params})
        flat, held = Portfolio(10000.0), Portfolio(0.0, 1.0)
        point = {DECISION_POINT: 1.0, "a": 2.0, "b": 0.0, "c": 0.0}
        assert strategy.decide(None, {"a": 2.0}, flat).exposure == "FLAT"  # no decision point: hold FLAT
        assert strategy.decide(None, {}, held).exposure == "LONG"  # no decision point: hold LONG
        assert strategy.decide(None, {DECISION_POINT: 1.0, CLOSE_ONLY: 1.0}, held).exposure == "FLAT"
        assert "fail closed" in strategy.decide(None, {DECISION_POINT: 1.0, "a": 2.0}, flat).reason
        decision = strategy.decide(None, point, flat)
        score = DecisionModelAdapter.load(path).score(point)
        assert decision.exposure == ("LONG" if score >= 0.5 else "FLAT") and "score=" in decision.reason
        low = select_strategy({"name": "model_filtered_momentum", "parameters": {**params, "score_threshold": 0.0}})
        assert low.decide(None, point, flat).exposure == "LONG"
        with pytest.raises(DecisionModelError):
            select_strategy({"name": "model_filtered_momentum", "parameters": {**params, "model_sha256": "0" * 64}})
