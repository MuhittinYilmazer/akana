"""Tool gateway debug endpoints (PR-T1)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from akana_server.api.deps import require_akana_bearer
from akana_server.tools.gateway import list_recent_tool_calls

router = APIRouter(tags=["tools"])


@router.get("/tools/recent", dependencies=[Depends(require_akana_bearer)])
async def get_tools_recent(
    limit: int = Query(default=20, ge=1, le=100),
) -> dict[str, Any]:
    items = list_recent_tool_calls(limit=limit)
    return {"count": len(items), "tools": items}
