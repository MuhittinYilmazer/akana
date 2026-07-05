"""★2 — bounded TTS queue (_DropOldestQueue) boundary-value locks.

When the TTS audio queues were unbounded, under heavy load (fast LLM + slow Piper +
long response) memory grew without limit. _DropOldestQueue: when full, put drops the
OLDEST, NEVER blocks, and the sentinel (None) enters safely. These tests lock those
three guarantees.
"""

from __future__ import annotations

import asyncio

from akana_server.api.routes.chat.streaming import _DropOldestQueue


def test_bounded_drops_oldest_keeps_newest() -> None:
    """When maxsize is exceeded the OLDEST drops; the last maxsize items are kept."""

    async def run() -> None:
        q = _DropOldestQueue(maxsize=3)
        for i in range(10):
            await q.put(i)  # must never block
        assert q.qsize() == 3
        got = [q.get_nowait() for _ in range(3)]
        assert got == [7, 8, 9]  # the newest 3 (older ones dropped)

    asyncio.run(run())


def test_put_never_blocks_when_full() -> None:
    """put (await) on a full queue does not block — the producer (LLM read) doesn't stall."""

    async def run() -> None:
        q = _DropOldestQueue(maxsize=2)
        await q.put("a")
        await q.put("b")  # full
        # On an unbounded queue this would wait forever; here it must return immediately.
        await asyncio.wait_for(q.put("c"), timeout=0.5)
        assert q.qsize() == 2

    asyncio.run(run())


def test_sentinel_none_survives_overflow() -> None:
    """The end sentinel (None) enters even on a full queue (oldest drops) → the consumer finishes."""

    async def run() -> None:
        q = _DropOldestQueue(maxsize=2)
        await q.put("a")
        await q.put("b")  # full
        await q.put(None)  # sentinel: "a" drops, None enters
        items = [q.get_nowait() for _ in range(q.qsize())]
        assert None in items

    asyncio.run(run())


def test_put_nowait_also_drops_oldest() -> None:
    """put_nowait (override) also does not raise QueueFull when full, it drops the oldest."""

    async def run() -> None:
        q = _DropOldestQueue(maxsize=1)
        q.put_nowait("eski")
        q.put_nowait("yeni")  # NOT QueueFull → "eski" drops
        assert q.get_nowait() == "yeni"

    asyncio.run(run())
