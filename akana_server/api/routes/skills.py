"""Skill registry API (PR-T2 / PR-T2b + SkillEngine F0-F2).

- ``GET /skills`` — L1 list; ``q=`` enables hybrid search (substring + FTS5 +
  optional vector, RRF fusion — ``skills/retrieval.py``), with ``type=``/``source=``
  filters. The response schema is the same as F0/F1 (the cmdk palette consumes it);
  only ``match_reason`` now carries the layer info (e.g. ``"title+fts"``).
- ``GET /skills/{skill_id}`` — a single skill's detail; ``include_body=true`` loads the L2 body
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from akana_server.api.deps import require_akana_bearer
from akana_server.api.errors import http_error
from akana_server.api.services import AppServices, get_services
from akana_server.skills.registry import get_registry, reload_skills

router = APIRouter(tags=["skills"])


def _registry(services: AppServices):
    return get_registry(services.settings.data_dir)


@router.get("/skills", dependencies=[Depends(require_akana_bearer)])
async def get_skills(
    reload: bool = False,
    q: str | None = Query(default=None, description="Hybrid search (substring+FTS5+vector)"),
    type_filter: str | None = Query(default=None, alias="type"),
    source_filter: str | None = Query(default=None, alias="source"),
    top_k: int = Query(default=10, ge=1, le=50),
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    if reload:
        reload_skills()
    reg = _registry(services)
    if q:
        results = [
            s.to_dict()
            for s in reg.search(q, top_k=top_k)
            if (not type_filter or s.entry.type == type_filter)
            and (not source_filter or s.entry.source == source_filter)
        ]
        return {"count": len(results), "skills": results, "query": q}
    items = [e.to_dict() for e in reg.list(type_filter=type_filter, source_filter=source_filter)]
    payload: dict[str, Any] = {"count": len(items), "skills": items}
    if reg.errors:
        payload["errors"] = reg.errors
    return payload


@router.get("/skills/{skill_id}", dependencies=[Depends(require_akana_bearer)])
async def get_skill_detail(
    skill_id: str,
    include_body: bool = False,
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    reg = _registry(services)
    entry = reg.get(skill_id)
    if entry is None:
        raise http_error(404, "SKILL_NOT_FOUND", f"skill not found: {skill_id}")
    data = entry.to_dict()
    data["resources"] = reg.list_resources(skill_id)  # L3 names (content not loaded)
    if include_body:
        data["body"] = reg.load_body(skill_id)  # L2 on-demand
    data["body_loaded"] = reg.body_loaded(skill_id)
    return {"skill": data}
