"""S02-CP06B dashboard view: regime analysis and selective policy results."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from ..charts import (
    make_delta_mae_histogram,
    make_policy_performance_figure,
    make_win_rate_by_regime_figure,
)
from ..config import DashboardConfig
from .common import download_button, fmt_number, fmt_price, fmt_pct, show_plot


@st.cache_data(ttl=300, show_spinner=False)
def _regime_dataset_cached(dataset_dir: str) -> pd.DataFrame:
    directory = Path(dataset_dir)
    features = pd.read_parquet(directory / "features.parquet")
    errors = pd.read_parquet(directory / "errors.parquet")
    merged = errors.merge(features.drop(columns=["cutoff"]), on="window_index", validate="one_to_one")
    merged["cutoff"] = pd.to_datetime(merged["cutoff"], utc=True)
    merged["target"] = pd.to_datetime(merged["target"], utc=True)
    return merged.sort_values("cutoff").reset_index(drop=True)


@st.cache_data(ttl=300, show_spinner=False)
def _regime_summary_cached(dataset_dir: str) -> dict:
    path = Path(dataset_dir) / "summary.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


@st.cache_data(ttl=300, show_spinner=False)
def _policy_results_cached(experiment_dir: str) -> dict:
    directory = Path(experiment_dir)
    decisions = pd.read_parquet(directory / "decisions.parquet")
    decisions["cutoff"] = pd.to_datetime(decisions["cutoff"], utc=True)
    decisions["target"] = pd.to_datetime(decisions["target"], utc=True)
    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    folds = json.loads((directory / "fold_summary.json").read_text(encoding="utf-8"))
    return {"decisions": decisions, "summary": summary, "folds": folds}


def _win_rate_table(stats: list[dict], key: str) -> pd.DataFrame:
    frame = pd.DataFrame(stats)
    if frame.empty:
        return frame
    return frame[[key, "observations", "timestral_win_rate", "delta_mae_usdt", "delta_mae_ci_low", "delta_mae_ci_high", "reliable"]]


def render_regime_policy(config: DashboardConfig) -> None:
    st.subheader("Market Regime & Selective Policy")
    dataset_dir = config.regime_dataset_dir
    experiment_dir = config.policy_results_dir
    if dataset_dir is None or not (Path(dataset_dir) / "features.parquet").is_file():
        st.info(
            "Dataset S02-CP06B non trovato. Genera `data/datasets/S02-CP06B-regime-features-v1` con "
            "`python -m crypto_decision_lab.regime_analysis`."
        )
        return
    data = _regime_dataset_cached(str(dataset_dir))
    regime_summary = _regime_summary_cached(str(dataset_dir))
    policy_available = experiment_dir is not None and (Path(experiment_dir) / "decisions.parquet").is_file()
    results = _policy_results_cached(str(experiment_dir)) if policy_available else None

    st.caption(
        "Feature calcolate esclusivamente su barre precedenti al cutoff; regimi a terzili di training; "
        "policy valutate walk-forward out-of-sample. Vista read-only."
    )
    start_default = max(data["cutoff"].min(), data["cutoff"].max() - pd.Timedelta(days=365))
    values = st.date_input(
        "Periodo (UTC)",
        value=(start_default.date(), data["cutoff"].max().date()),
        min_value=data["cutoff"].min().date(),
        max_value=data["cutoff"].max().date(),
        key="regime_policy_period",
    )
    if isinstance(values, (tuple, list)) and len(values) == 2:
        start_date, end_date = values
    else:
        start_date = end_date = values
    start = pd.Timestamp(start_date, tz="UTC")
    end_exclusive = pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1)
    period = data[(data["cutoff"] >= start) & (data["cutoff"] < end_exclusive)].copy()
    if period.empty:
        st.warning("Nessuna finestra nel periodo selezionato.")
        return

    left, right = st.columns(2)
    with left:
        show_plot(make_delta_mae_histogram(period), key="regime_delta_hist")
    with right:
        stats_frames = {}
        stats_frames["trend"] = _win_rate_table(regime_summary.get("trend_regime_stats", []), "trend_regime")
        stats_frames["vol"] = _win_rate_table(regime_summary.get("vol_regime_stats", []), "vol_regime")
        show_plot(
            make_win_rate_by_regime_figure(
                {"Trend regime": stats_frames["trend"], "Vol regime": stats_frames["vol"]}
            ),
            key="regime_win_rate",
        )

    st.markdown("##### Statistiche per regime (full sample)")
    if not regime_summary:
        st.info("Statistiche di regime non disponibili nel dataset S02-CP06B.")
    else:
        display = stats_frames["trend"].copy()
        display["timestral_win_rate"] = (display["timestral_win_rate"] * 100).round(1)
        display["delta_mae_usdt"] = display["delta_mae_usdt"].round(2)
        display["delta_mae_ci_low"] = display["delta_mae_ci_low"].round(2)
        display["delta_mae_ci_high"] = display["delta_mae_ci_high"].round(2)
        st.dataframe(display, width="stretch", hide_index=True)
        st.caption(
            "reliable=False segnala sottogruppi sotto 30 osservazioni: non trarre conclusioni. "
            "Le terzili sono full-sample a scopo descrittivo; le policy usano terzili di training."
        )
        vol_display = stats_frames["vol"].copy()
        vol_display["timestral_win_rate"] = (vol_display["timestral_win_rate"] * 100).round(1)
        st.dataframe(vol_display, width="stretch", hide_index=True)

    st.markdown("##### Errori nel tempo (target h24)")
    errors_frame = period.sort_values("target")
    show_plot(_errors_over_time_figure(errors_frame, config), key="regime_errors_time")

    if results is None:
        return
    summary = results["summary"]
    st.markdown("##### Performance policy · out-of-sample (walk-forward)")
    rows = []
    for name, payload in summary["aggregate"].items():
        out = payload["out_of_sample"]
        rows.append(
            {
                "name": name,
                "label": payload["label"] + (" · ORACLE" if payload["uses_target"] else ""),
                "mae_usdt": out["mae_usdt"],
                "delta": out["delta_mae_vs_naive_usdt"],
                "ci_low": out["delta_mae_vs_naive_ci"]["ci_low"],
                "ci_high": out["delta_mae_vs_naive_ci"]["ci_high"],
                "share_timestral": out["share_timestral"],
                "win_rate_vs_naive": out["win_rate_vs_naive"],
                "uses_target": payload["uses_target"],
            }
        )
    show_plot(make_policy_performance_figure(rows), key="policy_performance")
    display = pd.DataFrame(rows)
    display["mae_usdt"] = display["mae_usdt"].round(2)
    display["delta"] = display["delta"].round(2)
    display["ci_low"] = display["ci_low"].round(2)
    display["ci_high"] = display["ci_high"].round(2)
    st.dataframe(
        display[
            [
                "label",
                "mae_usdt",
                "delta",
                "ci_low",
                "ci_high",
                "share_timestral",
                "win_rate_vs_naive",
            ]
        ],
        width="stretch",
        hide_index=True,
    )
    st.caption(
        "MAE e delta sono out-of-sample (300 finestre di test). CI95 = moving-block bootstrap "
        "(blocco 10, 1000 draw). Oracle usa l'esito reale: limite teorico non implementabile."
    )

    st.markdown("##### In-sample vs out-of-sample")
    in_sample = pd.DataFrame(
        [
            {
                "policy": name,
                "in_sample_mae": values["mae_usdt"],
                "share_timestral": values["share_timestral"],
            }
            for name, values in summary["in_sample_full_period"].items()
        ]
    )
    out_sample = pd.DataFrame(
        [
            {"policy": name, "out_of_sample_mae": values["out_of_sample"]["mae_usdt"]}
            for name, values in summary["aggregate"].items()
        ]
    )
    comparison = in_sample.merge(out_sample, on="policy")
    comparison["overfit_gap"] = comparison["out_of_sample_mae"] - comparison["in_sample_mae"]
    st.dataframe(comparison.round(2), width="stretch", hide_index=True)
    st.caption(
        "L'in-sample è calcolato su tutte le 500 finestre con modello fittato sugli stessi dati: "
        "ottimistico per policy addestrabili. Un gap positivo indica overfitting."
    )

    st.markdown("##### Dettaglio decisioni USE_TIMESTRAL / USE_NAIVE")
    decisions = results["decisions"]
    policy_options = sorted(decisions["policy"].unique())
    chosen_policy = st.selectbox(
        "Policy",
        options=policy_options,
        index=policy_options.index("regime_rules") if "regime_rules" in policy_options else 0,
        key="regime_policy_selector",
    )
    fold_options = sorted(decisions["fold"].unique().tolist())
    chosen_fold = st.selectbox("Fold", options=fold_options, index=0, key="regime_policy_fold")
    detail = decisions[
        (decisions["policy"] == chosen_policy) & (decisions["fold"] == chosen_fold)
    ].sort_values("cutoff")
    detail = detail[
        [
            "window_index",
            "cutoff",
            "trend_regime",
            "vol_regime",
            "decision",
            "explanation",
            "chosen_price_24h",
            "actual_price_24h",
            "abs_error",
            "delta_vs_naive",
        ]
    ]
    st.dataframe(detail.round(2), width="stretch", hide_index=True)
    st.caption(
        "Le decisioni usano solo informazioni al cutoff (feature di regime e valore della previsione "
        "Timestral). L'actual è mostrato solo per valutare l'esito."
    )
    download_button(
        decisions,
        filename="crypto_decision_lab_policy_decisions.csv",
        label="Scarica decisioni policy (CSV)",
        key="policy_decisions_csv",
    )
    leakage = summary.get("leakage_checks", [])
    if leakage:
        with st.expander(f"Controlli di leakage ({len(leakage)})", expanded=False):
            for check in leakage:
                status = "PASS" if check["passed"] else "FAIL"
                st.caption(f"{status} · {check['check']} · {check.get('detail', '')}")


def _errors_over_time_figure(period: pd.DataFrame, config: DashboardConfig):
    import plotly.graph_objects as go

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=period["target"],
            y=period["abs_error_timestral"],
            mode="markers",
            name="|errore| Timestral",
            marker={"size": 5, "color": config.colors.get("timestral", "#4cc9f0")},
            hovertemplate="target %{x|%Y-%m-%d}<br>Timestral %{y:,.1f}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=period["target"],
            y=period["abs_error_naive"],
            mode="markers",
            name="|errore| Naive",
            marker={"size": 5, "color": config.colors.get("naive", "#9aa4b2")},
            hovertemplate="target %{x|%Y-%m-%d}<br>Naive %{y:,.1f}<extra></extra>",
        )
    )
    fig.update_yaxes(title_text="Errore assoluto h24 (USDT)")
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0b0e14",
        plot_bgcolor="#111722",
        font={"color": "#d7dee8"},
        height=380,
        legend={"orientation": "h", "y": 1.02, "yanchor": "bottom"},
    )
    return fig
