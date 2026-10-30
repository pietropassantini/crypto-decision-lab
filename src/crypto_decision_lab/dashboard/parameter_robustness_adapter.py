"""Read-only adapter for the S02-CP08-03 parameter robustness archive.

The adapter only *reads* the additive archive written by
``crypto_decision_lab.parameter_robustness``.  It never recomputes a metric and
never writes: the dashboard must show exactly the frozen numbers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

import pandas as pd

REQUIRED_FILES = (
    "scenario_metrics.parquet",
    "threshold_surface.parquet",
    "summary.json",
    "checks.json",
    "manifest.json",
)
OPTIONAL_FILES = (
    "fold_stability.parquet",
    "threshold_selection.parquet",
    "concentration.json",
)


class RobustnessArchiveError(ValueError):
    """Raised when the CP08-03 archive is missing or inconsistent."""


@dataclass(frozen=True)
class RobustnessArchive:
    directory: Path
    scenarios: pd.DataFrame
    surface: pd.DataFrame
    summary: dict[str, Any]
    manifest: dict[str, Any]
    checks: tuple[dict[str, Any], ...]
    folds: pd.DataFrame
    selection: pd.DataFrame
    concentration: dict[str, Any]
    issues: tuple[str, ...] = field(default_factory=tuple)

    @property
    def available(self) -> bool:
        return not self.scenarios.empty

    @property
    def initial_capital(self) -> float:
        return float(self.manifest.get("initial_capital", 10_000.0))

    def family(self, name: str) -> pd.DataFrame:
        return self.scenarios[self.scenarios["family"] == name].reset_index(drop=True)

    def scenario(self, name: str) -> pd.Series | None:
        subset = self.scenarios[self.scenarios["scenario"] == name]
        return subset.iloc[0] if len(subset) else None

    def surface_scope(self, scope: str) -> pd.DataFrame:
        return self.surface[self.surface["scope"] == scope].reset_index(drop=True)

    def fold_stability(self, scenario: str) -> pd.DataFrame:
        if self.folds.empty:
            return self.folds
        return self.folds[self.folds["scenario"] == scenario].sort_values("fold")

    @property
    def primary(self) -> pd.Series | None:
        subset = self.scenarios[self.scenarios["primary"] & (self.scenarios["family"] == "combined")]
        return subset.iloc[0] if len(subset) else None

    @property
    def checks_failed(self) -> tuple[str, ...]:
        return tuple(
            str(check.get("id")) for check in self.checks if check.get("status") == "FAIL"
        )


def load_robustness_archive(directory: str | Path) -> RobustnessArchive:
    """Load the CP08-03 archive, validating the mandatory files."""

    root = Path(directory)
    if not root.is_dir():
        raise RobustnessArchiveError(f"Archivio S02-CP08-03 non trovato: {root}")
    missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    if missing:
        raise RobustnessArchiveError(
            "Archivio S02-CP08-03 incompleto: " + ", ".join(missing)
        )

    scenarios = pd.read_parquet(root / "scenario_metrics.parquet")
    surface = pd.read_parquet(root / "threshold_surface.parquet")
    summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    checks = tuple(json.loads((root / "checks.json").read_text(encoding="utf-8")))

    folds = pd.DataFrame()
    selection = pd.DataFrame()
    concentration: dict[str, Any] = {}
    issues: list[str] = []
    if (root / "fold_stability.parquet").is_file():
        folds = pd.read_parquet(root / "fold_stability.parquet")
    else:
        issues.append("fold_stability.parquet assente: stabilità per fold non disponibile")
    if (root / "threshold_selection.parquet").is_file():
        selection = pd.read_parquet(root / "threshold_selection.parquet")
    else:
        issues.append("threshold_selection.parquet assente: soglie selezionate non disponibili")
    if (root / "concentration.json").is_file():
        concentration = json.loads((root / "concentration.json").read_text(encoding="utf-8"))
    else:
        issues.append("concentration.json assente: concentrazione non disponibile")

    if manifest.get("source_hashes_unchanged") is not True:
        issues.append("manifest: source_hashes_unchanged non è true")
    if manifest.get("original_artifacts_modified") is not False:
        issues.append("manifest: original_artifacts_modified non è false")
    if manifest.get("strategic_gate") != "INCONCLUSIVE":
        issues.append("manifest: strategic_gate atteso INCONCLUSIVE")
    if manifest.get("promotion") != "NO-GO":
        issues.append("manifest: promotion atteso NO-GO")
    failed = [check.get("id") for check in checks if check.get("status") == "FAIL"]
    if failed:
        issues.append("check falliti: " + ", ".join(str(item) for item in failed))
    return RobustnessArchive(
        directory=root.resolve(),
        scenarios=scenarios,
        surface=surface,
        summary=summary,
        manifest=manifest,
        checks=checks,
        folds=folds,
        selection=selection,
        concentration=concentration,
        issues=tuple(issues),
    )
