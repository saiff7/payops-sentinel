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
