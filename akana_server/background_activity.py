"""Which conversations have BACKGROUND work running right now — the server-side truth.

A schedule / ``background_run`` turn is not a chat turn: it never registers an
``_ActiveTurn``, so its only footprint was the one-shot ``turn_active`` WS event.
Every consumer that RE-DERIVES state from the server after that event — an F5, a
WS reconnect, a second tab, the sidebar's periodic activity refresh — therefore
concluded "nothing is running" and cleared the working indicator while the job was
still working, for the remaining minutes of the run.

This registry is the missing state: ``conversation_id → started_at`` (epoch
seconds), written by the producer around its ``turn_active`` / ``turn_completed``
pair. It deliberately does NOT make the conversation "busy": a background job must
never block the user from sending their own message (that is the whole point of
detaching it).

CONSUMER CONTRACT (owned by the chat-resume route, not by this module): a probe
that finds no streaming turn should still report the conversation as working when
it appears here, handing back ``started_at`` so a reconnecting client resumes the
elapsed clock instead of restarting it at 0:00.

In-memory by design — a process restart kills the runs this describes.
"""

from __future__ import annotations

import time
from typing import Any

_ATTR = "background_active_turns"


def _registry(app: Any) -> "dict[str, list[Any]]":
    """conv_id → ``[refcount, started_at]``.

    REFCOUNTED, not a flag: overlapping jobs in one conversation are one continuous
    "working" period, so the record must survive until the LAST of them finishes.
    With a plain ``pop`` the first job to end erased the record while the other ran
    on for minutes, and every reconcile after that (F5, chat switch, second tab) read
    "idle" and retired the still-running job's indicator and clock."""
    reg = getattr(getattr(app, "state", None), _ATTR, None)
    if not isinstance(reg, dict):
        reg = {}
        setattr(app.state, _ATTR, reg)
    return reg


def _entry(reg: "dict[str, list[Any]]", cid: str) -> "list[Any] | None":
    value = reg.get(cid)
    return value if isinstance(value, list) and len(value) == 2 else None


def mark_background_active(app: Any, conversation_id: str | None) -> None:
    """Record that background work started in ``conversation_id`` (idempotent).

    Re-entry keeps the ORIGINAL start time: two overlapping jobs in one
    conversation are one continuous "working" period to the user — so a
    reconnecting client resumes the elapsed clock instead of restarting it."""
    cid = (conversation_id or "").strip()
    if app is None or not cid:
        return
    try:
        reg = _registry(app)
        entry = _entry(reg, cid)
        if entry is None:
            reg[cid] = [1, time.time()]
        else:
            entry[0] = int(entry[0]) + 1
    except Exception:  # pragma: no cover - a UI hint must never break the producer
        pass


def clear_background_active(app: Any, conversation_id: str | None) -> None:
    """Release ONE job's hold — MUST be paired with every ``mark_background_active``
    on every exit path (success, error, dropped delivery, cancellation), or the
    indicator rehydrates as "still working" forever after a reload.

    The record only goes away when the last holder releases it. An unpaired clear
    cannot drive the count negative (that would wedge the conversation "working"
    for the rest of the process)."""
    cid = (conversation_id or "").strip()
    if app is None or not cid:
        return
    try:
        reg = _registry(app)
        entry = _entry(reg, cid)
        if entry is None:
            reg.pop(cid, None)  # legacy/foreign shape → drop it outright
            return
        entry[0] = int(entry[0]) - 1
        if entry[0] <= 0:
            reg.pop(cid, None)
    except Exception:  # pragma: no cover
        pass


def background_started_at(app: Any, conversation_id: str | None) -> float | None:
    """Epoch seconds when background work started here, or ``None`` if idle."""
    cid = (conversation_id or "").strip()
    if app is None or not cid:
        return None
    try:
        entry = _entry(_registry(app), cid)
    except Exception:  # pragma: no cover
        return None
    if entry is None:
        return None
    value = entry[1]
    return float(value) if isinstance(value, (int, float)) else None


def background_active_conversations(app: Any) -> dict[str, float]:
    """Snapshot of every conversation with background work in flight.

    Public shape is unchanged (``conv_id → started_at``) — the refcount is an
    internal bookkeeping detail, not part of the consumer contract."""
    if app is None:
        return {}
    try:
        reg = _registry(app)
        out: dict[str, float] = {}
        for cid in list(reg):
            entry = _entry(reg, cid)
            if entry is not None and isinstance(entry[1], (int, float)):
                out[cid] = float(entry[1])
        return out
    except Exception:  # pragma: no cover
        return {}


__all__ = [
    "background_active_conversations",
    "background_started_at",
    "clear_background_active",
    "mark_background_active",
]
