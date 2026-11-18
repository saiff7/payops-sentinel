"""PayOps Sentinel — Streamlit entry point.  uv run streamlit run src/payops_sentinel/ui/app.py"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

UI = Path(__file__).parent

st.navigation(
    [
        st.Page(UI / "overview.py", title="Overview", default=True),
        st.Page(UI / "pages" / "1_Batches.py", title="Batches", url_path="Batches"),
        st.Page(UI / "pages" / "2_Exceptions.py", title="Exceptions", url_path="Exceptions"),
        st.Page(UI / "pages" / "3_Governance.py", title="Governance", url_path="Governance"),
    ]
).run()
