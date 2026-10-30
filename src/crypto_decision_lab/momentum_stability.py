"""S02-CP08-02: Momentum stability and profit concentration (read-only).

Scope
-----
Descriptive follow-up of CP08-01.  It consumes *only* frozen artifacts that
already exist in the repository:

* CP07 ``decisions.parquet`` (1.800 decisioni, 6 policy);
* CP07 ``labels.parquet`` (500 finestre, prezzi open di entry/exit);
* CP06B ``features.parquet`` (feature disponibili **al cutoff**);
* CP08-01 ``equity_curves.parquet`` / ``summary.json`` / ``metrics_comparison.csv``;
* snapshot BTCUSDT 1h (sola lettura, per il mark-to-market orario).

Nothing is trained, nothing is re-labelled, no threshold is re-selected and no
artifact is rewritten.  The module *reuses* ``simulator_integrity_audit`` for
the archive reconstruction, the replay, the monetary metrics, the calendar-time
curves and the concentration leave-out profile, so the numbers are chained to
the audited simulator instead of being recomputed with a private formula.

Conventions (unchanged from CP08-01, reported separately)
---------------------------------------------------------
* per **trade**: one executed position, net return % and monetary P&L (USDT);
* per **window**: one OOS cutoff, including ``NO_TRADE`` (0 return, 0 P&L);
* per **period**: chronological aggregation (fold / year / quarter / month).

Capital-dependent metrics (profit factor, concentration, drawdown, contribution
to P&L) always use **monetary** P&L.  Percentage sums are kept only where CP07
reports them, as a cross-check.

Everything here is descriptive.  A high or low metric does not promote the
strategy: the statistical promotion gate stays in CP07/CP08-01 and is *not*
recomputed here.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from .decision_contract import ACTION_LONG, ACTION_NO_TRADE, ACTION_SHORT, HORIZON_HOURS
from .policy_evaluation import make_folds
from .regime_analysis import fit_tercile_thresholds
from .simulator_integrity_audit import (
    AuditPaths,
    _json_safe,
    calendar_equity_curves,
    concentration_profile,
    contract_frame,
    dollar_metrics,
    load_archive,
    max_drawdown,
    reconstruct_returns,
    replay_policy,
)
from .sync import digest
from .trading_simulator import DEFAULT_INITIAL_CAPITAL

STABILITY_SCHEMA = "cdl.cp08.momentum_stability.v1"
POLICY = "momentum_threshold"
POLICY_LABEL = "Momentum threshold"
TOP_K = (1, 3, 5, 10)
DRAWDOWN_LEVELS = (0.01, 0.02, 0.05, 0.10)
PERIOD_GRANULARITIES = ("window", "fold", "month", "quarter", "year")
TREND_BUCKETS = ("TREND_DOWN", "SIDEWAYS", "TREND_UP")
VOL_BUCKETS = ("VOL_LOW", "VOL_MEDIUM", "VOL_HIGH")
MIN_BUCKET_TRADES = 10
FEATURE_COLUMNS = (
    "momentum_168h",
    "volatility_168h",
    "atr_pct",
    "trend_strength_pct",
    "dist_sma50_pct",
    "rsi14",
    "volume_ratio",
)


@dataclass(frozen=True)
class StabilityPaths:
    data_root: Path = Path("data")
    snapshot_dir: Path = Path("data/snapshots/BTCUSDT-1h-raw-v1")
    label_dir: Path = Path("data/datasets/S02-CP07-decision-labels-v1")
    regime_dir: Path = Path("data/datasets/S02-CP06B-regime-features-v1")
    trading_dir: Path = Path("data/experiments/S02-CP07-trading-simulation-v1")
    audit_dir: Path = Path("data/experiments/S02-CP08-simulator-integrity-audit-v1")
    output_dir: Path = Path("data/experiments/S02-CP08-momentum-stability-v1")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _source_hashes(paths: StabilityPaths) -> dict[str, str]:
    """Hash every input, including the CP08-01 archive we chain from."""

    files = {
        "trading_decisions": paths.trading_dir / "decisions.parquet",
        "trading_summary": paths.trading_dir / "summary.json",
        "trading_fold_summary": paths.trading_dir / "fold_summary.json",
        "trading_sensitivity": paths.trading_dir / "sensitivity.json",
        "trading_manifest": paths.trading_dir / "manifest.json",
        "labels": paths.label_dir / "labels.parquet",
        "labels_manifest": paths.label_dir / "manifest.json",
        "regime_features": paths.regime_dir / "features.parquet",
        "regime_errors": paths.regime_dir / "errors.parquet",
        "snapshot_raw_manifest": paths.snapshot_dir / "raw-manifest.json",
        "snapshot_anomalies": paths.snapshot_dir / "anomalies.json",
        "audit_summary": paths.audit_dir / "summary.json",
        "audit_checks": paths.audit_dir / "checks.json",
        "audit_metrics": paths.audit_dir / "metrics_comparison.csv",
        "audit_equity": paths.audit_dir / "equity_curves.parquet",
        "audit_thresholds": paths.audit_dir / "threshold_robustness.json",
        "audit_manifest": paths.audit_dir / "manifest.json",
    }
    return {name: digest(path) for name, path in files.items() if path.is_file()}


def _safe_divide(numerator: float, denominator: float) -> float | None:
    if denominator is None or not np.isfinite(denominator) or denominator == 0:
        return None
    return float(numerator) / float(denominator)


def _gini(values: Iterable[float]) -> float:
    array = np.asarray([value for value in values if np.isfinite(value)], dtype=float)
    array = array[array > 0.0]
    if array.size == 0:
        return 0.0
    ordered = np.sort(array)
    n = ordered.size
    positions = np.arange(1, n + 1, dtype=float)
    return float(2.0 * (positions * ordered).sum() / (n * ordered.sum()) - (n + 1) / n)


def _herfindahl(values: Iterable[float]) -> dict[str, float | None]:
    array = np.asarray([value for value in values if np.isfinite(value)], dtype=float)
    array = array[array > 0.0]
    if array.size == 0:
        return {"index": None, "effective_trades": None}
    shares = array / array.sum()
    index = float((shares**2).sum())
    return {"index": index, "effective_trades": float(1.0 / index)}


def _profit_factor(profits: float, losses: float) -> float | None:
    if losses <= 0.0:
        return None
    return float(profits / losses)


def _distribution_stats(values: Iterable[float]) -> dict[str, float | None]:
    """Descriptive moments and quantiles of a per-trade metric."""

    array = np.asarray([value for value in values if np.isfinite(value)], dtype=float)
    if array.size == 0:
        return {}
    series = pd.Series(array)
    return {
        "count": int(array.size),
        "mean": float(array.mean()),
        "median": float(np.median(array)),
        "std": float(array.std(ddof=1)) if array.size > 1 else 0.0,
        "min": float(array.min()),
        "max": float(array.max()),
        "p05": float(np.quantile(array, 0.05)),
        "p25": float(np.quantile(array, 0.25)),
        "p75": float(np.quantile(array, 0.75)),
        "p95": float(np.quantile(array, 0.95)),
        "skew": float(series.skew()) if array.size > 2 else None,
        "kurtosis": float(series.kurt()) if array.size > 3 else None,
        "positive_share_pct": 100.0 * float((array > 0).mean()),
    }


# ---------------------------------------------------------------------------
# reconstruction (reuses the audited CP08-01 helpers)
# ---------------------------------------------------------------------------


def prepare_archive(
    archive: Mapping[str, Any], *, initial_capital: float = DEFAULT_INITIAL_CAPITAL
) -> dict[str, Any]:
    """Reconstruct the 106 momentum trades from the frozen CP07 archive."""

    decisions = archive["decisions"]
    momentum_archive = decisions[decisions["policy"] == POLICY].sort_values("cutoff").reset_index(drop=True)
    if momentum_archive.empty:
        raise ValueError("No momentum_threshold decisions in the archive")
    frame, contract_diagnostics = contract_frame(momentum_archive, archive["labels"])
    rebuilt, reconstruction = reconstruct_returns(frame, archive["labels"])
    ledger, metrics = replay_policy(rebuilt, initial_capital=initial_capital)
    dollars = dollar_metrics(ledger, initial_capital=initial_capital)
    # Enrich the audited metric dictionary with the simulator's own drawdown and
    # total return so CP07/CP08 reconciliation uses one source of truth.
    dollars["max_drawdown_pct"] = float(metrics.max_drawdown_pct)
    dollars["total_return_pct"] = float(metrics.total_return_pct)
    return {
        "archive_decisions": momentum_archive,
        "frame": rebuilt,
        "ledger": ledger,
        "metrics": metrics,
        "dollars": dollars,
        "contract_diagnostics": contract_diagnostics,
        "reconstruction": reconstruction,
    }


def fold_definitions(archive: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The archived 5 walk-forward folds, rebuilt from labels + features only."""

    merged = archive["labels"].merge(
        archive["features"][["window_index", *FEATURE_COLUMNS]],
        on="window_index",
        how="inner",
        validate="one_to_one",
    ).sort_values("cutoff").reset_index(drop=True)
    if len(merged) != len(archive["labels"]):
        raise ValueError("Labels and features do not share the same window set")
    return make_folds(merged, n_folds=5, min_train=200, test_size=60)


def _fold_feature_regimes(
    archive: Mapping[str, Any], folds: Iterable[dict[str, Any]]
) -> tuple[pd.DataFrame, dict[str, Any], dict[int, list[int]]]:
    """Train-only tercile buckets for volatility and trend.

    Thresholds are fitted **only** on the training windows of each fold.  No
    target, no OOS return and no frozen threshold is used, so the bucketing
    cannot leak the future.  The archived ``regime`` column (trend terciles
    computed by CP07) is reproduced as a reconciliation check.
    """

    labels = archive["labels"]
    features = archive["features"].set_index("window_index")
    rows: list[dict[str, Any]] = []
    fold_thresholds: dict[str, Any] = {}
    fold_test_index: dict[int, list[int]] = {}
    for fold in folds:
        train = labels.loc[fold["train_index"]]
        train_momentum = features.loc[train["window_index"], "momentum_168h"].to_numpy(dtype=float)
        train_volatility = features.loc[train["window_index"], "volatility_168h"].to_numpy(dtype=float)
        trend_thresholds = fit_tercile_thresholds(train_momentum)
        vol_thresholds = fit_tercile_thresholds(train_volatility)
        fold_thresholds[str(fold["fold"])] = {
            "trend": [float(trend_thresholds[0]), float(trend_thresholds[1])],
            "volatility": [float(vol_thresholds[0]), float(vol_thresholds[1])],
            "train_windows": int(fold["train_size"]),
            "test_windows": int(fold["test_size"]),
        }
        test_index = labels.loc[fold["test_index"]].sort_values("cutoff")["window_index"].tolist()
        fold_test_index[int(fold["fold"])] = [int(value) for value in test_index]
        for window_index in test_index:
            momentum = float(features.loc[window_index, "momentum_168h"])
            volatility = float(features.loc[window_index, "volatility_168h"])
            rows.append(
                {
                    "window_index": int(window_index),
                    "fold": int(fold["fold"]),
                    "trend_regime": (
                        TREND_BUCKETS[0]
                        if momentum < trend_thresholds[0]
                        else TREND_BUCKETS[2]
                        if momentum > trend_thresholds[1]
                        else TREND_BUCKETS[1]
                    ),
                    "vol_regime": (
                        VOL_BUCKETS[0]
                        if volatility < vol_thresholds[0]
                        else VOL_BUCKETS[2]
                        if volatility > vol_thresholds[1]
                        else VOL_BUCKETS[1]
                    ),
                    "trend_tercile_low": float(trend_thresholds[0]),
                    "trend_tercile_high": float(trend_thresholds[1]),
                    "vol_tercile_low": float(vol_thresholds[0]),
                    "vol_tercile_high": float(vol_thresholds[1]),
                }
            )
    return pd.DataFrame(rows), fold_thresholds, fold_test_index


def build_window_frame(
    archive: Mapping[str, Any],
    prepared: Mapping[str, Any],
    *,
    initial_capital: float = DEFAULT_INITIAL_CAPITAL,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """One row per OOS window (300), including the flat NO_TRADE windows."""

    ledger = prepared["ledger"].sort_values("entry_timestamp").reset_index(drop=True)
    folds = fold_definitions(archive)
    window_regimes, fold_thresholds, fold_test_index = _fold_feature_regimes(archive, folds)

    features = archive["features"].set_index("window_index")
    feature_bar = pd.to_datetime(features["feature_bar"], utc=True)
    cutoff = pd.to_datetime(features["cutoff"], utc=True)
    feature_guard = {
        "feature_bar_equals_cutoff_minus_hour": bool(
            (feature_bar.reindex(features.index) == cutoff.reindex(features.index) - pd.Timedelta(hours=1)).all()
        ),
        "rows_checked": int(len(features)),
    }

    frame = pd.DataFrame(
        {
            "window_index": ledger["window_index"].astype(int),
            "decision_id": ledger["decision_id"].astype(str),
            "fold": ledger["fold"].astype(int),
            "cutoff": pd.to_datetime(ledger["entry_timestamp"], utc=True),
            "exit_timestamp": pd.to_datetime(ledger["exit_timestamp"], utc=True),
            "action": ledger["action"].astype(str),
            "net_return_pct": ledger["net_return_pct"].astype(float),
            "gross_return_pct": ledger["gross_return_pct"].astype(float),
            "equity_after_usdt": ledger["equity_after"].astype(float),
            "executed": ledger["executed"].astype(bool),
        }
    )
    frame["equity_before_usdt"] = np.concatenate(
        [[float(initial_capital)], frame["equity_after_usdt"].to_numpy(dtype=float)[:-1]]
    )
    frame["pnl_usdt"] = frame["equity_before_usdt"] * frame["net_return_pct"] / 100.0
    frame["period_year"] = frame["cutoff"].dt.year.astype(int)
    frame["period_quarter"] = frame["cutoff"].dt.year.astype(int).astype(str) + "Q" + frame["cutoff"].dt.quarter.astype(str)
    frame["period_month"] = frame["cutoff"].dt.strftime("%Y-%m")

    feature_index = features.reindex(frame["window_index"])
    for column in FEATURE_COLUMNS:
        frame[column] = feature_index[column].to_numpy(dtype=float)
    frame["feature_bar"] = pd.to_datetime(feature_index["feature_bar"], utc=True).to_numpy()

    regime_index = window_regimes.set_index("window_index")
    frame["trend_regime"] = regime_index["trend_regime"].reindex(frame["window_index"]).to_numpy()
    frame["vol_regime"] = regime_index["vol_regime"].reindex(frame["window_index"]).to_numpy()
    frame["trend_tercile_low"] = regime_index["trend_tercile_low"].reindex(frame["window_index"]).to_numpy(dtype=float)
    frame["trend_tercile_high"] = regime_index["trend_tercile_high"].reindex(frame["window_index"]).to_numpy(dtype=float)
    frame["vol_tercile_low"] = regime_index["vol_tercile_low"].reindex(frame["window_index"]).to_numpy(dtype=float)
    frame["vol_tercile_high"] = regime_index["vol_tercile_high"].reindex(frame["window_index"]).to_numpy(dtype=float)
    frame["archived_regime"] = ledger["regime"].astype(str).to_numpy()

    # Same-window fold assignment check: the archived fold must equal the fold
    # in which the window is a test block (never a training window).
    expected_fold = {}
    for fold, windows in fold_test_index.items():
        for window_index in windows:
            expected_fold[window_index] = fold
    fold_assignment_match = int(
        sum(1 for row in frame.itertuples(index=False) if expected_fold.get(int(row.window_index)) == int(row.fold))
    )

    diagnostics = {
        "windows": int(len(frame)),
        "trades": int((frame["action"] != ACTION_NO_TRADE).sum()),
        "no_trade": int((frame["action"] == ACTION_NO_TRADE).sum()),
        "skipped": int((~frame["executed"]).sum()),
        "feature_cutoff_guard": feature_guard,
        "fold_assignment_matches": fold_assignment_match,
        "fold_assignment_total": int(len(frame)),
        "fold_thresholds": fold_thresholds,
        "trend_regime_matches_archive": int((frame["trend_regime"] == frame["archived_regime"]).sum()),
        "threshold_values_archived": sorted(
            {float(value) for value in archive["decisions"].loc[archive["decisions"]["policy"] == POLICY, "threshold"].dropna()}
        ),
        "pnl_attribution_error_usdt": float(
            frame["pnl_usdt"].sum() - (frame["equity_after_usdt"].iloc[-1] - float(initial_capital))
        ),
    }
    return frame, diagnostics


# ---------------------------------------------------------------------------
# per-trade attribution
# ---------------------------------------------------------------------------


def trade_attribution(
    window_frame: pd.DataFrame, *, initial_capital: float = DEFAULT_INITIAL_CAPITAL
) -> pd.DataFrame:
    """The 106 executed momentum trades with monetary attribution columns."""

    trades = window_frame[window_frame["action"] != ACTION_NO_TRADE].copy()
    trades = trades.sort_values("cutoff").reset_index(drop=True)
    trades.insert(0, "trade_index", np.arange(1, len(trades) + 1))
    total_pnl = float(trades["pnl_usdt"].sum())
    gross_profit = float(trades.loc[trades["pnl_usdt"] > 0, "pnl_usdt"].sum())
    trades["cost_drag_pct"] = trades["gross_return_pct"] - trades["net_return_pct"]
    trades["pnl_pct_of_initial_capital"] = 100.0 * trades["pnl_usdt"] / float(initial_capital)
    trades["cumulative_pnl_usdt"] = trades["pnl_usdt"].cumsum()
    trades["pnl_share_of_net_pnl_pct"] = 100.0 * trades["pnl_usdt"] / total_pnl if total_pnl else np.nan
    trades["pnl_share_of_gross_profit_pct"] = (
        100.0 * trades["pnl_usdt"].clip(lower=0.0) / gross_profit if gross_profit else np.nan
    )
    trades["rank_by_pnl"] = trades["pnl_usdt"].rank(ascending=False, method="first").astype(int)
    trades["rank_by_net_pct"] = trades["net_return_pct"].rank(ascending=False, method="first").astype(int)
    trades["outcome"] = np.where(
        trades["net_return_pct"] > 0, "WIN", np.where(trades["net_return_pct"] < 0, "LOSS", "FLAT")
    )
    trades["direction"] = trades["action"].map({ACTION_LONG: "LONG", ACTION_SHORT: "SHORT"})

    # Exit-marked drawdown of the monetary equity path (trade granularity).
    equity = np.concatenate([[float(initial_capital)], trades["equity_after_usdt"].to_numpy(dtype=float)])
    peak = np.maximum.accumulate(equity)
    trades["equity_peak_usdt"] = peak[1:]
    trades["drawdown_from_peak_pct"] = 100.0 * (equity[1:] / peak[1:] - 1.0)

    ordered = [
        "trade_index",
        "decision_id",
        "window_index",
        "fold",
        "cutoff",
        "exit_timestamp",
        "direction",
        "action",
        "horizon_hours",
        "year",
        "quarter",
        "month",
        "net_return_pct",
        "gross_return_pct",
        "cost_drag_pct",
        "pnl_usdt",
        "pnl_pct_of_initial_capital",
        "pnl_share_of_net_pnl_pct",
        "pnl_share_of_gross_profit_pct",
        "cumulative_pnl_usdt",
        "equity_before_usdt",
        "equity_after_usdt",
        "equity_peak_usdt",
        "drawdown_from_peak_pct",
        "rank_by_pnl",
        "rank_by_net_pct",
        "outcome",
        "trend_regime",
        "vol_regime",
        "archived_regime",
        "momentum_168h",
        "volatility_168h",
        "atr_pct",
        "trend_strength_pct",
        "dist_sma50_pct",
        "rsi14",
        "volume_ratio",
        "feature_bar",
    ]
    trades["horizon_hours"] = HORIZON_HOURS
    trades["year"] = trades["period_year"]
    trades["quarter"] = trades["period_quarter"]
    trades["month"] = trades["period_month"]
    return trades[ordered].reset_index(drop=True)


# ---------------------------------------------------------------------------
# per-window and per-period metrics
# ---------------------------------------------------------------------------


def _metrics_from_rows(
    rows: pd.DataFrame, *, initial_capital: float, contiguous: bool = True
) -> dict[str, Any]:
    """Metrics of a chronologically ordered slice of the window frame.

    ``contiguous=False`` is used for regime buckets, whose windows are not a
    contiguous slice of the equity path: realised P&L is still additive, but
    ``equity_start``/``equity_end`` are not meaningful and are left as NaN.
    """

    net = rows["net_return_pct"].to_numpy(dtype=float)
    pnl = rows["pnl_usdt"].to_numpy(dtype=float)
    traded = rows["action"].to_numpy() != ACTION_NO_TRADE
    trade_net = net[traded]
    trade_pnl = pnl[traded]
    wins = trade_net[trade_net > 0]
    losses = trade_net[trade_net < 0]
    gross_profit = float(trade_pnl[trade_pnl > 0].sum())
    gross_loss = float(abs(trade_pnl[trade_pnl < 0].sum()))
    if contiguous and len(rows):
        equity_start = float(rows["equity_before_usdt"].iloc[0])
        equity_end = float(rows["equity_after_usdt"].iloc[-1])
    elif not contiguous:
        equity_start = float("nan")
        equity_end = float("nan")
    else:
        equity_start = float(initial_capital)
        equity_end = float(initial_capital)
    long_mask = rows["action"].to_numpy() == ACTION_LONG
    short_mask = rows["action"].to_numpy() == ACTION_SHORT
    return {
        "windows": int(len(rows)),
        "trades": int(traded.sum()),
        "long_trades": int(long_mask.sum()),
        "short_trades": int(short_mask.sum()),
        "no_trade": int((~traded).sum()),
        "wins": int((trade_net > 0).sum()),
        "losses": int((trade_net < 0).sum()),
        "win_rate_trade_pct": 100.0 * float((trade_net > 0).mean()) if len(trade_net) else 0.0,
        "win_rate_window_pct": 100.0 * float((net > 0).mean()) if len(net) else 0.0,
        "net_pnl_usdt": float(trade_pnl.sum()),
        "gross_profit_usdt": gross_profit,
        "gross_loss_usdt": gross_loss,
        "profit_factor_usdt": _profit_factor(gross_profit, gross_loss),
        "profit_factor_pct": _profit_factor(float(wins.sum()), float(abs(losses.sum())))
        if len(losses)
        else None,
        "compounded_return_pct": 100.0 * (float(np.prod(1.0 + net / 100.0)) - 1.0) if len(net) else 0.0,
        "avg_net_trade_pct": float(trade_net.mean()) if len(trade_net) else 0.0,
        "median_net_trade_pct": float(np.median(trade_net)) if len(trade_net) else 0.0,
        "std_net_trade_pct": float(np.std(trade_net, ddof=1)) if len(trade_net) > 1 else 0.0,
        "avg_net_window_pct": float(net.mean()) if len(net) else 0.0,
        "best_net_pct": float(trade_net.max()) if len(trade_net) else 0.0,
        "worst_net_pct": float(trade_net.min()) if len(trade_net) else 0.0,
        "equity_start_usdt": equity_start,
        "equity_end_usdt": equity_end,
        "trades_per_window": float(traded.sum() / len(rows)) if len(rows) else 0.0,
    }


def period_summary(window_frame: pd.DataFrame, *, initial_capital: float = DEFAULT_INITIAL_CAPITAL) -> pd.DataFrame:
    """Per-window, per-fold, per-month, per-quarter and per-year metrics."""

    total_pnl = float(window_frame["pnl_usdt"].sum())
    windows = window_frame.sort_values("cutoff").reset_index(drop=True)
    key_columns = {
        "window": "window_index",
        "fold": "fold",
        "month": "period_month",
        "quarter": "period_quarter",
        "year": "period_year",
    }
    rows: list[dict[str, Any]] = []
    for granularity in PERIOD_GRANULARITIES:
        key = key_columns[granularity]
        groups = windows.groupby(key, sort=True)
        for period, group in groups:
            group = group.sort_values("cutoff")
            values = _metrics_from_rows(group, initial_capital=initial_capital)
            label = f"window-{int(period):04d}" if granularity == "window" else str(period)
            rows.append(
                {
                    "granularity": granularity,
                    "period": label,
                    "period_start": pd.Timestamp(group["cutoff"].iloc[0]),
                    "period_end": pd.Timestamp(group["exit_timestamp"].iloc[-1]),
                    **values,
                    "share_of_net_pnl_pct": 100.0 * values["net_pnl_usdt"] / total_pnl if total_pnl else 0.0,
                    "cumulative_pnl_usdt": values["net_pnl_usdt"],
                }
            )
    summary = pd.DataFrame(rows)
    # Cumulative monetary attribution is additive: sum of period P&L == total.
    summary["cumulative_pnl_usdt"] = summary.groupby("granularity", sort=False)["net_pnl_usdt"].cumsum()
    return summary


def summary_reconciliation(periods: pd.DataFrame, total_pnl: float) -> dict[str, Any]:
    """Every period granularity must add up to the same monetary total."""

    checks = {}
    worst = 0.0
    for granularity in PERIOD_GRANULARITIES:
        subset = periods[periods["granularity"] == granularity]
        error = float(abs(subset["net_pnl_usdt"].sum() - total_pnl))
        worst = max(worst, error)
        checks[granularity] = {
            "rows": int(len(subset)),
            "net_pnl_usdt_sum": float(subset["net_pnl_usdt"].sum()),
            "abs_error_usdt": error,
        }
    return {"per_granularity": checks, "max_abs_error_usdt": worst, "additive": bool(worst < 1e-6)}


# ---------------------------------------------------------------------------
# streaks (win / loss sequences)
# ---------------------------------------------------------------------------


def _run_lengths(outcomes: np.ndarray) -> list[tuple[str, int]]:
    runs: list[tuple[str, int]] = []
    for outcome in outcomes:
        if runs and runs[-1][0] == outcome:
            runs[-1] = (outcome, runs[-1][1] + 1)
        else:
            runs.append((outcome, 1))
    return runs


def streak_analysis(trades: pd.DataFrame) -> dict[str, Any]:
    """Win/loss sequences and a descriptive runs test (not a promotion gate)."""

    ordered = trades.sort_values("cutoff").reset_index(drop=True)
    outcomes = ordered["outcome"].to_numpy()
    binary = outcomes[outcomes != "FLAT"]
    runs = _run_lengths(binary)
    win_runs = [length for label, length in runs if label == "WIN"]
    loss_runs = [length for label, length in runs if label == "LOSS"]
    pnl = ordered.loc[ordered["outcome"] != "FLAT", "pnl_usdt"].to_numpy(dtype=float)
    dates = ordered.loc[ordered["outcome"] != "FLAT", "cutoff"].to_numpy()

    def _png_for(label: str) -> list[dict[str, Any]]:
        output = []
        position = 0
        for run_label, length in runs:
            run_pnl = float(pnl[position:position + length].sum())
            start = pd.Timestamp(dates[position]) if len(dates) else None
            end = pd.Timestamp(dates[position + length - 1]) if len(dates) else None
            if run_label == label:
                output.append(
                    {
                        "length": int(length),
                        "start": start.isoformat() if start is not None else None,
                        "end": end.isoformat() if end is not None else None,
                        "pnl_usdt": run_pnl,
                    }
                )
            position += length
        return output

    winning = _png_for("WIN")
    losing = _png_for("LOSS")
    n1 = int((binary == "WIN").sum())
    n2 = int((binary == "LOSS").sum())
    n = n1 + n2
    observed_runs = len(runs)
    expected_runs = (2.0 * n1 * n2) / n + 1.0 if n else 0.0
    variance = (
        (2.0 * n1 * n2 * (2.0 * n1 * n2 - n)) / (n**2 * (n - 1)) if n > 1 and n1 and n2 else 0.0
    )
    z_score = (observed_runs - expected_runs) / np.sqrt(variance) if variance > 0 else 0.0

    def _distribution(lengths: list[int]) -> dict[str, int]:
        return {str(length): int(lengths.count(length)) for length in sorted(set(lengths))}

    return {
        "trades": int(len(ordered)),
        "wins": n1,
        "losses": n2,
        "flat": int(len(outcomes) - n),
        "win_rate_pct": 100.0 * n1 / n if n else 0.0,
        "runs": int(observed_runs),
        "max_win_streak": int(max(win_runs)) if win_runs else 0,
        "max_loss_streak": int(max(loss_runs)) if loss_runs else 0,
        "mean_win_streak": float(np.mean(win_runs)) if win_runs else 0.0,
        "mean_loss_streak": float(np.mean(loss_runs)) if loss_runs else 0.0,
        "win_streak_distribution": _distribution(win_runs),
        "loss_streak_distribution": _distribution(loss_runs),
        "win_streak_pnl": winning,
        "loss_streak_pnl": losing,
        "longest_win_streak_pnl_usdt": max(winning, key=lambda item: item["pnl_usdt"])["pnl_usdt"] if winning else 0.0,
        "worst_loss_streak_pnl_usdt": min(losing, key=lambda item: item["pnl_usdt"])["pnl_usdt"] if losing else 0.0,
        "largest_loss_streaks": sorted(losing, key=lambda item: item["pnl_usdt"])[:5],
        "runs_test": {
            "observed_runs": int(observed_runs),
            "expected_runs_if_iid": float(expected_runs),
            "variance": float(variance),
            "z_score": float(z_score),
            "interpretation": (
                "Descriptive only. |z|>1.96 would flag serial dependence of win/loss "
                "outcomes; it is not a strategy promotion test."
            ),
        },
    }


# ---------------------------------------------------------------------------
# concentration
# ---------------------------------------------------------------------------


def _share_needed(sorted_pnl: np.ndarray, total: float, target: float) -> int | None:
    if total <= 0:
        return None
    cumulative = np.cumsum(sorted_pnl)
    hits = np.where(cumulative >= target * total)[0]
    return int(hits[0] + 1) if len(hits) else None


def concentration_summary(
    trades: pd.DataFrame,
    periods: pd.DataFrame,
    ledger: pd.DataFrame,
    *,
    initial_capital: float = DEFAULT_INITIAL_CAPITAL,
) -> dict[str, Any]:
    """Top-k, HHI/Gini and temporal concentration of the monetary P&L."""

    ordered = trades.sort_values("pnl_usdt", ascending=False).reset_index(drop=True)
    pnl = ordered["pnl_usdt"].to_numpy(dtype=float)
    total_pnl = float(pnl.sum())
    positive = pnl[pnl > 0]
    gross_profit = float(positive.sum())
    gross_loss = float(abs(pnl[pnl < 0].sum()))
    final_equity = float(initial_capital) + total_pnl
    recompounded = concentration_profile(ledger, initial_capital=initial_capital)["leave_out"]

    cumulative = np.cumsum(pnl)
    threshold = max(abs(total_pnl), 1e-12) * 1e-9
    crossing = int(np.argmax(cumulative >= total_pnl - threshold) + 1) if len(pnl) else 0
    top_k: dict[str, Any] = {}
    for k in TOP_K:
        if k > len(pnl):
            continue
        selected = ordered.iloc[:k]
        removed_pnl = float(selected["pnl_usdt"].sum())
        equity_after = final_equity - removed_pnl
        top_k[str(k)] = {
            "trades_removed": int(k),
            "pnl_usdt": removed_pnl,
            "share_of_gross_profit_pct": 100.0 * float(selected["pnl_usdt"].clip(lower=0.0).sum()) / gross_profit
            if gross_profit
            else None,
            "share_of_net_pnl_pct": 100.0 * removed_pnl / total_pnl if total_pnl else None,
            "equity_after_removal_usdt": equity_after,
            "return_pct_after_removal": 100.0 * (equity_after / float(initial_capital) - 1.0),
            "recompounded_return_pct_after_removal": recompounded.get(str(k)),
            "removed_trades": [
                {
                    "rank": int(position + 1),
                    "decision_id": str(row.decision_id),
                    "cutoff": pd.Timestamp(row.cutoff).isoformat(),
                    "fold": int(row.fold),
                    "month": str(row.month),
                    "direction": str(row.direction),
                    "net_return_pct": float(row.net_return_pct),
                    "pnl_usdt": float(row.pnl_usdt),
                }
                for position, row in enumerate(selected.itertuples(index=False))
            ],
            "distinct_months": int(selected["month"].nunique()),
            "distinct_folds": int(selected["fold"].nunique()),
        }

    temporal = _temporal_concentration(ordered, periods, total_pnl)
    curve = [
        {
            "rank": int(position + 1),
            "decision_id": str(row.decision_id),
            "fold": int(row.fold),
            "month": str(row.month),
            "pnl_usdt": float(row.pnl_usdt),
            "cumulative_pnl_usdt": float(cumulative[position]),
            "share_of_net_pnl_pct": 100.0 * float(cumulative[position]) / total_pnl if total_pnl else None,
            "share_of_gross_profit_pct": 100.0 * float(max(cumulative[position], 0.0)) / gross_profit
            if gross_profit
            else None,
        }
        for position, row in enumerate(ordered.itertuples(index=False))
    ]
    positive_hhi = _herfindahl(positive)
    loss_hhi = _herfindahl(np.abs(pnl[pnl < 0]))
    return {
        "schema_version": "cdl.cp08.momentum_stability.concentration.v1",        "policy": POLICY,
        "policy_label": POLICY_LABEL,
        "initial_capital": float(initial_capital),
        "trades": int(len(ordered)),
        "positive_trades": int(len(positive)),
        "negative_trades": int((pnl < 0).sum()),
        "total_net_pnl_usdt": total_pnl,
        "total_return_pct": 100.0 * (final_equity / float(initial_capital) - 1.0),
        "final_equity_usdt": final_equity,
        "gross_profit_usdt": gross_profit,
        "gross_loss_usdt": gross_loss,
        "profit_factor_usdt": _profit_factor(gross_profit, gross_loss),
        "avg_win_usdt": float(positive.mean()) if len(positive) else 0.0,
        "avg_loss_usdt": float(pnl[pnl < 0].mean()) if (pnl < 0).any() else 0.0,
        "top_k": top_k,
        "leave_top_k_recompounded_pct": {
            str(key): float(value) for key, value in sorted(recompounded.items(), key=lambda item: int(item[0]))
        },
        "leave_top_k_monetary_pct": {
            str(k): top_k[str(k)]["return_pct_after_removal"] for k in TOP_K if str(k) in top_k
        },
        "gini": {
            "positive_pnl": _gini(positive),
            "absolute_pnl": _gini(np.abs(pnl)),
        },
        "herfindahl": {
            "positive_pnl": positive_hhi,
            "absolute_loss": loss_hhi,
        },
        "top_decile_share_of_gross_profit_pct": (
            100.0 * float(np.sort(positive)[::-1][: max(1, len(positive) // 10)].sum()) / gross_profit
            if gross_profit
            else None
        ),
        "trade_distribution": {
            "net_return_pct": _distribution_stats(ordered["net_return_pct"].to_numpy(dtype=float)),
            "pnl_usdt": _distribution_stats(pnl),
        },
        "cumulative_share_curve": curve,
        "trades_to_reach_total_pnl": crossing,
        "temporal_concentration": temporal,
        "notes": [
            "P&L monetario (equity composta al 100%), non somma di percentuali.",
            "leave_top_k_recompounded_pct replica CP08-01: ricompone la curva senza i k trade migliori.",
            "leave_top_k_monetary_pct sottrae il P&L monetario dei k trade migliori: è additivo e non riordina l'equity.",
            "Le soglie della policy non sono ri-selezionate: la policy è quella congelata da CP07 (5% in 5/5 fold).",
        ],
    }


def _temporal_concentration(ordered: pd.DataFrame, periods: pd.DataFrame, total_pnl: float) -> dict[str, Any]:
    monthly = (
        periods[(periods["granularity"] == "month") & (periods["trades"] > 0)]
        .sort_values("net_pnl_usdt", ascending=False)
        .reset_index(drop=True)
    )
    quarterly = (
        periods[(periods["granularity"] == "quarter") & (periods["trades"] > 0)]
        .sort_values("net_pnl_usdt", ascending=False)
        .reset_index(drop=True)
    )
    folds = (
        periods[(periods["granularity"] == "fold") & (periods["trades"] > 0)]
        .sort_values("net_pnl_usdt", ascending=False)
        .reset_index(drop=True)
    )
    month_pnl = monthly["net_pnl_usdt"].to_numpy(dtype=float)
    fold_pnl = folds["net_pnl_usdt"].to_numpy(dtype=float)
    top_trades = ordered.head(5)
    best_month = (
        {
            "period": str(monthly["period"].iloc[0]),
            "pnl_usdt": float(month_pnl[0]),
            "share_of_net_pnl_pct": 100.0 * float(month_pnl[0]) / total_pnl if total_pnl else None,
            "trades": int(monthly["trades"].iloc[0]),
        }
        if len(monthly)
        else None
    )
    best_quarter = (
        {
            "period": str(quarterly["period"].iloc[0]),
            "pnl_usdt": float(quarterly["net_pnl_usdt"].iloc[0]),
            "share_of_net_pnl_pct": 100.0 * float(quarterly["net_pnl_usdt"].iloc[0]) / total_pnl if total_pnl else None,
        }
        if len(quarterly)
        else None
    )
    best_fold = (
        {
            "fold": int(folds["period"].iloc[0]),
            "pnl_usdt": float(fold_pnl[0]),
            "share_of_net_pnl_pct": 100.0 * float(fold_pnl[0]) / total_pnl if total_pnl else None,
            "trades": int(folds["trades"].iloc[0]),
        }
        if len(folds)
        else None
    )
    return {
        "months_with_trades": int(len(monthly)),
        "months_analyzed": int((periods["granularity"] == "month").sum()),
        "profitable_months": int((monthly["net_pnl_usdt"] > 0).sum()),
        "losing_months": int((monthly["net_pnl_usdt"] < 0).sum()),
        "positive_pnl_concentration": {
            "best_month": best_month,
            "best_quarter": best_quarter,
            "best_fold": best_fold,
            "top_1_month_share_of_net_pnl_pct": 100.0 * float(month_pnl[:1].sum()) / total_pnl if total_pnl else None,
            "top_3_months_share_of_net_pnl_pct": 100.0 * float(month_pnl[:3].sum()) / total_pnl if total_pnl else None,
            "top_5_months_share_of_net_pnl_pct": 100.0 * float(month_pnl[:5].sum()) / total_pnl if total_pnl else None,
            "top_1_fold_share_of_net_pnl_pct": 100.0 * float(fold_pnl[:1].sum()) / total_pnl if total_pnl else None,
        },
        "months_to_reach": {
            "50pct": _share_needed(month_pnl, total_pnl, 0.50),
            "80pct": _share_needed(month_pnl, total_pnl, 0.80),
            "100pct": _share_needed(month_pnl, total_pnl, 0.999999),
        },
        "folds_to_reach": {
            "50pct": _share_needed(fold_pnl, total_pnl, 0.50),
            "80pct": _share_needed(fold_pnl, total_pnl, 0.80),
        },
        "best_months": [
            {
                "period": str(row.period),
                "pnl_usdt": float(row.net_pnl_usdt),
                "trades": int(row.trades),
                "win_rate_trade_pct": float(row.win_rate_trade_pct),
                "share_of_net_pnl_pct": float(row.share_of_net_pnl_pct),
            }
            for row in monthly.head(5).itertuples(index=False)
        ],
        "worst_months": [
            {
                "period": str(row.period),
                "pnl_usdt": float(row.net_pnl_usdt),
                "trades": int(row.trades),
                "win_rate_trade_pct": float(row.win_rate_trade_pct),
                "share_of_net_pnl_pct": float(row.share_of_net_pnl_pct),
            }
            for row in monthly.tail(5).sort_values("net_pnl_usdt").itertuples(index=False)
        ],
        "top_trade_clustering": {
            "top_5_trades": [
                {
                    "rank": int(position + 1),
                    "decision_id": str(row.decision_id),
                    "cutoff": pd.Timestamp(row.cutoff).isoformat(),
                    "fold": int(row.fold),
                    "month": str(row.month),
                    "direction": str(row.direction),
                    "net_return_pct": float(row.net_return_pct),
                    "pnl_usdt": float(row.pnl_usdt),
                }
                for position, row in enumerate(top_trades.itertuples(index=False))
            ],
            "distinct_months": int(top_trades["month"].nunique()),
            "distinct_folds": int(top_trades["fold"].nunique()),
            "month_counts": {str(key): int(value) for key, value in top_trades["month"].value_counts().items()},
            "fold_counts": {str(key): int(value) for key, value in top_trades["fold"].value_counts().items()},
            "pnl_share_by_month": {
                str(key): 100.0 * float(value) / total_pnl if total_pnl else None
                for key, value in top_trades.groupby("month")["pnl_usdt"].sum().items()
            },
        },
    }


# ---------------------------------------------------------------------------
# drawdown (calendar-time, exit-marked and mark-to-market)
# ---------------------------------------------------------------------------


def drawdown_episodes(equity: pd.Series, *, peak_tolerance: float = 1e-12) -> dict[str, Any]:
    """All drawdown episodes of a calendar-time equity curve.

    An episode starts at a running peak and ends when that peak is recovered
    (or at the end of the sample, and is then flagged as ``recovered: False``).
    """

    if equity.empty:
        return {
            "points": 0,
            "max_drawdown_pct": 0.0,
            "episodes": [],
            "time_underwater_pct": 0.0,
            "longest_underwater_hours": 0.0,
            "ulcer_index": 0.0,
        }
    values = equity.to_numpy(dtype=float)
    index = pd.DatetimeIndex(equity.index)
    peak = np.maximum.accumulate(values)
    drawdown = values / peak - 1.0
    underwater = drawdown < -peak_tolerance
    episodes: list[dict[str, Any]] = []
    start = None
    for position in range(len(values)):
        if underwater[position] and start is None:
            start = position
        elif not underwater[position] and start is not None:
            episodes.append(_episode_payload(values, drawdown, index, start, position - 1, position))
            start = None
    if start is not None:
        episodes.append(_episode_payload(values, drawdown, index, start, len(values) - 1, None))

    for episode in episodes:
        # Duration is measured from the running peak to the recovery (or to the
        # end of the sample when the episode is still open).  ``underwater_hours``
        # is the same calendar duration for readability; ``underwater_points``
        # counts the hourly grid points strictly below the peak.
        peak_time = pd.Timestamp(episode["peak_time"])
        end_time = pd.Timestamp(episode["recovery_time"]) if episode["recovery_time"] else index[-1]
        episode["duration_hours"] = float((end_time - peak_time).total_seconds() / 3600.0)
        episode["underwater_hours"] = episode["duration_hours"]

    deepest = min(episodes, key=lambda item: item["depth_pct"]) if episodes else None
    durations = [episode["duration_hours"] for episode in episodes]
    recovered = [episode["duration_hours"] for episode in episodes if episode["recovered"]]
    return {
        "points": int(len(values)),
        "start": index[0].isoformat(),
        "end": index[-1].isoformat(),
        "max_drawdown_pct": float(drawdown.min() * 100.0),
        "max_drawdown_episode": deepest,
        "episodes": episodes,
        "episode_count": int(len(episodes)),
        "episodes_by_depth": {
            f"at_least_{int(level * 100)}pct": int(sum(1 for item in episodes if item["depth_pct"] <= -100.0 * level))
            for level in DRAWDOWN_LEVELS
        },
        "time_underwater_pct": 100.0 * float(underwater.mean()),
        "longest_underwater_hours": float(max(durations)) if durations else 0.0,
        "median_recovery_hours": float(np.median(recovered)) if recovered else 0.0,
        "recovered_episodes": int(len(recovered)),
        "open_episodes": int(len(episodes) - len(recovered)),
        "ulcer_index": float(np.sqrt(np.mean((drawdown * 100.0) ** 2))),
    }


def _episode_payload(
    values: np.ndarray,
    drawdown: np.ndarray,
    index: pd.DatetimeIndex,
    start: int,
    end: int,
    recovery_position: int | None,
) -> dict[str, Any]:
    segment = drawdown[start : end + 1]
    trough_offset = int(np.argmin(segment))
    trough_position = start + trough_offset
    recovery = index[recovery_position] if recovery_position is not None else None
    return {
        "peak_time": index[start - 1].isoformat() if start > 0 else index[start].isoformat(),
        "peak_equity_usdt": float(values[start - 1]) if start > 0 else float(values[start]),
        "trough_time": index[trough_position].isoformat(),
        "trough_equity_usdt": float(values[trough_position]),
        "depth_pct": float(segment.min() * 100.0),
        "recovery_time": recovery.isoformat() if recovery is not None else None,
        "recovered": recovery is not None,
        "underwater_points": int(end - start + 1),
        "decline_hours": float((index[trough_position] - index[start]).total_seconds() / 3600.0),
    }


def drawdown_analysis(
    trades: pd.DataFrame,
    window_frame: pd.DataFrame,
    step: pd.Series,
    mark: pd.Series,
    *,
    initial_capital: float = DEFAULT_INITIAL_CAPITAL,
) -> dict[str, Any]:
    """Drawdown of the monetary equity path, exit-marked and mark-to-market."""

    exit_marked = drawdown_episodes(step)
    mark_to_market = drawdown_episodes(mark)
    trade_equity = np.concatenate([[float(initial_capital)], trades["equity_after_usdt"].to_numpy(dtype=float)])
    trade_level_dd = max_drawdown(trade_equity[1:], initial_capital=initial_capital)

    per_fold: list[dict[str, Any]] = []
    for fold, group in window_frame.groupby("fold", sort=True):
        group = group.sort_values("cutoff")
        fold_start = pd.Timestamp(group["cutoff"].iloc[0])
        fold_end = pd.Timestamp(group["exit_timestamp"].iloc[-1])
        fold_equity = float(group["equity_before_usdt"].iloc[0])
        fold_exit = pd.concat(
            [
                pd.Series([fold_equity], index=[fold_start]),
                pd.Series(group["equity_after_usdt"].to_numpy(dtype=float), index=pd.DatetimeIndex(group["exit_timestamp"])),
            ]
        )
        fold_mark = mark.loc[fold_start:fold_end]
        if fold_mark.empty:
            fold_mark = fold_exit
        else:
            fold_mark = pd.concat([pd.Series([fold_equity], index=[fold_start]), fold_mark])
        marked = drawdown_episodes(fold_exit)
        mtm = drawdown_episodes(fold_mark)
        per_fold.append(
            {
                "fold": int(fold),
                "windows": int(len(group)),
                "trades": int((group["action"] != ACTION_NO_TRADE).sum()),
                "net_pnl_usdt": float(group["pnl_usdt"].sum()),
                "exit_marked_max_drawdown_pct": marked["max_drawdown_pct"],
                "exit_marked_longest_underwater_hours": marked["longest_underwater_hours"],
                "mark_to_market_max_drawdown_pct": mtm["max_drawdown_pct"],
                "mark_to_market_longest_underwater_hours": mtm["longest_underwater_hours"],
            }
        )

    return {
        "schema_version": "cdl.cp08.momentum_stability.drawdown.v1",
        "policy": POLICY,
        "initial_capital": float(initial_capital),
        "conventions": {
            "exit_marked": "equity costante durante l'hold, aggiornata all'exit open (ledger del simulatore)",
            "mark_to_market": "valore di liquidazione a ogni chiusura oraria dentro l'hold, con fee e slippage di uscita",
            "episode": "da un picco alla sua recuperazione; se non recuperato entro fine campione è 'open'",
            "monetary": "drawdown calcolato sull'equity in USDT, non su rendimenti percentuali",
        },
        "exit_marked": exit_marked,
        "mark_to_market": mark_to_market,
        "delta_mtm_minus_exit_marked_pct": mark_to_market["max_drawdown_pct"] - exit_marked["max_drawdown_pct"],
        "trade_level_max_drawdown_pct": trade_level_dd,
        "per_fold": per_fold,
        "worst_5_episodes_mark_to_market": sorted(
            mark_to_market["episodes"], key=lambda item: item["depth_pct"]
        )[:5],
        "notes": [
            "Il drawdown mark-to-market è la metrica di rischio capitale raccomandata in CP08-01.",
            "La durata è in ore di calendario sull'intera griglia oraria, quindi non dipende dalla densità dei trade.",
            "Calmar-like e altre metriche di rischio-rendimento non sono usate come gate: restano descrittive.",
        ],
    }


# ---------------------------------------------------------------------------
# regime breakdown (cutoff-only features)
# ---------------------------------------------------------------------------


def regime_breakdown(window_frame: pd.DataFrame, *, initial_capital: float = DEFAULT_INITIAL_CAPITAL) -> pd.DataFrame:
    """Volatility / trend / direction buckets using cutoff-only features.

    Trend and volatility buckets are fitted on the **training windows of each
    fold** (terciles of ``momentum_168h`` and ``volatility_168h``).  No target
    and no OOS return enters the bucketing.
    """

    total_pnl = float(window_frame["pnl_usdt"].sum())
    rows: list[dict[str, Any]] = []
    groupings = {
        "trend_regime": ("trend", "trend_regime", TREND_BUCKETS),
        "vol_regime": ("volatility", "vol_regime", VOL_BUCKETS),
        "direction": ("direction", "direction", ("LONG", "SHORT")),
        "trend_x_vol": ("trend_x_volatility", "trend_x_vol", None),
    }
    frame = window_frame.copy()
    frame["direction"] = np.where(
        frame["action"] == ACTION_LONG, "LONG", np.where(frame["action"] == ACTION_SHORT, "SHORT", "NO_TRADE")
    )
    frame["trend_x_vol"] = frame["trend_regime"].astype(str) + "/" + frame["vol_regime"].astype(str)

    for _, (granularity, column, catalog) in groupings.items():
        buckets = list(catalog) if catalog else sorted(frame[column].unique())
        for bucket in buckets:
            subset = frame[frame[column] == bucket]
            if subset.empty:
                continue
            values = _metrics_from_rows(
                subset.sort_values("cutoff"), initial_capital=initial_capital, contiguous=False
            )
            rows.append(
                {
                    "granularity": granularity,
                    "bucket": str(bucket),
                    **values,
                    "share_of_net_pnl_pct": 100.0 * values["net_pnl_usdt"] / total_pnl if total_pnl else 0.0,
                    "avg_volatility_168h": float(subset["volatility_168h"].mean()),
                    "avg_momentum_168h": float(subset["momentum_168h"].mean()),
                    "avg_atr_pct": float(subset["atr_pct"].mean()),
                    "low_sample": bool(values["trades"] < MIN_BUCKET_TRADES),
                }
            )
        # Full-sample row for reference.
        values = _metrics_from_rows(
            frame.sort_values("cutoff"), initial_capital=initial_capital, contiguous=False
        )
        rows.append(
            {
                "granularity": granularity,
                "bucket": "ALL",
                **values,
                "share_of_net_pnl_pct": 100.0 * values["net_pnl_usdt"] / total_pnl if total_pnl else 0.0,
                "avg_volatility_168h": float(frame["volatility_168h"].mean()),
                "avg_momentum_168h": float(frame["momentum_168h"].mean()),
                "avg_atr_pct": float(frame["atr_pct"].mean()),
                "low_sample": False,
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# reconciliation with CP07 / CP08-01
# ---------------------------------------------------------------------------


def _compare(reference: float | None, recomputed: float | None, *, absolute_tolerance: float, label: str) -> dict[str, Any]:
    if reference is None or recomputed is None:
        return {"label": label, "reference": reference, "recomputed": recomputed, "match": False, "note": "missing"}
    delta = float(recomputed) - float(reference)
    return {
        "label": label,
        "reference": float(reference),
        "recomputed": float(recomputed),
        "abs_delta": abs(delta),
        "relative_delta": abs(delta) / abs(float(reference)) if float(reference) else None,
        "tolerance": float(absolute_tolerance),
        "match": bool(abs(delta) <= absolute_tolerance),
    }


def reconcile(
    paths: StabilityPaths,
    archive: Mapping[str, Any],
    prepared: Mapping[str, Any],
    trades: pd.DataFrame,
    periods: pd.DataFrame,
    concentration: Mapping[str, Any],
    drawdown: Mapping[str, Any],
    diagnostics: Mapping[str, Any],
    *,
    initial_capital: float = DEFAULT_INITIAL_CAPITAL,
) -> dict[str, Any]:
    """Reconcile every recomputed metric against CP07 and CP08-01."""

    dollars = prepared["dollars"]
    archive_summary = archive["summary"]["aggregate"][POLICY]["out_of_sample"]
    fold_metrics = archive["fold_metrics"]
    momentum_folds = fold_metrics[fold_metrics["policy"] == POLICY].sort_values("fold")
    fold_periods = periods[periods["granularity"] == "fold"].set_index("period")
    fold_drawdowns = {str(int(item["fold"])): item for item in drawdown["per_fold"]}

    checks: list[dict[str, Any]] = []
    checks.append(_compare(archive_summary["trades"], dollars["trades"], absolute_tolerance=0, label="cp07/aggregate/trades"))
    checks.append(
        _compare(
            archive_summary["total_return_pct"],
            100.0 * (dollars["final_equity"] / float(initial_capital) - 1.0),
            absolute_tolerance=1e-9,
            label="cp07/aggregate/total_return_pct",
        )
    )
    checks.append(_compare(archive_summary["max_drawdown_pct"], dollars["max_drawdown_pct"], absolute_tolerance=1e-9, label="cp07/aggregate/max_drawdown_pct"))
    checks.append(_compare(archive_summary["profit_factor"], dollars["profit_factor_pct"], absolute_tolerance=1e-9, label="cp07/aggregate/profit_factor_pct"))
    checks.append(_compare(archive_summary["win_rate_pct"], dollars["win_rate_trade_pct"], absolute_tolerance=1e-9, label="cp07/aggregate/win_rate_pct"))
    checks.append(_compare(float(archive_summary["no_trade"]), float(dollars["no_trade"]), absolute_tolerance=0, label="cp07/aggregate/no_trade"))
    checks.append(_compare(float(archive_summary["skipped_signals"]), float(dollars["skipped"]), absolute_tolerance=0, label="cp07/aggregate/skipped"))

    for row in momentum_folds.itertuples(index=False):
        fold_key = str(int(row.fold))
        period = fold_periods.loc[fold_key]
        compounded = float(period["compounded_return_pct"])
        fold_local_final = float(initial_capital) * (1.0 + compounded / 100.0)
        for field, recomputed, tolerance in (
            ("trades", float(period["trades"]), 0),
            ("total_return_pct", compounded, 1e-6),
            ("win_rate_pct", float(period["win_rate_window_pct"]), 1e-9),
            ("final_equity", fold_local_final, 1e-6),
            (
                "max_drawdown_pct",
                float(fold_drawdowns[fold_key]["exit_marked_max_drawdown_pct"]),
                1e-6,
            ),
        ):
            checks.append(
                _compare(
                    float(getattr(row, field)),
                    recomputed,
                    absolute_tolerance=tolerance,
                    label=f"cp07/fold-{fold_key}/{field}",
                )
            )

    # CP07 year and regime breakdowns are per-window compounds.
    by_year = {str(item["group"]): item for item in archive["summary"]["by_year"][POLICY]}
    for period, item in by_year.items():
        subset = periods[(periods["granularity"] == "year") & (periods["period"] == period)]
        if subset.empty:
            continue
        row = subset.iloc[0]
        checks.append(_compare(item["trades"], float(row["trades"]), absolute_tolerance=0, label=f"cp07/year-{period}/trades"))
        checks.append(
            _compare(item["total_return_pct"], float(row["compounded_return_pct"]), absolute_tolerance=1e-6, label=f"cp07/year-{period}/return_pct")
        )
        checks.append(
            _compare(item["win_rate_pct"], float(row["win_rate_window_pct"]), absolute_tolerance=1e-9, label=f"cp07/year-{period}/win_rate_window_pct")
        )
    by_regime = {str(item["group"]): item for item in archive["summary"]["by_regime"][POLICY]}
    trend_summary = regime_window_summary(archive)
    for regime, item in by_regime.items():
        recomputed = trend_summary.get(regime)
        if recomputed is None:
            continue
        checks.append(_compare(item["trades"], recomputed["trades"], absolute_tolerance=0, label=f"cp07/regime-{regime}/trades"))
        checks.append(
            _compare(item["total_return_pct"], recomputed["compounded_return_pct"], absolute_tolerance=1e-6, label=f"cp07/regime-{regime}/return_pct")
        )
        checks.append(
            _compare(item["win_rate_pct"], recomputed["win_rate_window_pct"], absolute_tolerance=1e-9, label=f"cp07/regime-{regime}/win_rate_window_pct")
        )

    # CP08-01 monetary metrics.
    audit_reference: dict[str, Any] = {}
    audit_metrics_path = paths.audit_dir / "metrics_comparison.csv"
    audit_summary_path = paths.audit_dir / "summary.json"
    if audit_metrics_path.is_file():
        metrics_frame = pd.read_csv(audit_metrics_path)
        row = metrics_frame[metrics_frame["policy"] == POLICY]
        if len(row):
            audit_reference = row.iloc[0].to_dict()
    if audit_summary_path.is_file():
        audit_summary = json.loads(audit_summary_path.read_text(encoding="utf-8"))
        calendar = audit_summary.get("calendar_metrics", {}).get(POLICY, {})
        robustness = audit_summary.get("robustness", {}).get(POLICY, {})
        audit_reference.update(
            {
                "calendar_max_drawdown_pct": calendar.get("step_max_drawdown_pct"),
                "calendar_mark_to_market_max_drawdown_pct": calendar.get("mark_to_market_max_drawdown_pct"),
                "concentration_leave_out": robustness.get("concentration", {}).get("leave_out", {}),
            }
        )

    if audit_reference:
        checks.append(_compare(audit_reference.get("profit_factor_usdt"), dollars["profit_factor_usdt"], absolute_tolerance=1e-9, label="cp08/pf_usdt"))
        checks.append(_compare(audit_reference.get("profit_factor_pct"), dollars["profit_factor_pct"], absolute_tolerance=1e-9, label="cp08/pf_pct"))
        checks.append(_compare(audit_reference.get("win_rate_trade_pct"), dollars["win_rate_trade_pct"], absolute_tolerance=1e-9, label="cp08/win_rate_trade_pct"))
        checks.append(_compare(audit_reference.get("win_rate_window_pct"), dollars["win_rate_window_pct"], absolute_tolerance=1e-9, label="cp08/win_rate_window_pct"))
        checks.append(_compare(audit_reference.get("final_equity"), dollars["final_equity"], absolute_tolerance=1e-6, label="cp08/final_equity"))
        checks.append(_compare(audit_reference.get("max_drawdown_pct"), dollars["max_drawdown_pct"], absolute_tolerance=1e-9, label="cp08/exit_marked_drawdown_pct"))
        checks.append(
            _compare(
                audit_reference.get("calendar_max_drawdown_pct"),
                drawdown["exit_marked"]["max_drawdown_pct"],
                absolute_tolerance=1e-6,
                label="cp08/calendar_exit_marked_drawdown_pct",
            )
        )
        checks.append(
            _compare(
                audit_reference.get("calendar_mark_to_market_max_drawdown_pct"),
                drawdown["mark_to_market"]["max_drawdown_pct"],
                absolute_tolerance=1e-6,
                label="cp08/calendar_mark_to_market_drawdown_pct",
            )
        )
        for key, value in (audit_reference.get("concentration_leave_out") or {}).items():
            recomputed = concentration["leave_top_k_recompounded_pct"].get(str(key))
            checks.append(_compare(value, recomputed, absolute_tolerance=1e-6, label=f"cp08/concentration/leave-{key}"))

    # CP08-01 manifest must reference the same CP07 decision hash we used.
    audit_manifest_path = paths.audit_dir / "manifest.json"
    digest_chain = None
    if audit_manifest_path.is_file():
        audit_manifest = json.loads(audit_manifest_path.read_text(encoding="utf-8"))
        digest_chain = audit_manifest.get("source_hashes", {}).get("trading_decisions")
    decisions_digest = digest(paths.trading_dir / "decisions.parquet")

    failed = [check for check in checks if not check["match"]]
    families: dict[str, dict[str, Any]] = {}
    for check in checks:
        family = check["label"].split("/")[0]
        entry = families.setdefault(family, {"checks": 0, "failed": 0, "max_abs_delta": 0.0})
        entry["checks"] += 1
        entry["failed"] += 0 if check["match"] else 1
        entry["max_abs_delta"] = max(entry["max_abs_delta"], float(check.get("abs_delta") or 0.0))
    return {
        "checks": checks,
        "checks_total": int(len(checks)),
        "checks_failed": int(len(failed)),
        "by_family": families,
        "max_abs_delta": float(max((check.get("abs_delta", 0.0) or 0.0) for check in checks)) if checks else 0.0,
        "all_match": bool(not failed),
        "failed_labels": [check["label"] for check in failed],
        "cp08_audit_hash_chain": {
            "audit_reference_decisions_sha256": digest_chain,
            "local_decisions_sha256": decisions_digest,
            "match": bool(digest_chain == decisions_digest) if digest_chain else None,
        },
        "diagnostics": {
            "contract": prepared["contract_diagnostics"],
            "reconstruction": prepared["reconstruction"],
            "window_frame": {
                key: value
                for key, value in diagnostics.items()
                if key
                in {
                    "windows",
                    "trades",
                    "no_trade",
                    "skipped",
                    "feature_cutoff_guard",
                    "fold_assignment_matches",
                    "fold_assignment_total",
                    "trend_regime_matches_archive",
                    "threshold_values_archived",
                    "pnl_attribution_error_usdt",
                }
            },
        },
    }


def regime_window_summary(archive: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Window-level aggregates per archived trend regime (CP07 ``by_regime``)."""

    decisions = archive["decisions"]
    momentum = decisions[decisions["policy"] == POLICY]
    output: dict[str, dict[str, Any]] = {}
    for regime, group in momentum.groupby(momentum["regime"].astype(str)):
        net = group["net_return_pct"].to_numpy(dtype=float)
        output[regime] = {
            "trades": int((group["action"] != ACTION_NO_TRADE).sum()),
            "compounded_return_pct": 100.0 * (float(np.prod(1.0 + net / 100.0)) - 1.0),
            "win_rate_window_pct": 100.0 * float((net > 0).mean()),
        }
    return output


# ---------------------------------------------------------------------------
# report assembly
# ---------------------------------------------------------------------------


def build_stability(
    paths: StabilityPaths = StabilityPaths(),
    *,
    initial_capital: float = DEFAULT_INITIAL_CAPITAL,
) -> dict[str, Any]:
    """Load the frozen archive, rebuild the analysis and reconcile everything."""

    source_hashes = _source_hashes(paths)
    archive = load_archive(
        AuditPaths(
            data_root=paths.data_root,
            snapshot_dir=paths.snapshot_dir,
            label_dir=paths.label_dir,
            regime_dir=paths.regime_dir,
            trading_dir=paths.trading_dir,
            output_dir=paths.audit_dir,
        )
    )
    prepared = prepare_archive(archive, initial_capital=initial_capital)
    window_frame, diagnostics = build_window_frame(archive, prepared, initial_capital=initial_capital)
    trades = trade_attribution(window_frame, initial_capital=initial_capital)
    periods = period_summary(window_frame, initial_capital=initial_capital)
    period_reconciliation = summary_reconciliation(periods, float(trades["pnl_usdt"].sum()))
    concentration = concentration_summary(trades, periods, prepared["ledger"], initial_capital=initial_capital)
    step, mark = calendar_equity_curves(prepared["ledger"], archive["market"], initial_capital=initial_capital)
    drawdown = drawdown_analysis(
        trades, window_frame, step, mark, initial_capital=initial_capital
    )
    streaks = streak_analysis(trades)
    regimes = regime_breakdown(window_frame, initial_capital=initial_capital)
    reconciliation = reconcile(
        paths,
        archive,
        prepared,
        trades,
        periods,
        concentration,
        drawdown,
        diagnostics,
        initial_capital=initial_capital,
    )
    reconciliation["period_attribution"] = period_reconciliation
    return {
        "schema_version": STABILITY_SCHEMA,
        "policy": POLICY,
        "generated_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "initial_capital": float(initial_capital),
        "source_hashes": source_hashes,
        "archive": archive,
        "prepared": prepared,
        "window_frame": window_frame,
        "trades": trades,
        "periods": periods,
        "concentration": concentration,
        "drawdown": drawdown,
        "streaks": streaks,
        "regimes": regimes,
        "equity": {"step": step, "mark_to_market": mark},
        "reconciliation": reconciliation,
        "diagnostics": diagnostics,
    }


def _equity_frame(report: Mapping[str, Any]) -> pd.DataFrame:
    frames = []
    for curve, series in report["equity"].items():
        if series.empty:
            continue
        values = series.to_numpy(dtype=float)
        peak = np.maximum.accumulate(values)
        frames.append(
            pd.DataFrame(
                {
                    "curve": curve,
                    "timestamp": series.index,
                    "equity": values,
                    "drawdown_pct": 100.0 * (values / peak - 1.0),
                }
            )
        )
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["curve", "timestamp", "equity", "drawdown_pct"]
    )


def write_artifacts(paths: StabilityPaths, report: Mapping[str, Any]) -> dict[str, Any]:
    """Write the additive archive; never overwrite and never touch the sources."""

    output = paths.output_dir
    if output.exists():
        raise FileExistsError(f"CP08-02 archive already exists; refusing to overwrite: {output}")
    before = report["source_hashes"]
    output.mkdir(parents=True)
    try:
        report["trades"].to_parquet(output / "trade_attribution.parquet", index=False)
        report["periods"].to_parquet(output / "period_summary.parquet", index=False)
        report["regimes"].to_parquet(output / "regime_breakdown.parquet", index=False)
        _equity_frame(report).to_parquet(output / "equity_curves.parquet", index=False)
        (output / "concentration_summary.json").write_text(
            json.dumps(_json_safe(report["concentration"]), indent=2) + "\n", encoding="utf-8"
        )
        (output / "drawdown_analysis.json").write_text(
            json.dumps(_json_safe(report["drawdown"]), indent=2) + "\n", encoding="utf-8"
        )
        (output / "streak_analysis.json").write_text(
            json.dumps(_json_safe(report["streaks"]), indent=2) + "\n", encoding="utf-8"
        )
        after = _source_hashes(paths)
        if before != after:
            raise RuntimeError("Source artifacts changed during the read-only analysis")
        outputs = {
            "trade_attribution": "trade_attribution.parquet",
            "period_summary": "period_summary.parquet",
            "regime_breakdown": "regime_breakdown.parquet",
            "equity_curves": "equity_curves.parquet",
            "concentration": "concentration_summary.json",
            "drawdown": "drawdown_analysis.json",
            "streaks": "streak_analysis.json",
        }
        manifest = {
            "schema_version": STABILITY_SCHEMA,
            "created_utc": pd.Timestamp.now(tz="UTC").isoformat(),
            "read_only": True,
            "analysis": "S02-CP08-02 momentum stability & profit concentration",
            "policy": POLICY,
            "policy_frozen": {
                "threshold": float(report["diagnostics"]["threshold_values_archived"][0])
                if report["diagnostics"]["threshold_values_archived"]
                else None,
                "threshold_reselected": False,
                "training_performed": False,
                "new_inference": False,
                "data_downloaded": False,
                "oos_threshold_optimisation": False,
            },
            "method": {
                "reconstruction": "simulator_integrity_audit.contract_frame + reconstruct_returns + replay_policy",
                "metrics": "monetary P&L (USDT) for capital-dependent metrics, percent per trade for behaviour",
                "periods": list(PERIOD_GRANULARITIES),
                "regime_buckets": "train-only terciles of cutoff-only momentum_168h and volatility_168h",
                "concentration": "top-k, HHI/Gini, temporal concentration, leave-top-k",
                "drawdown": "calendar-time step (exit-marked) and hourly mark-to-market",
            },
            "initial_capital": float(report["initial_capital"]),
            "source_hashes": before,
            "source_hashes_after": after,
            "source_hashes_unchanged": bool(before == after),
            "original_artifacts_modified": bool(before != after),
            "reconciliation": {
                "checks_total": report["reconciliation"]["checks_total"],
                "checks_failed": report["reconciliation"]["checks_failed"],
                "all_match": report["reconciliation"]["all_match"],
                "by_family": report["reconciliation"]["by_family"],
                "max_abs_delta": report["reconciliation"]["max_abs_delta"],
                "failed_labels": report["reconciliation"]["failed_labels"],
                "period_attribution": report["reconciliation"]["period_attribution"],
                "cp08_audit_hash_chain": report["reconciliation"]["cp08_audit_hash_chain"],
                "diagnostics": report["reconciliation"]["diagnostics"],
            },
            "headline": {
                "windows": report["diagnostics"]["windows"],
                "trades": report["diagnostics"]["trades"],
                "no_trade": report["diagnostics"]["no_trade"],
                "total_return_pct": report["concentration"]["total_return_pct"],
                "final_equity_usdt": report["concentration"]["final_equity_usdt"],
                "profit_factor_usdt": report["concentration"]["profit_factor_usdt"],
                "max_drawdown_exit_marked_pct": report["drawdown"]["exit_marked"]["max_drawdown_pct"],
                "max_drawdown_mark_to_market_pct": report["drawdown"]["mark_to_market"]["max_drawdown_pct"],
                "max_win_streak": report["streaks"]["max_win_streak"],
                "max_loss_streak": report["streaks"]["max_loss_streak"],
            },
            "outputs": {
                name: {"path": filename, "sha256": digest(output / filename)}
                for name, filename in outputs.items()
            },
        }
        (output / "manifest.json").write_text(
            json.dumps(_json_safe(manifest), indent=2, default=str) + "\n", encoding="utf-8"
        )
        return {"output_dir": str(output), "manifest": manifest}
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise


def run(
    paths: StabilityPaths = StabilityPaths(),
    *,
    initial_capital: float = DEFAULT_INITIAL_CAPITAL,
    write: bool = True,
) -> dict[str, Any]:
    report = build_stability(paths, initial_capital=initial_capital)
    if write:
        report["written"] = write_artifacts(paths, report)
    return report


def _printable(report: Mapping[str, Any]) -> dict[str, Any]:
    trades = report["trades"]
    concentration = report["concentration"]
    drawdown = report["drawdown"]
    return _json_safe(
        {
            "policy": POLICY,
            "initial_capital": report["initial_capital"],
            "windows": int(len(report["window_frame"])),
            "trades": int(len(trades)),
            "trades_long": int((trades["direction"] == "LONG").sum()),
            "trades_short": int((trades["direction"] == "SHORT").sum()),
            "total_return_pct": concentration["total_return_pct"],
            "final_equity_usdt": concentration["final_equity_usdt"],
            "profit_factor_usdt": concentration["profit_factor_usdt"],
            "top_k": {
                key: {
                    "pnl_usdt": value["pnl_usdt"],
                    "share_of_net_pnl_pct": value["share_of_net_pnl_pct"],
                    "return_pct_after_removal": value["return_pct_after_removal"],
                }
                for key, value in concentration["top_k"].items()
            },
            "temporal_concentration": concentration["temporal_concentration"],
            "max_drawdown_exit_marked_pct": drawdown["exit_marked"]["max_drawdown_pct"],
            "max_drawdown_mark_to_market_pct": drawdown["mark_to_market"]["max_drawdown_pct"],
            "longest_underwater_hours": drawdown["mark_to_market"]["longest_underwater_hours"],
            "time_underwater_pct": drawdown["mark_to_market"]["time_underwater_pct"],
            "max_win_streak": report["streaks"]["max_win_streak"],
            "max_loss_streak": report["streaks"]["max_loss_streak"],
            "runs_test_z": report["streaks"]["runs_test"]["z_score"],
            "reconciliation": {
                "checks_total": report["reconciliation"]["checks_total"],
                "checks_failed": report["reconciliation"]["checks_failed"],
                "max_abs_delta": report["reconciliation"]["max_abs_delta"],
                "failed_labels": report["reconciliation"]["failed_labels"],
            },
        }
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S02-CP08-02 momentum stability and profit concentration")
    parser.add_argument("--data-root", default=str(StabilityPaths.data_root))
    parser.add_argument("--snapshot-dir", default=str(StabilityPaths.snapshot_dir))
    parser.add_argument("--label-dataset-dir", default=str(StabilityPaths.label_dir))
    parser.add_argument("--regime-dataset-dir", default=str(StabilityPaths.regime_dir))
    parser.add_argument("--trading-dir", default=str(StabilityPaths.trading_dir))
    parser.add_argument("--audit-dir", default=str(StabilityPaths.audit_dir))
    parser.add_argument("--output-dir", default=str(StabilityPaths.output_dir))
    parser.add_argument("--capital", type=float, default=DEFAULT_INITIAL_CAPITAL)
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)
    paths = StabilityPaths(
        data_root=Path(args.data_root),
        snapshot_dir=Path(args.snapshot_dir),
        label_dir=Path(args.label_dataset_dir),
        regime_dir=Path(args.regime_dataset_dir),
        trading_dir=Path(args.trading_dir),
        audit_dir=Path(args.audit_dir),
        output_dir=Path(args.output_dir),
    )
    report = run(paths, initial_capital=args.capital, write=not args.no_write)
    payload = _printable(report)
    if "written" in report:
        payload["written"] = report["written"]["output_dir"]
        payload["manifest_sha256"] = digest(Path(report["written"]["output_dir"]) / "manifest.json")
    print(json.dumps(payload, indent=2, default=str))
    return 0 if report["reconciliation"]["all_match"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
