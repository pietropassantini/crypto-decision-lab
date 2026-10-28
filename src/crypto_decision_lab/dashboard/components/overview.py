"""Overview section: dataset status, summary metrics and main chart."""
from __future__ import annotations

from typing import Iterable

import pandas as pd
import streamlit as st

from ..charts import empty_figure, make_market_figure
from ..config import DashboardConfig
from ..data_loader import MarketData, display_frame_with_gaps, filter_range, latest_candle, resample_ohlcv
from ..forecast_adapter import (
    ForecastDataset,
    align_models,
    available_selected,
    common_targets,
)
from ..indicators import IndicatorRequest, compute_indicators
from ..metrics import metrics_table
from .common import (
    availability_line,
    download_button,
    fmt_number,
    fmt_price,
    fmt_timestamp,
    read_only_banner,
    show_issues,
    show_plot,
)


def _range_points(forecasts: ForecastDataset, start, end_exclusive) -> pd.DataFrame:
    data = forecasts.points
    if data.empty:
        return data
    return data[(data["target"] >= start) & (data["target"] < end_exclusive)].copy()


def render_overview(
    market: MarketData,
    forecasts: ForecastDataset,
    config: DashboardConfig,
    *,
    start: pd.Timestamp,
    end_exclusive: pd.Timestamp,
    timeframe: str,
    view: str,
    selected_models: Iterable[str],
    forecast_mode: str,
    selected_window: int | None,
    indicator_requests: Iterable[IndicatorRequest] = (),
) -> None:
    read_only_banner(market.latest_timestamp)
    selected_models = list(selected_models)
    available = available_selected(forecasts, selected_models)
    range_points = _range_points(forecasts, start, end_exclusive)
    metric_models = [model for model in ("timestral", "naive") if model in forecasts.models]
    metrics = metrics_table(range_points, metric_models, horizon=24) if metric_models else pd.DataFrame()
    timestral = metrics[metrics["model"] == "timestral"] if not metrics.empty else pd.DataFrame()
    naive = metrics[metrics["model"] == "naive"] if not metrics.empty else pd.DataFrame()
    timestral_mae = float(timestral["mae"].iloc[0]) if not timestral.empty else float("nan")
    naive_mae = float(naive["mae"].iloc[0]) if not naive.empty else float("nan")
    improvement = (
        float(timestral["improvement_vs_naive_pct"].iloc[0])
        if not timestral.empty and pd.notna(timestral["improvement_vs_naive_pct"].iloc[0])
        else float("nan")
    )
    samples = int(timestral["samples"].iloc[0]) if not timestral.empty else 0
    timestral_samples = (
        int(range_points[range_points["model"] == "timestral"].drop_duplicates("sample_id").shape[0])
        if not range_points.empty and "timestral" in set(range_points["model"])
        else 0
    )
    latest = latest_candle(market.frame)
    latest_ts = market.frame.index.max()

    columns = st.columns(5)
    columns[0].metric("Ultima candela dataset", fmt_timestamp(latest_ts), help="Non è un prezzo live.")
    columns[1].metric("Close ultima candela", fmt_price(float(latest["close"])))
    columns[2].metric("Campioni forecast", f"{timestral_samples:,}", help="Righe Timestral nell'intervallo selezionato.")
    columns[3].metric(
        "MAE Timestral h24",
        fmt_price(timestral_mae),
        help=f"Target +24h comuni a Timestral/naive · {samples} campioni",
    )
    columns[4].metric(
        "MAE naive h24",
        fmt_price(naive_mae),
        help=f"Target +24h comuni a Timestral/naive · {samples} campioni",
    )
    if pd.notna(improvement):
        st.caption(
            f"Miglioramento MAE di Timestral vs naive sui target +24h comuni: "
            f"{fmt_number(improvement, 2)}% · campioni: {samples} · "
            f"run: {forecasts.snapshot_id or 'snapshot non dichiarato'}"
        )
    else:
        st.caption("Miglioramento vs naive non calcolabile nell'intervallo selezionato.")
    show_issues(forecasts.issues, title="Avvisi forecast")

    chart_start, chart_end = start, end_exclusive
    notes = []
    if forecast_mode == "cutoff_path" and selected_window is not None:
        window = forecasts.windows[forecasts.windows["window_index"] == int(selected_window)]
        if not window.empty:
            cutoff = pd.Timestamp(window.iloc[0]["cutoff"])
            chart_start = min(chart_start, cutoff - pd.Timedelta(days=14))
            chart_end = max(chart_end, cutoff + pd.Timedelta(days=2))
            notes.append("Intervallo esteso per mostrare la finestra di cutoff selezionata.")

    chart_market = filter_range(market.frame, chart_start, chart_end)
    chart_market = resample_ohlcv(chart_market, timeframe)
    indicator_results = compute_indicators(chart_market, list(indicator_requests)) if indicator_requests else []
    overlays = [result for result in indicator_results if result.spec.kind == "overlay"]
    oscillators = [result for result in indicator_results if result.spec.kind == "oscillator"]
    volume_overlays = [result for result in indicator_results if result.spec.kind == "volume" and result.spec.key == "volume_ma"]

    forecast_points = range_points.copy()
    if forecast_mode == "cutoff_path":
        forecast_points = (
            forecasts.points_for(available, window_index=selected_window)
            if selected_window is not None
            else forecast_points.iloc[0:0]
        )
        selected_cutoff = (
            pd.Timestamp(forecast_points["cutoff"].iloc[0]) if not forecast_points.empty else None
        )
    else:
        if available:
            forecast_points = align_models(range_points, available, horizon=24)
        else:
            forecast_points = forecast_points.iloc[0:0]
        selected_cutoff = None

    if selected_models and not available:
        st.warning("Nessuna delle serie selezionate è disponibile nell'archivio corrente.")
    elif len(available) < len(selected_models):
        st.info("Serie non disponibili: " + availability_line(forecasts, selected_models))

    if forecast_points.empty:
        notes.append("Nessun punto forecast cade nell'intervallo/target selezionato: la serie non viene inventata.")
    for note in notes:
        st.caption(note)

    fig = make_market_figure(
        chart_market,
        timeframe=timeframe,
        view=view,
        overlays=overlays,
        oscillators=oscillators,
        volume_overlays=volume_overlays,
        show_volume=True,
        forecast_points=forecast_points,
        forecast_mode=forecast_mode,
        selected_cutoff=selected_cutoff,
        colors=config.colors,
        title="Bitcoin Forecast Explorer · BTCUSDT",
        height=config.chart_height,
    )
    show_plot(fig, key="overview_main_chart")

    download_frame = chart_market.copy()
    if not forecast_points.empty:
        download_frame = download_frame.join(
            forecast_points.pivot_table(
                index="target", columns="model", values="predicted", aggfunc="last"
            ),
            how="outer",
        )
    download_button(
        download_frame,
        filename="crypto_decision_lab_overview.csv",
        label="Scarica dati vista (CSV)",
        key="overview_csv",
    )
    st.caption(
        "Il grafico PNG è disponibile dalla toolbar Plotly. La linea reale può interrompersi "
        "in corrispondenza delle ore mancanti nel dataset; nessun valore è interpolato."
    )
