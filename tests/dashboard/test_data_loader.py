from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab.dashboard.data_loader import (
    DataValidationError,
    display_frame_with_gaps,
    filter_range,
    load_market_data,
    normalize_ohlcv,
    resample_ohlcv,
)


def test_load_snapshot_preserves_rows_and_provenance(archive):
    market = load_market_data(archive.data_root, archive.snapshot)
    assert market.rows == len(archive.market)
    assert market.dataset_id == "BTCUSDT-1h-test-v1"
    assert market.source_manifest_sha256 == "a" * 64
    assert market.source_files == 3
    assert market.latest_timestamp == archive.market.index.max()
    assert list(market.frame.columns) == ["open", "high", "low", "close", "volume"]


def test_missing_hours_are_reported_not_filled(tmp_path):
    from conftest import make_market_frame, write_snapshot

    frame = make_market_frame(120)
    missing_timestamp = frame.index[50]
    frame = frame.drop(index=missing_timestamp)
    snapshot = write_snapshot(tmp_path / "gapdata", frame)
    market = load_market_data(tmp_path / "gapdata", snapshot, verify_checksums=True)
    assert market.rows == 119
    assert market.gap_count == 1
    assert market.missing_hours[0] == missing_timestamp
    assert missing_timestamp not in market.frame.index
    assert any("missing hourly bars" in issue for issue in market.issues)


def test_checksum_mismatch_is_rejected(archive):
    chunk = next((archive.data_root / "chunks").glob("**/*.parquet"))
    chunk.write_bytes(chunk.read_bytes() + b"tamper")
    with pytest.raises(DataValidationError, match="Checksum changed"):
        load_market_data(archive.data_root, archive.snapshot, verify_checksums=True)


def test_duplicate_and_invalid_ohlcv_rejected(synthetic_market):
    duplicated = pd.concat([synthetic_market.iloc[:5], synthetic_market.iloc[[3]]])
    with pytest.raises(DataValidationError, match="Duplicate timestamps"):
        normalize_ohlcv(duplicated)
    invalid = synthetic_market.iloc[:5].copy()
    invalid.iloc[2, invalid.columns.get_loc("high")] = invalid.iloc[2]["low"] - 1
    with pytest.raises(DataValidationError, match="high < low"):
        normalize_ohlcv(invalid)
    empty = synthetic_market.iloc[0:0]
    with pytest.raises(DataValidationError, match="Empty"):
        normalize_ohlcv(empty)


def test_resample_ohlcv_hourly_to_daily_and_four_hours():
    index = pd.date_range("2024-01-01", periods=6, freq="1h", tz="UTC", name="timestamp")
    frame = pd.DataFrame(
        {
            "open": [10, 11, 12, 13, 14, 15],
            "high": [12, 13, 14, 15, 16, 17],
            "low": [9, 10, 11, 12, 13, 14],
            "close": [11, 12, 13, 14, 15, 16],
            "volume": [1, 2, 3, 4, 5, 6],
        },
        index=index,
    )
    daily = resample_ohlcv(frame, "1D")
    assert len(daily) == 1
    row = daily.iloc[0]
    assert row["open"] == 10 and row["close"] == 16
    assert row["high"] == 17 and row["low"] == 9
    assert row["volume"] == 21
    four = resample_ohlcv(frame, "4h")
    assert len(four) == 2
    assert four.iloc[1]["open"] == 14 and four.iloc[1]["close"] == 16
    assert four.iloc[1]["volume"] == 11
    hourly = resample_ohlcv(frame, "1h")
    assert hourly.equals(normalize_ohlcv(frame))


def test_display_frame_inserts_nan_gaps_without_filling(synthetic_market):
    frame = synthetic_market.iloc[:10].drop(index=synthetic_market.index[4])
    display = display_frame_with_gaps(frame, "1h")
    assert len(display) == 10
    assert display.index[4] == synthetic_market.index[4]
    assert display.iloc[4].isna().all()
    assert display["close"].isna().sum() == 1


def test_filter_range_is_end_exclusive(synthetic_market):
    start = synthetic_market.index[10]
    end = synthetic_market.index[20]
    filtered = filter_range(synthetic_market, start, end)
    assert filtered.index.min() == start
    assert filtered.index.max() == synthetic_market.index[19]
    assert len(filtered) == 10


def test_daily_indicators_are_computed_on_daily_bars(synthetic_market):
    from crypto_decision_lab.dashboard.indicators import compute_indicator

    daily = resample_ohlcv(synthetic_market, "1D")
    sma = compute_indicator(daily, "sma", {"window": 3})["sma"]
    assert sma.index.equals(daily.index)
    assert sma.notna().sum() > 0
