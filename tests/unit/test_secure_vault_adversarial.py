"""Adversarial probes for SecureVault's trust boundaries.

The vault stores real credentials on disk under ``credentials/<namespace>/<profile>/``
and ``vault/keys.json``. The load-bearing guard is the namespace/profile/key
**charset validators** — the only thing standing between a caller-supplied name
and an arbitrary filesystem path.

Each test asserts a security invariant (traversal rejected, corrupt state degrades
to empty rather than crashing/leaking). A failure here is a real containment or
availability bug, not a style nit.
"""

from __future__ import annotations

import pytest

from akana_server import vault_crypto
from akana_server.secure_vault import (
    credential_dir,
    credentials_root,
    delete_field,
    delete_profile,
    delete_scalar,
    get_scalar,
    list_profiles,
    load_scalars,
    profile_status,
    resolve_data_dir,
    set_fields,
    set_scalar,
)


# --------------------------------------------------------------------------- #
# Path-traversal / charset guard — malicious names must NEVER build a path.    #
# --------------------------------------------------------------------------- #


_MALICIOUS_NAMES = [
    "..",
    "../etc",
    "../../etc/passwd",
    "a/b",            # path separator
    "a\\b",           # windows separator
    "WhatsApp",       # uppercase start (regex requires lowercase)
    "1abc",           # digit start
    "with space",
    ".hidden",        # leading dot
    "x" * 65,         # over the 64-char cap
    "null\x00byte",   # NUL injection
    "",               # empty
]


@pytest.mark.parametrize("bad", _MALICIOUS_NAMES)
def test_malicious_namespace_rejected(tmp_path, bad: str) -> None:
    with pytest.raises(ValueError):
        credential_dir(tmp_path, bad, "default")


@pytest.mark.parametrize("bad", _MALICIOUS_NAMES)
def test_malicious_profile_rejected(tmp_path, bad: str) -> None:
    # Empty profile is special: it DEFAULTS to "default" by design, so it is the
    # one input in the list that must NOT raise — assert the documented default.
    if bad == "":
        path = credential_dir(tmp_path, "ok", bad)
        assert path.name == "default"
        return
    with pytest.raises(ValueError):
        credential_dir(tmp_path, "ok", bad)


def test_traversal_blocked_across_every_public_entrypoint(tmp_path) -> None:
    """Every function that accepts a namespace must validate it — no back door."""
    evil = "../../../etc"
    with pytest.raises(ValueError):
        profile_status(tmp_path, evil)
    with pytest.raises(ValueError):
        list_profiles(tmp_path, evil)
    with pytest.raises(ValueError):
        set_fields(tmp_path, evil, {"u": "v"})
    with pytest.raises(ValueError):
        delete_profile(tmp_path, evil)
    with pytest.raises(ValueError):
        delete_field(tmp_path, evil, "u")


def test_valid_name_stays_within_credentials_root(tmp_path) -> None:
    """A legitimate namespace/profile resolves strictly under credentials_root."""
    root = credentials_root(tmp_path).resolve()
    path = credential_dir(tmp_path, "whatsapp", "default").resolve()
    assert str(path).startswith(str(root))
    assert ".." not in path.parts


def test_trailing_whitespace_in_name_is_normalized_not_smuggled(tmp_path) -> None:
    """Leading/trailing whitespace (incl. a trailing newline) is stripped to the
    base name *before* the charset check, so ``"abc\\n"`` collapses to ``"abc"`` —
    there is no way to create a distinct ``"abc\\n"`` directory beside ``"abc"``.

    (The validators ``.strip()`` first, which is why the ``$``-vs-``\\Z`` regex
    quirk is unreachable here: no trailing newline ever reaches the anchor.)
    """
    assert credential_dir(tmp_path, "abc\n", "default").parent.name == "abc"
    assert credential_dir(tmp_path, "  abc  ", "default").parent.name == "abc"


def test_embedded_whitespace_in_name_is_rejected(tmp_path) -> None:
    """Internal whitespace survives ``strip()`` and must fail the charset guard —
    no smuggling a newline/tab/space into the middle of a path segment."""
    for bad in ("ab\ncd", "ab\tcd", "ab cd"):
        with pytest.raises(ValueError):
            credential_dir(tmp_path, bad, "default")


def test_trailing_newline_secret_key_is_normalized(tmp_path) -> None:
    """A scalar key with a trailing newline is stored under its trimmed name, and
    the raw ``"apikey\\n"`` form never appears in the keyfile."""
    set_scalar(tmp_path, "apikey\n", "secretvalue123")
    assert get_scalar(tmp_path, "apikey") == "secretvalue123"
    keys = load_scalars(tmp_path)
    assert "apikey" in keys
    assert "apikey\n" not in keys


# --------------------------------------------------------------------------- #
# Corrupt / malformed keyfile — degrade to empty, never crash, never leak.      #
# --------------------------------------------------------------------------- #


def _write_keyfile(tmp_path, raw: bytes) -> None:
    keys = tmp_path / "vault" / "keys.json"
    keys.parent.mkdir(parents=True, exist_ok=True)
    keys.write_bytes(raw)


def test_keyfile_invalid_utf8_degrades_to_empty(tmp_path) -> None:
    # Not encrypted, not valid UTF-8 → load_text returns None → {} (no crash).
    _write_keyfile(tmp_path, b"\xff\xfe\xff\x00\x01")
    assert load_scalars(tmp_path) == {}


def test_keyfile_plaintext_non_json_degrades_to_empty(tmp_path) -> None:
    # Legacy-plaintext path: decodes as text, then json.loads raises → {}.
    _write_keyfile(tmp_path, b"this is not json at all {{{")
    assert load_scalars(tmp_path) == {}


def test_keyfile_encrypted_non_dict_degrades_to_empty(tmp_path) -> None:
    # A properly-encrypted blob whose JSON is a list (not an object) → {}.
    _write_keyfile(tmp_path, vault_crypto.encrypt_str('["a", "b", 1]'))
    assert load_scalars(tmp_path) == {}


def test_keyfile_encrypted_dict_with_junk_values_is_filtered(tmp_path) -> None:
    # Only str→str non-empty entries survive; junk values are dropped, not fatal.
    import json

    blob = vault_crypto.encrypt_str(
        json.dumps({"good": "value123", "blank": "", "num": 5, "nested": {"x": 1}})
    )
    _write_keyfile(tmp_path, blob)
    assert load_scalars(tmp_path) == {"good": "value123"}


# --------------------------------------------------------------------------- #
# Boundary raises / honest no-ops on empty keys & fields.                       #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_get_scalar_blank_key_returns_none(tmp_path, blank) -> None:
    assert get_scalar(tmp_path, blank) is None  # type: ignore[arg-type]


@pytest.mark.parametrize("blank", ["", "   "])
def test_set_scalar_blank_key_raises(tmp_path, blank: str) -> None:
    with pytest.raises(ValueError):
        set_scalar(tmp_path, blank, "value123")


@pytest.mark.parametrize("blank", ["", "   "])
def test_delete_scalar_blank_key_raises(tmp_path, blank: str) -> None:
    with pytest.raises(ValueError):
        delete_scalar(tmp_path, blank)


def test_delete_field_blank_field_raises(tmp_path) -> None:
    with pytest.raises(ValueError):
        delete_field(tmp_path, "reddit", "")


# --------------------------------------------------------------------------- #
# resolve_data_dir — explicit arg > AKANA_DATA_DIR env > ~/.akana default.      #
# --------------------------------------------------------------------------- #


def test_resolve_data_dir_explicit_arg_wins(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", "/should/not/win")
    assert resolve_data_dir(tmp_path) == tmp_path


def test_resolve_data_dir_env_fallback(monkeypatch) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", "/tmp/akana-env-probe")
    from pathlib import Path

    assert resolve_data_dir(None) == Path("/tmp/akana-env-probe")


def test_resolve_data_dir_default_is_home_akana(monkeypatch) -> None:
    from pathlib import Path

    monkeypatch.delenv("AKANA_DATA_DIR", raising=False)
    assert resolve_data_dir(None) == Path.home() / ".akana"
