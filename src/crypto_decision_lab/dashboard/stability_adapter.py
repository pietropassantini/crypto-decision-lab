"""Read-only adapter for the S02-CP08-02 momentum stability archive.

The adapter only *reads* the additive archive written by
``crypto_decision_lab.momentum_stability``.  It never recomputes metrics and
never writes: the dashboard must show exactly the frozen numbers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

import pandas as pd

REQUIRED_FILES = (
    "trade_attribution.parquet",
    "period_summary.parquet",
    "concentration_summary.json",
    "drawdown_analysis.json",
    "manifest.json",
)
OPTIONAL_FILES = (
    "regime_breakdown.parquet",
    "equity_curves.parquet",
    "streak_analysis.json",
)


class StabilityArchiveError(ValueError):
    """Raised when the CP08-02 archive is missing or inconsistent."""


@dataclass(frozen=True)
class StabilityArchive:
    directory: Path
    trades: pd.DataFrame
    periods: pd.DataFrame
    equity: pd.DataFrame
    regimes: pd.DataFrame
    concentration: dict[str, Any]
    drawdown: dict[str, Any]
    streaks: dict[str, Any]
    manifest: dict[str, Any]
    issues: tuple[str, ...] = field(default_factory=tuple)

    @property
    def available(self) -> bool:
        return not self.trades.empty

    @property
    def initial_capital(self) -> float:
        return float(self.manifest.get("initial_capital", 10_000.0))

    def period(self, granularity: str) -> pd.DataFrame:
        return self.periods[self.periods["granularity"] == granularity].sort_values("period_start")

    def regime(self, granularity: str) -> pd.DataFrame:
        return self.regimes[self.regimes["granularity"] == granularity]

    @property
    def concentration_curve(self) -> pd.DataFrame:
        curve = pd.DataFrame(self.concentration.get("cumulative_share_curve", []))
        return curve


def load_stability_archive(directory: str | Path) -> StabilityArchive:
    """Load the CP08-02 archive, validating the mandatory files."""

    root = Path(directory)
    if not root.is_dir():
        raise StabilityArchiveError(f"Archivio S02-CP08-02 non trovato: {root}")
    missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    if missing:
        raise StabilityArchiveError(
            "Archivio S02-CP08-02 incompleto: " + ", ".join(missing)
        )

    trades = pd.read_parquet(root / "trade_attribution.parquet")
    periods = pd.read_parquet(root / "period_summary.parquet")
    for column in ("cutoff", "exit_timestamp"):
        if column in trades.columns:
            trades[column] = pd.to_datetime(trades[column], utc=True)
    for column in ("period_start", "period_end"):
        periods[column] = pd.to_datetime(periods[column], utc=True)

    equity = pd.DataFrame(columns=["curve", "timestamp", "equity", "drawdown_pct"])
    regimes = pd.DataFrame(columns=["granularity", "bucket"])
    streaks: dict[str, Any] = {}
    issues: list[str] = []
    if (root / "equity_curves.parquet").is_file():
        equity = pd.read_parquet(root / "equity_curves.parquet")
        equity["timestamp"] = pd.to_datetime(equity["timestamp"], utc=True)
    else:
        issues.append("equity_curves.parquet assente: grafici equity/underwater non disponibili")
    if (root / "regime_breakdown.parquet").is_file():
        regimes = pd.read_parquet(root / "regime_breakdown.parquet")
    else:
        issues.append("regime_breakdown.parquet assente: breakdown per regime non disponibile")
    if (root / "streak_analysis.json").is_file():
        streaks = json.loads((root / "streak_analysis.json").read_text(encoding="utf-8"))
    else:
        issues.append("streak_analysis.json assente: analisi delle sequenze non disponibile")

    concentration = json.loads((root / "concentration_summary.json").read_text(encoding="utf-8"))
    drawdown = json.loads((root / "drawdown_analysis.json").read_text(encoding="utf-8"))
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("source_hashes_unchanged") is not True:
        issues.append("manifest: source_hashes_unchanged non è true")
    if isinstance(manifest.get("reconciliation"), dict):
        if manifest["reconciliation"].get("all_match") is not True:
            issues.append("manifest: reconciliation.all_match non è true")
    return StabilityArchive(
        directory=root.resolve(),
        trades=trades,
        periods=periods,
        equity=equity,
        regimes=regimes,
        concentration=concentration,
        drawdown=drawdown,
        streaks=streaks,
        manifest=manifest,
        issues=tuple(issues),
    )
