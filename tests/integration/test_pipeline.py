"""CSV → validation → SQLite → reconciliation → exceptions → evidence, end to end."""

from __future__ import annotations

import json
import sqlite3

import pytest

# conftest constants (importlib mode: import via the tests package path is not available)
from payops_sentinel.config import PROJECT_ROOT
from payops_sentinel.domain.enums import FileRejectionCode
from payops_sentinel.domain.models import ConfigurationError, FileRejectedError
from payops_sentinel.hashing import sha256_hex
from payops_sentinel.ingestion import service
from payops_sentinel.ingestion.service import ingest_payroll_file
from payops_sentinel.storage import repositories as repo
from payops_sentinel.storage.db import connect_readonly
from payops_sentinel.synthetic import write_dataset

SYNTHETIC_DIR = PROJECT_ROOT / "data" / "synthetic"
EXPECTED = json.loads((PROJECT_ROOT / "evals" / "expected_reconciliation.json").read_text())
TABLES = ("payroll_batches", "payroll_rows", "validation_errors", "exceptions", "evidence")


def _ingest_fixture(conn, settings, name: str):
    content = (SYNTHETIC_DIR / name).read_bytes()
    return ingest_payroll_file(conn, settings, content=content, file_name=name)


@pytest.mark.parametrize("name", ["payroll_clean.csv", "payroll_with_exceptions.csv"])
def test_golden_files_match_expected_reconciliation(conn, settings, name):
    expected = EXPECTED[name]
    result = _ingest_fixture(conn, settings, name)
    assert (result.total_rows, result.valid_rows, result.invalid_rows) == (
        expected["total_rows"],
        expected["valid_rows"],
        expected["invalid_rows"],
    )

    actual = repo.list_exceptions(conn, result.batch_id)
    assert len(actual) == len(expected["exceptions"]) == result.exception_count
    for exp, act in zip(expected["exceptions"], actual, strict=True):
        for key, value in exp.items():
            assert act[key] == value, (exp, key)


def test_all_four_exception_types_detected(conn, settings):
    result = _ingest_fixture(conn, settings, "payroll_with_exceptions.csv")
    types = {e["exception_type"] for e in repo.list_exceptions(conn, result.batch_id)}
    assert types == {
        "MISSING_CONTRIBUTION",
        "INCORRECT_CONTRIBUTION_RATE",
        "CONTRIBUTION_AFTER_TERMINATION",
        "DUPLICATE_OR_INVALID_RECORD",
    }


def test_every_exception_is_explainable_from_the_database(conn, settings):
    result = _ingest_fixture(conn, settings, "payroll_with_exceptions.csv")
    for exc in repo.list_exceptions(conn, result.batch_id):
        evidence = repo.list_evidence(conn, exc["exception_id"])
        assert len(evidence) >= 2, exc["exception_id"]
        for ev in evidence:
            assert sha256_hex(ev["snapshot_json"]) == ev["content_hash"]
            json.loads(ev["snapshot_json"])
        if exc["rule_id"]:
            assert any(ev["source_reference"] == f"plan_rules:{exc['rule_id']}" for ev in evidence)


def test_batch_metadata_and_validation_errors_recorded(conn, settings):
    result = _ingest_fixture(conn, settings, "payroll_with_exceptions.csv")
    batch = repo.get_batch(conn, result.batch_id)
    assert batch["status"] == "processed"
    assert batch["data_classification"] == "SYNTHETIC_DEMO"
    assert (batch["pay_period_start"], batch["pay_period_end"]) == ("2026-10-01", "2026-10-31")
    assert batch["file_sha256"] == sha256_hex(
        (SYNTHETIC_DIR / "payroll_with_exceptions.csv").read_bytes()
    )
    codes = {e["reason_code"] for e in repo.list_validation_errors(conn, result.batch_id)}
    assert {
        "NEGATIVE_AMOUNT",
        "MALFORMED_DATE",
        "MALFORMED_AMOUNT",
        "DUPLICATE_ROW",
        "MALFORMED_EMPLOYEE_REF",
        "UNKNOWN_PARTICIPANT",
    } <= codes


def test_untrusted_raw_values_never_persisted(conn, settings):
    _ingest_fixture(conn, settings, "payroll_with_exceptions.csv")
    dump = "\n".join(conn.iterdump())
    for raw in ("EMP-17A", "2026-10-32", "12,00", "-1500.00"):
        assert raw not in dump


def test_rejected_file_stores_nothing(conn, settings, make_csv, row):
    header = (
        "employee_ref,pay_period_start,pay_period_end,eligible_compensation,"
        "employee_contribution,ssn"
    )
    content = make_csv(",".join(row("EMP-0001", "1.00", "0.05")) + ",123-45-6789", header=header)
    with pytest.raises(FileRejectedError) as exc:
        ingest_payroll_file(conn, settings, content=content, file_name="bad.csv")
    assert exc.value.code is FileRejectionCode.RESTRICTED_COLUMNS
    assert all(repo.count_rows(conn, t) == 0 for t in TABLES)


def test_failure_mid_transaction_rolls_back_everything(conn, settings, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated failure after rows were inserted")

    monkeypatch.setattr(service, "reconcile", boom)
    with pytest.raises(RuntimeError, match="simulated"):
        _ingest_fixture(conn, settings, "payroll_with_exceptions.csv")
    assert all(repo.count_rows(conn, t) == 0 for t in TABLES)
    monkeypatch.undo()
    assert _ingest_fixture(conn, settings, "payroll_with_exceptions.csv").created  # retry is clean


def test_unknown_employer_and_ruleset_are_configuration_errors(conn, settings, make_csv, row):
    content = make_csv(row("EMP-0001", "1.00", "0.05"))
    with pytest.raises(ConfigurationError):
        ingest_payroll_file(conn, settings, content=content, file_name="a", employer_code="NOPE")
    with pytest.raises(ConfigurationError):
        ingest_payroll_file(conn, settings, content=content, file_name="a", ruleset_version="x")


def test_readonly_connection_cannot_write(conn, settings):
    _ingest_fixture(conn, settings, "payroll_clean.csv")
    ro = connect_readonly(settings.db_path)
    assert ro.execute("SELECT COUNT(*) FROM payroll_batches").fetchone()[0] == 1
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("DELETE FROM payroll_batches")
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("CREATE TABLE x (y)")


def test_audit_and_approval_tables_are_append_only(conn):
    conn.execute(
        "INSERT INTO audit_events (event_id, actor_id, event_type, object_id, payload_json, "
        "previous_hash, event_hash, created_at) VALUES ('A1','sys','t','o','{}','0','h','now')"
    )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("UPDATE audit_events SET actor_id = 'x'")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute("DELETE FROM audit_events")


def test_committed_synthetic_data_is_reproducible(tmp_path):
    write_dataset(tmp_path / "synthetic", tmp_path / "expected.json")
    for f in SYNTHETIC_DIR.iterdir():
        assert (tmp_path / "synthetic" / f.name).read_bytes() == f.read_bytes(), f.name
    expected_file = PROJECT_ROOT / "evals" / "expected_reconciliation.json"
    assert (tmp_path / "expected.json").read_bytes() == expected_file.read_bytes()
