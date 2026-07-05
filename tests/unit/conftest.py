"""Shared isolation for unit tests.

The process-global circuit-breaker reset now lives in the suite-wide
``tests/conftest.py`` (``_reset_network_registry``) so that integration and e2e
tests get the same isolation — it used to live here and only protected unit
tests. This file keeps the vault-key isolation, which is unit-scoped.
"""

from __future__ import annotations

import os

import pytest

from akana_server import vault_crypto

_VAULT_ENV = ("AKANA_VAULT_KEYFILE", "AKANA_VAULT_KEY", "AKANA_VAULT_KEYRING")


@pytest.fixture(autouse=True)
def _isolated_vault_key(tmp_path_factory):
    """Each test gets a throwaway vault master key.

    Keeps encryption deterministic per-test and prevents the suite from
    touching (or generating) the developer's real ``~/.config`` keyfile.

    Env is set via ``os.environ`` (not ``monkeypatch``) on purpose: some tests
    call ``monkeypatch.undo()`` mid-body, which would otherwise revert our
    keyfile override and make later reads decrypt with the wrong key.
    """
    keyfile = tmp_path_factory.mktemp("vault-key") / "vault.key"
    prev = {k: os.environ.get(k) for k in _VAULT_ENV}
    os.environ["AKANA_VAULT_KEYFILE"] = str(keyfile)
    os.environ.pop("AKANA_VAULT_KEY", None)
    os.environ.pop("AKANA_VAULT_KEYRING", None)
    vault_crypto.reset_cache()
    yield
    for key, value in prev.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    vault_crypto.reset_cache()
