"""Microsoft Edge neural TTS engine (``edge-tts`` package) — primary, online.

Free Microsoft neural voices (default ``tr-TR-EmelNeural``), MP3 output.
The package is optional: everything is import-guarded so the server runs
without it (registry then resolves ``auto`` to Piper).

Sync/async bridging: ``synthesize`` is the blocking protocol method. The
streaming pipeline (``streaming_tts``) calls it via ``anyio.to_thread.run_sync``
— i.e. from a worker thread with no running loop, where ``asyncio.run`` is
safe. If some caller ever invokes it on an event-loop thread, we hop to a
private thread instead of nesting loops. Each call owns a fresh loop, so it is
thread-safe.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib.util
import logging
import os as _os
from typing import TYPE_CHECKING, Any

from akana_server.voice.tts import TtsError, TtsTimeout

if TYPE_CHECKING:
    from collections.abc import Coroutine

    from akana_server.config import Settings

log = logging.getLogger(__name__)

MP3_MIME = "audio/mpeg"
DEFAULT_VOICE_TR = "tr-TR-EmelNeural"
DEFAULT_VOICE_EN = "en-US-JennyNeural"

# Edge cold (websocket setup) measured ~1.4s, warm ~0.7s. This value is NO LONGER a
# piper trigger — it is only a safety ceiling that cuts off a genuine hang
# (connected but the stream stalled). If the network is dead, a connection error
# (ClientConnectorError etc.) arrives within seconds and falls back to piper; this
# ceiling is never reached. When Edge is merely slow (jitter) we WAIT for it instead
# of the robotic piper → hence the generous value. If exceeded, the sentence is
# skipped (no fallback to piper). Overridable via env.
_DEFAULT_SYNTH_TIMEOUT_S = 10.0


def _env_timeout(default: float = _DEFAULT_SYNTH_TIMEOUT_S) -> float:
    """Read ``AKANA_TTS_EDGE_TIMEOUT_S`` at import; a malformed value must NOT crash
    the import of the whole voice package (that would take the server down at startup
    and break the README's graceful-degradation promise). Falls back to the default."""
    raw = _os.environ.get("AKANA_TTS_EDGE_TIMEOUT_S")
    if not raw:
        return default
    try:
        val = float(raw)
    except (TypeError, ValueError):
        log.warning(
            "AKANA_TTS_EDGE_TIMEOUT_S=%r is not a number — using default %.0fs",
            raw,
            default,
        )
        return default
    if val <= 0:
        log.warning(
            "AKANA_TTS_EDGE_TIMEOUT_S=%r must be positive — using default %.0fs",
            raw,
            default,
        )
        return default
    return val


SYNTH_TIMEOUT_S = _env_timeout()

# Curated catalog — the full list needs a network call (edge_tts.list_voices()).
_KNOWN_VOICES: tuple[dict[str, str], ...] = (
    {"id": DEFAULT_VOICE_TR, "lang": "tr", "gender": "female"},
    {"id": "tr-TR-AhmetNeural", "lang": "tr", "gender": "male"},
    {"id": DEFAULT_VOICE_EN, "lang": "en", "gender": "female"},
    {"id": "en-US-AriaNeural", "lang": "en", "gender": "female"},
    {"id": "en-US-GuyNeural", "lang": "en", "gender": "male"},
    {"id": "en-GB-SoniaNeural", "lang": "en", "gender": "female"},
)


def _run_coro_blocking(coro: Coroutine[Any, Any, bytes]) -> bytes:
    """Run *coro* to completion from sync code, safe in any calling context."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # On an event-loop thread: never block it with a nested run — use a
    # dedicated thread that owns its own loop.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class EdgeTtsEngine:
    name = "edge"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def available(self) -> bool:
        """Importable ⇒ assumed online-capable.

        Unreachability (connection refused/DNS) surfaces from ``synthesize`` as a
        ``TtsError`` and triggers the Piper fallback in the caller. Mere slowness
        surfaces as a ``TtsTimeout`` (hang ceiling) and does NOT fall back to piper.
        """
        try:
            return importlib.util.find_spec("edge_tts") is not None
        except (ImportError, ValueError):
            return False

    def default_voice(self, lang: str) -> str:
        use_en = (lang or "").strip().lower().startswith("en")
        return DEFAULT_VOICE_EN if use_en else DEFAULT_VOICE_TR

    def synthesize(self, text: str, voice: str) -> tuple[bytes, str]:
        t = (text or "").strip()
        if not t:
            raise TtsError("empty text for TTS", status_code=400)
        limit = max(256, min(int(self._settings.voice_tts_max_chars), 50_000))
        if len(t) > limit:
            t = t[:limit]
        try:
            import edge_tts
        except ImportError as e:
            raise TtsError(
                "edge-tts package is not installed — `pip install edge-tts`.",
                status_code=503,
            ) from e

        async def _synth() -> bytes:
            communicate = edge_tts.Communicate(t, voice or DEFAULT_VOICE_TR)
            buf = bytearray()
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio" and chunk.get("data"):
                    buf += chunk["data"]
            return bytes(buf)

        try:
            audio = _run_coro_blocking(asyncio.wait_for(_synth(), timeout=SYNTH_TIMEOUT_S))
        except TtsError:
            raise
        except TimeoutError as e:  # asyncio.TimeoutError is an alias on 3.11+
            # Edge connected but exceeded the ceiling → SLOW, not unreachable. Distinct
            # exception type so the caller skips the sentence instead of falling back to
            # piper, and stays on edge.
            raise TtsTimeout(
                f"edge-tts exceeded the {SYNTH_TIMEOUT_S:.0f}s hang ceiling", status_code=503
            ) from e
        except Exception as e:
            # Connection refused / DNS / websocket could not be set up → unreachable → piper.
            raise TtsError(f"edge-tts unreachable: {e}", status_code=503) from e
        if not audio:
            raise TtsError("edge-tts produced empty audio", status_code=503)
        return audio, MP3_MIME

    def list_voices(self) -> list[dict[str, Any]]:
        return [{**v, "name": v["id"], "engine": self.name} for v in _KNOWN_VOICES]


__all__ = [
    "DEFAULT_VOICE_EN",
    "DEFAULT_VOICE_TR",
    "EdgeTtsEngine",
    "MP3_MIME",
    "SYNTH_TIMEOUT_S",
]
