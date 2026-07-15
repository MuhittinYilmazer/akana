"""Broadcast conversation turn events to ``/ws/events`` for LIVE UI updates.

Background producers — the schedule engine (and any future detached worker) — create
conversations and append turns OUTSIDE the normal chat SSE flow. Without a push,
the web UI never learns that a new thread appeared or that a turn finished until
the user manually reloads the page (the exact symptom: "the scheduled reminder's
chat doesn't show up and no notification pops").

These helpers emit the SAME two events a detached chat turn emits
(``turn_active`` when work starts, ``turn_completed`` when it finishes). The
frontend already handles both: ``turn_completed`` on a non-current conversation
refreshes the thread sidebar and shows a toast; on the current conversation it
reloads the thread log (so the result renders live). ``turn_active`` drives the
"working…" affordance.

Everything here is DEFENSIVE: no event hub (headless/tests) → a silent no-op; a
broadcast failure is logged, never raised — a UI-notification miss must never
break a schedule fire.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


def event_hub(app: Any) -> Any | None:
    """The live :class:`~akana_server.events.EventHub`, or ``None`` if absent."""
    try:
        from akana_server.events import EventHub

        hub = getattr(getattr(app, "state", None), "event_hub", None)
        return hub if isinstance(hub, EventHub) else None
    except Exception:  # pragma: no cover - import/attr issues degrade to no-op
        return None


async def broadcast_turn_active(app: Any, conversation_id: str | None) -> None:
    """Announce that a background turn STARTED in ``conversation_id``.

    Drives the "working…" indicator and makes a just-created background thread
    appear in the sidebar while it runs."""
    hub = event_hub(app)
    cid = (conversation_id or "").strip()
    if hub is None or not cid:
        return
    try:
        await hub.broadcast_json({"type": "turn_active", "conversation_id": cid})
    except Exception:  # noqa: BLE001 - a notification miss must not break the producer
        log.debug("turn_active broadcast failed (conv=%s)", cid, exc_info=True)


async def broadcast_turn_completed(
    app: Any,
    conversation_id: str | None,
    *,
    status: str = "ok",
    assistant_turn_id: str | None = None,
) -> None:
    """Announce that a background turn FINISHED in ``conversation_id``.

    On a non-current conversation the frontend refreshes the sidebar (the new
    thread appears) and toasts on ``status == "ok"``; on the current conversation
    it reloads the thread log so the freshly written result renders without a
    page refresh."""
    hub = event_hub(app)
    cid = (conversation_id or "").strip()
    if hub is None or not cid:
        return
    payload: dict[str, Any] = {
        "type": "turn_completed",
        "conversation_id": cid,
        "status": str(status or "ok"),
    }
    if assistant_turn_id:
        payload["assistant_turn_id"] = str(assistant_turn_id)
    try:
        await hub.broadcast_json(payload)
    except Exception:  # noqa: BLE001 - a notification miss must not break the producer
        log.debug("turn_completed broadcast failed (conv=%s)", cid, exc_info=True)


__all__ = ["broadcast_turn_active", "broadcast_turn_completed", "event_hub"]
