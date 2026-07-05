"""OpenAI model catalog — LIVE model list from the OpenAI API (raw httpx).

The OpenAI counterpart of ``gemini_catalog.py``: fetches REAL models instead of a
static list (``GET {base_url}/models`` with the user's own ``openai_api_key``). So
when new versions (gpt-5.x, o5, etc.) ship, the UI shows them automatically — no
manual code edits.

Difference from gemini_catalog: NO google-genai SDK; the transport is raw ``httpx``
(we don't use the openai SDK, same pattern as ollama/openai_provider). Therefore
there is NO ``genai_installed`` precondition — only the key is required. TTL cache
keyed by api-key fingerprint + ``force_refresh`` + a static fallback
(``openai_model_options``); if there is no key / the API is unreachable:
``reachable=False`` + the fallback list (never 500). Only chat models are kept
(embedding/whisper/tts/dall-e/moderation/realtime/instruct are excluded).
"""

from __future__ import annotations

import logging
import re
from typing import Any

from akana_server.config import Settings
from akana_server.llm_settings import openai_model_options
from akana_server.orchestrator.catalog_core import (
    CatalogCache,
    build_models_response,
    key_fingerprint,
)
from akana_server.orchestrator.openai_shared import (
    resolve_openai_base_url,
    resolve_openai_key,
)

log = logging.getLogger(__name__)

_LIST_TIMEOUT = 15.0


_cache = CatalogCache()


def invalidate_openai_catalog_cache() -> None:
    """Reset the cache when the key changes / on a forced refresh."""
    _cache.invalidate()


#: Non-chat model name fragments (hard name-based filtering). OpenAI ``/models``
#: returns EVERY type (embedding/whisper/tts/dall-e/moderation/realtime…); we filter
#: non-text-chat models by name. "instruct" → old completions model (not chat).
#: These fragments only appear in non-chat models → plain ``in`` check is safe.
_NON_CHAT_HINTS = (
    "embedding",
    "embed",
    "whisper",
    "-tts",
    "tts-",
    "dall-e",
    "image",
    "moderation",
    "transcribe",
    "realtime",
    "instruct",
    "babbage",
    "davinci",
)

#: Token-bounded non-chat fragments: ``audio`` (voice) / ``search`` (dedicated
#: web-search surface). A plain ``in`` check would match these anywhere and
#: incorrectly filter legitimate chat models → match only as a dash-delimited
#: token (``-audio-`` / ``...-search``). In OpenAI ids these surfaces are always
#: a separate token (``gpt-4o-audio-preview`` / ``gpt-4o-search-preview``).
_NON_CHAT_TOKEN_HINTS = ("audio", "search")

#: Chat model prefixes: ``gpt-*`` / ``chatgpt-*`` / ``o<digit>`` (o1/o3/o5…).
_O_SERIES = re.compile(r"^o\d")


def _is_chat_model(model_id: str) -> bool:
    """Is ``model_id`` a text-chat model: name-based hard filtering + chat prefix check.

    Filtering comes FIRST (e.g. ``gpt-4o-audio-preview`` starts with ``gpt-`` but is
    a voice model → filtered out); then the chat-family check (gpt/chatgpt/o-series).
    ``audio``/``search`` are matched token-bounded (a plain substring check would
    incorrectly exclude legitimate models)."""
    low = model_id.lower()
    if any(h in low for h in _NON_CHAT_HINTS):
        return False
    tokens = low.split("-")
    if any(h in tokens for h in _NON_CHAT_TOKEN_HINTS):
        return False
    return low.startswith("gpt-") or low.startswith("chatgpt-") or bool(_O_SERIES.match(low))


def _created(m: Any) -> int:
    """Safely coerce ``created`` (unix timestamp) to int (missing/malformed → 0)."""
    try:
        return int(m.get("created", 0)) if isinstance(m, dict) else 0
    except (TypeError, ValueError):
        return 0


def _options_from_models(models: list[Any]) -> list[dict[str, str]]:
    """Live ``/models`` ``data`` list → ``{value,label}`` (newest to oldest).

    OpenAI does not provide ``display_name`` → label = id. Sorted by ``created``
    descending (newest model on top); the OpenAI counterpart of gemini's name-based
    sorting (ids don't sort cleanly by version, creation time is more accurate)."""
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for m in sorted(models, key=_created, reverse=True):
        mid = ((m.get("id") if isinstance(m, dict) else "") or "").strip()
        if not mid or mid in seen or not _is_chat_model(mid):
            continue
        seen.add(mid)
        rows.append({"value": mid, "label": mid})
    return rows


async def _list_models(settings: Settings) -> dict[str, Any]:
    """``GET {base_url}/models`` → ``{ok, models}`` or ``{ok:False, error}``.

    Raw httpx (no openai SDK); ``Authorization: Bearer {key}``. The caller already
    guards against a missing key, but a defensive check is also present here."""
    import httpx

    key = resolve_openai_key(settings)
    if not key:
        return {"ok": False, "error": "No OpenAI API key."}
    base = resolve_openai_base_url(settings)
    try:
        async with httpx.AsyncClient(timeout=_LIST_TIMEOUT) as client:
            resp = await client.get(
                f"{base}/models", headers={"Authorization": f"Bearer {key}"}
            )
    except Exception as exc:  # noqa: BLE001 - network/auth/transport → structured error
        return {"ok": False, "error": f"Could not reach the OpenAI API: {exc}"}
    if resp.status_code != 200:
        return {
            "ok": False,
            "error": f"OpenAI API {resp.status_code}: {resp.text[:160]}",
        }
    try:
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - malformed JSON → structured error
        return {"ok": False, "error": f"Could not parse the OpenAI response: {exc}"}
    models = data.get("data") if isinstance(data, dict) else None
    return {"ok": True, "models": models if isinstance(models, list) else []}


async def _fetch_cached(settings: Settings, *, force_refresh: bool = False) -> dict[str, Any]:
    """Single-flight + TTL cache (shared :mod:`catalog_core` discipline).

    Previously the module lock was held ACROSS the network ``GET /models`` call, so
    one slow request serialized every concurrent catalog/status probe; the core
    releases the lock before awaiting the fetch.
    """
    key = resolve_openai_key(settings) or ""
    fp = key_fingerprint(key)
    if not fp:
        return {"ok": False, "error": "No OpenAI API key."}

    return await _cache.get(
        fp, lambda: _list_models(settings), force_refresh=force_refresh
    )


async def probe_openai_api(
    settings: Settings, *, force_refresh: bool = False
) -> dict[str, Any]:
    """Live OpenAI API health — key + catalog reachability (cached).

    A shape symmetric with the gemini/cursor/claude probes for the dashboard
    ``/system/status``: ``{key_set, reachable, error, model_count}``. No key →
    ``key_set=False``; otherwise the cached catalog is fetched and only LIVE chat
    models are counted (``model_count=0`` if it falls back to the static list).
    Defensive: never blows up. (NO ``genai_installed`` precondition like gemini —
    httpx is a hard dependency.)"""
    if not resolve_openai_key(settings):
        return {
            "key_set": False,
            "reachable": False,
            "error": "No OpenAI API key",
            "model_count": 0,
        }
    try:
        result = await _fetch_cached(settings, force_refresh=force_refresh)
    except Exception as exc:  # noqa: BLE001 - probe → structured error (never 500)
        return {
            "key_set": True,
            "reachable": False,
            "error": f"Could not reach the OpenAI API: {exc}",
            "model_count": 0,
        }
    if not result.get("ok"):
        return {
            "key_set": True,
            "reachable": False,
            "error": str(result.get("error") or "OpenAI API unreachable"),
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


async def fetch_openai_models(
    settings: Settings, *, force_refresh: bool = False
) -> dict[str, Any]:
    """Live OpenAI catalog for UI (same shape as ``/system/gemini/models``).

    ``{reachable, models, active, error, source, cached}``. No key / API error →
    ``reachable=False`` + static fallback (never 500). No SDK precondition (unlike
    gemini — httpx is a hard dependency); the shared
    :func:`catalog_core.build_models_response` assembles the response."""
    from akana_server.llm_settings import load_llm_settings, resolve_openai_model_tag

    llm = load_llm_settings(settings.data_dir, settings)
    active = resolve_openai_model_tag(settings, llm)
    fallback = openai_model_options()

    return await build_models_response(
        _cache,
        fp=key_fingerprint(resolve_openai_key(settings) or ""),
        fetch=lambda fr: _fetch_cached(settings, force_refresh=fr),
        options_from=_options_from_models,
        fallback=fallback,
        active=active,
        preconditions=[
            (
                bool(resolve_openai_key(settings)),
                "No OpenAI API key — enter it under Settings → Identity.",
            ),
        ],
        unreachable_error="OpenAI API unreachable",
        force_refresh=force_refresh,
    )


__all__ = [
    "fetch_openai_models",
    "invalidate_openai_catalog_cache",
    "probe_openai_api",
]
