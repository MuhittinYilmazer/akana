"""Best-effort startup self-check for the built-in MCP servers.

At boot the backend spawns each built-in stdio MCP server (``akana_memory`` /
``akana_vault``) exactly as the providers do, runs a quick initialize + tools/list
handshake, and logs the result — so ``server.log`` answers the question the user keeps
asking ("is memory connected?") at a glance, instead of finding out only when the model
says a tool is unavailable. This is the automatic companion to ``akana.py doctor --mcp``.

Never blocks startup (the caller schedules it as a background task) and never raises.
Disable with ``AKANA_MCP_SELFCHECK=0``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from akana_server.config import Settings

log = logging.getLogger("akana_server")

_HANDSHAKE = (
    '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":'
    '"2024-11-05","capabilities":{},"clientInfo":{"name":"selfcheck","version":"1"}}}\n'
    '{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n'
).encode("utf-8")

_BUILTIN = ("akana_memory", "akana_vault")
_TIMEOUT = 20.0


def selfcheck_enabled() -> bool:
    return os.environ.get("AKANA_MCP_SELFCHECK", "1").strip().lower() not in (
        "0",
        "false",
        "off",
        "no",
    )


async def _check_one(name: str, cfg: dict) -> None:
    env = {**os.environ, **{k: str(v) for k, v in (cfg.get("env") or {}).items()}}
    try:
        proc = await asyncio.create_subprocess_exec(
            str(cfg["command"]),
            *[str(a) for a in cfg.get("args", [])],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(cfg["cwd"]) if cfg.get("cwd") else None,
        )
    except Exception as exc:  # noqa: BLE001 - self-check must never break startup
        log.warning("MCP self-check: %s could not spawn (%s)", name, exc)
        return
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(_HANDSHAKE), timeout=_TIMEOUT
        )
    except asyncio.CancelledError:
        # BUG: on server shutdown the lifespan cancels+awaits this task specifically
        # to stop the MCP subprocess it spawns, but CancelledError is a BaseException
        # (NOT Exception) so the handler below cannot catch it → the child was
        # orphaned. Kill AND reap the child before re-raising the cancellation.
        try:
            proc.kill()
        except Exception:  # noqa: BLE001 - already dead / race
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:  # noqa: BLE001 - already reaped / race
            pass
        raise
    except (TimeoutError, Exception) as exc:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        # BUG: the killed child was never awaited → zombie + open pipe FDs until the
        # asyncio child-watcher happens to reap it. Reap it now so its transport and
        # stdin/stdout/stderr pipes are closed promptly.
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:  # noqa: BLE001 - already reaped / race
            pass
        log.warning(
            "MCP self-check: %s did not handshake (%s) — its tools may be unavailable",
            name,
            type(exc).__name__,
        )
        return
    tools: list | None = None
    for line in out.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("id") == 2 and isinstance(obj.get("result"), dict):
            tools = obj["result"].get("tools", [])
    if tools is not None:
        log.info("MCP self-check: %s connected (%d tools)", name, len(tools))
    else:
        last = (err.decode("utf-8", "replace").strip().splitlines() or [""])[-1]
        log.warning("MCP self-check: %s FAILED handshake — %s", name, last[:200])


async def run_mcp_selfcheck(settings: Settings) -> None:
    """Spawn + handshake each built-in MCP server, logging connected/FAILED per server."""
    try:
        from akana_server.orchestrator.memory_tools import mcp_servers_payload

        payload = mcp_servers_payload(settings) or {}
        for name in _BUILTIN:
            cfg = payload.get(name)
            if isinstance(cfg, dict):
                await _check_one(name, cfg)
    except Exception:  # noqa: BLE001 - best-effort; never propagate to the lifespan
        log.warning("MCP self-check failed", exc_info=True)
