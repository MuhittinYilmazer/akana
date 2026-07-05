"""Piper TTS: text → WAV bytes (worker thread, cached voice models)."""

from __future__ import annotations

import io
import logging
import threading
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from akana_server.config import Settings

log = logging.getLogger(__name__)

_voice_lock = threading.Lock()
_voices: dict[str, object] = {}

# Piper's espeak-ng phonemization keeps process-wide global state and is NOT
# thread-safe under concurrent ``.synthesize()`` calls; streaming prefetch
# (PREFETCH_DEPTH) can call the same shared ``PiperVoice`` from several worker
# threads at once → garbled/corrupted audio. We serialize synthesis with this lock
# (loading is guarded by ``_voice_lock``).
_synth_lock = threading.Lock()


class TtsError(Exception):
    def __init__(self, message: str, *, status_code: int = 503) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class TtsTimeout(TtsError):
    """Engine (e.g. edge) is alive but TOO SLOW — the hang ceiling was exceeded.

    A separate type, because the calling layer distinguishes this from
    "unreachable" (→ piper): under slowness we skip the sentence and stay on edge
    rather than dropping to the robotic piper voice.
    """


def resolve_tts_lang(
    settings: Settings,
    *,
    tts_lang: str | None,
    stt_lang: str | None = None,
) -> str:
    """Map ``auto|tr|en`` (with optional STT detection) to a concrete language."""
    raw = (tts_lang or "auto").strip().lower()
    if raw == "auto":
        sl = (stt_lang or "").strip().lower()
        if sl.startswith("tr"):
            return "tr"
        if sl.startswith("en"):
            return "en"
        return settings.primary_lang
    if raw in ("tr", "en"):
        return raw
    raise TtsError(f"invalid tts_lang: {tts_lang!r} (use auto, tr, en)", status_code=400)


def resolve_tts_voice_path(
    settings: Settings,
    *,
    tts_lang: str | None,
    stt_lang: str | None = None,
) -> Path:
    """Map ``auto|tr|en`` (with optional STT detection) to a Piper .onnx path."""
    lang = resolve_tts_lang(settings, tts_lang=tts_lang, stt_lang=stt_lang)
    path = settings.piper_voice_en if lang == "en" else settings.piper_voice_tr
    if not path.is_file():
        raise TtsError(
            f"Piper voice model not found: {path}. Run: python akana.py setup --voice piper",
            status_code=503,
        )
    return path


def list_available_voices(settings: Settings) -> list[dict[str, Any]]:
    """Return all .onnx files in ``voices_dir`` plus the configured tr/en pair."""
    out: dict[str, dict[str, Any]] = {}
    configured = {"tr": settings.piper_voice_tr, "en": settings.piper_voice_en}
    for lang, path in configured.items():
        out[str(path)] = {
            "lang": lang,
            "name": path.name,
            "path": str(path),
            "exists": path.is_file(),
            "size_bytes": path.stat().st_size if path.is_file() else 0,
            "configured": True,
        }
    if settings.voices_dir.is_dir():
        for onnx in sorted(settings.voices_dir.glob("*.onnx")):
            key = str(onnx.resolve())
            if key in out:
                continue
            lower = onnx.name.lower()
            lang = "tr" if lower.startswith("tr") else "en" if lower.startswith("en") else "?"
            out[key] = {
                "lang": lang,
                "name": onnx.name,
                "path": str(onnx),
                "exists": True,
                "size_bytes": onnx.stat().st_size,
                "configured": False,
            }
    return list(out.values())


def _get_piper_voice(path: Path) -> object:
    key = str(path.resolve())
    with _voice_lock:
        if key not in _voices:
            if not path.is_file():
                raise TtsError(
                    f"Piper voice model not found: {path}. Run: python akana.py setup --voice piper",
                    status_code=503,
                )
            try:
                from piper import PiperVoice
            except ImportError as e:
                raise TtsError(
                    "piper-tts is not installed — `python akana.py setup --voice piper`.",
                    status_code=503,
                ) from e
            _voices[key] = PiperVoice.load(str(path))
            log.info("piper: loaded voice %s", path.name)
        return _voices[key]


def _synthesize_sync(text: str, settings: Settings, voice_path: Path) -> bytes:
    t = text.strip()
    if not t:
        raise TtsError("empty assistant text for TTS", status_code=400)
    limit = max(256, min(int(settings.voice_tts_max_chars), 50_000))
    if len(t) > limit:
        t = t[:limit]

    voice: Any = _get_piper_voice(voice_path)
    # Prefer ``synthesize()`` chunks: ``synthesize_wav`` can raise
    # "# channels not specified" when Piper returns no audio (e.g. "—", "...").
    # ``_synth_lock``: espeak global state is corrupted under concurrent synthesis (see above).
    with _synth_lock:
        chunks = list(voice.synthesize(t))
    if not chunks:
        raise TtsError("TTS produced no audio for text", status_code=503)

    buf = io.BytesIO()
    first = chunks[0]
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(int(first.sample_channels))
        wf.setsampwidth(int(first.sample_width))
        wf.setframerate(int(first.sample_rate))
        for chunk in chunks:
            wf.writeframes(chunk.audio_int16_bytes)
    out = buf.getvalue()
    if len(out) < 44:
        raise TtsError("TTS produced empty WAV", status_code=503)
    return out


def synthesize_wav_sync(text: str, settings: Settings, voice_path: Path) -> bytes:
    """Blocking Piper synth with error mapping (raises :class:`TtsError` on failure)."""
    try:
        return _synthesize_sync(text, settings, voice_path)
    except TtsError:
        raise
    except Exception as e:
        log.warning("piper synthesize failed: %s", e)
        raise TtsError(f"speech synthesis failed: {e}", status_code=503) from e
