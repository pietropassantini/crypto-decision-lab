"""S02-CP08-07: read-only, fail-closed forward dataset quality monitor.

Run: python -m crypto_decision_lab.forward_monitoring --root data/forward/BTCUSDT-1h-forward-v1
Optional --report writes a JSON report outside the dataset.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from .forward_collection import (
    BASE_COLUMNS, DATASET_SCHEMA, _canonical_json, _hash_obj,
    data_sha256, digest_bytes, record_chain_sha256,
)

HOUR = pd.Timedelta(hours=1)
HOUR_MS = 3_600_000


def audit(root: Path, *, now: datetime | None = None, max_lag_hours: float = 2.0) -> dict:
    """Evaluate a snapshot. No writes, network, collector, or strategy calls."""
    root = Path(root)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    checks: list[dict] = []
    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "status": "PASS" if bool(ok) else "FAIL", "detail": detail})

    report = {"schema_version": "cdl.cp08.forward_monitoring.v1", "checked_utc": now.isoformat(),
              "dataset_root": str(root), "checks": checks, "metrics": {}, "gate": "FAIL"}
    try:
        latest_file = root / "manifests/latest.json"
        latest = json.loads(latest_file.read_text(encoding="utf-8"))
        name = latest["manifest"]
        if Path(name).name != name or not name.startswith("dataset-manifest-") or not name.endswith(".json"):
            raise ValueError("unsafe/invalid manifest filename")
        manifest_file = root / "manifests" / name
        raw = manifest_file.read_bytes()
        manifest = json.loads(raw)
        check("LATEST_HASH", sha256(raw).hexdigest() == latest["sha256"])
        check("MANIFEST_VERSION", name == f"dataset-manifest-{manifest['manifest_version']:04d}.json")
        check("DATASET_ID", manifest["dataset_id"] == root.name)
        check("DATASET_ROLE", manifest.get("dataset_role") == "independent_forward_collection")
        check("FORWARD_ONLY", manifest.get("research_use_forbidden") is True and
              manifest.get("evaluation_executed") is False and
              manifest.get("strategy_metrics_present") is False and
              manifest.get("final_evaluation_gate") == "NOT_ACTIVATED")
        check("NO_CONFLICTS", not manifest.get("conflicts"))
        check("NO_QUARANTINE", not manifest.get("quarantine"))
        check("NO_DECLARED_GAPS", not manifest.get("gaps") and manifest.get("missing_hours") == 0)
        check("NO_DECLARED_DUPLICATES", manifest.get("duplicate_timestamps") == 0)
        chunks = manifest["chunks"]
        if not chunks:
            raise ValueError("manifest has no active chunks")
        checksums = []
        frames = []
        previous_end = None
        for i, chunk in enumerate(chunks):
            cid = chunk["chunk_id"]
            if Path(cid).name != cid or '/' in cid or '\\' in cid:
                raise ValueError(f"unsafe chunk id at index {i}")
            p = root / "chunks" / f"{cid}.parquet"
            mp = root / "chunks" / f"{cid}.json"
            meta = json.loads(mp.read_text(encoding="utf-8"))
            frame = pd.read_parquet(p)
            prefix = f"CHUNK[{i}]"
            check(prefix + ".FILE_HASH", sha256(p.read_bytes()).hexdigest() == chunk["sha256"])
            check(prefix + ".METADATA", meta["sha256"] == chunk["sha256"] and
                  meta["data_sha256"] == chunk["data_sha256"] and
                  meta["chunk_id"] == cid and meta["rows"] == chunk["rows"])
            check(prefix + ".STATUS", chunk["status"] == "VALID" and meta["status"] == "VALID")
            check(prefix + ".ROWS", len(frame) == chunk["rows"])
            start = pd.Timestamp(chunk["start_utc"])
            end = pd.Timestamp(chunk["end_utc"])
            check(prefix + ".SPAN", start.tzinfo is not None and end > start and
                  end-start == len(frame)*HOUR)
            if previous_end is not None:
                check(prefix + ".CONTIGUITY", previous_end == start)
            previous_end = end
            check(prefix + ".DATA_HASH", data_sha256(frame) == chunk["data_sha256"])
            check(prefix + ".RECORD_CHAIN", record_chain_sha256(frame) == meta["record_chain_sha256"])
            rows = frame[BASE_COLUMNS].to_dict(orient="records")
            expected = [digest_bytes(_canonical_json({"schema_version": DATASET_SCHEMA, "row": row}).encode("utf-8")) for row in rows]
            check(prefix + ".RECORD_HASHES", expected == frame["record_checksum"].tolist())
            checksums.append([cid, chunk["data_sha256"]])
            frames.append(frame)
        check("CHUNK_CHAIN", _hash_obj({"chain": [h for _, h in checksums]}) == manifest["chunk_chain_sha256"])
        check("DATASET_HASH", _hash_obj({"dataset_id": manifest["dataset_id"],
                                       "schema_version": DATASET_SCHEMA, "chunks": checksums}) == manifest["dataset_sha256"])
        df = pd.concat(frames, ignore_index=True).sort_values("timestamp", kind="stable")
        ts = pd.to_datetime(df["timestamp"], utc=True)
        n = len(df)
        cov = manifest["coverage"]
        first = pd.Timestamp(cov["start_utc"])
        end_ex = pd.Timestamp(cov["end_exclusive_utc"])
        check("COVERAGE", n == cov["rows"] == cov["hours"] and
              ts.iloc[0] == first == pd.Timestamp(manifest["boundary_utc"]) and
              ts.iloc[-1] + HOUR == end_ex and
              int(first.timestamp() * 1000) == manifest["coverage_start_ms"] and
              int(end_ex.timestamp() * 1000) == manifest["coverage_end_ms"])
        check("TIMESTAMPS", ts.notna().all() and ts.is_unique and
              ts.diff().iloc[1:].eq(HOUR).all())
        check("OPEN_TIME", df["open_time"].eq(ts.astype("int64") // 1_000_000).all())
        check("CLOSE_TIME", df["close_time"].eq(df["open_time"] + HOUR_MS - 1).all())
        prices = df[["open", "high", "low", "close"]]
        check("PRICE_FINITE_POSITIVE", np.isfinite(prices.to_numpy(dtype=float)).all() and
              prices.gt(0).all().all())
        check("OHLC", df["low"].le(prices[["open", "close", "high"]].min(axis=1)).all() and
              df["high"].ge(prices[["open", "close", "low"]].max(axis=1)).all())
        amounts = df[["volume", "quote_volume", "taker_buy_base", "taker_buy_quote", "trades"]]
        check("VOLUMES_AND_TRADES", np.isfinite(amounts.to_numpy(dtype=float)).all() and
              amounts.ge(0).all().all())
        check("SYMBOL_SOURCE", df["symbol"].eq(manifest["symbol"]).all() and
              df["source_id"].eq("binance-spot").all())
        # Open timestamp denotes candle start: end-exclusive is the latest fully covered boundary.
        # A negative lag is an error (future data), and the limit is user-configurable.
        lag_hours = (pd.Timestamp(now) - end_ex).total_seconds() / 3600
        report["metrics"] = {"rows": n, "chunks": len(chunks), "manifest": name,
                              "coverage_end_exclusive_utc": end_ex.isoformat(),
                              "lag_hours": round(lag_hours, 4)}
        check("FRESHNESS", 0 <= lag_hours <= max_lag_hours, f"lag_hours={lag_hours:.3f}, limit={max_lag_hours}")
        # Fail if the collector advanced the latest pointer while we audited.
        check("SNAPSHOT_STABLE", json.loads(latest_file.read_text(encoding="utf-8")) == latest)
    except Exception as exc:
        check("AUDIT_EXCEPTION", False, f"{type(exc).__name__}: {exc}")
    report["gate"] = "PASS" if all(c["status"] == "PASS" for c in checks) else "FAIL"
    report["failures"] = [c for c in checks if c["status"] == "FAIL"]
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, required=True)
    ap.add_argument("--max-lag-hours", type=float, default=2.0)
    ap.add_argument("--report", type=Path, help="Optional JSON path outside dataset")
    args = ap.parse_args(argv)
    if args.max_lag_hours <= 0:
        ap.error("--max-lag-hours must be positive")
    result = audit(args.root, max_lag_hours=args.max_lag_hours)
    output = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    print(output, end="")
    if args.report:
        target = args.report.resolve()
        dataset = args.root.resolve()
        if target == dataset or dataset in target.parents:
            ap.error("--report must be outside the dataset")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(output, encoding="utf-8")
    return 0 if result["gate"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
