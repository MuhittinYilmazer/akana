"""Shared base for the twin full-duplex realtime voice bridges.

Both provider bridges — :class:`akana_server.voice.gemini_live.LiveBridge`
(google-genai SDK) and :class:`akana_server.voice.openai_realtime.OpenAIRealtimeBridge`
(raw OpenAI Realtime WS) — translate the SAME browser protocol
(``[0x01]+pcm16`` frames up, ``transcript``/``interrupt``/``turn_complete`` JSON +
raw PCM bytes down) to and from a provider session, and persist each completed turn
to ``memory.db`` identically. Everything provider-neutral lives here so a
protocol/persistence fix is written once:

- ``FRAME_AUDIO`` + :func:`parse_browser_frame` — the browser binary framing.
- :func:`_off_loop` — offload sqlite/file side-effects to a worker thread.
- :class:`RealtimeBridge` — the run/pump skeleton, transcript buffers, orphan-guarded
  turn persistence, EventHub ``chat_done`` broadcast, conversation ensure, and safe
  WS I/O (a broken socket write must not crash the bridge).

Provider specifics stay in the subclass: opening the session (``_open_session``),
availability (``_available``), the two pump directions (``_from_browser`` /
``_from_provider``), and the EventHub ``source`` label (``_broadcast_source``).

Reconciled drift (previously the two copies differed):

- Interrupted-turn persistence trigger. Both providers persist the interrupted
  turn on their barge-in signal — Gemini on the ``interrupted`` server frame,
  OpenAI on ``input_audio_buffer.speech_started`` — by calling :meth:`_persist_turn`.
  The provider signal names differ (that is inherent to each API) but the
  SEMANTICS are now identical: persist-if-complete then reset, driven by the same
  base method.
- Barge-in buffer/clock reset. The reset is owned by :meth:`_persist_turn` and
  happens ONLY when a real (user+assistant) turn is written. An orphan barge-in
  (user interjects before any assistant text) is a no-op that leaves the partial
  user transcript intact, so it is not lost or mis-attributed to the old turn.
  Both bridges get exactly this behaviour by delegating to the base.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

import ulid
from fastapi import WebSocket, WebSocketDisconnect

from akana_server.concurrency import off_loop

if TYPE_CHECKING:
    from akana_server.config import Settings

log = logging.getLogger(__name__)

#: Browser→server audio frame tag: ``[0x01] + pcm16le`` mono.
#: (0x02 reserved for control-binary in the future; today, audio only.)
#: Sample rate is provider-specific (Gemini 16k, OpenAI 24k) but the framing is shared.
FRAME_AUDIO = 0x01

#: Ceiling on how long ONE spoken turn holds the conversation in the busy registry.
#: The session is long-lived and deliberately un-guarded, but an utterance is a turn,
#: and a turn that never finishes (the user speaks, the provider never answers, the
#: socket stalls) must not wedge the conversation for the rest of the session — a web
#: send would queue behind it with nothing left to wait for. The claim expires on its
#: own here, so nothing is blocked for longer than a single spoken exchange.
_SPOKEN_TURN_MAX_HOLD_S = 60.0


def parse_browser_frame(data: bytes) -> tuple[int, bytes]:
    """Browser binary frame → ``(tag, payload)``. Empty data → ``(-1, b"")``.

    The tag is preserved so the caller can distinguish and ignore non-audio flags."""
    if not data:
        return (-1, b"")
    return (data[0], bytes(data[1:]))


async def _off_loop(fn, *args, **kwargs):
    """Offload synchronous sqlite/file side-effects to a worker thread.

    Thin re-export of :func:`akana_server.concurrency.off_loop` under the name
    the voice bridges import; the shared home keeps them off any api/routes
    dependency."""
    return await off_loop(fn, *args, **kwargs)


class RealtimeBridge:
    """Single WS ↔ single provider realtime session, bound to a ``conv_id``.

    Lifecycle in :meth:`run`: availability gate → open session → dual pump → clean
    close. The SESSION is not busy-guarded (it is long-lived, and guarding it would
    make the conversation permanently busy) — but each spoken TURN is registered in
    the non-streaming busy registry for as long as it is being produced, so a
    background result cannot land between the spoken question and its answer. See
    :meth:`_claim_spoken_turn`. Subclasses supply the provider-specific hooks.
    """

    #: EventHub ``chat_done`` source label — subclass overrides (voice_live / voice_realtime).
    _broadcast_source = "voice"
    #: Human label used in the pre-gate and mid-session close reasons.
    _label = "Realtime"

    def __init__(
        self, websocket: WebSocket, settings: Settings, *, app: Any, conv_id: str
    ) -> None:
        self.ws = websocket
        self.settings = settings
        self.app = app
        self.conv_id = conv_id
        #: Busy-registry handle for the spoken turn in flight (None = not claimed).
        #: Assigned before ``_in_buf`` below, whose setter reads it.
        self._turn_handle: "asyncio.Task[Any] | None" = None
        self._in_buf = ""  # user transcript (input transcription)
        self._out_buf = ""  # assistant transcript (output transcription)
        self._turn_t0 = time.perf_counter()

    # --- The spoken turn's claim on the conversation -----------------------

    @property
    def _in_buf(self) -> str:
        """User transcript of the turn being spoken RIGHT NOW.

        A property rather than a plain attribute because it is the ONE signal both
        provider bridges already share for "the user has started producing this turn":
        every ``_in_buf +=`` / ``_in_buf =`` in either subclass is that moment. Hooking
        the claim here registers the turn once, from one place, instead of asking every
        provider-specific transcript branch to remember to."""
        return self.__in_buf

    @_in_buf.setter
    def _in_buf(self, value: str) -> None:
        self.__in_buf = value
        if value.strip():
            self._claim_spoken_turn()

    def _claim_spoken_turn(self) -> None:
        """Register the spoken turn in the non-streaming busy registry (best-effort).

        The bridge persists the user+assistant PAIR only at the END of the utterance,
        so while it is being spoken the conversation looked completely FREE to every
        probe: a background job completing meanwhile injected its result straight in,
        ABOVE the question it answers, and the agent-resume context note was attached at
        the wrong point. Claiming also gives the ``turn_completed`` that
        :meth:`_broadcast_done` already emitted its matching ``turn_active`` — realtime
        used to be a completion producer the gate had no record of.

        Registered is the TURN, never the session. Never raises and never waits: when
        the conversation is already busy (409) the bridge simply does not own the claim
        — a live voice session must not fail because a text turn happens to be running.
        """
        if self._turn_handle is not None or self.app is None or not self.conv_id:
            return
        try:
            from akana_server.api.routes.chat.turn_gate import (
                register_turn,
                swap_turn_handle,
            )

            # ``register_turn`` records the CURRENT task as the cancel handle — here that
            # is the provider pump, whose cancellation would tear the whole session down.
            # Hold the claim with a dedicated expiring sentinel instead: a STOP cancels
            # only it, and an utterance that never completes releases itself.
            sentinel = asyncio.create_task(
                asyncio.sleep(_SPOKEN_TURN_MAX_HOLD_S),
                name=f"voice-turn-claim:{self.conv_id}",
            )
            try:
                handle = register_turn(self.app, self.conv_id, source="user")
            except BaseException:
                sentinel.cancel()
                raise
            if handle is None:
                sentinel.cancel()
                return
            swap_turn_handle(self.app, self.conv_id, sentinel)  # no await → atomic
            self._turn_handle = sentinel
        except Exception:  # noqa: BLE001 - 409 busy / no loop / no app must not break voice
            log.debug(
                "realtime: spoken-turn claim skipped (conv=%s)", self.conv_id, exc_info=True
            )

    async def _release_spoken_turn(self, *, status: str, announce: bool) -> None:
        """Free the spoken turn's claim; ``announce`` emits the ``turn_completed`` it owes.

        ``announce=False`` on the persist path ONLY: :meth:`_broadcast_done` emits that
        turn's one completion itself, because it is the only caller that knows the
        persisted assistant turn id the chat log reloads by. Every other exit (the
        session closing on an unfinished utterance) has to announce here or the
        ``turn_active`` stays latched."""
        handle, self._turn_handle = self._turn_handle, None
        if handle is None:
            return
        try:
            from akana_server.api.routes.chat.turn_gate import release_turn

            if not handle.done():
                handle.cancel()
            release_turn(
                self.app, self.conv_id, handle, status=status, announce=announce
            )
        except Exception:  # noqa: BLE001 - releasing the UI must never break the close path
            log.debug(
                "realtime: spoken-turn release failed (conv=%s)", self.conv_id, exc_info=True
            )

    # --- Provider hooks (subclass implements) ------------------------------

    def _available(self) -> bool:
        """True when the provider can serve a session (SDK/key present)."""
        raise NotImplementedError

    def _begin_turn_mode(self) -> str:
        """Observability mode tag for ``begin_turn`` (e.g. ``voice_live``)."""
        raise NotImplementedError

    async def _open_session(self) -> None:
        """Open the provider session and run the dual pump.

        Called inside :meth:`run`'s try/except; must open the session, send the
        browser ``ready`` JSON, and drive :meth:`_pump`. Provider errors propagate
        to :meth:`run` (which maps them to a clean close)."""
        raise NotImplementedError

    async def _from_browser(self, session: Any) -> None:
        """WS→provider pump: stream browser audio frames into the session."""
        raise NotImplementedError

    async def _from_provider(self, session: Any) -> None:
        """provider→WS pump: forward audio + transcripts + tool calls to the browser."""
        raise NotImplementedError

    # --- Run / pump skeleton -----------------------------------------------

    async def run(self) -> None:
        if not self._available():
            await self._safe_close(1011, f"{self._label} unavailable (SDK/key).")
            return
        from akana_server.observability import begin_turn

        await self._ensure_conversation()
        begin_turn(self.conv_id, mode=self._begin_turn_mode())
        try:
            await self._open_session()
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001 - session error should close WS cleanly
            log.warning("%s session error: %s", self._label, exc, exc_info=True)
            # BUG B2 fix: mid-session provider errors use a distinct app code (4001,
            # reconnectable) instead of 1011, which the client blocklists for reconnect
            # (1011 is reserved for the pre-bridge gate failure above, where retrying
            # is pointless).
            await self._safe_close(4001, f"{self._label} session closed unexpectedly.")
        finally:
            # An announced spoken turn owes exactly ONE completion on EVERY exit path.
            # ``_persist_turn`` already released the turns it wrote, so this only fires
            # for an utterance the session ended in the middle of — the user spoke and
            # no answer was ever persisted, which is "cancelled", not "ok".
            await self._release_spoken_turn(status="cancelled", announce=True)
            # When the provider stream ends normally, the browser WS must not remain
            # open → half-open socket; the frontend would stream microphone audio into
            # a dead socket and the orb would stall. ``_safe_close`` is a no-op when
            # already closed/disconnected (double-close is safe).
            await self._safe_close(1000, f"{self._label} session ended.")

    async def _pump(self, session: Any) -> None:
        """Run the two pump directions in parallel; cancel the other when the first completes."""
        from_browser = asyncio.create_task(self._from_browser(session))
        from_provider = asyncio.create_task(self._from_provider(session))
        done, pending = await asyncio.wait(
            {from_browser, from_provider}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
                raise exc

    # --- Turn persistence (shared, orphan-guarded) -------------------------

    async def _persist_turn(self) -> None:
        """Write the input/output transcript pair to ``memory.db``.

        Orphan-guard: only writes when BOTH user AND assistant text are present →
        a partial/empty turn is structurally impossible. This is the single
        persistence trigger for BOTH a completed turn AND a barge-in interruption
        (each provider calls it from its own signal — see the module docstring).

        Reset semantics: buffers and the latency clock are reset ONLY when a real
        turn is written. An orphan/no-op persist (e.g. barge-in before assistant
        text arrives) returns early WITHOUT resetting, so the interjecting user's
        partial transcript survives into the next turn instead of being deleted
        (the old strip→reset→guard order lost it) or mis-attributed.
        """
        user_text = self._in_buf.strip()
        assistant_text = self._out_buf.strip()
        latency_ms = int((time.perf_counter() - self._turn_t0) * 1000)
        if not (user_text and assistant_text):
            return
        self._in_buf = ""
        self._out_buf = ""
        self._turn_t0 = time.perf_counter()

        from akana_server.orchestrator.turn_writer import (
            persist_assistant_turn,
            persist_user_turn,
        )

        lang = getattr(self.settings, "primary_lang", "en") or "en"
        data_dir = self.settings.data_dir
        user_turn_id = await _off_loop(
            persist_user_turn,
            conversation_id=self.conv_id,
            user_text=user_text,
            lang=lang,
            data_dir=data_dir,
        )
        assistant_turn_id = await _off_loop(
            persist_assistant_turn,
            conversation_id=self.conv_id,
            assistant_text=assistant_text,
            user_turn_id=user_turn_id,
            lang=lang,
            latency_ms=latency_ms,
            intent="chat",
            data_dir=data_dir,
        )
        # The claim covers the WRITE, not just the speaking: releasing before the pair
        # is on disk is exactly the window an injection used to slip into, landing above
        # the question it answers. ``announce=False`` — the broadcast below is this
        # turn's one completion, and only it carries the assistant turn id.
        await self._release_spoken_turn(status="ok", announce=False)
        await self._broadcast_done(
            assistant_text, latency_ms, assistant_turn_id=str(assistant_turn_id or "")
        )

    async def _broadcast_done(
        self, assistant_text: str, latency_ms: int, *, assistant_turn_id: str = ""
    ) -> None:
        """Announce the spoken turn via EventHub — UI consistency (same as text chat).

        ``chat_done`` alone never achieved that: no chat-UI consumer has ever handled
        that event, so a whole live-voice exchange was persisted server-side and stayed
        invisible in the open chat pane until F5. The chat log refreshes on
        ``turn_completed``, so the turn is announced through the real lifecycle contract
        as well; ``chat_done`` is kept for the voice/aurora surfaces that read it.
        The user is speaking this turn themselves → source="user" (no background-work
        indicator, no desktop notification)."""
        from akana_server.conversation_events import broadcast_turn_completed
        from akana_server.events import EventHub

        await broadcast_turn_completed(
            self.app,
            self.conv_id,
            status="ok",
            assistant_turn_id=assistant_turn_id or None,
            source="user",
        )
        hub = getattr(getattr(self.app, "state", None), "event_hub", None)
        if not isinstance(hub, EventHub):
            return
        await hub.broadcast_json(
            {
                "type": "chat_done",
                "turn_id": str(ulid.new()),
                "conversation_id": self.conv_id,
                "intent": "chat",
                "approval_required": False,
                "tool_calls_count": 0,
                "latency_ms": latency_ms,
                "preview": assistant_text[:400],
                "source": self._broadcast_source,
            }
        )

    async def _ensure_conversation(self) -> None:
        from akana_server.conversation_service import ConversationService

        conv_svc = getattr(getattr(self.app, "state", None), "conversation_service", None)
        if isinstance(conv_svc, ConversationService):
            await _off_loop(conv_svc.ensure, self.conv_id)

    # --- Safe WS I/O (broken socket writes must not crash the bridge) -------

    async def _send_json(self, payload: dict[str, Any]) -> None:
        try:
            await self.ws.send_json(payload)
        except (WebSocketDisconnect, RuntimeError):  # pragma: no cover - broken socket
            pass

    async def _safe_send_bytes(self, data: bytes) -> None:
        try:
            await self.ws.send_bytes(data)
        except (WebSocketDisconnect, RuntimeError):  # pragma: no cover - broken socket
            pass

    async def _safe_close(self, code: int, reason: str) -> None:
        try:
            await self.ws.close(code=code, reason=reason)
        except (WebSocketDisconnect, RuntimeError):  # pragma: no cover - already closed
            pass


__all__ = [
    "FRAME_AUDIO",
    "RealtimeBridge",
    "parse_browser_frame",
]
