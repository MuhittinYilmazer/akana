"""Blitz 4 — FE↔BE contract drift (fe-be-contract-3 / fe-be-contract-4).

fe-be-contract-3: akana-settings.js kept dead handleWsEvent toast branches for
    policy_update / task_update / reminder_fire — frames no server code can emit
    (the PolicyEngine / task-runner / scheduler were removed pre-OSS). Verified by
    the node-vm harness below.

fe-be-contract-4: chat_producer.py yielded an SSE `timing` frame on every streamed
    turn, but no FE stream handler consumes it — dead wire bytes. The audit-side
    `stream_timings` capture must stay; the SSE emission must go.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_node_harness(harness: Path) -> None:
    # timeout: don't let a hung harness (dangling timer etc.) keep the suite
    # waiting forever — fail fast. Harnesses process.exit(0) on success.
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
    assert proc.returncode == 0, proc.stderr or proc.stdout


# ── fe-be-contract-3 ────────────────────────────────────────────────────────────
def test_blitz4_settings_ws_contract_harness() -> None:
    """akana-settings.js handleWsEvent: removed WS types reach the bus but produce
    no toast; the real turn/queue events still drive the chat surface."""
    _run_node_harness(REPO_ROOT / "tests/web/blitz4_fe-be-contract.harness.mjs")


# ── fe-be-contract-4 ────────────────────────────────────────────────────────────
def test_blitz4_chat_producer_no_timing_sse_frame() -> None:
    """The streaming producer must NOT ship a `timing` SSE frame (no FE consumer),
    while still recording timing phases in the audit blob + metrics."""
    src = (REPO_ROOT / "akana_server/api/routes/chat/chat_producer.py").read_text(
        encoding="utf-8"
    )
    # regression: the dead SSE emission is gone …
    assert '_sse_pack("timing"' not in src, (
        "chat_producer.py must not yield a 'timing' SSE frame — the FE stream "
        "handler has no 'timing' branch, so it is dropped as unread wire bytes"
    )
    # … but the audit-side capture + agent-timing metric survive.
    assert "stream_timings[str(timing[" in src, (
        "timing phases must still be recorded in the stream_timings audit blob"
    )
    assert "record_agent_timing_metric(timing.get(" in src, (
        "the agent_ready_ms metric hook must still fire"
    )


def test_blitz4_no_timing_sse_frame_anywhere_in_chat_routes() -> None:
    """No chat route should emit a 'timing' SSE frame that the FE cannot consume."""
    routes = REPO_ROOT / "akana_server/api/routes/chat"
    offenders = [
        p.name
        for p in routes.glob("*.py")
        if '_sse_pack("timing"' in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"'timing' SSE frame still emitted by: {offenders}"
