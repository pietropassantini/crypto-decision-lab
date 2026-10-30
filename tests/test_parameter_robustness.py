"""Tests for S02-CP08-03 parameter & execution robustness."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab.decision_contract import (
    ACTION_LONG,
    ACTION_NO_TRADE,
    ACTION_SHORT,
    CostModel,
    net_return,
)
from crypto_decision_lab.parameter_robustness import (
    BOOTSTRAP_BLOCK,
    COST_SCENARIOS,
    FROZEN_FEE,
    FROZEN_SLIPPAGE,
    FROZEN_THRESHOLD,
    LATENCY_HOURS,
    PRIMARY_LATENCY_HOURS,
    PREREGISTERED_THRESHOLD_GRID,
    REQUIRED_GRID_VALUES,
    RobustnessPaths,
    base_cost_scenario,
    build_robustness,
    carry_fraction,
    concentration_detail,
    cost_model,
    evaluate_frame,
    frozen_reference,
    grid_frame,
    select_threshold,
    threshold_selection_probe,
    threshold_surface,
    transform_frozen_frame,
    vectorized_net_pct,
    write_artifacts,
)
from crypto_decision_lab.policy_evaluation import make_folds
from crypto_decision_lab.simulator_integrity_audit import AuditPaths, load_archive

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# synthetic inputs
# ---------------------------------------------------------------------------


def _synthetic_labels(n: int = 500, *, interval_hours: int = 24) -> pd.DataFrame:
    """Cutoff-only rows with real-looking open prices and a momentum signal."""

    index = np.arange(n)
    hours = pd.date_range("2024-01-01", periods=n * interval_hours + 48, freq="h", tz="UTC")
    steps = np.arange(len(hours), dtype=float)
    price = 100.0 * np.exp(0.00008 * steps + 0.05 * np.sin(steps / 13.0))
    cutoffs = hours[index * interval_hours]
    entry = price[index * interval_hours]
    exit_ = price[index * interval_hours + 24]
    momentum = 0.16 * np.sin(index / 6.0)
    return pd.DataFrame(
        {
            "window_index": index,
            "cutoff": cutoffs,
            "entry_timestamp": cutoffs,
            "exit_timestamp": cutoffs + pd.Timedelta(hours=24),
            "reference_price": price[index * interval_hours - 1],
            "entry_open": entry,
            "exit_open": exit_,
            "momentum_168h": momentum,
            "timestral_return_24h": momentum / 2.0,
            "volatility_168h": 0.01 + 0.001 * (index % 7),
            "best_action": np.where(momentum > 0, ACTION_LONG, ACTION_SHORT),
            "best_net_pct": np.abs(momentum) * 10.0,
            "long_net_pct": momentum * 10.0,
            "short_net_pct": -momentum * 10.0,
            "long_gross_pct": momentum * 10.0,
            "short_gross_pct": -momentum * 10.0,
        }
    )


def _market_from_labels(labels: pd.DataFrame) -> pd.DataFrame:
    start = pd.Timestamp(labels["entry_timestamp"].min())
    end = pd.Timestamp(labels["exit_timestamp"].max()) + pd.Timedelta(hours=3)
    index = pd.date_range(start, end, freq="h", tz="UTC")
    steps = np.arange(len(index), dtype=float)
    close = 100.0 * np.exp(0.00008 * steps + 0.05 * np.sin(steps / 13.0))
    open_ = np.concatenate([[close[0]], close[:-1]])
    return pd.DataFrame({"open": open_, "close": close}, index=index)


def _actions(labels: pd.DataFrame) -> np.ndarray:
    return np.where(
        labels["momentum_168h"].to_numpy(dtype=float) > 0.05,
        ACTION_LONG,
        np.where(
            labels["momentum_168h"].to_numpy(dtype=float) < -0.05,
            ACTION_SHORT,
            ACTION_NO_TRADE,
        ),
    )


def _base_frame(labels: pd.DataFrame, market: pd.DataFrame, *, fold: int = 0) -> pd.DataFrame:
    frame, _ = grid_frame(
        labels,
        _actions(labels),
        costs=cost_model(FROZEN_FEE, FROZEN_SLIPPAGE),
        fold=fold,
        market=market,
    )
    return frame


# ---------------------------------------------------------------------------
# pre-registration
# ---------------------------------------------------------------------------


def test_preregistered_grid_contains_required_thresholds():
    grid = list(PREREGISTERED_THRESHOLD_GRID)
    assert grid == sorted(set(grid))
    for required in REQUIRED_GRID_VALUES:
        assert any(abs(value - required) < 1e-12 for value in grid)
    assert FROZEN_THRESHOLD in grid
    assert len(grid) >= 10


def test_cost_scenarios_are_ordered_and_include_the_cp07_base():
    totals = [item["fee_rate"] + item["slippage"] for item in COST_SCENARIOS]
    assert totals == sorted(totals)
    base = base_cost_scenario()
    assert base["fee_rate"] == pytest.approx(FROZEN_FEE)
    assert base["slippage"] == pytest.approx(FROZEN_SLIPPAGE)
    assert base["fee_rate"] + base["slippage"] == pytest.approx(0.0015)
    assert len({item["id"] for item in COST_SCENARIOS}) == len(COST_SCENARIOS)


def test_latency_scenarios_include_the_primary_one_hour():
    assert LATENCY_HOURS == (0, 1, 2)
    assert PRIMARY_LATENCY_HOURS == 1


# ---------------------------------------------------------------------------
# audited formula equivalence
# ---------------------------------------------------------------------------


def test_vectorized_net_matches_audited_net_return():
    labels = _synthetic_labels(40)
    actions = _actions(labels)
    costs = cost_model(FROZEN_FEE, FROZEN_SLIPPAGE)
    vector = vectorized_net_pct(
        labels["entry_open"].to_numpy(dtype=float),
        labels["exit_open"].to_numpy(dtype=float),
        actions,
        costs,
    )
    audited = np.array(
        [
            100.0 * net_return(entry, exit_, action, costs)
            if action != ACTION_NO_TRADE
            else 0.0
            for entry, exit_, action in zip(
                labels["entry_open"].to_numpy(dtype=float),
                labels["exit_open"].to_numpy(dtype=float),
                actions,
            )
        ]
    )
    np.testing.assert_allclose(vector, audited, rtol=0.0, atol=1e-12)


# ---------------------------------------------------------------------------
# threshold selection
# ---------------------------------------------------------------------------


def _selection_frame() -> pd.DataFrame:
    """Rows where only |momentum| = 16% is profitable, at both directions."""

    rows = []
    for position in range(24):
        high = position % 2 == 0
        direction = ACTION_LONG if position % 4 == 0 else ACTION_SHORT
        # Winners sit at |momentum| = 16%, losers at 9%: thresholds 10-15% trade
        # only the winners, thresholds <= 9% also trade the losers.
        momentum = (0.16 if high else 0.095) * (1 if direction == ACTION_LONG else -1)
        entry = 100.0
        exit_ = 110.0 if high else 95.0
        if direction == ACTION_SHORT:
            exit_ = 90.0 if high else 105.0
        rows.append(
            {
                "window_index": position,
                "cutoff": pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(hours=24 * position),
                "entry_timestamp": pd.Timestamp("2024-01-01", tz="UTC")
                + pd.Timedelta(hours=24 * position),
                "exit_timestamp": pd.Timestamp("2024-01-01", tz="UTC")
                + pd.Timedelta(hours=24 * position + 24),
                "reference_price": 100.0,
                "entry_open": entry,
                "exit_open": exit_,
                "momentum_168h": momentum,
                "timestral_return_24h": momentum,
            }
        )
    return pd.DataFrame(rows)


def test_threshold_selection_picks_the_train_argmax_with_lowest_tie_break():
    train = _selection_frame()
    state = select_threshold(train, costs=cost_model(0.0, 0.0))
    # 10%, 12% and 15% all trade only the profitable rows: the lowest wins the tie.
    assert state["threshold"] == pytest.approx(0.10)
    assert state["train_trades"] == 12
    assert state["train_multiple"] > 1.0
    assert set(state["train_multiples"]) == {
        str(float(value)) for value in PREREGISTERED_THRESHOLD_GRID
    }


def test_threshold_selection_uses_training_rows_only():
    labels = _synthetic_labels(500)
    folds = make_folds(labels, n_folds=5, min_train=200, test_size=60)
    probe = threshold_selection_probe(
        labels, folds, costs=cost_model(FROZEN_FEE, FROZEN_SLIPPAGE)
    )
    assert probe["all_identical"] is True
    assert all(row["train_test_overlap"] == 0 for row in probe["folds"])
    assert all(row["identical"] for row in probe["folds"])


def test_folds_are_chronological_and_purged():
    labels = _synthetic_labels(500)
    folds = make_folds(labels, n_folds=5, min_train=200, test_size=60)
    for fold in folds:
        train = labels.loc[fold["train_index"]]
        test = labels.loc[fold["test_index"]]
        assert train["cutoff"].max() < test["cutoff"].min()
        assert fold["train_size"] + fold["purged_windows"] == 200 + 60 * fold["fold"]


# ---------------------------------------------------------------------------
# cost sensitivity on a frozen ledger
# ---------------------------------------------------------------------------


def test_cost_sensitivity_is_monotone_on_the_same_ledger():
    labels = _synthetic_labels(120)
    market = _market_from_labels(labels)
    frame = _base_frame(labels, market)
    returns = []
    for item in COST_SCENARIOS:
        transformed, diagnostics = transform_frozen_frame(
            frame, costs=cost_model(item["fee_rate"], item["slippage"]), market=market
        )
        assert diagnostics["missing_bars"] == 0
        metrics = evaluate_frame(
            transformed,
            market=market,
            bootstrap=False,
            with_concentration=False,
            with_curves=False,
        )
        assert metrics["trades"] == int((frame["action"] != ACTION_NO_TRADE).sum())
        returns.append(metrics["total_return_pct"])
    assert all(returns[index + 1] < returns[index] for index in range(len(returns) - 1))
    base_index = next(
        index for index, item in enumerate(COST_SCENARIOS) if item["id"] == "cost_0015_base"
    )
    assert returns[base_index] == pytest.approx(
        evaluate_frame(
            frame,
            market=market,
            bootstrap=False,
            with_concentration=False,
            with_curves=False,
        )["total_return_pct"]
    )


def test_cost_transform_keeps_the_same_actions_and_timestamps():
    labels = _synthetic_labels(80)
    market = _market_from_labels(labels)
    frame = _base_frame(labels, market)
    transformed, _ = transform_frozen_frame(
        frame, costs=cost_model(0.003, 0.001), market=market
    )
    assert transformed["action"].tolist() == frame["action"].tolist()
    assert transformed["entry_timestamp"].tolist() == frame["entry_timestamp"].tolist()
    traded = transformed[transformed["action"] != ACTION_NO_TRADE]
    assert np.allclose(traded["fee_rate"].to_numpy(dtype=float), 0.003)
    assert np.allclose(traded["slippage"].to_numpy(dtype=float), 0.001)
    assert bool((traded["net_return_pct"] < frame.loc[traded.index, "net_return_pct"]).all())


# ---------------------------------------------------------------------------
# latency
# ---------------------------------------------------------------------------


def test_latency_shifts_entry_and_exit_on_real_opens():
    labels = _synthetic_labels(60)
    market = _market_from_labels(labels)
    frame = _base_frame(labels, market)
    shifted, diagnostics = transform_frozen_frame(
        frame, costs=cost_model(FROZEN_FEE, FROZEN_SLIPPAGE), market=market, delay_hours=1
    )
    assert diagnostics["missing_bars"] == 0
    assert (shifted["entry_timestamp"] - frame["entry_timestamp"] == pd.Timedelta(hours=1)).all()
    assert (shifted["exit_timestamp"] - frame["exit_timestamp"] == pd.Timedelta(hours=1)).all()
    costs = cost_model(FROZEN_FEE, FROZEN_SLIPPAGE)
    for row in shifted[shifted["action"] != ACTION_NO_TRADE].itertuples(index=False):
        entry_price = float(market["open"].loc[row.entry_timestamp])
        exit_price = float(market["open"].loc[row.exit_timestamp])
        expected = 100.0 * net_return(entry_price, exit_price, row.action, costs)
        assert row.net_return_pct == pytest.approx(expected, abs=1e-12)


def test_latency_reports_missing_bars_instead_of_inventing_prices():
    labels = _synthetic_labels(40)
    market = _market_from_labels(labels)
    frame = _base_frame(labels, market)
    missing_timestamp = pd.Timestamp(frame["entry_timestamp"].iloc[2]) + pd.Timedelta(hours=1)
    reduced = market.drop(index=missing_timestamp)
    shifted, diagnostics = transform_frozen_frame(
        frame, costs=cost_model(FROZEN_FEE, FROZEN_SLIPPAGE), market=reduced, delay_hours=1
    )
    assert diagnostics["missing_bars"] >= 1
    assert len(shifted) == len(frame) - diagnostics["missing_bars"]


# ---------------------------------------------------------------------------
# SHORT carry hypotheses
# ---------------------------------------------------------------------------


def test_carry_fraction_matches_the_declared_hypothesis_formula():
    # 0.01% every 8h -> 3 payments in 24h, plus 0.02% borrow per day.
    assert carry_fraction(0.0001, 0.0002) == pytest.approx(0.0005)
    assert carry_fraction(0.0003, 0.0005) == pytest.approx(0.0014)
    assert carry_fraction(-0.0001, 0.0) == pytest.approx(-0.0003)


def test_carry_touches_short_trades_only_and_is_multiplicative():
    labels = _synthetic_labels(80)
    market = _market_from_labels(labels)
    frame = _base_frame(labels, market)
    carry = 0.002
    carried, _ = transform_frozen_frame(
        frame,
        costs=cost_model(FROZEN_FEE, FROZEN_SLIPPAGE),
        market=market,
        carry_short=carry,
    )
    merged = frame.merge(
        carried[["window_index", "action", "net_return_pct"]],
        on=["window_index", "action"],
        suffixes=("_base", "_carry"),
    )
    assert len(merged) == len(frame)
    for row in merged.itertuples(index=False):
        if row.action == ACTION_LONG:
            assert row.net_return_pct_carry == pytest.approx(row.net_return_pct_base, abs=1e-12)
        elif row.action == ACTION_SHORT:
            expected = 100.0 * (
                (1.0 + row.net_return_pct_base / 100.0) * (1.0 - carry) - 1.0
            )
            assert row.net_return_pct_carry == pytest.approx(expected, abs=1e-12)
        else:
            assert row.net_return_pct_carry == 0.0


# ---------------------------------------------------------------------------
# metrics and concentration
# ---------------------------------------------------------------------------


def test_evaluate_frame_reports_every_required_metric():
    labels = _synthetic_labels(120)
    market = _market_from_labels(labels)
    frame = _base_frame(labels, market)
    metrics = evaluate_frame(frame, market=market)
    for key in (
        "trades",
        "windows",
        "total_return_pct",
        "profit_factor_usdt",
        "max_drawdown_exit_pct",
        "max_drawdown_mtm_pct",
        "win_rate_trade_pct",
        "ci",
    ):
        assert key in metrics
    assert metrics["max_drawdown_exit_pct"] <= 0.0
    assert metrics["max_drawdown_mtm_pct"] <= 0.0
    ci = metrics["ci"]
    assert ci["block"] == BOOTSTRAP_BLOCK
    assert ci["total_return_pct"]["ci_low"] <= ci["total_return_pct"]["point"]
    assert ci["total_return_pct"]["ci_high"] >= ci["total_return_pct"]["point"]
    assert 0.0 <= ci["total_return_pct"]["p_positive"] <= 1.0


def test_concentration_leave_top_k_is_monotone_and_additive():
    ledger = pd.DataFrame(
        {
            "executed": [True, True, True, True],
            "action": [ACTION_LONG, ACTION_LONG, ACTION_SHORT, ACTION_LONG],
            "net_return_pct": [10.0, -5.0, 2.0, -1.0],
            "equity_after": [1100.0, 1045.0, 1065.9, 1055.24],
        }
    )
    detail = concentration_detail(ledger, initial_capital=1000.0)
    recompounded = detail["leave_top_k_recompounded_pct"]
    assert recompounded["1"] > recompounded["3"]
    assert detail["top_k"]["1"]["share_of_net_pnl_pct"] > 0
    assert sum(value["pnl_usdt"] for value in detail["top_k"].values()) > 0
    assert detail["net_pnl_usdt"] == pytest.approx(55.241, abs=1e-9)


# ---------------------------------------------------------------------------
# exploratory surface
# ---------------------------------------------------------------------------


def test_threshold_surface_is_exploratory_and_scoped():
    labels = _synthetic_labels(500)
    folds = make_folds(labels, n_folds=5, min_train=200, test_size=60)
    market = _market_from_labels(labels)
    grid = (0.0, 0.05, 0.08, 0.10, 0.20)
    surface = threshold_surface(
        data=labels,
        folds=folds,
        market=market,
        initial_capital=10_000.0,
        grid=grid,
    )
    assert set(surface["scope"]) == {"train", "test_fold", "test_all"}
    assert surface[surface["scope"] == "train"]["exploratory"].eq(False).all()
    assert surface[surface["scope"] != "train"]["exploratory"].eq(True).all()
    assert len(surface[surface["scope"] == "test_all"]) == len(grid)
    assert len(surface[surface["scope"] == "test_fold"]) == len(grid) * 5
    assert "promoted" not in surface.columns
    frozen = surface[
        (surface["scope"] == "test_all") & (surface["threshold"] == 0.05)
    ]
    assert len(frozen) == 1


# ---------------------------------------------------------------------------
# real archive regression
# ---------------------------------------------------------------------------


def _real_paths() -> RobustnessPaths:
    paths = RobustnessPaths()
    return RobustnessPaths(
        data_root=ROOT / paths.data_root,
        snapshot_dir=ROOT / paths.snapshot_dir,
        label_dir=ROOT / paths.label_dir,
        regime_dir=ROOT / paths.regime_dir,
        trading_dir=ROOT / paths.trading_dir,
        audit_dir=ROOT / paths.audit_dir,
        stability_dir=ROOT / paths.stability_dir,
        output_dir=ROOT / paths.output_dir,
    )


@pytest.fixture(scope="module")
def real_report():
    paths = _real_paths()
    required = (
        paths.trading_dir / "decisions.parquet",
        paths.label_dir / "labels.parquet",
        paths.regime_dir / "features.parquet",
        paths.snapshot_dir / "raw-manifest.json",
    )
    if not all(path.is_file() for path in required):
        pytest.skip("Frozen CP07/CP06B archive not available")
    return build_robustness(paths, initial_capital=10_000.0)


def test_real_frozen_policy_is_reproduced_bit_for_bit(real_report):
    frozen = real_report["frozen_reference"]
    assert frozen["trades"] == 106
    assert frozen["total_return_pct"] == pytest.approx(39.17576428398106, abs=1e-9)
    assert frozen["profit_factor_usdt"] == pytest.approx(1.3295465337690715, abs=1e-9)
    assert frozen["max_drawdown_exit_pct"] == pytest.approx(-21.180367003355915, abs=1e-9)
    assert frozen["max_drawdown_mtm_pct"] == pytest.approx(-27.202366839386947, abs=1e-9)
    assert frozen["threshold"] == pytest.approx(0.05)

    scenarios = real_report["scenario_metrics"]
    base = scenarios[scenarios["scenario"] == "cost_0015_base"].iloc[0]
    assert base["trades"] == 106
    assert base["total_return_pct"] == pytest.approx(frozen["total_return_pct"], abs=1e-9)
    assert base["profit_factor_usdt"] == pytest.approx(frozen["profit_factor_usdt"], abs=1e-9)


def test_real_latency_reproduces_cp08_01_and_one_hour_is_primary(real_report):
    scenarios = real_report["scenario_metrics"]
    latency = scenarios[scenarios["family"] == "latency"].set_index("delay_hours")
    assert latency.loc[0, "total_return_pct"] == pytest.approx(39.17576428398106, abs=1e-6)
    assert latency.loc[1, "total_return_pct"] == pytest.approx(23.590450507610083, abs=1e-6)
    assert latency.loc[2, "total_return_pct"] == pytest.approx(23.897290922673207, abs=1e-6)
    assert bool(latency.loc[1, "primary"]) is True
    assert int(latency["missing_bars"].sum()) == 0
    # Latency reduces the return but deepens the mark-to-market drawdown.
    assert latency.loc[1, "max_drawdown_mtm_pct"] < latency.loc[0, "max_drawdown_mtm_pct"]


def test_real_cost_sensitivity_is_monotone_without_refit(real_report):
    scenarios = real_report["scenario_metrics"]
    frozen = scenarios[scenarios["family"] == "cost_frozen"].sort_values("total_cost_per_side")
    returns = frozen["total_return_pct"].to_numpy(dtype=float)
    assert all(returns[index + 1] < returns[index] for index in range(len(returns) - 1))
    assert real_report["experiments"]["cost_sensitivity_frozen"]["monotonicity"]["monotone"] is True
    # All cost scenarios keep the same 106 frozen trades.
    assert frozen["trades"].nunique() == 1
    assert int(frozen["trades"].iloc[0]) == 106


def test_real_refit_sensitivity_is_a_different_non_monotone_experiment(real_report):
    scenarios = real_report["scenario_metrics"]
    refit = scenarios[scenarios["family"] == "policy_refit"].sort_values("total_cost_per_side")
    returns = refit["total_return_pct"].to_numpy(dtype=float)
    assert real_report["experiments"]["policy_refit_sensitivity"]["monotone"] is False
    assert any(returns[index + 1] > returns[index] for index in range(len(returns) - 1))
    selection = real_report["selection"]
    assert not selection.empty
    for value in selection["selected_threshold"]:
        assert any(abs(float(value) - float(candidate)) < 1e-12 for candidate in PREREGISTERED_THRESHOLD_GRID)
    # The refit at CP07 base costs is *not* the frozen policy and does not beat it OOS.
    base_refit = refit[refit["scenario"] == "refit_cost_0015_base"].iloc[0]
    frozen = scenarios[scenarios["scenario"] == "cost_0015_base"].iloc[0]
    assert int(base_refit["trades"]) != int(frozen["trades"])
    assert base_refit["total_return_pct"] < frozen["total_return_pct"]


def test_real_cp07_cost_grid_is_reproduced(real_report):
    reproduction = real_report["diagnostics"]["cp07_sensitivity_reproduction"]
    assert reproduction["matches_archive"] is True
    assert reproduction["max_abs_delta_pct"] == pytest.approx(0.0, abs=1e-9)
    assert len(reproduction["scenarios"]) == 9


def test_real_surface_contains_5_8_10_and_is_not_promoted(real_report):
    surface = real_report["surface"]
    for required in (0.05, 0.08, 0.10):
        assert (surface["threshold"] == required).any()
    oos = surface[surface["scope"] == "test_all"]
    frozen = oos[oos["threshold"] == 0.05].iloc[0]
    assert frozen["total_return_pct"] == pytest.approx(39.17576428398106, abs=1e-9)
    assert bool(frozen["is_frozen_threshold"]) is True
    assert oos["exploratory"].all()
    block = real_report["experiments"]["threshold_surface_oos"]
    assert block["promotion"] is False
    assert block["exploratory_only"] is True
    # The best OOS threshold is reported as an observation only.
    best = oos.sort_values("total_return_pct", ascending=False).iloc[0]
    assert block["best_oos_threshold_exploratory"] == pytest.approx(best["threshold"])
    assert block["best_oos_return_pct_exploratory"] == pytest.approx(best["total_return_pct"])


def test_real_carry_hypotheses_are_short_only_and_ordered(real_report):
    block = real_report["experiments"]["short_carry_hypotheses"]
    assert block["hypothesis_only"] is True
    assert block["checks"]["long_unaffected"] is True
    assert block["checks"]["return_non_increasing_in_carry"] is True
    scenarios = real_report["scenario_metrics"]
    carry = scenarios[scenarios["family"] == "short_carry"].sort_values("carry_short_pct")
    returns = carry["total_return_pct"].to_numpy(dtype=float)
    assert all(returns[index] >= returns[index + 1] - 1e-12 for index in range(len(returns) - 1))
    # Every carry scenario keeps the same trades and leaves LONG untouched.
    assert carry["trades"].nunique() == 1


def test_real_fold_stability_matches_cp08_02_and_is_additive(real_report):
    folds = real_report["fold_stability"]
    base = folds[folds["scenario"] == "cost_0015_base"].sort_values("fold")
    assert base["chained_pnl_usdt"].to_list() == pytest.approx(
        [260.8386, 2561.8393, 977.1181, -1893.7555, 2011.5359], abs=1e-3
    )
    assert base["chained_pnl_usdt"].sum() == pytest.approx(3917.5764, abs=1e-2)
    assert base["share_of_net_pnl_pct"].sum() == pytest.approx(100.0, abs=1e-6)
    # The fold with the deepest mark-to-market drawdown is the losing one.
    worst = base.loc[base["max_drawdown_mtm_pct"].idxmin()]
    assert int(worst["fold"]) == 3


def test_real_concentration_stays_extreme_in_the_primary_scenario(real_report):
    concentration = real_report["concentration"]
    frozen = concentration["frozen"]
    assert frozen["top_k"]["5"]["share_of_net_pnl_pct"] == pytest.approx(122.358, abs=1e-3)
    assert frozen["top_k"]["5"]["return_pct_after_removal_recompounded"] < 0.0
    primary = concentration["primary_1h"]
    assert primary["top_k"]["3"]["return_pct_after_removal_recompounded"] < 0.0
    assert primary["top_k"]["1"]["share_of_net_pnl_pct"] > 40.0


def test_real_checks_and_gates(real_report):
    assert real_report["technical_gate"] == "PASS"
    assert real_report["checks_failed"] == []
    assert real_report["strategic_gate"] == "INCONCLUSIVE"
    assert real_report["promotion"] == "NO-GO"
    statuses = {check["id"]: check["status"] for check in real_report["checks"]}
    assert statuses["CP08-03.1"] == "PASS"
    assert statuses["CP08-03.4"] == "PASS"
    assert statuses["CP08-03.5"] == "PASS"
    assert statuses["CP08-03.8"] == "PASS"
    assert statuses["CP08-03.11"] == "PASS"
    assert statuses["CP08-03.6"] == "WARN"


def test_archive_write_is_additive_and_refuses_overwrite(real_report, tmp_path):
    paths = _real_paths()
    output = tmp_path / "S02-CP08-parameter-robustness-v1"
    writable = RobustnessPaths(
        data_root=paths.data_root,
        snapshot_dir=paths.snapshot_dir,
        label_dir=paths.label_dir,
        regime_dir=paths.regime_dir,
        trading_dir=paths.trading_dir,
        audit_dir=paths.audit_dir,
        stability_dir=paths.stability_dir,
        output_dir=output,
    )
    before = dict(real_report["source_hashes"])
    written = write_artifacts(writable, real_report)
    manifest = written["manifest"]
    assert manifest["source_hashes_unchanged"] is True
    assert manifest["original_artifacts_modified"] is False
    assert manifest["technical_gate"] == "PASS"
    assert (output / "manifest.json").is_file()
    assert (output / "scenario_metrics.parquet").is_file()
    assert (output / "threshold_surface.parquet").is_file()
    assert (output / "fold_stability.parquet").is_file()
    after = {name: digest_value for name, digest_value in before.items()}
    assert all(manifest["source_hashes"][name] == after[name] for name in after)
    with pytest.raises(FileExistsError):
        write_artifacts(writable, real_report)
