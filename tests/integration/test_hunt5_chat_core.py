"""hunt-5 chat-core regression: run the node-vm contract harness.

Covers the background-work indicator + status strip in web_ui/static/akana-chat.js and
web_ui/static/akana-turn-status.js:
  · only source:"background" turn events drive the indicator (an unstamped event is the
    user's own turn — fail quiet), and the marker is a per-conversation COUNT, so the
    user's quick reply completing cannot hide a job that is still running;
  · the indicator reconciles against the server's live-turn snapshot: no phantom strip
    after a missed turn_completed, and a job that survived F5 becomes visible again;
  · voice conversation mode is pinned to the chat its scene shows, so a desktop
    notification click cannot retarget the next utterance;
  · AkanaTurnStatus.clear() takes the strip down instead of leaving it painting an
    epoch-sized elapsed time.

(Own _run_node_harness copy — shared test files are edited by other agents in parallel.)
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_node_harness(harness: Path) -> None:
    try:
        proc = subprocess.run(
            ["node", str(harness)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            env={**os.environ, "AKANA_LLM_CHAT_TITLES": "0"},
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - defensive
        raise AssertionError(
            f"node harness did not finish within 60s: {harness.name}"
        ) from exc
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_hunt5_chat_core_harness() -> None:
    _run_node_harness(REPO_ROOT / "tests/web/hunt5_chat_core.harness.mjs")
