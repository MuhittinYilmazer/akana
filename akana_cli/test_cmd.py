"""Run pytest."""

from __future__ import annotations

import os

from akana_cli import io
from akana_cli.paths import REPO_ROOT, venv_exists, venv_python
from akana_cli.runner import run


def run_test() -> int:
    if not venv_exists():
        io.fail("venv missing — first run: python akana.py setup")
        return 1
    py = venv_python()
    env = os.environ.copy()
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    io.step("pytest")
    # Autoload stays off so stray ROS/ament global plugins can't break the isolated
    # venv (see pytest.ini), but the suite's async tests need pytest-asyncio — load it
    # explicitly so it survives the autoload opt-out.
    cp = run(
        [str(py), "-m", "pytest", "-p", "pytest_asyncio.plugin", "tests/", "-q"],
        cwd=REPO_ROOT,
        env=env,
        check=False,
    )
    return cp.returncode
