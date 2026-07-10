"""blitz3 fe-settings — node-vm contract harness runner (backend-free).

Six verified settings-area front-end fixes (pair QR recompose / serve-inactive toast,
tab-hide WS teardown, vault reload coalescing, runtime source-badge live language flip,
persona fork i18n suffix). The behavior contracts live in the node harness; this wrapper
just runs it under pytest. See tests/web/blitz3_fe-settings.harness.mjs.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_node_harness(harness: Path) -> None:
    # timeout: don't let a hung harness (dangling timer etc.) keep the suite waiting —
    # fail fast. Harnesses exit with process.exit(0) on success.
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


def test_blitz3_fe_settings_harness() -> None:
    _run_node_harness(REPO_ROOT / "tests/web/blitz3_fe-settings.harness.mjs")
