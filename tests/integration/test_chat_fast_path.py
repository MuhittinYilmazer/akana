"""FAST PATH — gate-chain optimization on short/simple messages + ThinkingMode.

* thinking_mode matrix: an invalid value gives 422,
* snapshot: the SSE event sequence stays byte-for-byte identical on the normal and fast paths.

NOTE: The chat plan-gate (planner.route + plan→approval) was removed → the
``planner_route`` call-count tests were dropped from this file. The fast-path ITSELF (skill
suggestion budget + ThinkingMode) is preserved; the snapshot tests proving that the SSE
contract does not change on the fast/normal path stay here.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app
from akana_server.api.routes import chat as chat_routes

CHAT_URL = "/api/v1/chat"

SHORT_TEXT = "bugün hava nasıl"  # < 80 characters, doesn't smell multi-step, not a quick-action
LONG_TEXT = (
    "bana yapay zekanın tarihçesini, bugünkü uygulama alanlarını ve gelecekte"
    " bizi nelerin beklediğini uzun uzun anlatır mısın rica etsem"
)  # > 80 characters — full suggestion budget


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "x")
    monkeypatch.setenv("AKANA_MEMORY_LLM_CAPTURE", "0")
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    prompts: list[str] = []

    async def fake_complete(settings, prompt, **kwargs):
        prompts.append(prompt)
        return "yanit", {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(chat_routes, "complete_chat_with_usage", fake_complete)
    return prompts


# -- ThinkingMode validation --------------------------------------------------------------


def test_invalid_thinking_mode_rejected(client: TestClient) -> None:
    r = client.post(CHAT_URL, json={"text": "selam", "thinking_mode": "warp"})
    assert r.status_code == 422


# -- snapshot: SSE event sequence unchanged ------------------------------------------------


def _mock_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _stream(*_args: Any, **_kwargs: Any) -> AsyncIterator[dict[str, Any]]:
        yield {"delta": "Merhaba", "done": False}
        yield {
            "done": True,
            "text": "Merhaba",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "tool_calls": []},
            "status": "finished",
            "tool_calls": [],
        }

    monkeypatch.setattr(chat_routes, "stream_user_chat", _stream)


def _sse_event_names(body: str) -> list[str]:
    names: list[str] = []
    for block in body.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("event:"):
                names.append(line.split(":", 1)[1].strip())
    return names


#: The golden sequence of the current flow: meta → status(preparing) → status(model) →
#: delta → done → tts_end. The fast-path does NOT change this sequence (only the gate
#: latency drops). ``tts_end`` is now emitted on EVERY turn (even on a non-voice reply; with
#: the ``tts_active`` flag in the payload) — a deliberate change that prevents the frontend
#: re-arm freeze (#freeze); the signal is sent even when ``tts_active=false``.
GOLDEN_SSE_SEQUENCE = ["meta", "status", "status", "delta", "done", "tts_end"]


def test_sse_event_sequence_unchanged_on_normal_path(
    client: TestClient, fake_llm: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_stream(monkeypatch)
    with client.stream(
        "POST", f"{CHAT_URL}/stream", json={"text": LONG_TEXT}
    ) as r:
        assert r.status_code == 200
        body = r.read().decode("utf-8")
    assert _sse_event_names(body) == GOLDEN_SSE_SEQUENCE
    events = [json.loads(line.split(":", 1)[1]) for line in body.splitlines() if line.startswith("data:")]
    # 'done' is no longer the LAST event (tts_end comes after it) → find the 'done' payload
    # carrying the final turn text by field (text is only on done; on delta it's 'delta').
    done = next(e for e in events if "text" in e)
    assert done["text"] == "Merhaba"


def test_sse_event_sequence_identical_on_fast_path(
    client: TestClient, fake_llm: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _mock_stream(monkeypatch)
    with client.stream(
        "POST", f"{CHAT_URL}/stream", json={"text": SHORT_TEXT}
    ) as r:
        assert r.status_code == 200
        body = r.read().decode("utf-8")
    assert _sse_event_names(body) == GOLDEN_SSE_SEQUENCE
