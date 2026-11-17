"""Shared Streamlit helpers. Pages read through repositories and act only through services."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import streamlit as st

from payops_sentinel.config import Settings, load_settings
from payops_sentinel.ingestion.reference_loader import seed_reference_data
from payops_sentinel.review.service import DEMO_PRINCIPALS, Principal, Role
from payops_sentinel.storage.db import connect, initialize

HUMAN_PRINCIPALS = [p for p in DEMO_PRINCIPALS.values() if p.role is not Role.AGENT]


def settings() -> Settings:
    override = os.environ.get("PAYOPS_CONFIG")
    return load_settings(Path(override) if override else None)


@st.cache_resource(show_spinner=False)
def _bootstrap(db_path: str, reference_dir: str) -> bool:
    conn = connect(Path(db_path))
    try:
        initialize(conn)
        seed_reference_data(conn, Path(reference_dir))
    finally:
        conn.close()
    return True


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    s = settings()
    _bootstrap(str(s.db_path), str(s.reference_data_dir))
    conn = connect(s.db_path)
    try:
        yield conn
    finally:
        conn.close()


def page_setup(title: str) -> Principal:
    """Common banner and the simulated sign-in. Returns the acting principal."""
    st.set_page_config(page_title=f"PayOps Sentinel · {title}", layout="wide")
    with st.sidebar:
        st.markdown("### Acting as")
        ids = [p.principal_id for p in HUMAN_PRINCIPALS]
        default = ids.index(st.session_state.get("principal_id", "reviewer.demo"))
        chosen = st.selectbox(
            "Simulated identity",
            ids,
            index=default,
            format_func=lambda pid: f"{DEMO_PRINCIPALS[pid].display_name} ({pid})",
            help="Local demo only. Production would authenticate with OIDC and map roles in RBAC.",
        )
        st.session_state["principal_id"] = chosen
        principal = DEMO_PRINCIPALS[chosen]
        st.caption(f"Role: **{principal.role.value}**")
        st.divider()
        st.caption("Synthetic demonstration data only. PayOps Sentinel never sends email.")
    st.title(title)
    return principal


def money(cents: int | None) -> str:
    return "—" if cents is None else f"${cents / 100:,.2f}"
