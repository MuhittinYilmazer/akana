"""RuntimeSettings — boundary-value tests (priority, validation, races, robustness).

EDGE cases beyond the existing ``test_runtime_settings.py``:
subtle corners of the runtime>env>default priority, rejection of invalid values
(type + bounds + csv/paths), concurrent atomic-write races, reset idempotency,
tolerance of a corrupt JSON file, restart-required-flagged setting (settings
overlay), empty value for a str-type setting.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from akana_server import runtime_settings as rs
from akana_server.runtime_settings import (
    RuntimeSettingError,
    get_runtime,
    get_store,
    reset_runtime_stores,
    validate_value,
)


@pytest.fixture(autouse=True)
def _isolated():
    reset_runtime_stores()
    yield
    reset_runtime_stores()


def _settings(tmp_path: Path, **attrs):
    return SimpleNamespace(data_dir=tmp_path, **attrs)


# -- subtle corners of the priority chain ----------------------------------------------------


def test_runtime_sifir_degeri_envi_ezer(tmp_path, monkeypatch):
    # 0 is falsy but a valid runtime value — it must override env (not None).
    monkeypatch.setenv("AKANA_SESSION_CLOSER_INTERVAL", "40")
    settings = _settings(tmp_path, session_closer_interval=40.0)
    get_store(tmp_path).set("session_closer_interval", 0)
    assert get_runtime("session_closer_interval", settings) == 0
    assert rs.resolve_source("session_closer_interval", settings) == "runtime"


def test_str_bos_runtime_degeri_korunur(tmp_path):
    # str type: an empty string is a valid runtime value (does not fall back to None).
    settings = _settings(tmp_path, whisper_prompt="env-sözlük")
    get_store(tmp_path).set("whisper_prompt", "")
    assert get_runtime("whisper_prompt", settings) == ""
    assert rs.resolve_source("whisper_prompt", settings) == "runtime"


def test_env_baked_enum_disi_ses_defaulta_duser(tmp_path):
    # CTX-5: an env-baked voice value (settings_attr) out of spec.options must be
    # rejected to the schema default at resolve time, exactly like PUT — otherwise
    # a misspelled AKANA_GEMINI_LIVE_VOICE reaches the provider unvalidated.
    bad = _settings(tmp_path, gemini_live_voice="Charonn", openai_realtime_voice="allowy")
    assert get_runtime("gemini_live_voice", bad) == rs.SCHEMA["gemini_live_voice"].default
    assert get_runtime("openai_realtime_voice", bad) == rs.SCHEMA["openai_realtime_voice"].default
    # A valid choice passes through unchanged.
    ok = _settings(tmp_path, gemini_live_voice="Kore")
    assert get_runtime("gemini_live_voice", ok) == "Kore"


def test_env_only_enum_disi_dil_defaulta_duser(tmp_path, monkeypatch):
    # CTX-5 (no settings_attr path): the env-only enum falls back through
    # _env_fallback too — an out-of-enum AKANA_LANGUAGE resolves to the default.
    monkeypatch.setenv("AKANA_LANGUAGE", "fr")
    assert get_runtime("language", _settings(tmp_path)) == "en"


def test_env_only_anahtar_runtime_yoksa_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AKANA_CONTEXT_MAX_CHARS", "5000")
    settings = _settings(tmp_path)
    assert get_runtime("context_max_chars", settings) == 5000


def test_data_dirsiz_env_only_default(monkeypatch):
    monkeypatch.delenv("AKANA_CONTEXT_MAX_CHARS", raising=False)
    s = SimpleNamespace()  # no data_dir
    assert get_runtime("context_max_chars", s) == 120_000


# -- rejection of invalid values -------------------------------------------------------------


@pytest.mark.parametrize(
    "key,value,parca",
    [
        ("session_closer_interval", 200_000, "at most"),
        ("session_closer_interval", -0.01, "at least"),
        ("session_closer_idle_minutes", 0, "at least"),
        ("session_closer_idle_minutes", 99999, "at most"),
        ("skill_inject_max", 11, "at most"),
        ("upload_max_mb", 0.05, "at least"),
        ("upload_max_mb", 501, "at most"),
        ("network_max_retries", 11, "at most"),
    ],
)
def test_sinir_disi_reddedilir(key, value, parca):
    with pytest.raises(RuntimeSettingError, match=parca):
        validate_value(rs.SCHEMA[key], value)


def test_tip_reddi_turkce():
    with pytest.raises(RuntimeSettingError, match="boolean"):
        validate_value(rs.SCHEMA["uploads_enabled"], "belki-açık")
    with pytest.raises(RuntimeSettingError, match="valid number"):
        validate_value(rs.SCHEMA["session_closer_idle_minutes"], "üç")
    with pytest.raises(RuntimeSettingError, match="text"):
        validate_value(rs.SCHEMA["whisper_prompt"], 123)


def test_bool_bool_olmayan_int_reddedilir():
    # passing a bool to an int field is rejected early (no True == 1 trap).
    with pytest.raises(RuntimeSettingError, match="number"):
        validate_value(rs.SCHEMA["session_closer_idle_minutes"], True)


def test_bool_string_varyantlari_kabul():
    assert validate_value(rs.SCHEMA["uploads_enabled"], "ON") is True
    assert validate_value(rs.SCHEMA["uploads_enabled"], "off") is False
    assert validate_value(rs.SCHEMA["uploads_enabled"], "1") is True
    assert validate_value(rs.SCHEMA["uploads_enabled"], "no") is False


def test_int_ondalik_string_reddedilir():
    with pytest.raises(RuntimeSettingError, match="valid number"):
        validate_value(rs.SCHEMA["context_max_chars"], "3.7")


def test_csv_100_ogeden_fazla_reddedilir():
    with pytest.raises(RuntimeSettingError, match="100 items"):
        validate_value(rs.SCHEMA["telegram_allowed_chat_ids"], ",".join(str(i) for i in range(101)))


def test_paths_goreli_reddedilir():
    with pytest.raises(RuntimeSettingError, match="absolute path"):
        validate_value(rs.SCHEMA["file_roots"], "proje/alt")


def test_paths_bos_segmentler_dusurulur():
    # Empty/whitespace segments are dropped. Split happens on ``os.pathsep`` (":" on
    # POSIX, ";" on Windows) → build the separators from the platform value, not ":".
    raw = f"/a{os.pathsep}{os.pathsep} {os.pathsep}/b"
    assert validate_value(rs.SCHEMA["file_roots"], raw) == ["/a", "/b"]


# -- atomic-write race --------------------------------------------------------------


def test_eszamanli_set_atomik_dosya_bozulmaz(tmp_path):
    store = get_store(tmp_path)
    errors: list[Exception] = []

    def yaz(i: int):
        try:
            store.set("context_max_chars", (i % 50) + 1)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=yaz, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    # The file is always valid JSON (no half-written state visible) + no leftover tmp.
    data = json.loads((tmp_path / "runtime_settings.json").read_text(encoding="utf-8"))
    assert "context_max_chars" in data
    assert not list(tmp_path.glob("*.tmp"))


def test_reset_idempotan(tmp_path):
    store = get_store(tmp_path)
    store.set("uploads_enabled", False)
    assert store.reset("uploads_enabled") is True
    assert store.reset("uploads_enabled") is False  # second call is a no-op
    assert store.reset("hic_yazilmamis") is False


# -- corrupt JSON file tolerance -----------------------------------------------------


def test_bozuk_json_dosyasi_default_dondurur(tmp_path, monkeypatch):
    monkeypatch.delenv("AKANA_CONTEXT_MAX_CHARS", raising=False)
    (tmp_path / "runtime_settings.json").write_text("{ bozuk ::", encoding="utf-8")
    settings = _settings(tmp_path)
    # Reading does not blow up; falls back to env/default (context_max_chars is env-only, default 120k).
    assert get_runtime("context_max_chars", settings) == 120_000
    assert get_store(tmp_path).load() == {}


def test_json_liste_kok_dict_degil_yok_sayilir(tmp_path):
    (tmp_path / "runtime_settings.json").write_text("[1, 2, 3]", encoding="utf-8")
    assert get_store(tmp_path).load() == {}


def test_store_sinirdisi_deger_envle_devam(tmp_path):
    # Hand-written out-of-bounds value → _coerce_runtime None → env/default.
    (tmp_path / "runtime_settings.json").write_text(
        json.dumps({"session_closer_idle_minutes": 99999}), encoding="utf-8"
    )
    settings = _settings(tmp_path, session_closer_idle_minutes=30)
    assert get_runtime("session_closer_idle_minutes", settings) == 30


# -- restart-required-flagged setting (settings overlay) ---------------------------------


def test_restart_gerekli_overlay_settingse_isler(tmp_path, monkeypatch):
    from akana_server.config import load_settings
    from akana_server.runtime_settings import apply_runtime_overrides

    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    store = get_store(tmp_path)
    store.set("telegram_enabled", True)
    store.set("telegram_allowed_chat_ids", ["100", "200"])
    settings = apply_runtime_overrides(load_settings())
    assert settings.telegram_enabled is True
    assert settings.telegram_allowed_chat_ids == ("100", "200")


def test_restart_bayragi_semada_dogru():
    assert rs.SCHEMA["telegram_enabled"].restart_required is True
    assert rs.SCHEMA["telegram_allowed_chat_ids"].restart_required is True
    assert rs.SCHEMA["session_closer_interval"].restart_required is False
    assert rs.SCHEMA["file_roots"].restart_required is False
