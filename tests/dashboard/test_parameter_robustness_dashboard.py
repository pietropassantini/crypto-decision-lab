"""Tests for the read-only S02-CP08-03 dashboard adapter and charts."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab.dashboard.charts import (
    make_cost_sensitivity_figure,
    make_latency_robustness_figure,
    make_scenario_interval_figure,
    make_threshold_surface_figure,
)
from crypto_decision_lab.dashboard.parameter_robustness_adapter import (
    RobustnessArchiveError,
    load_robustness_archive,
)


def _scenarios() -> pd.DataFrame:
    rows = [
        ("cost_frozen", "cost_0000", 0.0, 0.0, 0, 0.0, 106, 91.28, 1.7820, -17.98, False, False),
        ("cost_frozen", "cost_0015_base", 0.001, 0.0005, 0, 0.0, 106, 39.18, 1.3295, -27.20, False, False),
        ("cost_frozen", "cost_0050", 0.0033, 0.0017, 0, 0.0, 106, -33.73, 0.7257, -51.45, False, False),
        ("policy_refit", "refit_cost_0010", 0.0005, 0.0005, 0, 0.0, 39, 40.57, 2.0180, -16.84, False, False),
        ("policy_refit", "refit_cost_0050", 0.0033, 0.0017, 0, 0.0, 8, -6.48, 0.6727, -21.40, False, False),
        ("latency", "latency_0h", 0.001, 0.0005, 0, 0.0, 106, 39.18, 1.3295, -27.20, False, False),
        ("latency", "latency_1h", 0.001, 0.0005, 1, 0.0, 106, 23.59, 1.2217, -30.76, False, True),
        ("latency", "latency_2h", 0.001, 0.0005, 2, 0.0, 106, 23.90, 1.2002, -30.03, False, False),
        ("short_carry", "carry_neutral", 0.001, 0.0005, 0, 0.05, 106, 35.81, 1.3000, -28.25, True, False),
        ("short_carry", "carry_stressed", 0.001, 0.0005, 0, 0.14, 106, 29.94, 1.2500, -30.13, True, False),
        ("combined", "conservative_1h", 0.001, 0.0005, 1, 0.0, 106, 23.59, 1.2217, -30.76, False, True),
        ("combined", "conservative_1h_cost20", 0.001, 0.001, 1, 0.0, 106, 11.16, 1.1040, -34.66, False, False),
        ("combined", "conservative_1h_cost20_carry", 0.001, 0.001, 1, 0.14, 106, 3.79, 1.0350, -37.35, True, False),
    ]
    frame = pd.DataFrame(
        rows,
        columns=[
            "family",
            "scenario",
            "fee_rate",
            "slippage",
            "delay_hours",
            "carry_short_pct",
            "trades",
            "total_return_pct",
            "profit_factor_usdt",
            "max_drawdown_mtm_pct",
            "hypothesis",
            "primary",
        ],
    )
    frame["label"] = frame["scenario"]
    frame["total_cost_per_side"] = frame["fee_rate"] + frame["slippage"]
    frame["max_drawdown_exit_pct"] = frame["max_drawdown_mtm_pct"] + 2.0
    frame["ci_return_low_pct"] = frame["total_return_pct"] - 50.0
    frame["ci_return_high_pct"] = frame["total_return_pct"] + 100.0
    frame["ci_return_p_positive"] = 0.75
    frame["ci_pf_low"] = 0.8
    frame["ci_pf_high"] = 2.0
    frame["ci_pf_p_above_one"] = 0.8
    frame["ci_mean_low_pct"] = -0.1
    frame["ci_mean_high_pct"] = 0.4
    frame["win_rate_trade_pct"] = 43.4
    frame["win_rate_window_pct"] = 15.3
    frame["avg_net_trade_pct"] = 0.35
    frame["no_trade"] = 194
    frame["skipped"] = 0
    frame["windows"] = 300
    frame["long_trades"] = 57
    frame["short_trades"] = 49
    frame["missing_bars"] = 0
    frame["net_pnl_usdt"] = 3917.58
    frame["final_equity_usdt"] = 13917.58
    return frame


def _surface() -> pd.DataFrame:
    rows = []
    for threshold in (0.0, 0.05, 0.08, 0.10, 0.20):
        for fold in range(5):
            rows.append(
                {
                    "scope": "train",
                    "fold": fold,
                    "threshold": threshold,
                    "is_frozen_threshold": threshold == 0.05,
                    "exploratory": False,
                    "trades": 100 - 10 * fold,
                    "total_return_pct": 10.0 + threshold * 100,
                    "profit_factor_usdt": np.nan,
                    "max_drawdown_exit_pct": np.nan,
                    "max_drawdown_mtm_pct": np.nan,
                    "win_rate_trade_pct": np.nan,
                    "ci_low_pct": np.nan,
                    "ci_high_pct": np.nan,
                }
            )
            rows.append(
                {
                    "scope": "test_fold",
                    "fold": fold,
                    "threshold": threshold,
                    "is_frozen_threshold": threshold == 0.05,
                    "exploratory": True,
                    "trades": 20,
                    "total_return_pct": 5.0,
                    "profit_factor_usdt": 1.2,
                    "max_drawdown_exit_pct": -5.0,
                    "max_drawdown_mtm_pct": np.nan,
                    "win_rate_trade_pct": 40.0,
                    "ci_low_pct": np.nan,
                    "ci_high_pct": np.nan,
                }
            )
        rows.append(
            {
                "scope": "test_all",
                "fold": -1,
                "threshold": threshold,
                "is_frozen_threshold": threshold == 0.05,
                "exploratory": True,
                "trades": 106,
                "total_return_pct": 39.18 - threshold * 50,
                "profit_factor_usdt": 1.33,
                "max_drawdown_exit_pct": -21.18,
                "max_drawdown_mtm_pct": -27.20,
                "win_rate_trade_pct": 43.4,
                "ci_low_pct": -24.0,
                "ci_high_pct": 148.19,
            }
        )
    frame = pd.DataFrame(rows)
    frame["promoted"] = False
    return frame


def _write_archive(root: Path, *, strategic_gate: str = "INCONCLUSIVE") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _scenarios().to_parquet(root / "scenario_metrics.parquet", index=False)
    _surface().to_parquet(root / "threshold_surface.parquet", index=False)
    pd.DataFrame(
        {
            "scenario": ["cost_0015_base"] * 5 + ["conservative_1h"] * 5,
            "family": ["cost_frozen"] * 5 + ["combined"] * 5,
            "fold": list(range(5)) * 2,
            "trades": [15, 35, 23, 19, 14] * 2,
            "net_pnl_usdt": [260.84, 2496.72, 762.02, -1372.31, 1689.51] * 2,
            "chained_pnl_usdt": [260.84, 2561.84, 977.12, -1893.76, 2011.54] * 2,
            "share_of_net_pnl_pct": [6.66, 65.39, 24.94, -48.34, 51.35] * 2,
            "compounded_return_pct": [2.61, 24.97, 7.62, -13.72, 16.90] * 2,
            "profit_factor_usdt": [1.28, 1.59, 1.42, 0.49, 4.70] * 2,
            "max_drawdown_exit_pct": [-3.95, -11.59, -7.39, -15.91, -3.13] * 2,
            "max_drawdown_mtm_pct": [-6.09, -18.49, -10.95, -19.68, -4.83] * 2,
            "win_rate_trade_pct": [26.7, 42.9, 43.5, 42.1, 64.3] * 2,
        }
    ).to_parquet(root / "fold_stability.parquet", index=False)
    pd.DataFrame(
        {
            "family": ["policy_refit"] * 5,
            "scenario": ["refit_cost_0015_base"] * 5,
            "fold": list(range(5)),
            "train_size": [200] * 5,
            "test_size": [60] * 5,
            "purged_windows": [0] * 5,
            "selected_threshold": [0.09] * 5,
            "train_multiple": [1.67, 1.64, 2.06, 2.09, 1.98],
            "train_compounded_return_pct": [67.28, 64.59, 106.65, 109.33, 98.12],
            "train_trades": [62, 68, 81, 88, 95],
        }
    ).to_parquet(root / "threshold_selection.parquet", index=False)
    (root / "summary.json").write_text(json.dumps({"technical_gate": "PASS"}), encoding="utf-8")
    (root / "concentration.json").write_text(
        json.dumps(
            {
                "frozen": {
                    "top_k": {
                        str(k): {
                            "pnl_usdt": 100.0 * k,
                            "share_of_net_pnl_pct": 20.0 * k,
                            "return_pct_after_removal_recompounded": 39.18 - 10.0 * k,
                        }
                        for k in (1, 2, 3, 5, 10)
                    }
                },
                "primary_1h": {
                    "top_k": {
                        str(k): {
                            "pnl_usdt": 100.0 * k,
                            "share_of_net_pnl_pct": 25.0 * k,
                            "return_pct_after_removal_recompounded": 23.59 - 10.0 * k,
                        }
                        for k in (1, 2, 3, 5, 10)
                    }
                },
                "by_scenario": {},
            }
        ),
        encoding="utf-8",
    )
    (root / "checks.json").write_text(
        json.dumps(
            [
                {"id": "CP08-03.1", "status": "PASS", "check": "griglia", "detail": "ok"},
                {"id": "CP08-03.6", "status": "WARN", "check": "refit", "detail": "confondimento"},
            ]
        ),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": "cdl.cp08.parameter_robustness.v1",
        "read_only": True,
        "initial_capital": 10_000.0,
        "technical_gate": "PASS",
        "strategic_gate": strategic_gate,
        "promotion": "NO-GO",
        "source_hashes_unchanged": True,
        "original_artifacts_modified": False,
        "outputs": {},
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_adapter_reads_the_archive_and_exposes_scenarios(tmp_path):
    root = _write_archive(tmp_path / "archive")
    archive = load_robustness_archive(root)
    assert archive.available
    assert archive.issues == ()
    assert archive.initial_capital == 10_000.0
    assert len(archive.family("latency")) == 3
    assert archive.scenario("cost_0015_base")["total_return_pct"] == pytest.approx(39.18)
    assert archive.primary["scenario"] == "conservative_1h"
    assert len(archive.surface_scope("test_all")) == 5
    assert archive.fold_stability("cost_0015_base")["fold"].tolist() == [0, 1, 2, 3, 4]
    assert archive.checks_failed == ()


def test_adapter_flags_missing_files(tmp_path):
    root = _write_archive(tmp_path / "archive")
    (root / "scenario_metrics.parquet").unlink()
    with pytest.raises(RobustnessArchiveError):
        load_robustness_archive(root)


def test_adapter_flags_broken_gate_and_failed_checks(tmp_path):
    root = _write_archive(tmp_path / "archive", strategic_gate="GO")
    (root / "checks.json").write_text(
        json.dumps([{"id": "CP08-03.5", "status": "FAIL", "check": "costi", "detail": "x"}]),
        encoding="utf-8",
    )
    archive = load_robustness_archive(root)
    joined = " ".join(archive.issues)
    assert "strategic_gate" in joined
    assert "CP08-03.5" in joined


def test_cost_figure_separates_frozen_and_refit(tmp_path):
    archive = load_robustness_archive(_write_archive(tmp_path / "archive"))
    figure = make_cost_sensitivity_figure(archive.scenarios)
    names = [trace.name for trace in figure.data]
    assert any("congelate" in name for name in names)
    assert any("rifittata" in name for name in names)
    assert figure.layout.title.text.startswith("Costi")


def test_latency_figure_has_return_and_drawdown(tmp_path):
    archive = load_robustness_archive(_write_archive(tmp_path / "archive"))
    figure = make_latency_robustness_figure(archive.scenarios)
    assert len(figure.data) == 2
    names = [trace.name for trace in figure.data]
    assert any("Rendimento" in name for name in names)
    assert any("mark-to-market" in name.lower() for name in names)


def test_surface_figure_marks_frozen_threshold_and_is_exploratory(tmp_path):
    archive = load_robustness_archive(_write_archive(tmp_path / "archive"))
    figure = make_threshold_surface_figure(archive.surface)
    names = [trace.name for trace in figure.data]
    assert any("esplorativo" in name for name in names)
    assert any("CI 95%" in name for name in names)
    assert any("Training" in name for name in names)
    shapes = [shape for shape in figure.layout.shapes if shape.type == "line"]
    assert len(shapes) >= 3  # frozen threshold + 8% + 10%


def test_interval_figure_uses_bootstrap_bounds(tmp_path):
    archive = load_robustness_archive(_write_archive(tmp_path / "archive"))
    figure = make_scenario_interval_figure(archive.scenarios)
    trace = figure.data[0]
    assert trace.error_x.type == "data"
    assert len(trace.x) >= 3


def test_figures_are_safe_on_empty_frames():
    empty = pd.DataFrame()
    assert len(make_cost_sensitivity_figure(empty).data) == 0
    assert len(make_latency_robustness_figure(empty).data) == 0
    assert len(make_threshold_surface_figure(empty).data) == 0
    assert len(make_scenario_interval_figure(empty).data) == 0


def test_component_renders_without_exception_on_the_real_archive():
    """Read-only smoke test of the new tab with the real additive archive."""

    root = Path(__file__).resolve().parents[2]
    archive_dir = root / "data" / "experiments" / "S02-CP08-parameter-robustness-v1"
    if not (archive_dir / "manifest.json").is_file():
        pytest.skip("CP08-03 archive not available")

    from streamlit.testing.v1 import AppTest

    code = (
        "import sys\n"
        f"sys.path.insert(0, r'{root.as_posix()}/src')\n"
        "from crypto_decision_lab.dashboard.config import load_config\n"
        "from crypto_decision_lab.dashboard.components.parameter_robustness import (\n"
        "    render_parameter_robustness,\n"
        ")\n"
        "render_parameter_robustness(load_config())\n"
    )
    app = AppTest.from_string(code, default_timeout=120)
    app.run()
    assert list(app.exception) == []
    assert len(app.tabs) == 5
    assert len(app.metric) >= 6
    assert len(app.get("plotly_chart")) == 4
