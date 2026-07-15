"""codex_provider — the OpenAI ``codex exec`` CLI provider (ChatGPT-subscription auth).

Hermetic, exactly like ``test_claude_provider``: no real ``codex`` binary runs —
``asyncio.create_subprocess_exec`` is replaced with a fake whose stdout is a pre-fed
``asyncio.StreamReader`` feeding scripted ``codex exec --json`` JSONL events, whose stdin
captures the prompt, and whose argv/env are captured for assertion.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from akana_server.config import Settings, load_settings
from akana_server.orchestrator import codex_provider
from akana_server.orchestrator.errors import LLMCallError

# The Akana MCP payload shape (memory_tools.mcp_servers_payload) the dispatcher passes in.
MCP_SERVERS: dict[str, Any] = {
    "akana_memory": {
        "type": "stdio",
        "command": "/usr/bin/python3",
        "args": ["/repo/scripts/mcp_memory.py"],
        "env": {"AKANA_DATA_DIR": "/data"},
    }
}


class _FakeStdin:
    """Captures what the provider writes to the child's stdin (the prompt)."""

    def __init__(self) -> None:
        self.data = b""
        self.closed = False

    def write(self, b: bytes) -> None:
        self.data += b

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakeProc:
    pid = 7777
    returncode: int | None = 0

    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.stdin = _FakeStdin()

    def feed(self, *events: dict[str, Any], eof: bool = True) -> None:
        for ev in events:
            self.stdout.feed_data((json.dumps(ev) + "\n").encode("utf-8"))
        if eof:
            self.stdout.feed_eof()

    async def wait(self) -> int:
        return self.returncode or 0

    def kill(self) -> None:  # pragma: no cover - only on timeout path
        self.returncode = -9


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Settings:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CURSOR_API_KEY", "test-key-123")
    return load_settings()


def _patch_spawn(monkeypatch: pytest.MonkeyPatch, proc: _FakeProc) -> dict[str, Any]:
    """Replace create_subprocess_exec + neutralise the Windows shim wrap so argv is
    exactly what ``_build_args`` produced; capture argv/kwargs, return the fake proc."""
    captured: dict[str, Any] = {}

    async def _fake_spawn(*cmd: str, **kwargs: Any):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    # On a host that happens to have a ``codex.cmd`` shim on PATH, executable_argv would
    # wrap argv[0] with ``cmd /c`` → argv assertions would shift. Force identity so the
    # argv is deterministic across dev machines.
    monkeypatch.setattr(codex_provider, "executable_argv", lambda a: a)
    monkeypatch.setattr(codex_provider, "needs_cmd_wrapper", lambda _b: False)
    return captured


# --------------------------------------------------------------------------- #
# Scripted JSONL events
# --------------------------------------------------------------------------- #
def _thread(tid: str = "th-abc") -> dict[str, Any]:
    return {"type": "thread.started", "thread_id": tid}


def _agent_msg(text: str, iid: str = "m1", completed: bool = True) -> dict[str, Any]:
    return {
        "type": "item.completed" if completed else "item.updated",
        "item": {"id": iid, "type": "agent_message", "text": text},
    }


def _reasoning(text: str, iid: str = "r1", completed: bool = True) -> dict[str, Any]:
    return {
        "type": "item.completed" if completed else "item.updated",
        "item": {"id": iid, "type": "reasoning", "text": text},
    }


def _cmd_start(iid: str = "c1", command: str = "ls -la") -> dict[str, Any]:
    return {
        "type": "item.started",
        "item": {"id": iid, "type": "command_execution", "command": command, "status": "in_progress"},
    }


def _cmd_done(iid: str = "c1", output: str = "files", exit_code: int = 0) -> dict[str, Any]:
    return {
        "type": "item.completed",
        "item": {
            "id": iid,
            "type": "command_execution",
            "command": "ls -la",
            "aggregated_output": output,
            "exit_code": exit_code,
            "status": "completed" if exit_code == 0 else "failed",
        },
    }


def _mcp_start(iid: str = "t1") -> dict[str, Any]:
    return {
        "type": "item.started",
        "item": {
            "id": iid,
            "type": "mcp_tool_call",
            "server": "akana_memory",
            "tool": "memory_search",
            "arguments": {"q": "x"},
            "status": "in_progress",
        },
    }


def _mcp_done(iid: str = "t1") -> dict[str, Any]:
    return {
        "type": "item.completed",
        "item": {
            "id": iid,
            "type": "mcp_tool_call",
            "server": "akana_memory",
            "tool": "memory_search",
            "result": {"content": [{"type": "text", "text": "found"}]},
            "status": "completed",
        },
    }


def _turn_completed(usage: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "turn.completed",
        "usage": usage
        or {"input_tokens": 11, "cached_input_tokens": 3, "output_tokens": 7},
    }


# --------------------------------------------------------------------------- #
# Event → wire mapping
# --------------------------------------------------------------------------- #
def test_stream_user_chat_yields_full_sequence(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(
            _thread("th-abc"),
            {"type": "turn.started"},
            _mcp_start("t1"),
            _mcp_done("t1"),
            _agent_msg("merhaba"),
            _turn_completed(),
        )
        _patch_spawn(monkeypatch, proc)

        events = [
            ev
            async for ev in codex_provider.stream_user_chat(
                settings, "selam", mcp_servers=MCP_SERVERS
            )
        ]

        assert events[0] == {"agent_id": "th-abc"}
        deltas = [e["delta"] for e in events if "delta" in e]
        assert deltas == ["merhaba"]

        tool_events = [e["tool_call"] for e in events if "tool_call" in e]
        start = next(t for t in tool_events if t["phase"] == "start")
        end = next(t for t in tool_events if t["phase"] == "end")
        assert start["id"] == "t1" and start["name"] == "mcp__akana_memory__memory_search"
        assert start["args"] == {"q": "x"}
        assert end["id"] == "t1" and end["status"] == "ok"

        final = events[-1]
        assert final["done"] is True
        assert final["text"] == "merhaba"
        assert final["status"] == "finished"
        assert final["usage"] == {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "tool_calls": [],
            "cache_read_tokens": 3,
        }
        assert any(tc["id"] == "t1" for tc in final["tool_calls"])

    asyncio.run(run())


def test_complete_chat_collects_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_thread(), _agent_msg("merhaba"), _turn_completed())
        _patch_spawn(monkeypatch, proc)
        text, status, raw = await codex_provider.complete_chat(settings, "selam")
        assert text == "merhaba"
        assert status == "finished"
        assert raw["usage"]["prompt_tokens"] == 11

    asyncio.run(run())


def test_agent_message_streamed_progressively(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Cumulative item.updated frames → incremental deltas (text diffed by item id)."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(
            _thread(),
            _agent_msg("mer", completed=False),
            _agent_msg("merhaba", completed=False),
            _agent_msg("merhaba dünya", completed=True),
            _turn_completed(),
        )
        _patch_spawn(monkeypatch, proc)
        events = [ev async for ev in codex_provider.stream_user_chat(settings, "selam")]
        deltas = [e["delta"] for e in events if "delta" in e]
        assert deltas == ["mer", "haba", " dünya"]
        assert events[-1]["text"] == "merhaba dünya"

    asyncio.run(run())


def test_reasoning_becomes_thinking_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(
            _thread(),
            _reasoning("düşün", completed=False),
            _reasoning("düşünüyorum", completed=True),
            _agent_msg("cevap"),
            _turn_completed(),
        )
        _patch_spawn(monkeypatch, proc)
        events = [ev async for ev in codex_provider.stream_user_chat(settings, "selam")]
        thinking = [e["thinking"] for e in events if "thinking" in e]
        assert thinking[0] == {"phase": "delta", "text": "düşün"}
        assert thinking[1] == {"phase": "delta", "text": "üyorum"}
        assert thinking[-1] == {"phase": "completed"}
        # Reasoning text never leaks into the answer.
        deltas = [e["delta"] for e in events if "delta" in e]
        assert deltas == ["cevap"]

    asyncio.run(run())


def test_command_execution_tool_cards(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(
            _thread(),
            _cmd_start("c1", "ls -la"),
            _cmd_done("c1", "a\nb", exit_code=0),
            _agent_msg("done"),
            _turn_completed(),
        )
        _patch_spawn(monkeypatch, proc)
        events = [ev async for ev in codex_provider.stream_user_chat(settings, "selam")]
        cards = [e["tool_call"] for e in events if "tool_call" in e]
        start = next(c for c in cards if c["phase"] == "start")
        end = next(c for c in cards if c["phase"] == "end")
        assert start["name"] == "shell" and start["args"] == {"command": "ls -la"}
        assert end["result"] == "a\nb" and end["status"] == "ok"

    asyncio.run(run())


def test_failed_command_marks_error_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(
            _thread(),
            _cmd_start("c1"),
            _cmd_done("c1", "boom", exit_code=1),
            _agent_msg("ok"),
            _turn_completed(),
        )
        _patch_spawn(monkeypatch, proc)
        events = [ev async for ev in codex_provider.stream_user_chat(settings, "selam")]
        end = next(
            e["tool_call"] for e in events if e.get("tool_call", {}).get("phase") == "end"
        )
        assert end["status"] == "error"

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# argv wiring: resume, MCP overrides, sandbox, effort, prompt-on-stdin
# --------------------------------------------------------------------------- #
def test_resume_argv_present(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_thread(), _agent_msg("ok"), _turn_completed())
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in codex_provider.stream_user_chat(
            settings, "selam", agent_id="th-prev", reuse_agent=True
        ):
            pass
        argv = captured["cmd"]
        assert argv[:4] == [argv[0], "exec", "resume", "th-prev"]

    asyncio.run(run())


def test_no_resume_when_reuse_false(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_thread(), _agent_msg("ok"), _turn_completed())
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in codex_provider.stream_user_chat(
            settings, "selam", agent_id="th-prev", reuse_agent=False
        ):
            pass
        assert "resume" not in captured["cmd"]

    asyncio.run(run())


def test_json_and_stdin_prompt(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """--json is set, the prompt is delivered on stdin (``-`` positional), NEVER on argv."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_thread(), _agent_msg("ok"), _turn_completed())
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in codex_provider.stream_user_chat(settings, "gizli-soru"):
            pass
        argv = captured["cmd"]
        assert "--json" in argv
        assert argv[-1] == "-"  # stdin sentinel is the last positional
        assert "gizli-soru" not in "\x00".join(argv)  # prompt never on argv
        assert b"gizli-soru" in proc.stdin.data  # prompt on stdin
        assert proc.stdin.closed

    asyncio.run(run())


def test_mcp_config_wired_as_c_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Akana's MCP payload → repeatable ``-c mcp_servers.<name>.<key>=<toml>`` overrides."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_thread(), _agent_msg("ok"), _turn_completed())
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in codex_provider.stream_user_chat(
            settings, "selam", mcp_servers=MCP_SERVERS
        ):
            pass
        argv = captured["cmd"]
        joined = "\x00".join(argv)
        assert 'mcp_servers.akana_memory.command="/usr/bin/python3"' in joined
        assert 'mcp_servers.akana_memory.args=["/repo/scripts/mcp_memory.py"]' in joined
        # Non-secret env inlined for determinism.
        assert 'mcp_servers.akana_memory.env.AKANA_DATA_DIR="/data"' in joined
        # Every -c override is passed as its own [-c, value] token pair.
        assert argv.count("-c") >= 3

    asyncio.run(run())


def test_mcp_secret_env_kept_off_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A secret-bearing MCP env value (vault master key) must NOT ride argv (ps/tasklist
    leak) — it is forwarded to the child through the process environment instead."""
    settings = _make_settings(monkeypatch, tmp_path)
    servers = {
        "akana_vault": {
            "type": "stdio",
            "command": "/usr/bin/python3",
            "args": ["/repo/scripts/mcp_vault.py"],
            "env": {"AKANA_DATA_DIR": "/data", "AKANA_VAULT_KEY": "MASTER-KEY-SECRET"},
        }
    }

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_thread(), _agent_msg("ok"), _turn_completed())
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in codex_provider.stream_user_chat(
            settings, "selam", mcp_servers=servers
        ):
            pass
        argv = captured["cmd"]
        # The master key never appears on the command line…
        assert "MASTER-KEY-SECRET" not in "\x00".join(argv)
        assert "AKANA_VAULT_KEY" not in "\x00".join(argv)
        # …but is forwarded through the child's inherited process environment.
        assert captured["kwargs"]["env"]["AKANA_VAULT_KEY"] == "MASTER-KEY-SECRET"
        # The non-secret data dir is still inlined for determinism.
        assert 'mcp_servers.akana_vault.env.AKANA_DATA_DIR="/data"' in "\x00".join(argv)

    asyncio.run(run())


def test_external_mcp_openai_key_never_reaches_codex_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """COST GUARD: an external MCP server that forwards its own OPENAI_API_KEY must NOT
    re-introduce that key into the codex process env after the strip — otherwise the CLI
    would silently switch from the ChatGPT-subscription session to API-key billing. The
    MCP-env merge is re-stripped for the auth denylist."""
    settings = _make_settings(monkeypatch, tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    servers = {
        "some_pack_server": {
            "type": "stdio",
            "command": "/usr/bin/node",
            "args": ["/pack/server.js"],
            # A very common case: an external MCP tool needs its own OpenAI key.
            "env": {"OPENAI_API_KEY": "sk-from-pack", "CODEX_API_KEY": "sk-codex-pack"},
        }
    }

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_thread(), _agent_msg("ok"), _turn_completed())
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in codex_provider.stream_user_chat(
            settings, "selam", mcp_servers=servers
        ):
            pass
        env = captured["kwargs"]["env"]
        # The auth-defeating keys are gone from the codex env even though the MCP
        # server forwarded them → codex stays on the subscription session.
        assert "OPENAI_API_KEY" not in env
        assert "CODEX_API_KEY" not in env

    asyncio.run(run())


def test_sandbox_full_tools_default_bypass(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)
    monkeypatch.delenv("AKANA_CLAUDE_FULL_TOOLS", raising=False)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_thread(), _agent_msg("ok"), _turn_completed())
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in codex_provider.stream_user_chat(settings, "selam"):
            pass
        argv = captured["cmd"]
        assert "--dangerously-bypass-approvals-and-sandbox" in argv
        assert "--sandbox" not in argv
        assert "--skip-git-repo-check" in argv

    asyncio.run(run())


def test_sandbox_readonly_when_full_tools_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from akana_server.llm_settings import update_llm_settings

    settings = _make_settings(monkeypatch, tmp_path)
    monkeypatch.delenv("AKANA_CLAUDE_FULL_TOOLS", raising=False)
    update_llm_settings(settings.data_dir, settings, {"claude_full_tools": False})

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_thread(), _agent_msg("ok"), _turn_completed())
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in codex_provider.stream_user_chat(settings, "selam"):
            pass
        argv = captured["cmd"]
        assert argv[argv.index("--sandbox") + 1] == "read-only"
        assert "--dangerously-bypass-approvals-and-sandbox" not in argv

    asyncio.run(run())


def test_thinking_mode_sets_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def _effort_for(mode: str | None) -> str | None:
        proc = _FakeProc()
        proc.feed(_thread(), _agent_msg("ok"), _turn_completed())
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in codex_provider.stream_user_chat(
            settings, "selam", thinking_mode=mode
        ):
            pass
        argv = captured["cmd"]
        for i, tok in enumerate(argv):
            if tok == "-c" and argv[i + 1].startswith("model_reasoning_effort="):
                return argv[i + 1].split("=", 1)[1]
        return None

    async def run() -> None:
        # Akana canonical tiers (claude/gemini vocabulary) still resolve.
        assert await _effort_for("hizli") == '"low"'
        assert await _effort_for("normal") == '"medium"'
        assert await _effort_for("derin") == '"high"'
        assert await _effort_for(None) is None
        # Codex NATIVE levels pass through verbatim (the composer sends these when
        # codex is active — no Akana-tier mapping). ``xhigh`` is native-only: no
        # canonical tier reaches it, only the native composer selection does.
        assert await _effort_for("minimal") == '"minimal"'
        assert await _effort_for("low") == '"low"'
        assert await _effort_for("medium") == '"medium"'
        assert await _effort_for("high") == '"high"'
        assert await _effort_for("xhigh") == '"xhigh"'

    asyncio.run(run())


def test_env_hygiene_strips_api_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-leak")
    monkeypatch.setenv("CODEX_API_KEY", "sk-codex-leak")
    monkeypatch.setenv("AKANA_TOKEN", "bearer-leak")

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_thread(), _agent_msg("ok"), _turn_completed())
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in codex_provider.stream_user_chat(settings, "selam"):
            pass
        env = captured["kwargs"]["env"]
        # API-key vars stripped → the CLI uses the ChatGPT OAuth session, not a key.
        assert "OPENAI_API_KEY" not in env
        assert "CODEX_API_KEY" not in env
        assert "CURSOR_API_KEY" not in env
        assert "AKANA_TOKEN" not in env

    asyncio.run(run())


def test_resolve_model_foreign_tags_never_leak(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)
    # A real Codex tag is used as-is.
    assert codex_provider._resolve_codex_model(settings, "gpt-5.4-codex") == "gpt-5.4-codex"
    # Foreign tags (cursor/claude/plain openai) never reach the codex CLI → default.
    for foreign in ("composer-2", "claude-sonnet-4-6", "gpt-5.4", "gemini-3-flash", ""):
        resolved = codex_provider._resolve_codex_model(settings, foreign)
        assert "codex" in resolved


def test_model_flag_on_argv(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_thread(), _agent_msg("ok"), _turn_completed())
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in codex_provider.stream_user_chat(
            settings, "selam", model="gpt-5-codex-mini"
        ):
            pass
        argv = captured["cmd"]
        assert argv[argv.index("-m") + 1] == "gpt-5-codex-mini"

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #
def test_missing_binary_maps_to_install_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(codex_provider, "executable_argv", lambda a: a)
    monkeypatch.setattr(codex_provider, "needs_cmd_wrapper", lambda _b: False)

    async def _raise_fnf(*cmd: str, **kwargs: Any):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _raise_fnf)

    async def run() -> None:
        with pytest.raises(LLMCallError) as exc:
            async for _ in codex_provider.stream_user_chat(settings, "selam"):
                pass
        msg = str(exc.value)
        assert exc.value.status_code == 503
        assert "Codex CLI not found" in msg
        assert "codex login" in msg

    asyncio.run(run())


def test_turn_failed_auth_maps_to_503_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.returncode = 1
        proc.feed(
            _thread(),
            {"type": "turn.failed", "error": {"message": "Not logged in. Please run codex login."}},
        )
        _patch_spawn(monkeypatch, proc)
        with pytest.raises(LLMCallError) as exc:
            async for _ in codex_provider.stream_user_chat(settings, "selam"):
                pass
        assert exc.value.status_code == 503
        assert "codex login" in str(exc.value)

    asyncio.run(run())


def test_error_event_generic_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.returncode = 1
        proc.feed(_thread(), {"type": "error", "message": "something broke downstream"})
        _patch_spawn(monkeypatch, proc)
        with pytest.raises(LLMCallError) as exc:
            async for _ in codex_provider.stream_user_chat(settings, "selam"):
                pass
        assert "something broke downstream" in str(exc.value)

    asyncio.run(run())


def test_subprocess_death_mid_stream_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """CLI dies mid-response (EOF, no turn.completed, rc!=0) → raises, not fake success;
    the deltas streamed up to that point still reached the user."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.returncode = 1
        proc.feed(_thread(), _agent_msg("yarım"))  # eof=True, NO turn.completed
        _patch_spawn(monkeypatch, proc)
        deltas: list[str] = []
        with pytest.raises(LLMCallError):
            async for ev in codex_provider.stream_user_chat(settings, "selam"):
                if "delta" in ev:
                    deltas.append(ev["delta"])
        assert deltas == ["yarım"]

    asyncio.run(run())


def test_corrupt_jsonl_line_is_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.stdout.feed_data((json.dumps(_thread()) + "\n").encode("utf-8"))
        proc.stdout.feed_data(b"{bozuk json %%%\n")
        proc.stdout.feed_data(b'"a string not a dict"\n')
        proc.feed(_agent_msg("merhaba"), _turn_completed(), eof=False)
        proc.stdout.feed_eof()
        _patch_spawn(monkeypatch, proc)
        events = [ev async for ev in codex_provider.stream_user_chat(settings, "selam")]
        assert [e["delta"] for e in events if "delta" in e] == ["merhaba"]
        assert events[-1]["done"] is True

    asyncio.run(run())


def test_bad_usage_value_does_not_crash_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(
            _thread(),
            _agent_msg("merhaba"),
            _turn_completed(
                {"input_tokens": "9.9", "output_tokens": "bozuk", "cached_input_tokens": [1]}
            ),
        )
        _patch_spawn(monkeypatch, proc)
        events = [ev async for ev in codex_provider.stream_user_chat(settings, "selam")]
        final = events[-1]
        assert final["usage"]["prompt_tokens"] == 9  # float-string rounded down
        assert final["usage"]["completion_tokens"] == 0  # nonsense → 0
        assert final["usage"]["cache_read_tokens"] == 0  # list → 0

    asyncio.run(run())


def test_timeout_kills_process_group(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A hung stream (no terminal event) is cut off at the idle ceiling → 504 + killpg."""
    settings = _make_settings(monkeypatch, tmp_path)
    from akana_server.orchestrator.llm_process import llm_pid_dir

    monkeypatch.setattr(codex_provider.base, "idle_timeout", lambda _s: 0.2)
    killed: list[int] = []

    async def _fake_term(pid: int, **_kw: Any) -> None:
        killed.append(pid)

    monkeypatch.setattr(codex_provider, "terminate_process_group", _fake_term)

    async def run() -> None:
        proc = _FakeProc()
        # thread.started only, then the stream hangs (no eof, no terminal).
        proc.stdout.feed_data((json.dumps(_thread()) + "\n").encode("utf-8"))
        _patch_spawn(monkeypatch, proc)
        with pytest.raises(LLMCallError, match="timed out"):
            async for _ in codex_provider.stream_user_chat(settings, "selam"):
                pass
        assert 7777 in killed  # the process group was killed
        # pid file cleaned up on the way out.
        assert list(llm_pid_dir(tmp_path).glob("*.json")) == []

    asyncio.run(run())


def test_spawned_in_own_session_and_pid_registered(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)
    from akana_server.orchestrator.llm_process import llm_pid_dir

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_thread(), _agent_msg("ok"), _turn_completed())
        captured = _patch_spawn(monkeypatch, proc)
        events = [ev async for ev in codex_provider.stream_user_chat(settings, "selam")]
        assert captured["kwargs"]["start_new_session"] is True
        assert events[-1]["done"] is True
        assert list(llm_pid_dir(tmp_path).glob("*.json")) == []  # cleaned on exit

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Dispatch routing: llm_dispatch resolves "codex" → codex_provider
# --------------------------------------------------------------------------- #
def test_dispatch_routes_codex_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from akana_server.orchestrator import llm_dispatch

    settings = _make_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(llm_dispatch, "_active_provider", lambda *_a, **_k: "codex")

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_thread(), _agent_msg("merhaba"), _turn_completed())
        _patch_spawn(monkeypatch, proc)
        events = [
            ev async for ev in llm_dispatch.stream_user_chat(settings, "selam")
        ]
        assert events[0] == {"agent_id": "th-abc"}
        assert events[-1]["done"] is True
        assert events[-1]["text"] == "merhaba"

    asyncio.run(run())


def test_dispatch_capabilities_codex_not_stateless() -> None:
    """Codex declares stateless=False (it resumes via thread_id) — the resume/agent-id
    path, not the always-flatten-history path."""
    from akana_server.orchestrator.llm_dispatch import provider_capabilities

    assert provider_capabilities("codex").stateless is False
