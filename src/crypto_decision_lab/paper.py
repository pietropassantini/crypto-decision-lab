"""S06 persistent paper trading sessions.

A session lives in ``<root>/sessions/<session_id>/session.sqlite`` and owns:

* ``meta``      — immutable configuration (normalized RunConfig and digest,
                  source identity, start/end, staleness, engine revision);
* ``state``     — one row: serialized :class:`~.simulation.Engine` state,
                  cursor, explicit status and an optimistic ``version``;
* ``events``    — processed events (engine record + raw feed values), the
                  identity used to refuse duplicates and revisions;
* ``history``   — ``prev_close`` per hour (warmup and processed), feeding
                  ``momentum_24h`` with closed bars only;
* ``ledger``    — the canonical ledger, append-only;
* ``status_log``— every status transition with the feed clock.

Each event is applied in **one SQLite transaction** (event, ledger records,
history and state together): an interruption before the commit leaves the
previous state, an interruption after it leaves the new one, and resuming
continues from the persisted cursor with the same ledger as an uninterrupted
session.  The runner works without the UI (CLI ``session-run``).

Explicit statuses: ``CREATED``, ``RUNNING``, ``WAITING`` (up to date),
``STALE`` (no new bar for ``stale_after_hours`` of feed clock), ``PAUSED_GAP``
(a later bar exists but the next hour is missing), ``DATA_ERROR`` (invalid or
conflicting bar, revised processed bar), ``SUSPENDED`` (operator),
``MODEL_ERROR`` / ``ENGINE_MISMATCH`` (pinned strategy, model or engine
unavailable), ``COMPLETED``.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable

import pandas as pd

from . import studio_analytics as analytics
from .execution_contract import HOUR
from .forward_isolation import guard_research_output
from .lab_feed import FeedView, LabFeed, SnapshotSource
from .simulation import Engine, MarketEvent, engine_digests
from .studio import build_run_config, config_digest, normalize_config

SESSION_SCHEMA = "cdl.s06.session.v1"
DEFAULT_ROOT = Path("var/paper")
BLOCKING = ("SUSPENDED", "COMPLETED", "MODEL_ERROR", "ENGINE_MISMATCH")
CRASH_HOOK: Callable[[str, pd.Timestamp], None] | None = None
_ENV_CRASH = "CDL_PAPER_CRASH"  # "<stage>:<n>" -> os._exit(97) at the n-th step of this process
_steps_in_process = 0


class SessionError(RuntimeError):
    """Raised for invalid session operations."""


class ConcurrentUpdate(SessionError):
    """Another runner advanced the session first."""


def _utc(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dump(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _crash_point(stage: str, timestamp: pd.Timestamp) -> None:
    global _steps_in_process
    if CRASH_HOOK is not None:
        CRASH_HOOK(stage, timestamp)
    spec = os.environ.get(_ENV_CRASH)
    if spec:
        wanted, _, count = spec.partition(":")
        if stage == wanted and _steps_in_process + 1 == int(count or 1):
            os._exit(97)


SCHEMA_SQL = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE state(id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL, engine TEXT NOT NULL,
                   cursor TEXT, status TEXT NOT NULL, detail TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE events(timestamp TEXT PRIMARY KEY, record TEXT NOT NULL, raw TEXT NOT NULL);
CREATE TABLE history(timestamp TEXT PRIMARY KEY, prev_close REAL);
CREATE TABLE ledger(sequence INTEGER PRIMARY KEY, kind TEXT NOT NULL, timestamp TEXT NOT NULL, record TEXT NOT NULL);
CREATE TABLE status_log(id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, clock TEXT, status TEXT NOT NULL,
                        detail TEXT NOT NULL);
"""


class _SeenEvents:
    """Engine duplicate map backed by the ``events`` table (same transaction)."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.pending_raw: dict | None = None

    def __contains__(self, timestamp) -> bool:
        return self.connection.execute("SELECT 1 FROM events WHERE timestamp = ?", (str(timestamp),)).fetchone() is not None

    def __getitem__(self, timestamp):
        row = self.connection.execute("SELECT record FROM events WHERE timestamp = ?", (str(timestamp),)).fetchone()
        if row is None:
            raise KeyError(timestamp)
        return json.loads(row[0])

    def __setitem__(self, timestamp, record) -> None:
        self.connection.execute("INSERT INTO events(timestamp, record, raw) VALUES (?, ?, ?)",
                                (str(timestamp), _dump(record), _dump(self.pending_raw)))


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:32] or "session"


class PaperSession:
    def __init__(self, directory: Path, *, repo_root: str | Path = ".") -> None:
        self.directory = Path(directory)
        self.repo_root = Path(repo_root).resolve()
        path = self.directory / "session.sqlite"
        if not path.is_file():
            raise SessionError(f"session database not found: {path}")
        self.connection = sqlite3.connect(path, isolation_level=None, timeout=30)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.meta = {key: json.loads(value) for key, value in self.connection.execute("SELECT key, value FROM meta")}
        self.config = None
        self.source = None
        self._load_pins()

    # ------------------------------------------------------------ creation
    @classmethod
    def create(cls, name: str, raw_config: dict, source: dict, *, start_at, end_at=None,
               stale_after_hours: int = 2, root: str | Path = DEFAULT_ROOT, repo_root: str | Path = ".",
               config_id: str | None = None) -> "PaperSession":
        repo = Path(repo_root).resolve()
        normalized = normalize_config(raw_config)
        build_run_config(normalized)  # strategy and model must load now: they are pinned at start
        digest = config_digest(normalized)
        start = _utc(start_at)
        end = _utc(end_at) if end_at is not None else None
        if end is not None and end <= start:
            raise SessionError("end_at must follow start_at")
        resolved = cls._open_source(source, repo)
        if resolved.kind == "snapshot" and end is None:
            raise SessionError("a replay session over a snapshot requires end_at")
        identity = resolved.identity()
        session_id = f"{_slug(name)}-" + hashlib.sha256(
            _dump([digest, identity, str(start), str(end)]).encode()).hexdigest()[:10]
        base = Path(root) if Path(root).is_absolute() else repo / root
        directory = guard_research_output(base / "sessions" / session_id, purpose="paper session")
        if directory.exists():
            raise FileExistsError(f"session already exists: {directory}")
        directory.mkdir(parents=True)
        engine_sha, _ = engine_digests()
        meta = {
            "schema_version": SESSION_SCHEMA, "session_id": session_id, "name": name,
            "config": normalized, "config_sha256": digest, "config_id": config_id,
            "source": {**source, "identity": identity},
            "mode": "paper-replay" if resolved.kind == "snapshot" else "paper",
            "start_at": str(start), "end_at": str(end) if end is not None else None,
            "stale_after_hours": int(stale_after_hours), "engine_sha256": engine_sha, "created_utc": _now(),
        }
        connection = sqlite3.connect(directory / "session.sqlite", isolation_level=None)
        connection.executescript(SCHEMA_SQL)
        connection.execute("BEGIN IMMEDIATE")
        connection.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", [(k, _dump(v)) for k, v in meta.items()])
        engine = Engine(build_run_config(normalized))
        connection.execute("INSERT INTO state VALUES (1, 0, ?, NULL, 'CREATED', '', ?)", (_dump(engine.state()), _now()))
        connection.execute("INSERT INTO status_log(at, clock, status, detail) VALUES (?, NULL, 'CREATED', '')", (_now(),))
        connection.execute("COMMIT")
        connection.close()
        (directory / "session.json").write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return cls(directory, repo_root=repo)

    @staticmethod
    def _open_source(source: dict, repo_root: Path):
        if source.get("kind") == "snapshot":
            return SnapshotSource(source["dataset_id"], repo_root=repo_root,
                                  catalog_path=source.get("catalog", "config/dataset-catalog.json"))
        if source.get("kind") == "lab_feed":
            path = Path(source["path"])
            return LabFeed(path if path.is_absolute() else repo_root / path)
        raise SessionError(f"unknown source kind: {source.get('kind')}")

    def _load_pins(self) -> None:
        """Strategy, model, engine and source are those pinned at creation."""

        status, _ = self._status()
        if status in ("COMPLETED",):
            return
        try:
            self.config = build_run_config(self.meta["config"])
        except Exception as exc:  # model artefact missing, replaced or tampered
            self._set_status("MODEL_ERROR", f"pinned strategy/model unavailable: {exc}")
            return
        if status == "MODEL_ERROR":  # the pinned artefact (same digest) is available again
            self._set_status("RUNNING", "pinned strategy/model available again")
        current, _ = engine_digests()
        if current != self.meta["engine_sha256"] and status != "ENGINE_MISMATCH":
            self._set_status("ENGINE_MISMATCH",
                             f"engine {current[:12]} differs from pinned {self.meta['engine_sha256'][:12]}")
        try:
            self.source = self._open_source(self.meta["source"], self.repo_root)
        except Exception as exc:
            self._set_status("DATA_ERROR", f"source unavailable: {exc}")
            return
        if self.source.identity() != self.meta["source"]["identity"]:
            self._set_status("DATA_ERROR", "source identity differs from the one pinned at creation")

    # ------------------------------------------------------------ state access
    def _status(self) -> tuple[str, str]:
        row = self.connection.execute("SELECT status, detail FROM state WHERE id = 1").fetchone()
        return row[0], row[1]

    def _row(self):
        return self.connection.execute("SELECT version, engine, cursor, status FROM state WHERE id = 1").fetchone()

    def _set_status(self, status: str, detail: str = "", clock=None) -> None:
        current = self._status()
        if current == (status, detail):
            return
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute("UPDATE state SET status = ?, detail = ?, updated_at = ?, version = version + 1 "
                                    "WHERE id = 1", (status, detail, _now()))
            self.connection.execute("INSERT INTO status_log(at, clock, status, detail) VALUES (?, ?, ?, ?)",
                                    (_now(), str(clock) if clock is not None else None, status, detail))
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def _commit_step(self, version: int, engine: Engine, records: list[dict], cursor, status: str, detail: str,
                     history: tuple | None, clock, stage_ts) -> None:
        cursor_value = str(cursor) if cursor is not None else None
        self.connection.executemany(
            "INSERT INTO ledger(sequence, kind, timestamp, record) VALUES (?, ?, ?, ?)",
            [(r["sequence"], r["kind"], r["timestamp"], _dump(r)) for r in records])
        if history is not None:
            self.connection.execute("INSERT OR REPLACE INTO history(timestamp, prev_close) VALUES (?, ?)", history)
        updated = self.connection.execute(
            "UPDATE state SET version = version + 1, engine = ?, cursor = ?, status = ?, detail = ?, updated_at = ? "
            "WHERE id = 1 AND version = ?", (_dump(engine.state()), cursor_value, status, detail, _now(), version))
        if updated.rowcount != 1:
            raise ConcurrentUpdate("session advanced by another runner")
        previous = self._status_in_tx()
        if previous != (status, detail):
            self.connection.execute("INSERT INTO status_log(at, clock, status, detail) VALUES (?, ?, ?, ?)",
                                    (_now(), str(clock) if clock is not None else None, status, detail))
        _crash_point("before_commit", stage_ts)
        self.connection.execute("COMMIT")
        _crash_point("after_commit", stage_ts)

    def _status_in_tx(self):
        row = self.connection.execute("SELECT status, detail FROM status_log ORDER BY id DESC LIMIT 1").fetchone()
        return (row[0], row[1]) if row else (None, None)

    def _engine(self, state_json: str) -> Engine:
        return Engine(self.config, state=json.loads(state_json), seen=_SeenEvents(self.connection))

    # ------------------------------------------------------------ feature input
    def _momentum(self, timestamp: pd.Timestamp, prev_close: float | None) -> dict:
        if prev_close is None:
            return {}
        row = self.connection.execute("SELECT prev_close FROM history WHERE timestamp = ?",
                                      (str(timestamp - 24 * HOUR),)).fetchone()
        if row is None or row[0] is None:
            return {}
        return {"momentum_24h": float(prev_close) / float(row[0]) - 1.0}

    def _warmup(self, view: FeedView, start: pd.Timestamp) -> None:
        rows = [(str(ts), record.prev_close) for ts, record in view.records.items()
                if ts < start and record.valid]
        if rows:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.executemany("INSERT OR IGNORE INTO history(timestamp, prev_close) VALUES (?, ?)", rows)
            self.connection.execute("COMMIT")

    def _revisions(self, view: FeedView, cursor) -> str | None:
        if cursor is None:
            return None
        for timestamp, raw in self.connection.execute("SELECT timestamp, raw FROM events"):
            record = view.records.get(_utc(timestamp))
            if record is not None and record.valid and _dump(record.raw()) != raw:
                return f"feed revised an already processed bar at {timestamp}"
        return None

    # ------------------------------------------------------------ operations
    def poll(self, now=None, *, max_events: int | None = None) -> dict:
        """Process every available event in order; return the snapshot."""

        global _steps_in_process
        status, _ = self._status()
        if status in BLOCKING or self.config is None or self.source is None:
            return self.snapshot()
        clock = _utc(now) if now is not None else self.source.now()
        view = self.source.records()
        start = _utc(self.meta["start_at"])
        end = _utc(self.meta["end_at"]) if self.meta["end_at"] else None
        if view.conflicts:
            self._set_status("DATA_ERROR", "; ".join(view.conflicts), clock)
            return self.snapshot()
        self._warmup(view, start)
        version, state_json, cursor, _ = self._row()
        cursor = _utc(cursor) if cursor else None
        revision = self._revisions(view, cursor)  # bars processed in earlier polls must not change
        if revision:
            self._set_status("DATA_ERROR", revision, clock)
            return self.snapshot()
        engine = self._engine(state_json)  # kept in memory: equal to the persisted state after each commit
        processed = 0
        while max_events is None or processed < max_events:
            next_ts = cursor + HOUR if cursor is not None else start
            if end is not None and next_ts >= end:
                self.finish("end of the configured period", now=clock)
                break
            record = view.records.get(next_ts)
            if record is None:
                later = [ts for ts in view.records if ts > next_ts]
                if later:
                    self._set_status("PAUSED_GAP", f"missing bar {next_ts}; next available {min(later)}", clock)
                elif clock is None:
                    self._set_status("DATA_ERROR", f"source ends before {next_ts}", clock)
                elif clock - next_ts >= int(self.meta["stale_after_hours"]) * HOUR:
                    self._set_status("STALE", f"no bar for {next_ts} at feed clock {clock}", clock)
                else:
                    self._set_status("WAITING", f"up to date; next bar {next_ts}", clock)
                break
            if not record.valid:
                self._set_status("DATA_ERROR", record.problem or "invalid bar", clock)
                break
            engine.seen.pending_raw = record.raw()
            event = MarketEvent(next_ts, float(record.open), self._momentum(next_ts, record.prev_close))
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                records = engine.step(event)
                self._commit_step(version, engine, records, next_ts, "RUNNING", f"last event {next_ts}",
                                  (str(next_ts), record.prev_close), clock, next_ts)
            except BaseException:
                # The in-memory engine may be ahead of the database: discard it with this object.
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise
            version += 1
            cursor = next_ts
            processed += 1
            _steps_in_process += 1
        return self.snapshot()

    def acknowledge_gap(self, note: str = "") -> dict:
        status, _ = self._status()
        if status != "PAUSED_GAP":
            raise SessionError("no paused gap to acknowledge")
        view = self.source.records()
        version, state_json, cursor, _ = self._row()
        next_ts = _utc(cursor) + HOUR if cursor else _utc(self.meta["start_at"])
        later = sorted(ts for ts in view.records if ts > next_ts)
        engine = self._engine(state_json)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if cursor is None:
                raise SessionError("the first bar is missing: change start_at instead")
            records = engine.acknowledge_gap(later[0], note=note or "operator acknowledged the gap")
            self._commit_step(version, engine, records, later[0] - HOUR, "RUNNING",
                              f"gap acknowledged; resume at {later[0]}", None, self.source.now(), later[0])
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        return self.snapshot()

    def suspend(self, reason: str = "operator") -> dict:
        status, _ = self._status()
        if status == "COMPLETED":
            raise SessionError("session is COMPLETED: it cannot be suspended or resumed")
        self._set_status("SUSPENDED", reason, self.source.now() if self.source else None)
        return self.snapshot()

    def resume(self) -> dict:
        status, _ = self._status()
        if status != "SUSPENDED":
            raise SessionError("session is not suspended")
        self._set_status("RUNNING", "resumed by operator", self.source.now() if self.source else None)
        # Resuming never bypasses the pins: model, engine and source are checked again.
        self._load_pins()
        return self.snapshot()

    def accept_engine_revision(self, note: str) -> dict:
        """Explicit operator decision to continue with a new engine revision."""

        if self._status()[0] == "COMPLETED":
            raise SessionError("session is COMPLETED")
        current, _ = engine_digests()
        self.connection.execute("BEGIN IMMEDIATE")
        self.connection.execute("UPDATE meta SET value = ? WHERE key = 'engine_sha256'", (_dump(current),))
        self.connection.execute("COMMIT")
        self.meta["engine_sha256"] = current
        self._set_status("RUNNING", f"engine revision {current[:12]} accepted: {note}")
        return self.snapshot()

    def finish(self, reason: str = "operator", *, now=None) -> dict:
        version, state_json, cursor, status = self._row()
        if status == "COMPLETED":
            return self.snapshot()
        engine = self._engine(state_json)
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            records = engine.finish() if engine.clock is not None else []
            self._commit_step(version, engine, records, _utc(cursor) if cursor else None, "COMPLETED", reason,
                              None, now, cursor)
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        return self.snapshot()

    # ------------------------------------------------------------ reading
    def ledger(self) -> list[dict]:
        return [json.loads(row[0]) for row in self.connection.execute("SELECT record FROM ledger ORDER BY sequence")]

    def status_log(self) -> list[dict]:
        return [dict(zip(("at", "clock", "status", "detail"), row)) for row in
                self.connection.execute("SELECT at, clock, status, detail FROM status_log ORDER BY id")]

    def snapshot(self) -> dict:
        version, state_json, cursor, status = self._row()
        _, detail = self._status()
        state = json.loads(state_json)
        ledger = self.ledger()
        initial = float(self.meta["config"]["initial_cash"])
        summary = analytics.summarize(ledger, initial) if ledger else None
        last_price = state.get("last_price")
        quantity = float(state.get("quantity") or 0.0)
        cash = float(state.get("cash"))
        equity = cash + quantity * float(last_price or 0.0)
        return {
            "session_id": self.meta["session_id"], "name": self.meta["name"], "mode": self.meta["mode"],
            "status": status, "detail": detail, "version": version,
            "cursor": cursor, "last_event": state.get("previous"),
            "feed_clock": str(self.source.now()) if self.source is not None and self.source.now() is not None else None,
            "cash": cash, "quantity": quantity, "last_price": last_price, "equity": equity,
            "pnl": equity - initial, "pending_order": state.get("pending"),
            "events": self.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            "ledger_records": len(ledger),
            "fees": summary["fees"] if summary else 0.0, "slippage": summary["slippage"] if summary else 0.0,
            "costs": summary["costs"] if summary else 0.0, "trades": summary["trades"] if summary else 0,
            "config_sha256": self.meta["config_sha256"], "strategy": self.meta["config"]["strategy"],
            "engine_sha256": self.meta["engine_sha256"], "source": self.meta["source"]["identity"],
        }

    def close(self) -> None:
        self.connection.close()


def list_sessions(root: str | Path = DEFAULT_ROOT, *, repo_root: str | Path = ".") -> list[Path]:
    base = Path(root) if Path(root).is_absolute() else Path(repo_root).resolve() / root
    directory = base / "sessions"
    if not directory.is_dir():
        return []
    return sorted(path for path in directory.iterdir() if (path / "session.sqlite").is_file())


def main(argv=None) -> int:
    import argparse
    import time

    parser = argparse.ArgumentParser(description="S06 persistent paper sessions and lab feeds")
    parser.add_argument("--repo-root", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    feed_create = sub.add_parser("feed-create")
    feed_create.add_argument("--scenario", required=True)
    feed_create.add_argument("--feed-dir", required=True)
    feed_advance = sub.add_parser("feed-advance")
    feed_advance.add_argument("--feed-dir", required=True)
    feed_advance.add_argument("--hours", type=int)
    feed_advance.add_argument("--until")
    create = sub.add_parser("session-create")
    create.add_argument("--name", required=True)
    create.add_argument("--config", required=True, help="RunConfig JSON file")
    create.add_argument("--snapshot", help="catalog dataset_id for a replay session")
    create.add_argument("--feed-dir", help="lab feed directory")
    create.add_argument("--start", required=True)
    create.add_argument("--end")
    create.add_argument("--stale-after-hours", type=int, default=2)
    create.add_argument("--root", default=str(DEFAULT_ROOT))
    for name in ("session-run", "session-status", "session-ack-gap", "session-suspend", "session-resume",
                 "session-finish"):
        command = sub.add_parser(name)
        command.add_argument("--session-dir", required=True)
        if name == "session-run":
            command.add_argument("--loop", action="store_true")
            command.add_argument("--interval", type=float, default=60.0)
        if name in ("session-ack-gap", "session-suspend", "session-finish"):
            command.add_argument("--note", default="operator")
    args = parser.parse_args(argv)
    repo = Path(args.repo_root).resolve()
    if args.command == "feed-create":
        scenario = json.loads(Path(args.scenario).read_text(encoding="utf-8-sig"))
        feed = LabFeed.create(args.feed_dir, scenario, repo_root=repo)
        print(json.dumps(feed.identity(), indent=2))
        return 0
    if args.command == "feed-advance":
        feed = LabFeed(Path(args.feed_dir) if Path(args.feed_dir).is_absolute() else repo / args.feed_dir)
        published = feed.advance(args.hours, until=args.until)
        print(json.dumps({"now": str(feed.now()), "published": published}))
        return 0
    if args.command == "session-create":
        if bool(args.snapshot) == bool(args.feed_dir):
            parser.error("choose exactly one of --snapshot or --feed-dir")
        source = {"kind": "snapshot", "dataset_id": args.snapshot} if args.snapshot else \
            {"kind": "lab_feed", "path": args.feed_dir}
        raw = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
        session = PaperSession.create(args.name, raw, source, start_at=args.start, end_at=args.end,
                                      stale_after_hours=args.stale_after_hours, root=args.root, repo_root=repo)
        print(json.dumps({"session_dir": str(session.directory), **session.snapshot()}, indent=2, default=str))
        return 0
    session = PaperSession(Path(args.session_dir), repo_root=repo)
    if args.command == "session-run":
        while True:
            snapshot = session.poll()
            print(json.dumps({k: snapshot[k] for k in ("status", "detail", "cursor", "equity", "trades")}, default=str),
                  flush=True)
            if not args.loop or snapshot["status"] in BLOCKING:
                return 0
            time.sleep(args.interval)
    actions = {
        "session-status": lambda: session.snapshot(),
        "session-ack-gap": lambda: session.acknowledge_gap(args.note),
        "session-suspend": lambda: session.suspend(args.note),
        "session-resume": lambda: session.resume(),
        "session-finish": lambda: session.finish(args.note),
    }
    print(json.dumps(actions[args.command](), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
