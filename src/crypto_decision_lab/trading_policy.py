"""S02-CP07 STEP 4-5: baseline trading policies with walk-forward evaluation.

Policies decide LONG / SHORT / NO_TRADE per window using only information
available at cutoff:

1. always_no_trade
2. always_long
3. always_short
4. timestral_threshold  - canonical ``timestral_return_24h`` threshold policy
5. momentum_threshold   - ``momentum_168h`` threshold policy
6. oracle_retrospective - picks the retrospective best action (upper bound,
   reads future outcomes, flagged ``uses_target=True``)

Thresholds are selected exclusively on the training rows of each fold by
maximizing simulated net return.  The walk-forward structure reuses the CP06B
fold maker (chronological, expanding window, 24h purge equal to the holding
period, so no training exit can overlap a test entry).
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .decision_contract import (
    ACTION_LONG,
    ACTION_NO_TRADE,
    ACTION_SHORT,
    CostModel,
    DECISION_SCHEMA,
    HORIZON_HOURS,
    build_decision,
    now_iso,
)
from .policy_evaluation import make_folds
from .regime_analysis import fit_tercile_thresholds, moving_block_bootstrap_ci
from .sync import digest
from .trading_simulator import DEFAULT_INITIAL_CAPITAL, simulate_sequence

THRESHOLD_GRID = (0.0, 0.001, 0.002, 0.005, 0.01, 0.02, 0.05)
SENSITIVITY_FEES = (0.0, 0.0005, 0.001)
SENSITIVITY_SLIPPAGE = (0.0, 0.0005, 0.001)
MIN_TRADES_FOR_VERDICT = 30

POLICY_CATALOG: dict[str, dict[str, Any]] = {
    "always_no_trade": {"label": "Always NO_TRADE", "uses_target": False, "trainable": False},
    "always_long": {"label": "Always LONG", "uses_target": False, "trainable": False},
    "always_short": {"label": "Always SHORT", "uses_target": False, "trainable": False},
    "timestral_threshold": {
        "label": "Timestral directional threshold",
        "uses_target": False,
        "trainable": True,
        "signal": "timestral_return_24h",
    },
    "momentum_threshold": {
        "label": "Momentum threshold",
        "uses_target": False,
        "trainable": True,
        "signal": "momentum_168h",
    },
    "oracle_retrospective": {
        "label": "Oracle retrospective",
        "uses_target": True,
        "trainable": False,
    },
}
POLICY_ORDER = tuple(POLICY_CATALOG)


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class TradingPaths:
    label_dataset_dir: Path = Path("data/datasets/S02-CP07-decision-labels-v1")
    regime_dataset_dir: Path = Path("data/datasets/S02-CP06B-regime-features-v1")
    output_dir: Path = Path("data/experiments/S02-CP07-trading-simulation-v1")


def load_inputs(label_dir: Path, regime_dir: Path) -> pd.DataFrame:
    labels = pd.read_parquet(label_dir / "labels.parquet")
    features = pd.read_parquet(regime_dir / "features.parquet")
    errors = pd.read_parquet(regime_dir / "errors.parquet")
    regime = errors.merge(
        features[["window_index", "momentum_168h", "volatility_168h"]],
        on="window_index",
        how="inner",
        validate="one_to_one",
    )
    regime = regime[["window_index", "cutoff", "timestral_return_pct_24h", "momentum_168h", "volatility_168h"]]
    merged = labels.merge(regime, on="window_index", how="inner", validate="one_to_one", suffixes=("", "_regime"))
    if len(merged) != len(labels):
        raise PolicyError("Label/regime window sets do not align")
    merged["cutoff"] = pd.to_datetime(merged["cutoff"], utc=True)
    # Canonical decimal return (0.05 = 5%) derived from the archived percent column.
    merged["timestral_return_24h"] = merged["timestral_return_pct_24h"].to_numpy(dtype=float) / 100.0
    return merged.sort_values("cutoff").reset_index(drop=True)


def _threshold_actions(signal: np.ndarray, threshold: float) -> np.ndarray:
    return np.where(
        signal > threshold,
        ACTION_LONG,
        np.where(signal < -threshold, ACTION_SHORT, ACTION_NO_TRADE),
    )


def _simulate_actions(
    frame: pd.DataFrame,
    actions: np.ndarray,
    costs: CostModel,
    *,
    policy_version: str,
    fold: int | None,
    data_version: str,
    initial_capital: float,
    thresholds: tuple[float, float] | None = None,
) -> tuple[list, dict[str, Any]]:
    decisions = []
    for position, (_, row) in enumerate(frame.iterrows()):
        regime = _trend_label(row["momentum_168h"], thresholds) if thresholds is not None else None
        decision = build_decision(
            window_index=int(row["window_index"]),
            cutoff=pd.Timestamp(row["cutoff"]),
            entry_open=float(row["entry_open"]),
            exit_open=float(row["exit_open"]),
            action=str(actions[position]),
            costs=costs,
            reference_price=float(row["reference_price"]),
            data_version=data_version,
            policy_version=policy_version,
            horizon_hours=HORIZON_HOURS,
            fold=fold,
            regime=regime,
        )
        decisions.append(decision)
    ledger, metrics = simulate_sequence(decisions, initial_capital=initial_capital)
    return decisions, {
        "trades": metrics.trades,
        "skipped_signals": metrics.skipped_signals,
        "no_trade": metrics.no_trade,
        "total_return_pct": metrics.total_return_pct,
        "max_drawdown_pct": metrics.max_drawdown_pct,
        "win_rate_pct": metrics.win_rate_pct,
        "profit_factor": metrics.profit_factor,
        "exposure_pct": metrics.exposure_pct,
        "avg_net_return_pct": metrics.avg_net_return_pct,
        "final_equity": metrics.final_equity,
    }


def _trend_label(momentum: float, thresholds: tuple[float, float]) -> str:
    low, high = thresholds
    if momentum < low:
        return "TREND_DOWN"
    if momentum > high:
        return "TREND_UP"
    return "SIDEWAYS"


def fit_threshold_policy(
    train: pd.DataFrame,
    signal_column: str,
    *,
    costs: CostModel,
    initial_capital: float = DEFAULT_INITIAL_CAPITAL,
) -> dict[str, Any]:
    """Grid search the threshold on training rows only (max total net return)."""

    signal = train[signal_column].to_numpy(dtype=float)
    best = None
    for threshold in THRESHOLD_GRID:
        actions = _threshold_actions(signal, threshold)
        net = np.array(
            [
                _net_for_action(row, action, costs)
                for action, (_, row) in zip(actions, train.iterrows())
            ]
        )
        # Equity multiple of the threshold policy on training rows.
        multiple = float(np.prod(1.0 + net / 100.0))
        candidate = {
            "threshold": float(threshold),
            "train_multiple": multiple,
            "train_trades": int((actions != ACTION_NO_TRADE).sum()),
        }
        if best is None or multiple > best["train_multiple"] + 1e-12:
            best = candidate
    return {
        "signal": signal_column,
        "threshold": best["threshold"],
        "train_multiple": best["train_multiple"],
        "train_trades": best["train_trades"],
        "grid": [float(value) for value in THRESHOLD_GRID],
    }


def _net_for_action(row: pd.Series, action: str, costs: CostModel) -> float:
    from .decision_contract import net_return

    if action == ACTION_NO_TRADE:
        return 0.0
    return 100.0 * net_return(float(row["entry_open"]), float(row["exit_open"]), action, costs)


def policy_actions(
    policy_name: str,
    test: pd.DataFrame,
    state: dict[str, Any],
) -> np.ndarray:
    if policy_name == "always_no_trade":
        return np.full(len(test), ACTION_NO_TRADE)
    if policy_name == "always_long":
        return np.full(len(test), ACTION_LONG)
    if policy_name == "always_short":
        return np.full(len(test), ACTION_SHORT)
    if policy_name == "oracle_retrospective":
        return test["best_action"].to_numpy()
    if policy_name in {"timestral_threshold", "momentum_threshold"}:
        signal_column = POLICY_CATALOG[policy_name]["signal"]
        signal = test[signal_column].to_numpy(dtype=float)
        return _threshold_actions(signal, float(state["threshold"]))
    raise PolicyError(f"Unknown policy: {policy_name}")


def evaluate_policies(
    data: pd.DataFrame,
    *,
    costs: CostModel,
    n_folds: int = 5,
    min_train: int = 200,
    test_size: int = 60,
    initial_capital: float = DEFAULT_INITIAL_CAPITAL,
) -> dict[str, Any]:
    folds = make_folds(data, n_folds=n_folds, min_train=min_train, test_size=test_size)
    data_version = "S02-CP06A-canonical/S02-CP07-labels-v1"
    decision_rows: list[pd.DataFrame] = []
    fold_metrics: list[dict[str, Any]] = []
    states: dict[str, dict[str, Any]] = {}
    regime_thresholds: dict[int, tuple[float, float]] = {}

    for fold_info in folds:
        train = data.loc[fold_info["train_index"]]
        test = data.loc[fold_info["test_index"]]
        trend_thresholds = fit_tercile_thresholds(train["momentum_168h"].to_numpy(dtype=float))
        regime_thresholds[fold_info["fold"]] = trend_thresholds
        for policy_name in POLICY_ORDER:
            if policy_name == "timestral_threshold":
                state = fit_threshold_policy(train, "timestral_return_24h", costs=costs, initial_capital=initial_capital)
            elif policy_name == "momentum_threshold":
                state = fit_threshold_policy(train, "momentum_168h", costs=costs, initial_capital=initial_capital)
            else:
                state = {}
            states[policy_name] = state
            actions = policy_actions(policy_name, test, state)
            decisions, metrics = _simulate_actions(
                test,
                actions,
                costs,
                policy_version=policy_name,
                fold=fold_info["fold"],
                data_version=data_version,
                initial_capital=initial_capital,
                thresholds=trend_thresholds,
            )
            for decision in decisions:
                decision_rows.append(
                    {
                        "fold": fold_info["fold"],
                        "policy": policy_name,
                        "decision_id": decision.decision_id,
                        "window_index": int(decision.decision_id.split("window-")[1].split(":")[0]),
                        "cutoff": decision.cutoff_timestamp,
                        "entry_timestamp": decision.entry_timestamp,
                        "exit_timestamp": decision.exit_timestamp,
                        "action": decision.action,
                        "reference_price": decision.reference_price,
                        "execution_price": decision.execution_price,
                        "exit_price": decision.exit_price,
                        "fee_rate": decision.fee_rate,
                        "slippage": decision.slippage,
                        "gross_return_pct": decision.gross_return_pct,
                        "net_return_pct": decision.net_return_pct,
                        "regime": decision.regime,
                        "uses_target": POLICY_CATALOG[policy_name]["uses_target"],
                        "threshold": state.get("threshold"),
                    }
                )
            fold_metrics.append(
                {
                    "fold": fold_info["fold"],
                    "policy": policy_name,
                    "train_size": fold_info["train_size"],
                    "test_size": fold_info["test_size"],
                    "purged_windows": fold_info["purged_windows"],
                    "state": {key: value for key, value in state.items()},
                    **metrics,
                }
            )

    decisions_frame = pd.DataFrame(decision_rows)
    skipped_by_policy: dict[str, int] = {}
    for fold_metrics_row in fold_metrics:
        skipped_by_policy[fold_metrics_row["policy"]] = (
            skipped_by_policy.get(fold_metrics_row["policy"], 0) + int(fold_metrics_row.get("skipped_signals", 0))
        )

    aggregate: dict[str, Any] = {}
    for policy_name in POLICY_ORDER:
        subset = decisions_frame[decisions_frame["policy"] == policy_name]
        executed = subset[subset["action"] != ACTION_NO_TRADE]
        net = subset["net_return_pct"].to_numpy(dtype=float)
        equity = np.cumprod(1.0 + net / 100.0)
        peak = np.maximum.accumulate(np.concatenate([[1.0], equity]))
        drawdown = np.concatenate([[1.0], equity]) / peak - 1.0
        traded_net = executed["net_return_pct"].to_numpy(dtype=float) if len(executed) else np.array([])
        wins = traded_net[traded_net > 0]
        losses = traded_net[traded_net < 0]
        ci = moving_block_bootstrap_ci(net)
        aggregate[policy_name] = {
            "label": POLICY_CATALOG[policy_name]["label"],
            "uses_target": POLICY_CATALOG[policy_name]["uses_target"],
            "out_of_sample": {
                "windows": int(len(subset)),
                "trades": int(len(executed)),
                "no_trade": int((subset["action"] == ACTION_NO_TRADE).sum()),
                "long_trades": int((subset["action"] == ACTION_LONG).sum()),
                "short_trades": int((subset["action"] == ACTION_SHORT).sum()),
                "total_return_pct": 100.0 * (float(equity[-1]) - 1.0) if len(equity) else 0.0,
                "avg_net_return_pct": float(net.mean()) if len(net) else 0.0,
                "avg_net_return_ci": ci,
                "win_rate_pct": 100.0 * float((traded_net > 0).mean()) if len(traded_net) else 0.0,
                "profit_factor": (
                    float(wins.sum() / abs(losses.sum()))
                    if len(losses) and losses.sum() != 0
                    else (float("inf") if len(wins) else None)
                ),
                "max_drawdown_pct": 100.0 * float(drawdown.min()),
                "skipped_signals": int(skipped_by_policy.get(policy_name, 0)),
            },
        }

    paired = paired_comparisons(decisions_frame)
    leakage = leakage_checks(data, decisions_frame, n_folds=n_folds, min_train=min_train, test_size=test_size)
    sensitivity = sensitivity_analysis(
        data,
        n_folds=n_folds,
        min_train=min_train,
        test_size=test_size,
        initial_capital=initial_capital,
    )
    by_year = breakdown(decisions_frame, "year")
    by_regime = breakdown(decisions_frame, "regime")
    verdict = build_verdict(aggregate, paired)
    return {
        "folds": folds,
        "regime_thresholds": {str(key): value for key, value in regime_thresholds.items()},
        "decisions": decisions_frame,
        "fold_metrics": fold_metrics,
        "aggregate": aggregate,
        "paired_comparisons": paired,
        "by_year": by_year,
        "by_regime": by_regime,
        "sensitivity": sensitivity,
        "leakage_checks": leakage,
        "verdict": verdict,
        "states_last_fold": states,
    }


def paired_comparisons(decisions_frame: pd.DataFrame) -> dict[str, Any]:
    """Paired mean net-return differences per policy vs every baseline."""

    baselines = ("always_no_trade", "always_long", "always_short")
    results: dict[str, Any] = {}
    pivot = decisions_frame.pivot_table(
        index="window_index", columns="policy", values="net_return_pct", aggfunc="last"
    )
    for policy_name in POLICY_ORDER:
        if policy_name in baselines:
            continue
        entry: dict[str, Any] = {}
        for baseline in baselines:
            if baseline == policy_name:
                continue
            both = pivot[[policy_name, baseline]].dropna()
            difference = (both[policy_name] - both[baseline]).to_numpy(dtype=float)
            ci = moving_block_bootstrap_ci(difference)
            entry[baseline] = {
                "paired_windows": int(len(both)),
                "mean_difference_pct": float(difference.mean()) if len(difference) else 0.0,
                "ci_low": ci["ci_low"],
                "ci_high": ci["ci_high"],
                "beats_baseline": bool(ci["ci_low"] > 0.0) if len(difference) else False,
            }
        results[policy_name] = entry
    return results


def breakdown(decisions_frame: pd.DataFrame, key: str) -> dict[str, Any]:
    data = decisions_frame.copy()
    if key == "year":
        data["group"] = pd.to_datetime(data["cutoff"], utc=True).dt.year.astype(int)
    elif key == "regime":
        data["group"] = data["regime"].fillna("n/a")
    else:
        raise PolicyError(f"Unknown breakdown key: {key}")
    output: dict[str, Any] = {}
    for policy_name in POLICY_ORDER:
        subset = data[data["policy"] == policy_name]
        rows = []
        for group_value, group in subset.groupby("group"):
            net = group["net_return_pct"].to_numpy(dtype=float)
            equity = float(np.prod(1.0 + net / 100.0) - 1.0)
            rows.append(
                {
                    "group": str(group_value),
                    "windows": int(len(group)),
                    "trades": int((group["action"] != ACTION_NO_TRADE).sum()),
                    "total_return_pct": 100.0 * equity,
                    "avg_net_return_pct": float(net.mean()) if len(net) else 0.0,
                    "win_rate_pct": 100.0 * float((net > 0).mean()) if len(net) else 0.0,
                }
            )
        output[policy_name] = rows
    return output


def sensitivity_analysis(
    data: pd.DataFrame,
    *,
    n_folds: int,
    min_train: int,
    test_size: int,
    initial_capital: float = DEFAULT_INITIAL_CAPITAL,
) -> dict[str, Any]:
    """Re-run the OOS simulation across a fee x slippage grid."""

    scenarios: list[dict[str, Any]] = []
    for fee in SENSITIVITY_FEES:
        for slippage in SENSITIVITY_SLIPPAGE:
            costs = CostModel(fee_rate=fee, slippage=slippage)
            folds = make_folds(data, n_folds=n_folds, min_train=min_train, test_size=test_size)
            policy_totals: dict[str, float] = {}
            for policy_name in POLICY_ORDER:
                net_values: list[float] = []
                for fold_info in folds:
                    train = data.loc[fold_info["train_index"]]
                    test = data.loc[fold_info["test_index"]]
                    if policy_name == "timestral_threshold":
                        state = fit_threshold_policy(train, "timestral_return_24h", costs=costs)
                    elif policy_name == "momentum_threshold":
                        state = fit_threshold_policy(train, "momentum_168h", costs=costs)
                    else:
                        state = {}
                    actions = policy_actions(policy_name, test, state)
                    net = [
                        _net_for_action(row, action, costs)
                        for action, (_, row) in zip(actions, test.iterrows())
                    ]
                    net_values.extend(net)
                    if policy_name not in policy_totals:
                        policy_totals[policy_name] = 0.0
                policy_totals[policy_name] = 100.0 * (float(np.prod(1.0 + np.array(net_values) / 100.0)) - 1.0)
            scenarios.append(
                {
                    "fee_rate": fee,
                    "slippage": slippage,
                    "total_return_pct": dict(policy_totals),
                }
            )
    return {"scenarios": scenarios}


def leakage_checks(
    data: pd.DataFrame,
    decisions_frame: pd.DataFrame,
    *,
    n_folds: int,
    min_train: int,
    test_size: int,
) -> list[dict[str, Any]]:
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
            "detail": "max(train cutoff) < min(test cutoff)",
        }
    )
    purge_ok = all(
        (
            data.loc[fold["train_index"], "cutoff"].max() + pd.Timedelta(hours=HORIZON_HOURS)
            <= data.loc[fold["test_index"], "cutoff"].min()
        )
        for fold in folds
    )
    checks.append(
        {
            "check": "purge equals holding period: no training exit overlaps a test entry",
            "passed": bool(purge_ok),
            "detail": f"purge={HORIZON_HOURS}h",
        }
    )
    counts = decisions_frame.groupby("policy")["window_index"].nunique()
    paired_ok = bool((counts == counts.iloc[0]).all())
    checks.append(
        {
            "check": "all policies evaluated on identical out-of-sample windows",
            "passed": paired_ok,
            "detail": f"windows per policy={counts.min()}..{counts.max()}",
        }
    )
    # Real policies must not react to scrambled retrospective labels.
    changed = 0
    fold = folds[0]
    train = data.loc[fold["train_index"]]
    test = data.loc[fold["test_index"]]
    scrambled = test.copy()
    scrambled["best_action"] = np.roll(scrambled["best_action"].to_numpy(), 1)
    for policy_name in ("always_no_trade", "always_long", "always_short", "timestral_threshold", "momentum_threshold"):
        state = (
            fit_threshold_policy(train, POLICY_CATALOG[policy_name]["signal"], costs=CostModel())
            if policy_name in {"timestral_threshold", "momentum_threshold"}
            else {}
        )
        baseline = policy_actions(policy_name, test, state)
        after = policy_actions(policy_name, scrambled, state)
        changed += int((np.asarray(baseline) != np.asarray(after)).sum())
    checks.append(
        {
            "check": "real policies ignore retrospective labels",
            "passed": changed == 0,
            "detail": f"decision changes={changed}",
        }
    )
    oracle = decisions_frame[decisions_frame["policy"] == "oracle_retrospective"]
    others = decisions_frame[decisions_frame["policy"] != "oracle_retrospective"]
    oracle_total = 100.0 * (
        float(np.prod(1.0 + oracle["net_return_pct"].to_numpy(dtype=float) / 100.0)) - 1.0
    )
    best_other = max(
        100.0 * (float(np.prod(1.0 + group["net_return_pct"].to_numpy(dtype=float) / 100.0)) - 1.0)
        for _, group in others.groupby("policy")
    )
    checks.append(
        {
            "check": "oracle is an upper bound for real policies",
            "passed": bool(oracle_total >= best_other - 1e-9),
            "detail": f"oracle={oracle_total:.4f}% best_real={best_other:.4f}%",
        }
    )
    return checks


def build_verdict(aggregate: dict[str, Any], paired: dict[str, Any]) -> dict[str, Any]:
    """GO / NO-GO / INCONCLUSIVE gate on real (non-oracle) policies."""

    baselines = ("always_no_trade", "always_long", "always_short")
    candidates: list[dict[str, Any]] = []
    for policy_name, payload in aggregate.items():
        if payload["uses_target"]:
            continue
        out = payload["out_of_sample"]
        beats = []
        for baseline in baselines:
            comparison = paired.get(policy_name, {}).get(baseline)
            if comparison is None:
                beats.append(False)
            else:
                beats.append(bool(comparison["beats_baseline"]))
        candidates.append(
            {
                "policy": policy_name,
                "trades": out["trades"],
                "beats_all_baselines": all(beats),
                "beats": dict(zip(baselines, beats)),
            }
        )
    stable = [
        candidate
        for candidate in candidates
        if candidate["beats_all_baselines"] and candidate["trades"] >= MIN_TRADES_FOR_VERDICT
    ]
    if stable:
        verdict = "GO"
        reason = "At least one real policy beats every baseline out-of-sample with CI>0 and enough trades."
    elif any(candidate["beats_all_baselines"] for candidate in candidates):
        verdict = "INCONCLUSIVE"
        reason = "A policy beats every baseline but with too few trades for a stable verdict."
    elif any(any(candidate["beats"].values()) for candidate in candidates):
        verdict = "INCONCLUSIVE"
        reason = "Some policies beat some baselines but not all; evidence is not stable."
    else:
        verdict = "NO-GO"
        reason = "No real policy beats the baselines after costs."
    return {
        "verdict": verdict,
        "reason": reason,
        "min_trades_for_verdict": MIN_TRADES_FOR_VERDICT,
        "candidates": candidates,
        "oracle_notice": "Oracle retrospective profit is a theoretical bound and never justifies training a complex model by itself.",
    }


def _source_hashes(paths: TradingPaths) -> dict[str, str]:
    files = {
        "labels": paths.label_dataset_dir / "labels.parquet",
        "labels_manifest": paths.label_dataset_dir / "manifest.json",
        "regime_features": paths.regime_dataset_dir / "features.parquet",
        "regime_errors": paths.regime_dataset_dir / "errors.parquet",
    }
    return {name: digest(path) for name, path in files.items() if path.is_file()}


def run(
    paths: TradingPaths = TradingPaths(),
    *,
    costs: CostModel | None = None,
    n_folds: int = 5,
    min_train: int = 200,
    test_size: int = 60,
    initial_capital: float = DEFAULT_INITIAL_CAPITAL,
    write: bool = True,
) -> dict[str, Any]:
    costs = costs or CostModel()
    data = load_inputs(paths.label_dataset_dir, paths.regime_dataset_dir)
    result = evaluate_policies(
        data,
        costs=costs,
        n_folds=n_folds,
        min_train=min_train,
        test_size=test_size,
        initial_capital=initial_capital,
    )
    if not all(check["passed"] for check in result["leakage_checks"]):
        failed = [check for check in result["leakage_checks"] if not check["passed"]]
        raise PolicyError(f"Leakage checks failed: {failed}")
    result["schema_version"] = "cdl.cp07.simulation.v1"
    result["decision_schema"] = DECISION_SCHEMA
    result["cost_model"] = {"fee_rate": costs.fee_rate, "slippage": costs.slippage}
    result["initial_capital"] = initial_capital
    result["windows"] = int(len(data))
    result["generated_utc"] = now_iso()
    if write:
        _write_experiment(paths, result)
    return result


def _write_experiment(paths: TradingPaths, result: dict[str, Any]) -> None:
    output = paths.output_dir
    if output.exists():
        raise FileExistsError(f"Trading experiment already exists; refusing to overwrite: {output}")
    before = _source_hashes(paths)
    output.mkdir(parents=True)
    try:
        decisions = result.pop("decisions")
        decisions.to_parquet(output / "decisions.parquet", index=False)
        result["decisions_sha256"] = digest(output / "decisions.parquet")
        fold_payload = {
            "folds": result["folds"],
            "fold_metrics": result["fold_metrics"],
            "regime_thresholds": result["regime_thresholds"],
            "states_last_fold": result["states_last_fold"],
        }
        (output / "fold_summary.json").write_text(
            json.dumps(fold_payload, indent=2, default=str) + "\n", encoding="utf-8"
        )
        sensitivity_path = output / "sensitivity.json"
        sensitivity_path.write_text(
            json.dumps(result["sensitivity"], indent=2, default=str) + "\n", encoding="utf-8"
        )
        summary = {
            key: value
            for key, value in result.items()
            if key not in {"folds", "fold_metrics", "regime_thresholds", "states_last_fold", "decisions", "sensitivity"}
        }
        (output / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
        after = _source_hashes(paths)
        manifest = {
            "schema_version": result["schema_version"],
            "created_utc": now_iso(),
            "source_hashes": before,
            "source_hashes_after": after,
            "source_hashes_unchanged": before == after,
            "original_artifacts_modified": before != after,
            "outputs": {
                "decisions": {"path": "decisions.parquet", "sha256": result["decisions_sha256"]},
                "fold_summary": {"path": "fold_summary.json", "sha256": digest(output / "fold_summary.json")},
                "sensitivity": {"path": "sensitivity.json", "sha256": digest(sensitivity_path)},
                "summary": {"path": "summary.json", "sha256": digest(output / "summary.json")},
            },
            "execution": {
                "entry": "open of the bar at cutoff (never the anchor bar)",
                "exit": f"open of the bar at cutoff + {HORIZON_HOURS}h",
                "sizing": "100% of equity per trade, no leverage, single position at a time",
                "purge_hours": HORIZON_HOURS,
            },
            "policy_catalog": POLICY_CATALOG,
        }
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8")
        if before != after:
            raise PolicyError("Source artifacts changed while writing the trading experiment")
    except BaseException:
        import shutil

        shutil.rmtree(output, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S02-CP07 offline trading simulation")
    parser.add_argument("--label-dataset-dir", default=str(TradingPaths.label_dataset_dir))
    parser.add_argument("--regime-dataset-dir", default=str(TradingPaths.regime_dataset_dir))
    parser.add_argument("--output-dir", default=str(TradingPaths.output_dir))
    parser.add_argument("--fee-rate", type=float, default=CostModel().fee_rate)
    parser.add_argument("--slippage", type=float, default=CostModel().slippage)
    parser.add_argument("--folds", type=int, default=5)
    parser.add_argument("--min-train", type=int, default=200)
    parser.add_argument("--test-size", type=int, default=60)
    parser.add_argument("--capital", type=float, default=DEFAULT_INITIAL_CAPITAL)
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)
    paths = TradingPaths(Path(args.label_dataset_dir), Path(args.regime_dataset_dir), Path(args.output_dir))
    result = run(
        paths,
        costs=CostModel(fee_rate=args.fee_rate, slippage=args.slippage),
        n_folds=args.folds,
        min_train=args.min_train,
        test_size=args.test_size,
        initial_capital=args.capital,
        write=not args.no_write,
    )
    payload = {
        "verdict": result["verdict"]["verdict"],
        "reason": result["verdict"]["reason"],
        "aggregate": {
            name: {"out_of_sample": values["out_of_sample"], "uses_target": values["uses_target"]}
            for name, values in result["aggregate"].items()
        },
        "leakage_checks": result["leakage_checks"],
    }
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
