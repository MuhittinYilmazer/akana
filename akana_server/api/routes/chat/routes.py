"""HTTP route handlers for the chat package (the ARCH-03 split).

The 8 chat endpoints + their small response models used to live in ``__init__.py``
alongside the backward-compat re-export hub. They moved here so ``__init__`` stays thin
(router mount + the patch-surface re-exports) and importing a chat submodule no longer
drags in the full route module. Behavior is UNCHANGED: the ``router`` object, every
route path/signature/dependency, and the decorator order are preserved verbatim.

SEAM (ARCH-01): the genuine test/voice patch surface (``_run_turn_gates`` /
``_start_detached_chat_turn``) is read LATE at call time via ``_chatpkg`` — a
``setattr`` on ``routes.chat`` resolves to the same object — exactly as the other
submodules do. All other dependencies are proper module-level imports flowing DOWNWARD
(streaming/chat_detached/turn_core/gates/persist/_base/models), so no module-level
back-import into the package is introduced.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import ulid
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from akana_server.api.deps import require_akana_bearer
from akana_server.api.services import AppServices, get_services
from akana_server.api.chat_turn_queue import (
    enqueue_message,
    queue_depth,
    queue_snapshot,
)
from akana_server.audit import write_event as audit_write
from akana_server.config import Settings
from akana_server.llm_settings import (
    load_llm_settings,
    resolve_provider,
)
from akana_server.memory_core import get_memory_core
from akana_server.conversation_service import ConversationService
from akana_server.events import EventHub
from akana_server.observability import begin_turn, registry, update_turn
from akana_server.chat_context import (
    async_llm_dropped_turns,
    bind_conversation_llm,
    conversation_service,
    effective_llm_settings,
    ensure_conversation,
    get_agent_id,
    make_bootstrap_retry_hooks,
    persist_agent_id,
    snapshot_conversation_llm,
)
from akana_server.context import ContextRequest
from akana_server.orchestrator.bridge_pool import (
    cursor_reuse_agent_enabled,
)
from akana_server.orchestrator.memory_tools import memory_mcp_servers
from akana_server.tools.gateway import _tool_name

from akana_server.api.routes.chat._base import (
    _active_cursor_model,
    _client_ip,
    _context_request,
    _off_loop,
    build_context_assembler,
    guard_nonstreaming_turn,
    voice_turn_suffix,
)
from akana_server.api.routes.chat.models import (
    ChatRequest,
    ChatResponse,
    TokenUsage,
)
from akana_server.api.routes.chat.persist import (
    _mirror_cursor_agent_meta,
    _persist_assistant_turn_end,
    _persist_user_turn_start,
    _record_tool_calls,
)
from akana_server.api.routes.chat.turn_core import (
    TurnError,
    run_nonstreaming_turn,
)
from akana_server.api.routes.chat.streaming import (
    _abort_bridge_run_for_conversation,
    _active_turns,
    _broadcast_queue_updated,
    _cancel_active_turn_impl,
    _conversation_chat_usable,
    _follow_turn,
    _is_turn_running,
    _maybe_drain_queue,
    _reset_cursor_bridge_for_conversation,
    _spawn_background,
    _sse_command_response,
    cleanup_conversation_chat_state,
)
from akana_server.api.routes.chat.chat_detached import (
    ConversationNotUsable,
    TurnAlreadyRunning,
)

log = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


async def _persist_command_response(
    request: Request, body: ChatRequest, resp: ChatResponse
) -> None:
    """Persist the user turn + a LIVE gate/command response before returning it.

    Symmetric with the DRAINED command path (_command_turn_gen): a gate response (the
    _files_gate unsupported-attachment rejection) has NO LLM turn, but the FE re-fetches
    the conversation log from the server on a chat switch / F5 — a response that is NOT
    persisted is DELETED on the re-fetch, dropping BOTH the user's message and the reply.
    The blocking (POST /chat) and streaming (POST /chat/stream) surfaces both funnel here
    so the three surfaces share one persist contract. A persist failure never breaks the
    response (best-effort; mirrors _command_turn_gen).
    """
    from akana_server.api.routes import chat as _chatpkg

    conv_id = (resp.conversation_id or body.conversation_id or "").strip()
    if not conv_id:
        return
    settings = getattr(request.app.state, "settings", None)
    try:
        # _persist_user_turn_start ensures a fresh (not-yet-ensured) conversation before
        # writing → the usability gate below then sees it as usable (same order as
        # _command_turn_gen). A tombstoned/soft-deleted conv is intentionally skipped there.
        user_turn_id = await _persist_user_turn_start(
            request,
            conversation_id=conv_id,
            user_text=body.text,
            lang=body.lang,
            file_ids=body.effective_file_ids,
        )
        if resp.text and _conversation_chat_usable(request.app, conv_id):
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
            "live gate/command response could not be persisted (conv=%s); reply delivered anyway",
            conv_id,
            exc_info=True,
        )


@router.post("/chat", dependencies=[Depends(require_akana_bearer)])
@guard_nonstreaming_turn(lambda a: getattr(a.get("body"), "conversation_id", None))
async def post_chat(
    body: ChatRequest,
    request: Request,
    services: AppServices = Depends(get_services),
) -> ChatResponse:
    # ``_run_turn_gates`` is a patch surface (tests monkeypatch ``routes.chat``), so it
    # is read late via ``_chatpkg``; every other dependency is a proper module import.
    from akana_server.api.routes import chat as _chatpkg

    begin_turn(
        (body.conversation_id or "").strip() or None,
        mode="voice" if body.voice else "blocking",
    )
    # The busy-guard is in the guard_nonstreaming_turn decorator (above) — do NOT
    # re-check busy here: the decorator already registered the conv, so a second
    # check would see its own turn as busy and reject itself with 409 (self-409).
    gates = await _chatpkg._run_turn_gates(request, body)
    if gates.response is not None:
        # A live gate response (e.g. the _files_gate rejection) has no LLM turn — persist
        # the user + response turns so they survive the FE's re-fetch (parity with the
        # drained command path); otherwise both vanish on a chat switch / F5.
        await _persist_command_response(request, gates.body, gates.response)
        return gates.response
    body = gates.body
    intent = gates.intent
    approval_required = gates.approval_required
    skill_plan = gates.skill_plan

    settings: Settings = services.settings
    conv_id = body.conversation_id or str(ulid.new())
    # The busy-guard (Convergence A #2) is in the _blocking_busy_guard yield-dependency
    # (a concurrent second turn on an existing conv gets 409; the release runs on every exit).
    await _off_loop(ensure_conversation, request, conv_id)
    with bind_conversation_llm(request, conv_id):
        await _off_loop(snapshot_conversation_llm, request, conv_id)
        _llm = effective_llm_settings(request, conv_id)
        provider = resolve_provider(settings, _llm) if _llm is not None else None
        update_turn(conversation_id=conv_id, provider=provider)
        log.info(
            "turn started [blocking] conv=%s intent=%s provider=%s",
            conv_id,
            intent,
            provider or "?",
        )
        user_turn_id = str(ulid.new())

        # ContextEngine F0: persona + skill block + memory + history are assembled
        # at ONE gate; the trace is the answer to "why was the context like this".
        assembled = await build_context_assembler(request).assemble(
            _context_request(
                body,
                conv_id,
                skill_plan=skill_plan,
                image_block=gates.image_block,
            )
        )
        history_msgs = assembled.history
        user_for_llm = assembled.user_text
        if body.voice:
            # Voice mode: directive appended to the LLM prompt only (the stored user
            # message stays as body.text → no log pollution). Body = editable +
            # bilingual voice directive (persona registry); follows the language
            # picker. Blocking path → no streaming opening-words line. Resolved
            # off-loop: get_voice_directive() reads the persona store (sqlite).
            voice_suffix = await _off_loop(voice_turn_suffix, settings, streaming=False)
            user_for_llm = f"{user_for_llm}\n\n{voice_suffix}"
        # NOTE (#5 orphan-turn): the user turn is NOT persisted BEFORE the LLM CALL.
        # Otherwise, when ``complete_chat_with_usage`` raises LLMCallError (raised
        # below), only the user turn has been written, leaving an "orphan/dangling"
        # turn with no assistant turn + the conversation meta counter off by 1.
        # Instead, AFTER success the user + assistant turns are written together (the
        # same contract as post_voice 5c2ddd4). On the blocking path early-persist has
        # no UI benefit (the response returns in one piece anyway); this turn's history
        # was already assembled above (ContextAssembler).
        agent_id = await _off_loop(get_agent_id, request, conv_id)
        reuse_agent = cursor_reuse_agent_enabled()
        bootstrap_loader, bootstrap_hook = make_bootstrap_retry_hooks(request, conv_id)

        t0 = time.perf_counter()
        # SHARED TURN CORE: the blocking path runs the LLM through the same
        # single-turn core as (eventually) the voice route, so it now has the streaming
        # producer's safeguards — empty-response retry + BreakerOpenError → LLM_RATE_LIMITED
        # mapping + file_ids — instead of a drifted hand-rolled call. Convergence A #6/#7:
        # complete_chat_with_usage (chat_mode) collects the STREAMING bridge →
        # usage["tool_calls"] + usage["agent_id"] (reuse → no cold-start).
        try:
            outcome = await run_nonstreaming_turn(
                settings,
                user_for_llm,
                history=history_msgs,
                model=_active_cursor_model(request),
                conversation_id=conv_id,
                agent_id=agent_id,
                reuse_agent=reuse_agent,
                mcp_servers=memory_mcp_servers(settings, conv_id),
                # Persona F1 binding: with the default akana it's None → the client
                # applies today's CHAT_SYSTEM_PREFIX (behavior-neutral); if there's a
                # real binding, the resolved persona system prompt goes instead.
                system_prompt=assembled.system_prompt_override,
                # gemini/openai NATIVE image input (inline_data / image_url).
                file_ids=body.effective_file_ids,
                bootstrap_history_loader=bootstrap_loader,
                on_bootstrap_retry=bootstrap_hook,
                context_mode=(
                    "resume" if assembled.history_skipped_resume else "bootstrap"
                ),
                on_active_run_reset=lambda: _reset_cursor_bridge_for_conversation(
                    request.app, conv_id
                ),
            )
        except TurnError as e:
            raise HTTPException(
                status_code=e.status_code,
                detail={"error": {"code": e.code, "message": e.message}},
            ) from e
        text = outcome.text
        usage = outcome.usage
        tool_calls = outcome.tool_calls
        # #6: persist the agent_id the bridge returned → the next blocking turn reuses
        # it (it used to be a cold-start every turn since one-shot didn't return agent_id).
        if outcome.agent_id:
            # persist_agent_id writes agent_id WITH its agent_provider tag (read from the
            # bind_conversation_llm snapshot bound above). Without it, get_agent_id's leak-guard
            # defaults a tagless id to 'cursor' → a claude blocking turn's session never resumes,
            # and a claude uuid could later leak into a cursor resume. Mirror the streaming path
            # (chat_producer.py), which writes BOTH persist_agent_id and the dual-write mirror.
            # Off-load: both run a locked memory.db UPDATE txn (busy_timeout=10000). On the loop
            # they would freeze every SSE/WS/HTTP endpoint under lock/txn contention (b2h-#4).
            await _off_loop(persist_agent_id, request, conv_id, outcome.agent_id)
            await _off_loop(
                _mirror_cursor_agent_meta, request, conv_id, outcome.agent_id
            )
        latency_ms = int((time.perf_counter() - t0) * 1000)
        registry.observe("turn_latency_ms", latency_ms)
        turn_id = str(ulid.new())

        await _record_tool_calls(
            request,
            settings,
            tool_calls,
            turn_id=turn_id,
            conv_id=conv_id,
            mode="blocking",
        )

        # LLM succeeded: write the user turn NOW (orphan-turn #5 is avoided) — then the
        # assistant turn. In order → user.ts < assistant.ts (list/preview ordered correctly).
        await _persist_user_turn_start(
            request,
            conversation_id=conv_id,
            user_text=body.text,
            lang=body.lang,
            user_turn_id=user_turn_id,
            file_ids=body.effective_file_ids,
        )

        memory_writes = await _persist_assistant_turn_end(
            request,
            conversation_id=conv_id,
            user_text=body.text,
            assistant_text=text,
            user_turn_id=user_turn_id,
            assistant_turn_id=turn_id,
            lang=body.lang,
            latency_ms=latency_ms,
            intent=intent,
            tool_calls=tool_calls,
        )
        # The recount runs AFTER persist_agent_id stored THIS turn's fresh session id,
        # which flips bootstrap_needed to False → for a bootstrap turn the recount reads 0
        # even though this turn actually truncated history to chat_max_turns. Reconcile
        # against the pre-turn assembled count (mirror the streaming producer's
        # max(dropped_before, dropped_after)); assembled.dropped_turns is the "before" term.
        dropped = max(assembled.dropped_turns, await async_llm_dropped_turns(request, conv_id))

        resp = ChatResponse(
            turn_id=turn_id,
            text=text,
            lang=body.lang,
            conversation_id=conv_id,
            history_turns=len(assembled.history),
            dropped_turns=dropped,
            intent=intent,
            approval_required=approval_required,
            tool_calls=tool_calls,
            memory_writes=memory_writes,
            skill_used=skill_plan.used_payload() if skill_plan is not None else [],
            latency_ms=latency_ms,
            tokens=TokenUsage(
                prompt=int(usage.get("prompt_tokens", 0) or 0),
                completion=int(usage.get("completion_tokens", 0) or 0),
            ),
        )
        hub = services.event_hub
        if isinstance(hub, EventHub):
            await hub.broadcast_json(
                {
                    "type": "chat_done",
                    "turn_id": turn_id,
                    "conversation_id": conv_id,
                    "intent": intent,
                    "approval_required": approval_required,
                    "tool_calls_count": len(tool_calls),
                    "latency_ms": latency_ms,
                    "preview": text[:400],
                }
            )
        await _off_loop(
            audit_write,
            settings.data_dir,
            "chat",
            turn_id=turn_id,
            conv_id=conv_id,
            client_ip=_client_ip(request),
            data={
                "mode": "blocking",
                "intent": intent,
                "approval_required": approval_required,
                "skill_used": [
                    e.get("id") for e in (skill_plan.used_payload() if skill_plan else [])
                ],
                "latency_ms": latency_ms,
                "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                "tool_calls": [
                    _tool_name(c) if isinstance(c, dict) else "?"
                    for c in tool_calls
                    if isinstance(c, dict)
                ],
                "user_preview": body.text[:200],
                "assistant_preview": text[:200],
                "dropped_turns": dropped,
            },
        )
        return resp


class ConversationTurnOut(BaseModel):
    role: str
    content: str
    ts: float


class ConversationOut(BaseModel):
    conversation_id: str
    turns: list[ConversationTurnOut]
    dropped_turns: int = 0


def _message_ts(created_at: str | None) -> float:
    if not created_at:
        return time.time()
    try:
        from datetime import datetime

        normalized = created_at.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except (ValueError, TypeError):
        return time.time()


@router.get(
    "/chat/conversations/{conversation_id}",
    dependencies=[Depends(require_akana_bearer)],
)
async def get_conversation(
    conversation_id: str,
    request: Request,
    services: AppServices = Depends(get_services),
) -> ConversationOut:
    """Return full episodic history for UI restore (compat shim — prefer /conversations/{id}/messages)."""
    settings: Settings = services.settings
    llm = await _off_loop(load_llm_settings, settings.data_dir, settings)
    max_turns = llm.chat_max_turns
    svc = conversation_service(request)
    meta = (
        await _off_loop(svc.get, conversation_id)
        if isinstance(svc, ConversationService)
        else None
    )
    if meta is None or svc is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Conversation not found."}},
        )
    messages = await _off_loop(svc.list_messages, conversation_id, limit=500)
    dropped = max(0, int(meta.message_count) - max_turns)
    return ConversationOut(
        conversation_id=conversation_id,
        turns=[
            ConversationTurnOut(
                role=m.role,
                content=m.content,
                ts=_message_ts(m.created_at),
            )
            for m in messages
            if m.role in ("user", "assistant")
        ],
        dropped_turns=dropped,
    )


@router.delete(
    "/chat/conversations/{conversation_id}",
    dependencies=[Depends(require_akana_bearer)],
    status_code=204,
)
async def reset_conversation(
    conversation_id: str,
    request: Request,
    services: AppServices = Depends(get_services),
) -> None:
    """Drop a conversation's episodic turns; idempotent (returns 204 either way)."""
    # #7: cancel the running turn + clear the queue (reset → NO tombstone; the chat
    # STAYS). Otherwise, on a mid-stream reset the running turn and queue-drain would
    # write back to the "cleared" conv and break the "I cleared the history" intent
    # (symmetric with DELETE /conversations).
    await cleanup_conversation_chat_state(
        request.app, conversation_id, tombstone=False
    )
    # Reset must ALSO drop the provider agent session — symmetric with DELETE
    # /conversations. Otherwise reuse (the default) RESUMES the surviving agent_id on the
    # next turn (CONTEXT_MODE_RESUME, no history re-send), so the provider-side agent still
    # holds the entire "cleared" conversation and breaks the "I cleared the history" intent.
    from akana_server.chat_context import clear_agent_id
    from akana_server.orchestrator.bridge_pool import (
        bridge_daemon_enabled,
        get_bridge_pool,
    )

    await _off_loop(clear_agent_id, request, conversation_id)
    await _off_loop(
        get_memory_core(services.settings.data_dir).reset_conversation,
        conversation_id,
    )
    if bridge_daemon_enabled():
        await get_bridge_pool(services.settings).close_session(conversation_id)
    return None


@router.get("/context/preview", dependencies=[Depends(require_akana_bearer)])
async def get_context_preview(
    request: Request, conversation_id: str, text: str = ""
) -> dict[str, Any]:
    """Assembled context preview (ContextEngine F0) — the "Context preview" backend.

    Runs the SAME assembler as a real turn: persona resolution, the history window,
    memory injection (over the ``text`` sample), and budget. Side-effect-free: the
    skill gate (which writes a decision to policy.db) and the plan gate are NOT run
    in preview; no record is CREATED for an unknown conversation (404).
    """
    conv_id = (conversation_id or "").strip()
    if not conv_id:
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "CONVERSATION_REQUIRED",
                    "message": "No conversation specified (the conversation_id parameter is required).",
                }
            },
        )
    svc = conversation_service(request)
    if svc is not None and await _off_loop(svc.get, conv_id) is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {"code": "NOT_FOUND", "message": "Conversation not found."}
            },
        )
    assembled = await build_context_assembler(request).assemble(
        ContextRequest(
            text=(text or "").strip()[:32000],
            conversation_id=conv_id,
            channel="web",
        )
    )
    return {
        "conversation_id": conv_id,
        "persona": {
            "id": assembled.persona_id,
            "source": assembled.persona_source,
            "default": assembled.system_prompt_is_default,
        },
        "system_prompt": assembled.system_prompt,
        "history": assembled.history,
        "history_turns": len(assembled.history),
        "dropped_turns": assembled.dropped_turns,
        "user_text": assembled.user_text,
        "injected_blocks": assembled.injected_blocks,
        "trace": assembled.trace,
    }


@router.get(
    "/chat/queue/{conversation_id}",
    dependencies=[Depends(require_akana_bearer)],
)
async def get_chat_queue(conversation_id: str, request: Request) -> dict[str, Any]:
    """The pending message queue (in memory; empty after a restart)."""
    conv_id = (conversation_id or "").strip()
    if not conv_id:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "BAD_REQUEST", "message": "No conversation specified (conversation_id is required)."}},
        )
    return queue_snapshot(request.app, conv_id)


async def _queue_stream_message(
    request: Request,
    conv_id: str,
    body: ChatRequest,
    tts: str | None,
) -> JSONResponse:
    """Queue a stream message behind a running turn → 202 (the shared queue path).

    Both queue paths in ``post_chat_stream`` (the busy pre-check and the
    ``TurnAlreadyRunning`` race) funnel here: carry the resolved TTS language on the
    queued body (b6 — so a drained voice turn is still spoken), enqueue, then re-arm the
    drain (b2 — the running turn may have finished and drained the then-empty queue during
    the awaits, orphaning the just-enqueued item; ``_maybe_drain_queue`` is idempotent),
    broadcast, and return the 202 envelope.
    """
    _q_body = body.model_copy(update={"tts": tts}) if tts is not None else body
    item = enqueue_message(request.app, conv_id, _q_body.model_dump(mode="json"))
    if not _is_turn_running(request.app, conv_id):
        _spawn_background(request.app, _maybe_drain_queue(request.app, conv_id))
    await _broadcast_queue_updated(request.app, conv_id)
    return JSONResponse(
        status_code=202,
        content={
            "queued": True,
            "item_id": item.id,
            "depth": queue_depth(request.app, conv_id),
            "conversation_id": conv_id,
        },
    )


@router.post(
    "/chat/stream",
    dependencies=[Depends(require_akana_bearer)],
    response_model=None,
)
async def post_chat_stream(
    body: ChatRequest, request: Request, tts: str | None = None
) -> StreamingResponse | JSONResponse:
    """Streaming chat (SSE). Same input shape as POST /chat.

    Query: `?tts=tr|en|auto|false` enables sentence-level Piper streaming TTS.
    The turn runs as a server-side task — a client disconnect does not cancel the
    turn (UNBREAKABLE RESPONSE; see `_run_turn_detached` for details).

    A message arriving while a turn is in progress is queued with **202**, not 409.
    """
    # ``_run_turn_gates`` / ``_start_detached_chat_turn`` are patch surfaces (tests
    # monkeypatch ``routes.chat``), so they are read late via ``_chatpkg``.
    from akana_server.api.routes import chat as _chatpkg

    begin_turn(
        (body.conversation_id or "").strip() or None,
        mode="voice" if body.voice else "stream",
    )
    conv_id_pre = (body.conversation_id or "").strip()
    if conv_id_pre and _is_turn_running(request.app, conv_id_pre):
        if not _conversation_chat_usable(request.app, conv_id_pre):
            raise HTTPException(
                status_code=404,
                detail={
                    "error": {
                        "code": "NOT_FOUND",
                        "message": "Conversation not found.",
                    }
                },
            )
        # Pre-LLM command short-circuits were removed — there are no commands to run
        # instantly while busy, so a message arriving mid-turn is simply queued and
        # drained as a normal LLM turn when the running turn finishes.
        await _off_loop(ensure_conversation, request, conv_id_pre)
        return await _queue_stream_message(request, conv_id_pre, body, tts)

    gates = await _chatpkg._run_turn_gates(request, body)
    resp = gates.response
    approval_required = gates.approval_required
    if resp is not None:
        # A live gate response (e.g. the _files_gate rejection) has no LLM turn — persist
        # the user + response turns BEFORE streaming so they survive the FE's re-fetch
        # (parity with the drained command path); otherwise both vanish on a switch / F5.
        await _persist_command_response(request, gates.body, resp)
        return _sse_command_response(resp, approval_required)

    body = gates.body
    conv_id = (body.conversation_id or "").strip() or str(ulid.new())
    body = body.model_copy(update={"conversation_id": conv_id})
    await _off_loop(ensure_conversation, request, conv_id)
    if not _conversation_chat_usable(request.app, conv_id):
        # A stream message to a deleted/tombstoned conversation (while NO turn is
        # running) — e.g. multi-tab: another tab deleted this chat, this tab is still
        # writing to the old id. PARITY with the 404 on the busy path; otherwise
        # `_start_detached_chat_turn`'s "not usable" RuntimeError turns into a 500 in
        # path-3 (the client couldn't handle a clean 404 and fall back to a new session).
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Conversation not found."}},
        )
    try:
        turn = await _chatpkg._start_detached_chat_turn(
            request,
            body,
            gates=gates,
            tts=tts,
            client_ip=_client_ip(request),
        )
    except ConversationNotUsable as e:
        # Race: during start, another tab deleted this chat (tombstone + soft-delete).
        # The turn is NOT running → it can't be converted to the busy path; give a
        # clean NOT_FOUND with parity to the no-turn 404 above (a bare RuntimeError
        # used to turn into a 500 and the client couldn't fall back to a new session).
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Conversation not found."}},
        ) from e
    except TurnAlreadyRunning:
        # Race: two concurrent FIRST messages arrived for the same conv_id; one grabbed
        # the registry, the other fell through with "turn already running". In this case,
        # instead of 500, give the same 202 behavior via the busy path (queue it). If the
        # turn is NOT actually running, defensively re-raise (the registration race may
        # have resolved).
        if _is_turn_running(request.app, conv_id):
            return await _queue_stream_message(request, conv_id, body, tts)
        raise
    return StreamingResponse(
        _follow_turn(turn),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/chat/active/{conversation_id}/cancel",
    dependencies=[Depends(require_akana_bearer)],
)
async def cancel_chat_active(conversation_id: str, request: Request) -> dict[str, Any]:
    """STOP — cancel an in-progress detached turn ON THE SERVER.

    Like the Cursor IDE: the running run is interrupted; ``agent_id`` is preserved,
    and the next message goes to the same agent via ``resume``.
    """
    conv_id = (conversation_id or "").strip()
    cancelled = await _cancel_active_turn_impl(request.app, conv_id)
    if not cancelled:
        return {"cancelled": False, "reason": "no_active_turn"}
    # NOTE: STOP intentionally PRESERVES the queue (design K4 — see
    # test_cancel_active_turn_preserves_queue). Queued messages are kept, NOT
    # auto-run, after a stop; the user decides what happens next. Do NOT drain here.
    return {"cancelled": True, "conversation_id": conv_id}


@router.post(
    "/chat/active/{conversation_id}/recover",
    dependencies=[Depends(require_akana_bearer)],
)
async def recover_chat_bridge(
    conversation_id: str,
    request: Request,
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    """Orphan active-run — softly cancel the bridge run while no turn is in the registry.

    Just like stopping and typing again in the Cursor IDE preserves context; this
    endpoint interrupts the stuck run in the bridge with the same logic, without
    deleting ``agent_id``. If it's still stuck, the client can request a hard reset
    via ``?hard=1``.
    """
    conv_id = (conversation_id or "").strip()
    if not conv_id:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "BAD_REQUEST", "message": "No conversation specified (conversation_id is required)."}},
        )
    hard = str(request.query_params.get("hard") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    settings: Settings = services.settings
    if hard:
        await _reset_cursor_bridge_for_conversation(request.app, conv_id)
        return {"recovered": True, "conversation_id": conv_id, "mode": "hard_reset"}
    # BK2: if there IS a REGISTERED (stuck) turn, cancel it first → release the registry
    # busy. Soft recover used to only interrupt the bridge run and NOT TOUCH
    # ``_active_turns``/``_nonstreaming_busy`` → if a turn was stuck in the registry it
    # stayed "busy", the next message was forever TURN_BUSY/queued, and "recover" appeared
    # to do nothing (the cancel endpoint clears the registry; recover-soft didn't).
    # ``await_cancel=False``: do NOT WAIT for the stuck turn's finally — so the un-stick
    # returns fast. If there's no registered turn (the real target: an ORPHAN bridge run),
    # this is a no-op → the work finishes in ``abort_run`` below (idempotent).
    await _cancel_active_turn_impl(request.app, conv_id, await_cancel=False)
    await _abort_bridge_run_for_conversation(settings, conv_id)
    return {"recovered": True, "conversation_id": conv_id, "mode": "abort_run"}


@router.get(
    "/chat/active/{conversation_id}",
    dependencies=[Depends(require_akana_bearer)],
    response_model=None,
)
async def get_chat_active(
    conversation_id: str, request: Request
) -> StreamingResponse | Response:
    """UNBREAKABLE RESPONSE — reconnect to an in-progress turn (resume).

    When the user navigates away and back, if a turn is still RUNNING in that
    conversation: it REPLAYS the SSE chunks accumulated so far (meta + deltas +
    the `tool_call`s up to that point) and then returns a new follower that LIVE-
    streams the remaining chunks. Multiple followers can watch the same turn
    (an append-only buffer + `cond.notify_all`).

    If there's NO active turn, it returns 204 — the frontend falls back to a
    normal `messages` fetch.
    """
    conv_id = (conversation_id or "").strip()
    turn = _active_turns(request.app).get(conv_id) if conv_id else None
    # A queue-drain reserves the slot with a not-done PLACEHOLDER (task=None, no chunks) while
    # it awaits the gate chain; when the real turn starts it REPLACES the registry entry
    # without ever notifying/marking the placeholder done. A follower attached to a placeholder
    # would await ``cond.wait()`` forever (only STOP marks a placeholder done). Treat it like
    # "no active turn": return 204 so the FE falls back to a normal messages fetch (b2h-#8).
    if turn is None or turn.done or turn.placeholder:
        return Response(status_code=204)
    return StreamingResponse(
        _follow_turn(turn),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
