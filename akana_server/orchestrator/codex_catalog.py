"""Codex model catalog + CLI/auth health for the OpenAI Codex CLI provider.

Unlike :mod:`.claude_catalog` / :mod:`.openai_catalog`, the Codex CLI does NOT expose
a key-authorized ``/v1/models`` catalog endpoint we can hit over HTTP: it authenticates
through the ChatGPT OAuth session that ``codex login`` writes to
``~/.codex/auth.json``, and there is no simple public "list the models this ChatGPT
plan grants" call. So this catalog is a CURATED STATIC list (the current Codex model
family — see :data:`llm_settings._CODEX_MODEL_OPTIONS`, sourced from
developers.openai.com/codex/models, captured 2026-07).

To keep the settings UI symmetric with the other providers, :func:`fetch_codex_models`
returns the SAME response shape as ``fetch_claude_models`` / ``fetch_openai_models``
(``{reachable, models, active, error, source, cached}``) — always ``source="static"``.
:func:`probe_codex_cli` runs ``codex login status`` (the CLI's own, cheap,
automation-friendly auth check) so the onboarding wizard / doctor can report whether the
CLI is installed AND logged in, mirroring ``claude_catalog.probe_claude_api``.

Behaviour parity with claude: a missing CLI or a not-logged-in session is reported as a
clear, actionable status — never a crash.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Any

from akana_server.config import Settings
from akana_server.llm_settings import (
    codex_model_options,
    load_llm_settings,
    resolve_codex_model_tag,
)

log = logging.getLogger(__name__)

#: The Codex CLI binary name resolved on PATH (``codex`` on POSIX, ``codex.cmd`` on
#: Windows via PATHEXT). Kept as a module constant so tests can monkeypatch the probe.
_CODEX_BIN = "codex"

#: ``codex login status`` is documented to "exit with 0 when logged in"; we cap it so a
#: hung CLI cannot stall the settings/onboarding probe.
_LOGIN_PROBE_TIMEOUT = 10.0


def _codex_on_path() -> str | None:
    """Absolute path of the ``codex`` CLI on PATH, or ``None`` (PATHEXT-aware)."""
    return shutil.which(_CODEX_BIN)


async def probe_codex_cli(settings: Settings) -> dict[str, Any]:
    """Codex CLI health — installed on PATH + logged in (via ``codex login status``).

    Returns a stable, language-neutral shape mirroring ``probe_claude_api``:

    ``{"installed": bool, "logged_in": bool, "reachable": bool, "error": str|None,
       "error_code": "not_installed"|"not_logged_in"|None, "model_count": int}``

    ``error_code`` classes:
      * ``not_installed`` — the CLI is not on PATH (install: ``npm i -g @openai/codex``).
      * ``not_logged_in`` — the CLI is present but ``codex login status`` did not exit 0
        (run ``codex login``). This is the auth-certain class.

    Any subprocess/timeout failure degrades to ``not_logged_in`` with an actionable
    message (never raises) — the catalog + onboarding must never 500 on a probe.
    """
    _ = settings  # accepted for call-site symmetry with the other provider probes
    bin_path = _codex_on_path()
    if not bin_path:
        return {
            "installed": False,
            "logged_in": False,
            "reachable": False,
            "error": "Codex CLI not found — install: npm install -g @openai/codex",
            "error_code": "not_installed",
            "model_count": 0,
        }
    logged_in, detail = await _login_status(bin_path)
    if not logged_in:
        return {
            "installed": True,
            "logged_in": False,
            "reachable": False,
            "error": detail or "Not logged in to Codex — run `codex login` in the terminal.",
            "error_code": "not_logged_in",
            "model_count": 0,
        }
    return {
        "installed": True,
        "logged_in": True,
        "reachable": True,
        "error": None,
        "error_code": None,
        "model_count": len(codex_model_options()),
    }


async def _login_status(bin_path: str) -> tuple[bool, str | None]:
    """Run ``codex login status`` → ``(logged_in, detail)``. Never raises.

    ``codex login status`` "prints the active authentication mode and exits with 0 when
    logged in" — so exit code 0 is the authoritative signal. A missing binary / spawn
    error / timeout is treated as "not logged in" with a short detail string.
    """
    from akana_server.orchestrator.llm_process import executable_argv

    argv = executable_argv([bin_path, "login", "status"])
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, NotADirectoryError, OSError) as exc:  # pragma: no cover - spawn race
        return False, f"could not run `codex login status`: {exc}"
    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=_LOGIN_PROBE_TIMEOUT)
    except (TimeoutError, asyncio.TimeoutError):
        try:
            proc.kill()
        except ProcessLookupError:  # pragma: no cover - already gone
            pass
        return False, "`codex login status` timed out"
    if proc.returncode == 0:
        return True, (out_b or b"").decode("utf-8", errors="replace").strip() or None
    detail = (err_b or out_b or b"").decode("utf-8", errors="replace").strip()
    return False, detail[:200] or None


async def fetch_codex_models(
    settings: Settings, *, force_refresh: bool = False
) -> dict[str, Any]:
    """Static Codex model catalog for the UI (in the shared provider-models shape).

    The Codex CLI has no HTTP model-list endpoint we can authorize, so the list is
    always the curated static one and ``source`` is always ``"static"``. ``reachable``
    tracks whether the CLI is installed + logged in (``probe_codex_cli``) so the UI can
    surface the same "not logged in — run `codex login`" affordance the other providers
    show for a missing key; the model list is still returned either way (never 500).

    ``force_refresh`` is accepted for call-site symmetry with the live catalogs but is a
    no-op here (there is nothing to re-fetch).
    """
    _ = force_refresh
    llm = load_llm_settings(settings.data_dir, settings)
    active = resolve_codex_model_tag(settings, llm)
    models = codex_model_options()
    probe = await probe_codex_cli(settings)
    return {
        "reachable": bool(probe.get("reachable")),
        "models": models,
        "active": active,
        "error": probe.get("error"),
        "source": "static",
        "cached": False,
    }
