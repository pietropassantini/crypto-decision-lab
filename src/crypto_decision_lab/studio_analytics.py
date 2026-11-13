"""S04: metrics derived only from the S03 canonical ledger.

Every number shown by Strategy Studio is recomputed here from ``ledger.json``
so that P&L, mark-to-market drawdown, costs, exposure and trade counts can be
reconciled with the registry (CP04.2):

* **equity** is the ``balance`` record of each event: cash + quantity * open
  of that event (mark-to-market at the only price observable at that time);
* **net P&L** is final equity minus initial cash; it equals the sum of the
  closed-trade P&L plus the mark-to-market of a position still open;
* **max drawdown (MTM)** is ``min(equity / running_peak - 1)`` on that curve,
  the same definition used by ``charts.make_drawdown_figure``;
* **costs** are fees plus slippage (``quantity * |exec - open|``) of each fill;
* **exposure** is the share of events with an open position and the average
  ``quantity * open / equity``;
* a **trade** is a BUY fill and its following SELL fill (or the open position
  valued at the last event when the run ends without a SELL).
"""
from __future__ import annotations

from typing import Any, Iterable

import numpy as np
import pandas as pd

TOLERANCE = 1e-6
WINDOW_FREQUENCIES = {"M": "Mese", "Q": "Trimestre", "W": "Settimana", "Y": "Anno"}
_PERIOD_ALIAS = {"M": "M", "Q": "Q", "W": "W", "Y": "Y"}


def _records(ledger: Iterable[dict], kind: str) -> list[dict]:
    return [record for record in ledger if record.get("kind") == kind]


def _ts(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    return timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")


def equity_frame(ledger: list[dict]) -> pd.DataFrame:
    """One row per processed event: cash, quantity, open, equity, exposure."""

    opens = {record["timestamp"]: float(record["open"]) for record in _records(ledger, "market")}
    rows = []
    for record in _records(ledger, "balance"):
        price = opens.get(record["timestamp"], np.nan)
        equity = float(record["equity"])
        quantity = float(record["quantity"])
        rows.append(
            {
                "timestamp": _ts(record["timestamp"]),
                "cash": float(record["cash"]),
                "quantity": quantity,
                "open": price,
                "equity": equity,
                "exposure": (quantity * price / equity) if equity > 0 else 0.0,
            }
        )
    frame = pd.DataFrame(rows, columns=["timestamp", "cash", "quantity", "open", "equity", "exposure"])
    return frame.set_index("timestamp") if not frame.empty else frame


def equity_curve(ledger: list[dict], initial_cash: float) -> pd.Series:
    frame = equity_frame(ledger)
    if frame.empty:
        return pd.Series(dtype=float)
    return frame["equity"].astype(float)


def max_drawdown_pct(equity: pd.Series, start_equity: float | None = None) -> float:
    values = equity.to_numpy(dtype=float)
    if start_equity is not None:
        values = np.concatenate([[float(start_equity)], values])
    if values.size == 0:
        return 0.0
    peak = np.maximum.accumulate(values)
    return float(100.0 * np.min(values / peak - 1.0))


def _order_context(ledger: list[dict]) -> dict[str, dict]:
    """Order id -> budget and the decision reason that produced the order."""

    context: dict[str, dict] = {}
    last_decision: dict | None = None
    for record in ledger:
        kind = record.get("kind")
        if kind == "decision":
            last_decision = record
        elif kind == "order":
            context[record["order_id"]] = {
                "budget": float(record["budget"]),
                "decided_at": record["timestamp"],
                "reason": (last_decision or {}).get("reason", ""),
                "exposure": (last_decision or {}).get("exposure", ""),
            }
    return context


def trades(ledger: list[dict]) -> pd.DataFrame:
    """Round trips paired from the fills, with reasons and costs."""

    orders = _order_context(ledger)
    end = next(iter(_records(ledger, "end")), {})
    last_open = None
    market = _records(ledger, "market")
    if market:
        last_open = float(market[-1]["open"])
    rows: list[dict] = []
    current: dict | None = None
    for fill in _records(ledger, "execution"):
        context = orders.get(fill["order_id"], {})
        if fill["side"] == "BUY":
            spent = context.get("budget", fill["quantity"] * fill["price"] + fill["fee"])
            current = {
                "trade": len(rows) + 1,
                "decision_at": _ts(context.get("decided_at", fill["timestamp"])),
                "entry_at": _ts(fill["timestamp"]),
                "entry_price": float(fill["price"]),
                "quantity": float(fill["quantity"]),
                "cash_out": float(spent),
                "entry_fee": float(fill["fee"]),
                "entry_slippage": float(fill["slippage_cost"]),
                "entry_reason": context.get("reason", ""),
            }
        elif current is not None:
            proceeds = float(fill["quantity"]) * float(fill["price"]) - float(fill["fee"])
            current.update(
                exit_at=_ts(fill["timestamp"]),
                exit_price=float(fill["price"]),
                cash_in=proceeds,
                exit_fee=float(fill["fee"]),
                exit_slippage=float(fill["slippage_cost"]),
                exit_reason=context.get("reason", ""),
                status="CLOSED",
            )
            rows.append(current)
            current = None
    if current is not None:
        valuation = float(current["quantity"]) * float(last_open or 0.0)
        current.update(
            exit_at=pd.NaT,
            exit_price=np.nan,
            cash_in=valuation,
            exit_fee=0.0,
            exit_slippage=0.0,
            exit_reason=f"aperta a fine run ({end.get('status', 'n/d')}), valorizzata all'ultimo open",
            status="OPEN",
        )
        rows.append(current)
    columns = [
        "trade", "status", "decision_at", "entry_at", "exit_at", "quantity", "entry_price", "exit_price",
        "cash_out", "cash_in", "pnl", "return_pct", "fees", "slippage", "costs",
        "entry_fee", "exit_fee", "entry_slippage", "exit_slippage", "entry_reason", "exit_reason",
    ]
    if not rows:
        return pd.DataFrame(columns=columns)
    frame = pd.DataFrame(rows)
    for column in ("decision_at", "entry_at", "exit_at"):
        frame[column] = pd.to_datetime(frame[column], utc=True)
    frame["pnl"] =frame["cash_in"] - frame["cash_out"]
    frame["return_pct"] = 100.0 * frame["pnl"] / frame["cash_out"]
    frame["fees"] = frame["entry_fee"] + frame["exit_fee"]
    frame["slippage"] = frame["entry_slippage"] + frame["exit_slippage"]
    frame["costs"] = frame["fees"] + frame["slippage"]
    return frame[columns]


def decisions(ledger: list[dict]) -> pd.DataFrame:
    """Decision log joined with the risk verdict of the same event."""

    rows = []
    pending: dict | None = None
    for record in ledger:
        if record.get("kind") == "decision":
            pending = record
        elif record.get("kind") == "risk" and pending is not None:
            rows.append(
                {
                    "timestamp": _ts(pending["timestamp"]),
                    "exposure": pending["exposure"],
                    "reason": pending["reason"],
                    "risk_accepted": bool(record["accepted"]),
                    "risk_reason": record["reason"],
                    "budget": float(record["budget"]),
                }
            )
            pending = None
    return pd.DataFrame(rows, columns=["timestamp", "exposure", "reason", "risk_accepted", "risk_reason", "budget"])


def summarize(ledger: list[dict], initial_cash: float) -> dict[str, Any]:
    frame = equity_frame(ledger)
    trade_frame = trades(ledger)
    fills = _records(ledger, "execution")
    end = next(iter(_records(ledger, "end")), {})
    final_equity = float(end.get("equity", initial_cash)) if end else float(initial_cash)
    fees = float(sum(float(fill["fee"]) for fill in fills))
    slippage = float(sum(float(fill["slippage_cost"]) for fill in fills))
    closed = trade_frame[trade_frame["status"] == "CLOSED"] if not trade_frame.empty else trade_frame
    wins = int((closed["pnl"] > 0).sum()) if not closed.empty else 0
    return {
        "status": end.get("status", "UNKNOWN"),
        "events": int(len(frame)),
        "first_event": str(frame.index[0]) if not frame.empty else None,
        "last_event": str(frame.index[-1]) if not frame.empty else None,
        "initial_cash": float(initial_cash),
        "final_equity": final_equity,
        "net_pnl": final_equity - float(initial_cash),
        "net_return_pct": 100.0 * (final_equity / float(initial_cash) - 1.0),
        "max_drawdown_mtm_pct": max_drawdown_pct(frame["equity"], initial_cash) if not frame.empty else 0.0,
        "fees": fees,
        "slippage": slippage,
        "costs": fees + slippage,
        "time_in_market_pct": 100.0 * float((frame["quantity"] > 0).mean()) if not frame.empty else 0.0,
        "avg_exposure_pct": 100.0 * float(frame["exposure"].mean()) if not frame.empty else 0.0,
        "trades": int(len(trade_frame)),
        "closed_trades": int(len(closed)),
        "open_trades": int(len(trade_frame) - len(closed)),
        "win_rate_pct": (100.0 * wins / len(closed)) if len(closed) else None,
        "position_open": bool(end.get("position_open", False)),
        "orders": len(_records(ledger, "order")),
        "orders_cancelled": len(_records(ledger, "order_cancelled")),
        "fills": len(fills),
    }


def window_summary(ledger: list[dict], initial_cash: float, freq: str = "M") -> pd.DataFrame:
    """Results per calendar window; window P&L chains end equity to end equity."""

    if freq not in _PERIOD_ALIAS:
        raise ValueError(f"unsupported window frequency: {freq}")
    frame = equity_frame(ledger)
    columns = [
        "window", "start", "end", "events", "start_equity", "end_equity", "pnl", "return_pct",
        "max_drawdown_mtm_pct", "trades_opened", "trades_closed", "fees", "slippage", "costs",
        "time_in_market_pct", "avg_exposure_pct",
    ]
    if frame.empty:
        return pd.DataFrame(columns=columns)
    trade_frame = trades(ledger)
    fills = pd.DataFrame(_records(ledger, "execution"))
    if not fills.empty:
        fills["timestamp"] = fills["timestamp"].map(_ts)
    naive = frame.index.tz_convert("UTC").tz_localize(None)
    periods = naive.to_period(_PERIOD_ALIAS[freq])
    rows = []
    start_equity = float(initial_cash)
    for period in periods.unique():
        mask = periods == period
        chunk = frame[mask]
        lo, hi = chunk.index[0], chunk.index[-1]
        end_equity = float(chunk["equity"].iloc[-1])
        window_fills = fills[(fills["timestamp"] >= lo) & (fills["timestamp"] <= hi)] if not fills.empty else fills
        fees = float(window_fills["fee"].sum()) if not window_fills.empty else 0.0
        slippage = float(window_fills["slippage_cost"].sum()) if not window_fills.empty else 0.0
        opened = int(((trade_frame["entry_at"] >= lo) & (trade_frame["entry_at"] <= hi)).sum()) if not trade_frame.empty else 0
        closed = 0
        if not trade_frame.empty:
            exits = trade_frame["exit_at"]
            closed = int(((exits >= lo) & (exits <= hi)).fillna(False).sum())
        rows.append(
            {
                "window": str(period),
                "start": lo,
                "end": hi,
                "events": int(len(chunk)),
                "start_equity": start_equity,
                "end_equity": end_equity,
                "pnl": end_equity - start_equity,
                "return_pct": 100.0 * (end_equity / start_equity - 1.0),
                "max_drawdown_mtm_pct": max_drawdown_pct(chunk["equity"], start_equity),
                "trades_opened": opened,
                "trades_closed": closed,
                "fees": fees,
                "slippage": slippage,
                "costs": fees + slippage,
                "time_in_market_pct": 100.0 * float((chunk["quantity"] > 0).mean()),
                "avg_exposure_pct": 100.0 * float(chunk["exposure"].mean()),
            }
        )
        start_equity = end_equity
    return pd.DataFrame(rows, columns=columns)


def reconcile(ledger: list[dict], initial_cash: float, freq: str = "M") -> list[dict[str, Any]]:
    """CP04.2 cross-checks between aggregated metrics and the raw ledger."""

    summary = summarize(ledger, initial_cash)
    trade_frame = trades(ledger)
    windows = window_summary(ledger, initial_cash, freq)
    frame = equity_frame(ledger)
    end = next(iter(_records(ledger, "end")), {})
    fills = _records(ledger, "execution")
    orders = {record["order_id"]: record for record in _records(ledger, "order")}
    cash_flow = float(initial_cash)
    for fill in fills:
        if fill["side"] == "BUY":
            cash_flow -= float(orders[fill["order_id"]]["budget"])
        else:
            cash_flow += float(fill["quantity"]) * float(fill["price"]) - float(fill["fee"])
    trade_pnl = float(trade_frame["pnl"].sum()) if not trade_frame.empty else 0.0
    checks = [
        ("P&L netto = somma P&L trade (chiusi + aperto MTM)", summary["net_pnl"], trade_pnl),
        ("Cassa finale = cassa iniziale ± flussi delle esecuzioni", float(end.get("cash", initial_cash)), cash_flow),
        ("P&L netto = somma P&L per finestra", summary["net_pnl"], float(windows["pnl"].sum()) if not windows.empty else 0.0),
        ("Equity finale = ultimo saldo del registro", summary["final_equity"], float(frame["equity"].iloc[-1]) if not frame.empty else float(initial_cash)),
        ("Costi = somma costi per trade", summary["costs"], float(trade_frame["costs"].sum()) if not trade_frame.empty else 0.0),
        ("Costi = somma costi per finestra", summary["costs"], float(windows["costs"].sum()) if not windows.empty else 0.0),
        ("Trade = esecuzioni BUY", float(summary["trades"]), float(sum(1 for fill in fills if fill["side"] == "BUY"))),
        ("Trade aperti per finestra = trade totali", float(summary["trades"]), float(windows["trades_opened"].sum()) if not windows.empty else 0.0),
        (
            "Drawdown aggregato ≤ drawdown di ogni finestra (picco globale)",
            summary["max_drawdown_mtm_pct"],
            min(summary["max_drawdown_mtm_pct"], float(windows["max_drawdown_mtm_pct"].min()) if not windows.empty else 0.0),
        ),
    ]
    return [
        {
            "check": name,
            "expected": float(expected),
            "observed": float(observed),
            "passed": bool(abs(float(expected) - float(observed)) <= TOLERANCE * max(1.0, abs(float(expected)))),
        }
        for name, expected, observed in checks
    ]


__all__ = [
    "WINDOW_FREQUENCIES",
    "decisions",
    "equity_curve",
    "equity_frame",
    "max_drawdown_pct",
    "reconcile",
    "summarize",
    "trades",
    "window_summary",
]
