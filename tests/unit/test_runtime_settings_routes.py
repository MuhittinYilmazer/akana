"""Runtime settings REST surface — GET/PUT/reset + bearer + cache invalidation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app
from akana_server.runtime_settings import reset_runtime_stores

URL = "/api/v1/settings/runtime"


@pytest.fixture(autouse=True)
def _isolated_stores():
    reset_runtime_stores()
    yield
    reset_runtime_stores()


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, data_dir: Path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AKANA_TOKEN", "gizli-token")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    app = create_app()
    with TestClient(app) as c:
        yield c


AUTH = {"Authorization": "Bearer gizli-token"}


def test_bearer_zorunlu(client: TestClient) -> None:
    # The token gate applies only to PROXIED requests; X-Forwarded-For makes the
    # request look proxied so the configured token is actually enforced.
    proxied = {"X-Forwarded-For": "1.2.3.4"}
    assert client.get(URL, headers=proxied).status_code == 401
    assert (
        client.put(URL, json={"context_max_chars": 3}, headers=proxied).status_code
        == 401
    )
    assert (
        client.post(f"{URL}/reset/context_max_chars", headers=proxied).status_code
        == 401
    )


def test_get_sema_deger_kaynak(client: TestClient) -> None:
    r = client.get(URL, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    keys = {s["key"] for s in body["settings"]}
    assert {"session_closer_interval", "file_roots"} <= keys
    # session_closer_enabled is hidden from the editable form (Memory Studio owns the
    # user-facing toggle); it must NOT appear in the form payload.
    assert "session_closer_enabled" not in keys
    for item in body["settings"]:
        assert item["source"] in ("runtime", "env", "default")
        assert item["category"] in {c["id"] for c in body["categories"]}
        assert item["label"] and item["description"]


def test_language_formdan_gizli_ama_include_hidden_ile_gelir(client: TestClient) -> None:
    """`language` is hidden from the form — the General tab carries its own language
    picker, and showing it in two places was confusing. But the spec STANDS: the i18n
    startup reconcile reads the language from here (``?include_hidden=1``) and the General
    tab's picker PUTs to this key. So the setting is hidden, the plumbing is not broken.
    """
    base = client.get(URL, headers=AUTH).json()
    assert "language" not in {s["key"] for s in base["settings"]}
    full = client.get(f"{URL}?include_hidden=1", headers=AUTH).json()
    lang = next((s for s in full["settings"] if s["key"] == "language"), None)
    assert lang is not None and lang["value"] in ("en", "tr")
    # The General tab's language picker writes to this key → PUT must still be valid.
    r = client.put(URL, headers=AUTH, json={"language": "tr"})
    assert r.status_code == 200 and "language" in r.json()["changed"]


def test_session_closer_enabled_formdan_gizli_ama_musluk_calisir(client: TestClient) -> None:
    """`session_closer_enabled` is hidden from the form — the user-facing 'session
    summarization' on/off toggle is carried by Memory Studio (`session_summary`); showing a
    second master switch that does the same job under Advanced was confusing. The spec STANDS:
    it remains as a kill-switch at the env level (`AKANA_SESSION_CLOSER_ENABLED`); it is read
    via include_hidden and PUT is still valid. So the setting is hidden, the plumbing is not broken.
    """
    base = client.get(URL, headers=AUTH).json()
    assert "session_closer_enabled" not in {s["key"] for s in base["settings"]}
    full = client.get(f"{URL}?include_hidden=1", headers=AUTH).json()
    scl = next((s for s in full["settings"] if s["key"] == "session_closer_enabled"), None)
    assert scl is not None and isinstance(scl["value"], bool)
    # Even though hidden, PUT is valid (the programmatic counterpart of the env kill-switch).
    r = client.put(URL, headers=AUTH, json={"session_closer_enabled": False})
    assert r.status_code == 200 and "session_closer_enabled" in r.json()["changed"]


_EFF = "/api/v1/settings/effective"


def test_effective_bearer_zorunlu(client: TestClient) -> None:
    # The token gate applies only to PROXIED requests; X-Forwarded-For makes the
    # request look proxied so the configured token is actually enforced.
    assert client.get(_EFF, headers={"X-Forwarded-For": "1.2.3.4"}).status_code == 401


def test_effective_birlesik_gorunum_ve_secret_gizli(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> None:
    """A single window returns the 4 config domains; a secret does not leak as a VALUE."""
    monkeypatch.setenv("AKANA_DATA_DIR", str(data_dir))
    monkeypatch.setenv("AKANA_TOKEN", "gizli-token")
    monkeypatch.setenv("CURSOR_API_KEY", "sk-sizmamali-123")
    with TestClient(create_app()) as c:
        r = c.get(_EFF, headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"server", "secrets_set", "llm", "voice", "runtime"}
        # cursor_api_key set → presence True; but its value does NOT appear in the response
        assert body["secrets_set"]["cursor_api_key"] is True
        assert "sk-sizmamali-123" not in json.dumps(body)
        # sub-domains are populated (sample fields)
        assert "port" in body["server"] and "claude_model" in body["server"]
        assert "chat_max_turns" in body["llm"]
        assert "tts_engine" in body["voice"]


def test_put_alan_bazli_dogrulama_turkce(client: TestClient) -> None:
    r = client.put(
        URL,
        headers=AUTH,
        json={"session_closer_idle_minutes": 99999, "bilinmeyen_anahtar": 1, "context_max_chars": 5},
    )
    assert r.status_code == 422
    err = r.json()["detail"]["error"]
    assert err["code"] == "VALIDATION"
    assert "at most" in err["fields"]["session_closer_idle_minutes"]
    assert "Unknown setting" in err["fields"]["bilinmeyen_anahtar"]
    # NO partial application: the valid field must also not have been written.
    g = client.get(URL, headers=AUTH).json()
    ctx = next(s for s in g["settings"] if s["key"] == "context_max_chars")
    assert ctx["source"] != "runtime"


def test_put_uygular_ve_dosyaya_yazar(client: TestClient, data_dir: Path) -> None:
    r = client.put(
        URL,
        headers=AUTH,
        json={"settings": {"context_max_chars": 4, "session_closer_enabled": False}},
    )
    assert r.status_code == 200
    body = r.json()
    assert sorted(body["changed"]) == ["context_max_chars", "session_closer_enabled"]
    assert body["restart_required"] == []
    by_key = {s["key"]: s for s in body["settings"]}
    assert by_key["context_max_chars"]["value"] == 4
    assert by_key["context_max_chars"]["source"] == "runtime"
    # session_closer_enabled is hidden → it still validates, lands in ``changed`` and on
    # disk (proof below), but is NOT echoed in the form payload.
    assert "session_closer_enabled" not in by_key
    disk = json.loads((data_dir / "runtime_settings.json").read_text(encoding="utf-8"))
    assert disk == {"context_max_chars": 4, "session_closer_enabled": False}


def test_put_telegram_restart_bayragi(client: TestClient) -> None:
    r = client.put(URL, headers=AUTH, json={"telegram_enabled": True})
    assert r.status_code == 200
    assert r.json()["restart_required"] == ["telegram_enabled"]


def test_reset_anahtari_dusurur(client: TestClient) -> None:
    client.put(URL, headers=AUTH, json={"context_max_chars": 4})
    r = client.post(f"{URL}/reset/context_max_chars", headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["reset"] == "context_max_chars" and body["removed"] is True
    ctx = next(s for s in body["settings"] if s["key"] == "context_max_chars")
    assert ctx["source"] != "runtime"
    assert client.post(f"{URL}/reset/yok-boyle-ayar", headers=AUTH).status_code == 404


def test_put_upload_siniri_store_dusurur(client: TestClient) -> None:
    sentinel = object()
    client.app.state.image_store = sentinel
    client.app.state.file_service = sentinel
    r = client.put(URL, headers=AUTH, json={"upload_max_mb": 2, "file_roots": "/tmp"})
    assert r.status_code == 200
    assert client.app.state.image_store is None
    assert client.app.state.file_service is None


def test_put_wake_threshold_canli_restartsiz(client: TestClient) -> None:
    """A wake_threshold with settings_attr must be applied to the LIVE snapshot on PUT — NO restart.

    Regression: when PUT only wrote to the store and did not refresh the FROZEN
    app.state.settings, the voice/wake score and /voice/config read the old (stale) threshold;
    the slider had no effect until a restart. Now PUT rebuilds app.state.settings.
    """
    # The startup value (no env → schema default 0.15) is pulled to a different threshold.
    before = client.app.state.settings.wake_threshold
    assert before != 0.42  # does not collide with the 0.15 default
    r = client.put(URL, headers=AUTH, json={"wake_threshold": 0.42})
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] == ["wake_threshold"]
    assert body["restart_required"] == []  # restart-free setting
    # 1) The FROZEN snapshot the live consumer reads is current — without a restart.
    assert client.app.state.settings.wake_threshold == 0.42
    # 2) The slider's data source /voice/wake/config also shows the current value.
    cfg = client.get("/api/v1/voice/wake/config", headers=AUTH).json()
    assert cfg["threshold"] == 0.42
    # 3) The broader surface /voice/config (the slider's initial load) is also current.
    vc = client.get("/api/v1/voice/config", headers=AUTH).json()
    assert vc["wake"]["threshold"] == 0.42


def test_put_wake_min_frames_canli_restartsiz(client: TestClient) -> None:
    """The sustain gate (wake_min_frames) must apply to the LIVE snapshot on PUT — NO restart.

    Same store>env>default + rebuild path as wake_threshold: the voice-panel slider PUTs
    this key and voice/wake.py reads settings.wake_min_frames per poll, so the gate tightens
    without a restart. Both config surfaces that seed the slider must reflect it.
    """
    before = client.app.state.settings.wake_min_frames
    assert before != 6  # does not collide with the default (3)
    r = client.put(URL, headers=AUTH, json={"wake_min_frames": 6})
    assert r.status_code == 200
    body = r.json()
    assert body["changed"] == ["wake_min_frames"]
    assert body["restart_required"] == []
    assert client.app.state.settings.wake_min_frames == 6
    cfg = client.get("/api/v1/voice/wake/config", headers=AUTH).json()
    assert cfg["min_frames"] == 6
    vc = client.get("/api/v1/voice/config", headers=AUTH).json()
    assert vc["wake"]["min_frames"] == 6


def test_put_wake_min_frames_rejects_out_of_range(client: TestClient) -> None:
    """min/max (1..10) validation guards the int; an over-range value is a 4xx, not applied."""
    r = client.put(URL, headers=AUTH, json={"wake_min_frames": 99})
    assert r.status_code >= 400
    assert client.app.state.settings.wake_min_frames != 99


def test_reset_wake_threshold_snapshot_geri_doner(client: TestClient) -> None:
    """Reset must return the LIVE snapshot to env/default (a fresh base is rebuilt)."""
    default_thr = client.app.state.settings.wake_threshold
    client.put(URL, headers=AUTH, json={"wake_threshold": 0.42})
    assert client.app.state.settings.wake_threshold == 0.42
    r = client.post(f"{URL}/reset/wake_threshold", headers=AUTH)
    assert r.status_code == 200
    assert r.json()["removed"] is True
    # Because a fresh base is built instead of patching the old overridden snapshot,
    # the deleted key is no longer in the store → the value returns to env/default.
    assert client.app.state.settings.wake_threshold == default_thr
