"""Plotly figures for the dashboard.

Forecast traces are deliberately *not* connected across independent windows.
Only a single selected cutoff may be drawn as a 1..24h path.
"""
from __future__ import annotations

from typing import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from .data_loader import display_frame_with_gaps
from .indicators import IndicatorResult

BACKGROUND = "#0b0e14"
PANEL = "#111722"
GRID = "#253044"
TEXT = "#d7dee8"
MUTED = "#8b98ab"
GREEN = "#2dd4bf"
RED = "#ff6b6b"


def model_color(model: str, colors: Mapping[str, str]) -> str:
    return colors.get(model, "#c7d2fe")


def _apply_layout(fig: go.Figure, title: str, height: int, *, legend: bool = True) -> go.Figure:
    fig.update_layout(
        template="plotly_dark",
        title={
            "text": title,
            "x": 0.01,
            "xanchor": "left",
            "y": 0.985,
            "yanchor": "top",
            "font": {"size": 18, "color": TEXT},
        },
        paper_bgcolor=BACKGROUND,
        plot_bgcolor=PANEL,
        font={"family": "Inter, Segoe UI, sans-serif", "color": TEXT, "size": 12},
        margin={"l": 56, "r": 24, "t": 108, "b": 42},
        hovermode="x unified",
        hoverlabel={"bgcolor": "#151d2b", "font": {"color": TEXT}},
        showlegend=legend,
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.0,
            "xanchor": "left",
            "x": 0.0,
            "bgcolor": "rgba(0,0,0,0)",
        },
        height=height,
    )
    fig.update_xaxes(gridcolor=GRID, zerolinecolor=GRID, linecolor=GRID, tickfont={"color": MUTED})
    fig.update_yaxes(gridcolor=GRID, zerolinecolor=GRID, linecolor=GRID, tickfont={"color": MUTED})
    return fig


def empty_figure(message: str, *, height: int = 360) -> go.Figure:
    fig = go.Figure()
    fig.add_annotation(
        text=message,
        x=0.5,
        y=0.5,
        xref="paper",
        yref="paper",
        showarrow=False,
        font={"size": 15, "color": MUTED},
    )
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return _apply_layout(fig, "", height, legend=False)


def _hover_forecast() -> str:
    return (
        "<b>%{fullData.name}</b><br>"
        "Target: %{x|%Y-%m-%d %H:%M UTC}<br>"
        "Price: %{y:,.2f} USDT<br>"
        "Cutoff: %{customdata[0]|%Y-%m-%d %H:%M UTC}<br>"
        "Horizon: h%{customdata[1]}<br>"
        "Window: %{customdata[2]}<br>"
        "Source: %{customdata[3]}<extra></extra>"
    )


def _forecast_customdata(frame: pd.DataFrame) -> np.ndarray:
    return np.column_stack(
        [
            frame["cutoff"].astype(str).to_numpy(),
            frame["horizon_hours"].astype(int).to_numpy(),
            frame["window_index"].astype(int).to_numpy(),
            frame["model_version"].astype(str).to_numpy(),
        ]
    )


def _add_forecast_traces(
    fig: go.Figure,
    points: pd.DataFrame,
    *,
    mode: str,
    colors: Mapping[str, str],
    row: int = 1,
) -> None:
    if points.empty:
        return
    for model, group in points.groupby("model", sort=False):
        group = group.sort_values(["target", "horizon_hours"])
        color = model_color(model, colors)
        name = str(group["model_label"].iloc[0])
        custom = _forecast_customdata(group)
        if mode == "cutoff_path":
            marker_only = model in {"ridge", "histgb", "decision"} or len(group) == 1
            fig.add_trace(
                go.Scatter(
                    x=group["target"],
                    y=group["predicted"],
                    mode="markers" if marker_only else "lines+markers",
                    name=name,
                    line={"color": color, "width": 2, "dash": "dot" if model == "naive" else "solid"},
                    marker={"size": 7 if marker_only else 6, "color": color},
                    customdata=custom,
                    hovertemplate=_hover_forecast(),
                ),
                row=row,
                col=1,
            )
        else:
            # h24_common and all_targets: independent forecasts, markers only.
            fig.add_trace(
                go.Scatter(
                    x=group["target"],
                    y=group["predicted"],
                    mode="markers",
                    name=name,
                    marker={"size": 7, "color": color, "symbol": "circle", "line": {"width": 0}},
                    customdata=custom,
                    hovertemplate=_hover_forecast(),
                ),
                row=row,
                col=1,
            )


def _add_timestral_band(
    fig: go.Figure, points: pd.DataFrame, *, colors: Mapping[str, str], row: int = 1
) -> None:
    subset = points[(points["model"] == "timestral") & points["lower"].notna() & points["upper"].notna()]
    if subset.empty:
        return
    subset = subset.sort_values("target")
    color = model_color("timestral", colors)
    fig.add_trace(
        go.Scatter(
            x=subset["target"],
            y=subset["upper"],
            mode="lines",
            line={"width": 0},
            hoverinfo="skip",
            showlegend=False,
            name="Timestral p90",
        ),
        row=row,
        col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=subset["target"],
            y=subset["lower"],
            mode="lines",
            line={"width": 0},
            fill="tonexty",
            fillcolor=_with_alpha(color, 0.12),
            hoverinfo="skip",
            showlegend=False,
            name="Timestral p10",
        ),
        row=row,
        col=1,
    )


def _with_alpha(hex_color: str, alpha: float) -> str:
    color = hex_color.lstrip("#")
    if len(color) != 6:
        return hex_color
    red, green, blue = (int(color[index:index + 2], 16) for index in (0, 2, 4))
    return f"rgba({red},{green},{blue},{alpha})"


def _add_overlays(
    fig: go.Figure, overlays: Sequence[IndicatorResult], colors: Mapping[str, str]
) -> None:
    palette = ["#38bdf8", "#f472b6", "#facc15", "#a3e635", "#c084fc", "#fb923c", "#22d3ee"]
    for index, result in enumerate(overlays):
        color = palette[index % len(palette)]
        for column_index, column in enumerate(result.data.columns):
            trace_color = color if column_index == 0 else palette[(index + column_index) % len(palette)]
            if result.spec.key == "bollinger":
                trace_color = "#38bdf8" if column == "bb_mid" else "#64748b"
            fill = "tonexty" if result.spec.key == "bollinger" and column == "bb_lower" else None
            fig.add_trace(
                go.Scatter(
                    x=result.data.index,
                    y=result.data[column],
                    mode="lines",
                    name=f"{result.label} · {column}",
                    line={"color": trace_color, "width": 1.4},
                    fill=fill,
                    fillcolor=_with_alpha(trace_color, 0.08) if fill else None,
                    hovertemplate=f"<b>{result.label}</b><br>%{{x|%Y-%m-%d %H:%M}}<br>%{{y:,.4f}}<extra></extra>",
                ),
                row=1,
                col=1,
            )


def make_market_figure(
    market: pd.DataFrame,
    *,
    timeframe: str,
    view: str,
    overlays: Sequence[IndicatorResult] = (),
    oscillators: Sequence[IndicatorResult] = (),
    volume_overlays: Sequence[IndicatorResult] = (),
    show_volume: bool = True,
    forecast_points: pd.DataFrame | None = None,
    forecast_mode: str = "h24_common",
    selected_cutoff: pd.Timestamp | None = None,
    colors: Mapping[str, str] | None = None,
    title: str = "BTCUSDT · market explorer",
    height: int = 650,
    show_interval: bool = True,
) -> go.Figure:
    """Main price/candlestick chart with overlays and target-aligned forecasts."""

    if market.empty:
        return empty_figure("Nessuna candela disponibile nell'intervallo selezionato", height=height)
    colors = colors or {}
    panel_count = len(oscillators) + (1 if show_volume else 0)
    rows = 1 + panel_count
    row_heights = [0.58] + ([0.42 / panel_count] * panel_count if panel_count else [])
    fig = make_subplots(
        rows=rows,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.035 if rows > 1 else 0.0,
        row_heights=row_heights,
    )
    display = display_frame_with_gaps(market, timeframe)
    if view == "candlestick":
        fig.add_trace(
            go.Candlestick(
                x=market.index,
                open=market["open"],
                high=market["high"],
                low=market["low"],
                close=market["close"],
                name="BTCUSDT OHLC",
                increasing_line_color=GREEN,
                increasing_fillcolor=_with_alpha(GREEN, 0.45),
                decreasing_line_color=RED,
                decreasing_fillcolor=_with_alpha(RED, 0.45),
                hovertext=[
                    f"O {row.open:,.2f} · H {row.high:,.2f} · L {row.low:,.2f} · C {row.close:,.2f}"
                    for row in market.itertuples()
                ],
                hoverinfo="x+text",
            ),
            row=1,
            col=1,
        )
    else:
        fig.add_trace(
            go.Scatter(
                x=display.index,
                y=display["close"],
                mode="lines",
                name="BTCUSDT close",
                line={"color": model_color("actual", colors), "width": 1.8},
                connectgaps=False,
                hovertemplate="BTCUSDT close<br>%{x|%Y-%m-%d %H:%M UTC}<br>%{y:,.2f} USDT<extra></extra>",
            ),
            row=1,
            col=1,
        )

    _add_overlays(fig, overlays, colors)
    if forecast_points is not None and not forecast_points.empty:
        if show_interval and forecast_mode == "cutoff_path":
            _add_timestral_band(fig, forecast_points, colors=colors)
        _add_forecast_traces(fig, forecast_points, mode=forecast_mode, colors=colors)

    if forecast_mode == "cutoff_path" and selected_cutoff is not None:
        target_end = selected_cutoff + pd.Timedelta(hours=24)
        fig.add_vrect(
            x0=selected_cutoff,
            x1=target_end,
            fillcolor=_with_alpha("#4cc9f0", 0.06),
            line_width=0,
            row=1,
            col=1,
        )
        fig.add_vline(
            x=selected_cutoff,
            line_width=1,
            line_dash="dash",
            line_color="#4cc9f0",
            row=1,
            col=1,
        )
        fig.add_annotation(
            x=selected_cutoff,
            y=1.0,
            yref="paper",
            text=f"cutoff · 24 target orari",
            showarrow=False,
            xanchor="left",
            yanchor="bottom",
            font={"size": 11, "color": "#4cc9f0"},
        )
    elif forecast_points is not None and not forecast_points.empty:
        fig.add_annotation(
            x=0.01,
            y=1.01,
            xref="paper",
            yref="paper",
            text="Punti forecast sui rispettivi timestamp target · nessuna linea tra finestre indipendenti",
            showarrow=False,
            xanchor="left",
            font={"size": 11, "color": MUTED},
        )

    next_row = 2
    if show_volume:
        colors_volume = np.where(market["close"] >= market["open"], GREEN, RED)
        fig.add_trace(
            go.Bar(
                x=market.index,
                y=market["volume"],
                name="Volume",
                marker={"color": colors_volume, "line": {"width": 0}},
                opacity=0.55,
                hovertemplate="Volume<br>%{x|%Y-%m-%d %H:%M}<br>%{y:,.2f}<extra></extra>",
            ),
            row=next_row,
            col=1,
        )
        fig.update_yaxes(title_text="Volume", row=next_row, col=1)
        for result in volume_overlays:
            for column_index, column in enumerate(result.data.columns):
                fig.add_trace(
                    go.Scatter(
                        x=result.data.index,
                        y=result.data[column],
                        mode="lines",
                        name=f"{result.label} · {column}",
                        line={"width": 1.3, "color": "#facc15"},
                        hovertemplate=f"{column}<br>%{{x|%Y-%m-%d %H:%M}}<br>%{{y:,.2f}}<extra></extra>",
                    ),
                    row=next_row,
                    col=1,
                )
        next_row += 1

    for result in oscillators:
        for column_index, column in enumerate(result.data.columns):
            if result.spec.key == "macd" and column == "macd_hist":
                bar_colors = np.where(result.data[column] >= 0, GREEN, RED)
                fig.add_trace(
                    go.Bar(
                        x=result.data.index,
                        y=result.data[column],
                        name=f"{result.label} · {column}",
                        marker={"color": bar_colors, "line": {"width": 0}},
                        opacity=0.45,
                        hovertemplate=f"{column}<br>%{{x|%Y-%m-%d %H:%M}}<br>%{{y:,.4f}}<extra></extra>",
                    ),
                    row=next_row,
                    col=1,
                )
            else:
                fig.add_trace(
                    go.Scatter(
                        x=result.data.index,
                        y=result.data[column],
                        mode="lines",
                        name=f"{result.label} · {column}",
                        line={"width": 1.3, "color": ["#38bdf8", "#f472b6", "#facc15", "#a3e635"][column_index % 4]},
                        hovertemplate=f"{column}<br>%{{x|%Y-%m-%d %H:%M}}<br>%{{y:,.4f}}<extra></extra>",
                    ),
                    row=next_row,
                    col=1,
                )
        if result.spec.key == "rsi":
            for level in (30, 70):
                fig.add_hline(y=level, line_width=0.7, line_dash="dot", line_color=GRID, row=next_row, col=1)
        if result.spec.key == "stochastic":
            for level in (20, 80):
                fig.add_hline(y=level, line_width=0.7, line_dash="dot", line_color=GRID, row=next_row, col=1)
        fig.update_yaxes(title_text=result.spec.label, row=next_row, col=1)
        next_row += 1

    fig.update_xaxes(rangeslider_visible=False, row=1, col=1)
    fig.update_yaxes(title_text="Prezzo (USDT)", row=1, col=1)
    for row in range(2, rows + 1):
        fig.update_xaxes(title_text="UTC", row=row, col=1)
    fig = _apply_layout(fig, title, height)
    fig.update_xaxes(showspikes=True, spikemode="across", spikesnap="cursor", spikedash="dot")
    return fig


def make_actual_vs_predicted_figure(
    residuals: pd.DataFrame,
    *,
    actual_series: pd.Series | None = None,
    colors: Mapping[str, str] | None = None,
    title: str = "Real vs predicted · timestamp target",
    height: int = 420,
) -> go.Figure:
    if residuals.empty:
        return empty_figure("Nessun campione comune disponibile", height=height)
    colors = colors or {}
    fig = go.Figure()
    if actual_series is not None and not actual_series.empty:
        fig.add_trace(
            go.Scatter(
                x=actual_series.index,
                y=actual_series.to_numpy(),
                mode="lines",
                name="BTCUSDT actual close",
                line={"color": model_color("actual", colors), "width": 1.5},
                connectgaps=False,
                hovertemplate="Actual<br>%{x|%Y-%m-%d %H:%M}<br>%{y:,.2f}<extra></extra>",
            )
        )
    for model, group in residuals.groupby("model", sort=False):
        fig.add_trace(
            go.Scatter(
                x=group["target"],
                y=group["predicted"],
                mode="markers",
                name=str(group["model_label"].iloc[0]),
                marker={"size": 7, "color": model_color(model, colors)},
                customdata=np.column_stack([group["cutoff"].astype(str), group["horizon_hours"]]),
                hovertemplate=(
                    "<b>%{fullData.name}</b><br>target %{x|%Y-%m-%d %H:%M}<br>"
                    "predicted %{y:,.2f}<br>cutoff %{customdata[0]} · h%{customdata[1]}<extra></extra>"
                ),
            )
        )
    fig.update_yaxes(title_text="Prezzo (USDT)")
    return _apply_layout(fig, title, height)


def make_error_over_time_figure(
    residuals: pd.DataFrame,
    *,
    colors: Mapping[str, str] | None = None,
    title: str = "Forecast error nel tempo",
    height: int = 380,
) -> go.Figure:
    if residuals.empty:
        return empty_figure("Nessun errore disponibile", height=height)
    colors = colors or {}
    fig = go.Figure()
    for model, group in residuals.groupby("model", sort=False):
        fig.add_trace(
            go.Scatter(
                x=group["target"],
                y=group["error"],
                mode="markers",
                name=str(group["model_label"].iloc[0]),
                marker={"size": 6, "color": model_color(model, colors), "opacity": 0.8},
                hovertemplate="target %{x|%Y-%m-%d %H:%M}<br>error %{y:,.2f} USDT<extra></extra>",
            )
        )
    fig.add_hline(y=0, line_width=1, line_color=MUTED)
    fig.update_yaxes(title_text="Predicted − Actual (USDT)")
    return _apply_layout(fig, title, height)


def make_error_distribution_figure(
    residuals: pd.DataFrame,
    *,
    colors: Mapping[str, str] | None = None,
    title: str = "Distribuzione degli errori",
    height: int = 380,
) -> go.Figure:
    if residuals.empty:
        return empty_figure("Nessun errore disponibile", height=height)
    colors = colors or {}
    fig = go.Figure()
    for model, group in residuals.groupby("model", sort=False):
        fig.add_trace(
            go.Violin(
                y=group["error"],
                name=str(group["model_label"].iloc[0]),
                line_color=model_color(model, colors),
                fillcolor=_with_alpha(model_color(model, colors), 0.18),
                box_visible=True,
                meanline_visible=True,
                points="outliers",
                hovertemplate="error %{y:,.2f} USDT<extra></extra>",
            )
        )
    fig.add_hline(y=0, line_width=1, line_color=MUTED)
    fig.update_yaxes(title_text="Predicted − Actual (USDT)")
    return _apply_layout(fig, title, height)


def make_scatter_actual_predicted_figure(
    residuals: pd.DataFrame,
    *,
    colors: Mapping[str, str] | None = None,
    title: str = "Scatter · actual vs predicted",
    height: int = 420,
) -> go.Figure:
    if residuals.empty:
        return empty_figure("Nessun campione comune disponibile", height=height)
    colors = colors or {}
    fig = go.Figure()
    values = pd.concat([residuals["actual"], residuals["predicted"]]).dropna()
    if not values.empty:
        low, high = float(values.min()), float(values.max())
        fig.add_trace(
            go.Scatter(
                x=[low, high],
                y=[low, high],
                mode="lines",
                name="Perfetta previsione",
                line={"color": MUTED, "dash": "dash", "width": 1},
                hoverinfo="skip",
            )
        )
    for model, group in residuals.groupby("model", sort=False):
        fig.add_trace(
            go.Scatter(
                x=group["actual"],
                y=group["predicted"],
                mode="markers",
                name=str(group["model_label"].iloc[0]),
                marker={"size": 7, "color": model_color(model, colors), "opacity": 0.8},
                customdata=np.column_stack([group["target"].astype(str), group["error"]]),
                hovertemplate=(
                    "actual %{x:,.2f}<br>predicted %{y:,.2f}<br>"
                    "error %{customdata[1]:,.2f}<br>%{customdata[0]}<extra></extra>"
                ),
            )
        )
    fig.update_xaxes(title_text="Actual (USDT)")
    fig.update_yaxes(title_text="Predicted (USDT)", scaleanchor="x", scaleratio=1)
    return _apply_layout(fig, title, height)


def make_regime_figure(
    regime_table: pd.DataFrame,
    *,
    regime_column: str,
    colors: Mapping[str, str] | None = None,
    title: str = "MAE per regime",
    height: int = 380,
    category_order: Sequence[str] | None = None,
) -> go.Figure:
    if regime_table.empty:
        return empty_figure("Regimi non disponibili per i campioni selezionati", height=height)
    colors = colors or {}
    fig = go.Figure()
    categories = list(category_order or regime_table[regime_column].dropna().unique())
    for model, group in regime_table.groupby("model", sort=False):
        group = group.set_index(regime_column).reindex(categories).reset_index()
        fig.add_trace(
            go.Bar(
                x=group[regime_column],
                y=group["mae"],
                name=str(group["model_label"].dropna().iloc[0]) if group["model_label"].notna().any() else model,
                marker={"color": model_color(model, colors)},
                customdata=np.column_stack([group["samples"].fillna(0).astype(int), group["bias"]]),
                hovertemplate=(
                    "regime %{x}<br>MAE %{y:,.2f} USDT<br>"
                    "campioni %{customdata[0]}<br>bias %{customdata[1]:,.2f}<extra></extra>"
                ),
            )
        )
    fig.update_xaxes(title_text=regime_column.replace("_", " ").title())
    fig.update_yaxes(title_text="MAE (USDT)")
    fig.update_layout(barmode="group")
    return _apply_layout(fig, title, height)


def make_forecast_inspector_figure(
    market: pd.DataFrame,
    window_points: pd.DataFrame,
    *,
    target: pd.Timestamp,
    cutoff: pd.Timestamp,
    colors: Mapping[str, str] | None = None,
    height: int = 340,
) -> go.Figure:
    """Small path/point chart for the inspector.

    The path is only for the selected window; +24h-only models appear as a
    single marker at their target.
    """

    if window_points.empty:
        return empty_figure("Nessuna previsione per la finestra selezionata", height=height)
    colors = colors or {}
    context_start = cutoff - pd.Timedelta(hours=72)
    context_end = max(target, cutoff) + pd.Timedelta(hours=6)
    context = market.loc[(market.index >= context_start) & (market.index < context_end)]
    fig = go.Figure()
    if not context.empty:
        fig.add_trace(
            go.Scatter(
                x=context.index,
                y=context["close"],
                mode="lines",
                name="BTCUSDT close",
                line={"color": model_color("actual", colors), "width": 1.6},
                connectgaps=False,
                hovertemplate="BTCUSDT<br>%{x|%Y-%m-%d %H:%M}<br>%{y:,.2f}<extra></extra>",
            )
        )
    for model, group in window_points.groupby("model", sort=False):
        group = group.sort_values("horizon_hours")
        marker_only = model in {"ridge", "histgb", "decision"} or len(group) == 1
        fig.add_trace(
            go.Scatter(
                x=group["target"],
                y=group["predicted"],
                mode="markers" if marker_only else "lines+markers",
                name=str(group["model_label"].iloc[0]),
                line={"color": model_color(model, colors), "width": 2, "dash": "dot" if model == "naive" else "solid"},
                marker={"size": 8 if marker_only else 6, "color": model_color(model, colors)},
                customdata=_forecast_customdata(group),
                hovertemplate=_hover_forecast(),
            )
        )
    fig.add_vline(x=cutoff, line_width=1, line_dash="dash", line_color="#4cc9f0")
    fig.add_vline(x=target, line_width=1, line_dash="dot", line_color="#f0b90b")
    fig.add_vrect(
        x0=cutoff,
        x1=target,
        fillcolor=_with_alpha("#4cc9f0", 0.05),
        line_width=0,
    )
    fig.update_yaxes(title_text="Prezzo (USDT)")
    return _apply_layout(
        fig,
        f"Cutoff {cutoff:%Y-%m-%d %H:%M} → target {target:%Y-%m-%d %H:%M} UTC",
        height,
    )
