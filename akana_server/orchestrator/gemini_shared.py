"""Gemini provider — shared client / key / reachability layer.

Both the text surface (``gemini_provider.py``, Phase 1) and the voice surface
(``voice/gemini_live.py``, Phase 2) use this module: a single ``genai.Client``
setup point, a single key resolution (secret_store > env), and a single
reachability gate (SDK installed + key present).

google-genai is an **OPTIONAL** dependency (``pip install -r requirements-gemini.txt``). If it
is NOT installed, this module NEVER blows up at import time: ``_genai`` stays
``None``, :func:`gemini_available` returns ``False``, :func:`make_client` returns
``None`` — and the caller turns this into a clean "provider unavailable" error.
So Akana boots fine without google-genai (like the ollama/xtts optional groups).

NOTE: there is NO ``AKANA_*`` env literal here (outside the drift-guard scope).
The key env fallback uses Google SDK's own canonical names (``GEMINI_API_KEY`` /
``GOOGLE_API_KEY``); the canonical source is still the secret_store populated from
the dashboard.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any

from akana_server.secret_store import get_secret

if TYPE_CHECKING:
    from akana_server.config import Settings

log = logging.getLogger(__name__)

# google-genai optional — import guard. If absent, _genai is None and all surfaces are "off".
try:  # pragma: no cover - installed/not-installed environments are tested in separate CI
    from google import genai as _genai
except ImportError:  # pragma: no cover
    _genai = None

#: secret_store canonical key name (the credentials route + vault use the same name).
SECRET_KEY = "gemini_api_key"

#: Key env fallbacks — the names Google SDK reads itself (NOT AKANA_*).
_ENV_KEY_NAMES = ("GEMINI_API_KEY", "GOOGLE_API_KEY")


def genai_installed() -> bool:
    """Could the google-genai SDK be imported (is the package installed)?"""
    return _genai is not None


def resolve_api_key(settings: Settings) -> str | None:
    """Gemini API key: secret_store > env (GEMINI/GOOGLE_API_KEY) > None.

    The canonical source is the secret_store populated from the dashboard; env is
    only a development fallback. An empty/whitespace value is treated as None.
    """
    try:
        stored = get_secret(settings.data_dir, SECRET_KEY)
    except Exception:  # pragma: no cover - a vault failure must not break key resolution
        stored = None
    if stored and stored.strip():
        return stored.strip()
    for name in _ENV_KEY_NAMES:
        raw = os.environ.get(name, "").strip()
        if raw:
            return raw
    return None


def gemini_available(settings: Settings) -> bool:
    """Are the Gemini surfaces usable: SDK installed AND key present.

    The Live-specific ``gemini_live_enabled`` gate is SEPARATE (in the WS route,
    Phase 2); this function only reports the shared precondition (SDK + key).
    """
    return genai_installed() and bool(resolve_api_key(settings))


#: Live native-audio surface defaults (identical to config.Settings; used when
#: the setting is empty/missing — the same literals so the single source doesn't
#: diverge into two).
_LIVE_MODEL_DEFAULT = "models/gemini-2.5-flash-native-audio-latest"
_LIVE_VOICE_DEFAULT = "Charon"


def resolve_gemini_live_model(settings: Settings) -> str:
    """Active Live model name: the persist/env setting (``gemini_live_model``) > default.

    This is SEPARATE from the text surface's ``resolve_gemini_model_tag`` — the
    Live preview native-audio model is a different namespace
    (``models/...-native-audio-...``).
    """
    return (getattr(settings, "gemini_live_model", "") or _LIVE_MODEL_DEFAULT).strip()


def resolve_gemini_live_voice(settings: Settings) -> str:
    """Active Live voice name: the persist/env setting (``gemini_live_voice``) > default."""
    return (getattr(settings, "gemini_live_voice", "") or _LIVE_VOICE_DEFAULT).strip()


def make_client(settings: Settings, *, live: bool = False) -> Any | None:
    """Build a ``genai.Client`` — ``None`` if unusable (never a raw blowup).

    ``live=True`` → ``v1alpha`` api_version for the Live native-audio API
    (preview); the default stable version for the text surface. If there is no
    SDK / no key / client setup errors, ``None`` is returned; the caller turns
    this into a "provider unavailable" (HTTP 503 / WS close) error.
    """
    if not genai_installed():
        return None
    key = resolve_api_key(settings)
    if not key:
        return None
    try:
        if live:
            return _genai.Client(api_key=key, http_options={"api_version": "v1alpha"})
        return _genai.Client(api_key=key)
    except Exception:  # pragma: no cover - SDK setup/network/configuration error
        log.warning("could not set up genai.Client", exc_info=True)
        return None


__all__ = [
    "SECRET_KEY",
    "genai_installed",
    "resolve_api_key",
    "gemini_available",
    "resolve_gemini_live_model",
    "resolve_gemini_live_voice",
    "make_client",
]
