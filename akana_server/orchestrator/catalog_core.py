"""Generic model-catalog core: fingerprint-keyed TTL cache + single-flight fetch.

The four provider catalogs (claude/cursor/gemini/openai) were ~80% identical: a
``_CatalogCache`` dataclass, a fingerprint-keyed TTL cache with OK/ERR TTLs, a
single-flight ``inflight`` task, and a ``force_refresh`` path. They had drifted:
claude/cursor used single-flight inflight tasks (and only claude carried the
"a concurrent force_refresh cancelled the shared inflight task → followers must
retry" fix), while gemini/openai held the module lock ACROSS the network fetch,
serializing every concurrent catalog/status request behind one slow list call.

This module is the single home for that discipline. A :class:`CatalogCache`
instance owns its own lock + TTL cache + inflight task; each provider catalog
holds one instance and passes a thin ``fetch`` coroutine factory. All four now
share the single-flight discipline (lock released before awaiting the fetch) and
the cancelled-inflight follower-recovery fix.

The stored value is the provider's own structured dict (``{"ok": bool, ...}``);
this core only reads ``result.get("ok")`` to pick the TTL. Fetch exceptions other
than :class:`asyncio.CancelledError` are captured into ``{"ok": False, "error": …}``
so a probe never blows up.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

# TTLs shared by every provider catalog (they were declared identically in all four).
CATALOG_TTL_OK = 600.0  # 10 min — the model list rarely changes
CATALOG_TTL_ERR = 45.0  # short — fast recovery after an auth/network error

#: A fetch is a no-arg coroutine factory returning the provider's ``{"ok", ...}`` dict.
FetchFn = Callable[[], Awaitable[dict[str, Any]]]


def key_fingerprint(key: str | None) -> str:
    """Short, stable, non-reversible fingerprint of a secret (``""`` when absent)."""
    if not key:
        return ""
    return hashlib.sha256(key.encode()).hexdigest()[:16]


async def build_models_response(
    cache: "CatalogCache",
    *,
    fp: str,
    fetch: Callable[[bool], Awaitable[dict[str, Any]]],
    options_from: Callable[[list[Any]], list[dict[str, str]]],
    fallback: list[dict[str, str]],
    active: str,
    preconditions: list[tuple[bool, str]],
    unreachable_error: str,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Assemble the ``fetch_*_models`` UI response shared by the four provider catalogs.

    The four ``fetch_<provider>_models`` functions had a byte-identical response-assembly
    skeleton on top of :meth:`CatalogCache.get`: precondition gates (no SDK / no key/token)
    → static fallback; else fingerprint → ``was_cached`` snapshot → fetch → error dict →
    success dict with ``models = live if live else fallback`` and
    ``source = "live" if live else "static"``. Only the credential resolution, the fetch
    transport, ``options_from`` and the per-provider precondition MESSAGES genuinely differ,
    so those stay in the provider modules and are passed in here.

    Contract (unchanged from the previous copies):
      * ``preconditions`` is an ORDERED list of ``(ok, error_message)``; the FIRST failing
        gate returns ``{reachable:False, models:fallback, active, error, source:"static",
        cached:False}``. (Gemini's ``genai_installed`` gate goes first, then the key gate;
        openai/claude/cursor have only the key/token gate.)
      * ``fetch(force_refresh)`` returns the provider's ``{"ok": bool, ...}`` cache dict.
      * ``unreachable_error`` is the default ``error`` when the result is not ok and carries
        no ``error`` of its own.
    """
    for ok, message in preconditions:
        if not ok:
            return {
                "reachable": False,
                "models": fallback,
                "active": active,
                "error": message,
                "source": "static",
                "cached": False,
            }

    was_cached = not force_refresh and cache.is_fresh(fp)
    result = await fetch(force_refresh)
    if not result.get("ok"):
        return {
            "reachable": False,
            "models": fallback,
            "active": active,
            "error": str(result.get("error") or unreachable_error),
            "source": "static",
            "cached": was_cached,
        }

    api_models = result.get("models") if isinstance(result.get("models"), list) else []
    live = options_from(api_models)
    return {
        "reachable": True,
        "models": live if live else fallback,
        "active": active,
        "error": None,
        "source": "live" if live else "static",
        "cached": was_cached,
    }


@dataclass
class CatalogCache:
    """Per-provider single-flight TTL cache.

    Holds the last fetched result keyed by a token/key fingerprint, plus the
    single in-flight refresh task so concurrent callers coalesce onto one fetch.
    """

    key_fp: str = ""
    fetched_at: float = 0.0
    result: dict[str, Any] | None = None
    inflight: "asyncio.Task[dict[str, Any]] | None" = field(default=None, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def _ttl(self, result: dict[str, Any]) -> float:
        return CATALOG_TTL_OK if result.get("ok") else CATALOG_TTL_ERR

    def _fresh(self, fp: str, result: dict[str, Any], now: float) -> bool:
        return (
            fp != ""
            and self.key_fp == fp
            and self.result is result
            and (now - self.fetched_at) < self._ttl(result)
        )

    def is_fresh(self, fp: str, now: float | None = None) -> bool:
        """True when a same-fingerprint result is cached and still within its TTL."""
        if not fp or self.result is None:
            return False
        return self._fresh(fp, self.result, now if now is not None else time.monotonic())

    def invalidate(self) -> None:
        """Reset the cache and cancel any in-flight refresh (key change / forced refresh)."""
        if self.inflight is not None and not self.inflight.done():
            self.inflight.cancel()
        self.key_fp = ""
        self.fetched_at = 0.0
        self.result = None
        self.inflight = None

    async def get(
        self, fp: str, fetch: FetchFn, *, force_refresh: bool = False
    ) -> dict[str, Any]:
        """Single-flight + TTL cached fetch.

        ``fp`` is the current key fingerprint (callers guarantee it is non-empty).
        ``fetch`` is awaited at most once per refresh; the lock is released before
        awaiting so a slow network fetch does not serialize concurrent callers.
        """
        now = time.monotonic()
        if not force_refresh and self._is_fresh_now(fp, now):
            return self.result  # type: ignore[return-value]

        async with self._lock:
            now = time.monotonic()
            if not force_refresh and self._is_fresh_now(fp, now):
                return self.result  # type: ignore[return-value]

            inflight = self.inflight
            if inflight is not None and not force_refresh and self.key_fp == fp:
                task = inflight
            else:

                async def _refresh() -> dict[str, Any]:
                    try:
                        result = await fetch()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - probe → structured error
                        result = {"ok": False, "error": str(exc)}
                    self.key_fp = fp
                    self.result = result
                    self.fetched_at = time.monotonic()
                    return result

                if force_refresh and inflight is not None and not inflight.done():
                    inflight.cancel()
                task = asyncio.create_task(_refresh())
                self.inflight = task

        try:
            return await task
        except asyncio.CancelledError:
            # A concurrent force_refresh (above) may cancel the SAME inflight `task`
            # this caller is a follower on — that cancellation is not ours (we never
            # called `task.cancel()`), so propagating it would abort this request with
            # an unhandled CancelledError instead of a probe result. `task.cancelled()`
            # is only true once the cancellation has actually been delivered to `task`
            # itself, so it distinguishes "our own await was cancelled" (this
            # coroutine's enclosing task — task may still be running) from "the shared
            # task we awaited was cancelled out from under us" — only the latter is safe
            # to retry, since a force_refresh caller has already started a fresh task.
            if task.cancelled():
                return await self.get(fp, fetch, force_refresh=force_refresh)
            raise
        finally:
            async with self._lock:
                if self.inflight is task:
                    self.inflight = None

    def _is_fresh_now(self, fp: str, now: float) -> bool:
        return self.result is not None and self._fresh(fp, self.result, now)
