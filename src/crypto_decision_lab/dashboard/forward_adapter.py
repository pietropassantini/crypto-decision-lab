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
    def audit_detail(self) -> str:
        """Why the audit is not simply current (empty when it is)."""
        if self.audit_report is None:
            return ""
        return str(self.audit_report.get("detail", ""))

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


AUDIT_SCHEMA = "cdl.cp08.forward_monitoring.v1"
AUDIT_FILE = "latest-audit.json"


def _audit_candidates(directory: Path, repo_root: Path | None) -> list[Path]:
    """Where the CP08-07 monitor writes ``latest-audit.json``.

    Only locations belonging to the repository are considered: the current
    working directory is never searched, so the state shown depends on the
    checkout and not on where the dashboard was started.
    """

    data_root = directory.parent.parent if directory.parent.name == "forward" else directory.parent
    roots = [data_root / "monitoring" / "forward", data_root / "monitoring"]
    if repo_root is not None:
        roots += [repo_root / "var" / "monitoring" / "forward", repo_root / "var" / "monitoring"]
    return [root / AUDIT_FILE for root in roots]


def _current_manifest(directory: Path) -> dict[str, Any]:
    latest_file = directory / "manifests" / "latest.json"
    version = sha256 = path = None
    if latest_file.is_file():
        try:
            latest = _read_json(latest_file)
            version = latest.get("manifest") or None
            sha256 = latest.get("sha256") or None
            path = str(directory / "manifests" / version) if version else None
        except (OSError, ValueError, AttributeError):
            pass
    return {"version": version, "sha256": sha256, "path": path}


def _audit_result(status: str, current: dict[str, Any], *, detail: str = "", checked_utc: str | None = None,
                  audit_version: str | None = None, match: bool = False, gate: str | None = None,
                  failures: list[dict[str, Any]] | None = None, metrics: dict[str, Any] | None = None,
                  schema: str = AUDIT_SCHEMA) -> dict[str, Any]:
    return {
        "schema_version": schema,
        "status": status,
        "detail": detail,
        "checked_utc": checked_utc,
        "manifest_current": current,
        "manifest_audit": {"version": audit_version, "sha256": None} if audit_version else None,
        "manifest_match": match,
        "gate": gate or status,
        "failures": failures or [],
        "metrics": metrics or {},
    }


def _load_audit_report(directory: Path, repo_root: Path | None = None) -> dict[str, Any]:
    """Classify the latest CP08-07 audit report relative to the current manifest.

    The result always carries a ``status``; success is only ``AUDIT_CURRENT``,
    which requires gate ``PASS``, no failed check and an audit run on the
    manifest that is current now (matched by manifest version: the audit report
    does not record manifest hashes).  Nothing is written.

    * ``AUDIT_CURRENT``    gate PASS on the current manifest;
    * ``AUDIT_STALE``      gate PASS but on a previous (or unidentifiable) manifest;
    * ``NO_AUDIT``         no report found;
    * ``AUDIT_FAILED``     gate FAIL (failed checks listed when the report has them);
    * ``AUDIT_READ_ERROR`` report unreadable or malformed;
    * ``AUDIT_UNKNOWN``    unrecognised gate or a PASS report that lists failures.
    """

    current = _current_manifest(directory)
    audit_path = next((path for path in _audit_candidates(directory, repo_root) if path.is_file()), None)
    if audit_path is None:
        return _audit_result("NO_AUDIT", current, detail="nessun report latest-audit.json trovato")
    try:
        raw = _read_json(audit_path)
    except (OSError, ValueError) as exc:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        return _audit_result("AUDIT_READ_ERROR", current, detail=f"{audit_path.name} non leggibile: {exc}")
    if not isinstance(raw, dict):
        return _audit_result("AUDIT_READ_ERROR", current, detail=f"{audit_path.name} non è un oggetto JSON")
    failures_raw = raw.get("failures", [])
    if not isinstance(failures_raw, list):
        return _audit_result("AUDIT_READ_ERROR", current, detail="il campo 'failures' del report non è una lista")
    failures = [item for item in failures_raw if isinstance(item, dict)]
    metrics = raw.get("metrics") if isinstance(raw.get("metrics"), dict) else {}
    lag = metrics.get("lag_hours")
    if isinstance(lag, bool) or not isinstance(lag, (int, float)) or lag != lag or lag in (float("inf"), float("-inf")):
        lag = None
    metrics = {**metrics, "lag_hours": lag}
    audit_version = metrics.get("manifest") if isinstance(metrics.get("manifest"), str) else None
    checked = raw.get("checked_utc") if isinstance(raw.get("checked_utc"), str) else None
    match = bool(audit_version and current["version"] and audit_version == current["version"])
    gate = raw.get("gate") if isinstance(raw.get("gate"), str) else None
    common = dict(checked_utc=checked, audit_version=audit_version, match=match, gate=gate, failures=failures,
                  metrics=metrics, schema=str(raw.get("schema_version", AUDIT_SCHEMA)))
    if gate == "PASS":
        if failures:
            return _audit_result("AUDIT_UNKNOWN", current, detail="gate PASS ma il report elenca controlli falliti",
                                 **common)
        if not match:
            which = audit_version or "non identificabile"
            return _audit_result("AUDIT_STALE", current, detail=f"audit sul manifest {which}", **common)
        return _audit_result("AUDIT_CURRENT", current, **common)
    if gate == "FAIL":
        detail = "" if failures else "il report non elenca i controlli falliti"
        return _audit_result("AUDIT_FAILED", current, detail=detail, **common)
    return _audit_result("AUDIT_UNKNOWN", current, detail=f"gate non riconosciuto: {gate!r}", **common)


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
    audit_report = _load_audit_report(directory, root)

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
