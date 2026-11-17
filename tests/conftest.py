from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from payops_sentinel.config import PROJECT_ROOT, Settings
from payops_sentinel.ingestion.reference_loader import seed_reference_data
from payops_sentinel.storage.db import connect, initialize

SYNTHETIC_DIR = PROJECT_ROOT / "data" / "synthetic"
EXPECTED_PATH = PROJECT_ROOT / "evals" / "expected_reconciliation.json"
HEADER = "employee_ref,pay_period_start,pay_period_end,eligible_compensation,employee_contribution"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "test.db",
        reference_data_dir=SYNTHETIC_DIR,
        employer_code="NORTHSTAR",
        ruleset_version="2026.10-demo",
        max_file_bytes=1_000_000,
        max_rows=1_000,
    )


@pytest.fixture
def conn(settings: Settings) -> Iterator[sqlite3.Connection]:
    connection = connect(settings.db_path)
    initialize(connection)
    seed_reference_data(connection, SYNTHETIC_DIR)
    yield connection
    connection.close()


@pytest.fixture
def make_csv() -> Callable[..., bytes]:
    """Build CSV bytes from row tuples; defaults to the standard header and October 2026."""

    def _make(*rows: tuple[str, ...] | str, header: str = HEADER) -> bytes:
        lines = [header]
        for r in rows:
            lines.append(r if isinstance(r, str) else ",".join(r))
        return ("\n".join(lines) + "\n").encode()

    return _make


def _row(
    ref: str, comp: str, contrib: str, start: str = "2026-10-01", end: str = "2026-10-31"
) -> tuple[str, ...]:
    return (ref, start, end, comp, contrib)


@pytest.fixture
def row() -> Callable[..., tuple[str, ...]]:
    """Build one CSV row tuple: row(ref, eligible_compensation, contribution, start=, end=)."""
    return _row


MANIFEST_PATH = PROJECT_ROOT / "governance" / "agent_manifest.toml"


@pytest.fixture
def ingested(conn: sqlite3.Connection, settings: Settings):
    """The database after processing the synthetic exceptions file (12 exceptions)."""
    from payops_sentinel.ingestion.service import ingest_payroll_file

    name = "payroll_with_exceptions.csv"
    return ingest_payroll_file(
        conn, settings, content=(SYNTHETIC_DIR / name).read_bytes(), file_name=name
    )


@pytest.fixture
def manifest_path() -> Path:
    return MANIFEST_PATH
