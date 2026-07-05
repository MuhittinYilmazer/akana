"""Runtime secret store — ``data_dir/secrets.json`` (0600, atomic writes).

Holds credentials the dashboard can change at runtime, overriding the frozen
``.env``-derived ``Settings`` values. Only whitelisted keys are accepted and
raw values never leave this module unmasked except via :func:`get_secret`.

This is the SYSTEM-PROVIDER-KEY partition of the vault. :mod:`akana_server.secure_vault`
is the single scalar-secret authority; it owns the partition rule "``ALLOWED_KEYS`` names
live here, everything else in the encrypted keyfile" and routes every scalar accessor
accordingly. Providers resolve their key from here directly (via :func:`get_secret`), so
this store — not the keyfile — is authoritative for the whitelisted names.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from akana_server import vault_crypto
from akana_server.config import clean_secret_value

log = logging.getLogger(__name__)

_SECRETS_FILE = "secrets.json"

ALLOWED_KEYS = frozenset(
    {
        "cursor_api_key",
        "claude_oauth_token",
        "gemini_api_key",
        "openai_api_key",
        "telegram_bot_token",
    }
)

_lock = threading.Lock()

# Substrings that mark a value as a shipped *placeholder*, not a real credential.
# ``.env.example`` ships ``CURSOR_API_KEY=your-cursor-api-key-here``; if that leaks
# into the runtime as a "set" key the dashboard badge claims the provider is
# configured, the user never enters a real key, and chat hangs on an invalid
# bearer. These markers are multi-char and effectively never occur inside a real
# (random) API key / OAuth token, so matching is safe. Compared lowercase.
_PLACEHOLDER_MARKERS = (
    "your-",
    "-here",
    "changeme",
    "change-me",
    "replace-me",
    "replace_me",
    "placeholder",
    "example.com",
    "xxxxxxxx",
)

# Shortest plausible real credential. Every whitelisted key (Cursor/OpenAI/Gemini
# API keys, Claude OAuth token, Telegram bot token) is far longer than this; the
# floor only rejects empty/truncated junk.
_MIN_SECRET_LEN = 8


def is_real_secret(value: str | None) -> bool:
    """Whether ``value`` is a genuine credential — not empty, truncated, or a placeholder.

    The dashboard "configured" badge keys off this (via the credentials API) so a
    leftover ``.env.example`` placeholder reports as UNSET instead of falsely
    "configured". Sanitizes first (shared rules) so a quoted/whitespace-padded
    placeholder is still recognised.
    """
    cleaned = clean_secret_value(value)
    if len(cleaned) < _MIN_SECRET_LEN:
        return False
    low = cleaned.lower()
    return not any(marker in low for marker in _PLACEHOLDER_MARKERS)


def looks_like_placeholder(value: str | None) -> bool:
    """True if ``value`` is a shipped placeholder (``your-…``/``-here``/``changeme``…).

    Unlike :func:`is_real_secret` this imposes NO length floor — it is for key
    *resolution* (``_runtime_cursor_key``), where any user-supplied value should pass
    EXCEPT a leftover ``.env.example`` placeholder. Applying the full length floor there
    would also reject legitimate short values; the floor belongs on the *write* path only.
    """
    low = clean_secret_value(value).lower()
    return any(marker in low for marker in _PLACEHOLDER_MARKERS)


def _secrets_path(data_dir: Path) -> Path:
    return Path(data_dir) / _SECRETS_FILE


def _clean_secret(value: str) -> str:
    """Sanitize a stored token/key — delegates to the shared :func:`clean_secret_value`.

    Strips wrapping quotes *and* ALL whitespace (inner + outer). API keys and OAuth
    tokens never contain either; both sneak in via copy-paste (line-wrapped terminal
    output, shell-style ``'…'`` quoting) and break the bearer → ``401 Invalid bearer
    token`` / ``Invalid User API Key``. Applied on every read (:func:`load_secrets`)
    so any value reaching the Cursor bridge is clean regardless of how it was written.
    """
    return clean_secret_value(value)


def load_secrets(data_dir: Path) -> dict[str, str]:
    """Read the store; a missing or corrupt file degrades to ``{}``.

    The at-rest blob is encrypted; legacy plaintext files are still read and get
    re-encrypted on the next :func:`set_secrets`.
    """
    path = _secrets_path(data_dir)
    try:
        blob = path.read_bytes()
    except OSError:
        return {}
    text = vault_crypto.load_text(blob)
    if text is None:
        return {}
    try:
        raw = json.loads(text)
    except ValueError:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        key: _clean_secret(value)
        for key, value in raw.items()
        if key in ALLOWED_KEYS and isinstance(value, str) and _clean_secret(value)
    }


def _write_atomic(path: Path, data: dict[str, str]) -> None:
    payload = vault_crypto.encrypt_str(json.dumps(data, indent=2) + "\n")
    vault_crypto.write_private_bytes_atomic(path, payload)


def set_secrets(data_dir: Path, patch: dict) -> dict[str, str]:
    """Apply a partial patch; empty/None value clears the key. Returns new state."""
    with vault_crypto.file_lock(Path(data_dir) / ".vault.lock"), _lock:
        # With a wrong/corrupt master key, refuse to start from an empty base and
        # overwrite the existing secrets (inside the lock, before the merge).
        vault_crypto.assert_writable(_secrets_path(data_dir))
        current = load_secrets(data_dir)
        for key, value in (patch or {}).items():
            if key not in ALLOWED_KEYS:
                continue
            text = _clean_secret(value) if isinstance(value, str) else ""
            if text:
                current[key] = text
            else:
                current.pop(key, None)
        _write_atomic(_secrets_path(data_dir), current)
        return current


def get_secret(data_dir: Path, key: str) -> str | None:
    return load_secrets(data_dir).get(key) or None


def mask_hint(value: str) -> str:
    """Display hint for a stored secret: ``…AbCd`` (last 4) or ``set`` if short."""
    value = value or ""
    if len(value) < 8:
        return "set"
    return "…" + value[-4:]
