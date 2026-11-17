"""Host-side enforcement: static tool allowlist, argument validation, limits, output checks.

These checks do not trust the MCP server's own schemas or annotations; they are an independent
layer in the agent host.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Annotated, Any

from mcp.types import Tool
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from payops_sentinel.agent.schemas import AgentInvestigationDraft, FailureCategory
from payops_sentinel.config import AgentSettings
from payops_sentinel.domain.enums import ExceptionStatus, ExceptionType
from payops_sentinel.domain.models import (
    BATCH_ID_PATTERN,
    CODE_PATTERN,
    EMPLOYEE_REF_PATTERN,
    EVIDENCE_ID_PATTERN,
    EXCEPTION_ID_PATTERN,
)

AGENT_PRINCIPAL = "agent_service"


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BatchArgs(_Args):
    batch_id: Annotated[str, Field(pattern=BATCH_ID_PATTERN)]


class ListExceptionsArgs(BatchArgs):
    exception_type: ExceptionType | None = None
    status: ExceptionStatus | None = None
    limit: Annotated[int, Field(ge=1, le=25)] = 10


class ExceptionArgs(_Args):
    exception_id: Annotated[str, Field(pattern=EXCEPTION_ID_PATTERN)]


class RuleArgs(_Args):
    plan_id: Annotated[str, Field(pattern=CODE_PATTERN)]
    rule_code: Annotated[str, Field(pattern=CODE_PATTERN)]
    as_of_date: date
    ruleset_version: Annotated[str, Field(max_length=40)] | None = None


class AssetArgs(_Args):
    asset_id: Annotated[str, Field(pattern=CODE_PATTERN)]


# The enforcing allowlist. Every entry is read-only. Nothing else may ever be called.
ALLOWED_TOOLS: dict[str, type[_Args]] = {
    "get_batch_summary": BatchArgs,
    "list_exceptions": ListExceptionsArgs,
    "get_exception_details": ExceptionArgs,
    "get_exception_evidence": ExceptionArgs,
    "lookup_plan_rule": RuleArgs,
    "get_agent_asset": AssetArgs,
}


class GuardrailViolation(Exception):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
        self.message = message


@dataclass(frozen=True)
class AgentLimits:
    max_iterations: int
    max_tool_calls: int
    max_result_chars: int
    tool_timeout_s: float
    max_draft_attempts: int

    @classmethod
    def from_settings(cls, s: AgentSettings) -> AgentLimits:
        return cls(
            max_iterations=s.max_iterations,
            max_tool_calls=s.max_tool_calls,
            max_result_chars=s.max_result_chars,
            tool_timeout_s=s.tool_timeout_s,
            max_draft_attempts=s.max_draft_attempts,
        )


# ------------------------------------------------------------------ tool catalog and calls


def check_tool_catalog(discovered: Sequence[Tool]) -> list[Tool]:
    """Expose only allowlisted tools; every allowlisted tool must be present and read-only."""
    by_name = {t.name: t for t in discovered}
    missing = sorted(set(ALLOWED_TOOLS) - set(by_name))
    if missing:
        raise GuardrailViolation(
            FailureCategory.MCP_CONTRACT_MISMATCH, f"allowlisted tools missing: {missing}"
        )
    exposed = [by_name[name] for name in ALLOWED_TOOLS]
    for tool in exposed:
        if tool.annotations is None or tool.annotations.read_only_hint is not True:
            raise GuardrailViolation(
                FailureCategory.MCP_CONTRACT_MISMATCH, f"{tool.name} is not declared read-only"
            )
    return exposed


def to_ollama_tools(tools: Iterable[Tool]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.input_schema,
            },
        }
        for t in tools
    ]


def validate_tool_call(name: str, arguments: Any) -> dict[str, Any]:
    """Reject unknown tools and invalid/extra arguments before anything is executed."""
    model = ALLOWED_TOOLS.get(name)
    if model is None:
        raise GuardrailViolation("REJECTED_NOT_ALLOWLISTED", f"tool '{name[:60]}' is not permitted")
    if not isinstance(arguments, dict):
        raise GuardrailViolation("REJECTED_INVALID_ARGUMENTS", "arguments must be an object")
    try:
        validated = model.model_validate(arguments)
    except ValidationError as exc:
        fields = sorted({".".join(map(str, e["loc"])) or "?" for e in exc.errors()})
        raise GuardrailViolation(
            "REJECTED_INVALID_ARGUMENTS", f"invalid arguments for {name}: {fields}"
        ) from exc
    return validated.model_dump(mode="json", exclude_none=True)


# ------------------------------------------------------------------ draft checks

_MONEY_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?")
_EMP_RE = re.compile(EMPLOYEE_REF_PATTERN.strip("^$"))
_EV_RE = re.compile(EVIDENCE_ID_PATTERN.strip("^$"))


def find_evidence_ids(value: Any) -> set[str]:
    """Every EV-###### identifier appearing anywhere in a (nested) tool result."""
    if isinstance(value, str):
        return set(_EV_RE.findall(value))
    if isinstance(value, dict):
        return set().union(*(find_evidence_ids(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(find_evidence_ids(v) for v in value)) if value else set()
    return set()


def mentioned_amounts_cents(text: str) -> set[int]:
    amounts = set()
    for whole, frac in _MONEY_RE.findall(text):
        value = Decimal(whole.replace(",", "")) + Decimal(f"0.{frac or '0'}")
        amounts.add(int(value * 100))
    return amounts


@dataclass(frozen=True)
class DraftContext:
    """Ground truth fetched by the host (not the model) for verifying a draft."""

    exception_id: str
    employee_ref: str | None
    evidence_ids: frozenset[str]
    allowed_amounts_cents: frozenset[int]


def validate_draft(
    draft: AgentInvestigationDraft, ctx: DraftContext, seen_evidence_ids: set[str]
) -> list[tuple[FailureCategory, str]]:
    problems: list[tuple[FailureCategory, str]] = []
    if draft.exception_id != ctx.exception_id:
        problems.append((FailureCategory.EVIDENCE_MISMATCH, "exception_id does not match"))
    foreign = sorted(set(draft.evidence_ids) - ctx.evidence_ids)
    if foreign:
        problems.append(
            (FailureCategory.EVIDENCE_MISMATCH, f"evidence not belonging to exception: {foreign}")
        )
    unseen = sorted((set(draft.evidence_ids) & ctx.evidence_ids) - seen_evidence_ids)
    if unseen:
        problems.append(
            (FailureCategory.EVIDENCE_MISMATCH, f"evidence cited but never retrieved: {unseen}")
        )
    text = " ".join(
        [
            draft.investigation_summary,
            draft.reasoning_summary,
            draft.email_subject,
            draft.email_body,
            *draft.limitations,
        ]
    )
    unsupported = sorted(mentioned_amounts_cents(text) - ctx.allowed_amounts_cents)
    if unsupported:
        shown = [f"${c / 100:,.2f}" for c in unsupported]
        problems.append((FailureCategory.UNSUPPORTED_CONTENT, f"amounts not in evidence: {shown}"))
    other_refs = sorted(set(_EMP_RE.findall(text)) - {ctx.employee_ref})
    if other_refs:
        problems.append(
            (FailureCategory.UNSUPPORTED_CONTENT, f"mentions other employee refs: {other_refs}")
        )
    return problems


def allowed_amounts(details: dict[str, Any], evidence: dict[str, Any]) -> frozenset[int]:
    """Amounts the deterministic engine produced or recorded for this exception."""
    values: set[int] = set()
    for key in ("expected_contribution_cents", "recorded_contribution_cents"):
        if isinstance(details.get(key), int):
            values.add(details[key])
    for item in evidence.get("evidence", []):
        for key, v in item.get("snapshot", {}).items():
            if key.endswith("_cents") and isinstance(v, int):
                values.add(abs(v))
    return frozenset(values)
