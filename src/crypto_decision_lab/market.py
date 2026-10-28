"""Binance public Spot 1h klines; UTC timestamps, no authenticated endpoints."""
from __future__ import annotations

from datetime import datetime, timezone
import time

import pandas as pd
import requests

HOUR_MS = 3_600_000
BASE_URL = "https://data-api.binance.vision"
COLUMNS = ["open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume", "trades", "taker_buy_base", "taker_buy_quote", "ignore"]
NUMERIC = ["open", "high", "low", "close", "volume", "quote_volume", "taker_buy_base", "taker_buy_quote"]


def utc_ms(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Specify a timezone: use ISO-8601 with Z or +00:00")
    return int(parsed.astimezone(timezone.utc).timestamp() * 1000)


def normalize_klines(rows: list) -> pd.DataFrame:
    if not rows:
        raise ValueError("No klines returned")
    frame = pd.DataFrame(rows, columns=COLUMNS)
    for name in NUMERIC:
        frame[name] = pd.to_numeric(frame[name], errors="raise")
    for name in ["open_time", "close_time", "trades"]:
        frame[name] = pd.to_numeric(frame[name], errors="raise").astype("int64")
    frame["timestamp"] = pd.to_datetime(frame["open_time"], unit="ms", utc=True)
    return frame[["timestamp", "open", "high", "low", "close", "volume", "quote_volume", "trades", "open_time", "close_time", "taker_buy_base", "taker_buy_quote"]].sort_values("timestamp", kind="stable").reset_index(drop=True)


def fetch_klines(start_ms: int, end_ms: int, *, symbol: str = "BTCUSDT", session=None, pause: float = 0.2) -> pd.DataFrame:
    """Fetch complete [start_ms, end_ms) hourly bars. End must be a closed-bar boundary."""
    if not symbol.isalnum() or symbol.upper() != symbol:
        raise ValueError("symbol must be uppercase alphanumeric")
    if start_ms >= end_ms or start_ms % HOUR_MS or end_ms % HOUR_MS:
        raise ValueError("start and end must be ordered UTC hour boundaries")
    if end_ms > (int(time.time() * 1000) // HOUR_MS) * HOUR_MS:
        raise ValueError("end cannot include an unfinished hourly candle")
    client = session or requests.Session()
    cursor = start_ms
    output = []
    while cursor < end_ms:
        response = None
        for attempt in range(5):
            try:
                response = client.get(f"{BASE_URL}/api/v3/klines", params={"symbol": symbol, "interval": "1h", "startTime": cursor, "endTime": end_ms - 1, "limit": 1000}, timeout=20)
                if response.status_code in (418, 429) or response.status_code >= 500:
                    if attempt == 4: response.raise_for_status()
                    time.sleep(min(2 ** attempt, 12))
                    continue
                response.raise_for_status()
                break
            except requests.RequestException:
                if attempt == 4: raise
                time.sleep(min(2 ** attempt, 12))
        rows = response.json()
        if not isinstance(rows, list) or not rows:
            raise RuntimeError(f"Empty or invalid exchange response starting at {cursor}; refusing partial dataset")
        times = [int(r[0]) for r in rows]
        if times != sorted(set(times)) or times[0] != cursor or any(x >= end_ms for x in times):
            raise RuntimeError("Unexpected timestamps or duplicates returned by provider")
        output.extend(rows)
        next_cursor = times[-1] + HOUR_MS
        if next_cursor <= cursor:
            raise RuntimeError("Pagination stalled")
        cursor = next_cursor
        if cursor < end_ms and pause:
            time.sleep(pause)
    return normalize_klines(output)
