"""Gemini Live bridge — full-duplex native-audio voice chat (Phase 2).

When ``provider==gemini`` is selected, the voice-chat button switches from the
turn-based ``/voice`` to this WS bridge (``/ws/voice/live``): the browser mic
streams PCM16@16k, the Gemini Live native-audio response streams back as PCM@24k;
there is NO Whisper/TTS in between (real-time, gapless, barge-in capable).

Design — an Akana-fed version of the JARVIS ``LiveBridge``:
- The client is set up via ``gemini_shared.make_client(settings, live=True)``
  (``v1alpha`` preview); if ``None``, the WS closes cleanly with ``close(1011)``
  (never a raw blow-up).
- Bidirectional pump: ``_from_browser`` (WS→Gemini audio) + ``_from_gemini``
  (Gemini→WS audio + transcript). When one finishes (browser disconnect / stream
  end), the other is cancelled.
- On ``turn_complete`` the input/output transcripts are **actually persisted**
  (``turn_writer`` — same ``memory.db`` path as text chat) + EventHub ``chat_done``.
- ``interrupted`` (barge-in) → ``{"type":"interrupt"}`` to the browser; playback drains.

The provider-neutral machinery (run/pump skeleton, orphan-guarded turn persistence,
EventHub broadcast, safe WS I/O, the browser framing) lives in
:mod:`akana_server.voice.realtime_base`; the session-prompt helpers
(``build_system_instruction`` / ``build_memory_snapshot`` / persona/directive
resolution) live in :mod:`akana_server.voice.session`. Both are re-exported here for
back-compat so existing importers keep working. The internal SDK touch points
(``connect``/``send_realtime_input``/``receive``) are isolated → tests run with
``FakeLiveSession``. The tool set (``memory_search`` + ``save_memory``) lives in the
``orchestrator/gemini_tools`` module — SHARED with the text surface.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from fastapi import WebSocket, WebSocketDisconnect

from akana_server.orchestrator.gemini_shared import (
    make_client,
    resolve_gemini_live_model,
    resolve_gemini_live_voice,
)
from akana_server.orchestrator.gemini_tools import (
    GEMINI_TOOL_DECLS,
    _function_response,
    dispatch_gemini_tool,
)
from akana_server.voice.realtime_base import (
    FRAME_AUDIO,
    RealtimeBridge,
    _off_loop,
    parse_browser_frame,
)

# Provider-neutral session helpers — re-exported here for back-compat so
# api/routes/voice*.py and openai_realtime keep importing from gemini_live until
# their owners migrate to akana_server.voice.session.
from akana_server.voice.session import (
    build_memory_snapshot,
    build_system_instruction,
    resolve_voice_directive,
    resolve_voice_persona_prefix,
)

if TYPE_CHECKING:
    from akana_server.config import Settings

log = logging.getLogger(__name__)

#: MIME of the audio sent to Gemini (the browser downsamples 48k→16k).
_INPUT_MIME = "audio/pcm;rate=16000"


# --- Pure helpers (testable without the SDK) -------------------------------


def build_live_config(settings: Settings, *, system_instruction: str) -> dict[str, Any]:
    """Plain dict for ``LiveConnectConfig`` (google-genai coerces it via pydantic).

    We pass a plain dict so there is no need to import the optional SDK's ``types``
    module (import-guard safe + pure testable). ``response_modalities=["AUDIO"]``
    native audio; ``input/output_audio_transcription`` raw transcript (for turn
    persistence); ``speech_config`` selects the preconfigured voice. Native
    function-calling (``memory_search`` + ``save_memory``, shared with the text
    surface) is enabled via the ``tools`` key.
    """
    return {
        "response_modalities": ["AUDIO"],
        "input_audio_transcription": {},
        "output_audio_transcription": {},
        "system_instruction": system_instruction,
        "speech_config": {
            "voice_config": {
                "prebuilt_voice_config": {
                    "voice_name": resolve_gemini_live_voice(settings)
                }
            }
        },
        # Native function-calling (memory_search + save_memory). The model calls them
        # when needed → LiveBridge dispatches → tool_response flows back.
        "tools": [{"function_declarations": GEMINI_TOOL_DECLS}],
    }


def _audio_blob(pcm: bytes) -> Any:
    """Audio bytes → google-genai ``Blob`` (when available) or a plain dict (fallback).

    When the SDK is installed returns the canonical ``types.Blob``; on test/coerce
    paths returns a plain dict — ``send_realtime_input(audio=...)`` accepts both.
    """
    try:  # pragma: no cover - SDK installed path
        from google.genai import types

        return types.Blob(data=pcm, mime_type=_INPUT_MIME)
    except Exception:  # pragma: no cover - SDK absent / version mismatch → dict fallback
        return {"data": pcm, "mime_type": _INPUT_MIME}


# --- Bridge -----------------------------------------------------------------


class LiveBridge(RealtimeBridge):
    """Single WS ↔ single Gemini Live session, bound to a ``conv_id``.

    Lifecycle in ``run()``: build client → open session → dual pump → clean close.
    """

    _broadcast_source = "voice_live"
    _label = "Gemini Live"

    def __init__(
        self, websocket: WebSocket, settings: Settings, *, app: Any, conv_id: str
    ) -> None:
        super().__init__(websocket, settings, app=app, conv_id=conv_id)
        self._session: Any = None  # active Live session (for tool_response)
        # Client is built lazily in _open_session so _available() can gate on it.
        self._client: Any = None

    def _available(self) -> bool:
        self._client = make_client(self.settings, live=True)
        return self._client is not None

    def _begin_turn_mode(self) -> str:
        return "voice_live"

    async def _open_session(self) -> None:
        model = resolve_gemini_live_model(self.settings)
        # Embed the session-start memory summary into system_instruction (off-loop DB).
        snapshot = await _off_loop(build_memory_snapshot, self.settings, self.conv_id)
        system_instruction = build_system_instruction(
            self.settings,
            memory_snapshot=snapshot,
            conv_id=self.conv_id,
            app=self.app,
        )
        config = build_live_config(self.settings, system_instruction=system_instruction)
        async with self._client.aio.live.connect(model=model, config=config) as session:
            self._session = session
            await self._send_json({"type": "ready", "conversation_id": self.conv_id})
            await self._pump(session)

    async def _from_browser(self, session: Any) -> None:
        """WS→Gemini: stream browser audio frames into the Live session."""
        while True:
            message = await self.ws.receive()
            if message.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect()
            data = message.get("bytes")
            if data is not None:
                tag, payload = parse_browser_frame(data)
                if tag == FRAME_AUDIO and payload:
                    await session.send_realtime_input(audio=_audio_blob(payload))
            # text messages (control) are ignored in Phase 2 — future: mute/end.

    async def _from_provider(self, session: Any) -> None:
        """Gemini→WS: forward audio bytes and transcripts to the browser.

        ``session.receive()`` is a PER-TURN async generator in the google-genai
        SDK: it yields a complete model turn and STOPS at ``turn_complete``, so it
        must be re-invoked for each subsequent turn (all official multi-turn Live
        examples wrap it in ``while True``). Re-invoke it as long as the session is
        live — but STOP when a ``receive()`` pass yields NOTHING: an open session
        blocks awaiting the next turn, so a zero-yield return means the session has
        closed (the generator is exhausted). Without that guard, a closed session
        would spin ``receive()`` forever (busy-loop) and this task would never end,
        deadlocking ``_pump``'s FIRST_COMPLETED wait.
        """
        while True:
            saw_any = False
            async for response in session.receive():
                saw_any = True
                await self._handle_response(response)
            if not saw_any:
                break  # session closed (generator exhausted) → end the pump task

    async def _handle_response(self, response: Any) -> None:
        sc = getattr(response, "server_content", None)
        interrupted = bool(getattr(sc, "interrupted", False)) if sc is not None else False
        # Barge-in FIRST: persist the interrupted turn BEFORE appending any new
        # user transcript from this frame — otherwise the interjecting user's words
        # would be written to the OLD turn (mis-attribution). Gemini does NOT send
        # turn_complete for an interrupted turn; the orphan-guard will not write on
        # the incomplete side anyway. Send the interrupt JSON first (stop playback).
        if interrupted:
            await self._send_json({"type": "interrupt"})
            await self._persist_turn()
        # Audio: do NOT play leftover TTS on an interrupted frame (that is the
        # cancelled response's queued output); for normal frames forward bytes immediately.
        audio = getattr(response, "data", None)
        if audio and not interrupted:
            await self._safe_send_bytes(audio)
        # If the model called a tool (memory_search), dispatch + send response back.
        tool_call = getattr(response, "tool_call", None)
        if tool_call is not None:
            await self._handle_tool_call(self._session, tool_call)
        if sc is None:
            return
        it = getattr(sc, "input_transcription", None)
        it_text = getattr(it, "text", None) if it is not None else None
        if it_text:
            # After an interrupt this is the start of the NEW turn (buffer was reset above).
            self._in_buf += it_text
            await self._send_json(
                {"type": "transcript", "role": "user", "text": it_text}
            )
        ot = getattr(sc, "output_transcription", None)
        ot_text = getattr(ot, "text", None) if ot is not None else None
        if ot_text:
            self._out_buf += ot_text
            await self._send_json(
                {"type": "transcript", "role": "assistant", "text": ot_text}
            )
        if getattr(sc, "turn_complete", False):
            await self._persist_turn()
            # turn_complete is the definitive turn boundary. When only one side was
            # transcribed (VAD triggered by noise so input is empty, or the
            # input-transcription stream dropped for this turn) _persist_turn is an
            # orphan no-op that leaves the transcribed side buffered — it would then
            # merge into the NEXT persisted turn's record. Drop the lingering one-sided
            # buffers here so a one-sided turn never carries over.
            self._in_buf = ""
            self._out_buf = ""
            await self._send_json({"type": "turn_complete"})

    async def _handle_tool_call(self, session: Any, tool_call: Any) -> None:
        """Model function calls → dispatch (off-loop) → ``send_tool_response``.

        Dispatch is DEFENSIVE (``dispatch_gemini_tool`` converts every error to text);
        the ``send_tool_response`` SDK call is isolated (tested with FakeLiveSession).
        Silently skips when session/tool is absent (never a raw crash)."""
        fcs = getattr(tool_call, "function_calls", None) or []
        responses: list[Any] = []
        for fc in fcs:
            name = getattr(fc, "name", "") or ""
            raw_args = getattr(fc, "args", None) or {}
            try:
                args = dict(raw_args)
            except (TypeError, ValueError):
                args = {}
            result = await _off_loop(
                dispatch_gemini_tool, self.settings, self.conv_id, name, args
            )
            responses.append(_function_response(fc, result))
            await self._send_json({"type": "tool", "name": name})
        if responses and session is not None:
            try:
                await session.send_tool_response(function_responses=responses)
            except Exception:  # pragma: no cover - SDK/network; must not cut audio
                log.warning("tool_response could not be sent", exc_info=True)


__all__ = [
    "FRAME_AUDIO",
    "GEMINI_TOOL_DECLS",
    "LiveBridge",
    "build_live_config",
    "build_memory_snapshot",
    "build_system_instruction",
    "dispatch_gemini_tool",
    "parse_browser_frame",
    "resolve_voice_directive",
    "resolve_voice_persona_prefix",
]
