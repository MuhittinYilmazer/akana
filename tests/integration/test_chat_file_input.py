"""MultimodalEngine PHASE2 — multi-type file attachment in chat (file_ids).

* claude + cursor → provider-native [Dosya: <path>] / [Görsel: <path>] block
  (both agents read the path themselves — empirically verified),
* file_ids ↔ image_ids alias union (effective_file_ids).
"""

from __future__ import annotations


import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app


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
        return "dosyayı inceledim.", {
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "tool_calls": [],
        }

    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage", fake_complete
    )
    return prompts


def _upload_text(client: TestClient, name: str = "notlar.txt") -> str:
    r = client.post(
        "/api/v1/uploads",
        files={"file": (name, b"merhaba dunya\n", "text/plain")},
    )
    assert r.status_code == 200, r.text
    return r.json()["image"]["id"]


def test_claude_injects_file_path_block_for_text_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    app = _client_factory(monkeypatch, tmp_path, provider="claude")
    with TestClient(app) as client:
        prompts = _mock_llm(monkeypatch)
        fid = _upload_text(client)
        r = client.post(
            "/api/v1/chat",
            json={"text": "bu dosyada ne yazıyor?", "file_ids": [fid]},
        )
        assert r.status_code == 200, r.text
        assert len(prompts) == 1
        # text file → [Dosya: <absolute-path>] (claude reads it via Read).
        assert "[Dosya: " in prompts[0]
        assert str(tmp_path / "uploads") in prompts[0]


def test_cursor_injects_file_path_block_for_text_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # Empirically verified: the cursor SDK agent also reads the file from its
    # absolute path → native like claude (the old "cursor rejects" behavior was wrong).
    app = _client_factory(monkeypatch, tmp_path, provider="cursor")
    with TestClient(app) as client:
        prompts = _mock_llm(monkeypatch)
        fid = _upload_text(client)
        r = client.post(
            "/api/v1/chat",
            json={"text": "bu dosyayı oku", "file_ids": [fid]},
        )
        assert r.status_code == 200, r.text
        assert len(prompts) == 1
        # text file → [Dosya: <absolute-path>] (the cursor agent reads the path too).
        assert "[Dosya: " in prompts[0]
        assert str(tmp_path / "uploads") in prompts[0]


def test_file_ids_alias_merges_with_image_ids(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """file_ids ↔ image_ids merge (deduplicated, order preserved) → one file block."""
    app = _client_factory(monkeypatch, tmp_path, provider="claude")
    with TestClient(app) as client:
        prompts = _mock_llm(monkeypatch)
        fid = _upload_text(client)
        # same id in both file_ids and image_ids → must appear only once.
        r = client.post(
            "/api/v1/chat",
            json={"text": "oku", "file_ids": [fid], "image_ids": [fid]},
        )
        assert r.status_code == 200, r.text
        assert prompts[0].count("[Dosya: ") == 1


def test_unknown_file_id_returns_400(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    app = _client_factory(monkeypatch, tmp_path, provider="claude")
    with TestClient(app) as client:
        _mock_llm(monkeypatch)
        r = client.post(
            "/api/v1/chat",
            json={"text": "oku", "file_ids": ["yok-boyle-id"]},
        )
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["error"]["code"] == "IMAGE_NOT_FOUND"


def test_empty_file_ids_is_noop(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    app = _client_factory(monkeypatch, tmp_path, provider="claude")
    with TestClient(app) as client:
        prompts = _mock_llm(monkeypatch)
        r = client.post("/api/v1/chat", json={"text": "selam", "file_ids": []})
        assert r.status_code == 200
        assert "[Dosya:" not in prompts[0]
