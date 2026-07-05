"""Shared LLM conversation context — episodic archive as source of truth."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from typing import Iterator

from fastapi import Request

import ulid

from akana_server.config import Settings
from akana_server.llm_context import (
    reset_conversation_llm,
    set_conversation_llm,
)
from akana_server.llm_settings import (
    _VALID_PROVIDERS,
    LlmSettings,
    conversation_llm_to_meta,
    load_llm_settings,
    llm_settings_to_conversation_patch,
    merge_conversation_llm,
    resolve_provider,
)
from akana_server.conversation_service import ConversationService


def conversation_service(request: Request) -> ConversationService | None:
    svc = getattr(request.app.state, "conversation_service", None)
    return svc if isinstance(svc, ConversationService) else None


def new_conversation_id(existing: str | None) -> str:
    return (existing or "").strip() or str(ulid.new())


def ensure_conversation(request: Request, conversation_id: str) -> None:
    svc = conversation_service(request)
    if svc is not None:
        svc.ensure(conversation_id)


def _global_llm(request: Request) -> LlmSettings:
    settings: Settings = request.app.state.settings
    cached = getattr(request.app.state, "llm_settings", None)
    if isinstance(cached, LlmSettings):
        return cached
    return load_llm_settings(settings.data_dir, settings)


def effective_llm_settings(request: Request, conversation_id: str) -> LlmSettings:
    """Effective LLM settings for the conversation: explicit metadata override + global."""
    base = _global_llm(request)
    cid = (conversation_id or "").strip()
    if not cid:
        return base
    svc = conversation_service(request)
    if svc is None:
        return base
    meta = svc.get_json_metadata(cid)
    return merge_conversation_llm(base, meta)


def restore_llm_settings(request: Request, conversation_id: str) -> LlmSettings:
    """Conversation-switch restore — including the legacy ``cursor_agent_provider`` hint."""
    base = _global_llm(request)
    cid = (conversation_id or "").strip()
    if not cid:
        return base
    svc = conversation_service(request)
    if svc is None:
        return base
    meta = svc.get_json_metadata(cid)
    from akana_server.llm_settings import merge_conversation_llm_for_restore

    return merge_conversation_llm_for_restore(base, meta)


def effective_provider(request: Request, conversation_id: str) -> str:
    settings: Settings = request.app.state.settings
    return resolve_provider(settings, effective_llm_settings(request, conversation_id))


def persist_conversation_llm(
    request: Request, conversation_id: str, patch: dict[str, object]
) -> None:
    """Persist the LLM selection to the conversation metadata."""
    svc = conversation_service(request)
    cid = (conversation_id or "").strip()
    if svc is None or not cid or not patch:
        return
    meta_patch = conversation_llm_to_meta(patch)
    if meta_patch:
        svc.merge_json_metadata(cid, meta_patch)


def snapshot_conversation_llm(request: Request, conversation_id: str) -> None:
    """After a turn/switcher, write the effective LLM to the conversation (last-used model)."""
    settings: Settings = request.app.state.settings
    cid = (conversation_id or "").strip()
    if not cid:
        return
    llm = effective_llm_settings(request, cid)
    persist_conversation_llm(
        request, cid, llm_settings_to_conversation_patch(llm, settings=settings)
    )


@contextmanager
def bind_conversation_llm(
    request: Request, conversation_id: str
) -> Iterator[LlmSettings]:
    """Reflect the conversation-specific LLM to clients for the duration of the active turn."""
    llm = effective_llm_settings(request, conversation_id)
    token = set_conversation_llm(llm)
    try:
        yield llm
    finally:
        reset_conversation_llm(token)


def _active_provider(request: Request, conversation_id: str | None = None) -> str:
    """Resolve the active LLM provider for this request (conversation-specific or global)."""
    if conversation_id and (conversation_id or "").strip():
        return effective_provider(request, conversation_id)
    settings: Settings = request.app.state.settings
    return resolve_provider(settings, _global_llm(request))


def _leak_guard_active_provider(request: Request, meta: dict[str, object]) -> str:
    """Active provider for the leak-guard — WITHOUT reading the ``cursor_agent_provider`` HINT.

    ROOT FIX (Issue #2 cursor→claude→cursor empty response): ``effective_provider``
    reads that hint as a legacy provider-hint (see ``conversation_llm_patch_from_meta``).
    But the hint already OWNS the stored id (``stored`` below). If the guard uses
    it as the "active provider", ``stored == active`` is ALWAYS true → the guard
    CANCELS itself and the old session leaks to the opposite provider
    (``claude --resume <cursor-uuid>`` → "No conversation found" → empty
    response). Only the user's EXPLICIT conv selection (``llm_provider``) + global
    count; the hint is excluded.
    """
    explicit = meta.get("llm_provider")
    if isinstance(explicit, str) and explicit.strip().lower() in _VALID_PROVIDERS:
        return explicit.strip().lower()
    settings: Settings = request.app.state.settings
    return resolve_provider(settings, _global_llm(request))


def get_agent_id(request: Request, conversation_id: str) -> str | None:
    """Stored agent/session id — only when it belongs to the ACTIVE provider.

    Every provider's agent/session id shares the same meta field; when the
    provider changes, if the old id leaks to ``claude --resume``/Cursor the run
    blows up with "No conversation found". If the provider does not match it
    returns None → a clean session is opened. Legacy records (no provider field)
    are treated as cursor.

    Backward compatibility: it reads the neutral ``agent_id``/``agent_provider``
    keys, falling back to the old ``cursor_agent_id``/``cursor_agent_provider``
    keys (so conversations persisted before the rename keep resuming).
    """
    svc = conversation_service(request)
    if svc is None:
        return None
    meta = svc.get_json_metadata(conversation_id)
    raw = meta.get("agent_id")
    if not (isinstance(raw, str) and raw.strip()):
        raw = meta.get("cursor_agent_id")  # legacy
    if not (isinstance(raw, str) and raw.strip()):
        return None
    stored_provider = meta.get("agent_provider")
    if not (isinstance(stored_provider, str) and stored_provider.strip()):
        stored_provider = meta.get("cursor_agent_provider")  # legacy
    stored = (
        stored_provider.strip().lower()
        if isinstance(stored_provider, str) and stored_provider.strip()
        else "cursor"
    )
    if stored != _leak_guard_active_provider(request, meta):
        return None
    return raw.strip()


def _persist_agent_provider(request: Request, conversation_id: str) -> str:
    """The provider to TAG a persisted agent/session id with.

    b15: use the provider the turn actually DISPATCHED with — the per-turn ContextVar snapshot
    bound by :func:`bind_conversation_llm` — NOT a fresh live read. A mid-turn model switch
    otherwise stores the wrong provider, so the NEXT turn's leak-guard (:func:`get_agent_id`)
    rejects the id and the resume fails ("could not resume" / empty response). Falls back to the
    live active provider only when no turn snapshot is bound.
    """
    from akana_server.llm_context import get_conversation_llm

    snapshot = get_conversation_llm()
    if snapshot is not None:
        settings: Settings = request.app.state.settings
        return resolve_provider(settings, snapshot)
    return _active_provider(request, conversation_id)


def persist_agent_id(
    request: Request, conversation_id: str, agent_id: str | None
) -> None:
    svc = conversation_service(request)
    if svc is None or not agent_id or not agent_id.strip():
        return
    svc.merge_json_metadata(
        conversation_id,
        {
            "agent_id": agent_id.strip(),
            "agent_provider": _persist_agent_provider(request, conversation_id),
        },
    )


def clear_agent_id(request: Request, conversation_id: str) -> None:
    svc = conversation_service(request)
    if svc is None:
        return
    svc.merge_json_metadata(
        conversation_id,
        {
            "agent_id": None,
            "agent_provider": None,
            "cursor_agent_id": None,  # legacy
            "cursor_agent_provider": None,  # legacy
        },
    )


# Session context modes — carried in SSE done + audit + metrics.
CONTEXT_MODE_RESUME = "resume"
CONTEXT_MODE_BOOTSTRAP = "bootstrap"
CONTEXT_MODE_BOOTSTRAP_RETRY = "bootstrap_retry"


def _chat_max_turns(request: Request, conversation_id: str) -> int:
    return effective_llm_settings(request, conversation_id).chat_max_turns


def _llm_dropped_turns_sync(request: Request, conversation_id: str) -> int:
    """Out-of-window turn count for the "old messages dropped" warning.

    Returns 0 when the session is RESUMED (cursor/claude with a stored agent id + reuse
    enabled): the history is NOT re-sent — the model keeps the FULL conversation in its own
    agent session, so nothing is dropped from its view. A non-zero count there made the UI
    warn "the model can no longer see that part" even though the resumed agent still
    remembers it (misleading). The count is meaningful only on the BOOTSTRAP path (stateless
    gemini/ollama, or the first turn before an agent id exists), where the prompt history is
    truncated to ``chat_max_turns``. Meta counter only — no episodic message read.
    """
    if not llm_history_bootstrap_needed_sync(request, conversation_id):
        return 0
    max_turns = _chat_max_turns(request, conversation_id)
    svc = conversation_service(request)
    if svc is None:
        return 0
    svc.ensure(conversation_id)
    meta = svc.get(conversation_id)
    total = int(meta.message_count) if meta else 0
    return max(0, total - max_turns)


def record_context_assemble_metrics(*, skipped_resume: bool) -> str:
    """Context mode after assemble + in-process counters."""
    from akana_server.observability.metrics import registry

    if skipped_resume:
        registry.incr("llm_history_skipped_resume")
        return CONTEXT_MODE_RESUME
    registry.incr("llm_history_bootstrap")
    return CONTEXT_MODE_BOOTSTRAP


def record_agent_timing_metric(reused: str | None) -> None:
    """Bridge timing.reused → metric (resume/create/cache/resume_failed)."""
    from akana_server.observability.metrics import registry

    key = {
        "resume": "llm_session_resume_ok",
        "create": "llm_session_created",
        "session": "llm_session_cache_hit",
        "resume_failed": "llm_session_resume_failed",
    }.get(str(reused or "").strip())
    if key:
        registry.incr(key)


def llm_history_bootstrap_needed_sync(
    request: Request, conversation_id: str
) -> bool:
    """Should the history be flattened into the LLM prompt?

    If a Cursor/Claude session can be resumed (stored agent/session id + reuse
    enabled), the history is not re-sent to the model — the episodic read is
    skipped. STATELESS providers (Ollama/Gemini: NO agent-reuse/session resume;
    every call is fresh) → the history must ALWAYS be flattened into the prompt.

    Statelessness is no longer an open-coded name-list here: each provider declares
    it as a capability (``base.ProviderCapabilities.stateless``) and this queries
    :func:`llm_dispatch.provider_capabilities`. The stateless set is exactly the old
    ``("ollama", "gemini")`` list, so behaviour is unchanged (unknown providers keep
    the resume/agent-id fall-through via the default ``stateless=False``).
    """
    from akana_server.orchestrator.llm_dispatch import provider_capabilities

    provider = effective_provider(request, conversation_id)
    if provider_capabilities(provider).stateless:
        return True
    from akana_server.orchestrator.bridge_pool import cursor_reuse_agent_enabled

    if not cursor_reuse_agent_enabled():
        return True
    return get_agent_id(request, conversation_id) is None


def _llm_history_and_dropped_sync(
    request: Request,
    conversation_id: str,
) -> tuple[list[dict[str, str]], int]:
    max_turns = _chat_max_turns(request, conversation_id)
    svc = conversation_service(request)
    if svc is None:
        return [], 0
    svc.ensure(conversation_id)
    msgs = svc.recent_llm_messages(conversation_id, max_turns=max_turns)
    meta = svc.get(conversation_id)
    total = int(meta.message_count) if meta else len(msgs)
    dropped = max(0, total - max_turns)
    return msgs, dropped


def _llm_history_for_assemble_sync(
    request: Request,
    conversation_id: str,
) -> tuple[list[dict[str, str]], int, bool]:
    """(history, dropped_turns, history_skipped_resume) — the assemble path."""
    if llm_history_bootstrap_needed_sync(request, conversation_id):
        msgs, dropped_full = _llm_history_and_dropped_sync(request, conversation_id)
        return msgs, dropped_full, False
    # Resume: the history lives in the agent session → none is re-sent and none is dropped
    # from the model's view, so the "old messages dropped" warning must not fire here.
    return [], 0, True


async def async_llm_history_and_dropped(
    request: Request,
    conversation_id: str,
) -> tuple[list[dict[str, str]], int]:
    """Return (messages for Cursor SDK, dropped_turns beyond LLM window).

    P0 stability: the episodic.db read + the llm settings file read are
    synchronous I/O — if they run on the event loop, the whole server waits
    during concurrent writer / disk slowness. Moved to a worker thread.
    """
    import asyncio

    return await asyncio.to_thread(
        _llm_history_and_dropped_sync, request, conversation_id
    )


async def async_llm_history_for_assemble(
    request: Request,
    conversation_id: str,
) -> tuple[list[dict[str, str]], int, bool]:
    """History for assemble — skips the episodic read when resume is active."""
    import asyncio

    return await asyncio.to_thread(
        _llm_history_for_assemble_sync, request, conversation_id
    )


async def async_llm_dropped_turns(request: Request, conversation_id: str) -> int:
    """The dropped_turns counter only (end-of-turn SSE / blocking response)."""
    import asyncio

    return await asyncio.to_thread(_llm_dropped_turns_sync, request, conversation_id)


BootstrapHistoryLoader = Callable[[], Awaitable[list[dict[str, str]]]]
BootstrapRetryHook = Callable[[], Awaitable[None]]


def make_bootstrap_retry_hooks(
    request: Request, conversation_id: str
) -> tuple[BootstrapHistoryLoader, BootstrapRetryHook]:
    """Resume-fail bootstrap hooks for the voice/blocking paths."""

    async def before_retry() -> None:
        import asyncio

        await asyncio.to_thread(clear_agent_id, request, conversation_id)

    async def load_history() -> list[dict[str, str]]:
        msgs, _ = await async_llm_history_and_dropped(request, conversation_id)
        return msgs

    return load_history, before_retry


__all__ = [
    "CONTEXT_MODE_BOOTSTRAP",
    "CONTEXT_MODE_BOOTSTRAP_RETRY",
    "CONTEXT_MODE_RESUME",
    "async_llm_dropped_turns",
    "async_llm_history_and_dropped",
    "async_llm_history_for_assemble",
    "bind_conversation_llm",
    "clear_agent_id",
    "conversation_service",
    "effective_llm_settings",
    "effective_provider",
    "ensure_conversation",
    "get_agent_id",
    "llm_history_bootstrap_needed_sync",
    "make_bootstrap_retry_hooks",
    "new_conversation_id",
    "persist_agent_id",
    "persist_conversation_llm",
    "record_agent_timing_metric",
    "record_context_assemble_metrics",
    "restore_llm_settings",
    "snapshot_conversation_llm",
]
