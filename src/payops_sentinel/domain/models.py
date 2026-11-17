"""Domain models. Pure data: no I/O, no AI. Money is integer cents, rates are basis points."""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from payops_sentinel.domain.enums import (
    BatchStatus,
    EnrollmentStatus,
    EvidenceType,
    ExceptionType,
    FileRejectionCode,
    ReasonCode,
    Severity,
)

EMPLOYEE_REF_PATTERN = r"^EMP-\d{4}$"
DEFERRAL_RULE_CODE = "EMPLOYEE_DEFERRAL_PCT"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Employer(_Frozen):
    employer_id: str
    employer_code: str
    name: str


class Participant(_Frozen):
    participant_id: str
    employee_ref: str = Field(pattern=EMPLOYEE_REF_PATTERN)
    termination_date: date | None = None


class Enrollment(_Frozen):
    enrollment_id: str
    employee_ref: str = Field(pattern=EMPLOYEE_REF_PATTERN)
    plan_id: str
    elected_rate_bp: int = Field(ge=0, le=10_000)
    effective_from: date
    effective_to: date | None = None
    status: EnrollmentStatus


class ContributionRule(_Frozen):
    rule_id: str
    ruleset_version: str
    plan_id: str
    rule_code: str
    version: str
    effective_from: date
    effective_to: date | None = None
    rounding: Literal["ROUND_HALF_UP"] = "ROUND_HALF_UP"
    tolerance_cents: int = Field(ge=0, le=100)
    description: str
    source_reference: str


class ReferenceData(_Frozen):
    """Everything reconciliation needs, keyed for lookup. Loaded once per batch."""

    participants: dict[str, Participant]
    enrollments: dict[str, tuple[Enrollment, ...]]
    rules: tuple[ContributionRule, ...]


class PayrollRow(_Frozen):
    """A fully validated payroll row."""

    source_row_number: int = Field(ge=2)
    employee_ref: str = Field(pattern=EMPLOYEE_REF_PATTERN)
    pay_period_start: date
    pay_period_end: date
    eligible_comp_cents: int = Field(ge=0)
    contribution_cents: int | None = Field(default=None, ge=0)  # None = blank in source


class RowError(_Frozen):
    source_row_number: int
    field: str
    reason_code: ReasonCode
    message: str


class ParsedRow(_Frozen):
    """Outcome of validating one source row.

    ``sanitized`` holds only values that passed format validation, so raw untrusted text
    (which could contain personal data) is never persisted.
    """

    source_row_number: int
    row: PayrollRow | None
    sanitized: dict[str, Any]
    errors: tuple[RowError, ...] = ()
    duplicate_of: int | None = None

    @property
    def is_valid(self) -> bool:
        return self.row is not None and not self.errors


class EvidenceItem(_Frozen):
    evidence_type: EvidenceType
    source_reference: str
    snapshot: dict[str, Any]


class ExceptionFinding(_Frozen):
    source_row_number: int
    exception_type: ExceptionType
    severity: Severity
    reason_code: ReasonCode
    reason: str
    employee_ref: str | None
    pay_period_end: date | None
    expected_cents: int | None
    actual_cents: int | None
    rule_id: str | None
    evidence: tuple[EvidenceItem, ...]


class IngestionResult(_Frozen):
    batch_id: str
    created: bool
    status: BatchStatus
    total_rows: int
    valid_rows: int
    invalid_rows: int
    exception_count: int


class FileRejectedError(Exception):
    """The whole file is rejected before anything is stored."""

    def __init__(self, code: FileRejectionCode, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class ConfigurationError(Exception):
    """Reference data or rules are missing or inconsistent. Processing must stop."""
