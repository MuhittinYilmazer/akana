"""claude_provider — the local `claude` CLI provider (subscription auth).

No real `claude` binary runs: ``asyncio.create_subprocess_exec`` is replaced
with a fake whose stdout is a pre-fed ``asyncio.StreamReader`` (the same type
the client reads in production) feeding scripted stream-json (NDJSON) lines,
and whose argv/env are captured for assertion.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

import pytest

from akana_server.config import Settings, load_settings
from akana_server.orchestrator import claude_provider
from akana_server.orchestrator.llm_dispatch import LLMCallError

MCP_SERVERS: dict[str, Any] = {
    "akana_memory": {
        "type": "stdio",
        "command": "/usr/bin/python3",
        "args": ["-m", "akana.memory.mcp"],
        "env": {"PYTHONPATH": "/repo/src", "AKANA_DATA_DIR": "/data"},
        "cwd": "/repo/src",
    }
}


class _FakeProc:
    pid = 7777
    returncode: int | None = 0

    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        # The Windows cmd-wrapper "spill" path writes the prompt to proc.stdin. The fake
        # needs the attribute (None → the `proc.stdin is not None` guard skips the write);
        # without it the path raised AttributeError whenever claude.cmd was on PATH.
        self.stdin = None
        self._waited = False

    def feed(self, *events: dict[str, Any], eof: bool = True) -> None:
        for ev in events:
            self.stdout.feed_data((json.dumps(ev) + "\n").encode("utf-8"))
        if eof:
            self.stdout.feed_eof()

    async def wait(self) -> int:
        self._waited = True
        return self.returncode or 0

    def kill(self) -> None:  # pragma: no cover - only on timeout path
        self.returncode = -9


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Settings:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CURSOR_API_KEY", "test-key-123")
    return load_settings()


def _patch_spawn(
    monkeypatch: pytest.MonkeyPatch, proc: _FakeProc
) -> dict[str, Any]:
    """Replace create_subprocess_exec; capture argv/kwargs, return the fake proc."""
    captured: dict[str, Any] = {}

    async def _fake_spawn(*cmd: str, **kwargs: Any):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    return captured


_INIT = {"type": "system", "subtype": "init", "session_id": "sess-abc", "model": "claude-sonnet-4-6"}


def _delta(text: str) -> dict[str, Any]:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        },
    }


def _thinking_start(index: int = 0) -> dict[str, Any]:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_start",
            "index": index,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
    }


def _thinking_delta(text: str, index: int = 0) -> dict[str, Any]:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "index": index,
            "delta": {"type": "thinking_delta", "thinking": text},
        },
    }


def _block_stop(index: int = 0) -> dict[str, Any]:
    return {
        "type": "stream_event",
        "event": {"type": "content_block_stop", "index": index},
    }


_TOOL_USE = {
    "type": "assistant",
    "message": {
        "content": [
            {"type": "tool_use", "id": "tu-1", "name": "memory_search", "input": {"q": "x"}}
        ]
    },
}
_TOOL_RESULT = {
    "type": "user",
    "message": {
        "content": [
            {"type": "tool_result", "tool_use_id": "tu-1", "content": "found", "is_error": False}
        ]
    },
}
_RESULT_OK = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "merhaba",
    "usage": {
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 2,
    },
    "session_id": "sess-abc",
}


def test_stream_user_chat_yields_full_sequence(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, _delta("mer"), _delta("haba"), _TOOL_USE, _TOOL_RESULT, _RESULT_OK)
        _patch_spawn(monkeypatch, proc)

        events = [
            ev
            async for ev in claude_provider.stream_user_chat(
                settings, "selam", mcp_servers=MCP_SERVERS
            )
        ]

        assert events[0] == {"agent_id": "sess-abc"}
        deltas = [e["delta"] for e in events if "delta" in e]
        assert deltas == ["mer", "haba"]

        tool_events = [e["tool_call"] for e in events if "tool_call" in e]
        start = next(t for t in tool_events if t["phase"] == "start")
        end = next(t for t in tool_events if t["phase"] == "end")
        assert start["id"] == "tu-1" and start["name"] == "memory_search"
        assert start["args"] == {"q": "x"}
        assert end["id"] == "tu-1" and end["name"] == "memory_search"
        assert end["status"] == "ok" and end["result"] == "found"

        final = events[-1]
        assert final["done"] is True
        assert final["text"] == "merhaba"  # from deltas
        assert final["status"] == "finished"
        assert final["usage"] == {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "tool_calls": [],
            "cache_read_tokens": 3,
            "cache_write_tokens": 2,
        }
        assert any(tc["id"] == "tu-1" for tc in final["tool_calls"])

    asyncio.run(run())


def test_complete_chat_collects_terminal(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, _delta("mer"), _delta("haba"), _RESULT_OK)
        _patch_spawn(monkeypatch, proc)
        text, status, raw = await claude_provider.complete_chat(settings, "selam")
        assert text == "merhaba"
        assert status == "finished"
        assert raw["usage"]["prompt_tokens"] == 11

    asyncio.run(run())


def test_fallback_to_assistant_text_when_no_deltas(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """No deltas → the assistant message's text is the answer. The terminal
    result.result carries that SAME final text, so it is NOT appended on top
    (that would double it)."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        assistant = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "tam metin"}]},
        }
        proc.feed(_INIT, assistant, _RESULT_OK)
        _patch_spawn(monkeypatch, proc)
        events = [ev async for ev in claude_provider.stream_user_chat(settings, "selam")]
        assert events[-1]["text"] == "tam metin"  # assistant only, result not re-appended

    asyncio.run(run())


def test_result_text_used_when_assistant_has_no_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """No deltas AND no assistant text block (only a tool_use) → the terminal
    result.result becomes the answer (the fallback still works)."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, _TOOL_USE, _TOOL_RESULT, _RESULT_OK)
        _patch_spawn(monkeypatch, proc)
        events = [ev async for ev in claude_provider.stream_user_chat(settings, "selam")]
        assert events[-1]["text"] == "merhaba"  # from result.result, no assistant text

    asyncio.run(run())


def test_no_deltas_identical_assistant_and_result_is_not_doubled(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Regression: a short, non-streamed answer arrives as an assistant text block
    AND an identical result.result with no text_delta events. The done.text must be
    the single answer, not the answer concatenated with itself ('Done.Done.')."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        assistant = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Done."}]},
        }
        result = {**_RESULT_OK, "result": "Done."}
        proc.feed(_INIT, assistant, result)
        _patch_spawn(monkeypatch, proc)
        events = [ev async for ev in claude_provider.stream_user_chat(settings, "selam")]
        assert events[-1]["text"] == "Done."  # NOT "Done.Done."

    asyncio.run(run())


def test_thinking_deltas_become_wire_thinking_events(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``thinking_delta`` → ``{"thinking": {phase, text}}`` (Cursor shape)."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(
            _INIT,
            _thinking_start(0),
            _thinking_delta("dü"),
            _thinking_delta("şün"),
            _block_stop(0),
            _delta("ce"),
            _delta("vap"),
            _RESULT_OK,
        )
        _patch_spawn(monkeypatch, proc)

        events = [ev async for ev in claude_provider.stream_user_chat(settings, "selam")]

        thinking = [e["thinking"] for e in events if "thinking" in e]
        assert thinking[0] == {"phase": "delta", "text": "dü"}
        assert thinking[1] == {"phase": "delta", "text": "şün"}
        assert thinking[-1] == {"phase": "completed"}

        # Thinking text does NOT mix into the reply/final text — only text_delta's.
        deltas = [e["delta"] for e in events if "delta" in e]
        assert deltas == ["ce", "vap"]
        assert events[-1]["text"] == "cevap"

    asyncio.run(run())


def test_thinking_between_text_welds_paragraph_gap(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A thinking block BETWEEN two answer segments must not let them collide.

    Claude streams "…buluyorum." then thinks then "Paket bulundu." with NO
    whitespace at the seam. Without the gap the persisted/streamed text reads
    "…buluyorum.Paket bulundu."; a ``\\n\\n`` is welded before the resumed segment.
    """
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(
            _INIT,
            _delta("Paketi buluyorum."),
            _thinking_start(1),
            _thinking_delta("düşün"),
            _block_stop(1),
            _delta("Paket bulundu."),
            _RESULT_OK,
        )
        _patch_spawn(monkeypatch, proc)

        events = [ev async for ev in claude_provider.stream_user_chat(settings, "selam")]
        deltas = [e["delta"] for e in events if "delta" in e]
        # The gap rides on the FRONT of the resumed segment's first delta → the
        # streamed text and the final ``text`` agree byte-for-byte.
        assert deltas == ["Paketi buluyorum.", "\n\nPaket bulundu."]
        assert events[-1]["text"] == "Paketi buluyorum.\n\nPaket bulundu."

    asyncio.run(run())


def test_tool_between_text_welds_paragraph_gap(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A tool call BETWEEN two answer segments gets a paragraph break too.

    "Dosyayı okuyorum." → Read → "Dosyada şunlar var." must not glue into
    "…okuyorum.Dosyada…" once the tool card is stripped out of the flat text."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(
            _INIT,
            _delta("Dosyayı okuyorum."),
            _TOOL_USE,
            _TOOL_RESULT,
            _delta("Dosyada şunlar var."),
            _RESULT_OK,
        )
        _patch_spawn(monkeypatch, proc)

        events = [ev async for ev in claude_provider.stream_user_chat(settings, "selam")]
        deltas = [e["delta"] for e in events if "delta" in e]
        assert deltas == ["Dosyayı okuyorum.", "\n\nDosyada şunlar var."]
        assert events[-1]["text"] == "Dosyayı okuyorum.\n\nDosyada şunlar var."

    asyncio.run(run())


def test_consecutive_deltas_in_one_segment_are_not_split(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Token-by-token streaming of ONE sentence keeps flowing — the gap only fires
    across a thinking/tool seam, never between adjacent chunks of the same segment."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, _delta("Merha"), _delta("ba "), _delta("dünya."), _RESULT_OK)
        _patch_spawn(monkeypatch, proc)

        events = [ev async for ev in claude_provider.stream_user_chat(settings, "selam")]
        deltas = [e["delta"] for e in events if "delta" in e]
        assert deltas == ["Merha", "ba ", "dünya."]
        assert events[-1]["text"] == "Merhaba dünya."

    asyncio.run(run())


def test_autocontinue_welds_gap_between_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Across auto-continue runs the second run's first segment must not glue onto
    the first run's last sentence ("…yapıyorum.İkinci…").

    ``_stream_single_run`` is faked so the loop runs exactly twice: run 1 ends with a
    tool call (→ keep going), run 2 emits the completion sentinel (→ stop). The gap is
    inserted by the continuation wrapper at the run seam.

    Auto-continuation is OFF by default (owner decision: a turn is a single run so a
    posed question waits for the user); this test opts into the multi-run loop via the
    ``agent_autocontinue`` master switch."""
    monkeypatch.setenv("AKANA_AGENT_AUTOCONTINUE", "1")
    settings = _make_settings(monkeypatch, tmp_path)
    from akana_server.orchestrator import claude_provider as _cp

    runs: list[list[dict[str, Any]]] = [
        [
            {"agent_id": "sess-1"},
            {"delta": "Birinci adımı yapıyorum.", "done": False},
            {"tool_call": {"id": "t1", "name": "Read", "phase": "start"}},
            {
                "done": True,
                "text": "Birinci adımı yapıyorum.",
                "status": "finished",
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "tool_calls": []},
                "tool_calls": [{"id": "t1", "name": "Read"}],
            },
        ],
        [
            {"delta": "İkinci adım tamam.", "done": False},
            {"delta": "[[AKANA_TASK_COMPLETE]]", "done": False},
            {
                "done": True,
                "text": "İkinci adım tamam.[[AKANA_TASK_COMPLETE]]",
                "status": "finished",
                "usage": {"prompt_tokens": 4, "completion_tokens": 2, "tool_calls": []},
                "tool_calls": [],
            },
        ],
    ]
    calls = {"n": 0}

    async def _fake_single(*args: Any, **kwargs: Any):
        idx = calls["n"]
        calls["n"] += 1
        for ev in runs[idx]:
            yield ev

    monkeypatch.setattr(_cp, "_stream_single_run", _fake_single)

    async def run() -> None:
        events = [
            ev
            async for ev in claude_provider.stream_user_chat(
                settings, "selam", auto_continue=True
            )
        ]
        assert calls["n"] == 2  # the loop really ran twice
        deltas = [e["delta"] for e in events if "delta" in e]
        # The sentinel is stripped; run 2's first delta carries the seam gap.
        assert deltas == ["Birinci adımı yapıyorum.", "\n\nİkinci adım tamam."]
        final = events[-1]
        assert final["done"] is True
        assert final["text"] == "Birinci adımı yapıyorum.\n\nİkinci adım tamam."

    asyncio.run(run())


def test_thinking_mode_sets_effort_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """6 levels → native ``--effort`` (low..max); None → no flag."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def _effort_for(mode: str | None) -> str | None:
        proc = _FakeProc()
        proc.feed(_INIT, _RESULT_OK)
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in claude_provider.stream_user_chat(
            settings, "selam", thinking_mode=mode
        ):
            pass
        argv = captured["cmd"]
        if "--effort" not in argv:
            return None
        return argv[argv.index("--effort") + 1]

    async def run() -> None:
        assert await _effort_for("hizli") == "low"
        assert await _effort_for("normal") == "medium"
        assert await _effort_for("derin") == "high"
        assert await _effort_for("yogun") == "xhigh"
        assert await _effort_for("azami") == "max"
        assert await _effort_for("ultra") == "max"
        assert await _effort_for(None) is None

    asyncio.run(run())


def test_ultra_appends_ultracode_keyword_on_fable_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """thinking_mode="ultra" + a "fable" model id → " ultracode" is appended to the
    CLI-bound prompt text (argv), never to any persisted store (this provider has no
    persistence — chat_producer persists ``body.text`` independently of this prompt)."""
    from akana_server.orchestrator import llm_process

    settings = _make_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(llm_process, "_IS_WIN", False)  # prompt inline on argv

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, _RESULT_OK)
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in claude_provider.stream_user_chat(
            settings,
            "selam",
            model="claude-fable-5",
            thinking_mode="ultra",
        ):
            pass
        argv = captured["cmd"]
        prompt = argv[argv.index("-p") + 1]
        assert prompt.endswith("selam ultracode")

    asyncio.run(run())


def test_ultra_no_keyword_on_non_fable_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """thinking_mode="ultra" on a non-fable model still sets --effort max, but the
    prompt text is NOT altered (degrades to plain "azami"-equivalent behavior)."""
    from akana_server.orchestrator import llm_process

    settings = _make_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(llm_process, "_IS_WIN", False)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, _RESULT_OK)
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in claude_provider.stream_user_chat(
            settings,
            "selam",
            model="claude-sonnet-4-6",
            thinking_mode="ultra",
        ):
            pass
        argv = captured["cmd"]
        prompt = argv[argv.index("-p") + 1]
        assert prompt == "selam"
        assert "ultracode" not in prompt
        assert argv[argv.index("--effort") + 1] == "max"

    asyncio.run(run())


def test_tool_policy_default_is_full(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Default: full authority — bypassPermissions, no tool is blocked."""
    settings = _make_settings(monkeypatch, tmp_path)
    monkeypatch.delenv("AKANA_CLAUDE_FULL_TOOLS", raising=False)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, _RESULT_OK)
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in claude_provider.stream_user_chat(
            settings, "selam", chat_mode=True, mcp_servers=MCP_SERVERS
        ):
            pass

        argv = captured["cmd"]
        allowed = argv[argv.index("--allowedTools") + 1]
        # full-capability persona: mcp + read-only trio still allowed
        assert "mcp__akana_memory" in allowed
        assert "Read" in allowed and "Grep" in allowed and "Glob" in allowed
        assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
        assert "--disallowedTools" not in argv  # nothing blocked by default

    asyncio.run(run())


def test_tool_policy_optout_via_setting(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Persisted claude_full_tools=False → default mode + write/shell blocked."""
    from akana_server.llm_settings import update_llm_settings

    settings = _make_settings(monkeypatch, tmp_path)
    monkeypatch.delenv("AKANA_CLAUDE_FULL_TOOLS", raising=False)
    update_llm_settings(settings.data_dir, settings, {"claude_full_tools": False})

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, _RESULT_OK)
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in claude_provider.stream_user_chat(
            settings, "selam", chat_mode=True, mcp_servers=MCP_SERVERS
        ):
            pass
        argv = captured["cmd"]
        allowed = argv[argv.index("--allowedTools") + 1]
        disallowed = argv[argv.index("--disallowedTools") + 1]
        assert "mcp__akana_memory" in allowed
        assert "Read" in allowed and "Grep" in allowed and "Glob" in allowed
        assert "Bash" not in allowed
        for blocked in ("Bash", "Write", "Edit"):
            assert blocked in disallowed
        assert argv[argv.index("--permission-mode") + 1] == "default"

    asyncio.run(run())


def test_tool_policy_env_can_disable(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """AKANA_CLAUDE_FULL_TOOLS=0 → disables full authority when no persisted setting exists."""
    settings = _make_settings(monkeypatch, tmp_path)
    monkeypatch.setenv("AKANA_CLAUDE_FULL_TOOLS", "0")

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, _RESULT_OK)
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in claude_provider.stream_user_chat(
            settings, "selam", chat_mode=True, mcp_servers=MCP_SERVERS
        ):
            pass
        argv = captured["cmd"]
        assert argv[argv.index("--permission-mode") + 1] == "default"
        assert "Bash" in argv[argv.index("--disallowedTools") + 1]

    asyncio.run(run())


def test_tool_policy_non_chat_mode(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, _RESULT_OK)
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in claude_provider.stream_user_chat(
            settings, "selam", chat_mode=False, mcp_servers=MCP_SERVERS
        ):
            pass
        argv = captured["cmd"]
        allowed = argv[argv.index("--allowedTools") + 1]
        for tool in ("Read", "Grep", "Glob", "mcp__akana_memory"):
            assert tool in allowed

    asyncio.run(run())


def test_strict_mcp_config_present_with_servers(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """BP-1: ``--strict-mcp-config`` rides alongside ``--mcp-config`` so the CLI uses
    ONLY Akana's servers and never inherits user/project/local Claude Code scopes."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, _RESULT_OK)
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in claude_provider.stream_user_chat(
            settings, "selam", chat_mode=True, mcp_servers=MCP_SERVERS
        ):
            pass
        argv = captured["cmd"]
        assert "--mcp-config" in argv
        assert "--strict-mcp-config" in argv

    asyncio.run(run())


def test_strict_mcp_config_present_without_servers(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """BP-1: the flag is added even when Akana passes NO servers — a turn with no
    Akana MCP payload must still inherit nothing from foreign Claude Code scopes."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, _RESULT_OK)
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in claude_provider.stream_user_chat(
            settings, "selam", chat_mode=True, mcp_servers=None
        ):
            pass
        argv = captured["cmd"]
        assert "--mcp-config" not in argv  # nothing to configure
        assert "--strict-mcp-config" in argv  # but still lock out foreign scopes

    asyncio.run(run())


def test_mcp_config_with_secret_env_is_spilled_off_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """VAULT-2: on the NON-cmd (POSIX/``.exe``) path, an MCP server ``env`` block (which can
    hold the vault master key ``AKANA_VAULT_KEY``) must be spilled to a 0600 temp file — never
    inlined into ``--mcp-config <json>`` on argv, where ``ps``/``/proc`` exposes it to every
    local user. The prompt stays a positional arg (DEVNULL stdin), unlike the cmd path."""
    settings = _make_settings(monkeypatch, tmp_path)
    # Force the POSIX/``.exe`` (non-cmd) path — the test host has a claude.cmd shim.
    monkeypatch.setattr(claude_provider, "needs_cmd_wrapper", lambda _bin: False)
    servers = {
        "akana_vault": {
            "type": "stdio",
            "command": "/usr/bin/python3",
            "args": ["mcp_vault.py"],
            "env": {"AKANA_DATA_DIR": "/data", "AKANA_VAULT_KEY": "MASTER-KEY-SECRET"},
        }
    }
    captured: dict[str, Any] = {}

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, _RESULT_OK)

        async def _fake_spawn(*cmd: str, **kwargs: Any):
            argv = list(cmd)
            captured["cmd"] = argv
            captured["kwargs"] = kwargs
            # Snapshot the spilled file WHILE it still exists (cleaned in the turn's finally).
            mcp_arg = argv[argv.index("--mcp-config") + 1]
            captured["mcp_arg"] = mcp_arg
            if os.path.exists(mcp_arg):
                captured["mcp_content"] = open(mcp_arg, encoding="utf-8").read()
                captured["mcp_mode"] = os.stat(mcp_arg).st_mode & 0o777
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
        async for _ in claude_provider.stream_user_chat(
            settings, "selam", chat_mode=True, mcp_servers=servers
        ):
            pass

    asyncio.run(run())

    argv = captured["cmd"]
    # The master key never rides the command line (inline JSON form).
    assert "MASTER-KEY-SECRET" not in "\x00".join(argv)
    # ``--mcp-config`` points to a spilled FILE, not inline JSON.
    mcp_arg = captured["mcp_arg"]
    assert not mcp_arg.lstrip().startswith("{")
    assert json.loads(captured["mcp_content"]) == {"mcpServers": servers}
    # Non-cmd path: prompt is positional, stdin is DEVNULL (no cmd-wrapper coupling).
    assert "selam" in argv
    assert captured["kwargs"]["stdin"] == asyncio.subprocess.DEVNULL
    # Spilled file is owner-only (best-effort chmod; 0600 on POSIX).
    if os.name == "posix":
        assert captured["mcp_mode"] == 0o600
    # And it is cleaned up after the turn.
    assert not os.path.exists(mcp_arg)


def test_mcp_config_without_env_stays_inline_on_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """VAULT-2 guard: an env-LESS MCP config keeps the original inline behaviour on the
    non-cmd path — no needless temp file for a payload with no secret."""
    settings = _make_settings(monkeypatch, tmp_path)
    # Force the POSIX/``.exe`` (non-cmd) path — the test host has a claude.cmd shim.
    monkeypatch.setattr(claude_provider, "needs_cmd_wrapper", lambda _bin: False)
    servers = {
        "akana_memory": {
            "type": "stdio",
            "command": "/usr/bin/python3",
            "args": ["-m", "akana.memory.mcp"],
        }
    }

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, _RESULT_OK)
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in claude_provider.stream_user_chat(
            settings, "selam", chat_mode=True, mcp_servers=servers
        ):
            pass
        argv = captured["cmd"]
        mcp_arg = argv[argv.index("--mcp-config") + 1]
        # Inline JSON (not a file path).
        assert json.loads(mcp_arg) == {"mcpServers": servers}

    asyncio.run(run())


def test_env_hygiene_strips_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leak")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://leak")
    monkeypatch.setenv("AKANA_TOKEN", "bearer-leak")

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, _RESULT_OK)
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in claude_provider.stream_user_chat(settings, "selam"):
            pass
        env = captured["kwargs"]["env"]
        assert "ANTHROPIC_API_KEY" not in env
        assert "ANTHROPIC_BASE_URL" not in env
        # Akana-side secrets also do not pass to the claude process (or MCP descendants).
        assert "CURSOR_API_KEY" not in env
        assert "AKANA_TOKEN" not in env

    asyncio.run(run())


def test_env_injects_oauth_token_from_secret_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from akana_server.secret_store import set_secrets

    settings = _make_settings(monkeypatch, tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leak")
    set_secrets(tmp_path, {"claude_oauth_token": "sk-ant-oat01-token"})

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, _RESULT_OK)
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in claude_provider.stream_user_chat(settings, "selam"):
            pass
        env = captured["kwargs"]["env"]
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-token"
        # ANTHROPIC_* stripping must survive the token injection.
        assert "ANTHROPIC_API_KEY" not in env

    asyncio.run(run())


def test_env_no_oauth_token_when_store_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, _RESULT_OK)
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in claude_provider.stream_user_chat(settings, "selam"):
            pass
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in captured["kwargs"]["env"]

    asyncio.run(run())


def test_auth_error_raises_503(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(
            _INIT,
            {
                "type": "result",
                "subtype": "error",
                "is_error": True,
                "result": "auth failed",
                "api_error_status": 401,
                "session_id": "sess-abc",
            },
        )
        _patch_spawn(monkeypatch, proc)
        with pytest.raises(LLMCallError) as exc:
            async for _ in claude_provider.stream_user_chat(settings, "selam"):
                pass
        assert exc.value.status_code == 503
        assert "claude login" in str(exc.value)

    asyncio.run(run())


def test_resume_flag_present(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, _RESULT_OK)
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in claude_provider.stream_user_chat(
            settings, "selam", agent_id="sess-prev", reuse_agent=True
        ):
            pass
        argv = captured["cmd"]
        assert "--resume" in argv
        assert argv[argv.index("--resume") + 1] == "sess-prev"

    asyncio.run(run())


def test_no_resume_when_reuse_false(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, _RESULT_OK)
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in claude_provider.stream_user_chat(
            settings, "selam", agent_id="sess-prev", reuse_agent=False
        ):
            pass
        assert "--resume" not in captured["cmd"]

    asyncio.run(run())


# NOTE: these tests use an ISOLATED data_dir (_make_settings → tmp_path). A bare
# load_settings() would read the real ~/.akana/llm_settings.json → depending on
# both the developer's actual dashboard choice and on another test that writes to
# that file during the suite (fragile/order-dependent). In a clean tmp dir there
# is no persisted llm_settings → the resolve fallback is settings.claude_model.
def test_resolve_model(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    settings = _make_settings(monkeypatch, tmp_path)
    assert claude_provider._resolve_claude_model(settings, "claude-opus-4-1") == "claude-opus-4-1"
    assert claude_provider._resolve_claude_model(settings, "opus") == "opus"
    # garbage tag → settings default
    assert claude_provider._resolve_claude_model(settings, "gpt-4") == settings.claude_model


@pytest.mark.parametrize(
    "leaked_tag",
    ["composer-2", "composer-2-fast", "gpt-5.4-mini", "gemini-3-flash", "default", ""],
)
def test_resolve_model_cursor_tags_never_leak(
    leaked_tag: str, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """When provider=claude, cursor/other tags must NOT leak into the claude CLI."""
    settings = _make_settings(monkeypatch, tmp_path)
    resolved = claude_provider._resolve_claude_model(settings, leaked_tag)
    assert resolved == settings.claude_model
    assert resolved.startswith("claude-")


def test_resolve_model_none_falls_back(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    settings = _make_settings(monkeypatch, tmp_path)
    assert claude_provider._resolve_claude_model(settings, None) == settings.claude_model


# --------------------------------------------------------------------------- #
# Error mapping — Turkish user messages
# --------------------------------------------------------------------------- #
def test_live_auth_failure_maps_to_turkish_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Live 401 shape: subtype=success + is_error + api_error_status=401."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(
            _INIT,
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "api_error_status": 401,
                "result": "Failed to authenticate. API Error: 401 Invalid authentication credentials",
                "session_id": "sess-abc",
            },
        )
        _patch_spawn(monkeypatch, proc)
        with pytest.raises(LLMCallError) as exc:
            async for _ in claude_provider.stream_user_chat(settings, "selam"):
                pass
        msg = str(exc.value)
        assert exc.value.status_code == 503
        assert "claude login" in msg
        assert "claude_oauth_token" in msg

    asyncio.run(run())


def test_stale_resume_yields_need_history_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """If the resume session is not found, same as Cursor: bootstrap retry signal."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.stderr = asyncio.StreamReader()
        proc.stderr.feed_data(
            b"No conversation found with session ID: 123e4567-e89b-12d3-a456-426614174000\n"
        )
        proc.stderr.feed_eof()
        proc.feed(
            _INIT,
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "num_turns": 0,
                "session_id": "123e4567-e89b-12d3-a456-426614174000",
            },
        )
        _patch_spawn(monkeypatch, proc)
        events: list[dict[str, Any]] = []
        async for ev in claude_provider.stream_user_chat(
            settings, "selam", agent_id="cursor-uuid", reuse_agent=True
        ):
            events.append(ev)
        assert events[-1] == {"need_history_bootstrap": True}
        assert any(ev.get("agent_id") for ev in events)

    asyncio.run(run())


def test_stale_resume_bootstrap_retry_completes(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The second attempt completes successfully with history, without --resume."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        stale_proc = _FakeProc()
        stale_proc.stderr = asyncio.StreamReader()
        stale_proc.stderr.feed_data(
            b"No conversation found with session ID: stale-sess\n"
        )
        stale_proc.stderr.feed_eof()
        stale_proc.feed(
            _INIT,
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "num_turns": 0,
                "session_id": "stale-sess",
            },
        )

        ok_proc = _FakeProc()
        ok_proc.feed(
            _INIT,
            _delta("merhaba"),
            {"type": "result", "subtype": "success", "is_error": False, "result": ""},
        )

        seq = [stale_proc, ok_proc]
        call_idx = 0
        captured_cmds: list[list[str]] = []

        async def _fake_spawn(*cmd: str, **kwargs: Any):
            nonlocal call_idx
            captured_cmds.append(list(cmd))
            proc = seq[call_idx]
            call_idx += 1
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)

        history: list[dict[str, str]] = []
        agent_id: str | None = "stale-sess"
        final: dict[str, Any] | None = None
        for _attempt in range(2):
            async for ev in claude_provider.stream_user_chat(
                settings,
                "devam",
                history=history,
                agent_id=agent_id,
                reuse_agent=True,
            ):
                if ev.get("need_history_bootstrap"):
                    agent_id = None
                    history = [{"role": "user", "content": "önceki tur"}]  # prior turn (TR input)
                    break
                if ev.get("done"):
                    final = ev
            if final is not None:
                break
        assert final is not None
        assert final.get("text") == "merhaba"
        assert len(captured_cmds) == 2
        assert "--resume" in captured_cmds[0]
        assert "--resume" not in captured_cmds[1]

    asyncio.run(run())


def test_unknown_model_maps_with_model_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(
            _INIT,
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "api_error_status": 404,
                "result": "API Error: 404 not_found_error model",
                "session_id": "sess-abc",
            },
        )
        _patch_spawn(monkeypatch, proc)
        with pytest.raises(LLMCallError) as exc:
            async for _ in claude_provider.stream_user_chat(
                settings, "selam", model="claude-yok-boyle-model"
            ):
                pass
        assert "claude-yok-boyle-model" in str(exc.value)

    asyncio.run(run())


def test_missing_binary_maps_to_install_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def _raise_fnf(*cmd: str, **kwargs: Any):
        raise FileNotFoundError(2, "No such file or directory", cmd[0])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _raise_fnf)

    async def run() -> None:
        with pytest.raises(LLMCallError) as exc:
            async for _ in claude_provider.stream_user_chat(settings, "selam"):
                pass
        msg = str(exc.value)
        assert exc.value.status_code == 503
        assert "claude CLI not found" in msg
        assert "install" in msg

    asyncio.run(run())


def test_generic_result_error_passes_meaningful_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(
            _INIT,
            {
                "type": "result",
                "subtype": "error",
                "is_error": True,
                "result": "rate limit exceeded, retry later",
                "session_id": "sess-abc",
            },
        )
        _patch_spawn(monkeypatch, proc)
        with pytest.raises(LLMCallError) as exc:
            async for _ in claude_provider.stream_user_chat(settings, "selam"):
                pass
        assert "rate limit exceeded" in str(exc.value)

    asyncio.run(run())


def test_error_without_any_detail_falls_back_to_generic(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(
            _INIT,
            {"type": "result", "subtype": "error", "is_error": True, "session_id": "s"},
        )
        _patch_spawn(monkeypatch, proc)
        with pytest.raises(LLMCallError) as exc:
            async for _ in claude_provider.stream_user_chat(settings, "selam"):
                pass
        assert str(exc.value) == "claude run failed"

    asyncio.run(run())


def test_subprocess_death_mid_stream_raises_not_silent_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Behavior invariant: if the CLI dies mid-response (stdout EOF, no result,
    rc!=0, empty stderr) it raises LLMCallError instead of a fake "empty success";
    the deltas streamed up to that point still reached the user (partial-save)."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.returncode = 1  # non-zero exit without stderr
        proc.feed(_INIT, _delta("yarım "))  # eof=True, NO result event
        _patch_spawn(monkeypatch, proc)
        deltas: list[str] = []
        with pytest.raises(LLMCallError) as exc:
            async for ev in claude_provider.stream_user_chat(settings, "selam"):
                if "delta" in ev:
                    deltas.append(ev["delta"])
        assert deltas == ["yarım "]
        assert str(exc.value) == "claude run failed"

    asyncio.run(run())


def test_corrupt_ndjson_line_is_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Behavior invariant: a corrupt line inside stream-json does not break the turn —
    the line is skipped and the remaining events are processed normally."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.stdout.feed_data((json.dumps(_INIT) + "\n").encode("utf-8"))
        proc.stdout.feed_data(b"{bozuk json satiri %%%\n")
        proc.stdout.feed_data(b'"dict degil ama json"\n')
        proc.feed(_delta("mer"), _delta("haba"), _RESULT_OK, eof=False)
        _patch_spawn(monkeypatch, proc)
        events = [ev async for ev in claude_provider.stream_user_chat(settings, "selam")]
        deltas = [e["delta"] for e in events if "delta" in e]
        assert deltas == ["mer", "haba"]
        assert events[-1]["done"] is True
        assert events[-1]["text"] == "merhaba"

    asyncio.run(run())


def test_bad_usage_value_in_result_does_not_crash_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """QUALITY turn: a corrupt token inside ``result.usage`` (float-string / nonsense
    string / list) must NOT crash the turn — the old ``int(...)`` path swallowed the
    ``done`` event and gave the user an empty reply. Now it safely 0s/rounds-down."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        bad_result = {
            **_RESULT_OK,
            "usage": {
                "input_tokens": "9.9",
                "output_tokens": "bozuk",
                "cache_read_input_tokens": [1],
                "cache_creation_input_tokens": None,
            },
        }
        proc.feed(_INIT, _delta("mer"), _delta("haba"), bad_result, eof=False)
        _patch_spawn(monkeypatch, proc)
        events = [ev async for ev in claude_provider.stream_user_chat(settings, "selam")]
        final = events[-1]
        assert final["done"] is True
        assert final["text"] == "merhaba"
        assert final["usage"]["prompt_tokens"] == 9  # float-string rounded down
        assert final["usage"]["completion_tokens"] == 0  # nonsense string → 0
        assert final["usage"]["cache_read_tokens"] == 0  # list → 0

    asyncio.run(run())


# -- BUG 1: process group (start_new_session) + pid registration + group-kill -----


def test_claude_spawned_in_own_session_and_pid_registered(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The claude CLI is spawned in its own process group (start_new_session=True)
    and a pid file is kept for the duration of the run, deleted on clean exit."""
    settings = _make_settings(monkeypatch, tmp_path)
    from akana_server.orchestrator.llm_process import llm_pid_dir

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, _delta("ok"), _RESULT_OK)
        captured = _patch_spawn(monkeypatch, proc)
        events = [ev async for ev in claude_provider.stream_user_chat(settings, "selam")]
        assert captured["kwargs"]["start_new_session"] is True
        assert events[-1]["done"] is True
        # On clean exit the pid file was deleted (release_llm_process).
        assert list(llm_pid_dir(tmp_path).glob("*.json")) == []

    asyncio.run(run())


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process-group kill (os.killpg); Windows uses taskkill via llm_process._IS_WIN",
)
def test_claude_timeout_kills_process_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """If the stream times out, the process GROUP (killpg) is killed and the pid cleaned up.

    A plain proc.kill() would orphan the claude CLI's child MCP processes."""
    settings = _make_settings(monkeypatch, tmp_path)
    monkeypatch.setenv("CLAUDE_BRIDGE_TIMEOUT", "0.2")
    settings = _make_settings(monkeypatch, tmp_path)
    from akana_server.orchestrator.llm_process import llm_pid_dir

    killpg_calls: list[int] = []
    monkeypatch.setattr(
        "akana_server.orchestrator.llm_process.os.killpg",
        lambda pgid, sig: killpg_calls.append(pgid),
    )
    # killpg fake → the process appears "alive"; terminate returns after a short grace.
    monkeypatch.setattr(
        "akana_server.orchestrator.llm_process._pid_alive", lambda pid: False
    )

    class _HangingProc(_FakeProc):
        pid = 9191

        def __init__(self) -> None:
            super().__init__()
            # send init but the terminal event NEVER arrives → _read_line timeout.
            self.stdout.feed_data((json.dumps(_INIT) + "\n").encode("utf-8"))

        def kill(self) -> None:
            self.returncode = -9

    async def run() -> None:
        proc = _HangingProc()
        _patch_spawn(monkeypatch, proc)
        with pytest.raises(LLMCallError, match="timed out"):
            async for _ in claude_provider.stream_user_chat(settings, "selam"):
                pass
        # killpg was called on the process group (pid=9191).
        assert 9191 in killpg_calls
        # the pid file was cleaned up.
        assert list(llm_pid_dir(tmp_path).glob("*.json")) == []

    asyncio.run(run())


def test_build_prompt_no_history_is_bare_message() -> None:
    """With no history, prompt = raw message; NO "User:" label at the start.

    A labeled open-ended turn pushes the model to continue the ``User:``/``Akana:``
    pattern (inventing its own fake turns) — when there is no history, never start it."""
    assert claude_provider._build_prompt("selam", None) == "selam"
    assert claude_provider._build_prompt("selam", []) == "selam"


def test_build_prompt_wraps_history_as_context_then_bare_turn() -> None:
    """History is wrapped in a delimited CONTEXT block; the new turn comes plain.

    This way the prompt does NOT END with an open ``User:`` label → the model gives
    its reply and stops, instead of inventing fake ``User:``/``Akana:`` turns."""
    history = [
        {"role": "user", "content": "merhaba"},
        {"role": "assistant", "content": "buyur"},
    ]
    # Default language is English (English-first); the framing label must NOT
    # leak Turkish (that biased replies toward Turkish in English mode).
    out = claude_provider._build_prompt("grup oluştur", history)
    assert "[Previous conversation" in out
    assert "[/Previous conversation]" in out
    assert "Önceki konuşma" not in out
    assert "User: merhaba" in out
    assert "Akana: buyur" in out
    tail = out.split("[/Previous conversation]", 1)[1]
    assert tail.strip() == "grup oluştur"
    assert not out.rstrip().endswith("User: grup oluştur")

    # language="tr" → Turkish framing (toggle-driven).
    tr_out = claude_provider._build_prompt("grup oluştur", history, "tr")
    assert "[Önceki konuşma" in tr_out
    assert "[/Önceki konuşma]" in tr_out
    assert tr_out.split("[/Önceki konuşma]", 1)[1].strip() == "grup oluştur"


def test_history_for_prompt_empty_on_resume_full_on_fresh() -> None:
    """Resume → None (the session holds the history); fresh session → returns full history."""
    h = [{"role": "user", "content": "x"}]
    assert claude_provider._history_for_prompt(h, resuming=True) is None
    assert claude_provider._history_for_prompt(h, resuming=False) is h


# --------------------------------------------------------------------------- #
# AskUserQuestion — when Claude asks the user a structured question
# (in headless `-p` mode the CLI auto-rejects the tool_use; Akana catches it → ask_user).
# --------------------------------------------------------------------------- #
_ASK_TOOL_USE = {
    "type": "assistant",
    "message": {
        "content": [
            {
                "type": "tool_use",
                "id": "tu-ask",
                "name": "AskUserQuestion",
                "input": {
                    "questions": [
                        {
                            "question": "Çay mı kahve mi?",
                            "header": "İçecek",
                            "multiSelect": False,
                            "options": [
                                {"label": "Çay", "description": "demli"},
                                {"label": "Kahve", "description": "filtre"},
                            ],
                        },
                        {
                            "question": "Hangi boyları istersin?",
                            "header": "Boy",
                            "multiSelect": True,
                            "options": [
                                {"label": "Küçük"},
                                {"label": "Orta"},
                                {"label": "Büyük"},
                            ],
                        },
                    ]
                },
            }
        ]
    },
}
#: CLI's headless auto-reject — observed shape: is_error + "Answer questions?".
_ASK_AUTO_DISMISS = {
    "type": "user",
    "message": {
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "tu-ask",
                "content": "Answer questions?",
                "is_error": True,
            }
        ]
    },
}
#: After the auto-reject the model switches to apologizing (observed) — this text
#: must NOT be the final reply (the question turn returns empty-text, the card is carried).
_ASK_APOLOGY_ASSISTANT = {
    "type": "assistant",
    "message": {"content": [{"type": "text", "text": "Üzgünüm, yanıt veremeden ilerleyemem."}]},
}
_ASK_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "Üzgünüm, sorularını yanıtlamadan devam edemem.",
    "session_id": "sess-abc",
}


def test_ask_user_question_emits_structured_event_and_suppresses_noise(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """AskUserQuestion → structured ``ask_user`` event + auto-reject/apology suppression.

    Contract: (1) the normalized ``ask_user`` is emitted; (2) the auto-reject's red
    ``tool_call`` card is NOT emitted; (3) the apology (assistant+result) does NOT
    join the final text → empty; (4) done carries ``status=awaiting_user`` + ``ask_user``.
    """
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, _ASK_TOOL_USE, _ASK_AUTO_DISMISS, _ASK_APOLOGY_ASSISTANT, _ASK_RESULT)
        _patch_spawn(monkeypatch, proc)
        events = [ev async for ev in claude_provider.stream_user_chat(settings, "iki şey sor")]

        # 1) Structured ask_user event — questions/options/multiSelect preserved.
        ask = next(e["ask_user"] for e in events if "ask_user" in e)
        assert ask["id"] == "tu-ask"
        assert [q["question"] for q in ask["questions"]] == [
            "Çay mı kahve mi?",
            "Hangi boyları istersin?",
        ]
        assert ask["questions"][0]["multiSelect"] is False
        assert ask["questions"][1]["multiSelect"] is True
        assert ask["questions"][0]["options"][0] == {"label": "Çay", "description": "demli"}
        assert ask["questions"][1]["options"][0] == {"label": "Küçük", "description": ""}

        # 2) The auto-reject's tool_call (red card) is NOT emitted.
        for e in events:
            if "tool_call" in e:
                assert e["tool_call"]["id"] != "tu-ask"

        # 3) The apology text (assistant + result) does not enter the final text → empty.
        done = events[-1]
        assert done["done"] is True
        assert done["text"] == ""

        # 4) The turn is "awaiting user" + ask_user is carried in done (card persists).
        assert done["status"] == "awaiting_user"
        assert done["ask_user"]["id"] == "tu-ask"

    asyncio.run(run())


def test_normalize_ask_user_defensive_coercion() -> None:
    """Defensive normalization of external input: empty question/label/option-less
    are dropped; types are coerced (multiSelect→bool, option→{label,description});
    a plain-string option is supported; surrounding whitespace is trimmed."""
    raw = {
        "questions": [
            {"question": "", "options": [{"label": "x"}]},  # empty question → dropped
            {"question": "Seçeneksiz?", "options": []},  # no options → dropped
            {
                "question": "  Boşluklu  ",
                "header": "  H  ",
                "multiSelect": 1,  # truthy int → True
                "options": [
                    {"label": "  A  ", "description": "  d  "},
                    {"label": ""},  # empty label → dropped
                    "Düz",  # plain-string option
                ],
            },
        ]
    }
    out = claude_provider._normalize_ask_user("tid-9", raw)
    assert out is not None
    assert out["id"] == "tid-9"
    assert len(out["questions"]) == 1
    q = out["questions"][0]
    assert q["question"] == "Boşluklu"
    assert q["header"] == "H"
    assert q["multiSelect"] is True
    assert q["options"] == [
        {"label": "A", "description": "d"},
        {"label": "Düz", "description": ""},
    ]


def test_normalize_ask_user_returns_none_on_garbage() -> None:
    """Invalid input → ``None`` (the caller falls back to the generic tool card, does not swallow the turn)."""
    assert claude_provider._normalize_ask_user("t", None) is None
    assert claude_provider._normalize_ask_user("t", {"questions": "nope"}) is None
    assert claude_provider._normalize_ask_user("t", {"no_questions": []}) is None
    # no valid question (all option-less) → None
    assert (
        claude_provider._normalize_ask_user("t", {"questions": [{"question": "q", "options": []}]})
        is None
    )


def test_normalize_ask_user_parses_json_string_input() -> None:
    """REGRESSION: ``AskUserQuestion`` is interactive-only in current ``claude -p``
    (absent from the init ``tools`` list). When the model calls it anyway the CLI
    rejects it and delivers the rejected tool's ``input`` as an UNPARSED JSON
    STRING (an executable tool's input is pre-parsed to a dict). The normalizer
    must parse the string — otherwise the question degrades into a generic red
    "No such tool available" error card (the reported bug)."""
    raw_str = json.dumps(
        {
            "questions": [
                {
                    "question": "RE'yi hangi amaçla öğrenmek istiyorsun?",
                    "header": "Odak",
                    "multiSelect": False,
                    "options": [
                        {"label": "CTF / rev-pwn", "description": "yarışma"},
                        {"label": "Malware analizi", "description": "savunma"},
                    ],
                }
            ]
        }
    )
    out = claude_provider._normalize_ask_user("tu-str", raw_str)
    assert out is not None
    assert out["id"] == "tu-str"
    assert len(out["questions"]) == 1
    assert out["questions"][0]["question"] == "RE'yi hangi amaçla öğrenmek istiyorsun?"
    assert out["questions"][0]["options"][0] == {"label": "CTF / rev-pwn", "description": "yarışma"}
    # A non-JSON string is still rejected → None (generic card, no crash).
    assert claude_provider._normalize_ask_user("t", "not json at all") is None


def test_normalize_plan_parses_json_string_input() -> None:
    """``ExitPlanMode`` is rejected in headless ``-p`` the same way as
    AskUserQuestion → its input also arrives as a JSON string; the plan normalizer
    must parse it too (symmetry via ``_coerce_tool_input``)."""
    raw_str = json.dumps({"plan": "# Plan\n\n1. do it", "planFilePath": "/tmp/p.md"})
    out = claude_provider._normalize_plan("tu-plan", raw_str)
    assert out is not None
    assert out["plan"].startswith("# Plan")
    assert out["plan_file"] == "/tmp/p.md"
    # Non-JSON / non-object string → None.
    assert claude_provider._normalize_plan("t", "garbage") is None
    assert claude_provider._normalize_plan("t", json.dumps({"plan": ""})) is None


def test_ask_user_rejected_tool_string_input_still_asks(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """REGRESSION (end-to-end): current ``claude -p`` has no AskUserQuestion tool
    → the model's call is rejected ("No such tool available … not enabled in this
    context") and the rejected input streams in + lands as a JSON STRING. Akana
    must STILL surface a structured ``ask_user`` (not a red error card), suppress
    the error ``tool_call``, and NOT emit a ``tool_call_delta`` for the streamed
    input (which would orphan a half-built card when the turn terminates early)."""
    settings = _make_settings(monkeypatch, tmp_path)
    qjson = json.dumps(
        {
            "questions": [
                {
                    "question": "Çay mı kahve mi?",
                    "header": "İçecek",
                    "multiSelect": False,
                    "options": [
                        {"label": "Çay", "description": ""},
                        {"label": "Kahve", "description": ""},
                    ],
                }
            ]
        }
    )
    blk_start = {
        "type": "stream_event",
        "event": {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "tu-ask", "name": "AskUserQuestion", "input": {}},
        },
    }

    def _delta(partial: str) -> dict[str, Any]:
        return {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": partial},
            },
        }

    # Complete assistant message — the rejected tool's input is an UNPARSED STRING.
    full = {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "id": "tu-ask", "name": "AskUserQuestion", "input": qjson}]
        },
    }
    err = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "tu-ask",
                    "is_error": True,
                    "content": (
                        "Error: No such tool available: AskUserQuestion. AskUserQuestion "
                        "exists but is not enabled in this context."
                    ),
                }
            ]
        },
    }

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, blk_start, _delta(qjson[:12]), _delta(qjson[12:]), full, err, _RESULT_OK)
        _patch_spawn(monkeypatch, proc)
        events = [ev async for ev in claude_provider.stream_user_chat(settings, "soru sor")]

        # 1) Structured ask_user emitted — parsed from the STRING input.
        ask = next(e["ask_user"] for e in events if "ask_user" in e)
        assert ask["id"] == "tu-ask"
        assert ask["questions"][0]["question"] == "Çay mı kahve mi?"
        assert ask["questions"][0]["options"][0]["label"] == "Çay"
        # 2) No tool_call (start OR the error result) for the rejected tool → no red card.
        assert all(e["tool_call"]["id"] != "tu-ask" for e in events if "tool_call" in e)
        # 3) No tool_call_delta for AskUserQuestion → no orphaned streaming card.
        assert not any("tool_call_delta" in e for e in events)
        # 4) done: awaiting_user + ask_user carried, empty final text.
        done = events[-1]
        assert done["done"] is True
        assert done["status"] == "awaiting_user"
        assert done["ask_user"]["id"] == "tu-ask"
        assert done["text"] == ""

    asyncio.run(run())


def test_ask_block_helpers() -> None:
    """The text-protocol primitives: extract inner JSON, strip the block from answer
    text, and stream-holdback (never flash the block, but release a partial-marker
    tail that turns out to be real text)."""
    blk = (
        "[[AKANA_ASK]]"
        + json.dumps({"questions": [{"question": "Q", "options": [{"label": "A"}]}]})
        + "[[/AKANA_ASK]]"
    )
    # extract → inner JSON (fed straight to the string-tolerant normalizer)
    inner = claude_provider._extract_ask_block("pre " + blk + " post")
    assert inner and claude_provider._normalize_ask_user("x", inner) is not None
    assert claude_provider._extract_ask_block("no block here") is None
    # strip → block removed from both sides; unterminated block dropped from open on
    assert claude_provider._strip_ask_block("pre " + blk + " post") == "pre  post".strip()
    assert claude_provider._strip_ask_block("keep [[AKANA_ASK]]{oops") == "keep"
    # streaming holdback: block never emitted, preamble is
    s = claude_provider._AskBlockStripper()
    emitted = "".join(s.feed(c) for c in ["Hi ", "there [[AKA", "NA_ASK]]{\"q\":1}[[/AKANA_ASK]]"])
    assert emitted == "Hi there " and s.flush() == ""
    # a trailing partial marker that never completes → released by flush (real text)
    s2 = claude_provider._AskBlockStripper()
    assert s2.feed("ends [[") == "ends " and s2.flush() == "[["


def test_ask_user_text_protocol_block_becomes_card(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Text-protocol ask_user (the primary path — AskUserQuestion is unavailable
    headless AND the model tends to refuse it): the model emits an
    [[AKANA_ASK]]{json}[[/AKANA_ASK]] block in its TEXT. Akana must (1) hold the
    block back from the live deltas, (2) emit a structured ask_user, (3) keep only
    the preamble as the answer, (4) done = awaiting_user + ask_user carried."""
    settings = _make_settings(monkeypatch, tmp_path)
    block = (
        "[[AKANA_ASK]]"
        + json.dumps(
            {
                "questions": [
                    {
                        "question": "Çay mı kahve mi?",
                        "header": "İçecek",
                        "multiSelect": False,
                        "options": [
                            {"label": "Çay", "description": ""},
                            {"label": "Kahve", "description": ""},
                        ],
                    }
                ]
            }
        )
        + "[[/AKANA_ASK]]"
    )
    full_text = "Tercihini netleştireyim. " + block

    def _td(t: str) -> dict[str, Any]:
        return {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": t},
            },
        }

    # Stream the text in 17-char chunks so the marker is split across deltas.
    chunks = [full_text[i : i + 17] for i in range(0, len(full_text), 17)]
    assistant = {"type": "assistant", "message": {"content": [{"type": "text", "text": full_text}]}}

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, *[_td(c) for c in chunks], assistant, _RESULT_OK)
        _patch_spawn(monkeypatch, proc)
        events = [ev async for ev in claude_provider.stream_user_chat(settings, "soru sor")]

        # 1) Structured ask_user emitted, parsed from the block.
        ask = next(e["ask_user"] for e in events if "ask_user" in e)
        assert ask["questions"][0]["question"] == "Çay mı kahve mi?"
        assert ask["questions"][0]["options"][0]["label"] == "Çay"
        # 2) Live deltas: preamble shown, the block held back (never flashed).
        streamed = "".join(
            e["delta"] for e in events if isinstance(e.get("delta"), str)
        )
        assert "Tercihini netleştireyim." in streamed
        assert "[[AKANA_ASK]]" not in streamed and "questions" not in streamed
        # 3) done: awaiting_user, ask_user carried, answer = preamble only (no block).
        done = events[-1]
        assert done["done"] is True
        assert done["status"] == "awaiting_user"
        assert done["ask_user"]["questions"][0]["header"] == "İçecek"
        assert "[[AKANA_ASK]]" not in done["text"] and "[[/AKANA_ASK]]" not in done["text"]
        assert "Tercihini netleştireyim." in done["text"]

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# ExitPlanMode — when Claude presents its plan in plan mode (in headless `-p` mode
# the CLI auto-rejects the tool_use; Akana catches it → plan). Same pattern as AskUserQuestion.
# --------------------------------------------------------------------------- #
_PLAN_MD = "# Plan: greet()\n\n## Approach\nCreate `/tmp/hello.py` with a `greet()` fn."
_PLAN_TOOL_USE = {
    "type": "assistant",
    "message": {
        "content": [
            {
                "type": "tool_use",
                "id": "tu-plan",
                "name": "ExitPlanMode",
                "input": {
                    "plan": _PLAN_MD,
                    "planFilePath": "/home/u/.claude/plans/plan-acorn.md",
                },
            }
        ]
    },
}
#: CLI's headless auto-reject — observed shape: is_error + "Exit plan mode?".
_PLAN_AUTO_DISMISS = {
    "type": "user",
    "message": {
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "tu-plan",
                "content": "Exit plan mode?",
                "is_error": True,
            }
        ]
    },
}
#: After the auto-reject the model writes a "plan ready" summary (observed) — this text
#: must NOT be the final reply (the plan turn returns empty-text, the card is carried).
_PLAN_SUMMARY_ASSISTANT = {
    "type": "assistant",
    "message": {
        "content": [{"type": "text", "text": "The plan is written and ready for your review."}]
    },
}
_PLAN_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "The plan is written and ready for your review.",
    "session_id": "sess-abc",
}


def _permission_mode(argv: list[str]) -> str | None:
    """Extract the ``--permission-mode`` value from argv (None if absent)."""
    try:
        return argv[argv.index("--permission-mode") + 1]
    except (ValueError, IndexError):
        return None


def test_plan_mode_emits_structured_event_and_suppresses_noise(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """ExitPlanMode → structured ``plan`` event + auto-reject/summary suppression.

    Contract: (1) the normalized ``plan`` (markdown + file path) is emitted;
    (2) the auto-reject's red ``tool_call`` card is NOT emitted; (3) the "plan ready"
    summary (assistant+result) does NOT join the final text → empty; (4) done carries
    ``status=awaiting_user`` + ``plan``.
    """
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(
            _INIT, _PLAN_TOOL_USE, _PLAN_AUTO_DISMISS, _PLAN_SUMMARY_ASSISTANT, _PLAN_RESULT
        )
        _patch_spawn(monkeypatch, proc)
        events = [ev async for ev in claude_provider.stream_user_chat(settings, "plan yap")]

        # 1) Structured plan event — markdown + file path preserved.
        plan = next(e["plan"] for e in events if "plan" in e and not e.get("done"))
        assert plan["id"] == "tu-plan"
        assert plan["plan"] == _PLAN_MD
        assert plan["plan_file"] == "/home/u/.claude/plans/plan-acorn.md"

        # 2) The auto-reject's tool_call (red card) is NOT emitted.
        for e in events:
            if "tool_call" in e:
                assert e["tool_call"]["id"] != "tu-plan"

        # 3) The "plan ready" summary (assistant + result) does not enter the final text → empty.
        done = events[-1]
        assert done["done"] is True
        assert done["text"] == ""

        # 4) The turn is "awaiting user" + plan is carried in done (card persists).
        assert done["status"] == "awaiting_user"
        assert done["plan"]["id"] == "tu-plan"
        assert done["plan"]["plan"] == _PLAN_MD

    asyncio.run(run())


def test_plan_mode_flag_sets_permission_mode_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``plan_mode=True`` → ``--permission-mode plan`` (overrides bypassPermissions);
    when off, the mode is never ``plan`` (the existing bypassPermissions/default is kept)."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        # plan_mode=True → mode "plan"
        proc_on = _FakeProc()
        proc_on.feed(_INIT, _RESULT_OK)
        cap_on = _patch_spawn(monkeypatch, proc_on)
        async for _ in claude_provider.stream_user_chat(settings, "x", plan_mode=True):
            pass
        assert _permission_mode(cap_on["cmd"]) == "plan"

        # plan_mode=False (default) → mode NOT "plan"
        proc_off = _FakeProc()
        proc_off.feed(_INIT, _RESULT_OK)
        cap_off = _patch_spawn(monkeypatch, proc_off)
        async for _ in claude_provider.stream_user_chat(settings, "x"):
            pass
        assert _permission_mode(cap_off["cmd"]) != "plan"

    asyncio.run(run())


def test_normalize_plan_extracts_markdown_and_path() -> None:
    """``ExitPlanMode`` input → {id, plan, plan_file}; whitespace trimmed."""
    out = claude_provider._normalize_plan(
        "tid-7", {"plan": "  # P\n\nadım  ", "planFilePath": "  /p/x.md  "}
    )
    assert out == {"id": "tid-7", "plan": "# P\n\nadım", "plan_file": "/p/x.md"}
    # if planFilePath is absent, plan_file = ""
    out2 = claude_provider._normalize_plan("t", {"plan": "P"})
    assert out2 == {"id": "t", "plan": "P", "plan_file": ""}


def test_normalize_plan_returns_none_on_garbage() -> None:
    """If the plan text is missing/corrupt → ``None`` (falls back to the generic tool card)."""
    assert claude_provider._normalize_plan("t", None) is None
    assert claude_provider._normalize_plan("t", {"planFilePath": "/x"}) is None  # no plan
    assert claude_provider._normalize_plan("t", {"plan": "   "}) is None  # empty plan
    assert claude_provider._normalize_plan("t", "nope") is None


def test_resume_sends_only_new_turn_not_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """When resume is active, history is NOT re-entered into the prompt — the claude session already holds it.

    Prevents double feeding (token waste) and the trigger for the model to continue
    the transcript; same decision as ``historyForAgent`` in the cursor daemon."""
    from akana_server.orchestrator import llm_process

    settings = _make_settings(monkeypatch, tmp_path)
    # Force the POSIX delivery path so the prompt is inline on argv regardless of whether the
    # test host has a claude.cmd shim (on Windows the cmd-wrapper spills the prompt to stdin
    # instead — that path is covered by test_windows_cmd_path_spills_prompt_and_files).
    monkeypatch.setattr(llm_process, "_IS_WIN", False)
    history = [
        {"role": "user", "content": "eski soru"},
        {"role": "assistant", "content": "eski cevap"},
    ]

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, _RESULT_OK)
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in claude_provider.stream_user_chat(
            settings,
            "yeni soru",
            history=history,
            agent_id="sess-prev",
            reuse_agent=True,
        ):
            pass
        argv = captured["cmd"]
        assert "--resume" in argv
        prompt = argv[argv.index("-p") + 1]
        assert prompt == "yeni soru"  # only the new turn
        assert "eski soru" not in prompt  # history was not re-sent
        assert "Önceki konuşma" not in prompt  # no transcript block

    asyncio.run(run())


def test_fresh_session_bootstraps_full_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """When there is no resume (fresh/first turn, provider-switch, reset) history is bootstrapped."""
    from akana_server.orchestrator import llm_process

    settings = _make_settings(monkeypatch, tmp_path)
    # Force POSIX delivery → prompt inline on argv (see the resume test above).
    monkeypatch.setattr(llm_process, "_IS_WIN", False)
    history = [
        {"role": "user", "content": "eski soru"},
        {"role": "assistant", "content": "eski cevap"},
    ]

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, _RESULT_OK)
        captured = _patch_spawn(monkeypatch, proc)
        async for _ in claude_provider.stream_user_chat(
            settings,
            "yeni soru",
            history=history,
            reuse_agent=False,
        ):
            pass
        argv = captured["cmd"]
        assert "--resume" not in argv
        prompt = argv[argv.index("-p") + 1]
        # Default language English-first → English framing label (no TR leak).
        assert "[Previous conversation" in prompt
        assert "Önceki konuşma" not in prompt
        assert "User: eski soru" in prompt
        assert "Akana: eski cevap" in prompt
        assert prompt.rstrip().endswith("yeni soru")

    asyncio.run(run())


# ── BUG 3: Windows ``claude.cmd`` path — cmd /c + prompt→stdin + spilled files ──


class _FakeStdin:
    """Minimal asyncio StreamWriter stand-in: records bytes written before EOF."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.closed = False

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    async def drain(self) -> None:  # pragma: no cover - trivial
        pass

    def close(self) -> None:
        self.closed = True


class _FakeWinProc(_FakeProc):
    def __init__(self) -> None:
        super().__init__()
        self.stdin = _FakeStdin()


def test_windows_cmd_path_spills_prompt_and_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """On a Windows ``claude.cmd`` shim the turn must (BatBadBut): launch via ``cmd /c``,
    deliver the prompt over stdin (NOT on the argv), and spill the system prompt + MCP
    config to temp files — then clean those files up. POSIX is unaffected (other tests)."""
    from akana_server.orchestrator import llm_process

    settings = _make_settings(monkeypatch, tmp_path)
    cmd_path = r"C:\npm\claude.cmd"
    # Pretend we're on Windows and ``claude`` resolves (via PATHEXT) to the .cmd shim.
    monkeypatch.setattr(llm_process, "_IS_WIN", True)
    monkeypatch.setattr(
        llm_process.shutil, "which", lambda name: cmd_path if name == "claude" else None
    )

    captured: dict[str, Any] = {}

    async def run() -> None:
        proc = _FakeWinProc()  # built inside the loop (StreamReader needs one)
        proc.feed(_INIT, _RESULT_OK)
        captured["proc"] = proc

        async def _fake_spawn(*cmd: str, **kwargs: Any):
            captured["cmd"] = list(cmd)
            captured["kwargs"] = kwargs
            # Snapshot the spilled files WHILE they still exist (cleaned in finally).
            argv = list(cmd)
            sys_file = argv[argv.index("--append-system-prompt-file") + 1]
            mcp_file = argv[argv.index("--mcp-config") + 1]
            captured["sys_file"] = sys_file
            captured["mcp_file"] = mcp_file
            captured["sys_content"] = open(sys_file, encoding="utf-8").read()
            captured["mcp_content"] = open(mcp_file, encoding="utf-8").read()
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
        async for _ in claude_provider.stream_user_chat(
            settings,
            "merhaba & dünya %PATH%",  # cmd.exe metacharacters: MUST NOT reach the argv
            system_prompt="SECRET-SYSTEM",
            mcp_servers=MCP_SERVERS,
            reuse_agent=False,
        ):
            pass

    asyncio.run(run())

    proc = captured["proc"]
    argv = captured["cmd"]
    # 1) Launched through the cmd.exe wrapper with the resolved .cmd path.
    assert argv[:3] == ["cmd", "/c", cmd_path]
    # 2) Print mode, but the prompt is NOT a positional arg — it went to stdin.
    assert "-p" in argv
    joined = "\x00".join(argv)
    assert "merhaba" not in joined and "%PATH%" not in joined
    assert bytes("merhaba & dünya %PATH%", "utf-8") in bytes(proc.stdin.buf)
    assert proc.stdin.closed is True
    # 3) stdin was the chosen pipe (not DEVNULL).
    assert captured["kwargs"]["stdin"] == asyncio.subprocess.PIPE
    # 4) System prompt + MCP config were spilled to files (file FLAG, not the inline form).
    assert "--append-system-prompt" not in argv  # only the *-file variant
    assert captured["sys_content"] == "SECRET-SYSTEM"
    assert json.loads(captured["mcp_content"]) == {"mcpServers": MCP_SERVERS}
    # 5) Temp files are cleaned up after the turn.
    assert not os.path.exists(captured["sys_file"])
    assert not os.path.exists(captured["mcp_file"])


def test_windows_cmd_path_cleans_files_when_spawn_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """If the spawn itself raises FileNotFoundError, the spilled temp dir is still removed."""
    from akana_server.orchestrator import llm_process

    settings = _make_settings(monkeypatch, tmp_path)
    monkeypatch.setattr(llm_process, "_IS_WIN", True)
    monkeypatch.setattr(
        llm_process.shutil, "which", lambda name: r"C:\npm\claude.cmd" if name == "claude" else None
    )

    seen: dict[str, str] = {}

    async def _raise_fnf(*cmd: str, **kwargs: Any):
        argv = list(cmd)
        seen["sys_file"] = argv[argv.index("--append-system-prompt-file") + 1]
        raise FileNotFoundError("cmd missing")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _raise_fnf)

    async def run() -> None:
        with pytest.raises(LLMCallError):
            async for _ in claude_provider.stream_user_chat(
                settings, "x", system_prompt="S", reuse_agent=False
            ):
                pass

    asyncio.run(run())
    assert seen["sys_file"] and not os.path.exists(seen["sys_file"])


# --------------------------------------------------------------------------- #
# C8 — _read_line: a non-positive idle timeout means "no ceiling", not wait_for(0)
# --------------------------------------------------------------------------- #
def test_read_line_non_positive_timeout_is_not_instant_timeout() -> None:
    """combine_cap yields 0 to mean "idle ceiling disabled" (e.g. CLAUDE_BRIDGE_TIMEOUT=0).
    Passing that 0 straight to ``wait_for`` would time out INSTANTLY → every stream dies on
    the first read. ``_read_line`` must treat <=0 as "wait indefinitely"."""

    async def run() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"x":1}\n')
        assert await claude_provider._read_line(reader, 0) == b'{"x":1}\n'
        reader.feed_data(b'{"y":2}\n')
        assert await claude_provider._read_line(reader, -1.0) == b'{"y":2}\n'
        # a POSITIVE timeout with no data still raises (the hang ceiling still works)
        with pytest.raises(asyncio.TimeoutError):
            await claude_provider._read_line(reader, 0.01)

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# C4 — an OPEN breaker must fast-fail BEFORE the per-turn temp spill is created
# --------------------------------------------------------------------------- #
def test_open_breaker_does_not_create_spill_temp_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """On the Windows cmd-wrapper path a per-turn temp "spill" dir is created. If the
    circuit breaker is OPEN, the fast-fail must happen BEFORE that spill is created —
    otherwise the temp dir leaks (the try/finally that cleans it is never entered)."""
    from akana_server.network.breaker import BreakerOpenError
    from akana_server.network.guard import global_registry, reset_global_registry

    settings = _make_settings(monkeypatch, tmp_path)
    reset_global_registry()
    br = global_registry().get_or_create("claude", threshold=1, cooldown=999.0)
    br.record_failure()  # threshold=1 → breaker is now OPEN

    # Pretend we're on the Windows cmd-wrapper path so a spill WOULD be created downstream.
    monkeypatch.setattr(claude_provider, "needs_cmd_wrapper", lambda _bin: True)
    spills = {"n": 0}
    real_spill = claude_provider._ClaudeSpill

    def _counting_spill(*a: Any, **k: Any):
        spills["n"] += 1
        return real_spill(*a, **k)

    monkeypatch.setattr(claude_provider, "_ClaudeSpill", _counting_spill)

    async def run() -> None:
        with pytest.raises(BreakerOpenError):
            async for _ in claude_provider._stream_single_run(
                settings, "hi", reuse_agent=False
            ):
                pass
        assert spills["n"] == 0, "spill temp dir created despite open breaker (leak)"

    asyncio.run(run())
    reset_global_registry()


# --------------------------------------------------------------------------- #
# Batch 1 — agent activity: TodoWrite -> todo event; Task -> subagent boundaries + parent_id
# --------------------------------------------------------------------------- #
def test_todo_and_subagent_events_emitted(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """TodoWrite yields a typed `todo` progress event (PLUS the normal tool card, no turn
    boundary); Task yields subagent start/end boundaries and the subagent's nested tool call
    carries parent_id = the Task id (so the UI can nest it)."""
    settings = _make_settings(monkeypatch, tmp_path)
    todo_use = {
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use", "id": "td1", "name": "TodoWrite",
            "input": {"todos": [
                {"content": "Step A", "status": "in_progress"},
                {"content": "Step B", "status": "pending"},
            ]},
        }]},
    }
    task_use = {
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use", "id": "tk1", "name": "Task",
            "input": {"subagent_type": "explorer", "description": "explore the repo"},
        }]},
    }
    nested_use = {
        "type": "assistant",
        "parent_tool_use_id": "tk1",
        "message": {"content": [{"type": "tool_use", "id": "c1", "name": "Grep", "input": {}}]},
    }
    task_result = {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "tk1", "content": "done"}]},
    }

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, todo_use, task_use, nested_use, task_result, _RESULT_OK)
        _patch_spawn(monkeypatch, proc)
        events = [ev async for ev in claude_provider.stream_user_chat(settings, "yap")]

        todos = [e["todo"] for e in events if "todo" in e]
        assert len(todos) == 1
        assert [i["content"] for i in todos[0]["items"]] == ["Step A", "Step B"]
        assert todos[0]["items"][0]["status"] == "in_progress"
        tool_calls = [e["tool_call"] for e in events if "tool_call" in e]
        # TodoWrite still renders the generic card (dual path) and does NOT end the turn.
        assert any(c["name"] == "TodoWrite" and c["phase"] == "start" for c in tool_calls)

        subs = [e["subagent"] for e in events if "subagent" in e]
        assert any(s["phase"] == "start" and s["name"] == "explorer" and s["id"] == "tk1" for s in subs)
        assert any(s["phase"] == "end" and s["id"] == "tk1" and s["status"] == "ok" for s in subs)
        # The subagent's nested tool call is tagged with the parent Task id; top-level ones aren't.
        nested = [c for c in tool_calls if c["name"] == "Grep"]
        assert nested and nested[0]["parent_id"] == "tk1"
        assert all(
            c.get("parent_id") is None for c in tool_calls if c["name"] in ("TodoWrite", "Task")
        )

    asyncio.run(run())


def test_tool_call_delta_carries_id_and_name(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """B2 streaming tool input: input_json_delta chunks (keyed by block INDEX) are
    enriched with the tool's id/name captured at content_block_start, so the UI can
    stream them into the SAME card the tool_call start/end events (keyed by id) target."""
    settings = _make_settings(monkeypatch, tmp_path)
    block_start = {
        "type": "stream_event",
        "event": {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "b1", "name": "Bash", "input": {}},
        },
    }

    def _input_delta(partial: str, index: int = 1) -> dict[str, Any]:
        return {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "input_json_delta", "partial_json": partial},
            },
        }

    full_use = {
        "type": "assistant",
        "message": {"content": [{
            "type": "tool_use", "id": "b1", "name": "Bash",
            "input": {"command": "ls -la"},
        }]},
    }

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(
            _INIT,
            block_start,
            _input_delta('{"command":"ls'),
            _input_delta(' -la"}'),
            full_use,
            _RESULT_OK,
        )
        _patch_spawn(monkeypatch, proc)
        events = [ev async for ev in claude_provider.stream_user_chat(settings, "koş")]

        deltas = [e["tool_call_delta"] for e in events if "tool_call_delta" in e]
        assert len(deltas) == 2, "each input_json_delta should emit a tool_call_delta"
        # Each delta carries the tool id + name (resolved from content_block_start) + index.
        assert all(d["id"] == "b1" and d["name"] == "Bash" and d["index"] == 1 for d in deltas)
        assert [d["partial"] for d in deltas] == ['{"command":"ls', ' -la"}']
        # The full tool_call (start) uses the SAME id → UI patches the streamed card.
        starts = [e["tool_call"] for e in events if "tool_call" in e and e["tool_call"]["phase"] == "start"]
        assert any(c["id"] == "b1" and c["name"] == "Bash" for c in starts)

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# Thinking-mode drift guard: claude keeps its own finer 5-level effort table but
# its KEYS must stay exactly the canonical ``modes.THINKING_MODES`` set, so adding
# a new canonical mode can never silently leave the claude provider behind.
# --------------------------------------------------------------------------- #
def test_effort_levels_cover_canonical_modes() -> None:
    from akana_server.orchestrator import modes

    # Keys are exactly the canonical set (no missing/extra mode).
    assert set(claude_provider._EFFORT_LEVELS) == set(modes.THINKING_MODES)
    # Every value is a real ``--effort`` level the claude CLI accepts.
    assert set(claude_provider._EFFORT_LEVELS.values()) <= {
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    }
    # claude's finer granularity is real: derin/yogun/azami are DISTINCT (the
    # shared three-level tier_map collapses them all to "high").
    assert (
        claude_provider._EFFORT_LEVELS["derin"]
        != claude_provider._EFFORT_LEVELS["yogun"]
        != claude_provider._EFFORT_LEVELS["azami"]
    )
    # An unknown / empty mode leaves the flag off (CLI default preserved).
    assert claude_provider._effort_level(None) is None
    assert claude_provider._effort_level("nope") is None


def test_event_translator_is_wired() -> None:
    # The subprocess generator delegates event translation to the extracted
    # ClaudeEventTranslator (providers:arch:4 split). Sanity-check it is importable
    # and re-exported on the provider module for callers/tests.
    from akana_server.orchestrator.claude_events import ClaudeEventTranslator

    assert claude_provider.ClaudeEventTranslator is ClaudeEventTranslator


def test_text_ask_block_still_surfaces_sibling_tool_use(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A generic tool_use in the SAME assistant message as a text-protocol
    [[AKANA_ASK]] block is STILL surfaced (early-termination is deferred to the
    end of the content scan — mirrors the pre-split inline behaviour). Guards the
    providers:arch:4 refactor against dropping the sibling tool card.
    """
    settings = _make_settings(monkeypatch, tmp_path)
    ask_json = json.dumps({"questions": [{"question": "A or B?", "options": ["A", "B"]}]})
    assistant = {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": f"pre [[AKANA_ASK]]{ask_json}[[/AKANA_ASK]]"},
                {"type": "tool_use", "id": "tu-side", "name": "memory_search", "input": {"q": "x"}},
            ]
        },
    }

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, assistant, _RESULT_OK)
        _patch_spawn(monkeypatch, proc)
        events = [ev async for ev in claude_provider.stream_user_chat(settings, "sor")]

        # The ask_user card was emitted...
        assert any("ask_user" in e for e in events)
        # ...AND the sibling generic tool_use still produced a start tool_call.
        starts = [
            e["tool_call"]
            for e in events
            if "tool_call" in e and e["tool_call"]["phase"] == "start"
        ]
        assert any(c["id"] == "tu-side" and c["name"] == "memory_search" for c in starts)
        # Terminal done carries awaiting_user status + the ask payload.
        done = next(e for e in events if e.get("done"))
        assert done["status"] == "awaiting_user"
        assert "ask_user" in done

    asyncio.run(run())


# -- BUG 7: cancellation at the Windows cmd-path stdin drain must not leak ----------


def test_cancel_at_stdin_drain_cleans_up_proc_pid_and_spill(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """On the Windows ``cmd /c`` claude path the flattened bootstrap prompt can exceed
    the ~64KB pipe buffer, so ``await proc.stdin.drain()`` SUSPENDS until the CLI reads.
    That await sat OUTSIDE the guarded try/finally: a turn cancelled there (STOP /
    client disconnect at turn start) leaked a forever-blocked claude.cmd process, its
    pid was not registered yet (so no reaper could find it), and the temp spill dir
    (system prompt / MCP config, possibly the vault key) was never removed.

    The fix registers the pid BEFORE the drain and guards the drain region so a
    CancelledError kills the process group, releases the pid, and cleans the spill."""
    import tempfile as _tempfile

    from akana_server.orchestrator import claude_provider as _cp
    from akana_server.orchestrator.llm_process import llm_pid_dir

    settings = _make_settings(monkeypatch, tmp_path)

    # Force the Windows cmd-wrapper path (prompt over stdin, spill temp dir created).
    monkeypatch.setattr(_cp, "needs_cmd_wrapper", lambda _bin: True)

    # Spill dir lands under tmp_path so we can assert it is removed on cancel.
    spill_dirs: list[str] = []
    _real_mkdtemp = _tempfile.mkdtemp

    def _fake_mkdtemp(*a: Any, **kw: Any) -> str:
        kw["dir"] = str(tmp_path)
        d = _real_mkdtemp(*a, **kw)
        spill_dirs.append(d)
        return d

    monkeypatch.setattr(_cp.tempfile, "mkdtemp", _fake_mkdtemp)

    terminate_calls: list[int] = []

    async def _fake_terminate(pid: int) -> None:
        terminate_calls.append(pid)

    monkeypatch.setattr(_cp, "terminate_process_group", _fake_terminate)

    class _BlockingStdin:
        def __init__(self) -> None:
            self.closed = False

        def write(self, _data: bytes) -> None:
            return None

        async def drain(self) -> None:
            # Never completes — mirrors a full pipe buffer with the CLI not yet reading.
            # The turn is cancelled while awaiting here.
            await asyncio.Event().wait()

        def close(self) -> None:
            self.closed = True

    class _DrainBlockProc(_FakeProc):
        pid = 8484

        def __init__(self) -> None:
            super().__init__()
            self.stdin = _BlockingStdin()
            self.returncode = None  # still "alive" so the guard kills it

        def kill(self) -> None:
            self.returncode = -9

    async def run() -> None:
        proc = _DrainBlockProc()
        _patch_spawn(monkeypatch, proc)

        agen = _cp.stream_user_chat(settings, "selam")
        # __anext__ runs the setup up to (and blocking on) the stdin drain — it never
        # reaches the first yield because the drain suspends before the read loop.
        task = asyncio.ensure_future(agen.__anext__())
        # Let the coroutine advance to the drain await.
        for _ in range(20):
            await asyncio.sleep(0)
            if terminate_calls or spill_dirs:
                # spill dir now exists; give a couple more ticks to reach the drain.
                break
        # The pid MUST already be registered (reapable) before the drain.
        assert len(list(llm_pid_dir(tmp_path).glob("*.json"))) == 1, (
            "pid file must be registered BEFORE the stdin drain (reapable on cancel)"
        )
        # Cancel the turn exactly at the pending drain.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The blocked claude process was killed (process group terminated).
        assert 8484 in terminate_calls, "cancelled turn left the claude process alive"
        # The pid file was released (not leaked → next-boot reaper won't hit a stale pid).
        assert list(llm_pid_dir(tmp_path).glob("*.json")) == []
        # The spill temp dir (system prompt / MCP config) was removed.
        assert spill_dirs, "expected a spill dir on the cmd-wrapper path"
        assert not os.path.exists(spill_dirs[0]), "spill temp dir leaked on cancel"

    asyncio.run(run())
