"""S04 Strategy Studio: Strategie, Nuovo backtest, Risultati, Confronto.

The view never computes economics itself: it saves immutable configurations,
asks :class:`crypto_decision_lab.studio.Studio` to run the S03 engine on an
admitted catalog source and reads back the persisted run artefacts, so a new
session shows exactly what was stored on disk.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

from ... import studio_analytics as analytics
from ...simulation import STRATEGIES
from ...studio import RUN_MODES, Studio, StudioError, compare_runs, contiguous_segments
from ..charts import make_drawdown_figure, make_studio_equity_figure, make_studio_trades_figure
from .common import download_button, fmt_number, fmt_pct, fmt_price, show_plot

SELECTED_RUN_KEY = "studio_selected_run"
STRATEGY_HELP = {
    "constant": "Esposizione costante LONG o FLAT: riferimento deterministico.",
    "momentum": "LONG se momentum_24h (frazione, 0.05 = 5%) supera la soglia, altrimenti FLAT.",
    "scheduled_momentum": "S05: decide solo ai decision point (cutoff S02). Gli eventi orari dello Studio non li "
                          "contengono, quindi qui non apre posizioni: si usa negli esperimenti S05.",
    "model_filtered_momentum": "S05: momentum filtrato dallo score del Decision Model. Parametri JSON: model_path, "
                               "model_sha256, threshold, score_threshold. Richiede decision point (esperimenti S05).",
}


@st.cache_data(ttl=600, show_spinner=False)
def _source_statuses(root: str, repo_root: str, catalog_path: str):
    return Studio(root, repo_root=repo_root, catalog_path=catalog_path).source_statuses()


@st.cache_data(ttl=600, show_spinner=False)
def _market(root: str, repo_root: str, catalog_path: str, dataset_id: str):
    return Studio(root, repo_root=repo_root, catalog_path=catalog_path).load_market(dataset_id)


# --------------------------------------------------------------------- views
def _strategy_parameters(name: str) -> dict:
    if name == "momentum":
        threshold = st.number_input(
            "Soglia momentum (frazione)", value=0.05, step=0.01, format="%.4f", key="studio_param_threshold"
        )
        feature = st.selectbox("Feature", options=["momentum_24h"], key="studio_param_feature")
        return {"threshold": float(threshold), "feature": feature}
    if name == "constant":
        exposure = st.selectbox("Esposizione", options=["LONG", "FLAT"], key="studio_param_exposure")
        return {"exposure": exposure}
    raw = st.text_area("Parametri (JSON)", value="{}", key="studio_param_json")
    try:
        return dict(json.loads(raw or "{}"))
    except (json.JSONDecodeError, TypeError, ValueError):
        st.error("Parametri JSON non validi")
        return {}


def _render_strategies(studio: Studio) -> None:
    st.markdown("##### Nuova configurazione")
    st.caption(
        "Una configurazione salvata è immutabile: l'identificativo contiene lo SHA-256 del contenuto "
        "normalizzato. Modificare un valore crea una nuova configurazione."
    )
    name = st.text_input("Nome", value="", key="studio_config_name", placeholder="es. momentum-5pct")
    strategy = st.selectbox("Strategia", options=sorted(STRATEGIES), key="studio_strategy")
    st.caption(STRATEGY_HELP.get(strategy, "Strategia registrata in simulation.STRATEGIES."))
    parameters = _strategy_parameters(strategy)
    left, middle, right = st.columns(3)
    with left:
        st.markdown("**Capitale e rischio**")
        initial_cash = st.number_input("Capitale iniziale (USDT)", min_value=1.0, value=10000.0, step=1000.0, key="studio_cash")
        max_exposure = st.number_input("Esposizione massima", min_value=0.0, max_value=1.0, value=1.0, step=0.05, key="studio_max_exposure")
        min_notional = st.number_input("Nozionale minimo (USDT)", min_value=0.0, value=10.0, step=1.0, key="studio_min_notional")
        allow_open = st.checkbox("Aperture consentite", value=True, key="studio_allow_open")
    with middle:
        st.markdown("**Esecuzione**")
        latency = st.number_input("Latenza (ore)", min_value=0, value=1, step=1, key="studio_latency")
        horizon = st.number_input("Orizzonte di detenzione (ore)", min_value=1, value=24, step=1, key="studio_horizon")
    with right:
        st.markdown("**Costi per lato**")
        fee = st.number_input("Fee", min_value=0.0, max_value=0.1, value=0.001, step=0.0005, format="%.4f", key="studio_fee")
        slippage = st.number_input("Slippage", min_value=0.0, max_value=0.1, value=0.0005, step=0.0005, format="%.4f", key="studio_slippage")
    raw = {
        "strategy": {"name": strategy, "parameters": parameters},
        "initial_cash": float(initial_cash),
        "hypothesis": {"latency_hours": int(latency), "horizon_hours": int(horizon), "fee_rate": float(fee), "slippage": float(slippage)},
        "risk": {"max_exposure": float(max_exposure), "min_notional": float(min_notional), "allow_open": bool(allow_open)},
    }
    if st.button("Salva configurazione", key="studio_save_config", type="primary"):
        try:
            saved = studio.save_config(name, raw)
            st.session_state["studio_backtest_config"] = saved.config_id
            st.success(f"Configurazione salvata: `{saved.config_id}`")
        except StudioError as exc:
            st.error(str(exc))

    st.markdown("##### Configurazioni salvate")
    configs = studio.list_configs()
    if not configs:
        st.info("Nessuna configurazione salvata.")
        return
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "ID": item.config_id,
                    "Nome": item.name,
                    "Strategia": item.config["strategy"]["name"],
                    "Parametri": json.dumps(item.config["strategy"]["parameters"], sort_keys=True),
                    "Latenza h": item.config["hypothesis"]["latency_hours"],
                    "Orizzonte h": item.config["hypothesis"]["horizon_hours"],
                    "Fee": item.config["hypothesis"]["fee_rate"],
                    "Slippage": item.config["hypothesis"]["slippage"],
                    "Max exp.": item.config["risk"]["max_exposure"],
                    "Creata (UTC)": item.created_utc,
                }
                for item in configs
            ]
        ),
        width="stretch",
        hide_index=True,
    )
    with st.expander("Dettaglio configurazione", expanded=False):
        chosen = st.selectbox("Configurazione", [item.config_id for item in configs], key="studio_config_detail")
        detail = next(item for item in configs if item.config_id == chosen)
        st.caption(f"SHA-256: `{detail.digest}` · file `{detail.path}`")
        st.json(detail.config)


def _render_new_backtest(studio: Studio, repo_root: str, catalog_path: str) -> None:
    configs = studio.list_configs()
    if not configs:
        st.info("Salva prima una configurazione nella vista Strategie.")
        return
    try:
        statuses = _source_statuses(str(studio.root), repo_root, catalog_path)
    except Exception as exc:  # catalog missing or malformed: nothing is offered
        st.error(f"Catalogo dati non leggibile: {exc}")
        return
    authorized = [status.dataset_id for status in statuses if status.admitted]
    excluded = [status for status in statuses if not status.admitted]
    config_ids = [item.config_id for item in configs]
    preferred = st.session_state.get("studio_backtest_config")
    config_id = st.selectbox(
        "Configurazione",
        options=config_ids,
        index=config_ids.index(preferred) if preferred in config_ids else len(config_ids) - 1,
        format_func=lambda value: next(item.label() for item in configs if item.config_id == value),
        key="studio_run_config",
    )
    if not authorized:
        st.error("Nessuna fonte di ricerca ammessa dal catalogo.")
        return
    dataset_id = st.selectbox("Dataset ammesso", options=authorized, key="studio_run_dataset")
    if excluded:
        with st.expander(f"Fonti escluse dallo Studio ({len(excluded)})", expanded=False):
            for status in excluded:
                st.caption(f"{status.dataset_id} · ruolo {status.role} · {status.reason}")
    try:
        market = _market(str(studio.root), repo_root, catalog_path, dataset_id)
    except Exception as exc:
        st.error(f"Impossibile caricare {dataset_id}: {exc}")
        return
    frame = market.frame
    segments = contiguous_segments(frame.index)
    longest = segments.sort_values("hours", ascending=False).head(8)
    last_segment = segments.iloc[-1]
    st.caption(
        f"{market.dataset_id} · {len(frame):,} barre · {frame.index[0]:%Y-%m-%d} → {frame.index[-1]:%Y-%m-%d} · "
        f"{market.gap_count} ore mancanti · manifest sha256 `{str(market.source_manifest_sha256)[:16]}…`"
    )
    default_end = last_segment["end"]
    default_start = max(last_segment["start"], default_end - pd.Timedelta(days=180))
    left, right, mode_col = st.columns(3)
    start_date = left.date_input(
        "Inizio (UTC, incluso)", value=default_start.date(), min_value=frame.index[0].date(),
        max_value=frame.index[-1].date(), key="studio_run_start",
    )
    end_date = right.date_input(
        "Fine (UTC, inclusa)", value=default_end.date(), min_value=frame.index[0].date(),
        max_value=frame.index[-1].date(), key="studio_run_end",
    )
    mode = mode_col.radio("Modalità", options=list(RUN_MODES), horizontal=True, key="studio_run_mode")
    start = pd.Timestamp(start_date, tz="UTC")
    end_exclusive = pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1)
    in_period = frame.index[(frame.index >= start) & (frame.index < end_exclusive)]
    period_segments = contiguous_segments(in_period)
    if len(period_segments) > 1:
        first_gap = period_segments.iloc[0]["end"] + pd.Timedelta(hours=1)
        st.warning(
            f"Il periodo contiene {len(period_segments) - 1} gap: il motore S03 si arresta (HALTED_GAP) "
            f"al primo, dopo {first_gap:%Y-%m-%d %H:%M} UTC. Scegli un segmento contiguo per un run completo."
        )
    with st.expander("Segmenti contigui più lunghi", expanded=False):
        st.dataframe(longest, width="stretch", hide_index=True)
    if st.button("Avvia run", key="studio_run_button", type="primary"):
        try:
            with st.spinner("Simulazione in corso…"):
                stored = studio.run_backtest(
                    config_id, dataset_id, start=start, end_exclusive=end_exclusive, mode=mode, market=market
                )
            st.session_state[SELECTED_RUN_KEY] = stored.run_id
            st.success(
                f"Run `{stored.run_id}` salvato · esito {stored.summary['status']} · "
                f"{stored.summary['trades']} trade. Apri la vista Risultati."
            )
        except (StudioError, ValueError) as exc:
            st.error(str(exc))


def _curve_with_start(stored) -> pd.Series:
    curve = analytics.equity_curve(stored.ledger, stored.initial_cash)
    if curve.empty:
        return curve
    start = pd.Series([stored.initial_cash], index=[curve.index[0] - pd.Timedelta(hours=1)])
    return pd.concat([start, curve])


def _render_results(studio: Studio) -> None:
    runs = studio.list_runs()
    if not runs:
        st.info("Nessun run salvato. Avviane uno dalla vista Nuovo backtest.")
        return
    ids = [item.run_id for item in runs]
    preferred = st.session_state.get(SELECTED_RUN_KEY)
    run_id = st.selectbox(
        "Run",
        options=ids,
        index=ids.index(preferred) if preferred in ids else 0,
        format_func=lambda value: next(item.label() for item in runs if item.run_id == value),
        key="studio_results_run",
    )
    stored = next(item for item in runs if item.run_id == run_id)
    ledger, cash = stored.ledger, stored.initial_cash
    summary = analytics.summarize(ledger, cash)
    studio_meta = stored.manifest.get("studio", {})
    hypothesis = stored.manifest["config"]["hypothesis"]
    st.caption(
        f"Dataset `{stored.manifest.get('source_id')}` · periodo {summary['first_event']} → {summary['last_event']} · "
        f"configurazione `{studio_meta.get('config_id')}` · modalità {stored.manifest.get('mode')} · "
        f"latenza {hypothesis['latency_hours']}h · orizzonte {hypothesis['horizon_hours']}h · "
        f"fee {hypothesis['fee_rate']:.4%} · slippage {hypothesis['slippage']:.4%} per lato"
    )
    if summary["status"] != "COMPLETED":
        st.warning(f"Esito {summary['status']}: il run si è fermato prima della fine del periodo richiesto.")
    columns = st.columns(6)
    columns[0].metric("P&L netto", fmt_price(summary["net_pnl"]), fmt_pct(summary["net_return_pct"]))
    columns[1].metric("Max drawdown MTM", fmt_number(summary["max_drawdown_mtm_pct"], 2) + "%")
    columns[2].metric("Costi", fmt_price(summary["costs"]), help=f"Fee {summary['fees']:.2f} · slippage {summary['slippage']:.2f}")
    columns[3].metric("Esposizione", fmt_number(summary["time_in_market_pct"], 1) + "%", help=f"Esposizione media {summary['avg_exposure_pct']:.1f}% dell'equity")
    columns[4].metric("Trade", f"{summary['trades']}", help=f"Chiusi {summary['closed_trades']} · aperti {summary['open_trades']}")
    win = summary["win_rate_pct"]
    columns[5].metric("Win rate", "n/d" if win is None else fmt_number(win, 1) + "%")

    curve = _curve_with_start(stored)
    trade_frame = analytics.trades(ledger)
    equity = analytics.equity_frame(ledger)
    left, right = st.columns(2)
    with left:
        show_plot(make_studio_equity_figure({run_id: curve}), key="studio_equity")
    with right:
        show_plot(make_drawdown_figure(curve, name=run_id), key="studio_drawdown")
    show_plot(make_studio_trades_figure(equity["open"] if not equity.empty else pd.Series(dtype=float), trade_frame), key="studio_trades_chart")

    st.markdown("##### Registro operazioni")
    if trade_frame.empty:
        st.info("Nessuna operazione eseguita in questo run.")
    else:
        st.dataframe(trade_frame.round(6), width="stretch", hide_index=True)
        download_button(trade_frame, filename=f"{run_id}-trades.csv", label="Scarica operazioni (CSV)", key="studio_trades_csv")

    st.markdown("##### Risultati per finestra")
    freq = st.radio(
        "Finestra", options=list(analytics.WINDOW_FREQUENCIES), format_func=analytics.WINDOW_FREQUENCIES.get,
        horizontal=True, key="studio_window_freq",
    )
    windows = analytics.window_summary(ledger, cash, freq)
    st.dataframe(windows.round(4), width="stretch", hide_index=True)

    with st.expander("Riconciliazione con il registro (CP04.2)", expanded=False):
        checks = pd.DataFrame(analytics.reconcile(ledger, cash, freq))
        if checks["passed"].all():
            st.success("Tutti i controlli di riconciliazione superati.")
        else:
            st.error("Riconciliazione fallita: vedere i controlli.")
        st.dataframe(checks, width="stretch", hide_index=True)
    with st.expander("Decisioni e motivazioni", expanded=False):
        decision_frame = analytics.decisions(ledger)
        only_changes = st.checkbox("Solo decisioni accettate dal rischio", value=True, key="studio_decisions_accepted")
        if only_changes:
            decision_frame = decision_frame[decision_frame["risk_accepted"]]
        st.dataframe(decision_frame, width="stretch", hide_index=True)
    with st.expander("Manifest del run", expanded=False):
        st.caption(f"Directory `{stored.path}`")
        st.json(stored.manifest)


def _render_comparison(studio: Studio) -> None:
    runs = studio.list_runs()
    if len(runs) < 2:
        st.info("Servono almeno due run salvati per il confronto.")
        return
    ids = [item.run_id for item in runs]
    selected = st.multiselect(
        "Run da confrontare",
        options=ids,
        default=ids[:2],
        format_func=lambda value: next(item.label() for item in runs if item.run_id == value),
        key="studio_compare_runs",
    )
    if len(selected) < 2:
        st.info("Seleziona almeno due run.")
        return
    chosen = [next(item for item in runs if item.run_id == run_id) for run_id in selected]
    report = compare_runs(chosen)
    if report["comparable"]:
        st.success("Run confrontabili a parità di condizioni: dataset, periodo, ingressi, ipotesi di esecuzione, capitale e motore coincidono.")
    else:
        st.warning("Condizioni diverse tra i run: " + ", ".join(report["differing_conditions"]) + ". Il confronto non è a parità di condizioni.")

    def frame(rows):
        return pd.DataFrame(
            [
                {"Voce": row["label"], "Differisce": "⚠ sì" if row["differs"] else "", **{
                    run_id: "" if value is None else str(value) for run_id, value in zip(selected, row["values"])
                }}
                for row in rows
            ]
        )

    st.markdown("##### Condizioni del confronto")
    st.dataframe(frame(report["conditions"]), width="stretch", hide_index=True)
    st.markdown("##### Oggetto del confronto")
    st.dataframe(frame(report["subject"]), width="stretch", hide_index=True)
    st.markdown("##### Metriche")
    metrics = pd.DataFrame(report["metrics"])[
        ["run_id", "status", "first_event", "last_event", "net_pnl", "net_return_pct", "max_drawdown_mtm_pct",
         "costs", "time_in_market_pct", "trades", "win_rate_pct"]
    ]
    st.dataframe(metrics.round(4), width="stretch", hide_index=True)
    show_plot(
        make_studio_equity_figure({item.run_id: _curve_with_start(item) for item in chosen}, title="Equity a confronto"),
        key="studio_compare_equity",
    )


def render_strategy_studio(
    repo_root: str | Path = ".",
    *,
    studio_root: str | Path | None = None,
    catalog_path: str | Path | None = None,
    paper_root: str = "var/paper",
    feeds_root: str = "var/lab-feeds",
) -> None:
    st.subheader("Strategy Studio · S04")
    st.caption(
        "Configura strategie immutabili, avvia backtest o paper replay con il motore S03 su fonti ammesse "
        "dal catalogo S02 e confronta i run salvati. Le fonti forward riservate a CP08 non sono mai esposte."
    )
    try:
        studio = Studio(studio_root, repo_root=repo_root, catalog_path=catalog_path)
    except StudioError as exc:
        st.error(str(exc))
        return
    tabs = st.tabs(["Strategie", "Nuovo backtest", "Risultati", "Confronto", "Paper trading"])
    with tabs[0]:
        _render_strategies(studio)
    with tabs[1]:
        _render_new_backtest(studio, str(studio.repo_root), str(studio.catalog_path))
    with tabs[2]:
        _render_results(studio)
    with tabs[3]:
        _render_comparison(studio)
    with tabs[4]:
        from .paper_sessions import render_paper_sessions

        render_paper_sessions(studio, studio.repo_root, root=paper_root, feeds_root=feeds_root)
