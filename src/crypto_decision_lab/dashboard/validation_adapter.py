"""Read-only adapter for the S02-CP08-04 independent validation protocol archive.

The adapter only *reads* the additive archive written by
``crypto_decision_lab.independent_validation``.  It never recomputes a metric,
never writes and never consumes independent data: the dashboard shows exactly
the frozen protocol, the registered periods and the current (non-)verdict.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

REQUIRED_FILES = (
    "summary.json",
    "criteria.json",
    "data_registry.json",
    "checks.json",
    "manifest.json",
)
OPTIONAL_FILES = (
    "protocol.json",
    "frozen_benchmark.json",
    "confirmation_engine.json",
    "anti_reuse.json",
    "momentum-independent-validation-v1.yaml",
    "momentum-confirmation-engine-v1.yaml",
)


class ValidationArchiveError(ValueError):
    """Raised when the CP08-04 archive is missing or inconsistent."""


@dataclass(frozen=True)
class ValidationProtocolArchive:
    directory: Path
    summary: dict[str, Any]
    criteria: dict[str, Any]
    data_registry: dict[str, Any]
    checks: tuple[dict[str, Any], ...]
    manifest: dict[str, Any]
    protocol: dict[str, Any]
    benchmark: dict[str, Any]
    engine: dict[str, Any]
    anti_reuse: dict[str, Any]
    issues: tuple[str, ...] = field(default_factory=tuple)

    @property
    def protocol_version(self) -> str:
        return str(self.protocol.get("protocol_meta", {}).get("protocol_version", "n/d"))

    @property
    def protocol_id(self) -> str:
        return str(self.protocol.get("protocol_meta", {}).get("protocol_id", "n/d"))

    @property
    def canonical_sha256(self) -> str | None:
        return self.protocol.get("protocol_meta", {}).get("canonical_sha256")

    @property
    def checks_failed(self) -> tuple[str, ...]:
        return tuple(
            str(check.get("id")) for check in self.checks if check.get("status") == "FAIL"
        )

    @property
    def technical_gate(self) -> str:
        return str(self.summary.get("technical_gate", "n/d"))

    @property
    def strategic_gate(self) -> str:
        return str(self.summary.get("strategic_gate", "n/d"))

    @property
    def verdict(self) -> str:
        return str(self.summary.get("independent_validation", {}).get("verdict", "n/d"))

    @property
    def data_available(self) -> bool:
        return bool(self.summary.get("independent_validation", {}).get("data_available", False))

    @property
    def registry_state(self) -> dict[str, Any]:
        return dict(self.summary.get("registry_state", {}))

    def checks_by_status(self, status: str) -> list[dict[str, Any]]:
        return [check for check in self.checks if check.get("status") == status]


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_validation_archive(directory: str | Path) -> ValidationProtocolArchive:
    """Load the CP08-04 archive, validating mandatory files and the gate."""

    root = Path(directory)
    if not root.is_dir():
        raise ValidationArchiveError(f"Archivio S02-CP08-04 non trovato: {root}")
    missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    if missing:
        raise ValidationArchiveError(
            "Archivio S02-CP08-04 incompleto: " + ", ".join(missing)
        )

    summary = _read_json(root / "summary.json")
    criteria = _read_json(root / "criteria.json")
    data_registry = _read_json(root / "data_registry.json")
    checks = tuple(json.loads((root / "checks.json").read_text(encoding="utf-8")))
    manifest = _read_json(root / "manifest.json")
    protocol = _read_json(root / "protocol.json") if (root / "protocol.json").is_file() else {}
    benchmark = (
        _read_json(root / "frozen_benchmark.json")
        if (root / "frozen_benchmark.json").is_file()
        else {}
    )
    engine = (
        _read_json(root / "confirmation_engine.json")
        if (root / "confirmation_engine.json").is_file()
        else {}
    )
    anti_reuse = (
        _read_json(root / "anti_reuse.json")
        if (root / "anti_reuse.json").is_file()
        else {}
    )

    issues: list[str] = []
    if not protocol:
        issues.append("protocol.json assente: configurazione non mostrabile")
    if not benchmark:
        issues.append("frozen_benchmark.json assente: riconciliazione non mostrabile")
    if not engine:
        issues.append("confirmation_engine.json assente: specifica engine non mostrabile")
    if manifest.get("frozen_artifacts_unchanged") is not True:
        issues.append("manifest: frozen_artifacts_unchanged non è true")
    if manifest.get("original_artifacts_modified") is not False:
        issues.append("manifest: original_artifacts_modified non è false")
    if manifest.get("strategic_gate") != "NO-GO":
        issues.append("manifest: strategic_gate atteso NO-GO")
    if manifest.get("independent_validation_status") != "NOT_EVALUATED":
        issues.append("manifest: lo stato atteso è NOT_EVALUATED (nessun dato indipendente)")
    failed = [check.get("id") for check in checks if check.get("status") == "FAIL"]
    if failed:
        issues.append("check falliti: " + ", ".join(str(item) for item in failed))
    if bool(summary.get("independent_validation", {}).get("data_available")):
        issues.append(
            "summary dichiara dati indipendenti disponibili: verificare il registro, "
            "la dashboard non esegue valutazioni"
        )
    return ValidationProtocolArchive(
        directory=root.resolve(),
        summary=summary,
        criteria=criteria,
        data_registry=data_registry,
        checks=checks,
        manifest=manifest,
        protocol=protocol,
        benchmark=benchmark,
        engine=engine,
        anti_reuse=anti_reuse,
        issues=tuple(issues),
    )
