"""Governance: asset registry, permissions, readiness, operations and audit verification."""

from __future__ import annotations

import tomllib

import streamlit as st

from payops_sentinel.agent.guardrails import ALLOWED_TOOLS
from payops_sentinel.config import PROJECT_ROOT
from payops_sentinel.operations.registry import (
    load_agent_asset,
    load_tool_permissions,
    readiness_checks,
)
from payops_sentinel.operations.telemetry import operational_snapshot
from payops_sentinel.review.audit import verify_chain
from payops_sentinel.storage import repositories as repo
from payops_sentinel.ui.common import db, page_setup, settings

page_setup("Governance and operations")
cfg = settings()
asset = load_agent_asset(cfg.agent.manifest_path)

st.subheader("Agent asset registry")
st.dataframe(
    [{"field": k, "value": v} for k, v in asset.model_dump().items()],
    hide_index=True,
    width="stretch",
)

checks = readiness_checks(
    asset,
    project_root=PROJECT_ROOT,
    enforced_tools=frozenset(ALLOWED_TOOLS),
    manifest_tools=load_tool_permissions(cfg.agent.tool_permissions_path, "agent_service"),
    latest_evaluation_passed=None,
)
ready = all(c.passed for c in checks)
(st.success if ready else st.warning)(
    "Operationally ready" if ready else "Not operationally ready — demo release (see failed checks)"
)
st.dataframe(
    [{"check": c.check, "passed": "✅" if c.passed else "❌", "detail": c.detail} for c in checks],
    hide_index=True,
    width="stretch",
)

st.subheader("Tool permissions")
with cfg.agent.tool_permissions_path.open("rb") as fh:
    principals = tomllib.load(fh)["principal"]
st.dataframe(
    [
        {
            "principal": name,
            "permission": p["permission"],
            "tools": ", ".join(p["tools"]),
            **{k: v for k, v in p.items() if k.startswith("can_")},
        }
        for name, p in principals.items()
    ],
    hide_index=True,
    width="stretch",
)
st.caption(
    "Enforced host-side allowlist: "
    + ", ".join(ALLOWED_TOOLS)
    + ". MCP read-only annotations are descriptive, not enforcement."
)

with (PROJECT_ROOT / "governance" / "data_classification.toml").open("rb") as fh:
    classes = tomllib.load(fh)["classifications"]
with st.expander("Data classification"):
    st.dataframe(
        [
            {"classification": k, "examples": ", ".join(v["examples"]), "treatment": v["treatment"]}
            for k, v in classes.items()
        ],
        hide_index=True,
        width="stretch",
    )

st.subheader("Operational metrics")
with db() as conn:
    snap = operational_snapshot(conn)
b = snap["batches"]
c = st.columns(5)
c[0].metric("Batches", b["batches"])
c[1].metric("Rows processed", b["rows_processed"])
c[2].metric("Rows rejected", b["rows_rejected"])
c[3].metric("Exceptions", b["exceptions"])
c[4].metric("Avg batch time", f"{b['avg_duration_ms']:.0f} ms")
left, right = st.columns(2)
with left:
    st.markdown("**Agent runs**")
    st.dataframe(snap["agent_runs"], hide_index=True, width="stretch")
    st.markdown("**Review queue** (latest version of each draft)")
    st.dataframe(snap["review_queue"], hide_index=True, width="stretch")
with right:
    st.markdown("**MCP tool activity**")
    st.dataframe(snap["tool_activity"], hide_index=True, width="stretch")

st.subheader("Evaluations")
st.info(
    "No evaluation run recorded yet. The 30-case evaluation harness arrives in Phase 4; "
    "no scores are shown until genuine runs exist."
)

st.subheader("Audit chain")
if st.button("Verify audit chain"):
    with db() as conn:
        result = verify_chain(conn)
    if result.valid:
        st.success(f"Chain intact: {result.events_checked} events verified.")
    else:
        st.error(f"Chain broken at sequence {result.first_invalid_sequence}: {result.reason}")
with db() as conn:
    events = repo.list_audit_events(conn)[-50:]
st.dataframe(
    [
        {
            k: e[k]
            for k in ("sequence", "created_at", "actor_id", "event_type", "object_id", "event_hash")
        }
        for e in reversed(events)
    ],
    hide_index=True,
    width="stretch",
)
st.caption(
    "Tamper-evident (hash chain + append-only triggers), not immutable: production would "
    "export events to independently controlled append-only storage."
)
