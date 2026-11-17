"""All SQL lives here. Every statement is parameterised."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import UTC, date, datetime
from typing import Any

from payops_sentinel.domain.enums import (
    BatchStatus,
    DataClassification,
    ExceptionStatus,
    ValidationStatus,
)
from payops_sentinel.domain.models import (
    ContributionRule,
    Employer,
    Enrollment,
    ExceptionFinding,
    ParsedRow,
    Participant,
    ReferenceData,
)
from payops_sentinel.hashing import canonical_json, sha256_hex


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def next_id(conn: sqlite3.Connection, prefix: str, width: int = 6) -> str:
    """Stable, human-readable sequential identifiers such as ``EXC-000017``."""
    (value,) = conn.execute(
        "INSERT INTO id_sequences (name, next_value) VALUES (?, 1) "
        "ON CONFLICT(name) DO UPDATE SET next_value = next_value + 1 RETURNING next_value",
        (prefix,),
    ).fetchone()
    return f"{prefix}-{value:0{width}d}"


# ------------------------------------------------------------------ reference data


def insert_employer(conn: sqlite3.Connection, employer: Employer) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO employers (employer_id, employer_code, name, data_classification) "
        "VALUES (?, ?, ?, ?)",
        (
            employer.employer_id,
            employer.employer_code,
            employer.name,
            DataClassification.SYNTHETIC_DEMO.value,
        ),
    )


def insert_participant(conn: sqlite3.Connection, employer_id: str, p: Participant) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO participants "
        "(participant_id, employer_id, employee_ref, termination_date) VALUES (?, ?, ?, ?)",
        (p.participant_id, employer_id, p.employee_ref, _iso(p.termination_date)),
    )


def insert_enrollment(conn: sqlite3.Connection, employer_id: str, e: Enrollment) -> None:
    row = conn.execute(
        "SELECT participant_id FROM participants WHERE employer_id = ? AND employee_ref = ?",
        (employer_id, e.employee_ref),
    ).fetchone()
    if row is None:
        raise ValueError(f"enrollment {e.enrollment_id} references unknown participant")
    conn.execute(
        "INSERT OR IGNORE INTO plan_enrollments (enrollment_id, participant_id, plan_id, "
        "elected_rate_bp, effective_from, effective_to, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            e.enrollment_id,
            row["participant_id"],
            e.plan_id,
            e.elected_rate_bp,
            _iso(e.effective_from),
            _iso(e.effective_to),
            e.status.value,
        ),
    )


def insert_rule(conn: sqlite3.Connection, r: ContributionRule) -> None:
    rule_json = canonical_json({"rounding": r.rounding, "tolerance_cents": r.tolerance_cents})
    conn.execute(
        "INSERT OR IGNORE INTO plan_rules (rule_id, ruleset_version, plan_id, rule_code, version, "
        "effective_from, effective_to, rule_json, description, source_reference) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            r.rule_id,
            r.ruleset_version,
            r.plan_id,
            r.rule_code,
            r.version,
            _iso(r.effective_from),
            _iso(r.effective_to),
            rule_json,
            r.description,
            r.source_reference,
        ),
    )


def get_employer_by_code(conn: sqlite3.Connection, employer_code: str) -> Employer | None:
    row = conn.execute(
        "SELECT employer_id, employer_code, name FROM employers WHERE employer_code = ?",
        (employer_code,),
    ).fetchone()
    return Employer(**dict(row)) if row else None


def ruleset_exists(conn: sqlite3.Connection, ruleset_version: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM plan_rules WHERE ruleset_version = ? LIMIT 1", (ruleset_version,)
    ).fetchone()
    return row is not None


def _rule_from_row(row: sqlite3.Row) -> ContributionRule:
    params = json.loads(row["rule_json"])
    return ContributionRule(
        rule_id=row["rule_id"],
        ruleset_version=row["ruleset_version"],
        plan_id=row["plan_id"],
        rule_code=row["rule_code"],
        version=row["version"],
        effective_from=date.fromisoformat(row["effective_from"]),
        effective_to=_date(row["effective_to"]),
        rounding=params["rounding"],
        tolerance_cents=params["tolerance_cents"],
        description=row["description"],
        source_reference=row["source_reference"],
    )


def load_reference_data(
    conn: sqlite3.Connection, employer_id: str, ruleset_version: str
) -> ReferenceData:
    participants = {
        r["employee_ref"]: Participant(
            participant_id=r["participant_id"],
            employee_ref=r["employee_ref"],
            termination_date=_date(r["termination_date"]),
        )
        for r in conn.execute(
            "SELECT participant_id, employee_ref, termination_date FROM participants "
            "WHERE employer_id = ?",
            (employer_id,),
        )
    }
    enrollments: dict[str, list[Enrollment]] = defaultdict(list)
    for r in conn.execute(
        "SELECT e.enrollment_id, p.employee_ref, e.plan_id, e.elected_rate_bp, "
        "e.effective_from, e.effective_to, e.status FROM plan_enrollments e "
        "JOIN participants p ON p.participant_id = e.participant_id "
        "WHERE p.employer_id = ? ORDER BY e.effective_from, e.enrollment_id",
        (employer_id,),
    ):
        enrollments[r["employee_ref"]].append(
            Enrollment(
                enrollment_id=r["enrollment_id"],
                employee_ref=r["employee_ref"],
                plan_id=r["plan_id"],
                elected_rate_bp=r["elected_rate_bp"],
                effective_from=date.fromisoformat(r["effective_from"]),
                effective_to=_date(r["effective_to"]),
                status=r["status"],
            )
        )
    rules = tuple(
        _rule_from_row(r)
        for r in conn.execute(
            "SELECT * FROM plan_rules WHERE ruleset_version = ? ORDER BY effective_from",
            (ruleset_version,),
        )
    )
    return ReferenceData(
        participants=participants,
        enrollments={k: tuple(v) for k, v in enrollments.items()},
        rules=rules,
    )


# ------------------------------------------------------------------ batches


def find_batch_id(
    conn: sqlite3.Connection, employer_id: str, file_sha256: str, ruleset_version: str
) -> str | None:
    row = conn.execute(
        "SELECT batch_id FROM payroll_batches "
        "WHERE employer_id = ? AND file_sha256 = ? AND ruleset_version = ?",
        (employer_id, file_sha256, ruleset_version),
    ).fetchone()
    return row["batch_id"] if row else None


def get_batch(conn: sqlite3.Connection, batch_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM payroll_batches WHERE batch_id = ?", (batch_id,)).fetchone()
    return dict(row) if row else None


def list_batches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute("SELECT * FROM payroll_batches ORDER BY created_at DESC, batch_id")
    ]


def insert_batch(
    conn: sqlite3.Connection,
    *,
    employer_id: str,
    file_sha256: str,
    file_name: str,
    ruleset_version: str,
) -> str:
    batch_id = next_id(conn, "BATCH")
    conn.execute(
        "INSERT INTO payroll_batches (batch_id, employer_id, file_sha256, file_name, "
        "ruleset_version, status, data_classification, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            batch_id,
            employer_id,
            file_sha256,
            file_name,
            ruleset_version,
            BatchStatus.PROCESSING.value,
            DataClassification.SYNTHETIC_DEMO.value,
            utc_now(),
        ),
    )
    return batch_id


def finalize_batch(
    conn: sqlite3.Connection,
    batch_id: str,
    *,
    pay_period: tuple[date, date] | None,
    total_rows: int,
    valid_rows: int,
    invalid_rows: int,
    exception_count: int,
    duration_ms: int,
) -> None:
    start, end = pay_period if pay_period else (None, None)
    conn.execute(
        "UPDATE payroll_batches SET status = ?, pay_period_start = ?, pay_period_end = ?, "
        "total_rows = ?, valid_rows = ?, invalid_rows = ?, exception_count = ?, duration_ms = ? "
        "WHERE batch_id = ?",
        (
            BatchStatus.PROCESSED.value,
            _iso(start),
            _iso(end),
            total_rows,
            valid_rows,
            invalid_rows,
            exception_count,
            duration_ms,
            batch_id,
        ),
    )


def insert_payroll_row(conn: sqlite3.Connection, batch_id: str, parsed: ParsedRow) -> int:
    s = parsed.sanitized
    status = ValidationStatus.VALID if parsed.is_valid else ValidationStatus.INVALID
    cur = conn.execute(
        "INSERT INTO payroll_rows (batch_id, source_row_number, employee_ref, pay_period_start, "
        "pay_period_end, eligible_comp_cents, contribution_cents, validation_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            batch_id,
            parsed.source_row_number,
            s.get("employee_ref"),
            _iso(s.get("pay_period_start")),
            _iso(s.get("pay_period_end")),
            s.get("eligible_comp_cents"),
            s.get("contribution_cents"),
            status.value,
        ),
    )
    row_id = cur.lastrowid
    assert row_id is not None
    for err in parsed.errors:
        conn.execute(
            "INSERT INTO validation_errors (batch_id, row_id, source_row_number, field, "
            "reason_code, message) VALUES (?, ?, ?, ?, ?, ?)",
            (
                batch_id,
                row_id,
                err.source_row_number,
                err.field,
                err.reason_code.value,
                err.message,
            ),
        )
    return row_id


def insert_exception(
    conn: sqlite3.Connection, batch_id: str, row_id: int, finding: ExceptionFinding
) -> str:
    exception_id = next_id(conn, "EXC")
    now = utc_now()
    conn.execute(
        "INSERT INTO exceptions (exception_id, batch_id, row_id, exception_type, severity, "
        "reason_code, reason, employee_ref, pay_period_end, expected_cents, actual_cents, "
        "rule_id, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            exception_id,
            batch_id,
            row_id,
            finding.exception_type.value,
            finding.severity.value,
            finding.reason_code.value,
            finding.reason,
            finding.employee_ref,
            _iso(finding.pay_period_end),
            finding.expected_cents,
            finding.actual_cents,
            finding.rule_id,
            ExceptionStatus.OPEN.value,
            now,
        ),
    )
    for item in finding.evidence:
        snapshot = canonical_json(item.snapshot)
        conn.execute(
            "INSERT INTO evidence (evidence_id, exception_id, evidence_type, source_reference, "
            "snapshot_json, content_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                next_id(conn, "EV"),
                exception_id,
                item.evidence_type.value,
                item.source_reference,
                snapshot,
                sha256_hex(snapshot),
                now,
            ),
        )
    return exception_id


# ------------------------------------------------------------------ queries


def list_exceptions(conn: sqlite3.Connection, batch_id: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT e.*, r.source_row_number FROM exceptions e "
            "JOIN payroll_rows r ON r.row_id = e.row_id "
            "WHERE e.batch_id = ? ORDER BY r.source_row_number, e.exception_type",
            (batch_id,),
        )
    ]


def list_evidence(conn: sqlite3.Connection, exception_id: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM evidence WHERE exception_id = ? ORDER BY evidence_id", (exception_id,)
        )
    ]


def list_validation_errors(conn: sqlite3.Connection, batch_id: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM validation_errors WHERE batch_id = ? "
            "ORDER BY source_row_number, error_id",
            (batch_id,),
        )
    ]


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    allowed = {"payroll_batches", "payroll_rows", "validation_errors", "exceptions", "evidence"}
    if table not in allowed:
        raise ValueError(f"table not countable: {table}")
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608 (allowlisted)


# ------------------------------------------------------------------ read models for MCP tools


def batch_summary(conn: sqlite3.Connection, batch_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT batch_id, pay_period_start, pay_period_end, total_rows, valid_rows, invalid_rows, "
        "exception_count, status, ruleset_version, data_classification "
        "FROM payroll_batches WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    return dict(row) if row else None


def query_exceptions(
    conn: sqlite3.Connection,
    batch_id: str,
    *,
    exception_type: str | None,
    status: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT exception_id, employee_ref, exception_type, severity, status "
            "FROM exceptions WHERE batch_id = ? "
            "AND (? IS NULL OR exception_type = ?) AND (? IS NULL OR status = ?) "
            "ORDER BY exception_id LIMIT ?",
            (batch_id, exception_type, exception_type, status, status, limit),
        )
    ]


def exception_details(conn: sqlite3.Connection, exception_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT e.exception_id, e.batch_id, e.employee_ref, e.pay_period_end, e.exception_type, "
        "e.severity, e.status, e.reason_code, e.reason, e.expected_cents, e.actual_cents, "
        "e.rule_id, r.source_row_number, b.pay_period_start, b.ruleset_version, "
        "pr.plan_id, pr.rule_code "
        "FROM exceptions e JOIN payroll_rows r ON r.row_id = e.row_id "
        "JOIN payroll_batches b ON b.batch_id = e.batch_id "
        "LEFT JOIN plan_rules pr ON pr.rule_id = e.rule_id WHERE e.exception_id = ?",
        (exception_id,),
    ).fetchone()
    if row is None:
        return None
    details = dict(row)
    details["evidence_ids"] = [
        r["evidence_id"]
        for r in conn.execute(
            "SELECT evidence_id FROM evidence WHERE exception_id = ? ORDER BY evidence_id",
            (exception_id,),
        )
    ]
    return details


def find_rule(
    conn: sqlite3.Connection,
    *,
    plan_id: str,
    rule_code: str,
    as_of: date,
    ruleset_version: str | None,
) -> ContributionRule | None:
    row = conn.execute(
        "SELECT * FROM plan_rules WHERE plan_id = ? AND rule_code = ? "
        "AND (? IS NULL OR ruleset_version = ?) AND effective_from <= ? "
        "AND (effective_to IS NULL OR effective_to >= ?) "
        "ORDER BY effective_from DESC, ruleset_version DESC LIMIT 1",
        (
            plan_id,
            rule_code,
            ruleset_version,
            ruleset_version,
            as_of.isoformat(),
            as_of.isoformat(),
        ),
    ).fetchone()
    return _rule_from_row(row) if row else None


# ------------------------------------------------------------------ agent runs


def insert_agent_run(
    conn: sqlite3.Connection,
    *,
    exception_id: str,
    batch_id: str | None,
    agent_version: str,
    model_version: str,
    prompt_version: str,
) -> str:
    run_id = next_id(conn, "RUN")
    conn.execute(
        "INSERT INTO agent_runs (run_id, batch_id, exception_id, agent_version, model_version, "
        "prompt_version, status, started_at) VALUES (?, ?, ?, ?, ?, ?, 'running', ?)",
        (run_id, batch_id, exception_id, agent_version, model_version, prompt_version, utc_now()),
    )
    return run_id


def finish_agent_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    status: str,
    error_category: str | None,
    error_detail: str | None,
    tool_call_count: int,
    duration_ms: int,
) -> None:
    conn.execute(
        "UPDATE agent_runs SET status = ?, error_category = ?, error_detail = ?, "
        "tool_call_count = ?, duration_ms = ? WHERE run_id = ?",
        (status, error_category, error_detail, tool_call_count, duration_ms, run_id),
    )


def insert_tool_event(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    sequence: int,
    actor: str,
    tool_name: str,
    arguments: dict[str, Any],
    result_hash: str | None,
    duration_ms: int,
    success: bool,
    error_category: str | None,
) -> None:
    conn.execute(
        "INSERT INTO tool_events (run_id, sequence, actor, tool_name, arguments_json, "
        "result_hash, duration_ms, success, error_category, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            sequence,
            actor,
            tool_name[:100],
            canonical_json(arguments)[:2000],
            result_hash,
            duration_ms,
            int(success),
            error_category,
            utc_now(),
        ),
    )


def insert_draft(
    conn: sqlite3.Connection,
    *,
    exception_id: str,
    run_id: str | None,
    author_id: str,
    draft: dict[str, Any],
) -> tuple[str, int, str]:
    """Create version 1 of a new draft. Returns (draft_id, version, content_hash)."""
    draft_id = next_id(conn, "DRF")
    body = canonical_json(draft)
    content_hash = sha256_hex(body)
    conn.execute(
        "INSERT INTO drafts (draft_id, draft_version, exception_id, run_id, author_id, "
        "draft_json, content_hash, status, created_at) "
        "VALUES (?, 1, ?, ?, ?, ?, ?, 'pending_review', ?)",
        (draft_id, exception_id, run_id, author_id, body, content_hash, utc_now()),
    )
    return draft_id, 1, content_hash


def set_exception_status(conn: sqlite3.Connection, exception_id: str, status: str) -> None:
    conn.execute(
        "UPDATE exceptions SET status = ? WHERE exception_id = ?",
        (ExceptionStatus(status).value, exception_id),
    )


def get_agent_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM agent_runs WHERE run_id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def list_tool_events(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM tool_events WHERE run_id = ? ORDER BY sequence", (run_id,)
        )
    ]


def list_drafts(conn: sqlite3.Connection, exception_id: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM drafts WHERE exception_id = ? ORDER BY created_at, draft_version",
            (exception_id,),
        )
    ]


# ------------------------------------------------------------------ audit chain

GENESIS_HASH = "0" * 64


def last_audit_hash(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT event_hash FROM audit_events ORDER BY sequence DESC LIMIT 1"
    ).fetchone()
    return row["event_hash"] if row else GENESIS_HASH


def insert_audit_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    actor_id: str,
    event_type: str,
    object_id: str,
    payload_json: str,
    previous_hash: str,
    event_hash: str,
    created_at: str,
) -> None:
    conn.execute(
        "INSERT INTO audit_events (event_id, actor_id, event_type, object_id, payload_json, "
        "previous_hash, event_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event_id,
            actor_id,
            event_type,
            object_id,
            payload_json,
            previous_hash,
            event_hash,
            created_at,
        ),
    )


def list_audit_events(
    conn: sqlite3.Connection, *, object_ids: list[str] | None = None, limit: int = 1000
) -> list[dict[str, Any]]:
    if object_ids is None:
        rows = conn.execute("SELECT * FROM audit_events ORDER BY sequence LIMIT ?", (limit,))
    else:
        marks = ",".join("?" * len(object_ids)) or "NULL"
        rows = conn.execute(
            f"SELECT * FROM audit_events WHERE object_id IN ({marks}) "  # noqa: S608 (placeholders)
            "ORDER BY sequence LIMIT ?",
            (*object_ids, limit),
        )
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ draft versions and approvals


def get_draft(conn: sqlite3.Connection, draft_id: str, version: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM drafts WHERE draft_id = ? AND draft_version = ?", (draft_id, version)
    ).fetchone()
    return dict(row) if row else None


def latest_draft_version(conn: sqlite3.Connection, draft_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM drafts WHERE draft_id = ? ORDER BY draft_version DESC LIMIT 1", (draft_id,)
    ).fetchone()
    return dict(row) if row else None


def insert_draft_version(
    conn: sqlite3.Connection,
    *,
    draft_id: str,
    version: int,
    exception_id: str,
    run_id: str | None,
    author_id: str,
    draft: dict[str, Any],
) -> str:
    body = canonical_json(draft)
    content_hash = sha256_hex(body)
    conn.execute(
        "INSERT INTO drafts (draft_id, draft_version, exception_id, run_id, author_id, "
        "draft_json, content_hash, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_review', ?)",
        (draft_id, version, exception_id, run_id, author_id, body, content_hash, utc_now()),
    )
    return content_hash


def set_draft_status(conn: sqlite3.Connection, draft_id: str, version: int, status: str) -> None:
    conn.execute(
        "UPDATE drafts SET status = ? WHERE draft_id = ? AND draft_version = ?",
        (status, draft_id, version),
    )


def insert_approval(
    conn: sqlite3.Connection,
    *,
    draft_id: str,
    version: int,
    reviewer_id: str,
    action: str,
    comment: str | None,
    content_hash: str,
) -> tuple[str, str]:
    approval_id = next_id(conn, "APR")
    created_at = utc_now()
    conn.execute(
        "INSERT INTO approvals (approval_id, draft_id, draft_version, reviewer_id, action, "
        "comment, content_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (approval_id, draft_id, version, reviewer_id, action, comment, content_hash, created_at),
    )
    return approval_id, created_at


def list_approvals(conn: sqlite3.Connection, draft_id: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM approvals WHERE draft_id = ? ORDER BY created_at", (draft_id,)
        )
    ]


def list_agent_runs(conn: sqlite3.Connection, exception_id: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM agent_runs WHERE exception_id = ? ORDER BY started_at DESC, run_id DESC",
            (exception_id,),
        )
    ]


# ------------------------------------------------------------------ operational metrics


def batch_metrics(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT COUNT(*) AS batches, COALESCE(SUM(total_rows), 0) AS rows_processed, "
        "COALESCE(SUM(invalid_rows), 0) AS rows_rejected, "
        "COALESCE(SUM(exception_count), 0) AS exceptions, "
        "COALESCE(AVG(duration_ms), 0) AS avg_duration_ms FROM payroll_batches"
    ).fetchone()
    return dict(row)


def agent_run_metrics(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT status, COALESCE(error_category, '-') AS error_category, COUNT(*) AS runs, "
            "CAST(AVG(duration_ms) AS INTEGER) AS avg_duration_ms FROM agent_runs "
            "GROUP BY status, error_category ORDER BY status, runs DESC"
        )
    ]


def tool_metrics(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT tool_name, actor, COUNT(*) AS calls, SUM(1 - success) AS failures, "
            "CAST(AVG(duration_ms) AS INTEGER) AS avg_duration_ms FROM tool_events "
            "GROUP BY tool_name, actor ORDER BY calls DESC"
        )
    ]


def review_metrics(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Status of the latest version of each draft, with the oldest item's creation time."""
    return [
        dict(r)
        for r in conn.execute(
            "SELECT d.status, COUNT(*) AS drafts, MIN(d.created_at) AS oldest_created_at "
            "FROM drafts d JOIN (SELECT draft_id, MAX(draft_version) AS v FROM drafts "
            "GROUP BY draft_id) latest "
            "ON latest.draft_id = d.draft_id AND latest.v = d.draft_version "
            "GROUP BY d.status ORDER BY d.status"
        )
    ]


def json_loads(value: str) -> dict[str, Any]:
    loaded = json.loads(value)
    if not isinstance(loaded, dict):
        raise ValueError("expected a JSON object")
    return loaded


# ------------------------------------------------------------------ evaluations


def insert_evaluation_run(conn: sqlite3.Connection, report: dict[str, Any]) -> None:
    model_version = report["model_version"]
    digest = model_version.split("@", 1)[1] if "@" in model_version else None
    conn.execute(
        "INSERT INTO evaluation_runs (evaluation_id, model_version, model_digest, agent_version, "
        "prompt_version, tools_version, started_at, completed_at, metrics_json, passed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            report["evaluation_id"],
            model_version,
            digest,
            report["agent_version"],
            report["prompt_version"],
            report["tools_version"],
            report["started_at"],
            report["completed_at"],
            canonical_json(report["metrics"]),
            int(report["passed"]),
        ),
    )


def insert_evaluation_case(
    conn: sqlite3.Connection, evaluation_id: str, result: dict[str, Any]
) -> None:
    conn.execute(
        "INSERT INTO evaluation_cases (evaluation_id, case_id, expected_tools, actual_tools, "
        "score, passed, details_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            evaluation_id,
            result["case_id"],
            canonical_json(result["expected_tools"]),
            canonical_json(result["model_tools"]),
            float(result["passed"]),
            int(result["passed"]),
            canonical_json(result),
        ),
    )


def list_evaluation_runs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM evaluation_runs ORDER BY started_at DESC")]


def list_evaluation_cases(conn: sqlite3.Connection, evaluation_id: str) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM evaluation_cases WHERE evaluation_id = ? ORDER BY case_id",
            (evaluation_id,),
        )
    ]
