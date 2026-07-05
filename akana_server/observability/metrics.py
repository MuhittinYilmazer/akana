"""In-process lightweight metric registry — counters + timers (stdlib-only).

Architectural hardening: today there are no numeric answers to "is the turn
stalled?", "how often does the LLM error?", "how many times did the timeout
fire?", "how deep is the queue?". This module provides a **dependency-free**
(no new pip packages), **thread-safe** in-process registry that can later be
dumped to the ``/metrics`` surface via ``snapshot()``.

Live metrics (written by the chat turn pipeline, exposed via ``/api/v1/system``
in ``api/routes/system.py``):

* ``turn_latency_ms``   — turn latency (timer; ``chat_producer.py`` / ``chat/__init__.py``).
* ``llm_errors``        — LLM error count (counter; same call sites).
* ``llm_timeout_fires`` — watchdog timeout fires (counter; same call sites).
* ``queue_depth``       — turn queue depth (gauge, via ``set``; ``chat_producer.py``).

Design:

* A single ``threading.Lock`` protects all registry mutations — simplicity over
  wing speed; this is not a hot path (millions of increments per second are not
  expected).
* ``Timer`` keeps only summary statistics (count/sum/min/max) — it does not
  store every sample; no unbounded memory growth, sufficient for ``/metrics``.
* ``snapshot()`` returns a deep, read-only flat ``dict`` (copied under the lock);
  the caller can freely serialise it without the registry's internal state leaking.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

__all__ = [
    "Counter",
    "Timer",
    "MetricsRegistry",
    "registry",
]


@dataclass
class Counter:
    """Monotonic (or ``set``-able like a gauge) numeric counter.

    Grows via ``inc``, set to an absolute value via ``set`` (for gauge uses such
    as queue depth). Mutations are called under the registry lock.
    """

    value: float = 0.0

    def inc(self, amount: float = 1.0) -> None:
        self.value += amount

    def set(self, amount: float) -> None:
        self.value = amount


@dataclass
class Timer:
    """Summary statistics of duration samples (without storing each sample).

    Fed via ``observe(ms)``; accumulates count/sum/min/max. ``avg_ms`` is
    derived. Individual samples are not retained → memory stays constant.
    """

    count: int = 0
    total_ms: float = 0.0
    min_ms: float | None = None
    max_ms: float | None = None

    def observe(self, ms: float) -> None:
        self.count += 1
        self.total_ms += ms
        if self.min_ms is None or ms < self.min_ms:
            self.min_ms = ms
        if self.max_ms is None or ms > self.max_ms:
            self.max_ms = ms

    @property
    def avg_ms(self) -> float:
        return (self.total_ms / self.count) if self.count else 0.0


@dataclass
class MetricsRegistry:
    """Thread-safe counter + timer container; dumped via ``snapshot()``.

    Names are lazily created: the first ``incr``/``observe`` call creates the
    metric. All reads/writes are under a single ``_lock`` — reads are also
    locked because ``snapshot`` must capture a consistent point-in-time view.
    """

    _counters: dict[str, Counter] = field(default_factory=dict)
    _timers: dict[str, Timer] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # --- counters -------------------------------------------------------
    def incr(self, name: str, amount: float = 1.0) -> None:
        """Increment the ``name`` counter by ``amount`` (creates it from 0 if absent)."""

        with self._lock:
            counter = self._counters.get(name)
            if counter is None:
                counter = Counter()
                self._counters[name] = counter
            counter.inc(amount)

    def set(self, name: str, amount: float) -> None:
        """Set the ``name`` counter to the absolute ``amount`` (gauge; e.g. queue_depth)."""

        with self._lock:
            counter = self._counters.get(name)
            if counter is None:
                counter = Counter()
                self._counters[name] = counter
            counter.set(amount)

    # --- timers -------------------------------------------------
    def observe(self, name: str, ms: float) -> None:
        """Add a duration sample (ms) to the ``name`` timer (creates it if absent)."""

        with self._lock:
            timer = self._timers.get(name)
            if timer is None:
                timer = Timer()
                self._timers[name] = timer
            timer.observe(ms)

    def time(self, name: str) -> _TimerContext:
        """``with registry.time("turn_latency_ms"):`` — measure block duration in ms.

        The elapsed time is ``observe``d on context exit (even if an exception occurs).
        """

        return _TimerContext(self, name)

    # --- reads ----------------------------------------------------------
    def snapshot(self) -> dict[str, dict[str, dict[str, float]]]:
        """Consistent, read-only flat copy of all metrics (for ``/metrics``).

        Shape::

            {
              "counters": {"<name>": {"value": float}, ...},
              "timers":   {"<name>": {"count","sum_ms","min_ms","max_ms","avg_ms"}, ...},
            }

        Copied under the lock; the returned dict is independent of the registry's
        internal state.
        """

        with self._lock:
            counters = {name: {"value": c.value} for name, c in self._counters.items()}
            timers = {
                name: {
                    "count": float(t.count),
                    "sum_ms": t.total_ms,
                    "min_ms": t.min_ms if t.min_ms is not None else 0.0,
                    "max_ms": t.max_ms if t.max_ms is not None else 0.0,
                    "avg_ms": t.avg_ms,
                }
                for name, t in self._timers.items()
            }
        return {"counters": counters, "timers": timers}

    def reset(self) -> None:
        """Clear all metrics (primarily for tests; rarely in production)."""

        with self._lock:
            self._counters.clear()
            self._timers.clear()


class _TimerContext:
    """Context manager for ``MetricsRegistry.time`` — measures wall-clock time."""

    __slots__ = ("_registry", "_name", "_start")

    def __init__(self, reg: MetricsRegistry, name: str) -> None:
        self._registry = reg
        self._name = name
        self._start = 0.0

    def __enter__(self) -> _TimerContext:
        import time

        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        import time

        elapsed_ms = (time.perf_counter() - self._start) * 1000.0
        self._registry.observe(self._name, elapsed_ms)
        return False  # do not swallow the exception


# Module-level shared registry — single instance for the lifetime of the process.
# In the future the ``/metrics`` route will call ``registry.snapshot()``; writers
# use ``registry.incr(...)``/``registry.observe(...)``.
registry = MetricsRegistry()
