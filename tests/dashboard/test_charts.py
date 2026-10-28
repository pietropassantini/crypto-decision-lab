from __future__ import annotations

import pandas as pd

from crypto_decision_lab.dashboard.charts import (
    make_actual_vs_predicted_figure,
    make_forecast_inspector_figure,
    make_market_figure,
    make_regime_figure,
)
from crypto_decision_lab.dashboard.forecast_adapter import align_models
from crypto_decision_lab.dashboard.indicators import IndicatorRequest, compute_indicators
from crypto_decision_lab.dashboard.metrics import add_error_columns, assign_regimes, regime_metrics


def _trace_by_name(fig, name):
    return next(trace for trace in fig.data if trace.name == name)


def test_h24_forecast_points_are_markers_not_false_lines(archive):
    points = align_models(archive.forecasts.points, ["timestral", "naive", "ridge", "histgb"], horizon=24)
    fig = make_market_figure(
        archive.market,
        timeframe="1h",
        view="line",
        forecast_points=points,
        forecast_mode="h24_common",
        colors={"actual": "#fff", "timestral": "#0ff", "naive": "#999", "ridge": "#f90", "histgb": "#90f"},
    )
    assert _trace_by_name(fig, "Timestral-209M").mode == "markers"
    assert _trace_by_name(fig, "Naive (persistence)").mode == "markers"
    assert _trace_by_name(fig, "Ridge residual").mode == "markers"
    assert _trace_by_name(fig, "HistGradientBoosting residual").mode == "markers"
    assert _trace_by_name(fig, "BTCUSDT close").mode == "lines"


def test_cutoff_path_is_drawn_only_for_selected_window(archive):
    points = archive.forecasts.points_for(["timestral", "naive", "ridge"], window_index=0)
    fig = make_market_figure(
        archive.market,
        timeframe="1h",
        view="candlestick",
        forecast_points=points,
        forecast_mode="cutoff_path",
        selected_cutoff=archive.paths["expected"][0]["cutoff"],
        colors={"actual": "#fff", "timestral": "#0ff", "naive": "#999", "ridge": "#f90"},
    )
    assert _trace_by_name(fig, "Timestral-209M").mode == "lines+markers"
    assert _trace_by_name(fig, "Naive (persistence)").mode == "lines+markers"
    assert _trace_by_name(fig, "Ridge residual").mode == "markers"
    assert any(getattr(trace, "type", "") == "candlestick" for trace in fig.data)


def test_market_figure_with_indicators_and_inspector(archive):
    results = compute_indicators(
        archive.market,
        [
            IndicatorRequest("sma", {"window": 7}),
            IndicatorRequest("ema", {"window": 9}),
            IndicatorRequest("rsi", {"period": 14}),
            IndicatorRequest("volume_ma", {"window": 5}),
        ],
    )
    overlays = [result for result in results if result.spec.kind == "overlay"]
    oscillators = [result for result in results if result.spec.kind == "oscillator"]
    volume_overlays = [result for result in results if result.spec.kind == "volume"]
    fig = make_market_figure(
        archive.market,
        timeframe="1h",
        view="line",
        overlays=overlays,
        oscillators=oscillators,
        volume_overlays=volume_overlays,
        show_volume=True,
    )
    assert any(trace.name and trace.name.startswith("SMA") for trace in fig.data)
    assert any(trace.name and trace.name.startswith("RSI") for trace in fig.data)
    points = archive.forecasts.points_for(
        ["timestral", "naive", "ridge"], window_index=0
    )
    inspector = make_forecast_inspector_figure(
        archive.market,
        points,
        target=archive.paths["expected"][0]["target_24"],
        cutoff=archive.paths["expected"][0]["cutoff"],
    )
    assert len(inspector.data) >= 4


def test_comparison_and_regime_figures(archive):
    aligned = align_models(
        archive.forecasts.points, ["timestral", "naive", "ridge", "histgb"], horizon=24
    )
    residuals = add_error_columns(aligned)
    fig = make_actual_vs_predicted_figure(residuals, actual_series=archive.market["close"])
    assert any(trace.mode == "markers" for trace in fig.data)
    regimes = assign_regimes(residuals, archive.market)
    table = regime_metrics(regimes, "volatility_regime", ["timestral", "naive", "ridge", "histgb"])
    regime_fig = make_regime_figure(table, regime_column="volatility_regime")
    assert len(regime_fig.data) >= 2
