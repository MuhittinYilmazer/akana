"""Driver contract — the thin boundary between Akana and any token producer.

Vision §3.A: "Cursor SDK = one driver, not the engine." Nothing Cursor-specific
(or Ollama-specific) crosses this boundary. Backends live in sibling modules
and are constructed directly by their callers (e.g.
``akana_server/orchestrator/openai_provider.py`` builds ``OpenAIDriver``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "Message",
    "ChatChunk",
    "ChatResult",
    "Driver",
    "DriverError",
    "DriverUnavailable",
]


@dataclass(frozen=True, slots=True)
class Message:
    """One conversation turn in provider-neutral (OpenAI-style) form."""

    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass(frozen=True, slots=True)
class ChatChunk:
    """One streamed piece of an answer. The final chunk has ``done=True``."""

    delta: str = ""
    done: bool = False
    usage: dict[str, Any] | None = None
    raw: dict[str, Any] | None = None  # backend extras (tool_call, timing) parked here


@dataclass(frozen=True, slots=True)
class ChatResult:
    """A one-shot (non-streamed) completion."""

    text: str
    model: str = ""
    usage: dict[str, Any] = field(default_factory=dict)


class DriverError(Exception):
    """Any backend failure, normalized. Callers never see Cursor/httpx specifics."""

    def __init__(
        self,
        message: str,
        *,
        kind: str = "unknown",
        retryable: bool = False,
        provider: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.kind = kind
        self.retryable = retryable
        self.provider = provider
        self.status_code = status_code


class DriverUnavailable(DriverError):
    """Backend not reachable/configured (bridge missing, API key absent, Ollama down)."""


class Driver(ABC):
    """A token producer. Implement :meth:`stream_chat`; :meth:`complete` is derived."""

    name: str = "driver"

    @abstractmethod
    def stream_chat(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
    ) -> AsyncIterator[ChatChunk]:
        """Async-generate :class:`ChatChunk`s; the last one has ``done=True``."""
        raise NotImplementedError

    async def complete(
        self,
        messages: list[Message],
        *,
        model: str | None = None,
    ) -> ChatResult:
        """One-shot completion. Default: drain :meth:`stream_chat`. Backends may override."""
        parts: list[str] = []
        usage: dict[str, Any] = {}
        async for chunk in self.stream_chat(messages, model=model):
            if chunk.delta:
                parts.append(chunk.delta)
            if chunk.done and chunk.usage:
                usage = chunk.usage
        return ChatResult(text="".join(parts), model=model or self.name, usage=usage)
