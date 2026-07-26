"""Windows master-key hardening must never wreck a directory Akana does not own.

``icacls <dir> /inheritance:r`` strips the inheritable ACEs every EXISTING child of
that directory relies on; with a non-inheritable replacement grant those children
are left with an EMPTY DACL and the owner is locked out of their own files (and
``icacls`` on them fails too — only takeown/icacls repair gets them back).
``AKANA_VAULT_KEYFILE`` is a user-chosen path, so the keyfile's parent is routinely
a populated folder (Documents, a drive root, the data dir itself).

Two halves of the guard are pinned here:

1. a parent directory that already existed is NEVER re-ACLed/chmodded — the key is
   protected by the owner-only ACL on the FILE;
2. when we do harden a directory we just created, the grant carries ``(OI)(CI)`` so
   its children keep an owner ACE instead of inheriting nothing.

``_restrict_to_owner`` returns early when ``os.name != "nt"``, so the argv tests
swap the module's ``os`` for a proxy that claims Windows and stub ``subprocess``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from akana_server import vault_crypto


class _OsProxy:
    """Stand-in for the ``os`` module ``vault_crypto`` uses.

    Delegates everything to the real module; optionally claims ``name == "nt"`` so
    the Windows-only hardening branch is exercised on any platform, and records
    ``chmod`` targets so a test can assert which paths had their mode touched.
    """

    def __init__(self, *, name: str | None = None) -> None:
        self._name = name
        self.chmod_paths: list[str] = []

    def __getattr__(self, attr: str):
        return getattr(os, attr)

    @property
    def name(self) -> str:
        return self._name or os.name

    def chmod(self, path, mode, *args, **kwargs):
        self.chmod_paths.append(str(path))
        return os.chmod(path, mode, *args, **kwargs)


class _SubprocessSpy:
    """Captures the icacls argv instead of running it."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        return None


@pytest.fixture
def icacls(monkeypatch: pytest.MonkeyPatch) -> _SubprocessSpy:
    spy = _SubprocessSpy()
    monkeypatch.setattr(vault_crypto, "os", _OsProxy(name="nt"))
    monkeypatch.setattr(vault_crypto, "subprocess", spy)
    monkeypatch.setenv("USERNAME", "tester")
    return spy


def _grant(argv: list[str]) -> str:
    return argv[argv.index("/grant:r") + 1]


# --- the grant must be inheritable for directories ---------------------------


def test_directory_grant_is_inheritable(icacls: _SubprocessSpy, tmp_path: Path) -> None:
    """A directory grant without (OI)(CI) leaves every child with an empty DACL."""
    vault_crypto._restrict_to_owner(tmp_path)
    assert icacls.calls, "icacls was not invoked for a directory"
    grant = _grant(icacls.calls[-1])
    assert "(OI)" in grant and "(CI)" in grant, grant
    assert grant.startswith("tester:")


def test_file_grant_has_no_inheritance_flags(icacls: _SubprocessSpy, tmp_path: Path) -> None:
    """Files have no children — inheritance flags on a file are meaningless/rejected."""
    target = tmp_path / "vault.key"
    target.write_bytes(b"k")
    vault_crypto._restrict_to_owner(target)
    assert _grant(icacls.calls[-1]) == "tester:F"


# --- a pre-existing parent directory is off limits ---------------------------


def test_existing_parent_dir_is_never_hardened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The user's Documents folder must survive minting a key inside it."""
    hardened: list[str] = []
    monkeypatch.setattr(
        vault_crypto, "_restrict_to_owner", lambda p: hardened.append(str(p))
    )
    proxy = _OsProxy()
    monkeypatch.setattr(vault_crypto, "os", proxy)

    documents = tmp_path / "Documents"
    documents.mkdir()
    (documents / "taxes.txt").write_text("mine", encoding="utf-8")
    keyfile = documents / "akana-vault.key"

    vault_crypto._write_keyfile(keyfile, b"master-key")

    assert keyfile.read_bytes() == b"master-key"
    assert str(documents) not in hardened, "pre-existing parent dir was re-ACLed"
    assert str(documents) not in proxy.chmod_paths, "pre-existing parent dir was chmodded"
    # The key itself is still protected (write_private_bytes_atomic hardens the file).
    assert str(keyfile) in hardened


def test_parent_dir_we_created_is_hardened(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The property the hardening exists for is not regressed for OUR own directory."""
    hardened: list[str] = []
    monkeypatch.setattr(
        vault_crypto, "_restrict_to_owner", lambda p: hardened.append(str(p))
    )
    proxy = _OsProxy()
    monkeypatch.setattr(vault_crypto, "os", proxy)

    parent = tmp_path / "akana" / "config"  # neither level exists yet
    keyfile = parent / "vault.key"

    vault_crypto._write_keyfile(keyfile, b"master-key")

    assert str(parent) in hardened
    assert str(parent) in proxy.chmod_paths
    assert str(keyfile) in hardened
