"""EventHub — a slow/broken WS client must not stall the broadcast (timeout + drop).

No real WebSocket: fake clients with a ``send_json`` interface. The real claim is
that ``broadcast_json`` is awaited from the chat SSE hot path — a stuck client must
not stall the whole server (the delta broadcast) indefinitely.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from akana_server.events import EventHub


class _HangingWS:
    """Mimics a full TCP buffer: send_json never returns."""

    async def send_json(self, data: dict[str, Any]) -> None:
        await asyncio.sleep(3600)


class _BrokenWS:
    async def send_json(self, data: dict[str, Any]) -> None:
        raise RuntimeError("connection closed")


class _GoodWS:
    def __init__(self) -> None:
        self.got: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        self.got.append(data)


def test_slow_client_does_not_block_broadcast() -> None:
    async def run() -> None:
        hub = EventHub(send_timeout=0.1)
        hang, good = _HangingWS(), _GoodWS()
        hub._clients = [hang, good]  # noqa: SLF001 - register requires a WS handshake

        t0 = time.monotonic()
        await hub.broadcast_json({"type": "chat_delta", "text": "x"})
        elapsed = time.monotonic() - t0

        assert elapsed < 1.0, f"broadcast took {elapsed:.2f}s — a slow client stalled it"
        # The healthy client received the message; the stuck client was dropped.
        assert good.got == [{"type": "chat_delta", "text": "x"}]
        assert hang not in hub._clients  # noqa: SLF001
        assert good in hub._clients  # noqa: SLF001

    asyncio.run(run())


def test_broken_client_dropped_others_kept() -> None:
    async def run() -> None:
        hub = EventHub(send_timeout=0.5)
        broken, good = _BrokenWS(), _GoodWS()
        hub._clients = [broken, good]  # noqa: SLF001

        await hub.broadcast_json({"type": "a"})
        assert broken not in hub._clients  # noqa: SLF001
        assert good.got == [{"type": "a"}]

    asyncio.run(run())


def test_sequential_broadcasts_preserve_per_client_order() -> None:
    async def run() -> None:
        hub = EventHub(send_timeout=0.5)
        good = _GoodWS()
        hub._clients = [good]  # noqa: SLF001

        for i in range(5):
            await hub.broadcast_json({"seq": i})
        assert [d["seq"] for d in good.got] == [0, 1, 2, 3, 4]

    asyncio.run(run())


def test_broadcast_with_no_clients_is_noop() -> None:
    async def run() -> None:
        hub = EventHub(send_timeout=0.5)
        await hub.broadcast_json({"type": "a"})  # no exception, silent no-op

    asyncio.run(run())


# -- QUALITY turn: heavy concurrent broadcast + mixed client health ------------------


class _YieldingWS:
    """A healthy client that yields control to the event loop on each send."""

    def __init__(self) -> None:
        self.got: list[dict[str, Any]] = []

    async def send_json(self, data: dict[str, Any]) -> None:
        await asyncio.sleep(0)
        self.got.append(data)


def test_hundred_concurrent_broadcasts_no_loss_to_slow_follower() -> None:
    """100 concurrent broadcasts + a slow follower: all delivered, none lost."""

    async def run() -> None:
        hub = EventHub(send_timeout=5.0)
        ws = _YieldingWS()
        hub._clients = [ws]  # noqa: SLF001
        await asyncio.gather(*(hub.broadcast_json({"n": i}) for i in range(100)))
        assert len(ws.got) == 100
        assert {d["n"] for d in ws.got} == set(range(100))

    asyncio.run(run())


def test_concurrent_broadcasts_drop_broken_keep_good() -> None:
    """During a concurrent broadcast a broken client is dropped; the healthy ones
    receive all messages (re-disconnecting a broken client is safe against ValueError)."""

    async def run() -> None:
        hub = EventHub(send_timeout=0.5)
        goods = [_YieldingWS() for _ in range(3)]
        broken = _BrokenWS()
        hub._clients = [*goods, broken]  # noqa: SLF001
        await asyncio.gather(*(hub.broadcast_json({"m": i}) for i in range(10)))
        assert broken not in hub._clients  # noqa: SLF001
        assert all(g in hub._clients for g in goods)  # noqa: SLF001
        assert all(len(g.got) == 10 for g in goods)

    asyncio.run(run())


def test_disconnect_unregistered_client_is_safe() -> None:
    """Disconnecting a client that was never registered is a silent no-op (idempotent)."""
    hub = EventHub(send_timeout=0.5)
    ghost = _GoodWS()
    hub.disconnect(ghost)  # no ValueError
    hub._clients = [ghost]  # noqa: SLF001
    hub.disconnect(ghost)
    hub.disconnect(ghost)  # safe a second time too
    assert ghost not in hub._clients  # noqa: SLF001
