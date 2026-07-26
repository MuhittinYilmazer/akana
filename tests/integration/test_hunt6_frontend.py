"""hunt-6 front-end regression: run the node-vm contract harness.

Covers the composer's send/stop paths in web_ui/static/akana-chat.js and the
language reach of the chat surface:
  · Stop→send is not re-entrant — a second click during the multi-second server-cancel
    await used to echo, persist and BILL the same typed message twice; the latch is per
    conversation and is released on every exit path (including a rejected cancel);
  · a Stop click always stops: a send rejected by the per-message attachment budget may
    not swallow the emergency brake, and the force-immediate latch may not leak into the
    next ordinary Enter (which would silently cancel the displayed chat's running turn);
  · Stop is never a silent no-op — with a queued message and no live stream it explains
    itself and re-reads the queue instead of doing nothing;
  · schema ↔ i18n drift: every non-hidden runtime setting has a Turkish label AND desc;
  · the wake-threshold slider's markup default equals DEFAULTS["wake_threshold"], and a
    failed GET /voice/config leaves no unverified number on screen;
  · chat timestamps and tool-card argument labels follow the APP language (the labels are
    resolved per render, not frozen at script-eval time before the language is known).

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


def test_hunt6_frontend_harness() -> None:
    _run_node_harness(REPO_ROOT / "tests/web/hunt6_frontend.harness.mjs")
