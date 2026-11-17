"""Identical submissions never create a second batch or duplicate exceptions."""

from __future__ import annotations

from payops_sentinel.ingestion.service import ingest_payroll_file
from payops_sentinel.storage import repositories as repo
from payops_sentinel.storage.db import transaction

TABLES = ("payroll_batches", "payroll_rows", "validation_errors", "exceptions", "evidence")


def _counts(conn) -> dict[str, int]:
    return {t: repo.count_rows(conn, t) for t in TABLES}


def test_same_file_twice_returns_existing_batch(conn, settings, make_csv, row):
    content = make_csv(row("EMP-0017", "4000.00", "0.00"), row("EMP-0001", "1.00", "-1"))
    first = ingest_payroll_file(conn, settings, content=content, file_name="a.csv")
    before = _counts(conn)
    second = ingest_payroll_file(conn, settings, content=content, file_name="renamed.csv")
    assert first.created and not second.created
    assert second.batch_id == first.batch_id
    assert second.exception_count == first.exception_count == 2
    assert _counts(conn) == before


def test_materially_different_file_same_period_is_new_batch(conn, settings, make_csv, row):
    a = ingest_payroll_file(
        conn, settings, file_name="a.csv", content=make_csv(row("EMP-0017", "4000.00", "0.00"))
    )
    b = ingest_payroll_file(
        conn, settings, file_name="a.csv", content=make_csv(row("EMP-0017", "4000.00", "200.00"))
    )
    assert a.batch_id != b.batch_id
    assert (a.exception_count, b.exception_count) == (1, 0)


def test_same_file_under_new_ruleset_version_is_new_batch(conn, settings, make_csv, row):
    rule = repo.load_reference_data(conn, "ER-NORTHSTAR", settings.ruleset_version).rules[0]
    with transaction(conn):
        repo.insert_rule(
            conn, rule.model_copy(update={"rule_id": "RULE-V2", "ruleset_version": "2026.11-demo"})
        )
    content = make_csv(row("EMP-0017", "4000.00", "0.00"))
    a = ingest_payroll_file(conn, settings, content=content, file_name="a.csv")
    b = ingest_payroll_file(
        conn, settings, content=content, file_name="a.csv", ruleset_version="2026.11-demo"
    )
    assert a.batch_id != b.batch_id and b.created


def test_reseeding_reference_data_is_idempotent(conn, settings):
    from payops_sentinel.ingestion.reference_loader import seed_reference_data

    def ref_counts():
        return [
            conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("employers", "participants", "plan_enrollments", "plan_rules")
        ]

    before = ref_counts()
    seed_reference_data(conn, settings.reference_data_dir)
    assert ref_counts() == before
