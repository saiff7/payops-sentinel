"""Bounded agent loop: Qwen3 (via Ollama function calling) ⇄ host guardrails ⇄ MCP client.

The model never touches MCP directly. The host discovers tools, exposes only allowlisted ones,
validates each requested call, executes it through the MCP client, and returns the result to the
model. The final draft is schema-constrained, Pydantic-validated and checked against ground
truth fetched by the host. Every failure ends in MANUAL_REVIEW; nothing is ever auto-approved.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import anyio
from mcp import Client, StdioServerParameters
from mcp.types import Tool
from pydantic import ValidationError

from payops_sentinel import __version__
from payops_sentinel.agent import prompts
from payops_sentinel.agent.guardrails import (
    AGENT_PRINCIPAL,
    AgentLimits,
    DraftContext,
    GuardrailViolation,
    allowed_amounts,
    check_tool_catalog,
    find_evidence_ids,
    to_ollama_tools,
    validate_draft,
    validate_tool_call,
)
from payops_sentinel.agent.ollama_client import (
    ChatModel,
    ModelReply,
    ModelUnavailableError,
    OllamaChatModel,
)
from payops_sentinel.agent.schemas import (
    AgentInvestigationDraft,
    FailureCategory,
    InvestigationOutcome,
    InvestigationStatus,
    ToolCallRecord,
)
from payops_sentinel.config import PROJECT_ROOT, Settings
from payops_sentinel.domain.enums import ExceptionStatus
from payops_sentinel.domain.models import EXCEPTION_ID_PATTERN
from payops_sentinel.hashing import canonical_json, sha256_hex
from payops_sentinel.review.audit import AuditEventType, append_event
from payops_sentinel.storage import repositories as repo
from payops_sentinel.storage.db import connect, initialize, transaction

log = logging.getLogger(__name__)


class _Stop(Exception):
    """Abort the investigation and fall back to manual review."""

    def __init__(self, category: FailureCategory, detail: str) -> None:
        super().__init__(detail)
        self.category = category
        self.detail = detail


@dataclass
class _Run:
    exception_id: str
    started: float = field(default_factory=time.perf_counter)
    calls: list[ToolCallRecord] = field(default_factory=list)
    model_call_count: int = 0
    seen_evidence_ids: set[str] = field(default_factory=set)
    failures: Counter[str] = field(default_factory=Counter)
    batch_id: str | None = None

    def record(self, **kwargs: Any) -> ToolCallRecord:
        rec = ToolCallRecord(sequence=len(self.calls) + 1, **kwargs)
        self.calls.append(rec)
        return rec


class InvestigationOrchestrator:
    def __init__(self, model: ChatModel, limits: AgentLimits, conn: sqlite3.Connection) -> None:
        self.model = model
        self.limits = limits
        self.conn = conn  # application (read-write) connection, used only to record the run

    # ------------------------------------------------------------------ public API

    async def investigate(self, client: Client, exception_id: str) -> InvestigationOutcome:
        if not re.fullmatch(EXCEPTION_ID_PATTERN, exception_id):
            raise ValueError("exception_id must look like EXC-000001")
        run = _Run(exception_id=exception_id)
        model_version = self.model.model_name
        try:
            model_version = await anyio.to_thread.run_sync(self.model.describe)
            exposed = check_tool_catalog(await self._list_tools(client))
            ctx = await self._ground_truth(client, run)
            messages = await self._gather_evidence(client, run, to_ollama_tools(exposed))
            draft = await self._draft(run, messages, ctx)
        except _Stop as stop:
            return self._finish(run, model_version, stop.category, stop.detail)
        except GuardrailViolation as v:
            return self._finish(run, model_version, FailureCategory(v.category), v.message)
        except ModelUnavailableError as exc:
            return self._finish(run, model_version, FailureCategory.MODEL_UNAVAILABLE, str(exc))
        except Exception as exc:  # fail closed on anything unexpected, but never silently
            log.exception("investigation_failed exception_id=%s", exception_id)
            return self._finish(
                run, model_version, FailureCategory.INTERNAL_ERROR, type(exc).__name__
            )
        return self._finish(run, model_version, None, None, draft)

    def fail_closed(
        self, exception_id: str, category: FailureCategory, detail: str
    ) -> InvestigationOutcome:
        """Record a run that could not even start (e.g. the MCP process failed to launch)."""
        return self._finish(
            _Run(exception_id=exception_id), self.model.model_name, category, detail
        )

    # ------------------------------------------------------------------ steps

    async def _ground_truth(self, client: Client, run: _Run) -> DraftContext:
        """Host-initiated reads (not offered to the model) used to verify the final draft."""
        args = {"exception_id": run.exception_id}
        details = await self._call(client, run, "host", "get_exception_details", args)
        if details is None:
            raise _Stop(FailureCategory.EXCEPTION_NOT_FOUND, f"{run.exception_id} not found")
        evidence = await self._call(client, run, "host", "get_exception_evidence", args)
        if evidence is None:
            raise _Stop(FailureCategory.MCP_UNAVAILABLE, "evidence could not be retrieved")
        run.batch_id = details.get("batch_id")
        return DraftContext(
            exception_id=run.exception_id,
            employee_ref=details.get("employee_ref"),
            evidence_ids=frozenset(details.get("evidence_ids", [])),
            allowed_amounts_cents=allowed_amounts(details, evidence),
        )

    async def _gather_evidence(
        self, client: Client, run: _Run, tools: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": prompts.SYSTEM_PROMPT},
            {"role": "user", "content": prompts.investigation_request(run.exception_id)},
        ]
        for _ in range(self.limits.max_iterations):
            reply = await self._chat(messages, tools=tools)
            if not reply.tool_calls:
                break
            messages.append(
                {
                    "role": "assistant",
                    "content": reply.content,
                    "tool_calls": [
                        {"function": {"name": c.name, "arguments": c.arguments}}
                        for c in reply.tool_calls
                    ],
                }
            )
            budget_exhausted = False
            for call in reply.tool_calls:
                if run.model_call_count >= self.limits.max_tool_calls:
                    budget_exhausted = True
                    content = "rejected: tool call limit reached; prepare the draft now"
                else:
                    run.model_call_count += 1
                    content = await self._model_tool_call(client, run, call.name, call.arguments)
                messages.append({"role": "tool", "tool_name": call.name, "content": content})
            if budget_exhausted:
                break
        return messages

    async def _model_tool_call(self, client: Client, run: _Run, name: str, arguments: Any) -> str:
        signature = f"{name}:{canonical_json(arguments)}"
        try:
            validated = validate_tool_call(name, arguments)
        except GuardrailViolation as v:
            run.record(
                actor="model",
                tool_name=name[:100],
                arguments=arguments if isinstance(arguments, dict) else {},
                success=False,
                error_category=v.category,
            )
            self._count_failure(run, signature)
            return f"rejected: {v.message}"
        result = await self._call(client, run, "model", name, validated)
        if result is None:
            self._count_failure(run, signature)
            return f"error: {run.calls[-1].error_category}"
        run.seen_evidence_ids |= find_evidence_ids(result)
        return json.dumps(result, sort_keys=True)

    async def _call(
        self, client: Client, run: _Run, actor: str, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Execute one MCP tool call. Transport failures raise; tool errors return None."""
        started = time.perf_counter()
        try:
            result = await client.call_tool(
                name, arguments, read_timeout_seconds=self.limits.tool_timeout_s
            )
        except Exception as exc:  # transport failure or timeout: the MCP process is unusable
            run.record(
                actor=actor,
                tool_name=name,
                arguments=arguments,
                success=False,
                error_category="TRANSPORT_ERROR",
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
            raise _Stop(FailureCategory.MCP_UNAVAILABLE, f"{name}: {type(exc).__name__}") from exc
        elapsed = int((time.perf_counter() - started) * 1000)
        if result.is_error or result.structured_content is None:
            text = str(getattr(result.content[0], "text", "")) if result.content else ""
            category = "NOT_FOUND" if "not_found" in text else "TOOL_ERROR"
            run.record(
                actor=actor,
                tool_name=name,
                arguments=arguments,
                success=False,
                error_category=category,
                duration_ms=elapsed,
            )
            return None
        body = canonical_json(result.structured_content)
        if len(body) > self.limits.max_result_chars:
            run.record(
                actor=actor,
                tool_name=name,
                arguments=arguments,
                success=False,
                error_category="RESULT_TOO_LARGE",
                duration_ms=elapsed,
            )
            return None
        run.record(
            actor=actor,
            tool_name=name,
            arguments=arguments,
            success=True,
            result_hash=sha256_hex(body),
            duration_ms=elapsed,
        )
        return result.structured_content

    async def _list_tools(self, client: Client) -> list[Tool]:
        try:
            return list((await client.list_tools()).tools)
        except Exception as exc:
            raise _Stop(
                FailureCategory.MCP_UNAVAILABLE, f"list_tools: {type(exc).__name__}"
            ) from exc

    def _count_failure(self, run: _Run, signature: str) -> None:
        run.failures[signature] += 1
        if run.failures[signature] >= 2:
            raise _Stop(
                FailureCategory.REPEATED_FAILED_CALL, "identical failing tool call repeated"
            )

    async def _draft(
        self, run: _Run, messages: list[dict[str, Any]], ctx: DraftContext
    ) -> AgentInvestigationDraft:
        schema = AgentInvestigationDraft.model_json_schema()
        messages.append({"role": "user", "content": prompts.drafting_instruction(run.exception_id)})
        last: tuple[FailureCategory, str] = (FailureCategory.INVALID_OUTPUT, "no draft produced")
        for _ in range(self.limits.max_draft_attempts):
            reply = await self._chat(messages, response_schema=schema)
            try:
                draft = AgentInvestigationDraft.model_validate_json(reply.content)
            except ValidationError as exc:
                errors = [f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()]
                last = (FailureCategory.INVALID_OUTPUT, "; ".join(errors)[:500])
            else:
                problems = validate_draft(draft, ctx, run.seen_evidence_ids)
                if not problems:
                    return draft
                errors = [msg for _, msg in problems]
                last = (problems[0][0], "; ".join(errors)[:500])
            messages.append({"role": "assistant", "content": reply.content[:4000]})
            messages.append({"role": "user", "content": prompts.retry_instruction(errors)})
        raise _Stop(*last)

    async def _chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ModelReply:
        return await anyio.to_thread.run_sync(lambda: self.model.chat(messages, **kwargs))

    # ------------------------------------------------------------------ persistence

    def _finish(
        self,
        run: _Run,
        model_version: str,
        category: FailureCategory | None,
        detail: str | None,
        draft: AgentInvestigationDraft | None = None,
    ) -> InvestigationOutcome:
        status = InvestigationStatus.DRAFT_READY if draft else InvestigationStatus.MANUAL_REVIEW
        duration_ms = int((time.perf_counter() - run.started) * 1000)
        draft_ref: tuple[str, int, str] | None = None
        exists = repo.exception_details(self.conn, run.exception_id) is not None
        with transaction(self.conn):
            run_id = repo.insert_agent_run(
                self.conn,
                exception_id=run.exception_id if exists else None,
                batch_id=run.batch_id,
                agent_version=__version__,
                model_version=model_version,
                prompt_version=prompts.PROMPT_VERSION,
            )
            for c in run.calls:
                repo.insert_tool_event(
                    self.conn,
                    run_id,
                    sequence=c.sequence,
                    actor=c.actor,
                    tool_name=c.tool_name,
                    arguments=c.arguments,
                    result_hash=c.result_hash,
                    duration_ms=c.duration_ms,
                    success=c.success,
                    error_category=c.error_category,
                )
            repo.finish_agent_run(
                self.conn,
                run_id,
                status=status.value,
                error_category=category.value if category else None,
                error_detail=detail,
                tool_call_count=run.model_call_count,
                duration_ms=duration_ms,
            )
            if exists:
                if draft is not None:
                    draft_ref = repo.insert_draft(
                        self.conn,
                        exception_id=run.exception_id,
                        run_id=run_id,
                        author_id=AGENT_PRINCIPAL,
                        draft=draft.model_dump(mode="json"),
                    )
                repo.set_exception_status(
                    self.conn,
                    run.exception_id,
                    ExceptionStatus.UNDER_INVESTIGATION if draft else ExceptionStatus.MANUAL_REVIEW,
                )
            append_event(
                self.conn,
                actor_id=AGENT_PRINCIPAL,
                event_type=AuditEventType.INVESTIGATION_COMPLETED,
                object_id=run.exception_id,
                payload={
                    "run_id": run_id,
                    "status": status.value,
                    "failure_category": category.value if category else None,
                    "draft_id": draft_ref[0] if draft_ref else None,
                    "content_hash": draft_ref[2] if draft_ref else None,
                    "model_version": model_version,
                    "prompt_version": prompts.PROMPT_VERSION,
                },
            )
        log.info(
            "investigation_finished run_id=%s exception_id=%s status=%s category=%s "
            "model_tool_calls=%d duration_ms=%d",
            run_id,
            run.exception_id,
            status.value,
            category,
            run.model_call_count,
            duration_ms,
        )
        return InvestigationOutcome(
            run_id=run_id,
            exception_id=run.exception_id,
            status=status,
            failure_category=category,
            failure_detail=detail,
            model_version=model_version,
            draft=draft,
            draft_id=draft_ref[0] if draft_ref else None,
            draft_version=draft_ref[1] if draft_ref else None,
            content_hash=draft_ref[2] if draft_ref else None,
            tool_calls=run.calls,
            duration_ms=duration_ms,
        )


# ------------------------------------------------------------------ application entry point


def server_parameters(settings: Settings) -> StdioServerParameters:
    """Launch our own reviewed server as a subprocess: list args (no shell), no extra secrets.

    The SDK passes only HOME, LOGNAME, PATH, SHELL and USER from the parent environment.
    """
    return StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "payops_sentinel.mcp_server.server",
            "--db",
            str(settings.db_path),
            "--manifest",
            str(settings.agent.manifest_path),
        ],
        env={},
        cwd=str(PROJECT_ROOT),
    )


def run_investigation(
    settings: Settings,
    exception_id: str,
    *,
    model: ChatModel | None = None,
    server: StdioServerParameters | None = None,
) -> InvestigationOutcome:
    """Synchronous entry point used by the CLI and the UI."""
    conn = connect(settings.db_path)
    initialize(conn)
    orchestrator = InvestigationOrchestrator(
        model or OllamaChatModel(settings.agent), AgentLimits.from_settings(settings.agent), conn
    )
    params = server or server_parameters(settings)

    async def _run() -> InvestigationOutcome:
        outcome: InvestigationOutcome | None = None
        try:
            async with Client(params, read_timeout_seconds=settings.agent.tool_timeout_s) as client:
                outcome = await orchestrator.investigate(client, exception_id)
        except Exception as exc:  # the server process failed to start or crashed
            if outcome is not None:
                return outcome
            log.warning(
                "mcp_unavailable exception_id=%s error=%s", exception_id, type(exc).__name__
            )
            return orchestrator.fail_closed(
                exception_id, FailureCategory.MCP_UNAVAILABLE, type(exc).__name__
            )
        return outcome

    try:
        return anyio.run(_run)
    finally:
        conn.close()
