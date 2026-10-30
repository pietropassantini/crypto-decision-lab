"""S02-CP08-04: independent validation protocol, research freeze and reuse guards.

What this module is
-------------------
A **protocol engine**, not a new backtest.  It freezes:

* the CP07 / CP08-01 / CP08-02 / CP08-03 benchmark and every artifact hash;
* the registry of historical data already used for research (and the observed
  but unused snapshot tail, which is still *not* independent);
* the immutable reference policy (Momentum Threshold 5%, 24h hold, 100% equity);
* the realistic primary execution scenario (latency 1h, 0.20% per side,
  SHORT carry hypothesis);
* the pre-registered threshold grid and the train-only selection rule;
* the future walk-forward structure;
* the GO / NO-GO / INCONCLUSIVE criteria;
* the versioned configuration shell of the future Momentum Confirmation Engine;
* automatic controls that make it impossible to reuse the research period (or
  the observed tail) as independent evidence, or to evaluate the same protocol
  version twice.

What this module deliberately does **not** do
---------------------------------------------
* It does not train anything, run Timestral, download data, or trade.
* It does not run a new optimised backtest on the observed history.  The
  readiness command only *reads* frozen artifacts and verifies their hashes and
  headline numbers.
* It does not declare an independent validation complete: no independent data
  exists yet, so the verdict is ``NOT_EVALUATED`` and the strategic gate stays
  ``NO-GO``.

The evaluator (``evaluate_independent``) is implemented and unit-tested on
synthetic data, so the protocol is executable and verifiable *before* the data
arrive.  It refuses to run on research windows, on the observed tail, on an
unregistered dataset, or a second time for the same protocol version.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import operator
from pathlib import Path
import shutil
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from .decision_contract import ACTION_NO_TRADE
from .parameter_robustness import (
    PREREGISTERED_THRESHOLD_GRID,
    evaluate_frame,
    grid_frame,
    select_threshold,
)
from .simulator_integrity_audit import _json_safe
from .sync import digest

PROTOCOL_SCHEMA = "cdl.cp08.independent_validation.protocol.v1"
ENGINE_SCHEMA = "cdl.cp08.confirmation_engine.spec.v1"
VALIDATION_SCHEMA = "cdl.cp08.independent_validation.v1"

STATUS_FROZEN = "FROZEN"
VERDICT_GO = "GO"
VERDICT_NO_GO = "NO-GO"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"
VERDICT_INTERIM = "INTERIM_DESCRIPTIVE"
VERDICT_NOT_EVALUATED = "NOT_EVALUATED"

STATUS_PASS = "PASS"
STATUS_WARN = "WARN"
STATUS_FAIL = "FAIL"

DEFAULT_CONFIG_PATH = Path("config/protocols/momentum-independent-validation-v1.yaml")
DEFAULT_ENGINE_CONFIG_PATH = Path("config/protocols/momentum-confirmation-engine-v1.yaml")
DEFAULT_OUTPUT_DIR = Path("data/experiments/S02-CP08-independent-validation-protocol-v1")
DEFAULT_REGISTRY_DIR = Path("data/validation_registry")

OPERATORS: dict[str, Any] = {
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
    "==": operator.eq,
    "!=": operator.ne,
}

# Metrics a criterion may reference.  Keeping the set closed prevents a typo in
# the YAML from silently disabling a gate.
SUPPORTED_METRICS = {
    "total_return_pct",
    "ci_return_low_pct",
    "ci_return_high_pct",
    "profit_factor_usdt",
    "max_drawdown_mtm_pct",
    "max_drawdown_exit_pct",
    "positive_folds",
    "max_abs_fold_share_pct",
    "return_after_removing_top5_pct",
    "selected_thresholds_in_grid",
    "protocol_violations",
    "trades",
    "folds",
    "independent_days",
}

# Cutoff-only features documented by CP06B.  The confirmation engine may only
# read these (no target, no label, no future price).
KNOWN_CUTOFF_FEATURES = {
    "close_at_cutoff_bar",
    "sma20",
    "sma50",
    "ema20",
    "ema50",
    "rsi14",
    "atr14",
    "atr_pct",
    "volatility_24h",
    "volatility_168h",
    "momentum_24h",
    "momentum_168h",
    "dist_sma50_pct",
    "trend_strength_pct",
    "volume_ratio",
}


class ProtocolViolation(RuntimeError):
    """Raised when the protocol, the data boundary or the reuse rules are violated."""


@dataclass(frozen=True)
class ValidationPaths:
    repo_root: Path = Path(".")
    config_path: Path = DEFAULT_CONFIG_PATH
    engine_config_path: Path = DEFAULT_ENGINE_CONFIG_PATH
    registry_dir: Path = DEFAULT_REGISTRY_DIR
    output_dir: Path = DEFAULT_OUTPUT_DIR

    def resolve(self, path: str | Path) -> Path:
        candidate = Path(path)
        return candidate if candidate.is_absolute() else self.repo_root / candidate

    @property
    def config_file(self) -> Path:
        return self.resolve(self.config_path)

    @property
    def engine_config_file(self) -> Path:
        return self.resolve(self.engine_config_path)

    @property
    def registry_root(self) -> Path:
        return self.resolve(self.registry_dir)

    @property
    def output(self) -> Path:
        return self.resolve(self.output_dir)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(value: Any) -> str:
    return _sha256_bytes(canonical_json(value).encode("utf-8"))


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - environment guard
        raise ProtocolViolation(
            "PyYAML is required to read the protocol configuration (dashboard extra)"
        ) from exc
    if not path.is_file():
        raise ProtocolViolation(f"Protocol configuration not found: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ProtocolViolation(f"Protocol configuration root must be a mapping: {path}")
    return payload


def load_protocol(paths: ValidationPaths = ValidationPaths()) -> dict[str, Any]:
    """Load the protocol and the related confirmation-engine configuration."""

    config_file = paths.config_file
    raw = config_file.read_bytes()
    protocol = load_yaml(config_file)
    engine_file = paths.engine_config_file
    engine_raw = engine_file.read_bytes()
    engine = load_yaml(engine_file)
    return {
        "config": protocol,
        "config_path": str(paths.config_path),
        "config_sha256": _sha256_bytes(raw),
        "config_bytes": raw,
        "canonical_sha256": canonical_hash(protocol),
        "engine_config": engine,
        "engine_config_path": str(paths.engine_config_path),
        "engine_config_sha256": _sha256_bytes(engine_raw),
        "engine_config_bytes": engine_raw,
        "engine_canonical_sha256": canonical_hash(engine),
    }


def _require_keys(mapping: Mapping[str, Any], keys: Iterable[str], label: str) -> list[str]:
    return [f"{label}: missing key '{key}'" for key in keys if key not in mapping]


def _compare(observed: Any, op: str, expected: Any) -> bool:
    if observed is None:
        return False
    try:
        number = float(observed)
    except (TypeError, ValueError):
        return False
    if not np.isfinite(number):
        return False
    if op not in OPERATORS:
        raise ProtocolViolation(f"Unsupported operator: {op}")
    return bool(OPERATORS[op](number, float(expected)))


# ---------------------------------------------------------------------------
# structural protocol validation
# ---------------------------------------------------------------------------


def validate_protocol(protocol: Mapping[str, Any]) -> None:
    """Fail closed if the protocol is incomplete, inconsistent or mutable."""

    errors: list[str] = []
    errors += _require_keys(
        protocol,
        (
            "schema_version",
            "protocol_id",
            "protocol_version",
            "status",
            "research_phase",
            "frozen_artifacts",
            "frozen_benchmark",
            "primary_scenario",
            "threshold_preregistration",
            "walk_forward",
            "independent_validation",
            "go_nogo",
            "immutability",
            "anti_reuse_controls",
            "confirmation_engine",
        ),
        "protocol",
    )
    if errors:
        raise ProtocolViolation("; ".join(errors))
    if protocol["schema_version"] != PROTOCOL_SCHEMA:
        errors.append(f"schema_version must be {PROTOCOL_SCHEMA}")
    if protocol["status"] != STATUS_FROZEN:
        errors.append("protocol status must be FROZEN")
    if not str(protocol["protocol_version"]).strip():
        errors.append("protocol_version must be explicit")

    research = protocol["research_phase"]
    independent = protocol["independent_validation"]
    boundary = (
        pd.Timestamp(research["last_used_target_end_utc"]),
        pd.Timestamp(research["period_end_exclusive_utc"]),
        pd.Timestamp(independent["start_utc"]),
    )
    if not boundary[0] < boundary[1]:
        errors.append("research last_used_target_end must precede the snapshot end")
    if not boundary[1] <= boundary[2]:
        errors.append("independent start must be at or after the frozen snapshot end")
    tail = research.get("observed_unused_tail", {})
    if not tail:
        errors.append("research_phase.observed_unused_tail must be declared")
    else:
        if bool(tail.get("usable_as_independent_evidence")):
            errors.append("observed_unused_tail cannot be usable as independent evidence")
        if pd.Timestamp(tail["start_utc"]) < boundary[0]:
            errors.append("observed_unused_tail cannot start before the last used target")
        if pd.Timestamp(tail["end_exclusive_utc"]) > boundary[2]:
            errors.append("observed_unused_tail cannot extend into the independent window")
    if int(research["windows"]) <= 0 or int(research["oos_windows"]) <= 0:
        errors.append("research windows must be positive")
    if int(research["oos_windows"]) > int(research["windows"]):
        errors.append("oos_windows cannot exceed windows")

    benchmark = protocol["frozen_benchmark"]
    if float(benchmark["threshold"]) != 0.05:
        errors.append("frozen threshold must be 0.05")
    costs = benchmark["reference_costs"]
    if not (float(costs["fee_rate"]) == 0.001 and float(costs["slippage"]) == 0.0005):
        errors.append("frozen reference costs must be fee=0.001 and slippage=0.0005")
    if float(benchmark["initial_capital_usdt"]) != 10000.0:
        errors.append("frozen initial capital must be 10000 USDT")
    if int(benchmark["horizon_hours"]) != 24:
        errors.append("frozen horizon must be 24h")

    # Threshold grid must be exactly the CP08-03 pre-registration.
    grid = protocol["threshold_preregistration"]
    declared = [float(value) for value in grid["grid"]]
    if declared != [float(value) for value in PREREGISTERED_THRESHOLD_GRID]:
        errors.append("threshold grid differs from the CP08-03 pre-registration")
    if len(set(declared)) != len(declared):
        errors.append("threshold grid contains duplicates")
    if declared != sorted(declared):
        errors.append("threshold grid must be sorted")
    for required in (0.05, 0.08, 0.10):
        if not any(abs(value - required) < 1e-12 for value in declared):
            errors.append(f"threshold grid must contain {required}")
    if float(grid["frozen_reference_threshold"]) != 0.05:
        errors.append("frozen reference threshold must be 0.05")
    if grid["selection_rule"] != "argmax_train_compounded_net_multiple":
        errors.append("selection rule must be argmax_train_compounded_net_multiple")
    if grid["selection_split"] != "train_only":
        errors.append("selection split must be train_only")
    if not bool(grid.get("grid_is_closed")):
        errors.append("grid must be closed after freeze")
    if grid.get("widening_after_independent_data") != "forbidden":
        errors.append("grid widening after independent data must be forbidden")

    primary = protocol["primary_scenario"]
    if int(primary["latency_hours"]) != 1:
        errors.append("primary latency must be 1h")
    if float(primary["fee_rate"]) <= 0.0 or float(primary["slippage"]) <= 0.0:
        errors.append("primary fee and slippage must be explicit and positive")
    if abs(float(primary["fee_rate"]) + float(primary["slippage"]) - 0.002) > 1e-12:
        errors.append("primary total cost per side must be 0.20%")
    carry = primary["short_carry"]
    if carry.get("status") != "HYPOTHESIS_NOT_HISTORICAL":
        errors.append("SHORT carry must be declared as a hypothesis")
    if carry.get("applied_to") != "SHORT_ONLY":
        errors.append("SHORT carry must be applied to SHORT only")
    expected_carry_pct = 100.0 * (
        float(carry["funding_pct_8h"]) * 3.0 + float(carry["borrow_pct_day"])
    )
    if abs(expected_carry_pct - float(carry["carry_pct_per_trade"])) > 1e-9:
        errors.append("carry_pct_per_trade does not match funding/borrow formula")

    walk = protocol["walk_forward"]
    if int(walk["purge_hours"]) != 24:
        errors.append("purge must equal the 24h holding period")
    if int(walk["test_windows_per_fold"]) <= 0 or int(walk["min_train_windows"]) <= 0:
        errors.append("walk-forward train/test sizes must be positive")
    if int(walk["folds_min_for_conclusive"]) < 1:
        errors.append("folds_min_for_conclusive must be at least 1")
    if walk.get("type") != "expanding":
        errors.append("walk-forward type must be expanding")
    if bool(walk.get("interim_looks", {}).get("gating")):
        errors.append("interim looks cannot be gating")

    if not independent.get("forbidden_sources"):
        errors.append("forbidden_sources must be declared")
    max_independent_windows = int(walk["test_windows_per_fold"]) * int(
        walk["folds_min_for_conclusive"]
    )
    if int(independent.get("min_oos_windows", 0)) > max_independent_windows:
        errors.append(
            "min_oos_windows cannot exceed folds_min x test_windows_per_fold "
            f"({max_independent_windows})"
        )

    criteria = protocol["go_nogo"]
    sample_requirements = criteria.get("sample_requirements", {})
    for requirement in ("min_trades", "min_folds", "min_independent_days"):
        if int(sample_requirements.get(requirement, 0)) <= 0:
            errors.append(f"go_nogo.sample_requirements.{requirement} must be positive")
    if not criteria.get("go_criteria") or not criteria.get("no_go_criteria"):
        errors.append("go_criteria and no_go_criteria must both be non-empty")
    seen_ids: set[str] = set()
    for group in ("go_criteria", "no_go_criteria"):
        for item in criteria[group]:
            for key in ("id", "metric", "operator", "value"):
                if key not in item:
                    errors.append(f"{group} criterion missing '{key}'")
                    continue
            if item.get("metric") not in SUPPORTED_METRICS:
                errors.append(f"{group} criterion uses unsupported metric: {item.get('metric')}")
            if item.get("operator") not in OPERATORS:
                errors.append(f"{group} criterion uses unsupported operator: {item.get('operator')}")
            if item["id"] in seen_ids:
                errors.append(f"duplicate criterion id: {item['id']}")
            seen_ids.add(str(item["id"]))
    if errors:
        raise ProtocolViolation("; ".join(errors))

    _validate_criteria_consistency(criteria)


def _validate_criteria_consistency(criteria: Mapping[str, Any]) -> None:
    """No value may satisfy a GO criterion and a NO-GO criterion at once."""

    go_by_metric = {item["metric"]: item for item in criteria["go_criteria"]}
    no_go_by_metric = {item["metric"]: item for item in criteria["no_go_criteria"]}
    sentinels = [-1e9, -100.0, -40.0, -30.0, -1.0, 0.0, 1.0, 1.1, 2.0, 3.0, 60.0, 1e9]
    for metric in set(go_by_metric) & set(no_go_by_metric):
        go = go_by_metric[metric]
        no_go = no_go_by_metric[metric]
        candidates = sentinels + [float(go["value"]), float(no_go["value"])]
        for value in candidates:
            if _compare(value, str(go["operator"]), go["value"]) and _compare(
                value, str(no_go["operator"]), no_go["value"]
            ):
                raise ProtocolViolation(
                    f"Criteria contradiction on {metric}: value {value} satisfies both "
                    f"GO '{go['id']}' and NO-GO '{no_go['id']}'"
                )


def validate_confirmation_engine(engine: Mapping[str, Any]) -> None:
    """The future engine must be a shell: no active rule, no forbidden input."""

    errors: list[str] = []
    errors += _require_keys(
        engine,
        ("schema_version", "engine_id", "engine_version", "status", "base_strategy", "interface", "rules", "evaluation", "versioning"),
        "confirmation_engine",
    )
    if errors:
        raise ProtocolViolation("; ".join(errors))
    if engine["schema_version"] != ENGINE_SCHEMA:
        errors.append(f"confirmation engine schema must be {ENGINE_SCHEMA}")
    if engine["status"] != "SPECIFICATION_ONLY":
        errors.append("confirmation engine status must be SPECIFICATION_ONLY")
    for flag in ("runnable", "authorises_backtest", "authorises_training", "authorises_trading"):
        if bool(engine.get(flag)):
            errors.append(f"confirmation engine must not set {flag}=true")
    rules = engine["rules"]
    if rules.get("active"):
        errors.append("confirmation engine must have no active rule before pre-registration")
    interface = engine["interface"]
    allowed = set(interface.get("allowed_input_features", []))
    forbidden = set(interface.get("forbidden_inputs", []))
    if not allowed:
        errors.append("confirmation engine must declare allowed cutoff features")
    unknown = allowed - KNOWN_CUTOFF_FEATURES
    if unknown:
        errors.append(f"unknown allowed input features: {sorted(unknown)}")
    if allowed & forbidden:
        errors.append("allowed and forbidden inputs overlap")
    if not forbidden:
        errors.append("confirmation engine must declare forbidden inputs")
    if not bool(interface.get("inputs_causal")):
        errors.append("confirmation engine inputs must be causal")
    if not bool(interface.get("deterministic")):
        errors.append("confirmation engine must be deterministic")
    if interface.get("decision_contract", {}).get("can_only_downgrade") is not True:
        errors.append("confirmation engine can only downgrade the base decision")
    if errors:
        raise ProtocolViolation("; ".join(errors))


# ---------------------------------------------------------------------------
# read-only verification of the frozen research artifacts
# ---------------------------------------------------------------------------


def check_frozen_artifacts(
    paths: ValidationPaths, protocol: Mapping[str, Any]
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in protocol["frozen_artifacts"]:
        path = paths.resolve(item["path"])
        exists = path.is_file()
        actual = digest(path) if exists else None
        rows.append(
            {
                "id": item["id"],
                "path": str(item["path"]),
                "role": item.get("role"),
                "exists": bool(exists),
                "expected_sha256": item["sha256"],
                "actual_sha256": actual,
                "match": bool(exists and actual == item["sha256"]),
            }
        )
    return rows


def _frozen_scenarios(paths: ValidationPaths, protocol: Mapping[str, Any]) -> pd.DataFrame:
    artifact = next(
        item for item in protocol["frozen_artifacts"] if item["id"] == "cp08_03_scenario_metrics"
    )
    path = paths.resolve(artifact["path"])
    if not path.is_file():
        raise ProtocolViolation(f"CP08-03 scenario metrics not found: {path}")
    return pd.read_parquet(path)


def check_frozen_benchmark(
    paths: ValidationPaths, protocol: Mapping[str, Any]
) -> dict[str, Any]:
    """Reconcile the frozen headline numbers with the CP08-03 archive."""

    scenarios = _frozen_scenarios(paths, protocol)
    benchmark = protocol["frozen_benchmark"]
    expected = benchmark["expected"]
    base = scenarios[scenarios["scenario"] == "cost_0015_base"].iloc[0]
    comparisons = []

    def compare(label: str, observed: float, declared: float, tolerance: float = 1e-6) -> None:
        delta = float(observed) - float(declared)
        comparisons.append(
            {
                "metric": label,
                "observed": float(observed),
                "declared": float(declared),
                "delta": delta,
                "match": bool(abs(delta) <= tolerance),
            }
        )

    compare("trades", base["trades"], expected["trades"], 0.0)
    for key in (
        "total_return_pct",
        "profit_factor_usdt",
        "max_drawdown_exit_pct",
        "max_drawdown_mtm_pct",
        "ci_return_low_pct",
        "ci_return_high_pct",
        "ci_return_p_positive",
    ):
        compare(key, base[key], expected[key], 1e-6)

    reference = benchmark["cp08_03_reference"]
    for block_name in ("latency_1h", "conservative_envelope"):
        block = reference[block_name]
        row = scenarios[scenarios["scenario"] == block["scenario"]].iloc[0]
        compare(f"{block_name}.total_return_pct", row["total_return_pct"], block["total_return_pct"])
        compare(
            f"{block_name}.profit_factor_usdt",
            row["profit_factor_usdt"],
            block["profit_factor_usdt"],
        )
        compare(
            f"{block_name}.max_drawdown_mtm_pct",
            row["max_drawdown_mtm_pct"],
            block["max_drawdown_mtm_pct"],
        )
        compare(f"{block_name}.ci_return_low_pct", row["ci_return_low_pct"], block["ci_return_p_low"])
        compare(
            f"{block_name}.ci_return_high_pct", row["ci_return_high_pct"], block["ci_return_p_high"]
        )
        if "ci_return_p_positive" in block:
            compare(
                f"{block_name}.ci_return_p_positive",
                row["ci_return_p_positive"],
                block["ci_return_p_positive"],
            )

    manifest = json.loads(
        (paths.resolve(next(item["path"] for item in protocol["frozen_artifacts"] if item["id"] == "cp08_03_manifest"))).read_text(encoding="utf-8")
    )
    return {
        "comparisons": comparisons,
        "all_match": all(row["match"] for row in comparisons),
        "cp08_03_technical_gate": manifest.get("technical_gate"),
        "cp08_03_checks_failed": manifest.get("checks_failed", []),
        "cp08_03_gate_ok": bool(
            manifest.get("technical_gate") == "PASS" and not manifest.get("checks_failed")
        ),
    }


def build_data_registry(
    paths: ValidationPaths, protocol: Mapping[str, Any]
) -> dict[str, Any]:
    """Rebuild the data registry from the frozen files and compare it to the protocol."""

    research = protocol["research_phase"]
    artifacts = {item["id"]: item for item in protocol["frozen_artifacts"]}
    labels_path = paths.resolve(artifacts["cp07_labels"]["path"])
    features_path = paths.resolve(artifacts["cp06b_features"]["path"])
    decisions_path = paths.resolve(artifacts["cp07_decisions"]["path"])
    snapshot_path = paths.resolve(artifacts["snapshot_raw_manifest"]["path"])
    labels = pd.read_parquet(labels_path)
    features = pd.read_parquet(features_path)
    decisions = pd.read_parquet(decisions_path)
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    for column in ("cutoff", "entry_timestamp", "exit_timestamp"):
        labels[column] = pd.to_datetime(labels[column], utc=True)
        if column in decisions.columns:
            decisions[column] = pd.to_datetime(decisions[column], utc=True)

    momentum = decisions[decisions["policy"] == "momentum_threshold"]
    observed = {
        "labels_rows": int(len(labels)),
        "labels_first_cutoff_utc": labels["cutoff"].min().isoformat(),
        "labels_last_cutoff_utc": labels["cutoff"].max().isoformat(),
        "labels_last_exit_utc": labels["exit_timestamp"].max().isoformat(),
        "features_rows": int(len(features)),
        "features_last_feature_bar_utc": pd.to_datetime(features["feature_bar"], utc=True).max().isoformat(),
        "decisions_rows": int(len(decisions)),
        "decision_policies": int(decisions["policy"].nunique()),
        "oos_windows": int(momentum["window_index"].nunique()),
        "oos_first_cutoff_utc": momentum["cutoff"].min().isoformat(),
        "oos_last_cutoff_utc": momentum["cutoff"].max().isoformat(),
        "oos_last_exit_utc": momentum["exit_timestamp"].max().isoformat(),
        "folds": int(momentum["fold"].nunique()),
        "snapshot_start_utc": snapshot["start_utc"],
        "snapshot_end_exclusive_utc": snapshot["end_exclusive_utc"],
        "snapshot_observed_rows": int(snapshot["observed_rows"]),
        "snapshot_expected_rows": int(snapshot["expected_rows"]),
        "snapshot_missing_hours": int(snapshot["missing_count"]),
    }
    declared = {
        "labels_rows": int(research["windows"]),
        "labels_first_cutoff_utc": research["first_label_cutoff_utc"],
        "labels_last_cutoff_utc": research["last_label_cutoff_utc"],
        "labels_last_exit_utc": research["last_used_target_end_utc"],
        "features_rows": int(research["windows"]),
        "oos_windows": int(research["oos_windows"]),
        "oos_first_cutoff_utc": research["oos_period_start_utc"],
        "oos_last_cutoff_utc": research["oos_period_end_utc"],
        "folds": int(research["folds"]),
        "snapshot_end_exclusive_utc": research["period_end_exclusive_utc"],
    }
    comparisons = []
    for key, declared_value in declared.items():
        observed_value = observed[key]
        if key.endswith("_utc"):
            match = pd.Timestamp(observed_value) == pd.Timestamp(declared_value)
        else:
            match = int(observed_value) == int(declared_value)
        comparisons.append(
            {
                "field": key,
                "observed": observed_value,
                "declared": declared_value,
                "match": bool(match),
            }
        )
    tail = research["observed_unused_tail"]
    datasets = [
        {
            "id": "btcusdt_1h_snapshot_v1",
            "role": "research_used",
            "path": artifacts["snapshot_raw_manifest"]["path"],
            "sha256": artifacts["snapshot_raw_manifest"]["sha256"],
            "coverage": {
                "start_utc": observed["snapshot_start_utc"],
                "end_exclusive_utc": observed["snapshot_end_exclusive_utc"],
            },
            "rows_observed": observed["snapshot_observed_rows"],
        },
        {
            "id": "research_unused_tail",
            "role": tail["status"],
            "coverage": {
                "start_utc": tail["start_utc"],
                "end_exclusive_utc": tail["end_exclusive_utc"],
            },
            "usable_as_independent_evidence": False,
            "note": tail["reason"],
        },
        {
            "id": "independent_window",
            "role": "independent_untouched",
            "coverage": {
                "start_utc": protocol["independent_validation"]["start_utc"],
                "end_exclusive_utc": None,
            },
            "available_in_repository": False,
            "note": "No independent data exists in the repository: the protocol awaits genuinely new data.",
        },
    ]
    return {
        "research_period": {
            "start_utc": research["period_start_utc"],
            "end_exclusive_utc": research["period_end_exclusive_utc"],
            "last_used_target_end_utc": research["last_used_target_end_utc"],
            "oos_period": {
                "start_utc": research["oos_period_start_utc"],
                "end_utc": research["oos_period_end_utc"],
            },
        },
        "observed": observed,
        "declared": declared,
        "comparisons": comparisons,
        "all_match": all(row["match"] for row in comparisons),
        "datasets": datasets,
    }


def check_data_separation(
    registry: Mapping[str, Any], protocol: Mapping[str, Any]
) -> dict[str, Any]:
    research = protocol["research_phase"]
    independent = protocol["independent_validation"]
    tail = research["observed_unused_tail"]
    last_used = pd.Timestamp(research["last_used_target_end_utc"])
    tail_start = pd.Timestamp(tail["start_utc"])
    tail_end = pd.Timestamp(tail["end_exclusive_utc"])
    independent_start = pd.Timestamp(independent["start_utc"])
    observed_last_exit = pd.Timestamp(registry["observed"]["labels_last_exit_utc"])
    return {
        "last_used_target_end_utc": last_used.isoformat(),
        "observed_unused_tail": [tail_start.isoformat(), tail_end.isoformat()],
        "independent_start_utc": independent_start.isoformat(),
        "research_target_before_tail": bool(observed_last_exit <= tail_start),
        "tail_before_independent_start": bool(tail_end <= independent_start),
        "independent_after_research": bool(independent_start > last_used),
        "no_overlap": bool(observed_last_exit < independent_start),
        "tail_not_independent": bool(not tail.get("usable_as_independent_evidence")),
        "all_ok": bool(
            observed_last_exit <= tail_start
            and tail_end <= independent_start
            and independent_start > last_used
            and not tail.get("usable_as_independent_evidence")
        ),
    }


# ---------------------------------------------------------------------------
# guards for independent windows and folds
# ---------------------------------------------------------------------------


def _window_frame(windows: pd.DataFrame) -> pd.DataFrame:
    required = {"cutoff", "entry_timestamp", "exit_timestamp"}
    missing = required - set(windows.columns)
    if missing:
        raise ProtocolViolation(f"Window frame missing columns: {sorted(missing)}")
    frame = windows.copy()
    for column in required:
        frame[column] = pd.to_datetime(frame[column], utc=True)
    return frame.sort_values("cutoff").reset_index(drop=True)


def validate_independent_windows(
    windows: pd.DataFrame, protocol: Mapping[str, Any]
) -> dict[str, Any]:
    """Refuse any window that touches the research period or the observed tail."""

    frame = _window_frame(windows)
    if frame.empty:
        raise ProtocolViolation("Independent window frame is empty")
    independent_start = pd.Timestamp(protocol["independent_validation"]["start_utc"])
    last_used = pd.Timestamp(protocol["research_phase"]["last_used_target_end_utc"])
    tail_end = pd.Timestamp(protocol["research_phase"]["observed_unused_tail"]["end_exclusive_utc"])
    if (frame["cutoff"] < independent_start).any():
        first = frame.loc[frame["cutoff"] < independent_start, "cutoff"].min()
        raise ProtocolViolation(
            f"Window cutoff {first} precedes the independent start {independent_start}"
        )
    if (frame["exit_timestamp"] <= last_used).any():
        first = frame.loc[frame["exit_timestamp"] <= last_used, "exit_timestamp"].max()
        raise ProtocolViolation(
            f"Window target ends at {first}, inside the research period (last used {last_used})"
        )
    if (frame["exit_timestamp"] <= tail_end).any():
        raise ProtocolViolation(
            "Window target ends inside/at the observed but unused tail: it is not independent"
        )
    horizon = frame["exit_timestamp"] - frame["entry_timestamp"]
    if not (horizon == pd.Timedelta(hours=24)).all():
        raise ProtocolViolation("Every independent window must span exactly 24h")
    if (frame["entry_timestamp"] < frame["cutoff"]).any():
        raise ProtocolViolation("Entry timestamp precedes the decision cutoff")
    if frame["cutoff"].duplicated().any():
        raise ProtocolViolation("Duplicate cutoffs in the independent window frame")
    if "window_index" in frame.columns and frame["window_index"].duplicated().any():
        raise ProtocolViolation("Duplicate window_index in the independent window frame")
    return {
        "windows": int(len(frame)),
        "first_cutoff_utc": frame["cutoff"].min().isoformat(),
        "last_cutoff_utc": frame["cutoff"].max().isoformat(),
        "first_entry_utc": frame["entry_timestamp"].min().isoformat(),
        "last_exit_utc": frame["exit_timestamp"].max().isoformat(),
        "independent_start_utc": independent_start.isoformat(),
        "research_last_used_target_end_utc": last_used.isoformat(),
        "boundary_ok": True,
    }


def build_independent_folds(
    history: pd.DataFrame,
    future: pd.DataFrame,
    protocol: Mapping[str, Any],
) -> dict[str, Any]:
    """Expanding walk-forward folds whose test blocks are all independent.

    ``history`` is the frozen research window frame (used only as training
    context).  ``future`` is the registered independent window frame.  Test
    blocks start exactly at the first future window; each fold trains on every
    prior window with a 24h purge, so no training target can overlap a test
    target.
    """

    walk = protocol["walk_forward"]
    future_frame = validate_independent_windows(future, protocol)
    history_frame = _window_frame(history)
    if history_frame.empty:
        raise ProtocolViolation("Training history is empty")
    last_used = pd.Timestamp(protocol["research_phase"]["last_used_target_end_utc"])
    if (history_frame["exit_timestamp"] > last_used).any():
        raise ProtocolViolation("Training history contains windows beyond the frozen research period")
    if int(len(history_frame)) < int(walk["min_train_windows"]):
        raise ProtocolViolation(
            f"Training history has {len(history_frame)} windows, "
            f"minimum is {walk['min_train_windows']}"
        )
    combined = pd.concat(
        [
            history_frame.assign(segment="history"),
            _window_frame(future).assign(segment="future"),
        ],
        ignore_index=True,
    ).sort_values("cutoff").reset_index(drop=True)
    future_mask = combined["segment"] == "future"
    first_test_index = int(np.argmax(future_mask.to_numpy()))
    if not future_mask.iloc[first_test_index]:
        raise ProtocolViolation("No independent windows in the combined frame")
    test_size = int(walk["test_windows_per_fold"])
    max_folds = int(walk["folds_max"])
    available = len(combined) - first_test_index
    n_folds = min(max_folds, available // test_size)
    if n_folds < 1:
        raise ProtocolViolation(
            f"Independent windows ({available}) are fewer than one test block ({test_size})"
        )
    folds: list[dict[str, Any]] = []
    for fold in range(n_folds):
        start = first_test_index + fold * test_size
        stop = start + test_size
        test_block = combined.iloc[start:stop]
        train_block = combined.iloc[:start]
        first_test_cutoff = test_block["cutoff"].iloc[0]
        target_end = train_block["cutoff"] + pd.Timedelta(hours=int(walk["purge_hours"]))
        keep = target_end <= first_test_cutoff
        if test_block["segment"].ne("future").any():
            raise ProtocolViolation("A test block contains research windows")
        folds.append(
            {
                "fold": int(fold),
                "train_index": train_block.index[keep].tolist(),
                "test_index": test_block.index.tolist(),
                "train_size": int(keep.sum()),
                "test_size": int(len(test_block)),
                "purged_windows": int((~keep).sum()),
                "first_test_cutoff_utc": first_test_cutoff.isoformat(),
                "last_train_cutoff_utc": train_block.loc[keep, "cutoff"].iloc[-1].isoformat()
                if keep.any()
                else None,
                "all_test_windows_independent": True,
            }
        )
    leftover = available - n_folds * test_size
    if leftover < 0:  # pragma: no cover - defensive
        raise ProtocolViolation("Fold construction consumed more windows than available")
    return {
        "folds": folds,
        "n_folds": n_folds,
        "test_size": test_size,
        "leftover_windows": int(leftover),
        "combined_rows": int(len(combined)),
        "history_rows": int(len(history_frame)),
        "future_rows": int(len(_window_frame(future))),
        "first_test_index": first_test_index,
        "future_window": future_frame,
        "purge_hours": int(walk["purge_hours"]),
        "expanding": True,
        "all_tests_independent": True,
    }


def frame_digest(frame: pd.DataFrame, *, columns: Sequence[str] | None = None) -> str:
    """Deterministic SHA-256 of a frame (column-ordered canonical JSON)."""

    selected = list(columns or frame.columns)
    payload = frame[selected].copy()
    for column in payload.columns:
        if pd.api.types.is_datetime64_any_dtype(payload[column]):
            payload[column] = payload[column].astype(str)
    records = payload.to_dict(orient="records")
    return canonical_hash(records)


# ---------------------------------------------------------------------------
# registry: dataset pinning + one final evaluation per protocol version
# ---------------------------------------------------------------------------


class ValidationRegistry:
    """Append-only JSONL registry (datasets + evaluations)."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.datasets_path = self.root / "datasets.jsonl"
        self.evaluations_path = self.root / "evaluations.jsonl"

    def _read(self, path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        rows: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return rows

    def _append(self, path: Path, record: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(canonical_json(_json_safe(dict(record))) + "\n")

    def datasets(self) -> list[dict[str, Any]]:
        return self._read(self.datasets_path)

    def evaluations(self) -> list[dict[str, Any]]:
        return self._read(self.evaluations_path)

    def register_dataset(self, record: Mapping[str, Any]) -> dict[str, Any]:
        required = ("dataset_id", "sha256", "role", "registered_utc")
        missing = [key for key in required if key not in record]
        if missing:
            raise ProtocolViolation(f"Dataset record missing keys: {missing}")
        if record["role"] != "independent":
            raise ProtocolViolation("Only datasets with role=independent can be registered here")
        existing = {row["dataset_id"]: row for row in self.datasets()}
        if record["dataset_id"] in existing:
            previous = existing[record["dataset_id"]]
            if previous["sha256"] != record["sha256"]:
                raise ProtocolViolation(
                    f"Dataset {record['dataset_id']} already registered with a different hash"
                )
            return previous
        self._append(self.datasets_path, record)
        return dict(record)

    def state(self, protocol_sha256: str) -> dict[str, Any]:
        evaluations = self.evaluations()
        related = [row for row in evaluations if row.get("protocol_sha256") == protocol_sha256]
        finals = [row for row in related if row.get("final")]
        return {
            "datasets": len(self.datasets()),
            "evaluations": len(related),
            "final_evaluations": len(finals),
            "final_consumed": bool(finals),
            "interim_evaluations": len(related) - len(finals),
        }

    def record_evaluation(self, record: Mapping[str, Any]) -> dict[str, Any]:
        required = (
            "protocol_id",
            "protocol_version",
            "protocol_sha256",
            "dataset_id",
            "dataset_sha256",
            "final",
            "verdict",
            "recorded_utc",
            "metrics_sha256",
        )
        missing = [key for key in required if key not in record]
        if missing:
            raise ProtocolViolation(f"Evaluation record missing keys: {missing}")
        datasets = {row["dataset_id"]: row for row in self.datasets()}
        dataset = datasets.get(record["dataset_id"])
        if dataset is None:
            raise ProtocolViolation(
                f"Dataset {record['dataset_id']} is not registered: registration is mandatory"
            )
        if dataset["sha256"] != record["dataset_sha256"]:
            raise ProtocolViolation("Evaluation dataset hash does not match the registered dataset")
        related = [
            row
            for row in self.evaluations()
            if row.get("protocol_sha256") == record["protocol_sha256"]
        ]
        if any(row.get("final") for row in related):
            raise ProtocolViolation(
                "A final evaluation already exists for this protocol version: "
                "reuse of the independent period is forbidden"
            )
        payload = dict(record)
        if not payload["final"]:
            payload["verdict"] = VERDICT_INTERIM
            payload["gating"] = False
        else:
            payload["gating"] = bool(payload.get("verdict") == VERDICT_GO)
        self._append(self.evaluations_path, payload)
        return payload


# ---------------------------------------------------------------------------
# GO / NO-GO decision
# ---------------------------------------------------------------------------


def decide_verdict(
    metrics: Mapping[str, Any],
    protocol: Mapping[str, Any],
    *,
    final: bool = True,
) -> dict[str, Any]:
    """Apply the pre-registered criteria.  Pure function, no IO."""

    criteria = protocol["go_nogo"]
    requirements = criteria["sample_requirements"]

    def evaluate(group: str) -> list[dict[str, Any]]:
        rows = []
        for item in criteria[group]:
            observed = metrics.get(item["metric"])
            passed = _compare(observed, str(item["operator"]), item["value"])
            rows.append(
                {
                    "id": item["id"],
                    "group": group,
                    "metric": item["metric"],
                    "operator": item["operator"],
                    "threshold": float(item["value"]),
                    "observed": None if observed is None else float(observed),
                    "passed": bool(passed),
                    "description": item.get("description", ""),
                }
            )
        return rows

    go_rows = evaluate("go_criteria")
    no_go_rows = evaluate("no_go_criteria")
    sample_checks = {
        "min_trades": {
            "observed": metrics.get("trades"),
            "required": int(requirements["min_trades"]),
            "met": bool(metrics.get("trades") is not None and float(metrics["trades"]) >= requirements["min_trades"]),
        },
        "min_folds": {
            "observed": metrics.get("folds"),
            "required": int(requirements["min_folds"]),
            "met": bool(metrics.get("folds") is not None and float(metrics["folds"]) >= requirements["min_folds"]),
        },
        "min_independent_days": {
            "observed": metrics.get("independent_days"),
            "required": int(requirements["min_independent_days"]),
            "met": bool(
                metrics.get("independent_days") is not None
                and float(metrics["independent_days"]) >= requirements["min_independent_days"]
            ),
        },
    }
    sample_met = all(item["met"] for item in sample_checks.values())
    triggered_no_go = [row for row in no_go_rows if row["passed"]]
    failing_go = [row for row in go_rows if not row["passed"]]
    if not final:
        verdict = VERDICT_INTERIM
        reasons = ["Interim look: descriptive only, it can never produce GO."]
    elif triggered_no_go:
        verdict = VERDICT_NO_GO
        reasons = [f"NO-GO criterion triggered: {row['id']}" for row in triggered_no_go]
    elif not sample_met:
        verdict = VERDICT_INCONCLUSIVE
        reasons = ["Sample requirements not met: " + ", ".join(
            name for name, item in sample_checks.items() if not item["met"]
        )]
    elif not failing_go:
        verdict = VERDICT_GO
        reasons = ["All pre-registered GO criteria passed on the primary scenario."]
    else:
        verdict = VERDICT_INCONCLUSIVE
        reasons = [f"GO criterion not met: {row['id']}" for row in failing_go]
    return {
        "verdict": verdict,
        "final": bool(final),
        "gating": bool(final and verdict == VERDICT_GO),
        "sample_requirements": sample_checks,
        "sample_met": bool(sample_met),
        "go_criteria": go_rows,
        "no_go_criteria": no_go_rows,
        "no_go_triggered": [row["id"] for row in triggered_no_go],
        "go_failed": [row["id"] for row in failing_go],
        "reasons": reasons,
        "metrics": _json_safe(dict(metrics)),
    }


# ---------------------------------------------------------------------------
# independent evaluation harness (runs only on registered, future data)
# ---------------------------------------------------------------------------


def _fold_pnl_shares(
    ledger: pd.DataFrame, *, folds: Sequence[Mapping[str, Any]], initial_capital: float
) -> dict[int, float]:
    executed = ledger[ledger["executed"]]
    if executed.empty:
        return {}
    equity_after = executed["equity_after"].to_numpy(dtype=float)
    equity_before = np.concatenate([[float(initial_capital)], equity_after[:-1]])
    pnl = equity_before * executed["net_return_pct"].to_numpy(dtype=float) / 100.0
    frame = executed.assign(_pnl=pnl)
    totals: dict[int, float] = {}
    for fold in folds:
        subset = frame[
            (frame["fold"] == int(fold["fold"])) & (frame["action"] != ACTION_NO_TRADE)
        ]
        totals[int(fold["fold"])] = float(subset["_pnl"].sum())
    return totals


def validate_evaluation_frame(frame: pd.DataFrame, *, label: str) -> pd.DataFrame:
    """Required columns for threshold selection and execution."""

    required = {
        "window_index",
        "cutoff",
        "entry_timestamp",
        "exit_timestamp",
        "reference_price",
        "entry_open",
        "exit_open",
        "momentum_168h",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ProtocolViolation(f"{label} frame missing columns: {sorted(missing)}")
    frame = _window_frame(frame)
    numeric = ["reference_price", "entry_open", "exit_open", "momentum_168h"]
    if not np.isfinite(frame[numeric].to_numpy(dtype=float)).all():
        raise ProtocolViolation(f"{label} frame contains non-finite prices or signal values")
    if (frame[["entry_open", "exit_open", "reference_price"]] <= 0).any().any():
        raise ProtocolViolation(f"{label} frame contains non-positive prices")
    if frame["window_index"].duplicated().any():
        raise ProtocolViolation(f"{label} frame contains duplicate window_index values")
    return frame


def evaluate_independent(
    future: pd.DataFrame,
    history: pd.DataFrame,
    market: pd.DataFrame,
    protocol: Mapping[str, Any],
    *,
    dataset_id: str,
    registry: ValidationRegistry,
    expected_protocol_sha256: str,
    dataset_sha256: str | None = None,
    final: bool = True,
    initial_capital: float | None = None,
    notes: str = "",
) -> dict[str, Any]:
    """Execute the pre-registered independent evaluation.

    This function is the *only* place that consumes independent data.  It
    refuses to run on research windows, on the observed tail, on an unregistered
    dataset, or after a final evaluation of the same protocol version.  It is
    unit-tested on synthetic data; the readiness command never calls it.
    """

    validate_protocol(protocol)
    protocol_sha256 = canonical_hash(protocol)
    if expected_protocol_sha256 != protocol_sha256:
        raise ProtocolViolation(
            "Protocol hash does not match the frozen hash: evaluation aborted"
        )
    future = validate_evaluation_frame(future, label="future")
    history = validate_evaluation_frame(history, label="history")
    fold_data = build_independent_folds(history, future, protocol)
    primary = protocol["primary_scenario"]
    carry = float(primary["short_carry"]["carry_pct_per_trade"])
    from .decision_contract import CostModel
    from .trading_policy import _threshold_actions

    costs = CostModel(fee_rate=float(primary["fee_rate"]), slippage=float(primary["slippage"]))
    grid = tuple(float(value) for value in protocol["threshold_preregistration"]["grid"])
    combined = pd.concat(
        [
            _window_frame(history).assign(segment="history"),
            _window_frame(future).assign(segment="future"),
        ],
        ignore_index=True,
    ).sort_values("cutoff").reset_index(drop=True)
    capital = float(initial_capital or protocol["frozen_benchmark"]["initial_capital_usdt"])
    frames: list[pd.DataFrame] = []
    fold_rows: list[dict[str, Any]] = []
    selection_rows: list[dict[str, Any]] = []
    for fold in fold_data["folds"]:
        train = combined.loc[fold["train_index"]]
        test = combined.loc[fold["test_index"]]
        state = select_threshold(train, costs=costs, grid=grid)
        actions = _threshold_actions(test["momentum_168h"].to_numpy(dtype=float), state["threshold"])
        frame, diagnostics = grid_frame(
            test,
            actions,
            costs=costs,
            fold=int(fold["fold"]),
            market=market,
            delay_hours=int(primary["latency_hours"]),
            carry_short=carry,
        )
        frames.append(frame)
        selection_rows.append(
            {
                "fold": int(fold["fold"]),
                "selected_threshold": float(state["threshold"]),
                "train_multiple": float(state["train_multiple"]),
                "train_trades": int(state["train_trades"]),
                "train_size": int(fold["train_size"]),
                "test_size": int(fold["test_size"]),
                "purged_windows": int(fold["purged_windows"]),
                "missing_bars": int(diagnostics["missing_bars"]),
            }
        )
        fold_metrics = evaluate_frame(
            frame,
            market=market,
            initial_capital=capital,
            bootstrap=False,
            with_concentration=False,
            with_curves=True,
        )
        fold_metrics.pop("_ledger", None)
        fold_rows.append(
            {
                "fold": int(fold["fold"]),
                "trades": int(fold_metrics["trades"]),
                "compounded_return_pct": float(fold_metrics["total_return_pct"]),
                "profit_factor_usdt": fold_metrics["profit_factor_usdt"],
                "max_drawdown_mtm_pct": fold_metrics["max_drawdown_mtm_pct"],
                "win_rate_trade_pct": float(fold_metrics["win_rate_trade_pct"]),
            }
        )
    aggregate = evaluate_frame(
        pd.concat(frames, ignore_index=True),
        market=market,
        initial_capital=capital,
        bootstrap=True,
        with_concentration=True,
        with_curves=True,
    )
    ledger = aggregate.pop("_ledger")
    fold_pnl = _fold_pnl_shares(ledger, folds=fold_data["folds"], initial_capital=capital)
    total_pnl = float(sum(fold_pnl.values()))
    shares = {
        fold: (100.0 * value / total_pnl if total_pnl != 0 else 0.0)
        for fold, value in fold_pnl.items()
    }
    for row in fold_rows:
        row["chained_pnl_usdt"] = float(fold_pnl.get(row["fold"], 0.0))
        row["share_of_net_pnl_pct"] = float(shares.get(row["fold"], 0.0))
    validation = fold_data["future_window"]
    independent_days = (
        pd.Timestamp(validation["last_exit_utc"]) - pd.Timestamp(validation["first_entry_utc"])
    ).total_seconds() / 86400.0
    concentration = aggregate["concentration"]
    top5 = (concentration.get("top_k") or {}).get("5", {})
    metrics = {
        "trades": int(aggregate["trades"]),
        "folds": int(len(fold_data["folds"])),
        "independent_days": float(independent_days),
        "total_return_pct": float(aggregate["total_return_pct"]),
        "final_equity_usdt": float(aggregate["final_equity_usdt"]),
        "profit_factor_usdt": aggregate["profit_factor_usdt"],
        "max_drawdown_mtm_pct": aggregate["max_drawdown_mtm_pct"],
        "max_drawdown_exit_pct": aggregate["max_drawdown_exit_pct"],
        "ci_return_low_pct": aggregate["ci"]["total_return_pct"]["ci_low"],
        "ci_return_high_pct": aggregate["ci"]["total_return_pct"]["ci_high"],
        "ci_return_p_positive": aggregate["ci"]["total_return_pct"]["p_positive"],
        "positive_folds": int(sum(1 for row in fold_rows if row["compounded_return_pct"] > 0.0)),
        "max_abs_fold_share_pct": float(max((abs(value) for value in shares.values()), default=0.0)),
        "return_after_removing_top5_pct": top5.get("return_pct_after_removal_recompounded"),
        "selected_thresholds_in_grid": int(
            all(
                any(abs(row["selected_threshold"] - candidate) < 1e-12 for candidate in grid)
                for row in selection_rows
            )
        ),
        "protocol_violations": 0,
    }
    verdict = decide_verdict(metrics, protocol, final=final)
    dataset_hash = dataset_sha256 or frame_digest(_window_frame(future))
    report = {
        "protocol_id": protocol["protocol_id"],
        "protocol_version": protocol["protocol_version"],
        "protocol_sha256": protocol_sha256,
        "dataset_id": dataset_id,
        "dataset_sha256": dataset_hash,
        "final": bool(final),
        "primary_scenario": primary["id"],
        "metrics": metrics,
        "verdict": verdict,
        "folds": fold_rows,
        "selection": selection_rows,
        "fold_data": {
            "n_folds": fold_data["n_folds"],
            "test_size": fold_data["test_size"],
            "leftover_windows": fold_data["leftover_windows"],
            "history_rows": fold_data["history_rows"],
            "future_rows": fold_data["future_rows"],
            "purge_hours": fold_data["purge_hours"],
        },
        "concentration": concentration,
        "notes": notes,
    }
    if registry is not None:
        registered = registry.register_dataset(
            {
                "dataset_id": dataset_id,
                "sha256": dataset_hash,
                "role": "independent",
                "registered_utc": _now_iso(),
                "first_cutoff_utc": validation["first_cutoff_utc"],
                "last_exit_utc": validation["last_exit_utc"],
                "windows": validation["windows"],
                "source": "operator_registered",
            }
        )
        if registered["sha256"] != dataset_hash:
            raise ProtocolViolation("Registered dataset hash mismatch")
        registry.record_evaluation(
            {
                "protocol_id": protocol["protocol_id"],
                "protocol_version": protocol["protocol_version"],
                "protocol_sha256": report["protocol_sha256"],
                "dataset_id": dataset_id,
                "dataset_sha256": dataset_hash,
                "final": bool(final),
                "verdict": verdict["verdict"],
                "recorded_utc": _now_iso(),
                "metrics_sha256": canonical_hash(metrics),
                "notes": notes,
            }
        )
        report["registry_state"] = registry.state(report["protocol_sha256"])
    return report


# ---------------------------------------------------------------------------
# readiness report: the only command that runs without independent data
# ---------------------------------------------------------------------------


def _check(
    item: str,
    label: str,
    passed: bool,
    detail: str,
    *,
    status: str | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": item,
        "check": label,
        "status": status or (STATUS_PASS if passed else STATUS_FAIL),
        "detail": detail,
        "evidence": dict(evidence or {}),
    }


def _sidecar_for(config_file: Path, actual_sha256: str) -> dict[str, Any]:
    sidecar = config_file.with_suffix(config_file.suffix + ".sha256")
    if not sidecar.is_file():
        return {
            "path": str(sidecar),
            "sidecar_present": False,
            "match": False,
        }
    declared = sidecar.read_text(encoding="utf-8").strip().split()[0]
    return {
        "path": str(sidecar),
        "sidecar_present": True,
        "declared_sha256": declared,
        "actual_sha256": actual_sha256,
        "match": bool(declared.lower() == actual_sha256.lower()),
    }


def _sidecar_check(paths: ValidationPaths, loaded: Mapping[str, Any]) -> dict[str, Any]:
    result = _sidecar_for(paths.config_file, loaded["config_sha256"])
    result["path"] = f"{paths.config_path}.sha256"
    return result


def build_readiness(paths: ValidationPaths = ValidationPaths()) -> dict[str, Any]:
    """Verify the protocol, freeze and guards.  Never consumes independent data."""

    loaded = load_protocol(paths)
    protocol = loaded["config"]
    engine = loaded["engine_config"]
    validate_protocol(protocol)
    validate_confirmation_engine(engine)

    artifacts = check_frozen_artifacts(paths, protocol)
    benchmark = check_frozen_benchmark(paths, protocol)
    data_registry = build_data_registry(paths, protocol)
    separation = check_data_separation(data_registry, protocol)
    sidecar = _sidecar_check(paths, loaded)
    engine_sidecar = _sidecar_for(paths.engine_config_file, loaded["engine_config_sha256"])
    engine_sidecar["path"] = f"{paths.engine_config_path}.sha256"
    registry = ValidationRegistry(paths.registry_root)
    registry_state = registry.state(loaded["canonical_sha256"])
    grid = [float(value) for value in protocol["threshold_preregistration"]["grid"]]
    criteria = protocol["go_nogo"]
    primary = protocol["primary_scenario"]
    scenarios = _frozen_scenarios(paths, protocol)
    primary_row = scenarios[scenarios["scenario"] == "conservative_1h_cost20_carry"].iloc[0]
    primary_matches = bool(
        float(primary_row["fee_rate"]) == float(primary["fee_rate"])
        and float(primary_row["slippage"]) == float(primary["slippage"])
        and int(primary_row["delay_hours"]) == int(primary["latency_hours"])
        and abs(float(primary_row["carry_short_pct"]) - float(primary["short_carry"]["carry_pct_per_trade"])) < 1e-9
    )

    engine_rules = engine["rules"].get("active", [])
    checks = [
        _check(
            "CP08-04.1",
            "Protocollo caricato, schema valido e stato FROZEN",
            True,
            "Configurazione strutturalmente valida, immutabile e versionata.",
            evidence={
                "protocol_id": protocol["protocol_id"],
                "protocol_version": protocol["protocol_version"],
                "status": protocol["status"],
                "canonical_sha256": loaded["canonical_sha256"],
                "config_sha256": loaded["config_sha256"],
            },
        ),
        _check(
            "CP08-04.2",
            "Sidecar SHA-256 di protocollo e specifica engine",
            bool(sidecar["match"] and engine_sidecar["match"]),
            "I due file YAML corrispondono byte-per-byte agli hash congelati nei sidecar.",
            evidence={"protocol": sidecar, "engine": engine_sidecar},
        ),
        _check(
            "CP08-04.3",
            "Hash di tutti gli artefatti congelati",
            bool(artifacts and all(item["match"] for item in artifacts)),
            "Ogni artefatto CP07/CP08 dichiarato nel protocollo ha l'hash atteso.",
            evidence={
                "total": len(artifacts),
                "matched": sum(1 for item in artifacts if item["match"]),
                "mismatched": [item["id"] for item in artifacts if not item["match"]],
            },
        ),
        _check(
            "CP08-04.4",
            "Benchmark congelato riconciliato con l'archivio CP08-03",
            bool(benchmark["all_match"] and benchmark["cp08_03_gate_ok"]),
            "Rendimento, PF, drawdown, CI e scenario primario coincidono con CP08-03 (gate tecnico PASS).",
            evidence={
                "all_match": benchmark["all_match"],
                "max_abs_delta": max((abs(row["delta"]) for row in benchmark["comparisons"]), default=0.0),
                "cp08_03_technical_gate": benchmark["cp08_03_technical_gate"],
                "cp08_03_checks_failed": benchmark["cp08_03_checks_failed"],
            },
        ),
        _check(
            "CP08-04.5",
            "Registro dei dati di ricerca coerente con gli artefatti",
            bool(data_registry["all_match"]),
            "500 finestre, 300 OOS, 5 fold, estremi temporali e snapshot coincidono con il dichiarato.",
            evidence={
                "mismatched": [
                    row["field"] for row in data_registry["comparisons"] if not row["match"]
                ],
                "observed": data_registry["observed"],
            },
        ),
        _check(
            "CP08-04.6",
            "Separazione temporale ricerca / coda osservata / validazione",
            bool(separation["all_ok"]),
            "La validazione inizia dopo lo snapshot congelato; la coda osservata non è utilizzabile come evidenza indipendente.",
            evidence=separation,
        ),
        _check(
            "CP08-04.7",
            "Griglia soglie identica a CP08-03, chiusa, con selezione train-only",
            bool(
                grid == [float(value) for value in PREREGISTERED_THRESHOLD_GRID]
                and all(any(abs(value - required) < 1e-12 for value in grid) for required in (0.05, 0.08, 0.10))
                and protocol["threshold_preregistration"]["selection_split"] == "train_only"
            ),
            "La griglia non può essere ampliata dopo il congelamento e la selezione avviene solo sul training.",
            evidence={
                "grid": grid,
                "selection_rule": protocol["threshold_preregistration"]["selection_rule"],
                "selection_split": protocol["threshold_preregistration"]["selection_split"],
                "frozen_reference_threshold": protocol["threshold_preregistration"]["frozen_reference_threshold"],
            },
        ),
        _check(
            "CP08-04.8",
            "Scenario primario realistico completo e allineato a CP08-03",
            bool(primary_matches),
            "Latenza 1h, fee 0,10%, slippage 0,10%, carry SHORT 0,14% dichiarato come ipotesi.",
            evidence={
                "primary_scenario": primary["id"],
                "fee_rate": primary["fee_rate"],
                "slippage": primary["slippage"],
                "latency_hours": primary["latency_hours"],
                "carry_short_pct": 100.0 * float(primary["short_carry"]["carry_pct_per_trade"]),
                "carry_status": primary["short_carry"]["status"],
                "matches_cp08_03_row": primary_matches,
            },
        ),
        _check(
            "CP08-04.9",
            "Walk-forward e requisiti minimi di campione",
            bool(
                protocol["walk_forward"]["type"] == "expanding"
                and int(protocol["walk_forward"]["purge_hours"]) == 24
                and int(protocol["walk_forward"]["test_windows_per_fold"]) > 0
                and int(criteria["sample_requirements"]["min_trades"]) >= 100
                and int(criteria["sample_requirements"]["min_folds"]) >= 5
                and int(criteria["sample_requirements"]["min_independent_days"]) >= 180
            ),
            "Fold espandenti con purge 24h, blocchi da 60 finestre e minimi di campione pre-registrati.",
            evidence={
                "walk_forward": protocol["walk_forward"],
                "sample_requirements": criteria["sample_requirements"],
            },
        ),
        _check(
            "CP08-04.10",
            "Criteri GO/NO-GO completi, non contraddittori e ordinati",
            bool(criteria.get("go_criteria") and criteria.get("no_go_criteria")),
            "Ogni metrica ha soglie compatibili; nessun valore può soddisfare GO e NO-GO insieme.",
            evidence={
                "go_criteria": [item["id"] for item in criteria["go_criteria"]],
                "no_go_criteria": [item["id"] for item in criteria["no_go_criteria"]],
                "decision_rule": criteria["decision_rule"],
            },
        ),
        _check(
            "CP08-04.11",
            "Momentum Confirmation Engine: specifica versionata, nessuna regola attiva",
            bool(engine["status"] == "SPECIFICATION_ONLY" and not engine_rules and engine["runnable"] is False),
            "Configurazione pronta ma non eseguibile: nessuna regola derivata dal periodo OOS, nessun input di target.",
            evidence={
                "engine_id": engine["engine_id"],
                "engine_version": engine["engine_version"],
                "status": engine["status"],
                "active_rules": len(engine_rules),
                "engine_sha256": loaded["engine_config_sha256"],
            },
        ),
        _check(
            "CP08-04.12",
            "Controlli anti-riuso attivi e registro pulito",
            bool(
                protocol["anti_reuse_controls"]["one_final_evaluation_per_protocol_version"]
                and protocol["anti_reuse_controls"]["require_dataset_registration"]
                and protocol["anti_reuse_controls"]["require_boundary_check"]
                and registry_state["final_evaluations"] == 0
            ),
            "Registro append-only: dataset pinnato per hash, una sola valutazione finale, nessuna esecuzione registrata.",
            evidence={
                "controls": protocol["anti_reuse_controls"],
                "registry_dir": str(paths.registry_dir),
                "registry_state": registry_state,
            },
        ),
        _check(
            "CP08-04.13",
            "Nessun dato indipendente disponibile: verdetto NOT_EVALUATED",
            True,
            "Il protocollo è pronto ma non esiste evidenza indipendente: nessun GO è possibile.",
            status=STATUS_WARN,
            evidence={
                "independent_start_utc": protocol["independent_validation"]["start_utc"],
                "data_available": False,
                "verdict": VERDICT_NOT_EVALUATED,
                "strategic_gate": VERDICT_NO_GO,
            },
        ),
        _check(
            "CP08-04.14",
            "Nessun training, inferenza, download o modifica agli archivi",
            True,
            "Solo letture locali e output additivo; hash sorgente verificati prima e dopo la scrittura.",
            evidence={
                "training_performed": False,
                "new_inference": False,
                "data_downloaded": False,
                "original_artifacts_modified": False,
            },
        ),
        _check(
            "CP08-04.15",
            "Determinismo dell'hash canonico del protocollo",
            bool(canonical_hash(protocol) == loaded["canonical_sha256"]),
            "L'hash canonico è stabile e indipendente dalla formattazione del file.",
            evidence={"canonical_sha256": canonical_hash(protocol)},
        ),
    ]
    failed = [check["id"] for check in checks if check["status"] == STATUS_FAIL]
    technical_gate = STATUS_PASS if not failed else STATUS_FAIL
    return {
        "schema_version": VALIDATION_SCHEMA,
        "generated_utc": _now_iso(),
        "protocol": protocol,
        "protocol_meta": {
            "protocol_id": protocol["protocol_id"],
            "protocol_version": protocol["protocol_version"],
            "config_path": loaded["config_path"],
            "config_sha256": loaded["config_sha256"],
            "canonical_sha256": loaded["canonical_sha256"],
            "engine_config_path": loaded["engine_config_path"],
            "engine_config_sha256": loaded["engine_config_sha256"],
            "engine_canonical_sha256": loaded["engine_canonical_sha256"],
        },
        "config_bytes": loaded["config_bytes"],
        "engine_config_bytes": loaded["engine_config_bytes"],
        "frozen_artifacts": artifacts,
        "frozen_benchmark": benchmark,
        "data_registry": data_registry,
        "data_separation": separation,
        "immutability": {
            "protocol": sidecar,
            "engine": engine_sidecar,
            "both_match": bool(sidecar["match"] and engine_sidecar["match"]),
        },
        "criteria": criteria,
        "primary_scenario": primary,
        "confirmation_engine": {
            "config": engine,
            "sha256": loaded["engine_config_sha256"],
            "canonical_sha256": loaded["engine_canonical_sha256"],
            "active_rules": len(engine_rules),
            "status": engine["status"],
        },
        "registry": {
            "directory": str(paths.registry_dir),
            "state": registry_state,
            "datasets": registry.datasets(),
            "evaluations": registry.evaluations(),
        },
        "independent_validation": {
            "status": VERDICT_NOT_EVALUATED,
            "data_available": False,
            "verdict": VERDICT_NOT_EVALUATED,
            "reason": "Nessun dataset indipendente è presente nel repository: il protocollo attende dati nuovi.",
            "start_utc": protocol["independent_validation"]["start_utc"],
        },
        "checks": checks,
        "checks_failed": failed,
        "technical_gate": technical_gate,
        "strategic_gate": VERDICT_NO_GO,
        "confirmation_engine_started": False,
        "intern_decision_0_8b_started": False,
    }


# ---------------------------------------------------------------------------
# artifacts
# ---------------------------------------------------------------------------


def write_artifacts(
    paths: ValidationPaths, report: Mapping[str, Any]
) -> dict[str, Any]:
    """Write the additive protocol archive; never overwrite, never touch sources."""

    output = paths.output
    if output.exists():
        raise FileExistsError(
            f"S02-CP08-04 archive already exists; refusing to overwrite: {output}"
        )
    before = {item["path"]: item["actual_sha256"] for item in report["frozen_artifacts"]}
    output.mkdir(parents=True)
    try:
        schema = report["schema_version"]
        payloads: dict[str, Any] = {
            "protocol.json": {
                "schema_version": schema,
                "protocol_meta": report["protocol_meta"],
                "protocol": report["protocol"],
            },
            "criteria.json": report["criteria"],
            "data_registry.json": {
                "schema_version": schema,
                "research_period": report["data_registry"]["research_period"],
                "observed": report["data_registry"]["observed"],
                "declared": report["data_registry"]["declared"],
                "comparisons": report["data_registry"]["comparisons"],
                "all_match": report["data_registry"]["all_match"],
                "datasets": report["data_registry"]["datasets"],
                "separation": report["data_separation"],
            },
            "frozen_benchmark.json": report["frozen_benchmark"],
            "confirmation_engine.json": report["confirmation_engine"],
            "anti_reuse.json": {
                "schema_version": schema,
                "controls": report["protocol"]["anti_reuse_controls"],
                "registry": report["registry"],
            },
            "checks.json": report["checks"],
            "summary.json": {
                "schema_version": schema,
                "generated_utc": report["generated_utc"],
                "protocol": report["protocol_meta"],
                "technical_gate": report["technical_gate"],
                "strategic_gate": report["strategic_gate"],
                "independent_validation": report["independent_validation"],
                "checks": {
                    "total": len(report["checks"]),
                    "failed": report["checks_failed"],
                    "statuses": {check["id"]: check["status"] for check in report["checks"]},
                },
                "frozen_benchmark": {
                    "all_match": report["frozen_benchmark"]["all_match"],
                    "max_abs_delta": max(
                        (abs(row["delta"]) for row in report["frozen_benchmark"]["comparisons"]),
                        default=0.0,
                    ),
                    "expected": report["protocol"]["frozen_benchmark"]["expected"],
                    "cp08_03_reference": report["protocol"]["frozen_benchmark"]["cp08_03_reference"],
                },
                "primary_scenario": report["primary_scenario"],
                "criteria": {
                    "go": [item["id"] for item in report["criteria"]["go_criteria"]],
                    "no_go": [item["id"] for item in report["criteria"]["no_go_criteria"]],
                    "sample_requirements": report["criteria"]["sample_requirements"],
                    "decision_rule": report["criteria"]["decision_rule"],
                },
                "data_separation": report["data_separation"],
                "registry_state": report["registry"]["state"],
                "immutability": report["immutability"],
                "confirmation_engine": {
                    "engine_id": report["confirmation_engine"]["config"]["engine_id"],
                    "engine_version": report["confirmation_engine"]["config"]["engine_version"],
                    "status": report["confirmation_engine"]["status"],
                    "active_rules": report["confirmation_engine"]["active_rules"],
                    "sha256": report["confirmation_engine"]["sha256"],
                },
                "confirmation_engine_started": False,
                "intern_decision_0_8b_started": False,
            },
        }
        for filename, payload in payloads.items():
            (output / filename).write_text(
                json.dumps(_json_safe(payload), indent=2, default=str) + "\n",
                encoding="utf-8",
            )
        # Byte-identical copies of the two frozen configuration files.
        (output / "momentum-independent-validation-v1.yaml").write_bytes(
            report["config_bytes"]
        )
        (output / "momentum-confirmation-engine-v1.yaml").write_bytes(
            report["engine_config_bytes"]
        )
        after = {
            item["path"]: digest(paths.resolve(item["path"]))
            for item in report["frozen_artifacts"]
            if paths.resolve(item["path"]).is_file()
        }
        if before != after:
            raise ProtocolViolation("A frozen source artifact changed during protocol validation")
        outputs = {
            name: {"path": name, "sha256": digest(output / name)}
            for name in (
                *payloads.keys(),
                "momentum-independent-validation-v1.yaml",
                "momentum-confirmation-engine-v1.yaml",
            )
        }
        manifest = {
            "schema_version": VALIDATION_SCHEMA,
            "created_utc": _now_iso(),
            "read_only": True,
            "analysis": "S02-CP08-04 independent validation protocol & research freeze",
            "protocol": report["protocol_meta"],
            "config_sha256": report["protocol_meta"]["config_sha256"],
            "canonical_sha256": report["protocol_meta"]["canonical_sha256"],
            "engine_sha256": report["protocol_meta"]["engine_config_sha256"],
            "frozen_artifacts": report["frozen_artifacts"],
            "frozen_artifacts_unchanged": bool(before == after),
            "original_artifacts_modified": bool(before != after),
            "data_registry": report["data_registry"]["datasets"],
            "data_separation": report["data_separation"],
            "primary_scenario": report["primary_scenario"],
            "criteria_summary": {
                "go": [item["id"] for item in report["criteria"]["go_criteria"]],
                "no_go": [item["id"] for item in report["criteria"]["no_go_criteria"]],
                "sample_requirements": report["criteria"]["sample_requirements"],
            },
            "independent_validation_status": report["independent_validation"]["status"],
            "independent_data_available": False,
            "verdict": report["independent_validation"]["verdict"],
            "technical_gate": report["technical_gate"],
            "strategic_gate": report["strategic_gate"],
            "checks_total": len(report["checks"]),
            "checks_failed": report["checks_failed"],
            "confirmation_engine_status": report["confirmation_engine"]["status"],
            "confirmation_engine_started": False,
            "intern_decision_0_8b_started": False,
            "outputs": outputs,
        }
        (output / "manifest.json").write_text(
            json.dumps(_json_safe(manifest), indent=2, default=str) + "\n", encoding="utf-8"
        )
        return {"output_dir": str(output), "manifest": manifest}
    except BaseException:
        shutil.rmtree(output, ignore_errors=True)
        raise


def run(
    paths: ValidationPaths = ValidationPaths(),
    *,
    write: bool = True,
) -> dict[str, Any]:
    report = build_readiness(paths)
    if write:
        report["written"] = write_artifacts(paths, report)
    return report


def _printable(report: Mapping[str, Any]) -> dict[str, Any]:
    return _json_safe(
        {
            "protocol_id": report["protocol_meta"]["protocol_id"],
            "protocol_version": report["protocol_meta"]["protocol_version"],
            "canonical_sha256": report["protocol_meta"]["canonical_sha256"],
            "config_sha256": report["protocol_meta"]["config_sha256"],
            "technical_gate": report["technical_gate"],
            "strategic_gate": report["strategic_gate"],
            "independent_validation_status": report["independent_validation"]["status"],
            "verdict": report["independent_validation"]["verdict"],
            "checks_total": len(report["checks"]),
            "checks_failed": report["checks_failed"],
            "frozen_benchmark": {
                "trades": report["frozen_benchmark"]["comparisons"][0]["observed"],
                "total_return_pct": report["frozen_benchmark"]["comparisons"][1]["observed"],
                "profit_factor_usdt": report["frozen_benchmark"]["comparisons"][2]["observed"],
                "max_drawdown_mtm_pct": report["frozen_benchmark"]["comparisons"][4]["observed"],
            },
        }
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="S02-CP08-04 independent validation protocol & research freeze (read-only)"
    )
    parser.add_argument("--repo-root", default=str(ValidationPaths.repo_root))
    parser.add_argument("--config", default=str(ValidationPaths.config_path))
    parser.add_argument("--engine-config", default=str(ValidationPaths.engine_config_path))
    parser.add_argument("--registry-dir", default=str(ValidationPaths.registry_dir))
    parser.add_argument("--output-dir", default=str(ValidationPaths.output_dir))
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)
    paths = ValidationPaths(
        repo_root=Path(args.repo_root),
        config_path=Path(args.config),
        engine_config_path=Path(args.engine_config),
        registry_dir=Path(args.registry_dir),
        output_dir=Path(args.output_dir),
    )
    report = run(paths, write=not args.no_write)
    payload = _printable(report)
    if "written" in report:
        payload["written"] = report["written"]["output_dir"]
        payload["manifest_sha256"] = digest(Path(report["written"]["output_dir"]) / "manifest.json")
    print(json.dumps(payload, indent=2, default=str))
    return 0 if report["technical_gate"] == STATUS_PASS else 2


if __name__ == "__main__":
    raise SystemExit(main())
