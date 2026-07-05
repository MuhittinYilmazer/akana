"""SecureVault F0 — credential dirs, scalar bridge, permissions, migration."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from akana_server.secure_vault import (
    FIELDS_FILE,
    audit_access,
    credential_dir,
    delete_field,
    delete_profile,
    delete_scalar,
    get_scalar,
    inventory,
    list_namespaces,
    load_fields,
    load_scalars,
    migrate_legacy,
    profile_status,
    set_fields,
    set_scalar,
    set_scalars,
    vault_summary,
)
from akana_server.secret_store import ALLOWED_KEYS, get_secret, set_secrets


def test_credential_dir_creates_private(tmp_path: Path) -> None:
    path = credential_dir(tmp_path, "whatsapp", "default", create=True)
    assert path.is_dir()
    # Unix 0700 mode bits don't map onto Windows ACLs; gate the bit-check on POSIX
    # while still asserting the private dir is created on every OS.
    if os.name != "nt":
        assert oct(path.stat().st_mode & 0o777) == "0o700"
    assert path == tmp_path / "credentials" / "whatsapp" / "default"


def test_profile_status_empty(tmp_path: Path) -> None:
    st = profile_status(tmp_path, "whatsapp")
    assert st["exists"] is False
    assert st["populated"] is False
    assert st["file_count"] == 0


def test_scalar_delegates_to_secret_store(tmp_path: Path) -> None:
    set_secrets(tmp_path, {"telegram_bot_token": "tok_12345678"})
    assert get_scalar(tmp_path, "telegram_bot_token") == "tok_12345678"
    assert get_secret(tmp_path, "telegram_bot_token") == "tok_12345678"


def test_extended_scalar_in_vault_keys(tmp_path: Path) -> None:
    # "Extended" = any scalar NOT in secret_store.ALLOWED_KEYS. System provider
    # credentials (cursor/claude/gemini/openai api keys) route to secrets.json via
    # the credentials API; everything else lands in the encrypted vault keyfile.
    # Guard the premise: if this example is ever promoted to a system key (as
    # gemini_api_key and openai_api_key were), fail loudly HERE instead of
    # cryptically at is_file(). Use a provider key not yet promoted to a system key.
    extended_key = "mistral_api_key"
    assert extended_key not in ALLOWED_KEYS
    set_scalar(tmp_path, extended_key, "gkey_abcdefgh")
    assert get_scalar(tmp_path, extended_key) == "gkey_abcdefgh"
    keys_path = tmp_path / "vault" / "keys.json"
    assert keys_path.is_file()
    # 0600 bit-check is POSIX-only (Windows ACLs); encryption-at-rest below is asserted
    # on every OS — that's the security-critical invariant, not the Unix mode bits.
    if os.name != "nt":
        assert oct(keys_path.stat().st_mode & 0o777) == "0o600"
    # Encrypted at rest — the raw value is never written in plaintext.
    blob = keys_path.read_bytes()
    assert blob.startswith(b"vault1:")
    assert b"gkey_abcdefgh" not in blob


def test_migrate_legacy_copies_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import akana_server.secure_vault as sv

    legacy = tmp_path / "legacy" / "whatsapp" / "default"
    legacy.mkdir(parents=True)
    (legacy / "creds.json").write_text('{"me": true}', encoding="utf-8")

    monkeypatch.setitem(
        sv.LEGACY_CREDENTIAL_DIRS,
        "whatsapp",
        [(legacy, "whatsapp", "default")],
    )

    result = migrate_legacy(tmp_path, "whatsapp")
    assert result["migrated"] is True
    dest = credential_dir(tmp_path, "whatsapp", "default")
    assert (dest / "creds.json").read_text(encoding="utf-8") == '{"me": true}'
    assert migrate_legacy(tmp_path, "whatsapp")["migrated"] is False


def test_vault_summary_lists_namespaces(tmp_path: Path) -> None:
    credential_dir(tmp_path, "whatsapp", "default", create=True)
    (credential_dir(tmp_path, "whatsapp", "default") / "a.json").write_text("{}", encoding="utf-8")
    summary = vault_summary(tmp_path)
    assert summary["namespaces"][0]["namespace"] == "whatsapp"
    assert summary["namespaces"][0]["profiles"][0]["populated"] is True
    assert list_namespaces(tmp_path) == ["whatsapp"]


def test_inventory_unions_keyfile_and_system_scalars(tmp_path: Path) -> None:
    # vault_list must surface BOTH stores vault_get can read: the keyfile (extended
    # keys) and set system provider keys (secret_store) — otherwise discovery hides
    # keys the model can still fetch.
    set_scalar(tmp_path, "github_token", "ghp")  # keyfile
    set_scalar(tmp_path, "gemini_api_key", "gkey_sys")  # system (secret_store)
    inv = inventory(tmp_path)
    assert inv["scalars"] == ["gemini_api_key", "github_token"]
    # An UNSET system key never appears (names-only, set-only).
    assert "openai_api_key" not in inv["scalars"]


def test_audit_access_appends_jsonl(tmp_path: Path) -> None:
    audit_access(tmp_path, {"action": "test", "key": "x"})
    path = tmp_path / "audit" / "vault_access.jsonl"
    assert path.is_file()
    row = json.loads(path.read_text(encoding="utf-8").strip())
    assert row["action"] == "test"


def test_audit_access_best_effort_when_audit_dir_blocked(tmp_path: Path) -> None:
    """VAULT-4: a stray regular file named ``audit`` makes the audit-dir mkdir fail, but
    audit_access must SWALLOW it (never raise) so a succeeded mutation isn't reported as a
    failure / HTTP 500."""
    (tmp_path / "audit").write_text("not a dir", encoding="utf-8")
    # Must not raise even though _ensure_private_dir(dd/'audit') can't create the dir.
    audit_access(tmp_path, {"action": "probe", "key": "x"})


def test_set_scalar_succeeds_even_when_audit_blocked(tmp_path: Path) -> None:
    """VAULT-4: the mutation is the source of truth — a blocked audit path must not turn a
    stored secret into a caller-visible failure."""
    (tmp_path / "audit").write_text("blocker", encoding="utf-8")
    set_scalar(tmp_path, "github_token", "ghp_persisted")  # audit_access runs after the write
    assert get_scalar(tmp_path, "github_token") == "ghp_persisted"


def test_ensure_private_dir_does_not_walk_past_data_dir(tmp_path: Path) -> None:
    """VAULT-6: the ancestor-hardening walk must STOP at the data dir. A data dir nested under
    an UNRELATED ancestor literally named 'vault'/'credentials' must NOT get chmod 0700 (the
    old Path==str anchor test never broke and walked to the fs root)."""
    if os.name == "nt":
        pytest.skip("chmod bits are a near-no-op on Windows")
    # Unrelated ancestor named exactly 'vault', OUTSIDE the data dir tree.
    outer_vault = tmp_path / "vault"
    outer_vault.mkdir()
    os.chmod(outer_vault, 0o755)
    data_dir = outer_vault / "akana"
    data_dir.mkdir()
    os.chmod(data_dir, 0o755)
    # A vault write under this data dir hardens <data_dir>/vault, NOT the outer 'vault'.
    set_scalar(data_dir, "github_token", "ghp_x")
    assert oct((data_dir / "vault").stat().st_mode & 0o777) == "0o700"
    # The unrelated ancestor named 'vault' was left untouched.
    assert oct(outer_vault.stat().st_mode & 0o777) == "0o755"


def test_fields_roundtrip_encrypted_at_rest(tmp_path: Path) -> None:
    set_fields(tmp_path, "reddit", {"username": "u_demo", "password": "p_secret123"})
    assert load_fields(tmp_path, "reddit", audit=False) == {
        "username": "u_demo",
        "password": "p_secret123",
    }
    blob = (credential_dir(tmp_path, "reddit", "default") / FIELDS_FILE).read_bytes()
    assert blob.startswith(b"vault1:")
    assert b"p_secret123" not in blob


def test_set_fields_empty_value_clears(tmp_path: Path) -> None:
    set_fields(tmp_path, "reddit", {"username": "u_demo", "password": "p_secret123"})
    set_fields(tmp_path, "reddit", {"password": ""})
    assert load_fields(tmp_path, "reddit", audit=False) == {"username": "u_demo"}


def test_delete_profile_removes_dir_and_prunes_namespace(tmp_path: Path) -> None:
    set_fields(tmp_path, "reddit", {"username": "u_demo", "password": "p_secret123"})
    path = credential_dir(tmp_path, "reddit", "default")
    assert path.is_dir()
    result = delete_profile(tmp_path, "reddit", "default")
    assert result == {"namespace": "reddit", "profile": "default", "removed": True}
    # real delete: the profile dir is gone and the orphaned namespace dir is pruned
    assert not path.exists()
    assert not path.parent.exists()
    assert load_fields(tmp_path, "reddit", audit=False) == {}
    # audit captures the action
    audit = (tmp_path / "audit" / "vault_access.jsonl").read_text(encoding="utf-8")
    assert "delete_profile" in audit


def test_delete_profile_missing_is_idempotent(tmp_path: Path) -> None:
    result = delete_profile(tmp_path, "ghost", "default")
    assert result == {"namespace": "ghost", "profile": "default", "removed": False}


def test_delete_profile_keeps_sibling_profiles(tmp_path: Path) -> None:
    set_fields(tmp_path, "reddit", {"username": "u_main"}, "default")
    set_fields(tmp_path, "reddit", {"username": "u_alt"}, "alt")
    delete_profile(tmp_path, "reddit", "default")
    # namespace dir survives because the 'alt' profile is still there
    assert load_fields(tmp_path, "reddit", "alt", audit=False) == {"username": "u_alt"}
    assert load_fields(tmp_path, "reddit", "default", audit=False) == {}


def test_delete_scalar_reports_whether_removed(tmp_path: Path) -> None:
    set_scalar(tmp_path, "mistral_api_key", "gkey_abcdefgh")
    assert delete_scalar(tmp_path, "mistral_api_key") is True
    assert get_scalar(tmp_path, "mistral_api_key") is None
    # a second delete (now absent) is an honest no-op
    assert delete_scalar(tmp_path, "mistral_api_key") is False
    # audit records the removed flag
    audit = (tmp_path / "audit" / "vault_access.jsonl").read_text(encoding="utf-8")
    assert "delete_scalar" in audit


def test_delete_scalar_handles_system_key(tmp_path: Path) -> None:
    # System provider keys live in secret_store, not the keyfile, but delete still
    # reports honestly via that branch.
    set_scalar(tmp_path, "gemini_api_key", "gkey_system_123")
    assert delete_scalar(tmp_path, "gemini_api_key") is True
    assert delete_scalar(tmp_path, "gemini_api_key") is False


def test_delete_field_reports_whether_removed(tmp_path: Path) -> None:
    set_fields(tmp_path, "reddit", {"username": "u_demo", "password": "p_secret123"})
    assert delete_field(tmp_path, "reddit", "password") is True
    assert load_fields(tmp_path, "reddit", audit=False) == {"username": "u_demo"}
    # clearing an absent field is an honest no-op that keeps siblings intact
    assert delete_field(tmp_path, "reddit", "password") is False
    assert load_fields(tmp_path, "reddit", audit=False) == {"username": "u_demo"}


def test_delete_field_missing_profile_is_false(tmp_path: Path) -> None:
    assert delete_field(tmp_path, "ghost", "password") is False


def test_load_fields_audits_consumer(tmp_path: Path) -> None:
    set_fields(tmp_path, "reddit", {"username": "u_demo"})
    load_fields(tmp_path, "reddit", consumer="reddit-pack", audit=True)
    audit = (tmp_path / "audit" / "vault_access.jsonl").read_text(encoding="utf-8")
    assert "load_fields" in audit
    assert "reddit-pack" in audit
    # The raw value must never land in the audit log.
    assert "u_demo" not in audit


def test_set_scalars_merge_and_clear(tmp_path: Path) -> None:
    set_scalars(tmp_path, {"a_key": "aaaa1111", "b_key": "bbbb2222"})
    set_scalars(tmp_path, {"a_key": ""})
    assert load_scalars(tmp_path) == {"b_key": "bbbb2222"}


def test_set_scalars_rejects_bad_key_oversize_and_flood(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        set_scalars(tmp_path, {"bad key!": "value123"})  # space + punctuation
    with pytest.raises(ValueError):
        set_scalars(tmp_path, {"toolong": "v" * 9000})  # > MAX_SECRET_VALUE_LEN
    with pytest.raises(ValueError):
        set_scalars(tmp_path, {f"k{i}": "v" for i in range(100)})  # > MAX_KEYS_PER_WRITE
    # Nothing was persisted by the rejected writes.
    assert load_scalars(tmp_path) == {}


def test_set_fields_rejects_bad_key(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        set_fields(tmp_path, "reddit", {"bad/key": "x"})


def test_clearing_tolerates_any_key_name(tmp_path: Path) -> None:
    # A clear (empty value) must never raise on key-name validation.
    set_scalars(tmp_path, {"weird key!": ""})
    assert load_scalars(tmp_path) == {}


def test_wrong_key_set_scalar_refuses_and_keeps_keyfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writing a keyfile scalar with the wrong master key does NOT OVERWRITE existing data.

    set_scalar/set_scalars read-modify-write vault/keys.json; starting from an empty
    base on an undecryptable blob would have deleted all scalars. It now raises a
    RuntimeError inside the writer lock and leaves the file untouched."""
    from cryptography.fernet import Fernet
    from akana_server import vault_crypto

    set_scalar(tmp_path, "mistral_api_key", "gkey_abcdefgh")
    keys_path = tmp_path / "vault" / "keys.json"
    original = keys_path.read_bytes()
    assert original.startswith(b"vault1:")

    monkeypatch.setenv("AKANA_VAULT_KEY", Fernet.generate_key().decode("utf-8"))
    vault_crypto.reset_cache()

    with pytest.raises(RuntimeError):
        set_scalar(tmp_path, "anthropic_api_key", "akey_99998888")
    assert keys_path.read_bytes() == original  # no scalar was lost

    # set_scalars (merge writer) has the same protection.
    with pytest.raises(RuntimeError):
        set_scalars(tmp_path, {"another_key": "vvvv4444"})
    assert keys_path.read_bytes() == original

    monkeypatch.delenv("AKANA_VAULT_KEY", raising=False)
    vault_crypto.reset_cache()
    assert load_scalars(tmp_path) == {"mistral_api_key": "gkey_abcdefgh"}


def test_wrong_key_set_fields_refuses_and_keeps_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writing a profile field with the wrong master key does NOT OVERWRITE existing fields."""
    from cryptography.fernet import Fernet
    from akana_server import vault_crypto

    set_fields(tmp_path, "reddit", {"username": "u_demo", "password": "p_secret123"})
    fields_path = credential_dir(tmp_path, "reddit", "default") / FIELDS_FILE
    original = fields_path.read_bytes()
    assert original.startswith(b"vault1:")

    monkeypatch.setenv("AKANA_VAULT_KEY", Fernet.generate_key().decode("utf-8"))
    vault_crypto.reset_cache()

    with pytest.raises(RuntimeError):
        set_fields(tmp_path, "reddit", {"password": "p_overwrite999"})
    assert fields_path.read_bytes() == original  # the fields remained untouched

    monkeypatch.delenv("AKANA_VAULT_KEY", raising=False)
    vault_crypto.reset_cache()
    assert load_fields(tmp_path, "reddit", audit=False) == {
        "username": "u_demo",
        "password": "p_secret123",
    }


# --------------------------------------------------------------------------- #
# Store unification (platform:arch:1) — one authority per scalar, no dual store. #
# --------------------------------------------------------------------------- #


def _write_legacy_keyfile(tmp_path: Path, data: dict) -> Path:
    """Force-write raw entries into vault/keys.json (simulates the OLD set_scalars that
    wrote every name — including ALLOWED_KEYS — into the keyfile)."""
    from akana_server import vault_crypto

    keys_path = tmp_path / "vault" / "keys.json"
    keys_path.parent.mkdir(parents=True, exist_ok=True)
    keys_path.write_bytes(vault_crypto.encrypt_str(json.dumps(data)))
    return keys_path


def test_system_key_routes_to_secret_store_not_keyfile(tmp_path: Path) -> None:
    # set_scalar on an ALLOWED_KEYS name goes to secrets.json (the provider authority),
    # never the keyfile — so vault_get returns exactly what the provider reads.
    set_scalar(tmp_path, "openai_api_key", "sk-real-openai-value")
    assert get_secret(tmp_path, "openai_api_key") == "sk-real-openai-value"
    assert get_scalar(tmp_path, "openai_api_key") == "sk-real-openai-value"
    # It is NOT in the keyfile view.
    assert "openai_api_key" not in load_scalars(tmp_path)


def test_get_scalar_ignores_legacy_keyfile_copy_of_system_key(tmp_path: Path) -> None:
    # A legacy keyfile copy of a system key must NEVER shadow secret_store: reads resolve
    # from the authority only, so the model can't be handed a stale value the provider
    # doesn't use. (secret_store is empty here → None, despite the keyfile copy.)
    _write_legacy_keyfile(tmp_path, {"openai_api_key": "STALE_KEYFILE"})
    assert get_scalar(tmp_path, "openai_api_key") is None
    # …and it is invisible to the keyfile listing + inventory (no discover/read split).
    assert "openai_api_key" not in load_scalars(tmp_path)
    assert "openai_api_key" not in inventory(tmp_path)["scalars"]


def test_set_scalar_migrates_stray_system_key_out_of_keyfile(tmp_path: Path) -> None:
    # Migrate-on-write: writing the real value to the authority purges the legacy keyfile
    # copy, so the two stores can never disagree afterwards.
    keys_path = _write_legacy_keyfile(
        tmp_path, {"openai_api_key": "STALE", "github_token": "ghp_keep"}
    )
    set_scalar(tmp_path, "openai_api_key", "sk-fresh")
    from akana_server import vault_crypto

    remaining = json.loads(vault_crypto.load_text(keys_path.read_bytes()))
    assert "openai_api_key" not in remaining  # purged
    assert remaining["github_token"] == "ghp_keep"  # extended key untouched
    assert get_scalar(tmp_path, "openai_api_key") == "sk-fresh"


def test_set_scalars_routes_mixed_patch_by_partition(tmp_path: Path) -> None:
    # A raw multi-key write splits by name: system keys → secret_store, others → keyfile.
    # This is what stops routes/vault.py's raw PUT from creating a second source of truth.
    set_scalars(tmp_path, {"openai_api_key": "sk-sys", "github_token": "ghp_ext"})
    assert get_secret(tmp_path, "openai_api_key") == "sk-sys"  # system → secret_store
    assert load_scalars(tmp_path) == {"github_token": "ghp_ext"}  # extended → keyfile only
    assert get_scalar(tmp_path, "openai_api_key") == "sk-sys"


def test_delete_scalar_targets_single_store_no_phantom_removal(tmp_path: Path) -> None:
    # A legacy keyfile copy of an already-absent system key is swept, but that housekeeping
    # must not fake a removal: with nothing in secret_store, delete honestly reports False.
    _write_legacy_keyfile(tmp_path, {"openai_api_key": "STALE"})
    assert delete_scalar(tmp_path, "openai_api_key") is False
    # …and the stray copy is gone (swept), so it can't resurface.
    assert "openai_api_key" not in load_scalars(tmp_path)


def test_audit_log_rotates_at_cap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import akana_server.secure_vault as sv

    monkeypatch.setattr(sv, "AUDIT_MAX_BYTES", 200)
    for i in range(60):
        audit_access(tmp_path, {"action": "probe", "i": i})
    audit_dir = tmp_path / "audit"
    assert (audit_dir / "vault_access.jsonl").is_file()
    assert (audit_dir / "vault_access.jsonl.1").is_file()
