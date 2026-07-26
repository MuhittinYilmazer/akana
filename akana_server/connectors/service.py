"""ConnectorEngine — lifespan wiring (following session_closer_service style).

``start_connectors(app)`` builds the registry from config, starts active
channels, and binds the inbound router. If no channel is active (default) it is
a silent no-op, but an empty registry is still placed on ``app.state`` so that
``GET /api/v1/connectors`` returns a consistent response.
``stop_connectors(app)`` is idempotent.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from akana_server.connectors.registry import ConnectorRegistry, build_registry
from akana_server.connectors.router import InboundRouter

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = ["reload_connectors", "start_connectors", "stop_connectors"]

log = logging.getLogger(__name__)

_REGISTRY_ATTR = "connector_registry"
_ROUTER_ATTR = "connector_router"

#: Serialises reload_connectors so two overlapping PUT /connectors/telegram
#: requests cannot both pass the stop phase and then both run start_connectors,
#: which would build two registries+routers, start two Telegram getUpdates
#: pollers on the same token (409 flapping), and orphan the first router/poller
#: (setattr on app.state is overwritten) so it can never be stopped.
_reload_lock = asyncio.Lock()

#: When a web/voice turn is running in the same conversation, the connector turn
#: waits at most this many seconds for it to finish. If the timeout is exceeded
#: (web turn is stuck), the connector turn is processed anyway
#: (liveness > perfect exclusion — worst case is the old daemon-serialisation behaviour).
_GUARD_MAX_WAIT_S = 90.0
_GUARD_POLL_S = 0.1

#: Max time a live reload waits for already-accepted connector messages (queued +
#: in-flight LLM turn) to finish before it force-stops the router. An LLM turn can take
#: 30-60s; the drain window covers a normal turn but is bounded so a stuck turn cannot
#: block the dashboard Save indefinitely.
_RELOAD_DRAIN_TIMEOUT_S = 60.0


def _make_turn_guard(app: FastAPI):
    """Per-conversation turn gate: MUTUAL EXCLUSION between the connector turn
    and the web/voice busy-registry. The web/voice path returns ``409``
    (the interactive client retries); there is no retrying client on the connector
    side → if busy, WAIT (serialise), then register atomically once free. This
    prevents a connector turn and a web turn from reaching the LLM concurrently
    in the same conversation (daemon ``session_key=conv_id`` serialisation →
    spurious ``LLM_TIMEOUT`` + history-read race closed; R4-E #1)."""
    # Lazy import (service → api is a downward reach the connector engine must not
    # bind at module level): use the PUBLIC turn-gate seam, not the underscore-private
    # route internals. ``register_turn`` raises the same 409 TURN_BUSY as the web/voice
    # path; ``swap_turn_handle`` re-points the claim at the per-turn child task.
    from fastapi import HTTPException

    from akana_server.api.routes.chat.turn_gate import (
        register_turn as _gate_register_turn,
        release_turn as _gate_release_turn,
        swap_turn_handle as _gate_swap_handle,
    )

    def _is_busy_conflict(exc: HTTPException) -> bool:
        """True only for the specific 409 TURN_BUSY signal (a turn is already running).

        Any OTHER HTTPException — and every non-HTTPException — is a real failure that
        must propagate, NOT be silently reinterpreted as 'busy' and stalled for 90s."""
        if exc.status_code != 409:
            return False
        detail = exc.detail
        code = detail.get("error", {}).get("code") if isinstance(detail, dict) else None
        return code == "TURN_BUSY"

    @asynccontextmanager
    async def guard(conversation_id: str | None):
        """Yields the register-child callback on a real claim, ``None`` when nothing
        was claimed.

        The yielded value is the body's ONLY way to know whether the gate actually
        claimed AND ANNOUNCED this turn. ``register_turn`` only emits ``turn_active``
        on a successful claim, so the degraded "processing anyway" branch below
        announces nothing — and a body that cannot tell the two apart broadcast an
        UNPAIRED ``turn_completed`` into a conversation whose OTHER turn was still live.
        """
        conv_id = (conversation_id or "").strip()
        if not conv_id:
            yield None
            return
        handle = None
        waited = 0.0
        while True:
            try:
                handle = _gate_register_turn(app, conv_id)
                break
            except HTTPException as exc:
                # ONLY 409 TURN_BUSY means 'a web/voice/other connector turn is running' →
                # wait. Any other HTTPException (or any non-HTTP error, which is NOT caught
                # here and so propagates to capture_failure) is a real bug, not busy — a
                # bare 'except Exception == busy' would turn it into a silent 90s stall.
                if not _is_busy_conflict(exc):
                    raise
                if waited >= _GUARD_MAX_WAIT_S:
                    log.warning(
                        "connector turn-gate: conv=%s did not free up in %.0fs; processing anyway",
                        conv_id,
                        waited,
                    )
                    handle = None  # could not register → holding no lock; worst case is old behaviour
                    break
                await asyncio.sleep(_GUARD_POLL_S)
                waited += _GUARD_POLL_S
        # ``register_turn`` (the turn-gate seam) records the CURRENT task as the cancel handle —
        # but the connector turn runs inside the long-lived per-conversation WORKER task, so an
        # external STOP/reset would cancel the whole worker (zombie chat: every future message
        # silently dropped).
        #
        # The registered handle is instead this HOLDER task, for two reasons:
        #   * it is what a STOP cancels, and its cancellation is forwarded to the per-TURN
        #     child only — the worker survives and keeps serving the next queued message;
        #   * every busy predicate is ``not handle.done()``, so registering the child task
        #     itself freed the conversation the instant the LLM returned, while the egress
        #     filter and BOTH turn persists still had to run inside the claim. The holder
        #     is done only when this context manager exits, so "claimed" and "the critical
        #     section is running" are the same interval — which is the whole point of
        #     doing the filter+persist in here.
        child: dict[str, Any] = {"task": None}

        async def _hold() -> None:
            try:
                await asyncio.Event().wait()  # released by the cancel in the finally
            except asyncio.CancelledError:
                task = child["task"]
                if task is not None and not task.done():
                    task.cancel()
                raise

        def register_turn(task: Any) -> None:
            child["task"] = task

        holder: "asyncio.Task[None] | None" = None
        if handle is not None:
            holder = asyncio.create_task(_hold(), name=f"connector-turn-claim:{conv_id}")
            _gate_swap_handle(app, conv_id, holder)

        cancelled = False
        try:
            # DEGRADED (``handle is None``): nothing claimed → the gate announced nothing
            # and the body owes nothing. Liveness > perfect exclusion; the message is
            # still processed, it just runs without the claim.
            yield register_turn if holder is not None else None
        except asyncio.CancelledError:
            cancelled = True  # STOP → preserve the queue (b8 contract), do not drain
            raise
        finally:
            if holder is not None:
                holder.cancel()  # ends the claim; a live child is cancelled with it
                # ``announce=False``: the gate announced this turn's START, but its ONE
                # completion belongs to ``InboundRouter.handle`` — only that knows the real
                # outcome and the persisted assistant turn id, and both exist only AFTER the
                # conversation has had to be freed here. Announcing on both sides is how the
                # channel turn ended up emitting one turn_active and TWO turn_completed, the
                # first of them before the reply was even written.
                _gate_release_turn(app, conv_id, holder, announce=False)
            # b1: the connector shares the busy-registry (so concurrent web sends queue with
            # 202) but never drained that queue → a web message queued behind a Telegram turn
            # was stranded. Mirror the web guards: parked INJECTIONS first, then the queue —
            # a Telegram-bound chat may have no later web/voice turn at all, so draining only
            # the queue stranded a promised background result (and the turn_completed the
            # engine deferred to its delivery) until the next restart.
            # NOTE: the drain helpers are chat-package internals with no public seam yet;
            # they stay behind a lazy import from the ``streaming`` facade until the chat
            # package exposes a public drain entry point (the turn-gate seam covers only the
            # busy-registry, not the injection inbox / follower queue this drains).
            if conv_id and not cancelled:
                from akana_server.api.routes.chat.chat_detached import (
                    _drain_injections_then_queue,
                )
                from akana_server.api.routes.chat.streaming import _spawn_background

                _spawn_background(app, _drain_injections_then_queue(app, conv_id))

    return guard


async def start_connectors(app: FastAPI) -> None:
    settings = app.state.settings
    registry = build_registry(settings)
    setattr(app.state, _REGISTRY_ATTR, registry)
    setattr(app.state, _ROUTER_ATTR, None)
    if not registry.connector_ids:
        return  # default: all channels disabled
    # F2: same persistence layer as chat.py — each chat_id is bound to a persistent conversation.
    router = InboundRouter(
        settings,
        registry,
        conversations=getattr(app.state, "conversation_service", None),
        turn_guard=_make_turn_guard(app),
        # Carrier of the /ws/events hub: a channel turn lands in the same conversation
        # store the web UI renders, so it must broadcast turn_completed or a bound
        # conversation open in the browser stays stale until F5.
        app=app,
    )
    setattr(app.state, _ROUTER_ATTR, router)
    await registry.start_all()
    router.start()
    log.info("connectors started: %s", ", ".join(registry.connector_ids))


async def stop_connectors(app: FastAPI) -> None:
    router: InboundRouter | None = getattr(app.state, _ROUTER_ATTR, None)
    if router is not None:
        await router.stop()
    registry: ConnectorRegistry | None = getattr(app.state, _REGISTRY_ATTR, None)
    if registry is not None:
        await registry.stop_all()


async def reload_connectors(app: FastAPI) -> None:
    """Tear the registry down and rebuild it from the CURRENT ``app.state.settings``.

    This is the live enable/disable seam: a connector setting changes (Telegram
    on/off, bot token, allowlist) → the dashboard PUTs it, refreshes the live
    settings snapshot, then calls this to bring the channel up/down WITHOUT a
    process restart. ``build_registry`` only registers an enabled channel, so a
    disabled channel simply stops; an enabled one (re)starts with the new config.
    Idempotent — safe to call when nothing is running.
    """
    # Serialise concurrent reloads (e.g. a double-clicked Save = two overlapping
    # PUTs): without this both could pass stop then both run start, orphaning a
    # live Telegram poller that no later stop/reload can ever reach.
    async with _reload_lock:
        # Graceful drain BEFORE teardown: a hard stop_connectors cancels the intake
        # task and every conversation worker, dropping messages sitting in the shared
        # inbound queue and per-worker queues — messages already offset-confirmed to
        # Telegram (never redelivered) — and aborting any in-flight LLM turn with no
        # reply. Draining lets those already-accepted messages finish first, then the
        # normal stop/start swaps the registry. If the drain times out (a genuinely
        # stuck turn) we fall through to the hard stop rather than blocking the reload.
        # Stop the inbound PRODUCERS (pollers) BEFORE draining. A running Telegram
        # poller keeps calling getUpdates during the drain window, and each getUpdates
        # advances/confirms the offset (Telegram never redelivers). Any message it
        # enqueues AFTER drain's one-time sweep is neither processed nor redelivered —
        # it is silently lost when start_connectors swaps in a fresh queue. Halting the
        # pollers first freezes the offset so the drain sees a stable, complete queue.
        # (connector.stop is idempotent, so the stop_connectors call below is a no-op
        # for the already-stopped pollers.)
        registry = getattr(app.state, _REGISTRY_ATTR, None)
        if registry is not None:
            try:
                await registry.stop_all()
            except Exception as e:  # a producer-stop failure must not block the reload
                log.warning("connector reload: stopping pollers failed: %s", e)
        router = getattr(app.state, _ROUTER_ATTR, None)
        if router is not None:
            try:
                await router.drain(timeout=_RELOAD_DRAIN_TIMEOUT_S)
            except Exception as e:  # a drain failure must not block the reload
                log.warning("connector reload: drain failed, forcing stop: %s", e)
        await stop_connectors(app)
        await start_connectors(app)
