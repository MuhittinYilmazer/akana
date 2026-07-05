"""NetworkEngine boundary-value tests — retry/breaker/errors/timeout.

Behavior-pinning: these tests lock the CURRENT (deemed-correct) behavior;
a future regression breaks them. Clock/sleep/jitter are injected →
no real time is awaited, it is deterministic.
"""

from __future__ import annotations

import asyncio

import pytest

from akana_server.network.breaker import (
    BreakerOpenError,
    BreakerRegistry,
    BreakerState,
    CircuitBreaker,
)
from akana_server.network.config import NetworkConfig
from akana_server.network.errors import (
    PermanentError,
    TransientError,
    classify_exception,
    is_transient,
)
from akana_server.network.retry import _backoff_delay, retry_async
from akana_server.network.timeout import NetworkTimeoutError, with_timeout


# --------------------------------------------------------------------------- #
# errors — permanent vs transient classification edges
# --------------------------------------------------------------------------- #


class _StatusErr(Exception):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"status {status_code}")


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (408, True),   # request timeout → transient
        (425, True),   # too early → transient
        (429, True),   # rate limit → transient
        (500, True),
        (503, True),
        (509, True),
        (400, False),  # bad request → permanent
        (404, False),
        (401, False),  # auth → NEVER retry
        (403, False),
        (418, False),  # other 4xx → permanent
        (200, False),  # 2xx meaningless in an error body → permanent
        (302, False),  # 3xx → permanent
        (100, False),  # 1xx → permanent
        (600, True),   # >=500 everything transient
    ],
)
def test_is_transient_status_edges(status: int, expected: bool) -> None:
    assert is_transient(_StatusErr(status)) is expected


def test_bool_status_not_treated_as_int() -> None:
    """``status_code=True`` (bool) must not be accidentally counted as 1 → unknown → permanent."""
    err = Exception()
    err.status_code = True  # type: ignore[attr-defined]
    assert is_transient(err) is False


def test_explicit_markers_win_over_status() -> None:
    """An explicit marker type takes precedence over the status code."""

    class TransientWith4xx(TransientError):
        status_code = 404

    class PermanentWith5xx(PermanentError):
        status_code = 503

    assert is_transient(TransientWith4xx()) is True
    assert is_transient(PermanentWith5xx()) is False


def test_builtin_network_exceptions_transient() -> None:
    assert is_transient(TimeoutError("x")) is True
    assert is_transient(ConnectionError("x")) is True
    assert is_transient(asyncio.TimeoutError()) is True


def test_unknown_exception_permanent() -> None:
    assert is_transient(ValueError("boom")) is False
    assert classify_exception(ValueError("boom")) == "permanent"
    assert classify_exception(TimeoutError()) == "transient"


# --------------------------------------------------------------------------- #
# retry — budget, attempt limit, jitter bounds
# --------------------------------------------------------------------------- #


def _run(coro):
    return asyncio.run(coro)


def test_retry_max_retries_one_means_single_attempt() -> None:
    cfg = NetworkConfig(max_retries=1)
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise TimeoutError("x")

    with pytest.raises(TimeoutError):
        _run(retry_async(boom, cfg, sleep=lambda d: asyncio.sleep(0)))
    assert calls["n"] == 1  # no retry


def test_retry_zero_max_retries_clamped_to_one() -> None:
    cfg = NetworkConfig(max_retries=0)
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise TimeoutError("x")

    with pytest.raises(TimeoutError):
        _run(retry_async(boom, cfg, sleep=lambda d: asyncio.sleep(0)))
    assert calls["n"] == 1


def test_retry_permanent_raises_immediately() -> None:
    cfg = NetworkConfig(max_retries=5)
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise ValueError("kalıcı")

    with pytest.raises(ValueError):
        _run(retry_async(boom, cfg, sleep=lambda d: asyncio.sleep(0)))
    assert calls["n"] == 1  # permanent → single attempt


def test_retry_cancelled_never_retried() -> None:
    cfg = NetworkConfig(max_retries=5)
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        _run(retry_async(boom, cfg, sleep=lambda d: asyncio.sleep(0)))
    assert calls["n"] == 1


def test_retry_total_timeout_boundary_blocks_sleep() -> None:
    """elapsed + delay >= total_timeout → final error; that sleep is never performed."""
    cfg = NetworkConfig(
        max_retries=5, base_delay=10.0, max_delay=8.0, total_timeout=5.0, jitter=0.0
    )
    slept: list[float] = []

    async def boom():
        raise TimeoutError("x")

    async def fake_sleep(d):
        slept.append(d)

    with pytest.raises(TimeoutError):
        _run(retry_async(boom, cfg, now=lambda: 0.0, sleep=fake_sleep, rand=lambda: 0.5))
    assert slept == []  # even the first retry exceeds the budget


def test_retry_total_timeout_zero_means_unlimited_budget() -> None:
    cfg = NetworkConfig(max_retries=3, base_delay=1.0, total_timeout=0.0, jitter=0.0)
    slept: list[float] = []

    async def boom():
        raise TimeoutError("x")

    async def fake_sleep(d):
        slept.append(d)

    with pytest.raises(TimeoutError):
        _run(retry_async(boom, cfg, now=lambda: 0.0, sleep=fake_sleep, rand=lambda: 0.5))
    assert len(slept) == 2  # 3 attempts → 2 sleeps


def test_retry_succeeds_after_transient() -> None:
    cfg = NetworkConfig(max_retries=3, jitter=0.0)
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise TimeoutError("x")
        return "ok"

    out = _run(retry_async(flaky, cfg, sleep=lambda d: asyncio.sleep(0)))
    assert out == "ok"
    assert calls["n"] == 2


def test_on_retry_callback_invoked() -> None:
    cfg = NetworkConfig(max_retries=3, base_delay=1.0, jitter=0.0)
    seen: list[tuple[int, float]] = []

    async def boom():
        raise TimeoutError("x")

    with pytest.raises(TimeoutError):
        _run(
            retry_async(
                boom,
                cfg,
                now=lambda: 0.0,
                sleep=lambda d: asyncio.sleep(0),
                rand=lambda: 0.5,
                on_retry=lambda a, e, d: seen.append((a, d)),
            )
        )
    assert [a for a, _ in seen] == [1, 2]


@pytest.mark.parametrize("rand_val", [0.0, 0.5, 0.9999])
def test_backoff_jitter_within_bounds(rand_val: float) -> None:
    cfg = NetworkConfig(base_delay=1.0, max_delay=8.0, jitter=0.25)
    d = _backoff_delay(2, cfg, lambda: rand_val)  # capped raw = 2.0
    # ±25% → [1.5, 2.5]
    assert 1.5 - 1e-9 <= d <= 2.5 + 1e-9
    assert d >= 0.0


def test_backoff_never_negative_with_max_jitter() -> None:
    cfg = NetworkConfig(base_delay=1.0, max_delay=8.0, jitter=1.0)
    assert _backoff_delay(1, cfg, lambda: 0.0) >= 0.0  # offset -capped → clamp 0


def test_backoff_caps_at_max_delay() -> None:
    cfg = NetworkConfig(base_delay=1.0, max_delay=4.0, jitter=0.0)
    assert _backoff_delay(10, cfg, lambda: 0.5) == 4.0  # 2^9 capped


# --------------------------------------------------------------------------- #
# breaker — half-open race, registry isolation, threshold
# --------------------------------------------------------------------------- #


def test_breaker_trips_at_threshold_exact() -> None:
    clk = [0.0]
    br = CircuitBreaker("x", threshold=3, cooldown=10, now=lambda: clk[0])
    br.record_failure()
    br.record_failure()
    assert br.state() == BreakerState.CLOSED  # 2 < 3
    br.record_failure()
    assert br.state() == BreakerState.OPEN  # 3 == threshold


def test_breaker_threshold_clamped_min_one() -> None:
    br = CircuitBreaker("x", threshold=0)
    br.record_failure()
    assert br.state() == BreakerState.OPEN  # threshold at least 1


def test_breaker_open_before_call_raises_transient() -> None:
    clk = [0.0]
    br = CircuitBreaker("prov", threshold=1, cooldown=30, now=lambda: clk[0])
    br.record_failure()
    with pytest.raises(BreakerOpenError) as ei:
        br.before_call()
    assert isinstance(ei.value, TransientError)
    assert ei.value.name == "prov"
    assert ei.value.retry_after == pytest.approx(30.0)


def test_breaker_cooldown_matures_to_half_open() -> None:
    clk = [0.0]
    br = CircuitBreaker("x", threshold=1, cooldown=10, now=lambda: clk[0])
    br.record_failure()
    assert br.state() == BreakerState.OPEN
    clk[0] = 9.999
    assert br.state() == BreakerState.OPEN  # cooldown not yet elapsed
    clk[0] = 10.0
    assert br.state() == BreakerState.HALF_OPEN  # exact boundary >= cooldown


def test_breaker_half_open_failure_reopens_and_resets_cooldown() -> None:
    clk = [0.0]
    br = CircuitBreaker("x", threshold=3, cooldown=10, now=lambda: clk[0])
    for _ in range(3):
        br.record_failure()
    clk[0] = 10.0
    assert br.state() == BreakerState.HALF_OPEN
    br.before_call()  # half-open: a single attempt passes
    br.record_failure()  # failure in half-open → immediately open
    assert br.state() == BreakerState.OPEN
    snap = br.snapshot()
    assert snap["state"] == "open"
    assert snap["retry_after"] == pytest.approx(10.0, abs=0.01)


def test_breaker_half_open_success_closes() -> None:
    clk = [0.0]
    br = CircuitBreaker("x", threshold=1, cooldown=5, now=lambda: clk[0])
    br.record_failure()
    clk[0] = 5.0
    br.before_call()
    br.record_success()
    assert br.state() == BreakerState.CLOSED
    assert br.snapshot()["failures"] == 0


def test_breaker_registry_isolates_providers() -> None:
    reg = BreakerRegistry(threshold=1, cooldown=5)
    cursor = reg.get("cursor")
    claude = reg.get("claude")
    assert cursor is not claude
    assert reg.get("cursor") is cursor  # same name → same breaker
    cursor.record_failure()
    assert cursor.state() == BreakerState.OPEN
    assert claude.state() == BreakerState.CLOSED  # the other is unaffected


def test_breaker_cooldown_zero_immediately_half_open() -> None:
    clk = [0.0]
    br = CircuitBreaker("x", threshold=1, cooldown=0.0, now=lambda: clk[0])
    br.record_failure()
    assert br.state() == BreakerState.HALF_OPEN  # cooldown 0 → mature immediately


# --------------------------------------------------------------------------- #
# timeout — zero/None unlimited, transient error on expiry
# --------------------------------------------------------------------------- #


def test_with_timeout_zero_unlimited() -> None:
    async def quick():
        return 7

    assert asyncio.run(with_timeout(quick(), 0)) == 7
    assert asyncio.run(with_timeout(quick(), None)) == 7
    assert asyncio.run(with_timeout(quick(), -1)) == 7


def test_with_timeout_expiry_raises_transient() -> None:
    async def slow():
        await asyncio.sleep(10)

    async def go():
        return await with_timeout(slow(), 0.01)

    with pytest.raises(NetworkTimeoutError) as ei:
        asyncio.run(go())
    assert isinstance(ei.value, TransientError)
    assert is_transient(ei.value) is True
