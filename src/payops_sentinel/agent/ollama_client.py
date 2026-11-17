"""Thin, replaceable boundary around the local Ollama runtime.

The orchestrator depends only on the ``ChatModel`` protocol, so tests use a scripted fake and
CI never needs a model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
import ollama

from payops_sentinel.config import AgentSettings


class ModelUnavailableError(Exception):
    """Ollama is not reachable, the model is missing, or the request failed/timed out."""


@dataclass(frozen=True)
class ToolCallRequest:
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelReply:
    content: str
    tool_calls: tuple[ToolCallRequest, ...] = field(default_factory=tuple)


class ChatModel(Protocol):
    model_name: str

    def describe(self) -> str:
        """Model identifier including its local digest, for reproducibility records."""
        ...

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> ModelReply: ...


class OllamaChatModel:
    def __init__(self, settings: AgentSettings) -> None:
        self.model_name = settings.model
        self._settings = settings
        self._client = ollama.Client(host=settings.ollama_host, timeout=settings.request_timeout_s)

    def describe(self) -> str:
        try:
            models = self._client.list().models
        except (httpx.HTTPError, ollama.ResponseError, ConnectionError) as exc:
            raise ModelUnavailableError(f"Ollama not reachable: {type(exc).__name__}") from exc
        for m in models:
            if m.model == self.model_name:
                return f"{self.model_name}@{(m.digest or 'unknown')[:12]}"
        raise ModelUnavailableError(f"model {self.model_name} is not installed locally")

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        response_schema: dict[str, Any] | None = None,
    ) -> ModelReply:
        try:
            response = self._client.chat(
                model=self.model_name,
                messages=messages,
                tools=tools,
                format=response_schema,
                think=False,  # Qwen3: no reasoning preamble; keeps tool calls and JSON clean
                options={"temperature": self._settings.temperature, "seed": self._settings.seed},
            )
        except (httpx.HTTPError, ollama.ResponseError, ConnectionError) as exc:
            raise ModelUnavailableError(f"model request failed: {type(exc).__name__}") from exc
        message = response.message
        calls = tuple(
            ToolCallRequest(name=c.function.name, arguments=dict(c.function.arguments or {}))
            for c in (message.tool_calls or [])
        )
        return ModelReply(content=message.content or "", tool_calls=calls)
