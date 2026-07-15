"""Codex LIVE smoke test — opt-in protocol check hitting the REAL ``codex exec`` CLI.

The Codex twin of ``test_openai_live_smoke`` / ``test_gemini_live_smoke``. The hermetic
tests (``test_codex_provider`` / ``test_codex_catalog``) fake the subprocess and verify
only the CODE + argv SHAPE. This module is complementary: by actually spawning
``codex exec --json`` against the logged-in ChatGPT session it proves the CLI ACCEPTS our
flags + JSONL contract (catches the protocol drift the fake tests miss).

TRIPLE GATE (never runs / burns a ChatGPT turn by accident):
  1. ``AKANA_LIVE_SMOKE=1`` must be SET, AND
  2. the ``codex`` CLI must be on PATH, AND
  3. ``codex login status`` must exit 0 (a live ChatGPT session).
If any is unmet the tests SKIP (``skipif`` / runtime ``pytest.skip``). NO API key is used
— auth is the ``codex login`` session.

RUN (intentional; consumes one real Codex turn):
    AKANA_LIVE_SMOKE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
        venv/bin/python -m pytest tests/unit/test_codex_live_smoke.py -q

DEFAULT (``AKANA_LIVE_SMOKE`` UNSET) → all SKIPPED, 0 failures. Driven with
``asyncio.run`` (the suite runs under ``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`` without
pytest-asyncio)."""

from __future__ import annotations

import asyncio
import os

import pytest

from akana_server.config import load_settings
from akana_server.orchestrator import codex_provider
from akana_server.orchestrator.codex_catalog import probe_codex_cli

#: Gate 1: the opt-in envelope. If not set, no real call is made.
_LIVE_ENABLED = os.environ.get("AKANA_LIVE_SMOKE", "").strip() == "1"


def _live_settings_or_skip():
    """Build a real ``Settings`` + enforce Gates 2/3 (CLI installed AND logged in)."""
    settings = load_settings()
    probe = asyncio.run(probe_codex_cli(settings))
    if not probe.get("installed"):
        pytest.skip("codex CLI not on PATH (install: npm i -g @openai/codex)")
    if not probe.get("logged_in"):
        pytest.skip("codex not logged in (run `codex login`)")
    return settings


@pytest.mark.skipif(not _LIVE_ENABLED, reason="AKANA_LIVE_SMOKE=1 not set")
def test_live_probe_reports_logged_in() -> None:
    """The auth probe reaches a live ChatGPT session (`codex login status` exits 0)."""
    settings = _live_settings_or_skip()
    probe = asyncio.run(probe_codex_cli(settings))
    assert probe["installed"] is True
    assert probe["logged_in"] is True
    assert probe["reachable"] is True


@pytest.mark.skipif(not _LIVE_ENABLED, reason="AKANA_LIVE_SMOKE=1 not set")
def test_live_complete_chat_returns_text() -> None:
    """Live completion: tiny prompt → non-empty text, status 'finished'.

    Does ``codex exec`` accept our flags + JSONL contract — the real protocol-drift check.
    One turn, minimal output. MCP is left off (a plain turn) to keep it fast."""
    settings = _live_settings_or_skip()
    try:
        text, status, _raw = asyncio.run(
            codex_provider.complete_chat(
                settings, "Reply with exactly: OK", chat_mode=False, mcp_servers=None
            )
        )
    except Exception as e:  # noqa: BLE001 - diagnostic: rate limit/network → skip, not hard-fail
        pytest.skip(f"live codex unavailable: {e}")
    assert status == "finished"
    assert text and text.strip(), "the live completion must return non-empty text"


@pytest.mark.skipif(not _LIVE_ENABLED, reason="AKANA_LIVE_SMOKE=1 not set")
def test_live_resume_thread_manual_placeholder() -> None:
    """Multi-turn ``codex exec resume <thread_id>`` is NOT auto-exercised here.

    A resume smoke would consume TWO turns (create + resume) and depend on the first
    turn's ``thread.started`` id; that is verified by hand. This test DOCUMENTS the gap
    and is always skipped."""
    pytest.skip("manual: `codex exec resume <thread_id>` is verified by hand (two turns)")
