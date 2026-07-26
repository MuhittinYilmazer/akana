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

The registry and the LIFECYCLE BROADCAST are one concern, not two: registering a turn
ANNOUNCES it (``turn_active``) and releasing it ALWAYS announces the outcome
(``turn_completed`` with the real ``status``). Emitting from scattered call sites is
what left the blocking ``POST /chat``, the voice route and the connector turns silent —
consumers that latch on ``turn_active`` then wait forever for a clear that no path
sends. Pairing them here makes the "exactly one completion per announcement" rule true
by construction.

Event contract::

    turn_active     {type, conversation_id, source}
    turn_completed  {type, conversation_id, status, source, assistant_turn_id?}
    source: "user" (the sender is watching it) | "background" (arrived on its own)
    status: "ok" | "error" | "cancelled"  — the REAL outcome

Public API::

    handle = register_turn(app, conversation_id)   # raises 409 HTTPException if busy
    ...                                            # run the turn
    release_turn(app, conversation_id, handle,     # in a finally, on every exit
                 status="ok"|"error"|"cancelled")

    swap_turn_handle(app, conversation_id, task)   # re-point the cancel handle (advanced)
    is_turn_busy(app, conversation_id)             # read-only predicate
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from akana_server.api.routes.chat.chat_state import (
    _is_turn_running,
    _nonstreaming_busy,
    _register_nonstreaming_turn,
    _release_nonstreaming_turn,
    _spawn_background,
)

log = logging.getLogger(__name__)

#: How long the completion announcement waits for its own ``turn_active`` to go out.
#: Both are fire-and-forget tasks (the register/release seam is synchronous), and a
#: completion overtaking its start would latch every consumer that clears on it.
_ANNOUNCE_ORDER_TIMEOUT = 5.0


def _announced(app: Any) -> "dict[str, dict[str, Any]]":
    """conv_id → the pending announcement record for a running non-streaming turn.

    Also the RESUME source of truth for these turns: a blocking/voice turn is not in
    ``_active_turns``, so after F5 ``GET /chat/active`` has nothing else to read and
    reported "idle" while the server was still busy.

    The record carries the registering turn's ``handle``, which is what makes
    :func:`release_turn` able to tell "my own record" from "a later turn overwrote it" —
    the only safe basis for deciding who owns the pending completion."""
    reg = getattr(app.state, "nonstreaming_turn_announced", None)
    if not isinstance(reg, dict):
        reg = {}
        app.state.nonstreaming_turn_announced = reg
    return reg


def nonstreaming_turn_started_at(app: Any, conversation_id: str | None) -> float | None:
    """Epoch seconds when the running blocking/voice turn started, else ``None``."""
    conv_id = (conversation_id or "").strip()
    if not conv_id or app is None:
        return None
    try:
        if not _is_turn_running(app, conv_id):
            return None
        meta = _announced(app).get(conv_id)
        if meta is None:
            return None
        return float(meta.get("started_at") or 0.0) or None
    except Exception:  # pragma: no cover - a probe must never raise into a route
        return None


async def _announce_completed(
    app: Any, conv_id: str, meta: "dict[str, Any]", status: str
) -> None:
    prior = meta.get("active_task")
    if isinstance(prior, asyncio.Task) and not prior.done():
        try:
            await asyncio.wait({prior}, timeout=_ANNOUNCE_ORDER_TIMEOUT)
        except Exception:  # pragma: no cover - ordering is best-effort
            pass
    from akana_server.conversation_events import broadcast_turn_completed

    await broadcast_turn_completed(
        app,
        conv_id,
        status=status,
        source=str(meta.get("source") or "user"),
    )


def register_turn(
    app: Any, conversation_id: str | None, *, source: str = "user"
) -> "Any | None":
    """Atomically claim the conversation for a non-streaming turn; return the release handle.

    The claim is a no-await busy re-check + registration of the current task, so a
    concurrent second turn on the same conversation raises ``HTTPException(409,
    TURN_BUSY)``. An empty/None conversation id claims nothing (a fresh ULID can't
    clash) and returns ``None``. The caller MUST pass the returned handle to
    :func:`release_turn` in a ``finally`` (on every exit, including exception/cancel).

    A successful claim also ANNOUNCES the turn (``turn_active``). ``source`` defaults to
    "user": the blocking/voice/connector surfaces all run a turn somebody sent and is
    waiting for, so it must not drive the background-work indicator or a notification.
    """
    handle = _register_nonstreaming_turn(app, conversation_id)
    if handle is None:
        return None
    conv_id = (conversation_id or "").strip()
    src = str(source or "user")
    active_task: "asyncio.Task[Any] | None" = None
    try:
        from akana_server.conversation_events import broadcast_turn_active

        active_task = _spawn_background(
            app, broadcast_turn_active(app, conv_id, source=src)
        )
    except Exception:  # noqa: BLE001 - an announcement miss must not fail the claim
        log.debug("turn_active announce failed (conv=%s)", conv_id, exc_info=True)
    _announced(app)[conv_id] = {
        "started_at": time.time(),
        "source": src,
        "active_task": active_task,
        # Ownership token for release: only the turn whose handle is still in the record
        # may answer this ``turn_active``.
        "handle": handle,
    }
    return handle


def swap_turn_handle(app: Any, conversation_id: str | None, task: Any) -> None:
    """Re-point a claimed turn's cancel handle at ``task`` (registry AND announce record).

    The connector worker needs this: ``register_turn`` records the caller's task, but the
    connector turn runs inside the long-lived per-conversation WORKER, so an external STOP
    would cancel the whole worker (zombie chat). It re-registers a per-TURN child task
    instead — and the announce record has to follow, or the worker's ``release_turn`` no
    longer recognises its own record and silently drops the owed completion.
    """
    conv_id = (conversation_id or "").strip()
    if not conv_id:
        return
    _nonstreaming_busy(app)[conv_id] = task
    meta = _announced(app).get(conv_id)
    if meta is not None:
        meta["handle"] = task


def release_turn(
    app: Any,
    conversation_id: str | None,
    handle: "Any | None",
    *,
    status: str = "ok",
    announce: bool = True,
) -> None:
    """Release the claim — only if ``handle`` is still the registered one — and announce.

    Idempotent and token-scoped: if another turn took over the conversation in the
    meantime, this does nothing (no permanent-busy failure mode).

    ``status`` is the REAL outcome of the turn ("ok" / "error" / "cancelled"), because
    consumers gate on it: the desktop notifier ignores anything but "ok" and the sidebar
    must not celebrate a failed turn. A caller that never sets it still gets a truthful
    "cancelled" when its handle was a cancelled task.

    ``announce=False`` frees the conversation and retires the record WITHOUT emitting —
    for the one caller (the connector router) that owes its own completion later, because
    the fact it must report (the persisted assistant turn id) only exists after the
    conversation has already had to be freed. That caller then owns the "exactly one
    completion" promise for its turn.
    """
    conv_id = (conversation_id or "").strip()
    outcome = str(status or "ok")
    if (
        outcome == "ok"
        and isinstance(handle, asyncio.Task)
        and handle.done()
        and handle.cancelled()
    ):
        outcome = "cancelled"
    _release_nonstreaming_turn(app, conversation_id, handle)
    if not conv_id:
        return
    try:
        reg = _announced(app)
        meta = reg.get(conv_id)
        if meta is None:
            return  # we never announced (empty conv / degraded registration)
        if meta.get("handle") is not handle:
            # A LATER turn overwrote the record: it re-announced under its own
            # ``turn_active`` and ITS release owns the completion — emitting here would
            # clear a turn that is still live. Note this is deliberately NOT "is anything
            # running": a STREAMING takeover writes no record of its own, so treating it
            # as the new owner left OUR turn_active unanswered forever and leaked a stale
            # ``started_at`` for the next ``/chat/active`` probe to serve.
            return
        reg.pop(conv_id, None)
        if announce:
            _spawn_background(app, _announce_completed(app, conv_id, meta, outcome))
    except Exception:  # noqa: BLE001 - an announcement miss must not break the handler
        log.debug("turn_completed announce failed (conv=%s)", conv_id, exc_info=True)


def is_turn_busy(app: Any, conversation_id: str | None) -> bool:
    """True while a turn (streaming OR non-streaming) is running in the conversation."""
    return _is_turn_running(app, conversation_id)


def busy_registry(app: Any) -> "dict[str, Any]":
    """The raw ``conv_id → request-task`` non-streaming busy map (advanced callers only).

    Read-mostly: it is what STOP/DELETE reach into, and what a test drives to simulate
    one. Do NOT re-point a claim through it — :func:`swap_turn_handle` exists because the
    announce record has to move with the handle. Most callers want
    :func:`register_turn` / :func:`release_turn` instead.
    """
    return _nonstreaming_busy(app)


__all__ = [
    "register_turn",
    "release_turn",
    "swap_turn_handle",
    "is_turn_busy",
    "busy_registry",
    "nonstreaming_turn_started_at",
]
