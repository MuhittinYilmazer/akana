"""NetworkEngine F0 — retry + circuit breaker + timeout unit tests.

No real network / real time: clock + sleep + jitter are injected; coroutine
factories are fake (count attempts, raise scripted errors). The classifier's
transient-vs-permanent distinction and the breaker state machine
(closed/open/half-open) are verified deterministically.
"""

from __future__ import annotations

import asyncio

import pytest

from akana_server.network import (
    BreakerOpenError,
    BreakerState,
    CircuitBreaker,
    NetworkConfig,
    NetworkTimeoutError,
    PermanentError,
    TransientError,
    classify_exception,
    guard,
    is_transient,
    retry_async,
    with_timeout,
)
from akana_server.network.breaker import BreakerRegistry


# --------------------------------------------------------------------------- #
# Fake clock — deterministic time advancement
# --------------------------------------------------------------------------- #
class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        # No real sleep: advance the virtual clock.
        self.t += seconds


class _Status(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"status {status_code}")
        self.status_code = status_code


def _run(coro):
    """``asyncio.run`` shortcut — under PYTEST_DISABLE_PLUGIN_AUTOLOAD
    pytest-asyncio is not loaded, so async tests are wrapped synchronously
    (existing test_claude_provider.py pattern)."""
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Error classification
# --------------------------------------------------------------------------- #
class TestClassification:
    def test_explicit_transient_and_permanent(self) -> None:
        assert is_transient(TransientError("x")) is True
        assert is_transient(PermanentError("x")) is False

    def test_auth_status_is_permanent(self) -> None:
        assert is_transient(_Status(401)) is False
        assert is_transient(_Status(403)) is False

    def test_5xx_and_429_transient_4xx_permanent(self) -> None:
        assert is_transient(_Status(500)) is True
        assert is_transient(_Status(503)) is True
        assert is_transient(_Status(429)) is True
        assert is_transient(_Status(400)) is False
        assert is_transient(_Status(404)) is False

    def test_builtin_network_errors_transient(self) -> None:
        assert is_transient(TimeoutError()) is True
        assert is_transient(ConnectionRefusedError()) is True
        assert is_transient(asyncio.TimeoutError()) is True

    def test_unknown_is_permanent(self) -> None:
        assert is_transient(ValueError("boom")) is False

    def test_classify_label(self) -> None:
        assert classify_exception(_Status(503)) == "transient"
        assert classify_exception(_Status(401)) == "permanent"


# --------------------------------------------------------------------------- #
# Retry — transient vs permanent, exponential backoff, budget
# --------------------------------------------------------------------------- #
class TestRetry:
    def test_transient_retried_until_success(self) -> None:
        clock = FakeClock()
        cfg = NetworkConfig(max_retries=4, base_delay=1.0, jitter=0.0)
        attempts = {"n": 0}

        async def factory() -> str:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise _Status(503)
            return "ok"

        out = _run(
            retry_async(
                factory, cfg, now=clock.now, sleep=clock.sleep, rand=lambda: 0.5
            )
        )
        assert out == "ok"
        assert attempts["n"] == 3

    def test_permanent_raises_immediately(self) -> None:
        cfg = NetworkConfig(max_retries=5, base_delay=1.0, jitter=0.0)
        attempts = {"n": 0}

        async def factory() -> str:
            attempts["n"] += 1
            raise _Status(401)  # auth → permanent

        with pytest.raises(_Status):
            _run(retry_async(factory, cfg, sleep=FakeClock().sleep))
        assert attempts["n"] == 1  # single attempt

    def test_exhausts_max_retries(self) -> None:
        clock = FakeClock()
        cfg = NetworkConfig(max_retries=3, base_delay=1.0, jitter=0.0)
        attempts = {"n": 0}

        async def factory() -> str:
            attempts["n"] += 1
            raise _Status(503)

        with pytest.raises(_Status):
            _run(retry_async(factory, cfg, now=clock.now, sleep=clock.sleep))
        assert attempts["n"] == 3

    def test_exponential_backoff_delays(self) -> None:
        clock = FakeClock()
        cfg = NetworkConfig(
            max_retries=4, base_delay=1.0, max_delay=100.0, jitter=0.0, total_timeout=0
        )
        delays: list[float] = []
        attempts = {"n": 0}

        async def factory() -> str:
            attempts["n"] += 1
            raise _Status(503)

        with pytest.raises(_Status):
            _run(
                retry_async(
                    factory,
                    cfg,
                    now=clock.now,
                    sleep=clock.sleep,
                    rand=lambda: 0.5,
                    on_retry=lambda a, e, d: delays.append(d),
                )
            )
        # base*2^0, base*2^1, base*2^2 = 1, 2, 4
        assert delays == [1.0, 2.0, 4.0]

    def test_total_timeout_budget_stops_early(self) -> None:
        clock = FakeClock()
        cfg = NetworkConfig(
            max_retries=10, base_delay=10.0, max_delay=10.0, jitter=0.0, total_timeout=15.0
        )
        attempts = {"n": 0}

        async def factory() -> str:
            attempts["n"] += 1
            raise _Status(503)

        with pytest.raises(_Status):
            _run(retry_async(factory, cfg, now=clock.now, sleep=clock.sleep))
        # First attempt + 10s sleep = 10; the next sleep exceeds the budget (15) → stops.
        assert attempts["n"] == 2

    def test_cancelled_not_retried(self) -> None:
        cfg = NetworkConfig(max_retries=5)
        attempts = {"n": 0}

        async def factory() -> str:
            attempts["n"] += 1
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            _run(retry_async(factory, cfg, sleep=FakeClock().sleep))
        assert attempts["n"] == 1


# --------------------------------------------------------------------------- #
# Timeout
# --------------------------------------------------------------------------- #
class TestTimeout:
    def test_completes_within_timeout(self) -> None:
        async def quick() -> int:
            return 42

        assert _run(with_timeout(quick(), 1.0)) == 42

    def test_raises_on_timeout(self) -> None:
        async def slow() -> int:
            await asyncio.sleep(10)
            return 1

        with pytest.raises(NetworkTimeoutError):
            _run(with_timeout(slow(), 0.01))

    def test_timeout_error_is_transient(self) -> None:
        async def slow() -> int:
            await asyncio.sleep(10)
            return 1

        with pytest.raises(NetworkTimeoutError) as ei:
            _run(with_timeout(slow(), 0.01))
        assert is_transient(ei.value) is True

    def test_zero_timeout_means_unbounded(self) -> None:
        async def quick() -> int:
            return 7

        assert _run(with_timeout(quick(), 0)) == 7

        async def quick2() -> int:
            return 7

        assert _run(with_timeout(quick2(), None)) == 7


# --------------------------------------------------------------------------- #
# Circuit breaker — state machine
# --------------------------------------------------------------------------- #
class TestCircuitBreaker:
    def test_opens_after_threshold(self) -> None:
        clock = FakeClock()
        br = CircuitBreaker("p", threshold=3, cooldown=10.0, now=clock.now)
        assert br.state() == BreakerState.CLOSED
        for _ in range(3):
            br.before_call()
            br.record_failure()
        assert br.state() == BreakerState.OPEN
        with pytest.raises(BreakerOpenError):
            br.before_call()

    def test_success_resets_failure_count(self) -> None:
        clock = FakeClock()
        br = CircuitBreaker("p", threshold=3, cooldown=10.0, now=clock.now)
        br.record_failure()
        br.record_failure()
        br.record_success()  # counter resets
        br.record_failure()
        br.record_failure()
        assert br.state() == BreakerState.CLOSED  # threshold (3) not exceeded

    def test_half_open_after_cooldown_then_closed_on_success(self) -> None:
        clock = FakeClock()
        br = CircuitBreaker("p", threshold=2, cooldown=10.0, now=clock.now)
        br.record_failure()
        br.record_failure()
        assert br.state() == BreakerState.OPEN
        clock.t += 11.0  # cooldown elapsed
        assert br.state() == BreakerState.HALF_OPEN
        br.before_call()  # single-probe window
        br.record_success()
        assert br.state() == BreakerState.CLOSED

    def test_half_open_failure_reopens(self) -> None:
        clock = FakeClock()
        br = CircuitBreaker("p", threshold=2, cooldown=10.0, now=clock.now)
        br.record_failure()
        br.record_failure()
        clock.t += 11.0
        assert br.state() == BreakerState.HALF_OPEN
        br.before_call()
        br.record_failure()  # failure in half-open → open again
        assert br.state() == BreakerState.OPEN
        with pytest.raises(BreakerOpenError):
            br.before_call()

    def test_half_open_admits_single_probe_rest_fast_fail(self) -> None:
        """Regression (live rate-limit): in half-open, a swarm arriving AT THE SAME
        TIME (parallel turns + a background 2nd LLM call) would all pass through and
        hammer the half-recovered provider again, so the breaker dropped straight to
        OPEN and could never CLOSE. Now only the FIRST call (probe) passes; the rest
        fast-fail."""
        clock = FakeClock()
        br = CircuitBreaker("p", threshold=2, cooldown=10.0, now=clock.now)
        br.record_failure()
        br.record_failure()
        clock.t += 11.0  # cooldown elapsed → half-open
        assert br.state() == BreakerState.HALF_OPEN
        br.before_call()  # FIRST call = probe, passes
        # Concurrent calls arriving before the probe resolves fast-fail (swarm protection).
        with pytest.raises(BreakerOpenError):
            br.before_call()
        with pytest.raises(BreakerOpenError):
            br.before_call()
        # Probe succeeded → closed; the swarm now passes normally.
        br.record_success()
        assert br.state() == BreakerState.CLOSED
        br.before_call()  # closed → free

    def test_half_open_stale_probe_recovers_after_cooldown(self) -> None:
        """Leak protection: if the probe hangs (the call failed with CancelledError →
        record_* was never called), the breaker must not lock permanently in half-open —
        after cooldown it is considered stale and a new probe is allowed."""
        clock = FakeClock()
        br = CircuitBreaker("p", threshold=2, cooldown=10.0, now=clock.now)
        br.record_failure()
        br.record_failure()
        clock.t += 11.0
        assert br.state() == BreakerState.HALF_OPEN
        br.before_call()  # probe passes but its result is NEVER recorded (cancellation leak)
        with pytest.raises(BreakerOpenError):
            br.before_call()  # hung probe → fast-fail
        clock.t += 11.0  # stale-probe window elapsed
        br.before_call()  # new probe allowed → no lock
        br.record_success()
        assert br.state() == BreakerState.CLOSED

    def test_snapshot_shape(self) -> None:
        clock = FakeClock()
        br = CircuitBreaker("cursor", threshold=2, cooldown=10.0, now=clock.now)
        br.record_failure()
        br.record_failure()
        snap = br.snapshot()
        assert snap["name"] == "cursor"
        assert snap["state"] == "open"
        assert snap["threshold"] == 2
        assert snap["retry_after"] > 0

    def test_registry_isolates_providers(self) -> None:
        clock = FakeClock()
        reg = BreakerRegistry(threshold=2, cooldown=10.0, now=clock.now)
        cursor = reg.get("cursor")
        claude = reg.get("claude")
        cursor.record_failure()
        cursor.record_failure()
        assert cursor.state() == BreakerState.OPEN
        assert claude.state() == BreakerState.CLOSED  # independent
        assert reg.get("cursor") is cursor  # same name → same breaker


# --------------------------------------------------------------------------- #
# Guard — breaker + timeout + retry combination
# --------------------------------------------------------------------------- #
class TestGuard:
    def test_success_path_unchanged(self) -> None:
        reg = BreakerRegistry()
        cfg = NetworkConfig(max_retries=3, jitter=0.0)

        async def factory() -> str:
            return "result"

        out = _run(guard(factory, provider="cursor", cfg=cfg, registry=reg))
        assert out == "result"
        assert reg.get("cursor").state() == BreakerState.CLOSED

    def test_open_breaker_fast_fails_without_call(self) -> None:
        reg = BreakerRegistry(threshold=1, cooldown=100.0)
        cfg = NetworkConfig(max_retries=1, breaker_threshold=1, breaker_cooldown=100.0)
        calls = {"n": 0}

        async def failing() -> str:
            calls["n"] += 1
            raise _Status(503)

        # The first call opens the breaker.
        with pytest.raises(_Status):
            _run(guard(failing, provider="cursor", cfg=cfg, registry=reg))
        assert calls["n"] == 1
        # Second call: breaker open → the factory is NEVER called.
        with pytest.raises(BreakerOpenError):
            _run(guard(failing, provider="cursor", cfg=cfg, registry=reg))
        assert calls["n"] == 1

    def test_permanent_error_records_failure_and_raises(self) -> None:
        reg = BreakerRegistry(threshold=10, cooldown=100.0)
        cfg = NetworkConfig(max_retries=3, breaker_threshold=10)

        async def auth_fail() -> str:
            raise _Status(401)

        with pytest.raises(_Status):
            _run(guard(auth_fail, provider="claude", cfg=cfg, registry=reg))
        snap = reg.get("claude").snapshot()
        assert snap["failures"] == 1  # auth also counts as a consecutive failure
