"""Reverse-proxy / open-mode auth guard (config startup + request layer).

Closes the Tailscale Serve hole: a loopback-bound server with no token used to
trust the bind address and serve /api/v1/* unauthenticated to anything a reverse
proxy forwarded in. Now the request layer rejects proxied no-token requests, and
the startup guard warns instead of silently trusting loopback.
"""

from __future__ import annotations

import logging
import types

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from akana_server.api.deps import (
    HostHeaderGuard,
    authorize_websocket,
    request_is_proxied,
    require_akana_bearer,
)
from akana_server.config import _warn_if_auth_disabled, allow_unauthenticated


@pytest.fixture(autouse=True)
def _clear_open_mode(monkeypatch: pytest.MonkeyPatch):
    # Hermetic: the runner env must not leak an opt-out into these tests.
    monkeypatch.delenv("AKANA_ALLOW_UNAUTHENTICATED", raising=False)


def _settings(*, api_token=None, server_host="127.0.0.1"):
    return types.SimpleNamespace(api_token=api_token, server_host=server_host)


def _bearer_app(api_token):
    app = FastAPI()
    app.state.settings = _settings(api_token=api_token)

    @app.get("/probe", dependencies=[Depends(require_akana_bearer)])
    def probe():
        return {"ok": True}

    return app


# --- request layer: open mode (no token) -------------------------------------


def test_direct_request_allowed_without_token():
    with TestClient(_bearer_app(None)) as c:
        assert c.get("/probe").status_code == 200


@pytest.mark.parametrize(
    "header",
    [
        "X-Forwarded-For",
        "X-Forwarded-Host",
        "X-Forwarded-Proto",
        "Forwarded",
        "X-Real-IP",
        "Tailscale-User-Login",
    ],
)
def test_proxied_request_rejected_without_token(header):
    with TestClient(_bearer_app(None)) as c:
        r = c.get("/probe", headers={header: "x"})
    assert r.status_code == 401
    assert r.json()["detail"]["error"]["code"] == "AUTH_REQUIRED"


def test_proxied_request_allowed_when_opt_out(monkeypatch):
    monkeypatch.setenv("AKANA_ALLOW_UNAUTHENTICATED", "1")
    with TestClient(_bearer_app(None)) as c:
        r = c.get("/probe", headers={"X-Forwarded-For": "1.2.3.4"})
    assert r.status_code == 200


# --- request layer: token configured (unchanged behaviour) -------------------


def test_token_trusts_loopback_but_requires_it_for_remote_and_proxied():
    # Token configured: a DIRECT LOOPBACK peer is trusted (the local owner/UI is never
    # gated). A PROXIED request OR a direct connection from a NON-LOOPBACK peer must
    # present the valid token. REGRESSION: a direct REMOTE connection to a non-loopback
    # bind used to be trusted (it set no proxy headers), bypassing auth entirely.
    app = _bearer_app("secret")
    # direct loopback peer → allowed even with no / incorrect bearer
    with TestClient(app, client=("127.0.0.1", 5000)) as c:
        assert c.get("/probe").status_code == 200
        assert c.get("/probe", headers={"Authorization": "Bearer wrong"}).status_code == 200
    # direct NON-loopback peer (the hole) → the valid token is required
    with TestClient(app, client=("1.2.3.4", 5000)) as c:
        assert c.get("/probe").status_code == 401
        assert c.get("/probe", headers={"Authorization": "Bearer wrong"}).status_code == 401
        assert c.get("/probe", headers={"Authorization": "Bearer secret"}).status_code == 200
    # proxied → token required regardless of (proxy) peer
    with TestClient(app, client=("127.0.0.1", 5000)) as c:
        assert c.get("/probe", headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 401
        assert (
            c.get(
                "/probe",
                headers={"Authorization": "Bearer secret", "X-Forwarded-For": "1.2.3.4"},
            ).status_code
            == 200
        )


def test_non_ascii_credentials_are_refused_not_crashed():
    """Headers/query strings arrive latin-1-decoded, so a client can put a byte >= 0x80
    in them; ``hmac.compare_digest`` on *str* raises TypeError on those → the gate used
    to answer an unhandled 500 with a traceback instead of a clean refusal."""
    from starlette.requests import Request

    from akana_server.api.deps import require_akana_bearer_strict

    app = _bearer_app("secret")
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/probe",
        "query_string": b"",
        "headers": [(b"authorization", "Bearer \xe9".encode("latin-1"))],
        "client": ("1.2.3.4", 5000),
        "app": app,
    }
    for gate in (require_akana_bearer, require_akana_bearer_strict):
        with pytest.raises(HTTPException) as exc:
            gate(Request(scope))
        assert exc.value.status_code == 401
    # WebSocket side: the ?token= query param is the same latin-1 surface.
    assert authorize_websocket(_ws(api_token="secret", peer_host="1.2.3.4"), token="é") is False


def test_valid_token_passes_even_when_proxied():
    with TestClient(_bearer_app("secret")) as c:
        r = c.get(
            "/probe",
            headers={"Authorization": "Bearer secret", "X-Forwarded-For": "1.2.3.4"},
        )
    assert r.status_code == 200


# --- authorize_websocket (the single shared WS gate) -------------------------


def _ws(*, api_token=None, headers=None, peer_host="127.0.0.1"):
    """Minimal WebSocket stand-in: authorize_websocket only reads app.state.settings,
    .headers, and .client.host — the same surface as require_akana_bearer."""
    app = types.SimpleNamespace(state=types.SimpleNamespace(settings=_settings(api_token=api_token)))
    client = types.SimpleNamespace(host=peer_host) if peer_host is not None else None
    return types.SimpleNamespace(
        app=app,
        headers=Headers(headers or {}),
        client=client,
    )


def test_ws_loopback_direct_allowed_open_mode():
    # No token, direct loopback peer, no proxy headers → authorized.
    assert authorize_websocket(_ws(api_token=None), token=None) is True


def test_ws_proxied_no_token_rejected():
    # No token but a reverse-proxy header present, opt-in OFF → rejected.
    ws = _ws(api_token=None, headers={"x-forwarded-for": "1.2.3.4"})
    assert authorize_websocket(ws, token=None) is False


def test_ws_proxied_with_opt_in_allowed(monkeypatch):
    # No token, proxied, but AKANA_ALLOW_UNAUTHENTICATED=1 → authorized.
    monkeypatch.setenv("AKANA_ALLOW_UNAUTHENTICATED", "1")
    ws = _ws(api_token=None, headers={"x-forwarded-for": "1.2.3.4"})
    assert authorize_websocket(ws, token=None) is True


def test_ws_remote_direct_requires_token():
    # Token configured, direct NON-loopback peer (the hole) → the valid token is required.
    assert authorize_websocket(_ws(api_token="secret", peer_host="1.2.3.4"), token=None) is False
    assert authorize_websocket(_ws(api_token="secret", peer_host="1.2.3.4"), token="wrong") is False
    assert authorize_websocket(_ws(api_token="secret", peer_host="1.2.3.4"), token="secret") is True


def test_ws_token_trusts_loopback_but_gates_proxied():
    # Token set: direct loopback peer is trusted even with no/incorrect token…
    assert authorize_websocket(_ws(api_token="secret"), token=None) is True
    assert authorize_websocket(_ws(api_token="secret"), token="wrong") is True
    # …but a proxied request must present the valid token regardless of peer.
    proxied = _ws(api_token="secret", headers={"x-forwarded-for": "1.2.3.4"})
    assert authorize_websocket(proxied, token=None) is False
    proxied_ok = _ws(api_token="secret", headers={"x-forwarded-for": "1.2.3.4"})
    assert authorize_websocket(proxied_ok, token="secret") is True


# --- Host header guard (DNS rebinding) ----------------------------------------
#
# "the peer is loopback ⇒ the owner" is the whole trust model. A page on
# http://evil.example that re-resolves its OWN name to 127.0.0.1 becomes a loopback
# peer whose origin the browser considers same-origin — SOP/CORS no longer keeps it
# off the API, and the peer address cannot tell it apart from the owner's UI. The
# Host header still carries the attacker's domain; that is the one usable signal.


def _host_app(*, api_token=None, server_host="127.0.0.1"):
    app = FastAPI()
    app.state.settings = _settings(api_token=api_token, server_host=server_host)
    app.add_middleware(HostHeaderGuard)

    @app.get("/probe")
    def probe():
        return {"ok": True}

    @app.websocket("/sock")
    async def sock(ws):
        await ws.accept()
        await ws.close()

    return app


def _get(app, host, headers=None):
    with TestClient(app) as c:
        return c.get("/probe", headers={"Host": host, **(headers or {})})


def test_rebinding_host_is_rejected():
    r = _get(_host_app(), "evil.example.com")
    assert r.status_code == 400


@pytest.mark.parametrize(
    "host",
    [
        "localhost:8766",
        "127.0.0.1:8766",
        "127.0.0.1",
        "[::1]:8766",  # split(":")[0] would mangle this into "["
        "[::1]",
        "192.168.1.40:8766",  # LAN bind, reached by address
        "testserver",  # every TestClient in the suite
        "akana-box",  # single-label intranet name (not registrable in public DNS)
        "akana-box.local",  # mDNS
        "my-laptop.tail1a2b.ts.net:443",  # what `tailscale serve` puts in Host
    ],
)
def test_legitimate_hosts_pass(host):
    assert _get(_host_app(), host).status_code == 200


def test_configured_hostname_bind_is_allowed():
    app = _host_app(server_host="akana.example.com")
    assert _get(app, "akana.example.com:8766").status_code == 200
    assert _get(app, "other.example.com").status_code == 400


def test_valid_token_passes_any_host():
    """A rebinding page cannot read the owner's localStorage, so it never has the
    token — an authenticated proxy under a custom domain keeps working."""
    app = _host_app(api_token="secret")
    assert _get(app, "akana.example.com").status_code == 400
    assert (
        _get(app, "akana.example.com", headers={"Authorization": "Bearer secret"}).status_code
        == 200
    )
    assert (
        _get(app, "akana.example.com", headers={"Authorization": "Bearer wrong"}).status_code
        == 400
    )


def test_empty_token_is_not_a_free_pass():
    """compare_digest("Bearer ", "Bearer ") must not let a no-token instance through."""
    with TestClient(_host_app(api_token=None)) as c:
        r = c.get("/probe", headers={"Host": "evil.example.com", "Authorization": "Bearer "})
    assert r.status_code == 400


def test_websocket_on_a_rebound_host_is_closed():
    from starlette.websockets import WebSocketDisconnect

    with TestClient(_host_app()) as c:
        with pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/sock", headers={"Host": "evil.example.com"}):
                pass


def test_real_app_installs_the_guard(tmp_path, monkeypatch):
    """Wiring: the guard is on the actual application, not just testable in isolation."""
    from akana_server.api.app import create_app

    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    with TestClient(create_app()) as c:
        assert c.get("/health").status_code == 200
        assert c.get("/health", headers={"Host": "evil.example.com"}).status_code == 400


# --- request_is_proxied helper -----------------------------------------------


def test_request_is_proxied_detects_forwarding():
    assert request_is_proxied(Headers({"x-forwarded-for": "1.2.3.4"})) is True
    assert request_is_proxied(Headers({"forwarded": "for=1.2.3.4"})) is True
    assert request_is_proxied(Headers({"tailscale-user-login": "a@b.c"})) is True


def test_request_is_proxied_false_for_direct():
    assert request_is_proxied(Headers({"user-agent": "curl", "host": "localhost"})) is False


# --- allow_unauthenticated ----------------------------------------------------


@pytest.mark.parametrize(
    "val,expected",
    [("1", True), ("true", True), ("YES", True), ("on", True), ("0", False), ("", False), ("nope", False)],
)
def test_allow_unauthenticated(monkeypatch, val, expected):
    monkeypatch.setenv("AKANA_ALLOW_UNAUTHENTICATED", val)
    assert allow_unauthenticated() is expected


# --- startup guard ------------------------------------------------------------


def test_startup_silent_when_token_set(caplog):
    with caplog.at_level(logging.WARNING):
        _warn_if_auth_disabled(_settings(api_token="t", server_host="0.0.0.0"))
    assert "SECURITY" not in caplog.text
    assert "reverse proxy" not in caplog.text.lower()


def test_startup_refuses_nonloopback_empty():
    with pytest.raises(RuntimeError):
        _warn_if_auth_disabled(_settings(api_token=None, server_host="0.0.0.0"))


def test_startup_nonloopback_opt_out_warns(monkeypatch, caplog):
    monkeypatch.setenv("AKANA_ALLOW_UNAUTHENTICATED", "1")
    with caplog.at_level(logging.WARNING):
        _warn_if_auth_disabled(_settings(api_token=None, server_host="0.0.0.0"))
    assert "AKANA_ALLOW_UNAUTHENTICATED=1" in caplog.text


def test_startup_loopback_empty_warns_about_proxy(caplog):
    with caplog.at_level(logging.WARNING):
        _warn_if_auth_disabled(_settings(api_token=None, server_host="127.0.0.1"))
    assert "reverse proxy" in caplog.text.lower()


def test_startup_loopback_opt_out_is_silent(monkeypatch, caplog):
    monkeypatch.setenv("AKANA_ALLOW_UNAUTHENTICATED", "1")
    with caplog.at_level(logging.WARNING):
        _warn_if_auth_disabled(_settings(api_token=None, server_host="127.0.0.1"))
    assert "reverse proxy" not in caplog.text.lower()


def test_is_loopback_host_handles_bracketed_ipv6():
    # Regression: bracketed IPv6 ([::1]) is a genuine loopback bind but ip_address()
    # rejects the brackets → it was misclassified as non-loopback (spurious startup refusal).
    from akana_server.config import _is_loopback_host

    assert _is_loopback_host("[::1]") is True
    assert _is_loopback_host("::1") is True
    assert _is_loopback_host("127.0.0.1") is True
    assert _is_loopback_host("localhost") is True
    assert _is_loopback_host("0.0.0.0") is False
    assert _is_loopback_host("example.com") is False
