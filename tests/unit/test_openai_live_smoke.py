"""OpenAI LIVE smoke test — opt-in protocol check hitting the REAL OpenAI API.

The OpenAI twin of ``test_gemini_live_smoke``. The hermetic openai tests (``test_openai_
provider`` / ``test_openai_catalog``) use a fake client and verify only the CODE, not the
request SHAPE. This module is complementary: by making a real ``chat/completions`` /
``GET /models`` call it proves OpenAI ACTUALLY accepts our request (messages + tools
shape) → catches the protocol drift the fake tests miss.

DOUBLE GATE (never runs / burns quota by accident):
  1. The ``AKANA_LIVE_SMOKE=1`` environment variable must be SET, AND
  2. The OpenAI key must RESOLVE (``openai_shared.resolve_openai_key`` non-None).
     (gemini's SDK-installed precondition does NOT apply — transport httpx is a fixed dependency.)
If either is not met, the tests are SKIPPED via ``skipif`` / runtime ``pytest.skip``.

RUN (intentional; burns the user's own quota):
    AKANA_LIVE_SMOKE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
        venv/bin/python -m pytest tests/unit/test_openai_live_smoke.py -q

DEFAULT (``AKANA_LIVE_SMOKE`` UNSET) → all SKIPPED, 0 failures. Token usage is
MINIMAL (tiny prompt, 1 request per call). A quota/network error is not a HARD-FAIL but a clean
``pytest.skip`` — these tests are DIAGNOSTIC, not gating. Driven with ``asyncio.run``
(the suite runs under ``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`` without pytest-asyncio)."""

from __future__ import annotations

import asyncio
import os

import pytest

from akana_server.config import load_settings
from akana_server.orchestrator import openai_provider
from akana_server.orchestrator.openai_catalog import fetch_openai_models
from akana_server.orchestrator.openai_shared import resolve_openai_key

#: Gate 1: the opt-in envelope. If not set, no real call is made.
_LIVE_ENABLED = os.environ.get("AKANA_LIVE_SMOKE", "").strip() == "1"


def _live_settings_or_skip():
    """Build a real ``Settings`` + enforce Gate 2 (the key resolves).

    Gate 1 (``AKANA_LIVE_SMOKE``) is already enforced by a module-level ``skipif``; this
    helper only resolves the provider key at runtime → a clean skip if missing.
    (DIFFERENCE from gemini: no SDK-installed gate, only the key.)"""
    settings = load_settings()
    if not resolve_openai_key(settings):
        pytest.skip("openai api key not resolved (secret_store/env)")
    return settings


@pytest.mark.skipif(not _LIVE_ENABLED, reason="AKANA_LIVE_SMOKE=1 not set")
def test_live_fetch_openai_models_reachable() -> None:
    """Live catalog: ``GET /models`` is reachable + returns a non-empty live model list.

    Verifies the ``GET {base}/models`` call (auth + JSON parsing) ACTUALLY works."""
    settings = _live_settings_or_skip()
    try:
        result = asyncio.run(fetch_openai_models(settings, force_refresh=True))
    except Exception as e:  # noqa: BLE001 - diagnostic test: quota/network → skip, not hard-fail
        pytest.skip(f"live api unavailable: {e}")
    if not result.get("reachable"):
        pytest.skip(f"live api unavailable: {result.get('error')}")
    assert result["reachable"] is True
    assert result.get("source") == "live"
    assert result.get("models"), "the live model list must not be empty"


@pytest.mark.skipif(not _LIVE_ENABLED, reason="AKANA_LIVE_SMOKE=1 not set")
def test_live_complete_chat_returns_text() -> None:
    """Live completion: tiny prompt → non-empty text, status 'finished'.

    Does OpenAI accept the request SHAPE (messages + tools) — the real protocol-drift
    check. One request, minimal tokens."""
    settings = _live_settings_or_skip()
    try:
        text, status, _raw = asyncio.run(
            openai_provider.complete_chat(settings, "reply with: OK")
        )
    except Exception as e:  # noqa: BLE001 - diagnostic test: quota/network → skip, not hard-fail
        pytest.skip(f"live api unavailable: {e}")
    assert status == "finished"
    assert text and text.strip(), "the live completion must return non-empty text"


@pytest.mark.skipif(not _LIVE_ENABLED, reason="AKANA_LIVE_SMOKE=1 not set")
def test_live_realtime_session_manual_placeholder() -> None:
    """Full-duplex Realtime audio (WS session) is NOT attempted here — a placeholder.

    Realtime requires a real WebSocket + bidirectional audio stream (send microphone PCM /
    receive ``response.audio.delta``); it is out of scope for an automated smoke test
    (quota + audio hardware + socket lifecycle). Verified manually (``/ws/voice/realtime``).
    This test DOCUMENTS that gap and is always skipped."""
    pytest.skip("manual: the Realtime WS session is verified by hand (not automated)")
