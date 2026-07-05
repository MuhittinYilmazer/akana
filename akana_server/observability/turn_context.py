"""Turn-level correlation context (``trace_id``) and log injection.

Design decisions:

* **contextvars** — carries ``trace_id`` through a deep async call chain
  (gate → context assembler → provider client → persist) without adding it to
  every function signature. Because ``asyncio.to_thread`` copies the context,
  synchronous side-effects moved to a worker thread via ``_off_loop``
  (policy.db, persist) also carry the same ``trace_id`` in their logs.
* **Task isolation** — every ``asyncio.Task`` receives a COPY of the current
  context at creation time. A handler/task that calls ``begin_turn`` at the
  start of a turn has its own ``trace_id``; no reset is needed (the context
  is scoped to the request/task lifetime), so the API is additive and low-risk.
* **reuse=True** — a detached streaming task INHERITS the ``trace_id`` from the
  calling context (e.g. ``post_chat_stream``); gate and stream are unified under
  one id. If there is no context to inherit in a background/drain path, a fresh
  id is generated.
"""

from __future__ import annotations

import contextvars
import dataclasses
import logging

import ulid


@dataclasses.dataclass(frozen=True)
class TurnContext:
    """Correlation identity and coarse classification of a single chat turn.

    ``trace_id`` is FIXED for the lifetime of the turn (never regenerated).
    The ``provider`` and ``mode`` fields can be enriched via ``update_turn`` as
    the turn progresses; they are read from the log/anchor line in "wrong
    provider responded" failures.
    """

    trace_id: str
    conversation_id: str | None = None
    provider: str | None = None
    mode: str | None = None


_current: contextvars.ContextVar[TurnContext | None] = contextvars.ContextVar(
    "akana_turn_context", default=None
)


def new_trace_id() -> str:
    """Short, sortable correlation id (same ulid as used elsewhere in the codebase)."""

    return str(ulid.new())


def current_turn() -> TurnContext | None:
    """The active turn in this context (``None`` if there is none)."""

    return _current.get()


def current_trace_id() -> str:
    """The active turn's ``trace_id``; returns ``"-"`` outside a turn (log-format safe)."""

    ctx = _current.get()
    return ctx.trace_id if ctx is not None else "-"


def begin_turn(
    conversation_id: str | None = None,
    *,
    provider: str | None = None,
    mode: str | None = None,
    reuse: bool = True,
) -> TurnContext:
    """Start a turn for this context (idempotent).

    If ``reuse=True`` (default) and an active turn already exists: a new id is
    NOT generated; only missing fields (conversation_id/provider/mode) are
    filled in and the existing ``trace_id`` is preserved. This allows inheriting
    paths such as ``post_chat_stream`` → detached task to be unified under one
    id. Otherwise a fresh turn is created.
    """

    if reuse:
        existing = _current.get()
        if existing is not None:
            merged = dataclasses.replace(
                existing,
                conversation_id=existing.conversation_id or conversation_id,
                provider=existing.provider or provider,
                mode=existing.mode or mode,
            )
            if merged != existing:
                _current.set(merged)
            return merged

    ctx = TurnContext(
        trace_id=new_trace_id(),
        conversation_id=conversation_id,
        provider=provider,
        mode=mode,
    )
    _current.set(ctx)
    return ctx


def update_turn(**changes: object) -> TurnContext | None:
    """Update fields of the active turn (e.g. when the provider is resolved). No-op if no turn."""

    existing = _current.get()
    if existing is None:
        return None
    updated = dataclasses.replace(existing, **changes)  # type: ignore[arg-type]
    _current.set(updated)
    return updated


class TurnLogFilter(logging.Filter):
    """Injects the active turn's ``trace_id`` into every ``LogRecord``.

    Attached to a Handler: because handler filters run BEFORE formatting,
    ``%(trace_id)s`` in that handler's format string is always populated.
    Records produced outside a turn receive ``"-"`` — the format never crashes.
    None of the 110+ existing ``log.*`` call sites need to change; ``trace_id``
    is added via the shared handler.
    """

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        if not hasattr(record, "trace_id"):
            record.trace_id = current_trace_id()
        return True
