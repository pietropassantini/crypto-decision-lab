"""S07 Binance Spot adapter behind the :mod:`.exchange` execution contract.

Verified against the official Binance Spot REST documentation
(developers.binance.com, pages last modified 2026-11-12; see
``docs/S07-live-adapter.md``):

* signed endpoints: HMAC-SHA256 of the query string, hex, header
  ``X-MBX-APIKEY``, ``timestamp`` in ms, ``recvWindow`` <= 60000 (5000 used);
* ``POST /api/v3/order`` MARKET with ``quantity`` or ``quoteOrderQty``, LIMIT
  GTC with ``price``; ``newOrderRespType=FULL`` returns ``fills``;
* ``GET /api/v3/order`` by ``origClientOrderId``; ``-2013`` = no such order;
* ``DELETE /api/v3/order`` by ``origClientOrderId``; ``-2011`` = cancel rejected;
* HTTP 5xx and ``-1007`` mean *execution status unknown*: never a failure.

Environments: ``testnet`` (https://testnet.binance.vision, virtual funds) is
the default and the only one allowed in S07.  ``mainnet`` requires both
``allow_mainnet=True`` and ``CDL_LIVE_ALLOW_MAINNET=1`` (S08 decision).
Credentials come only from environment variables, never from config files.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import time
from typing import Any, Callable
from urllib.parse import urlencode

from .exchange import (
    D,
    ExchangeError,
    ExchangeRejected,
    ExchangeUnknown,
    Fill,
    OrderNotFound,
    OrderReport,
    OrderRequest,
    SymbolRules,
)

BASE_URLS = {"testnet": "https://testnet.binance.vision", "mainnet": "https://api.binance.com"}
KEY_ENV = {"testnet": ("CDL_BINANCE_TESTNET_API_KEY", "CDL_BINANCE_TESTNET_API_SECRET"),
           "mainnet": ("CDL_BINANCE_API_KEY", "CDL_BINANCE_API_SECRET")}
MAINNET_ENV = "CDL_LIVE_ALLOW_MAINNET"
UNKNOWN_CODES = {-1006, -1007}  # unexpected response / timeout: execution status unknown
NOT_FOUND_CODES = {-2013}

# transport(method, url, params, headers, timeout) -> (http_status, json_payload)
Transport = Callable[[str, str, dict, dict, float], tuple[int, Any]]


def sign(secret: str, query: str) -> str:
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


class TransportUnknown(Exception):
    """Raised by a transport when the request may have reached the server."""


def requests_transport(method: str, url: str, params: dict, headers: dict, timeout: float):
    import requests

    try:
        response = requests.request(method, url, params=params, headers=headers, timeout=timeout)
    except requests.exceptions.ConnectTimeout as exc:  # never connected: nothing sent
        raise ConnectionError(str(exc)) from exc
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
        raise TransportUnknown(str(exc)) from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {"code": -1006, "msg": response.text[:200]}
    return response.status_code, payload


class BinanceSpotAdapter:
    name = "binance-spot"

    def __init__(self, environment: str = "testnet", *, api_key: str | None = None, api_secret: str | None = None,
                 transport: Transport | None = None, clock: Callable[[], float] = time.time,
                 recv_window: int = 5000, timeout: float = 10.0, allow_mainnet: bool = False) -> None:
        if environment not in BASE_URLS:
            raise ValueError(f"unknown environment: {environment}")
        if environment == "mainnet" and not (allow_mainnet and os.environ.get(MAINNET_ENV) == "1"):
            raise PermissionError("mainnet requires allow_mainnet and CDL_LIVE_ALLOW_MAINNET=1 (S08 activation)")
        key_env, secret_env = KEY_ENV[environment]
        self.environment = environment
        self.base_url = BASE_URLS[environment]
        self.api_key = api_key if api_key is not None else os.environ.get(key_env, "")
        self.api_secret = api_secret if api_secret is not None else os.environ.get(secret_env, "")
        self.transport = transport or requests_transport
        self.clock = clock
        self.recv_window = int(recv_window)
        self.timeout = float(timeout)
        self._rules: dict[str, SymbolRules] = {}

    # ------------------------------------------------------------ transport
    def _call(self, method: str, path: str, params: dict | None = None, *, signed: bool = False,
              mutating: bool = False) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        headers = {}
        if signed:
            if not self.api_key or not self.api_secret:
                raise ExchangeError(f"missing API credentials ({' / '.join(KEY_ENV[self.environment])})")
            params["recvWindow"] = self.recv_window
            params["timestamp"] = int(self.clock() * 1000)
            params["signature"] = sign(self.api_secret, urlencode(params))
            headers["X-MBX-APIKEY"] = self.api_key
        try:
            status, payload = self.transport(method, self.base_url + path, params, headers, self.timeout)
        except TransportUnknown as exc:
            raise ExchangeUnknown(f"{method} {path}: {exc}") from exc
        except ConnectionError as exc:
            if mutating:  # be conservative: a connect error is treated as unknown for writes
                raise ExchangeUnknown(f"{method} {path}: {exc}") from exc
            raise ExchangeError(f"{method} {path}: {exc}") from exc
        code = payload.get("code") if isinstance(payload, dict) else None
        message = payload.get("msg", "") if isinstance(payload, dict) else ""
        if status >= 500 or code in UNKNOWN_CODES:
            raise ExchangeUnknown(f"{method} {path}: HTTP {status} {code} {message}", code=code)
        if status >= 400:
            if code in NOT_FOUND_CODES or (path == "/api/v3/order" and method == "DELETE" and code == -2011
                                           and "Unknown order" in message):
                raise OrderNotFound(message or "Order does not exist.", code=code)
            if mutating or status == 400:
                raise ExchangeRejected(f"{method} {path}: {code} {message}", code=code)
            raise ExchangeError(f"{method} {path}: HTTP {status} {code} {message}", code=code)
        return payload

    # ------------------------------------------------------------ parsing
    @staticmethod
    def parse_rules(info: dict, symbol: str) -> SymbolRules:
        entry = next((s for s in info.get("symbols", []) if s.get("symbol") == symbol), None)
        if entry is None:
            raise ExchangeRejected(f"symbol not listed: {symbol}", code=-1121)
        filters = {f["filterType"]: f for f in entry.get("filters", [])}
        lot = filters.get("MARKET_LOT_SIZE") if D(filters.get("MARKET_LOT_SIZE", {}).get("stepSize", "0")) > 0 \
            else filters["LOT_SIZE"]
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {"minNotional": "0"}
        return SymbolRules(symbol, entry["baseAsset"], entry["quoteAsset"], D(lot["stepSize"]).normalize(),
                           D(lot["minQty"]), D(lot["maxQty"]), D(filters["PRICE_FILTER"]["tickSize"]).normalize(),
                           D(notional["minNotional"]),
                           int(entry.get("quoteAssetPrecision", entry.get("quotePrecision", 8))))

    @staticmethod
    def parse_order(payload: dict) -> OrderReport:
        fills = tuple(Fill(str(f["tradeId"]), D(f["price"]), D(f["qty"]), D(f["commission"]), f["commissionAsset"])
                      for f in payload.get("fills", []))
        return OrderReport(payload["clientOrderId"], str(payload["orderId"]), payload["symbol"], payload["side"],
                           payload["type"], payload["status"], D(payload["origQty"]), D(payload["executedQty"]),
                           D(payload["cummulativeQuoteQty"]), fills, D(payload.get("price") or "0"))

    @staticmethod
    def parse_trade(payload: dict) -> Fill:
        return Fill(str(payload["id"]), D(payload["price"]), D(payload["qty"]), D(payload["commission"]),
                    payload["commissionAsset"])

    # ------------------------------------------------------------ adapter API
    def identity(self) -> dict:
        return {"exchange": self.name, "environment": self.environment, "base_url": self.base_url}

    def server_time(self) -> int:
        return int(self._call("GET", "/api/v3/time")["serverTime"])

    def rules(self, symbol: str) -> SymbolRules:
        if symbol not in self._rules:
            self._rules[symbol] = self.parse_rules(self._call("GET", "/api/v3/exchangeInfo", {"symbol": symbol}),
                                                   symbol)
        return self._rules[symbol]

    def submit(self, request: OrderRequest) -> OrderReport:
        params = {"symbol": request.symbol, "side": request.side, "type": request.order_type,
                  "newClientOrderId": request.client_order_id, "newOrderRespType": "FULL",
                  "quantity": format(request.quantity, "f") if request.quantity is not None else None,
                  "quoteOrderQty": format(request.quote_quantity, "f") if request.quote_quantity is not None else None}
        if request.order_type == "LIMIT":
            params.update(timeInForce="GTC", price=format(request.price, "f"))
        return self.parse_order(self._call("POST", "/api/v3/order", params, signed=True, mutating=True))

    def query(self, symbol: str, client_order_id: str) -> OrderReport:
        report = self.parse_order(self._call("GET", "/api/v3/order",
                                             {"symbol": symbol, "origClientOrderId": client_order_id}, signed=True))
        if D(report.executed_quantity) > 0:  # GET /order has no fills: read them from myTrades
            report = OrderReport(**{**report.__dict__, "fills": tuple(self.trades(symbol, report.exchange_order_id))})
        return report

    def cancel(self, symbol: str, client_order_id: str) -> OrderReport:
        self._call("DELETE", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_order_id},
                   signed=True, mutating=True)
        return self.query(symbol, client_order_id)

    def open_orders(self, symbol: str) -> list[OrderReport]:
        return [self.parse_order(o) for o in self._call("GET", "/api/v3/openOrders", {"symbol": symbol}, signed=True)]

    trades_page_limit = 1000  # Binance maximum (default 500)

    def trades(self, symbol: str, exchange_order_id: str) -> list[Fill]:
        """All trades of one order, paginated with ``fromId`` (a page is at most ``limit`` trades)."""
        fills: list[Fill] = []
        params = {"symbol": symbol, "orderId": exchange_order_id, "limit": self.trades_page_limit}
        while True:
            page = self._call("GET", "/api/v3/myTrades", params, signed=True)
            fills.extend(self.parse_trade(t) for t in page)
            if len(page) < self.trades_page_limit:
                return fills
            params = {**params, "fromId": max(int(t["id"]) for t in page) + 1}

    def balances(self) -> dict:
        account = self._call("GET", "/api/v3/account", {"omitZeroBalances": "true"}, signed=True)
        return {b["asset"]: D(b["free"]) + D(b["locked"]) for b in account.get("balances", [])}

    def klines(self, symbol: str, limit: int = 30) -> list[list]:
        return self._call("GET", "/api/v3/klines", {"symbol": symbol, "interval": "1h", "limit": int(limit)})


class BinanceKlineFeed:
    """Live hourly feed from the adapter's environment (closed bars + current open).

    Record ``t`` holds the open of the bar starting at ``t`` (observable at
    ``t``) and the close of bar ``t - 1h`` (closed), like the lab feed.
    """

    kind = "binance_klines"

    def __init__(self, adapter: BinanceSpotAdapter, symbol: str = "BTCUSDT", limit: int = 30) -> None:
        self.adapter, self.symbol, self.limit = adapter, symbol, limit
        self._now = None

    def records(self):
        import pandas as pd

        from .lab_feed import FeedRecord, FeedView

        rows = self.adapter.klines(self.symbol, self.limit)
        self._now = pd.Timestamp(self.adapter.server_time(), unit="ms", tz="UTC")
        closes = {pd.Timestamp(r[0], unit="ms", tz="UTC"): float(r[4]) for r in rows}
        view = FeedView()
        for row in rows:
            ts = pd.Timestamp(row[0], unit="ms", tz="UTC")
            view.records[ts] = FeedRecord(ts, float(row[1]), closes.get(ts - pd.Timedelta(hours=1)))
        return view

    def now(self):
        import pandas as pd

        return self._now if self._now is not None else pd.Timestamp(self.adapter.server_time(), unit="ms", tz="UTC")

    def identity(self) -> dict:
        return {"kind": self.kind, "symbol": self.symbol, **self.adapter.identity()}


__all__ = ["BASE_URLS", "BinanceKlineFeed", "BinanceSpotAdapter", "KEY_ENV", "MAINNET_ENV", "TransportUnknown",
           "requests_transport", "sign"]
