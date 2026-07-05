"""Observability core primitives — unit tests.

This wave ADDITIVELY adds three primitives; the tests lock their contracts:

* ``errors`` — ``code``/``http_status``/``category`` mapping + safe
  ``user_message`` + instance-based override.
* ``capture_failure`` — correlates the record with the **active turn's**
  ``trace_id``, categorizes ``AkanaError``, marks a generic exception
  ``uncategorized``, honors the ``reraise`` flag.
* ``metrics`` — counter/gauge/timer + ``snapshot()`` + thread-safe increment.

No existing path implements these yet; here only the vocabulary itself is
validated.
"""

from __future__ import annotations

import logging
import threading

import pytest

from akana_server.observability import (
    Counter,
    AkanaError,
    MetricsRegistry,
    Timer,
    capture_failure,
    registry,
)
from akana_server.observability.turn_context import _current, begin_turn


def _reset_turn() -> None:
    _current.set(None)


# --------------------------------------------------------------------------- #
# errors.py — taxonomy: code / http_status / category / user_message
# --------------------------------------------------------------------------- #


def test_error_default_code_and_status_mapping() -> None:
    """The base class carries its own default code + http_status + category."""

    exc = AkanaError()
    assert exc.code == "akana_error"
    assert exc.http_status == 500
    # category == concrete class name (log/metrics key)
    assert exc.category == type(exc).__name__


def test_error_is_an_exception() -> None:
    assert issubclass(AkanaError, Exception)


def test_error_has_safe_user_message_by_default() -> None:
    """``AkanaError`` yields a non-empty ``user_message`` that leaks no internal detail."""

    exc = AkanaError()
    assert isinstance(exc.user_message, str)
    assert exc.user_message  # not empty


def test_error_instance_overrides_win() -> None:
    """Instance-based code/http_status/user_message override the defaults."""

    exc = AkanaError(
        "upstream 502 from cursor",
        code="cursor_unavailable",
        http_status=502,
        user_message="Sağlayıcıya ulaşılamadı, tekrar denenecek.",
    )
    assert exc.code == "cursor_unavailable"
    assert exc.http_status == 502
    assert exc.user_message == "Sağlayıcıya ulaşılamadı, tekrar denenecek."
    # the internal (technical) message is in str(); kept separate from user_message
    assert "upstream 502" in str(exc)
    assert exc.category == "AkanaError"


def test_error_message_defaults_to_code_when_omitted() -> None:
    """When no technical message is given, ``str(exc)`` falls back to the code (never empty/meaningless)."""

    assert str(AkanaError()) == "akana_error"
    assert str(AkanaError(code="missing_field")) == "missing_field"


# --------------------------------------------------------------------------- #
# failures.py — capture_failure: trace correlation + categorize + reraise
# --------------------------------------------------------------------------- #


def test_capture_failure_records_active_trace_id(caplog: pytest.LogCaptureFixture) -> None:
    """The record is correlated with the active turn's ``trace_id``."""

    _reset_turn()
    ctx = begin_turn("conv-cap")
    try:
        with caplog.at_level(logging.ERROR):
            ret = capture_failure(RuntimeError("boom"), where="provider.stream")
        # the exception is returned back (for flow control)
        assert isinstance(ret, RuntimeError)
        rec = next(r for r in caplog.records if r.getMessage().startswith("turn failure captured"))
        assert rec.trace_id == ctx.trace_id
        assert rec.failure_where == "provider.stream"
        assert rec.failure_type == "RuntimeError"
        # traceback captured (the opposite of silent swallowing)
        assert rec.exc_info is not None
    finally:
        _reset_turn()


def test_capture_failure_categorizes_akana_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If it's an ``AkanaError``, category + code are reflected in the record."""

    _reset_turn()
    begin_turn("conv-cat")
    try:
        with caplog.at_level(logging.ERROR):
            capture_failure(
                AkanaError(code="vector_down"), where="memory.recall"
            )
        rec = next(r for r in caplog.records if r.getMessage().startswith("turn failure captured"))
        assert rec.failure_category == "AkanaError"
        assert rec.failure_code == "vector_down"
    finally:
        _reset_turn()


def test_capture_failure_marks_generic_uncategorized(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Generic exception → ``uncategorized`` (no code)."""

    _reset_turn()
    begin_turn("conv-generic")
    try:
        with caplog.at_level(logging.ERROR):
            capture_failure(ValueError("nope"), where="parse.input")
        rec = next(r for r in caplog.records if r.getMessage().startswith("turn failure captured"))
        assert rec.failure_category == "uncategorized"
        assert rec.failure_code is None
        assert rec.failure_type == "ValueError"
    finally:
        _reset_turn()


def test_capture_failure_outside_turn_uses_placeholder(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Even outside a turn the record emits safely with ``trace_id="-"`` (does not blow up)."""

    _reset_turn()
    with caplog.at_level(logging.ERROR):
        capture_failure(RuntimeError("orphan"), where="startup")
    rec = next(r for r in caplog.records if r.getMessage().startswith("turn failure captured"))
    assert rec.trace_id == "-"


def test_capture_failure_reraise_flag() -> None:
    """``reraise=True`` re-raises the same exception; ``False`` returns it."""

    _reset_turn()
    begin_turn("conv-reraise")
    try:
        sentinel = AkanaError("retry me")
        with pytest.raises(AkanaError) as ei:
            capture_failure(sentinel, where="x", reraise=True)
        assert ei.value is sentinel
        # reraise=False → does not raise, returns the same object
        out = capture_failure(sentinel, where="x", reraise=False)
        assert out is sentinel
    finally:
        _reset_turn()


def test_capture_failure_respects_level(caplog: pytest.LogCaptureFixture) -> None:
    """The ``level`` parameter sets the record level (expected failure → WARNING)."""

    _reset_turn()
    begin_turn("conv-level")
    try:
        with caplog.at_level(logging.WARNING):
            capture_failure(
                AkanaError("busy"), where="queue", level=logging.WARNING
            )
        rec = next(r for r in caplog.records if r.getMessage().startswith("turn failure captured"))
        assert rec.levelno == logging.WARNING
    finally:
        _reset_turn()


# --------------------------------------------------------------------------- #
# metrics.py — counter / gauge / timer / snapshot / thread-safety
# --------------------------------------------------------------------------- #


def test_counter_inc_and_set() -> None:
    c = Counter()
    assert c.value == 0.0
    c.inc()
    c.inc(4)
    assert c.value == 5.0
    c.set(2)  # gauge behavior
    assert c.value == 2.0


def test_timer_summary_stats() -> None:
    t = Timer()
    for ms in (10.0, 30.0, 20.0):
        t.observe(ms)
    assert t.count == 3
    assert t.total_ms == 60.0
    assert t.min_ms == 10.0
    assert t.max_ms == 30.0
    assert t.avg_ms == 20.0
    # avg does not blow up on an empty timer
    assert Timer().avg_ms == 0.0


def test_registry_counter_and_snapshot() -> None:
    reg = MetricsRegistry()
    reg.incr("llm_errors")
    reg.incr("llm_errors", 2)
    reg.incr("llm_timeout_fires")
    reg.set("queue_depth", 7)
    snap = reg.snapshot()
    assert snap["counters"]["llm_errors"]["value"] == 3.0
    assert snap["counters"]["llm_timeout_fires"]["value"] == 1.0
    assert snap["counters"]["queue_depth"]["value"] == 7.0


def test_registry_timer_observe_and_snapshot() -> None:
    reg = MetricsRegistry()
    reg.observe("turn_latency_ms", 100.0)
    reg.observe("turn_latency_ms", 300.0)
    snap = reg.snapshot()
    timer = snap["timers"]["turn_latency_ms"]
    assert timer["count"] == 2.0
    assert timer["sum_ms"] == 400.0
    assert timer["min_ms"] == 100.0
    assert timer["max_ms"] == 300.0
    assert timer["avg_ms"] == 200.0


def test_registry_time_contextmanager_records() -> None:
    reg = MetricsRegistry()
    with reg.time("turn_latency_ms"):
        pass
    snap = reg.snapshot()
    assert snap["timers"]["turn_latency_ms"]["count"] == 1.0
    # measured duration cannot be negative
    assert snap["timers"]["turn_latency_ms"]["sum_ms"] >= 0.0


def test_registry_time_records_even_on_exception() -> None:
    """Even if an exception occurs within the context, the duration is recorded and the exception is not swallowed."""

    reg = MetricsRegistry()
    with pytest.raises(ValueError):
        with reg.time("turn_latency_ms"):
            raise ValueError("mid-timer")
    assert reg.snapshot()["timers"]["turn_latency_ms"]["count"] == 1.0


def test_registry_snapshot_is_decoupled_copy() -> None:
    """``snapshot()`` is a read-only copy; mutating it does not leak into the record."""

    reg = MetricsRegistry()
    reg.incr("x")
    snap = reg.snapshot()
    snap["counters"]["x"]["value"] = 999.0
    snap["counters"]["injected"] = {"value": 1.0}
    fresh = reg.snapshot()
    assert fresh["counters"]["x"]["value"] == 1.0
    assert "injected" not in fresh["counters"]


def test_registry_reset_clears() -> None:
    reg = MetricsRegistry()
    reg.incr("a")
    reg.observe("b", 5.0)
    reg.reset()
    snap = reg.snapshot()
    assert snap["counters"] == {}
    assert snap["timers"] == {}


def test_registry_is_thread_safe_under_contention() -> None:
    """Concurrent increments are not lost (the lock really protects)."""

    reg = MetricsRegistry()
    threads_n = 8
    per_thread = 1000

    def _hammer() -> None:
        for _ in range(per_thread):
            reg.incr("hits")

    threads = [threading.Thread(target=_hammer) for _ in range(threads_n)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert reg.snapshot()["counters"]["hits"]["value"] == float(threads_n * per_thread)


def test_module_registry_is_shared_singleton() -> None:
    """Module-level ``registry`` is a shared single instance (later /metrics reads from here)."""

    assert isinstance(registry, MetricsRegistry)
    before = registry.snapshot()["counters"].get(
        "test_shared_marker", {"value": 0.0}
    )["value"]
    registry.incr("test_shared_marker")
    after = registry.snapshot()["counters"]["test_shared_marker"]["value"]
    assert after == before + 1.0
    # test isolation: clean up the trace we left behind
    registry.reset()
