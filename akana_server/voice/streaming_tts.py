"""Sentence-level streaming TTS over an async text stream.

As the LLM emits delta tokens, accumulate them in a buffer. When the buffer
contains a sentence terminator (`.`, `!`, `?`, `…`, newline) we hand the
fully-formed sentence to the selected TTS engine, base64-encode the audio
bytes, and yield them as a chunk (with their MIME type). First audible audio
arrives within seconds of the first LLM token.

Engine selection is registry-based (:mod:`akana_server.voice.engines`)
via :class:`VoiceSelection`. The fallback chain lives here: if the primary
engine (e.g. edge) fails or times out, the SAME sentence is re-synthesized
with Piper so the audio stream never breaks; the failing engine is demoted
for the remainder of the stream to avoid a per-sentence timeout stall.
"""

from __future__ import annotations

import asyncio as _asyncio
import base64
import logging
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

import anyio

# Absolute submodule import: `from akana_server.voice import engines` would create
# a module-level back-edge into the package __init__ and introduce a cycle (arch test);
# the `tts_engines` module alias is preserved as-is (tts_engines.resolve/get namespace).
import akana_server.voice.engines as tts_engines
from akana_server.voice.engines import TtsEngine, VoiceSelection
from akana_server.voice.tts import TtsError, TtsTimeout
from akana_server.voice_preferences import (
    VoicePreferences,
    load_voice_preferences,
)

if TYPE_CHECKING:
    from akana_server.config import Settings

log = logging.getLogger(__name__)


_MD_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"```(?:[\w+-]*)\n?(.*?)```", re.DOTALL), r"\1"),
    # Sentence splitting may cut a fence in half — lone ``` lines
    # (e.g. "```python") are also stripped so they are never spoken.
    (re.compile(r"(?m)^[ \t]*```[\w+-]*[ \t]*$"), ""),
    (re.compile(r"`([^`\n]+)`"), r"\1"),
    (re.compile(r"\*\*([^*\n]+)\*\*"), r"\1"),
    (re.compile(r"__([^_\n]+)__"), r"\1"),
    (re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)"), r"\1"),
    (re.compile(r"(?<!_)_([^_\n]+)_(?!_)"), r"\1"),
    (re.compile(r"\[([^\]\n]+)\]\(([^)\n]+)\)"), r"\1"),
    (re.compile(r"(?m)^#{1,6}\s+"), ""),
    (re.compile(r"(?m)^\s*[-*+]\s+"), ""),
    (re.compile(r"(?m)^\s*\d+\.\s+"), ""),
)


# Emoji/pictogram blocks — TTS engines either skip them or read them oddly
# ("celebration face" etc.); strip them from spoken text. Letters in the
# Unicode letter class (including Turkish) are unaffected.
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # emoji + pictogram blocks (including flags)
    "☀-➿"  # miscellaneous symbols + dingbats
    "⬀-⯿"  # star/direction symbols
    "︀-️"  # variation selectors
    "‍"  # zero-width joiner
    "⃣"  # keycap combiner
    "]+"
)


def strip_markdown_for_tts(text: str) -> str:
    if not text:
        return ""
    out = text
    for pat, repl in _MD_PATTERNS:
        out = pat.sub(repl, out)
    out = _EMOJI_RE.sub("", out)
    out = re.sub(r"\n{3,}", "\n\n", out).replace("\t", " ")
    return out.strip()


_SENT_END = re.compile(r"([.!?…:]+)(?:\s+|$)|\n+")
# Soft boundary (comma/semicolon/dash): when no hard boundary is found and the
# buffer is long enough, flush here → reduces first-audio latency, stream speaks sooner.
_SOFT_SENT_END = re.compile(r"([,;–—])(?:\s+|$)")
_MIN_SENT_CHARS = 8
# Soft-flush: buf longer than this threshold + a comma/dash found after it → early yield.
# The search STARTS at this index → the first split chunk is at least this long
# (i.e. a short opener like "Sure, ..." with a comma won't flush alone; a full clause is awaited).
_SOFT_FLUSH_CHARS = 18
_HARD_FLUSH_CHARS = 120
# At least two letters/digits (incl. Turkish) — skips "—", "...", table rules.
_SPEAKABLE_CHARS = re.compile(r"[\w\u00C0-\u024F]", re.UNICODE)
_TABLE_RULE = re.compile(r"^[\s|:\-—_]+$")


def is_speakable_text(text: str) -> bool:
    """Return False for punctuation-only / markdown table debris."""
    t = (text or "").strip()
    if not t:
        return False
    if _TABLE_RULE.fullmatch(t):
        return False
    if len(_SPEAKABLE_CHARS.findall(t)) < 2:
        return False
    return True


# Common abbreviations that end with a dot — not counted as sentence boundaries.
_TR_DOT_ABBREVS = frozenset(
    {
        "dr", "prof", "doç", "yrd", "öğr", "gör", "av", "op", "uzm",
        "vb", "vs", "vd", "örn", "bkz", "krş", "çev", "haz", "yy",
        "sn", "hz", "no", "tel", "apt", "mah", "cad", "sok",
    }
)
_WORD_BEFORE_DOT = re.compile(r"(\w+)$")


def _is_false_boundary(buf: str, m: re.Match[str]) -> bool:
    """Is the matched separator an abbreviation/ordinal dot (not a true sentence boundary)?"""
    if m.group(1) != ".":  # \n+, "!?…:" and "..." are real boundaries
        return False
    wm = _WORD_BEFORE_DOT.search(buf, 0, m.start(1))
    if wm is None:
        return False
    word = wm.group(1)
    if word.isdigit():
        # Ordinal "3." — if followed by a lower-case letter the sentence has not ended.
        rest = buf[m.end():].lstrip()
        return bool(rest) and rest[:1].islower()
    return word.lower() in _TR_DOT_ABBREVS or (len(word) == 1 and word.isalpha())


def split_first_sentence(buf: str) -> tuple[str | None, str]:
    pos = 0
    while True:
        m = _SENT_END.search(buf, pos)
        if m is None:
            # No hard boundary — if the buffer is long enough, flush on a soft
            # boundary (comma/dash) to reduce first-audio latency. If still short,
            # wait for the hard-flush threshold.
            if len(buf) >= _SOFT_FLUSH_CHARS:
                # Search after _SOFT_FLUSH_CHARS → a short opener (e.g. "Sure,")
                # won't flush alone; a comma is only found after a full clause.
                soft = _SOFT_SENT_END.search(buf, _SOFT_FLUSH_CHARS)
                if soft is not None:
                    end = soft.end()
                    sentence = buf[:end].strip()
                    if len(sentence) >= _MIN_SENT_CHARS:
                        return sentence, buf[end:]
            if len(buf) >= _HARD_FLUSH_CHARS:
                return buf, ""
            return None, buf
        end = m.end()
        if _is_false_boundary(buf, m):
            pos = end
            continue
        sentence = buf[:end].strip()
        remainder = buf[end:]
        if len(sentence) < _MIN_SENT_CHARS and remainder:
            # Short fragment (e.g. "Yes.") is accumulated until the NEXT boundary.
            # The old behaviour would find the same first boundary repeatedly and
            # stall the stream: audio only arrived as one chunk when the stream ended.
            pos = end
            continue
        return sentence, remainder


def _engine_preference(settings: Settings, prefs: VoicePreferences | None) -> str:
    """Resolution order: env override (``AKANA_TTS_ENGINE``) > prefs > auto."""
    env = (getattr(settings, "tts_engine", "") or "").strip().lower()
    if env:
        return env
    if prefs is not None:
        pref = (getattr(prefs, "tts_engine", "") or "").strip().lower()
        if pref:
            return pref
    return "auto"


def _lang_hint(settings: Settings, voice_path: Path | None) -> str:
    if voice_path is not None:
        name = Path(voice_path).name.lower()
        if name.startswith("tr"):
            return "tr"
        if name.startswith("en"):
            return "en"
    return (settings.primary_lang or "tr").strip().lower()


def _engine_voice(
    engine: TtsEngine,
    lang: str,
    *,
    voice_path: Path | None,
    prefs: VoicePreferences,
) -> str:
    """Pick the engine-specific voice id for *lang*."""
    voice = ""
    if engine.name == "piper" and voice_path is not None:
        voice = str(voice_path)
    elif engine.name == "edge":
        raw = prefs.tts_voice_en if lang.startswith("en") else prefs.tts_voice_tr
        voice = (raw or "").strip()
    if not voice:
        default_voice = getattr(engine, "default_voice", None)
        if callable(default_voice):
            voice = str(default_voice(lang))
    return voice


def resolve_voice_selection(
    settings: Settings,
    *,
    lang: str | None = None,
    voice_path: Path | None = None,
    prefs: VoicePreferences | None = None,
) -> VoiceSelection:
    """Build the effective :class:`VoiceSelection` from env/prefs/availability.

    ``auto`` preference resolves to edge when usable, else Piper. *voice_path*
    keeps legacy callers working: it pins the Piper voice and hints the
    language for neural voices.
    """
    if prefs is None:
        prefs = load_voice_preferences(settings.data_dir)
    engine = tts_engines.resolve(_engine_preference(settings, prefs), settings)
    # ``auto`` is an UNRESOLVED marker, not a concrete language: treat it like
    # unset so the voice_path/primary_lang hint decides. Otherwise "auto" is
    # truthy, skips _lang_hint, and _engine_voice/default_voice fall to the
    # Turkish voice for every auto-language turn (an English reply then speaks
    # in tr-TR). The one-shot endpoints resolve auto→tr/en before selection;
    # this makes the streaming chat path match.
    lng = (lang or "").strip().lower()
    if lng in ("", "auto"):
        lng = _lang_hint(settings, voice_path)
    return VoiceSelection(
        engine=engine.name,
        voice=_engine_voice(engine, lng, voice_path=voice_path, prefs=prefs),
    )


def _piper_fallback_voice(
    settings: Settings, *, voice_path: Path | None, selection: VoiceSelection
) -> str:
    if voice_path is not None:
        return str(voice_path)
    lang = (selection.voice or "")[:2].lower() or _lang_hint(settings, None)
    use_en = lang.startswith("en")
    return str(settings.piper_voice_en if use_en else settings.piper_voice_tr)


async def _synthesize_via(engine: TtsEngine, text: str, voice: str) -> tuple[bytes, str]:
    return await anyio.to_thread.run_sync(engine.synthesize, text, voice)


async def synthesize_with_fallback(
    text: str,
    settings: Settings,
    *,
    selection: VoiceSelection | None = None,
    lang: str | None = None,
    voice_path: Path | None = None,
    fallback_on_timeout: bool = False,
) -> tuple[bytes, str]:
    """One-shot synth through the registry with automatic Piper fallback.

    Returns ``(audio_bytes, mime)``. Raises :class:`TtsError` only when the
    fallback engine fails too. ``fallback_on_timeout``: in single-shot (non-streaming)
    calls, fall back to Piper on timeout too — deliver robotic-but-working audio
    rather than crashing the whole turn with a 503. Leave False for the streaming path.
    """
    sel = selection or resolve_voice_selection(settings, lang=lang, voice_path=voice_path)
    primary = tts_engines.get(sel.engine, settings)
    try:
        return await _synthesize_via(primary, text, sel.voice)
    except TtsTimeout:
        # Edge is slow (not unreachable) — in streaming we prefer to wait for the
        # natural voice rather than fall back to robotic Piper (raise). In single-shot
        # mode (fallback_on_timeout) we fall back to Piper: a working response beats a 503.
        if not fallback_on_timeout or primary.name == "piper":
            raise
        log.warning("tts: %s timeout — falling back to piper (single-shot)", primary.name)
    except Exception as e:
        if primary.name == "piper":
            raise
        log.warning(
            "tts: %s unreachable (%s) — falling back to piper",
            primary.name,
            getattr(e, "message", e),
        )
    fallback = tts_engines.get("piper", settings)
    fb_voice = _piper_fallback_voice(settings, voice_path=voice_path, selection=sel)
    return await _synthesize_via(fallback, text, fb_voice)


def _resolve_stream_engines(
    settings: Settings,
    voice_path: Path | None,
    selection: VoiceSelection | None,
) -> tuple[TtsEngine, TtsEngine | None, str, VoiceSelection]:
    """Resolve the primary engine (+ Piper fallback) for a streaming session.

    Returns ``(primary, fallback, fallback_voice, selection)``. ``fallback`` is
    ``None`` when the primary IS Piper (nothing to fall back to). Selection comes
    from *selection* if given, else env/preferences (``auto`` → edge if available,
    else Piper), with *voice_path* as the Piper voice + language hint. Any
    :class:`TtsError` while resolving degrades to Piper rather than aborting.
    """
    sel = selection
    if sel is None:
        try:
            sel = resolve_voice_selection(settings, voice_path=voice_path)
        except TtsError as e:
            log.warning("streaming tts: selection failed (%s) — using piper", e.message)
            sel = VoiceSelection(engine="piper", voice="")
    try:
        primary = tts_engines.get(sel.engine, settings)
    except TtsError as e:
        log.warning("streaming tts: %s — using piper", e.message)
        primary = tts_engines.get("piper", settings)
        sel = VoiceSelection(engine="piper", voice="")
    if primary.name == "piper" and not sel.voice:
        sel = VoiceSelection(
            engine="piper",
            voice=_piper_fallback_voice(settings, voice_path=voice_path, selection=sel),
        )
    fallback: TtsEngine | None = None
    fallback_voice = ""
    if primary.name != "piper":
        fallback = tts_engines.get("piper", settings)
        fallback_voice = _piper_fallback_voice(settings, voice_path=voice_path, selection=sel)
    # Diagnostic: which engine was selected? edge=natural / piper=robotic.
    # A mid-stream demote will ALSO emit the "falling back to piper" warning.
    log.info("streaming tts: engine=%s voice=%s", primary.name, sel.voice or "(default)")
    return primary, fallback, fallback_voice, sel


class _StreamSynthesizer:
    """Per-sentence synth for one streaming session with automatic Piper fallback.

    The primary engine is tried first; a *timeout* skips the sentence but keeps the
    primary (edge may be merely jittery), while an *unreachable* error demotes the
    primary for the REST of the stream so a dead engine does not stall every
    sentence by its timeout. Returns ``(audio_bytes, mime)`` or ``None`` when a
    sentence could not be synthesized (it is dropped, the stream continues).
    """

    def __init__(
        self,
        primary: TtsEngine,
        fallback: TtsEngine | None,
        fallback_voice: str,
        selection: VoiceSelection,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._fallback_voice = fallback_voice
        self._sel = selection
        self._primary_demoted = False

    async def synth_one(self, spoken: str) -> tuple[bytes, str] | None:
        if not self._primary_demoted:
            result = await self._try_primary(spoken)
            if result is not _RETRY_ON_FALLBACK:
                return result
        return await self._try_fallback(spoken)

    async def _try_primary(self, spoken: str) -> tuple[bytes, str] | None | object:
        """Try the primary engine. Returns the synth result, ``None`` (skip this
        sentence), or the ``_RETRY_ON_FALLBACK`` sentinel (primary demoted → use
        the fallback for this and every subsequent sentence)."""
        try:
            return await _synthesize_via(self._primary, spoken, self._sel.voice)
        except TtsTimeout as e:
            # Edge is slow (not unreachable): skip this sentence but do NOT demote —
            # the next sentence will try edge again. Robotic Piper should not interject.
            log.warning(
                "streaming tts: %s timeout (%s) — sentence skipped, keeping %s",
                self._primary.name,
                e,
                self._primary.name,
            )
            return None
        except Exception as e:  # unreachable / other error — fall back for rest of stream
            if self._fallback is None:
                log.warning("streaming tts: synth failed: %s", e)
                return None
            # Demote for the rest of this stream: a dead primary would otherwise
            # stall every sentence by its timeout. Due to prefetch, at most one more
            # sentence (already in-flight) may still try primary; subsequent ones go
            # directly to the fallback.
            self._primary_demoted = True
            log.warning(
                "streaming tts: %s unreachable (%s) — rest of stream via %s",
                self._primary.name,
                e,
                self._fallback.name,
            )
            return _RETRY_ON_FALLBACK

    async def _try_fallback(self, spoken: str) -> tuple[bytes, str] | None:
        if self._fallback is None:
            return None
        try:
            return await _synthesize_via(self._fallback, spoken, self._fallback_voice)
        except TtsError as e:
            log.warning("streaming tts: fallback synth failed: %s", e)
            return None


#: Sentinel: primary engine was demoted → this sentence must retry on the fallback.
_RETRY_ON_FALLBACK = object()


class _PrefetchQueue:
    """FIFO window of concurrently-synthesizing sentences → ordered chunk output.

    Depth-1 prefetch (synthesizing only the next sentence) went SILENT when
    synthesis was slower than playback: edge-tts makes a separate network request
    per sentence. Here up to ``depth`` sentences are synthesized concurrently and
    yielded in FIFO order, so chunks stay ahead of playback and gaps disappear.
    Ready chunks are drained WITHOUT waiting (low first-audio latency); once the
    window is full the oldest is awaited (back-pressure). Order is always preserved.

    Until the first synthesis resolves (``primed``) the window is capped at 2 (one
    prefetch + a dead-primary guard: detect an unreachable primary within at most 2
    sentences → demote → the rest go straight to the fallback, instead of piling up
    N concurrent timeouts on a dead engine). After the primary is proven, the full
    ``depth`` applies.
    """

    def __init__(self, synth: _StreamSynthesizer, *, depth: int = 3) -> None:
        self._synth = synth
        self._depth = depth
        self._seq = 0
        self._primed = False
        self._q: list[tuple[str, _asyncio.Task[tuple[bytes, str] | None]]] = []

    def spawn(self, sentence: str, spoken: str) -> None:
        """Start synthesizing *spoken*, tagged with the *sentence* to display."""
        self._q.append((sentence, _asyncio.ensure_future(self._synth.synth_one(spoken))))

    def _window(self) -> int:
        return self._depth if self._primed else 2

    async def _pop_chunk(self) -> dict[str, object] | None:
        sentence, task = self._q.pop(0)
        self._primed = True
        result = await task
        if result is None:
            return None
        audio, mime = result
        self._seq += 1
        return {
            "seq": self._seq,
            "sentence": sentence,
            "audio_b64": base64.standard_b64encode(audio).decode("ascii"),
            "mime": mime,
        }

    async def drain_ready(self) -> AsyncIterator[dict[str, object]]:
        """Yield every already-finished chunk at the head of the queue, without waiting."""
        while self._q and self._q[0][1].done():
            chunk = await self._pop_chunk()
            if chunk is not None:
                yield chunk

    async def apply_backpressure(self) -> AsyncIterator[dict[str, object]]:
        """While the window is full, await + yield the oldest chunk (order preserved)."""
        while len(self._q) >= self._window():
            chunk = await self._pop_chunk()
            if chunk is not None:
                yield chunk

    async def drain_all(self) -> AsyncIterator[dict[str, object]]:
        """Await + yield every remaining chunk in order (end-of-stream)."""
        while self._q:
            chunk = await self._pop_chunk()
            if chunk is not None:
                yield chunk

    def cancel_pending(self) -> None:
        """Cancel any dangling synthesis tasks (consumer aborted early)."""
        for _sentence, task in self._q:
            task.cancel()


#: Force a sentence break if the delta stream pauses this long mid-turn (see below).
_DEADLINE_S = 1.0


async def stream_text_to_tts_chunks(
    deltas: AsyncIterator[str],
    settings: Settings,
    voice_path: Path | None = None,
    *,
    selection: VoiceSelection | None = None,
) -> AsyncIterator[dict[str, object]]:
    """Yield ``{"seq", "sentence", "audio_b64", "mime"}`` per sentence.

    Engine selection: *selection* (explicit engine+voice via the registry) if given,
    else resolved from env/preferences (``auto`` → edge if available, else Piper),
    with *voice_path* as the Piper voice + language hint.
    """
    primary, fallback, fallback_voice, sel = _resolve_stream_engines(
        settings, voice_path, selection
    )
    synth = _StreamSynthesizer(primary, fallback, fallback_voice, sel)
    queue = _PrefetchQueue(synth)

    # Deadline-flush: during a tool call / model thinking the delta stream pauses
    # temporarily (especially when Claude is calling a tool and emits no text in between).
    # If the buffer contains speakable text, force a sentence break → closes the
    # pathology where "the opening sentence is waiting and gets dumped all at once
    # when the tool finishes".
    # Correct pattern: keep a single waiting task, race it with asyncio.wait —
    # wrapping __anext__ with wait_for corrupts async generator state
    # (StopAsyncIteration → RuntimeError conversion).
    _END = object()

    async def _next_or_end() -> object:
        try:
            return await deltas_iter.__anext__()
        except StopAsyncIteration:
            return _END

    buf = ""
    deltas_iter = deltas.__aiter__()
    pending_get: _asyncio.Task[object] | None = None
    stream_ended = False

    try:
        while not stream_ended:
            if pending_get is None:
                pending_get = _asyncio.create_task(_next_or_end())
            done, _ = await _asyncio.wait({pending_get}, timeout=_DEADLINE_S)
            if pending_get not in done:
                # Deadline fired: if the buffer holds speakable text, force-yield it.
                # Does not apply _MIN_SENT_CHARS (silence > short sentence).
                buf = _flush_deadline(buf, queue)
                async for chunk in queue.drain_ready():
                    yield chunk
                continue
            item = pending_get.result()
            pending_get = None
            if item is _END:
                stream_ended = True
                continue
            if not isinstance(item, str) or not item:
                continue
            buf += item
            leftover: list[str] = []
            async for chunk in _emit_sentences(buf, queue, leftover):
                yield chunk
            buf = leftover[0]
            # AFTER every delta, immediately send any ready prefetch chunks: yield
            # as soon as the first audio synthesis completes, without waiting for
            # the NEXT sentence to finish. (In short single-sentence replies the
            # symptom was "audio starts way too late": the first chunk was only
            # sent when the 2nd sentence finished / during drain.)
            async for chunk in queue.drain_ready():
                yield chunk

        # Stream ended: enqueue the tail, then drain all in order.
        tail = strip_markdown_for_tts(buf.strip())
        if is_speakable_text(tail):
            queue.spawn(buf.strip(), tail)
        async for chunk in queue.drain_all():
            yield chunk
    finally:
        # If the consumer closes early (abort), cancel any dangling synthesis tasks.
        queue.cancel_pending()
        # Also cancel the pending __anext__ task from deadline-flush so it does not
        # leak — otherwise the generator does not exit cleanly → backend never sends
        # the tts_end SSE → client stays with ttsStreamOpen=true → onend never
        # re-opens the microphone.
        if pending_get is not None and not pending_get.done():
            pending_get.cancel()


def _flush_deadline(buf: str, queue: _PrefetchQueue) -> str:
    """Deadline fired mid-turn: enqueue whatever speakable text is buffered. Returns
    the new buffer ("" if flushed, unchanged otherwise)."""
    stripped = buf.strip()
    if not stripped:
        return buf
    spoken = strip_markdown_for_tts(stripped)
    if not is_speakable_text(spoken):
        return buf
    queue.spawn(stripped, spoken)
    return ""


async def _emit_sentences(
    buf: str, queue: _PrefetchQueue, leftover: list[str]
) -> AsyncIterator[dict[str, object]]:
    """Split every complete sentence out of *buf*, enqueue each for synthesis, and
    yield chunks as back-pressure forces the window to drain. The incomplete trailing
    remainder is appended to *leftover* (a single-element out-parameter) so the caller
    can carry it into the next delta."""
    while True:
        sentence, remainder = split_first_sentence(buf)
        if sentence is None:
            break
        buf = remainder
        spoken = strip_markdown_for_tts(sentence)
        if not is_speakable_text(spoken):
            continue
        queue.spawn(sentence, spoken)
        # Back-pressure: while the prefetch window is full, drain the oldest (order
        # is preserved). Until the first synthesis is proven the window is 2.
        async for chunk in queue.apply_backpressure():
            yield chunk
    leftover.append(buf)


__all__ = [
    "VoiceSelection",
    "is_speakable_text",
    "resolve_voice_selection",
    "split_first_sentence",
    "stream_text_to_tts_chunks",
    "strip_markdown_for_tts",
    "synthesize_with_fallback",
]
