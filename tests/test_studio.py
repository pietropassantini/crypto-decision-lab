"""S04 Strategy Studio backend: CP04.1 persistence, CP04.2 reconciliation, CP04.3 comparison."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from crypto_decision_lab import studio_analytics as analytics
from crypto_decision_lab.execution_contract import ExecutionHypothesis
from crypto_decision_lab.simulation import MarketEvent, RunConfig, market_events, run
from crypto_decision_lab.studio import Studio, StudioError, compare_runs, contiguous_segments, normalize_config

STUDIO_RESEARCH_ID = "BTCUSDT-1h-raw-v1"  # see tests/conftest.py::make_studio_repo
STUDIO_FORWARD_ID = "BTCUSDT-1h-forward-v1"

CONSTANT = {
    "strategy": {"name": "constant", "parameters": {"exposure": "LONG"}},
    "initial_cash": 10000,
    "hypothesis": {"latency_hours": 0, "horizon_hours": 6, "fee_rate": 0.001, "slippage": 0.0005},
    "risk": {"max_exposure": 1.0, "min_notional": 1.0, "allow_open": True},
}
MOMENTUM = {**CONSTANT, "strategy": {"name": "momentum", "parameters": {"threshold": 0.0}}}


def studio_for(repo: Path) -> Studio:
    return Studio("var/studio", repo_root=repo)


# --------------------------------------------------------------- configs
class TestImmutableConfigs:
    def test_same_content_same_id_and_file_untouched(self, studio_repo):
        studio = studio_for(studio_repo)
        first = studio.save_config("Costante", CONSTANT)
        before = first.path.read_bytes()
        again = studio.save_config("Costante", json.loads(json.dumps(CONSTANT)))
        assert again.config_id == first.config_id
        assert first.path.read_bytes() == before
        assert len(studio.list_configs()) == 1

    def test_numeric_spelling_does_not_change_identity(self, studio_repo):
        studio = studio_for(studio_repo)
        as_float = json.loads(json.dumps(CONSTANT))
        as_float["initial_cash"] = 10000.0
        as_float["risk"]["max_exposure"] = 1
        assert studio.save_config("x", CONSTANT).digest == studio.save_config("x", as_float).digest

    def test_changed_parameter_is_a_new_configuration(self, studio_repo):
        studio = studio_for(studio_repo)
        a = studio.save_config("m", MOMENTUM)
        b = studio.save_config("m", {**MOMENTUM, "strategy": {"name": "momentum", "parameters": {"threshold": 0.01}}})
        assert a.config_id != b.config_id
        assert {c.config_id for c in studio.list_configs()} == {a.config_id, b.config_id}

    def test_tampered_configuration_is_refused(self, studio_repo):
        studio = studio_for(studio_repo)
        saved = studio.save_config("c", CONSTANT)
        record = json.loads(saved.path.read_text(encoding="utf-8"))
        record["config"]["hypothesis"]["fee_rate"] = 0.0
        saved.path.write_text(json.dumps(record), encoding="utf-8")
        with pytest.raises(StudioError, match="alterata"):
            studio.load_config(saved.config_id)
        assert studio.list_configs() == []

    def test_invalid_configuration_is_refused(self, studio_repo):
        studio = studio_for(studio_repo)
        with pytest.raises(StudioError):
            studio.save_config("bad", {**CONSTANT, "strategy": {"name": "absent"}})
        with pytest.raises(StudioError):
            studio.save_config("", CONSTANT)


# --------------------------------------------------------------- sources and runs
class TestSourcesAndRuns:
    def test_studio_exposes_only_authorized_sources(self, studio_repo):
        studio = studio_for(studio_repo)
        assert studio.authorized_sources() == [STUDIO_RESEARCH_ID]
        statuses = {s.dataset_id: s for s in studio.source_statuses()}
        assert not statuses[STUDIO_FORWARD_ID].admitted
        with pytest.raises(StudioError, match="non autorizzata"):
            studio.load_market(STUDIO_FORWARD_ID)

    def test_market_of_another_source_cannot_be_injected(self, studio_repo):
        studio = studio_for(studio_repo)
        saved = studio.save_config("c", CONSTANT)
        market = studio.load_market(STUDIO_RESEARCH_ID)
        with pytest.raises(StudioError):
            studio.run_backtest(saved.config_id, STUDIO_FORWARD_ID, market=market)

    def test_storage_inside_forward_archive_is_refused(self, studio_repo):
        with pytest.raises(StudioError):
            Studio(f"data/forward/{STUDIO_FORWARD_ID}/studio", repo_root=studio_repo)

    def test_cp041_run_is_persisted_and_reread_by_a_new_instance(self, studio_repo):
        studio = studio_for(studio_repo)
        saved = studio.save_config("c", CONSTANT)
        stored = studio.run_backtest(saved.config_id, STUDIO_RESEARCH_ID,
                                     start="2024-01-31", end_exclusive="2024-02-02")
        assert stored.summary["trades"] > 0
        assert stored.manifest["studio"]["config_id"] == saved.config_id
        assert stored.manifest["studio"]["first_event"] == "2024-01-31 00:00:00+00:00"
        assert stored.manifest["studio"]["last_event"] == "2024-02-01 23:00:00+00:00"
        assert "source_manifest_sha256" in stored.manifest
        assert stored.manifest["studio"]["dataset_id"] == STUDIO_RESEARCH_ID
        reread = Studio("var/studio", repo_root=studio_repo).load_run(stored.run_id)
        assert reread.ledger == stored.ledger
        assert reread.summary == analytics.summarize(reread.ledger, reread.initial_cash)

    def test_tampered_ledger_is_detected(self, studio_repo):
        studio = studio_for(studio_repo)
        stored = studio.run_backtest(studio.save_config("c", CONSTANT).config_id, STUDIO_RESEARCH_ID)
        ledger = stored.path / "ledger.json"
        ledger.write_text(ledger.read_text(encoding="utf-8").replace('"BUY"', '"SELL"', 1), encoding="utf-8")
        with pytest.raises(StudioError, match="alterato"):
            studio.load_run(stored.run_id)
        assert studio.list_runs() == []

    def test_studio_events_match_engine_cli_definition(self, studio_repo):
        studio = studio_for(studio_repo)
        frame = studio.load_market(STUDIO_RESEARCH_ID).frame
        full = market_events(frame)
        period = market_events(frame, "2024-01-31", "2024-02-01")
        by_ts = {e.timestamp: e for e in full}
        assert period and all(by_ts[e.timestamp] == e for e in period)
        expected = frame["close"].pct_change(24).shift(1)
        assert full[30].features["momentum_24h"] == pytest.approx(expected.iloc[30])
        assert full[10].features == {}

    def test_momentum_is_not_computed_across_a_gap(self):
        index = pd.date_range("2024-01-01", periods=60, freq="h", tz="UTC").delete(40)
        frame = pd.DataFrame({"open": 100.0, "close": np.arange(59, dtype=float) + 100}, index=index)
        events = {e.timestamp: e for e in market_events(frame)}
        missing = pd.Timestamp("2024-01-02 16:00", tz="UTC")  # position 40
        assert missing not in events
        assert events[missing + pd.Timedelta(hours=1)].features == {}  # close(t-1h) is the missing bar
        # row-based pct_change would have compared closes 25 rows apart = 26 hours apart
        assert events[missing + pd.Timedelta(hours=10)].features["momentum_24h"] == pytest.approx(
            frame["close"].asfreq("h").pct_change(24, fill_method=None).shift(1)[missing + pd.Timedelta(hours=10)]
        )
        assert len(contiguous_segments(index)) == 2


# --------------------------------------------------------------- CP04.2
PRICES = (100.0, 110.0, 90.0, 95.0, 120.0, 100.0)
FEE = 0.001


def fixture_run(prices=PRICES, start="2024-01-31 22:00"):
    events = [MarketEvent(t, p) for t, p in zip(pd.date_range(start, periods=len(prices), freq="h", tz="UTC"), prices)]
    config = RunConfig(strategy={"name": "constant"},
                       hypothesis=ExecutionHypothesis(latency_hours=0, horizon_hours=2, fee_rate=FEE, slippage=0.0))
    return run(events, config).ledger


class TestCP042Reconciliation:
    """Hand-computed fixture: buy@100, horizon sell@90, buy@95, horizon sell@100."""

    q1 = 10000 / (100 * (1 + FEE))
    cash1 = q1 * 90 * (1 - FEE)
    q2 = cash1 / (95 * (1 + FEE))
    cash2 = q2 * 100 * (1 - FEE)
    equity = np.array([q1 * 100, q1 * 110, cash1, q2 * 95, q2 * 120, cash2])

    def test_aggregate_metrics_match_hand_calculation(self):
        ledger = fixture_run()
        summary = analytics.summarize(ledger, 10000)
        assert summary["net_pnl"] == pytest.approx(self.cash2 - 10000)
        expected_fees = FEE * (self.q1 * 100 + self.q1 * 90 + self.q2 * 95 + self.q2 * 100)
        assert summary["fees"] == pytest.approx(expected_fees)
        assert summary["costs"] == pytest.approx(expected_fees)
        series = np.concatenate([[10000.0], self.equity])
        peak = np.maximum.accumulate(series)
        assert summary["max_drawdown_mtm_pct"] == pytest.approx(100 * np.min(series / peak - 1))
        assert summary["time_in_market_pct"] == pytest.approx(100 * 4 / 6)
        assert summary["trades"] == 2 and summary["closed_trades"] == 2
        curve = analytics.equity_curve(ledger, 10000)
        assert curve.to_numpy() == pytest.approx(self.equity)

    def test_trade_log_reasons_and_pnl(self):
        trade_frame = analytics.trades(fixture_run())
        assert list(trade_frame["entry_price"]) == [100.0, 95.0]
        assert list(trade_frame["exit_price"]) == [90.0, 100.0]
        assert trade_frame["pnl"].iloc[0] == pytest.approx(self.cash1 - 10000)
        assert all(trade_frame["entry_reason"] == "constant LONG")
        assert all(trade_frame["exit_reason"] == "holding horizon reached")

    def test_windows_split_and_reconcile(self):
        ledger = fixture_run()
        windows = analytics.window_summary(ledger, 10000, "M")
        assert list(windows["window"]) == ["2024-01", "2024-02"]
        assert windows["pnl"].iloc[0] == pytest.approx(self.q1 * 110 - 10000)
        assert windows["pnl"].iloc[1] == pytest.approx(self.cash2 - self.q1 * 110)
        assert windows["trades_opened"].tolist() == [1, 1]
        assert windows["trades_closed"].tolist() == [0, 2]
        assert all(check["passed"] for check in analytics.reconcile(ledger, 10000, "M"))

    def test_open_position_is_valued_mark_to_market(self):
        ledger = fixture_run(prices=(100.0, 110.0))
        trade_frame = analytics.trades(ledger)
        assert trade_frame["status"].tolist() == ["OPEN"]
        summary = analytics.summarize(ledger, 10000)
        assert summary["net_pnl"] == pytest.approx(self.q1 * 110 - 10000)
        assert all(check["passed"] for check in analytics.reconcile(ledger, 10000))

    def test_reconciliation_on_a_persisted_real_engine_run(self, studio_repo):
        studio = studio_for(studio_repo)
        stored = studio.run_backtest(studio.save_config("m", MOMENTUM).config_id, STUDIO_RESEARCH_ID)
        for freq in analytics.WINDOW_FREQUENCIES:
            assert all(check["passed"] for check in analytics.reconcile(stored.ledger, stored.initial_cash, freq))


# --------------------------------------------------------------- CP04.3
class TestCP043Comparison:
    def test_same_conditions_are_comparable(self, studio_repo):
        studio = studio_for(studio_repo)
        a = studio.run_backtest(studio.save_config("c", CONSTANT).config_id, STUDIO_RESEARCH_ID,
                                start="2024-01-31", end_exclusive="2024-02-02")
        b = studio.run_backtest(studio.save_config("m", MOMENTUM).config_id, STUDIO_RESEARCH_ID,
                                start="2024-01-31", end_exclusive="2024-02-02")
        report = compare_runs([a, b])
        assert report["comparable"] and report["differing_conditions"] == []
        assert next(r for r in report["subject"] if r["key"] == "strategy")["differs"]

    def test_period_change_is_visible(self, studio_repo):
        studio = studio_for(studio_repo)
        config_id = studio.save_config("c", CONSTANT).config_id
        a = studio.run_backtest(config_id, STUDIO_RESEARCH_ID, start="2024-01-31", end_exclusive="2024-02-02")
        b = studio.run_backtest(config_id, STUDIO_RESEARCH_ID, start="2024-01-31", end_exclusive="2024-02-01")
        report = compare_runs([a, b])
        assert not report["comparable"]
        assert "Periodo richiesto · fine (esclusa)" in report["differing_conditions"]
        assert "Ultimo evento" in report["differing_conditions"]

    def test_cost_model_change_is_visible(self, studio_repo):
        studio = studio_for(studio_repo)
        costly = json.loads(json.dumps(CONSTANT))
        costly["hypothesis"]["fee_rate"] = 0.002
        a = studio.run_backtest(studio.save_config("c", CONSTANT).config_id, STUDIO_RESEARCH_ID)
        b = studio.run_backtest(studio.save_config("c", costly).config_id, STUDIO_RESEARCH_ID)
        report = compare_runs([a, b])
        assert report["differing_conditions"] == ["Fee per lato"]
        assert report["metrics"][0]["costs"] < report["metrics"][1]["costs"]

    def test_comparison_requires_two_runs(self, studio_repo):
        studio = studio_for(studio_repo)
        a = studio.run_backtest(studio.save_config("c", CONSTANT).config_id, STUDIO_RESEARCH_ID)
        with pytest.raises(StudioError):
            compare_runs([a])


def test_normalize_config_is_canonical():
    assert normalize_config(CONSTANT) == normalize_config(json.loads(json.dumps(CONSTANT)))
