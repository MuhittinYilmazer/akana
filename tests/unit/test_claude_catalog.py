"""Unit tests for the Claude model catalog (live ``/v1/models`` + cache)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from akana_server.orchestrator import claude_catalog
from akana_server.orchestrator.claude_catalog import (
    _options_from_api,
    fetch_claude_models,
    invalidate_claude_catalog_cache,
    probe_claude_api,
)


@pytest.fixture(autouse=True)
def _clear_catalog_cache() -> None:
    invalidate_claude_catalog_cache()


def test_options_from_api_aliases_first_dedupe_and_newest_first() -> None:
    raw = [
        {"id": "claude-sonnet-4-6", "display_name": "Claude Sonnet 4.6", "created_at": "2026-01-01"},
        {"id": "claude-sonnet-4-6", "display_name": "Dup"},  # dedupe
        {"id": "", "display_name": "Skip"},  # empty id skipped
        {"id": "gpt-4", "display_name": "Not Claude"},  # non-claude skipped
        {"id": "claude-opus-4-8", "display_name": "Claude Opus 4.8", "created_at": "2026-06-01"},
    ]
    out = _options_from_api(raw)
    # "always newest" aliases come first
    assert [m["value"] for m in out[:3]] == ["opus", "sonnet", "haiku"]
    concrete = [m["value"] for m in out[3:]]
    # created_at descending (newest model on top) + dedupe + drops non-claude
    assert concrete == ["claude-opus-4-8", "claude-sonnet-4-6"]
    labels = {m["value"]: m["label"] for m in out}
    assert labels["claude-opus-4-8"] == "Claude Opus 4.8"


def test_probe_cache_reuses_result_without_second_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    calls = {"n": 0}

    async def fake_http(_token):
        calls["n"] += 1
        return {"ok": True, "models": [{"id": "claude-opus-4-8", "display_name": "Opus 4.8"}]}

    settings = SimpleNamespace(data_dir=tmp_path)
    monkeypatch.setattr(claude_catalog, "_claude_oauth_token", lambda _s: "tok-abc")
    monkeypatch.setattr(claude_catalog, "_fetch_models_http", fake_http)

    async def run() -> None:
        await probe_claude_api(settings)
        body = await probe_claude_api(settings)
        assert calls["n"] == 1  # second call from cache
        assert body["reachable"] is True
        assert body["model_count"] == 1
        assert body["token_set"] is True

    asyncio.run(run())


def test_probe_force_refresh_bypasses_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    calls = {"n": 0}

    async def fake_http(_token):
        calls["n"] += 1
        return {"ok": True, "models": [{"id": "claude-opus-4-8", "display_name": "Opus 4.8"}]}

    settings = SimpleNamespace(data_dir=tmp_path)
    monkeypatch.setattr(claude_catalog, "_claude_oauth_token", lambda _s: "tok-abc")
    monkeypatch.setattr(claude_catalog, "_fetch_models_http", fake_http)

    async def run() -> None:
        await probe_claude_api(settings)
        await probe_claude_api(settings, force_refresh=True)
        assert calls["n"] == 2

    asyncio.run(run())


def test_no_token_returns_static_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    settings = SimpleNamespace(data_dir=tmp_path)
    monkeypatch.setattr(claude_catalog, "_claude_oauth_token", lambda _s: "")
    # llm-settings reads are also called on the no-token path → isolate them.
    import akana_server.llm_settings as llmmod

    monkeypatch.setattr(llmmod, "load_llm_settings", lambda _d, _s: SimpleNamespace())
    monkeypatch.setattr(llmmod, "resolve_claude_model_tag", lambda _s, _l: "sonnet")

    async def run() -> None:
        body = await fetch_claude_models(settings)
        assert body["reachable"] is False
        assert body["source"] == "static"
        assert body["active"] == "sonnet"
        # static fallback list is not empty (offline selection is possible)
        assert any(m["value"] == "sonnet" for m in body["models"])

    asyncio.run(run())


def test_no_session_probe_error_is_english(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The no-session probe error must be ENGLISH (English-first UI). It was a hardcoded
    Turkish string ('Claude oturumu yok…') shown even to English-mode users and surfaced
    verbatim in the onboarding/settings connection banner (i18n leak)."""
    monkeypatch.setattr(claude_catalog, "_claude_oauth_token", lambda _s: "")
    res = asyncio.run(probe_claude_api(SimpleNamespace(data_dir=tmp_path)))
    assert res["token_set"] is False
    assert res["reachable"] is False
    err = res["error"] or ""
    assert "oturumu yok" not in err  # no leftover Turkish
    assert "claude login" in err  # actionable English hint


def test_unreachable_api_falls_back_to_static(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = SimpleNamespace(data_dir=tmp_path)
    monkeypatch.setattr(claude_catalog, "_claude_oauth_token", lambda _s: "tok-stale")

    async def fake_http(_token):
        return {"ok": False, "error": "Anthropic API 401: invalid"}

    monkeypatch.setattr(claude_catalog, "_fetch_models_http", fake_http)
    import akana_server.llm_settings as llmmod

    monkeypatch.setattr(llmmod, "load_llm_settings", lambda _d, _s: SimpleNamespace())
    monkeypatch.setattr(llmmod, "resolve_claude_model_tag", lambda _s, _l: "opus")

    async def run() -> None:
        body = await fetch_claude_models(settings)
        assert body["reachable"] is False
        assert body["source"] == "static"
        assert "401" in (body["error"] or "")
        assert len(body["models"]) > 0  # static fallback

    asyncio.run(run())
