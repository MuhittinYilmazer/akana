"""Public turn-gate service — register / release / busy-check for non-streaming turns.

The blocking ``POST /chat`` handler, the ``/voice`` route, and the connector channel
(Telegram/Discord/…) all run *non-streaming* turns: a single request→response with no
follower/resume buffer. They share ONE per-conversation busy registry so a second
non-streaming turn (or a streaming turn) on the same conversation cannot reach the LLM
concurrently (Convergence A #2). The streaming surface, by contrast, queues (202).

This module is the STABLE PUBLIC seam onto that registry. The implementation still
lives in :mod:`akana_server.api.routes.chat.chat_state` (the leaf that also owns the
``_active_turns`` buffer + predicates); the underscore-private names there remain as
thin aliases so existing importers (``akana_server.connectors.service``) keep working
until they migrate to this module. New callers should import from here.

Public API::

    handle = register_turn(app, conversation_id)   # raises 409 HTTPException if busy
    ...                                            # run the turn
    release_turn(app, conversation_id, handle)     # in a finally, on every exit

    busy_registry(app)[conversation_id] = task     # swap the cancel handle (advanced)
    is_turn_busy(app, conversation_id)             # read-only predicate
"""

from __future__ import annotations

from typing import Any

from akana_server.api.routes.chat.chat_state import (
    _is_turn_running,
    _nonstreaming_busy,
    _register_nonstreaming_turn,
    _release_nonstreaming_turn,
)


def register_turn(app: Any, conversation_id: str | None) -> "Any | None":
    """Atomically claim the conversation for a non-streaming turn; return the release handle.

    The claim is a no-await busy re-check + registration of the current task, so a
    concurrent second turn on the same conversation raises ``HTTPException(409,
    TURN_BUSY)``. An empty/None conversation id claims nothing (a fresh ULID can't
    clash) and returns ``None``. The caller MUST pass the returned handle to
    :func:`release_turn` in a ``finally`` (on every exit, including exception/cancel).
    """
    return _register_nonstreaming_turn(app, conversation_id)


def release_turn(app: Any, conversation_id: str | None, handle: "Any | None") -> None:
    """Release the claim — only if ``handle`` is still the registered one.

    Idempotent and token-scoped: if another turn took over the conversation in the
    meantime, this does nothing (no permanent-busy failure mode).
    """
    _release_nonstreaming_turn(app, conversation_id, handle)


def is_turn_busy(app: Any, conversation_id: str | None) -> bool:
    """True while a turn (streaming OR non-streaming) is running in the conversation."""
    return _is_turn_running(app, conversation_id)


def busy_registry(app: Any) -> "dict[str, Any]":
    """The raw ``conv_id → request-task`` non-streaming busy map (advanced callers only).

    Exposed for the connector worker, which swaps the registered handle to a per-turn
    child task so an external STOP cancels only the turn, not the long-lived worker.
    Most callers want :func:`register_turn` / :func:`release_turn` instead.
    """
    return _nonstreaming_busy(app)


__all__ = [
    "register_turn",
    "release_turn",
    "is_turn_busy",
    "busy_registry",
]
