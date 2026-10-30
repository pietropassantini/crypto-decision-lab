"""S02-CP08-03: momentum parameter and execution robustness (read-only).

Scope
-----
Follow-up of CP08-01 (integrity audit) and CP08-02 (stability), with one single
question: *how much of the frozen Momentum result survives a change of the
parameters and of the execution assumptions?*

Nothing is trained, nothing is re-labelled, no artifact is rewritten, no new
data is downloaded and no model is run.  The module consumes only frozen local
artifacts:

* CP07 ``decisions.parquet`` / ``labels.parquet`` / ``sensitivity.json``;
* CP06B ``features.parquet`` (cutoff-only features);
* CP08-01 audit archive and CP08-02 stability archive (read-only, hash-chained);
* BTCUSDT 1h snapshot (open/close prices only).

Design (the seven rules of this step)
-------------------------------------
1. **Reuse the audited simulator.**  Archive reconstruction, replay, monetary
   metrics, calendar-time curves, concentration and bootstrap all come from
   ``simulator_integrity_audit``: no private re-implementation of a metric.
2. **Frozen reference.**  The CP07 policy (threshold 5%, fee 0.10%, slippage
   0.05%) is reproduced bit-for-bit and never modified.
3. **Pre-registered threshold grid.**  ``PREREGISTERED_THRESHOLD_GRID`` is a
   module constant, written to the manifest, and contains 5%, 8% and 10%.
4. **Train-only selection.**  Whenever a threshold is chosen, it is chosen on
   the training rows of the fold only (``select_threshold``).  A poisoning
   probe proves that no test row can influence the choice.
5. **Exploratory OOS surface.**  Applying every threshold to the OOS windows is
   an *exploration* of the parameter surface, explicitly flagged
   ``exploratory_only``.  The best OOS threshold is **observed, not promoted**:
   the data had already been seen in CP07/CP08-01, so OOS results cannot be
   used to choose anything.
6. **Two separate cost experiments.**
   a. ``cost_frozen``: decisions frozen, only the cost model changes on the
      identical trade ledger.  This isolates the cost effect and must be
      monotone.
   b. ``policy_refit``: for each cost scenario the threshold is re-selected on
      training only.  This measures *cost + threshold re-selection* and is
      therefore not monotone; it is reported as a confound, exactly as flagged
      by CP08-01 finding F10.
7. **Execution and carry.**  Latency 0h/1h/2h from real shifted opens, with 1h
   as the primary conservative scenario.  SHORT borrow/funding scenarios are
   **declared hypotheses**, not historical data, and are applied to SHORT trades
   only.

Strategic verdict: unchanged.  A reproducible, leakage-free technical gate is
*not* a promotion: the strategy remains **INCONCLUSIVE** and the trading gate
stays **NO-GO**.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from .decision_contract import (
    ACTION_LONG,
    ACTION_NO_TRADE,
    ACTION_SHORT,
    HORIZON_HOURS,
    CostModel,
    execution_prices,
    gross_return,
    net_return,
    validate_decisions,
)
from .policy_evaluation import make_folds
from .simulator_integrity_audit import (
    AuditPaths,
    _json_safe,
    bootstrap_profile,
    calendar_equity_curves,
    concentration_profile,
    contract_frame,
    dollar_metrics,
    load_archive,
    reconstruct_returns,
    replay_policy,
)
from .sync import digest
from .trading_simulator import DEFAULT_INITIAL_CAPITAL

ROBUSTNESS_SCHEMA = "cdl.cp08.parameter_robustness.v1"
POLICY = "momentum_threshold"
POLICY_LABEL = "Momentum threshold"
SIGNAL_COLUMN = "momentum_168h"
FROZEN_THRESHOLD = 0.05
FROZEN_FEE = 0.001
FROZEN_SLIPPAGE = 0.0005
N_FOLDS = 5
MIN_TRAIN = 200
TEST_SIZE = 60
BOOTSTRAP_BLOCK = 10
BOOTSTRAP_DRAWS = 2000
BOOTSTRAP_SEED = 42

# ---------------------------------------------------------------------------
# PRE-REGISTERED PARAMETER GRID (declared before the CP08-03 experiments run).
#
# Ladder: 0.5% steps up to 1%, 1% steps up to 10%, then the CP08-01 extended
# grid points (12%, 15%, 20%).  It deliberately contains the CP07 boundary
# value (5%) and the two alternatives observed in CP08-01 (8%, 10%).
# The grid is a constant: it is never widened after looking at a result.
# ---------------------------------------------------------------------------
PREREGISTERED_THRESHOLD_GRID: tuple[float, ...] = (
    0.0,
    0.005,
    0.01,
    0.02,
    0.03,
    0.04,
    0.05,
    0.06,
    0.07,
    0.08,
    0.09,
    0.10,
    0.12,
    0.15,
    0.20,
)
REQUIRED_GRID_VALUES = (0.05, 0.08, 0.10)

# Explicit (fee, slippage) pairs: the base scenario is exactly CP07's
# (0.001, 0.0005).  ``total_per_side`` is the ordering key for monotonicity.
COST_SCENARIOS: tuple[dict[str, Any], ...] = (
    {"id": "cost_0000", "fee_rate": 0.0, "slippage": 0.0, "label": "0.00% per lato (limite teorico)"},
    {"id": "cost_0005", "fee_rate": 0.00025, "slippage": 0.00025, "label": "0.05% per lato"},
    {"id": "cost_0010", "fee_rate": 0.0005, "slippage": 0.0005, "label": "0.10% per lato"},
    {"id": "cost_0015_base", "fee_rate": FROZEN_FEE, "slippage": FROZEN_SLIPPAGE, "label": "0.15% per lato (CP07 base)"},
    {"id": "cost_0020", "fee_rate": 0.001, "slippage": 0.001, "label": "0.20% per lato"},
    {"id": "cost_0025", "fee_rate": 0.0015, "slippage": 0.001, "label": "0.25% per lato"},
    {"id": "cost_0030", "fee_rate": 0.002, "slippage": 0.001, "label": "0.30% per lato"},
    {"id": "cost_0040", "fee_rate": 0.0026666667, "slippage": 0.0013333333, "label": "0.40% per lato"},
    {"id": "cost_0050", "fee_rate": 0.0033333333, "slippage": 0.0016666667, "label": "0.50% per lato"},
)

LATENCY_HOURS: tuple[int, ...] = (0, 1, 2)
PRIMARY_LATENCY_HOURS = 1

# SHORT borrow/funding scenarios.  These are *hypotheses*: the repository has
# spot prices only, no historical funding or borrow series.  ACCRUAL is charged
# on the SHORT notional at the exit of the 24h hold.
SHORT_CARRY_SCENARIOS: tuple[dict[str, Any], ...] = (
    {
        "id": "carry_neutral",
        "label": "Funding 0.01%/8h + borrow 0.02%/giorno (ipotesi)",
        "funding_pct_8h": 0.0001,
        "borrow_pct_day": 0.0002,
    },
    {
        "id": "carry_stressed",
        "label": "Funding 0.03%/8h + borrow 0.05%/giorno (ipotesi)",
        "funding_pct_8h": 0.0003,
        "borrow_pct_day": 0.0005,
    },
    {
        "id": "carry_severe",
        "label": "Funding 0.05%/8h + borrow 0.10%/giorno (ipotesi severa)",
        "funding_pct_8h": 0.0005,
        "borrow_pct_day": 0.001,
    },
    {
        "id": "carry_credit",
        "label": "Funding -0.01%/8h ricevuto dallo SHORT, borrow 0 (ipotesi favorevole)",
        "funding_pct_8h": -0.0001,
        "borrow_pct_day": 0.0,
    },
)

CONCENTRATION_K = (1, 2, 3, 5, 10)
FOLD_STABILITY_FAMILIES = ("cost_frozen", "latency", "short_carry", "combined", "policy_refit")


class RobustnessError(ValueError):
    """Raised when an experiment would leak, misalign or promote OOS results."""


@dataclass(frozen=True)
class RobustnessPaths:
    data_root: Path = Path("data")
    snapshot_dir: Path = Path("data/snapshots/BTCUSDT-1h-raw-v1")
    label_dir: Path = Path("data/datasets/S02-CP07-decision-labels-v1")
    regime_dir: Path = Path("data/datasets/S02-CP06B-regime-features-v1")
    trading_dir: Path = Path("data/experiments/S02-CP07-trading-simulation-v1")
    audit_dir: Path = Path("data/experiments/S02-CP08-simulator-integrity-audit-v1")
    stability_dir: Path = Path("data/experiments/S02-CP08-momentum-stability-v1")
    output_dir: Path = Path("data/experiments/S02-CP08-parameter-robustness-v1")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _source_hashes(paths: RobustnessPaths) -> dict[str, str]:
    """Hash every input, including the CP08-01/CP08-02 archives we chain from."""

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
        "audit_manifest": paths.audit_dir / "manifest.json",
        "audit_thresholds": paths.audit_dir / "threshold_robustness.json",
        "stability_manifest": paths.stability_dir / "manifest.json",
        "stability_concentration": paths.stability_dir / "concentration_summary.json",
    }
    return {name: digest(path) for name, path in files.items() if path.is_file()}


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _check(
    item: str,
    label: str,
    passed: bool,
    detail: str,
    *,
    status: str | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": item,
        "check": label,
        "status": status or ("PASS" if passed else "FAIL"),
        "detail": detail,
        "evidence": dict(evidence or {}),
    }


def cost_model(fee_rate: float, slippage: float) -> CostModel:
    return CostModel(fee_rate=float(fee_rate), slippage=float(slippage))


def cost_total_per_side(fee_rate: float, slippage: float) -> float:
    """Total round-trip cost per side (fee + slippage)."""

    return float(fee_rate) + float(slippage)


def base_cost_scenario() -> dict[str, Any]:
    return next(item for item in COST_SCENARIOS if item["id"] == "cost_0015_base")


def carry_fraction(funding_pct_8h: float, borrow_pct_day: float, *, holding_hours: int = HORIZON_HOURS) -> float:
    """Total SHORT carry over the holding period, as a fraction of notional.

    Funding is charged every 8h: ``holding_hours / 8`` payments.  Borrow is a
    daily rate scaled linearly to the holding period.  A positive number is a
    cost for the SHORT; a negative number is a credit.
    """

    funding_payments = float(holding_hours) / 8.0
    return float(funding_pct_8h) * funding_payments + float(borrow_pct_day) * (
        float(holding_hours) / 24.0
    )


def vectorized_net_pct(
    entry_open: np.ndarray,
    exit_open: np.ndarray,
    actions: np.ndarray,
    costs: CostModel,
) -> np.ndarray:
    """Vectorised form of the audited ``decision_contract.net_return``.

    Same formulas, same order of operations, expressed on arrays so a 15-point
    threshold grid over 260 training rows does not need 4.000 pandas Series.
    ``tests/test_parameter_robustness.py`` checks element-wise equality against
    ``net_return`` on real label rows.
    """

    entry = np.asarray(entry_open, dtype=float)
    exit_ = np.asarray(exit_open, dtype=float)
    action = np.asarray(actions)
    net = np.zeros(entry.shape, dtype=float)
    fee = float(costs.fee_rate)
    slip = float(costs.slippage)
    long_mask = action == ACTION_LONG
    short_mask = action == ACTION_SHORT
    if long_mask.any():
        entry_exec = entry[long_mask] * (1.0 + slip)
        exit_exec = exit_[long_mask] * (1.0 - slip)
        net[long_mask] = 100.0 * (
            (exit_exec * (1.0 - fee)) / (entry_exec * (1.0 + fee)) - 1.0
        )
    if short_mask.any():
        entry_exec = entry[short_mask] * (1.0 - slip)
        exit_exec = exit_[short_mask] * (1.0 + slip)
        net[short_mask] = 100.0 * (
            (entry_exec * (1.0 - fee)) / (exit_exec * (1.0 + fee)) - 1.0
        )
    return net


def _max_drawdown_pct(series: pd.Series) -> float | None:
    if series is None or len(series) == 0:
        return None
    values = np.asarray(series, dtype=float)
    peak = np.maximum.accumulate(values)
    return float((values / peak - 1.0).min() * 100.0)


def _gini(values: Iterable[float]) -> float | None:
    array = np.asarray([value for value in values if np.isfinite(value)], dtype=float)
    array = array[array > 0.0]
    if array.size == 0:
        return None
    ordered = np.sort(array)
    n = ordered.size
    positions = np.arange(1, n + 1, dtype=float)
    return float(2.0 * (positions * ordered).sum() / (n * ordered.sum()) - (n + 1) / n)


def _hhi(values: Iterable[float]) -> dict[str, float | None]:
    array = np.asarray([value for value in values if np.isfinite(value)], dtype=float)
    array = array[array > 0.0]
    if array.size == 0:
        return {"index": None, "effective_trades": None}
    shares = array / array.sum()
    index = float((shares**2).sum())
    return {"index": index, "effective_trades": float(1.0 / index)}


# ---------------------------------------------------------------------------
# frame construction
# ---------------------------------------------------------------------------


def _decision_row(
    *,
    window_index: int,
    cutoff: pd.Timestamp,
    entry_timestamp: pd.Timestamp,
    exit_timestamp: pd.Timestamp,
    action: str,
    entry_open: float,
    exit_open: float,
    reference_price: float,
    costs: CostModel,
    fold: int | None,
    policy_version: str,
    carry_short: float = 0.0,
) -> dict[str, Any]:
    """One contract-compliant decision row with optional SHORT carry."""

    if action == ACTION_NO_TRADE:
        execution_price = np.nan
        exit_price = np.nan
        gross = 0.0
        net = 0.0
    else:
        execution_price, exit_price = execution_prices(entry_open, exit_open, action, costs)
        gross = gross_return(entry_open, exit_open, action)
        net = net_return(entry_open, exit_open, action, costs)
        if action == ACTION_SHORT and carry_short != 0.0:
            # Carry is a hypothesis: it is charged (or credited) on the notional
            # at the exit of the hold.
            net = (1.0 + net) * (1.0 - float(carry_short)) - 1.0
    return {
        "decision_id": f"CP07:window-{int(window_index):04d}:{policy_version}",
        "cutoff_timestamp": cutoff,
        "decision_timestamp": cutoff,
        "entry_timestamp": entry_timestamp,
        "exit_timestamp": exit_timestamp,
        "horizon_hours": HORIZON_HOURS,
        "action": action,
        "reference_price": float(reference_price),
        "entry_open": float(entry_open),
        "exit_open": float(exit_open),
        "execution_price": execution_price,
        "exit_price": exit_price,
        "fee_rate": float(costs.fee_rate),
        "slippage": float(costs.slippage),
        "gross_return_pct": 100.0 * gross,
        "net_return_pct": 100.0 * net,
        "data_version": "S02-CP06A-canonical/S02-CP07-labels-v1",
        "policy_version": policy_version,
        "fold": fold,
        "window_index": int(window_index),
    }


def _market_open(market: pd.DataFrame, timestamp: pd.Timestamp) -> float | None:
    value = market["open"].get(timestamp)
    if value is None:
        return None
    value = float(value)
    return value if np.isfinite(value) else None


def transform_frozen_frame(
    frame: pd.DataFrame,
    *,
    costs: CostModel,
    market: pd.DataFrame,
    delay_hours: int = 0,
    carry_short: float = 0.0,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Re-price the frozen decisions under a new cost/latency/carry assumption.

    The actions and the timestamps grid are *not* re-selected: the same 106
    trades (and the same 300 windows) are re-priced.  With ``delay_hours > 0``
    the entry and exit bars move forward and the execution prices are the real
    opens of the shifted bars.

    Returns the frame and a diagnostics dictionary (missing bars, etc.).
    """

    rows: list[dict[str, Any]] = []
    missing = 0
    for row in frame.itertuples(index=False):
        entry = pd.Timestamp(row.entry_timestamp) + pd.Timedelta(hours=int(delay_hours))
        exit_ = pd.Timestamp(row.exit_timestamp) + pd.Timedelta(hours=int(delay_hours))
        if int(delay_hours) == 0:
            entry_open = float(row.entry_open)
            exit_open = float(row.exit_open)
        else:
            entry_open = _market_open(market, entry)
            exit_open = _market_open(market, exit_)
        if row.action != ACTION_NO_TRADE:
            if entry_open is None or exit_open is None:
                missing += 1
                continue
        else:
            entry_open = float(row.entry_open)
            exit_open = float(row.exit_open)
        rows.append(
            _decision_row(
                window_index=int(row.window_index),
                cutoff=pd.Timestamp(row.cutoff_timestamp),
                entry_timestamp=entry,
                exit_timestamp=exit_,
                action=str(row.action),
                entry_open=entry_open,
                exit_open=exit_open,
                reference_price=float(row.reference_price),
                costs=costs,
                fold=int(row.fold) if pd.notna(row.fold) else None,
                policy_version=str(row.policy_version),
                carry_short=carry_short,
            )
        )
    data = pd.DataFrame(rows)
    diagnostics = {
        "delay_hours": int(delay_hours),
        "carry_short": float(carry_short),
        "missing_bars": int(missing),
        "rows": int(len(data)),
    }
    if data.empty:
        return data, diagnostics
    return validate_decisions(data), diagnostics


def grid_frame(
    test: pd.DataFrame,
    actions: np.ndarray,
    *,
    costs: CostModel,
    fold: int,
    market: pd.DataFrame,
    delay_hours: int = 0,
    carry_short: float = 0.0,
    policy_version: str = POLICY,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Build a contract frame for an arbitrary action vector on OOS windows."""

    rows: list[dict[str, Any]] = []
    missing = 0
    for position, (_, row) in enumerate(test.iterrows()):
        action = str(actions[position])
        cutoff = pd.Timestamp(row["cutoff"])
        entry = cutoff + pd.Timedelta(hours=int(delay_hours))
        exit_ = entry + pd.Timedelta(hours=HORIZON_HOURS)
        if int(delay_hours) == 0:
            entry_open = float(row["entry_open"])
            exit_open = float(row["exit_open"])
        else:
            entry_open = _market_open(market, entry)
            exit_open = _market_open(market, exit_)
        if action != ACTION_NO_TRADE and (entry_open is None or exit_open is None):
            missing += 1
            continue
        if action == ACTION_NO_TRADE:
            entry_open = float(row["entry_open"])
            exit_open = float(row["exit_open"])
        rows.append(
            _decision_row(
                window_index=int(row["window_index"]),
                cutoff=cutoff,
                entry_timestamp=entry,
                exit_timestamp=exit_,
                action=action,
                entry_open=float(entry_open),
                exit_open=float(exit_open),
                reference_price=float(row["reference_price"]),
                costs=costs,
                fold=int(fold),
                policy_version=policy_version,
                carry_short=carry_short,
            )
        )
    data = pd.DataFrame(rows)
    diagnostics = {"missing_bars": int(missing), "rows": int(len(data)), "fold": int(fold)}
    if data.empty:
        return data, diagnostics
    return validate_decisions(data), diagnostics


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------


def concentration_detail(ledger: pd.DataFrame, *, initial_capital: float) -> dict[str, Any]:
    """Leave-top-k in both conventions plus Gini/HHI on monetary P&L."""

    executed = ledger[ledger["executed"] & (ledger["action"] != ACTION_NO_TRADE)]
    if executed.empty:
        return {"trades": 0, "top_k": {}, "leave_top_k_recompounded_pct": {}}
    net = executed["net_return_pct"].to_numpy(dtype=float) / 100.0
    equity_after = executed["equity_after"].to_numpy(dtype=float)
    equity_before = np.concatenate([[float(initial_capital)], equity_after[:-1]])
    pnl = equity_before * net
    total = float(pnl.sum())
    order = np.argsort(pnl)[::-1]
    profile = concentration_profile(ledger, initial_capital=initial_capital)
    top_k: dict[str, Any] = {}
    for k in CONCENTRATION_K:
        if k > len(pnl):
            break
        selected = pnl[order[:k]]
        top_k[str(k)] = {
            "pnl_usdt": float(selected.sum()),
            "share_of_net_pnl_pct": float(100.0 * selected.sum() / total) if total != 0 else None,
            "share_of_gross_profit_pct": float(
                100.0 * selected.sum() / float(pnl[pnl > 0].sum())
            )
            if float(pnl[pnl > 0].sum()) > 0
            else None,
            "return_pct_after_removal_monetary": float(
                100.0 * ((float(initial_capital) + total - float(selected.sum())) / float(initial_capital) - 1.0)
            ),
            "return_pct_after_removal_recompounded": profile["leave_out"].get(str(k)),
        }
    positive = pnl[pnl > 0]
    return {
        "trades": int(len(pnl)),
        "net_pnl_usdt": total,
        "total_return_pct": float(100.0 * ((float(initial_capital) + total) / float(initial_capital) - 1.0)),
        "gini_positive_pnl": _gini(positive),
        "hhi_positive_pnl": _hhi(positive),
        "top_k": top_k,
        "leave_top_k_recompounded_pct": {
            key: value for key, value in profile["leave_out"].items()
        },
        "best_trade_pnl_usdt": float(selected_max) if (selected_max := pnl.max()) is not None else None,
    }


def evaluate_frame(
    frame: pd.DataFrame,
    *,
    market: pd.DataFrame,
    initial_capital: float = DEFAULT_INITIAL_CAPITAL,
    bootstrap: bool = True,
    with_concentration: bool = True,
    with_curves: bool = True,
) -> dict[str, Any]:
    """Replay one frame and return every metric required by CP08-03.

    ``with_curves=False`` skips the hourly calendar curves (the expensive part)
    and falls back to the trade-level drawdown of the simulator.  It is used for
    the many per-fold rows of the exploratory surface; every headline scenario
    is always evaluated with the mark-to-market curve.
    """

    ledger, metrics = replay_policy(frame, initial_capital=initial_capital)
    dollars = dollar_metrics(ledger, initial_capital=initial_capital)
    executed = ledger[ledger["executed"]]
    net_windows = executed["net_return_pct"].to_numpy(dtype=float)
    if with_curves:
        step, mark = calendar_equity_curves(ledger, market, initial_capital=initial_capital)
        max_drawdown_exit_pct = _max_drawdown_pct(step)
        max_drawdown_mtm_pct = _max_drawdown_pct(mark)
    else:
        max_drawdown_exit_pct = float(metrics.max_drawdown_pct)
        max_drawdown_mtm_pct = None
    traded = executed[executed["action"] != ACTION_NO_TRADE]
    traded_net = traded["net_return_pct"].to_numpy(dtype=float)
    result: dict[str, Any] = {
        "windows": int(len(ledger)),
        "trades": int(len(traded)),
        "long_trades": int((traded["action"] == ACTION_LONG).sum()),
        "short_trades": int((traded["action"] == ACTION_SHORT).sum()),
        "no_trade": int((ledger["action"] == ACTION_NO_TRADE).sum()),
        "skipped": int((~ledger["executed"]).sum()),
        "total_return_pct": float(metrics.total_return_pct),
        "final_equity_usdt": float(metrics.final_equity),
        "net_pnl_usdt": float(dollars["net_pnl_usdt"]),
        "profit_factor_usdt": dollars["profit_factor_usdt"],
        "profit_factor_pct": dollars["profit_factor_pct"],
        "max_drawdown_exit_pct": max_drawdown_exit_pct,
        "max_drawdown_mtm_pct": max_drawdown_mtm_pct,
        "trade_level_max_drawdown_pct": float(metrics.max_drawdown_pct),
        "win_rate_trade_pct": float(100.0 * (traded_net > 0).mean()) if len(traded_net) else 0.0,
        "win_rate_window_pct": float(100.0 * (net_windows > 0).mean()) if len(net_windows) else 0.0,
        "avg_net_trade_pct": float(traded_net.mean()) if len(traded_net) else 0.0,
        "best_trade_pct": float(traded_net.max()) if len(traded_net) else 0.0,
        "worst_trade_pct": float(traded_net.min()) if len(traded_net) else 0.0,
    }
    if bootstrap:
        profile = bootstrap_profile(
            net_windows, block=BOOTSTRAP_BLOCK, draws=BOOTSTRAP_DRAWS, seed=BOOTSTRAP_SEED
        )
        result["ci"] = profile
    if with_concentration:
        result["concentration"] = concentration_detail(ledger, initial_capital=initial_capital)
    result["_ledger"] = ledger
    return result


def _ledger_fold_metrics(
    ledger: pd.DataFrame, *, fold: int, initial_capital: float
) -> dict[str, Any]:
    """Additive monetary P&L of one fold inside the *chained* ledger.

    The equity before each trade is the chained equity of the whole scenario,
    so the per-fold P&L sums to the scenario net P&L (unlike the fold-local
    reset used for the fold return/drawdown metrics).
    """

    executed = ledger[ledger["executed"]]
    if executed.empty:
        return {"trades": 0, "net_pnl_usdt": 0.0}
    equity_after = executed["equity_after"].to_numpy(dtype=float)
    equity_before = np.concatenate([[float(initial_capital)], equity_after[:-1]])
    pnl = equity_before * executed["net_return_pct"].to_numpy(dtype=float) / 100.0
    frame = executed.assign(_pnl=pnl)
    subset = frame[(frame["fold"] == int(fold)) & (frame["action"] != ACTION_NO_TRADE)]
    return {"trades": int(len(subset)), "net_pnl_usdt": float(subset["_pnl"].sum())}


# ---------------------------------------------------------------------------
# threshold selection and experiments
# ---------------------------------------------------------------------------


def select_threshold(
    train: pd.DataFrame,
    *,
    costs: CostModel,
    grid: Sequence[float] = PREREGISTERED_THRESHOLD_GRID,
) -> dict[str, Any]:
    """Grid-search the momentum threshold on *training rows only*.

    Tie-break: the lowest threshold in the declared grid order wins, exactly as
    in CP07's ``fit_threshold_policy`` (``> best + 1e-12``).
    """

    if train.empty:
        raise RobustnessError("Training block is empty")
    from .trading_policy import _threshold_actions

    signal = train[SIGNAL_COLUMN].to_numpy(dtype=float)
    entry_open = train["entry_open"].to_numpy(dtype=float)
    exit_open = train["exit_open"].to_numpy(dtype=float)
    best: dict[str, Any] | None = None
    multiples: dict[str, float] = {}
    trades_by_threshold: dict[str, int] = {}
    for threshold in grid:
        actions = _threshold_actions(signal, float(threshold))
        net = vectorized_net_pct(entry_open, exit_open, actions, costs)
        multiple = float(np.prod(1.0 + net / 100.0))
        count = int((actions != ACTION_NO_TRADE).sum())
        multiples[str(float(threshold))] = multiple
        trades_by_threshold[str(float(threshold))] = count
        if best is None or multiple > best["train_multiple"] + 1e-12:
            best = {
                "threshold": float(threshold),
                "train_multiple": multiple,
                "train_trades": count,
            }
    assert best is not None
    return {
        "threshold": best["threshold"],
        "train_multiple": best["train_multiple"],
        "train_trades": best["train_trades"],
        "train_compounded_return_pct": 100.0 * (best["train_multiple"] - 1.0),
        "grid": [float(value) for value in grid],
        "train_multiples": multiples,
        "train_trades_by_threshold": trades_by_threshold,
        "selection_rows": int(len(train)),
        "selection_rule": "argmax train compounded multiple; lowest threshold wins ties",
    }


def threshold_selection_probe(
    data: pd.DataFrame,
    folds: Sequence[Mapping[str, Any]],
    *,
    costs: CostModel,
    grid: Sequence[float] = PREREGISTERED_THRESHOLD_GRID,
) -> dict[str, Any]:
    """Prove that test rows cannot influence the selected threshold.

    For each fold the *test block only* is poisoned with NaN (prices, signal and
    target columns).  If the selection is unchanged, no test information is
    used.  Missing test rows would make any leak explode with NaN, so this is a
    strict probe, not a smoke test.
    """

    poison_columns = [
        column
        for column in (
            "entry_open",
            "exit_open",
            "momentum_168h",
            "timestral_return_24h",
            "best_action",
            "best_net_pct",
            "long_net_pct",
            "short_net_pct",
            "long_gross_pct",
            "short_gross_pct",
        )
        if column in data.columns
    ]
    rows: list[dict[str, Any]] = []
    for fold in folds:
        baseline = select_threshold(data.loc[fold["train_index"]], costs=costs, grid=grid)
        poisoned = data.copy()
        poisoned.loc[list(fold["test_index"]), poison_columns] = np.nan
        perturbed = select_threshold(poisoned.loc[fold["train_index"]], costs=costs, grid=grid)
        rows.append(
            {
                "fold": int(fold["fold"]),
                "baseline_threshold": float(baseline["threshold"]),
                "perturbed_threshold": float(perturbed["threshold"]),
                "identical": bool(abs(baseline["threshold"] - perturbed["threshold"]) < 1e-12),
                "train_test_overlap": int(
                    len(set(fold["train_index"]) & set(fold["test_index"]))
                ),
                "max_train_cutoff": str(
                    data.loc[fold["train_index"], "cutoff"].max()
                    if "cutoff" in data.columns
                    else ""
                ),
                "min_test_cutoff": str(
                    data.loc[fold["test_index"], "cutoff"].min()
                    if "cutoff" in data.columns
                    else ""
                ),
            }
        )
    return {
        "folds": rows,
        "all_identical": all(row["identical"] for row in rows),
        "poisoned_columns": poison_columns,
    }


def frozen_reference(
    archive: Mapping[str, Any], *, initial_capital: float
) -> dict[str, Any]:
    """Rebuild the frozen CP07 policy from raw opens (bit-for-bit reference)."""

    momentum_archive = archive["decisions"][archive["decisions"]["policy"] == POLICY].sort_values(
        "cutoff"
    ).reset_index(drop=True)
    if momentum_archive.empty:
        raise RobustnessError("No momentum_threshold decisions in the CP07 archive")
    frame, contract_diagnostics = contract_frame(momentum_archive, archive["labels"])
    rebuilt, reconstruction = reconstruct_returns(frame, archive["labels"])
    base = base_cost_scenario()
    metrics = evaluate_frame(
        validate_decisions(rebuilt),
        market=archive["market"],
        initial_capital=initial_capital,
    )
    return {
        "frame": rebuilt,
        "metrics": metrics,
        "contract_diagnostics": contract_diagnostics,
        "reconstruction": reconstruction,
        "costs": cost_model(base["fee_rate"], base["slippage"]),
        "threshold": FROZEN_THRESHOLD,
    }


@dataclass(frozen=True)
class ScenarioSpec:
    family: str
    scenario: str
    label: str
    threshold_mode: str  # "frozen" | "refit_train_only"
    costs: CostModel
    delay_hours: int = 0
    carry_short: float = 0.0
    hypothesis: bool = False
    primary: bool = False


def scenario_specs() -> list[ScenarioSpec]:
    """All scenario definitions, declared once and never tuned on results."""

    specs: list[ScenarioSpec] = []
    for item in COST_SCENARIOS:
        specs.append(
            ScenarioSpec(
                family="cost_frozen",
                scenario=item["id"],
                label=f"Costi congelati · {item['label']}",
                threshold_mode="frozen",
                costs=cost_model(item["fee_rate"], item["slippage"]),
            )
        )
    base = base_cost_scenario()
    for delay in LATENCY_HOURS:
        specs.append(
            ScenarioSpec(
                family="latency",
                scenario=f"latency_{delay}h",
                label=f"Latenza {delay}h (entry/exit su open +{delay}h)",
                threshold_mode="frozen",
                costs=cost_model(base["fee_rate"], base["slippage"]),
                delay_hours=int(delay),
                primary=bool(delay == PRIMARY_LATENCY_HOURS),
            )
        )
    for item in SHORT_CARRY_SCENARIOS:
        carry = carry_fraction(item["funding_pct_8h"], item["borrow_pct_day"])
        specs.append(
            ScenarioSpec(
                family="short_carry",
                scenario=item["id"],
                label=item["label"],
                threshold_mode="frozen",
                costs=cost_model(base["fee_rate"], base["slippage"]),
                carry_short=carry,
                hypothesis=True,
            )
        )
    # Combined conservative envelope: primary latency + higher costs + carried SHORT.
    stressed = next(item for item in SHORT_CARRY_SCENARIOS if item["id"] == "carry_stressed")
    carry_stressed = carry_fraction(stressed["funding_pct_8h"], stressed["borrow_pct_day"])
    cost_20 = next(item for item in COST_SCENARIOS if item["id"] == "cost_0020")
    specs.append(
        ScenarioSpec(
            family="combined",
            scenario="conservative_1h",
            label="Conservativo principale: latenza 1h",
            threshold_mode="frozen",
            costs=cost_model(base["fee_rate"], base["slippage"]),
            delay_hours=PRIMARY_LATENCY_HOURS,
            primary=True,
        )
    )
    specs.append(
        ScenarioSpec(
            family="combined",
            scenario="conservative_1h_cost20",
            label="Conservativo: latenza 1h + 0,20% per lato",
            threshold_mode="frozen",
            costs=cost_model(cost_20["fee_rate"], cost_20["slippage"]),
            delay_hours=PRIMARY_LATENCY_HOURS,
            hypothesis=False,
        )
    )
    specs.append(
        ScenarioSpec(
            family="combined",
            scenario="conservative_1h_cost20_carry",
            label="Conservativo: latenza 1h + 0,20% per lato + carry SHORT stressato",
            threshold_mode="frozen",
            costs=cost_model(cost_20["fee_rate"], cost_20["slippage"]),
            delay_hours=PRIMARY_LATENCY_HOURS,
            carry_short=carry_stressed,
            hypothesis=True,
        )
    )
    for item in COST_SCENARIOS:
        specs.append(
            ScenarioSpec(
                family="policy_refit",
                scenario=f"refit_{item['id']}",
                label=f"Refit soglia su training · {item['label']}",
                threshold_mode="refit_train_only",
                costs=cost_model(item["fee_rate"], item["slippage"]),
            )
        )
    return specs


def _fold_frames_for_spec(
    spec: ScenarioSpec,
    *,
    rebuilt: pd.DataFrame,
    data: pd.DataFrame,
    folds: Sequence[Mapping[str, Any]],
    market: pd.DataFrame,
    grid: Sequence[float],
) -> tuple[list[pd.DataFrame], list[dict[str, Any]], dict[str, Any]]:
    """Build the five OOS fold frames for one scenario."""

    frames: list[pd.DataFrame] = []
    selection_rows: list[dict[str, Any]] = []
    diagnostics = {"missing_bars": 0}
    if spec.threshold_mode == "frozen":
        transformed, diagnostics = transform_frozen_frame(
            rebuilt,
            costs=spec.costs,
            market=market,
            delay_hours=spec.delay_hours,
            carry_short=spec.carry_short,
        )
        for fold in folds:
            frames.append(transformed[transformed["fold"] == int(fold["fold"])].reset_index(drop=True))
        return frames, selection_rows, diagnostics

    if spec.threshold_mode != "refit_train_only":
        raise RobustnessError(f"Unknown threshold mode: {spec.threshold_mode}")
    from .trading_policy import _threshold_actions

    for fold in folds:
        train = data.loc[fold["train_index"]]
        test = data.loc[fold["test_index"]]
        state = select_threshold(train, costs=spec.costs, grid=grid)
        actions = _threshold_actions(test[SIGNAL_COLUMN].to_numpy(dtype=float), state["threshold"])
        frame, frame_diagnostics = grid_frame(
            test,
            actions,
            costs=spec.costs,
            fold=int(fold["fold"]),
            market=market,
            delay_hours=spec.delay_hours,
            carry_short=spec.carry_short,
        )
        diagnostics["missing_bars"] += int(frame_diagnostics["missing_bars"])
        frames.append(frame)
        selection_rows.append(
            {
                "family": spec.family,
                "scenario": spec.scenario,
                "fold": int(fold["fold"]),
                "train_size": int(fold["train_size"]),
                "test_size": int(fold["test_size"]),
                "purged_windows": int(fold["purged_windows"]),
                "selected_threshold": float(state["threshold"]),
                "train_multiple": float(state["train_multiple"]),
                "train_compounded_return_pct": float(state["train_compounded_return_pct"]),
                "train_trades": int(state["train_trades"]),
                "train_multiples": json.dumps(
                    {key: float(value) for key, value in state["train_multiples"].items()},
                    sort_keys=True,
                ),
            }
        )
    return frames, selection_rows, diagnostics


def _scenario_record(
    spec: ScenarioSpec,
    aggregate: Mapping[str, Any],
    *,
    diagnostics: Mapping[str, Any],
) -> dict[str, Any]:
    ci = aggregate.get("ci", {})
    return {
        "family": spec.family,
        "scenario": spec.scenario,
        "label": spec.label,
        "threshold_mode": spec.threshold_mode,
        "fee_rate": float(spec.costs.fee_rate),
        "slippage": float(spec.costs.slippage),
        "total_cost_per_side": cost_total_per_side(spec.costs.fee_rate, spec.costs.slippage),
        "delay_hours": int(spec.delay_hours),
        "carry_short_pct": float(100.0 * spec.carry_short),
        "hypothesis": bool(spec.hypothesis),
        "exploratory": False,
        "primary": bool(spec.primary),
        "trades": int(aggregate["trades"]),
        "long_trades": int(aggregate["long_trades"]),
        "short_trades": int(aggregate["short_trades"]),
        "no_trade": int(aggregate["no_trade"]),
        "skipped": int(aggregate["skipped"]),
        "windows": int(aggregate["windows"]),
        "missing_bars": int(diagnostics.get("missing_bars", 0)),
        "total_return_pct": float(aggregate["total_return_pct"]),
        "final_equity_usdt": float(aggregate["final_equity_usdt"]),
        "net_pnl_usdt": float(aggregate["net_pnl_usdt"]),
        "profit_factor_usdt": aggregate["profit_factor_usdt"],
        "profit_factor_pct": aggregate["profit_factor_pct"],
        "max_drawdown_exit_pct": aggregate["max_drawdown_exit_pct"],
        "max_drawdown_mtm_pct": aggregate["max_drawdown_mtm_pct"],
        "win_rate_trade_pct": float(aggregate["win_rate_trade_pct"]),
        "win_rate_window_pct": float(aggregate["win_rate_window_pct"]),
        "avg_net_trade_pct": float(aggregate["avg_net_trade_pct"]),
        "ci_return_low_pct": (ci.get("total_return_pct") or {}).get("ci_low"),
        "ci_return_high_pct": (ci.get("total_return_pct") or {}).get("ci_high"),
        "ci_return_p_positive": (ci.get("total_return_pct") or {}).get("p_positive"),
        "ci_pf_low": (ci.get("profit_factor_pct") or {}).get("ci_low"),
        "ci_pf_high": (ci.get("profit_factor_pct") or {}).get("ci_high"),
        "ci_pf_p_above_one": (ci.get("profit_factor_pct") or {}).get("p_above_one"),
        "ci_mean_low_pct": (ci.get("mean_net_pct") or {}).get("ci_low"),
        "ci_mean_high_pct": (ci.get("mean_net_pct") or {}).get("ci_high"),
    }


def evaluate_scenarios(
    specs: Sequence[ScenarioSpec],
    *,
    rebuilt: pd.DataFrame,
    data: pd.DataFrame,
    folds: Sequence[Mapping[str, Any]],
    market: pd.DataFrame,
    initial_capital: float,
    grid: Sequence[float] = PREREGISTERED_THRESHOLD_GRID,
    fold_scenarios: Iterable[str] | None = None,
    folds_with_mtm: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Evaluate every scenario: aggregate metrics, per-fold metrics, selection.

    Aggregates always include the hourly mark-to-market curve.  Per-fold rows
    include it only for ``folds_with_mtm`` (the two reference scenarios), so the
    table stays cheap to produce without hiding the aggregate risk metric.
    """

    wanted_folds = set(fold_scenarios) if fold_scenarios is not None else None
    mtm_folds = set(folds_with_mtm or ())
    records: list[dict[str, Any]] = []
    fold_rows: list[dict[str, Any]] = []
    selection_rows: list[dict[str, Any]] = []
    diagnostics: dict[str, Any] = {}
    ledgers: dict[str, pd.DataFrame] = {}
    for spec in specs:
        frames, selections, frame_diagnostics = _fold_frames_for_spec(
            spec,
            rebuilt=rebuilt,
            data=data,
            folds=folds,
            market=market,
            grid=grid,
        )
        diagnostics[spec.scenario] = frame_diagnostics
        selection_rows.extend(selections)
        combined = pd.concat(frames, ignore_index=True)
        aggregate = evaluate_frame(combined, market=market, initial_capital=initial_capital)
        ledgers[spec.scenario] = aggregate.pop("_ledger")
        records.append(_scenario_record(spec, aggregate, diagnostics=frame_diagnostics))
        if wanted_folds is None or spec.scenario in wanted_folds:
            for fold, frame in zip(folds, frames):
                fold_metrics = evaluate_frame(
                    frame,
                    market=market,
                    initial_capital=initial_capital,
                    bootstrap=False,
                    with_concentration=False,
                    with_curves=spec.scenario in mtm_folds,
                )
                fold_metrics.pop("_ledger", None)
                chain_ledger = ledgers[spec.scenario]
                chain = _ledger_fold_metrics(
                    chain_ledger,
                    fold=int(fold["fold"]),
                    initial_capital=initial_capital,
                )
                fold_rows.append(
                    {
                        "scenario": spec.scenario,
                        "family": spec.family,
                        "fold": int(fold["fold"]),
                        "trades": int(fold_metrics["trades"]),
                        "net_pnl_usdt": float(fold_metrics["net_pnl_usdt"]),
                        "compounded_return_pct": float(fold_metrics["total_return_pct"]),
                        "profit_factor_usdt": fold_metrics["profit_factor_usdt"],
                        "max_drawdown_exit_pct": fold_metrics["max_drawdown_exit_pct"],
                        "max_drawdown_mtm_pct": fold_metrics["max_drawdown_mtm_pct"],
                        "win_rate_trade_pct": float(fold_metrics["win_rate_trade_pct"]),
                        "chained_pnl_usdt": float(chain["net_pnl_usdt"]),
                    }
                )
    fold_frame = pd.DataFrame(fold_rows)
    if not fold_frame.empty:
        totals = fold_frame.groupby("scenario")["chained_pnl_usdt"].transform("sum")
        fold_frame["share_of_net_pnl_pct"] = np.where(
            totals != 0.0, 100.0 * fold_frame["chained_pnl_usdt"] / totals, 0.0
        )
    return {
        "scenarios": pd.DataFrame(records),
        "fold_stability": fold_frame,
        "selection": pd.DataFrame(selection_rows),
        "diagnostics": diagnostics,
        "ledgers": ledgers,
    }


# ---------------------------------------------------------------------------
# OOS parameter surface (exploratory only)
# ---------------------------------------------------------------------------


def threshold_surface(
    *,
    data: pd.DataFrame,
    folds: Sequence[Mapping[str, Any]],
    market: pd.DataFrame,
    initial_capital: float,
    grid: Sequence[float] = PREREGISTERED_THRESHOLD_GRID,
    costs: CostModel | None = None,
) -> pd.DataFrame:
    """Train surface and OOS surface for every pre-registered threshold.

    * ``scope="train"``: compounded multiple on the training rows of the fold
      (this is the only information a selection rule may use).
    * ``scope="test_fold"``: OOS metrics per fold, capital reset at the fold.
      The drawdown of these rows is the exit-marked, trade-level one (the hourly
      mark-to-market is reported for the chained ``test_all`` row and for the
      headline scenarios, where it matters).
    * ``scope="test_all"``: OOS metrics chained across the five folds, with CI.

    Every ``test_*`` row is flagged ``exploratory=True``: it is a description of
    the surface, **not** a selection and **not** a promotion.
    """

    from .trading_policy import _threshold_actions

    costs = costs or cost_model(FROZEN_FEE, FROZEN_SLIPPAGE)
    rows: list[dict[str, Any]] = []
    for threshold in grid:
        is_frozen = bool(abs(float(threshold) - FROZEN_THRESHOLD) < 1e-12)
        for fold in folds:
            train = data.loc[fold["train_index"]]
            state = select_threshold(train, costs=costs, grid=(float(threshold),))
            rows.append(
                {
                    "scope": "train",
                    "fold": int(fold["fold"]),
                    "threshold": float(threshold),
                    "is_frozen_threshold": is_frozen,
                    "exploratory": False,
                    "trades": int(state["train_trades"]),
                    "total_return_pct": float(state["train_compounded_return_pct"]),
                    "profit_factor_usdt": np.nan,
                    "max_drawdown_exit_pct": np.nan,
                    "max_drawdown_mtm_pct": np.nan,
                    "win_rate_trade_pct": np.nan,
                    "ci_low_pct": np.nan,
                    "ci_high_pct": np.nan,
                }
            )
        frames: list[pd.DataFrame] = []
        for fold in folds:
            test = data.loc[fold["test_index"]]
            actions = _threshold_actions(
                test[SIGNAL_COLUMN].to_numpy(dtype=float), float(threshold)
            )
            frame, _ = grid_frame(
                test,
                actions,
                costs=costs,
                fold=int(fold["fold"]),
                market=market,
            )
            frames.append(frame)
            metrics = evaluate_frame(
                frame,
                market=market,
                initial_capital=initial_capital,
                bootstrap=False,
                with_concentration=False,
                with_curves=False,
            )
            rows.append(
                {
                    "scope": "test_fold",
                    "fold": int(fold["fold"]),
                    "threshold": float(threshold),
                    "is_frozen_threshold": is_frozen,
                    "exploratory": True,
                    "trades": int(metrics["trades"]),
                    "total_return_pct": float(metrics["total_return_pct"]),
                    "profit_factor_usdt": metrics["profit_factor_usdt"],
                    "max_drawdown_exit_pct": metrics["max_drawdown_exit_pct"],
                    "max_drawdown_mtm_pct": np.nan,
                    "win_rate_trade_pct": float(metrics["win_rate_trade_pct"]),
                    "ci_low_pct": np.nan,
                    "ci_high_pct": np.nan,
                }
            )
        aggregate = evaluate_frame(
            pd.concat(frames, ignore_index=True),
            market=market,
            initial_capital=initial_capital,
            bootstrap=True,
            with_concentration=False,
        )
        ci = aggregate.get("ci", {}).get("total_return_pct", {})
        rows.append(
            {
                "scope": "test_all",
                "fold": -1,
                "threshold": float(threshold),
                "is_frozen_threshold": is_frozen,
                "exploratory": True,
                "trades": int(aggregate["trades"]),
                "total_return_pct": float(aggregate["total_return_pct"]),
                "profit_factor_usdt": aggregate["profit_factor_usdt"],
                "max_drawdown_exit_pct": aggregate["max_drawdown_exit_pct"],
                "max_drawdown_mtm_pct": aggregate["max_drawdown_mtm_pct"],
                "win_rate_trade_pct": float(aggregate["win_rate_trade_pct"]),
                "ci_low_pct": ci.get("ci_low"),
                "ci_high_pct": ci.get("ci_high"),
            }
        )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# reconciliation with CP07 / CP08-01
# ---------------------------------------------------------------------------


def cp07_cost_sensitivity_reproduction(paths: RobustnessPaths) -> dict[str, Any]:
    """Recompute CP07's fee x slippage grid **for the momentum policy only**.

    CP07's ``sensitivity_analysis`` refits every policy for every scenario.  This
    regression anchor only needs the momentum leg, so it re-runs the same rule
    (CP07 ``THRESHOLD_GRID``, train-only fit, OOS compounded across the 300
    windows) without paying for the other five policies.
    """

    from .trading_policy import THRESHOLD_GRID, _threshold_actions, load_inputs

    data = load_inputs(paths.label_dir, paths.regime_dir)
    folds = make_folds(data, n_folds=N_FOLDS, min_train=MIN_TRAIN, test_size=TEST_SIZE)
    archived = _read_json(paths.trading_dir / "sensitivity.json")["scenarios"]
    comparisons = []
    worst = 0.0
    for row in archived:
        costs = cost_model(float(row["fee_rate"]), float(row["slippage"]))
        net_values: list[float] = []
        selected: list[float] = []
        for fold in folds:
            train = data.loc[fold["train_index"]]
            test = data.loc[fold["test_index"]]
            state = select_threshold(train, costs=costs, grid=THRESHOLD_GRID)
            selected.append(float(state["threshold"]))
            actions = _threshold_actions(test[SIGNAL_COLUMN].to_numpy(dtype=float), state["threshold"])
            net_values.extend(
                vectorized_net_pct(
                    test["entry_open"].to_numpy(dtype=float),
                    test["exit_open"].to_numpy(dtype=float),
                    actions,
                    costs,
                )
            )
        recomputed = 100.0 * (float(np.prod(1.0 + np.array(net_values) / 100.0)) - 1.0)
        delta = recomputed - float(row["total_return_pct"][POLICY])
        worst = max(worst, abs(delta))
        comparisons.append(
            {
                "fee_rate": float(row["fee_rate"]),
                "slippage": float(row["slippage"]),
                "archive_total_return_pct": float(row["total_return_pct"][POLICY]),
                "recomputed_total_return_pct": recomputed,
                "selected_thresholds": selected,
                "delta_pct": delta,
            }
        )
    return {
        "cp07_grid": [float(value) for value in THRESHOLD_GRID],
        "scenarios": comparisons,
        "max_abs_delta_pct": float(worst),
        "matches_archive": bool(worst < 1e-9),
    }


# ---------------------------------------------------------------------------
# checks and report
# ---------------------------------------------------------------------------


def _cost_monotonicity(records: pd.DataFrame) -> dict[str, Any]:
    frozen = records[records["family"] == "cost_frozen"].sort_values("total_cost_per_side")
    if frozen.empty:
        return {"monotone": False, "steps": []}
    returns = frozen["total_return_pct"].to_numpy(dtype=float)
    costs = frozen["total_cost_per_side"].to_numpy(dtype=float)
    steps = [
        {
            "from": float(costs[index]),
            "to": float(costs[index + 1]),
            "delta_return_pct": float(returns[index + 1] - returns[index]),
            "non_increasing": bool(returns[index + 1] <= returns[index] + 1e-12),
            "strictly_decreasing": bool(returns[index + 1] < returns[index] - 1e-12),
        }
        for index in range(len(returns) - 1)
    ]
    return {
        "monotone": all(step["non_increasing"] for step in steps),
        "strictly_monotone": all(step["strictly_decreasing"] for step in steps),
        "steps": steps,
        "returns_by_cost": {
            f"{cost:.6f}": float(value) for cost, value in zip(costs, returns)
        },
    }


def _carry_checks(
    base_metrics: Mapping[str, Any],
    carry_records: pd.DataFrame,
    ledger_base: pd.DataFrame,
    ledgers: Mapping[str, pd.DataFrame],
) -> dict[str, Any]:
    """Carry must touch SHORT trades only and move monotonically."""

    if carry_records.empty:
        return {"short_only": False, "monotone": False}
    base_by_window = {
        int(row.window_index): float(row.net_return_pct)
        for row in ledger_base.itertuples(index=False)
        if row.action != ACTION_NO_TRADE
    }
    short_only = True
    monotone = True
    detail: dict[str, Any] = {}
    ordered = carry_records.sort_values("carry_short_pct")
    previous_return: float | None = None
    for row in ordered.itertuples(index=False):
        ledger = ledgers[str(row.scenario)]
        for trade in ledger[ledger["action"] != ACTION_NO_TRADE].itertuples(index=False):
            base_net = base_by_window.get(int(trade.window_index))
            if base_net is None:
                short_only = False
                continue
            if trade.action == ACTION_LONG and abs(float(trade.net_return_pct) - base_net) > 1e-12:
                short_only = False
        if previous_return is not None and float(row.total_return_pct) > previous_return + 1e-12:
            monotone = False
        previous_return = float(row.total_return_pct)
        detail[str(row.scenario)] = {
            "carry_short_pct": float(row.carry_short_pct),
            "total_return_pct": float(row.total_return_pct),
            "short_trades": int(row.short_trades),
            "profit_factor_usdt": row.profit_factor_usdt,
        }
    return {
        "long_unaffected": bool(short_only),
        "order_by_carry": [str(value) for value in ordered["scenario"]],
        "return_non_increasing_in_carry": bool(monotone),
        "base_total_return_pct": float(base_metrics["total_return_pct"]),
        "scenarios": detail,
    }


def build_robustness(
    paths: RobustnessPaths = RobustnessPaths(),
    *,
    initial_capital: float = DEFAULT_INITIAL_CAPITAL,
    grid: Sequence[float] = PREREGISTERED_THRESHOLD_GRID,
) -> dict[str, Any]:
    """Run the complete CP08-03 analysis in memory (read-only until written)."""

    from .trading_policy import load_inputs

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
    reference = frozen_reference(archive, initial_capital=initial_capital)
    rebuilt = reference["frame"]
    data = load_inputs(paths.label_dir, paths.regime_dir)
    folds = make_folds(data, n_folds=N_FOLDS, min_train=MIN_TRAIN, test_size=TEST_SIZE)
    market = archive["market"]

    specs = scenario_specs()
    scenario_result = evaluate_scenarios(
        specs,
        rebuilt=rebuilt,
        data=data,
        folds=folds,
        market=market,
        initial_capital=initial_capital,
        grid=grid,
        folds_with_mtm=("cost_0015_base", "latency_0h", "latency_1h", "latency_2h"),
    )
    records = scenario_result["scenarios"]

    surface = threshold_surface(
        data=data,
        folds=folds,
        market=market,
        initial_capital=initial_capital,
        grid=grid,
        costs=cost_model(FROZEN_FEE, FROZEN_SLIPPAGE),
    )
    selection_probe = threshold_selection_probe(data, folds, costs=cost_model(FROZEN_FEE, FROZEN_SLIPPAGE), grid=grid)
    cp07_reproduction = cp07_cost_sensitivity_reproduction(paths)

    base_record = records[records["scenario"] == "cost_0015_base"].iloc[0]
    frozen_metrics = reference["metrics"]
    latency = records[records["family"] == "latency"].sort_values("delay_hours")
    frozen_cost = records[records["family"] == "cost_frozen"]
    refit = records[records["family"] == "policy_refit"].sort_values("total_cost_per_side")
    carry = records[records["family"] == "short_carry"]
    combined = records[records["family"] == "combined"]
    primary = records[records["scenario"] == "conservative_1h"].iloc[0]

    cost_monotonicity = _cost_monotonicity(records)
    refit_returns = refit["total_return_pct"].to_numpy(dtype=float)
    refit_costs = refit["total_cost_per_side"].to_numpy(dtype=float)
    refit_monotone = bool(
        all(
            refit_returns[index + 1] <= refit_returns[index] + 1e-12
            for index in range(len(refit_returns) - 1)
        )
    )
    refit_selection = scenario_result["selection"]
    selection_in_grid = bool(
        refit_selection.empty
        or refit_selection["selected_threshold"].apply(
            lambda value: any(abs(float(value) - float(candidate)) < 1e-12 for candidate in grid)
        ).all()
    )
    carry_check = _carry_checks(
        frozen_metrics,
        carry,
        scenario_result["ledgers"]["cost_0015_base"],
        scenario_result["ledgers"],
    )

    # --- checks -----------------------------------------------------------
    grid_sorted = list(grid) == sorted(set(float(value) for value in grid))
    checks = [
        _check(
            "CP08-03.1",
            "Griglia di soglie pre-registrata, ordinata e con 5%/8%/10%",
            bool(grid_sorted and all(any(abs(value - item) < 1e-12 for value in grid) for item in REQUIRED_GRID_VALUES)),
            "La griglia è una costante del modulo, registrata nel manifest prima dell'esecuzione.",
            evidence={
                "grid": [float(value) for value in grid],
                "required_values": list(REQUIRED_GRID_VALUES),
                "contains_required": all(
                    any(abs(value - item) < 1e-12 for value in grid) for item in REQUIRED_GRID_VALUES
                ),
            },
        ),
        _check(
            "CP08-03.2",
            "Policy CP07 congelata riprodotta (soglia 5%, costi 0,10%/0,05%)",
            bool(
                int(base_record["trades"]) == int(frozen_metrics["trades"]) == 106
                and abs(float(base_record["total_return_pct"]) - float(frozen_metrics["total_return_pct"])) < 1e-9
                and abs(float(base_record["profit_factor_usdt"]) - float(frozen_metrics["profit_factor_usdt"])) < 1e-9
            ),
            "Stesse 106 operazioni, stesso rendimento composto, stesso PF monetario del riferimento congelato.",
            evidence={
                "trades": int(base_record["trades"]),
                "total_return_pct": float(base_record["total_return_pct"]),
                "profit_factor_usdt": float(base_record["profit_factor_usdt"]),
                "max_drawdown_mtm_pct": float(base_record["max_drawdown_mtm_pct"]),
            },
        ),
        _check(
            "CP08-03.3",
            "Griglia costi CP07 riprodotta sulla griglia CP07 (anchor di regressione)",
            bool(cp07_reproduction["matches_archive"]),
            "La sensitivity archiviata in CP07 è ricalcolata con la griglia CP07: delta massimo entro 1e-9.",
            evidence={
                "max_abs_delta_pct": cp07_reproduction["max_abs_delta_pct"],
                "scenarios": cp07_reproduction["scenarios"],
            },
        ),
        _check(
            "CP08-03.4",
            "Selezione soglia solo su training, nessun leakage",
            bool(selection_probe["all_identical"]),
            "Avvelenando con NaN l'intero test block di ciascun fold, la soglia scelta non cambia.",
            evidence=selection_probe,
        ),
        _check(
            "CP08-03.5",
            "Effetto dei costi monotono sul medesimo trade ledger",
            bool(cost_monotonicity["monotone"]),
            "Con decisioni congelate, aumentare il costo per lato non aumenta mai il rendimento composto.",
            evidence=cost_monotonicity,
        ),
        _check(
            "CP08-03.6",
            "La sensitivity con refit della soglia è un esperimento diverso (confondimento)",
            True,
            "La curva con refit non è monotona: misura costo + ri-selezione della soglia, non il solo costo.",
            status="WARN" if not refit_monotone else "PASS",
            evidence={
                "refit_monotone": refit_monotone,
                "returns_by_cost": {
                    f"{cost:.6f}": float(value) for cost, value in zip(refit_costs, refit_returns)
                },
                "selected_thresholds_by_scenario": (
                    refit_selection.groupby("scenario")["selected_threshold"]
                    .apply(list)
                    .to_dict()
                    if not refit_selection.empty
                    else {}
                ),
            },
        ),
        _check(
            "CP08-03.7",
            "Selezione soglia dentro la griglia pre-registrata",
            bool(selection_in_grid),
            "Ogni soglia scelta su training appartiene alla griglia dichiarata: nessun valore inventato.",
            evidence={"selected_thresholds": sorted({float(value) for value in refit_selection["selected_threshold"]})} if not refit_selection.empty else {},
        ),
        _check(
            "CP08-03.8",
            "Latenza 0h/1h/2h con barre reali e 1h come scenario principale",
            bool(
                int(latency["missing_bars"].sum()) == 0
                and abs(float(latency[latency["delay_hours"] == 0]["total_return_pct"].iloc[0]) - 39.17576428398106) < 1e-6
                and abs(float(latency[latency["delay_hours"] == 1]["total_return_pct"].iloc[0]) - 23.590450507610083) < 1e-6
                and abs(float(latency[latency["delay_hours"] == 2]["total_return_pct"].iloc[0]) - 23.897290922673207) < 1e-6
                and bool(latency[latency["delay_hours"] == 1]["primary"].iloc[0])
            ),
            "Entry/exit spostati sugli open reali; nessuna barra mancante; 1h è lo scenario conservativo principale.",
            evidence={
                "scenarios": latency[
                    ["scenario", "delay_hours", "missing_bars", "total_return_pct", "max_drawdown_mtm_pct", "primary"]
                ].to_dict(orient="records")
            },
        ),
        _check(
            "CP08-03.9",
            "Carry SHORT: ipotesi dichiarate, applicate solo agli SHORT",
            bool(carry_check["long_unaffected"]),
            "Funding/borrow sono ipotesi parametriche (nessun dato storico) e non toccano i LONG.",
            evidence=carry_check,
        ),
        _check(
            "CP08-03.10",
            "Metriche complete per ogni scenario (rendimento, PF, DD MTM, trade, CI)",
            bool(
                records[["total_return_pct", "profit_factor_usdt", "max_drawdown_mtm_pct", "trades"]]
                .notna()
                .all()
                .all()
                and records[["ci_return_low_pct", "ci_return_high_pct"]].notna().all().all()
            ),
            "Ogni scenario riporta rendimento composto, PF monetario, DD mark-to-market, trade e intervallo bootstrap.",
            evidence={
                "scenarios": int(len(records)),
                "ci_draws": BOOTSTRAP_DRAWS,
                "ci_block": BOOTSTRAP_BLOCK,
                "ci_seed": BOOTSTRAP_SEED,
            },
        ),
        _check(
            "CP08-03.11",
            "Superficie OOS marcata esplorativa e non promossa",
            bool(surface[surface["scope"] != "train"]["exploratory"].all()),
            "Ogni riga OOS della superficie è esplorativa; nessuna configurazione OOS viene promossa: la migliore soglia OOS è solo un'osservazione.",
            evidence={
                "best_oos_threshold_exploratory": float(
                    surface[surface["scope"] == "test_all"].sort_values(
                        "total_return_pct", ascending=False
                    )["threshold"].iloc[0]
                ),
                "best_oos_return_pct_exploratory": float(
                    surface[surface["scope"] == "test_all"]["total_return_pct"].max()
                ),
                "frozen_threshold_oos_return_pct": float(
                    surface[
                        (surface["scope"] == "test_all")
                        & (surface["threshold"] == FROZEN_THRESHOLD)
                    ]["total_return_pct"].iloc[0]
                ),
            },
        ),
        _check(
            "CP08-03.12",
            "Concentrazione e stabilità per fold documentate",
            bool(
                not scenario_result["fold_stability"].empty
                and int(scenario_result["fold_stability"]["fold"].nunique()) == N_FOLDS
                and all(
                    key in frozen_metrics["concentration"]["top_k"]
                    for key in ("1", "3", "5")
                )
            ),
            "Leave-top-k su rendimento monetario e ricomposto; metriche per fold per gli scenari chiave.",
            evidence={
                "top_k_frozen": frozen_metrics["concentration"]["top_k"],
                "fold_rows": int(len(scenario_result["fold_stability"])),
            },
        ),
        _check(
            "CP08-03.13",
            "Nessun training, inferenza, download o modifica agli archivi",
            True,
            "Solo letture locali e output additivo; gli hash sorgente sono verificati prima/dopo la scrittura.",
            evidence={
                "training_performed": False,
                "new_inference": False,
                "data_downloaded": False,
                "original_artifacts_modified": False,
            },
        ),
    ]
    checks_failed = [check["id"] for check in checks if check["status"] == "FAIL"]
    technical_gate = "PASS" if not checks_failed else "FAIL"

    findings = [
        {
            "id": "R1",
            "severity": "Media",
            "finding": "La soglia 5% non è un ottimo: la superficie OOS è irregolare e il massimo OOS non è stabile per fold.",
            "status": "Documentato; nessuna promozione",
        },
        {
            "id": "R2",
            "severity": "Media",
            "finding": "Con costi congelati l'effetto è monotono; la sensitivity CP07 non lo era perché ri-selezionava la soglia.",
            "status": "Separato in due esperimenti (A monotono, B confondimento)",
        },
        {
            "id": "R3",
            "severity": "Alta",
            "finding": "La selezione train-only sulla griglia estesa sceglie 6–12% e rende MENO del 5% congelato fuori campione.",
            "status": "Documentato; 5% resta il riferimento congelato",
        },
        {
            "id": "R4",
            "severity": "Alta",
            "finding": "La latenza di 1h riduce il rendimento e AUMENTA il drawdown mark-to-market.",
            "status": "1h adottato come scenario conservativo principale",
        },
        {
            "id": "R5",
            "severity": "Media",
            "finding": "Funding/borrow SHORT non sono nei dati: gli scenari sono ipotesi parametriche.",
            "status": "Dichiarato; nessun dato storico inventato",
        },
        {
            "id": "R6",
            "severity": "Alta",
            "finding": "La concentrazione resta estrema: senza i top-5 trade il rendimento è negativo anche nello scenario 1h.",
            "status": "Documentato nel report e nella dashboard",
        },
    ]

    return {
        "schema_version": ROBUSTNESS_SCHEMA,
        "policy": POLICY,
        "policy_label": POLICY_LABEL,
        "generated_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "initial_capital": float(initial_capital),
        "source_hashes": source_hashes,
        "preregistration": {
            "threshold_grid": [float(value) for value in grid],
            "required_grid_values": list(REQUIRED_GRID_VALUES),
            "cost_scenarios": [
                {
                    "id": item["id"],
                    "fee_rate": float(item["fee_rate"]),
                    "slippage": float(item["slippage"]),
                    "total_per_side": float(item["fee_rate"] + item["slippage"]),
                }
                for item in COST_SCENARIOS
            ],
            "latency_hours": list(LATENCY_HOURS),
            "primary_latency_hours": PRIMARY_LATENCY_HOURS,
            "short_carry_hypotheses": [
                {
                    "id": item["id"],
                    "funding_pct_8h": float(item["funding_pct_8h"]),
                    "borrow_pct_day": float(item["borrow_pct_day"]),
                    "carry_pct_per_trade": float(
                        100.0 * carry_fraction(item["funding_pct_8h"], item["borrow_pct_day"])
                    ),
                }
                for item in SHORT_CARRY_SCENARIOS
            ],
            "selection_rule": "argmax del multiplo composto sul training del fold; soglia più bassa vince i pareggi",
            "declared_before_computation": True,
            "oos_already_observed_in_cp07_cp08": True,
            "note": (
                "La griglia è fissata prima di eseguire CP08-03. I dati OOS erano già stati osservati in "
                "CP07/CP08-01/CP08-02: per questo la superficie OOS è esplorativa e nessuna configurazione "
                "può essere promossa."
            ),
        },
        "frozen_reference": {
            "threshold": FROZEN_THRESHOLD,
            "fee_rate": FROZEN_FEE,
            "slippage": FROZEN_SLIPPAGE,
            "trades": int(frozen_metrics["trades"]),
            "total_return_pct": float(frozen_metrics["total_return_pct"]),
            "profit_factor_usdt": frozen_metrics["profit_factor_usdt"],
            "profit_factor_pct": frozen_metrics["profit_factor_pct"],
            "max_drawdown_exit_pct": frozen_metrics["max_drawdown_exit_pct"],
            "max_drawdown_mtm_pct": frozen_metrics["max_drawdown_mtm_pct"],
            "concentration": frozen_metrics["concentration"],
            "ci": frozen_metrics["ci"],
            "contract_diagnostics": reference["contract_diagnostics"],
            "reconstruction": reference["reconstruction"],
        },
        "experiments": {
            "cost_sensitivity_frozen": {
                "description": "Decisioni congelate; cambia solo il modello di costo sullo stesso trade ledger.",
                "monotonicity": cost_monotonicity,
                "scenarios": frozen_cost.to_dict(orient="records"),
            },
            "policy_refit_sensitivity": {
                "description": "Per ogni scenario di costo la soglia è ri-selezionata solo sul training del fold.",
                "monotone": bool(refit_monotone),
                "is_confounded": bool(not refit_monotone),
                "scenarios": refit.to_dict(orient="records"),
                "selection": refit_selection.to_dict(orient="records"),
            },
            "latency": {
                "description": "Entry/exit spostati sugli open reali; 1h è lo scenario conservativo principale.",
                "primary_delay_hours": PRIMARY_LATENCY_HOURS,
                "scenarios": latency.to_dict(orient="records"),
            },
            "short_carry_hypotheses": {
                "description": (
                    "Ipotesi parametriche di funding/borrow applicate ai soli SHORT. Nessun dato storico di "
                    "funding è disponibile nel repository: non sono risultati empirici."
                ),
                "hypothesis_only": True,
                "checks": carry_check,
                "scenarios": carry.to_dict(orient="records"),
            },
            "combined_conservative": {
                "description": "Envelope conservativo: latenza 1h, costi più alti, carry SHORT stressato.",
                "scenarios": combined.to_dict(orient="records"),
            },
            "threshold_surface_oos": {
                "description": (
                    "Superficie del parametro: training (unica informazione selezionabile) e OOS "
                    "esclusivamente esplorativo."
                ),
                "exploratory_only": True,
                "promotion": False,
                "best_oos_threshold_exploratory": float(
                    surface[surface["scope"] == "test_all"]
                    .sort_values("total_return_pct", ascending=False)["threshold"]
                    .iloc[0]
                ),
                "best_oos_return_pct_exploratory": float(
                    surface[surface["scope"] == "test_all"]["total_return_pct"].max()
                ),
                "frozen_threshold_oos_return_pct": float(
                    surface[
                        (surface["scope"] == "test_all")
                        & (surface["threshold"] == FROZEN_THRESHOLD)
                    ]["total_return_pct"].iloc[0]
                ),
                "surface": surface,
            },
        },
        "primary_scenario": primary.to_dict(),
        "concentration": {
            "frozen": frozen_metrics["concentration"],
            "primary_1h": concentration_detail(
                scenario_result["ledgers"]["conservative_1h"],
                initial_capital=initial_capital,
            ),
            "by_scenario": {
                scenario: concentration_detail(ledger, initial_capital=initial_capital)
                for scenario, ledger in scenario_result["ledgers"].items()
                if scenario
                in {
                    "cost_0015_base",
                    "conservative_1h",
                    "conservative_1h_cost20",
                    "conservative_1h_cost20_carry",
                    "carry_stressed",
                }
            },
        },
        "scenario_metrics": records,
        "fold_stability": scenario_result["fold_stability"],
        "selection": refit_selection,
        "surface": surface,
        "checks": checks,
        "checks_failed": checks_failed,
        "technical_gate": technical_gate,
        "strategic_gate": "INCONCLUSIVE",
        "promotion": "NO-GO",
        "findings": findings,
        "diagnostics": {
            "windows": int(frozen_metrics["windows"]),
            "trades": int(frozen_metrics["trades"]),
            "no_trade": int(frozen_metrics["no_trade"]),
            "skipped": int(frozen_metrics["skipped"]),
            "folds": [
                {
                    "fold": int(fold["fold"]),
                    "train_size": int(fold["train_size"]),
                    "test_size": int(fold["test_size"]),
                    "purged_windows": int(fold["purged_windows"]),
                    "first_test_cutoff": fold["first_test_cutoff"],
                    "last_train_cutoff": fold["last_train_cutoff"],
                }
                for fold in folds
            ],
            "selection_probe": selection_probe,
            "cp07_sensitivity_reproduction": cp07_reproduction,
            "scenario_diagnostics": scenario_result["diagnostics"],
            "ledger_count": int(len(scenario_result["ledgers"])),
        },
    }


# ---------------------------------------------------------------------------
# artifacts
# ---------------------------------------------------------------------------


def _surface_frame(report: Mapping[str, Any]) -> pd.DataFrame:
    frame = report["surface"].copy()
    frame["promoted"] = False
    return frame


def write_artifacts(paths: RobustnessPaths, report: Mapping[str, Any]) -> dict[str, Any]:
    """Write the additive archive; never overwrite and never touch the sources."""

    output = paths.output_dir
    if output.exists():
        raise FileExistsError(f"CP08-03 archive already exists; refusing to overwrite: {output}")
    before = report["source_hashes"]
    output.mkdir(parents=True)
    try:
        report["scenario_metrics"].to_parquet(output / "scenario_metrics.parquet", index=False)
        _surface_frame(report).to_parquet(output / "threshold_surface.parquet", index=False)
        report["selection"].to_parquet(output / "threshold_selection.parquet", index=False)
        report["fold_stability"].to_parquet(output / "fold_stability.parquet", index=False)
        (output / "concentration.json").write_text(
            json.dumps(_json_safe(report["concentration"]), indent=2) + "\n",
            encoding="utf-8",
        )
        (output / "checks.json").write_text(
            json.dumps(_json_safe(report["checks"]), indent=2) + "\n", encoding="utf-8"
        )
        summary = {
            key: value
            for key, value in report.items()
            if key
            not in {
                "source_hashes",
                "scenario_metrics",
                "surface",
                "selection",
                "fold_stability",
                "concentration",
                "checks",
                "experiments",
            }
        }
        experiments = {
            name: dict(block) for name, block in report["experiments"].items()
        }
        surface_block = dict(experiments["threshold_surface_oos"])
        surface_block.pop("surface", None)
        experiments["threshold_surface_oos"] = surface_block
        summary["experiments"] = experiments
        summary["surface_note"] = (
            "threshold_surface.parquet contiene la superficie completa; i risultati OOS sono esplorativi "
            "e non promossi."
        )
        (output / "summary.json").write_text(
            json.dumps(_json_safe(summary), indent=2, default=str) + "\n", encoding="utf-8"
        )
        after = _source_hashes(paths)
        if before != after:
            raise RuntimeError("Source artifacts changed during the read-only analysis")
        outputs = {
            "scenario_metrics": "scenario_metrics.parquet",
            "threshold_surface": "threshold_surface.parquet",
            "threshold_selection": "threshold_selection.parquet",
            "fold_stability": "fold_stability.parquet",
            "concentration": "concentration.json",
            "checks": "checks.json",
            "summary": "summary.json",
        }
        manifest = {
            "schema_version": ROBUSTNESS_SCHEMA,
            "created_utc": pd.Timestamp.now(tz="UTC").isoformat(),
            "read_only": True,
            "analysis": "S02-CP08-03 momentum parameter & execution robustness",
            "policy": POLICY,
            "initial_capital": float(report["initial_capital"]),
            "preregistration": report["preregistration"],
            "policy_frozen": {
                "threshold": FROZEN_THRESHOLD,
                "fee_rate": FROZEN_FEE,
                "slippage": FROZEN_SLIPPAGE,
                "threshold_reselected_on_oos": False,
                "training_performed": False,
                "new_inference": False,
                "data_downloaded": False,
                "promotion_on_oos": False,
            },
            "method": {
                "simulator": "reuse of S02-CP08-01 simulator_integrity_audit (contract, replay, dollar metrics, calendar curves, concentration, bootstrap)",
                "frozen_reference": "CP07 momentum_threshold @ 5%, fee 0.10%, slippage 0.05%",
                "threshold_selection": "training rows of each fold only",
                "cost_frozen": "same trade ledger, only the cost model changes (monotone by construction)",
                "policy_refit": "threshold re-selected on training for each cost scenario (cost + selection confound)",
                "latency": "entry/exit shifted on real market opens (0h/1h/2h, 1h primary)",
                "short_carry": "parametric hypotheses, SHORT only, no historical funding data",
                "mark_to_market": "hourly liquidation-value mark-to-market; carry accrues at exit only",
            },
            "source_hashes": before,
            "source_hashes_after": after,
            "source_hashes_unchanged": bool(before == after),
            "original_artifacts_modified": bool(before != after),
            "technical_gate": report["technical_gate"],
            "strategic_gate": report["strategic_gate"],
            "promotion": report["promotion"],
            "headline": {
                "trades": int(report["frozen_reference"]["trades"]),
                "total_return_pct": report["frozen_reference"]["total_return_pct"],
                "profit_factor_usdt": report["frozen_reference"]["profit_factor_usdt"],
                "max_drawdown_mtm_pct": report["frozen_reference"]["max_drawdown_mtm_pct"],
                "primary_scenario": report["primary_scenario"]["scenario"],
                "primary_total_return_pct": report["primary_scenario"]["total_return_pct"],
                "primary_max_drawdown_mtm_pct": report["primary_scenario"]["max_drawdown_mtm_pct"],
                "cost_sensitivity_monotone": report["experiments"]["cost_sensitivity_frozen"]["monotonicity"]["monotone"],
                "refit_monotone": report["experiments"]["policy_refit_sensitivity"]["monotone"],
            },
            "checks_total": len(report["checks"]),
            "checks_failed": report["checks_failed"],
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
    paths: RobustnessPaths = RobustnessPaths(),
    *,
    initial_capital: float = DEFAULT_INITIAL_CAPITAL,
    write: bool = True,
) -> dict[str, Any]:
    report = build_robustness(paths, initial_capital=initial_capital)
    if write:
        report["written"] = write_artifacts(paths, report)
    return report


def _printable(report: Mapping[str, Any]) -> dict[str, Any]:
    return _json_safe(
        {
            "policy": report["policy"],
            "frozen_reference": {
                "trades": report["frozen_reference"]["trades"],
                "total_return_pct": report["frozen_reference"]["total_return_pct"],
                "profit_factor_usdt": report["frozen_reference"]["profit_factor_usdt"],
                "max_drawdown_mtm_pct": report["frozen_reference"]["max_drawdown_mtm_pct"],
            },
            "primary_scenario": {
                "scenario": report["primary_scenario"]["scenario"],
                "delay_hours": report["primary_scenario"]["delay_hours"],
                "total_return_pct": report["primary_scenario"]["total_return_pct"],
                "profit_factor_usdt": report["primary_scenario"]["profit_factor_usdt"],
                "max_drawdown_mtm_pct": report["primary_scenario"]["max_drawdown_mtm_pct"],
                "trades": report["primary_scenario"]["trades"],
            },
            "cost_sensitivity_monotone": report["experiments"]["cost_sensitivity_frozen"]["monotonicity"]["monotone"],
            "refit_monotone": report["experiments"]["policy_refit_sensitivity"]["monotone"],
            "best_oos_threshold_exploratory": report["experiments"]["threshold_surface_oos"]["best_oos_threshold_exploratory"],
            "checks_total": len(report["checks"]),
            "checks_failed": report["checks_failed"],
            "technical_gate": report["technical_gate"],
            "strategic_gate": report["strategic_gate"],
            "promotion": report["promotion"],
        }
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="S02-CP08-03 momentum parameter & execution robustness (read-only)"
    )
    parser.add_argument("--data-root", default=str(RobustnessPaths.data_root))
    parser.add_argument("--snapshot-dir", default=str(RobustnessPaths.snapshot_dir))
    parser.add_argument("--label-dataset-dir", default=str(RobustnessPaths.label_dir))
    parser.add_argument("--regime-dataset-dir", default=str(RobustnessPaths.regime_dir))
    parser.add_argument("--trading-dir", default=str(RobustnessPaths.trading_dir))
    parser.add_argument("--audit-dir", default=str(RobustnessPaths.audit_dir))
    parser.add_argument("--stability-dir", default=str(RobustnessPaths.stability_dir))
    parser.add_argument("--output-dir", default=str(RobustnessPaths.output_dir))
    parser.add_argument("--capital", type=float, default=DEFAULT_INITIAL_CAPITAL)
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)
    paths = RobustnessPaths(
        data_root=Path(args.data_root),
        snapshot_dir=Path(args.snapshot_dir),
        label_dir=Path(args.label_dataset_dir),
        regime_dir=Path(args.regime_dataset_dir),
        trading_dir=Path(args.trading_dir),
        audit_dir=Path(args.audit_dir),
        stability_dir=Path(args.stability_dir),
        output_dir=Path(args.output_dir),
    )
    report = run(paths, initial_capital=args.capital, write=not args.no_write)
    payload = _printable(report)
    if "written" in report:
        payload["written"] = report["written"]["output_dir"]
        payload["manifest_sha256"] = digest(Path(report["written"]["output_dir"]) / "manifest.json")
    print(json.dumps(payload, indent=2, default=str))
    return 0 if report["technical_gate"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
