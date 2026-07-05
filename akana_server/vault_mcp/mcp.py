"""SecureVault MCP server — serves vault tools via stdio JSON-RPC.

Each built-in MCP server owns its own protocol loop (no shared base). Protocol:
line-delimited JSON-RPC 2.0; stdout is protocol-only, logs go to stderr.

The process is ``data_dir``-scoped, fed from ``AKANA_DATA_DIR``. The Fernet master
key is resolved by :mod:`akana_server.vault_crypto` (env ``AKANA_VAULT_KEY`` /
``AKANA_VAULT_KEYFILE`` / ``AKANA_VAULT_KEYRING``, else the default keyfile) — the
parent forwards those env vars when set so the child decrypts with the same key.

Run::

    AKANA_DATA_DIR=~/.akana python -m akana_server.vault_mcp.mcp
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, TextIO

from akana_server.vault_mcp.tools import VaultTools, vault_schemas

__all__ = ["McpServer", "mcp_tool_list", "serve", "main"]

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "akana-vault", "version": "0.1.0"}

#: Per-line size cap to prevent a runaway client from exhausting server memory.
MAX_LINE_CHARS = 4 * 1024 * 1024
#: Strict-decoding stdin should not enter an infinite UnicodeDecodeError loop.
_MAX_CONSECUTIVE_DECODE_FAILURES = 100

# JSON-RPC error codes
_PARSE_ERROR = -32700
_INVALID_REQUEST = -32600
_METHOD_NOT_FOUND = -32601
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603


def mcp_tool_list() -> list[dict[str, Any]]:
    """Vault schemas in MCP format (``inputSchema``, underscore-separated names)."""
    out: list[dict[str, Any]] = []
    for schema in vault_schemas():
        out.append(
            {
                "name": schema["name"],
                "description": schema.get("description", ""),
                "inputSchema": schema["input_schema"],
            }
        )
    return out


def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error(msg_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


class McpServer:
    """Dispatches a single JSON-RPC message; pure logic, no I/O (testable)."""

    def __init__(self, tools: VaultTools) -> None:
        self._tools = tools

    def handle(self, msg: Any) -> dict[str, Any] | None:
        """Return a response dict, or ``None`` if no response is needed."""
        if isinstance(msg, list):
            return _error(None, _INVALID_REQUEST, "batch requests are not supported")
        if not isinstance(msg, dict):
            return _error(None, _INVALID_REQUEST, "request must be a JSON object")
        method = msg.get("method")
        msg_id = msg.get("id")
        if not isinstance(method, str):
            return None  # a response or garbage — nothing to do

        if method == "initialize":
            params = msg.get("params") or {}
            requested = params.get("protocolVersion") if isinstance(params, dict) else None
            version = requested if isinstance(requested, str) and requested else PROTOCOL_VERSION
            return _result(
                msg_id,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": dict(SERVER_INFO),
                },
            )
        if method.startswith("notifications/"):
            return None
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            return _result(msg_id, {"tools": mcp_tool_list()})
        if method == "tools/call":
            return self._tools_call(msg_id, msg.get("params"))
        if "id" not in msg:
            return None  # unknown notification — ignore per JSON-RPC spec
        return _error(msg_id, _METHOD_NOT_FOUND, f"method not found: {method}")

    def _tools_call(self, msg_id: Any, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict) or not isinstance(params.get("name"), str):
            return _error(msg_id, _INVALID_PARAMS, "tools/call requires params.name")
        name = params["name"]
        arguments = params.get("arguments")
        args = arguments if isinstance(arguments, dict) else {}
        out = self._tools.handle_tool_call(name, args)
        return _result(
            msg_id,
            {
                "content": [
                    {"type": "text", "text": json.dumps(out, ensure_ascii=False)}
                ],
                "isError": "error" in out,
            },
        )


def serve(tools: VaultTools, stdin: TextIO, stdout: TextIO) -> None:
    """Line-delimited JSON-RPC loop; runs until stdin EOF.

    Adversarial input does not kill the loop: undecodable bytes → -32700, oversized
    lines and batches → -32600, handler crash → -32603 — reading continues in all cases.
    """
    server = McpServer(tools)

    def _write(obj: dict[str, Any]) -> None:
        # Defense-in-depth: a single failed protocol write must never silently kill the
        # server. main() forces UTF-8 stdout; if that fails to take, ensure_ascii=True
        # escapes non-ASCII to \uXXXX (still valid JSON-RPC) so the message goes out
        # instead of crashing. A broken pipe (client gone) is logged, not raised.
        try:
            stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
            stdout.flush()
        except UnicodeEncodeError:
            stdout.write(json.dumps(obj, ensure_ascii=True) + "\n")
            stdout.flush()
        except (BrokenPipeError, OSError) as exc:  # pragma: no cover - client closed pipe
            log.warning("mcp stdout write failed (client gone?): %s", exc)

    decode_failures = 0
    while True:
        try:
            line = stdin.readline()
        except UnicodeDecodeError:
            log.warning("dropping undecodable stdin data")
            _write(_error(None, _PARSE_ERROR, "parse error: stdin is not valid UTF-8"))
            decode_failures += 1
            if decode_failures >= _MAX_CONSECUTIVE_DECODE_FAILURES:
                log.error("stdin no longer decodable; shutting down")
                return
            continue
        decode_failures = 0
        if not line:
            return  # EOF
        if len(line) > MAX_LINE_CHARS:
            _write(_error(None, _INVALID_REQUEST, f"line too large (limit {MAX_LINE_CHARS} chars)"))
            continue
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _write(_error(None, _PARSE_ERROR, "parse error"))
            continue
        try:
            resp = server.handle(msg)
        except Exception as e:  # noqa: BLE001 — the protocol loop outlives a bad call
            log.exception("mcp handler failed")
            msg_id = msg.get("id") if isinstance(msg, dict) else None
            resp = _error(msg_id, _INTERNAL_ERROR, f"internal error: {type(e).__name__}")
        if resp is not None:
            _write(resp)


def build_tools(data_dir: Path) -> VaultTools:
    """Setup seam that ``main()`` runs and tests drive directly."""
    return VaultTools(data_dir)


def main() -> int:
    # WINDOWS CRITICAL: stdout/stderr default to the locale code page (cp1252/cp1254),
    # not UTF-8. The protocol writes JSON with ensure_ascii=False; a Turkish secret
    # value (ı/ş/ğ…) returned by vault_get would raise UnicodeEncodeError and kill the
    # server mid-stream. MCP stdio is UTF-8 by spec — force it. (Mirrors the memory MCP
    # server + the CLI's audit-M1 fix in akana_cli/main.py.)
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:  # pragma: no cover - reconfigure missing/locked → best effort
            pass
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    data_dir = Path(
        os.environ.get("AKANA_DATA_DIR") or Path.home() / ".akana"
    ).expanduser()
    tools = build_tools(data_dir)
    # Wrap binary stdin ourselves: strict decoding would crash mid-readline on a single
    # bad byte; errors="replace" turns a bad byte into U+FFFD, JSON parse fails, and
    # the client gets a -32700 instead of a dead server.
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8", errors="replace")
    log.info("akana-vault MCP serving on stdio (data_dir=%s)", data_dir)
    serve(tools, stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
