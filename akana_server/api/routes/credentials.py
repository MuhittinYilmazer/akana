"""Runtime credentials API — masked GET/PUT plus a per-key audited reveal for the
secret-store whitelist (cursor_api_key, claude_oauth_token, gemini_api_key,
openai_api_key, telegram_bot_token).

Listing and PUT stay masked (responses carry only ``set`` flags and ``…last4``
hints). The ``/{key}/reveal`` GET is the one deliberate exception: a single
credential's RAW value, bearer-gated and audited, so the dashboard owner can see
what the model already reads through the vault MCP (``vault_get`` dual-routes
``ALLOWED_KEYS`` to this store). Registered in ``app.py`` under the ``/api/v1``
prefix, so the routes are live at ``/api/v1/system/credentials`` (GET/PUT) and
``/api/v1/system/credentials/{key}/reveal`` (GET), all bearer-protected.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from akana_server.api.deps import require_akana_bearer, require_akana_bearer_strict
from akana_server.api.services import AppServices, get_services
from akana_server.config import Settings, clean_secret_value
from akana_server.secret_store import (
    ALLOWED_KEYS,
    is_real_secret,
    load_secrets,
    mask_hint,
    set_secrets,
)
from akana_server.secure_vault import audit_access

router = APIRouter(tags=["system"])


def _masked_payload(secrets: dict[str, str], settings: Settings) -> dict[str, Any]:
    credentials: dict[str, Any] = {}
    for key in sorted(ALLOWED_KEYS):
        # "Set" if the runtime store OR the environment (Settings) has a REAL value —
        # store wins, mirroring the orchestrator's store→env resolution. ``is_real_secret``
        # (not ``bool``) is the gate so a leftover ``.env.example`` placeholder
        # (``your-cursor-api-key-here``) reports as UNSET; otherwise the badge claimed
        # "configured", the user never set a real key, and chat hung on an invalid bearer.
        stored = secrets.get(key)
        env_val = getattr(settings, key, None)
        value = stored or env_val
        real = is_real_secret(value)
        # Single source of truth, surfaced for the Settings panel: which layer the
        # effective value came from (runtime store overrides .env).
        source = ("store" if is_real_secret(stored) else "env") if real else None
        credentials[key] = {
            "set": real,
            "hint": mask_hint(value) if real else None,
            "source": source,
        }
    return {"credentials": credentials}


def _parse_body(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError('JSON body must be an object, e.g. {"cursor_api_key":"..."}')
    patch = {k: raw[k] for k in ALLOWED_KEYS if k in raw}
    for key, value in patch.items():
        # Empty/None clears the key (allowed). A non-string value (number/bool/object) is
        # NOT a clear — set_secrets would coerce it to "" and silently DELETE a working key.
        # Reject it as a 422 (only null/empty may clear), mirroring vault._coerce_patch's
        # "no silent type coercion".
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(
                f"{key!r} must be a string (send an empty string or null to clear it)."
            )
        # A non-empty value that fails the real-secret gate is a placeholder or truncated
        # paste — reject it now so the user fixes it, instead of "saving" a key that
        # silently never authenticates.
        if clean_secret_value(value) and not is_real_secret(value):
            raise ValueError(
                f"{key!r} looks like a placeholder or is too short — paste the real "
                "value, or send an empty string to clear it."
            )
    return patch


@router.get("/system/credentials", dependencies=[Depends(require_akana_bearer)])
async def get_credentials(
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    settings = services.settings
    return _masked_payload(load_secrets(settings.data_dir), settings)


@router.put("/system/credentials", dependencies=[Depends(require_akana_bearer)])
async def put_credentials(
    request: Request, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    settings = services.settings
    try:
        raw = await request.json()
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "INVALID_JSON", "message": str(e)}},
        ) from e
    try:
        patch = _parse_body(raw)
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "INVALID_BODY", "message": str(e)}},
        ) from e
    # On a provider key change, drop that provider's model-catalog cache so the dashboard
    # lists models with the NEW key immediately. Each catalog also auto-refreshes via a
    # key fingerprint; the explicit invalidation just avoids the stale-until-TTL window.
    if "cursor_api_key" in patch:
        from akana_server.orchestrator.cursor_catalog import invalidate_cursor_catalog_cache

        invalidate_cursor_catalog_cache()
    if "gemini_api_key" in patch:
        from akana_server.orchestrator.gemini_catalog import invalidate_gemini_catalog_cache

        invalidate_gemini_catalog_cache()
    if "openai_api_key" in patch:
        from akana_server.orchestrator.openai_catalog import invalidate_openai_catalog_cache

        invalidate_openai_catalog_cache()
    if "claude_oauth_token" in patch:
        from akana_server.orchestrator.claude_catalog import invalidate_claude_catalog_cache

        invalidate_claude_catalog_cache()
    return _masked_payload(set_secrets(settings.data_dir, patch), settings)


@router.get(
    "/system/credentials/{key}/reveal",
    # Raw-secret reveal: bearer required even on loopback when a token is configured, so
    # another local OS user can't read plaintext credentials (see require_akana_bearer_strict).
    dependencies=[Depends(require_akana_bearer_strict)],
)
async def reveal_credential(
    key: str, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    """Return the RAW value of ONE provider credential — a deliberate, audited owner reveal.

    The model can already read these through the vault MCP (``vault_get`` dual-routes
    ``ALLOWED_KEYS`` to ``secret_store``); this gives the dashboard owner the same
    visibility. Resolves the effective value (runtime store → ``.env``/Settings) so the
    revealed value matches the masked hint shown in the listing, and writes a single
    ``reveal_credential`` audit entry for the key actually shown.
    """
    settings = services.settings
    if key not in ALLOWED_KEYS:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": f"Unknown credential '{key}'."}},
        )
    value = load_secrets(settings.data_dir).get(key) or getattr(settings, key, None)
    if not value:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": f"'{key}' is not set."}},
        )
    audit_access(
        settings.data_dir,
        {"action": "reveal_credential", "key": key, "consumer": "dashboard"},
    )
    return {"key": key, "value": value}
