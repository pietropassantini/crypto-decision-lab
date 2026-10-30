"""S02-CP08-05: forward collection, isolation and readiness tests.

No test touches the network: the reusable Binance adapter is replaced by
synthetic candle factories, and the few HTTP-level assertions use a fake
``requests.Session``.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
import yaml

from crypto_decision_lab import forward_collection as fc
from crypto_decision_lab.forward_isolation import (
    ForwardIsolationViolation,
    guard_metric_payload,
    guard_research_output,
    guard_research_path,
    is_forward_path,
)
from crypto_decision_lab.market import HOUR_MS, fetch_klines, normalize_klines

REPO = Path(__file__).resolve().parents[1]
BOUNDARY = pd.Timestamp("2026-10-23T16:00:00Z")
BOUNDARY_MS = int(BOUNDARY.timestamp() * 1000)


# ---------------------------------------------------------------------------
# fixtures: synthetic repository + synthetic source
# ---------------------------------------------------------------------------


def candle(open_time_ms: int, price: float = 100.0, *, close_ms: int | None = None) -> list:
    close = price + 1.0
    return [
        open_time_ms,
        f"{price}",
        f"{max(price, close) + 1.0}",
        f"{min(price, close) - 1.0}",
        f"{close}",
        "10",
        close_ms if close_ms is not None else open_time_ms + HOUR_MS - 1,
        "1010",
        12,
        "4",
        "404",
        "0",
    ]


def frame_for(start_ms: int, end_ms: int, *, price: float = 100.0, skip: set[int] | None = None,
              close_override: int | None = None, shift_ms: int = 0) -> pd.DataFrame:
    skip = skip or set()
    rows = []
    for value in range(start_ms, end_ms, HOUR_MS):
        if value in skip:
            continue
        close_ms = close_override if close_override is not None else None
        rows.append(candle(value + shift_ms, price + (value - start_ms) / HOUR_MS, close_ms=close_ms))
    return normalize_klines(rows)


def write_config(repo: Path, **overrides) -> Path:
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
    payload.update(overrides)
    path = repo / "config" / "forward-collection.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def make_repo(tmp_path: Path, *, protocol_copy: bool = True) -> Path:
    repo = tmp_path / "repo"
    protocols = repo / "config" / "protocols"
    protocols.mkdir(parents=True)

    if protocol_copy:
        for name in (
            "momentum-independent-validation-v1.yaml",
            "momentum-confirmation-engine-v1.yaml",
        ):
            for suffix in ("", ".sha256", ".sha256.v2.json"):
                source = REPO / "config" / "protocols" / f"{name}{suffix}"
                shutil.copy(source, protocols / source.name)

    source_archive = (
        REPO
        / "data"
        / "experiments"
        / "S02-CP08-independent-validation-protocol-v1"
    )
    archive = (
        repo
        / "data"
        / "experiments"
        / "S02-CP08-independent-validation-protocol-v1"
    )

    manifest = json.loads(
        (source_archive / "manifest.json").read_text(encoding="utf-8")
    )

    archive.mkdir(parents=True, exist_ok=True)

    shutil.copy(
        source_archive / "manifest.json",
        archive / "manifest.json",
    )

    for name in manifest["outputs"]:
        source_file = source_archive / name
        destination = archive / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, destination)



    snapshot_dir = repo / "data" / "snapshots" / "BTCUSDT-1h-raw-v1"
    chunk_dir = repo / "data" / "chunks" / "BTCUSDT" / "1h"
    snapshot_dir.mkdir(parents=True)
    chunk_dir.mkdir(parents=True)
    # Reuse the frozen snapshot manifest, anomalies and the last monthly chunk so
    # that the warm-up window is real and its hash is the frozen one.
    for name in ("raw-manifest.json", "anomalies.json"):
        shutil.copy(REPO / "data" / "snapshots" / "BTCUSDT-1h-raw-v1" / name, snapshot_dir / name)
    last_chunk = "20261001T0000_20261023T1600.parquet"
    shutil.copy(
        REPO / "data" / "chunks" / "BTCUSDT" / "1h" / last_chunk,
        chunk_dir / last_chunk,
    )
    (snapshot_dir / "report.json").write_text(
        json.dumps({"dataset_id": "BTCUSDT-1h-raw-v1", "lookback_hours": 168, "horizon_hours": 24}),
        encoding="utf-8",
    )
    write_config(repo)
    return repo


@pytest.fixture
def repo(tmp_path) -> Path:
    return make_repo(tmp_path)


@pytest.fixture
def cfg(repo: Path) -> fc.ForwardConfig:
    return fc.load_config(paths=fc.ForwardPaths(repo_root=repo))


def clock(hours: int = 12) -> datetime:
    return (BOUNDARY + pd.Timedelta(hours=hours)).to_pydatetime()


def network_guard(*_args, **_kwargs):  # pragma: no cover - must never run
    raise AssertionError("the test must not touch the network")


# ---------------------------------------------------------------------------
# configuration, boundary and protocol authority
# ---------------------------------------------------------------------------


def test_config_uses_protocol_values(cfg: fc.ForwardConfig) -> None:
    assert cfg.boundary_ms == BOUNDARY_MS
    assert cfg.boundary_utc == "2026-10-23T16:00:00+00:00"
    assert cfg.chunk_hours == 24
    assert cfg.readiness["min_windows"] == 300
    assert cfg.readiness["min_folds"] == 5
    assert cfg.readiness["min_days"] == 180
    assert cfg.readiness["min_trades"] == 100
    assert cfg.protocol_meta["sidecar_match"] is True
    assert cfg.protocol_meta["archive_manifest_match"] is True
    assert cfg.archive_dir == (cfg.paths.repo_root / "data" / "forward" / fc.DATASET_ID).resolve()


@pytest.mark.parametrize(
    "override",
    [
        {"dataset": {"dataset_id": fc.DATASET_ID, "symbol": "BTCUSDT", "interval": "1h", "chunk_hours": 24,
                     "boundary_utc": "2026-10-23T00:00:00Z"}},
        {"readiness": {"min_windows": 100, "min_folds": 5, "min_days": 180, "min_trades": 100}},
        {"windows": {"stride_hours": 24, "lookback_hours": 100}},
        {"windows": {"stride_hours": 24, "latency_hours": 0}},
    ],
)
def test_config_cannot_override_the_protocol(tmp_path, override) -> None:
    repo = make_repo(tmp_path)
    write_config(repo, **override)
    with pytest.raises(fc.ForwardConfigError, match="diverges from the frozen protocol"):
        fc.load_config(paths=fc.ForwardPaths(repo_root=repo))


def test_config_rejects_credentials(tmp_path) -> None:
    repo = make_repo(tmp_path)
    write_config(repo, source={"provider": "binance-spot", "api_key": "secret"})
    with pytest.raises(fc.ForwardConfigError, match="Credentials"):
        fc.load_config(paths=fc.ForwardPaths(repo_root=repo))


# ---------------------------------------------------------------------------
# first sync, incremental sync, idempotency, resume
# ---------------------------------------------------------------------------


def test_first_sync_promotes_closed_candles_only(cfg: fc.ForwardConfig) -> None:
    result = fc.sync(cfg, now=clock(8), downloader=lambda s, e, **k: frame_for(s, e), sleep=lambda _s: None)
    assert result.status == "SYNCED"
    assert result.promoted >= 1
    assert result.last_acquired_ms == BOUNDARY_MS + 8 * HOUR_MS

    view = fc.load_archive_view(fc.ForwardArchive(cfg.archive_dir))
    assert view.selected()
    frame = view.frames()
    assert frame["open_time"].min() == BOUNDARY_MS
    # no candle may close after the last closed hour
    assert (frame["close_time"] < BOUNDARY_MS + 8 * HOUR_MS).all()
    assert (frame["close_time"] == frame["open_time"] + HOUR_MS - 1).all()
    assert set(frame["symbol"].unique()) == {"BTCUSDT"}
    assert (frame["record_checksum"].str.len() == 64).all()


def test_sync_never_requests_an_unclosed_interval(cfg: fc.ForwardConfig) -> None:
    requested = []

    def downloader(start_ms, end_ms, *, symbol):
        requested.append((start_ms, end_ms))
        return frame_for(start_ms, end_ms)

    fc.sync(cfg, now=clock(3), downloader=downloader, sleep=lambda _s: None)
    assert requested
    for _start, end in requested:
        assert end <= BOUNDARY_MS + 3 * HOUR_MS


def test_sync_is_idempotent_and_does_not_refetch(cfg: fc.ForwardConfig) -> None:
    first = fc.sync(cfg, now=clock(8), downloader=lambda s, e, **k: frame_for(s, e), sleep=lambda _s: None)
    second = fc.sync(cfg, now=clock(8), downloader=network_guard, sleep=lambda _s: None)
    assert second.status == "UP_TO_DATE"
    assert second.reused == second.cells.__len__()
    assert second.promoted == 0
    assert second.dataset_sha256 == first.dataset_sha256
    manifests = list((cfg.archive_dir / "manifests").glob("dataset-manifest-*.json"))
    assert len(manifests) == 1  # no churn when nothing changed


def test_incremental_sync_extends_coverage(cfg: fc.ForwardConfig) -> None:
    first = fc.sync(cfg, now=clock(30), downloader=lambda s, e, **k: frame_for(s, e), sleep=lambda _s: None)
    assert first.promoted >= 2  # one complete grid cell + the trailing cell
    second = fc.sync(cfg, now=clock(36), downloader=lambda s, e, **k: frame_for(s, e), sleep=lambda _s: None)
    assert second.promoted >= 1
    assert second.reused >= 1  # the complete grid cell is not downloaded again
    view = fc.load_archive_view(fc.ForwardArchive(cfg.archive_dir))
    assert view.coverage_end_ms() == BOUNDARY_MS + 36 * HOUR_MS


def test_tail_chunk_is_superseded_by_the_complete_grid_chunk(cfg: fc.ForwardConfig) -> None:
    fc.sync(cfg, now=clock(8), downloader=lambda s, e, **k: frame_for(s, e), sleep=lambda _s: None)
    archives = sorted((cfg.archive_dir / "chunks").glob("*.json"))
    assert any(path.stem.startswith("tail-") for path in archives)
    fc.sync(cfg, now=clock(40), downloader=lambda s, e, **k: frame_for(s, e), sleep=lambda _s: None)
    view = fc.load_archive_view(fc.ForwardArchive(cfg.archive_dir))
    selected = view.selected()
    assert len({row["cell_start_ms"] for row in selected}) == len(selected)
    for row in selected[:1]:
        assert row["kind"] == "grid"
    # tail objects are preserved (immutability), not deleted
    assert any(path.stem.startswith("tail-") for path in archives)


def test_resume_after_interruption(cfg: fc.ForwardConfig) -> None:
    second_cell = BOUNDARY_MS + 24 * HOUR_MS

    def flaky(start_ms, end_ms, *, symbol):
        if start_ms == second_cell:
            raise ConnectionError("connection reset by peer")
        return frame_for(start_ms, end_ms)

    interrupted = fc.sync(cfg, now=clock(60) , downloader=flaky, sleep=lambda _s: None)  # noqa: E203
    assert interrupted.status == "INTERRUPTED"
    assert interrupted.promoted == 1
    assert "connection reset" in (interrupted.error or "")
    events = fc.ForwardArchive(cfg.archive_dir).events()
    assert any(row["event"] == "SYNC_INTERRUPTED" for row in events)

    resumed = fc.sync(cfg, now=clock(60), downloader=lambda s, e, **k: frame_for(s, e), sleep=lambda _s: None)
    assert resumed.status == "SYNCED"
    assert resumed.reused >= 1
    assert fc.load_archive_view(fc.ForwardArchive(cfg.archive_dir)).coverage_end_ms() == BOUNDARY_MS + 60 * HOUR_MS


def test_rate_limit_retry_then_success(cfg: fc.ForwardConfig) -> None:
    attempts = {"n": 0}
    sleeps: list[float] = []

    def limited(start_ms, end_ms, *, symbol):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("HTTP 429 Too Many Requests")
        return frame_for(start_ms, end_ms)

    result = fc.sync(cfg, now=clock(8), downloader=limited, sleep=sleeps.append)
    assert result.status == "SYNCED"
    assert attempts["n"] == 3
    assert sleeps == [cfg.backoff_seconds[0], cfg.backoff_seconds[1]]


def test_retry_exhaustion_is_recorded_and_aborts(cfg: fc.ForwardConfig) -> None:
    result = fc.sync(
        cfg,
        now=clock(8),
        downloader=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("HTTP 503")),
        sleep=lambda _s: None,
    )
    assert result.status == "INTERRUPTED"
    assert "503" in (result.error or "")
    assert not list((cfg.archive_dir / "chunks").glob("*.parquet"))


# ---------------------------------------------------------------------------
# conflicts, quarantine, immutability, atomicity
# ---------------------------------------------------------------------------


def test_source_discrepancy_is_never_overwritten(cfg: fc.ForwardConfig) -> None:
    fc.sync(cfg, now=clock(8), downloader=lambda s, e, **k: frame_for(s, e), sleep=lambda _s: None)
    before = {
        path.name: path.read_bytes()
        for path in (cfg.archive_dir / "chunks").glob("*")
    }
    conflict = fc.sync(
        cfg,
        now=clock(8),
        downloader=lambda s, e, **k: frame_for(s, e, price=500.0),
        sleep=lambda _s: None,
        recheck_source=True,
    )
    assert conflict.conflicts >= 1
    after = {path.name: path.read_bytes() for path in (cfg.archive_dir / "chunks").glob("*")}
    assert after == before  # nothing replaced
    events = fc.ForwardArchive(cfg.archive_dir).events()
    discrepancies = [row for row in events if row["event"] == "SOURCE_DISCREPANCY"]
    assert discrepancies and discrepancies[0]["promotion_blocked"] is True


def test_gap_is_quarantined_and_registered(cfg: fc.ForwardConfig) -> None:
    missing_hour = BOUNDARY_MS + 5 * HOUR_MS
    result = fc.sync(
        cfg,
        now=clock(8),
        downloader=lambda s, e, **k: frame_for(s, e, skip={missing_hour}),
        sleep=lambda _s: None,
    )
    assert result.quarantined >= 1
    assert not list((cfg.archive_dir / "chunks").glob("*.parquet"))
    quarantined = list((cfg.archive_dir / "quarantine").glob("*.parquet"))
    assert quarantined
    events = fc.ForwardArchive(cfg.archive_dir).events()
    assert any(row["event"] == "CHUNK_QUARANTINED" for row in events)

    status = fc.status(cfg, now=clock(8))
    assert status["quarantined_chunks"] >= 1
    assert status["valid_chunks"] == 0
    readiness = fc.readiness(cfg, now=clock(8))
    assert readiness["status"] == fc.READINESS_NO_DATA
    assert readiness["eligible_windows"]["count"] == 0


def test_partial_candle_is_quarantined(cfg: fc.ForwardConfig) -> None:
    result = fc.sync(
        cfg,
        now=clock(8),
        downloader=lambda s, e, **k: frame_for(s, e, close_override=BOUNDARY_MS + 1000),
        sleep=lambda _s: None,
    )
    assert result.quarantined >= 1
    meta = json.loads(next((cfg.archive_dir / "quarantine").glob("*.json")).read_text(encoding="utf-8"))
    codes = [item["code"] for item in meta["quality"]["issues"]]
    assert "PARTIAL_CANDLE" in codes
    assert meta["status"] == fc.STATUS_QUARANTINED


@pytest.mark.parametrize(
    "kwargs,expected_code",
    [
        ({"shift_ms": 60_000}, "UNALIGNED_TIMESTAMPS"),
        ({"skip": {BOUNDARY_MS + HOUR_MS}}, "MISSING_HOURS"),
    ],
)
def test_bad_timestamps_are_quarantined(cfg: fc.ForwardConfig, kwargs, expected_code) -> None:
    result = fc.sync(
        cfg,
        now=clock(4),
        downloader=lambda s, e, **k: frame_for(s, e, **kwargs),
        sleep=lambda _s: None,
    )
    assert result.quarantined >= 1
    meta = json.loads(next((cfg.archive_dir / "quarantine").glob("*.json")).read_text(encoding="utf-8"))
    assert expected_code in [item["code"] for item in meta["quality"]["issues"]]


def test_zero_volume_requires_review_and_is_not_promoted(cfg: fc.ForwardConfig) -> None:
    def zeros(start_ms, end_ms, *, symbol):
        frame = frame_for(start_ms, end_ms)
        for column in ("volume", "quote_volume", "taker_buy_base", "taker_buy_quote"):
            frame[column] = 0.0
        return frame

    result = fc.sync(cfg, now=clock(4), downloader=zeros, sleep=lambda _s: None)
    assert result.quarantined >= 1
    meta = json.loads(next((cfg.archive_dir / "quarantine").glob("*.json")).read_text(encoding="utf-8"))
    assert meta["status"] == fc.STATUS_REQUIRES_REVIEW


def test_candles_before_the_boundary_are_rejected(cfg: fc.ForwardConfig) -> None:
    def early(start_ms, end_ms, *, symbol):
        return frame_for(start_ms - HOUR_MS, end_ms - HOUR_MS)

    result = fc.sync(cfg, now=clock(4), downloader=early, sleep=lambda _s: None)
    assert result.quarantined >= 1
    meta = json.loads(next((cfg.archive_dir / "quarantine").glob("*.json")).read_text(encoding="utf-8"))
    assert "BOUNDARY_VIOLATION" in [item["code"] for item in meta["quality"]["issues"]]
    assert not list((cfg.archive_dir / "chunks").glob("*.parquet"))


def test_assess_chunk_refuses_an_interval_that_starts_before_the_boundary() -> None:
    assessment = fc.assess_chunk(
        frame_for(BOUNDARY_MS - 2 * HOUR_MS, BOUNDARY_MS + HOUR_MS),
        start_ms=BOUNDARY_MS - 2 * HOUR_MS,
        end_ms=BOUNDARY_MS + HOUR_MS,
        boundary_ms=BOUNDARY_MS,
        closed_boundary_ms=BOUNDARY_MS + 10 * HOUR_MS,
    )
    assert assessment.status == fc.STATUS_INVALID
    assert assessment.issues[0]["code"] == "BOUNDARY_VIOLATION"


def test_promotion_is_write_once(cfg: fc.ForwardConfig) -> None:
    fc.sync(cfg, now=clock(4), downloader=lambda s, e, **k: frame_for(s, e), sleep=lambda _s: None)
    archive = fc.ForwardArchive(cfg.archive_dir)
    view = fc.load_archive_view(archive)
    meta = view.valid_chunks[0]
    frame = archive.read_chunk(meta)
    tampered = frame.copy()
    tampered.loc[0, "close"] = tampered.loc[0, "close"] + 123.0
    with pytest.raises(fc.ForwardConflictError, match="different data"):
        archive.promote(tampered, {**meta, "data_sha256": "deadbeef"})


def test_atomic_writes_leave_no_temporary_files(cfg: fc.ForwardConfig) -> None:
    fc.sync(cfg, now=clock(8), downloader=lambda s, e, **k: frame_for(s, e), sleep=lambda _s: None)
    leftovers = [path for path in cfg.archive_dir.rglob("*") if path.name.endswith(".tmp")]
    assert leftovers == []


def test_manifest_is_versioned_and_never_overwritten(cfg: fc.ForwardConfig) -> None:
    archive = fc.ForwardArchive(cfg.archive_dir)
    archive.ensure()
    first, created = archive.write_manifest({"dataset_sha256": "a" * 64, "chunk_chain_sha256": "b" * 64})
    second, created_again = archive.write_manifest({"dataset_sha256": "c" * 64, "chunk_chain_sha256": "d" * 64})
    assert created and created_again
    assert first != second
    assert json.loads(first.read_text(encoding="utf-8"))["dataset_sha256"] == "a" * 64
    assert json.loads(second.read_text(encoding="utf-8"))["dataset_sha256"] == "c" * 64
    latest = json.loads((cfg.archive_dir / "manifests" / "latest.json").read_text(encoding="utf-8"))
    assert latest["manifest"] == second.name


def test_corrupted_chunk_blocks_the_view_and_is_reported(cfg: fc.ForwardConfig) -> None:
    fc.sync(cfg, now=clock(8), downloader=lambda s, e, **k: frame_for(s, e), sleep=lambda _s: None)
    archive = fc.ForwardArchive(cfg.archive_dir)
    chunk = sorted((cfg.archive_dir / "chunks").glob("*.parquet"))[0]
    chunk.write_bytes(chunk.read_bytes() + b"corrupt")
    view = fc.load_archive_view(archive)
    assert any(row["code"] == "CHUNK_HASH_MISMATCH" for row in view.issues)
    assert not view.valid_chunks
    audit = fc.audit(cfg, now=clock(8))
    assert audit["technical_gate"] == "FAIL"
    assert "FWD.1" in audit["checks_failed"]


# ---------------------------------------------------------------------------
# readiness
# ---------------------------------------------------------------------------


def test_eligible_windows_respect_cp07_contract() -> None:
    boundary = BOUNDARY_MS
    times = set(range(boundary - 168 * HOUR_MS, boundary + 50 * HOUR_MS, HOUR_MS))
    windows = fc.eligible_windows(
        times,
        boundary_ms=boundary,
        last_bar_open_ms=boundary + 26 * HOUR_MS,
        stride_hours=24,
        lookback_hours=168,
        latency_hours=1,
        horizon_hours=24,
    )
    assert [row["cutoff_utc"] for row in windows] == ["2026-10-23T16:00:00+00:00"]
    assert windows[0]["exit_utc"] == "2026-10-24T17:00:00+00:00"


def test_eligible_windows_never_cross_a_gap() -> None:
    boundary = BOUNDARY_MS
    times = set(range(boundary - 168 * HOUR_MS, boundary + 50 * HOUR_MS, HOUR_MS))
    times.discard(boundary + 25 * HOUR_MS)  # exit bar of the first window
    windows = fc.eligible_windows(
        times,
        boundary_ms=boundary,
        last_bar_open_ms=boundary + 49 * HOUR_MS,
        stride_hours=24,
        lookback_hours=168,
        latency_hours=1,
        horizon_hours=24,
    )
    assert windows == []


def test_readiness_reports_insufficient_sample_without_strategy_metrics(cfg: fc.ForwardConfig) -> None:
    fc.sync(cfg, now=clock(48), downloader=lambda s, e, **k: frame_for(s, e), sleep=lambda _s: None)
    readiness = fc.readiness(cfg, now=clock(48))
    assert readiness["status"] == fc.READINESS_NOT_READY
    assert readiness["evaluation_executed"] is False
    assert readiness["final_evaluation_gate"] == "NOT_ACTIVATED"
    assert readiness["strategic_gate"] == "NO-GO"
    assert readiness["verdict"] == "NOT_EVALUATED"
    missing = {item["id"] for item in readiness["missing_requirements"]}
    assert {"windows", "folds", "days", "trades"} <= missing
    payload = json.dumps(readiness).lower()
    for forbidden in ("profit_factor_usdt", "total_return_pct", "max_drawdown_mtm_pct", "equity", "signal"):
        assert forbidden not in payload
    assert readiness["eligible_windows"]["count"] > 0
    assert readiness["eligible_windows"]["first"]["cutoff_utc"] == "2026-10-23T16:00:00+00:00"


def test_readiness_never_calls_the_evaluator(cfg: fc.ForwardConfig, monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):  # pragma: no cover - guard
        raise AssertionError("final evaluation must not run during CP08-05")

    monkeypatch.setattr(fc.iv, "evaluate_independent", forbidden)
    fc.sync(cfg, now=clock(24), downloader=lambda s, e, **k: frame_for(s, e), sleep=lambda _s: None)
    for payload in (
        fc.status(cfg, now=clock(24)),
        fc.readiness(cfg, now=clock(24)),
        fc.audit(cfg, now=clock(24)),
    ):
        assert payload["evaluation_executed"] is False


def test_offline_commands_do_not_write(cfg: fc.ForwardConfig) -> None:
    fc.sync(cfg, now=clock(24), downloader=lambda s, e, **k: frame_for(s, e), sleep=lambda _s: None)

    def snapshot() -> dict[str, tuple[int, int]]:
        return {
            str(path): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in sorted(cfg.archive_dir.rglob("*"))
            if path.is_file()
        }

    before = snapshot()
    fc.status(cfg, now=clock(24))
    fc.readiness(cfg, now=clock(24))
    fc.audit(cfg, now=clock(24))
    assert snapshot() == before


def test_readiness_distinguishes_unacquired_from_gap(cfg: fc.ForwardConfig) -> None:
    missing_hour = BOUNDARY_MS + 5 * HOUR_MS
    fc.sync(
        cfg,
        now=clock(8),
        downloader=lambda s, e, **k: frame_for(s, e, skip={missing_hour}),
        sleep=lambda _s: None,
    )
    readiness = fc.readiness(cfg, now=clock(8))
    # nothing promoted: the whole interval is "still to acquire", not a silent hole
    assert readiness["hours_available"] == 0
    status = fc.status(cfg, now=clock(8))
    assert status["hours_not_yet_acquired"] == 8
    assert status["quarantined_chunks"] == 1
    assert status["gap_hours"] == 0


# ---------------------------------------------------------------------------
# audit and protocol hash
# ---------------------------------------------------------------------------


def test_audit_passes_on_a_clean_archive(cfg: fc.ForwardConfig) -> None:
    fc.sync(cfg, now=clock(30), downloader=lambda s, e, **k: frame_for(s, e), sleep=lambda _s: None)
    result = fc.audit(cfg, now=clock(30))
    assert result["technical_gate"] == "PASS"
    assert result["checks_failed"] == []
    # the synthetic repository does not carry the 17 frozen research artefacts:
    # that is an explicit WARN, never a silent PASS.
    fwd9 = next(item for item in result["checks"] if item["id"] == "FWD.9")
    assert fwd9["status"] == "WARN"
    assert fwd9["evidence"]["missing_in_this_repository"]
    assert {item["id"] for item in result["checks"]} == {f"FWD.{index}" for index in range(1, 12)}


def test_protocol_hash_mismatch_is_detected(tmp_path) -> None:
    repo = make_repo(tmp_path)
    protocol = repo / "config" / "protocols" / "momentum-independent-validation-v1.yaml"
    protocol.write_text(protocol.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
    cfg = fc.load_config(paths=fc.ForwardPaths(repo_root=repo))
    audit = fc.audit(cfg, now=clock(4))
    assert "FWD.7" in audit["checks_failed"]
    assert audit["technical_gate"] == "FAIL"
    readiness = fc.readiness(cfg, now=clock(4))
    assert readiness["isolation"]["protocol_hash_ok"] is False
    assert readiness["isolation"]["all_ok"] is False


def test_audit_never_touches_the_validation_registry(tmp_path) -> None:
    repo = make_repo(tmp_path)
    cfg = fc.load_config(paths=fc.ForwardPaths(repo_root=repo))
    fc.sync(cfg, now=clock(12), downloader=lambda s, e, **k: frame_for(s, e), sleep=lambda _s: None)
    registry = repo / "data" / "validation_registry"
    assert not registry.exists() or not (registry / "datasets.jsonl").exists()
    audit = fc.audit(cfg, now=clock(12))
    assert audit["checks_failed"] == []


# ---------------------------------------------------------------------------
# validation isolation (research <=> forward)
# ---------------------------------------------------------------------------


def test_forward_paths_are_detected_and_guarded(tmp_path) -> None:
    repo = make_repo(tmp_path)
    chunk = repo / "data" / "forward" / fc.DATASET_ID / "chunks" / "chunk-x.parquet"
    chunk.parent.mkdir(parents=True, exist_ok=True)
    chunk.write_bytes(b"x")
    assert is_forward_path(chunk, repo_root=repo)
    with pytest.raises(ForwardIsolationViolation, match="independent"):
        guard_research_path(chunk, repo_root=repo)
    with pytest.raises(ForwardIsolationViolation, match="cannot write"):
        guard_research_output(r"data\forward\BTCUSDT-1h-forward-v1\chunks", repo_root=repo)
    assert not is_forward_path(repo / "data" / "datasets" / "x.parquet", repo_root=repo)


def test_research_pipelines_refuse_the_forward_archive(tmp_path) -> None:
    repo = make_repo(tmp_path)
    forward_dir = repo / "data" / "forward" / fc.DATASET_ID
    from crypto_decision_lab.freeze import freeze
    from crypto_decision_lab.sync import sync as research_sync

    with pytest.raises(ForwardIsolationViolation):
        research_sync(
            "BTCUSDT",
            "2026-10-24T00:00:00Z",
            "2026-10-25T00:00:00Z",
            str(forward_dir),
            downloader=lambda *a, **k: pytest.fail("must not download"),
        )
    with pytest.raises(ForwardIsolationViolation):
        freeze(str(forward_dir), str(forward_dir / "raw-manifest.json"), str(repo / "data" / "snapshots" / "x"), 168, 24, 24)


def test_metric_payload_from_forward_is_refused() -> None:
    with pytest.raises(ForwardIsolationViolation, match="strategy metrics"):
        guard_metric_payload({"total_return_pct": 1.0, "profit_factor_usdt": 1.2}, source="forward report")
    guard_metric_payload({"hours_available": 10}, source="forward status")


def test_forward_archive_cannot_live_inside_research_data(tmp_path) -> None:
    repo = make_repo(tmp_path)
    write_config(repo, archive={"root": "data/experiments"})
    with pytest.raises(ForwardIsolationViolation, match="research data"):
        fc.load_config(paths=fc.ForwardPaths(repo_root=repo))


def test_forward_collection_never_registers_a_dataset(cfg: fc.ForwardConfig) -> None:
    fc.sync(cfg, now=clock(24), downloader=lambda s, e, **k: frame_for(s, e), sleep=lambda _s: None)
    registry = cfg.paths.resolve(cfg.paths.registry_dir)
    assert not (registry / "datasets.jsonl").exists()
    readiness = fc.readiness(cfg, now=clock(24))
    assert readiness["dataset_registered"] is False
    assert readiness["isolation"]["final_evaluation_consumed"] is False
    candidate = readiness["registration_candidate"]
    assert candidate["registered"] is False
    assert candidate["sha256"] == readiness["dataset_sha256"]
    assert candidate["role"] == "independent"
    assert candidate["registration_api"].endswith("register_dataset")
    assert not (registry / "datasets.jsonl").exists()


# ---------------------------------------------------------------------------
# CLI (offline) and structured logging
# ---------------------------------------------------------------------------


def test_cli_status_and_readiness_are_offline(tmp_path, capsys, monkeypatch) -> None:
    repo = make_repo(tmp_path)
    cfg = fc.load_config(paths=fc.ForwardPaths(repo_root=repo))
    fc.sync(cfg, now=clock(12), downloader=lambda s, e, **k: frame_for(s, e), sleep=lambda _s: None)

    monkeypatch.setattr(fc, "fetch_klines", network_guard)
    assert fc.main(["status", "--repo-root", str(repo), "--now", "2026-10-24T04:00:00Z"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["dataset_id"] == fc.DATASET_ID
    assert status["evaluation_executed"] is False

    assert fc.main(["readiness", "--repo-root", str(repo), "--now", "2026-10-24T04:00:00Z"]) == 0
    readiness = json.loads(capsys.readouterr().out)
    assert readiness["status"] == fc.READINESS_NOT_READY
    assert "strategic_gate" in readiness and readiness["strategic_gate"] == "NO-GO"

    assert fc.main(["audit", "--repo-root", str(repo), "--now", "2026-10-24T04:00:00Z"]) == 0
    audit = json.loads(capsys.readouterr().out)
    assert audit["technical_gate"] == "PASS"


def test_cli_sync_uses_the_lock_and_emits_structured_logs(tmp_path, capsys) -> None:
    repo = make_repo(tmp_path)
    log = tmp_path / "forward.jsonl"
    # The mock adapter is injected by pointing the source at a local downloader:
    # here we simply verify the lock prevents a second concurrent run.
    archive = fc.ForwardArchive(repo / "data" / "forward" / fc.DATASET_ID)
    handle = archive.lock()
    try:
        assert fc.main([
            "sync", "--repo-root", str(repo), "--now", "2026-10-25T00:00:00Z", "--log-file", str(log)
        ]) == fc.EXIT_BLOCKED
    finally:
        fc._unlock(handle)
        handle.close()
    events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert any(row["event"] == "SYNC_BLOCKED" for row in events)


def test_real_adapter_enforces_closed_candles_without_network() -> None:
    """The reused Binance adapter refuses an unfinished hourly candle."""

    class Session:
        def get(self, *args, **kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("unfinished candle must be rejected before the call")

    now_hour_ms = int(pd.Timestamp.now(tz="UTC").floor("h").timestamp() * 1000)
    with pytest.raises(ValueError, match="unfinished"):
        fetch_klines(now_hour_ms, now_hour_ms + 2 * HOUR_MS, session=Session())
