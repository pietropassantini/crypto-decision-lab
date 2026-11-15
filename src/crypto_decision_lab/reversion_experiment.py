"""E04: preregistered mean-reversion experiment.

Spec: ``config/experiments/E04-mean-reversion-v1.json`` (schema
``cdl.e04.experiment.v1``).  Protocol, selection rule and acceptance criteria
are the shared ones of :mod:`crypto_decision_lab.experiment_protocol` (the
same as E01 and E03), so the outcomes are comparable.
"""
from __future__ import annotations

import itertools

from . import experiment_protocol as protocol
from .mean_reversion import reversion_features
from .pattern_formation_experiment import ExperimentError
from .studio import build_run_config

GRID_KEYS = {"entry_z", "window_hours", "horizon_hours"}


def _validate(spec: dict) -> None:
    inputs = spec["inputs"]
    if set(inputs["grid"]) != GRID_KEYS:
        raise ExperimentError(f"E04 grid must be exactly {sorted(GRID_KEYS)}")
    if int(inputs["fixed"].get("regime_sma_hours", 0)) < 2:
        raise ExperimentError("spec inputs.fixed require regime_sma_hours")


def grid_configurations(spec: dict) -> list[dict]:
    fixed = spec["inputs"]["fixed"]
    grid = spec["inputs"]["grid"]
    keys = list(grid)
    out = []
    for index, values in enumerate(itertools.product(*(grid[k] for k in keys))):
        point = dict(zip(keys, values))
        run_config = {"strategy": {"name": "mean_reversion",
                                   "parameters": {**fixed.get("strategy", {}), "entry_z": point["entry_z"]}},
                      "initial_cash": fixed["initial_cash"],
                      "hypothesis": {**fixed["hypothesis"], "horizon_hours": point["horizon_hours"]},
                      "risk": fixed["risk"]}
        build_run_config(run_config)  # validate early
        out.append({"config_id": f"g{index:02d}", "grid": point, "run_config": run_config,
                    "feature_key": int(point["window_hours"])})
    return out


def _features(frame, spec: dict, window_hours: int) -> dict:
    return reversion_features(frame, window_hours, int(spec["inputs"]["fixed"]["regime_sma_hours"]))


def _exit_cause(reason: str) -> str:
    if reason.startswith("exit: reverted"):
        return "reverted"
    if reason.startswith("missing feature"):
        return "missing_feature"
    return "other"


ADAPTER = protocol.Adapter(label="E04", schema="cdl.e04.experiment.v1", report_schema="cdl.e04.report.v1",
                           configurations=grid_configurations, features=_features, exit_cause=_exit_cause,
                           validate=_validate)


def load_spec(spec_path):
    return protocol.load_spec(ADAPTER, spec_path)


def develop(spec_path, **kwargs) -> dict:
    return protocol.develop(ADAPTER, spec_path, **kwargs)


def evaluate(spec_path, **kwargs) -> dict:
    return protocol.evaluate(ADAPTER, spec_path, **kwargs)


def main(argv=None) -> int:
    return protocol.cli(ADAPTER, "E04 preregistered mean-reversion experiment", argv)


if __name__ == "__main__":
    raise SystemExit(main())
