"""NetworkEngine — unified async wrapper (breaker + timeout + retry).

:func:`guard` is the single entry point used by orchestrator call sites:

    out = await guard(
        coro_factory,            # callable that produces a FRESH coroutine on each attempt
        provider="cursor",       # breaker isolation key
        cfg=load_network_config(settings),
        timeout=settings.bridge_timeout,
    )

Flow (per-provider breaker, OUTSIDE the retry loop):

1. ``breaker.before_call()`` — if open, raises :class:`BreakerOpenError`
   fast-fail without making a call.
2. ``retry_async`` — each attempt wraps ``coro_factory()`` with ``with_timeout``
   for a hard deadline; transient errors are retried with exponential backoff,
   permanent errors (auth) raise immediately.
3. The result is reported to the breaker: success → ``record_success``;
   **both permanent AND transient** final failures → ``record_failure``
   (auth failures are also counted so that a provider that returns 401 forever
   can still open the circuit).

There is no :func:`guard_stream` for stream-producing (``async def`` generator)
calls — in streaming, breaker/timeout are wrapped with ``guard`` up to the first
byte; thereafter the stream operates within its own read timeout (behaviour
preserved).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from akana_server.network.breaker import (
    BreakerOpenError,
    BreakerRegistry,
    BreakerState,
    CircuitBreaker,
)
from akana_server.network.config import NetworkConfig
from akana_server.network.retry import retry_async
from akana_server.network.timeout import with_timeout

__all__ = [
    "breaker_degraded",
    "guard",
    "global_registry",
    "reset_global_registry",
    "stream_breaker",
]

log = logging.getLogger(__name__)

T = TypeVar("T")

#: Process-global breaker registry — read by REST /network/status; shared by
#: call sites (cursor/claude/bridge_daemon each get separate breakers).
_GLOBAL_REGISTRY: BreakerRegistry | None = None


def global_registry() -> BreakerRegistry:
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        _GLOBAL_REGISTRY = BreakerRegistry()
    return _GLOBAL_REGISTRY


def reset_global_registry() -> None:
    """Reset the process-global breaker registry (test isolation)."""
    global _GLOBAL_REGISTRY
    _GLOBAL_REGISTRY = None


def breaker_degraded(provider: str = "cursor") -> bool:
    """Is the ``provider`` breaker NOT CLOSED (i.e. OPEN or HALF_OPEN)?

    Burst guard: when the breaker is degraded (open or in the single-probe
    window), background 2nd LLM calls (memory capture) MUST NOT be made —
    they would consume the probe or deepen a rate-limit. ``state()`` reads the
    state without raising; returns ``False`` on read error (no blocking).
    NOTE: ``.state`` is a METHOD — calling it without parentheses (compare with
    enum) was always False, a silent bug; called correctly here.

    Read-only lookup: this is called with providers that are never
    breaker-guarded (e.g. "gemini"/"openai"/"ollama", or "" when provider
    resolution fails — see chat_state.py), so it must NOT mint a phantom
    breaker as a side effect. ``BreakerRegistry.get`` creates-and-caches an
    entry for an unknown name (registry defaults); that would pollute
    GET /network/status with breakers that are never actually enforced.
    A missing/unknown provider is simply not degraded."""
    try:
        registry = global_registry()
        breaker = registry._breakers.get(provider)
        if breaker is None:
            return False
        return breaker.state() != BreakerState.CLOSED
    except Exception:  # pragma: no cover - breaker okunamazsa engelleme
        return False


def _resync_breaker_config(breaker: CircuitBreaker, cfg: NetworkConfig) -> None:
    """Re-bind ``threshold``/``cooldown`` onto an already-created breaker.

    ``get_or_create`` only applies these two runtime settings at CREATION
    time (breaker.py:202-224 explicitly preserves an existing breaker's
    config). Both specs are documented as restart-free (schema.py), so a
    later change made in the settings panel must take effect on the very
    next call — otherwise it is silently ignored for the rest of the
    process lifetime. There is no public mutator on ``CircuitBreaker`` for
    this (only the constructor and ``BreakerRegistry.configure``, which only
    affects breakers created afterwards), so the two attributes are
    resynced directly here; this does not touch the breaker's state
    machine (open/half-open/closed, failure count).
    """
    breaker._threshold = max(1, int(cfg.breaker_threshold))
    breaker._cooldown = max(0.0, float(cfg.breaker_cooldown))


async def guard(
    coro_factory: Callable[[], Awaitable[T]],
    *,
    provider: str,
    cfg: NetworkConfig,
    timeout: float | None = None,
    registry: BreakerRegistry | None = None,
) -> T:
    """Single-call guard: run with the process-global (or provided) registry."""
    reg = registry or global_registry()
    # Per-call config is bound to the breaker at CREATION time; the shared
    # registry defaults are not mutated (the old ``reg.configure`` would flip-flop
    # shared defaults on every call — concurrent calls with different configs
    # and the first call's config was never applied, because configure only
    # affected breakers opened AFTER the call).
    breaker = (
        reg.get_or_create(
            provider,
            threshold=cfg.breaker_threshold,
            cooldown=cfg.breaker_cooldown,
        )
        if cfg.breaker_enabled
        else None
    )
    if breaker is not None:
        _resync_breaker_config(breaker, cfg)
    return await _run(coro_factory, breaker=breaker, cfg=cfg, timeout=timeout)


class stream_breaker:  # noqa: N801 - context manager (readability of with-statement)
    """Circuit-breaker context manager for streaming calls (no retry).

    Stream-producing paths (cursor/claude ``stream_user_chat``) cannot be
    retried — deltas would be replayed. Instead:

    * ``__enter__`` → raises :class:`BreakerOpenError` if open (before spawning).
    * normal exit → ``record_success`` (stream completed without error).
    * exception exit → ``record_failure`` (except cancellation).

    Usage::

        with stream_breaker("cursor", cfg):
            proc = await create_subprocess(...)
            async for ev in read(proc): yield ev
    """

    def __init__(
        self,
        provider: str,
        cfg: NetworkConfig,
        *,
        registry: BreakerRegistry | None = None,
    ) -> None:
        reg = registry or global_registry()
        # Per-call config is bound to the breaker at creation time (shared
        # registry defaults are not mutated — see ``guard`` note).
        self._breaker = (
            reg.get_or_create(
                provider,
                threshold=cfg.breaker_threshold,
                cooldown=cfg.breaker_cooldown,
            )
            if cfg.breaker_enabled
            else None
        )
        if self._breaker is not None:
            _resync_breaker_config(self._breaker, cfg)

    def __enter__(self) -> "stream_breaker":
        if self._breaker is not None:
            self._breaker.before_call()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # type: ignore[no-untyped-def]
        if self._breaker is None:
            return False
        if exc_type is None:
            self._breaker.record_success()
        elif (
            issubclass(exc_type, asyncio.CancelledError)
            # BH: a consumer disconnect (GeneratorExit) is not a provider fault —
            # excluded alongside CancelledError so a disconnect burst never
            # trips/re-opens the breaker.
            or issubclass(exc_type, GeneratorExit)
            or issubclass(exc_type, BreakerOpenError)
        ):
            pass  # cancellation / disconnect / already-open does not dirty the breaker
        else:
            self._breaker.record_failure()
        return False  # do not swallow the exception


async def _run(
    coro_factory: Callable[[], Awaitable[T]],
    *,
    breaker: CircuitBreaker | None,
    cfg: NetworkConfig,
    timeout: float | None,
) -> T:
    if breaker is not None:
        breaker.before_call()  # raises BreakerOpenError if open (no retry)

    async def _attempt() -> T:
        return await with_timeout(coro_factory(), timeout)

    try:
        if cfg.retry_enabled:
            result = await retry_async(_attempt, cfg)
        else:
            result = await _attempt()
    except asyncio.CancelledError:
        raise  # cancellation does not dirty the breaker
    except BreakerOpenError:
        raise  # theoretical after before_call; don't count
    except BaseException:
        if breaker is not None:
            breaker.record_failure()
        raise

    if breaker is not None:
        breaker.record_success()
    return result
