"""Standalone Streamlit entry point for Strategy Studio (S04).

Run from the repository root::

    streamlit run src/crypto_decision_lab/dashboard/studio_app.py

The Studio does not depend on the dashboard snapshot sidebar or on forecast
archives: it reads sources only through the S02 catalog.
"""
from __future__ import annotations

from pathlib import Path
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st

from crypto_decision_lab.dashboard.components.strategy_studio import render_strategy_studio
from crypto_decision_lab.dashboard.config import REPO_ROOT_FALLBACK

st.set_page_config(page_title="Strategy Studio", page_icon="◈", layout="wide")


def main() -> None:
    st.title("Crypto Decision Lab")
    render_strategy_studio(Path(REPO_ROOT_FALLBACK).resolve())


if __name__ == "__main__":
    main()
