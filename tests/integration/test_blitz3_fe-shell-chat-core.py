"""blitz3 — fe-shell-ui + fe-chat-core regression contracts (node-vm harness wrapper).

Runs the real static-module contract harness (tests/web/blitz3_fe-shell-chat-core.harness.mjs)
which loads akana-turn-status.js / akana-mobile-nav.js / aurora-ui.js / akana-shell.js /
akana-chat-threads.js / akana-chat.js in a bare node:vm with a fake DOM and asserts the
behaviour contracts for the 12 verified bugs fixed in this batch. The harness prints a
summary line and exits non-zero on any failed contract.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_node_harness(harness: Path) -> None:
    # timeout: don't let a hung harness (dangling timer etc.) keep the suite waiting forever.
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


def test_blitz3_fe_shell_chat_core_harness() -> None:
    """fe-shell-ui-1..6 + fe-chat-core-1..6 behaviour contracts (node-vm, fake DOM)."""
    _run_node_harness(REPO_ROOT / "tests/web/blitz3_fe-shell-chat-core.harness.mjs")
