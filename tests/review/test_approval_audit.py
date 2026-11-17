"""Draft versioning, approval/rejection and the tamper-evident audit chain."""

from __future__ import annotations

import json
import sqlite3

import pytest

from payops_sentinel.hashing import sha256_hex
from payops_sentinel.review import service
from payops_sentinel.review.audit import AuditEventType, verify_chain
from payops_sentinel.review.service import DraftEdit, StaleDraftError, resolve_principal
from payops_sentinel.storage import repositories as repo

REVIEWER = resolve_principal("reviewer.demo")
NEW_BODY = (
    "Hello, our review of the October 31, 2026 file found a contribution record for EMP-0017 "
    "that needs clarification. Please confirm or send a corrected record."
)


def _approve(conn, settings, ref, **kw):
    return service.approve_draft(conn, REVIEWER, artifacts_dir=settings.artifacts_dir, **ref, **kw)


def _events(conn, event_type):
    return [e for e in repo.list_audit_events(conn) if e["event_type"] == event_type]


def test_reviewer_approves_exact_version(conn, settings, pending_draft):
    result = _approve(conn, settings, pending_draft, comment="looks right")
    assert result.status == "approved"
    draft = repo.get_draft(conn, pending_draft["draft_id"], 1)
    assert draft["status"] == "approved"
    (approval,) = repo.list_approvals(conn, pending_draft["draft_id"])
    assert (approval["reviewer_id"], approval["action"], approval["draft_version"]) == (
        "reviewer.demo",
        "approve",
        1,
    )
    assert approval["content_hash"] == pending_draft["content_hash"]
    # approval of the communication does not resolve the exception
    assert repo.exception_details(conn, "EXC-000004")["status"] != "closed"
    (event,) = _events(conn, AuditEventType.DRAFT_APPROVED)
    payload = json.loads(event["payload_json"])
    assert (event["actor_id"], payload["content_hash"]) == (
        "reviewer.demo",
        pending_draft["content_hash"],
    )
    assert payload["artifact_sha256"] == result.artifact_sha256


def test_artifact_is_linked_to_version_and_audit(conn, settings, pending_draft):
    result = _approve(conn, settings, pending_draft)
    data = (settings.artifacts_dir / f"{pending_draft['draft_id']}-v1-approved.txt").read_bytes()
    assert sha256_hex(data) == result.artifact_sha256
    text = data.decode()
    assert "NOT SENT" in text and pending_draft["content_hash"] in text and "EMP-0017" in text
    rebuilt = service.approved_artifact(conn, REVIEWER, pending_draft["draft_id"])
    assert rebuilt.content == data
    service.record_download(conn, REVIEWER, pending_draft["draft_id"], rebuilt.sha256)
    assert len(_events(conn, AuditEventType.ARTIFACT_DOWNLOADED)) == 1


def test_edit_creates_new_version_and_supersedes_old(conn, settings, pending_draft):
    edit = DraftEdit(email_subject="Payroll record needs clarification", email_body=NEW_BODY)
    v2 = service.edit_draft(conn, REVIEWER, edit=edit, **pending_draft)
    assert v2.draft_version == 2 and v2.content_hash != pending_draft["content_hash"]
    v1 = repo.get_draft(conn, pending_draft["draft_id"], 1)
    assert v1["status"] == "superseded"
    assert sha256_hex(v1["draft_json"]) == pending_draft["content_hash"]  # original preserved
    stored = repo.get_draft(conn, pending_draft["draft_id"], 2)
    assert stored["author_id"] == "reviewer.demo"
    assert json.loads(stored["draft_json"])["email_body"] == NEW_BODY
    assert len(_events(conn, AuditEventType.DRAFT_EDITED)) == 1


def test_superseded_version_cannot_be_approved(conn, settings, pending_draft):
    edit = DraftEdit(email_subject="Payroll record needs clarification", email_body=NEW_BODY)
    service.edit_draft(conn, REVIEWER, edit=edit, **pending_draft)
    with pytest.raises(StaleDraftError, match="superseded"):
        _approve(conn, settings, pending_draft)


def test_hash_mismatch_cannot_be_approved(conn, settings, pending_draft):
    with pytest.raises(StaleDraftError, match="hash"):
        _approve(conn, settings, pending_draft | {"content_hash": "0" * 64})
    assert repo.list_approvals(conn, pending_draft["draft_id"]) == []


def test_approved_draft_cannot_be_approved_edited_or_rejected_again(conn, settings, pending_draft):
    _approve(conn, settings, pending_draft)
    edit = DraftEdit(email_subject="Payroll record needs clarification", email_body=NEW_BODY)
    with pytest.raises(StaleDraftError, match="already approved"):
        _approve(conn, settings, pending_draft)
    with pytest.raises(StaleDraftError):
        service.edit_draft(conn, REVIEWER, edit=edit, **pending_draft)
    with pytest.raises(StaleDraftError):
        service.reject_draft(conn, REVIEWER, reason="changed my mind", **pending_draft)


def test_reviewer_rejects_with_reason(conn, pending_draft):
    with pytest.raises(service.InvalidDraftError, match="reason"):
        service.reject_draft(conn, REVIEWER, reason=" ", **pending_draft)
    result = service.reject_draft(conn, REVIEWER, reason="Wrong pay period cited", **pending_draft)
    assert result.status == "rejected"
    assert repo.get_draft(conn, pending_draft["draft_id"], 1)["status"] == "rejected"
    assert repo.exception_details(conn, "EXC-000004")["status"] == "manual_review"
    with pytest.raises(service.InvalidDraftError, match="not been approved"):
        service.approved_artifact(conn, REVIEWER, pending_draft["draft_id"])


def test_draft_content_and_approvals_are_immutable_in_the_database(conn, settings, pending_draft):
    _approve(conn, settings, pending_draft)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("UPDATE drafts SET draft_json = '{}'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE approvals SET reviewer_id = 'agent_service'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM approvals")


# ------------------------------------------------------------------ audit chain


def test_audit_chain_covers_the_workflow_and_verifies(conn, settings, pending_draft):
    edit = DraftEdit(email_subject="Payroll record needs clarification", email_body=NEW_BODY)
    v2 = service.edit_draft(conn, REVIEWER, edit=edit, **pending_draft)
    _approve(
        conn, settings, {"draft_id": v2.draft_id, "version": 2, "content_hash": v2.content_hash}
    )
    types = [e["event_type"] for e in repo.list_audit_events(conn)]
    assert types == ["BATCH_INGESTED", "DRAFT_EDITED", "DRAFT_APPROVED"]
    result = verify_chain(conn)
    assert result.valid and result.events_checked == 3


def _drop_audit_triggers(conn):
    conn.execute("DROP TRIGGER audit_events_no_update")
    conn.execute("DROP TRIGGER audit_events_no_delete")


def test_altered_history_is_detected(conn, settings, pending_draft):
    _approve(conn, settings, pending_draft)
    _drop_audit_triggers(conn)  # simulate an attacker with file-level database access
    conn.execute(
        "UPDATE audit_events SET actor_id = 'agent_service' WHERE event_type = 'DRAFT_APPROVED'"
    )
    result = verify_chain(conn)
    assert not result.valid and result.reason == "event content does not match its hash"
    assert result.first_invalid_sequence == 2


def test_deleted_history_is_detected(conn, settings, pending_draft):
    _approve(conn, settings, pending_draft)
    service.record_download(conn, REVIEWER, pending_draft["draft_id"], "x" * 64)
    _drop_audit_triggers(conn)
    conn.execute("DELETE FROM audit_events WHERE sequence = 2")
    result = verify_chain(conn)
    assert not result.valid and "removed" in result.reason


def test_rejected_upload_is_audited_without_content(conn, settings, make_csv, row):
    from payops_sentinel.domain.models import FileRejectedError
    from payops_sentinel.ingestion.service import ingest_payroll_file

    content = make_csv(row("123-45-6789", "1.00", "0.05"))
    with pytest.raises(FileRejectedError):
        ingest_payroll_file(conn, settings, content=content, file_name="x.csv", actor_id="op")
    (event,) = repo.list_audit_events(conn)
    assert event["event_type"] == "BATCH_REJECTED" and event["actor_id"] == "op"
    assert "123-45-6789" not in event["payload_json"]
    assert verify_chain(conn).valid
