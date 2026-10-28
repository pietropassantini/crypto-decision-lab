"""Streamlit entry point for the read-only Crypto Decision Lab dashboard.

Run from the repository root::

    streamlit run src/crypto_decision_lab/dashboard/app.py
"""
from __future__ import annotations

from pathlib import Path
import sys

# Allow the documented command without requiring an editable install.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pandas as pd
import streamlit as st

from crypto_decision_lab.dashboard.components.forecast_inspector import render_forecast_inspector
from crypto_decision_lab.dashboard.components.market_explorer import render_market_explorer
from crypto_decision_lab.dashboard.components.model_comparison import render_model_comparison
from crypto_decision_lab.dashboard.components.overview import render_overview
from crypto_decision_lab.dashboard.config import DashboardConfig, discover_snapshots, load_config
from crypto_decision_lab.dashboard.data_loader import (
    DataValidationError,
    MarketData,
    clear_market_cache,
    load_market_data,
)
from crypto_decision_lab.dashboard.forecast_adapter import (
    ForecastDataset,
    clear_forecast_cache,
    load_forecasts,
    validate_forecast_actuals,
)
from crypto_decision_lab.dashboard.indicators import (
    IndicatorRequest,
    format_indicator_label,
    indicator_specs,
)

st.set_page_config(
    page_title="Crypto Decision Lab · S03-CP01",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .stApp { background: #0b0e14; }
    .block-container { padding-top: 1.1rem; padding-bottom: 2rem; max-width: 1600px; }
    [data-testid="stMetric"] {
        background: #111722; border: 1px solid #253044; border-radius: 9px;
        padding: 10px 12px;
    }
    [data-testid="stMetricLabel"] { color: #8b98ab; }
    [data-testid="stMetricValue"] { color: #d7dee8; }
    [data-testid="stSidebar"] { background: #0d121c; border-right: 1px solid #1f2937; }
    h1, h2, h3 { letter-spacing: -0.02em; }
    .cdl-kicker { color:#4cc9f0; font-size:0.78rem; letter-spacing:0.08em; text-transform:uppercase; }
    </style>
    """,
    unsafe_allow_html=True,
)


@st.cache_data(ttl=300, show_spinner=False)
def _market_cached(data_root: str, snapshot_dir: str, verify: bool) -> MarketData:
    return load_market_data(data_root, snapshot_dir, verify_checksums=verify)


@st.cache_data(ttl=300, show_spinner=False)
def _forecasts_cached(
    batch_dir: str,
    residual_dir: str,
    corrected_residual_dir: str,
    snapshot_dir: str,
    decision_glob: str,
) -> ForecastDataset:
    return load_forecasts(
        batch_dir,
        residual_dir or None,
        snapshot_dir,
        decision_glob or None,
        corrected_residual_dir or None,
    )


def _preset_label(item: dict) -> str:
    specs = indicator_specs()
    key = str(item.get("key", ""))
    if key not in specs:
        return key
    request = IndicatorRequest(key, dict(item.get("params") or {}))
    return format_indicator_label(request, specs[key])


def _indicator_request(item: dict, index: int) -> IndicatorRequest:
    key = str(item["key"])
    params = dict(item.get("params") or {})
    spec = indicator_specs()[key]
    st.sidebar.markdown(f"**{_preset_label(item)}**")
    for name, default in spec.default_params.items():
        widget_key = f"indicator_{index}_{name}"
        if isinstance(default, bool):
            params[name] = st.sidebar.checkbox(name, value=bool(params.get(name, default)), key=widget_key)
        elif isinstance(default, float):
            params[name] = st.sidebar.number_input(
                name,
                min_value=0.01,
                value=float(params.get(name, default)),
                step=0.1,
                key=widget_key,
            )
        else:
            params[name] = st.sidebar.number_input(
                name,
                min_value=0,
                value=int(params.get(name, default)),
                step=1,
                key=widget_key,
            )
    return IndicatorRequest(key, params)


def _date_range(market: MarketData, config: DashboardConfig) -> tuple[pd.Timestamp, pd.Timestamp]:
    latest = market.latest_timestamp
    earliest = market.first_timestamp
    default_start = max(earliest, latest - pd.Timedelta(days=config.default_days))
    values = st.sidebar.date_input(
        "Periodo (UTC)",
        value=(default_start.date(), latest.date()),
        min_value=earliest.date(),
        max_value=latest.date(),
        key="date_range",
    )
    if isinstance(values, (tuple, list)) and len(values) == 2:
        start_date, end_date = values
    else:
        start_date = end_date = values
    start = pd.Timestamp(start_date, tz="UTC")
    end_exclusive = pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1)
    if start >= end_exclusive:
        raise DataValidationError("La data iniziale deve precedere quella finale")
    return start, end_exclusive


def _model_selector(forecasts: ForecastDataset, config: DashboardConfig) -> list[str]:
    options = list(forecasts.models)
    available_defaults = [
        model
        for model in config.default_models
        if model in forecasts.models and forecasts.models[model].available
    ]
    if not available_defaults:
        available_defaults = [model for model in ("timestral", "naive") if model in forecasts.models]

    def label(model: str) -> str:
        info = forecasts.models[model]
        marker = "✓" if info.available else "—"
        return f"{marker} {info.label} · {info.horizons}"

    selected = st.sidebar.multiselect(
        "Serie forecast",
        options=options,
        default=[model for model in available_defaults if model in options],
        format_func=label,
        key="selected_models",
    )
    unavailable = [model for model in selected if not forecasts.models[model].available]
    if unavailable:
        st.sidebar.caption(
            "Non disponibili (nessun valore simulato): "
            + ", ".join(forecasts.models[model].label for model in unavailable)
        )
    return selected


def _indicator_selector(config: DashboardConfig) -> list[IndicatorRequest]:
    presets = list(config.indicator_presets)
    labels = [_preset_label(item) for item in presets]
    defaults = [
        label
        for label, item in zip(labels, presets)
        if item.get("key") in {"sma", "ema"}
        and int((item.get("params") or {}).get("window", 0)) in {20, 50}
    ][:2]
    if not defaults and labels:
        defaults = labels[:1]
    selected = st.sidebar.multiselect(
        "Indicatori tecnici",
        options=labels,
        default=defaults,
        key="selected_indicators",
        help="Aggiungi/rimuovi indicatori. I parametri sono modificabili qui sotto.",
    )
    requests: list[IndicatorRequest] = []
    for label in selected:
        item = presets[labels.index(label)]
        with st.sidebar.expander(label, expanded=False):
            requests.append(_indicator_request(item, labels.index(label)))
    st.sidebar.caption(
        "Gli indicatori sono calcolati sul timeframe selezionato. "
        "Nuovi indicatori si registrano con `register_indicator` in `indicators.py`."
    )
    return requests


def _window_selector(forecasts: ForecastDataset) -> int | None:
    if forecasts.windows.empty:
        return None
    options = []
    for row in forecasts.windows.sort_values("cutoff", ascending=False).itertuples(index=False):
        options.append((int(row.window_index), f"window-{int(row.window_index):04d} · {pd.Timestamp(row.cutoff):%Y-%m-%d %H:%M} UTC"))
    selected = st.sidebar.selectbox(
        "Finestra forecast (cutoff / inspector)",
        options=options,
        format_func=lambda item: item[1],
        key="global_window",
    )
    return int(selected[0])


def main() -> None:
    config = load_config()
    st.markdown('<div class="cdl-kicker">S03-CP01 · forecast & market analysis</div>', unsafe_allow_html=True)
    st.title("Crypto Decision Lab")
    st.caption(
        "Dashboard read-only per BTCUSDT: storico reale, Timestral, baseline statistiche e "
        "futuro Decision Model. Nessun trading, nessun download, nessuna modifica allo snapshot."
    )

    st.sidebar.header("Configurazione")
    st.sidebar.caption("Fonte dati e archivio forecast")
    snapshots = discover_snapshots(config.data_root, config.snapshot_dir)
    snapshot_labels = {path.name: path for path in snapshots}
    selected_snapshot_name = st.sidebar.selectbox(
        "Snapshot",
        options=list(snapshot_labels),
        index=list(snapshot_labels).index(config.snapshot_dir.name)
        if config.snapshot_dir.name in snapshot_labels
        else 0,
        key="snapshot",
    )
    snapshot_dir = snapshot_labels[selected_snapshot_name]
    verify = st.sidebar.checkbox(
        "Verifica SHA-256 chunk",
        value=config.verify_checksums,
        help="Verifica i file Parquet del manifest. Non modifica nulla.",
    )
    if st.sidebar.button("Aggiorna cache", width="stretch"):
        st.cache_data.clear()
        clear_market_cache()
        clear_forecast_cache()
        st.rerun()

    try:
        market = _market_cached(str(config.data_root), str(snapshot_dir), verify)
    except (DataValidationError, FileNotFoundError, ValueError) as exc:
        st.error(f"Impossibile leggere lo snapshot di mercato: {exc}")
        st.info(
            "Esegui la dashboard dalla radice del repository e verifica "
            "`data/snapshots/BTCUSDT-1h-raw-v1/`."
        )
        st.stop()

    st.sidebar.success(f"{market.dataset_id} · {market.rows:,} barre · {market.latest_timestamp:%Y-%m-%d %H:%M} UTC")
    with st.sidebar.expander("Integrità dataset", expanded=False):
        st.write(f"Chunk referenziati: {market.source_files}")
        st.write(f"Quarantena inclusa read-only: {market.quarantined_files}")
        st.write(f"Ore mancanti: {market.gap_count}")
        st.write(f"Manifest: `{market.source_manifest_path}`")
        for issue in market.issues:
            st.caption(issue)

    try:
        start, end_exclusive = _date_range(market, config)
    except DataValidationError as exc:
        st.sidebar.error(str(exc))
        st.stop()

    timeframe = st.sidebar.radio(
        "Timeframe",
        options=["1h", "4h", "1D"],
        index=["1h", "4h", "1D"].index(config.default_timeframe)
        if config.default_timeframe in {"1h", "4h", "1D"}
        else 2,
        horizontal=True,
        key="timeframe",
    )
    view = st.sidebar.radio(
        "Vista prezzo",
        options=["line", "candlestick"],
        format_func=lambda value: "Line" if value == "line" else "Candlestick OHLC",
        horizontal=True,
        key="view",
    )
    forecast_mode = st.sidebar.radio(
        "Modalità forecast",
        options=["h24_common", "cutoff_path"],
        format_func=lambda value: (
            "Confronto +24h su timestamp comuni"
            if value == "h24_common"
            else "Percorso 1–24h della finestra selezionata"
        ),
        index=0 if config.default_forecast_mode != "cutoff_path" else 1,
        key="forecast_mode",
    )

    expansion_available = bool(
        config.expansion_dir and (config.expansion_dir / "config.json").is_file()
    )
    active_batch_dir = config.expansion_dir if expansion_available else config.batch_dir
    try:
        forecasts = _forecasts_cached(
            str(active_batch_dir),
            str(config.residual_baselines_dir) if config.residual_baselines_dir else "",
            str(config.residual_corrected_dir) if config.residual_corrected_dir else "",
            str(snapshot_dir),
            config.decision_glob or "",
        )
    except Exception as exc:  # defensive: show real candles even if archive is broken
        st.error(f"Archivio forecast non leggibile: {exc}")
        forecasts = ForecastDataset(
            points=pd.DataFrame(),
            windows=pd.DataFrame(),
            models={},
            issues=[str(exc)],
        )

    if forecasts.empty:
        st.warning(
            "Nessun forecast archiviato disponibile. La dashboard mostra comunque BTCUSDT reale; "
            "nessun valore forecast viene inventato."
        )
    selected_models = _model_selector(forecasts, config)
    selected_window = _window_selector(forecasts)
    selected_indicators = _indicator_selector(config)
    horizon_choice = st.sidebar.selectbox(
        "Orizzonte Model Comparison",
        options=[24, None],
        format_func=lambda value: "+24h (target comuni)" if value == 24 else "Tutti i target 1–24h",
        index=0,
        key="comparison_horizon",
    )
    show_forecast = st.sidebar.checkbox("Mostra forecast nel Market Explorer", value=True)

    if expansion_available:
        st.sidebar.caption(
            f"Archivio attivo: S02-CP06A ({forecasts.windows.shape[0]} finestre canonical) · "
            "CP05R preferito per Ridge/HistGB"
        )
    if forecasts.issues:
        with st.sidebar.expander("Avvisi archivio forecast", expanded=False):
            for issue in forecasts.issues[:12]:
                st.caption(issue)
            if len(forecasts.issues) > 12:
                st.caption(f"... e altri {len(forecasts.issues) - 12} avvisi")

    cross_issues = []
    if not forecasts.empty:
        cross_issues = validate_forecast_actuals(forecasts.points, market.frame)
    if cross_issues:
        with st.sidebar.expander("Cross-check actual vs mercato", expanded=False):
            for issue in cross_issues[:12]:
                st.caption(issue)

    tabs = st.tabs(["Overview", "Market Explorer", "Model Comparison", "Forecast Inspector"])
    with tabs[0]:
        render_overview(
            market,
            forecasts,
            config,
            start=start,
            end_exclusive=end_exclusive,
            timeframe=timeframe,
            view=view,
            selected_models=selected_models,
            forecast_mode=forecast_mode,
            selected_window=selected_window,
            indicator_requests=selected_indicators,
        )
    with tabs[1]:
        render_market_explorer(
            market,
            forecasts,
            config,
            start=start,
            end_exclusive=end_exclusive,
            timeframe=timeframe,
            view=view,
            selected_models=selected_models,
            forecast_mode=forecast_mode,
            selected_window=selected_window,
            indicator_requests=selected_indicators,
            show_forecast=show_forecast,
        )
    with tabs[2]:
        render_model_comparison(
            market,
            forecasts,
            config,
            start=start,
            end_exclusive=end_exclusive,
            selected_models=selected_models,
            horizon=horizon_choice,
        )
    with tabs[3]:
        render_forecast_inspector(
            market,
            forecasts,
            config,
            selected_models=selected_models,
            selected_window=selected_window,
        )

    st.divider()
    st.caption(
        f"Read-only · snapshot `{market.dataset_id}` · ultima candela {market.latest_timestamp} · "
        "I forecast sono indipendenti per finestra; i target h24 cadono a cutoff+23h. "
        "Nessun prezzo live o dato sintetico."
    )


if __name__ == "__main__":
    main()
