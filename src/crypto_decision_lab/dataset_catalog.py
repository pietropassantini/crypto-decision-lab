"""S02: catalog of admissible data sources for the research pipeline.

A source is admissible for research (Dataset Builder, training, backtests)
only when ALL of these guards pass:

* **role guard** — the catalog entry is registered with role ``research``;
  reserved forward collections are listed with role ``reserved-forward``;
* **identity guard** — the source path does not live inside a forward
  archive (``data/forward/...``) in any checkout (:mod:`.forward_isolation`);
* **provenance guard** — the snapshot manifest does not identify a reserved
  forward dataset (a copy of the forward archive registered under a new
  dataset_id is still refused);
* **alias guard** — no chunk file of the source is byte-identical (same
  SHA-256) to a Parquet file of a reserved forward collection, so renamed or
  relocated copies of the reserved data cannot enter research through the
  catalog.

Failures raise :class:`CatalogAdmissionError` (a hard failure, never a
warning).  Market loading reuses the read-only, checksum-verifying adapter
:func:`crypto_decision_lab.dashboard.data_loader.load_market_data`.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Iterator

from .forward_isolation import ForwardIsolationViolation, guard_research_path

CATALOG_SCHEMA = "cdl.s02.catalog.v1"
ROLE_RESEARCH = "research"
ROLE_RESERVED_FORWARD = "reserved-forward"
KIND_SNAPSHOT = "snapshot"
DEFAULT_CATALOG_PATH = Path("config/dataset-catalog.json")


class CatalogError(ValueError):
    """Raised when the catalog file or an entry is malformed."""


class CatalogAdmissionError(CatalogError):
    """Raised when a source is not admissible for the research pipeline."""


def _digest_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _hex_digests(payload: Any) -> Iterator[str]:
    """Yield every sha256-looking string embedded in a JSON structure."""

    if isinstance(payload, str):
        if len(payload) == 64 and all(char in "0123456789abcdef" for char in payload):
            yield payload
    elif isinstance(payload, dict):
        for value in payload.values():
            yield from _hex_digests(value)
    elif isinstance(payload, (list, tuple)):
        for value in payload:
            yield from _hex_digests(value)


@dataclass(frozen=True)
class CatalogEntry:
    dataset_id: str
    path: str
    kind: str = KIND_SNAPSHOT
    role: str = ROLE_RESEARCH
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.dataset_id:
            raise CatalogError("catalog entries require a dataset_id")
        if self.role not in (ROLE_RESEARCH, ROLE_RESERVED_FORWARD):
            raise CatalogError(f"unknown role for {self.dataset_id}: {self.role}")


@dataclass(frozen=True)
class Catalog:
    entries: tuple[CatalogEntry, ...]
    reserved_dataset_ids: tuple[str, ...] = ("BTCUSDT-1h-forward-v1",)

    @classmethod
    def load(cls, path: str | Path = DEFAULT_CATALOG_PATH) -> "Catalog":
        catalog_path = Path(path)
        if not catalog_path.is_file():
            raise CatalogError(f"catalog file not found: {catalog_path}")
        try:
            raw = json.loads(catalog_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CatalogError(f"catalog file is not valid JSON: {catalog_path}") from exc
        if raw.get("schema_version") != CATALOG_SCHEMA:
            raise CatalogError(f"unsupported catalog schema: {raw.get('schema_version')}")
        sources = raw.get("sources")
        if not isinstance(sources, list) or not sources:
            raise CatalogError("catalog requires a non-empty 'sources' list")
        entries = []
        for item in sources:
            if not isinstance(item, dict) or "dataset_id" not in item or "path" not in item:
                raise CatalogError(f"catalog entries require dataset_id and path: {item}")
            fields = CatalogEntry.__dataclass_fields__
            entries.append(CatalogEntry(**{key: item[key] for key in fields if key in item}))
        entries_tuple = tuple(entries)
        ids = [entry.dataset_id for entry in entries]
        if len(ids) != len(set(ids)):
            raise CatalogError("duplicate dataset_id values in catalog")
        reserved = tuple(raw.get("reserved_dataset_ids") or ())
        if not reserved:
            reserved = tuple(entry.dataset_id for entry in entries if entry.role == ROLE_RESERVED_FORWARD)
        return cls(entries=entries_tuple, reserved_dataset_ids=reserved)

    def entry(self, dataset_id: str) -> CatalogEntry:
        for entry in self.entries:
            if entry.dataset_id == dataset_id:
                return entry
        raise CatalogError(f"dataset_id not in catalog: {dataset_id}")

    def research_entries(self) -> tuple[CatalogEntry, ...]:
        return tuple(entry for entry in self.entries if entry.role == ROLE_RESEARCH)

    def reserved_entries(self) -> tuple[CatalogEntry, ...]:
        return tuple(entry for entry in self.entries if entry.role == ROLE_RESERVED_FORWARD)


def snapshot_manifest(snapshot_dir: Path) -> dict[str, Any]:
    manifest_path = snapshot_dir / "raw-manifest.json"
    if not manifest_path.is_file():
        raise CatalogError(f"snapshot manifest missing: {manifest_path}")
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CatalogError(f"snapshot manifest is not valid JSON: {manifest_path}") from exc


def _source_parquet_digests(snapshot_dir: Path, data_root: Path) -> set[str]:
    """Digests of the Parquet chunks referenced by a snapshot manifest."""

    manifest = snapshot_manifest(snapshot_dir)
    chunks = manifest.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise CatalogError(f"snapshot manifest contains no chunks: {snapshot_dir}")
    digests: set[str] = set()
    for chunk in chunks:
        relative = chunk.get("file") if isinstance(chunk, dict) else None
        if not relative:
            raise CatalogError(f"malformed chunk entry in {snapshot_dir / 'raw-manifest.json'}")
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise CatalogError(f"unsafe chunk path in snapshot manifest: {relative}")
        path = data_root / candidate
        if not path.is_file():
            raise CatalogError(f"referenced chunk does not exist: {path}")
        digests.add(_digest_file(path))
    return digests


def _reserved_digests(reserved_entries: tuple[CatalogEntry, ...], repo_root: Path) -> set[str]:
    """Every digest registered or stored by the reserved forward collections."""

    digests: set[str] = set()
    for entry in reserved_entries:
        root = (repo_root / entry.path).resolve() if not Path(entry.path).is_absolute() else Path(entry.path)
        if not root.is_dir():
            continue  # fresh checkout without the forward archive: identity/provenance guards still apply
        for manifest_path in sorted(root.rglob("*.json")):
            try:
                digests.update(_hex_digests(json.loads(manifest_path.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, OSError):
                continue
        for parquet_path in sorted(root.rglob("*.parquet")):
            digests.add(_digest_file(parquet_path))
    return digests


def assert_research_admissible(
    catalog: Catalog,
    dataset_id: str,
    *,
    repo_root: str | Path = ".",
) -> CatalogEntry:
    """Refuse anything that is not clean research data. Returns the entry."""

    entry = catalog.entry(dataset_id)
    root = Path(repo_root).resolve()
    if entry.role != ROLE_RESEARCH:
        raise CatalogAdmissionError(
            f"{dataset_id} has role '{entry.role}': only '{ROLE_RESEARCH}' sources feed the research pipeline"
        )
    snapshot_dir = root / entry.path if not Path(entry.path).is_absolute() else Path(entry.path)
    # Identity guard: the same bytes under a forward path are never research input.
    try:
        guard_research_path(snapshot_dir, purpose=f"research source {dataset_id}")
    except ForwardIsolationViolation as exc:
        raise CatalogAdmissionError(str(exc)) from exc
    manifest = snapshot_manifest(snapshot_dir)
    manifest_dataset_id = str(manifest.get("dataset_id") or snapshot_dir.name)
    if manifest_dataset_id in catalog.reserved_dataset_ids:
        raise CatalogAdmissionError(
            f"{dataset_id} provenance violation: snapshot manifest identifies the reserved forward "
            f"dataset {manifest_dataset_id}"
        )
    if str(manifest.get("source_manifest") or "") and any(
        reserved_id in str(manifest.get("source_manifest")) for reserved_id in catalog.reserved_dataset_ids
    ):
        raise CatalogAdmissionError(
            f"{dataset_id} provenance violation: source manifest references a reserved forward collection"
        )
    # Alias guard: byte-identical copies of reserved chunks cannot enter research.
    data_root = root / "data" if (root / "data").is_dir() else root
    source_digests = _source_parquet_digests(snapshot_dir, data_root)
    reserved = _reserved_digests(catalog.reserved_entries(), root)
    collisions = sorted(source_digests & reserved)
    if collisions:
        raise CatalogAdmissionError(
            f"{dataset_id} alias violation: {len(collisions)} chunk digest(s) match the reserved "
            "forward collections (copies or renames of forward data are not research input)"
        )
    return entry


def load_market(
    catalog: Catalog,
    dataset_id: str,
    *,
    repo_root: str | Path = ".",
):
    """Admit a catalog source and load it through the read-only market adapter."""

    from .dashboard.data_loader import load_market_data

    entry = assert_research_admissible(catalog, dataset_id, repo_root=repo_root)
    root = Path(repo_root).resolve()
    snapshot_dir = guard_research_path(root / entry.path, purpose=f"research source {dataset_id}")
    data_root = root / "data" if (root / "data").is_dir() else root
    return load_market_data(data_root, snapshot_dir, verify_checksums=True)


__all__ = [
    "CATALOG_SCHEMA",
    "Catalog",
    "CatalogAdmissionError",
    "CatalogEntry",
    "CatalogError",
    "DEFAULT_CATALOG_PATH",
    "KIND_SNAPSHOT",
    "ROLE_RESEARCH",
    "ROLE_RESERVED_FORWARD",
    "assert_research_admissible",
    "load_market",
]
