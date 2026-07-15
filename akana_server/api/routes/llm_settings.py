"""Dashboard LLM settings API — Cursor model picker + chat history depth."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from akana_server.api.deps import require_akana_bearer
from akana_server.api.errors import http_error
from akana_server.api.services import AppServices, get_services
from akana_server.llm_settings import (
    _VALID_PROVIDERS,
    load_llm_settings,
    public_llm_payload,
    resolve_cursor_model_tag,
    update_llm_settings,
)

router = APIRouter(tags=["system"])

_ALLOWED_KEYS = frozenset(
    {
        "cursor_model",
        "chat_max_turns",
        "tts_lang",
        # NOTE: wake_threshold was REMOVED from here — the single source of truth
        # is runtime_settings (PUT /api/v1/settings/runtime). If accepted here it
        # would be written to llm_settings.json but never read (a silent dead copy).
        "provider",
        "claude_model",
        "ollama_model",
        "gemini_model",
        "openai_model",
        "codex_model",
        "claude_full_tools",
    }
)


def _parse_body(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError('JSON body must be an object, e.g. {"cursor_model":"composer-2"}')
    nested = raw.get("settings")
    src = nested if isinstance(nested, dict) else raw
    return {k: src[k] for k in _ALLOWED_KEYS if k in src}


@router.get("/system/llm-settings", dependencies=[Depends(require_akana_bearer)])
async def get_llm_settings(
    request: Request, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    settings = services.settings
    llm = getattr(request.app.state, "llm_settings", None) or load_llm_settings(
        settings.data_dir, settings
    )
    return public_llm_payload(
        llm, settings=settings, active_tag=resolve_cursor_model_tag(settings, llm)
    )


@router.get("/system/ollama/models", dependencies=[Depends(require_akana_bearer)])
async def get_ollama_models(
    request: Request, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    """Installed Ollama models (live ``/api/tags``) + the active selection.

    If the daemon is down/unreachable, ``reachable=false`` + an empty list (NEVER
    500 — the UI shows 'Ollama not running' and guides the user). The switcher
    calls this ONLY when Ollama is selected → no unnecessary request to Ollama on
    every settings load.
    """
    from akana.driver.ollama import OllamaDriver
    from akana_server.llm_settings import resolve_ollama_model_tag

    settings = services.settings
    llm = getattr(request.app.state, "llm_settings", None) or load_llm_settings(
        settings.data_dir, settings
    )
    url = getattr(settings, "ollama_url", None) or "http://localhost:11434"
    active = resolve_ollama_model_tag(settings, llm)
    try:
        models = await OllamaDriver(url=url).list_models()
        return {"reachable": True, "url": url, "models": models, "active": active}
    except Exception as exc:  # noqa: BLE001 - probe: any error → reachable:false (not 500)
        return {"reachable": False, "url": url, "models": [], "active": active, "error": str(exc)}


@router.get("/system/cursor/models", dependencies=[Depends(require_akana_bearer)])
async def get_cursor_models(
    request: Request,
    services: AppServices = Depends(get_services),
    refresh: bool = Query(False, description="Skip the cache, make a live Cursor API call"),
) -> dict[str, Any]:
    """Models on the Cursor account (live ``Cursor.models.list``) + the active selection.

    If the API is unreachable, ``reachable=false`` + a static fallback list (NEVER
    500 — the UI shows a clear error). The result is cached for 10 min; ``?refresh=1``
    forces a refresh.
    """
    from akana_server.orchestrator.cursor_catalog import fetch_cursor_models

    return await fetch_cursor_models(services.settings, force_refresh=refresh)


@router.get("/system/claude/models", dependencies=[Depends(require_akana_bearer)])
async def get_claude_models(
    request: Request,
    services: AppServices = Depends(get_services),
    refresh: bool = Query(False, description="Skip the cache, make a live Anthropic API call"),
) -> dict[str, Any]:
    """Models on the Claude subscription (live ``/v1/models``) + the active selection.

    Fetched with the subscription OAuth token (``~/.claude/.credentials.json``); if
    the token is missing/stale, ``reachable=false`` + a static fallback list (NEVER
    500). The result is cached for 10 min; ``?refresh=1`` forces a refresh
    (symmetric with the cursor models).
    """
    from akana_server.orchestrator.claude_catalog import fetch_claude_models

    return await fetch_claude_models(services.settings, force_refresh=refresh)


@router.get("/system/gemini/models", dependencies=[Depends(require_akana_bearer)])
async def get_gemini_models(
    request: Request,
    services: AppServices = Depends(get_services),
    refresh: bool = Query(False, description="Skip the cache, make a live Google API call"),
) -> dict[str, Any]:
    """LIVE models on the Gemini account (``client.models.list``) + the active selection.

    If google-genai isn't installed / the key is missing / the API is unreachable,
    ``reachable=false`` + a static fallback list (NEVER 500). The result is cached
    for 10 min; ``?refresh=1`` forces a refresh (symmetric with cursor/claude). The
    switcher calls this ONLY when Gemini is selected.
    """
    from akana_server.orchestrator.gemini_catalog import fetch_gemini_models

    return await fetch_gemini_models(services.settings, force_refresh=refresh)


@router.get("/system/openai/models", dependencies=[Depends(require_akana_bearer)])
async def get_openai_models(
    request: Request,
    services: AppServices = Depends(get_services),
    refresh: bool = Query(False, description="Skip the cache, make a live OpenAI API call"),
) -> dict[str, Any]:
    """LIVE models on the OpenAI account (``/v1/models``) + the active selection.

    If the key is missing / the API is unreachable, ``reachable=false`` + a static
    fallback list (NEVER 500). The result is cached for 10 min; ``?refresh=1`` forces
    a refresh (symmetric with gemini/claude). The switcher calls this ONLY when
    OpenAI is selected.
    """
    from akana_server.orchestrator.openai_catalog import fetch_openai_models

    return await fetch_openai_models(services.settings, force_refresh=refresh)


@router.get("/system/codex/models", dependencies=[Depends(require_akana_bearer)])
async def get_codex_models(
    request: Request,
    services: AppServices = Depends(get_services),
    refresh: bool = Query(False, description="No-op for codex (static catalog); accepted for symmetry"),
) -> dict[str, Any]:
    """Codex-family models (a CURATED STATIC list) + the active selection.

    Unlike the other providers, the Codex CLI has no key-authorized ``/v1/models``
    endpoint (it authenticates via the ChatGPT OAuth session ``codex login`` writes), so
    the list is always static (``source="static"``). ``reachable`` reflects whether the
    CLI is installed AND logged in (``codex login status``) so the UI can surface a
    "run `codex login`" affordance; the model list is returned either way (NEVER 500).
    """
    from akana_server.orchestrator.codex_catalog import fetch_codex_models

    return await fetch_codex_models(services.settings, force_refresh=refresh)


@router.put("/system/llm-settings", dependencies=[Depends(require_akana_bearer)])
async def put_llm_settings(
    request: Request, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    settings = services.settings
    try:
        raw = await request.json()
    except Exception as e:
        raise http_error(400, "INVALID_JSON", str(e)) from e
    try:
        patch = _parse_body(raw)
    except ValueError as e:
        raise http_error(422, "INVALID_BODY", str(e)) from e

    # Reject an out-of-enum provider at the boundary. _merge would otherwise keep
    # the previous value (never wiping it), but a silent "your typo was ignored"
    # is a worse UX than a clear field error — the write is refused whole.
    prov = patch.get("provider")
    if isinstance(prov, str) and prov.strip().lower() not in _VALID_PROVIDERS:
        raise http_error(
            422,
            "VALIDATION",
            "Unknown provider; no changes were applied.",
            fields={"provider": f"must be one of: {', '.join(sorted(_VALID_PROVIDERS))}"},
        )

    llm = update_llm_settings(settings.data_dir, settings, patch)
    request.app.state.llm_settings = llm
    return public_llm_payload(
        llm, settings=settings, active_tag=resolve_cursor_model_tag(settings, llm)
    )
