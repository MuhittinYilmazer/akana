"""Akana launcher (akana_cli) unit tests."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from contextlib import contextmanager

from akana_cli.env_util import EnvDecodeError, read_env_key, server_host_port
from akana_cli.main import build_parser
from akana_cli.paths import REPO_ROOT, venv_python
from akana_cli.stop_cmd import find_pids_on_port, terminate_pid
from akana_cli.voice_assets import (
    PIPER_BY_NAME,
    PIPER_CATALOG,
    PIPER_VOICES,
    _download,
    _expected_length,
    _resolve_selection,
    _voice_urls,
    default_voice_names,
    ensure_voice,
    install_piper_voices,
    resolve_voices_dir,
)


def test_repo_root_exists() -> None:
    assert (REPO_ROOT / "akana.py").is_file()
    assert (REPO_ROOT / "akana_server").is_dir()


def test_venv_python_path_shape() -> None:
    p = venv_python()
    if sys.platform == "win32":
        assert p.name == "python.exe"
        assert "Scripts" in p.parts
    else:
        assert p.name == "python"
        assert p.parent.name == "bin"


def test_cli_parser_commands() -> None:
    p = build_parser()
    assert p.parse_args(["doctor"]).command == "doctor"
    assert p.parse_args(["stop"]).command == "stop"


def test_voice_urls_match_install_script() -> None:
    onnx, cfg = _voice_urls("tr_TR-dfki-medium", "tr/tr_TR/dfki/medium")
    assert onnx.endswith("tr_TR-dfki-medium.onnx")
    assert "huggingface.co/rhasspy/piper-voices" in onnx
    assert cfg.endswith("tr_TR-dfki-medium.onnx.json")
    assert len(PIPER_VOICES) == 2


def test_ensure_voice_skips_existing(tmp_path: Path) -> None:
    voices = tmp_path / "voices"
    voices.mkdir()
    name, sub = PIPER_VOICES[0]
    (voices / f"{name}.onnx").write_bytes(b"x" * 8)
    (voices / f"{name}.onnx.json").write_text("{}")

    with patch("akana_cli.voice_assets._download") as dl:
        assert ensure_voice(name, sub, voices, verbose=False) is True
        dl.assert_not_called()


def test_piper_defaults_are_the_two_shipped_voices() -> None:
    """The default selection stays TR dfki + EN amy (the shipped voices) so the
    non-interactive / CI path is unchanged."""
    assert default_voice_names() == ["tr_TR-dfki-medium", "en_US-amy-medium"]
    assert PIPER_VOICES == (
        ("tr_TR-dfki-medium", "tr/tr_TR/dfki/medium"),
        ("en_US-amy-medium", "en/en_US/amy/medium"),
    )
    # Catalog is a superset offering more choices, and every default is in it.
    assert len(PIPER_CATALOG) > 2
    for name in default_voice_names():
        assert name in PIPER_BY_NAME


def test_resolve_selection_maps_names_and_falls_back() -> None:
    # None → shipped defaults.
    assert _resolve_selection(None) == PIPER_VOICES
    # An explicit pick installs exactly those, in given order, deduped.
    assert _resolve_selection(["en_US-lessac-medium", "en_US-lessac-medium"]) == (
        ("en_US-lessac-medium", "en/en_US/lessac/medium"),
    )
    # Unknown names are ignored; an all-unknown / empty pick falls back to defaults.
    assert _resolve_selection(["does-not-exist"]) == PIPER_VOICES
    assert _resolve_selection([]) == PIPER_VOICES


def test_install_piper_voices_downloads_only_selected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A selection installs only the chosen voices, not the fixed set."""
    voices = tmp_path / "voices"
    calls: list[str] = []

    def _fake_ensure(name, sub, target, *, verbose=True):
        calls.append(name)
        return False

    monkeypatch.setattr("akana_cli.voice_assets.ensure_voice", _fake_ensure)
    install_piper_voices(selection=["en_GB-alba-medium"], voices_dir=voices, verbose=False)
    assert calls == ["en_GB-alba-medium"]


def test_setup_installs_voice_extras_interactively(monkeypatch: pytest.MonkeyPatch) -> None:
    """First-run setup customizes voice downloads: voice extras install INTERACTIVELY
    (so the Piper-voice checklist + Whisper-size prompts fire), while providers stay in
    the clean non-interactive batch (their keys are configured afterwards in the UI)."""
    from akana_cli import add_cmd, io, setup_cmd

    monkeypatch.setattr(io, "ask_checklist", lambda *_a, **_k: ["gemini", "voice-full"])
    # Force everything to look not-yet-installed so both are pending.
    monkeypatch.setattr("akana_cli.components.deps_installed", lambda _c: False)

    seen: dict[str, bool] = {}

    def _fake_install(comp, *, interactive=True):
        seen[comp.id] = interactive
        return True

    monkeypatch.setattr(add_cmd, "install_component", _fake_install)

    picks = setup_cmd._select_and_install()
    assert seen["voice-full"] is True   # voice customizes (which voices / Whisper size)
    assert seen["gemini"] is False      # provider stays in the clean batch
    assert set(picks) == {"gemini", "voice-full"}


def test_voice_full_post_install_prompts_voices_not_wake(monkeypatch: pytest.MonkeyPatch) -> None:
    """voice-full post-install prompts WHICH Piper voices + the Whisper size, but NEVER
    prompts to keep/disable the bundled 'Hey Akana' wake word — it is not a choice."""
    from akana_cli import add_cmd, io, setup_cmd
    from akana_cli.components import REGISTRY

    def _boom(*_a, **_k):
        raise AssertionError("the bundled wake word must not be a setup choice")

    monkeypatch.setattr(io, "ask_yes_no", _boom)  # a wake opt-out would use this
    monkeypatch.setattr(io, "ask_checklist", lambda *_a, **_k: [])  # Piper picker → defaults
    monkeypatch.setattr(io, "ask_choice", lambda *_a, **_k: "small")  # Whisper size
    monkeypatch.setattr("akana_cli.voice_assets.install_piper_voices", lambda **_k: None)
    writes: list[tuple[str, str]] = []
    monkeypatch.setattr(setup_cmd, "_write_env_key", lambda k, v: writes.append((k, v)))

    add_cmd._post_install(REGISTRY["voice-full"], interactive=True)
    assert ("WHISPER_MODEL", "small") in writes  # Whisper size was chosen interactively


def test_resolve_voices_dir_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom = tmp_path / "my-voices"
    monkeypatch.setenv("AKANA_VOICES_DIR", str(custom))
    assert resolve_voices_dir() == custom.resolve()


def test_read_env_key_from_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text("CURSOR_API_KEY=secret\nAKANA_PORT=9999\n", encoding="utf-8")
    monkeypatch.setattr("akana_cli.env_util.ENV_FILE", env)
    assert read_env_key("CURSOR_API_KEY") == "secret"
    assert read_env_key("MISSING") is None


def test_server_host_port_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AKANA_HOST", raising=False)
    monkeypatch.delenv("AKANA_PORT", raising=False)
    with patch("akana_cli.env_util.ENV_FILE", Path("/nonexistent/.env")):
        host, port = server_host_port()
    assert host == "127.0.0.1"
    assert port == 8766


def test_find_pids_on_port_parses_ss(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("akana_cli.stop_cmd.sys.platform", "linux")
    fake = MagicMock(returncode=0, stdout="LISTEN 0 128 127.0.0.1:8766 users:((\"python\",pid=4242,fd=3))")
    with patch("akana_cli.stop_cmd._which", return_value="/usr/bin/ss"):
        with patch("akana_cli.stop_cmd.subprocess.run", return_value=fake):
            assert find_pids_on_port(8766) == [4242]


def test_terminate_pid_already_dead() -> None:
    assert terminate_pid(99999999, grace_seconds=0.1) is True


def test_find_pids_on_port_windows_matches_port_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    """E2: Windows netstat parsing must match the Local-Address PORT exactly. A bare
    ':{port}' substring match killed the WRONG process tree (port 80 matched ':8080')."""
    monkeypatch.setattr("akana_cli.stop_cmd.sys.platform", "win32")
    netstat = (
        "Active Connections\n"
        "  Proto  Local Address          Foreign Address        State           PID\n"
        "  TCP    127.0.0.1:8766         0.0.0.0:0              LISTENING       4242\n"
        "  TCP    127.0.0.1:8080         0.0.0.0:0              LISTENING       9999\n"
        "  TCP    127.0.0.1:80           0.0.0.0:0              LISTENING       7777\n"
        "  UDP    127.0.0.1:8766         *:*                                   5555\n"
    )
    fake = MagicMock(returncode=0, stdout=netstat)
    with patch("akana_cli.stop_cmd.subprocess.run", return_value=fake):
        # 8766: only the TCP LISTENING row (not the UDP row, not :8080/:80).
        assert find_pids_on_port(8766) == [4242]
        # 80: must NOT match ':8766' or ':8080'.
        assert find_pids_on_port(80) == [7777]


def test_load_repo_dotenv_expands_only_path_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """E4: only path-valued keys get ~ expansion. A token/secret beginning with ~ must be
    stored VERBATIM — expanding every value corrupted such tokens (e.g. AKANA_TOKEN)."""
    import os

    from akana_cli import env_util

    env = tmp_path / ".env"
    env.write_text("AKANA_TOKEN=~keep-literal\nAKANA_DATA_DIR=~/akdata\n", encoding="utf-8")
    monkeypatch.setattr(env_util, "ENV_FILE", env)
    monkeypatch.delenv("AKANA_TOKEN", raising=False)
    monkeypatch.delenv("AKANA_DATA_DIR", raising=False)
    monkeypatch.delenv("AKANA_CURSOR_TOKEN", raising=False)
    try:
        env_util.load_repo_dotenv()
        assert os.environ["AKANA_TOKEN"] == "~keep-literal"  # NON-path key → NOT expanded
        assert os.environ["AKANA_DATA_DIR"] == os.path.expanduser("~/akdata")  # path key → expanded
    finally:
        os.environ.pop("AKANA_TOKEN", None)
        os.environ.pop("AKANA_DATA_DIR", None)


class _FakeResp:
    """Minimal urlopen() context-manager stand-in for _download tests."""

    def __init__(self, body: bytes, content_length: str | None) -> None:
        self._body = body
        self.headers = {} if content_length is None else {"Content-Length": content_length}

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, *_a) -> bool:  # noqa: ANN002
        return False

    def read(self) -> bytes:
        return self._body


@contextmanager
def _patch_urlopen(body: bytes, content_length: str | None):
    def _fake(_req, *_a, **_k):  # noqa: ANN001, ANN202
        return _FakeResp(body, content_length)

    with patch("akana_cli.voice_assets.urllib.request.urlopen", _fake):
        yield


def test_expected_length_parses_or_skips() -> None:
    # Absent or unparseable → None (skip the short-read check, never abort).
    assert _expected_length(None) is None
    assert _expected_length("not-a-number") is None
    assert _expected_length("") is None
    # A plain int parses; a comma-joined RFC-legal duplicate takes the first part.
    assert _expected_length("1234") == 1234
    assert _expected_length("1234, 1234") == 1234


def test_download_malformed_content_length_still_succeeds(tmp_path: Path) -> None:
    """A duplicated/garbled Content-Length must NOT abort a fully-received body across
    all retries — the short-read check is skipped when the header can't be parsed."""
    dest = tmp_path / "voice.onnx"
    with _patch_urlopen(b"REALBYTES", content_length="9, 9"):
        _download("https://example/voice.onnx", dest, retries=1)
    assert dest.read_bytes() == b"REALBYTES"


def test_download_empty_body_with_positive_length_raises(tmp_path: Path) -> None:
    """A Content-Length>0 whose body reads as b'' (truncated connection) must be caught
    as a short read, not committed as a 0-byte 'successful' download."""
    dest = tmp_path / "voice.onnx"
    with _patch_urlopen(b"", content_length="512"):
        with pytest.raises(RuntimeError, match="download failed"):
            _download("https://example/voice.onnx", dest, retries=2)
    assert not dest.exists()  # no 0-byte file committed


def test_download_no_content_length_writes_body(tmp_path: Path) -> None:
    """No Content-Length header → write whatever arrived (validation skipped)."""
    dest = tmp_path / "cfg.json"
    with _patch_urlopen(b"{}", content_length=None):
        _download("https://example/cfg.json", dest, retries=1)
    assert dest.read_bytes() == b"{}"


def test_read_env_text_rejects_bom_less_utf16(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A BOM-less UTF-16LE .env decodes under utf-8-sig into NUL-laced garbage without
    raising; _read_env_text must catch the NUL and raise EnvDecodeError instead."""
    from akana_cli import env_util

    env = tmp_path / ".env"
    env.write_bytes("AKANA_LANGUAGE=tr\n".encode("utf-16-le"))  # no BOM
    monkeypatch.setattr(env_util, "ENV_FILE", env)
    with pytest.raises(EnvDecodeError):
        env_util.read_env_key("AKANA_LANGUAGE")


def test_resolved_provider_rejects_invalid_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A corrupt/legacy provider in llm_settings.json (e.g. 'foo') must resolve to ''
    so `start` still shows the 'no provider configured' warning (chat is dead too)."""
    from akana_cli import start_cmd

    store_dir = tmp_path
    (store_dir / "llm_settings.json").write_text('{"provider": "foo"}', encoding="utf-8")
    monkeypatch.setattr(start_cmd, "default_data_dir", lambda: store_dir)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    assert start_cmd._resolved_provider() == ""

    # A recognized value passes through unchanged.
    (store_dir / "llm_settings.json").write_text('{"provider": "cursor"}', encoding="utf-8")
    assert start_cmd._resolved_provider() == "cursor"


def test_oww_preinstall_failure_suppresses_generic_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the openwakeword preinstall fails, install_component must NOT also print the
    generic 'reported success but not importable' verify line (it would contradict the
    specific oww message the user just saw). It still returns False (install failed)."""
    from akana_cli import add_cmd, io
    from akana_cli.components import REGISTRY

    comp = REGISTRY["voice-full"]
    monkeypatch.setattr(add_cmd, "deps_installed", lambda _c: False)
    # Preinstall reports a specific failure and aborts the install.
    monkeypatch.setattr(add_cmd, "_install_requirements", lambda _c: False)
    monkeypatch.setattr(add_cmd, "_post_install", lambda *_a, **_k: None)

    fails: list[str] = []
    monkeypatch.setattr(io, "fail", lambda msg, *_a, **_k: fails.append(msg))
    monkeypatch.setattr(io, "warn", lambda *_a, **_k: None)
    monkeypatch.setattr(io, "ok", lambda *_a, **_k: None)

    ok = add_cmd.install_component(comp, interactive=False)
    assert ok is False
    from akana_cli import i18n

    generic = i18n.t("add.verify_failed_pip", id=comp.id)
    assert generic not in fails, "generic verify line must be suppressed after the specific one"


def test_run_test_propagates_pytest_returncode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (B3): `akana.py test` used to return 0 even when pytest failed (the only
    failure signal was an uncaught CalledProcessError). It must return pytest's exit code."""
    import subprocess

    from akana_cli import test_cmd

    monkeypatch.setattr(test_cmd, "venv_exists", lambda: True)
    monkeypatch.setattr(test_cmd, "venv_python", lambda: "py")
    monkeypatch.setattr(test_cmd, "run", lambda *a, **k: subprocess.CompletedProcess([], 1))
    assert test_cmd.run_test() == 1
    monkeypatch.setattr(test_cmd, "run", lambda *a, **k: subprocess.CompletedProcess([], 0))
    assert test_cmd.run_test() == 0
