"""In-process MCP client bridge — external MCP server tools for native function-calling.

The cursor/claude providers reach external MCP servers (filesystem, fetch, github, …
declared in ``mcp_servers.yaml``) because their CLIs speak MCP natively. The
native-function-calling providers (ollama/gemini/openai) do NOT speak MCP — they take an
OpenAI ``tools=[...]`` array. This module bridges the gap: it connects to the configured
external MCP servers IN-PROCESS (official ``mcp`` SDK), lists their tools, exposes them as
OpenAI tool declarations (namespaced ``mcp__<server>__<tool>``) and dispatches calls back.

Built-in ``akana_memory`` / ``akana_vault`` are NOT bridged here: the native providers
already dispatch memory/vault in-process (``gemini_tools`` / ``vault_tools``). Only the
EXTERNAL servers (:func:`mcp_config.load_external_mcp_servers`) flow through this bridge, so
a user with no ``mcp_servers.yaml`` pays ZERO cost — no subprocess, no ``mcp`` import.

DEFENSIVE by design (mirrors ``mcp_config`` / ``gemini_tools``): a server that fails to
start, a missing ``mcp`` package, a tool that errors mid-call — none of these break the
turn. Failures are logged and skipped; a tool error is returned to the model as text.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from akana_server.orchestrator.mcp_config import load_external_mcp_servers

if TYPE_CHECKING:
    from akana_server.config import Settings

log = logging.getLogger(__name__)

#: Per-request read timeout for the MCP session (initialize / list_tools / call_tool).
#: External servers cold-start (``npx`` may fetch a package); a generous cap avoids
#: killing a slow-but-healthy server while still bounding a truly hung one.
_READ_TIMEOUT = timedelta(seconds=60)

#: ``mcp__<server>__<tool>`` — the namespace prefix that marks a bridged tool so the
#: provider routes its dispatch here instead of to the in-process native tools.
TOOL_PREFIX = "mcp__"


def _to_openai_decl(qualified_name: str, tool: Any) -> dict[str, Any]:
    """One MCP ``Tool`` → an OpenAI tools entry (the shape ollama/gemini/openai consume).

    ``Tool.inputSchema`` is already a JSON schema (``{type,properties,required}``) → carried
    as the OpenAI ``parameters`` as-is. A missing/invalid schema falls back to an empty
    object schema so the declaration is always well-formed."""
    schema = getattr(tool, "inputSchema", None)
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": qualified_name,
            "description": getattr(tool, "description", "") or "",
            "parameters": schema,
        },
    }


def _stringify_result(result: Any) -> str:
    """``CallToolResult`` → text the model reads. Joins text content blocks; never raises.

    MCP results carry a list of content blocks (text/image/resource). We surface the text
    blocks (the model can't consume binary here). ``isError`` results still return their
    text — it's the error message for the model to reason about, not an exception."""
    try:
        content = getattr(result, "content", None) or []
        parts: list[str] = []
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str) and text:
                parts.append(text)
        out = "\n".join(parts).strip()
        if out:
            return out
        return "(tool returned no text content)"
    except Exception:  # pragma: no cover - a malformed result must not break the turn
        return "(could not read tool result)"


class McpToolBridge:
    """Connects to external MCP servers and exposes their tools as OpenAI declarations.

    Use as an async context manager bound to a single chat turn::

        async with McpToolBridge(servers) as bridge:
            tools = NATIVE_DECLS + bridge.decls
            ...                              # run the function-calling loop
            result = await bridge.dispatch("mcp__filesystem__read_file", {"path": ...})

    With an empty ``servers`` mapping it is a pure no-op: no ``mcp`` import, no subprocess,
    empty ``decls``."""

    def __init__(self, servers: dict[str, dict[str, Any]] | None) -> None:
        self._servers = servers or {}
        self._stack: AsyncExitStack | None = None
        self._decls: list[dict[str, Any]] = []
        self._routes: dict[str, tuple[Any, str]] = {}  # qualified name → (session, tool)

    @property
    def decls(self) -> list[dict[str, Any]]:
        """OpenAI tool declarations for every successfully-listed external tool ([] = none)."""
        return self._decls

    async def __aenter__(self) -> "McpToolBridge":
        if not self._servers:
            return self  # zero-cost no-op (no yaml entries)
        try:
            import mcp  # noqa: F401 — availability probe only
        except ImportError:
            log.warning(
                "mcp_servers.yaml has entries but the 'mcp' package is not installed — "
                "external MCP tools skipped (pip install mcp)"
            )
            return self
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        try:
            for name, cfg in self._servers.items():
                try:
                    await self._add_server(name, cfg)
                except Exception:  # one bad server must not sink the others / the turn
                    log.warning("MCP server %r failed to start — skipped", name, exc_info=True)
        except BaseException:
            # A BaseException (e.g. CancelledError on STOP/timeout mid-setup) escapes
            # __aenter__, so Python never calls __aexit__ → any transports already entered
            # on the stack (spawned MCP subprocesses) would leak. Tear the stack down here
            # before re-raising so those subprocesses are closed.
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        if self._stack is not None:
            try:
                await self._stack.__aexit__(*exc)
            except Exception:  # pragma: no cover - teardown noise must not mask the turn
                log.warning("error while closing MCP sessions", exc_info=True)
            self._stack = None
        return False

    async def _add_server(self, name: str, cfg: dict[str, Any]) -> None:
        """Open a session, ``initialize``, list tools and register them under ``mcp__name__*``."""
        session = await self._open_session(cfg)
        await session.initialize()
        listed = await session.list_tools()
        for tool in getattr(listed, "tools", None) or []:
            tname = getattr(tool, "name", "") or ""
            if not tname:
                continue
            qualified = f"{TOOL_PREFIX}{name}__{tname}"
            self._decls.append(_to_openai_decl(qualified, tool))
            self._routes[qualified] = (session, tname)

    async def _open_session(self, cfg: dict[str, Any]) -> Any:
        """Open the transport + ``ClientSession`` for one server config (entered on the stack)."""
        from mcp import ClientSession, StdioServerParameters

        assert self._stack is not None
        typ = str(cfg.get("type") or "stdio").lower()
        if typ == "stdio":
            from mcp.client.stdio import stdio_client

            params = StdioServerParameters(
                command=cfg["command"],
                args=list(cfg.get("args") or []),
                env=cfg.get("env"),
                cwd=cfg.get("cwd"),
            )
            read, write = await self._stack.enter_async_context(stdio_client(params))
        elif typ == "sse":
            from mcp.client.sse import sse_client

            read, write = await self._stack.enter_async_context(
                sse_client(cfg["url"], headers=cfg.get("headers"))
            )
        elif typ == "http":
            from mcp.client.streamable_http import streamablehttp_client

            streams = await self._stack.enter_async_context(
                streamablehttp_client(cfg["url"], headers=cfg.get("headers"))
            )
            read, write = streams[0], streams[1]  # (read, write, get_session_id)
        else:  # pragma: no cover - mcp_config already validates the type
            raise ValueError(f"unsupported MCP server type: {typ!r}")

        return await self._stack.enter_async_context(
            ClientSession(read, write, read_timeout_seconds=_READ_TIMEOUT)
        )

    def handles(self, name: str) -> bool:
        """Whether ``name`` is a bridged (``mcp__…``) tool this bridge can dispatch."""
        return name in self._routes

    async def dispatch(self, name: str, args: dict[str, Any] | None) -> str:
        """Call a bridged tool → text for the model. DEFENSIVE: every failure → clean text."""
        route = self._routes.get(name)
        if route is None:
            return f"Unknown tool: {name}"
        session, tool = route
        try:
            result = await session.call_tool(tool, arguments=args or {})
            return _stringify_result(result)
        except Exception:  # network/protocol/timeout — never break the turn
            log.warning("MCP tool %r dispatch error", name, exc_info=True)
            return "The tool is unavailable right now."


def external_mcp_bridge(settings: Settings) -> McpToolBridge:
    """Bridge for the user's external MCP servers (``<data_dir>/mcp_servers.yaml``).

    A missing/empty/broken yaml yields a no-op bridge (no subprocess, no ``mcp`` import,
    zero cost) — see :class:`McpToolBridge`."""
    try:
        servers = load_external_mcp_servers(settings.data_dir)
    except Exception:  # pragma: no cover - loader is already defensive
        log.warning("could not load external MCP servers", exc_info=True)
        servers = {}
    return McpToolBridge(servers)


__all__ = ["McpToolBridge", "TOOL_PREFIX", "external_mcp_bridge"]
