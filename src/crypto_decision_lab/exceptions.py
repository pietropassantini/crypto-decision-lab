"""Audited source anomalies. Never synthesize market data."""
from __future__ import annotations

from .market import utc_ms

# Precise Binance Spot BTCUSDT 1h interruption, verified via raw API output.
KNOWN_SOURCE_EVENTS = (
    {
        'id': 'BINANCE_BTCUSDT_20210211_MAINTENANCE',
        'provider': 'binance-spot', 'symbol': 'BTCUSDT', 'interval': '1h',
        'missing_open_ms': utc_ms('2021-02-11T04:00:00Z'),
        'partial_open_ms': utc_ms('2021-02-11T03:00:00Z'),
        'actual_close_ms': 1613014854773,
        'reason': 'historical exchange interruption; observed in source API',
    },
)
