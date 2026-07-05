"""Vault crypto primitives — key provider, tagged Fernet cipher, legacy decode."""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet

from akana_server import vault_crypto


def test_encrypt_decrypt_roundtrip() -> None:
    blob = vault_crypto.encrypt_str("hunter2")
    assert blob.startswith(b"vault1:")
    assert b"hunter2" not in blob
    assert vault_crypto.decrypt_to_str(blob) == "hunter2"


def test_is_encrypted_tagging() -> None:
    assert vault_crypto.is_encrypted(vault_crypto.encrypt_str("x"))
    assert not vault_crypto.is_encrypted(b'{"plain": true}')
    assert not vault_crypto.is_encrypted(b"")


def test_load_text_handles_encrypted_and_legacy() -> None:
    enc = vault_crypto.encrypt_str('{"k": "v"}')
    assert vault_crypto.load_text(enc) == '{"k": "v"}'
    # Legacy plaintext passes through untouched.
    assert vault_crypto.load_text(b'{"k": "v"}') == '{"k": "v"}'
    # Non-UTF-8 garbage → None (caller degrades to empty).
    assert vault_crypto.load_text(b"\xff\xfe\x00bad") is None
    assert vault_crypto.load_text(b"") is None


def test_decrypt_rejects_foreign_and_corrupt() -> None:
    assert vault_crypto.decrypt_to_str(b"not-ours") is None
    assert vault_crypto.decrypt_to_str(b"vault1:garbage-token") is None


def test_keyfile_generated_with_0600(tmp_path: Path, monkeypatch) -> None:
    keyfile = tmp_path / "nested" / "vault.key"
    monkeypatch.setenv("AKANA_VAULT_KEYFILE", str(keyfile))
    monkeypatch.delenv("AKANA_VAULT_KEY", raising=False)
    vault_crypto.reset_cache()
    key = vault_crypto.get_master_key()
    assert keyfile.is_file()
    # Unix 0600 mode bits don't map onto Windows ACLs; gate the bit-check on POSIX
    # while still asserting the keyfile is generated/persisted on every OS.
    if os.name != "nt":
        assert oct(keyfile.stat().st_mode & 0o777) == "0o600"
    # Stable across calls.
    assert vault_crypto.get_master_key() == key


def test_env_key_takes_precedence(tmp_path: Path, monkeypatch) -> None:
    raw = Fernet.generate_key()
    monkeypatch.setenv("AKANA_VAULT_KEY", raw.decode("utf-8"))
    monkeypatch.setenv("AKANA_VAULT_KEYFILE", str(tmp_path / "unused.key"))
    vault_crypto.reset_cache()
    assert vault_crypto.get_master_key() == raw
    # No keyfile is created when the env key is supplied.
    assert not (tmp_path / "unused.key").exists()


def test_health_healthy_by_default() -> None:
    vault_crypto.reset_cache()
    h = vault_crypto.health()
    assert h["available"] is True
    assert h["healthy"] is True
    assert h["decrypt_failures"] == 0
    assert h["key_source"] in ("keyfile", "env", "keyring")


def test_undecryptable_blob_is_noticed_not_silent() -> None:
    vault_crypto.reset_cache()
    # Tagged ciphertext that cannot be decrypted with the current key.
    assert vault_crypto.load_text(b"vault1:bogus-token-value") is None
    h = vault_crypto.health()
    assert h["decrypt_failures"] >= 1
    assert h["healthy"] is False
    vault_crypto.reset_cache()  # counter clears on rotation/restart
    assert vault_crypto.health()["decrypt_failures"] == 0


def test_file_lock_acquires_and_releases(tmp_path: Path) -> None:
    lock = tmp_path / "nested" / ".vault.lock"
    with vault_crypto.file_lock(lock):
        assert lock.exists()
    # Re-acquiring after release must not block (lock was freed).
    with vault_crypto.file_lock(lock):
        pass
