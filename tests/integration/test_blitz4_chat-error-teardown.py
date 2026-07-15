"""Bug-blitz 4 — chat-error-teardown regression (node-vm harness wrapper).

Runs the backend-free node harness that loads the REAL akana-chat-transport.js and
akana-chat-threads.js in a VM with a fake DOM and drives the disconnect / cancel /
clean-close teardown paths. Each finding is proved RED (a synthetic revert exhibits the
bug) → GREEN (the shipped source cures it). See tests/web/blitz4_chat-error-teardown.harness.mjs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_node_harness(harness: Path) -> None:
    # timeout: don't let a hung harness (dangling timer etc.) keep the suite waiting
    # forever. Harnesses exit with process.exit(0) on success.
    try:
        proc = subprocess.run(
            ["node", str(harness)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - defensive
        raise AssertionError(
            f"node harness did not finish within 60s (likely a dangling timer): {harness.name}"
        ) from exc
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_blitz4_chat_error_teardown_harness() -> None:
    _run_node_harness(REPO_ROOT / "tests/web/blitz4_chat-error-teardown.harness.mjs")
