"""Provider-neutral LLM dispatch hub.

This module is the common entry point for ALL providers. The public surface
(``complete_chat`` / ``stream_user_chat`` / ``complete_chat_with_usage`` /
``complete_chat_aggregated``) resolves ``_active_provider(settings)`` ONCE and
branches: ``ollama``/``claude``/``gemini``/``openai`` → the corresponding
``*_provider`` module; ``cursor`` → the built-in Cursor provider in
:mod:`cursor_provider` (direct spawn) or :mod:`bridge_pool` (the default daemon).
No provider is privileged as a default: with nothing configured
(``_active_provider`` → ""), chat fails fast with :data:`NO_PROVIDER_CONFIGURED_MSG`
instead of falling to cursor.

HISTORY: this module used to be the whole cursor client (``cursor_client``) AND
the home of the shared error vocabulary. That implementation has been split:

* the Cursor provider (payload/env/subprocess/NDJSON) now lives in
  :mod:`cursor_provider`;
* the cross-provider error types (``LLMCallError`` / ``friendly_provider_error`` /
  ``LLMResult``) now live in :mod:`errors`;
* the timeout resolvers and the shared NDJSON stream decoder live in :mod:`base`.

The historical backward-compat alias layer (a block of ~13 ``_xxx = cursor_provider.xxx``
/ ``base.xxx`` re-exports plus ``_idle_timeout`` / ``_total_timeout`` delegates) has been
removed: the cursor dispatch tail below calls :mod:`cursor_provider` and :mod:`base`
directly, and every importer/monkeypatch site was repointed to those canonical homes.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from akana_server.config import Settings
from akana_server.orchestrator import base, cursor_provider
from akana_server.orchestrator.errors import (
    LLMCallError,
    LLMResult,
    friendly_provider_error,  # noqa: F401 — facade re-export (test_provider_errors, test_cursor_live_usage)
)

log = logging.getLogger(__name__)


# Raised when chat is attempted with no LLM provider configured. The cursor path is
# the implicit dispatch tail; rather than silently privileging it, an unconfigured
# server fails fast with this actionable message (HTTP 503).
NO_PROVIDER_CONFIGURED_MSG = (
    "No LLM provider configured. Choose one in Settings → Identity, "
    "or run: python akana.py add <provider>"
)


def _active_provider(settings: Settings) -> str:
    """Resolve the active LLM provider from persisted settings.

    One of "cursor" | "claude" | "ollama" | "gemini" | "openai", or "" when no
    provider is configured. No provider is privileged as a default — an
    unset/invalid value resolves to "" and chat refuses with a clear message.
    """
    try:
        from akana_server.llm_context import load_effective_llm_settings
        from akana_server.llm_settings import resolve_provider

        return resolve_provider(
            settings, load_effective_llm_settings(settings.data_dir, settings)
        )
    except Exception:  # pragma: no cover - settings file unreadable → env default
        return (getattr(settings, "llm_provider", "") or "").strip().lower()


# --------------------------------------------------------------------------- #
# Provider registry (dispatch:arch:3)
# --------------------------------------------------------------------------- #
# The four module-providers (ollama/claude/gemini/openai) satisfy the
# :class:`base.ChatProvider` seam: each exposes ``stream_user_chat`` +
# ``complete_chat`` with the SAME provider-neutral keyword set (a provider that
# cannot act on a kwarg accepts-and-ignores it — see base.ChatProvider). So the
# dispatch fan-out no longer needs five hand-maintained if/elif branches forwarding
# slightly different kwarg subsets; it looks up the module and forwards ONE uniform
# kwarg dict. Adding a provider is one registry entry + its module.
#
# The imports stay LAZY (a factory per entry) for two reasons that predate this
# table: (1) the provider modules import shared error types FROM this hub, so an
# eager import at module load would form a cycle; (2) openai/gemini pull optional
# SDKs — importing them only when selected keeps the other paths working when those
# deps are absent.
#
# ``cursor`` is NOT in the registry: it is the built-in dispatch tail with its own
# daemon-vs-direct routing and patchable payload seams, handled explicitly below.


def _load_ollama() -> base.ChatProvider:
    from akana_server.orchestrator import ollama_provider

    return ollama_provider


def _load_claude() -> base.ChatProvider:
    from akana_server.orchestrator import claude_provider

    return claude_provider


def _load_gemini() -> base.ChatProvider:
    from akana_server.orchestrator import gemini_provider

    return gemini_provider


def _load_openai() -> base.ChatProvider:
    from akana_server.orchestrator import openai_provider

    return openai_provider


def _load_cursor() -> base.ChatProvider:
    return cursor_provider


#: name → lazy module loader. Unlike the four registry providers, ``cursor`` is NOT
#: dispatched through this map (it is the built-in tail below); it is included here
#: only so :func:`provider_capabilities` can read every provider's declared traits
#: from ONE table.
_PROVIDER_MODULES: dict[str, Callable[[], base.ChatProvider]] = {
    "ollama": _load_ollama,
    "claude": _load_claude,
    "gemini": _load_gemini,
    "openai": _load_openai,
    "cursor": _load_cursor,
}

#: name → lazy module loader for DISPATCH. ``cursor`` is handled separately (built-in
#: tail), so it is excluded here; capabilities are looked up from
#: :data:`_PROVIDER_MODULES` (which does include cursor).
_PROVIDERS: dict[str, Callable[[], base.ChatProvider]] = {
    name: loader
    for name, loader in _PROVIDER_MODULES.items()
    if name != "cursor"
}


def provider_capabilities(provider: str) -> base.ProviderCapabilities:
    """The declared :class:`base.ProviderCapabilities` for ``provider``.

    Single source of truth for provider traits (e.g. statelessness): each
    ``*_provider`` module declares its own ``CAPABILITIES`` constant and this
    resolver reads it, replacing the name-lists the upper layers used to
    open-code. An unknown/unconfigured provider (including the empty string when
    nothing is configured) returns the default record — ``stateless=False`` — which
    matches the historical fall-through where a name absent from the stateless list
    took the resume/agent-id path.
    """
    loader = _PROVIDER_MODULES.get((provider or "").strip().lower())
    if loader is None:
        return base.ProviderCapabilities()
    caps = getattr(loader(), "CAPABILITIES", None)
    if isinstance(caps, base.ProviderCapabilities):
        return caps
    return base.ProviderCapabilities()


async def complete_chat(
    settings: Settings,
    user_text: str,
    *,
    history: list[dict[str, str]] | None = None,
    model: str | None = None,
    chat_mode: bool = True,
    conversation_id: str | None = None,
    agent_id: str | None = None,
    reuse_agent: bool = True,
    mcp_servers: dict[str, Any] | None = None,
    system_prompt: str | None = None,
    thinking_mode: str | None = None,
    file_ids: list[str] | None = None,  # used for gemini + openai NATIVE image input
) -> LLMResult:
    # dispatch:smell:1 — resolve the provider ONCE (each call chains synchronous
    # settings file I/O on the event loop + is a TOCTOU across branches).
    provider = _active_provider(settings)
    loader = _PROVIDERS.get(provider)
    if loader is not None:
        # One uniform kwarg forward to every module-provider (base.ChatProvider).
        # ``file_ids`` reaches gemini/openai native vision; claude/ollama accept it as
        # a documented no-op.
        text, status, raw = await loader().complete_chat(
            settings,
            user_text,
            history=history,
            model=model,
            chat_mode=chat_mode,
            conversation_id=conversation_id,
            agent_id=agent_id,
            reuse_agent=reuse_agent,
            mcp_servers=mcp_servers,
            system_prompt=system_prompt,
            thinking_mode=thinking_mode,
            file_ids=file_ids,
        )
        return LLMResult(text=text, status=status, raw=raw)

    # Cursor is the implicit dispatch tail. Reaching it with a non-cursor active
    # provider means nothing is configured — fail fast instead of silently using cursor.
    if provider != "cursor":
        raise LLMCallError(NO_PROVIDER_CONFIGURED_MSG, status_code=503)

    # Built-in cursor one-shot. Resolve payload/env/args via :mod:`cursor_provider`
    # (tests patch ``cursor_provider.ensure_api_key``/``bridge_args``/``bridge_env`` to
    # take effect here), then delegate the spawn+parse to cursor_provider.
    cursor_provider.ensure_api_key(settings)
    payload = cursor_provider.build_payload(
        settings,
        user_text,
        history=history,
        model=model,
        stream=False,
        chat_mode=chat_mode,
        conversation_id=conversation_id,
        agent_id=agent_id,
        reuse_agent=reuse_agent,
        mcp_servers=mcp_servers,
        system_prompt=system_prompt,
    )
    return await cursor_provider.run_one_shot(
        settings,
        args=cursor_provider.bridge_args(settings),
        env=cursor_provider.bridge_env(settings),
        payload=payload,
        call_timeout=base.total_timeout(settings),
    )


async def complete_chat_with_usage(
    settings: Settings,
    user_text: str,
    *,
    history: list[dict[str, str]] | None = None,
    model: str | None = None,
    chat_mode: bool = True,
    conversation_id: str | None = None,
    agent_id: str | None = None,
    reuse_agent: bool = True,
    mcp_servers: dict[str, Any] | None = None,
    system_prompt: str | None = None,
    bootstrap_history_loader: Callable[[], Awaitable[list[dict[str, str]]]] | None = None,
    on_bootstrap_retry: Callable[[], Awaitable[None]] | None = None,
    context_mode: str | None = None,
    file_ids: list[str] | None = None,  # used for gemini + openai NATIVE image input
) -> tuple[str, dict[str, Any]]:
    """Akana-compatible return: (assistant_text, usage dict).

    ``chat_mode=True`` (voice/blocking conversation turn): Convergence A #6/#7 —
    produces the result by aggregating the STREAMING bridge → ``usage["tool_calls"]``
    (tool cards/ledger) is populated + ``usage["agent_id"]`` (caller persists and
    reuses it, no cold-start). The one-shot bridge provided neither.

    ``chat_mode=False`` (e.g. memory-capture, stateless): the one-shot path is
    preserved — streaming agent management/conversation is not needed, only the text
    is required.
    """
    if chat_mode:
        text, usage, agent_id = await complete_chat_aggregated(
            settings,
            user_text,
            history=history,
            model=model,
            conversation_id=conversation_id,
            agent_id=agent_id,
            reuse_agent=reuse_agent,
            mcp_servers=mcp_servers,
            system_prompt=system_prompt,
            bootstrap_history_loader=bootstrap_history_loader,
            on_bootstrap_retry=on_bootstrap_retry,
            context_mode=context_mode,
            file_ids=file_ids,
        )
        if agent_id:
            usage["agent_id"] = agent_id
        return text, usage
    result = await complete_chat(
        settings,
        user_text,
        history=history,
        model=model,
        chat_mode=False,
        conversation_id=conversation_id,
        agent_id=agent_id,
        reuse_agent=reuse_agent,
        mcp_servers=mcp_servers,
        system_prompt=system_prompt,
        file_ids=file_ids,
    )
    est = max(1, len(result.text) // 4)
    usage = {
        "prompt_tokens": est,
        "completion_tokens": est,
        "tool_calls": [],
    }
    return result.text, usage


async def complete_chat_aggregated(
    settings: Settings,
    user_text: str,
    *,
    history: list[dict[str, str]] | None = None,
    model: str | None = None,
    conversation_id: str | None = None,
    agent_id: str | None = None,
    reuse_agent: bool = True,
    mcp_servers: dict[str, Any] | None = None,
    system_prompt: str | None = None,
    bootstrap_history_loader: Callable[[], Awaitable[list[dict[str, str]]]] | None = None,
    on_bootstrap_retry: Callable[[], Awaitable[None]] | None = None,
    context_mode: str | None = None,
    file_ids: list[str] | None = None,  # used for gemini + openai NATIVE image input
) -> tuple[str, dict[str, Any], str | None]:
    """Produce a non-streaming result by aggregating the STREAMING bridge → (text, usage, agent_id).

    Convergence A #6/#7: the one-shot bridge does NOT provide ``tool_calls`` +
    ``agent_id`` (the one-shot SDK API does not surface them); the streaming bridge
    emits both (proven working in UI/ledger). Voice/blocking turns aggregate this to
    gain tool cards + agent reuse. Provider-agnostic: ``stream_user_chat`` delegates
    to the active provider. Errors/timeouts from ``stream_user_chat`` are raised as
    ``LLMCallError`` (the caller catches them with its existing ``except``).

    ``usage["tool_calls"]`` is the accumulated list from the done event; ``agent_id``
    comes from the ``agent_id`` event (when present) — the caller persists it and
    reuses it on the next turn (prevents cold-start). On AskUserQuestion/ExitPlanMode
    turns the done event carries an empty text with
    ``usage["status"]="awaiting_user"`` + ``usage["ask_user"]``/``usage["plan"]``
    (BUG 3) — the caller surfaces the question/plan.
    """
    from akana_server.chat_context import CONTEXT_MODE_BOOTSTRAP_RETRY, CONTEXT_MODE_RESUME

    history_msgs = list(history or [])
    agent_id_in = agent_id
    mode = context_mode or (
        CONTEXT_MODE_RESUME
        if agent_id_in and not history_msgs
        else "bootstrap"
    )
    bootstrap_retried = False

    while True:
        parts: list[str] = []
        final_text: str | None = None
        usage: dict[str, Any] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "tool_calls": [],
        }
        agent_id: str | None = None
        need_retry = False

        async for ev in stream_user_chat(
            settings,
            user_text,
            history=history_msgs,
            model=model,
            conversation_id=conversation_id,
            agent_id=agent_id_in,
            reuse_agent=reuse_agent,
            mcp_servers=mcp_servers,
            system_prompt=system_prompt,
            file_ids=file_ids,
        ):
            if ev.get("need_history_bootstrap"):
                need_retry = True
                break
            if ev.get("agent_id"):
                agent_id = str(ev["agent_id"])
            if ev.get("done"):
                # Every provider now yields the SAME canonical terminal event
                # (base.stream_done_event): ``tool_calls``/``text``/``status`` at the
                # TOP LEVEL (providers:arch:0 unification), so this reads ONE shape —
                # no more per-provider "top-level vs inside-usage" fallback.
                final_text = ev.get("text")
                if isinstance(ev.get("usage"), dict):
                    usage = dict(ev["usage"])
                # tool_calls is canonical at the top level; mirror it into usage (the
                # blocking/voice persist path reads usage["tool_calls"] as the ledger).
                usage["tool_calls"] = list(ev.get("tool_calls") or [])
                # In AskUserQuestion/ExitPlanMode the done event carries empty text +
                # ``status=awaiting_user`` + ``ask_user``/``plan``. Promote these into
                # usage — otherwise the voice/blocking path would return empty text and
                # silently discard the question/plan (symmetry with tool_calls).
                if ev.get("status"):
                    usage["status"] = str(ev["status"])
                if isinstance(ev.get("ask_user"), dict):
                    usage["ask_user"] = ev["ask_user"]
                if isinstance(ev.get("plan"), dict):
                    usage["plan"] = ev["plan"]
            elif "delta" in ev and not ev.get("done"):
                parts.append(str(ev["delta"]))

        if not need_retry:
            # Providers that stream the whole answer as deltas emit ``text=""`` in the
            # done event → fall back to the accumulated deltas; claude/cursor emit the
            # authoritative welded text (parts may be filtered) → use it. ``final_text``
            # falsy (None or "") both mean "no authoritative done text".
            text = (final_text or "".join(parts)).strip()
            usage.setdefault("tool_calls", [])
            usage["context_mode"] = mode
            usage["history_bootstrap_turns"] = (
                len(history_msgs) if mode != CONTEXT_MODE_RESUME else 0
            )
            return text, usage, agent_id

        if bootstrap_retried or bootstrap_history_loader is None:
            raise LLMCallError(
                "could not load history for the resume session",
                status_code=503,
            )
        bootstrap_retried = True
        from akana_server.observability.metrics import registry

        registry.incr("llm_session_bootstrap_retry")
        mode = CONTEXT_MODE_BOOTSTRAP_RETRY
        if on_bootstrap_retry is not None:
            await on_bootstrap_retry()
        history_msgs = list(await bootstrap_history_loader())
        agent_id_in = None
        log.warning(
            "agent resume failed — aggregated path bootstrap retry "
            "(conv=%s history_turns=%s)",
            conversation_id,
            len(history_msgs),
        )


async def stream_user_chat(
    settings: Settings,
    user_text: str,
    *,
    history: list[dict[str, str]] | None = None,
    model: str | None = None,
    conversation_id: str | None = None,
    agent_id: str | None = None,
    reuse_agent: bool = True,
    mcp_servers: dict[str, Any] | None = None,
    system_prompt: str | None = None,
    thinking_mode: str | None = None,
    plan_mode: bool = False,
    file_ids: list[str] | None = None,  # used for gemini + openai NATIVE image input
    auto_continue: bool = False,  # claude-only: multi-turn autonomous continuation
) -> AsyncIterator[dict[str, Any]]:
    """Streaming dispatcher → `{delta|thinking|tool_call|done|usage}` events.

    Resolves the active provider ONCE and branches: ollama/claude/gemini/openai →
    the corresponding provider; ``cursor`` (default, fall-through) → the built-in
    Cursor path — the persistent daemon (:mod:`bridge_pool`) by default, or the
    direct spawn in :mod:`cursor_provider` when ``AKANA_BRIDGE_DAEMON=0``. Wire
    format matches what `chat.py` expects:
      - {"delta": "<chunk>", "done": False}
      - {"thinking": {"phase": ..., "text": ...}}
      - {"tool_call": {...}}
      - {"timing": {...}} optional TTFT / agent_ready
      - {"agent_id": "..."} when known
      - {"done": True, "usage": {...}}

    ``thinking_mode`` is HONOURED by each provider branch that has an effort knob:
    claude (``--effort``), ollama (``think``), gemini (``thinking_level``), openai
    (``reasoning_effort``). Cursor has NO such input knob (the SDK exposes reasoning
    only as a model-declared ``ModelSelection.params`` entry, not a plain toggle), so
    the built-in Cursor payload deliberately does NOT carry ``thinking_mode`` — the
    effort control is a no-op on Cursor. ``plan_mode`` is claude-specific
    (``--permission-mode plan`` → ``ExitPlanMode``);
    other providers accept and ignore it for symmetry.
    """
    # dispatch:smell:1 — resolve the provider ONCE (each call chains synchronous
    # settings file I/O on the event loop + is a TOCTOU across branches).
    provider = _active_provider(settings)
    loader = _PROVIDERS.get(provider)
    if loader is not None:
        # One uniform kwarg forward to every module-provider (base.ChatProvider).
        # ``plan_mode``/``auto_continue`` steer claude; ``file_ids`` reaches
        # gemini/openai native vision; the other providers accept each as a documented
        # no-op (see base.ChatProvider), so no branch-specific kwarg subsets remain.
        async for ev in loader().stream_user_chat(
            settings,
            user_text,
            history=history,
            model=model,
            conversation_id=conversation_id,
            agent_id=agent_id,
            reuse_agent=reuse_agent,
            mcp_servers=mcp_servers,
            system_prompt=system_prompt,
            thinking_mode=thinking_mode,
            plan_mode=plan_mode,
            file_ids=file_ids,
            auto_continue=auto_continue,
        ):
            yield ev
        return

    # Cursor is the implicit dispatch tail. Reaching it with a non-cursor active
    # provider means nothing is configured — fail fast instead of silently using cursor.
    if provider != "cursor":
        raise LLMCallError(NO_PROVIDER_CONFIGURED_MSG, status_code=503)

    # Built-in cursor stream. Resolve payload/env/args via :mod:`cursor_provider`
    # (tests patch those attributes to take effect here).
    cursor_provider.ensure_api_key(settings)
    payload = cursor_provider.build_payload(
        settings,
        user_text,
        history=history,
        model=model,
        stream=True,
        conversation_id=conversation_id,
        agent_id=agent_id,
        reuse_agent=reuse_agent,
        mcp_servers=mcp_servers,
        system_prompt=system_prompt,
    )

    from akana_server.orchestrator.bridge_pool import (
        bridge_daemon_enabled,
        get_bridge_pool,
    )

    if bridge_daemon_enabled():
        pool = get_bridge_pool(settings)
        async for ev in pool.stream_run(payload):
            yield ev
        return

    # Direct (daemon-less) spawn: delegate to cursor_provider.run_stream with the
    # args/env resolved here.
    async for ev in cursor_provider.run_stream(
        settings,
        args=cursor_provider.bridge_args(settings),
        env=cursor_provider.bridge_env(settings),
        payload=payload,
        model=model,
    ):
        yield ev
