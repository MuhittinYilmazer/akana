"""Hunt-5 archive-sidebar regressions (node-vm harness wrapper).

Runs the backend-free node harness that loads the REAL akana-chat-archive.js in a VM with
a fake DOM and drives the three "the sidebar must not lie" contracts: activity badges are
reconciled for every LISTED row after an F5 (not just the displayed chat), a transient list
fetch failure never blanks a populated sidebar nor an in-progress inline rename, and every
path that PATCHes pinned keeps the pinned meta cache in step.
See tests/web/hunt5_archive.harness.mjs.
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


def test_hunt5_archive_sidebar_harness() -> None:
    _run_node_harness(REPO_ROOT / "tests/web/hunt5_archive.harness.mjs")
