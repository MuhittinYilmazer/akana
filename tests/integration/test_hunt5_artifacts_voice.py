"""Hunt 5 "live state" — artifacts + voice singleton contracts (pytest wrapper).

Runs the node-vm harness (tests/web/hunt5_artifacts_voice.harness.mjs) against the real
web_ui/static modules:

  * akana-voice-pipeline.js — the single-shot wake POST snapshots the DISPLAYED conversation
    and stops writing shared state (pane rows, active-thread record, setConversationId rebind,
    composer clear) into whatever chat the user switched to while it was in flight.
  * akana-artifacts.js + akana-shell.js — the artifact panel is a module singleton; the
    conversation-switch dismiss site closes it and drops current/lastTrigger without stealing
    focus back into the chat being left.

Self-contained copy of the _run_node_harness pattern from test_web_ui_modules.py
(do not edit shared test files)."""

from __future__ import annotations

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
        )
    except subprocess.TimeoutExpired as exc:
        raise AssertionError(
            f"node harness did not finish within 60s (likely a dangling timer): {harness.name}"
        ) from exc
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_hunt5_artifacts_voice_harness() -> None:
    """A mid-flight chat switch must not hijack the new chat's pane/thread/composer, and a
    conversation switch must dismiss the shared artifact preview panel."""
    _run_node_harness(REPO_ROOT / "tests/web/hunt5_artifacts_voice.harness.mjs")
