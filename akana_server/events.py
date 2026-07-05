"""In-process pub/sub for WebSocket clients (F1)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from starlette.websockets import WebSocket

log = logging.getLogger(__name__)

#: Upper bound (seconds) for a single send to one client. A slow/disconnected
#: client (full TCP buffer) must not block broadcast indefinitely; on timeout
#: the client is dropped. Since broadcast_json is awaited from the chat SSE hot
#: path, this limit also caps "how long can a broadcast stall".
WS_SEND_TIMEOUT = 2.0


class EventHub:
    """Broadcast JSON to all connected `/ws/events` clients."""

    def __init__(self, *, send_timeout: float = WS_SEND_TIMEOUT) -> None:
        self._clients: list[WebSocket] = []
        self._send_timeout = send_timeout

    async def register(self, ws: WebSocket) -> None:
        self._clients.append(ws)
        try:
            await ws.send_json({"type": "ready", "phase": "F1"})
        except Exception:
            # If the first "ready" send fails (client disconnected immediately after
            # accept), don't leave a dead ws stuck in the list until the next
            # broadcast — drop it right away. disconnect is idempotent.
            self.disconnect(ws)
            raise

    def disconnect(self, ws: WebSocket) -> None:
        try:
            self._clients.remove(ws)
        except ValueError:
            pass

    async def _send_or_drop(
        self, ws: WebSocket, data: dict[str, Any]
    ) -> WebSocket | None:
        """Send to a single client; mark the client for drop on error/timeout."""
        try:
            await asyncio.wait_for(ws.send_json(data), timeout=self._send_timeout)
            return None
        except TimeoutError:
            log.debug("ws send timed out after %.1fs; client dropped", self._send_timeout)
            return ws
        except Exception as e:
            log.debug("ws send failed: %s", e)
            return ws

    async def broadcast_json(self, data: dict[str, Any]) -> None:
        clients = list(self._clients)
        if not clients:
            return
        # Concurrent sends: a slow client cannot delay delivery to other clients.
        # Since a single broadcast call awaits all sends, ordering of consecutive
        # broadcasts to the same client is preserved.
        stale = await asyncio.gather(
            *(self._send_or_drop(ws, data) for ws in clients)
        )
        for ws in stale:
            if ws is not None:
                self.disconnect(ws)
                # Close the socket too: removing it from the hub only stops
                # future broadcasts, but the /ws/events handler stays parked in
                # receive_text() with the TCP connection open, so the browser's
                # ws.onclose never fires and it silently receives nothing while
                # still showing "connected". Closing wakes the handler and lets
                # the client reconnect.
                try:
                    await ws.close()
                except Exception:
                    pass
