"""Read-only hourly feed from the public Binance Spot market data (no account, no keys).

A feed for persistent paper sessions on the real market (E05-P1).  Record ``t`` holds the
open of the bar starting at ``t`` (observable at ``t``) and the close of bar ``t - 1h``, like
the lab feed; the bar in progress contributes only its open, never a close.

Each :meth:`PublicKlineSource.records` call reads the last ``history_hours`` closed bars,
paginated, plus the current bar's open.  Missing hours (exchange maintenance) are not an
error: they are absent from the view and the session handles them (gap, undefined features).
Only the market data host is used; nothing here can place an order.
"""
from __future__ import annotations

import time
from typing import Any

import pandas as pd

from .execution_contract import HOUR
from .lab_feed import FeedError, FeedRecord, FeedView

BASE_URL = "https://data-api.binance.vision"
HOUR_MS = 3_600_000
PAGE = 1000
DEFAULT_HISTORY_HOURS = 2600   # E05: 100 daily closes (2376 h) plus margin


def new_session():
    """HTTP client factory; tests replace it."""
    import requests

    return requests.Session()


class PublicKlineSource:
    kind = "public_klines"

    def __init__(self, symbol: str = "BTCUSDT", history_hours: int = DEFAULT_HISTORY_HOURS, *,
                 base_url: str = BASE_URL, session=None, pause: float = 0.2, retries: int = 4) -> None:
        if not symbol.isalnum() or symbol.upper() != symbol:
            raise FeedError("symbol must be uppercase alphanumeric")
        if int(history_hours) < 48:
            raise FeedError("history_hours must be at least 48")
        self.symbol, self.history_hours = symbol, int(history_hours)
        self.base_url, self.pause, self.retries = base_url.rstrip("/"), pause, int(retries)
        self._session = session

    # ------------------------------------------------------------ transport
    def _client(self):
        if self._session is None:
            self._session = new_session()
        return self._session

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self._client().get(f"{self.base_url}{path}", params=params, timeout=20)
                if response.status_code in (418, 429) or response.status_code >= 500:
                    raise FeedError(f"exchange answered {response.status_code} for {path}")
                response.raise_for_status()
                return response.json()
            except Exception as exc:  # network error, throttling, bad payload: retry, then fail the poll
                last = exc
                if attempt < self.retries and self.pause:
                    time.sleep(min(2 ** attempt, 12))
        raise FeedError(f"public market data unavailable ({path}): {last}")

    # ------------------------------------------------------------ feed interface
    def now(self) -> pd.Timestamp:
        payload = self._get("/api/v3/time", {})
        try:
            return pd.Timestamp(int(payload["serverTime"]), unit="ms", tz="UTC")
        except (KeyError, TypeError, ValueError) as exc:
            raise FeedError(f"invalid server time answer: {payload!r}") from exc

    def records(self) -> FeedView:
        now = self.now()
        hour = now.floor("h")
        hour_ms = int(hour.timestamp() * 1000)
        cursor = hour_ms - self.history_hours * HOUR_MS
        rows: list[list] = []
        while cursor < hour_ms:
            page = self._get("/api/v3/klines", {"symbol": self.symbol, "interval": "1h", "startTime": cursor,
                                                "endTime": hour_ms - 1, "limit": PAGE})
            if not isinstance(page, list):
                raise FeedError(f"invalid klines answer: {page!r}")
            if not page:
                break
            times = [int(row[0]) for row in page]
            if times != sorted(set(times)) or times[0] < cursor or times[-1] >= hour_ms:
                raise FeedError("klines out of order, duplicated or outside the requested range")
            rows.extend(page)
            cursor = times[-1] + HOUR_MS
            if len(page) < PAGE:
                break
            if self.pause:
                time.sleep(self.pause)
        current = self._get("/api/v3/klines", {"symbol": self.symbol, "interval": "1h", "startTime": hour_ms,
                                               "limit": 1})
        if not isinstance(current, list):
            raise FeedError(f"invalid klines answer: {current!r}")
        return self._view(rows, [r for r in current if int(r[0]) == hour_ms], hour)

    def _view(self, closed: list[list], current: list[list], hour: pd.Timestamp) -> FeedView:
        try:
            closes = {pd.Timestamp(int(r[0]), unit="ms", tz="UTC"): float(r[4]) for r in closed}
            view = FeedView()
            for row in closed + current:
                stamp = pd.Timestamp(int(row[0]), unit="ms", tz="UTC")
                view.records[stamp] = FeedRecord(stamp, float(row[1]), closes.get(stamp - HOUR))
        except (IndexError, TypeError, ValueError) as exc:
            raise FeedError(f"malformed kline row: {exc}") from exc
        return view

    def identity(self) -> dict:
        return {"kind": self.kind, "symbol": self.symbol, "history_hours": self.history_hours,
                "base_url": self.base_url}
