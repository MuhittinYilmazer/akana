"""In-memory per-conversation chat message queue (restart clears all).

Messages arriving while a turn is in progress are placed in a FIFO queue; once
the turn finishes (or the registry empties after a STOP), ``drain`` starts the
next message as a separate turn.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import ulid
from fastapi import HTTPException

QUEUE_MAX_DEPTH = 10


@dataclass(slots=True)
class QueuedChatMessage:
    id: str
    payload: dict[str, Any]
    enqueued_at: float = field(default_factory=time.time)


def _queues(app: Any) -> dict[str, deque[QueuedChatMessage]]:
    reg = getattr(app.state, "chat_turn_queues", None)
    if not isinstance(reg, dict):
        reg = {}
        app.state.chat_turn_queues = reg
    return reg


def queue_depth(app: Any, conversation_id: str | None) -> int:
    conv_id = (conversation_id or "").strip()
    if not conv_id:
        return 0
    q = _queues(app).get(conv_id)
    return len(q) if q else 0


def list_queue(app: Any, conversation_id: str) -> list[QueuedChatMessage]:
    conv_id = (conversation_id or "").strip()
    if not conv_id:
        return []
    q = _queues(app).get(conv_id)
    if not q:
        return []
    return list(q)


def clear_queue(app: Any, conversation_id: str | None) -> None:
    conv_id = (conversation_id or "").strip()
    if conv_id:
        _queues(app).pop(conv_id, None)


def enqueue_message(app: Any, conversation_id: str, payload: dict[str, Any]) -> QueuedChatMessage:
    conv_id = (conversation_id or "").strip()
    if not conv_id:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "CONVERSATION_REQUIRED",
                    "message": "Could not queue the message: no conversation specified (conversation_id is required).",
                }
            },
        )
    payload = {**payload, "conversation_id": conv_id}
    queues = _queues(app)
    q = queues.setdefault(conv_id, deque())
    if len(q) >= QUEUE_MAX_DEPTH:
        raise HTTPException(
            status_code=429,
            detail={
                "error": {
                    "code": "QUEUE_FULL",
                    "message": (
                        f"Queue is full (at most {QUEUE_MAX_DEPTH} messages); "
                        "cancel the current turn with STOP, then send."
                    ),
                }
            },
        )
    item = QueuedChatMessage(
        id=str(ulid.new()),
        payload=payload,
    )
    q.append(item)
    return item


def requeue_front(app: Any, conversation_id: str, item: QueuedChatMessage) -> None:
    """Put a popped item back at the FRONT of the queue (drain-race recovery).

    An item taken from the front via ``pop_next`` must NOT be lost if another
    turn grabbed the slot during startup. The old path used
    ``enqueue_message(item.payload)`` — two flaws: (1) it appends the item to
    the BACK of the queue → if a new message slipped in, FIFO is broken (the
    front-popped item falls to the back); (2) if the queue is full it raises
    429 → the item is merely logged and DROPPED (data loss). This function puts
    the original ``QueuedChatMessage`` (preserving id + enqueued_at) at the
    front with ``appendleft`` and does NOT enforce the capacity LIMIT: the item
    was already accepted, this is a re-insertion. Since pop(-1) + requeue(+1)
    nets to zero, the ceiling is bounded by ``QUEUE_MAX_DEPTH + 1`` (the next pop
    brings it back under the limit); we prefer this over losing a single message.
    """
    conv_id = (conversation_id or "").strip()
    if not conv_id:
        return
    q = _queues(app).setdefault(conv_id, deque())
    q.appendleft(item)


def pop_next(app: Any, conversation_id: str) -> QueuedChatMessage | None:
    conv_id = (conversation_id or "").strip()
    if not conv_id:
        return None
    q = _queues(app).get(conv_id)
    if not q:
        return None
    try:
        return q.popleft()
    except IndexError:
        return None


def queue_snapshot(app: Any, conversation_id: str) -> dict[str, Any]:
    items = list_queue(app, conversation_id)
    return {
        "conversation_id": conversation_id,
        "depth": len(items),
        "items": [
            {
                "id": it.id,
                "text_preview": _preview_text(it.payload.get("text")),
                "enqueued_at": it.enqueued_at,
            }
            for it in items
        ],
    }


def _preview_text(text: Any, *, max_len: int = 80) -> str:
    s = str(text or "").strip().replace("\n", " ")
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"
