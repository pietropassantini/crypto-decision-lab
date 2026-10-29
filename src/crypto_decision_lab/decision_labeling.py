"""S02-CP07 STEP 3: retrospective LONG/SHORT/NO_TRADE labels per window.

Labels use the realized exit open and therefore contain future information by
construction: they are analysis targets only and must never be used as policy
features (enforced by the policy module, which never reads this dataset's
action/margin columns when deciding).
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .decision_contract import (
    ACTION_LONG,
    ACTION_NO_TRADE,
    ACTION_SHORT,
    CostModel,
    DEFAULT_AMBIGUITY_MARGIN,
    LABEL_SCHEMA,
    DECISION_SCHEMA,
    HORIZON_HOURS,
    now_iso,
    utc,
)
from .sync import digest
from .trading_simulator import DEFAULT_INITIAL_CAPITAL  # re-exported for docs


@dataclass(frozen=True)
class LabelPaths:
    data_root: Path = Path("data")
    snapshot_dir: Path = Path("data/snapshots/BTCUSDT-1h-raw-v1")
    expansion_dir: Path = Path("data/experiments/S02-CP06A-historical-expansion-v1")
    output_dir: Path = Path("data/datasets/S02-CP07-decision-labels-v1")


class LabelingError(ValueError):
    pass


def build_labels(
    canonical: pd.DataFrame,
    market_open: pd.Series,
    *,
    costs: CostModel,
    ambiguity_margin: float = DEFAULT_AMBIGUITY_MARGIN,
) -> pd.DataFrame:
    """Compute retrospective net returns of LONG/SHORT/NO_TRADE per window."""

    cutoffs = pd.to_datetime(canonical["cutoff"], utc=True)
    entry_open = market_open.reindex(cutoffs).to_numpy(dtype=float)
    exit_open = market_open.reindex(cutoffs + pd.Timedelta(hours=HORIZON_HOURS)).to_numpy(dtype=float)
    if not np.isfinite(entry_open).all() or not np.isfinite(exit_open).all():
        raise LabelingError("Missing entry/exit open bars for some windows")
    reference = canonical["anchor_price"].to_numpy(dtype=float)
    from .decision_contract import gross_return, net_return

    long_net = np.array(
        [net_return(e, x, ACTION_LONG, costs) for e, x in zip(entry_open, exit_open)]
    )
    short_net = np.array(
        [net_return(e, x, ACTION_SHORT, costs) for e, x in zip(entry_open, exit_open)]
    )
    long_gross = np.array([gross_return(e, x, ACTION_LONG) for e, x in zip(entry_open, exit_open)])
    short_gross = np.array([gross_return(e, x, ACTION_SHORT) for e, x in zip(entry_open, exit_open)])
    no_trade = np.zeros(len(canonical))
    returns = np.vstack([long_net, short_net, no_trade])
    best_index = returns.argmax(axis=0)
    actions = np.array([ACTION_LONG, ACTION_SHORT, ACTION_NO_TRADE])[best_index]
    sorted_returns = np.sort(returns, axis=0)
    margin = sorted_returns[-1] - sorted_returns[-2]
    frame = pd.DataFrame(
        {
            "schema_version": LABEL_SCHEMA,
            "window_index": canonical["window_index"].to_numpy(dtype=int),
            "cutoff": cutoffs,
            "entry_timestamp": cutoffs,
            "exit_timestamp": cutoffs + pd.Timedelta(hours=HORIZON_HOURS),
            "horizon_hours": HORIZON_HOURS,
            "reference_price": reference,
            "entry_open": entry_open,
            "exit_open": exit_open,
            "long_net_pct": 100.0 * long_net,
            "short_net_pct": 100.0 * short_net,
            "no_trade_net_pct": 0.0,
            "long_gross_pct": 100.0 * long_gross,
            "short_gross_pct": 100.0 * short_gross,
            "best_action": actions,
            "best_net_pct": 100.0 * returns.max(axis=0),
            "margin_vs_second_pct": 100.0 * margin,
            "ambiguous": margin < float(ambiguity_margin),
            "fee_rate": costs.fee_rate,
            "slippage": costs.slippage,
            "ambiguity_margin_pct": 100.0 * float(ambiguity_margin),
        }
    )
    return frame.sort_values("cutoff").reset_index(drop=True)


def label_summary(labels: pd.DataFrame) -> dict[str, Any]:
    counts = labels["best_action"].value_counts().to_dict()
    return {
        "windows": int(len(labels)),
        "best_action_counts": {key: int(value) for key, value in counts.items()},
        "ambiguous_windows": int(labels["ambiguous"].sum()),
        "ambiguous_pct": 100.0 * float(labels["ambiguous"].mean()),
        "long_net_mean_pct": float(labels["long_net_pct"].mean()),
        "short_net_mean_pct": float(labels["short_net_pct"].mean()),
        "margin_mean_pct": float(labels["margin_vs_second_pct"].mean()),
        "by_year": [
            {
                "year": int(year),
                "windows": int(len(group)),
                "long_net_mean_pct": float(group["long_net_pct"].mean()),
                "short_net_mean_pct": float(group["short_net_pct"].mean()),
                "best_long": int((group["best_action"] == ACTION_LONG).sum()),
                "best_short": int((group["best_action"] == ACTION_SHORT).sum()),
                "best_no_trade": int((group["best_action"] == ACTION_NO_TRADE).sum()),
            }
            for year, group in labels.groupby(labels["cutoff"].dt.year)
        ],
        "warning": "Retrospective labels use future outcomes; never use best_action or margins as model features.",
    }


def _source_hashes(paths: LabelPaths) -> dict[str, str]:
    files = {
        "snapshot_raw_manifest": paths.snapshot_dir / "raw-manifest.json",
        "snapshot_report": paths.snapshot_dir / "report.json",
        "expansion_manifest": paths.expansion_dir / "manifest.json",
        "expansion_canonical": paths.expansion_dir / "canonical_predictions.parquet",
    }
    return {name: digest(path) for name, path in files.items() if path.is_file()}


def run(
    paths: LabelPaths = LabelPaths(),
    *,
    costs: CostModel | None = None,
    ambiguity_margin: float = DEFAULT_AMBIGUITY_MARGIN,
    write: bool = True,
) -> dict[str, Any]:
    from .dashboard.data_loader import load_market_data
    from .regime_analysis import load_canonical

    costs = costs or CostModel()
    canonical = load_canonical(paths.expansion_dir)
    market = load_market_data(paths.data_root, paths.snapshot_dir, verify_checksums=True).frame
    labels = build_labels(
        canonical,
        market["open"],
        costs=costs,
        ambiguity_margin=ambiguity_margin,
    )
    summary = {
        "schema_version": LABEL_SCHEMA,
        "decision_schema": DECISION_SCHEMA,
        "generated_utc": now_iso(),
        "cost_model": {"fee_rate": costs.fee_rate, "slippage": costs.slippage},
        **label_summary(labels),
    }
    if write:
        _write_dataset(paths, labels, summary)
    summary["labels_preview"] = labels.head(5).to_dict(orient="records")
    return summary


def _write_dataset(paths: LabelPaths, labels: pd.DataFrame, summary: dict[str, Any]) -> None:
    output = paths.output_dir
    if output.exists():
        raise FileExistsError(f"Label dataset already exists; refusing to overwrite: {output}")
    before = _source_hashes(paths)
    output.mkdir(parents=True)
    try:
        labels.to_parquet(output / "labels.parquet", index=False)
        summary["labels_sha256"] = digest(output / "labels.parquet")
        (output / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
        after = _source_hashes(paths)
        manifest = {
            "schema_version": LABEL_SCHEMA,
            "created_utc": now_iso(),
            "source_hashes": before,
            "source_hashes_after": after,
            "source_hashes_unchanged": before == after,
            "original_artifacts_modified": before != after,
            "outputs": {"labels": {"path": "labels.parquet", "sha256": summary["labels_sha256"]}},
            "execution_semantics": {
                "entry": "open of the bar at cutoff (first price after the anchor close)",
                "exit": f"open of the bar at cutoff + {HORIZON_HOURS}h",
                "reference": "anchor close at cutoff - 1h (the price used to generate the decision)",
                "no_lookahead": "entry bar is strictly after the bar whose close generated the decision",
            },
        }
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8")
        if before != after:
            raise LabelingError("Source artifacts changed while writing labels")
    except BaseException:
        import shutil

        shutil.rmtree(output, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S02-CP07 retrospective decision labels")
    parser.add_argument("--data-root", default=str(LabelPaths.data_root))
    parser.add_argument("--snapshot-dir", default=str(LabelPaths.snapshot_dir))
    parser.add_argument("--expansion-dir", default=str(LabelPaths.expansion_dir))
    parser.add_argument("--output-dir", default=str(LabelPaths.output_dir))
    parser.add_argument("--fee-rate", type=float, default=CostModel().fee_rate)
    parser.add_argument("--slippage", type=float, default=CostModel().slippage)
    parser.add_argument("--ambiguity-margin", type=float, default=DEFAULT_AMBIGUITY_MARGIN)
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)
    paths = LabelPaths(
        Path(args.data_root), Path(args.snapshot_dir), Path(args.expansion_dir), Path(args.output_dir)
    )
    summary = run(
        paths,
        costs=CostModel(fee_rate=args.fee_rate, slippage=args.slippage),
        ambiguity_margin=args.ambiguity_margin,
        write=not args.no_write,
    )
    payload = {key: value for key, value in summary.items() if key != "labels_preview"}
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
