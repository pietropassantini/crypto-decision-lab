"""S04 CP04.1/CP04.3: Strategy Studio UI driven through Streamlit AppTest."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest  # noqa: E402

SRC = Path(__file__).resolve().parents[2] / "src"


def _app(repo: Path) -> AppTest:
    code = (
        "import sys\n"
        f"sys.path.insert(0, r'{SRC.as_posix()}')\n"
        "from crypto_decision_lab.dashboard.components.strategy_studio import render_strategy_studio\n"
        f"render_strategy_studio(r'{repo.as_posix()}', studio_root='var/studio')\n"
    )
    return AppTest.from_string(code, default_timeout=120)


def _metric(app: AppTest, label: str):
    return next(metric for metric in app.metric if metric.label == label)


def _save_constant_config(app: AppTest) -> None:
    app.text_input(key="studio_config_name").set_value("costante-long")
    app.selectbox(key="studio_strategy").set_value("constant")
    app.run()
    app.selectbox(key="studio_param_exposure").set_value("LONG")
    app.number_input(key="studio_latency").set_value(1)
    app.number_input(key="studio_horizon").set_value(6)
    app.button(key="studio_save_config").click()
    app.run()


def test_cp041_save_config_run_open_trade_and_reread_in_new_session(studio_repo: Path) -> None:
    app = _app(studio_repo)
    app.run()
    assert list(app.exception) == []
    assert [tab.label for tab in app.tabs] == ["Strategie", "Nuovo backtest", "Risultati", "Confronto", "Paper trading"]

    _save_constant_config(app)
    assert list(app.exception) == []
    assert any("Configurazione salvata" in item.value for item in app.success)
    configs = list((studio_repo / "var" / "studio" / "configs").glob("*.json"))
    assert len(configs) == 1
    saved = json.loads(configs[0].read_text(encoding="utf-8"))
    assert saved["config"]["strategy"] == {"name": "constant", "parameters": {"exposure": "LONG"}}

    # Only the admitted research source is offered.
    assert app.selectbox(key="studio_run_dataset").options == ["BTCUSDT-1h-raw-v1"]
    app.button(key="studio_run_button").click()
    app.run()
    assert list(app.exception) == []
    assert any("Run `" in item.value for item in app.success)
    runs = [path for path in (studio_repo / "var" / "studio" / "runs").iterdir() if path.is_dir()]
    assert len(runs) == 1
    ledger = json.loads((runs[0] / "ledger.json").read_text(encoding="utf-8"))
    assert any(record["kind"] == "execution" and record["side"] == "BUY" for record in ledger)
    trades_shown = int(_metric(app, "Trade").value)
    pnl_shown = _metric(app, "P&L netto").value
    assert trades_shown > 0

    # New session: results are read back from disk, not from session state.
    fresh = _app(studio_repo)
    fresh.run()
    assert list(fresh.exception) == []
    assert fresh.selectbox(key="studio_results_run").value == runs[0].name
    assert int(_metric(fresh, "Trade").value) == trades_shown
    assert _metric(fresh, "P&L netto").value == pnl_shown
    assert any("riconciliazione superati" in item.value for item in fresh.success)


def test_cp062_paper_tab_shows_persisted_state_and_runner_works_without_ui(studio_repo: Path) -> None:
    import subprocess
    import sys

    from crypto_decision_lab.lab_feed import FEED_SCHEMA, LabFeed
    from crypto_decision_lab.paper import PaperSession

    feed = LabFeed.create("var/lab-feeds/ui", {
        "schema_version": FEED_SCHEMA, "feed_id": "ui", "source_dataset_id": "BTCUSDT-1h-raw-v1",
        "start": "2024-01-30 00:00", "end": "2024-02-03 00:00",
        "faults": [{"type": "gap", "at": "2024-01-31 06:00"}]}, repo_root=studio_repo)
    session = PaperSession.create("ui", {"strategy": {"name": "constant"}},
                                  {"kind": "lab_feed", "path": "var/lab-feeds/ui"},
                                  start_at="2024-01-31 02:00", repo_root=studio_repo)
    feed.advance(until="2024-01-31 04:00")
    # Runner without UI (CLI), as an operator would schedule it.
    result = subprocess.run([sys.executable, "-m", "crypto_decision_lab.paper", "--repo-root", str(studio_repo),
                             "session-run", "--session-dir", str(session.directory)],
                            capture_output=True, text=True, timeout=300,
                            env={**__import__("os").environ, "PYTHONPATH": str(SRC)})
    assert result.returncode == 0, result.stderr
    assert '"WAITING"' in result.stdout

    app = _app(studio_repo)
    app.run()
    assert list(app.exception) == []
    assert any("Stato **WAITING**" in item.value for item in app.success)
    assert _metric(app, "Ultimo evento").value.startswith("2024-01-31 04:00")
    app.number_input(key="paper_feed_hours").set_value(4)
    app.button(key="paper_feed_advance").click()
    app.run()
    assert any("Stato **PAUSED_GAP**" in item.value for item in app.warning)
    app.button(key="paper_ack").click()
    app.run()
    assert list(app.exception) == []
    fresh = _app(studio_repo)
    fresh.run()
    assert any("RUNNING" in item.value or "WAITING" in item.value for item in fresh.success)
    assert any(row["kind"] == "gap_acknowledged" for row in PaperSession(session.directory, repo_root=studio_repo).ledger())


def test_cp043_comparison_view_flags_period_change(studio_repo: Path) -> None:
    from crypto_decision_lab.studio import Studio

    studio = Studio("var/studio", repo_root=studio_repo)
    config = studio.save_config("c", {"strategy": {"name": "constant"}})
    studio.run_backtest(config.config_id, "BTCUSDT-1h-raw-v1", start="2024-01-31", end_exclusive="2024-02-02")
    studio.run_backtest(config.config_id, "BTCUSDT-1h-raw-v1", start="2024-01-31", end_exclusive="2024-02-01")
    app = _app(studio_repo)
    app.run()
    assert list(app.exception) == []
    assert any("Condizioni diverse" in item.value and "Periodo richiesto" in item.value for item in app.warning)
