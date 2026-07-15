"""ScheduleEngine — the ``ScheduleItem`` record and its (de)serialisation.

A ``ScheduleItem`` is one user- or assistant-created reminder / recurring
prompt. When it comes due the engine runs an LLM turn with :attr:`prompt` and
delivers the result to a chat thread and/or an outbound connector.

Four kinds of schedule are supported — NO cron-expression dependency; the
recurrence is expressed with plain fields and computed with datetime math in
Turkey local time (fixed +03:00, no DST — the project convention):

* ``once``     — fire a single time. :attr:`when` is an ISO-8601 datetime.
* ``interval`` — fire every N seconds. :attr:`when` is the integer seconds.
* ``daily``    — fire every day at ``HH:MM``. :attr:`when` is ``"HH:MM"``.
* ``weekly``   — fire every week at ``HH:MM`` on :attr:`weekday`
  (0=Monday … 6=Sunday). :attr:`when` is ``"HH:MM"``.

The store precomputes :attr:`next_run_at` (an ISO string) so the due-query is a
cheap timestamp comparison rather than a per-item recurrence evaluation. A
``once`` item disables itself after firing; a recurring item recomputes
:attr:`next_run_at` for its next occurrence.

This module is intentionally pure data: no I/O, no datetime math (that lives in
:mod:`akana_server.schedule.store`). The dataclasses only know how to round-trip
themselves to/from the JSON-clean dicts the store persists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "KINDS",
    "DELIVERY_MODES",
    "CREATED_BY",
    "Delivery",
    "ScheduleItem",
]

#: The four recurrence kinds (see the module docstring). A cron string is
#: deliberately NOT one of them — recurrence is plain-field + datetime math.
KINDS: frozenset[str] = frozenset({"once", "interval", "daily", "weekly"})

#: How a fired run is delivered. ``thread`` appends the exchange to a visible
#: chat conversation; ``connector`` pushes it out over an outbound channel
#: (e.g. Telegram); ``both`` does both.
DELIVERY_MODES: frozenset[str] = frozenset({"thread", "connector", "both"})

#: Who created the schedule — a human via the UI/REST or the assistant via a tool.
CREATED_BY: frozenset[str] = frozenset({"user", "assistant"})


@dataclass(slots=True)
class Delivery:
    """Where a fired schedule's result goes.

    * ``mode``            — one of :data:`DELIVERY_MODES`.
    * ``channel``         — connector id (e.g. ``"telegram"``); required for the
      ``connector`` / ``both`` modes, ignored for ``thread``.
    * ``chat_id``         — the connector-side chat/peer id to send to; required
      for ``connector`` / ``both``.
    * ``conversation_id`` — an EXISTING chat thread to append into. When empty
      (and the mode includes ``thread``) the engine creates a new conversation
      titled from the schedule and writes the created id back here, so the next
      run of a recurring schedule appends to the SAME thread instead of spawning
      a fresh one every time.
    """

    mode: str = "thread"
    channel: str = ""
    chat_id: str = ""
    conversation_id: str | None = None
    #: SAME-CHAT delivery: the schedule was created FROM a live conversation and
    #: its fires are INJECTED into that conversation as assistant messages
    #: (busy-safe, via chat_injections) — no prompt/user-turn pair is appended.
    #: False = the classic engine-owned thread (pair-append) behavior.
    same_chat: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "channel": self.channel,
            "chat_id": self.chat_id,
            "conversation_id": self.conversation_id,
            "same_chat": bool(self.same_chat),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "Delivery":
        """Build from a stored dict; a missing/broken shape degrades to a safe
        thread-only delivery (never raises — corruption tolerance)."""
        if not isinstance(raw, dict):
            return cls()
        mode = str(raw.get("mode") or "thread").strip().lower()
        if mode not in DELIVERY_MODES:
            mode = "thread"
        conv = raw.get("conversation_id")
        return cls(
            mode=mode,
            channel=str(raw.get("channel") or "").strip(),
            chat_id=str(raw.get("chat_id") or "").strip(),
            conversation_id=str(conv).strip() if conv else None,
            same_chat=bool(raw.get("same_chat")),
        )


@dataclass(slots=True)
class ScheduleItem:
    """One schedule record. See the module docstring for the field contract.

    ``last_run`` is a small dict written after each fire:
    ``{"at": iso, "status": "ok"|"error"|"skipped", "error"?: str,
    "conversation_id"?: str}``. It is ``None`` until the first run.
    """

    id: str
    title: str
    prompt: str
    kind: str
    when: str
    next_run_at: str
    #: VERBATIM reminder body (mutually exclusive with :attr:`prompt`). When set,
    #: the engine delivers this text AS-IS and runs NO LLM turn — the fast, free
    #: path for a plain "remind me to X" reminder (a scheduled LLM run would
    #: otherwise 'riff' on the literal text instead of just repeating it). Exactly
    #: one of ``prompt`` (run an LLM turn) or ``message`` (deliver verbatim) is set;
    #: the store enforces that at create/update time. Default "" = the classic
    #: prompt-driven schedule (so old rows, which have no ``message`` key, load
    #: unchanged — see :meth:`from_dict`).
    message: str = ""
    enabled: bool = True
    weekday: int | None = None
    delivery: Delivery = field(default_factory=Delivery)
    created_by: str = "user"
    language: str = "en"
    created_at: str = ""
    last_run: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-clean dict for the store."""
        return {
            "id": self.id,
            "title": self.title,
            "prompt": self.prompt,
            "message": self.message,
            "kind": self.kind,
            "when": self.when,
            "next_run_at": self.next_run_at,
            "enabled": bool(self.enabled),
            "weekday": self.weekday,
            "delivery": self.delivery.to_dict(),
            "created_by": self.created_by,
            "language": self.language,
            "created_at": self.created_at,
            "last_run": self.last_run,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ScheduleItem":
        """Rebuild from a stored dict. The caller (store) filters out rows that
        raise here, so this stays strict about the identity/kind fields it needs
        while being forgiving about optional metadata."""
        weekday = raw.get("weekday")
        try:
            weekday_val = int(weekday) if weekday is not None else None
        except (TypeError, ValueError):
            weekday_val = None
        last_run = raw.get("last_run")
        return cls(
            id=str(raw["id"]),
            title=str(raw.get("title") or ""),
            prompt=str(raw.get("prompt") or ""),
            # TOLERANT: rows written before the verbatim-message feature have no
            # ``message`` key — default to "" so they keep loading as prompt-driven.
            message=str(raw.get("message") or ""),
            kind=str(raw["kind"]),
            when=str(raw.get("when") or ""),
            next_run_at=str(raw.get("next_run_at") or ""),
            enabled=bool(raw.get("enabled", True)),
            weekday=weekday_val,
            delivery=Delivery.from_dict(raw.get("delivery")),
            created_by=str(raw.get("created_by") or "user"),
            language=str(raw.get("language") or "en"),
            created_at=str(raw.get("created_at") or ""),
            last_run=last_run if isinstance(last_run, dict) else None,
        )

    def public_dict(self) -> dict[str, Any]:
        """Dict for the REST/tool surface — identical to :meth:`to_dict` today
        (a schedule carries no secret material), kept as a distinct method so a
        future field that must NOT leave the server can be dropped here without
        touching the persistence shape."""
        return self.to_dict()
