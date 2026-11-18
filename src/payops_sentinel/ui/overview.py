"""Overview page: headline metrics and the workflow."""

from __future__ import annotations

import streamlit as st

from payops_sentinel.storage import repositories as repo
from payops_sentinel.ui.common import db, page_setup

page_setup("Overview")

st.markdown(
    "Governed investigation of **synthetic** payroll contribution exceptions.\n\n"
    "> Python decides **what** is wrong. The agent investigates **why**. "
    "A human decides what may leave the system."
)

with db() as conn:
    b = repo.batch_metrics(conn)
    queue = {r["status"]: r["drafts"] for r in repo.review_metrics(conn)}

c1, c2, c3, c4 = st.columns(4)
c1.metric("Batches processed", b["batches"])
c2.metric("Open exceptions found", b["exceptions"])
c3.metric("Drafts awaiting review", queue.get("pending_review", 0))
c4.metric("Drafts approved for export", queue.get("approved", 0))

st.subheader("Workflow")
st.markdown(
    "1. **Batches** — upload a payroll CSV; validation and deterministic reconciliation run "
    "without any AI.\n"
    "2. **Exceptions** — inspect evidence, run the local Qwen3 agent (read-only MCP tools), then "
    "edit, approve or reject its draft as a reviewer.\n"
    "3. **Governance** — agent registry, permissions, readiness gate, operational metrics and "
    "audit-chain verification."
)
st.info(
    "Approval approves a *communication for export*, not the payroll exception. "
    "Approved artifacts are downloaded; nothing is sent."
)
