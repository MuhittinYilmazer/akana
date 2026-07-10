"""Blitz-3 be-api regression tests (chat routes + voice + app lifespan).

Covers four verified bugs:
  be-api-1  reset_conversation must ALSO drop the provider agent session (clear_agent_id)
  be-api-2  voice turns must persist tool_calls (+ tool-only turns must persist at all)
  be-api-3  live gate/command responses must be persisted (survive a server re-fetch)
  be-api-5  app.state.chat_shutting_down must be reset on startup (second lifespan)
"""

from __future__ import annotations

import ulid
import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8767")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    monkeypatch.setenv("AKANA_LLM_CHAT_TITLES", "0")
    app = create_app()
    with TestClient(app) as c:
        yield c


# -- be-api-1: reset must clear the provider agent session --------------------------


def test_reset_conversation_clears_agent_id(client: TestClient) -> None:
    """DELETE /chat/conversations/{id} (reset) must drop the stored agent_id, symmetric
    with DELETE /conversations. Otherwise reuse (default) RESUMES the surviving provider
    session on the next turn and the model still holds the just-"cleared" history."""
    app = client.app
    svc = app.state.conversation_service
    cid = client.post("/api/v1/conversations", json={"title": "Reset agent"}).json()["id"]
    # A persisted provider agent session for this conversation (cursor-tagged).
    svc.merge_json_metadata(cid, {"agent_id": "agent-cursor-1", "agent_provider": "cursor"})
    assert svc.get_json_metadata(cid).get("agent_id") == "agent-cursor-1"

    r = client.delete(f"/api/v1/chat/conversations/{cid}")
    assert r.status_code == 204, r.text

    meta = svc.get_json_metadata(cid)
    assert not meta.get("agent_id"), "reset left the agent session alive → history not cleared"
    assert not meta.get("agent_provider")


# -- be-api-2: voice tool_calls / tool-only persistence -----------------------------


async def _mock_transcribe(*_args, **_kwargs):
    return "voice question", "en"


def test_voice_persists_tool_calls_on_reload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A voice turn that ran tools must persist tool_calls with the assistant turn, so a
    /messages reload returns the tool cards (was dropped → cards vanished on reload)."""
    monkeypatch.setattr(
        "akana_server.api.routes.voice.transcribe_wav_bytes", _mock_transcribe
    )

    async def _complete_with_tools(*_a, **_k):
        return "Done — remembered it.", {
            "prompt_tokens": 2,
            "completion_tokens": 4,
            "tool_calls": [{"name": "memory_remember", "args": {}}],
        }

    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage", _complete_with_tools
    )

    cid = client.post("/api/v1/conversations", json={"title": "Voice tools"}).json()["id"]
    fake_wav = b"RIFF" + b"\x00" * 200
    r = client.post(
        "/api/v1/voice",
        files={"audio": ("t.wav", fake_wav, "audio/wav")},
        data={"conversation_id": cid},
    )
    assert r.status_code == 200, r.text

    messages = client.get(f"/api/v1/conversations/{cid}/messages").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]
    asst = messages[1]
    assert asst["tool_calls"], "assistant turn persisted without tool_calls → cards gone on reload"
    assert asst["tool_calls"][0]["name"] == "memory_remember"


def test_voice_tool_only_turn_is_persisted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tool-only voice turn (tools ran, no final text) must still persist BOTH turns with
    a placeholder body — mirroring the streaming path — instead of silently dropping the
    entire exchange from the archive."""
    monkeypatch.setattr(
        "akana_server.api.routes.voice.transcribe_wav_bytes", _mock_transcribe
    )

    async def _complete_tool_only(*_a, **_k):
        return "", {
            "prompt_tokens": 2,
            "completion_tokens": 0,
            "tool_calls": [{"name": "memory_remember", "args": {}}],
        }

    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage", _complete_tool_only
    )

    cid = client.post("/api/v1/conversations", json={"title": "Voice tool-only"}).json()["id"]
    fake_wav = b"RIFF" + b"\x00" * 200
    r = client.post(
        "/api/v1/voice",
        files={"audio": ("t.wav", fake_wav, "audio/wav")},
        data={"conversation_id": cid},
    )
    assert r.status_code == 200, r.text

    messages = client.get(f"/api/v1/conversations/{cid}/messages").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"], (
        "tool-only voice turn dropped the whole exchange from history"
    )
    assert messages[1]["tool_calls"], "tool-only turn persisted without its tool cards"


# -- be-api-3: live gate/command responses must be persisted ------------------------


def _fake_gate_response(text: str):
    """A monkeypatch of _run_turn_gates that short-circuits with a file-gate rejection."""
    from akana_server.api.routes.chat.gates import _GateResult
    from akana_server.api.routes.chat.models import ChatResponse

    async def _run(_request, body):
        conv_id = (body.conversation_id or "").strip() or str(ulid.new())
        resp = ChatResponse(
            turn_id=str(ulid.new()),
            text=text,
            lang=body.lang,
            conversation_id=conv_id,
            intent="system_action",
            action="file_unsupported",
        )
        return _GateResult(
            intent="system_action", approval_required=False, body=body, response=resp
        )

    return _run


def test_blocking_gate_response_is_persisted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /chat that returns a gate rejection must persist the user turn + reply, else
    both vanish when the FE re-fetches the log on a chat switch / F5."""
    reply = "I can't process the files: the active provider does not support image input."
    monkeypatch.setattr(
        "akana_server.api.routes.chat._run_turn_gates", _fake_gate_response(reply)
    )
    cid = client.post("/api/v1/conversations", json={"title": "Gate blocking"}).json()["id"]
    r = client.post("/api/v1/chat", json={"text": "read this file", "conversation_id": cid})
    assert r.status_code == 200, r.text

    messages = client.get(f"/api/v1/conversations/{cid}/messages").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"], (
        "live gate response was not persisted → user msg + reply lost on re-fetch"
    )
    assert messages[0]["content"] == "read this file"
    assert messages[1]["content"] == reply


def test_streaming_gate_response_is_persisted(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /chat/stream that returns a gate rejection must persist the user turn + reply."""
    reply = "I can't process the files: the active provider does not support image input."
    monkeypatch.setattr(
        "akana_server.api.routes.chat._run_turn_gates", _fake_gate_response(reply)
    )
    cid = client.post("/api/v1/conversations", json={"title": "Gate stream"}).json()["id"]
    r = client.post(
        "/api/v1/chat/stream", json={"text": "read this file", "conversation_id": cid}
    )
    assert r.status_code == 200, r.text

    messages = client.get(f"/api/v1/conversations/{cid}/messages").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"], (
        "live streaming gate response was not persisted → user msg + reply lost on re-fetch"
    )
    assert messages[1]["content"] == reply


# -- be-api-5: chat_shutting_down reset on startup ----------------------------------


def test_second_lifespan_resets_shutting_down_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The lifespan finally sets chat_shutting_down=True; a SECOND lifespan on the same app
    must reset it on startup, else queue draining is permanently disabled."""
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8767")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    monkeypatch.setenv("AKANA_LLM_CHAT_TITLES", "0")

    app = create_app()
    with TestClient(app):
        pass
    # After the first shutdown the flag is stuck True.
    assert getattr(app.state, "chat_shutting_down", False) is True
    with TestClient(app):
        # The second startup must clear it — otherwise _maybe_drain_queue early-returns forever.
        assert getattr(app.state, "chat_shutting_down", True) is False
