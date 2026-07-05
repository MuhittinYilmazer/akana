"""OpenAI Realtime WS endpoint — ``/ws/voice/realtime`` (the twin of gemini ``voice_live``).

When ``provider==openai`` + ``openai_realtime_enabled``, the browser voice-chat button
connects to this socket (full-duplex Realtime instead of turn-based ``/voice``). The
gate order is identical to ``voice_live.py``:

1. ``hmac.compare_digest`` token gate (wrong token → ``close(1008)``).
2. If the ``openai_realtime_enabled`` flag is off → ``close(1011)`` (with a guiding reason).
3. Is a key present (``openai_realtime_available``)? If not → ``close(1011)``.
4. ``conv_id`` (provided or a new ULID) → run :class:`OpenAIRealtimeBridge`.

It is prefix-less (``ws_routes`` in ``app.py``) → the canonical path is ``/ws/voice/realtime``.
"""

from __future__ import annotations

import logging

import ulid
from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from akana_server.api.deps import authorize_websocket
from akana_server.config import Settings
from akana_server.orchestrator.openai_shared import openai_realtime_available
from akana_server.voice.openai_realtime import OpenAIRealtimeBridge

log = logging.getLogger(__name__)

router = APIRouter(tags=["voice-realtime"])


@router.websocket("/ws/voice/realtime")
async def voice_realtime_socket(
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
    # 2) Realtime flag (OFF by default — opt-in; audio flows to OpenAI).
    if not getattr(settings, "openai_realtime_enabled", False):
        await websocket.close(
            code=1011, reason="OpenAI Realtime is off — enable it in Settings → Voice."
        )
        return
    # 3) Shared precondition: an API key is present (the websockets transport is a hard dependency).
    if not openai_realtime_available(settings):
        await websocket.close(
            code=1011,
            reason="OpenAI is unavailable — no API key (Settings → Identity).",
        )
        return
    conv_id = (conversation_id or "").strip() or str(ulid.new())
    bridge = OpenAIRealtimeBridge(websocket, settings, app=websocket.app, conv_id=conv_id)
    try:
        await bridge.run()
    except WebSocketDisconnect:  # pragma: no cover - normal close
        pass
