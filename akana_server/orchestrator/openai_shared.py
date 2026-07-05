"""OpenAI provider — shared key / base-URL / reachability layer.

The OpenAI counterpart of ``gemini_shared.py``: a single key resolution
(secret_store > env), a single base-URL resolution (env > default), and a single
reachability gate (key present). ``openai_provider`` (the text surface) uses this
module — it goes DIRECTLY to OpenAI via raw httpx (NO openai SDK; same pattern as
the ollama driver), using the user's own ``openai_api_key`` (NOT Cursor's key).

UNLIKE gemini_shared, there is NO optional-SDK import guard here: the transport
(``httpx``) is already a hard dependency (the ollama driver uses it too), so no
separate optional group is needed. Reachability depends only on the key being present.

NOTE: There are NO ``AKANA_*`` env literals here (outside the drift-guard scope).
The key/base env fallbacks use OpenAI SDK's own canonical names (``OPENAI_API_KEY``
/ ``OPENAI_BASE_URL``); the canonical source is still the secret_store populated
from the dashboard.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from akana_server.secret_store import get_secret

if TYPE_CHECKING:
    from akana_server.config import Settings

#: secret_store canonical key name (the credentials route + vault use the same name).
SECRET_KEY = "openai_api_key"

#: Key env fallback — the name OpenAI SDK reads itself (NOT AKANA_*).
_ENV_KEY_NAME = "OPENAI_API_KEY"

#: Base-URL env fallback + default — OpenAI SDK's own canonical name.
#: Can be overridden via env for custom/compatible endpoints (Azure/proxy/local gateway).
_ENV_BASE_URL_NAME = "OPENAI_BASE_URL"
_DEFAULT_BASE_URL = "https://api.openai.com/v1"


def resolve_openai_key(settings: Settings) -> str | None:
    """OpenAI API key: secret_store > env (OPENAI_API_KEY) > None.

    The canonical source is the secret_store populated from the dashboard; env is
    only a development fallback. An empty/whitespace value is treated as None
    (identical pattern to gemini_shared.resolve_api_key)."""
    try:
        stored = get_secret(settings.data_dir, SECRET_KEY)
    except Exception:  # pragma: no cover - a vault failure must not break key resolution
        stored = None
    if stored and stored.strip():
        return stored.strip()
    raw = os.environ.get(_ENV_KEY_NAME, "").strip()
    return raw or None


def resolve_openai_base_url(settings: Settings) -> str:
    """OpenAI base-URL: env (OPENAI_BASE_URL) > default (https://api.openai.com/v1).

    Can be overridden via env for compatible/custom endpoints (Azure OpenAI, proxy,
    local gateway); a trailing ``/`` is stripped (the caller appends
    ``{base}/chat/completions``). An empty/whitespace env value falls back to the
    default."""
    raw = os.environ.get(_ENV_BASE_URL_NAME, "").strip()
    return (raw or _DEFAULT_BASE_URL).rstrip("/")


# --- Realtime (full-duplex voice) — counterpart of gemini_shared's Live helpers ---

#: Realtime WS defaults (identical to config.Settings; used when the setting is
#: empty/missing — the same literals so the single source doesn't diverge into two).
#: Default BETA model (``gpt-4o-realtime-preview``): the bridge's ``session.update``
#: shape + ``OpenAI-Beta: realtime=v1`` header + event names (``response.audio.delta``
#: etc.) are based on this generation. The GA ``gpt-realtime`` requires a different
#: session shape; event names are handled as twins in the bridge but session config
#: may need manual adjustment for GA (override via env).
_REALTIME_MODEL_DEFAULT = "gpt-4o-realtime-preview"
#: ``alloy`` (identical to config.Settings): marin/cedar are only valid for the GA
#: ``gpt-realtime``; BETA ``gpt-4o-realtime-preview`` rejects them (see config.py).
_REALTIME_VOICE_DEFAULT = "alloy"

#: Realtime WS endpoint — env (``OPENAI_REALTIME_URL``) > OpenAI default. Can be
#: overridden via env for Azure/proxy/compatible gateways (SEPARATE namespace from
#: base_url: realtime is a ``wss://`` socket, not the ``/v1`` REST base).
_ENV_REALTIME_URL_NAME = "OPENAI_REALTIME_URL"
_DEFAULT_REALTIME_URL = "wss://api.openai.com/v1/realtime"


def resolve_openai_realtime_model(settings: Settings) -> str:
    """Active Realtime model name: persist/env setting (``openai_realtime_model``) > default."""
    return (
        getattr(settings, "openai_realtime_model", "") or _REALTIME_MODEL_DEFAULT
    ).strip()


def is_ga_realtime_model(model: str) -> bool:
    """Is the model from the GA Realtime family (``gpt-realtime`` / ``gpt-realtime-*``)?

    The GA (generally available) model diverges from BETA in two ways: it requires a
    nested ``session.update`` shape AND does NOT use the ``OpenAI-Beta`` header. The
    BETA ``gpt-4o-realtime-preview`` name does not start with ``gpt-realtime``
    (``gpt-4o-...``) so it is safely distinguished — only names prefixed with
    ``gpt-realtime`` are treated as GA."""
    name = (model or "").strip().lower()
    return name == "gpt-realtime" or name.startswith("gpt-realtime")


def resolve_openai_realtime_voice(settings: Settings) -> str:
    """Active Realtime voice name: persist/env setting (``openai_realtime_voice``) > default."""
    return (
        getattr(settings, "openai_realtime_voice", "") or _REALTIME_VOICE_DEFAULT
    ).strip()


def openai_realtime_available(settings: Settings) -> bool:
    """Is the Realtime voice surface usable: is the key present? (transport ``websockets``
    is a hard dependency). The Realtime-specific ``openai_realtime_enabled`` gate is
    SEPARATE (in the WS route); this function only reports the shared precondition (key)."""
    return bool(resolve_openai_key(settings))


def resolve_realtime_url(settings: Settings, model: str) -> str:
    """Realtime WS URL: env (OPENAI_REALTIME_URL) > default + ``model=`` query parameter.

    ``model`` is URL-encoded (special characters / GA model names must not corrupt the
    socket); if the base already contains ``?``, the parameter is appended with ``&``
    (an overridden env endpoint may already contain a query string)."""
    base = (os.environ.get(_ENV_REALTIME_URL_NAME, "").strip() or _DEFAULT_REALTIME_URL).rstrip("/")
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{urlencode({'model': model})}"


def realtime_headers(settings: Settings, model: str | None = None) -> dict[str, str]:
    """Realtime WS auth headers: Bearer key (+ BETA ``OpenAI-Beta: realtime=v1``).

    If ``model`` is provided, the header is set based on the generation: GA
    (``gpt-realtime``) does NOT use ``OpenAI-Beta`` → it is omitted; BETA
    (``gpt-4o-realtime-preview``) requires it. Backwards compatibility: if ``model``
    is not given (None) the legacy behaviour is preserved — the BETA header is added
    (the default model is already BETA). If there is no key, an empty Bearer is
    returned (the caller uses ``openai_realtime_available`` to distinguish; the route
    will not connect without a key)."""
    key = resolve_openai_key(settings) or ""
    headers = {"Authorization": f"Bearer {key}"}
    if not (model is not None and is_ga_realtime_model(model)):
        headers["OpenAI-Beta"] = "realtime=v1"
    return headers


__all__ = [
    "SECRET_KEY",
    "resolve_openai_key",
    "resolve_openai_base_url",
    "resolve_openai_realtime_model",
    "is_ga_realtime_model",
    "resolve_openai_realtime_voice",
    "openai_realtime_available",
    "resolve_realtime_url",
    "realtime_headers",
]
