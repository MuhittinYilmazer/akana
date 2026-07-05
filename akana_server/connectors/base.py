"""ConnectorEngine F0 — channel-agnostic core types.

We start with a single channel (Telegram) but the boundary is drawn here: every
external channel implements the :class:`Connector` protocol, incoming messages
land in the shared queue as :class:`InboundMessage`, and outgoing replies return
via :class:`OutboundMessage`. Adding a new channel means implementing this
protocol and registering it; the router, policy, and egress filter are unaware
of channel names.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "Connector",
    "ConnectorSendError",
    "InboundMessage",
    "OutboundMessage",
    "split_text",
]


def split_text(text: str, limit: int) -> list[str]:
    """Split *text* into chunks that do not exceed *limit* characters (channel message limit).

    Split priority: double newline > single newline > space > hard cut. Empty
    text returns a single empty chunk (the caller decides what to do with an
    empty reply).
    """
    if limit <= 0 or len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[: limit + 1]
        cut = -1
        for sep in ("\n\n", "\n", " "):
            cut = window.rfind(sep)
            if cut > 0:
                break
        if cut <= 0:
            cut = limit  # single oversized word block: hard cut
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return [c for c in chunks if c] or [text[:limit]]


class ConnectorSendError(Exception):
    """Send to channel API failed — the router catches this and does not break the flow."""


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """Normalised form of a single message arriving from a channel (system boundary).

    ``raw`` carries the original payload (debug / F2 replay); the router reads
    only the normalised fields.
    """

    connector_id: str
    chat_id: str
    text: str
    sender_id: str = ""
    sender_name: str = ""
    message_id: str = ""
    raw: dict[str, Any] | None = field(default=None, hash=False)


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """Single reply to be sent back to a channel — carries text that has PASSED the egress filter."""

    connector_id: str
    chat_id: str
    text: str


@runtime_checkable
class Connector(Protocol):
    """Lifecycle contract for a single external channel.

    * ``start(inbound)`` — starts listening; puts incoming messages onto the
      ``inbound`` queue. If it cannot start (e.g. missing token) it does NOT
      raise — it reports its state via ``status()``.
    * ``stop()`` — stops listening; must be idempotent.
    * ``send(message)`` — sends a message to the channel; errors are raised as
      :class:`ConnectorSendError`.
    * ``status()`` — status dict for the REST surface, must NOT contain secrets.
    """

    connector_id: str

    async def start(self, inbound: asyncio.Queue[InboundMessage]) -> None: ...

    async def stop(self) -> None: ...

    async def send(self, message: OutboundMessage) -> None: ...

    def status(self) -> dict[str, Any]: ...
