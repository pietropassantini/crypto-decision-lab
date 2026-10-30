"""S02-CP08-04 dashboard view: independent validation protocol (read-only).

Every number shown here comes from the additive archive
``data/experiments/S02-CP08-08-independent-validation-protocol-v1``.  The view
never recomputes a metric, never writes and never consumes independent data: it
is a reader of the frozen protocol and of the current (non-)verdict.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from ..charts import make_validation_timeline_figure
from ..config import DashboardConfig
from ..validation_adapter import (
    ValidationArchiveError,
    load_validation_archive,
)
from .common import download_button, fmt_pct, show_plot

STATUS_ICON = {"PASS": "🟢", "WARN": "🟠", "FAIL": "🔴", "FIXED": "🔧"}


@st.cache_data(ttl=300, show_spinner=False)
def _cached_archive(directory: str):
    return load_validation_archive(directory)


def _optional(value, digits: int = 4) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return "n/d"
    if isinstance(value, str):
        return value
    return f"{float(value):,.{digits}f}"


def _criteria_table(rows: list[dict]) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame(rows)[
        ["id", "metric", "operator", "value", "description"]
    ].copy()
    frame.columns = ["Criterio", "Metrica", "Operatore", "Soglia", "Descrizione"]
    return frame


def render_validation_protocol(config: DashboardConfig) -> None:
    st.subheader("Validation Protocol · S02-CP08-04")
    directory = config.validation_protocol_dir
    if directory is None or not (Path(directory) / "manifest.json").is_file():
        st.info(
            "Archivio S02-CP08-04 non trovato. Genera il protocollo con "
            "`python -m crypto_decision_lab.independent_validation` "
            "(read-only: nessun training, nessuna inferenza, nessun download)."
        )
        return
    try:
        archive = _cached_archive(str(directory))
    except (ValidationArchiveError, FileNotFoundError, ValueError) as exc:
        st.error(f"Archivio S02-CP08-04 non leggibile: {exc}")
        return

    st.markdown(
        """
        <div style="background:#2a1a12;border:1px solid #f0b90b;border-radius:8px;
                    padding:10px 14px;margin-bottom:10px;color:#f0b90b;font-size:0.9rem;">
          <b>NESSUN DATO INDIPENDENTE · VALIDAZIONE NON VALUTATA · NO-GO</b><br>
          <span style="color:#d7dee8;">Il protocollo è congelato e verificato, ma nessun dato
          successivo al 2026-10-23T16:00Z è disponibile. Nessun GO è possibile e nessun
          backtest viene eseguito da questa vista.</span>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if archive.issues:
        for issue in archive.issues:
            st.warning(issue)

    summary = archive.summary
    independent = summary.get("independent_validation", {})
    benchmark = archive.benchmark
    expected = (benchmark.get("expected") or summary.get("frozen_benchmark", {}).get("expected", {}))
    passed = len(archive.checks) - len(archive.checks_failed)

    columns = st.columns(6)
    columns[0].metric("Protocollo", f"{archive.protocol_id} v{archive.protocol_version}")
    columns[1].metric("Gate tecnico", archive.technical_gate, help=f"{passed}/{len(archive.checks)} PASS")
    columns[2].metric("Gate strategico", archive.strategic_gate)
    columns[3].metric("Verifiche", f"{passed}/{len(archive.checks)}")
    columns[4].metric(
        "Benchmark 5% congelato",
        fmt_pct(expected.get("total_return_pct")),
        help=f"{expected.get('trades', 'n/d')} trade · PF {_optional(expected.get('profit_factor_usdt'))}",
    )
    columns[5].metric("Verdetto", archive.verdict)

    st.caption(
        "Protocollo pre-registrato e immutabile: hash SHA-256 del file YAML nei sidecar e "
        "nell'archivio. Le soglie GO/NO-GO sono state fissate prima dell'esistenza di dati "
        "indipendenti e non possono essere rilassate dopo averli visti. "
        "I numeri del benchmark sono un anchor di riconciliazione, non un target."
    )

    show_plot(
        make_validation_timeline_figure(
            archive.data_registry,
            min_independent_days=int(
                archive.summary.get("criteria", {})
                .get("sample_requirements", {})
                .get("min_independent_days", 180)
            ),
        ),
        key="cp0804_timeline",
    )
    st.caption(
        "La coda 2026-10-23T00:00Z → 16:00Z è presente nello snapshot ma **non** è evidenza "
        "indipendente: era già osservabile durante la ricerca. La finestra viola è un'attesa, "
        "non un risultato."
    )

    tabs = st.tabs(["Protocollo", "Criteri GO / NO-GO", "Dati & anti-riuso", "Controlli"])

    with tabs[0]:
        st.markdown("#### Policy congelata e scenario primario")
        left, right = st.columns(2)
        with left:
            policy = {
                "Policy": expected.get("policy_id", "momentum_threshold_5pct"),
                "Segnale": "momentum_168h",
                "Soglia": f"{100 * float(expected.get('threshold', 0.05)):.1f}%",
                "Orizzonte": "24h",
                "Capitale iniziale": f"{float(expected.get('initial_capital_usdt', 10000)):,.0f} USDT",
                "Sizing": "100% equity, nessuna leva",
                "Posizioni": "una alla volta, nessuna sovrapposizione",
                "Costi di riferimento": "0,10% fee + 0,05% slippage per lato",
            }
            st.dataframe(
                pd.DataFrame({"Campo": list(policy), "Valore": list(policy.values())}),
                width="stretch",
                hide_index=True,
            )
        with right:
            primary = archive.summary.get("primary_scenario", {})
            scenario = {
                "Scenario": primary.get("id", "n/d"),
                "Latenza": f"{int(primary.get('latency_hours', 1))}h",
                "Fee": f"{100 * float(primary.get('fee_rate', 0)):.3f}%",
                "Slippage": f"{100 * float(primary.get('slippage', 0)):.3f}%",
                "Costo totale per lato": f"{100 * float(primary.get('total_cost_per_side', 0)):.3f}%",
                "Carry SHORT": f"{float(primary.get('short_carry', {}).get('carry_pct_per_trade', 0)):.3f}% per trade",
                "Stato carry": primary.get("short_carry", {}).get("status", "n/d"),
                "Mark-to-market": "orario, valore di liquidazione",
            }
            st.dataframe(
                pd.DataFrame({"Campo": list(scenario), "Valore": list(scenario.values())}),
                width="stretch",
                hide_index=True,
            )
            st.caption(
                "Il carry SHORT è un'ipotesi parametrica: nel repository non esiste una serie "
                "storica di funding/borrow."
            )

        protocol_config = archive.protocol.get("protocol", {})
        st.markdown("#### Griglia di soglie e walk-forward")
        grid = protocol_config.get("threshold_preregistration", {})
        walk = protocol_config.get("walk_forward", {})
        left, right = st.columns(2)
        with left:
            st.write(
                f"Griglia: **{', '.join(f'{100 * float(v):g}%' for v in grid.get('grid', []))}**"
            )
            st.write(f"Regola: `{grid.get('selection_rule', 'n/d')}` · split `{grid.get('selection_split', 'n/d')}`")
            st.write(f"Pareggio: `{grid.get('tie_break', 'n/d')}` · griglia chiusa: **{grid.get('grid_is_closed')}**")
        with right:
            st.write(
                f"Fold: **{walk.get('type', 'n/d')}** · {walk.get('test_windows_per_fold', 'n/d')} finestre di test · "
                f"min train {walk.get('min_train_windows', 'n/d')} · purge {walk.get('purge_hours', 'n/d')}h"
            )
            st.write(f"Fold minimi per conclusività: **{walk.get('folds_min_for_conclusive', 'n/d')}**")
            st.write(f"Look intermedi: descrittivi, mai gating = **{walk.get('interim_looks', {}).get('gating')}**")

        st.markdown("#### Riconciliazione del benchmark congelato con CP08-03")
        comparisons = benchmark.get("comparisons", [])
        if comparisons:
            table = pd.DataFrame(comparisons)[["metric", "observed", "declared", "delta", "match"]]
            table.columns = ["Metrica", "Osservato", "Dichiarato", "Delta", "Match"]
            st.dataframe(table.round(9), width="stretch", hide_index=True)
        st.caption(
            "Il confronto è una verifica di hash e di numeri, non una nuova esecuzione: "
            "CP08-04 non esegue backtest sul periodo di ricerca."
        )

    with tabs[1]:
        st.markdown("#### Criteri pre-registrati · scenario primario")
        criteria = archive.criteria
        requirements = criteria.get("sample_requirements", {})
        st.write(
            f"Campione minimo: **{requirements.get('min_trades', 'n/d')} trade**, "
            f"**{requirements.get('min_folds', 'n/d')} fold**, "
            f"**{requirements.get('min_independent_days', 'n/d')} giorni indipendenti**."
        )
        st.markdown("**Criteri GO** (tutti necessari)")
        st.dataframe(_criteria_table(criteria.get("go_criteria", [])), width="stretch", hide_index=True)
        st.markdown("**Criteri NO-GO** (un solo trigger è sufficiente)")
        st.dataframe(_criteria_table(criteria.get("no_go_criteria", [])), width="stretch", hide_index=True)
        st.markdown("**Regola di decisione**")
        for line in criteria.get("decision_rule", []):
            st.write(f"- {line}")
        st.warning(
            "Nessun criterio è stato valutato: manca il campione indipendente. Le soglie sono "
            "state fissate prima dei dati e non possono essere rilassate dopo averli visti; "
            "un rilassamento richiede una nuova versione del protocollo e un nuovo periodo."
        )
        download_button(
            pd.DataFrame(criteria.get("go_criteria", []) + criteria.get("no_go_criteria", [])),
            filename="cp08-04-criteria.csv",
            label="Scarica criteri",
            key="cp0804_download_criteria",
        )

    with tabs[2]:
        st.markdown("#### Registro dei dati")
        datasets = pd.DataFrame(archive.data_registry.get("datasets", []))
        if not datasets.empty:
            view = datasets.copy()
            if "coverage" in view.columns:
                view["coverage"] = view["coverage"].apply(
                    lambda value: f"{value.get('start_utc')} → {value.get('end_exclusive_utc') or 'n/d'}"
                    if isinstance(value, dict)
                    else str(value)
                )
            st.dataframe(view, width="stretch", hide_index=True)
        separation = archive.data_registry.get("separation", {})
        st.markdown("#### Separazione temporale")
        if separation:
            st.dataframe(
                pd.DataFrame(
                    [
                        {"Controllo": key, "Valore": str(value)}
                        for key, value in separation.items()
                        if key != "observed_unused_tail"
                    ]
                ),
                width="stretch",
                hide_index=True,
            )

        st.markdown("#### Stato anti-riuso")
        registry = summary.get("registry_state", {})
        st.dataframe(
            pd.DataFrame(
                [
                    {"Controllo": "Dataset registrati", "Valore": str(registry.get("datasets", 0))},
                    {"Controllo": "Valutazioni registrate", "Valore": str(registry.get("evaluations", 0))},
                    {"Controllo": "Valutazioni finali", "Valore": str(registry.get("final_evaluations", 0))},
                    {"Controllo": "Periodo finale consumato", "Valore": str(registry.get("final_consumed", False))},
                ]
            ),
            width="stretch",
            hide_index=True,
        )
        controls = (archive.protocol.get("protocol", {}) or {}).get("anti_reuse_controls", {})
        if controls:
            st.caption(
                "Controlli attivi: "
                + " · ".join(f"`{key}`" for key, value in controls.items() if value is True)
            )
        st.info(
            "Il registro append-only impedisce una seconda valutazione finale per la stessa "
            "versione di protocollo, lo scambio del dataset e l'uso del periodo di ricerca o "
            "della coda osservata come evidenza indipendente."
        )

        st.markdown("#### Momentum Confirmation Engine")
        engine = archive.engine.get("config", archive.engine)
        st.write(
            f"Engine `{engine.get('engine_id', 'n/d')}` v{engine.get('engine_version', 'n/d')} · "
            f"stato **{engine.get('status', 'n/d')}** · regole attive "
            f"**{len((engine.get('rules') or {}).get('active', []))}** · runnable "
            f"**{engine.get('runnable', 'n/d')}**"
        )
        st.caption(
            "Configurazione versionata pronta per il futuro engine, ma deliberatamente vuota: "
            "nessuna regola derivata dai dati OOS (inclusi i bucket di volatilità di CP08-02 o "
            "la superficie di CP08-03)."
        )

    with tabs[3]:
        st.markdown("#### Controlli automatici CP08-04")
        checks = pd.DataFrame(
            [
                {
                    "id": check.get("id"),
                    "stato": f"{STATUS_ICON.get(str(check.get('status')), '')} {check.get('status')}",
                    "controllo": check.get("check"),
                    "dettaglio": check.get("detail"),
                }
                for check in archive.checks
            ]
        )
        st.dataframe(checks, width="stretch", hide_index=True)
        manifest = archive.manifest
        st.markdown(
            f"- Hash canonico protocollo: `{archive.canonical_sha256}`\n"
            f"- Hash file YAML: `{manifest.get('config_sha256')}`\n"
            f"- Hash specifica engine: `{manifest.get('engine_sha256')}`\n"
            f"- Artefatti congelati invariati: **{manifest.get('frozen_artifacts_unchanged')}**\n"
            f"- Archivio originale modificato: **{manifest.get('original_artifacts_modified')}**\n"
            f"- Verdetto: **{archive.verdict}** · gate strategico: **{archive.strategic_gate}**"
        )
        st.caption(
            "Un PASS tecnico certifica che il protocollo è completo, immutabile e riproducibile. "
            "Non è una validazione e non è un GO: servono dati realmente nuovi e una sola "
            "valutazione finale."
        )
