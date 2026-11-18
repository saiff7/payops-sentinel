"""Agent asset registry and tool-permission manifests (governance/*.toml)."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict


class AgentAsset(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    asset_id: str
    asset_name: str
    agent_version: str
    model: str
    model_digest: str
    prompt_version: str
    tools_version: str
    retrieval_mode: str
    technical_owner: str
    business_owner: str
    backup_owner: str
    environment: str
    status: str
    release_status: str
    evaluation_status: str
    data_classification: str
    runbook: str


def load_agent_asset(path: Path) -> AgentAsset:
    with path.open("rb") as fh:
        return AgentAsset.model_validate(tomllib.load(fh)["asset"])


def load_tool_permissions(path: Path, principal: str) -> frozenset[str]:
    with path.open("rb") as fh:
        return frozenset(tomllib.load(fh)["principal"][principal]["tools"])


UNASSIGNED = "UNASSIGNED"


class ReadinessCheck(BaseModel):
    model_config = ConfigDict(frozen=True)

    check: str
    passed: bool
    detail: str


def readiness_checks(
    asset: AgentAsset,
    *,
    project_root: Path,
    enforced_tools: frozenset[str],
    manifest_tools: frozenset[str],
    latest_evaluation_passed: bool | None,
) -> list[ReadinessCheck]:
    """Release gate for operational readiness. A demo asset is expected to fail some checks."""

    def assigned(value: str) -> bool:
        return bool(value.strip()) and value.strip().upper() != UNASSIGNED

    return [
        ReadinessCheck(
            check="Technical owner identified",
            passed=assigned(asset.technical_owner),
            detail=asset.technical_owner,
        ),
        ReadinessCheck(
            check="Business owner identified",
            passed=assigned(asset.business_owner),
            detail=asset.business_owner,
        ),
        ReadinessCheck(
            check="Backup owner assigned",
            passed=assigned(asset.backup_owner),
            detail=asset.backup_owner,
        ),
        ReadinessCheck(
            check="Tool permissions match enforced allowlist",
            passed=enforced_tools == manifest_tools,
            detail=f"{len(enforced_tools)} enforced / {len(manifest_tools)} documented",
        ),
        ReadinessCheck(
            check="Evaluation passed release thresholds",
            passed=latest_evaluation_passed is True,
            detail="no evaluation run yet"
            if latest_evaluation_passed is None
            else ("passed" if latest_evaluation_passed else "failed"),
        ),
        ReadinessCheck(
            check="Runbook present",
            passed=(project_root / asset.runbook).exists(),
            detail=asset.runbook,
        ),
        ReadinessCheck(
            check="Release status is production",
            passed=asset.release_status == "production",
            detail=asset.release_status,
        ),
    ]


def latest_matching_evaluation(
    evaluations: list[dict], *, model: str, prompt_version: str
) -> dict | None:
    """Latest evaluation that applies to the *configured* agent (same model and prompt version).

    An evaluation of a different model or prompt must never satisfy the release gate.
    ``evaluations`` is ordered newest first (``repositories.list_evaluation_runs``).
    """
    for e in evaluations:
        if e["model_version"].split("@", 1)[0] == model and e["prompt_version"] == prompt_version:
            return e
    return None
