# Binance Spot response fixtures (S07)

Shapes follow the official Binance Spot REST documentation (developers.binance.com,
trading-endpoints, errors, filters, enums; pages read 2026-11-13). Values are synthetic:
no account data, no credentials. Used by `tests/test_live.py` to verify the adapter parsing
and the error classification (rejected / unknown / not found).
