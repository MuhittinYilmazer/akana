"""Blitz-3 be-infra regressions: doctor/start_cmd resolve the ACTIVE provider and its
key the SAME way the server does (persisted store + secret store), not from .env alone."""

from __future__ import annotations

from pathlib import Path

import pytest

from akana_cli import doctor, io, start_cmd


def _capture_io(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    """Redirect io.ok/warn/fail/step/banner into lists so a run_doctor() call is silent
    and inspectable."""
    sink: dict[str, list[str]] = {"ok": [], "warn": [], "fail": []}
    monkeypatch.setattr(io, "ok", lambda msg, *_a, **_k: sink["ok"].append(str(msg)))
    monkeypatch.setattr(io, "warn", lambda msg, *_a, **_k: sink["warn"].append(str(msg)))
    monkeypatch.setattr(io, "fail", lambda msg, *_a, **_k: sink["fail"].append(str(msg)))
    monkeypatch.setattr(io, "banner", lambda *_a, **_k: None)
    monkeypatch.setattr(io, "step", lambda *_a, **_k: None)
    return sink


def _neutralize_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("LLM_PROVIDER", "CURSOR_API_KEY", "AKANA_HOST", "AKANA_PORT"):
        monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv("AKANA_" + var, raising=False)


# ── be-infra-1: doctor resolves the provider store-first (llm_settings.json), not .env ──
def test_doctor_provider_comes_from_store_not_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Settings UI records provider switches only in llm_settings.json (never .env).
    After a switch away from cursor, doctor must NOT hard-fail on the cursor Node/bridge
    requirements for a provider that is no longer active."""
    _neutralize_env(monkeypatch)
    (tmp_path / "llm_settings.json").write_text('{"provider": "ollama"}', encoding="utf-8")
    (tmp_path / ".env").write_text("LLM_PROVIDER=cursor\n", encoding="utf-8")

    monkeypatch.setattr(doctor, "default_data_dir", lambda: tmp_path)
    # .env still says cursor — the stale value doctor used to trust.
    monkeypatch.setattr(
        doctor, "read_env_key", lambda k: "cursor" if k == "LLM_PROVIDER" else None
    )
    monkeypatch.setattr(doctor, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(doctor, "venv_exists", lambda: False)  # skip venv module probes
    monkeypatch.setattr(doctor, "find_system_python", lambda: "py")
    # Node/npm absent — would trip the cursor hard-failures if cursor were treated active.
    monkeypatch.setattr(doctor.shutil, "which", lambda _n: None)
    sink = _capture_io(monkeypatch)

    doctor.run_doctor(verbose=True, probe_network=False)

    node_missing = doctor.i18n.t("doctor.node_missing_cursor")
    bridge_missing = doctor.i18n.t("doctor.bridge_missing")
    assert node_missing not in sink["fail"], "cursor Node failure fired for inactive provider"
    assert bridge_missing not in sink["fail"], "cursor bridge failure fired for inactive provider"
    # And the active provider (ollama) is the one exercised.
    assert doctor.i18n.t("doctor.ollama") in sink["ok"]


def test_doctor_resolved_provider_store_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct unit: store value wins; a corrupt store value falls back to .env/env."""
    _neutralize_env(monkeypatch)
    monkeypatch.setattr(doctor, "default_data_dir", lambda: tmp_path)
    monkeypatch.setattr(
        doctor, "read_env_key", lambda k: "cursor" if k == "LLM_PROVIDER" else None
    )
    (tmp_path / "llm_settings.json").write_text('{"provider": "ollama"}', encoding="utf-8")
    assert doctor._resolved_provider() == "ollama"
    # Corrupt store provider → fall through to the .env value.
    (tmp_path / "llm_settings.json").write_text('{"provider": "bogus"}', encoding="utf-8")
    assert doctor._resolved_provider() == "cursor"


# ── be-infra-2: doctor accepts a key configured via the UI (secret store), not just .env ─
def test_doctor_key_from_secret_store_not_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The documented happy path stores the key in secrets.json (lowercase name), not .env.
    doctor must read the store first, so a UI-configured key does NOT report as empty."""
    _neutralize_env(monkeypatch)
    (tmp_path / ".env").write_text("LLM_PROVIDER=cursor\n", encoding="utf-8")
    monkeypatch.setattr(doctor, "default_data_dir", lambda: tmp_path)
    monkeypatch.setattr(
        doctor, "read_env_key", lambda k: "cursor" if k == "LLM_PROVIDER" else None
    )
    monkeypatch.setattr(doctor, "ENV_FILE", tmp_path / ".env")
    # venv present so no venv_missing failure; a bogus interpreter makes the optional
    # module probes fail harmlessly (subprocess OSError is swallowed, no issue tallied).
    monkeypatch.setattr(doctor, "venv_exists", lambda: True)
    monkeypatch.setattr(doctor, "venv_python", lambda: tmp_path / "nope-python")
    monkeypatch.setattr(doctor, "find_system_python", lambda: "py")
    # Node/bridge present so the ONLY possible failure is the key check under test.
    monkeypatch.setattr(doctor.shutil, "which", lambda _n: "/usr/bin/" + _n)
    (tmp_path / "node_modules" / "@cursor" / "sdk").mkdir(parents=True)
    monkeypatch.setattr(doctor, "BRIDGE_DIR", tmp_path)

    # The key lives in the store under the LOWERCASE ALLOWED_KEYS name only.
    def _fake_get_secret(_dd, key):
        return "real-cursor-key-abcdefgh" if key == "cursor_api_key" else None

    monkeypatch.setattr("akana_server.secret_store.get_secret", _fake_get_secret)
    sink = _capture_io(monkeypatch)

    rc = doctor.run_doctor(verbose=True, probe_network=False)

    key_empty = doctor.i18n.t("doctor.key_empty", key="CURSOR_API_KEY", provider="cursor")
    assert key_empty not in sink["fail"], "UI-configured key wrongly reported empty"
    assert doctor.i18n.t("doctor.key_defined", key="CURSOR_API_KEY") in sink["ok"]
    assert rc == 0, "a healthy UI-configured install must not exit non-zero"


def test_doctor_stored_key_helper_lowercases_and_gates_placeholder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """_stored_key normalises the uppercase env name to the lowercase store name and
    rejects a shipped placeholder even if present verbatim in .env."""
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.setattr(doctor, "default_data_dir", lambda: tmp_path)
    monkeypatch.setattr(doctor, "read_env_key", lambda _k: None)
    monkeypatch.setattr(
        "akana_server.secret_store.get_secret",
        lambda _dd, key: "real-cursor-key-abcdefgh" if key == "cursor_api_key" else None,
    )
    assert doctor._stored_key("CURSOR_API_KEY") == "real-cursor-key-abcdefgh"

    # No store entry + a placeholder in .env → treated as unset.
    monkeypatch.setattr("akana_server.secret_store.get_secret", lambda _dd, _k: None)
    monkeypatch.setattr(
        doctor, "read_env_key", lambda _k: "your-cursor-api-key-here"
    )
    assert doctor._stored_key("CURSOR_API_KEY") is None


# ── be-infra-3: start_cmd._key_present queries the store with the lowercase name ──────
def test_start_key_present_uses_lowercase_store_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """_key_present receives the UPPERCASE env-var name but the store only holds lowercase
    keys; it must lowercase before the lookup or the store-first check is dead."""
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    seen: list[str] = []

    def _fake_get_secret(_dd, key):
        seen.append(key)
        return "real-cursor-key-abcdefgh" if key == "cursor_api_key" else None

    monkeypatch.setattr("akana_server.secret_store.get_secret", _fake_get_secret)
    assert start_cmd._key_present("CURSOR_API_KEY") is True
    assert "cursor_api_key" in seen, "store must be queried with the lowercase name"
