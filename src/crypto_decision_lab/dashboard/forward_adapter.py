"""Read-only adapter for the S02-CP08-05 forward collection archive.

The adapter *reads* the forward archive and asks the collection module for its
offline ``status``/``readiness`` payloads.  It never downloads, never writes,
never selects a threshold and never evaluates the strategy: the dashboard is a
window on data availability, not on performance.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
from pathlib import Path
from typing import Any

from .. import forward_collection as fc


class ForwardArchiveError(ValueError):
    """Raised when the forward archive is missing or unreadable."""


@dataclass(frozen=True)
class ForwardDataset:
    directory: Path
    status: dict[str, Any]
    readiness: dict[str, Any]
    chunks: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    quarantine: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    events: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    issues: tuple[str, ...] = field(default_factory=tuple)

    @property
    def dataset_id(self) -> str:
        return str(self.status.get("dataset_id", "n/d"))

    @property
    def readiness_status(self) -> str:
        return str(self.readiness.get("status", "n/d"))

    @property
    def boundary_utc(self) -> str | None:
        return self.status.get("boundary_utc")

    @property
    def last_acquired_bar_utc(self) -> str | None:
        return self.status.get("last_acquired_bar_utc")

    @property
    def last_closed_bar_utc(self) -> str | None:
        return self.status.get("last_closed_bar_utc")

    @property
    def dataset_sha256(self) -> str | None:
        return self.status.get("dataset_sha256")

    @property
    def evaluation_executed(self) -> bool:
        return bool(self.status.get("evaluation_executed", False))

    @property
    def strategic_gate(self) -> str:
        return str(self.status.get("strategic_gate", "NO-GO"))

    @property
    def protocol(self) -> dict[str, Any]:
        return dict(self.readiness.get("protocol", {}))

    @property
    def isolation(self) -> dict[str, Any]:
        return dict(self.readiness.get("isolation", {}))

    @property
    def missing_requirements(self) -> list[dict[str, Any]]:
        return list(self.readiness.get("missing_requirements", []))


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_forward_dataset(
    repo_root: str | Path | None = None,
    *,
    dataset_dir: str | Path | None = None,
    config_path: str | Path | None = None,
) -> ForwardDataset:
    """Load the forward archive read-only.

    ``repo_root`` defaults to the current working directory; the dataset
    directory comes from the collection configuration unless ``dataset_dir`` is
    given explicitly for diagnostics.
    """

    root = Path(repo_root or Path.cwd()).resolve()
    paths = fc.ForwardPaths(repo_root=root)
    selected = Path(config_path).resolve() if config_path else paths.resolve(paths.config_path)
    try:
        # A missing explicit path falls back to the repository default / example.
        config = fc.load_config(selected if selected.is_file() else None, paths=paths)
    except fc.ForwardConfigError as exc:
        raise ForwardArchiveError(
            f"Configurazione forward non disponibile ({exc}). "
            "Copia config/forward-collection.example.yaml in config/forward-collection.yaml."
        ) from exc
    directory = Path(dataset_dir).resolve() if dataset_dir is not None else config.archive_dir
    if not directory.is_dir():
        raise ForwardArchiveError(
            f"Archivio forward non trovato: {directory}. "
            "Esegui `python -m crypto_decision_lab.forward_collection status` per diagnosticare."
        )
    if directory != config.archive_dir:
        config = replace(config, archive_dir=directory)

    archive = fc.ForwardArchive(directory)
    status = fc.status(config, archive=archive, paths=paths)
    readiness = fc.readiness(config, archive=archive, paths=paths)
    view = fc.load_archive_view(archive)
    issues = [item.get("code", "ISSUE") for item in view.issues]
    for item in status.get("integrity_issues", []):
        issues.append(str(item.get("code", "ISSUE")))
    if config.used_example_config:
        issues.append("USING_EXAMPLE_CONFIG")
    return ForwardDataset(
        directory=directory,
        status=status,
        readiness=readiness,
        chunks=tuple(view.selected()),
        quarantine=tuple(view.quarantined),
        events=tuple(archive.events()),
        issues=tuple(dict.fromkeys(issues)),
    )
