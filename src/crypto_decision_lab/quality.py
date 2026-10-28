"""Hard failures for integrity issues; no silent gap filling."""
from __future__ import annotations

import pandas as pd
from .market import HOUR_MS


def check_quality(df: pd.DataFrame, start_ms: int, end_ms: int) -> dict:
    issues = []
    expected = (end_ms - start_ms) // HOUR_MS
    if df.empty:
        return {"ok": False, "rows": 0, "expected_rows": expected, "issues": ["empty dataset"]}
    ts = df["open_time"].astype("int64")
    expected_times = pd.RangeIndex(start_ms, end_ms, HOUR_MS)
    actual = set(ts.tolist())
    missing = set(expected_times) - actual
    extra = actual - set(expected_times)
    duplicate_count = int(ts.duplicated().sum())
    if duplicate_count: issues.append(f"duplicate timestamps: {duplicate_count}")
    if missing: issues.append(f"missing hours: {len(missing)}, first: {pd.to_datetime(min(missing), unit='ms', utc=True)}")
    if extra: issues.append(f"out of range hours: {len(extra)}")
    if (ts % HOUR_MS != 0).any(): issues.append("unaligned hourly timestamps")
    if not ts.is_monotonic_increasing: issues.append("timestamps are not sorted")
    if (df["close_time"].astype("int64") != ts + HOUR_MS - 1).any(): issues.append("incorrect candle close time")
    if df.isna().any().any(): issues.append("missing or NaN cell values")
    for field in ["open", "high", "low", "close"]:
        if (df[field] <= 0).any(): issues.append(f"nonpositive {field}")
    if (df["high"] < df[["open", "close", "low"]].max(axis=1)).any(): issues.append("high inconsistent with OHLC")
    if (df["low"] > df[["open", "close", "high"]].min(axis=1)).any(): issues.append("low inconsistent with OHLC")
    for field in ["volume", "quote_volume", "trades", "taker_buy_base", "taker_buy_quote"]:
        if (df[field] < 0).any(): issues.append(f"negative {field}")
    if (df["taker_buy_base"] > df["volume"] + 1e-9).any(): issues.append("taker base volume exceeds volume")
    return {"ok": not issues, "rows": len(df), "expected_rows": expected, "start_utc": str(pd.to_datetime(start_ms, unit='ms', utc=True)), "end_exclusive_utc": str(pd.to_datetime(end_ms, unit='ms', utc=True)), "issues": issues}
