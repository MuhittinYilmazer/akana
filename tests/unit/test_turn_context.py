"""Turn correlation context (``trace_id``) — unit tests.

Step A observability primitive: that ``trace_id`` stays CONSTANT throughout the
turn, the reuse semantics (an inheriting path merges; ``reuse=False`` mints a
fresh id) and the MOST CRITICAL property — asyncio task isolation (consecutive
turns do NOT INHERIT each other's id) — are locked in here. This isolation is the
guarantee the ``_run_turn_detached(reuse=False)`` design relies on: even if
consecutive turns copy the context via drain, the id does not leak.
"""

from __future__ import annotations

import asyncio
import logging

from akana_server.observability import (
    TurnLogFilter,
    begin_turn,
    current_trace_id,
    current_turn,
    new_trace_id,
    update_turn,
)
from akana_server.observability.turn_context import _current


def _reset() -> None:
    _current.set(None)


def test_new_trace_id_unique() -> None:
    ids = {new_trace_id() for _ in range(200)}
    assert len(ids) == 200


def test_begin_turn_sets_current() -> None:
    _reset()
    assert current_trace_id() == "-"
    assert current_turn() is None
    t = begin_turn("conv-1", mode="blocking")
    assert current_turn() is t
    assert current_trace_id() == t.trace_id
    assert t.conversation_id == "conv-1"
    assert t.mode == "blocking"
    _reset()


def test_begin_turn_reuse_enriches_without_new_id() -> None:
    _reset()
    first = begin_turn("conv-1", mode="stream")
    # reuse=True (default): same trace_id, the missing provider is filled in
    second = begin_turn("conv-1", provider="claude")
    assert second.trace_id == first.trace_id
    assert second.provider == "claude"
    assert second.mode == "stream"  # the existing field wins
    _reset()


def test_begin_turn_reuse_false_mints_fresh() -> None:
    _reset()
    first = begin_turn("conv-1")
    second = begin_turn("conv-1", reuse=False)
    assert second.trace_id != first.trace_id
    _reset()


def test_update_turn_keeps_trace_id() -> None:
    _reset()
    t = begin_turn("conv-1")
    updated = update_turn(provider="cursor", mode="voice")
    assert updated is not None
    assert updated.trace_id == t.trace_id
    assert updated.provider == "cursor"
    assert updated.mode == "voice"
    _reset()
    assert update_turn(provider="x") is None  # no-op when there is no turn


def test_log_filter_injects_trace_id() -> None:
    _reset()
    flt = TurnLogFilter()
    rec = logging.LogRecord("n", logging.INFO, __file__, 1, "msg", None, None)
    assert flt.filter(rec) is True
    assert rec.trace_id == "-"  # record outside a turn → safe placeholder
    begin_turn("conv-1")
    rec2 = logging.LogRecord("n", logging.INFO, __file__, 1, "msg", None, None)
    flt.filter(rec2)
    assert rec2.trace_id == current_trace_id()
    assert rec2.trace_id != "-"
    _reset()


def test_task_isolation_no_cross_turn_bleed() -> None:
    """MOST CRITICAL: each asyncio task gets its own trace context → no leak.

    ``create_task`` copies the parent context; a child task's ``begin_turn`` call
    mutates only ITS OWN copy. So consecutive/concurrent turns do not see each
    other's (or the parent's) ``trace_id`` — the ``reuse=False`` design leans on
    this guarantee.
    """

    async def _main() -> None:
        begin_turn("conv-parent")  # T1 in the parent context
        parent_id = current_trace_id()
        seen: dict[str, str] = {}

        async def _turn(name: str) -> None:
            # The task COPIED the parent context (inherits T1); reuse=False stamps a fresh id.
            t = begin_turn(f"conv-{name}", reuse=False)
            await asyncio.sleep(0)  # task switch — does another turn leak in?
            seen[name] = current_trace_id()
            assert current_trace_id() == t.trace_id

        await asyncio.gather(_turn("a"), _turn("b"))

        assert seen["a"] != seen["b"]  # the two turns have separate ids
        assert seen["a"] != parent_id and seen["b"] != parent_id  # separate from the parent too
        assert current_trace_id() == parent_id  # the parent context was NOT POLLUTED

    asyncio.run(_main())
    _current.set(None)
