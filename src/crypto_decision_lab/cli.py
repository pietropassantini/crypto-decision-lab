"""Command line: reproducible ingestion with atomic output publishing."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

import pandas as pd
from .market import fetch_klines, utc_ms
from .forward_isolation import guard_research_output
from .quality import check_quality


def execute(args) -> int:
    start_ms, end_ms = utc_ms(args.start), utc_ms(args.end)
    if start_ms >= end_ms:
        raise ValueError("start must precede end")
    root = Path(args.output_dir)
    # S02-CP08-05: a research ingest must never write inside the forward archive.
    guard_research_output(root, purpose="research ingest output")
    target = root / "processed" / f"{args.symbol}_1h_{args.start[:10]}_{args.end[:10]}.parquet"
    report = root / "reports" / (target.stem + ".json")
    if (target.exists() or report.exists()) and not args.force:
        raise FileExistsError(f"Output exists; use --force to replace: {target}")
    df = fetch_klines(start_ms, end_ms, symbol=args.symbol)
    qc = check_quality(df, start_ms, end_ms)
    metadata = {"schema_version": 1, "provider": "binance-spot", "symbol": args.symbol, "interval": "1h", "collected_utc": datetime.now(timezone.utc).isoformat(), "quality": qc}
    report.parent.mkdir(parents=True, exist_ok=True)
    if not qc["ok"]:
        report.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(qc, indent=2), file=sys.stderr)
        return 2
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(".parquet.tmp")
    try:
        df.to_parquet(temp, index=False, engine="pyarrow", compression="zstd")
        reread = pd.read_parquet(temp)
        if not check_quality(reread, start_ms, end_ms)["ok"]:
            raise RuntimeError("Round-trip Parquet quality failed")
        metadata["parquet_sha256"] = hashlib.sha256(temp.read_bytes()).hexdigest()
        os.replace(temp, target)
        report.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    finally:
        temp.unlink(missing_ok=True)
    print(json.dumps({"file": str(target), "report": str(report), **metadata}, indent=2))
    return 0


def main(argv=None):
    tokens = list(sys.argv[1:]) if argv is None else list(argv)
    parser = argparse.ArgumentParser(description="Crypto Decision Lab: verified Binance Spot hourly OHLCV")
    sub = parser.add_subparsers(dest="command", required=True)
    ingest = sub.add_parser("ingest", help="download closed hourly candles and validate")
    ingest.add_argument("--symbol", default="BTCUSDT")
    ingest.add_argument("--start", required=True, help="ISO UTC, inclusive, e.g. 2024-01-01T00:00:00Z")
    ingest.add_argument("--end", required=True, help="ISO UTC, exclusive, must be closed hour")
    ingest.add_argument("--output-dir", default="data")
    ingest.add_argument("--force", action="store_true")
    sync_cmd = sub.add_parser("sync", help="resumable monthly chunks with checksums")
    sync_cmd.add_argument("--symbol", default="BTCUSDT")
    sync_cmd.add_argument("--start", required=True)
    sync_cmd.add_argument("--end", required=True)
    sync_cmd.add_argument("--output-dir", default="data")
    sync_cmd.add_argument("--mode", choices=("strict", "verified", "audit"), default="verified",
                          help="strict: zero anomalies; verified: known exceptions; audit: quarantine unknown anomalies")
    fr = sub.add_parser("freeze", help="verify audit manifest and export read-only RAW snapshot and safe forecast windows")
    fr.add_argument("--manifest", required=True)
    fr.add_argument("--output-dir", default="data")
    fr.add_argument("--snapshot-dir", required=True)
    fr.add_argument("--lookback", type=int, default=168)
    fr.add_argument("--horizon", type=int, default=24)
    fr.add_argument("--stride", type=int, default=24)
    ds = sub.add_parser("build-dataset", help="S02: build a versioned research dataset from a catalog source")
    ds.add_argument("--catalog", default="config/dataset-catalog.json")
    ds.add_argument("--source", required=True, help="admitted catalog dataset_id")
    ds.add_argument("--repo-root", default=".")
    ds.add_argument("--data-root", default=None)
    ds.add_argument("--output-dir", default=None)
    ds.add_argument("--dataset-id", default="BTCUSDT-1h-research")
    ds.add_argument("--version", default="v1")
    ds.add_argument("--stride-hours", type=int, default=24)
    ds.add_argument("--warmup-hours", type=int, default=168)
    ds.add_argument("--latency-hours", type=int, default=0)
    ds.add_argument("--horizon-hours", type=int, default=24)
    ds.add_argument("--fee-rate", type=float, default=0.001)
    ds.add_argument("--slippage", type=float, default=0.0005)
    ds.add_argument("--no-write", action="store_true")
    dv = sub.add_parser("verify-dataset", help="S02: recompute dataset digests and compare with the manifest")
    dv.add_argument("--dataset-dir", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "freeze":
            from .freeze import freeze
            print(json.dumps(freeze(args.output_dir, args.manifest, args.snapshot_dir, args.lookback, args.horizon, args.stride), indent=2))
            return 0
        if args.command == "sync":
            from .sync import sync
            print(json.dumps(sync(args.symbol, args.start, args.end, args.output_dir, mode=args.mode), indent=2))
            return 0
        if args.command in ("build-dataset", "verify-dataset"):
            from .dataset_builder import main as dataset_main
            rest = ["build" if args.command == "build-dataset" else "verify"] + tokens[1:]
            return dataset_main(rest)
        return execute(args)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    sys.exit(main())
