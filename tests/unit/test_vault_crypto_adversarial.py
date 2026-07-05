"""Adversarial probes for the vault crypto layer's trust + durability invariants.

``vault_crypto`` owns the master key and the fail-closed cipher. Three properties
are load-bearing and must never silently degrade:

* **key durability** — the resolved master key must be STABLE across restarts;
  a key that changes between boots orphans every previously-encrypted secret
  (silent, unrecoverable data loss). This includes the keyring-down fallback.
* **fail-closed encryption** — if the key is missing/corrupt, ``encrypt_str``
  RAISES; it never writes plaintext (the old silent-plaintext fallback was a
  security hole).
* **no destructive overwrite** — ``assert_writable`` refuses to let a writer
  overwrite a populated-but-undecryptable blob (wrong/rotated key), which would
  otherwise re-encrypt an empty base and permanently delete the secrets.

A failure here is a real confidentiality or data-loss bug, not a style nit.

Isolation (throwaway keyfile, cache reset, clean env) is provided by the
autouse ``_isolated_vault_key`` fixture in ``conftest.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from akana_server import vault_crypto


# --------------------------------------------------------------------------- #
# Key durability — the master key must survive a restart.                       #
# Regression: keyring enabled but its backend is broken (no D-Bus/SecretService)#
# used to mint a NEW key every boot and orphan all secrets.                     #
# --------------------------------------------------------------------------- #


def test_keyring_down_keyfile_fallback_survives_restart(tmp_path, monkeypatch) -> None:
    """REGRESSION: keyring on, but ``set_password`` fails → key is written to the
    fallback keyfile. The NEXT boot (keyring still broken) must read that keyfile
    back instead of minting a fresh key."""
    kf = tmp_path / "kr-fallback.key"
    monkeypatch.setenv("AKANA_VAULT_KEYRING", "1")
    monkeypatch.setenv("AKANA_VAULT_KEYFILE", str(kf))
    monkeypatch.delenv("AKANA_VAULT_KEY", raising=False)
    monkeypatch.setattr(vault_crypto, "_keyring_get", lambda: None)
    monkeypatch.setattr(vault_crypto, "_keyring_set", lambda key: False)

    vault_crypto.reset_cache()
    k1 = vault_crypto.get_master_key()
    assert kf.is_file()  # fallback keyfile was written

    vault_crypto.reset_cache()  # simulate process restart (keyring STILL broken)
    k2 = vault_crypto.get_master_key()
    assert k1 == k2, "keyring-down restart minted a new key → secret loss"


def test_secret_decrypts_after_keyring_down_restart(tmp_path, monkeypatch) -> None:
    """End-to-end of the regression: a secret encrypted before the restart must
    still decrypt after it (proves no key rotation slipped in)."""
    kf = tmp_path / "kr.key"
    monkeypatch.setenv("AKANA_VAULT_KEYRING", "1")
    monkeypatch.setenv("AKANA_VAULT_KEYFILE", str(kf))
    monkeypatch.delenv("AKANA_VAULT_KEY", raising=False)
    monkeypatch.setattr(vault_crypto, "_keyring_get", lambda: None)
    monkeypatch.setattr(vault_crypto, "_keyring_set", lambda key: False)

    vault_crypto.reset_cache()
    blob = vault_crypto.encrypt_str("survive-the-restart")

    vault_crypto.reset_cache()  # restart
    assert vault_crypto.decrypt_to_str(blob) == "survive-the-restart"


def test_keyring_used_when_available_no_keyfile_written(tmp_path, monkeypatch) -> None:
    """Happy keyring path: a stored key is used verbatim and NO keyfile is created
    (the keyfile only exists as a degraded fallback)."""
    unused = tmp_path / "unused.key"
    stored = Fernet.generate_key()
    monkeypatch.setenv("AKANA_VAULT_KEYRING", "1")
    monkeypatch.setenv("AKANA_VAULT_KEYFILE", str(unused))
    monkeypatch.delenv("AKANA_VAULT_KEY", raising=False)
    monkeypatch.setattr(vault_crypto, "_keyring_get", lambda: stored)
    monkeypatch.setattr(vault_crypto, "_keyring_set", lambda key: True)

    vault_crypto.reset_cache()
    assert vault_crypto.get_master_key() == stored
    assert not unused.exists()
    assert vault_crypto.health()["key_source"] == "keyring"


def test_keyring_empty_generates_and_stores_without_keyfile(tmp_path, monkeypatch) -> None:
    """Keyring present but empty: a key is generated and stored IN the keyring;
    the keyfile fallback is NOT touched because the keyring write succeeded."""
    unused = tmp_path / "unused.key"
    box: dict[str, bytes] = {}
    monkeypatch.setenv("AKANA_VAULT_KEYRING", "1")
    monkeypatch.setenv("AKANA_VAULT_KEYFILE", str(unused))
    monkeypatch.delenv("AKANA_VAULT_KEY", raising=False)
    monkeypatch.setattr(vault_crypto, "_keyring_get", lambda: box.get("k"))

    def _set(key: bytes) -> bool:
        box["k"] = key
        return True

    monkeypatch.setattr(vault_crypto, "_keyring_set", _set)

    vault_crypto.reset_cache()
    k = vault_crypto.get_master_key()
    assert box["k"] == k
    assert not unused.exists()


# --------------------------------------------------------------------------- #
# assert_writable — destructive-overwrite guard. Untested before this batch.    #
# --------------------------------------------------------------------------- #


def test_assert_writable_missing_file_is_allowed(tmp_path) -> None:
    vault_crypto.assert_writable(tmp_path / "does-not-exist.json")  # no raise


def test_assert_writable_empty_file_is_allowed(tmp_path) -> None:
    p = tmp_path / "empty.json"
    p.write_bytes(b"")
    vault_crypto.assert_writable(p)  # no raise


def test_assert_writable_legacy_plaintext_is_allowed(tmp_path) -> None:
    # un-tagged content is legacy plaintext → migration write is allowed
    p = tmp_path / "legacy.json"
    p.write_bytes(b'{"token": "plain"}')
    vault_crypto.assert_writable(p)  # no raise


def test_assert_writable_decryptable_ciphertext_is_allowed(tmp_path) -> None:
    p = tmp_path / "ok.json"
    p.write_bytes(vault_crypto.encrypt_str('{"token": "v"}'))
    vault_crypto.assert_writable(p)  # current key decrypts it → fine


def test_assert_writable_undecryptable_blob_raises(tmp_path) -> None:
    p = tmp_path / "bad.json"
    p.write_bytes(b"vault1:totally-bogus-token-value")
    with pytest.raises(vault_crypto.VaultUndecryptableError):
        vault_crypto.assert_writable(p)


def test_assert_writable_blocks_overwrite_after_key_rotation(tmp_path, monkeypatch) -> None:
    """The core data-loss-prevention contract: a blob written under key A must NOT
    be overwritable once the process boots with a different key B — otherwise the
    read-modify-write would start from an empty base and wipe the secret."""
    p = tmp_path / "secrets.json"
    key_a = Fernet.generate_key()
    monkeypatch.setenv("AKANA_VAULT_KEY", key_a.decode())
    vault_crypto.reset_cache()
    p.write_bytes(vault_crypto.encrypt_str("the-real-secret"))

    key_b = Fernet.generate_key()  # wrong/rotated key on the next boot
    monkeypatch.setenv("AKANA_VAULT_KEY", key_b.decode())
    vault_crypto.reset_cache()
    with pytest.raises(vault_crypto.VaultUndecryptableError):
        vault_crypto.assert_writable(p)


# --------------------------------------------------------------------------- #
# Fail-closed encryption — corrupt/missing key RAISES, never plaintext.         #
# --------------------------------------------------------------------------- #


def test_encrypt_str_fails_closed_on_corrupt_master_key(monkeypatch) -> None:
    monkeypatch.setenv("AKANA_VAULT_KEY", "this-is-not-a-valid-fernet-key")
    vault_crypto.reset_cache()
    with pytest.raises(RuntimeError):
        vault_crypto.encrypt_str("must-never-be-written-as-plaintext")


def test_decrypt_returns_none_and_counts_failure_on_corrupt_key(monkeypatch) -> None:
    good = Fernet.generate_key()
    monkeypatch.setenv("AKANA_VAULT_KEY", good.decode())
    vault_crypto.reset_cache()
    blob = vault_crypto.encrypt_str("x")

    monkeypatch.setenv("AKANA_VAULT_KEY", "corrupt-key-not-base64")
    vault_crypto.reset_cache()
    assert vault_crypto.decrypt_to_str(blob) is None
    assert vault_crypto.load_text(blob) is None
    assert vault_crypto.health()["decrypt_failures"] >= 1
    assert vault_crypto.health()["healthy"] is False


def test_blob_from_one_key_is_undecryptable_with_another(monkeypatch) -> None:
    key_a = Fernet.generate_key()
    monkeypatch.setenv("AKANA_VAULT_KEY", key_a.decode())
    vault_crypto.reset_cache()
    blob = vault_crypto.encrypt_str("secret-A")

    key_b = Fernet.generate_key()
    monkeypatch.setenv("AKANA_VAULT_KEY", key_b.decode())
    vault_crypto.reset_cache()
    assert vault_crypto.decrypt_to_str(blob) is None


# --------------------------------------------------------------------------- #
# Keyfile parsing — whitespace tolerance, empty → regenerate.                   #
# --------------------------------------------------------------------------- #


def test_keyfile_surrounding_whitespace_is_stripped(tmp_path, monkeypatch) -> None:
    raw = Fernet.generate_key()
    kf = tmp_path / "ws.key"
    kf.write_bytes(b"\n  " + raw + b"  \n")  # padded with whitespace/newlines
    monkeypatch.setenv("AKANA_VAULT_KEYFILE", str(kf))
    monkeypatch.delenv("AKANA_VAULT_KEY", raising=False)
    vault_crypto.reset_cache()
    assert vault_crypto.get_master_key() == raw


def test_empty_keyfile_regenerates_and_persists(tmp_path, monkeypatch) -> None:
    kf = tmp_path / "blank.key"
    kf.write_bytes(b"   \n")  # whitespace-only strips to empty → treated as absent
    monkeypatch.setenv("AKANA_VAULT_KEYFILE", str(kf))
    monkeypatch.delenv("AKANA_VAULT_KEY", raising=False)
    vault_crypto.reset_cache()
    k = vault_crypto.get_master_key()
    assert k
    assert kf.read_bytes().strip() == k  # freshly generated key was persisted


def test_master_key_cached_until_reset(tmp_path, monkeypatch) -> None:
    kf = tmp_path / "cache.key"
    monkeypatch.setenv("AKANA_VAULT_KEYFILE", str(kf))
    monkeypatch.delenv("AKANA_VAULT_KEY", raising=False)
    vault_crypto.reset_cache()
    k1 = vault_crypto.get_master_key()
    kf.unlink()  # delete the source file...
    assert vault_crypto.get_master_key() == k1  # ...cache still serves it
    vault_crypto.reset_cache()
    assert vault_crypto.get_master_key() != k1  # reset + file gone → fresh key


# --------------------------------------------------------------------------- #
# Tagging / decode edges — prefix must be exact and at the start.               #
# --------------------------------------------------------------------------- #


def test_is_encrypted_prefix_must_be_exact_and_leading() -> None:
    assert vault_crypto.is_encrypted(b"vault1:anything")
    assert vault_crypto.is_encrypted(b"vault1:")  # bare tag still counts as ours
    assert not vault_crypto.is_encrypted(b"  vault1:x")  # not at offset 0
    assert not vault_crypto.is_encrypted(b"VAULT1:x")  # case-sensitive
    assert not vault_crypto.is_encrypted(b"vault2:x")  # different version tag
    assert not vault_crypto.is_encrypted(b"")


def test_decrypt_bare_prefix_is_none() -> None:
    # tagged but the ciphertext is empty → InvalidToken → None (no crash)
    assert vault_crypto.decrypt_to_str(b"vault1:") is None


def test_load_text_legacy_plaintext_passthrough_is_not_decrypted() -> None:
    # un-tagged UTF-8 bytes are returned verbatim (legacy migration path)
    assert vault_crypto.load_text(b'{"k": "v"}') == '{"k": "v"}'


# --------------------------------------------------------------------------- #
# Failure observability — a decrypt failure must be loud, not silent.           #
# --------------------------------------------------------------------------- #


def test_note_decrypt_failure_increments_and_marks_unhealthy() -> None:
    vault_crypto.reset_cache()
    before = vault_crypto.health()["decrypt_failures"]
    vault_crypto.note_decrypt_failure("secrets.json")
    h = vault_crypto.health()
    assert h["decrypt_failures"] == before + 1
    assert h["healthy"] is False


# --------------------------------------------------------------------------- #
# Key location — the master key lives OUTSIDE the data dir (backup-safety).      #
# --------------------------------------------------------------------------- #


def test_default_keyfile_honors_xdg_config_home(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert vault_crypto.default_keyfile() == tmp_path / "akana" / "vault.key"


def test_default_keyfile_falls_back_to_platform_default(monkeypatch) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    kf = vault_crypto.default_keyfile()
    if os.name == "nt":
        # Windows: %APPDATA%\akana, unless a legacy ~/.config key is already present.
        legacy = Path.home() / ".config" / "akana" / "vault.key"
        if legacy.is_file():
            assert kf == legacy
        else:
            appdata = os.environ.get("APPDATA", "").strip()
            base = Path(appdata) / "akana" if appdata else Path.home() / ".config" / "akana"
            assert kf == base / "vault.key"
    else:
        assert kf == Path.home() / ".config" / "akana" / "vault.key"


# --------------------------------------------------------------------------- #
# Keyring I/O wrappers — must never raise; missing backend → None / False.       #
# --------------------------------------------------------------------------- #


def test_keyring_wrappers_roundtrip_via_fake_backend(monkeypatch) -> None:
    import sys
    import types

    store: dict[tuple[str, str], str] = {}
    fake = types.ModuleType("keyring")
    fake.get_password = lambda svc, usr: store.get((svc, usr))  # type: ignore[attr-defined]
    fake.set_password = lambda svc, usr, val: store.__setitem__((svc, usr), val)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", fake)

    assert vault_crypto._keyring_get() is None  # empty backend
    assert vault_crypto._keyring_set(b"a-master-key") is True
    assert vault_crypto._keyring_get() == b"a-master-key"  # round-trips as bytes


def test_keyring_wrappers_swallow_backend_errors(monkeypatch) -> None:
    import sys
    import types

    def _boom(*_a, **_k):
        raise RuntimeError("no secret-service backend")

    fake = types.ModuleType("keyring")
    fake.get_password = _boom  # type: ignore[attr-defined]
    fake.set_password = _boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "keyring", fake)

    # a broken backend must degrade quietly, never propagate
    assert vault_crypto._keyring_get() is None
    assert vault_crypto._keyring_set(b"x") is False
