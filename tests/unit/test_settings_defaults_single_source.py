"""Single-source-of-truth guard for settings defaults (platform:arch:0).

The same default literal used to be declared 2-3 times: a ``Settings`` dataclass
field default, a ``load_settings()`` env fallback, and a ``schema.py`` ``default=``.
They are now all sourced from :data:`akana_server.settings_defaults.DEFAULTS`.

These tests fail if any of the three sites drifts from the canonical table, so a
default can only be changed in ONE place.
"""

from __future__ import annotations

import dataclasses


from akana_server import config as cfg
from akana_server.config import Settings, load_settings
from akana_server.runtime_settings.schema import SCHEMA
from akana_server.settings_defaults import DEFAULTS


def _dataclass_field_defaults() -> dict[str, object]:
    """Every ``Settings`` field that carries a literal default (not MISSING)."""
    out: dict[str, object] = {}
    for f in dataclasses.fields(Settings):
        if f.default is not dataclasses.MISSING:
            out[f.name] = f.default
    return out


# A few canonical keys map to REQUIRED Settings fields (no dataclass default —
# their value is resolved only inside load_settings via env/default), so they
# are asserted by the schema + load_settings tests, not the dataclass one.
_REQUIRED_FIELD_KEYS = {"bridge_timeout", "wake_threshold", "wake_min_frames"}


def test_dataclass_field_defaults_match_canonical_table():
    """Each Settings field default that is in the table equals DEFAULTS[field]."""
    field_defaults = _dataclass_field_defaults()
    for key, canonical in DEFAULTS.items():
        if key in _REQUIRED_FIELD_KEYS:
            continue  # no dataclass default (env-resolved in load_settings)
        assert key in field_defaults, f"{key} is not a Settings field default"
        assert field_defaults[key] == canonical, (
            f"Settings.{key} default {field_defaults[key]!r} != DEFAULTS[{key!r}] {canonical!r}"
        )


def test_schema_defaults_match_canonical_table():
    """Each schema spec whose key is in DEFAULTS uses that exact default."""
    for key, canonical in DEFAULTS.items():
        spec = SCHEMA.get(key)
        if spec is None:
            # A few Settings-only fields (e.g. tts_engine/llm_provider) have no
            # runtime spec — those are exercised by the dataclass test only.
            continue
        assert spec.default == canonical, (
            f"schema[{key!r}].default {spec.default!r} != DEFAULTS[{key!r}] {canonical!r}"
        )


def test_schema_and_dataclass_agree_on_overlap():
    """The ~20 overlapping fields have IDENTICAL defaults across both consumers."""
    field_defaults = _dataclass_field_defaults()
    overlap = [k for k in DEFAULTS if k in SCHEMA and k in field_defaults]
    # Sanity: the overlap set is the expected size (guards against silent shrinkage).
    assert len(overlap) >= 20, f"expected >=20 overlapping keys, got {len(overlap)}"
    for key in overlap:
        assert field_defaults[key] == SCHEMA[key].default == DEFAULTS[key], key


def test_load_settings_uses_canonical_defaults(monkeypatch, tmp_path):
    """With every relevant env var unset, load_settings() yields DEFAULTS values.

    Proves the ``load_settings()`` fallbacks are the canonical literals, not a
    stale second copy.
    """
    # Point data_dir at a scratch dir; clear the env vars whose default we assert.
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    env_to_clear = [
        "CURSOR_BRIDGE_TIMEOUT", "CLAUDE_BRIDGE_TIMEOUT", "CLAUDE_BIN", "CLAUDE_MODEL",
        "WAKE_THRESHOLD", "WAKE_MIN_FRAMES", "AKANA_SESSION_CLOSER_INTERVAL",
        "AKANA_SESSION_CLOSER_IDLE_MINUTES", "AKANA_SESSION_CLOSER_CHAR_THRESHOLD",
        "AKANA_SESSION_CLOSER_MAX_CHARS", "AKANA_SESSION_SUMMARY_INJECT_MAX_CHARS",
        "AKANA_SUMMARY_CONSOLIDATION_INTERVAL", "AKANA_SUMMARY_CONSOLIDATION_MIN_OVERLAP",
        "AKANA_OLLAMA_URL", "AKANA_OLLAMA_MODEL", "AKANA_UPLOAD_MAX_MB",
        "AKANA_GEMINI_LIVE_MODEL", "AKANA_GEMINI_LIVE_VOICE",
        "AKANA_OPENAI_REALTIME_MODEL", "AKANA_OPENAI_REALTIME_VOICE",
    ]
    for name in env_to_clear:
        monkeypatch.delenv(name, raising=False)

    settings = load_settings()
    for key in (
        "bridge_timeout", "claude_bridge_timeout", "claude_bin", "claude_model",
        "wake_threshold", "wake_min_frames", "session_closer_interval", "session_closer_idle_minutes",
        "session_closer_char_threshold", "session_closer_max_chars",
        "session_summary_inject_max_chars", "summary_consolidation_interval",
        "summary_consolidation_min_overlap", "ollama_url", "ollama_model",
        "upload_max_mb", "gemini_live_model", "gemini_live_voice",
        "openai_realtime_model", "openai_realtime_voice",
    ):
        assert getattr(settings, key) == DEFAULTS[key], key


def test_no_duplicate_default_literal_left_in_config_source():
    """The most drift-prone model strings must appear ONCE (in DEFAULTS), not re-typed
    as bare literals in config.py's Settings/load_settings."""
    src = (cfg.__file__)
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    for literal in (
        "claude-sonnet-4-6",
        "gpt-4o-realtime-preview",
        "models/gemini-2.5-flash-native-audio-latest",
    ):
        assert literal not in text, (
            f"{literal!r} is still a bare literal in config.py — it should come only "
            f"from settings_defaults.DEFAULTS"
        )
