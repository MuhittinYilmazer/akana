"""config._int_env range (lo/hi) clamp — a corrupt/out-of-range env must not break boot/behavior.

Round-2 B5: ``AKANA_PORT=0/99999999`` could lead to a uvicorn bind crash. The
env path had no range clamp.
"""

from __future__ import annotations

import pytest

from akana_server import config as cfg


def test_int_env_unset_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("X_TEST_INT", raising=False)
    assert cfg._int_env("X_TEST_INT", 7) == 7


def test_int_env_valid_in_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_TEST_INT", "12")
    assert cfg._int_env("X_TEST_INT", 7, lo=0, hi=23) == 12


def test_int_env_non_int_falls_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_TEST_INT", "abc")
    assert cfg._int_env("X_TEST_INT", 7, lo=0, hi=23) == 7


def test_int_env_above_hi_falls_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_TEST_INT", "25")
    assert cfg._int_env("X_TEST_INT", 3, lo=0, hi=23) == 3


def test_int_env_below_lo_falls_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_TEST_INT", "-5")
    assert cfg._int_env("X_TEST_INT", 3, lo=0, hi=23) == 3


def test_int_env_at_bounds_inclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_TEST_INT", "0")
    assert cfg._int_env("X_TEST_INT", 3, lo=0, hi=23) == 0
    monkeypatch.setenv("X_TEST_INT", "23")
    assert cfg._int_env("X_TEST_INT", 3, lo=0, hi=23) == 23


def test_int_env_no_bounds_accepts_any_int(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_TEST_INT", "99999")
    assert cfg._int_env("X_TEST_INT", 7) == 99999


def test_port_out_of_range_uses_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AKANA_PORT", "99999999")
    assert cfg.load_settings().server_port == 8766


# -- C9: _float_env range (lo/hi) clamp — a NEGATIVE bridge timeout would feed combine_cap
#         and silently DISABLE the stream idle/hang ceiling. --


def test_float_env_unset_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("X_TEST_FLOAT", raising=False)
    assert cfg._float_env("X_TEST_FLOAT", 1.5) == 1.5


def test_float_env_valid_in_range(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_TEST_FLOAT", "2.5")
    assert cfg._float_env("X_TEST_FLOAT", 1.0, lo=0.0, hi=10.0) == 2.5


def test_float_env_non_float_falls_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_TEST_FLOAT", "abc")
    assert cfg._float_env("X_TEST_FLOAT", 1.0, lo=0.0) == 1.0


def test_float_env_below_lo_falls_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_TEST_FLOAT", "-30")
    assert cfg._float_env("X_TEST_FLOAT", 1800.0, lo=0.0) == 1800.0


def test_float_env_no_bounds_accepts_any_float(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("X_TEST_FLOAT", "-7.5")
    assert cfg._float_env("X_TEST_FLOAT", 1.0) == -7.5


def test_negative_bridge_timeout_floored_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """A negative CLAUDE_BRIDGE_TIMEOUT must not survive to disable the hang ceiling."""
    monkeypatch.setenv("CLAUDE_BRIDGE_TIMEOUT", "-1")
    assert cfg.load_settings().claude_bridge_timeout == 1800.0
    monkeypatch.setenv("CURSOR_BRIDGE_TIMEOUT", "-5")
    assert cfg.load_settings().bridge_timeout == 1800.0


# -- _load_env: a non-UTF-8 .env must raise a clear startup error, not a raw UnicodeDecodeError --


def test_load_env_non_utf8_raises_clear_error(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A UTF-16 .env (PowerShell `>` redirection) must surface a clear, file-named
    startup error instead of python-dotenv's raw UnicodeDecodeError traceback."""
    env = tmp_path / ".env"
    env.write_bytes("AKANA_LANGUAGE=tr\n".encode("utf-16"))  # BOM + UTF-16
    monkeypatch.setattr(cfg, "_repo_root", lambda: tmp_path)
    with pytest.raises(RuntimeError, match="not UTF-8"):
        cfg._load_env()


def test_load_env_utf8_loads_cleanly(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A well-formed UTF-8 .env still loads without error (no regression)."""
    env = tmp_path / ".env"
    env.write_text("X_LOAD_ENV_PROBE=1\n", encoding="utf-8")
    monkeypatch.setattr(cfg, "_repo_root", lambda: tmp_path)
    monkeypatch.delenv("X_LOAD_ENV_PROBE", raising=False)
    cfg._load_env()  # must not raise


# -- R4-F #1/#3: EMPTY string env → default (os.environ.get only returns default for a MISSING key) --


def test_str_env_unset_empty_whitespace_fall_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("X_TEST_STR", raising=False)
    assert cfg._str_env("X_TEST_STR", "def") == "def"  # unset
    monkeypatch.setenv("X_TEST_STR", "")
    assert cfg._str_env("X_TEST_STR", "def") == "def"  # empty
    monkeypatch.setenv("X_TEST_STR", "   ")
    assert cfg._str_env("X_TEST_STR", "def") == "def"  # whitespace
    monkeypatch.setenv("X_TEST_STR", " val ")
    assert cfg._str_env("X_TEST_STR", "def") == "val"  # set → stripped value


def test_empty_data_dir_does_not_resolve_to_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    """AKANA_DATA_DIR="" → all data must not land under CWD (fall back to the ~/.akana default)."""
    monkeypatch.setenv("AKANA_DATA_DIR", "")
    data_dir = cfg.load_settings().data_dir
    from pathlib import Path

    assert data_dir != Path.cwd(), "empty data_dir must not resolve to CWD (data loss)"
    assert data_dir.name == ".akana", f"must fall back to the ~/.akana default, got {data_dir}"


def test_empty_host_does_not_bind_all_interfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    """AKANA_HOST="" → not 0.0.0.0 (all interfaces) but the safe 127.0.0.1 default."""
    monkeypatch.setenv("AKANA_HOST", "")
    assert cfg.load_settings().server_host == "127.0.0.1"


# -- R4-F #2: an empty new-name must not mask the legacy value (apply_legacy_env_aliases) --


def test_legacy_alias_bridges_when_new_key_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty ``AKANA_TOKEN=`` (.env line) must not prevent bridging the legacy
    ``AKANA_CURSOR_TOKEN`` — otherwise the token would be silently disabled."""
    import os

    monkeypatch.setenv("AKANA_TOKEN", "")  # like an empty line in .env
    monkeypatch.setenv("AKANA_CURSOR_TOKEN", "legacy-secret")
    cfg.apply_legacy_env_aliases()
    assert os.environ["AKANA_TOKEN"] == "legacy-secret"


def test_legacy_alias_explicit_new_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicitly given (non-empty) new name always beats the legacy one."""
    import os

    monkeypatch.setenv("AKANA_TOKEN", "new-token")
    monkeypatch.setenv("AKANA_CURSOR_TOKEN", "legacy")
    cfg.apply_legacy_env_aliases()
    assert os.environ["AKANA_TOKEN"] == "new-token"
