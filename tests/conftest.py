"""Shared fixtures: a minimal repository for Strategy Studio (S04) tests."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

STUDIO_RESEARCH_ID = "BTCUSDT-1h-raw-v1"
STUDIO_FORWARD_ID = "BTCUSDT-1h-forward-v1"


def _write_chunk(path: Path, opens: np.ndarray, start: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    index = pd.date_range(start, periods=len(opens), freq="1h", tz="UTC")
    frame = pd.DataFrame(
        {"timestamp": index, "open": opens, "high": opens + 1, "low": opens - 1,
         "close": opens + 0.5, "volume": np.full(len(opens), 5.0)}
    )
    frame.to_parquet(path, index=False)


def make_studio_repo(root: Path, *, hours: int = 96) -> Path:
    """Research snapshot (oscillating prices), reserved forward archive, catalog."""

    opens = 100.0 + 10.0 * np.sin(np.arange(hours) / 6.0) + np.arange(hours) * 0.1
    research_chunk = root / "data" / "chunks" / "BTCUSDT" / "research.parquet"
    _write_chunk(research_chunk, opens, "2024-01-30")
    snapshot = root / "data" / "snapshots" / STUDIO_RESEARCH_ID
    snapshot.mkdir(parents=True)
    (snapshot / "raw-manifest.json").write_text(
        json.dumps({"dataset_id": STUDIO_RESEARCH_ID, "chunks": [
            {"file": "chunks/BTCUSDT/research.parquet", "sha256": hashlib.sha256(research_chunk.read_bytes()).hexdigest()}
        ]}),
        encoding="utf-8",
    )
    forward = root / "data" / "forward" / STUDIO_FORWARD_ID
    forward_chunk = forward / "chunks" / "chunk-0001.parquet"
    _write_chunk(forward_chunk, np.linspace(200.0, 220.0, 24), "2026-10-23")
    (forward / "manifests").mkdir(parents=True)
    (forward / "manifests" / "dataset-manifest-0001.json").write_text(
        json.dumps({"dataset_id": STUDIO_FORWARD_ID,
                    "chunks": [{"data_sha256": hashlib.sha256(forward_chunk.read_bytes()).hexdigest()}]}),
        encoding="utf-8",
    )
    (root / "config").mkdir(exist_ok=True)
    (root / "config" / "dataset-catalog.json").write_text(
        json.dumps({
            "schema_version": "cdl.s02.catalog.v1",
            "sources": [
                {"dataset_id": STUDIO_RESEARCH_ID, "path": f"data/snapshots/{STUDIO_RESEARCH_ID}", "role": "research"},
                {"dataset_id": STUDIO_FORWARD_ID, "path": f"data/forward/{STUDIO_FORWARD_ID}", "role": "reserved-forward"},
            ],
            "reserved_dataset_ids": [STUDIO_FORWARD_ID],
        }),
        encoding="utf-8",
    )
    return root


@pytest.fixture()
def studio_repo(tmp_path: Path) -> Path:
    return make_studio_repo(tmp_path / "repo")
