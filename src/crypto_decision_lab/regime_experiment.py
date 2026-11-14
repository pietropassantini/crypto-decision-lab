"""E03: preregistered experiment for momentum with hysteresis and regime filter.

Spec: ``config/experiments/E03-regime-momentum-v1.json`` (schema
``cdl.e03.experiment.v1``).  Same protocol and acceptance criteria as E01
(:mod:`crypto_decision_lab.turnover_experiment`), so the two outcomes are
comparable:

1. ``develop``: preregistration lock before any data is read; the grid and the
   baselines on the development window; selection (``closed_trades >= 30``,
   highest net return, ties -> fewer trades -> grid order); write-once
   ``selection.json``, to be committed before phase 2.
2. ``evaluate``: one evaluation of the selected configuration and of the
   baselines on validation and confirmation; the five validation and two
   confirmation criteria of E01, applied against the ``momentum_v1`` baseline;
   outcome CANDIDATE / INCONCLUSIVE / REJECTED.

The ``trend_ratio`` feature (:func:`regime_momentum.trend_features`) is
computed over the whole admitted frame, so bars before a window only warm the
SMA up; gaps are acknowledged as in ``release.evaluate_backtest``.
"""
from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pandas as pd

from .decision_contract import utc
from .pattern_formation_experiment import ExperimentError, _file_sha256, _now, _write_json, metrics
from .regime_momentum import trend_features
from .simulation import Engine, MarketEvent, engine_digests, market_events
from .studio import build_run_config
from .turnover_experiment import (
    _load_admitted,
    _output,
    confirmation_criteria,
    decide,
    select,
    validation_criteria,
)

EXPERIMENT_SCHEMA = "cdl.e03.experiment.v1"
REPORT_SCHEMA = "cdl.e03.report.v1"
WINDOWS = ("development", "validation", "confirmation")
OUTCOMES = ("CANDIDATE", "INCONCLUSIVE", "REJECTED")
TURNOVER_BASELINE = "momentum_v1"


# --------------------------------------------------------------- spec
def load_spec(spec_path: str | Path) -> tuple[dict, str]:
    path = Path(spec_path)
    spec = json.loads(path.read_text(encoding="utf-8-sig"))
    if spec.get("schema_version") != EXPERIMENT_SCHEMA:
        raise ExperimentError(f"unsupported spec schema: {spec.get('schema_version')}")
    if spec.get("status") != "PREREGISTERED":
        raise ExperimentError(f"spec status is {spec.get('status')!r}: only a PREREGISTERED spec can run")
    inputs = spec.get("inputs", {})
    for key in ("dataset_id", "source_manifest_sha256", "fixed", "grid", "windows"):
        if key not in inputs:
            raise ExperimentError(f"spec inputs require {key}")
    if set(inputs["grid"]) != {"entry_threshold", "exit_threshold", "sma_hours"}:
        raise ExperimentError("E03 grid must be entry_threshold x exit_threshold x sma_hours")
    if TURNOVER_BASELINE not in spec.get("baselines", {}):
        raise ExperimentError(f"spec baselines require {TURNOVER_BASELINE} (turnover criterion)")
    windows = inputs["windows"]
    if tuple(windows) != WINDOWS:
        raise ExperimentError(f"windows must be exactly {WINDOWS} in order")
    bounds = [(utc(windows[w]["start"]), utc(windows[w]["end"])) for w in WINDOWS]
    if any(start >= end for start, end in bounds) or any(a[1] > b[0] for a, b in zip(bounds, bounds[1:])):
        raise ExperimentError("windows must be non-empty, ordered and non-overlapping")
    return spec, _file_sha256(path)


def grid_configurations(spec: dict) -> list[dict]:
    fixed = spec["inputs"]["fixed"]
    grid = spec["inputs"]["grid"]
    keys = list(grid)
    out = []
    for index, values in enumerate(itertools.product(*(grid[k] for k in keys))):
        point = dict(zip(keys, values))
        parameters = {**fixed.get("strategy", {}), "entry_threshold": point["entry_threshold"],
                      "exit_threshold": point["exit_threshold"]}
        run_config = {"strategy": {"name": "regime_momentum", "parameters": parameters},
                      "initial_cash": fixed["initial_cash"], "hypothesis": fixed["hypothesis"], "risk": fixed["risk"]}
        build_run_config(run_config)  # validate early (hysteresis: exit <= entry)
        out.append({"config_id": f"g{index:02d}", "grid": point, "sma_hours": int(point["sma_hours"]),
                    "run_config": run_config})
    return out


def baseline_configurations(spec: dict) -> dict[str, dict]:
    fixed = spec["inputs"]["fixed"]
    out = {}
    for name, baseline in spec["baselines"].items():
        run_config = {"strategy": baseline["strategy"], "initial_cash": fixed["initial_cash"],
                      "hypothesis": {**fixed["hypothesis"], **baseline.get("hypothesis_override", {})},
                      "risk": fixed["risk"]}
        build_run_config(run_config)
        out[name] = run_config
    return out


# --------------------------------------------------------------- evaluation
def run_window(frame: pd.DataFrame, run_config: dict, window: dict,
               features: dict | None = None) -> tuple[dict, list[dict]]:
    engine = Engine(build_run_config(run_config))
    ledger: list[dict] = []
    gaps = missing = 0
    for event in market_events(frame, window["start"], window["end"]):
        if features is not None:
            event = MarketEvent(event.timestamp, event.open, {**event.features, **features[utc(event.timestamp)]})
        records = engine.step(event)
        if engine.status == "HALTED_GAP":
            ledger.extend(records)
            acknowledged = engine.acknowledge_gap(event.timestamp, note="E03: gap acknowledged (S08 rule)")
            gaps += 1
            missing += next(r["missing_hours"] for r in acknowledged if r["kind"] == "gap_acknowledged")
            ledger.extend(acknowledged)
            records = engine.step(event)
        ledger.extend(records)
    if not ledger:
        raise ExperimentError(f"no market events in window {window}")
    ledger.extend(engine.finish())
    return metrics(ledger, float(run_config["initial_cash"]), window, gaps, missing), ledger


def _exit_reasons(ledger: list[dict]) -> dict[str, int]:
    """Why positions were closed: hysteresis exit, regime off, missing feature, horizon, window end."""
    counts: dict[str, int] = {}
    decision = None
    for record in ledger:
        if record["kind"] == "decision":
            decision = record
        elif record["kind"] == "order" and record["side"] == "SELL" and decision is not None:
            reason = decision["reason"]
            cause = ("exit_threshold" if reason.startswith("exit:") else "regime_off" if reason.startswith("regime off")
                     else "missing_feature" if reason.startswith("missing feature") else
                     "horizon" if reason == "holding horizon reached" else
                     "window" if reason == "evaluation window closed" else "other")
            counts[cause] = counts.get(cause, 0) + 1
    return dict(sorted(counts.items()))


def _run(frame, configuration_or_config, window, features_for):
    if "config_id" in configuration_or_config:
        result, ledger = run_window(frame, configuration_or_config["run_config"], window,
                                    features_for(configuration_or_config["sma_hours"]))
    else:
        result, ledger = run_window(frame, configuration_or_config, window)
    result["exit_reasons"] = _exit_reasons(ledger)
    return result, ledger


def develop(spec_path: str | Path, *, repo_root: str | Path = ".", output_dir: str | Path | None = None,
            catalog_path: str | Path = "config/dataset-catalog.json") -> dict:
    root = Path(repo_root).resolve()
    spec, spec_sha = load_spec(spec_path)
    out = _output(spec, root, output_dir)
    if out.exists():
        raise FileExistsError(f"experiment output exists; refusing to overwrite: {out}")
    out.mkdir(parents=True)
    _write_json(out / "preregistration.json", {
        "experiment_id": spec["experiment_id"], "spec_sha256": spec_sha, "spec": spec,
        "locked_utc": _now(), "note": "written before reading data",
    })
    frame = _load_admitted(spec, root, catalog_path)
    cache: dict[int, dict] = {}

    def features_for(sma_hours):
        if sma_hours not in cache:
            cache[sma_hours] = trend_features(frame, sma_hours)
        return cache[sma_hours]

    window = spec["inputs"]["windows"]["development"]
    configurations = grid_configurations(spec)
    development = {}
    for configuration in configurations:
        development[configuration["config_id"]], ledger = _run(frame, configuration, window, features_for)
        _write_json(out / "ledgers" / f"development-{configuration['config_id']}.json", ledger)
    baselines = {}
    for name, run_config in baseline_configurations(spec).items():
        baselines[name], ledger = _run(frame, run_config, window, features_for)
        _write_json(out / "ledgers" / f"development-baseline-{name}.json", ledger)
    selected, rule = select(development, [c["config_id"] for c in configurations])
    selection = {
        "experiment_id": spec["experiment_id"], "spec_sha256": spec_sha, "engine_sha256": engine_digests()[0],
        "selected": selected, "selected_grid": next((c["grid"] for c in configurations
                                                    if c["config_id"] == selected), None),
        "rule": rule, "configurations": {c["config_id"]: c["grid"] for c in configurations},
        "development": development, "baselines": baselines, "locked_utc": _now(),
        "note": "written before validation and confirmation are evaluated",
    }
    _write_json(out / "selection.json", selection)
    return selection


def evaluate(spec_path: str | Path, *, repo_root: str | Path = ".", output_dir: str | Path | None = None,
             catalog_path: str | Path = "config/dataset-catalog.json") -> dict:
    root = Path(repo_root).resolve()
    spec, spec_sha = load_spec(spec_path)
    out = _output(spec, root, output_dir)
    selection_path = out / "selection.json"
    if not selection_path.is_file():
        raise ExperimentError("phase 2 requires the phase-1 selection.json (run develop first)")
    if (out / "report.json").exists():
        raise FileExistsError("validation and confirmation were already evaluated for this output")
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    if selection["spec_sha256"] != spec_sha:
        raise ExperimentError("selection.json was written for a different spec digest")
    if selection["engine_sha256"] != engine_digests()[0]:
        raise ExperimentError("engine changed since the selection was locked")
    configurations = {c["config_id"]: c for c in grid_configurations(spec)}
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA, "experiment_id": spec["experiment_id"], "spec_sha256": spec_sha,
        "engine_sha256": selection["engine_sha256"],
        "source_manifest_sha256": spec["inputs"]["source_manifest_sha256"],
        "configurations": {k: v["grid"] for k, v in configurations.items()},
        "development": {"grid": selection["development"], "baselines": selection["baselines"]},
        "selection": {"selected": selection["selected"], "grid": selection["selected_grid"],
                      "rule": selection["rule"], "selection_sha256": _file_sha256(selection_path)},
    }
    if selection["selected"] is None:
        report.update(outcome="INCONCLUSIVE", outcome_reason=selection["rule"])
    else:
        frame = _load_admitted(spec, root, catalog_path)
        cache: dict[int, dict] = {}

        def features_for(sma_hours):
            if sma_hours not in cache:
                cache[sma_hours] = trend_features(frame, sma_hours)
            return cache[sma_hours]

        selected = configurations[selection["selected"]]
        for name in ("validation", "confirmation"):
            window = spec["inputs"]["windows"][name]
            result, ledger = _run(frame, selected, window, features_for)
            _write_json(out / "ledgers" / f"{name}-{selected['config_id']}.json", ledger)
            baselines = {}
            for baseline_name, run_config in baseline_configurations(spec).items():
                baselines[baseline_name], ledger = _run(frame, run_config, window, features_for)
                _write_json(out / "ledgers" / f"{name}-baseline-{baseline_name}.json", ledger)
            reference = baselines[TURNOVER_BASELINE]
            criteria = validation_criteria(result, reference) if name == "validation" else confirmation_criteria(result)
            report[name] = {"selected": result, "baselines": baselines, "criteria": criteria,
                            "turnover_ratio_vs_momentum_v1": (result["closed_trades_per_year"]
                                                              / reference["closed_trades_per_year"]
                                                              if reference["closed_trades_per_year"] else None)}
        outcome, reason = decide(report["validation"], report["confirmation"])
        report.update(outcome=outcome, outcome_reason=reason)
    report["evaluated_utc"] = _now()
    _write_json(out / "report.json", report)
    files = sorted(p for p in out.rglob("*") if p.is_file())
    _write_json(out / "manifest.json", {
        "experiment_id": spec["experiment_id"], "spec_sha256": spec_sha, "created_utc": _now(),
        "files": {p.relative_to(out).as_posix(): _file_sha256(p) for p in files},
    })
    return report


def ledger_digests(report: dict) -> dict[str, str]:
    out = {f"development-{k}": v["ledger_sha256"] for k, v in report["development"]["grid"].items()}
    out.update({f"development-baseline-{k}": v["ledger_sha256"] for k, v in report["development"]["baselines"].items()})
    for name in ("validation", "confirmation"):
        if name in report:
            out[f"{name}-selected"] = report[name]["selected"]["ledger_sha256"]
            out.update({f"{name}-baseline-{k}": v["ledger_sha256"] for k, v in report[name]["baselines"].items()})
    return out


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="E03 preregistered regime-momentum experiment")
    parser.add_argument("phase", choices=("develop", "evaluate", "all"))
    parser.add_argument("--spec", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--catalog", default="config/dataset-catalog.json")
    args = parser.parse_args(argv)
    kwargs = dict(repo_root=args.repo_root, output_dir=args.output_dir, catalog_path=args.catalog)

    def brief(result):
        return {k: result[k] for k in ("net_return_pct", "max_drawdown_mtm_pct", "closed_trades",
                                       "closed_trades_per_year", "costs", "gross_pnl", "exit_reasons")}

    if args.phase in ("develop", "all"):
        selection = develop(args.spec, **kwargs)
        print(json.dumps({"selected": selection["selected"], "grid": selection["selected_grid"],
                          "rule": selection["rule"],
                          "baselines": {k: brief(v) for k, v in selection["baselines"].items()},
                          "development": {k: brief(v) for k, v in selection["development"].items()}},
                         indent=2, default=str))
    if args.phase in ("evaluate", "all"):
        report = evaluate(args.spec, **kwargs)
        print(json.dumps({"outcome": report["outcome"], "reason": report["outcome_reason"],
                          **{w: {"selected": brief(report[w]["selected"]),
                                 "baselines": {k: brief(v) for k, v in report[w]["baselines"].items()},
                                 "criteria": [(c["criterion"], c["observed"], c["threshold"], c["passed"])
                                              for c in report[w]["criteria"]]}
                             for w in ("validation", "confirmation") if w in report}}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
