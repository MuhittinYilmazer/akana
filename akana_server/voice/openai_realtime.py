"""OpenAI Realtime bridge — full-duplex audio (twin of ``gemini_live.LiveBridge``).

When ``provider==openai`` + ``openai_realtime_enabled``, the voice chat button switches
from the turn-based ``/voice`` endpoint to this WS bridge (``/ws/voice/realtime``):
the browser streams microphone PCM16@24k, and OpenAI Realtime native-audio responses
flow back as PCM16@24k; no Whisper/TTS in between (real-time, continuous, barge-in
supported).

DIFFERENCE from Gemini Live — PROTOCOL: connects to the OpenAI Realtime WS using raw
``websockets`` instead of the google-genai SDK; messages are JSON events
(``session.update`` / ``input_audio_buffer.append`` / ``response.audio.delta`` …),
audio is embedded as base64.
Browser↔Akana protocol (``[0x01]+pcm`` + transcript/interrupt/turn_complete JSON) is
IDENTICAL to Gemini → the bridge translates between the two protocols; the frontend
is nearly unchanged.

The provider-neutral machinery (run/pump skeleton, orphan-guarded persistence,
EventHub broadcast, safe WS I/O, browser framing) is inherited from
:class:`akana_server.voice.realtime_base.RealtimeBridge`; the session-prompt helpers
(``build_system_instruction`` / ``build_memory_snapshot``) are imported from
:mod:`akana_server.voice.session`. Isolated WS touch-point: ``_connect`` is the single
seam — tests run with a fake WS (``send`` + async-iter). Tool set from ``llm_tools``
(shared with the text surface)."""

from __future__ import annotations

import base64
import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import WebSocket, WebSocketDisconnect

from akana_server.orchestrator.llm_tools import OPENAI_TOOL_DECLS, dispatch_llm_tool
from akana_server.orchestrator.openai_shared import (
    is_ga_realtime_model,
    openai_realtime_available,
    realtime_headers,
    resolve_openai_realtime_model,
    resolve_openai_realtime_voice,
    resolve_realtime_url,
)
from akana_server.voice.realtime_base import (
    FRAME_AUDIO,
    RealtimeBridge,
    _off_loop,
    parse_browser_frame,
)
from akana_server.voice.session import (
    build_memory_snapshot,
    build_system_instruction,
)

if TYPE_CHECKING:
    from akana_server.config import Settings

log = logging.getLogger(__name__)


def _realtime_tools() -> list[dict[str, Any]]:
    """``OPENAI_TOOL_DECLS`` (Chat fmt ``{type, function:{...}}``) → Realtime fmt
    (FLAT ``{type, name, description, parameters}``). Realtime API tools do NOT use
    the nested ``function`` wrapper (difference from Chat Completions)."""
    out: list[dict[str, Any]] = []
    for d in OPENAI_TOOL_DECLS:
        fn = d.get("function") or {}
        out.append(
            {
                "type": "function",
                "name": fn.get("name", "") or "",
                "description": fn.get("description", "") or "",
                "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return out


def build_session_update(settings: Settings, *, instructions: str) -> dict[str, Any]:
    """``session.update`` event — session configuration based on the active model generation.

    BETA (``gpt-4o-realtime-preview``) and GA (``gpt-realtime``) have DIFFERENT ``session``
    shapes (event names are handled identically in the bridge but session config is
    branched MANUALLY): BETA is flat (``modalities`` + ``input/output_audio_format`` +
    ``input_audio_transcription``), GA is nested (``type:"realtime"`` +
    ``audio.input/output``). Tool set (``_realtime_tools``, flat fmt) + ``tool_choice``
    are shared across both generations; the wrong shape SILENTLY misconfigures the
    session in GA (audio/transcript will not flow)."""
    if is_ga_realtime_model(resolve_openai_realtime_model(settings)):
        return _build_session_update_ga(settings, instructions=instructions)
    return _build_session_update_beta(settings, instructions=instructions)


def _build_session_update_beta(settings: Settings, *, instructions: str) -> dict[str, Any]:
    """BETA (``gpt-4o-realtime-preview``) FLAT ``session.update``.

    ``modalities=[audio,text]`` native audio; ``input/output_audio_format=pcm16`` (24k);
    ``input_audio_transcription`` raw user transcript (for turn persistence);
    ``turn_detection=server_vad`` server detects end-of-speech (auto response); ``voice``
    preconfigured voice; ``tools`` native function-calling (memory_search + save_memory)."""
    return {
        "type": "session.update",
        "session": {
            "modalities": ["audio", "text"],
            "instructions": instructions,
            "voice": resolve_openai_realtime_voice(settings),
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "input_audio_transcription": {"model": "whisper-1"},
            "turn_detection": {"type": "server_vad"},
            "tools": _realtime_tools(),
            "tool_choice": "auto",
        },
    }


def _build_session_update_ga(settings: Settings, *, instructions: str) -> dict[str, Any]:
    """GA (``gpt-realtime``) NESTED ``session.update``.

    Instead of BETA's flat ``input/output_audio_format`` + ``modalities`` fields, GA
    requires ``session.type="realtime"`` and groups audio settings under
    ``audio.input``/``audio.output``: format is ``{type:"audio/pcm", rate:24000}``
    (not BETA's ``"pcm16"`` string), transcript model is ``gpt-4o-mini-transcribe``
    (not whisper-1), ``turn_detection`` on the input side; ``voice`` on the output side.
    Tools in flat fmt (``_realtime_tools``) + ``tool_choice`` remain shared with BETA."""
    fmt = {"type": "audio/pcm", "rate": 24000}
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": instructions,
            "audio": {
                "input": {
                    "format": fmt,
                    "transcription": {"model": "gpt-4o-mini-transcribe"},
                    "turn_detection": {"type": "server_vad"},
                },
                "output": {
                    "format": fmt,
                    "voice": resolve_openai_realtime_voice(settings),
                },
            },
            "tools": _realtime_tools(),
            "tool_choice": "auto",
        },
    }


# --- Bridge -----------------------------------------------------------------


class OpenAIRealtimeBridge(RealtimeBridge):
    """Single WS ↔ single OpenAI Realtime session, bound to a ``conv_id``.

    Same lifecycle as ``LiveBridge`` (run → connect → dual pump → close) but using the
    OpenAI Realtime JSON-event protocol. The ``_connect`` seam is isolated for
    testability (a fake WS can be injected)."""

    _broadcast_source = "voice_realtime"
    _label = "OpenAI Realtime"

    def __init__(
        self, websocket: WebSocket, settings: Settings, *, app: Any, conv_id: str
    ) -> None:
        super().__init__(websocket, settings, app=app, conv_id=conv_id)
        self._oai: Any = None  # active OpenAI Realtime WS
        self._fc_names: dict[str, str] = {}  # call_id → function name (from output_item.added)
        self._response_active = False  # whether an in-flight response exists (response.cancel gate)
        # A Realtime tool call spans TWO responses: response 1 carries the spoken preamble
        # + function_call (the bridge answers with function_call_output + response.create),
        # response 2 carries the actual answer. Set when we issue that continuation
        # response.create so the response.done ending response 1 does NOT persist a partial
        # turn or emit turn_complete mid-tool — both are deferred until the follow-up
        # response.done, and _out_buf accumulates across the two responses so the answer
        # pairs with the original user question (not split into two records).
        self._tool_continuation_pending = False
        # Assistant text of a response whose user transcript had NOT yet arrived at
        # response.done (input transcription runs async and can land AFTER the
        # response). Held here so the LATE input_audio_transcription.completed can
        # persist the turn correctly instead of the text leaking into _out_buf and
        # merging with the next turn. See _handle_response_done / _emit_user_completed.
        self._pending_assistant = ""
        # Turn identity for the late-transcript path. Input transcription is async and a
        # completed can arrive AFTER its turn was already resolved (persisted with a
        # placeholder by _flush_pending_assistant when the next turn started). Such a
        # STRAGGLER must NOT be written into _in_buf — otherwise it poisons the NEXT
        # turn's user text (VB-4 corruption: turn A's transcript paired with turn B's
        # reply). Two guards, item_id-first:
        #   • _flushed_item_ids — item_ids of turns already flushed with a placeholder;
        #     a completed carrying such an item_id is a straggler (authoritative when the
        #     Realtime API supplies item_id, which it does for real sessions).
        #   • _expect_straggler — a payload-agnostic fallback (e.g. the hermetic tests,
        #     which omit item_id): a placeholder flush arms a one-shot "the next unmatched
        #     completed belongs to the just-closed turn — drop it".
        self._pending_item_id = ""  # item_id of the turn currently being transcribed (best-effort)
        # Snapshot of the DEFERRED turn's item_id, captured at deferral time in
        # _handle_response_done. Distinct from _pending_item_id because the NEXT turn's
        # speech_started (which precedes its response.created on the real wire) overwrites
        # _pending_item_id before the deferred turn is flushed — so the flush would mark
        # the WRONG (next) turn's item as flushed, dropping its real transcript and
        # cross-pairing the closed turn's late transcript. This locked snapshot is what
        # _flush_pending_assistant records as flushed while an assistant is pending.
        self._deferred_item_id = ""
        self._flushed_item_ids: set[str] = set()
        self._expect_straggler = False  # a flush just closed a turn; the next unmatched completed is stale

    def _available(self) -> bool:
        return openai_realtime_available(self.settings)

    def _begin_turn_mode(self) -> str:
        return "voice_realtime"

    def _connect(self, model: str) -> Any:
        """OpenAI Realtime WS connection (async context manager). Tests patch this
        method to return a fake WS → runs without a network."""
        import websockets

        return websockets.connect(
            resolve_realtime_url(self.settings, model),
            # Pass ``model`` → the ``OpenAI-Beta`` header is omitted for GA
            # (``gpt-realtime``) (BETA requires it, GA rejects it); otherwise the GA WS
            # handshake would close due to that header.
            additional_headers=realtime_headers(self.settings, model),
            max_size=None,
        )

    async def _open_session(self) -> None:
        model = resolve_openai_realtime_model(self.settings)
        snapshot = await _off_loop(build_memory_snapshot, self.settings, self.conv_id)
        instructions = build_system_instruction(
            self.settings,
            memory_snapshot=snapshot,
            conv_id=self.conv_id,
            app=self.app,
        )
        async with self._connect(model) as oai:
            self._oai = oai
            await self._send_event(
                build_session_update(self.settings, instructions=instructions)
            )
            await self._send_json({"type": "ready", "conversation_id": self.conv_id})
            try:
                await self._pump(oai)
            finally:
                # Session ended: rescue the last turn if its assistant text is still
                # pending (the late user transcript never arrived — see
                # _handle_response_done). Otherwise it would be silently dropped.
                await self._flush_pending_assistant()

    async def _from_browser(self, oai: Any) -> None:
        """WS→OpenAI: convert browser audio frames into ``input_audio_buffer.append`` events."""
        while True:
            message = await self.ws.receive()
            if message.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect()
            data = message.get("bytes")
            if data is not None:
                tag, payload = parse_browser_frame(data)
                if tag == FRAME_AUDIO and payload:
                    await self._send_event(
                        {
                            "type": "input_audio_buffer.append",
                            "audio": base64.b64encode(payload).decode("ascii"),
                        }
                    )

    async def _from_provider(self, oai: Any) -> None:
        """OpenAI→WS: read Realtime events and dispatch as audio/transcript/tool."""
        async for raw in oai:
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):  # pragma: no cover - malformed frame is skipped
                continue
            if isinstance(event, dict):
                await self._handle_event(event)

    async def _handle_event(self, event: dict[str, Any]) -> None:
        etype = str(event.get("type") or "")
        # Accept both BETA (gpt-4o-realtime-preview: "response.audio.delta") and GA
        # (gpt-realtime: "response.output_audio.delta") event names → the bridge works
        # regardless of model generation (otherwise audio/transcript would SILENTLY
        # never flow on a GA model). Transcript arrives under two names in the same way.
        if etype in ("response.audio.delta", "response.output_audio.delta"):
            await self._emit_audio(event.get("delta"))
        elif etype in (
            "response.audio_transcript.delta",
            "response.output_audio_transcript.delta",
        ):
            await self._emit_transcript("assistant", event.get("delta"))
        elif etype == "conversation.item.input_audio_transcription.delta":
            # Track the item_id of the user turn currently being transcribed so a LATE
            # completed can be tied back to its turn (see _emit_user_completed).
            self._track_input_item(event.get("item_id"))
            await self._emit_transcript("user", event.get("delta"))
        elif etype == "conversation.item.input_audio_transcription.completed":
            await self._emit_user_completed(event.get("transcript"), event.get("item_id"))
        elif etype == "response.created":
            # A new response is starting. If a prior turn's assistant text is still
            # pending (its late user transcript never arrived), flush it now with a
            # placeholder user text rather than let it linger and merge into this new
            # turn — this also rescues the last turn when the session ends before the
            # transcript lands (end-of-session turn loss).
            await self._flush_pending_assistant()
            self._response_active = True
        elif etype == "response.output_item.added":
            item = event.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "function_call":
                self._fc_names[str(item.get("call_id") or "")] = str(item.get("name") or "")
        elif etype == "response.function_call_arguments.done":
            await self._handle_function_call(event)
        elif etype == "input_audio_buffer.speech_started":
            # A fresh user utterance opens: track its item_id (if any) as the current
            # turn's identity for the late-transcript straggler guard.
            self._track_input_item(event.get("item_id"))
            # Barge-in: send ``response.cancel`` so that audio/transcript deltas from
            # the interrupted response do NOT keep flowing and pollute the next turn's
            # _out_buf — BUT ONLY when an in-flight response exists. server_vad emits
            # speech-start every turn; cancelling when there is no response would cause
            # a spurious ``error`` from the model at the start of each turn.
            # Afterwards: notify the browser with interrupt, persist the interrupted
            # turn, and reset buffers (response.done will NOT arrive for this turn).
            # This is the OpenAI analogue of Gemini's ``interrupted`` frame — both
            # persist the interrupted turn through the shared, orphan-guarded
            # RealtimeBridge._persist_turn (see realtime_base module docstring).
            if self._response_active:
                await self._send_event({"type": "response.cancel"})
                self._response_active = False
            # A barge-in ends the current turn; any in-flight tool continuation is moot.
            self._tool_continuation_pending = False
            await self._send_json({"type": "interrupt"})
            await self._persist_turn()
            # Input transcription is async: a barge before the interrupted turn's own
            # completed lands (or a noise-triggered response with no transcript) hits the
            # orphan guard with _in_buf empty, so _persist_turn is a no-op that leaves the
            # cancelled reply fragment in _out_buf. Drop that one-sided _out_buf here (keep
            # _in_buf per the retain-user-side contract) so it never prepends to the next
            # turn's assistant text — mirrors gemini turn_complete's one-sided cleanup.
            self._out_buf = ""
        elif etype == "response.done":
            # NOTE: the Realtime API has no separate "response.cancelled" server
            # event — a cancelled response is acknowledged via "response.done"
            # with response.status == "cancelled". Skip persist/turn_complete
            # for that case (barge-in already persisted in speech_started to
            # avoid a double write of a corrupt turn pairing old + new audio).
            self._response_active = False
            status = str((event.get("response") or {}).get("status") or "")
            if status == "cancelled":
                return
            if self._tool_continuation_pending:
                # This response.done ends the tool preamble (response 1); the answer is
                # still coming in the continuation response. Do NOT persist or flip the
                # browser to LISTENING now — keep _out_buf (the preamble) so the answer
                # accumulates onto it and the whole turn persists as ONE record.
                self._tool_continuation_pending = False
                return
            await self._handle_response_done()
            await self._send_json({"type": "turn_complete"})
        elif etype == "error":
            log.warning("openai realtime error event: %s", event.get("error"))

    async def _emit_audio(self, b64: Any) -> None:
        if not b64:
            return
        try:
            audio = base64.b64decode(b64)
        except (ValueError, TypeError):  # pragma: no cover - malformed base64 must not cut audio
            return
        if audio:
            await self._safe_send_bytes(audio)

    async def _emit_transcript(self, role: str, text: Any) -> None:
        text = str(text or "")
        if not text:
            return
        if role == "assistant":
            self._out_buf += text
        else:
            self._in_buf += text
        await self._send_json({"type": "transcript", "role": role, "text": text})

    def _track_input_item(self, item_id: Any) -> None:
        """Record the item_id of the user turn currently being transcribed.

        Best-effort turn identity: the Realtime API tags input-transcription and
        speech-start events with the conversation item they belong to, letting a LATE
        completed be matched back to its turn (straggler detection in
        _emit_user_completed). Absent in the hermetic tests; the epoch fallback covers
        that. Ignore stragglers of an already-flushed turn — they must not overwrite the
        current turn's identity. Also refuse to overwrite while a turn is DEFERRED
        (assistant stashed): on the real wire the next turn's speech_started arrives
        before the deferred turn is flushed, and clobbering the identity here would make
        _flush_pending_assistant mark the wrong item as flushed."""
        iid = str(item_id or "")
        if not iid or iid in self._flushed_item_ids:
            return
        if self._pending_assistant:
            return
        self._pending_item_id = iid

    def _is_straggler(self, item_id: str) -> bool:
        """True when this completed belongs to a turn already resolved (persisted).

        Two signals, item_id-first: an explicit item_id already in _flushed_item_ids is
        authoritative; otherwise (no item_id in the payload) fall back to the one-shot
        _expect_straggler flag armed by the last placeholder flush — the next unmatched
        completed after a flush is the just-closed turn's late transcript."""
        if item_id and item_id in self._flushed_item_ids:
            return True
        if not item_id and self._expect_straggler and not self._pending_assistant:
            return True
        return False

    async def _emit_user_completed(self, transcript: Any, item_id: Any = None) -> None:
        """``input_audio_transcription.completed`` → AUTHORITATIVE (final/corrected)
        form of the user transcript. ``completed`` sets ``_in_buf`` (the persisted
        source) authoritatively; only the NOT-YET-SHOWN portion is sent to the browser
        (a suffix if it extends the deltas, otherwise the full text) → the display
        neither double-counts nor loses content.

        STRAGGLER guard (VB-4): a completed can arrive AFTER its own turn was already
        resolved — response.done stashed the assistant text and the NEXT turn's
        response.created then flushed it with a placeholder (see
        _flush_pending_assistant). Such a late completed belongs to the CLOSED turn, not
        the live one; writing it into _in_buf would pair the old user text with the new
        turn's reply. So a straggler is emitted to the browser (display continuity) but
        never buffered or persisted here."""
        full = str(transcript or "")
        if not full:
            return
        iid = str(item_id or "")
        if self._is_straggler(iid):
            # Late transcript of an already-persisted turn: show it, but do NOT let it
            # poison the next turn's _in_buf (would mispair user/assistant across turns).
            self._expect_straggler = False
            self._flushed_item_ids.discard(iid)
            await self._send_json({"type": "transcript", "role": "user", "text": full})
            return
        self._track_input_item(iid)
        shown = self._in_buf
        self._in_buf = full  # authoritative for persistence
        if full != shown:
            suffix = full[len(shown):] if full.startswith(shown) else full
            if suffix:
                await self._send_json({"type": "transcript", "role": "user", "text": suffix})
        # LATE transcript (matched): response.done for this turn already fired and stashed
        # the assistant text (because _in_buf was empty then). Now that the user text has
        # arrived, restore it and persist the turn so it is NOT merged into the next one
        # (input transcription is async in the Realtime API and can trail the response).
        # No-op in the common case where completed precedes response.done.
        #
        # Only pair when this completed actually belongs to the deferred turn: when both
        # carry item_ids, they must match; if they differ this is the NEXT turn's transcript
        # arriving early and pairing it with the deferred reply would cross-pair the turns.
        # With no item_id (hermetic tests) fall back to the arrival-order assumption.
        if self._pending_assistant and not self._out_buf:
            if iid and self._deferred_item_id and iid != self._deferred_item_id:
                return
            self._out_buf = self._pending_assistant
            self._pending_assistant = ""
            self._deferred_item_id = ""
            self._pending_item_id = ""
            await self._persist_turn()

    async def _handle_response_done(self) -> None:
        """Persist the completed turn, deferring when the user transcript is late.

        Input transcription is asynchronous in the Realtime API: for a short
        utterance with a fast answer, ``input_audio_transcription.completed`` can
        arrive AFTER ``response.done``. If we called ``_persist_turn`` directly in
        that ordering, the orphan guard (empty ``_in_buf``) would return WITHOUT
        clearing ``_out_buf``; the assistant text would then linger and merge with
        the next turn's text into one corrupted record. Instead, when we have
        assistant text but no user text yet, stash the assistant text and clear
        ``_out_buf`` — ``_emit_user_completed`` finishes the persist when the late
        transcript lands. When ``_in_buf`` is already present (the common case, and
        every barge-in path), persist immediately as before.
        """
        if self._out_buf.strip() and not self._in_buf.strip():
            self._pending_assistant = self._out_buf
            self._out_buf = ""
            # Lock the deferred turn's identity NOW: the next turn's speech_started will
            # overwrite _pending_item_id before this turn is flushed (real wire order).
            self._deferred_item_id = self._pending_item_id
            # Leave _turn_t0 untouched: the deferred _persist_turn (fired when the
            # late transcript arrives) measures latency from the real turn start.
            return
        if self._in_buf.strip() and not self._out_buf.strip():
            # A response that produced NO assistant text (response.done status
            # failed/incomplete, or an empty response) is a dead turn — the question got
            # no answer. The orphan guard in _persist_turn would return without clearing,
            # retaining this stale _in_buf; the NEXT turn's late-transcript deferral is
            # then defeated (its condition needs an empty _in_buf) and the stale question
            # cross-pairs with the next turn's reply, dropping that turn's real transcript.
            # Drop the dead turn here instead (no assistant side to pair with anyway).
            self._in_buf = ""
            self._pending_item_id = ""
            return
        await self._persist_turn()
        self._pending_item_id = ""

    async def _flush_pending_assistant(self) -> None:
        """Persist a stashed assistant turn whose user transcript never arrived.

        Fallback for the deferral in :meth:`_handle_response_done` when the late
        ``input_audio_transcription.completed`` never comes (transcription
        failed/dropped, or the session ended first). Rather than lose the assistant
        turn or let it merge into the next one, persist it against a placeholder
        user text so the record is complete and self-contained. No-op unless an
        unmatched assistant is pending.

        On a real flush, remember the turn's identity so a LATE completed for it is
        recognised as a straggler and NOT buffered into the next turn (see
        _emit_user_completed): mark the item_id (when known) and arm the payload-agnostic
        one-shot straggler flag for the no-item_id case."""
        if not self._pending_assistant.strip():
            self._pending_assistant = ""
            return
        if not self._in_buf.strip():
            # "[voice]" — a neutral marker the orphan guard accepts; the assistant
            # text is preserved and attributed to a voice turn with no captured
            # transcript, EN by default (persist uses primary_lang for the label).
            self._in_buf = "[voice]"
        self._out_buf = self._pending_assistant
        self._pending_assistant = ""
        # This turn is now closed; its late user transcript (if it ever lands) is a
        # straggler. Record the DEFERRED turn's identity (locked at deferral time, before
        # the next turn's speech_started overwrote _pending_item_id) so _emit_user_completed
        # drops the right straggler and never marks the next turn's item as flushed.
        flushed_iid = self._deferred_item_id or self._pending_item_id
        if flushed_iid:
            self._flushed_item_ids.add(flushed_iid)
        self._expect_straggler = True
        self._deferred_item_id = ""
        self._pending_item_id = ""
        await self._persist_turn()

    async def _handle_function_call(self, event: dict[str, Any]) -> None:
        """``response.function_call_arguments.done`` → dispatch → function_call_output
        + ``response.create`` (so the model can continue with the tool result).

        Dispatch is DEFENSIVE (``dispatch_llm_tool`` converts every error to text);
        if the name is absent from the ``.done`` event, fall back to the name tracked
        from ``output_item.added``."""
        call_id = str(event.get("call_id") or "")
        # pop (unconditionally), not get: the tracked name is single-use — one .done
        # per call_id — so draining it here keeps _fc_names from growing unbounded
        # across a long continuous session that makes many tool calls, whether or not
        # the .done event carries its own name.
        tracked = self._fc_names.pop(call_id, "")
        name = str(event.get("name") or "") or tracked
        raw_args = event.get("arguments")
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args else {}
        except (TypeError, ValueError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        result = await _off_loop(dispatch_llm_tool, self.settings, self.conv_id, name, args)
        await self._send_json({"type": "tool", "name": name})
        await self._send_event(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": str(result),
                },
            }
        )
        await self._send_event({"type": "response.create"})
        # The response ending now (response 1) is only the tool preamble; the real answer
        # comes in the response we just requested. Defer persist + turn_complete until then.
        self._tool_continuation_pending = True

    # --- OpenAI Realtime event send (in addition to the base safe browser I/O) ---

    async def _send_event(self, event: dict[str, Any]) -> None:
        """Send a JSON event to the OpenAI Realtime WS (a broken socket must not crash the bridge)."""
        if self._oai is None:
            return
        try:
            await self._oai.send(json.dumps(event))
        except Exception:  # pragma: no cover - broken/network; pump will already stop
            pass


__all__ = [
    "FRAME_AUDIO",
    "OpenAIRealtimeBridge",
    "build_session_update",
    "parse_browser_frame",
]
