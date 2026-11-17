"""Agent loop, guardrails and fail-closed behaviour.

A scripted fake replaces only the model; tool calls go through the real MCP client and server.
"""

from __future__ import annotations

import json
import sys
import tomllib
from dataclasses import replace

import anyio
import pytest
from mcp import Client, StdioServerParameters
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from payops_sentinel.agent.guardrails import ALLOWED_TOOLS, AgentLimits
from payops_sentinel.agent.ollama_client import ModelReply, ModelUnavailableError, ToolCallRequest
from payops_sentinel.agent.orchestrator import InvestigationOrchestrator, run_investigation
from payops_sentinel.agent.schemas import FailureCategory, InvestigationStatus
from payops_sentinel.config import PROJECT_ROOT, AgentSettings
from payops_sentinel.hashing import canonical_json, sha256_hex
from payops_sentinel.mcp_server.server import build_server
from payops_sentinel.storage import repositories as repo

EXC = "EXC-000004"  # EMP-0017, missing $200.00 contribution
RULE_ARGS = {
    "plan_id": "PLAN-401K-A",
    "rule_code": "EMPLOYEE_DEFERRAL_PCT",
    "as_of_date": "2026-10-31",
}


class FakeModel:
    model_name = "fake:test"

    def __init__(self, *script, unavailable: bool = False):
        self.script = list(script)
        self.calls: list[dict] = []
        self.unavailable = unavailable

    def describe(self) -> str:
        if self.unavailable:
            raise ModelUnavailableError("Ollama not reachable: ConnectError")
        return "fake:test@000000000000"

    def chat(self, messages, *, tools=None, response_schema=None):
        self.calls.append(
            {"messages": [dict(m) for m in messages], "tools": tools, "schema": response_schema}
        )
        step = self.script.pop(0) if self.script else ModelReply("")
        if isinstance(step, Exception):
            raise step
        return step


def tool(name: str, **args) -> ModelReply:
    return ModelReply("", (ToolCallRequest(name, args),))


def text(content: str) -> ModelReply:
    return ModelReply(content)


GATHER = (
    tool("get_exception_details", exception_id=EXC),
    tool("get_exception_evidence", exception_id=EXC),
    tool("lookup_plan_rule", **RULE_ARGS),
    text("I have the evidence."),
)


def draft_json(evidence_ids, **overrides) -> ModelReply:
    body = {
        "exception_id": EXC,
        "investigation_summary": "The engine found a blank or zero contribution for EMP-0017.",
        "evidence_ids": evidence_ids,
        "reasoning_summary": "Payroll row shows $0.00 while the calculation expected $200.00.",
        "proposed_next_step": "REQUEST_CORRECTED_RECORD",
        "email_subject": "Contribution record requiring review",
        "email_body": "Hello, please verify the October 31, 2026 entry for employee reference "
        "EMP-0017. The reported contribution was $0.00. Thank you.",
        "limitations": [],
        "requires_human_review": True,
    } | overrides
    return text(json.dumps(body))


@pytest.fixture
def evidence_ids(ingested, conn):
    return repo.exception_details(conn, EXC)["evidence_ids"]


@pytest.fixture
def investigate(ingested, conn, settings, manifest_path):
    def _run(model, exception_id=EXC, limits=None, server=None):
        orch = InvestigationOrchestrator(
            model, limits or AgentLimits.from_settings(settings.agent), conn
        )

        async def go():
            async with Client(server or build_server(settings.db_path, manifest_path)) as c:
                return await orch.investigate(c, exception_id)

        return anyio.run(go)

    return _run


def _model_calls(outcome):
    return [c for c in outcome.tool_calls if c.actor == "model"]


# ------------------------------------------------------------------ happy path


def test_full_investigation_produces_validated_pending_draft(investigate, conn, evidence_ids):
    model = FakeModel(*GATHER, draft_json(evidence_ids))
    out = investigate(model)

    assert out.status is InvestigationStatus.DRAFT_READY and out.failure_category is None
    assert [c.tool_name for c in _model_calls(out)] == [
        "get_exception_details",
        "get_exception_evidence",
        "lookup_plan_rule",
    ]
    assert all(c.success for c in out.tool_calls)
    # the model was offered exactly the allowlisted tools, and the final call was schema-bound
    assert [t["function"]["name"] for t in model.calls[0]["tools"]] == list(ALLOWED_TOOLS)
    assert model.calls[-1]["tools"] is None and model.calls[-1]["schema"]["title"] == (
        "AgentInvestigationDraft"
    )
    # tool results were returned to the model for the next reasoning step
    second_turn = model.calls[1]["messages"]
    assert second_turn[-1]["role"] == "tool" and "EMP-0017" in second_turn[-1]["content"]

    drafts = repo.list_drafts(conn, EXC)
    assert len(drafts) == 1 and drafts[0]["status"] == "pending_review"
    assert drafts[0]["author_id"] == "agent_service"
    assert drafts[0]["content_hash"] == sha256_hex(drafts[0]["draft_json"]) == out.content_hash
    assert repo.exception_details(conn, EXC)["status"] == "under_investigation"
    run = repo.get_agent_run(conn, out.run_id)
    assert (run["status"], run["tool_call_count"], run["prompt_version"]) == (
        "draft_ready",
        3,
        "1.0.0",
    )
    events = repo.list_tool_events(conn, out.run_id)
    assert [e["actor"] for e in events] == ["host", "host", "model", "model", "model"]


def test_invalid_draft_is_retried_once_then_accepted(investigate, evidence_ids):
    model = FakeModel(*GATHER, text("{not json"), draft_json(evidence_ids))
    out = investigate(model)
    assert out.status is InvestigationStatus.DRAFT_READY
    assert "rejected" in model.calls[-1]["messages"][-1]["content"]


# ------------------------------------------------------------------ tool guardrails


def test_unknown_tool_rejected_and_never_executed(investigate, conn, evidence_ids):
    model = FakeModel(
        tool("approve_exception", exception_id=EXC), *GATHER, draft_json(evidence_ids)
    )
    out = investigate(model)
    rejected = _model_calls(out)[0]
    assert (rejected.tool_name, rejected.success) == ("approve_exception", False)
    assert rejected.error_category == "REJECTED_NOT_ALLOWLISTED"
    assert "not permitted" in model.calls[1]["messages"][-1]["content"]
    assert out.status is InvestigationStatus.DRAFT_READY
    assert repo.list_drafts(conn, EXC)[0]["status"] == "pending_review"  # never approved


@pytest.mark.parametrize(
    "args",
    [{"exception_id": "EXC-4"}, {"exception_id": EXC, "extra": "x"}, {"exception_id": 7}, {}],
)
def test_invalid_arguments_rejected_before_execution(investigate, evidence_ids, args):
    model = FakeModel(tool("get_exception_details", **args), *GATHER, draft_json(evidence_ids))
    out = investigate(model)
    assert _model_calls(out)[0].error_category == "REJECTED_INVALID_ARGUMENTS"


def test_repeated_identical_failed_call_stops(investigate, conn):
    bad = tool("get_exception_details", exception_id="EXC-999999")
    out = investigate(FakeModel(bad, bad, bad))
    assert out.status is InvestigationStatus.MANUAL_REVIEW
    assert out.failure_category is FailureCategory.REPEATED_FAILED_CALL
    assert repo.list_drafts(conn, EXC) == []
    assert repo.exception_details(conn, EXC)["status"] == "manual_review"


def test_iteration_limit_enforced(investigate, settings, evidence_ids):
    limits = replace(AgentLimits.from_settings(settings.agent), max_iterations=2)
    loop = [tool("get_exception_details", exception_id=EXC)] * 6
    model = FakeModel(tool("get_exception_evidence", exception_id=EXC), *loop)
    out = investigate(model, limits=limits)
    assert len(_model_calls(out)) == 2
    assert model.calls[2]["schema"] is not None  # third model call is the drafting call


def test_tool_call_limit_enforced(investigate, settings):
    limits = replace(AgentLimits.from_settings(settings.agent), max_tool_calls=3)
    burst = ModelReply(
        "", tuple(ToolCallRequest("get_exception_details", {"exception_id": EXC}) for _ in range(6))
    )
    model = FakeModel(burst)
    out = investigate(model, limits=limits)
    assert len(_model_calls(out)) == 3
    tool_msgs = [m for m in model.calls[1]["messages"] if m["role"] == "tool"]
    assert sum("limit reached" in m["content"] for m in tool_msgs) == 3


def test_server_exposing_extra_tools_is_filtered(
    ingested, investigate, settings, manifest_path, evidence_ids
):
    server = build_server(settings.db_path, manifest_path)

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def delete_records() -> str:
        """Should never be offered to the model."""
        return "deleted"

    model = FakeModel(*GATHER, draft_json(evidence_ids))
    out = investigate(model, server=server)
    assert "delete_records" not in [t["function"]["name"] for t in model.calls[0]["tools"]]
    assert out.status is InvestigationStatus.DRAFT_READY


def test_server_missing_allowlisted_tool_fails_closed(investigate):
    partial = MCPServer("partial")

    @partial.tool(annotations=ToolAnnotations(read_only_hint=True))
    def get_exception_details(exception_id: str) -> dict:
        """Partial server."""
        return {"exception_id": exception_id, "evidence_ids": []}

    out = investigate(FakeModel(), server=partial)
    assert out.status is InvestigationStatus.MANUAL_REVIEW
    assert out.failure_category is FailureCategory.MCP_CONTRACT_MISMATCH


# ------------------------------------------------------------------ output guardrails


def test_foreign_evidence_ids_rejected(investigate, conn, evidence_ids):
    foreign = repo.exception_details(conn, "EXC-000001")["evidence_ids"][0]
    bad = draft_json([*evidence_ids, foreign])
    out = investigate(FakeModel(*GATHER, bad, bad))
    assert out.failure_category is FailureCategory.EVIDENCE_MISMATCH
    assert repo.list_drafts(conn, EXC) == []


def test_evidence_cited_but_never_retrieved_rejected(investigate, evidence_ids):
    skip_evidence = (tool("get_exception_details", exception_id=EXC), text("done"))
    bad = draft_json([evidence_ids[0]])
    out = investigate(FakeModel(*skip_evidence, bad, bad))
    # details lists evidence IDs, so they were retrieved; cite one that is only in evidence text
    assert out.status is InvestigationStatus.DRAFT_READY
    out = investigate(FakeModel(text("no tools"), bad, bad))
    assert out.failure_category is FailureCategory.EVIDENCE_MISMATCH


def test_requires_human_review_must_be_true(investigate, evidence_ids):
    bad = draft_json(evidence_ids, requires_human_review=False)
    out = investigate(FakeModel(*GATHER, bad, bad))
    assert out.failure_category is FailureCategory.INVALID_OUTPUT


def test_confidence_scores_and_extra_fields_rejected(investigate, evidence_ids):
    bad = draft_json(evidence_ids, confidence=0.97)
    out = investigate(FakeModel(*GATHER, bad, bad))
    assert out.failure_category is FailureCategory.INVALID_OUTPUT


@pytest.mark.parametrize(
    "overrides",
    [
        {"email_body": "Please correct EMP-0017; the shortfall is $187.42 for October 2026."},
        {"email_body": "Please verify EMP-0017 and EMP-0005 for the October 2026 period, thanks."},
    ],
)
def test_unsupported_amounts_or_people_rejected(investigate, evidence_ids, overrides):
    bad = draft_json(evidence_ids, **overrides)
    out = investigate(FakeModel(*GATHER, bad, bad))
    assert out.failure_category is FailureCategory.UNSUPPORTED_CONTENT


def test_invalid_json_after_all_attempts_goes_to_manual_review(investigate, conn):
    out = investigate(FakeModel(*GATHER, text("nope"), text("[]")))
    assert out.failure_category is FailureCategory.INVALID_OUTPUT
    assert repo.get_agent_run(conn, out.run_id)["status"] == "manual_review"


# ------------------------------------------------------------------ infrastructure failures


def test_ollama_unavailable_falls_back_to_manual_review(investigate, conn):
    out = investigate(FakeModel(unavailable=True))
    assert out.failure_category is FailureCategory.MODEL_UNAVAILABLE
    assert repo.exception_details(conn, EXC)["status"] == "manual_review"


def test_model_failure_mid_loop_falls_back(investigate):
    model = FakeModel(GATHER[0], ModelUnavailableError("model request failed: ReadTimeout"))
    out = investigate(model)
    assert out.failure_category is FailureCategory.MODEL_UNAVAILABLE


def test_unknown_exception_fails_closed(investigate, conn):
    out = investigate(FakeModel(), exception_id="EXC-999999")
    assert out.failure_category is FailureCategory.EXCEPTION_NOT_FOUND
    assert repo.get_agent_run(conn, out.run_id)["exception_id"] is None


def test_mcp_process_failure_preserves_deterministic_results(ingested, conn, settings):
    before = repo.list_exceptions(conn, ingested.batch_id)
    crash = StdioServerParameters(command=sys.executable, args=["-c", "import sys; sys.exit(3)"])
    out = run_investigation(settings, EXC, model=FakeModel(*GATHER), server=crash)
    assert out.status is InvestigationStatus.MANUAL_REVIEW
    assert out.failure_category is FailureCategory.MCP_UNAVAILABLE
    after = repo.list_exceptions(conn, ingested.batch_id)
    assert [(e["exception_id"], e["expected_cents"]) for e in after] == [
        (e["exception_id"], e["expected_cents"]) for e in before
    ]


def test_end_to_end_over_separate_stdio_process(ingested, conn, settings, evidence_ids):
    out = run_investigation(settings, EXC, model=FakeModel(*GATHER, draft_json(evidence_ids)))
    assert out.status is InvestigationStatus.DRAFT_READY
    assert len(_model_calls(out)) == 3


# ------------------------------------------------------------------ governance and configuration


def test_enforcing_allowlist_matches_governance_manifest():
    with (PROJECT_ROOT / "governance" / "tool_permissions.toml").open("rb") as fh:
        manifest = tomllib.load(fh)["principal"]["agent_service"]
    assert manifest["tools"] == list(ALLOWED_TOOLS)
    assert not any(
        manifest[k] for k in manifest if k.startswith("can_") and k != "can_generate_drafts"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model", "qwen3:8b-cloud"),
        ("model", "gpt-oss:120b-cloud"),
        ("ollama_host", "https://ollama.com"),
        ("ollama_host", "http://10.0.0.5:11434"),
    ],
)
def test_configuration_rejects_cloud_models_and_remote_hosts(field, value):
    with pytest.raises(ValueError, match="local"):
        AgentSettings(**{field: value})


def test_draft_content_hash_is_canonical(investigate, conn, evidence_ids):
    out = investigate(FakeModel(*GATHER, draft_json(evidence_ids)))
    assert out.content_hash == sha256_hex(canonical_json(out.draft.model_dump(mode="json")))
