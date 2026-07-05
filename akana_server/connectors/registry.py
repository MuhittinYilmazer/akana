"""ConnectorEngine F0 — registry: active channels from config + shared inbound queue.

:class:`ConnectorRegistry` holds the active connectors, starts/stops them all
at once, and routes outbound messages to the correct channel via ``connector_id``.
:func:`build_registry` is the config → registry bridge: adding a new channel
means adding an ``if settings.<channel>_enabled`` block here (to be moved to a
manifest in F1+).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from akana_server import audit
from akana_server.connectors.base import (
    Connector,
    InboundMessage,
    OutboundMessage,
    split_text,
)
from akana_server.connectors.egress_filter import filter_outbound
from akana_server.observability import capture_failure

if TYPE_CHECKING:
    from akana_server.config import Settings

__all__ = ["ConnectorRegistry", "build_registry"]

log = logging.getLogger(__name__)


class ConnectorRegistry:
    """Active connector registry + single shared inbound queue.

    :meth:`send` is the SINGLE outbound seam for every channel: the egress filter
    (secret/PII redaction) runs HERE, unconditionally, so EVERY message leaving
    Akana over a connector is scrubbed — router replies, ScheduleEngine reminders
    (:meth:`send_to`), proactive pushes and any future sender alike. The router
    also redacts (for archive/audit consistency); re-filtering already-redacted
    text is idempotent (a no-op), so there is no double-redaction artifact.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._connectors: dict[str, Connector] = {}
        # ``settings`` is optional (tests build a bare registry). When present it is
        # used only to locate ``data_dir`` for the egress audit line — redaction
        # itself never depends on it (fail-closed regardless).
        self._settings = settings
        # maxsize: an unbounded queue was an OOM risk (even a single user can flood it).
        # Connectors use a blocking ``await put``, so a full queue = backpressure (poll
        # pauses, Telegram keeps updates on the server → no loss), no OOM.
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=100)

    def register(self, connector: Connector) -> None:
        cid = (getattr(connector, "connector_id", "") or "").strip()
        if not cid:
            raise ValueError("connector_id must not be empty")
        if cid in self._connectors:
            raise ValueError(f"connector already registered: {cid!r}")
        self._connectors[cid] = connector

    @property
    def connector_ids(self) -> tuple[str, ...]:
        return tuple(self._connectors)

    def get(self, connector_id: str) -> Connector | None:
        return self._connectors.get(connector_id)

    async def start_all(self) -> None:
        """Start all connectors; a failure in one channel does not stop the others."""
        for cid, connector in self._connectors.items():
            try:
                await connector.start(self.inbound)
            except Exception as e:
                capture_failure(e, where=f"connectors.ConnectorRegistry.start_all[{cid}]")

    async def stop_all(self) -> None:
        for cid, connector in self._connectors.items():
            try:
                await connector.stop()
            except Exception as e:
                capture_failure(e, where=f"connectors.ConnectorRegistry.stop_all[{cid}]")

    async def send(self, message: OutboundMessage) -> None:
        """Send to a channel — the UNCONDITIONAL egress-filter seam.

        Every outbound path funnels through here, so the secret/PII redaction is
        applied to EVERY message regardless of who produced it (router reply,
        reminder, proactive push, future sender). Previously the filter lived only
        in ``InboundRouter.handle`` → any other sender (e.g. ScheduleEngine via
        :meth:`send_to`) bypassed it and could leak a credential. Redaction here is
        fail-closed and idempotent (the router's own redaction is not undone).
        """
        connector = self._connectors.get(message.connector_id)
        if connector is None:
            raise KeyError(f"unknown connector: {message.connector_id!r}")
        result = filter_outbound(message.text)
        if result.redacted:
            self._audit_egress(message.connector_id, message.chat_id, result.matched)
            message = OutboundMessage(
                connector_id=message.connector_id,
                chat_id=message.chat_id,
                text=result.text,
            )
        await connector.send(message)

    async def send_to(self, channel: str, chat_id: str, text: str) -> None:
        """Channel-agnostic send service (ScheduleEngine and other internal consumers).

        Splits the text according to the channel's message limit
        (``max_message_len``) and sends chunks in order. Each chunk goes through
        :meth:`send`, so the egress filter is applied to reminders / proactive
        pushes too (NOT only to router LLM replies — that was the bypass).
        An unknown channel raises :class:`KeyError` (the caller decides).
        """
        connector = self._connectors.get(channel)
        if connector is None:
            raise KeyError(f"unknown connector: {channel!r}")
        limit = int(getattr(connector, "max_message_len", 0) or 0)
        chunks = split_text(text, limit) if limit else [text]
        for chunk in chunks:
            await self.send(
                OutboundMessage(connector_id=channel, chat_id=chat_id, text=chunk)
            )

    def _audit_egress(
        self, connector_id: str, chat_id: str, matched: tuple[str, ...]
    ) -> None:
        """Write a ``connector_egress_filtered`` audit line (best-effort).

        Only fires when the registry was built with ``settings`` (production); a
        bare test registry skips the audit but still redacts. The router writes
        its own audit line for replies it filters first; this covers EVERY OTHER
        sender that reaches the send seam (reminders/proactive/future)."""
        data_dir = getattr(self._settings, "data_dir", None)
        if data_dir is None:
            return
        try:
            audit.write_event(
                data_dir,
                "connector_egress_filtered",
                data={
                    "connector": connector_id,
                    "chat_id": chat_id,
                    "patterns": list(matched),
                    "seam": "registry.send",
                },
            )
        except Exception as e:  # audit must never break a send
            capture_failure(e, where="connectors.ConnectorRegistry._audit_egress")

    def status(self) -> list[dict[str, Any]]:
        """Status list for the REST surface — must not contain secrets."""
        out: list[dict[str, Any]] = []
        for cid, connector in self._connectors.items():
            try:
                out.append(connector.status())
            except Exception as e:
                capture_failure(e, where=f"connectors.ConnectorRegistry.status[{cid}]")
                out.append({"id": cid, "running": False, "error": "status unavailable"})
        return out


def build_registry(settings: Settings) -> ConnectorRegistry:
    """Build active channels from config. Default: none (all opt-in)."""
    registry = ConnectorRegistry(settings)
    if getattr(settings, "telegram_enabled", False):
        from akana_server.connectors.telegram import TelegramConnector

        registry.register(TelegramConnector(settings))
    return registry
