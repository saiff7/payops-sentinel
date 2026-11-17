"""Deterministic reconciliation: turns validated rows + reference data into exception findings.

Pure and side-effect free, so it is fully unit-testable and runs with every AI component offline.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from payops_sentinel.domain.enums import EvidenceType, ExceptionType, ReasonCode, Severity
from payops_sentinel.domain.models import (
    DEFERRAL_RULE_CODE,
    ConfigurationError,
    ContributionRule,
    Enrollment,
    EvidenceItem,
    ExceptionFinding,
    ParsedRow,
    Participant,
    PayrollRow,
    ReferenceData,
)
from payops_sentinel.reconciliation import rules

SEVERITY: dict[ExceptionType, Severity] = {
    ExceptionType.MISSING_CONTRIBUTION: Severity.MEDIUM,
    ExceptionType.INCORRECT_CONTRIBUTION_RATE: Severity.MEDIUM,
    ExceptionType.CONTRIBUTION_AFTER_TERMINATION: Severity.HIGH,
    ExceptionType.DUPLICATE_OR_INVALID_RECORD: Severity.LOW,
}


# ------------------------------------------------------------------ evidence builders


def _row_evidence(batch_id: str, n: int, snapshot: dict[str, Any]) -> EvidenceItem:
    return EvidenceItem(
        evidence_type=EvidenceType.PAYROLL_ROW,
        source_reference=f"payroll_rows:{batch_id}:row-{n}",
        snapshot={"source_row_number": n, **snapshot},
    )


def _payroll_evidence(batch_id: str, row: PayrollRow) -> EvidenceItem:
    return _row_evidence(
        batch_id, row.source_row_number, row.model_dump(mode="json", exclude={"source_row_number"})
    )


def _participant_evidence(p: Participant) -> EvidenceItem:
    return EvidenceItem(
        evidence_type=EvidenceType.PARTICIPANT,
        source_reference=f"participants:{p.participant_id}",
        snapshot=p.model_dump(mode="json", exclude={"participant_id"}),
    )


def _enrollment_evidence(e: Enrollment) -> EvidenceItem:
    return EvidenceItem(
        evidence_type=EvidenceType.PLAN_ENROLLMENT,
        source_reference=f"plan_enrollments:{e.enrollment_id}",
        snapshot=e.model_dump(mode="json"),
    )


def _rule_evidence(r: ContributionRule) -> EvidenceItem:
    return EvidenceItem(
        evidence_type=EvidenceType.PLAN_RULE,
        source_reference=f"plan_rules:{r.rule_id}",
        snapshot=r.model_dump(
            mode="json",
            include={
                "rule_id",
                "rule_code",
                "version",
                "ruleset_version",
                "plan_id",
                "effective_from",
                "effective_to",
                "rounding",
                "tolerance_cents",
                "source_reference",
            },
        ),
    )


def _calculation_evidence(
    row: PayrollRow,
    enrollment: Enrollment | None,
    rule: ContributionRule | None,
    expected: int,
    actual: int,
) -> EvidenceItem:
    return EvidenceItem(
        evidence_type=EvidenceType.CALCULATION,
        source_reference=f"reconciliation:row-{row.source_row_number}",
        snapshot={
            "formula": "round_half_up(eligible_comp_cents * elected_rate_bp / 10000)",
            "eligible_comp_cents": row.eligible_comp_cents,
            "elected_rate_bp": enrollment.elected_rate_bp if enrollment else 0,
            "expected_cents": expected,
            "actual_cents": actual,
            "difference_cents": actual - expected,
            "tolerance_cents": rule.tolerance_cents if rule else 0,
        },
    )


# ------------------------------------------------------------------ findings


def _finding(
    row: PayrollRow,
    exception_type: ExceptionType,
    reason_code: ReasonCode,
    reason: str,
    *,
    expected: int | None,
    actual: int,
    rule: ContributionRule | None,
    evidence: Iterable[EvidenceItem],
) -> ExceptionFinding:
    return ExceptionFinding(
        source_row_number=row.source_row_number,
        exception_type=exception_type,
        severity=SEVERITY[exception_type],
        reason_code=reason_code,
        reason=reason,
        employee_ref=row.employee_ref,
        pay_period_end=row.pay_period_end,
        expected_cents=expected,
        actual_cents=actual,
        rule_id=rule.rule_id if rule else None,
        evidence=tuple(evidence),
    )


def reconcile_row(batch_id: str, row: PayrollRow, ref: ReferenceData) -> ExceptionFinding | None:
    participant = ref.participants.get(row.employee_ref)
    if participant is None:  # validator guarantees this; fail loudly if bypassed
        raise ConfigurationError(f"row {row.source_row_number}: participant not in reference data")
    actual = row.contribution_cents or 0
    base = [_payroll_evidence(batch_id, row), _participant_evidence(participant)]

    if actual > 0 and rules.is_after_termination(participant, row.pay_period_start):
        return _finding(
            row,
            ExceptionType.CONTRIBUTION_AFTER_TERMINATION,
            ReasonCode.PERIOD_STARTS_AFTER_TERMINATION,
            "Contribution reported for a pay period that starts after the recorded "
            "termination date. Flagged for human investigation.",
            expected=None,
            actual=actual,
            rule=None,
            evidence=base,
        )

    enrollment = rules.active_enrollment(
        ref.enrollments.get(row.employee_ref, ()), row.pay_period_end
    )
    if enrollment is None:
        if actual == 0:
            return None
        return _finding(
            row,
            ExceptionType.INCORRECT_CONTRIBUTION_RATE,
            ReasonCode.NO_ACTIVE_ENROLLMENT,
            "Contribution reported but no active enrollment is effective on the pay period end; "
            "the configured election is 0.",
            expected=0,
            actual=actual,
            rule=None,
            evidence=[*base, _calculation_evidence(row, None, None, 0, actual)],
        )

    rule = rules.applicable_rule(
        ref.rules, enrollment.plan_id, DEFERRAL_RULE_CODE, row.pay_period_end
    )
    if rule is None:
        raise ConfigurationError(
            f"no {DEFERRAL_RULE_CODE} rule for plan {enrollment.plan_id} on {row.pay_period_end}"
        )
    expected = rules.expected_contribution_cents(
        row.eligible_comp_cents, enrollment.elected_rate_bp
    )
    if rules.within_tolerance(actual, expected, rule.tolerance_cents):
        return None

    evidence = [
        *base,
        _enrollment_evidence(enrollment),
        _rule_evidence(rule),
        _calculation_evidence(row, enrollment, rule, expected, actual),
    ]
    if actual == 0:
        return _finding(
            row,
            ExceptionType.MISSING_CONTRIBUTION,
            ReasonCode.CONTRIBUTION_ZERO_OR_BLANK,
            "Contribution is blank or zero although the participant has an active positive "
            "election and positive eligible compensation.",
            expected=expected,
            actual=actual,
            rule=rule,
            evidence=evidence,
        )
    return _finding(
        row,
        ExceptionType.INCORRECT_CONTRIBUTION_RATE,
        ReasonCode.CONTRIBUTION_RATE_MISMATCH,
        "Reported contribution differs from the configured election calculation by more than "
        "the rule tolerance.",
        expected=expected,
        actual=actual,
        rule=rule,
        evidence=evidence,
    )


def invalid_row_finding(batch_id: str, parsed: ParsedRow) -> ExceptionFinding:
    """Every invalid row (malformed, unknown participant, duplicate) becomes one exception."""
    assert parsed.errors, "only invalid rows produce invalid-record findings"
    s = parsed.sanitized
    evidence = [
        _row_evidence(
            batch_id,
            parsed.source_row_number,
            {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in s.items()},
        ),
        EvidenceItem(
            evidence_type=EvidenceType.VALIDATION_ERRORS,
            source_reference=f"validation_errors:{batch_id}:row-{parsed.source_row_number}",
            snapshot={
                "errors": [
                    {"field": e.field, "reason_code": e.reason_code.value} for e in parsed.errors
                ],
                "duplicate_of_source_row": parsed.duplicate_of,
            },
        ),
    ]
    first = parsed.errors[0]
    return ExceptionFinding(
        source_row_number=parsed.source_row_number,
        exception_type=ExceptionType.DUPLICATE_OR_INVALID_RECORD,
        severity=Severity.MEDIUM if first.reason_code is ReasonCode.DUPLICATE_ROW else Severity.LOW,
        reason_code=first.reason_code,
        reason="; ".join(e.message for e in parsed.errors),
        employee_ref=s.get("employee_ref"),
        pay_period_end=s.get("pay_period_end"),
        expected_cents=None,
        actual_cents=s.get("contribution_cents"),
        rule_id=None,
        evidence=tuple(evidence),
    )


def reconcile(
    batch_id: str, parsed_rows: Iterable[ParsedRow], ref: ReferenceData
) -> list[ExceptionFinding]:
    findings: list[ExceptionFinding] = []
    for parsed in parsed_rows:
        if parsed.row is None:
            findings.append(invalid_row_finding(batch_id, parsed))
            continue
        finding = reconcile_row(batch_id, parsed.row, ref)
        if finding is not None:
            findings.append(finding)
    return findings
