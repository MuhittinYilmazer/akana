"""OpenAI live model catalog — ``GET /v1/models`` → UI list.

Hermetic: no real OpenAI API. ``httpx.AsyncClient`` is patched with a fake client
(``.get`` returns a fixed response/error); a missing key is also simulated. Covers the
filter (gpt-*/chatgpt-*/o-series; excluding embedding/whisper/tts/dall-e/moderation/
realtime/instruct) + ``created`` descending sort + static fallback + the live path.
The OpenAI twin of the ``gemini_catalog`` test (httpx mock instead of the SDK)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx

from akana_server.orchestrator import openai_catalog as oc


def _settings(tmp_path):
    return SimpleNamespace(data_dir=tmp_path, cursor_model="composer-2", openai_model="")


# --- Fake httpx client -----------------------------------------------------


class _FakeResp:
    def __init__(self, *, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


class _FakeAsyncClient:
    def __init__(self, *, resp=None, exc=None):
        self._resp = resp
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get(self, _url, headers=None):  # noqa: ANN001
        if self._exc is not None:
            raise self._exc
        return self._resp


def _patch_httpx(monkeypatch, *, resp=None, exc=None, key="sk-test123"):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **_kw: _FakeAsyncClient(resp=resp, exc=exc))
    monkeypatch.setattr(oc, "resolve_openai_key", lambda settings: key)
    oc.invalidate_openai_catalog_cache()


def _models_payload(ids_with_created):
    return {"object": "list", "data": [{"id": i, "created": c} for i, c in ids_with_created]}


# --- Pure filter -----------------------------------------------------------


def test_is_chat_model_filter() -> None:
    assert oc._is_chat_model("gpt-5.4") is True
    assert oc._is_chat_model("gpt-4o") is True
    assert oc._is_chat_model("o5-mini") is True  # o-series
    assert oc._is_chat_model("chatgpt-4o-latest") is True
    assert oc._is_chat_model("text-embedding-3-large") is False  # filters out embedding
    assert oc._is_chat_model("whisper-1") is False
    assert oc._is_chat_model("dall-e-3") is False
    assert oc._is_chat_model("gpt-4o-audio-preview") is False  # filters out audio
    assert oc._is_chat_model("gpt-4o-realtime-preview") is False  # filters out realtime (voice)
    assert oc._is_chat_model("gpt-3.5-turbo-instruct") is False  # filters out instruct (completions)
    assert oc._is_chat_model("omni-moderation-latest") is False


def test_options_from_models_filters_and_sorts() -> None:
    models = [
        {"id": "gpt-4o", "created": 100},
        {"id": "gpt-5.4", "created": 300},
        {"id": "o5-mini", "created": 200},
        {"id": "text-embedding-3-large", "created": 400},  # filtered out
        {"id": "gpt-4o", "created": 100},  # duplicate → dedup
    ]
    opts = oc._options_from_models(models)
    vals = [o["value"] for o in opts]
    assert vals == ["gpt-5.4", "o5-mini", "gpt-4o"]  # created descending, embed excluded, unique
    assert opts[0]["label"] == "gpt-5.4"  # label = id (OpenAI gives no display_name)


# --- fetch_openai_models ---------------------------------------------------


def test_fetch_live_models(tmp_path, monkeypatch) -> None:
    _patch_httpx(
        monkeypatch,
        resp=_FakeResp(
            payload=_models_payload(
                [("gpt-5.4", 300), ("gpt-4o", 100), ("text-embedding-3-large", 400)]
            )
        ),
    )
    res = asyncio.run(oc.fetch_openai_models(_settings(tmp_path)))
    assert res["reachable"] is True
    assert res["source"] == "live"
    vals = [m["value"] for m in res["models"]]
    assert "gpt-5.4" in vals and "gpt-4o" in vals
    assert "text-embedding-3-large" not in vals


def test_fetch_no_key_falls_back_static(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(oc, "resolve_openai_key", lambda settings: None)
    oc.invalidate_openai_catalog_cache()
    res = asyncio.run(oc.fetch_openai_models(_settings(tmp_path)))
    assert res["reachable"] is False
    assert res["source"] == "static"
    assert res["models"]  # static fallback is populated
    assert "api key" in res["error"].lower()


def test_fetch_http_error_falls_back_static(tmp_path, monkeypatch) -> None:
    """401/transport error → reachable False + static fallback (never 500)."""
    _patch_httpx(monkeypatch, resp=_FakeResp(status_code=401, text="invalid api key"))
    res = asyncio.run(oc.fetch_openai_models(_settings(tmp_path)))
    assert res["reachable"] is False
    assert res["source"] == "static"
    assert res["models"]  # the fallback is still selectable


def test_fetch_transport_exception_falls_back_static(tmp_path, monkeypatch) -> None:
    _patch_httpx(monkeypatch, exc=RuntimeError("connection refused"))
    res = asyncio.run(oc.fetch_openai_models(_settings(tmp_path)))
    assert res["reachable"] is False
    assert res["source"] == "static"
    assert res["models"]


# --- probe_openai_api ------------------------------------------------------


def test_probe_no_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(oc, "resolve_openai_key", lambda settings: None)
    oc.invalidate_openai_catalog_cache()
    res = asyncio.run(oc.probe_openai_api(_settings(tmp_path)))
    assert res["key_set"] is False
    assert res["reachable"] is False
    assert res["model_count"] == 0


def test_probe_reachable_counts_live(tmp_path, monkeypatch) -> None:
    _patch_httpx(
        monkeypatch,
        resp=_FakeResp(
            payload=_models_payload(
                [("gpt-5.4", 300), ("gpt-4o", 100), ("text-embedding-3-large", 400)]
            )
        ),
    )
    res = asyncio.run(oc.probe_openai_api(_settings(tmp_path)))
    assert res["key_set"] is True
    assert res["reachable"] is True
    assert res["error"] is None
    assert res["model_count"] == 2  # only two live chat models (embed doesn't count)


def test_probe_api_error(tmp_path, monkeypatch) -> None:
    _patch_httpx(monkeypatch, resp=_FakeResp(status_code=500, text="server error"))
    res = asyncio.run(oc.probe_openai_api(_settings(tmp_path)))
    assert res["key_set"] is True
    assert res["reachable"] is False
    assert res["model_count"] == 0
    assert res["error"]
