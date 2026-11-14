"""E02: preregistered experiment for the dynamic pattern-in-formation strategy.

Protocol
--------
1. **Preregistration.**  The spec must have ``status: PREREGISTERED``.  Its
   SHA-256 (line endings normalised to LF) is written to
   ``preregistration.json`` in a new output directory *before* any data is
   read.  Every later artefact cites that digest.
2. **Admitted data only.**  The market is loaded through the catalog
   (``load_market``) and its source manifest digest must equal the one in the
   spec.  Forward data is refused by the catalog and by the output guard.
3. **Selection on development only.**  Every grid configuration runs on the
   development window; the selection rule picks one and ``selection.json`` is
   written (write-once) before validation or confirmation are evaluated.
4. **One evaluation** of the selected configuration and of the baselines on
   validation and confirmation, same engine, costs and risk limits.
5. **Outcome** ``CANDIDATE`` / ``INCONCLUSIVE`` / ``REJECTED`` only from the
   registered criteria, each reported with threshold and observed value.

Gaps in the raw snapshot are acknowledged as in
:func:`crypto_decision_lab.release.evaluate_backtest`; the pattern detector
resets at each gap, so no pattern spans missing hours.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import itertools
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from . import studio_analytics as analytics
from .dataset_catalog import Catalog, load_market
from .decision_contract import utc
from .forward_isolation import guard_research_output
from .pattern_detector import DETECTOR_VERSION, DetectorParams, pattern_features
from .simulation import Engine, MarketEvent, engine_digests, market_events
from .studio import build_run_config

EXPERIMENT_SCHEMA = "cdl.e02.experiment.v1"
REPORT_SCHEMA = "cdl.e02.report.v1"
OUTCOMES = ("CANDIDATE", "INCONCLUSIVE", "REJECTED")
WINDOWS = ("development", "validation", "confirmation")
HOURS_PER_YEAR = 24 * 365.25
OPS = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b}


class ExperimentError(RuntimeError):
    """The spec or the data violate the preregistered protocol."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:  # write-once
        handle.write(json.dumps(payload, indent=2, sort_keys=True, allow_nan=False, default=str) + "\n")


# --------------------------------------------------------------- spec
def load_spec(spec_path: str | Path) -> tuple[dict, str]:
    path = Path(spec_path)
    spec = json.loads(path.read_text(encoding="utf-8-sig"))
    if spec.get("schema_version") != EXPERIMENT_SCHEMA:
        raise ExperimentError(f"unsupported spec schema: {spec.get('schema_version')}")
    if spec.get("status") != "PREREGISTERED":
        raise ExperimentError(f"spec status is {spec.get('status')!r}: only a PREREGISTERED spec can run")
    for key in ("experiment_id", "inputs", "baselines", "selection", "acceptance"):
        if key not in spec:
            raise ExperimentError(f"spec requires {key}")
    windows = spec["inputs"].get("windows", {})
    if tuple(windows) != WINDOWS:
        raise ExperimentError(f"windows must be exactly {WINDOWS} in order")
    bounds = [(utc(windows[w]["start"]), utc(windows[w]["end"])) for w in WINDOWS]
    if any(start >= end for start, end in bounds) or any(a[1] > b[0] for a, b in zip(bounds, bounds[1:])):
        raise ExperimentError("windows must be non-empty, ordered and non-overlapping")
    for name in spec["inputs"]["grid"]:
        if name.split(".", 1)[0] not in ("detector", "strategy"):
            raise ExperimentError(f"grid key must start with detector. or strategy.: {name}")
    return spec, _file_sha256(path)


def grid_configurations(spec: dict) -> list[dict]:
    """Cartesian product of the grid in declared key order; ``config_id`` is stable."""
    fixed = spec["inputs"]["fixed"]
    grid = spec["inputs"]["grid"]
    keys = list(grid)
    configurations = []
    for index, values in enumerate(itertools.product(*(grid[k] for k in keys))):
        detector = dict(fixed.get("detector", {}))
        strategy = dict(fixed.get("strategy", {}))
        for key, value in zip(keys, values):
            section, name = key.split(".", 1)
            (detector if section == "detector" else strategy)[name] = value
        params = DetectorParams(**detector)
        run_config = {"strategy": {"name": "pattern", "parameters": strategy},
                      "initial_cash": fixed["initial_cash"], "hypothesis": fixed["hypothesis"], "risk": fixed["risk"]}
        build_run_config(run_config)  # validate early
        configurations.append({"config_id": f"g{index:02d}", "grid": dict(zip(keys, values)),
                               "detector": detector, "detector_sha256": params.digest(), "run_config": run_config})
    return configurations


def baseline_configurations(spec: dict) -> dict[str, dict]:
    fixed = spec["inputs"]["fixed"]
    out = {}
    for name, baseline in spec["baselines"].items():
        hypothesis = {**fixed["hypothesis"], **baseline.get("hypothesis_override", {})}
        run_config = {"strategy": baseline["strategy"], "initial_cash": fixed["initial_cash"],
                      "hypothesis": hypothesis, "risk": fixed["risk"]}
        build_run_config(run_config)
        out[name] = run_config
    return out


# --------------------------------------------------------------- evaluation
def _exit_cause(reason: str) -> str:
    if reason == "holding horizon reached":
        return "horizon"
    if reason == "evaluation window closed":
        return "window"
    for cause in ("target", "invalidation", "age"):
        if f" {cause}" in reason:
            return cause
    return "strategy"


def exit_causes(ledger: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    decision = None
    for record in ledger:
        if record["kind"] == "decision":
            decision = record
        elif record["kind"] == "order" and record["side"] == "SELL" and decision is not None:
            cause = _exit_cause(decision["reason"])
            counts[cause] = counts.get(cause, 0) + 1
    return dict(sorted(counts.items()))


def run_window(frame: pd.DataFrame, run_config: dict, window: dict,
               features: dict | None = None) -> tuple[dict, list[dict]]:
    """Engine run over ``[start, end)``, gaps acknowledged (S08 rule), optional pattern features."""
    engine = Engine(build_run_config(run_config))
    ledger: list[dict] = []
    gaps = missing = 0
    for event in market_events(frame, window["start"], window["end"]):
        if features is not None:
            event = MarketEvent(event.timestamp, event.open, {**event.features, **features[utc(event.timestamp)]})
        records = engine.step(event)
        if engine.status == "HALTED_GAP":
            ledger.extend(records)
            acknowledged = engine.acknowledge_gap(event.timestamp, note="E02: gap acknowledged (S08 rule)")
            gaps += 1
            missing += next(r["missing_hours"] for r in acknowledged if r["kind"] == "gap_acknowledged")
            ledger.extend(acknowledged)
            records = engine.step(event)
        ledger.extend(records)
    if not ledger:
        raise ExperimentError(f"no market events in window {window}")
    ledger.extend(engine.finish())
    return metrics(ledger, float(run_config["initial_cash"]), window, gaps, missing), ledger


def metrics(ledger: list[dict], initial: float, window: dict, gaps: int = 0, missing: int = 0) -> dict:
    summary = analytics.summarize(ledger, initial)
    years = (utc(window["end"]) - utc(window["start"])).total_seconds() / 3600 / HOURS_PER_YEAR
    gross = summary["net_pnl"] + summary["costs"]
    return {
        "net_return_pct": summary["net_return_pct"],
        "net_pnl": summary["net_pnl"],
        "max_drawdown_mtm_pct": summary["max_drawdown_mtm_pct"],
        "closed_trades": summary["closed_trades"],
        "closed_trades_per_year": summary["closed_trades"] / years,
        "win_rate_pct": summary["win_rate_pct"],
        "costs": summary["costs"],
        "gross_pnl": gross,
        "costs_share_of_gross": summary["costs"] / gross if gross > 0 else None,
        "time_in_market_pct": summary["time_in_market_pct"],
        "exit_causes": exit_causes(ledger),
        "position_open": summary["position_open"],
        "gaps_acknowledged": gaps,
        "missing_hours": missing,
        "ledger_sha256": _sha(_canonical(ledger)),
    }


def select(spec: dict, development: dict[str, dict], order: list[str]) -> tuple[str | None, str]:
    rule = spec["selection"]
    minimum = int(rule["min_closed_trades"])
    eligible = [c for c in order if development[c]["closed_trades"] >= minimum]
    if not eligible:
        return None, f"no configuration with closed_trades >= {minimum} on development"
    # Highest net return; ties -> fewer closed trades -> grid order.
    best = min(eligible, key=lambda c: (-development[c]["net_return_pct"], development[c]["closed_trades"],
                                        order.index(c)))
    return best, (f"highest net_return_pct among {len(eligible)} configurations with closed_trades >= {minimum}; "
                  "ties -> fewer closed trades -> grid order")


def evaluate_criteria(criteria: list[dict], observed: dict[str, Any]) -> list[dict]:
    results = []
    for criterion in criteria:
        value = observed.get(criterion["metric"])
        passed = value is not None and math.isfinite(float(value)) and OPS[criterion["op"]](float(value),
                                                                                            float(criterion["value"]))
        results.append({**criterion, "observed": value, "passed": bool(passed)})
    return results


def decide(acceptance: dict, validation: dict, confirmation: dict) -> tuple[str, str]:
    minimum = int(acceptance["min_closed_trades"])
    if validation["selected"]["closed_trades"] < minimum:
        return "INCONCLUSIVE", f"validation closed_trades < {minimum}"
    if not all(c["passed"] for c in validation["criteria"]):
        return "REJECTED", "at least one validation criterion failed"
    if not all(c["passed"] for c in confirmation["criteria"]):
        return "INCONCLUSIVE", "validation passed, at least one confirmation criterion failed"
    return "CANDIDATE", "all validation and confirmation criteria passed"


def _observed(selected: dict, baselines: dict[str, dict]) -> dict[str, Any]:
    observed = dict(selected)
    for name, result in baselines.items():
        observed[f"excess_net_return_pct_vs_{name}"] = selected["net_return_pct"] - result["net_return_pct"]
    return observed


def evaluate(spec: dict, spec_sha: str, frame: pd.DataFrame, out: Path) -> dict:
    """Steps 3-5 on an already admitted market frame. ``out`` holds the preregistration."""
    windows = spec["inputs"]["windows"]
    configurations = grid_configurations(spec)
    baselines = baseline_configurations(spec)
    features_by_detector: dict[str, dict] = {}

    def features_for(configuration):
        digest = configuration["detector_sha256"]
        if digest not in features_by_detector:
            features_by_detector[digest] = pattern_features(frame, DetectorParams(**configuration["detector"]))
        return features_by_detector[digest]

    def save(name, ledger):
        _write_json(out / "ledgers" / f"{name}.json", ledger)

    # 3. development: grid and baselines, then selection lock.
    development: dict[str, dict] = {}
    for configuration in configurations:
        result, ledger = run_window(frame, configuration["run_config"], windows["development"],
                                    features_for(configuration))
        development[configuration["config_id"]] = result
        save(f"development-{configuration['config_id']}", ledger)
    development_baselines = {}
    for name, run_config in baselines.items():
        development_baselines[name], ledger = run_window(frame, run_config, windows["development"])
        save(f"development-baseline-{name}", ledger)
    order = [c["config_id"] for c in configurations]
    selected_id, rule = select(spec, development, order)
    _write_json(out / "selection.json", {
        "spec_sha256": spec_sha, "selected": selected_id, "rule": rule, "development": development,
        "baselines": development_baselines, "locked_utc": _now(),
        "note": "written before validation and confirmation are evaluated",
    })
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA, "experiment_id": spec["experiment_id"], "spec_sha256": spec_sha,
        "detector_version": DETECTOR_VERSION, "engine_sha256": engine_digests()[0],
        "configurations": configurations, "development": {"grid": development, "baselines": development_baselines},
        "selection": {"selected": selected_id, "rule": rule},
    }
    if selected_id is None:
        report.update(outcome="INCONCLUSIVE", outcome_reason=rule)
        return report
    selected = next(c for c in configurations if c["config_id"] == selected_id)

    # 4. one evaluation on validation and confirmation.
    for window_name in ("validation", "confirmation"):
        window = windows[window_name]
        result, ledger = run_window(frame, selected["run_config"], window, features_for(selected))
        save(f"{window_name}-{selected_id}", ledger)
        window_baselines = {}
        for name, run_config in baselines.items():
            window_baselines[name], ledger = run_window(frame, run_config, window)
            save(f"{window_name}-baseline-{name}", ledger)
        criteria = evaluate_criteria(spec["acceptance"][window_name], _observed(result, window_baselines))
        report[window_name] = {"selected": result, "baselines": window_baselines, "criteria": criteria}

    # 5. outcome from the registered criteria only.
    outcome, reason = decide(spec["acceptance"], report["validation"], report["confirmation"])
    report.update(outcome=outcome, outcome_reason=reason)
    return report


# --------------------------------------------------------------- runner
def run_experiment(spec_path: str | Path, *, repo_root: str | Path = ".", output_dir: str | Path | None = None,
                   catalog_path: str | Path = "config/dataset-catalog.json") -> dict:
    root = Path(repo_root).resolve()
    spec, spec_sha = load_spec(spec_path)
    out = Path(output_dir) if output_dir is not None else Path("data/experiments") / spec["experiment_id"]
    out = guard_research_output(out if out.is_absolute() else root / out, purpose="E02 experiment output")
    if out.exists():
        raise FileExistsError(f"experiment output exists; refusing to overwrite: {out}")
    out.mkdir(parents=True)
    # 1. Preregistration lock: written before any data access.
    _write_json(out / "preregistration.json", {
        "experiment_id": spec["experiment_id"], "spec_sha256": spec_sha, "spec": spec,
        "locked_utc": _now(), "note": "written before reading data",
    })
    # 2. Admitted data only.
    catalog_file = Path(catalog_path) if Path(catalog_path).is_absolute() else root / catalog_path
    inputs = spec["inputs"]
    market = load_market(Catalog.load(catalog_file), inputs["dataset_id"], repo_root=root)
    if not market.source_manifest_sha256 or market.source_manifest_sha256 != inputs["source_manifest_sha256"]:
        raise ExperimentError(f"source manifest {market.source_manifest_sha256} differs from the preregistered "
                              f"{inputs['source_manifest_sha256']}")
    report = evaluate(spec, spec_sha, market.frame, out)
    report["source_manifest_sha256"] = market.source_manifest_sha256
    _write_json(out / "report.json", report)
    files = sorted(p for p in out.rglob("*") if p.is_file())
    _write_json(out / "manifest.json", {
        "experiment_id": spec["experiment_id"], "spec_sha256": spec_sha, "created_utc": _now(),
        "files": {p.relative_to(out).as_posix(): _file_sha256(p) for p in files},
    })
    return report


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="E02 preregistered pattern-in-formation experiment")
    parser.add_argument("--spec", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--catalog", default="config/dataset-catalog.json")
    args = parser.parse_args(argv)
    report = run_experiment(args.spec, repo_root=args.repo_root, output_dir=args.output_dir,
                            catalog_path=args.catalog)

    def brief(result):
        return {k: result[k] for k in ("net_return_pct", "max_drawdown_mtm_pct", "closed_trades", "costs")}

    print(json.dumps({
        "outcome": report["outcome"], "reason": report["outcome_reason"],
        "selected": report["selection"]["selected"],
        **{w: {"selected": brief(report[w]["selected"]),
               "baselines": {n: brief(b) for n, b in report[w]["baselines"].items()},
               "criteria": [(c["metric"], c["op"], c["value"], c["observed"], c["passed"]) for c in report[w]["criteria"]]}
           for w in ("validation", "confirmation") if w in report},
    }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
