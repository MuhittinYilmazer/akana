"""Provider-aware reasoning-effort vocabulary — pytest wrapper.

Runs the node-vm contract harness (tests/web/effort_provider_vocab.harness.mjs) that drives
the real web_ui/static/akana-chat.js: codex/openai expose their native reasoning ladder
(minimal…xhigh, sent verbatim), claude/gemini keep the Akana tiers, cursor/ollama hide the
menu, and each vocabulary keeps its own persisted selection. Self-contained copy of the
_run_node_harness pattern from test_web_ui_modules.py (do not edit shared files)."""

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


def test_effort_provider_vocab_harness() -> None:
    """codex/openai → native reasoning ladder incl. xhigh; claude/gemini → Akana tiers
    (ultra claude-only); cursor/ollama → menu hidden; per-vocabulary persistence across
    provider switches; ultra→azami collapse on gemini."""
    _run_node_harness(REPO_ROOT / "tests/web/effort_provider_vocab.harness.mjs")
