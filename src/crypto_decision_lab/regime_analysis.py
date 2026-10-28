"""S02-CP06B STEP 1-3: forecast error analysis and cutoff-only regime features.

Contracts
---------
* Errors are derived from the S02-CP06A canonical archive (``cdl.canonical.v1``).
* Every feature uses only bars with ``timestamp < cutoff`` (causal indicators).
* Regime thresholds are terciles fitted **only** on the training rows supplied by
  the caller; they are never recomputed on test rows.
* Signed error convention: ``error = predicted - actual`` (USDT).
* ``delta_mae = abs_error_timestral - abs_error_naive``; negative means Timestral
  was closer to the realized price.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd

from .canonical import TARGET_OFFSET, validate_h24_frame
from .sync import digest

REGIME_SCHEMA = "cdl.cp06b.regime.v1"
TREND_REGIMES = ("TREND_DOWN", "SIDEWAYS", "TREND_UP")
VOL_REGIMES = ("VOL_LOW", "VOL_MEDIUM", "VOL_HIGH")
MIN_REGIME_OBS = 30
REQUIRED_HISTORY_BARS = 168
BOOTSTRAP_BLOCKS = 10
BOOTSTRAP_DRAWS = 1000
BOOTSTRAP_SEED = 42

FEATURE_COLUMNS = (
    "sma20",
    "sma50",
    "ema20",
    "ema50",
    "rsi14",
    "atr14",
    "atr_pct",
    "volatility_24h",
    "volatility_168h",
    "momentum_24h",
    "momentum_168h",
    "dist_sma50_pct",
    "trend_strength_pct",
    "volume_ratio",
)

ERROR_COLUMNS = (
    "abs_error_timestral",
    "abs_error_naive",
    "signed_error_timestral",
    "signed_error_naive",
    "delta_mae",
    "winner",
    "predicted_direction",
    "actual_direction",
    "direction_correct",
)


@dataclass(frozen=True)
class RegimePaths:
    data_root: Path = Path("data")
    snapshot_dir: Path = Path("data/snapshots/BTCUSDT-1h-raw-v1")
    expansion_dir: Path = Path("data/experiments/S02-CP06A-historical-expansion-v1")
    output_dir: Path = Path("data/datasets/S02-CP06B-regime-features-v1")


class RegimeError(ValueError):
    """Raised when the regime pipeline would leak or misalign data."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc(value: Any) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def load_canonical(expansion_dir: Path) -> pd.DataFrame:
    """Load and validate the S02-CP06A canonical h24 table."""

    path = expansion_dir / "canonical_predictions.parquet"
    if not path.is_file():
        raise RegimeError(f"Canonical predictions missing: {path}")
    frame = pd.read_parquet(path)
    return validate_h24_frame(frame)


def build_errors(canonical: pd.DataFrame) -> pd.DataFrame:
    """STEP 1: per-window error decomposition on the h24 target."""

    data = canonical.sort_values("cutoff").reset_index(drop=True).copy()
    actual = data["actual_price_24h"].to_numpy(dtype=float)
    timestral = data["timestral_price_24h"].to_numpy(dtype=float)
    naive = data["naive_price_24h"].to_numpy(dtype=float)
    anchor = data["anchor_price"].to_numpy(dtype=float)
    errors = pd.DataFrame(
        {
            "window_index": data["window_index"].astype(int),
            "cutoff": data["cutoff"],
            "target": data["target"],
            "source": data.get("source"),
            "anchor_price": anchor,
            "actual_price_24h": actual,
            "timestral_price_24h": timestral,
            "naive_price_24h": naive,
            "timestral_return_pct_24h": data["timestral_return_pct_24h"].to_numpy(dtype=float),
            "abs_error_timestral": np.abs(timestral - actual),
            "abs_error_naive": np.abs(naive - actual),
            "signed_error_timestral": timestral - actual,
            "signed_error_naive": naive - actual,
        }
    )
    errors["delta_mae"] = errors["abs_error_timestral"] - errors["abs_error_naive"]
    errors["winner"] = np.where(
        errors["delta_mae"] < 0,
        "TIMESTRAL",
        np.where(errors["delta_mae"] > 0, "NAIVE", "TIE"),
    )
    errors["predicted_direction"] = np.sign(timestral - anchor)
    errors["actual_direction"] = np.sign(actual - anchor)
    errors["direction_correct"] = np.where(
        (errors["predicted_direction"] != 0) & (errors["actual_direction"] != 0),
        (errors["predicted_direction"] == errors["actual_direction"]).astype(float),
        np.nan,
    )
    return errors


def reconcile_with_benchmark(
    errors: pd.DataFrame, expansion_dir: Path, *, atol: float = 1e-6
) -> list[dict[str, Any]]:
    """Cross-check STEP 1 errors against the archived CP06A benchmark."""

    summary_path = expansion_dir / "benchmark_summary.json"
    checks: list[dict[str, Any]] = []
    if not summary_path.is_file():
        checks.append({"check": "benchmark summary present", "passed": False, "detail": str(summary_path)})
        return checks
    benchmark = json.loads(summary_path.read_text(encoding="utf-8"))
    expected_rows = int(benchmark.get("windows", -1))
    checks.append(
        {
            "check": "window count matches benchmark",
            "passed": len(errors) == expected_rows,
            "detail": f"errors={len(errors)} benchmark={expected_rows}",
        }
    )
    pairs = (
        ("timestral MAE", errors["abs_error_timestral"].mean(), benchmark["timestral_vs_naive"]["timestral"]["mae_usdt"]),
        ("naive MAE", errors["abs_error_naive"].mean(), benchmark["timestral_vs_naive"]["naive"]["mae_usdt"]),
        ("timestral RMSE", np.sqrt((errors["signed_error_timestral"] ** 2).mean()), benchmark["timestral_vs_naive"]["timestral"]["rmse_usdt"]),
        ("naive RMSE", np.sqrt((errors["signed_error_naive"] ** 2).mean()), benchmark["timestral_vs_naive"]["naive"]["rmse_usdt"]),
        ("timestral bias", errors["signed_error_timestral"].mean(), benchmark["timestral_vs_naive"]["timestral"]["bias_usdt"]),
        ("naive bias", errors["signed_error_naive"].mean(), benchmark["timestral_vs_naive"]["naive"]["bias_usdt"]),
        ("directional accuracy", errors["direction_correct"].mean(), benchmark["timestral_vs_naive"]["timestral"]["directional_accuracy"]),
    )
    for label, actual_value, expected in pairs:
        expected = float(expected)
        finite = np.isfinite(actual_value) and np.isfinite(expected)
        passed = finite and abs(float(actual_value) - expected) <= max(atol, abs(expected) * 1e-9)
        checks.append(
            {
                "check": f"reconciled {label}",
                "passed": bool(passed),
                "detail": f"computed={float(actual_value):.9g} benchmark={expected:.9g}",
            }
        )
    direction_evaluable = int(errors["direction_correct"].notna().sum())
    checks.append(
        {
            "check": "direction evaluable count",
            "passed": direction_evaluable == int(benchmark["timestral_vs_naive"]["timestral"]["direction_evaluable"]),
            "detail": f"computed={direction_evaluable} benchmark={benchmark['timestral_vs_naive']['timestral']['direction_evaluable']}",
        }
    )
    return checks


def _causal_indicator(market: pd.DataFrame, key: str, params: dict[str, Any]) -> pd.Series:
    from .dashboard.indicators import compute_indicator

    result = compute_indicator(market, key, params)
    if len(result.columns) != 1:
        raise RegimeError(f"Expected a single output column for {key}")
    return result.iloc[:, 0]


def compute_features(
    market: pd.DataFrame,
    windows: pd.DataFrame,
    *,
    history_bars: int = 400,
) -> pd.DataFrame:
    """STEP 2: cutoff-only features reusing the causal indicator registry.

    ``windows`` must provide ``window_index`` and ``cutoff`` columns.  Every
    indicator value is read at the last bar strictly before the cutoff, so no
    target-horizon information can enter the features.

    Formulas (close = hourly closes with timestamp < cutoff):

    * sma20/sma50, ema20/ema50, rsi14, atr14: causal registry indicators.
    * momentum_24h  = close[-1] / close[-25]  - 1   (24h change, decimal)
    * momentum_168h = close[-1] / close[-168] - 1   (trailing 168-bar window,
      first-to-last, matching S02-CP03 ``trend_168h``)
    * volatility_24h  = std(ddof=1) of log returns over trailing 24 closes
    * volatility_168h = std(ddof=1) of log returns over trailing 168 closes
      (matching S02-CP03 ``volatility_168h``)
    * atr_pct          = 100 * atr14 / close[-1]
    * dist_sma50_pct   = 100 * (close[-1] / sma50 - 1)
    * trend_strength_pct = 100 * (ema20 / ema50 - 1)
    * volume_ratio     = mean(volume, 24h) / mean(volume, 168h)
    """

    missing = {"window_index", "cutoff"} - set(windows.columns)
    if missing:
        raise RegimeError(f"windows frame missing columns: {sorted(missing)}")

    from .dashboard.indicators import compute_indicator

    close = market["close"].astype(float)
    series: dict[str, pd.Series] = {
        "sma20": _causal_indicator(market, "sma", {"window": 20}),
        "sma50": _causal_indicator(market, "sma", {"window": 50}),
        "ema20": _causal_indicator(market, "ema", {"window": 20}),
        "ema50": _causal_indicator(market, "ema", {"window": 50}),
        "rsi14": _causal_indicator(market, "rsi", {"period": 14}),
        "atr14": _causal_indicator(market, "atr", {"period": 14}),
        "volume_ma24": market["volume"].rolling(24, min_periods=24).mean(),
        "volume_ma168": market["volume"].rolling(168, min_periods=168).mean(),
    }
    records: list[dict[str, Any]] = []
    for row in windows[["window_index", "cutoff"]].itertuples(index=False):
        window_index = int(row.window_index)
        cutoff = _utc(row.cutoff)
        bar = cutoff - pd.Timedelta(hours=1)
        if bar not in market.index:
            raise RegimeError(f"Cutoff bar {bar} missing from market data (window {window_index})")
        history = close.loc[:bar]
        if len(history) < REQUIRED_HISTORY_BARS:
            raise RegimeError(f"Insufficient history for window {window_index}: {len(history)} bars")
        close_now = float(history.iloc[-1])
        close_24 = float(history.iloc[-25])
        close_168 = float(history.iloc[-168])
        returns_24 = np.diff(np.log(history.iloc[-24:].to_numpy(dtype=float)))
        returns_168 = np.diff(np.log(history.iloc[-168:].to_numpy(dtype=float)))
        atr = float(series["atr14"].loc[bar])
        sma50 = float(series["sma50"].loc[bar])
        ema20 = float(series["ema20"].loc[bar])
        ema50 = float(series["ema50"].loc[bar])
        volume_ma24 = float(series["volume_ma24"].loc[bar])
        volume_ma168 = float(series["volume_ma168"].loc[bar])
        records.append(
            {
                "window_index": int(window_index),
                "cutoff": cutoff,
                "feature_bar": bar,
                "close_at_cutoff_bar": close_now,
                "sma20": float(series["sma20"].loc[bar]),
                "sma50": sma50,
                "ema20": ema20,
                "ema50": ema50,
                "rsi14": float(series["rsi14"].loc[bar]),
                "atr14": atr,
                "atr_pct": 100.0 * atr / close_now,
                "volatility_24h": float(np.std(returns_24, ddof=1)),
                "volatility_168h": float(np.std(returns_168, ddof=1)),
                "momentum_24h": close_now / close_24 - 1.0,
                "momentum_168h": close_now / close_168 - 1.0,
                "dist_sma50_pct": 100.0 * (close_now / sma50 - 1.0),
                "trend_strength_pct": 100.0 * (ema20 / ema50 - 1.0),
                "volume_ratio": volume_ma24 / volume_ma168,
            }
        )
    features = pd.DataFrame(records)
    numeric = features[list(FEATURE_COLUMNS)].to_numpy(dtype=float)
    if not np.isfinite(numeric).all():
        raise RegimeError("Non-finite regime features")
    return features


def reconcile_anchor_prices(
    features: pd.DataFrame, errors: pd.DataFrame, *, atol: float = 1e-2
) -> list[dict[str, Any]]:
    merged = errors.merge(
        features[["window_index", "close_at_cutoff_bar", "feature_bar"]],
        on="window_index",
        how="inner",
        validate="one_to_one",
    )
    if len(merged) != len(errors):
        raise RegimeError("Feature/error window sets differ")
    delta = (merged["anchor_price"] - merged["close_at_cutoff_bar"]).abs()
    expected_bar = merged["cutoff"] - pd.Timedelta(hours=1)
    return [
        {
            "check": "anchor equals market close one hour before cutoff (float32 tolerance)",
            "passed": bool((delta <= atol).all()),
            "detail": f"max_abs={float(delta.max()):.6g} atol={atol}",
        },
        {
            "check": "feature bar is cutoff minus one hour",
            "passed": bool((merged["feature_bar"] == expected_bar).all()),
            "detail": "feature_bar == cutoff - 1h",
        },
    ]


def fit_tercile_thresholds(train_values: np.ndarray) -> tuple[float, float]:
    """Training-only terciles used as regime thresholds."""

    values = np.asarray(train_values, dtype=float)
    values = values[np.isfinite(values)]
    if len(values) < 3:
        raise RegimeError("Not enough training rows to fit tercile thresholds")
    return (
        float(np.quantile(values, 1.0 / 3.0)),
        float(np.quantile(values, 2.0 / 3.0)),
    )


def assign_trend_regime(momentum_168h: pd.Series, thresholds: tuple[float, float]) -> pd.Series:
    low, high = thresholds
    return pd.Series(
        np.where(
            momentum_168h < low,
            TREND_REGIMES[0],
            np.where(momentum_168h > high, TREND_REGIMES[2], TREND_REGIMES[1]),
        ),
        index=momentum_168h.index,
    )


def assign_vol_regime(volatility_168h: pd.Series, thresholds: tuple[float, float]) -> pd.Series:
    low, high = thresholds
    return pd.Series(
        np.where(
            volatility_168h < low,
            VOL_REGIMES[0],
            np.where(volatility_168h > high, VOL_REGIMES[2], VOL_REGIMES[1]),
        ),
        index=volatility_168h.index,
    )


def assign_regimes(
    frame: pd.DataFrame,
    *,
    trend_thresholds: tuple[float, float] | None = None,
    vol_thresholds: tuple[float, float] | None = None,
) -> pd.DataFrame:
    """Attach regime labels; thresholds must come from training rows only."""

    data = frame.copy()
    if trend_thresholds is None:
        trend_thresholds = fit_tercile_thresholds(data["momentum_168h"].to_numpy(dtype=float))
    if vol_thresholds is None:
        vol_thresholds = fit_tercile_thresholds(data["volatility_168h"].to_numpy(dtype=float))
    data["trend_regime"] = assign_trend_regime(data["momentum_168h"], trend_thresholds)
    data["vol_regime"] = assign_vol_regime(data["volatility_168h"], vol_thresholds)
    data["combined_regime"] = data["trend_regime"] + "/" + data["vol_regime"]
    data["trend_thresholds"] = [str(trend_thresholds)] * len(data)
    data["vol_thresholds"] = [str(vol_thresholds)] * len(data)
    return data


def moving_block_bootstrap_ci(
    values: np.ndarray,
    *,
    statistic: Callable[[np.ndarray], float] | None = None,
    block: int = BOOTSTRAP_BLOCKS,
    draws: int = BOOTSTRAP_DRAWS,
    seed: int = BOOTSTRAP_SEED,
    alpha: float = 0.05,
) -> dict[str, float]:
    """Moving-block bootstrap that preserves short-range temporal dependence."""

    array = np.asarray(values, dtype=float)
    array = array[np.isfinite(array)]
    if statistic is None:
        statistic = lambda x: float(np.mean(x))  # noqa: E731
    if len(array) < 2:
        point = statistic(array) if len(array) else float("nan")
        return {"point": point, "ci_low": float("nan"), "ci_high": float("nan"), "draws": 0}
    generator = np.random.default_rng(seed)
    n = len(array)
    block = max(1, min(int(block), n))
    starts = np.arange(n - block + 1)
    statistics = np.empty(draws, dtype=float)
    for draw in range(draws):
        positions = generator.choice(starts, size=int(np.ceil(n / block)), replace=True)
        sample = np.concatenate([array[position:position + block] for position in positions])[:n]
        statistics[draw] = statistic(sample)
    return {
        "point": statistic(array),
        "ci_low": float(np.quantile(statistics, alpha / 2.0)),
        "ci_high": float(np.quantile(statistics, 1.0 - alpha / 2.0)),
        "draws": int(draws),
    }


def regime_statistics(
    frame: pd.DataFrame,
    group_column: str,
    *,
    min_obs: int = MIN_REGIME_OBS,
) -> pd.DataFrame:
    """STEP 3: per-regime error statistics with block-bootstrap CI on delta MAE."""

    if group_column not in frame.columns:
        raise RegimeError(f"Unknown grouping column: {group_column}")
    rows: list[dict[str, Any]] = []
    for regime, group in frame.groupby(group_column, dropna=False):
        delta = group["delta_mae"].to_numpy(dtype=float)
        ci = moving_block_bootstrap_ci(delta)
        rows.append(
            {
                group_column: regime,
                "observations": int(len(group)),
                "timestral_mae_usdt": float(group["abs_error_timestral"].mean()),
                "naive_mae_usdt": float(group["abs_error_naive"].mean()),
                "delta_mae_usdt": float(delta.mean()),
                "delta_mae_ci_low": ci["ci_low"],
                "delta_mae_ci_high": ci["ci_high"],
                "timestral_win_rate": float((group["winner"] == "TIMESTRAL").mean()),
                "signed_error_timestral_mean": float(group["signed_error_timestral"].mean()),
                "signed_error_naive_mean": float(group["signed_error_naive"].mean()),
                "delta_mae_median": float(np.median(delta)),
                "delta_mae_p10": float(np.quantile(delta, 0.10)),
                "delta_mae_p90": float(np.quantile(delta, 0.90)),
                "reliable": bool(len(group) >= min_obs),
            }
        )
    return pd.DataFrame(rows).sort_values(group_column).reset_index(drop=True)


def period_breakdown(frame: pd.DataFrame) -> pd.DataFrame:
    """Per-calendar-year error breakdown (UTC cutoff year)."""
    data = frame.copy()
    data["period"] = data["cutoff"].dt.year.astype(int).astype(str)
    rows = []
    for period, group in data.groupby("period"):
        rows.append(
            {
                "period": period,
                "windows": int(len(group)),
                "timestral_mae_usdt": float(group["abs_error_timestral"].mean()),
                "naive_mae_usdt": float(group["abs_error_naive"].mean()),
                "delta_mae_usdt": float(group["delta_mae"].mean()),
                "timestral_win_rate": float((group["winner"] == "TIMESTRAL").mean()),
            }
        )
    return pd.DataFrame(rows)


def _source_hashes(paths: RegimePaths) -> dict[str, str]:
    files = {
        "snapshot_raw_manifest": paths.snapshot_dir / "raw-manifest.json",
        "snapshot_report": paths.snapshot_dir / "report.json",
        "expansion_manifest": paths.expansion_dir / "manifest.json",
        "expansion_canonical": paths.expansion_dir / "canonical_predictions.parquet",
        "expansion_benchmark": paths.expansion_dir / "benchmark_summary.json",
    }
    return {name: digest(path) for name, path in files.items() if path.is_file()}


def run(
    paths: RegimePaths = RegimePaths(),
    *,
    write: bool = True,
) -> dict[str, Any]:
    from .dashboard.data_loader import load_market_data

    canonical = load_canonical(paths.expansion_dir)
    errors = build_errors(canonical)
    checks = reconcile_with_benchmark(errors, paths.expansion_dir)
    market = load_market_data(paths.data_root, paths.snapshot_dir, verify_checksums=True).frame
    features = compute_features(market, errors[["window_index", "cutoff"]])
    checks.extend(reconcile_anchor_prices(features, errors))
    actual_check = market.loc[errors["target"].map(lambda value: _utc(value)), "close"].to_numpy(dtype=float)
    checks.append(
        {
            "check": "actual equals market close at target",
            "passed": bool(np.allclose(actual_check, errors["actual_price_24h"].to_numpy(dtype=float), rtol=0, atol=1e-2)),
            "detail": f"max_abs={float(np.max(np.abs(actual_check - errors['actual_price_24h'].to_numpy(dtype=float)))):.6g}",
        }
    )
    if not all(check["passed"] for check in checks):
        failed = [check for check in checks if not check["passed"]]
        raise RegimeError(f"Reconciliation failed: {failed}")

    merged = errors.merge(features, on=["window_index", "cutoff"], how="inner", validate="one_to_one")
    if len(merged) != len(errors):
        raise RegimeError("Feature merge lost windows")
    merged = merged.sort_values("cutoff").reset_index(drop=True)
    descriptive = assign_regimes(merged)

    trend_stats = regime_statistics(descriptive, "trend_regime")
    vol_stats = regime_statistics(descriptive, "vol_regime")
    combined_stats = regime_statistics(descriptive, "combined_regime")
    periods = period_breakdown(descriptive)

    result: dict[str, Any] = {
        "schema_version": REGIME_SCHEMA,
        "windows": int(len(merged)),
        "generated_utc": _now(),
        "reconciliation_checks": checks,
        "overall": {
            "timestral_mae_usdt": float(merged["abs_error_timestral"].mean()),
            "naive_mae_usdt": float(merged["abs_error_naive"].mean()),
            "delta_mae_usdt": float(merged["delta_mae"].mean()),
            "delta_mae_ci": moving_block_bootstrap_ci(merged["delta_mae"].to_numpy(dtype=float)),
            "timestral_win_rate": float((merged["winner"] == "TIMESTRAL").mean()),
            "directional_accuracy": float(merged["direction_correct"].mean()),
        },
        "trend_regime_stats": trend_stats.to_dict(orient="records"),
        "vol_regime_stats": vol_stats.to_dict(orient="records"),
        "combined_regime_stats": combined_stats.to_dict(orient="records"),
        "period_breakdown": periods.to_dict(orient="records"),
        "feature_thresholds": {
            "note": "Descriptive full-sample terciles; policy folds refit thresholds on training rows only.",
            "trend": descriptive["trend_thresholds"].iloc[0],
            "volatility": descriptive["vol_thresholds"].iloc[0],
        },
    }
    if write:
        _write_dataset(paths, merged, result)
    return result


def _write_dataset(paths: RegimePaths, merged: pd.DataFrame, result: dict[str, Any]) -> None:
    output = paths.output_dir
    if output.exists():
        raise FileExistsError(f"Regime dataset already exists; refusing to overwrite: {output}")
    before = _source_hashes(paths)
    output.mkdir(parents=True)
    try:
        feature_columns = ["window_index", "cutoff", "feature_bar", "close_at_cutoff_bar", *FEATURE_COLUMNS]
        error_columns = [
            "window_index",
            "cutoff",
            "target",
            "source",
            "anchor_price",
            "actual_price_24h",
            "timestral_price_24h",
            "naive_price_24h",
            "timestral_return_pct_24h",
            *ERROR_COLUMNS,
        ]
        merged[feature_columns].to_parquet(output / "features.parquet", index=False)
        merged[error_columns].to_parquet(output / "errors.parquet", index=False)
        result["features_sha256"] = digest(output / "features.parquet")
        result["errors_sha256"] = digest(output / "errors.parquet")
        (output / "summary.json").write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
        after = _source_hashes(paths)
        manifest = {
            "schema_version": REGIME_SCHEMA,
            "created_utc": _now(),
            "source_hashes": before,
            "source_hashes_after": after,
            "source_hashes_unchanged": before == after,
            "original_artifacts_modified": before != after,
            "outputs": {
                "features": {"path": "features.parquet", "sha256": result["features_sha256"]},
                "errors": {"path": "errors.parquet", "sha256": result["errors_sha256"]},
            },
            "documentation": {
                "signed_error": "predicted - actual (USDT)",
                "delta_mae": "abs_error_timestral - abs_error_naive (negative favours Timestral)",
                "features": "indicator values at the last bar strictly before cutoff",
                "regimes": "full-sample terciles for description; policy folds fit terciles on training only",
            },
        }
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str) + "\n", encoding="utf-8")
        if before != after:
            raise RegimeError("Source artifacts changed while writing the regime dataset")
    except BaseException:
        import shutil

        shutil.rmtree(output, ignore_errors=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="S02-CP06B market regime analysis")
    parser.add_argument("--data-root", default=str(RegimePaths.data_root))
    parser.add_argument("--snapshot-dir", default=str(RegimePaths.snapshot_dir))
    parser.add_argument("--expansion-dir", default=str(RegimePaths.expansion_dir))
    parser.add_argument("--output-dir", default=str(RegimePaths.output_dir))
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)
    paths = RegimePaths(
        Path(args.data_root), Path(args.snapshot_dir), Path(args.expansion_dir), Path(args.output_dir)
    )
    result = run(paths, write=not args.no_write)
    payload = {
        "windows": result["windows"],
        "overall": result["overall"],
        "reconciliation_checks": result["reconciliation_checks"],
        "trend_regime_stats": result["trend_regime_stats"],
        "vol_regime_stats": result["vol_regime_stats"],
    }
    print(json.dumps(payload, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
