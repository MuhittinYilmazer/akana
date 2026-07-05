"""Subprocess helpers."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path


def run(
    cmd: Sequence[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        env=env,
        text=True,
        check=check,
    )


def run_quiet(
    cmd: Sequence[str], *, cwd: Path | None = None, env: dict[str, str] | None = None
) -> bool:
    try:
        subprocess.run(
            list(cmd),
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        return True
    except (OSError, subprocess.CalledProcessError):
        return False


def npm_base() -> list[str]:
    """Cross-platform base argv for npm.

    On Windows npm ships as ``npm.cmd`` (a batch file); ``subprocess``/CreateProcess
    cannot launch it from the bare name ``npm`` (and Python 3.12+ refuses to run
    .cmd/.bat without a shell), so route through ``cmd.exe``. On Linux/macOS npm is a
    normal executable, so the bare name works.
    """
    import os

    return ["cmd", "/c", "npm"] if os.name == "nt" else ["npm"]


def run_progress(
    cmd: Sequence[str],
    label: str,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    substatus: "Callable[[str], str | None] | None" = None,
) -> tuple[bool, str]:
    """Run a command showing live progress; capture output and return (ok, output).

    On a TTY an animated spinner with elapsed seconds gives "it's working" feedback
    even while an install downloads in the background; otherwise (CI / piped) a single
    line is printed. Output is captured and returned so the caller can show it only on
    failure. Never raises for process errors — returns ok=False instead.

    ``substatus`` makes the wait TRANSPARENT: it is called with each output line and may
    return a short, human phrase (e.g. "downloading numpy") that is shown live next to
    the spinner — so the user sees WHAT is happening, not just that time is passing. On a
    non-TTY the distinct sub-status phrases are printed as they change (still bounded —
    only when the phrase changes, never a per-line flood).
    """
    import sys
    import threading
    import time

    try:
        proc = subprocess.Popen(
            list(cmd),
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            # Decode captured pip/npm output as UTF-8 (what modern tools emit), not the
            # Windows locale code page (cp1252/cp1254) that text=True would otherwise use
            # — errors="replace" guarantees no UnicodeDecodeError on a multibyte console.
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        return False, str(exc)

    buf: list[str] = []
    state = {"sub": ""}  # latest live sub-status phrase (mutated by the drain thread)

    is_tty = bool(getattr(sys.stdout, "isatty", lambda: False)())

    def _drain() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            buf.append(line)
            if substatus is None:
                continue
            try:
                phrase = substatus(line)
            except Exception:  # noqa: BLE001 - a parser bug must never break the install
                phrase = None
            if phrase and phrase != state["sub"]:
                state["sub"] = phrase
                # Non-TTY: echo each NEW phrase on its own line (bounded — on change only).
                if not is_tty:
                    print(f"      {phrase}")

    drainer = threading.Thread(target=_drain, daemon=True)
    drainer.start()

    if not is_tty:
        print(f"  · {label}…")
    frames = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    i = 0
    start = time.time()
    while proc.poll() is None:
        if is_tty:
            elapsed = int(time.time() - start)
            sub = state["sub"]
            tail = f" · {sub}" if sub else ""
            line = f"\r  {frames[i % len(frames)]} {label}{tail} … {elapsed}s "
            # Pad to clear a previously-longer sub-status, then trim to keep it on one row.
            sys.stdout.write(line.ljust(78)[:110])
            sys.stdout.flush()
            i += 1
        time.sleep(0.12)
    drainer.join(timeout=2)
    if is_tty:
        sys.stdout.write("\r" + " " * 110 + "\r")
        sys.stdout.flush()
    return proc.returncode == 0, "".join(buf)
