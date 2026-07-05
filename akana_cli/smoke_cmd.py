"""Core smoke: doctor + fast API tests."""

from __future__ import annotations

import os

from akana_cli import i18n, io
from akana_cli.doctor import run_doctor
from akana_cli.paths import REPO_ROOT, venv_exists, venv_python
from akana_cli.runner import run


def run_smoke() -> int:
    io.banner(i18n.t("smoke.banner"))
    code = run_doctor(verbose=True)
    if code != 0:
        io.fail(i18n.t("smoke.doctor_failed"))
        return code
    if not venv_exists():
        io.fail(i18n.t("smoke.venv_missing"))
        return 1
    py = venv_python()
    env = os.environ.copy()
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    io.step(i18n.t("smoke.running"))
    # Autoload off (ROS/ament safety, see pytest.ini); force-load pytest-asyncio so
    # async tests still run under the opt-out.
    cp = run(
        [
            str(py),
            "-m",
            "pytest",
            "-p",
            "pytest_asyncio.plugin",
            "tests/integration/test_core_smoke.py",
            "tests/unit/test_health.py",
            "tests/unit/test_akana_cli.py",
            "tests/integration/test_conversations_persist.py::test_create_and_list_conversations",
            "-q",
            "--tb=short",
        ],
        cwd=REPO_ROOT,
        env=env,
        check=False,
    )
    if cp.returncode != 0:
        io.fail(i18n.t("smoke.failed"))
        return cp.returncode
    io.ok(i18n.t("smoke.passed"))
    return 0
