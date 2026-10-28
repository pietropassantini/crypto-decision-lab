from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab.policy_evaluation import (
    DECISION_NAIVE,
    DECISION_TIMESTRAL,
    HORIZON_HOURS,
    LogisticPolicy,
    OraclePolicy,
    PolicyError,
    RegimeRulePolicy,
    AlwaysPolicy,
    evaluate_policies,
    load_regime_dataset,
    make_folds,
    verify_no_target_leakage,
)


def _dataset(rows: int = 120, *, signal: bool = True, spacing_hours: int = 24) -> pd.DataFrame:
    """Synthetic regime dataset with an optional learnable regime signal.

    When ``signal`` is true, Timestral is systematically closer in the
    TREND_UP regime and systematically worse elsewhere, so a rule policy fitted
    on training rows can recover the mapping.
    """

    cutoffs = pd.date_range("2023-01-01", periods=rows, freq=f"{spacing_hours}h", tz="UTC")
    # Exactly repeating thirds so training terciles match the generating labels.
    momentum = np.tile([-0.6, 0.0, 0.6], int(np.ceil(rows / 3.0)))[:rows]
    trend_labels = np.tile(["TREND_DOWN", "SIDEWAYS", "TREND_UP"], int(np.ceil(rows / 3.0)))[:rows]
    volatility = 0.004 + 0.0002 * np.sin(np.arange(rows) / 13.0)
    timestral_error = np.where(trend_labels == "TREND_UP", 4.0, 12.0) if signal else 8.0
    naive_error = 10.0
    generator = np.random.default_rng(11)
    signs = np.sign(generator.normal(size=rows))
    anchor = 100.0
    actual = anchor + generator.normal(scale=1.0, size=rows)
    timestral = actual + signs * timestral_error
    naive = actual + np.sign(generator.normal(size=rows)) * naive_error
    abs_t = np.abs(timestral - actual)
    abs_n = np.abs(naive - actual)
    return pd.DataFrame(
        {
            "window_index": np.arange(rows),
            "cutoff": cutoffs,
            "target": cutoffs + pd.Timedelta(hours=HORIZON_HOURS - 1),
            "anchor_price": anchor,
            "actual_price_24h": actual,
            "timestral_price_24h": timestral,
            "naive_price_24h": naive,
            "timestral_return_pct_24h": 100.0 * (timestral / anchor - 1.0),
            "abs_error_timestral": abs_t,
            "abs_error_naive": abs_n,
            "signed_error_timestral": timestral - actual,
            "signed_error_naive": naive - actual,
            "delta_mae": abs_t - abs_n,
            "winner": np.where(abs_t < abs_n, "TIMESTRAL", "NAIVE"),
            "momentum_168h": momentum,
            "volatility_168h": volatility,
            "momentum_24h": momentum / 2.0,
            "volatility_24h": volatility / 2.0,
            "rsi14": 50.0 + 10.0 * momentum,
            "atr_pct": 0.8 + 0.1 * volatility * 100.0,
            "dist_sma50_pct": 10.0 * momentum,
            "trend_strength_pct": 5.0 * momentum,
            "volume_ratio": 1.0 + 0.1 * momentum,
        }
    )


def test_make_folds_are_chronological_and_purge_overlaps():
    data = _dataset(rows=120, spacing_hours=1)  # hourly cutoffs force purged windows
    folds = make_folds(data, n_folds=2, min_train=40, test_size=20)
    assert len(folds) == 2
    first, second = folds
    assert first["train_size"] + first["purged_windows"] == 40
    assert first["purged_windows"] > 0  # 24h purge removes overlapping targets
    for fold in folds:
        train_cutoffs = data.loc[fold["train_index"], "cutoff"]
        test_cutoffs = data.loc[fold["test_index"], "cutoff"]
        assert train_cutoffs.max() < test_cutoffs.min()
        assert train_cutoffs.max() + pd.Timedelta(hours=HORIZON_HOURS) <= test_cutoffs.min()
    assert second["train_size"] >= first["train_size"]


def test_make_folds_rejects_insufficient_data():
    with pytest.raises(PolicyError, match="Not enough windows"):
        make_folds(_dataset(rows=50), n_folds=5, min_train=200, test_size=60)


def test_regime_rule_policy_learns_training_signal_only():
    data = _dataset(rows=120, signal=True)
    policy = RegimeRulePolicy(min_cell_obs=1)
    state = policy.fit(data.iloc[:60])
    test = data.iloc[60:]
    decisions = policy.decide(test, state)
    up_mask = test["momentum_168h"] > 0.3
    assert (decisions[up_mask] == DECISION_TIMESTRAL).all()
    down_mask = test["momentum_168h"] < -0.3
    assert (decisions[down_mask] == DECISION_NAIVE).all()
    explanation = policy.explain(test, state)
    assert explanation.str.startswith("cell=").any()


def test_regime_rule_policy_thresholds_come_from_training_rows():
    data = _dataset(rows=200, signal=True)
    train = data.iloc[:100]
    state = RegimeRulePolicy().fit(train)
    perturbed_train = train.copy()
    perturbed_train["momentum_168h"] = perturbed_train["momentum_168h"] * 3.0
    perturbed_state = RegimeRulePolicy().fit(perturbed_train)
    assert perturbed_state["trend_thresholds"] != state["trend_thresholds"]
    assert state["trend_thresholds"] == pytest.approx(
        tuple(np.quantile(train["momentum_168h"], [1 / 3, 2 / 3])), rel=1e-12
    )


def test_logistic_policy_selects_c_and_ignores_test_outcomes():
    data = _dataset(rows=160, signal=True)
    train, test = data.iloc[:100], data.iloc[100:]
    policy = LogisticPolicy()
    state = policy.fit(train)
    assert state["chosen_c"] in (0.01, 0.1, 1.0, 10.0)
    baseline = policy.decide(test, state)
    scrambled = test.copy()
    scrambled["actual_price_24h"] *= 4.0
    scrambled["abs_error_timestral"] = np.abs(scrambled["timestral_price_24h"] - scrambled["actual_price_24h"])
    scrambled["abs_error_naive"] = np.abs(scrambled["naive_price_24h"] - scrambled["actual_price_24h"])
    scrambled["delta_mae"] = scrambled["abs_error_timestral"] - scrambled["abs_error_naive"]
    pd.testing.assert_series_equal(baseline, policy.decide(scrambled, state))


def test_always_and_oracle_policies():
    data = _dataset(rows=40)
    always = AlwaysPolicy(DECISION_NAIVE)
    assert (always.decide(data, always.fit(data)) == DECISION_NAIVE).all()
    oracle = OraclePolicy()
    state = oracle.fit(data)
    decisions = oracle.decide(data, state)
    expected = np.where(data["abs_error_timestral"] <= data["abs_error_naive"], DECISION_TIMESTRAL, DECISION_NAIVE)
    assert (decisions.to_numpy() == expected).all()
    oracle_abs = np.where(
        decisions == DECISION_TIMESTRAL,
        data["abs_error_timestral"],
        data["abs_error_naive"],
    )
    assert oracle_abs.mean() <= data["abs_error_naive"].mean()


def test_evaluate_policies_paired_samples_and_oracle_bound():
    data = _dataset(rows=140, signal=True)
    result = evaluate_policies(data, n_folds=2, min_train=40, test_size=50)
    decisions = result["decisions"]
    counts = decisions.groupby("policy")["window_index"].nunique()
    assert set(counts.unique()) == {100}
    aggregate = result["aggregate"]
    oracle_mae = aggregate["oracle"]["out_of_sample"]["mae_usdt"]
    for name in ("always_naive", "always_timestral", "regime_rules", "logistic"):
        assert aggregate[name]["out_of_sample"]["mae_usdt"] >= oracle_mae - 1e-9
        assert aggregate[name]["uses_target"] is False
    assert aggregate["oracle"]["uses_target"] is True
    assert all(check["passed"] for check in result["leakage_checks"])
    rule_fold = [row for row in result["fold_summaries"] if row["policy"] == "regime_rules"]
    assert len(rule_fold) == 2
    assert all(row["samples"] == 50 for row in rule_fold)


def test_evaluate_policies_in_sample_is_reported_separately():
    data = _dataset(rows=140, signal=True)
    result = evaluate_policies(data, n_folds=2, min_train=40, test_size=50)
    in_sample = result["in_sample_full_period"]
    assert set(in_sample) == {"always_naive", "always_timestral", "regime_rules", "logistic", "oracle"}
    assert all("note" in payload for payload in in_sample.values())
    assert in_sample["regime_rules"]["mae_usdt"] < in_sample["always_naive"]["mae_usdt"]


def test_verify_no_target_leakage_passes_on_synthetic_data():
    data = _dataset(rows=140, signal=True)
    checks = verify_no_target_leakage(data, n_folds=2, min_train=40, test_size=50)
    assert all(check["passed"] for check in checks)
    assert any("oracle" in check["check"] for check in checks)


def test_load_regime_dataset_aligns_features_and_errors(tmp_path):
    data = _dataset(rows=30)
    features = data[
        [
            "window_index",
            "cutoff",
            "momentum_168h",
            "volatility_168h",
            "momentum_24h",
            "volatility_24h",
            "rsi14",
            "atr_pct",
            "dist_sma50_pct",
            "trend_strength_pct",
            "volume_ratio",
        ]
    ].copy()
    features["feature_bar"] = features["cutoff"] - pd.Timedelta(hours=1)
    errors = data[
        [
            "window_index",
            "cutoff",
            "target",
            "anchor_price",
            "actual_price_24h",
            "timestral_price_24h",
            "naive_price_24h",
            "timestral_return_pct_24h",
            "abs_error_timestral",
            "abs_error_naive",
            "signed_error_timestral",
            "signed_error_naive",
            "delta_mae",
            "winner",
        ]
    ].copy()
    features.to_parquet(tmp_path / "features.parquet", index=False)
    errors.to_parquet(tmp_path / "errors.parquet", index=False)
    loaded = load_regime_dataset(tmp_path)
    assert len(loaded) == 30
    assert loaded["cutoff"].is_monotonic_increasing
    with pytest.raises((PolicyError, FileNotFoundError)):
        load_regime_dataset(tmp_path / "missing")
