"""Health and chat API smoke tests."""

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


def test_uvicorn_entrypoint_app_attribute(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Regression shield: uvicorn loads via the 'akana_server.main:app' string →
    the main module MUST export the 'app' attribute. (A lint polish mistook this
    re-export for F401 and deleted it, and the server failed to start with
    'Attribute app not found'; it slipped through because no test used main:app.)"""
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CURSOR_API_KEY", "")
    import importlib

    main_mod = importlib.import_module("akana_server.main")
    app = getattr(main_mod, "app", None)
    assert app is not None, "akana_server.main must export 'app' (uvicorn entry-point)"
    assert type(app).__name__ == "FastAPI", "main:app must be a FastAPI application"


def test_dashboard_root(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in (r.headers.get("content-type") or "")


def test_memory_studio_page(client: TestClient) -> None:
    r = client.get("/memory")
    assert r.status_code == 200
    assert "memory-studio-page" in r.text
    assert "memory-studio-root" in r.text  # studio shell mount point


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert data["service"] == "akana"
    assert data["phase"] == "P0"
    assert data["server"]["port"] == 8766


def test_chat_requires_cursor_key(client: TestClient) -> None:
    r = client.post("/api/v1/chat", json={"text": "hello"})
    assert r.status_code == 503


def test_system_status(client: TestClient) -> None:
    r = client.get("/api/v1/system/status")
    assert r.status_code == 200
    body = r.json()
    assert body["chat_path"] == "cursor"
    assert "server" in body
    model = body["model"]
    # The canonical fields (UI model-pill contract) + backward-compat fields.
    assert model["provider"] == "cursor"
    assert model["active_tag"] == model["cursor_tag"]
    assert model["agent_id"] == "cursor"
    assert model["claude_tag"].startswith("claude-")
    # OpenAI fields are symmetric with gemini: tag for the model-pill + a dependency probe.
    assert "openai_tag" in model
    cursor_api = body["dependencies"]["cursor_api"]
    assert cursor_api["reachable"] is False
    assert cursor_api["key_set"] is False
    assert "error" in cursor_api
    openai_api = body["dependencies"]["openai_api"]
    assert openai_api["reachable"] is False
    assert openai_api["key_set"] is False
    assert "error" in openai_api


def test_system_status_claude_provider_active_tag(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """When provider=claude, active_tag must be claude_tag (NOT the cursor tag)."""
    from akana_server.llm_settings import LlmSettings, save_llm_settings

    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    save_llm_settings(
        tmp_path,
        LlmSettings(
            cursor_model="composer-2", provider="claude", claude_model="claude-opus-4-7"
        ),
    )
    app = create_app()
    with TestClient(app) as c:
        body = c.get("/api/v1/system/status").json()
    assert body["chat_path"] == "claude"
    model = body["model"]
    assert model["provider"] == "claude"
    assert model["claude_tag"] == "claude-opus-4-7"
    assert model["active_tag"] == "claude-opus-4-7"
    assert model["cursor_tag"] == "composer-2"  # backward-compat field preserved


def test_voice_config(client: TestClient) -> None:
    r = client.get("/api/v1/voice/config")
    assert r.status_code == 200
    body = r.json()
    # ``engine`` reflects the resolved preference (default auto); the engines are in ``engines``.
    assert body["tts"]["engine"] in ("auto", "edge", "piper")
    assert "piper" in body["tts"]["engines"]
    assert "stt" in body
    assert "wake" in body
