"""Stop Akana server listening on configured port."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time

from akana_cli import i18n, io
from akana_cli.env_util import server_host_port


def find_pids_on_port(port: int, host: str = "127.0.0.1") -> list[int]:
    """Return PIDs listening on TCP *port* (best-effort, cross-platform)."""
    if sys.platform == "win32":
        return _pids_windows(port)
    pids = _pids_unix_ss(port)
    if pids:
        return pids
    return _pids_unix_lsof(port, host)


def _pids_windows(port: int) -> list[int]:
    try:
        out = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    pids: set[int] = set()
    for line in out.stdout.splitlines():
        # TCP listening rows: "Proto  Local  Foreign  State  PID" (5 columns). Parse the
        # columns and match the Local-Address PORT EXACTLY — a bare ``:{port}`` substring
        # match killed the WRONG process tree (e.g. port 80 matched ":8080").
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        # The State column ("LISTENING") is localized by netstat.exe on non-English
        # Windows display languages (e.g. German "ABHOEREN") — matching it as a literal
        # would silently find nothing there. A listening TCP socket's Foreign Address is
        # always the locale-independent wildcard "0.0.0.0:0" / "[::]:0" / "*:*", so use
        # that instead of the State text.
        foreign = parts[2]
        if not (foreign.endswith(":0") or foreign.endswith(":*")):
            continue
        # Local address is host:port — IPv4 ``1.2.3.4:8766`` or IPv6 ``[::]:8766``.
        _, sep, port_str = parts[1].rpartition(":")
        if not sep:
            continue
        try:
            if int(port_str) != port:
                continue
            pids.add(int(parts[4]))
        except ValueError:
            continue
    return sorted(pids)


def _pids_unix_ss(port: int) -> list[int]:
    if not _which("ss"):
        return []
    try:
        out = subprocess.run(
            ["ss", "-lptn", f"sport = :{port}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    pids: set[int] = set()
    for match in re.finditer(r"pid=(\d+)", out.stdout):
        pids.add(int(match.group(1)))
    return sorted(pids)


def _pids_unix_lsof(port: int, host: str) -> list[int]:
    if not _which("lsof"):
        return []
    specs = [f":{port}", f"TCP:{port}", f"{host}:{port}"]
    pids: set[int] = set()
    for spec in specs:
        try:
            out = subprocess.run(
                ["lsof", "-ti", spec],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if out.returncode != 0:
            continue
        for line in out.stdout.splitlines():
            line = line.strip()
            if line.isdigit():
                pids.add(int(line))
    return sorted(pids)


def _which(name: str) -> str | None:
    from shutil import which

    return which(name)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        # Windows (audit C3): os.kill(pid, 0) sends signal 0 to the Ctrl+C group and
        # blows up → do NOT use it for liveness. Query the pid via tasklist; if not
        # found, it is dead.
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
            return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def terminate_pid(pid: int, *, grace_seconds: float = 5.0) -> bool:
    """Send SIGTERM, wait, then SIGKILL if needed. Returns True if process exited."""
    if not _pid_alive(pid):
        return True
    # WINDOWS (audit C3): there is no graceful group-signal; os.kill(SIGTERM) is
    # already TerminateProcess (not graceful) and the old liveness check was broken.
    # Take down the whole pid TREE (children included) directly with taskkill /T /F,
    # then verify.
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                return True
            time.sleep(0.2)
        return not _pid_alive(pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return not _pid_alive(pid)

    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.2)

    if not _pid_alive(pid):
        return True
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    time.sleep(0.3)
    return not _pid_alive(pid)


def run_stop() -> int:
    host, port = server_host_port()
    io.step(i18n.t("stop.looking", host=host, port=port))
    pids = find_pids_on_port(port, host)
    if not pids:
        io.ok(i18n.t("stop.port_free", port=port))
        print("  " + i18n.t("stop.not_found_note"))
        return 0

    stopped = 0
    for pid in pids:
        io.step(i18n.t("stop.stopping", pid=pid))
        if terminate_pid(pid):
            io.ok(i18n.t("stop.stopped_pid", pid=pid))
            stopped += 1
        else:
            io.warn(i18n.t("stop.stop_failed_pid", pid=pid))

    if stopped:
        print()
        io.ok(i18n.t("stop.stopped"))
        return 0
    return 1
