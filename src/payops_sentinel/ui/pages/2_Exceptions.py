"""Exception queue: evidence, agent investigation trace and human review of drafts."""

from __future__ import annotations

import json

import streamlit as st

from payops_sentinel.agent.orchestrator import run_investigation
from payops_sentinel.domain.enums import ExceptionStatus, ExceptionType
from payops_sentinel.review import service
from payops_sentinel.review.service import DraftEdit, Permission, ReviewError, has_permission
from payops_sentinel.storage import repositories as repo
from payops_sentinel.ui.common import db, money, page_setup, settings

principal = page_setup("Exception and review queue")
cfg = settings()


def flash(kind: str, message: str) -> None:
    st.session_state["flash"] = (kind, message)


if "flash" in st.session_state:
    kind, message = st.session_state.pop("flash")
    getattr(st, kind)(message)

with db() as conn:
    batches = repo.list_batches(conn)
if not batches:
    st.info("No batches yet. Process a file on the Batches page first.")
    st.stop()

ids = [b["batch_id"] for b in batches]
current = st.session_state.get("batch_id")
f1, f2, f3 = st.columns(3)
batch_id = f1.selectbox("Batch", ids, index=ids.index(current) if current in ids else 0)
st.session_state["batch_id"] = batch_id
type_filter = f2.multiselect("Exception type", [t.value for t in ExceptionType])
status_filter = f3.multiselect("Status", [s.value for s in ExceptionStatus])

with db() as conn:
    rows = [
        e
        for e in repo.list_exceptions(conn, batch_id)
        if (not type_filter or e["exception_type"] in type_filter)
        and (not status_filter or e["status"] in status_filter)
    ]
st.dataframe(
    [
        {
            "exception_id": e["exception_id"],
            "row": e["source_row_number"],
            "employee_ref": e["employee_ref"],
            "type": e["exception_type"],
            "reason": e["reason_code"],
            "severity": e["severity"],
            "status": e["status"],
            "expected": money(e["expected_cents"]),
            "reported": money(e["actual_cents"]),
        }
        for e in rows
    ],
    hide_index=True,
    width="stretch",
)
if not rows:
    st.stop()

exc_ids = [e["exception_id"] for e in rows]
chosen = st.session_state.get("exception_id")
exception_id = st.selectbox(
    "Exception", exc_ids, index=exc_ids.index(chosen) if chosen in exc_ids else 0
)
st.session_state["exception_id"] = exception_id

with db() as conn:
    details = repo.exception_details(conn, exception_id)
    evidence = repo.list_evidence(conn, exception_id)
    runs = repo.list_agent_runs(conn, exception_id)
    drafts = repo.list_drafts(conn, exception_id)

st.divider()
st.subheader(f"{exception_id} · {details['exception_type']}")
m = st.columns([1, 1, 1, 2])
m[0].metric("Employee reference", details["employee_ref"] or "malformed")
m[1].metric("Expected (deterministic)", money(details["expected_cents"]))
m[2].metric("Reported", money(details["actual_cents"]))
m[3].metric("Status", details["status"].replace("_", " "))
st.write(f"**Finding** ({details['reason_code']}): {details['reason']}")
st.caption(
    f"Source row {details['source_row_number']} · pay period "
    f"{details['pay_period_start']} to {details['pay_period_end']} · rule "
    f"{details['rule_id'] or 'n/a'} · ruleset {details['ruleset_version']}"
)

with st.expander(f"Evidence ({len(evidence)} records)", expanded=False):
    for ev in evidence:
        st.markdown(f"**{ev['evidence_id']}** · `{ev['evidence_type']}` · {ev['source_reference']}")
        st.json(json.loads(ev["snapshot_json"]), expanded=False)
        st.caption(f"sha256 {ev['content_hash']}")

# ------------------------------------------------------------------ agent investigation
st.subheader("AI investigation")
can_investigate = has_permission(principal, Permission.AGENT_INVESTIGATE)
if st.button(
    "Run AI investigation",
    disabled=not can_investigate,
    type="primary",
    help=None if can_investigate else "Your role cannot start investigations.",
):
    with st.spinner("Local Qwen3 is selecting MCP tools and drafting (30-60 s)…"):
        outcome = run_investigation(cfg, exception_id)
    if outcome.draft:
        flash("success", f"{outcome.run_id}: draft {outcome.draft_id} ready for human review.")
    else:
        flash(
            "warning",
            f"{outcome.run_id}: routed to manual review — "
            f"{outcome.failure_category.value} ({outcome.failure_detail}).",
        )
    st.rerun()

if runs:
    with db() as conn:
        for run in runs[:3]:
            label = (
                f"{run['run_id']} · {run['status']} · {run['model_version']} · "
                f"prompt {run['prompt_version']} · {run['duration_ms']} ms"
            )
            with st.expander(label, expanded=run is runs[0]):
                if run["error_category"]:
                    st.warning(f"Manual review: {run['error_category']} — {run['error_detail']}")
                st.dataframe(
                    [
                        {
                            "#": t["sequence"],
                            "actor": t["actor"],
                            "tool": t["tool_name"],
                            "arguments": t["arguments_json"],
                            "result": "ok" if t["success"] else t["error_category"],
                            "ms": t["duration_ms"],
                        }
                        for t in repo.list_tool_events(conn, run["run_id"])
                    ],
                    hide_index=True,
                    width="stretch",
                )
                st.caption(
                    "actor=model: chosen by Qwen3 and validated by the host before "
                    "execution · actor=host: ground-truth reads used to verify the draft "
                    "(in host-prefetch mode also given to the model as its first results)"
                )
else:
    st.caption("No investigation has run for this exception yet.")

# ------------------------------------------------------------------ human review
st.subheader("Human review")
if not drafts:
    st.caption("No draft yet. Manual investigation remains available for every exception.")
    st.stop()

by_id: dict[str, list[dict]] = {}
for d in drafts:
    by_id.setdefault(d["draft_id"], []).append(d)
draft_id = st.selectbox("Draft", list(by_id), index=len(by_id) - 1)
versions = sorted(by_id[draft_id], key=lambda d: d["draft_version"])
latest = versions[-1]
body = json.loads(latest["draft_json"])

st.markdown(
    f"**{draft_id} v{latest['draft_version']}** · status **{latest['status']}** · "
    f"author `{latest['author_id']}`"
)
st.caption(f"content sha256 {latest['content_hash']}")
left, right = st.columns([3, 2])
with left:
    st.text_input(
        "Subject", body["email_subject"], disabled=True, key=f"s-{latest['content_hash']}"
    )
    st.text_area(
        "Body", body["email_body"], height=200, disabled=True, key=f"b-{latest['content_hash']}"
    )
with right:
    st.markdown(f"**Summary:** {body['investigation_summary']}")
    st.markdown(f"**Reasoning:** {body['reasoning_summary']}")
    st.markdown(f"**Proposed next step:** `{body['proposed_next_step']}`")
    st.markdown(f"**Evidence cited:** {', '.join(body['evidence_ids'])}")
    if body["limitations"]:
        st.markdown("**Limitations:** " + "; ".join(body["limitations"]))

pending = latest["status"] == "pending_review"
ref = {
    "draft_id": draft_id,
    "version": latest["draft_version"],
    "content_hash": latest["content_hash"],
}

if pending and has_permission(principal, Permission.DRAFT_EDIT):
    tab_edit, tab_approve, tab_reject = st.tabs(["Edit", "Approve for export", "Reject"])
    with tab_edit, st.form("edit"):
        subject = st.text_input("Subject", body["email_subject"])
        text = st.text_area("Body", body["email_body"], height=200)
        if st.form_submit_button("Save as new version"):
            try:
                with db() as conn:
                    r = service.edit_draft(
                        conn,
                        principal,
                        edit=DraftEdit(email_subject=subject, email_body=text),
                        **ref,
                    )
                flash("success", f"Saved {draft_id} v{r.draft_version}.")
                st.rerun()
            except (ReviewError, ValueError) as exc:
                st.error(str(exc))
    with tab_approve, st.form("approve"):
        st.caption(
            "Approves this exact version for export as a downloadable artifact. "
            "It does not approve the payroll exception and nothing is sent."
        )
        comment = st.text_input("Comment (optional)")
        if st.form_submit_button("Approve this version", type="primary"):
            try:
                with db() as conn:
                    r = service.approve_draft(
                        conn,
                        principal,
                        artifacts_dir=cfg.artifacts_dir,
                        comment=comment or None,
                        **ref,
                    )
                flash("success", f"Approved ({r.approval_id}). Artifact sha256 {r.artifact_sha256}")
                st.rerun()
            except ReviewError as exc:
                st.error(str(exc))
    with tab_reject, st.form("reject"):
        reason = st.text_area("Reason (required)")
        if st.form_submit_button("Reject draft"):
            try:
                with db() as conn:
                    r = service.reject_draft(conn, principal, reason=reason, **ref)
                flash("warning", f"Rejected ({r.approval_id}); exception set to manual review.")
                st.rerun()
            except ReviewError as exc:
                st.error(str(exc))
elif pending:
    st.info(f"Your role ({principal.role.value}) cannot edit, approve or reject drafts.")

if latest["status"] == "approved" and has_permission(principal, Permission.ARTIFACT_DOWNLOAD):
    with db() as conn:
        artifact = service.approved_artifact(conn, principal, draft_id)

    def _record_download() -> None:
        with db() as c:
            service.record_download(c, principal, draft_id, artifact.sha256)

    st.download_button(
        "Download approved artifact",
        artifact.content,
        file_name=artifact.file_name,
        mime="text/plain",
        on_click=_record_download,
    )
    st.caption(f"artifact sha256 {artifact.sha256}")

with st.expander(f"Version history ({len(versions)})"):
    st.dataframe(
        [
            {
                k: v[k]
                for k in ("draft_version", "status", "author_id", "content_hash", "created_at")
            }
            for v in versions
        ],
        hide_index=True,
        width="stretch",
    )
with st.expander("Audit trail for this exception"), db() as conn:
    events = repo.list_audit_events(conn, object_ids=[exception_id, *by_id])
    st.dataframe(
        [
            {
                k: e[k]
                for k in (
                    "sequence",
                    "created_at",
                    "actor_id",
                    "event_type",
                    "object_id",
                    "event_hash",
                )
            }
            for e in events
        ],
        hide_index=True,
        width="stretch",
    )
