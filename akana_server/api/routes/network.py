"""NetworkEngine F0 — observability REST surface (bearer-protected).

* ``GET /api/v1/network/status`` — per-provider circuit breaker states
  (closed/open/half_open), the consecutive-failure counter, threshold/cooldown
  and the current network configuration (retry/timeout). Read from the
  process-wide breaker registry.

It has no side effects; it only returns the current state (for the panel/diagnostics).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from akana_server.api.deps import require_akana_bearer
from akana_server.api.services import AppServices, get_services
from akana_server.network import load_network_config
from akana_server.network.guard import global_registry

router = APIRouter(tags=["network"])


@router.get("/network/status", dependencies=[Depends(require_akana_bearer)])
def network_status(
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    """Current circuit breaker states + the current network configuration."""
    settings = services.settings
    cfg = load_network_config(settings)
    return {
        "config": {
            "max_retries": cfg.max_retries,
            "base_delay": cfg.base_delay,
            "max_delay": cfg.max_delay,
            "total_timeout": cfg.total_timeout,
            "jitter": cfg.jitter,
            "breaker_threshold": cfg.breaker_threshold,
            "breaker_cooldown": cfg.breaker_cooldown,
            "retry_enabled": cfg.retry_enabled,
            "breaker_enabled": cfg.breaker_enabled,
        },
        "breakers": global_registry().snapshot(),
    }
