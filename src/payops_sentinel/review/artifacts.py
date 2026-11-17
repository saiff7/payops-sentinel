"""Approved, downloadable communication artifacts. The system never sends anything."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from payops_sentinel.hashing import sha256_hex


@dataclass(frozen=True)
class Artifact:
    file_name: str
    content: bytes
    sha256: str


def render(
    draft: dict[str, Any], *, approval_id: str, reviewer_id: str, approved_at: str
) -> Artifact:
    """Deterministic rendering: the same approved version always yields identical bytes."""
    body = json.loads(draft["draft_json"])
    text = "\n".join(
        [
            "PAYOPS SENTINEL - APPROVED EMPLOYER COMMUNICATION (EXPORT ONLY - NOT SENT)",
            "Data classification: SYNTHETIC_DEMO",
            f"Exception: {draft['exception_id']}",
            f"Draft: {draft['draft_id']} version {draft['draft_version']}",
            f"Draft content SHA-256: {draft['content_hash']}",
            f"Approved by: {reviewer_id} at {approved_at} ({approval_id})",
            "Approval covers this communication only; it does not resolve the payroll exception.",
            "-" * 72,
            f"Subject: {body['email_subject']}",
            "",
            body["email_body"],
            "-" * 72,
            f"Evidence references: {', '.join(body['evidence_ids'])}",
            "",
        ]
    )
    content = text.encode("utf-8")
    name = f"{draft['draft_id']}-v{draft['draft_version']}-approved.txt"
    return Artifact(file_name=name, content=content, sha256=sha256_hex(content))


def write(artifact: Artifact, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / artifact.file_name
    path.write_bytes(artifact.content)
    return path
