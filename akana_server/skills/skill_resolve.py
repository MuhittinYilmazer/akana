"""Skill → MCP server + pack persona resolution (read-only; NO mount/startup).

These functions are the **persistent** helper resolvers needed by the in-chat
skill injection path (``turn_injection.py``) and persona discovery
(``persona/registry.py``) — split out of the old ``work_mode.py`` (v0.1: the
skill_run work mode / tool-lock was removed, but the "which server does this
skill need" + "what is the pack persona" resolution STAYS in chat injection).

None of them start a server or WRITE ``.mcp.json``/``mcp_servers.yaml`` — they
only read the existing, consented configuration and narrow/mark it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from akana_server.orchestrator.memory_tools import mcp_servers_payload
from akana_server.packs.host import pack_discovery_roots

log = logging.getLogger(__name__)

__all__ = [
    "find_pack_persona",
    "needed_servers",
    "resolve_skill_servers",
    "server_for_tool",
]

#: Built-in memory MCP server name (the ``memory_*`` tools come from here).
_MEMORY_SERVER = "akana_memory"


def server_for_tool(tool: str) -> str | None:
    """Reduces a single ``tools_allowed`` entry to the MCP server name it requires.

    Supported forms:

    * ``browser.navigate``          → ``browser`` (logical server before the dot)
    * ``mcp__ghidra__decompile``    → ``ghidra``
    * ``memory_remember``           → ``akana_memory`` (built-in)
    * a plain tool name with no server → ``None`` (no server requirement)
    """
    t = (tool or "").strip()
    if not t:
        return None
    if t.startswith("mcp__"):
        head = t[len("mcp__") :].split("__", 1)[0].strip()
        return head or None
    if t.startswith("memory_"):
        return _MEMORY_SERVER
    if "." in t:
        head = t.split(".", 1)[0].strip()
        return head or None
    return None


def needed_servers(tools_allowed: Iterable[str]) -> list[str]:
    """The MCP server names required from a ``tools_allowed`` list (sorted, unique)."""
    out: set[str] = set()
    for tool in tools_allowed or ():
        srv = server_for_tool(str(tool))
        if srv:
            out.add(srv)
    return sorted(out)


def _fold_server(name: str) -> str:
    """Loose comparison for server name matching (``ghidra-mcp`` ≈ ``ghidra_mcp``)."""
    return (name or "").strip().lower().replace("-", "").replace("_", "")


def resolve_skill_servers(
    settings: Any, tools_allowed: Iterable[str]
) -> tuple[dict[str, Any], list[str]]:
    """(selected mcp_servers payload, missing server names).

    From the available payload (built-in ``akana_memory`` + consented
    ``mcp_servers.yaml`` entries), only the servers the skill requires +
    ``akana_memory`` are selected. Required but unmounted servers fall into
    ``missing`` — they are never auto-mounted (a missing-tool signal).
    """
    available = mcp_servers_payload(settings) or {}
    by_fold = {_fold_server(name): name for name in available}
    selected: dict[str, Any] = {}
    if _MEMORY_SERVER in available:
        selected[_MEMORY_SERVER] = available[_MEMORY_SERVER]
    missing: list[str] = []
    for srv in needed_servers(tools_allowed):
        actual = by_fold.get(_fold_server(srv)) or by_fold.get(_fold_server(srv) + "mcp")
        if actual is not None:
            selected[actual] = available[actual]
        else:
            missing.append(srv)
    return selected, missing


def find_pack_persona(skill_id: str, *, roots: list[Path] | None = None) -> str | None:
    """Find the ``system_prompt`` of the persona of the pack containing the skill (None if absent).

    Pack manifests (``pack.yaml``) are scanned; the first valid persona of the
    first pack whose skills include ``skill_id`` wins (``plugins/personas/<id>.yaml``
    — the same loader as PersonasAdapter). Skills/personas are AUTO-DISCOVERED from
    the folder layout (same rule as the pack host), so a minimal ``pack.yaml`` with
    no ``contains`` block still resolves. Every error quietly falls back to ``None``:
    the persona is an enhancement and can never break the run.
    """
    try:
        from akana_server.packs.adapters import PersonasAdapter, autodiscover_contents
        from packs.contract.manifest import load_manifest
    except Exception:  # without the contract package, continue without a persona
        return None
    for root in roots if roots is not None else pack_discovery_roots():
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            manifest_path = child / "pack.yaml"
            if not child.is_dir() or not manifest_path.is_file():
                continue
            try:
                manifest = load_manifest(manifest_path)
                autodiscover_contents(manifest, child)
            except Exception:
                continue
            if skill_id not in (manifest.contains.skills or []):
                continue
            for pid in manifest.contains.personas or []:
                try:
                    data = PersonasAdapter._load(child, pid)  # noqa: SLF001 — same loader
                except Exception:
                    continue
                if isinstance(data, dict):
                    prompt = data.get("system_prompt")
                    if isinstance(prompt, str) and prompt.strip():
                        return prompt.strip()
    return None
