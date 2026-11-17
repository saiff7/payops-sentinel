"""Asset registry and the operational-readiness gate."""

from __future__ import annotations

from payops_sentinel.agent.guardrails import ALLOWED_TOOLS
from payops_sentinel.config import PROJECT_ROOT
from payops_sentinel.operations.registry import (
    load_agent_asset,
    load_tool_permissions,
    readiness_checks,
)

GOV = PROJECT_ROOT / "governance"


def _checks(asset, evaluation=None):
    return {
        c.check: c.passed
        for c in readiness_checks(
            asset,
            project_root=PROJECT_ROOT,
            enforced_tools=frozenset(ALLOWED_TOOLS),
            manifest_tools=load_tool_permissions(GOV / "tool_permissions.toml", "agent_service"),
            latest_evaluation_passed=evaluation,
        )
    }


def test_demo_asset_is_registered_but_not_operationally_ready():
    asset = load_agent_asset(GOV / "agent_manifest.toml")
    assert asset.model == "qwen3:8b" and asset.data_classification == "SYNTHETIC_DEMO"
    checks = _checks(asset)
    assert checks["Technical owner identified"]
    assert checks["Tool permissions match enforced allowlist"]
    assert not checks["Backup owner assigned"]  # never invent a backup person
    assert not checks["Evaluation passed release thresholds"]
    assert not all(checks.values())


def test_gate_passes_only_when_every_requirement_is_met(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "runbook.md").write_text("runbook")
    asset = load_agent_asset(GOV / "agent_manifest.toml").model_copy(
        update={
            "business_owner": "Ops Lead",
            "backup_owner": "Second Engineer",
            "release_status": "production",
        }
    )
    checks = {
        c.check: c.passed
        for c in readiness_checks(
            asset,
            project_root=tmp_path,
            enforced_tools=frozenset(ALLOWED_TOOLS),
            manifest_tools=frozenset(ALLOWED_TOOLS),
            latest_evaluation_passed=True,
        )
    }
    assert all(checks.values())
