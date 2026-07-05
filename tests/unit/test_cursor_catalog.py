"""Unit tests for Cursor model catalog bridge helpers."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from akana_server.orchestrator import cursor_catalog
from akana_server.orchestrator.cursor_catalog import (
    _options_from_sdk,
    invalidate_cursor_catalog_cache,
    probe_cursor_api,
)


def test_options_from_sdk_maps_display_name_and_dedupes() -> None:
    raw = [
        {"id": "composer-2", "displayName": "Composer 2", "description": "Fast agent"},
        {"id": "composer-2", "displayName": "Dup"},
        {"id": "", "displayName": "Skip"},
        {"id": "gpt-5", "displayName": "GPT-5"},
    ]
    out = _options_from_sdk(raw)
    assert [m["value"] for m in out] == ["composer-2", "gpt-5"]
    assert out[0]["label"].startswith("Composer 2 — Fast agent")


@pytest.fixture(autouse=True)
def _clear_catalog_cache() -> None:
    invalidate_cursor_catalog_cache()


def test_bridge_cache_reuses_result_without_second_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    calls = {"n": 0}

    async def fake_bridge(_settings):
        calls["n"] += 1
        return {"ok": True, "models": [{"id": "composer-2", "displayName": "Composer 2"}]}

    settings = SimpleNamespace(data_dir=tmp_path, cursor_api_key="test-key-abc")
    monkeypatch.setattr(cursor_catalog, "runtime_cursor_key", lambda _s: "test-key-abc")
    monkeypatch.setattr(cursor_catalog, "_run_list_models_bridge", fake_bridge)

    async def run() -> None:
        await probe_cursor_api(settings)
        await probe_cursor_api(settings)
        assert calls["n"] == 1

        body = await probe_cursor_api(settings)
        assert body["reachable"] is True
        assert body["model_count"] == 1

    asyncio.run(run())


def test_bridge_cache_force_refresh_bypasses_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    calls = {"n": 0}

    async def fake_bridge(_settings):
        calls["n"] += 1
        return {"ok": True, "models": [{"id": "composer-2", "displayName": "Composer 2"}]}

    settings = SimpleNamespace(data_dir=tmp_path, cursor_api_key="test-key-abc")
    monkeypatch.setattr(cursor_catalog, "runtime_cursor_key", lambda _s: "test-key-abc")
    monkeypatch.setattr(cursor_catalog, "_run_list_models_bridge", fake_bridge)

    async def run() -> None:
        await probe_cursor_api(settings)
        await probe_cursor_api(settings, force_refresh=True)
        assert calls["n"] == 2

    asyncio.run(run())


def test_runtime_cursor_key_rejects_env_placeholder(tmp_path) -> None:
    """The shipped .env.example placeholder (CURSOR_API_KEY=your-cursor-api-key-here)
    must resolve to NO key — not a bogus set-but-invalid key. Otherwise probe_cursor_api
    reports key_set:true, the first-run onboarding banner reads 'key saved but not
    reachable', and chat 401s on a placeholder bearer (fresh-install trap)."""
    from akana_server.orchestrator.cursor_provider import runtime_cursor_key as _runtime_cursor_key

    placeholder = SimpleNamespace(data_dir=tmp_path, cursor_api_key="your-cursor-api-key-here")
    assert _runtime_cursor_key(placeholder) == ""
    assert _runtime_cursor_key(SimpleNamespace(data_dir=tmp_path, cursor_api_key="")) == ""
    # A real key passes through untouched (is_real_secret only rejects junk/placeholders).
    real = SimpleNamespace(data_dir=tmp_path, cursor_api_key="key_realABCD1234")
    assert _runtime_cursor_key(real) == "key_realABCD1234"


def test_probe_placeholder_key_reports_unset(tmp_path) -> None:
    """End-to-end: a placeholder-only .env → probe_cursor_api reports key_set:false
    (consistent with the credentials endpoint) WITHOUT spawning the Node bridge."""
    settings = SimpleNamespace(data_dir=tmp_path, cursor_api_key="your-cursor-api-key-here")
    res = asyncio.run(probe_cursor_api(settings))
    assert res["key_set"] is False
    assert res["reachable"] is False


def test_friendly_bridge_error_maps_missing_sdk() -> None:
    """A raw Node ERR_MODULE_NOT_FOUND (@cursor/sdk absent — cursor picked without
    `add cursor`) → actionable 'add cursor' hint, so the first-run banner shows guidance
    instead of a cryptic Node stack trace. Genuine auth errors pass through unchanged."""
    from akana_server.orchestrator.cursor_catalog import _friendly_bridge_error

    raw = (
        "node:internal/errors:490\n  Error [ERR_MODULE_NOT_FOUND]: Cannot find package "
        "'@cursor/sdk' imported from .../cursor_bridge/list_models.mjs"
    )
    assert _friendly_bridge_error(raw) == "Cursor bridge not installed — run: python akana.py add cursor"
    assert _friendly_bridge_error("Invalid User API Key") == "Invalid User API Key"
