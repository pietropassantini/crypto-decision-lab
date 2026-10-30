"""Configuration objects for the read-only dashboard."""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
from typing import Any, Mapping

import yaml

from .indicators import DEFAULT_PRESETS

REPO_ROOT_FALLBACK = Path(__file__).resolve().parents[3]
DEFAULT_COLORS: dict[str, str] = {
    "actual": "#f0b90b",
    "timestral": "#4cc9f0",
    "naive": "#9aa4b2",
    "ridge": "#ff8f6b",
    "histgb": "#b388ff",
    "decision": "#2dd4bf",
}


@dataclass(frozen=True)
class DashboardConfig:
    data_root: Path
    snapshot_dir: Path
    batch_dir: Path
    expansion_dir: Path | None
    residual_baselines_dir: Path | None
    residual_corrected_dir: Path | None
    diagnostics_dir: Path | None
    residual_dataset_dir: Path | None
    regime_dataset_dir: Path | None
    policy_results_dir: Path | None
    trading_label_dir: Path | None
    trading_experiment_dir: Path | None
    momentum_stability_dir: Path | None
    parameter_robustness_dir: Path | None
    validation_protocol_dir: Path | None
    forward_dataset_dir: Path | None
    forward_config_path: Path | None
    decision_glob: str | None
    default_timeframe: str = "1D"
    default_days: int = 3650
    default_models: tuple[str, ...] = ("timestral", "naive")
    default_forecast_mode: str = "h24_common"
    verify_checksums: bool = True
    cache_ttl_seconds: int = 300
    chart_height: int = 650
    colors: Mapping[str, str] = field(default_factory=lambda: dict(DEFAULT_COLORS))
    indicator_presets: tuple[dict[str, Any], ...] = field(default_factory=lambda: tuple(DEFAULT_PRESETS))
    regime_vol_bins: tuple[float, float] = (0.005, 0.008)
    regime_trend_bins: tuple[float, float] = (-0.05, 0.05)
    source_label: str = "BTCUSDT · Binance Spot · 1h · UTC"

    @property
    def symbol(self) -> str:
        return "BTCUSDT"


def _resolve(path_value: str | Path | None, base: Path) -> Path | None:
    if path_value is None:
        return None
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _section(raw: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = raw.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"Config section '{name}' must be a mapping")
    return value


def load_config(
    path: str | Path | None = None,
    *,
    repo_root: str | Path | None = None,
) -> DashboardConfig:
    """Load YAML configuration, falling back to repository defaults.

    Relative paths are resolved against ``repo_root`` (default: the repository
    containing this package).  ``CDL_DASHBOARD_CONFIG`` and ``CDL_DATA_ROOT``
    environment variables are supported for local runs.
    """

    root = Path(repo_root or REPO_ROOT_FALLBACK).resolve()
    selected = path or os.environ.get("CDL_DASHBOARD_CONFIG")
    config_path = Path(selected).expanduser() if selected else root / "config" / "dashboard.yaml"
    if not config_path.is_absolute():
        config_path = (root / config_path).resolve()
    raw: dict[str, Any] = {}
    if config_path.is_file():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if loaded is not None and not isinstance(loaded, Mapping):
            raise ValueError("Dashboard config root must be a mapping")
        raw = dict(loaded or {})

    data = _section(raw, "data")
    forecast = _section(raw, "forecast")
    display = _section(raw, "display")
    integrity = _section(raw, "integrity")
    regimes = _section(raw, "regimes")
    models = _section(raw, "models")

    data_root = _resolve(os.environ.get("CDL_DATA_ROOT") or data.get("root", "data"), root)
    assert data_root is not None
    snapshot_dir = _resolve(data.get("snapshot_dir", "data/snapshots/BTCUSDT-1h-raw-v1"), root)
    batch_dir = _resolve(
        forecast.get("batch_dir", "data/experiments/S02-CP02-batch-100"), root
    )
    assert snapshot_dir is not None and batch_dir is not None

    colors = dict(DEFAULT_COLORS)
    colors.update({str(key): str(value) for key, value in _section(raw, "colors").items()})
    colors.update({str(key): str(value) for key, value in models.items() if isinstance(value, str)})

    presets = raw.get("indicator_presets", DEFAULT_PRESETS)
    if not isinstance(presets, (list, tuple)):
        raise ValueError("indicator_presets must be a list")
    normalized_presets = tuple(dict(item) for item in presets if isinstance(item, Mapping)) or tuple(DEFAULT_PRESETS)

    vol_bins = regimes.get("volatility_bins", (0.005, 0.008))
    trend_bins = regimes.get("trend_bins", (-0.05, 0.05))
    if len(vol_bins) != 2 or len(trend_bins) != 2:
        raise ValueError("Regime bins must contain exactly two thresholds")

    return DashboardConfig(
        data_root=data_root,
        snapshot_dir=snapshot_dir,
        batch_dir=batch_dir,
        expansion_dir=_resolve(
            forecast.get("expansion_dir", "data/experiments/S02-CP06A-historical-expansion-v1"),
            root,
        ),
        residual_baselines_dir=_resolve(
            forecast.get("residual_baselines_dir", "data/experiments/S02-CP05-residual-baselines-v1"),
            root,
        ),
        residual_corrected_dir=_resolve(
            forecast.get("residual_corrected_dir", "data/experiments/S02-CP05R-corrected-benchmark-v1"),
            root,
        ),
        diagnostics_dir=_resolve(
            forecast.get("diagnostics_dir", "data/diagnostics/S02-CP03-batch-100-v1"), root
        ),
        residual_dataset_dir=_resolve(
            forecast.get("residual_dataset_dir", "data/datasets/S02-CP04-residuals-v1"), root
        ),
        regime_dataset_dir=_resolve(
            forecast.get("regime_dataset_dir", "data/datasets/S02-CP06B-regime-features-v1"), root
        ),
        policy_results_dir=_resolve(
            forecast.get("policy_results_dir", "data/experiments/S02-CP06B-selective-policy-v1"), root
        ),
        trading_label_dir=_resolve(
            forecast.get("trading_label_dir", "data/datasets/S02-CP07-decision-labels-v1"), root
        ),
        trading_experiment_dir=_resolve(
            forecast.get("trading_experiment_dir", "data/experiments/S02-CP07-trading-simulation-v1"), root
        ),
        momentum_stability_dir=_resolve(
            forecast.get(
                "momentum_stability_dir",
                "data/experiments/S02-CP08-momentum-stability-v1",
            ),
            root,
        ),
        parameter_robustness_dir=_resolve(
            forecast.get(
                "parameter_robustness_dir",
                "data/experiments/S02-CP08-parameter-robustness-v1",
            ),
            root,
        ),
        validation_protocol_dir=_resolve(
            forecast.get(
                "validation_protocol_dir",
                "data/experiments/S02-CP08-independent-validation-protocol-v1",
            ),
            root,
        ),
        forward_dataset_dir=_resolve(
            forecast.get("forward_dataset_dir", "data/forward/BTCUSDT-1h-forward-v1"), root
        ),
        forward_config_path=_resolve(
            forecast.get("forward_config_path", "config/forward-collection.yaml"), root
        ),
        decision_glob=forecast.get("decision_glob", "data/experiments/S03-*"),
        default_timeframe=str(display.get("default_timeframe", "1D")),
        default_days=int(display.get("default_days", 3650)),
        default_models=tuple(forecast.get("default_models", ("timestral", "naive"))),
        default_forecast_mode=str(forecast.get("default_mode", "h24_common")),
        verify_checksums=bool(integrity.get("verify_checksums", True)),
        cache_ttl_seconds=int(display.get("cache_ttl_seconds", 300)),
        chart_height=int(display.get("chart_height", 650)),
        colors=colors,
        indicator_presets=normalized_presets,
        regime_vol_bins=(float(vol_bins[0]), float(vol_bins[1])),
        regime_trend_bins=(float(trend_bins[0]), float(trend_bins[1])),
        source_label=str(display.get("source_label", "BTCUSDT · Binance Spot · 1h · UTC")),
    )


def discover_snapshots(data_root: Path, default: Path) -> list[Path]:
    """Return available snapshot directories, default first."""
    candidates: list[Path] = []
    snapshots = data_root / "snapshots"
    if snapshots.is_dir():
        candidates.extend(sorted(path for path in snapshots.iterdir() if path.is_dir()))
    if default not in candidates:
        candidates.insert(0, default)
    return [path for path in candidates if path.is_dir()]
