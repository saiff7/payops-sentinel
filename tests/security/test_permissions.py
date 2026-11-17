"""Separation of duties: the agent drafts, only reviewers approve, nobody sends."""

from __future__ import annotations

import pytest

from payops_sentinel.agent.guardrails import ALLOWED_TOOLS
from payops_sentinel.review import service
from payops_sentinel.review.service import (
    ROLE_PERMISSIONS,
    AuthorizationError,
    DraftEdit,
    Permission,
    Role,
    resolve_principal,
)
from payops_sentinel.storage import repositories as repo

EDIT = DraftEdit(
    email_subject="Changed subject line",
    email_body="Changed body text that is long enough to pass validation rules.",
)


def _attempts(conn, settings, principal, ref):
    return [
        lambda: service.approve_draft(conn, principal, artifacts_dir=settings.artifacts_dir, **ref),
        lambda: service.reject_draft(conn, principal, reason="not allowed to do this", **ref),
        lambda: service.edit_draft(conn, principal, edit=EDIT, **ref),
    ]


@pytest.mark.parametrize("principal_id", ["agent_service", "operator.demo", "viewer.demo"])
def test_non_reviewers_cannot_approve_reject_or_edit(conn, settings, pending_draft, principal_id):
    principal = resolve_principal(principal_id)
    for attempt in _attempts(conn, settings, principal, pending_draft):
        with pytest.raises(AuthorizationError):
            attempt()
    draft = repo.get_draft(conn, pending_draft["draft_id"], 1)
    assert draft["status"] == "pending_review"
    assert repo.list_approvals(conn, pending_draft["draft_id"]) == []
    denied = [e for e in repo.list_audit_events(conn) if e["event_type"] == "AUTHORIZATION_DENIED"]
    assert len(denied) == 3 and {e["actor_id"] for e in denied} == {principal_id}


def test_unknown_principal_is_refused():
    with pytest.raises(AuthorizationError):
        resolve_principal("admin")


def test_agent_cannot_download_artifacts(conn, settings, pending_draft):
    service.approve_draft(
        conn,
        resolve_principal("reviewer.demo"),
        artifacts_dir=settings.artifacts_dir,
        **pending_draft,
    )
    with pytest.raises(AuthorizationError):
        service.approved_artifact(
            conn, resolve_principal("agent_service"), pending_draft["draft_id"]
        )


def test_role_matrix_matches_blueprint():
    agent = ROLE_PERMISSIONS[Role.AGENT]
    assert agent == {Permission.DRAFT_CREATE}
    approvers = {r for r, perms in ROLE_PERMISSIONS.items() if Permission.DRAFT_APPROVE in perms}
    assert approvers == {Role.REVIEWER}
    assert not any("send" in p.value for perms in ROLE_PERMISSIONS.values() for p in perms)


def test_agent_tooling_has_no_approval_or_send_capability():
    forbidden = ("approve", "reject", "send", "edit", "delete", "update", "change")
    assert not any(word in name for name in ALLOWED_TOOLS for word in forbidden)
