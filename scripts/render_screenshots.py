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
    make_market_figure,
)
from crypto_decision_lab.dashboard.config import load_config
from crypto_decision_lab.dashboard.data_loader import filter_range, load_market_data, resample_ohlcv
from crypto_decision_lab.dashboard.forecast_adapter import align_models, load_forecasts
from crypto_decision_lab.dashboard.indicators import IndicatorRequest, compute_indicators
from crypto_decision_lab.dashboard.metrics import add_error_columns


def main() -> None:
    config = load_config(repo_root=ROOT)
    market = load_market_data(config.data_root, config.snapshot_dir, verify_checksums=True)
    forecasts = load_forecasts(
        config.batch_dir,
        config.residual_baselines_dir,
        config.snapshot_dir,
        config.decision_glob,
    )
    out = ROOT / "docs" / "screenshots"
    out.mkdir(parents=True, exist_ok=True)
    start = market.latest_timestamp - pd.Timedelta(days=365)
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
        ["timestral", "naive", "ridge", "histgb"],
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
        title="Bitcoin Forecast Explorer · BTCUSDT · ultimi 365 giorni",
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
    print(f"Wrote {len(list(out.glob('*.png')))} screenshots to {out}")
    print("Note: kaleido needs a local Chrome/Chromium; the dashboard exports PNG from the Plotly toolbar as well.")


if __name__ == "__main__":
    main()
