"""S08 release and controlled activation.

Flow (each artefact is write-once and identified by SHA-256)::

    releases/<candidate>/criteria.json            1. criteria registered BEFORE any evaluation
    releases/<candidate>/<code8>/drill.json        2. runbook drill in collaudo (clean install)
    releases/<candidate>/<code8>/release-card.json 3. version, evidence, checks, READY | BLOCKED
    releases/<candidate>/<code8>/approval.json     4. operator approval (READY cards only)
    releases/<candidate>/<code8>/decisions.jsonl   5. ACTIVATED | NOT_ACTIVATED, append-only

* The **criteria** pin the candidate live configuration (strategy, capital,
  limits, exchange) by digest, the evaluation windows and the thresholds.
* The **card** records the exact version (git commit + dirty paths, package
  version, checkout-independent code digest), backtest and paper evidence on
  the registered windows, the S07 testnet evidence, the drill, the PRD
  backlog status and every check with observed value.  Any failed check makes
  the card ``BLOCKED`` with the reasons.
* **Activation** on mainnet is possible only through
  :func:`verify_activation`: the approval must reference a READY card that
  passes :func:`validate_card` (bound to its registered criteria: digest,
  configuration, target, full check set with registered thresholds, decision
  consistent with the checks) and match the session's configuration digest,
  exchange, environment and code digest.  :class:`~.live.LiveSession` calls it for every mainnet session.
"""
from __future__ import annotations

from datetime import datetime, timezone
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

CRITERIA_SCHEMA = "cdl.s08.criteria.v1"
CARD_SCHEMA = "cdl.s08.release-card.v1"
APPROVAL_SCHEMA = "cdl.s08.approval.v1"
DECISION_SCHEMA = "cdl.s08.decision.v1"
DRILL_SCHEMA = "cdl.s08.drill.v1"
DEFAULT_RELEASES = Path("releases")
OPS = {">": lambda a, b: a > b, ">=": lambda a, b: a >= b, "<": lambda a, b: a < b, "<=": lambda a, b: a <= b,
       "==": lambda a, b: a == b}
CODE_PATHS = ("src", "config", "pyproject.toml")


def _last_hour(end: Any) -> str:
    """Last hourly bar of a window ``[start, end)``, formatted like ledger timestamps."""
    import pandas as pd

    stamp = pd.Timestamp(end)
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    return str(stamp - pd.Timedelta(hours=1))


class ReleaseError(RuntimeError):
    """Invalid release operation."""


class ActivationError(ReleaseError):
    """The live configuration does not match an approved READY release."""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _write_once(path: Path, payload: dict) -> str:
    path = Path(path)
    if path.exists():
        raise FileExistsError(f"write-once artefact exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return file_sha256(path)


def _read(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def live_config_sha256(raw_live_config: dict) -> str:
    from .live import normalize_live_config

    return sha256_text(_canonical(normalize_live_config(raw_live_config)))


# ====================================================================== 1. criteria
def register_criteria(criteria_path: str | Path, *, repo_root: str | Path = ".",
                      releases: str | Path = DEFAULT_RELEASES) -> Path:
    """Copy the criteria into the release registry (write-once) with the candidate digest."""
    repo = Path(repo_root).resolve()
    criteria = _read(criteria_path)
    if criteria.get("schema_version") != CRITERIA_SCHEMA:
        raise ReleaseError("unsupported criteria schema")
    for key in ("candidate", "candidate_config", "windows", "backtest", "paper", "operational"):
        if key not in criteria:
            raise ReleaseError(f"criteria require {key}")
    for check in criteria["backtest"] + criteria["paper"]:
        if check.get("op") not in OPS:
            raise ReleaseError(f"unknown operator in {check}")
    config_path = repo / criteria["candidate_config"]
    live_config = _read(config_path)
    registered = {**criteria, "registered_utc": _now(),
                  "candidate_live_config": live_config,
                  "candidate_live_config_sha256": live_config_sha256(live_config)}
    target = _registry(repo, releases) / criteria["candidate"] / "criteria.json"
    _write_once(target, registered)
    return target


def _registry(repo: Path, releases: str | Path) -> Path:
    base = Path(releases)
    return base if base.is_absolute() else repo / base


# ====================================================================== 2. evidence
def git_state(repo: Path) -> dict:
    def git(*args):
        result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else None

    top = git("rev-parse", "--show-toplevel")
    if top is None or Path(top).resolve() != Path(repo).resolve():  # not the root of its own checkout
        return {"commit": None, "dirty_paths": [], "available": False}
    head = git("rev-parse", "HEAD")
    dirty = git("status", "--porcelain", "--", *CODE_PATHS)
    return {"commit": head, "dirty_paths": [line[3:] for line in (dirty or "").splitlines() if line.strip()],
            "available": head is not None}


def prd_status(prd_path: Path) -> dict[str, str]:
    statuses = {}
    for line in Path(prd_path).read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\| (S\d\dA?|A00) \| ([A-Z_]+) \|", line)
        if match:
            statuses[match.group(1)] = match.group(2)
    return statuses


def _summary(ledger: list[dict], initial: float) -> dict:
    from . import studio_analytics as analytics

    keys = ("status", "events", "first_event", "last_event", "final_equity", "net_return_pct",
            "max_drawdown_mtm_pct", "fees", "slippage", "costs", "trades", "closed_trades", "win_rate_pct",
            "time_in_market_pct", "position_open")
    summary = analytics.summarize(ledger, initial)
    return {key: summary.get(key) for key in keys}


def evaluate_backtest(repo: Path, run_config: dict, window: dict) -> tuple[dict, list[dict]]:
    """Backtest over the whole window.

    The raw snapshot has missing hours: :func:`~.simulation.run` halts at the
    first one.  Here each gap is acknowledged like an operator does in a paper
    session (S06 rule: a pending order is cancelled, no price is invented, the
    position is valued only after the gap) and the run continues; the number
    of acknowledged gaps and missing hours is part of the evidence.
    """
    from .dataset_catalog import Catalog, load_market
    from .simulation import Engine, market_events
    from .studio import build_run_config

    market = load_market(Catalog.load(repo / "config/dataset-catalog.json"), window["dataset_id"], repo_root=repo)
    engine = Engine(build_run_config(run_config))
    ledger: list[dict] = []
    gaps = missing = 0
    for event in market_events(market.frame, window["start"], window["end"]):
        records = engine.step(event)
        if engine.status == "HALTED_GAP":
            ledger.extend(records)
            acknowledged = engine.acknowledge_gap(event.timestamp, note="backtest: gap acknowledged (S06 rule)")
            gaps += 1
            missing += next(r["missing_hours"] for r in acknowledged if r["kind"] == "gap_acknowledged")
            ledger.extend(acknowledged)
            records = engine.step(event)
        ledger.extend(records)
    ledger.extend(engine.finish())
    manifest = engine.manifest(mode="backtest", source_id=window["dataset_id"])
    evidence = {"window": window, "source_manifest_sha256": market.source_manifest_sha256,
                "ledger_sha256": sha256_text(_canonical(ledger)), "engine_sha256": manifest["engine_sha256"],
                "config_digest": manifest["config_digest"], "gaps_acknowledged": gaps, "missing_hours": missing,
                **_summary(ledger, float(run_config["initial_cash"]))}
    return evidence, ledger


def evaluate_paper(repo: Path, run_config: dict, window: dict, root: Path, name: str) -> dict:
    """Persistent paper session (S06) replaying the registered paper window."""
    from .paper import PaperSession, _slug, list_sessions

    source = {"kind": "snapshot", "dataset_id": window["dataset_id"]}
    try:
        session = PaperSession.create(name, run_config, source, start_at=window["start"], end_at=window["end"],
                                      root=root, repo_root=repo)
    except FileExistsError:  # same config, source and window: resume the existing session
        session = next(PaperSession(path, repo_root=repo) for path in list_sessions(root, repo_root=repo)
                       if path.name.startswith(_slug(name) + "-"))
    snapshot = session.poll()
    ledger = session.ledger()
    backtest_evidence, backtest_ledger = evaluate_backtest(repo, run_config, window)
    evidence = {"window": window, "session_id": snapshot["session_id"], "session_status": snapshot["status"],
                "mode": snapshot["mode"], "ledger_sha256": sha256_text(_canonical(ledger)),
                "matches_backtest": ledger == backtest_ledger,
                "status_log": [s["status"] for s in session.status_log()],
                **_summary(ledger, float(run_config["initial_cash"]))}
    session.close()
    return evidence


def testnet_evidence(repo: Path, patterns: list[str]) -> dict:
    files = sorted({path for pattern in patterns for path in glob.glob(str(repo / pattern))})
    runs = []
    for path in files:
        payload = _read(Path(path))
        runs.append({"path": str(Path(path).relative_to(repo)), "sha256": file_sha256(Path(path)),
                     "started_utc": payload.get("started_utc"),
                     "environment": payload.get("identity", {}).get("environment"),
                     "all_ok": bool(payload.get("steps")) and all(step.get("ok") for step in payload["steps"])})
    return {"runs": runs, "passed": any(r["all_ok"] and r["environment"] == "testnet" for r in runs)}


# ====================================================================== 2b. runbook drill (CP08.1)
DRILL_STRATEGY = {"name": "constant", "parameters": {"exposure": "LONG"}}


def run_drill(repo_root: str | Path, workdir: str | Path, *, live_config: dict, python: str = sys.executable,
              dataset_id: str = "BTCUSDT-1h-raw-v1", feed_start: str = "2024-03-01 00:00",
              feed_end: str = "2024-03-05 00:00", candidate_hours: int = 24, env: dict | None = None) -> dict:
    """Repeat the runbook in collaudo through the installed CLIs (fixture exchange, lab feed).

    Part A follows start, crash after send, resume, suspension, balance
    mismatch, rebaseline, flatten and finish with a constant strategy so that
    orders are certain; part B a resting LIMIT order across a restart; part C
    the candidate configuration unchanged over the whole feed.
    """
    import pandas as pd

    from .exchange import FakeExchange
    from .lab_feed import FEED_SCHEMA

    repo = Path(repo_root).resolve()
    work = Path(workdir).resolve()
    if work.exists():
        raise FileExistsError(f"drill workdir exists: {work}")
    work.mkdir(parents=True)
    steps: list[dict] = []
    base_env = {**os.environ, **(env or {})}
    base_env.pop("CDL_LIVE_CRASH", None)
    start = pd.Timestamp(feed_start, tz="UTC") + pd.Timedelta(hours=26)  # warmup for momentum_24h

    def cli(module, *args, extra_env=None):
        command = [python, "-m", f"crypto_decision_lab.{module}", "--repo-root", str(repo), *args]
        result = subprocess.run(command, capture_output=True, text=True, cwd=work,
                                env={**base_env, **(extra_env or {})})
        return result

    def record(step_id, procedure, expected, ok, observed):
        steps.append({"id": step_id, "procedure": procedure, "expected": expected, "observed": observed,
                      "result": "PASS" if ok else "FAIL"})
        return ok

    def status(session_dir):
        result = cli("live", "session-status", "--session-dir", session_dir)
        return json.loads(result.stdout) if result.returncode == 0 else {"error": result.stderr[-400:]}

    probe = subprocess.run([python, "-c", "import crypto_decision_lab, sys; print(crypto_decision_lab.__file__)"],
                           capture_output=True, text=True, cwd=work, env=base_env)
    location = probe.stdout.strip()
    record("install", f"{python} -c 'import crypto_decision_lab'", "package importable from the tested installation",
           probe.returncode == 0, location or probe.stderr[-300:])

    def make_part(name, config, balances):
        feed_dir = work / f"feed-{name}"
        scenario = work / f"scenario-{name}.json"
        scenario.write_text(json.dumps({"schema_version": FEED_SCHEMA, "feed_id": f"drill-{name}",
                                        "source_dataset_id": dataset_id, "start": feed_start, "end": feed_end,
                                        "faults": []}), encoding="utf-8")
        created = cli("paper", "feed-create", "--scenario", str(scenario), "--feed-dir", str(feed_dir))
        cli("paper", "feed-advance", "--feed-dir", str(feed_dir), "--until", str(start - pd.Timedelta(hours=1)))
        fake = FakeExchange(work / f"exchange-{name}.json", balances=balances, price=1)
        config_path = work / f"live-{name}.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        session = cli("live", "session-create", "--name", f"drill-{name}", "--config", str(config_path),
                      "--exchange", "fake", "--fake-state", str(fake.path), "--feed-dir", str(feed_dir),
                      "--start", str(start), "--root", str(work / "live"))
        ok = created.returncode == 0 and session.returncode == 0
        session_dir = json.loads(session.stdout)["session_dir"] if ok else ""
        record(f"{name}.start", "paper feed-create; live session-create (fixture exchange, lab feed)",
               "feed and live session created; status CREATED", ok and status(session_dir).get("status") == "CREATED",
               (session.stderr or created.stderr)[-300:] if not ok else status(session_dir).get("status"))
        return feed_dir, fake, session_dir

    def advance(feed_dir, hours):
        cli("paper", "feed-advance", "--feed-dir", str(feed_dir), "--hours", str(hours))

    capital = float(live_config["run_config"]["initial_cash"])
    balances = {"BTC": "0", "USDT": str(capital * 4)}

    # ------------------------------------------------------------ part A
    ops_config = {**live_config, "run_config": {**live_config["run_config"], "strategy": DRILL_STRATEGY}}
    feed_a, fake_a, dir_a = make_part("a", ops_config, balances)
    advance(feed_a, 1)
    killed = cli("live", "session-run", "--session-dir", dir_a, extra_env={"CDL_LIVE_CRASH": "after_send"})
    record("a.crash_after_send", "session-run with CDL_LIVE_CRASH=after_send", "runner killed (exit 97) after sending",
           killed.returncode == 97, f"exit {killed.returncode}")
    resumed = cli("live", "session-run", "--session-dir", dir_a)
    snap = status(dir_a)
    submits = FakeExchange(fake_a.path).state.calls.count("submit")
    record("a.resume_reconcile", "session-run after the crash", "order reconciled FILLED, one submit, WAITING",
           resumed.returncode == 0 and snap.get("status") == "WAITING" and submits == 1 and snap.get("fills", 0) >= 1,
           f"status={snap.get('status')} submits={submits} fills={snap.get('fills')} position={snap.get('position')}")
    cli("live", "suspend-openings", "--session-dir", dir_a, "--note", "drill suspension")
    horizon = int(live_config["run_config"]["hypothesis"]["horizon_hours"])
    advance(feed_a, horizon + 1)
    cli("live", "session-run", "--session-dir", dir_a)
    snap = status(dir_a)
    sides = [o["side"] for o in FakeExchange(fake_a.path).state.orders.values()]
    record("a.suspension", "suspend-openings; advance past the horizon; session-run",
           "status OPENINGS_SUSPENDED, horizon exit executed, no reopening",
           snap.get("status") == "OPENINGS_SUSPENDED" and sides == ["BUY", "SELL"],
           f"status={snap.get('status')} exchange orders={sides}")
    cli("live", "resume-openings", "--session-dir", dir_a, "--note", "drill resume")
    advance(feed_a, 1)
    cli("live", "session-run", "--session-dir", dir_a)
    fake_a = FakeExchange(fake_a.path)
    fake_a.external_transfer("USDT", "-25")
    advance(feed_a, 1)
    cli("live", "session-run", "--session-dir", dir_a)
    snap = status(dir_a)
    record("a.balance_mismatch", "external transfer of -25 USDT; session-run", "status HALTED with balance mismatch",
           snap.get("status") == "HALTED" and "USDT expected" in str(snap.get("halted")),
           f"status={snap.get('status')} halted={snap.get('halted')}")
    rebased = cli("live", "rebaseline", "--session-dir", dir_a, "--note", "drill: 25 USDT withdrawn on purpose")
    cli("live", "session-run", "--session-dir", dir_a)
    snap = status(dir_a)
    record("a.rebaseline", "rebaseline --note; session-run", "halt cleared, session running again",
           rebased.returncode == 0 and snap.get("halted") is None and snap.get("status") in ("WAITING",),
           f"status={snap.get('status')} halted={snap.get('halted')}")
    flat = cli("live", "flatten", "--session-dir", dir_a, "--note", "drill flatten")
    finish = cli("live", "session-finish", "--session-dir", dir_a, "--note", "drill end")
    snap = status(dir_a)
    record("a.flatten_finish", "flatten; session-finish", "position sold, session COMPLETED, no open orders",
           flat.returncode == 0 and finish.returncode == 0 and snap.get("status") == "COMPLETED"
           and not snap.get("open_orders"),
           f"flatten={flat.returncode} finish={finish.returncode} status={snap.get('status')} "
           f"position={snap.get('position')}")

    # ------------------------------------------------------------ part B
    limit_config = {**ops_config, "order_type": "LIMIT"}
    feed_b, fake_b, dir_b = make_part("b", limit_config, balances)
    fake_b.push({"rest": True})
    advance(feed_b, 1)
    cli("live", "session-run", "--session-dir", dir_b)
    remote = FakeExchange(fake_b.path).state.orders
    client_id = next(iter(remote), None)
    record("b.resting_order", "session-run with a LIMIT order left resting", "one order NEW on the exchange",
           client_id is not None and remote[client_id]["status"] == "NEW", str({k: v["status"] for k, v in remote.items()}))
    restarted = cli("live", "session-run", "--session-dir", dir_b)
    submits = FakeExchange(fake_b.path).state.calls.count("submit")
    record("b.restart_with_open_order", "second session-run (runner restart) with the order open",
           "order reconciled, still NEW, no new submit", restarted.returncode == 0 and submits == 1,
           f"submits={submits} status={status(dir_b).get('status')}")
    if client_id:
        FakeExchange(fake_b.path).fill_resting(client_id, "0.5")
    reconciled = cli("live", "session-reconcile", "--session-dir", dir_b)
    snap = status(dir_b)
    record("b.reconcile_partial", "partial fill while idle; session-reconcile", "fill imported, order still open",
           reconciled.returncode == 0 and snap.get("fills") == 1 and snap.get("open_orders") == [client_id],
           f"fills={snap.get('fills')} open={snap.get('open_orders')}")
    cancelled = cli("live", "cancel-open-orders", "--session-dir", dir_b, "--note", "drill cancel")
    cli("live", "flatten", "--session-dir", dir_b, "--note", "drill flatten")
    cli("live", "session-finish", "--session-dir", dir_b, "--note", "drill end")
    snap = status(dir_b)
    statuses = [o["status"] for o in FakeExchange(fake_b.path).state.orders.values()]
    record("b.cancel_and_close", "cancel-open-orders; flatten; session-finish",
           "remainder CANCELED, partial position sold, COMPLETED",
           cancelled.returncode == 0 and statuses[:1] == ["CANCELED"] and snap.get("status") == "COMPLETED",
           f"orders={statuses} status={snap.get('status')} position={snap.get('position')}")

    # ------------------------------------------------------------ part C
    feed_c, fake_c, dir_c = make_part("c", live_config, balances)
    available = int((pd.Timestamp(feed_end, tz="UTC") - start) / pd.Timedelta(hours=1)) - 1
    hours = min(int(candidate_hours), available)
    for _ in range(hours):
        advance(feed_c, 1)
        cli("live", "session-run", "--session-dir", dir_c)
    cancelled = cli("live", "cancel-open-orders", "--session-dir", dir_c, "--note", "drill end")
    snap = status(dir_c)
    exchange_c = FakeExchange(fake_c.path)
    record("c.candidate_run", f"candidate configuration, {hours} hourly session-run",
           "every bar processed, no unresolved order, balances reconciled (status not HALTED/UNRESOLVED)",
           snap.get("status") in ("WAITING", "OPENINGS_SUSPENDED") and not snap.get("open_orders"),
           f"status={snap.get('status')} orders={len(exchange_c.state.orders)} fills={snap.get('fills')} "
           f"cursor={snap.get('cursor')}")
    result = {"schema_version": DRILL_SCHEMA, "run_utc": _now(), "python": python, "package_location": location,
              "dataset_id": dataset_id, "feed": [feed_start, feed_end], "steps": steps,
              "passed": all(step["result"] == "PASS" for step in steps)}
    (work / "drill.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def build_package(repo: Path, out: Path) -> dict:
    """Wheel of the working tree plus a fresh virtual environment where it is installed."""
    out.mkdir(parents=True, exist_ok=True)
    dist = out / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    built = subprocess.run([sys.executable, "-m", "pip", "wheel", str(repo), "--no-deps", "--no-build-isolation",
                            "-w", str(dist)], capture_output=True, text=True)
    if built.returncode != 0:
        raise ReleaseError(f"wheel build failed: {built.stderr[-500:]}")
    wheel = next(dist.glob("crypto_decision_lab-*.whl"))
    venv = out / "venv"
    if venv.exists():
        shutil.rmtree(venv)
    subprocess.run([sys.executable, "-m", "venv", "--system-site-packages", str(venv)], check=True)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    installed = subprocess.run([str(python), "-m", "pip", "install", "--no-deps", "--force-reinstall", str(wheel)],
                               capture_output=True, text=True)
    if installed.returncode != 0:
        raise ReleaseError(f"wheel install failed: {installed.stderr[-500:]}")
    return {"wheel": wheel.name, "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
            "python": str(python)}


# ====================================================================== 3. release card (CP08.2)
def _check(check_id: str, kind: str, observed: Any, op: str, threshold: Any, description: str = "") -> dict:
    passed = observed is not None and OPS[op](observed, threshold)
    return {"id": check_id, "kind": kind, "description": description, "observed": observed, "op": op,
            "threshold": threshold, "passed": bool(passed)}


def required_checks(criteria: dict) -> list[dict]:
    """Every check a card for ``criteria`` must carry: id, kind, operator, threshold (no observed value).

    Single source for :func:`build_card` and :func:`validate_card`, so a card cannot drop or
    weaken a check without failing validation.
    """
    operational, windows = criteria["operational"], criteria["windows"]

    def spec(check_id, kind, op, threshold, description=""):
        return {"id": check_id, "kind": kind, "op": op, "threshold": threshold, "description": description}

    checks = [spec(f"backtest.{c['id']}", "backtest", c["op"], c["threshold"], c.get("description", ""))
              for c in criteria["backtest"]]
    checks += [spec(f"paper.{c['id']}", "paper", c["op"], c["threshold"], c.get("description", ""))
               for c in criteria["paper"]]
    checks += [
        spec("backtest.completed", "backtest", "==", "COMPLETED", "the backtest covers the whole registered window"),
        spec("backtest.window_covered", "backtest", "==", _last_hour(windows["backtest"]["end"]),
             "last processed bar is the last hour of the window"),
        spec("paper.matches_backtest", "paper", "==", True, "persistent paper replay reproduces the backtest ledger"),
        spec("paper.completed", "paper", "==", "COMPLETED"),
        spec("ops.candidate_config_unchanged", "operational", "==", criteria["candidate_live_config_sha256"],
             "configuration on disk = registered candidate"),
        spec("ops.capital", "operational", "<=", operational["max_capital"]),
        spec("ops.max_order_notional", "operational", "<=", operational["max_order_notional"]),
        spec("ops.max_position_notional", "operational", "<=", operational["max_position_notional"]),
        spec("ops.git_commit", "operational", "==", True, "release built from a git checkout"),
        spec("ops.git_clean", "operational", "==", 0, "no uncommitted change under src/, config/, pyproject.toml"),
        spec("ops.testnet_collaudo", "operational", "==", True, "S07 smoke test passed on the Binance Spot testnet"),
        spec("ops.runbook_drill", "operational", "==", True, "CP08.1 drill passed on this code revision"),
    ]
    checks += [spec(f"ops.step_{step}_verified", "operational", "==", "VERIFIED", "PRD backlog status")
               for step in operational.get("required_steps_verified", [])]
    return checks


def build_card(candidate: str, *, repo_root: str | Path = ".", releases: str | Path = DEFAULT_RELEASES,
               drill: dict | None = None, package: dict | None = None, paper_root: str | Path = "var/release/paper",
               prd: str | Path = "Crypto_Decision_Lab_PRD.md") -> Path:
    from .live import live_digests

    repo = Path(repo_root).resolve()
    registry = _registry(repo, releases) / candidate
    criteria_path = registry / "criteria.json"
    if not criteria_path.is_file():
        raise ReleaseError("criteria must be registered before the release card")
    criteria = _read(criteria_path)
    live_config = criteria["candidate_live_config"]
    current_config = _read(repo / criteria["candidate_config"])
    code_sha, code_files = live_digests()
    out = registry / code_sha[:8]
    run_config = live_config["run_config"]
    windows = criteria["windows"]
    backtest, _ = evaluate_backtest(repo, run_config, windows["backtest"])
    paper_root_path = Path(paper_root) if Path(paper_root).is_absolute() else repo / paper_root
    paper = evaluate_paper(repo, run_config, windows["paper"], paper_root_path, f"release-{candidate}")
    operational = criteria["operational"]
    git = git_state(repo)
    steps = prd_status(repo / prd)
    testnet = testnet_evidence(repo, operational.get("testnet_evidence", ["var/s07-testnet/smoke-*.json"]))
    limits = {**live_config.get("limits", {})}
    observed = {f"backtest.{c['id']}": backtest.get(c["metric"]) for c in criteria["backtest"]}
    observed.update({f"paper.{c['id']}": paper.get(c["metric"]) for c in criteria["paper"]})
    observed.update({
        "backtest.completed": backtest["status"],
        "backtest.window_covered": backtest["last_event"],
        "paper.matches_backtest": paper["matches_backtest"],
        "paper.completed": paper["session_status"],
        "ops.candidate_config_unchanged": live_config_sha256(current_config),
        "ops.capital": float(run_config["initial_cash"]),
        "ops.max_order_notional": float(limits.get("max_order_notional", 1e18)),
        "ops.max_position_notional": float(limits.get("max_position_notional", 1e18)),
        "ops.git_commit": git["available"],
        "ops.git_clean": len(git["dirty_paths"]),
        "ops.testnet_collaudo": testnet["passed"],
        "ops.runbook_drill": bool(drill and drill.get("passed")),
    })
    observed.update({f"ops.step_{step}_verified": steps.get(step)
                     for step in operational.get("required_steps_verified", [])})
    checks = [_check(s["id"], s["kind"], observed[s["id"]], s["op"], s["threshold"], s["description"])
              for s in required_checks(criteria)]
    failed = [c for c in checks if not c["passed"]]
    card = {
        "schema_version": CARD_SCHEMA, "release_id": f"{candidate}-{code_sha[:8]}", "candidate": candidate,
        "created_utc": _now(),
        "version": {"git_commit": git["commit"], "git_dirty_paths": git["dirty_paths"],
                    "package_version": _package_version(repo), "code_sha256": code_sha, "code_files": code_files,
                    **({"wheel": package["wheel"], "wheel_sha256": package["wheel_sha256"]} if package else {})},
        "criteria": {"path": str(criteria_path.relative_to(repo)), "sha256": file_sha256(criteria_path),
                     "registered_utc": criteria["registered_utc"]},
        "target": {"exchange": operational["exchange"], "environment": operational["environment"],
                   "symbol": live_config.get("symbol", "BTCUSDT")},
        "configuration": {"live_config": live_config, "live_config_sha256": criteria["candidate_live_config_sha256"],
                          "limits": limits, "capital": run_config["initial_cash"]},
        "evidence": {"backtest": backtest, "paper": paper, "testnet": testnet,
                     "drill": {k: drill[k] for k in ("run_utc", "package_location", "passed")} if drill else None,
                     "prd_backlog": steps},
        "checks": checks,
        "decision": "READY" if not failed else "BLOCKED",
        "reasons": [f"{c['id']}: observed {c['observed']!r}, required {c['op']} {c['threshold']!r}" for c in failed],
    }
    if criteria["registered_utc"] > card["created_utc"]:
        raise ReleaseError("criteria registered after the card")
    if drill is not None:
        _write_once(out / "drill.json", drill)
    _write_once(out / "release-card.json", card)
    (out / "release-card.md").write_text(render_card(card), encoding="utf-8", newline="\n")
    return out / "release-card.json"


def _package_version(repo: Path) -> str | None:
    match = re.search(r'^version\s*=\s*"([^"]+)"', (repo / "pyproject.toml").read_text(encoding="utf-8"), re.M) \
        if (repo / "pyproject.toml").is_file() else None
    return match.group(1) if match else None


def render_card(card: dict) -> str:
    version, evidence = card["version"], card["evidence"]
    lines = [f"# Scheda di rilascio {card['release_id']}", "",
             f"**Decisione: {card['decision']}** · creata {card['created_utc']}", "",
             "## Versione", "",
             f"- Commit `{version['git_commit']}`; percorsi non committati: {len(version['git_dirty_paths'])}",
             f"- Pacchetto {version['package_version']}"
             + (f", wheel `{version.get('wheel')}` sha256 `{version.get('wheel_sha256', '')[:16]}…`"
                if version.get("wheel") else ""),
             f"- Codice (motore + live) sha256 `{version['code_sha256']}`",
             f"- Criteri `{card['criteria']['path']}` sha256 `{card['criteria']['sha256'][:16]}…`, "
             f"registrati {card['criteria']['registered_utc']}", "",
             "## Destinazione e limiti", "",
             f"- {card['target']['exchange']} {card['target']['environment']} {card['target']['symbol']}",
             f"- Capitale {card['configuration']['capital']}; limiti {json.dumps(card['configuration']['limits'])}", "",
             "## Evidenze", ""]
    for name in ("backtest", "paper"):
        item = evidence[name]
        lines.append(f"- **{name}** {item['window']['start']} → {item['window']['end']}: rendimento netto "
                     f"{item['net_return_pct']:.2f}%, drawdown {item['max_drawdown_mtm_pct']:.2f}%, trade chiusi "
                     f"{item['closed_trades']}, costi {item['costs']:.2f}, ledger `{item['ledger_sha256'][:12]}`")
    lines.append(f"- **testnet**: {len(evidence['testnet']['runs'])} esecuzioni, superato: {evidence['testnet']['passed']}")
    lines.append(f"- **drill CP08.1**: {evidence['drill']['passed'] if evidence['drill'] else 'assente'}")
    lines += ["", "## Controlli", "", "| Controllo | Osservato | Richiesto | Esito |", "|---|---|---|---|"]
    for check in card["checks"]:
        observed = f"{check['observed']:.4g}" if isinstance(check["observed"], float) else check["observed"]
        lines.append(f"| `{check['id']}` | {observed} | {check['op']} {check['threshold']} | "
                     f"{'PASS' if check['passed'] else 'FAIL'} |")
    if card["reasons"]:
        lines += ["", "## Motivi del blocco", ""] + [f"- {reason}" for reason in card["reasons"]]
    return "\n".join(lines) + "\n"


def validate_card(card_path: str | Path) -> dict:
    """Return the card if it is bound to its registered criteria; raise :class:`ReleaseError` otherwise.

    Checks, against ``<candidate>/criteria.json`` next to the card's revision folder: schema,
    location and release id, criteria digest, configuration digest, target, limits and capital,
    the exact set of required checks with their registered operators and thresholds, each
    ``passed`` flag recomputed from the observed value, and the decision implied by the checks.
    Observed evidence values are not re-measured here (that is what rebuilding the card does).
    """
    card_path = Path(card_path)
    if not card_path.is_file():
        raise ReleaseError(f"release card not found: {card_path}")
    try:
        card = _read(card_path)
    except (OSError, ValueError) as exc:
        raise ReleaseError(f"release card unreadable: {exc}") from exc
    problems: list[str] = []
    if not isinstance(card, dict) or card.get("schema_version") != CARD_SCHEMA:
        raise ReleaseError("release card: unsupported schema")
    criteria_path = card_path.parent.parent / "criteria.json"
    if not criteria_path.is_file():
        raise ReleaseError(f"release card: registered criteria not found at {criteria_path}")
    criteria = _read(criteria_path)
    try:
        candidate = card["candidate"]
        code_sha = card["version"]["code_sha256"]
        if criteria.get("schema_version") != CRITERIA_SCHEMA or criteria.get("candidate") != candidate:
            problems.append("criteria schema or candidate differs from the card")
        if not (isinstance(code_sha, str) and re.fullmatch(r"[0-9a-f]{64}", code_sha)):
            problems.append("code_sha256 is not a sha256 digest")
        elif (card_path.parent.name != code_sha[:8] or card_path.parent.parent.name != candidate
              or card["release_id"] != f"{candidate}-{code_sha[:8]}"):
            problems.append("release id or location does not match candidate and code revision")
        if card["criteria"].get("sha256") != file_sha256(criteria_path):
            problems.append("criteria digest differs from the registered criteria")
        if card["criteria"].get("registered_utc") != criteria.get("registered_utc") \
                or criteria.get("registered_utc", "") > card.get("created_utc", ""):
            problems.append("criteria registration time inconsistent with the card")
        live_config = criteria["candidate_live_config"]
        configuration = card["configuration"]
        registered_sha = criteria["candidate_live_config_sha256"]
        if configuration.get("live_config_sha256") != registered_sha \
                or live_config_sha256(configuration.get("live_config", {})) != registered_sha:
            problems.append("configuration differs from the registered candidate")
        if configuration.get("limits") != live_config.get("limits", {}) \
                or configuration.get("capital") != live_config["run_config"]["initial_cash"]:
            problems.append("limits or capital differ from the registered candidate")
        operational = criteria["operational"]
        if card["target"] != {"exchange": operational["exchange"], "environment": operational["environment"],
                              "symbol": live_config.get("symbol", "BTCUSDT")}:
            problems.append("target differs from the registered criteria")
        expected = {s["id"]: s for s in required_checks(criteria)}
        checks = card["checks"]
        ids = [c["id"] for c in checks]
        if len(ids) != len(set(ids)) or set(ids) != set(expected):
            missing, extra = sorted(set(expected) - set(ids)), sorted(set(ids) - set(expected))
            problems.append(f"check set differs from the criteria (missing {missing}, unexpected {extra}"
                            f"{', duplicated' if len(ids) != len(set(ids)) else ''})")
        for check in checks:
            spec = expected.get(check["id"])
            if spec is None:
                continue
            if check["op"] != spec["op"] or check["threshold"] != spec["threshold"]:
                problems.append(f"check {check['id']}: operator or threshold differs from the criteria")
            recomputed = check["observed"] is not None and OPS[spec["op"]](check["observed"], spec["threshold"])
            if bool(recomputed) != check["passed"]:
                problems.append(f"check {check['id']}: passed flag inconsistent with the observed value")
        all_passed = bool(checks) and all(c["passed"] for c in checks)
        if card["decision"] not in ("READY", "BLOCKED") or (card["decision"] == "READY") != all_passed:
            problems.append(f"decision {card['decision']!r} inconsistent with the checks")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ReleaseError(f"release card malformed: missing or invalid {exc}") from exc
    if problems:
        raise ReleaseError("release card invalid: " + "; ".join(problems))
    return card


# ====================================================================== 4. approval and activation gate
def approve(card_path: str | Path, *, operator: str, note: str) -> Path:
    card_path = Path(card_path)
    card = validate_card(card_path)
    if card["decision"] != "READY":
        raise ReleaseError(f"only READY cards can be approved; this card is {card['decision']}")
    if not operator or not note:
        raise ReleaseError("approval requires operator and note")
    approval = {"schema_version": APPROVAL_SCHEMA, "release_id": card["release_id"], "approved_utc": _now(),
                "operator": operator, "note": note, "card_sha256": file_sha256(card_path),
                "target": card["target"], "live_config_sha256": card["configuration"]["live_config_sha256"],
                "limits": card["configuration"]["limits"], "capital": card["configuration"]["capital"],
                "code_sha256": card["version"]["code_sha256"], "git_commit": card["version"]["git_commit"]}
    target = card_path.parent / "approval.json"
    _write_once(target, approval)
    return target


def verify_activation(approval_path: str | Path, *, live_config: dict, exchange: dict, code_sha256: str) -> dict:
    """Raise :class:`ActivationError` unless the session matches an approved READY release."""
    approval_path = Path(approval_path)
    if not approval_path.is_file():
        raise ActivationError(f"approval not found: {approval_path}")
    approval = _read(approval_path)
    card_path = approval_path.parent / "release-card.json"
    problems = []
    if approval.get("schema_version") != APPROVAL_SCHEMA:
        problems.append("unsupported approval schema")
    if not card_path.is_file() or file_sha256(card_path) != approval.get("card_sha256"):
        problems.append("release card missing or changed after approval")
    else:
        try:
            card = validate_card(card_path)
        except ReleaseError as exc:
            problems.append(str(exc))
        else:
            if card["decision"] != "READY":
                problems.append("release card is not READY")
            bound = {"release_id": card["release_id"], "target": card["target"],
                     "live_config_sha256": card["configuration"]["live_config_sha256"],
                     "limits": card["configuration"]["limits"], "capital": card["configuration"]["capital"],
                     "code_sha256": card["version"]["code_sha256"]}
            if any(approval.get(key) != value for key, value in bound.items()):
                problems.append("approval fields differ from the release card")
    if live_config_sha256(live_config) != approval.get("live_config_sha256"):
        problems.append("live configuration differs from the approved one (strategy, capital or limits)")
    target = approval.get("target", {})
    if exchange.get("name") != target.get("exchange") or exchange.get("environment") != target.get("environment"):
        problems.append(f"exchange {exchange.get('name')}/{exchange.get('environment')} differs from approved "
                        f"{target.get('exchange')}/{target.get('environment')}")
    if live_config.get("symbol", "BTCUSDT") != target.get("symbol"):
        problems.append("symbol differs from the approved one")
    if code_sha256 != approval.get("code_sha256"):
        problems.append("code revision differs from the approved one")
    if problems:
        raise ActivationError("; ".join(problems))
    return {"approval_sha256": file_sha256(approval_path), "release_id": approval["release_id"]}


# ====================================================================== 5. operational decision (CP08.3)
DECISIONS = ("ACTIVATED", "NOT_ACTIVATED")


def record_decision(release_dir: str | Path, *, decision: str, decided_by: str, reason: str,
                    open_requirements: list[str] | None = None, operating_mode: str | None = None,
                    live_session: str | None = None) -> dict:
    release_dir = Path(release_dir)
    card_path = release_dir / "release-card.json"
    if decision not in DECISIONS:
        raise ReleaseError(f"decision must be one of {DECISIONS}")
    if not card_path.is_file():
        raise ReleaseError("decision requires a release card")
    card = validate_card(card_path)
    entry = {"schema_version": DECISION_SCHEMA, "release_id": card["release_id"], "decided_utc": _now(),
             "decision": decision, "decided_by": decided_by, "reason": reason,
             "card_sha256": file_sha256(card_path), "card_decision": card["decision"]}
    if decision == "ACTIVATED":
        approval = release_dir / "approval.json"
        if card["decision"] != "READY" or not approval.is_file():
            raise ReleaseError("ACTIVATED requires a READY card and an approval")
        if not live_session:
            raise ReleaseError("ACTIVATED requires the live session directory for the first reconciliation")
        entry.update(approval_sha256=file_sha256(approval), live_session=live_session,
                     first_reconciliation="verify with live session-status: no unresolved orders, no HALTED")
    else:
        if not open_requirements and not operating_mode:
            raise ReleaseError("NOT_ACTIVATED requires the open requirements or the chosen operating mode")
        entry.update(open_requirements=open_requirements or [], operating_mode=operating_mode)
    with (release_dir / "decisions.jsonl").open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(_canonical(entry) + "\n")
    return entry


def decisions(release_dir: str | Path) -> list[dict]:
    path = Path(release_dir) / "decisions.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []


# ====================================================================== CLI
def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="S08 release card, approval and activation decision")
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--releases", default=str(DEFAULT_RELEASES))
    sub = parser.add_subparsers(dest="command", required=True)
    register = sub.add_parser("register-criteria")
    register.add_argument("--criteria", required=True)
    package = sub.add_parser("package")
    package.add_argument("--out", default="var/release/package")
    drill = sub.add_parser("drill")
    drill.add_argument("--candidate", required=True)
    drill.add_argument("--python", default=sys.executable)
    drill.add_argument("--workdir", required=True)
    card = sub.add_parser("build-card")
    card.add_argument("--candidate", required=True)
    card.add_argument("--drill", help="drill.json produced by the drill command")
    card.add_argument("--package-out", help="directory of the package command (wheel identity)")
    approval = sub.add_parser("approve")
    approval.add_argument("--card", required=True)
    approval.add_argument("--operator", required=True)
    approval.add_argument("--note", required=True)
    verify = sub.add_parser("verify")
    verify.add_argument("--approval", required=True)
    verify.add_argument("--config", required=True)
    verify.add_argument("--environment", default="mainnet")
    decide = sub.add_parser("decide")
    decide.add_argument("--release-dir", required=True)
    decide.add_argument("--decision", choices=DECISIONS, required=True)
    decide.add_argument("--decided-by", required=True)
    decide.add_argument("--reason", required=True)
    decide.add_argument("--open-requirement", action="append", default=[])
    decide.add_argument("--operating-mode")
    decide.add_argument("--live-session")
    args = parser.parse_args(argv)
    repo = Path(args.repo_root).resolve()
    if args.command == "register-criteria":
        print(register_criteria(args.criteria, repo_root=repo, releases=args.releases))
    elif args.command == "package":
        out = Path(args.out) if Path(args.out).is_absolute() else repo / args.out
        info = build_package(repo, out)
        (out / "package.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(info, indent=2))
    elif args.command == "drill":
        criteria = _read(_registry(repo, args.releases) / args.candidate / "criteria.json")
        result = run_drill(repo, args.workdir, live_config=criteria["candidate_live_config"], python=args.python)
        print(json.dumps({"passed": result["passed"], "steps": [(s["id"], s["result"]) for s in result["steps"]]}))
        return 0 if result["passed"] else 1
    elif args.command == "build-card":
        drill_result = _read(args.drill) if args.drill else None
        package_info = _read(Path(args.package_out) / "package.json") if args.package_out else None
        path = build_card(args.candidate, repo_root=repo, releases=args.releases, drill=drill_result,
                          package=package_info)
        card_data = _read(path)
        print(json.dumps({"card": str(path), "decision": card_data["decision"], "reasons": card_data["reasons"]},
                         indent=2))
    elif args.command == "approve":
        print(approve(args.card, operator=args.operator, note=args.note))
    elif args.command == "verify":
        from .live import live_digests

        print(json.dumps(verify_activation(args.approval, live_config=_read(args.config),
                                           exchange={"name": "binance-spot", "environment": args.environment},
                                           code_sha256=live_digests()[0])))
    elif args.command == "decide":
        print(json.dumps(record_decision(args.release_dir, decision=args.decision, decided_by=args.decided_by,
                                         reason=args.reason, open_requirements=args.open_requirement,
                                         operating_mode=args.operating_mode, live_session=args.live_session),
                         indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
