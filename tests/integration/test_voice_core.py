"""Voice API core integration tests (config, preferences, upload validation)."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_voice_config_returns_200(client: TestClient) -> None:
    r = client.get("/api/v1/voice/config")
    assert r.status_code == 200
    body = r.json()
    assert "tts" in body
    assert "stt" in body
    assert "wake" in body
    assert body["stt"]["engine"] == "faster-whisper"
    # ``engine`` now reflects the resolved preference (default: auto); the engines
    # are in the ``engines`` list, and the persisted choice is returned in the
    # ``selected_*`` fields.
    assert body["tts"]["engine"] in ("auto", "edge", "piper")
    assert "piper" in body["tts"]["engines"]
    assert body["tts"]["selected_engine"] == "auto"
    assert "engine_voices" in body["tts"]


def test_post_voice_empty_audio_returns_client_error(client: TestClient) -> None:
    r = client.post(
        "/api/v1/voice",
        files={"audio": ("empty.wav", b"", "audio/wav")},
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["error"]["code"] == "BAD_REQUEST"


def test_post_voice_invalid_audio_does_not_crash(client: TestClient) -> None:
    """Non-empty garbage WAV must fail gracefully (400 decode/STT or 503 if whisper missing)."""
    r = client.post(
        "/api/v1/voice",
        files={"audio": ("bad.wav", b"not-a-valid-wav" * 8, "audio/wav")},
    )
    assert r.status_code in (400, 503)
    detail = r.json()["detail"]
    assert "error" in detail
    assert detail["error"]["code"] in ("STT_ERROR", "BAD_REQUEST")


async def _mock_transcribe(*_args, **_kwargs):
    return "ses ile soru", "tr"


async def _mock_complete(*_args, **_kwargs):
    return "Ses ile cevap.", {
        "prompt_tokens": 2,
        "completion_tokens": 4,
        "tool_calls": [],
    }


def test_voice_persists_user_and_assistant_to_episodic(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: voice round-trip must write both turns to episodic.db."""
    monkeypatch.setattr(
        "akana_server.api.routes.voice.transcribe_wav_bytes",
        _mock_transcribe,
    )
    # The shared turn core reads complete_chat_with_usage from the chat package
    # namespace at call time (turn_core), so voice is patched there too.
    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage",
        _mock_complete,
    )

    created = client.post("/api/v1/conversations", json={"title": "Voice persist"})
    cid = created.json()["id"]

    fake_wav = b"RIFF" + b"\x00" * 200
    r = client.post(
        "/api/v1/voice",
        files={"audio": ("test.wav", fake_wav, "audio/wav")},
        data={"conversation_id": cid},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["transcript"] == "ses ile soru"
    assert body["text"] == "Ses ile cevap."
    assert body["conversation_id"] == cid

    messages = client.get(f"/api/v1/conversations/{cid}/messages").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "ses ile soru"
    assert messages[1]["content"] == "Ses ile cevap."


def test_voice_response_lang_reflects_stt_lang_not_tts(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#3 regression: the ``lang`` in the response must reflect the turn's
    (persisted) language = stt_lang; the TTS output language must NOT override it.
    The old code returned the input param (None) when want_tts=False and the TTS
    language when want_tts=True, creating an inconsistency."""
    monkeypatch.setattr(
        "akana_server.api.routes.voice.transcribe_wav_bytes", _mock_transcribe
    )
    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage", _mock_complete
    )
    fake_wav = b"RIFF" + b"\x00" * 200

    # TTS off → lang == stt_lang ("tr"); the old code would return None.
    r = client.post("/api/v1/voice", files={"audio": ("a.wav", fake_wav, "audio/wav")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lang"] == "tr"
    assert body["stt_lang"] == "tr"

    # TTS on and in a DIFFERENT language (en) → lang must STILL be "tr" (TTS language does not override).
    async def _fake_tts(*_a, **_k):
        return b"RIFFxxxx", "audio/wav"

    monkeypatch.setattr(
        "akana_server.api.routes.voice.resolve_tts_voice_path",
        lambda *a, **k: Path("x.onnx"),
    )
    monkeypatch.setattr(
        "akana_server.api.routes.voice.synthesize_with_fallback", _fake_tts
    )
    r2 = client.post(
        "/api/v1/voice",
        files={"audio": ("a.wav", fake_wav, "audio/wav")},
        data={"tts": "1", "tts_lang": "en"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["lang"] == "tr", "TTS language (en) must not override the response lang"


def test_voice_tts_stream_invalid_lang_returns_400_not_500(client: TestClient) -> None:
    """VB-1: an invalid ``lang`` on /voice/tts/stream raises TtsError BEFORE the SSE
    stream starts; it must map to a clean 400 (like the one-shot /voice/tts), not a
    raw 500 — otherwise the client's documented one-shot fallback never engages."""
    r = client.post(
        "/api/v1/voice/tts/stream",
        json={"text": "hello", "lang": "xx"},
    )
    assert r.status_code == 400, r.text
    assert r.json()["detail"]["error"]["code"] == "TTS_ERROR"


def test_voice_tts_failure_after_persist_degrades_to_text_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """VB-2: a TTS failure AFTER the turn is persisted must NOT abort the response.

    The user + assistant turns are already committed; returning a 5xx would leave a
    committed-but-invisible turn that the user re-asks → duplicate pair. Instead the
    turn text is returned with no audio + a ``tts_error`` hint, and the turn is still
    persisted/visible on reload."""
    from akana_server.voice import TtsError

    monkeypatch.setattr(
        "akana_server.api.routes.voice.transcribe_wav_bytes", _mock_transcribe
    )
    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage", _mock_complete
    )
    monkeypatch.setattr(
        "akana_server.api.routes.voice.resolve_tts_voice_path",
        lambda *a, **k: Path("x.onnx"),
    )

    async def _boom_tts(*_a, **_k):
        raise TtsError("Piper voice model not found", status_code=503)

    monkeypatch.setattr(
        "akana_server.api.routes.voice.synthesize_with_fallback", _boom_tts
    )

    created = client.post("/api/v1/conversations", json={"title": "Voice degrade"})
    cid = created.json()["id"]

    fake_wav = b"RIFF" + b"\x00" * 200
    r = client.post(
        "/api/v1/voice",
        files={"audio": ("a.wav", fake_wav, "audio/wav")},
        data={"tts": "1", "conversation_id": cid},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["text"] == "Ses ile cevap."
    assert body["audio_wav_base64"] is None
    assert body["tts_error"] and "Piper" in body["tts_error"]

    # The turn is durably persisted (would-be duplicate on a client retry otherwise).
    messages = client.get(f"/api/v1/conversations/{cid}/messages").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]


def test_voice_llm_timeout_maps_to_unified_code_and_leaves_no_orphan(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rest-api:arch:1 regression: voice error mapping flows through the shared turn core.

    Before voice was rebased onto ``run_nonstreaming_turn`` it caught ``LLMCallError``
    directly and mapped *every* non-400 failure to ``LLM_UNAVAILABLE`` — a drifted copy
    of the blocking path that could never surface ``LLM_TIMEOUT``. Now a timeout raised
    by the dispatch fn must reach the client as the SAME 504/``LLM_TIMEOUT`` the blocking
    ``POST /chat`` path returns. It must also leave NO turn persisted (the pair is written
    only after the LLM succeeds — no answerless orphan user turn).
    """
    from akana_server.orchestrator.llm_dispatch import LLMCallError

    monkeypatch.setattr(
        "akana_server.api.routes.voice.transcribe_wav_bytes", _mock_transcribe
    )

    async def _timeout(*_a, **_k):
        raise LLMCallError("LLM_TIMEOUT: the model took too long", status_code=504)

    # Patched on the chat package namespace — the core reads it there at call time.
    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage", _timeout
    )

    created = client.post("/api/v1/conversations", json={"title": "Voice timeout"})
    cid = created.json()["id"]

    fake_wav = b"RIFF" + b"\x00" * 200
    r = client.post(
        "/api/v1/voice",
        files={"audio": ("t.wav", fake_wav, "audio/wav")},
        data={"conversation_id": cid},
    )
    assert r.status_code == 504, r.text
    assert r.json()["detail"]["error"]["code"] == "LLM_TIMEOUT"

    # No orphan: neither the user nor the assistant turn was written on failure.
    messages = client.get(f"/api/v1/conversations/{cid}/messages").json()["messages"]
    assert messages == []


def test_voice_preferences_get_and_patch(client: TestClient) -> None:
    r = client.get("/api/v1/voice/preferences")
    assert r.status_code == 200
    body = r.json()
    assert "wake_autostart" in body
    assert "stream_tts" in body
    assert body["wake_autostart"] is False
    assert body["stream_tts"] is False

    patched = client.patch(
        "/api/v1/voice/preferences",
        json={"wake_autostart": True, "stream_tts": True},
    )
    assert patched.status_code == 200
    updated = patched.json()
    assert updated["wake_autostart"] is True
    assert updated["stream_tts"] is True

    again = client.get("/api/v1/voice/preferences")
    assert again.status_code == 200
    assert again.json()["wake_autostart"] is True
    assert again.json()["stream_tts"] is True


def test_voice_preferences_patch_persists_engine_and_voice(client: TestClient) -> None:
    """Regression (bug: «voice keeps reverting to the old voice»): the selected
    engine/voice must actually be saved via PATCH — previously VoicePreferencesPatch
    did not recognize these fields, so pydantic silently dropped them and the choice
    never persisted."""
    patched = client.patch(
        "/api/v1/voice/preferences",
        json={"tts_engine": "edge", "tts_voice_tr": "tr-TR-AhmetNeural"},
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["tts_engine"] == "edge"
    assert body["tts_voice_tr"] == "tr-TR-AhmetNeural"
    # Still selected on a new GET (does not revert to default on another request).
    again = client.get("/api/v1/voice/preferences").json()
    assert again["tts_engine"] == "edge"
    assert again["tts_voice_tr"] == "tr-TR-AhmetNeural"
    # /voice/config also reflects the persisted choice (the UI picker reads this).
    cfg = client.get("/api/v1/voice/config").json()["tts"]
    assert cfg["selected_engine"] == "edge"
    assert cfg["selected_voice_tr"] == "tr-TR-AhmetNeural"


def test_voice_preferences_corrupt_file_returns_defaults(
    client: TestClient, tmp_path
) -> None:
    """A corrupt voice_preferences.json does not produce a 500 — defaults are returned."""
    (tmp_path / "voice_preferences.json").write_text("{bozuk", encoding="utf-8")
    r = client.get("/api/v1/voice/preferences")
    assert r.status_code == 200
    body = r.json()
    assert body["wake_autostart"] is False
    assert body["tts_engine"] == "auto"
    # PATCH replaces the corrupt file with valid content.
    patched = client.patch("/api/v1/voice/preferences", json={"stream_tts": True})
    assert patched.status_code == 200
    assert patched.json()["stream_tts"] is True
    assert client.get("/api/v1/voice/preferences").json()["stream_tts"] is True


def test_voice_routes_require_bearer_when_token_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "gizli-token")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    app = create_app()
    with TestClient(app) as c:
        prox = {"X-Forwarded-For": "1.2.3.4"}
        r = c.get("/api/v1/voice/preferences", headers=prox)
        assert r.status_code == 401
        r2 = c.post(
            "/api/v1/voice",
            files={"audio": ("a.wav", b"RIFF" + b"\x00" * 64, "audio/wav")},
            headers=prox,
        )
        assert r2.status_code == 401
        ok = c.get(
            "/api/v1/voice/preferences",
            headers={"Authorization": "Bearer gizli-token", **prox},
        )
        assert ok.status_code == 200


def test_voice_wake_oversize_upload_returns_413(client: TestClient) -> None:
    big = b"\x00" * (10 * 1024 * 1024 + 1)
    r = client.post("/api/v1/voice/wake", files={"audio": ("big.wav", big, "audio/wav")})
    assert r.status_code == 413
    assert r.json()["detail"]["error"]["code"] == "PAYLOAD_TOO_LARGE"


def test_voice_tts_rejects_bad_payload(client: TestClient) -> None:
    # Empty text hits the pydantic constraint (min_length=1) — 422.
    r = client.post("/api/v1/voice/tts", json={"text": ""})
    assert r.status_code == 422
    # Overly long text also hits the schema constraint (max_length=10000).
    r2 = client.post("/api/v1/voice/tts", json={"text": "a" * 10001})
    assert r2.status_code == 422


def test_voice_transcribe_is_stt_only(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """transcribe returns only text; no LLM call is made (that is the chat pipeline's job)."""
    monkeypatch.setattr(
        "akana_server.api.routes.voice.transcribe_wav_bytes",
        _mock_transcribe,
    )

    def _no_llm(*_args, **_kwargs):
        raise AssertionError("transcribe-only must not call the LLM")

    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage",
        _no_llm,
    )
    fake_wav = b"RIFF" + b"\x00" * 200
    r = client.post(
        "/api/v1/voice/transcribe",
        files={"audio": ("test.wav", fake_wav, "audio/wav")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["transcript"] == "ses ile soru"
    assert body["stt_lang"] == "tr"
    # STT-only: no LLM/TTS field in the response.
    assert set(body.keys()) == {"transcript", "stt_lang"}


def test_voice_transcribe_silence_returns_empty_transcript(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silence/empty audio → 200 + empty transcript (the client silently listens again)."""

    async def _silence(*_args, **_kwargs):
        return "", None

    monkeypatch.setattr(
        "akana_server.api.routes.voice.transcribe_wav_bytes",
        _silence,
    )
    fake_wav = b"RIFF" + b"\x00" * 200
    r = client.post(
        "/api/v1/voice/transcribe",
        files={"audio": ("test.wav", fake_wav, "audio/wav")},
    )
    assert r.status_code == 200, r.text
    assert r.json()["transcript"] == ""


def test_voice_transcribe_empty_audio_returns_400(client: TestClient) -> None:
    r = client.post(
        "/api/v1/voice/transcribe",
        files={"audio": ("empty.wav", b"", "audio/wav")},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error"]["code"] == "BAD_REQUEST"
