"""Individual deterministic contribution rules. Pure functions; no I/O, no AI."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from payops_sentinel.domain.enums import EnrollmentStatus
from payops_sentinel.domain.models import ContributionRule, Enrollment, Participant

BASIS_POINTS = Decimal(10_000)


def expected_contribution_cents(eligible_comp_cents: int, rate_bp: int) -> int:
    """round_half_up(eligible_comp_cents * rate_bp / 10 000), exact Decimal arithmetic."""
    if eligible_comp_cents < 0 or not 0 <= rate_bp <= 10_000:
        raise ValueError("compensation must be >= 0 and rate between 0 and 10000 bp")
    amount = Decimal(eligible_comp_cents) * Decimal(rate_bp) / BASIS_POINTS
    return int(amount.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _effective(start: date, end: date | None, on: date) -> bool:
    return start <= on and (end is None or on <= end)


def active_enrollment(enrollments: Iterable[Enrollment], on: date) -> Enrollment | None:
    """The active enrollment effective on ``on`` (inclusive bounds). Latest start wins."""
    matches = [
        e
        for e in enrollments
        if e.status is EnrollmentStatus.ACTIVE and _effective(e.effective_from, e.effective_to, on)
    ]
    return max(matches, key=lambda e: e.effective_from) if matches else None


def applicable_rule(
    rules: Iterable[ContributionRule], plan_id: str, rule_code: str, on: date
) -> ContributionRule | None:
    matches = [
        r
        for r in rules
        if r.plan_id == plan_id
        and r.rule_code == rule_code
        and _effective(r.effective_from, r.effective_to, on)
    ]
    return max(matches, key=lambda r: r.effective_from) if matches else None


def is_after_termination(participant: Participant, pay_period_start: date) -> bool:
    """A period that *starts* after termination. A final paycheck covering the date is normal."""
    return (
        participant.termination_date is not None and pay_period_start > participant.termination_date
    )


def within_tolerance(actual_cents: int, expected_cents: int, tolerance_cents: int) -> bool:
    return abs(actual_cents - expected_cents) <= tolerance_cents
