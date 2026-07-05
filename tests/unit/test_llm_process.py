"""LLM process registration + group-kill + orphan reaper (core of BUG 1).

Real-but-short subprocesses (``python -c``) are used: a parent process spawns a
child ``sleep``; thanks to ``start_new_session=True``, killpg takes down BOTH the
parent AND the child. The reaper cleans up stale pid files.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest

from akana_server.orchestrator import llm_process
from akana_server.orchestrator.llm_process import (
    executable_argv,
    llm_pid_dir,
    needs_cmd_wrapper,
    reap_orphan_llm_processes,
    register_llm_process,
    release_llm_process,
    resolve_executable,
    terminate_process_group,
)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# Parent: spawn a child sleep, write the child pid to stdout, then sleep itself.
_PARENT_SRC = (
    "import subprocess,sys,time;"
    "c=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
    "print(c.pid,flush=True);"
    "time.sleep(30)"
)


def _spawn_parent_with_child():
    """Spawn via plain ``subprocess`` (no asyncio loop → no transport-cleanup
    warning). Returns (popen, child_pid); both in the parent's session group
    thanks to ``start_new_session=True``. Caller must ``popen.wait()`` after
    the group is killed to reap the parent zombie."""
    import subprocess

    proc = subprocess.Popen(
        [sys.executable, "-c", _PARENT_SRC],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert proc.stdout
    line = proc.stdout.readline()
    child_pid = int(line.decode().strip())
    proc.stdout.close()
    return proc, child_pid


def _wait_dead(*pids: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(_pid_alive(p) for p in pids):
        time.sleep(0.05)


@pytest.mark.skipif(os.name != "posix", reason="killpg POSIX-only")
def test_terminate_process_group_kills_child_too() -> None:
    """killpg takes down the parent process group → the child sleep DIES too (no orphan)."""
    parent, child_pid = _spawn_parent_with_child()
    parent_pid = parent.pid
    assert _pid_alive(parent_pid)
    assert _pid_alive(child_pid)

    asyncio.run(terminate_process_group(parent_pid, grace=0.3))
    parent.wait(timeout=5.0)  # zombie reap

    _wait_dead(parent_pid, child_pid)
    assert not _pid_alive(parent_pid), "parent process is still alive"
    assert not _pid_alive(child_pid), "child process was orphaned (killpg did not take down the tree)"


@pytest.mark.skipif(os.name != "posix", reason="killpg POSIX-only")
def test_reaper_kills_live_stale_pid_and_cleans_files(tmp_path: Path) -> None:
    """Reaper: killpg's a live stale pid group + deletes the file; the child dies too."""
    parent, child_pid = _spawn_parent_with_child()
    parent_pid = parent.pid
    # Write a pid file as if it were left over from a previous session.
    register_llm_process(tmp_path, "stale-token", parent_pid, "cursor_bridge")
    assert (llm_pid_dir(tmp_path) / "stale-token.json").is_file()

    findings = reap_orphan_llm_processes(tmp_path)
    parent.wait(timeout=5.0)  # zombie reap

    reaped = [f for f in findings if f.get("reaped")]
    assert len(reaped) == 1
    assert reaped[0]["pid"] == parent_pid
    assert reaped[0]["kind"] == "cursor_bridge"
    # the pid file was cleaned up.
    assert not (llm_pid_dir(tmp_path) / "stale-token.json").is_file()
    # the tree went down.
    _wait_dead(parent_pid, child_pid)
    assert not _pid_alive(parent_pid)
    assert not _pid_alive(child_pid)


def test_reaper_removes_dead_pid_file(tmp_path: Path) -> None:
    """A dead pid (never alive) → the file is deleted, reaped=False."""
    # A pid that is very likely not in use.
    register_llm_process(tmp_path, "dead-token", 2_000_000_000, "claude_cli")
    findings = reap_orphan_llm_processes(tmp_path)
    assert len(findings) == 1
    assert findings[0]["alive"] is False
    assert findings[0]["reaped"] is False
    assert not (llm_pid_dir(tmp_path) / "dead-token.json").is_file()


def test_reaper_removes_corrupt_pid_file(tmp_path: Path) -> None:
    """A corrupt JSON pid file → silently cleaned up, does not break startup."""
    d = llm_pid_dir(tmp_path)
    (d / "garbage.json").write_text("{not json", encoding="utf-8")
    findings = reap_orphan_llm_processes(tmp_path)
    assert findings and findings[0]["token"] is None
    assert not (d / "garbage.json").is_file()


def test_release_removes_pid_file(tmp_path: Path) -> None:
    register_llm_process(tmp_path, "tok", 12345, "cursor_bridge")
    assert (llm_pid_dir(tmp_path) / "tok.json").is_file()
    release_llm_process(tmp_path, "tok")
    assert not (llm_pid_dir(tmp_path) / "tok.json").is_file()


def test_terminate_noop_on_dead_pid() -> None:
    """An already-dead/missing pid group → silent no-op (raises no error)."""
    asyncio.run(terminate_process_group(2_000_000_000, grace=0.1))


def test_register_pid_file_contents(tmp_path: Path) -> None:
    register_llm_process(tmp_path, "abc", 4242, "claude_cli")
    data = json.loads((llm_pid_dir(tmp_path) / "abc.json").read_text(encoding="utf-8"))
    assert data["pid"] == 4242
    assert data["pgid"] == 4242  # start_new_session ⇒ pgid == pid
    assert data["kind"] == "claude_cli"
    assert data["token"] == "abc"


# ── BUG 3: cross-platform executable resolution (Windows .cmd / PATHEXT) ──────


def _force_windows(monkeypatch: pytest.MonkeyPatch, which_map: dict[str, str]) -> None:
    """Simulate Windows: ``_IS_WIN`` True and ``shutil.which`` honoring PATHEXT
    (returns a ``.cmd``/``.exe`` path for a bare name), so the resolution helpers can
    be exercised on a POSIX CI runner."""
    monkeypatch.setattr(llm_process, "_IS_WIN", True)
    monkeypatch.setattr(
        llm_process.shutil, "which", lambda name: which_map.get(name)
    )


def test_resolve_executable_keeps_explicit_path() -> None:
    """An explicit path (has a separator) is returned unchanged — no PATH lookup."""
    p = os.path.join("usr", "local", "bin", "claude")
    assert resolve_executable(p) == p


def test_resolve_executable_bare_name_uses_which(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_process.shutil, "which", lambda name: "/usr/bin/node")
    assert resolve_executable("node") == "/usr/bin/node"


def test_resolve_executable_falls_back_to_name_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not found on PATH → original name (callers keep their FileNotFoundError + hint)."""
    monkeypatch.setattr(llm_process.shutil, "which", lambda name: None)
    assert resolve_executable("claude") == "claude"


@pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX-only: on Windows needs_cmd_wrapper('claude') is True for the claude.cmd shim",
)
def test_needs_cmd_wrapper_false_on_posix() -> None:
    assert needs_cmd_wrapper("claude") is False  # real OS is POSIX here


def test_needs_cmd_wrapper_true_for_windows_cmd_shim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_windows(monkeypatch, {"claude": r"C:\npm\claude.cmd"})
    assert needs_cmd_wrapper("claude") is True


def test_needs_cmd_wrapper_false_for_windows_exe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``node`` resolves to ``node.exe`` (CreateProcess handles ``.exe``) → no wrapper."""
    _force_windows(monkeypatch, {"node": r"C:\Program Files\nodejs\node.exe"})
    assert needs_cmd_wrapper("node") is False


def test_executable_argv_wraps_windows_cmd_shim(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_windows(monkeypatch, {"claude": r"C:\npm\claude.cmd"})
    out = executable_argv(["claude", "-p", "--model", "x"])
    assert out == ["cmd", "/c", r"C:\npm\claude.cmd", "-p", "--model", "x"]


def test_executable_argv_absolutises_windows_exe(monkeypatch: pytest.MonkeyPatch) -> None:
    _force_windows(monkeypatch, {"node": r"C:\nodejs\node.exe"})
    out = executable_argv(["node", "script.mjs"])
    assert out == [r"C:\nodejs\node.exe", "script.mjs"]  # no cmd /c


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX argv passthrough; on real Windows executable_argv absolutises + wraps via cmd /c",
)
def test_executable_argv_noop_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    """POSIX: argv is returned UNCHANGED (create_subprocess_exec resolves PATH itself);
    never absolutised, never wrapped with cmd /c — so the working POSIX path is stable."""
    # which is never consulted on POSIX; the result must be byte-for-byte the input.
    monkeypatch.setattr(llm_process.shutil, "which", lambda name: "/usr/bin/node")
    assert executable_argv(["node", "x"]) == ["node", "x"]
