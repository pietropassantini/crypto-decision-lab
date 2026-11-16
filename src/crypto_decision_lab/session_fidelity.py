"""Fidelity check of a paper session against the engine (E05-P1).

The session's own history (``prev_close`` per hour) and processed events (``open`` per hour)
are enough to rebuild the hourly bars it saw: ``close(T) = prev_close(T + 1h)``.  The frozen
candidate is then run over exactly those bars with the experiment-side features
(:func:`daily_trend.daily_features` over the whole frame) and the resulting ledger is compared
with the session's.  A match shows that the runner took the same decisions, orders and fills the
backtest would take on the same data.

The check works on a :class:`~.session_snapshot.SessionSnapshot` (one consistent read-only read of
the session): it never writes, never contacts a data source and never asks for a clock.

A gap is replayed the way the runner handles it: the session stops before the missing bar and, when
the operator acknowledges, logs ``gap_acknowledged`` and resumes; the replay therefore acknowledges the
gap before stepping the first bar after it (no intermediate ``gap`` record).  An acknowledgement already
stored but not yet followed by a bar (the runner has not resumed, or the session was finished right after
it) is replayed from the resume instant the acknowledgement records.

Compared after normalisation: feature values rounded to 12 significant digits (the runner and
the whole-frame computation sum the daily closes in a different order, so the last digit can
differ) and the free-text ``note`` of acknowledged gaps (operator text versus replay text).
Everything else, including decisions, orders, fills, balances and sequence numbers, must be equal.

What the check does NOT prove: it certifies the internal consistency of the session (ledger against the
bars the session itself recorded).  It cannot prove *when* a recorded close became available, nor that the
recorded bars are the market's.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from .daily_trend import daily_features
from .execution_contract import HOUR
from .session_snapshot import SessionSnapshot, read_snapshot
from .simulation import Engine, MarketEvent, market_events
from .studio import build_run_config


def _round_features(features: dict) -> dict:
    return {k: float(f"{v:.12g}") if isinstance(v, float) else v for k, v in features.items()}


def normalized(ledger: list[dict]) -> list[dict]:
    """Ledger without the two differences that are not decisions (see module docstring)."""
    out = []
    for record in ledger:
        record = dict(record)
        if record.get("kind") == "market" and "features" in record:
            record["features"] = _round_features(record["features"])
        if record.get("kind") == "gap_acknowledged":
            record.pop("note", None)
        out.append(record)
    return out


def recorded_frame(snapshot: SessionSnapshot) -> pd.DataFrame:
    """Hourly frame rebuilt from what the session stored: opens of processed bars, closes from ``prev_close``."""
    prev_close = {pd.Timestamp(ts): value for ts, value in snapshot.history}
    opens = {pd.Timestamp(ts): raw["open"] for ts, raw in snapshot.events}
    index = sorted(prev_close)
    close = [prev_close.get(ts + HOUR) for ts in index]
    # Bars before the session start were only needed as feature input: their open is never traded.
    open_ = [opens.get(ts, close_value) for ts, close_value in zip(index, close)]
    frame = pd.DataFrame({"open": open_, "close": close}, index=pd.DatetimeIndex(index))
    frame["high"] = frame["low"] = frame["open"]
    frame["volume"] = 1.0
    return frame


def run_engine(snapshot: SessionSnapshot, config: dict | None = None, *, with_features: bool = True
               ) -> tuple[list[dict], int]:
    """Ledger of ``config`` (default: the session's frozen candidate) over the snapshot's recorded bars.

    The run ends with the engine's finish records only if the snapshot's own ledger has its ``end`` record
    (a completed session).  ``with_features=False`` runs strategies that need nothing beyond
    ``momentum_24h`` (the buy and hold of the P1 report).
    """
    run_config = build_run_config(config or snapshot.meta["config"])
    frame = recorded_frame(snapshot)
    processed = sorted(pd.Timestamp(ts) for ts, _ in snapshot.events)
    if not processed:
        return [], 0
    spec = snapshot.meta.get("features") if with_features else None
    features = daily_features(frame, spec["momentum_days"], spec["sma_days"]) if spec else None
    engine = Engine(run_config)
    ledger: list[dict] = []
    wanted = set(processed)
    events = [e for e in market_events(frame, processed[0], processed[-1] + HOUR) if pd.Timestamp(e.timestamp) in wanted]
    for event in events:
        stamp = pd.Timestamp(event.timestamp)
        if engine.previous is not None and stamp != engine.previous + HOUR:
            ledger.extend(engine.acknowledge_gap(stamp, note="fidelity replay"))  # as the operator does in a session
        if features is not None:
            event = MarketEvent(event.timestamp, event.open, {**event.features, **features[stamp]})
        ledger.extend(engine.step(event))
    # A gap acknowledged after the last processed bar, before the runner resumed (or before the session was
    # finished): the session already holds the acknowledgement; replay it from the resume instant it records.
    last_market = max((i for i, r in enumerate(snapshot.ledger) if r["kind"] == "market"), default=-1)
    pending_ack = [r for r in snapshot.ledger[last_market + 1:] if r["kind"] == "gap_acknowledged"]
    if pending_ack and engine.previous is not None:
        resume = pd.Timestamp(pending_ack[-1]["resumed_at"])
        if resume > engine.previous + HOUR:
            ledger.extend(engine.acknowledge_gap(resume, note="fidelity replay"))
    if any(r["kind"] == "end" for r in snapshot.ledger):
        ledger.extend(engine.finish())
    return ledger, len(events)


def replay(snapshot: SessionSnapshot) -> tuple[list[dict], int]:
    """Ledger of the frozen candidate over the session's recorded bars, and the number of events."""
    return run_engine(snapshot)


def check(session_dir: str | Path | None = None, *, repo_root: str | Path = ".",
          snapshot: SessionSnapshot | None = None) -> dict[str, Any]:
    snapshot = snapshot if snapshot is not None else read_snapshot(session_dir)
    expected, events = replay(snapshot)
    left, right = normalized(snapshot.ledger), normalized(expected)
    first = next((i for i, (a, b) in enumerate(zip(left, right)) if a != b), None)
    if first is None and len(left) != len(right):
        first = min(len(left), len(right))
    return {"match": first is None, "events": events, "session_records": len(left), "replay_records": len(right),
            "first_difference": first,
            "session_record": left[first] if first is not None and first < len(left) else None,
            "replay_record": right[first] if first is not None and first < len(right) else None}


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="E05-P1 fidelity check of a paper session")
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--repo-root", default=".")
    args = parser.parse_args(argv)
    result = check(args.session_dir, repo_root=args.repo_root)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["match"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
