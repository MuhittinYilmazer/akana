"""Read repo .env for CLI commands."""

from __future__ import annotations

import os

from akana_cli.paths import ENV_FILE

#: Only these (path-valued) keys get ``~`` expansion. Expanding EVERY value corrupted a
#: token/secret that happens to start with ``~`` (e.g. ``AKANA_TOKEN=~abc`` → home path).
_PATH_ENV_KEYS = frozenset({"AKANA_DATA_DIR", "AKANA_VOICES_DIR", "AKANA_WORKSPACE"})


class EnvDecodeError(Exception):
    """.env exists but isn't decodable as UTF-8.

    Windows PowerShell 5.1 ``echo X > .env`` writes UTF-16LE and an ANSI-defaulting
    editor writes cp1254, either of which makes ``read_text(encoding="utf-8")`` raise
    a bare ``UnicodeDecodeError`` — which, because the language read happens before
    ``main``'s command guard, would crash EVERY CLI command (including ``setup``, the
    one meant to repair the install) with an undiagnosable traceback. We raise this
    instead so callers can print a clear "re-save .env as UTF-8" message.
    """


def _read_env_text() -> str:
    """Read .env robustly. ``utf-8-sig`` strips a UTF-8 BOM (no-op for plain UTF-8),
    so a BOM'd first key parses like any other; a genuinely non-UTF-8 file (UTF-16 /
    cp1254) raises :class:`EnvDecodeError` with the underlying reason.

    A BOM-less UTF-16LE file is the sneaky case: its NUL bytes are valid UTF-8, so
    ``utf-8-sig`` decodes it WITHOUT error into NUL-interleaved garbage
    (``"A\\x00K\\x00…"``) instead of raising — the recorded key would then silently
    fail to match and fall back to English with no re-save hint. A NUL in a decoded
    text ``.env`` never appears legitimately, so we treat it as the same undecodable
    condition and raise :class:`EnvDecodeError`."""
    try:
        text = ENV_FILE.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise EnvDecodeError(str(exc)) from exc
    if "\x00" in text:
        raise EnvDecodeError("NUL byte in decoded text (likely UTF-16 without a BOM)")
    return text


def load_repo_dotenv() -> None:
    if ENV_FILE.is_file():
        for line in _read_env_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            key = k.strip()
            if key and key not in os.environ:
                val = v.strip().strip('"').strip("'")
                os.environ[key] = os.path.expanduser(val) if key in _PATH_ENV_KEYS else val
    # Bridge legacy AKANA_CURSOR_* names to their new AKANA_* counterparts (same
    # backward-compat as server config.py). The user's .env or shell may still set
    # the old name → make CLI commands see the new name too.
    from akana_server.config import apply_legacy_env_aliases

    apply_legacy_env_aliases()


def read_env_key(key: str) -> str | None:
    if not ENV_FILE.is_file():
        return None
    for line in _read_env_text().splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            val = v.strip().strip('"').strip("'")
            return val if val else None
    return None


def server_host_port() -> tuple[str, int]:
    load_repo_dotenv()
    host = (os.environ.get("AKANA_HOST") or read_env_key("AKANA_HOST") or "127.0.0.1").strip()
    port_raw = os.environ.get("AKANA_PORT") or read_env_key("AKANA_PORT") or "8766"
    try:
        port = int(str(port_raw).strip())
    except ValueError:
        port = 8766
    return host, port
