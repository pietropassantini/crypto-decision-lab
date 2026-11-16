"""Read-only, consistent snapshot of a paper session for the E05-P1 reports and checks.

The reports must not open a session the way the runner does: ``PaperSession`` re-checks the pins (it can
change the stored status), opens the data source and asks it for the clock (network).  A snapshot reads
everything the reports need -- metadata, state, ledger, events, history, status log -- in **one read
transaction** of a read-only connection, so:

* nothing in the session database is written;
* no data source is contacted;
* the pieces are consistent with one another even while the runner is committing events.

``until(instant)`` / ``read_snapshot(as_of=...)`` cut every piece at an instant, so a report for a past
instant never shows data from after it.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import sqlite3

import pandas as pd

GAP_RECORDS = ("gap_acknowledged", "order_cancelled")


def _utc(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


@dataclass(frozen=True)
class SessionSnapshot:
    directory: Path
    meta: dict
    status: str
    cursor: str | None
    ledger: list[dict]
    events: list[tuple[str, dict]]          # (bar timestamp, raw feed values) of the processed bars
    history: list[tuple[str, float | None]]  # (timestamp, prev_close): warmup and processed bars
    status_log: list[dict]                   # at (wall clock), clock, status, detail
    as_of: pd.Timestamp | None = None

    def until(self, instant) -> "SessionSnapshot":
        """The session as it was at ``instant`` (included): every piece cut there.

        Records that the runner writes when it resumes after an acknowledged gap belong to the first bar
        after the gap: a cut that falls inside the gap drops them, as the engine cannot replay them without
        that bar.
        """
        cut = _utc(instant)
        ledger = [r for r in self.ledger if _utc(r["timestamp"]) <= cut]
        last_market = max((i for i, r in enumerate(ledger) if r["kind"] == "market"), default=-1)
        while len(ledger) > last_market + 1 and ledger[-1]["kind"] in GAP_RECORDS:
            ledger.pop()
        return replace(
            self, ledger=ledger, as_of=cut,
            events=[(ts, raw) for ts, raw in self.events if _utc(ts) <= cut],
            history=[(ts, value) for ts, value in self.history if _utc(ts) <= cut],
            status_log=[row for row in self.status_log if _utc(row["at"]) <= cut])


def read_snapshot(directory: str | Path, *, as_of=None) -> SessionSnapshot:
    """One consistent read of the session, cut at ``as_of`` when given (a report for a past instant)."""
    path = Path(directory)
    database = (path / "session.sqlite").resolve()
    if not database.is_file():
        raise FileNotFoundError(f"session database not found: {database}")
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=30, isolation_level=None)
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
