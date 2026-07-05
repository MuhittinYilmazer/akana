"""★1a — metric wiring: turn_latency_ms / llm_errors / llm_timeout_fires + /system/metrics.

The observability/metrics.py registry (foundation) is now fed from the chat turn
path; this test locks the emits + the dump endpoint. Hermetic: packs off, LLM
mocked, the registry is reset on every test (module-singleton isolation).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app
from akana_server.observability import registry
from akana_server.orchestrator.llm_dispatch import LLMCallError


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("CURSOR_API_KEY", "x")
    monkeypatch.setattr(
        "akana_server.packs.host.AkanaPackHost.register_all", lambda self: []
    )
    registry.reset()
    app = create_app()
    with TestClient(app) as c:
        yield c
    registry.reset()


def _mock_blocking(monkeypatch, fn):
    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage", fn
    )


def test_metrics_endpoint_shape(client: TestClient) -> None:
    snap = client.get("/api/v1/system/metrics").json()
    assert set(snap) == {"counters", "timers"}


def test_blocking_turn_records_latency(client: TestClient, monkeypatch) -> None:
    async def ok(settings, user_text, **kwargs):
        return "tamam.", {"prompt_tokens": 1, "completion_tokens": 1, "tool_calls": []}

    _mock_blocking(monkeypatch, ok)
    assert client.post("/api/v1/chat", json={"text": "bugünü özetler misin"}).status_code == 200
    snap = client.get("/api/v1/system/metrics").json()
    assert snap["timers"].get("turn_latency_ms", {}).get("count", 0) >= 1


def test_llm_error_increments_counter(client: TestClient, monkeypatch) -> None:
    async def boom(settings, user_text, **kwargs):
        raise LLMCallError("upstream blew up", status_code=502)

    _mock_blocking(monkeypatch, boom)
    assert client.post("/api/v1/chat", json={"text": "hata ver"}).status_code == 502
    snap = client.get("/api/v1/system/metrics").json()
    assert snap["counters"].get("llm_errors", {}).get("value", 0) >= 1
    # a plain error is NOT a timeout → the timeout counter must not appear (or be 0)
    assert snap["counters"].get("llm_timeout_fires", {}).get("value", 0) == 0


def test_llm_timeout_increments_both(client: TestClient, monkeypatch) -> None:
    async def timed_out(settings, user_text, **kwargs):
        raise LLMCallError("LLM_TIMEOUT: cursor bridge timed out", status_code=504)

    _mock_blocking(monkeypatch, timed_out)
    assert client.post("/api/v1/chat", json={"text": "asıl"}).status_code == 504
    snap = client.get("/api/v1/system/metrics").json()
    assert snap["counters"].get("llm_errors", {}).get("value", 0) >= 1
    assert snap["counters"].get("llm_timeout_fires", {}).get("value", 0) >= 1
