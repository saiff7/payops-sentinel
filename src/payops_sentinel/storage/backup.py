"""Online SQLite backup and restore verification.

Uses SQLite's backup API (safe while the application is running), never a raw file copy.
A backup is only trusted after ``verify_database`` passes on the restored copy.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict

from payops_sentinel.review.audit import verify_chain
from payops_sentinel.storage.db import SCHEMA_VERSION, connect_readonly

COUNTED_TABLES = (
    "payroll_batches",
    "exceptions",
    "evidence",
    "drafts",
    "approvals",
    "audit_events",
    "agent_runs",
    "evaluation_runs",
)


class DatabaseCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    integrity_ok: bool
    schema_version: int
    audit_chain_valid: bool
    audit_events: int
    row_counts: dict[str, int]

    @property
    def healthy(self) -> bool:
        return (
            self.integrity_ok and self.schema_version == SCHEMA_VERSION and self.audit_chain_valid
        )


def backup_database(source: Path, destination: Path) -> Path:
    if destination.exists():
        raise FileExistsError(f"refusing to overwrite existing backup {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(f"file:{quote(str(source.resolve()))}?mode=ro", uri=True)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return destination


def verify_database(path: Path) -> DatabaseCheck:
    conn = connect_readonly(path)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        counts = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608 (constant)
            for t in COUNTED_TABLES
        }
        chain = verify_chain(conn)
    finally:
        conn.close()
    return DatabaseCheck(
        path=str(path),
        integrity_ok=integrity,
        schema_version=version,
        audit_chain_valid=chain.valid,
        audit_events=chain.events_checked,
        row_counts=counts,
    )
