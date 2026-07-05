"""Cursor bridge session/run control helpers (the Step B4-2 split).

A narrow seam extracted from the ``streaming.py`` god-file: interrupting the
running Cursor run (``_abort_bridge_run_for_conversation`` — like the IDE STOP,
the agent is preserved), closing the bridge session (``_close_bridge_session`` —
hard reset), and the hard-reset composite (``_reset_cursor_bridge_for_conversation``).
Within the package it depends only on ``chat_state`` (``_synthetic_request``) +
the leaf ``_base`` (``_off_loop``) → the DAG flows downward, no cycle.
"""

from __future__ import annotations

import logging
from typing import Any

from akana_server.config import Settings
from akana_server.chat_context import clear_agent_id
from akana_server.orchestrator.bridge_pool import (
    bridge_daemon_enabled,
    get_bridge_pool,
)

from akana_server.api.routes.chat._base import _off_loop
from akana_server.api.routes.chat.chat_state import _synthetic_request

log = logging.getLogger(__name__)


async def _close_bridge_session(settings: Settings, conversation_id: str | None) -> None:
    """Close the Cursor bridge session — hard reset (chat deletion / unrecoverable stuck)."""
    conv_id = (conversation_id or "").strip()
    if not conv_id or not bridge_daemon_enabled():
        return
    try:
        await get_bridge_pool(settings).close_session(conv_id)
    except Exception:
        log.warning("bridge close_session failed (conv=%s)", conv_id, exc_info=True)


async def _abort_bridge_run_for_conversation(
    settings: Settings, conversation_id: str | None
) -> None:
    """Interrupt the running Cursor run — agent + agent_id are preserved (like the IDE STOP)."""
    conv_id = (conversation_id or "").strip()
    if not conv_id or not bridge_daemon_enabled():
        return
    try:
        await get_bridge_pool(settings).abort_run(conv_id)
    except Exception:
        log.warning("bridge abort_run failed (conv=%s)", conv_id, exc_info=True)


async def _reset_cursor_bridge_for_conversation(app: Any, conv_id: str) -> None:
    """Hard reset: close the bridge + clear agent_id (when abort isn't enough)."""
    cid = (conv_id or "").strip()
    if not cid:
        return
    settings = getattr(app.state, "settings", None)
    if isinstance(settings, Settings):
        await _close_bridge_session(settings, cid)
    await _off_loop(clear_agent_id, _synthetic_request(app), cid)
