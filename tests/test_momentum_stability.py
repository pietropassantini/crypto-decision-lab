"""Tests for S02-CP08-02 momentum stability and profit concentration."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab.decision_contract import ACTION_LONG, ACTION_NO_TRADE, ACTION_SHORT
from crypto_decision_lab.momentum_stability import (
    StabilityPaths,
    build_stability,
    concentration_summary,
    drawdown_analysis,
    drawdown_episodes,
    period_summary,
    regime_breakdown,
    run,
    streak_analysis,
    trade_attribution,
)
from crypto_decision_lab.simulator_integrity_audit import concentration_profile

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# synthetic window frames (same shape produced by build_window_frame)
# ---------------------------------------------------------------------------


def _window_frame(
    rows: list[tuple[str, float]],
    *,
    initial_capital: float = 1_000.0,
    start: str = "2024-01-01",
    fold: int = 0,
) -> pd.DataFrame:
    equity = float(initial_capital)
    records = []
    for position, (action, net) in enumerate(rows):
        cutoff = pd.Timestamp(start, tz="UTC") + pd.Timedelta(hours=24 * position)
        before = equity
        equity = equity * (1.0 + net / 100.0)
        records.append(
            {
                "window_index": 100 + position,
                "decision_id": f"CP07:window-{100 + position:04d}:momentum_threshold",
                "fold": fold,
                "cutoff": cutoff,
                "exit_timestamp": cutoff + pd.Timedelta(hours=24),
                "action": action,
                "net_return_pct": net,
                "gross_return_pct": net + 0.3 if action != ACTION_NO_TRADE else 0.0,
                "equity_before_usdt": before,
                "equity_after_usdt": equity,
                "pnl_usdt": before * net / 100.0,
                "executed": True,
                "period_year": cutoff.year,
                "period_quarter": f"{cutoff.year}Q{cutoff.quarter}",
                "period_month": cutoff.strftime("%Y-%m"),
                "momentum_168h": 0.10 if action == ACTION_LONG else -0.10 if action == ACTION_SHORT else 0.0,
                "volatility_168h": 0.01,
                "atr_pct": 2.0,
                "trend_strength_pct": 3.0,
                "dist_sma50_pct": 2.0,
                "rsi14": 55.0,
                "volume_ratio": 1.1,
                "trend_regime": "TREND_UP" if action == ACTION_LONG else "TREND_DOWN" if action == ACTION_SHORT else "SIDEWAYS",
                "vol_regime": "VOL_MEDIUM",
                "archived_regime": "TREND_UP" if action == ACTION_LONG else "TREND_DOWN" if action == ACTION_SHORT else "SIDEWAYS",
                "feature_bar": cutoff - pd.Timedelta(hours=1),
            }
        )
    return pd.DataFrame(records)


def _trades_and_periods(rows: list[tuple[str, float]], *, initial_capital: float = 1_000.0):
    frame = _window_frame(rows, initial_capital=initial_capital)
    trades = trade_attribution(frame, initial_capital=initial_capital)
    periods = period_summary(frame, initial_capital=initial_capital)
    return frame, trades, periods


# ---------------------------------------------------------------------------
# per-trade attribution
# ---------------------------------------------------------------------------


def test_trade_attribution_monetary_pnl_is_additive_and_chains_equity():
    frame, trades, _ = _trades_and_periods(
        [
            (ACTION_NO_TRADE, 0.0),
            (ACTION_LONG, 10.0),
            (ACTION_SHORT, -5.0),
            (ACTION_LONG, 2.0),
        ]
    )
    assert len(trades) == 3
    assert trades["pnl_usdt"].to_numpy() == pytest.approx([100.0, -55.0, 20.9])
    assert trades["equity_after_usdt"].to_numpy() == pytest.approx([1100.0, 1045.0, 1065.9])
    assert frame["pnl_usdt"].sum() == pytest.approx(frame["equity_after_usdt"].iloc[-1] - 1000.0)
    assert trades["cost_drag_pct"].to_numpy() == pytest.approx([0.3, 0.3, 0.3])
    assert trades["rank_by_pnl"].tolist() == [1, 3, 2]
    assert trades["pnl_share_of_net_pnl_pct"].sum() == pytest.approx(100.0)
    assert trades["direction"].tolist() == ["LONG", "SHORT", "LONG"]
    # The exit-marked drawdown path includes the initial capital as first peak.
    assert trades["drawdown_from_peak_pct"].min() == pytest.approx(-5.0, abs=1e-9)


def test_trade_attribution_rejects_flat_rows():
    frame = _window_frame([(ACTION_NO_TRADE, 0.0), (ACTION_NO_TRADE, 0.0)])
    trades = trade_attribution(frame, initial_capital=1_000.0)
    assert trades.empty


# ---------------------------------------------------------------------------
# per-period summary
# ---------------------------------------------------------------------------


def test_period_summary_granularities_are_additive_and_reconcile():
    frame = _window_frame(
        [
            (ACTION_LONG, 5.0),
            (ACTION_NO_TRADE, 0.0),
            (ACTION_SHORT, -3.0),
            (ACTION_LONG, 4.0),
        ]
    )
    periods = period_summary(frame, initial_capital=1_000.0)
    total = float(frame["pnl_usdt"].sum())
    for granularity in ("window", "fold", "month", "quarter", "year"):
        subset = periods[periods["granularity"] == granularity]
        assert not subset.empty
        assert subset["net_pnl_usdt"].sum() == pytest.approx(total)
        assert subset["cumulative_pnl_usdt"].iloc[-1] == pytest.approx(total)
    window_rows = periods[periods["granularity"] == "window"]
    assert len(window_rows) == len(frame)
    assert set(window_rows["period"]) == {f"window-{index:04d}" for index in frame["window_index"]}
    fold_row = periods[periods["granularity"] == "fold"].iloc[0]
    assert fold_row["trades"] == 3
    assert fold_row["compounded_return_pct"] == pytest.approx(
        100.0 * ((1.05 * 0.97 * 1.04) - 1.0)
    )
    assert fold_row["win_rate_trade_pct"] == pytest.approx(200.0 / 3.0)
    assert fold_row["win_rate_window_pct"] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# concentration
# ---------------------------------------------------------------------------


def _ledger_from_trades(trades: pd.DataFrame, *, initial_capital: float) -> pd.DataFrame:
    ledger = trades.assign(
        cutoff_timestamp=trades["cutoff"],
        executed=True,
    )
    return ledger


def test_concentration_top_k_is_monotone_and_matches_audited_leave_out():
    frame, trades, periods = _trades_and_periods(
        [(ACTION_LONG, 12.0), (ACTION_LONG, 1.0), (ACTION_SHORT, -4.0), (ACTION_LONG, 2.0)]
    )
    ledger = _ledger_from_trades(trades, initial_capital=1_000.0)
    concentration = concentration_summary(trades, periods, ledger, initial_capital=1_000.0)
    returns = [concentration["top_k"][str(k)]["return_pct_after_removal"] for k in (1, 3)]
    assert returns[0] > returns[1]
    reference = concentration_profile(ledger, initial_capital=1_000.0)["leave_out"]
    for key, value in reference.items():
        assert concentration["leave_top_k_recompounded_pct"][str(key)] == pytest.approx(value, abs=1e-9)
    assert 0.0 <= concentration["gini"]["positive_pnl"] <= 1.0
    assert concentration["herfindahl"]["positive_pnl"]["effective_trades"] <= len(trades)
    assert concentration["top_k"]["1"]["distinct_months"] == 1
    assert concentration["cumulative_share_curve"][-1]["share_of_net_pnl_pct"] == pytest.approx(100.0)


def test_concentration_single_winning_trade_is_maximally_concentrated():
    frame, trades, periods = _trades_and_periods(
        [(ACTION_LONG, 10.0), (ACTION_SHORT, -1.0), (ACTION_SHORT, -1.0)]
    )
    ledger = _ledger_from_trades(trades, initial_capital=1_000.0)
    concentration = concentration_summary(trades, periods, ledger, initial_capital=1_000.0)
    herfindahl = concentration["herfindahl"]["positive_pnl"]
    assert herfindahl["index"] == pytest.approx(1.0)
    assert herfindahl["effective_trades"] == pytest.approx(1.0)
    assert concentration["gini"]["positive_pnl"] == pytest.approx(0.0)
    assert concentration["top_k"]["1"]["share_of_gross_profit_pct"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# streaks
# ---------------------------------------------------------------------------


def test_streak_analysis_counts_runs_on_a_known_sequence():
    frame, trades, _ = _trades_and_periods(
        [
            (ACTION_LONG, 2.0),
            (ACTION_LONG, 1.0),
            (ACTION_SHORT, -1.0),
            (ACTION_SHORT, -2.0),
            (ACTION_SHORT, -0.5),
            (ACTION_LONG, 3.0),
            (ACTION_LONG, -1.0),
        ]
    )
    streaks = streak_analysis(trades)
    assert streaks["wins"] == 3
    assert streaks["losses"] == 4
    assert streaks["max_win_streak"] == 2
    assert streaks["max_loss_streak"] == 3
    assert streaks["runs"] == 4
    assert streaks["win_streak_distribution"] == {"1": 1, "2": 1}
    assert streaks["loss_streak_distribution"] == {"1": 1, "3": 1}
    assert streaks["worst_loss_streak_pnl_usdt"] < 0.0
    assert streaks["runs_test"]["observed_runs"] == 4


# ---------------------------------------------------------------------------
# drawdown
# ---------------------------------------------------------------------------


def test_drawdown_episodes_tracks_recovery_and_open_episode():
    index = pd.date_range("2024-01-01", periods=5, freq="h", tz="UTC")
    equity = pd.Series([100.0, 120.0, 90.0, 130.0, 125.0], index=index)
    analysis = drawdown_episodes(equity)
    assert analysis["max_drawdown_pct"] == pytest.approx(-25.0)
    assert analysis["episode_count"] == 2
    assert analysis["recovered_episodes"] == 1
    assert analysis["open_episodes"] == 1
    first = analysis["episodes"][0]
    assert first["depth_pct"] == pytest.approx(-25.0)
    assert first["recovered"] is True
    assert first["duration_hours"] == pytest.approx(2.0)
    assert first["underwater_hours"] == pytest.approx(2.0)
    assert analysis["episodes"][1]["recovered"] is False
    assert analysis["time_underwater_pct"] == pytest.approx(100.0 * 2.0 / 5.0)
    assert analysis["ulcer_index"] > 0.0


def test_drawdown_analysis_reports_both_conventions():
    frame, trades, _ = _trades_and_periods(
        [(ACTION_LONG, 10.0), (ACTION_SHORT, -20.0), (ACTION_LONG, 15.0)]
    )
    index = pd.date_range("2024-01-01", periods=72, freq="h", tz="UTC")
    step = pd.Series(1_000.0, index=index, dtype=float)
    step.iloc[24:] = trades["equity_after_usdt"].iloc[0]
    step.iloc[48:] = trades["equity_after_usdt"].iloc[1]
    step.iloc[-1] = trades["equity_after_usdt"].iloc[2]
    mark = step.copy()
    mark.iloc[30] = trades["equity_after_usdt"].iloc[0] * 0.78
    analysis = drawdown_analysis(trades, frame, step, mark, initial_capital=1_000.0)
    assert analysis["exit_marked"]["max_drawdown_pct"] == pytest.approx(-20.0, abs=1e-9)
    assert analysis["mark_to_market"]["max_drawdown_pct"] < analysis["exit_marked"]["max_drawdown_pct"]
    assert analysis["delta_mtm_minus_exit_marked_pct"] < 0.0
    assert analysis["per_fold"][0]["fold"] == 0


# ---------------------------------------------------------------------------
# regime breakdown (cutoff-only features, train-only buckets)
# ---------------------------------------------------------------------------


def test_regime_breakdown_shares_are_additive_and_flags_small_samples():
    frame = _window_frame(
        [
            (ACTION_LONG, 6.0),
            (ACTION_SHORT, -2.0),
            (ACTION_NO_TRADE, 0.0),
            (ACTION_LONG, 3.0),
        ]
    )
    frame.loc[frame["action"] == ACTION_SHORT, "vol_regime"] = "VOL_HIGH"
    breakdown = regime_breakdown(frame, initial_capital=1_000.0)
    volatility = breakdown[(breakdown["granularity"] == "volatility") & (breakdown["bucket"] != "ALL")]
    assert volatility["share_of_net_pnl_pct"].sum() == pytest.approx(100.0)
    assert set(volatility["bucket"]) == {"VOL_MEDIUM", "VOL_HIGH"}
    assert bool(volatility["low_sample"].all())
    assert np.isnan(volatility["equity_start_usdt"]).all()
    all_row = breakdown[(breakdown["granularity"] == "volatility") & (breakdown["bucket"] == "ALL")].iloc[0]
    assert all_row["trades"] == 3
    assert all_row["net_pnl_usdt"] == pytest.approx(frame["pnl_usdt"].sum())


# ---------------------------------------------------------------------------
# real archive (read-only integration)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_paths() -> StabilityPaths:
    paths = StabilityPaths()
    required = [
        ROOT / paths.trading_dir / "decisions.parquet",
        ROOT / paths.label_dir / "labels.parquet",
        ROOT / paths.regime_dir / "features.parquet",
        ROOT / paths.snapshot_dir / "raw-manifest.json",
    ]
    if not all(path.is_file() for path in required):
        pytest.skip("CP07/CP06B frozen datasets not available")
    return StabilityPaths(
        data_root=ROOT / paths.data_root,
        snapshot_dir=ROOT / paths.snapshot_dir,
        label_dir=ROOT / paths.label_dir,
        regime_dir=ROOT / paths.regime_dir,
        trading_dir=ROOT / paths.trading_dir,
        audit_dir=ROOT / paths.audit_dir,
        output_dir=ROOT / paths.output_dir,
    )


def test_real_archive_reconciles_with_cp07_and_cp08(real_paths):
    report = build_stability(real_paths, initial_capital=10_000.0)
    reconciliation = report["reconciliation"]
    diagnostics = report["diagnostics"]

    assert diagnostics["windows"] == 300
    assert diagnostics["trades"] == 106
    assert diagnostics["no_trade"] == 194
    assert diagnostics["skipped"] == 0
    assert diagnostics["feature_cutoff_guard"]["feature_bar_equals_cutoff_minus_hour"] is True
    assert diagnostics["fold_assignment_matches"] == 300
    assert diagnostics["trend_regime_matches_archive"] == 300
    assert diagnostics["threshold_values_archived"] == [0.05]
    assert abs(diagnostics["pnl_attribution_error_usdt"]) < 1e-8

    assert reconciliation["checks_total"] >= 60
    assert reconciliation["checks_failed"] == 0, reconciliation["failed_labels"]
    assert reconciliation["all_match"] is True
    assert reconciliation["max_abs_delta"] < 1e-6
    assert reconciliation["period_attribution"]["additive"] is True
    assert reconciliation["cp08_audit_hash_chain"]["match"] is True

    trades = report["trades"]
    assert len(trades) == 106
    assert (trades["direction"] == "LONG").sum() == 57
    assert (trades["direction"] == "SHORT").sum() == 49
    concentration = report["concentration"]
    assert concentration["total_return_pct"] == pytest.approx(39.17576428398106, abs=1e-6)
    assert concentration["profit_factor_usdt"] == pytest.approx(1.3295465337690715, abs=1e-9)
    assert concentration["leave_top_k_recompounded_pct"]["3"] == pytest.approx(8.696570174701934, abs=1e-6)
    assert concentration["leave_top_k_recompounded_pct"]["5"] == pytest.approx(-5.790145813970637, abs=1e-6)
    drawdown = report["drawdown"]
    assert drawdown["exit_marked"]["max_drawdown_pct"] == pytest.approx(-21.180367003355915, abs=1e-6)
    assert drawdown["mark_to_market"]["max_drawdown_pct"] == pytest.approx(-27.202366839386947, abs=1e-6)
    assert drawdown["trade_level_max_drawdown_pct"] == pytest.approx(-21.180367003355915, abs=1e-6)
    assert report["streaks"]["wins"] == 46 and report["streaks"]["losses"] == 60


def test_archive_write_is_additive_and_refuses_overwrite(real_paths, tmp_path):
    output = tmp_path / "S02-CP08-momentum-stability-v1"
    paths = StabilityPaths(
        data_root=real_paths.data_root,
        snapshot_dir=real_paths.snapshot_dir,
        label_dir=real_paths.label_dir,
        regime_dir=real_paths.regime_dir,
        trading_dir=real_paths.trading_dir,
        audit_dir=real_paths.audit_dir,
        output_dir=output,
    )
    report = run(paths, initial_capital=10_000.0, write=True)
    written = report["written"]["manifest"]
    assert written["source_hashes_unchanged"] is True
    assert written["original_artifacts_modified"] is False
    assert written["policy_frozen"]["threshold_reselected"] is False
    assert written["policy_frozen"]["training_performed"] is False
    for name in (
        "trade_attribution.parquet",
        "period_summary.parquet",
        "concentration_summary.json",
        "drawdown_analysis.json",
        "manifest.json",
    ):
        assert (output / name).is_file(), name
    with pytest.raises(FileExistsError):
        run(paths, initial_capital=10_000.0, write=True)
