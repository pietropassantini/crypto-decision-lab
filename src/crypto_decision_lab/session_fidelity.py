"""Fidelity check of a paper session against the engine (E05-P1, week-12 criterion).

The session's own history (``prev_close`` per hour) and processed events (``open`` per hour)
are enough to rebuild the hourly bars it saw: ``close(T) = prev_close(T + 1h)``.  The frozen
candidate is then run over exactly those bars with the experiment-side features
(:func:`daily_trend.daily_features` over the whole frame) and the resulting ledger is compared
with the session's.  A match shows that the runner took the same decisions, orders and fills the
backtest would take on the same data.

Compared after normalisation: feature values rounded to 12 significant digits (the runner and
the whole-frame computation sum the daily closes in a different order, so the last digit can
differ) and the free-text ``note`` of acknowledged gaps (operator text versus experiment text).
Everything else, including decisions, orders, fills, balances and sequence numbers, must be equal.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

from .daily_trend import daily_features
from .execution_contract import HOUR
from .paper import PaperSession
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


def recorded_frame(session: PaperSession) -> pd.DataFrame:
    """Hourly frame rebuilt from what the session stored: opens of processed bars, closes from ``prev_close``."""
    prev_close = {pd.Timestamp(ts): value for ts, value in
                  session.connection.execute("SELECT timestamp, prev_close FROM history ORDER BY timestamp")}
    opens = {pd.Timestamp(ts): json.loads(raw)["open"] for ts, raw in
             session.connection.execute("SELECT timestamp, raw FROM events ORDER BY timestamp")}
    index = sorted(prev_close)
    close = [prev_close.get(ts + HOUR) for ts in index]
    # Bars before the session start were only needed as feature input: their open is never traded.
    open_ = [opens.get(ts, close_value) for ts, close_value in zip(index, close)]
    frame = pd.DataFrame({"open": open_, "close": close}, index=pd.DatetimeIndex(index))
    frame["high"] = frame["low"] = frame["open"]
    frame["volume"] = 1.0
    return frame


def run_engine(session: PaperSession, config: dict | None = None, *, with_features: bool = True,
               until: pd.Timestamp | None = None) -> tuple[list[dict], int]:
    """Ledger of ``config`` (default: the session's frozen candidate) over the session's recorded bars.

    The same bars, gap rule and finishing as the session: the ledger ends with the engine's finish
    records only if the session itself is COMPLETED.  ``with_features=False`` runs strategies that need
    nothing beyond ``momentum_24h`` (the buy and hold of the P1 report).  ``until`` keeps only the bars up to
    and including that instant and never finishes the run (evaluation as of a past instant).
    """
    run_config = build_run_config(config or session.meta["config"])
    frame = recorded_frame(session)
    processed = sorted(pd.Timestamp(ts) for (ts,) in session.connection.execute("SELECT timestamp FROM events"))
    if until is not None:
        processed = [ts for ts in processed if ts <= until]
    if not processed:
        return [], 0
    spec = session.meta.get("features") if with_features else None
    features = daily_features(frame, spec["momentum_days"], spec["sma_days"]) if spec else None
    engine = Engine(run_config)
    ledger: list[dict] = []
    wanted = set(processed)
    events = [e for e in market_events(frame, processed[0], processed[-1] + HOUR) if pd.Timestamp(e.timestamp) in wanted]
    for event in events:
        if features is not None:
            event = MarketEvent(event.timestamp, event.open, {**event.features, **features[pd.Timestamp(event.timestamp)]})
        records = engine.step(event)
        if engine.status == "HALTED_GAP":
            ledger.extend(records)
            ledger.extend(engine.acknowledge_gap(event.timestamp, note="fidelity replay"))
            records = engine.step(event)
        ledger.extend(records)
    if until is None and session.snapshot()["status"] == "COMPLETED":
        ledger.extend(engine.finish())
    return ledger, len(events)


def replay(session: PaperSession) -> tuple[list[dict], int]:
    """Ledger of the frozen candidate over the session's recorded bars, and the number of events."""
    return run_engine(session)


def check(session_dir: str | Path, *, repo_root: str | Path = ".") -> dict[str, Any]:
    session = PaperSession(Path(session_dir), repo_root=repo_root)
    try:
        expected, events = replay(session)
        actual = session.ledger()
    finally:
        session.close()
    left, right = normalized(actual), normalized(expected)
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
