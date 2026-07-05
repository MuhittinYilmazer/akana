"""Piper TTS engine adapter — offline fallback, wraps ``voice/tts.py`` 1:1.

Output is identical to the legacy path: WAV bytes + ``audio/wav``. Voice ids
are ``.onnx`` model paths.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import TYPE_CHECKING, Any

from akana_server.voice.tts import list_available_voices, synthesize_wav_sync

if TYPE_CHECKING:
    from akana_server.config import Settings

WAV_MIME = "audio/wav"


class PiperEngine:
    name = "piper"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def available(self) -> bool:
        try:
            if importlib.util.find_spec("piper") is None:
                return False
        except (ImportError, ValueError):
            return False
        s = self._settings
        return s.piper_voice_tr.is_file() or s.piper_voice_en.is_file()

    def default_voice(self, lang: str) -> str:
        s = self._settings
        use_en = (lang or "").strip().lower().startswith("en")
        return str(s.piper_voice_en if use_en else s.piper_voice_tr)

    def synthesize(self, text: str, voice: str) -> tuple[bytes, str]:
        return synthesize_wav_sync(text, self._settings, Path(voice)), WAV_MIME

    def list_voices(self) -> list[dict[str, Any]]:
        return [
            {**v, "id": v.get("path"), "engine": self.name}
            for v in list_available_voices(self._settings)
        ]


__all__ = ["PiperEngine", "WAV_MIME"]
