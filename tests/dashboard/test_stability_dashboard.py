"""Tests for the S02-CP08-02 dashboard adapter and charts (read-only)."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab.dashboard.charts import (
    make_period_pnl_figure,
    make_pnl_distribution_figure,
    make_profit_concentration_figure,
    make_stability_equity_figure,
    make_underwater_figure,
)
from crypto_decision_lab.dashboard.stability_adapter import (
    StabilityArchiveError,
    load_stability_archive,
)


def _trades() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "trade_index": [1, 2, 3, 4],
            "decision_id": [f"CP07:window-{index}:momentum_threshold" for index in range(4)],
            "cutoff": pd.to_datetime(
                ["2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01"], utc=True
            ),
            "exit_timestamp": pd.to_datetime(
                ["2024-01-02", "2024-02-02", "2024-03-02", "2024-04-02"], utc=True
            ),
            "fold": [0, 0, 1, 1],
            "month": ["2024-01", "2024-02", "2024-03", "2024-04"],
            "direction": ["LONG", "SHORT", "LONG", "SHORT"],
            "net_return_pct": [8.0, -4.0, 3.0, -1.0],
            "pnl_usdt": [800.0, -368.0, 276.0, -92.0],
            "equity_after_usdt": [10_800.0, 10_432.0, 10_708.0, 10_616.0],
            "rank_by_pnl": [1, 4, 2, 3],
        }
    )


def _periods() -> pd.DataFrame:
    rows = []
    cumulative = 0.0
    for period, pnl, trades in [("2024-01", 800.0, 2), ("2024-02", -100.0, 3), ("2024-03", 78.0, 2)]:
        cumulative += pnl
        rows.append(
            {
                "granularity": "month",
                "period": period,
                "period_start": pd.Timestamp(f"{period}-01", tz="UTC"),
                "period_end": pd.Timestamp(f"{period}-28", tz="UTC"),
                "trades": trades,
                "net_pnl_usdt": pnl,
                "profit_factor_usdt": 1.5,
                "compounded_return_pct": pnl / 10.0,
                "win_rate_trade_pct": 50.0,
                "win_rate_window_pct": 40.0,
                "avg_net_trade_pct": pnl / trades / 100.0,
                "best_net_pct": 8.0,
                "worst_net_pct": -4.0,
                "share_of_net_pnl_pct": pnl / 7.78,
                "cumulative_pnl_usdt": cumulative,
                "windows": trades,
            }
        )
    empty = pd.DataFrame(rows[:1]).assign(granularity="fold", period="0")
    return pd.concat([pd.DataFrame(rows), empty], ignore_index=True)


def _equity() -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=6, freq="h", tz="UTC")
    values = np.array([10_000.0, 10_000.0, 9_600.0, 10_500.0, 10_400.0, 10_600.0])
    peak = np.maximum.accumulate(values)
    return pd.DataFrame(
        {
            "curve": ["step"] * 6 + ["mark_to_market"] * 6,
            "timestamp": list(index) + list(index),
            "equity": np.concatenate([values, values * 0.99]),
            "drawdown_pct": np.concatenate(
                [100.0 * (values / peak - 1.0), 100.0 * (values * 0.99 / np.maximum.accumulate(values * 0.99) - 1.0)]
            ),
        }
    )


def _concentration_curve() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "rank": [1, 2, 3, 4],
            "decision_id": [f"d{index}" for index in range(4)],
            "fold": [0, 1, 1, 0],
            "month": ["2024-01", "2024-03", "2024-04", "2024-02"],
            "pnl_usdt": [800.0, 276.0, -92.0, -368.0],
            "cumulative_pnl_usdt": [800.0, 1076.0, 984.0, 616.0],
            "share_of_net_pnl_pct": [129.9, 174.7, 159.7, 100.0],
            "share_of_gross_profit_pct": [74.4, 100.0, 100.0, 100.0],
        }
    )


def _write_archive(root: Path, *, complete: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _trades().to_parquet(root / "trade_attribution.parquet", index=False)
    _periods().to_parquet(root / "period_summary.parquet", index=False)
    concentration = {
        "total_return_pct": 6.16,
        "cumulative_share_curve": _concentration_curve().to_dict(orient="records"),
        "top_k": {"1": {"pnl_usdt": 800.0, "share_of_net_pnl_pct": 129.9}},
    }
    (root / "concentration_summary.json").write_text(json.dumps(concentration), encoding="utf-8")
    (root / "drawdown_analysis.json").write_text(json.dumps({"exit_marked": {}, "mark_to_market": {}}), encoding="utf-8")
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "initial_capital": 10_000.0,
                "source_hashes_unchanged": True,
                "reconciliation": {"all_match": True},
            }
        ),
        encoding="utf-8",
    )
    if complete:
        _equity().to_parquet(root / "equity_curves.parquet", index=False)
        pd.DataFrame(
            [{"granularity": "trend", "bucket": "TREND_UP", "trades": 2, "net_pnl_usdt": 1076.0}]
        ).to_parquet(root / "regime_breakdown.parquet", index=False)
        (root / "streak_analysis.json").write_text(json.dumps({"max_win_streak": 1}), encoding="utf-8")
    return root


def test_adapter_reads_the_archive_without_recomputing(tmp_path):
    root = _write_archive(tmp_path / "archive")
    archive = load_stability_archive(root)
    assert archive.available
    assert archive.issues == ()
    assert archive.initial_capital == 10_000.0
    assert len(archive.period("month")) == 3
    assert archive.regime("trend")["bucket"].tolist() == ["TREND_UP"]
    assert len(archive.concentration_curve) == 4
    assert archive.trades["cutoff"].dt.tz is not None


def test_adapter_flags_missing_optional_files_and_failed_reconciliation(tmp_path):
    root = _write_archive(tmp_path / "archive", complete=False)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    manifest["reconciliation"]["all_match"] = False
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    archive = load_stability_archive(root)
    assert archive.equity.empty
    assert any("equity_curves.parquet" in issue for issue in archive.issues)
    assert any("all_match" in issue for issue in archive.issues)


def test_adapter_rejects_incomplete_required_set(tmp_path):
    root = tmp_path / "broken"
    root.mkdir()
    (root / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(StabilityArchiveError):
        load_stability_archive(root)
    with pytest.raises(StabilityArchiveError):
        load_stability_archive(tmp_path / "does-not-exist")


def test_equity_and_underwater_charts_use_both_conventions():
    equity = _equity()
    figure = make_stability_equity_figure(equity, initial_capital=10_000.0)
    names = [trace.name for trace in figure.data]
    assert set(names) == {"Exit-marked (ledger)", "Mark-to-market orario"}
    step = next(trace for trace in figure.data if trace.name == "Exit-marked (ledger)")
    assert step.y[0] == pytest.approx(10_000.0)
    underwater = make_underwater_figure(equity)
    assert all(trace.fill == "tozeroy" for trace in underwater.data)
    assert underwater.data[0].y.min() < 0


def test_pnl_distribution_splits_wins_and_losses():
    figure = make_pnl_distribution_figure(_trades(), metric="pnl_usdt")
    histogram = [trace for trace in figure.data if trace.type == "histogram"]
    assert len(histogram) == 2
    assert set(histogram[0].x) == {800.0, 276.0}
    assert set(histogram[1].x) == {-368.0, -92.0}
    assert len([shape for shape in figure.layout.shapes if shape.type == "line"]) >= 2
    empty = make_pnl_distribution_figure(pd.DataFrame(), metric="pnl_usdt")
    assert not empty.data


def test_profit_concentration_chart_has_bars_and_cumulative_line():
    figure = make_profit_concentration_figure(_concentration_curve())
    assert figure.data[0].type == "bar"
    assert figure.data[1].type == "scatter"
    assert figure.data[1].y[-1] == pytest.approx(100.0)
    assert len(figure.layout.annotations) >= 2


def test_period_pnl_chart_bars_per_requested_granularity():
    periods = _periods()
    figure = make_period_pnl_figure(periods, granularity="month")
    assert figure.data[0].type == "bar"
    assert len(figure.data[0].x) == 3
    assert figure.data[1].type == "scatter"
    fold_figure = make_period_pnl_figure(periods, granularity="fold")
    assert len(fold_figure.data[0].x) == 1
    missing = make_period_pnl_figure(periods, granularity="quarter")
    assert not missing.data
