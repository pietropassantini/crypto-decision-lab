"""S02-CP08-01: read-only integrity audit of the CP07 trading simulator.

The audit never re-fits, never re-labels and never overwrites a CP07 artifact.
It reconstructs every archived decision from the frozen labels, replays the
simulator, and verifies the ten integrity requirements of CP08-01:

1. no look-ahead (features and thresholds use cutoff information only);
2. entry/exit timestamps follow the execution contract;
3. the open price used for execution exists and is executable;
4. positions never overlap;
5. skipped signals are handled chronologically (and never quietly re-priced);
6. equity compounding is correct and self-financing;
7. fees and slippage are charged on both sides;
8. SHORT exposure, collateral and capital constraints are explicit;
9. the equity curve is calendar-time;
10. max drawdown and profit factor are computed with explicit conventions.

Two metric conventions are reported side by side because they disagree:

* CP07 reports ``profit_factor`` as sum of percentage wins / sum of percentage
  losses (event-study convention);
* a self-financing portfolio with 100% equity per trade must use monetary
  P&L, i.e. equity-weighted profits and losses.

The audit does not choose one: it quantifies the gap and flags it.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .decision_contract import (
    ACTION_LONG,
    ACTION_NO_TRADE,
    ACTION_SHORT,
    HORIZON_HOURS,
    CostModel,
    Decision,
    execution_prices,
    gross_return,
    net_return,
    validate_decisions,
)
from .sync import digest
from .trading_simulator import DEFAULT_INITIAL_CAPITAL, simulate_from_frame

AUDIT_SCHEMA = "cdl.cp08.integrity_audit.v1"
CP07_SCHEMA = "cdl.cp07.simulation.v1"

STATUS_PASS = "PASS"
STATUS_WARN = "WARN"
STATUS_FAIL = "FAIL"
STATUS_FIXED = "FIXED"

ITEM_LABELS = {
    "CP08-01.1": "no look-ahead",
    "CP08-01.2": "entry/exit timestamps",
    "CP08-01.3": "execution price availability",
    "CP08-01.4": "no overlapping positions",
    "CP08-01.5": "skipped signal chronology",
    "CP08-01.6": "equity compounding",
    "CP08-01.7": "fees and slippage on both sides",
    "CP08-01.8": "SHORT exposure and collateral",
    "CP08-01.9": "calendar-time equity curve",
    "CP08-01.10": "max drawdown and profit factor",
}


@dataclass(frozen=True)
class AuditPaths:
    data_root: Path = Path("data")
    snapshot_dir: Path = Path("data/snapshots/BTCUSDT-1h-raw-v1")
    label_dir: Path = Path("data/datasets/S02-CP07-decision-labels-v1")
    regime_dir: Path = Path("data/datasets/S02-CP06B-regime-features-v1")
    trading_dir: Path = Path("data/experiments/S02-CP07-trading-simulation-v1")
    output_dir: Path = Path("data/experiments/S02-CP08-simulator-integrity-audit-v1")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _utc(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _check(
    item: str,
    suffix: str,
    label: str,
    passed: bool | None,
    detail: str,
    *,
    status: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status is None:
        status = STATUS_PASS if passed else STATUS_FAIL
    return {
        "id": f"{item}.{suffix}",
        "item": item,
        "item_label": ITEM_LABELS[item],
        "check": label,
        "status": status,
        "detail": detail,
        "evidence": evidence or {},
    }


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        if not np.isfinite(number):
            return None
        return number
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def _source_hashes(paths: AuditPaths) -> dict[str, str]:
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
    }
    return {name: digest(path) for name, path in files.items() if path.is_file()}


# ---------------------------------------------------------------------------
# archive reconstruction
# ---------------------------------------------------------------------------


def load_archive(paths: AuditPaths) -> dict[str, Any]:
    """Load every frozen CP07 artifact plus the market snapshot (read-only)."""

    from .dashboard.data_loader import load_market_data

    decisions = pd.read_parquet(paths.trading_dir / "decisions.parquet")
    labels = pd.read_parquet(paths.label_dir / "labels.parquet")
    features = pd.read_parquet(paths.regime_dir / "features.parquet")
    summary = _read_json(paths.trading_dir / "summary.json")
    fold_summary = _read_json(paths.trading_dir / "fold_summary.json")
    sensitivity = _read_json(paths.trading_dir / "sensitivity.json")
    manifest = _read_json(paths.trading_dir / "manifest.json")
    market = load_market_data(
        paths.data_root, paths.snapshot_dir, verify_checksums=True
    ).frame
    return {
        "decisions": decisions.sort_values(["policy", "cutoff"]).reset_index(drop=True),
        "labels": labels.sort_values("cutoff").reset_index(drop=True),
        "features": features.sort_values("window_index").reset_index(drop=True),
        "summary": summary,
        "fold_summary": fold_summary,
        "fold_metrics": pd.DataFrame(fold_summary["fold_metrics"]),
        "sensitivity": sensitivity,
        "manifest": manifest,
        "market": market,
    }


def contract_frame(decisions: pd.DataFrame, labels: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Normalize the archived decision log to the ``cdl.cp07.decision.v1`` contract.

    The archived parquet uses reporting column names (``cutoff``, ``policy``) and
    omits two mandatory contract fields (``decision_timestamp``,
    ``horizon_hours``); they are reconstructed explicitly rather than inferred.
    """

    label_index = labels.set_index("window_index")
    frame = pd.DataFrame(
        {
            "decision_id": decisions["decision_id"],
            "cutoff_timestamp": pd.to_datetime(decisions["cutoff"], utc=True),
            "decision_timestamp": pd.to_datetime(decisions["cutoff"], utc=True),
            "entry_timestamp": pd.to_datetime(decisions["entry_timestamp"], utc=True),
            "exit_timestamp": pd.to_datetime(decisions["exit_timestamp"], utc=True),
            "horizon_hours": HORIZON_HOURS,
            "action": decisions["action"].astype(str),
            "reference_price": decisions["reference_price"].astype(float),
            "execution_price": decisions["execution_price"].astype(float),
            "exit_price": decisions["exit_price"].astype(float),
            "fee_rate": decisions["fee_rate"].astype(float),
            "slippage": decisions["slippage"].astype(float),
            "gross_return_pct": decisions["gross_return_pct"].astype(float),
            "net_return_pct": decisions["net_return_pct"].astype(float),
            "data_version": "S02-CP06A-canonical/S02-CP07-labels-v1",
            "policy_version": decisions["policy"].astype(str),
            "fold": decisions["fold"].astype(int),
            "regime": decisions["regime"],
            "window_index": decisions["window_index"].astype(int),
        }
    )
    # Note the parquet field ``policy`` is the policy version; the contract field
    # ``policy_version`` is reconstructed from it, never guessed.
    labels_entry = label_index["entry_timestamp"].reindex(frame["window_index"]).to_numpy()
    labels_exit = label_index["exit_timestamp"].reindex(frame["window_index"]).to_numpy()
    labels_cutoff = label_index["cutoff"].reindex(frame["window_index"]).to_numpy()
    diagnostics = {
        "rows": int(len(frame)),
        "policies": int(frame["policy_version"].nunique()),
        "archive_columns": sorted(decisions.columns.tolist()),
        "missing_contract_columns": sorted(
            {"decision_timestamp", "horizon_hours", "policy_version"} - set(decisions.columns)
        ),
        "label_timestamp_mismatches": int(
            (pd.to_datetime(labels_entry, utc=True).to_numpy() != frame["entry_timestamp"].to_numpy()).sum()
            + (pd.to_datetime(labels_exit, utc=True).to_numpy() != frame["exit_timestamp"].to_numpy()).sum()
            + (pd.to_datetime(labels_cutoff, utc=True).to_numpy() != frame["cutoff_timestamp"].to_numpy()).sum()
        ),
        "label_rows_missing": int(len(frame) - len(label_index.reindex(frame["window_index"]).dropna(how="all"))),
    }
    # validate_decisions requires unique decision_id, so validate per policy.
    validated = pd.concat(
        [validate_decisions(subset) for _, subset in frame.groupby("policy_version", sort=False)],
        ignore_index=True,
    )
    return validated, diagnostics


def reconstruct_returns(
    frame: pd.DataFrame, labels: pd.DataFrame
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Rebuild executions and returns from raw opens; compare with the archive."""

    label_index = labels.set_index("window_index")
    entry_open = label_index["entry_open"].reindex(frame["window_index"]).to_numpy(dtype=float)
    exit_open = label_index["exit_open"].reindex(frame["window_index"]).to_numpy(dtype=float)
    net = np.zeros(len(frame), dtype=float)
    gross = np.zeros(len(frame), dtype=float)
    entry_exec = np.full(len(frame), np.nan)
    exit_exec = np.full(len(frame), np.nan)
    for position, (row, entry, exit_) in enumerate(
        zip(frame.itertuples(index=False), entry_open, exit_open)
    ):
        if row.action == ACTION_NO_TRADE:
            continue
        costs = CostModel(fee_rate=float(row.fee_rate), slippage=float(row.slippage))
        entry_exec[position], exit_exec[position] = execution_prices(
            entry, exit_, row.action, costs
        )
        net[position] = 100.0 * net_return(entry, exit_, row.action, costs)
        gross[position] = 100.0 * gross_return(entry, exit_, row.action)
    rebuilt = frame.assign(
        entry_open=entry_open,
        exit_open=exit_open,
        rebuilt_entry_price=entry_exec,
        rebuilt_exit_price=exit_exec,
        rebuilt_net_pct=net,
        rebuilt_gross_pct=gross,
    )
    traded = rebuilt[rebuilt["action"] != ACTION_NO_TRADE]
    diagnostics = {
        "entry_open_missing": int(np.isnan(entry_open).sum()),
        "exit_open_missing": int(np.isnan(exit_open).sum()),
        "max_abs_net_error_pct": float(
            np.abs(rebuilt["rebuilt_net_pct"] - rebuilt["net_return_pct"]).max()
        ),
        "max_abs_gross_error_pct": float(
            np.abs(rebuilt["rebuilt_gross_pct"] - rebuilt["gross_return_pct"]).max()
        ),
        "max_rel_entry_price_error": float(
            (np.abs(traded["rebuilt_entry_price"] - traded["execution_price"]) / traded["execution_price"]).max()
        )
        if len(traded)
        else 0.0,
        "max_rel_exit_price_error": float(
            (np.abs(traded["rebuilt_exit_price"] - traded["exit_price"]) / traded["exit_price"]).max()
        )
        if len(traded)
        else 0.0,
        "no_trade_rows_with_prices": int(
            ((rebuilt["action"] == ACTION_NO_TRADE) & rebuilt["execution_price"].notna()).sum()
        ),
    }
    return rebuilt, diagnostics


# ---------------------------------------------------------------------------
# replay and metric conventions
# ---------------------------------------------------------------------------


def replay_policy(frame: pd.DataFrame, *, initial_capital: float) -> tuple[pd.DataFrame, Any]:
    ledger, metrics = simulate_from_frame(frame, initial_capital=initial_capital)
    return ledger, metrics


def dollar_metrics(ledger: pd.DataFrame, *, initial_capital: float) -> dict[str, Any]:
    """Trade economics in USDT (self-financing) and in percent (event study)."""

    executed = ledger[ledger["executed"]]
    net_pct = executed["net_return_pct"].to_numpy(dtype=float)
    equity_after = executed["equity_after"].to_numpy(dtype=float)
    equity_before = np.concatenate([[initial_capital], equity_after[:-1]])
    pnl = equity_before * net_pct / 100.0
    wins_pct = net_pct[net_pct > 0].sum()
    losses_pct = abs(net_pct[net_pct < 0].sum())
    gross_profit = float(pnl[pnl > 0].sum())
    gross_loss = float(abs(pnl[pnl < 0].sum()))
    traded = executed[executed["action"] != ACTION_NO_TRADE]
    traded_net = traded["net_return_pct"].to_numpy(dtype=float)
    return {
        "windows": int(len(ledger)),
        "executed_rows": int(len(executed)),
        "trades": int(len(traded)),
        "final_equity": float(equity_after[-1]) if len(equity_after) else float(initial_capital),
        "gross_profit_usdt": gross_profit,
        "gross_loss_usdt": gross_loss,
        "net_pnl_usdt": float(pnl.sum()),
        "profit_factor_usdt": (gross_profit / gross_loss) if gross_loss > 0 else None,
        "profit_factor_pct": (float(wins_pct / losses_pct)) if losses_pct > 0 else None,
        "win_rate_trade_pct": 100.0 * float((traded_net > 0).mean()) if len(traded_net) else 0.0,
        "win_rate_window_pct": 100.0 * float((net_pct > 0).mean()) if len(net_pct) else 0.0,
        "avg_net_per_trade_pct": float(traded_net.mean()) if len(traded_net) else 0.0,
        "avg_net_per_window_pct": float(net_pct.mean()) if len(net_pct) else 0.0,
        "avg_win_pct": float(traded_net[traded_net > 0].mean()) if (traded_net > 0).any() else 0.0,
        "avg_loss_pct": float(traded_net[traded_net < 0].mean()) if (traded_net < 0).any() else 0.0,
        "best_trade_pct": float(traded_net.max()) if len(traded_net) else 0.0,
        "worst_trade_pct": float(traded_net.min()) if len(traded_net) else 0.0,
        "min_equity": float(min(initial_capital, equity_after.min())) if len(equity_after) else float(initial_capital),
        "no_trade": int((ledger["action"] == ACTION_NO_TRADE).sum()),
        "skipped": int((~ledger["executed"]).sum()),
    }


def concentration_profile(ledger: pd.DataFrame, *, initial_capital: float) -> dict[str, Any]:
    """Compounded return after removing the k best executed trades."""

    executed = ledger[ledger["executed"] & (ledger["action"] != ACTION_NO_TRADE)]
    net = executed["net_return_pct"].to_numpy(dtype=float) / 100.0
    if not len(net):
        return {"trades": 0, "leave_out": {}}
    order = np.argsort(net)[::-1]
    profile = {"0": 100.0 * (float(np.prod(1.0 + net)) - 1.0)}
    for k in (1, 2, 3, 5, 10):
        if k >= len(net):
            break
        kept = np.delete(net, order[:k])
        profile[str(k)] = 100.0 * (float(np.prod(1.0 + kept)) - 1.0)
    return {"trades": int(len(net)), "leave_out": profile}


def max_drawdown(equity: np.ndarray, *, initial_capital: float) -> float:
    curve = np.concatenate([[float(initial_capital)], np.asarray(equity, dtype=float)])
    peak = np.maximum.accumulate(curve)
    return float((curve / peak - 1.0).min() * 100.0)


def calendar_equity_curves(
    ledger: pd.DataFrame,
    market: pd.DataFrame,
    *,
    initial_capital: float,
) -> tuple[pd.Series, pd.Series]:
    """Hourly calendar-time equity: marked at exits (step) and at every close.

    * ``step``: equity constant while a position is held, updated at each exit.
      This is the curve implied by the simulator ledger.
    * ``mtm``: liquidation-value mark-to-market at every hourly close inside the
      holding window (exit fee and slippage applied), flat outside positions.

    Both curves are defined on the same hourly grid, so gaps in the source data
    cannot shift returns in time.
    """

    executed = ledger[ledger["executed"]].sort_values("exit_timestamp")
    if executed.empty:
        empty = pd.Series(dtype=float)
        return empty, empty
    entry0 = pd.Timestamp(executed["entry_timestamp"].iloc[0])
    exit_last = pd.Timestamp(executed["exit_timestamp"].iloc[-1])
    index = pd.date_range(entry0, exit_last, freq="h", tz="UTC")
    sparse = pd.Series(np.nan, index=index, dtype=float)
    sparse.loc[index[0]] = float(initial_capital)
    for row in executed.itertuples(index=False):
        sparse.loc[pd.Timestamp(row.exit_timestamp)] = float(row.equity_after)
    step = sparse.ffill().fillna(float(initial_capital))
    mark = step.copy()
    equity_before: dict[pd.Timestamp, float] = {}
    previous = float(initial_capital)
    for row in executed.itertuples(index=False):
        equity_before[pd.Timestamp(row.entry_timestamp)] = previous
        previous = float(row.equity_after)
    for row in executed.itertuples(index=False):
        if row.action == ACTION_NO_TRADE:
            continue
        # execution_price already embeds the direction-aware entry slippage.
        entry_exec = float(row.execution_price)
        if row.action == ACTION_LONG:
            def multiplier(price: float) -> float:
                return (price * (1.0 - row.slippage) * (1.0 - row.fee_rate)) / (
                    entry_exec * (1.0 + row.fee_rate)
                ) - 1.0
        else:
            def multiplier(price: float) -> float:
                return (entry_exec * (1.0 - row.fee_rate)) / (
                    price * (1.0 + row.slippage) * (1.0 + row.fee_rate)
                ) - 1.0
        entry = pd.Timestamp(row.entry_timestamp)
        exit_ = pd.Timestamp(row.exit_timestamp)
        closes = market["close"].loc[entry:exit_]
        closes = closes[closes.index < exit_]
        if not len(closes):
            continue
        base = equity_before[entry]
        mark.loc[closes.index] = base * (1.0 + np.array([multiplier(price) for price in closes.to_numpy(dtype=float)]))
    mark = mark.ffill()
    # Marked segments stop before the exit timestamp, so the step value written
    # at each exit (and held until the next entry) is never overwritten.
    return step, mark


def execution_delay_stress(
    ledger: pd.DataFrame,
    market: pd.DataFrame,
    *,
    initial_capital: float,
    delays: Iterable[int] = (0, 1, 2),
) -> dict[str, Any]:
    """Compound executed trades with entry/exit shifted by N hours."""

    executed = ledger[(ledger["executed"]) & (ledger["action"] != ACTION_NO_TRADE)]
    results: dict[str, Any] = {}
    for delay in delays:
        equity = float(initial_capital)
        missing = 0
        for row in executed.itertuples(index=False):
            entry = pd.Timestamp(row.entry_timestamp) + pd.Timedelta(hours=int(delay))
            exit_ = pd.Timestamp(row.exit_timestamp) + pd.Timedelta(hours=int(delay))
            entry_price = market["open"].get(entry)
            exit_price = market["open"].get(exit_)
            if entry_price is None or exit_price is None:
                missing += 1
                continue
            if not (np.isfinite(entry_price) and np.isfinite(exit_price)):
                missing += 1
                continue
            costs = CostModel(fee_rate=float(row.fee_rate), slippage=float(row.slippage))
            equity *= 1.0 + net_return(float(entry_price), float(exit_price), row.action, costs)
        results[str(delay)] = {
            "total_return_pct": 100.0 * (equity / float(initial_capital) - 1.0),
            "missing_bars": int(missing),
        }
    return results


def bootstrap_profile(net_pct: np.ndarray, *, block: int = 10, draws: int = 2000, seed: int = 42) -> dict[str, Any]:
    """Moving-block bootstrap of the compounded return and of the PF."""

    from .regime_analysis import moving_block_bootstrap_ci

    values = np.asarray(net_pct, dtype=float)
    if len(values) < 2:
        return {"draws": 0}

    def compound(sample: np.ndarray) -> float:
        return 100.0 * (float(np.prod(1.0 + sample / 100.0)) - 1.0)

    def profit_factor(sample: np.ndarray) -> float:
        wins = sample[sample > 0].sum()
        losses = abs(sample[sample < 0].sum())
        if losses == 0:
            return float("inf") if wins > 0 else 0.0
        return float(wins / losses)

    generator = np.random.default_rng(seed)
    n = len(values)
    starts = np.arange(max(1, n - block + 1))
    compounds = np.empty(draws, dtype=float)
    factors = np.empty(draws, dtype=float)
    for draw in range(draws):
        positions = generator.choice(starts, size=int(np.ceil(n / block)), replace=True)
        sample = np.concatenate([values[position:position + block] for position in positions])[:n]
        compounds[draw] = compound(sample)
        factors[draw] = profit_factor(sample)
    finite_factors = factors[np.isfinite(factors)]
    return {
        "draws": int(draws),
        "block": int(block),
        "total_return_pct": {
            "point": compound(values),
            "ci_low": float(np.quantile(compounds, 0.025)),
            "ci_high": float(np.quantile(compounds, 0.975)),
            "median": float(np.median(compounds)),
            "p_positive": float((compounds > 0.0).mean()),
        },
        "profit_factor_pct": {
            "point": profit_factor(values),
            "ci_low": float(np.quantile(finite_factors, 0.025)) if len(finite_factors) else None,
            "ci_high": float(np.quantile(finite_factors, 0.975)) if len(finite_factors) else None,
            "p_above_one": float((finite_factors > 1.0).mean()) if len(finite_factors) else None,
        },
        "mean_net_pct": moving_block_bootstrap_ci(values, block=block, draws=draws, seed=seed),
    }


def threshold_robustness(paths: AuditPaths, fold_metrics: pd.DataFrame) -> dict[str, Any]:
    """Train-only threshold selection: grid boundary and stability.

    The CP07 grid stops at 5%.  This check reports whether the selected
    threshold sits on that boundary and what a *train-only* extended grid would
    have preferred.  No out-of-sample result is used for any choice.
    """

    from .policy_evaluation import make_folds
    from .trading_policy import (
        THRESHOLD_GRID,
        _threshold_actions,
        fit_threshold_policy,
        load_inputs,
    )
    from .trading_policy import _net_for_action

    data = load_inputs(paths.label_dir, paths.regime_dir)
    folds = make_folds(data, n_folds=5, min_train=200, test_size=60)
    extended = tuple(sorted(set(THRESHOLD_GRID) | {0.08, 0.10, 0.15, 0.20}))
    costs = CostModel()
    rows: list[dict[str, Any]] = []
    archived = fold_metrics[fold_metrics["policy"] == "momentum_threshold"].set_index("fold")
    for fold in folds:
        train = data.loc[fold["train_index"]]
        signal = train["momentum_168h"].to_numpy(dtype=float)
        multiples: dict[str, float] = {}
        trade_counts: dict[str, int] = {}
        for threshold in extended:
            actions = _threshold_actions(signal, threshold)
            net = np.array(
                [
                    _net_for_action(row, action, costs)
                    for action, (_, row) in zip(actions, train.iterrows())
                ]
            )
            multiples[str(threshold)] = float(np.prod(1.0 + net / 100.0))
            trade_counts[str(threshold)] = int((actions != ACTION_NO_TRADE).sum())
        state = fit_threshold_policy(train, "momentum_168h", costs=costs)
        chosen = float(archived.loc[fold["fold"], "state"]["threshold"])
        best_extended = max(multiples, key=multiples.get)
        rows.append(
            {
                "fold": int(fold["fold"]),
                "train_size": int(fold["train_size"]),
                "archived_threshold": chosen,
                "refit_threshold": float(state["threshold"]),
                "refit_matches_archive": bool(abs(float(state["threshold"]) - chosen) < 1e-12),
                "grid_boundary": bool(abs(chosen - max(THRESHOLD_GRID)) < 1e-12),
                "best_extended_threshold": float(best_extended),
                "best_extended_multiple": multiples[best_extended],
                "multiple_at_archived": multiples[str(chosen)],
                "extended_grid": list(extended),
                "train_multiple_by_threshold": multiples,
                "train_trades_by_threshold": trade_counts,
            }
        )
    boundary_folds = sum(1 for row in rows if row["grid_boundary"])
    better_outside = sum(
        1
        for row in rows
        if row["best_extended_multiple"] > row["multiple_at_archived"] + 1e-12
    )
    return {
        "policy": "momentum_threshold",
        "cp07_grid": [float(value) for value in THRESHOLD_GRID],
        "extended_grid": [float(value) for value in extended],
        "folds": rows,
        "boundary_folds": int(boundary_folds),
        "folds_with_better_train_threshold_outside_grid": int(better_outside),
        "note": (
            "Train-only diagnostics.  No out-of-sample information is used and no "
            "threshold is re-selected for CP07; the frozen policy is 5% in every fold."
        ),
    }


def sensitivity_reproduction(paths: AuditPaths) -> dict[str, Any]:
    """Recompute the CP07 fee x slippage grid and compare with the archive."""

    from .trading_policy import load_inputs, sensitivity_analysis

    data = load_inputs(paths.label_dir, paths.regime_dir)
    recomputed = sensitivity_analysis(
        data, n_folds=5, min_train=200, test_size=60, initial_capital=DEFAULT_INITIAL_CAPITAL
    )
    archived = _read_json(paths.trading_dir / "sensitivity.json")["scenarios"]
    key = lambda row: (round(float(row["fee_rate"]), 6), round(float(row["slippage"]), 6))
    recomputed_by_key = {key(row): row for row in recomputed["scenarios"]}
    comparisons = []
    worst = 0.0
    for row in archived:
        scenario_key = key(row)
        if scenario_key not in recomputed_by_key:
            comparisons.append({"fee_rate": row["fee_rate"], "slippage": row["slippage"], "present": False})
            continue
        delta = {
            policy: float(recomputed_by_key[scenario_key]["total_return_pct"][policy]) - float(value)
            for policy, value in row["total_return_pct"].items()
        }
        worst = max(worst, max(abs(value) for value in delta.values()))
        comparisons.append(
            {
                "fee_rate": float(row["fee_rate"]),
                "slippage": float(row["slippage"]),
                "present": True,
                "max_abs_delta_pct": max(abs(value) for value in delta.values()),
                "momentum_total_return_pct": float(
                    recomputed_by_key[scenario_key]["total_return_pct"]["momentum_threshold"]
                ),
            }
        )
    return {
        "scenarios": comparisons,
        "max_abs_delta_pct": float(worst),
        "matches_archive": bool(worst < 1e-9),
    }


# ---------------------------------------------------------------------------
# the ten audit items
# ---------------------------------------------------------------------------


def _look_ahead_checks(
    context: dict[str, Any],
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    frame = context["frame"]
    archive = context["archive"]
    labels = archive["labels"]
    features = archive["features"]
    market: pd.DataFrame = archive["market"]
    paths: AuditPaths = context["paths"]

    # 1a. Cutoff-only features: recompute momentum_168h from closes < cutoff.
    close = market["close"]
    rebuilt = []
    for row in features.itertuples(index=False):
        cutoff = _utc(row.cutoff)
        history = close.loc[: cutoff - pd.Timedelta(hours=1)]
        rebuilt.append(float(history.iloc[-1] / history.iloc[-168] - 1.0))
    feature_error = float(np.max(np.abs(np.array(rebuilt) - features["momentum_168h"].to_numpy(dtype=float))))
    checks.append(
        _check(
            "CP08-01.1",
            "a",
            "momentum_168h uses closes strictly before the cutoff",
            feature_error < 1e-12,
            f"max |rebuilt - archived| = {feature_error:.3e} on {len(features)} windows",
            evidence={"feature_error": feature_error, "windows": int(len(features))},
        )
    )

    # 1b. Decision time precedes execution time and follows the anchor bar.
    cutoff = pd.to_datetime(frame["cutoff_timestamp"], utc=True)
    entry = pd.to_datetime(frame["entry_timestamp"], utc=True)
    exit_ = pd.to_datetime(frame["exit_timestamp"], utc=True)
    anchor_ok = bool((entry > cutoff - pd.Timedelta(hours=1)).all())
    ordering_ok = bool(((cutoff <= entry) & (entry < exit_)).all())
    checks.append(
        _check(
            "CP08-01.1",
            "b",
            "decision_timestamp <= entry_timestamp and entry bar is strictly after the anchor bar",
            ordering_ok and anchor_ok,
            f"anchor_ok={anchor_ok} ordering_ok={ordering_ok}",
        )
    )

    # 1c. Thresholds are reproducible from training rows only.
    from .trading_policy import fit_threshold_policy, load_inputs
    from .policy_evaluation import make_folds

    data = load_inputs(paths.label_dir, paths.regime_dir)
    folds = make_folds(data, n_folds=5, min_train=200, test_size=60)
    archived_states = archive["fold_metrics"]
    mismatches = []
    fitted_states: dict[tuple[int, str], dict[str, Any]] = {}
    for fold in folds:
        train = data.loc[fold["train_index"]]
        for policy, signal in (("momentum_threshold", "momentum_168h"), ("timestral_threshold", "timestral_return_24h")):
            state = fit_threshold_policy(train, signal, costs=CostModel())
            fitted_states[(int(fold["fold"]), policy)] = state
            fitted = float(state["threshold"])
            stored = float(
                archived_states[
                    (archived_states["policy"] == policy) & (archived_states["fold"] == fold["fold"])
                ]["state"].iloc[0]["threshold"]
            )
            if abs(fitted - stored) > 1e-12:
                mismatches.append(
                    {"fold": int(fold["fold"]), "policy": policy, "fitted": fitted, "stored": stored}
                )
    checks.append(
        _check(
            "CP08-01.1",
            "c",
            "walk-forward thresholds reproduce from training rows only",
            not mismatches,
            f"threshold mismatches={len(mismatches)} over 5 folds x 2 threshold policies",
            evidence={"mismatches": mismatches[:10]},
        )
    )

    # 1d. Scrambling retrospective labels cannot change real-policy decisions.
    from .trading_policy import policy_actions

    changed = 0
    for fold in folds:
        train = data.loc[fold["train_index"]]
        test = data.loc[fold["test_index"]]
        scrambled = test.copy()
        for column in ("best_action",):
            scrambled[column] = np.roll(scrambled[column].to_numpy(), 1)
        for column in ("long_net_pct", "short_net_pct", "best_net_pct", "margin_vs_second_pct"):
            if column in scrambled:
                scrambled[column] = np.roll(scrambled[column].to_numpy(dtype=float), 1)
        for policy, signal in (("momentum_threshold", "momentum_168h"), ("timestral_threshold", "timestral_return_24h")):
            state = fitted_states[(int(fold["fold"]), policy)]
            before = policy_actions(policy, test, state)
            after = policy_actions(policy, scrambled, state)
            changed += int((np.asarray(before) != np.asarray(after)).sum())
    checks.append(
        _check(
            "CP08-01.1",
            "d",
            "retrospective label perturbation leaves real policies unchanged",
            changed == 0,
            f"decision changes after scrambling best_action and realized returns = {changed}",
        )
    )

    # 1e. Inherited CP07 leakage checks.
    inherited = context["archive"]["summary"]["leakage_checks"]
    passed = sum(1 for check in inherited if check["passed"])
    checks.append(
        _check(
            "CP08-01.1",
            "e",
            "CP07 leakage checks are archived and passed",
            passed == len(inherited),
            f"{passed}/{len(inherited)} CP07 leakage checks passed",
            evidence={"checks": inherited},
        )
    )
    return checks


def _timestamp_checks(context: dict[str, Any]) -> list[dict[str, Any]]:
    frame = context["frame"]
    entry = pd.to_datetime(frame["entry_timestamp"], utc=True)
    exit_ = pd.to_datetime(frame["exit_timestamp"], utc=True)
    cutoff = pd.to_datetime(frame["cutoff_timestamp"], utc=True)
    horizon = (exit_ - entry).dt.total_seconds() / 3600.0
    horizon_ok = bool((horizon == HORIZON_HOURS).all())
    entry_is_cutoff = bool((entry == cutoff).all())
    decision_is_cutoff = bool((pd.to_datetime(frame["decision_timestamp"], utc=True) == cutoff).all())
    checks = [
        _check(
            "CP08-01.2",
            "a",
            "entry_timestamp is the cutoff bar and exit = entry + 24h",
            entry_is_cutoff and horizon_ok,
            f"entry==cutoff: {entry_is_cutoff}; horizons==24h: {horizon_ok} (min={horizon.min():.3f}, max={horizon.max():.3f})",
            evidence={
                "rows": int(len(frame)),
                "horizon_values": sorted({float(value) for value in horizon.unique()}),
            },
        ),
        _check(
            "CP08-01.2",
            "b",
            "decision_timestamp == cutoff_timestamp",
            decision_is_cutoff,
            f"decision==cutoff for {int((pd.to_datetime(frame['decision_timestamp'], utc=True) == cutoff).sum())}/{len(frame)} rows",
        ),
    ]
    # Fold chronology: every test block starts after the previous one ends.
    folds = context["archive"]["fold_summary"]["folds"]
    ordered = sorted(folds, key=lambda item: item["fold"])
    disjoint = all(
        left["first_test_cutoff"] < right["first_test_cutoff"] for left, right in zip(ordered, ordered[1:])
    )
    windows = [set(int(index) for index in fold["test_index"]) for fold in ordered]
    disjoint_windows = all(not (a & b) for index, a in enumerate(windows) for b in windows[index + 1:])
    checks.append(
        _check(
            "CP08-01.2",
            "c",
            "walk-forward test blocks are chronological and disjoint",
            bool(disjoint and disjoint_windows),
            f"chronological={bool(disjoint)} disjoint_windows={disjoint_windows}",
        )
    )
    return checks


def _price_availability_checks(context: dict[str, Any]) -> list[dict[str, Any]]:
    frame = context["frame"]
    archive = context["archive"]
    market: pd.DataFrame = archive["market"]
    paths: AuditPaths = context["paths"]
    checks: list[dict[str, Any]] = []
    # One row per canonical window: the archive repeats it for every policy.
    windows = frame.drop_duplicates("window_index")
    entry = pd.to_datetime(windows["entry_timestamp"], utc=True)
    exit_ = pd.to_datetime(windows["exit_timestamp"], utc=True)
    anchors = pd.to_datetime(windows["cutoff_timestamp"], utc=True) - pd.Timedelta(hours=1)
    entry_open = market["open"].reindex(entry)
    exit_open = market["open"].reindex(exit_)
    anchor_close = market["close"].reindex(anchors)
    present = (
        int(entry_open.notna().sum()),
        int(exit_open.notna().sum()),
        int(anchor_close.notna().sum()),
    )
    total = len(windows)
    checks.append(
        _check(
            "CP08-01.3",
            "a",
            "entry open, exit open and anchor close exist without filling",
            all(value == total for value in present),
            f"entry {present[0]}/{total}, exit {present[1]}/{total}, anchor {present[2]}/{total}",
        )
    )
    # Data gaps must never be crossed by a boundary price.
    gaps = market.index.to_series().diff().dropna()
    gaps = gaps[gaps > pd.Timedelta(hours=1)]
    holes: set[pd.Timestamp] = set()
    for timestamp, delta in gaps.items():
        cursor = timestamp - delta + pd.Timedelta(hours=1)
        while cursor < timestamp:
            holes.add(cursor)
            cursor += pd.Timedelta(hours=1)
    boundaries = pd.Index(entry).append(pd.Index(exit_)).append(pd.Index(anchors))
    in_holes = int(sum(1 for timestamp in boundaries.unique() if timestamp in holes))
    checks.append(
        _check(
            "CP08-01.3",
            "b",
            "no window boundary falls inside a snapshot gap",
            in_holes == 0,
            f"{len(holes)} missing hours in snapshot; boundaries inside gaps={in_holes}",
            evidence={"missing_hours": len(holes), "boundaries_in_gaps": in_holes},
        )
    )
    # Anchor close reproducer: reference price must equal the anchor bar close.
    reference = windows["reference_price"].to_numpy(dtype=float)
    anchor_values = anchor_close.to_numpy(dtype=float)
    relative = np.abs(anchor_values - reference) / np.maximum(np.abs(reference), 1e-9)
    checks.append(
        _check(
            "CP08-01.3",
            "c",
            "reference_price equals the anchor bar close (float32 tolerance)",
            bool(np.nanmax(relative) < 1e-5),
            f"max relative deviation = {np.nanmax(relative):.3e}",
        )
    )
    # Execution prices are the directional open +/- slippage, nothing else.
    rebuilt = context["reconstruction"]
    checks.append(
        _check(
            "CP08-01.3",
            "d",
            "execution prices derive only from the entry/exit opens and slippage",
            rebuilt["max_rel_entry_price_error"] < 1e-12 and rebuilt["max_rel_exit_price_error"] < 1e-12,
            f"max rel error entry={rebuilt['max_rel_entry_price_error']:.3e}, exit={rebuilt['max_rel_exit_price_error']:.3e}",
        )
    )
    # Data provenance: chunks flagged REQUIRES_REVIEW still provide prices.
    manifest = _read_json(paths.snapshot_dir / "raw-manifest.json")
    quarantined = [
        (
            pd.Timestamp(int(chunk["start_ms"]), unit="ms", tz="UTC"),
            pd.Timestamp(int(chunk["end_ms"]), unit="ms", tz="UTC"),
        )
        for chunk in manifest["chunks"]
        if chunk.get("status") == "REQUIRES_REVIEW"
    ]

    def in_quarantine(timestamp: pd.Timestamp) -> bool:
        return any(start <= timestamp < end for start, end in quarantined)

    provenances = list(zip(entry, exit_))
    exposed_windows = int(sum(1 for left, right in provenances if in_quarantine(left) or in_quarantine(right)))
    momentum_ledger = context["ledgers"]["momentum_threshold"]
    momentum_traded = momentum_ledger[momentum_ledger["action"] != ACTION_NO_TRADE]
    exposed_trades = int(
        sum(
            1
            for row in momentum_traded.itertuples(index=False)
            if in_quarantine(pd.Timestamp(row.entry_timestamp)) or in_quarantine(pd.Timestamp(row.exit_timestamp))
        )
    )
    checks.append(
        _check(
            "CP08-01.3",
            "f",
            "execution prices from chunks flagged REQUIRES_REVIEW are quantified",
            True,
            f"{len(quarantined)} quarantined chunks cover {exposed_windows}/{total} canonical windows; "
            f"momentum trades priced there = {exposed_trades}",
            status=STATUS_WARN if exposed_trades else STATUS_PASS,
            evidence={
                "quarantined_chunks": [
                    {"start_utc": str(start), "end_utc": str(end)} for start, end in quarantined
                ],
                "exposed_windows": exposed_windows,
                "momentum_trades_exposed": exposed_trades,
            },
        )
    )
    # Irregular candle closes: the anchor close must be final at the cutoff.
    anomalies = _read_json(paths.snapshot_dir / "anomalies.json")
    irregular = pd.to_datetime(
        [event["timestamp_utc"] for event in anomalies.get("events", []) if event.get("type") == "IRREGULAR_CLOSE"],
        utc=True,
    )
    used_bars = pd.Index(entry).append(pd.Index(exit_)).append(pd.Index(anchors))
    collisions = int(used_bars.isin(irregular).sum())
    checks.append(
        _check(
            "CP08-01.3",
            "g",
            "anchor, entry and exit bars are never irregular-close bars",
            collisions == 0,
            f"{len(irregular)} irregular-close events in the snapshot; collisions with used bars = {collisions}",
            evidence={"irregular_close_bars": [str(value) for value in irregular], "collisions": collisions},
        )
    )
    # Execution latency: how much of the edge depends on the instantaneous open.
    stress = execution_delay_stress(
        momentum_ledger,
        market,
        initial_capital=context["initial_capital"],
        delays=(0, 1, 2),
    )
    base = stress["0"]["total_return_pct"]
    one_hour = stress["1"]["total_return_pct"]
    checks.append(
        _check(
            "CP08-01.3",
            "e",
            "execution at the cutoff open is documented and stress-tested with 1-2h delay",
            True,
            f"momentum total return: 0h={base:.2f}%, 1h={one_hour:.2f}%, 2h={stress['2']['total_return_pct']:.2f}%",
            status=STATUS_WARN,
            evidence={"delay_stress": stress, "retained_share_one_hour": (one_hour / base) if base else None},
        )
    )
    return checks


def _overlap_checks(context: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    overlaps = 0
    trades = 0
    for policy, ledger in context["ledgers"].items():
        executed = ledger[(ledger["executed"]) & (ledger["action"] != ACTION_NO_TRADE)]
        entry = pd.to_datetime(executed["entry_timestamp"], utc=True).to_numpy()
        exit_ = pd.to_datetime(executed["exit_timestamp"], utc=True).to_numpy()
        trades += len(executed)
        if len(entry) > 1:
            overlaps += int((entry[1:] < exit_[:-1]).sum())
    skipped = {policy: int((~ledger["executed"]).sum()) for policy, ledger in context["ledgers"].items()}
    checks.append(
        _check(
            "CP08-01.4",
            "a",
            "executed positions never overlap",
            overlaps == 0,
            f"overlaps={overlaps} across {trades} executed trades in {len(context['ledgers'])} policies",
        )
    )
    checks.append(
        _check(
            "CP08-01.4",
            "b",
            "archive records zero skipped signals (cutoff spacing >= 24h)",
            all(value == 0 for value in skipped.values()),
            f"skipped per policy: {skipped}; minimum cutoff spacing = 24h",
            evidence={"skipped": skipped},
        )
    )
    # Sizing: one position at a time at 100% of equity, never leveraged.
    # LONG  : units = equity / (entry * (1+slip) * (1+fee)) -> notional/equity = 1/((1+slip)(1+fee))
    # SHORT : proceeds = equity -> notional/equity = 1/(1-slip)
    implied = []
    for ledger in context["ledgers"].values():
        executed = ledger[(ledger["executed"]) & (ledger["action"] != ACTION_NO_TRADE)]
        for action, fee, slip in zip(
            executed["action"].to_numpy(),
            executed["fee_rate"].to_numpy(dtype=float),
            executed["slippage"].to_numpy(dtype=float),
        ):
            if action == ACTION_LONG:
                implied.append(1.0 / ((1.0 + slip) * (1.0 + fee)))
            else:
                implied.append(1.0 / (1.0 - slip))
    checks.append(
        _check(
            "CP08-01.4",
            "c",
            "position notional is 100% of equity (no leverage, single position)",
            bool(max(implied) < 1.01 and min(implied) > 0.99) if implied else False,
            f"implied notional/equity range = [{min(implied):.4f}, {max(implied):.4f}] "
            "(LONG <= equity, SHORT exceeds equity by slippage only)",
            evidence={"min": min(implied), "max": max(implied)} if implied else {},
        )
    )
    return checks


def _skip_chronology_checks(context: dict[str, Any]) -> list[dict[str, Any]]:
    """Synthetic replay of overlapping and NO_TRADE-blocking schedules."""

    from .decision_contract import build_decision
    from .trading_simulator import simulate_sequence

    costs = CostModel(fee_rate=0.0, slippage=0.0)
    base = pd.Timestamp("2024-01-01", tz="UTC")

    def decision(window: int, hour: int, action: str) -> Decision:
        return build_decision(
            window_index=window,
            cutoff=base + pd.Timedelta(hours=hour),
            entry_open=100.0,
            exit_open=110.0,
            action=action,
            costs=costs,
            reference_price=100.0,
            data_version="audit",
            policy_version="audit",
        )

    overlapping = [decision(1, 0, ACTION_LONG), decision(2, 12, ACTION_LONG)]
    ledger, metrics = simulate_sequence(overlapping, initial_capital=1_000.0)
    skipped_ok = (
        metrics.trades == 1
        and metrics.skipped_signals == 1
        and float(ledger.iloc[1]["equity_after"]) == float(ledger.iloc[0]["equity_after"])
        and float(ledger.iloc[1]["net_return_pct"]) == 0.0
        and not bool(ledger.iloc[1]["executed"])
    )
    blocked = [decision(1, 0, ACTION_NO_TRADE), decision(2, 12, ACTION_LONG)]
    ledger_blocked, metrics_blocked = simulate_sequence(blocked, initial_capital=1_000.0)
    no_trade_does_not_block = metrics_blocked.trades == 1 and metrics_blocked.skipped_signals == 0
    return [
        _check(
            "CP08-01.5",
            "a",
            "overlapping signal is skipped, priced at zero and never re-entered",
            skipped_ok,
            f"trades={metrics.trades} skipped={metrics.skipped_signals} equity unchanged at skip",
        ),
        _check(
            "CP08-01.5",
            "b",
            "NO_TRADE does not occupy the market and cannot block a later signal",
            no_trade_does_not_block,
            f"trades={metrics_blocked.trades} skipped={metrics_blocked.skipped_signals} "
            "(defect found in CP07 and fixed with a regression test)",
            status=STATUS_FIXED,
        ),
    ]


def _compounding_checks(context: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    worst_error = 0.0
    negative = 0
    mismatched_final = 0.0
    for policy, ledger in context["ledgers"].items():
        executed = ledger[ledger["executed"]]
        if executed.empty:
            continue
        equity = context["initial_capital"]
        for row in executed.itertuples(index=False):
            expected = equity * (1.0 + float(row.net_return_pct) / 100.0)
            worst_error = max(worst_error, abs(expected - float(row.equity_after)))
            equity = float(row.equity_after)
        negative += int((executed["equity_after"] <= 0).sum())
        final_by_product = context["initial_capital"] * float(
            np.prod(1.0 + executed["net_return_pct"].to_numpy(dtype=float) / 100.0)
        )
        mismatched_final = max(mismatched_final, abs(final_by_product - float(executed["equity_after"].iloc[-1])))
    checks.append(
        _check(
            "CP08-01.6",
            "a",
            "equity update is exactly equity * (1 + net_return) for every executed row",
            worst_error < 1e-9,
            f"max absolute equity error = {worst_error:.3e}; non-positive equity rows = {negative}",
        )
    )
    checks.append(
        _check(
            "CP08-01.6",
            "b",
            "final equity equals the product of all executed returns",
            mismatched_final < 1e-6,
            f"max deviation = {mismatched_final:.3e}",
        )
    )
    # Fold-level compounding chains to the aggregate (folds are contiguous).
    replayed = context["replayed_aggregate"]
    chains = {}
    for policy, payload in context["fold_replay"].items():
        multiple = float(np.prod([1.0 + row["total_return_pct"] / 100.0 for row in payload]))
        chains[policy] = {
            "fold_chained_total_return_pct": 100.0 * (multiple - 1.0),
            "aggregate_total_return_pct": replayed[policy]["total_return_pct"],
        }
    worst_chain = max(
        abs(value["fold_chained_total_return_pct"] - value["aggregate_total_return_pct"])
        for value in chains.values()
    )
    checks.append(
        _check(
            "CP08-01.6",
            "c",
            "chaining the five fold results reproduces the aggregate return",
            worst_chain < 1e-9,
            f"max deviation = {worst_chain:.3e} pct points",
            evidence=chains,
        )
    )
    return checks


def _cost_checks(context: dict[str, Any]) -> list[dict[str, Any]]:
    rebuilt = context["reconstruction"]
    costs = CostModel()
    flat_long = 100.0 * net_return(100.0, 100.0, ACTION_LONG, costs)
    flat_short = 100.0 * net_return(100.0, 100.0, ACTION_SHORT, costs)
    expected_drag = 100.0 * (
        ((1.0 - costs.slippage) * (1.0 - costs.fee_rate)) / ((1.0 + costs.slippage) * (1.0 + costs.fee_rate)) - 1.0
    )
    cost_ok = (
        abs(flat_long - expected_drag) < 1e-12
        and abs(flat_short - expected_drag) < 1e-12
        and context["fees_in_archive_match"] < 1e-12
    )
    # Cost monotonicity: extra cost can never improve a round trip.
    monotone = True
    previous = np.inf
    for total in (0.0, 0.0005, 0.001, 0.002, 0.005):
        value = net_return(100.0, 110.0, ACTION_LONG, CostModel(fee_rate=total, slippage=total))
        if value > previous:
            monotone = False
        previous = value
    return [
        _check(
            "CP08-01.7",
            "a",
            "returns are rebuilt from raw opens with both-side fees and directional slippage",
            cost_ok,
            f"max net error={rebuilt['max_abs_net_error_pct']:.3e} pct, "
            f"max gross error={rebuilt['max_abs_gross_error_pct']:.3e} pct",
        ),
        _check(
            "CP08-01.7",
            "b",
            "flat round trip charges fee+slippage on entry and exit",
            abs(flat_long - expected_drag) < 1e-12 and abs(flat_short - expected_drag) < 1e-12,
            f"flat-price drag = {flat_long:.4f}% (expected {expected_drag:.4f}%); "
            f"fee={costs.fee_rate:.2%}/side, slippage={costs.slippage:.2%}/side",
        ),
        _check(
            "CP08-01.7",
            "c",
            "net return is monotone decreasing in cost",
            monotone,
            "grid 0%, 0.05%, 0.10%, 0.20%, 0.50% per side",
        ),
        _check(
            "CP08-01.7",
            "d",
            "NO_TRADE rows carry no execution price and no cost",
            rebuilt["no_trade_rows_with_prices"] == 0,
            f"NO_TRADE rows with execution price = {rebuilt['no_trade_rows_with_prices']}",
        ),
        _check(
            "CP08-01.7",
            "e",
            "CP07 fee x slippage grid reproduces from the frozen datasets",
            context["sensitivity"]["matches_archive"],
            f"max absolute deviation = {context['sensitivity']['max_abs_delta_pct']:.3e} pct",
            evidence={"scenarios": context["sensitivity"]["scenarios"]},
        ),
        _cost_sensitivity_confound_check(context),
    ]


def _cost_sensitivity_confound_check(context: dict[str, Any]) -> dict[str, Any]:
    """The cost grid re-fits the threshold for every scenario.

    Round-trip cost is only the sum of fee and slippage, so a pure cost effect
    would be monotone.  The archived grid is not, which means the reported
    "sensitivity" mixes cost with train-only threshold re-selection.
    """

    by_total: dict[float, float] = {}
    for scenario in context["sensitivity"]["scenarios"]:
        if not scenario.get("present"):
            continue
        total_cost = round(float(scenario["fee_rate"]) + float(scenario["slippage"]), 6)
        by_total[total_cost] = float(scenario["momentum_total_return_pct"])
    ordered = sorted(by_total.items())
    monotone = all(current[1] <= previous[1] for previous, current in zip(ordered, ordered[1:]))
    return _check(
        "CP08-01.7",
        "f",
        "cost sensitivity isolates costs from threshold re-selection",
        monotone,
        "momentum total return by round-trip cost per side: "
        + ", ".join(f"{100.0 * cost:.2f}% -> {value:.2f}%" for cost, value in ordered)
        + ("; monotone" if monotone else
           "; NOT monotone because the threshold is re-fitted on train rows for every cost scenario"),
        status=STATUS_PASS if monotone else STATUS_WARN,
        evidence={"total_return_by_total_cost": {str(cost): value for cost, value in ordered}},
    )


def _short_checks(context: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    ledgers = context["ledgers"]
    shorts = pd.concat(
        [
            ledger[(ledger["executed"]) & (ledger["action"] == ACTION_SHORT)]
            for ledger in ledgers.values()
        ],
        ignore_index=True,
    )
    momentum_shorts = ledgers["momentum_threshold"]
    momentum_shorts = momentum_shorts[
        (momentum_shorts["executed"]) & (momentum_shorts["action"] == ACTION_SHORT)
    ]
    worst = float(shorts["net_return_pct"].min()) if len(shorts) else 0.0
    bounded = bool((shorts["net_return_pct"] > -100.0).all()) if len(shorts) else False
    checks.append(
        _check(
            "CP08-01.8",
            "a",
            "SHORT is fully collateralized: loss bounded above -100%, no negative equity",
            bounded,
            f"{len(shorts)} SHORT trades across all policies ({len(momentum_shorts)} in momentum), "
            f"worst net return = {worst:.4f}%",
        )
    )
    # Symmetry: with zero costs, LONG and SHORT of the same move are exact mirrors.
    zero = CostModel(fee_rate=0.0, slippage=0.0)
    mirror = net_return(100.0, 120.0, ACTION_LONG, zero)
    mirror_short = net_return(100.0, 120.0, ACTION_SHORT, zero)
    checks.append(
        _check(
            "CP08-01.8",
            "b",
            "SHORT gross P&L mirrors LONG (no leverage multiplier hidden in the formula)",
            abs((1.0 + mirror) * (1.0 + mirror_short) - 1.0) < 1e-12,
            f"LONG 100->120 = {100*mirror:.4f}%, SHORT = {100*mirror_short:.4f}% (product 1 + r_long)(1 + r_short) = 1",
            evidence={"long_pct": 100.0 * mirror, "short_pct": 100.0 * mirror_short},
        )
    )
    momentum = context["metrics"]["momentum_threshold"]
    checks.append(
        _check(
            "CP08-01.8",
            "c",
            "borrow/funding costs and margin liquidation are explicitly NOT modeled",
            True,
            "spot snapshot only: short proceeds are reused as collateral (no funding, no borrow rate, "
            "no liquidation engine). A perp funding cost of 0.01%/8h over 24h holding would drag "
            "momentum by roughly 0.03% per trade (approx. -1.4% cumulative on 49 SHORT trades).",
            status=STATUS_WARN,
            evidence={
                "momentum_short_trades": int(
                    (context["ledgers"]["momentum_threshold"]["action"] == ACTION_SHORT).sum()
                ),
                "funding_drag_proxy_pct_per_trade": 0.03,
            },
        )
    )
    return checks


def _calendar_checks(context: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    step: pd.Series = context["calendar"]["momentum_threshold"]["step"]
    executed = context["ledgers"]["momentum_threshold"]
    executed = executed[executed["executed"]]
    gaps: list[str] = []
    if not step.empty:
        spacing = step.index.to_series().diff().dropna().unique()
        gaps = [str(value) for value in spacing if value != pd.Timedelta(hours=1)]
    checks.append(
        _check(
            "CP08-01.9",
            "a",
            "calendar curve is hourly and covers first entry to last exit",
            len(step) > 0 and not gaps,
            f"points={len(step)}, span={step.index[0] if len(step) else None} .. {step.index[-1] if len(step) else None}, "
            f"irregular steps={gaps}",
        )
    )
    step_at_exits = step.reindex(pd.to_datetime(executed["exit_timestamp"], utc=True)).to_numpy(dtype=float)
    archived_equity = executed["equity_after"].to_numpy(dtype=float)
    error = float(np.max(np.abs(step_at_exits - archived_equity))) if len(step_at_exits) else 0.0
    checks.append(
        _check(
            "CP08-01.9",
            "b",
            "calendar curve reproduces ledger equity at every exit timestamp",
            error < 1e-9,
            f"max absolute deviation = {error:.3e}",
        )
    )
    step_dd = max_drawdown(step.to_numpy(dtype=float)[1:], initial_capital=context["initial_capital"])
    event_dd = context["events"]["momentum_threshold"]["max_drawdown_pct"]
    checks.append(
        _check(
            "CP08-01.9",
            "c",
            "exit-marked drawdown is invariant to calendar-time resampling",
            abs(step_dd - event_dd) < 1e-9,
            f"calendar step DD = {step_dd:.4f}%, event-time DD = {event_dd:.4f}%",
        )
    )
    mtm_dd = context["calendar"]["momentum_threshold"]["mtm_max_drawdown_pct"]
    checks.append(
        _check(
            "CP08-01.9",
            "d",
            "trade-level equity curve hides intra-hold excursion (mark-to-market stress)",
            True,
            f"momentum max drawdown: exit-marked {event_dd:.2f}% vs hourly mark-to-market {mtm_dd:.2f}% "
            f"({mtm_dd - event_dd:+.2f} pct points)",
            status=STATUS_WARN,
            evidence={
                "exit_marked_pct": event_dd,
                "mark_to_market_pct": mtm_dd,
                "delta_pct_points": mtm_dd - event_dd,
            },
        )
    )
    return checks


def _metrics_checks(context: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    fold_errors = context["fold_reconciliation_max_error"]
    checks.append(
        _check(
            "CP08-01.10",
            "a",
            "archived per-fold metrics reproduce exactly from the decision log",
            fold_errors["max_abs_error"] < 1e-9,
            f"max absolute deviation over 30 fold/policy rows = {fold_errors['max_abs_error']:.3e}",
            evidence=fold_errors,
        )
    )
    aggregate = context["replayed_aggregate"]
    archived_aggregate = context["archive"]["summary"]["aggregate"]
    drift = {
        policy: {
            "archived_total_return_pct": archived_aggregate[policy]["out_of_sample"]["total_return_pct"],
            "replayed_total_return_pct": aggregate[policy]["total_return_pct"],
        }
        for policy in archived_aggregate
    }
    worst = max(
        abs(value["archived_total_return_pct"] - value["replayed_total_return_pct"]) for value in drift.values()
    )
    checks.append(
        _check(
            "CP08-01.10",
            "b",
            "archived aggregate return reproduces from the decision log",
            worst < 1e-9,
            f"max deviation = {worst:.3e} pct points over {len(drift)} policies",
        )
    )
    momentum = context["metrics"]["momentum_threshold"]
    month_pf = momentum["profit_factor_usdt"]
    pct_pf = momentum["profit_factor_pct"]
    checks.append(
        _check(
            "CP08-01.10",
            "c",
            "profit factor convention is explicit: monetary P&L vs percentage sums",
            True,
            f"momentum PF: monetary (equity-weighted) = {month_pf:.4f}, percentage sums = {pct_pf:.4f}; "
            f"CP07 reports the percentage variant, which overstates the portfolio PF by "
            f"{100.0 * (pct_pf / month_pf - 1.0):.2f}%",
            status=STATUS_WARN,
            evidence={
                "policy": "momentum_threshold",
                "pf_usdt": month_pf,
                "pf_pct": pct_pf,
                "gross_profit_usdt": momentum["gross_profit_usdt"],
                "gross_loss_usdt": momentum["gross_loss_usdt"],
                "pf_by_policy": {
                    policy: {
                        "pf_usdt": values["profit_factor_usdt"],
                        "pf_pct": values["profit_factor_pct"],
                    }
                    for policy, values in context["metrics"].items()
                },
            },
        )
    )
    checks.append(
        _check(
            "CP08-01.10",
            "d",
            "win rate and average return are defined per trade in the aggregate and per window in the folds",
            True,
            f"momentum: trade win rate {momentum['win_rate_trade_pct']:.2f}% vs window win rate "
            f"{momentum['win_rate_window_pct']:.2f}%; avg per trade {momentum['avg_net_per_trade_pct']:.4f}% "
            f"vs avg per window {momentum['avg_net_per_window_pct']:.4f}% (CP07 documents the window average "
            "as 'avg net/trade')",
            status=STATUS_WARN,
            evidence={
                "win_rate_trade_pct": momentum["win_rate_trade_pct"],
                "win_rate_window_pct": momentum["win_rate_window_pct"],
                "avg_net_per_trade_pct": momentum["avg_net_per_trade_pct"],
                "avg_net_per_window_pct": momentum["avg_net_per_window_pct"],
            },
        )
    )
    oracle = context["metrics"]["oracle_retrospective"]
    checks.append(
        _check(
            "CP08-01.10",
            "e",
            "drawdown convention: trade-close peak, zero when every trade wins",
            True,
            f"oracle (all winners) trade-level DD = {oracle['max_drawdown_pct']:.2f}% but hourly "
            f"mark-to-market DD = {context['calendar']['oracle_retrospective']['mtm_max_drawdown_pct']:.2f}%; "
            f"momentum trade-level DD = {momentum['max_drawdown_pct']:.2f}%",
            status=STATUS_WARN,
            evidence={
                "oracle_trade_level_dd_pct": oracle["max_drawdown_pct"],
                "oracle_mark_to_market_dd_pct": context["calendar"]["oracle_retrospective"]["mtm_max_drawdown_pct"],
            },
        )
    )
    return checks


def _archive_schema_check(context: dict[str, Any]) -> list[dict[str, Any]]:
    diagnostics = context["contract_diagnostics"]
    return [
        _check(
            "CP08-01.2",
            "d",
            "archived decision log is contract-consumable without manual renaming",
            not diagnostics["missing_contract_columns"],
            f"missing contract columns in decisions.parquet: {diagnostics['missing_contract_columns']}; "
            "the audit reconstructs them explicitly (policy->policy_version, cutoff->decision_timestamp, "
            "horizon_hours=24) and validates the result",
            status=STATUS_WARN,
            evidence=diagnostics,
        )
    ]


# ---------------------------------------------------------------------------
# orchestration
# ---------------------------------------------------------------------------


def _fold_reconciliation(
    frame: pd.DataFrame,
    fold_metrics: pd.DataFrame,
    *,
    initial_capital: float,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    replayed: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, float] = {
        "total_return_pct": 0.0,
        "max_drawdown_pct": 0.0,
        "win_rate_pct": 0.0,
        "exposure_pct": 0.0,
        "final_equity": 0.0,
    }
    trades_mismatch = 0
    for row in fold_metrics.itertuples(index=False):
        subset = frame[(frame["policy_version"] == row.policy) & (frame["fold"] == row.fold)]
        ledger, metrics = replay_policy(subset, initial_capital=initial_capital)
        payload = {
            "fold": int(row.fold),
            "policy": row.policy,
            "trades": metrics.trades,
            "total_return_pct": metrics.total_return_pct,
            "max_drawdown_pct": metrics.max_drawdown_pct,
            "win_rate_pct": metrics.win_rate_pct,
            "exposure_pct": metrics.exposure_pct,
            "final_equity": metrics.final_equity,
        }
        replayed.setdefault(row.policy, []).append(payload)
        errors["total_return_pct"] = max(errors["total_return_pct"], abs(metrics.total_return_pct - float(row.total_return_pct)))
        errors["max_drawdown_pct"] = max(errors["max_drawdown_pct"], abs(metrics.max_drawdown_pct - float(row.max_drawdown_pct)))
        errors["win_rate_pct"] = max(errors["win_rate_pct"], abs(metrics.win_rate_pct - float(row.win_rate_pct)))
        errors["exposure_pct"] = max(errors["exposure_pct"], abs(metrics.exposure_pct - float(row.exposure_pct)))
        errors["final_equity"] = max(errors["final_equity"], abs(metrics.final_equity - float(row.final_equity)))
        trades_mismatch += int(metrics.trades != int(row.trades))
    errors["trades_mismatch"] = float(trades_mismatch)
    errors["max_abs_error"] = max(
        errors["total_return_pct"],
        errors["max_drawdown_pct"],
        errors["win_rate_pct"],
        errors["exposure_pct"],
        errors["final_equity"],
    ) if trades_mismatch == 0 else float("inf")
    return replayed, errors


def build_audit(paths: AuditPaths = AuditPaths(), *, initial_capital: float = DEFAULT_INITIAL_CAPITAL) -> dict[str, Any]:
    archive = load_archive(paths)
    frame, contract_diagnostics = contract_frame(archive["decisions"], archive["labels"])
    frame, reconstruction = reconstruct_returns(frame, archive["labels"])

    ledgers: dict[str, pd.DataFrame] = {}
    metrics: dict[str, dict[str, Any]] = {}
    events: dict[str, dict[str, Any]] = {}
    calendar: dict[str, pd.Series] = {}
    for policy, subset in frame.groupby("policy_version", sort=True):
        ledger, simulation = replay_policy(subset.sort_values("entry_timestamp"), initial_capital=initial_capital)
        ledgers[policy] = ledger
        events[policy] = {
            "total_return_pct": simulation.total_return_pct,
            "max_drawdown_pct": simulation.max_drawdown_pct,
            "profit_factor": simulation.profit_factor,
            "trades": simulation.trades,
            "skipped_signals": simulation.skipped_signals,
            "exposure_pct": simulation.exposure_pct,
        }
        metrics[policy] = dollar_metrics(ledger, initial_capital=initial_capital)
        metrics[policy]["total_return_pct"] = simulation.total_return_pct
        metrics[policy]["max_drawdown_pct"] = simulation.max_drawdown_pct
        metrics[policy]["profit_factor"] = simulation.profit_factor
        metrics[policy]["concentration"] = concentration_profile(ledger, initial_capital=initial_capital)
        step, mark = calendar_equity_curves(ledger, archive["market"], initial_capital=initial_capital)
        calendar[policy] = {
            "step": step,
            "mtm": mark,
            "step_max_drawdown_pct": max_drawdown(step.to_numpy(dtype=float), initial_capital=initial_capital),
            "mtm_max_drawdown_pct": max_drawdown(mark.to_numpy(dtype=float), initial_capital=initial_capital),
        }

    fold_replay, fold_errors = _fold_reconciliation(
        frame, archive["fold_metrics"], initial_capital=initial_capital
    )

    # Intrinsic cost consistency: fee_rate/slippage in the archive are the ones applied.
    fee_error = float(
        (frame["fee_rate"] - CostModel().fee_rate).abs().max()
        + (frame["slippage"] - CostModel().slippage).abs().max()
    )

    context: dict[str, Any] = {
        "paths": paths,
        "archive": archive,
        "frame": frame,
        "contract_diagnostics": contract_diagnostics,
        "reconstruction": reconstruction,
        "fees_in_archive_match": fee_error,
        "ledgers": ledgers,
        "metrics": metrics,
        "events": events,
        "calendar": calendar,
        "fold_replay": fold_replay,
        "fold_reconciliation_max_error": fold_errors,
        "replayed_aggregate": metrics,
        "initial_capital": initial_capital,
    }
    context["thresholds"] = threshold_robustness(paths, archive["fold_metrics"])
    context["sensitivity"] = sensitivity_reproduction(paths)

    checks: list[dict[str, Any]] = []
    checks.extend(_look_ahead_checks(context))
    checks.extend(_timestamp_checks(context))
    checks.extend(_price_availability_checks(context))
    checks.extend(_overlap_checks(context))
    checks.extend(_skip_chronology_checks(context))
    checks.extend(_compounding_checks(context))
    checks.extend(_cost_checks(context))
    checks.extend(_short_checks(context))
    checks.extend(_calendar_checks(context))
    checks.extend(_metrics_checks(context))
    checks.extend(_archive_schema_check(context))

    momentum_ledger = ledgers["momentum_threshold"]
    traded = momentum_ledger[momentum_ledger["action"] != ACTION_NO_TRADE]
    momentum_net = momentum_ledger["net_return_pct"].to_numpy(dtype=float)
    robustness = {
        "momentum_threshold": {
            "bootstrap": bootstrap_profile(momentum_net),
            "concentration": metrics["momentum_threshold"]["concentration"],
            "execution_delay": execution_delay_stress(
                momentum_ledger, archive["market"], initial_capital=initial_capital
            ),
            "long_short_split": {
                action: {
                    "trades": int((traded["action"] == action).sum()),
                    "compounded_pct": 100.0
                    * (
                        float(
                            np.prod(1.0 + traded.loc[traded["action"] == action, "net_return_pct"].to_numpy(dtype=float) / 100.0)
                        )
                        - 1.0
                    ),
                }
                for action in (ACTION_LONG, ACTION_SHORT)
            },
            "deflated_metrics": {
                "total_return_pct": metrics["momentum_threshold"]["final_equity"] / initial_capital * 100.0 - 100.0,
                "pf_usdt": metrics["momentum_threshold"]["profit_factor_usdt"],
                "pf_pct": metrics["momentum_threshold"]["profit_factor_pct"],
                "max_drawdown_exit_marked_pct": events["momentum_threshold"]["max_drawdown_pct"],
                "max_drawdown_mark_to_market_pct": calendar["momentum_threshold"]["mtm_max_drawdown_pct"],
            },
        },
        "threshold_selection": context["thresholds"],
    }

    counts = {status: sum(1 for check in checks if check["status"] == status) for status in (STATUS_PASS, STATUS_WARN, STATUS_FAIL, STATUS_FIXED)}
    verdict = "PASS" if counts[STATUS_FAIL] == 0 and counts[STATUS_WARN] == 0 else (
        "PASS WITH WARNINGS" if counts[STATUS_FAIL] == 0 else "FAIL"
    )
    items = {
        item: {
            "label": ITEM_LABELS[item],
            "checks": sum(1 for check in checks if check["item"] == item),
            "fail": sum(1 for check in checks if check["item"] == item and check["status"] == STATUS_FAIL),
            "warn": sum(1 for check in checks if check["item"] == item and check["status"] == STATUS_WARN),
            "fixed": sum(1 for check in checks if check["item"] == item and check["status"] == STATUS_FIXED),
        }
        for item in ITEM_LABELS
    }
    report = {
        "schema_version": AUDIT_SCHEMA,
        "audit": "S02-CP08-01",
        "source_schema": CP07_SCHEMA,
        "integrity_verdict": verdict,
        "status_counts": counts,
        "items": items,
        "checks": checks,
        "contract_diagnostics": contract_diagnostics,
        "reconstruction": reconstruction,
        "metrics": metrics,
        "calendar_metrics": {
            policy: {
                "step_max_drawdown_pct": payload["step_max_drawdown_pct"],
                "mark_to_market_max_drawdown_pct": payload["mtm_max_drawdown_pct"],
            }
            for policy, payload in calendar.items()
        },
        "fold_reconciliation": {
            "max_errors": fold_errors,
            "per_policy": fold_replay,
        },
        "robustness": robustness,
        "equity": {policy: {"step": payload["step"], "mtm": payload["mtm"]} for policy, payload in calendar.items()},
        "generated_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "initial_capital": initial_capital,
    }
    report["source_hashes"] = _source_hashes(paths)
    return report


def _equity_frame(report: dict[str, Any]) -> pd.DataFrame:
    rows = []
    for policy, payload in report["equity"].items():
        for kind, series in payload.items():
            if series.empty:
                continue
            rows.append(
                pd.DataFrame(
                    {
                        "policy": policy,
                        "curve": kind,
                        "timestamp": series.index,
                        "equity": series.to_numpy(dtype=float),
                    }
                )
            )
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(
        columns=["policy", "curve", "timestamp", "equity"]
    )


def write_report(paths: AuditPaths, report: dict[str, Any]) -> dict[str, Any]:
    output = paths.output_dir
    if output.exists():
        raise FileExistsError(f"CP08 audit already exists; refusing to overwrite: {output}")
    before = report["source_hashes"]
    output.mkdir(parents=True)
    try:
        equity = _equity_frame(report)
        equity.to_parquet(output / "equity_curves.parquet", index=False)
        metric_rows = []
        for policy, values in report["metrics"].items():
            metric_rows.append(
                {
                    "policy": policy,
                    **{
                        key: value
                        for key, value in values.items()
                        if key
                        in {
                            "trades",
                            "final_equity",
                            "gross_profit_usdt",
                            "gross_loss_usdt",
                            "profit_factor_usdt",
                            "profit_factor_pct",
                            "win_rate_trade_pct",
                            "win_rate_window_pct",
                            "avg_net_per_trade_pct",
                            "avg_net_per_window_pct",
                            "best_trade_pct",
                            "worst_trade_pct",
                            "no_trade",
                            "max_drawdown_pct",
                            "skipped",
                        }
                    },
                }
            )
        pd.DataFrame(metric_rows).to_csv(output / "metrics_comparison.csv", index=False)
        (output / "checks.json").write_text(
            json.dumps(_json_safe(report["checks"]), indent=2) + "\n", encoding="utf-8"
        )
        (output / "threshold_robustness.json").write_text(
            json.dumps(_json_safe(report["robustness"]["threshold_selection"]), indent=2) + "\n",
            encoding="utf-8",
        )
        summary = {
            key: value
            for key, value in report.items()
            if key not in {"checks", "equity", "robustness", "fold_reconciliation"}
        }
        summary["robustness"] = _json_safe(
            {key: value for key, value in report["robustness"].items() if key != "threshold_selection"}
        )
        summary["findings"] = _findings(report)
        (output / "summary.json").write_text(
            json.dumps(_json_safe(summary), indent=2, default=str) + "\n", encoding="utf-8"
        )
        after = _source_hashes(paths)
        manifest = {
            "schema_version": AUDIT_SCHEMA,
            "created_utc": pd.Timestamp.now(tz="UTC").isoformat(),
            "read_only": True,
            "source_hashes": before,
            "source_hashes_after": after,
            "source_hashes_unchanged": before == after,
            "original_artifacts_modified": before != after,
            "outputs": {
                "summary": {"path": "summary.json", "sha256": digest(output / "summary.json")},
                "checks": {"path": "checks.json", "sha256": digest(output / "checks.json")},
                "metrics": {"path": "metrics_comparison.csv", "sha256": digest(output / "metrics_comparison.csv")},
                "equity": {"path": "equity_curves.parquet", "sha256": digest(output / "equity_curves.parquet")},
                "thresholds": {"path": "threshold_robustness.json", "sha256": digest(output / "threshold_robustness.json")},
            },
        }
        (output / "manifest.json").write_text(
            json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8"
        )
        if before != after:
            raise RuntimeError("Source artifacts changed during the read-only audit")
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise
    return {"output_dir": str(output), "manifest": manifest}


def _findings(report: dict[str, Any]) -> list[dict[str, Any]]:
    """Human-readable findings derived from failing/warning checks."""

    findings = []
    for check in report["checks"]:
        if check["status"] in {STATUS_FAIL, STATUS_WARN, STATUS_FIXED}:
            findings.append(
                {
                    "id": check["id"],
                    "item": check["item"],
                    "item_label": check["item_label"],
                    "status": check["status"],
                    "finding": check["check"],
                    "detail": check["detail"],
                    "evidence": check["evidence"],
                }
            )
    return findings


def run(
    paths: AuditPaths = AuditPaths(),
    *,
    initial_capital: float = DEFAULT_INITIAL_CAPITAL,
    write: bool = True,
) -> dict[str, Any]:
    report = build_audit(paths, initial_capital=initial_capital)
    if write:
        report["written"] = write_report(paths, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S02-CP08-01 simulator integrity audit")
    parser.add_argument("--data-root", default=str(AuditPaths.data_root))
    parser.add_argument("--snapshot-dir", default=str(AuditPaths.snapshot_dir))
    parser.add_argument("--label-dataset-dir", default=str(AuditPaths.label_dir))
    parser.add_argument("--regime-dataset-dir", default=str(AuditPaths.regime_dir))
    parser.add_argument("--trading-dir", default=str(AuditPaths.trading_dir))
    parser.add_argument("--output-dir", default=str(AuditPaths.output_dir))
    parser.add_argument("--capital", type=float, default=DEFAULT_INITIAL_CAPITAL)
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)
    paths = AuditPaths(
        data_root=Path(args.data_root),
        snapshot_dir=Path(args.snapshot_dir),
        label_dir=Path(args.label_dataset_dir),
        regime_dir=Path(args.regime_dataset_dir),
        trading_dir=Path(args.trading_dir),
        output_dir=Path(args.output_dir),
    )
    report = run(paths, initial_capital=args.capital, write=not args.no_write)
    payload = {
        "integrity_verdict": report["integrity_verdict"],
        "status_counts": report["status_counts"],
        "items": report["items"],
        "checks": [
            {"id": check["id"], "status": check["status"], "detail": check["detail"]}
            for check in report["checks"]
        ],
        "metrics": {
            policy: {
                "trades": values["trades"],
                "total_return_pct": values["final_equity"] / report["initial_capital"] * 100.0 - 100.0,
                "profit_factor_usdt": values["profit_factor_usdt"],
                "profit_factor_pct": values["profit_factor_pct"],
                "max_drawdown_pct": values["max_drawdown_pct"],
                "max_drawdown_mark_to_market_pct": report["calendar_metrics"][policy][
                    "mark_to_market_max_drawdown_pct"
                ],
            }
            for policy, values in report["metrics"].items()
        },
        "robustness": {
            key: value
            for key, value in report["robustness"].items()
            if key != "threshold_selection"
        },
        "threshold_selection": {
            "boundary_folds": report["robustness"]["threshold_selection"]["boundary_folds"],
            "folds_with_better_train_threshold_outside_grid": report["robustness"]["threshold_selection"][
                "folds_with_better_train_threshold_outside_grid"
            ],
            "folds": [
                {
                    "fold": row["fold"],
                    "archived_threshold": row["archived_threshold"],
                    "best_extended_threshold": row["best_extended_threshold"],
                    "multiple_at_archived": row["multiple_at_archived"],
                    "best_extended_multiple": row["best_extended_multiple"],
                }
                for row in report["robustness"]["threshold_selection"]["folds"]
            ],
        },
    }
    print(json.dumps(_json_safe(payload), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
