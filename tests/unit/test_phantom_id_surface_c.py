"""The phantom turn-id round — SURFACE C (voice).

``persist_user_turn`` / ``persist_assistant_turn`` used to hand back the ULID they had
MINTED whether or not the row reached ``memory.db``, so both voice surfaces treated a
minted id as a receipt:

  * the realtime bridges cleared the transcript buffers BEFORE attempting the write and
    then announced ``turn_completed{status:"ok"}`` with that id. A voice turn has no
    client-side copy — the pane's rows are painted from transcript frames the bridge
    itself produced — so a swallowed write deleted the only copy of the exchange AND
    made the client reload the log over the rows it was showing;
  * ``POST /voice`` ignored both results and returned 200 + an "ok" completion for an
    exchange that is absent from the archive.

Every failure here is simulated the way production fails: ``Memory.remember_turn``
raises, the writer catches/retries/logs, and the row is genuinely not in the store.
Patching the writer to return "" instead would skip the very code under test.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from akana_server.chat_injections import _turn_running
from akana_server.conversation_service import ConversationService
from akana_server.events import EventHub


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


class _RecordingHub(EventHub):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[dict] = []

    async def broadcast_json(self, data):  # type: ignore[override]
        self.sent.append(data)


class _FakeWS:
    def __init__(self) -> None:
        self.sent_json: list[dict] = []

    async def send_json(self, payload):
        self.sent_json.append(payload)

    async def send_bytes(self, data):  # pragma: no cover - unused here
        pass

    async def close(self, code=1000, reason=""):  # pragma: no cover - unused here
        pass


def _settings(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(data_dir=Path(tmp_path), primary_lang="en")


def _app(tmp_path) -> SimpleNamespace:
    return SimpleNamespace(
        state=SimpleNamespace(
            event_hub=_RecordingHub(),
            settings=_settings(tmp_path),
            conversation_service=ConversationService(Path(tmp_path)),
            active_turns={},
        )
    )


def _make_conv(tmp_path) -> str:
    return ConversationService(Path(tmp_path)).create(title="Voice").id


def _messages(tmp_path, conv_id) -> list[Any]:
    return ConversationService(Path(tmp_path)).list_messages(conv_id)


def _texts(tmp_path, conv_id) -> list[str]:
    return [m.content for m in _messages(tmp_path, conv_id)]


def _frames(app, kind: str) -> list[dict]:
    return [e for e in app.state.event_hub.sent if e.get("type") == kind]


def _break_store(monkeypatch, tmp_path, *, roles=("user", "assistant", "error")) -> dict:
    """Make ``remember_turn`` raise for ``roles`` — production's own failure mode.

    Returns a switch dict so a test can heal the store between turns
    (``switch["broken"] = False``)."""
    from akana_server.memory_core import get_memory_core
    from akana_server.orchestrator import turn_writer

    monkeypatch.setattr(turn_writer, "_PERSIST_BACKOFF_S", 0.0)
    mem = get_memory_core(Path(tmp_path))
    real = mem.remember_turn
    switch = {"broken": True}

    def _boom(**kwargs):
        if switch["broken"] and kwargs.get("role") in roles:
            raise sqlite3.OperationalError("database is locked")
        return real(**kwargs)

    monkeypatch.setattr(mem, "remember_turn", _boom)
    return switch


def _bridge(tmp_path, app, conv_id: str):
    from akana_server.voice.realtime_base import RealtimeBridge

    class _Bridge(RealtimeBridge):
        _broadcast_source = "voice_test"
        _label = "Realtime Test"

        def _available(self) -> bool:
            return True

        def _begin_turn_mode(self) -> str:
            return "voice_test"

        async def _open_session(self) -> None:  # pragma: no cover - unused
            pass

        async def _from_browser(self, session):  # pragma: no cover - unused
            pass

        async def _from_provider(self, session):  # pragma: no cover - unused
            pass

    return _Bridge(_FakeWS(), _settings(tmp_path), app=app, conv_id=conv_id)


async def _speak(bridge, question: str, answer: str) -> None:
    bridge._in_buf += question
    bridge._out_buf += answer
    await bridge._persist_turn()
    await asyncio.sleep(0.05)  # let the fire-and-forget announcements land


# --------------------------------------------------------------------------- #
# C1 — the realtime bridges
# --------------------------------------------------------------------------- #


def test_a_lost_voice_write_is_never_announced_as_a_completed_turn(
    tmp_path, monkeypatch
) -> None:
    """The bridge announced ``ok`` + the minted assistant id for a row that is not
    there, which is what makes the client reload the log and wipe the pane."""
    app = _app(tmp_path)
    conv = _make_conv(tmp_path)
    bridge = _bridge(tmp_path, app, conv)
    _break_store(monkeypatch, tmp_path)

    asyncio.run(_speak(bridge, "what is the weather", "it is sunny"))

    assert _texts(tmp_path, conv) == [], "the write was supposed to fail"
    completed = _frames(app, "turn_completed")
    assert len(completed) == 1, app.state.event_hub.sent
    assert completed[0]["status"] == "error", completed[0]
    assert not completed[0].get("assistant_turn_id"), (
        "a completion pointing at a row that does not exist makes the client reload "
        "the chat log and erase the transcript rows the voice pane is showing"
    )
    # …and the turn does not stay latched: exactly one announcement, one completion.
    assert len(_frames(app, "turn_active")) == 1, app.state.event_hub.sent
    assert _turn_running(app, conv) is False


def test_a_lost_spoken_exchange_is_not_deleted_but_written_by_the_next_turn(
    tmp_path, monkeypatch
) -> None:
    """The buffers are the ONLY copy of a spoken turn. Clearing them before the write
    was attempted destroyed the exchange outright; carrying them lets the next
    successful write rescue the words."""
    app = _app(tmp_path)
    conv = _make_conv(tmp_path)
    bridge = _bridge(tmp_path, app, conv)
    switch = _break_store(monkeypatch, tmp_path)

    async def main() -> None:
        await _speak(bridge, "what is the weather", "it is sunny")
        assert _texts(tmp_path, conv) == []
        switch["broken"] = False  # the lock clears
        await _speak(bridge, "and tomorrow", "rain")

    asyncio.run(main())

    texts = _texts(tmp_path, conv)
    assert len(texts) == 2, texts  # one user row, one assistant row — not four
    assert "what is the weather" in texts[0] and "and tomorrow" in texts[0], texts
    assert "it is sunny" in texts[1] and "rain" in texts[1], texts
    completed = _frames(app, "turn_completed")
    assert [c["status"] for c in completed] == ["error", "ok"], completed
    assert completed[1]["assistant_turn_id"] == _messages(tmp_path, conv)[-1].id


def test_a_question_whose_answer_was_lost_is_not_stored_twice(
    tmp_path, monkeypatch
) -> None:
    """Half a pair: the question lands, the answer does not. The retained transcript is
    re-written under the SAME turn id, so the archive does not end up holding the
    question twice — and the answerless question is announced as an error, not ok."""
    app = _app(tmp_path)
    conv = _make_conv(tmp_path)
    bridge = _bridge(tmp_path, app, conv)
    switch = _break_store(monkeypatch, tmp_path, roles=("assistant",))

    async def main() -> str:
        await _speak(bridge, "what is the weather", "it is sunny")
        assert _texts(tmp_path, conv) == ["what is the weather"], "only the question"
        assert _frames(app, "turn_completed")[0]["status"] == "error"
        first_user_id = _messages(tmp_path, conv)[0].id
        switch["broken"] = False
        await _speak(bridge, "and tomorrow", "rain")
        return first_user_id

    first_user_id = asyncio.run(main())

    msgs = _messages(tmp_path, conv)
    assert [m.role for m in msgs] == ["user", "assistant"], [m.role for m in msgs]
    assert msgs[0].id == first_user_id, "the stored question was duplicated"
    assert "what is the weather" in msgs[0].content and "and tomorrow" in msgs[0].content
    assert "it is sunny" in msgs[1].content and "rain" in msgs[1].content


def test_a_question_that_never_landed_gets_no_orphan_answer(tmp_path, monkeypatch) -> None:
    """The worst half-pair is the other one: an assistant row with no question is fed to
    the next turn as LLM history and the model contradicts itself. If the question did
    not reach the store, the answer must not be written on its own."""
    app = _app(tmp_path)
    conv = _make_conv(tmp_path)
    bridge = _bridge(tmp_path, app, conv)
    _break_store(monkeypatch, tmp_path, roles=("user",))

    asyncio.run(_speak(bridge, "what is the weather", "it is sunny"))

    assert _texts(tmp_path, conv) == [], "an answer was stored with no question"
    assert _frames(app, "turn_completed")[0]["status"] == "error"


def test_the_gemini_turn_boundary_does_not_delete_an_unsaved_transcript(
    tmp_path, monkeypatch
) -> None:
    """Through the real provider path: ``turn_complete`` clears the one-sided buffers
    right after the persist, so the retained words must live in the base, not in the
    provider-owned buffers."""
    from akana_server.voice.gemini_live import LiveBridge

    class _T:
        def __init__(self, text: str) -> None:
            self.text = text

    class _SC:
        def __init__(self, *, inp=None, out=None, turn_complete=False) -> None:
            self.input_transcription = _T(inp) if inp is not None else None
            self.output_transcription = _T(out) if out is not None else None
            self.turn_complete = turn_complete
            self.interrupted = False

    class _Resp:
        def __init__(self, sc) -> None:
            self.data = None
            self.server_content = sc
            self.tool_call = None

    app = _app(tmp_path)
    conv = _make_conv(tmp_path)
    bridge = LiveBridge(_FakeWS(), _settings(tmp_path), app=app, conv_id=conv)
    switch = _break_store(monkeypatch, tmp_path)

    async def turn(question: str, answer: str) -> None:
        await bridge._handle_response(_Resp(_SC(inp=question)))
        await bridge._handle_response(_Resp(_SC(out=answer)))
        await bridge._handle_response(_Resp(_SC(turn_complete=True)))
        await asyncio.sleep(0.05)

    async def main() -> None:
        await turn("who won", "nobody")
        assert _texts(tmp_path, conv) == []
        switch["broken"] = False
        await turn("say again", "nobody won")

    asyncio.run(main())

    texts = _texts(tmp_path, conv)
    assert len(texts) == 2, texts
    assert "who won" in texts[0], texts
    assert "nobody" in texts[1], texts


def test_the_openai_flush_does_not_overwrite_an_unsaved_transcript(
    tmp_path, monkeypatch
) -> None:
    """``_flush_pending_assistant`` REASSIGNS ``_in_buf`` after the persist (it seeds the
    next turn's diverted deltas). Retention that lived in that buffer would be gone."""
    from akana_server.voice.openai_realtime import OpenAIRealtimeBridge

    app = _app(tmp_path)
    conv = _make_conv(tmp_path)
    bridge = OpenAIRealtimeBridge(_FakeWS(), _settings(tmp_path), app=app, conv_id=conv)
    switch = _break_store(monkeypatch, tmp_path)

    async def main() -> None:
        bridge._pending_assistant = "the answer"
        bridge._deferred_in_buf = "next turn prefix "
        await bridge._flush_pending_assistant()  # user transcript never arrived
        await asyncio.sleep(0.05)
        assert _texts(tmp_path, conv) == []
        switch["broken"] = False
        bridge._out_buf += "second answer"
        await bridge._persist_turn()
        await asyncio.sleep(0.05)

    asyncio.run(main())

    texts = _texts(tmp_path, conv)
    assert len(texts) == 2, texts
    assert "the answer" in texts[1], "the unsaved reply was overwritten by the seed"


# --------------------------------------------------------------------------- #
# C2 — POST /voice
# --------------------------------------------------------------------------- #


async def _mock_transcribe(*_args, **_kwargs):
    return "what is the weather", "en"


async def _mock_complete(*_args, **_kwargs):
    return "it is sunny", {"prompt_tokens": 1, "completion_tokens": 2, "tool_calls": []}


@pytest.fixture
def voice_client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    from akana_server.api.app import create_app

    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8767")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    monkeypatch.setattr(
        "akana_server.api.routes.voice.transcribe_wav_bytes", _mock_transcribe
    )
    monkeypatch.setattr(
        "akana_server.api.routes.chat.complete_chat_with_usage", _mock_complete
    )
    app = create_app()
    with TestClient(app) as c:
        yield c


def _post_voice(client: TestClient, conv_id: str):
    return client.post(
        "/api/v1/voice",
        files={"audio": ("t.wav", b"RIFF" + b"\x00" * 200, "audio/wav")},
        data={"conversation_id": conv_id, "tts": "false"},
    )


def _ws_frames(client: TestClient, kind: str) -> list[dict]:
    hub = client.app.state.event_hub
    return [e for e in getattr(hub, "_recorded", []) if e.get("type") == kind]


def _record_hub(client: TestClient) -> None:
    """Tee the app's real EventHub so the lifecycle frames can be asserted."""
    hub = client.app.state.event_hub
    hub._recorded = []
    original = hub.broadcast_json

    async def _tee(data):
        hub._recorded.append(data)
        await original(data)

    hub.broadcast_json = _tee  # type: ignore[method-assign]


def test_post_voice_reports_a_lost_exchange_instead_of_a_clean_200(
    voice_client, tmp_path, monkeypatch
) -> None:
    """POST /voice ignored BOTH writer results: the user heard the reply, got a 200 and
    a ``turn_completed{status:"ok"}``, and the exchange (with its tool_calls) was simply
    absent from the archive — ``history_turns`` failing to advance was the only hint."""
    conv = _make_conv(tmp_path)
    _record_hub(voice_client)
    _break_store(monkeypatch, tmp_path, roles=("user", "assistant"))

    r = _post_voice(voice_client, conv)

    assert r.status_code == 200, r.text  # the answer is spoken; do not break it
    assert r.json()["history_turns"] == 0
    assert _texts(tmp_path, conv) == [], "the write was supposed to fail"
    time.sleep(0.3)  # the completion is announced by a fire-and-forget task
    completed = _ws_frames(voice_client, "turn_completed")
    assert len(completed) == 1, completed
    assert completed[0]["status"] == "error", completed[0]


def test_post_voice_does_not_orphan_an_answer_whose_question_was_lost(
    voice_client, tmp_path, monkeypatch
) -> None:
    """A stored answer with no question becomes the next turn's LLM history."""
    conv = _make_conv(tmp_path)
    _break_store(monkeypatch, tmp_path, roles=("user",))

    assert _post_voice(voice_client, conv).status_code == 200
    assert _texts(tmp_path, conv) == []


def test_post_voice_leaves_a_marker_when_only_the_answer_was_lost(
    voice_client, tmp_path, monkeypatch
) -> None:
    """The question landed and its answer did not: the archive would show that question
    with nothing under it forever. A role="error" marker takes the answer's place — it
    re-renders on reload and is excluded from the LLM history window."""
    conv = _make_conv(tmp_path)
    _break_store(monkeypatch, tmp_path, roles=("assistant",))

    assert _post_voice(voice_client, conv).status_code == 200

    msgs = _messages(tmp_path, conv)
    assert [m.role for m in msgs] == ["user", "error"], [m.role for m in msgs]
    assert "could not be saved" in msgs[1].content


def test_post_voice_still_reports_ok_when_the_turn_is_stored(
    voice_client, tmp_path
) -> None:
    """The happy path is untouched: one completion, status ok, and the exchange is in
    the archive with an advanced history count."""
    conv = _make_conv(tmp_path)
    _record_hub(voice_client)

    r = _post_voice(voice_client, conv)

    assert r.status_code == 200, r.text
    assert r.json()["history_turns"] == 2
    assert _texts(tmp_path, conv) == ["what is the weather", "it is sunny"]
    time.sleep(0.3)
    completed = _ws_frames(voice_client, "turn_completed")
    assert len(completed) == 1, completed
    assert completed[0]["status"] == "ok", completed[0]
