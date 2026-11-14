"""S07 live adapter: CP07.1 order lifecycle, CP07.2 timeout/restart reconciliation, CP07.3 guards.

The collaudo runs against two fixtures of the exchange:
* :class:`FakeExchange` (Binance Spot semantics, fault injection, file-backed);
* the real :class:`BinanceSpotAdapter` over a transport that serves recorded
  response shapes (tests/fixtures/binance_spot) or translates to the fake.
"""
from __future__ import annotations

from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys

import pandas as pd
import pytest

from crypto_decision_lab import live
from crypto_decision_lab.binance_spot import BinanceSpotAdapter, TransportUnknown, sign
from crypto_decision_lab.exchange import (
    D,
    ExchangeRejected,
    ExchangeUnknown,
    FakeExchange,
    OrderNotFound,
    OrderRequest,
)
from crypto_decision_lab.lab_feed import FEED_SCHEMA, LabFeed
from crypto_decision_lab.live import LiveError, LiveSession

SOURCE = "BTCUSDT-1h-raw-v1"
SRC = Path(__file__).resolve().parents[1] / "src"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "binance_spot"
START = pd.Timestamp("2024-01-31 00:00", tz="UTC")
H = pd.Timedelta(hours=1)
CONSTANT = {
    "symbol": "BTCUSDT",
    "run_config": {"strategy": {"name": "constant", "parameters": {"exposure": "LONG"}}, "initial_cash": 1000,
                   "hypothesis": {"latency_hours": 0, "horizon_hours": 3, "fee_rate": 0.001, "slippage": 0.0},
                   "risk": {"max_exposure": 0.5, "min_notional": 10, "allow_open": True}},
    "limits": {"max_order_notional": 400, "max_position_notional": 600},
}
LIMIT = {**CONSTANT, "order_type": "LIMIT", "limit_offset": 0.001}


@pytest.fixture(autouse=True)
def _no_crash_hook():
    live.CRASH_HOOK = None
    yield
    live.CRASH_HOOK = None


def make(repo: Path, name: str = "live", config: dict = CONSTANT, faults=(), balances=None):
    feed = LabFeed.create(repo / "var" / "lab" / name, {
        "schema_version": FEED_SCHEMA, "feed_id": f"lab-{name}", "source_dataset_id": SOURCE,
        "start": "2024-01-30 00:00", "end": "2024-02-03 00:00", "faults": list(faults)}, repo_root=repo)
    feed.advance(until=START - H)
    fake = FakeExchange(repo / "var" / f"{name}-exchange.json", balances=balances or {"BTC": "0", "USDT": "5000"},
                        price=100)
    session = LiveSession.create(name, config, exchange={"name": "fake", "state_path": str(fake.path)},
                                 source={"kind": "lab_feed", "path": str(feed.directory)}, start_at=START,
                                 repo_root=repo, adapter=fake, feed=feed)
    return session, feed, fake


def step(session, feed, until):
    feed.advance(until=until)
    return session.poll()


def assert_reconciled(session, fake):
    """Local fills, order quantities and balances agree with the exchange."""
    for order in session.orders():
        if order["exchange_order_id"] is None:
            assert order["client_order_id"] not in fake.state.orders
            continue
        remote = fake.query("BTCUSDT", order["client_order_id"])
        local = sum((D(f["quantity"]) for f in session.fills() if f["client_order_id"] == order["client_order_id"]),
                    Decimal(0))
        assert local == remote.executed_quantity
        assert order["status"] == remote.status
        assert D(order["executed_quantity"]) == remote.executed_quantity
    assert len({f["trade_id"] for f in session.fills()}) == len(session.fills())
    assert session.balance_mismatch() is None
    # no client id was ever sent twice
    assert len(fake.state.orders) == sum(1 for o in session.orders() if o["exchange_order_id"] is not None)


def kinds(session, kind):
    return [r for r in session.ledger() if r["kind"] == kind]


# ====================================================================== CP07.1
def test_cp071_market_open_fill_and_horizon_exit_reconcile(studio_repo):
    session, feed, fake = make(studio_repo)
    snap = step(session, feed, START)
    order = session.orders()[0]
    assert order["status"] == "FILLED" and order["side"] == "BUY" and order["purpose"] == "open"
    request = json.loads(order["request"])
    assert D(request["quote_quantity"]) == D("400")  # min(cash*0.5, max_order_notional)
    fill = session.fills()[0]
    open_price = feed.records().records[START].open
    assert D(fill["price"]) == D(open_price)
    assert fill["commission_asset"] == "BTC"
    book = session.book()
    assert book["position"] == D(fill["quantity"]) - D(fill["commission"])
    assert book["cash"] == D(1000) - D(fill["price"]) * D(fill["quantity"])
    assert snap["status"] == "WAITING"
    step(session, feed, START + 3 * H)
    sells = [o for o in session.orders() if o["side"] == "SELL"]
    assert len(sells) == 1 and sells[0]["purpose"] == "horizon_exit" and sells[0]["status"] == "FILLED"
    assert session.book()["position"] < session.rules.min_qty
    assert {r["kind"] for r in session.ledger()} >= {"market", "decision", "risk", "order", "order_update",
                                                     "execution", "balance"}
    assert_reconciled(session, fake)


def test_cp071_rejection_is_terminal_and_next_bar_uses_a_new_client_id(studio_repo):
    session, feed, fake = make(studio_repo, "reject")
    fake.push({"reject": -2010, "msg": "Account has insufficient balance for requested action."})
    step(session, feed, START)
    first = session.orders()[0]
    assert first["status"] == "REJECTED" and "-2010" in first["detail"]
    assert session.fills() == [] and session.book()["position"] == 0
    step(session, feed, START + H)
    second = session.orders()[1]
    assert second["status"] == "FILLED" and second["client_order_id"] != first["client_order_id"]
    assert_reconciled(session, fake)


def test_cp071_limit_partial_fill_then_cancel(studio_repo):
    session, feed, fake = make(studio_repo, "partial", config=LIMIT)
    fake.push({"partial": "0.4"})
    step(session, feed, START)
    order = session.orders()[0]
    assert order["status"] == "PARTIALLY_FILLED"
    remote = fake.state.orders[order["client_order_id"]]
    first_qty = D(remote["executed_quantity"])
    assert first_qty == fake.rules("BTCUSDT").quantity(D(remote["orig_quantity"]) * D("0.4"))
    fake.fill_resting(order["client_order_id"], "0.5")  # market trades through part of the rest
    step(session, feed, START + H)  # older than order_timeout_seconds: reconciled, then cancelled
    order = session.orders()[0]
    assert order["status"] == "CANCELED"
    executed = D(order["executed_quantity"])
    assert first_qty < executed < D(remote["orig_quantity"])
    assert len(session.fills()) == 2
    assert kinds(session, "order_cancelled")[0]["executed_quantity"] == str(executed)
    commissions = sum(D(f["commission"]) for f in session.fills())
    assert session.book()["position"] == executed - commissions
    # the cancelled remainder is not re-bought on the same bar: the position is held (exposure unchanged)
    assert all(o["side"] != "BUY" or o["seq"] == 0 for o in session.orders())
    assert_reconciled(session, fake)


def test_cp071_market_partial_expires_and_resting_limit_is_cancelled(studio_repo):
    session, feed, fake = make(studio_repo, "expire")
    fake.push({"partial": "0.25"})
    step(session, feed, START)
    order = session.orders()[0]
    assert order["status"] == "EXPIRED" and D(order["executed_quantity"]) > 0
    assert_reconciled(session, fake)

    resting, feed2, fake2 = make(studio_repo, "resting", config=LIMIT)
    fake2.push({"rest": True})
    step(resting, feed2, START)
    assert resting.orders()[0]["status"] == "NEW"
    assert resting.snapshot()["open_orders"] == [resting.orders()[0]["client_order_id"]]
    snap = resting.cancel_open_orders("operator test")
    assert resting.orders()[0]["status"] == "CANCELED" and snap["open_orders"] == []
    assert resting.fills() == []
    assert_reconciled(resting, fake2)


# ====================================================================== CP07.2
def test_cp072_timeout_after_send_is_reconciled_before_any_new_order(studio_repo):
    session, feed, fake = make(studio_repo, "timeout")
    fake.push({"timeout": "after"})
    snap = step(session, feed, START)
    assert session.orders()[0]["status"] == "UNKNOWN" and snap["status"] == "UNRESOLVED"
    assert session.fills() == []  # the exchange filled it, the runner does not know yet
    fake.fail_next("query")  # reconciliation itself times out once
    snap = step(session, feed, START + 2 * H)
    assert snap["status"] == "UNRESOLVED" and snap["cursor"] == str(START)
    assert fake.state.calls.count("submit") == 1  # no bar processed, nothing sent
    snap = session.poll()
    assert session.orders()[0]["status"] == "FILLED" and len(session.fills()) == 1
    assert snap["cursor"] == str(START + 2 * H)
    assert fake.state.calls.count("submit") == 1  # position held, no duplicate BUY
    assert_reconciled(session, fake)


def test_cp072_timeout_before_reaching_the_exchange_becomes_not_placed(studio_repo):
    session, feed, fake = make(studio_repo, "lost")
    fake.push({"timeout": "before"})
    step(session, feed, START)
    assert session.orders()[0]["status"] == "UNKNOWN"
    session.poll()  # same instant: not found but within the grace period
    assert session.orders()[0]["status"] == "UNKNOWN"
    step(session, feed, START + H)
    first, second = session.orders()[:2]
    assert first["status"] == "NOT_PLACED" and second["status"] == "FILLED"
    assert list(fake.state.orders) == [second["client_order_id"]]
    assert_reconciled(session, fake)


class Crash(Exception):
    pass


@pytest.mark.parametrize("stage", ["before_send", "after_send"])
def test_cp072_restart_with_order_in_flight_does_not_duplicate(studio_repo, stage):
    session, feed, fake = make(studio_repo, f"crash-{stage}")

    def hook(point, client_id):
        if point == stage:
            raise Crash(client_id)

    live.CRASH_HOOK = hook
    feed.advance(until=START)
    with pytest.raises(Crash):
        session.poll()
    live.CRASH_HOOK = None
    session.close()
    reopened = LiveSession(session.directory, repo_root=studio_repo)  # adapter and feed rebuilt from the pins
    assert reopened.orders()[0]["status"] == "INTENT"
    reopened.poll()
    if stage == "after_send":
        assert reopened.orders()[0]["status"] == "FILLED" and len(reopened.fills()) == 1
    else:
        assert reopened.orders()[0]["status"] == "INTENT"  # within grace: still blocking
        step(reopened, LabFeed(feed.directory), START + H)
        assert reopened.orders()[0]["status"] == "NOT_PLACED"
        assert reopened.orders()[1]["status"] == "FILLED"
    exchange = FakeExchange(fake.path)
    assert exchange.state.calls.count("submit") == 1
    assert_reconciled(reopened, exchange)


def test_cp072_restart_with_resting_order_reconciles_and_sends_nothing(studio_repo):
    session, feed, fake = make(studio_repo, "restart-open", config=LIMIT)
    fake.push({"rest": True})
    step(session, feed, START)
    client_id = session.orders()[0]["client_order_id"]
    session.close()
    fake.fill_resting(client_id, "0.5")  # filled partly while the runner was down
    reopened = LiveSession(session.directory, repo_root=studio_repo)
    reopened.reconcile()
    order = reopened.orders()[0]
    assert order["status"] == "PARTIALLY_FILLED" and len(reopened.fills()) == 1
    assert FakeExchange(fake.path).state.calls.count("submit") == 1
    assert_reconciled(reopened, FakeExchange(fake.path))


def _cli(repo, *args, env=None):
    environment = {**os.environ, "PYTHONPATH": str(SRC), **(env or {})}
    return subprocess.run([sys.executable, "-m", "crypto_decision_lab.live", "--repo-root", str(repo), *args],
                          capture_output=True, text=True, env=environment, cwd=repo)


def test_cp072_hard_kill_of_runner_process_after_send(studio_repo):
    feed = LabFeed.create(studio_repo / "var/lab/kill", {
        "schema_version": FEED_SCHEMA, "feed_id": "lab-kill", "source_dataset_id": SOURCE,
        "start": "2024-01-30 00:00", "end": "2024-02-03 00:00", "faults": []}, repo_root=studio_repo)
    feed.advance(until=START - H)
    fake = FakeExchange(studio_repo / "var/kill-exchange.json", balances={"BTC": "0", "USDT": "5000"}, price=100)
    config = studio_repo / "live.json"
    config.write_text(json.dumps(CONSTANT), encoding="utf-8")
    created = _cli(studio_repo, "session-create", "--name", "kill", "--config", str(config), "--exchange", "fake",
                   "--fake-state", str(fake.path), "--feed-dir", str(feed.directory), "--start", str(START))
    assert created.returncode == 0, created.stderr
    directory = json.loads(created.stdout)["session_dir"]
    feed.advance(until=START)
    killed = _cli(studio_repo, "session-run", "--session-dir", directory, env={"CDL_LIVE_CRASH": "after_send"})
    assert killed.returncode == 97
    resumed = _cli(studio_repo, "session-run", "--session-dir", directory)
    assert resumed.returncode == 0, resumed.stderr
    session = LiveSession(Path(directory), repo_root=studio_repo)
    assert [o["status"] for o in session.orders()] == ["FILLED"]
    exchange = FakeExchange(fake.path)
    assert exchange.state.calls.count("submit") == 1
    assert_reconciled(session, exchange)


# ====================================================================== CP07.3
def test_cp073_exposure_limits_cap_and_block_openings(studio_repo):
    capped, feed, fake = make(studio_repo, "cap", config={**CONSTANT, "limits": {"max_order_notional": 400,
                                                                                    "max_position_notional": 250}})
    step(capped, feed, START)
    assert D(json.loads(capped.orders()[0]["request"])["quote_quantity"]) == D(250)
    blocked, feed2, fake2 = make(studio_repo, "block", config={**CONSTANT, "limits": {"max_position_notional": 3}})
    for hours in range(3):
        step(blocked, feed2, START + hours * H)
    assert blocked.orders() == [] and fake2.state.calls.count("submit") == 0
    assert {g["guard"] for g in kinds(blocked, "guard")} == {"exposure_limit"}


def test_cp073_stale_feed_blocks_openings_and_status_is_stale(studio_repo):
    outage = {"type": "outage", "from": str(START + 4 * H), "to": str(START + 8 * H)}
    session, feed, fake = make(studio_repo, "stale", faults=[outage],
                               config={**CONSTANT, "run_config": {**CONSTANT["run_config"],
                                                                  "hypothesis": {**CONSTANT["run_config"]["hypothesis"],
                                                                                 "horizon_hours": 1}}})
    step(session, feed, START + 2 * H)  # first poll late: bars 0 and 1 are older than 1h
    guards = kinds(session, "guard")
    assert [g["timestamp"] for g in guards] == [str(START)]
    assert session.orders()[0]["bar_ts"] == str(START + H)
    snap = step(session, feed, START + 7 * H)
    assert snap["status"] == "STALE"
    before = len(session.orders())
    step(session, feed, START + 8 * H)  # backfill: bars 4..7 arrive late
    late_opens = [g for g in kinds(session, "guard") if g["guard"] == "stale_feed"]
    assert {g["timestamp"] for g in late_opens} >= {str(START + 6 * H)}
    for order in session.orders()[before:]:
        if order["side"] == "BUY":
            assert pd.Timestamp(order["bar_ts"]) >= START + 7 * H
    assert_reconciled(session, fake)


def test_cp073_balance_mismatch_halts_every_order_until_rebaseline(studio_repo):
    session, feed, fake = make(studio_repo, "mismatch")
    step(session, feed, START)
    fake.external_transfer("BTC", "-0.5")  # someone sold part of the position outside the runner
    snap = step(session, feed, START + 3 * H)  # horizon exit due, but the state is not trusted
    assert snap["status"] == "HALTED" and "BTC expected" in snap["halted"]
    assert [o["side"] for o in session.orders()] == ["BUY"]
    assert {g["guard"] for g in kinds(session, "guard")} == {"halted"}
    with pytest.raises(LiveError):
        session.flatten("manual")
    session.rebaseline("0.5 BTC moved manually; accepted by operator")
    snap = step(session, feed, START + 4 * H)
    assert snap["halted"] is None
    assert [o["side"] for o in session.orders()] == ["BUY", "SELL"]
    assert session.balance_mismatch() is None


def test_cp073_manual_suspension_blocks_openings_but_not_exits(studio_repo):
    session, feed, fake = make(studio_repo, "suspend")
    step(session, feed, START)
    snap = session.suspend_openings("operator pause")
    assert snap["status"] == "OPENINGS_SUSPENDED"
    step(session, feed, START + 6 * H)
    sides = [o["side"] for o in session.orders()]
    assert sides == ["BUY", "SELL"]  # horizon exit allowed, no reopening
    assert any(g["guard"] == "openings_suspended" for g in kinds(session, "guard"))
    session.resume_openings("operator resume")
    step(session, feed, START + 7 * H)
    assert [o["side"] for o in session.orders()] == ["BUY", "SELL", "BUY"]
    assert_reconciled(session, fake)


def test_cp073_flatten_and_finish(studio_repo):
    session, feed, fake = make(studio_repo, "flatten")
    step(session, feed, START)
    session.flatten("end of collaudo")
    assert session.orders()[-1]["purpose"] == "operator_flatten" and session.orders()[-1]["status"] == "FILLED"
    snap = session.finish("collaudo done")
    assert snap["status"] == "COMPLETED"
    assert_reconciled(session, fake)


def test_live_specs_refuse_mainnet_and_inline_credentials(studio_repo, monkeypatch):
    with pytest.raises(PermissionError):
        BinanceSpotAdapter("mainnet")
    monkeypatch.setenv("CDL_LIVE_ALLOW_MAINNET", "1")
    with pytest.raises(PermissionError):
        BinanceSpotAdapter("mainnet")  # the flag in code is also required
    with pytest.raises(LiveError):
        LiveSession.create("x", CONSTANT, exchange={"name": "binance-spot", "environment": "mainnet"},
                           source={"kind": "lab_feed", "path": "nowhere"}, start_at=START, repo_root=studio_repo)
    with pytest.raises(LiveError):
        LiveSession.create("x", CONSTANT, exchange={"name": "fake", "api_key": "k", "api_secret": "s"},
                           source={"kind": "lab_feed", "path": "nowhere"}, start_at=START, repo_root=studio_repo)


def test_session_pins_code_revision(studio_repo, monkeypatch):
    session, feed, fake = make(studio_repo, "pin")
    session.close()
    monkeypatch.setattr(live, "live_digests", lambda: ("0" * 64, {}))
    reopened = LiveSession(session.directory, repo_root=studio_repo)
    feed.advance(until=START)
    assert reopened.poll()["status"] == "ENGINE_MISMATCH" and reopened.orders() == []
    reopened.accept_code_revision("reviewed")
    assert reopened.poll()["status"] == "WAITING" and len(reopened.orders()) == 1


# ====================================================================== review 2026-11-13 regressions
def test_review_f1_finished_session_accepts_no_command_and_never_trades(studio_repo):
    session, feed, fake = make(studio_repo, "f1")
    step(session, feed, START)
    session.flatten("close")
    session.finish("done")
    for command in (lambda: session.suspend_openings("x"), lambda: session.resume_openings("x"),
                    lambda: session.flatten("x"), lambda: session.cancel_open_orders("x"),
                    lambda: session.rebaseline("x"), lambda: session.accept_code_revision("x"),
                    lambda: session.finish("again")):
        with pytest.raises(LiveError, match="COMPLETED"):
            command()
    orders = len(session.orders())
    snap = step(session, feed, START + 3 * H)
    assert snap["status"] == "COMPLETED" and len(session.orders()) == orders
    assert FakeExchange(fake.path).state.calls.count("submit") == orders


def test_review_f2_operator_commands_keep_engine_mismatch(studio_repo, monkeypatch):
    session, feed, fake = make(studio_repo, "f2")
    session.close()
    monkeypatch.setattr(live, "live_digests", lambda: ("0" * 64, {}))
    reopened = LiveSession(session.directory, repo_root=studio_repo)
    assert reopened.status()[0] == "ENGINE_MISMATCH"
    assert reopened.suspend_openings("pause")["status"] == "ENGINE_MISMATCH"
    assert reopened.resume_openings("resume")["status"] == "ENGINE_MISMATCH"
    for command in (lambda: reopened.flatten("x"), lambda: reopened.cancel_open_orders("x"),
                    lambda: reopened.rebaseline("x"), lambda: reopened.finish("x")):
        with pytest.raises(LiveError, match="accept-code-revision"):
            command()
    feed.advance(until=START + H)
    assert reopened.poll()["status"] == "ENGINE_MISMATCH" and reopened.orders() == []
    reopened.accept_code_revision("reviewed diff")
    assert reopened.poll()["status"] == "WAITING" and len(reopened.orders()) == 1


def _drop_remote(fake, client_id):
    fake._reload()
    removed = fake.state.orders.pop(client_id)
    fake._save()
    return removed


def test_review_f3_open_order_unknown_to_the_exchange_halts_until_resolved(studio_repo):
    session, feed, fake = make(studio_repo, "f3", config=LIMIT)
    fake.push({"rest": True})
    step(session, feed, START)
    client_id = session.orders()[0]["client_order_id"]
    _drop_remote(fake, client_id)  # e.g. testnet reset or archived order
    snap = session.poll()
    assert session.orders()[0]["status"] == "MISSING" and snap["status"] == "UNRESOLVED"
    snap = step(session, feed, START + 2 * H)  # beyond unknown_grace_seconds
    assert snap["status"] == "HALTED" and "no longer known by the exchange" in snap["halted"]
    assert len(session.orders()) == 1 and FakeExchange(fake.path).state.calls.count("submit") == 1
    with pytest.raises(LiveError):
        session.rebaseline("too early")  # the missing order must be resolved first
    session.resolve_missing_order(client_id, "not in the exchange order history: never filled")
    assert session.orders()[0]["status"] == "LOST"
    session.rebaseline("no balance difference after the lost order")
    snap = step(session, feed, START + 3 * H)
    assert snap["halted"] is None and len(session.orders()) == 2  # trading resumes with a new client id
    assert session.orders()[1]["client_order_id"] != client_id


def test_review_f3_transient_not_found_recovers_without_halting(studio_repo):
    session, feed, fake = make(studio_repo, "f3t", config=LIMIT)
    fake.push({"rest": True})
    step(session, feed, START)
    client_id = session.orders()[0]["client_order_id"]
    removed = _drop_remote(fake, client_id)
    session.poll()
    assert session.orders()[0]["status"] == "MISSING"
    fake._reload()
    fake.state.orders[client_id] = removed
    fake._save()
    snap = session.poll()
    assert session.orders()[0]["status"] == "NEW" and snap["halted"] is None


# ====================================================================== independent review 2026-11-13 (R1-R3)
def _mainnet_session(repo, monkeypatch, name="r1"):
    """Real-capital session on the fixture exchange: the S08 gate is stubbed at creation only."""
    from crypto_decision_lab import release

    monkeypatch.setattr(release, "verify_activation",
                        lambda path, **kw: {"approval_sha256": "a" * 64, "release_id": "rel-old"})
    session, feed, fake = make(repo, name)  # builds feed + fake; replaced below by a mainnet session
    session.close()
    spec = {"name": "binance-spot", "environment": "mainnet", "allow_mainnet": True, "approval": "old/approval.json"}
    mainnet = LiveSession.create(f"{name}-main", CONSTANT, exchange=spec,
                                 source={"kind": "lab_feed", "path": str(feed.directory)}, start_at=START,
                                 repo_root=repo, adapter=fake, feed=feed)
    monkeypatch.undo()
    return mainnet, feed, fake


def test_review_r1_code_revision_of_a_real_capital_session_needs_a_new_approval(studio_repo, monkeypatch):
    from crypto_decision_lab import release

    session, feed, fake = _mainnet_session(studio_repo, monkeypatch)
    assert session.real_capital() and session.meta["activation"]["release_id"] == "rel-old"
    session.close()
    monkeypatch.setattr(live, "live_digests", lambda: ("1" * 64, {}))
    reopened = LiveSession(session.directory, repo_root=studio_repo, adapter=fake, feed=feed)
    assert reopened.status()[0] == "ENGINE_MISMATCH"
    with pytest.raises(LiveError, match="new S08 release"):
        reopened.accept_code_revision("hotfix")
    approval = studio_repo / "var" / "r1-approval.json"
    approval.write_text("{}", encoding="utf-8")
    with pytest.raises(LiveError, match="refused"):  # the real gate: not a valid approval of this code
        reopened.accept_code_revision("hotfix", approval=approval)
    assert reopened.status()[0] == "ENGINE_MISMATCH" and reopened.meta["code_sha256"] != "1" * 64
    seen = {}

    def gate(path, *, live_config, exchange, code_sha256):
        seen.update(path=Path(path), code=code_sha256, environment=exchange["environment"])
        return {"approval_sha256": "b" * 64, "release_id": "rel-new"}

    monkeypatch.setattr(release, "verify_activation", gate)
    reopened.accept_code_revision("hotfix approved", approval=approval)
    assert seen == {"path": approval, "code": "1" * 64, "environment": "mainnet"}
    assert reopened.status()[0] == "RUNNING" and reopened.meta["activation"]["release_id"] == "rel-new"
    reopened.close()
    again = LiveSession(session.directory, repo_root=studio_repo, adapter=fake, feed=feed)
    assert again.meta["activation"]["release_id"] == "rel-new" and again.status()[0] == "RUNNING"


def test_review_r1_testnet_and_fixture_sessions_accept_code_without_approval(studio_repo, monkeypatch):
    session, feed, fake = make(studio_repo, "r1t")
    assert not session.real_capital()
    session.close()
    monkeypatch.setattr(live, "live_digests", lambda: ("2" * 64, {}))
    reopened = LiveSession(session.directory, repo_root=studio_repo)
    assert reopened.accept_code_revision("reviewed")["status"] == "RUNNING"


def test_review_r3_finish_from_another_process_cannot_interleave_with_a_poll(studio_repo):
    session, feed, fake = make(studio_repo, "r3")
    operator = LiveSession(session.directory, repo_root=studio_repo)
    operator.lock_wait_seconds = 0.2
    original = session.feed.records
    attempts = []

    def records():  # the operator acts while the runner is between its checks and the send
        for name, action in (("finish", lambda: operator.finish("concurrent")),
                             ("poll", operator.poll), ("suspend", lambda: operator.suspend_openings("x"))):
            try:
                action()
                attempts.append((name, "ran"))
            except live.SessionBusy:
                attempts.append((name, "busy"))
        return original()

    session.feed.records = records
    feed.advance(until=START)
    session.poll()
    session.feed.records = original
    assert {outcome for _, outcome in attempts} == {"busy"}
    assert not kinds(session, "end") and session.status()[0] != "COMPLETED"
    assert len(session.orders()) == 1  # the runner's own order, sent while the session was open
    operator.finish("after the poll")  # lock released at the end of the poll
    ledger = session.ledger()
    assert ledger[-1]["kind"] == "end"
    submits = FakeExchange(fake.path).state.calls.count("submit")
    step(session, feed, START + 3 * H)
    assert session.status()[0] == "COMPLETED" and FakeExchange(fake.path).state.calls.count("submit") == submits
    assert session.ledger()[-1]["kind"] == "end"


def test_review_r3_send_refuses_an_intent_of_a_closed_session(studio_repo):
    session, feed, fake = make(studio_repo, "r3s")
    sent = []

    def hook(stage, client_id):  # simulate the close landing between intent commit and send
        if stage == "before_send":
            sent.append(client_id)

    live.CRASH_HOOK = hook
    original = session._send

    def closing_send(request):
        session._set_status("COMPLETED", "closed elsewhere")
        return original(request)

    session._send = closing_send
    step(session, feed, START)
    assert sent == [] and FakeExchange(fake.path).state.calls.count("submit") == 0
    assert session.orders()[0]["status"] == "NOT_PLACED"


def test_review_r3_lock_is_released_when_the_holder_dies(studio_repo):
    session, feed, fake = make(studio_repo, "r3k")
    code = ("import sys, os; from pathlib import Path; from crypto_decision_lab import live; "
            f"s = live.LiveSession(Path(r'{session.directory}'), repo_root=r'{studio_repo}'); "
            "s.lock.acquire(0); os._exit(97)")
    result = subprocess.run([sys.executable, "-c", code], env={**os.environ, "PYTHONPATH": str(SRC)})
    assert result.returncode == 97
    session.lock_wait_seconds = 1.0
    assert session.suspend_openings("after a killed holder")["status"] == "OPENINGS_SUSPENDED"


def test_review_r2_executed_order_with_missing_trades_stays_reconcilable(studio_repo):
    session, feed, fake = make(studio_repo, "r2")
    fake.push({"hide_fills": True})
    snap = step(session, feed, START)
    order = session.orders()[0]
    assert order["status"] == "FILLS_PENDING" and session.fills() == []
    assert snap["status"] == "UNRESOLVED" and snap["halted"] is None  # no misleading balance halt
    assert session.poll()["status"] == "UNRESOLVED"  # queried again, still incomplete (within the grace)
    fake.release_fills(order["client_order_id"])
    snap = session.poll()
    assert session.orders()[0]["status"] == "FILLED" and len(session.fills()) == 1
    assert snap["halted"] is None and snap["status"] == "WAITING"
    assert_reconciled(session, fake)
    snap = step(session, feed, START + H)
    assert snap["cursor"] == str(START + H)  # bars are processed again


def test_review_r2_trades_never_reported_halt_until_resolved(studio_repo):
    session, feed, fake = make(studio_repo, "r2h")
    fake.push({"hide_fills": True})
    step(session, feed, START)
    client_id = session.orders()[0]["client_order_id"]
    snap = step(session, feed, START + 2 * H)  # beyond unknown_grace_seconds
    assert snap["status"] == "HALTED" and "trades are still incomplete" in snap["halted"]
    with pytest.raises(LiveError):
        session.rebaseline("too early")
    session.resolve_missing_order(client_id, "myTrades on the exchange shows the fill; accepted from balances")
    assert session.orders()[0]["status"] == "FILLS_INCOMPLETE"
    session.rebaseline("BTC bought by the order whose trades were never reported")
    book = session.book()
    assert book["position"] > 0 and session.balance_mismatch() is None
    snap = step(session, feed, START + 4 * H)
    assert snap["halted"] is None and snap["cursor"] == str(START + 4 * H)


def test_review_r10_buy_without_imported_trades_still_exits_at_the_horizon(studio_repo):
    session, feed, fake = make(studio_repo, "r10")
    fake.push({"hide_fills": True})
    step(session, feed, START)
    client_id = session.orders()[0]["client_order_id"]
    assert step(session, feed, START + 2 * H)["status"] == "HALTED"
    session.resolve_missing_order(client_id, "myTrades on the exchange shows the fill; accepted from balances")
    session.rebaseline("BTC bought by the order whose trades were never reported")
    assert session.fills() == [] and session.book()["position"] > 0
    assert session._last_buy_bar() == START  # the horizon clock starts at the executed BUY
    step(session, feed, START + 4 * H)
    exits = [o for o in session.orders() if o["purpose"] == "horizon_exit"]
    assert exits
    assert pd.Timestamp(exits[0]["bar_ts"]) == START + 3 * H  # horizon_hours = 3


def test_review_r9_busy_session_gives_a_json_line_and_exit_code_not_a_traceback(studio_repo, capsys, monkeypatch):
    session, feed, fake = make(studio_repo, "r9")
    original = live._SessionLock.acquire
    monkeypatch.setattr(live._SessionLock, "acquire", lambda self, wait: original(self, min(wait, 0.1)))
    holder = LiveSession(session.directory, repo_root=studio_repo)
    holder.lock.acquire(0)
    try:
        for command in (["suspend-openings", "--note", "x"], ["flatten"], ["session-finish"]):
            code = live.main(["--repo-root", str(studio_repo), command[0], "--session-dir", str(session.directory),
                              *command[1:]])
            line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
            assert code == live.EXIT_BUSY == 75, command
            assert line["status"] == "BUSY" and "retry" in line["detail"], command
    finally:
        holder.lock.release()
    assert live.main(["--repo-root", str(studio_repo), "session-status", "--session-dir",
                      str(session.directory)]) == 0


def test_review_r8_trades_beyond_final_executed_quantity_halt_at_once(studio_repo):
    session, feed, fake = make(studio_repo, "r8")
    fake.push({"understate_executed": True})
    step(session, feed, START)
    client_id = session.orders()[0]["client_order_id"]
    assert session.orders()[0]["status"] == "FILLS_EXCESS"
    snap = session.poll()  # next reconciliation: no grace period
    assert snap["status"] == "HALTED" and "exceed the final executedQty" in snap["halted"]
    assert snap["cursor"] == str(START) and FakeExchange(fake.path).state.calls.count("submit") == 1
    with pytest.raises(LiveError):
        session.rebaseline("too early")
    session.resolve_missing_order(client_id, "exchange trade list checked: executedQty report was wrong")
    assert session.orders()[0]["status"] == "FILLS_EXCESS_ACCEPTED"
    session.rebaseline("balances agree with the imported trades")
    snap = step(session, feed, START + H)
    assert snap["halted"] is None and snap["cursor"] == str(START + H)


def test_review_r2_adapter_paginates_my_trades():
    trade = lambda i: {"id": i, "price": "100", "qty": "0.1", "commission": "0", "commissionAsset": "BNB"}  # noqa: E731
    order = {"symbol": "BTCUSDT", "orderId": 7, "clientOrderId": "cdl-1a2b3c4d-000009", "price": "0",
             "origQty": "0.5", "executedQty": "0.5", "cummulativeQuoteQty": "50", "status": "FILLED",
             "type": "MARKET", "side": "BUY"}
    exchange, transport = adapter((200, order), (200, [trade(1), trade(2)]), (200, [trade(3), trade(4)]),
                                  (200, [trade(5)]))
    exchange.trades_page_limit = 2
    report = exchange.query("BTCUSDT", "cdl-1a2b3c4d-000009")
    assert [f.trade_id for f in report.fills] == ["1", "2", "3", "4", "5"]
    assert sum(f.quantity for f in report.fills) == report.executed_quantity
    pages = [call[2] for call in transport.calls[1:]]
    assert [p.get("fromId") for p in pages] == [None, 3, 5]
    assert all(str(p["orderId"]) == "7" and int(p["limit"]) == 2 for p in pages)


# ====================================================================== Binance adapter
def _fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, params, headers, timeout):
        self.calls.append((method, url, dict(params), dict(headers)))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def adapter(*responses):
    transport = Recorder(*responses)
    return BinanceSpotAdapter("testnet", api_key="key", api_secret="secret", transport=transport,
                              clock=lambda: 1731456000.0), transport


def test_signature_matches_the_official_example():
    query = ("symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000"
             "&timestamp=1499827319559")
    secret = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
    assert sign(secret, query) == "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"


def test_adapter_parses_rules_orders_trades_and_balances():
    exchange, transport = adapter((200, _fixture("exchange_info_btcusdt.json")),
                                  (200, _fixture("order_full_market_buy.json")),
                                  (200, _fixture("order_query_partially_filled.json")),
                                  (200, _fixture("my_trades_order_29.json")),
                                  (200, _fixture("account.json")))
    rules = exchange.rules("BTCUSDT")
    assert (rules.step_size, rules.min_qty, rules.tick_size, rules.min_notional) == \
        (D("0.00001"), D("0.00001"), D("0.01"), D("5"))
    report = exchange.submit(OrderRequest("cdl-1a2b3c4d-000000", "BTCUSDT", "BUY", quote_quantity=D("400")))
    method, url, params, headers = transport.calls[1]
    assert (method, url) == ("POST", "https://testnet.binance.vision/api/v3/order")
    assert params["newClientOrderId"] == "cdl-1a2b3c4d-000000" and params["quoteOrderQty"] == "400"
    assert params["newOrderRespType"] == "FULL" and headers == {"X-MBX-APIKEY": "key"}
    unsigned = "&".join(f"{k}={v}" for k, v in params.items() if k != "signature")
    assert params["signature"] == sign("secret", unsigned)
    assert report.status == "FILLED" and [f.trade_id for f in report.fills] == ["1001", "1002"]
    assert sum(f.quantity for f in report.fills) == report.executed_quantity
    partial = exchange.query("BTCUSDT", "cdl-1a2b3c4d-000001")
    assert partial.status == "PARTIALLY_FILLED" and partial.fills[0].trade_id == "2001"
    assert transport.calls[3][2]["orderId"] == "29"
    assert exchange.balances() == {"BTC": D("1.005"), "USDT": D("9995")}


@pytest.mark.parametrize("response, error", [
    ((400, "insufficient_balance"), ExchangeRejected),
    ((400, "duplicate"), ExchangeRejected),
    ((400, "recv_window"), ExchangeRejected),
    ((400, "lot_size"), ExchangeRejected),
    ((503, None), ExchangeUnknown),
    ((200, "timeout"), ExchangeUnknown),
    (TransportUnknown("read timed out"), ExchangeUnknown),
    (ConnectionError("connect timeout"), ExchangeUnknown),
])
def test_adapter_classifies_submit_errors(response, error):
    errors = _fixture("errors.json")
    if isinstance(response, Exception):
        served = response
    else:
        status, key = response
        served = (status, errors[key] if key else {"code": -1000, "msg": "Internal error"})
        if key == "timeout":
            served = (500, errors[key])
    exchange, _ = adapter(served)
    with pytest.raises(error):
        exchange.submit(OrderRequest("cdl-1a2b3c4d-000002", "BTCUSDT", "SELL", quantity=D("0.001")))


def test_adapter_query_not_found_and_cancel_rejected():
    errors = _fixture("errors.json")
    exchange, _ = adapter((400, errors["no_such_order"]), (400, errors["cancel_rejected"]))
    with pytest.raises(OrderNotFound):
        exchange.query("BTCUSDT", "cdl-1a2b3c4d-000003")
    with pytest.raises(ExchangeRejected):
        exchange.cancel("BTCUSDT", "cdl-1a2b3c4d-000003")


class BinanceShim:
    """Transport that serves Binance-shaped JSON from a FakeExchange (adapter + runner end to end)."""

    def __init__(self, fake: FakeExchange):
        self.fake = fake

    @staticmethod
    def order_json(report, with_fills=True):
        payload = {"symbol": report.symbol, "orderId": int(report.exchange_order_id),
                   "clientOrderId": report.client_order_id, "price": str(report.price),
                   "origQty": str(report.orig_quantity), "executedQty": str(report.executed_quantity),
                   "cummulativeQuoteQty": str(report.quote_executed), "status": report.status,
                   "type": report.order_type, "side": report.side}
        if with_fills:
            payload["fills"] = [{"price": str(f.price), "qty": str(f.quantity), "commission": str(f.commission),
                                 "commissionAsset": f.commission_asset, "tradeId": int(f.trade_id)}
                                for f in report.fills]
        return payload

    def __call__(self, method, url, params, headers, timeout):
        path = url.split("binance.vision")[1]
        try:
            if path == "/api/v3/exchangeInfo":
                return 200, _fixture("exchange_info_btcusdt.json")
            if path == "/api/v3/order" and method == "POST":
                request = OrderRequest(params["newClientOrderId"], params["symbol"], params["side"], params["type"],
                                       quantity=D(params["quantity"]) if "quantity" in params else None,
                                       quote_quantity=D(params["quoteOrderQty"]) if "quoteOrderQty" in params else None,
                                       price=D(params["price"]) if "price" in params else None)
                return 200, self.order_json(self.fake.submit(request))
            if path == "/api/v3/order" and method == "GET":
                return 200, self.order_json(self.fake.query(params["symbol"], params["origClientOrderId"]), False)
            if path == "/api/v3/order" and method == "DELETE":
                return 200, self.order_json(self.fake.cancel(params["symbol"], params["origClientOrderId"]), False)
            if path == "/api/v3/myTrades":
                return 200, [{"id": int(f.trade_id), "price": str(f.price), "qty": str(f.quantity),
                              "commission": str(f.commission), "commissionAsset": f.commission_asset}
                             for f in self.fake.trades(params["symbol"], params["orderId"])]
            if path == "/api/v3/openOrders":
                return 200, [self.order_json(o, False) for o in self.fake.open_orders(params["symbol"])]
            if path == "/api/v3/account":
                return 200, {"balances": [{"asset": a, "free": str(v), "locked": "0"}
                                          for a, v in self.fake.balances().items()]}
        except ExchangeUnknown:
            return 503, {"code": -1007, "msg": "Timeout waiting for response from backend server."}
        except OrderNotFound:
            return 400, {"code": -2013, "msg": "Order does not exist."}
        except ExchangeRejected as exc:
            return 400, {"code": exc.code, "msg": str(exc)}
        raise AssertionError(f"unexpected call {method} {path}")


def test_binance_adapter_end_to_end_with_timeout_and_restart(studio_repo):
    feed = LabFeed.create(studio_repo / "var/lab/shim", {
        "schema_version": FEED_SCHEMA, "feed_id": "lab-shim", "source_dataset_id": SOURCE,
        "start": "2024-01-30 00:00", "end": "2024-02-03 00:00", "faults": []}, repo_root=studio_repo)
    feed.advance(until=START - H)
    fake = FakeExchange(studio_repo / "var/shim-exchange.json", balances={"BTC": "0", "USDT": "5000"}, price=100)
    binance = BinanceSpotAdapter("testnet", api_key="k", api_secret="s", transport=BinanceShim(fake))
    session = LiveSession.create("shim", CONSTANT, exchange={"name": "binance-spot", "environment": "testnet",
                                                             "follow_feed": False},
                                 source={"kind": "lab_feed", "path": str(feed.directory)}, start_at=START,
                                 repo_root=studio_repo, adapter=binance, feed=feed)
    fake.push({"timeout": "after"})
    feed.advance(until=START)
    session.poll()
    assert session.orders()[0]["status"] == "UNKNOWN"
    session.close()
    reopened = LiveSession(session.directory, repo_root=studio_repo, adapter=binance, feed=feed)
    reopened.poll()
    order = reopened.orders()[0]
    assert order["status"] == "FILLED" and len(reopened.fills()) == 1  # fills read from myTrades
    feed.advance(until=START + 3 * H)
    reopened.poll()
    assert [o["status"] for o in reopened.orders()] == ["FILLED", "FILLED"]
    assert fake.state.calls.count("submit") == 2
    assert_reconciled(reopened, fake)
