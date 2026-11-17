"""SQLite connections, transactions and schema initialisation."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from urllib.parse import quote

SCHEMA_VERSION = 2
BUSY_TIMEOUT_MS = 5000


def _configure(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def connect(db_path: Path) -> sqlite3.Connection:
    """Read-write connection in autocommit mode; use :func:`transaction` for writes."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return _configure(sqlite3.connect(db_path, isolation_level=None))


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Read-only connection: opened with ``mode=ro`` and ``PRAGMA query_only``."""
    if not db_path.exists():
        raise FileNotFoundError(f"database not found: {db_path}")
    uri = f"file:{quote(str(db_path.resolve()))}?mode=ro"
    conn = _configure(sqlite3.connect(uri, uri=True, isolation_level=None))
    conn.execute("PRAGMA query_only = ON")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """All-or-nothing unit of work. Rolls back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def initialize(conn: sqlite3.Connection) -> None:
    """Create the schema on an empty database; refuse unknown versions."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    if version != 0:
        raise RuntimeError(
            f"database schema v{version} is not v{SCHEMA_VERSION}; pre-release schemas are not "
            "migrated: delete the local demo database and run `payops init`"
        )
    script = files("payops_sentinel.storage").joinpath("schema.sql").read_text(encoding="utf-8")
    conn.executescript(f"BEGIN;\n{script}\nCOMMIT;")  # atomic: all tables or none
