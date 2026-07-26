"""Deleting a conversation must reclaim the bytes of the files attached to it.

``<data_dir>/uploads/`` had NO reclamation path at all: the store is append-only, nothing
sweeps it, and ``DELETE /conversations/{id}`` cleared the turns without touching a single
file. Every attachment of every deleted conversation stayed on disk forever.

The reverse failure is the harder half, so it is pinned here too: a file another
conversation still references, and a file the composer has staged but not yet sent, must
BOTH survive the sweep.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app

CONV_A = "01CONVUPLOADGCAAAAAAAAAAAAA"
CONV_B = "01CONVUPLOADGCBBBBBBBBBBBBB"


def _png_chunk(ctype: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + ctype
        + data
        + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
    )


def _png(seed: int = 0) -> bytes:
    ihdr = struct.pack(">IIBBBBB", 2, 1, 8, 2, 0, 0, 0)
    raw = b"\x00" + bytes([seed, 0xCD, 0xEF]) * 2
    return b"\x89PNG\r\n\x1a\n" + (
        _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(raw))
        + _png_chunk(b"IEND", b"")
    )


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("CURSOR_API_KEY", "x")
    monkeypatch.setenv("AKANA_MEMORY_LLM_CAPTURE", "0")
    monkeypatch.setenv("AKANA_LLM_CHAT_TITLES", "0")
    monkeypatch.setenv("LLM_PROVIDER", "claude")

    async def fake_complete(settings, user_text, **kwargs):
        return "ok.", {"prompt_tokens": 1, "completion_tokens": 1, "tool_calls": []}

    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage", fake_complete
    )
    app = create_app()
    with TestClient(app) as c:
        yield c


def _upload(client: TestClient, data: bytes, name: str = "foto.png") -> str:
    r = client.post("/api/v1/uploads", files={"file": (name, data, "image/png")})
    assert r.status_code == 200, r.text
    return r.json()["image"]["id"]


def _disk_path(client: TestClient, file_id: str) -> Path:
    r = client.get(f"/api/v1/uploads/{file_id}")
    assert r.status_code == 200, r.text
    return Path(r.json()["image"]["path"])


def _send(client: TestClient, conv: str, file_ids: list[str]) -> None:
    r = client.post(
        "/api/v1/chat",
        json={"text": "look at this", "conversation_id": conv, "file_ids": file_ids},
    )
    assert r.status_code == 200, r.text


def test_deleting_a_conversation_reclaims_its_attachment_bytes(
    client: TestClient,
) -> None:
    fid = _upload(client, _png(1))
    path = _disk_path(client, fid)
    _send(client, CONV_A, [fid])
    assert path.is_file()

    assert client.delete(f"/api/v1/conversations/{CONV_A}").status_code == 204
    assert not path.exists(), "the attachment survived the conversation it belonged to"


def test_attachment_referenced_by_another_conversation_survives(
    client: TestClient,
) -> None:
    """Content dedup means two chats can share ONE upload id — deleting either must not
    pull the bytes out from under the other."""
    fid = _upload(client, _png(2))
    path = _disk_path(client, fid)
    _send(client, CONV_A, [fid])
    _send(client, CONV_B, [fid])

    assert client.delete(f"/api/v1/conversations/{CONV_A}").status_code == 204
    assert path.is_file(), "deleted one chat and broke the attachment of another"
    # Still fully usable from the surviving conversation.
    assert client.get(f"/api/v1/uploads/{fid}/raw").status_code == 200


def test_upload_staged_but_never_sent_is_untouched(client: TestClient) -> None:
    """The composer parks an attachment BEFORE the message is sent; it belongs to no turn
    yet, so a sweep keyed on "unreferenced" would delete the file the user is about to
    send. Candidates come only from the deleted conversation's own turns."""
    parked = _upload(client, _png(3), name="parked.png")
    attached = _upload(client, _png(4), name="attached.png")
    parked_path = _disk_path(client, parked)
    attached_path = _disk_path(client, attached)
    _send(client, CONV_A, [attached])

    assert client.delete(f"/api/v1/conversations/{CONV_A}").status_code == 204
    assert not attached_path.exists()
    assert parked_path.is_file(), "deleted an attachment the user had not sent yet"


def test_delete_without_attachments_still_succeeds(client: TestClient) -> None:
    r = client.post(
        "/api/v1/chat", json={"text": "hello", "conversation_id": CONV_B}
    )
    assert r.status_code == 200, r.text
    assert client.delete(f"/api/v1/conversations/{CONV_B}").status_code == 204
