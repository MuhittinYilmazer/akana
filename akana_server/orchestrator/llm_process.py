"""LLM process registration + group-kill helpers (shared orchestrator component).

Common requirement for two live bugs: fully kill the background LLM processes
(Cursor bridge daemon + claude CLI) and ALL their child processes on server
shutdown or during stale-session reaping.

Design (pid-file registry + group-kill via ``killpg``):

* Every LLM process is the leader of its own process group via
  ``start_new_session=True`` (pgid == pid), so a single ``os.killpg`` call
  takes down **the entire tree**; a plain ``proc.kill()`` would only kill the
  directly spawned process, orphaning the real cursor/node bridge or claude
  child processes.
* At startup each process writes a ``<data_dir>/run/llm/<token>.json`` pid
  file, which is deleted on clean shutdown.
* At bootstrap :func:`reap_orphan_llm_processes` scans the directory; it
  killpg's any live stale pids (the lifespan finally may never have run after
  a SIGKILL crash) and removes dead/corrupt records.

``terminate_process_group`` applies the SIGTERM → short wait → SIGKILL cascade
(gives the daemon a chance to shut down cleanly without leaving descendants).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from akana_server.orchestrator.errors import LLMCallError

# WINDOWS process management (audit C2). os.killpg / signal.SIGKILL / os.kill(pid,0)
# do NOT exist / work on Windows → AttributeError/error. _IS_WIN branches use
# taskkill+tasklist instead. _SIG_KILL: Windows has no SIGKILL so it falls back to
# SIGTERM (taskkill /F already forces termination; the distinction is unnecessary).
_IS_WIN = sys.platform == "win32"
_SIG_KILL = getattr(signal, "SIGKILL", signal.SIGTERM)

log = logging.getLogger(__name__)

#: Seconds to wait after SIGTERM before sending SIGKILL.
_TERM_GRACE_SECONDS = 1.5

# Windows batch shims that CreateProcess cannot exec directly (need cmd.exe).
_WIN_SHIM_SUFFIXES = (".cmd", ".bat")


def resolve_executable(name: str) -> str:
    """Resolve a bare command name to a concrete launchable path (cross-platform).

    ``asyncio.create_subprocess_exec`` / ``subprocess`` use ``CreateProcess`` on
    Windows (no shell). CreateProcess only appends ``.exe`` when searching PATH — it
    does NOT honor ``PATHEXT``, so a bare ``"claude"`` fails with FileNotFoundError
    even though the installed file is ``claude.cmd`` (``where claude`` / ``shutil.which``
    DO find it via PATHEXT). This is the root of the runtime "claude CLI not found"
    while ``doctor`` (which uses ``shutil.which``) reports it present.

    Returns the concrete resolved path, or the original ``name`` unchanged when it is
    already an explicit path or cannot be found (callers keep their existing
    FileNotFoundError handling + install hint). On POSIX this is effectively a no-op
    that just absolutises the PATH lookup.
    """
    if not name:
        return name
    # An explicit path (absolute or containing a separator) is launched as given.
    if os.path.isabs(name) or os.sep in name or (os.altsep and os.altsep in name):
        return name
    return shutil.which(name) or name


def needs_cmd_wrapper(name: str) -> bool:
    """Whether launching ``name`` requires the Windows ``cmd.exe`` wrapper.

    True only for a Windows ``.cmd``/``.bat`` shim (e.g. npm-installed ``claude.cmd``):
    CreateProcess cannot exec a batch file directly, so it must run via ``cmd /c``.
    When True the caller MUST keep arbitrary/user text OFF the argv — ``cmd.exe``
    re-parses its command line (``%VAR%`` expansion, ``&|<>^`` metacharacters, quote
    toggling), corrupting content and enabling command injection (BatBadBut). Deliver
    such content via stdin or temp files instead.
    """
    if not _IS_WIN:
        return False
    return resolve_executable(name).lower().endswith(_WIN_SHIM_SUFFIXES)


def node_missing_error() -> "LLMCallError":
    """A 503 for the Cursor bridge's ``node`` spawn raising FileNotFoundError.

    When Node.js is not on PATH the OS raises a bare ``[WinError 2]`` / ENOENT that
    says nothing about the missing dependency. This names it — mirrors
    ``claude_provider``'s "claude CLI not found" branch so both providers surface
    a missing runtime as an actionable message. Kept here so the three node spawn
    sites (bridge daemon, one-shot, stream) stay in sync.
    """
    from akana_server.orchestrator.errors import LLMCallError

    return LLMCallError(
        "Node.js not found — the Cursor bridge requires Node.js 18+ "
        "(install it from https://nodejs.org/ and add it to PATH)",
        status_code=503,
    )


def executable_argv(argv: list[str]) -> list[str]:
    """Make a logical argv launchable on Windows; a NO-OP on POSIX.

    POSIX ``create_subprocess_exec`` already resolves bare names via ``PATH``, so the
    argv is returned untouched (the working Linux/macOS path never changes). On Windows
    ``argv[0]`` is resolved via :func:`resolve_executable` (PATHEXT-aware); a
    ``.cmd``/``.bat`` shim is then wrapped with ``cmd /c`` (mirrors the blessed
    ``akana_cli.runner.npm_base`` pattern), while an ``.exe`` is launched directly from
    its resolved path. The bridge daemon (``node``) resolves to ``node.exe`` → no wrapper.
    """
    if not argv or not _IS_WIN:
        return argv
    resolved = resolve_executable(argv[0])
    rest = argv[1:]
    if resolved.lower().endswith(_WIN_SHIM_SUFFIXES):
        return ["cmd", "/c", resolved, *rest]
    return [resolved, *rest]


def _pid_alive(pid: int, *, default_on_error: bool = True) -> bool:
    """Is the given pid alive? ``default_on_error``: value returned when the Windows
    ``tasklist`` query cannot be resolved. Made parametric because the two callers
    want OPPOSITE answers on ambiguity (R4-D #2): the grace-wait loop wants "assume
    alive" (True → keep waiting); the REAPER wants "assume dead" (False) — otherwise
    a quickly recycled stale pid on Windows could cause ``taskkill /T /F`` to kill
    an UNRELATED process.
    """
    if pid <= 0:
        return False
    if _IS_WIN:
        # Windows: os.kill(pid, 0) (signal 0) is NOT SUPPORTED → raises an error.
        # Query the pid with tasklist; if not found the process is dead. Fall back
        # to the caller's preference if the query fails.
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            return str(pid) in (r.stdout or "")
        except (OSError, ValueError, subprocess.SubprocessError):
            return default_on_error
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - alive but not ours
        return True
    return True


def _group_alive(pgid: int) -> bool:
    """Is there any live member in the process GROUP? (BUG 1) ``_pid_alive`` only
    checks the LEADER pid; if the leader dies on SIGTERM but a child in the group
    is still alive, the grace loop returns early and the SIGKILL cascade never runs
    → orphan child. On POSIX ``os.killpg(pgid, 0)`` raises ``ProcessLookupError``
    when NO member remains in the group; the group is considered alive as long as any
    child lives. ``PermissionError`` = group exists but is not ours → assume alive.
    No killpg on Windows → fall back to checking the leader.
    """
    if pgid <= 0:
        return False
    if _IS_WIN:  # os.killpg not available; on Windows pgid == pid → leader check suffices
        return _pid_alive(pgid)
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - group exists but is not ours
        return True
    return True


def llm_pid_dir(data_dir: Path) -> Path:
    """``<data_dir>/run/llm`` — created if it does not exist."""
    d = (Path(data_dir) / "run" / "llm").resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def register_llm_process(
    data_dir: Path, token: str, pid: int, kind: str
) -> Path | None:
    """Write a ``<data_dir>/run/llm/<token>.json`` pid file (best-effort).

    ``kind`` is for diagnostics only ("cursor_bridge" | "claude_cli"). Returns
    ``None`` and does not disrupt the call flow if the write fails.
    """
    try:
        d = llm_pid_dir(data_dir)
    except OSError:  # pragma: no cover - pid directory cannot be opened
        log.warning("llm_process: pid directory unavailable (kind=%s)", kind)
        return None
    record = {
        "token": str(token),
        "pid": int(pid),
        "pgid": int(pid),  # start_new_session=True ⇒ pgid == pid
        "kind": str(kind),
        "started_at": time.time(),
    }
    path = d / f"{Path(str(token)).name}.json"
    try:
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    except OSError:  # pragma: no cover - registration failure must not drop the run
        log.warning("llm_process: pid file could not be written (token=%s)", token)
        return None
    return path


def release_llm_process(data_dir: Path, token: str) -> None:
    """Delete the pid file (process shut down cleanly)."""
    try:
        (llm_pid_dir(data_dir) / f"{Path(str(token)).name}.json").unlink(
            missing_ok=True
        )
    except OSError:  # pragma: no cover - cleanup failure must not drop the run
        log.debug("llm_process: pid file could not be deleted (token=%s)", token)


def _killpg(pgid: int, *, force: bool) -> bool:
    """Terminate a process GROUP. ``force=False`` is graceful (POSIX SIGTERM /
    Windows ``taskkill /T``); ``force=True`` is forceful (POSIX SIGKILL / Windows
    ``taskkill /T /F``). Returns True if the signal/command could be sent.

    NOTE (Windows): the ``/F`` decision is tied to ``force`` — previously it was
    ``sig == _SIG_KILL`` but since ``SIGKILL`` does not exist on Windows
    ``_SIG_KILL`` falls back to ``SIGTERM`` → even a GRACEFUL call was equal to
    ``_SIG_KILL`` and received ``/F`` → 1.5s grace window bypassed (every
    termination was an immediate force-kill). The ``force`` flag removes this
    ambiguity.
    """
    if _IS_WIN:
        # os.killpg not available (AttributeError). taskkill /T takes down the
        # process TREE (start_new_session is ignored on Windows → pgid == pid;
        # /T captures the whole tree).
        cmd = ["taskkill", "/PID", str(pgid), "/T"]
        if force:
            cmd.append("/F")
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=10, check=False)
            return r.returncode == 0
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
    try:
        os.killpg(pgid, _SIG_KILL if force else signal.SIGTERM)
        return True
    except (ProcessLookupError, PermissionError, OSError, AttributeError):
        return False


async def terminate_process_group(pid: int, *, grace: float = _TERM_GRACE_SECONDS) -> None:
    """SIGTERM → short wait → SIGKILL for the whole tree, assuming pgid == pid.

    Passes silently if the process is already dead. The caller must have spawned
    with ``start_new_session=True``; otherwise the pgid does not cover the
    process group.

    Windows note: ``_killpg``/``_group_alive`` shell out to ``taskkill``/``tasklist``
    via blocking ``subprocess.run``. This coroutine is awaited from hot cancellation
    paths (STOP, idle-timeout, ask_user early-terminate) while OTHER conversations'
    SSE streams are running on the same event loop, so every blocking call is pushed
    to a worker thread via ``asyncio.to_thread`` — otherwise a single termination
    would freeze the whole loop (all concurrent streams) for the poll's duration.
    """
    if pid <= 0:
        return
    if not await asyncio.to_thread(_killpg, pid, force=False) and not _IS_WIN:
        return  # POSIX: ESRCH = group gone / already dead. (Windows: graceful
        # taskkill may return non-zero even when the process is still alive →
        # continue to the force phase.)
    # Give the process a chance to shut down cleanly: short poll loop (no busy-spin).
    deadline = time.monotonic() + max(0.0, grace)
    while time.monotonic() < deadline:
        # BUG 1: poll GROUP liveness (not just the leader). If the leader dies
        # but a child survives, do not return early — proceed to SIGKILL cascade
        # to avoid leaving orphans.
        if not await asyncio.to_thread(_group_alive, pid):
            return
        await asyncio.sleep(0.05)
    await asyncio.to_thread(_killpg, pid, force=True)


def reap_orphan_llm_processes(data_dir: Path) -> list[dict[str, Any]]:
    """Scan + killpg LLM pids left over from a previous session (called at bootstrap).

    After a SIGKILL crash the lifespan finally never runs; surviving bridge daemon /
    claude CLI processes are left as orphans. This reaper is the permanent fix:
    it takes down live stale process groups via SIGTERM→SIGKILL and removes
    dead/corrupt records. Each returned entry is diagnostic:
    ``token, pid, kind, alive, reaped``.
    """
    findings: list[dict[str, Any]] = []
    try:
        d = llm_pid_dir(data_dir)
    except OSError:  # pragma: no cover
        return findings
    for path in sorted(d.glob("*.json")):
        record: dict[str, Any] | None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            record = None
        if not isinstance(record, dict) or not isinstance(record.get("pid"), int):
            path.unlink(missing_ok=True)
            findings.append(
                {"token": None, "pid": None, "kind": None, "alive": False, "reaped": False}
            )
            continue
        pid = int(record["pid"])
        pgid = int(record.get("pgid") or pid)
        # REAPER: on ambiguity (tasklist error) assume dead → avoid accidentally killing
        # a recycled stale pid (R4-D #2). Linux os.kill(pid,0) is already definitive.
        alive = _pid_alive(pid, default_on_error=False)
        reaped = False
        if alive:
            # Graceful termination, short wait, force if needed — synchronous bootstrap.
            _killpg(pgid, force=False)
            deadline = time.monotonic() + _TERM_GRACE_SECONDS
            # BUG 1: poll GROUP liveness (not just the leader), matching
            # terminate_process_group. If the leader (node bridge / claude CLI)
            # dies on SIGTERM but a child in the group survives, _pid_alive(pid)
            # returns False and the SIGKILL cascade never fires → orphan leaked.
            while time.monotonic() < deadline and _group_alive(pgid):
                time.sleep(0.05)
            if _group_alive(pgid):
                _killpg(pgid, force=True)
            reaped = True
            log.warning(
                "llm_process: orphan process reaped (kind=%s pid=%s)",
                record.get("kind"), pid,
            )
        path.unlink(missing_ok=True)
        findings.append(
            {
                "token": record.get("token"),
                "pid": pid,
                "kind": record.get("kind"),
                "alive": alive,
                "reaped": reaped,
            }
        )
    return findings


__all__ = [
    "llm_pid_dir",
    "register_llm_process",
    "release_llm_process",
    "reap_orphan_llm_processes",
    "terminate_process_group",
    "resolve_executable",
    "needs_cmd_wrapper",
    "executable_argv",
    "node_missing_error",
]
