"""Governance: asset registry, permissions, readiness, operations and audit verification."""

from __future__ import annotations

import json
import tomllib

import streamlit as st

from payops_sentinel.agent.guardrails import ALLOWED_TOOLS
from payops_sentinel.agent.prompts import prompt_version
from payops_sentinel.config import PROJECT_ROOT
from payops_sentinel.operations.registry import (
    latest_matching_evaluation,
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

with db() as conn:
    evaluations = repo.list_evaluation_runs(conn)
latest_eval = evaluations[0] if evaluations else None
gate_eval = latest_matching_evaluation(
    evaluations, model=cfg.agent.model, prompt_version=prompt_version(cfg.agent.prefetch)
)

checks = readiness_checks(
    asset,
    project_root=PROJECT_ROOT,
    enforced_tools=frozenset(ALLOWED_TOOLS),
    manifest_tools=load_tool_permissions(cfg.agent.tool_permissions_path, "agent_service"),
    latest_evaluation_passed=bool(gate_eval["passed"]) if gate_eval else None,
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
if not evaluations:
    st.info(
        "No evaluation run recorded yet. Run `uv run payops evaluate`; no scores are shown "
        "until genuine runs exist."
    )
else:
    latest_metrics = json.loads(latest_eval["metrics_json"])
    gate = "PASSED" if latest_eval["passed"] else "FAILED"
    st.caption(
        f"Readiness uses the latest evaluation of the configured agent ({cfg.agent.model}, prompt "
        f"{prompt_version(cfg.agent.prefetch)}): "
        f"{gate_eval['evaluation_id'] if gate_eval else 'none yet'}."
    )
    st.markdown(
        f"Latest: **{latest_eval['evaluation_id']}** · model `{latest_eval['model_version']}`"
        f" · prompt {latest_eval['prompt_version']} · tools {latest_eval['tools_version']} "
        f"· release gate **{gate}**"
    )
    tiles = st.columns(4)
    for tile, (label, key) in zip(
        tiles,
        [
            ("Tool selection", "tool_selection_accuracy"),
            ("Schema validity", "schema_validity"),
            ("Evidence coverage", "evidence_coverage"),
            ("P95 latency", "latest_p95"),
        ],
        strict=True,
    ):
        if key == "latest_p95":
            tile.metric(label, f"{latest_metrics['latency_p95_ms'] / 1000:.1f} s")
        else:
            previous = json.loads(evaluations[1]["metrics_json"]) if len(evaluations) > 1 else {}
            delta = (latest_metrics[key] - previous[key]) if key in previous else None
            tile.metric(
                label,
                f"{latest_metrics[key]:.0%}",
                delta=None if delta is None else f"{delta:+.0%}",
            )
    st.markdown("**Evaluation history**")
    st.dataframe(
        [
            {
                "evaluation_id": e["evaluation_id"],
                "model": e["model_version"],
                "prompt": e["prompt_version"],
                "tools": e["tools_version"],
                "passed": bool(e["passed"]),
                **{
                    k: v
                    for k, v in json.loads(e["metrics_json"]).items()
                    if k
                    in (
                        "case_pass_rate",
                        "tool_selection_accuracy",
                        "tool_argument_accuracy",
                        "failed_model_tool_calls",
                        "critical_security_violations",
                    )
                },
            }
            for e in evaluations
        ],
        hide_index=True,
        width="stretch",
    )
    with db() as conn:
        failing = [
            c
            for c in repo.list_evaluation_cases(conn, latest_eval["evaluation_id"])
            if not c["passed"]
        ]
    if failing:
        st.markdown(f"**Failing cases in {latest_eval['evaluation_id']}**")
        st.dataframe(
            [
                {
                    "case_id": c["case_id"],
                    "actual_tools": c["actual_tools"],
                    "notes": "; ".join(json.loads(c["details_json"])["notes"]),
                }
                for c in failing
            ],
            hide_index=True,
            width="stretch",
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
