"""LLM call layer hang protection — idle (streaming) + total (blocking) cap.

INCIDENT: if a Cursor-bridge / claude-CLI call (network stall, frozen sub-
process) stops producing chunks and hangs FOREVER, the user sees an unresolving
spinner ("the page is freezing, restart required"). These tests prove, WITHOUT
making a real LLM call (fake bridge/proc; we control the underlying read
timing), that the new caps end the turn with a clean «LLM_TIMEOUT» (504) and do
not wrongly interrupt HEALTHY calls (a slow but progressing stream).

The caps impose a STRICTER limit via ``min()`` on top of the existing
``bridge_timeout`` / ``claude_bridge_timeout`` (30 min); tests gain speed by
making the knob small (0.2 s). On overrun, the existing cancel/cleanup path
(``terminate_process_group`` / ``subprocess.run`` internal timeout) is triggered
— no new leak path.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from typing import Any

import pytest

from akana_server.config import Settings, load_settings
from akana_server.orchestrator import base, claude_provider, cursor_provider, llm_dispatch
from akana_server.orchestrator.llm_dispatch import LLMCallError
from akana_server.runtime_settings import reset_runtime_stores

# Small idle/total cap used in tests — for speed instead of the real 120/300 s.
_TEST_TIMEOUT = 0.2
# Upper bound (seconds) within which the actual hang tests must run; if the cap is
# 0.2 s, the turn must end with LLM_TIMEOUT within ~this at the latest (otherwise it means "it hung").
_MAX_WALL = 5.0


@pytest.fixture(autouse=True)
def _isolate_runtime_stores():
    """Ensure each test sees a clean runtime store cache (no knob env leaking)."""
    reset_runtime_stores()
    yield
    reset_runtime_stores()


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Settings:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CURSOR_API_KEY", "test-key-123")
    return load_settings()


# --------------------------------------------------------------------------- #
# Fake claude subprocess (exactly following the test_claude_provider.py pattern)
# --------------------------------------------------------------------------- #
class _FakeProc:
    pid = 4242
    returncode: int | None = 0

    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        # Claude cmd-wrapper spill path checks proc.stdin (None → write skipped). Subclasses
        # that exercise the cursor bridge override this with a recording _FakeStdin.
        self.stdin = None

    def feed(self, *events: dict[str, Any], eof: bool = True) -> None:
        for ev in events:
            self.stdout.feed_data((json.dumps(ev) + "\n").encode("utf-8"))
        if eof:
            self.stdout.feed_eof()

    async def wait(self) -> int:
        return self.returncode or 0

    def kill(self) -> None:  # pragma: no cover - only on the timeout path
        self.returncode = -9


def _patch_spawn(monkeypatch: pytest.MonkeyPatch, proc: _FakeProc) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def _fake_spawn(*cmd: str, **kwargs: Any):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    return captured


def _neuter_killpg(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Prevent real process killing (fake pid) + make terminate return quickly."""
    calls: list[int] = []
    monkeypatch.setattr(
        "akana_server.orchestrator.llm_process.os.killpg",
        lambda pgid, sig: calls.append(pgid),
    )
    monkeypatch.setattr(
        "akana_server.orchestrator.llm_process._pid_alive", lambda pid: False
    )
    return calls


_INIT = {"type": "system", "subtype": "init", "session_id": "sess-1", "model": "claude-sonnet-4-6"}
_RESULT_OK = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "tamam",
    "usage": {"input_tokens": 5, "output_tokens": 3},
    "session_id": "sess-1",
}


def _delta(text: str) -> dict[str, Any]:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        },
    }


# --------------------------------------------------------------------------- #
# (b) STREAM HANGS MID-STREAM → idle-timeout fires → clean LLM_TIMEOUT
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="asserts POSIX process-group kill (os.killpg via _neuter_killpg); Windows uses taskkill",
)
def test_claude_stream_hang_midstream_idle_timeout_fires(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """init + one delta arrive, then the stream GOES SILENT (no new chunk, no EOF).

    Instead of the existing 30 min ``claude_bridge_timeout``, the 0.2 s idle cap
    fires; the turn ends ~immediately with «LLM_TIMEOUT» (504), the process group
    is killed and the TEST DOES NOT HANG (wall-clock < _MAX_WALL)."""
    monkeypatch.setenv("AKANA_LLM_IDLE_TIMEOUT", str(_TEST_TIMEOUT))
    settings = _make_settings(monkeypatch, tmp_path)
    killpg_calls = _neuter_killpg(monkeypatch)

    async def run() -> None:
        proc = _FakeProc()
        # feed init + delta but DO NOT give EOF and DO NOT SEND a terminal event → hang.
        proc.feed(_INIT, _delta("yarım"), eof=False)
        _patch_spawn(monkeypatch, proc)

        deltas: list[str] = []
        t0 = time.monotonic()
        with pytest.raises(LLMCallError) as exc:
            async for ev in claude_provider.stream_user_chat(settings, "selam"):
                if "delta" in ev:
                    deltas.append(ev["delta"])
        elapsed = time.monotonic() - t0

        assert exc.value.status_code == 504
        assert "LLM_TIMEOUT" in str(exc.value)
        assert deltas == ["yarım"]  # the chunk before the hang reached the user
        assert proc.pid in killpg_calls  # process group was killed (existing cleanup)
        assert elapsed < _MAX_WALL  # did not hang FOREVER

    asyncio.run(asyncio.wait_for(run(), timeout=_MAX_WALL))


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="asserts POSIX process-group kill (os.killpg via _neuter_killpg); Windows uses taskkill",
)
def test_cursor_direct_stream_hang_idle_timeout_fires(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The idle cap also fires on the Cursor direct (daemon-less) stream.

    With ``AKANA_BRIDGE_DAEMON=0`` the in-file read loop is used; the bridge
    spawn/key guards are bypassed with fakes."""
    monkeypatch.setenv("AKANA_LLM_IDLE_TIMEOUT", str(_TEST_TIMEOUT))
    monkeypatch.setenv("AKANA_BRIDGE_DAEMON", "0")
    monkeypatch.setenv("AKANA_NETWORK_BREAKER_THRESHOLD", "0")  # breaker off
    settings = _make_settings(monkeypatch, tmp_path)
    killpg_calls = _neuter_killpg(monkeypatch)

    # Bridge spawn preconditions: assume the script exists, make the key check a no-op.
    monkeypatch.setattr(cursor_provider, "ensure_api_key", lambda s: None)
    monkeypatch.setattr(cursor_provider, "bridge_args", lambda s: ["node", "fake.mjs"])
    monkeypatch.setattr(cursor_provider, "bridge_env", lambda s: {})

    class _CursorHangProc(_FakeProc):
        pid = 4343

        def __init__(self) -> None:
            super().__init__()
            self.stdin = _FakeStdin()
            # feed one delta, then go silent (no EOF, no done) → hang.
            self.stdout.feed_data(b'{"ev":"delta","text":"ya"}\n')

    async def run() -> None:
        proc = _CursorHangProc()
        _patch_spawn(monkeypatch, proc)
        deltas: list[str] = []
        t0 = time.monotonic()
        with pytest.raises(LLMCallError) as exc:
            async for ev in llm_dispatch.stream_user_chat(settings, "selam"):
                if "delta" in ev:
                    deltas.append(ev["delta"])
        elapsed = time.monotonic() - t0
        assert exc.value.status_code == 504
        assert "LLM_TIMEOUT" in str(exc.value)
        assert deltas == ["ya"]
        assert proc.pid in killpg_calls
        assert elapsed < _MAX_WALL

    asyncio.run(asyncio.wait_for(run(), timeout=_MAX_WALL))


class _FakeStdin:
    """The Cursor stream writes a payload to stdin; the fake end swallows it all."""

    def write(self, data: bytes) -> None:  # noqa: D401 - asyncio StreamWriter shape
        pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- #
# (c) STREAM SLOW BUT PROGRESSING → under the idle window → NO FALSE timeout
# --------------------------------------------------------------------------- #
def test_claude_slow_progressing_stream_does_not_false_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Chunks arrive at intervals SHORTER than the idle window → the turn ends normally.

    Because each chunk resets the counter, even if the total duration (4 × 0.08 =
    0.32 s) EXCEEDS the idle cap (0.2 s), the timeout DOES NOT FIRE — a critical
    robustness point: a long but live stream must never be wrongly interrupted."""
    monkeypatch.setenv("AKANA_LLM_IDLE_TIMEOUT", str(_TEST_TIMEOUT))  # 0.2 s
    settings = _make_settings(monkeypatch, tmp_path)

    gap = _TEST_TIMEOUT * 0.4  # 0.08 s — SAFELY under the idle window
    events_to_feed = [_INIT, _delta("a"), _delta("b"), _delta("c"), _RESULT_OK]

    async def run() -> None:
        proc = _FakeProc()
        _patch_spawn(monkeypatch, proc)

        async def _feeder() -> None:
            # Feed chunks over time: the gap between each is < the idle cap.
            for ev in events_to_feed:
                await asyncio.sleep(gap)
                proc.stdout.feed_data((json.dumps(ev) + "\n").encode("utf-8"))
            proc.stdout.feed_eof()

        feeder = asyncio.create_task(_feeder())
        deltas: list[str] = []
        final: dict[str, Any] | None = None
        async for ev in claude_provider.stream_user_chat(settings, "selam"):
            if "delta" in ev:
                deltas.append(ev["delta"])
            if ev.get("done"):
                final = ev
        await feeder

        assert deltas == ["a", "b", "c"]  # all chunks arrived, no interruption
        assert final is not None and final["done"] is True
        assert final["text"] == "abc"
        assert final["status"] == "finished"

    asyncio.run(asyncio.wait_for(run(), timeout=_MAX_WALL))


# --------------------------------------------------------------------------- #
# (a) BLOCKING (non-streaming) CALL HANGS → total-timeout fires → LLM_TIMEOUT
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="asserts POSIX process-group kill (os.killpg via _neuter_killpg); Windows uses taskkill",
)
def test_cursor_blocking_call_total_timeout_fires_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """If a blocking ``complete_chat`` hangs (``communicate`` never returns), the
    guard's total-duration cap (asyncio.wait_for) gives a clean «LLM_TIMEOUT» (504)
    and the process group is killed — it does not hang FOREVER (the user sees no frozen spinner).

    No real node: ``create_subprocess_exec`` is replaced with a fake; the fake
    ``communicate`` sleeps LONGER than the cap → only the outer guard cap kicks in."""
    monkeypatch.setenv("AKANA_LLM_TOTAL_TIMEOUT", str(_TEST_TIMEOUT))
    monkeypatch.setenv("AKANA_NETWORK_MAX_RETRIES", "1")  # no retry → single attempt
    monkeypatch.setenv("AKANA_NETWORK_BREAKER_THRESHOLD", "0")
    settings = _make_settings(monkeypatch, tmp_path)
    killpg_calls = _neuter_killpg(monkeypatch)
    monkeypatch.setattr(cursor_provider, "ensure_api_key", lambda s: None)
    monkeypatch.setattr(cursor_provider, "bridge_args", lambda s: ["node", "fake.mjs"])
    monkeypatch.setattr(cursor_provider, "bridge_env", lambda s: {})

    class _HangProc:
        pid = 5151
        returncode: int | None = None

        async def communicate(self, _input: bytes | None = None):
            await asyncio.sleep(_MAX_WALL * 2)  # hang; the guard wait_for cuts it first
            return b"", b""

        def kill(self) -> None:  # pragma: no cover - the killpg path is used
            self.returncode = -9

    _patch_spawn(monkeypatch, _HangProc())

    async def run() -> None:
        t0 = time.monotonic()
        with pytest.raises(LLMCallError) as exc:
            await llm_dispatch.complete_chat(settings, "selam")
        elapsed = time.monotonic() - t0
        assert exc.value.status_code == 504
        assert "LLM_TIMEOUT" in str(exc.value)
        assert 5151 in killpg_calls  # the hung process group was cleaned up (no orphan)
        assert elapsed < _MAX_WALL  # did not hang FOREVER

    asyncio.run(asyncio.wait_for(run(), timeout=_MAX_WALL))


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="asserts POSIX process-group kill (os.killpg via _neuter_killpg); Windows uses taskkill",
)
def test_cursor_blocking_call_bridge_failure_maps_to_friendly_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """If the bridge fails on a blocking call (returncode≠0, MODULE_NOT_FOUND
    stderr), the user sees a CLEAR + actionable message ('npm install'), NOT a raw
    Node stack trace. No fallback — only an explicit error (the user's decision)."""
    monkeypatch.setenv("AKANA_NETWORK_MAX_RETRIES", "1")
    monkeypatch.setenv("AKANA_NETWORK_BREAKER_THRESHOLD", "0")
    settings = _make_settings(monkeypatch, tmp_path)
    _neuter_killpg(monkeypatch)
    monkeypatch.setattr(cursor_provider, "ensure_api_key", lambda s: None)
    monkeypatch.setattr(cursor_provider, "bridge_args", lambda s: ["node", "fake.mjs"])
    monkeypatch.setattr(cursor_provider, "bridge_env", lambda s: {})

    stderr = (
        b"node:internal/modules/cjs/loader:818\n  throw err;\n  ^\n"
        b"Error: Cannot find module 'foo'\n  code: 'MODULE_NOT_FOUND'"
    )

    class _FailProc:
        pid = 5252
        returncode = 1

        async def communicate(self, _input: bytes | None = None):
            return b"", stderr

        def kill(self) -> None:  # pragma: no cover
            pass

    _patch_spawn(monkeypatch, _FailProc())

    async def run() -> None:
        with pytest.raises(LLMCallError) as exc:
            await llm_dispatch.complete_chat(settings, "selam")
        msg = str(exc.value)
        assert "npm install" in msg  # actionable guidance
        assert "MODULE_NOT_FOUND" not in msg and "loader" not in msg  # raw trace does not leak

    asyncio.run(asyncio.wait_for(run(), timeout=_MAX_WALL))


# --------------------------------------------------------------------------- #
# OFF-SWITCH + combining logic (knob behavior contract)
# --------------------------------------------------------------------------- #
def test_idle_timeout_disabled_falls_back_to_bridge_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``llm_idle_timeout=0`` → the new cap is OFF; effective idle = bridge_timeout.

    Behavior reverts exactly to the old one (no extra ceiling)."""
    monkeypatch.setenv("AKANA_LLM_IDLE_TIMEOUT", "0")
    monkeypatch.setenv("CURSOR_BRIDGE_TIMEOUT", "123")
    monkeypatch.setenv("CLAUDE_BRIDGE_TIMEOUT", "456")
    settings = _make_settings(monkeypatch, tmp_path)
    assert base.idle_timeout(settings) == 123.0
    assert claude_provider._idle_timeout(settings) == 456.0


def test_idle_timeout_takes_min_not_max(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Effective cap = min(bridge_timeout, llm_idle_timeout) — never loosens.

    bridge=1800, idle=120 → 120 (stricter); bridge=60, idle=120 → 60 (if the
    existing one is stricter it is kept, the knob does not extend it)."""
    monkeypatch.setenv("AKANA_LLM_IDLE_TIMEOUT", "120")
    monkeypatch.setenv("CURSOR_BRIDGE_TIMEOUT", "1800")
    settings = _make_settings(monkeypatch, tmp_path)
    assert base.idle_timeout(settings) == 120.0

    reset_runtime_stores()
    monkeypatch.setenv("CURSOR_BRIDGE_TIMEOUT", "60")
    settings = _make_settings(monkeypatch, tmp_path)
    assert base.idle_timeout(settings) == 60.0


def test_total_timeout_disabled_falls_back_to_bridge_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``llm_total_timeout=0`` → the blocking cap is OFF; effective = bridge_timeout."""
    monkeypatch.setenv("AKANA_LLM_TOTAL_TIMEOUT", "0")
    monkeypatch.setenv("CURSOR_BRIDGE_TIMEOUT", "777")
    settings = _make_settings(monkeypatch, tmp_path)
    assert base.total_timeout(settings) == 777.0


def test_default_caps_disabled_fall_back_to_bridge(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """If the knob is not set, the default is OFF (idle=0, total=0 — the user's
    choice) → NO extra idle/total ceiling; effective cap = bridge_timeout. Long
    thinking/tool use is not interrupted; only the bridge last-resort cap (30 min) remains."""
    monkeypatch.delenv("AKANA_LLM_IDLE_TIMEOUT", raising=False)
    monkeypatch.delenv("AKANA_LLM_TOTAL_TIMEOUT", raising=False)
    monkeypatch.setenv("CURSOR_BRIDGE_TIMEOUT", "1800")
    monkeypatch.setenv("CLAUDE_BRIDGE_TIMEOUT", "1800")
    settings = _make_settings(monkeypatch, tmp_path)
    assert base.idle_timeout(settings) == 1800.0
    assert base.total_timeout(settings) == 1800.0
    assert claude_provider._idle_timeout(settings) == 1800.0
