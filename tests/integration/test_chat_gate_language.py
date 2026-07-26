"""Language contract on the file gate (``chat/gates.py``).

Two DIFFERENT audiences share this module and they do not get the same rule:

* the "the provider can't read this file" reply is Akana ANSWERING THE USER — it is
  persisted as the assistant turn and rendered in the bubble, so it must follow the
  ``language`` runtime setting like every other assistant-visible string (English default,
  Turkish only on explicit choice);
* the ``[Image:/File: <path>]`` attachment line is prompt scaffolding the MODEL reads —
  it must be STABLE and English regardless of the setting, because a foreign-language
  token in the prompt is exactly what nudges the reply into the wrong language.
"""

from __future__ import annotations

import struct
import zlib

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app


def _png_chunk(ctype: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + ctype
        + data
        + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
    )


def _png(width: int = 2, height: int = 1) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xab\xcd\xef" * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + (
        _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )


def _app(monkeypatch: pytest.MonkeyPatch, tmp_path, *, provider: str, lang: str):
    from akana_server.runtime_settings import reset_runtime_stores

    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "x")
    monkeypatch.setenv("AKANA_MEMORY_LLM_CAPTURE", "0")
    monkeypatch.setenv("AKANA_LLM_CHAT_TITLES", "0")
    monkeypatch.setenv("LLM_PROVIDER", provider)
    monkeypatch.setenv("AKANA_LANGUAGE", lang)
    reset_runtime_stores()
    return create_app()


def _mock_llm(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    prompts: list[str] = []

    async def fake_complete(settings, user_text, **kwargs):
        prompts.append(user_text)
        return "ok.", {"prompt_tokens": 1, "completion_tokens": 1, "tool_calls": []}

    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage", fake_complete
    )
    return prompts


def _upload(client: TestClient, name: str, data: bytes, ctype: str) -> str:
    r = client.post("/api/v1/uploads", files={"file": (name, data, ctype)})
    assert r.status_code == 200, r.text
    return r.json()["image"]["id"]


# -- the user-facing rejection follows the language setting ------------------------


def test_unsupported_file_rejection_is_turkish_in_turkish_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """ollama reads no attachment → the gate answers the user BEFORE the LLM. In Turkish
    mode that reply used to be the only English paragraph in the whole conversation."""
    app = _app(monkeypatch, tmp_path, provider="ollama", lang="tr")
    with TestClient(app) as client:
        _mock_llm(monkeypatch)
        fid = _upload(client, "notlar.txt", b"merhaba dunya\n", "text/plain")
        r = client.post(
            "/api/v1/chat", json={"text": "bu dosyada ne var?", "file_ids": [fid]}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["action"] == "file_unsupported"
        text = body["text"]
        assert "I can't process the files" not in text
        assert "switch to the claude provider" not in text
        assert "sağlayıcısı" in text and "Ayarlar" in text, text
        assert "metin dosyası" in text, text  # the kind label is localized too


def test_unsupported_image_rejection_is_english_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """English is the DEFAULT everywhere — an unset language must not fall to Turkish."""
    app = _app(monkeypatch, tmp_path, provider="ollama", lang="en")
    with TestClient(app) as client:
        _mock_llm(monkeypatch)
        fid = _upload(client, "foto.png", _png(), "image/png")
        r = client.post(
            "/api/v1/chat", json={"text": "what is this?", "image_ids": [fid]}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["action"] == "image_unsupported"
        assert "image input" in body["text"]
        assert "Settings" in body["text"]


# -- the model-facing attachment label is stable English ---------------------------


def test_attachment_label_is_english_in_english_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    app = _app(monkeypatch, tmp_path, provider="claude", lang="en")
    with TestClient(app) as client:
        prompts = _mock_llm(monkeypatch)
        img = _upload(client, "foto.png", _png(), "image/png")
        txt = _upload(client, "notes.txt", b"hello world\n", "text/plain")
        r = client.post(
            "/api/v1/chat",
            json={"text": "what is in these?", "file_ids": [img, txt]},
        )
        assert r.status_code == 200, r.text
        assert "[Görsel:" not in prompts[0], "Turkish token injected into an English prompt"
        assert "[Dosya:" not in prompts[0]
        assert "[Image: " in prompts[0]
        assert "[File: " in prompts[0]


def test_attachment_label_stays_english_in_turkish_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Prompt scaffolding is STABLE: the marker does not change shape with the setting,
    so nothing downstream has to parse two spellings of the same line."""
    app = _app(monkeypatch, tmp_path, provider="claude", lang="tr")
    with TestClient(app) as client:
        prompts = _mock_llm(monkeypatch)
        img = _upload(client, "foto.png", _png(), "image/png")
        r = client.post(
            "/api/v1/chat", json={"text": "bu görselde ne var?", "image_ids": [img]}
        )
        assert r.status_code == 200, r.text
        assert "[Image: " in prompts[0]
        assert "[Görsel:" not in prompts[0]
