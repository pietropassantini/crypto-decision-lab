"""S06 view inside Strategy Studio: persistent paper sessions.

The UI never owns session state: it opens the session database, shows the
persisted status and issues the same commands as the CLI runner
(``python -m crypto_decision_lab.paper session-run``), which keeps working
when the UI is closed.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from ... import studio_analytics as analytics
from ...lab_feed import LabFeed
from ...paper import BLOCKING, PaperSession, SessionError, list_sessions
from ..charts import make_studio_equity_figure
from .common import fmt_pct, fmt_price, show_plot

STATUS_HELP = {
    "CREATED": "creata, nessun evento elaborato",
    "RUNNING": "in elaborazione",
    "WAITING": "aggiornata: attende la prossima barra",
    "STALE": "feed fermo oltre la soglia di obsolescenza",
    "PAUSED_GAP": "manca una barra mentre ne esistono di successive: attendere il recupero o riconoscere il gap",
    "DATA_ERROR": "barra invalida, in conflitto o revisionata: nessun avanzamento",
    "SUSPENDED": "sospesa dall'operatore",
    "MODEL_ERROR": "strategia o modello fissati all'avvio non disponibili",
    "ENGINE_MISMATCH": "revisione del motore diversa da quella fissata all'avvio",
    "COMPLETED": "chiusa",
}


def _feeds(repo_root: Path, feeds_root: str) -> list[Path]:
    base = repo_root / feeds_root
    return sorted(path for path in base.iterdir() if (path / "feed.json").is_file()) if base.is_dir() else []


def _create_form(studio, repo_root: Path, root: str, feeds_root: str) -> None:
    configs = studio.list_configs()
    if not configs:
        st.info("Salva prima una configurazione nella vista Strategie.")
        return
    with st.expander("Nuova sessione paper", expanded=False):
        config_id = st.selectbox("Configurazione", [item.config_id for item in configs], key="paper_new_config")
        name = st.text_input("Nome sessione", value="paper", key="paper_new_name")
        kind = st.radio("Sorgente", ["lab_feed", "snapshot"], horizontal=True, key="paper_new_kind",
                        format_func=lambda value: "Feed di laboratorio" if value == "lab_feed" else "Replay snapshot")
        stale = st.number_input("Obsolescenza dopo (ore)", min_value=1, value=2, step=1, key="paper_new_stale")
        if kind == "lab_feed":
            feeds = _feeds(repo_root, feeds_root)
            if not feeds:
                st.caption(f"Nessun feed in `{feeds_root}`: creane uno con `python -m crypto_decision_lab.paper feed-create`.")
                return
            feed_path = st.selectbox("Feed", feeds, format_func=lambda path: path.name, key="paper_new_feed")
            feed = LabFeed(feed_path)
            start = st.text_input("Inizio (UTC)", value=str(feed.start + pd.Timedelta(hours=25)), key="paper_new_start")
            end = None
            source = {"kind": "lab_feed", "path": str(feed_path.relative_to(repo_root))
                      if feed_path.is_relative_to(repo_root) else str(feed_path)}
        else:
            dataset = st.selectbox("Dataset ammesso", studio.authorized_sources(), key="paper_new_dataset")
            start = st.text_input("Inizio (UTC)", key="paper_new_start_snapshot")
            end = st.text_input("Fine esclusa (UTC)", key="paper_new_end_snapshot")
            source = {"kind": "snapshot", "dataset_id": dataset}
        if st.button("Crea sessione", key="paper_create"):
            saved = studio.load_config(config_id)
            try:
                session = PaperSession.create(name, saved.config, source, start_at=start, end_at=end or None,
                                              stale_after_hours=int(stale), root=root, repo_root=repo_root,
                                              config_id=saved.config_id)
                st.session_state["paper_selected"] = session.directory.name
                st.success(f"Sessione `{session.directory.name}` creata")
            except (SessionError, ValueError, FileExistsError) as exc:
                st.error(str(exc))


def render_paper_sessions(studio, repo_root: str | Path, *, root: str = "var/paper",
                          feeds_root: str = "var/lab-feeds") -> None:
    repo = Path(repo_root).resolve()
    st.caption(
        "Sessioni paper persistenti: stato, cursore ed eventi in SQLite, una transazione per evento. "
        "Il runner `python -m crypto_decision_lab.paper session-run` avanza anche a UI chiusa."
    )
    _create_form(studio, repo, root, feeds_root)
    sessions = list_sessions(root, repo_root=repo)
    if not sessions:
        st.info("Nessuna sessione paper.")
        return
    names = [path.name for path in sessions]
    preferred = st.session_state.get("paper_selected")
    chosen = st.selectbox("Sessione", names, index=names.index(preferred) if preferred in names else 0,
                          key="paper_session")
    session = PaperSession(sessions[names.index(chosen)], repo_root=repo)
    try:
        snapshot = session.snapshot()
        controls = st.columns(5)
        if controls[0].button("Elabora eventi disponibili", key="paper_poll", disabled=snapshot["status"] in BLOCKING):
            snapshot = session.poll()
        if controls[1].button("Riconosci gap e riprendi", key="paper_ack", disabled=snapshot["status"] != "PAUSED_GAP"):
            snapshot = session.acknowledge_gap("acknowledged from Strategy Studio")
        if snapshot["status"] == "SUSPENDED":
            if controls[2].button("Riprendi", key="paper_resume"):
                snapshot = session.resume()
        elif controls[2].button("Sospendi", key="paper_suspend", disabled=snapshot["status"] == "COMPLETED"):
            snapshot = session.suspend("suspended from Strategy Studio")
        if controls[3].button("Chiudi sessione", key="paper_finish", disabled=snapshot["status"] == "COMPLETED"):
            snapshot = session.finish("closed from Strategy Studio")
        if snapshot["source"]["kind"] == "lab_feed":
            hours = controls[4].number_input("Ore feed", min_value=1, value=1, step=1, key="paper_feed_hours")
            if controls[4].button("Avanza feed (lab)", key="paper_feed_advance"):
                feed_path = Path(session.meta["source"]["path"])
                LabFeed(feed_path if feed_path.is_absolute() else repo / feed_path).advance(int(hours))
                snapshot = session.poll()

        status = snapshot["status"]
        message = f"Stato **{status}** · {STATUS_HELP.get(status, '')} · {snapshot['detail']}"
        if status in ("RUNNING", "WAITING", "COMPLETED"):
            st.success(message)
        elif status in ("STALE", "PAUSED_GAP", "SUSPENDED"):
            st.warning(message)
        else:
            st.error(message)
        metrics = st.columns(6)
        metrics[0].metric("Ultimo evento", str(snapshot["last_event"] or "n/d")[:16])
        metrics[1].metric("Orologio feed", str(snapshot["feed_clock"] or "replay")[:16])
        metrics[2].metric("Posizione (BTC)", f"{snapshot['quantity']:.6f}")
        metrics[3].metric("Equity", fmt_price(snapshot["equity"]),
                          fmt_pct(100 * snapshot["pnl"] / float(session.meta["config"]["initial_cash"])))
        metrics[4].metric("Costi", fmt_price(snapshot["costs"]))
        metrics[5].metric("Trade", f"{snapshot['trades']}")
        st.caption(
            f"Sessione `{snapshot['session_id']}` · modalità {snapshot['mode']} · strategia "
            f"`{snapshot['strategy']['name']}` {snapshot['strategy']['parameters']} · config sha `{snapshot['config_sha256'][:12]}` · "
            f"motore `{snapshot['engine_sha256'][:12]}` · eventi {snapshot['events']}"
        )
        if snapshot["pending_order"]:
            st.info(f"Ordine pendente: {snapshot['pending_order']}")
        ledger = session.ledger()
        if ledger:
            curve = analytics.equity_curve(ledger, float(session.meta["config"]["initial_cash"]))
            show_plot(make_studio_equity_figure({snapshot["session_id"]: curve}, title="Equity della sessione"),
                      key="paper_equity")
            trades = analytics.trades(ledger)
            st.markdown("##### Operazioni")
            st.dataframe(trades.round(6) if not trades.empty else trades, width="stretch", hide_index=True)
            orders = [r for r in ledger if r["kind"] in ("order", "order_cancelled", "gap_acknowledged")]
            with st.expander(f"Ordini ed eventi operativi ({len(orders)})", expanded=False):
                st.dataframe(pd.DataFrame(orders), width="stretch", hide_index=True)
        with st.expander("Storico degli stati", expanded=False):
            st.dataframe(pd.DataFrame(session.status_log()), width="stretch", hide_index=True)
    finally:
        session.close()
