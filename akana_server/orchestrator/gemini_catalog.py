"""Gemini model catalog — LIVE model list from the Google API (google-genai SDK).

Like Cursor/Claude/Ollama, gemini now fetches the REAL models instead of a
static list: ``client.aio.models.list()`` (with the user's own
``gemini_api_key``). So when new versions (3.x, etc.) ship, the UI shows them
automatically — no manual code edits.

Follows the :mod:`.claude_catalog` pattern: a TTL cache keyed by an api-key
fingerprint + ``force_refresh`` + a static fallback (``gemini_model_options``) —
if the SDK is not installed / there is no key / the API is unreachable,
``reachable=False`` + the fallback list (never 500). Only ``gemini-*`` chat
models that support ``generateContent`` are kept (excluding embedding/tts/aqa).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from akana_server.config import Settings
from akana_server.llm_settings import gemini_model_options
from akana_server.orchestrator.catalog_core import (
    CatalogCache,
    build_models_response,
    key_fingerprint,
)
from akana_server.orchestrator.gemini_shared import (
    genai_installed,
    make_client,
    resolve_api_key,
)

log = logging.getLogger(__name__)

_LIST_TIMEOUT = 15.0


_cache = CatalogCache()


def invalidate_gemini_catalog_cache() -> None:
    """Reset the cache when the key changes / on a forced refresh."""
    _cache.invalidate()


#: NON-chat model name fragments (filtered out by name even if tts/embedding/
#: image-gen report ``generateContent``). "image" → ``gemini-*-image`` (image
#: GENERATING; not chat); image INPUT is not a separate name (it's a modality).
_NON_CHAT_HINTS = ("embedding", "embed", "-tts", "image", "imagen", "aqa", "gemma")


def _is_chat_model(value: str, supported_actions: list[str] | None) -> bool:
    """Is this a ``gemini-*`` chat model: name-based hard filtering (always) +
    ``generateContent`` support. Name filtering comes FIRST because tts/image
    models can also report ``generateContent`` (e.g. ``gemini-2.0-flash-tts``)."""
    if not value.startswith("gemini-"):
        return False
    low = value.lower()
    if any(h in low for h in _NON_CHAT_HINTS):
        return False
    if supported_actions:
        return "generateContent" in supported_actions
    return True


def _options_from_models(models: list[Any]) -> list[dict[str, str]]:
    """Live ``Model`` list → ``{value,label}`` (the ``models/`` prefix is stripped)."""
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for m in models:
        name = (getattr(m, "name", "") or "").strip()
        value = name[len("models/") :] if name.startswith("models/") else name
        if not value or value in seen:
            continue
        actions = getattr(m, "supported_actions", None)
        if not _is_chat_model(value, actions):
            continue
        seen.add(value)
        label = (getattr(m, "display_name", "") or "").strip() or value
        rows.append({"value": value, "label": label})
    # Version-heuristic: descending by name (gemini-3* > gemini-2.5* > …) → newest on top.
    rows.sort(key=lambda r: r["value"], reverse=True)
    return rows


async def _list_models(settings: Settings) -> dict[str, Any]:
    """``client.aio.models.list()`` → ``{ok, models}`` or ``{ok:False, error}``."""
    client = make_client(settings)
    if client is None:  # no SDK or no key (the caller already distinguishes these)
        return {"ok": False, "error": "Could not set up the Gemini client (SDK/key)."}
    try:
        models: list[Any] = []

        async def _drain() -> None:
            pager = await client.aio.models.list()
            async for m in pager:
                models.append(m)

        await asyncio.wait_for(_drain(), timeout=_LIST_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 - network/auth/SDK → structured error
        return {"ok": False, "error": f"Could not reach the Gemini API: {exc}"}
    return {"ok": True, "models": models}


async def _fetch_cached(settings: Settings, *, force_refresh: bool = False) -> dict[str, Any]:
    """Single-flight + TTL cache (shared :mod:`catalog_core` discipline).

    Previously the module lock was held ACROSS the network ``list()`` call, so one
    slow request serialized every concurrent catalog/status probe; the core releases
    the lock before awaiting the fetch.
    """
    key = resolve_api_key(settings) or ""
    fp = key_fingerprint(key)
    if not fp:
        return {"ok": False, "error": "No Gemini API key."}

    return await _cache.get(
        fp, lambda: _list_models(settings), force_refresh=force_refresh
    )


async def probe_gemini_api(
    settings: Settings, *, force_refresh: bool = False
) -> dict[str, Any]:
    """Live Gemini API health — key + catalog reachability (cached).

    A shape symmetric with the cursor/claude probes for the dashboard
    ``/system/status``: ``{key_set, reachable, error, model_count}``. No SDK →
    ``key_set=False``; no key → ``key_set=False``; otherwise the cached catalog is
    fetched and only LIVE models are counted (``model_count=0`` if it falls back
    to the static list). Defensive: never blows up — an unexpected error yields
    ``reachable=False`` + error text.
    """
    if not genai_installed():
        # Report the key truthfully even without the SDK. A user who pasted a valid key
        # but skipped the provider deps should see "key saved, but the SDK is missing"
        # (actionable) — NOT "no key", which sent them back to re-enter a key that was
        # already there. The SDK is the missing piece, so name it and how to add it.
        return {
            "key_set": bool(resolve_api_key(settings)),
            "reachable": False,
            "error": "google-genai SDK is not installed — run: python akana.py add gemini",
            "model_count": 0,
        }
    if not resolve_api_key(settings):
        return {
            "key_set": False,
            "reachable": False,
            "error": "No Gemini API key",
            "model_count": 0,
        }
    try:
        result = await _fetch_cached(settings, force_refresh=force_refresh)
    except Exception as exc:  # noqa: BLE001 - probe → structured error (never 500)
        return {
            "key_set": True,
            "reachable": False,
            "error": f"Could not reach the Gemini API: {exc}",
            "model_count": 0,
        }
    if not result.get("ok"):
        return {
            "key_set": True,
            "reachable": False,
            "error": str(result.get("error") or "Gemini API unreachable"),
            "model_count": 0,
        }
    api_models = result.get("models") if isinstance(result.get("models"), list) else []
    live = _options_from_models(api_models)
    return {
        "key_set": True,
        "reachable": True,
        "error": None,
        # Only LIVE chat models (the static fallback is not counted here).
        "model_count": len(live),
    }


async def fetch_gemini_models(
    settings: Settings, *, force_refresh: bool = False
) -> dict[str, Any]:
    """Live Gemini catalog for UI (same shape as ``/system/cursor/models``).

    ``{reachable, models, active, error, source, cached}``. No SDK/no key/API
    error → ``reachable=False`` + static fallback (never 500). The gemini-specific
    ``genai_installed`` precondition gate goes first (before the key gate); the shared
    :func:`catalog_core.build_models_response` assembles the rest."""
    from akana_server.llm_settings import load_llm_settings, resolve_gemini_model_tag

    llm = load_llm_settings(settings.data_dir, settings)
    active = resolve_gemini_model_tag(settings, llm)
    fallback = gemini_model_options()

    return await build_models_response(
        _cache,
        fp=key_fingerprint(resolve_api_key(settings) or ""),
        fetch=lambda fr: _fetch_cached(settings, force_refresh=fr),
        options_from=_options_from_models,
        fallback=fallback,
        active=active,
        preconditions=[
            (
                genai_installed(),
                "Gemini SDK is not installed — pip install -r requirements-gemini.txt",
            ),
            (
                bool(resolve_api_key(settings)),
                "No Gemini API key — enter it under Settings → Identity.",
            ),
        ],
        unreachable_error="Gemini API unreachable",
        force_refresh=force_refresh,
    )


__all__ = [
    "fetch_gemini_models",
    "invalidate_gemini_catalog_cache",
    "probe_gemini_api",
]
