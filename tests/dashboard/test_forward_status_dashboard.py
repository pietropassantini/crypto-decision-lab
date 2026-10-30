"""Tests for the read-only S02-CP08-05 dashboard adapter, charts and tab."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pandas as pd
import pytest
import yaml

from crypto_decision_lab import forward_collection as fc
from crypto_decision_lab.dashboard.charts import (
    make_forward_coverage_figure,
    make_forward_readiness_figure,
)
from crypto_decision_lab.dashboard.config import load_config
from crypto_decision_lab.dashboard.forward_adapter import (
    ForwardArchiveError,
    load_forward_dataset,
)
from crypto_decision_lab.market import HOUR_MS, normalize_klines

REPO = Path(__file__).resolve().parents[2]
BOUNDARY = pd.Timestamp("2026-10-23T16:00:00Z")
BOUNDARY_MS = int(BOUNDARY.timestamp() * 1000)
FORBIDDEN = ("total_return_pct", "profit_factor_usdt", "max_drawdown_mtm_pct", "equity", "signal")


def _frame(start_ms: int, end_ms: int, *, skip: set[int] | None = None) -> pd.DataFrame:
    skip = skip or set()
    rows = []
    for value in range(start_ms, end_ms, HOUR_MS):
        if value in skip:
            continue
        price = 100.0 + (value - start_ms) / HOUR_MS
        rows.append([value, f"{price}", f"{price + 1}", f"{price - 1}", f"{price + 0.5}", "10",
                     value + HOUR_MS - 1, "1010", 12, "4", "404", "0"])
    return normalize_klines(rows)


def _config(repo: Path) -> Path:
    payload = {
        "dataset": {"dataset_id": fc.DATASET_ID, "symbol": "BTCUSDT", "interval": "1h", "chunk_hours": 24},
        "archive": {"root": "data/forward"},
        "source": {"provider": "binance-spot", "source_id": "binance-spot"},
        "acquisition": {"pause_seconds": 0, "max_attempts": 3, "backoff_seconds": [0, 0, 0]},
        "quality": {"zero_volume_requires_review": True, "flat_ohlc_requires_review": True},
        "windows": {"stride_hours": 24},
        "readiness": {"min_windows": 300, "min_folds": 5, "min_days": 180, "min_trades": 100},
        "data_root": "data",
        "snapshot_manifest": "data/snapshots/BTCUSDT-1h-raw-v1/raw-manifest.json",
        "protocol_archive_manifest": "data/experiments/S02-CP08-independent-validation-protocol-v1/manifest.json",
    }
    path = repo / "config" / "forward-collection.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    protocols = repo / "config" / "protocols"
    protocols.mkdir(parents=True)
    for name in ("momentum-independent-validation-v1.yaml", "momentum-confirmation-engine-v1.yaml"):
        for suffix in ("", ".sha256"):
            shutil.copy(REPO / "config" / "protocols" / f"{name}{suffix}", protocols / f"{name}{suffix}")
    archive = repo / "data" / "experiments" / "S02-CP08-independent-validation-protocol-v1"
    archive.mkdir(parents=True)
    shutil.copy(
        REPO / "data" / "experiments" / "S02-CP08-independent-validation-protocol-v1" / "manifest.json",
        archive / "manifest.json",
    )
    snapshot = repo / "data" / "snapshots" / "BTCUSDT-1h-raw-v1"
    chunk_dir = repo / "data" / "chunks" / "BTCUSDT" / "1h"
    snapshot.mkdir(parents=True)
    chunk_dir.mkdir(parents=True)
    for name in ("raw-manifest.json", "anomalies.json"):
        shutil.copy(REPO / "data" / "snapshots" / "BTCUSDT-1h-raw-v1" / name, snapshot / name)
    last_chunk = "20261001T0000_20261023T1600.parquet"
    shutil.copy(REPO / "data" / "chunks" / "BTCUSDT" / "1h" / last_chunk, chunk_dir / last_chunk)
    _config(repo)
    return repo


def _populated(repo: Path, *, hours: int = 48, skip: set[int] | None = None) -> None:
    paths = fc.ForwardPaths(repo_root=repo)
    config = fc.load_config(paths=paths)
    now = (BOUNDARY + pd.Timedelta(hours=hours)).to_pydatetime()
    fc.sync(
        config,
        now=now,
        downloader=lambda s, e, **k: _frame(s, e, skip=skip),
        sleep=lambda _s: None,
    )


@pytest.fixture
def repo(tmp_path) -> Path:
    return _repo(tmp_path)


def test_adapter_reads_status_readiness_and_provenance(repo: Path) -> None:
    _populated(repo, hours=48)
    dataset = load_forward_dataset(repo)
    assert dataset.dataset_id == fc.DATASET_ID
    assert dataset.boundary_utc.startswith("2026-10-23T16:00")
    assert dataset.last_acquired_bar_utc.startswith("2026-10-25T15:00")
    assert dataset.readiness_status == fc.READINESS_NOT_READY
    assert dataset.evaluation_executed is False
    assert dataset.strategic_gate == "NO-GO"
    assert dataset.chunks and dataset.chunks[0]["sha256"]
    assert (dataset.chunks[0]["source"] or {})["source_id"] == "binance-spot"
    assert dataset.isolation["research_use"] == "FORBIDDEN"
    assert dataset.protocol["sidecar_match"] is True


def test_adapter_payload_contains_no_strategy_metrics(repo: Path) -> None:
    _populated(repo, hours=30)
    dataset = load_forward_dataset(repo)
    payload = json.dumps({"status": dataset.status, "readiness": dataset.readiness}).lower()
    for forbidden in FORBIDDEN:
        assert forbidden not in payload


def test_adapter_requires_an_existing_archive(tmp_path, repo: Path) -> None:
    empty = _repo(tmp_path / "empty")
    with pytest.raises(ForwardArchiveError, match="Archivio forward non trovato"):
        load_forward_dataset(empty)


def test_adapter_reports_missing_config(tmp_path) -> None:
    bare = tmp_path / "bare"
    (bare / "config").mkdir(parents=True)
    with pytest.raises(ForwardArchiveError, match="Configurazione forward"):
        load_forward_dataset(bare)


def test_adapter_and_commands_never_write(repo: Path) -> None:
    _populated(repo, hours=48)

    def snapshot() -> dict[str, tuple[int, int]]:
        return {
            str(path): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in sorted(repo.rglob("*"))
            if path.is_file()
        }

    before = snapshot()
    load_forward_dataset(repo)
    config = load_config(repo_root=repo)
    assert config.forward_dataset_dir is not None
    assert snapshot() == before


def test_charts_handle_populated_and_empty_archives(repo: Path) -> None:
    _populated(repo, hours=48)
    dataset = load_forward_dataset(repo)
    coverage = make_forward_coverage_figure(dataset.status, dataset.readiness)
    assert coverage.data
    labels = [annotation.text for annotation in coverage.layout.annotations]
    assert any("boundary congelato" in str(text) for text in labels)
    assert any("ACQUISITO" in str(text) for text in labels)

    requirements = make_forward_readiness_figure(dataset.readiness)
    assert len(requirements.data) == 2

    empty = make_forward_coverage_figure({"boundary_utc": "2026-10-23T16:00:00Z"}, {})
    assert not [text for text in (a.text for a in empty.layout.annotations) if "ACQUISITO" in str(text)]
    assert make_forward_readiness_figure({}).data == ()


def test_charts_show_gaps_and_quarantine(repo: Path) -> None:
    missing = BOUNDARY_MS + 5 * HOUR_MS
    _populated(repo, hours=30, skip={missing})
    dataset = load_forward_dataset(repo)
    # nothing promoted for the only cell: quarantine is visible, coverage empty
    assert dataset.quarantine
    figure = make_forward_coverage_figure(dataset.status, dataset.readiness)
    names = [trace.name for trace in figure.data]
    assert "quarantena" in names


def test_component_renders_read_only(repo: Path) -> None:
    _populated(repo, hours=48)
    from streamlit.testing.v1 import AppTest

    code = (
        "import sys\n"
        f"sys.path.insert(0, r'{(REPO / 'src').as_posix()}')\n"
        "from crypto_decision_lab.dashboard.config import load_config\n"
        "from crypto_decision_lab.dashboard.components.forward_status import render_forward_status\n"
        f"render_forward_status(load_config(repo_root=r'{repo.as_posix()}'))\n"
    )
    app = AppTest.from_string(code, default_timeout=120)
    app.run()
    assert list(app.exception) == []
    assert len(app.tabs) == 4
    assert len(app.metric) == 6
    assert len(app.get("plotly_chart")) == 2
    body = " ".join(str(item.value) for item in app.markdown) + " ".join(
        str(item.value) for item in app.warning
    )
    assert "NOT_EVALUATED" in body or "NO-GO" in body
