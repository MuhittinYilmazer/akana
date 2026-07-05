"""Bridge stdout line reader (Python 3.11 compatibility) + bridge env/key resolution."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from akana_server.orchestrator.base import STDOUT_LINE_LIMIT
from akana_server.orchestrator.base import read_ndjson_line as _read_bridge_line
from akana_server.orchestrator.cursor_provider import (
    bridge_env as _bridge_env,
    ensure_api_key as _ensure_api_key,
)
from akana_server.orchestrator.llm_dispatch import LLMCallError
from akana_server.secret_store import set_secrets


def test_read_bridge_line_simple() -> None:
    async def run() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b'{"ev":"delta","text":"hi"}\n{"ev":"done"}\n')
        line1 = await _read_bridge_line(reader, timeout=1.0)
        assert line1 == b'{"ev":"delta","text":"hi"}\n'
        line2 = await _read_bridge_line(reader, timeout=1.0)
        assert line2 == b'{"ev":"done"}\n'

    asyncio.run(run())


def test_read_bridge_line_large_within_limit() -> None:
    async def run() -> None:
        payload = b"x" * (512 * 1024) + b"\n"
        assert len(payload) < STDOUT_LINE_LIMIT
        reader = asyncio.StreamReader()
        reader.feed_data(payload + b"tail\n")
        line = await _read_bridge_line(reader, timeout=2.0)
        assert line == payload
        tail = await _read_bridge_line(reader, timeout=1.0)
        assert tail == b"tail\n"

    asyncio.run(run())


def test_read_bridge_line_4mb_delta_survives() -> None:
    """Boundary value: a 4MB+ single line (large tool payload) is read losslessly,
    the next line is not corrupted (buffer push-back correctness)."""

    async def run() -> None:
        payload = b"y" * (4 * 1024 * 1024) + b"\n"
        assert len(payload) < STDOUT_LINE_LIMIT
        reader = asyncio.StreamReader()
        reader.feed_data(payload + b'{"ev":"done"}\n')
        line = await _read_bridge_line(reader, timeout=5.0)
        assert line == payload
        tail = await _read_bridge_line(reader, timeout=1.0)
        assert tail == b'{"ev":"done"}\n'

    asyncio.run(run())


def test_read_bridge_line_over_limit_raises_not_truncates() -> None:
    """A line exceeding the 8MB limit is not silently truncated — LimitOverrunError is raised
    (the chat layer surfaces this as STREAM_ERROR, does not process corrupt half JSON)."""

    async def run() -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"z" * (STDOUT_LINE_LIMIT + 16))  # NO newline
        reader.feed_eof()
        with pytest.raises(asyncio.LimitOverrunError):
            await _read_bridge_line(reader, timeout=10.0)

    asyncio.run(run())


def test_bridge_env_prefers_secret_store(tmp_path) -> None:
    set_secrets(tmp_path, {"cursor_api_key": "store-key-9999"})
    settings = SimpleNamespace(data_dir=tmp_path, cursor_api_key="env-key-1111")
    assert _bridge_env(settings)["CURSOR_API_KEY"] == "store-key-9999"


def test_bridge_env_falls_back_to_settings(tmp_path) -> None:
    settings = SimpleNamespace(data_dir=tmp_path, cursor_api_key="env-key-1111")
    assert _bridge_env(settings)["CURSOR_API_KEY"] == "env-key-1111"


def test_bridge_env_defensive_without_data_dir() -> None:
    settings = SimpleNamespace(cursor_api_key="env-key-1111")
    assert _bridge_env(settings)["CURSOR_API_KEY"] == "env-key-1111"
    assert _bridge_env(SimpleNamespace())["CURSOR_API_KEY"] == ""


def test_build_payload_includes_chat_mode_and_session() -> None:
    from akana_server.orchestrator.cursor_provider import build_payload as _build_payload

    settings = SimpleNamespace(
        data_dir=Path("/tmp/x"),
        cursor_model="composer-2",
        workspace=Path("/tmp/x"),  # unified cwd — _build_payload always reads settings.workspace
    )
    payload = _build_payload(
        settings,
        "merhaba",
        history=[],
        model="composer-2",
        stream=True,
        chat_mode=True,
        conversation_id="conv-1",
        agent_id="agent-1",
    )
    assert payload["chat_mode"] is True
    assert payload["session_key"] == "conv-1"
    assert payload["cursor_agent_id"] == "agent-1"


def test_build_payload_never_carries_thinking_mode() -> None:
    """Cursor has no effort/reasoning input knob (the SDK exposes reasoning only via
    a model-declared ModelSelection.params entry, not a plain toggle), so the effort
    control is a deliberate no-op on Cursor: ``build_payload`` neither accepts nor
    emits a ``thinking_mode`` key — the dispatch cursor branch does not forward it."""
    from akana_server.orchestrator.cursor_provider import build_payload as _build_payload

    settings = SimpleNamespace(
        data_dir=Path("/tmp/x"),
        cursor_model="composer-2",
        workspace=Path("/tmp/x"),
    )
    payload = _build_payload(
        settings,
        "merhaba",
        history=[],
        model="composer-2",
        stream=True,
    )
    assert "thinking_mode" not in payload
    # The dead key is gone at the signature level too: passing it is a TypeError.
    with pytest.raises(TypeError):
        _build_payload(
            settings, "merhaba", history=[], model="composer-2", stream=True,
            thinking_mode="derin",
        )


def test_scan_one_shot_need_history() -> None:
    from akana_server.orchestrator.cursor_provider import (
        scan_one_shot_need_history as _scan_one_shot_need_history,
    )

    assert _scan_one_shot_need_history('{"ev":"need_history"}\n') is True
    assert _scan_one_shot_need_history('{"ev":"done","ok":true}\n') is False


def test_ensure_api_key_accepts_store_only(tmp_path) -> None:
    settings = SimpleNamespace(data_dir=tmp_path, cursor_api_key=None)
    with pytest.raises(LLMCallError):
        _ensure_api_key(settings)
    set_secrets(tmp_path, {"cursor_api_key": "store-key-9999"})
    _ensure_api_key(settings)


def test_complete_chat_with_usage_chat_mode_aggregates_stream(monkeypatch) -> None:
    """Convergence A #6/#7: chat_mode=True aggregates the STREAMING bridge →
    usage['tool_calls'] + usage['agent_id'] populated (the one-shot path did not)."""
    from akana_server.orchestrator import llm_dispatch

    async def _fake_stream(settings, user_text, **kwargs):
        yield {"agent_id": "agent-xyz"}
        yield {"delta": "Mer", "done": False}
        yield {"delta": "haba", "done": False}
        yield {
            "done": True,
            "text": "Merhaba",
            "status": "finished",
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "tool_calls": []},
            "tool_calls": [
                {"id": "t1", "name": "memory_remember", "phase": "end", "status": "ok"}
            ],
        }

    monkeypatch.setattr(llm_dispatch, "stream_user_chat", _fake_stream)
    text, usage = asyncio.run(
        llm_dispatch.complete_chat_with_usage(SimpleNamespace(), "selam", chat_mode=True)
    )
    assert text == "Merhaba"
    assert usage["agent_id"] == "agent-xyz"  # #6: for reuse
    assert [t["name"] for t in usage["tool_calls"]] == ["memory_remember"]  # #7
    assert usage["prompt_tokens"] == 3


def test_complete_chat_with_usage_done_event_carrying_agent_id_keeps_usage(monkeypatch) -> None:
    """Regression: the REAL daemon terminal event carries agent_id INSIDE done
    (bridge_pool's done yield). The old ``elif done`` was mutually-exclusive →
    when agent_id arrived together with done the done body was SKIPPED, and
    usage+tool_calls were dropped (a silent regression since text survived the
    delta-join). The test above did not catch this because it emitted agent_id in a SEPARATE event."""
    from akana_server.orchestrator import llm_dispatch

    async def _fake_stream(settings, user_text, **kwargs):
        yield {"delta": "Mer", "done": False}
        yield {"delta": "haba", "done": False}
        # Daemon terminal event: agent_id + done + usage + tool_calls TOGETHER.
        yield {
            "done": True,
            "text": "Merhaba",
            "status": "finished",
            "usage": {"prompt_tokens": 7, "completion_tokens": 4},
            "tool_calls": [{"id": "t1", "name": "memory_remember", "phase": "end"}],
            "agent_id": "agent-xyz",
        }

    monkeypatch.setattr(llm_dispatch, "stream_user_chat", _fake_stream)
    text, usage = asyncio.run(
        llm_dispatch.complete_chat_with_usage(SimpleNamespace(), "selam", chat_mode=True)
    )
    assert text == "Merhaba"
    assert usage["agent_id"] == "agent-xyz"
    assert [t["name"] for t in usage["tool_calls"]] == ["memory_remember"]  # not dropped
    assert usage["prompt_tokens"] == 7  # token accounting preserved


def test_complete_chat_with_usage_capture_mode_stays_one_shot(monkeypatch) -> None:
    """chat_mode=False (memory-capture, stateless) stays one-shot — does NOT go to streaming."""
    from akana_server.orchestrator import llm_dispatch

    used_stream = {"hit": False}

    async def _fake_stream(*_a, **_k):
        used_stream["hit"] = True
        yield {"done": True, "text": "x"}

    async def _fake_complete(settings, user_text, **kwargs):
        return llm_dispatch.LLMResult(text="tek-atış yanıt", status="completed", raw={})

    monkeypatch.setattr(llm_dispatch, "stream_user_chat", _fake_stream)
    monkeypatch.setattr(llm_dispatch, "complete_chat", _fake_complete)
    text, usage = asyncio.run(
        llm_dispatch.complete_chat_with_usage(SimpleNamespace(), "selam", chat_mode=False)
    )
    assert text == "tek-atış yanıt"
    assert used_stream["hit"] is False  # did not go to the streaming bridge
    assert usage["tool_calls"] == []


# Is a PID still alive? (test-local; does not duplicate llm_process's internal helper).
def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - alive but not ours
        return True
    return True


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX process-group teardown (start_new_session/killpg); Windows uses taskkill",
)
def test_complete_chat_kills_child_on_timeout(tmp_path, monkeypatch) -> None:
    """Resource-leak regression: when the one-shot ``complete_chat`` times out the
    bridge CHILD PROCESS MUST be killed — it must not hang in the executor thread.

    The old path ran ``subprocess.run`` blocking inside a ``run_in_executor`` THREAD;
    since ``asyncio.wait_for`` cancellation cannot interrupt the thread, the child lived
    until its own internal timeout (seconds) → token waste + a hanging process. The new
    path is ``create_subprocess_exec`` (+ ``start_new_session``) → on cancel/timeout it
    takes down the WHOLE group via ``terminate_process_group``. The fake bridge writes
    its PID to a file and sleeps for a long time; AFTER the call times out that PID must be dead.
    """
    pidfile = tmp_path / "child.pid"
    fake_bridge = tmp_path / "fake_bridge.py"
    # consume stdin, write the PID, sleep long (call_timeout ≪ 30 s) → never produce output.
    fake_bridge.write_text(
        "import os, sys, time\n"
        "sys.stdin.buffer.read()\n"
        f"open(r'{pidfile}', 'w').write(str(os.getpid()))\n"
        "sys.stdout.flush()\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )

    from akana_server.orchestrator import cursor_provider, llm_dispatch
    from akana_server.network.guard import reset_global_registry

    reset_global_registry()  # test isolation: prior breaker state must not leak
    monkeypatch.setattr(llm_dispatch, "_active_provider", lambda _s: "cursor")
    monkeypatch.setattr(
        cursor_provider, "bridge_args", lambda _s: [sys.executable, str(fake_bridge)]
    )

    settings = SimpleNamespace(
        data_dir=tmp_path,
        bridge_dir=tmp_path,
        workspace=tmp_path,  # chat_mode=False → _build_payload cwd
        cursor_model="composer-2",
        cursor_api_key="k-test",
        # call_timeout = min(bridge_timeout, llm_total_timeout) = 0.5 s.
        bridge_timeout=0.5,
        llm_total_timeout=0.5,
        # retry + breaker off → exactly ONE spawn, deterministic.
        network_max_retries=1,
        network_breaker_threshold=0,
    )

    started = time.monotonic()
    with pytest.raises(LLMCallError) as ei:
        asyncio.run(llm_dispatch.complete_chat(settings, "selam", chat_mode=False))
    elapsed = time.monotonic() - started

    assert ei.value.status_code == 504  # the «LLM_TIMEOUT» contract is preserved
    assert elapsed < 10  # exited by timeout WITHOUT waiting the 30 s sleep (did not hang in the thread)

    # Was the child PID written to the file? (the spawn really happened)
    deadline = time.monotonic() + 3.0
    while not pidfile.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pidfile.exists(), "the fake bridge never started (spawn could not be verified)"
    child_pid = int(pidfile.read_text().strip())

    # CRITICAL: give a short window for the SIGTERM→SIGKILL escalation, then it must be dead.
    deadline = time.monotonic() + 5.0
    while _pid_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _pid_alive(child_pid), (
        f"the bridge child process (pid={child_pid}) is STILL alive after the timeout — leak"
    )

    # the pid record file must also be cleaned up (release_llm_process ran in finally).
    leftover = list((Path(tmp_path) / "run" / "llm").glob("*.json"))
    assert leftover == [], f"the llm pid record file was not cleaned up: {leftover}"
