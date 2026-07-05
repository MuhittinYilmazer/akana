"""No provider is privileged as a default.

An unset/invalid ``LLM_PROVIDER`` resolves to "" ("unconfigured"); chat then
refuses with a clear, actionable message instead of silently falling back to the
cursor path. These tests are sync (``asyncio.run`` for the async dispatch surface)
so they run under the canonical autoload-off runner (``python akana.py test``).
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app
from akana_server.config import load_settings
from akana_server.llm_settings import LlmSettings, resolve_provider
from akana_server.orchestrator import llm_dispatch


def _settings(provider: str):
    # Settings is frozen+slots → build a real one and swap only llm_provider.
    return dataclasses.replace(load_settings(), llm_provider=provider)


# -- resolve_provider: the single source of truth for "which provider" -------------


def test_resolve_provider_empty_when_unconfigured() -> None:
    assert resolve_provider(_settings(""), LlmSettings(provider="")) == ""


def test_resolve_provider_empty_for_invalid_value() -> None:
    # An out-of-set value is treated as unconfigured, never coerced to a default.
    assert resolve_provider(_settings(""), LlmSettings(provider="not-a-provider")) == ""


def test_resolve_provider_uses_env_setting() -> None:
    assert resolve_provider(_settings("claude"), LlmSettings(provider="")) == "claude"


def test_resolve_provider_persisted_wins_over_env() -> None:
    assert resolve_provider(_settings("cursor"), LlmSettings(provider="gemini")) == "gemini"


# -- dispatch guard: unconfigured → fail fast (no silent cursor fallback) -----------


def test_complete_chat_raises_when_no_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_settings()
    monkeypatch.setattr(llm_dispatch, "_active_provider", lambda *_a, **_k: "")
    with pytest.raises(llm_dispatch.LLMCallError) as ei:
        asyncio.run(llm_dispatch.complete_chat(settings, "merhaba"))
    assert ei.value.status_code == 503
    assert ei.value.message == llm_dispatch.NO_PROVIDER_CONFIGURED_MSG


def test_stream_user_chat_raises_when_no_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load_settings()
    monkeypatch.setattr(llm_dispatch, "_active_provider", lambda *_a, **_k: "")

    async def _drain() -> None:
        async for _ev in llm_dispatch.stream_user_chat(settings, "merhaba"):
            pass

    with pytest.raises(llm_dispatch.LLMCallError) as ei:
        asyncio.run(_drain())
    assert ei.value.status_code == 503
    assert ei.value.message == llm_dispatch.NO_PROVIDER_CONFIGURED_MSG


# -- API surface: an unconfigured server reports "" (not a default) -----------------


def test_active_provider_empty_over_api_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    # Override the suite-wide hermetic pin (conftest sets "cursor") → unconfigured.
    monkeypatch.setenv("LLM_PROVIDER", "")
    with TestClient(create_app()) as c:
        body = c.get("/api/v1/system/llm-settings").json()
        assert body["settings"]["provider"] == ""
        assert body["active_provider"] == ""
