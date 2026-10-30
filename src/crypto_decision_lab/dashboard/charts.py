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


def make_delta_mae_histogram(
    errors_frame: pd.DataFrame,
    *,
    title: str = "Distribuzione delta MAE (Timestral - Naive) · h24",
    height: int = 380,
) -> go.Figure:
    if errors_frame.empty:
        return empty_figure("Nessun dato delta MAE disponibile", height=height)
    fig = go.Figure()
    fig.add_trace(
        go.Histogram(
            x=errors_frame["delta_mae"],
            nbinsx=40,
            marker={"color": _with_alpha(model_color("timestral", {}), 0.55), "line": {"color": "#4cc9f0", "width": 1}},
            hovertemplate="delta MAE %{x:,.1f} USDT<br>finestre %{y}<extra></extra>",
        )
    )
    mean_value = float(errors_frame["delta_mae"].mean())
    fig.add_vline(x=0, line_width=1, line_color=MUTED, line_dash="dash")
    fig.add_vline(x=mean_value, line_width=1.4, line_color=GREEN)
    fig.add_annotation(
        x=mean_value,
        y=1.0,
        yref="paper",
        text=f"media {mean_value:+,.1f} USDT",
        showarrow=False,
        xanchor="left",
        font={"size": 11, "color": GREEN},
    )
    fig.update_xaxes(title_text="delta MAE (USDT), negativo = Timestral migliore")
    fig.update_yaxes(title_text="Finestre")
    return _apply_layout(fig, title, height)


def make_win_rate_by_regime_figure(
    stats_frames: dict[str, pd.DataFrame],
    *,
    height: int = 380,
) -> go.Figure:
    fig = go.Figure()
    palette = ["#4cc9f0", "#f472b6", "#facc15"]
    for index, (name, frame) in enumerate(stats_frames.items()):
        if frame.empty:
            continue
        fig.add_trace(
            go.Bar(
                x=frame.iloc[:, 0],
                y=frame["timestral_win_rate"] * 100.0,
                name=name,
                marker={"color": palette[index % len(palette)]},
                customdata=np.column_stack(
                    [frame["observations"], frame["delta_mae_usdt"]]
                ),
                hovertemplate=(
                    "regime %{x}<br>win Timestral %{y:.1f}%<br>"
                    "finestre %{customdata[0]}<br>delta MAE %{customdata[1]:+,.1f} USDT<extra></extra>"
                ),
            )
        )
    fig.add_hline(y=50, line_width=1, line_dash="dot", line_color=MUTED)
    fig.update_yaxes(title_text="Win rate Timestral (%)")
    fig.update_layout(barmode="group")
    return _apply_layout(fig, "Win rate Timestral per regime · full sample", height)


def make_policy_performance_figure(
    policy_rows: list[dict],
    *,
    height: int = 420,
) -> go.Figure:
    if not policy_rows:
        return empty_figure("Nessuna policy valutata", height=height)
    fig = go.Figure()
    names = [row["label"] for row in policy_rows]
    mae = [row["mae_usdt"] for row in policy_rows]
    colors = [model_color("naive", {}) if row["name"] == "always_naive" else RED if row["uses_target"] else "#4cc9f0" for row in policy_rows]
    fig.add_trace(
        go.Bar(
            x=names,
            y=mae,
            marker={"color": colors},
            customdata=np.array([[row["delta"], row["ci_low"], row["ci_high"], row["share_timestral"]] for row in policy_rows]),
            hovertemplate=(
                "<b>%{x}</b><br>MAE OOS %{y:,.2f} USDT<br>"
                "delta vs naive %{customdata[0]:+,.2f} USDT<br>"
                "CI95 [%{customdata[1]:+,.2f}, %{customdata[2]:+,.2f}]<br>"
                "share Timestral %{customdata[3]:.0%}<extra></extra>"
            ),
        )
    )
    baseline = next((row["mae_usdt"] for row in policy_rows if row["name"] == "always_naive"), None)
    if baseline is not None:
        fig.add_hline(y=baseline, line_width=1.2, line_dash="dash", line_color=MUTED)
    fig.update_yaxes(title_text="MAE out-of-sample (USDT)")
    return _apply_layout(fig, "Policy performance · walk-forward out-of-sample", height)


POLICY_COLORS = {
    "always_no_trade": "#9aa4b2",
    "always_long": "#2dd4bf",
    "always_short": "#ff6b6b",
    "timestral_threshold": "#4cc9f0",
    "momentum_threshold": "#f0b90b",
    "oracle_retrospective": "#b388ff",
}


def policy_color(name: str) -> str:
    return POLICY_COLORS.get(name, "#c7d2fe")


def make_equity_curve_figure(
    curves: dict[str, pd.Series],
    *,
    title: str = "Equity curve · out-of-sample walk-forward",
    height: int = 420,
) -> go.Figure:
    fig = go.Figure()
    for name, curve in curves.items():
        if curve.empty:
            continue
        fig.add_trace(
            go.Scatter(
                x=curve.index,
                y=curve.to_numpy(dtype=float),
                mode="lines",
                name=name,
                line={"color": policy_color(name), "width": 1.8},
                hovertemplate="%{x|%Y-%m-%d}<br>equity %{y:,.2f}<extra></extra>",
            )
        )
    fig.update_yaxes(title_text="Equity (USDT)")
    return _apply_layout(fig, title, height)


def make_drawdown_figure(
    curve: pd.Series,
    *,
    name: str,
    height: int = 300,
) -> go.Figure:
    if curve.empty:
        return empty_figure("Nessuna curva disponibile", height=height)
    equity = curve.to_numpy(dtype=float)
    peak = np.maximum.accumulate(equity)
    drawdown = 100.0 * (equity / peak - 1.0)
    fig = go.Figure(
        go.Scatter(
            x=curve.index,
            y=drawdown,
            mode="lines",
            name="drawdown",
            fill="tozeroy",
            line={"color": RED, "width": 1.4},
            fillcolor=_with_alpha(RED, 0.2),
            hovertemplate="%{x|%Y-%m-%d}<br>drawdown %{y:.2f}%<extra></extra>",
        )
    )
    fig.update_yaxes(title_text="Drawdown (%)")
    return _apply_layout(fig, f"Drawdown · {name}", height)


STABILITY_CURVE_COLORS = {"step": "#4cc9f0", "mark_to_market": "#f0b90b"}
STABILITY_CURVE_LABELS = {"step": "Exit-marked (ledger)", "mark_to_market": "Mark-to-market orario"}


def make_stability_equity_figure(
    equity: pd.DataFrame,
    *,
    initial_capital: float = 10_000.0,
    height: int = 460,
) -> go.Figure:
    """Equity curve of the momentum policy: ledger and mark-to-market."""

    if equity.empty:
        return empty_figure("Nessuna curva equity nell'archivio CP08-02", height=height)
    fig = go.Figure()
    # Mark-to-market is drawn first so the exit-marked step stays visible on top.
    ordered = sorted(equity.groupby("curve", sort=False), key=lambda item: str(item[0]) != "mark_to_market")
    for curve, group in ordered:
        group = group.sort_values("timestamp")
        color = STABILITY_CURVE_COLORS.get(str(curve), MUTED)
        label = STABILITY_CURVE_LABELS.get(str(curve), str(curve))
        fig.add_trace(
            go.Scatter(
                x=group["timestamp"],
                y=group["equity"].to_numpy(dtype=float),
                mode="lines",
                name=label,
                line={"color": color, "width": 2.4 if str(curve) == "step" else 1.2},
                hovertemplate="%{x|%Y-%m-%d %H:%M}<br>equity %{y:,.2f} USDT<extra>" + label + "</extra>",
            )
        )
    fig.add_hline(
        y=float(initial_capital),
        line={"color": MUTED, "width": 1, "dash": "dot"},
        annotation_text="capitale iniziale",
        annotation_position="bottom right",
    )
    fig.update_yaxes(title_text="Equity (USDT)")
    return _apply_layout(fig, "Momentum · equity OOS (100% equity, nessuna leva)", height)


def make_underwater_figure(equity: pd.DataFrame, *, height: int = 340) -> go.Figure:
    """Underwater curve (drawdown from the running peak) per convention."""

    if equity.empty:
        return empty_figure("Nessuna curva underwater nell'archivio CP08-02", height=height)
    fig = go.Figure()
    for curve, group in equity.groupby("curve", sort=False):
        group = group.sort_values("timestamp")
        color = STABILITY_CURVE_COLORS.get(str(curve), RED)
        label = STABILITY_CURVE_LABELS.get(str(curve), str(curve))
        fig.add_trace(
            go.Scatter(
                x=group["timestamp"],
                y=group["drawdown_pct"].to_numpy(dtype=float),
                mode="lines",
                name=label,
                fill="tozeroy",
                line={"color": color, "width": 1.3},
                fillcolor=_with_alpha(color, 0.18),
                hovertemplate="%{x|%Y-%m-%d %H:%M}<br>drawdown %{y:.2f}%<extra>" + label + "</extra>",
            )
        )
    fig.update_yaxes(title_text="Drawdown from peak (%)", rangemode="tozero")
    return _apply_layout(fig, "Underwater · mark-to-market vs exit-marked", height)


def make_pnl_distribution_figure(
    trades: pd.DataFrame,
    *,
    metric: str = "pnl_usdt",
    title: str = "Distribuzione P&L per trade",
    height: int = 360,
) -> go.Figure:
    """Win/loss histogram of the per-trade P&L (monetary by default)."""

    if trades.empty or metric not in trades.columns:
        return empty_figure("Nessun trade per la distribuzione P&L", height=height)
    values = trades[metric].to_numpy(dtype=float)
    wins = values[values > 0]
    losses = values[values < 0]
    unit = " USDT" if metric.endswith("usdt") else "%"
    fig = go.Figure()
    if len(wins):
        fig.add_trace(
            go.Histogram(
                x=wins,
                name=f"Vincite ({len(wins)})",
                marker={"color": _with_alpha(GREEN, 0.75), "line": {"color": GREEN, "width": 0.6}},
                opacity=0.85,
                hovertemplate="%{x:.2f}" + unit + "<br>n %{y}<extra></extra>",
            )
        )
    if len(losses):
        fig.add_trace(
            go.Histogram(
                x=losses,
                name=f"Perdite ({len(losses)})",
                marker={"color": _with_alpha(RED, 0.75), "line": {"color": RED, "width": 0.6}},
                opacity=0.85,
                hovertemplate="%{x:.2f}" + unit + "<br>n %{y}<extra></extra>",
            )
        )
    fig.add_vline(
        x=float(np.mean(values)),
        line={"color": TEXT, "width": 1.2, "dash": "dash"},
        annotation_text=f"media {np.mean(values):+,.1f}{unit}",
        annotation_position="top",
    )
    fig.add_vline(
        x=float(np.median(values)),
        line={"color": MUTED, "width": 1.2, "dash": "dot"},
        annotation_text=f"mediana {np.median(values):+,.1f}{unit}",
        annotation_position="bottom",
    )
    fig.update_layout(barmode="overlay", bargap=0.04)
    fig.update_xaxes(title_text=f"P&L per trade ({unit.strip()})")
    fig.update_yaxes(title_text="Numero di trade")
    return _apply_layout(fig, title, height)


def make_profit_concentration_figure(
    curve: pd.DataFrame,
    *,
    top_k: Sequence[int] = (1, 3, 5, 10),
    height: int = 420,
) -> go.Figure:
    """Ranked trade P&L and cumulative share of the net profit."""

    if curve.empty or "pnl_usdt" not in curve.columns:
        return empty_figure("Nessuna curva di concentrazione disponibile", height=height)
    frame = curve.sort_values("rank").reset_index(drop=True)
    ranks = frame["rank"].to_numpy(dtype=int)
    pnl = frame["pnl_usdt"].to_numpy(dtype=float)
    share = frame["share_of_net_pnl_pct"].to_numpy(dtype=float)
    colors = np.where(pnl >= 0, _with_alpha(GREEN, 0.8), _with_alpha(RED, 0.8))
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(
        go.Bar(
            x=ranks,
            y=pnl,
            name="P&L per trade (USDT)",
            marker={"color": colors},
            hovertemplate="rank %{x}<br>P&L %{y:,.2f} USDT<extra></extra>",
        ),
        secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(
            x=ranks,
            y=share,
            mode="lines",
            name="share cumulata del P&L netto (%)",
            line={"color": "#b388ff", "width": 1.8},
            hovertemplate="rank %{x}<br>share cumulata %{y:.1f}%<extra></extra>",
        ),
        secondary_y=True,
    )
    for k in top_k:
        if k <= len(frame):
            fig.add_vline(
                x=k,
                line={"color": MUTED, "width": 1, "dash": "dot"},
                annotation_text=f"top {k}",
                annotation_position="bottom",
            )
    # The cumulative curve peaks at the gross profit and then declines to the net
    # P&L: annotate the peak instead of silently showing values above 100%.
    peak_position = int(np.argmax(frame["cumulative_pnl_usdt"].to_numpy(dtype=float)))
    peak_value = float(frame["cumulative_pnl_usdt"].iloc[peak_position])
    peak_share = float(frame["share_of_net_pnl_pct"].iloc[peak_position])
    fig.add_trace(
        go.Scatter(
            x=[frame["rank"].iloc[peak_position]],
            y=[peak_value],
            mode="markers+text",
            name="profitto lordo cumulato",
            marker={"color": "#b388ff", "size": 9, "symbol": "diamond"},
            text=[f"profitto lordo {peak_value:,.0f} USDT ({peak_share:.0f}% del netto)"],
            textposition="bottom center",
            textfont={"color": "#b388ff", "size": 11},
            hovertemplate="%{x}<br>cumulato %{y:,.2f} USDT<extra></extra>",
            showlegend=False,
        ),
        secondary_y=False,
    )
    fig.update_xaxes(title_text="Trade ordinati per P&L decrescente")
    fig.update_yaxes(title_text="P&L (USDT)", secondary_y=False)
    fig.update_yaxes(title_text="Share cumulata (% del P&L netto)", secondary_y=True)
    return _apply_layout(fig, "Profit concentration · top 1/3/5/10 trade", height)


def make_period_pnl_figure(
    periods: pd.DataFrame,
    *,
    granularity: str = "month",
    height: int = 380,
) -> go.Figure:
    """Monetary P&L per period with the cumulative attribution line."""

    subset = periods[periods["granularity"] == granularity].sort_values("period_start")
    if subset.empty:
        return empty_figure(f"Nessun periodo '{granularity}' disponibile", height=height)
    label = {"month": "mese", "quarter": "trimestre", "year": "anno", "fold": "fold", "window": "finestra"}.get(
        granularity, granularity
    )
    pnl = subset["net_pnl_usdt"].to_numpy(dtype=float)
    cumulative = np.cumsum(pnl)
    colors = np.where(pnl >= 0, _with_alpha(GREEN, 0.85), _with_alpha(RED, 0.85))
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(
        go.Bar(
            x=subset["period"],
            y=pnl,
            name="P&L monetario nel periodo",
            marker={"color": colors},
            customdata=subset["trades"].to_numpy(dtype=int).reshape(-1, 1),
            hovertemplate="%{x}<br>P&L %{y:,.2f} USDT<br>trade %{customdata[0]}<extra></extra>",
        ),
        secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(
            x=subset["period"],
            y=cumulative,
            mode="lines+markers",
            name="P&L cumulato",
            line={"color": "#b388ff", "width": 1.8},
            marker={"size": 4},
            hovertemplate="%{x}<br>cumulato %{y:,.2f} USDT<extra></extra>",
        ),
        secondary_y=True,
    )
    fig.update_xaxes(title_text=label)
    fig.update_yaxes(title_text="P&L del periodo (USDT)", secondary_y=False)
    fig.update_yaxes(title_text="P&L cumulato (USDT)", secondary_y=True)
    return _apply_layout(fig, f"P&L per {label} · attribuzione monetaria", height)


def make_trade_signal_figure(decisions: pd.DataFrame, *, policy: str, height: int = 380) -> go.Figure:
    if decisions.empty:
        return empty_figure("Nessun segnale disponibile", height=height)
    fig = go.Figure()
    styles = {
        "LONG": ("triangle-up", GREEN),
        "SHORT": ("triangle-down", RED),
        "NO_TRADE": ("circle", MUTED),
    }
    for action, (symbol, color) in styles.items():
        subset = decisions[decisions["action"] == action]
        if subset.empty:
            continue
        fig.add_trace(
            go.Scatter(
                x=pd.to_datetime(subset["entry_timestamp"], utc=True),
                y=subset["reference_price"].to_numpy(dtype=float),
                mode="markers",
                name=action,
                marker={"symbol": symbol, "size": 7, "color": color},
                customdata=np.column_stack(
                    [subset["net_return_pct"].to_numpy(dtype=float), subset["window_index"].to_numpy()]
                ),
                hovertemplate=(
                    "%{x|%Y-%m-%d %H:%M}<br>reference %{y:,.2f} USDT<br>"
                    "net %{customdata[0]:+.2f}%<br>window %{customdata[1]}<extra></extra>"
                ),
            )
        )
    fig.update_yaxes(title_text="Reference price (USDT)")
    return _apply_layout(fig, f"Segnali · {policy}", height)


def make_sensitivity_heatmap(
    scenarios: list[dict],
    *,
    policy: str,
    height: int = 360,
) -> go.Figure:
    if not scenarios:
        return empty_figure("Sensitivity non disponibile", height=height)
    fees = sorted({item["fee_rate"] for item in scenarios})
    slips = sorted({item["slippage"] for item in scenarios})
    z = np.full((len(slips), len(fees)), np.nan)
    for item in scenarios:
        row = slips.index(item["slippage"])
        column = fees.index(item["fee_rate"])
        z[row, column] = item["total_return_pct"].get(policy, np.nan)
    fig = go.Figure(
        go.Heatmap(
            x=[f"{value:.4%}" for value in fees],
            y=[f"{value:.4%}" for value in slips],
            z=z,
            colorscale=[[0.0, "#ff6b6b"], [0.5, "#111722"], [1.0, "#2dd4bf"]],
            zmid=0.0,
            text=np.round(z, 2),
            texttemplate="%{text}%",
            hovertemplate="fee %{x}<br>slippage %{y}<br>total return %{z:+.2f}%<extra></extra>",
            colorbar={"title": "%"},
        )
    )
    fig.update_xaxes(title_text="Fee per side")
    fig.update_yaxes(title_text="Slippage per side")
    return _apply_layout(fig, f"Sensitivity · {policy}", height)


def make_policy_return_figure(
    rows: list[dict],
    *,
    title: str = "Policy total return · out-of-sample",
    height: int = 380,
) -> go.Figure:
    if not rows:
        return empty_figure("Nessuna policy valutata", height=height)
    names = [row["label"] for row in rows]
    returns = [row["total_return"] for row in rows]
    colors = [RED if row["uses_target"] else (GREEN if row["total_return"] >= 0 else "#f59e0b") for row in rows]
    fig = go.Figure(
        go.Bar(
            x=names,
            y=returns,
            marker={"color": colors},
            customdata=np.array(
                [
                    [
                        row["trades"],
                        row["win_rate"] * 100.0,
                        row["max_drawdown"],
                        row.get("ci_low", np.nan),
                        row.get("ci_high", np.nan),
                    ]
                    for row in rows
                ]
            ),
            hovertemplate=(
                "<b>%{x}</b><br>total return %{y:+.2f}%<br>trades %{customdata[0]}<br>"
                "win rate %{customdata[1]:.1f}%<br>max DD %{customdata[2]:.2f}%<extra></extra>"
            ),
        )
    )
    fig.add_hline(y=0, line_width=1, line_color=MUTED)
    fig.update_yaxes(title_text="Total return OOS (%)")
    return _apply_layout(fig, title, height)


# ---------------------------------------------------------------------------
# S02-CP08-03 · parameter & execution robustness (read-only archive readers)
# ---------------------------------------------------------------------------

ROBUSTNESS_FROZEN_COLOR = "#4cc9f0"
ROBUSTNESS_REFIT_COLOR = "#f0b90b"
ROBUSTNESS_EXPLORATORY_COLOR = "#b388ff"


def make_cost_sensitivity_figure(
    scenarios: pd.DataFrame,
    *,
    height: int = 400,
) -> go.Figure:
    """Frozen-ledger cost curve vs threshold-refit cost curve.

    Two different experiments on the same axes:
    * ``cost_frozen``: decisions frozen, only costs change -> monotone.
    * ``policy_refit``: threshold re-selected on training for each cost -> the
      confound documented in CP08-01 (F10).
    """

    if scenarios is None or scenarios.empty:
        return empty_figure("Sensitivity ai costi non disponibile", height=height)
    fig = go.Figure()
    definitions = (
        ("cost_frozen", "Decisioni congelate (solo costo)", ROBUSTNESS_FROZEN_COLOR, "circle"),
        ("policy_refit", "Soglia rifittata su training (costo + selezione)", ROBUSTNESS_REFIT_COLOR, "diamond"),
    )
    for family, label, color, symbol in definitions:
        subset = scenarios[scenarios["family"] == family].sort_values("total_cost_per_side")
        if subset.empty:
            continue
        fig.add_trace(
            go.Scatter(
                x=100.0 * subset["total_cost_per_side"].to_numpy(dtype=float),
                y=subset["total_return_pct"].to_numpy(dtype=float),
                mode="lines+markers",
                name=label,
                line={"color": color, "width": 2.4},
                marker={"color": color, "size": 8, "symbol": symbol},
                customdata=np.stack(
                    [
                        subset["trades"].to_numpy(dtype=float),
                        subset["profit_factor_usdt"].to_numpy(dtype=float),
                        subset["max_drawdown_mtm_pct"].to_numpy(dtype=float),
                        subset["scenario"].astype(str).to_numpy(),
                    ],
                    axis=1,
                ),
                hovertemplate=(
                    "costo %{x:.3f}% per lato<br>return %{y:+.2f}%<br>"
                    "trade %{customdata[0]:.0f}<br>PF monetario %{customdata[1]:.4f}<br>"
                    "DD MTM %{customdata[2]:.2f}%<br>%{customdata[3]}<extra>" + label + "</extra>"
                ),
            )
        )
    fig.add_hline(y=0, line_width=1, line_color=MUTED)
    fig.add_vline(
        x=0.15,
        line={"color": MUTED, "width": 1, "dash": "dot"},
        annotation_text="CP07 base 0,15%",
        annotation_position="top right",
    )
    fig.update_xaxes(title_text="Costo totale per lato (%)")
    fig.update_yaxes(title_text="Rendimento composto OOS (%)")
    return _apply_layout(fig, "Costi · decisioni congelate vs refit della soglia", height)


def make_latency_robustness_figure(
    scenarios: pd.DataFrame,
    *,
    height: int = 400,
) -> go.Figure:
    """Return and mark-to-market drawdown as a function of execution latency."""

    if scenarios is None or scenarios.empty:
        return empty_figure("Scenari di latenza non disponibili", height=height)
    subset = scenarios[scenarios["family"] == "latency"].sort_values("delay_hours")
    if subset.empty:
        return empty_figure("Scenari di latenza non disponibili", height=height)
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(
        go.Bar(
            x=[f"{int(value)}h" for value in subset["delay_hours"]],
            y=subset["total_return_pct"].to_numpy(dtype=float),
            name="Rendimento composto",
            marker={"color": [GREEN if value >= 0 else RED for value in subset["total_return_pct"]]},
            customdata=np.stack(
                [
                    subset["profit_factor_usdt"].to_numpy(dtype=float),
                    subset["trades"].to_numpy(dtype=float),
                ],
                axis=1,
            ),
            hovertemplate=(
                "latenza %{x}<br>return %{y:+.2f}%<br>PF monetario %{customdata[0]:.4f}<br>"
                "trade %{customdata[1]:.0f}<extra></extra>"
            ),
        ),
        secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(
            x=[f"{int(value)}h" for value in subset["delay_hours"]],
            y=subset["max_drawdown_mtm_pct"].to_numpy(dtype=float),
            mode="lines+markers",
            name="Max DD mark-to-market",
            line={"color": RED, "width": 2.4},
            marker={"size": 9},
            hovertemplate="latenza %{x}<br>max DD MTM %{y:.2f}%<extra></extra>",
        ),
        secondary_y=True,
    )
    fig.add_hline(y=0, line_width=1, line_color=MUTED, secondary_y=False)
    fig.update_yaxes(title_text="Rendimento composto (%)", secondary_y=False)
    fig.update_yaxes(title_text="Max drawdown MTM (%)", secondary_y=True)
    fig.update_xaxes(title_text="Latenza di esecuzione (open + N ore)")
    return _apply_layout(fig, "Latenza di esecuzione · 1h è lo scenario conservativo principale", height)


def make_threshold_surface_figure(
    surface: pd.DataFrame,
    *,
    frozen_threshold: float = 0.05,
    height: int = 440,
) -> go.Figure:
    """Train multiples and OOS return of every pre-registered threshold.

    The OOS band and line are explicitly exploratory: the figure never marks a
    "best" configuration as selectable.
    """

    if surface is None or surface.empty:
        return empty_figure("Superficie delle soglie non disponibile", height=height)
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    train = surface[surface["scope"] == "train"]
    for fold, group in sorted(train.groupby("fold")):
        group = group.sort_values("threshold")
        fig.add_trace(
            go.Scatter(
                x=100.0 * group["threshold"].to_numpy(dtype=float),
                y=group["total_return_pct"].to_numpy(dtype=float),
                mode="lines",
                name=f"Training fold {int(fold)}",
                line={"color": "rgba(139,152,171,0.45)", "width": 1},
                showlegend=bool(int(fold) == 0),
                legendgroup="train",
                hovertemplate="soglia %{x:.1f}%<br>training return %{y:+.1f}%<extra></extra>",
            ),
            secondary_y=True,
        )
    oos = surface[surface["scope"] == "test_all"].sort_values("threshold")
    if not oos.empty:
        thresholds = 100.0 * oos["threshold"].to_numpy(dtype=float)
        lower = oos["ci_low_pct"].to_numpy(dtype=float)
        upper = oos["ci_high_pct"].to_numpy(dtype=float)
        fig.add_trace(
            go.Scatter(
                x=np.concatenate([thresholds, thresholds[::-1]]),
                y=np.concatenate([lower, upper[::-1]]),
                fill="toself",
                fillcolor="rgba(179,136,255,0.14)",
                line={"color": "rgba(0,0,0,0)"},
                name="CI 95% OOS (bootstrap)",
                hoverinfo="skip",
            ),
            secondary_y=False,
        )
        fig.add_trace(
            go.Scatter(
                x=thresholds,
                y=oos["total_return_pct"].to_numpy(dtype=float),
                mode="lines+markers",
                name="OOS · esplorativo (non promosso)",
                line={"color": ROBUSTNESS_EXPLORATORY_COLOR, "width": 2.8},
                marker={"size": 7},
                customdata=np.stack(
                    [
                        oos["trades"].to_numpy(dtype=float),
                        oos["profit_factor_usdt"].to_numpy(dtype=float),
                        oos["max_drawdown_mtm_pct"].to_numpy(dtype=float),
                    ],
                    axis=1,
                ),
                hovertemplate=(
                    "soglia %{x:.1f}%<br>OOS return %{y:+.2f}%<br>trade %{customdata[0]:.0f}<br>"
                    "PF monetario %{customdata[1]:.4f}<br>DD MTM %{customdata[2]:.2f}%<extra></extra>"
                ),
            ),
            secondary_y=False,
        )
    fig.add_vline(
        x=100.0 * float(frozen_threshold),
        line={"color": ROBUSTNESS_FROZEN_COLOR, "width": 1.6, "dash": "dash"},
        annotation_text="soglia congelata 5%",
        annotation_position="top left",
    )
    for required in (8.0, 10.0):
        fig.add_vline(
            x=required,
            line={"color": MUTED, "width": 1, "dash": "dot"},
            annotation_text=f"{required:.0f}%",
            annotation_position="bottom right",
        )
    fig.update_xaxes(title_text="Soglia momentum_168h (%)")
    fig.update_yaxes(title_text="Rendimento OOS (%)", secondary_y=False)
    fig.update_yaxes(title_text="Rendimento training (%)", secondary_y=True, showgrid=False)
    return _apply_layout(fig, "Superficie soglia · training (grigio) vs OOS esplorativo (viola)", height)


def make_scenario_interval_figure(
    scenarios: pd.DataFrame,
    *,
    height: int = 400,
) -> go.Figure:
    """Bootstrap interval of selected scenarios (base, 1h, cost stress, carry)."""

    if scenarios is None or scenarios.empty:
        return empty_figure("Intervalli bootstrap non disponibili", height=height)
    wanted = (
        "cost_0015_base",
        "conservative_1h",
        "conservative_1h_cost20",
        "conservative_1h_cost20_carry",
        "carry_stressed",
    )
    subset = scenarios[scenarios["scenario"].isin(wanted)].copy()
    if subset.empty:
        return empty_figure("Intervalli bootstrap non disponibili", height=height)
    order = {name: index for index, name in enumerate(wanted)}
    subset["_order"] = subset["scenario"].map(order)
    subset = subset.sort_values("_order", ascending=False)
    point = subset["total_return_pct"].to_numpy(dtype=float)
    lower = point - subset["ci_return_low_pct"].to_numpy(dtype=float)
    upper = subset["ci_return_high_pct"].to_numpy(dtype=float) - point
    fig = go.Figure(
        go.Scatter(
            x=point,
            y=subset["scenario"].astype(str),
            mode="markers",
            marker={"color": ROBUSTNESS_FROZEN_COLOR, "size": 10},
            error_x={
                "type": "data",
                "symmetric": False,
                "array": upper,
                "arrayminus": lower,
                "color": MUTED,
                "thickness": 1.6,
            },
            customdata=np.stack(
                [
                    subset["profit_factor_usdt"].to_numpy(dtype=float),
                    subset["max_drawdown_mtm_pct"].to_numpy(dtype=float),
                    subset["ci_return_p_positive"].to_numpy(dtype=float),
                ],
                axis=1,
            ),
            hovertemplate=(
                "<b>%{y}</b><br>return %{x:+.2f}%<br>PF monetario %{customdata[0]:.4f}<br>"
                "DD MTM %{customdata[1]:.2f}%<br>P(>0) %{customdata[2]:.1%}<extra></extra>"
            ),
        )
    )
    fig.add_vline(x=0, line={"color": MUTED, "width": 1})
    fig.update_xaxes(title_text="Rendimento composto OOS (%) · CI 95% bootstrap")
    return _apply_layout(fig, "Scenari chiave · intervallo bootstrap del rendimento", height)


# ---------------------------------------------------------------------------
# S02-CP08-04 · independent validation protocol timeline (read-only)
# ---------------------------------------------------------------------------

VALIDATION_RESEARCH_COLOR = "#4cc9f0"
VALIDATION_TAIL_COLOR = "#f0b90b"
VALIDATION_INDEPENDENT_COLOR = "#b388ff"


def make_validation_timeline_figure(
    data_registry: Mapping[str, Any],
    *,
    min_independent_days: int = 180,
    height: int = 340,
) -> go.Figure:
    """Research period, observed-but-unused tail and the awaited independent window.

    The independent window is drawn as an *expectation*: no independent data
    exists in the repository, so nothing is marked as evaluated.
    """

    period = (data_registry or {}).get("research_period", {})
    if not period:
        return empty_figure("Registro dati del protocollo non disponibile", height=height)
    research_start = pd.Timestamp(period["start_utc"])
    research_end = pd.Timestamp(period["end_exclusive_utc"])
    last_used = pd.Timestamp(period["last_used_target_end_utc"])
    tail = (data_registry or {}).get("separation", {}).get("observed_unused_tail", [])
    independent_start = pd.Timestamp(
        (data_registry or {}).get("separation", {}).get("independent_start_utc", research_end)
    )
    independent_end = independent_start + pd.Timedelta(days=int(min_independent_days))

    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=[research_start, independent_end],
            y=[0.0, 0.0],
            mode="markers",
            marker={"opacity": 0.0, "size": 1},
            showlegend=False,
            hoverinfo="skip",
        )
    )
    fig.add_shape(
        type="rect",
        x0=research_start,
        x1=research_end,
        y0=0.55,
        y1=0.95,
        line={"color": VALIDATION_RESEARCH_COLOR, "width": 1.2},
        fillcolor="rgba(76,201,240,0.22)",
    )
    fig.add_shape(
        type="rect",
        x0=last_used,
        x1=research_end,
        y0=0.10,
        y1=0.42,
        line={"color": VALIDATION_TAIL_COLOR, "width": 1.2},
        fillcolor="rgba(240,185,11,0.28)",
    )
    fig.add_shape(
        type="rect",
        x0=independent_start,
        x1=independent_end,
        y0=0.55,
        y1=0.95,
        line={"color": VALIDATION_INDEPENDENT_COLOR, "width": 2.0, "dash": "dash"},
        fillcolor="rgba(179,136,255,0.10)",
    )
    fig.add_vline(
        x=independent_start,
        line={"color": VALIDATION_INDEPENDENT_COLOR, "width": 2, "dash": "dash"},
    )
    fig.add_annotation(
        x=research_start + (research_end - research_start) / 2,
        y=0.75,
        text="RICERCA GIÀ OSSERVATA<br>500 finestre · 300 OOS · 5 fold",
        showarrow=False,
        font={"size": 12, "color": TEXT},
    )
    if len(tail) == 2:
        fig.add_annotation(
            x=pd.Timestamp(tail[0]) + (pd.Timestamp(tail[1]) - pd.Timestamp(tail[0])) / 2,
            y=0.26,
            text="coda osservata<br>non indipendente",
            showarrow=False,
            font={"size": 10, "color": VALIDATION_TAIL_COLOR},
        )
    fig.add_annotation(
        x=independent_start + (independent_end - independent_start) / 2,
        y=0.75,
        text="VALIDAZIONE INDIPENDENTE<br>nessun dato disponibile",
        showarrow=False,
        font={"size": 12, "color": VALIDATION_INDEPENDENT_COLOR},
    )
    fig.update_yaxes(visible=False, range=[0.0, 1.05])
    fig.update_xaxes(title_text="Tempo (UTC)")
    return _apply_layout(
        fig,
        "Separazione temporale · ricerca, coda osservata, validazione attesa",
        height,
        legend=False,
    )
