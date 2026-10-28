"""Forecast Comparison section: aligned metrics, errors and regimes."""
from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd
import streamlit as st

from ..charts import (
    empty_figure,
    make_actual_vs_predicted_figure,
    make_error_distribution_figure,
    make_error_over_time_figure,
    make_regime_figure,
    make_scatter_actual_predicted_figure,
)
from ..config import DashboardConfig
from ..data_loader import MarketData
from ..forecast_adapter import ALIGNMENT_KEYS, ForecastDataset, align_models, available_selected
from ..metrics import add_error_columns, assign_regimes, compute_metrics, metrics_table, regime_metrics
from .common import (
    availability_line,
    download_button,
    fmt_number,
    fmt_price,
    show_issues,
    show_plot,
)


def render_model_comparison(
    market: MarketData,
    forecasts: ForecastDataset,
    config: DashboardConfig,
    *,
    start: pd.Timestamp,
    end_exclusive: pd.Timestamp,
    selected_models: Iterable[str],
    horizon: int | None = 24,
) -> None:
    selected_models = list(selected_models)
    available = available_selected(forecasts, selected_models)
    st.subheader("Model Comparison")
    st.caption(
        "Tutte le metriche sono calcolate sui timestamp target presenti per *tutti* i modelli "
        "selezionati. Cutoff e target devono coincidere: nessuna previsione indipendente viene "
        "trattata come una traiettoria continua."
    )
    if selected_models:
        st.caption("Stato serie: " + availability_line(forecasts, selected_models))
    if not available:
        st.error("Nessun modello selezionato è disponibile nell'archivio corrente.")
        return
    if len(available) < len(selected_models):
        st.warning(
            "Alcune serie selezionate non sono disponibili e vengono escluse dal confronto, "
            "non sostituite con valori inventati."
        )

    points = forecasts.points
    points = points[(points["target"] >= start) & (points["target"] < end_exclusive)].copy()
    aligned = align_models(points, available, horizon=horizon)
    samples = int(len(aligned.drop_duplicates(ALIGNMENT_KEYS))) if not aligned.empty else 0
    metrics = metrics_table(points, available, horizon=horizon)
    if aligned.empty or metrics.empty:
        st.warning(
            "Nessun campione comune nell'intervallo selezionato. "
            "Riduci il vincolo di orizzonte o amplia il periodo temporale."
        )
        show_issues(forecasts.issues, title="Avvisi forecast")
        return

    effective_horizons = sorted(aligned["horizon_hours"].unique().tolist())
    horizon_text = (
        f"h{effective_horizons[0]}" if len(effective_horizons) == 1 else "tutti i target 1–24h"
    )
    if horizon is None and any(model in {"ridge", "histgb"} for model in available):
        st.info(
            "Ridge/HistGB sono archiviati solo a +24h: l'intersezione automatica "
            "riduce il confronto ai target +24h comuni."
        )
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Campioni comuni", f"{samples:,}", help=f"Orizzonte effettivo: {horizon_text}")
    best = metrics.dropna(subset=["mae"]).sort_values("mae")
    if not best.empty:
        c2.metric("MAE migliore", fmt_price(float(best.iloc[0]["mae"])), help=str(best.iloc[0]["model_label"]))
        c3.metric("Modello", str(best.iloc[0]["model_label"]))
    best_dir = metrics.dropna(subset=["directional_accuracy"]).sort_values("directional_accuracy", ascending=False)
    if not best_dir.empty:
        c4.metric(
            "Directional accuracy migliore",
            f"{100 * float(best_dir.iloc[0]['directional_accuracy']):.1f}%",
            help=str(best_dir.iloc[0]["model_label"]),
        )
    else:
        c4.metric("Directional accuracy", "n/d", help="Naive non ha direzione definita")

    display = metrics[
        [
            "model_label",
            "samples",
            "mae",
            "rmse",
            "bias",
            "mape",
            "directional_accuracy",
            "direction_evaluable",
            "naive_mae",
            "improvement_vs_naive_pct",
        ]
    ].copy()
    display["directional_accuracy"] = display["directional_accuracy"] * 100.0
    display = display.rename(
        columns={
            "model_label": "Modello",
            "samples": "Campioni",
            "mae": "MAE (USDT)",
            "rmse": "RMSE (USDT)",
            "bias": "Bias medio (USDT)",
            "mape": "MAPE (%)",
            "directional_accuracy": "Directional acc. (%)",
            "direction_evaluable": "Dir. valutabili",
            "naive_mae": "MAE naive (USDT)",
            "improvement_vs_naive_pct": "Migl. vs naive (%)",
        }
    )
    st.dataframe(
        display,
        width="stretch",
        hide_index=True,
        column_config={
            "MAE (USDT)": st.column_config.NumberColumn(format="%.2f"),
            "RMSE (USDT)": st.column_config.NumberColumn(format="%.2f"),
            "Bias medio (USDT)": st.column_config.NumberColumn(format="%.2f"),
            "MAPE (%)": st.column_config.NumberColumn(format="%.3f"),
            "Directional acc. (%)": st.column_config.NumberColumn(format="%.1f"),
            "MAE naive (USDT)": st.column_config.NumberColumn(format="%.2f"),
            "Migl. vs naive (%)": st.column_config.NumberColumn(format="%+.2f"),
        },
    )
    st.caption(
        f"Orizzonte effettivo: {horizon_text} · campioni comuni: {samples} · "
        "Bias = media(predicted − actual); MAPE su actual positivi; "
        "direzione misurata dal prezzo al cutoff."
    )

    residuals = add_error_columns(aligned)
    target_min, target_max = residuals["target"].min(), residuals["target"].max()
    actual_series = market.frame.loc[
        (market.frame.index >= target_min - pd.Timedelta(days=1))
        & (market.frame.index <= target_max + pd.Timedelta(days=1)),
        "close",
    ]

    left, right = st.columns(2)
    with left:
        show_plot(
            make_actual_vs_predicted_figure(
                residuals, actual_series=actual_series, colors=config.colors
            ),
            key="comparison_actual_vs_predicted",
        )
    with right:
        show_plot(
            make_error_over_time_figure(residuals, colors=config.colors),
            key="comparison_error_time",
        )
    left, right = st.columns(2)
    with left:
        show_plot(
            make_error_distribution_figure(residuals, colors=config.colors),
            key="comparison_error_distribution",
        )
    with right:
        show_plot(
            make_scatter_actual_predicted_figure(residuals, colors=config.colors),
            key="comparison_scatter",
        )

    regime_frame = assign_regimes(
        residuals,
        market.frame,
        vol_bins=config.regime_vol_bins,
        trend_bins=config.regime_trend_bins,
    )
    vol_table = regime_metrics(regime_frame, "volatility_regime", available)
    trend_table = regime_metrics(regime_frame, "trend_regime", available)
    left, right = st.columns(2)
    with left:
        show_plot(
            make_regime_figure(
                vol_table,
                regime_column="volatility_regime",
                colors=config.colors,
                title="MAE per regime di volatilità (osservabile al cutoff)",
                category_order=["Low", "Mid", "High", "Unknown"],
            ),
            key="comparison_regime_vol",
        )
    with right:
        show_plot(
            make_regime_figure(
                trend_table,
                regime_column="trend_regime",
                colors=config.colors,
                title="MAE per regime di trend (osservabile al cutoff)",
                category_order=["Down", "Sideways", "Up", "Unknown"],
            ),
            key="comparison_regime_trend",
        )
    st.caption(
        "Regimi calcolati solo con chiusure precedenti al cutoff (volatilità dei log-rendimenti "
        "su 168h e trend 168h). Nessuna informazione del target entra nella classificazione."
    )
    show_issues(forecasts.issues, title="Avvisi forecast")
    download_button(
        residuals,
        filename="crypto_decision_lab_model_comparison.csv",
        label="Scarica campioni comuni + errori (CSV)",
        key="comparison_csv",
    )
