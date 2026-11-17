"""Reviewer authorisation and the draft lifecycle. The only path to approval or rejection.

Identity in this local demo is simulated (a fixed principal register); authorisation is enforced
here, server-side, regardless of what the UI shows. Production would authenticate via OIDC and map
identity to roles in a central RBAC system: authentication alone does not grant approval rights.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from payops_sentinel.agent.schemas import AgentInvestigationDraft
from payops_sentinel.domain.enums import ExceptionStatus
from payops_sentinel.hashing import sha256_hex
from payops_sentinel.redaction import find_restricted
from payops_sentinel.review import artifacts
from payops_sentinel.review.audit import AuditEventType, append_event
from payops_sentinel.storage import repositories as repo
from payops_sentinel.storage.db import transaction


class Role(StrEnum):
    AGENT = "agent"
    OPERATOR = "operator"
    REVIEWER = "reviewer"
    VIEWER = "viewer"


class Permission(StrEnum):
    BATCH_UPLOAD = "batch.upload"
    AGENT_INVESTIGATE = "agent.investigate"
    DRAFT_CREATE = "draft.create"
    DRAFT_EDIT = "draft.edit"
    DRAFT_APPROVE = "draft.approve"
    DRAFT_REJECT = "draft.reject"
    ARTIFACT_DOWNLOAD = "artifact.download"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.AGENT: frozenset({Permission.DRAFT_CREATE}),
    Role.OPERATOR: frozenset({Permission.BATCH_UPLOAD, Permission.AGENT_INVESTIGATE}),
    Role.REVIEWER: frozenset(
        {
            Permission.AGENT_INVESTIGATE,
            Permission.DRAFT_EDIT,
            Permission.DRAFT_APPROVE,
            Permission.DRAFT_REJECT,
            Permission.ARTIFACT_DOWNLOAD,
        }
    ),
    Role.VIEWER: frozenset(),
}


@dataclass(frozen=True)
class Principal:
    principal_id: str
    display_name: str
    role: Role


# Simulated identities for the local demo only.
DEMO_PRINCIPALS: dict[str, Principal] = {
    p.principal_id: p
    for p in (
        Principal("operator.demo", "Demo operator", Role.OPERATOR),
        Principal("reviewer.demo", "Demo reviewer", Role.REVIEWER),
        Principal("viewer.demo", "Demo viewer (read-only)", Role.VIEWER),
        Principal("agent_service", "AI agent service identity", Role.AGENT),
    )
}


class ReviewError(Exception):
    """Base class for refused review actions."""


class AuthorizationError(ReviewError):
    pass


class StaleDraftError(ReviewError):
    """The draft version or content hash shown to the reviewer is no longer current."""


class InvalidDraftError(ReviewError):
    pass


class DraftEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    email_subject: str = Field(min_length=5, max_length=150)
    email_body: str = Field(min_length=40, max_length=3000)


class ReviewResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    draft_id: str
    draft_version: int
    content_hash: str
    status: str
    approval_id: str | None = None
    artifact_path: str | None = None
    artifact_sha256: str | None = None


def resolve_principal(principal_id: str) -> Principal:
    principal = DEMO_PRINCIPALS.get(principal_id)
    if principal is None:
        raise AuthorizationError("unknown principal")
    return principal


def has_permission(principal: Principal, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS[principal.role]


def authorize(
    conn: sqlite3.Connection, principal: Principal, permission: Permission, object_id: str
) -> None:
    """Raise and record a denial unless the principal holds the permission."""
    if has_permission(principal, permission):
        return
    with transaction(conn):
        append_event(
            conn,
            actor_id=principal.principal_id,
            event_type=AuditEventType.AUTHORIZATION_DENIED,
            object_id=object_id,
            payload={"permission": permission.value, "role": principal.role.value},
        )
    raise AuthorizationError(f"{principal.principal_id} lacks {permission.value}")


def _current(
    conn: sqlite3.Connection, draft_id: str, version: int, content_hash: str
) -> dict[str, Any]:
    """The exact version the reviewer saw must still be the latest, pending and unaltered."""
    latest = repo.latest_draft_version(conn, draft_id)
    if latest is None:
        raise InvalidDraftError(f"draft {draft_id} does not exist")
    if latest["draft_version"] != version:
        raise StaleDraftError(f"version {version} is superseded by v{latest['draft_version']}")
    if latest["status"] != "pending_review":
        raise StaleDraftError(f"draft is already {latest['status']}")
    if content_hash != latest["content_hash"] or sha256_hex(latest["draft_json"]) != content_hash:
        raise StaleDraftError("content hash does not match the stored draft")
    return latest


def _reject_restricted(*texts: str) -> None:
    found = sorted({name for t in texts for name in find_restricted(t)})
    if found:
        raise InvalidDraftError(f"draft contains restricted data: {', '.join(found)}")


def edit_draft(
    conn: sqlite3.Connection,
    principal: Principal,
    *,
    draft_id: str,
    version: int,
    content_hash: str,
    edit: DraftEdit,
) -> ReviewResult:
    authorize(conn, principal, Permission.DRAFT_EDIT, draft_id)
    _reject_restricted(edit.email_subject, edit.email_body)
    with transaction(conn):
        base = _current(conn, draft_id, version, content_hash)
        content = repo.json_loads(base["draft_json"]) | edit.model_dump()
        try:
            AgentInvestigationDraft.model_validate(content)
        except ValidationError as exc:
            raise InvalidDraftError(str(exc.errors()[0]["msg"])) from exc
        new_version = version + 1
        new_hash = repo.insert_draft_version(
            conn,
            draft_id=draft_id,
            version=new_version,
            exception_id=base["exception_id"],
            run_id=base["run_id"],
            author_id=principal.principal_id,
            draft=content,
        )
        repo.set_draft_status(conn, draft_id, version, "superseded")
        append_event(
            conn,
            actor_id=principal.principal_id,
            event_type=AuditEventType.DRAFT_EDITED,
            object_id=draft_id,
            payload={
                "from_version": version,
                "from_hash": content_hash,
                "to_version": new_version,
                "to_hash": new_hash,
            },
        )
    return ReviewResult(
        draft_id=draft_id, draft_version=new_version, content_hash=new_hash, status="pending_review"
    )


def approve_draft(
    conn: sqlite3.Connection,
    principal: Principal,
    *,
    draft_id: str,
    version: int,
    content_hash: str,
    artifacts_dir: Path,
    comment: str | None = None,
) -> ReviewResult:
    """Approve the communication for export. This does not approve or resolve the exception."""
    authorize(conn, principal, Permission.DRAFT_APPROVE, draft_id)
    with transaction(conn):
        draft = _current(conn, draft_id, version, content_hash)
        _reject_restricted(draft["draft_json"])
        approval_id, approved_at = repo.insert_approval(
            conn,
            draft_id=draft_id,
            version=version,
            reviewer_id=principal.principal_id,
            action="approve",
            comment=comment,
            content_hash=content_hash,
        )
        repo.set_draft_status(conn, draft_id, version, "approved")
        artifact = artifacts.render(
            draft,
            approval_id=approval_id,
            reviewer_id=principal.principal_id,
            approved_at=approved_at,
        )
        append_event(
            conn,
            actor_id=principal.principal_id,
            event_type=AuditEventType.DRAFT_APPROVED,
            object_id=draft_id,
            payload={
                "version": version,
                "content_hash": content_hash,
                "approval_id": approval_id,
                "exception_id": draft["exception_id"],
                "artifact_sha256": artifact.sha256,
                "comment": comment,
            },
        )
    path = artifacts.write(artifact, artifacts_dir)
    return ReviewResult(
        draft_id=draft_id,
        draft_version=version,
        content_hash=content_hash,
        status="approved",
        approval_id=approval_id,
        artifact_path=str(path),
        artifact_sha256=artifact.sha256,
    )


def reject_draft(
    conn: sqlite3.Connection,
    principal: Principal,
    *,
    draft_id: str,
    version: int,
    content_hash: str,
    reason: str,
) -> ReviewResult:
    authorize(conn, principal, Permission.DRAFT_REJECT, draft_id)
    if len(reason.strip()) < 5:
        raise InvalidDraftError("a rejection reason of at least 5 characters is required")
    with transaction(conn):
        draft = _current(conn, draft_id, version, content_hash)
        approval_id, _ = repo.insert_approval(
            conn,
            draft_id=draft_id,
            version=version,
            reviewer_id=principal.principal_id,
            action="reject",
            comment=reason.strip()[:1000],
            content_hash=content_hash,
        )
        repo.set_draft_status(conn, draft_id, version, "rejected")
        repo.set_exception_status(conn, draft["exception_id"], ExceptionStatus.MANUAL_REVIEW)
        append_event(
            conn,
            actor_id=principal.principal_id,
            event_type=AuditEventType.DRAFT_REJECTED,
            object_id=draft_id,
            payload={
                "version": version,
                "content_hash": content_hash,
                "approval_id": approval_id,
                "reason": reason.strip()[:1000],
            },
        )
    return ReviewResult(
        draft_id=draft_id,
        draft_version=version,
        content_hash=content_hash,
        status="rejected",
        approval_id=approval_id,
    )


def approved_artifact(
    conn: sqlite3.Connection, principal: Principal, draft_id: str
) -> artifacts.Artifact:
    """Rebuild the approved artifact from the database and verify it against the audit record."""
    authorize(conn, principal, Permission.ARTIFACT_DOWNLOAD, draft_id)
    approvals = [a for a in repo.list_approvals(conn, draft_id) if a["action"] == "approve"]
    if not approvals:
        raise InvalidDraftError("draft has not been approved")
    a = approvals[-1]
    draft = repo.get_draft(conn, draft_id, a["draft_version"])
    assert draft is not None
    artifact = artifacts.render(
        draft,
        approval_id=a["approval_id"],
        reviewer_id=a["reviewer_id"],
        approved_at=a["created_at"],
    )
    recorded = [
        e
        for e in repo.list_audit_events(conn, object_ids=[draft_id])
        if e["event_type"] == AuditEventType.DRAFT_APPROVED
    ]
    if not recorded or repo.json_loads(recorded[-1]["payload_json"])["artifact_sha256"] != (
        artifact.sha256
    ):
        raise InvalidDraftError("artifact does not match the audited approval")
    return artifact


def record_download(
    conn: sqlite3.Connection, principal: Principal, draft_id: str, artifact_sha256: str
) -> None:
    with transaction(conn):
        append_event(
            conn,
            actor_id=principal.principal_id,
            event_type=AuditEventType.ARTIFACT_DOWNLOADED,
            object_id=draft_id,
            payload={"artifact_sha256": artifact_sha256},
        )
