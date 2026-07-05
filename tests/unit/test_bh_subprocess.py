"""Subprocess-teardown leak locks (bug-hunt batch "Subprocess lifecycle").

Hermetic: NO real subprocess is spawned. ``asyncio.create_subprocess_exec`` is
monkeypatched to hand back a fake proc, and the reaper's liveness/kill helpers are
monkeypatched so the process-group logic is exercised without touching the OS.

Covered regressions (see plan batch 5):
  (a) mcp_selfcheck._check_one: CancelledError during the handshake kills AND reaps
      the child before re-raising (shutdown no longer orphans the MCP subprocess).
  (b) mcp_selfcheck._check_one: the timeout/error branch awaits the killed child
      (proc.wait) so it is reaped rather than left a zombie + open pipe FDs.
  (c) llm_process.reap_orphan_llm_processes: the grace loop + force-kill guard consult
      _group_alive (whole group) rather than only _pid_alive (leader), so an orphan
      child that outlives the leader is still SIGKILL'd.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from akana_server.orchestrator import llm_process, mcp_selfcheck


# -- fake asyncio subprocess (duck-type of asyncio.subprocess.Process) --------------


class _FakeProc:
    """Records kill()/wait() so the teardown path can be asserted; ``communicate`` is
    driven to raise the scenario under test."""

    def __init__(self, *, raise_exc: BaseException):
        self._raise_exc = raise_exc
        self.pid = 4321
        self.killed = False
        self.waited = False

    async def communicate(self, _input=None):
        raise self._raise_exc

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        self.waited = True
        return 0


_CFG = {"command": "python", "args": ["-c", "pass"], "env": {}, "cwd": None}


def _patch_spawn(monkeypatch, proc: _FakeProc) -> None:
    async def _fake_exec(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)


# -- (a) CancelledError during the self-check kills + reaps + re-raises --------------


def test_selfcheck_cancelled_kills_and_reaps_proc(monkeypatch) -> None:
    """A shutdown cancellation while handshaking must kill AND await the child, then
    re-raise the CancelledError (not swallow it)."""
    proc = _FakeProc(raise_exc=asyncio.CancelledError())
    _patch_spawn(monkeypatch, proc)

    async def run() -> None:
        with pytest.raises(asyncio.CancelledError):
            await mcp_selfcheck._check_one("akana_memory", _CFG)

    asyncio.run(run())
    assert proc.killed is True  # kill path was taken despite BaseException
    assert proc.waited is True  # child reaped (no orphan / dangling FDs)


# -- (b) the timeout branch awaits the killed child ---------------------------------


def test_selfcheck_timeout_reaps_proc(monkeypatch) -> None:
    """On handshake timeout the killed child must be awaited (proc.wait) so it is
    reaped instead of leaking as a zombie + open pipes."""
    proc = _FakeProc(raise_exc=TimeoutError())
    _patch_spawn(monkeypatch, proc)

    async def run() -> None:
        # returns cleanly (best-effort self-check never raises on timeout)
        await mcp_selfcheck._check_one("akana_vault", _CFG)

    asyncio.run(run())
    assert proc.killed is True
    assert proc.waited is True  # BUG fix: proc.wait() awaited after kill


# -- (c) the reaper consults the GROUP check, not just the leader --------------------


def test_reaper_uses_group_alive_not_just_pid(monkeypatch, tmp_path) -> None:
    """When the leader dies but a group child survives, the reaper must keep polling
    via _group_alive and then force-kill the GROUP — not return early on _pid_alive."""
    # Stale pid file for a group whose LEADER is already gone.
    pid_dir = llm_process.llm_pid_dir(tmp_path)
    (pid_dir / "tok.json").write_text(
        json.dumps({"token": "tok", "pid": 4321, "pgid": 4321, "kind": "cursor_bridge"}),
        encoding="utf-8",
    )

    group_alive_calls: list[int] = []
    kill_calls: list[tuple[int, bool]] = []

    # Leader detection: the initial "worth acting on" gate must see it alive so the
    # reaper enters the termination branch at all.
    monkeypatch.setattr(
        llm_process, "_pid_alive", lambda pid, **kw: True, raising=True
    )

    # The child in the group outlives the SIGTERM'd leader: _group_alive stays True the
    # whole time. Records every consult to prove the GROUP check (not just the leader)
    # is what drives the loop + the force-kill guard.
    def _fake_group_alive(pgid: int) -> bool:
        group_alive_calls.append(pgid)
        return True

    monkeypatch.setattr(llm_process, "_group_alive", _fake_group_alive, raising=True)
    monkeypatch.setattr(
        llm_process,
        "_killpg",
        lambda pgid, *, force: kill_calls.append((pgid, force)) or True,
        raising=True,
    )
    # Deterministic clock: cross the grace deadline after the first poll so the loop
    # runs exactly one _group_alive iteration, then the force-kill guard fires (no
    # wall-clock dependency / no real sleeping).
    _ticks = iter([0.0, 0.0, 100.0])  # deadline base, first check (< deadline), then expired

    def _fake_monotonic() -> float:
        try:
            return next(_ticks)
        except StopIteration:
            return 100.0

    monkeypatch.setattr(llm_process.time, "monotonic", _fake_monotonic, raising=True)
    monkeypatch.setattr(llm_process.time, "sleep", lambda _s: None, raising=True)

    findings = llm_process.reap_orphan_llm_processes(tmp_path)

    # The GROUP check was consulted (regression: previously only _pid_alive(pid) was).
    assert group_alive_calls, "_group_alive was never consulted by the reaper"
    assert all(p == 4321 for p in group_alive_calls)
    # Graceful SIGTERM then a forced SIGKILL of the GROUP (child outlived the leader).
    assert (4321, False) in kill_calls
    assert (4321, True) in kill_calls
    assert findings and findings[0]["reaped"] is True
