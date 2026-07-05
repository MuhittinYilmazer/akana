"""Voice preferences persistence and env defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

from akana_server.voice_preferences import (
    load_voice_preferences,
    save_voice_preferences,
    update_voice_preferences,
    VoicePreferences,
)


def test_defaults_wake_autostart_off(tmp_path: Path) -> None:
    prefs = load_voice_preferences(tmp_path)
    assert prefs.wake_autostart is False
    assert prefs.stream_tts is False


def test_persist_wake_autostart(tmp_path: Path) -> None:
    save_voice_preferences(tmp_path, VoicePreferences(wake_autostart=False))
    prefs = load_voice_preferences(tmp_path)
    assert prefs.wake_autostart is False


def test_patch_wake_autostart(tmp_path: Path) -> None:
    update_voice_preferences(tmp_path, {"wake_autostart": False})
    assert load_voice_preferences(tmp_path).wake_autostart is False
    update_voice_preferences(tmp_path, {"wake_autostart": True})
    assert load_voice_preferences(tmp_path).wake_autostart is True


def test_env_akana_wake_autostart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AKANA_WAKE_AUTOSTART", "0")
    assert load_voice_preferences(tmp_path).wake_autostart is False
    monkeypatch.setenv("AKANA_WAKE_AUTOSTART", "1")
    assert load_voice_preferences(tmp_path).wake_autostart is True


def test_corrupt_preferences_file_recovers_to_defaults(tmp_path: Path) -> None:
    """A corrupt voice_preferences.json does not bring the server down — defaults are returned."""
    path = tmp_path / "voice_preferences.json"
    path.write_text("{bozuk json!!", encoding="utf-8")
    prefs = load_voice_preferences(tmp_path)
    assert prefs == VoicePreferences()
    # Patching on top of a corrupt file also works (defaults + patch are written).
    updated = update_voice_preferences(tmp_path, {"stream_tts": True})
    assert updated.stream_tts is True
    assert load_voice_preferences(tmp_path).stream_tts is True


def test_non_dict_json_recovers_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "voice_preferences.json"
    path.write_text('["liste", 42]', encoding="utf-8")
    assert load_voice_preferences(tmp_path) == VoicePreferences()


def test_invalid_tts_engine_value_keeps_previous(tmp_path: Path) -> None:
    update_voice_preferences(tmp_path, {"tts_engine": "piper"})
    prefs = update_voice_preferences(tmp_path, {"tts_engine": "espeak-yok"})
    assert prefs.tts_engine == "piper"
    # An empty/None voice name does not break the default.
    prefs2 = update_voice_preferences(tmp_path, {"tts_voice_tr": "   "})
    assert prefs2.tts_voice_tr == prefs.tts_voice_tr


def test_save_is_atomic_no_partial_main_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even if the write crashes the real file is not left half-written — tmp is written first.

    os.replace is patched and the 'rename' step is blown up: the tmp file must have
    the full JSON written, but the real voice_preferences.json must stay untouched
    (its previous fully-written state) → load does not fall back to defaults.
    """
    import os as _os

    # First leave a sound persisted state (an atomic full write).
    save_voice_preferences(tmp_path, VoicePreferences(tts_engine="piper", stream_tts=True))
    path = tmp_path / "voice_preferences.json"
    good = path.read_text(encoding="utf-8")

    # Now crash os.replace — the real file must stay unchanged in tmp.
    def boom(src, dst):  # noqa: ANN001
        raise OSError("disk doldu")

    monkeypatch.setattr(_os, "replace", boom)
    with pytest.raises(OSError):
        save_voice_preferences(tmp_path, VoicePreferences(tts_engine="edge"))

    # The real file was not corrupted: full JSON, old content preserved → does NOT fall back to default.
    assert path.read_text(encoding="utf-8") == good
    assert load_voice_preferences(tmp_path).tts_engine == "piper"


def test_concurrent_updates_no_crash(tmp_path: Path) -> None:
    """Two+ concurrent writers (two tabs/devices PATCH at once) must write without crashing.

    Before the fix: a shared ``.json.tmp`` + no lock → an ``os.replace`` race where one
    moves the tmp while the other hits ``FileNotFoundError`` (HTTP 500). The fix: a
    per-data_dir lock + a unique tmp name → serial, crash-free, leaves no tmp residue.
    """
    import threading

    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            for i in range(60):
                update_voice_preferences(tmp_path, {"tts_voice_tr": f"v{n}-{i}"})
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"error on concurrent writes (race): {errors[:3]}"
    # The file is readable + valid (no half-written JSON / missing file).
    prefs = load_voice_preferences(tmp_path)
    assert prefs.tts_voice_tr.startswith("v")
    # Unique-tmp cleanup: no tmp file should remain now.
    leftovers = list(tmp_path.glob("voice_preferences.json.*"))
    assert leftovers == [], f"tmp residue remained: {leftovers}"
