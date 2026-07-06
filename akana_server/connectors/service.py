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
    # path; ``busy_registry`` is the raw conv→task map (for the per-turn handle swap).
    from fastapi import HTTPException

    from akana_server.api.routes.chat.turn_gate import (
        busy_registry,
        register_turn as _gate_register_turn,
        release_turn as _gate_release_turn,
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
        # silently dropped). The ``register_turn`` callback below lets the body swap the handle to
        # a per-TURN child task, so STOP cancels only the turn and the worker survives.
        current: dict[str, Any] = {"handle": handle}

        def register_turn(task: Any) -> None:
            if handle is None:
                return  # degraded (registration failed) → nothing claimed; no-op
            busy_registry(app)[conv_id] = task
            current["handle"] = task

        cancelled = False
        try:
            yield register_turn
        except asyncio.CancelledError:
            cancelled = True  # STOP → preserve the queue (b8 contract), do not drain
            raise
        finally:
            _gate_release_turn(app, conv_id, current["handle"])
            # b1: the connector shares the busy-registry (so concurrent web sends queue with
            # 202) but never drained that queue → a web message queued behind a Telegram turn
            # was stranded. Mirror the web guards: drain on normal completion (not on STOP).
            # NOTE: the queue-drain helpers are chat-package internals with no public seam yet;
            # they stay behind a lazy import from the ``streaming`` facade until the chat
            # package exposes a public drain entry point (the turn-gate seam covers only the
            # busy-registry, not the follower/resume queue this drains).
            if conv_id and not cancelled:
                from akana_server.api.routes.chat.streaming import (
                    _maybe_drain_queue,
                    _spawn_background,
                )

                _spawn_background(app, _maybe_drain_queue(app, conv_id))

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
        router = getattr(app.state, _ROUTER_ATTR, None)
        if router is not None:
            try:
                await router.drain(timeout=_RELOAD_DRAIN_TIMEOUT_S)
            except Exception as e:  # a drain failure must not block the reload
                log.warning("connector reload: drain failed, forcing stop: %s", e)
        await stop_connectors(app)
        await start_connectors(app)
