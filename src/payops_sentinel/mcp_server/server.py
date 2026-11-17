"""MCP v2 server: six read-only tools over a read-only SQLite connection, served on stdio.

Run as a separate process:  python -m payops_sentinel.mcp_server.server --db <path>
stdout carries only MCP protocol messages; diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import date
from pathlib import Path
from typing import Annotated

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from payops_sentinel.domain.enums import ExceptionStatus, ExceptionType
from payops_sentinel.domain.models import (
    BATCH_ID_PATTERN,
    CODE_PATTERN,
    EXCEPTION_ID_PATTERN,
)
from payops_sentinel.mcp_server import tools
from payops_sentinel.operations.registry import load_agent_asset
from payops_sentinel.storage.db import connect_readonly

SERVER_NAME = "payops-sentinel-readonly"
READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

BatchId = Annotated[str, Field(pattern=BATCH_ID_PATTERN, description="e.g. BATCH-000001")]
ExceptionId = Annotated[str, Field(pattern=EXCEPTION_ID_PATTERN, description="e.g. EXC-000004")]
Code = Annotated[str, Field(pattern=CODE_PATTERN)]


def build_server(db_path: Path, manifest_path: Path) -> MCPServer:
    """Create the server. Only read-only tools exist; there is no write capability to misuse."""
    asset = load_agent_asset(manifest_path)
    server = MCPServer(SERVER_NAME, version=tools.TOOLS_VERSION)

    @contextmanager
    def readonly() -> Iterator:
        with closing(connect_readonly(db_path)) as conn:
            yield conn

    @server.tool(annotations=READ_ONLY)
    def get_batch_summary(batch_id: BatchId) -> tools.BatchSummary:
        """Overview of a payroll batch: period, row counts, exception count and status."""
        with readonly() as conn:
            return tools.get_batch_summary(conn, batch_id)

    @server.tool(annotations=READ_ONLY)
    def list_exceptions(
        batch_id: BatchId,
        exception_type: ExceptionType | None = None,
        status: ExceptionStatus | None = None,
        limit: Annotated[int, Field(ge=1, le=25)] = 10,
    ) -> tools.ExceptionList:
        """List exceptions in a batch, optionally filtered by type and status (max 25)."""
        with readonly() as conn:
            return tools.list_exceptions(conn, batch_id, exception_type, status, limit)

    @server.tool(annotations=READ_ONLY)
    def get_exception_details(exception_id: ExceptionId) -> tools.ExceptionDetails:
        """Deterministic findings for one exception: amounts, rule, reason and evidence IDs."""
        with readonly() as conn:
            return tools.get_exception_details(conn, exception_id)

    @server.tool(annotations=READ_ONLY)
    def get_exception_evidence(exception_id: ExceptionId) -> tools.ExceptionEvidence:
        """Evidence records (payroll row, participant, enrollment, rule, calculation)."""
        with readonly() as conn:
            return tools.get_exception_evidence(conn, exception_id)

    @server.tool(annotations=READ_ONLY)
    def lookup_plan_rule(
        plan_id: Code,
        rule_code: Code,
        as_of_date: date,
        ruleset_version: Annotated[str, Field(max_length=40)] | None = None,
    ) -> tools.PlanRule:
        """The configured plan rule effective on a date (ISO YYYY-MM-DD)."""
        with readonly() as conn:
            return tools.lookup_plan_rule(conn, plan_id, rule_code, as_of_date, ruleset_version)

    @server.tool(annotations=READ_ONLY)
    def get_agent_asset(asset_id: Code) -> tools.AgentAssetRecord:
        """The registered configuration of this agent (version, model, owners, status)."""
        return tools.get_agent_asset(asset, asset_id)

    return server


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="payops-mcp-server")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)  # stdout is protocol-only
    build_server(args.db, args.manifest).run("stdio")


if __name__ == "__main__":
    main()
