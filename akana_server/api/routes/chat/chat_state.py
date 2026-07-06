"""Detached-turn shared state + queue/predicate helpers (the Step B4-2 split).

The LOWEST layer extracted from the ``streaming.py`` god-file: the registries of
running turns (``_active_turns`` / ``_nonstreaming_busy`` / ``_background_tasks`` /
``_chat_cleanup_tombstones``), the ``_ActiveTurn`` buffer class, ``_DropOldestQueue``,
and the "is a turn running / is the conversation usable" predicates. Within the
package this module depends ONLY on the leaves (``_base``/``models``); it NEVER imports
the producer/detached machine above it at module level (a DAG leaf — no cycle).

The patch surface lives in the PACKAGE namespace (``akana_server.api.routes.chat``);
the names here are re-exported into that namespace via the ``streaming`` facade, so a
``setattr`` on ``routes.chat`` resolves to the same object.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException, Request

from akana_server.api.chat_turn_queue import queue_snapshot
from akana_server.config import Settings
from akana_server.events import EventHub
from akana_server.conversation_service import ConversationService

log = logging.getLogger(__name__)


def _turn_wrote_memory(tool_calls: list[dict[str, Any]]) -> bool:
    """True when the turn explicitly stored a memory via a memory-WRITE tool.

    Shared by BOTH turn surfaces (the streaming producer + the blocking/voice persist
    path) so each skips the redundant post-turn background capture when the agent
    already wrote memory in-turn. It lives in this leaf module so both callers import
    it downward (no chat_producer ↔ persist cycle).

    Akana has TWO memory write paths: the agent's in-turn memory-write tool AND the post-turn
    background ``llm_capture``. They do NOT dedup against each other (the inbox only dedups
    same-key WITHIN a path), so a fact stated once lands TWICE under different keys (e.g.
    "the user's name is Alice" + auto "name: Alice"). When the agent already wrote memory this
    turn, the redundant auto-capture is skipped — one fact, one write.

    The write tool surfaces under DIFFERENT names by provider: ``memory_remember`` (the MCP tool
    for cursor/claude, sometimes provider-prefixed like ``akana_memory/memory_remember`` or
    ``mcp__akana_memory__memory_remember``) and ``save_memory`` (the gemini/openai native
    function-call decl in gemini_tools/llm_tools). Match any of them (substring, lowercased) —
    matching only ``memory_remember`` missed gemini/openai's ``save_memory`` → double capture.

    A FAILED call stored nothing: the tool-call dict carries a ``status`` populated at
    phase='end' (``status='error'`` for an errored MCP call). Only a NON-errored write counts
    as "explicitly stored a memory" — otherwise a failed ``memory_remember`` (MCP subprocess
    down) would suppress the healthy background ``llm_capture`` fallback and the fact is lost
    from BOTH durable memory and the inbox.
    """
    for call in tool_calls:
        call = call or {}
        name = str(call.get("name") or "").lower()
        if (
            "memory_remember" in name
            or "save_memory" in name
            or ("memory" in name and "remember" in name)  # defensive: memory.remember / prefixed
        ):
            if str(call.get("status") or "").lower() == "error":
                continue  # the write failed → don't suppress the fallback capture
            return True
    return False


#: Upper bound on the streaming TTS queues. On a TTS-ON turn, if the LLM is very fast +
#: Piper TTS is slow + the response is long, the queues grew UNBOUNDED (a memory risk;
#: audit note: "queue doesn't block producer — aspirational, not enforced"). This bound
#: + drop-oldest: AUDIO degrades gracefully (a short gap); the TEXT STREAM (direct SSE
#: yield) and the LLM READ are UNAFFECTED — the producer is never blocked and the TTS
#: speed doesn't slow the LLM. On a TTS-OFF turn no data enters these queues (the bound
#: is neutral).
_TTS_QUEUE_MAX = 256


class _DropOldestQueue(asyncio.Queue):
    """When full, ``put`` drops the OLDEST item and adds the new one — NEVER blocks.

    Only for the TTS audio queues (loss is tolerated: a short audio gap). Since asyncio
    is single-threaded there is no await between ``full()`` + ``get_nowait`` +
    ``put_nowait`` -> atomic, no race. ``put`` (await) doesn't block either: it falls
    straight to ``put_nowait``. The sentinel (``None``) therefore enters safely (room is
    made).
    """

    def put_nowait(self, item: Any) -> None:
        if self.full():
            try:
                self.get_nowait()
            except asyncio.QueueEmpty:
                pass
        super().put_nowait(item)

    async def put(self, item: Any) -> None:  # does not block
        self.put_nowait(item)


@dataclass(slots=True)
class _ActiveTurn:
    """A conversation's running turn: the SSE chunk buffer + the producer task.

    Multi-follower safety: ``chunks`` is append-only, and each follower reads from its
    own cursor (index). Instead of a single ``asyncio.Event``, an ``asyncio.Condition``
    is used — when a new chunk is written, ALL followers are woken via ``notify_all``
    (eliminating the "lost wake on two followers" race in the single-Event + ``clear()``
    pattern). The resume endpoint (``GET /chat/active/{cid}``) replays the buffer
    accumulated so far and feeds the second+ live-continuing follower through this channel.

    NOTE (deliberate design — NO trimming): ``chunks`` is kept in full for the DURATION
    of the turn. The resume contract requires a replay from the START of the turn (on
    refresh the user sees the WHOLE response) → trimming below a fast follower's cursor
    would break the from-the-start replay of a resume that hasn't connected yet. The
    buffer lives only for the duration of the turn; when the turn ends it's dropped from
    the registry → growth is bounded by a single response, no leak.
    """

    conversation_id: str
    chunks: list[bytes] = field(default_factory=list)
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    done: bool = False
    task: "asyncio.Task[None] | None" = None
    status: str = "ok"
    assistant_turn_id: str | None = None
    #: A queue-drain SLOT RESERVATION: while the drained item runs its gates (an await
    #: window), this not-done placeholder holds the conversation slot so a fresh message is
    #: queued (FIFO) instead of overtaking it. Replaced by the real turn in
    #: ``_start_detached_*``; a placeholder is never a real running turn (no task/buffer).
    placeholder: bool = False


def _active_turns(app: Any) -> dict[str, _ActiveTurn]:
    reg = getattr(app.state, "active_turns", None)
    if not isinstance(reg, dict):
        reg = {}
        app.state.active_turns = reg
    return reg


def _background_tasks(app: Any) -> "set[asyncio.Task[Any]]":
    """A strong reference to ownerless fire-and-forget tasks (GC protection).

    If a reference to the ``asyncio.create_task`` return isn't held, the task can be
    garbage-collected while suspended ("Task was destroyed but it is pending") → the
    work is left half-done. This set holds the reference; when the task finishes it's
    dropped via ``add_done_callback``.
    """
    reg = getattr(app.state, "chat_background_tasks", None)
    if not isinstance(reg, set):
        reg = set()
        app.state.chat_background_tasks = reg
    return reg


def _active_breaker_provider(settings: "Settings | None") -> str:
    """The active LLM provider's breaker isolation key ("" if unavailable).

    The capture call is now dispatched to the active provider
    (gemini/openai/ollama/claude/cursor); so the burst-guard must read THAT provider's
    breaker. Only cursor/claude keep a breaker record — for the others (and for the
    unconfigured "" key) ``breaker_degraded`` already returns False (no blocking),
    which is correct. No provider is privileged: if ``settings`` is
    missing/unresolvable, the key is "" (no breaker, no blocking)."""
    if not isinstance(settings, Settings):
        return ""
    try:
        from akana_server.llm_context import load_effective_llm_settings
        from akana_server.llm_settings import resolve_provider

        return resolve_provider(
            settings, load_effective_llm_settings(settings.data_dir, settings)
        )
    except Exception:  # pragma: no cover - safe default if settings can't be read
        return ""


def _cursor_breaker_open(settings: "Settings | None" = None) -> bool:
    """Is the active provider's LLM circuit breaker degraded (OPEN/HALF_OPEN)? (reads without raising).

    Burst protection: when parallel detached turns open the breaker, the background
    memory capture (a 2nd LLM call) pours fuel on the fire — every degraded-breaker turn
    makes one more LLM attempt that deepens the rate-limit / consumes the single probe.
    Skip it while degraded (memory capture is best-effort; nothing is lost once the turn
    finishes).

    If ``settings`` is given, the active provider's breaker is read (cursor not
    hardcoded); if not, the key is "" (no breaker → never blocks).

    The old version compared the enum to ``.state`` (the METHOD, no parentheses) → ALWAYS
    False; this guard never ran. It now calls ``breaker_degraded`` correctly."""
    from akana_server.network.guard import breaker_degraded

    return breaker_degraded(_active_breaker_provider(settings))


def _spawn_background(app: Any, coro: "Any") -> "asyncio.Task[Any]":
    """Start a background task + hold its reference against GC.

    The done-callback RETRIEVES + LOGS the exception: otherwise, if a background task
    (drain, memory-capture, cleanup) crashes, the error is silently swallowed + a "Task
    exception was never retrieved" spam appeared. Now the crash is clearly logged → the
    root of symptoms like "the server is gone" during a concurrent turn becomes visible."""
    task = asyncio.create_task(coro)
    reg = _background_tasks(app)
    reg.add(task)

    def _on_done(t: "asyncio.Task[Any]") -> None:
        reg.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            log.error("background task crashed: %s", exc, exc_info=exc)

    task.add_done_callback(_on_done)
    return task


def _chat_cleanup_tombstones(app: Any) -> set[str]:
    """Deleted conversations — prevents drain/persist from resurrecting a soft-delete."""
    reg = getattr(app.state, "chat_cleanup_tombstones", None)
    if not isinstance(reg, set):
        reg = set()
        app.state.chat_cleanup_tombstones = reg
    return reg


def _conversation_chat_usable(app: Any, conversation_id: str | None) -> bool:
    conv_id = (conversation_id or "").strip()
    if not conv_id or conv_id in _chat_cleanup_tombstones(app):
        return False
    svc = getattr(app.state, "conversation_service", None)
    if isinstance(svc, ConversationService):
        return svc.get(conv_id) is not None
    return True


def _nonstreaming_busy(app: Any) -> "dict[str, asyncio.Task[Any]]":
    """Running non-streaming (blocking/voice) turns: conv_id → request task.

    Streaming turns are held in ``_active_turns`` with a follower/resume buffer; since
    blocking/voice is a single request-response it isn't written there → so the
    busy-guard DIDN'T SEE them (Convergence A #2: a concurrent 2nd blocking/voice turn on
    the same conv collides). This lightweight record is for busy (#2) + cancel (#3); NO
    buffer/follower → the load-bearing streaming registry is untouched (low blast-radius).

    The value = the request task (``asyncio.current_task()``): both the "running" marker
    and the cancel handle (``_cancel_nonstreaming_turn`` → ``task.cancel()``). Cleanup is
    done in the handler's ``finally`` by token (task) identity → if another turn took
    over the conv, the old release doesn't touch it; no permanent-busy failure mode.
    """
    reg = getattr(app.state, "nonstreaming_busy", None)
    if not isinstance(reg, dict):
        reg = {}
        app.state.nonstreaming_busy = reg
    return reg


def _is_turn_running(app: Any, conversation_id: str | None) -> bool:
    conv_id = (conversation_id or "").strip()
    if not conv_id:
        return False
    turn = _active_turns(app).get(conv_id)
    if turn is not None and not turn.done:
        return True
    busy = _nonstreaming_busy(app).get(conv_id)
    return busy is not None and not busy.done()


def _register_nonstreaming_turn(
    app: Any, conversation_id: str | None
) -> "asyncio.Task[Any] | None":
    """Mark the conv as a running blocking/voice turn; return the release handle (task).

    PUBLIC SEAM: new callers should use ``turn_gate.register_turn`` (this underscore
    name is kept as the implementation + a temporary alias for
    ``akana_server.connectors.service`` until it migrates).

    Atomic (no await) busy re-check + registration → a concurrent second turn on the
    same conv gets 409 TURN_BUSY. The current request task is recorded (the cancel
    handle). The caller MUST PASS the returned handle to
    :func:`_release_nonstreaming_turn` in ``finally`` (on every exit, including
    exception/cancel).
    """
    conv_id = (conversation_id or "").strip()
    if not conv_id:
        return None
    if _is_turn_running(app, conv_id):
        raise HTTPException(
            status_code=409,
            detail={
                "error": {
                    "code": "TURN_BUSY",
                    "message": (
                        "A reply is still being generated in this conversation;"
                        " you can send your new message once it finishes."
                    ),
                }
            },
        )
    task = asyncio.current_task()
    if task is None:  # a request always runs in a task; defensive
        return None
    _nonstreaming_busy(app)[conv_id] = task
    return task


def _release_nonstreaming_turn(
    app: Any, conversation_id: str | None, handle: "asyncio.Task[Any] | None"
) -> None:
    """Remove the record — only if the handle is still ours (another turn didn't take over the conv)."""
    conv_id = (conversation_id or "").strip()
    if not conv_id or handle is None:
        return
    reg = _nonstreaming_busy(app)
    if reg.get(conv_id) is handle:
        reg.pop(conv_id, None)


async def _cancel_nonstreaming_turn(
    app: Any, conv_id: str, *, await_cancel: bool = True
) -> bool:
    """Cancel a running blocking/voice turn (#3): cancel the request task.

    When the task is cancelled, the handler's LLM await gets ``CancelledError`` → the
    yield-dep ``finally`` releases the record. It waits briefly (for the handler cleanup
    to finish), then does a defensive pop. If there's no turn to cancel, ``False``.

    R3 #4: ``task.cancel()`` only interrupts the handler; a cursor run RUNNING on the
    bridge would be orphaned on the server (the streaming cancel path calls
    ``_abort_bridge_run_for_conversation`` — the voice/blocking STOP path didn't → a
    resource leak). After cancel, the bridge run is also interrupted, symmetric with the
    streaming branch. Lazy import: ``chat_bridge`` imports from this leaf module → no
    module-level cycle.
    """
    reg = _nonstreaming_busy(app)
    task = reg.get(conv_id)
    if task is None or task.done():
        return False
    if task is asyncio.current_task():
        # Don't cancel your OWN turn: a command like "delete the chat" triggers
        # cleanup→cancel from within its own non-streaming turn; cancelling itself would
        # cut the command short. The decorator's finally will release the record anyway.
        return False
    task.cancel()
    # b23: the DELETE path passes await_cancel=False for an INSTANT delete — the conversation
    # is being tombstoned (late writes are already blocked), so don't block the delete waiting
    # for a mid-LLM cancel's finally; the abort runs fire-and-forget in the background.
    if await_cancel:
        await asyncio.wait({task}, timeout=_CANCEL_AWAIT_TIMEOUT)
    if reg.get(conv_id) is task:
        reg.pop(conv_id, None)
    settings = getattr(app.state, "settings", None)
    if isinstance(settings, Settings):
        from akana_server.api.routes.chat.chat_bridge import (
            _abort_bridge_run_for_conversation,
        )

        def _took_over() -> bool:
            # Symmetric with the streaming cancel guard (chat_detached): during the
            # cancel window the cancelled handler's finally releases the busy slot, so
            # a fresh turn (second tab, a connector worker, a queued voice turn) can
            # register and start ITS OWN bridge run for this conversation. The backstop
            # abort must not kill that new run — it owns its run and the bridge pool's
            # active-run retry recovers the old one. New owner = a different, live task
            # in the non-streaming registry, or a running streaming/detached turn.
            nb = reg.get(conv_id)
            if nb is not None and nb is not task and not nb.done():
                return True
            return _is_turn_running(app, conv_id)

        if await_cancel:
            if not _took_over():
                await _abort_bridge_run_for_conversation(settings, conv_id)
        else:
            # Fire-and-forget: re-check ownership inside the spawned coroutine, just
            # before the abort, since the takeover may land after this returns.
            async def _abort_if_not_taken_over() -> None:
                if not _took_over():
                    await _abort_bridge_run_for_conversation(settings, conv_id)

            _spawn_background(app, _abort_if_not_taken_over())
    return True


def _synthetic_request(app: Any) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/chat/stream",
            "headers": [],
            "query_string": b"",
            "app": app,
            "client": None,
        }
    )


async def _broadcast_queue_updated(app: Any, conversation_id: str) -> None:
    conv_id = (conversation_id or "").strip()
    if not conv_id:
        return
    hub = getattr(app.state, "event_hub", None)
    if not isinstance(hub, EventHub):
        return
    snap = queue_snapshot(app, conv_id)
    try:
        await hub.broadcast_json({"type": "queue_updated", **snap})
    except Exception:
        log.debug("queue_updated broadcast failed (conv=%s)", conv_id, exc_info=True)


def _extract_turn_id(chunk: bytes) -> str | None:
    """Extract the assistant turn_id from the SSE `meta` chunk (for enrichment).

    Read-only, defensive: parsing returns None on any failure — it can never break
    the turn stream.
    """
    try:
        text = chunk.decode("utf-8")
    except Exception:
        return None
    if "event: meta" not in text:
        return None
    for line in text.splitlines():
        if line.startswith("data:"):
            try:
                data = json.loads(line[len("data:") :].strip())
            except Exception:
                return None
            tid = data.get("turn_id") if isinstance(data, dict) else None
            return str(tid) if tid else None
    return None


async def _append_chunk(turn: _ActiveTurn, chunk: bytes) -> None:
    """Append the chunk to the buffer + wake all followers (multi-follower safe)."""
    async with turn.cond:
        turn.chunks.append(chunk)
        if turn.assistant_turn_id is None:
            tid = _extract_turn_id(chunk)
            if tid:
                turn.assistant_turn_id = tid
        turn.cond.notify_all()


#: Upper bound for the turn's finally (including partial persist) to complete after STOP.
#: If exceeded, STOP responds while the turn continues in the background — so pathological
#: cases like sqlite lock contention don't block STOP indefinitely.
_CANCEL_AWAIT_TIMEOUT = 15.0
