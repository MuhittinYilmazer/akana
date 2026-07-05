"""External MCP servers — extend Akana's tool ecosystem from one YAML file.

Akana ships its own ``akana_memory`` MCP server (see ``memory_tools``). This
module lets the owner plug in EXTERNAL servers (filesystem, fetch, github, ...)
without touching code: drop a ``mcp_servers.yaml`` into the data dir and every
chat/voice agent run receives those tools via the Cursor SDK ``mcp_servers``
payload.

``<data_dir>/mcp_servers.yaml`` example::

    servers:
      filesystem:
        type: stdio
        command: npx
        args: ["-y", "@modelcontextprotocol/server-filesystem", "/home/me/notes"]
        env:
          LOG_LEVEL: warn        # only what is written here reaches the child
        cwd: /home/me
      fetch:
        type: stdio              # if type is omitted, it is inferred from command (stdio)
        command: uvx
        args: ["mcp-server-fetch"]
      github:
        type: http               # http | sse → url required
        url: https://api.githubcopilot.com/mcp/
        headers:
          Authorization: Bearer ghp_xxx
        enabled: false           # kept in the file, skipped at runtime

Validation is forgiving by design: a broken file or entry must never take the
assistant down. Bad entries are skipped with a ``log.warning``; a broken or
missing file yields ``{}`` — this module never raises.

SECURITY: the server's (Akana's) own environment variables are NOT forwarded
AUTOMATICALLY to external MCP processes — only the ``env`` explicitly written in
the yaml reaches the child process. ``${VAR}`` / ``${VAR:-default}`` template
expansion IS SUPPORTED, but only for variables the USER wrote EXPLICITLY in the
yaml (explicit allowlist — NO blind/wholesale os.environ injection): only the
``${...}`` patterns present in the text are read from ``os.environ``. An undefined
variable with no default collapses to an EMPTY string (never raises, never leaks a
raw ``${VAR}``). Expansion is applied to all string config values: ``command``,
``args``, ``env`` values, ``cwd``, ``url``, ``headers`` values.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["CONFIG_FILENAME", "RESERVED_SERVER_NAMES", "load_external_mcp_servers"]

CONFIG_FILENAME = "mcp_servers.yaml"

#: Names Akana claims for built-in servers — colliding user entries are skipped.
RESERVED_SERVER_NAMES = frozenset({"akana_memory", "akana_vault"})

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_REMOTE_TYPES = ("http", "sse")

#: Pattern for ``${VAR}`` and ``${VAR:-default}``. Variable names are POSIX-style
#: restricted (letters/digits/underscore, must not start with a digit); the default
#: part captures everything up to the closing ``}`` (no nested ``}``). ``$$`` escape
#: is supported.
_VAR_RE = re.compile(
    r"""
    \$\$                                  # (1) $$ → literal $ (escape)
    |
    \$\{                                  # (2) ${...} block
        (?P<name>[A-Za-z_][A-Za-z0-9_]*)  #     variable name
        (?: :- (?P<default>[^}]*) )?      #     optional :-default
    \}
    """,
    re.VERBOSE,
)


def _expand_env(value: str) -> str:
    """``${VAR}`` / ``${VAR:-default}`` → ``os.environ`` (explicit allowlist).

    Only ``${...}`` patterns PRESENT in the text are read (no blind os.environ
    injection). Rules:
      * ``${VAR}``        → its value if defined; empty string if undefined.
      * ``${VAR:-def}``   → its value if defined AND non-empty; otherwise ``def``
                            (POSIX ``:-`` semantics — empty also falls back to default).
      * ``$$``            → literal ``$`` (escape; bypasses expansion).
    Never raises; unmatched ``$`` or text outside ``${...}`` is left as-is."""

    def _sub(m: "re.Match[str]") -> str:
        if m.group(0) == "$$":
            return "$"
        name = m.group("name")
        default = m.group("default")
        env_val = os.environ.get(name)
        if env_val:  # defined and non-empty
            return env_val
        # undefined OR empty → default (if provided), otherwise empty string
        return default if default is not None else ""

    return _VAR_RE.sub(_sub, value)


def load_external_mcp_servers(data_dir: Path) -> dict[str, dict[str, Any]]:
    """User-defined MCP servers from ``<data_dir>/mcp_servers.yaml``.

    Returns a ``{name: McpServerConfig}`` dict ready to merge into the Cursor
    SDK ``mcp_servers`` payload; ``{}`` when the file is missing, empty or
    broken. Never raises — see the module docstring for schema and rules.
    """
    path = Path(data_dir) / CONFIG_FILENAME
    if not path.is_file():
        return {}
    try:
        import yaml  # lazy: if PyYAML is missing, the agent run must still survive
    except ImportError:  # pragma: no cover - depends on the environment
        log.warning("%s found but PyYAML is not installed — external MCP servers skipped", path)
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # broken yaml / IO — never raise
        log.warning("could not read mcp_servers.yaml, external MCP servers skipped (%s): %s", path, exc)
        return {}
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        log.warning("mcp_servers.yaml root is not a mapping (%s) — ignored", type(raw).__name__)
        return {}
    servers_raw = raw.get("servers")
    if servers_raw is None:
        return {}
    if not isinstance(servers_raw, dict):
        log.warning("mcp_servers.yaml: 'servers' is not a mapping — ignored")
        return {}

    result: dict[str, dict[str, Any]] = {}
    for raw_name, cfg in servers_raw.items():
        name = str(raw_name)
        entry = _validate_entry(name, cfg)
        if entry is not None:
            result[name] = entry
    return result


def _validate_entry(name: str, cfg: Any) -> dict[str, Any] | None:
    """One yaml entry → Cursor ``McpServerConfig`` dict, or ``None`` (skip)."""
    if not _NAME_RE.fullmatch(name):
        log.warning(
            "invalid MCP server name: %r (expected: ^[a-zA-Z0-9_-]{1,64}$) — skipped", name
        )
        return None
    if name in RESERVED_SERVER_NAMES:
        log.warning("MCP server name %r is reserved for Akana — yaml entry skipped", name)
        return None
    if not isinstance(cfg, dict):
        log.warning("MCP server %r: definition is not a mapping — skipped", name)
        return None
    if not cfg.get("enabled", True):
        log.debug("MCP server %r: enabled=false — skipped", name)
        return None

    srv_type = cfg.get("type")
    if srv_type is None:  # if type is not given, infer it from the fields
        srv_type = "stdio" if "command" in cfg else "http" if "url" in cfg else ""
    srv_type = str(srv_type).strip().lower()
    if srv_type == "stdio":
        return _stdio_entry(name, cfg)
    if srv_type in _REMOTE_TYPES:
        return _remote_entry(name, srv_type, cfg)
    log.warning(
        "MCP server %r: unknown type %r (stdio|http|sse) — skipped", name, cfg.get("type")
    )
    return None


def _stdio_entry(name: str, cfg: dict[str, Any]) -> dict[str, Any] | None:
    command = cfg.get("command")
    if not isinstance(command, str) or not command.strip():
        log.warning("MCP server %r: 'command' is required for stdio — skipped", name)
        return None
    # Unknown fields are DELIBERATELY STRIPPED: only the Cursor SDK McpServerConfig
    # shape goes into the payload (type, command, args, env, cwd).
    entry: dict[str, Any] = {"type": "stdio", "command": _expand_env(command), "args": []}
    args = cfg.get("args")
    if isinstance(args, list):
        entry["args"] = [_expand_env(str(a)) for a in args]
    elif args is not None:
        log.warning("MCP server %r: 'args' is not a list — ignored", name)
    # SECURITY: no automatic leakage from the server environment (os.environ) —
    # only the env dict explicitly written in the yaml is forwarded to the child.
    # ``${VAR}`` / ``${VAR:-default}`` expansion is an explicit allowlist: only
    # variables present in the text are read from ``os.environ`` (see _expand_env).
    env = _str_map(name, "env", cfg.get("env"))
    if env:
        entry["env"] = env
    cwd = cfg.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        entry["cwd"] = _expand_env(cwd)
    elif cwd is not None:
        log.warning("MCP server %r: 'cwd' is not text — ignored", name)
    return entry


def _remote_entry(name: str, srv_type: str, cfg: dict[str, Any]) -> dict[str, Any] | None:
    url = cfg.get("url")
    if not isinstance(url, str) or not url.strip():
        log.warning("MCP server %r: 'url' is required for %s — skipped", name, srv_type)
        return None
    entry: dict[str, Any] = {"type": srv_type, "url": _expand_env(url)}
    headers = _str_map(name, "headers", cfg.get("headers"))
    if headers:
        entry["headers"] = headers
    return entry


def _str_map(name: str, field: str, value: Any) -> dict[str, str]:
    """Reduce an ``env`` / ``headers`` mapping to str→str; returns empty on error.

    Values are expanded for ``${VAR}`` / ``${VAR:-default}`` (explicit allowlist —
    see :func:`_expand_env`); keys are left UNCHANGED (env variable names are the
    user's intent and are not expanded)."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        log.warning("MCP server %r: '%s' is not a mapping — ignored", name, field)
        return {}
    return {str(k): _expand_env(str(v)) for k, v in value.items()}
