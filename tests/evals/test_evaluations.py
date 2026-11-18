"""Evaluation harness: scoring, release gate, persistence and regression comparison.

A deterministic reference model follows the documented investigation method, so the harness can
be exercised end to end in CI (real orchestrator, real stdio MCP server, no Ollama).
"""

from __future__ import annotations

import json
import re

import pytest

from payops_sentinel.agent.ollama_client import ModelReply, ToolCallRequest
from payops_sentinel.config import PROJECT_ROOT
from payops_sentinel.operations import evaluations as ev
from payops_sentinel.storage import repositories as repo

DATASET = PROJECT_ROOT / "evals" / "golden_cases.jsonl"


class ReferenceModel:
    """Follows the method in the system prompt. ``invent_rule_lookups`` reproduces the observed
    qwen3:8b behaviour of calling lookup_plan_rule with made-up arguments when no rule applies."""

    model_name = "reference"

    def __init__(self, invent_rule_lookups: bool = False):
        self.invent = invent_rule_lookups

    def describe(self):
        return "reference@000000000000"

    def chat(self, messages, *, tools=None, response_schema=None):
        user = next(m["content"] for m in messages if m["role"] == "user")
        exc = re.search(r"EXC-\d{6}", user).group(0)
        results = {
            m["tool_name"]: json.loads(m["content"])
            for m in messages
            if m["role"] == "tool" and m["content"].startswith("{")
        }
        called = {m["tool_name"] for m in messages if m["role"] == "tool"}
        details = results.get("get_exception_details", {})
        if response_schema is not None:
            ref = details.get("employee_ref") or "the referenced employee"
            return ModelReply(
                json.dumps(
                    {
                        "exception_id": exc,
                        "investigation_summary": f"The engine flagged {exc} for review.",
                        "evidence_ids": details.get("evidence_ids", [])[:3],
                        "reasoning_summary": "The cited evidence supports the finding.",
                        "proposed_next_step": "REQUEST_CLARIFICATION",
                        "email_subject": "Payroll record requiring review",
                        "email_body": f"Hello, please verify the payroll record for {ref} "
                        "and provide clarification or a corrected record. Thank you.",
                        "limitations": [],
                        "requires_human_review": True,
                    }
                )
            )
        batch = re.search(r"BATCH-\d{6}", user)
        if batch and "get_batch_summary" not in called:
            return _call("get_batch_summary", batch_id=batch.group(0))
        if "get_exception_details" not in called:
            return _call("get_exception_details", exception_id=exc)
        if "get_exception_evidence" not in called:
            return _call("get_exception_evidence", exception_id=exc)
        if "lookup_plan_rule" not in called:
            if details.get("plan_id"):
                return _call(
                    "lookup_plan_rule",
                    plan_id=details["plan_id"],
                    rule_code=details["rule_code"],
                    as_of_date=details["pay_period_end"],
                )
            if self.invent:
                return _call(
                    "lookup_plan_rule",
                    plan_id="PLAN-000001",
                    rule_code="RULE-000001",
                    as_of_date="2026-10-31",
                )
        return ModelReply("I have the evidence.")


def _call(name, **args):
    return ModelReply("", (ToolCallRequest(name, args),))


@pytest.fixture(scope="module")
def reports(tmp_path_factory):
    """Run the full 30-case dataset twice (good and regressed reference models) once."""
    from payops_sentinel.config import Settings

    tmp = tmp_path_factory.mktemp("eval")
    settings = Settings(
        db_path=tmp / "app.db",
        artifacts_dir=tmp / "art",
        reference_data_dir=PROJECT_ROOT / "data" / "synthetic",
        employer_code="NORTHSTAR",
        ruleset_version="2026.10-demo",
        max_file_bytes=1_000_000,
        max_rows=1_000,
    )
    cases = ev.load_cases(DATASET)
    good = ev.run_evaluation(
        settings, cases, work_dir=tmp / "w1", dataset_sha256="x", model=ReferenceModel()
    )
    bad = ev.run_evaluation(
        settings,
        cases,
        work_dir=tmp / "w2",
        dataset_sha256="x",
        model=ReferenceModel(invent_rule_lookups=True),
    )
    return settings, good, bad


def test_reference_agent_passes_every_case_and_the_gate(reports):
    _, good, _ = reports
    failing = [(c.case_id, c.notes) for c in good.cases if not c.passed]
    assert failing == []
    assert good.passed and good.case_count == 30 and not good.partial
    m = good.metrics
    assert m["tool_selection_accuracy"] == m["tool_argument_accuracy"] == 1.0
    assert m["critical_security_violations"] == 0.0
    assert m["draft_ready_rate"] == pytest.approx(28 / 30, abs=1e-4)  # 2 not-found cases


def test_invented_rule_lookups_are_measured_as_tool_selection_failures(reports):
    _, _, bad = reports
    failing = {c.case_id for c in bad.cases if not c.passed}
    no_rule_cases = {
        c.case_id
        for c in ev.load_cases(DATASET)
        if c.category != "not_found" and "lookup_plan_rule" not in c.required_tools
    }
    assert failing == no_rule_cases
    assert bad.metrics["tool_selection_accuracy"] < 0.9
    assert bad.metrics["failed_model_tool_calls"] == len(no_rule_cases)
    assert not bad.passed


def test_regression_comparison_flags_the_drop(reports):
    _, good, bad = reports
    cmp = ev.compare_reports(good, bad)
    assert "tool_selection_accuracy" in cmp.regressions
    assert "failed_model_tool_calls" in cmp.regressions
    no_rule = [
        c
        for c in ev.load_cases(DATASET)
        if c.category != "not_found" and "lookup_plan_rule" not in c.required_tools
    ]
    assert len(cmp.newly_failing) == len(no_rule) and cmp.newly_passing == []
    assert ev.compare_reports(bad, good).regressions == []


def test_reports_are_persisted_to_db_and_json(reports, tmp_path):
    settings, good, _ = reports
    from payops_sentinel.storage.db import connect, initialize

    conn = connect(settings.db_path)
    initialize(conn)
    path = ev.save_report(conn, good, tmp_path)
    assert ev.load_report(path) == good
    (run,) = [
        r for r in repo.list_evaluation_runs(conn) if r["evaluation_id"] == good.evaluation_id
    ]
    assert run["passed"] == 1 and run["model_digest"] == "000000000000"
    assert len(repo.list_evaluation_cases(conn, good.evaluation_id)) == 30


def test_partial_runs_never_pass_the_gate(tmp_path):
    from payops_sentinel.config import Settings

    settings = Settings(
        db_path=tmp_path / "app.db",
        reference_data_dir=PROJECT_ROOT / "data" / "synthetic",
        employer_code="NORTHSTAR",
        ruleset_version="2026.10-demo",
        max_file_bytes=1_000_000,
        max_rows=1_000,
    )
    cases = ev.load_cases(DATASET)[:2]
    report = ev.run_evaluation(
        settings, cases, work_dir=tmp_path, dataset_sha256="x", model=ReferenceModel(), partial=True
    )
    assert ev.release_gate(report.metrics) and not report.passed


@pytest.mark.parametrize(
    ("metric", "value"),
    [
        ("tool_selection_accuracy", 0.89),
        ("evidence_coverage", 0.99),
        ("critical_security_violations", 1.0),
        ("allowlist_compliance", 0.5),
    ],
)
def test_release_gate_thresholds(metric, value):
    metrics = {k: (0.0 if op == "<=" else 1.0) for k, (op, _) in ev.THRESHOLDS.items()}
    assert ev.release_gate(metrics)
    assert not ev.release_gate(metrics | {metric: value})


def test_prefetch_credit_applies_only_to_labelled_prefetch_runs():
    from payops_sentinel.agent.schemas import InvestigationOutcome, ToolCallRecord

    case = next(
        c
        for c in ev.load_cases(DATASET)
        if c.exception_id == "EXC-000009" and c.category == "standard"
    )
    args = {"exception_id": "EXC-000009"}
    host = [
        ToolCallRecord(sequence=i, actor="host", tool_name=n, arguments=args, success=True)
        for i, n in enumerate(["get_exception_details", "get_exception_evidence"], 1)
    ]

    def outcome(prefetched):
        return InvestigationOutcome(
            run_id="R",
            exception_id="EXC-000009",
            status="manual_review",
            model_version="m",
            tool_calls=host,
            prefetched_tools=prefetched,
        )

    def score(o, methodology):
        return ev.score_case(
            case, o, set(), None, max_tool_calls=8, max_iterations=4, methodology=methodology
        )

    pre = ["get_exception_details", "get_exception_evidence"]
    assert score(outcome(pre), ev.HOST_PREFETCH).tool_selection_correct is True
    assert score(outcome(pre), ev.HOST_PREFETCH).arguments_matched == 2
    # same data, but scored as a normal run: host calls are never credited
    assert score(outcome(pre), ev.MODEL_SELECTED).tool_selection_correct is False
    # ground-truth host reads that were NOT given to the model are never credited
    assert score(outcome([]), ev.HOST_PREFETCH).tool_selection_correct is False


def test_comparison_flags_methodology_change(reports):
    _, good, _ = reports
    labelled = good.model_copy(update={"methodology": ev.HOST_PREFETCH})
    assert ev.compare_reports(good, labelled).methodology_changed
    assert not ev.compare_reports(good, good).methodology_changed
