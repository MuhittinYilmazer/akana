"""complete_chat_aggregated — resume-fail bootstrap retry."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from akana_server.config import load_settings
from akana_server.orchestrator import llm_dispatch


async def _fake_stream(
    events: list[dict[str, Any]],
) -> AsyncIterator[dict[str, Any]]:
    for ev in events:
        yield ev


@pytest.mark.asyncio
async def test_aggregated_bootstrap_retry_on_need_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = load_settings()
    calls: list[list[dict[str, str]] | None] = []

    async def loader() -> list[dict[str, str]]:
        return [{"role": "user", "content": "eski"}]

    async def hook() -> None:
        return None

    seq = [
        [{"need_history_bootstrap": True}],
        [{"delta": "tamam", "done": False}, {"done": True, "text": "tamam", "usage": {}}],
    ]

    async def fake_stream_user_chat(*_a: Any, **kw: Any) -> AsyncIterator[dict[str, Any]]:
        calls.append(kw.get("history"))
        batch = seq.pop(0)
        async for ev in _fake_stream(batch):
            yield ev

    monkeypatch.setattr(llm_dispatch, "stream_user_chat", fake_stream_user_chat)

    text, usage, agent_id = await llm_dispatch.complete_chat_aggregated(
        settings,
        "yeni mesaj",
        history=[],
        agent_id="stale-agent",
        conversation_id="conv-retry",
        bootstrap_history_loader=loader,
        on_bootstrap_retry=hook,
        context_mode="resume",
    )

    assert text == "tamam"
    assert agent_id is None
    assert usage["context_mode"] == "bootstrap_retry"
    assert usage["history_bootstrap_turns"] == 1
    assert len(calls) == 2
    assert calls[0] == []
    assert calls[1] == [{"role": "user", "content": "eski"}]
