"""NetworkEngine — exponential backoff + jitter retry (transient errors only).

:func:`retry_async` takes a ``coro_factory`` (a callable that produces a FRESH
coroutine on each attempt); on failure it classifies the error with
:func:`is_transient`:

* **permanent** (auth 401/403, invalid request, unknown) → re-raised IMMEDIATELY
  (single attempt).
* **transient** (timeout, connection, 5xx, 429) → waits ``base_delay * 2**(n-1)``
  (with jitter, capped at ``max_delay``) and tries again; the LAST error is raised
  once ``max_retries`` attempts or the ``total_timeout`` budget is exceeded.

Clock + sleep + jitter generator are injectable (tests do not wait on real time;
deterministic jitter). The optional ``on_retry`` callback is called before each
retry with ``(attempt, exc, delay)`` (observability).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar

from akana_server.network.config import NetworkConfig
from akana_server.network.errors import is_transient

__all__ = ["retry_async"]

T = TypeVar("T")


def _backoff_delay(
    attempt: int, cfg: NetworkConfig, rand: Callable[[], float]
) -> float:
    """Jittered exponential delay for attempt number ``attempt`` (1-indexed)."""
    raw = cfg.base_delay * (2 ** (attempt - 1))
    capped = min(raw, cfg.max_delay)
    if cfg.jitter > 0:
        # Symmetric ±jitter: rand()∈[0,1) is scaled to [-1,1).
        offset = (rand() * 2.0 - 1.0) * cfg.jitter * capped
        capped = max(0.0, capped + offset)
    return capped


async def retry_async(
    coro_factory: Callable[[], Awaitable[T]],
    cfg: NetworkConfig,
    *,
    now: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    rand: Callable[[], float] = random.random,
    on_retry: Callable[[int, BaseException, float], None] | None = None,
) -> T:
    """Retry ``coro_factory()`` with exponential backoff on transient errors.

    Raises the last exception once a permanent error occurs or the attempt/time budget is exhausted.
    """
    max_attempts = max(1, cfg.max_retries)
    started = now()
    last_exc: BaseException | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_factory()
        except BaseException as exc:  # noqa: BLE001 - classify and re-raise
            last_exc = exc
            # asyncio cancellation is never retried (honour the cancellation contract).
            if isinstance(exc, asyncio.CancelledError):
                raise
            if not is_transient(exc):
                raise
            if attempt >= max_attempts:
                raise
            delay = _backoff_delay(attempt, cfg, rand)
            # Total time budget: if sleeping now would exceed the budget, raise immediately.
            if cfg.total_timeout > 0:
                elapsed = now() - started
                if elapsed + delay >= cfg.total_timeout:
                    raise
            if on_retry is not None:
                on_retry(attempt, exc, delay)
            await sleep(delay)

    # Theoretically unreachable (the loop always returns or raises) — for type safety.
    assert last_exc is not None  # noqa: S101
    raise last_exc
