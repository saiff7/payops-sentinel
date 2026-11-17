"""Tamper-evident audit chain: H_n = SHA256(H_{n-1} || canonical_event_n).

Tamper-evident, not immutable: UPDATE/DELETE are blocked by triggers and any rewrite of history
breaks the chain, but someone with file-level access to a local SQLite database could rebuild it.
Production would export events to independently controlled append-only storage.
"""

from __future__ import annotations

import sqlite3
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict

from payops_sentinel.hashing import canonical_json, sha256_hex
from payops_sentinel.storage import repositories as repo


class AuditEventType(StrEnum):
    BATCH_INGESTED = "BATCH_INGESTED"
    BATCH_REJECTED = "BATCH_REJECTED"
    INVESTIGATION_COMPLETED = "INVESTIGATION_COMPLETED"
    DRAFT_EDITED = "DRAFT_EDITED"
    DRAFT_APPROVED = "DRAFT_APPROVED"
    DRAFT_REJECTED = "DRAFT_REJECTED"
    ARTIFACT_DOWNLOADED = "ARTIFACT_DOWNLOADED"
    AUTHORIZATION_DENIED = "AUTHORIZATION_DENIED"


class ChainVerification(BaseModel):
    model_config = ConfigDict(frozen=True)

    valid: bool
    events_checked: int
    first_invalid_sequence: int | None = None
    reason: str | None = None


def _event_hash(previous_hash: str, event: dict[str, Any]) -> str:
    return sha256_hex(previous_hash + canonical_json(event))


def append_event(
    conn: sqlite3.Connection,
    *,
    actor_id: str,
    event_type: AuditEventType,
    object_id: str,
    payload: dict[str, Any],
) -> str:
    """Append one event. Call inside a write transaction so the chain head cannot race."""
    if not conn.in_transaction:
        raise RuntimeError("audit events must be appended inside a transaction")
    event_id = repo.next_id(conn, "AUD")
    created_at = repo.utc_now()
    payload_json = canonical_json(payload)
    event = {
        "event_id": event_id,
        "actor_id": actor_id,
        "event_type": event_type.value,
        "object_id": object_id,
        "payload_json": payload_json,
        "created_at": created_at,
    }
    previous = repo.last_audit_hash(conn)
    event_hash = _event_hash(previous, event)
    repo.insert_audit_event(conn, previous_hash=previous, event_hash=event_hash, **event)
    return event_id


def verify_chain(conn: sqlite3.Connection) -> ChainVerification:
    previous = repo.GENESIS_HASH
    expected_sequence = None
    events = repo.list_audit_events(conn, limit=10_000_000)
    for e in events:
        if expected_sequence is not None and e["sequence"] != expected_sequence:
            return ChainVerification(
                valid=False,
                events_checked=len(events),
                first_invalid_sequence=expected_sequence,
                reason="sequence gap: an event was removed",
            )
        if e["previous_hash"] != previous:
            return ChainVerification(
                valid=False,
                events_checked=len(events),
                first_invalid_sequence=e["sequence"],
                reason="previous_hash does not match the prior event",
            )
        event = {
            k: e[k]
            for k in (
                "event_id",
                "actor_id",
                "event_type",
                "object_id",
                "payload_json",
                "created_at",
            )
        }
        if _event_hash(previous, event) != e["event_hash"]:
            return ChainVerification(
                valid=False,
                events_checked=len(events),
                first_invalid_sequence=e["sequence"],
                reason="event content does not match its hash",
            )
        previous = e["event_hash"]
        expected_sequence = e["sequence"] + 1
    return ChainVerification(valid=True, events_checked=len(events))
