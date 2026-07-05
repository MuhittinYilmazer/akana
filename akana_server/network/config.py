"""NetworkEngine — configuration resolution (from the restart-free runtime layer).

All network resilience parameters live in the ``runtime_settings.py`` SCHEMA
(category: ``ag``) and can therefore be adjusted via the panel/REST
**without restarting**. This module reduces them to a single frozen
:class:`NetworkConfig` value.

Resolution is refreshed from the runtime store on every call
(``load_network_config``); paths that carry no ``Settings`` or cannot reach the
store fall back to schema defaults (never raises — ``get_runtime`` is already
defensive).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["NetworkConfig", "load_network_config"]


@dataclass(frozen=True, slots=True)
class NetworkConfig:
    """Resolved network parameters for retry + circuit breaker + timeout."""

    #: retry — maximum number of attempts (1 = single attempt, no retry).
    max_retries: int = 3
    #: retry — initial backoff delay (seconds); grows exponentially.
    base_delay: float = 0.5
    #: retry — backoff ceiling (seconds).
    max_delay: float = 8.0
    #: retry — total time budget across all attempts (seconds, 0 = unlimited).
    total_timeout: float = 60.0
    #: retry — jitter ratio (0..1); delay is randomised by ±this fraction.
    jitter: float = 0.25
    #: breaker — number of consecutive failures before transitioning to "open".
    breaker_threshold: int = 5
    #: breaker — wait time (seconds) in "open" before the "half-open" probe attempt.
    breaker_cooldown: float = 30.0

    @property
    def retry_enabled(self) -> bool:
        return self.max_retries > 1

    @property
    def breaker_enabled(self) -> bool:
        return self.breaker_threshold > 0


def _runtime(key: str, settings: Any, fallback: Any) -> Any:
    """Thin wrapper around ``get_runtime`` — returns the fallback if the schema is absent."""
    try:
        from akana_server.runtime_settings import get_runtime

        return get_runtime(key, settings)
    except Exception:  # schema/key absent → fall back to default
        return fallback


def load_network_config(settings: Any) -> NetworkConfig:
    """Resolve a fresh :class:`NetworkConfig` from the runtime store + env (defensive)."""
    d = NetworkConfig()
    return NetworkConfig(
        max_retries=int(_runtime("network_max_retries", settings, d.max_retries)),
        base_delay=float(_runtime("network_base_delay", settings, d.base_delay)),
        max_delay=float(_runtime("network_max_delay", settings, d.max_delay)),
        total_timeout=float(
            _runtime("network_total_timeout", settings, d.total_timeout)
        ),
        jitter=float(_runtime("network_jitter", settings, d.jitter)),
        breaker_threshold=int(
            _runtime("network_breaker_threshold", settings, d.breaker_threshold)
        ),
        breaker_cooldown=float(
            _runtime("network_breaker_cooldown", settings, d.breaker_cooldown)
        ),
    )
