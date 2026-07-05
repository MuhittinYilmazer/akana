"""Vault CRUD routes — masked GET/PUT/DELETE for scalars and structured fields.

Mounted on a bare app with real ``Settings`` (avoids the full ``create_app``).
Listing/PUT responses never carry raw values; on disk they are encrypted. The
one deliberate exception is the per-key ``/reveal`` GET — a single secret's RAW
value, bearer-gated and audited, so the owner can verify what they stored.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from akana_server.api.routes.vault import router
from akana_server.config import load_settings

SCALARS = "/api/v1/system/vault/scalars"
PROFILE = "/api/v1/system/vault/reddit/default"
FIELDS = "/api/v1/system/vault/reddit/default/fields"

# Use an EXTENDED (non-ALLOWED_KEYS) scalar to exercise the keyfile path: system provider
# keys now route to secret_store (secrets.json), so a system-key name here would land there,
# not in vault/keys.json. mistral_api_key is deliberately not a system key (see
# test_secure_vault.py's extended_key convention).
SCALAR_KEY = "mistral_api_key"
GEMINI = "gkey_abcdefgh"  # hint → …efgh
USERNAME = "alice_demo"  # hint → …demo
PASSWORD = "s3cret_password"  # hint → …word


def _make_app(monkeypatch: pytest.MonkeyPatch, tmp_path, *, token: str = "") -> FastAPI:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", token)
    monkeypatch.setenv("CURSOR_API_KEY", "")
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.settings = load_settings()
    return app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    with TestClient(_make_app(monkeypatch, tmp_path)) as c:
        yield c


# ── scalars ───────────────────────────────────────────────────────────────────


def test_scalars_initially_empty(client: TestClient) -> None:
    assert client.get(SCALARS).json() == {"scalars": {}}


def test_scalars_put_masks_and_persists_encrypted(client: TestClient, tmp_path) -> None:
    r = client.put(SCALARS, json={"scalars": {SCALAR_KEY: GEMINI}})
    assert r.status_code == 200
    assert r.json()["scalars"][SCALAR_KEY] == {"set": True, "hint": "…efgh"}
    assert GEMINI not in r.text
    # Reflected on GET, still masked.
    assert client.get(SCALARS).json()["scalars"][SCALAR_KEY] == {"set": True, "hint": "…efgh"}
    # Encrypted at rest.
    blob = (tmp_path / "vault" / "keys.json").read_bytes()
    assert blob.startswith(b"vault1:")
    assert GEMINI.encode() not in blob


def test_scalars_delete_clears(client: TestClient) -> None:
    client.put(SCALARS, json={"scalars": {SCALAR_KEY: GEMINI}})
    r = client.delete(f"{SCALARS}/{SCALAR_KEY}")
    assert r.status_code == 200
    assert r.json() == {"scalars": {}}


def test_scalars_put_invalid_body_422(client: TestClient) -> None:
    r = client.put(SCALARS, json={"nope": {}})
    assert r.status_code == 422
    assert r.json()["detail"]["error"]["code"] == "INVALID_BODY"


def test_scalars_put_invalid_key_422(client: TestClient) -> None:
    r = client.put(SCALARS, json={"scalars": {"bad key!": "value123"}})
    assert r.status_code == 422
    assert r.json()["detail"]["error"]["code"] == "INVALID_REQUEST"


def test_status_reports_encryption_health(client: TestClient) -> None:
    r = client.get("/api/v1/system/vault")
    assert r.status_code == 200
    enc = r.json()["encryption"]
    assert enc["available"] is True
    assert enc["healthy"] is True
    assert "key_source" in enc


def test_scalar_reveal_returns_raw_value(client: TestClient) -> None:
    client.put(SCALARS, json={"scalars": {SCALAR_KEY: GEMINI}})
    r = client.get(f"{SCALARS}/{SCALAR_KEY}/reveal")
    assert r.status_code == 200
    assert r.json() == {"key": SCALAR_KEY, "value": GEMINI}


def test_scalar_reveal_missing_is_404(client: TestClient) -> None:
    r = client.get(f"{SCALARS}/never_stored/reveal")
    assert r.status_code == 404
    assert r.json()["detail"]["error"]["code"] == "NOT_FOUND"


# ── structured fields ──────────────────────────────────────────────────────────


def test_fields_put_get_mask_and_encrypt(client: TestClient, tmp_path) -> None:
    r = client.put(FIELDS, json={"fields": {"username": USERNAME, "password": PASSWORD}})
    assert r.status_code == 200
    body = r.json()
    assert body["namespace"] == "reddit"
    assert body["fields"]["username"] == {"set": True, "hint": "…demo"}
    assert body["fields"]["password"] == {"set": True, "hint": "…word"}
    # No raw values in the response.
    assert USERNAME not in r.text
    assert PASSWORD not in r.text
    # GET returns the same masked view.
    g = client.get(FIELDS)
    assert g.status_code == 200
    assert set(g.json()["fields"]) == {"username", "password"}
    # Encrypted at rest under the profile dir.
    blob = (tmp_path / "credentials" / "reddit" / "default" / "secrets.enc").read_bytes()
    assert blob.startswith(b"vault1:")
    assert PASSWORD.encode() not in blob
    assert USERNAME.encode() not in blob


def test_fields_delete_one_keeps_other(client: TestClient) -> None:
    client.put(FIELDS, json={"fields": {"username": USERNAME, "password": PASSWORD}})
    r = client.delete(f"{FIELDS}/password")
    assert r.status_code == 200
    assert set(r.json()["fields"]) == {"username"}


def test_profile_delete_removes_whole_profile(client: TestClient, tmp_path) -> None:
    client.put(FIELDS, json={"fields": {"username": USERNAME, "password": PASSWORD}})
    prof_dir = tmp_path / "credentials" / "reddit" / "default"
    assert prof_dir.is_dir()
    r = client.delete(PROFILE)
    assert r.status_code == 200
    assert r.json() == {"namespace": "reddit", "profile": "default", "removed": True}
    # real delete: the encrypted bundle dir is gone, GET fields is empty again
    assert not prof_dir.exists()
    assert client.get(FIELDS).json()["fields"] == {}


def test_profile_delete_missing_is_idempotent(client: TestClient) -> None:
    r = client.delete("/api/v1/system/vault/ghost/default")
    assert r.status_code == 200
    assert r.json()["removed"] is False


def test_profile_delete_invalid_namespace_422(client: TestClient) -> None:
    r = client.delete("/api/v1/system/vault/BadNS/default")
    assert r.status_code == 422
    assert r.json()["detail"]["error"]["code"] == "INVALID_NAMESPACE"


def test_fields_invalid_namespace_422(client: TestClient) -> None:
    r = client.get("/api/v1/system/vault/BadNS/default/fields")
    assert r.status_code == 422
    assert r.json()["detail"]["error"]["code"] == "INVALID_NAMESPACE"


def test_fields_empty_initially(client: TestClient) -> None:
    assert client.get(FIELDS).json()["fields"] == {}


def test_field_reveal_returns_raw_value(client: TestClient) -> None:
    client.put(FIELDS, json={"fields": {"username": USERNAME, "password": PASSWORD}})
    r = client.get(f"{FIELDS}/password/reveal")
    assert r.status_code == 200
    assert r.json() == {
        "namespace": "reddit",
        "profile": "default",
        "key": "password",
        "value": PASSWORD,
    }


def test_field_reveal_unknown_key_is_404(client: TestClient) -> None:
    client.put(FIELDS, json={"fields": {"username": USERNAME}})
    r = client.get(f"{FIELDS}/password/reveal")
    assert r.status_code == 404
    assert r.json()["detail"]["error"]["code"] == "NOT_FOUND"


def test_field_reveal_invalid_namespace_422(client: TestClient) -> None:
    r = client.get("/api/v1/system/vault/BadNS/default/fields/password/reveal")
    assert r.status_code == 422
    assert r.json()["detail"]["error"]["code"] == "INVALID_NAMESPACE"


# ── auth ────────────────────────────────────────────────────────────────────────


def test_bearer_required_when_token_set(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    app = _make_app(monkeypatch, tmp_path, token="sekret-token")
    # The token gate applies only to PROXIED requests; X-Forwarded-For makes the
    # request look proxied so the configured token is actually enforced.
    proxied = {"X-Forwarded-For": "1.2.3.4"}
    with TestClient(app) as c:
        assert c.get(SCALARS, headers=proxied).status_code == 401
        assert (
            c.put(
                FIELDS, json={"fields": {"username": USERNAME}}, headers=proxied
            ).status_code
            == 401
        )
        ok = c.get(SCALARS, headers={**proxied, "Authorization": "Bearer sekret-token"})
        assert ok.status_code == 200


def test_reveal_requires_bearer_on_loopback_when_token_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """VAULT-3: raw-secret /reveal must demand the bearer even from a DIRECT loopback peer
    when a token is configured — 'loopback' means same machine, not same OS user, so another
    local account must not read plaintext secrets. Non-reveal routes keep the loopback skip
    (local UI just works)."""
    app = _make_app(monkeypatch, tmp_path, token="sekret-token")
    # A direct LOOPBACK peer (no proxy headers) — the "local UI" origin.
    with TestClient(app, client=("127.0.0.1", 54321)) as c:
        c.put(SCALARS, json={"scalars": {SCALAR_KEY: GEMINI}})  # seed a secret (loopback-ok)
        # Reveal WITHOUT the bearer is now rejected even on loopback.
        assert c.get(f"{SCALARS}/{SCALAR_KEY}/reveal").status_code == 401
        # With the bearer it succeeds and returns the raw value.
        ok = c.get(
            f"{SCALARS}/{SCALAR_KEY}/reveal",
            headers={"Authorization": "Bearer sekret-token"},
        )
        assert ok.status_code == 200
        assert ok.json()["value"] == GEMINI
        # A NON-reveal route still skips the token on loopback (local UI unbroken).
        assert c.get(SCALARS).status_code == 200


def test_reveal_open_mode_loopback_still_works_without_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """VAULT-3 guard: with NO token configured the local-UI-just-works behaviour is
    preserved — a loopback reveal succeeds with no bearer (only tightened when a token is set)."""
    app = _make_app(monkeypatch, tmp_path, token="")
    with TestClient(app, client=("127.0.0.1", 54321)) as c:
        c.put(SCALARS, json={"scalars": {SCALAR_KEY: GEMINI}})
        r = c.get(f"{SCALARS}/{SCALAR_KEY}/reveal")
        assert r.status_code == 200
        assert r.json()["value"] == GEMINI
