"""Core API smoke — static pages and key read-only endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_dashboard_root(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in (r.headers.get("content-type") or "")


def test_memory_studio_page(client: TestClient) -> None:
    r = client.get("/memory")
    assert r.status_code == 200
    assert "memory-studio-page" in r.text


def test_system_status(client: TestClient) -> None:
    r = client.get("/api/v1/system/status")
    assert r.status_code == 200
    body = r.json()
    assert body["chat_path"] == "cursor"
    assert "server" in body
    # The model block carries ALL provider tags (the UI pill reads these; gemini_tag
    # was missing → with gemini selected the pill fell back to the cursor model — regression guard).
    model = body["model"]
    assert {"cursor_tag", "claude_tag", "ollama_tag", "gemini_tag", "active_tag"} <= set(model)
    assert model["gemini_tag"]  # not empty (resolve_gemini_model_tag → default)
    # The Gemini API health probe lives in dependencies (symmetric with cursor/claude). In the default
    # environment there is no key → key_set/reachable False (even if the SDK is installed, without a key).
    gemini_dep = body["dependencies"]["gemini_api"]
    assert {"key_set", "reachable"} <= set(gemini_dep)
    assert gemini_dep["key_set"] is False
    assert gemini_dep["reachable"] is False


def test_voice_config(client: TestClient) -> None:
    r = client.get("/api/v1/voice/config")
    assert r.status_code == 200
    body = r.json()
    assert "tts" in body
    assert "stt" in body
    assert "wake" in body
    # ``engine`` reflects the resolved preference (default auto); registered engines are listed.
    assert body["tts"]["engine"] in ("auto", "edge", "piper")
    assert "piper" in body["tts"]["engines"]
    # Gemini Live capability block (Phase 2): the UI toggle reads this. In the default environment
    # the flag is off + no SDK/key → enabled/available/provider_is_gemini False.
    live = body["live"]
    assert set(live) == {"enabled", "available", "provider_is_gemini", "voice"}
    assert live["enabled"] is False
    assert live["available"] is False
    assert live["provider_is_gemini"] is False
