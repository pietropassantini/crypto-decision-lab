"""S02-CP08-03 dashboard view: parameter & execution robustness (read-only).

Every number shown here comes from the additive archive
``data/experiments/S02-CP08-parameter-robustness-v1``.  The view never recomputes
a metric, never writes and never re-optimises a threshold.  It exists to show
*how fragile* the frozen policy is, not to promote a configuration.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

from ..charts import (
    make_cost_sensitivity_figure,
    make_latency_robustness_figure,
    make_scenario_interval_figure,
    make_threshold_surface_figure,
)
from ..config import DashboardConfig
from ..parameter_robustness_adapter import (
    RobustnessArchiveError,
    load_robustness_archive,
)
from .common import download_button, fmt_number, fmt_pct, show_plot

STATUS_COLORS = {"PASS": "🟢", "WARN": "🟠", "FAIL": "🔴", "FIXED": "🔧"}


@st.cache_data(ttl=300, show_spinner=False)
def _cached_archive(directory: str):
    return load_robustness_archive(directory)


def _metric(scenarios: pd.DataFrame, name: str) -> pd.Series | None:
    subset = scenarios[scenarios["scenario"] == name]
    return subset.iloc[0] if len(subset) else None


def _table(frame: pd.DataFrame, columns: dict[str, str]) -> pd.DataFrame:
    present = [column for column in columns if column in frame.columns]
    table = frame[present].copy().rename(columns={column: columns[column] for column in present})
    for column in table.columns:
        if pd.api.types.is_numeric_dtype(table[column]):
            table[column] = table[column].round(4)
    return table


def _scenario_table(frame: pd.DataFrame) -> pd.DataFrame:
    return _table(
        frame,
        {
            "label": "Scenario",
            "fee_rate": "Fee",
            "slippage": "Slippage",
            "delay_hours": "Latenza h",
            "carry_short_pct": "Carry SHORT %",
            "trades": "Trade",
            "total_return_pct": "Return %",
            "profit_factor_usdt": "PF monetario",
            "max_drawdown_exit_pct": "Max DD exit %",
            "max_drawdown_mtm_pct": "Max DD MTM %",
            "ci_return_low_pct": "CI 95% low %",
            "ci_return_high_pct": "CI 95% high %",
            "ci_return_p_positive": "P(>0)",
            "hypothesis": "Ipotesi",
        },
    )


def render_parameter_robustness(config: DashboardConfig) -> None:
    st.subheader("Parameter Robustness · S02-CP08-03")
    directory = config.parameter_robustness_dir
    if directory is None or not (Path(directory) / "manifest.json").is_file():
        st.info(
            "Archivio S02-CP08-03 non trovato. Genera l'analisi con "
            "`python -m crypto_decision_lab.parameter_robustness` "
            "(read-only: nessun training, nessuna inferenza, nessun download)."
        )
        return
    try:
        archive = _cached_archive(str(directory))
    except (RobustnessArchiveError, FileNotFoundError, ValueError) as exc:
        st.error(f"Archivio S02-CP08-03 non leggibile: {exc}")
        return

    st.caption(
        "Robustezza di parametri ed esecuzione del Momentum Threshold congelato "
        "(soglia 5%, fee 0,10%, slippage 0,05%). La policy CP07 non è modificata: "
        "**nessuna soglia viene promossa** e il gate strategico resta **INCONCLUSIVE**."
    )
    if archive.issues:
        for issue in archive.issues:
            st.warning(issue)

    scenarios = archive.scenarios
    base = _metric(scenarios, "cost_0015_base")
    primary = getattr(archive, "primary", None)
    if primary is None:
        primary = _metric(scenarios, "conservative_1h")
    failed = archive.checks_failed

    columns = st.columns(6)
    if base is not None:
        columns[0].metric(
            "Base CP07 · return",
            fmt_pct(base["total_return_pct"]),
            help=f"trade {int(base['trades'])} · soglia 5% · costi CP07",
        )
        columns[1].metric("Base · PF monetario", fmt_number(base["profit_factor_usdt"], 4))
        columns[2].metric("Base · DD MTM", fmt_pct(base["max_drawdown_mtm_pct"]))
    if primary is not None:
        columns[3].metric(
            "1h conservativo · return",
            fmt_pct(primary["total_return_pct"]),
            help=(
                f"PF {fmt_number(primary['profit_factor_usdt'], 4)} · "
                f"DD MTM {fmt_pct(primary['max_drawdown_mtm_pct'])} · "
                f"P(>0) {fmt_number(primary['ci_return_p_positive'] * 100, 1)}%"
            ),
        )
        columns[4].metric("1h · PF monetario", fmt_number(primary["profit_factor_usdt"], 4))
        columns[5].metric("1h · DD MTM", fmt_pct(primary["max_drawdown_mtm_pct"]))
    st.caption(
        f"Scenario conservativo principale: **1h di latenza**. "
        f"Controlli: {len(archive.checks) - len(failed)}/{len(archive.checks)} PASS"
        + (f" · falliti: {', '.join(failed)}" if failed else " · nessun FAIL")
    )

    st.warning(
        "**La superficie OOS è esplorativa e non promuovibile.** I dati out-of-sample erano già stati "
        "osservati in CP07/CP08-01: nessuna soglia migliore in OOS può essere selezionata o dichiarata "
        "strategia. L'unica selezione ammessa è quella sul training di ciascun fold."
    )
    st.info(
        "**Funding e borrow per gli SHORT sono ipotesi parametriche**, non dati storici: il repository "
        "contiene solo prezzi spot. Gli scenari di carry servono a misurare l'ordine di grandezza del "
        "costo, non a stimare il funding realmente pagato."
    )

    sections = st.tabs(
        ["Costi", "Latenza & carry", "Superficie soglia", "Fold & concentrazione", "Controlli"]
    )

    with sections[0]:
        st.markdown("#### Esperimento A · costi con decisioni congelate")
        frozen = archive.family("cost_frozen")
        st.caption(
            "Stesso trade ledger (106 operazioni, stesse date e direzioni): cambia solo il modello di "
            "costo. L'effetto deve essere monotono e lo è: aumentare i costi non aumenta mai il rendimento."
        )
        show_plot(
            make_cost_sensitivity_figure(scenarios),
            key="cp0803_cost_figure",
        )
        st.dataframe(
            _scenario_table(frozen),
            width="stretch",
            hide_index=True,
        )
        download_button(
            frozen,
            filename="cp08-03-cost-frozen.csv",
            label="Scarica costi (congelati)",
            key="cp0803_download_cost_frozen",
        )

        st.markdown("#### Esperimento B · refit della soglia sul training")
        refit = archive.family("policy_refit")
        selection = archive.selection
        refit_selection = (
            selection[selection["family"] == "policy_refit"] if not selection.empty else selection
        )
        st.caption(
            "Per ogni scenario di costo la soglia viene ri-selezionata **solo sul training** del fold. "
            "Questa curva misura costo **più** ri-selezione: non è monotona e non va letta come "
            "'costa meno, rende meno'. È il confondimento già segnalato da CP08-01 (F10)."
        )
        st.dataframe(_scenario_table(refit), width="stretch", hide_index=True)
        if not refit_selection.empty:
            pivot = refit_selection.pivot_table(
                index="scenario",
                columns="fold",
                values="selected_threshold",
                aggfunc="first",
            )
            st.markdown("**Soglie scelte sul training (5 fold)**")
            st.dataframe((100.0 * pivot).round(1), width="stretch")
            st.caption(
                "Valori in %. Le soglie cambiano con il livello di costo: la regola di selezione è "
                "instabile rispetto al parametro che non dovrebbe cambiare la decisione strategica."
            )

    with sections[1]:
        st.markdown("#### Latenza di esecuzione (0h / 1h / 2h)")
        latency = archive.family("latency")
        show_plot(make_latency_robustness_figure(scenarios), key="cp0803_latency_figure")
        st.dataframe(_scenario_table(latency), width="stretch", hide_index=True)
        st.caption(
            "Con 1h di ritardo il rendimento scende e il drawdown mark-to-market **peggiora**: "
            "la latenza non è solo un costo di rendimento, è un aumento del rischio di percorso."
        )

        st.markdown("#### Carry SHORT · ipotesi dichiarate")
        carry = archive.family("short_carry")
        st.dataframe(_scenario_table(carry), width="stretch", hide_index=True)
        st.caption(
            "Il carry è applicato al solo nozionale SHORT alla chiusura del hold di 24h. Il mark-to-market "
            "non include la maturazione intra-hold del carry: il DD MTM di questi scenari è quindi un "
            "limite inferiore. I LONG non sono toccati."
        )

        st.markdown("#### Envelope conservativo")
        combined = archive.family("combined")
        st.dataframe(_scenario_table(combined), width="stretch", hide_index=True)
        show_plot(make_scenario_interval_figure(scenarios), key="cp0803_interval_figure")
        st.caption(
            "Intervalli bootstrap (moving block, blocco 10, 2.000 draw, seed 42) calcolati sulle 300 "
            "finestre OOS, inclusi i NO_TRADE. Lo scenario conservativo 1h+0,20%+carry resta positivo "
            "nel punto ma l'intervallo include lo zero."
        )

    with sections[2]:
        st.markdown("#### Superficie della soglia · training vs OOS esplorativo")
        show_plot(make_threshold_surface_figure(archive.surface), key="cp0803_surface_figure")
        oos = archive.surface_scope("test_all")
        if not oos.empty:
            view = oos.assign(threshold_pct=100.0 * oos["threshold"]).pipe(
                _table,
                {
                    "threshold_pct": "Soglia %",
                    "is_frozen_threshold": "Soglia congelata",
                    "trades": "Trade",
                    "total_return_pct": "Return OOS %",
                    "profit_factor_usdt": "PF monetario",
                    "max_drawdown_mtm_pct": "Max DD MTM %",
                    "win_rate_trade_pct": "Win rate %",
                    "ci_low_pct": "CI 95% low %",
                    "ci_high_pct": "CI 95% high %",
                },
            )
            st.dataframe(view, width="stretch", hide_index=True)
            best = oos.sort_values("total_return_pct", ascending=False).iloc[0]
            frozen_row = oos[oos["is_frozen_threshold"]].iloc[0]
            st.markdown(
                f"- Migliore soglia OOS **esplorativa**: {100.0 * best['threshold']:.1f}% "
                f"({fmt_pct(best['total_return_pct'])}, PF {fmt_number(best['profit_factor_usdt'], 4)}). "
                "**Non promossa.**\n"
                f"- Soglia congelata 5%: {fmt_pct(frozen_row['total_return_pct'])} · "
                f"PF {fmt_number(frozen_row['profit_factor_usdt'], 4)} · "
                f"DD MTM {fmt_pct(frozen_row['max_drawdown_mtm_pct'])}.\n"
                "- La curva OOS non è monotona né stabile: 5%, 8% e 10% non sono punti equivalenti e "
                "nessuno è un ottimo robusto."
            )
            st.caption(
                "Le linee grigie sono i rendimenti di training dei 5 fold per ogni soglia: è l'unica "
                "informazione che una regola di selezione può usare. La linea viola e la banda sono OOS "
                "e servono solo a descrivere la superficie."
            )

    with sections[3]:
        st.markdown("#### Stabilità per fold")
        folds = archive.folds
        if folds.empty:
            st.info("fold_stability.parquet non disponibile.")
        else:
            options = list(dict.fromkeys(folds["scenario"]))
            default = [name for name in ("cost_0015_base", "conservative_1h") if name in options]
            selected = st.multiselect(
                "Scenari da confrontare",
                options=options,
                default=default or options[:2],
                key="cp0803_fold_scenarios",
            )
            subset = folds[folds["scenario"].isin(selected)]
            st.dataframe(
                _table(
                    subset,
                    {
                        "scenario": "Scenario",
                        "fold": "Fold",
                        "trades": "Trade",
                        "net_pnl_usdt": "P&L fold (reset) USDT",
                        "chained_pnl_usdt": "P&L concatenato USDT",
                        "share_of_net_pnl_pct": "Share P&L %",
                        "compounded_return_pct": "Return fold %",
                        "profit_factor_usdt": "PF monetario",
                        "max_drawdown_exit_pct": "Max DD exit %",
                        "max_drawdown_mtm_pct": "Max DD MTM %",
                        "win_rate_trade_pct": "Win rate %",
                    },
                ),
                width="stretch",
                hide_index=True,
            )
            st.caption(
                "`P&L concatenato` usa l'equity composta dello scenario ed è additivo tra fold; "
                "`P&L fold (reset)` e `Return fold` ripartono da 10.000 USDT per isolare il blocco. "
                "Il DD mark-to-market per fold è calcolato per la base e per i tre scenari di latenza; "
                "per gli altri scenari è riportato il DD exit-marked."
            )
            download_button(
                subset,
                filename="cp08-03-fold-stability.csv",
                label="Scarica stabilità per fold",
                key="cp0803_download_folds",
            )

        st.markdown("#### Concentrazione dei profitti")
        concentration = archive.concentration
        rows = []
        for label, key in (("Base CP07", "frozen"), ("Conservativo 1h", "primary_1h")):
            block = concentration.get(key, {})
            top = block.get("top_k", {})
            for k in ("1", "3", "5", "10"):
                if k not in top:
                    continue
                rows.append(
                    {
                        "scenario": label,
                        "k": int(k),
                        "pnl_top_k_usdt": top[k]["pnl_usdt"],
                        "share_net_pnl_pct": top[k]["share_of_net_pnl_pct"],
                        "return_after_removal_pct": top[k]["return_pct_after_removal_recompounded"],
                    }
                )
        if rows:
            st.dataframe(pd.DataFrame(rows).round(4), width="stretch", hide_index=True)
        st.caption(
            "`return_after_removal_pct` è il rendimento ricomposto senza i k trade migliori. "
            "Nello scenario 1h bastano 3 trade rimossi per rendere il rendimento negativo."
        )

    with sections[4]:
        st.markdown("#### Controlli CP08-03")
        checks = pd.DataFrame(
            [
                {
                    "id": check.get("id"),
                    "stato": f"{STATUS_COLORS.get(str(check.get('status')), '')} {check.get('status')}",
                    "controllo": check.get("check"),
                    "dettaglio": check.get("detail"),
                }
                for check in archive.checks
            ]
        )
        st.dataframe(checks, width="stretch", hide_index=True)
        manifest = archive.manifest
        st.markdown(
            f"- Gate tecnico: **{manifest.get('technical_gate')}** "
            f"({len(archive.checks) - len(failed)}/{len(archive.checks)} PASS, "
            f"{len(failed)} FAIL)\n"
            f"- Gate strategico: **{manifest.get('strategic_gate')}**\n"
            f"- Promozione: **{manifest.get('promotion')}**\n"
            f"- Artefatti originali modificati: **{manifest.get('original_artifacts_modified')}**"
        )
        st.caption(
            "Un PASS tecnico certifica riproducibilità e assenza di leakage: non è un GO per il trading. "
            "La validazione indipendente richiesta da CP08-01 §11 resta aperta."
        )
