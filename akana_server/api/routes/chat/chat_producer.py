"""Live SSE producer — persona + history + LLM stream → SSE events (the Step B4-2 split).

The largest seam extracted from the ``streaming.py`` god-file: the
``_stream_chat_response`` generator. The turn machine that runs INDEPENDENT of the
client (``chat_detached``) wraps this producer and writes to the ``_ActiveTurn``
buffer. Its in-package module-level dependencies flow only DOWNWARD: ``chat_state``
(queue + predicate + spawn) and ``chat_bridge`` (reset). The helpers still defined in
``__init__`` (``_sse_pack`` / persist / ledger / tool / ``_context_request`` ...) and
the patched ``stream_user_chat`` are read LATE at call time via ``_chatpkg`` (an
in-function late import to avoid creating a circular import).
"""

from __future__ import annotations

import time
import logging
from collections.abc import AsyncIterator
from typing import Any

import ulid
from fastapi import Request

from akana_server.audit import write_event as audit_write
from akana_server.config import Settings
from akana_server.events import EventHub
from akana_server.observability import current_trace_id, registry
from akana_server.network.breaker import BreakerOpenError
from akana_server.orchestrator.llm_dispatch import LLMCallError
from akana_server.chat_context import (
    CONTEXT_MODE_BOOTSTRAP_RETRY,
    CONTEXT_MODE_RESUME,
    async_llm_dropped_turns,
    async_llm_history_and_dropped,
    clear_agent_id,
    effective_llm_settings,
    ensure_conversation,
    get_agent_id,
    persist_agent_id,
    record_agent_timing_metric,
    record_context_assemble_metrics,
    snapshot_conversation_llm,
)
from akana_server.llm_context import reset_conversation_llm, set_conversation_llm
from akana_server.orchestrator.bridge_pool import (
    _is_active_run_message,
    cursor_reuse_agent_enabled,
)
from akana_server.orchestrator.memory_tools import memory_mcp_servers
from akana_server.skills.turn_injection import SkillTurnPlan
from akana_server.tools.gateway import _tool_name, record_tool_call
from akana_server.voice import (
    TtsError,
    VoiceSelection,
    resolve_tts_voice_path,
    resolve_voice_selection,
)

from akana_server.api.routes.chat._base import (
    _active_cursor_model,
    _client_ip,
    _context_request,
    _off_loop,
    _sse_memory_use,
    _sse_pack,
    build_context_assembler,
    voice_turn_suffix,
)
from akana_server.api.routes.chat.models import ChatRequest
from akana_server.api.routes.chat.chat_state import (
    _conversation_chat_usable,
    _cursor_breaker_open,
    _spawn_background,
    _turn_wrote_memory,
)
from akana_server.api.routes.chat.chat_bridge import (
    _reset_cursor_bridge_for_conversation,
)
from akana_server.api.routes.chat.tts_pipeline import TtsPipeline
# Persistence/capture helpers — imported DOWNWARD at module level (the seam split).
# persist is now below chat_producer (it imports chat_state, not chat_producer), so
# these are proper imports instead of the old call-time reach-up into the package.
from akana_server.api.routes.chat.persist import (
    _accumulate_tool_call,
    _capture_memory_background,
    _mirror_cursor_agent_meta,
    _persist_assistant_turn_end,
    _persist_error_turn_end,
    _persist_user_turn_start,
)

log = logging.getLogger(__name__)


def _ask_user_summary(payload: dict[str, Any] | None) -> str:
    """Produce a readable question summary from the ``ask_user`` payload for persistence.

    On a question turn the assistant TEXT is empty (the card is drawn from the
    structured ``ask_user`` event). To persist the turn anyway, we reduce the question
    texts to plain text → so the reload has no "answerless user turn" (dangling-user)
    and the history bootstrap stays consistent. An empty/corrupt payload → "" (the
    turn is not persisted).
    """
    if not isinstance(payload, dict):
        return ""
    lines: list[str] = []
    for q in payload.get("questions") or []:
        if not isinstance(q, dict):
            continue
        text = str(q.get("question") or "").strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def _plan_summary(payload: dict[str, Any] | None) -> str:
    """Produce a readable plan text from the ``plan`` payload for persistence.

    On a plan turn the assistant TEXT is empty (the card is drawn from the structured
    ``plan`` event). To persist the turn anyway, we store the plan markdown → so the
    reload has no "answerless user turn" (dangling-user). An empty/corrupt payload → "".
    """
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("plan") or "").strip()


def _tool_only_summary(tool_calls: list[dict[str, Any]]) -> str:
    """Placeholder body for a tool-only turn (tool calls, no final assistant text).

    Some provider runs execute tools and end with no assistant text (cursor runs
    ending textless, late-aborted runs). The real content is the tool cards, which
    ride on ``tool_calls``; this non-empty body only exists so the assistant turn
    is PERSISTED (persist_assistant_turn drops an empty body), avoiding a dangling
    user turn + lost tool cards on reload — the same reason ask_user/plan turns get
    a summary. Not localized (like those summaries): it lists the tool names run.
    """
    names = [
        _tool_name(c)
        for c in tool_calls
        if isinstance(c, dict) and _tool_name(c) != "?"
    ]
    label = ", ".join(dict.fromkeys(names)) if names else str(len(tool_calls))
    return f"[tool calls: {label}]"


async def _emit_error_tail(
    request: Request,
    settings: Settings,
    tts: TtsPipeline,
    *,
    err_code: str,
    err_message: str | None,
    parts: list[str],
    tool_calls: list[dict[str, Any]],
    conv_id: str,
    turn_id: str,
    user_turn_id: str,
    body: ChatRequest,
    intent: str,
    approval_required: bool,
    client_ip: str | None,
    t0: float,
    persist_user_once: Any,
    persisted_out: list[bool],
) -> AsyncIterator[bytes]:
    """Terminal error branch: flush TTS, persist the partial/error turn, emit `error`.

    PURE MOVE of the ``if err_code is not None:`` block. The one piece of state that
    feeds back to the caller's ``finally`` — whether the assistant turn was persisted —
    is written to ``persisted_out[0]`` (the caller passes its ``assistant_persisted``
    holder). ``persist_user_once`` is the caller's idempotent user-turn writer, passed
    in so the closure semantics are unchanged. SSE frame order is unchanged: pending
    TTS drain chunks, then the ``error`` event.
    """
    await tts.close_input()
    await tts.flush()
    async for out in tts.drain_ready():
        yield out
    # Use only the STRIPPED value: a whitespace-only accumulation must be
    # falsy so it flows to the _persist_error_turn_end branch. Otherwise
    # _persist_assistant_turn_end gets whitespace, persist_assistant_turn
    # drops it (empty body → no write), assistant_persisted is set True, and
    # the error-turn marker is never written — the error card then fails to
    # re-render after F5. The SSE partial_text field carries "" (more honest).
    partial = "".join(parts).strip()
    if partial:
        await persist_user_once()  # user first (there's content → LLM succeeded)
        await _persist_assistant_turn_end(
            request,
            conversation_id=conv_id,
            user_text=body.text,
            assistant_text=partial,
            user_turn_id=user_turn_id,
            assistant_turn_id=turn_id,
            lang=body.lang,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            intent=intent,
            tool_calls=tool_calls,
            # Error path: persist the partial text but do NOT do memory-capture
            # (a 2nd LLM call) — don't pile a second call onto a failed bridge +
            # extracting memory from a half/corrupt response is low value.
            stage_captures=False,
        )
        persisted_out[0] = True
    else:
        # No partial text (e.g. LLM_UNAVAILABLE before the first delta): persist a
        # FAILED-turn marker (role="error") so the error card re-renders from the
        # server on a page reload (F5) like any other message — no reliance on the
        # client-only ``_localError``. The user turn is already persisted (line ~417);
        # ``turn_id`` (the unused assistant slot) becomes the error turn id.
        await _persist_error_turn_end(
            request,
            conversation_id=conv_id,
            error_text=err_message or err_code or "stream error",
            turn_id=turn_id,
            lang=body.lang,
        )
    yield _sse_pack(
        "error",
        {
            "code": err_code,
            "message": err_message,
            "partial_text": partial,
        },
    ).encode("utf-8")
    await _off_loop(
        audit_write,
        settings.data_dir,
        "chat",
        turn_id=turn_id,
        conv_id=conv_id,
        client_ip=client_ip,
        data={
            "mode": "stream",
            "status": "error",
            "error_code": err_code,
            "error_message": err_message,
            "intent": intent,
            "approval_required": approval_required,
            "user_preview": body.text[:200],
            "assistant_preview": partial[:200],
        },
    )


async def _emit_empty_response_tail(
    request: Request,
    settings: Settings,
    tts: TtsPipeline,
    *,
    conv_id: str,
    turn_id: str,
    body: ChatRequest,
    intent: str,
    approval_required: bool,
    client_ip: str | None,
) -> AsyncIterator[bytes]:
    """Terminal empty-response branch: flush TTS, persist an error marker, emit `error`.

    PURE MOVE of the ``not assistant_text and not tool_calls and no ask_user/plan``
    branch. Reads only the parameters passed in (no producer closure state feeds back
    — the caller ``return``s right after this drains). The SSE frame order is
    unchanged: any pending TTS ``drain_ready`` chunks, then the ``error`` event.
    """
    registry.incr("llm_empty_response")
    await tts.flush()
    async for out in tts.drain_ready():
        yield out
    empty_message = (
        "The model returned an empty response (no text and no tool call). "
        "Try again; if it persists, the active provider/session may be "
        "having trouble (Settings → LLM)."
    )
    # Symmetric with the error path: persist a FAILED-turn marker (role="error")
    # so the empty-response card re-renders from the server on a page reload (F5).
    # The user turn is already persisted (line ~417); reuse ``turn_id`` as the id.
    await _persist_error_turn_end(
        request,
        conversation_id=conv_id,
        error_text=empty_message,
        turn_id=turn_id,
        lang=body.lang,
    )
    yield _sse_pack(
        "error",
        {
            "code": "EMPTY_RESPONSE",
            "message": empty_message,
            "partial_text": "",
        },
    ).encode("utf-8")
    await _off_loop(
        audit_write,
        settings.data_dir,
        "chat",
        turn_id=turn_id,
        conv_id=conv_id,
        client_ip=client_ip,
        data={
            "mode": "stream",
            "status": "empty",
            "intent": intent,
            "approval_required": approval_required,
            "user_preview": body.text[:200],
        },
    )


def _done_tokens_block(usage: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize the tokens block for the ``done`` SSE (prompt/completion + cost).

    ``usage`` is the orchestrator's ``_usage_to_tokens`` output. ``cost_usd`` is added
    only if it came from the provider (claude ``total_cost_usd`` > 0) — on other
    providers the field never appears, so the frontend doesn't show a misleading cost
    like "0.000$". All of it is hardened against external input (int/float coerce).
    """
    u = usage if isinstance(usage, dict) else {}
    block: dict[str, Any] = {
        "prompt": int(u.get("prompt_tokens", 0) or 0),
        "completion": int(u.get("completion_tokens", 0) or 0),
    }
    try:
        cost = float(u.get("cost_usd") or 0)
    except (TypeError, ValueError):
        cost = 0.0
    if cost > 0:
        block["cost_usd"] = cost
    return block


async def _stream_chat_response(
    request: Request,
    settings: Settings,
    hub: EventHub | None,
    body: ChatRequest,
    intent: str,
    approval_required: bool,
    tts_lang: str | None = None,
    client_ip: str | None = None,
    skill_plan: SkillTurnPlan | None = None,
    image_block: str = "",
) -> AsyncIterator[bytes]:
    """SSE generator: persona + history + LLM stream → SSE events.

    Events emitted:
      - `meta`      once at start ({turn_id, conversation_id, intent, approval_required})
      - `skill_used` when WI-1 injects/blocks a skill ({skills, count})
      - `delta`     per token chunk ({text})
      - `memory_use` when compile/recall injects memory ({items, count})
      - `tool_call` when the gateway invokes a tool ({call})
      - `done`      final ({text, latency_ms, tokens, tool_calls})
      - `error`     on failure ({code, message})
    """
    # SEAM: the SSE/context/persist helpers are now MODULE-LEVEL imports (from _base
    # and persist) — the old call-time reach-up into the package is gone. Only the
    # genuine patch surface ``stream_user_chat`` is read at call time via ``_chatpkg``
    # (a ``routes.chat`` setattr resolves to the same object; the e2e/stream tests
    # monkeypatch it there).
    from akana_server.api.routes import chat as _chatpkg

    conv_id = body.conversation_id or str(ulid.new())
    user_turn_id = str(ulid.new())
    turn_id = str(ulid.new())

    # Memory questions flow through the normal stream path; the LLM calls the
    # memory_search MCP tool itself (there is no separate recall pre-pass).

    import asyncio as _asyncio

    try:
        await _off_loop(ensure_conversation, request, conv_id)
        await _off_loop(snapshot_conversation_llm, request, conv_id)
    except Exception:  # a meta-write failure must not block the first SSE byte
        log.warning("ensure_conversation failed (conv=%s); the stream continues", conv_id, exc_info=True)

    _llm_cv = set_conversation_llm(
        await _off_loop(effective_llm_settings, request, conv_id)
    )
    yield _sse_pack(
        "meta",
        {
            "turn_id": turn_id,
            "trace_id": current_trace_id(),
            "conversation_id": conv_id,
            "intent": intent,
            "approval_required": approval_required,
        },
    ).encode("utf-8")
    if skill_plan is not None and skill_plan.has_signal:
        used = skill_plan.used_payload()
        yield _sse_pack("skill_used", {"skills": used, "count": len(used)}).encode(
            "utf-8"
        )
    yield _sse_pack("status", {"phase": "preparing"}).encode("utf-8")

    # ContextEngine F0: persona + skill block + memory + history are assembled at ONE
    # gate (the components are read in parallel inside the assembler — the old task pattern).
    assemble_task = _asyncio.create_task(
        build_context_assembler(request).assemble(
            _context_request(
                body,
                conv_id,
                skill_plan=skill_plan,
                image_block=image_block,
            )
        )
    )
    tts_path_task: _asyncio.Task[str | None] | None = None
    if tts_lang is not None:
        tts_path_task = _asyncio.create_task(
            _asyncio.to_thread(
                resolve_tts_voice_path,
                settings,
                tts_lang=tts_lang,
                stt_lang=None,
            )
        )

    try:
        assembled = await assemble_task
    except BaseException:
        # The assembler raised BEFORE the main try/finally below → release the resources
        # already set up (the LLM ContextVar token + the pending TTS-path task) instead of
        # leaking them (the finally that normally cleans these only covers the main try).
        if tts_path_task is not None and not tts_path_task.done():
            tts_path_task.cancel()
        reset_conversation_llm(_llm_cv)
        raise
    history_msgs = assembled.history
    dropped_before = assembled.dropped_turns
    context_mode = record_context_assemble_metrics(
        skipped_resume=assembled.history_skipped_resume
    )
    user_for_llm = assembled.user_text
    if body.voice:
        # Voice mode: the [mode: voice] directive is appended to the LLM prompt only
        # (the stored/displayed user message stays as body.text → no log pollution).
        # Body = editable + bilingual voice directive (persona registry) + a streaming
        # opening-words line; follows the language picker (fixes EN-mode TR replies).
        # Resolved off-loop: get_voice_directive() reads the persona store (sqlite).
        voice_suffix = await _off_loop(voice_turn_suffix, settings, streaming=True)
        user_for_llm = f"{user_for_llm}\n\n{voice_suffix}"
    if assembled.memory_trace:
        yield _sse_memory_use(assembled.memory_trace)

    # NOTE (#6 dangling-user, SYMMETRIC with blocking): the user turn is NOT persisted
    # BEFORE the LLM CALL. If ``stream_user_chat`` errors BEFORE the first delta
    # (504/active-run/400), only the user turn would be written + an "orphan" turn with
    # no assistant + the meta counter would drift. Instead the user turn is written via
    # ``_persist_user_once`` when content is produced (JUST BEFORE the assistant turn is
    # persisted) → user.ts < assistant.ts is preserved, and on the error path no turn is
    # written.
    # TTS active = did the client request a language (tts_lang). The engine CHOICE
    # depends on prefs/env (edge/xtts/piper), NOT on the Piper path. The gate used to be
    # `tts_voice_path` (a Piper .onnx) → when edge/xtts was selected but Piper was
    # missing, the stream went SILENT + a misleading "Piper not found" error appeared
    # (R2-B2 #17). Now it's resolved via selection.
    tts_active = tts_lang is not None
    tts_voice_path = None
    tts_selection: VoiceSelection | None = None
    if tts_path_task is not None:
        try:
            tts_voice_path = await tts_path_task
        except TtsError:
            # No Piper .onnx — but if edge/xtts is selected the Piper path is NOT needed.
            # Don't surface the error TO THE CLIENT (misleading); if selection resolves
            # to edge, the stream plays with edge. Only report below if no engine at all.
            tts_voice_path = None
    if tts_active:
        try:
            tts_selection = resolve_voice_selection(
                settings, lang=tts_lang, voice_path=tts_voice_path
            )
        except TtsError as e:
            # No TTS engine is usable (neither edge nor Piper) → silent + ONE clear
            # error; close the pump so it doesn't accumulate deltas pointlessly.
            yield _sse_pack(
                "tts_error", {"code": "TTS_ERROR", "message": e.message}
            ).encode("utf-8")
            tts_active = False

    yield _sse_pack("status", {"phase": "model"}).encode("utf-8")

    parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    # Bootstrap-retry double-stream shield: has at least one ``delta`` (or done-fallback
    # text) VISIBLE to the client been emitted? Kept SEPARATE from ``parts`` because a
    # retry does ``parts.clear()`` but bytes ON THE WIRE can't be taken back. If the
    # resume session is lost (``need_history_bootstrap``) and output was ALREADY sent,
    # re-streaming the full response from scratch would show/speak the TEXT TWICE.
    streamed_any = False
    # AskUserQuestion: Claude asked the user a question (the provider emits a structured
    # ``ask_user`` event). If set, the turn is "awaiting an answer" → exempt from the
    # empty-response shields, carried in the done payload, and its summary is persisted.
    last_ask_user: dict[str, Any] | None = None
    # ExitPlanMode: Claude presented its plan in plan mode (the provider emits a
    # structured ``plan`` event). Treated the same as AskUserQuestion: exempt from the
    # empty-response shields, carried in the done payload, and the plan text is persisted.
    last_plan: dict[str, Any] | None = None

    # Streaming TTS side-pipeline: LLM deltas are fed in, synthesized to WAV, and
    # forwarded back as ``tts_chunk`` SSE bytes through a bounded drop-oldest queue so
    # the producer stays single-coroutine and is never blocked by slow synthesis. The
    # three-queue/two-task machine lives in TtsPipeline (the seam split).
    tts = TtsPipeline(
        settings,
        conv_id,
        active=tts_active,
        voice_path=tts_voice_path,
        selection=tts_selection,
    )

    t0 = time.perf_counter()
    tts.start(t0)

    agent_id: str | None = None
    stream_timings: dict[str, Any] = {}
    assistant_persisted = False
    user_persisted = False
    memory_writes: list[dict[str, str]] = []

    async def _persist_user_once() -> None:
        """Write the user turn ONCE (idempotent). Called BEFORE the LLM call → so the
        message is persisted immediately (visible on a mid-turn return). It's also called
        again on the assistant-persist paths but is a no-op thanks to the guard (user.ts <
        assistant.ts is preserved)."""
        nonlocal user_persisted
        if user_persisted:
            return
        ok: list[bool] = []
        await _persist_user_turn_start(
            request,
            conversation_id=conv_id,
            user_text=body.text,
            lang=body.lang,
            user_turn_id=user_turn_id,
            file_ids=body.effective_file_ids,
            ok_out=ok,
        )
        # Mark done ONLY on a confirmed write (or intentional tombstone skip). On a swallowed
        # persist failure (sqlite lock/disk) leave the flag False so a later assistant-persist
        # call retries — otherwise the assistant turn lands with NO preceding user turn.
        if ok and ok[0]:
            user_persisted = True

    # Persist the user turn BEFORE the LLM → so the message is persisted INSTANTLY. If,
    # while a detached turn is in progress, the user switches to another chat and comes
    # BACK (hydrate reads from the server), they SEE their message. Late-persist (only
    # once content is produced) broke the ChatGPT-like optimistic UI: on a mid-turn
    # return the user message looked missing. If the LLM errors before the first delta,
    # the user turn STAYS (an assistant-less "failed" turn) — this is the right UX
    # (ChatGPT also keeps the message + shows an error).
    # The whole stream is inside this generator/task — the DETACHED turn + STOP cancel are preserved.
    bootstrap_history_retried = False
    empty_retried = False
    try:
        # agent-id read + user-turn persist are sqlite I/O — run them INSIDE the try so a
        # failure goes through the finally (cancels the TTS helper tasks + resets the LLM
        # ContextVar) instead of leaking them (the TTS tasks were created above).
        agent_id = await _off_loop(get_agent_id, request, conv_id)
        await _persist_user_once()
        # LLM chat title (Cursor/Claude-style): fire-and-forget at turn START, from the
        # FIRST user message (do NOT wait for the reply). The titler self-gates (idempotent
        # via ``llm_titled`` + skips a manual title) so spawning per turn is safe and cheap
        # — a non-first turn returns after a single meta read, before any LLM call. It never
        # raises and never touches this turn. ``create_task`` is guarded so even the spawn
        # cannot crash the producer. Skipped on voice turns (a background title summary is
        # not useful for a spoken exchange and would add a needless provider call).
        if not body.voice:
            try:
                from akana_server.orchestrator import chat_titler

                _spawn_background(
                    request.app,
                    chat_titler.maybe_title_conversation(
                        settings=settings,
                        hub=hub,
                        conversation_id=conv_id,
                        first_user_text=body.text,
                        lang=body.lang,
                    ),
                )
            except Exception:  # spawning the title task must never break the stream
                log.debug("could not spawn chat titler (conv=%s)", conv_id, exc_info=True)
        try:
            while True:
                need_bootstrap_reload = False
                llm_iter = _chatpkg.stream_user_chat(
                    settings,
                    user_for_llm,
                    history=history_msgs,
                    model=_active_cursor_model(request),
                    conversation_id=conv_id,
                    agent_id=agent_id,
                    reuse_agent=cursor_reuse_agent_enabled(),
                    mcp_servers=memory_mcp_servers(settings, conv_id),
                    system_prompt=assembled.system_prompt_override,
                    thinking_mode=body.thinking_mode,
                    plan_mode=body.plan_mode,
                    file_ids=body.effective_file_ids,  # gemini NATIVE image input
                    # Claude-only opt-in: request the multi-turn loop, but it is GATED by
                    # the ``agent_autocontinue`` master switch, which is OFF by default —
                    # so by default every turn is a single run and a question that ends a
                    # turn waits for the user. Enabling the setting restores the deep
                    # cross-turn loop. Voice mode is always excluded (spoken turns stay
                    # short and never enter the loop). No-op for non-claude providers.
                    auto_continue=not body.voice,
                ).__aiter__()
                llm_next: _asyncio.Task[Any] | None = _asyncio.create_task(
                    llm_iter.__anext__()
                )
                # Event-driven TTS interleave: instead of a 50ms poll of the audio queue
                # on every iteration (up-to-50ms jitter + constant wakeups even on TTS-off
                # turns), await the two sources directly. On a TTS-off turn the pipeline's
                # SSE queue is never fed, so we simply await the LLM iterator. On a TTS-on
                # turn we mux the LLM iterator against a pending pipeline SSE get();
                # ``tts_drained`` marks the pump's None sentinel so we stop pulling from an
                # exhausted queue.
                tts_get: _asyncio.Task[bytes | None] | None = None
                tts_drained = False
                try:
                    while llm_next is not None:
                        if tts_active and not tts_drained:
                            if tts_get is None:
                                tts_get = tts.sse_get()
                            await _asyncio.wait(
                                {llm_next, tts_get},
                                return_when=_asyncio.FIRST_COMPLETED,
                            )
                            if tts_get.done():
                                item = tts_get.result()
                                tts_get = None
                                if item is None:
                                    # The pump's None sentinel is the LAST item on the
                                    # queue — stop pulling (any later get() would block on
                                    # an exhausted queue).
                                    tts_drained = True
                                else:
                                    yield item
                            if not llm_next.done():
                                continue
                        else:
                            await _asyncio.wait({llm_next})
                        try:
                            ev = llm_next.result()
                        except StopAsyncIteration:
                            llm_next = None
                            continue
                        llm_next = _asyncio.create_task(llm_iter.__anext__())

                        if ev.get("need_history_bootstrap"):
                            need_bootstrap_reload = True
                            if llm_next is not None and not llm_next.done():
                                llm_next.cancel()
                            llm_next = None
                            break

                        if ev.get("agent_id"):
                            agent_id = str(ev["agent_id"])
                        timing = ev.get("timing")
                        if isinstance(timing, dict) and timing.get("phase"):
                            stream_timings[str(timing["phase"])] = timing
                            if timing.get("phase") == "agent_ready_ms":
                                record_agent_timing_metric(timing.get("reused"))
                            log.info(
                                "chat timing conv=%s turn=%s phase=%s ms=%s reused=%s",
                                conv_id,
                                turn_id,
                                timing.get("phase"),
                                timing.get("ms"),
                                timing.get("reused"),
                            )
                            yield _sse_pack("timing", timing).encode("utf-8")
                        thinking = ev.get("thinking")
                        if isinstance(thinking, dict):
                            yield _sse_pack("thinking", thinking).encode("utf-8")
                        activity = ev.get("activity")
                        if isinstance(activity, dict):
                            yield _sse_pack("activity", activity).encode("utf-8")
                        # Batch 1 (agent activity): turn-level TODO progress (from TodoWrite) and
                        # subagent (Task) start/end boundaries. Additive — older clients ignore
                        # unknown SSE events; the generic tool_call still renders the cards.
                        todo = ev.get("todo")
                        if isinstance(todo, dict):
                            yield _sse_pack("todo", todo).encode("utf-8")
                        subagent = ev.get("subagent")
                        if isinstance(subagent, dict):
                            yield _sse_pack("subagent", subagent).encode("utf-8")
                        # Task 3: live token/cost update.
                        # usage_live → the SSE `usage` event (contract v2 clause 1).
                        # cost_usd is included only if > 0 (never shown when uncertain).
                        usage_live = ev.get("usage_live")
                        if isinstance(usage_live, dict):
                            _live_payload: dict[str, object] = {
                                "prompt": int(usage_live.get("prompt") or 0),
                                "completion": int(usage_live.get("completion") or 0),
                            }
                            try:
                                _lc = float(usage_live.get("cost_usd") or 0)
                            except (TypeError, ValueError):
                                _lc = 0.0
                            if _lc > 0:
                                _live_payload["cost_usd"] = _lc
                            yield _sse_pack("usage", _live_payload).encode("utf-8")
                        # Task 7: tool input streaming — the SSE `tool_call_delta` event.
                        # A harmless no-op if the frontend doesn't consume it.
                        tool_call_delta = ev.get("tool_call_delta")
                        if isinstance(tool_call_delta, dict):
                            yield _sse_pack("tool_call_delta", tool_call_delta).encode("utf-8")
                        delta = ev.get("delta", "")
                        tool_call = ev.get("tool_call")
                        if delta:
                            parts.append(delta)
                            streamed_any = True
                            yield _sse_pack("delta", {"text": delta}).encode("utf-8")
                            if tts_active:
                                await tts.feed(delta)
                            if hub is not None:
                                await hub.broadcast_json(
                                    {
                                        "type": "chat_delta",
                                        "turn_id": turn_id,
                                        "conversation_id": conv_id,
                                        "text": delta,
                                    }
                                )
                        if isinstance(tool_call, dict):
                            _accumulate_tool_call(tool_calls, tool_call)
                            try:
                                await _off_loop(
                                    record_tool_call,
                                    settings.data_dir,
                                    tool_call,
                                    turn_id=turn_id,
                                    conv_id=conv_id,
                                    client_ip=_client_ip(request),
                                    mode="stream",
                                )
                            except Exception:  # best-effort audit — a policy.db hiccup
                                # must not abort an in-flight streaming turn.
                                log.warning(
                                    "record_tool_call failed (conv=%s)", conv_id, exc_info=True
                                )
                            yield _sse_pack("tool_call", {"call": tool_call}).encode(
                                "utf-8"
                            )
                            if hub is not None:
                                await hub.broadcast_json(
                                    {
                                        "type": "tool_call",
                                        "turn_id": turn_id,
                                        "conversation_id": conv_id,
                                        "call": tool_call,
                                    }
                                )
                        # AskUserQuestion → a LIVE question card (shown immediately,
                        # without waiting for done). The done event also carries
                        # ``ask_user`` → so it's separated with ``not ev.get("done")`` to
                        # avoid emitting the SSE there again.
                        ask_user_q = ev.get("ask_user")
                        if isinstance(ask_user_q, dict) and not ev.get("done"):
                            last_ask_user = ask_user_q
                            yield _sse_pack(
                                "ask_user", {"question": ask_user_q}
                            ).encode("utf-8")
                            if hub is not None:
                                await hub.broadcast_json(
                                    {
                                        "type": "ask_user",
                                        "turn_id": turn_id,
                                        "conversation_id": conv_id,
                                        "question": ask_user_q,
                                    }
                                )
                        # ExitPlanMode → a LIVE plan card (same pattern as
                        # AskUserQuestion). The SSE event name is ``plan_review``; the
                        # claude_provider wire is ``plan``.
                        plan_q = ev.get("plan")
                        if isinstance(plan_q, dict) and not ev.get("done"):
                            last_plan = plan_q
                            yield _sse_pack("plan_review", {"plan": plan_q}).encode(
                                "utf-8"
                            )
                            if hub is not None:
                                await hub.broadcast_json(
                                    {
                                        "type": "plan_review",
                                        "turn_id": turn_id,
                                        "conversation_id": conv_id,
                                        "plan": plan_q,
                                    }
                                )
                        if ev.get("done"):
                            if last_ask_user is None and isinstance(
                                ev.get("ask_user"), dict
                            ):
                                last_ask_user = ev["ask_user"]
                            if last_plan is None and isinstance(ev.get("plan"), dict):
                                last_plan = ev["plan"]
                            u = ev.get("usage") or {}
                            if isinstance(u, dict):
                                usage = u
                            if not parts:
                                done_text = ev.get("text")
                                if isinstance(done_text, str) and done_text:
                                    parts.append(done_text)
                                    streamed_any = True
                                    yield _sse_pack(
                                        "delta", {"text": done_text}
                                    ).encode("utf-8")
                                    if tts_active:
                                        await tts.feed(done_text)
                                    if hub is not None:
                                        await hub.broadcast_json(
                                            {
                                                "type": "chat_delta",
                                                "turn_id": turn_id,
                                                "conversation_id": conv_id,
                                                "text": done_text,
                                            }
                                        )
                finally:
                    if llm_next is not None and not llm_next.done():
                        llm_next.cancel()
                    # The pending pipeline SSE get() (event-driven mux) must not leak when
                    # the LLM loop ends/breaks; cancelling a queue-get is a no-op on the
                    # queue itself (no item was consumed).
                    if tts_get is not None and not tts_get.done():
                        tts_get.cancel()
                if not need_bootstrap_reload:
                    # EMPTY-RESPONSE AUTO-RETRY: the model produced neither text nor a tool
                    # call (and it is not a question/plan turn) and NOTHING was streamed to
                    # the client yet (no delta/tool/audio). Before surfacing the hard
                    # EMPTY_RESPONSE error, retry the turn ONCE — invisible except for a short
                    # extra wait; catches transient empties ("sometimes it errors"). A second
                    # empty falls through to the EMPTY_RESPONSE path below. The ``streamed_any``
                    # guard shares the double-stream shield's intent: never re-stream once
                    # visible output has reached the client.
                    if (
                        not streamed_any
                        and not parts
                        and not tool_calls
                        and last_ask_user is None
                        and last_plan is None
                        and not empty_retried
                    ):
                        empty_retried = True
                        registry.incr("llm_empty_response_retry")
                        log.warning(
                            "empty LLM response — retrying once (conv=%s turn=%s)",
                            conv_id,
                            turn_id,
                        )
                        usage = {}
                        parts.clear()
                        tool_calls.clear()
                        continue
                    break
                if streamed_any:
                    # DOUBLE-STREAM SHIELD: the resume session is lost but visible output
                    # (delta/audio) ALREADY went to the client. Re-streaming the full
                    # response from scratch would show/speak the text TWICE
                    # (``parts.clear()`` only resets the server buffer, NOT the bytes on
                    # the wire). So do NOT replay-retry: finish the turn cleanly with the
                    # text produced so far (in ``parts``) (best-effort) — the fall-through
                    # normal-completion path does done + persist. A fresh-turn bootstrap
                    # (no output yet) retries below as before.
                    log.warning(
                        "agent resume was lost but output was already streamed — re-stream "
                        "SKIPPED (double-stream prevented; conv=%s turn=%s)",
                        conv_id,
                        turn_id,
                    )
                    registry.incr("llm_session_bootstrap_retry_skipped")
                    break
                if bootstrap_history_retried:
                    raise LLMCallError(
                        "could not load history for the resume session",
                        status_code=503,
                    )
                bootstrap_history_retried = True
                registry.incr("llm_session_bootstrap_retry")
                context_mode = CONTEXT_MODE_BOOTSTRAP_RETRY
                await _off_loop(clear_agent_id, request, conv_id)
                history_msgs, _ = await async_llm_history_and_dropped(request, conv_id)
                # The streaming path persists the user turn BEFORE the LLM (_persist_user_once),
                # so this reload includes the CURRENT user turn as the last history entry. The
                # retry re-dispatches it as the live prompt below — without stripping it the
                # question is sent TWICE (last history message + live prompt) and one genuine
                # older turn is pushed out of the chat_max_turns window. The stored user turn
                # body is body.text (user_for_llm may carry a voice suffix), so match on that.
                if (
                    history_msgs
                    and history_msgs[-1].get("role") == "user"
                    and history_msgs[-1].get("content") == body.text
                ):
                    history_msgs = history_msgs[:-1]
                log.warning(
                    "agent resume failed — retrying with a history bootstrap "
                    "(conv=%s turn=%s history_turns=%s)",
                    conv_id,
                    turn_id,
                    len(history_msgs),
                )
                agent_id = None
                parts.clear()
                tool_calls.clear()
        except BreakerOpenError as e:
            # The circuit breaker is OPEN (consecutive LLM failures/burst → fail-fast).
            # Previously a raw BreakerOpenError fell into the generic `except Exception`
            # and became "STREAM_ERROR" → the user thought "the server is gone / the
            # screen broke". Parallel detached turns + each turn's background 2nd LLM
            # call (memory capture) can create a burst and open the breaker; this is NOT
            # a CRASH, it's a temporary rate-limit. Show it with a clear message
            # (includes retry_after) → the UI says "Interrupted: …in N sec…" and the user
            # waits and retries.
            registry.incr("llm_breaker_open")
            err_code = "LLM_RATE_LIMITED"
            err_message = str(e)
        except LLMCallError as e:
            registry.incr("llm_errors")
            is_llm_timeout = e.status_code == 504 and "LLM_TIMEOUT" in (e.message or "")
            if is_llm_timeout:
                registry.incr("llm_timeout_fires")
            if _is_active_run_message(e.message):
                await _reset_cursor_bridge_for_conversation(request.app, conv_id)
            # Distinguish 504+LLM_TIMEOUT from LLM_UNAVAILABLE (diagnostics + FE auto-cancel accuracy).
            err_code = "LLM_TIMEOUT" if is_llm_timeout else ("BAD_REQUEST" if e.status_code == 400 else "LLM_UNAVAILABLE")
            err_message = e.message
        except Exception as e:
            log.exception("stream_user_chat failed")
            err_code = "STREAM_ERROR"
            err_message = str(e) or type(e).__name__
        else:
            err_code = None
            err_message = None

        if err_code is not None:
            # ``assistant_persisted`` may be flipped to True inside the tail (partial
            # persist) → carried back via a one-element holder so the shared ``finally``
            # doesn't double-persist. All other state is read-only in the tail.
            _persisted_holder = [assistant_persisted]
            async for out in _emit_error_tail(
                request,
                settings,
                tts,
                err_code=err_code,
                err_message=err_message,
                parts=parts,
                tool_calls=tool_calls,
                conv_id=conv_id,
                turn_id=turn_id,
                user_turn_id=user_turn_id,
                body=body,
                intent=intent,
                approval_required=approval_required,
                client_ip=client_ip,
                t0=t0,
                persist_user_once=_persist_user_once,
                persisted_out=_persisted_holder,
            ):
                yield out
            assistant_persisted = _persisted_holder[0]
            return

        # LLM stream finished — signal end-of-deltas so the pump can synthesize the
        # tail; ``done`` is not blocked on it (UI unlocks as soon as text ends).
        await tts.close_input()

        raw_full = "".join(parts)
        full = raw_full.strip()
        assistant_text = full or raw_full
        # The model returned NEITHER text NOR a tool call → do NOT SEND a silent empty
        # `done`. The old path sent an empty `done` (text=""); the FE turned this into a
        # MISLEADING error bubble like "Empty response; CURSOR_API_KEY must be set..."
        # (it blamed Cursor regardless of the active provider — the visible face of the
        # empty-response bug after a cursor→claude→cursor switch). Instead, emit an
        # honest `error` event SYMMETRIC with the error path: the user turn is already
        # persisted (line ~350), and the retry tries again with a clean bootstrap. A
        # tool-only turn (tool_calls populated) does NOT FALL here → it ends with a
        # normal `done`. An AskUserQuestion/ExitPlanMode turn can end with no text + no
        # tool call (apology/summary suppressed, no generic tool card) → this is NOT an
        # EMPTY RESPONSE, it's a question/plan. If ``ask_user``/``plan`` is set, exempt
        # it from the empty-response shield.
        if (
            not assistant_text
            and not tool_calls
            and last_ask_user is None
            and last_plan is None
        ):
            async for out in _emit_empty_response_tail(
                request,
                settings,
                tts,
                conv_id=conv_id,
                turn_id=turn_id,
                body=body,
                intent=intent,
                approval_required=approval_required,
                client_ip=client_ip,
            ):
                yield out
            return
        latency_ms = int((time.perf_counter() - t0) * 1000)
        registry.observe("turn_latency_ms", latency_ms)
        if agent_id and _conversation_chat_usable(request.app, conv_id):
            # Tombstone gate: an agent-id write to a deleted conversation (e.g. a turn
            # finishing in the background after the >15s cancel timeout) must NOT
            # RESURRECT the conversation via merge_json_metadata → ensure →
            # _revive_soft_deleted_conversation. Symmetric protection with
            # `_persist_assistant_turn_end`.
            await _off_loop(persist_agent_id, request, conv_id, agent_id)
            await _off_loop(_mirror_cursor_agent_meta, request, conv_id, agent_id)
        # On a question turn the assistant text is empty but we still persist the turn:
        # the question summary (so there's no dangling-user on reload). On a normal turn
        # persist_text = the assistant text. Memory-capture is SKIPPED on a question turn
        # (there's no real response yet → nothing to capture).
        persist_text = (
            assistant_text
            or (_ask_user_summary(last_ask_user) if last_ask_user is not None else "")
            or (_plan_summary(last_plan) if last_plan is not None else "")
        )
        # Tool-only turn (tool calls but empty final text — exempted from the
        # empty-response shield above): persist a placeholder body so the assistant
        # turn + its tool cards survive a reload instead of a dangling user turn.
        if not persist_text and tool_calls:
            persist_text = _tool_only_summary(tool_calls)
        if persist_text:
            await _persist_user_once()  # user first (there's content → LLM succeeded)
            # Contract v2 clause 4: we add usage (prompt/completion/cost_usd?) to the
            # assistant turn so the token/cost info is preserved across a page refresh.
            memory_writes = await _persist_assistant_turn_end(
                request,
                conversation_id=conv_id,
                user_text=body.text,
                assistant_text=persist_text,
                user_turn_id=user_turn_id,
                assistant_turn_id=turn_id,
                lang=body.lang,
                latency_ms=latency_ms,
                intent=intent,
                tool_calls=tool_calls,
                stage_captures=False,
                usage=_done_tokens_block(usage) if usage else None,
                # Persist the structured question so the interactive card survives a chat
                # switch / reload; the turn body stays the _ask_user_summary text (feeds LLM
                # history bootstrap + previews). NULL on a normal / plan turn.
                ask_user=last_ask_user if isinstance(last_ask_user, dict) else None,
            )
            assistant_persisted = True
            # Memory capture (the 2nd LLM call) must NOT BLOCK ``done`` → run it in the
            # background. So the "Typing" indicator closes as soon as the answer ends;
            # the turn finishes normally here and leaves the registry (so the next
            # message doesn't hit TURN_BUSY). No capture while the breaker is open or
            # on an ask_user/plan question turn.
            if (
                not _cursor_breaker_open(settings)
                and last_ask_user is None
                and last_plan is None
            ):
                if _turn_wrote_memory(tool_calls):
                    # The agent already stored memory via memory_remember this turn → skip the
                    # background llm_capture so the same fact is not captured TWICE under a
                    # different key (the two memory paths don't cross-dedup). One fact, one write.
                    registry.incr("llm_capture_skipped_tool_write")
                    log.info(
                        "memory auto-capture skipped — turn used memory_remember (conv=%s turn=%s)",
                        conv_id,
                        turn_id,
                    )
                else:
                    _spawn_background(
                        request.app,
                        _capture_memory_background(
                            request.app,
                            conversation_id=conv_id,
                            user_text=body.text,
                            assistant_text=assistant_text,
                            # Stateless capture: do NOT PASS a cursor-specific tag → with
                            # model=None the active provider (gemini/openai/ollama/claude)
                            # resolves its own default; cursor falls to settings.cursor_model.
                            model=None,
                            assistant_turn_id=turn_id,
                        ),
                    )
        try:
            dropped_after = await async_llm_dropped_turns(request, conv_id)
        except Exception:  # don't let the done event die over a counter when we have the full text
            log.warning("history counter could not be read (conv=%s)", conv_id, exc_info=True)
            dropped_after = dropped_before
        dropped = max(dropped_before, dropped_after)
        # Conflict protection (task 2): if the done payload has both ask_user and
        # plan_review, ask_user WINS — plan_review is not sent. Once the user answers the
        # question the plan will surface on its own anyway. This avoids the confusion of
        # AskUserQuestion followed by ExitPlanMode in the same CLI turn.
        _emit_plan_review = last_plan if last_ask_user is None else None
        yield _sse_pack(
            "done",
            {
                "turn_id": turn_id,
                "conversation_id": conv_id,
                "text": assistant_text if assistant_text else full,
                "latency_ms": latency_ms,
                "tokens": _done_tokens_block(usage),
                "tool_calls": tool_calls,
                "intent": intent,
                "approval_required": approval_required,
                "dropped_turns": dropped,
                "context_mode": context_mode,
                "history_bootstrap_turns": (
                    len(history_msgs)
                    if context_mode != CONTEXT_MODE_RESUME
                    else 0
                ),
                "memory_writes": memory_writes,
                "skill_used": (
                    skill_plan.used_payload() if skill_plan is not None else []
                ),
                # AskUserQuestion: the card on a question turn is also carried in done →
                # so even if the live ``ask_user`` event is missed, the frontend card is
                # finalized from done.
                "ask_user": last_ask_user,
                # ExitPlanMode: the Claude plan card on a plan turn is also carried in
                # done (the ``plan_review`` key; the live event has the same name). If
                # ask_user is present, plan_review is not sent (conflict protection).
                "plan_review": _emit_plan_review,
            },
        ).encode("utf-8")

        await tts.flush()
        async for out in tts.drain_ready():
            yield out
        # All audio chunks are on the wire — the single authoritative signal of "no more
        # audio is coming". The client reopens listening ONLY after this arrives + the
        # queue empties (so a temporary queue-gap between sentences isn't mistaken for
        # "done" and the mic isn't opened early).
        # Sent on EVERY turn (even when tts_active is false): for a NON-voice response
        # too (empty/tool-only/TTS off), the voice-mode turn ends with this and reopens
        # the mic. It used to be conditional on `if tts_active` → on a silent response the
        # client stayed STUCK with ttsStreamOpen=true and froze on "responding" for ~10s.
        yield _sse_pack(
            "tts_end", {"turn_id": turn_id, "tts_active": tts_active}
        ).encode("utf-8")
        if hub is not None:
            await hub.broadcast_json(
                {
                    "type": "chat_done",
                    "turn_id": turn_id,
                    "conversation_id": conv_id,
                    "intent": intent,
                    "approval_required": approval_required,
                    "tool_calls_count": len(tool_calls),
                    "latency_ms": latency_ms,
                    "preview": full[:400],
                    "source": "stream",
                }
            )
        await _off_loop(
            audit_write,
            settings.data_dir,
            "chat",
            turn_id=turn_id,
            conv_id=conv_id,
            client_ip=client_ip,
            data={
                "mode": "stream",
                "status": "ok",
                "intent": intent,
                "approval_required": approval_required,
                "latency_ms": latency_ms,
                "prompt_tokens": int(usage.get("prompt_tokens", 0) or 0),
                "completion_tokens": int(usage.get("completion_tokens", 0) or 0),
                "tool_calls": [
                    _tool_name(c) if isinstance(c, dict) else "?"
                    for c in tool_calls
                    if isinstance(c, dict)
                ],
                "user_preview": body.text[:200],
                "assistant_preview": full[:200],
                "dropped_turns": dropped,
                "context_mode": context_mode,
                "history_bootstrap_turns": (
                    len(history_msgs)
                    if context_mode != CONTEXT_MODE_RESUME
                    else 0
                ),
                "tts": tts_lang,
                "agent_id": agent_id,
                "stream_timings": stream_timings,
            },
        )
    finally:
        reset_conversation_llm(_llm_cv)
        # Cancel the TTS pump + forwarder and clamp the backpressure gauge (b2h-#10).
        # If the SSE consumer disconnects mid-stream (GeneratorExit), the pump must not
        # keep synthesizing audio nobody is listening for; the CANCEL path (STOP /
        # shutdown) skips the normal/error flush, so without this the forwarder would
        # hang forever on an empty queue (one leaked task per STOP). Idempotent.
        await tts.shutdown()
        # Disconnect safety: if we accumulated text but the normal-completion
        # branch never ran (GeneratorExit / CancelledError mid-stream), the
        # assistant turn would otherwise be lost. Persist what we have.
        if not assistant_persisted and parts:
            tail = "".join(parts).strip()
            if tail:
                try:
                    # Persist the mid-stream-captured agent_id too — the normal-completion
                    # path (persist_agent_id + _mirror) is skipped on a STOP/shutdown cancel,
                    # so without this a turn that minted a FRESH bridge session orphans it:
                    # the next message cold-starts a new agent instead of resuming this
                    # (already paid-for) partial exchange. Tombstone-gated like the normal path.
                    if agent_id and _conversation_chat_usable(request.app, conv_id):
                        await _off_loop(persist_agent_id, request, conv_id, agent_id)
                        await _off_loop(
                            _mirror_cursor_agent_meta, request, conv_id, agent_id
                        )
                    await _persist_user_once()  # user first (there's partial content)
                    await _persist_assistant_turn_end(
                        request,
                        conversation_id=conv_id,
                        user_text=body.text,
                        assistant_text=tail,
                        user_turn_id=user_turn_id,
                        assistant_turn_id=turn_id,
                        lang=body.lang,
                        latency_ms=int((time.perf_counter() - t0) * 1000),
                        intent=intent,
                        tool_calls=tool_calls,
                        # Disconnect/cancel cleanup: rescue only the partial text; don't
                        # do the 2nd LLM call (capture) on the cleanup/GeneratorExit path.
                        stage_captures=False,
                    )
                except Exception as exc:
                    log.warning("partial persist on disconnect failed: %s", exc)
