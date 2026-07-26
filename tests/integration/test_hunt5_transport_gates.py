"""Hunt-5 "foreground gate" regression (node-vm harness wrapper).

Runs tests/web/hunt5_transport_gates.harness.mjs, which loads the REAL
web_ui/static/akana-chat-transport.js in a node:vm with a fake DOM and drives
resumeActiveTurn / streamChat / handleChatStreamEvent to lock down the confirmed
findings in the transport's foreground-gate family:

  * resumeActiveTurn bound the SINGLETON turn-status strip to a conversation the
    user had switched away from during the probe await (concurrent-turns-1,
    singleton-ui-across-chats-2).
  * The 202 "queued" branch wrote the global queue chip / STOP mode for a
    conversation that was no longer displayed (singleton-ui-across-chats-4,
    concurrent-turns-2).
  * streamChat's setPhase("connecting") repainted the displayed conversation's
    strip and corrupted its per-conversation clock (concurrent-turns-3).
  * chat:stream:error and voice:tool reached the Aurora voice scene from
    BACKGROUND streams (voice-vs-chat-5, voice-vs-chat-2).
  * A GET /chat/active resume follower replays the detached turn's buffer from
    index 0, so its tts_chunk frames re-spoke already-heard audio
    (voice-vs-chat-1).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
HARNESS = REPO_ROOT / "tests/web/hunt5_transport_gates.harness.mjs"


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


def test_hunt5_transport_gates_harness() -> None:
    _run_node_harness(HARNESS)
