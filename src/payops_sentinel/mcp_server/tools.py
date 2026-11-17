"""The six read-only investigation tools.

Each function takes a read-only SQLite connection and returns a typed Pydantic result. Expected
failures raise ``ToolError`` subclasses (their message reaches the model); anything else is
masked by the SDK as ``Error executing tool <name>``, so internals never leak.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, ConfigDict

from payops_sentinel.domain.enums import ExceptionStatus, ExceptionType
from payops_sentinel.operations.registry import AgentAsset
from payops_sentinel.storage import repositories as repo

TOOLS_VERSION = "1.0.0"
AMOUNT_NOTE = (
    "Amounts are outputs of the deterministic reconciliation engine. Quote them as given; "
    "do not recalculate them."
)


class NotFoundError(ToolError):
    """A well-formed identifier that does not exist."""


class _Out(BaseModel):
    model_config = ConfigDict(frozen=True)


class BatchSummary(_Out):
    batch_id: str
    pay_period_start: str | None
    pay_period_end: str | None
    total_rows: int
    valid_rows: int
    invalid_rows: int
    exceptions: int
    batch_status: str
    ruleset_version: str
    data_classification: str


class ExceptionListItem(_Out):
    exception_id: str
    employee_ref: str | None
    exception_type: str
    severity: str
    status: str


class ExceptionList(_Out):
    items: list[ExceptionListItem]
    count: int


class ExceptionDetails(_Out):
    exception_id: str
    batch_id: str
    employee_ref: str | None
    pay_period_start: str | None
    pay_period_end: str | None
    source_row_number: int
    exception_type: str
    severity: str
    status: str
    reason_code: str
    reconciliation_reason: str
    recorded_contribution_cents: int | None
    expected_contribution_cents: int | None
    rule_id: str | None
    plan_id: str | None
    rule_code: str | None
    ruleset_version: str
    evidence_ids: list[str]
    amount_note: str = AMOUNT_NOTE


class EvidenceRecord(_Out):
    evidence_id: str
    evidence_type: str
    source_reference: str
    snapshot: dict[str, Any]
    content_hash: str


class ExceptionEvidence(_Out):
    exception_id: str
    evidence: list[EvidenceRecord]


class PlanRule(_Out):
    rule_id: str
    version: str
    ruleset_version: str
    plan_id: str
    rule_code: str
    effective_from: str
    effective_to: str | None
    configured_rule: dict[str, Any]
    description: str
    source_reference: str
    evidence_reference: str


class AgentAssetRecord(_Out):
    asset_id: str
    asset_name: str
    agent_version: str
    model: str
    prompt_version: str
    tools_version: str
    environment: str
    status: str
    release_status: str
    owner: str
    backup_owner: str
    evaluation_status: str
    data_classification: str


def get_batch_summary(conn: sqlite3.Connection, batch_id: str) -> BatchSummary:
    row = repo.batch_summary(conn, batch_id)
    if row is None:
        raise NotFoundError(f"not_found: batch {batch_id}")
    return BatchSummary(
        batch_id=row["batch_id"],
        pay_period_start=row["pay_period_start"],
        pay_period_end=row["pay_period_end"],
        total_rows=row["total_rows"],
        valid_rows=row["valid_rows"],
        invalid_rows=row["invalid_rows"],
        exceptions=row["exception_count"],
        batch_status=row["status"],
        ruleset_version=row["ruleset_version"],
        data_classification=row["data_classification"],
    )


def list_exceptions(
    conn: sqlite3.Connection,
    batch_id: str,
    exception_type: ExceptionType | None,
    status: ExceptionStatus | None,
    limit: int,
) -> ExceptionList:
    if repo.batch_summary(conn, batch_id) is None:
        raise NotFoundError(f"not_found: batch {batch_id}")
    rows = repo.query_exceptions(
        conn,
        batch_id,
        exception_type=exception_type.value if exception_type else None,
        status=status.value if status else None,
        limit=limit,
    )
    items = [ExceptionListItem(**r) for r in rows]
    return ExceptionList(items=items, count=len(items))


def get_exception_details(conn: sqlite3.Connection, exception_id: str) -> ExceptionDetails:
    d = repo.exception_details(conn, exception_id)
    if d is None:
        raise NotFoundError(f"not_found: exception {exception_id}")
    return ExceptionDetails(
        exception_id=d["exception_id"],
        batch_id=d["batch_id"],
        employee_ref=d["employee_ref"],
        pay_period_start=d["pay_period_start"],
        pay_period_end=d["pay_period_end"],
        source_row_number=d["source_row_number"],
        exception_type=d["exception_type"],
        severity=d["severity"],
        status=d["status"],
        reason_code=d["reason_code"],
        reconciliation_reason=d["reason"],
        recorded_contribution_cents=d["actual_cents"],
        expected_contribution_cents=d["expected_cents"],
        rule_id=d["rule_id"],
        plan_id=d["plan_id"],
        rule_code=d["rule_code"],
        ruleset_version=d["ruleset_version"],
        evidence_ids=d["evidence_ids"],
    )


def get_exception_evidence(conn: sqlite3.Connection, exception_id: str) -> ExceptionEvidence:
    if repo.exception_details(conn, exception_id) is None:
        raise NotFoundError(f"not_found: exception {exception_id}")
    evidence = [
        EvidenceRecord(
            evidence_id=e["evidence_id"],
            evidence_type=e["evidence_type"],
            source_reference=e["source_reference"],
            snapshot=json.loads(e["snapshot_json"]),
            content_hash=e["content_hash"],
        )
        for e in repo.list_evidence(conn, exception_id)
    ]
    return ExceptionEvidence(exception_id=exception_id, evidence=evidence)


def lookup_plan_rule(
    conn: sqlite3.Connection,
    plan_id: str,
    rule_code: str,
    as_of_date: date,
    ruleset_version: str | None,
) -> PlanRule:
    rule = repo.find_rule(
        conn,
        plan_id=plan_id,
        rule_code=rule_code,
        as_of=as_of_date,
        ruleset_version=ruleset_version,
    )
    if rule is None:
        raise NotFoundError(f"not_found: no {rule_code} rule for plan {plan_id} on {as_of_date}")
    return PlanRule(
        rule_id=rule.rule_id,
        version=rule.version,
        ruleset_version=rule.ruleset_version,
        plan_id=rule.plan_id,
        rule_code=rule.rule_code,
        effective_from=rule.effective_from.isoformat(),
        effective_to=rule.effective_to.isoformat() if rule.effective_to else None,
        configured_rule={"rounding": rule.rounding, "tolerance_cents": rule.tolerance_cents},
        description=rule.description,
        source_reference=rule.source_reference,
        evidence_reference=f"plan_rules:{rule.rule_id}",
    )


def get_agent_asset(asset: AgentAsset, asset_id: str) -> AgentAssetRecord:
    if asset_id != asset.asset_id:
        raise NotFoundError(f"not_found: agent asset {asset_id}")
    return AgentAssetRecord(
        asset_id=asset.asset_id,
        asset_name=asset.asset_name,
        agent_version=asset.agent_version,
        model=asset.model,
        prompt_version=asset.prompt_version,
        tools_version=asset.tools_version,
        environment=asset.environment,
        status=asset.status,
        release_status=asset.release_status,
        owner=asset.technical_owner,
        backup_owner=asset.backup_owner,
        evaluation_status=asset.evaluation_status,
        data_classification=asset.data_classification,
    )
