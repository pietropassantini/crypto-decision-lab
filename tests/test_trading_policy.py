from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab.decision_contract import (
    ACTION_LONG,
    ACTION_NO_TRADE,
    ACTION_SHORT,
    CostModel,
)
from crypto_decision_lab.trading_policy import (
    POLICY_CATALOG,
    TradingPaths,
    _threshold_actions,
    build_verdict,
    fit_threshold_policy,
    load_inputs,
    paired_comparisons,
    policy_actions,
    run,
)


def _synthetic(rows: int = 120) -> dict[str, pd.DataFrame]:
    cutoffs = pd.date_range("2023-01-01", periods=rows, freq="24h", tz="UTC")
    idx = np.arange(rows)
    # Alternating deterministic regimes/returns, no randomness.
    momentum = np.where(idx % 3 == 0, 0.08, np.where(idx % 3 == 1, -0.08, 0.0))
    signal_pct = np.where(idx % 2 == 0, 1.0, -1.0)
    entry = 100.0 + 0.1 * (idx % 5)
    move = np.where(idx % 2 == 0, 0.02, -0.02)
    exit_ = entry * (1.0 + move)
    long_net = exit_ / entry - 1.0
    short_net = entry / exit_ - 1.0
    best = np.where(long_net >= np.maximum(short_net, 0.0), ACTION_LONG, np.where(short_net > np.maximum(long_net, 0.0), ACTION_SHORT, ACTION_NO_TRADE))
    labels = pd.DataFrame(
        {
            "window_index": idx,
            "cutoff": cutoffs,
            "entry_timestamp": cutoffs,
            "exit_timestamp": cutoffs + pd.Timedelta(hours=24),
            "reference_price": entry / (1.0 + 0.0005),
            "entry_open": entry,
            "exit_open": exit_,
            "long_net_pct": 100.0 * long_net,
            "short_net_pct": 100.0 * short_net,
            "no_trade_net_pct": 0.0,
            "best_action": best,
            "best_net_pct": np.maximum.reduce([100.0 * long_net, 100.0 * short_net, np.zeros(rows)]),
            "margin_vs_second_pct": np.ones(rows),
            "ambiguous": np.zeros(rows, dtype=bool),
        }
    )
    features = pd.DataFrame(
        {
            "window_index": idx,
            "cutoff": cutoffs,
            "momentum_168h": momentum,
            "volatility_168h": np.full(rows, 0.004),
        }
    )
    errors = pd.DataFrame(
        {
            "window_index": idx,
            "cutoff": cutoffs,
            "timestral_return_pct_24h": signal_pct,
        }
    )
    return {"labels": labels, "features": features, "errors": errors}


def _write_inputs(tmp_path, data: dict[str, pd.DataFrame]) -> TradingPaths:
    label_dir = tmp_path / "labels"
    regime_dir = tmp_path / "regime"
    label_dir.mkdir()
    regime_dir.mkdir()
    data["labels"].to_parquet(label_dir / "labels.parquet", index=False)
    data["features"].to_parquet(regime_dir / "features.parquet", index=False)
    data["errors"].to_parquet(regime_dir / "errors.parquet", index=False)
    return TradingPaths(label_dir, regime_dir, tmp_path / "out")


def test_threshold_action_mapping():
    signal = np.array([0.05, 0.01, 0.0, -0.01, -0.05])
    actions = _threshold_actions(signal, 0.02)
    assert actions.tolist() == [ACTION_LONG, ACTION_NO_TRADE, ACTION_NO_TRADE, ACTION_NO_TRADE, ACTION_SHORT]


def test_fit_threshold_uses_training_rows_only(tmp_path):
    data = _synthetic()
    paths = _write_inputs(tmp_path, data)
    loaded = load_inputs(paths.label_dataset_dir, paths.regime_dataset_dir)
    train = loaded.iloc[:60]
    state = fit_threshold_policy(train, "timestral_return_24h", costs=CostModel(fee_rate=0.0, slippage=0.0))
    perturbed = train.copy()
    perturbed["timestral_return_pct_24h"] *= -1.0
    changed = fit_threshold_policy(perturbed, "timestral_return_24h", costs=CostModel(fee_rate=0.0, slippage=0.0))
    assert state["threshold"] in state["grid"]
    # The selected threshold depends on training signals; perturbing training
    # changes the fit, while test rows cannot be part of fit_threshold_policy.
    assert state["signal"] == "timestral_return_24h"
    assert changed["signal"] == state["signal"]


def test_real_policies_ignore_retrospective_labels(tmp_path):
    data = _synthetic()
    paths = _write_inputs(tmp_path, data)
    loaded = load_inputs(paths.label_dataset_dir, paths.regime_dataset_dir)
    train = loaded.iloc[:60]
    test = loaded.iloc[60:90]
    for policy in ("always_no_trade", "always_long", "always_short", "timestral_threshold", "momentum_threshold"):
        if policy in {"timestral_threshold", "momentum_threshold"}:
            column = POLICY_CATALOG[policy]["signal"]
            state = fit_threshold_policy(train, column, costs=CostModel())
        else:
            state = {}
        baseline = policy_actions(policy, test, state)
        scrambled = test.copy()
        scrambled["best_action"] = np.roll(scrambled["best_action"].to_numpy(), 1)
        after = policy_actions(policy, scrambled, state)
        assert np.array_equal(baseline, after), policy


def test_walk_forward_run_is_deterministic_and_paired(tmp_path):
    data = _synthetic()
    paths = _write_inputs(tmp_path, data)
    first = run(paths, costs=CostModel(fee_rate=0.0, slippage=0.0), n_folds=2, min_train=40, test_size=20, write=False)
    second = run(paths, costs=CostModel(fee_rate=0.0, slippage=0.0), n_folds=2, min_train=40, test_size=20, write=False)
    for policy in POLICY_CATALOG:
        a = first["aggregate"][policy]["out_of_sample"]
        b = second["aggregate"][policy]["out_of_sample"]
        assert a["total_return_pct"] == pytest.approx(b["total_return_pct"])
        assert a["trades"] == b["trades"]
    counts = first["decisions"].groupby("policy")["window_index"].nunique()
    assert set(counts.unique()) == {40}
    assert all(check["passed"] for check in first["leakage_checks"])


def test_oracle_is_upper_bound_in_synthetic_run(tmp_path):
    data = _synthetic()
    paths = _write_inputs(tmp_path, data)
    result = run(paths, costs=CostModel(fee_rate=0.0, slippage=0.0), n_folds=2, min_train=40, test_size=20, write=False)
    oracle = result["aggregate"]["oracle_retrospective"]["out_of_sample"]["total_return_pct"]
    real = [
        payload["out_of_sample"]["total_return_pct"]
        for name, payload in result["aggregate"].items()
        if name != "oracle_retrospective"
    ]
    assert oracle >= max(real) - 1e-9
    assert result["aggregate"]["oracle_retrospective"]["uses_target"] is True


def test_paired_comparison_structure(tmp_path):
    data = _synthetic()
    paths = _write_inputs(tmp_path, data)
    result = run(paths, costs=CostModel(fee_rate=0.0, slippage=0.0), n_folds=2, min_train=40, test_size=20, write=False)
    paired = result["paired_comparisons"]
    for policy in ("timestral_threshold", "momentum_threshold"):
        assert set(paired[policy]) == {"always_no_trade", "always_long", "always_short"}
        for baseline in paired[policy].values():
            assert baseline["paired_windows"] == 40
            assert baseline["ci_low"] <= baseline["ci_high"]


def test_verdict_no_go_when_nothing_beats_baselines():
    aggregate = {
        "always_naive": {"uses_target": False, "out_of_sample": {"trades": 0}},
        "always_long": {"uses_target": False, "out_of_sample": {"trades": 10}},
        "always_short": {"uses_target": False, "out_of_sample": {"trades": 10}},
        "timestral_threshold": {"uses_target": False, "out_of_sample": {"trades": 10}},
        "momentum_threshold": {"uses_target": False, "out_of_sample": {"trades": 10}},
    }
    paired = {
        "timestral_threshold": {
            "always_no_trade": {"beats_baseline": False},
            "always_long": {"beats_baseline": False},
            "always_short": {"beats_baseline": False},
        },
        "momentum_threshold": {
            "always_no_trade": {"beats_baseline": False},
            "always_long": {"beats_baseline": False},
            "always_short": {"beats_baseline": False},
        },
    }
    assert build_verdict(aggregate, paired)["verdict"] == "NO-GO"


def test_sensitivity_contains_fee_slippage_grid(tmp_path):
    data = _synthetic(rows=100)
    paths = _write_inputs(tmp_path, data)
    result = run(paths, costs=CostModel(fee_rate=0.001, slippage=0.0005), n_folds=2, min_train=40, test_size=20, write=False)
    scenarios = result["sensitivity"]["scenarios"]
    assert len(scenarios) == 9
    assert {(item["fee_rate"], item["slippage"]) for item in scenarios} == {
        (fee, slippage) for fee in (0.0, 0.0005, 0.001) for slippage in (0.0, 0.0005, 0.001)
    }
