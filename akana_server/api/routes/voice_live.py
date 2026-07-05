"""Gemini Live WS endpoint — ``/ws/voice/live`` (Phase 2).

When ``provider==gemini`` + ``gemini_live_enabled``, the browser voice-chat button
connects to this socket (full-duplex Live instead of turn-based ``/voice``). The
gate order follows the same discipline as ``ws.py`` (events):

1. ``hmac.compare_digest`` token gate (wrong token → ``close(1008)``).
2. If the ``gemini_live_enabled`` flag is off → ``close(1011)`` (with a guiding reason).
3. Is the SDK installed + a key present (``gemini_available``)? If not → ``close(1011)``.
4. ``conv_id`` (provided or a new ULID) → run :class:`LiveBridge`.

It is prefix-less (like ``ws_routes`` in ``app.py``) → the canonical path is ``/ws/voice/live``.
"""

from __future__ import annotations

import logging

import ulid
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from akana_server.api.deps import authorize_websocket
from akana_server.config import Settings
from akana_server.orchestrator.gemini_shared import gemini_available
from akana_server.voice.gemini_live import LiveBridge

log = logging.getLogger(__name__)

router = APIRouter(tags=["voice-live"])


@router.websocket("/ws/voice/live")
async def voice_live_socket(
    websocket: WebSocket,
    token: str | None = Query(
        default=None, description="Same value as AKANA_TOKEN (when auth is enabled)"
    ),
    conversation_id: str | None = Query(
        default=None, description="Conversation to resume; empty means a new ULID"
    ),
) -> None:
    settings: Settings = websocket.app.state.settings
    await websocket.accept()
    # 1) Auth gate — single shared token/proxy/loopback gate (see deps.authorize_websocket).
    if not authorize_websocket(websocket, token):
        await websocket.close(code=1008)
        return
    # 2) Live flag (OFF by default — opt-in; audio flows to Google).
    if not getattr(settings, "gemini_live_enabled", False):
        await websocket.close(
            code=1011, reason="Gemini Live is off — enable it in Settings → Voice."
        )
        return
    # 3) Shared precondition: the google-genai SDK is installed + an API key is present.
    if not gemini_available(settings):
        await websocket.close(
            code=1011,
            reason="Gemini is unavailable — the SDK is not installed or there is no API key.",
        )
        return
    conv_id = (conversation_id or "").strip() or str(ulid.new())
    bridge = LiveBridge(websocket, settings, app=websocket.app, conv_id=conv_id)
    try:
        await bridge.run()
    except WebSocketDisconnect:  # pragma: no cover - normal close
        pass
