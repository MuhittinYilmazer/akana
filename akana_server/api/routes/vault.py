"""SecureVault API — status, masked listing, write-only CRUD, and per-key reveal.

Listing reads stay masked (``{"set": bool, "hint": "…1234"}``) and writes are
write-only (empty/None clears a key). The ``/reveal`` GETs are the one deliberate
exception: a single key's RAW value, bearer-gated and audited, so the owner can
verify what they stored in the dashboard. Two stores:

* **scalars** — keyfile API keys in ``vault/keys.json``. The masked listing also
  surfaces the system credentials in ``secret_store.ALLOWED_KEYS`` (stored in
  ``secrets.json``, also managed via ``/system/credentials``) tagged
  ``is_system_credential`` so the owner can see they are set; reveal/delete of those
  rows dual-route to the secret store.
* **fields**  — structured account credentials per ``<namespace>/<profile>``
  (e.g. ``reddit/default`` → ``{"username": ..., "password": ...}``).
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from akana_server import vault_crypto
from akana_server.api.deps import require_akana_bearer, require_akana_bearer_strict
from akana_server.api.services import AppServices, get_services
from akana_server.secret_store import ALLOWED_KEYS, load_secrets, mask_hint
from akana_server.secure_vault import (
    audit_access,
    delete_profile,
    get_scalar,
    load_fields,
    load_scalars,
    migrate_legacy,
    profile_status,
    set_fields,
    set_scalar,
    set_scalars,
    vault_summary,
)

router = APIRouter(tags=["system"])


def _mask_map(values: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Render a ``{key: value}`` map as ``{key: {set, hint}}`` — never raw."""
    return {key: {"set": True, "hint": mask_hint(value)} for key, value in sorted(values.items())}


def _merged_scalars(data_dir: Any) -> dict[str, dict[str, Any]]:
    """Masked union of keyfile scalars (``vault/keys.json``) and system credentials.

    System credentials live in ``secrets.json`` (``secret_store.ALLOWED_KEYS``) and were
    previously invisible in the vault UI because the listing only read the keyfile. They
    are merged in here — masked, tagged ``is_system_credential`` — so the owner can see
    that a provider key (Cursor/Claude/…) is set. Keyfile scalars win on a name clash;
    the result is name-sorted for a stable UI order.
    """
    merged = _mask_map(load_scalars(data_dir))
    system_secrets = load_secrets(data_dir)
    for key in sorted(ALLOWED_KEYS):
        value = system_secrets.get(key)
        if not value or key in merged:
            continue
        merged[key] = {"set": True, "hint": mask_hint(value), "is_system_credential": True}
    return dict(sorted(merged.items()))


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        raw = await request.json()
    except Exception as exc:  # noqa: BLE001 - any parse failure → 400
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "INVALID_JSON", "message": str(exc)}},
        ) from exc
    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "INVALID_BODY", "message": "The request body must be a JSON object."}},
        )
    return raw


def _coerce_patch(mapping: object, field: str) -> dict[str, str]:
    if not isinstance(mapping, dict):
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "INVALID_BODY",
                    "message": f'The body must be of the form: {{"{field}": {{"key": "value", ...}}}}',
                }
            },
        )
    patch: dict[str, str] = {}
    for key, value in mapping.items():
        if not isinstance(key, str):
            continue
        if value is None:
            patch[key] = ""
        elif isinstance(value, str):
            patch[key] = value
        # non-string values are ignored defensively (no silent type coercion)
    return patch


def _invalid_namespace(exc: ValueError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": {"code": "INVALID_NAMESPACE", "message": str(exc)}},
    )


def _invalid_request(exc: ValueError) -> HTTPException:
    # Bad namespace/profile, bad secret key name, oversize value, too many keys.
    return HTTPException(
        status_code=422,
        detail={"error": {"code": "INVALID_REQUEST", "message": str(exc)}},
    )


def _undecryptable(exc: vault_crypto.VaultUndecryptableError) -> HTTPException:
    # BUG (vault write routes): assert_writable raises VaultUndecryptableError
    # (a RuntimeError) when the on-disk blob is present but won't decrypt under the
    # current master key. Uncaught it escaped the async handler as an opaque HTTP 500
    # with a stack trace, so the dashboard could not tell "wrong master key, refusing
    # to write" from a server crash. Map it to a clean 409 (a conflict with the
    # existing, still-encrypted state — the fail-closed STOP is deliberate).
    return HTTPException(
        status_code=409,
        detail={
            "error": {
                "code": "VAULT_UNDECRYPTABLE",
                "message": (
                    "wrong or corrupt master key; refusing to overwrite the vault "
                    "(to prevent secret loss)."
                ),
            }
        },
    )


@router.get("/system/vault", dependencies=[Depends(require_akana_bearer)])
async def get_vault_status(
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    settings = services.settings
    summary = vault_summary(settings.data_dir)
    summary["encryption"] = vault_crypto.health()
    return summary


@router.post(
    "/system/vault/migrate/{namespace}",
    dependencies=[Depends(require_akana_bearer)],
)
async def post_vault_migrate(
    namespace: str,
    profile: str = "default",
    force: bool = False,
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    settings = services.settings
    try:
        # BUG (loop-freeze): migrate_legacy runs synchronous shutil.copytree/copy2 and
        # takes the cross-process vault file_lock — offload it so the event loop keeps
        # serving other requests while it blocks.
        return await asyncio.to_thread(
            migrate_legacy, settings.data_dir, namespace, profile, force=force
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "INVALID_NAMESPACE", "message": str(exc)}},
        ) from exc


@router.get(
    "/system/vault/{namespace}/{profile}",
    dependencies=[Depends(require_akana_bearer)],
)
async def get_vault_profile(
    namespace: str,
    profile: str,
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    settings = services.settings
    try:
        return profile_status(settings.data_dir, namespace, profile)
    except ValueError as exc:
        raise _invalid_namespace(exc) from exc


# ── scalars (vault/keys.json) ─────────────────────────────────────────────────


@router.get("/system/vault/scalars", dependencies=[Depends(require_akana_bearer)])
async def get_vault_scalars(
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    settings = services.settings
    return {"scalars": _merged_scalars(settings.data_dir)}


@router.put("/system/vault/scalars", dependencies=[Depends(require_akana_bearer)])
async def put_vault_scalars(
    request: Request, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    settings = services.settings
    body = await _json_object(request)
    patch = _coerce_patch(body.get("scalars"), "scalars")
    try:
        # BUG (loop-freeze): set_scalars enters the blocking cross-process file_lock and
        # does synchronous Fernet encrypt + disk write — offload it off the event loop.
        state = await asyncio.to_thread(set_scalars, settings.data_dir, patch)
    except ValueError as exc:
        raise _invalid_request(exc) from exc
    except vault_crypto.VaultUndecryptableError as exc:
        raise _undecryptable(exc) from exc
    return {"scalars": _mask_map(state)}


@router.delete("/system/vault/scalars/{key}", dependencies=[Depends(require_akana_bearer)])
async def delete_vault_scalar(
    key: str, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    settings = services.settings
    try:
        # BUG (loop-freeze): offload the blocking, lock-holding write off the loop.
        # A system credential (Cursor/Claude/…) can live in secrets.json (written via
        # /system/credentials) AND/OR the keyfile (the raw /scalars PUT writes there),
        # so clear BOTH stores — else the row would survive in the store we skipped.
        # Keyfile FIRST: set_scalars asserts writability, so a wrong/corrupt master key
        # raises here (→ 409) before any partial change.
        await asyncio.to_thread(set_scalars, settings.data_dir, {key: ""})
        if key in ALLOWED_KEYS:
            await asyncio.to_thread(set_scalar, settings.data_dir, key, "")
    except ValueError as exc:
        raise _invalid_request(exc) from exc
    except vault_crypto.VaultUndecryptableError as exc:
        # BUG: was previously uncaught here → HTTP 500 on a wrong/corrupt master key.
        raise _undecryptable(exc) from exc
    return {"scalars": _merged_scalars(settings.data_dir)}


@router.get(
    "/system/vault/scalars/{key}/reveal",
    # Raw-secret reveal: bearer required even on loopback when a token is set, so another
    # local OS user can't read plaintext secrets (see require_akana_bearer_strict).
    dependencies=[Depends(require_akana_bearer_strict)],
)
async def reveal_vault_scalar(
    key: str, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    """Return the RAW value of ONE scalar — a deliberate, audited owner reveal.

    The masked listing merges keyfile scalars (``load_scalars`` → ``vault/keys.json``)
    with the system credentials in ``ALLOWED_KEYS`` (``secrets.json``). A key can live in
    EITHER store, so this reveal checks the keyfile first and, for an ``ALLOWED_KEYS``
    name, falls back to the secret store (``get_scalar``) — the button reveals exactly the
    row shown. Writes a single ``reveal_scalar`` audit entry for the key actually shown.
    """
    settings = services.settings
    value = load_scalars(settings.data_dir).get(key)
    if not value and key in ALLOWED_KEYS:
        value = get_scalar(settings.data_dir, key, consumer="dashboard:reveal")
    if not value:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": f"No secret named '{key}'."}},
        )
    audit_access(
        settings.data_dir,
        {"action": "reveal_scalar", "key": key, "consumer": "dashboard"},
    )
    return {"key": key, "value": value}


# ── structured fields (credentials/<namespace>/<profile>/secrets.enc) ─────────


@router.get(
    "/system/vault/{namespace}/{profile}/fields",
    dependencies=[Depends(require_akana_bearer)],
)
async def get_vault_fields(
    namespace: str, profile: str, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    settings = services.settings
    try:
        fields = load_fields(
            settings.data_dir, namespace, profile, consumer="dashboard", audit=False
        )
    except ValueError as exc:
        raise _invalid_namespace(exc) from exc
    return {"namespace": namespace, "profile": profile, "fields": _mask_map(fields)}


@router.put(
    "/system/vault/{namespace}/{profile}/fields",
    dependencies=[Depends(require_akana_bearer)],
)
async def put_vault_fields(
    namespace: str, profile: str, request: Request, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    settings = services.settings
    body = await _json_object(request)
    patch = _coerce_patch(body.get("fields"), "fields")
    try:
        # BUG (loop-freeze): set_fields enters the blocking cross-process file_lock and
        # does synchronous Fernet encrypt + disk write — offload it off the event loop.
        state = await asyncio.to_thread(
            set_fields, settings.data_dir, namespace, patch, profile
        )
    except ValueError as exc:
        raise _invalid_request(exc) from exc
    except vault_crypto.VaultUndecryptableError as exc:
        raise _undecryptable(exc) from exc
    return {"namespace": namespace, "profile": profile, "fields": _mask_map(state)}


@router.delete(
    "/system/vault/{namespace}/{profile}/fields/{key}",
    dependencies=[Depends(require_akana_bearer)],
)
async def delete_vault_field(
    namespace: str, profile: str, key: str, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    settings = services.settings
    try:
        # BUG (loop-freeze): offload the blocking, lock-holding set_fields off the loop.
        state = await asyncio.to_thread(
            set_fields, settings.data_dir, namespace, {key: ""}, profile
        )
    except ValueError as exc:
        raise _invalid_namespace(exc) from exc
    except vault_crypto.VaultUndecryptableError as exc:
        raise _undecryptable(exc) from exc
    return {"namespace": namespace, "profile": profile, "fields": _mask_map(state)}


@router.get(
    "/system/vault/{namespace}/{profile}/fields/{key}/reveal",
    # Raw-secret reveal: bearer required even on loopback when a token is set, so another
    # local OS user can't read plaintext credential fields (see require_akana_bearer_strict).
    dependencies=[Depends(require_akana_bearer_strict)],
)
async def reveal_vault_field(
    namespace: str, profile: str, key: str, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    """Return the RAW value of ONE credential field — deliberate, audited owner reveal.

    Loads with ``audit=False`` (avoid logging every sibling key) and writes a single
    ``reveal_field`` audit entry for just the field that was actually shown.
    """
    settings = services.settings
    try:
        fields = load_fields(
            settings.data_dir, namespace, profile, consumer="dashboard:reveal", audit=False
        )
    except ValueError as exc:
        raise _invalid_namespace(exc) from exc
    if key not in fields:
        raise HTTPException(
            status_code=404,
            detail={
                "error": {
                    "code": "NOT_FOUND",
                    "message": f"No field '{key}' in {namespace}/{profile}.",
                }
            },
        )
    audit_access(
        settings.data_dir,
        {
            "action": "reveal_field",
            "namespace": namespace,
            "profile": profile,
            "field": key,
            "consumer": "dashboard",
        },
    )
    return {"namespace": namespace, "profile": profile, "key": key, "value": fields[key]}


@router.delete(
    "/system/vault/{namespace}/{profile}",
    dependencies=[Depends(require_akana_bearer)],
)
async def delete_vault_profile(
    namespace: str, profile: str, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    """Permanently delete the whole ``<namespace>/<profile>`` credential profile (rmtree)."""
    settings = services.settings
    try:
        # BUG (loop-freeze): delete_profile runs synchronous shutil.rmtree and takes the
        # cross-process vault file_lock — offload it so the event loop keeps serving.
        return await asyncio.to_thread(
            delete_profile, settings.data_dir, namespace, profile
        )
    except ValueError as exc:
        raise _invalid_namespace(exc) from exc
