"""Gemini Live bridge (Phase 2) — pure helpers + turn flow with FakeLiveSession.

Hermetic: NO real Live session/network — ``make_client`` is patched with a fake client
(``client.aio.live.connect`` → ``FakeLiveSession``); persist functions are replaced with
recorders. For ``PYTEST_DISABLE_PLUGIN_AUTOLOAD=1`` compatibility async driving is via
``asyncio.run``.

NOTE: ``_audio_blob``/``_function_response`` return real ``types.Blob``/``types.FunctionResponse``
if google-genai is INSTALLED, otherwise a plain dict. The tests therefore read fields via
:func:`_field` (dict OR object) — so whether or not the SDK is installed does not change the result."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from akana_server.events import EventHub
from akana_server.voice import gemini_live as gl


def _field(obj, key):
    """Read a field from a dict OR an object (types.* if the SDK is installed, else dict)."""
    return obj[key] if isinstance(obj, dict) else getattr(obj, key, None)


def _settings(tmp_path: Path):
    return SimpleNamespace(
        data_dir=tmp_path,
        primary_lang="tr",
        gemini_live_model="",
        gemini_live_voice="",
    )


# --- Pure helper tests -----------------------------------------------------


def test_build_system_instruction_has_time_persona_voice(tmp_path: Path) -> None:
    # English-first default (no runtime language set → "en"): EN labels + hint.
    si = gl.build_system_instruction(_settings(tmp_path))
    assert "[CURRENT TIME]" in si
    assert "[mode: voice_live]" in si
    assert "Akana" in si  # configured persona prefix embedded
    # Empty memory snapshot → no block (seam filled at session start).
    assert "[memory summary]" not in si
    si2 = gl.build_system_instruction(_settings(tmp_path), memory_snapshot="user likes cats")
    assert "[memory summary]" in si2 and "likes cats" in si2


def test_build_system_instruction_tr_labels(tmp_path: Path, monkeypatch) -> None:
    # language=="tr" → Turkish labels + Turkish voice hint (toggle-driven i18n).
    # _voice_language + build_system_instruction now live in voice.session; patch
    # there (gemini_live re-exports build_system_instruction for back-compat).
    from akana_server.voice import session as vsession

    monkeypatch.setattr(vsession, "_voice_language", lambda s: "tr")
    si = gl.build_system_instruction(
        _settings(tmp_path), persona_prefix="PERSONA", memory_snapshot="kedi"
    )
    assert "[ŞU ANKİ ZAMAN]" in si
    assert "[hafıza özeti]" in si
    assert "Gerçek-zamanlı SESLİ" in si  # TR voice hint


def test_voice_uses_configured_base_prompt_override(tmp_path: Path) -> None:
    # The core requirement: a user-customized base_prompt (core) reaches the
    # voice system instruction — same source the text chat path uses.
    from akana_server.persona.registry import (
        get_persona_registry,
        reset_persona_registries,
    )

    reset_persona_registries()
    try:
        get_persona_registry(tmp_path).set_base_prompt("CEKIRDEK-OVERRIDE-XYZ")
        prefix = gl.resolve_voice_persona_prefix(_settings(tmp_path))
        assert "CEKIRDEK-OVERRIDE-XYZ" in prefix
        si = gl.build_system_instruction(_settings(tmp_path))
        assert "CEKIRDEK-OVERRIDE-XYZ" in si
    finally:
        reset_persona_registries()


def test_build_live_config_audio_voice_no_tools(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    cfg = gl.build_live_config(s, system_instruction="SİSTEM")
    assert cfg["response_modalities"] == ["AUDIO"]
    assert cfg["system_instruction"] == "SİSTEM"
    assert "input_audio_transcription" in cfg
    assert "output_audio_transcription" in cfg
    voice = cfg["speech_config"]["voice_config"]["prebuilt_voice_config"]["voice_name"]
    assert voice == "Charon"  # default (setting empty)
    assert "tools" in cfg  # Phase 3: the memory_search function-declaration was added


def test_build_live_config_honors_voice_setting(tmp_path: Path) -> None:
    s = _settings(tmp_path)
    s.gemini_live_voice = "Puck"
    cfg = gl.build_live_config(s, system_instruction="x")
    assert cfg["speech_config"]["voice_config"]["prebuilt_voice_config"]["voice_name"] == "Puck"


def test_parse_browser_frame() -> None:
    assert gl.parse_browser_frame(bytes([gl.FRAME_AUDIO, 9, 8, 7])) == (1, b"\x09\x08\x07")
    assert gl.parse_browser_frame(b"") == (-1, b"")
    # the tag is preserved (so the caller can distinguish and ignore non-audio flags)
    assert gl.parse_browser_frame(bytes([0x02, 1])) == (2, b"\x01")


# --- Fake Live session + WS + client ---------------------------------------


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


# --- Fake memory (snapshot + memory_search dispatch tests) ------------------


class _FakeFact:
    def __init__(self, key: str, value: str) -> None:
        self.key = key
        self.value = value


class _FakeBlock:
    def __init__(self, text: str, score: float = 1.0) -> None:
        self.text = text
        self.score = score


class _FakeRecall:
    def __init__(self, blocks) -> None:
        self.blocks = blocks


class _FakeMemory:
    def __init__(self, *, facts=None, blocks=None) -> None:
        self._facts = facts or []
        self._blocks = blocks or []

    def list_facts(self, *, min_trust=None, limit=50):
        return self._facts[:limit]

    def recall(self, query, *, conversation_id=None, limit=6, budget_tokens=1000):
        return _FakeRecall(self._blocks)


class _FakeSession:
    """Fake google-genai Live session.

    Models the real SDK's PER-TURN ``receive()`` contract: it yields a complete
    model turn and STOPS. The pump re-invokes ``receive()`` for the next turn. So
    the first ``receive()`` call yields all queued responses (the recorded turn[s])
    and returns; a subsequent call yields NOTHING — signalling the session has
    closed (an OPEN session between turns would block instead, which
    ``block_after=True`` models). A zero-yield ``receive()`` is how ``_from_gemini``
    detects session-close and ends its pump task (instead of spinning forever).
    """

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
            await asyncio.Event().wait()  # keep the session open (until cancelled)
        # subsequent calls yield nothing → session closed (real per-turn semantics)


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


class _FakeWS:
    def __init__(self, incoming) -> None:
        self._incoming = list(incoming)
        self.sent_json: list = []
        self.sent_bytes: list = []
        self.closed = None

    async def receive(self):
        if self._incoming:
            return self._incoming.pop(0)
        await asyncio.Event().wait()  # browser is silent → block until cancelled

    async def send_json(self, payload):
        self.sent_json.append(payload)

    async def send_bytes(self, data):
        self.sent_bytes.append(data)

    async def close(self, *, code, reason=""):
        self.closed = (code, reason)


def _fake_app(hub: EventHub | None = None):
    state = SimpleNamespace(event_hub=hub, conversation_service=None)
    return SimpleNamespace(state=state)


def _patch_common(monkeypatch, session, capture, *, memory=None):
    monkeypatch.setattr(gl, "make_client", lambda settings, **_kw: _FakeClient(session, capture))
    monkeypatch.setattr("akana_server.observability.begin_turn", lambda *a, **k: None)
    # Hermetic: the session-start snapshot must NOT touch the real memory.db (run() now
    # calls build_memory_snapshot). Default empty fake → snapshot "".
    monkeypatch.setattr(
        "akana_server.memory_core.get_memory_core",
        lambda dd: memory if memory is not None else _FakeMemory(),
    )


# --- Bridge stream tests ---------------------------------------------------


def test_turn_complete_persists_user_and_assistant(tmp_path, monkeypatch) -> None:
    capture: dict = {}
    session = _FakeSession(
        [
            _Resp(server_content=_SC(inp="merhaba")),
            _Resp(data=b"\x01\x02", server_content=_SC(out="selam, nasılsın")),
            _Resp(server_content=_SC(turn_complete=True)),
        ]
    )
    _patch_common(monkeypatch, session, capture)
    calls: list = []
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_user_turn",
        lambda **kw: calls.append(("user", kw)) or "uid-1",
    )
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_assistant_turn",
        lambda **kw: calls.append(("assistant", kw)) or "aid-1",
    )
    hub = EventHub()
    broadcasts: list = []

    async def _rec(payload):
        broadcasts.append(payload)

    monkeypatch.setattr(hub, "broadcast_json", _rec)

    ws = _FakeWS([])  # browser silent → _from_gemini finishes first (persist happens)
    bridge = gl.LiveBridge(ws, _settings(tmp_path), app=_fake_app(hub), conv_id="conv-x")
    asyncio.run(bridge.run())

    roles = [c[0] for c in calls]
    assert roles == ["user", "assistant"]
    assert calls[0][1]["user_text"] == "merhaba"
    assert calls[1][1]["assistant_text"] == "selam, nasılsın"
    assert calls[1][1]["user_turn_id"] == "uid-1"
    # the config was forwarded to Gemini (model + AUDIO)
    assert capture["config"]["response_modalities"] == ["AUDIO"]
    # audio was forwarded to the browser + ready/transcript/turn_complete JSON messages
    assert ws.sent_bytes == [b"\x01\x02"]
    types_sent = [m["type"] for m in ws.sent_json]
    assert "ready" in types_sent and "turn_complete" in types_sent
    assert any(m["type"] == "transcript" and m["role"] == "user" for m in ws.sent_json)
    # EventHub chat_done broadcast
    assert broadcasts and broadcasts[-1]["type"] == "chat_done"
    assert broadcasts[-1]["source"] == "voice_live"


def test_turn_complete_without_user_text_is_orphan_safe(tmp_path, monkeypatch) -> None:
    """Only assistant text (no user transcript) → NO turn is written."""
    capture: dict = {}
    session = _FakeSession(
        [
            _Resp(server_content=_SC(out="kendiliğinden konuşma")),
            _Resp(server_content=_SC(turn_complete=True)),
        ]
    )
    _patch_common(monkeypatch, session, capture)
    calls: list = []
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_user_turn",
        lambda **kw: calls.append("user") or "uid",
    )
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_assistant_turn",
        lambda **kw: calls.append("assistant") or "aid",
    )
    ws = _FakeWS([])
    bridge = gl.LiveBridge(ws, _settings(tmp_path), app=_fake_app(None), conv_id="c")
    asyncio.run(bridge.run())
    assert calls == []  # orphan guard: no pair → no write


def test_orphan_turn_complete_does_not_merge_into_next_turn(tmp_path, monkeypatch) -> None:
    """REGRESSION: an orphan turn (only assistant transcribed — VAD fired on noise) whose
    turn_complete is a no-op orphan persist must NOT leave _out_buf populated, or the stale
    reply merges into the NEXT persisted turn's assistant_text. The following full turn must
    persist with its OWN answer only, not 'A1-orphan A2-reply'."""
    capture: dict = {}
    session = _FakeSession(
        [
            # Orphan turn: assistant text, no input transcription, then turn_complete.
            _Resp(server_content=_SC(out="A1-orphan ")),
            _Resp(server_content=_SC(turn_complete=True)),
            # A real full turn follows.
            _Resp(server_content=_SC(inp="question2")),
            _Resp(server_content=_SC(out="A2-reply")),
            _Resp(server_content=_SC(turn_complete=True)),
        ]
    )
    _patch_common(monkeypatch, session, capture)
    calls: list = []
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_user_turn",
        lambda **kw: calls.append(("user", kw.get("user_text"))) or "uid",
    )
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_assistant_turn",
        lambda **kw: calls.append(("assistant", kw.get("assistant_text"))) or "aid",
    )
    ws = _FakeWS([])
    bridge = gl.LiveBridge(ws, _settings(tmp_path), app=_fake_app(None), conv_id="c")
    asyncio.run(bridge.run())
    user_texts = [v for (role, v) in calls if role == "user"]
    assistant_texts = [v for (role, v) in calls if role == "assistant"]
    # Exactly one persisted record (the orphan turn was dropped), the answer NOT merged.
    assert user_texts == ["question2"]
    assert assistant_texts == ["A2-reply"]  # NOT "A1-orphan A2-reply"


def test_interrupt_forwarded_to_browser(tmp_path, monkeypatch) -> None:
    capture: dict = {}
    session = _FakeSession([_Resp(server_content=_SC(interrupted=True))])
    _patch_common(monkeypatch, session, capture)
    ws = _FakeWS([])
    bridge = gl.LiveBridge(ws, _settings(tmp_path), app=_fake_app(None), conv_id="c")
    asyncio.run(bridge.run())
    assert any(m["type"] == "interrupt" for m in ws.sent_json)


def test_interrupt_persists_truncated_turn_and_resets_no_contamination(
    tmp_path, monkeypatch
) -> None:
    """REGRESSION: barge-in persists the interrupted turn and RESETS the buffers → the next
    turn is NOT contaminated by the partial transcript. Previously ``interrupted`` was only
    forwarded to the browser and the buffer was not reset; since turn_complete never arrived
    the partial 'cevap baş' leaked into the next turn's assistant text ('cevap başcevap iki')."""
    capture: dict = {}
    session = _FakeSession(
        [
            _Resp(server_content=_SC(inp="soru bir")),
            _Resp(data=b"\x01", server_content=_SC(out="cevap baş")),
            _Resp(server_content=_SC(interrupted=True)),  # barge-in → persist + reset (TR text below is input data)
            _Resp(server_content=_SC(inp="soru iki")),
            _Resp(server_content=_SC(out="cevap iki")),
            _Resp(server_content=_SC(turn_complete=True)),
        ]
    )
    _patch_common(monkeypatch, session, capture)
    calls: list = []
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_user_turn",
        lambda **kw: calls.append(("user", kw.get("user_text"))) or f"uid-{len(calls)}",
    )
    monkeypatch.setattr(
        "akana_server.orchestrator.turn_writer.persist_assistant_turn",
        lambda **kw: calls.append(("assistant", kw.get("assistant_text"))) or "aid",
    )
    ws = _FakeWS([])
    bridge = gl.LiveBridge(ws, _settings(tmp_path), app=_fake_app(None), conv_id="c")
    asyncio.run(bridge.run())
    user_texts = [v for (role, v) in calls if role == "user"]
    assistant_texts = [v for (role, v) in calls if role == "assistant"]
    # Two separate turns; the second assistant text is NOT contaminated by the partial "cevap baş".
    assert user_texts == ["soru bir", "soru iki"]
    assert assistant_texts == ["cevap baş", "cevap iki"]


def test_browser_audio_frame_forwarded_to_gemini(tmp_path, monkeypatch) -> None:
    capture: dict = {}
    # The session produces no response but stays open → _from_browser processes the frame.
    session = _FakeSession([], block_after=True)
    _patch_common(monkeypatch, session, capture)
    audio_frame = {"type": "websocket.receive", "bytes": bytes([gl.FRAME_AUDIO]) + b"PCMDATA"}
    disconnect = {"type": "websocket.disconnect"}
    ws = _FakeWS([audio_frame, disconnect])
    bridge = gl.LiveBridge(ws, _settings(tmp_path), app=_fake_app(None), conv_id="c")
    asyncio.run(bridge.run())
    assert session.sent_audio  # send_realtime_input was called
    blob = session.sent_audio[0]
    # types.Blob if the SDK is installed, else dict — read from both
    assert _field(blob, "data") == b"PCMDATA"
    assert _field(blob, "mime_type") == gl._INPUT_MIME


def test_client_unavailable_closes_1011(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(gl, "make_client", lambda settings, **_kw: None)
    ws = _FakeWS([])
    bridge = gl.LiveBridge(ws, _settings(tmp_path), app=_fake_app(None), conv_id="c")
    asyncio.run(bridge.run())
    assert ws.closed is not None and ws.closed[0] == 1011


# --- Phase 3: memory snapshot + memory_search tool --------------------------


def test_build_memory_snapshot_formats_facts(tmp_path, monkeypatch) -> None:
    fake = _FakeMemory(
        facts=[_FakeFact("ad", "Alice"), _FakeFact("içecek", "kahve"), _FakeFact("", "düz not")]
    )
    monkeypatch.setattr("akana_server.memory_core.get_memory_core", lambda dd: fake)
    snap = gl.build_memory_snapshot(_settings(tmp_path), "c")
    assert "- ad: Alice" in snap
    assert "- içecek: kahve" in snap
    assert "- düz not" in snap  # if the key is empty, a plain value line


def test_build_memory_snapshot_caps_chars(tmp_path, monkeypatch) -> None:
    fake = _FakeMemory(facts=[_FakeFact("k", "v" * 1000)])
    monkeypatch.setattr("akana_server.memory_core.get_memory_core", lambda dd: fake)
    snap = gl.build_memory_snapshot(_settings(tmp_path), "c", max_chars=50)
    assert len(snap) <= 51 and snap.endswith("…")


def test_build_memory_snapshot_defensive_on_error(tmp_path, monkeypatch) -> None:
    def boom(dd):
        raise RuntimeError("db down")

    monkeypatch.setattr("akana_server.memory_core.get_memory_core", boom)
    assert gl.build_memory_snapshot(_settings(tmp_path), "c") == ""  # never blows up


def test_build_live_config_includes_memory_search_tool(tmp_path) -> None:
    cfg = gl.build_live_config(_settings(tmp_path), system_instruction="x")
    decls = cfg["tools"][0]["function_declarations"]
    names = [d["name"] for d in decls]
    assert "memory_search" in names
    # parameter schema: query (required)
    ms = next(d for d in decls if d["name"] == "memory_search")
    assert "query" in ms["parameters"]["properties"]
    assert ms["parameters"]["required"] == ["query"]


def test_dispatch_gemini_tool_memory_search(tmp_path, monkeypatch) -> None:
    fake = _FakeMemory(blocks=[_FakeBlock("[user] kahve sever"), _FakeBlock("[assistant] not ettim")])
    monkeypatch.setattr("akana_server.memory_core.get_memory_core", lambda dd: fake)
    out = gl.dispatch_gemini_tool(_settings(tmp_path), "c", "memory_search", {"query": "kahve"})
    assert "kahve sever" in out
    # empty query + unknown tool → clean message (the session is not broken)
    assert "empty" in gl.dispatch_gemini_tool(_settings(tmp_path), "c", "memory_search", {"query": ""}).lower()
    assert "Unknown" in gl.dispatch_gemini_tool(_settings(tmp_path), "c", "nope", {})


def test_dispatch_gemini_tool_empty_recall(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("akana_server.memory_core.get_memory_core", lambda dd: _FakeMemory())
    out = gl.dispatch_gemini_tool(_settings(tmp_path), "c", "memory_search", {"query": "x"})
    assert "no matching records" in out.lower()


def test_dispatch_gemini_tool_defensive_on_error(tmp_path, monkeypatch) -> None:
    def boom(dd):
        raise RuntimeError("x")

    monkeypatch.setattr("akana_server.memory_core.get_memory_core", boom)
    out = gl.dispatch_gemini_tool(_settings(tmp_path), "c", "memory_search", {"query": "x"})
    assert "unavailable" in out  # error → clean text


def test_tool_call_dispatched_and_response_sent(tmp_path, monkeypatch) -> None:
    """The model emits a function-call → LiveBridge dispatches it + send_tool_response."""
    calls: list = []
    # _handle_tool_call now calls the shared dispatch_gemini_tool (a name imported from
    # gemini_tools) — patch that name in the gl namespace.
    monkeypatch.setattr(
        gl, "dispatch_gemini_tool", lambda s, c, n, a: calls.append((n, a)) or "RESULT"
    )
    fc = SimpleNamespace(name="memory_search", args={"query": "kahve"}, id="t1")
    session = _FakeSession([_Resp(tool_call=SimpleNamespace(function_calls=[fc]))])
    capture: dict = {}
    _patch_common(monkeypatch, session, capture)
    ws = _FakeWS([])
    bridge = gl.LiveBridge(ws, _settings(tmp_path), app=_fake_app(None), conv_id="c")
    asyncio.run(bridge.run())
    assert calls == [("memory_search", {"query": "kahve"})]
    assert session.tool_responses, "send_tool_response must be called"
    resp = session.tool_responses[0][0]  # function_responses[0] (types.* OR dict)
    assert _field(resp, "name") == "memory_search"
    assert _field(_field(resp, "response"), "result") == "RESULT"
    assert any(m.get("type") == "tool" for m in ws.sent_json)  # browser notification
