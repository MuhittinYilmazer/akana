"""Blitz 4 — voice-e2e area: OpenAI Realtime cross-turn transcript pairing.

voice-e2e-2: _flush_pending_assistant pairs the NEXT turn's already-buffered
transcription DELTAS with the deferred previous turn's reply. The deferred-turn
guards protected only the item-id tracking (_track_input_item), not the text
buffer that input_audio_transcription.delta appends to (_emit_transcript). This
reproduces the delta path the item-id machinery does not cover.

Hermetic — reuses the fake-WS harness style of tests/unit/test_openai_realtime.py."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from akana_server.events import EventHub
from akana_server.voice import openai_realtime as oar


def _settings(tmp_path):
    return SimpleNamespace(data_dir=tmp_path, primary_lang="tr")


class _FakeOAIWS:
    def __init__(self, events, *, block_after=True) -> None:
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


def test_next_turn_delta_not_cross_paired_with_deferred_reply(tmp_path, monkeypatch) -> None:
    """voice-e2e-2: turn A defers (response.done before its transcript) and A's completed
    NEVER arrives. Turn B's transcription DELTAS then land before B's response.created.
    The flush must still use the "[voice]" placeholder for A (not pair A's reply with B's
    partial question). Before the fix, B's delta sat in _in_buf and the flush cross-paired
    ('B-partial', 'A-reply')."""
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
            # Turn A: assistant text, response.done BEFORE A's user transcript → deferred.
            {"type": "response.audio_transcript.delta", "delta": "A-reply"},
            {"type": "response.done"},
            # A's completed NEVER arrives. Turn B's transcription DELTA lands next...
            {"type": "conversation.item.input_audio_transcription.delta", "delta": "B-partial"},
            # ...before B's response.created flushes the deferred turn A.
            {"type": "response.created"},
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
    pairs = list(zip(user_texts, assistant_texts))
    # The core defect: A's deferred reply must never be paired with the NEXT turn's partial.
    assert ("B-partial", "A-reply") not in pairs, pairs
    # A is flushed with its placeholder against its own reply.
    assert ("[voice]", "A-reply") in pairs, pairs


def test_deferred_then_matched_completed_still_pairs_correctly(tmp_path, monkeypatch) -> None:
    """Regression guard for the matched-restore path: turn A defers, then A's OWN completed
    arrives (matched) — the diverted-delta bookkeeping must not corrupt the pairing."""
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
            {"type": "response.audio_transcript.delta", "delta": "cevap1"},
            {"type": "response.done"},
            {"type": "conversation.item.input_audio_transcription.completed", "transcript": "soru1"},
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
    assert assistant_texts == ["cevap1", "cevap2"]
