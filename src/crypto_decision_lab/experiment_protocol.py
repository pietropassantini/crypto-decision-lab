"""Generic two-phase preregistered experiment protocol (E04 onwards).

Same protocol and acceptance criteria as E01 and E03, factored out so a new
experiment only declares an :class:`Adapter` (its grid, features and exit
classification):

1. ``develop``: the spec must be ``PREREGISTERED``; ``preregistration.json``
   is written before any data is read; the grid and the baselines run on the
   development window; selection (``closed_trades >= 30``, highest net return,
   ties -> fewer trades -> grid order); write-once ``selection.json``, to be
   committed before phase 2.
2. ``evaluate``: requires that ``selection.json`` (same spec digest, same
   engine); one evaluation of the selected configuration and of the baselines
   on validation and confirmation; the E01 criteria against the turnover
   baseline; outcome CANDIDATE / INCONCLUSIVE / REJECTED; ``report.json`` and
   ``manifest.json``.

Gaps are acknowledged as in ``release.evaluate_backtest``.  The E01 and E03
runners keep their own modules and are not routed through this one.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from .dataset_catalog import Catalog, load_market
from .decision_contract import utc
from .forward_isolation import guard_research_output
from .pattern_formation_experiment import ExperimentError, _file_sha256, _now, _write_json, metrics
from .simulation import Engine, MarketEvent, engine_digests, market_events
from .studio import build_run_config
from .turnover_experiment import confirmation_criteria, decide, select, validation_criteria

WINDOWS = ("development", "validation", "confirmation")
OUTCOMES = ("CANDIDATE", "INCONCLUSIVE", "REJECTED")


@dataclass(frozen=True)
class Adapter:
    """What one experiment adds to the shared protocol."""

    label: str                                   # short id used in notes and messages, e.g. "E04"
    schema: str                                  # spec schema_version
    report_schema: str
    configurations: Callable[[dict], list[dict]]  # spec -> [{config_id, grid, run_config, feature_key}]
    features: Callable[[pd.DataFrame, dict, Any], dict | None]  # (frame, spec, feature_key) -> per-timestamp features
    exit_cause: Callable[[str], str]             # decision reason of a closing order -> cause label
    turnover_baseline: str = "momentum_v1"
    validate: Callable[[dict], None] | None = None
    # Minimum closed trades; None keeps the E01 values (30 for selection, 15 for acceptance).
    selection_min_trades: int | None = None
    acceptance_min_trades: int | None = None


def load_spec(adapter: Adapter, spec_path: str | Path) -> tuple[dict, str]:
    path = Path(spec_path)
    spec = json.loads(path.read_text(encoding="utf-8-sig"))
    if spec.get("schema_version") != adapter.schema:
        raise ExperimentError(f"unsupported spec schema: {spec.get('schema_version')}")
    if spec.get("status") != "PREREGISTERED":
        raise ExperimentError(f"spec status is {spec.get('status')!r}: only a PREREGISTERED spec can run")
    inputs = spec.get("inputs", {})
    for key in ("dataset_id", "source_manifest_sha256", "fixed", "grid", "windows"):
        if key not in inputs:
            raise ExperimentError(f"spec inputs require {key}")
    if adapter.turnover_baseline not in spec.get("baselines", {}):
        raise ExperimentError(f"spec baselines require {adapter.turnover_baseline} (turnover criterion)")
    windows = inputs["windows"]
    if tuple(windows) != WINDOWS:
        raise ExperimentError(f"windows must be exactly {WINDOWS} in order")
    bounds = [(utc(windows[w]["start"]), utc(windows[w]["end"])) for w in WINDOWS]
    if any(start >= end for start, end in bounds) or any(a[1] > b[0] for a, b in zip(bounds, bounds[1:])):
        raise ExperimentError("windows must be non-empty, ordered and non-overlapping")
    if adapter.validate is not None:
        adapter.validate(spec)
    return spec, _file_sha256(path)


def _select(adapter: Adapter, development: dict[str, dict], order: list[str]) -> tuple[str | None, str]:
    if adapter.selection_min_trades is None:
        return select(development, order)
    minimum = int(adapter.selection_min_trades)
    eligible = [c for c in order if development[c]["closed_trades"] >= minimum]
    if not eligible:
        return None, f"no configuration with closed_trades >= {minimum} on development"
    best = min(eligible, key=lambda c: (-development[c]["net_return_pct"], development[c]["closed_trades"],
                                        order.index(c)))
    return best, (f"highest net_return_pct among {len(eligible)} configurations with closed_trades >= {minimum}; "
                  "ties -> fewer closed trades -> grid order")


def _validation_criteria(adapter: Adapter, selected: dict, baseline: dict) -> list[dict]:
    criteria = validation_criteria(selected, baseline)
    if adapter.acceptance_min_trades is not None:
        minimum = int(adapter.acceptance_min_trades)
        index = next(i for i, c in enumerate(criteria) if c["criterion"].startswith("closed_trades >="))
        criteria[index] = {"criterion": f"closed_trades >= {minimum}", "observed": selected["closed_trades"],
                           "threshold": minimum, "passed": selected["closed_trades"] >= minimum}
    return criteria


def _decide(adapter: Adapter, validation: dict, confirmation: dict) -> tuple[str, str]:
    if adapter.acceptance_min_trades is None:
        return decide(validation, confirmation)
    minimum = int(adapter.acceptance_min_trades)
    if validation["selected"]["closed_trades"] < minimum:
        return "INCONCLUSIVE", f"validation closed_trades < {minimum}"
    if not all(c["passed"] for c in validation["criteria"]):
        return "REJECTED", "at least one validation criterion failed"
    if not all(c["passed"] for c in confirmation["criteria"]):
        return "INCONCLUSIVE", "validation passed, at least one confirmation criterion failed"
    return "CANDIDATE", "all validation and confirmation criteria passed"


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


def run_window(frame: pd.DataFrame, run_config: dict, window: dict, features: dict | None = None,
               label: str = "experiment") -> tuple[dict, list[dict]]:
    engine = Engine(build_run_config(run_config))
    ledger: list[dict] = []
    gaps = missing = 0
    for event in market_events(frame, window["start"], window["end"]):
        if features is not None:
            event = MarketEvent(event.timestamp, event.open, {**event.features, **features[utc(event.timestamp)]})
        records = engine.step(event)
        if engine.status == "HALTED_GAP":
            ledger.extend(records)
            acknowledged = engine.acknowledge_gap(event.timestamp, note=f"{label}: gap acknowledged (S08 rule)")
            gaps += 1
            missing += next(r["missing_hours"] for r in acknowledged if r["kind"] == "gap_acknowledged")
            ledger.extend(acknowledged)
            records = engine.step(event)
        ledger.extend(records)
    if not ledger:
        raise ExperimentError(f"no market events in window {window}")
    ledger.extend(engine.finish())
    return metrics(ledger, float(run_config["initial_cash"]), window, gaps, missing), ledger


def exit_reasons(ledger: list[dict], classify: Callable[[str], str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    decision = None
    for record in ledger:
        if record["kind"] == "decision":
            decision = record
        elif record["kind"] == "order" and record["side"] == "SELL" and decision is not None:
            reason = decision["reason"]
            cause = ("horizon" if reason == "holding horizon reached" else
                     "window" if reason == "evaluation window closed" else classify(reason))
            counts[cause] = counts.get(cause, 0) + 1
    return dict(sorted(counts.items()))


class _Session:
    """Admitted frame plus cached features for one run."""

    def __init__(self, adapter: Adapter, spec: dict, root: Path, catalog_path: str | Path) -> None:
        catalog_file = Path(catalog_path) if Path(catalog_path).is_absolute() else root / catalog_path
        inputs = spec["inputs"]
        market = load_market(Catalog.load(catalog_file), inputs["dataset_id"], repo_root=root)
        if not market.source_manifest_sha256 or market.source_manifest_sha256 != inputs["source_manifest_sha256"]:
            raise ExperimentError(f"source manifest {market.source_manifest_sha256} differs from the preregistered "
                                  f"{inputs['source_manifest_sha256']}")
        self.adapter, self.spec, self.frame = adapter, spec, market.frame
        self._cache: dict[Any, dict | None] = {}

    def run(self, run_config: dict, window: dict, feature_key: Any = None) -> tuple[dict, list[dict]]:
        features = None
        if feature_key is not None:
            if feature_key not in self._cache:
                self._cache[feature_key] = self.adapter.features(self.frame, self.spec, feature_key)
            features = self._cache[feature_key]
        result, ledger = run_window(self.frame, run_config, window, features, self.adapter.label)
        result["exit_reasons"] = exit_reasons(ledger, self.adapter.exit_cause)
        return result, ledger


def _output(adapter: Adapter, spec: dict, root: Path, output_dir: str | Path | None) -> Path:
    out = Path(output_dir) if output_dir is not None else Path("data/experiments") / spec["experiment_id"]
    return guard_research_output(out if out.is_absolute() else root / out,
                                 purpose=f"{adapter.label} experiment output")


def develop(adapter: Adapter, spec_path: str | Path, *, repo_root: str | Path = ".",
            output_dir: str | Path | None = None, catalog_path: str | Path = "config/dataset-catalog.json") -> dict:
    root = Path(repo_root).resolve()
    spec, spec_sha = load_spec(adapter, spec_path)
    out = _output(adapter, spec, root, output_dir)
    if out.exists():
        raise FileExistsError(f"experiment output exists; refusing to overwrite: {out}")
    out.mkdir(parents=True)
    _write_json(out / "preregistration.json", {
        "experiment_id": spec["experiment_id"], "spec_sha256": spec_sha, "spec": spec,
        "locked_utc": _now(), "note": "written before reading data",
    })
    session = _Session(adapter, spec, root, catalog_path)
    window = spec["inputs"]["windows"]["development"]
    configurations = adapter.configurations(spec)
    development = {}
    for configuration in configurations:
        development[configuration["config_id"]], ledger = session.run(
            configuration["run_config"], window, configuration["feature_key"])
        _write_json(out / "ledgers" / f"development-{configuration['config_id']}.json", ledger)
    baselines = {}
    for name, run_config in baseline_configurations(spec).items():
        baselines[name], ledger = session.run(run_config, window)
        _write_json(out / "ledgers" / f"development-baseline-{name}.json", ledger)
    selected, rule = _select(adapter, development, [c["config_id"] for c in configurations])
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


def evaluate(adapter: Adapter, spec_path: str | Path, *, repo_root: str | Path = ".",
             output_dir: str | Path | None = None, catalog_path: str | Path = "config/dataset-catalog.json") -> dict:
    root = Path(repo_root).resolve()
    spec, spec_sha = load_spec(adapter, spec_path)
    out = _output(adapter, spec, root, output_dir)
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
    configurations = {c["config_id"]: c for c in adapter.configurations(spec)}
    report: dict[str, Any] = {
        "schema_version": adapter.report_schema, "experiment_id": spec["experiment_id"], "spec_sha256": spec_sha,
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
        session = _Session(adapter, spec, root, catalog_path)
        selected = configurations[selection["selected"]]
        for name in ("validation", "confirmation"):
            window = spec["inputs"]["windows"][name]
            result, ledger = session.run(selected["run_config"], window, selected["feature_key"])
            _write_json(out / "ledgers" / f"{name}-{selected['config_id']}.json", ledger)
            baselines = {}
            for baseline_name, run_config in baseline_configurations(spec).items():
                baselines[baseline_name], ledger = session.run(run_config, window)
                _write_json(out / "ledgers" / f"{name}-baseline-{baseline_name}.json", ledger)
            reference = baselines[adapter.turnover_baseline]
            criteria = (_validation_criteria(adapter, result, reference) if name == "validation"
                        else confirmation_criteria(result))
            report[name] = {"selected": result, "baselines": baselines, "criteria": criteria,
                            "turnover_ratio_vs_baseline": (result["closed_trades_per_year"]
                                                           / reference["closed_trades_per_year"]
                                                           if reference["closed_trades_per_year"] else None)}
        outcome, reason = _decide(adapter, report["validation"], report["confirmation"])
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


def cli(adapter: Adapter, description: str, argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("phase", choices=("develop", "evaluate", "all"))
    parser.add_argument("--spec", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--catalog", default="config/dataset-catalog.json")
    args = parser.parse_args(argv)
    kwargs = dict(repo_root=args.repo_root, output_dir=args.output_dir, catalog_path=args.catalog)

    def brief(result):
        return {k: result[k] for k in ("net_return_pct", "max_drawdown_mtm_pct", "closed_trades",
                                       "closed_trades_per_year", "costs", "gross_pnl", "time_in_market_pct",
                                       "exit_reasons")}

    if args.phase in ("develop", "all"):
        selection = develop(adapter, args.spec, **kwargs)
        print(json.dumps({"selected": selection["selected"], "grid": selection["selected_grid"],
                          "rule": selection["rule"],
                          "baselines": {k: brief(v) for k, v in selection["baselines"].items()},
                          "development": {k: brief(v) for k, v in selection["development"].items()}},
                         indent=2, default=str))
    if args.phase in ("evaluate", "all"):
        report = evaluate(adapter, args.spec, **kwargs)
        print(json.dumps({"outcome": report["outcome"], "reason": report["outcome_reason"],
                          **{w: {"selected": brief(report[w]["selected"]),
                                 "baselines": {k: brief(v) for k, v in report[w]["baselines"].items()},
                                 "criteria": [(c["criterion"], c["observed"], c["threshold"], c["passed"])
                                              for c in report[w]["criteria"]]}
                             for w in ("validation", "confirmation") if w in report}}, indent=2, default=str))
    return 0
