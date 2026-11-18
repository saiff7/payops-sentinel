"""The six read-only MCP tools, via the real MCP client (in-process and over stdio)."""

from __future__ import annotations

import sys

import anyio
import pytest
from mcp import Client, StdioServerParameters

from payops_sentinel.agent.guardrails import ALLOWED_TOOLS
from payops_sentinel.hashing import sha256_hex
from payops_sentinel.mcp_server.server import build_server

FORBIDDEN_WORDS = ("approve", "send", "email", "delete", "update", "change", "write", "insert")


def _call(settings, manifest_path, name, args):
    async def go():
        async with Client(build_server(settings.db_path, manifest_path)) as c:
            return await c.call_tool(name, args)

    return anyio.run(go)


def _tools(settings, manifest_path):
    async def go():
        async with Client(build_server(settings.db_path, manifest_path)) as c:
            return (await c.list_tools()).tools

    return anyio.run(go)


def test_exactly_six_read_only_tools(ingested, settings, manifest_path):
    tools = _tools(settings, manifest_path)
    assert [t.name for t in tools] == list(ALLOWED_TOOLS)
    for t in tools:
        assert t.annotations.read_only_hint is True
        assert t.annotations.destructive_hint is False
        assert t.annotations.open_world_hint is False
        assert not any(w in t.name for w in FORBIDDEN_WORDS)


def test_get_batch_summary(ingested, settings, manifest_path):
    r = _call(settings, manifest_path, "get_batch_summary", {"batch_id": ingested.batch_id})
    assert not r.is_error
    s = r.structured_content
    assert (s["total_rows"], s["invalid_rows"], s["exceptions"]) == (43, 6, 12)
    assert s["pay_period_end"] == "2026-10-31"
    assert s["data_classification"] == "SYNTHETIC_DEMO"


def test_list_exceptions_with_filter(ingested, settings, manifest_path):
    args = {"batch_id": ingested.batch_id, "exception_type": "MISSING_CONTRIBUTION", "limit": 25}
    r = _call(settings, manifest_path, "list_exceptions", args)
    items = r.structured_content["items"]
    assert {i["employee_ref"] for i in items} == {"EMP-0005", "EMP-0017"}
    assert r.structured_content["count"] == 2


def test_get_exception_details_and_evidence(ingested, settings, manifest_path):
    d = _call(settings, manifest_path, "get_exception_details", {"exception_id": "EXC-000004"})
    det = d.structured_content
    assert det["employee_ref"] == "EMP-0017"
    assert (det["expected_contribution_cents"], det["recorded_contribution_cents"]) == (20_000, 0)
    assert det["plan_id"] == "PLAN-401K-A" and det["rule_code"] == "EMPLOYEE_DEFERRAL_PCT"
    assert len(det["evidence_ids"]) == 5

    e = _call(settings, manifest_path, "get_exception_evidence", {"exception_id": "EXC-000004"})
    evidence = e.structured_content["evidence"]
    assert [x["evidence_id"] for x in evidence] == det["evidence_ids"]
    for x in evidence:
        from payops_sentinel.hashing import canonical_json

        assert sha256_hex(canonical_json(x["snapshot"])) == x["content_hash"]


def test_lookup_plan_rule(ingested, settings, manifest_path):
    args = {
        "plan_id": "PLAN-401K-A",
        "rule_code": "EMPLOYEE_DEFERRAL_PCT",
        "as_of_date": "2026-10-31",
    }
    r = _call(settings, manifest_path, "lookup_plan_rule", args)
    assert r.structured_content["rule_id"] == "RULE-DEFERRAL-PCT-V1"
    assert r.structured_content["configured_rule"] == {
        "rounding": "ROUND_HALF_UP",
        "tolerance_cents": 1,
    }
    assert r.structured_content["evidence_reference"] == "plan_rules:RULE-DEFERRAL-PCT-V1"


def test_get_agent_asset(ingested, settings, manifest_path):
    r = _call(settings, manifest_path, "get_agent_asset", {"asset_id": "PAYOPS-AGENT-001"})
    assert r.structured_content["model"] == "qwen3:8b"
    assert r.structured_content["backup_owner"] == "UNASSIGNED"


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("get_batch_summary", {"batch_id": "BATCH-999999"}),
        ("list_exceptions", {"batch_id": "BATCH-999999"}),
        ("get_exception_details", {"exception_id": "EXC-999999"}),
        ("get_exception_evidence", {"exception_id": "EXC-999999"}),
        ("lookup_plan_rule", {"plan_id": "NOPE", "rule_code": "X", "as_of_date": "2026-10-31"}),
        ("get_agent_asset", {"asset_id": "OTHER-AGENT"}),
    ],
)
def test_unknown_ids_return_controlled_not_found(ingested, settings, manifest_path, name, args):
    r = _call(settings, manifest_path, name, args)
    assert r.is_error
    assert "not_found" in r.content[0].text


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("list_exceptions", {"batch_id": "BATCH-000001", "limit": 26}),
        ("list_exceptions", {"batch_id": "BATCH-000001", "limit": 0}),
        ("list_exceptions", {"batch_id": "BATCH-000001", "exception_type": "ANYTHING"}),
        ("get_exception_details", {"exception_id": "EXC-1' OR '1'='1"}),
        ("get_batch_summary", {}),
        ("lookup_plan_rule", {"plan_id": "P", "rule_code": "R", "as_of_date": "yesterday"}),
    ],
)
def test_invalid_arguments_rejected_by_server(ingested, settings, manifest_path, name, args):
    r = _call(settings, manifest_path, name, args)
    assert r.is_error and r.structured_content is None


def test_write_capabilities_do_not_exist(ingested, settings, manifest_path):
    for name in ("approve_exception", "send_email", "change_payroll", "delete_records"):
        r = _call(settings, manifest_path, name, {})
        assert r.is_error and "Unknown tool" in r.content[0].text


def test_tools_never_modify_the_database(ingested, settings, manifest_path):
    before = sha256_hex(settings.db_path.read_bytes())
    for name, args in [
        ("get_batch_summary", {"batch_id": ingested.batch_id}),
        ("list_exceptions", {"batch_id": ingested.batch_id, "limit": 25}),
        ("get_exception_details", {"exception_id": "EXC-000001"}),
        ("get_exception_evidence", {"exception_id": "EXC-000001"}),
    ]:
        assert not _call(settings, manifest_path, name, args).is_error
    assert sha256_hex(settings.db_path.read_bytes()) == before


def test_separate_stdio_server_process(ingested, settings, manifest_path):
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "payops_sentinel.mcp_server.server",
            "--db",
            str(settings.db_path),
            "--manifest",
            str(manifest_path),
        ],
        env={},
    )

    async def go():
        async with Client(params) as c:
            names = [t.name for t in (await c.list_tools()).tools]
            r = await c.call_tool("get_exception_details", {"exception_id": "EXC-000004"})
            return names, r

    names, r = anyio.run(go)
    assert names == list(ALLOWED_TOOLS)
    assert r.structured_content["employee_ref"] == "EMP-0017"


@pytest.mark.parametrize(
    ("exception_id", "expected"),
    [
        (
            "EXC-000004",
            {
                "plan_id": "PLAN-401K-A",
                "rule_code": "EMPLOYEE_DEFERRAL_PCT",
                "as_of_date": "2026-10-31",
            },
        ),
        ("EXC-000009", None),  # after-termination finding: no plan rule applies
        ("EXC-000010", None),  # duplicate row
    ],
)
def test_rule_lookup_hint(ingested, settings, manifest_path, exception_id, expected):
    r = _call(settings, manifest_path, "get_exception_details", {"exception_id": exception_id})
    assert r.structured_content["rule_lookup"] == expected


def test_amounts_are_served_pre_formatted_in_dollars(ingested, settings, manifest_path):
    """EXC-000003: the model once wrote 363140 cents as "$363,140.00" instead of "$3,631.40"."""
    d = _call(settings, manifest_path, "get_exception_details", {"exception_id": "EXC-000003"})
    assert d.structured_content["display_amounts"] == {
        "recorded_contribution_cents": "$363.16",
        "expected_contribution_cents": "$363.14",
    }
    e = _call(settings, manifest_path, "get_exception_evidence", {"exception_id": "EXC-000003"})
    by_type = {x["evidence_type"]: x for x in e.structured_content["evidence"]}
    assert by_type["payroll_row"]["display_amounts"]["eligible_comp_cents"] == "$3,631.40"
    calc = by_type["calculation"]["display_amounts"]
    assert calc["difference_cents"] == "$0.02" and calc["tolerance_cents"] == "$0.01"
    # display strings are derived; the hashed evidence snapshot itself is unchanged
    from payops_sentinel.hashing import canonical_json

    for x in e.structured_content["evidence"]:
        assert "display_amounts" not in x["snapshot"]
        assert sha256_hex(canonical_json(x["snapshot"])) == x["content_hash"]
