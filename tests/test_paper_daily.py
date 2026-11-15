"""E05-P1 prerequisites: daily features in the paper runner (PRE-1) and the public klines source (PRE-2).

Synthetic data only; no network: the market is a fake HTTP client serving a synthetic series hour by hour.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab import experiment_protocol as protocol
from crypto_decision_lab import paper, public_klines, session_fidelity
from crypto_decision_lab.daily_trend import daily_features, daily_features_at
from crypto_decision_lab.dataset_catalog import Catalog, load_market
from crypto_decision_lab.lab_feed import FeedError
from crypto_decision_lab.paper import PaperSession, SessionError
from crypto_decision_lab.public_klines import PublicKlineSource

RESEARCH_ID = "BTCUSDT-1h-raw-v1"
HOUR = pd.Timedelta(hours=1)
DAY = pd.Timedelta(days=1)
T0 = pd.Timestamp("2024-06-01 00:00", tz="UTC")
MOMENTUM_DAYS, SMA_DAYS = 7, 20
CONFIG = {"strategy": {"name": "daily_trend", "parameters": {"entry_threshold": 0.0, "exit_threshold": 0.0,
                                                              "regime_min": 0.0}},
          "initial_cash": 500, "hypothesis": {"latency_hours": 0, "horizon_hours": 1000000, "fee_rate": 0.001,
                                              "slippage": 0.0005},
          "risk": {"max_exposure": 0.5, "min_notional": 10, "allow_open": True}}
FEATURES = {"momentum_days": MOMENTUM_DAYS, "sma_days": SMA_DAYS}


def _series(days=400, seed=17) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    hours = days * 24
    drift = 0.0007 * np.sin(np.arange(hours) / (24 * 6.0))
    opens = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.004, hours) + drift))
    closes = np.append(opens[1:], opens[-1] * 1.001)
    index = pd.date_range("2024-01-01", periods=hours, freq="1h", tz="UTC")
    return pd.DataFrame({"open": opens, "high": np.maximum(opens, closes) * 1.002,
                         "low": np.minimum(opens, closes) * 0.998, "close": closes, "volume": 10.0}, index=index)


def _write_catalog(root: Path, frame: pd.DataFrame) -> None:
    chunk = root / "data" / "chunks" / "BTCUSDT" / "research.parquet"
    chunk.parent.mkdir(parents=True)
    out = frame.reset_index().rename(columns={"index": "timestamp"})
    out.to_parquet(chunk, index=False)
    snapshot = root / "data" / "snapshots" / RESEARCH_ID
    snapshot.mkdir(parents=True)
    (snapshot / "raw-manifest.json").write_text(json.dumps({
        "dataset_id": RESEARCH_ID, "source_manifest_sha256": hashlib.sha256(b"paper-daily").hexdigest(), "chunks": [
            {"file": "chunks/BTCUSDT/research.parquet", "sha256": hashlib.sha256(chunk.read_bytes()).hexdigest()}]}),
        encoding="utf-8")
    (root / "config").mkdir()
    (root / "config" / "dataset-catalog.json").write_text(json.dumps({
        "schema_version": "cdl.s02.catalog.v1",
        "sources": [{"dataset_id": RESEARCH_ID, "path": f"data/snapshots/{RESEARCH_ID}", "role": "research"},
                    {"dataset_id": "BTCUSDT-1h-forward-v1", "path": "data/forward/BTCUSDT-1h-forward-v1",
                     "role": "reserved-forward"}],
        "reserved_dataset_ids": ["BTCUSDT-1h-forward-v1"]}), encoding="utf-8")


@pytest.fixture(scope="module")
def market_frame():
    return _series()


@pytest.fixture
def repo(tmp_path, market_frame):
    root = tmp_path / "repo"
    root.mkdir()
    _write_catalog(root, market_frame)
    return root


def _kline(ts: pd.Timestamp, row: pd.Series) -> list:
    return [int(ts.timestamp() * 1000), repr(float(row.open)), repr(float(row.high)), repr(float(row.low)),
            repr(float(row.close)), repr(float(row.volume)), int(ts.timestamp() * 1000) + 3_599_999, "0", 0, "0", "0",
            "0"]


class _Response:
    def __init__(self, status: int, payload) -> None:
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeMarket:
    """Fake public market data API: serves the synthetic series up to ``now``, like the real one."""

    def __init__(self, frame: pd.DataFrame, now: pd.Timestamp) -> None:
        self.frame, self.now, self.fail, self.calls, self.statuses = frame, now, False, [], []

    def get(self, url, params=None, timeout=None):
        path = url.split("binance.vision", 1)[1]
        self.calls.append((path, dict(params or {})))
        if self.statuses:
            return _Response(self.statuses.pop(0), {})
        if self.fail:
            raise ConnectionError("network down")
        if path == "/api/v3/time":
            return _Response(200, {"serverTime": int(self.now.timestamp() * 1000)})
        assert path == "/api/v3/klines"
        start = pd.Timestamp(params["startTime"], unit="ms", tz="UTC")
        stop = self.now if "endTime" not in params else min(self.now, pd.Timestamp(params["endTime"], unit="ms",
                                                                                   tz="UTC"))
        window = self.frame.loc[start:stop].iloc[: int(params.get("limit", 500))]
        return _Response(200, [_kline(ts, row) for ts, row in window.iterrows()])


@pytest.fixture
def market(monkeypatch, market_frame):
    fake = FakeMarket(market_frame, T0 + pd.Timedelta(minutes=5))
    monkeypatch.setattr(public_klines, "new_session", lambda: fake)
    monkeypatch.setattr(public_klines.time, "sleep", lambda *_: None)
    return fake


# ======================================================================== PRE-1: daily features in the runner
def _history_lookup(frame: pd.DataFrame):
    close = frame["close"]

    def prev_close(ts):
        before = ts - HOUR
        return float(close.loc[before]) if ts in close.index and before in close.index else None

    return prev_close


@pytest.mark.parametrize("drop", [[], [5000, 5063, 6200]])  # 5063 is a 23:00 bar
def test_daily_features_at_equals_the_whole_frame_features(market_frame, drop):
    frame = market_frame.drop(index=market_frame.index[drop]) if drop else market_frame
    prev_close = _history_lookup(frame)
    for momentum_days, sma_days in ((3, 5), (7, 20), (28, 100)):
        expected = daily_features(frame, momentum_days, sma_days)
        compared = 0
        for ts in frame.index[2800:7000]:
            got = daily_features_at(ts, prev_close(ts), prev_close, momentum_days, sma_days)
            assert set(got) == set(expected[ts]), (ts, momentum_days, sma_days)
            for key, value in got.items():
                assert value == pytest.approx(expected[ts][key], rel=1e-12), (ts, key)
            compared += ts.hour == 0
        assert compared > 100
    if drop:  # the missing 23:00 bar leaves a day without close: the windows that contain it are undefined
        missing = market_frame.index[5063]
        assert missing.hour == 23
        lost = missing + HOUR  # the decision of the following day has no daily close
        assert daily_features_at(lost, prev_close(lost), prev_close, 1, 5) == {"daily_decision": 1.0}
        after = lost + DAY     # one day later the 1-day return would need the missing close
        assert "d_momentum" not in daily_features_at(after, prev_close(after), prev_close, 1, 5)
        assert "d_trend" not in daily_features_at(after + 3 * DAY, prev_close(after + 3 * DAY), prev_close, 1, 5)
        assert "d_trend" in daily_features_at(after + 5 * DAY, prev_close(after + 5 * DAY), prev_close, 1, 5)


def test_session_features_must_match_the_strategy(repo):
    source = {"kind": "snapshot", "dataset_id": RESEARCH_ID}
    base = dict(start_at="2024-06-01", end_at="2024-06-10", root="var/paper", repo_root=repo)
    for bad in (None, {}, {"momentum_days": 7}, {"momentum_days": 0, "sma_days": 20},
                {"momentum_days": 7, "sma_days": 1}, {"momentum_days": 7.5, "sma_days": 20},
                {"momentum_days": 7, "sma_days": 20, "other": 1}):
        with pytest.raises(SessionError):
            PaperSession.create("bad", CONFIG, source, features=bad, **base)
    momentum = {**CONFIG, "strategy": {"name": "momentum", "parameters": {"threshold": 0.0}}}
    with pytest.raises(SessionError, match="takes no feature parameters"):
        PaperSession.create("bad", momentum, source, features=FEATURES, **base)
    plain = PaperSession.create("plain", momentum, source, **base)
    assert "features" not in plain.meta  # sessions of other strategies keep their metadata unchanged
    one = PaperSession.create("daily", CONFIG, source, features=FEATURES, **base)
    other = PaperSession.create("daily", CONFIG, source, features={"momentum_days": 7, "sma_days": 30}, **base)
    assert one.meta["features"] == FEATURES and one.meta["session_id"] != other.meta["session_id"]


def test_replay_session_with_daily_features_matches_the_backtest_ledger(repo):
    window = {"start": str(T0.tz_localize(None)), "end": str((T0 + 60 * DAY).tz_localize(None))}
    session = PaperSession.create("replay", CONFIG, {"kind": "snapshot", "dataset_id": RESEARCH_ID},
                                  start_at=window["start"], end_at=window["end"], root="var/paper",
                                  repo_root=repo, features=FEATURES)
    snapshot = session.poll()
    assert snapshot["status"] == "COMPLETED"
    frame = load_market(Catalog.load(repo / "config" / "dataset-catalog.json"), RESEARCH_ID, repo_root=repo).frame
    result, backtest = protocol.run_window(frame, CONFIG, window, daily_features(frame, **{
        "momentum_days": MOMENTUM_DAYS, "sma_days": SMA_DAYS}))
    ledger = session.ledger()
    assert session_fidelity.normalized(ledger) == session_fidelity.normalized(backtest)  # as the experiment runner
    assert session_fidelity.check(session.directory, repo_root=repo)["match"]
    orders = [r for r in ledger if r["kind"] == "order"]
    assert len(orders) >= 4 and all(pd.Timestamp(r["timestamp"]).hour == 0 for r in orders)
    assert result["closed_trades"] >= 2
    session.close()


# ======================================================================== PRE-2: public klines source
def test_source_reads_closed_bars_and_the_open_of_the_current_bar(market, market_frame):
    source = PublicKlineSource("BTCUSDT", history_hours=2600, pause=0)
    view = source.records()
    hour = market.now.floor("h")
    assert len(view.records) == 2600 + 1 and min(view.records) == hour - 2600 * HOUR and max(view.records) == hour
    current = view.records[hour]
    assert current.open == market_frame.loc[hour, "open"] and current.prev_close == market_frame.loc[hour - HOUR, "close"]
    assert hour + HOUR not in view.records  # the future does not exist
    assert all(r.prev_close == market_frame.loc[ts - HOUR, "close"] for ts, r in view.records.items()
               if ts - HOUR in view.records)
    pages = [c for c in market.calls if c[0] == "/api/v3/klines" and "endTime" in c[1]]
    assert len(pages) == 3  # 2600 closed bars at 1000 per page
    assert source.now() == market.now.floor("ms") and source.identity()["kind"] == "public_klines"


def test_source_tolerates_missing_hours_and_hides_the_unfinished_close(market, market_frame):
    market.frame = market_frame.drop(index=[T0 - 40 * HOUR, T0 - 3 * HOUR, T0])  # also the current bar
    view = PublicKlineSource("BTCUSDT", history_hours=600, pause=0).records()
    assert T0 - 40 * HOUR not in view.records and T0 - 3 * HOUR not in view.records and T0 not in view.records
    assert view.records[T0 - 2 * HOUR].prev_close is None   # its predecessor is missing: no invented price
    assert view.records[T0 - 4 * HOUR].prev_close is not None


def test_source_retries_throttling_then_fails_cleanly(market):
    market.statuses = [429, 503]
    source = PublicKlineSource("BTCUSDT", history_hours=100, pause=1e-9)
    assert source.now() == market.now.floor("ms")  # two refusals, then the answer
    market.fail = True
    with pytest.raises(FeedError, match="unavailable"):
        source.records()
    with pytest.raises(FeedError):
        PublicKlineSource("btcusdt")
    with pytest.raises(FeedError):
        PublicKlineSource("BTCUSDT", history_hours=10)


# ======================================================================== both: a live paper session
def _live_session(repo, **kwargs):
    return PaperSession.create("live", CONFIG, {"kind": "public_klines", "symbol": "BTCUSDT", "history_hours": 600},
                               start_at=T0, root="var/paper", repo_root=repo, features=FEATURES, **kwargs)


def test_live_session_never_replays_past_bars(repo, market):
    market.now = T0 + 5 * HOUR
    with pytest.raises(SessionError, match="current hour"):
        _live_session(repo)
    market.now = T0 + pd.Timedelta(minutes=5)
    assert _live_session(repo).meta["mode"] == "paper"
    market.fail = True
    with pytest.raises(SessionError, match="unavailable"):
        PaperSession.create("down", CONFIG, {"kind": "public_klines", "symbol": "BTCUSDT"}, start_at=T0,
                            root="var/paper", repo_root=repo, features=FEATURES)


def test_live_session_hour_by_hour_reproduces_the_backtest(repo, market, market_frame):
    session = _live_session(repo)
    days = 14
    for step in range(days * 24):
        market.now = T0 + step * HOUR + pd.Timedelta(minutes=5)
        snapshot = session.poll()
        assert snapshot["status"] == "WAITING" and snapshot["cursor"] == str(T0 + step * HOUR)
        if step == 70:  # a network outage in the middle: nothing changes, the next poll resumes
            market.fail = True
            down = session.poll()
            assert down["status"] == "DATA_ERROR" and "source unavailable" in down["detail"]
            assert down["cursor"] == str(T0 + step * HOUR)
            market.fail = False
    ongoing = session_fidelity.check(session.directory, repo_root=repo)  # before the session is closed
    assert ongoing["match"] and ongoing["events"] == days * 24, ongoing
    session.finish("end of the test")
    window = {"start": str(T0.tz_localize(None)), "end": str((T0 + days * DAY).tz_localize(None))}
    _, backtest = protocol.run_window(market_frame, CONFIG, window,
                                      daily_features(market_frame, MOMENTUM_DAYS, SMA_DAYS))
    ledger = session.ledger()
    assert session_fidelity.normalized(ledger) == session_fidelity.normalized(backtest)
    assert session_fidelity.check(session.directory, repo_root=repo)["match"]  # and after
    assert session_fidelity.main(["--session-dir", str(session.directory), "--repo-root", str(repo)]) == 0
    session.connection.execute("UPDATE ledger SET record = replace(record, '\"side\":\"BUY\"', '\"side\":\"SELL\"') "
                               "WHERE rowid = (SELECT MIN(rowid) FROM ledger WHERE kind = 'order')")
    tampered = session_fidelity.check(session.directory, repo_root=repo)
    assert not tampered["match"] and tampered["session_record"]["kind"] == "order"  # a changed order is detected
    assert session_fidelity.main(["--session-dir", str(session.directory), "--repo-root", str(repo)]) == 1
    assert any(r["kind"] == "order" for r in ledger)
    assert all(pd.Timestamp(r["timestamp"]).hour == 0 for r in ledger if r["kind"] == "order" and r["side"] == "BUY")
    session.close()


def test_cli_creates_a_live_session_and_requires_one_source(repo, market, tmp_path, capsys):
    config = tmp_path / "config.json"
    config.write_text(json.dumps(CONFIG), encoding="utf-8")
    args = ["--repo-root", str(repo), "session-create", "--name", "cli", "--config", str(config),
            "--start", str(T0), "--root", "var/paper-cli", "--momentum-days", "7", "--sma-days", "20"]
    with pytest.raises(SystemExit):  # no source
        paper.main(args)
    with pytest.raises(SystemExit):  # two sources
        paper.main([*args, "--snapshot", RESEARCH_ID, "--public-klines", "BTCUSDT"])
    capsys.readouterr()
    assert paper.main([*args, "--public-klines", "BTCUSDT", "--history-hours", "600"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["mode"] == "paper" and created["status"] == "CREATED"
    session = PaperSession(Path(created["session_dir"]), repo_root=repo)
    assert session.meta["features"] == FEATURES and session.meta["source"]["history_hours"] == 600
    session.close()
