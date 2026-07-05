"""Gemini live model catalog — google-genai ``models.list`` → UI list.

Hermetic: no real Google API. ``make_client`` is patched with a fake client (async
pager); the absence of the SDK/key is also simulated. The filter (gemini-* +
generateContent, excluding embedding/tts) + static fallback + live path are covered."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from akana_server.orchestrator import gemini_catalog as gc


def _settings(tmp_path):
    return SimpleNamespace(data_dir=tmp_path, cursor_model="composer-2", gemini_model="")


class _FakeModel:
    def __init__(self, name, actions=None, display=""):
        self.name = name
        self.supported_actions = actions
        self.display_name = display


class _FakePager:
    def __init__(self, models):
        self._models = models

    def __aiter__(self):
        async def _gen():
            for m in self._models:
                yield m

        return _gen()


class _FakeAioModels:
    def __init__(self, models):
        self._models = models

    async def list(self):
        return _FakePager(self._models)


class _FakeClient:
    def __init__(self, models):
        self.aio = SimpleNamespace(models=_FakeAioModels(models))


def _patch_available(monkeypatch, models):
    monkeypatch.setattr(gc, "genai_installed", lambda: True)
    monkeypatch.setattr(gc, "resolve_api_key", lambda settings: "k-abc123")
    monkeypatch.setattr(gc, "make_client", lambda settings, **_kw: _FakeClient(models))
    gc.invalidate_gemini_catalog_cache()


# --- Saf filtre ------------------------------------------------------------


def test_is_chat_model_filter() -> None:
    assert gc._is_chat_model("gemini-3-pro", ["generateContent"]) is True
    assert gc._is_chat_model("gemini-2.5-flash", None) is True  # no action → name-based
    assert gc._is_chat_model("text-embedding-004", ["embedContent"]) is False  # not gemini-
    assert gc._is_chat_model("gemini-embedding-001", ["embedContent"]) is False  # name filters out
    assert gc._is_chat_model("gemini-2.0-flash-tts", ["generateContent"]) is False  # tts name filters out
    assert gc._is_chat_model("gemini-2.5-flash-image", ["generateContent"]) is False  # image filters out


def test_options_from_models_filters_and_sorts() -> None:
    models = [
        _FakeModel("models/gemini-2.5-flash", ["generateContent"], "Gemini 2.5 Flash"),
        _FakeModel("models/gemini-3-pro-preview", ["generateContent"], "Gemini 3 Pro"),
        _FakeModel("models/text-embedding-004", ["embedContent"]),
        _FakeModel("models/gemini-2.0-flash-tts", ["generateContent"]),
    ]
    opts = gc._options_from_models(models)
    vals = [o["value"] for o in opts]
    assert vals == ["gemini-3-pro-preview", "gemini-2.5-flash"]  # descending order, tts/embed excluded
    assert opts[0]["label"] == "Gemini 3 Pro"  # display_name
    assert opts[1]["label"] == "Gemini 2.5 Flash"


# --- fetch_gemini_models ---------------------------------------------------


def test_fetch_live_models(tmp_path, monkeypatch) -> None:
    _patch_available(
        monkeypatch,
        [
            _FakeModel("models/gemini-3-pro-preview", ["generateContent"], "Gemini 3 Pro"),
            _FakeModel("models/gemini-2.5-flash", ["generateContent"]),
            _FakeModel("models/embedding-001", ["embedContent"]),
        ],
    )
    res = asyncio.run(gc.fetch_gemini_models(_settings(tmp_path)))
    assert res["reachable"] is True
    assert res["source"] == "live"
    vals = [m["value"] for m in res["models"]]
    assert "gemini-3-pro-preview" in vals and "gemini-2.5-flash" in vals
    assert "embedding-001" not in vals


def test_fetch_sdk_missing_falls_back_static(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(gc, "genai_installed", lambda: False)
    gc.invalidate_gemini_catalog_cache()
    res = asyncio.run(gc.fetch_gemini_models(_settings(tmp_path)))
    assert res["reachable"] is False
    assert res["source"] == "static"
    assert res["models"]  # static fallback is populated
    assert "not installed" in res["error"]


def test_fetch_no_key_falls_back_static(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(gc, "genai_installed", lambda: True)
    monkeypatch.setattr(gc, "resolve_api_key", lambda settings: None)
    gc.invalidate_gemini_catalog_cache()
    res = asyncio.run(gc.fetch_gemini_models(_settings(tmp_path)))
    assert res["reachable"] is False
    assert res["source"] == "static"
    assert "api key" in res["error"].lower()


def test_fetch_api_error_falls_back_static(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(gc, "genai_installed", lambda: True)
    monkeypatch.setattr(gc, "resolve_api_key", lambda settings: "k-abc123")

    class _BoomModels:
        async def list(self):
            raise RuntimeError("403 permission denied")

    monkeypatch.setattr(
        gc, "make_client", lambda settings, **_kw: SimpleNamespace(aio=SimpleNamespace(models=_BoomModels()))
    )
    gc.invalidate_gemini_catalog_cache()
    res = asyncio.run(gc.fetch_gemini_models(_settings(tmp_path)))
    assert res["reachable"] is False
    assert res["source"] == "static"
    assert res["models"]  # the fallback is still selectable


# --- probe_gemini_api ------------------------------------------------------


def test_probe_sdk_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(gc, "genai_installed", lambda: False)
    gc.invalidate_gemini_catalog_cache()
    res = asyncio.run(gc.probe_gemini_api(_settings(tmp_path)))
    assert res["key_set"] is False
    assert res["reachable"] is False
    assert res["model_count"] == 0
    assert "not installed" in res["error"]


def test_probe_sdk_missing_with_key_reports_key_set(tmp_path, monkeypatch) -> None:
    """SDK absent but a key IS present → key_set stays TRUE (truthful) with an actionable
    'add gemini' error. Regression: it used to force key_set=False, so after pasting a
    valid key the onboarding read 'needs a key' and looped the user back to re-enter the
    key that was already stored."""
    monkeypatch.setattr(gc, "genai_installed", lambda: False)
    monkeypatch.setattr(gc, "resolve_api_key", lambda settings: "k-abc123")
    gc.invalidate_gemini_catalog_cache()
    res = asyncio.run(gc.probe_gemini_api(_settings(tmp_path)))
    assert res["key_set"] is True
    assert res["reachable"] is False
    assert "not installed" in res["error"]
    assert "add gemini" in res["error"]  # actionable: tells the user how to fix it


def test_probe_no_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(gc, "genai_installed", lambda: True)
    monkeypatch.setattr(gc, "resolve_api_key", lambda settings: None)
    gc.invalidate_gemini_catalog_cache()
    res = asyncio.run(gc.probe_gemini_api(_settings(tmp_path)))
    assert res["key_set"] is False
    assert res["reachable"] is False
    assert res["model_count"] == 0


def test_probe_reachable_counts_live(tmp_path, monkeypatch) -> None:
    _patch_available(
        monkeypatch,
        [
            _FakeModel("models/gemini-3-pro-preview", ["generateContent"], "Gemini 3 Pro"),
            _FakeModel("models/gemini-2.5-flash", ["generateContent"]),
            _FakeModel("models/embedding-001", ["embedContent"]),  # not counted
        ],
    )
    res = asyncio.run(gc.probe_gemini_api(_settings(tmp_path)))
    assert res["key_set"] is True
    assert res["reachable"] is True
    assert res["error"] is None
    assert res["model_count"] == 2  # only two live chat models


def test_probe_api_error(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(gc, "genai_installed", lambda: True)
    monkeypatch.setattr(gc, "resolve_api_key", lambda settings: "k-abc123")

    class _BoomModels:
        async def list(self):
            raise RuntimeError("403 permission denied")

    monkeypatch.setattr(
        gc, "make_client", lambda settings, **_kw: SimpleNamespace(aio=SimpleNamespace(models=_BoomModels()))
    )
    gc.invalidate_gemini_catalog_cache()
    res = asyncio.run(gc.probe_gemini_api(_settings(tmp_path)))
    assert res["key_set"] is True
    assert res["reachable"] is False
    assert res["model_count"] == 0
    assert res["error"]
