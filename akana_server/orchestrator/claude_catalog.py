"""Claude model catalog + API health via the Anthropic ``/v1/models`` endpoint.

The ``claude`` CLI has no ``models list`` subcommand, but the subscription OAuth
token it stores (``~/.claude/.credentials.json`` → ``claudeAiOauth.accessToken``,
or a long-lived ``claude setup-token`` kept in the secret store) authorizes a
plain ``GET https://api.anthropic.com/v1/models`` — returning the live catalog
(``id`` / ``display_name`` / ``max_tokens`` …). We mirror :mod:`.cursor_catalog`:
a TTL cache keyed by a token fingerprint, single-flight refresh, ``force_refresh``
and a static fallback (``claude_model_options``) when the API is unreachable.

The OAuth token is refreshed by the CLI on every run (~8 h TTL). We DO NOT refresh
it ourselves — rotating the refresh-token out from under the CLI would break the
user's ``claude`` auth. A stale token simply 401s here → ``reachable=False`` →
static fallback, exactly like Cursor degrades when its key is unset.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from akana_server.config import Settings
from akana_server.llm_settings import claude_model_options
from akana_server.orchestrator.catalog_core import (
    CatalogCache,
    build_models_response,
    key_fingerprint,
)

log = logging.getLogger(__name__)

_MODELS_URL = "https://api.anthropic.com/v1/models"
_ANTHROPIC_VERSION = "2023-06-01"
_PROBE_TIMEOUT = 15.0
_PAGE_LIMIT = 1000  # a single page is more than enough; has_more is still tracked
_MAX_PAGES = 5

#: ``--model`` "always latest" aliases — /v1/models does not return these (it
#: returns concrete version ids), so they are prepended to the live list.
_ALIAS_VALUES = frozenset({"opus", "sonnet", "haiku"})


_cache = CatalogCache()


# --------------------------------------------------------------------------- #
# Token source
# --------------------------------------------------------------------------- #
def _credentials_path() -> Path:
    """The ``claude`` CLI's credentials file — follows ``CLAUDE_CONFIG_DIR`` if set."""
    cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    base_dir = Path(cfg_dir) if cfg_dir else Path.home() / ".claude"
    return base_dir / ".credentials.json"


def _token_from_credentials() -> str:
    """Subscription OAuth access token (the CLI refreshes it on every run) — '' if absent."""
    try:
        data = json.loads(_credentials_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if isinstance(oauth, dict):
        tok = oauth.get("accessToken")
        if isinstance(tok, str):
            return tok.strip()
    return ""


def _claude_oauth_token(settings: Settings) -> str:
    """Resolution order: secret-store ``claude_oauth_token`` (setup-token,
    long-lived, isolated config-dir scenario) → the subscription
    ``credentials.json`` accessToken.

    ``getattr`` defense: so SimpleNamespace test doubles without ``data_dir``
    also work (the cursor_catalog pattern).
    """
    data_dir = getattr(settings, "data_dir", None)
    if data_dir is not None:
        try:
            from akana_server.secret_store import get_secret

            stored = get_secret(data_dir, "claude_oauth_token")
            if stored:
                return stored.strip()
        except Exception:  # pragma: no cover - store unreadable → credentials/file
            pass
    return _token_from_credentials()


def _key_fingerprint(token: str) -> str:
    return key_fingerprint(token)


def invalidate_claude_catalog_cache() -> None:
    """Reset the cache when the token changes or on a forced refresh."""
    _cache.invalidate()


# --------------------------------------------------------------------------- #
# HTTP fetch (httpx, async)
# --------------------------------------------------------------------------- #
def _humanize_status(status: int, body: str) -> str:
    if status in (401, 403):
        return (
            "Claude session token is invalid/expired — sending a chat "
            "refreshes the CLI token, then try again."
        )
    snippet = body.strip().replace("\n", " ")[:200]
    return f"Anthropic API {status}: {snippet}" if snippet else f"Anthropic API {status}"


async def _fetch_models_http(token: str) -> dict[str, Any]:
    """``GET /v1/models`` (paginated) → ``{ok, models}`` or ``{ok:False, error}``."""
    import httpx

    headers = {
        "Authorization": f"Bearer {token}",
        "anthropic-version": _ANTHROPIC_VERSION,
    }
    models: list[dict[str, Any]] = []
    after_id: str | None = None
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            for _ in range(_MAX_PAGES):
                params: dict[str, Any] = {"limit": _PAGE_LIMIT}
                if after_id:
                    params["after_id"] = after_id
                resp = await client.get(_MODELS_URL, headers=headers, params=params)
                if resp.status_code != 200:
                    return {
                        "ok": False,
                        "error": _humanize_status(resp.status_code, resp.text),
                    }
                payload = resp.json()
                page = payload.get("data") if isinstance(payload, dict) else None
                if isinstance(page, list):
                    models.extend(m for m in page if isinstance(m, dict))
                if not (isinstance(payload, dict) and payload.get("has_more")):
                    break
                after_id = payload.get("last_id") if isinstance(payload, dict) else None
                if not after_id:
                    break
    except Exception as exc:  # noqa: BLE001 - network/parsing → structured error
        return {"ok": False, "error": f"Could not reach the Anthropic API: {exc}"}
    return {"ok": True, "models": models}


async def _fetch_cached(settings: Settings, *, force_refresh: bool = False) -> dict[str, Any]:
    """Single-flight + TTL cache (shared :mod:`catalog_core` discipline)."""
    token = _claude_oauth_token(settings)
    fp = _key_fingerprint(token)
    if not fp:
        return {"ok": False, "error": "No Claude session — run `claude login` (or `claude setup-token`)"}

    # `_fetch_models_http` is resolved at call time so tests can monkeypatch it.
    return await _cache.get(
        fp, lambda: _fetch_models_http(token), force_refresh=force_refresh
    )


# --------------------------------------------------------------------------- #
# Mapping + public API
# --------------------------------------------------------------------------- #
def _alias_options() -> list[dict[str, str]]:
    return [o for o in claude_model_options() if o.get("value") in _ALIAS_VALUES]


def _options_from_api(models: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Live ``/v1/models`` → ``{value,label}`` (newest first), aliases at the front."""
    rows: list[tuple[str, dict[str, str]]] = []
    seen: set[str] = set()
    for raw in models:
        mid = str(raw.get("id") or "").strip()
        if not mid or mid in seen or not mid.startswith("claude-"):
            continue
        seen.add(mid)
        label = str(raw.get("display_name") or mid).strip() or mid
        created = str(raw.get("created_at") or "")
        rows.append((created, {"value": mid, "label": label}))
    # created_at descending (newest model on top); entries without created go last.
    rows.sort(key=lambda r: r[0], reverse=True)
    live = [opt for _, opt in rows]
    return _alias_options() + live


def _claude_error_code(result: dict[str, Any]) -> str:
    """Language-neutral tag for a failed probe (``token_expired`` | ``unreachable``).

    ``token_expired`` is the AUTH-CERTAIN class — Anthropic answered 401/403 (the token
    is invalid/expired), which ``_humanize_status`` renders as an "invalid/expired"
    string. The onboarding wizard treats it as a hard rejection; the catch-all
    ``unreachable`` (network/parse failure) keeps the provider selected with an amber
    warning so a transient blip on a valid session doesn't un-select Claude. (The
    ``no_session`` / no-token case is tagged separately in :func:`probe_claude_api`.)"""
    low = str(result.get("error") or "").lower()
    if "invalid/expired" in low or "401" in low or "403" in low:
        return "token_expired"
    return "unreachable"


def _probe_from_result(result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("ok"):
        return {
            "token_set": True,
            "reachable": False,
            "error": str(result.get("error") or "Anthropic API unreachable"),
            "error_code": _claude_error_code(result),
            "model_count": 0,
        }
    models = result.get("models") if isinstance(result.get("models"), list) else []
    return {
        "token_set": True,
        "reachable": True,
        "error": None,
        "error_code": None,
        "model_count": len(models),
    }


async def probe_claude_api(
    settings: Settings, *, force_refresh: bool = False
) -> dict[str, Any]:
    """Live Claude API health — token + catalog reachability (cached).

    ``error_code`` is a stable, language-neutral tag (``no_session`` | ``token_expired`` |
    ``unreachable``) the onboarding wizard maps to a localized message; ``error`` stays as
    the English default for surfaces without a dictionary. ``no_session`` (no token) and
    ``token_expired`` (401/403) are auth-certain; ``unreachable`` is transient."""
    token = _claude_oauth_token(settings)
    if not token:
        return {
            "token_set": False,
            "reachable": False,
            "error": "No Claude session — run `claude login` (or `claude setup-token`)",
            "error_code": "no_session",
            "model_count": 0,
        }
    result = await _fetch_cached(settings, force_refresh=force_refresh)
    return _probe_from_result(result)


async def fetch_claude_models(
    settings: Settings, *, force_refresh: bool = False
) -> dict[str, Any]:
    """Live model catalog for UI (in the Cursor ``/system/cursor/models`` shape).

    No token → static fallback; the shared :func:`catalog_core.build_models_response`
    assembles the rest (the token/no-session gate is the claude counterpart of the other
    providers' key gate)."""
    from akana_server.llm_settings import load_llm_settings, resolve_claude_model_tag

    llm = load_llm_settings(settings.data_dir, settings)
    active = resolve_claude_model_tag(settings, llm)
    fallback = claude_model_options()

    token = _claude_oauth_token(settings)
    return await build_models_response(
        _cache,
        fp=_key_fingerprint(token),
        fetch=lambda fr: _fetch_cached(settings, force_refresh=fr),
        options_from=_options_from_api,
        fallback=fallback,
        active=active,
        preconditions=[
            (
                bool(token),
                "No Claude session — run `claude login` (or `claude setup-token`)",
            ),
        ],
        unreachable_error="Anthropic API unreachable",
        force_refresh=force_refresh,
    )
