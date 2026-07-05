"""Voice route — wiring composer attachments (file_ids) into the turn-based /voice path.

POST /voice now accepts the `file_ids` form field (comma-separated upload ids);
it parses them and passes them to `complete_chat_with_usage(file_ids=...)`
(gemini/openai NATIVE image input). When the field is absent, None is passed.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app
from akana_server.api.routes.voice import _parse_file_ids


async def _mock_transcribe(*_args, **_kwargs):
    return "ses ile soru", "tr"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8767")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    app = create_app()
    with TestClient(app) as c:
        yield c


# ── Pure helper: parsing contract ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, []),
        ("", []),
        ("   ", []),
        ("a", ["a"]),
        ("a,b,c", ["a", "b", "c"]),
        ("  a , b ,c ", ["a", "b", "c"]),  # whitespace is trimmed
        ("a,,b,", ["a", "b"]),  # empty parts are dropped
        ("a,b,a,c,b", ["a", "b", "c"]),  # order-preserving deduplication
        (",,,", []),
    ],
)
def test_parse_file_ids(raw: str | None, expected: list[str]) -> None:
    assert _parse_file_ids(raw) == expected


# ── End-to-end: does the form field reach dispatch ───────────────────────────


def _capture_complete(captured: dict[str, Any]):
    async def fake_complete(*_args, **kwargs):
        captured.update(kwargs)
        return "Ses ile cevap.", {
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "tool_calls": [],
        }

    return fake_complete


def test_post_voice_threads_parsed_file_ids_to_dispatch(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The `file_ids` form field is cleaned up and passed to complete_chat_with_usage as a list."""
    monkeypatch.setattr(
        "akana_server.api.routes.voice.transcribe_wav_bytes", _mock_transcribe
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage",
        _capture_complete(captured),
    )

    fake_wav = b"RIFF" + b"\x00" * 200
    r = client.post(
        "/api/v1/voice",
        files={"audio": ("test.wav", fake_wav, "audio/wav")},
        # strip + drop-empty + order-preserving dedupe are proven here too
        data={"file_ids": " up_1 , up_2 ,, up_1 "},
    )
    assert r.status_code == 200, r.text
    assert captured.get("file_ids") == ["up_1", "up_2"]


def test_post_voice_file_ids_absent_passes_none(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the field is absent (or empty), None goes to dispatch — NATIVE input is skipped."""
    monkeypatch.setattr(
        "akana_server.api.routes.voice.transcribe_wav_bytes", _mock_transcribe
    )
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage",
        _capture_complete(captured),
    )

    fake_wav = b"RIFF" + b"\x00" * 200
    r = client.post(
        "/api/v1/voice",
        files={"audio": ("test.wav", fake_wav, "audio/wav")},
    )
    assert r.status_code == 200, r.text
    assert "file_ids" in captured
    assert captured["file_ids"] is None
