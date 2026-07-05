"""Guard the voice-layer deduplication (voice:arch:0 / voice:arch:1).

Both realtime bridges must share the single ``RealtimeBridge`` base (so a
protocol/persistence fix is written once), and the provider-neutral session
helpers must live in ``voice.session`` (not in the Gemini-specific module), with
gemini_live keeping back-compat re-exports until the route owners migrate."""

from __future__ import annotations

from akana_server.voice import gemini_live, openai_realtime, session
from akana_server.voice.realtime_base import RealtimeBridge


def test_both_bridges_share_realtime_base() -> None:
    assert issubclass(gemini_live.LiveBridge, RealtimeBridge)
    assert issubclass(openai_realtime.OpenAIRealtimeBridge, RealtimeBridge)


def test_shared_persistence_and_broadcast_not_reimplemented() -> None:
    # The orphan-guarded persist + EventHub broadcast + conversation ensure + safe
    # WS I/O must be inherited, not re-defined on either subclass.
    for shared in (
        "_persist_turn",
        "_broadcast_done",
        "_ensure_conversation",
        "_pump",
        "_send_json",
        "_safe_send_bytes",
        "_safe_close",
    ):
        assert shared not in gemini_live.LiveBridge.__dict__, shared
        assert shared not in openai_realtime.OpenAIRealtimeBridge.__dict__, shared
        assert shared in RealtimeBridge.__dict__, shared


def test_broadcast_source_labels_distinct() -> None:
    assert gemini_live.LiveBridge._broadcast_source == "voice_live"
    assert openai_realtime.OpenAIRealtimeBridge._broadcast_source == "voice_realtime"


def test_frame_protocol_is_the_shared_one() -> None:
    # Both bridges re-export the SAME browser framing from realtime_base (no copy).
    from akana_server.voice import realtime_base as rb

    assert gemini_live.FRAME_AUDIO is rb.FRAME_AUDIO
    assert openai_realtime.FRAME_AUDIO is rb.FRAME_AUDIO
    assert gemini_live.parse_browser_frame is rb.parse_browser_frame
    assert openai_realtime.parse_browser_frame is rb.parse_browser_frame


def test_session_helpers_live_in_neutral_module() -> None:
    # build_system_instruction/build_memory_snapshot are defined in voice.session;
    # gemini_live and openai_realtime import (re-export) the SAME objects.
    assert gemini_live.build_system_instruction is session.build_system_instruction
    assert gemini_live.build_memory_snapshot is session.build_memory_snapshot
    assert openai_realtime.build_system_instruction is session.build_system_instruction
    assert openai_realtime.build_memory_snapshot is session.build_memory_snapshot


def test_gemini_live_keeps_backcompat_reexports() -> None:
    # Route files still import these from gemini_live until their owner migrates.
    for name in (
        "resolve_voice_persona_prefix",
        "resolve_voice_directive",
        "build_system_instruction",
        "build_memory_snapshot",
    ):
        assert getattr(gemini_live, name) is getattr(session, name), name
