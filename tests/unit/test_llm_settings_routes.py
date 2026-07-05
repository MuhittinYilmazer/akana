"""LLM settings routes — GET/PUT /api/v1/system/llm-settings (web_ui akana-settings.js contract)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app

URL = "/api/v1/system/llm-settings"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    # Pin env-derived defaults so assertions don't depend on the repo .env.
    monkeypatch.setenv("CURSOR_MODEL", "composer-2")
    monkeypatch.setenv("WAKE_THRESHOLD", "0.15")
    # Clear the OpenAI model env fallback → active_openai_model_tag falls to the
    # default (first catalog option); the dev machine's OPENAI_MODEL must not affect the test.
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_get_returns_env_defaults(client: TestClient) -> None:
    r = client.get(URL)
    assert r.status_code == 200
    body = r.json()
    # NOTE: wake_threshold is no longer on the llm-settings surface — the single
    # source of truth is runtime_settings (see test_runtime_settings.py wake_threshold).
    assert body["settings"] == {
        "cursor_model": "composer-2",
        "chat_max_turns": 12,
        "tts_lang": "auto",
        "provider": "",
        "claude_model": "",
        "ollama_model": "",
        "gemini_model": "",
        "openai_model": "",
        "claude_full_tools": True,
    }
    assert body["active_cursor_model_tag"] == "composer-2"
    assert body["active_provider"] == "cursor"
    assert body["active_claude_model_tag"].startswith("claude-")
    assert body["active_ollama_model_tag"] == "llama3.1"  # default when no env
    assert body["active_gemini_model_tag"] == "gemini-2.5-flash"  # default when no env
    assert body["active_openai_model_tag"] == "gpt-5.4"  # first catalog option when no env
    # the claude provider is at full authority by default (bypassPermissions).
    assert body["active_claude_full_tools"] is True
    # Model picker options for the dashboard dropdown.
    values = [opt["value"] for opt in body["cursor_models"]]
    assert "composer-2" in values
    assert all(set(opt) == {"value", "label"} for opt in body["cursor_models"])
    claude_values = [opt["value"] for opt in body["claude_models"]]
    assert "claude-sonnet-4-6" in claude_values
    # Each option is either a claude-* tag or a bare alias (the CLI resolves these to
    # the newest version); both are considered valid by claude_provider.
    assert all(
        v.startswith("claude-") or v in {"opus", "sonnet", "haiku"}
        for v in claude_values
    )
    assert all(set(opt) == {"value", "label"} for opt in body["claude_models"])
    gemini_values = [opt["value"] for opt in body["gemini_models"]]
    assert "gemini-2.5-flash" in gemini_values
    assert all(v.startswith("gemini-") for v in gemini_values)
    assert all(set(opt) == {"value", "label"} for opt in body["gemini_models"])
    openai_values = [opt["value"] for opt in body["openai_models"]]
    assert "gpt-5.4" in openai_values
    # NATIVE OpenAI name (gpt-* / o-series) — not a cursor-routed alias.
    assert all(v.startswith("gpt-") or v.startswith("o") for v in openai_values)
    assert all(set(opt) == {"value", "label"} for opt in body["openai_models"])
    provider_values = [opt["value"] for opt in body["providers"]]
    assert provider_values == ["cursor", "claude", "ollama", "gemini", "openai"]
    assert body["defaults"]["chat_max_turns"] == 12
    assert body["defaults"]["cursor_model"] == "composer-2"


def test_put_updates_and_persists_to_disk(
    client: TestClient, tmp_path
) -> None:
    r = client.put(URL, json={"cursor_model": "claude-haiku-4-5", "chat_max_turns": 20})
    assert r.status_code == 200
    body = r.json()
    assert body["settings"]["cursor_model"] == "claude-haiku-4-5"
    assert body["settings"]["chat_max_turns"] == 20
    assert body["active_cursor_model_tag"] == "claude-haiku-4-5"

    # Persisted file in data_dir.
    saved = json.loads((tmp_path / "llm_settings.json").read_text(encoding="utf-8"))
    assert saved["cursor_model"] == "claude-haiku-4-5"
    assert saved["chat_max_turns"] == 20

    # Subsequent GET serves the updated state.
    again = client.get(URL).json()
    assert again["settings"]["cursor_model"] == "claude-haiku-4-5"
    assert again["settings"]["chat_max_turns"] == 20


def test_put_survives_app_restart(client: TestClient) -> None:
    assert client.put(URL, json={"cursor_model": "gpt-5.4-mini"}).status_code == 200
    # New app instance, same data_dir env — must reload from disk.
    with TestClient(create_app()) as second:
        body = second.get(URL).json()
        assert body["settings"]["cursor_model"] == "gpt-5.4-mini"
        assert body["active_cursor_model_tag"] == "gpt-5.4-mini"


def test_put_accepts_nested_settings_object(client: TestClient) -> None:
    r = client.put(URL, json={"settings": {"tts_lang": "tr", "chat_max_turns": 8}})
    assert r.status_code == 200
    body = r.json()
    assert body["settings"]["tts_lang"] == "tr"
    assert body["settings"]["chat_max_turns"] == 8


def test_put_invalid_json_returns_400(client: TestClient) -> None:
    r = client.put(URL, content=b"not-json{", headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"]["code"] == "INVALID_JSON"


@pytest.mark.parametrize("payload", [["cursor_model"], "composer-2", 42])
def test_put_non_object_body_returns_422(client: TestClient, payload) -> None:
    r = client.put(URL, json=payload)
    assert r.status_code == 422
    assert r.json()["detail"]["error"]["code"] == "INVALID_BODY"


def test_put_unknown_fields_silently_ignored(client: TestClient) -> None:
    """Real behavior: unknown keys are dropped (no 4xx), known state untouched."""
    before = client.get(URL).json()["settings"]
    r = client.put(URL, json={"nope": 1, "api_key": "leak?", "model": "composer-2"})
    assert r.status_code == 200
    body = r.json()
    assert body["settings"] == before
    assert "nope" not in body["settings"]


def test_put_empty_object_is_a_noop(client: TestClient) -> None:
    before = client.get(URL).json()["settings"]
    r = client.put(URL, json={})
    assert r.status_code == 200
    assert r.json()["settings"] == before


def test_put_clamps_and_sanitizes_values(client: TestClient) -> None:
    # chat_max_turns clamped to [2, 64]. (wake_threshold is no longer here —
    # it is verified on the runtime_settings surface; see test_runtime_settings.py.)
    r = client.put(URL, json={"chat_max_turns": 999})
    assert r.status_code == 200
    assert r.json()["settings"]["chat_max_turns"] == 64

    r = client.put(URL, json={"chat_max_turns": -3})
    assert r.json()["settings"]["chat_max_turns"] == 2

    # Non-numeric values fall back to the previous value, not an error.
    r = client.put(URL, json={"chat_max_turns": "abc"})
    assert r.status_code == 200
    assert r.json()["settings"]["chat_max_turns"] == 2

    # Invalid tts_lang falls back to "auto".
    r = client.put(URL, json={"tts_lang": "de"})
    assert r.status_code == 200
    assert r.json()["settings"]["tts_lang"] == "auto"


def test_put_claude_model_persists_and_resolves(client: TestClient) -> None:
    r = client.put(URL, json={"claude_model": "claude-opus-4-7", "provider": "claude"})
    assert r.status_code == 200
    body = r.json()
    assert body["settings"]["claude_model"] == "claude-opus-4-7"
    assert body["active_claude_model_tag"] == "claude-opus-4-7"
    assert body["active_provider"] == "claude"

    # A bare alias (the CLI resolves to the newest version) is now valid and persisted.
    r = client.put(URL, json={"claude_model": "opus"})
    assert r.status_code == 200
    assert r.json()["settings"]["claude_model"] == "opus"
    assert r.json()["active_claude_model_tag"] == "opus"

    # Non-claude tag is rejected back to the previous value.
    r = client.put(URL, json={"claude_model": "gpt-4"})
    assert r.status_code == 200
    assert r.json()["settings"]["claude_model"] == "opus"


def test_put_invalid_provider_rejected_and_previous_kept(client: TestClient) -> None:
    # A valid provider persists.
    r = client.put(URL, json={"provider": "claude"})
    assert r.status_code == 200
    assert r.json()["active_provider"] == "claude"

    # An out-of-enum provider is rejected at the boundary (422) instead of silently
    # WIPING the configured provider — the previous selection is untouched (CTX-1).
    r = client.put(URL, json={"provider": "gpt4-typo"})
    assert r.status_code == 422
    err = r.json()["detail"]["error"]
    assert err["code"] == "VALIDATION"
    assert "provider" in err["fields"]

    r = client.get(URL)
    assert r.json()["active_provider"] == "claude"


def test_put_openai_model_rejects_foreign_tags(client: TestClient) -> None:
    # A real OpenAI o-series tag is accepted.
    r = client.put(URL, json={"openai_model": "o5-mini"})
    assert r.status_code == 200
    assert r.json()["settings"]["openai_model"] == "o5-mini"

    # Foreign tags that merely start with 'o' (claude alias, ollama namespace) must
    # be rejected back to the previous value, not accepted (CTX-3).
    for foreign in ("opus", "ollama-llama3"):
        r = client.put(URL, json={"openai_model": foreign})
        assert r.status_code == 200
        assert r.json()["settings"]["openai_model"] == "o5-mini"

    # A gpt- tag is still valid.
    r = client.put(URL, json={"openai_model": "gpt-5.4"})
    assert r.json()["settings"]["openai_model"] == "gpt-5.4"


def test_put_claude_full_tools_toggles(client: TestClient) -> None:
    # Turn off: full authority drops.
    r = client.put(URL, json={"claude_full_tools": False})
    assert r.status_code == 200
    body = r.json()
    assert body["settings"]["claude_full_tools"] is False
    assert body["active_claude_full_tools"] is False

    # Turn it back on.
    r = client.put(URL, json={"claude_full_tools": True})
    assert r.json()["settings"]["claude_full_tools"] is True
    assert r.json()["active_claude_full_tools"] is True


def test_put_legacy_composer2_fast_alias_normalized(client: TestClient) -> None:
    r = client.put(URL, json={"cursor_model": "composer-2-fast"})
    assert r.status_code == 200
    assert r.json()["settings"]["cursor_model"] == "composer-2"


def test_put_empty_cursor_model_keeps_previous(client: TestClient) -> None:
    """Quirk: '' is falsy in the merge — the model cannot be cleared via the API."""
    client.put(URL, json={"cursor_model": "kimi-k2.5"})
    r = client.put(URL, json={"cursor_model": ""})
    assert r.status_code == 200
    assert r.json()["settings"]["cursor_model"] == "kimi-k2.5"


def test_bearer_required_when_token_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "sekret-token")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    monkeypatch.setenv("CURSOR_MODEL", "composer-2")
    # The token gate applies only to PROXIED requests; X-Forwarded-For makes the
    # request look proxied so the configured token is actually enforced.
    proxied = {"X-Forwarded-For": "1.2.3.4"}
    with TestClient(create_app()) as c:
        assert c.get(URL, headers=proxied).status_code == 401
        assert c.put(URL, json={"tts_lang": "en"}, headers=proxied).status_code == 401
        ok = c.get(URL, headers={**proxied, "Authorization": "Bearer sekret-token"})
        assert ok.status_code == 200


# -- Ollama model listing + selection (feeds the switcher dropdown) -----------------

OLLAMA_URL = "/api/v1/system/ollama/models"


def test_ollama_models_endpoint_lists_when_reachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_list(self):
        return ["llama3.1:latest", "qwen2.5:7b"]

    monkeypatch.setattr("akana.driver.ollama.OllamaDriver.list_models", fake_list)
    body = client.get(OLLAMA_URL).json()
    assert body["reachable"] is True
    assert body["models"] == ["llama3.1:latest", "qwen2.5:7b"]
    assert body["active"] == "llama3.1"  # no selection yet → default


def test_ollama_models_endpoint_degrades_when_unreachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from akana.driver.base import DriverUnavailable

    async def boom(self):
        raise DriverUnavailable("down", kind="unavailable", retryable=True, provider="ollama")

    monkeypatch.setattr("akana.driver.ollama.OllamaDriver.list_models", boom)
    body = client.get(OLLAMA_URL).json()
    assert body["reachable"] is False  # NOT 500 — the UI shows 'Ollama not running'
    assert body["models"] == []


def test_put_ollama_model_persists_and_resolves(client: TestClient) -> None:
    r = client.put(URL, json={"ollama_model": "qwen2.5:7b"})
    assert r.status_code == 200
    body = r.json()
    assert body["settings"]["ollama_model"] == "qwen2.5:7b"
    assert body["active_ollama_model_tag"] == "qwen2.5:7b"


def test_put_gemini_model_persists_and_resolves(client: TestClient) -> None:
    r = client.put(URL, json={"gemini_model": "gemini-2.5-pro", "provider": "gemini"})
    assert r.status_code == 200
    body = r.json()
    assert body["settings"]["gemini_model"] == "gemini-2.5-pro"
    assert body["active_gemini_model_tag"] == "gemini-2.5-pro"
    assert body["active_provider"] == "gemini"

    # A foreign tag (cursor/claude/default) is not written to gemini_model → falls back to the previous one.
    r = client.put(URL, json={"gemini_model": "composer-2"})
    assert r.status_code == 200
    assert r.json()["settings"]["gemini_model"] == "gemini-2.5-pro"


def test_put_openai_model_persists_and_resolves(client: TestClient) -> None:
    # Exactly symmetric with gemini: openai_model is in _ALLOWED_KEYS → the PUT is accepted
    # and persisted, active_openai_model_tag is resolved, provider=openai is returned.
    r = client.put(URL, json={"openai_model": "gpt-5.4-mini", "provider": "openai"})
    assert r.status_code == 200
    body = r.json()
    assert body["settings"]["openai_model"] == "gpt-5.4-mini"
    assert body["active_openai_model_tag"] == "gpt-5.4-mini"
    assert body["active_provider"] == "openai"

    # An o-series reasoning name is also accepted as NATIVE (gpt-* | o*).
    r = client.put(URL, json={"openai_model": "o5-mini"})
    assert r.status_code == 200
    assert r.json()["settings"]["openai_model"] == "o5-mini"
    assert r.json()["active_openai_model_tag"] == "o5-mini"

    # A foreign tag (cursor/claude/gemini/default) is not written to openai_model → falls back to the previous one.
    r = client.put(URL, json={"openai_model": "composer-2"})
    assert r.status_code == 200
    assert r.json()["settings"]["openai_model"] == "o5-mini"
    r = client.put(URL, json={"openai_model": "gemini-2.5-pro"})
    assert r.status_code == 200
    assert r.json()["settings"]["openai_model"] == "o5-mini"

    # The PUT round-trip persists to disk (route _ALLOWED_KEYS gate).
    again = client.get(URL).json()
    assert again["settings"]["openai_model"] == "o5-mini"


# -- Cursor model listing + selection (feeds the switcher dropdown) ----------------

CURSOR_URL = "/api/v1/system/cursor/models"


def test_cursor_models_endpoint_lists_when_reachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(_settings, *, force_refresh=False):
        return {
            "reachable": True,
            "models": [
                {"value": "composer-2", "label": "Composer 2"},
                {"value": "gpt-5", "label": "GPT-5"},
            ],
            "active": "composer-2",
            "error": None,
            "source": "live",
        }

    monkeypatch.setattr(
        "akana_server.orchestrator.cursor_catalog.fetch_cursor_models", fake_fetch
    )
    body = client.get(CURSOR_URL).json()
    assert body["reachable"] is True
    assert body["models"][0]["value"] == "composer-2"
    assert body["active"] == "composer-2"
    assert body["source"] == "live"


def test_cursor_models_endpoint_degrades_when_unreachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(_settings, *, force_refresh=False):
        return {
            "reachable": False,
            "models": [{"value": "composer-2", "label": "Composer 2 (fallback)"}],
            "active": "composer-2",
            "error": "auth failed",
            "source": "static",
        }

    monkeypatch.setattr(
        "akana_server.orchestrator.cursor_catalog.fetch_cursor_models", fake_fetch
    )
    body = client.get(CURSOR_URL).json()
    assert body["reachable"] is False
    assert body["error"] == "auth failed"
    assert body["models"][0]["value"] == "composer-2"


# -- Claude model listing + selection (live /v1/models, symmetric with cursor) -----

CLAUDE_URL = "/api/v1/system/claude/models"


def test_claude_models_endpoint_lists_when_reachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(_settings, *, force_refresh=False):
        return {
            "reachable": True,
            "models": [
                {"value": "opus", "label": "Claude Opus (en yeni)"},
                {"value": "claude-opus-4-8", "label": "Claude Opus 4.8"},
            ],
            "active": "sonnet",
            "error": None,
            "source": "live",
            "cached": False,
        }

    monkeypatch.setattr(
        "akana_server.orchestrator.claude_catalog.fetch_claude_models", fake_fetch
    )
    body = client.get(CLAUDE_URL).json()
    assert body["reachable"] is True
    assert body["models"][0]["value"] == "opus"
    assert body["active"] == "sonnet"
    assert body["source"] == "live"


def test_claude_models_endpoint_degrades_when_unreachable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_fetch(_settings, *, force_refresh=False):
        return {
            "reachable": False,
            "models": [{"value": "sonnet", "label": "Claude Sonnet (fallback)"}],
            "active": "sonnet",
            "error": "Claude oturum token'ı geçersiz",
            "source": "static",
            "cached": False,
        }

    monkeypatch.setattr(
        "akana_server.orchestrator.claude_catalog.fetch_claude_models", fake_fetch
    )
    body = client.get(CLAUDE_URL).json()
    assert body["reachable"] is False  # NOT 500 — the UI shows the static fallback + a hint
    assert "geçersiz" in body["error"]
    assert body["models"][0]["value"] == "sonnet"
