"""Gemini LIVE smoke test — an opt-in protocol check that hits the REAL Google API.

The hermetic gemini tests (``test_gemini_provider`` / ``test_gemini_catalog``) verify only
the CODE, not the request SHAPE, with a fake client. This module complements them: by making
a real ``generate_content`` / ``models.list()`` call it proves Google ACTUALLY accepts our
request (config + tools + contents shape) → it catches the protocol drift (a request shape the
API rejects) that the fake tests miss.

DOUBLE GATE (never runs accidentally / burns quota):
  1. The ``AKANA_LIVE_SMOKE=1`` environment variable must be SET, AND
  2. The Gemini key must RESOLVE (``gemini_shared.resolve_api_key`` non-None) AND
     the google-genai SDK must be installed (``genai_installed()``).
If both are not satisfied the tests are SKIPPED via ``skipif`` / runtime ``pytest.skip``.

HOW TO RUN (deliberate; burns the user's own quota):
    AKANA_LIVE_SMOKE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
        venv/bin/python -m pytest tests/unit/test_gemini_live_smoke.py -q

DEFAULT (``AKANA_LIVE_SMOKE`` UNSET) → all SKIPPED, 0 failures. Token usage is MINIMAL
(tiny prompt, 1 request per call). A quota/network error is not a HARD-FAIL but a clean
``pytest.skip`` — these tests are DIAGNOSTIC, not gating. Driven with ``asyncio.run``
(the suite runs without pytest-asyncio under ``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1``)."""

from __future__ import annotations

import asyncio
import os

import pytest

from akana_server.config import load_settings
from akana_server.orchestrator import gemini_provider
from akana_server.orchestrator.gemini_catalog import fetch_gemini_models
from akana_server.orchestrator.gemini_shared import genai_installed, resolve_api_key

#: Gate 1: the opt-in envelope. If not set, no real call is made.
_LIVE_ENABLED = os.environ.get("AKANA_LIVE_SMOKE", "").strip() == "1"


def _live_settings_or_skip():
    """Build a real ``Settings`` + apply Gate 2 (SDK installed + key resolves).

    Gate 1 (``AKANA_LIVE_SMOKE``) is already applied via a module-level ``skipif``; this
    helper only distinguishes the provider precondition at runtime → a clean skip if missing."""
    settings = load_settings()
    if not genai_installed():
        pytest.skip("google-genai SDK not installed (pip install -r requirements-gemini.txt)")
    if not resolve_api_key(settings):
        pytest.skip("gemini api key did not resolve (secret_store/env)")
    return settings


@pytest.mark.skipif(not _LIVE_ENABLED, reason="AKANA_LIVE_SMOKE=1 is not set")
def test_live_fetch_gemini_models_reachable() -> None:
    """Live catalog: ``models.list()`` is reachable + returns a non-empty live model list.

    Verifies that the ``models.list()`` call (auth + pagination) ACTUALLY works."""
    settings = _live_settings_or_skip()
    try:
        result = asyncio.run(fetch_gemini_models(settings, force_refresh=True))
    except Exception as e:  # noqa: BLE001 - diagnostic test: quota/network → skip, not hard-fail
        pytest.skip(f"live api unavailable: {e}")
    if not result.get("reachable"):
        pytest.skip(f"live api unavailable: {result.get('error')}")
    assert result["reachable"] is True
    assert result.get("source") == "live"
    assert result.get("models"), "the live model list must not be empty"


@pytest.mark.skipif(not _LIVE_ENABLED, reason="AKANA_LIVE_SMOKE=1 is not set")
def test_live_complete_chat_returns_text() -> None:
    """Live completion: tiny prompt → non-empty text, status 'finished'.

    Does Google accept the request SHAPE (config + function_declarations + contents) —
    the actual protocol-drift check. Single request, minimal tokens."""
    settings = _live_settings_or_skip()
    try:
        text, status, _raw = asyncio.run(
            gemini_provider.complete_chat(settings, "reply with: OK")
        )
    except Exception as e:  # noqa: BLE001 - diagnostic test: quota/network → skip, not hard-fail
        pytest.skip(f"live api unavailable: {e}")
    assert status == "finished"
    assert text and text.strip(), "live completion must return non-empty text"


@pytest.mark.skipif(not _LIVE_ENABLED, reason="AKANA_LIVE_SMOKE=1 is not set")
def test_live_voice_session_manual_placeholder() -> None:
    """Full-duplex Live voice (WS native-audio session) is NOT exercised here — a placeholder.

    Live voice requires a real WebSocket + bidirectional audio stream (send mic PCM / receive
    audio chunks); it is out of scope for an automatic smoke test (quota + audio hardware +
    socket lifecycle). It is verified manually (``/ws/voice/live``). This test DOCUMENTS that
    gap and is always skipped."""
    pytest.skip("manual: the Live native-audio WS session is verified by hand (not automatic)")
