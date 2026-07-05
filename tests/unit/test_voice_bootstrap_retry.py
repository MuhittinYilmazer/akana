"""Voice route — stale resume bootstrap retry wiring (blocking / complete_chat path)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app
from akana_server.chat_context import CONTEXT_MODE_BOOTSTRAP, CONTEXT_MODE_RESUME
from akana_server.orchestrator import llm_dispatch


async def _mock_transcribe(*_args, **_kwargs):
    return "ses ile soru", "tr"


async def _fake_stream(
    events: list[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    for ev in events:
        yield ev


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_post_voice_passes_bootstrap_hooks_and_context_mode(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Voice blocking path must pass the same bootstrap hooks as chat blocking."""
    monkeypatch.setattr(
        "akana_server.api.routes.voice.transcribe_wav_bytes",
        _mock_transcribe,
    )

    captured: dict[str, Any] = {}

    async def fake_complete(*_args, **kwargs):
        captured.update(kwargs)
        return "Ses ile cevap.", {
            "prompt_tokens": 2,
            "completion_tokens": 4,
            "tool_calls": [],
            "context_mode": kwargs.get("context_mode"),
        }

    # turn_core reads complete_chat_with_usage from the chat package at call time.
    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage",
        fake_complete,
    )

    fake_wav = b"RIFF" + b"\x00" * 200
    r = client.post(
        "/api/v1/voice",
        files={"audio": ("test.wav", fake_wav, "audio/wav")},
    )
    assert r.status_code == 200, r.text
    assert captured.get("bootstrap_history_loader") is not None
    assert captured.get("on_bootstrap_retry") is not None
    assert captured.get("context_mode") in (CONTEXT_MODE_RESUME, CONTEXT_MODE_BOOTSTRAP)


@pytest.mark.asyncio
async def test_voice_path_bootstrap_retry_on_stale_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """complete_chat_aggregated retry — the blocking path used by voice."""
    from akana_server.config import load_settings

    settings = load_settings()
    hook_calls = 0

    async def loader() -> list[dict[str, str]]:
        return [{"role": "user", "content": "önceki tur"}]

    async def hook() -> None:
        nonlocal hook_calls
        hook_calls += 1

    seq = [
        [{"need_history_bootstrap": True}],
        [{"delta": "tamam", "done": False}, {"done": True, "text": "tamam", "usage": {}}],
    ]

    async def fake_stream_user_chat(*_a: Any, **kw: Any) -> AsyncIterator[dict[str, Any]]:
        batch = seq.pop(0)
        async for ev in _fake_stream(batch):
            yield ev

    monkeypatch.setattr(llm_dispatch, "stream_user_chat", fake_stream_user_chat)

    text, usage, agent_id = await llm_dispatch.complete_chat_aggregated(
        settings,
        "[mode: voice]\nyeni soru",
        history=[],
        agent_id="stale-agent",
        conversation_id="conv-voice-retry",
        bootstrap_history_loader=loader,
        on_bootstrap_retry=hook,
        context_mode=CONTEXT_MODE_RESUME,
    )

    assert text == "tamam"
    assert agent_id is None
    assert hook_calls == 1
    assert usage["context_mode"] == "bootstrap_retry"
    assert usage["history_bootstrap_turns"] == 1
