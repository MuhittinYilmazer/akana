"""Canonical UTC timestamp helper for the memory package.

Every store stamps rows with the same millisecond-precision, ``Z``-suffixed
UTC ISO-8601 string. Memory ordering relies on these strings sorting
lexicographically, so the format must be identical across every store; this
leaf module (no intra-package imports) is the single source of truth the store
methods delegate to.

Kept inside ``akana.memory`` on purpose: the package is self-contained and does
not import the surrounding project, so it carries its own copy rather than
reaching into an ``akana_server`` util.
"""

from __future__ import annotations

from datetime import UTC, datetime


def iso_now() -> str:
    """Current UTC instant as a millisecond-precision ``...Z`` ISO-8601 string."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
