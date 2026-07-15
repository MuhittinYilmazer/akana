"""Same-chat delivery for background results — the busy-safe injection inbox.

A fired reminder should land **in the conversation the user is already in**, as an assistant message — not in a separate thread. Two hard
problems live here:

1. **The busy scenario** — the background result becomes ready while the user's
   OWN turn is still streaming in that conversation. Writing mid-turn would
   interleave a foreign assistant message between a user turn and its answer
   (store order corruption + UI log reload mid-stream). So: if the conversation
   has an active turn (or queued user messages), the injection is parked in a
   DURABLE inbox and drained right after the turn completes — before the next
   queued user message, so the user's follow-up sees the result in history.

2. **Agent-session memory** — claude/cursor/codex resume their agent session and
   do NOT get history re-sent, so an injected turn is invisible to the model on
   the next turn. Every injection therefore also records a **context note**; the
   next chat turn prepends it to the user text (both paths: on stateless
   providers the note is redundant-but-harmless since history carries the turn).

The inbox is a single JSON file under ``data_dir`` guarded by
:func:`json_store.cross_process_lock` (same discipline as the schedule/task
stores). Everything is defensive: a delivery failure is logged and never breaks
the producer (a schedule fire must not die because the UI write failed).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import ulid

from akana_server.json_store import cross_process_lock, write_json_atomic
from akana_server.timeutil import iso_now

log = logging.getLogger(__name__)

_FILENAME = "chat_injections.json"

#: Cap per conversation — a runaway producer must not grow the inbox unbounded.
MAX_PENDING_PER_CONV = 50
#: Cap on stored context notes per conversation (oldest dropped first).
MAX_NOTES_PER_CONV = 20
#: A single context note is clipped to this many chars when prepended to a turn.
NOTE_CLIP_CHARS = 700


def _path(data_dir: Path | str) -> Path:
    return Path(data_dir) / _FILENAME


def _load(data_dir: Path | str) -> dict[str, Any]:
    """Read the inbox file; corruption-tolerant (a broken file resets empty)."""
    import json

    p = _path(data_dir)
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {"pending": {}, "notes": {}}
    except Exception:
        log.warning("chat_injections: unreadable store — starting empty", exc_info=True)
        return {"pending": {}, "notes": {}}
    if not isinstance(data, dict):
        return {"pending": {}, "notes": {}}
    pending = data.get("pending")
    notes = data.get("notes")
    return {
        "pending": pending if isinstance(pending, dict) else {},
        "notes": notes if isinstance(notes, dict) else {},
    }


def _save(data_dir: Path | str, data: dict[str, Any]) -> None:
    write_json_atomic(_path(data_dir), data)


# --------------------------------------------------------------------------- #
# Busy detection
# --------------------------------------------------------------------------- #


def conversation_busy(app: Any, conversation_id: str) -> bool:
    """Is a turn active (or user messages queued) in this conversation?

    Imported lazily to keep this module dependency-light (chat_state imports a
    large surface). ``app`` may be ``None`` (headless callers) → not busy."""
    if app is None:
        return False
    try:
        from akana_server.api.chat_turn_queue import queue_depth
        from akana_server.api.routes.chat.chat_state import _active_turns

        conv_id = (conversation_id or "").strip()
        if not conv_id:
            return False
        if conv_id in _active_turns(app):
            return True
        return queue_depth(app, conv_id) > 0
    except Exception:  # pragma: no cover - registry probe must never break delivery
        log.debug("chat_injections: busy probe failed", exc_info=True)
        return False


# --------------------------------------------------------------------------- #
# The public API
# --------------------------------------------------------------------------- #


async def deliver_or_queue(
    app: Any,
    settings: Any,
    conversation_id: str,
    text: str,
    *,
    kind: str = "schedule",
    title: str = "",
) -> str:
    """Deliver ``text`` into the conversation now, or park it until the turn ends.

    Returns ``"delivered"`` (written + broadcast), ``"queued"`` (parked in the
    durable inbox — drained on turn completion), or ``"dropped"`` (blank text /
    unknown conversation / store full)."""
    conv_id = (conversation_id or "").strip()
    body = (text or "").strip()
    if not conv_id or not body:
        return "dropped"
    if conversation_busy(app, conv_id):
        return await asyncio.to_thread(
            _enqueue, settings.data_dir, conv_id, body, kind, title
        )
    return await _inject_now(app, settings, conv_id, body, kind=kind, title=title)


def _enqueue(data_dir: Path | str, conv_id: str, body: str, kind: str, title: str) -> str:
    with cross_process_lock(data_dir, _path(data_dir)):
        data = _load(data_dir)
        items = data["pending"].setdefault(conv_id, [])
        if len(items) >= MAX_PENDING_PER_CONV:
            log.warning(
                "chat_injections: inbox full for conv=%s — dropping %r", conv_id, title
            )
            return "dropped"
        items.append(
            {
                "id": str(ulid.new()),
                "text": body,
                "kind": kind,
                "title": title,
                "created_at": iso_now(),
            }
        )
        _save(data_dir, data)
    log.info("chat_injections: queued %s injection for busy conv=%s", kind, conv_id)
    return "queued"


async def _inject_now(
    app: Any,
    settings: Any,
    conv_id: str,
    body: str,
    *,
    kind: str,
    title: str,
) -> str:
    """Write the assistant turn + context note + broadcast — the actual delivery."""

    def _write() -> str:
        from akana_server.conversation_service import ConversationService
        from akana_server.orchestrator.turn_writer import persist_assistant_turn

        svc = ConversationService(Path(settings.data_dir))
        try:
            meta = svc.get(conv_id)
        except Exception:
            meta = None
        if meta is None:
            log.warning(
                "chat_injections: conv=%s no longer exists — dropping %s injection",
                conv_id,
                kind,
            )
            return ""
        turn_id = persist_assistant_turn(
            conversation_id=conv_id,
            assistant_text=body,
            user_turn_id="",  # a standalone assistant message (no paired user turn)
            data_dir=Path(settings.data_dir),
        )
        # Context note for agent-resume providers (claude/cursor/codex): the next
        # turn prepends this so the model knows what was injected while it was away.
        note = body if len(body) <= NOTE_CLIP_CHARS else body[: NOTE_CLIP_CHARS - 1] + "…"
        header = f"[{kind}:{title}] " if title else f"[{kind}] "
        _add_note(settings.data_dir, conv_id, header + note)
        return turn_id

    try:
        turn_id = await asyncio.to_thread(_write)
    except Exception:  # noqa: BLE001 - delivery must never break the producer
        log.exception("chat_injections: persist failed (conv=%s)", conv_id)
        return "dropped"
    if not turn_id:
        return "dropped"
    # LIVE UI: the open conversation reloads its log (the message appears in place);
    # a non-open conversation gets the sidebar refresh + "response ready" toast.
    from akana_server.conversation_events import broadcast_turn_completed

    await broadcast_turn_completed(
        app, conv_id, status="ok", assistant_turn_id=turn_id
    )
    return "delivered"


async def drain_pending(app: Any, settings: Any, conversation_id: str) -> int:
    """Deliver parked injections for a conversation (called after a turn ends).

    Re-checks busyness per item: if a new user turn starts mid-drain, the rest
    stays parked for the next completion. Returns how many were delivered."""
    conv_id = (conversation_id or "").strip()
    if not conv_id:
        return 0
    delivered = 0
    while not conversation_busy(app, conv_id):
        item = await asyncio.to_thread(_pop_first, settings.data_dir, conv_id)
        if item is None:
            break
        result = await _inject_now(
            app,
            settings,
            conv_id,
            str(item.get("text") or ""),
            kind=str(item.get("kind") or "task"),
            title=str(item.get("title") or ""),
        )
        if result == "delivered":
            delivered += 1
    return delivered


def _pop_first(data_dir: Path | str, conv_id: str) -> dict[str, Any] | None:
    with cross_process_lock(data_dir, _path(data_dir)):
        data = _load(data_dir)
        items = data["pending"].get(conv_id) or []
        if not items:
            return None
        item = items.pop(0)
        if items:
            data["pending"][conv_id] = items
        else:
            data["pending"].pop(conv_id, None)
        _save(data_dir, data)
        return item


async def drain_all_pending(app: Any, settings: Any) -> int:
    """Startup sweep: deliver everything left over from before a restart."""
    try:
        data = await asyncio.to_thread(_load, settings.data_dir)
    except Exception:  # pragma: no cover - unreadable store already logged in _load
        return 0
    total = 0
    for conv_id in list(data.get("pending", {})):
        total += await drain_pending(app, settings, conv_id)
    if total:
        log.info("chat_injections: startup drain delivered %d parked message(s)", total)
    return total


# --------------------------------------------------------------------------- #
# Context notes (agent-resume memory bridge)
# --------------------------------------------------------------------------- #


def _add_note(data_dir: Path | str, conv_id: str, note: str) -> None:
    with cross_process_lock(data_dir, _path(data_dir)):
        data = _load(data_dir)
        notes = data["notes"].setdefault(conv_id, [])
        notes.append(note)
        if len(notes) > MAX_NOTES_PER_CONV:
            del notes[: len(notes) - MAX_NOTES_PER_CONV]
        _save(data_dir, data)


def pop_context_notes(settings: Any, conversation_id: str) -> list[str]:
    """Consume (return + clear) the pending context notes for a conversation.

    Called by the chat turn builder: the notes are prepended to the user text so
    an agent-resume provider learns what was injected while its session was
    parked. Popping is atomic — a note is delivered to exactly one turn."""
    conv_id = (conversation_id or "").strip()
    if not conv_id:
        return []
    try:
        with cross_process_lock(settings.data_dir, _path(settings.data_dir)):
            data = _load(settings.data_dir)
            notes = [str(n) for n in (data["notes"].pop(conv_id, None) or [])]
            if notes:
                _save(settings.data_dir, data)
            return notes
    except Exception:  # pragma: no cover - a note miss must never break the turn
        log.debug("chat_injections: note pop failed", exc_info=True)
        return []


__all__ = [
    "conversation_busy",
    "deliver_or_queue",
    "drain_all_pending",
    "drain_pending",
    "pop_context_notes",
]
