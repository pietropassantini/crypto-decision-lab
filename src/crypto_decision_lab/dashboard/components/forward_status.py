"""S02-CP08-05 dashboard tab: Forward Data Status (read-only).

Every value comes from the forward archive written by
``crypto_decision_lab.forward_collection``.  The view never downloads, never
writes and never computes strategy results: it shows data availability,
provenance, gaps, quarantine and readiness only.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from ..charts import make_forward_coverage_figure, make_forward_readiness_figure
from ..config import DashboardConfig
from ..forward_adapter import ForwardArchiveError, load_forward_dataset
from .common import show_plot

STATUS_ICON = {
    "NO_DATA": "⚪",
    "NOT_READY": "🟠",
    "DATA_READY_EVALUATION_PENDING": "🟢",
}
QUARANTINE_ICON = {"VALID": "🟢", "REQUIRES_REVIEW": "🟠", "QUARANTINED": "🔴", "INVALID": "⛔"}


def _fmt(value, digits: int = 2) -> str:
    if value is None:
        return "n/d"
    if isinstance(value, str):
        return value
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


@st.cache_data(ttl=120, show_spinner=False)
def _cached_dataset(repo_root: str, dataset_dir: str | None, config_path: str | None) -> object:
    return load_forward_dataset(
        repo_root, dataset_dir=dataset_dir or None, config_path=config_path or None
    )


def _short(hash_value: str | None) -> str:
    if not hash_value:
        return "n/d"
    return f"{hash_value[:16]}…"


def render_forward_status(config: DashboardConfig) -> None:
    st.subheader("Forward Data Status · S02-CP08-05")
    # data_root is ``<repo>/data``: the repository root is its parent.
    repo_root = Path(config.data_root).resolve().parent
    directory = config.forward_dataset_dir
    if directory is None or not Path(directory).is_dir():
        st.info(
            "Archivio forward assente. La raccolta è un processo separato: "
            "`python -m crypto_decision_lab.forward_collection sync`. "
            "Questa scheda è read-only, non scarica e non scrive nulla."
        )
        return
    try:
        dataset = _cached_dataset(
            str(repo_root),
            str(directory),
            str(config.forward_config_path) if config.forward_config_path else None,
        )
    except (ForwardArchiveError, ValueError) as exc:
        st.error(f"Archivio forward non leggibile: {exc}")
        return

    st.markdown(
        f"**{dataset.dataset_id}** · ruolo `independent_forward_collection` · "
        f"archivio `{dataset.directory}`"
    )
    st.warning(
        "NESSUN DATO INDIPENDENTE VALUTATO · Nessuna metrica di strategia · "
        f"Gate strategico **{dataset.strategic_gate}** · Validazione **NOT_EVALUATED**"
    )

    status = dataset.status
    readiness = dataset.readiness
    columns = st.columns(6)
    columns[0].metric("Stato acquisizione", f"{STATUS_ICON.get(dataset.readiness_status, '⚪')} {dataset.readiness_status}")
    columns[1].metric("Ore acquisite", _fmt(status.get("hours_available"), 0))
    columns[2].metric("Ultima candela completa", (dataset.last_acquired_bar_utc or "n/d").replace("+00:00", " UTC")[:16])
    columns[3].metric("Chunk validi", _fmt(status.get("valid_chunks"), 0))
    columns[4].metric("In quarantena", _fmt(status.get("quarantined_chunks"), 0))
    columns[5].metric("Gap (ore)", _fmt(status.get("gap_hours"), 0))

    st.caption(
        f"Boundary congelato: **{dataset.boundary_utc}** · ultima ora chiusa dalla sorgente: "
        f"**{status.get('last_closed_bar_utc')}** · copertura "
        f"**{_fmt(status.get('coverage_pct'))}%** delle ore attese · "
        f"finestre eleggibili: **{_fmt(readiness.get('eligible_windows', {}).get('count'), 0)}** · "
        f"fold possibili: **{_fmt(readiness.get('folds', {}).get('possible'), 0)}**"
    )

    show_plot(make_forward_coverage_figure(status, readiness), key="forward_coverage")
    show_plot(make_forward_readiness_figure(readiness), key="forward_requirements")

    tabs = st.tabs(["Readiness", "Chunk & provenienza", "Gap & quarantena", "Registry"])

    with tabs[0]:
        st.markdown("#### Requisiti minimi ancora mancanti")
        missing = dataset.missing_requirements
        if missing:
            st.dataframe(
                pd.DataFrame(missing)[["label", "required", "observed"]],
                width="stretch",
                hide_index=True,
            )
        else:
            st.success("Nessun requisito di campione mancante.")
        st.caption(
            "I requisiti sono letti dal protocollo congelato. I trade eseguiti non sono "
            "misurabili prima della valutazione finale: la raccolta non li stima."
        )
        isolation = dataset.isolation
        st.markdown("#### Isolamento della validazione")
        st.write(
            f"- Protocollo `{dataset.protocol.get('protocol_id', 'n/d')}` "
            f"v{dataset.protocol.get('protocol_version', 'n/d')} · hash valido: "
            f"**{isolation.get('protocol_hash_ok')}** · archivio CP08-04 allineato: "
            f"**{isolation.get('protocol_archive_match')}**"
        )
        st.write(f"- Uso della ricerca sul dataset forward: **{isolation.get('research_use', 'FORBIDDEN')}**")
        st.write(
            f"- Valutazione finale consumata: **{isolation.get('final_evaluation_consumed')}** · "
            f"dataset registrato: **{isolation.get('dataset_registered')}** · "
            f"selezione soglie su forward: **{isolation.get('threshold_selection_on_forward')}**"
        )
        st.write(
            f"- Protocollo: `{_short(dataset.protocol.get('config_sha256'))}` · canonico: "
            f"`{_short(dataset.protocol.get('canonical_sha256'))}`"
        )

    with tabs[1]:
        st.markdown("#### Chunk promossi (immutabili, SHA-256 verificato)")
        chunks = dataset.chunks
        if chunks:
            table = pd.DataFrame(
                [
                    {
                        "Chunk": row.get("chunk_id"),
                        "Tipo": row.get("kind"),
                        "Inizio (UTC)": row.get("start_utc"),
                        "Fine (UTC)": row.get("end_utc"),
                        "Righe": row.get("rows"),
                        "SHA-256": _short(row.get("sha256")),
                        "Sorgente": (row.get("source") or {}).get("source_id"),
                        "Ingestione": row.get("ingested_utc"),
                    }
                    for row in chunks
                ]
            )
            st.dataframe(table, width="stretch", hide_index=True)
        else:
            st.info("Nessun chunk promosso: l'archivio è vuoto.")
        st.caption(
            f"Dataset SHA-256: `{dataset.dataset_sha256 or 'n/d'}` · "
            f"manifest v{status.get('manifest', {}).get('version')} · "
            "l'ingestion timestamp è metadato e non entra nell'hash di contenuto."
        )

    with tabs[2]:
        st.markdown("#### Quarantena")
        quarantine = dataset.quarantine
        if quarantine:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Chunk": row.get("chunk_id"),
                            "Stato": f"{QUARANTINE_ICON.get(row.get('status'), '⚪')} {row.get('status')}",
                            "Inizio (UTC)": row.get("start_utc"),
                            "Fine (UTC)": row.get("end_utc"),
                            "Anomalie": ", ".join(
                                item.get("code")
                                for item in (row.get("quality") or {}).get("issues", [])
                            ),
                        }
                        for row in quarantine
                    ]
                ),
                width="stretch",
                hide_index=True,
            )
        else:
            st.info("Nessun oggetto in quarantena.")
        st.markdown("#### Gap registrati")
        gaps = status.get("gaps", [])
        if gaps:
            st.dataframe(pd.DataFrame(gaps), width="stretch", hide_index=True)
        else:
            st.caption("Nessun gap nella copertura acquisita.")
        if status.get("pending_hours"):
            st.caption(
                f"{status['pending_hours']} ore non ancora acquisite (periodo non dichiarato completo)."
            )

    with tabs[3]:
        st.markdown("#### Eventi append-only")
        events = dataset.events
        if events:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Evento": row.get("event"),
                            "Quando (UTC)": row.get("at_utc"),
                            "Chunk": row.get("chunk_id") or row.get("cell") or "",
                            "Dettaglio": row.get("reason")
                            or ", ".join(row.get("issues") or [])
                            or row.get("error")
                            or "",
                        }
                        for row in events[-200:]
                    ]
                ),
                width="stretch",
                hide_index=True,
            )
        else:
            st.info("Registry vuoto: nessuna esecuzione registrata.")
        st.caption(
            "Lo snapshot di ricerca, i dataset CP07/CP08 e il registro di validazione "
            "non vengono modificati da questa scheda né dalla raccolta forward."
        )
