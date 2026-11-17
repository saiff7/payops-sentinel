"""Batch operations: upload, history, validation failures and exception totals."""

from __future__ import annotations

from collections import Counter

import streamlit as st

from payops_sentinel.domain.models import FileRejectedError
from payops_sentinel.ingestion.service import ingest_payroll_file
from payops_sentinel.review.service import AuthorizationError, Permission, authorize
from payops_sentinel.storage import repositories as repo
from payops_sentinel.ui.common import db, page_setup, settings

principal = page_setup("Batch operations")
cfg = settings()


def process(content: bytes, file_name: str) -> None:
    with db() as conn:
        try:
            authorize(conn, principal, Permission.BATCH_UPLOAD, f"upload:{file_name[:40]}")
            result = ingest_payroll_file(
                conn, cfg, content=content, file_name=file_name, actor_id=principal.principal_id
            )
        except AuthorizationError:
            st.error("Your role cannot upload batches. The attempt was recorded in the audit log.")
            return
        except FileRejectedError as exc:
            st.error(f"File rejected — {exc.code.value}: {exc.message}. Nothing was stored.")
            return
    if result.created:
        st.success(
            f"{result.batch_id} processed: {result.total_rows} rows, {result.invalid_rows} "
            f"invalid, {result.exception_count} exceptions."
        )
    else:
        st.info(f"Identical file already processed as {result.batch_id} (idempotent).")
    st.session_state["batch_id"] = result.batch_id


st.subheader("Submit a payroll contribution file")
st.caption(
    "Accepted columns only: employee_ref, pay_period_start, pay_period_end, "
    "eligible_compensation, employee_contribution. Restricted data rejects the whole file."
)
upload = st.file_uploader("Synthetic payroll CSV", type=["csv"])
left, mid, right = st.columns(3)
if left.button("Process uploaded file", disabled=upload is None, type="primary"):
    assert upload is not None
    process(upload.getvalue(), upload.name)
demo_dir = cfg.reference_data_dir
if mid.button("Process demo file with exceptions"):
    process((demo_dir / "payroll_with_exceptions.csv").read_bytes(), "payroll_with_exceptions.csv")
if right.button("Process clean demo file"):
    process((demo_dir / "payroll_clean.csv").read_bytes(), "payroll_clean.csv")

st.divider()
st.subheader("Batch history")
with db() as conn:
    batches = repo.list_batches(conn)
    if not batches:
        st.info("No batches yet. Process a demo file above.")
        st.stop()
    st.dataframe(
        [
            {
                k: b[k]
                for k in (
                    "batch_id",
                    "file_name",
                    "pay_period_end",
                    "status",
                    "total_rows",
                    "valid_rows",
                    "invalid_rows",
                    "exception_count",
                    "duration_ms",
                    "created_at",
                )
            }
            for b in batches
        ],
        hide_index=True,
        width="stretch",
    )
    ids = [b["batch_id"] for b in batches]
    current = st.session_state.get("batch_id")
    batch_id = st.selectbox("Batch", ids, index=ids.index(current) if current in ids else 0)
    st.session_state["batch_id"] = batch_id
    batch = repo.get_batch(conn, batch_id)
    exceptions = repo.list_exceptions(conn, batch_id)
    errors = repo.list_validation_errors(conn, batch_id)

c = st.columns(5)
c[0].metric("Rows", batch["total_rows"])
c[1].metric("Valid", batch["valid_rows"])
c[2].metric("Invalid", batch["invalid_rows"])
c[3].metric("Exceptions", batch["exception_count"])
c[4].metric("Processing time", f"{batch['duration_ms']} ms")
st.caption(
    f"File SHA-256 {batch['file_sha256']} · ruleset {batch['ruleset_version']} · "
    f"{batch['data_classification']}"
)

left, right = st.columns(2)
with left:
    st.markdown("**Exceptions by type**")
    counts = Counter(e["exception_type"] for e in exceptions)
    st.dataframe(
        [{"exception_type": k, "count": v} for k, v in sorted(counts.items())],
        hide_index=True,
        width="stretch",
    )
with right:
    st.markdown("**Validation failures** (raw values are never stored)")
    st.dataframe(
        [
            {k: e[k] for k in ("source_row_number", "field", "reason_code", "message")}
            for e in errors
        ],
        hide_index=True,
        width="stretch",
    )
st.caption("Next: open **Exceptions** in the sidebar to investigate and review.")
