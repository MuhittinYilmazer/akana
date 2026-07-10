"""Bug-blitz-3 fe-voice regression wrappers — run the node-vm contract harness.

Locks in two jury-CONFIRMED voice fixes (see tests/web/blitz3_fe-voice.harness.mjs):
  fe-voice-2  Settings mic-device picker re-arms wake after tearing the audio graph down.
  fe-voice-3  Aurora "Stop" routes to the live barge (AkanaVoiceLive.interrupt) in Live mode.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_node_harness(harness: Path) -> None:
    # timeout: don't let a hung harness (dangling timer etc.) wedge the suite — fail fast.
    # Harnesses exit with process.exit(0) on success.
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


def test_blitz3_fe_voice_harness() -> None:
    _run_node_harness(REPO_ROOT / "tests/web/blitz3_fe-voice.harness.mjs")
