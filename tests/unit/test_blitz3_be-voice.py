"""Blitz-3 be-voice regressions — barge-in / failed-turn buffer contamination.

Hermetic twins of the existing test_openai_realtime / test_gemini_live suites (no
real provider WS/session). Each test drives a specific event ORDERING the existing
regressions miss:

- be-voice-1: OpenAI barge-in that arrives BEFORE the user's async input transcript
  lands — the interrupted reply fragment must not carry into the next turn.
- be-voice-2: Gemini ``interrupted`` on a noise-triggered (input-less) turn — the
  cancelled reply must not carry into the next turn (only ``turn_complete`` had the
  one-sided drop).
- be-voice-3: OpenAI ``response.done`` with status ``failed`` (no output) — the stale
  question must not mispair with the NEXT turn's reply.

Fakes are copied locally (not shared) so the parallel blitz agents don't collide."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from akana_server.voice import gemini_live as gl
from akana_server.voice import openai_realtime as oar


def _settings(tmp_path):
    return SimpleNamespace(
        data_dir=tmp_path,
        primary_lang="en",
        gemini_live_model="",
        gemini_live_voice="",
    )


def _fake_app():
    return SimpleNamespace(state=SimpleNamespace(event_hub=None, conversation_service=None))


# --- OpenAI Realtime fakes -------------------------------------------------


class _FakeOAIWS:
    def __init__(self, events, *, block_after=False) -> None:
        self._events = [json.dumps(e) for e in events]
        self._block_after = block_after
        self.sent: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, data):
        self.sent.append(json.loads(data))

    def __aiter__(self):
        async def _gen():
            for raw in self._events:
                yield raw
            if self._block_after:
                await asyncio.Event().wait()

        return _gen()


class _FakeWS:
    def __init__(self, incoming) -> None:
        self._incoming = list(incoming)
        self.sent_json: list = []
        self.sent_bytes: list = []
        self.closed = None

    async def receive(self):
        if self._incoming:
            return self._incoming.pop(0)
        await asyncio.Event().wait()

    async def send_json(self, payload):
        self.sent_json.append(payload)

    async def send_bytes(self, data):
        self.sent_bytes.append(data)

    async def close(self, *, code, reason=""):
        self.closed = (code, reason)


def _patch_oai(monkeypatch, oai):
    monkeypatch.setattr(oar.OpenAIRealtimeBridge, "_connect", lambda self, model: oai)
    monkeypatch.setattr(oar, "openai_realtime_available", lambda settings: True)
    monkeypatch.setattr("akana_server.observability.begin_turn", lambda *a, **k: None)
    monkeypatch.setattr(oar, "build_memory_snapshot", lambda *a, **k: "")


def _record_persist(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_user_turn",
        lambda **kw: calls.append(("user", kw.get("user_text"))) or "uid",
    )
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_assistant_turn",
        lambda **kw: calls.append(("assistant", kw.get("assistant_text"))) or "aid",
    )
    return calls


def _run_oai(tmp_path, ws, oai):
    bridge = oar.OpenAIRealtimeBridge(ws, _settings(tmp_path), app=_fake_app(), conv_id="c")
    asyncio.run(bridge.run())
    return bridge


# --- be-voice-1 ------------------------------------------------------------


def test_barge_in_before_late_user_transcript_no_out_buf_carryover(tmp_path, monkeypatch) -> None:
    """be-voice-1 REGRESSION: the user barges in BEFORE their own async input
    transcription lands. At barge time _in_buf is empty and _out_buf holds the
    interrupted reply fragment; the orphan-guarded persist is a no-op that leaves
    _out_buf intact. The fragment must be dropped so it does not prepend to the NEXT
    turn's assistant text (corrupt record user='B-question'/assistant='A-partial B-reply')."""
    calls = _record_persist(monkeypatch)
    oai = _FakeOAIWS(
        [
            # Turn A opens and starts answering (its input transcript is still pending).
            {"type": "input_audio_buffer.speech_started"},
            {"type": "response.created"},
            {"type": "response.audio_transcript.delta", "delta": "A-partial-reply "},
            # Impatient barge-in for turn B, BEFORE A's transcript lands.
            {"type": "input_audio_buffer.speech_started"},
            {"type": "response.done", "response": {"status": "cancelled"}},
            # A's transcript lands late (belongs to the interrupted turn A).
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": "A-question"},
            # Turn B answers normally.
            {"type": "response.created"},
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": "B-question"},
            {"type": "response.audio_transcript.delta", "delta": "B-reply"},
            {"type": "response.done"},
        ],
        block_after=False,
    )
    _patch_oai(monkeypatch, oai)
    ws = _FakeWS([])
    _run_oai(tmp_path, ws, oai)
    assistant_texts = [v for (role, v) in calls if role == "assistant"]
    # The interrupted fragment must NOT be prepended to B's reply.
    assert "A-partial-reply B-reply" not in assistant_texts
    assert assistant_texts == ["B-reply"]


# --- be-voice-3 ------------------------------------------------------------


def test_failed_response_does_not_mispair_stale_question(tmp_path, monkeypatch) -> None:
    """be-voice-3 REGRESSION: turn A's response.done has status 'failed' (no output).
    The stale _in_buf ('A-question') must be cleared so it cannot pair with turn B's
    reply. Currently the failed turn's question is retained and the next turn persists
    (user='A-question', assistant='B-reply') while B's real question is lost."""
    calls = _record_persist(monkeypatch)
    oai = _FakeOAIWS(
        [
            # Turn A: transcript arrives, but the response FAILS with no output.
            {"type": "input_audio_buffer.speech_started"},
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": "A-question"},
            {"type": "response.created"},
            {"type": "response.done", "response": {"status": "failed"}},
            # Turn B: a short answer completes before B's async transcript lands.
            {"type": "input_audio_buffer.speech_started"},
            {"type": "response.created"},
            {"type": "response.audio_transcript.delta", "delta": "B-reply"},
            {"type": "response.done"},
            # B's transcript lands late.
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": "B-question"},
        ],
        block_after=False,
    )
    _patch_oai(monkeypatch, oai)
    ws = _FakeWS([])
    _run_oai(tmp_path, ws, oai)
    user_texts = [v for (role, v) in calls if role == "user"]
    assistant_texts = [v for (role, v) in calls if role == "assistant"]
    # B's reply must pair with B's own question; A's failed question is dropped, never
    # cross-paired with B's reply.
    assert assistant_texts == ["B-reply"]
    assert user_texts == ["B-question"]
    assert "A-question" not in user_texts


# --- be-voice-2 (Gemini) ---------------------------------------------------


class _T:
    def __init__(self, text: str) -> None:
        self.text = text


class _SC:
    def __init__(self, *, inp=None, out=None, turn_complete=False, interrupted=False) -> None:
        self.input_transcription = _T(inp) if inp is not None else None
        self.output_transcription = _T(out) if out is not None else None
        self.turn_complete = turn_complete
        self.interrupted = interrupted


class _Resp:
    def __init__(self, *, data=None, server_content=None, tool_call=None) -> None:
        self.data = data
        self.server_content = server_content
        self.tool_call = tool_call


class _FakeSession:
    def __init__(self, responses, *, block_after=False) -> None:
        self._responses = responses
        self._block_after = block_after
        self._drained = False
        self.sent_audio: list = []
        self.tool_responses: list = []

    async def send_realtime_input(self, *, audio):
        self.sent_audio.append(audio)

    async def send_tool_response(self, *, function_responses):
        self.tool_responses.append(function_responses)

    async def receive(self):
        if not self._drained:
            self._drained = True
            for r in self._responses:
                yield r
        if self._block_after:
            await asyncio.Event().wait()


class _FakeConnectCM:
    def __init__(self, session, capture) -> None:
        self._session = session
        self._capture = capture

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    def __init__(self, session, capture) -> None:
        connect = lambda *, model, config: (  # noqa: E731
            capture.update(model=model, config=config) or _FakeConnectCM(session, capture)
        )
        self.aio = SimpleNamespace(live=SimpleNamespace(connect=connect))


class _FakeMemory:
    def list_facts(self, *, min_trust=None, limit=50):
        return []

    def recall(self, query, *, conversation_id=None, limit=6, budget_tokens=1000):
        return SimpleNamespace(blocks=[])


def _patch_gemini(monkeypatch, session, capture):
    monkeypatch.setattr(gl, "make_client", lambda settings, **_kw: _FakeClient(session, capture))
    monkeypatch.setattr("akana_server.observability.begin_turn", lambda *a, **k: None)
    monkeypatch.setattr(
        "akana_server.memory_core.get_memory_core", lambda dd: _FakeMemory()
    )


def test_gemini_interrupted_noise_turn_no_out_buf_carryover(tmp_path, monkeypatch) -> None:
    """be-voice-2 REGRESSION: background noise triggers a spurious (input-less) reply,
    the user barges in ('interrupted' frame). The orphan-guarded persist leaves the
    junk _out_buf intact because only turn_complete had the one-sided drop — and Gemini
    sends NO turn_complete for an interrupted turn. The junk reply must be dropped so it
    does not merge into the next real turn (assistant='A-noise-reply B-reply')."""
    capture: dict = {}
    session = _FakeSession(
        [
            # Noise-triggered turn: assistant text only, no input transcription.
            _Resp(server_content=_SC(out="A-noise-reply ")),
            # User talks over it → interrupted (no turn_complete for this turn).
            _Resp(server_content=_SC(interrupted=True)),
            # The user's real next turn.
            _Resp(server_content=_SC(inp="B-question")),
            _Resp(server_content=_SC(out="B-reply")),
            _Resp(server_content=_SC(turn_complete=True)),
        ]
    )
    _patch_gemini(monkeypatch, session, capture)
    calls = _record_persist(monkeypatch)
    ws = _FakeWS([])
    bridge = gl.LiveBridge(ws, _settings(tmp_path), app=_fake_app(), conv_id="c")
    asyncio.run(bridge.run())
    user_texts = [v for (role, v) in calls if role == "user"]
    assistant_texts = [v for (role, v) in calls if role == "assistant"]
    assert user_texts == ["B-question"]
    assert assistant_texts == ["B-reply"]  # NOT "A-noise-reply B-reply"
