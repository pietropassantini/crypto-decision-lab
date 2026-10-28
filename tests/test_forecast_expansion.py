from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab.forecast_expansion import (
    ExpansionPaths,
    HORIZON_HOURS,
    _write_window,
    benchmark,
    select_new_indices,
    stage_plan,
)


def _frame(cutoff: pd.Timestamp, anchor: float, factor: float) -> pd.DataFrame:
    timestamps = pd.date_range(cutoff, periods=HORIZON_HOURS, freq="h", tz="UTC")
    actual = anchor * np.linspace(1.0, factor, HORIZON_HOURS)
    p50 = actual * 1.001
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "actual": actual,
            "timestral_p10": p50 * 0.99,
            "timestral_p50": p50,
            "timestral_p90": p50 * 1.01,
            "naive": np.full(HORIZON_HOURS, anchor),
        }
    )


def _metrics(index: int, cutoff: pd.Timestamp, anchor: float) -> dict:
    return {
        "schema_version": "cdl.cp06a.forecast.v1",
        "experiment": "S02-CP06A",
        "source": "new-inference",
        "reused": False,
        "model": "test-model",
        "revision_requested": "test-rev",
        "device": "cpu",
        "window_index": index,
        "lookback": 168,
        "horizon": HORIZON_HOURS,
        "provenance": {
            "forecast_start_utc": str(cutoff),
            "target_end_exclusive_utc": str(cutoff + pd.Timedelta(hours=24)),
        },
        "last_context_close": anchor,
        "metrics": {},
    }


def test_selection_excludes_cp02_and_is_deterministic():
    cp02 = [0, 20, 40, 60]
    selected = select_new_indices(100, cp02, 20)
    assert len(selected) == 16
    assert len(set(selected)) == len(selected)
    assert not set(selected).intersection(cp02)
    assert selected == select_new_indices(100, cp02, 20)


def test_stage_plan_sizes_and_no_overlap():
    new = list(range(1, 401))
    plan = stage_plan(new)
    assert len(plan["smoke"]) == 5
    assert len(plan["verification"]) == 50
    assert len(plan["expand"]) == 345
    assert set(plan["smoke"]).isdisjoint(plan["verification"])
    assert set(plan["verification"]).isdisjoint(plan["expand"])
    assert set(plan["smoke"]).isdisjoint(plan["expand"])


def test_window_write_refuses_overwrite(tmp_path):
    paths = ExpansionPaths(
        data_root=tmp_path / "data",
        snapshot_dir=tmp_path / "data" / "snap",
        cp02_dir=tmp_path / "data" / "cp02",
        cp05r_dir=tmp_path / "data" / "cp05r",
        output_dir=tmp_path / "out",
    )
    cutoff = pd.Timestamp("2024-01-01", tz="UTC")
    frame = _frame(cutoff, 100.0, 1.05)
    _write_window(paths, 0, frame, _metrics(0, cutoff, 100.0))
    with pytest.raises(FileExistsError):
        _write_window(paths, 0, frame, _metrics(0, cutoff, 100.0))


def test_benchmark_rebuilds_canonical_predictions(tmp_path):
    output = tmp_path / "out"
    output.mkdir()
    paths = ExpansionPaths(
        data_root=tmp_path / "data",
        snapshot_dir=tmp_path / "data" / "snap",
        cp02_dir=tmp_path / "data" / "cp02",
        cp05r_dir=tmp_path / "data" / "cp05r",
        output_dir=output,
    )
    cutoff = pd.Timestamp("2024-01-01", tz="UTC")
    for index, factor in ((0, 1.05), (1, 0.98)):
        _write_window(paths, index, _frame(cutoff, 100.0, factor), _metrics(index, cutoff, 100.0))
    pd.DataFrame(
        {
            "window_index": [0, 1],
            "forecast_start_utc": [cutoff, cutoff],
            "context_start_utc": [cutoff - pd.Timedelta(hours=168)] * 2,
            "target_end_exclusive_utc": [cutoff + pd.Timedelta(hours=24)] * 2,
            "source": ["new-inference", "new-inference"],
        }
    ).to_parquet(output / "windows.parquet", index=False)
    config = {
        "schema_version": "cdl.cp06a.expansion.v1",
        "experiment": "S02-CP06A",
        "dataset_id": "test",
        "model": "test-model",
        "revision": "test-rev",
        "target_windows": 2,
        "cp02_indices": [],
        "new_indices": [0, 1],
        "selected_indices": [0, 1],
        "stages": {"smoke": [0], "verification": [1], "expand": []},
    }
    (output / "config.json").write_text(json.dumps(config, indent=2))
    summary = benchmark(paths)
    assert summary["windows"] == 2
    assert summary["target_rows"] == 2
    assert summary["canonical_checks"]["unique_windows"] is True
    canonical = pd.read_parquet(output / "canonical_predictions.parquet")
    assert canonical["target"].iloc[0] == cutoff + pd.Timedelta(hours=23)
    assert (canonical["timestral_p50"] > 0).all()
    assert (output / "summary.json").is_file()
