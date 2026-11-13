"""S07 execution contract between the live runner and an exchange adapter.

The runner never talks to an exchange directly: it uses an
:class:`ExecutionAdapter`.  Every order carries a **client order id chosen
and persisted locally before it is sent**, so an order whose response was lost
can be found again by that id.

Error semantics (the core of reconciliation)
--------------------------------------------
* :class:`ExchangeRejected` — the exchange answered and the order was **not**
  created (filters, insufficient balance, duplicate id...).  Terminal.
* :class:`ExchangeUnknown`  — timeout, connection reset after sending, HTTP
  5xx or Binance ``-1007``: the order **may or may not exist**.  The runner
  must query it by client id before sending anything else.
* :class:`OrderNotFound`    — a query by client id found nothing (``-2013``).

Amounts are :class:`~decimal.Decimal` end to end; they are stored as strings.

:class:`FakeExchange` is the deterministic test exchange used as response
fixture for the collaudo: it follows Binance Spot semantics (MARKET/LIMIT,
partial fills, ``EXPIRED`` market remainder, commissions in the received
asset, duplicate-id rejection among open orders) and can inject faults:
rejection, timeout before/after the order reaches the book, partial fills,
resting orders and balance changes made outside the runner.  Its state can be
file-backed so that it survives a killed runner process.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import ROUND_DOWN, Decimal
import json
from pathlib import Path
import re
import time
from typing import Any, Protocol

SIDES = ("BUY", "SELL")
ORDER_TYPES = ("MARKET", "LIMIT")
OPEN_STATUSES = ("NEW", "PENDING_NEW", "PARTIALLY_FILLED", "PENDING_CANCEL")
TERMINAL_STATUSES = ("FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH")
# Conservative client id: the Binance pages do not document a regex; letters,
# digits and '-' within 32 characters are accepted by every Binance venue.
CLIENT_ID_RE = re.compile(r"^[a-z0-9-]{1,32}$")


def D(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(repr(value))
    return Decimal(str(value))


def floor_to(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return value
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


class ExchangeError(RuntimeError):
    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class ExchangeRejected(ExchangeError):
    """The exchange answered: the order was not created / not cancelled."""


class ExchangeUnknown(ExchangeError):
    """Outcome unknown: the request may have been executed."""


class OrderNotFound(ExchangeError):
    """No order with this client id."""


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    base_asset: str
    quote_asset: str
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    tick_size: Decimal
    min_notional: Decimal
    quote_precision: int = 8

    def quantity(self, value: Decimal) -> Decimal:
        return floor_to(D(value), self.step_size)

    def price(self, value: Decimal) -> Decimal:
        return floor_to(D(value), self.tick_size)

    def quote(self, value: Decimal) -> Decimal:
        return floor_to(D(value), Decimal(1).scaleb(-self.quote_precision))

    def record(self) -> dict:
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(self).items()}


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str
    symbol: str
    side: str
    order_type: str = "MARKET"
    quantity: Decimal | None = None
    quote_quantity: Decimal | None = None
    price: Decimal | None = None

    def __post_init__(self) -> None:
        if not CLIENT_ID_RE.match(self.client_order_id):
            raise ValueError(f"invalid client order id: {self.client_order_id}")
        if self.side not in SIDES or self.order_type not in ORDER_TYPES:
            raise ValueError("invalid side or order type")
        if self.order_type == "MARKET" and (self.quantity is None) == (self.quote_quantity is None):
            raise ValueError("MARKET order needs exactly one of quantity or quote_quantity")
        if self.order_type == "LIMIT" and (self.quantity is None or self.price is None):
            raise ValueError("LIMIT order needs quantity and price")

    def record(self) -> dict:
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(self).items()}


@dataclass(frozen=True)
class Fill:
    trade_id: str
    price: Decimal
    quantity: Decimal
    commission: Decimal
    commission_asset: str

    def record(self) -> dict:
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(self).items()}


@dataclass(frozen=True)
class OrderReport:
    client_order_id: str
    exchange_order_id: str
    symbol: str
    side: str
    order_type: str
    status: str
    orig_quantity: Decimal
    executed_quantity: Decimal
    quote_executed: Decimal
    fills: tuple[Fill, ...] = ()
    price: Decimal = Decimal(0)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    def record(self) -> dict:
        payload = {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(self).items() if k != "fills"}
        payload["fills"] = [fill.record() for fill in self.fills]
        return payload


class ExecutionAdapter(Protocol):
    """What the live runner needs from an exchange."""

    def identity(self) -> dict: ...
    def rules(self, symbol: str) -> SymbolRules: ...
    def submit(self, request: OrderRequest) -> OrderReport: ...
    def query(self, symbol: str, client_order_id: str) -> OrderReport: ...
    def cancel(self, symbol: str, client_order_id: str) -> OrderReport: ...
    def open_orders(self, symbol: str) -> list[OrderReport]: ...
    def trades(self, symbol: str, exchange_order_id: str) -> list[Fill]: ...
    def balances(self) -> dict[str, Decimal]: ...


# ====================================================================== fake exchange
DEFAULT_RULES = SymbolRules("BTCUSDT", "BTC", "USDT", D("0.00001"), D("0.00001"), D("9000"), D("0.01"),
                            D("5"), quote_precision=8)


@dataclass
class _FakeState:
    balances: dict[str, str] = field(default_factory=lambda: {"BTC": "0", "USDT": "10000"})
    price: str = "40000"
    orders: dict[str, dict] = field(default_factory=dict)
    next_order: int = 1
    next_trade: int = 1
    script: list[dict] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    adverse_bps: str = "0"  # MARKET fills move against the order by this many basis points


class FakeExchange:
    """Deterministic Binance-Spot-like exchange for fixtures and collaudo.

    ``script`` is a queue of behaviours consumed by successive ``submit``
    calls (``{}`` = normal), e.g. ``{"reject": -2010, "msg": "..."}``,
    ``{"timeout": "before"}`` (order never created), ``{"timeout": "after"}``
    (order created and executed, response lost), ``{"partial": "0.4"}``
    (MARKET: 40 % filled then ``EXPIRED``; LIMIT: 40 % filled, rest open),
    ``{"rest": true}`` (LIMIT stays ``NEW``).  Queries can also be scripted
    with ``{"query_timeout": n}`` via :meth:`fail_next`.
    """

    name = "fake"
    environment = "fixture"

    def __init__(self, path: str | Path | None = None, *, rules: SymbolRules = DEFAULT_RULES,
                 fee_rate: Decimal = D("0.001"), balances: dict | None = None, price: Any = None) -> None:
        self.path = Path(path) if path else None
        self._rules = rules
        self.fee_rate = D(fee_rate)
        self._fail: list[str] = []
        if self.path and self.path.is_file():
            self.state = _FakeState(**json.loads(self.path.read_text(encoding="utf-8")))
        else:
            self.state = _FakeState()
            if balances is not None:
                self.state.balances = {k: str(D(v)) for k, v in balances.items()}
            if price is not None:
                self.state.price = str(D(price))
            self._save()

    # ------------------------------------------------------------ fixture controls
    def _save(self) -> None:
        if self.path:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self.state), sort_keys=True, indent=1), encoding="utf-8")
            for attempt in range(50):  # Windows: a scanner may hold the target for a moment
                try:
                    tmp.replace(self.path)
                    break
                except PermissionError:
                    if attempt == 49:
                        raise
                    time.sleep(0.02)

    def _reload(self) -> None:
        if self.path and self.path.is_file():
            self.state = _FakeState(**json.loads(self.path.read_text(encoding="utf-8")))

    def set_price(self, price: Any) -> None:
        self._reload()
        self.state.price = str(D(price))
        self._save()

    def set_adverse_bps(self, bps: Any) -> None:
        self._reload()
        self.state.adverse_bps = str(D(bps))
        self._save()

    def push(self, *behaviours: dict) -> None:
        self._reload()
        self.state.script.extend(dict(b) for b in behaviours)
        self._save()

    def fail_next(self, operation: str) -> None:
        """Next ``operation`` ('query', 'cancel', 'open_orders', 'balances') raises ExchangeUnknown."""
        self._fail.append(operation)

    def external_transfer(self, asset: str, delta: Any) -> None:
        """Balance change made outside the runner (deposit, manual trade...)."""
        self._reload()
        self.state.balances[asset] = str(D(self.state.balances.get(asset, "0")) + D(delta))
        self._save()

    def fill_resting(self, client_order_id: str, fraction: Any = 1) -> OrderReport:
        """Market moves through a resting LIMIT order: fill ``fraction`` of the remainder."""
        self._reload()
        order = self.state.orders[client_order_id]
        remaining = D(order["orig_quantity"]) - D(order["executed_quantity"])
        self._execute(order, self._rules.quantity(remaining * D(fraction)), D(order["price"]))
        if D(order["executed_quantity"]) >= D(order["orig_quantity"]):
            order["status"] = "FILLED"
        self._save()
        return self._report(order)

    # ------------------------------------------------------------ internals
    def _check(self, operation: str) -> None:
        self.state.calls.append(operation)
        if operation in self._fail:
            self._fail.remove(operation)
            raise ExchangeUnknown(f"{operation}: timeout", code=-1007)

    def _bal(self, asset: str) -> Decimal:
        return D(self.state.balances.get(asset, "0"))

    def _add(self, asset: str, delta: Decimal) -> None:
        self.state.balances[asset] = str(self._bal(asset) + delta)

    def _execute(self, order: dict, quantity: Decimal, price: Decimal) -> None:
        if quantity <= 0:
            return
        rules = self._rules
        quote = quantity * price
        if order["side"] == "BUY":
            commission, asset = quantity * self.fee_rate, rules.base_asset
            self._add(rules.quote_asset, -quote)
            self._add(rules.base_asset, quantity - commission)
        else:
            commission, asset = quote * self.fee_rate, rules.quote_asset
            self._add(rules.base_asset, -quantity)
            self._add(rules.quote_asset, quote - commission)
        order["fills"].append(dict(trade_id=str(self.state.next_trade), price=str(price), quantity=str(quantity),
                                   commission=str(commission), commission_asset=asset))
        self.state.next_trade += 1
        order["executed_quantity"] = str(D(order["executed_quantity"]) + quantity)
        order["quote_executed"] = str(D(order["quote_executed"]) + quote)
        order["status"] = "PARTIALLY_FILLED"

    def _report(self, order: dict) -> OrderReport:
        return OrderReport(order["client_order_id"], order["exchange_order_id"], order["symbol"], order["side"],
                           order["type"], order["status"], D(order["orig_quantity"]), D(order["executed_quantity"]),
                           D(order["quote_executed"]),
                           tuple(Fill(f["trade_id"], D(f["price"]), D(f["quantity"]), D(f["commission"]),
                                      f["commission_asset"]) for f in order["fills"]),
                           D(order.get("price") or 0))

    # ------------------------------------------------------------ adapter API
    def identity(self) -> dict:
        return {"exchange": self.name, "environment": self.environment, "symbol_rules": self._rules.record()}

    def rules(self, symbol: str) -> SymbolRules:
        self._check("rules")
        if symbol != self._rules.symbol:
            raise ExchangeRejected(f"invalid symbol {symbol}", code=-1121)
        return self._rules

    def submit(self, request: OrderRequest) -> OrderReport:
        self._reload()
        self.state.calls.append("submit")
        behaviour = self.state.script.pop(0) if self.state.script else {}
        self._save()
        if behaviour.get("timeout") == "before":
            raise ExchangeUnknown("timeout before the order reached the exchange", code=-1007)
        if "reject" in behaviour:
            raise ExchangeRejected(behaviour.get("msg", "Order rejected"), code=int(behaviour["reject"]))
        if request.client_order_id in self.state.orders:
            existing = self.state.orders[request.client_order_id]
            if existing["status"] in OPEN_STATUSES:
                raise ExchangeRejected("Duplicate order sent.", code=-2010)
        rules = self._rules
        price = D(self.state.price)
        if request.order_type == "MARKET":
            quantity = request.quantity if request.quantity is not None else rules.quantity(request.quote_quantity / price)
        else:
            quantity = request.quantity
        if quantity != rules.quantity(quantity) or quantity < rules.min_qty or quantity > rules.max_qty:
            raise ExchangeRejected("Filter failure: LOT_SIZE", code=-1013)
        if quantity * (request.price or price) < rules.min_notional:
            raise ExchangeRejected("Filter failure: NOTIONAL", code=-1013)
        if request.side == "BUY" and quantity * (request.price or price) > self._bal(rules.quote_asset):
            raise ExchangeRejected("Account has insufficient balance for requested action.", code=-2010)
        if request.side == "SELL" and quantity > self._bal(rules.base_asset):
            raise ExchangeRejected("Account has insufficient balance for requested action.", code=-2010)
        order = dict(client_order_id=request.client_order_id, exchange_order_id=str(self.state.next_order),
                     symbol=request.symbol, side=request.side, type=request.order_type, status="NEW",
                     orig_quantity=str(quantity), executed_quantity="0", quote_executed="0",
                     price=str(request.price) if request.price is not None else "0", fills=[])
        self.state.next_order += 1
        self.state.orders[request.client_order_id] = order
        partial = D(behaviour["partial"]) if "partial" in behaviour else None
        if request.order_type == "MARKET":
            adverse = D(self.state.adverse_bps) / D(10000)
            fill_price = price if not adverse else (rules.price(price * (1 + adverse)) if request.side == "BUY"
                                                    else rules.price(price * (1 - adverse)))
            self._execute(order, rules.quantity(quantity * partial) if partial is not None else quantity, fill_price)
            order["status"] = "FILLED" if D(order["executed_quantity"]) >= quantity else "EXPIRED"
        elif not behaviour.get("rest"):
            crosses = price <= request.price if request.side == "BUY" else price >= request.price
            if crosses or partial is not None:
                fill = rules.quantity(quantity * partial) if partial is not None else quantity
                self._execute(order, fill, request.price)
                if D(order["executed_quantity"]) >= quantity:
                    order["status"] = "FILLED"
        self._save()
        if behaviour.get("timeout") == "after":
            raise ExchangeUnknown("timeout after sending: execution status unknown", code=-1007)
        return self._report(order)

    def query(self, symbol: str, client_order_id: str) -> OrderReport:
        self._reload()
        self._check("query")
        order = self.state.orders.get(client_order_id)
        if order is None:
            raise OrderNotFound("Order does not exist.", code=-2013)
        return self._report(order)

    def cancel(self, symbol: str, client_order_id: str) -> OrderReport:
        self._reload()
        self._check("cancel")
        order = self.state.orders.get(client_order_id)
        if order is None:
            raise OrderNotFound("Unknown order sent.", code=-2011)
        if order["status"] not in OPEN_STATUSES:
            raise ExchangeRejected("Unknown order sent.", code=-2011)
        order["status"] = "CANCELED"
        self._save()
        return self._report(order)

    def open_orders(self, symbol: str) -> list[OrderReport]:
        self._reload()
        self._check("open_orders")
        return [self._report(o) for o in self.state.orders.values() if o["status"] in OPEN_STATUSES]

    def trades(self, symbol: str, exchange_order_id: str) -> list[Fill]:
        self._reload()
        for order in self.state.orders.values():
            if order["exchange_order_id"] == str(exchange_order_id):
                return list(self._report(order).fills)
        return []

    def balances(self) -> dict[str, Decimal]:
        self._reload()
        self._check("balances")
        # Total (free + locked): resting orders do not reserve funds in the fake.
        return {asset: D(value) for asset, value in self.state.balances.items()}


__all__ = ["CLIENT_ID_RE", "D", "DEFAULT_RULES", "ExchangeError", "ExchangeRejected", "ExchangeUnknown",
           "ExecutionAdapter", "FakeExchange", "Fill", "OPEN_STATUSES", "OrderNotFound", "OrderReport",
           "OrderRequest", "SymbolRules", "TERMINAL_STATUSES", "floor_to"]
