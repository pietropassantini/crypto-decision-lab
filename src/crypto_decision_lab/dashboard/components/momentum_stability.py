"""S02-CP08-02 dashboard view: Momentum stability (read-only).

Every number shown here comes from the additive archive
``data/experiments/S02-CP08-momentum-stability-v1``.  The view never recomputes
metrics, never writes and never re-optimises a threshold: it is a reader of the
frozen analysis, like the trading-simulation view is a reader of CP07.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import streamlit as st

from ..charts import (
    make_period_pnl_figure,
    make_pnl_distribution_figure,
    make_profit_concentration_figure,
    make_stability_equity_figure,
    make_underwater_figure,
)
from ..config import DashboardConfig
from ..stability_adapter import StabilityArchiveError, load_stability_archive
from .common import download_button, fmt_number, fmt_pct, show_plot

GRANULARITY_LABELS = {
    "month": "Mese",
    "quarter": "Trimestre",
    "year": "Anno",
    "fold": "Fold",
    "window": "Finestra OOS",
}
REGIME_LABELS = {
    "trend": "Trend (tercili train-only di momentum_168h)",
    "volatility": "Volatilità (tercili train-only di volatility_168h)",
    "direction": "Direzione",
    "trend_x_volatility": "Trend × volatilità",
}


@st.cache_data(ttl=300, show_spinner=False)
def _cached_archive(directory: str):
    return load_stability_archive(directory)


def _fmt_optional(value, digits: int = 2, suffix: str = "") -> str:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return "n/d"
    if isinstance(value, str):
        return value
    return f"{float(value):,.{digits}f}{suffix}"


def _period_table(frame: pd.DataFrame) -> pd.DataFrame:
    columns = {
        "period": "Periodo",
        "windows": "Finestre",
        "trades": "Trade",
        "net_pnl_usdt": "P&L USDT",
        "compounded_return_pct": "Return periodo %",
        "profit_factor_usdt": "PF monetario",
        "win_rate_trade_pct": "Win rate trade %",
        "win_rate_window_pct": "Win rate finestra %",
        "avg_net_trade_pct": "Media net/trade %",
        "best_net_pct": "Migliore %",
        "worst_net_pct": "Peggiore %",
        "share_of_net_pnl_pct": "Share P&L %",
        "cumulative_pnl_usdt": "P&L cumulato USDT",
    }
    table = frame[[column for column in columns if column in frame.columns]].copy()
    table = table.rename(columns=columns)
    for column in table.columns:
        if column != "Periodo" and pd.api.types.is_numeric_dtype(table[column]):
            table[column] = table[column].round(3)
    return table


def render_momentum_stability(config: DashboardConfig) -> None:
    st.subheader("Momentum Stability · S02-CP08-02")
    directory = config.momentum_stability_dir
    if directory is None or not (Path(directory) / "manifest.json").is_file():
        st.info(
            "Archivio S02-CP08-02 non trovato. Genera l'analisi con "
            "`python -m crypto_decision_lab.momentum_stability` (read-only, nessun training)."
        )
        return
    try:
        archive = _cached_archive(str(directory))
    except (StabilityArchiveError, FileNotFoundError, ValueError) as exc:
        st.error(f"Archivio S02-CP08-02 non leggibile: {exc}")
        return

    concentration = archive.concentration
    drawdown = archive.drawdown
    headline = archive.manifest.get("headline", {})
    reconciliation = archive.manifest.get("reconciliation", {})
    st.caption(
        "Analisi descrittiva read-only del Momentum Threshold congelato (soglia 5%, costi 0,10%/0,05%, "
        "100% equity). P&L monetario per le metriche dipendenti dal capitale. "
        "Nessun training, nessuna inferenza, nessuna ri-selezione della soglia sui dati OOS. "
        "**Non è un gate di promozione**: il verdetto resta INCONCLUSIVE."
    )
    if archive.issues:
        for issue in archive.issues:
            st.warning(issue)

    top_k = concentration.get("top_k", {})
    columns = st.columns(6)
    columns[0].metric("Trade OOS", f"{int(headline.get('trades', len(archive.trades))):,}",
                      help=f"finestre: {headline.get('windows', 'n/d')} · NO_TRADE: {headline.get('no_trade', 'n/d')}")
    columns[1].metric("Rendimento composto", fmt_pct(concentration.get("total_return_pct")))
    columns[2].metric("Profit factor monetario", _fmt_optional(concentration.get("profit_factor_usdt"), 4))
    columns[3].metric(
        "Max DD mark-to-market",
        fmt_pct(drawdown.get("mark_to_market", {}).get("max_drawdown_pct")),
        help=f"exit-marked: {fmt_pct(drawdown.get('exit_marked', {}).get('max_drawdown_pct'))}",
    )
    columns[4].metric(
        "Top 5 trade · share P&L",
        fmt_pct(top_k.get("5", {}).get("share_of_net_pnl_pct")),
        help="Somma del P&L monetario dei 5 trade migliori / P&L netto totale",
    )
    streaks = archive.streaks
    columns[5].metric(
        "Streak perdite max",
        f"{int(streaks.get('max_loss_streak', 0))}",
        help=f"streak vincenti max: {int(streaks.get('max_win_streak', 0))}",
    )

    st.markdown("#### Equity e underwater")
    left, right = st.columns([3, 2])
    with left:
        show_plot(
            make_stability_equity_figure(archive.equity, initial_capital=archive.initial_capital),
            key="stability_equity",
        )
    with right:
        show_plot(make_underwater_figure(archive.equity), key="stability_underwater")
    st.caption(
        "Exit-marked: equity costante durante l'hold, aggiornata all'exit open (ledger). "
        "Mark-to-market: valore di liquidazione a ogni chiusura oraria dentro l'hold, con costi di uscita. "
        "Il drawdown mark-to-market è più profondo e resta la metrica di rischio capitale."
    )

    st.markdown("#### Distribuzione P&L e concentrazione dei profitti")
    distribution_metric = st.radio(
        "Metrica di distribuzione",
        options=["pnl_usdt", "net_return_pct"],
        format_func=lambda value: "P&L monetario (USDT)" if value == "pnl_usdt" else "Rendimento netto per trade (%)",
        horizontal=True,
        key="stability_distribution_metric",
    )
    left, right = st.columns(2)
    with left:
        show_plot(
            make_pnl_distribution_figure(archive.trades, metric=distribution_metric),
            key="stability_pnl_distribution",
        )
    with right:
        show_plot(
            make_profit_concentration_figure(archive.concentration_curve),
            key="stability_profit_concentration",
        )

    rows = []
    for k in ("1", "3", "5", "10"):
        item = top_k.get(k)
        if not item:
            continue
        rows.append(
            {
                "Top k": int(k),
                "P&L rimosso USDT": round(item["pnl_usdt"], 2),
                "Share P&L netto %": round(item["share_of_net_pnl_pct"], 2),
                "Share profitto lordo %": round(item["share_of_gross_profit_pct"], 2),
                "Return dopo rimozione %": round(item["return_pct_after_removal"], 2),
                "Return ricomposto %": round(item["recompounded_return_pct_after_removal"], 2),
                "Mesi distinti": item["distinct_months"],
                "Fold distinti": item["distinct_folds"],
            }
        )
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    temporal = concentration.get("temporal_concentration", {})
    best_month = (temporal.get("positive_pnl_concentration") or {}).get("best_month") or {}
    best_fold = (temporal.get("positive_pnl_concentration") or {}).get("best_fold") or {}
    clustering = temporal.get("top_trade_clustering", {})
    st.caption(
        f"Miglior mese: {best_month.get('period', 'n/d')} · {fmt_pct(best_month.get('share_of_net_pnl_pct'))} del P&L netto · "
        f"miglior fold: {best_fold.get('fold', 'n/d')} · {fmt_pct(best_fold.get('share_of_net_pnl_pct'))} · "
        f"mesi necessari al 50% del P&L: {temporal.get('months_to_reach', {}).get('50pct', 'n/d')} · "
        f"top-5 trade in {clustering.get('distinct_months', 'n/d')} mesi e {clustering.get('distinct_folds', 'n/d')} fold distinti."
    )

    st.markdown("#### P&L per periodo")
    granularity = st.selectbox(
        "Granularità",
        options=["month", "quarter", "year", "fold"],
        format_func=lambda value: GRANULARITY_LABELS.get(value, value),
        index=0,
        key="stability_granularity",
    )
    show_plot(
        make_period_pnl_figure(archive.periods, granularity=granularity),
        key="stability_period_pnl",
    )
    table = _period_table(archive.period(granularity))
    st.dataframe(table, width="stretch", hide_index=True)
    download_button(
        archive.period(granularity),
        filename=f"cq08_02_period_{granularity}.csv",
        label="Scarica tabella periodo (CSV)",
        key="stability_period_csv",
    )
    st.caption(
        "Il P&L monetario per periodo è additivo: la somma su qualunque granularità coincide con il P&L totale. "
        "Il 'return periodo %' ricompone solo i trade interni al periodo e non è additivo."
    )

    st.markdown("#### Drawdown e durata")
    episode_rows = []
    for episode in drawdown.get("worst_5_episodes_mark_to_market", []):
        episode_rows.append(
            {
                "Picco": pd.Timestamp(episode["peak_time"]).strftime("%Y-%m-%d %H:%M"),
                "Trough": pd.Timestamp(episode["trough_time"]).strftime("%Y-%m-%d %H:%M"),
                "Profondità %": round(episode["depth_pct"], 2),
                "Ore sott'acqua": round(episode["underwater_hours"], 0),
                "Recuperato": "sì" if episode["recovered"] else "no (aperto)",
                "Recovery": pd.Timestamp(episode["recovery_time"]).strftime("%Y-%m-%d %H:%M")
                if episode.get("recovery_time")
                else "—",
            }
        )
    left, right = st.columns([2, 3])
    with left:
        st.dataframe(pd.DataFrame(episode_rows), width="stretch", hide_index=True)
        st.caption(
            f"Episodi MTM: {(drawdown.get('mark_to_market') or {}).get('episode_count', 'n/d')} · "
            f"tempo sott'acqua: {_fmt_optional((drawdown.get('mark_to_market') or {}).get('time_underwater_pct'))}% · "
            f"ulcer index: {_fmt_optional((drawdown.get('mark_to_market') or {}).get('ulcer_index'), 2)}"
        )
    with right:
        fold_rows = pd.DataFrame(
            [
                {
                    "Fold": item["fold"],
                    "Trade": item["trades"],
                    "P&L USDT": round(item["net_pnl_usdt"], 2),
                    "DD exit-marked %": round(item["exit_marked_max_drawdown_pct"], 2),
                    "DD mark-to-market %": round(item["mark_to_market_max_drawdown_pct"], 2),
                    "Ore underwater (MTM)": round(item["mark_to_market_longest_underwater_hours"], 0),
                }
                for item in drawdown.get("per_fold", [])
            ]
        )
        st.dataframe(fold_rows, width="stretch", hide_index=True)
        st.caption(
            "Il tempo 'sott'acqua' in mark-to-market include i micro-drawdown da costo di entrata: "
            "per la persistenza usare l'exit-marked, per il rischio capitale il mark-to-market."
        )

    st.markdown("#### Sequenze di vittorie e perdite")
    runs = streaks.get("runs_test", {})
    left, right = st.columns([2, 3])
    with left:
        streak_rows = pd.DataFrame(
            [
                {"Metrica": "Vincite / perdite", "Valore": f"{streaks.get('wins', 0)} / {streaks.get('losses', 0)}"},
                {"Metrica": "Win rate %", "Valore": str(_fmt_optional(streaks.get("win_rate_pct")))},
                {"Metrica": "Streak vincente max", "Valore": str(streaks.get("max_win_streak"))},
                {"Metrica": "Streak perdente max", "Valore": str(streaks.get("max_loss_streak"))},
                {
                    "Metrica": "Streak perdente peggiore USDT",
                    "Valore": str(round(streaks.get("worst_loss_streak_pnl_usdt", 0.0), 2)),
                },
                {
                    "Metrica": "Run osservati / attesi iid",
                    "Valore": f"{runs.get('observed_runs', 'n/d')} / {_fmt_optional(runs.get('expected_runs_if_iid'), 1)}",
                },
                {"Metrica": "Runs test z", "Valore": str(_fmt_optional(runs.get("z_score"), 3))},
            ]
        )
        st.dataframe(streak_rows, width="stretch", hide_index=True)
    with right:
        st.dataframe(
            pd.DataFrame(
                [
                    {"Lunghezza": length, "Streak vincenti": streaks.get("win_streak_distribution", {}).get(str(length), 0),
                     "Streak perdenti": streaks.get("loss_streak_distribution", {}).get(str(length), 0)}
                    for length in range(1, 10)
                ]
            ),
            width="stretch",
            hide_index=True,
        )
        st.caption(
            "Runs test descrittivo: |z|>1.96 segnala dipendenza seriale degli esiti. "
            "Non è un test di promozione della strategia."
        )

    st.markdown("#### Regime di volatilità e trend (solo feature al cutoff)")
    regime_granularity = st.selectbox(
        "Vista regime",
        options=["volatility", "trend", "direction", "trend_x_volatility"],
        format_func=lambda value: REGIME_LABELS.get(value, value),
        index=0,
        key="stability_regime",
    )
    regime_table = archive.regime(regime_granularity).copy()
    if not regime_table.empty:
        regime_table = regime_table[
            [
                "bucket",
                "windows",
                "trades",
                "net_pnl_usdt",
                "profit_factor_usdt",
                "compounded_return_pct",
                "avg_net_trade_pct",
                "win_rate_trade_pct",
                "share_of_net_pnl_pct",
                "avg_volatility_168h",
                "low_sample",
            ]
        ].rename(
            columns={
                "bucket": "Bucket",
                "windows": "Finestre",
                "trades": "Trade",
                "net_pnl_usdt": "P&L USDT",
                "profit_factor_usdt": "PF monetario",
                "compounded_return_pct": "Return ricomposto %",
                "avg_net_trade_pct": "Media net/trade %",
                "win_rate_trade_pct": "Win rate %",
                "share_of_net_pnl_pct": "Share P&L %",
                "avg_volatility_168h": "Vol media 168h",
                "low_sample": "Campione basso",
            }
        )
        st.dataframe(regime_table.round(4), width="stretch", hide_index=True)
        if bool(regime_table["Campione basso"].any()):
            st.warning("Alcuni bucket hanno meno di 10 trade: la lettura è indicativa, non conclusiva.")
        st.caption(
            "I bucket sono tercili calcolati **solo** sulle finestre di training di ciascun fold "
            "(nessun target, nessun rendimento OOS). Per il Momentum Threshold la direzione è "
            "collineare con il trend: i LONG sono tutti TREND_UP e gli SHORT tutti TREND_DOWN."
        )
    else:
        st.info("Breakdown per regime non disponibile nell'archivio.")

    st.markdown("#### Top trade e cluster temporale")
    top = archive.trades.sort_values("rank_by_pnl").head(15)
    st.dataframe(
        pd.DataFrame(
            {
                "Rank": top["rank_by_pnl"].to_numpy(),
                "Data": pd.to_datetime(top["cutoff"], utc=True).dt.strftime("%Y-%m-%d"),
                "Fold": top["fold"].to_numpy(),
                "Mese": top["month"].to_numpy(),
                "Direzione": top["direction"].to_numpy(),
                "Net %": top["net_return_pct"].round(3).to_numpy(),
                "P&L USDT": top["pnl_usdt"].round(2).to_numpy(),
                "Equity dopo USDT": top["equity_after_usdt"].round(2).to_numpy(),
            }
        ),
        width="stretch",
        hide_index=True,
    )
    download_button(
        archive.trades,
        filename="cq08_02_trade_attribution.csv",
        label="Scarica trade attribution (CSV)",
        key="stability_trades_csv",
    )

    with st.expander("Manifest, riconciliazione e vincoli", expanded=False):
        st.write(f"**Cartella archivio:** `{archive.directory}`")
        st.write(
            f"**Riconciliazione:** {reconciliation.get('checks_total', 'n/d')} controlli · "
            f"{reconciliation.get('checks_failed', 'n/d')} falliti · "
            f"delta massimo {reconciliation.get('max_abs_delta', 0.0):.3e}"
        )
        st.write(f"**Sorgenti invariate:** {archive.manifest.get('source_hashes_unchanged')}")
        st.write(f"**Policy congelata:** {json.dumps(archive.manifest.get('policy_frozen', {}))}")
        st.write("**Metodo:** " + json.dumps(archive.manifest.get("method", {}), ensure_ascii=False))
        st.caption(
            "Soglie e costi non sono stati ri-selezionati; nessun artefatto CP07/CP08-01 è stato modificato."
        )

    st.divider()
    st.caption(
        "Gate CP08-02: la riconciliazione numerica è verificata, ma la promozione della strategia "
        "resta un gate statistico separato. **Nessun GO al trading** deriva da questa vista."
    )
