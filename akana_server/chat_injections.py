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

#: conv_id → (event loop, drain lock). :func:`drain_pending` is reachable from several
#: producers at once (the startup sweep vs an early turn completion; a STOP's
#: injection-only drain vs the drain a fast command turn spawns), and its loop is a
#: peek → deliver → remove, not an atomic pop: two overlapping drains both see the same
#: head, both persist it (a DUPLICATE assistant message) and the loser's removal then
#: drops the next, still-undelivered item. The lock makes one conversation's drains
#: strictly serial. Keyed with the loop it was created on so a process that runs several
#: event loops (tests, a restarted lifespan) never awaits a lock bound to a dead one.
_drain_locks: dict[str, tuple[Any, asyncio.Lock]] = {}


def _drain_lock(conv_id: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    entry = _drain_locks.get(conv_id)
    if entry is None or entry[0] is not loop:
        entry = (loop, asyncio.Lock())
        _drain_locks[conv_id] = entry  # no await above → the swap is atomic
    return entry[1]


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


def _turn_running(app: Any, conversation_id: str) -> bool:
    """Is a turn ACTUALLY running in this conversation — on ANY surface?

    Two registries, not one: streaming turns live in ``_active_turns``, while the
    whole non-streaming surface registers ONLY in ``_nonstreaming_busy`` — the
    blocking ``POST /chat``, the ``/voice`` route, the connector/Telegram worker, and
    (as a short-lived per-utterance claim, not for the whole session) the realtime
    voice bridge. Probing just the streaming registry left the interleaving this
    module exists to prevent wide open for those turns — they persist their
    user+assistant pair only AFTER the LLM returns, so an injection written meanwhile
    is ordered ABOVE the question the user just asked.

    Imported lazily to keep this module dependency-light (chat_state imports a
    large surface). ``app`` may be ``None`` (headless callers) → not running."""
    if app is None:
        return False
    conv_id = (conversation_id or "").strip()
    if not conv_id:
        return False
    try:
        from akana_server.api.routes.chat.chat_state import (
            _active_turns,
            _nonstreaming_busy,
        )

        if conv_id in _active_turns(app):
            return True
        busy = _nonstreaming_busy(app).get(conv_id)
        return busy is not None and not busy.done()
    except Exception:  # pragma: no cover - registry probe must never break delivery
        log.debug("chat_injections: busy probe failed", exc_info=True)
        return False


def conversation_busy(app: Any, conversation_id: str) -> bool:
    """Is a turn running OR a user message queued in this conversation?

    The PARKING decision only: a queued user message means the conversation is
    about to start another turn, so a fresh injection waits. The post-turn DRAIN
    deliberately uses :func:`_turn_running` instead — a parked result must land
    BEFORE the queued follow-up that is probably asking about it."""
    if app is None:
        return False
    if _turn_running(app, conversation_id):
        return True
    try:
        from akana_server.api.chat_turn_queue import queue_depth

        return queue_depth(app, (conversation_id or "").strip()) > 0
    except Exception:  # pragma: no cover - registry probe must never break delivery
        log.debug("chat_injections: queue probe failed", exc_info=True)
        return False


# --------------------------------------------------------------------------- #
# The public API
# --------------------------------------------------------------------------- #


#: Internal outcome of :func:`_inject_now`: the write could not be made RIGHT NOW but
#: the conversation is (as far as we know) still there — a lock/IO hiccup. The caller
#: must KEEP the message (park it / leave it parked), never discard it.
_RETRY = "retry"

#: :func:`deliver_or_queue` outcome: the conversation is ALIVE but its inbox is at
#: :data:`MAX_PENDING_PER_CONV`, so this message was shed. Distinct from ``"dropped"``
#: on purpose: "dropped" means the conversation is GONE, and a caller that conflates the
#: two treats a transient overflow as a deletion — the schedule engine then rehomed the
#: run into a recovery thread and rebound the schedule to it permanently.
FULL = "full"


async def deliver_or_queue(
    app: Any,
    settings: Any,
    conversation_id: str,
    text: str,
    *,
    kind: str = "schedule",
    title: str = "",
    status: str = "ok",
) -> str:
    """Deliver ``text`` into the conversation now, or park it until the turn ends.

    ``status`` is the REAL outcome of the background turn this message reports and
    travels with the message all the way to the ``turn_completed`` broadcast: the
    desktop notifier and the sidebar toast gate on it, so a failure report must not
    be announced as finished work.

    Returns ``"delivered"`` (written + broadcast), ``"queued"`` (parked in the durable
    inbox — drained on turn completion), :data:`FULL` (the conversation is alive but its
    inbox is at capacity) or ``"dropped"`` (blank text / the conversation is GONE)."""
    conv_id = (conversation_id or "").strip()
    body = (text or "").strip()
    if not conv_id or not body:
        return "dropped"
    if not conversation_busy(app, conv_id):
        outcome = await _inject_now(
            app, settings, conv_id, body, kind=kind, title=title, status=status
        )
        if outcome != _RETRY:
            return outcome
        # Transient write failure — park it rather than lose it (the inbox is durable,
        # so the next turn completion or a restart sweep still delivers it).
    return await asyncio.to_thread(
        _enqueue, settings.data_dir, conv_id, body, kind, title, status
    )


def _enqueue(
    data_dir: Path | str,
    conv_id: str,
    body: str,
    kind: str,
    title: str,
    status: str = "ok",
) -> str:
    with cross_process_lock(data_dir, _path(data_dir)):
        data = _load(data_dir)
        items = data["pending"].setdefault(conv_id, [])
        if len(items) >= MAX_PENDING_PER_CONV:
            log.warning(
                "chat_injections: inbox full for conv=%s — dropping %r", conv_id, title
            )
            return FULL
        items.append(
            {
                "id": str(ulid.new()),
                "text": body,
                "kind": kind,
                "title": title,
                "status": status,
                "created_at": iso_now(),
            }
        )
        _save(data_dir, data)
    log.info("chat_injections: queued %s injection for busy conv=%s", kind, conv_id)
    return "queued"


def _turn_landed(data_dir: Path | str, turn_id: str) -> bool:
    """Is ``turn_id`` actually a row in ``memory.db``?

    The only honest proof that an injection was written: ``turn_writer`` reports its
    own db failures to the LOG, not to its caller. A probe failure counts as NOT
    landed — the item then stays parked, which is the safe direction (the inbox is
    durable, a later drain or the restart sweep retries it)."""
    try:
        from akana_server.memory_core import get_memory_core

        return get_memory_core(Path(data_dir)).episodic.get_turn(turn_id) is not None
    except Exception:  # noqa: BLE001 - unreadable store → treat as not written
        log.debug("chat_injections: turn read-back failed (turn=%s)", turn_id, exc_info=True)
        return False


async def _inject_now(
    app: Any,
    settings: Any,
    conv_id: str,
    body: str,
    *,
    kind: str,
    title: str,
    status: str = "ok",
) -> str:
    """Write the assistant turn + context note + broadcast — the actual delivery.

    Returns ``"delivered"``, ``"dropped"`` (the conversation is genuinely GONE — the
    message is undeliverable, forever) or :data:`_RETRY` (a transient failure: the
    caller must keep the message). Conflating the two is data loss: a sqlite lock
    hiccup during the drain — precisely the concurrent-turn regime that parks items
    in the first place — used to be reported as "the conversation was deleted"."""

    def _write() -> "tuple[str, str]":
        """``(outcome, turn_id)`` — outcome is "delivered" / "dropped" / :data:`_RETRY`."""
        from akana_server.conversation_service import ConversationService
        from akana_server.orchestrator import turn_writer

        svc = ConversationService(Path(settings.data_dir))
        # A RAISING get() is a transient store failure, NOT a deletion: only an
        # explicit None means the conversation is gone.
        meta = svc.get(conv_id)
        if meta is None:
            log.warning(
                "chat_injections: conv=%s no longer exists — dropping %s injection",
                conv_id,
                kind,
            )
            return ("dropped", "")
        turn_id = turn_writer.persist_assistant_turn(
            conversation_id=conv_id,
            assistant_text=body,
            user_turn_id="",  # a standalone assistant message (no paired user turn)
            data_dir=Path(settings.data_dir),
        )
        # A TURN ID IS NOT A RECEIPT. ``turn_writer`` catches every db failure, logs it
        # loudly and falls off the end — while ``persist_assistant_turn`` hands back the
        # ULID it MINTED regardless. Taking that id as proof told the user "your response
        # is ready", pointed them at a row that does not exist, and let ``drain_pending``
        # delete the durable inbox copy forever. Confirm the row landed; a miss is a
        # TRANSIENT failure (the item stays parked), never a deletion.
        if not turn_id or not _turn_landed(settings.data_dir, turn_id):
            log.warning(
                "chat_injections: %s injection for conv=%s was not persisted — keeping it",
                kind,
                conv_id,
            )
            return (_RETRY, "")
        # Context note for agent-resume providers (claude/cursor/codex): the next
        # turn prepends this so the model knows what was injected while it was away.
        # A note failure must not re-run the (already persisted) turn → swallowed.
        try:
            note = (
                body if len(body) <= NOTE_CLIP_CHARS else body[: NOTE_CLIP_CHARS - 1] + "…"
            )
            header = f"[{kind}:{title}] " if title else f"[{kind}] "
            _add_note(settings.data_dir, conv_id, header + note)
        except Exception:  # noqa: BLE001 - the note bridge is best-effort
            log.debug("chat_injections: note write failed (conv=%s)", conv_id, exc_info=True)
        return ("delivered", turn_id)

    try:
        outcome, turn_id = await asyncio.to_thread(_write)
    except Exception:  # noqa: BLE001 - delivery must never break the producer
        log.exception("chat_injections: persist failed (conv=%s) — keeping it", conv_id)
        return _RETRY
    if outcome != "delivered":
        return outcome
    # LIVE UI: the open conversation reloads its log (the message appears in place);
    # a non-open conversation gets the sidebar refresh + "response ready" toast. The
    # status is the background turn's REAL outcome — the notifier refuses to announce
    # a failed job as finished work, which it can only do if we tell the truth here.
    from akana_server.conversation_events import broadcast_turn_completed

    await broadcast_turn_completed(
        app, conv_id, status=str(status or "ok"), assistant_turn_id=turn_id
    )
    return "delivered"


async def drain_pending(app: Any, settings: Any, conversation_id: str) -> int:
    """Deliver parked injections for a conversation (called after a turn ends).

    Gated on a RUNNING turn only — NOT on ``conversation_busy``: the caller's whole
    reason for draining here is that a parked result must land before the next
    QUEUED user message, which is very likely asking about it. Re-checked per item,
    so a new turn starting mid-drain leaves the rest parked for the next completion.

    DELIVER-THEN-REMOVE: the item leaves the durable inbox only once it is actually
    written. A transient failure (or a crash) between the two leaves it parked — the
    inbox exists to make this promise, and popping first broke it exactly at the
    handoff. The whole loop is serialised per conversation (:func:`_drain_lock`) because
    deliver-then-remove is only safe when nobody else is peeking the same head.
    Returns how many were delivered."""
    conv_id = (conversation_id or "").strip()
    if not conv_id:
        return 0
    delivered = 0
    async with _drain_lock(conv_id):
        while not _turn_running(app, conv_id):
            item = await asyncio.to_thread(_peek_first, settings.data_dir, conv_id)
            if item is None:
                break
            result = await _inject_now(
                app,
                settings,
                conv_id,
                str(item.get("text") or ""),
                kind=str(item.get("kind") or "task"),
                title=str(item.get("title") or ""),
                status=str(item.get("status") or "ok"),
            )
            if result == _RETRY:
                break  # keep it parked; the next completion (or the restart sweep) retries
            await asyncio.to_thread(
                _remove_item, settings.data_dir, conv_id, str(item.get("id") or "")
            )
            if result == "delivered":
                delivered += 1
            else:
                # UNDELIVERABLE (the conversation is gone). The producer handed its ONE
                # turn_completed to this delivery the moment the item was parked, so
                # dropping the item silently leaves that turn_active latched forever.
                # Report the truth: the promised result is not coming.
                from akana_server.conversation_events import broadcast_turn_completed

                await broadcast_turn_completed(app, conv_id, status="error")
    return delivered


def _peek_first(data_dir: Path | str, conv_id: str) -> dict[str, Any] | None:
    with cross_process_lock(data_dir, _path(data_dir)):
        items = _load(data_dir)["pending"].get(conv_id) or []
        return items[0] if items else None


def _remove_item(data_dir: Path | str, conv_id: str, item_id: str) -> None:
    """Delete ONE delivered/undeliverable item by id (position-independent: another
    process may have appended to the same conversation meanwhile).

    Exactly one match is removed, and NOTHING is removed when there is no match. Removing
    "the head anyway" on a miss deleted an item that was never delivered (the head is a
    DIFFERENT message once a peer drain removed ours), and filtering by id deleted every
    legacy id-less row at once. A miss cannot loop forever either: it means our item is
    already gone, so the next peek returns a different head."""
    with cross_process_lock(data_dir, _path(data_dir)):
        data = _load(data_dir)
        items = data["pending"].get(conv_id) or []
        kept: list[dict[str, Any]] | None = None
        for idx, item in enumerate(items):
            if str(item.get("id") or "") == item_id:
                kept = items[:idx] + items[idx + 1 :]
                break
        if kept is None:
            return
        if kept:
            data["pending"][conv_id] = kept
        else:
            data["pending"].pop(conv_id, None)
        _save(data_dir, data)


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
    "FULL",
    "conversation_busy",
    "deliver_or_queue",
    "drain_all_pending",
    "drain_pending",
    "pop_context_notes",
]
