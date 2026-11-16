"""Read-only, consistent snapshot of a paper session for the E05-P1 reports and checks.

The reports must not open a session the way the runner does: ``PaperSession`` re-checks the pins (it can
change the stored status), opens the data source and asks it for the clock (network).  A snapshot reads
everything the reports need -- metadata, state, ledger, events, history, status log -- in **one read
transaction** of a read-only connection, so:

* no table of the session database is changed and the main database file is never written;
* no data source is contacted;
* the pieces are consistent with one another even while the runner is committing events.

Contract on SQLite's auxiliary files: a session database is in WAL mode, and SQLite may create the
``-wal`` and ``-shm`` files next to it when a reader opens it and they are absent (the folder must then be
writable).  For an archived copy in a read-only folder without a ``-wal`` file, the reader falls back to
``immutable`` mode, which is safe exactly because there is no write-ahead log to ignore.  A read-only folder
that still holds a ``-wal`` file cannot be read safely in place: copy the session to a writable folder.

``until(instant)`` / ``read_snapshot(as_of=...)`` give the session **in market time** as of ``instant``: the
ledger, the processed events and the history are cut on the bar timestamp; the status log is cut on the
wall clock of each row (and on the feed clock it records).  A bar committed late, after ``instant``, but whose
market timestamp is not later, stays in the cut while its commit row does not: the cut is a retrospective
replay by market time, and timeliness is measured on the wall clock only.  The cursor of a cut is the last
bar it contains; its status is ``None`` because the runner's status at that instant is not stored.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3

import pandas as pd


def _utc(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


@dataclass(frozen=True)
class SessionSnapshot:
    directory: Path
    meta: dict
    status: str | None                       # None in a cut: the status at that instant is not stored
    cursor: str | None
    ledger: list[dict]
    events: list[tuple[str, dict]]          # (bar timestamp, raw feed values) of the processed bars
    history: list[tuple[str, float | None]]  # (timestamp, prev_close): warmup and processed bars
    status_log: list[dict]                   # at (wall clock), clock (feed clock), status, detail
    as_of: pd.Timestamp | None = None

    def until(self, instant) -> "SessionSnapshot":
        """The session in market time as of ``instant`` (included); see the module docstring."""
        cut = _utc(instant)
        events = [(ts, raw) for ts, raw in self.events if _utc(ts) <= cut]
        log = [row for row in self.status_log
               if _utc(row["at"]) <= cut and (row["clock"] is None or _utc(row["clock"]) <= cut)]
        return replace(
            self, as_of=cut, status=None, cursor=events[-1][0] if events else None, events=events,
            ledger=[r for r in self.ledger if _utc(r["timestamp"]) <= cut],
            history=[(ts, value) for ts, value in self.history if _utc(ts) <= cut], status_log=log)


def _connect_read_only(database: Path) -> sqlite3.Connection:
    uri = f"{database.as_uri()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=30, isolation_level=None)
        connection.execute("SELECT 1 FROM meta LIMIT 1")
        return connection
    except sqlite3.OperationalError as error:
        # A read-only folder: SQLite cannot create the -shm file it needs for a WAL database.  Without a
        # -wal file every committed page is in the main file, so reading it as immutable loses nothing.
        if Path(f"{database}-wal").exists():
            raise sqlite3.OperationalError(
                f"{error}: cannot read {database}, which has a -wal file next to it. If the folder is read-only, "
                "copy the whole session folder (database, -wal and -shm) to a writable folder and read the copy"
            ) from error
        connection = sqlite3.connect(f"{uri}&immutable=1", uri=True, timeout=30, isolation_level=None)
        connection.execute("SELECT 1 FROM meta LIMIT 1")
        return connection


def read_snapshot(directory: str | Path, *, as_of=None) -> SessionSnapshot:
    """One consistent read of the session, cut at ``as_of`` when given (a report for a past instant)."""
    path = Path(directory)
    database = (path / "session.sqlite").resolve()
    if not database.is_file():
        raise FileNotFoundError(f"session database not found: {database}")
    connection = _connect_read_only(database)
    try:
        connection.execute("BEGIN")
        meta = {key: json.loads(value) for key, value in connection.execute("SELECT key, value FROM meta")}
        status, cursor = connection.execute("SELECT status, cursor FROM state WHERE id = 1").fetchone()
        ledger = [json.loads(record) for (record,) in connection.execute("SELECT record FROM ledger ORDER BY sequence")]
        events = [(ts, json.loads(raw)) for ts, raw in connection.execute("SELECT timestamp, raw FROM events ORDER BY timestamp")]
        history = list(connection.execute("SELECT timestamp, prev_close FROM history ORDER BY timestamp"))
        log = [dict(zip(("at", "clock", "status", "detail"), row)) for row in
               connection.execute("SELECT at, clock, status, detail FROM status_log ORDER BY id")]
        connection.execute("COMMIT")
    finally:
        connection.close()
    snapshot = SessionSnapshot(path, meta, status, cursor, ledger, events, history, log)
    return snapshot.until(as_of) if as_of is not None else snapshot
