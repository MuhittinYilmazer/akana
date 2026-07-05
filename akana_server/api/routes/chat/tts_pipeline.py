"""Streaming-TTS side-pipeline for the live SSE producer (seam split from chat_producer).

The producer streams LLM deltas to the client as ``delta`` SSE events; in a voice/TTS
turn those same deltas must ALSO be synthesized to audio and interleaved as
``tts_chunk`` SSE events. That three-queue, two-task machine used to live inline in the
~1000-line ``_stream_chat_response`` generator. It is a self-contained unit with a
narrow interface, so it lives here as :class:`TtsPipeline`; the producer keeps only the
LLM demux loop and the persistence phases.

Wire contract (BYTE-STABLE — unchanged from the inline version):
  * ``tts_chunk`` — one per synthesized WAV chunk (``{...chunk fields}``)
  * ``tts_error`` — ``{code: "TTS_ERROR", message}`` if the pump fails mid-turn

Data flow:  producer deltas ─put→ ``delta_q`` ─(_delta_iter)→ ``stream_text_to_tts_chunks``
            ─put→ ``tts_chunk_q`` ─(_forward)→ SSE-encoded ─put→ ``sse_q`` ─get→ producer

Loss policy: all three queues are :class:`_DropOldestQueue` (bounded, drop-oldest, never
block the producer). Audio loss under back-pressure is tolerated by design (a short
audio gap); the TEXT stream + the LLM read are never blocked or slowed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator

from akana_server.config import Settings
from akana_server.observability import registry
from akana_server.voice import VoiceSelection, stream_text_to_tts_chunks

from akana_server.api.routes.chat._base import _sse_pack
from akana_server.api.routes.chat.chat_state import _DropOldestQueue, _TTS_QUEUE_MAX

log = logging.getLogger(__name__)


class TtsPipeline:
    """Owns the delta→WAV→SSE queues + the pump/forward tasks for one turn.

    Lifecycle::

        pipe = TtsPipeline(settings, conv_id, tts_active, voice_path, selection)
        pipe.start(t0)                    # spawn pump + forward tasks
        ...                               # per delta: await pipe.feed(delta)
        await pipe.close_input()          # LLM stream ended → put the None sentinel
        await pipe.flush()                # await pump+forward (audio tail on the wire)
        async for sse in pipe.drain_ready(): yield sse
        ...
        await pipe.shutdown()             # finally: cancel both tasks (idempotent)

    On a TTS-OFF turn the queues are still created and the pump drains ``delta_q`` (so a
    full queue never blocks the producer), but no audio is ever produced and
    :meth:`sse_get` is not used by the producer's mux.
    """

    def __init__(
        self,
        settings: Settings,
        conversation_id: str,
        *,
        active: bool,
        voice_path: str | None,
        selection: VoiceSelection | None,
    ) -> None:
        self._settings = settings
        self._conv_id = conversation_id
        self.active = active
        self._voice_path = voice_path
        self._selection = selection
        self._t0 = 0.0
        # delta_q: LLM deltas in; tts_chunk_q: synthesized WAV chunks; sse_q: SSE bytes out.
        self._delta_q: asyncio.Queue[str | None] = _DropOldestQueue(
            maxsize=_TTS_QUEUE_MAX
        )
        self._chunk_q: asyncio.Queue[dict[str, object] | None] = _DropOldestQueue(
            maxsize=_TTS_QUEUE_MAX
        )
        self._sse_q: asyncio.Queue[bytes | None] = _DropOldestQueue(
            maxsize=_TTS_QUEUE_MAX
        )
        self._pump_task: asyncio.Task[None] | None = None
        self._forward_task: asyncio.Task[None] | None = None

    async def _delta_iter(self) -> AsyncIterator[str]:
        while True:
            item = await self._delta_q.get()
            if item is None:
                return
            yield item

    async def _pump(self) -> None:
        if not self.active:
            # Drain deltas anyway so the queue doesn't block the producer.
            async for _ in self._delta_iter():
                pass
            await self._chunk_q.put(None)
            return
        try:
            async for chunk in stream_text_to_tts_chunks(
                self._delta_iter(),
                self._settings,
                self._voice_path,
                selection=self._selection,
            ):
                await self._chunk_q.put(chunk)
        except Exception as e:  # pragma: no cover - logged inside helper
            await self._chunk_q.put({"_error": f"streaming tts pump failed: {e}"})
        finally:
            await self._chunk_q.put(None)

    async def _forward(self) -> None:
        first = True
        while True:
            chunk = await self._chunk_q.get()
            if chunk is None:
                break
            if isinstance(chunk, dict) and "_error" in chunk:
                await self._sse_q.put(
                    _sse_pack(
                        "tts_error",
                        {"code": "TTS_ERROR", "message": str(chunk["_error"])},
                    ).encode("utf-8")
                )
                continue
            if first:
                first = False
                # Diagnostics: seconds from the text stream start to first audio ready.
                try:
                    log.info(
                        "tts first chunk conv=%s +%.2fs",
                        self._conv_id,
                        time.perf_counter() - self._t0,
                    )
                except Exception:  # pragma: no cover - diagnostics only
                    pass
            await self._sse_q.put(_sse_pack("tts_chunk", chunk).encode("utf-8"))
        await self._sse_q.put(None)

    def start(self, t0: float) -> None:
        """Spawn the pump + forward tasks (call once, at the turn's stream start)."""
        self._t0 = t0
        self._pump_task = asyncio.create_task(self._pump())
        self._forward_task = asyncio.create_task(self._forward())

    async def feed(self, delta: str) -> None:
        """Push one LLM delta into the synthesis pipeline (drop-oldest; never blocks).

        Also refreshes the ``queue_depth`` back-pressure gauge (the deepest of the three
        queues) so /system/metrics reflects live TTS back-pressure. No-op text is still
        queued by the caller only when :attr:`active`.
        """
        await self._delta_q.put(delta)
        registry.set(
            "queue_depth",
            max(
                self._delta_q.qsize(),
                self._chunk_q.qsize(),
                self._sse_q.qsize(),
            ),
        )

    async def close_input(self) -> None:
        """Signal end-of-deltas (the None sentinel) so the pump can finish + flush."""
        await self._delta_q.put(None)

    def sse_get(self) -> asyncio.Task[bytes | None]:
        """A task awaiting the next SSE-encoded audio item (for the producer's mux).

        Returns a fresh ``asyncio.Task`` wrapping ``sse_q.get()``. The producer waits on
        this alongside the LLM iterator (FIRST_COMPLETED); a ``None`` result is the
        pump's terminal sentinel (no more audio).
        """
        return asyncio.create_task(self._sse_q.get())

    async def drain_ready(self) -> AsyncIterator[bytes]:
        """Yield all SSE audio items already queued (non-blocking; stops at the sentinel).

        Used on the error/empty/done finalization paths to flush whatever audio is
        buffered without waiting for more synthesis.
        """
        while True:
            try:
                item = self._sse_q.get_nowait()
            except asyncio.QueueEmpty:
                return
            if item is None:
                return
            yield item

    async def flush(self) -> None:
        """Await the pump + forward tasks so all audio is on the wire before ``done``.

        Swallows CancelledError (STOP/shutdown) — the caller then drains any ready
        items. Idempotent: a task already awaited/finished is a no-op.
        """
        for task in (self._pump_task, self._forward_task):
            if task is None:
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def shutdown(self) -> None:
        """Cancel both helper tasks (the turn's ``finally``) — symmetric + idempotent.

        The CANCEL path (STOP / shutdown) skips the normal/error flush, so ``_forward``
        may be blocked forever on an empty ``chunk_q`` (the pump was cancelled before
        writing its None sentinel). Cancelling both here prevents a leaked task per STOP.
        Also clamps the ``queue_depth`` gauge to 0 (only when this turn used TTS) so an
        idle server does not report a stuck non-zero depth.
        """
        if self.active:
            registry.set("queue_depth", 0)
        for task in (self._pump_task, self._forward_task):
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
