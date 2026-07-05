"""Runtime settings REST surface — bearer-protected (the RuntimeSettings layer).

* ``GET  /api/v1/settings/runtime``             — schema + values + source
  (``runtime`` | ``env`` | ``default``); the UI form is generated from this payload.
* ``PUT  /api/v1/settings/runtime``             — per-field validation (Turkish
  error, applied only if all fields are valid); the response carries ``changed`` +
  ``restart_required`` lists.
* ``POST /api/v1/settings/runtime/reset/{key}`` — return a key to env/default.

Cache invalidation after a write: since the search service / image store / file
service are built lazily on ``app.state``, the relevant one is dropped when its
key changes — the next request rebuilds it with the runtime value.

Also, since restart-free keys with a ``settings_attr`` (such as wake_threshold)
are applied to the FROZEN ``app.state.settings`` snapshot, after a write this
snapshot is freshly built via :func:`rebuild_app_settings` — so a live consumer
(voice/wake.py + /voice/config) reads the new value without a restart.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, Request

from akana_server.api.deps import require_akana_bearer
from akana_server.api.errors import http_error
from akana_server.api.services import AppServices, get_services
from akana_server.llm_settings import load_llm_settings
from akana_server.runtime_settings import (
    SCHEMA,
    RuntimeSettingError,
    get_store,
    rebuild_app_settings,
    runtime_payload,
    validate_value,
)
from akana_server.voice_preferences import load_voice_preferences

router = APIRouter(tags=["settings"])

#: Keys that, when changed, drop the lazily-built service on app.state.
_STATE_INVALIDATION: dict[str, tuple[str, ...]] = {
    "image_store": ("upload_max_mb",),
    "file_service": ("file_roots",),
}


def _invalidate_state(request: Request, changed: list[str]) -> None:
    changed_set = set(changed)
    for attr, keys in _STATE_INVALIDATION.items():
        if changed_set & set(keys) and hasattr(request.app.state, attr):
            setattr(request.app.state, attr, None)


@router.get("/settings/runtime", dependencies=[Depends(require_akana_bearer)])
async def get_runtime_settings(
    include_hidden: bool = False,
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    # ``include_hidden`` lets a dedicated control read a setting that is hidden from
    # the editable form — e.g. the i18n boot reconcile reads ``language`` (hidden
    # because the Overview tab owns its picker). The form's own GET omits the param,
    # so it stays decluttered.
    return runtime_payload(services.settings, include_hidden=include_hidden)


#: Secret fields in Settings — the value is NEVER returned, only a set/unset boolean.
_SECRET_FIELDS = (
    "api_token",
    "cursor_api_key",
    "telegram_bot_token",
)


@router.get("/settings/effective", dependencies=[Depends(require_akana_bearer)])
async def get_effective_settings(
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    """Show the entire config in ONE window, resolved (read-only, for debuggability).

    The config was spread across 4 sources (env/``config.py`` + ``llm_settings.json``
    + ``voice_preferences.json`` + ``runtime_settings.json``) and read from separate
    endpoints. This endpoint unifies them all — for the "see the config in one place"
    need. **Secrets are NOT returned as values**, only a set/unset boolean under
    ``secrets_set`` (no leak even though it's bearer-protected).
    """
    s = services.settings
    return {
        "server": {
            "host": s.server_host,
            "port": s.server_port,
            "data_dir": str(s.data_dir),
            "workspace": str(s.workspace),
            "log_level": s.log_level,
            "llm_provider": s.llm_provider,
            "cursor_model": s.cursor_model,
            "claude_model": s.claude_model,
            "claude_bin": s.claude_bin,
            "bridge_timeout": s.bridge_timeout,
            "claude_bridge_timeout": s.claude_bridge_timeout,
        },
        "secrets_set": {
            name: bool((getattr(s, name) or "") and str(getattr(s, name)).strip())
            for name in _SECRET_FIELDS
        },
        "llm": asdict(load_llm_settings(s.data_dir, s)),
        "voice": asdict(load_voice_preferences(s.data_dir)),
        # Debug "everything in one place" view → include hidden specs (e.g. wake_threshold,
        # which is hidden from the editable form but still part of the resolved config).
        "runtime": runtime_payload(s, include_hidden=True),
    }


@router.put("/settings/runtime", dependencies=[Depends(require_akana_bearer)])
async def put_runtime_settings(
    request: Request, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    settings = services.settings
    try:
        raw = await request.json()
    except Exception as e:
        raise http_error(400, "INVALID_JSON", f"Invalid JSON body: {e}") from e
    src = raw.get("settings") if isinstance(raw, dict) and isinstance(raw.get("settings"), dict) else raw
    if not isinstance(src, dict) or not src:
        raise http_error(
            422,
            "INVALID_BODY",
            'The body must be a key→value object, e.g. {"wake_threshold": 0.2}',
        )

    validated: dict[str, Any] = {}
    field_errors: dict[str, str] = {}
    for key, value in src.items():
        spec = SCHEMA.get(str(key))
        if spec is None:
            field_errors[str(key)] = "Unknown setting — not in the runtime allowlist."
            continue
        try:
            validated[spec.key] = validate_value(spec, value)
        except RuntimeSettingError as e:
            field_errors[spec.key] = str(e)
    if field_errors:
        raise http_error(
            422,
            "VALIDATION",
            "Some fields could not be validated; no changes were applied.",
            fields=field_errors,
        )

    store = get_store(settings.data_dir)
    try:
        # Atomic multi-key write: a per-key loop could persist the first keys and
        # then fail on a later one (disk full / read-only data_dir), leaving a
        # partial subset written while the client sees the request "fail". set_many
        # applies all-or-none; a storage failure surfaces as the canonical error
        # envelope instead of a bare, contract-breaking 500.
        store.set_many(validated)
    except OSError as e:
        raise http_error(
            500,
            "PERSIST_FAILED",
            "Settings could not be saved due to a storage error; no changes were applied.",
        ) from e
    changed = list(validated)
    _invalidate_state(request, changed)
    # Since restart-free ``settings_attr`` keys (such as wake_threshold) are applied
    # to the FROZEN app.state.settings, refresh the live snapshot — so the next
    # request (voice/wake score, /voice/config) reads the new value without a restart.
    rebuilt = rebuild_app_settings(request.app)
    if rebuilt is not None:
        settings = rebuilt
    payload = runtime_payload(settings)
    payload["changed"] = changed
    payload["restart_required"] = [k for k in changed if SCHEMA[k].restart_required]
    return payload


@router.post(
    "/settings/runtime/reset/{key}",
    dependencies=[Depends(require_akana_bearer)],
)
async def reset_runtime_setting(
    request: Request, key: str, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    settings = services.settings
    spec = SCHEMA.get(key)
    if spec is None:
        raise http_error(404, "UNKNOWN_KEY", f"Unknown runtime setting: {key}")
    removed = get_store(settings.data_dir).reset(spec.key)
    _invalidate_state(request, [spec.key])
    # After reset, refresh the live snapshot: a fresh load_settings() base +
    # current store overrides → the ``settings_attr`` key returns to env/default.
    rebuilt = rebuild_app_settings(request.app)
    if rebuilt is not None:
        settings = rebuilt
    payload = runtime_payload(settings)
    payload["reset"] = spec.key
    payload["removed"] = removed
    payload["restart_required"] = [spec.key] if (removed and spec.restart_required) else []
    return payload
