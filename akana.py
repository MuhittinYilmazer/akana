#!/usr/bin/env python3
"""Akana — cross-platform launcher (setup, start, doctor, test, and more).

Usage:
  python akana.py setup          Interactive install
  python akana.py add            Install an optional component later
  python akana.py start          Run server
  python akana.py stop           Stop the server
  python akana.py doctor         Pre-flight checks
  python akana.py smoke          Core smoke (doctor + pytest)
  python akana.py test           Run pytest
  python akana.py ship           Pack a portable tarball
  python akana.py reset-memory   Delete Inbox/staging/semantic/graph caches
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent


def _maybe_reexec_in_venv() -> None:
    """Switch to the repo venv even when `python3 akana.py ...` runs under the system python.

    All dependencies (fastapi, fastembed, faster-whisper) live in ONE place — the
    venv. This avoids the "present in venv but missing from system python" problem
    (e.g. fastembed → vector recall silently disabled). An env marker prevents an
    infinite loop; with no venv, or when already inside it, execution continues
    as-is; on an exec error it silently falls back to the current python (never
    breaks startup).
    """
    if os.environ.get("AKANA_VENV_REEXEC") == "1":
        return
    bindir, exe = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    venv_python = _ROOT / "venv" / bindir / exe
    if not venv_python.is_file():
        return
    try:
        if Path(sys.executable).resolve() == venv_python.resolve():
            return  # already on the venv python
        os.environ["AKANA_VENV_REEXEC"] = "1"
        argv = [str(venv_python), str(Path(__file__).resolve()), *sys.argv[1:]]
        if os.name == "nt":
            # os.execv does NOT replace the process on Windows — the launching shell
            # would return while a detached child runs, so Ctrl+C and the "press
            # Ctrl+C to stop" hint (long-running `start`) become misleading. Run the
            # venv python as a child, wait, and mirror its exit code instead.
            import subprocess

            try:
                raise SystemExit(subprocess.run(argv).returncode)
            except KeyboardInterrupt:
                # Ctrl+C's CTRL_C_EVENT reaches this whole console group too: the
                # child (main.py) already prints its own clean cancellation line and
                # exits 130, but this parent still gets a pending KeyboardInterrupt
                # once subprocess.run() returns. Mirror the same clean one-liner +
                # exit code instead of leaking a raw traceback here.
                raise SystemExit(130)
        os.execv(str(venv_python), argv)
    except OSError:
        os.environ.pop("AKANA_VENV_REEXEC", None)  # exec did not happen → continue with current python


_maybe_reexec_in_venv()

# Allow running before deps are installed (clone-and-run; no editable install).
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# THE single mechanism that makes `import akana` resolve to src/akana instead of
# THIS launcher file (which shadows the package name). All entry points route
# through _akana_src_bootstrap; no module does its own sys.path surgery anymore.
from _akana_src_bootstrap import ensure_src_on_path  # noqa: E402

ensure_src_on_path()

from akana_cli.main import main  # noqa: E402 — must come AFTER re-exec + sys.path setup

if __name__ == "__main__":
    raise SystemExit(main())
