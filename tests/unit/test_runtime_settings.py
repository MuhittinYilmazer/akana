"""RuntimeSettings layer — precedence chain, validation, real consumers.

Scope:

* Precedence: runtime_settings.json > env > default (with source reporting).
* Validation bounds reject with a localized error; atomic write + reset.
* At least 3 real consumers changing WITHOUT a RESTART:
  - search rate limit / provider order → when the service is rebuilt,
  - context budget / planner step limit → via the bound store,
  - file roots / upload limit / skill threshold → at call time.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from akana_server import runtime_settings as rs
from akana_server.runtime_settings import (
    RuntimeSettingError,
    apply_runtime_overrides,
    bind_runtime_data_dir,
    get_runtime,
    get_store,
    reset_runtime_stores,
    runtime_override,
    runtime_payload,
    validate_value,
)


@pytest.fixture(autouse=True)
def _isolated_stores():
    reset_runtime_stores()
    yield
    reset_runtime_stores()


def _settings(tmp_path: Path, **attrs):
    return SimpleNamespace(data_dir=tmp_path, **attrs)


# ── precedence chain ────────────────────────────────────────────────────────


def test_oncelik_default_env_runtime(tmp_path, monkeypatch):
    settings = _settings(tmp_path, session_closer_interval=25.0)
    # default (no env, no runtime) — the Settings attr carries env-or-default.
    monkeypatch.delenv("AKANA_SESSION_CLOSER_INTERVAL", raising=False)
    assert get_runtime("session_closer_interval", settings) == 25.0
    assert rs.resolve_source("session_closer_interval", settings) == "default"
    # env layer: load_settings bakes env into the attr; source reported as "env".
    monkeypatch.setenv("AKANA_SESSION_CLOSER_INTERVAL", "40")
    settings_env = _settings(tmp_path, session_closer_interval=40.0)
    assert get_runtime("session_closer_interval", settings_env) == 40.0
    assert rs.resolve_source("session_closer_interval", settings_env) == "env"
    # the runtime layer overrides env.
    get_store(tmp_path).set("session_closer_interval", 10.0)
    assert get_runtime("session_closer_interval", settings_env) == 10.0
    assert rs.resolve_source("session_closer_interval", settings_env) == "runtime"
    # reset → falls back to env.
    assert get_store(tmp_path).reset("session_closer_interval") is True
    assert get_runtime("session_closer_interval", settings_env) == 40.0


def test_resolve_source_bozuk_runtime_get_runtime_ile_ayni_katman(
    tmp_path, monkeypatch
):
    """#17: a CORRUPT runtime value in the store → get_runtime won't use it (falls
    back to env/default); resolve_source must NOT say 'runtime' either (otherwise the UI shows the wrong layer)."""
    monkeypatch.delenv("AKANA_SESSION_CLOSER_INTERVAL", raising=False)
    settings = _settings(tmp_path, session_closer_interval=25.0)
    # Write the corrupt numeric value straight to the store file (bypassing set()):
    (tmp_path / "runtime_settings.json").write_text(
        json.dumps({"session_closer_interval": "abc-bozuk"}), encoding="utf-8"
    )
    reset_runtime_stores()  # clear cache → load fresh from the file

    # get_runtime won't use the corrupt value → no env → default (Settings attr 25.0).
    assert get_runtime("session_closer_interval", settings) == 25.0
    # resolve_source must report the SAME layer: NOT 'runtime' → 'default'.
    assert rs.resolve_source("session_closer_interval", settings) == "default"

    # if env exists: get_runtime uses env → resolve_source 'env' (still not 'runtime').
    monkeypatch.setenv("AKANA_SESSION_CLOSER_INTERVAL", "40")
    settings_env = _settings(tmp_path, session_closer_interval=40.0)
    assert get_runtime("session_closer_interval", settings_env) == 40.0
    assert rs.resolve_source("session_closer_interval", settings_env) == "env"


def test_oncelik_env_only_anahtar(tmp_path, monkeypatch):
    """Keys without a settings_attr (context_max_chars) parse env at runtime."""
    settings = _settings(tmp_path)
    monkeypatch.delenv("AKANA_CONTEXT_MAX_CHARS", raising=False)
    assert get_runtime("context_max_chars", settings) == 120_000
    monkeypatch.setenv("AKANA_CONTEXT_MAX_CHARS", "5000")
    assert get_runtime("context_max_chars", settings) == 5000
    get_store(tmp_path).set("context_max_chars", 7000)
    assert get_runtime("context_max_chars", settings) == 7000


def test_data_dirsiz_settings_asla_patlamaz(monkeypatch):
    monkeypatch.delenv("AKANA_CONTEXT_MAX_CHARS", raising=False)
    s = SimpleNamespace(session_closer_interval=7.0)
    assert get_runtime("session_closer_interval", s) == 7.0
    assert get_runtime("context_max_chars", s) == 120_000


def test_bozuk_runtime_degeri_envle_devam_eder(tmp_path):
    """A manually written corrupt value (out of bounds) falls back to env/default."""
    (tmp_path / "runtime_settings.json").write_text(
        json.dumps({"session_closer_idle_minutes": 99999}), encoding="utf-8"
    )
    settings = _settings(tmp_path, session_closer_idle_minutes=30)
    assert get_runtime("session_closer_idle_minutes", settings) == 30


# ── validation bounds (localized error) ─────────────────────────────────────


@pytest.mark.parametrize(
    "key,value",
    [
        ("session_closer_interval", -1),
        ("session_closer_idle_minutes", 99999),
        ("skill_inject_threshold", 1.5),
        ("skill_inject_max", 0),
        ("upload_max_mb", 600),
        ("upload_max_mb", 0),
        ("session_closer_idle_minutes", 0),
    ],
)
def test_sinir_disi_sayilar_reddedilir(key, value):
    with pytest.raises(RuntimeSettingError) as e:
        validate_value(rs.SCHEMA[key], value)
    msg = str(e.value)
    assert "at least" in msg or "at most" in msg


def test_tip_hatalari_turkce(tmp_path):
    with pytest.raises(RuntimeSettingError, match="boolean"):
        validate_value(rs.SCHEMA["uploads_enabled"], "belki")
    with pytest.raises(RuntimeSettingError, match="valid number"):
        validate_value(rs.SCHEMA["context_max_chars"], "çok")
    with pytest.raises(RuntimeSettingError, match="absolute path"):
        validate_value(rs.SCHEMA["file_roots"], "göreli/yol")


def test_csv_ve_paths_metin_ya_da_liste_kabul_eder():
    assert validate_value(rs.SCHEMA["telegram_allowed_chat_ids"], ["1", " 2 "]) == [
        "1",
        "2",
    ]
    # The validator splits a string on ``os.pathsep`` (":" on POSIX, ";" on Windows),
    # so build the CSV with the platform separator instead of a hardcoded ":".
    assert validate_value(rs.SCHEMA["file_roots"], f"/a{os.pathsep}/b") == ["/a", "/b"]
    assert validate_value(rs.SCHEMA["file_roots"], "~/proj") == ["~/proj"]


def test_atomik_yazim_ve_reset(tmp_path):
    store = get_store(tmp_path)
    store.set("session_closer_enabled", False)
    store.set("context_max_chars", 4)
    data = json.loads((tmp_path / "runtime_settings.json").read_text(encoding="utf-8"))
    assert data == {"session_closer_enabled": False, "context_max_chars": 4}
    assert not list(tmp_path.glob("*.tmp"))  # no leftover tmp file
    assert store.reset("session_closer_enabled") is True
    assert store.reset("session_closer_enabled") is False  # second reset is a no-op
    data = json.loads((tmp_path / "runtime_settings.json").read_text(encoding="utf-8"))
    assert data == {"context_max_chars": 4}


# ── payload (schema single source) ──────────────────────────────────────────


def test_runtime_payload_sema_deger_kaynak(tmp_path, monkeypatch):
    monkeypatch.delenv("AKANA_CONTEXT_MAX_CHARS", raising=False)
    settings = _settings(
        tmp_path,
        session_closer_interval=25.0,
        file_roots=(Path("/tmp/x"),),
    )
    get_store(tmp_path).set("context_max_chars", 6)
    payload = runtime_payload(settings)
    by_key = {item["key"]: item for item in payload["settings"]}
    # The form carries only VISIBLE specs; hidden ones (wake_threshold → has its
    # own slider in the voice panel) are not listed.
    visible = [s for s in rs.SCHEMA.values() if not s.hidden]
    assert len(by_key) == len(visible)
    assert "wake_threshold" not in by_key
    # With include_hidden=True (the debug /settings/effective path) all are visible.
    full = runtime_payload(settings, include_hidden=True)
    assert "wake_threshold" in {item["key"] for item in full["settings"]}
    assert len({item["key"] for item in full["settings"]}) == len(rs.SCHEMA)
    assert {c["id"] for c in payload["categories"]} >= {"zamanlama", "telegram"}
    ctx = by_key["context_max_chars"]
    assert ctx["value"] == 6 and ctx["source"] == "runtime"
    assert ctx["description"]  # a description is required
    # tuple/Path fields collapse to a JSON-clean list. ``_payload_value`` converts a
    # Path with ``str(v)`` → ``\tmp\x`` (backslash) on Windows; compare with the same conversion.
    assert by_key["file_roots"]["value"] == [str(Path("/tmp/x"))]
    # Telegram is now managed by the live panel of the Channels tab →
    # hidden from the form; but visible with include_hidden and carries an honest restart flag.
    assert "telegram_enabled" not in by_key
    full_by_key = {item["key"]: item for item in full["settings"]}
    assert full_by_key["telegram_enabled"]["restart_required"] is True
    assert full_by_key["telegram_allowed_chat_ids"]["restart_required"] is True
    # Those applied without a restart carry no flag.
    assert by_key["session_closer_interval"]["restart_required"] is False


def test_apply_runtime_overrides_settingse_isler(tmp_path, monkeypatch):
    """Restart path: keys like telegram are applied to Settings at startup."""
    from akana_server.config import load_settings

    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    store = get_store(tmp_path)
    store.set("telegram_enabled", True)
    store.set("telegram_allowed_chat_ids", ["123", "456"])
    store.set("session_closer_interval", 9.5)
    settings = apply_runtime_overrides(load_settings())
    assert settings.telegram_enabled is True
    assert settings.telegram_allowed_chat_ids == ("123", "456")
    assert settings.session_closer_interval == 9.5


# ── real consumers: change without a restart ────────────────────────────────


def test_baglam_butcesi_runtime_override(tmp_path, monkeypatch):
    from akana_server.context.assembler import context_budget_chars

    monkeypatch.delenv("AKANA_CONTEXT_MAX_CHARS", raising=False)
    bind_runtime_data_dir(tmp_path)
    store = get_store(tmp_path)
    store.set("context_max_chars", 1234)
    assert context_budget_chars() == 1234
    # When unbound the env chain continues (behavior-neutral fallback).
    bind_runtime_data_dir(None)
    assert runtime_override("context_max_chars") is None
    assert context_budget_chars() == 120_000


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="runtime file_roots validator accepts only POSIX-style absolute paths "
    "('/' or '~'); a Windows tmp_path ('C:\\...') is rejected by design",
)
def test_dosya_kokleri_ve_upload_siniri_runtime(tmp_path):
    from akana_server.files.service import FileService
    from akana_server.multimodal.store import ImageStore

    root = tmp_path / "izinli"
    root.mkdir()
    settings = _settings(tmp_path, file_roots=(), upload_max_mb=10.0)
    assert FileService.from_settings(settings).configured is False
    get_store(tmp_path).set("file_roots", [str(root)])
    svc = FileService.from_settings(settings)
    assert svc.configured is True and root.resolve() in svc.roots

    get_store(tmp_path).set("upload_max_mb", 1)
    assert ImageStore.for_settings(settings).max_bytes == 1024 * 1024


def test_skill_esigi_runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("AKANA_SKILL_INJECT_THRESHOLD", raising=False)
    settings = _settings(tmp_path)
    assert get_runtime("skill_inject_threshold", settings) == 0.03
    get_store(tmp_path).set("skill_inject_threshold", 0.5)
    assert get_runtime("skill_inject_threshold", settings) == 0.5


def test_skill_inject_env_semantigi_korunur(tmp_path, monkeypatch):
    """Historical AKANA_SKILL_INJECT parsing: only 0/false/off disables it."""
    settings = _settings(tmp_path)
    monkeypatch.setenv("AKANA_SKILL_INJECT", "off")
    assert get_runtime("skill_inject_enabled", settings) is False
    monkeypatch.setenv("AKANA_SKILL_INJECT", "evet")
    assert get_runtime("skill_inject_enabled", settings) is True


# ── tools module toggles (configurable from the UI) ─────────────────────────


def test_arac_specleri_semada():
    """2 module toggles in SCHEMA, in the 'araclar' category, bool."""
    for key in (
        "memory_tools_enabled",
        "vault_tools_enabled",
    ):
        spec = rs.SCHEMA.get(key)
        assert spec is not None, key
        assert spec.type == "bool"
        assert spec.category == "araclar"
        assert spec.default is True
    assert any(c["id"] == "araclar" for c in rs.CATEGORIES)


def test_memory_tools_runtime_override(tmp_path, monkeypatch):
    from akana_server.orchestrator.memory_tools import memory_tools_enabled

    bind_runtime_data_dir(tmp_path)
    monkeypatch.delenv("AKANA_MEMORY_TOOLS", raising=False)
    get_store(tmp_path).set("memory_tools_enabled", False)
    assert memory_tools_enabled() is False
    get_store(tmp_path).reset("memory_tools_enabled")
    # with no bound store it falls back to env (historical behavior preserved)
    bind_runtime_data_dir(None)
    monkeypatch.setenv("AKANA_MEMORY_TOOLS", "0")
    assert memory_tools_enabled() is False


# ── voice & wake: wake_threshold single source of truth (consolidation lock) ──


def test_wake_threshold_tek_kaynak_runtime_store(tmp_path, monkeypatch):
    """CONSOLIDATION LOCK: wake_threshold lives only in runtime_settings.

    Formerly the value was both in ``llm_settings.json`` and in env; but the live
    consumer (voice/wake.py + /voice/* status) read ONLY ``settings.wake_threshold``,
    so the llm_settings copy was silently dead. Now there is a single chain:
    runtime store > env (WAKE_THRESHOLD) > default; at startup
    ``apply_runtime_overrides`` reflects the value into ``settings.wake_threshold`` →
    the live consumer sees the threshold changed from the UI without a restart.
    """
    from akana_server.config import load_settings
    from akana_server.llm_settings import LlmSettings

    # 1) Schema row: voice category, correct env/attr, wide clamp bounds
    #    (score full range 0.01–1.0; UI setting range = these bounds).
    spec = rs.SCHEMA.get("wake_threshold")
    assert spec is not None
    assert spec.type == "float"
    assert spec.category == "ses"
    assert spec.env_var == "WAKE_THRESHOLD"
    assert spec.settings_attr == "wake_threshold"
    assert (spec.min, spec.max, spec.default) == (0.01, 1.0, 0.5)
    assert any(c["id"] == "ses" for c in rs.CATEGORIES)

    # 2) Precedence chain: default → env → runtime store.
    monkeypatch.delenv("WAKE_THRESHOLD", raising=False)
    settings = _settings(tmp_path, wake_threshold=0.5)
    assert get_runtime("wake_threshold", settings) == 0.5
    assert rs.resolve_source("wake_threshold", settings) == "default"
    settings_env = _settings(tmp_path, wake_threshold=0.40)  # value baked into env
    monkeypatch.setenv("WAKE_THRESHOLD", "0.40")
    assert get_runtime("wake_threshold", settings_env) == 0.40
    assert rs.resolve_source("wake_threshold", settings_env) == "env"
    get_store(tmp_path).set("wake_threshold", 0.22)
    assert get_runtime("wake_threshold", settings_env) == 0.22
    assert rs.resolve_source("wake_threshold", settings_env) == "runtime"

    # 3) An out-of-bounds value is rejected with a localized error (UI validation).
    with pytest.raises(RuntimeSettingError, match="at most"):
        validate_value(spec, 5.0)
    with pytest.raises(RuntimeSettingError, match="at least"):
        validate_value(spec, 0.0001)

    # 4) Startup reflection: the runtime store value is applied into
    #    settings.wake_threshold that the live consumer reads (restart-free effect path).
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    reflected = apply_runtime_overrides(load_settings())
    assert reflected.wake_threshold == 0.22

    # 5) No dead copy: wake_threshold is NO LONGER a field of LlmSettings.
    assert not hasattr(LlmSettings(), "wake_threshold")
    assert "wake_threshold" not in LlmSettings().to_dict()


# ── regression: settings-tab bug audit fixes (2026-07-03) ────────────────────


def test_nan_ve_inf_reddedilir():
    """H1: NaN slips past min/max (every comparison with NaN is False) — validate_value
    must reject NaN and ±Inf so they are never persisted to runtime_settings.json."""
    for key in ("wake_threshold", "skill_inject_threshold"):
        spec = rs.SCHEMA[key]
        for bad in (float("nan"), float("inf"), float("-inf")):
            with pytest.raises(RuntimeSettingError):
                validate_value(spec, bad)


def test_env_fallback_bool_bilinmeyen_token_defaulta_duser(monkeypatch):
    """M1: a default-OFF bool without settings_attr must NOT flip ON from a non-falsy
    typo — unknown tokens fall back to the schema default, matching validate_value."""
    spec = rs.SCHEMA["agent_autocontinue"]
    assert spec.settings_attr is None and spec.type == "bool" and spec.default is False
    for word in ("disabled", "enabled", "maybe"):  # non-falsy words → must stay OFF
        monkeypatch.setenv(spec.env_var, word)
        assert rs._env_fallback(spec) == (True, False)
    for on in ("1", "true", "YES", "On"):
        monkeypatch.setenv(spec.env_var, on)
        assert rs._env_fallback(spec) == (True, True)
    for off in ("0", "false", "NO", "Off"):
        monkeypatch.setenv(spec.env_var, off)
        assert rs._env_fallback(spec) == (True, False)


def test_env_fallback_sayisal_sinir_disi_defaulta_duser(monkeypatch):
    """M2: an env-only numeric setting must enforce the SAME schema bounds the PUT
    path does — an out-of-range value falls back to the default, not through raw."""
    spec = rs.SCHEMA["agent_max_continue_iters"]
    assert spec.settings_attr is None and (spec.min, spec.max) == (1, 100)
    monkeypatch.setenv(spec.env_var, "100000")  # > max → default
    assert rs._env_fallback(spec) == (True, spec.default)
    monkeypatch.setenv(spec.env_var, "0")  # < min → default
    assert rs._env_fallback(spec) == (True, spec.default)
    monkeypatch.setenv(spec.env_var, "50")  # in range → used verbatim
    assert rs._env_fallback(spec) == (True, 50)


def test_apply_bozuk_str_degeri_none_yazmaz(tmp_path, monkeypatch):
    """M11: a corrupt stored value for a str-typed settings_attr spec must NOT be
    stamped as None onto Settings — the field falls back to env/default instead."""
    from akana_server.config import load_settings

    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WHISPER_PROMPT", "keep-me")
    (tmp_path / "runtime_settings.json").write_text(
        json.dumps({"whisper_prompt": 42}), encoding="utf-8"  # corrupt: not a str
    )
    reset_runtime_stores()
    reflected = apply_runtime_overrides(load_settings())
    assert reflected.whisper_prompt == "keep-me"  # env preserved, NOT None


def test_set_many_atomik_ve_birlestirir(tmp_path):
    """M3: set_many writes multiple keys in ONE atomic replace, merges with existing
    keys, and is a no-op for an empty dict."""
    store = get_store(tmp_path)
    store.set_many({"session_closer_enabled": False, "context_max_chars": 4})
    data = json.loads((tmp_path / "runtime_settings.json").read_text(encoding="utf-8"))
    assert data == {"session_closer_enabled": False, "context_max_chars": 4}
    assert not list(tmp_path.glob("*.tmp"))  # no leftover tmp file
    store.set_many({"context_max_chars": 9})  # merge, must not clobber the other key
    data = json.loads((tmp_path / "runtime_settings.json").read_text(encoding="utf-8"))
    assert data == {"session_closer_enabled": False, "context_max_chars": 9}
    store.set_many({})  # empty → no-op, must not raise or rewrite
    data = json.loads((tmp_path / "runtime_settings.json").read_text(encoding="utf-8"))
    assert data == {"session_closer_enabled": False, "context_max_chars": 9}
