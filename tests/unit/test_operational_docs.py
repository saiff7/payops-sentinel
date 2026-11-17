"""Runbook, handover and configuration stay consistent with the code."""

from __future__ import annotations

import re

from payops_sentinel.agent.guardrails import ALLOWED_TOOLS
from payops_sentinel.agent.schemas import FailureCategory
from payops_sentinel.cli import main
from payops_sentinel.config import DEFAULT_CONFIG_PATH, PROJECT_ROOT, load_settings
from payops_sentinel.operations.registry import load_agent_asset

DOCS = PROJECT_ROOT / "docs"
REQUIRED_DOCS = [
    "architecture",
    "data_dictionary",
    "threat_model",
    "evaluation_plan",
    "runbook",
    "handover",
    "release_checklist",
]


def test_required_documents_exist():
    for name in REQUIRED_DOCS:
        assert (DOCS / f"{name}.md").stat().st_size > 500, name
    assert (PROJECT_ROOT / "README.md").exists()


def test_runbook_covers_every_blueprint_failure_scenario():
    runbook = (DOCS / "runbook.md").read_text().lower()
    for scenario in [
        "ollama unavailable",
        "model missing",
        "mcp server crashes",
        "database locked",
        "database corrupted",
        "invalid payroll file",
        "agent evaluation fails",
        "new version misbehaves",
        "reviewer service fails",
        "kill switch",
        "roll back",
        "backup and restore",
    ]:
        assert scenario in runbook, scenario


def test_runbook_payops_commands_exist():
    commands = set(re.findall(r"payops ([a-z-]+)", (DOCS / "runbook.md").read_text()))
    parser_commands = {
        "generate-data",
        "init",
        "ingest",
        "show",
        "investigate",
        "evaluate",
        "eval-compare",
        "backup",
        "verify-db",
    }
    assert commands <= parser_commands
    for cmd in parser_commands:
        try:
            main([cmd, "--help"])
        except SystemExit as exc:
            assert exc.code == 0, cmd


def test_shipped_configuration_is_valid_and_local():
    settings = load_settings(DEFAULT_CONFIG_PATH)
    assert settings.agent.enabled and settings.agent.model == "qwen3:8b"
    assert "127.0.0.1" in settings.agent.ollama_host
    asset = load_agent_asset(settings.agent.manifest_path)
    assert (PROJECT_ROOT / asset.runbook).exists()
    assert asset.model == settings.agent.model


def test_documented_failure_categories_and_tools_match_code():
    dictionary = (DOCS / "data_dictionary.md").read_text()
    assert all(c.value in dictionary for c in FailureCategory)
    handover = (DOCS / "handover.md").read_text() + (DOCS / "threat_model.md").read_text()
    assert "read-only" in handover and len(ALLOWED_TOOLS) == 6
