"""S07 live runner: real orders through an :class:`~.exchange.ExecutionAdapter`.

A live session lives in ``<root>/sessions/<session_id>/live.sqlite`` (default
root ``var/live``) and reuses the S03/S06 decision path: the same feed view
(open of the hour + previous close), the same ``momentum_24h`` feature, the
same :class:`~.simulation.Strategy` and :class:`~.simulation.RiskEngine`.
Only execution differs: orders go to the exchange and the portfolio is the
sum of **fills reported by the exchange**, never of simulated prices.

Order lifecycle (write-ahead)
-----------------------------
1. The decision, the order intent and its **client order id** are committed in
   one transaction (status ``INTENT``) *before* anything is sent.
2. ``submit``: the report is applied (``NEW``/``PARTIALLY_FILLED``/``FILLED``/
   ``EXPIRED``); a rejection is terminal (``REJECTED``); a timeout or 5xx makes
   the order ``UNKNOWN``.
3. Every ``poll`` starts with :meth:`LiveSession.reconcile`: each ``INTENT``,
   ``UNKNOWN`` or open order is queried by client id; fills are imported once
   (primary key = exchange trade id).  An order that the exchange does not know
   after ``unknown_grace_seconds`` becomes ``NOT_PLACED``.  Open orders older
   than ``order_timeout_seconds`` are cancelled.  **While an order is
   unresolved no bar is processed and no order is sent.**
4. Balances (free + locked) are compared with the baseline captured at
   creation plus the fills: a mismatch halts every new order (``HALTED``)
   until the operator re-baselines.

Guards on new openings (CP07.3): manual suspension, stale signal/feed,
exposure limits (per order and per position), exchange minimums, unresolved
orders and balance mismatch.  Exits remain allowed except when the session is
``HALTED`` or an order is unresolved.

Statuses: ``CREATED``, ``RUNNING``, ``WAITING``, ``STALE``, ``UNRESOLVED``,
``HALTED``, ``ENGINE_MISMATCH``, ``COMPLETED``; ``openings_suspended`` is a
separate flag shown as ``OPENINGS_SUSPENDED`` when nothing more severe applies.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Callable

import pandas as pd

from .decision_contract import ACTION_LONG
from .exchange import (
    D,
    OPEN_STATUSES,
    ExchangeError,
    ExchangeRejected,
    ExchangeUnknown,
    ExecutionAdapter,
    FakeExchange,
    OrderNotFound,
    OrderReport,
    OrderRequest,
)
from .execution_contract import ACTION_FLAT, HOUR
from .forward_isolation import guard_research_output
from .simulation import Portfolio, Proposal, RiskEngine, engine_digests, select_strategy
from .studio import build_run_config, config_digest, normalize_config

LIVE_SCHEMA = "cdl.s07.live.v1"
DEFAULT_ROOT = Path("var/live")
LIVE_MODULES = ("exchange.py", "binance_spot.py", "live.py")
UNRESOLVED = ("INTENT", "UNKNOWN")
# MISSING: an order the exchange acknowledged (open) but no longer knows; it blocks new
# orders and, after the grace period, halts the session until the operator resolves it.
# FILLS_PENDING: terminal on the exchange, but the trades received so far do not add up to
# executedQty; it is queried again until they do (the position is not known meanwhile).
# FILLS_EXCESS: terminal on the exchange with fewer executedQty than the trades imported
# (inconsistent exchange data): it halts the session at once until the operator resolves it.
LOCAL_OPEN = UNRESOLVED + OPEN_STATUSES + ("MISSING", "FILLS_PENDING", "FILLS_EXCESS")
# Orders that stop bar processing: the portfolio is not known until they are resolved.
WAIT_STATUSES = UNRESOLVED + ("FILLS_PENDING", "FILLS_EXCESS")
# Orders shown as UNRESOLVED and blocking a rebaseline.
OPERATOR_STATUSES = UNRESOLVED + ("MISSING", "FILLS_PENDING", "FILLS_EXCESS")
# Statuses that operator commands and status refreshes must never overwrite.
STICKY = ("COMPLETED", "ENGINE_MISMATCH")
DEFAULT_LIMITS = {
    "max_order_notional": 1000.0,      # quote per order
    "max_position_notional": 2000.0,   # quote, position valued at the bar open
    "max_signal_age_hours": 1.0,       # older bars are processed but cannot open
    "stale_after_hours": 2.0,          # no bar for this long: STALE
    "order_timeout_seconds": 300.0,    # open orders older than this are cancelled
    "unknown_grace_seconds": 60.0,     # unknown + not found for this long: NOT_PLACED
    "balance_abs_tolerance": 1e-8,
    "balance_rel_tolerance": 1e-6,
}
CRASH_HOOK: Callable[[str, str], None] | None = None
_ENV_CRASH = "CDL_LIVE_CRASH"  # "<stage>" -> os._exit(97): before_send | after_send


class LiveError(RuntimeError):
    """Invalid live session operation."""


class SessionBusy(LiveError):
    """Another process (runner or operator command) holds the session lock."""


def _lock_file(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class _SessionLock:
    """Exclusive, re-entrant OS lock on ``session.lock``: one actor at a time per session.

    The runner holds it for a whole poll (decision, intent commit *and* send);
    operator commands hold it for their whole action.  The operating system
    releases it when the holder dies, so a killed runner never leaves it stale.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None
        self.depth = 0

    def acquire(self, wait: float) -> bool:
        """Return True on the outermost acquisition."""
        if self.depth:
            self.depth += 1
            return False
        handle = open(self.path, "a+b")
        deadline = time.monotonic() + max(wait, 0.0)
        while True:
            try:
                _lock_file(handle)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise SessionBusy(f"session busy: another process (runner or operator command) holds "
                                      f"{self.path}; retry when it has finished") from None
                time.sleep(0.05)
        self.handle, self.depth = handle, 1
        return True

    def release(self) -> None:
        self.depth -= 1
        if self.depth == 0:
            handle, self.handle = self.handle, None
            try:
                _unlock_file(handle)
            finally:
                handle.close()


def _exclusive(method):
    """Run a mutating session method under the session lock."""
    import functools

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.exclusive():
            return method(self, *args, **kwargs)

    return wrapper


def _utc(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _iso(value: Any) -> str:
    return str(_utc(value))


def _dump(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _wall() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(timezone.utc))


def _crash(stage: str, client_id: str) -> None:
    if CRASH_HOOK is not None:
        CRASH_HOOK(stage, client_id)
    if os.environ.get(_ENV_CRASH) == stage:
        os._exit(97)


def live_digests() -> tuple[str, dict]:
    """Engine digest (S03) plus the live execution modules."""
    engine, files = engine_digests()
    package = Path(__file__).resolve().parent
    combined = hashlib.sha256(engine.encode())
    files = dict(files)
    for name in LIVE_MODULES:
        files[name] = hashlib.sha256((package / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        combined.update(f"{name}\0{files[name]}\n".encode())
    return combined.hexdigest(), files


SCHEMA_SQL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE state(id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL, payload TEXT NOT NULL,
                   status TEXT NOT NULL, detail TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE orders(client_order_id TEXT PRIMARY KEY, seq INTEGER NOT NULL UNIQUE, bar_ts TEXT NOT NULL,
                    side TEXT NOT NULL, purpose TEXT NOT NULL, request TEXT NOT NULL, status TEXT NOT NULL,
                    exchange_order_id TEXT, executed_quantity TEXT NOT NULL DEFAULT '0',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, report TEXT, detail TEXT NOT NULL DEFAULT '');
CREATE TABLE fills(trade_id TEXT PRIMARY KEY, client_order_id TEXT NOT NULL, side TEXT NOT NULL, price TEXT NOT NULL,
                   quantity TEXT NOT NULL, commission TEXT NOT NULL, commission_asset TEXT NOT NULL,
                   recorded_at TEXT NOT NULL);
CREATE TABLE history(timestamp TEXT PRIMARY KEY, prev_close REAL);
CREATE TABLE ledger(sequence INTEGER PRIMARY KEY, kind TEXT NOT NULL, timestamp TEXT NOT NULL, record TEXT NOT NULL);
CREATE TABLE status_log(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, status TEXT NOT NULL,
                        detail TEXT NOT NULL);
"""


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:24] or "live"


def normalize_live_config(raw: dict) -> dict:
    run = normalize_config(raw.get("run_config") or {})
    limits = {**DEFAULT_LIMITS, **(raw.get("limits") or {})}
    unknown = set(limits) - set(DEFAULT_LIMITS)
    if unknown:
        raise LiveError(f"unknown limits: {sorted(unknown)}")
    limits = {key: float(value) for key, value in limits.items()}
    if any(value < 0 for value in limits.values()):
        raise LiveError("limits must be non-negative")
    order_type = raw.get("order_type", "MARKET")
    if order_type not in ("MARKET", "LIMIT"):
        raise LiveError("order_type must be MARKET or LIMIT")
    return {"run_config": run, "symbol": raw.get("symbol", "BTCUSDT"), "order_type": order_type,
            "limit_offset": float(raw.get("limit_offset", 0.001)), "limits": limits,
            "allow_exits_when_suspended": bool(raw.get("allow_exits_when_suspended", True))}


def build_adapter(spec: dict, *, repo_root: Path) -> ExecutionAdapter:
    """Adapter from the exchange spec pinned in the session (no credentials stored)."""
    name = spec.get("name")
    if name == "fake":
        path = Path(spec["state_path"])
        return FakeExchange(path if path.is_absolute() else repo_root / path)
    if name == "binance-spot":
        from .binance_spot import BinanceSpotAdapter

        return BinanceSpotAdapter(spec.get("environment", "testnet"), allow_mainnet=bool(spec.get("allow_mainnet")))
    raise LiveError(f"unknown exchange: {name}")


def open_feed(source: dict, *, repo_root: Path, adapter=None):
    kind = source.get("kind")
    if kind == "lab_feed":
        from .lab_feed import LabFeed

        path = Path(source["path"])
        return LabFeed(path if path.is_absolute() else repo_root / path)
    if kind == "binance_klines":
        from .binance_spot import BinanceKlineFeed, BinanceSpotAdapter

        feed_adapter = adapter if isinstance(adapter, BinanceSpotAdapter) else BinanceSpotAdapter(
            source.get("environment", "testnet"))
        return BinanceKlineFeed(feed_adapter, source.get("symbol", "BTCUSDT"))
    raise LiveError(f"live sessions accept lab_feed or binance_klines sources, not {kind}")


class LiveSession:
    def __init__(self, directory: Path, *, repo_root: str | Path = ".", adapter: ExecutionAdapter | None = None,
                 feed=None, clock: Callable[[], pd.Timestamp] | None = None) -> None:
        self.directory = Path(directory)
        self.repo_root = Path(repo_root).resolve()
        path = self.directory / "live.sqlite"
        if not path.is_file():
            raise LiveError(f"live session database not found: {path}")
        self.connection = sqlite3.connect(path, isolation_level=None, timeout=30)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.lock = _SessionLock(self.directory / "session.lock")
        self.lock_wait_seconds = 10.0
        self._load_meta()
        self.config = self.meta["config"]
        self.limits = self.config["limits"]
        self.run_config = build_run_config(self.config["run_config"])
        self.adapter = adapter or build_adapter(self.meta["exchange"], repo_root=self.repo_root)
        self.feed = feed or open_feed(self.meta["source"], repo_root=self.repo_root, adapter=self.adapter)
        self.clock = clock or (self.feed.now if self.meta["source"]["kind"] == "lab_feed" else _wall)
        self.rules = self.adapter.rules(self.config["symbol"])
        self.rules_changed = self.rules.record() != self.meta["symbol_rules"]
        self.strategy = select_strategy(self.run_config.strategy)
        self.risk = RiskEngine(self.run_config.risk, self.run_config.hypothesis.fee_rate)
        # Pins are enforced under the lock; if another process holds it now, the checks run
        # on this instance's first locked operation.
        try:
            with self.exclusive(wait=0):
                pass
        except SessionBusy:
            pass

    def _load_meta(self) -> None:
        self.meta = {k: json.loads(v) for k, v in self.connection.execute("SELECT key, value FROM meta")}

    @contextmanager
    def exclusive(self, *, wait: float | None = None):
        """Hold the session lock; on the outermost entry re-read what other processes may have changed."""
        outer = self.lock.acquire(self.lock_wait_seconds if wait is None else wait)
        try:
            if outer:
                self._load_meta()  # adjustments, accepted code revision, activation
                self._enforce_pins()
            yield
        finally:
            self.lock.release()

    def _enforce_pins(self) -> None:
        if self.status()[0] == "COMPLETED":
            return
        if self.rules_changed:  # once per opening, as before: a rebaseline clears it
            self.rules_changed = False
            state = self._state()
            if not state.get("halted"):
                with self._tx():
                    state["halted"] = "exchange symbol rules changed since creation; review before trading"
                    self._save_state(state)
                self._refresh_status()
        current, _ = live_digests()
        if current != self.meta["code_sha256"] and self.status()[0] != "ENGINE_MISMATCH":
            self._set_status("ENGINE_MISMATCH", f"code {current[:12]} differs from pinned {self.meta['code_sha256'][:12]}")

    # ================================================================== creation
    @classmethod
    def create(cls, name: str, raw_config: dict, *, exchange: dict, source: dict, start_at,
               root: str | Path = DEFAULT_ROOT, repo_root: str | Path = ".", adapter: ExecutionAdapter | None = None,
               feed=None, clock=None) -> "LiveSession":
        repo = Path(repo_root).resolve()
        config = normalize_live_config(raw_config)
        if "api_key" in exchange or "api_secret" in exchange:
            raise LiveError("credentials must come from environment variables, not from the session spec")
        activation = None
        if exchange.get("name") == "binance-spot" and exchange.get("environment", "testnet") != "testnet":
            # S08 gate: real capital only through an approved READY release matching this session.
            from .release import ActivationError, verify_activation

            if not exchange.get("approval"):
                raise LiveError("mainnet requires an approved READY release (S08): set exchange.approval")
            approval = Path(exchange["approval"])
            try:
                activation = verify_activation(approval if approval.is_absolute() else repo / approval,
                                               live_config=raw_config, exchange=exchange,
                                               code_sha256=live_digests()[0])
            except ActivationError as exc:
                raise LiveError(f"activation refused: {exc}") from exc
        adapter = adapter or build_adapter(exchange, repo_root=repo)
        feed = feed or open_feed(source, repo_root=repo, adapter=adapter)
        rules = adapter.rules(config["symbol"])
        balances = {asset: str(value) for asset, value in adapter.balances().items()}
        for asset in (rules.base_asset, rules.quote_asset):
            balances.setdefault(asset, "0")
        if D(balances[rules.quote_asset]) < D(config["run_config"]["initial_cash"]):
            raise LiveError("allocated capital exceeds the quote balance on the exchange")
        digest = config_digest(config["run_config"])
        start = _utc(start_at)
        session_id = f"{_slug(name)}-" + hashlib.sha256(
            _dump([config, feed.identity(), exchange, str(start)]).encode()).hexdigest()[:8]
        base = Path(root) if Path(root).is_absolute() else repo / root
        directory = guard_research_output(base / "sessions" / session_id, purpose="live session")
        if directory.exists():
            raise FileExistsError(f"live session already exists: {directory}")
        directory.mkdir(parents=True)
        code_sha, code_files = live_digests()
        meta = {
            "schema_version": LIVE_SCHEMA, "session_id": session_id, "name": name, "config": config,
            "config_sha256": hashlib.sha256(_dump(config).encode()).hexdigest(), "run_config_sha256": digest,
            "exchange": exchange, "exchange_identity": adapter.identity(), "symbol_rules": rules.record(),
            "source": {**source, "identity": feed.identity()}, "start_at": str(start),
            "client_prefix": hashlib.sha256(session_id.encode()).hexdigest()[:8],
            "baseline_balances": balances, "code_sha256": code_sha, "code_files": code_files,
            "activation": activation,
            "created_utc": _iso(_wall()),
        }
        connection = sqlite3.connect(directory / "live.sqlite", isolation_level=None)
        connection.executescript(SCHEMA_SQL)
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", [(k, _dump(v)) for k, v in meta.items()])
        state = {"cursor": None, "next_seq": 0, "openings_suspended": None, "halted": None}
        connection.execute("INSERT INTO state VALUES (1, 0, ?, 'CREATED', '', ?)", (_dump(state), _iso(_wall())))
        connection.execute("INSERT INTO status_log(at, status, detail) VALUES (?, 'CREATED', '')", (_iso(_wall()),))
        connection.execute("COMMIT")
        connection.close()
        (directory / "session.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return cls(directory, repo_root=repo, adapter=adapter, feed=feed, clock=clock)

    # ================================================================== persistence helpers
    @contextmanager
    def _tx(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        self.connection.execute("COMMIT")

    def _state(self) -> dict:
        return json.loads(self.connection.execute("SELECT payload FROM state WHERE id = 1").fetchone()[0])

    def _save_state(self, state: dict) -> None:
        self.connection.execute("UPDATE state SET payload = ?, version = version + 1, updated_at = ? WHERE id = 1",
                                (_dump(state), _iso(_wall())))

    def status(self) -> tuple[str, str]:
        row = self.connection.execute("SELECT status, detail FROM state WHERE id = 1").fetchone()
        return row[0], row[1]

    def _set_status(self, status: str, detail: str = "") -> None:
        if self.status() == (status, detail):
            return
        own = not self.connection.in_transaction
        if own:
            self.connection.execute("BEGIN IMMEDIATE")
        self.connection.execute("UPDATE state SET status = ?, detail = ?, updated_at = ? WHERE id = 1",
                                (status, detail, _iso(_wall())))
        self.connection.execute("INSERT INTO status_log(at, status, detail) VALUES (?, ?, ?)",
                                (_iso(_wall()), status, detail))
        if own:
            self.connection.execute("COMMIT")

    def _log(self, kind: str, timestamp, **values) -> None:
        sequence = self.connection.execute("SELECT COALESCE(MAX(sequence) + 1, 0) FROM ledger").fetchone()[0]
        record = dict(sequence=sequence, kind=kind, timestamp=_iso(timestamp), **values)
        self.connection.execute("INSERT INTO ledger(sequence, kind, timestamp, record) VALUES (?, ?, ?, ?)",
                                (sequence, kind, record["timestamp"], _dump(record)))

    # ================================================================== portfolio from fills
    def book(self) -> dict:
        """Cash, position and per-asset deltas derived only from exchange fills."""
        rules = self.rules
        cash = D(self.config["run_config"]["initial_cash"])
        position = Decimal(0)
        deltas: dict[str, Decimal] = {rules.base_asset: Decimal(0), rules.quote_asset: Decimal(0)}
        fees_quote = Decimal(0)
        other_fees: dict[str, Decimal] = {}
        for side, price, quantity, commission, asset in self.connection.execute(
                "SELECT side, price, quantity, commission, commission_asset FROM fills ORDER BY rowid"):
            price, quantity, commission = D(price), D(quantity), D(commission)
            quote = price * quantity
            sign = 1 if side == "BUY" else -1
            cash -= sign * quote
            position += sign * quantity
            deltas[rules.quote_asset] -= sign * quote
            deltas[rules.base_asset] += sign * quantity
            deltas[asset] = deltas.get(asset, Decimal(0)) - commission
            if asset == rules.base_asset:
                position -= commission
                fees_quote += commission * price
            elif asset == rules.quote_asset:
                cash -= commission
                fees_quote += commission
            else:
                other_fees[asset] = other_fees.get(asset, Decimal(0)) + commission
        # Operator-accepted differences (rebaseline): they change what the session owns.
        for asset, amount in self.meta.get("adjustments", {}).items():
            amount = D(amount)
            deltas[asset] = deltas.get(asset, Decimal(0)) + amount
            if asset == rules.base_asset:
                position += amount
            elif asset == rules.quote_asset:
                cash += amount
        return {"cash": cash, "position": position, "deltas": deltas, "fees_quote": fees_quote,
                "other_fees": other_fees}

    def _tradable(self, position: Decimal, price: float) -> bool:
        quantity = self.rules.quantity(position)
        return quantity >= self.rules.min_qty and quantity * D(price) >= self.rules.min_notional

    # ================================================================== orders
    def _orders(self, where: str = "", params: tuple = ()) -> list[dict]:
        cols = ("client_order_id", "seq", "bar_ts", "side", "purpose", "request", "status", "exchange_order_id",
                "executed_quantity", "created_at", "updated_at", "detail")
        rows = self.connection.execute(f"SELECT {', '.join(cols)} FROM orders {where} ORDER BY seq", params)
        return [dict(zip(cols, row)) for row in rows]

    def orders(self) -> list[dict]:
        return self._orders()

    def unresolved_orders(self) -> list[dict]:
        marks = ",".join("?" * len(LOCAL_OPEN))
        return self._orders(f"WHERE status IN ({marks})", LOCAL_OPEN)

    def _intent(self, state: dict, side: str, bar_ts, purpose: str, *, budget: Decimal | None,
                reference_price: float) -> OrderRequest:
        """Build and persist (inside the caller's transaction) an order intent."""
        seq = int(state["next_seq"])
        state["next_seq"] = seq + 1
        client_id = f"cdl-{self.meta['client_prefix']}-{seq:06d}"
        rules, kind = self.rules, self.config["order_type"]
        offset = D(self.config["limit_offset"])
        if side == "BUY":
            if kind == "MARKET":
                request = OrderRequest(client_id, rules.symbol, side, "MARKET", quote_quantity=rules.quote(budget))
            else:
                price = rules.price(D(reference_price) * (1 + offset))
                request = OrderRequest(client_id, rules.symbol, side, "LIMIT", quantity=rules.quantity(budget / price),
                                       price=price)
        else:
            quantity = rules.quantity(self.book()["position"])
            if kind == "MARKET":
                request = OrderRequest(client_id, rules.symbol, side, "MARKET", quantity=quantity)
            else:
                request = OrderRequest(client_id, rules.symbol, side, "LIMIT", quantity=quantity,
                                       price=rules.price(D(reference_price) * (1 - offset)))
        now = _iso(self.clock())
        self.connection.execute(
            "INSERT INTO orders(client_order_id, seq, bar_ts, side, purpose, request, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'INTENT', ?, ?)",
            (client_id, seq, _iso(bar_ts), side, purpose, _dump(request.record()), now, now))
        self._log("order", bar_ts, order_id=client_id, purpose=purpose, **request.record())
        return request

    def _apply_report(self, client_id: str, report: OrderReport, *, note: str = "") -> int:
        """Persist an exchange report (inside the caller's transaction); return new fills."""
        row = self.connection.execute("SELECT status, side FROM orders WHERE client_order_id = ?",
                                      (client_id,)).fetchone()
        previous, side = row
        new = 0
        now = self.clock()
        for fill in report.fills:
            inserted = self.connection.execute(
                "INSERT OR IGNORE INTO fills(trade_id, client_order_id, side, price, quantity, commission, "
                "commission_asset, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (fill.trade_id, client_id, side, str(fill.price), str(fill.quantity), str(fill.commission),
                 fill.commission_asset, _iso(now))).rowcount
            if inserted:
                new += 1
                fee_quote = (fill.commission if fill.commission_asset == self.rules.quote_asset else
                             fill.commission * fill.price if fill.commission_asset == self.rules.base_asset else 0)
                self._log("execution", now, client_order_id=client_id, order_id=client_id, side=side,
                          trade_id=fill.trade_id, quantity=float(fill.quantity), price=float(fill.price),
                          fee=float(fee_quote), commission=str(fill.commission),
                          commission_asset=fill.commission_asset, quote=float(fill.price * fill.quantity),
                          slippage_cost=0.0)
        executed = sum((D(q) for (q,) in self.connection.execute(
            "SELECT quantity FROM fills WHERE client_order_id = ?", (client_id,))), Decimal(0))
        status = report.status
        if executed != report.executed_quantity:
            note = (note + "; " if note else "") + \
                f"fills {executed} differ from executedQty {report.executed_quantity}: fills pending"
            if not report.is_open and executed < report.executed_quantity:
                # Terminal on the exchange but trades still missing: keep it reconcilable.
                status = "FILLS_PENDING"
            elif not report.is_open:
                # More trades than the final executedQty: never expected (an open order may show
                # this transiently, because myTrades is read after the order).
                status = "FILLS_EXCESS"
        # updated_at = last change (status or fills): it measures how long a MISSING or
        # FILLS_PENDING order has been stuck.
        changed = previous != status or new
        self.connection.execute(
            "UPDATE orders SET status = ?, exchange_order_id = ?, executed_quantity = ?, report = ?, "
            "updated_at = CASE WHEN ? THEN ? ELSE updated_at END, detail = ? WHERE client_order_id = ?",
            (status, report.exchange_order_id, str(report.executed_quantity), _dump(report.record()),
             1 if changed else 0, _iso(now), note, client_id))
        if changed:
            self._log("order_update", now, client_order_id=client_id, previous=previous, status=status,
                      exchange_status=report.status,
                      executed_quantity=str(report.executed_quantity), quote_executed=str(report.quote_executed),
                      exchange_order_id=report.exchange_order_id, note=note)
        return new

    def _mark(self, client_id: str, status: str, detail: str) -> None:
        previous = self.connection.execute("SELECT status FROM orders WHERE client_order_id = ?",
                                           (client_id,)).fetchone()[0]
        self.connection.execute("UPDATE orders SET status = ?, detail = ?, updated_at = ? WHERE client_order_id = ?",
                                (status, detail, _iso(self.clock()), client_id))
        if previous != status:
            self._log("order_update", self.clock(), client_order_id=client_id, previous=previous, status=status,
                      note=detail)

    def _send(self, request: OrderRequest) -> str:
        # Defence in depth (the session lock already serialises actors): never send for a
        # session that was closed, or an intent that is no longer pending.
        status = self.status()[0]
        row = self.connection.execute("SELECT status FROM orders WHERE client_order_id = ?",
                                      (request.client_order_id,)).fetchone()
        if status in STICKY or row is None or row[0] != "INTENT":
            with self._tx():
                if row is not None and row[0] == "INTENT":
                    self._mark(request.client_order_id, "NOT_PLACED", f"not sent: session {status}")
            return "NOT_SENT"
        _crash("before_send", request.client_order_id)
        try:
            report = self.adapter.submit(request)
        except ExchangeRejected as exc:
            with self._tx():
                self._mark(request.client_order_id, "REJECTED", f"{exc.code}: {exc}")
            return "REJECTED"
        except (ExchangeUnknown, ExchangeError) as exc:
            with self._tx():
                self._mark(request.client_order_id, "UNKNOWN", f"submit outcome unknown: {exc}")
            return "UNKNOWN"
        _crash("after_send", request.client_order_id)
        with self._tx():
            self._apply_report(request.client_order_id, report)
        return report.status

    # ================================================================== reconciliation
    @_exclusive
    def reconcile(self) -> dict:
        """Bring every non-final local order and the balances in line with the exchange."""
        now = self.clock()
        summary = {"queried": 0, "new_fills": 0, "not_placed": 0, "cancelled": 0, "unresolved": 0, "foreign": [],
                   "missing": []}
        for order in self.unresolved_orders():
            client_id = order["client_order_id"]
            summary["queried"] += 1
            try:
                report = self.adapter.query(self.rules.symbol, client_id)
            except OrderNotFound:
                if order["status"] in UNRESOLVED:
                    age = (now - _utc(order["created_at"])).total_seconds()
                    if age >= self.limits["unknown_grace_seconds"]:
                        with self._tx():
                            self._mark(client_id, "NOT_PLACED", f"not found on the exchange after {age:.0f}s")
                        summary["not_placed"] += 1
                    else:
                        summary["unresolved"] += 1
                else:
                    # It was acknowledged by the exchange: fills may exist, it cannot be NOT_PLACED.
                    if order["status"] != "MISSING":
                        with self._tx():
                            self._mark(client_id, "MISSING",
                                       f"{order['status']} order no longer known by the exchange")
                    summary["missing"].append(client_id)
                continue
            except ExchangeError:
                summary["unresolved"] += 1
                continue
            with self._tx():
                summary["new_fills"] += self._apply_report(client_id, report, note="reconciled")
            if report.is_open:
                age = (now - _utc(order["created_at"])).total_seconds()
                if age >= self.limits["order_timeout_seconds"]:
                    summary["cancelled"] += self._cancel(client_id, f"order timeout after {age:.0f}s")
        # Orders on the exchange that this session does not know.
        try:
            known = {o["client_order_id"] for o in self._orders()}
            open_remote = self.adapter.open_orders(self.rules.symbol)
            summary["foreign"] = [o.client_order_id for o in open_remote if o.client_order_id not in known]
        except ExchangeError:
            summary["unresolved"] += 1
        state = self._state()
        ours = [c for c in summary["foreign"] if c.startswith(f"cdl-{self.meta['client_prefix']}-")]
        halted = state.get("halted")
        grace = self.limits["unknown_grace_seconds"]

        def overdue(status: str) -> list[str]:
            return [o["client_order_id"] for o in self._orders("WHERE status = ?", (status,))
                    if (now - _utc(o["updated_at"])).total_seconds() >= grace]

        missing, pending = overdue("MISSING"), overdue("FILLS_PENDING")
        excess = [o["client_order_id"] for o in self._orders("WHERE status = 'FILLS_EXCESS'")]
        if ours:
            halted = f"exchange has orders with this session's prefix unknown locally: {ours}"
        elif excess:
            halted = (f"imported trades exceed the final executedQty of {excess}; check myTrades and the order on "
                      "the exchange, then resolve-missing-order and rebaseline")
        elif missing:
            halted = (f"open orders no longer known by the exchange: {missing}; check the exchange history, "
                      "then resolve-missing-order and rebaseline")
        elif pending:
            halted = (f"executed orders whose trades are still incomplete: {pending}; check myTrades on the "
                      "exchange, then resolve-missing-order and rebaseline")
        elif not any(o["status"] in WAIT_STATUSES for o in self.unresolved_orders()):
            mismatch = self.balance_mismatch()
            if mismatch:
                halted = f"balance mismatch: {mismatch}"
        with self._tx():
            if halted != state.get("halted"):
                state["halted"] = halted
                self._save_state(state)
                if halted:
                    self._log("guard", now, guard="halted", reason=halted)
            if summary["queried"] or summary["foreign"] or summary["missing"]:
                self._log("reconciliation", now, **{k: v for k, v in summary.items()})
        return summary

    def _cancel(self, client_id: str, reason: str) -> int:
        try:
            report = self.adapter.cancel(self.rules.symbol, client_id)
        except ExchangeRejected:  # already filled or cancelled meanwhile
            report = self.adapter.query(self.rules.symbol, client_id)
        except ExchangeError:
            return 0
        with self._tx():
            self._apply_report(client_id, report, note=reason)
            if report.status == "CANCELED":
                self._log("order_cancelled", self.clock(), order_id=client_id, client_order_id=client_id,
                          reason=reason, executed_quantity=str(report.executed_quantity))
        return int(report.status == "CANCELED")

    def balance_mismatch(self) -> str | None:
        """Difference between exchange balances and baseline + fills (None = consistent or not observable)."""
        try:
            observed = self.adapter.balances()
        except ExchangeError:
            return None  # not observable now: the previous verdict (halted or not) is kept
        baseline = {k: D(v) for k, v in self.meta["baseline_balances"].items()}
        for asset, delta in self.book()["deltas"].items():
            expected = baseline.get(asset, Decimal(0)) + delta
            seen = observed.get(asset, Decimal(0))
            tolerance = D(self.limits["balance_abs_tolerance"]) + D(self.limits["balance_rel_tolerance"]) * abs(expected)
            if abs(seen - expected) > tolerance:
                return f"{asset} expected {expected} observed {seen}"
        return None

    # ================================================================== bars and decisions
    def _momentum(self, timestamp: pd.Timestamp, prev_close: float | None) -> dict:
        if prev_close is None:
            return {}
        row = self.connection.execute("SELECT prev_close FROM history WHERE timestamp = ?",
                                      (str(timestamp - 24 * HOUR),)).fetchone()
        if row is None or row[0] is None:
            return {}
        return {"momentum_24h": float(prev_close) / float(row[0]) - 1.0}

    def _last_buy_bar(self) -> pd.Timestamp | None:
        row = self.connection.execute(
            "SELECT o.bar_ts FROM orders o JOIN fills f ON f.client_order_id = o.client_order_id "
            "WHERE o.side = 'BUY' ORDER BY o.seq DESC LIMIT 1").fetchone()
        return _utc(row[0]) if row else None

    def _decide(self, state: dict, timestamp: pd.Timestamp, open_price: float, features: dict,
                now: pd.Timestamp) -> OrderRequest | None:
        book = self.book()
        holding = self._tradable(book["position"], open_price)
        portfolio = Portfolio(float(book["cash"]), float(book["position"]) if holding else 0.0)
        horizon = int(self.run_config.hypothesis.horizon_hours)
        last_buy = self._last_buy_bar()
        if holding and last_buy is not None and timestamp >= last_buy + horizon * HOUR:
            exposure, reason = ACTION_FLAT, "holding horizon reached"
        else:
            proposal = self.strategy.decide(timestamp, dict(features), portfolio)
            exposure, reason = proposal.exposure, proposal.reason
        self._log("decision", timestamp, exposure=exposure, reason=reason)
        accepted, risk_reason, budget = self.risk.assess(Proposal(exposure, reason), portfolio)
        budget = D(budget)
        guard = None
        age_hours = (now - timestamp) / HOUR
        if (exposure == ACTION_LONG) == holding:
            accepted, risk_reason = False, "exposure unchanged"
        elif self.unresolved_orders():
            accepted, risk_reason = False, "order already pending"
        elif state.get("halted"):
            guard = "halted"
        elif exposure == ACTION_LONG:
            position_value = book["position"] * D(open_price)
            room = D(self.limits["max_position_notional"]) - position_value
            budget = min(budget, D(self.limits["max_order_notional"]), max(room, Decimal(0)))
            if state.get("openings_suspended"):
                guard = "openings_suspended"
            elif age_hours > self.limits["max_signal_age_hours"]:
                guard = "stale_feed"
            elif accepted and budget < self.rules.min_notional:
                guard = "exposure_limit" if room < D(self.limits["max_order_notional"]) else "below_exchange_minimum"
        elif state.get("openings_suspended") and not self.config["allow_exits_when_suspended"]:
            guard = "openings_suspended"
        if guard:
            accepted, risk_reason = False, f"guard: {guard}"
            self._log("guard", timestamp, guard=guard, exposure=exposure, signal_age_hours=float(age_hours))
        self._log("risk", timestamp, accepted=accepted, reason=risk_reason,
                  budget=float(budget) if exposure == ACTION_LONG else 0.0)
        if not accepted:
            return None
        purpose = "open" if exposure == ACTION_LONG else ("horizon_exit" if reason == "holding horizon reached"
                                                          else "strategy_exit")
        return self._intent(state, "BUY" if exposure == ACTION_LONG else "SELL", timestamp, purpose,
                            budget=budget if exposure == ACTION_LONG else None, reference_price=open_price)

    @_exclusive
    def poll(self, *, max_bars: int | None = None) -> dict:
        status, _ = self.status()
        if status in STICKY:
            return self.snapshot()
        self.reconcile()
        processed = 0
        start = _utc(self.meta["start_at"])
        while max_bars is None or processed < max_bars:
            if self.status()[0] in STICKY:
                break  # closed or code changed while polling
            if any(o["status"] in WAIT_STATUSES for o in self.unresolved_orders()):
                break  # reconcile before any further decision or order
            now = self.clock()
            view = self.feed.records()
            state = self._state()
            cursor = _utc(state["cursor"]) if state["cursor"] else None
            if cursor is None:
                warm = [(str(ts), r.prev_close) for ts, r in view.records.items() if ts < start and r.valid]
                with self._tx():
                    self.connection.executemany("INSERT OR IGNORE INTO history VALUES (?, ?)", warm)
            candidates = sorted(ts for ts, r in view.records.items()
                                if ts >= start and (cursor is None or ts > cursor) and ts <= now)
            if not candidates:
                break
            timestamp = candidates[0]
            record = view.records[timestamp]
            request = None
            with self._tx():
                if cursor is not None and timestamp != cursor + HOUR:
                    self._log("gap", timestamp, expected=str(cursor + HOUR),
                              missing_hours=int((timestamp - cursor) / HOUR) - 1)
                state["cursor"] = str(timestamp)
                if not record.valid:
                    self._log("data_error", timestamp, problem=record.problem)
                else:
                    features = self._momentum(timestamp, record.prev_close)
                    self.connection.execute("INSERT OR REPLACE INTO history VALUES (?, ?)",
                                            (str(timestamp), record.prev_close))
                    self._log("market", timestamp, open=float(record.open), features=features,
                              feature_available_at=str(timestamp), observed_at=_iso(now))
                    request = self._decide(state, timestamp, float(record.open), features, now)
                    book = self.book()
                    self._log("balance", timestamp, cash=float(book["cash"]), quantity=float(book["position"]),
                              equity=float(book["cash"] + book["position"] * D(record.open)))
                self._save_state(state)
            processed += 1
            if request is not None:
                if hasattr(self.adapter, "set_price") and self.meta["exchange"].get("name") == "fake" \
                        and self.meta["exchange"].get("follow_feed", True):
                    self.adapter.set_price(record.open)
                self._send(request)
        self._refresh_status()
        return self.snapshot()

    @_exclusive
    def reconcile_and_refresh(self) -> dict:
        self.reconcile()
        self._refresh_status()
        return self.snapshot()

    def _refresh_status(self) -> None:
        # Callers hold the session lock: the status read here cannot change before it is written.
        if self.status()[0] in STICKY:  # only finish / accept_code_revision leave these
            return
        state = self._state()
        now = self.clock()
        unresolved = [o for o in self.unresolved_orders() if o["status"] in OPERATOR_STATUSES]
        if state.get("halted"):
            self._set_status("HALTED", state["halted"])
        elif unresolved:
            self._set_status("UNRESOLVED", f"orders to reconcile: {[o['client_order_id'] for o in unresolved]}")
        elif state["cursor"] and (now - _utc(state["cursor"])) / HOUR > self.limits["stale_after_hours"]:
            self._set_status("STALE", f"no bar since {state['cursor']} at {now}")
        elif state.get("openings_suspended"):
            self._set_status("OPENINGS_SUSPENDED", state["openings_suspended"])
        elif state["cursor"]:
            self._set_status("WAITING", f"up to date; last bar {state['cursor']}")

    # ================================================================== operator commands
    def _require(self, action: str, *, trading: bool) -> None:
        """A COMPLETED session accepts no command; ENGINE_MISMATCH accepts no exchange action."""
        status = self.status()[0]
        if status == "COMPLETED":
            raise LiveError(f"{action}: session is COMPLETED; create a new session")
        if trading and status == "ENGINE_MISMATCH":
            raise LiveError(f"{action}: code revision differs from the pinned one; accept-code-revision first "
                            "(or act on the exchange directly and rebaseline afterwards)")

    @_exclusive
    def suspend_openings(self, reason: str = "operator") -> dict:
        self._require("suspend-openings", trading=False)
        state = self._state()
        with self._tx():
            state["openings_suspended"] = reason
            self._save_state(state)
            self._log("operator", self.clock(), action="suspend_openings", reason=reason)
        self._refresh_status()
        return self.snapshot()

    @_exclusive
    def resume_openings(self, reason: str = "operator") -> dict:
        self._require("resume-openings", trading=False)
        state = self._state()
        with self._tx():
            state["openings_suspended"] = None
            self._save_state(state)
            self._log("operator", self.clock(), action="resume_openings", reason=reason)
        self._refresh_status()
        return self.snapshot()

    @_exclusive
    def cancel_open_orders(self, reason: str = "operator") -> dict:
        self._require("cancel-open-orders", trading=True)
        self.reconcile()
        for order in self.unresolved_orders():
            if order["status"] in OPEN_STATUSES:
                self._cancel(order["client_order_id"], reason)
        self._refresh_status()
        return self.snapshot()

    @_exclusive
    def flatten(self, reason: str = "operator") -> dict:
        """Sell the whole tradable position now (MARKET), after reconciliation."""
        self._require("flatten", trading=True)
        self.reconcile()
        state = self._state()
        if self.unresolved_orders():
            raise LiveError("orders still open or unresolved: cancel or reconcile them first")
        if state.get("halted"):
            raise LiveError(f"session halted: {state['halted']}")
        book = self.book()
        price = self.feed.records().records
        last = price[max(price)].open if price else None
        if last is None or not self._tradable(book["position"], last):
            raise LiveError("no tradable position to flatten")
        with self._tx():
            self._log("operator", self.clock(), action="flatten", reason=reason)
            request = self._intent(state, "SELL", self.clock(), "operator_flatten", budget=None, reference_price=last)
            self._save_state(state)
        self._send(request)
        self._refresh_status()
        return self.snapshot()

    @_exclusive
    def rebaseline(self, note: str) -> dict:
        """Operator accepts the exchange balances as the truth.

        The difference ``observed - (baseline + fills + previous adjustments)``
        is recorded as an adjustment per asset: base/quote adjustments change
        the session position/cash, so later exits sell what really exists.
        Unresolved orders must be reconciled first.
        """
        self._require("rebaseline", trading=True)
        if not note:
            raise LiveError("a note explaining the balance difference is required")
        if any(o["status"] in OPERATOR_STATUSES for o in self.unresolved_orders()):
            raise LiveError("reconcile unresolved orders and resolve missing ones before re-baselining")
        observed = self.adapter.balances()
        baseline = {k: D(v) for k, v in self.meta["baseline_balances"].items()}
        deltas = self.book()["deltas"]
        adjustments = {k: D(v) for k, v in self.meta.get("adjustments", {}).items()}
        changes = {}
        for asset in set(deltas) | set(baseline):
            difference = observed.get(asset, Decimal(0)) - (baseline.get(asset, Decimal(0)) + deltas.get(asset, Decimal(0)))
            if difference:
                changes[asset] = str(difference)
                adjustments[asset] = adjustments.get(asset, Decimal(0)) + difference
        adjustments = {k: str(v) for k, v in adjustments.items()}
        state = self._state()
        with self._tx():
            self.connection.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('adjustments', ?)",
                                    (_dump(adjustments),))
            self._log("operator", self.clock(), action="rebaseline", note=note, changes=changes,
                      adjustments=adjustments)
            state["halted"] = None
            self._save_state(state)
        self.meta["adjustments"] = adjustments
        self._refresh_status()
        return self.snapshot()

    @_exclusive
    def resolve_missing_order(self, client_order_id: str, note: str) -> dict:
        """Operator closes an order whose exchange-side history is not obtainable.

        ``MISSING`` (the exchange no longer knows the order) becomes ``LOST``;
        ``FILLS_PENDING`` (executed, trades incomplete) becomes ``FILLS_INCOMPLETE``;
        ``FILLS_EXCESS`` (trades beyond executedQty) becomes ``FILLS_EXCESS_ACCEPTED``.
        Fills already imported stay; anything executed but never reported shows
        up as a balance difference, accepted afterwards with :meth:`rebaseline`.
        """
        self._require("resolve-missing-order", trading=True)
        if not note:
            raise LiveError("a note with the exchange-side evidence is required")
        rows = self._orders("WHERE client_order_id = ?", (client_order_id,))
        targets = {"MISSING": "LOST", "FILLS_PENDING": "FILLS_INCOMPLETE", "FILLS_EXCESS": "FILLS_EXCESS_ACCEPTED"}
        if not rows or rows[0]["status"] not in targets:
            raise LiveError(f"{client_order_id} is not a MISSING, FILLS_PENDING or FILLS_EXCESS order")
        with self._tx():
            self._mark(client_order_id, targets[rows[0]["status"]], f"resolved by operator: {note}")
            self._log("operator", self.clock(), action="resolve_missing_order", client_order_id=client_order_id,
                      note=note)
        self._refresh_status()
        return self.snapshot()

    def real_capital(self) -> bool:
        """Mainnet session, or one created through the S08 activation gate."""
        exchange = self.meta["exchange"]
        return bool(self.meta.get("activation")) or (
            exchange.get("name") == "binance-spot" and exchange.get("environment", "testnet") != "testnet")

    @_exclusive
    def accept_code_revision(self, note: str, *, approval: str | Path | None = None) -> dict:
        """Pin the current code.  With real capital it needs a new S08 approval of this exact code."""
        self._require("accept-code-revision", trading=False)
        current, files = live_digests()
        activation = self.meta.get("activation")
        if self.real_capital():
            from .release import ActivationError, verify_activation

            if not approval:
                raise LiveError("accept-code-revision: real-capital session; a code change needs a new S08 release "
                                "card and approval for this code (pass --approval)")
            path = Path(approval)
            try:
                activation = verify_activation(path if path.is_absolute() else self.repo_root / path,
                                               live_config=self.config,
                                               exchange={**self.meta["exchange"], "approval": str(approval)},
                                               code_sha256=current)
            except ActivationError as exc:
                raise LiveError(f"accept-code-revision refused: {exc}") from exc
        with self._tx():
            self.connection.executemany("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                                        [("code_sha256", _dump(current)), ("code_files", _dump(files)),
                                         ("activation", _dump(activation))])
            self._log("operator", self.clock(), action="accept_code_revision", code_sha256=current, note=note,
                      activation=activation)
        self.meta.update(code_sha256=current, code_files=files, activation=activation)
        self._set_status("RUNNING", f"code revision {current[:12]} accepted: {note}")
        return self.snapshot()

    @_exclusive
    def finish(self, reason: str = "operator") -> dict:
        self._require("session-finish", trading=True)
        self.reconcile()
        if self.unresolved_orders():
            raise LiveError("cannot finish with open or unresolved orders")
        book = self.book()
        with self._tx():
            self._log("end", self.clock(), reason=reason, cash=float(book["cash"]), quantity=float(book["position"]),
                      position_open=bool(book["position"] > 0))
        self._set_status("COMPLETED", reason)
        return self.snapshot()

    # ================================================================== reading
    def ledger(self) -> list[dict]:
        return [json.loads(r[0]) for r in self.connection.execute("SELECT record FROM ledger ORDER BY sequence")]

    def fills(self) -> list[dict]:
        cols = ("trade_id", "client_order_id", "side", "price", "quantity", "commission", "commission_asset")
        return [dict(zip(cols, r)) for r in self.connection.execute(f"SELECT {', '.join(cols)} FROM fills ORDER BY rowid")]

    def status_log(self) -> list[dict]:
        return [dict(zip(("at", "status", "detail"), r)) for r in
                self.connection.execute("SELECT at, status, detail FROM status_log ORDER BY id")]

    def snapshot(self) -> dict:
        status, detail = self.status()
        state = self._state()
        book = self.book()
        return {
            "session_id": self.meta["session_id"], "name": self.meta["name"], "status": status, "detail": detail,
            "exchange": self.meta["exchange_identity"], "cursor": state["cursor"],
            "openings_suspended": state.get("openings_suspended"), "halted": state.get("halted"),
            "cash": str(book["cash"]), "position": str(book["position"]), "fees_quote": str(book["fees_quote"]),
            "orders": len(self._orders()), "open_orders": [o["client_order_id"] for o in self.unresolved_orders()],
            "fills": self.connection.execute("SELECT COUNT(*) FROM fills").fetchone()[0],
            "config_sha256": self.meta["config_sha256"], "code_sha256": self.meta["code_sha256"],
        }

    def close(self) -> None:
        self.connection.close()


def list_sessions(root: str | Path = DEFAULT_ROOT, *, repo_root: str | Path = ".") -> list[Path]:
    base = Path(root) if Path(root).is_absolute() else Path(repo_root).resolve() / root
    directory = base / "sessions"
    return sorted(p for p in directory.iterdir() if (p / "live.sqlite").is_file()) if directory.is_dir() else []


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="S07 live runner (testnet / fixture exchange)")
    parser.add_argument("--repo-root", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("session-create")
    create.add_argument("--name", required=True)
    create.add_argument("--config", required=True, help="live config JSON")
    create.add_argument("--exchange", choices=("binance-testnet", "binance-mainnet", "fake"), required=True)
    create.add_argument("--fake-state", help="state file of the fixture exchange")
    create.add_argument("--approval", help="S08 approval.json (required for binance-mainnet)")
    create.add_argument("--feed-dir", help="lab feed directory (else Binance klines of the same environment)")
    create.add_argument("--start", required=True)
    create.add_argument("--root", default=str(DEFAULT_ROOT))
    commands = ("session-run", "session-status", "session-reconcile", "suspend-openings", "resume-openings",
                "cancel-open-orders", "flatten", "rebaseline", "resolve-missing-order", "accept-code-revision",
                "session-finish")
    for name in commands:
        command = sub.add_parser(name)
        command.add_argument("--session-dir", required=True)
        command.add_argument("--note", default="operator")
        if name == "resolve-missing-order":
            command.add_argument("--client-order-id", required=True)
        if name == "accept-code-revision":
            command.add_argument("--approval", help="S08 approval.json of the new code (real-capital sessions)")
        if name == "session-run":
            command.add_argument("--loop", action="store_true")
            command.add_argument("--interval", type=float, default=60.0)
    args = parser.parse_args(argv)
    repo = Path(args.repo_root).resolve()
    if args.command == "session-create":
        raw = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
        if args.exchange == "fake":
            if not args.fake_state:
                parser.error("--fake-state is required with --exchange fake")
            exchange = {"name": "fake", "state_path": args.fake_state}
        elif args.exchange == "binance-mainnet":
            exchange = {"name": "binance-spot", "environment": "mainnet", "allow_mainnet": True,
                        "approval": args.approval}
        else:
            exchange = {"name": "binance-spot", "environment": "testnet"}
        source = {"kind": "lab_feed", "path": args.feed_dir} if args.feed_dir else \
            {"kind": "binance_klines", "environment": exchange.get("environment", "testnet"),
             "symbol": raw.get("symbol", "BTCUSDT")}
        session = LiveSession.create(args.name, raw, exchange=exchange, source=source, start_at=args.start,
                                     root=args.root, repo_root=repo)
        print(json.dumps({"session_dir": str(session.directory), **session.snapshot()}, indent=2, default=str))
        return 0
    session = LiveSession(Path(args.session_dir), repo_root=repo)
    if args.command == "session-run":
        while True:
            try:
                snapshot = session.poll()
            except SessionBusy as exc:
                if not args.loop:
                    raise
                print(json.dumps({"status": "BUSY", "detail": str(exc)}), flush=True)
                time.sleep(args.interval)
                continue
            print(json.dumps({k: snapshot[k] for k in ("status", "detail", "cursor", "position", "open_orders")},
                             default=str), flush=True)
            if not args.loop or snapshot["status"] in ("COMPLETED", "ENGINE_MISMATCH"):
                return 0
            time.sleep(args.interval)
    actions = {
        "session-status": session.snapshot,
        "session-reconcile": session.reconcile_and_refresh,
        "suspend-openings": lambda: session.suspend_openings(args.note),
        "resume-openings": lambda: session.resume_openings(args.note),
        "cancel-open-orders": lambda: session.cancel_open_orders(args.note),
        "flatten": lambda: session.flatten(args.note),
        "rebaseline": lambda: session.rebaseline(args.note),
        "resolve-missing-order": lambda: session.resolve_missing_order(args.client_order_id, args.note),
        "accept-code-revision": lambda: session.accept_code_revision(args.note, approval=args.approval),
        "session-finish": lambda: session.finish(args.note),
    }
    print(json.dumps(actions[args.command](), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
