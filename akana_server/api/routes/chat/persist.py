"""Chat turn persistence + memory-capture (Step B4 — split from the god-file).

The persist/capture helpers shared by ``__init__`` (the endpoints) and
``chat_producer`` (the detached turn) are gathered here. Layer: ABOVE ``chat_state``
(the leaf — predicates + ``_turn_wrote_memory`` are imported downward), BELOW
``chat_producer`` (which imports these helpers at MODULE level now — the seam split
removed the old call-time reach-up). ``__init__`` re-exports the turn_writer /
memory-capture *patch-surface* names it needs; persist reads those from the package
at call time (tests monkeypatch ``routes.chat.persist_*`` / ``propose_memory_captures``).

Rule: a persist/capture failure NEVER breaks the turn — an exception thrown from the
SSE generator silently kills the stream (neither ``error`` nor ``done`` in the UI).
Errors are logged, the stream continues.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

import ulid
from fastapi import Request

from akana_server.api.routes.chat._base import (
    _client_ip,
    _off_loop,
)
# Import the shared state predicates + the memory-write guard DOWNWARD from the
# chat_state leaf (they used to be reached via the streaming facade / chat_producer,
# which created a persist ↔ chat_producer cycle). ``_turn_wrote_memory`` lets the
# blocking endpoint (which also serves voice turns) skip the redundant 2nd-pass
# capture the same way the streaming path does — see _persist_assistant_turn_end.
from akana_server.api.routes.chat.chat_state import (
    _chat_cleanup_tombstones,
    _conversation_chat_usable,
    _turn_wrote_memory,
)
from akana_server.chat_context import conversation_service
from akana_server.config import Settings
from akana_server.events import EventHub
from akana_server.memory_core import get_memory_core
from akana_server.conversation_service import ConversationService
from akana_server.tools.gateway import record_tool_call

# NOTE: persist_user_turn / persist_assistant_turn (turn_writer) are NOT imported at
# MODULE level — they are read from the PACKAGE (`routes.chat`) at call time. Tests
# monkeypatch these functions via `routes.chat.persist_*` (persist failure/slowness
# scenarios); a module-level binding wouldn't see the patch. (B4 regression: this patch
# surface was broken during the split.)

log = logging.getLogger(__name__)


def _conversation_tombstoned(app: Any, conversation_id: str) -> bool:
    """True ONLY if the conversation was DELETED (tombstone) — False for a fresh/invisible one.

    ``_conversation_chat_usable`` folds two SEPARATE states into a single "unusable"
    signal: (a) a deleted/tombstoned conversation, (b) a FRESH conversation not yet
    ``ensure``d (with no row on the server). For the early-persist of the user turn, this
    distinction is critical: writing to (a) resurrects the soft-delete (forbidden), but
    NOT writing to (b) SILENTLY drops the message the user just typed (the reported bug).
    This helper marks only (a).

    Conditions for being considered deleted:
    * conv_id is in the in-memory cleanup tombstone set (``_chat_cleanup_tombstones``), OR
    * the conversation row EXISTS in episodic but ``deleted_at`` is set (soft-delete) →
      ``get()`` returns None (the ``deleted_at IS NULL`` filter) BUT ``_conversation_exists``
      (no filter) returns True. The difference between the two is exactly "soft-deleted".
    """
    conv_id = (conversation_id or "").strip()
    if not conv_id:
        return True
    if conv_id in _chat_cleanup_tombstones(app):
        return True
    svc = getattr(app.state, "conversation_service", None)
    if not isinstance(svc, ConversationService):
        return False
    # The row is visible (get != None) → not deleted; not a tombstone.
    if svc.get(conv_id) is not None:
        return False
    # No visible row. If a never-deleted row EXISTS, this is a soft-delete.
    try:
        return svc._conversation_exists(conv_id)
    except Exception:  # defensive — classification must never break the turn/persist
        return False


def _user_turn_persisted(data_dir: Any, turn_id: str) -> bool:
    """True ONLY if the user turn row is DURABLY present in episodic.

    The write goes through ``turn_writer._persist_turn``, which catches every db error
    internally (LOUD ``log.error``, then ``return`` — it never raises), so a normal return
    from ``persist_user_turn`` does NOT prove the row was written. This confirms durability
    by reading the row back. Conservative on ambiguity: a missing ``data_dir`` (the writer
    couldn't have written) → False; a verify-read that itself errors is INCONCLUSIVE →
    True, so a transient read hiccup doesn't force a needless duplicate user write.
    """
    if data_dir is None:
        return False
    try:
        return get_memory_core(data_dir).episodic.get_turn(turn_id) is not None
    except Exception:  # a durability probe must never break the turn — treat as inconclusive
        log.debug(
            "user-turn durability probe failed (turn=%s) — assuming written", turn_id, exc_info=True
        )
        return True


async def _persist_user_turn_start(
    request: Request,
    *,
    conversation_id: str,
    user_text: str,
    lang: str | None,
    user_turn_id: str | None = None,
    file_ids: list[str] | None = None,
    ok_out: list[bool] | None = None,
) -> str:
    """Write user message to episodic + conversation meta before LLM runs.

    A persist failure (sqlite lock/disk) NEVER breaks the turn — especially on the
    SSE path, an exception thrown from inside the generator silently kills the
    stream (neither ``error`` nor ``done`` appears in the UI). The error is logged,
    the stream continues.

    ``ok_out`` (optional): if given, exactly one bool is appended — ``True`` when the user
    turn was durably handled (written, or an intentional tombstone skip), ``False`` ONLY
    when the write was attempted and failed (swallowed). A caller that gates an idempotency
    flag on this must not mark the turn persisted on ``False``, else a failed write leaves a
    dangling answerless assistant turn.
    """
    conv_svc = conversation_service(request)
    if not _conversation_chat_usable(request.app, conversation_id):
        # The gate said "unusable". Separate the two reasons: if the conversation was
        # DELETED (tombstone), don't write — resurrecting a soft-delete is forbidden.
        # But if it's only a FRESH conversation not yet ``ensure``d (a conv opened in
        # the background in the multi-chat flow that isn't visible on the server yet),
        # don't SILENTLY DROP the message the user just typed → ensure the conversation
        # and continue writing. This is the backend leg of the "type in A → switch to B
        # → return to A, message gone" bug.
        if _conversation_tombstoned(request.app, conversation_id):
            if ok_out is not None:
                ok_out.append(True)  # intentional skip (tombstone) — do not retry
            return user_turn_id or str(ulid.new())
        if conv_svc is not None:
            try:
                conv_svc.ensure(conversation_id)
            except Exception:
                log.warning(
                    "could not ensure a fresh conversation (conv=%s); the user turn will"
                    " still be persisted",
                    conversation_id,
                    exc_info=True,
                )
    settings = getattr(request.app.state, "settings", None)
    data_dir = getattr(settings, "data_dir", None)
    from akana_server.api.routes.chat import persist_user_turn  # patch surface
    try:
        tid = await _off_loop(
            persist_user_turn,
            conversation_id=conversation_id,
            user_text=user_text,
            lang=lang,
            turn_id=user_turn_id,
            file_ids=file_ids,
            data_dir=data_dir,
        )
        if ok_out is not None:
            # turn_writer._persist_turn swallows ALL db errors internally (LOUD log, then
            # ``return`` — it never raises), so ``persist_user_turn`` returns a turn id even
            # when the row was NOT written (lock/disk exhausted the 3 internal retries). A
            # normal return therefore does not prove durability → confirm the row actually
            # landed before signalling True, else the caller marks the user turn persisted
            # and the assistant turn lands with no preceding user turn (dangling turn +
            # off-by-one counter). Only downgrade to False on a POSITIVE "row missing"; a
            # verify-read error is inconclusive → keep True (no spurious retry).
            ok_out.append(await _off_loop(_user_turn_persisted, data_dir, tid))
        return tid
    except Exception:
        log.warning(
            "user turn could not be persisted (conv=%s); the chat stream continues",
            conversation_id,
            exc_info=True,
        )
        if ok_out is not None:
            ok_out.append(False)  # transient failure → caller may retry on a later path
        return user_turn_id or str(ulid.new())


def _mirror_cursor_agent_meta(
    request: Request, conversation_id: str, agent_id: str | None
) -> None:
    """B2.3 dual-write: also write agent_id to the new conversations meta.

    The old path (``chat_context.persist_agent_id``) stays as-is; this mirror is
    best-effort and its failure never breaks the old persist.
    """
    if not agent_id or not agent_id.strip():
        return
    try:
        from akana_server.memory_core import get_memory_core

        settings: Settings = request.app.state.settings
        get_memory_core(settings.data_dir).conversations_meta.merge_json_metadata(
            conversation_id, {"agent_id": agent_id.strip()}
        )
    except Exception as exc:  # the new path must NEVER break the old persist
        log.warning(
            "B2.3 dual-write: could not write agent_id meta (conv=%s): %s",
            conversation_id,
            exc,
        )


def _existing_capture_pairs(memory: Any) -> set[tuple[str, str]]:
    """Folded ``(key, value)`` pairs already known — pending inbox rows (any extractor except
    session summaries), durable facts, plus recently-rejected inbox rows (~30 days) — so a
    background-capture candidate that EXACTLY restates one is dropped instead of adding a
    duplicate inbox row.

    Deliberately an exact (key AND value) match: a same-key/new-value CORRECTION is not a
    duplicate (staging supersedes it), and a same-value/different-key pair may be a genuinely
    different fact — so only an identical pair counts as already-captured. The capture model is
    also shown the pending rows and told not to reword them (``memory_capture._pending_snapshot``);
    this is the deterministic backstop, notably against DURABLE facts, which staging's own
    same-key dedup never sees."""
    from akana.memory.terms import fold_text

    pairs: set[tuple[str, str]] = set()
    try:
        for s in memory.staging.list_pending(limit=500):
            if s.extractor != "session_closer" and s.key and s.value:
                pairs.add((fold_text(s.key), fold_text(s.value)))
    except Exception:  # a dedup read must never break capture
        log.debug("capture dedup: pending read failed", exc_info=True)
    try:
        for f in memory.list_facts(limit=500):
            key, value = getattr(f, "key", ""), getattr(f, "value", "")
            if key and value:
                pairs.add((fold_text(key), fold_text(value)))
    except Exception:
        log.debug("capture dedup: durable facts read failed", exc_info=True)
    return pairs


# audit C29: captures for one conversation run in a thread (_off_loop); serialize the
# read-existing-then-stage critical section so two concurrent captures can't each miss the
# other's not-yet-staged candidate and both land a duplicate.
_CAPTURE_LOCK = threading.Lock()


def _recently_rejected_pairs(memory: Any) -> set[tuple[str, str]]:
    """Recently-REJECTED (~30 days) inbox (key, value) pairs, folded.

    BUG D2: a candidate the user explicitly REJECTED was consulted by no dedup layer, so
    the identical pair sailed back in on the next turn. This is kept SEPARATE from
    :func:`_existing_capture_pairs` (audit C30) so the caller can apply the suppression
    CONDITIONALLY — a value the user just re-stated in the current turn is a user-directed
    re-add and must not be dropped merely because a model proposal for it was rejected weeks ago.
    """
    from datetime import UTC, datetime, timedelta

    from akana.memory.terms import fold_text

    pairs: set[tuple[str, str]] = set()
    try:
        cutoff = (datetime.now(UTC) - timedelta(days=30)).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")
        for s in memory.staging.list_all(status="rejected", limit=500):
            if s.extractor != "session_closer" and s.key and s.value and s.ts >= cutoff:
                pairs.add((fold_text(s.key), fold_text(s.value)))
    except Exception:  # a dedup read must never break capture
        log.debug("capture dedup: rejected read failed", exc_info=True)
    return pairs


def _stage_candidates(
    memory: Any,
    candidates: list[Any],
    *,
    conversation_id: str | None,
    user_text: str = "",
) -> list[dict[str, str]]:
    """Write LLM-proposed candidates to the ``memory.db`` staging inbox; return the UI chip shape.

    The capture is written to the ``memory.db`` staging inbox (``get_memory_core``) →
    the inbox API SHOWS what was captured (a single store; no drift of landing in a
    separate file and being invisible). A synchronous sqlite write → the caller offloads
    it to a thread via ``_off_loop`` (the loop isn't blocked).

    Duplicate guard: a candidate that exactly restates an already-captured (key, value) —
    pending inbox row or durable fact — is skipped, so a fact the agent already saved via the
    memory tool (which sits in the inbox under a different key) is not re-captured on a later
    turn (the reported bug). See :func:`_existing_capture_pairs`.
    """
    from akana.memory.settings import load_memory_settings
    from akana.memory.staging import FactCandidate
    from akana.memory.terms import fold_text

    # If unapproved remembering (allow_direct) is ON, captured candidates are promoted
    # (moved to durable) without waiting in the inbox — the SAME behavior as the
    # memory.remember tool + session_closer. If OFF, they wait for approval in the inbox
    # (the default; "everything to the inbox").
    data_dir = getattr(memory, "_data_dir", None)
    allow_direct = bool(load_memory_settings(data_dir).allow_direct) if data_dir else False
    curator = memory.make_curator() if allow_direct else None

    user_fold = fold_text(user_text or "")
    out: list[dict[str, str]] = []
    # audit C29: hold the capture lock across the read-existing → stage loop so a
    # concurrent capture (this or another turn) can't miss an as-yet-unstaged candidate
    # and land a duplicate. Captures are post-turn/off-loop, so serializing is cheap.
    with _CAPTURE_LOCK:
        seen = _existing_capture_pairs(memory)  # pending + durable (always suppress)
        rejected = _recently_rejected_pairs(memory)  # recently-rejected (conditional, C30)
        for cand in candidates:
            key_fold, val_fold = fold_text(cand.key), fold_text(cand.value)
            sig = (key_fold, val_fold)
            if sig in seen:
                # Already captured (inbox or durable) — don't add a duplicate row.
                log.info(
                    "memory capture: skipped already-captured candidate key=%r (dedup)", cand.key
                )
                continue
            # audit C30: suppress a recently-rejected pair ONLY when the user did NOT just
            # restate the value this turn — otherwise a user-directed re-add of a value they
            # once rejected is silently dropped for ~30 days.
            if sig in rejected and val_fold and val_fold not in user_fold:
                log.info(
                    "memory capture: skipped recently-rejected candidate key=%r (dedup)", cand.key
                )
                continue
            seen.add(sig)  # also dedup identical candidates WITHIN this batch
            staged = memory.staging.stage(
                FactCandidate(
                    key=cand.key,
                    value=cand.value,
                    reason=cand.reason,
                    trust="inferred",
                    extractor="llm_capture",
                ),
                conversation_id=conversation_id,
            )
            kind = "staging"
            if curator is not None:
                try:
                    if curator.promote(staged.id) is not None:
                        kind = "stored"  # unapproved remembering: durable immediately, no inbox wait
                except Exception:  # a promote error must not break capture; staged remains
                    log.warning("capture auto-promote failed (staged=%s)", staged.id, exc_info=True)
            out.append({"id": staged.id, "kind": kind, "key": cand.key})
    return out


async def _stage_memory_captures(
    request: Request,
    *,
    conversation_id: str,
    user_text: str,
    assistant_text: str,
) -> list[dict[str, str]]:
    """LLM-proposed Inbox captures (separate from turn persistence).

    The context read + staging write go to a single ``Memory`` store →
    what's captured is visible in the inbox (no store drift).
    """
    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, Settings):
        return []
    # Breaker guard (audit C31): the streaming path checks this before its capture
    # LLM call and the background path re-checks it, but the inline blocking/voice
    # path did not — so a voice turn fired the 2nd LLM request at a half-recovered
    # provider, adding latency and burning the single recovery probe. Skip capture
    # while the active provider's breaker is OPEN/HALF_OPEN (matches chat_producer).
    from akana_server.api.routes.chat.chat_state import _cursor_breaker_open
    if _cursor_breaker_open(settings):
        log.debug(
            "memory capture skipped — provider breaker open (conv=%s)", conversation_id
        )
        return []
    memory = get_memory_core(settings.data_dir)
    from akana_server.api.routes.chat import propose_memory_captures  # patch surface
    candidates = await propose_memory_captures(
        settings,
        memory,
        user_text=user_text,
        assistant_text=assistant_text,
        conversation_id=conversation_id,
        # Stateless capture call: do NOT PASS a cursor-specific tag — with model=None each
        # provider resolves its own default (gemini/openai/ollama would already ignore a
        # foreign tag; cursor falls to settings.cursor_model).
        model=None,
    )
    if not candidates:
        return []
    # b25: the usability gate was evaluated BEFORE the (up to ~45s) capture LLM call above. Re-check
    # after it — don't stage facts into a conversation that was deleted/reset in the meantime
    # (the tombstone / soft-delete is already visible via _chat_cleanup_tombstones + svc.get()).
    if not _conversation_chat_usable(request.app, conversation_id):
        return []
    staged = await _off_loop(
        _stage_candidates,
        memory,
        candidates,
        conversation_id=conversation_id,
        user_text=user_text,
    )
    if staged:
        log.info(
            "memory capture staged %d item(s) for conversation %s",
            len(staged),
            conversation_id,
        )
    return staged


async def _persist_assistant_turn_end(
    request: Request,
    *,
    conversation_id: str,
    user_text: str,
    assistant_text: str,
    user_turn_id: str,
    assistant_turn_id: str,
    lang: str | None,
    latency_ms: int | None,
    intent: str | None,
    tool_calls: list[dict[str, Any]] | None = None,
    stage_captures: bool = True,
    usage: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Write assistant message after stream/LLM completes; optional memory capture.

    A persist/capture failure NEVER breaks the turn: throwing an exception from the
    generator when we have the response text would swallow the ``done`` event in the
    SSE (the UI freezes). The error is logged; in the worst case memory_writes is empty.

    ``stage_captures=False``: memory-capture (the 2nd LLM call) is SKIPPED — only the
    fast persist is done. The stream path uses this and runs the capture in the
    background AFTER ``done`` (``_capture_memory_background``); otherwise the "Typing"
    indicator stays stuck until that 2nd LLM call finishes. The blocking surface
    (a one-shot response) keeps capture inline with the default ``True``.
    """
    if not _conversation_chat_usable(request.app, conversation_id):
        return []
    settings = getattr(request.app.state, "settings", None)
    from akana_server.api.routes.chat import persist_assistant_turn  # patch surface
    try:
        asst_id = await _off_loop(
            persist_assistant_turn,
            conversation_id=conversation_id,
            assistant_text=assistant_text,
            user_turn_id=user_turn_id,
            assistant_turn_id=assistant_turn_id,
            lang=lang,
            latency_ms=latency_ms,
            intent=intent,
            tool_calls=[c for c in (tool_calls or []) if isinstance(c, dict)] or None,
            data_dir=getattr(settings, "data_dir", None),
            usage=usage,
        )
    except Exception:
        log.warning(
            "assistant turn could not be persisted (conv=%s); the chat stream continues",
            conversation_id,
            exc_info=True,
        )
        return []
    if not asst_id:
        return []
    writes: list[dict[str, str]] = [
        {"id": user_turn_id, "kind": "episodic"},
        {"id": asst_id, "kind": "episodic"},
    ]
    # BUG D1: the streaming path skips the 2nd-pass capture when the agent already wrote
    # memory via a memory-write tool this turn (_turn_wrote_memory); the blocking path
    # (incl. voice turns, which route through here) lacked this check, so an agent that
    # called memory_remember AND produced a captured candidate got double-captured.
    if stage_captures and not _turn_wrote_memory(tool_calls or []):
        try:
            writes.extend(
                await _stage_memory_captures(
                    request,
                    conversation_id=conversation_id,
                    user_text=user_text,
                    assistant_text=assistant_text,
                )
            )
        except Exception:
            log.warning("memory capture staging failed; the turn continues", exc_info=True)
    return writes


async def _persist_error_turn_end(
    request: Request,
    *,
    conversation_id: str,
    error_text: str,
    turn_id: str,
    lang: str | None,
) -> str:
    """Write a FAILED-turn marker (role="error") after the stream errors out.

    Symmetric to ``_persist_assistant_turn_end`` but for the failure case (LLM
    unavailable / empty response). The user turn was already persisted before the
    LLM call, so this records only the assistant-side failure → on a page reload the
    error card re-renders from the server like any other message (no client-only
    ``_localError`` reliance). A persist failure NEVER breaks the stream: the
    ``error`` SSE frame is still emitted; the loss is logged.
    """
    if not _conversation_chat_usable(request.app, conversation_id):
        return ""
    settings = getattr(request.app.state, "settings", None)
    from akana_server.api.routes.chat import persist_error_turn  # patch surface
    try:
        return await _off_loop(
            persist_error_turn,
            conversation_id=conversation_id,
            error_text=error_text,
            turn_id=turn_id,
            lang=lang,
            data_dir=getattr(settings, "data_dir", None),
        )
    except Exception:
        log.warning(
            "error turn could not be persisted (conv=%s); the chat stream continues",
            conversation_id,
            exc_info=True,
        )
        return ""


#: Upper bound for the memory-capture (the 2nd LLM call) that runs in the background
#: AFTER ``done``. This call does NOT BLOCK the UI (``done`` was already sent → "Typing"
#: closed); if it hangs it affects only this orphan task — bounded with ``wait_for`` so
#: tasks don't accumulate. Kept generous: a slow but returning call shouldn't be cut off.
_MEMORY_CAPTURE_TIMEOUT_S = 45.0


async def _capture_memory_background(
    app: Any,
    *,
    conversation_id: str,
    user_text: str,
    assistant_text: str,
    model: str | None,
    assistant_turn_id: str | None = None,
) -> None:
    """Fire-and-forget memory capture AFTER the turn's ``done`` (doesn't block the UI).

    This LLM-based capture used to run inline BEFORE ``done`` → even after the response
    text finished streaming, the "Typing" indicator stayed stuck until this 2nd LLM call
    returned (seconds if slow, indefinitely if hung). Now ``done`` is sent as soon as the
    response ends, the turn leaves the registry (so the next message doesn't hit
    TURN_BUSY), and capture runs independently here.

    Edge cases: ``request`` is NOT PASSED (the response is done; only ``app.state`` +
    scalar values are used). A stateless LLM call with ``conversation_id=None`` → it
    doesn't collide with the conversation's main agent. Every error/timeout is logged and
    SWALLOWED; it never affects the completed turn/UI."""
    try:
        settings = getattr(app.state, "settings", None)
        if not isinstance(settings, Settings):
            return
        if not _conversation_chat_usable(app, conversation_id):
            return
        # Burst protection (re-check): the breaker was healthy at spawn time but this 2nd
        # LLM call may have become DEGRADED while waiting in the queue (parallel turns).
        # If degraded (OPEN/HALF_OPEN) don't make the call — it would hammer a
        # half-recovered provider and deepen the rate-limit / consume the user turn's
        # single probe. Capture is best-effort; skipping doesn't affect the turn/UI. Read
        # the active provider's breaker (cursor not hardcoded) — capture is now dispatched
        # to that provider.
        from akana_server.api.routes.chat.chat_state import _cursor_breaker_open
        if _cursor_breaker_open(settings):
            return
        memory = get_memory_core(settings.data_dir)  # A7: capture to staging
        from akana_server.api.routes.chat import propose_memory_captures  # patch surface
        candidates = await asyncio.wait_for(
            propose_memory_captures(
                settings,
                memory,
                user_text=user_text,
                assistant_text=assistant_text,
                conversation_id=conversation_id,
                model=model,
            ),
            timeout=_MEMORY_CAPTURE_TIMEOUT_S,
        )
        if not candidates:
            return
        # staging is a synchronous sqlite write → a thread so it doesn't block the loop.
        staged = await _off_loop(
            _stage_candidates,
            memory,
            candidates,
            conversation_id=conversation_id,
        )
        if staged:
            log.info(
                "memory capture staged %d item(s) (background) conv=%s",
                len(staged),
                conversation_id,
            )
            # Send the "Saved" source chips to the UI: since capture now runs AFTER
            # ``done``, it can't be carried in the done payload → a separate WS event.
            # The frontend (ws:memory_staged) adds the chips under the relevant turn
            # (turn_id). A broadcast failure doesn't break capture (best-effort).
            hub = getattr(app.state, "event_hub", None)
            if isinstance(hub, EventHub):
                try:
                    await hub.broadcast_json(
                        {
                            "type": "memory_staged",
                            "conversation_id": conversation_id,
                            "turn_id": assistant_turn_id,
                            "writes": staged,
                        }
                    )
                except Exception:
                    log.debug(
                        "memory_staged WS broadcast failed conv=%s",
                        conversation_id,
                        exc_info=True,
                    )
    except asyncio.TimeoutError:
        log.warning(
            "memory capture did not return within %.0fs conv=%s — skipped (UI unaffected)",
            _MEMORY_CAPTURE_TIMEOUT_S,
            conversation_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception(
            "memory capture (background) unexpected error conv=%s", conversation_id
        )


async def _record_tool_calls(
    request: Request,
    settings: Settings,
    calls: list[Any],
    *,
    turn_id: str,
    conv_id: str,
    mode: str,
) -> None:
    ip = _client_ip(request)
    for call in calls:
        if isinstance(call, dict):
            # record_tool_call = policy score + policy.db audit + ledger write
            # (busy_timeout=10000) — if it runs on the loop, the whole server freezes
            # under lock contention.
            # enforce=False is DELIBERATE: the provider process runs the MCP tools, and
            # this point is a POST-execution record — raising here would make a tool that
            # already ran look "blocked". Pre-execution blocking is elsewhere: the
            # provider tool-allowlist (work_mode), the ReAct toolbox, the task gate, the
            # sandbox. A deny is still visible via audit + WS policy_update.
            await _off_loop(
                record_tool_call,
                settings.data_dir,
                call,
                turn_id=turn_id,
                conv_id=conv_id,
                client_ip=ip,
                mode=mode,
            )


def _accumulate_tool_call(
    tool_calls: list[dict[str, Any]], call: dict[str, Any]
) -> None:
    """Accumulate streaming ``tool_call`` events into the turn list, DEDUPing.

    The orchestrator emits TWO events per tool: ``phase="start"`` (args populated)
    and ``phase="end"`` (result/status populated) — both carry the same ``id``.
    Since this list feeds the ``done`` payload, the client's persistent tool-call
    cache, and the audit count, ``end`` must update the existing ``start`` record
    IN PLACE, not ADD a new row. Otherwise two records accumulate per tool and the
    "N tools" header (raw length) shows twice as many as the deduplicated cards
    ("4 tools" but 2 cards). If there's no ``id`` we can't match → append.
    """
    cid = call.get("id") or call.get("call_id")
    if cid is not None:
        for existing in tool_calls:
            if (existing.get("id") or existing.get("call_id")) == cid:
                for key, value in call.items():
                    if value is not None:
                        existing[key] = value
                return
    tool_calls.append(call)
