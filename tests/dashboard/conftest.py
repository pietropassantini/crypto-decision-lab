from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab.dashboard.data_loader import digest_file
from crypto_decision_lab.dashboard.forecast_adapter import clear_forecast_cache, load_forecasts


def make_market_frame(n: int = 240, start: str = "2024-01-01", freq: str = "1h") -> pd.DataFrame:
    index = pd.date_range(start, periods=n, freq=freq, tz="UTC", name="timestamp")
    steps = np.arange(n, dtype=float)
    close = 100.0 + 0.20 * steps + 3.0 * np.sin(steps / 7.0)
    open_ = np.concatenate([[close[0]], close[:-1]])
    high = np.maximum(open_, close) + 0.40
    low = np.minimum(open_, close) - 0.40
    volume = 1000.0 + 10.0 * steps
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


def write_snapshot(data_root: Path, frame: pd.DataFrame) -> Path:
    snapshot = data_root / "snapshots" / "BTCUSDT-1h-test-v1"
    chunks_dir = data_root / "chunks" / "BTCUSDT" / "1h"
    snapshot.mkdir(parents=True, exist_ok=True)
    chunks_dir.mkdir(parents=True, exist_ok=True)
    chunks = []
    chunk_size = 100
    for offset in range(0, len(frame), chunk_size):
        part = frame.iloc[offset:offset + chunk_size].copy()
        part.index.name = "timestamp"
        start_ms = int(part.index.min().timestamp() * 1000)
        end_ms = int((part.index.max() + pd.Timedelta(hours=1)).timestamp() * 1000)
        path = chunks_dir / f"{pd.Timestamp(start_ms, unit='ms', tz='UTC'):%Y%m%dT%H%M}_{pd.Timestamp(end_ms, unit='ms', tz='UTC'):%Y%m%dT%H%M}.parquet"
        part.reset_index().to_parquet(path, index=False)
        chunks.append(
            {
                "file": str(path.relative_to(data_root)).replace("\\", "/"),
                "sha256": digest_file(path),
                "status": "VALID",
                "rows": len(part),
                "expected_rows": len(part),
                "start_ms": start_ms,
                "end_ms": end_ms,
            }
        )
    raw_manifest = {
        "schema_version": 1,
        "dataset_id": "BTCUSDT-1h-test-v1",
        "source_manifest_sha256": "a" * 64,
        "symbol": "BTCUSDT",
        "interval": "1h",
        "chunks": chunks,
        "anomalies": [],
    }
    (snapshot / "raw-manifest.json").write_text(__import__("json").dumps(raw_manifest, indent=2))
    (snapshot / "report.json").write_text(
        __import__("json").dumps(
            {
                "dataset_id": "BTCUSDT-1h-test-v1",
                "source_manifest_sha256": "a" * 64,
                "lookback_hours": 168,
                "horizon_hours": 24,
            },
            indent=2,
        )
    )
    return snapshot


def write_forecast_archive(data_root: Path, frame: pd.DataFrame) -> dict:
    batch = data_root / "experiments" / "S02-CP02-batch-100"
    residual = data_root / "experiments" / "S02-CP05-residual-baselines-v1"
    batch.mkdir(parents=True, exist_ok=True)
    residual.mkdir(parents=True, exist_ok=True)
    cutoffs = [frame.index[100], frame.index[148]]
    config = {
        "schema_version": 1,
        "experiment": "S02-CP02",
        "model": "mistralai/Timestral-209M",
        "revision": "test-rev",
        "window_indices": list(range(len(cutoffs))),
        "source_manifest_sha256": "a" * 64,
        "windows_sha256": "b" * 64,
        "lookback": 168,
        "horizon": 24,
    }
    (batch / "config.json").write_text(__import__("json").dumps(config, indent=2))
    cp05_rows = []
    expected = {}
    for window_index, cutoff in enumerate(cutoffs):
        targets = pd.date_range(cutoff, periods=24, freq="1h", tz="UTC")
        actual = frame.loc[targets, "close"].to_numpy(dtype=float)
        anchor = float(frame.loc[cutoff - pd.Timedelta(hours=1), "close"])
        p50 = actual * 1.01 + 0.25
        p10 = p50 - 2.0
        p90 = p50 + 2.0
        forecast = pd.DataFrame(
            {
                "timestamp": targets,
                "actual": actual,
                "timestral_p10": p10,
                "timestral_p50": p50,
                "timestral_p90": p90,
                "naive": np.full(24, anchor),
            }
        )
        folder = batch / f"window-{window_index:04d}"
        folder.mkdir()
        forecast.to_parquet(folder / "forecast.parquet", index=False)
        metrics = {
            "experiment": "S02-CP02",
            "model": "mistralai/Timestral-209M",
            "revision_requested": "test-rev",
            "window_index": window_index,
            "provenance": {
                "forecast_start_utc": str(cutoff),
                "target_end_exclusive_utc": str(cutoff + pd.Timedelta(hours=24)),
            },
            "last_context_close": anchor,
            "metrics": {},
            "forecast_sha256": digest_file(folder / "forecast.parquet"),
        }
        (folder / "metrics.json").write_text(__import__("json").dumps(metrics, indent=2))
        target_24 = targets[-1]
        cp05_rows.append(
            {
                "fold": 0,
                "window_index": window_index,
                "forecast_start_utc": str(cutoff),
                "train_count": 1,
                "actual_return_pct_24h": (actual[-1] / anchor - 1) * 100,
                "timestral_return_pct_24h": (p50[-1] / anchor - 1) * 100,
                "naive_return_pct_24h": 0.0,
                "actual_price_24h": actual[-1],
                "timestral_price_24h": p50[-1],
                "naive_price_24h": anchor,
                "ridge_predicted_residual_pct": 0.5,
                "ridge_return_pct_24h": 0.5,
                "ridge_price_24h": actual[-1] * 1.002,
                "histgb_predicted_residual_pct": -0.5,
                "histgb_return_pct_24h": -0.5,
                "histgb_price_24h": actual[-1] * 0.998,
            }
        )
        expected[window_index] = {
            "cutoff": cutoff,
            "target_24": target_24,
            "actual_24": actual[-1],
            "anchor": anchor,
            "p50_24": p50[-1],
        }
    cp05 = pd.DataFrame(cp05_rows)
    cp05.to_parquet(residual / "predictions.parquet", index=False)
    summary = {
        "evaluation_rows": len(cp05),
        "folds": 1,
        "predictions_sha256": digest_file(residual / "predictions.parquet"),
        "metrics": {},
    }
    (residual / "summary.json").write_text(__import__("json").dumps(summary, indent=2))
    return {"batch": batch, "residual": residual, "expected": expected, "config": config}


@dataclass
class FixtureArchive:
    data_root: Path
    snapshot: Path
    market: pd.DataFrame
    paths: dict
    forecasts: object


@pytest.fixture(autouse=True)
def clear_caches():
    clear_forecast_cache()
    from crypto_decision_lab.dashboard.data_loader import clear_market_cache

    clear_market_cache()
    yield
    clear_forecast_cache()
    clear_market_cache()


@pytest.fixture
def synthetic_market() -> pd.DataFrame:
    return make_market_frame()


@pytest.fixture
def archive(tmp_path, synthetic_market) -> FixtureArchive:
    data_root = tmp_path / "data"
    snapshot = write_snapshot(data_root, synthetic_market)
    paths = write_forecast_archive(data_root, synthetic_market)
    forecasts = load_forecasts(paths["batch"], paths["residual"], snapshot)
    return FixtureArchive(data_root, snapshot, synthetic_market, paths, forecasts)
