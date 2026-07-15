"""codex_catalog — static Codex model list + CLI/auth health (`codex login status`).

Hermetic: no real ``codex`` binary — PATH resolution and the ``codex login status``
subprocess are faked so the probe classes (not_installed / not_logged_in / logged-in)
are exercised without a live login.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from akana_server.config import load_settings
from akana_server.orchestrator import codex_catalog


def _settings(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CODEX_MODEL", raising=False)
    return load_settings()


class _FakeLoginProc:
    def __init__(self, returncode: int, out: bytes = b"", err: bytes = b"") -> None:
        self.returncode = returncode
        self._out = out
        self._err = err

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._out, self._err

    def kill(self) -> None:  # pragma: no cover - only on timeout path
        pass


def _patch_login(monkeypatch: pytest.MonkeyPatch, proc: _FakeLoginProc | None) -> None:
    async def _fake_spawn(*_cmd: str, **_kwargs: Any):
        if proc is None:
            raise FileNotFoundError(2, "no such file", "codex")
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_spawn)


# --------------------------------------------------------------------------- #
# Static model catalog
# --------------------------------------------------------------------------- #
def test_model_options_are_codex_family() -> None:
    from akana_server.llm_settings import codex_model_options

    opts = codex_model_options()
    assert opts, "codex catalog must not be empty"
    assert all(set(o) == {"value", "label"} for o in opts)
    assert all("codex" in o["value"] for o in opts)
    assert opts[0]["value"] == "gpt-5-codex"  # default = first option


def test_resolve_default_and_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    from akana_server.llm_settings import (
        LlmSettings,
        resolve_codex_model_tag,
    )

    settings = _settings(monkeypatch, tmp_path)
    # Default (no persisted setting, no env).
    assert resolve_codex_model_tag(settings, LlmSettings()) == "gpt-5-codex"
    # Persisted setting wins.
    assert resolve_codex_model_tag(settings, LlmSettings(codex_model="gpt-5.4-codex")) == "gpt-5.4-codex"
    # Env fallback.
    monkeypatch.setenv("CODEX_MODEL", "gpt-5.2-codex")
    settings2 = load_settings()
    assert resolve_codex_model_tag(settings2, LlmSettings()) == "gpt-5.2-codex"


def test_merge_rejects_foreign_codex_tag(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """A non-codex tag (plain openai gpt-5.4 / claude alias) falls back to base."""
    from akana_server.llm_settings import LlmSettings, _merge

    settings = _settings(monkeypatch, tmp_path)
    base = LlmSettings(codex_model="gpt-5-codex")
    assert _merge(base, {"codex_model": "gpt-5.4"}).codex_model == "gpt-5-codex"
    assert _merge(base, {"codex_model": "claude-opus-4-7"}).codex_model == "gpt-5-codex"
    # A real codex tag is accepted.
    assert _merge(base, {"codex_model": "gpt-5.4-codex"}).codex_model == "gpt-5.4-codex"


# --------------------------------------------------------------------------- #
# CLI/auth probe
# --------------------------------------------------------------------------- #
def test_probe_not_installed(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    monkeypatch.setattr(codex_catalog, "_codex_on_path", lambda: None)
    res = asyncio.run(codex_catalog.probe_codex_cli(settings))
    assert res["installed"] is False
    assert res["reachable"] is False
    assert res["error_code"] == "not_installed"
    assert "npm install -g @openai/codex" in res["error"]


def test_probe_installed_not_logged_in(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    monkeypatch.setattr(codex_catalog, "_codex_on_path", lambda: "/usr/bin/codex")
    _patch_login(monkeypatch, _FakeLoginProc(returncode=1, err=b"Not logged in"))
    res = asyncio.run(codex_catalog.probe_codex_cli(settings))
    assert res["installed"] is True
    assert res["logged_in"] is False
    assert res["error_code"] == "not_logged_in"


def test_probe_installed_and_logged_in(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    settings = _settings(monkeypatch, tmp_path)
    monkeypatch.setattr(codex_catalog, "_codex_on_path", lambda: "/usr/bin/codex")
    _patch_login(monkeypatch, _FakeLoginProc(returncode=0, out=b"Logged in via ChatGPT"))
    res = asyncio.run(codex_catalog.probe_codex_cli(settings))
    assert res["installed"] is True
    assert res["logged_in"] is True
    assert res["reachable"] is True
    assert res["error"] is None
    assert res["model_count"] > 0


# --------------------------------------------------------------------------- #
# UI catalog response shape
# --------------------------------------------------------------------------- #
def test_fetch_codex_models_shape_logged_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    settings = _settings(monkeypatch, tmp_path)
    monkeypatch.setattr(codex_catalog, "_codex_on_path", lambda: "/usr/bin/codex")
    _patch_login(monkeypatch, _FakeLoginProc(returncode=0))
    res = asyncio.run(codex_catalog.fetch_codex_models(settings))
    assert res["reachable"] is True
    assert res["source"] == "static"
    assert res["cached"] is False
    assert res["active"] == "gpt-5-codex"
    assert res["models"] and all(set(o) == {"value", "label"} for o in res["models"])
    assert res["error"] is None


def test_fetch_codex_models_static_list_even_when_unreachable(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A missing CLI → reachable:false but the static model list is still returned
    (never 500 — the UI shows the list + a 'run codex login/install' affordance)."""
    settings = _settings(monkeypatch, tmp_path)
    monkeypatch.setattr(codex_catalog, "_codex_on_path", lambda: None)
    res = asyncio.run(codex_catalog.fetch_codex_models(settings))
    assert res["reachable"] is False
    assert res["source"] == "static"
    assert res["models"], "the static list is returned even when the CLI is absent"
    assert res["error"] and "npm install" in res["error"]
