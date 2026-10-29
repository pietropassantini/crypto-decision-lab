"""S02-CP07 dashboard view: offline trading simulation (read-only)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

from ..charts import (
    make_drawdown_figure,
    make_equity_curve_figure,
    make_policy_return_figure,
    make_sensitivity_heatmap,
    make_trade_signal_figure,
)
from ..config import DashboardConfig
from .common import download_button, fmt_number, fmt_price, show_plot

POLICY_LABELS = {
    "always_no_trade": "Always NO_TRADE",
    "always_long": "Always LONG",
    "always_short": "Always SHORT",
    "timestral_threshold": "Timestral threshold",
    "momentum_threshold": "Momentum threshold",
    "oracle_retrospective": "Oracle retrospective (theoretical)",
}


@st.cache_data(ttl=300, show_spinner=False)
def _trading_cached(experiment_dir: str) -> dict:
    directory = Path(experiment_dir)
    decisions = pd.read_parquet(directory / "decisions.parquet")
    decisions["cutoff"] = pd.to_datetime(decisions["cutoff"], utc=True)
    decisions["entry_timestamp"] = pd.to_datetime(decisions["entry_timestamp"], utc=True)
    decisions["exit_timestamp"] = pd.to_datetime(decisions["exit_timestamp"], utc=True)
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    sensitivity = json.loads((directory / "sensitivity.json").read_text(encoding="utf-8"))
    folds = json.loads((directory / "fold_summary.json").read_text(encoding="utf-8"))
    return {
        "decisions": decisions,
        "summary": summary,
        "sensitivity": sensitivity,
        "folds": folds,
    }


def _curves(decisions: pd.DataFrame, initial_capital: float) -> dict[str, pd.Series]:
    curves = {}
    for policy, group in decisions.groupby("policy"):
        ordered = group.sort_values("cutoff")
        equity = initial_capital * np.cumprod(1.0 + ordered["net_return_pct"].to_numpy(dtype=float) / 100.0)
        index = pd.DatetimeIndex(ordered["exit_timestamp"], name="exit_timestamp")
        series = pd.Series(equity, index=index)
        series = pd.concat([pd.Series([initial_capital], index=[series.index[0] - pd.Timedelta(hours=1)]), series])
        curves[policy] = series
    return curves


def render_trading_simulation(config: DashboardConfig) -> None:
    st.subheader("Trading Simulation · S02-CP07")
    experiment_dir = config.trading_experiment_dir
    if experiment_dir is None or not (Path(experiment_dir) / "decisions.parquet").is_file():
        st.info(
            "Esperimento S02-CP07 non trovato. Genera label e simulazione con "
            "`python -m crypto_decision_lab.decision_labeling` e "
            "`python -m crypto_decision_lab.trading_policy`."
        )
        return
    payload = _trading_cached(str(experiment_dir))
    decisions = payload["decisions"]
    summary = payload["summary"]
    initial_capital = float(summary.get("initial_capital", 10_000.0))
    st.caption(
        "Simulazione offline deterministica: una posizione alla volta, 100% equity, nessuna leva. "
        "Tutte le metriche mostrate sono walk-forward out-of-sample. L'oracle usa il futuro ed è "
        "un limite teorico, non una policy implementabile."
    )
    st.caption(
        f"Costi base: fee {summary.get('cost_model', {}).get('fee_rate', 0):.4%} per side · "
        f"slippage {summary.get('cost_model', {}).get('slippage', 0):.4%} per side · "
        f"capitale iniziale {fmt_price(initial_capital)}"
    )

    values = st.date_input(
        "Periodo OOS (UTC)",
        value=(decisions["cutoff"].min().date(), decisions["cutoff"].max().date()),
        min_value=decisions["cutoff"].min().date(),
        max_value=decisions["cutoff"].max().date(),
        key="trading_period",
    )
    if isinstance(values, (tuple, list)) and len(values) == 2:
        start_date, end_date = values
    else:
        start_date = end_date = values
    start = pd.Timestamp(start_date, tz="UTC")
    end_exclusive = pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1)
    period = decisions[(decisions["cutoff"] >= start) & (decisions["cutoff"] < end_exclusive)].copy()
    if period.empty:
        st.warning("Nessuna decisione nel periodo selezionato.")
        return

    curves = _curves(period, initial_capital)
    show_plot(
        make_equity_curve_figure(
            {POLICY_LABELS.get(name, name): curve for name, curve in curves.items()},
            title="Equity curve · out-of-sample walk-forward",
        ),
        key="trading_equity",
    )

    policies = sorted(period["policy"].unique())
    selected = st.selectbox(
        "Policy",
        options=policies,
        index=policies.index("momentum_threshold") if "momentum_threshold" in policies else 0,
        format_func=lambda value: POLICY_LABELS.get(value, value),
        key="trading_policy_select",
    )
    selected_decisions = period[period["policy"] == selected].sort_values("cutoff")
    metrics = summary["aggregate"][selected]["out_of_sample"]
    columns = st.columns(5)
    columns[0].metric("Total return OOS", fmt_number(metrics["total_return_pct"], 2) + "%")
    columns[1].metric("Max drawdown", fmt_number(metrics["max_drawdown_pct"], 2) + "%")
    columns[2].metric("Trades", f"{metrics['trades']:,}", help=f"NO_TRADE: {metrics['no_trade']}")
    columns[3].metric("Win rate", fmt_number(metrics["win_rate_pct"], 1) + "%")
    profit_factor = metrics.get("profit_factor")
    columns[4].metric(
        "Profit factor",
        "∞" if profit_factor == float("inf") else (fmt_number(profit_factor, 2) if profit_factor is not None else "n/d"),
    )
    if summary["aggregate"][selected]["uses_target"]:
        st.warning("Questa è la policy oracle: usa l'esito reale, non è implementabile.")

    left, right = st.columns(2)
    with left:
        show_plot(
            make_drawdown_figure(curves[selected], name=POLICY_LABELS.get(selected, selected)),
            key="trading_drawdown",
        )
    with right:
        show_plot(
            make_trade_signal_figure(selected_decisions, policy=POLICY_LABELS.get(selected, selected)),
            key="trading_signals",
        )

    st.markdown("##### Performance per policy · OOS")
    rows = []
    for name, payload_item in summary["aggregate"].items():
        out = payload_item["out_of_sample"]
        rows.append(
            {
                "name": name,
                "label": POLICY_LABELS.get(name, name),
                "total_return": out["total_return_pct"],
                "trades": out["trades"],
                "win_rate": out["win_rate_pct"] / 100.0,
                "max_drawdown": out["max_drawdown_pct"],
                "ci_low": out["avg_net_return_ci"]["ci_low"],
                "ci_high": out["avg_net_return_ci"]["ci_high"],
                "uses_target": payload_item["uses_target"],
            }
        )
    show_plot(make_policy_return_figure(rows, height=380), key="trading_policy_performance")
    table = pd.DataFrame(
        [
            {
                "Policy": POLICY_LABELS.get(name, name),
                "Trades": out["trades"],
                "NO_TRADE": out["no_trade"],
                "Total return %": round(out["total_return_pct"], 2),
                "Avg net %": round(out["avg_net_return_pct"], 4),
                "Win rate %": round(out["win_rate_pct"], 2),
                "Max DD %": round(out["max_drawdown_pct"], 2),
                "Profit factor": out["profit_factor"],
                "Skips": out["skipped_signals"],
                "Uses target": payload_item["uses_target"],
            }
            for name, payload_item in summary["aggregate"].items()
            for out in [payload_item["out_of_sample"]]
        ]
    )
    st.dataframe(table, width="stretch", hide_index=True)
    st.caption("Δ return nel grafico = total return % della policy; il MAE non è applicabile al P&L.")

    st.markdown("##### Sensitivity · fee × slippage (total return %)")
    sensitivity = payload["sensitivity"]
    show_plot(
        make_sensitivity_heatmap(sensitivity["scenarios"], policy=selected),
        key="trading_sensitivity",
    )
    scenario_rows = []
    for scenario in sensitivity["scenarios"]:
        scenario_rows.append(
            {
                "fee": scenario["fee_rate"],
                "slippage": scenario["slippage"],
                **scenario["total_return_pct"],
            }
        )
    st.dataframe(pd.DataFrame(scenario_rows).round(2), width="stretch", hide_index=True)

    st.markdown("##### Trade log")
    log = selected_decisions[
        [
            "window_index",
            "cutoff",
            "entry_timestamp",
            "exit_timestamp",
            "action",
            "reference_price",
            "execution_price",
            "exit_price",
            "gross_return_pct",
            "net_return_pct",
            "regime",
            "threshold",
        ]
    ]
    st.dataframe(log.round(4), width="stretch", hide_index=True)
    download_button(
        selected_decisions,
        filename=f"crypto_decision_lab_{selected}_trades.csv",
        label="Scarica trade log (CSV)",
        key="trading_trades_csv",
    )
    with st.expander("Verdetto e controlli di leakage", expanded=False):
        verdict = summary.get("verdict", {})
        st.write(f"**Verdetto:** {verdict.get('verdict')} — {verdict.get('reason')}")
        for check in summary.get("leakage_checks", []):
            st.caption(f"{'PASS' if check['passed'] else 'FAIL'} · {check['check']} · {check.get('detail', '')}")

    st.markdown("##### Dettaglio decisione selezionata")
    single = st.selectbox(
        "Window",
        options=selected_decisions["window_index"].tolist(),
        index=0,
        key="trading_window_select",
    )
    row = selected_decisions[selected_decisions["window_index"] == single].iloc[0]
    detail = st.columns(4)
    detail[0].metric("Azione", row["action"])
    detail[1].metric("Reference price", fmt_price(row["reference_price"]))
    detail[2].metric("Execution price", fmt_price(row["execution_price"]) if pd.notna(row["execution_price"]) else "n/d")
    detail[3].metric("Net return", f"{row['net_return_pct']:+.3f}%")
    st.caption(
        f"Cutoff {row['cutoff']} · entry {row['entry_timestamp']} · exit {row['exit_timestamp']} · "
        f"regime {row['regime']} · threshold {row['threshold']}"
    )
