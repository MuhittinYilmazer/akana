"""Repository and venv paths (Windows + Linux)."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = REPO_ROOT / "venv"
ENV_FILE = REPO_ROOT / ".env"
ENV_EXAMPLE = REPO_ROOT / ".env.example"
BRIDGE_DIR = REPO_ROOT / "cursor_bridge"

# Newest first, to match install.ps1/install.sh (so `doctor` and the venv build
# resolve the same interpreter the bootstrap chose). _resolved_executable enforces
# the >= 3.11 floor; there is no version ceiling, so a future 3.15 keeps working.
PYTHON_CANDIDATES = (
    "python3.14",
    "python3.13",
    "python3.12",
    "python3.11",
    "python3",
    "python",
)


def venv_python() -> Path:
    if sys.platform == "win32":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def venv_exists() -> bool:
    py = venv_python()
    return py.is_file()


def _resolved_executable(argv: list[str]) -> str | None:
    """Run `argv -c ...` and, if it resolves to Python >= 3.11, return its
    real executable path (sys.executable) — a single, directly-runnable path
    regardless of whether `argv` was a plain name or a `py -3.x` launcher call."""
    try:
        import subprocess

        out = subprocess.run(
            [*argv, "-c", "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}|{sys.executable}')"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if out.returncode != 0:
            return None
        version, _, exe = out.stdout.strip().partition("|")
        major, minor = (int(x) for x in version.split(".")[:2])
        if (major, minor) >= (3, 11) and exe:
            return exe
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return None


def find_system_python() -> str | None:
    # Prefer the interpreter already running this process — it's guaranteed to
    # satisfy the version check (main.py won't run under < 3.11) and is exactly
    # what install.ps1/install.sh just resolved (via `py -3.x` or `python3.x`)
    # to launch the wizard, so the venv ends up built with that same interpreter.
    if sys.executable and sys.version_info[:2] >= (3, 11):
        return sys.executable

    for name in PYTHON_CANDIDATES:
        path = shutil.which(name)
        if not path:
            continue
        found = _resolved_executable([path])
        if found:
            return found

    # Windows: versioned python3.x.exe binaries aren't a real installer
    # convention (only python.exe/py.exe are), so the `py` launcher — which
    # install.ps1 itself prefers — is the reliable way to find a versioned
    # install that isn't first on PATH.
    if sys.platform == "win32" and shutil.which("py"):
        # `py -3` (and bare `py`) select the newest installed Python, so a machine
        # with only 3.14+ — where no versioned tag below matches — is still found.
        for tag in ("-3.14", "-3.13", "-3.12", "-3.11", "-3"):
            found = _resolved_executable(["py", tag])
            if found:
                return found
        found = _resolved_executable(["py"])
        if found:
            return found

    return None


def expand_user_path(raw: str) -> Path:
    return Path(os.path.expanduser(raw)).resolve()


def default_data_dir() -> Path:
    # The old AKANA_CURSOR_DATA_DIR may still be set in the shell/environment → fallback.
    raw = os.environ.get("AKANA_DATA_DIR") or os.environ.get("AKANA_CURSOR_DATA_DIR") or "~/.akana"
    return expand_user_path(raw)
