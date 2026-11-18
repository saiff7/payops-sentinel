"""Structured agent output and investigation outcome models."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from payops_sentinel.domain.models import EVIDENCE_ID_PATTERN, EXCEPTION_ID_PATTERN

EvidenceId = Annotated[str, Field(pattern=EVIDENCE_ID_PATTERN)]


class ProposedNextStep(StrEnum):
    REQUEST_CLARIFICATION = "REQUEST_CLARIFICATION"
    REQUEST_CORRECTED_RECORD = "REQUEST_CORRECTED_RECORD"
    VERIFY_TERMINATION_STATUS = "VERIFY_TERMINATION_STATUS"
    ESCALATE_INTERNALLY = "ESCALATE_INTERNALLY"


class AgentInvestigationDraft(BaseModel):
    """The only shape of agent output the application accepts. No confidence score by design."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    exception_id: str = Field(pattern=EXCEPTION_ID_PATTERN)
    investigation_summary: str = Field(min_length=20, max_length=1000)
    evidence_ids: list[EvidenceId] = Field(min_length=1, max_length=20)
    reasoning_summary: str = Field(min_length=20, max_length=1500)
    proposed_next_step: ProposedNextStep
    email_subject: str = Field(min_length=5, max_length=150)
    email_body: str = Field(min_length=40, max_length=3000)
    limitations: list[Annotated[str, Field(max_length=300)]] = Field(max_length=5)
    requires_human_review: Literal[True]


class InvestigationStatus(StrEnum):
    DRAFT_READY = "draft_ready"
    MANUAL_REVIEW = "manual_review"


class FailureCategory(StrEnum):
    EXCEPTION_NOT_FOUND = "EXCEPTION_NOT_FOUND"
    MCP_UNAVAILABLE = "MCP_UNAVAILABLE"
    MCP_CONTRACT_MISMATCH = "MCP_CONTRACT_MISMATCH"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    REPEATED_FAILED_CALL = "REPEATED_FAILED_CALL"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    EVIDENCE_MISMATCH = "EVIDENCE_MISMATCH"
    UNSUPPORTED_CONTENT = "UNSUPPORTED_CONTENT"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    AGENT_DISABLED = "AGENT_DISABLED"


class ToolCallRecord(BaseModel):
    model_config = ConfigDict(frozen=True)

    sequence: int
    actor: Literal["model", "host"]
    tool_name: str
    arguments: dict[str, Any]
    success: bool
    error_category: str | None = None
    result_hash: str | None = None
    duration_ms: int = 0


class InvestigationOutcome(BaseModel):
    model_config = ConfigDict(frozen=True)

    run_id: str
    exception_id: str
    status: InvestigationStatus
    failure_category: FailureCategory | None = None
    failure_detail: str | None = None
    model_version: str
    draft: AgentInvestigationDraft | None = None
    draft_id: str | None = None
    draft_version: int | None = None
    content_hash: str | None = None
    tool_calls: list[ToolCallRecord] = []
    prefetched_tools: list[str] = []  # host-supplied results given to the model
    model_turns: int = 0
    draft_attempts: int = 0
    duration_ms: int = 0
