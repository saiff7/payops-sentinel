"""Restricted data never enters the system, the model prompt, telemetry or artifacts."""

from __future__ import annotations

import json

import anyio
import pytest
from mcp import Client

from payops_sentinel.agent.guardrails import AgentLimits
from payops_sentinel.agent.ollama_client import ModelReply, ToolCallRequest
from payops_sentinel.agent.orchestrator import InvestigationOrchestrator
from payops_sentinel.domain.enums import FileRejectionCode
from payops_sentinel.domain.models import FileRejectedError
from payops_sentinel.ingestion.service import ingest_payroll_file
from payops_sentinel.mcp_server.server import build_server
from payops_sentinel.redaction import find_restricted
from payops_sentinel.review import service
from payops_sentinel.review.service import DraftEdit, InvalidDraftError, resolve_principal
from payops_sentinel.storage import repositories as repo


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("SSN 123-45-6789", "ssn"),
        ("write to jane.doe@example.com", "email"),
        ("call (555) 123-4567", "phone"),
        ("card 4111 1111 1111 1111", "card_or_account_number"),
        ("routing 021000021", "routing_number_label"),
    ],
)
def test_restricted_patterns_detected(text, category):
    assert category in find_restricted(text)


@pytest.mark.parametrize(
    "text",
    ["EMP-0017 contribution $200.00 for 2026-10-31", "EV-000016, EXC-000004, BATCH-000001"],
)
def test_normal_business_text_not_flagged(text):
    assert find_restricted(text) == []


@pytest.mark.parametrize(
    "cell_row",
    [
        ("123-45-6789", "4000.00", "0.00"),
        ("EMP-0017", "jane@example.com", "0.00"),
        ("EMP-0017", "4000.00", "555-123-4567"),
    ],
)
def test_upload_with_restricted_cell_content_rejected(conn, settings, make_csv, row, cell_row):
    with pytest.raises(FileRejectedError) as exc:
        ingest_payroll_file(conn, settings, content=make_csv(row(*cell_row)), file_name="x.csv")
    assert exc.value.code is FileRejectionCode.RESTRICTED_CONTENT
    assert "@" not in exc.value.message and "6789" not in exc.value.message
    assert repo.count_rows(conn, "payroll_rows") == 0


def test_reviewer_cannot_add_restricted_data_to_a_draft(conn, pending_draft):
    edit = DraftEdit(
        email_subject="Contribution record requiring review",
        email_body="Please reply to payroll.lead@northstar.example with the SSN "
        "123-45-6789 so we can match the record.",
    )
    with pytest.raises(InvalidDraftError, match="restricted"):
        service.edit_draft(conn, resolve_principal("reviewer.demo"), edit=edit, **pending_draft)
    assert repo.latest_draft_version(conn, pending_draft["draft_id"])["draft_version"] == 1


class _ScriptedModel:
    model_name = "fake"

    def __init__(self, exception_id):
        self.messages: list[dict] = []
        self.script = [
            ModelReply(
                "", (ToolCallRequest("get_exception_details", {"exception_id": exception_id}),)
            ),
            ModelReply(
                "", (ToolCallRequest("get_exception_evidence", {"exception_id": exception_id}),)
            ),
            ModelReply("done"),
            ModelReply("{}"),
            ModelReply("{}"),
        ]

    def describe(self):
        return "fake@0"

    def chat(self, messages, **_):
        self.messages = [dict(m) for m in messages]
        return self.script.pop(0)


def test_raw_upload_values_never_reach_the_model(ingested, conn, settings, manifest_path):
    """The invalid EMP-17A row is investigated, but only sanitised evidence is shown."""
    exc = next(
        e
        for e in repo.list_exceptions(conn, ingested.batch_id)
        if e["reason_code"] == "MALFORMED_EMPLOYEE_REF"
    )
    model = _ScriptedModel(exc["exception_id"])
    orch = InvestigationOrchestrator(model, AgentLimits.from_settings(settings.agent), conn)

    async def go():
        async with Client(build_server(settings.db_path, manifest_path)) as c:
            return await orch.investigate(c, exc["exception_id"])

    anyio.run(go)
    prompt = json.dumps(model.messages)
    assert "EMP-17A" not in prompt and "4100.00" not in prompt
    assert exc["exception_id"] in prompt


def test_tool_telemetry_contains_identifiers_not_amounts(ingested, conn, settings, manifest_path):
    model = _ScriptedModel("EXC-000004")
    orch = InvestigationOrchestrator(model, AgentLimits.from_settings(settings.agent), conn)

    async def go():
        async with Client(build_server(settings.db_path, manifest_path)) as c:
            return await orch.investigate(c, "EXC-000004")

    outcome = anyio.run(go)
    for event in repo.list_tool_events(conn, outcome.run_id):
        args = json.loads(event["arguments_json"])
        assert set(args) <= {
            "exception_id",
            "batch_id",
            "plan_id",
            "rule_code",
            "as_of_date",
            "limit",
            "exception_type",
            "status",
            "ruleset_version",
            "asset_id",
        }
        assert "20000" not in event["arguments_json"]
