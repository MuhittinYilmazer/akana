"""Bug-blitz 3 — fe-chat-render regression wrapper.

Runs the node-vm harness that loads the REAL web_ui/static/akana-markdown.js and
akana-chat-render.js against a fake DOM and asserts the seven verified fixes
(fence-verbatim preprocessing, mid-sentence '#' not promoted to a heading, dotted
tool names routing to the memory family, the streaming decorate-guard reading the
per-pane flag, the shell-as-toolname term-card command fallback, no fabricated 0ms
elapsed on history-restored cards/groups, and the todo-card subagent hijack).

Mirrors the `_run_node_harness` pattern from test_web_ui_modules.py (kept local so
this file does not edit shared test modules).
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
        )
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - defensive
        raise AssertionError(
            f"node harness did not finish within 60s: {harness.name}"
        ) from exc
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr


def test_blitz3_fe_chat_render_harness() -> None:
    _run_node_harness(REPO_ROOT / "tests/web/blitz3_fe-chat-render.harness.mjs")
