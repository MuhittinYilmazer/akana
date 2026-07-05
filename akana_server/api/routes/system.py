"""System status helpers — audit tail only.

Active model + history depth now live under /system/llm-settings; the legacy
local/claude chat-profile picker was removed with the OpenClaw gateway.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from akana_server.api.deps import require_akana_bearer
from akana_server.api.services import AppServices, get_services
from akana_server.audit import read_tail as audit_read_tail
from akana_server.observability import registry

router = APIRouter(tags=["system"])


@router.get("/system/audit/tail", dependencies=[Depends(require_akana_bearer)])
async def get_audit_tail(
    limit: int = Query(default=100, ge=1, le=1000),
    kind: str | None = Query(default=None, max_length=64),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    events = audit_read_tail(services.settings.data_dir, limit=limit)
    if kind:
        events = [e for e in events if e.get("kind") == kind]
    return {"count": len(events), "events": events}


@router.get("/system/metrics", dependencies=[Depends(require_akana_bearer)])
async def get_metrics() -> dict[str, Any]:
    """In-process metrics snapshot — operational visibility.

    counters: ``llm_errors`` / ``llm_timeout_fires`` / ``queue_depth`` (gauge);
    timers: ``turn_latency_ms``. In-process (reset on restart), not a persistent
    TSDB — for the "what's happening right now" question. The writing paths feed
    ``observability.registry`` (the chat turn path + streaming).
    """
    return registry.snapshot()
