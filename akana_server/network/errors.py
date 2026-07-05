"""NetworkEngine — error classification (transient vs permanent).

The heart of the retry decision: is a failure **transient** (network blip →
retry) or **permanent** (auth/invalid request → raise immediately)?
Classification looks at three signals in order; the first match wins:

1. Explicit marker types — :class:`TransientError` / :class:`PermanentError`
   (the caller signals intent directly).
2. HTTP-like status code — the ``status_code`` attribute of the exception object
   (e.g. ``LLMCallError``). 429 + 5xx → transient; 408/425 → transient; other
   4xx → permanent (especially 401/403 auth → NEVER retried).
3. Built-in network exceptions — ``TimeoutError``, ``ConnectionError`` and their
   subtypes are treated as transient.

Unknown/unclassifiable errors are treated as **permanent**: silently retrying
under uncertainty (e.g. mistaking a logic error for a network blip) can
multiply side-effectful calls — the safe default is a single attempt.
"""

from __future__ import annotations

import asyncio

__all__ = [
    "PermanentError",
    "TransientError",
    "classify_exception",
    "is_transient",
]

#: HTTP-like status codes treated as transient (will be retried).
_TRANSIENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 509})
#: Permanent auth codes — explicitly NEVER retried under any condition.
_AUTH_STATUS = frozenset({401, 403})


class TransientError(Exception):
    """Explicit signal from the caller: 'this is transient, retrying is safe'."""


class PermanentError(Exception):
    """Explicit signal from the caller: 'this is permanent, do not retry'."""


def _status_of(exc: BaseException) -> int | None:
    """Extract an HTTP-like ``status_code`` from an exception (None if absent)."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, bool):  # bool is a subtype of int — don't count it by accident
        return None
    if isinstance(status, int):
        return status
    return None


def is_transient(exc: BaseException) -> bool:
    """Is an exception transient (retriable)? Unknown → permanent (False)."""
    # 1) Explicit marker types win.
    if isinstance(exc, PermanentError):
        return False
    if isinstance(exc, TransientError):
        return True

    # 2) HTTP-like status code (e.g. LLMCallError.status_code).
    status = _status_of(exc)
    if status is not None:
        if status in _AUTH_STATUS:
            return False
        if status in _TRANSIENT_STATUS:
            return True
        if 400 <= status < 500:
            return False  # invalid request / not-found → permanent
        if status >= 500:
            return True
        # 1xx/2xx/3xx in an error body is meaningless → permanent (safe).
        return False

    # 3) Built-in network exceptions.
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, ConnectionError)):
        return True

    # Unknown → permanent (safe default: single attempt).
    return False


def classify_exception(exc: BaseException) -> str:
    """Label for observability: ``"transient"`` | ``"permanent"``."""
    return "transient" if is_transient(exc) else "permanent"
