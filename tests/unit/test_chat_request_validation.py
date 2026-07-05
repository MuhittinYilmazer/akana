"""POST /chat request boundary-value validation (QUALITY turn).

Input validation at the system boundary: empty/whitespace-only text, overlong text,
invalid thinking_mode, more than 30 image ids, overlong conversation_id,
attachment-only message (empty text + file_ids — EC3).
Whitespace-only text used to leak through to the LLM (the research route rejected it
with 422 while /chat accepted it — an inconsistency); now /chat also returns 422.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app

CHAT_URL = "/api/v1/chat"
STREAM_URL = "/api/v1/chat/stream"

#: Whitespace-only inputs — ``str.strip()`` trims them all (including NBSP \xa0).
_WHITESPACE_ONLY = ["   ", "\t", "\n\n", " \xa0 "]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_empty_text_rejected_422(client: TestClient) -> None:
    assert client.post(CHAT_URL, json={"text": ""}).status_code == 422


@pytest.mark.parametrize("text", _WHITESPACE_ONLY)
def test_whitespace_only_text_rejected_422(client: TestClient, text: str) -> None:
    """Whitespace-only text must not leak to the LLM — 422 (consistent with research)."""
    assert client.post(CHAT_URL, json={"text": text}).status_code == 422
    assert client.post(STREAM_URL, json={"text": text}).status_code == 422


def test_overlong_text_rejected_422(client: TestClient) -> None:
    assert client.post(CHAT_URL, json={"text": "a" * 32001}).status_code == 422


def test_text_at_max_length_passes_validation(client: TestClient) -> None:
    """Exactly 32000 chars (upper bound) PASSES validation (not 422).

    The LLM key is empty in the test → 503; but that is a runtime limit, not
    validation. What matters is that it is NOT 422."""
    r = client.post(CHAT_URL, json={"text": "a" * 32000})
    assert r.status_code != 422


def test_invalid_thinking_mode_rejected_422(client: TestClient) -> None:
    r = client.post(CHAT_URL, json={"text": "merhaba", "thinking_mode": "asiri"})
    assert r.status_code == 422


@pytest.mark.parametrize("mode", ["hizli", "normal", "derin", "yogun", "azami", "ultra"])
def test_valid_thinking_modes_pass_validation(client: TestClient, mode: str) -> None:
    r = client.post(CHAT_URL, json={"text": "merhaba", "thinking_mode": mode})
    assert r.status_code != 422


def test_too_many_image_ids_rejected_422(client: TestClient) -> None:
    """Ceiling is 30 (the real provider-specific limit is on the frontend) — 31 ids → 422."""
    r = client.post(CHAT_URL, json={"text": "merhaba", "image_ids": ["x"] * 31})
    assert r.status_code == 422


def test_thirty_image_ids_pass_validation(client: TestClient) -> None:
    """Exactly 30 ids (upper bound) PASSES validation (not 422)."""
    r = client.post(CHAT_URL, json={"text": "merhaba", "image_ids": ["x"] * 30})
    assert r.status_code != 422


def test_empty_text_with_attachment_passes_validation(client: TestClient) -> None:
    """EC3: attachment-only message — empty text but present file_ids PASSES validation."""
    r = client.post(CHAT_URL, json={"text": "", "file_ids": ["x"]})
    assert r.status_code != 422


def test_overlong_conversation_id_rejected_422(client: TestClient) -> None:
    r = client.post(CHAT_URL, json={"text": "merhaba", "conversation_id": "a" * 65})
    assert r.status_code == 422


def test_normal_text_with_surrounding_whitespace_passes_validation(
    client: TestClient,
) -> None:
    """Text with meaningful content but leading/trailing whitespace is NOT rejected (no auto-strip)."""
    r = client.post(CHAT_URL, json={"text": "  merhaba  "})
    assert r.status_code != 422
