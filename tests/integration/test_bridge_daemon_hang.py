"""Bridge DAEMON stream hang protection — per-turn idle ceiling (the most common path).

INCIDENT context: the LLM hang knobs (``llm_idle_timeout`` / ``llm_total_timeout``)
at first only tightened the DIRECT cursor + claude paths. But the DEFAULT stream
path is the persistent **daemon** (``BridgePool.stream_run``): its per-turn idle wait
was still bound to the generous ``bridge_timeout`` (~30 min) → the most-used path was
not protected against a hang. These tests prove that the daemon idle ceiling is now
``min(bridge_timeout, llm_idle_timeout)`` and that it falls back to the existing
cancel/cleanup path (``abort_run`` — run-cancellation on the shared daemon, the IDE
STOP equivalent); and that it does NOT WRONGLY cut a HEALTHY but slowly progressing stream.

NO REAL subprocess/LLM: ``asyncio.create_subprocess_exec`` is replaced with a fake;
the fake daemon's stdout is a ``StreamReader`` that we feed (the type the pool reads in
production). The knob is small in the test (0.2 s) → speed. Each test is wrapped in an
outer ``asyncio.wait_for`` so that a regression does NOT HANG CI.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from akana_server.config import Settings, load_settings
from akana_server.orchestrator import bridge_pool
from akana_server.orchestrator.bridge_pool import BridgePool
from akana_server.orchestrator.llm_dispatch import LLMCallError
from akana_server.runtime_settings import reset_runtime_stores

# Small idle ceiling used in tests — for speed instead of the real 120 s.
_TEST_IDLE = 0.2
# Upper bound (s) within which the hang tests must run; if the ceiling is 0.2 s the turn
# must finish with LLM_TIMEOUT at the latest within ~this (otherwise it means "hung").
_MAX_WALL = 5.0

_PONG = {"id": "ping", "ev": "pong"}


@pytest.fixture(autouse=True)
def _isolate_runtime_stores():
    """Each test sees a clean runtime store cache (so knob env does not leak)."""
    reset_runtime_stores()
    yield
    reset_runtime_stores()


class _FakeStdin:
    def __init__(self) -> None:
        self.data = b""

    def write(self, chunk: bytes) -> None:
        self.data += chunk

    async def drain(self) -> None:
        return None

    def lines(self) -> list[dict[str, Any]]:
        return [
            json.loads(ln)
            for ln in self.data.decode("utf-8").splitlines()
            if ln.strip()
        ]


class _FakeProc:
    """Fake daemon process: stdout is fed, stdin is swallowed, kill is tracked."""

    pid = 4242
    returncode: int | None = None

    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.stdout = asyncio.StreamReader()
        self.stderr = None
        self.killed = False

    def feed(self, *events: dict[str, Any], eof: bool = True) -> None:
        for ev in events:
            self.stdout.feed_data((json.dumps(ev) + "\n").encode("utf-8"))
        if eof:
            self.stdout.feed_eof()

    def kill(self) -> None:  # pragma: no cover - only on the cleanup path
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


def _make_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path, *, bridge_timeout: str = "1800"
) -> Settings:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CURSOR_API_KEY", "test-key-123")
    monkeypatch.setenv("CURSOR_MODEL", "composer-2")
    monkeypatch.setenv("CURSOR_BRIDGE_TIMEOUT", bridge_timeout)
    return load_settings()


def _patch_spawn(monkeypatch: pytest.MonkeyPatch, proc: _FakeProc) -> dict[str, Any]:
    spawned: dict[str, Any] = {}

    async def _fake_spawn(*cmd: str, **kwargs: Any):
        spawned["cmd"] = list(cmd)
        spawned["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    return spawned


# --------------------------------------------------------------------------- #
# (a) DAEMON STREAM GOES SILENT MID-STREAM → idle ceiling fires → clean LLM_TIMEOUT
# --------------------------------------------------------------------------- #
def test_daemon_stream_silent_midstream_idle_timeout_fires(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """pong + one delta arrive, then the daemon GOES SILENT (no new chunk, no EOF, no done).

    Old behavior: the per-turn wait was bound to the ~30 min ``bridge_timeout`` → the stream
    would in practice hang forever. New: the 0.2 s idle ceiling fires; the turn ends almost
    instantly with «LLM_TIMEOUT» (504), the EXISTING cleanup (``abort_run`` — cuts this run)
    is called, and the TEST DOES NOT HANG (wall-clock < _MAX_WALL).
    """
    monkeypatch.setenv("AKANA_LLM_IDLE_TIMEOUT", str(_TEST_IDLE))
    settings = _make_settings(monkeypatch, tmp_path)

    async def run() -> None:
        proc = _FakeProc()
        # Feed pong (for ping) + one delta but DON'T send EOF, DON'T send a terminal → hang.
        proc.feed(
            _PONG,
            {"id": "1", "ev": "delta", "text": "yarım"},
            eof=False,
        )
        _patch_spawn(monkeypatch, proc)
        pool = BridgePool(settings)

        deltas: list[str] = []
        t0 = time.monotonic()
        with pytest.raises(LLMCallError) as exc:
            async for ev in pool.stream_run(
                {"prompt": "p", "session_key": "conv-hang"}
            ):
                if "delta" in ev:
                    deltas.append(ev["delta"])
        elapsed = time.monotonic() - t0

        assert exc.value.status_code == 504
        assert "LLM_TIMEOUT" in str(exc.value)
        assert deltas == ["yarım"]  # the chunk before the hang reached the user
        assert elapsed < _MAX_WALL  # did NOT hang FOREVER (not the old 30 min)
        # EXISTING cleanup path: abort_run was written for this run (no new kill path;
        # the shared daemon process-group is not killed, only this run is cut).
        abort_ops = [
            ln
            for ln in proc.stdin.lines()
            if ln.get("op") == "abort_run" and ln.get("session_key") == "conv-hang"
        ]
        assert abort_ops, "abort_run (the existing cleanup) was expected after the idle-timeout"
        # The shared persistent daemon process-group was NOT KILLED (concurrent conversations survive).
        assert proc.killed is False

    asyncio.run(asyncio.wait_for(run(), timeout=_MAX_WALL))


# --------------------------------------------------------------------------- #
# (b) DAEMON STREAM SLOW BUT PROGRESSING → under the idle window → NO FALSE timeout
# --------------------------------------------------------------------------- #
def test_daemon_slow_progressing_stream_does_not_false_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Chunks arrive at intervals SHORTER than the idle window → the turn ends normally.

    CRITICAL ROBUSTNESS: each chunk (delta) RE-ARMS the ``wait_for(q.get(), idle)`` wait →
    the idle counter resets. Even if the total time (5 × 0.08 = 0.40 s) EXCEEDS the idle
    ceiling (0.2 s), the timeout is NOT TRIGGERED — a long but LIVE daemon stream must never
    be wrongly cut (long tool calls, deep thinking, etc.).
    """
    monkeypatch.setenv("AKANA_LLM_IDLE_TIMEOUT", str(_TEST_IDLE))  # 0.2 s
    settings = _make_settings(monkeypatch, tmp_path)

    gap = _TEST_IDLE * 0.4  # 0.08 s — SAFELY under the idle window
    # 5 chunks → 5 × 0.08 = 0.40 s total > 0.2 s idle (would time out if the counter didn't reset).
    timed_events = [
        {"id": "1", "ev": "delta", "text": "a"},
        {"id": "1", "ev": "delta", "text": "b"},
        {"id": "1", "ev": "heartbeat", "phase": "run_wait"},
        {"id": "1", "ev": "delta", "text": "c"},
        {"id": "1", "ev": "done", "ok": True, "text": "abc", "status": "finished"},
    ]

    async def run() -> None:
        proc = _FakeProc()
        # Feed pong immediately (let ping pass); the persistent daemon does not close stdout.
        proc.feed(_PONG, eof=False)
        _patch_spawn(monkeypatch, proc)
        pool = BridgePool(settings)

        async def _feeder() -> None:
            # Feed chunks over time: the gap between each is < the idle ceiling.
            for ev in timed_events:
                await asyncio.sleep(gap)
                proc.stdout.feed_data((json.dumps(ev) + "\n").encode("utf-8"))

        feeder = asyncio.create_task(_feeder())
        deltas: list[str] = []
        final: dict[str, Any] | None = None
        t0 = time.monotonic()
        async for ev in pool.stream_run({"prompt": "p", "session_key": "conv-slow"}):
            if "delta" in ev:
                deltas.append(ev["delta"])
            if ev.get("done"):
                final = ev
        elapsed = time.monotonic() - t0
        await feeder

        assert deltas == ["a", "b", "c"]  # all chunks arrived, NO INTERRUPTION
        assert final is not None and final["done"] is True
        assert final["text"] == "abc"
        assert final["status"] == "finished"
        # The total time exceeded the idle ceiling (0.2) but there was no timeout → proof of reset.
        assert elapsed > _TEST_IDLE
        assert proc.killed is False  # healthy stream: the process was untouched

    asyncio.run(asyncio.wait_for(run(), timeout=_MAX_WALL))


# --------------------------------------------------------------------------- #
# KNOB CONTRACT — daemon idle ceiling = min(bridge_timeout, llm_idle_timeout)
# --------------------------------------------------------------------------- #
def test_daemon_idle_timeout_takes_min_not_max(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The effective daemon idle ceiling never LOOSENS: the smaller of the two.

    bridge=1800, idle=120 → 120 (tighter); bridge=60, idle=120 → 60 (the existing one is kept
    if it's tighter; the knob does not extend it). Exactly the same logic as the cursor/claude paths.
    """
    monkeypatch.setenv("AKANA_LLM_IDLE_TIMEOUT", "120")
    settings = _make_settings(monkeypatch, tmp_path, bridge_timeout="1800")
    assert bridge_pool._idle_timeout(settings) == 120.0

    reset_runtime_stores()
    settings = _make_settings(monkeypatch, tmp_path, bridge_timeout="60")
    assert bridge_pool._idle_timeout(settings) == 60.0


def test_daemon_idle_timeout_disabled_falls_back_to_bridge_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``llm_idle_timeout=0`` → ceiling OFF; effective idle = bridge_timeout (old behavior)."""
    monkeypatch.setenv("AKANA_LLM_IDLE_TIMEOUT", "0")
    settings = _make_settings(monkeypatch, tmp_path, bridge_timeout="777")
    assert bridge_pool._idle_timeout(settings) == 777.0


def test_daemon_idle_timeout_default_disabled_uses_bridge(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """If the knob is not set, the default is OFF (0, user preference) → effective idle =
    bridge_timeout; NO extra inter-chunk ceiling (long thinking/tool use is not cut).
    """
    monkeypatch.delenv("AKANA_LLM_IDLE_TIMEOUT", raising=False)
    settings = _make_settings(monkeypatch, tmp_path, bridge_timeout="1800")
    assert bridge_pool._idle_timeout(settings) == 1800.0


# --------------------------------------------------------------------------- #
# HEALTH (secondary): an EXITED daemon is not reused — the EXISTING logic already covers this
# --------------------------------------------------------------------------- #
def test_exited_daemon_is_recreated_not_reused(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """If the pooled daemon is DEAD/EXITED (``returncode`` set) it is not handed out again;
    ``_ensure_proc`` spawns a fresh daemon (no handing out a zombie).

    NOTE (secondary-task rationale): an extra ``_eof_seen``/``os.kill(pid,0)`` health probe was
    considered but NOT ADDED — it broke the persistent SHARED daemon's existing "reuse the same
    daemon after an active-run error" contract (two existing tests) and its benefit largely
    overlapped with the existing ``returncode``-based recreation + mid-stream-death cleanup. This
    test pins down that the "an exited daemon is not reused" behavior is ALREADY provided.
    """
    settings = _make_settings(monkeypatch, tmp_path)

    procs: list[_FakeProc] = []

    async def _fake_spawn(*cmd: str, **kwargs: Any):
        proc = _FakeProc()
        procs.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)
    # We test the SELECTION logic (don't hand out an exited proc), not the ping/reader
    # handshake → make ping a no-op (the fake reader race is not the subject of this test).
    async def _noop_ping(self) -> None:  # noqa: ANN001
        return None

    monkeypatch.setattr(BridgePool, "_ping_unlocked", _noop_ping)
    pool = BridgePool(settings)

    async def run() -> None:
        first = await pool._ensure_proc()
        assert len(procs) == 1
        # The same (live) daemon is KEPT on the second call (healthy reuse, no new spawn).
        assert await pool._ensure_proc() is first
        assert len(procs) == 1

        # EXITED daemon simulation: returncode set → the next _ensure_proc does not reuse it
        # and opens a fresh daemon (the existing returncode-guard).
        first.returncode = 0
        second = await pool._ensure_proc()
        assert second is not first
        assert second.returncode is None  # the fresh daemon is alive
        assert len(procs) == 2

    asyncio.run(asyncio.wait_for(run(), timeout=_MAX_WALL))
