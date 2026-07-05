"""Error taxonomy — a vocabulary for classifying silent ``except Exception`` blocks.

Architectural hardening: ~222 ``except Exception`` sites in the codebase swallow
failures without distinguishing whether an error is retriable, a bug, bad user
input, or a degraded-but-continuing subsystem. This module provides a single
root class, ``AkanaError``, carrying that classification.
``capture_failure`` (failures.py) categorises the record by checking whether
an exception is an ``AkanaError``.

Each ``AkanaError`` carries:

* ``code``         — stable, machine-readable short label (log/metrics key).
* ``http_status``  — appropriate status code if this error becomes an HTTP response.
* ``user_message`` — message **safe** to show to the user (no internal details/stack
  leaked); if not provided, a sensible default is used.

The four original subcategories (``TransientError``/``FatalError``/``UserError``/
``DegradedError``) were vocabulary-only scaffolding with zero production raise
or catch sites and were removed; raise ``AkanaError`` directly with an explicit
``code``/``http_status`` when a call site needs to be classified. Network-layer
retriability already has its own live taxonomy in
:mod:`akana_server.network.errors` (``TransientError``/``PermanentError``),
which this module does not duplicate.
"""

from __future__ import annotations


class AkanaError(Exception):
    """Root class of all classified Akana errors.

    Carries ``code``/``http_status``/``user_message``, each overridable per
    instance. Raise directly (or via a project-specific subclass) with an
    explicit ``code`` to classify a failure.
    """

    default_code: str = "akana_error"
    default_http_status: int = 500
    default_user_message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        http_status: int | None = None,
        user_message: str | None = None,
    ) -> None:
        # ``message`` is the internal/technical description (goes to logs/str);
        # ``user_message`` is the safe summary shown to the end user.
        # If message is not provided, derive it from the code.
        self.code: str = code or self.default_code
        self.http_status: int = (
            http_status if http_status is not None else self.default_http_status
        )
        self.user_message: str = user_message or self.default_user_message
        super().__init__(message if message is not None else self.code)

    @property
    def category(self) -> str:
        """Coarse class label (concrete subclass name), for log/metrics."""

        return type(self).__name__


__all__ = [
    "AkanaError",
]
