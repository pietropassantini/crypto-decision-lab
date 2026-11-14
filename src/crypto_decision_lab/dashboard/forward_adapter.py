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
    audit_report: dict[str, Any] | None = field(default=None)

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

    @property
    def audit_status(self) -> str:
        """Short audit status label for the UI."""
        if self.audit_report is None:
            return "NO_AUDIT"
        return str(self.audit_report.get("status", "NO_AUDIT"))

    @property
    def audit_gate(self) -> str:
        """CP08-07 gate result."""
        if self.audit_report is None:
            return "NO_AUDIT"
        return str(self.audit_report.get("gate", "NO_AUDIT"))

    @property
    def audit_failures(self) -> list[dict[str, Any]]:
        """List of failed checks from the audit report."""
        if self.audit_report is None:
            return []
        return list(self.audit_report.get("failures", []))

    @property
    def audit_lag_hours(self) -> float | None:
        """Lag in hours from the audit report."""
        if self.audit_report is None:
            return None
        return self.audit_report.get("metrics", {}).get("lag_hours")

    @property
    def audit_checked_utc(self) -> str | None:
        """Timestamp when the audit was last run."""
        if self.audit_report is None:
            return None
        return self.audit_report.get("checked_utc")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_audit_report(
    directory: Path,
    monitoring_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Read the latest CP08-07 audit report if available.

    Returns ``None`` when no audit report exists.  The function never writes
    and never touches the forward archive itself.
    """

    manifest_dir = directory / "manifests"
    latest_file = manifest_dir / "latest.json"
    current_manifest: dict[str, Any] = {}
    current_manifest_hash: str | None = None
    current_manifest_version: str | None = None
    current_manifest_path: str | None = None

    if latest_file.is_file():
        try:
            current_manifest = _read_json(latest_file)
            manifest_name = current_manifest.get("manifest", "")
            current_manifest_hash = current_manifest.get("sha256")
            current_manifest_version = manifest_name
            current_manifest_path = str(manifest_dir / manifest_name)
        except Exception:
            pass

    # Locate the latest audit report in the monitoring directory.
    # The shell script writes it to ``data/monitoring/forward/latest-audit.json``
    # or ``var/monitoring/forward/latest-audit.json`` depending on setup.
    audit_path: Path | None = None
    if monitoring_dir is not None and monitoring_dir.is_dir():
        candidate = monitoring_dir / "latest-audit.json"
        if candidate.is_file():
            audit_path = candidate
    else:
        # Fallback: look in common monitoring locations relative to the repo root.
        # directory is like ``data/forward/…`` → parent is ``data/forward`` →
        # grandparent is ``data`` where monitoring lives.
        if directory.parent.name == "forward":
            data_root = directory.parent.parent
        else:
            data_root = directory.parent
        for candidate in (
            data_root / "monitoring" / "forward" / "latest-audit.json",
            data_root / "var" / "monitoring" / "forward" / "latest-audit.json",
            data_root / "monitoring" / "latest-audit.json",
            data_root / "var" / "monitoring" / "latest-audit.json",
            Path.cwd() / "data" / "monitoring" / "forward" / "latest-audit.json",
        ):
            if candidate.is_file():
                audit_path = candidate
                break

    if audit_path is None or not audit_path.is_file():
        # No audit report found — return a minimal structure.
        return {
            "schema_version": "cdl.cp08.forward_monitoring.v1",
            "status": "NO_AUDIT",
            "checked_utc": None,
            "manifest_current": {
                "version": current_manifest_version,
                "sha256": current_manifest_hash,
                "path": current_manifest_path,
            },
            "manifest_audit": None,
            "manifest_match": False,
            "gate": "NO_AUDIT",
            "failures": [],
            "metrics": {},
        }

    try:
        audit_report = _read_json(audit_path)
    except Exception:
        return {
            "schema_version": "cdl.cp08.forward_monitoring.v1",
            "status": "AUDIT_READ_ERROR",
            "checked_utc": None,
            "manifest_current": {
                "version": current_manifest_version,
                "sha256": current_manifest_hash,
                "path": current_manifest_path,
            },
            "manifest_audit": None,
            "manifest_match": False,
            "gate": "AUDIT_READ_ERROR",
            "failures": [],
            "metrics": {},
        }

    audit_manifest = audit_report.get("metrics", {}).get("manifest")
    audit_sha256: str | None = None
    audit_checked_utc: str | None = audit_report.get("checked_utc")
    gate = audit_report.get("gate", "FAIL")
    failures = audit_report.get("failures", [])

    # Try to extract the audit manifest hash from checks.
    for check in audit_report.get("checks", []):
        if check.get("name") == "LATEST_HASH":
            # LATEST_HASH checks that the latest.json file hash matches the
            # sha256 stored inside latest.json — it does not give us the
            # manifest content hash directly.  We fall back to comparing
            # the manifest name.
            pass
        elif check.get("name") == "DATASET_HASH":
            audit_sha256 = check.get("detail", "")

    # Determine the relationship between the audit manifest and the current one.
    manifest_match = False
    if audit_manifest and current_manifest_version:
        if audit_manifest == current_manifest_version:
            manifest_match = True
        elif current_manifest_hash and audit_sha256:
            manifest_match = current_manifest_hash == audit_sha256

    # Determine the audit status label.
    if gate == "PASS" and manifest_match:
        status_label = "AUDIT_CURRENT"
    elif gate == "PASS" and not manifest_match:
        status_label = "AUDIT_STALE"
    elif gate == "FAIL":
        status_label = "AUDIT_FAILED"
    else:
        status_label = "AUDIT_UNKNOWN"

    return {
        "schema_version": audit_report.get("schema_version", "cdl.cp08.forward_monitoring.v1"),
        "status": status_label,
        "checked_utc": audit_checked_utc,
        "manifest_current": {
            "version": current_manifest_version,
            "sha256": current_manifest_hash,
            "path": current_manifest_path,
        },
        "manifest_audit": {
            "version": audit_manifest,
            "sha256": audit_sha256,
        },
        "manifest_match": manifest_match,
        "gate": gate,
        "failures": failures,
        "metrics": audit_report.get("metrics", {}),
    }


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

    # Load the CP08-07 audit report (read-only, may return None).
    audit_report = _load_audit_report(directory)

    return ForwardDataset(
        directory=directory,
        status=status,
        readiness=readiness,
        chunks=tuple(view.selected()),
        quarantine=tuple(view.quarantined),
        events=tuple(archive.events()),
        issues=tuple(dict.fromkeys(issues)),
        audit_report=audit_report,
    )
