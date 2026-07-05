"""``/ws/voice/live`` WS gate ladder (Phase 2) — token → flag → SDK/key.

Hermetic: NO API key → ``gemini_available`` False (even if the SDK is installed); even
with the flag on, the SDK/key gate returns a clean ``close(1011)`` (never a 500/raw blowup).
The happy path (a real Live session) is NOT tested here — it needs a real key + network;
the bridge logic is covered with FakeLiveSession in ``tests/unit/test_gemini_live.py``."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from akana_server.api.app import create_app

URL = "/ws/voice/live"


def _make_client(monkeypatch, tmp_path, *, token: str = "", live: str = "0") -> TestClient:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", token)
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    monkeypatch.setenv("AKANA_GEMINI_LIVE_ENABLED", live)
    return TestClient(create_app())


def test_wrong_token_closes_1008(monkeypatch, tmp_path) -> None:
    with _make_client(monkeypatch, tmp_path, token="s3cret", live="1") as client:
        with pytest.raises(WebSocketDisconnect) as ei:
            with client.websocket_connect(f"{URL}?token=wrong") as ws:
                ws.receive_text()
        assert ei.value.code == 1008  # auth gate (BEFORE flag/sdk)


def test_flag_disabled_closes_1011(monkeypatch, tmp_path) -> None:
    # Auth off (token=""), Live flag default OFF → 1011.
    with _make_client(monkeypatch, tmp_path, token="", live="0") as client:
        with pytest.raises(WebSocketDisconnect) as ei:
            with client.websocket_connect(URL) as ws:
                ws.receive_text()
        assert ei.value.code == 1011


def test_flag_on_but_unavailable_closes_1011(monkeypatch, tmp_path) -> None:
    # Flag ON but Gemini is unavailable (no key; even if the SDK is installed) → clean 1011.
    with _make_client(monkeypatch, tmp_path, token="", live="1") as client:
        with pytest.raises(WebSocketDisconnect) as ei:
            with client.websocket_connect(URL) as ws:
                ws.receive_text()
        assert ei.value.code == 1011
