"""Runtime secret store — secrets.json (0600 perms, atomic write, whitelist)."""

from __future__ import annotations

import json
import os

import pytest

from akana_server import secret_store, vault_crypto
from akana_server.secret_store import (
    ALLOWED_KEYS,
    get_secret,
    load_secrets,
    mask_hint,
    set_secrets,
)


def test_load_missing_file_returns_empty(tmp_path) -> None:
    assert load_secrets(tmp_path) == {}


def test_set_and_load_roundtrip(tmp_path) -> None:
    state = set_secrets(tmp_path, {"cursor_api_key": "key_abc12345"})
    assert state == {"cursor_api_key": "key_abc12345"}
    assert load_secrets(tmp_path) == {"cursor_api_key": "key_abc12345"}
    assert get_secret(tmp_path, "cursor_api_key") == "key_abc12345"
    assert get_secret(tmp_path, "claude_oauth_token") is None


def test_internal_whitespace_stripped_on_save(tmp_path) -> None:
    """Whitespace/newlines injected during copy-paste are cleaned — root of the auth 401 bug.

    OAuth tokens/keys never contain internal whitespace; a lead/trail strip is not
    enough, internal whitespace breaks the bearer (API '401 Invalid bearer token')."""
    set_secrets(tmp_path, {"claude_oauth_token": "sk-ant-oat01 -abc \n def"})
    assert get_secret(tmp_path, "claude_oauth_token") == "sk-ant-oat01-abcdef"


def test_internal_whitespace_cleaned_on_read_for_legacy_value(tmp_path) -> None:
    """A value saved with whitespace BEFORE the hardening is also cleaned on READ →
    when the user restarts, the old broken token fixes itself without a re-save (backward-compatible)."""
    secret_store._write_atomic(
        secret_store._secrets_path(tmp_path), {"cursor_api_key": "ab cd\tef"}
    )
    assert get_secret(tmp_path, "cursor_api_key") == "abcdef"


def test_file_permissions_are_0600(tmp_path) -> None:
    set_secrets(tmp_path, {"claude_oauth_token": "sk-ant-oat01-xyz"})
    path = tmp_path / "secrets.json"
    # Unix 0600 mode bits don't map onto Windows ACLs (st_mode never reflects the
    # chmod), so the bit-check is POSIX-gated; the write path itself runs on every OS.
    if os.name != "nt":
        assert oct(path.stat().st_mode & 0o777) == "0o600"
    # Permissions survive an overwrite too.
    set_secrets(tmp_path, {"cursor_api_key": "key_abc12345"})
    if os.name != "nt":
        assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_atomic_write_no_tmp_leftover(tmp_path) -> None:
    set_secrets(tmp_path, {"cursor_api_key": "key_abc12345"})
    assert not (tmp_path / "secrets.json.tmp").exists()


def test_atomic_write_failure_keeps_old_content(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_secrets(tmp_path, {"cursor_api_key": "key_abc12345"})

    def boom(src, dst):
        raise OSError("disk full")

    # Atomic write now lives in vault_crypto.write_private_bytes_atomic — patch the
    # os.replace it actually calls (the seam moved during the D2 consolidation).
    monkeypatch.setattr(vault_crypto.os, "replace", boom)
    with pytest.raises(OSError):
        set_secrets(tmp_path, {"cursor_api_key": "key_other9999"})
    monkeypatch.undo()
    assert load_secrets(tmp_path) == {"cursor_api_key": "key_abc12345"}


def test_whitelist_rejects_unknown_keys(tmp_path) -> None:
    state = set_secrets(
        tmp_path, {"cursor_api_key": "key_abc12345", "evil": "x", "api_token": "y"}
    )
    assert state == {"cursor_api_key": "key_abc12345"}
    assert load_secrets(tmp_path) == {"cursor_api_key": "key_abc12345"}


def test_encrypted_at_rest(tmp_path) -> None:
    set_secrets(tmp_path, {"cursor_api_key": "key_abc12345"})
    blob = (tmp_path / "secrets.json").read_bytes()
    # Tagged ciphertext on disk — the raw secret never appears in plaintext.
    assert blob.startswith(b"vault1:")
    assert b"key_abc12345" not in blob


def test_legacy_plaintext_is_read_then_reencrypted(tmp_path) -> None:
    # Pre-encryption store: plaintext JSON written directly.
    (tmp_path / "secrets.json").write_text(
        json.dumps({"cursor_api_key": "key_legacy123"}), encoding="utf-8"
    )
    assert load_secrets(tmp_path) == {"cursor_api_key": "key_legacy123"}
    # The next write migrates it to encrypted form without losing data.
    set_secrets(tmp_path, {"claude_oauth_token": "sk-ant-oat01-new"})
    blob = (tmp_path / "secrets.json").read_bytes()
    assert blob.startswith(b"vault1:")
    assert load_secrets(tmp_path) == {
        "cursor_api_key": "key_legacy123",
        "claude_oauth_token": "sk-ant-oat01-new",
    }


def test_empty_string_clears_key(tmp_path) -> None:
    set_secrets(
        tmp_path,
        {"cursor_api_key": "key_abc12345", "claude_oauth_token": "sk-ant-oat01-xyz"},
    )
    state = set_secrets(tmp_path, {"cursor_api_key": ""})
    assert state == {"claude_oauth_token": "sk-ant-oat01-xyz"}
    assert get_secret(tmp_path, "cursor_api_key") is None


def test_partial_patch_keeps_other_keys(tmp_path) -> None:
    set_secrets(tmp_path, {"cursor_api_key": "key_abc12345"})
    state = set_secrets(tmp_path, {"claude_oauth_token": "sk-ant-oat01-xyz"})
    assert state == {
        "cursor_api_key": "key_abc12345",
        "claude_oauth_token": "sk-ant-oat01-xyz",
    }


def test_corrupt_file_degrades_to_empty(tmp_path) -> None:
    (tmp_path / "secrets.json").write_text("not-json{", encoding="utf-8")
    assert load_secrets(tmp_path) == {}
    (tmp_path / "secrets.json").write_text('["a list"]', encoding="utf-8")
    assert load_secrets(tmp_path) == {}
    # set_secrets on a corrupt store starts fresh instead of crashing.
    state = set_secrets(tmp_path, {"cursor_api_key": "key_abc12345"})
    assert state == {"cursor_api_key": "key_abc12345"}


def test_load_filters_non_string_and_unknown(tmp_path) -> None:
    (tmp_path / "secrets.json").write_text(
        json.dumps({"cursor_api_key": 42, "claude_oauth_token": " tok_12345 ", "x": "y"}),
        encoding="utf-8",
    )
    assert load_secrets(tmp_path) == {"claude_oauth_token": "tok_12345"}


def test_mask_hint() -> None:
    assert mask_hint("key_abcdEfGh") == "…EfGh"
    assert mask_hint("short") == "set"
    assert mask_hint("1234567") == "set"
    assert mask_hint("12345678") == "…5678"
    assert mask_hint("") == "set"


def test_wrong_key_set_refuses_and_does_not_wipe(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """set_secrets with the wrong master key does NOT overwrite the existing encrypted secret.

    Previous behavior: when load_secrets cannot decrypt it returns {} → the
    read-modify-write starts from an empty base → it writes an empty dict with the
    new key and PERMANENTLY deletes all secrets. It now raises RuntimeError inside
    the writer lock and the file is left untouched (no secret loss)."""
    from cryptography.fernet import Fernet

    set_secrets(tmp_path, {"cursor_api_key": "key_abc12345"})
    path = tmp_path / "secrets.json"
    original = path.read_bytes()
    assert original.startswith(b"vault1:")

    # Change the master key (wrong/corrupt key scenario) and reset the cache.
    monkeypatch.setenv("AKANA_VAULT_KEY", Fernet.generate_key().decode("utf-8"))
    secret_store.vault_crypto.reset_cache()

    with pytest.raises(RuntimeError):
        set_secrets(tmp_path, {"claude_oauth_token": "sk-ant-oat01-new"})

    # The encrypted file must stay bit-for-bit identical — no secret was lost.
    assert path.read_bytes() == original

    # Once the correct key is restored, the original secret is still readable.
    monkeypatch.delenv("AKANA_VAULT_KEY", raising=False)
    secret_store.vault_crypto.reset_cache()
    assert load_secrets(tmp_path) == {"cursor_api_key": "key_abc12345"}


def test_allowed_keys_frozen() -> None:
    assert ALLOWED_KEYS == frozenset(
        {
            "cursor_api_key",
            "claude_oauth_token",
            "gemini_api_key",
            "openai_api_key",
            "telegram_bot_token",
        }
    )
