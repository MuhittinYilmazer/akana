"""WAV → float32 mono 16 kHz → faster-whisper (worker thread, optional dep)."""

from __future__ import annotations

import io
import logging
import threading
import wave
from typing import TYPE_CHECKING, Any

import anyio
import numpy as np

if TYPE_CHECKING:
    from akana_server.config import Settings

log = logging.getLogger(__name__)

_model_lock = threading.Lock()
_model: object | None = None
_model_device: str | None = None


def _reset_whisper_model() -> None:
    global _model, _model_device
    with _model_lock:
        _model = None
        _model_device = None


def _is_cuda_runtime_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    needles = (
        "libcublas",
        "cublas",
        "cudnn",
        "cuda",
        "cudart",
        "out of memory",
        "no cuda",
    )
    return any(n in msg for n in needles)


def _cpu_compute_type(settings: Settings) -> str:
    ct = (settings.whisper_compute_type or "int8").strip()
    if ct.startswith("float16") or ct.startswith("float32"):
        return "int8"
    return ct


class SttError(Exception):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def decode_wav_to_float_mono16k(
    wav_bytes: bytes,
    *,
    max_seconds: float,
) -> np.ndarray:
    """Decode RIFF WAV → mono float32 in [-1, 1] @ 16 kHz."""
    if not wav_bytes or len(wav_bytes) < 44:
        raise SttError("empty or too small WAV payload", status_code=400)
    buf = io.BytesIO(wav_bytes)
    try:
        with wave.open(buf, "rb") as wf:
            n_channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            framerate = wf.getframerate()
            n_frames = wf.getnframes()
            if n_channels not in (1, 2) or sample_width != 2 or framerate <= 0:
                raise SttError(
                    "unsupported WAV: need 16-bit PCM mono or stereo, any sample rate",
                    status_code=400,
                )
            max_frames = int(max(0.1, max_seconds) * framerate)
            if n_frames > max_frames:
                n_frames = max_frames
            raw = wf.readframes(n_frames)
    except wave.Error as e:
        raise SttError(f"invalid WAV: {e}", status_code=400) from e

    if not raw:
        raise SttError("WAV contains no audio frames", status_code=400)

    bytes_per_frame = sample_width * n_channels
    if len(raw) % bytes_per_frame != 0:
        raise SttError("invalid WAV: truncated audio frame", status_code=400)

    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if n_channels == 2:
        x = x.reshape(-1, 2).mean(axis=1)

    if framerate == 16000:
        return x

    target_len = max(1, round(len(x) * 16000 / framerate))
    t_src = np.linspace(0.0, float(len(x) - 1), num=len(x), dtype=np.float64)
    t_dst = np.linspace(0.0, float(len(x) - 1), num=target_len, dtype=np.float64)
    return np.interp(t_dst, t_src, x.astype(np.float64)).astype(np.float32)


def _load_whisper_model(settings: Settings, device: str) -> object:
    from faster_whisper import WhisperModel

    model_name = (settings.whisper_model or "small").strip()
    dev = device.strip().lower()
    compute = (
        _cpu_compute_type(settings) if dev == "cpu" else (settings.whisper_compute_type or "int8").strip()
    )
    return WhisperModel(model_name, device=dev, compute_type=compute)


def _get_whisper_model(settings: Settings, *, device: str | None = None) -> object:
    global _model, _model_device
    device_pref = (device or settings.whisper_device or "auto").strip().lower()

    with _model_lock:
        if _model is not None and (device is None or _model_device == device_pref):
            return _model
        try:
            from faster_whisper import WhisperModel  # noqa: F401
        except ImportError as e:
            raise SttError(
                "faster-whisper is not installed — run `python akana.py setup --voice full`.",
                status_code=503,
            ) from e

        if device_pref in ("auto", ""):
            last_err: Exception | None = None
            for dev in ("cuda", "cpu"):
                try:
                    m = _load_whisper_model(settings, dev)
                except Exception as e:
                    last_err = e
                    if dev == "cuda":
                        log.info("whisper: CUDA init failed (%s), trying CPU", e)
                    continue
                _model = m
                _model_device = dev
                log.info("whisper: loaded on %s", dev)
                return _model
            raise SttError(f"whisper init failed: {last_err}", status_code=503) from last_err

        try:
            _model = _load_whisper_model(settings, device_pref)
        except Exception as e:
            raise SttError(f"whisper init failed: {e}", status_code=503) from e
        _model_device = device_pref
        log.info("whisper: loaded on %s", device_pref)
        return _model


def _transcribe_with_model(
    model: Any,
    audio: np.ndarray,
    language: str | None,
    initial_prompt: str | None = None,
) -> tuple[str, str | None]:
    segments, info = model.transcribe(
        audio,
        language=language if language else None,
        vad_filter=True,
        initial_prompt=initial_prompt or None,
    )
    parts: list[str] = []
    for seg in segments:
        t = getattr(seg, "text", None)
        if isinstance(t, str) and t.strip():
            parts.append(t.strip())
    text = " ".join(parts).strip()
    lang = getattr(info, "language", None)
    detected = lang.strip() if isinstance(lang, str) and lang.strip() else None
    return text, detected


def _resolve_stt_prompt(settings: Settings) -> str | None:
    """STT ``initial_prompt``: term glossary → Whisper writes mixed technical terms
    more accurately (model/language unchanged, speed cost ~zero).
    Runtime setting > env(Settings). Empty → no prompt (behaviour-neutral)."""
    try:
        from akana_server.runtime_settings import get_runtime

        v = get_runtime("whisper_prompt", settings)
        if isinstance(v, str) and v.strip():
            return v.strip()
    except Exception:  # setting resolution must never break STT
        pass
    p = (getattr(settings, "whisper_prompt", "") or "").strip()
    return p or None


def _fallback_to_cpu_model(settings: Settings) -> object:
    """Perform the CUDA→CPU transition ATOMICALLY: device-check + reset + CPU-reload
    in a single ``_model_lock`` acquisition.

    Previously ``_transcribe_sync`` read the global ``_model_device`` and called
    ``_reset_whisper_model()`` + CPU-reload without holding the lock. Concurrent
    transcriptions in anyio worker threads could interleave reset/reload on a CUDA OOM;
    or a third request could reload onto CUDA between the reset and the CPU-reload →
    intermittent 503s / wasted CUDA loads / half-initialised model. Here, while one
    thread does reset+CPU-reload all others wait; idempotent — if the model has already
    been moved to CPU it is reused (no redundant second reload). Long transcriptions
    stay OUTSIDE the lock (no new deadlock); only model-management mutations are locked.
    """
    global _model, _model_device
    with _model_lock:
        if _model is not None and _model_device == "cpu":
            return _model  # another thread already switched to CPU
        _model = _load_whisper_model(settings, "cpu")
        _model_device = "cpu"
        log.info("whisper: reloaded on cpu (CUDA fallback)")
        return _model


def _transcribe_sync(
    audio: np.ndarray, settings: Settings, language: str | None
) -> tuple[str, str | None]:
    model: Any = _get_whisper_model(settings)
    prompt = _resolve_stt_prompt(settings)
    try:
        return _transcribe_with_model(model, audio, language, prompt)
    except Exception as e:
        # Read ``_model_device`` WITHOUT a lock (TOCTOU): if another thread has
        # already switched to CPU, a "cuda" check would wrongly drop this request
        # to a 503. ``_fallback_to_cpu_model`` is already locked + idempotent (if
        # already on CPU it returns the current model) → gate only on error class.
        if _is_cuda_runtime_error(e):
            log.warning("whisper CUDA transcribe failed (%s), reloading on CPU", e)
            cpu_model = _fallback_to_cpu_model(settings)
            return _transcribe_with_model(cpu_model, audio, language, prompt)
        raise


def _decode_and_transcribe_sync(
    wav_bytes: bytes, settings: Settings, language: str | None
) -> tuple[str, str | None]:
    # WAV parsing + linear resampling (np.interp, millions of samples) would block
    # the event loop for hundreds of ms on a large upload; moved onto the same
    # thread as transcribe.
    audio = decode_wav_to_float_mono16k(
        wav_bytes, max_seconds=settings.voice_max_record_seconds
    )
    if audio.size < 160:
        raise SttError("audio too short", status_code=400)
    return _transcribe_sync(audio, settings, language)


async def transcribe_wav_bytes(
    wav_bytes: bytes,
    settings: Settings,
    *,
    language: str | None = None,
) -> tuple[str, str | None]:
    try:
        return await anyio.to_thread.run_sync(
            _decode_and_transcribe_sync, wav_bytes, settings, language
        )
    except SttError:
        raise
    except Exception as e:
        log.warning("whisper transcribe failed: %s", e)
        raise SttError(f"speech recognition failed: {e}", status_code=503) from e
