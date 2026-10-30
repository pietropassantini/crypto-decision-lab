"""Tests for the read-only S02-CP08-04 dashboard adapter, chart and tab."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from crypto_decision_lab.dashboard.charts import make_validation_timeline_figure
from crypto_decision_lab.dashboard.validation_adapter import (
    ValidationArchiveError,
    load_validation_archive,
)

ROOT = Path(__file__).resolve().parents[2]


def _write_archive(root: Path, *, strategic_gate: str = "NO-GO") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    summary = {
        "schema_version": "cdl.cp08.independent_validation.v1",
        "protocol": {
            "protocol_id": "momentum-independent-validation",
            "protocol_version": "1.0.0",
            "canonical_sha256": "a" * 64,
            "config_sha256": "b" * 64,
        },
        "technical_gate": "PASS",
        "strategic_gate": strategic_gate,
        "independent_validation": {
            "status": "NOT_EVALUATED",
            "data_available": False,
            "verdict": "NOT_EVALUATED",
            "start_utc": "2026-10-23T16:00:00Z",
        },
        "frozen_benchmark": {
            "expected": {
                "policy_id": "momentum_threshold_5pct",
                "threshold": 0.05,
                "trades": 106,
                "total_return_pct": 39.17576428398106,
                "profit_factor_usdt": 1.3295465337690715,
                "initial_capital_usdt": 10000.0,
            }
        },
        "primary_scenario": {
            "id": "realistic_1h_cost20_carry",
            "latency_hours": 1,
            "fee_rate": 0.001,
            "slippage": 0.001,
            "total_cost_per_side": 0.002,
            "short_carry": {"carry_pct_per_trade": 0.14, "status": "HYPOTHESIS_NOT_HISTORICAL"},
        },
        "criteria": {
            "sample_requirements": {
                "min_trades": 100,
                "min_folds": 5,
                "min_independent_days": 180,
            }
        },
        "registry_state": {
            "datasets": 0,
            "evaluations": 0,
            "final_evaluations": 0,
            "final_consumed": False,
        },
    }
    (root / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    criteria = {
        "sample_requirements": summary["criteria"]["sample_requirements"],
        "go_criteria": [
            {
                "id": "net_return_positive",
                "metric": "total_return_pct",
                "operator": ">",
                "value": 0.0,
                "description": "positive",
            }
        ],
        "no_go_criteria": [
            {
                "id": "net_return_non_positive",
                "metric": "total_return_pct",
                "operator": "<=",
                "value": 0.0,
                "description": "non positive",
            }
        ],
        "decision_rule": ["NO-GO if any no_go criterion triggers"],
    }
    (root / "criteria.json").write_text(json.dumps(criteria), encoding="utf-8")
    data_registry = {
        "research_period": {
            "start_utc": "2021-01-01T00:00:00Z",
            "end_exclusive_utc": "2026-10-23T16:00:00Z",
            "last_used_target_end_utc": "2026-10-23T00:00:00Z",
        },
        "datasets": [
            {"id": "btcusdt_1h_snapshot_v1", "role": "research_used"},
            {"id": "independent_window", "role": "independent_untouched"},
        ],
        "separation": {
            "observed_unused_tail": ["2026-10-23T00:00:00+00:00", "2026-10-23T16:00:00+00:00"],
            "independent_start_utc": "2026-10-23T16:00:00+00:00",
            "all_ok": True,
        },
    }
    (root / "data_registry.json").write_text(json.dumps(data_registry), encoding="utf-8")
    checks = [
        {"id": "CP08-04.1", "status": "PASS", "check": "schema", "detail": "ok"},
        {"id": "CP08-04.13", "status": "WARN", "check": "no data", "detail": "awaiting"},
    ]
    (root / "checks.json").write_text(json.dumps(checks), encoding="utf-8")
    manifest = {
        "schema_version": "cdl.cp08.independent_validation.v1",
        "read_only": True,
        "technical_gate": "PASS",
        "strategic_gate": strategic_gate,
        "verdict": "NOT_EVALUATED",
        "independent_validation_status": "NOT_EVALUATED",
        "frozen_artifacts_unchanged": True,
        "original_artifacts_modified": False,
        "canonical_sha256": "a" * 64,
        "config_sha256": "b" * 64,
        "engine_sha256": "c" * 64,
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "protocol.json").write_text(
        json.dumps(
            {
                "protocol_meta": {
                    "protocol_id": "momentum-independent-validation",
                    "protocol_version": "1.0.0",
                    "canonical_sha256": "a" * 64,
                },
                "protocol": {
                    "threshold_preregistration": {
                        "grid": [0.0, 0.05, 0.08, 0.10, 0.20],
                        "selection_rule": "argmax_train_compounded_net_multiple",
                        "selection_split": "train_only",
                        "tie_break": "lowest_threshold_in_grid_order",
                        "grid_is_closed": True,
                    },
                    "walk_forward": {
                        "type": "expanding",
                        "test_windows_per_fold": 60,
                        "min_train_windows": 200,
                        "purge_hours": 24,
                        "folds_min_for_conclusive": 5,
                        "interim_looks": {"gating": False},
                    },
                    "anti_reuse_controls": {
                        "one_final_evaluation_per_protocol_version": True,
                        "require_dataset_registration": True,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "frozen_benchmark.json").write_text(
        json.dumps({"expected": summary["frozen_benchmark"]["expected"], "comparisons": []}),
        encoding="utf-8",
    )
    (root / "confirmation_engine.json").write_text(
        json.dumps(
            {
                "status": "SPECIFICATION_ONLY",
                "active_rules": 0,
                "config": {
                    "engine_id": "momentum-confirmation-engine",
                    "engine_version": "0.1.0",
                    "status": "SPECIFICATION_ONLY",
                    "runnable": False,
                    "rules": {"active": []},
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "anti_reuse.json").write_text(json.dumps({"controls": {}, "registry": {}}), encoding="utf-8")
    return root


def test_adapter_reads_protocol_archive(tmp_path):
    archive = load_validation_archive(_write_archive(tmp_path / "archive"))
    assert archive.protocol_version == "1.0.0"
    assert archive.technical_gate == "PASS"
    assert archive.strategic_gate == "NO-GO"
    assert archive.verdict == "NOT_EVALUATED"
    assert archive.data_available is False
    assert archive.checks_failed == ()
    assert archive.registry_state["final_evaluations"] == 0
    assert archive.issues == ()


def test_adapter_flags_missing_required_file(tmp_path):
    root = _write_archive(tmp_path / "archive")
    (root / "criteria.json").unlink()
    with pytest.raises(ValidationArchiveError):
        load_validation_archive(root)


def test_adapter_flags_broken_gate_and_failed_check(tmp_path):
    root = _write_archive(tmp_path / "archive", strategic_gate="GO")
    (root / "checks.json").write_text(
        json.dumps([{"id": "CP08-04.4", "status": "FAIL", "check": "benchmark", "detail": "x"}]),
        encoding="utf-8",
    )
    archive = load_validation_archive(root)
    joined = " ".join(archive.issues)
    assert "strategic_gate" in joined
    assert "CP08-04.4" in joined


def test_timeline_figure_has_three_regions(tmp_path):
    archive = load_validation_archive(_write_archive(tmp_path / "archive"))
    figure = make_validation_timeline_figure(archive.data_registry, min_independent_days=180)
    assert len(figure.layout.shapes) == 4  # research, observed tail, independent, boundary
    annotations = " ".join(
        str(annotation.text) for annotation in figure.layout.annotations
    )
    assert "RICERCA" in annotations
    assert "VALIDAZIONE INDIPENDENTE" in annotations
    assert "non indipendente" in annotations


def test_timeline_figure_is_safe_on_empty_registry():
    figure = make_validation_timeline_figure({})
    assert len(figure.data) == 0


def test_component_renders_without_exception_on_the_real_archive():
    archive_dir = ROOT / "data" / "experiments" / "S02-CP08-independent-validation-protocol-v1"
    if not (archive_dir / "manifest.json").is_file():
        pytest.skip("CP08-04 archive not available")

    from streamlit.testing.v1 import AppTest

    code = (
        "import sys\n"
        f"sys.path.insert(0, r'{ROOT.as_posix()}/src')\n"
        "from crypto_decision_lab.dashboard.config import load_config\n"
        "from crypto_decision_lab.dashboard.components.validation_protocol import (\n"
        "    render_validation_protocol,\n"
        ")\n"
        "render_validation_protocol(load_config())\n"
    )
    app = AppTest.from_string(code, default_timeout=120)
    app.run()
    assert list(app.exception) == []
    assert len(app.tabs) == 4
    assert len(app.metric) == 6
    assert len(app.get("plotly_chart")) == 1
