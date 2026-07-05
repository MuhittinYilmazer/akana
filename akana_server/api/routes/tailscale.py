"""Tailscale REST surface — detect-and-guide status + Serve/Funnel control.

* ``GET  /system/tailscale``        (bearer): full state machine snapshot
  (installed? logged in? serve/funnel active? the https tailnet URL + guidance).
* ``POST /system/tailscale/serve``  (bearer): ``{mode}`` in ``off|serve|funnel``.

HARD SECURITY RULE — ``mode="funnel"`` exposes the instance on the PUBLIC
internet. It is REFUSED with 400 when ``settings.api_token`` is empty: an
unauthenticated instance must never be published to the world. (Serve, which is
tailnet-private, is allowed either way; the request-layer bearer guard still
applies to proxied requests.)
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request

from akana_server.api.deps import (
    _peer_is_loopback,
    request_is_proxied,
    require_akana_bearer,
)
from akana_server.api.services import AppServices, get_services
from akana_server.network import tailscale as ts

router = APIRouter(tags=["tailscale"])


@router.get("/system/tailscale", dependencies=[Depends(require_akana_bearer)])
async def tailscale_status() -> dict[str, Any]:
    """Current Tailscale state (installed / logged-in / serve / funnel + URL)."""
    return await ts.get_status()


@router.post("/system/tailscale/serve", dependencies=[Depends(require_akana_bearer)])
async def tailscale_serve(
    request: Request,
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    """Set Tailscale exposure mode for the API port: off | serve | funnel.

    ``funnel`` is refused with 400 when no ``api_token`` is configured — never
    publish an unauthenticated instance to the public internet.
    """
    settings = services.settings
    try:
        raw = await request.json()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "INVALID_JSON", "message": f"Invalid JSON body: {e}"}},
        ) from e
    mode = (raw or {}).get("mode") if isinstance(raw, dict) else None
    if mode not in ("off", "serve", "funnel"):
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "INVALID_MODE",
                    "message": 'mode must be one of "off", "serve", "funnel".',
                }
            },
        )

    if mode == "funnel" and not getattr(settings, "api_token", None):
        # Funnel = public internet. Refuse without an access token so a passer-by
        # cannot reach the vault/API. The UI shows this as a bilingual-ready line.
        raise HTTPException(
            status_code=400,
            detail={
                "error": {
                    "code": "FUNNEL_REQUIRES_TOKEN",
                    "message": (
                        "Funnel publishes this instance on the public internet. "
                        "Set an API access token (AKANA_TOKEN) before enabling "
                        "Funnel — refusing to expose an unauthenticated instance."
                    ),
                }
            },
        )

    port = int(getattr(settings, "server_port", 0) or 0)
    result = await ts.set_serve(port, mode)
    if not result.get("ok"):
        raise HTTPException(
            status_code=502,
            detail={
                "error": {
                    "code": "TAILSCALE_FAILED",
                    "message": result.get("error") or "tailscale command failed",
                    "guidance": result.get("guidance"),
                }
            },
        )
    # Return the fresh status so the panel re-renders without a second round-trip.
    status = await ts.get_status()
    status["applied_mode"] = mode
    return status


@router.get("/system/pair")
async def system_pair(
    request: Request,
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    """Phone-pairing payload for the LOCAL owner only (loopback, no proxy).

    The browser builds the phone-pair QR from ``localStorage["akana.apiToken"]``,
    which is EMPTY for a normal loopback desktop user: loopback skips bearer auth,
    so they never entered a token, and the QR silently fails even when AKANA_TOKEN
    is set. The server already knows both the token and the tailnet URL, so we
    expose a ready-to-scan ``pair_url`` here.

    SECURITY — loopback-only, and deliberately NOT guarded by ``require_akana_bearer``
    (a loopback owner has no token to send). Instead we gate INSIDE the handler and
    serve this ONLY to the trusted local owner: the DIRECT peer must be loopback and
    the request must carry no reverse-proxy / Tailscale-Serve headers. Any proxied or
    tailnet caller gets a generic 404 — never 401/403 — so the endpoint's existence
    is not even disclosed. This is critical because the response composes the raw
    AKANA_TOKEN into ``pair_url``; if it were reachable over Tailscale Serve the token
    could be exfiltrated. The raw token is NEVER returned on its own — only the
    composed ``pair_url``, and only when both a token and an https URL exist.
    """
    if request_is_proxied(request.headers) or not _peer_is_loopback(request):
        # Do not reveal that this endpoint exists to non-local callers.
        raise HTTPException(status_code=404, detail="Not Found")

    token = getattr(services.settings, "api_token", "") or ""
    status = await ts.get_status()
    https_url = status.get("https_url")
    return {
        "token_set": bool(token),
        "https_url": https_url,
        "self_dns_name": status.get("self_dns_name"),
        "serve_active": status.get("serve_active"),
        "pair_url": (
            f"{https_url}/#token={quote(token, safe='')}"
            if (token and https_url)
            else None
        ),
    }
