"""BridgePool wire protocol — the NDJSON request line sent to bridge_daemon.mjs.

No real Node process is spawned: ``asyncio.create_subprocess_exec`` is replaced
with a fake whose stdout is a pre-fed ``asyncio.StreamReader`` (the same type the
pool reads in production) and whose stdin records every byte written. The last
test smoke-runs the real daemon with ``node`` for ops that need no CURSOR_API_KEY.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from akana_server.config import Settings, load_settings
from akana_server.orchestrator.bridge_pool import (
    BridgePool,
    _died_midresponse_error,
    _friendly_bridge_error,
    _stderr_has_known_cause,
)
from akana_server.orchestrator.cursor_provider import build_payload as _build_payload
from akana_server.orchestrator.llm_dispatch import LLMCallError
from akana_server.orchestrator.llm_process import executable_argv

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DAEMON = _REPO_ROOT / "cursor_bridge" / "bridge_daemon.mjs"

# Shape mirrors orchestrator.memory_tools.memory_mcp_servers() — the production payload.
MCP_SERVERS: dict[str, Any] = {
    "akana_memory": {
        "type": "stdio",
        "command": "/usr/bin/python3",
        "args": ["-m", "akana.memory.mcp"],
        "env": {"PYTHONPATH": "/repo/src", "AKANA_DATA_DIR": "/data"},
        "cwd": "/repo/src",
    }
}


class _FakeStdin:
    def __init__(self) -> None:
        self.data = b""

    def write(self, chunk: bytes) -> None:
        self.data += chunk

    async def drain(self) -> None:
        return None

    def lines(self) -> list[dict[str, Any]]:
        return [json.loads(ln) for ln in self.data.decode("utf-8").splitlines() if ln.strip()]


class _FakeProc:
    pid = 4242
    returncode: int | None = None

    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.stdout = asyncio.StreamReader()
        self.stderr = None

    def feed(self, *events: dict[str, Any], eof: bool = True) -> None:
        for ev in events:
            self.stdout.feed_data((json.dumps(ev) + "\n").encode("utf-8"))
        if eof:
            self.stdout.feed_eof()


def _make_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path, *, timeout: str = "5"
) -> Settings:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CURSOR_API_KEY", "test-key-123")
    monkeypatch.setenv("CURSOR_MODEL", "composer-2")
    monkeypatch.setenv("CURSOR_BRIDGE_TIMEOUT", timeout)
    return load_settings()


def _fake_pool(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> tuple[BridgePool, _FakeProc, dict[str, Any]]:
    """BridgePool whose subprocess spawn is captured instead of executed."""
    proc = _FakeProc()
    spawned: dict[str, Any] = {}

    async def _fake_spawn(*cmd: str, **kwargs: Any):
        spawned["cmd"] = list(cmd)
        spawned["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    return BridgePool(settings), proc, spawned


_PONG = {"id": "ping", "ev": "pong"}


def test_run_request_line_carries_payload_verbatim(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Production payload (built by llm_dispatch._build_payload) must reach the
    daemon unchanged, with only id/op/stream envelope fields added."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        pool, proc, spawned = _fake_pool(monkeypatch, settings)
        proc.feed(
            _PONG,
            {"id": "1", "ev": "timing", "phase": "agent_ready_ms", "ms": 12, "reused": "create"},
            {"id": "1", "ev": "meta", "agent_id": "agent-xyz"},
            {"id": "1", "ev": "delta", "text": "mer"},
            {"id": "1", "ev": "delta", "text": "haba"},
            {
                "id": "1",
                "ev": "done",
                "ok": True,
                "text": "merhaba",
                "status": "finished",
                "usage": {"inputTokens": 3, "outputTokens": 5},
                "agent_id": "agent-xyz",
            },
        )

        payload = _build_payload(
            settings,
            "merhaba dünya",
            history=[{"role": "user", "content": "selam"}],
            model="claude-haiku-4-5",
            stream=True,
            chat_mode=True,
            conversation_id="conv-42",
            agent_id="agent-prev",
            reuse_agent=True,
            mcp_servers=MCP_SERVERS,
        )
        events = [ev async for ev in pool.stream_run(payload)]

        # --- daemon spawn command/env (no node actually ran) ---
        # ``executable_argv`` is a no-op on POSIX but resolves ``node`` → the full
        # ``node.exe`` path on Windows (PATHEXT-aware); assert against the same
        # transform the production ``_daemon_args`` applies so this holds on every OS.
        assert spawned["cmd"] == executable_argv(
            ["node", str(settings.bridge_dir / "bridge_daemon.mjs")]
        )
        assert spawned["kwargs"]["cwd"] == str(settings.bridge_dir)
        assert spawned["kwargs"]["env"]["CURSOR_API_KEY"] == "test-key-123"

        # --- request lines written to daemon stdin ---
        sent = proc.stdin.lines()
        assert sent[0] == {"id": "ping", "op": "ping"}
        body = sent[1]
        # Verbatim pass-through: payload + envelope, nothing dropped or renamed.
        assert body == {**payload, "id": "1", "op": "run", "stream": True}
        assert body["op"] == "run"
        assert body["stream"] is True
        assert body["prompt"] == "merhaba dünya"  # Turkish literal preserved — asserted value
        assert body["model"] == "claude-haiku-4-5"
        assert body["cwd"] == str(settings.workspace)  # unified workspace cwd (no chat sandbox)
        assert body["session_key"] == "conv-42"
        assert body["conversation_id"] == "conv-42"
        assert body["cursor_agent_id"] == "agent-prev"
        assert body["reuse_agent"] is True
        assert body["history"] == [{"role": "user", "content": "selam"}]
        assert isinstance(body["system"], str) and body["system"].strip()
        # CRITICAL: mcp_servers must survive byte-for-byte (memory tools depend on it).
        assert body["mcp_servers"] == MCP_SERVERS
        # ensure_ascii=False — non-ASCII text goes over the wire as UTF-8, not \uXXXX.
        raw_line = proc.stdin.data.decode("utf-8").splitlines()[1]
        assert "merhaba dünya" in raw_line

        # --- events surfaced to the chat route ---
        assert {"timing": {"phase": "agent_ready_ms", "ms": 12, "reused": "create"}} in events
        assert {"agent_id": "agent-xyz"} in events
        deltas = [e["delta"] for e in events if "delta" in e]
        assert deltas == ["mer", "haba"]
        final = events[-1]
        assert final["done"] is True
        assert final["text"] == "merhaba"
        assert final["status"] == "finished"
        assert final["agent_id"] == "agent-xyz"
        # daemon done.usage: bridge_pool calls ``_usage_to_tokens(usage)`` plain (opt-out)
        # → does NOT carry cost (backward-compat; same contract as token-coercion tests).
        # Cost is only added on the DIRECT ``stream_user_chat`` path (model is passed).
        # Daemon-path cost gap is documented in the CUR-4 report.
        usage = final["usage"]
        assert usage["prompt_tokens"] == 3
        assert usage["completion_tokens"] == 5
        assert usage["tool_calls"] == []
        assert usage["cache_read_tokens"] == 0
        assert usage["cache_write_tokens"] == 0
        assert "cost_usd" not in usage

        tool_events = [e for e in events if "tool_call" in e]
        assert tool_events == []

    asyncio.run(run())


def test_payload_language_follows_runtime_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The cursor bridge frames prior turns in the ACTIVE ``language`` — that label
    rides on the payload, so ``_build_payload`` must stamp the current ``language``
    runtime setting (en|tr). A hardcoded Turkish frame made English-mode multi-turn
    chats reply in Turkish (mirrors ``claude_provider._HISTORY_FRAME``); this locks
    the toggle so switching the setting at runtime switches the framing language."""
    from akana_server.runtime_settings import get_store

    settings = _make_settings(monkeypatch, tmp_path)
    monkeypatch.delenv("AKANA_LANGUAGE", raising=False)

    def _lang() -> str:
        return _build_payload(
            settings,
            "hi",
            history=[{"role": "user", "content": "prev"}],
            model="composer-2",
            stream=True,
            chat_mode=True,
            conversation_id="c1",
            system_prompt="SYS",
        )["language"]

    # Nothing stored, no env → English-first default.
    assert _lang() == "en"
    # Runtime setting switched to Turkish → payload follows.
    get_store(tmp_path).set("language", "tr")
    assert _lang() == "tr"
    # Switched back to English at runtime → payload follows again (the toggle).
    get_store(tmp_path).set("language", "en")
    assert _lang() == "en"


def test_stream_run_surfaces_tool_call_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Bridge tool events must preserve toolName from Cursor SDK shape."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        pool, proc, _ = _fake_pool(monkeypatch, settings)
        proc.feed(
            _PONG,
            {
                "id": "1",
                "ev": "tool",
                "phase": "start",
                "call_id": "tc-1",
                "name": "grep",
                "args": {"pattern": "foo"},
            },
            {
                "id": "1",
                "ev": "tool",
                "phase": "end",
                "call_id": "tc-1",
                "name": "grep",
                "status": "ok",
            },
            {"id": "1", "ev": "done", "ok": True, "text": "ok", "status": "finished"},
        )
        events = [ev async for ev in pool.stream_run({"prompt": "p"})]
        tools = [e["tool_call"] for e in events if "tool_call" in e]
        assert len(tools) == 2
        assert tools[0]["name"] == "grep"
        assert tools[1]["name"] == "grep"

    asyncio.run(run())


def test_stream_run_forwards_thinking_heartbeat_activity(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Non-terminal bridge events (thinking/heartbeat/activity) reach the chat route."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        pool, proc, _ = _fake_pool(monkeypatch, settings)
        proc.feed(
            _PONG,
            {"id": "1", "ev": "thinking", "phase": "delta", "text": "planlıyorum…"},
            {"id": "1", "ev": "activity", "kind": "shell", "text": "line 1\n"},
            {"id": "1", "ev": "heartbeat", "phase": "run_wait"},
            {"id": "1", "ev": "done", "ok": True, "text": "bitti", "status": "finished"},
        )
        events = [ev async for ev in pool.stream_run({"prompt": "p"})]
        assert {"thinking": {"phase": "delta", "text": "planlıyorum…"}} in events
        assert {
            "activity": {"kind": "shell", "phase": None, "text": "line 1\n"}
        } in events
        assert {"activity": {"kind": "heartbeat", "phase": "run_wait"}} in events
        assert events[-1]["done"] is True

    asyncio.run(run())


def test_build_payload_omits_optional_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """No conversation/agent/mcp_servers → optional keys absent; #10: session_key
    is a UNIQUE one-shot (never the shared 'default') + reuse_agent is forced off."""
    settings = _make_settings(monkeypatch, tmp_path)
    payload = _build_payload(
        settings,
        "hi",
        history=None,
        model=None,
        stream=True,
        chat_mode=False,
        conversation_id=None,
        agent_id=None,
        reuse_agent=True,
        mcp_servers=None,
    )
    assert "mcp_servers" not in payload
    assert "conversation_id" not in payload
    assert "cursor_agent_id" not in payload
    assert "system" not in payload  # only added in chat_mode
    # #10: even without conversation/agent, never falls back to 'default' — unique oneshot + reuse off.
    assert payload["session_key"].startswith("oneshot:")
    assert payload["reuse_agent"] is False
    assert payload["cwd"] == str(settings.workspace)
    assert payload["model"] == "composer-2"  # falls back to settings.cursor_model
    assert payload["history"] == []


def test_build_payload_session_key_three_way(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """#10: session_key avoids 'default' collision — three paths + uniqueness.

    (1) If conv_id present, key==conv_id (reuse preserved).
    (2) If no conv_id but agent present, key=='agent:<id>' (stable reuse).
    (3) If neither, key=='oneshot:<uuid>' + reuse_agent=False, and two separate
        calls get DIFFERENT keys (so concurrent memory calls are not serialized).
    """
    settings = _make_settings(monkeypatch, tmp_path)

    def build(**kw: Any) -> dict[str, Any]:
        return _build_payload(
            settings, "hi", history=None, model=None, stream=True,
            chat_mode=False, mcp_servers=None, **kw,
        )

    # (1) conv_id → key == conv_id, reuse preserved.
    p_conv = build(conversation_id="conv-7", agent_id=None, reuse_agent=True)
    assert p_conv["session_key"] == "conv-7"
    assert p_conv["conversation_id"] == "conv-7"
    assert p_conv["reuse_agent"] is True

    # (2) no conv, agent present → key fixed to 'agent:', reuse preserved, agent_id passed.
    p_agent = build(conversation_id=None, agent_id="ag-9", reuse_agent=True)
    assert p_agent["session_key"] == "agent:ag-9"
    assert p_agent["cursor_agent_id"] == "ag-9"
    assert p_agent["reuse_agent"] is True
    assert "conversation_id" not in p_agent

    # (3) neither → unique oneshot + reuse off; two calls get different keys.
    p1 = build(conversation_id=None, agent_id=None, reuse_agent=True)
    p2 = build(conversation_id=None, agent_id=None, reuse_agent=True)
    assert p1["session_key"].startswith("oneshot:")
    assert p2["session_key"].startswith("oneshot:")
    assert p1["session_key"] != p2["session_key"]  # uniqueness: NO serialization
    assert p1["reuse_agent"] is False and p2["reuse_agent"] is False


def test_stream_run_ignores_foreign_request_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        pool, proc, _ = _fake_pool(monkeypatch, settings)
        proc.feed(
            _PONG,
            {"id": "99", "ev": "delta", "text": "YANLIŞ"},
            {"id": "1", "ev": "delta", "text": "doğru"},
            {"id": "1", "ev": "done", "ok": True, "text": "doğru", "status": "finished"},
        )
        events = [ev async for ev in pool.stream_run({"prompt": "p"})]
        deltas = [e["delta"] for e in events if "delta" in e]
        assert deltas == ["doğru"]
        assert events[-1]["text"] == "doğru"

    asyncio.run(run())


def test_stream_run_error_event_raises_cursor_call_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        pool, proc, _ = _fake_pool(monkeypatch, settings)
        proc.feed(_PONG, {"id": "1", "ev": "error", "ok": False, "error": "boom"})
        with pytest.raises(LLMCallError, match="boom"):
            async for _ in pool.stream_run({"prompt": "p"}):
                pass

    asyncio.run(run())


def test_done_event_completes_stream_without_eof(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path, timeout="0.4")

    async def run() -> None:
        pool, proc, _ = _fake_pool(monkeypatch, settings)
        # NOTE eof=False: a persistent daemon never closes stdout between turns.
        proc.feed(
            _PONG,
            {"id": "1", "ev": "done", "ok": True, "text": "bitti", "status": "finished"},
            eof=False,
        )
        events = [ev async for ev in pool.stream_run({"prompt": "p"})]
        assert events and events[-1].get("done") is True
        assert events[-1]["text"] == "bitti"

    asyncio.run(run())


def test_daemon_death_mid_stream_raises_error_and_restarts(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """stdout EOF + no terminal event = daemon died mid-response.

    Old behavior produced a fake "empty success" ({"done": True, "text": ""}) —
    user sees an empty reply with no error. New behavior: LLMCallError with
    explanation + dead process is cleaned up; next turn opens a fresh daemon.
    """
    settings = _make_settings(monkeypatch, tmp_path)

    class _MortalProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__()
            self.killed = False

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            return self.returncode or 0

    procs: list[_MortalProc] = []

    async def _fake_spawn(*cmd: str, **kwargs: Any):
        proc = _MortalProc()
        procs.append(proc)
        if len(procs) == 1:
            # First daemon: EOF after one delta (crashed) — no done/error.
            proc.feed(_PONG, {"id": "1", "ev": "delta", "text": "yarım"})
        else:
            # Second daemon is healthy; persistent process does not close stdout.
            # NOTE: rid counter lives in the pool → second request uses "2".
            proc.feed(
                _PONG,
                {"id": "2", "ev": "done", "ok": True, "text": "tam", "status": "finished"},
                eof=False,
            )
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    pool = BridgePool(settings)

    async def run() -> None:
        deltas: list[str] = []
        with pytest.raises(LLMCallError) as exc_info:
            async for ev in pool.stream_run({"prompt": "p"}):
                if "delta" in ev:
                    deltas.append(ev["delta"])
        assert deltas == ["yarım"]  # streamed portion reached the user (partial-save)
        assert "closed mid-response" in exc_info.value.message  # explanation in error message
        assert "send your message again" in exc_info.value.message
        assert exc_info.value.status_code == 503
        assert procs[0].killed  # dead process was cleaned up

        # Next turn: pool opens a fresh daemon and completes normally.
        events = [ev async for ev in pool.stream_run({"prompt": "p2"})]
        assert len(procs) == 2
        assert events[-1]["done"] is True
        assert events[-1]["text"] == "tam"

    asyncio.run(run())


def test_friendly_bridge_error_uses_structural_hints() -> None:
    """The daemon's normalizeError now carries error_code + HTTP status; an auth
    failure (status 401) must map to the clear "authentication failed" message even
    though the raw text alone is ambiguous. Regression for BUG A: agent-creation auth
    errors now reach the consumer WITH the real id and these structural hints."""
    msg = _friendly_bridge_error(
        {"error": "Invalid User API Key", "error_code": "error", "status": 401}
    )
    assert "authentication failed" in msg.lower()
    assert "api key" in msg.lower()


def test_died_midresponse_surfaces_known_stderr_cause() -> None:
    """A daemon that CRASHED leaving an auth error on stderr surfaces THAT, not the
    opaque generic line (the bridge pool now keeps a stderr tail)."""
    tail = [
        "akana bridge_daemon ready",
        "akana bridge_daemon uncaughtException: Error: Invalid User API Key",
    ]
    msg = _died_midresponse_error(1, tail)
    assert "authentication failed" in msg.lower()
    assert "closed mid-response" not in msg  # the real cause replaced the generic line


def test_died_midresponse_generic_keeps_contract_and_adds_diagnostics() -> None:
    """No recognizable cause → keep the original advice (next turn opens a fresh
    daemon) AND append the exit code + last stderr line for diagnosis."""
    msg = _died_midresponse_error(-9, ["some benign node warning"])
    assert "closed mid-response" in msg
    assert "send your message again" in msg
    assert "exit code -9" in msg
    assert "some benign node warning" in msg
    # Empty tail + unknown exit: bare generic message, no trailing noise.
    bare = _died_midresponse_error(None, [])
    assert "closed mid-response" in bare
    assert "exit code" not in bare
    assert "Last bridge output" not in bare


def test_stderr_known_cause_is_conservative() -> None:
    assert _stderr_has_known_cause("TypeError: Invalid User API Key")
    assert _stderr_has_known_cause("Error: Cannot find module '@cursor/sdk'")
    assert _stderr_has_known_cause("connect ECONNREFUSED 127.0.0.1:443")
    assert not _stderr_has_known_cause("akana bridge_daemon ready")
    assert not _stderr_has_known_cause("some unrelated warning about deprecation")


def test_abort_run_writes_abort_op(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        pool, proc, _ = _fake_pool(monkeypatch, settings)
        pool._proc = proc
        await pool.abort_run("conv-99")
        assert proc.stdin.lines() == [
            {"id": "abort", "op": "abort_run", "session_key": "conv-99"}
        ]

    asyncio.run(run())


def test_close_session_writes_close_op(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        pool, proc, _ = _fake_pool(monkeypatch, settings)
        pool._proc = proc  # daemon already "running"
        await pool.close_session("conv-42")
        assert proc.stdin.lines() == [
            {"id": "close", "op": "close_session", "session_key": "conv-42"}
        ]

    asyncio.run(run())


@pytest.mark.skipif(
    shutil.which("node") is None
    or not (_DAEMON.parent / "node_modules" / "@cursor" / "sdk").is_dir(),
    reason="node or @cursor/sdk not installed",
)
def test_bridge_daemon_node_smoke_no_api_key() -> None:
    """Real daemon over stdio: ping/unknown-op/empty-prompt paths need no CURSOR_API_KEY."""
    requests = [
        {"id": "t1", "op": "ping"},
        {"id": "t2", "op": "bogus"},
        {"id": "t3", "op": "run", "prompt": "   "},  # guard fires before SDK use
        {"id": "t5", "op": "abort_run", "session_key": "conv-smoke"},
        {"id": "t6", "op": "abort_run"},  # no explicit key → do not abort the default session
        {"id": "t4", "op": "shutdown"},  # guarantees process exit
    ]
    stdin = "".join(json.dumps(r) + "\n" for r in requests).encode("utf-8")
    proc = subprocess.run(
        ["node", str(_DAEMON)],
        input=stdin,
        capture_output=True,
        timeout=30,
        env={**os.environ, "CURSOR_API_KEY": ""},
        cwd=str(_DAEMON.parent),
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")[:400]
    out = [
        json.loads(ln)
        for ln in proc.stdout.decode("utf-8").splitlines()
        if ln.strip().startswith("{")
    ]
    by_id = {e.get("id"): e for e in out}
    assert by_id["t1"]["ev"] == "pong"
    assert by_id["t2"]["ev"] == "error"
    assert "unknown op" in by_id["t2"]["error"]
    assert by_id["t3"]["ev"] == "error"
    assert by_id["t3"]["error"] == "empty prompt"
    assert by_id["t5"]["ev"] == "pong"
    assert by_id["t5"]["aborted"] == "conv-smoke"
    assert by_id["t5"]["had_run"] is False
    assert by_id["t6"]["ev"] == "pong"
    assert by_id["t6"]["aborted"] in (None, "")
    assert by_id["t6"]["had_run"] is False
    assert "bridge_daemon ready" in proc.stderr.decode("utf-8", errors="replace")


def test_daemon_restarts_when_runtime_key_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """When the key changes via the dashboard, the daemon must not keep running with the old env."""
    from akana_server.secret_store import set_secrets

    settings = _make_settings(monkeypatch, tmp_path)

    class _RotProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__()
            self.killed = False
            self.feed(_PONG, eof=False)

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            return self.returncode or 0

    procs: list[_RotProc] = []
    envs: list[dict[str, Any]] = []

    async def _fake_spawn(*cmd: str, **kwargs: Any):
        proc = _RotProc()
        procs.append(proc)
        envs.append(kwargs.get("env") or {})
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    pool = BridgePool(settings)

    async def run() -> None:
        first = await pool._ensure_proc()
        # Second call with the same key preserves the process.
        assert await pool._ensure_proc() is first
        assert len(procs) == 1
        assert envs[0]["CURSOR_API_KEY"] == "test-key-123"

        set_secrets(tmp_path, {"cursor_api_key": "rotated-key-456"})
        second = await pool._ensure_proc()
        assert second is not first
        assert procs[0].killed
        assert len(procs) == 2
        assert envs[1]["CURSOR_API_KEY"] == "rotated-key-456"

    asyncio.run(run())


def test_key_rotation_deferred_while_run_active(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """b12: a cursor key change must NOT kill the shared daemon while a run is streaming on it —
    that would EOF every OTHER conversation's in-flight stream ('closed mid-response'). The
    rotation is deferred until no run queue is active, then the next turn respawns on the new key."""
    from akana_server.secret_store import set_secrets

    settings = _make_settings(monkeypatch, tmp_path)

    class _RotProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__()
            self.killed = False
            self.feed(_PONG, eof=False)

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            return self.returncode or 0

    procs: list[_RotProc] = []

    async def _fake_spawn(*cmd: str, **kwargs: Any):
        proc = _RotProc()
        procs.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    pool = BridgePool(settings)

    async def run() -> None:
        first = await pool._ensure_proc()
        # Simulate an ACTIVE run stream on the shared daemon (a numeric rid queue).
        pool._register_queue("1")
        set_secrets(tmp_path, {"cursor_api_key": "rotated-key-456"})
        # Key changed BUT a run is active → the daemon must be PRESERVED (deferred rotation).
        same = await pool._ensure_proc()
        assert same is first, "rotation must be deferred while a run is active"
        assert not procs[0].killed
        assert len(procs) == 1
        # The run finishes → its queue is released → the next turn respawns on the new key.
        pool._release_queue("1")
        second = await pool._ensure_proc()
        assert second is not first, "rotation must happen once the daemon is idle"
        assert procs[0].killed
        assert len(procs) == 2

    asyncio.run(run())


# -- Quality pass: stdout boundary cases (half JSON line, garbage line, corrupt usage) --


def test_stream_run_reassembles_half_json_line_across_feeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """If the daemon writes an NDJSON line in two chunks (TCP fragmentation),
    ``_read_bridge_line`` reads until ``\\n`` and reassembles the line completely;
    a partial line is never dropped with ``JSONDecodeError``."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        pool, proc, _ = _fake_pool(monkeypatch, settings)
        proc.stdout.feed_data((json.dumps(_PONG) + "\n").encode("utf-8"))
        # the delta line is split into two chunks — NO newline in between.
        proc.stdout.feed_data(b'{"id":"1","ev":"delta","text":"Mer')
        proc.stdout.feed_data(b'haba"}\n')
        proc.stdout.feed_data(
            (json.dumps({"id": "1", "ev": "done", "text": "Merhaba", "status": "finished"}) + "\n").encode("utf-8")
        )
        proc.stdout.feed_eof()
        events = [ev async for ev in pool.stream_run({"prompt": "p"})]
        deltas = [e["delta"] for e in events if "delta" in e]
        assert deltas == ["Merhaba"]
        assert events[-1]["text"] == "Merhaba"

    asyncio.run(run())


def test_stream_run_skips_garbage_line_mid_stream(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A non-JSON line arriving MID-STREAM is skipped; the turn continues."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        pool, proc, _ = _fake_pool(monkeypatch, settings)
        proc.stdout.feed_data((json.dumps(_PONG) + "\n").encode("utf-8"))
        proc.stdout.feed_data(
            (json.dumps({"id": "1", "ev": "delta", "text": "a"}) + "\n").encode("utf-8")
        )
        proc.stdout.feed_data(b"bu satir hic JSON degil <<<\n")  # garbage
        proc.stdout.feed_data(b"\n")  # empty line
        proc.stdout.feed_data(
            (json.dumps({"id": "1", "ev": "delta", "text": "b"}) + "\n").encode("utf-8")
        )
        proc.stdout.feed_data(
            (json.dumps({"id": "1", "ev": "done", "text": "ab", "status": "finished"}) + "\n").encode("utf-8")
        )
        proc.stdout.feed_eof()
        events = [ev async for ev in pool.stream_run({"prompt": "p"})]
        deltas = [e["delta"] for e in events if "delta" in e]
        assert deltas == ["a", "b"]
        assert events[-1]["text"] == "ab"

    asyncio.run(run())


def test_stream_run_bad_usage_value_does_not_crash_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Corrupt usage in a ``done`` event (float-string token) must not crash the turn.

    The old ``int(...)`` path would raise ``ValueError`` here and swallow the ``done``
    event — user would see an empty/hanging reply. Now it safely rounds down to 0."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        pool, proc, _ = _fake_pool(monkeypatch, settings)
        proc.feed(
            _PONG,
            {
                "id": "1",
                "ev": "done",
                "text": "bitti",
                "status": "finished",
                "usage": {"inputTokens": "12.5", "outputTokens": "x"},
            },
        )
        events = [ev async for ev in pool.stream_run({"prompt": "p"})]
        done = events[-1]
        assert done["done"] is True
        assert done["text"] == "bitti"
        assert done["usage"]["prompt_tokens"] == 12  # float-string rounded down
        assert done["usage"]["completion_tokens"] == 0  # nonsense string → 0

    asyncio.run(run())


# -- BUG 2: concurrent multi-conversation (different agents run in parallel) ---------------


def test_two_conversations_stream_concurrently(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Two DIFFERENT conversations run CONCURRENTLY over the same daemon.

    The old design held a single global ``asyncio.Lock`` for the entire stream →
    the second conversation was blocked until the first finished (and in production
    Cursor's "already has active run" could surface). With the new demultiplexer
    two streams are multiplexed by ID: deltas interleave, both complete WITHOUT
    an "active run" error.
    """
    settings = _make_settings(monkeypatch, tmp_path, timeout="5")

    async def run() -> None:
        pool, proc, _ = _fake_pool(monkeypatch, settings)
        proc.stdout.feed_data((json.dumps(_PONG) + "\n").encode("utf-8"))

        started = asyncio.Event()

        async def feeder() -> None:
            # Wait until both rids ("1" and "2") have started.
            await started.wait()
            await asyncio.sleep(0)
            # INTERLEAVED deltas: daemon multiplexes two runs.
            for ev in (
                {"id": "1", "ev": "delta", "text": "A1"},
                {"id": "2", "ev": "delta", "text": "B1"},
                {"id": "1", "ev": "delta", "text": "A2"},
                {"id": "2", "ev": "delta", "text": "B2"},
                {"id": "2", "ev": "done", "text": "B1B2", "status": "finished"},
                {"id": "1", "ev": "done", "text": "A1A2", "status": "finished"},
            ):
                proc.stdout.feed_data((json.dumps(ev) + "\n").encode("utf-8"))
                await asyncio.sleep(0)

        async def consume(payload: dict[str, Any]) -> list[dict[str, Any]]:
            started.set()
            return [ev async for ev in pool.stream_run(payload)]

        feeder_task = asyncio.create_task(feeder())
        a_events, b_events = await asyncio.gather(
            consume({"prompt": "pa", "session_key": "conv-A"}),
            consume({"prompt": "pb", "session_key": "conv-B"}),
        )
        await feeder_task

        a_deltas = [e["delta"] for e in a_events if "delta" in e]
        b_deltas = [e["delta"] for e in b_events if "delta" in e]
        assert a_deltas == ["A1", "A2"]
        assert b_deltas == ["B1", "B2"]
        # Both received a terminal done; NO "active run" error.
        assert a_events[-1]["done"] is True and a_events[-1]["text"] == "A1A2"
        assert b_events[-1]["done"] is True and b_events[-1]["text"] == "B1B2"
        # Only one daemon was spawned (pool is shared).
        assert proc.stdin.lines()[0] == {"id": "ping", "op": "ping"}

    asyncio.run(run())


def test_active_run_error_maps_to_friendly_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """If "already has active run" still arrives, the raw error does NOT leak → user-facing message."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        pool, proc, _ = _fake_pool(monkeypatch, settings)
        proc.feed(
            _PONG,
            {
                "id": "1",
                "ev": "error",
                "ok": False,
                "error": "Agent abc-123 already has active run",
            },
            {
                "id": "2",
                "ev": "error",
                "ok": False,
                "error": "Agent abc-123 already has active run",
            },
        )
        with pytest.raises(LLMCallError) as exc:
            async for _ in pool.stream_run({"prompt": "p", "session_key": "conv-x"}):
                pass
        assert "already in progress" in exc.value.message
        assert "already has active run" not in exc.value.message  # raw error is hidden
        abort_ops = [
            ln
            for ln in proc.stdin.lines()
            if ln.get("op") == "abort_run" and ln.get("session_key") == "conv-x"
        ]
        assert abort_ops, "abort_run expected after active-run error"

    asyncio.run(run())


def test_active_run_retries_after_abort_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """First attempt gets active-run → abort_run → second attempt succeeds."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        pool, proc, _ = _fake_pool(monkeypatch, settings)
        proc.feed(
            _PONG,
            {
                "id": "1",
                "ev": "error",
                "ok": False,
                "error": "Agent abc-123 already has active run",
            },
            {"id": "2", "ev": "timing", "phase": "agent_ready_ms", "ms": 1, "reused": "create"},
            {"id": "2", "ev": "meta", "agent_id": "agent-new"},
            {"id": "2", "ev": "done", "ok": True, "text": "tamam", "status": "finished"},
        )
        events = [ev async for ev in pool.stream_run({"prompt": "p", "session_key": "conv-y"})]
        assert events[-1]["done"] is True
        assert events[-1]["text"] == "tamam"

    asyncio.run(run())


def test_stream_run_broken_pipe_on_write_raises_503_and_releases_queue(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """C1: the daemon dies right as we hand it the request → the stdin write/drain
    raises ``BrokenPipeError``. That must surface as a 503 ``LLMCallError`` (not a raw
    OSError bubbling out of the generator) AND the rid queue must be RELEASED, not leaked."""
    from akana_server.network.guard import reset_global_registry

    reset_global_registry()
    settings = _make_settings(monkeypatch, tmp_path)

    class _BrokenStdin(_FakeStdin):
        def __init__(self) -> None:
            super().__init__()
            self.drains = 0

        async def drain(self) -> None:
            self.drains += 1
            # drain #1 is the post-spawn ping (must succeed); #2 is the run write.
            if self.drains >= 2:
                raise BrokenPipeError("daemon pipe gone")
            return None

    class _BrokenProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__()
            self.stdin = _BrokenStdin()
            self.returncode = 1  # daemon already exited

        def kill(self) -> None:  # pragma: no cover - not reached on this path
            self.returncode = -9

        async def wait(self) -> int:
            return self.returncode or 0

    async def _fake_spawn(*cmd: str, **kwargs: Any):
        proc = _BrokenProc()  # built inside the loop (StreamReader needs a running loop)
        proc.feed(_PONG, eof=False)  # ping answers; persistent stdout stays open
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    pool = BridgePool(settings)

    async def run() -> None:
        with pytest.raises(LLMCallError) as exc:
            async for _ in pool.stream_run({"prompt": "p", "session_key": "conv-c1"}):
                pass
        assert exc.value.status_code == 503
        # The rid queue for this request must not linger after the failed write.
        assert pool._queues == {}, "rid queue leaked after broken-pipe write"

    asyncio.run(run())


def test_failed_post_spawn_ping_tears_down_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """C2: a freshly spawned daemon that never answers its ping (here: stdout EOF instead
    of a pong) must be TORN DOWN — group-killed + ``self._proc`` cleared — not left
    assigned and leaking, so the next turn spawns a clean daemon."""
    from akana_server.network.guard import reset_global_registry

    reset_global_registry()
    settings = _make_settings(monkeypatch, tmp_path)

    killed: dict[str, int] = {}

    async def _fake_terminate(pid: int) -> None:
        killed["pid"] = pid

    monkeypatch.setattr(
        "akana_server.orchestrator.bridge_pool.terminate_process_group", _fake_terminate
    )

    class _PingProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__()
            self.killed = False

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            return self.returncode or 0

    holder: dict[str, _PingProc] = {}

    async def _fake_spawn(*cmd: str, **kwargs: Any):
        proc = _PingProc()  # built inside the loop (StreamReader needs a running loop)
        proc.stdout.feed_eof()  # no pong → the ping read sees EOF → ping fails
        holder["proc"] = proc
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    pool = BridgePool(settings)

    async def run() -> None:
        with pytest.raises(LLMCallError):
            await pool._ensure_proc()
        proc = holder["proc"]
        assert killed.get("pid") == proc.pid  # the wedged daemon's group was killed
        assert proc.killed
        assert pool._proc is None  # not left assigned (no leak, no reuse of a dead daemon)

    asyncio.run(run())


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process-group kill (os.killpg); Windows uses taskkill via llm_process._IS_WIN",
)
def test_aclose_kills_daemon_process_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """BUG 1: pool.aclose() kills the daemon as a process group + cleans up the pid."""
    settings = _make_settings(monkeypatch, tmp_path)

    killpg_calls: list[int] = []
    monkeypatch.setattr(
        "akana_server.orchestrator.llm_process.os.killpg",
        lambda pgid, sig: killpg_calls.append(pgid),
    )

    class _LiveProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__()
            self.killed = False
            self.feed(_PONG, eof=False)

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            return self.returncode or 0

    holder: dict[str, _LiveProc] = {}

    async def _fake_spawn(*cmd: str, **kwargs: Any):
        proc = _LiveProc()
        holder["proc"] = proc
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    pool = BridgePool(settings)

    async def run() -> None:
        await pool._ensure_proc()  # start daemon + write pid file
        proc = holder["proc"]
        from akana_server.orchestrator.llm_process import llm_pid_dir

        pid_files = list(llm_pid_dir(tmp_path).glob("*.json"))
        assert len(pid_files) == 1  # cursor_bridge pid record written

        await pool.aclose()
        # shutdown op + killpg (proc.pid=4242) called.
        assert 4242 in killpg_calls
        assert proc.killed
        # pid file cleaned up.
        assert list(llm_pid_dir(tmp_path).glob("*.json")) == []

    asyncio.run(run())
