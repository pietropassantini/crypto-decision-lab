"""Shared Streamlit helpers for dashboard components."""
from __future__ import annotations

from typing import Iterable

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

PLOTLY_CONFIG = {
    "displayModeBar": True,
    "displaylogo": False,
    "responsive": True,
    "modeBarButtonsToRemove": ["lasso2d", "select2d"],
    "toImageButtonOptions": {
        "format": "png",
        "filename": "crypto_decision_lab",
        "height": 900,
        "width": 1600,
        "scale": 2,
    },
}


def fmt_price(value: float | int | None) -> str:
    if value is None or pd.isna(value):
        return "n/d"
    return f"{float(value):,.2f} USDT"


def fmt_number(value: float | int | None, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "n/d"
    return f"{float(value):,.{digits}f}"


def fmt_pct(value: float | int | None, digits: int = 2) -> str:
    if value is None or pd.isna(value):
        return "n/d"
    return f"{float(value):+,.{digits}f}%"


def fmt_timestamp(value) -> str:
    if value is None or pd.isna(value):
        return "n/d"
    timestamp = pd.Timestamp(value)
    return timestamp.strftime("%Y-%m-%d %H:%M UTC")


def show_plot(fig: go.Figure, *, key: str) -> None:
    st.plotly_chart(fig, width="stretch", config=PLOTLY_CONFIG, key=key)


def show_issues(issues: Iterable[str], *, title: str = "Avvisi dati") -> None:
    unique = list(dict.fromkeys(str(issue) for issue in issues if issue))
    if not unique:
        return
    with st.expander(f"{title} ({len(unique)})", expanded=False):
        for issue in unique:
            st.warning(issue)


def download_button(
    frame: pd.DataFrame,
    *,
    filename: str,
    label: str,
    key: str,
) -> None:
    if frame is None or frame.empty:
        return
    csv = frame.to_csv(index=True).encode("utf-8")
    st.download_button(
        label=label,
        data=csv,
        file_name=filename,
        mime="text/csv",
        key=key,
        width="content",
    )


def availability_line(forecasts, selected: Iterable[str]) -> str:
    parts = []
    for model in selected:
        info = forecasts.models.get(model)
        if info is None:
            parts.append(f"{model}: sconosciuto")
        elif not info.available:
            parts.append(f"{info.label}: non disponibile")
        else:
            parts.append(f"{info.label}: {info.horizons}")
    return " · ".join(parts)


def read_only_banner(latest_timestamp) -> None:
    st.markdown(
        f"""
        <div style="background:#111722;border:1px solid #253044;border-radius:8px;
                    padding:10px 14px;margin-bottom:10px;color:#8b98ab;font-size:0.86rem;">
          <b style="color:#d7dee8;">READ-ONLY</b> · dataset statico ·
          ultima candela disponibile: <b style="color:#f0b90b;">{fmt_timestamp(latest_timestamp)}</b> ·
          nessun prezzo live, nessun ordine, nessuna scrittura sul dataset.
        </div>
        """,
        unsafe_allow_html=True,
    )
