"""S02 tests: versioned Dataset Builder.

* CP02.1 — two builds with the same inputs and parameters produce identical
  feature and target contents (canonical content digests); build timestamps
  live only in the manifest metadata.
* CP02.2 — forward inputs are refused by identity; feature frames whose
  availability exceeds the cutoff are refused; windows lacking the required
  bars are refused.
* CP02.3 — a fixture with known prices and times reconstructs the target
  with the same economic outcome as the S03 engine primitive
  (``resolve_round_trip``) and the legacy CP07 formulas.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab.dashboard.data_loader import MarketData
from crypto_decision_lab.decision_contract import ACTION_LONG, CostModel, net_return
from crypto_decision_lab.dataset_builder import (
    FEATURE_COLUMNS,
    TARGET_COLUMNS,
    BuildSpec,
    DatasetBuilderError,
    FeatureAvailabilityError,
    WindowDataError,
    build_dataset,
    build_features,
    build_targets,
    canonical_frame_digest,
    eligible_cutoffs,
    validate_execution_window,
    validate_feature_frame,
    validate_feature_inputs,
    validate_feature_window,
    verify_dataset,
)
from crypto_decision_lab.execution_contract import ExecutionHypothesis
from crypto_decision_lab.forward_isolation import ForwardIsolationViolation

FEE = 0.001
SLIP = 0.0005
COSTS = CostModel(fee_rate=FEE, slippage=SLIP)


def make_market(start="2024-01-01", hours=300, base=100.0, drift=0.001) -> pd.DataFrame:
    index = pd.date_range(start, periods=hours, freq="1h", tz="UTC")
    open_ = base * (1.0 + drift) ** np.arange(hours, dtype=float)
    return pd.DataFrame(
        {
            "open": open_,
            "high": open_ * 1.01,
            "low": open_ * 0.99,
            "close": open_ * 1.005,
            "volume": np.full(hours, 12.0),
        },
        index=index,
    )


def make_market_data(frame: pd.DataFrame, tmp_path: Path) -> MarketData:
    return MarketData(
        frame=frame,
        dataset_id="fixture-1h",
        snapshot_dir=tmp_path / "snapshots" / "fixture-1h",
        data_root=tmp_path,
        source_manifest_sha256=None,
        source_manifest_path=None,
        source_files=0,
    )


def default_spec(**overrides) -> BuildSpec:
    params = dict(
        dataset_id="FIXTURE-1h-research",
        version="v1",
        stride_hours=24,
        warmup_hours=168,
        hypothesis=ExecutionHypothesis(latency_hours=0, horizon_hours=24, fee_rate=FEE, slippage=SLIP),
    )
    params.update(overrides)
    return BuildSpec(**params)


class TestEligibleCutoffs:
    def test_stride_and_completeness(self):
        market = make_market(hours=300)
        spec = default_spec()
        cutoffs = eligible_cutoffs(market, spec)
        assert cutoffs[0] == market.index[0] + pd.Timedelta(hours=168)
        gaps = pd.Series(cutoffs).diff().dropna()
        assert (gaps == pd.Timedelta(hours=24)).all()
        for cutoff in cutoffs:
            validate_feature_window(market, cutoff, spec)
            validate_execution_window(market, cutoff, spec.hypothesis)

    def test_windows_straddling_gaps_are_skipped(self):
        market = make_market(hours=300)
        hole = market.index[180]
        punctured = market.drop(index=[hole])
        spec = default_spec()
        cutoffs = eligible_cutoffs(punctured, spec)
        # The cutoff whose warmup or holding period covers the hole is skipped.
        assert all(validate_execution_window(punctured, c, spec.hypothesis) for c in cutoffs)
        with pytest.raises(WindowDataError):
            validate_feature_window(punctured, market.index[0] + pd.Timedelta(hours=192), spec)


class TestFeatureGuards:
    def test_feature_inputs_beyond_cutoff_are_rejected(self):
        market = make_market(hours=300)
        spec = default_spec()
        cutoff = market.index[0] + pd.Timedelta(hours=168)
        bars = market.loc[cutoff - pd.Timedelta(hours=168) : cutoff]
        with pytest.raises(FeatureAvailabilityError, match="beyond the cutoff"):
            validate_feature_inputs(bars, cutoff)

    def test_feature_frame_available_after_cutoff_is_rejected(self):
        market = make_market(hours=300)
        spec = default_spec()
        cutoffs = eligible_cutoffs(market, spec)
        features = build_features(market, cutoffs, spec)
        tampered = features.copy()
        tampered["feature_available_at"] = pd.to_datetime(tampered["feature_available_at"], utc=True) + pd.Timedelta(hours=1)
        with pytest.raises(FeatureAvailabilityError, match="available beyond the cutoff"):
            validate_feature_frame(tampered)

    def test_features_carry_availability_and_stationary_columns(self):
        market = make_market(hours=300)
        spec = default_spec()
        cutoffs = eligible_cutoffs(market, spec)
        features = build_features(market, cutoffs, spec)
        assert list(features.columns[:4]) == ["window_index", "cutoff", "feature_available_at", "feature_bar"]
        assert set(FEATURE_COLUMNS).issubset(features.columns)
        assert not {"sma20", "sma50", "ema20", "ema50", "atr14", "close_at_cutoff_bar"} & set(features.columns)
        assert (pd.to_datetime(features["feature_available_at"], utc=True) == pd.to_datetime(features["cutoff"], utc=True)).all()
        assert (pd.to_datetime(features["feature_bar"], utc=True) < pd.to_datetime(features["cutoff"], utc=True)).all()


class TestWindowDataGuards:
    def test_target_window_with_gap_is_rejected(self):
        market = make_market(hours=300)
        cutoff = market.index[0] + pd.Timedelta(hours=168)
        punctured = market.drop(index=[cutoff + pd.Timedelta(hours=10)])
        spec = default_spec()
        with pytest.raises(WindowDataError, match="contiguous"):
            build_targets(punctured, [cutoff], spec)

    def test_missing_exit_bar_is_rejected(self):
        market = make_market(hours=300)
        cutoff = market.index[0] + pd.Timedelta(hours=168)
        shortened = market.iloc[:190]  # exit bar at cutoff + 24h does not exist
        with pytest.raises(WindowDataError, match="exit bar missing"):
            build_targets(shortened, [cutoff], default_spec())

    def test_incomplete_warmup_is_rejected(self):
        market = make_market(hours=300)
        cutoff = market.index[0] + pd.Timedelta(hours=168)
        trimmed = market.iloc[5:]
        with pytest.raises(WindowDataError, match="warmup"):
            build_features(trimmed, [cutoff], default_spec())

    def test_forward_source_is_refused_by_identity(self, tmp_path):
        frame = make_market(hours=300)
        market = make_market_data(frame, tmp_path)
        market = MarketData(
            **{**market.__dict__, "snapshot_dir": tmp_path / "data" / "forward" / "BTCUSDT-1h-forward-v1"}
        )
        with pytest.raises(ForwardIsolationViolation):
            build_dataset(market, default_spec(), output_dir=tmp_path / "out")


class TestTargetsEconomics:
    """CP02.3: known fixture, latency and commissions included."""

    def test_known_prices_reconstruct_engine_outcome(self):
        market = make_market(hours=300, base=100.0, drift=0.0)
        cutoff = market.index[0] + pd.Timedelta(hours=168)
        spec = default_spec(hypothesis=ExecutionHypothesis(latency_hours=1, horizon_hours=24, fee_rate=FEE, slippage=SLIP))
        targets = build_targets(market, [cutoff], spec)
        row = targets.iloc[0]
        entry_open = float(market.loc[cutoff + pd.Timedelta(hours=1), "open"])
        exit_open = float(market.loc[cutoff + pd.Timedelta(hours=25), "open"])
        assert row["entry_open"] == entry_open
        assert row["exit_open"] == exit_open
        # Hand-computed economics with latency and both-side costs.
        entry_exec = entry_open * (1.0 + SLIP)
        exit_exec = exit_open * (1.0 - SLIP)
        expected_net_pct = 100.0 * ((exit_exec * (1.0 - FEE)) / (entry_exec * (1.0 + FEE)) - 1.0)
        assert row["entry_exec_price"] == pytest.approx(entry_exec)
        assert row["exit_exec_price"] == pytest.approx(exit_exec)
        assert row["long_net_return_pct"] == pytest.approx(expected_net_pct)
        assert row["round_trip_cost_pct"] == pytest.approx(row["long_gross_return_pct"] - row["long_net_return_pct"])

    def test_zero_latency_matches_legacy_cp07_economics(self):
        market = make_market(hours=300, base=100.0, drift=0.002)
        cutoff = market.index[0] + pd.Timedelta(hours=168)
        targets = build_targets(market, [cutoff], default_spec())
        row = targets.iloc[0]
        entry_open = float(market.loc[cutoff, "open"])
        exit_open = float(market.loc[cutoff + pd.Timedelta(hours=24), "open"])
        legacy_net_pct = 100.0 * net_return(entry_open, exit_open, ACTION_LONG, COSTS)
        assert row["long_net_return_pct"] == legacy_net_pct
        assert row["entry_open"] == entry_open
        assert row["exit_open"] == exit_open

    def test_targets_and_features_are_separate_frames(self):
        market = make_market(hours=300)
        spec = default_spec()
        cutoffs = eligible_cutoffs(market, spec)
        features = build_features(market, cutoffs, spec)
        targets = build_targets(market, cutoffs, spec)
        assert set(FEATURE_COLUMNS).isdisjoint(set(TARGET_COLUMNS))
        assert list(features["window_index"]) == list(targets["window_index"])
        assert (targets["long_trade_worth"] == (targets["long_net_return_pct"] > 0.0)).all()


class TestDeterminism:
    """CP02.1: same inputs and parameters, identical contents."""

    def test_two_builds_produce_identical_contents(self, tmp_path):
        market = make_market_data(make_market(hours=300), tmp_path)
        spec = default_spec()
        first = build_dataset(market, spec, output_dir=tmp_path / "a")
        second = build_dataset(market, spec, output_dir=tmp_path / "b")
        assert first["features_content_sha256"] == second["features_content_sha256"]
        assert first["targets_content_sha256"] == second["targets_content_sha256"]
        # Parquet bytes are byte-identical for identical frames.
        assert (tmp_path / "a" / "features.parquet").read_bytes() == (tmp_path / "b" / "features.parquet").read_bytes()
        assert (tmp_path / "a" / "targets.parquet").read_bytes() == (tmp_path / "b" / "targets.parquet").read_bytes()
        # created_utc is build metadata and is excluded from the content digests.
        assert "created_utc" in first and "created_utc" in second

    def test_different_parameters_change_the_digest(self, tmp_path):
        market = make_market(hours=300)
        first = build_dataset(make_market_data(market, tmp_path), default_spec(), output_dir=tmp_path / "a", write=False)
        second = build_dataset(
            make_market_data(market, tmp_path),
            default_spec(hypothesis=ExecutionHypothesis(latency_hours=2)),
            output_dir=tmp_path / "b",
            write=False,
        )
        assert first["targets_content_sha256"] != second["targets_content_sha256"]

    def test_refuses_to_overwrite_existing_dataset(self, tmp_path):
        market = make_market_data(make_market(hours=300), tmp_path)
        out = tmp_path / "out"
        build_dataset(market, default_spec(), output_dir=out)
        with pytest.raises(FileExistsError):
            build_dataset(market, default_spec(), output_dir=out)


class TestWriteAndVerify:
    def test_written_dataset_verifies_against_its_manifest(self, tmp_path):
        market = make_market_data(make_market(hours=300), tmp_path)
        out = tmp_path / "dataset"
        result = build_dataset(market, default_spec(), output_dir=out)
        assert (out / "features.parquet").is_file()
        assert (out / "targets.parquet").is_file()
        assert (out / "manifest.json").is_file()
        assert (out / "summary.json").is_file()
        report = verify_dataset(out)
        assert report["ok"], report["mismatches"]
        manifest = __import__("json").loads((out / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["outputs"]["features"]["content_sha256"] == result["features_content_sha256"]
        assert manifest["execution_declaration"]["latency_hours"] == 0
        assert manifest["provenance"]["market_dataset_id"] == "fixture-1h"

    def test_tampered_dataset_fails_verification(self, tmp_path):
        market = make_market_data(make_market(hours=300), tmp_path)
        out = tmp_path / "dataset"
        build_dataset(market, default_spec(), output_dir=out)
        targets = pd.read_parquet(out / "targets.parquet")
        targets.loc[0, "long_net_return_pct"] += 1.0
        targets.to_parquet(out / "targets.parquet", index=False)
        report = verify_dataset(out)
        assert not report["ok"]
        assert any("targets" in item for item in report["mismatches"])

    def test_output_inside_forward_archive_is_refused(self, tmp_path):
        market = make_market_data(make_market(hours=300), tmp_path)
        forward_out = tmp_path / "data" / "forward" / "research-out"
        with pytest.raises(ForwardIsolationViolation):
            build_dataset(market, default_spec(), output_dir=forward_out)


class TestSpec:
    def test_rejects_invalid_params(self):
        with pytest.raises(DatasetBuilderError):
            BuildSpec(stride_hours=0)
        with pytest.raises(DatasetBuilderError):
            BuildSpec(dataset_id="", version="v1")

    def test_params_digest_is_stable_and_param_sensitive(self):
        base = default_spec().params_digest()
        same = default_spec().params_digest()
        other = default_spec(hypothesis=ExecutionHypothesis(latency_hours=1)).params_digest()
        assert base == same
        assert base != other


class TestCanonicalDigest:
    def test_row_order_does_not_change_the_digest(self):
        frame = pd.DataFrame({"a": [1.5, 2.5, 3.5], "t": pd.date_range("2024-01-01", periods=3, tz="UTC")})
        shuffled = frame.iloc[::-1].reset_index(drop=True)
        assert canonical_frame_digest(frame, ["a", "t"]) == canonical_frame_digest(shuffled, ["a", "t"])

    def test_content_change_changes_the_digest(self):
        frame = pd.DataFrame({"a": [1.5]})
        changed = pd.DataFrame({"a": [1.5000000001]})
        assert canonical_frame_digest(frame, ["a"]) != canonical_frame_digest(changed, ["a"])
