"""MultimodalEngine F1 — image input (image_ids) binding in chat.

* claude + cursor provider → a `[Görsel: <absolute-path>]` block is added to the
  prompt (both agents read the path themselves — empirically verified),
* unknown id → 400 (`error.code=IMAGE_NOT_FOUND`).
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


def make_png(width: int = 2, height: int = 1) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xab\xcd\xef" * width for _ in range(height))
    body = (
        _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )
    return b"\x89PNG\r\n\x1a\n" + body


def _client_factory(monkeypatch: pytest.MonkeyPatch, tmp_path, *, provider: str):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "x")
    monkeypatch.setenv("AKANA_MEMORY_LLM_CAPTURE", "0")
    monkeypatch.setenv("LLM_PROVIDER", provider)
    return create_app()


def _mock_llm(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    prompts: list[str] = []

    async def fake_complete(settings, user_text, **kwargs):
        prompts.append(user_text)
        return "görseli inceledim.", {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "tool_calls": [],
        }

    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage", fake_complete
    )
    return prompts


def _upload_png(client: TestClient) -> str:
    r = client.post(
        "/api/v1/uploads", files={"file": ("foto.png", make_png(), "image/png")}
    )
    assert r.status_code == 200, r.text
    return r.json()["image"]["id"]


def test_claude_provider_injects_image_path_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    app = _client_factory(monkeypatch, tmp_path, provider="claude")
    with TestClient(app) as client:
        prompts = _mock_llm(monkeypatch)
        image_id = _upload_png(client)
        r = client.post(
            "/api/v1/chat",
            json={"text": "bu görselde ne var?", "image_ids": [image_id]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["text"] == "görseli inceledim."
        assert len(prompts) == 1
        # [Görsel: <absolute-path>] line — the claude CLI's Read tool reads the path.
        assert "[Görsel: " in prompts[0]
        assert str(tmp_path / "uploads") in prompts[0]
        assert prompts[0].rstrip().endswith("]")


def test_cursor_provider_injects_image_path_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # Empirically verified: the cursor SDK agent also reads the image from the
    # absolute path → native like claude (the old "cursor rejection" behavior was wrong).
    app = _client_factory(monkeypatch, tmp_path, provider="cursor")
    with TestClient(app) as client:
        prompts = _mock_llm(monkeypatch)
        image_id = _upload_png(client)
        r = client.post(
            "/api/v1/chat",
            json={"text": "bu görselde ne var?", "image_ids": [image_id]},
        )
        assert r.status_code == 200, r.text
        assert r.json()["text"] == "görseli inceledim."
        assert len(prompts) == 1
        # [Görsel: <absolute-path>] line — the cursor agent also reads the path itself.
        assert "[Görsel: " in prompts[0]
        assert str(tmp_path / "uploads") in prompts[0]


def test_unknown_image_id_returns_400(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    app = _client_factory(monkeypatch, tmp_path, provider="claude")
    with TestClient(app) as client:
        prompts = _mock_llm(monkeypatch)
        r = client.post(
            "/api/v1/chat",
            json={"text": "bu görselde ne var?", "image_ids": ["yok-boyle-id"]},
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"]["code"] == "IMAGE_NOT_FOUND"
        assert prompts == []


def test_unknown_image_id_returns_400_on_stream_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The SSE endpoint goes through the same gate — 400 before the stream starts."""
    app = _client_factory(monkeypatch, tmp_path, provider="claude")
    with TestClient(app) as client:
        r = client.post(
            "/api/v1/chat/stream",
            json={"text": "görseli anlat", "image_ids": ["yok-boyle-id"]},
        )
        assert r.status_code == 400
        assert r.json()["detail"]["error"]["code"] == "IMAGE_NOT_FOUND"


def test_image_ids_empty_list_is_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    app = _client_factory(monkeypatch, tmp_path, provider="cursor")
    with TestClient(app) as client:
        prompts = _mock_llm(monkeypatch)
        r = client.post("/api/v1/chat", json={"text": "selam", "image_ids": []})
        assert r.status_code == 200
        assert len(prompts) == 1
        assert "[Görsel:" not in prompts[0]
