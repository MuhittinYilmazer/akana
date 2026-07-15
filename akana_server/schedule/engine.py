"""ScheduleEngine — the background poll loop that fires due schedules.

Lifecycle mirrors the memory crons (``session_closer_service`` /
``summary_consolidation_service``): a single always-set-up
:func:`akana_server.orchestrator._bridge_cron.poll_loop` task that, each turn,
asks the store for due items and runs them. It is started from the app lifespan
via :func:`start_schedule_engine` and torn down via :func:`stop_schedule_engine`.
(The wiring into ``app.py`` is done centrally — see this module's report notes.)

For each due schedule the engine:

1. runs ONE non-streamed LLM turn with the schedule's ``prompt`` (via
   :func:`llm_dispatch.complete_chat_aggregated` — text + usage + tool calls),
   under the default Akana persona;
2. delivers the result — appends the exchange into a visible chat conversation
   (creating one titled from the schedule when needed, so the user SEES it in
   the web UI) and/or pushes it out over a connector (Telegram) through the
   registry's ``send_to`` seam, which applies the egress secret/PII filter;
3. records the outcome and advances the schedule — a ``once`` item disables
   itself; a recurring item rolls ``next_run_at`` forward to its next FUTURE
   occurrence (catch-up policy: a schedule missed while the server was off fires
   exactly ONCE, never a backlog storm).

FAILURE ISOLATION is the load-bearing property: an unconfigured provider
(``LLMCallError`` 503), a delivery error, or any other exception is caught, the
run is marked failed/skipped, and the loop keeps going — a scheduled turn can
never crash the engine or the server.

Schedules run SEQUENTIALLY (one LLM turn at a time) — the engine does not fan
out concurrent provider calls.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from akana_server.orchestrator import llm_dispatch
from akana_server.orchestrator._bridge_cron import (
    poll_loop as _poll_loop,
    start_task as _start_task,
    stop_task as _stop_task,
)
from akana_server.schedule.model import ScheduleItem
from akana_server.schedule.store import (
    ScheduleStore,
    get_schedule_store,
    now_tr,
)

log = logging.getLogger(__name__)

_TASK_ATTR = "schedule_engine_task"
#: When (hypothetically) inactive, how often to re-check the gate. The engine is
#: effectively always active (the poll interval floor is 5s), so this is only the
#: shared ``poll_loop`` contract's inactive cadence.
_DISABLED_CHECK_SECONDS = 60.0
_DEFAULT_POLL_SECONDS = 30.0
_MIN_POLL_SECONDS = 5.0

#: The LLM call signature the engine drives. Returns (text, usage, agent_id).
CompleteFn = Callable[..., Awaitable[tuple[str, dict[str, Any], str | None]]]


# --------------------------------------------------------------------------- #
# Runtime gate (poll cadence)
# --------------------------------------------------------------------------- #


def poll_seconds(settings: Any) -> float:
    """Resolved poll interval (runtime > env > default), floored at 5s.

    Reads ``schedule_poll_seconds`` defensively — a settings-resolution failure
    must never break the loop, so it falls back to the default."""
    try:
        from akana_server.runtime_settings import get_runtime

        value = float(get_runtime("schedule_poll_seconds", settings))
    except Exception:
        value = _DEFAULT_POLL_SECONDS
    return max(_MIN_POLL_SECONDS, value)


def schedule_engine_active(settings: Any) -> bool:
    """The engine is always active (it does nothing when there are no due
    schedules, so there is no separate on/off — per the owner mandate no
    ``schedule_enabled`` switch is added). Kept as a seam for the shared
    ``poll_loop`` active-gate contract."""
    return poll_seconds(settings) > 0


# --------------------------------------------------------------------------- #
# Delivery helpers (kept small + monkeypatchable for hermetic tests)
# --------------------------------------------------------------------------- #


def _build_system_prompt(settings: Any, item: ScheduleItem) -> str | None:
    """Default Akana persona system prompt for the schedule's language.

    Any failure degrades to ``None`` (the provider then inserts its own default
    persona prefix) — persona resolution must never block a scheduled run."""
    try:
        from akana_server.persona.builtin import builtin_personas

        lang = (item.language or "en").strip().lower()
        return builtin_personas(lang)[0].system_prompt
    except Exception:  # pragma: no cover - persona import/edge failure
        return None


def _mcp_servers(settings: Any, conversation_id: str | None) -> dict[str, Any] | None:
    """The built-in MCP tool payload for the scheduled turn (memory/vault/
    schedule + external servers), so a briefing prompt can recall memory. A
    failure degrades to no tools (a plain agent run)."""
    try:
        from akana_server.orchestrator.memory_tools import mcp_servers_payload

        return mcp_servers_payload(settings, conversation_id)
    except Exception:  # pragma: no cover - payload build must not block a run
        return None


def _append_turn_pair(
    data_dir: Any, conversation_id: str, prompt: str, result: str
) -> None:
    """Write the (prompt → result) pair into a conversation via the SAME single
    writer chat/voice use (``turn_writer``). Factored out as a module function so
    tests can patch it without a real ``memory.db``."""
    from akana_server.orchestrator.turn_writer import (
        persist_assistant_turn,
        persist_user_turn,
    )

    dd = Path(data_dir) if data_dir is not None else None
    uid = persist_user_turn(
        conversation_id=conversation_id, user_text=prompt, data_dir=dd
    )
    persist_assistant_turn(
        conversation_id=conversation_id,
        assistant_text=result,
        user_turn_id=uid,
        data_dir=dd,
    )


def _deliver_thread(
    settings: Any, conversations: Any, item: ScheduleItem, text: str
) -> str | None:
    """Append the exchange into a chat thread; returns the conversation id used.

    Reuses ``item.delivery.conversation_id`` when it still exists; otherwise
    creates a NEW conversation titled from the schedule (so a recurring schedule
    keeps landing in one growing thread once the id is written back by
    ``mark_ran``). Returns ``None`` when no conversation service is available."""
    if conversations is None:
        return None
    cid = item.delivery.conversation_id
    if cid:
        try:
            if conversations.get(cid) is None:
                cid = None  # thread was deleted → start a fresh one
        except Exception:
            cid = None
    if not cid:
        meta = conversations.create(title=item.title or "Scheduled")
        cid = getattr(meta, "id", None)
    if not cid:
        return None
    _append_turn_pair(getattr(settings, "data_dir", None), cid, item.prompt, text)
    return cid


async def _deliver_connector(
    registry: Any, item: ScheduleItem, text: str
) -> tuple[bool, str | None]:
    """Push the result out over a connector via ``registry.send_to`` (which
    applies the egress secret/PII filter). SAFETY: connector delivery only
    happens when the connector is actually registered/enabled — a schedule
    targeting a disabled channel is skipped with a note, never an error. Returns
    ``(delivered, note)``."""
    if registry is None:
        return False, "no connector registry"
    channel = item.delivery.channel
    if not channel or registry.get(channel) is None:
        return False, f"connector {channel!r} is not enabled"
    await registry.send_to(channel, item.delivery.chat_id, text)
    return True, None


# --------------------------------------------------------------------------- #
# Running a single schedule + the due sweep
# --------------------------------------------------------------------------- #


def _same_chat_body(item: ScheduleItem, body: str) -> str:
    """The injected same-chat message: a compact reminder header + the LLM result.

    The header names WHICH schedule fired (the user may have several); the body is
    the model's own reminder text. Language follows the schedule's stored language
    (set at create time from the runtime toggle)."""
    turkish = str(item.language or "").lower().startswith("tr")
    head = f"⏰ Hatırlatma: «{item.title}»" if turkish else f"⏰ Reminder: «{item.title}»"
    return f"{head}\n\n{body}"


async def _run_one(
    settings: Any,
    store: ScheduleStore,
    item: ScheduleItem,
    *,
    registry: Any,
    conversations: Any,
    now,
    complete: CompleteFn | None,
    app: Any = None,
    advance: bool = True,
) -> dict[str, Any]:
    """Run one schedule end-to-end (LLM or verbatim → deliver → record). NEVER
    raises except :class:`asyncio.CancelledError`; every other failure is captured
    into the schedule's ``last_run`` and returned as an outcome dict.

    ``advance`` maps to ``mark_ran(roll_forward=...)``: the sweep advances a
    recurring schedule to its next slot (``True``); the manual 'run now' path fires
    out of band and leaves the slot intact (``False`` — see BUG 8)."""
    # BUG 9 — verbatim message mode: a plain reminder ("remind me to X") carries a
    # literal ``message`` and MUST NOT run an LLM turn. Running one made the model
    # 'riff' on the reminder text instead of just repeating it (weird output, a
    # wasted + slow provider call). When ``message`` is set we skip the LLM (and its
    # MCP tool payload / persona) entirely and deliver the text as-is.
    verbatim = (item.message or "").strip()
    if verbatim:
        body = verbatim
    else:
        run_complete = complete or llm_dispatch.complete_chat_aggregated
        system_prompt = _build_system_prompt(settings, item)
        mcp = _mcp_servers(settings, item.delivery.conversation_id)

        # 1) LLM turn. An unconfigured provider raises LLMCallError (503); any
        #    exception is caught → the schedule is marked failed and rolled forward.
        try:
            text, _usage, _agent = await run_complete(
                settings,
                item.prompt,
                system_prompt=system_prompt,
                mcp_servers=mcp,
                conversation_id=item.delivery.conversation_id,
                reuse_agent=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a scheduled turn must never crash the loop
            log.warning("schedule %s: LLM run failed: %s", item.id, exc, exc_info=True)
            store.mark_ran(item.id, status="error", error=str(exc), now=now, roll_forward=advance)
            return {"id": item.id, "status": "error", "error": str(exc)}

        body = (text or "").strip()

    if not body:
        # Nothing to deliver; still advance the schedule so it does not re-fire.
        store.mark_ran(item.id, status="skipped", error="empty result", now=now, roll_forward=advance)
        return {"id": item.id, "status": "skipped", "error": "empty result"}

    # 2) Delivery — thread and/or connector, each isolated. Track per-target
    #    outcomes and COLLECT notes: in "both" mode a thread success must not mask a
    #    connector failure (the user watching Telegram would see nothing while the UI
    #    showed green). A failure on any REQUESTED target downgrades the status.
    mode = item.delivery.mode
    conversation_id: str | None = None
    notes: list[str] = []
    thread_ok = connector_ok = None  # None = not requested; True/False = outcome

    if mode in ("thread", "both"):
        thread_ok = False
        try:
            if item.delivery.same_chat and item.delivery.conversation_id and app is not None:
                # SAME-CHAT: the reminder was created FROM this conversation — inject
                # the result there as a single assistant message (no prompt/user-turn
                # pair), busy-safe: if the user's own turn is streaming, the message
                # parks in the durable inbox and lands the moment that turn ends.
                # deliver_or_queue broadcasts the live event itself on real delivery.
                from akana_server.chat_injections import deliver_or_queue

                outcome = await deliver_or_queue(
                    app,
                    settings,
                    str(item.delivery.conversation_id),
                    _same_chat_body(item, body),
                    kind="schedule",
                    title=item.title,
                )
                conversation_id = (
                    str(item.delivery.conversation_id)
                    if outcome in ("delivered", "queued")
                    else None
                )
                if conversation_id:
                    thread_ok = True
                else:
                    notes.append("same-chat injection dropped (conversation gone?)")
            else:
                conversation_id = await asyncio.to_thread(
                    _deliver_thread, settings, conversations, item, body
                )
                if conversation_id:
                    thread_ok = True
                else:
                    notes.append("no conversation service available")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("schedule %s: thread delivery failed: %s", item.id, exc, exc_info=True)
            notes.append(f"thread delivery error: {exc}")

    if mode in ("connector", "both"):
        connector_ok = False
        try:
            ok, connector_note = await _deliver_connector(registry, item, body)
            connector_ok = ok
            if connector_note:
                notes.append(connector_note)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("schedule %s: connector delivery failed: %s", item.id, exc, exc_info=True)
            notes.append(f"connector delivery error: {exc}")

    requested = [x for x in (thread_ok, connector_ok) if x is not None]
    delivered_any = any(requested)
    all_ok = bool(requested) and all(requested)
    note = "; ".join(notes) or None
    # "ok" only if EVERY requested target succeeded; "partial" if some did; else "skipped".
    status = "ok" if all_ok else "partial" if delivered_any else "skipped"
    store.mark_ran(
        item.id,
        status=status,
        error=note if status != "ok" else None,
        conversation_id=conversation_id,
        now=now,
        roll_forward=advance,
    )
    # LIVE UI: a thread was created/updated → announce it so the sidebar shows the
    # reminder's thread and a "ready" toast fires, without a page refresh. Only when
    # a thread actually received content (conversation_id present); a connector-only
    # delivery has no thread to surface. Reported as "ok" so the frontend toasts
    # (the thread IS ready regardless of a separate connector outcome).
    # SAME-CHAT deliveries skip this: deliver_or_queue broadcasts on REAL delivery
    # (a parked/queued injection must not toast before it actually lands).
    if app is not None and conversation_id and not item.delivery.same_chat:
        from akana_server.conversation_events import broadcast_turn_completed

        await broadcast_turn_completed(app, conversation_id, status="ok")
    return {
        "id": item.id,
        "status": status,
        "error": note if status != "ok" else None,
        "conversation_id": conversation_id,
        "text": body,
    }



async def run_due_schedules(
    settings: Any,
    *,
    registry: Any = None,
    conversations: Any = None,
    now=None,
    complete: CompleteFn | None = None,
    app: Any = None,
) -> int:
    """Fire every schedule that is due at ``now`` (sequentially). Returns the
    number fired. A per-item failure never aborts the sweep. ``app`` (when given)
    lets a fired schedule broadcast a live turn event so its thread appears in the
    UI + toasts without a page refresh."""
    ref = now or now_tr()
    store = get_schedule_store(getattr(settings, "data_dir"))
    # BUG 4b — self-cleaning history: prune spent (disabled, aged-out) one-shot rows
    # each sweep so a lifetime of reminders never accumulates unbounded tombstones.
    # Defensive: a prune failure must never stop the due sweep.
    try:
        store.prune_spent(now=ref)
    except Exception:  # noqa: BLE001 - housekeeping must not block firing
        log.warning("schedule_engine: prune_spent failed (continuing)", exc_info=True)
    due = store.due(ref)
    fired = 0
    for item in due:
        try:
            # ``now`` (NOT ``ref``) is threaded to mark_ran: in production it is
            # None, so mark_ran re-samples ``now_tr()`` at COMPLETION time. This is
            # load-bearing for interval schedules — anchoring the roll-forward on the
            # sweep-start ``ref`` means a turn that runs longer than its own interval
            # produces a next_run_at already in the past → the item is due again on
            # the very next poll → continuous back-to-back firing. A test injects
            # ``now`` and gets deterministic roll-forward from that instant.
            await _run_one(
                settings,
                store,
                item,
                registry=registry,
                conversations=conversations,
                now=now,
                complete=complete,
                app=app,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - defense-in-depth; _run_one already guards
            log.exception("schedule %s: unexpected error in run sweep", item.id)
        fired += 1
    if fired:
        log.info("schedule_engine: fired %d due schedule(s)", fired)
    return fired


async def run_schedule_now(
    settings: Any,
    schedule_id: str,
    *,
    registry: Any = None,
    conversations: Any = None,
    complete: CompleteFn | None = None,
    now=None,
    app: Any = None,
) -> dict[str, Any] | None:
    """Fire ONE schedule immediately regardless of its ``next_run_at`` — the
    manual 'run now' path (REST ``POST /schedule/{id}/run`` and the UI test
    button). Returns the outcome dict, or ``None`` if the id is unknown."""
    store = get_schedule_store(getattr(settings, "data_dir"))
    item = store.get(schedule_id)
    if item is None:
        return None
    return await _run_one(
        settings,
        store,
        item,
        registry=registry,
        conversations=conversations,
        # Same anchoring rule as the sweep: pass raw ``now`` so mark_ran re-samples
        # completion time in production (None) while tests stay deterministic.
        now=now,
        complete=complete,
        app=app,
        # BUG 8 — a manual run is OUT OF BAND: a recurring schedule keeps its real
        # next_run_at (the day's genuine slot is not swallowed by a test run). A
        # ``once`` still self-disables inside mark_ran (it ran → it is spent).
        advance=False,
    )


# --------------------------------------------------------------------------- #
# App lifespan wiring (mirrors session_closer_service)
# --------------------------------------------------------------------------- #


def start_schedule_engine(app: Any) -> None:
    """Start the always-on poll loop. The loop reads ``app.state`` FRESH each
    turn (live settings + connector registry + conversation service), so a
    settings change or a connector reload takes effect without a restart.

    NOTE: this is NOT wired into ``app.py`` by this module — the integration is
    done centrally. See the report for the exact lifespan line.
    """
    settings = app.state.settings

    def is_active(_captured: Any) -> bool:
        return schedule_engine_active(app.state.settings)

    def interval_seconds(_captured: Any) -> float:
        return poll_seconds(app.state.settings)

    async def run_once_live(_captured: Any) -> int:
        s = app.state.settings
        return await run_due_schedules(
            s,
            registry=getattr(app.state, "connector_registry", None),
            conversations=getattr(app.state, "conversation_service", None),
            app=app,
        )

    _start_task(
        app,
        _TASK_ATTR,
        _poll_loop(
            settings,
            log=log,
            name="schedule_engine",
            is_active=is_active,
            interval_seconds=interval_seconds,
            disabled_check_seconds=_DISABLED_CHECK_SECONDS,
            run_once=run_once_live,
        ),
    )


async def stop_schedule_engine(app: Any) -> None:
    await _stop_task(app, _TASK_ATTR)


__all__ = [
    "poll_seconds",
    "schedule_engine_active",
    "run_due_schedules",
    "run_schedule_now",
    "start_schedule_engine",
    "stop_schedule_engine",
]
