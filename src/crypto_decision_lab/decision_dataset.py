"""S02-CP09: decision dataset with Timestral uncertainty features and continuous targets.

Contracts
---------
* ``features.parquet`` holds only information available at the cutoff:
  causal regime indicators (stationary ones only, no USDT price levels) and
  quantities derived from the Timestral forecast path (p10/p50/p90), which is
  itself produced from bars strictly before the cutoff.
* ``targets.parquet`` holds realized outcomes of the 24h holding period. It
  contains future information by construction and must never be joined to
  inference inputs.
* :func:`build_features` never receives realized prices: the ``actual`` column
  of the archived forecasts is dropped before the call, and the function
  rejects frames that still carry it.
* Units: columns ending in ``_pct`` are percent (5.0 = 5%); ``volatility_*``
  are hourly log-return standard deviations (decimal), as in S02-CP06B.

Execution semantics follow the S02-CP07 decision contract: entry at the open
of the bar at ``cutoff``, exit at the open of the bar at ``cutoff + 24h``.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import shutil
from typing import Any

import numpy as np
import pandas as pd

from .decision_contract import HORIZON_HOURS, now_iso
from .sync import digest

DATASET_SCHEMA = "cdl.cp09.decision-dataset.v1"
FEATURE_HORIZONS = (4, 12, 24)
# Width of a Gaussian p10-p90 interval in standard deviations (2 * 1.2816).
P10_P90_SIGMAS = 2.5631031311
DEFAULT_BARRIER_SIGMAS = 1.0
FORECAST_QUANTILE_COLUMNS = ("timestamp", "timestral_p10", "timestral_p50", "timestral_p90")

# Stationary cutoff-only indicators reused from S02-CP06B. Price levels in USDT
# (sma/ema/atr14/close) are deliberately excluded: they track the price regime
# of the sample period rather than a repeatable market state.
REGIME_FEATURES = (
    "rsi14",
    "atr_pct",
    "volatility_24h",
    "volatility_168h",
    "momentum_24h",
    "momentum_168h",
    "dist_sma50_pct",
    "trend_strength_pct",
    "volume_ratio",
)
TIMESTRAL_FEATURES = (
    *(f"ts_return_pct_{h}h" for h in FEATURE_HORIZONS),
    *(f"ts_width_pct_{h}h" for h in FEATURE_HORIZONS),
    "ts_skew_24h",
    "ts_return_z_24h",
    "ts_width_growth",
    "ts_width_vs_realized",
    "ts_path_max_pct",
    "ts_path_min_pct",
    "ts_path_monotonicity",
)
CALENDAR_FEATURES = ("dow_sin", "dow_cos")
FEATURE_COLUMNS = (*REGIME_FEATURES, *TIMESTRAL_FEATURES, *CALENDAR_FEATURES)
TARGET_COLUMNS = (
    "gross_return_pct_24h",
    "abs_return_pct_24h",
    "long_net_pct",
    "short_net_pct",
    "vol_scaled_return_24h",
    "realized_vol_24h",
    "max_favorable_excursion_pct",
    "max_adverse_excursion_pct",
    "barrier_pct",
    "triple_barrier",
    "triple_barrier_hit_hour",
    "triple_barrier_same_bar",
    "trade_worth",
    "sample_weight",
    "best_action",
)


class DecisionDatasetError(ValueError):
    """Raised when the decision dataset would leak or misalign data."""


@dataclass(frozen=True)
class DatasetPaths:
    data_root: Path = Path("data")
    snapshot_dir: Path = Path("data/snapshots/BTCUSDT-1h-raw-v1")
    expansion_dir: Path = Path("data/experiments/S02-CP06A-historical-expansion-v2")
    regime_dir: Path = Path("data/datasets/S02-CP06B-regime-features-v2")
    label_dir: Path = Path("data/datasets/S02-CP07-decision-labels-v2")
    output_dir: Path = Path("data/datasets/S02-CP09-decision-dataset-v1")


def _utc(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series, utc=True)


def build_features(regime: pd.DataFrame, forecasts: dict[int, pd.DataFrame]) -> pd.DataFrame:
    """Cutoff-only features: stationary regime indicators + Timestral path shape.

    ``forecasts`` maps ``window_index`` to the 24-row quantile path. Frames
    must not contain realized prices.
    """

    missing = {"window_index", "cutoff", "close_at_cutoff_bar", *REGIME_FEATURES} - set(regime.columns)
    if missing:
        raise DecisionDatasetError(f"Regime columns missing: {sorted(missing)}")
    if regime["window_index"].duplicated().any():
        raise DecisionDatasetError("Duplicate window_index in regime features")
    if set(regime["window_index"].astype(int)) != set(forecasts):
        raise DecisionDatasetError("Regime/forecast window sets differ")
    data = regime.sort_values("cutoff").reset_index(drop=True)
    cutoffs = _utc(data["cutoff"])
    records: list[dict[str, Any]] = []
    for position, row in enumerate(data.itertuples(index=False)):
        index = int(row.window_index)
        frame = forecasts[index]
        if "actual" in frame.columns:
            raise DecisionDatasetError(f"Forecast frame for window {index} carries realized prices")
        if set(FORECAST_QUANTILE_COLUMNS) - set(frame.columns) or len(frame) != HORIZON_HOURS:
            raise DecisionDatasetError(f"Invalid forecast path for window {index}")
        cutoff = cutoffs.iloc[position]
        if pd.Timestamp(_utc(frame["timestamp"]).iloc[0]) != cutoff:
            raise DecisionDatasetError(f"Forecast start is not the cutoff for window {index}")
        anchor = float(row.close_at_cutoff_bar)
        p10 = frame["timestral_p10"].to_numpy(dtype=float)
        p50 = frame["timestral_p50"].to_numpy(dtype=float)
        p90 = frame["timestral_p90"].to_numpy(dtype=float)
        width = (p90 - p10) / anchor
        if anchor <= 0 or not (width > 0).all():
            raise DecisionDatasetError(f"Degenerate anchor or interval for window {index}")
        path_return = p50 / anchor - 1.0
        record: dict[str, Any] = {"window_index": index, "cutoff": cutoff}
        record.update({name: float(getattr(row, name)) for name in REGIME_FEATURES})
        for horizon in FEATURE_HORIZONS:
            record[f"ts_return_pct_{horizon}h"] = 100.0 * float(path_return[horizon - 1])
            record[f"ts_width_pct_{horizon}h"] = 100.0 * float(width[horizon - 1])
        steps = np.diff(np.concatenate([[anchor], p50]))
        realized_sigma_24h = float(row.volatility_168h) * np.sqrt(HORIZON_HOURS)
        record.update(
            {
                # +1: all upside (p50 at p10); -1: all downside; 0: symmetric.
                "ts_skew_24h": float((p90[-1] + p10[-1] - 2.0 * p50[-1]) / (p90[-1] - p10[-1])),
                "ts_return_z_24h": float(path_return[-1] / (width[-1] / P10_P90_SIGMAS)),
                "ts_width_growth": float(width[-1] / width[FEATURE_HORIZONS[0] - 1]),
                # Model-implied 24h sigma over trailing realized 24h sigma.
                "ts_width_vs_realized": float((width[-1] / P10_P90_SIGMAS) / realized_sigma_24h),
                "ts_path_max_pct": 100.0 * float(path_return.max()),
                "ts_path_min_pct": 100.0 * float(path_return.min()),
                "ts_path_monotonicity": float(np.mean(np.sign(steps))),
                "dow_sin": float(np.sin(2.0 * np.pi * cutoff.dayofweek / 7.0)),
                "dow_cos": float(np.cos(2.0 * np.pi * cutoff.dayofweek / 7.0)),
            }
        )
        records.append(record)
    features = pd.DataFrame(records)[["window_index", "cutoff", *FEATURE_COLUMNS]]
    if not np.isfinite(features[list(FEATURE_COLUMNS)].to_numpy(dtype=float)).all():
        raise DecisionDatasetError("Non-finite decision features")
    return features


def build_targets(
    labels: pd.DataFrame,
    market: pd.DataFrame,
    volatility_168h: pd.Series,
    *,
    barrier_sigmas: float = DEFAULT_BARRIER_SIGMAS,
) -> pd.DataFrame:
    """Realized 24h outcomes (future information; analysis targets only).

    ``volatility_168h`` is indexed by ``window_index`` and is the cutoff-time
    hourly volatility used to scale returns and to size the triple barrier:
    ``barrier = barrier_sigmas * volatility_168h * sqrt(24)`` around the entry
    open. ``triple_barrier`` is +1/-1 for the first barrier touched by the
    hourly high/low inside the holding period, 0 on timeout or when both
    barriers are touched inside the same bar (order unknowable at 1h).
    """

    needed = {"window_index", "cutoff", "entry_open", "exit_open", "long_net_pct", "short_net_pct",
              "long_gross_pct", "best_action", "fee_rate", "slippage"}
    if needed - set(labels.columns):
        raise DecisionDatasetError(f"Label columns missing: {sorted(needed - set(labels.columns))}")
    if barrier_sigmas <= 0:
        raise DecisionDatasetError("barrier_sigmas must be positive")
    data = labels.sort_values("cutoff").reset_index(drop=True)
    cutoffs = _utc(data["cutoff"])
    records: list[dict[str, Any]] = []
    for position, row in enumerate(data.itertuples(index=False)):
        index = int(row.window_index)
        cutoff = cutoffs.iloc[position]
        bars = market.loc[cutoff : cutoff + pd.Timedelta(hours=HORIZON_HOURS - 1)]
        if len(bars) != HORIZON_HOURS:
            raise DecisionDatasetError(f"Holding period is not {HORIZON_HOURS} contiguous bars: window {index}")
        entry = float(row.entry_open)
        if abs(float(bars["open"].iloc[0]) / entry - 1.0) > 1e-9:
            raise DecisionDatasetError(f"Entry open does not match the market bar: window {index}")
        sigma_24h = float(volatility_168h.loc[index]) * np.sqrt(HORIZON_HOURS)
        barrier = float(barrier_sigmas) * sigma_24h
        high = bars["high"].to_numpy(dtype=float)
        low = bars["low"].to_numpy(dtype=float)
        hit_up = high >= entry * (1.0 + barrier)
        hit_down = low <= entry * (1.0 - barrier)
        touched = np.flatnonzero(hit_up | hit_down)
        outcome, hit_hour, same_bar = 0, None, False
        if len(touched):
            first = int(touched[0])
            hit_hour = first + 1
            same_bar = bool(hit_up[first] and hit_down[first])
            outcome = 0 if same_bar else (1 if hit_up[first] else -1)
        # Hourly log returns from the entry open through the 24 closes.
        prices = np.concatenate([[entry], bars["close"].to_numpy(dtype=float)])
        gross = float(row.long_gross_pct)
        round_trip_cost_pct = 100.0 * 2.0 * (float(row.fee_rate) + float(row.slippage))
        best_net = max(float(row.long_net_pct), float(row.short_net_pct), 0.0)
        records.append(
            {
                "window_index": index,
                "cutoff": cutoff,
                "gross_return_pct_24h": gross,
                "abs_return_pct_24h": abs(gross),
                "long_net_pct": float(row.long_net_pct),
                "short_net_pct": float(row.short_net_pct),
                "vol_scaled_return_24h": gross / (100.0 * sigma_24h),
                "realized_vol_24h": float(np.std(np.diff(np.log(prices)), ddof=1)),
                "max_favorable_excursion_pct": 100.0 * float(high.max() / entry - 1.0),
                "max_adverse_excursion_pct": 100.0 * float(low.min() / entry - 1.0),
                "barrier_pct": 100.0 * barrier,
                "triple_barrier": int(outcome),
                "triple_barrier_hit_hour": hit_hour,
                "triple_barrier_same_bar": same_bar,
                # Meta-label: was any directional trade profitable after costs?
                "trade_worth": bool(abs(gross) > round_trip_cost_pct and best_net > 0.0),
                # Economic importance of getting this window right.
                "sample_weight": best_net,
                "best_action": row.best_action,
            }
        )
    targets = pd.DataFrame(records)
    targets["triple_barrier_hit_hour"] = targets["triple_barrier_hit_hour"].astype("Int64")
    return targets


def load_forecast_paths(expansion_dir: Path, window_indices: list[int]) -> dict[int, pd.DataFrame]:
    """Read archived quantile paths, verifying checksums and dropping realized prices."""

    paths: dict[int, pd.DataFrame] = {}
    for index in window_indices:
        folder = expansion_dir / f"window-{int(index):04d}"
        metrics = json.loads((folder / "metrics.json").read_text(encoding="utf-8"))
        forecast_path = folder / "forecast.parquet"
        if int(metrics.get("window_index", -1)) != int(index) or metrics.get("forecast_sha256") != digest(forecast_path):
            raise DecisionDatasetError(f"Invalid archived forecast or checksum: window {index}")
        paths[int(index)] = pd.read_parquet(forecast_path, columns=list(FORECAST_QUANTILE_COLUMNS))
    return paths


def _source_hashes(paths: DatasetPaths) -> dict[str, str]:
    files = {
        "snapshot_raw_manifest": paths.snapshot_dir / "raw-manifest.json",
        "expansion_config": paths.expansion_dir / "config.json",
        "expansion_canonical": paths.expansion_dir / "canonical_predictions.parquet",
        "regime_features": paths.regime_dir / "features.parquet",
        "labels": paths.label_dir / "labels.parquet",
    }
    return {name: digest(path) for name, path in files.items() if path.is_file()}


def run(
    paths: DatasetPaths = DatasetPaths(),
    *,
    barrier_sigmas: float = DEFAULT_BARRIER_SIGMAS,
    write: bool = True,
) -> dict[str, Any]:
    from .dashboard.data_loader import load_market_data

    regime = pd.read_parquet(paths.regime_dir / "features.parquet")
    labels = pd.read_parquet(paths.label_dir / "labels.parquet")
    if set(regime["window_index"]) != set(labels["window_index"]):
        raise DecisionDatasetError("Regime and label window sets differ")
    forecasts = load_forecast_paths(paths.expansion_dir, sorted(int(i) for i in regime["window_index"]))
    features = build_features(regime, forecasts)
    market = load_market_data(paths.data_root, paths.snapshot_dir, verify_checksums=True).frame
    targets = build_targets(
        labels,
        market,
        regime.set_index("window_index")["volatility_168h"],
        barrier_sigmas=barrier_sigmas,
    )
    if not (features["window_index"].to_numpy() == targets["window_index"].to_numpy()).all():
        raise DecisionDatasetError("Feature/target row order differs")
    gap_hours = _utc(features["cutoff"]).diff().dt.total_seconds().div(3600.0).dropna()
    summary: dict[str, Any] = {
        "schema_version": DATASET_SCHEMA,
        "generated_utc": now_iso(),
        "rows": int(len(features)),
        "first_cutoff": str(features["cutoff"].iloc[0]),
        "last_cutoff": str(features["cutoff"].iloc[-1]),
        "min_cutoff_gap_hours": float(gap_hours.min()),
        "barrier_sigmas": float(barrier_sigmas),
        "triple_barrier_counts": {str(k): int(v) for k, v in targets["triple_barrier"].value_counts().sort_index().items()},
        "triple_barrier_same_bar": int(targets["triple_barrier_same_bar"].sum()),
        "trade_worth_rate": float(targets["trade_worth"].mean()),
        "mean_abs_return_pct_24h": float(targets["abs_return_pct_24h"].mean()),
        "feature_columns": list(FEATURE_COLUMNS),
        "target_columns": list(TARGET_COLUMNS),
        "warning": "targets.parquet contains future outcomes; never join it to inference inputs.",
        "purge_note": f"holding period is {HORIZON_HOURS}h: purge >= {HORIZON_HOURS}h between train and test.",
    }
    if write:
        _write_dataset(paths, features, targets, summary)
    return summary


def _write_dataset(
    paths: DatasetPaths, features: pd.DataFrame, targets: pd.DataFrame, summary: dict[str, Any]
) -> None:
    output = paths.output_dir
    if output.exists():
        raise FileExistsError(f"Decision dataset already exists; refusing to overwrite: {output}")
    before = _source_hashes(paths)
    output.mkdir(parents=True)
    try:
        features[["window_index", "cutoff", *FEATURE_COLUMNS]].to_parquet(output / "features.parquet", index=False)
        targets[["window_index", "cutoff", *TARGET_COLUMNS]].to_parquet(output / "targets.parquet", index=False)
        summary["features_sha256"] = digest(output / "features.parquet")
        summary["targets_sha256"] = digest(output / "targets.parquet")
        (output / "summary.json").write_text(json.dumps(summary, indent=2, default=str) + "\n", encoding="utf-8")
        after = _source_hashes(paths)
        manifest = {
            "schema_version": DATASET_SCHEMA,
            "created_utc": now_iso(),
            "source_hashes": before,
            "source_hashes_unchanged": before == after,
            "outputs": {
                "features": {"path": "features.parquet", "sha256": summary["features_sha256"]},
                "targets": {"path": "targets.parquet", "sha256": summary["targets_sha256"]},
            },
            "documentation": {
                "features": "cutoff-only: stationary S02-CP06B indicators, Timestral p10/p50/p90 path shape, weekday",
                "ts_width_pct": "100 * (p90 - p10) / anchor at the given horizon",
                "ts_return_z_24h": "p50 24h return divided by the model-implied sigma (width / 2.5631)",
                "ts_width_vs_realized": "model-implied 24h sigma / (volatility_168h * sqrt(24))",
                "triple_barrier": "+1/-1 first barrier touched by hourly high/low, 0 timeout or same-bar touch",
                "barrier": "barrier_sigmas * volatility_168h * sqrt(24), around the entry open",
                "execution": "entry open at cutoff, exit open at cutoff + 24h (S02-CP07 contract)",
            },
        }
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8")
        if before != after:
            raise DecisionDatasetError("Source artifacts changed while writing the decision dataset")
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S02-CP09 decision dataset (uncertainty features, continuous targets)")
    parser.add_argument("--data-root", default=str(DatasetPaths.data_root))
    parser.add_argument("--snapshot-dir", default=str(DatasetPaths.snapshot_dir))
    parser.add_argument("--expansion-dir", default=str(DatasetPaths.expansion_dir))
    parser.add_argument("--regime-dir", default=str(DatasetPaths.regime_dir))
    parser.add_argument("--label-dir", default=str(DatasetPaths.label_dir))
    parser.add_argument("--output-dir", default=str(DatasetPaths.output_dir))
    parser.add_argument("--barrier-sigmas", type=float, default=DEFAULT_BARRIER_SIGMAS)
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)
    paths = DatasetPaths(
        Path(args.data_root), Path(args.snapshot_dir), Path(args.expansion_dir),
        Path(args.regime_dir), Path(args.label_dir), Path(args.output_dir),
    )
    summary = run(paths, barrier_sigmas=args.barrier_sigmas, write=not args.no_write)
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
