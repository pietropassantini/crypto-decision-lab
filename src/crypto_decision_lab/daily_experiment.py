"""E05: preregistered daily-bar trend experiment.

Spec: ``config/experiments/E05-daily-trend-v1.json`` (schema
``cdl.e05.experiment.v1``).  Shared two-phase protocol
(:mod:`crypto_decision_lab.experiment_protocol`) with the E01 criteria, except
the minimum numbers of closed trades, which the spec lowers for daily
decisions (``selection.min_closed_trades`` and ``acceptance.min_closed_trades``).
"""
from __future__ import annotations

import itertools

from . import experiment_protocol as protocol
from .daily_trend import daily_features
from .pattern_formation_experiment import ExperimentError
from .studio import build_run_config

GRID_KEYS = {"momentum_days", "sma_days", "entry_threshold"}
SELECTION_MIN_TRADES = 12
ACCEPTANCE_MIN_TRADES = 8


def _validate(spec: dict) -> None:
    if set(spec["inputs"]["grid"]) != GRID_KEYS:
        raise ExperimentError(f"E05 grid must be exactly {sorted(GRID_KEYS)}")
    declared = (spec.get("selection", {}).get("min_closed_trades"), spec.get("acceptance", {}).get("min_closed_trades"))
    if declared != (SELECTION_MIN_TRADES, ACCEPTANCE_MIN_TRADES):
        raise ExperimentError(f"spec minimum trades {declared} differ from the runner's "
                              f"{(SELECTION_MIN_TRADES, ACCEPTANCE_MIN_TRADES)}")


def grid_configurations(spec: dict) -> list[dict]:
    fixed = spec["inputs"]["fixed"]
    grid = spec["inputs"]["grid"]
    keys = list(grid)
    out = []
    for index, values in enumerate(itertools.product(*(grid[k] for k in keys))):
        point = dict(zip(keys, values))
        run_config = {"strategy": {"name": "daily_trend",
                                   "parameters": {**fixed.get("strategy", {}),
                                                  "entry_threshold": point["entry_threshold"]}},
                      "initial_cash": fixed["initial_cash"], "hypothesis": fixed["hypothesis"], "risk": fixed["risk"]}
        build_run_config(run_config)  # validate early
        out.append({"config_id": f"g{index:02d}", "grid": point, "run_config": run_config,
                    "feature_key": (int(point["momentum_days"]), int(point["sma_days"]))})
    return out


def _features(frame, spec: dict, key: tuple[int, int]) -> dict:
    return daily_features(frame, key[0], key[1])


def _exit_cause(reason: str) -> str:
    if reason.startswith("exit:"):
        return "exit_threshold"
    if reason.startswith("regime off"):
        return "regime_off"
    if reason.startswith("missing feature"):
        return "missing_feature"
    return "other"


ADAPTER = protocol.Adapter(label="E05", schema="cdl.e05.experiment.v1", report_schema="cdl.e05.report.v1",
                           configurations=grid_configurations, features=_features, exit_cause=_exit_cause,
                           validate=_validate, selection_min_trades=SELECTION_MIN_TRADES,
                           acceptance_min_trades=ACCEPTANCE_MIN_TRADES)


def load_spec(spec_path):
    return protocol.load_spec(ADAPTER, spec_path)


def develop(spec_path, **kwargs) -> dict:
    return protocol.develop(ADAPTER, spec_path, **kwargs)


def evaluate(spec_path, **kwargs) -> dict:
    return protocol.evaluate(ADAPTER, spec_path, **kwargs)


def main(argv=None) -> int:
    return protocol.cli(ADAPTER, "E05 preregistered daily-bar trend experiment", argv)


if __name__ == "__main__":
    raise SystemExit(main())
