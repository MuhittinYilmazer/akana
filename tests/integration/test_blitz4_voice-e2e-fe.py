"""Blitz 4 — voice-e2e frontend pytest wrapper.

Runs the node-vm contract harness (tests/web/blitz4_voice-e2e.harness.mjs) that drives
the real web_ui/static/akana-voice-live.js through a fake WebSocket. Self-contained copy
of the _run_node_harness pattern from test_web_ui_modules.py (do not edit shared files).

Distinct basename from tests/unit/test_blitz4_voice-e2e.py so pytest's prepend import
mode (no tests/*/__init__.py) does not see a module-name collision."""

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


def test_blitz4_voice_e2e_harness() -> None:
    """voice-e2e-1 (server interrupt frame clears the client barge latch → the post-Stop
    reply is audible) + fe-be-contract-1 (live 'tool' frame emits voice:tool for the
    aurora tool chip). Behavioural drive of _onServerMessage via a fake WS."""
    _run_node_harness(REPO_ROOT / "tests/web/blitz4_voice-e2e.harness.mjs")
