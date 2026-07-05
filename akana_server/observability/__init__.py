"""Observability primitives (turn tracing, correlation).

Architectural hardening Step A: every chat turn is assigned an end-to-end
``trace_id``; it is carried across gate → provider → streaming → persist via
``contextvars``, automatically appended to ALL log lines via ``TurnLogFilter``,
and broadcast to the client in the SSE ``meta`` event. This makes "turn stalled"
/ "wrong provider responded" class failures traceable end-to-end from a single id.

The correlation id is intentionally kept separate from persistent record ids
(``turn_id`` = persisted user/assistant ulid) and is named ``trace_id`` to
avoid confusion — this module does not touch record semantics; it only adds
observation.
"""

from __future__ import annotations

from akana_server.observability.errors import AkanaError
from akana_server.observability.failures import capture_failure
from akana_server.observability.metrics import (
    Counter,
    MetricsRegistry,
    Timer,
    registry,
)
from akana_server.observability.turn_context import (
    TurnContext,
    TurnLogFilter,
    begin_turn,
    current_trace_id,
    current_turn,
    new_trace_id,
    update_turn,
)

__all__ = [
    # turn correlation (Step A)
    "TurnContext",
    "TurnLogFilter",
    "begin_turn",
    "current_trace_id",
    "current_turn",
    "new_trace_id",
    "update_turn",
    # error taxonomy
    "AkanaError",
    # failure capture
    "capture_failure",
    # metrics
    "Counter",
    "Timer",
    "MetricsRegistry",
    "registry",
]
