"""Canonical settings-default literals — the single source of truth.

Previously the same default value was written 2-3 times: once as a
:class:`akana_server.config.Settings` dataclass field default, once as the
``load_settings()`` env fallback, and (for the ~20 runtime-tunable fields) a
third time as ``default=`` in :mod:`akana_server.runtime_settings.schema`.
Changing a default in one place silently diverged the others.

Now every one of those three sites reads its literal from :data:`DEFAULTS`
here, so a default changes in EXACTLY one place. Values are the ground truth;
this module intentionally imports nothing from ``akana_server`` (a pure leaf,
no import-cycle edges).

The keys are the ``Settings`` field / ``settings_attr`` names. The
``tests/unit/test_settings_defaults_single_source.py`` guard asserts that the
schema ``default`` for every overlapping key equals ``DEFAULTS[key]`` so the
two consumers can never drift.
"""

from __future__ import annotations

from typing import Any

#: Canonical default value for each duplicated setting, keyed by the
#: ``Settings`` field name (which is also the runtime ``settings_attr``).
DEFAULTS: dict[str, Any] = {
    # -- LLM provider / model ------------------------------------------------
    "tts_engine": "",
    "llm_provider": "",
    "claude_bin": "claude",
    "claude_model": "claude-sonnet-4-6",
    "claude_bridge_timeout": 1800.0,
    "bridge_timeout": 1800.0,
    "llm_chat_titles": True,
    # -- session closer ------------------------------------------------------
    "session_closer_enabled": True,
    "session_closer_interval": 300.0,
    "session_closer_idle_minutes": 30,
    "session_closer_char_threshold": 4000,
    "session_closer_max_chars": 6000,
    # -- session summaries ---------------------------------------------------
    "session_summary_inject_enabled": True,
    "session_summary_inject_max_chars": 1500,
    "summary_consolidation_enabled": True,
    "summary_consolidation_interval": 3600.0,
    "summary_consolidation_min_overlap": 2,
    # -- telegram connector --------------------------------------------------
    "telegram_enabled": False,
    "telegram_allowed_chat_ids": (),
    # -- ollama --------------------------------------------------------------
    "ollama_url": "http://localhost:11434",
    "ollama_model": "llama3.1",
    # -- file roots / uploads ------------------------------------------------
    "file_roots": (),
    "uploads_enabled": True,
    "upload_max_mb": 10.0,
    # -- wake word -----------------------------------------------------------
    # 0.5 suits the shipped hey_akana model (validation: true-accept 0.96,
    # hard-neg false-accept 0.015 at 0.5); raise toward 0.7 to cut false wakes.
    "wake_threshold": 0.5,
    # Sustain gate: a trigger requires this many CONSECUTIVE 80 ms frames at/above
    # the threshold within one poll window, not a single peak frame. The peak of a
    # ~3 s window offers ~37 chances for one spurious frame to cross; requiring a run
    # of >=N frames (openWakeWord's own debounce guidance) collapses false wakes with
    # negligible recall loss — a real "hey akana" produces many consecutive hot frames.
    # 1 = legacy single-frame behavior; 3 is a safe default; raise toward 5 to be stricter.
    "wake_min_frames": 3,
    # -- Gemini Live ---------------------------------------------------------
    "gemini_live_enabled": False,
    "gemini_live_model": "models/gemini-2.5-flash-native-audio-latest",
    "gemini_live_voice": "Charon",
    # -- OpenAI Realtime -----------------------------------------------------
    "openai_realtime_enabled": False,
    "openai_realtime_model": "gpt-4o-realtime-preview",
    "openai_realtime_voice": "alloy",
}
