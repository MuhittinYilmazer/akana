"""Shared single-turn core for the NON-STREAMING surfaces (blocking /chat, later /voice).

Why this exists: the blocking ``POST /chat`` handler used to hand-roll its own LLM call
+ error mapping, which had drifted from the streaming producer — it lacked the
streaming path's safeguards (empty-response retry, ``BreakerOpenError`` mapping). Every
future producer fix had to be mirrored by hand. This module is the single place a
non-streaming turn runs the LLM with those safeguards, so the surfaces converge.

Design: framework-agnostic. It does NOT import FastAPI or touch ``Request`` — it takes
plain values + optional callbacks, returns a :class:`TurnOutcome`, and raises
:class:`TurnError` (code/message/status) that the HTTP caller maps to an
``HTTPException``. This lets the ``/voice`` route (a LATER agent) rebase onto the same
core without pulling in the chat route's request plumbing.

ENTRY POINT (documented for the voice-rebase agent):
    ``await run_nonstreaming_turn(settings, user_text, *, history, model, conversation_id,
    agent_id, reuse_agent, mcp_servers, system_prompt, file_ids, bootstrap_history_loader,
    on_bootstrap_retry, context_mode, on_active_run_reset) -> TurnOutcome``

The dispatch function ``complete_chat_with_usage`` is read at call time from the chat
package namespace (``routes.chat.complete_chat_with_usage``) so the existing test
monkeypatch surface keeps working for every non-streaming surface.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from akana_server.config import Settings
from akana_server.network.breaker import BreakerOpenError
from akana_server.observability import registry
from akana_server.orchestrator.bridge_pool import _is_active_run_message
from akana_server.orchestrator.llm_dispatch import LLMCallError

log = logging.getLogger(__name__)


@dataclass(slots=True)
class TurnOutcome:
    """The result of one successful non-streaming LLM turn."""

    text: str
    usage: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    #: The agent_id the bridge returned (caller persists it → next turn reuses it).
    agent_id: str | None = None


class TurnError(Exception):
    """A non-streaming turn failed with a mapped, user-facing error.

    Carries the SAME error codes as the streaming producer so the two surfaces map
    failures identically: ``LLM_RATE_LIMITED`` (breaker open), ``LLM_TIMEOUT`` (504),
    ``BAD_REQUEST`` (400), ``LLM_UNAVAILABLE`` (other). The HTTP caller turns this into
    an ``HTTPException(status_code, {"error": {"code", "message"}})``.
    """

    def __init__(self, code: str, message: str | None, status_code: int) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message
        self.status_code = status_code


async def run_nonstreaming_turn(
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
    file_ids: list[str] | None = None,
    bootstrap_history_loader: Callable[[], Awaitable[list[dict[str, str]]]] | None = None,
    on_bootstrap_retry: Callable[[], Awaitable[None]] | None = None,
    context_mode: str | None = None,
    on_active_run_reset: Callable[[], Awaitable[None]] | None = None,
) -> TurnOutcome:
    """Run one non-streaming LLM turn with the streaming path's safeguards.

    Safeguards mirrored from ``chat_producer._stream_chat_response`` (so the blocking /
    voice surfaces stop drifting):

    * **empty-response retry** — if the model returns neither text nor a tool call, the
      turn is retried ONCE before surfacing the empty result (catches transient empties);
    * **BreakerOpenError mapping** — a circuit-breaker-open is a temporary rate-limit, not
      a crash: mapped to ``TurnError(LLM_RATE_LIMITED)`` instead of an unmapped 500;
    * **file_ids** — provider-native image/PDF input is forwarded (parity with streaming);
    * **active-run reset** — a cursor "active run" error triggers ``on_active_run_reset``
      (the caller resets the bridge for the conversation), same as streaming.

    Raises :class:`TurnError` on a mapped LLM failure; returns a :class:`TurnOutcome` on
    success (including the empty-after-retry case, which the caller may treat as empty).
    """
    # Read the dispatch fn from the package namespace so the test monkeypatch surface
    # (routes.chat.complete_chat_with_usage) applies to every non-streaming caller.
    from akana_server.api.routes import chat as _chatpkg

    empty_retried = False
    while True:
        try:
            text, usage = await _chatpkg.complete_chat_with_usage(
                settings,
                user_text,
                history=history,
                model=model,
                conversation_id=conversation_id,
                agent_id=agent_id,
                reuse_agent=reuse_agent,
                mcp_servers=mcp_servers,
                system_prompt=system_prompt,
                file_ids=file_ids,
                bootstrap_history_loader=bootstrap_history_loader,
                on_bootstrap_retry=on_bootstrap_retry,
                context_mode=context_mode,
            )
        except BreakerOpenError as e:
            # Temporary rate-limit (burst / consecutive failures), not a crash — map it
            # the same way the streaming producer does so the UI shows "retry in N sec".
            registry.incr("llm_breaker_open")
            raise TurnError("LLM_RATE_LIMITED", str(e), status_code=503) from e
        except LLMCallError as e:
            registry.incr("llm_errors")
            is_timeout = e.status_code == 504 and "LLM_TIMEOUT" in (e.message or "")
            if is_timeout:
                registry.incr("llm_timeout_fires")
            if _is_active_run_message(e.message) and on_active_run_reset is not None:
                await on_active_run_reset()
            code = (
                "LLM_TIMEOUT"
                if is_timeout
                else ("BAD_REQUEST" if e.status_code == 400 else "LLM_UNAVAILABLE")
            )
            raise TurnError(code, e.message, status_code=e.status_code) from e

        tool_calls = usage.get("tool_calls") if isinstance(usage, dict) else None
        if not isinstance(tool_calls, list):
            tool_calls = []
        new_agent_id = usage.get("agent_id") if isinstance(usage, dict) else None

        # EMPTY-RESPONSE AUTO-RETRY (parity with streaming): neither text nor a tool call
        # → retry ONCE before returning the empty outcome. A transient empty ("sometimes
        # it errors") is invisibly recovered; a second empty falls through.
        if not (text or "").strip() and not tool_calls and not empty_retried:
            empty_retried = True
            registry.incr("llm_empty_response_retry")
            log.warning(
                "empty LLM response (blocking) — retrying once (conv=%s)",
                conversation_id,
            )
            continue

        return TurnOutcome(
            text=text,
            usage=usage if isinstance(usage, dict) else {},
            tool_calls=tool_calls,
            agent_id=str(new_agent_id) if new_agent_id else None,
        )
