"""Render static dashboard previews (optional, requires kaleido).

Usage (from repository root)::

    python scripts/render_screenshots.py

The script is read-only: it writes only to docs/screenshots/.
"""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd

from crypto_decision_lab.dashboard.charts import (
    make_actual_vs_predicted_figure,
    make_cost_sensitivity_figure,
    make_latency_robustness_figure,
    make_market_figure,
    make_period_pnl_figure,
    make_pnl_distribution_figure,
    make_profit_concentration_figure,
    make_scenario_interval_figure,
    make_stability_equity_figure,
    make_threshold_surface_figure,
    make_underwater_figure,
    make_validation_timeline_figure,
)
from crypto_decision_lab.dashboard.config import load_config
from crypto_decision_lab.dashboard.data_loader import filter_range, load_market_data, resample_ohlcv
from crypto_decision_lab.dashboard.forecast_adapter import align_models, load_forecasts
from crypto_decision_lab.dashboard.indicators import IndicatorRequest, compute_indicators
from crypto_decision_lab.dashboard.metrics import add_error_columns
from crypto_decision_lab.dashboard.parameter_robustness_adapter import load_robustness_archive
from crypto_decision_lab.dashboard.stability_adapter import load_stability_archive
from crypto_decision_lab.dashboard.validation_adapter import load_validation_archive


def main() -> None:
    config = load_config(repo_root=ROOT)
    market = load_market_data(config.data_root, config.snapshot_dir, verify_checksums=True)
    active_batch = (
        config.expansion_dir
        if config.expansion_dir and (config.expansion_dir / "config.json").is_file()
        else config.batch_dir
    )
    forecasts = load_forecasts(
        active_batch,
        config.residual_baselines_dir,
        config.snapshot_dir,
        config.decision_glob,
        config.residual_corrected_dir,
    )
    out = ROOT / "docs" / "screenshots"
    out.mkdir(parents=True, exist_ok=True)
    start = market.first_timestamp
    end = market.latest_timestamp + pd.Timedelta(hours=1)
    market_slice = filter_range(market.frame, start, end)
    daily = resample_ohlcv(market_slice, "1D")
    indicators = compute_indicators(
        daily,
        [
            IndicatorRequest("sma", {"window": 20}),
            IndicatorRequest("ema", {"window": 50}),
            IndicatorRequest("rsi", {"period": 14}),
            IndicatorRequest("volume_ma", {"window": 20}),
        ],
    )
    common = align_models(
        forecasts.points[(forecasts.points["target"] >= start) & (forecasts.points["target"] < end)],
        ["timestral", "naive"],
        horizon=24,
    )
    overview = make_market_figure(
        daily,
        timeframe="1D",
        view="candlestick",
        overlays=[result for result in indicators if result.spec.kind == "overlay"],
        oscillators=[result for result in indicators if result.spec.kind == "oscillator"],
        volume_overlays=[result for result in indicators if result.spec.kind == "volume"],
        show_volume=True,
        forecast_points=common,
        forecast_mode="h24_common",
        colors=config.colors,
        title="Bitcoin Forecast Explorer · BTCUSDT · 500 finestre canoniche",
        height=900,
    )
    overview.write_image(out / "overview.png", width=1600, height=900, scale=1)

    residuals = add_error_columns(common)
    comparison = make_actual_vs_predicted_figure(
        residuals,
        actual_series=market_slice["close"],
        colors=config.colors,
        title="Real vs predicted · target +24h comuni",
        height=520,
    )
    comparison.write_image(out / "model_comparison.png", width=1500, height=520, scale=1)

    _render_stability(out, config)
    _render_robustness(out, config)
    _render_validation(out, config)

    print(f"Wrote {len(list(out.glob('*.png')))} screenshots to {out}")
    print("Note: kaleido needs a local Chrome/Chromium; the dashboard exports PNG from the Plotly toolbar as well.")


def _render_stability(out: Path, config) -> None:
    """S02-CP08-02 figures, only if the additive archive is present (read-only)."""

    directory = config.momentum_stability_dir
    if directory is None or not (Path(directory) / "manifest.json").is_file():
        print("S02-CP08-02 archive not found: skipping momentum stability figures")
        return
    archive = load_stability_archive(directory)
    figures = {
        "momentum_stability_equity.png": make_stability_equity_figure(
            archive.equity, initial_capital=archive.initial_capital, height=460
        ),
        "momentum_stability_underwater.png": make_underwater_figure(archive.equity, height=340),
        "momentum_stability_pnl_distribution.png": make_pnl_distribution_figure(
            archive.trades, metric="pnl_usdt"
        ),
        "momentum_stability_concentration.png": make_profit_concentration_figure(
            archive.concentration_curve
        ),
        "momentum_stability_period_pnl.png": make_period_pnl_figure(
            archive.periods, granularity="month"
        ),
    }
    for filename, figure in figures.items():
        figure.write_image(out / filename, width=1500, height=460, scale=1)
    print(f"Wrote {len(figures)} S02-CP08-02 figures to {out}")


def _render_robustness(out: Path, config) -> None:
    """S02-CP08-03 figures, only if the additive archive is present (read-only)."""

    directory = config.parameter_robustness_dir
    if directory is None or not (Path(directory) / "manifest.json").is_file():
        print("S02-CP08-03 archive not found: skipping parameter robustness figures")
        return
    archive = load_robustness_archive(directory)
    figures = {
        "parameter_robustness_cost.png": make_cost_sensitivity_figure(
            archive.scenarios, height=460
        ),
        "parameter_robustness_latency.png": make_latency_robustness_figure(
            archive.scenarios, height=460
        ),
        "parameter_robustness_threshold_surface.png": make_threshold_surface_figure(
            archive.surface, height=460
        ),
        "parameter_robustness_intervals.png": make_scenario_interval_figure(
            archive.scenarios, height=460
        ),
    }
    for filename, figure in figures.items():
        figure.write_image(out / filename, width=1500, height=460, scale=1)
    print(f"Wrote {len(figures)} S02-CP08-03 figures to {out}")


def _render_validation(out: Path, config) -> None:
    """S02-CP08-04 figure, only if the additive protocol archive is present."""

    directory = config.validation_protocol_dir
    if directory is None or not (Path(directory) / "manifest.json").is_file():
        print("S02-CP08-04 archive not found: skipping validation protocol figure")
        return
    archive = load_validation_archive(directory)
    min_days = int(
        archive.summary.get("criteria", {})
        .get("sample_requirements", {})
        .get("min_independent_days", 180)
    )
    figure = make_validation_timeline_figure(
        archive.data_registry, min_independent_days=min_days, height=460
    )
    figure.write_image(out / "validation_protocol_timeline.png", width=1500, height=460, scale=1)
    print(f"Wrote 1 S02-CP08-04 figure to {out}")


if __name__ == "__main__":
    main()
