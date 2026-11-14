"""S06 sources for paper sessions: reproducible lab feed and snapshot replay.

Both sources expose the same view: ``records()`` maps an hourly timestamp to a
:class:`FeedRecord` holding the open observable at that timestamp and the
close of the previous bar (``prev_close``), so features use closed bars only.

Lab feed
--------
A lab feed is a directory outside the CP08 perimetre (``data/forward`` is
refused) built from a catalog-admitted research snapshot and a scenario::

    feed.json        scenario, scenario digest, source provenance
    clock.json       simulated "now" of the feed
    bars.jsonl       append-only publications (what a session may read)
    .schedule.jsonl  deterministic publication plan (never read by sessions)

The scenario injects faults deterministically: ``late`` (published
``delay_hours`` after the bar), ``invalid`` (open missing, optionally corrected
``correct_after_hours`` later), ``outage`` (nothing published in
``[from, to)``, then backfilled) and ``gap`` (never published).  Publications
carry the planned ``published_at`` and are appended in plan order, so the same
scenario and clock give byte-identical ``bars.jsonl`` whatever the sequence of
``advance`` calls.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from .dataset_catalog import Catalog, assert_research_admissible, load_market
from .forward_isolation import guard_research_output, guard_research_path

FEED_SCHEMA = "cdl.s06.labfeed.v1"
HOUR = pd.Timedelta(hours=1)
FAULT_TYPES = ("late", "invalid", "outage", "gap")


class FeedError(ValueError):
    """Raised for malformed scenarios or feed directories."""


def _utc(value: Any) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _finite_positive(value) -> bool:
    return value is not None and isinstance(value, (int, float)) and math.isfinite(value) and value > 0


@dataclass(frozen=True)
class FeedRecord:
    timestamp: pd.Timestamp
    open: float | None
    prev_close: float | None
    revision: int = 0

    @property
    def problem(self) -> str | None:
        if not _finite_positive(self.open):
            return f"open missing or invalid at {self.timestamp}"
        if self.prev_close is not None and not _finite_positive(self.prev_close):
            return f"prev_close invalid at {self.timestamp}"
        return None

    @property
    def valid(self) -> bool:
        return self.problem is None

    def raw(self) -> dict:
        return {"open": self.open, "prev_close": self.prev_close}


@dataclass
class FeedView:
    records: dict[pd.Timestamp, FeedRecord] = field(default_factory=dict)
    conflicts: list[str] = field(default_factory=list)


def resolve_lines(lines: list[dict]) -> FeedView:
    """Latest valid revision per timestamp; differing valid revisions conflict."""

    grouped: dict[pd.Timestamp, list[FeedRecord]] = {}
    for line in lines:
        record = FeedRecord(_utc(line["timestamp"]), line.get("open"), line.get("prev_close"),
                            int(line.get("revision", 0)))
        grouped.setdefault(record.timestamp, []).append(record)
    view = FeedView()
    for timestamp, versions in sorted(grouped.items()):
        valid = [item for item in versions if item.valid]
        if len({(item.open, item.prev_close) for item in valid}) > 1:
            view.conflicts.append(f"conflicting valid publications for {timestamp}")
        view.records[timestamp] = valid[-1] if valid else versions[-1]
    return view


def _frame_records(frame: pd.DataFrame, start=None, end=None) -> list[FeedRecord]:
    close = frame["close"].astype(float)
    selected = frame
    if start is not None:
        selected = selected[selected.index >= _utc(start)]
    if end is not None:
        selected = selected[selected.index < _utc(end)]
    records = []
    for timestamp, row in selected.iterrows():
        before = timestamp - HOUR
        prev_close = float(close.loc[before]) if before in close.index else None
        records.append(FeedRecord(timestamp, float(row["open"]), prev_close))
    return records


# ------------------------------------------------------------------ snapshot replay
class SnapshotSource:
    """All bars of an admitted research snapshot are available at once (replay)."""

    kind = "snapshot"

    def __init__(self, dataset_id: str, *, repo_root: str | Path = ".",
                 catalog_path: str | Path = "config/dataset-catalog.json") -> None:
        root = Path(repo_root).resolve()
        catalog_file = Path(catalog_path) if Path(catalog_path).is_absolute() else root / catalog_path
        self.dataset_id = dataset_id
        self.market = load_market(Catalog.load(catalog_file), dataset_id, repo_root=root)
        self._view = FeedView({record.timestamp: record for record in _frame_records(self.market.frame)})

    def records(self) -> FeedView:
        return self._view

    def now(self) -> pd.Timestamp | None:
        return None

    def identity(self) -> dict:
        return {"kind": self.kind, "dataset_id": self.dataset_id,
                "source_manifest_sha256": self.market.source_manifest_sha256}


# ------------------------------------------------------------------ lab feed
def validate_scenario(scenario: Mapping[str, Any]) -> None:
    if scenario.get("schema_version") != FEED_SCHEMA:
        raise FeedError(f"unsupported lab feed schema: {scenario.get('schema_version')}")
    for key in ("feed_id", "source_dataset_id", "start", "end"):
        if not scenario.get(key):
            raise FeedError(f"scenario requires {key}")
    if _utc(scenario["start"]) >= _utc(scenario["end"]):
        raise FeedError("scenario start must precede end")
    for fault in scenario.get("faults", []):
        if fault.get("type") not in FAULT_TYPES:
            raise FeedError(f"unknown fault type: {fault.get('type')}")


def publication_plan(records: list[FeedRecord], faults: list[Mapping[str, Any]]) -> list[dict]:
    """Deterministic list of publications ordered by (published_at, timestamp, revision)."""

    by_type: dict[str, dict[pd.Timestamp, Mapping]] = {kind: {} for kind in ("late", "invalid", "gap")}
    outages = []
    for fault in faults:
        if fault["type"] == "outage":
            outages.append((_utc(fault["from"]), _utc(fault["to"])))
        else:
            by_type[fault["type"]][_utc(fault["at"])] = fault
    plan = []

    def publish_at(when: pd.Timestamp) -> pd.Timestamp:
        for start, stop in outages:
            if start <= when < stop:
                return stop
        return when

    for record in records:
        ts = record.timestamp
        if ts in by_type["gap"]:
            continue
        base = {"timestamp": str(ts), "open": record.open, "prev_close": record.prev_close, "revision": 0}
        if ts in by_type["invalid"]:
            fault = by_type["invalid"][ts]
            plan.append({**base, "open": None, "published_at": str(publish_at(ts))})
            if fault.get("correct_after_hours"):
                when = ts + int(fault["correct_after_hours"]) * HOUR
                plan.append({**base, "revision": 1, "published_at": str(publish_at(when))})
            continue
        delay = int(by_type["late"][ts]["delay_hours"]) if ts in by_type["late"] else 0
        plan.append({**base, "published_at": str(publish_at(ts + delay * HOUR))})
    plan.sort(key=lambda item: (_utc(item["published_at"]), _utc(item["timestamp"]), item["revision"]))
    return plan


def _dump(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


class LabFeed:
    kind = "lab_feed"

    def __init__(self, directory: Path) -> None:
        self.directory = guard_research_path(directory, purpose="lab feed")
        manifest_path = self.directory / "feed.json"
        if not manifest_path.is_file():
            raise FeedError(f"not a lab feed directory: {directory}")
        self.manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if self.manifest.get("schema_version") != FEED_SCHEMA:
            raise FeedError("unsupported lab feed manifest")

    # -- creation
    @classmethod
    def create(cls, directory: str | Path, scenario: Mapping[str, Any], *, repo_root: str | Path = ".",
               catalog_path: str | Path = "config/dataset-catalog.json") -> "LabFeed":
        validate_scenario(scenario)
        root = Path(repo_root).resolve()
        target = Path(directory)
        target = guard_research_output(target if target.is_absolute() else root / target, purpose="lab feed")
        if target.exists():
            raise FileExistsError(f"lab feed exists; refusing to overwrite: {target}")
        catalog_file = Path(catalog_path) if Path(catalog_path).is_absolute() else root / catalog_path
        catalog = Catalog.load(catalog_file)
        assert_research_admissible(catalog, scenario["source_dataset_id"], repo_root=root)
        market = load_market(catalog, scenario["source_dataset_id"], repo_root=root)
        records = _frame_records(market.frame, scenario["start"], scenario["end"])
        if not records:
            raise FeedError("scenario window contains no source bars")
        plan = publication_plan(records, list(scenario.get("faults", [])))
        target.mkdir(parents=True)
        (target / ".schedule.jsonl").write_text("".join(_dump(item) + "\n" for item in plan), encoding="utf-8")
        (target / "bars.jsonl").write_text("", encoding="utf-8")
        start = _utc(scenario["start"])
        (target / "clock.json").write_text(_dump({"now": str(start - HOUR)}) + "\n", encoding="utf-8")
        manifest = {
            "schema_version": FEED_SCHEMA,
            "feed_id": scenario["feed_id"],
            "scenario": dict(scenario),
            "scenario_sha256": hashlib.sha256(_dump(dict(scenario)).encode()).hexdigest(),
            "source": {"dataset_id": market.dataset_id, "source_manifest_sha256": market.source_manifest_sha256,
                       "catalog_role": "research"},
            "perimeter": "lab feed outside CP08: built from an admitted research snapshot, never from data/forward",
            "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        (target / "feed.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return cls(target)

    # -- clock and publication (the simulated exchange side)
    def now(self) -> pd.Timestamp:
        return _utc(json.loads((self.directory / "clock.json").read_text(encoding="utf-8"))["now"])

    def advance(self, hours: int | None = None, *, until: Any = None) -> int:
        current = self.now()
        target = _utc(until) if until is not None else current + int(hours or 0) * HOUR
        if target < current:
            raise FeedError("the lab clock cannot go backwards")
        published = {(item["timestamp"], item["revision"]) for item in self._lines()}
        fresh = []
        for line in (self.directory / ".schedule.jsonl").read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if _utc(item["published_at"]) <= target and (item["timestamp"], item["revision"]) not in published:
                fresh.append(item)
        with (self.directory / "bars.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
            for item in fresh:
                handle.write(_dump(item) + "\n")
            handle.flush()
        (self.directory / "clock.json").write_text(_dump({"now": str(target)}) + "\n", encoding="utf-8")
        return len(fresh)

    # -- reading (the session side)
    def _lines(self) -> list[dict]:
        lines = []
        for raw in (self.directory / "bars.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                lines.append(json.loads(raw))
            except json.JSONDecodeError:
                continue  # torn last line of an interrupted append
        return lines

    def records(self) -> FeedView:
        return resolve_lines(self._lines())

    def identity(self) -> dict:
        # Content identity only: moving the checkout does not change the feed.
        return {"kind": self.kind, "feed_id": self.manifest["feed_id"],
                "scenario_sha256": self.manifest["scenario_sha256"], "source": self.manifest["source"]}

    @property
    def start(self) -> pd.Timestamp:
        return _utc(self.manifest["scenario"]["start"])

    @property
    def end(self) -> pd.Timestamp:
        return _utc(self.manifest["scenario"]["end"])


__all__ = ["FEED_SCHEMA", "FeedError", "FeedRecord", "FeedView", "LabFeed", "SnapshotSource",
           "publication_plan", "resolve_lines", "validate_scenario"]
