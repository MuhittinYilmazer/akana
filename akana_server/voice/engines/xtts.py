"""XTTS-v2 (Coqui) TTS engine — local, multilingual, Turkish + voice cloning.

Optional engine: requires ``coqui-tts`` + ``torch`` (both optional dependencies;
without them the registry resolves ``auto`` to edge/piper). The model is CPML
licensed (non-commercial) → for personal use it is auto-accepted via
``COQUI_TOS_AGREED=1``; in the OSS distribution this engine is user opt-in (the
model is not bundled, ~2GB is downloaded on first synth).

Hardware: ~4GB VRAM on an RTX 3060 6GB, ~1-3s/sentence. The model is loaded once
IN-PROCESS and cached (loading on every synth would be catastrophic). GPU calls
are serialized with a single lock (concurrent tts → CUDA race/OOM risk).

Voice id format (lang + optional speaker):
* ``"tr"`` / ``"en"``           → that language, default built-in speaker
* ``"tr|Claribel Dervla"``      → built-in speaker name
* ``"tr|/path/ref.wav"``        → CLONE the reference voice
Additionally, if ``<data_dir>/voices/xtts_ref.wav`` exists, it is treated as the
auto clone reference when no speaker is given.
"""

from __future__ import annotations

import importlib.util
import io
import logging
import os
import threading
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Any

from akana_server.voice.tts import TtsError

if TYPE_CHECKING:
    from akana_server.config import Settings

log = logging.getLogger(__name__)

WAV_MIME = "audio/wav"
_MODEL_ID = "tts_models/multilingual/multi-dataset/xtts_v2"
#: Languages supported by XTTS-v2 (model card). Unknown → falls back to 'tr'.
_SUPPORTED_LANGS = frozenset(
    {"tr", "en", "es", "fr", "de", "it", "pt", "pl", "ru", "nl", "cs",
     "ar", "zh-cn", "hu", "ko", "ja", "hi"}
)
_DEFAULT_SR = 24000  # XTTS-v2 output sample rate (used if the model reports one)

# Auto-accept the CPML license for personal use — otherwise the TTS package asks
# for interactive confirmation on first load (which hangs on a server).
os.environ.setdefault("COQUI_TOS_AGREED", "1")

# In-process singleton model + load/synth locks.
_MODEL: Any = None
_LOAD_LOCK = threading.Lock()
_SYNTH_LOCK = threading.Lock()
_DEFAULT_SPEAKER: str | None = None


def _device() -> str:
    try:
        import torch

        return "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:  # noqa: BLE001 - if torch is missing/broken, use CPU
        return "cpu"


def _load_model() -> Any:
    """Load XTTS-v2 once (cached). The first call downloads the model (~2GB)."""
    global _MODEL, _DEFAULT_SPEAKER
    if _MODEL is not None:
        return _MODEL
    with _LOAD_LOCK:
        if _MODEL is None:
            from TTS.api import TTS  # coqui-tts

            dev = _device()
            log.info("loading XTTS-v2 (device=%s) — the model is downloaded on first run…", dev)
            model = TTS(_MODEL_ID)
            model.to(dev)
            speakers = list(getattr(model, "speakers", None) or [])
            _DEFAULT_SPEAKER = speakers[0] if speakers else None
            _MODEL = model
            log.info("XTTS-v2 ready (device=%s, %d built-in speakers)", dev, len(speakers))
    return _MODEL


def prewarm() -> bool:
    """Load the model in the background → shift the cold-start (~38s) to startup.

    Idempotent (``_load_model`` is cached + locked). Errors are SWALLOWED: even if
    prewarm fails, the first synth falls back to lazy loading. Should only be called
    when ``tts_engine=xtts`` (otherwise it wastes ~4GB VRAM + ~38s). Returns: whether
    it loaded.
    """
    try:
        _load_model()
        log.info("XTTS-v2 prewarm complete (the first voice reply will be fast)")
        return True
    except Exception:  # noqa: BLE001 - prewarm NEVER breaks startup
        log.warning(
            "XTTS-v2 prewarm failed — the first synth will load lazily", exc_info=True
        )
        return False


def _to_wav_bytes(wav: Any, sample_rate: int) -> bytes:
    """XTTS float waveform (list[float], [-1,1]) → 16-bit PCM WAV bytes."""
    import numpy as np

    arr = np.asarray(wav, dtype=np.float32)
    arr = np.clip(arr, -1.0, 1.0)
    pcm = (arr * 32767.0).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sample_rate))
        w.writeframes(pcm)
    return buf.getvalue()


class XttsEngine:
    name = "xtts"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def available(self) -> bool:
        """Cheap readiness probe: are ``coqui-tts`` (TTS) + ``torch`` importable?"""
        try:
            return (
                importlib.util.find_spec("TTS") is not None
                and importlib.util.find_spec("torch") is not None
            )
        except (ImportError, ValueError):
            return False

    def default_voice(self, lang: str) -> str:
        """Voice id by language (the speaker is resolved at synth time)."""
        return "en" if (lang or "").strip().lower().startswith("en") else "tr"

    def _parse_voice(self, voice: str) -> tuple[str, str]:
        """``"<lang>"`` or ``"<lang>|<speaker_or_wav>"`` → (lang, speaker)."""
        raw = (voice or "tr").strip()
        if "|" in raw:
            lang, spk = raw.split("|", 1)
            lang, spk = lang.strip().lower(), spk.strip()
        else:
            lang, spk = raw.lower(), ""
        if lang not in _SUPPORTED_LANGS:
            lang = "tr"
        return lang, spk

    def _reference_wav(self, speaker: str) -> Path | None:
        """Cloning reference voice: explicit .wav path or data_dir/voices/xtts_ref.wav."""
        if speaker.lower().endswith(".wav"):
            p = Path(speaker)
            return p if p.is_file() else None
        cand = self._settings.voices_dir / "xtts_ref.wav"
        return cand if cand.is_file() else None

    def synthesize(self, text: str, voice: str) -> tuple[bytes, str]:
        t = (text or "").strip()
        if not t:
            raise TtsError("empty text for TTS", status_code=400)
        limit = max(256, min(int(self._settings.voice_tts_max_chars), 50_000))
        if len(t) > limit:
            t = t[:limit]
        lang, speaker = self._parse_voice(voice)
        try:
            model = _load_model()
        except ImportError as e:
            raise TtsError(
                "coqui-tts/torch is not installed for XTTS — "
                "`pip install coqui-tts` + a compatible torch.",
                status_code=503,
            ) from e
        except Exception as e:  # noqa: BLE001 - model download/load error
            raise TtsError(f"could not load the XTTS-v2 model: {e}", status_code=503) from e

        kwargs: dict[str, Any] = {"text": t, "language": lang}
        ref = self._reference_wav(speaker)
        if ref is not None:
            kwargs["speaker_wav"] = str(ref)  # voice cloning
        elif speaker:
            kwargs["speaker"] = speaker  # built-in speaker name
        elif _DEFAULT_SPEAKER:
            kwargs["speaker"] = _DEFAULT_SPEAKER  # default built-in speaker
        try:
            with _SYNTH_LOCK:  # serialize GPU calls (avoid race/OOM)
                wav = model.tts(**kwargs)
                sr = int(getattr(model.synthesizer, "output_sample_rate", _DEFAULT_SR))
        except Exception as e:  # noqa: BLE001 - synth error → caller falls back to piper
            raise TtsError(f"XTTS synthesis error: {e}", status_code=503) from e
        audio = _to_wav_bytes(wav, sr)
        if not audio:
            raise TtsError("XTTS produced empty audio", status_code=503)
        return audio, WAV_MIME

    def list_voices(self) -> list[dict[str, Any]]:
        return [
            {"id": "tr", "name": "XTTS Türkçe (local)", "lang": "tr", "engine": self.name},
            {"id": "en", "name": "XTTS English (local)", "lang": "en", "engine": self.name},
        ]


__all__ = ["WAV_MIME", "XttsEngine", "prewarm"]
