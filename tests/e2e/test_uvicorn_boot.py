"""Real uvicorn subprocess smoke — boots + /health + server.log written.

The in-process TestClient runs the lifespan but doesn't answer "does the
import chain/uvicorn config actually come up?"; this single test covers that.
Timeout-bounded, must kill the process group — leaves no hung process in CI.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOOT_TIMEOUT = 30.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="real-uvicorn e2e uses POSIX process-group teardown (start_new_session/signal/os.killpg)",
)
def test_uvicorn_boots_health_and_server_log(tmp_path: Path) -> None:
    port = _free_port()
    env = dict(os.environ)
    env.update(
        {
            "AKANA_DATA_DIR": str(tmp_path),
            "AKANA_TOKEN": "",
            "AKANA_PORT": str(port),
            "CURSOR_API_KEY": "",
            "AKANA_MEMORY_LLM_CAPTURE": "0",
            "AKANA_SESSION_CLOSER_ENABLED": "0",
            "AKANA_TELEGRAM_ENABLED": "0",
        }
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "akana_server.api.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,  # process group — killpg on cleanup
    )
    try:
        health: dict | None = None
        deadline = time.monotonic() + BOOT_TIMEOUT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                stderr = (proc.stderr.read() or b"").decode("utf-8", "replace")
                pytest.fail(f"uvicorn died during boot (rc={proc.returncode}):\n{stderr[-2000:]}")
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.0)
                if r.status_code == 200:
                    health = r.json()
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        assert health is not None, f"/health did not return 200 within {BOOT_TIMEOUT}s"
        assert health["status"] == "ok"
        assert health["service"] == "akana"

        log_path = tmp_path / "logs" / "server.log"
        assert log_path.is_file(), "lifespan did not create server.log"
        assert "akana server log started" in log_path.read_text(encoding="utf-8")
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=5)
        if proc.stderr is not None:
            proc.stderr.close()
