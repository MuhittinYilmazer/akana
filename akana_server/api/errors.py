"""Canonical HTTP error envelope for the REST API.

Every route raises errors in the same shape so the frontend can rely on a single
contract::

    {"error": {"code": "SOME_CODE", "message": "Human-readable text", ...}}

``http_error`` is the ONE place that builds this envelope. Optional ``**extra``
keys are merged INTO the ``error`` object (e.g. ``fields=`` on validation
errors) — this keeps byte-identical parity with the per-module helpers it
replaces, some of which carried extra keys.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException


def http_error(status: int, code: str, message: str, **extra: Any) -> HTTPException:
    """Build an :class:`HTTPException` with the canonical ``{"error": {...}}`` body.

    ``status`` is the HTTP status code; ``code`` is a stable machine token; ``message``
    is human-readable. Any ``**extra`` keys are merged into the ``error`` object.
    """
    error: dict[str, Any] = {"code": code, "message": message}
    if extra:
        error.update(extra)
    return HTTPException(status_code=status, detail={"error": error})
