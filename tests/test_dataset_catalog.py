"""S02 tests: catalog admission guards (identity, provenance, alias).

CP02.2 evidence: research inputs taken from the forward archive are refused
by path identity, by manifest provenance and by content digest even when the
copy is renamed or registered under a new dataset_id.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab import dataset_catalog as dc

FORWARD_ID = "BTCUSDT-1h-forward-v1"


def write_parquet(path: Path, rows: int = 48, base: float = 100.0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    index = pd.date_range("2026-10-23", periods=rows, freq="1h", tz="UTC")
    open_ = base + np.arange(rows, dtype=float)
    frame = pd.DataFrame(
        {"timestamp": index, "open": open_, "high": open_ + 1, "low": open_ - 1,
         "close": open_ + 0.5, "volume": np.full(rows, 5.0)}
    )
    frame.to_parquet(path, index=False)


def sha256_of(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """Minimal repository layout: research snapshot + forward archive + catalog."""

    # Research snapshot with its own distinct chunks.
    research_dir = tmp_path / "data" / "snapshots" / "BTCUSDT-1h-raw-v1"
    research_dir.mkdir(parents=True)
    write_parquet(tmp_path / "data" / "chunks" / "BTCUSDT" / "research.parquet", base=50.0)
    (research_dir / "raw-manifest.json").write_text(
        json.dumps(
            {
                "dataset_id": "BTCUSDT-1h-raw-v1",
                "chunks": [{"file": "chunks/BTCUSDT/research.parquet", "sha256": sha256_of(tmp_path / "data" / "chunks" / "BTCUSDT" / "research.parquet")}],
            }
        ),
        encoding="utf-8",
    )

    # Reserved forward archive.
    forward_dir = tmp_path / "data" / "forward" / FORWARD_ID
    forward_chunk = forward_dir / "chunks" / "chunk-0001.parquet"
    write_parquet(forward_chunk, base=200.0)
    (forward_dir / "manifests").mkdir(parents=True)
    (forward_dir / "manifests" / "dataset-manifest-0001.json").write_text(
        json.dumps({"dataset_id": FORWARD_ID, "chunks": [{"data_sha256": sha256_of(forward_chunk)}]}),
        encoding="utf-8",
    )

    catalog = {
        "schema_version": dc.CATALOG_SCHEMA,
        "sources": [
            {"dataset_id": "BTCUSDT-1h-raw-v1", "path": "data/snapshots/BTCUSDT-1h-raw-v1", "kind": "snapshot", "role": "research"},
            {"dataset_id": FORWARD_ID, "path": f"data/forward/{FORWARD_ID}", "kind": "snapshot", "role": "reserved-forward"},
        ],
        "reserved_dataset_ids": [FORWARD_ID],
    }
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "dataset-catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    return tmp_path


class TestCatalogLoad:
    def test_load_entries_and_roles(self, repo: Path):
        catalog = dc.Catalog.load(repo / "config" / "dataset-catalog.json")
        assert [entry.dataset_id for entry in catalog.research_entries()] == ["BTCUSDT-1h-raw-v1"]
        assert [entry.dataset_id for entry in catalog.reserved_entries()] == [FORWARD_ID]
        assert catalog.reserved_dataset_ids == (FORWARD_ID,)

    def test_unknown_dataset_id_is_rejected(self, repo: Path):
        catalog = dc.Catalog.load(repo / "config" / "dataset-catalog.json")
        with pytest.raises(dc.CatalogError):
            catalog.entry("nope")

    def test_malformed_catalog_is_rejected(self, tmp_path: Path):
        path = tmp_path / "catalog.json"
        path.write_text(json.dumps({"schema_version": "wrong"}), encoding="utf-8")
        with pytest.raises(dc.CatalogError):
            dc.Catalog.load(path)


class TestAdmission:
    def test_clean_research_source_is_admitted(self, repo: Path):
        catalog = dc.Catalog.load(repo / "config" / "dataset-catalog.json")
        entry = dc.assert_research_admissible(catalog, "BTCUSDT-1h-raw-v1", repo_root=repo)
        assert entry.role == dc.ROLE_RESEARCH

    def test_reserved_entry_is_refused_by_role(self, repo: Path):
        catalog = dc.Catalog.load(repo / "config" / "dataset-catalog.json")
        with pytest.raises(dc.CatalogAdmissionError, match="role"):
            dc.assert_research_admissible(catalog, FORWARD_ID, repo_root=repo)

    def test_forward_path_is_refused_by_identity(self, repo: Path):
        catalog = dc.Catalog.load(repo / "config" / "dataset-catalog.json")
        # A catalog entry that claims to be research but lives under data/forward.
        tampered = dc.Catalog(
            entries=catalog.entries + (dc.CatalogEntry("forward-copy", f"data/forward/{FORWARD_ID}/copy"),),
            reserved_dataset_ids=catalog.reserved_dataset_ids,
        )
        with pytest.raises(dc.CatalogAdmissionError, match="forward"):
            dc.assert_research_admissible(tampered, "forward-copy", repo_root=repo)

    def test_forward_manifest_identity_is_refused_by_provenance(self, repo: Path):
        catalog = dc.Catalog.load(repo / "config" / "dataset-catalog.json")
        # A renamed copy of the forward archive: path is outside data/forward,
        # but the manifest still identifies the reserved dataset.
        copy_dir = repo / "data" / "snapshots" / "forward-renamed"
        copy_dir.mkdir(parents=True)
        forward_chunk = repo / "data" / "forward" / FORWARD_ID / "chunks" / "chunk-0001.parquet"
        renamed_chunk = repo / "data" / "chunks" / "BTCUSDT" / "renamed.parquet"
        renamed_chunk.write_bytes(forward_chunk.read_bytes())
        (copy_dir / "raw-manifest.json").write_text(
            json.dumps(
                {
                    "dataset_id": FORWARD_ID,
                    "chunks": [{"file": "chunks/BTCUSDT/renamed.parquet", "sha256": sha256_of(renamed_chunk)}],
                }
            ),
            encoding="utf-8",
        )
        tampered = dc.Catalog(
            entries=catalog.entries + (dc.CatalogEntry("renamed-forward", "data/snapshots/forward-renamed"),),
            reserved_dataset_ids=catalog.reserved_dataset_ids,
        )
        with pytest.raises(dc.CatalogAdmissionError, match="provenance"):
            dc.assert_research_admissible(tampered, "renamed-forward", repo_root=repo)

    def test_forward_bytes_are_refused_by_alias_digest(self, repo: Path):
        catalog = dc.Catalog.load(repo / "config" / "dataset-catalog.json")
        # Scrubbed manifest (research-looking id, research path) but one chunk
        # is byte-identical to a reserved forward parquet.
        alias_dir = repo / "data" / "snapshots" / "alias-v1"
        alias_dir.mkdir(parents=True)
        forward_chunk = repo / "data" / "forward" / FORWARD_ID / "chunks" / "chunk-0001.parquet"
        alias_chunk = repo / "data" / "chunks" / "BTCUSDT" / "alias.parquet"
        alias_chunk.write_bytes(forward_chunk.read_bytes())
        (alias_dir / "raw-manifest.json").write_text(
            json.dumps(
                {
                    "dataset_id": "alias-v1",
                    "chunks": [{"file": "chunks/BTCUSDT/alias.parquet", "sha256": sha256_of(alias_chunk)}],
                }
            ),
            encoding="utf-8",
        )
        tampered = dc.Catalog(
            entries=catalog.entries + (dc.CatalogEntry("alias-v1", "data/snapshots/alias-v1"),),
            reserved_dataset_ids=catalog.reserved_dataset_ids,
        )
        with pytest.raises(dc.CatalogAdmissionError, match="alias"):
            dc.assert_research_admissible(tampered, "alias-v1", repo_root=repo)

    def test_missing_chunk_fails_closed(self, repo: Path):
        catalog = dc.Catalog.load(repo / "config" / "dataset-catalog.json")
        (repo / "data" / "chunks" / "BTCUSDT" / "research.parquet").unlink()
        with pytest.raises(dc.CatalogError, match="chunk"):
            dc.assert_research_admissible(catalog, "BTCUSDT-1h-raw-v1", repo_root=repo)


class TestLoadMarket:
    def test_load_market_reuses_verified_adapter(self, repo: Path):
        catalog = dc.Catalog.load(repo / "config" / "dataset-catalog.json")
        market = dc.load_market(catalog, "BTCUSDT-1h-raw-v1", repo_root=repo)
        assert market.dataset_id == "BTCUSDT-1h-raw-v1"
        assert len(market.frame) == 48
        assert market.source_files == 1

    def test_load_market_refuses_reserved_source(self, repo: Path):
        catalog = dc.Catalog.load(repo / "config" / "dataset-catalog.json")
        with pytest.raises(dc.CatalogAdmissionError):
            dc.load_market(catalog, FORWARD_ID, repo_root=repo)
