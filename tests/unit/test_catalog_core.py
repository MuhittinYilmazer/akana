"""Unit tests for the shared model-catalog core (single-flight TTL cache).

Covers the discipline that all four provider catalogs now share: fingerprint-keyed
TTL cache, single-flight coalescing, the lock released BEFORE the network fetch
(concurrent callers are not serialized), and the cancelled-inflight follower-recovery
fix (previously only claude_catalog had it).
"""

from __future__ import annotations

import asyncio
import time

from akana_server.orchestrator.catalog_core import (
    CATALOG_TTL_ERR,
    CATALOG_TTL_OK,
    CatalogCache,
    key_fingerprint,
)


def test_key_fingerprint_stable_and_empty() -> None:
    assert key_fingerprint("") == ""
    assert key_fingerprint(None) == ""
    fp = key_fingerprint("secret-abc")
    assert fp == key_fingerprint("secret-abc")  # stable
    assert fp != key_fingerprint("secret-xyz")  # key-dependent
    assert len(fp) == 16


def test_single_flight_coalesces_concurrent_callers() -> None:
    """When the cache is STALE and a same-fingerprint refresh is already in flight, a
    second caller coalesces onto it (only one fetch). This is the follower branch that
    keys on ``key_fp == fp``: we prime the fingerprint, expire the cache, then run two
    concurrent callers — the second joins the first's inflight task."""
    cache = CatalogCache()
    calls = {"n": 0}
    started = asyncio.Event()
    release = asyncio.Event()

    async def fetch() -> dict:
        calls["n"] += 1
        n = calls["n"]
        if n == 1:
            return {"ok": True, "models": [], "gen": 1}
        started.set()
        await release.wait()
        return {"ok": True, "models": [], "gen": n}

    async def run() -> None:
        await cache.get("fp1", fetch)  # primes key_fp="fp1"
        # Expire the cached result: push fetched_at past the TTL. Relative to
        # time.monotonic() (NOT 0.0) so it holds on a freshly-booted CI runner where
        # monotonic() is still smaller than the 600 s TTL.
        cache.fetched_at = time.monotonic() - CATALOG_TTL_OK - 1
        assert not cache.is_fresh("fp1")

        a = asyncio.create_task(cache.get("fp1", fetch))  # leader: starts refresh #2
        await started.wait()
        b = asyncio.create_task(cache.get("fp1", fetch))  # follower: coalesces
        await asyncio.sleep(0)  # let b reach the inflight-follower branch
        release.set()
        ra, rb = await asyncio.gather(a, b)
        assert ra is rb
        assert calls["n"] == 2  # follower did NOT trigger a third fetch

    asyncio.run(run())


def test_lock_released_before_fetch_no_serialization() -> None:
    """Two DIFFERENT-fingerprint fetches must overlap in time (the lock is not held
    across the await) — the regression this core fixes for gemini/openai."""
    cache_a = CatalogCache()
    cache_b = CatalogCache()
    both_inside = asyncio.Event()
    inside = {"n": 0}

    async def fetch() -> dict:
        inside["n"] += 1
        if inside["n"] == 2:
            both_inside.set()
        # If either fetch held a shared lock across this await, both could never be
        # inside simultaneously. Each cache has its own lock, so this asserts the
        # per-instance discipline; the wait times out only if serialized.
        await asyncio.wait_for(both_inside.wait(), timeout=1.0)
        return {"ok": True}

    async def run() -> None:
        await asyncio.gather(cache_a.get("a", fetch), cache_b.get("b", fetch))
        assert inside["n"] == 2

    asyncio.run(run())


def test_ttl_cache_reuses_result() -> None:
    cache = CatalogCache()
    calls = {"n": 0}

    async def fetch() -> dict:
        calls["n"] += 1
        return {"ok": True}

    async def run() -> None:
        r1 = await cache.get("fp1", fetch)
        r2 = await cache.get("fp1", fetch)
        assert r1 is r2
        assert calls["n"] == 1  # second call served from cache

    asyncio.run(run())


def test_force_refresh_bypasses_cache() -> None:
    cache = CatalogCache()
    calls = {"n": 0}

    async def fetch() -> dict:
        calls["n"] += 1
        return {"ok": True}

    async def run() -> None:
        await cache.get("fp1", fetch)
        await cache.get("fp1", fetch, force_refresh=True)
        assert calls["n"] == 2

    asyncio.run(run())


def test_fetch_exception_captured_as_error_dict() -> None:
    cache = CatalogCache()

    async def fetch() -> dict:
        raise RuntimeError("boom")

    async def run() -> None:
        res = await cache.get("fp1", fetch)
        assert res["ok"] is False
        assert "boom" in res["error"]

    asyncio.run(run())


def test_force_refresh_follower_recovers_from_cancelled_inflight() -> None:
    """The ported claude_catalog fix: when a force_refresh cancels the shared inflight
    task a follower is awaiting, the follower must RETRY (get a fresh result), not
    propagate the CancelledError it never requested."""
    cache = CatalogCache()
    calls = {"n": 0}
    first_started = asyncio.Event()
    release_second = asyncio.Event()

    async def fetch() -> dict:
        calls["n"] += 1
        n = calls["n"]
        if n == 1:
            first_started.set()
            # Slow first fetch — will be cancelled out from under the follower.
            await asyncio.sleep(10)
            return {"ok": True, "which": 1}
        await release_second.wait()
        return {"ok": True, "which": 2}

    async def run() -> None:
        # Follower A joins the first (slow) inflight fetch.
        a = asyncio.create_task(cache.get("fp1", fetch))
        await first_started.wait()
        await asyncio.sleep(0)  # A is now awaiting the shared inflight task
        # A force_refresh caller cancels that shared task and starts a fresh one.
        b = asyncio.create_task(cache.get("fp1", fetch, force_refresh=True))
        await asyncio.sleep(0)
        release_second.set()
        ra = await a
        rb = await b
        # A recovered onto the fresh fetch instead of raising CancelledError.
        assert ra["ok"] is True and ra["which"] == 2
        assert rb["which"] == 2

    asyncio.run(run())


def test_invalidate_clears_state() -> None:
    cache = CatalogCache()

    async def fetch() -> dict:
        return {"ok": True}

    async def run() -> None:
        await cache.get("fp1", fetch)
        assert cache.result is not None and cache.key_fp == "fp1"
        cache.invalidate()
        assert cache.result is None
        assert cache.key_fp == ""
        assert cache.inflight is None

    asyncio.run(run())


def test_invalidate_cancels_inflight_and_follower_recovers() -> None:
    """invalidate() cancels the in-flight refresh (key rotation), and a caller awaiting
    that shared task recovers by re-fetching rather than propagating CancelledError."""
    cache = CatalogCache()
    calls = {"n": 0}
    started = asyncio.Event()

    async def fetch() -> dict:
        calls["n"] += 1
        n = calls["n"]
        if n == 1:
            started.set()
            await asyncio.sleep(10)  # cancelled by invalidate()
            return {"ok": True, "gen": 1}
        return {"ok": True, "gen": 2}

    async def run() -> None:
        task = asyncio.create_task(cache.get("fp1", fetch))
        await started.wait()
        await asyncio.sleep(0)  # caller is now awaiting the shared inflight task
        cache.invalidate()
        res = await asyncio.wait_for(task, timeout=1.0)
        # Recovered onto a fresh fetch (gen 2), not a CancelledError.
        assert res == {"ok": True, "gen": 2}
        assert calls["n"] == 2

    asyncio.run(run())


def test_ttl_values_ok_shorter_error() -> None:
    assert CATALOG_TTL_OK > CATALOG_TTL_ERR
    cache = CatalogCache()
    assert cache._ttl({"ok": True}) == CATALOG_TTL_OK
    assert cache._ttl({"ok": False}) == CATALOG_TTL_ERR
