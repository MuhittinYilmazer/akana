"""Bug-blitz 3 — fe-chat-transport regression (node-vm harness wrapper).

Runs tests/web/blitz3_fe-chat-transport.harness.mjs, which loads the REAL
web_ui/static/akana-chat-transport.js in a node:vm with a fake DOM and drives
streamChat / finalizeThoughtFeed to lock down four verified fixes:

  1. streamChat sampled the conv id AFTER `await fetch` → a mid-connect chat switch
     routed the stream row into the wrong pane; now sampled ONCE before the fetch.
  3. The live usage HUD pill was never removed on transport-level (CONN/EMPTY)
     serverError paths → it lingered with stale token numbers.
  4. finalizeThoughtFeed hardcoded the Turkish "sn" seconds abbreviation.
  5. The memory-toast key-list fallback was the hardcoded Turkish word "bilgi".

Each finding is proved RED→GREEN inside the harness: the REAL source passes the
contract while a synthetic variant that reverts ONLY that fix exhibits the bug.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "tests/web/blitz3_fe-chat-transport.harness.mjs"


def _run_node_harness(harness: Path) -> None:
    # timeout: don't let a hung harness (dangling timer etc.) block the suite — fail
    # fast. Harnesses exit with process.exit(0) on success.
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")
    try:
        proc = subprocess.run(
            [node, str(harness)],
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


def test_blitz3_fe_chat_transport_harness() -> None:
    _run_node_harness(HARNESS)
