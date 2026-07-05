"""UNBREAKABLE RESPONSE: a client-independent server-side turn machine (the Step B4-2 split).

Live bug: while the SSE generator was tied to the client, closing the tab/switching
the chat cancelled the LLM turn, and everything "thinking in the background" was lost.
Fix: the turn runs as an asyncio task INDEPENDENT of the client
(``_run_turn_detached``); the SSE bytes it produces are written to a per-conversation
``_ActiveTurn`` buffer, and the HTTP response is a separate follower generator that
watches this buffer (``_follow_turn``). A client disconnect only closes the follower —
the turn continues, and when it finishes it does the normal persist + a WS
``turn_completed`` broadcast (the UI refreshes the conversation when it returns). A
second message to the same conversation while a turn is in progress returns 409
``TURN_BUSY`` (queue F2). On server shutdown, active turns are cleanly cancelled from
the lifespan (``shutdown_active_turns``) — the partial persist in the generator's
finally runs on this path too.

The top seam extracted from the ``streaming.py`` god-file: its in-package module-level
dependencies flow only DOWNWARD — ``chat_state`` (buffer/registry/predicate),
``chat_bridge`` (close session), ``chat_commands_sse`` (the command SSE triple),
``chat_producer`` (the LLM producer). Names patched in the PACKAGE namespace
(``_run_turn_gates`` / ``_start_detached_chat_turn`` /
``_abort_bridge_run_for_conversation`` / ``persist_assistant_turn``) are read LATE at
call time via ``_chatpkg`` (so a ``routes.chat`` setattr resolves to the same object).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import ulid
from fastapi import Request

from akana_server.config import Settings
from akana_server.events import EventHub
from akana_server.observability import begin_turn
from akana_server.api.chat_turn_queue import (
    clear_queue,
    pop_next,
    requeue_front,
)

from akana_server.api.routes.chat._base import (
    _off_loop,
    _resolve_tts_lang,
    _sse_pack,
)
from akana_server.api.routes.chat.models import ChatRequest, ChatResponse
from akana_server.api.routes.chat.gates import _GateResult
from akana_server.api.routes.chat.persist import (
    _persist_error_turn_end,
    _persist_user_turn_start,
)
from akana_server.api.routes.chat.chat_state import (
    _ActiveTurn,
    _CANCEL_AWAIT_TIMEOUT,
    _active_turns,
    _append_chunk,
    _broadcast_queue_updated,
    _cancel_nonstreaming_turn,
    _chat_cleanup_tombstones,
    _conversation_chat_usable,
    _is_turn_running,
    _spawn_background,
    _synthetic_request,
)
from akana_server.api.routes.chat.chat_bridge import _close_bridge_session
from akana_server.api.routes.chat.chat_commands_sse import _command_sse_chunks
from akana_server.api.routes.chat.chat_producer import _stream_chat_response

log = logging.getLogger(__name__)


class TurnAlreadyRunning(RuntimeError):
    """A turn is already running in the same conversation (race: two first-messages collided).

    The caller (``post_chat_stream``) converts this to the 202/queue path — it is
    not a real error, the concurrent second turn is queued.
    """


class ConversationNotUsable(RuntimeError):
    """The conversation can't be used for a detached turn (deleted/tombstoned).

    Kept SEPARATE from ``TurnAlreadyRunning``: the turn is not running → it can't
    be converted to the busy path; the caller must convert this to a clean 404
    NOT_FOUND (multi-tab race: another tab deleted the chat during start).
    """


async def _claim_and_run(
    app: Any,
    conv_id: str,
    gen: AsyncIterator[bytes],
    hub: EventHub | None,
) -> _ActiveTurn:
    """Atomically claim the conversation slot, spawn the detached turn, announce it.

    The race-sensitive claim logic lived twice (``_start_detached_chat_turn`` /
    ``_start_detached_command_turn``); it now exists ONCE here. Busy-guard: check BOTH
    ``_active_turns`` AND ``_nonstreaming_busy`` via ``_is_turn_running`` — looking at
    only ``_active_turns`` was blind to a concurrent voice / non-streaming turn on the
    same conv → a 2nd parallel LLM/persist on the same agent_id. There is NO await
    between the check and the registry set (atomic); on a collision the generator is
    closed and ``TurnAlreadyRunning`` is raised (the caller converts it to the
    202/queue path). b10: a queue drain reserves the slot with a placeholder before
    running gates; claiming that placeholder here is not a busy collision — replace it
    with the real turn.
    """
    reg = _active_turns(app)
    _existing = reg.get(conv_id)
    if not (_existing is not None and _existing.placeholder) and _is_turn_running(
        app, conv_id
    ):
        await gen.aclose()
        raise TurnAlreadyRunning(f"turn already running for {conv_id}")
    turn = _ActiveTurn(conversation_id=conv_id)
    reg[conv_id] = turn
    turn.task = asyncio.create_task(_run_turn_detached(app, gen, turn))
    if hub is not None:
        try:
            await hub.broadcast_json({"type": "turn_active", "conversation_id": conv_id})
        except Exception:
            log.debug("turn_active broadcast failed (conv=%s)", conv_id, exc_info=True)
    return turn


async def _start_detached_chat_turn(
    request: Request,
    body: ChatRequest,
    *,
    gates: Any | None = None,
    tts: str | None = None,
    client_ip: str | None = None,
) -> _ActiveTurn:
    """Start a detached turn (no HTTP follower — dequeue / background)."""
    # ``_run_turn_gates`` is a patch surface (tests monkeypatch ``routes.chat``), so it
    # is read late via ``_chatpkg``; the SSE/persist helpers are proper module imports.
    from akana_server.api.routes import chat as _chatpkg

    # The caller (post_chat_stream / drain) may have RESOLVED the conv_id and
    # persisted it via ensure_conversation; gates.body must not overwrite this id.
    # gates.body only carries CONTENT transforms (plan exec_text) — its
    # conversation_id is None when the client sent it empty. So conv_id is resolved
    # first from the INCOMING body, then gates.body, and lastly a fresh ULID;
    # otherwise a second fresh ULID generated here is never ensured, so
    # _conversation_chat_usable returns False and the turn would fail with
    # "conversation not usable".
    incoming_conv_id = (body.conversation_id or "").strip()
    if gates is None:
        gates = await _chatpkg._run_turn_gates(request, body)
    if gates.response is not None:
        raise RuntimeError("queued command response not supported for detached drain")
    body = gates.body
    settings: Settings = request.app.state.settings
    hub = getattr(request.app.state, "event_hub", None)
    if not isinstance(hub, EventHub):
        hub = None
    # b6: on a drained turn the caller passes no tts arg → fall back to the tts carried on the
    # queued body, so a queued voice turn is still spoken.
    tts_lang = _resolve_tts_lang(tts if tts is not None else getattr(body, "tts", None))
    conv_id = incoming_conv_id or (body.conversation_id or "").strip() or str(ulid.new())
    body = body.model_copy(update={"conversation_id": conv_id})
    if not _conversation_chat_usable(request.app, conv_id):
        raise ConversationNotUsable(
            f"conversation not usable for detached turn: {conv_id}"
        )
    gen = _stream_chat_response(
        request,
        settings,
        hub,
        body,
        gates.intent,
        gates.approval_required,
        tts_lang=tts_lang,
        client_ip=client_ip or "",
        skill_plan=gates.skill_plan,
        image_block=gates.image_block,
    )
    return await _claim_and_run(request.app, conv_id, gen, hub)


async def _command_turn_gen(
    request: Request,
    conv_id: str,
    body: ChatRequest,
    resp: ChatResponse,
    approval_required: bool,
) -> AsyncIterator[bytes]:
    """One-shot turn producer for a COMMAND/PLAN response drained from the queue.

    NO LLM turn: it writes the command SSE triple (meta/delta/done) to the buffer.
    BEFORE ``done`` it persists the user + response turns — a detached turn has no
    HTTP follower; after ``turn_completed`` the UI re-fetches the log from the
    server (``reloadConversationLogFromServer`` resets the DOM), so a response that
    is NOT persisted is DELETED on the re-fetch. A persist failure doesn't break the
    turn (best-effort).
    """
    # persist_assistant_turn is a patch surface (read late via _chatpkg);
    # _persist_user_turn_start is a proper module-level import.
    from akana_server.api.routes import chat as _chatpkg

    meta, delta, done = _command_sse_chunks(resp, approval_required)
    yield meta
    if resp.text:
        yield delta
    try:
        user_turn_id = await _persist_user_turn_start(
            request,
            conversation_id=conv_id,
            user_text=body.text,
            lang=body.lang,
        )
        if resp.text and _conversation_chat_usable(request.app, conv_id):
            settings = getattr(request.app.state, "settings", None)
            await _off_loop(
                _chatpkg.persist_assistant_turn,
                conversation_id=conv_id,
                assistant_text=resp.text,
                user_turn_id=user_turn_id,
                assistant_turn_id=resp.turn_id,
                lang=body.lang,
                intent=resp.intent,
                data_dir=getattr(settings, "data_dir", None),
            )
    except Exception:
        log.warning(
            "queued command turn could not be persisted (conv=%s); the reply is delivered anyway",
            conv_id,
            exc_info=True,
        )
    yield done


async def _start_detached_command_turn(
    request: Request,
    conv_id: str,
    body: ChatRequest,
    gates: _GateResult,
) -> _ActiveTurn:
    """Deliver a command/plan response from the queue as a one-shot detached turn.

    When a drained message produces a gate response (a plan proposal / skill
    approval / file note / command) rather than an LLM turn, ``_start_detached_chat_turn``
    used to reject it with "queued command response not supported" and SILENTLY drop
    the message (the user got 202 but no response ever arrived). Instead, the response
    is published with the same detached machine as a normal turn (``_run_turn_detached``):
    the SSE is written to the buffer, ``turn_completed`` is broadcast, and the next
    queue item is drained automatically.
    """
    app = request.app
    hub = getattr(app.state, "event_hub", None)
    if not isinstance(hub, EventHub):
        hub = None
    resp = gates.response
    if resp is None:  # the caller only calls this when there's a response — defensive
        raise RuntimeError("command turn requires a gate response")
    gen = _command_turn_gen(request, conv_id, body, resp, gates.approval_required)
    return await _claim_and_run(app, conv_id, gen, hub)


def _gate_error_message(exc: Exception) -> str:
    """Human-readable message from a gate exception (HTTPException detail or str)."""
    detail = getattr(exc, "detail", None)
    if isinstance(detail, dict):
        err = detail.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("code")
            if isinstance(msg, str) and msg.strip():
                return msg.strip()
        if isinstance(detail.get("message"), str) and detail["message"].strip():
            return detail["message"].strip()
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    return str(exc) or type(exc).__name__


async def _rescue_dropped_queue_item(
    app: Any,
    request: Request,
    conv_id: str,
    body: ChatRequest,
    exc: Exception,
) -> None:
    """Surface a queued message whose gate chain raised so it is not silently lost.

    The live path returns an HTTP 400 the client renders; the queued path already
    sent a 202 ("queued"), so a gate failure on drain (e.g. an expired upload id →
    _files_gate 400) would otherwise vanish — no SSE, no persisted turn, no error
    card. Mirror the live 400: persist the user turn + a role="error" marker so the
    failure re-renders from the server on the next log re-fetch, and broadcast a
    turn_completed(status="error") so the open client re-fetches. Best-effort — a
    persist/broadcast failure must not stop the rest of the queue from draining.
    """
    try:
        user_turn_id = await _persist_user_turn_start(
            request,
            conversation_id=conv_id,
            user_text=body.text,
            lang=body.lang,
        )
        await _persist_error_turn_end(
            request,
            conversation_id=conv_id,
            error_text=_gate_error_message(exc),
            turn_id=str(ulid.new()),
            lang=body.lang,
        )
    except Exception:
        log.warning(
            "could not persist the dropped queued item's error turn (conv=%s)",
            conv_id,
            exc_info=True,
        )
        user_turn_id = ""
    hub = getattr(app.state, "event_hub", None)
    if isinstance(hub, EventHub):
        try:
            await hub.broadcast_json(
                {
                    "type": "turn_completed",
                    "conversation_id": conv_id,
                    "status": "error",
                }
            )
        except Exception:  # a broadcast failure can't stop the drain
            log.warning(
                "turn_completed(error) broadcast failed for dropped queued item (conv=%s)",
                conv_id,
                exc_info=True,
            )
    _ = user_turn_id  # persisted for the log re-fetch; no follower to hand it to


async def _maybe_drain_queue(app: Any, conversation_id: str) -> None:
    """When a turn finishes/after STOP, start the next queued message as a separate turn."""
    from akana_server.api.routes import chat as _chatpkg

    conv_id = (conversation_id or "").strip()
    if not conv_id or _is_turn_running(app, conv_id):
        return
    # Shutdown: do NOT start a new turn from the queue (the lifespan finally flag).
    # Otherwise a turn completing during shutdown triggers the drain→new-turn recursion
    # and produces a partial write/hanging task while bridge_pool is torn down.
    if getattr(app.state, "chat_shutting_down", False):
        return
    if not _conversation_chat_usable(app, conv_id):
        clear_queue(app, conv_id)
        return
    item = pop_next(app, conv_id)
    if item is None:
        await _broadcast_queue_updated(app, conv_id)
        return
    # b10: reserve the turn slot ATOMICALLY (no await since the _is_turn_running check at the
    # top of this function) so a fresh message arriving during the gate evaluation below is
    # QUEUED (FIFO) instead of overtaking this popped item. The real turn replaces it in
    # _start_detached_*; it is dropped on every early-return/error path here.
    reg = _active_turns(app)
    placeholder = _ActiveTurn(conversation_id=conv_id, placeholder=True)
    reg[conv_id] = placeholder

    def _drop_placeholder() -> None:
        if reg.get(conv_id) is placeholder:
            reg.pop(conv_id, None)

    try:
        body = ChatRequest.model_validate(item.payload)
    except Exception:
        _drop_placeholder()
        log.warning("queue item invalid (conv=%s id=%s)", conv_id, item.id, exc_info=True)
        await _maybe_drain_queue(app, conv_id)
        return
    req = _synthetic_request(app)
    # The gates run ONCE here (they used to run inside _start_detached_chat_turn);
    # they were pulled out so the response can be inspected UP FRONT and routed to a
    # command/plan turn — there is NO double run, the computed gates are passed to
    # _start_detached_chat_turn. If a gate fails (policy rejection, etc.) the item is
    # already popped: don't get SILENTLY stuck — log it and drain the REST of the queue.
    try:
        gates = await _chatpkg._run_turn_gates(req, body)
    except Exception as gate_exc:
        _drop_placeholder()
        log.warning(
            "queue item dropped at the gate (conv=%s id=%s); surfacing the error and"
            " trying the next",
            conv_id,
            item.id,
            exc_info=True,
        )
        # The client got a 202 for this message → don't let a gate failure vanish;
        # persist the user turn + an error marker and broadcast so it's visible
        # (the queued equivalent of the live path's 400). Best-effort; still drain.
        await _rescue_dropped_queue_item(app, req, conv_id, body, gate_exc)
        await _broadcast_queue_updated(app, conv_id)
        await _maybe_drain_queue(app, conv_id)
        return
    # b10: a STOP that landed during the gate await above cancels our placeholder (marks
    # it cancelled + drops it from the registry) — but only preserves the queue, it does
    # NOT drain. If the placeholder is no longer OUR reserved entry, honor that STOP: put
    # the popped item back at the FRONT (preserved, not auto-run, matching "STOP preserves
    # the queue") and return WITHOUT starting the turn. Otherwise _start_detached_* below
    # would find an empty slot and run the very turn the user just cancelled.
    if reg.get(conv_id) is not placeholder:
        requeue_front(app, conv_id, item)
        await _broadcast_queue_updated(app, conv_id)
        return
    try:
        if gates.response is not None:
            # A command/plan/skill response (no LLM turn) — it used to be silently
            # dropped; now it is persisted and published as a one-shot detached turn.
            await _start_detached_command_turn(req, conv_id, body, gates)
        else:
            await _chatpkg._start_detached_chat_turn(req, body, gates=gates)
    except Exception:
        # The real turn did not take over → drop our slot reservation so the conversation
        # is not left permanently "running" on the placeholder.
        _drop_placeholder()
        # Race shield: in the window between the gate check and the registry set,
        # another incoming message may have grabbed the turn → do NOT LOSE the POPPED
        # item, put it back at the FRONT of the queue so it's re-drained when that turn
        # finishes. ``requeue_front`` (appendleft, capacity-bypass): the old
        # ``enqueue_message`` appended the item to the END (breaking FIFO) + threw 429
        # if the queue was full and DROPPED the item (R3-#6 data loss). It preserves the
        # original item (id + enqueued_at).
        if _is_turn_running(app, conv_id):
            requeue_front(app, conv_id, item)
            await _broadcast_queue_updated(app, conv_id)
        else:
            # The turn didn't start (not a race) → don't let the queue stall, try the next.
            log.exception(
                "queue drain turn could not be started (conv=%s id=%s)", conv_id, item.id
            )
            await _broadcast_queue_updated(app, conv_id)
            await _maybe_drain_queue(app, conv_id)
        return
    await _broadcast_queue_updated(app, conv_id)


async def _run_turn_detached(app: Any, gen: AsyncIterator[bytes], turn: _ActiveTurn) -> None:
    """The turn producer — writes SSE chunks to the buffer, runs independent of the client.

    Cancellation (server shutdown) lands inside the generator as a CancelledError;
    `_stream_chat_response`'s finally persists the partial response. An unexpected
    producer failure lands in the buffer as an `error` event — so followers don't
    hang.
    """

    # Each detached turn gets its OWN fresh trace_id (reuse=False): so consecutive
    # turns via drain don't copy the context through `_spawn_background` and INHERIT
    # each other's id. trace_id flows from here through stream+persist+meta.
    begin_turn(turn.conversation_id or None, mode="stream", reuse=False)
    log.info("turn started [stream] conv=%s", turn.conversation_id)
    status = "ok"
    try:
        async for chunk in gen:
            await _append_chunk(turn, chunk)
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    except Exception:
        status = "error"
        log.exception("detached chat turn crashed (conv=%s)", turn.conversation_id)
        await _append_chunk(
            turn,
            _sse_pack(
                "error",
                {"code": "TURN_CRASHED", "message": "The server could not complete the turn."},
            ).encode("utf-8"),
        )
    finally:
        turn.status = status
        async with turn.cond:
            turn.done = True
            turn.cond.notify_all()
        reg = _active_turns(app)
        if reg.get(turn.conversation_id) is turn:
            reg.pop(turn.conversation_id, None)
        if status != "cancelled":
            hub = getattr(app.state, "event_hub", None)
            if isinstance(hub, EventHub):
                payload: dict[str, Any] = {
                    "type": "turn_completed",
                    "conversation_id": turn.conversation_id,
                    "status": status,
                }
                if turn.assistant_turn_id:
                    payload["assistant_turn_id"] = turn.assistant_turn_id
                try:
                    await hub.broadcast_json(payload)
                except Exception:  # a broadcast failure can't break the persisted turn
                    log.warning(
                        "turn_completed broadcast failed (conv=%s)",
                        turn.conversation_id,
                        exc_info=True,
                    )
            # Do NOT SPAWN a drain during shutdown: a normally-completing turn's
            # (status != cancelled) finally could refill the drained task set and spawn
            # a new turn during shutdown. Skip if the flag is set (shutdown_background_tasks
            # already cancels in-flight ones; the queue is recovered at server startup).
            if not getattr(app.state, "chat_shutting_down", False):
                _spawn_background(app, _maybe_drain_queue(app, turn.conversation_id))
        else:
            await _broadcast_queue_updated(app, turn.conversation_id)


async def _follow_turn(turn: _ActiveTurn) -> AsyncIterator[bytes]:
    """HTTP follower watching the buffer — if the client disconnects, only this closes.

    Safe for multiple followers: each follower reads from its own ``idx`` cursor
    and waits for a new chunk via ``cond``. The resume endpoint can attach several
    followers to the same turn — each replays the buffer from the start and
    continues live, and when one disconnects the others are unaffected.
    """
    idx = 0
    while True:
        # Lock the buffer outside of waiting: take a snapshot length, then don't hold
        # the lock while yielding (holding the lock during yield would block the other
        # follower/producer).
        async with turn.cond:
            while idx >= len(turn.chunks) and not turn.done:
                await turn.cond.wait()
            end = len(turn.chunks)
            done = turn.done
        while idx < end:
            chunk = turn.chunks[idx]
            idx += 1
            if chunk:
                yield chunk
        if done and idx >= end:
            return


async def shutdown_active_turns(app: Any) -> None:
    """Lifespan shutdown: cleanly cancel running turns (the partial persist is preserved)."""
    reg = getattr(app.state, "active_turns", None)
    if not isinstance(reg, dict) or not reg:
        return
    turns = [t for t in reg.values() if isinstance(t, _ActiveTurn)]
    settings = getattr(app.state, "settings", None)
    tasks = [t.task for t in turns if t.task is not None and not t.task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    if isinstance(settings, Settings):
        for turn in turns:
            await _close_bridge_session(settings, turn.conversation_id)
    reg.clear()
    clear_all = getattr(app.state, "chat_turn_queues", None)
    if isinstance(clear_all, dict):
        clear_all.clear()


async def shutdown_background_tasks(app: Any) -> None:
    """Lifespan shutdown: cleanly cancel the fire-and-forget chat background tasks.

    Tasks started via ``_spawn_background`` (the memory-capture 2nd LLM call + queue
    drain) are held in ``app.state.chat_background_tasks``. Shutdown did NOT touch
    them at all → when the ``shutdown_bridge_pool`` daemon was torn down beneath them
    they kept running and produced a dead-daemon error / partial write / "Task was
    destroyed but pending"; the drain task could even spawn a NEW turn during shutdown.
    Since capture is best-effort, cancellation is the correct semantics (we don't block
    shutdown on a 2nd LLM call). Must be called BEFORE the bridge pool shutdown."""
    reg = getattr(app.state, "chat_background_tasks", None)
    if not isinstance(reg, set) or not reg:
        return
    tasks = [t for t in list(reg) if not t.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    reg.clear()


async def _cancel_active_turn_impl(
    app: Any, conv_id: str, *, await_cancel: bool = True
) -> bool:
    """Cancel the running turn; the queue is untouched. (#3) If there's no streaming
    turn, it cancels the registered blocking/voice turn — since the busy-guard prevents
    both from existing at once, one or the other is present.

    ``await_cancel`` (default True): WAIT up to ``_CANCEL_AWAIT_TIMEOUT`` for the turn's
    finally (partial-response persist + bridge abort) to complete — needed for STOP (so
    the partial response is preserved) + reset (so the write finishes BEFORE the reset).
    The DELETE path passes ``False``: the conversation is already being deleted +
    tombstoned (late writes are blocked), and waiting for a mid-LLM turn cancel's finally
    would block the delete for 5-6 sec → don't wait; the cancel finishes in the
    background and the delete returns immediately."""
    from akana_server.api.routes import chat as _chatpkg

    turn = _active_turns(app).get(conv_id)
    if turn is not None and turn.placeholder:
        # b10: a queue-drain reserved this slot with a not-done placeholder (task=None) and
        # is AWAITING its gates. A placeholder is NOT a running turn — do not fall through to
        # the streaming-cancel machinery (there is no task/bridge run to interrupt). Mark it
        # cancelled + drop it so the owning drain detects (reg.get(conv_id) is not placeholder)
        # that STOP landed and does NOT start the queued turn; the item is preserved at the
        # front of the queue (STOP does not drain). STOP genuinely took effect → return True.
        async with turn.cond:
            turn.status = "cancelled"
            turn.done = True
            turn.cond.notify_all()
        reg = _active_turns(app)
        if reg.get(conv_id) is turn:
            reg.pop(conv_id, None)
        return True
    if turn is None or turn.done:
        # No streaming turn → try to cancel the registered blocking/voice turn (#3).
        return await _cancel_nonstreaming_turn(app, conv_id, await_cancel=await_cancel)
    task = turn.task
    if task is not None and not task.done():
        task.cancel()
    # b9: free the conversation IMMEDIATELY (mark done + drop the registry entry) BEFORE
    # awaiting the cancelled task's partial-persist finally, so a STOP + immediate resend
    # starts a FRESH turn instead of being queued and then orphaned (STOP does not drain).
    # The await below only lets the task's own finally finish; it does not un-cancel it.
    if not turn.done:
        async with turn.cond:
            turn.status = "cancelled"
            turn.done = True
            turn.cond.notify_all()
    reg = _active_turns(app)
    if reg.get(conv_id) is turn:
        reg.pop(conv_id, None)
    # DELETE (await_cancel=False): do NOT WAIT for the finally — so the delete returns immediately.
    if task is not None and not task.done() and await_cancel:
        # asyncio.wait does NOT re-cancel the task on timeout — wait_for would, and
        # that would cut short the partial-response persist in the turn's finally.
        done_set, _ = await asyncio.wait({task}, timeout=_CANCEL_AWAIT_TIMEOUT)
        if not done_set:
            log.warning(
                "cancel: the turn's finally did not finish within %ss (conv=%s); it continues in the background",
                _CANCEL_AWAIT_TIMEOUT,
                conv_id,
            )
        elif not task.cancelled():
            exc = task.exception()
            if exc is not None:
                log.debug("cancel: task await failure (conv=%s): %s", conv_id, exc)
    settings = getattr(app.state, "settings", None)
    # Backstop-abort the cancelled turn's bridge run — but ONLY if no fresh turn has claimed
    # the conversation during the cancel window (a resend owns its own run, which must not be
    # aborted; the bridge pool's active-run retry recovers the old run in that case).
    if isinstance(settings, Settings) and not _is_turn_running(app, conv_id):
        await _chatpkg._abort_bridge_run_for_conversation(settings, conv_id)
    return True


async def cleanup_conversation_chat_state(
    app: Any, conversation_id: str, *, tombstone: bool = True
) -> None:
    """Cancel the running turn + clear the queue (the shared chat delete/reset machine).

    ``tombstone``: add a PERMANENT tombstone (the default — for the DELETE path; so a
    late write-back doesn't resurrect the soft-delete). The RESET path (``DELETE /chat/...``,
    the chat STAYS) passes ``tombstone=False``: since the tombstone is permanent (never
    removed), adding it on reset would make the conversation permanently unusable. On
    reset, stopping the running turn + clearing the queue is enough; reset DROPS the
    turns anyway (cancel awaited → the partial write finishes before the reset)."""
    conv_id = (conversation_id or "").strip()
    if not conv_id:
        return
    if tombstone:
        _chat_cleanup_tombstones(app).add(conv_id)
    # DELETE (tombstone=True): cancel the turn but do NOT WAIT for its finally → the
    # delete returns immediately (the 5-6 sec wait of a mid-LLM turn cancel is gone;
    # the tombstone blocks late writes). RESET (tombstone=False): await — so the partial
    # write finishes BEFORE the reset and the reset cleans it up too (order guarantee).
    await _cancel_active_turn_impl(app, conv_id, await_cancel=not tombstone)
    clear_queue(app, conv_id)
    await _broadcast_queue_updated(app, conv_id)
