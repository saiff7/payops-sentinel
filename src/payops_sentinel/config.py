"""Typed application settings loaded from ``config.toml``."""

from __future__ import annotations

import tomllib
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class AgentSettings(BaseModel):
    """Local model and agent-loop limits. These are application limits, not MCP limits."""

    model_config = ConfigDict(frozen=True)

    model: str = "qwen3:8b"
    ollama_host: str = "http://127.0.0.1:11434"
    request_timeout_s: float = Field(default=180, gt=0)
    temperature: float = Field(default=0.0, ge=0, le=1)
    seed: int = 7
    max_iterations: int = Field(default=4, ge=1, le=10)
    max_tool_calls: int = Field(default=8, ge=1, le=20)
    max_result_chars: int = Field(default=10_000, ge=500)
    tool_timeout_s: float = Field(default=15, gt=0)
    max_draft_attempts: int = Field(default=2, ge=1, le=3)
    asset_id: str = "PAYOPS-AGENT-001"
    manifest_path: Path = PROJECT_ROOT / "governance" / "agent_manifest.toml"
    tool_permissions_path: Path = PROJECT_ROOT / "governance" / "tool_permissions.toml"

    @field_validator("model")
    @classmethod
    def _local_model_only(cls, value: str) -> str:
        if "cloud" in value.lower():
            raise ValueError("cloud-hosted models are not permitted; use a local Ollama model")
        return value

    @field_validator("ollama_host")
    @classmethod
    def _local_host_only(cls, value: str) -> str:
        if urlparse(value).hostname not in LOCAL_HOSTS:
            raise ValueError("ollama_host must be a local endpoint (127.0.0.1/localhost)")
        return value


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    environment: str = "local-demo"
    db_path: Path
    artifacts_dir: Path = PROJECT_ROOT / "data" / "artifacts"
    reference_data_dir: Path
    employer_code: str
    ruleset_version: str
    max_file_bytes: int = Field(gt=0)
    max_rows: int = Field(gt=0)
    agent: AgentSettings = AgentSettings()


def load_settings(config_path: Path | None = None) -> Settings:
    path = (config_path or DEFAULT_CONFIG_PATH).resolve()
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    base = path.parent
    ingestion = raw["ingestion"]
    agent = dict(raw.get("agent", {}))
    for key in ("manifest_path", "tool_permissions_path"):
        if key in agent:
            agent[key] = base / agent[key]
    return Settings(
        environment=raw["app"]["environment"],
        db_path=base / raw["storage"]["db_path"],
        artifacts_dir=base / raw["storage"].get("artifacts_dir", "data/artifacts"),
        reference_data_dir=base / raw["reference_data"]["dir"],
        employer_code=ingestion["employer_code"],
        ruleset_version=ingestion["ruleset_version"],
        max_file_bytes=ingestion["max_file_bytes"],
        max_rows=ingestion["max_rows"],
        agent=AgentSettings(**agent),
    )
