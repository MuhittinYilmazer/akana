"""Credentials routes — GET/PUT /api/v1/system/credentials (masked, write-only).

The router is not yet registered in ``create_app`` (integration happens in the
main session), so tests mount it on a bare FastAPI app with real ``Settings``.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import akana_server.api.routes.credentials as credentials_module
from akana_server.api.routes.credentials import router
from akana_server.config import load_settings
from akana_server.secret_store import load_secrets

URL = "/api/v1/system/credentials"

RAW_KEY = "key_AAAABBBBCCCCDD"
RAW_TOKEN = "sk-ant-oat01-WXYZ"


def _make_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path, *, token: str = "", cursor_key: str = ""
) -> FastAPI:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", token)
    monkeypatch.setenv("CURSOR_API_KEY", cursor_key)
    # The masked payload now reflects env-resolved Settings secrets too; clear the
    # other env-backed ones so the suite stays hermetic regardless of the runner env.
    for var in ("AKANA_TELEGRAM_BOT_TOKEN",):
        monkeypatch.delenv(var, raising=False)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.settings = load_settings()
    return app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path):
    with TestClient(_make_app(monkeypatch, tmp_path)) as c:
        yield c


def test_get_initially_unset(client: TestClient) -> None:
    r = client.get(URL)
    assert r.status_code == 200
    assert r.json() == {
        "credentials": {
            "claude_oauth_token": {"set": False, "hint": None, "source": None},
            "cursor_api_key": {"set": False, "hint": None, "source": None},
            "gemini_api_key": {"set": False, "hint": None, "source": None},
            "openai_api_key": {"set": False, "hint": None, "source": None},
            "telegram_bot_token": {"set": False, "hint": None, "source": None},
        }
    }


def test_put_sets_and_masks(client: TestClient, tmp_path) -> None:
    r = client.put(URL, json={"cursor_api_key": RAW_KEY, "claude_oauth_token": RAW_TOKEN})
    assert r.status_code == 200
    body = r.json()
    assert body["credentials"]["cursor_api_key"] == {"set": True, "hint": "…CCDD", "source": "store"}
    assert body["credentials"]["claude_oauth_token"] == {"set": True, "hint": "…WXYZ", "source": "store"}
    # Raw values never appear anywhere in the response.
    assert RAW_KEY not in r.text
    assert RAW_TOKEN not in r.text
    # Persisted to data_dir/secrets.json, encrypted at rest (no plaintext on disk).
    blob = (tmp_path / "secrets.json").read_bytes()
    assert blob.startswith(b"vault1:")
    assert RAW_KEY.encode() not in blob
    assert RAW_TOKEN.encode() not in blob
    # …but decrypts back to the original values.
    assert load_secrets(tmp_path) == {"cursor_api_key": RAW_KEY, "claude_oauth_token": RAW_TOKEN}


def test_get_never_returns_raw_value(client: TestClient) -> None:
    client.put(URL, json={"cursor_api_key": RAW_KEY})
    r = client.get(URL)
    assert r.status_code == 200
    assert RAW_KEY not in r.text
    assert r.json()["credentials"]["cursor_api_key"] == {"set": True, "hint": "…CCDD", "source": "store"}


def test_get_reflects_env_set_cursor_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """An .env-resolved key with no store entry still shows as set (the `akana
    setup` path), mirroring the orchestrator's store→env resolution."""
    with TestClient(_make_app(monkeypatch, tmp_path, cursor_key=RAW_KEY)) as c:
        r = c.get(URL)
    assert r.status_code == 200
    # Resolved from .env (no store entry) → source "env".
    assert r.json()["credentials"]["cursor_api_key"] == {"set": True, "hint": "…CCDD", "source": "env"}
    assert RAW_KEY not in r.text


def test_store_key_overrides_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    with TestClient(_make_app(monkeypatch, tmp_path, cursor_key="key_ENVAAAABBBB9999")) as c:
        c.put(URL, json={"cursor_api_key": RAW_KEY})
        r = c.get(URL)
    # Store value wins → its hint (…CCDD), not the env key's (…9999); source "store".
    assert r.json()["credentials"]["cursor_api_key"] == {"set": True, "hint": "…CCDD", "source": "store"}


def test_put_partial_patch_keeps_other_key(client: TestClient) -> None:
    client.put(URL, json={"cursor_api_key": RAW_KEY})
    r = client.put(URL, json={"claude_oauth_token": RAW_TOKEN})
    creds = r.json()["credentials"]
    assert creds["cursor_api_key"]["set"] is True
    assert creds["claude_oauth_token"]["set"] is True


def test_put_empty_string_clears(client: TestClient) -> None:
    client.put(URL, json={"cursor_api_key": RAW_KEY})
    r = client.put(URL, json={"cursor_api_key": ""})
    assert r.status_code == 200
    assert r.json()["credentials"]["cursor_api_key"] == {"set": False, "hint": None, "source": None}


def test_put_null_clears(client: TestClient) -> None:
    """VAULT-5: an explicit JSON null clears the key (documented clear), like empty string."""
    client.put(URL, json={"cursor_api_key": RAW_KEY})
    r = client.put(URL, json={"cursor_api_key": None})
    assert r.status_code == 200
    assert r.json()["credentials"]["cursor_api_key"] == {"set": False, "hint": None, "source": None}


@pytest.mark.parametrize("bad", [12345, True, {"a": 1}, ["x"]])
def test_put_non_string_value_rejected_and_keeps_existing_key(
    client: TestClient, bad
) -> None:
    """VAULT-5: a non-string value (number/bool/object) is a 422 — NOT a silent clear.
    An already-stored key must survive the rejected request."""
    client.put(URL, json={"cursor_api_key": RAW_KEY})
    r = client.put(URL, json={"cursor_api_key": bad})
    assert r.status_code == 422
    assert r.json()["detail"]["error"]["code"] == "INVALID_BODY"
    # The working key was NOT wiped by the rejected write.
    g = client.get(URL)
    assert g.json()["credentials"]["cursor_api_key"]["set"] is True


def test_put_short_value_rejected(client: TestClient) -> None:
    """A too-short value (below the real-secret floor) is rejected on save, not stored
    as a bogus "configured" key (BUG 1 — tighten the configured check)."""
    # Non-word value so the "not leaked" check can't collide with the error text.
    r = client.put(URL, json={"cursor_api_key": "Zx9Qp2"})
    assert r.status_code == 422
    assert r.json()["detail"]["error"]["code"] == "INVALID_BODY"
    assert "Zx9Qp2" not in r.text


def test_put_placeholder_rejected(client: TestClient) -> None:
    """The shipped ``.env.example`` placeholder must not be storable as a real key —
    otherwise the badge claims "configured" and chat hangs on an invalid bearer."""
    r = client.put(URL, json={"cursor_api_key": "your-cursor-api-key-here"})
    assert r.status_code == 422
    assert r.json()["detail"]["error"]["code"] == "INVALID_BODY"


def test_put_unknown_keys_silently_dropped(client: TestClient) -> None:
    r = client.put(URL, json={"nope": 1, "api_token": "leak?", "cursor_api_key": RAW_KEY})
    assert r.status_code == 200
    assert set(r.json()["credentials"]) == {
        "cursor_api_key",
        "claude_oauth_token",
        "gemini_api_key",
        "openai_api_key",
        "telegram_bot_token",
    }
    assert r.json()["credentials"]["cursor_api_key"]["set"] is True


def test_put_invalidates_matching_provider_catalog_cache(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provider key change drops that provider's model-catalog cache — all four wired.

    Regression guard for providers:dead:4: openai/claude were previously unwired (only
    cursor+gemini invalidated), so a new openai/claude key listed models against the TTL
    cache from the old key.
    """
    called: set[str] = set()
    for provider in ("cursor", "gemini", "openai", "claude"):
        mod = __import__(
            f"akana_server.orchestrator.{provider}_catalog", fromlist=["x"]
        )
        fn = f"invalidate_{provider}_catalog_cache"
        monkeypatch.setattr(mod, fn, lambda p=provider: called.add(p))

    client.put(URL, json={"cursor_api_key": RAW_KEY})
    client.put(URL, json={"gemini_api_key": "gemsecretkey123456"})
    client.put(URL, json={"openai_api_key": "sk-openaisecretkey123456"})
    client.put(URL, json={"claude_oauth_token": RAW_TOKEN})
    assert called == {"cursor", "gemini", "openai", "claude"}

    # A key NOT tied to a live catalog (telegram) invalidates nothing.
    called.clear()
    client.put(URL, json={"telegram_bot_token": "1234567890:AAAA-telegram-bot-tok"})
    assert called == set()


@pytest.mark.parametrize("payload", [["cursor_api_key"], "key", 42])
def test_put_non_object_body_returns_422(client: TestClient, payload) -> None:
    r = client.put(URL, json=payload)
    assert r.status_code == 422
    assert r.json()["detail"]["error"]["code"] == "INVALID_BODY"


def test_put_invalid_json_returns_400(client: TestClient) -> None:
    r = client.put(URL, content=b"not-json{", headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"]["code"] == "INVALID_JSON"


def test_bearer_required_when_token_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    app = _make_app(monkeypatch, tmp_path, token="sekret-token")
    # The token gate applies only to PROXIED requests; X-Forwarded-For makes the
    # request look proxied so the configured token is actually enforced.
    proxied = {"X-Forwarded-For": "1.2.3.4"}
    with TestClient(app) as c:
        assert c.get(URL, headers=proxied).status_code == 401
        assert (
            c.put(URL, json={"cursor_api_key": RAW_KEY}, headers=proxied).status_code
            == 401
        )
        ok = c.get(URL, headers={**proxied, "Authorization": "Bearer sekret-token"})
        assert ok.status_code == 200


def test_put_offloads_set_secrets_off_the_event_loop(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """put_credentials must run the blocking, lock-holding set_secrets off the loop.

    set_secrets takes the cross-process .vault.lock (up to ~30s on Windows under
    contention) + does synchronous Fernet encrypt + atomic disk write; running it inline
    on the single-threaded asyncio loop freezes all SSE/WS/HTTP. Every vault.py write is
    wrapped in asyncio.to_thread; credentials.py must be too. We assert set_secrets runs
    on a WORKER thread (asyncio.to_thread offloads there), not the loop thread.
    """
    import asyncio

    real_set_secrets = credentials_module.set_secrets
    seen: dict[str, object] = {}

    def spy(data_dir, patch):
        # asyncio.to_thread runs the func in a worker thread with NO running loop →
        # get_running_loop() raises. An inline (un-offloaded) call runs in the loop's
        # thread, where get_running_loop() returns the loop. This is robust under
        # starlette's TestClient (which runs the loop on a portal thread, not main).
        try:
            asyncio.get_running_loop()
            seen["on_loop"] = True
        except RuntimeError:
            seen["on_loop"] = False
        return real_set_secrets(data_dir, patch)

    monkeypatch.setattr(credentials_module, "set_secrets", spy)
    r = client.put(URL, json={"cursor_api_key": RAW_KEY})
    assert r.status_code == 200
    assert seen, "set_secrets was never called"
    assert seen["on_loop"] is False  # offloaded off the event loop


# ── /system/credentials/{key}/reveal (audited owner reveal) ──────────────────

REVEAL = f"{URL}/cursor_api_key/reveal"


def test_reveal_returns_stored_value(client: TestClient) -> None:
    client.put(URL, json={"cursor_api_key": RAW_KEY})
    r = client.get(REVEAL)
    assert r.status_code == 200
    assert r.json() == {"key": "cursor_api_key", "value": RAW_KEY}


def test_reveal_reflects_env_value(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """An .env-resolved key with no store entry is still revealable — it matches the
    masked hint, which already reflects env (parity with the listing)."""
    with TestClient(_make_app(monkeypatch, tmp_path, cursor_key=RAW_KEY)) as c:
        r = c.get(REVEAL)
    assert r.status_code == 200
    assert r.json()["value"] == RAW_KEY


def test_reveal_store_overrides_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    with TestClient(_make_app(monkeypatch, tmp_path, cursor_key="key_ENVAAAABBBB9999")) as c:
        c.put(URL, json={"cursor_api_key": RAW_KEY})
        r = c.get(REVEAL)
    # Store value wins over the .env key, same precedence as the masked listing.
    assert r.json()["value"] == RAW_KEY


def test_reveal_unset_returns_404(client: TestClient) -> None:
    r = client.get(REVEAL)
    assert r.status_code == 404
    assert r.json()["detail"]["error"]["code"] == "NOT_FOUND"


def test_reveal_unknown_key_returns_404(client: TestClient) -> None:
    r = client.get(f"{URL}/not_a_real_key/reveal")
    assert r.status_code == 404
    assert r.json()["detail"]["error"]["code"] == "NOT_FOUND"


def test_reveal_requires_bearer(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    app = _make_app(monkeypatch, tmp_path, token="sekret-token")
    proxied = {"X-Forwarded-For": "1.2.3.4"}
    authed = {**proxied, "Authorization": "Bearer sekret-token"}
    with TestClient(app) as c:
        c.put(URL, json={"cursor_api_key": RAW_KEY}, headers=authed)  # authorized write
        assert c.get(REVEAL, headers=proxied).status_code == 401
        ok = c.get(REVEAL, headers=authed)
        assert ok.status_code == 200
        assert ok.json()["value"] == RAW_KEY
