"""The 30-case golden dataset must stay consistent with the synthetic data and the allowlist."""

from __future__ import annotations

from collections import Counter

from payops_sentinel.agent.guardrails import ALLOWED_TOOLS, validate_tool_call
from payops_sentinel.config import PROJECT_ROOT
from payops_sentinel.operations.evaluations import load_cases
from payops_sentinel.storage import repositories as repo

DATASET = PROJECT_ROOT / "evals" / "golden_cases.jsonl"


def test_dataset_shape():
    cases = load_cases(DATASET)
    assert len(cases) == 30
    assert len({c.case_id for c in cases}) == 30
    assert Counter(c.category for c in cases) == {
        "standard": 12,
        "phrasing": 8,
        "batch_context": 3,
        "adversarial": 5,
        "not_found": 2,
    }
    assert all(c.expected_human_review_requirement for c in cases)


def test_cases_reference_real_exceptions_and_allowlisted_tools(ingested, conn):
    for c in load_cases(DATASET):
        details = repo.exception_details(conn, c.exception_id)
        if c.category == "not_found":
            assert details is None and c.expected_outcomes == ["manual_review"]
            continue
        assert details is not None, c.case_id
        assert details["exception_type"] == c.exception_type, c.case_id
        assert set(c.required_tools) | set(c.acceptable_extra_tools) <= set(ALLOWED_TOOLS)
        # a rule lookup is required exactly when the deterministic finding has a rule
        assert ("lookup_plan_rule" in c.required_tools) == (details["rule_id"] is not None)
        for tool, args in c.expected_tool_arguments.items():
            validate_tool_call(tool, args)  # expected arguments are themselves valid calls
