"""Forecast Inspector: single forecast provenance and cutoff/outcome split."""
from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd
import streamlit as st

from ..charts import make_forecast_inspector_figure
from ..config import DashboardConfig
from ..data_loader import MarketData
from ..forecast_adapter import ForecastDataset, available_selected
from ..indicators import compute_indicator
from .common import (
    download_button,
    fmt_number,
    fmt_pct,
    fmt_price,
    fmt_timestamp,
    show_plot,
)


def _direction(value: float, anchor: float) -> str:
    if not np.isfinite(value) or not np.isfinite(anchor) or value == anchor:
        return "n/d"
    return "▲ Up" if value > anchor else "▼ Down"


def _last_indicator(frame: pd.DataFrame, key: str, params: dict) -> float | None:
    if frame.empty:
        return None
    try:
        value = compute_indicator(frame, key, params).iloc[-1, 0]
        return float(value) if pd.notna(value) else None
    except Exception:
        return None


def _features_at_cutoff(market: pd.DataFrame, cutoff: pd.Timestamp) -> dict[str, float | None]:
    """Compute only information available strictly before the cutoff."""

    history = market.loc[market.index < cutoff].tail(500)
    if history.empty:
        return {}
    close = history["close"].astype(float)
    features: dict[str, float | None] = {
        "close_cutoff": float(close.iloc[-1]) if not close.empty else None,
        "SMA 20 (1h)": _last_indicator(history, "sma", {"window": 20}),
        "EMA 50 (1h)": _last_indicator(history, "ema", {"window": 50}),
        "RSI 14 (1h)": _last_indicator(history, "rsi", {"period": 14}),
        "ATR 14 (1h)": _last_indicator(history, "atr", {"period": 14}),
        "Rolling vol 168h": _last_indicator(
            history, "rolling_volatility", {"window": 168, "annualize": False, "periods_per_year": 0}
        ),
    }
    if len(close) >= 169:
        features["Trend 168h"] = float(close.iloc[-1] / close.iloc[-169] - 1.0)
    else:
        features["Trend 168h"] = None
    if len(close) >= 25:
        features["Momentum 24h"] = float(close.iloc[-1] / close.iloc[-25] - 1.0)
    else:
        features["Momentum 24h"] = None
    return features


def _window_options(forecasts: ForecastDataset) -> list[tuple[int, str]]:
    windows = forecasts.windows
    if windows.empty:
        return []
    options = []
    for row in windows.sort_values("cutoff", ascending=False).itertuples(index=False):
        options.append(
            (
                int(row.window_index),
                f"window-{int(row.window_index):04d} · cutoff {fmt_timestamp(row.cutoff)}",
            )
        )
    return options


def render_forecast_inspector(
    market: MarketData,
    forecasts: ForecastDataset,
    config: DashboardConfig,
    *,
    selected_models: Iterable[str],
    selected_window: int | None,
) -> None:
    st.subheader("Forecast Inspector")
    options = _window_options(forecasts)
    if not options:
        st.warning("Nessuna finestra forecast archiviata disponibile.")
        return
    option_indices = [index for index, _ in options]
    default_index = option_indices.index(selected_window) if selected_window in option_indices else 0
    window = st.selectbox(
        "Finestra / cutoff",
        options=options,
        index=default_index,
        format_func=lambda item: item[1],
        key="inspector_window",
    )
    window_index = int(window[0])
    window_points = forecasts.points[forecasts.points["window_index"] == window_index].copy()
    if window_points.empty:
        st.error("La finestra selezionata non contiene righe forecast.")
        return
    available = available_selected(forecasts, list(selected_models)) or forecasts.available_models()
    horizons = sorted(window_points[window_points["model"].isin(available)]["horizon_hours"].unique().tolist())
    if not horizons:
        st.warning("Nessun modello disponibile per questa finestra.")
        return
    default_horizon = 24 if 24 in horizons else horizons[-1]
    horizon = st.selectbox(
        "Target / orizzonte",
        options=horizons,
        index=horizons.index(default_horizon),
        format_func=lambda value: f"h{int(value)}",
        key="inspector_horizon",
    )
    model_rows = window_points[
        (window_points["horizon_hours"] == int(horizon))
        & (window_points["model"].isin(available))
    ].copy()
    if model_rows.empty:
        st.warning("Nessun modello selezionato ha un valore su questo target.")
        return
    preferred = [model for model in ("timestral", "naive", "ridge", "histgb", "decision") if model in set(model_rows["model"])]
    primary_model = st.selectbox(
        "Modello da ispezionare",
        options=preferred,
        index=0,
        format_func=lambda model: forecasts.models[model].label if model in forecasts.models else model,
        key="inspector_model",
    )
    primary = model_rows[model_rows["model"] == primary_model].iloc[0]
    cutoff = pd.Timestamp(primary["cutoff"])
    target = pd.Timestamp(primary["target"])
    anchor = float(primary["anchor_price"]) if pd.notna(primary["anchor_price"]) else np.nan
    if not np.isfinite(anchor):
        previous = market.frame.loc[market.frame.index < cutoff]
        anchor = float(previous["close"].iloc[-1]) if not previous.empty else np.nan
    predicted = float(primary["predicted"]) if pd.notna(primary["predicted"]) else np.nan
    actual = float(primary["actual"]) if pd.notna(primary["actual"]) else np.nan
    residual = actual - predicted if np.isfinite(actual) and np.isfinite(predicted) else np.nan
    absolute_error = abs(residual) if np.isfinite(residual) else np.nan
    pct_error = absolute_error / actual * 100.0 if np.isfinite(absolute_error) and np.isfinite(actual) and actual != 0 else np.nan
    naive_row = window_points[
        (window_points["model"] == "naive")
        & (window_points["window_index"] == window_index)
        & (window_points["horizon_hours"] == int(horizon))
        & (window_points["target"] == target)
    ]
    naive = float(naive_row["predicted"].iloc[0]) if not naive_row.empty else np.nan

    st.markdown(
        f"**{primary['model_label']}** · sample `{primary['sample_id']}` · "
        f"target **{fmt_timestamp(target)}** (h{int(horizon)})"
    )
    left, right = st.columns(2)
    with left:
        st.markdown(
            '<div style="border-left:3px solid #4cc9f0;padding-left:10px;">'
            '<b style="color:#4cc9f0;">OSSERVABILE AL CUTOFF</b></div>',
            unsafe_allow_html=True,
        )
        st.metric("Cutoff / generazione", fmt_timestamp(cutoff))
        st.metric("Prezzo al cutoff (ultima close nota)", fmt_price(anchor))
        st.metric("Previsione modello", fmt_price(predicted))
        st.metric("Baseline naive", fmt_price(naive))
        st.caption(
            f"Target timestamp: {fmt_timestamp(target)} · orizzonte h{int(horizon)} · "
            f"relazione: cutoff + {int(horizon) - 1}h (la 24ª barra cade a cutoff+23h)."
        )
    with right:
        st.markdown(
            '<div style="border-left:3px solid #f0b90b;padding-left:10px;">'
            '<b style="color:#f0b90b;">CONOSCIUTO A POSTERIORI</b></div>',
            unsafe_allow_html=True,
        )
        st.metric("Prezzo reale al target", fmt_price(actual))
        st.metric("Residuo (actual − predicted)", fmt_price(residual))
        st.metric("Errore assoluto / percentuale", f"{fmt_price(absolute_error)} · {fmt_number(pct_error, 3)}%")
        st.metric(
            "Direzione prevista / reale",
            f"{_direction(predicted, anchor)} / {_direction(actual, anchor)}",
        )

    st.markdown("#### Indicator values al cutoff")
    features = _features_at_cutoff(market.frame, cutoff)
    if not features:
        st.caption("Storico insufficiente per calcolare gli indicatori al cutoff.")
    else:
        feature_rows = []
        for key, value in features.items():
            if key == "close_cutoff":
                continue
            if key in {"Trend 168h", "Momentum 24h"} and value is not None:
                shown = fmt_pct(100 * float(value), 3)
            else:
                shown = fmt_number(value, 4)
            feature_rows.append({"indicatore": key, "valore al cutoff": shown})
        st.dataframe(pd.DataFrame(feature_rows), width="stretch", hide_index=True)
    st.caption(
        "Questi valori usano esclusivamente chiusure con timestamp strettamente precedente al cutoff. "
        "Non vengono usati actual o target futuri."
    )

    window_models = window_points[window_points["model"].isin(available)]
    show_plot(
        make_forecast_inspector_figure(
            market.frame,
            window_models,
            target=target,
            cutoff=cutoff,
            colors=config.colors,
        ),
        key="inspector_chart",
    )

    st.markdown("#### Output per modello sullo stesso target")
    table = model_rows[
        ["model_label", "model", "window_index", "cutoff", "target", "horizon_hours", "predicted", "actual", "anchor_price"]
    ].copy()
    table["error_predicted_minus_actual"] = table["predicted"] - table["actual"]
    table["residual_actual_minus_predicted"] = table["actual"] - table["predicted"]
    st.dataframe(table, width="stretch", hide_index=True)

    with st.expander("Metadati e provenienza", expanded=False):
        st.json(
            {
                "model": primary["model"],
                "model_label": primary["model_label"],
                "model_version": primary["model_version"],
                "experiment": primary["experiment"],
                "window_index": int(primary["window_index"]),
                "sample_id": primary["sample_id"],
                "cutoff": str(cutoff),
                "target": str(target),
                "horizon_hours": int(horizon),
                "source": primary["source"],
                "forecast_sha256": primary["forecast_sha256"] if pd.notna(primary["forecast_sha256"]) else None,
                "snapshot_id": forecasts.snapshot_id,
                "snapshot_source_manifest_sha256": forecasts.snapshot_sha256,
                "archive_run": forecasts.runs,
            }
        )
    download_button(
        model_rows,
        filename=f"crypto_decision_lab_inspector_window_{window_index:04d}_h{int(horizon)}.csv",
        label="Scarica dettaglio previsione (CSV)",
        key="inspector_csv",
    )
