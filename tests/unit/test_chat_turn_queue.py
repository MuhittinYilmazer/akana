"""Server-side chat turn queue (in-memory FIFO)."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from akana_server.api.chat_turn_queue import (
    QUEUE_MAX_DEPTH,
    clear_queue,
    enqueue_message,
    pop_next,
    queue_depth,
    queue_snapshot,
    requeue_front,
)


class _App:
    state: object

    def __init__(self) -> None:
        self.state = type("S", (), {})()


def test_enqueue_pop_fifo() -> None:
    app = _App()
    enqueue_message(app, "c1", {"text": "bir"})
    enqueue_message(app, "c1", {"text": "iki"})
    assert queue_depth(app, "c1") == 2
    first = pop_next(app, "c1")
    assert first is not None and first.payload["text"] == "bir"
    second = pop_next(app, "c1")
    assert second is not None and second.payload["text"] == "iki"
    assert pop_next(app, "c1") is None


def test_queue_max_depth() -> None:
    app = _App()
    for i in range(QUEUE_MAX_DEPTH):
        enqueue_message(app, "c1", {"text": f"m{i}"})
    with pytest.raises(HTTPException) as exc:
        enqueue_message(app, "c1", {"text": "overflow"})
    assert exc.value.status_code == 429
    assert exc.value.detail["error"]["code"] == "QUEUE_FULL"


def test_requeue_front_preserves_fifo_after_race() -> None:
    # Drain race: while the front-popped item is being started, a NEW message arrives.
    # requeue_front must place the item at the FRONT → the popped (older) item is
    # drained again BEFORE the next one (the old enqueue_message appended to the END,
    # breaking FIFO).
    app = _App()
    enqueue_message(app, "c1", {"text": "ilk"})
    popped = pop_next(app, "c1")
    assert popped is not None and popped.payload["text"] == "ilk"
    enqueue_message(app, "c1", {"text": "araya-giren"})  # arrived during the race
    requeue_front(app, "c1", popped)
    assert queue_depth(app, "c1") == 2
    again = pop_next(app, "c1")
    assert again is not None and again.payload["text"] == "ilk"
    assert again.id == popped.id  # original item (id/enqueued_at preserved), not a copy
    nxt = pop_next(app, "c1")
    assert nxt is not None and nxt.payload["text"] == "araya-giren"


def test_requeue_front_does_not_drop_when_full() -> None:
    # pop+requeue while the queue is FULL: the old path raised 429 via enqueue_message
    # and DROPPED the popped item (data loss). requeue_front bypasses the capacity
    # → the item is not lost; ceiling is MAX+1 (the next pop brings it back below the limit).
    app = _App()
    for i in range(QUEUE_MAX_DEPTH):
        enqueue_message(app, "c1", {"text": f"m{i}"})
    popped = pop_next(app, "c1")  # depth MAX-1
    assert popped is not None
    enqueue_message(app, "c1", {"text": "yeni"})  # filled during the race → MAX again
    requeue_front(app, "c1", popped)  # must not raise 429, must not drop
    assert queue_depth(app, "c1") == QUEUE_MAX_DEPTH + 1
    front = pop_next(app, "c1")
    assert front is not None and front.id == popped.id


def test_clear_queue() -> None:
    app = _App()
    enqueue_message(app, "c1", {"text": "x"})
    clear_queue(app, "c1")
    assert queue_depth(app, "c1") == 0
    snap = queue_snapshot(app, "c1")
    assert snap["depth"] == 0
