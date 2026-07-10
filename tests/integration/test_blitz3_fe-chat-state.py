"""Blitz-3 fe-chat-state regression: run the node-vm contract harness.

Covers five verified bugs in web_ui/static/akana-chat-archive.js + akana-core.js:
  1  a failed DELETE must un-tombstone the id (rollback reload can restore the row);
  2  search in the archived tab must not hit /conversations/search (excludes archived);
  3  the launch-param handler must strip only consumed params (keep ?view= + hash);
  4  inline rename Escape must neutralize blur-save; a background re-render must not
     destroy an in-progress edit;
  5  search-result rows must carry the real pinned state (unpin from search possible).

The harness loads the REAL static JS in node:vm with a fake DOM and asserts the
behaviour contracts. (Own _run_node_harness copy — shared test files are edited by
other agents in parallel.)
"""

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
            env={**_os_environ(), "AKANA_LLM_CHAT_TITLES": "0"},
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - defensive
        raise AssertionError(
            f"node harness did not finish within 60s: {harness.name}"
        ) from exc
    assert proc.returncode == 0, proc.stderr or proc.stdout


def _os_environ() -> dict[str, str]:
    import os

    return dict(os.environ)


def test_blitz3_fe_chat_state_harness() -> None:
    _run_node_harness(REPO_ROOT / "tests/web/blitz3_fe-chat-state.harness.mjs")
