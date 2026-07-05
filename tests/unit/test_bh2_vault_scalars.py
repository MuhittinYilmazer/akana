"""BH2 — provider (system) credentials are VISIBLE in the vault scalars listing.

Regression: keys in :data:`secret_store.ALLOWED_KEYS` (cursor_api_key,
claude_oauth_token, …) are stored in ``secrets.json`` via ``set_secrets`` and work,
but the vault UI's ``GET /system/vault/scalars`` only read ``vault/keys.json``
(``load_scalars``) — so provider keys never appeared. The endpoint now MERGES the
two masked stores: keyfile scalars plus the system credentials from ``secrets.json``,
the latter tagged ``is_system_credential``. Reveal/delete of those rows dual-route
through ``get_scalar``/``set_scalar`` (which resolve ``ALLOWED_KEYS`` from the secret
store), so what the row shows is what gets revealed/cleared.

Hermetic: bare app + real ``Settings`` on a ``tmp_path`` data dir (mirrors
``test_vault_routes.py``); values are written straight into that dir via the store
helpers, never touching a shared/global store.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from akana_server.api.routes.vault import router
from akana_server.config import load_settings
from akana_server.secret_store import mask_hint, set_secrets
from akana_server.secure_vault import set_scalars

SCALARS = "/api/v1/system/vault/scalars"

# Real-looking secrets: long enough (>= 8) and free of placeholder markers so the
# secret store keeps them (a "your-…-here" style value would be filtered out).
CURSOR_KEY = "cur_live_abcdefghijklmnop"  # hint -> …mnop
CLAUDE_TOKEN = "sk-ant-oat01-abcdefgh1234"  # hint -> …1234
# A genuinely EXTENDED scalar (NOT in secret_store.ALLOWED_KEYS): it lives in the keyfile
# and is listed WITHOUT the is_system_credential tag. A system-key name (e.g.
# gemini_api_key) would route to secret_store and be tagged, defeating this test's premise.
PLAIN_KEY = "mistral_api_key"
PLAIN_VAL = "gkey_abcdefgh"  # hint -> …efgh


def _make_app(monkeypatch: pytest.MonkeyPatch, tmp_path) -> FastAPI:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    # Keep the env-derived Settings from pre-seeding a provider key.
    monkeypatch.setenv("CURSOR_API_KEY", "")
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.settings = load_settings()
    return app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    with TestClient(_make_app(monkeypatch, tmp_path)) as c:
        yield c


def test_system_credential_appears_masked_and_tagged(client: TestClient, tmp_path) -> None:
    # Write a Cursor provider key into the secret store (secrets.json).
    set_secrets(tmp_path, {"cursor_api_key": CURSOR_KEY})

    r = client.get(SCALARS)
    assert r.status_code == 200
    scalars = r.json()["scalars"]

    assert "cursor_api_key" in scalars, "system provider key must be visible in the vault listing"
    entry = scalars["cursor_api_key"]
    assert entry["set"] is True
    assert entry["hint"] == mask_hint(CURSOR_KEY)  # masked, never raw
    assert entry["is_system_credential"] is True
    # The raw secret is never present in the response body.
    assert CURSOR_KEY not in r.text


def test_system_and_keyfile_scalars_coexist(client: TestClient, tmp_path) -> None:
    set_secrets(tmp_path, {"claude_oauth_token": CLAUDE_TOKEN})
    set_scalars(tmp_path, {PLAIN_KEY: PLAIN_VAL})

    scalars = client.get(SCALARS).json()["scalars"]

    # System credential — tagged.
    assert scalars["claude_oauth_token"] == {
        "set": True,
        "hint": mask_hint(CLAUDE_TOKEN),
        "is_system_credential": True,
    }
    # Plain keyfile scalar — present and NOT tagged as a system credential.
    assert scalars[PLAIN_KEY]["set"] is True
    assert scalars[PLAIN_KEY]["hint"] == mask_hint(PLAIN_VAL)
    assert "is_system_credential" not in scalars[PLAIN_KEY]


def test_plain_keyfile_scalar_still_appears_without_system_keys(client: TestClient, tmp_path) -> None:
    # No system credentials written — a plain vault/keys.json scalar still shows.
    set_scalars(tmp_path, {PLAIN_KEY: PLAIN_VAL})
    scalars = client.get(SCALARS).json()["scalars"]
    assert set(scalars) == {PLAIN_KEY}
    assert scalars[PLAIN_KEY] == {"set": True, "hint": mask_hint(PLAIN_VAL)}


def test_reveal_dual_routes_for_system_credential(client: TestClient, tmp_path) -> None:
    set_secrets(tmp_path, {"cursor_api_key": CURSOR_KEY})
    # Reveal uses the same /scalars/<key>/reveal endpoint the UI calls; it must
    # resolve the system key from the secret store, not 404 on the keyfile.
    r = client.get(f"{SCALARS}/cursor_api_key/reveal")
    assert r.status_code == 200
    assert r.json() == {"key": "cursor_api_key", "value": CURSOR_KEY}


def test_delete_dual_routes_and_clears_system_credential(client: TestClient, tmp_path) -> None:
    set_secrets(tmp_path, {"cursor_api_key": CURSOR_KEY})
    # Sanity: visible before delete.
    assert "cursor_api_key" in client.get(SCALARS).json()["scalars"]

    r = client.delete(f"{SCALARS}/cursor_api_key")
    assert r.status_code == 200
    # DELETE returns the merged masked view; the system key is gone from it…
    assert "cursor_api_key" not in r.json()["scalars"]
    # …and a fresh listing confirms the secret store row was actually cleared.
    assert "cursor_api_key" not in client.get(SCALARS).json()["scalars"]
