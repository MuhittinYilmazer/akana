"""Hunt-5 restore/F5 regression (node-vm harness wrapper).

Runs the backend-free node harness that loads the REAL akana-chat-store.js,
akana-chat-threads.js and akana-chat.js in a VM with a fake DOM and drives the
reload/restore paths: the stale-restore guard, the transient-failure binding rule, the
still-queued (202) message rescue, per-conversation attachment parking and the post-F5
background-work marker. Each contract is proved RED by a synthetic string-revert of only
that fix. See tests/web/hunt5_threads_restore.harness.mjs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_node_harness(harness: Path) -> None:
    # timeout: a hung harness (dangling timer) must not keep the suite waiting forever.
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


def test_hunt5_threads_restore_harness() -> None:
    _run_node_harness(REPO_ROOT / "tests/web/hunt5_threads_restore.harness.mjs")
