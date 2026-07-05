"""Bug-hunt regression: vault write path — off-loop offload, master-key race, and
undecryptable-error mapping.

Findings (``akana-bughunt-providers-files-vault.md`` — batch "Vault write path:
event-loop blocking, cross-process races, and error mapping"):

* HIGH: the mutating vault ROUTES called the synchronous ``secure_vault`` functions
  directly on the asyncio event loop while those functions hold a BLOCKING
  cross-process file lock (~30s on Windows). The fix offloads each blocking call via
  ``asyncio.to_thread`` (the idiom :mod:`akana_server.api.routes.uploads` uses).
* MED: ``vault_crypto.get_master_key`` minted + wrote the keyfile under only the
  per-process ``threading`` lock, so two PROCESSES with the keyfile absent generated
  divergent keys (silent secret loss). The fix takes the cross-process ``file_lock``
  and DOUBLE-CHECKS the keyfile inside it, adopting a peer's freshly written key.
* LOW: ``assert_writable`` raises ``VaultUndecryptableError`` (a ``RuntimeError``);
  uncaught in the write routes it surfaced as an opaque HTTP 500. The fix maps it to
  a clean 409 ``VAULT_UNDECRYPTABLE``.

All tests are hermetic: ``tmp_path`` data/key dirs, no real network, and the master
key is supplied/rotated via env so nothing touches the developer's real keyfile.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from akana_server import vault_crypto
from akana_server.api.routes import vault as vault_route
from akana_server.config import load_settings

SCALARS = "/api/v1/system/vault/scalars"
FIELDS = "/api/v1/system/vault/reddit/default/fields"

DEMO = "demo_scalar_value"  # hint → …alue


def _make_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FastAPI:
    """Bare app with the vault router + real Settings, rooted at a tmp data dir.

    Mirrors ``tests/unit/test_vault_routes.py``: no token (local, non-proxied
    requests pass the bearer gate) and an isolated ``AKANA_DATA_DIR``.
    """
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    app = FastAPI()
    app.include_router(vault_route.router, prefix="/api/v1")
    app.state.settings = load_settings()
    return app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    with TestClient(_make_app(monkeypatch, tmp_path)) as c:
        yield c


@pytest.fixture
def to_thread_spy(monkeypatch: pytest.MonkeyPatch):
    """Spy on ``asyncio.to_thread`` AS SEEN BY THE VAULT ROUTE MODULE.

    The route does ``import asyncio``; patching ``vault_route.asyncio.to_thread``
    intercepts the exact call site. The spy still awaits the real implementation so
    the endpoint behaves normally — it merely records the callable it offloads.
    """
    real_to_thread = asyncio.to_thread
    calls: list[object] = []

    async def _spy(func, /, *args, **kwargs):
        calls.append(func)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(vault_route.asyncio, "to_thread", _spy)
    return calls


# ── (a) write routes offload blocking secure_vault work off the event loop ──────


def test_put_scalars_offloads_blocking_set_scalars(
    client: TestClient, to_thread_spy: list[object]
) -> None:
    r = client.put(SCALARS, json={"scalars": {"gemini_api_key": DEMO}})
    assert r.status_code == 200
    # The blocking, lock-holding set_scalars must be dispatched via asyncio.to_thread
    # (the function object imported into the route module), never called inline.
    assert vault_route.set_scalars in to_thread_spy


def test_put_fields_offloads_blocking_set_fields(
    client: TestClient, to_thread_spy: list[object]
) -> None:
    r = client.put(FIELDS, json={"fields": {"username": "alice_demo"}})
    assert r.status_code == 200
    assert vault_route.set_fields in to_thread_spy


def test_delete_scalar_offloads_off_loop(
    client: TestClient, to_thread_spy: list[object]
) -> None:
    # A NON-ALLOWED (keyfile) scalar → the delete routes to the keyfile set_scalars.
    # System credentials in ALLOWED_KEYS now dual-route to set_scalar (secrets.json);
    # that path + its offload are covered in test_bh2_vault_scalars.py.
    r = client.delete(f"{SCALARS}/my_note")
    assert r.status_code == 200
    assert vault_route.set_scalars in to_thread_spy


def test_delete_profile_offloads_off_loop(
    client: TestClient, to_thread_spy: list[object]
) -> None:
    r = client.delete("/api/v1/system/vault/reddit/default")
    assert r.status_code == 200
    assert vault_route.delete_profile in to_thread_spy


def test_write_route_does_not_block_loop_when_offload_neutralised(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If ``asyncio.to_thread`` is neutralised, the sync write must NOT run inline.

    Replacing ``to_thread`` with a no-op that never calls its target proves the
    handler routes its blocking work exclusively through it: with the off-loop hop
    stubbed out, nothing is persisted. A regression that called ``set_scalars``
    directly on the loop would write ``keys.json`` and fail this test.
    """
    data_dir = Path(os.environ["AKANA_DATA_DIR"])

    async def _swallow(func, /, *args, **kwargs):
        return None  # deliberately do NOT invoke func — simulate the off-loop boundary

    monkeypatch.setattr(vault_route.asyncio, "to_thread", _swallow)

    # to_thread swallowed → the handler gets None back and may 500 while masking it;
    # irrelevant here — we only assert the blocking write never ran on the loop.
    no_raise = TestClient(client.app, raise_server_exceptions=False)
    no_raise.put(SCALARS, json={"scalars": {"gemini_api_key": DEMO}})

    assert not (data_dir / "vault" / "keys.json").exists()


# ── (b) get_master_key is idempotent/consistent when the keyfile already exists ──


def test_get_or_create_keyfile_adopts_existing_key_inside_lock(
    tmp_path: Path,
) -> None:
    """The double-checked helper must ADOPT an existing keyfile, never re-mint.

    Simulates the race resolution: a peer already wrote the keyfile before we took
    the lock. ``_get_or_create_keyfile`` must return that on-disk key and leave the
    file byte-for-byte unchanged (no second Fernet.generate_key clobbering it).
    """
    keyfile = tmp_path / "vault.key"
    peer_key = Fernet.generate_key()
    vault_crypto.write_private_bytes_atomic(keyfile, peer_key)
    before = keyfile.read_bytes()

    got = vault_crypto._get_or_create_keyfile(keyfile)

    assert got == peer_key
    assert keyfile.read_bytes() == before  # existing key untouched (no re-mint)


def test_get_or_create_keyfile_mints_once_then_is_stable(tmp_path: Path) -> None:
    """First call mints + persists; every later call returns that SAME key."""
    keyfile = tmp_path / "nested" / "vault.key"
    first = vault_crypto._get_or_create_keyfile(keyfile)
    assert keyfile.is_file()
    on_disk = keyfile.read_bytes().strip()
    assert first == on_disk
    # Idempotent: a second call adopts the persisted key rather than minting anew.
    assert vault_crypto._get_or_create_keyfile(keyfile) == first
    assert keyfile.read_bytes().strip() == on_disk


def test_get_master_key_consistent_when_keyfile_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated resolution (even across a cache reset) yields ONE stable key.

    A cache reset makes the second ``get_master_key`` re-read from disk exactly like
    a second PROCESS would — it must load the persisted key, not mint a divergent one.
    """
    keyfile = tmp_path / "vault.key"
    monkeypatch.delenv("AKANA_VAULT_KEY", raising=False)
    monkeypatch.delenv("AKANA_VAULT_KEYRING", raising=False)
    monkeypatch.setenv("AKANA_VAULT_KEYFILE", str(keyfile))

    vault_crypto.reset_cache()
    k1 = vault_crypto.get_master_key()
    assert keyfile.is_file()

    vault_crypto.reset_cache()  # forget the in-process cache → re-read from disk
    k2 = vault_crypto.get_master_key()

    assert k1 == k2
    vault_crypto.reset_cache()


def test_concurrent_get_master_key_threads_converge_on_one_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent first-use resolvers must all end up with the SAME persisted key.

    The cross-process ``file_lock`` (an OS lock) also serialises threads, and the
    double-checked re-read means only the first holder mints; the rest adopt. Every
    caller must therefore agree with the single key on disk — the invariant the race
    used to break (divergent keys / silent secret loss).
    """
    import threading

    keyfile = tmp_path / "vault.key"
    monkeypatch.delenv("AKANA_VAULT_KEY", raising=False)
    monkeypatch.delenv("AKANA_VAULT_KEYRING", raising=False)
    monkeypatch.setenv("AKANA_VAULT_KEYFILE", str(keyfile))
    vault_crypto.reset_cache()

    results: list[bytes] = []
    lock = threading.Lock()
    barrier = threading.Barrier(6)

    def _resolve() -> None:
        barrier.wait()  # maximise overlap on the mint path
        # Bypass the shared in-process cache so each thread exercises the on-disk
        # resolve/generate path (the cross-process race the fix guards).
        key = vault_crypto._get_or_create_keyfile(keyfile)
        with lock:
            results.append(key)

    threads = [threading.Thread(target=_resolve) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 6
    assert len(set(results)) == 1  # all threads agree — no divergent keys
    assert results[0] == keyfile.read_bytes().strip()  # ...and it is what is on disk
    vault_crypto.reset_cache()


# ── (c) VaultUndecryptableError from a write route maps to 4xx/503, NOT 500 ─────


def _write_scalars_under_key(tmp_path: Path, key: bytes, data: dict[str, str]) -> None:
    """Persist an ENCRYPTED keys.json under a SPECIFIC master key (out of band)."""
    prev = os.environ.get("AKANA_VAULT_KEY")
    os.environ["AKANA_VAULT_KEY"] = key.decode("utf-8")
    vault_crypto.reset_cache()
    try:
        blob = vault_crypto.encrypt_str(json.dumps(data, sort_keys=True) + "\n")
        path = tmp_path / "vault" / "keys.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        vault_crypto.write_private_bytes_atomic(path, blob)
    finally:
        if prev is None:
            os.environ.pop("AKANA_VAULT_KEY", None)
        else:
            os.environ["AKANA_VAULT_KEY"] = prev
        vault_crypto.reset_cache()


def test_put_scalars_wrong_master_key_maps_to_409_not_500(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A populated-but-undecryptable keys.json → clean 409, never a 500 stacktrace.

    Write real secrets under key A, then serve the app under key B. ``assert_writable``
    sees a tagged blob it cannot decrypt and raises ``VaultUndecryptableError``; the
    route must catch it and return 409 ``VAULT_UNDECRYPTABLE`` (fail-closed: refuse to
    overwrite from an empty base), not surface an opaque 500.
    """
    key_a = Fernet.generate_key()
    _write_scalars_under_key(tmp_path, key_a, {"gemini_api_key": "old_secret_value"})

    key_b = Fernet.generate_key()
    assert key_b != key_a
    monkeypatch.setenv("AKANA_VAULT_KEY", key_b.decode("utf-8"))
    vault_crypto.reset_cache()

    app = _make_app(monkeypatch, tmp_path)  # _make_app does not clear AKANA_VAULT_KEY
    # raise_server_exceptions=True (default): a bug that let the RuntimeError escape
    # would surface as a raised exception / 500 here rather than a clean 409.
    with TestClient(app) as c:
        r = c.put(SCALARS, json={"scalars": {"gemini_api_key": "new_value_123"}})

    assert r.status_code == 409
    assert r.status_code != 500
    assert r.json()["detail"]["error"]["code"] == "VAULT_UNDECRYPTABLE"
    vault_crypto.reset_cache()


def test_delete_scalar_wrong_master_key_maps_to_409_not_500(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """DELETE scalar (previously caught NOTHING → 500) must also map to 409."""
    key_a = Fernet.generate_key()
    _write_scalars_under_key(tmp_path, key_a, {"gemini_api_key": "old_secret_value"})

    key_b = Fernet.generate_key()
    monkeypatch.setenv("AKANA_VAULT_KEY", key_b.decode("utf-8"))
    vault_crypto.reset_cache()

    app = _make_app(monkeypatch, tmp_path)
    with TestClient(app) as c:
        r = c.delete(f"{SCALARS}/gemini_api_key")

    assert r.status_code == 409
    assert r.json()["detail"]["error"]["code"] == "VAULT_UNDECRYPTABLE"
    vault_crypto.reset_cache()


def test_put_fields_wrong_master_key_maps_to_409_not_500(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Structured-fields write under a mismatched key → 409, not a 500."""
    key_a = Fernet.generate_key()
    # Persist a real encrypted secrets.enc under key A.
    prev = os.environ.get("AKANA_VAULT_KEY")
    os.environ["AKANA_VAULT_KEY"] = key_a.decode("utf-8")
    vault_crypto.reset_cache()
    try:
        blob = vault_crypto.encrypt_str(json.dumps({"password": "p"}, sort_keys=True) + "\n")
        prof = tmp_path / "credentials" / "reddit" / "default"
        prof.mkdir(parents=True, exist_ok=True)
        vault_crypto.write_private_bytes_atomic(prof / "secrets.enc", blob)
    finally:
        if prev is None:
            os.environ.pop("AKANA_VAULT_KEY", None)
        else:
            os.environ["AKANA_VAULT_KEY"] = prev
        vault_crypto.reset_cache()

    key_b = Fernet.generate_key()
    monkeypatch.setenv("AKANA_VAULT_KEY", key_b.decode("utf-8"))
    vault_crypto.reset_cache()

    app = _make_app(monkeypatch, tmp_path)
    with TestClient(app) as c:
        r = c.put(FIELDS, json={"fields": {"password": "new_pw"}})

    assert r.status_code == 409
    assert r.json()["detail"]["error"]["code"] == "VAULT_UNDECRYPTABLE"
    vault_crypto.reset_cache()
