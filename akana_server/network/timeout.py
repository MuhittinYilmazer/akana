"""NetworkEngine — hard per-call time limit (``asyncio.wait_for`` wrapper).

Cancels a coroutine if it does not complete within the given number of seconds
and raises :class:`NetworkTimeoutError` (a ``TransientError`` subtype →
retriable). ``timeout <= 0`` or ``None`` → no limit (direct ``await``).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar

from akana_server.network.errors import TransientError

__all__ = ["NetworkTimeoutError", "with_timeout"]

T = TypeVar("T")


class NetworkTimeoutError(TransientError):
    """Call exceeded the hard time limit (transient — retriable)."""


async def with_timeout(awaitable: Awaitable[T], timeout: float | None) -> T:
    """Await ``awaitable`` within ``timeout`` seconds; cancel + raise on expiry.

    If ``timeout`` is None or <= 0 no limit is applied. On expiry the
    underlying task is cancelled by ``asyncio.wait_for``; we convert that to
    a :class:`NetworkTimeoutError`.
    """
    if timeout is None or timeout <= 0:
        return await awaitable
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError) as e:
        raise NetworkTimeoutError(
            f"network call did not complete within {timeout:g} s"
        ) from e
