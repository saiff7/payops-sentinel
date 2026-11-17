"""Batch ingestion: fingerprint → validate → store → reconcile, in one transaction."""

from __future__ import annotations

import logging
import sqlite3
import time

from payops_sentinel.config import Settings
from payops_sentinel.domain.enums import BatchStatus
from payops_sentinel.domain.models import ConfigurationError, IngestionResult
from payops_sentinel.hashing import sha256_hex
from payops_sentinel.ingestion.csv_loader import load_csv
from payops_sentinel.ingestion.validator import batch_pay_period, validate_headers, validate_rows
from payops_sentinel.reconciliation.engine import reconcile
from payops_sentinel.storage import repositories as repo
from payops_sentinel.storage.db import transaction

log = logging.getLogger(__name__)


def _existing_result(conn: sqlite3.Connection, batch_id: str) -> IngestionResult:
    b = repo.get_batch(conn, batch_id)
    assert b is not None
    return IngestionResult(
        batch_id=batch_id,
        created=False,
        status=BatchStatus(b["status"]),
        total_rows=b["total_rows"],
        valid_rows=b["valid_rows"],
        invalid_rows=b["invalid_rows"],
        exception_count=b["exception_count"],
    )


def ingest_payroll_file(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    content: bytes,
    file_name: str,
    employer_code: str | None = None,
    ruleset_version: str | None = None,
) -> IngestionResult:
    """Process one uploaded payroll file.

    Idempotent per (employer, file SHA-256, ruleset version): re-submitting the same file returns
    the existing batch. Raises ``FileRejectedError`` for whole-file problems (nothing stored) and
    ``ConfigurationError`` for missing reference data (transaction rolled back).
    """
    employer_code = employer_code or settings.employer_code
    ruleset_version = ruleset_version or settings.ruleset_version
    file_sha = sha256_hex(content)

    employer = repo.get_employer_by_code(conn, employer_code)
    if employer is None:
        raise ConfigurationError(f"unknown employer code {employer_code!r}")
    if not repo.ruleset_exists(conn, ruleset_version):
        raise ConfigurationError(f"no plan rules loaded for ruleset {ruleset_version!r}")

    existing = repo.find_batch_id(conn, employer.employer_id, file_sha, ruleset_version)
    if existing:
        log.info("batch_duplicate_submission batch_id=%s", existing)
        return _existing_result(conn, existing)

    headers, raw_rows = load_csv(
        content, max_bytes=settings.max_file_bytes, max_rows=settings.max_rows
    )
    validate_headers(headers)
    ref = repo.load_reference_data(conn, employer.employer_id, ruleset_version)
    parsed = validate_rows(raw_rows, known_refs=set(ref.participants))

    started = time.perf_counter()
    try:
        with transaction(conn):
            batch_id = repo.insert_batch(
                conn,
                employer_id=employer.employer_id,
                file_sha256=file_sha,
                file_name=file_name[:255],
                ruleset_version=ruleset_version,
            )
            row_ids = {
                p.source_row_number: repo.insert_payroll_row(conn, batch_id, p) for p in parsed
            }
            findings = reconcile(batch_id, parsed, ref)
            for f in findings:
                repo.insert_exception(conn, batch_id, row_ids[f.source_row_number], f)
            valid = sum(1 for p in parsed if p.is_valid)
            duration_ms = int((time.perf_counter() - started) * 1000)
            repo.finalize_batch(
                conn,
                batch_id,
                pay_period=batch_pay_period(p for p in parsed if p.is_valid),
                total_rows=len(parsed),
                valid_rows=valid,
                invalid_rows=len(parsed) - valid,
                exception_count=len(findings),
                duration_ms=duration_ms,
            )
    except sqlite3.IntegrityError:
        # A concurrent identical submission won the race: return its batch.
        concurrent = repo.find_batch_id(conn, employer.employer_id, file_sha, ruleset_version)
        if concurrent is None:
            raise
        return _existing_result(conn, concurrent)

    log.info(
        "batch_processed batch_id=%s rows=%d valid=%d exceptions=%d duration_ms=%d",
        batch_id,
        len(parsed),
        valid,
        len(findings),
        duration_ms,
    )
    return IngestionResult(
        batch_id=batch_id,
        created=True,
        status=BatchStatus.PROCESSED,
        total_rows=len(parsed),
        valid_rows=valid,
        invalid_rows=len(parsed) - valid,
        exception_count=len(findings),
    )
