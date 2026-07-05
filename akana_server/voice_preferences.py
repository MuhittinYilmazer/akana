"""Persisted voice UI preferences (wake autostart, TTS engine/voices, etc.)."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from akana_server.json_store import lock_for, write_json_atomic

log = logging.getLogger(__name__)

_PREFERENCES_FILE = "voice_preferences.json"

VALID_TTS_ENGINES = ("auto", "edge", "piper", "xtts")
DEFAULT_TTS_VOICE_TR = "tr-TR-EmelNeural"
DEFAULT_TTS_VOICE_EN = "en-US-JennyNeural"


@dataclass
class VoicePreferences:
    wake_autostart: bool = False
    stream_tts: bool = False
    # "auto" = edge when available, else piper. Env AKANA_TTS_ENGINE overrides.
    tts_engine: str = "auto"
    # Neural (edge) voice names per language; Piper voices stay path-based in Settings.
    tts_voice_tr: str = DEFAULT_TTS_VOICE_TR
    tts_voice_en: str = DEFAULT_TTS_VOICE_EN

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _preferences_path(data_dir: Path) -> Path:
    return data_dir / _PREFERENCES_FILE


def _env_wake_autostart() -> bool | None:
    raw = os.environ.get("AKANA_WAKE_AUTOSTART", "").strip().lower()
    if not raw:
        return None
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return None


def defaults_from_env() -> VoicePreferences:
    env = _env_wake_autostart()
    if env is not None:
        return VoicePreferences(wake_autostart=env)
    return VoicePreferences()


def _clean_engine(raw: Any, fallback: str) -> str:
    value = str(raw or "").strip().lower()
    if value in VALID_TTS_ENGINES:
        return value
    log.warning("invalid tts_engine %r — keeping %r", raw, fallback)
    return fallback


def _clean_voice(raw: Any, fallback: str) -> str:
    value = str(raw or "").strip()
    return value if value else fallback


def _merge(base: VoicePreferences, raw: dict[str, Any]) -> VoicePreferences:
    wake_autostart = base.wake_autostart
    stream_tts = base.stream_tts
    tts_engine = base.tts_engine
    tts_voice_tr = base.tts_voice_tr
    tts_voice_en = base.tts_voice_en
    if "wake_autostart" in raw:
        wake_autostart = bool(raw["wake_autostart"])
    if "stream_tts" in raw:
        stream_tts = bool(raw["stream_tts"])
    if "tts_engine" in raw:
        tts_engine = _clean_engine(raw["tts_engine"], base.tts_engine)
    if "tts_voice_tr" in raw:
        tts_voice_tr = _clean_voice(raw["tts_voice_tr"], base.tts_voice_tr)
    if "tts_voice_en" in raw:
        tts_voice_en = _clean_voice(raw["tts_voice_en"], base.tts_voice_en)
    return VoicePreferences(
        wake_autostart=wake_autostart,
        stream_tts=stream_tts,
        tts_engine=tts_engine,
        tts_voice_tr=tts_voice_tr,
        tts_voice_en=tts_voice_en,
    )


def load_voice_preferences(data_dir: Path) -> VoicePreferences:
    base = defaults_from_env()
    path = _preferences_path(data_dir)
    if not path.is_file():
        return base
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("could not read voice preferences %s: %s", path, e)
        return base
    if not isinstance(raw, dict):
        return base
    return _merge(base, raw)


def save_voice_preferences(data_dir: Path, prefs: VoicePreferences) -> VoicePreferences:
    data_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(_preferences_path(data_dir), prefs.to_dict())
    return prefs


def update_voice_preferences(data_dir: Path, patch: dict[str, Any]) -> VoicePreferences:
    # Under lock: load+merge+save → concurrent PATCHes do not cause lost updates.
    with lock_for(data_dir):
        return save_voice_preferences(
            data_dir, _merge(load_voice_preferences(data_dir), patch)
        )
