"""Pack management API (lifecycle subset) — bearer-protected.

- ``GET  /packs``            — list loaded packs (id/title/version/state/contents)
- ``POST /packs/enable``     — enable a pack by id (hot-reload; body ``{pack_id}``)
- ``POST /packs/disable``    — disable a pack by id (hot-reload; body ``{pack_id}``)
- ``POST /packs/rescan``     — reconcile loaded packs with ``packs/`` (add new, hot-delete vanished)
- ``GET  /packs/consents``   — per-pack MCP consent state (pending vs. mounted); ``?pack_id=`` narrows
- ``POST /packs/consent``    — human-approved MCP mount (body ``{pack_id, server_configs?}``)
- ``POST /packs/consent/revoke`` — withdraw a pack's mounted MCP entries (body ``{pack_id}``)

Enable/disable hot-reload the pack's content (skills/personas); disable also
removes the pack's skills from the capability catalog and persona list (both
derive from the registries). The pack's source directory is never touched, and
the disabled set is persisted to ``data_dir/packs_state.json``.

MCP mounting is separate from enable: enabling a pack only *declares + probes* its
external tools. Writing a pack's MCP server into ``mcp_servers.yaml`` happens ONLY
through ``POST /packs/consent`` — the bearer-protected, human-in-the-loop gate
(PACK_INTERFACE.md §5.1). Merely declaring a tool never self-mounts it.

``pack_id`` is carried in the request BODY (not the path) because it has the
``namespace/name`` form — a slash that a path parameter would split.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from akana_server.api.deps import require_akana_bearer
from akana_server.api.errors import http_error
from akana_server.api.services import AppServices, get_services
from akana_server.packs.host import UnknownPackError

router = APIRouter(tags=["packs"])


def _require_host(services: AppServices) -> Any:
    host = services.pack_host
    if host is None:
        raise http_error(503, "PACK_HOST_UNAVAILABLE", "pack host not initialized")
    return host


class PackIdRequest(BaseModel):
    pack_id: str = Field(..., min_length=1, max_length=128)


class PackConsentRequest(BaseModel):
    pack_id: str = Field(..., min_length=1, max_length=128)
    #: Optional per-server config overrides (``name -> {command|url, ...}``). When
    #: absent, the server's ``mcp`` block from the manifest is used; a server with
    #: no config lands in ``needs_config`` and is never fabricated.
    server_configs: dict[str, dict[str, Any]] | None = None


@router.get("/packs", dependencies=[Depends(require_akana_bearer)])
async def list_packs(services: AppServices = Depends(get_services)) -> dict[str, Any]:
    host = _require_host(services)
    items = host.list_views()
    return {"count": len(items), "packs": items}


@router.post("/packs/enable", dependencies=[Depends(require_akana_bearer)])
async def enable_pack(
    body: PackIdRequest, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    host = _require_host(services)
    try:
        host.enable(body.pack_id)
    except UnknownPackError:
        raise http_error(
            404, "PACK_NOT_FOUND", f"pack not found: {body.pack_id}"
        ) from None
    return {"pack": host.pack_view(body.pack_id)}


@router.post("/packs/disable", dependencies=[Depends(require_akana_bearer)])
async def disable_pack(
    body: PackIdRequest, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    host = _require_host(services)
    try:
        host.disable(body.pack_id)
    except UnknownPackError:
        raise http_error(
            404, "PACK_NOT_FOUND", f"pack not found: {body.pack_id}"
        ) from None
    return {"pack": host.pack_view(body.pack_id)}


@router.post("/packs/rescan", dependencies=[Depends(require_akana_bearer)])
async def rescan_packs(services: AppServices = Depends(get_services)) -> dict[str, Any]:
    host = _require_host(services)
    delta = host.rescan()
    items = host.list_views()
    return {
        "added": delta["added"],
        "removed": delta["removed"],
        "count": len(items),
        "packs": items,
    }


# --------------------------------------------------------------------------- #
# MCP consent (the human-in-the-loop gate — PACK_INTERFACE.md §5.1)           #
# --------------------------------------------------------------------------- #


@router.get("/packs/consents", dependencies=[Depends(require_akana_bearer)])
async def list_pack_consents(
    pack_id: str | None = None, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    """Per-pack MCP consent state: which declared servers are ``pending`` vs. ``mounted``.

    ``?pack_id=`` narrows to one pack. Packs that declare no MCP servers are
    omitted. Reaching this route does not mount anything.
    """
    host = _require_host(services)
    items = host.consent_view(pack_id)
    return {"count": len(items), "consents": items}


@router.post("/packs/consent", dependencies=[Depends(require_akana_bearer)])
async def grant_pack_consent(
    body: PackConsentRequest, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    """Human-approved MCP mount for a pack's declared servers (the sole write path).

    Idempotent: adds ``managed_by``-stamped entries to ``mcp_servers.yaml`` and
    never overwrites entries the user placed by hand (those come back in
    ``conflicts``). Servers with no usable config land in ``needs_config`` and are
    never fabricated.
    """
    host = _require_host(services)
    try:
        result = host.grant_consent(body.pack_id, server_configs=body.server_configs)
    except UnknownPackError:
        raise http_error(
            404, "PACK_NOT_FOUND", f"pack not found: {body.pack_id}"
        ) from None
    return {"pack_id": body.pack_id, "result": result}


@router.post("/packs/consent/revoke", dependencies=[Depends(require_akana_bearer)])
async def revoke_pack_consent(
    body: PackIdRequest, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    """Withdraw the MCP entries mounted on behalf of a pack (idempotent)."""
    host = _require_host(services)
    try:
        removed = host.revoke_consent(body.pack_id)
    except UnknownPackError:
        raise http_error(
            404, "PACK_NOT_FOUND", f"pack not found: {body.pack_id}"
        ) from None
    return {"pack_id": body.pack_id, "removed": removed}
