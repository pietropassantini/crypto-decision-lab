"""S05: preregistered out-of-sample experiment for a Decision Model entry filter.

Protocol
--------
1. **Preregistration.**  The experiment spec (hypothesis, dataset digests,
   target, baseline, model, folds, threshold grid, decision criteria) is a
   versioned JSON file.  Its SHA-256 is written to ``preregistration.json``
   in the output directory *before* any data is read or any model is fit, and
   every artefact produced afterwards cites that digest.
2. **Chronological folds with purge.**  For each fold the training rows are
   the cutoffs whose target is *mature* (``cutoff + latency + horizon``)
   before ``dev_start``; development rows mature before ``test_start``; test
   rows lie in ``[test_start, test_end)``.  Violations raise.
3. **Training and threshold selection.**  The model is fit on training
   baseline entries only; the score threshold is chosen on development
   baseline entries from the preregistered grid (``0`` = no filter).
4. **Out-of-sample comparison.**  Baseline and filtered strategies run through
   the S03 engine on the same hourly market events, the same execution
   hypothesis (prices, latency, costs) and the same risk limits.  Strategies
   decide only at the S02 cutoffs, so entries and exits fill at the target
   prices.  A final ``close_only`` decision closes the open position.
5. **Decision.**  ``CANDIDATE``, ``INCONCLUSIVE`` or ``REJECTED`` is derived
   only from the preregistered criteria; each criterion is reported with its
   threshold and observed value.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from . import studio_analytics as analytics
from .dataset_builder import FEATURE_COLUMNS, verify_dataset
from .dataset_catalog import Catalog, load_market
from .decision_model import (
    CLOSE_ONLY,
    DECISION_POINT,
    DecisionModelAdapter,
    build_artefact,
    check_feature_schema,
    train_logistic,
    write_artefact,
)
from .execution_contract import ExecutionHypothesis, HOUR
from .forward_isolation import guard_research_output, guard_research_path
from .simulation import MarketEvent, RiskLimits, RunConfig, run

EXPERIMENT_SCHEMA = "cdl.s05.experiment.v1"
REPORT_SCHEMA = "cdl.s05.report.v1"
OUTCOMES = ("CANDIDATE", "INCONCLUSIVE", "REJECTED")
TARGET = "long_trade_worth"
NET_RETURN = "long_net_return_pct"


class ExperimentError(ValueError):
    """Raised when the protocol would be violated."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _finite(value: float | None) -> float | None:
    return None if value is None or not math.isfinite(float(value)) else float(value)


# ------------------------------------------------------------------ spec
@dataclass(frozen=True)
class Fold:
    name: str
    dev_start: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    train_start: pd.Timestamp | None = None


def load_spec(path: str | Path) -> tuple[dict, str]:
    """Return the validated spec and the SHA-256 of its LF-normalized text."""

    text = Path(path).read_text(encoding="utf-8-sig")
    spec = json.loads(text)
    validate_spec(spec)
    return spec, _sha256_text(text)


def validate_spec(spec: Mapping[str, Any]) -> None:
    if spec.get("schema_version") != EXPERIMENT_SCHEMA:
        raise ExperimentError(f"unsupported experiment schema: {spec.get('schema_version')}")
    for key in ("experiment_id", "hypothesis", "dataset", "target", "baseline", "model", "folds",
                "threshold_grid", "threshold_selection", "evaluation", "criteria"):
        if key not in spec:
            raise ExperimentError(f"experiment spec is missing '{key}'")
    if spec["target"].get("column") != TARGET:
        raise ExperimentError(f"target column must be {TARGET}")
    for key in ("features_content_sha256", "targets_content_sha256", "path"):
        if not spec["dataset"].get(key):
            raise ExperimentError(f"dataset.{key} must be pinned in the spec")
    try:
        features = check_feature_schema(spec["model"]["features"])
    except ValueError as exc:
        raise ExperimentError(f"invalid model features: {exc}") from exc
    unknown = sorted(set(features) - set(FEATURE_COLUMNS))
    if unknown:
        raise ExperimentError(f"model features not produced by the S02 dataset: {unknown}")
    grid = [float(v) for v in spec["threshold_grid"]]
    if not grid or any(not 0.0 <= v <= 1.0 for v in grid) or grid != sorted(set(grid)):
        raise ExperimentError("threshold_grid must be sorted unique probabilities")
    folds = parse_folds(spec)
    for previous, current in zip(folds, folds[1:]):
        if current.test_start < previous.test_end:
            raise ExperimentError("test windows must not overlap")
    criteria = spec["criteria"]
    for key in ("min_filtered_trades_per_fold", "min_winning_folds", "min_total_improvement_pct",
                "max_drawdown_tolerance_pct"):
        if key not in criteria:
            raise ExperimentError(f"criteria.{key} is required")
    if not 1 <= int(criteria["min_winning_folds"]) <= len(folds):
        raise ExperimentError("min_winning_folds must be between 1 and the number of folds")


def parse_folds(spec: Mapping[str, Any]) -> list[Fold]:
    folds = []
    for item in spec["folds"]:
        fold = Fold(
            name=str(item["name"]),
            dev_start=_utc(item["dev_start"]),
            test_start=_utc(item["test_start"]),
            test_end=_utc(item["test_end"]),
            train_start=_utc(item["train_start"]) if item.get("train_start") else None,
        )
        if not fold.dev_start < fold.test_start < fold.test_end:
            raise ExperimentError(f"fold {fold.name}: dev_start < test_start < test_end is required")
        folds.append(fold)
    if not folds:
        raise ExperimentError("at least one fold is required")
    return folds


# --------------------------------------------------------------- dataset
@dataclass
class DatasetBundle:
    directory: Path
    manifest: dict
    features: pd.DataFrame
    targets: pd.DataFrame
    hypothesis: ExecutionHypothesis
    stride_hours: int

    @property
    def maturity(self) -> pd.Timedelta:
        return (int(self.hypothesis.latency_hours) + int(self.hypothesis.horizon_hours)) * HOUR


def load_dataset(spec: Mapping[str, Any], repo_root: Path) -> DatasetBundle:
    directory = Path(spec["dataset"]["path"])
    directory = guard_research_path(directory if directory.is_absolute() else repo_root / directory,
                                    purpose="S05 experiment dataset")
    check = verify_dataset(directory)
    if not check["ok"]:
        raise ExperimentError(f"dataset verification failed: {check['mismatches']}")
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    outputs = manifest["outputs"]
    for name in ("features", "targets"):
        pinned = spec["dataset"][f"{name}_content_sha256"]
        if outputs[name]["content_sha256"] != pinned:
            raise ExperimentError(
                f"{name} content {outputs[name]['content_sha256']} differs from the preregistered {pinned}"
            )
    if manifest.get("provenance", {}).get("catalog_role") != "research":
        raise ExperimentError("dataset provenance is not a catalog research source")
    features = pd.read_parquet(directory / "features.parquet").sort_values("cutoff").reset_index(drop=True)
    targets = pd.read_parquet(directory / "targets.parquet").sort_values("cutoff").reset_index(drop=True)
    for frame in (features, targets):
        frame["cutoff"] = pd.to_datetime(frame["cutoff"], utc=True)
    if not (features["cutoff"].equals(targets["cutoff"]) and features["window_index"].equals(targets["window_index"])):
        raise ExperimentError("features and targets are not aligned by window")
    execution = manifest["spec"]["execution"]
    hypothesis = ExecutionHypothesis(
        latency_hours=int(execution["latency_hours"]),
        horizon_hours=int(execution["horizon_hours"]),
        fee_rate=float(execution["fee_rate_per_side"]),
        slippage=float(execution["slippage_per_side"]),
    )
    stride = int(manifest["spec"]["stride_hours"])
    if stride != int(hypothesis.horizon_hours):
        raise ExperimentError(
            "stride_hours must equal horizon_hours: decisions at consecutive cutoffs then reproduce the target exits"
        )
    return DatasetBundle(directory, manifest, features, targets, hypothesis, stride)


# --------------------------------------------------------------- folds
def split_fold(cutoffs: pd.Series, fold: Fold, maturity: pd.Timedelta) -> dict[str, np.ndarray]:
    """Index masks with purge: every target matures before the next boundary."""

    mature_at = cutoffs + maturity
    train = (mature_at <= fold.dev_start).to_numpy()
    if fold.train_start is not None:
        train &= (cutoffs >= fold.train_start).to_numpy()
    dev = ((cutoffs >= fold.dev_start) & (mature_at <= fold.test_start)).to_numpy()
    test = ((cutoffs >= fold.test_start) & (cutoffs < fold.test_end)).to_numpy()
    if (train & dev).any() or (train & test).any() or (dev & test).any():
        raise ExperimentError(f"fold {fold.name}: overlapping partitions")
    if not train.any() or not dev.any() or not test.any():
        raise ExperimentError(f"fold {fold.name}: empty train, development or test partition")
    if mature_at[train].max() > fold.dev_start or mature_at[dev].max() > fold.test_start:
        raise ExperimentError(f"fold {fold.name}: a target matures after its boundary")
    return {"train": train, "dev": dev, "test": test}


def fold_boundaries(cutoffs: pd.Series, masks: Mapping[str, np.ndarray], fold: Fold,
                    maturity: pd.Timedelta) -> dict[str, Any]:
    def span(mask):
        values = cutoffs[mask]
        return str(values.min()), str(values.max())

    train_first, train_last = span(masks["train"])
    dev_first, dev_last = span(masks["dev"])
    test_first, test_last = span(masks["test"])
    return {
        "dev_start": str(fold.dev_start), "test_start": str(fold.test_start), "test_end": str(fold.test_end),
        "train_first_cutoff": train_first, "train_last_cutoff": train_last,
        "train_last_target_matures": str(pd.Timestamp(train_last) + maturity),
        "dev_first_cutoff": dev_first, "dev_last_cutoff": dev_last,
        "dev_last_target_matures": str(pd.Timestamp(dev_last) + maturity),
        "test_first_cutoff": test_first, "test_last_cutoff": test_last,
        "maturity_hours": int(maturity / HOUR),
        "purge_ok": bool(pd.Timestamp(train_last) + maturity <= fold.dev_start
                         and pd.Timestamp(dev_last) + maturity <= fold.test_start),
    }


# --------------------------------------------------------------- selection
def select_threshold(scores: np.ndarray, net_pct: np.ndarray, grid, selection: Mapping[str, Any]):
    """Best preregistered threshold on development entries (ties -> less filtering)."""

    minimum = max(int(selection.get("min_kept", 1)),
                  int(math.ceil(float(selection.get("min_kept_fraction", 0.0)) * len(scores))))
    table, best = [], None
    for threshold in grid:
        kept = scores >= threshold if threshold > 0 else np.ones(len(scores), dtype=bool)
        count = int(kept.sum())
        objective = float(net_pct[kept].sum()) if count else 0.0
        eligible = count >= minimum
        table.append({"threshold": float(threshold), "kept": count, "objective_sum_net_pct": objective,
                      "mean_net_pct": float(net_pct[kept].mean()) if count else None, "eligible": eligible})
        if eligible and (best is None or objective > best["objective_sum_net_pct"]):
            best = table[-1]
    if best is None:
        raise ExperimentError("no threshold satisfies the preregistered minimum on the development set")
    return best["threshold"], table


def calibration(scores: np.ndarray, outcomes: np.ndarray, bins: int, base_rate: float) -> dict[str, Any]:
    scores = np.asarray(scores, dtype=float)
    outcomes = np.asarray(outcomes, dtype=float)
    edges = np.linspace(0.0, 1.0, int(bins) + 1)
    rows = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        mask = (scores >= lo) & ((scores < hi) if hi < 1.0 else (scores <= hi))
        count = int(mask.sum())
        rows.append({"bin": f"[{lo:.2f},{hi:.2f}{']' if hi >= 1.0 else ')'}", "count": count,
                     "mean_score": float(scores[mask].mean()) if count else None,
                     "observed_rate": float(outcomes[mask].mean()) if count else None})
    n = len(scores)
    brier = float(np.mean((scores - outcomes) ** 2)) if n else None
    reference = float(np.mean((base_rate - outcomes) ** 2)) if n else None
    auc = None
    if n and 0 < outcomes.sum() < n:
        from sklearn.metrics import roc_auc_score

        auc = float(roc_auc_score(outcomes, scores))
    return {"n": int(n), "positive_rate": float(outcomes.mean()) if n else None, "bins": rows,
            "brier": brier, "brier_reference_train_base_rate": reference,
            "brier_skill": (1.0 - brier / reference) if n and reference else None, "roc_auc": auc}


# --------------------------------------------------------------- engine
def build_events(market: pd.DataFrame, decisions: pd.DataFrame, feature_names, close_at: pd.Timestamp,
                 latency_hours: int) -> list[MarketEvent]:
    """Hourly events from the first test cutoff to the closing fill, features at cutoffs only."""

    start = decisions["cutoff"].min()
    end = close_at + int(latency_hours) * HOUR
    frame = market.loc[start:end]
    expected = pd.date_range(start, end, freq="1h", tz="UTC")
    missing = expected.difference(frame.index)
    if len(missing):
        raise ExperimentError(
            f"test window {start} -> {end} is not contiguous ({len(missing)} missing hours, first {missing[0]}); "
            "the S03 engine halts at gaps: choose test folds inside a contiguous segment"
        )
    by_cutoff = {row.cutoff: row for row in decisions.itertuples(index=False)}
    events = []
    for timestamp, bar in frame.iterrows():
        features: dict[str, float] = {}
        row = by_cutoff.get(timestamp)
        if row is not None:
            features = {name: float(getattr(row, name)) for name in feature_names}
            features[DECISION_POINT] = 1.0
        elif timestamp == close_at:
            features = {DECISION_POINT: 1.0, CLOSE_ONLY: 1.0}
        events.append(MarketEvent(timestamp, float(bar["open"]), features))
    return events


def engine_metrics(ledger: list[dict], initial_cash: float) -> dict[str, Any]:
    summary = analytics.summarize(ledger, initial_cash)
    trades = analytics.trades(ledger)
    months = analytics.window_summary(ledger, initial_cash, "M")
    checks = analytics.reconcile(ledger, initial_cash, "M")
    canonical = json.dumps(ledger, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {
        **summary,
        "trade_return_std_pct": _finite(trades["return_pct"].std(ddof=1)) if len(trades) > 1 else None,
        "trade_return_mean_pct": _finite(trades["return_pct"].mean()) if len(trades) else None,
        "monthly_return_std_pct": _finite(months["return_pct"].std(ddof=1)) if len(months) > 1 else None,
        "monthly_returns_pct": {row.window: float(row.return_pct) for row in months.itertuples()},
        "reconciled": all(check["passed"] for check in checks),
        "ledger_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


# --------------------------------------------------------------- decision
def decide_outcome(folds: list[Mapping[str, Any]], criteria: Mapping[str, Any]) -> tuple[str, list[dict]]:
    min_trades = int(criteria["min_filtered_trades_per_fold"])
    trades = [int(fold["engine"]["filtered"]["closed_trades"]) for fold in folds]
    improvements = [float(fold["improvement_pct"]) for fold in folds]
    wins = sum(1 for value in improvements if value > 0)
    baseline_total = 100.0 * (np.prod([1 + f["engine"]["baseline"]["net_return_pct"] / 100 for f in folds]) - 1)
    filtered_total = 100.0 * (np.prod([1 + f["engine"]["filtered"]["net_return_pct"] / 100 for f in folds]) - 1)
    total = float(filtered_total - baseline_total)
    tolerance = float(criteria["max_drawdown_tolerance_pct"])
    dd_deltas = [float(f["engine"]["filtered"]["max_drawdown_mtm_pct"] - f["engine"]["baseline"]["max_drawdown_mtm_pct"])
                 for f in folds]
    reasons = [
        {"criterion": "numerosità: trade filtrati chiusi per fold >= minimo", "threshold": min_trades,
         "observed": trades, "passed": min(trades) >= min_trades},
        {"criterion": "miglioramento composto totale (punti %) > minimo", "threshold": float(criteria["min_total_improvement_pct"]),
         "observed": total, "passed": total > float(criteria["min_total_improvement_pct"])},
        {"criterion": "fold con miglioramento > 0 >= minimo", "threshold": int(criteria["min_winning_folds"]),
         "observed": wins, "passed": wins >= int(criteria["min_winning_folds"])},
        {"criterion": "drawdown filtrato non peggiore della baseline oltre la tolleranza (punti %)",
         "threshold": -tolerance, "observed": dd_deltas, "passed": min(dd_deltas) >= -tolerance},
    ]
    numerosity, improvement, winning, drawdown = (reason["passed"] for reason in reasons)
    if not numerosity:
        outcome = "INCONCLUSIVE"
    elif improvement and winning and drawdown:
        outcome = "CANDIDATE"
    elif not improvement:
        outcome = "REJECTED"
    else:
        outcome = "INCONCLUSIVE"
    return outcome, reasons


# --------------------------------------------------------------- runner
def _write_json(path: Path, payload: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False, default=str) + "\n")


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_experiment(spec_path: str | Path, *, repo_root: str | Path = ".", output_dir: str | Path | None = None,
                   catalog_path: str | Path = "config/dataset-catalog.json") -> dict[str, Any]:
    root = Path(repo_root).resolve()
    spec, spec_sha = load_spec(spec_path)
    out = Path(output_dir) if output_dir is not None else Path("data/experiments") / spec["experiment_id"]
    out = guard_research_output(out if out.is_absolute() else root / out, purpose="S05 experiment output")
    if out.exists():
        raise FileExistsError(f"experiment output exists; refusing to overwrite: {out}")
    out.mkdir(parents=True)
    (out / "models").mkdir()
    (out / "ledgers").mkdir()
    # 1. Preregistration lock: written before any data access or fit.
    _write_json(out / "preregistration.json", {
        "experiment_id": spec["experiment_id"], "spec_sha256": spec_sha, "spec": spec,
        "locked_utc": _now(), "note": "written before reading data or fitting any model",
    })

    dataset = load_dataset(spec, root)
    catalog_file = Path(catalog_path) if Path(catalog_path).is_absolute() else root / catalog_path
    source_id = dataset.manifest["provenance"]["catalog_dataset_id"]
    market = load_market(Catalog.load(catalog_file), source_id, repo_root=root)
    if market.source_manifest_sha256 != dataset.manifest["provenance"].get("source_manifest_sha256"):
        raise ExperimentError("market source manifest differs from the one the dataset was built from")

    schema = check_feature_schema(spec["model"]["features"])
    base = spec["baseline"]
    feature_name = base.get("feature", "momentum_24h")
    momentum_threshold = float(base["momentum_threshold"])
    evaluation = spec["evaluation"]
    engine_hypothesis = ExecutionHypothesis(
        latency_hours=dataset.hypothesis.latency_hours,
        horizon_hours=int(evaluation["max_holding_hours"]),
        fee_rate=dataset.hypothesis.fee_rate,
        slippage=dataset.hypothesis.slippage,
    )
    risk = RiskLimits(max_exposure=float(evaluation["max_exposure"]), min_notional=float(evaluation["min_notional"]))
    cash = float(evaluation["initial_cash"])
    features, targets = dataset.features, dataset.targets
    cutoffs = features["cutoff"]
    signals = (features[feature_name] > momentum_threshold).to_numpy()
    worth = targets[TARGET].astype(int).to_numpy()
    net = targets[NET_RETURN].astype(float).to_numpy()
    X_all = features[schema].to_numpy(dtype=float)
    dataset_ref = {
        "path": spec["dataset"]["path"],
        "dataset_id": dataset.manifest.get("dataset_id"),
        "version": dataset.manifest.get("version"),
        "features_content_sha256": dataset.manifest["outputs"]["features"]["content_sha256"],
        "targets_content_sha256": dataset.manifest["outputs"]["targets"]["content_sha256"],
        "source_manifest_sha256": dataset.manifest["provenance"].get("source_manifest_sha256"),
    }

    fold_reports, predictions = [], []
    for fold in parse_folds(spec):
        masks = split_fold(cutoffs, fold, dataset.maturity)
        train_rows = masks["train"] & signals
        dev_rows = masks["dev"] & signals
        test_rows = masks["test"]
        parameters = train_logistic(
            X_all[train_rows], worth[train_rows], schema,
            C=float(spec["model"].get("C", 1.0)), seed=int(spec["model"]["seed"]),
            class_weight=spec["model"].get("class_weight"),
        )
        provisional = DecisionModelAdapter(build_artefact(parameters, score_threshold=0.0, provenance={}))
        dev_scores = provisional.score_matrix(X_all[dev_rows])
        threshold, table = select_threshold(dev_scores, net[dev_rows], spec["threshold_grid"],
                                            spec["threshold_selection"])
        boundaries = fold_boundaries(cutoffs, masks, fold, dataset.maturity)
        document = build_artefact(parameters, score_threshold=threshold, provenance={
            "experiment_id": spec["experiment_id"], "spec_sha256": spec_sha, "fold": fold.name,
            "dataset": dataset_ref, "train_population": "baseline entries (momentum signal) with mature targets",
            "boundaries": boundaries,
        })
        model_file = out / "models" / f"{fold.name}.json"
        write_artefact(document, model_file)
        adapter = DecisionModelAdapter.load(model_file, expected_sha256=document["model_sha256"])

        test_frame = features[test_rows].reset_index(drop=True)
        test_targets = targets[test_rows].reset_index(drop=True)
        test_signal = signals[test_rows]
        scores = adapter.score_matrix(test_frame[schema].to_numpy(dtype=float))
        kept = test_signal & ((scores >= threshold) if threshold > 0 else True)
        for i in range(len(test_frame)):
            predictions.append({"fold": fold.name, "cutoff": test_frame["cutoff"].iloc[i],
                                "signal": bool(test_signal[i]), "score": float(scores[i]),
                                "kept": bool(kept[i]), TARGET: bool(test_targets[TARGET].iloc[i]),
                                NET_RETURN: float(test_targets[NET_RETURN].iloc[i])})

        close_at = test_frame["cutoff"].max() + int(dataset.hypothesis.horizon_hours) * HOUR
        events = build_events(market.frame, test_frame, list(FEATURE_COLUMNS), close_at,
                              int(dataset.hypothesis.latency_hours))
        strategies = {
            "baseline": {"name": "scheduled_momentum",
                         "parameters": {"threshold": momentum_threshold, "feature": feature_name}},
            "filtered": {"name": "model_filtered_momentum",
                         "parameters": {"threshold": momentum_threshold, "feature": feature_name,
                                        "model_path": str(model_file), "model_sha256": adapter.model_sha256}},
        }
        engine = {}
        for variant, strategy in strategies.items():
            result = run(events, RunConfig(strategy=strategy, initial_cash=cash, hypothesis=engine_hypothesis,
                                           risk=risk), mode="backtest", source_id=source_id)
            (out / "ledgers" / f"{fold.name}-{variant}.json").write_text(result.canonical_ledger() + "\n",
                                                                         encoding="utf-8")
            engine[variant] = {**engine_metrics(result.ledger, cash), "engine_sha256": result.manifest["engine_sha256"]}
        signal_net = test_targets[NET_RETURN].to_numpy()[test_signal]
        kept_net = test_targets[NET_RETURN].to_numpy()[kept]
        dropped = test_signal & ~kept
        fold_reports.append({
            "name": fold.name,
            "boundaries": boundaries,
            "counts": {
                "train_rows": int(masks["train"].sum()), "train_entries": int(train_rows.sum()),
                "train_positive_rate": parameters["training_positive_rate"],
                "dev_entries": int(dev_rows.sum()), "test_windows": int(test_rows.sum()),
                "test_signals": int(test_signal.sum()), "test_kept": int(kept.sum()),
                "test_filtered_out": int(dropped.sum()),
            },
            "threshold": {"selected": threshold, "grid": table},
            "model": {"file": f"models/{fold.name}.json", "model_sha256": adapter.model_sha256,
                      "feature_schema": list(adapter.feature_schema)},
            "targets_view": {
                "baseline_mean_net_pct": _finite(signal_net.mean()) if len(signal_net) else None,
                "kept_mean_net_pct": _finite(kept_net.mean()) if len(kept_net) else None,
                "filtered_out_mean_net_pct": _finite(test_targets[NET_RETURN].to_numpy()[dropped].mean()) if dropped.any() else None,
                "baseline_hit_rate": _finite(test_targets[TARGET].to_numpy()[test_signal].mean()) if test_signal.any() else None,
                "kept_hit_rate": _finite(test_targets[TARGET].to_numpy()[kept].mean()) if kept.any() else None,
            },
            "calibration": calibration(scores[test_signal], test_targets[TARGET].to_numpy()[test_signal].astype(float),
                                       int(spec.get("calibration", {}).get("bins", 5)),
                                       parameters["training_positive_rate"]),
            "engine": engine,
            "improvement_pct": engine["filtered"]["net_return_pct"] - engine["baseline"]["net_return_pct"],
        })

    outcome, reasons = decide_outcome(fold_reports, spec["criteria"])
    prediction_frame = pd.DataFrame(predictions)
    pooled = prediction_frame[prediction_frame["signal"]]
    last = fold_reports[-1]
    candidate = {
        "experiment_id": spec["experiment_id"], "spec_sha256": spec_sha, "status": outcome,
        "usable_by_strategy_layer": outcome == "CANDIDATE",
        "model_file": last["model"]["file"], "model_sha256": last["model"]["model_sha256"],
        "feature_schema": last["model"]["feature_schema"], "score_threshold": last["threshold"]["selected"],
        "trained_until": last["boundaries"]["train_last_target_matures"], "dataset": dataset_ref,
        "strategy_config": {"name": "model_filtered_momentum", "parameters": {
            "threshold": momentum_threshold, "feature": feature_name,
            "model_path": f"<experiment_dir>/{last['model']['file']}", "model_sha256": last["model"]["model_sha256"]}},
    }
    report = {
        "schema_version": REPORT_SCHEMA,
        "experiment_id": spec["experiment_id"],
        "spec_sha256": spec_sha,
        "hypothesis": spec["hypothesis"],
        "dataset": dataset_ref,
        "execution": {"dataset": dataset.hypothesis.declaration(), "engine_max_holding_hours": int(evaluation["max_holding_hours"]),
                      "risk": {"max_exposure": risk.max_exposure, "min_notional": risk.min_notional}, "initial_cash": cash},
        "folds": fold_reports,
        "aggregate": {
            "baseline_compound_return_pct": float(100 * (np.prod([1 + f["engine"]["baseline"]["net_return_pct"] / 100 for f in fold_reports]) - 1)),
            "filtered_compound_return_pct": float(100 * (np.prod([1 + f["engine"]["filtered"]["net_return_pct"] / 100 for f in fold_reports]) - 1)),
            "pooled_calibration": calibration(pooled["score"].to_numpy(), pooled[TARGET].to_numpy(dtype=float),
                                              int(spec.get("calibration", {}).get("bins", 5)),
                                              float(np.mean([f["counts"]["train_positive_rate"] for f in fold_reports]))),
            "all_ledgers_reconciled": all(f["engine"][v]["reconciled"] for f in fold_reports for v in ("baseline", "filtered")),
        },
        "outcome": outcome,
        "criteria": reasons,
        "candidate": candidate,
    }
    prediction_frame.to_parquet(out / "predictions.parquet", index=False)
    _write_json(out / "report.json", report)
    _write_json(out / "candidate.json", candidate)
    files = sorted(p for p in out.rglob("*") if p.is_file() and p.name != "manifest.json")
    _write_json(out / "manifest.json", {
        "experiment_id": spec["experiment_id"], "spec_sha256": spec_sha, "outcome": outcome,
        "created_utc": _now(), "files": {p.relative_to(out).as_posix(): _file_sha256(p) for p in files},
    })
    return report


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="S05 preregistered Decision Model experiment")
    parser.add_argument("--spec", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--catalog", default="config/dataset-catalog.json")
    args = parser.parse_args(argv)
    report = run_experiment(args.spec, repo_root=args.repo_root, output_dir=args.output_dir, catalog_path=args.catalog)
    print(json.dumps({
        "outcome": report["outcome"],
        "folds": [{"fold": f["name"], "threshold": f["threshold"]["selected"],
                   "baseline_pct": round(f["engine"]["baseline"]["net_return_pct"], 3),
                   "filtered_pct": round(f["engine"]["filtered"]["net_return_pct"], 3),
                   "filtered_trades": f["engine"]["filtered"]["closed_trades"]} for f in report["folds"]],
        "aggregate": {k: v for k, v in report["aggregate"].items() if k != "pooled_calibration"},
        "criteria": [(c["criterion"], c["passed"]) for c in report["criteria"]],
    }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
