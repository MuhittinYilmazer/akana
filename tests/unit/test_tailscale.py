"""Tailscale integration — status parsing + the funnel-without-token 400 guard.

Two layers:

* ``akana_server.network.tailscale`` — ``get_status``/``set_serve`` parsing is
  exercised by mocking the subprocess wrapper (``_run``) and ``find_binary`` so
  no real ``tailscale`` binary is needed (it is not installed on CI/dev).
* ``/api/v1/system/tailscale`` — the route, especially the HARD security guard:
  ``mode="funnel"`` MUST be refused with 400 when no ``AKANA_TOKEN`` is set.

Sync tests use ``asyncio.run`` for the async surface so they run under the
canonical autoload-off runner.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app
from akana_server.network import tailscale as ts

# A trimmed but realistic `tailscale status --json` for a logged-in node.
_STATUS_LOGGED_IN = json.dumps(
    {
        "BackendState": "Running",
        "Self": {
            "DNSName": "my-box.tailnet-1234.ts.net.",
            "TailscaleIPs": ["100.101.102.103", "fd7a:115c:a1e0::1"],
        },
    }
)

_STATUS_NEEDS_LOGIN = json.dumps(
    {"BackendState": "NeedsLogin", "Self": {"DNSName": "", "TailscaleIPs": []}}
)


def _fake_run(mapping):
    """Build an async ``_run`` replacement.

    ``mapping`` maps the FIRST CLI arg (e.g. "status", "serve", "funnel") to a
    ``(returncode, stdout, stderr)`` tuple.
    """

    async def _run(binary, *args):  # noqa: ANN001
        key = args[0] if args else ""
        return mapping.get(key, (0, "", ""))

    return _run


# -- get_status parsing --------------------------------------------------------


def test_status_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ts, "find_binary", lambda: None)
    out = asyncio.run(ts.get_status())
    assert out["installed"] is False
    assert out["logged_in"] is False
    assert out["https_url"] is None
    assert out["error"] is None
    # Platform-aware install guidance is present + points at the download page.
    assert out["guidance"] and "tailscale.com/download" in out["guidance"]


def test_status_logged_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ts, "find_binary", lambda: "tailscale")
    monkeypatch.setattr(
        ts,
        "_run",
        _fake_run(
            {
                "status": (0, _STATUS_LOGGED_IN, ""),
                # serve status --json: an active web mapping + funnel off.
                "serve": (0, json.dumps({"Web": {"foo": {}}, "AllowFunnel": {}}), ""),
            }
        ),
    )
    out = asyncio.run(ts.get_status())
    assert out["installed"] is True
    assert out["backend_state"] == "Running"
    assert out["logged_in"] is True
    # Trailing dot stripped from the MagicDNS name.
    assert out["self_dns_name"] == "my-box.tailnet-1234.ts.net"
    assert out["https_url"] == "https://my-box.tailnet-1234.ts.net"
    assert "100.101.102.103" in out["tailscale_ips"]
    assert out["serve_active"] is True
    assert out["funnel_active"] is False
    assert out["error"] is None


def test_status_logged_in_funnel_active(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ts, "find_binary", lambda: "tailscale")
    monkeypatch.setattr(
        ts,
        "_run",
        _fake_run(
            {
                "status": (0, _STATUS_LOGGED_IN, ""),
                "serve": (
                    0,
                    json.dumps({"Web": {"foo": {}}, "AllowFunnel": {"foo:443": True}}),
                    "",
                ),
            }
        ),
    )
    out = asyncio.run(ts.get_status())
    assert out["serve_active"] is True
    assert out["funnel_active"] is True


def test_status_not_logged_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ts, "find_binary", lambda: "tailscale")
    monkeypatch.setattr(
        ts, "_run", _fake_run({"status": (0, _STATUS_NEEDS_LOGIN, "")})
    )
    out = asyncio.run(ts.get_status())
    assert out["installed"] is True
    assert out["logged_in"] is False
    assert out["backend_state"] == "NeedsLogin"
    assert out["https_url"] is None
    assert out["guidance"] and "tailscale up" in out["guidance"]


def test_status_daemon_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    # Non-zero exit from `status` → installed but not running; guidance offered.
    monkeypatch.setattr(ts, "find_binary", lambda: "tailscale")
    monkeypatch.setattr(
        ts, "_run", _fake_run({"status": (1, "", "failed to connect to local tailscaled")})
    )
    out = asyncio.run(ts.get_status())
    assert out["installed"] is True
    assert out["logged_in"] is False
    assert out["error"]
    assert out["guidance"]


def test_status_garbage_json_defensive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ts, "find_binary", lambda: "tailscale")
    monkeypatch.setattr(ts, "_run", _fake_run({"status": (0, "not json at all {", "")}))
    out = asyncio.run(ts.get_status())
    # Defensive parse: no crash, degrades to logged-out.
    assert out["installed"] is True
    assert out["logged_in"] is False
    assert out["self_dns_name"] is None


# -- set_serve command mapping + error guidance --------------------------------


def test_set_serve_invalid_mode() -> None:
    out = asyncio.run(ts.set_serve(8766, "bogus"))
    assert out["ok"] is False
    assert "invalid mode" in out["error"]


def test_set_serve_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ts, "find_binary", lambda: None)
    out = asyncio.run(ts.set_serve(8766, "serve"))
    assert out["ok"] is False
    assert out["guidance"] and "tailscale.com/download" in out["guidance"]


def test_set_serve_serve_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []

    async def _run(binary, *args):  # noqa: ANN001
        calls.append(args)
        return (0, "", "")

    monkeypatch.setattr(ts, "find_binary", lambda: "tailscale")
    monkeypatch.setattr(ts, "_run", _run)
    out = asyncio.run(ts.set_serve(8766, "serve"))
    assert out["ok"] is True
    # Persistent --bg form pointed at the loopback API port.
    assert ("serve", "--bg", "http://127.0.0.1:8766") in calls


def test_set_serve_off_resets_both(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []

    async def _run(binary, *args):  # noqa: ANN001
        seen.append(args[0] if args else "")
        return (0, "", "")

    monkeypatch.setattr(ts, "find_binary", lambda: "tailscale")
    monkeypatch.setattr(ts, "_run", _run)
    out = asyncio.run(ts.set_serve(8766, "off"))
    assert out["ok"] is True
    assert "serve" in seen and "funnel" in seen


def test_set_serve_funnel_not_enabled_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ts, "find_binary", lambda: "tailscale")
    monkeypatch.setattr(
        ts,
        "_run",
        _fake_run({"funnel": (1, "", "Funnel not enabled for this tailnet")}),
    )
    out = asyncio.run(ts.set_serve(8766, "funnel"))
    assert out["ok"] is False
    assert out["guidance"] and "Funnel is not enabled" in out["guidance"]


# -- route: security guard (funnel requires a token) ---------------------------


@pytest.fixture
def client_no_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    # Pretend tailscale is installed so the guard, not the missing binary, is hit.
    monkeypatch.setattr(ts, "find_binary", lambda: "tailscale")
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client_with_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AKANA_TOKEN", "gizli-token")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    monkeypatch.setattr(ts, "find_binary", lambda: "tailscale")
    app = create_app()
    with TestClient(app) as c:
        yield c


_SERVE_URL = "/api/v1/system/tailscale/serve"
_STATUS_URL = "/api/v1/system/tailscale"


def test_funnel_without_token_is_refused(client_no_token: TestClient) -> None:
    r = client_no_token.post(_SERVE_URL, json={"mode": "funnel"})
    assert r.status_code == 400
    err = r.json()["detail"]["error"]
    assert err["code"] == "FUNNEL_REQUIRES_TOKEN"
    assert "public internet" in err["message"]


def test_serve_without_token_is_allowed(
    client_no_token: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # serve (tailnet-private) does not require a token; stub the CLI so it succeeds.
    async def _ok(binary, *args):  # noqa: ANN001
        if args and args[0] == "status":
            return (0, _STATUS_NEEDS_LOGIN, "")
        return (0, "", "")

    monkeypatch.setattr(ts, "_run", _ok)
    r = client_no_token.post(_SERVE_URL, json={"mode": "serve"})
    assert r.status_code == 200
    assert r.json()["applied_mode"] == "serve"


def test_funnel_with_token_passes_guard(
    client_with_token: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _ok(binary, *args):  # noqa: ANN001
        if args and args[0] == "status":
            return (0, _STATUS_LOGGED_IN, "")
        if args and args[0] == "serve":
            return (0, json.dumps({"Web": {"x": {}}, "AllowFunnel": {"x:443": True}}), "")
        return (0, "", "")

    monkeypatch.setattr(ts, "_run", _ok)
    # A configured token → funnel is allowed; the request goes through to the CLI.
    r = client_with_token.post(
        _SERVE_URL,
        json={"mode": "funnel"},
        headers={"Authorization": "Bearer gizli-token"},
    )
    assert r.status_code == 200
    assert r.json()["applied_mode"] == "funnel"


def test_invalid_mode_is_422(client_no_token: TestClient) -> None:
    r = client_no_token.post(_SERVE_URL, json={"mode": "sideways"})
    assert r.status_code == 422
    assert r.json()["detail"]["error"]["code"] == "INVALID_MODE"


def test_status_route_returns_snapshot(
    client_no_token: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ts, "find_binary", lambda: None)
    r = client_no_token.get(_STATUS_URL)
    assert r.status_code == 200
    body = r.json()
    assert body["installed"] is False
    assert "guidance" in body
