"""Shared time helpers for the server layer.

``iso_now()`` is the single canonical UTC timestamp shape used across the
server's stores (audit, file oplog, multimodal, persona, tool gateway): a
millisecond-precision, ``Z``-suffixed ISO-8601 string. These stamps are
compared lexicographically, so every writer must emit the exact same format —
this module is the one place that format is defined.

The ``akana.memory`` package deliberately keeps its own copy
(``akana.memory._time``) because it is self-contained and must not import the
surrounding server; both copies are byte-identical by design.
"""

from __future__ import annotations

from datetime import UTC, datetime


def iso_now() -> str:
    """Current UTC instant as a millisecond-precision ``...Z`` ISO-8601 string."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
