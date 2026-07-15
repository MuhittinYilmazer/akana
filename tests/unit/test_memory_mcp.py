"""MCP stdio server — the memory.* tools as the Cursor agent sees them."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from akana.memory import Memory
from akana.memory.mcp import MAX_LINE_CHARS, MCP_TO_TOOL, McpServer, mcp_tool_list, serve


@pytest.fixture()
def memory(tmp_path):
    return Memory.for_data_dir(tmp_path)


@pytest.fixture()
def server(memory):
    return McpServer(memory.make_orchestrator())


def _req(method, msg_id=1, **params):
    msg = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params:
        msg["params"] = params
    return msg


# -- protocol ---------------------------------------------------------------------


def test_initialize_echoes_protocol_version(server):
    resp = server.handle(_req("initialize", protocolVersion="2025-06-18"))
    assert resp["id"] == 1
    result = resp["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["serverInfo"]["name"] == "akana-memory"
    assert "tools" in result["capabilities"]


def test_initialized_notification_is_silent(server):
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_ping(server):
    assert server.handle(_req("ping", msg_id=7))["result"] == {}


def test_unknown_method(server):
    resp = server.handle(_req("resources/list", msg_id=9))
    assert resp["error"]["code"] == -32601
    # an unknown notification (no id) is silently swallowed
    assert server.handle({"jsonrpc": "2.0", "method": "resources/changed"}) is None


def test_tools_list_shape(server):
    resp = server.handle(_req("tools/list", msg_id=2))
    tools = resp["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {
        "memory_search",
        "memory_remember",
        "memory_forget",
    }
    search = next(t for t in tools if t["name"] == "memory_search")
    assert search["inputSchema"]["required"] == ["query"]
    assert MCP_TO_TOOL["memory_search"] == "memory.search"
    assert mcp_tool_list()[0]["description"]


# -- tools/call -------------------------------------------------------------------


def test_tools_call_search(memory, server):
    memory.assert_fact_direct(key="kedi adı", value="Pamuk", trust="user_statement")
    resp = server.handle(
        _req("tools/call", msg_id=3, name="memory_search", arguments={"query": "kedi"})
    )
    result = resp["result"]
    assert result["isError"] is False
    payload = json.loads(result["content"][0]["text"])
    assert payload["items"] and payload["explain_id"]


def test_tools_call_remember_roundtrip(memory, server):
    """With default settings, even a direct request via MCP lands in the inbox (K30 clamp)."""
    resp = server.handle(
        _req(
            "tools/call",
            msg_id=4,
            name="memory_remember",
            arguments={"content": "Pamuk", "kind": "fact", "key": "kedi adı", "policy": "direct"},
        )
    )
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["status"] == "staged"
    assert payload["requested_policy"] == "direct"
    staged = memory.staging.get(payload["staged_id"])
    assert staged is not None and staged.value == "Pamuk"


def test_tools_call_invalid_args_is_error(server):
    resp = server.handle(_req("tools/call", msg_id=5, name="memory_search", arguments={}))
    result = resp["result"]
    assert result["isError"] is True
    payload = json.loads(result["content"][0]["text"])
    assert payload["error"]["code"] == "invalid_request"


def test_tools_call_unknown_tool_is_error(server):
    resp = server.handle(_req("tools/call", msg_id=6, name="memory_nuke", arguments={}))
    assert resp["result"]["isError"] is True


def test_tools_call_missing_name(server):
    resp = server.handle(_req("tools/call", msg_id=8))
    assert resp["error"]["code"] == -32602


# -- serve loop -------------------------------------------------------------------


def test_serve_round_trip(memory):
    lines = "\n".join(
        [
            json.dumps(_req("initialize", protocolVersion="2024-11-05")),
            "bozuk json {",
            json.dumps(_req("tools/list", msg_id=2)),
        ]
    )
    out = io.StringIO()
    serve(memory.make_orchestrator(), io.StringIO(lines + "\n"), out)
    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    assert len(responses) == 3
    assert responses[0]["id"] == 1 and "result" in responses[0]
    assert responses[1]["error"]["code"] == -32700
    assert {t["name"] for t in responses[2]["result"]["tools"]} == set(MCP_TO_TOOL)


def test_serve_survives_handler_crash():
    class _Boom:
        def handle_tool_call(self, *a, **k):
            raise RuntimeError("boom")

    out = io.StringIO()
    line = json.dumps(_req("tools/call", msg_id=11, name="memory_search", arguments={"query": "x"}))
    serve(_Boom(), io.StringIO(line + "\n"), out)  # type: ignore[arg-type]
    resp = json.loads(out.getvalue().strip())
    assert resp["error"]["code"] == -32603


# -- serve loop: hostile input ------------------------------------------------------


def _serve_collect(orchestrator, stdin) -> list[dict]:
    out = io.StringIO()
    serve(orchestrator, stdin, out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_serve_invalid_utf8_then_recovers(memory):
    """A corrupt byte yields -32700; the NEXT request in the same stream responds normally."""
    raw = b'\xff\xfe{"bozuk\n' + json.dumps(_req("ping", msg_id=2)).encode() + b"\n"
    # exactly the same wrapping as main(): binary buffer + errors="replace"
    stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8", errors="replace")
    responses = _serve_collect(memory.make_orchestrator(), stdin)
    assert [r.get("id") for r in responses] == [None, 2]
    assert responses[0]["error"]["code"] == -32700
    assert responses[1]["result"] == {}


def test_serve_strict_stdin_decode_error_does_not_kill_loop(memory):
    """Even on strict stdin (no errors='replace'), a decode error does not kill the process."""
    stdin = io.TextIOWrapper(io.BytesIO(b"\xff bozuk\n"), encoding="utf-8")
    responses = _serve_collect(memory.make_orchestrator(), stdin)  # must return without raising
    assert responses and all(r["error"]["code"] == -32700 for r in responses)


def test_serve_stuck_strict_decoder_terminates(memory):
    """A half multibyte at EOF makes the strict decoder raise on every readline;
    serve must return (fail cap) without entering an infinite loop."""
    stdin = io.TextIOWrapper(io.BytesIO(b"\xc3"), encoding="utf-8")
    responses = _serve_collect(memory.make_orchestrator(), stdin)
    assert responses and all(r["error"]["code"] == -32700 for r in responses)


def test_handle_batch_array_rejected(server):
    resp = server.handle([_req("ping", msg_id=1), _req("ping", msg_id=2)])
    assert resp["id"] is None
    assert resp["error"]["code"] == -32600
    assert "batch" in resp["error"]["message"]


def test_handle_non_object_rejected(server):
    assert server.handle("merhaba")["error"]["code"] == -32600


def test_serve_batch_then_next_request(memory):
    """A batch array is not silently swallowed: a -32600 response comes back and the loop continues."""
    lines = json.dumps([_req("ping", msg_id=1)]) + "\n" + json.dumps(_req("ping", msg_id=2)) + "\n"
    responses = _serve_collect(memory.make_orchestrator(), io.StringIO(lines))
    assert responses[0]["error"]["code"] == -32600 and responses[0]["id"] is None
    assert responses[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_serve_line_too_large_then_continues(memory):
    big = '{"jsonrpc":"2.0","id":1,"method":"ping","params":{"pad":"' + "x" * MAX_LINE_CHARS + '"}}'
    lines = big + "\n" + json.dumps(_req("ping", msg_id=2)) + "\n"
    responses = _serve_collect(memory.make_orchestrator(), io.StringIO(lines))
    assert responses[0]["error"]["code"] == -32600 and responses[0]["id"] is None
    assert "line too large" in responses[0]["error"]["message"]
    assert responses[1]["result"] == {}


# -- id reflection -------------------------------------------------------------------


@pytest.mark.parametrize("msg_id", [None, 3.5, "abc", 0])
def test_response_reflects_id_exactly(server, msg_id):
    """id may be null/float/string/0 — the response reflects the same id exactly, type included."""
    resp = server.handle({"jsonrpc": "2.0", "id": msg_id, "method": "ping"})
    assert resp["id"] == msg_id
    assert type(resp["id"]) is type(msg_id)
    # unknown method: if the "id" key is PRESENT (even null), it is not left unanswered
    err = server.handle({"jsonrpc": "2.0", "id": msg_id, "method": "yok/boyle"})
    assert err["error"]["code"] == -32601
    assert err["id"] == msg_id


# -- real subprocess smoke ----------------------------------------------------------


def test_subprocess_smoke_bad_byte_then_valid(tmp_path):
    """`python -m akana.memory.mcp` (cwd=src, just as Cursor spawns it):
    a corrupt byte does not kill the process (-32700), later requests respond, exit 0 at EOF."""
    repo = Path(__file__).resolve().parents[2]
    src = repo / "src"
    env = os.environ | {"AKANA_DATA_DIR": str(tmp_path), "PYTHONPATH": str(src)}
    payload = (
        b"\xff\xfebozuk\n"
        + json.dumps({"jsonrpc": "2.0", "id": "abc", "method": "ping"}).encode() + b"\n"
        + json.dumps(_req("tools/list", msg_id=3.5)).encode() + b"\n"
    )
    proc = subprocess.run(
        [sys.executable, "-m", "akana.memory.mcp"],
        input=payload, capture_output=True, cwd=src, env=env, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    responses = [json.loads(line) for line in proc.stdout.decode().splitlines()]
    assert responses[0]["error"]["code"] == -32700
    assert responses[1] == {"jsonrpc": "2.0", "id": "abc", "result": {}}
    assert responses[2]["id"] == 3.5
    assert {t["name"] for t in responses[2]["result"]["tools"]} == set(MCP_TO_TOOL)


def test_subprocess_survives_cp1252_stdout(tmp_path):
    """Windows regression (the real root cause of 'memory MCP won't connect'): stdout
    defaults to a non-UTF-8 code page (cp1252/cp1254). The protocol writes
    ensure_ascii=False JSON and the tool descriptions contain Turkish characters (ı/ş/ğ),
    so the FIRST tools/list used to raise UnicodeEncodeError and kill the server
    mid-handshake → the MCP client was stuck 'connecting' forever. PYTHONIOENCODING=cp1252
    reproduces that stdout on ANY OS; main() must reconfigure to UTF-8 (with an ASCII
    fallback in _write) so tools/list still goes out and the process exits cleanly."""
    repo = Path(__file__).resolve().parents[2]
    src = repo / "src"
    # Guard the test's own premise: a tool description MUST contain a non-ASCII char,
    # else cp1252 would never have crashed and this test would be vacuous.
    blob = json.dumps(mcp_tool_list(), ensure_ascii=False)
    assert any(ord(c) > 127 for c in blob), "tool schemas are pure ASCII — test is vacuous"
    env = os.environ | {
        "AKANA_DATA_DIR": str(tmp_path),
        "PYTHONPATH": str(src),
        "PYTHONIOENCODING": "cp1252",  # simulate the Windows console/pipe code page
    }
    payload = (
        json.dumps(_req("initialize", msg_id=1, protocolVersion="2024-11-05")).encode() + b"\n"
        + json.dumps(_req("tools/list", msg_id=2)).encode() + b"\n"
    )
    proc = subprocess.run(
        [sys.executable, "-m", "akana.memory.mcp"],
        input=payload, capture_output=True, cwd=src, env=env, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    assert b"UnicodeEncodeError" not in proc.stderr
    by_id = {
        r.get("id"): r
        for r in (json.loads(ln) for ln in proc.stdout.decode("utf-8").splitlines() if ln.strip())
    }
    assert 2 in by_id, "tools/list response missing — server crashed on cp1252 stdout"
    assert {t["name"] for t in by_id[2]["result"]["tools"]} == set(MCP_TO_TOOL)


# -- server glue (mcpServers config) ------------------------------------------------


def test_memory_mcp_servers_config(monkeypatch, tmp_path):
    from akana_server.orchestrator.memory_tools import memory_mcp_servers

    monkeypatch.delenv("AKANA_MEMORY_TOOLS", raising=False)
    cfg = memory_mcp_servers(SimpleNamespace(data_dir=tmp_path))
    assert cfg is not None
    entry = cfg["akana_memory"]
    assert entry["type"] == "stdio"
    assert entry["command"] == sys.executable
    # Standalone launcher FILE (cwd/PYTHONPATH/shadowing-immune), not `-m akana.memory.mcp`.
    assert len(entry["args"]) == 1
    assert entry["args"][0].endswith(str(Path("scripts") / "mcp_memory.py"))
    assert entry["env"]["AKANA_DATA_DIR"] == str(tmp_path)
    assert "PYTHONPATH" not in entry["env"]  # no longer needed
    assert "cwd" not in entry  # the launcher is cwd-immune


def test_memory_mcp_servers_disabled(monkeypatch, tmp_path):
    from akana_server.orchestrator.memory_tools import memory_mcp_servers

    monkeypatch.setenv("AKANA_MEMORY_TOOLS", "0")
    # The built-in akana_vault + akana_schedule must be off too;
    # otherwise the payload is not None even with memory off alone (each is a
    # separate built-in server).
    monkeypatch.setenv("AKANA_VAULT_TOOLS", "0")
    monkeypatch.setenv("AKANA_SCHEDULE_TOOLS", "0")
    assert memory_mcp_servers(SimpleNamespace(data_dir=tmp_path)) is None
