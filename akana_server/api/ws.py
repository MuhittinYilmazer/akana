"""WebSocket endpoints (F1)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from akana_server.api.deps import authorize_websocket
from akana_server.events import EventHub

log = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])


@router.websocket("/ws/events")
async def events_socket(
    websocket: WebSocket,
    token: str | None = Query(
        default=None,
        description="Same value as AKANA_TOKEN when auth is enabled",
    ),
) -> None:
    hub: EventHub = websocket.app.state.event_hub

    await websocket.accept()
    # Single shared token/proxy/loopback gate (see deps.authorize_websocket).
    if not authorize_websocket(websocket, token):
        await websocket.close(code=1008)
        return

    await hub.register(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        log.debug("ws client disconnected")
    except KeyError:
        # receive_text() reads message["text"]; a binary frame has no "text" key
        # and raises KeyError instead of WebSocketDisconnect. This socket is
        # broadcast-only, so a binary frame is unsupported data — close cleanly
        # with 1003 rather than letting the KeyError escape to the ASGI layer as
        # an unhandled-exception traceback.
        log.debug("ws client sent a non-text frame; closing")
        try:
            await websocket.close(code=1003)
        except Exception:
            pass
    finally:
        hub.disconnect(websocket)
