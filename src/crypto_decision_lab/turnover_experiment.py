"""E01 (S09): preregistered turnover-reduction experiment on the momentum candidate.

Spec: ``config/experiments/S09-E01-turnover-reduction-v1.json`` (schema
``cdl.s09.experiment.v1``).  The spec is the authority; this runner encodes its
selection rule and acceptance criteria literally (see :data:`VALIDATION` and
:data:`CONFIRMATION`) and never modifies it.

Two phases, so the selection exists in a versioned file before validation:

1. ``develop``: preregistration lock (written before reading data), baseline
   and the 12 grid configurations on the development window, selection by the
   registered rule, ``selection.json`` (write-once).  The operator commits a
   copy of ``selection.json`` before phase 2.
2. ``evaluate``: requires the phase-1 ``selection.json`` of the same spec
   digest, then one evaluation of the selected configuration and of the
   baseline on validation and confirmation, criteria and outcome,
   ``report.json`` and ``manifest.json``.

``all`` runs both phases in one process (used for the reproducibility run).

Interpretations, declared because the spec is silent:

* No configuration with ``closed_trades >= 30`` on development: outcome
  ``INCONCLUSIVE``, validation is not run (as in E02).
* Selection ties beyond "fewer closed trades": grid order.
* Fewer than 15 closed trades on validation: ``INCONCLUSIVE`` takes
  precedence over ``REJECTED``, as the outcome table lists it under
  INCONCLUSIVE.

Gaps are acknowledged as in :func:`crypto_decision_lab.release.evaluate_backtest`.
"""
from __future__ import annotations

import itertools
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from .dataset_catalog import Catalog, load_market
from .decision_contract import utc
from .forward_isolation import guard_research_output
from .pattern_formation_experiment import ExperimentError, _file_sha256, _now, _write_json, metrics
from .simulation import Engine, engine_digests, market_events
from .studio import build_run_config

EXPERIMENT_SCHEMA = "cdl.s09.experiment.v1"
REPORT_SCHEMA = "cdl.e01.report.v1"
WINDOWS = ("development", "validation", "confirmation")
OUTCOMES = ("CANDIDATE", "INCONCLUSIVE", "REJECTED")
SELECTION_MIN_TRADES = 30          # spec selection_rule
ACCEPTANCE_MIN_TRADES = 15         # spec acceptance.outcomes INCONCLUSIVE
TURNOVER_RATIO_MAX = 0.30          # closed_trades_per_year <= 30% of the baseline
COSTS_SHARE_MAX = 0.50             # costs <= 50% of the gross result (net + costs)
BASELINE = {"threshold": 0.0, "horizon_hours": 6}   # spec baseline.candidate: btcusdt-momentum-v1


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
    if inputs["fixed"].get("strategy") != "momentum" or set(inputs["grid"]) != {"threshold", "horizon_hours"}:
        raise ExperimentError("E01 runner expects a momentum strategy with a threshold x horizon_hours grid")
    windows = inputs["windows"]
    if tuple(windows) != WINDOWS:
        raise ExperimentError(f"windows must be exactly {WINDOWS} in order")
    bounds = [(utc(windows[w]["start"]), utc(windows[w]["end"])) for w in WINDOWS]
    if any(start >= end for start, end in bounds) or any(a[1] > b[0] for a, b in zip(bounds, bounds[1:])):
        raise ExperimentError("windows must be non-empty, ordered and non-overlapping")
    return spec, _file_sha256(path)


def _run_config(fixed: dict, threshold: float, horizon_hours: int) -> dict:
    config = {"strategy": {"name": "momentum", "parameters": {"threshold": threshold}},
              "initial_cash": fixed["initial_cash"],
              "hypothesis": {**fixed["hypothesis"], "horizon_hours": horizon_hours}, "risk": fixed["risk"]}
    build_run_config(config)  # validate early
    return config


def grid_configurations(spec: dict) -> list[dict]:
    """``threshold`` x ``horizon_hours`` in declared order; ids g00..g11 are stable."""
    inputs = spec["inputs"]
    grid = inputs["grid"]
    keys = list(grid)
    out = []
    for index, values in enumerate(itertools.product(*(grid[k] for k in keys))):
        point = dict(zip(keys, values))
        out.append({"config_id": f"g{index:02d}", "grid": point,
                    "run_config": _run_config(inputs["fixed"], point["threshold"], point["horizon_hours"])})
    return out


def baseline_configuration(spec: dict) -> dict:
    return _run_config(spec["inputs"]["fixed"], BASELINE["threshold"], BASELINE["horizon_hours"])


# --------------------------------------------------------------- evaluation
def run_window(frame: pd.DataFrame, run_config: dict, window: dict) -> tuple[dict, list[dict]]:
    """Engine run over ``[start, end)`` with gaps acknowledged (S08 rule)."""
    engine = Engine(build_run_config(run_config))
    ledger: list[dict] = []
    gaps = missing = 0
    for event in market_events(frame, window["start"], window["end"]):
        records = engine.step(event)
        if engine.status == "HALTED_GAP":
            ledger.extend(records)
            acknowledged = engine.acknowledge_gap(event.timestamp, note="E01: gap acknowledged (S08 rule)")
            gaps += 1
            missing += next(r["missing_hours"] for r in acknowledged if r["kind"] == "gap_acknowledged")
            ledger.extend(acknowledged)
            records = engine.step(event)
        ledger.extend(records)
    if not ledger:
        raise ExperimentError(f"no market events in window {window}")
    ledger.extend(engine.finish())
    return metrics(ledger, float(run_config["initial_cash"]), window, gaps, missing), ledger


def select(development: dict[str, dict], order: list[str]) -> tuple[str | None, str]:
    eligible = [c for c in order if development[c]["closed_trades"] >= SELECTION_MIN_TRADES]
    if not eligible:
        return None, f"no configuration with closed_trades >= {SELECTION_MIN_TRADES} on development"
    best = min(eligible, key=lambda c: (-development[c]["net_return_pct"], development[c]["closed_trades"],
                                        order.index(c)))
    return best, (f"highest net_return_pct among {len(eligible)} configurations with closed_trades >= "
                  f"{SELECTION_MIN_TRADES}; ties -> fewer closed trades -> grid order")


def _criterion(text: str, observed: Any, threshold: Any, passed: bool) -> dict:
    return {"criterion": text, "observed": observed, "threshold": threshold, "passed": bool(passed)}


def _finite(value: Any) -> bool:
    return value is not None and math.isfinite(float(value))


def validation_criteria(selected: dict, baseline: dict) -> list[dict]:
    """The five validation criteria of the spec, literally."""
    turnover_cap = TURNOVER_RATIO_MAX * baseline["closed_trades_per_year"]
    costs_cap = COSTS_SHARE_MAX * selected["gross_pnl"]
    return [
        _criterion("net_return_pct > 0", selected["net_return_pct"], 0,
                   _finite(selected["net_return_pct"]) and selected["net_return_pct"] > 0),
        _criterion("max_drawdown_mtm_pct >= -25", selected["max_drawdown_mtm_pct"], -25,
                   _finite(selected["max_drawdown_mtm_pct"]) and selected["max_drawdown_mtm_pct"] >= -25),
        _criterion("closed_trades >= 15", selected["closed_trades"], ACCEPTANCE_MIN_TRADES,
                   selected["closed_trades"] >= ACCEPTANCE_MIN_TRADES),
        _criterion("closed_trades_per_year <= 30% of the baseline on the same window",
                   selected["closed_trades_per_year"], turnover_cap,
                   selected["closed_trades_per_year"] <= turnover_cap),
        _criterion("costs <= 50% of the gross result (net + costs)", selected["costs"], costs_cap,
                   selected["costs"] <= costs_cap),
    ]


def confirmation_criteria(selected: dict) -> list[dict]:
    return [
        _criterion("net_return_pct > 0", selected["net_return_pct"], 0,
                   _finite(selected["net_return_pct"]) and selected["net_return_pct"] > 0),
        _criterion("max_drawdown_mtm_pct >= -25", selected["max_drawdown_mtm_pct"], -25,
                   _finite(selected["max_drawdown_mtm_pct"]) and selected["max_drawdown_mtm_pct"] >= -25),
    ]


def decide(validation: dict, confirmation: dict) -> tuple[str, str]:
    if validation["selected"]["closed_trades"] < ACCEPTANCE_MIN_TRADES:
        return "INCONCLUSIVE", f"validation closed_trades < {ACCEPTANCE_MIN_TRADES}"
    if not all(c["passed"] for c in validation["criteria"]):
        return "REJECTED", "at least one validation criterion failed"
    if not all(c["passed"] for c in confirmation["criteria"]):
        return "INCONCLUSIVE", "validation passed, at least one confirmation criterion failed"
    return "CANDIDATE", "all validation and confirmation criteria passed"


# --------------------------------------------------------------- phases
def _load_admitted(spec: dict, root: Path, catalog_path: str | Path) -> pd.DataFrame:
    catalog_file = Path(catalog_path) if Path(catalog_path).is_absolute() else root / catalog_path
    inputs = spec["inputs"]
    market = load_market(Catalog.load(catalog_file), inputs["dataset_id"], repo_root=root)
    if not market.source_manifest_sha256 or market.source_manifest_sha256 != inputs["source_manifest_sha256"]:
        raise ExperimentError(f"source manifest {market.source_manifest_sha256} differs from the preregistered "
                              f"{inputs['source_manifest_sha256']}")
    return market.frame


def _output(spec: dict, root: Path, output_dir: str | Path | None) -> Path:
    out = Path(output_dir) if output_dir is not None else Path("data/experiments") / spec["experiment_id"]
    return guard_research_output(out if out.is_absolute() else root / out, purpose="E01 experiment output")


def develop(spec_path: str | Path, *, repo_root: str | Path = ".", output_dir: str | Path | None = None,
            catalog_path: str | Path = "config/dataset-catalog.json") -> dict:
    """Phase 1: preregistration lock, development runs, selection lock."""
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
    window = spec["inputs"]["windows"]["development"]
    configurations = grid_configurations(spec)
    development = {}
    for configuration in configurations:
        development[configuration["config_id"]], ledger = run_window(frame, configuration["run_config"], window)
        _write_json(out / "ledgers" / f"development-{configuration['config_id']}.json", ledger)
    baseline, ledger = run_window(frame, baseline_configuration(spec), window)
    _write_json(out / "ledgers" / "development-baseline.json", ledger)
    selected, rule = select(development, [c["config_id"] for c in configurations])
    selection = {
        "experiment_id": spec["experiment_id"], "spec_sha256": spec_sha, "engine_sha256": engine_digests()[0],
        "selected": selected, "selected_grid": next((c["grid"] for c in configurations
                                                    if c["config_id"] == selected), None),
        "rule": rule, "configurations": {c["config_id"]: c["grid"] for c in configurations},
        "development": development, "baseline": baseline, "locked_utc": _now(),
        "note": "written before validation and confirmation are evaluated",
    }
    _write_json(out / "selection.json", selection)
    return selection


def evaluate(spec_path: str | Path, *, repo_root: str | Path = ".", output_dir: str | Path | None = None,
             catalog_path: str | Path = "config/dataset-catalog.json") -> dict:
    """Phase 2: one evaluation on validation and confirmation from the locked selection."""
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
        "engine_sha256": selection["engine_sha256"], "source_manifest_sha256": spec["inputs"]["source_manifest_sha256"],
        "configurations": {k: v["grid"] for k, v in configurations.items()},
        "baseline_configuration": BASELINE,
        "development": {"grid": selection["development"], "baseline": selection["baseline"]},
        "selection": {"selected": selection["selected"], "grid": selection["selected_grid"], "rule": selection["rule"],
                      "selection_sha256": _file_sha256(selection_path)},
    }
    if selection["selected"] is None:
        report.update(outcome="INCONCLUSIVE", outcome_reason=selection["rule"])
    else:
        frame = _load_admitted(spec, root, catalog_path)
        selected = configurations[selection["selected"]]
        for name in ("validation", "confirmation"):
            window = spec["inputs"]["windows"][name]
            result, ledger = run_window(frame, selected["run_config"], window)
            _write_json(out / "ledgers" / f"{name}-{selected['config_id']}.json", ledger)
            baseline, ledger = run_window(frame, baseline_configuration(spec), window)
            _write_json(out / "ledgers" / f"{name}-baseline.json", ledger)
            criteria = validation_criteria(result, baseline) if name == "validation" else confirmation_criteria(result)
            report[name] = {"selected": result, "baseline": baseline, "criteria": criteria,
                            "turnover_ratio_vs_baseline": (result["closed_trades_per_year"]
                                                           / baseline["closed_trades_per_year"]
                                                           if baseline["closed_trades_per_year"] else None)}
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
    """Every ledger digest of a report (CP-E01.1 reproducibility)."""
    out = {f"development-{k}": v["ledger_sha256"] for k, v in report["development"]["grid"].items()}
    out["development-baseline"] = report["development"]["baseline"]["ledger_sha256"]
    for name in ("validation", "confirmation"):
        if name in report:
            out[f"{name}-selected"] = report[name]["selected"]["ledger_sha256"]
            out[f"{name}-baseline"] = report[name]["baseline"]["ledger_sha256"]
    return out


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="E01 preregistered turnover-reduction experiment")
    parser.add_argument("phase", choices=("develop", "evaluate", "all"))
    parser.add_argument("--spec", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--catalog", default="config/dataset-catalog.json")
    args = parser.parse_args(argv)
    kwargs = dict(repo_root=args.repo_root, output_dir=args.output_dir, catalog_path=args.catalog)

    def brief(result):
        return {k: result[k] for k in ("net_return_pct", "max_drawdown_mtm_pct", "closed_trades",
                                       "closed_trades_per_year", "costs", "gross_pnl")}

    if args.phase in ("develop", "all"):
        selection = develop(args.spec, **kwargs)
        print(json.dumps({"selected": selection["selected"], "grid": selection["selected_grid"],
                          "rule": selection["rule"], "baseline": brief(selection["baseline"]),
                          "development": {k: brief(v) for k, v in selection["development"].items()}},
                         indent=2, default=str))
    if args.phase in ("evaluate", "all"):
        report = evaluate(args.spec, **kwargs)
        print(json.dumps({"outcome": report["outcome"], "reason": report["outcome_reason"],
                          **{w: {"selected": brief(report[w]["selected"]), "baseline": brief(report[w]["baseline"]),
                                 "criteria": [(c["criterion"], c["observed"], c["threshold"], c["passed"])
                                              for c in report[w]["criteria"]]}
                             for w in ("validation", "confirmation") if w in report}}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
