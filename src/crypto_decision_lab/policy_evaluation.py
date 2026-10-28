"""S02-CP06B STEP 4-5: selective forecasting policies with walk-forward validation.

Leakage contract
----------------
* Every policy decision for a test window uses only:
  - rows of the *training* folds (features and outcomes), and
  - cutoff-available information of the test window itself (regime features and
    the Timestral forecast value, which is produced at cutoff time).
* Test-window outcomes (actual price, errors, winner label) never enter a real
  policy.  The single exception is ``oracle``, a theoretical bound that is
  explicitly flagged with ``uses_target=True`` and is not deployable.
* Folds are chronological, expanding-window, with a 24h purge so that no
  training target overlaps a test target.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .regime_analysis import (
    BOOTSTRAP_BLOCKS,
    BOOTSTRAP_DRAWS,
    BOOTSTRAP_SEED,
    assign_regimes,
    fit_tercile_thresholds,
    moving_block_bootstrap_ci,
)

POLICY_SCHEMA = "cdl.cp06b.policy.v1"
DECISION_TIMESTRAL = "TIMESTRAL"
DECISION_NAIVE = "NAIVE"
HORIZON_HOURS = 24
PURGE_HOURS = HORIZON_HOURS
MIN_CELL_OBS = 10
LOGISTIC_FEATURES = (
    "rsi14",
    "atr_pct",
    "volatility_24h",
    "volatility_168h",
    "momentum_24h",
    "momentum_168h",
    "dist_sma50_pct",
    "trend_strength_pct",
    "volume_ratio",
    "timestral_return_pct_24h",
)
LOGISTIC_C_GRID = (0.01, 0.1, 1.0, 10.0)
INNER_VALIDATION_FRACTION = 0.2


@dataclass(frozen=True)
class PolicyPaths:
    regime_dataset_dir: Path = Path("data/datasets/S02-CP06B-regime-features-v1")
    output_dir: Path = Path("data/experiments/S02-CP06B-selective-policy-v1")


class PolicyError(ValueError):
    """Raised when a policy evaluation would leak or misalign data."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class PolicySpec:
    name: str
    label: str
    uses_target: bool
    description: str
    trainable: bool = False


POLICY_CATALOG: dict[str, PolicySpec] = {
    "always_naive": PolicySpec("always_naive", "Always Naive", False, "Persistence baseline on every window."),
    "always_timestral": PolicySpec("always_timestral", "Always Timestral", False, "Timestral p50 on every window."),
    "regime_rules": PolicySpec(
        "regime_rules",
        "Regime rules",
        False,
        "Per combined-regime choice fitted on training delta MAE; train-only tercile thresholds.",
        trainable=True,
    ),
    "logistic": PolicySpec(
        "logistic",
        "Logistic (L2)",
        False,
        "Regularized classifier on cutoff features + Timestral forecast; C chosen on inner chronological split.",
        trainable=True,
    ),
    "oracle": PolicySpec(
        "oracle",
        "Oracle (theoretical)",
        True,
        "Picks the closer prediction per window using the realized outcome; not deployable.",
    ),
}


class AlwaysPolicy:
    """Fixed model choice; the training fold is only used for reporting."""

    def __init__(self, decision: str) -> None:
        self.decision = decision

    def fit(self, train: pd.DataFrame) -> dict[str, Any]:
        return {"decision": self.decision}

    def decide(self, test: pd.DataFrame, state: dict[str, Any]) -> pd.Series:
        return pd.Series(self.decision, index=test.index)

    def explain(self, test: pd.DataFrame, state: dict[str, Any]) -> pd.Series:
        return pd.Series("fixed", index=test.index)


class RegimeRulePolicy:
    """Combined trend/vol regime table fitted on training delta MAE."""

    def __init__(self, min_cell_obs: int = MIN_CELL_OBS) -> None:
        self.min_cell_obs = int(min_cell_obs)

    def _labels(self, frame: pd.DataFrame, state: dict[str, Any]) -> pd.Series:
        return (
            assign_trend(frame, state["trend_thresholds"])
            + "/"
            + assign_vol(frame, state["vol_thresholds"])
        )

    def fit(self, train: pd.DataFrame) -> dict[str, Any]:
        trend_thresholds = fit_tercile_thresholds(train["momentum_168h"].to_numpy(dtype=float))
        vol_thresholds = fit_tercile_thresholds(train["volatility_168h"].to_numpy(dtype=float))
        labelled = train.copy()
        labelled["combined_regime"] = self._labels(labelled, {"trend_thresholds": trend_thresholds, "vol_thresholds": vol_thresholds})
        table: dict[str, str] = {}
        for regime, group in labelled.groupby("combined_regime"):
            if len(group) >= self.min_cell_obs:
                table[regime] = DECISION_TIMESTRAL if group["delta_mae"].mean() < 0 else DECISION_NAIVE
        fallback = DECISION_TIMESTRAL if labelled["delta_mae"].mean() < 0 else DECISION_NAIVE
        return {
            "trend_thresholds": trend_thresholds,
            "vol_thresholds": vol_thresholds,
            "table": table,
            "fallback": fallback,
            "cell_observations": {regime: int(len(group)) for regime, group in labelled.groupby("combined_regime")},
        }

    def decide(self, test: pd.DataFrame, state: dict[str, Any]) -> pd.Series:
        regimes = self._labels(test, state)
        return regimes.map(lambda regime: state["table"].get(regime, state["fallback"]))

    def explain(self, test: pd.DataFrame, state: dict[str, Any]) -> pd.Series:
        regimes = self._labels(test, state)
        return regimes.map(
            lambda regime: (
                f"cell={regime}"
                if regime in state["table"]
                else f"fallback (cell={regime}, n_train={state['cell_observations'].get(regime, 0)})"
            )
        )


class LogisticPolicy:
    """L2 logistic classifier on cutoff features; C tuned on an inner split."""

    def fit(self, train: pd.DataFrame) -> dict[str, Any]:
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        x = train[list(LOGISTIC_FEATURES)].to_numpy(dtype=float)
        y = (train["delta_mae"].to_numpy(dtype=float) < 0).astype(int)
        split = max(1, int(len(train) * (1.0 - INNER_VALIDATION_FRACTION)))
        inner_train_x, inner_val_x = x[:split], x[split:]
        inner_train_y = y[:split]
        validation_frame = train.iloc[split:]
        timestral_abs = validation_frame["abs_error_timestral"].to_numpy(dtype=float)
        naive_abs = validation_frame["abs_error_naive"].to_numpy(dtype=float)
        if len(inner_val_x) == 0 or len(np.unique(inner_train_y)) < 2:
            chosen_c = LOGISTIC_C_GRID[1]
        else:
            best_c, best_mae = None, None
            for c_value in LOGISTIC_C_GRID:
                candidate = make_pipeline(
                    StandardScaler(), LogisticRegression(C=c_value, max_iter=2000, solver="lbfgs")
                )
                candidate.fit(inner_train_x, inner_train_y)
                probability = candidate.predict_proba(inner_val_x)[:, 1]
                chosen = np.where(probability >= 0.5, DECISION_TIMESTRAL, DECISION_NAIVE)
                mae = float(np.mean(np.where(chosen == DECISION_TIMESTRAL, timestral_abs, naive_abs)))
                if best_mae is None or mae < best_mae:
                    best_mae, chosen_c = mae, c_value
            chosen_c = float(chosen_c)
        model = make_pipeline(
            StandardScaler(), LogisticRegression(C=chosen_c, max_iter=2000, solver="lbfgs")
        )
        model.fit(x, y)
        return {"model": model, "chosen_c": float(chosen_c), "feature_names": list(LOGISTIC_FEATURES)}

    def decide(self, test: pd.DataFrame, state: dict[str, Any]) -> pd.Series:
        probability = state["model"].predict_proba(test[list(LOGISTIC_FEATURES)].to_numpy(dtype=float))[:, 1]
        return pd.Series(
            np.where(probability >= 0.5, DECISION_TIMESTRAL, DECISION_NAIVE), index=test.index
        )

    def explain(self, test: pd.DataFrame, state: dict[str, Any]) -> pd.Series:
        probability = state["model"].predict_proba(test[list(LOGISTIC_FEATURES)].to_numpy(dtype=float))[:, 1]
        return pd.Series(
            [f"p_timestral={value:.3f} (C={state['chosen_c']:g})" for value in probability],
            index=test.index,
        )


class OraclePolicy:
    """Theoretical bound: uses the realized outcome. Never deployable."""

    uses_target = True

    def fit(self, train: pd.DataFrame) -> dict[str, Any]:
        return {}

    def decide(self, test: pd.DataFrame, state: dict[str, Any]) -> pd.Series:
        return pd.Series(
            np.where(
                test["abs_error_timestral"].to_numpy(dtype=float)
                <= test["abs_error_naive"].to_numpy(dtype=float),
                DECISION_TIMESTRAL,
                DECISION_NAIVE,
            ),
            index=test.index,
        )

    def explain(self, test: pd.DataFrame, state: dict[str, Any]) -> pd.Series:
        return pd.Series("oracle: realized outcome", index=test.index)


def assign_trend(frame: pd.DataFrame, thresholds: tuple[float, float]) -> pd.Series:
    from .regime_analysis import assign_trend_regime

    return assign_trend_regime(frame["momentum_168h"], thresholds)


def assign_vol(frame: pd.DataFrame, thresholds: tuple[float, float]) -> pd.Series:
    from .regime_analysis import assign_vol_regime

    return assign_vol_regime(frame["volatility_168h"], thresholds)


POLICY_IMPLEMENTATIONS: dict[str, Any] = {
    "always_naive": lambda: AlwaysPolicy(DECISION_NAIVE),
    "always_timestral": lambda: AlwaysPolicy(DECISION_TIMESTRAL),
    "regime_rules": lambda: RegimeRulePolicy(),
    "logistic": lambda: LogisticPolicy(),
    "oracle": lambda: OraclePolicy(),
}


def load_regime_dataset(dataset_dir: Path) -> pd.DataFrame:
    features = pd.read_parquet(dataset_dir / "features.parquet")
    errors = pd.read_parquet(dataset_dir / "errors.parquet")
    merged = errors.merge(
        features.drop(columns=["cutoff"]),
        on="window_index",
        how="inner",
        validate="one_to_one",
    )
    if len(merged) != len(errors) or len(merged) == 0:
        raise PolicyError("Regime dataset features/errors do not align")
    merged["cutoff"] = pd.to_datetime(merged["cutoff"], utc=True)
    merged["target"] = pd.to_datetime(merged["target"], utc=True)
    return merged.sort_values("cutoff").reset_index(drop=True)


def make_folds(
    data: pd.DataFrame,
    *,
    n_folds: int = 5,
    min_train: int = 200,
    test_size: int = 60,
) -> list[dict[str, Any]]:
    """Chronological expanding-window folds with a 24h purge gap."""

    total = len(data)
    if min_train + n_folds * test_size > total:
        raise PolicyError(
            f"Not enough windows for {n_folds} folds: need {min_train + n_folds * test_size}, have {total}"
        )
    folds: list[dict[str, Any]] = []
    for fold in range(n_folds):
        test_start = min_train + fold * test_size
        test_stop = test_start + test_size
        test_block = data.iloc[test_start:test_stop]
        train_block = data.iloc[:test_start]
        first_test_cutoff = test_block["cutoff"].iloc[0]
        target_end = train_block["cutoff"] + pd.Timedelta(hours=HORIZON_HOURS)
        keep = target_end <= first_test_cutoff
        purged = int((~keep).sum())
        folds.append(
            {
                "fold": fold,
                "train_index": train_block.index[keep].tolist(),
                "test_index": test_block.index.tolist(),
                "train_size": int(keep.sum()),
                "test_size": int(len(test_block)),
                "purged_windows": purged,
                "first_test_cutoff": str(first_test_cutoff),
                "last_train_cutoff": str(train_block.loc[keep, "cutoff"].iloc[-1]) if keep.any() else None,
            }
        )
    return folds


def _chosen_price(frame: pd.DataFrame, decisions: pd.Series) -> np.ndarray:
    return np.where(
        decisions.to_numpy() == DECISION_TIMESTRAL,
        frame["timestral_price_24h"].to_numpy(dtype=float),
        frame["naive_price_24h"].to_numpy(dtype=float),
    )


def _fold_metrics(frame: pd.DataFrame, decisions: pd.Series) -> dict[str, float]:
    chosen = _chosen_price(frame, decisions)
    actual = frame["actual_price_24h"].to_numpy(dtype=float)
    abs_error = np.abs(chosen - actual)
    naive_abs = frame["abs_error_naive"].to_numpy(dtype=float)
    return {
        "samples": int(len(frame)),
        "mae_usdt": float(abs_error.mean()),
        "rmse_usdt": float(np.sqrt((abs_error**2).mean())),
        "bias_usdt": float((chosen - actual).mean()),
        "share_timestral": float((decisions.to_numpy() == DECISION_TIMESTRAL).mean()),
        "win_rate_vs_naive": float((abs_error < naive_abs).mean()),
        "delta_mae_vs_naive_usdt": float((abs_error - naive_abs).mean()),
    }


def evaluate_policies(
    data: pd.DataFrame,
    *,
    n_folds: int = 5,
    min_train: int = 200,
    test_size: int = 60,
    policies: tuple[str, ...] = ("always_naive", "always_timestral", "regime_rules", "logistic", "oracle"),
) -> dict[str, Any]:
    """Walk-forward evaluation; every policy is judged on the same test rows."""

    folds = make_folds(data, n_folds=n_folds, min_train=min_train, test_size=test_size)
    decision_rows: list[pd.DataFrame] = []
    fold_summaries: list[dict[str, Any]] = []
    states: dict[str, dict[str, Any]] = {}
    for fold_info in folds:
        train = data.loc[fold_info["train_index"]]
        test = data.loc[fold_info["test_index"]]
        for policy_name in policies:
            spec = POLICY_CATALOG[policy_name]
            implementation = POLICY_IMPLEMENTATIONS[policy_name]()
            state = implementation.fit(train)
            states[policy_name] = state
            decisions = implementation.decide(test, state)
            explanation = implementation.explain(test, state)
            chosen = _chosen_price(test, decisions)
            actual = test["actual_price_24h"].to_numpy(dtype=float)
            abs_error = np.abs(chosen - actual)
            if policy_name == "regime_rules":
                trend_labels = assign_trend(test, state["trend_thresholds"])
                vol_labels = assign_vol(test, state["vol_thresholds"])
            else:
                trend_labels = pd.Series([None] * len(test), index=test.index)
                vol_labels = pd.Series([None] * len(test), index=test.index)
            decision_rows.append(
                pd.DataFrame(
                    {
                        "fold": fold_info["fold"],
                        "policy": policy_name,
                        "window_index": test["window_index"].to_numpy(),
                        "cutoff": test["cutoff"].to_numpy(),
                        "target": test["target"].to_numpy(),
                        "trend_regime": trend_labels.to_numpy(),
                        "vol_regime": vol_labels.to_numpy(),
                        "decision": decisions.to_numpy(),
                        "explanation": explanation.to_numpy(),
                        "chosen_price_24h": chosen,
                        "actual_price_24h": actual,
                        "abs_error": abs_error,
                        "abs_error_naive": test["abs_error_naive"].to_numpy(dtype=float),
                        "abs_error_timestral": test["abs_error_timestral"].to_numpy(dtype=float),
                        "delta_vs_naive": abs_error - test["abs_error_naive"].to_numpy(dtype=float),
                        "uses_target": spec.uses_target,
                        "out_of_sample": True,
                    }
                )
            )
            fold_summaries.append(
                {
                    "fold": fold_info["fold"],
                    "policy": policy_name,
                    **_fold_metrics(test, decisions),
                    "purged_windows": fold_info["purged_windows"],
                    "train_size": fold_info["train_size"],
                }
            )
    decisions_frame = pd.concat(decision_rows, ignore_index=True)

    aggregate: dict[str, Any] = {}
    for policy_name in policies:
        subset = decisions_frame[decisions_frame["policy"] == policy_name]
        delta = subset["delta_vs_naive"].to_numpy(dtype=float)
        ci = moving_block_bootstrap_ci(delta)
        per_fold = [
            {key: value for key, value in row.items() if key != "policy"}
            for row in [item for item in fold_summaries if item["policy"] == policy_name]
        ]
        aggregate[policy_name] = {
            "label": POLICY_CATALOG[policy_name].label,
            "uses_target": POLICY_CATALOG[policy_name].uses_target,
            "trainable": POLICY_CATALOG[policy_name].trainable,
            "out_of_sample": {
                "samples": int(len(subset)),
                "mae_usdt": float(subset["abs_error"].mean()),
                "rmse_usdt": float(np.sqrt((subset["abs_error"].to_numpy(dtype=float) ** 2).mean())),
                "bias_usdt": float(
                    (subset["chosen_price_24h"] - subset["actual_price_24h"]).mean()
                ),
                "share_timestral": float((subset["decision"] == DECISION_TIMESTRAL).mean()),
                "win_rate_vs_naive": float(
                    (subset["abs_error"] < subset["abs_error_naive"]).mean()
                ),
                "delta_mae_vs_naive_usdt": float(delta.mean()),
                "delta_mae_vs_naive_ci": {
                    "point": ci["point"],
                    "ci_low": ci["ci_low"],
                    "ci_high": ci["ci_high"],
                    "draws": ci["draws"],
                },
            },
            "per_fold": per_fold,
        }

    in_sample: dict[str, Any] = {}
    for policy_name in policies:
        implementation = POLICY_IMPLEMENTATIONS[policy_name]()
        state = implementation.fit(data)
        decisions = implementation.decide(data, state)
        in_sample[policy_name] = {
            "label": POLICY_CATALOG[policy_name].label,
            **_fold_metrics(data, decisions),
            "note": "In-sample on all 500 windows; optimistic for trainable policies.",
        }

    leakage_checks = run_leakage_checks(data, decisions_frame, n_folds=n_folds, min_train=min_train, test_size=test_size)
    return {
        "folds": folds,
        "aggregate": aggregate,
        "in_sample_full_period": in_sample,
        "decisions": decisions_frame,
        "leakage_checks": leakage_checks,
        "fold_summaries": fold_summaries,
        "fit_states": {
            name: {
                key: value for key, value in state.items() if key != "model"
            }
            for name, state in states.items()
        },
    }


def run_leakage_checks(
    data: pd.DataFrame,
    decisions_frame: pd.DataFrame,
    *,
    n_folds: int,
    min_train: int,
    test_size: int,
) -> list[dict[str, Any]]:
    """Structural guarantees: ordering, purge, paired samples, outcome independence."""

    folds = make_folds(data, n_folds=n_folds, min_train=min_train, test_size=test_size)
    checks: list[dict[str, Any]] = []
    chronological = all(
        data.loc[fold["train_index"], "cutoff"].max() < data.loc[fold["test_index"], "cutoff"].min()
        for fold in folds
    )
    checks.append(
        {
            "check": "training precedes testing in every fold",
            "passed": bool(chronological),
            "detail": "max(train cutoff) < min(test cutoff) for all folds",
        }
    )
    no_overlap = all(
        (
            data.loc[fold["train_index"], "cutoff"].max() + pd.Timedelta(hours=HORIZON_HOURS)
            <= data.loc[fold["test_index"], "cutoff"].min()
        )
        for fold in folds
    )
    checks.append(
        {
            "check": "no train target overlaps test targets (24h purge)",
            "passed": bool(no_overlap),
            "detail": f"purge={PURGE_HOURS}h on horizon {HORIZON_HOURS}h",
        }
    )
    counts = decisions_frame.groupby("policy")["window_index"].nunique()
    expected = len(data) - min_train
    paired = bool((counts == expected).all())
    checks.append(
        {
            "check": "all policies evaluated on identical out-of-sample windows",
            "passed": paired,
            "detail": f"per-policy windows={counts.min()}..{counts.max()} expected={expected}",
        }
    )
    real = decisions_frame[~decisions_frame["uses_target"]]
    oracle = decisions_frame[decisions_frame["uses_target"]]
    oracle_bound = float(oracle["abs_error"].mean()) <= float(real.groupby("policy")["abs_error"].mean().min())
    checks.append(
        {
            "check": "oracle is a lower bound for real policies",
            "passed": bool(oracle_bound),
            "detail": f"oracle_mae={float(oracle['abs_error'].mean()):.4f}",
        }
    )
    return checks


def verify_no_target_leakage(
    data: pd.DataFrame,
    *,
    n_folds: int = 5,
    min_train: int = 200,
    test_size: int = 60,
) -> list[dict[str, Any]]:
    """Scramble every outcome column and confirm real-policy decisions hold.

    Two guarantees are exercised:

    1. test-outcome independence: scrambling the realized outcomes of the test
       rows must not change any real policy decision;
    2. oracle isolation: only the oracle may read outcomes, and it is flagged.
    """

    folds = make_folds(data, n_folds=n_folds, min_train=min_train, test_size=test_size)
    checks: list[dict[str, Any]] = []
    scale = float(np.abs(data["actual_price_24h"]).mean())

    def scramble(frame: pd.DataFrame) -> pd.DataFrame:
        scrambled = frame.copy()
        scrambled["actual_price_24h"] = scrambled["actual_price_24h"] * 3.0 + scale
        scrambled["abs_error_timestral"] = np.abs(
            scrambled["timestral_price_24h"] - scrambled["actual_price_24h"]
        )
        scrambled["abs_error_naive"] = np.abs(
            scrambled["naive_price_24h"] - scrambled["actual_price_24h"]
        )
        scrambled["delta_mae"] = scrambled["abs_error_timestral"] - scrambled["abs_error_naive"]
        return scrambled

    for policy_name in ("always_naive", "always_timestral", "regime_rules", "logistic"):
        changed = 0
        for fold in folds:
            train = data.loc[fold["train_index"]]
            test = data.loc[fold["test_index"]]
            implementation = POLICY_IMPLEMENTATIONS[policy_name]()
            state = implementation.fit(train)
            changed += int(
                (
                    implementation.decide(test, state).to_numpy()
                    != implementation.decide(scramble(test), state).to_numpy()
                ).sum()
            )
        checks.append(
            {
                "check": f"{policy_name} decisions ignore test outcomes",
                "passed": changed == 0,
                "detail": f"decision changes={changed}",
            }
        )
    # The oracle must depend on outcomes: verify it reacts, proving the flag is honest.
    fold = folds[0]
    train = data.loc[fold["train_index"]]
    test = data.loc[fold["test_index"]]
    oracle = OraclePolicy()
    oracle_state = oracle.fit(train)
    reacted = int(
        (
            oracle.decide(test, oracle_state).to_numpy()
            != oracle.decide(scramble(test), oracle_state).to_numpy()
        ).sum()
    )
    checks.append(
        {
            "check": "oracle reads outcomes and is flagged uses_target=true",
            "passed": reacted > 0 and POLICY_CATALOG["oracle"].uses_target,
            "detail": f"decision changes={reacted}, uses_target={POLICY_CATALOG['oracle'].uses_target}",
        }
    )
    return checks


def _source_hashes(paths: PolicyPaths) -> dict[str, str]:
    from .sync import digest

    files = {
        "regime_features": paths.regime_dataset_dir / "features.parquet",
        "regime_errors": paths.regime_dataset_dir / "errors.parquet",
        "regime_manifest": paths.regime_dataset_dir / "manifest.json",
        "regime_summary": paths.regime_dataset_dir / "summary.json",
    }
    return {name: digest(path) for name, path in files.items() if path.is_file()}


def run(
    paths: PolicyPaths = PolicyPaths(),
    *,
    write: bool = True,
    n_folds: int = 5,
    min_train: int = 200,
    test_size: int = 60,
) -> dict[str, Any]:
    data = load_regime_dataset(paths.regime_dataset_dir)
    result = evaluate_policies(data, n_folds=n_folds, min_train=min_train, test_size=test_size)
    leakage = verify_no_target_leakage(data, n_folds=n_folds, min_train=min_train, test_size=test_size)
    result["leakage_checks"] = result["leakage_checks"] + leakage
    if not all(check["passed"] for check in result["leakage_checks"]):
        failed = [check for check in result["leakage_checks"] if not check["passed"]]
        raise PolicyError(f"Leakage checks failed: {failed}")
    result["schema_version"] = POLICY_SCHEMA
    result["windows"] = int(len(data))
    result["generated_utc"] = _now()
    if write:
        _write_experiment(paths, data, result)
    return result


def _write_experiment(paths: PolicyPaths, data: pd.DataFrame, result: dict[str, Any]) -> None:
    output = paths.output_dir
    if output.exists():
        raise FileExistsError(f"Policy experiment already exists; refusing to overwrite: {output}")
    before = _source_hashes(paths)
    output.mkdir(parents=True)
    try:
        decisions = result.pop("decisions")
        decisions.to_parquet(output / "decisions.parquet", index=False)
        result["decisions_sha256"] = digest_or_none(output / "decisions.parquet")
        fold_payload = {
            "folds": result["folds"],
            "fold_summaries": result["fold_summaries"],
            "fit_states": result["fit_states"],
        }
        (output / "fold_summary.json").write_text(
            json.dumps(fold_payload, indent=2, default=str) + "\n", encoding="utf-8"
        )
        result["fold_summary_sha256"] = digest_or_none(output / "fold_summary.json")
        summary = {key: value for key, value in result.items() if key not in {"folds", "fold_summaries", "fit_states"}}
        (output / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
        after = _source_hashes(paths)
        manifest = {
            "schema_version": POLICY_SCHEMA,
            "created_utc": _now(),
            "source_hashes": before,
            "source_hashes_after": after,
            "source_hashes_unchanged": before == after,
            "original_artifacts_modified": before != after,
            "outputs": {
                "decisions": {"path": "decisions.parquet", "sha256": result["decisions_sha256"]},
                "fold_summary": {"path": "fold_summary.json", "sha256": result["fold_summary_sha256"]},
                "summary": {"path": "summary.json", "sha256": digest_or_none(output / "summary.json")},
            },
            "policy_catalog": {
                name: {
                    "label": spec.label,
                    "uses_target": spec.uses_target,
                    "trainable": spec.trainable,
                    "description": spec.description,
                }
                for name, spec in POLICY_CATALOG.items()
            },
            "validation": {
                "folds": "chronological expanding-window, no shuffle",
                "purge_hours": PURGE_HOURS,
                "hyperparameter_selection": "inner chronological split of the training fold",
                "confidence_intervals": f"moving-block bootstrap (block={BOOTSTRAP_BLOCKS}, draws={BOOTSTRAP_DRAWS}, seed={BOOTSTRAP_SEED})",
            },
        }
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8")
        if before != after:
            raise PolicyError("Source artifacts changed while writing the policy experiment")
    except BaseException:
        import shutil

        shutil.rmtree(output, ignore_errors=True)
        raise


def digest_or_none(path: Path) -> str | None:
    from .sync import digest

    return digest(path) if path.is_file() else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S02-CP06B selective policy evaluation")
    parser.add_argument("--regime-dataset-dir", default=str(PolicyPaths.regime_dataset_dir))
    parser.add_argument("--output-dir", default=str(PolicyPaths.output_dir))
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--min-train", type=int, default=200)
    parser.add_argument("--test-size", type=int, default=60)
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)
    paths = PolicyPaths(Path(args.regime_dataset_dir), Path(args.output_dir))
    result = run(
        paths,
        write=not args.no_write,
        n_folds=args.folds,
        min_train=args.min_train,
        test_size=args.test_size,
    )
    payload = {
        "windows": result["windows"],
        "leakage_checks": result["leakage_checks"],
        "aggregate": {
            name: {
                "out_of_sample": values["out_of_sample"],
                "uses_target": values["uses_target"],
            }
            for name, values in result["aggregate"].items()
        },
    }
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
