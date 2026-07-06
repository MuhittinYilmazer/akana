"""Single write path for conversation turns — ``memory.db``.

`chat` (blocking + stream) and `voice` both pass through here → disk state stays
consistent. LLM-based memory capture (Inbox staging) is deliberately NOT here; this
writer only persists user/assistant turns + conversation metadata.

Single-writer discipline: turns are written DIRECTLY via
``memory_core.get_memory_core`` → ``Memory.remember_turn`` + ``conversations_meta``
to a single store (``memory.db``). Previously there were two separate stores +
a "best-effort mirror" pattern; when the write location diverged from the read
location this caused message loss on ``database is locked`` (bug-av #6) and a
dual-writer rotation race on ``event_log.jsonl`` (#12). A single writer makes this
structurally impossible; busy_timeout (10s) waits out locks.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import ulid


log = logging.getLogger(__name__)

# Limited retry to avoid permanently losing a turn on a transient write error (a
# brief ``database is locked`` that exceeds the busy_timeout, or a momentary IO
# hiccup). busy_timeout (10s) already absorbs most locks; this retry rescues
# one-off transient failures. The call is ``_off_loop`` (background thread), so
# the short backoff does not block responses.
_PERSIST_ATTEMPTS = 3
_PERSIST_BACKOFF_S = 0.1


def _persist_turn(
    *,
    data_dir: Path | None,
    role: str,
    conversation_id: str,
    text: str,
    turn_id: str,
    lang: str | None,
    duration_ms: int | None = None,
    tool_calls: list[dict[str, object]] | None = None,
    file_ids: list[str] | None = None,
    usage: dict[str, object] | None = None,
    ask_user: dict[str, object] | None = None,
) -> None:
    """Write the turn to ``memory.db`` (PRIMARY — sole writer since A5).

    * Idempotent: the same ``turn_id`` is UPSERTed; the metadata counter only
      increments for a NEW turn.
    * ``tool_calls``/``file_ids`` are written to the turn → a /messages reload
      does not lose tool cards or attachments.
    * Errors are NOT swallowed but also NOT propagated: logged loudly (``log.error``)
      — the response still reaches the user, but the loss is VISIBLE (unlike the old
      silent-warning approach). Locks (``database is locked``) are already waited out
      by busy_timeout=10s; on top of that, limited retry (``_PERSIST_ATTEMPTS``)
      rescues transient failures.
    * Retry is safe: ``remember_turn`` uses ``turn_id`` UPSERT (idempotent);
      ``is_new`` is computed ONCE outside the loop → if the episodic write succeeds
      but the meta transaction is locked, the retry still performs the counter bump
      (no under-count), and ``return`` only happens on full success so the bump runs
      EXACTLY ONCE (no double-count).
    """
    dd = Path(data_dir) if data_dir is not None else None
    if dd is None:
        log.error("turn_writer: data_dir could not be resolved (conv=%s turn=%s)", conversation_id, turn_id)
        return
    from akana_server.memory_core import get_memory_core

    last_exc: Exception | None = None
    # ``is_new`` is computed ONCE (OUTSIDE the retry loop). Previously it was
    # refreshed on every attempt: if ``remember_turn`` (episodic write) SUCCEEDED
    # but the SEPARATE meta transaction (``on_*`` — counter + ``ensure``) FAILED with
    # ``database is locked``, the retry would see the row already written, assume
    # ``is_new=False``, and SKIP the counter bump → permanent under-count + the
    # conversation might not appear in the sidebar until the next turn (R3-#5).
    # By capturing the true initial state and repeating the bump on retry, we fix
    # this: ``remember_turn`` is idempotent (UPSERT) so re-writing is safe, and
    # ``return`` only happens on full success so the bump runs EXACTLY ONCE
    # (on the successful attempt; no double-count).
    try:
        mem = get_memory_core(dd)
        is_new = mem.episodic.get_turn(turn_id) is None
    except Exception as exc:  # noqa: BLE001 - setup errors must not break the contract: LOUD log + no propagation
        log.error(
            "turn_writer: %s turn COULD NOT BE WRITTEN (conv=%s turn=%s) — setup error "
            "(get_memory_core/get_turn), turn not persisted",
            role,
            conversation_id,
            turn_id,
            exc_info=exc,
        )
        return
    for attempt in range(_PERSIST_ATTEMPTS):
        try:
            mem.remember_turn(
                conversation_id=conversation_id,
                role=role,  # type: ignore[arg-type]
                text=text,
                turn_id=turn_id,
                lang=lang,
                duration_ms=duration_ms,
                tool_calls=tool_calls,
                file_ids=file_ids,
                usage=usage,
                ask_user=ask_user,
            )
            if is_new:
                if role == "user":
                    mem.conversations_meta.on_user_message(conversation_id, text)
                elif role != "error":
                    # "error" = failed-turn marker (LLM unavailable / empty response).
                    # It is persisted so the UI can re-render the error card after a
                    # reload, but it is NOT a real assistant reply: do NOT bump the
                    # message counter / last-activity preview (otherwise the sidebar
                    # would show a failed turn as the conversation's latest message).
                    mem.conversations_meta.on_assistant_message(conversation_id)
            return  # success
        except Exception as exc:  # noqa: BLE001 - must not break the turn/response; retry + VISIBLE
            last_exc = exc
            if attempt + 1 < _PERSIST_ATTEMPTS:
                log.warning(
                    "turn_writer: %s turn write %d/%d failed (conv=%s turn=%s); retrying",
                    role, attempt + 1, _PERSIST_ATTEMPTS, conversation_id, turn_id,
                )
                time.sleep(_PERSIST_BACKOFF_S * (attempt + 1))
    log.error(
        "turn_writer: %s turn COULD NOT BE WRITTEN (conv=%s turn=%s) — %d attempts exhausted, turn not persisted",
        role,
        conversation_id,
        turn_id,
        _PERSIST_ATTEMPTS,
        exc_info=last_exc,
    )


def persist_user_turn(
    *,
    conversation_id: str,
    user_text: str,
    lang: str | None = None,
    turn_id: str | None = None,
    file_ids: list[str] | None = None,
    data_dir: Path | None = None,
) -> str:
    """Write the user turn + update conversation metadata; returns the turn id.

    The turn is written DIRECTLY to ``memory.db`` (``data_dir`` → ``memory.db``).
    Pass an explicit ``turn_id`` so the stored row shares the same id as the SSE
    meta event.
    """
    uid = turn_id or str(ulid.new())
    _persist_turn(
        data_dir=data_dir,
        role="user",
        conversation_id=conversation_id,
        text=user_text,
        turn_id=uid,
        lang=lang,
        file_ids=file_ids,
    )
    return uid


def persist_assistant_turn(
    *,
    conversation_id: str,
    assistant_text: str,
    user_turn_id: str,
    assistant_turn_id: str | None = None,
    lang: str | None = None,
    latency_ms: int | None = None,
    intent: str | None = None,
    tool_calls: list[dict[str, object]] | None = None,
    data_dir: Path | None = None,
    usage: dict[str, object] | None = None,
    ask_user: dict[str, object] | None = None,
) -> str:
    """Write the assistant turn + metadata; returns the turn id (empty string if text is blank — no write).

    The turn is written DIRECTLY to ``memory.db``. ``tool_calls`` is written to the
    turn → a /messages reload returns tool cards. ``usage`` is stored as
    {prompt, completion, cost_usd?} → token/cost information survives a page reload.
    """
    body = (assistant_text or "").strip()
    if not body:
        return ""
    asst_id = assistant_turn_id or str(ulid.new())
    _persist_turn(
        data_dir=data_dir,
        role="assistant",
        conversation_id=conversation_id,
        text=body,
        turn_id=asst_id,
        lang=lang,
        duration_ms=latency_ms,
        tool_calls=tool_calls,
        usage=usage,
        ask_user=ask_user,
    )
    return asst_id


def persist_error_turn(
    *,
    conversation_id: str,
    error_text: str,
    turn_id: str | None = None,
    lang: str | None = None,
    data_dir: Path | None = None,
) -> str:
    """Write a FAILED-turn marker (role="error") + return the turn id (empty if blank — no write).

    Persisted so the UI can re-render the error card after a page reload (F5),
    exactly like any other message. The "error" role is deliberately EXCLUDED from
    the LLM history window (``conversations.recent_llm_messages`` filters to
    user/assistant only) so a stored failure never pollutes a later turn's context,
    and from the conversation message counter (see ``_persist_turn``). The user turn
    is persisted SEPARATELY (before the LLM call), so this only records the
    assistant-side failure.
    """
    body = (error_text or "").strip()
    if not body:
        return ""
    err_id = turn_id or str(ulid.new())
    _persist_turn(
        data_dir=data_dir,
        role="error",
        conversation_id=conversation_id,
        text=body,
        turn_id=err_id,
        lang=lang,
    )
    return err_id


__all__ = ["persist_assistant_turn", "persist_error_turn", "persist_user_turn"]
