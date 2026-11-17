"""Typed application settings loaded from ``config.toml``."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.toml"


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    environment: str = "local-demo"
    db_path: Path
    reference_data_dir: Path
    employer_code: str
    ruleset_version: str
    max_file_bytes: int = Field(gt=0)
    max_rows: int = Field(gt=0)


def load_settings(config_path: Path | None = None) -> Settings:
    path = (config_path or DEFAULT_CONFIG_PATH).resolve()
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    base = path.parent
    ingestion = raw["ingestion"]
    return Settings(
        environment=raw["app"]["environment"],
        db_path=base / raw["storage"]["db_path"],
        reference_data_dir=base / raw["reference_data"]["dir"],
        employer_code=ingestion["employer_code"],
        ruleset_version=ingestion["ruleset_version"],
        max_file_bytes=ingestion["max_file_bytes"],
        max_rows=ingestion["max_rows"],
    )
