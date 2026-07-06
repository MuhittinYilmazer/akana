"""OpenAI Realtime bridge — raw WS event protocol → Akana wire/persist.

Hermetic: no real OpenAI WS. ``OpenAIRealtimeBridge._connect`` is patched with a fake WS
(``send`` captures events + async-iter drives fake event JSONs); the browser
WS (``_FakeWS``) is the same as in the gemini_live test. ``asyncio.run`` for
``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`` compatibility. The OpenAI twin of the ``test_gemini_live`` pattern."""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

from akana_server.events import EventHub
from akana_server.orchestrator import openai_shared
from akana_server.voice import openai_realtime as oar


def _settings(tmp_path):
    return SimpleNamespace(data_dir=tmp_path, primary_lang="tr")


# --- Fake OpenAI Realtime WS + browser WS --------------------------------


class _FakeOAIWS:
    """OpenAI Realtime WS twin: ``send`` captures events, async-iter drives events.

    ``_connect`` returns this → ``async with`` (``__aenter__`` returns self) +
    ``async for raw in oai`` (__aiter__) + ``await oai.send(json)``."""

    def __init__(self, events, *, block_after=True) -> None:
        self._events = [json.dumps(e) for e in events]
        self._block_after = block_after
        self.sent: list = []  # sent events (parsed)

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
                await asyncio.Event().wait()  # keep the session open (until cancellation)

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


def _fake_app(hub: EventHub | None = None):
    return SimpleNamespace(state=SimpleNamespace(event_hub=hub, conversation_service=None))


def _patch_common(monkeypatch, oai):
    monkeypatch.setattr(oar.OpenAIRealtimeBridge, "_connect", lambda self, model: oai)
    monkeypatch.setattr(oar, "openai_realtime_available", lambda settings: True)
    monkeypatch.setattr("akana_server.observability.begin_turn", lambda *a, **k: None)
    monkeypatch.setattr(oar, "build_memory_snapshot", lambda *a, **k: "")


def _run(tmp_path, ws, oai):
    bridge = oar.OpenAIRealtimeBridge(ws, _settings(tmp_path), app=_fake_app(None), conv_id="c")
    asyncio.run(bridge.run())
    return bridge


# --- Pure helpers -------------------------------------------------------


def test_realtime_tools_flat_format() -> None:
    """OPENAI_TOOL_DECLS (Chat: {type, function:{...}}) → Realtime FLAT {type, name, ...}."""
    tools = oar._realtime_tools()
    assert tools and all(t["type"] == "function" for t in tools)
    assert all("name" in t and "function" not in t for t in tools)
    names = {t["name"] for t in tools}
    assert "memory_search" in names and "save_memory" in names


def test_build_session_update_shape(tmp_path) -> None:
    """BETA (default model gpt-4o-realtime-preview): FLAT session shape."""
    ev = oar.build_session_update(_settings(tmp_path), instructions="Sen Akana'sın")
    assert ev["type"] == "session.update"
    s = ev["session"]
    assert s["instructions"] == "Sen Akana'sın"
    assert s["input_audio_format"] == "pcm16" and s["output_audio_format"] == "pcm16"
    assert s["turn_detection"]["type"] == "server_vad"
    assert s["modalities"] == ["audio", "text"]
    assert isinstance(s["tools"], list) and s["tools"]
    assert "audio" not in s and "type" not in s  # NOT the GA nested shape


def test_build_session_update_ga_nested_shape(tmp_path, monkeypatch) -> None:
    """GA (gpt-realtime): NESTED session shape — type=realtime + audio.input/output;
    the BETA flat ``input/output_audio_format`` + ``modalities`` fields are ABSENT."""
    monkeypatch.setattr(oar, "resolve_openai_realtime_model", lambda settings: "gpt-realtime")
    ev = oar.build_session_update(_settings(tmp_path), instructions="Sen Akana'sın")
    assert ev["type"] == "session.update"
    s = ev["session"]
    assert s["type"] == "realtime"
    assert s["instructions"] == "Sen Akana'sın"
    # Flat BETA fields MUST NOT be present in GA
    assert "modalities" not in s
    assert "input_audio_format" not in s and "output_audio_format" not in s
    # Nested audio config
    audio_in = s["audio"]["input"]
    audio_out = s["audio"]["output"]
    assert audio_in["format"] == {"type": "audio/pcm", "rate": 24000}
    assert audio_out["format"] == {"type": "audio/pcm", "rate": 24000}
    assert audio_in["transcription"]["model"] == "gpt-4o-mini-transcribe"
    assert audio_in["turn_detection"]["type"] == "server_vad"
    assert audio_out["voice"]  # on the audio output side
    # Tools (flat fmt) + tool_choice common to both generations
    assert isinstance(s["tools"], list) and s["tools"]
    assert s["tool_choice"] == "auto"


def test_parse_browser_frame() -> None:
    assert oar.parse_browser_frame(b"") == (-1, b"")
    assert oar.parse_browser_frame(bytes([1]) + b"abc") == (1, b"abc")


def test_is_ga_realtime_model() -> None:
    """GA family (gpt-realtime / gpt-realtime-*) True; BETA gpt-4o-realtime-preview False."""
    assert openai_shared.is_ga_realtime_model("gpt-realtime") is True
    assert openai_shared.is_ga_realtime_model("gpt-realtime-2025-08-28") is True
    assert openai_shared.is_ga_realtime_model("gpt-4o-realtime-preview") is False
    assert openai_shared.is_ga_realtime_model("") is False


def test_realtime_headers_omits_beta_for_ga(tmp_path, monkeypatch) -> None:
    """GA model → ``OpenAI-Beta`` header is OMITTED; BETA/None → added (backward-compat)."""
    monkeypatch.setattr(openai_shared, "resolve_openai_key", lambda settings: "k")
    settings = _settings(tmp_path)
    # GA: no beta header
    ga = openai_shared.realtime_headers(settings, "gpt-realtime")
    assert "OpenAI-Beta" not in ga and ga["Authorization"] == "Bearer k"
    # BETA model: beta header present
    beta = openai_shared.realtime_headers(settings, "gpt-4o-realtime-preview")
    assert beta["OpenAI-Beta"] == "realtime=v1"
    # when no model is given (backward-compat) the beta header is added
    legacy = openai_shared.realtime_headers(settings)
    assert legacy["OpenAI-Beta"] == "realtime=v1"


# --- Bridge flow -----------------------------------------------------------


def test_session_update_and_ready_on_connect(tmp_path, monkeypatch) -> None:
    """On connect, session.update is sent FIRST + a ready JSON to the browser."""
    oai = _FakeOAIWS([], block_after=False)
    _patch_common(monkeypatch, oai)
    ws = _FakeWS([])
    _run(tmp_path, ws, oai)
    assert oai.sent and oai.sent[0]["type"] == "session.update"
    types_sent = [m["type"] for m in ws.sent_json]
    assert "ready" in types_sent


def test_browser_audio_to_input_audio_buffer_append(tmp_path, monkeypatch) -> None:
    """Browser [0x01]+pcm frame → OpenAI ``input_audio_buffer.append`` (base64)."""
    oai = _FakeOAIWS([], block_after=True)
    _patch_common(monkeypatch, oai)
    ws = _FakeWS(
        [
            {"type": "websocket.receive", "bytes": bytes([oar.FRAME_AUDIO]) + b"PCM!"},
            {"type": "websocket.disconnect"},
        ]
    )
    _run(tmp_path, ws, oai)
    appends = [e for e in oai.sent if e["type"] == "input_audio_buffer.append"]
    assert len(appends) == 1
    assert base64.b64decode(appends[0]["audio"]) == b"PCM!"


def test_audio_delta_forwarded_to_browser_bytes(tmp_path, monkeypatch) -> None:
    """``response.audio.delta`` (base64) → raw PCM bytes to the browser."""
    b64 = base64.b64encode(b"\x01\x02\x03").decode("ascii")
    oai = _FakeOAIWS([{"type": "response.audio.delta", "delta": b64}], block_after=False)
    _patch_common(monkeypatch, oai)
    ws = _FakeWS([])
    _run(tmp_path, ws, oai)
    assert ws.sent_bytes == [b"\x01\x02\x03"]


def test_transcripts_persisted_on_response_done(tmp_path, monkeypatch) -> None:
    """User (transcription.completed) + assistant (audio_transcript.delta) →
    on response.done BOTH are persisted."""
    calls: list = []
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_user_turn",
        lambda **kw: calls.append(("user", kw.get("user_text"))) or "uid",
    )
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_assistant_turn",
        lambda **kw: calls.append(("assistant", kw.get("assistant_text"))) or "aid",
    )
    oai = _FakeOAIWS(
        [
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "naber",
            },
            {"type": "response.audio_transcript.delta", "delta": "iyidir"},
            {"type": "response.done"},
        ],
        block_after=False,
    )
    _patch_common(monkeypatch, oai)
    ws = _FakeWS([])
    _run(tmp_path, ws, oai)
    assert calls == [("user", "naber"), ("assistant", "iyidir")]
    assert "turn_complete" in [m["type"] for m in ws.sent_json]


def test_speech_started_interrupts_and_resets_no_contamination(tmp_path, monkeypatch) -> None:
    """Barge-in (input_audio_buffer.speech_started): interrupted turn persisted + buffer reset
    → the next turn is not contaminated (the OpenAI twin of the gemini interrupted regression)."""
    calls: list = []
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_user_turn",
        lambda **kw: calls.append(("user", kw.get("user_text"))) or "uid",
    )
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_assistant_turn",
        lambda **kw: calls.append(("assistant", kw.get("assistant_text"))) or "aid",
    )
    oai = _FakeOAIWS(
        [
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": "soru1"},
            {"type": "response.audio_transcript.delta", "delta": "cevap baş"},
            {"type": "input_audio_buffer.speech_started"},  # barge-in → persist+reset
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": "soru2"},
            {"type": "response.audio_transcript.delta", "delta": "cevap iki"},
            {"type": "response.done"},
        ],
        block_after=False,
    )
    _patch_common(monkeypatch, oai)
    ws = _FakeWS([])
    _run(tmp_path, ws, oai)
    user_texts = [v for (role, v) in calls if role == "user"]
    assistant_texts = [v for (role, v) in calls if role == "assistant"]
    assert user_texts == ["soru1", "soru2"]
    assert assistant_texts == ["cevap baş", "cevap iki"]  # NOT "cevap başcevap iki"
    assert "interrupt" in [m["type"] for m in ws.sent_json]


def test_late_input_transcript_after_response_done_does_not_merge(tmp_path, monkeypatch) -> None:
    """Input transcription is async: ``transcription.completed`` can arrive AFTER
    ``response.done``. Turn 1 must persist correctly when the LATE transcript lands
    and must NOT merge into turn 2 (VB-4 regression: user='soru1'+'soru2' /
    assistant='cevap1'+'cevap2' as one corrupted record)."""
    calls: list = []
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_user_turn",
        lambda **kw: calls.append(("user", kw.get("user_text"))) or "uid",
    )
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_assistant_turn",
        lambda **kw: calls.append(("assistant", kw.get("assistant_text"))) or "aid",
    )
    oai = _FakeOAIWS(
        [
            # Turn 1: assistant text, then response.done BEFORE the user transcript.
            {"type": "response.audio_transcript.delta", "delta": "cevap1"},
            {"type": "response.done"},
            # Late user transcript for turn 1 → deferred persist fires here.
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": "soru1"},
            # Turn 2: normal ordering.
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": "soru2"},
            {"type": "response.audio_transcript.delta", "delta": "cevap2"},
            {"type": "response.done"},
        ],
        block_after=False,
    )
    _patch_common(monkeypatch, oai)
    ws = _FakeWS([])
    _run(tmp_path, ws, oai)
    user_texts = [v for (role, v) in calls if role == "user"]
    assistant_texts = [v for (role, v) in calls if role == "assistant"]
    assert user_texts == ["soru1", "soru2"]
    assert assistant_texts == ["cevap1", "cevap2"]  # NOT "cevap1cevap2"


def test_late_straggler_after_placeholder_flush_does_not_poison_next_turn(
    tmp_path, monkeypatch
) -> None:
    """VB-4 straggler: turn A defers (response.done before its transcript), then turn B's
    response.created flushes A with a placeholder. A's LATE transcript then arrives — it
    must be shown but NOT written into _in_buf, or it would pair with turn B's reply
    (corrupt cross-turn record). Turn B must persist with ITS OWN user text."""
    calls: list = []
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_user_turn",
        lambda **kw: calls.append(("user", kw.get("user_text"))) or "uid",
    )
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_assistant_turn",
        lambda **kw: calls.append(("assistant", kw.get("assistant_text"))) or "aid",
    )
    oai = _FakeOAIWS(
        [
            # Turn A: assistant text, then response.done BEFORE A's user transcript.
            {"type": "response.audio_transcript.delta", "delta": "A-reply"},
            {"type": "response.done"},
            # Turn B opens → A is flushed with a placeholder user text.
            {"type": "response.created"},
            # A's LATE transcript now arrives — a straggler of the already-flushed turn.
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": "A-transcript"},
            # Turn B's real transcript + reply.
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": "B-transcript"},
            {"type": "response.audio_transcript.delta", "delta": "B-reply"},
            {"type": "response.done"},
        ],
        block_after=False,
    )
    _patch_common(monkeypatch, oai)
    ws = _FakeWS([])
    _run(tmp_path, ws, oai)
    user_texts = [v for (role, v) in calls if role == "user"]
    assistant_texts = [v for (role, v) in calls if role == "assistant"]
    # A persisted with a placeholder user text (NOT "A-transcript"); B persisted with its
    # own user text. Crucially B is NOT paired with "A-transcript".
    assert assistant_texts == ["A-reply", "B-reply"]
    assert user_texts[0] == "[voice]"  # A flushed with the placeholder, not its straggler
    assert user_texts[1] == "B-transcript"  # B keeps its own user text
    assert "A-transcript" not in user_texts


def test_late_straggler_dropped_by_item_id(tmp_path, monkeypatch) -> None:
    """When the Realtime API supplies item_id, a straggler is recognised authoritatively:
    A's late completed carries A's item_id (already flushed) → dropped from _in_buf, so
    turn B (different item_id) is never mispaired even if events interleave."""
    calls: list = []
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_user_turn",
        lambda **kw: calls.append(("user", kw.get("user_text"))) or "uid",
    )
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_assistant_turn",
        lambda **kw: calls.append(("assistant", kw.get("assistant_text"))) or "aid",
    )
    oai = _FakeOAIWS(
        [
            {"type": "input_audio_buffer.speech_started", "item_id": "itemA"},
            {"type": "response.audio_transcript.delta", "delta": "A-reply"},
            {"type": "response.done"},
            {"type": "response.created"},  # flush A (item itemA now flushed)
            # A's late transcript carrying itemA → straggler, dropped.
            {"type": "conversation.item.input_audio_transcription.completed",
             "transcript": "A-transcript", "item_id": "itemA"},
            {"type": "input_audio_buffer.speech_started", "item_id": "itemB"},
            {"type": "conversation.item.input_audio_transcription.completed",
             "transcript": "B-transcript", "item_id": "itemB"},
            {"type": "response.audio_transcript.delta", "delta": "B-reply"},
            {"type": "response.done"},
        ],
        block_after=False,
    )
    _patch_common(monkeypatch, oai)
    ws = _FakeWS([])
    _run(tmp_path, ws, oai)
    user_texts = [v for (role, v) in calls if role == "user"]
    assistant_texts = [v for (role, v) in calls if role == "assistant"]
    assert assistant_texts == ["A-reply", "B-reply"]
    assert user_texts[0] == "[voice]"
    assert user_texts[1] == "B-transcript"
    assert "A-transcript" not in user_texts


def test_straggler_real_wire_order_speech_started_before_flush(tmp_path, monkeypatch) -> None:
    """REGRESSION (VB-4 real order): on the actual server_vad wire, turn B's speech_started
    (item_id=B) arrives BEFORE B's response.created flushes the deferred turn A. The straggler
    guard must key on A's OWN item_id (locked at deferral), not on whatever _pending_item_id
    the next turn's speech_started overwrote — otherwise the flush marks item B flushed, A's
    late transcript poisons B's user record, and B's real transcript is dropped."""
    calls: list = []
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_user_turn",
        lambda **kw: calls.append(("user", kw.get("user_text"))) or "uid",
    )
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_assistant_turn",
        lambda **kw: calls.append(("assistant", kw.get("assistant_text"))) or "aid",
    )
    oai = _FakeOAIWS(
        [
            # Turn A opens and answers; response.done fires BEFORE A's transcript.
            {"type": "input_audio_buffer.speech_started", "item_id": "itemA"},
            {"type": "response.created"},
            {"type": "response.audio_transcript.delta", "delta": "A-reply"},
            {"type": "response.done"},
            # Turn B's speech_started (its item_id) precedes B's response.created — the
            # real wire order that overwrites _pending_item_id if unguarded.
            {"type": "input_audio_buffer.speech_started", "item_id": "itemB"},
            {"type": "response.created"},  # flush A → must record itemA (not itemB) flushed
            # A's late transcript (itemA) → straggler, dropped.
            {"type": "conversation.item.input_audio_transcription.completed",
             "transcript": "A-transcript", "item_id": "itemA"},
            # B's real transcript + reply.
            {"type": "conversation.item.input_audio_transcription.completed",
             "transcript": "B-transcript", "item_id": "itemB"},
            {"type": "response.audio_transcript.delta", "delta": "B-reply"},
            {"type": "response.done"},
        ],
        block_after=False,
    )
    _patch_common(monkeypatch, oai)
    ws = _FakeWS([])
    _run(tmp_path, ws, oai)
    user_texts = [v for (role, v) in calls if role == "user"]
    assistant_texts = [v for (role, v) in calls if role == "assistant"]
    assert assistant_texts == ["A-reply", "B-reply"]
    assert user_texts == ["[voice]", "B-transcript"]  # NOT ['[voice]', 'A-transcript']
    assert "A-transcript" not in user_texts


def test_tool_call_turn_persists_once_and_single_turn_complete(tmp_path, monkeypatch) -> None:
    """REGRESSION: a Realtime tool call spans TWO responses (preamble+function_call, then the
    answer). It must persist as ONE record pairing the user question with the full answer, and
    emit turn_complete exactly ONCE — not split into (question, preamble) + ('[voice]', answer)
    with turn_complete fired mid-tool (which flips the browser orb to LISTENING early)."""
    monkeypatch.setattr(oar, "dispatch_llm_tool", lambda s, c, n, a: "TOOL-RESULT")
    calls: list = []
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_user_turn",
        lambda **kw: calls.append(("user", kw.get("user_text"))) or "uid",
    )
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_assistant_turn",
        lambda **kw: calls.append(("assistant", kw.get("assistant_text"))) or "aid",
    )
    oai = _FakeOAIWS(
        [
            # Response 1: user question, spoken preamble, then a function_call.
            {"type": "response.created"},
            {"type": "conversation.item.input_audio_transcription.completed",
             "transcript": "what is on my calendar"},
            {"type": "response.audio_transcript.delta", "delta": "Let me check."},
            {"type": "response.output_item.added",
             "item": {"type": "function_call", "call_id": "call_1", "name": "memory_search"}},
            {"type": "response.function_call_arguments.done", "call_id": "call_1", "arguments": "{}"},
            {"type": "response.done"},  # ends the preamble response — must NOT persist here
            # Response 2: the real answer.
            {"type": "response.created"},
            {"type": "response.audio_transcript.delta", "delta": "You have a dentist appointment."},
            {"type": "response.done"},
        ],
        block_after=False,
    )
    _patch_common(monkeypatch, oai)
    ws = _FakeWS([])
    _run(tmp_path, ws, oai)
    user_texts = [v for (role, v) in calls if role == "user"]
    assistant_texts = [v for (role, v) in calls if role == "assistant"]
    # ONE record: the question paired with the full assistant turn (preamble + answer).
    assert user_texts == ["what is on my calendar"]
    assert assistant_texts == ["Let me check.You have a dentist appointment."]
    assert "[voice]" not in user_texts
    # turn_complete emitted exactly once (at the end of the whole tool turn, not mid-tool).
    assert [m["type"] for m in ws.sent_json].count("turn_complete") == 1


def test_pending_assistant_flushed_with_placeholder_at_session_end(tmp_path, monkeypatch) -> None:
    """If the late user transcript NEVER arrives (dropped / session ends first), the
    assistant turn is rescued against a placeholder user text rather than lost."""
    calls: list = []
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_user_turn",
        lambda **kw: calls.append(("user", kw.get("user_text"))) or "uid",
    )
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_assistant_turn",
        lambda **kw: calls.append(("assistant", kw.get("assistant_text"))) or "aid",
    )
    oai = _FakeOAIWS(
        [
            {"type": "response.audio_transcript.delta", "delta": "yalnız cevap"},
            {"type": "response.done"},  # user transcript never comes; session then ends
        ],
        block_after=False,
    )
    _patch_common(monkeypatch, oai)
    ws = _FakeWS([])
    _run(tmp_path, ws, oai)
    assert ("assistant", "yalnız cevap") in calls
    # a (placeholder) user turn was written so the pair is complete, not orphaned
    assert any(role == "user" for (role, _v) in calls)


def test_fc_names_drained_after_function_call(tmp_path, monkeypatch) -> None:
    """The call_id→name map is single-use: it is popped after the call is handled so it
    does not grow unbounded across a long session (VB-6)."""
    monkeypatch.setattr(oar, "dispatch_llm_tool", lambda s, c, n, a: "ok")
    oai = _FakeOAIWS(
        [
            {
                "type": "response.output_item.added",
                "item": {"type": "function_call", "call_id": "call_x", "name": "memory_search"},
            },
            {
                "type": "response.function_call_arguments.done",
                "call_id": "call_x",
                "arguments": "{}",
            },
        ],
        block_after=False,
    )
    _patch_common(monkeypatch, oai)
    ws = _FakeWS([])
    bridge = _run(tmp_path, ws, oai)
    assert bridge._fc_names == {}


def test_function_call_dispatched_and_output_sent(tmp_path, monkeypatch) -> None:
    """function_call_arguments.done → dispatch → function_call_output + response.create.
    The name is tracked from ``output_item.added``; arguments are parsed from the JSON string."""
    dispatched: list = []
    monkeypatch.setattr(
        oar, "dispatch_llm_tool", lambda s, c, n, a: dispatched.append((n, a)) or "ARAÇ-SONUCU"
    )
    oai = _FakeOAIWS(
        [
            {
                "type": "response.output_item.added",
                "item": {"type": "function_call", "call_id": "call_1", "name": "memory_search"},
            },
            {
                "type": "response.function_call_arguments.done",
                "call_id": "call_1",
                "arguments": '{"query": "kahve"}',
            },
        ],
        block_after=False,
    )
    _patch_common(monkeypatch, oai)
    ws = _FakeWS([])
    _run(tmp_path, ws, oai)
    assert dispatched == [("memory_search", {"query": "kahve"})]
    out = [e for e in oai.sent if e["type"] == "conversation.item.create"]
    assert out and out[0]["item"]["type"] == "function_call_output"
    assert out[0]["item"]["call_id"] == "call_1"
    assert out[0]["item"]["output"] == "ARAÇ-SONUCU"
    assert any(e["type"] == "response.create" for e in oai.sent)
    assert any(m["type"] == "tool" and m["name"] == "memory_search" for m in ws.sent_json)


def test_unavailable_closes_1011(tmp_path, monkeypatch) -> None:
    """No key (openai_realtime_available False) → clean close(1011), does not connect."""
    monkeypatch.setattr(oar, "openai_realtime_available", lambda settings: False)
    ws = _FakeWS([])
    bridge = oar.OpenAIRealtimeBridge(ws, _settings(tmp_path), app=_fake_app(None), conv_id="c")
    asyncio.run(bridge.run())
    assert ws.closed and ws.closed[0] == 1011


def test_chat_done_broadcast_on_persist(tmp_path, monkeypatch) -> None:
    """When the turn is persisted, ``chat_done`` (source=voice_realtime) is broadcast to the EventHub."""
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_user_turn", lambda **kw: "uid"
    )
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_assistant_turn", lambda **kw: "aid"
    )
    hub = EventHub()
    broadcasts: list = []
    monkeypatch.setattr(hub, "broadcast_json", lambda p: broadcasts.append(p) or _async_none())
    oai = _FakeOAIWS(
        [
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": "x"},
            {"type": "response.audio_transcript.delta", "delta": "y"},
            {"type": "response.done"},
        ],
        block_after=False,
    )
    _patch_common(monkeypatch, oai)
    ws = _FakeWS([])
    bridge = oar.OpenAIRealtimeBridge(ws, _settings(tmp_path), app=_fake_app(hub), conv_id="c")
    asyncio.run(bridge.run())
    assert broadcasts and broadcasts[-1]["type"] == "chat_done"
    assert broadcasts[-1]["source"] == "voice_realtime"


async def _async_none():
    return None


def test_ga_event_names_handled(tmp_path, monkeypatch) -> None:
    """GA (gpt-realtime) event names are also handled: response.output_audio.delta →
    browser bytes; response.output_audio_transcript.delta → assistant transcript.
    (Alongside the Beta names; otherwise on a GA model audio/transcript would SILENTLY not flow.)"""
    b64 = base64.b64encode(b"\x09\x08").decode("ascii")
    oai = _FakeOAIWS(
        [
            {"type": "response.output_audio.delta", "delta": b64},
            {"type": "response.output_audio_transcript.delta", "delta": "merhaba"},
        ],
        block_after=False,
    )
    _patch_common(monkeypatch, oai)
    ws = _FakeWS([])
    _run(tmp_path, ws, oai)
    assert ws.sent_bytes == [b"\x09\x08"]
    assert any(
        m["type"] == "transcript" and m["role"] == "assistant" and m["text"] == "merhaba"
        for m in ws.sent_json
    )


def test_barge_in_sends_response_cancel(tmp_path, monkeypatch) -> None:
    """Barge-in: WHILE an in-flight response exists, ``response.cancel`` is sent to the model (so the
    interrupted response's deltas don't contaminate the next turn) + interrupt to the browser."""
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_user_turn", lambda **kw: "uid"
    )
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_assistant_turn", lambda **kw: "aid"
    )
    # response.created → response in-flight; the speech_started barge-in triggers the cancel.
    oai = _FakeOAIWS(
        [
            {"type": "response.created"},
            {"type": "input_audio_buffer.speech_started"},
        ],
        block_after=False,
    )
    _patch_common(monkeypatch, oai)
    ws = _FakeWS([])
    _run(tmp_path, ws, oai)
    assert any(e["type"] == "response.cancel" for e in oai.sent)
    assert any(m["type"] == "interrupt" for m in ws.sent_json)


def test_barge_in_no_cancel_when_no_active_response(tmp_path, monkeypatch) -> None:
    """server_vad emits speech_started on every turn; WHILE no in-flight response exists
    ``response.cancel`` is NOT sent (otherwise a per-turn spurious error from the model)."""
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_user_turn", lambda **kw: "uid"
    )
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_assistant_turn", lambda **kw: "aid"
    )
    oai = _FakeOAIWS([{"type": "input_audio_buffer.speech_started"}], block_after=False)
    _patch_common(monkeypatch, oai)
    ws = _FakeWS([])
    _run(tmp_path, ws, oai)
    assert not any(e["type"] == "response.cancel" for e in oai.sent)
    assert any(m["type"] == "interrupt" for m in ws.sent_json)  # interrupt is still sent
