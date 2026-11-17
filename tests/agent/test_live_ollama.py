"""Opt-in live check against the real local model:  uv run pytest -m ollama"""

from __future__ import annotations

import pytest

from payops_sentinel.agent.ollama_client import ModelUnavailableError, OllamaChatModel
from payops_sentinel.agent.orchestrator import run_investigation
from payops_sentinel.agent.schemas import InvestigationStatus

pytestmark = pytest.mark.ollama


@pytest.fixture
def live_model(settings):
    model = OllamaChatModel(settings.agent)
    try:
        model.describe()
    except ModelUnavailableError as exc:
        pytest.skip(f"local model unavailable: {exc}")
    return model


@pytest.mark.parametrize("exception_id", ["EXC-000004", "EXC-000009"])
def test_real_qwen3_investigation_through_mcp(ingested, settings, live_model, exception_id):
    out = run_investigation(settings, exception_id, model=live_model)
    model_calls = [c for c in out.tool_calls if c.actor == "model"]
    assert model_calls, "the model must select at least one tool"
    assert all(c.tool_name.startswith(("get_", "list_", "lookup_")) for c in model_calls)
    # Either a fully validated draft or a fail-closed manual review; never anything else.
    assert out.status in {InvestigationStatus.DRAFT_READY, InvestigationStatus.MANUAL_REVIEW}
    if out.draft:
        assert out.draft.requires_human_review is True
        assert out.draft.exception_id == exception_id
