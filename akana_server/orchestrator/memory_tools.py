"""MCP glue — the ``mcp_servers`` payload every Cursor agent run receives.

Two sources, merged by :func:`mcp_servers_payload`:

* ``akana_memory`` — the built-in stdio server from the clean memory package
  (``python -m akana.memory.mcp``), exposing ``memory_search`` /
  ``memory_remember`` / ``memory_forget`` as native
  tools. Runs as its own process over the same ``<data_dir>/db/memory.db``
  (SQLite WAL + short-lived connections make that safe). Disable with
  ``AKANA_MEMORY_TOOLS=0``.
* external servers — the owner's ``<data_dir>/mcp_servers.yaml``
  (see :mod:`.mcp_config` for schema and an example).

``memory_mcp_servers`` is the historical name kept for chat.py / voice.py —
same signature, now returns the merged payload.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from akana_server.config import Settings
from akana_server.orchestrator.mcp_config import load_external_mcp_servers

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Memory-settings env overrides forwarded to the MCP child process when set
#: in the server's environment — so the owner's .env steers the K30 clamp and
#: vector mode without the child needing the server's full env. This applies
#: to the BUILT-IN akana_memory child only; external yaml servers never
#: inherit anything from the server environment (see mcp_config).
_PASSTHROUGH_ENV = ("AKANA_MEMORY_ALLOW_DIRECT", "AKANA_MEMORY_VECTOR")

#: Master-key source env vars forwarded to the akana_vault child so it decrypts with
#: the SAME key as the server. The default keyfile (~/.config/akana/vault.key) needs
#: nothing — same user, same path; only env/keyring setups need this passthrough.
_VAULT_KEY_ENV = ("AKANA_VAULT_KEY", "AKANA_VAULT_KEYFILE", "AKANA_VAULT_KEYRING")

__all__ = [
    "mcp_servers_payload",
    "memory_mcp_servers",
    "memory_tools_enabled",
    "schedule_tools_enabled",
    "vault_tools_enabled",
]


def _runtime_bool(key: str) -> bool | None:
    """Bool override from runtime settings (changeable via UI); returns None if not bound."""
    try:
        from akana_server.runtime_settings import runtime_override

        ov = runtime_override(key)
        return None if ov is None else bool(ov)
    except Exception:  # settings resolution must never break this gate
        return None


def memory_tools_enabled() -> bool:
    ov = _runtime_bool("memory_tools_enabled")
    if ov is not None:
        return ov
    return os.environ.get("AKANA_MEMORY_TOOLS", "1").strip().lower() not in {
        "0",
        "false",
        "off",
    }


def vault_tools_enabled() -> bool:
    """Whether the built-in ``akana_vault`` MCP server is added to the agent run.

    DEFAULT ON (like memory): the model gets secure-vault tools (discover + fetch a
    secret to act with it). Access-gating is intentionally OFF — the vault still
    audits every read. Disable with ``AKANA_VAULT_TOOLS=0`` or the runtime setting.
    """
    ov = _runtime_bool("vault_tools_enabled")
    if ov is not None:
        return ov
    return os.environ.get("AKANA_VAULT_TOOLS", "1").strip().lower() not in {
        "0",
        "false",
        "off",
    }


def schedule_tools_enabled() -> bool:
    """Whether the built-in ``akana_schedule`` MCP server is added to the agent run.

    DEFAULT ON (like memory/vault): the model gets scheduling tools (create/list/
    cancel/update reminders + recurring prompts). The engine loop itself does
    nothing without schedules, so this only controls whether the MODEL can create
    them. Disable with ``AKANA_SCHEDULE_TOOLS=0`` or the runtime setting.
    """
    ov = _runtime_bool("schedule_tools_enabled")
    if ov is not None:
        return ov
    return os.environ.get("AKANA_SCHEDULE_TOOLS", "1").strip().lower() not in {
        "0",
        "false",
        "off",
    }


def _akana_memory_server(settings: Settings) -> dict[str, Any] | None:
    """The built-in akana_memory stdio entry, or ``None`` (off / missing)."""
    if not memory_tools_enabled():
        return None
    launcher = _REPO_ROOT / "scripts" / "mcp_memory.py"
    if not launcher.is_file():
        return None
    env = {"AKANA_DATA_DIR": str(settings.data_dir)}
    for key in _PASSTHROUGH_ENV:
        value = os.environ.get(key, "")
        if value.strip():
            env[key] = value
    return {
        "type": "stdio",
        "command": sys.executable,
        # A standalone launcher FILE (not `-m akana.memory.mcp`). Running a file sets
        # sys.path[0] to the script's dir (scripts/), never the cwd, and the launcher
        # puts <repo>/src first on sys.path from its own __file__ — so the spawn needs
        # NO cwd and NO PYTHONPATH and the repo-root akana.py can't shadow the `akana`
        # package. That cwd/PYTHONPATH/shadowing fragility is exactly what broke the
        # child on Windows when a client ignored the config's cwd ("stuck connecting").
        "args": [str(launcher)],
        "env": env,
    }


def _akana_vault_server(settings: Settings) -> dict[str, Any] | None:
    """The built-in akana_vault stdio entry, or ``None`` (off).

    Same process pattern as the memory child; the ``akana_server`` package is imported
    from the repo root. Vault access is file/crypto — NO loopback REST, no
    ``conversation_id``. The master-key source env (``_VAULT_KEY_ENV``) is forwarded
    when set so the child decrypts with the same key as the server.
    """
    if not vault_tools_enabled():
        return None
    launcher = _REPO_ROOT / "scripts" / "mcp_vault.py"
    if not launcher.is_file():
        return None
    env = {"AKANA_DATA_DIR": str(settings.data_dir)}
    for key in _VAULT_KEY_ENV:
        value = os.environ.get(key, "")
        if value.strip():
            env[key] = value
    return {
        "type": "stdio",
        "command": sys.executable,
        # Standalone launcher FILE (cwd/PYTHONPATH-immune); symmetric with akana_memory.
        "args": [str(launcher)],
        "env": env,
    }


def _conv_env(settings: Settings, conversation_id: str | None) -> dict[str, str]:
    """Child env for the schedule MCP server: data dir + the CALLER's conversation.

    ``AKANA_CONVERSATION_ID`` makes SAME-CHAT delivery work on the MCP path
    (claude/cursor/codex): those CLIs spawn the stdio children per run, so the
    per-turn payload env is the only channel that can tell the child WHICH
    conversation invoked ``schedule_create``. Absent (voice snapshots built
    without a conversation, external clients) → the tools fall back to
    own-thread delivery."""
    env = {"AKANA_DATA_DIR": str(settings.data_dir)}
    conv = (conversation_id or "").strip()
    if conv:
        env["AKANA_CONVERSATION_ID"] = conv
    return env


def _akana_schedule_server(
    settings: Settings, conversation_id: str | None = None
) -> dict[str, Any] | None:
    """The built-in akana_schedule stdio entry, or ``None`` (off).

    Same process pattern as the memory/vault children; the ``akana_server``
    package is imported from the repo root via the standalone launcher FILE.
    Schedules are plain JSON in ``<data_dir>/schedules.json`` — no crypto key and
    no loopback REST, so only the ``_conv_env`` pair needs forwarding.
    """
    if not schedule_tools_enabled():
        return None
    launcher = _REPO_ROOT / "scripts" / "mcp_schedule.py"
    if not launcher.is_file():
        return None
    return {
        "type": "stdio",
        "command": sys.executable,
        # Standalone launcher FILE (cwd/PYTHONPATH-immune); symmetric with akana_vault.
        "args": [str(launcher)],
        "env": _conv_env(settings, conversation_id),
    }


def mcp_servers_payload(
    settings: Settings, conversation_id: str | None = None
) -> dict[str, Any] | None:
    """The full ``mcp_servers`` dict for an agent run, or ``None`` if empty.

    Merges sources: the built-in ``akana_memory`` server
    (``AKANA_MEMORY_TOOLS=0`` turns it off; default ON), the built-in ``akana_vault``
    server (secure-vault discover/fetch tools — default ON; ``AKANA_VAULT_TOOLS=0`` to
    turn off), and the owner's external servers from ``<data_dir>/mcp_servers.yaml``.
    ``conversation_id`` is accepted for call-site compatibility. ``None`` (nothing
    enabled) degrades to a plain agent run.
    """
    servers: dict[str, Any] = {}
    memory_entry = _akana_memory_server(settings)
    if memory_entry is not None:
        servers["akana_memory"] = memory_entry
    vault_entry = _akana_vault_server(settings)
    if vault_entry is not None:
        servers["akana_vault"] = vault_entry
    # conversation_id flows into the schedule child so SAME-CHAT delivery works
    # on the MCP path too (see _conv_env).
    schedule_entry = _akana_schedule_server(settings, conversation_id)
    if schedule_entry is not None:
        servers["akana_schedule"] = schedule_entry
    for name, cfg in load_external_mcp_servers(Path(settings.data_dir)).items():
        # built-in names (akana_memory/akana_vault/akana_schedule) are reserved
        # in mcp_config; setdefault = belt-and-suspenders (built-in servers are never overwritten).
        servers.setdefault(name, cfg)
    return servers or None


def memory_mcp_servers(
    settings: Settings, conversation_id: str | None = None
) -> dict[str, Any] | None:
    """Backwards-compatible name for :func:`mcp_servers_payload`.

    Historically returned only the ``akana_memory`` entry; it now returns the
    merged payload (memory + vault + external yaml servers) so the chat.py/voice.py
    call sites keep working unchanged.
    """
    return mcp_servers_payload(settings, conversation_id)
