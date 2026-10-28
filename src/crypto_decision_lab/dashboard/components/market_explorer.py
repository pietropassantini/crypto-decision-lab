"""Market Explorer: interactive OHLCV, indicators and forecast overlay."""
from __future__ import annotations

from typing import Iterable

import pandas as pd
import streamlit as st

from ..charts import empty_figure, make_market_figure
from ..config import DashboardConfig
from ..data_loader import MarketData, filter_range, resample_ohlcv
from ..forecast_adapter import ForecastDataset, align_models, available_selected
from ..indicators import IndicatorRequest, compute_indicators
from .common import (
    availability_line,
    download_button,
    fmt_price,
    fmt_timestamp,
    show_issues,
    show_plot,
)


def _forecast_points(
    forecasts: ForecastDataset,
    *,
    models: list[str],
    mode: str,
    window: int | None,
    start: pd.Timestamp,
    end_exclusive: pd.Timestamp,
) -> pd.DataFrame:
    available = available_selected(forecasts, models)
    if not available:
        return forecasts.points.iloc[0:0]
    if mode == "cutoff_path":
        return forecasts.points_for(available, window_index=window) if window is not None else forecasts.points.iloc[0:0]
    in_range = forecasts.points[(forecasts.points["target"] >= start) & (forecasts.points["target"] < end_exclusive)]
    return align_models(in_range, available, horizon=24)


def render_market_explorer(
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
    indicator_requests: Iterable[IndicatorRequest],
    show_forecast: bool = True,
) -> None:
    requests = list(indicator_requests)
    selected_models = list(selected_models)
    price = resample_ohlcv(filter_range(market.frame, start, end_exclusive), timeframe)
    results = compute_indicators(price, requests) if requests else []
    overlays = [result for result in results if result.spec.kind == "overlay"]
    oscillators = [result for result in results if result.spec.kind == "oscillator"]
    volume_overlays = [result for result in results if result.spec.key == "volume_ma"]

    top = st.columns(4)
    top[0].metric("Barre visualizzate", f"{len(price):,}")
    top[1].metric("Prima barra", fmt_timestamp(price.index.min()))
    top[2].metric("Ultima barra", fmt_timestamp(price.index.max()))
    top[3].metric("Indicatori attivi", f"{len(requests)}")
    st.caption(
        f"Timeframe: {timeframe} · vista: {view} · gli indicatori sono calcolati sulla serie "
        f"ricampionata {timeframe}, non solo ridisegnati."
    )
    if requests:
        st.caption("Indicatori: " + " · ".join(result.label for result in results))
    if selected_models:
        st.caption("Forecast: " + availability_line(forecasts, selected_models))

    forecast_points = (
        _forecast_points(
            forecasts,
            models=selected_models,
            mode=forecast_mode,
            window=selected_window,
            start=start,
            end_exclusive=end_exclusive,
        )
        if show_forecast
        else forecasts.points.iloc[0:0]
    )
    selected_cutoff = (
        pd.Timestamp(forecast_points["cutoff"].iloc[0])
        if forecast_mode == "cutoff_path" and not forecast_points.empty
        else None
    )
    if show_forecast and forecast_points.empty:
        st.info(
            "Nessun punto forecast visibile nell'intervallo selezionato. "
            "Le serie non disponibili non vengono simulate."
        )
    show_issues(forecasts.issues, title="Avvisi forecast")

    if price.empty:
        show_plot(empty_figure("Nessuna candela nel periodo selezionato"), key="market_empty")
        return
    fig = make_market_figure(
        price,
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
        title=f"Market Explorer · BTCUSDT · {timeframe}",
        height=max(config.chart_height, 700),
    )
    show_plot(fig, key="market_explorer_chart")

    export = price.copy()
    for result in results:
        for column in result.data.columns:
            export[f"{result.label} · {column}"] = result.data[column]
    actions = st.columns([1, 1, 3])
    with actions[0]:
        download_button(
            export,
            filename=f"crypto_decision_lab_market_{timeframe}.csv",
            label="Scarica OHLCV + indicatori (CSV)",
            key="market_csv",
        )
    with actions[1]:
        if not forecast_points.empty:
            download_button(
                forecast_points,
                filename="crypto_decision_lab_forecast_points.csv",
                label="Scarica forecast visibili (CSV)",
                key="market_forecast_csv",
            )
    with actions[2]:
        st.caption(f"Ultima close selezionata: {fmt_price(float(price['close'].iloc[-1]))}")
