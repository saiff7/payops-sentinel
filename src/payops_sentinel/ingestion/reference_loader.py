"""Load the synthetic reference data (employer, participants, enrollments, plan rules)."""

from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from payops_sentinel.domain.models import ContributionRule, Employer, Enrollment, Participant
from payops_sentinel.storage import repositories as repo
from payops_sentinel.storage.db import transaction


def _blank_to_none(row: dict[str, str]) -> dict[str, str | None]:
    return {k: (v or None) for k, v in row.items()}


def _read_csv(path: Path) -> list[dict[str, str | None]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return [_blank_to_none(r) for r in csv.DictReader(fh)]


def load_reference_files(
    directory: Path,
) -> tuple[Employer, list[Participant], list[Enrollment], list[ContributionRule]]:
    employer = Employer.model_validate_json((directory / "employer.json").read_text("utf-8"))
    participants = [
        Participant.model_validate(r) for r in _read_csv(directory / "participants.csv")
    ]
    enrollments = [Enrollment.model_validate(r) for r in _read_csv(directory / "enrollments.csv")]
    ruleset = json.loads((directory / "plan_rules.json").read_text("utf-8"))
    rules = [
        ContributionRule.model_validate({**r, "ruleset_version": ruleset["ruleset_version"]})
        for r in ruleset["rules"]
    ]
    return employer, participants, enrollments, rules


def seed_reference_data(conn: sqlite3.Connection, directory: Path) -> Employer:
    """Idempotent: re-seeding the same files changes nothing."""
    employer, participants, enrollments, rules = load_reference_files(directory)
    with transaction(conn):
        repo.insert_employer(conn, employer)
        for p in participants:
            repo.insert_participant(conn, employer.employer_id, p)
        for e in enrollments:
            repo.insert_enrollment(conn, employer.employer_id, e)
        for r in rules:
            repo.insert_rule(conn, r)
    return employer
