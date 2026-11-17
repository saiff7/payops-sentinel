"""Starter golden cases (Phase 2) must stay consistent with the synthetic dataset."""

from __future__ import annotations

import json

from payops_sentinel.agent.guardrails import ALLOWED_TOOLS
from payops_sentinel.config import PROJECT_ROOT
from payops_sentinel.storage import repositories as repo


def test_golden_cases_reference_real_exceptions(ingested, conn):
    lines = (PROJECT_ROOT / "evals" / "golden_cases.jsonl").read_text().splitlines()
    cases = [json.loads(line) for line in lines]
    assert len({c["case_id"] for c in cases}) == len(cases) >= 6
    for c in cases:
        details = repo.exception_details(conn, c["exception_id"])
        assert details is not None, c["case_id"]
        assert details["exception_type"] == c["exception_type"], c["case_id"]
        assert set(c["required_tools"]) | set(c["acceptable_extra_tools"]) <= set(ALLOWED_TOOLS)
        assert c["expected_human_review_requirement"] is True
