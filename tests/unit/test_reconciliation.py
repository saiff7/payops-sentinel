"""Deterministic reconciliation rules, exercised without any database or AI component."""

from __future__ import annotations

from datetime import date

import pytest

from payops_sentinel.domain.enums import EnrollmentStatus, EvidenceType, ExceptionType, ReasonCode
from payops_sentinel.domain.models import (
    DEFERRAL_RULE_CODE,
    ConfigurationError,
    ContributionRule,
    Enrollment,
    Participant,
    PayrollRow,
    ReferenceData,
)
from payops_sentinel.reconciliation import rules
from payops_sentinel.reconciliation.engine import reconcile_row

END = date(2026, 10, 31)
RULE = ContributionRule(
    rule_id="R1",
    ruleset_version="t",
    plan_id="P",
    rule_code=DEFERRAL_RULE_CODE,
    version="1",
    effective_from=date(2026, 1, 1),
    tolerance_cents=1,
    description="d",
    source_reference="s",
)


def _enr(
    rate: int = 500,
    start: date = date(2025, 1, 1),
    end: date | None = None,
    status: EnrollmentStatus = EnrollmentStatus.ACTIVE,
    eid: str = "E1",
) -> Enrollment:
    return Enrollment(
        enrollment_id=eid,
        employee_ref="EMP-0001",
        plan_id="P",
        elected_rate_bp=rate,
        effective_from=start,
        effective_to=end,
        status=status,
    )


def _ref(
    *enrollments: Enrollment,
    termination: date | None = None,
    rule_list: tuple[ContributionRule, ...] = (RULE,),
) -> ReferenceData:
    p = Participant(participant_id="PRT-1", employee_ref="EMP-0001", termination_date=termination)
    return ReferenceData(
        participants={"EMP-0001": p}, enrollments={"EMP-0001": enrollments}, rules=rule_list
    )


def _row(
    comp: int = 400_000, contrib: int | None = 20_000, start: date = date(2026, 10, 1)
) -> PayrollRow:
    return PayrollRow(
        source_row_number=2,
        employee_ref="EMP-0001",
        pay_period_start=start,
        pay_period_end=END,
        eligible_comp_cents=comp,
        contribution_cents=contrib,
    )


# ------------------------------------------------------------------ arithmetic


@pytest.mark.parametrize(
    ("comp", "bp", "expected"),
    [
        (400_000, 500, 20_000),  # $4,000.00 x 5% = $200.00 (blueprint example)
        (1_050, 500, 53),  # 52.5 cents rounds half-up to 53
        (1_030, 500, 52),  # 51.5 -> 52
        (1_029, 500, 51),  # 51.45 -> 51
        (123_457, 300, 3_704),  # 3703.71 -> 3704
        (0, 1_000, 0),
        (999_999_999, 10_000, 999_999_999),
        (500_000, 0, 0),
    ],
)
def test_expected_contribution_rounding(comp, bp, expected):
    assert rules.expected_contribution_cents(comp, bp) == expected


@pytest.mark.parametrize(("comp", "bp"), [(-1, 500), (100, -1), (100, 10_001)])
def test_expected_contribution_rejects_invalid_inputs(comp, bp):
    with pytest.raises(ValueError, match="compensation"):
        rules.expected_contribution_cents(comp, bp)


# ------------------------------------------------------------------ outcomes


def test_correct_contribution_has_no_exception():
    assert reconcile_row("B", _row(), _ref(_enr())) is None


@pytest.mark.parametrize("delta", [-1, 1])
def test_one_cent_difference_is_within_tolerance(delta):
    assert reconcile_row("B", _row(contrib=20_000 + delta), _ref(_enr())) is None


@pytest.mark.parametrize("delta", [-2, 2, -10_000])
def test_difference_beyond_tolerance_is_incorrect_rate(delta):
    f = reconcile_row("B", _row(contrib=20_000 + delta), _ref(_enr()))
    assert f.exception_type is ExceptionType.INCORRECT_CONTRIBUTION_RATE
    assert f.reason_code is ReasonCode.CONTRIBUTION_RATE_MISMATCH
    assert (f.expected_cents, f.actual_cents, f.rule_id) == (20_000, 20_000 + delta, "R1")


@pytest.mark.parametrize("contrib", [0, None])
def test_zero_or_blank_contribution_is_missing(contrib):
    f = reconcile_row("B", _row(contrib=contrib), _ref(_enr()))
    assert f.exception_type is ExceptionType.MISSING_CONTRIBUTION
    assert (f.expected_cents, f.actual_cents) == (20_000, 0)


def test_zero_election_with_zero_contribution_is_fine():
    assert reconcile_row("B", _row(contrib=0), _ref(_enr(rate=0))) is None


def test_zero_compensation_with_zero_contribution_is_fine():
    assert reconcile_row("B", _row(comp=0, contrib=0), _ref(_enr())) is None


def test_inactive_enrollment_without_contribution_is_fine():
    ref = _ref(_enr(status=EnrollmentStatus.INACTIVE))
    assert reconcile_row("B", _row(contrib=0), ref) is None


def test_contribution_without_active_enrollment_is_incorrect_rate():
    f = reconcile_row("B", _row(contrib=500), _ref(_enr(status=EnrollmentStatus.INACTIVE)))
    assert f.exception_type is ExceptionType.INCORRECT_CONTRIBUTION_RATE
    assert f.reason_code is ReasonCode.NO_ACTIVE_ENROLLMENT
    assert (f.expected_cents, f.actual_cents) == (0, 500)


# ------------------------------------------------------------------ effective dating


@pytest.mark.parametrize(
    ("start", "end", "active"),
    [
        (END, None, True),  # starts exactly on period end
        (date(2025, 1, 1), END, True),  # ends exactly on period end (inclusive)
        (date(2025, 1, 1), date(2026, 10, 30), False),
        (date(2026, 11, 1), None, False),  # future enrollment
    ],
)
def test_enrollment_effective_date_boundaries(start, end, active):
    found = rules.active_enrollment([_enr(start=start, end=end)], END)
    assert (found is not None) is active


def test_latest_effective_enrollment_wins():
    old = _enr(rate=300, end=date(2026, 10, 30), eid="OLD")
    new = _enr(rate=700, start=END, eid="NEW")
    f = reconcile_row("B", _row(contrib=12_000), _ref(old, new))  # 300bp would be 12,000
    assert f.expected_cents == 28_000


def test_rule_effective_dating_selects_matching_version():
    v2 = RULE.model_copy(
        update={
            "rule_id": "R2",
            "version": "2",
            "tolerance_cents": 5,
            "effective_from": date(2026, 10, 1),
        }
    )
    v1 = RULE.model_copy(update={"effective_to": date(2026, 9, 30)})
    assert reconcile_row("B", _row(contrib=20_004), _ref(_enr(), rule_list=(v1, v2))) is None
    assert (
        rules.applicable_rule((v1, v2), "P", DEFERRAL_RULE_CODE, date(2026, 9, 30)).rule_id == "R1"
    )


def test_missing_rule_is_configuration_error():
    with pytest.raises(ConfigurationError):
        reconcile_row("B", _row(), _ref(_enr(), rule_list=()))


# ------------------------------------------------------------------ termination


def test_period_starting_after_termination_is_flagged():
    f = reconcile_row("B", _row(), _ref(_enr(), termination=date(2026, 9, 15)))
    assert f.exception_type is ExceptionType.CONTRIBUTION_AFTER_TERMINATION
    assert f.severity.value == "high"
    assert f.expected_cents is None


def test_final_paycheck_covering_termination_date_is_normal():
    assert reconcile_row("B", _row(), _ref(_enr(), termination=date(2026, 10, 20))) is None


def test_terminated_participant_with_zero_contribution_not_flagged_as_after_termination():
    f = reconcile_row("B", _row(contrib=0), _ref(_enr(), termination=date(2026, 9, 15)))
    assert f is None or f.exception_type is not ExceptionType.CONTRIBUTION_AFTER_TERMINATION


# ------------------------------------------------------------------ evidence


def test_findings_carry_traceable_evidence():
    f = reconcile_row("BATCH-9", _row(contrib=0), _ref(_enr()))
    types = [e.evidence_type for e in f.evidence]
    assert types == [
        EvidenceType.PAYROLL_ROW,
        EvidenceType.PARTICIPANT,
        EvidenceType.PLAN_ENROLLMENT,
        EvidenceType.PLAN_RULE,
        EvidenceType.CALCULATION,
    ]
    assert f.evidence[0].source_reference == "payroll_rows:BATCH-9:row-2"
    calc = f.evidence[-1].snapshot
    assert (calc["expected_cents"], calc["elected_rate_bp"], calc["tolerance_cents"]) == (
        20_000,
        500,
        1,
    )


# ------------------------------------------------------------------ architecture boundary


def test_deterministic_layers_have_no_ai_dependencies():
    import ast
    from pathlib import Path

    import payops_sentinel

    root = Path(payops_sentinel.__file__).parent
    for layer in ("domain", "ingestion", "reconciliation", "storage"):
        for path in (root / layer).glob("*.py"):
            tree = ast.parse(path.read_text())
            names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            names |= {
                n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
            }
            forbidden = {m for m in names if m.split(".")[0] in {"ollama", "mcp", "streamlit"}}
            forbidden |= {
                m
                for m in names
                if m.startswith(("payops_sentinel.agent", "payops_sentinel.mcp_server"))
            }
            assert not forbidden, f"{path.name} imports {forbidden}"


# ------------------------------------------------------------------ dollar display formatting


@pytest.mark.parametrize(
    ("cents", "text"),
    [
        (363_140, "$3,631.40"),
        (20_000, "$200.00"),
        (0, "$0.00"),
        (1, "$0.01"),
        (-2, "-$0.02"),
        (99_999_999_999, "$999,999,999.99"),
    ],
)
def test_dollar_display_matches_guardrail_parsing(cents, text):
    from payops_sentinel.agent.guardrails import mentioned_amounts_cents
    from payops_sentinel.mcp_server.tools import dollars

    assert dollars(cents) == text
    assert mentioned_amounts_cents(f"amount {text} here") == {abs(cents)}
