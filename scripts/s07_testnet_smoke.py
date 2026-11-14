"""S07 collaudo on the Binance Spot testnet (virtual funds).

Requires CDL_BINANCE_TESTNET_API_KEY / CDL_BINANCE_TESTNET_API_SECRET in the
environment (keys from https://testnet.binance.vision, never committed).
Writes an evidence file under var/s07-testnet/ without credentials.

Sequence (CP07.1 / CP07.2 on the real API):
  1. exchangeInfo filters and account balances;
  2. MARKET BUY by quoteOrderQty -> FILLED, fills reconciled with executedQty;
  3. MARKET SELL whose response is dropped on purpose (simulated timeout
     after send) -> query by client id finds the order: no resend;
  4. LIMIT BUY far below the market -> NEW -> cancel -> CANCELED;
  5. a rejected order (quantity below LOT_SIZE) -> ExchangeRejected;
  6. query of a never-sent client id -> OrderNotFound;
  7. open orders empty and balances consistent with the fills.

Usage: python scripts/s07_testnet_smoke.py [--quote 20]
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from crypto_decision_lab.binance_spot import BinanceSpotAdapter, TransportUnknown, requests_transport  # noqa: E402
from crypto_decision_lab.exchange import D, ExchangeRejected, OrderNotFound, OrderRequest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--quote", type=float, default=20.0)
    args = parser.parse_args()
    drop = {"next": False}

    def transport(method, url, params, headers, timeout):
        result = requests_transport(method, url, params, headers, timeout)
        if drop["next"] and method == "POST":
            drop["next"] = False
            raise TransportUnknown("response dropped on purpose after send (collaudo)")
        return result

    exchange = BinanceSpotAdapter("testnet", transport=transport)
    run = uuid.uuid4().hex[:8]
    evidence: dict = {"started_utc": datetime.now(timezone.utc).isoformat(), "identity": exchange.identity(),
                      "steps": []}

    def record(name, ok, **values):
        evidence["steps"].append({"step": name, "ok": bool(ok), **{k: str(v) for k, v in values.items()}})
        print(("PASS " if ok else "FAIL ") + name, values)

    symbol = args.symbol
    rules = exchange.rules(symbol)
    before = exchange.balances()
    record("rules_and_balances", True, rules=rules.record(), base=before.get(rules.base_asset),
           quote=before.get(rules.quote_asset))

    buy = exchange.submit(OrderRequest(f"cdl-smoke-{run}-b", symbol, "BUY", quote_quantity=rules.quote(D(args.quote))))
    filled = sum((f.quantity for f in buy.fills), Decimal(0))
    record("market_buy", buy.status == "FILLED" and filled == buy.executed_quantity, status=buy.status,
           executed=buy.executed_quantity, fills=len(buy.fills))

    received = buy.executed_quantity - sum((f.commission for f in buy.fills if f.commission_asset == rules.base_asset),
                                           Decimal(0))
    sell_id = f"cdl-smoke-{run}-s"
    drop["next"] = True
    try:
        exchange.submit(OrderRequest(sell_id, symbol, "SELL", quantity=rules.quantity(received)))
        record("sell_timeout_simulated", False, detail="response was not dropped")
    except Exception as exc:  # ExchangeUnknown expected
        found = exchange.query(symbol, sell_id)
        record("sell_timeout_then_query", type(exc).__name__ == "ExchangeUnknown" and found.status == "FILLED",
               error=type(exc).__name__, status=found.status, executed=found.executed_quantity,
               trade_fills=len(found.fills))

    last_price = buy.fills[-1].price if buy.fills else D(1)
    limit_id = f"cdl-smoke-{run}-l"
    quantity = rules.quantity(max(rules.min_notional * 2, D(args.quote)) / (last_price * D("0.8")))
    resting = exchange.submit(OrderRequest(limit_id, symbol, "BUY", "LIMIT", quantity=quantity,
                                           price=rules.price(last_price * D("0.8"))))
    cancelled = exchange.cancel(symbol, limit_id)
    record("limit_new_then_cancel", resting.status == "NEW" and cancelled.status == "CANCELED",
           first=resting.status, after_cancel=cancelled.status)

    try:
        exchange.submit(OrderRequest(f"cdl-smoke-{run}-r", symbol, "BUY", quantity=rules.step_size / 10))
        record("reject_below_lot_size", False)
    except ExchangeRejected as exc:
        record("reject_below_lot_size", True, code=exc.code)

    try:
        exchange.query(symbol, f"cdl-smoke-{run}-never")
        record("query_unknown_id", False)
    except OrderNotFound as exc:
        record("query_unknown_id", True, code=exc.code)

    mine = [o.client_order_id for o in exchange.open_orders(symbol) if o.client_order_id.startswith(f"cdl-smoke-{run}")]
    after = exchange.balances()
    record("no_open_orders", not mine, open=mine)
    record("balances_after", True, base=after.get(rules.base_asset), quote=after.get(rules.quote_asset))
    out = Path("var/s07-testnet")
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"smoke-{run}.json"
    path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(f"evidence: {path}")
    return 0 if all(step["ok"] for step in evidence["steps"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
