"""``capture_failure`` — converts a silent ``except`` into a loud, traceable record.

Architectural hardening: today exceptions in the codebase are mostly swallowed
via ``except Exception: log.warning(...)`` (or a completely silent ``pass``),
losing which turn failed, where it exploded, and what category it falls into.
``capture_failure`` reduces this to a single call:

    try:
        ...
    except Exception as exc:  # noqa: BLE001
        capture_failure(exc, where="provider.stream")

The record is correlated with the **active turn**'s ``trace_id``
(``turn_context``), located with a ``where`` label, includes the exception type,
and if the exception is an ``AkanaError`` it is categorised with
``code``/``category``. This turns a previously invisible failure into a
categorised, end-to-end searchable log line via ``trace_id``.

Live callers today include the connectors package
(``akana_server/connectors/registry.py``, ``akana_server/connectors/router.py``);
more ``except`` sites can adopt this incrementally.
"""

from __future__ import annotations

import logging

from akana_server.observability.errors import AkanaError
from akana_server.observability.turn_context import current_trace_id

__all__ = ["capture_failure"]

_log = logging.getLogger("akana.observability.failures")


def capture_failure(
    exc: BaseException,
    *,
    where: str,
    reraise: bool = False,
    level: int = logging.ERROR,
    logger: logging.Logger | None = None,
) -> BaseException:
    """Process an exception as a structured record correlated against the active turn.

    Converts a previously silent ``except`` into a **loud** record that is
    correlated via ``trace_id``, located with a ``where`` label, and classified
    by type/category.

    Args:
        exc: The caught exception (``except ... as exc``).
        where: A stable, short label indicating where the error occurred
            (e.g. ``"provider.stream"``, ``"persist.assistant_turn"``).
        reraise: If ``True``, ``exc`` is re-raised after being recorded
            (record-and-propagate pattern). Default ``False`` (swallow-but-loud).
        level: Log level for the record (default ``ERROR``). The caller can
            lower this to ``WARNING`` for expected/benign failures.
        logger: Target logger; defaults to the module logger. ``trace_id`` is
            already added via the common ``TurnLogFilter``-equipped handler, but
            it is also carried in ``extra`` so the record is readable
            independently of the turn context.

    Returns:
        ``exc`` — the caller can use it for flow control if desired.

    Raises:
        ``exc`` if ``reraise=True``.
    """

    trace_id = current_trace_id()
    exc_type = type(exc).__name__

    # Extract taxonomy fields if AkanaError; otherwise "uncategorized".
    if isinstance(exc, AkanaError):
        category = exc.category
        code = exc.code
    else:
        category = "uncategorized"
        code = None

    log = logger if logger is not None else _log

    # exc_info=exc → traceback enters the record (opposite of silent swallow: full context).
    # extra → machine-readable fields; message is the human-readable summary.
    log.log(
        level,
        "turn failure captured: where=%s type=%s category=%s code=%s trace_id=%s",
        where,
        exc_type,
        category,
        code if code is not None else "-",
        trace_id,
        exc_info=exc,
        extra={
            "trace_id": trace_id,
            "failure_where": where,
            "failure_type": exc_type,
            "failure_category": category,
            "failure_code": code,
        },
    )

    if reraise:
        raise exc
    return exc
