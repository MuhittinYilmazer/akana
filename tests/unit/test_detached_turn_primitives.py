"""Boundary-value locks for the DETACHED RESPONSE internal primitives (QUALITY turn).

The ``_ActiveTurn`` buffer + ``_follow_turn`` + ``_append_chunk`` + ``_extract_turn_id``
are pure, app-free units. The integration tests (test_chat_detached_turn.py) set up the
end-to-end flow; here we only lock the subtle race/parse boundaries: empty-chunk skip,
done-flip wakeup, late follower replay, turn_id parse defense.
"""

from __future__ import annotations

import asyncio

import akana_server.api.routes.chat as chat


async def _drain(turn) -> list[bytes]:
    return [chunk async for chunk in chat._follow_turn(turn)]


def test_follow_turn_skips_empty_chunks() -> None:
    """Empty (``b\"\"``) chunks are NOT yielded to the follower even if written to the buffer."""

    async def run() -> None:
        turn = chat._ActiveTurn(conversation_id="cv")
        await chat._append_chunk(turn, b"")
        await chat._append_chunk(turn, b"X")
        await chat._append_chunk(turn, b"")
        async with turn.cond:
            turn.done = True
            turn.cond.notify_all()
        assert await _drain(turn) == [b"X"]

    asyncio.run(run())


def test_late_follower_replays_full_buffer_after_done() -> None:
    """A follower that connects AFTER the turn finishes replays the whole buffer, then returns."""

    async def run() -> None:
        turn = chat._ActiveTurn(conversation_id="cv")
        await chat._append_chunk(turn, b"A")
        await chat._append_chunk(turn, b"B")
        async with turn.cond:
            turn.done = True
            turn.cond.notify_all()
        assert b"".join(await _drain(turn)) == b"AB"

    asyncio.run(run())


def test_done_flip_with_no_new_chunk_wakes_waiting_follower() -> None:
    """A waiting follower finishes cleanly (does not hang) when done=True with NO new chunk."""

    async def run() -> None:
        turn = chat._ActiveTurn(conversation_id="cv")
        await chat._append_chunk(turn, b"only")

        async def finisher() -> None:
            await asyncio.sleep(0.01)
            async with turn.cond:
                turn.done = True
                turn.cond.notify_all()

        task = asyncio.create_task(finisher())
        out = await asyncio.wait_for(_drain(turn), timeout=2.0)
        await task
        assert out == [b"only"]

    asyncio.run(run())


def test_multiple_followers_one_disconnects_other_completes() -> None:
    """Two followers watch the same turn; if one drops mid-way the other gets the full stream."""

    async def run() -> None:
        turn = chat._ActiveTurn(conversation_id="cv")

        async def producer() -> None:
            for i in range(5):
                await asyncio.sleep(0.005)
                await chat._append_chunk(turn, f"c{i}".encode())
            async with turn.cond:
                turn.done = True
                turn.cond.notify_all()

        async def follow_partial(stop_after: int) -> list[bytes]:
            out: list[bytes] = []
            async for chunk in chat._follow_turn(turn):
                out.append(chunk)
                if len(out) >= stop_after:
                    break
            return out

        prod = asyncio.create_task(producer())
        full = asyncio.create_task(_drain(turn))
        partial = asyncio.create_task(follow_partial(2))
        full_out = await asyncio.wait_for(full, timeout=2.0)
        partial_out = await asyncio.wait_for(partial, timeout=2.0)
        await prod
        assert [c.decode() for c in full_out] == ["c0", "c1", "c2", "c3", "c4"]
        assert [c.decode() for c in partial_out] == ["c0", "c1"]

    asyncio.run(run())


def test_first_meta_turn_id_wins() -> None:
    """``assistant_turn_id`` is taken from the first meta chunk; a later meta does not overwrite it."""

    async def run() -> None:
        turn = chat._ActiveTurn(conversation_id="cv")
        await chat._append_chunk(
            turn, b'event: meta\ndata: {"turn_id": "FIRST"}\n\n'
        )
        await chat._append_chunk(
            turn, b'event: meta\ndata: {"turn_id": "SECOND"}\n\n'
        )
        assert turn.assistant_turn_id == "FIRST"

    asyncio.run(run())


def test_extract_turn_id_defensive_parsing() -> None:
    """``_extract_turn_id`` returns None on any corrupt input — it cannot break the turn stream."""
    assert chat._extract_turn_id(b'event: meta\ndata: {"turn_id": "T1"}\n\n') == "T1"
    assert chat._extract_turn_id(b'event: meta\ndata: {"x": 1}\n\n') is None
    assert chat._extract_turn_id(b'event: delta\ndata: {"turn_id": "T2"}\n\n') is None
    assert chat._extract_turn_id(b"event: meta\ndata: not json\n\n") is None
    assert chat._extract_turn_id(b"\xff\xfe gecersiz utf8") is None
    assert chat._extract_turn_id(b"") is None


def test_maybe_drain_queue_noop_during_shutdown() -> None:
    """When the shutdown flag is set, drain does not start a NEW turn from the queue (no item popped).

    BK1: the active turn's finally spawns ``_maybe_drain_queue``; that in turn opens a
    new active turn via ``_start_detached_chat_turn`` (drain↔turn recursion). At shutdown
    this fired AFTER ``shutdown_background_tasks`` had emptied the set and, while bridge_pool
    was being torn down, produced half-writes / "Task destroyed but pending". When
    ``app.state.chat_shutting_down`` is set, drain returns early → the queue is PRESERVED
    (the item is re-processed at server startup via recover_tasks)."""
    from akana_server.api.chat_turn_queue import enqueue_message, queue_depth

    class _App:
        def __init__(self) -> None:
            self.state = type("S", (), {})()

    app = _App()
    enqueue_message(app, "c1", {"text": "kapanışta drain olmamalı"})
    assert queue_depth(app, "c1") == 1

    app.state.chat_shutting_down = True
    asyncio.run(chat._maybe_drain_queue(app, "c1"))

    assert queue_depth(app, "c1") == 1, (
        "the queue item must not be popped while the shutdown flag is set (the new-turn spawn must be cut)"
    )
