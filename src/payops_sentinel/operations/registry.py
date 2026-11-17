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
