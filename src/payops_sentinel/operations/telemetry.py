"""Operational metrics derived from SQLite. No payroll values or prompts are logged or shown."""

from __future__ import annotations

import sqlite3
from typing import Any

from payops_sentinel.storage import repositories as repo


def operational_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "batches": repo.batch_metrics(conn),
        "agent_runs": repo.agent_run_metrics(conn),
        "tool_activity": repo.tool_metrics(conn),
        "review_queue": repo.review_metrics(conn),
    }
