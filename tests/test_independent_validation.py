"""Tests for S02-CP08-04 independent validation protocol and research freeze."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab.independent_validation import (
    ENGINE_SCHEMA,
    PROTOCOL_SCHEMA,
    STATUS_PASS,
    VERDICT_GO,
    VERDICT_INCONCLUSIVE,
    VERDICT_INTERIM,
    VERDICT_NO_GO,
    VERDICT_NOT_EVALUATED,
    ProtocolViolation,
    ValidationPaths,
    ValidationRegistry,
    _sidecar_for,
    build_independent_folds,
    build_readiness,
    canonical_hash,
    decide_verdict,
    evaluate_independent,
    load_protocol,
    main,
    validate_confirmation_engine,
    validate_evaluation_frame,
    validate_independent_windows,
    validate_protocol,
    write_artifacts,
)
from crypto_decision_lab.parameter_robustness import PREREGISTERED_THRESHOLD_GRID
from crypto_decision_lab.policy_evaluation import make_folds

ROOT = Path(__file__).resolve().parents[1]


def _paths() -> ValidationPaths:
    return ValidationPaths(repo_root=ROOT)


@pytest.fixture(scope="module")
def protocol_bundle():
    return load_protocol(_paths())


@pytest.fixture(scope="module")
def protocol(protocol_bundle):
    return protocol_bundle["config"]


@pytest.fixture(scope="module")
def readiness():
    return build_readiness(_paths())


def _copy(value):
    return json.loads(json.dumps(value))


# ---------------------------------------------------------------------------
# synthetic independent data (used only to test the harness, never the history)
# ---------------------------------------------------------------------------


def _synthetic_dataset(seed: int = 11) -> dict[str, pd.DataFrame]:
    start = pd.Timestamp("2026-01-01", tz="UTC")
    end = pd.Timestamp("2028-06-01", tz="UTC")
    index = pd.date_range(start, end, freq="h", tz="UTC")
    generator = np.random.default_rng(seed)
    returns = 0.00012 + generator.normal(0.0, 0.004, len(index))
    close = 30_000.0 * np.exp(np.cumsum(returns))
    open_ = np.concatenate([[close[0]], close[:-1]])
    market = pd.DataFrame({"open": open_, "close": close}, index=index)

    def windows(count: int, first_cutoff: str, base_index: int) -> pd.DataFrame:
        cutoffs = pd.date_range(first_cutoff, periods=count, freq="24h", tz="UTC")
        entry = market["open"].reindex(cutoffs).to_numpy(dtype=float)
        exit_ = market["open"].reindex(cutoffs + pd.Timedelta(hours=24)).to_numpy(dtype=float)
        reference = market["close"].reindex(cutoffs - pd.Timedelta(hours=1)).to_numpy(dtype=float)
        momentum = []
        for cutoff in cutoffs:
            past = market["close"].loc[cutoff - pd.Timedelta(hours=168): cutoff - pd.Timedelta(hours=1)]
            momentum.append(float(past.iloc[-1] / past.iloc[0] - 1.0))
        return pd.DataFrame(
            {
                "window_index": np.arange(count) + base_index,
                "cutoff": cutoffs,
                "entry_timestamp": cutoffs,
                "exit_timestamp": cutoffs + pd.Timedelta(hours=24),
                "reference_price": reference,
                "entry_open": entry,
                "exit_open": exit_,
                "momentum_168h": momentum,
            }
        )

    return {
        "history": windows(200, "2026-03-01", 0),
        "future": windows(300, "2026-11-01", 1_000),
        "market": market,
    }


@pytest.fixture(scope="module")
def synthetic():
    return _synthetic_dataset()


# ---------------------------------------------------------------------------
# protocol structure and immutability
# ---------------------------------------------------------------------------


def test_protocol_loads_frozen_with_preregistered_grid(protocol_bundle):
    protocol = protocol_bundle["config"]
    assert protocol["schema_version"] == PROTOCOL_SCHEMA
    assert protocol["status"] == "FROZEN"
    assert protocol["protocol_id"] == "momentum-independent-validation"
    assert protocol["protocol_version"] == "1.0.0"
    grid = [float(value) for value in protocol["threshold_preregistration"]["grid"]]
    assert grid == [float(value) for value in PREREGISTERED_THRESHOLD_GRID]
    for required in (0.05, 0.08, 0.10):
        assert required in grid
    assert protocol["threshold_preregistration"]["selection_split"] == "train_only"
    validate_protocol(protocol)


def test_protocol_validation_rejects_schema_and_grid_drift(protocol):
    bad = _copy(protocol)
    bad["schema_version"] = "something.else"
    with pytest.raises(ProtocolViolation):
        validate_protocol(bad)

    bad = _copy(protocol)
    bad["threshold_preregistration"]["grid"] = bad["threshold_preregistration"]["grid"][:-1]
    with pytest.raises(ProtocolViolation):
        validate_protocol(bad)


def test_protocol_validation_rejects_overlapping_independent_start(protocol):
    bad = _copy(protocol)
    bad["independent_validation"]["start_utc"] = "2026-10-01T00:00:00Z"
    with pytest.raises(ProtocolViolation):
        validate_protocol(bad)


def test_protocol_validation_rejects_unflagged_short_carry(protocol):
    bad = _copy(protocol)
    bad["primary_scenario"]["short_carry"]["status"] = "HISTORICAL_DATA"
    with pytest.raises(ProtocolViolation):
        validate_protocol(bad)


def test_criteria_contradiction_is_rejected(protocol):
    bad = _copy(protocol)
    bad["go_nogo"]["go_criteria"].append(
        {
            "id": "impossible_overlap",
            "metric": "total_return_pct",
            "operator": "<",
            "value": 5.0,
            "description": "deliberate contradiction for the test",
        }
    )
    with pytest.raises(ProtocolViolation):
        validate_protocol(bad)


def test_protocol_hash_is_deterministic_and_sensitive(protocol_bundle):
    protocol = protocol_bundle["config"]
    assert canonical_hash(protocol) == canonical_hash(_copy(protocol))
    assert canonical_hash(protocol) == protocol_bundle["canonical_sha256"]
    mutated = _copy(protocol)
    mutated["primary_scenario"]["fee_rate"] = 0.002
    assert canonical_hash(mutated) != protocol_bundle["canonical_sha256"]


def test_sidecar_pins_the_configuration(tmp_path, protocol_bundle):
    paths = _paths()
    sidecar = _sidecar_for(paths.config_file, protocol_bundle["config_sha256"])
    assert sidecar["sidecar_present"] is True
    assert sidecar["match"] is True
    engine_sidecar = _sidecar_for(paths.engine_config_file, protocol_bundle["engine_config_sha256"])
    assert engine_sidecar["match"] is True

    tampered = tmp_path / "protocol.yaml"
    tampered.write_bytes(paths.config_file.read_bytes() + b"\n# tampered\n")
    (tmp_path / "protocol.yaml.sha256").write_text(
        f"{protocol_bundle['config_sha256']}  protocol.yaml\n", encoding="utf-8"
    )
    from crypto_decision_lab.sync import digest

    assert _sidecar_for(tampered, digest(tampered))["match"] is False


def test_confirmation_engine_is_specification_only(protocol_bundle):
    engine = protocol_bundle["engine_config"]
    assert engine["schema_version"] == ENGINE_SCHEMA
    assert engine["status"] == "SPECIFICATION_ONLY"
    assert engine["runnable"] is False
    assert engine["rules"]["active"] == []
    validate_confirmation_engine(engine)

    bad = _copy(engine)
    bad["rules"]["active"] = [{"id": "vol_high_filter"}]
    with pytest.raises(ProtocolViolation):
        validate_confirmation_engine(bad)

    bad = _copy(engine)
    bad["interface"]["allowed_input_features"].append("best_net_pct")
    with pytest.raises(ProtocolViolation):
        validate_confirmation_engine(bad)


# ---------------------------------------------------------------------------
# frozen artifacts and benchmark reconciliation
# ---------------------------------------------------------------------------


def test_all_frozen_artifacts_match_their_hashes(readiness):
    artifacts = readiness["frozen_artifacts"]
    assert len(artifacts) >= 15
    assert all(item["exists"] for item in artifacts)
    assert all(item["match"] for item in artifacts)
    assert {item["role"] for item in artifacts} >= {"research_used", "market_data_observed"}


def test_frozen_benchmark_reconciles_with_cp08_03(readiness):
    benchmark = readiness["frozen_benchmark"]
    assert benchmark["all_match"] is True
    assert benchmark["cp08_03_gate_ok"] is True
    deltas = [abs(row["delta"]) for row in benchmark["comparisons"]]
    assert max(deltas) < 1e-9
    observed = {row["metric"]: row["observed"] for row in benchmark["comparisons"]}
    assert observed["total_return_pct"] == pytest.approx(39.17576428398106, abs=1e-9)
    assert observed["profit_factor_usdt"] == pytest.approx(1.3295465337690715, abs=1e-9)


def test_research_data_registry_matches_declared_boundaries(readiness):
    registry = readiness["data_registry"]
    assert registry["all_match"] is True
    observed = registry["observed"]
    assert observed["labels_rows"] == 500
    assert observed["oos_windows"] == 300
    assert observed["folds"] == 5
    assert observed["labels_last_exit_utc"].startswith("2026-10-23T00:00:00")
    assert observed["snapshot_end_exclusive_utc"].startswith("2026-10-23T16:00:00")


def test_data_separation_excludes_the_observed_tail(readiness, protocol):
    separation = readiness["data_separation"]
    assert separation["all_ok"] is True
    assert separation["tail_not_independent"] is True
    assert separation["independent_after_research"] is True
    assert separation["no_overlap"] is True
    tail = separation["observed_unused_tail"]
    assert pd.Timestamp(tail[0]) == pd.Timestamp(
        protocol["research_phase"]["observed_unused_tail"]["start_utc"]
    )
    assert pd.Timestamp(tail[1]) == pd.Timestamp(
        protocol["research_phase"]["observed_unused_tail"]["end_exclusive_utc"]
    )


# ---------------------------------------------------------------------------
# reuse guards
# ---------------------------------------------------------------------------


def test_independent_windows_reject_research_period(protocol):
    research = pd.DataFrame(
        {
            "window_index": [1],
            "cutoff": [pd.Timestamp("2026-05-01", tz="UTC")],
            "entry_timestamp": [pd.Timestamp("2026-05-01", tz="UTC")],
            "exit_timestamp": [pd.Timestamp("2026-05-02", tz="UTC")],
        }
    )
    with pytest.raises(ProtocolViolation):
        validate_independent_windows(research, protocol)


def test_independent_windows_reject_the_observed_tail(synthetic, protocol):
    # The tail ends at 2026-10-23T16:00Z.  A decision taken before 16:00 has a
    # target that ends inside/at the tail even after shifting the boundary, so
    # the guard must refuse it.
    bad = _copy(protocol)
    bad["independent_validation"]["start_utc"] = "2026-10-22T00:00:00Z"
    tail_window = pd.DataFrame(
        {
            "window_index": [1],
            "cutoff": [pd.Timestamp("2026-10-22T00:00:00Z")],
            "entry_timestamp": [pd.Timestamp("2026-10-22T00:00:00Z")],
            "exit_timestamp": [pd.Timestamp("2026-10-23T00:00:00Z")],
        }
    )
    with pytest.raises(ProtocolViolation):
        validate_independent_windows(tail_window, bad)


def test_independent_windows_accept_future_data(synthetic, protocol):
    validation = validate_independent_windows(synthetic["future"], protocol)
    assert validation["boundary_ok"] is True
    assert validation["windows"] == 300


def test_evaluation_frame_requires_the_audited_columns(synthetic):
    validate_evaluation_frame(synthetic["future"], label="future")
    broken = synthetic["future"].drop(columns=["momentum_168h"])
    with pytest.raises(ProtocolViolation):
        validate_evaluation_frame(broken, label="future")


def test_folds_are_expanding_purged_and_all_tests_independent(synthetic, protocol):
    result = build_independent_folds(synthetic["history"], synthetic["future"], protocol)
    assert result["n_folds"] == 5
    assert result["all_tests_independent"] is True
    assert result["leftover_windows"] == 0
    assert result["future_window"]["boundary_ok"] is True
    combined = pd.concat([synthetic["history"], synthetic["future"]], ignore_index=True)
    for fold in result["folds"]:
        train = combined.loc[fold["train_index"]]
        test = combined.loc[fold["test_index"]]
        assert len(test) == 60
        assert int(test.index.min()) >= 200
        assert train["cutoff"].max() < test["cutoff"].min()
        assert (train["cutoff"] + pd.Timedelta(hours=24) <= test["cutoff"].iloc[0]).all()
        assert fold["purged_windows"] >= 0


def test_fold_builder_matches_the_audited_cp07_fold_maker(synthetic, protocol):
    result = build_independent_folds(synthetic["history"], synthetic["future"], protocol)
    combined = pd.concat(
        [synthetic["history"], synthetic["future"]], ignore_index=True
    ).sort_values("cutoff").reset_index(drop=True)
    reference = make_folds(combined, n_folds=5, min_train=200, test_size=60)
    for ours, theirs in zip(result["folds"], reference):
        assert ours["train_index"] == theirs["train_index"]
        assert ours["test_index"] == theirs["test_index"]
        assert ours["purged_windows"] == theirs["purged_windows"]


def test_fold_builder_refuses_short_history(synthetic, protocol):
    with pytest.raises(ProtocolViolation):
        build_independent_folds(synthetic["history"].iloc[:150], synthetic["future"], protocol)


def test_threshold_selection_uses_training_rows_only(synthetic, protocol):
    from crypto_decision_lab.independent_validation import _window_frame
    from crypto_decision_lab.parameter_robustness import cost_model, select_threshold

    result = build_independent_folds(synthetic["history"], synthetic["future"], protocol)
    combined = pd.concat(
        [_window_frame(synthetic["history"]), _window_frame(synthetic["future"])],
        ignore_index=True,
    ).sort_values("cutoff").reset_index(drop=True)
    primary = protocol["primary_scenario"]
    costs = cost_model(float(primary["fee_rate"]), float(primary["slippage"]))
    grid = tuple(float(value) for value in protocol["threshold_preregistration"]["grid"])
    poison_columns = ["entry_open", "exit_open", "momentum_168h"]
    for fold in result["folds"]:
        baseline = select_threshold(combined.loc[fold["train_index"]], costs=costs, grid=grid)
        poisoned = combined.copy()
        poisoned.loc[fold["test_index"], poison_columns] = np.nan
        perturbed = select_threshold(poisoned.loc[fold["train_index"]], costs=costs, grid=grid)
        assert baseline["threshold"] == pytest.approx(perturbed["threshold"])
        assert any(
            abs(baseline["threshold"] - candidate) < 1e-12 for candidate in grid
        )


# ---------------------------------------------------------------------------
# GO / NO-GO decision
# ---------------------------------------------------------------------------


def _go_metrics() -> dict:
    return {
        "total_return_pct": 12.0,
        "ci_return_low_pct": 1.0,
        "ci_return_high_pct": 30.0,
        "profit_factor_usdt": 1.30,
        "max_drawdown_mtm_pct": -20.0,
        "max_drawdown_exit_pct": -15.0,
        "positive_folds": 4,
        "max_abs_fold_share_pct": 45.0,
        "return_after_removing_top5_pct": 3.0,
        "selected_thresholds_in_grid": 1,
        "protocol_violations": 0,
        "trades": 120,
        "folds": 5,
        "independent_days": 200.0,
    }


def test_verdict_go_when_all_criteria_pass(protocol):
    result = decide_verdict(_go_metrics(), protocol)
    assert result["verdict"] == VERDICT_GO
    assert result["gating"] is True
    assert result["sample_met"] is True
    assert result["no_go_triggered"] == []


def test_verdict_no_go_on_hard_failure(protocol):
    metrics = _go_metrics()
    metrics["total_return_pct"] = -2.0
    metrics["profit_factor_usdt"] = 0.9
    result = decide_verdict(metrics, protocol)
    assert result["verdict"] == VERDICT_NO_GO
    assert result["gating"] is False
    assert {"net_return_non_positive", "profit_factor_below_one"} <= set(result["no_go_triggered"])


def test_verdict_inconclusive_when_sample_is_too_small(protocol):
    metrics = _go_metrics()
    metrics["trades"] = 40
    metrics["folds"] = 3
    metrics["independent_days"] = 90.0
    result = decide_verdict(metrics, protocol)
    assert result["verdict"] == VERDICT_INCONCLUSIVE
    assert result["sample_met"] is False


def test_verdict_inconclusive_when_ci_includes_zero(protocol):
    metrics = _go_metrics()
    metrics["ci_return_low_pct"] = -1.0
    result = decide_verdict(metrics, protocol)
    assert result["verdict"] == VERDICT_INCONCLUSIVE
    assert "net_return_ci_low_positive" in result["go_failed"]


def test_interim_look_can_never_produce_go(protocol):
    result = decide_verdict(_go_metrics(), protocol, final=False)
    assert result["verdict"] == VERDICT_INTERIM
    assert result["gating"] is False


# ---------------------------------------------------------------------------
# registry and reuse prevention
# ---------------------------------------------------------------------------


def _dataset_record(sha: str = "a" * 64) -> dict:
    return {
        "dataset_id": "future-btcusdt-v1",
        "sha256": sha,
        "role": "independent",
        "registered_utc": "2026-11-01T00:00:00+00:00",
    }


def _evaluation_record(**overrides) -> dict:
    record = {
        "protocol_id": "momentum-independent-validation",
        "protocol_version": "1.0.0",
        "protocol_sha256": "b" * 64,
        "dataset_id": "future-btcusdt-v1",
        "dataset_sha256": "a" * 64,
        "final": True,
        "verdict": VERDICT_INCONCLUSIVE,
        "recorded_utc": "2026-11-02T00:00:00+00:00",
        "metrics_sha256": "c" * 64,
    }
    record.update(overrides)
    return record


def test_registry_blocks_a_second_final_evaluation(tmp_path):
    registry = ValidationRegistry(tmp_path / "registry")
    registry.register_dataset(_dataset_record())
    first = registry.record_evaluation(_evaluation_record())
    assert first["gating"] is False
    assert registry.state("b" * 64)["final_evaluations"] == 1
    with pytest.raises(ProtocolViolation):
        registry.record_evaluation(_evaluation_record())


def test_registry_requires_a_registered_dataset(tmp_path):
    registry = ValidationRegistry(tmp_path / "registry")
    with pytest.raises(ProtocolViolation):
        registry.record_evaluation(_evaluation_record())
    registry.register_dataset(_dataset_record())
    with pytest.raises(ProtocolViolation):
        registry.record_evaluation(_evaluation_record(dataset_sha256="d" * 64))


def test_registry_blocks_dataset_hash_swap(tmp_path):
    registry = ValidationRegistry(tmp_path / "registry")
    registry.register_dataset(_dataset_record())
    with pytest.raises(ProtocolViolation):
        registry.register_dataset(_dataset_record(sha="f" * 64))


def test_registry_allows_interim_but_never_marks_it_gating(tmp_path):
    registry = ValidationRegistry(tmp_path / "registry")
    registry.register_dataset(_dataset_record())
    interim = registry.record_evaluation(_evaluation_record(final=False))
    assert interim["verdict"] == VERDICT_INTERIM
    assert interim["gating"] is False
    final = registry.record_evaluation(_evaluation_record(final=True))
    assert final["verdict"] == VERDICT_INCONCLUSIVE
    with pytest.raises(ProtocolViolation):
        registry.record_evaluation(_evaluation_record(final=False))


# ---------------------------------------------------------------------------
# end-to-end harness on synthetic data
# ---------------------------------------------------------------------------


def test_evaluate_independent_end_to_end_on_synthetic_data(synthetic, protocol, protocol_bundle, tmp_path):
    registry = ValidationRegistry(tmp_path / "registry")
    report = evaluate_independent(
        synthetic["future"],
        synthetic["history"],
        synthetic["market"],
        protocol,
        dataset_id="synthetic-independent-v1",
        registry=registry,
        expected_protocol_sha256=protocol_bundle["canonical_sha256"],
        notes="synthetic harness test",
    )
    assert report["verdict"]["verdict"] in {
        VERDICT_GO,
        VERDICT_NO_GO,
        VERDICT_INCONCLUSIVE,
    }
    assert report["metrics"]["folds"] == 5
    assert report["metrics"]["protocol_violations"] == 0
    assert report["metrics"]["selected_thresholds_in_grid"] == 1
    assert report["metrics"]["trades"] > 0
    assert len(report["folds"]) == 5
    assert len(report["selection"]) == 5
    assert registry.state(protocol_bundle["canonical_sha256"])["final_evaluations"] == 1
    with pytest.raises(ProtocolViolation):
        evaluate_independent(
            synthetic["future"],
            synthetic["history"],
            synthetic["market"],
            protocol,
            dataset_id="synthetic-independent-v1",
            registry=registry,
            expected_protocol_sha256=protocol_bundle["canonical_sha256"],
        )


def test_evaluate_independent_requires_the_frozen_protocol_hash(synthetic, protocol, tmp_path):
    registry = ValidationRegistry(tmp_path / "registry")
    with pytest.raises(ProtocolViolation):
        evaluate_independent(
            synthetic["future"],
            synthetic["history"],
            synthetic["market"],
            protocol,
            dataset_id="synthetic-independent-v1",
            registry=registry,
            expected_protocol_sha256="0" * 64,
        )


def test_evaluate_independent_refuses_research_windows(synthetic, protocol, protocol_bundle, tmp_path):
    registry = ValidationRegistry(tmp_path / "registry")
    with pytest.raises(ProtocolViolation):
        evaluate_independent(
            synthetic["history"],
            synthetic["history"],
            synthetic["market"],
            protocol,
            dataset_id="research-reuse",
            registry=registry,
            expected_protocol_sha256=protocol_bundle["canonical_sha256"],
        )


# ---------------------------------------------------------------------------
# readiness report, archive and CLI
# ---------------------------------------------------------------------------


def test_readiness_gate_and_status(readiness):
    assert readiness["technical_gate"] == STATUS_PASS
    assert readiness["checks_failed"] == []
    assert len(readiness["checks"]) == 15
    statuses = {check["id"]: check["status"] for check in readiness["checks"]}
    assert statuses["CP08-04.13"] == "WARN"
    assert all(
        status == "PASS" for cid, status in statuses.items() if cid != "CP08-04.13"
    )
    assert readiness["strategic_gate"] == VERDICT_NO_GO
    assert readiness["independent_validation"]["status"] == VERDICT_NOT_EVALUATED
    assert readiness["independent_validation"]["data_available"] is False
    assert readiness["confirmation_engine_started"] is False
    assert readiness["intern_decision_0_8b_started"] is False
    assert readiness["registry"]["state"]["final_evaluations"] == 0


def test_readiness_is_deterministic(readiness, protocol_bundle):
    again = build_readiness(_paths())
    assert again["protocol_meta"]["canonical_sha256"] == protocol_bundle["canonical_sha256"]
    assert again["protocol_meta"]["config_sha256"] == protocol_bundle["config_sha256"]
    assert again["checks_failed"] == readiness["checks_failed"]
    assert again["data_separation"] == readiness["data_separation"]


def test_archive_write_is_additive_and_refuses_overwrite(readiness, tmp_path):
    output = tmp_path / "S02-CP08-independent-validation-protocol-v1"
    paths = ValidationPaths(
        repo_root=ROOT,
        config_path=ValidationPaths.config_path,
        engine_config_path=ValidationPaths.engine_config_path,
        registry_dir=tmp_path / "registry",
        output_dir=output,
    )
    written = write_artifacts(paths, readiness)
    manifest = written["manifest"]
    assert manifest["technical_gate"] == STATUS_PASS
    assert manifest["strategic_gate"] == VERDICT_NO_GO
    assert manifest["verdict"] == VERDICT_NOT_EVALUATED
    assert manifest["frozen_artifacts_unchanged"] is True
    assert manifest["original_artifacts_modified"] is False
    assert (output / "manifest.json").is_file()
    assert (output / "momentum-independent-validation-v1.yaml").read_bytes() == readiness["config_bytes"]
    assert (output / "momentum-confirmation-engine-v1.yaml").read_bytes() == readiness["engine_config_bytes"]
    with pytest.raises(FileExistsError):
        write_artifacts(paths, readiness)


def test_cli_no_write_returns_zero(capsys):
    exit_code = main(["--repo-root", str(ROOT), "--no-write"])
    captured = capsys.readouterr()
    assert exit_code == 0
    payload = json.loads(captured.out)
    assert payload["technical_gate"] == STATUS_PASS
    assert payload["strategic_gate"] == VERDICT_NO_GO
