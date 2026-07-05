"""One-shot SSE packing helpers for command/plan responses (the Step B4-2 split).

A narrow seam extracted from the ``streaming.py`` god-file: the SSE triple
(``meta``/``delta``/``done``) for a command/plan response that has NO LLM turn is
produced in one place (``_command_sse_chunks``) and shared by both the live
``POST /chat/stream`` path (``_sse_command_response``) and the command turn drained
from the queue (``_command_turn_gen``) → so the command SSE shape can't drift
between surfaces. Within the package it depends only on the leaf ``models``
(``ChatResponse``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi.responses import StreamingResponse

from akana_server.observability import current_trace_id

from akana_server.api.routes.chat._base import _sse_pack
from akana_server.api.routes.chat.models import ChatResponse


def _command_sse_chunks(
    resp: ChatResponse, approval_required: bool
) -> tuple[bytes, bytes, bytes]:
    """The one-shot SSE triple for a command/plan response: ``(meta, delta, done)``.

    Shared by both the live ``POST /chat/stream`` path (``_sse_command_response``)
    and the command turn drained from the queue (``_command_turn_gen``) — so the
    command SSE shape can't drift between surfaces.
    """
    meta = _sse_pack(
        "meta",
        {
            "turn_id": resp.turn_id,
            "trace_id": current_trace_id(),
            "conversation_id": resp.conversation_id,
            "intent": resp.intent,
            "approval_required": approval_required,
        },
    ).encode("utf-8")
    delta = _sse_pack("delta", {"text": resp.text}).encode("utf-8")
    done = _sse_pack(
        "done",
        {
            "turn_id": resp.turn_id,
            "conversation_id": resp.conversation_id,
            "text": resp.text,
            "latency_ms": 0,
            "tokens": {"prompt": 0, "completion": 0},
            "tool_calls": [],
            "intent": resp.intent,
            "approval_required": approval_required,
            "dropped_turns": 0,
            "memory_writes": [],
            "action": resp.action,
            "plan": resp.plan,
            "skill_used": resp.skill_used,
        },
    ).encode("utf-8")
    return meta, delta, done


def _sse_command_response(
    resp: ChatResponse, approval_required: bool
) -> StreamingResponse:
    """Pack a command response (no LLM, instant) as a one-shot SSE.

    Shared by both the normal ``gates.response`` path and the "command arriving
    while a turn is in progress" path — so the command SSE shape can't drift
    between the two surfaces.
    """
    meta, delta, done = _command_sse_chunks(resp, approval_required)

    async def _command_sse() -> AsyncIterator[bytes]:
        yield meta
        yield delta
        yield done

    return StreamingResponse(
        _command_sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
