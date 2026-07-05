"""NetworkEngine — circuit breaker, per provider/endpoint.

State machine::

    closed    --(``threshold`` consecutive failures)-->  open
    open      --(``cooldown`` seconds elapsed, next call)-->  half-open
    half-open --(success)--> closed   |   --(failure)--> open

* **closed** — calls pass through; a consecutive failure counter is kept and
  reset on success.
* **open** — NO call is made; :meth:`before_call` raises
  :class:`BreakerOpenError` fast-fail. A single probe window opens after
  the cooldown expires.
* **half-open** — one probe passes; success → closed (counter reset),
  failure → open (cooldown restarts).

The clock is injectable (for tests). :class:`BreakerRegistry` maps name →
breaker so that the ``cursor`` and ``claude`` provider breakers are
independent (one provider crashing does not fast-fail the other).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from enum import Enum

from akana_server.network.errors import TransientError

__all__ = [
    "BreakerOpenError",
    "BreakerRegistry",
    "BreakerState",
    "CircuitBreaker",
]


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class BreakerOpenError(TransientError):
    """Circuit is open — fast-fail without making a call (counted as transient).

    It is a ``TransientError`` subtype, but retrying inside :func:`retry_async`
    makes no sense; ``guard`` checks the breaker OUTSIDE the retry loop to
    prevent this (no retry at all while open).
    """

    def __init__(self, name: str, retry_after: float) -> None:
        self.name = name
        self.retry_after = max(0.0, retry_after)
        super().__init__(
            f"«{name}» temporarily disabled (too many consecutive failures); "
            f"will retry in about {self.retry_after:.0f}s."
        )


class CircuitBreaker:
    """Circuit breaker for a single provider/endpoint (thread-safe)."""

    def __init__(
        self,
        name: str,
        *,
        threshold: int = 5,
        cooldown: float = 30.0,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._name = name
        self._threshold = max(1, int(threshold))
        self._cooldown = max(0.0, float(cooldown))
        self._now = now
        self._lock = threading.Lock()
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        # Single-flight probe: only one attempt passes through half-open at a time.
        self._probe_in_flight = False
        self._probe_at = 0.0

    @property
    def name(self) -> str:
        return self._name

    def state(self) -> BreakerState:
        """Current state (matures open→half-open if the cooldown has elapsed)."""
        with self._lock:
            self._mature_locked()
            return self._state

    def _mature_locked(self) -> None:
        if (
            self._state == BreakerState.OPEN
            and self._now() - self._opened_at >= self._cooldown
        ):
            self._state = BreakerState.HALF_OPEN
            # New half-open window → fresh probe right (clear stale flag).
            self._probe_in_flight = False

    def before_call(self) -> None:
        """Is a call permitted? Raises :class:`BreakerOpenError` if open."""
        with self._lock:
            self._mature_locked()
            if self._state == BreakerState.OPEN:
                retry_after = self._cooldown - (self._now() - self._opened_at)
                raise BreakerOpenError(self._name, retry_after)
            if self._state == BreakerState.HALF_OPEN:
                # SINGLE-FLIGHT probe: only ONE attempt passes through half-open.
                # Previously every concurrent call passed → parallel turns + each
                # turn's background 2nd LLM call (memory capture) formed a
                # "thundering herd" that hammered a half-recovered provider; the
                # breaker would flip back to OPEN immediately and could NEVER close
                # (rate-limit "stuck open"). Now the rest of the herd fast-fails
                # (BreakerOpenError).
                # Leak guard: if a probe gets stuck (call dropped with CancelledError
                # → record_* never called), it is considered stale after the cooldown
                # and a new probe is allowed; no permanent deadlock.
                stale = self._now() - self._probe_at >= self._cooldown
                if self._probe_in_flight and not stale:
                    retry_after = max(
                        0.0, self._cooldown - (self._now() - self._probe_at)
                    )
                    raise BreakerOpenError(self._name, retry_after)
                self._probe_in_flight = True
                self._probe_at = self._now()

    def record_success(self) -> None:
        """Success: reset counter, set state to closed."""
        with self._lock:
            self._failures = 0
            self._state = BreakerState.CLOSED
            self._probe_in_flight = False

    def record_failure(self) -> None:
        """Failure: immediately open in half-open; open in closed once the threshold is exceeded."""
        with self._lock:
            if self._state == BreakerState.HALF_OPEN:
                self._trip_locked()
                return
            self._failures += 1
            if self._failures >= self._threshold:
                self._trip_locked()

    def _trip_locked(self) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = self._now()
        self._failures = self._threshold
        self._probe_in_flight = False

    def snapshot(self) -> dict[str, object]:
        """State summary for observability (REST /network/status)."""
        with self._lock:
            self._mature_locked()
            retry_after = 0.0
            if self._state == BreakerState.OPEN:
                retry_after = max(
                    0.0, self._cooldown - (self._now() - self._opened_at)
                )
            return {
                "name": self._name,
                "state": self._state.value,
                "failures": self._failures,
                "threshold": self._threshold,
                "cooldown": self._cooldown,
                "retry_after": round(retry_after, 3),
            }


class BreakerRegistry:
    """Name → :class:`CircuitBreaker` mapping (per-provider isolation)."""

    def __init__(
        self,
        *,
        threshold: int = 5,
        cooldown: float = 30.0,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = threshold
        self._cooldown = cooldown
        self._now = now
        self._lock = threading.Lock()
        self._breakers: dict[str, CircuitBreaker] = {}

    def get(self, name: str) -> CircuitBreaker:
        with self._lock:
            br = self._breakers.get(name)
            if br is None:
                br = CircuitBreaker(
                    name,
                    threshold=self._threshold,
                    cooldown=self._cooldown,
                    now=self._now,
                )
                self._breakers[name] = br
            return br

    def get_or_create(
        self, name: str, *, threshold: int, cooldown: float
    ) -> CircuitBreaker:
        """Return the ``name`` breaker; create it with the given threshold/cooldown if absent.

        Per-call config WITHOUT mutating the shared registry defaults: the old
        path called ``configure`` on every call and changed the shared defaults →
        concurrent calls with different configs would flip-flop the defaults, and
        the first call's config was never applied anyway (``configure`` only
        affects breakers opened AFTER the call). Here the config is bound to the
        breaker at creation time; the existing breaker's state/config is
        preserved (state machine is not disturbed)."""
        with self._lock:
            br = self._breakers.get(name)
            if br is None:
                br = CircuitBreaker(
                    name,
                    threshold=threshold,
                    cooldown=cooldown,
                    now=self._now,
                )
                self._breakers[name] = br
            return br

    def snapshot(self) -> list[dict[str, object]]:
        with self._lock:
            breakers = list(self._breakers.values())
        return [br.snapshot() for br in breakers]
