"""hunt5 misc — node-vm contract harness runner (backend-free).

Conversation-switch LLM restore (akana-settings.js) must be generation-guarded: it is
fired un-awaited on every switch and mutates GLOBAL runtime provider/model + header pill
+ thinking-provider, so a superseded restore must not land. Contract lives in the harness;
this wrapper just runs it. See tests/web/hunt5_misc.harness.mjs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_node_harness(harness: Path) -> None:
    # timeout: a hung harness (dangling timer) must fail fast, not stall the suite.
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


def test_hunt5_misc_harness() -> None:
    _run_node_harness(REPO_ROOT / "tests/web/hunt5_misc.harness.mjs")
