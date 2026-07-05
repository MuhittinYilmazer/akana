"""Fresh-install wake pipeline: shared feature-model ensure + honest config gate.

Two coupled bugs made a freshly-set-up wake unusable:

  1. ``openwakeword.model.Model()`` also loads the SHARED feature models
     (melspectrogram + audio embedding), which are NOT shipped in the wheel. For the
     bundled "hey_akana" file path nothing ever downloaded them, so the first wake use
     503s with NO_SUCHFILE. ``_ensure_oww_feature_models`` fetches them on demand, and
     a failed (offline) fetch is negative-cached so the 300 ms poll does not re-download
     and re-log on every tick.
  2. ``/voice/wake/config`` reported ``enabled=true`` without verifying the model loads,
     so the frontend disabled the browser fallback and wake had zero working path. The
     gate now attempts the (cached) load and reports ``enabled=false`` + a hint on failure.

``openwakeword`` internals are monkeypatched → these run without the package installed.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

# These tests monkeypatch openWakeWord internals via mock.patch("openwakeword..."),
# which imports the package to patch it. On a minimal install (setup --voice none, as CI
# runs) openwakeword is absent, so skip the module rather than error at patch time.
pytest.importorskip("openwakeword")

from akana_server.api.app import create_app
from akana_server.runtime_settings import reset_runtime_stores
from akana_server.voice import wake


@pytest.fixture(autouse=True)
def _reset_feature_cache():
    """Every test starts from a pristine negative-cache + no background fetch in flight."""
    wake._feature_state = wake._BackoffState()
    wake._feature_bg_inflight.clear()
    wake._wake_model_state.clear()
    wake._wake_model_bg_inflight.clear()
    wake._models.clear()
    yield
    wake._feature_state = wake._BackoffState()
    wake._feature_bg_inflight.clear()
    wake._wake_model_state.clear()
    wake._wake_model_bg_inflight.clear()


def test_ensure_feature_models_noop_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the feature files already exist, ensure must not touch the network."""
    monkeypatch.setattr(wake, "_feature_models_present", lambda _md, _fw: True)
    with mock.patch("openwakeword.utils.download_models") as dl:
        wake._ensure_oww_feature_models("onnx")
    dl.assert_not_called()


def test_ensure_feature_models_downloads_targeted_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh install fetches the feature models via a NON-EMPTY sentinel name.

    ``download_models([])`` would pull every official pretrained model — the ensure step
    must pass a name that matches no official model so only the feature (+VAD) files land.
    """
    present = {"v": False}
    monkeypatch.setattr(
        wake, "_feature_models_present", lambda _md, _fw: present["v"]
    )

    def _fake_download(names, target_directory=None):  # noqa: ARG001
        assert names == ["__none__"], names  # sentinel, NOT [] (would fetch all models)
        present["v"] = True

    with mock.patch("openwakeword.utils.download_models", side_effect=_fake_download) as dl:
        wake._ensure_oww_feature_models("onnx")
    dl.assert_called_once()


def test_ensure_feature_models_offline_is_negative_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An offline fetch fails once, then short-circuits — no per-poll download/log spam."""
    monkeypatch.setattr(wake, "_feature_models_present", lambda _md, _fw: False)

    def _boom(names, target_directory=None):  # noqa: ARG001
        raise OSError("offline: name resolution failed")

    with mock.patch("openwakeword.utils.download_models", side_effect=_boom) as dl:
        for _ in range(5):  # simulate five 300 ms polls
            with pytest.raises(wake.WakeError) as ei:
                wake._ensure_oww_feature_models("onnx")
            assert ei.value.status_code == 503
    # Only the FIRST poll hit the network; the rest were served from the negative-cache.
    assert dl.call_count == 1
    assert wake._feature_state.backoff >= wake._BACKOFF_MIN


def test_ensure_feature_models_download_succeeds_but_files_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 'successful' download that still leaves the framework files absent 503s, not True."""
    monkeypatch.setattr(wake, "_feature_models_present", lambda _md, _fw: False)
    with mock.patch("openwakeword.utils.download_models"):  # no-op → files still missing
        with pytest.raises(wake.WakeError) as ei:
            wake._ensure_oww_feature_models("onnx")
    assert ei.value.status_code == 503


# ── WAKE-2: the config gate verifies the model actually loads ────────────────


AUTH = {"Authorization": "Bearer gizli-token"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    reset_runtime_stores()
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("AKANA_TOKEN", "gizli-token")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    app = create_app()
    with TestClient(app) as c:
        yield c
    reset_runtime_stores()


def test_wake_config_enabled_false_when_model_wont_load(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Feature models present but Model() init 503s → enabled=false + a fix hint,
    so the frontend keeps the browser fallback instead of promising a dead server path."""
    import akana_server.api.routes.voice as voice_routes

    def _fail(*_a, **_k):
        raise wake.WakeError("wake model init failed: NO_SUCHFILE", status_code=503)

    monkeypatch.setattr(voice_routes, "feature_models_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(voice_routes, "get_oww_model_from_disk", _fail)
    monkeypatch.setattr(voice_routes, "_has", lambda _m: True)  # pretend oWW installed
    cfg = client.get("/api/v1/voice/wake/config", headers=AUTH).json()
    assert cfg["enabled"] is False
    assert cfg["status"] == "error"
    assert "voice-full" in cfg["hint"]


def test_wake_config_enabled_true_when_model_loads(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Feature models present + model loads → enabled=true (server scoring is the wake source)."""
    import akana_server.api.routes.voice as voice_routes

    monkeypatch.setattr(voice_routes, "feature_models_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(
        voice_routes, "get_oww_model_from_disk", lambda *_a, **_k: object()
    )
    monkeypatch.setattr(voice_routes, "_has", lambda _m: True)
    cfg = client.get("/api/v1/voice/wake/config", headers=AUTH).json()
    assert cfg["enabled"] is True
    assert cfg["status"] == "ready"


def test_wake_config_never_blocks_on_download_and_triggers_background(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fresh install (feature models absent): config must NOT download inline.

    It returns enabled=false + status=preparing immediately and fires a ONE-SHOT
    background fetch; ``download_models`` is never awaited on the request path, and the
    disk-only load path is never even attempted (files are absent)."""
    import akana_server.api.routes.voice as voice_routes

    monkeypatch.setattr(voice_routes, "feature_models_ready", lambda *_a, **_k: False)
    monkeypatch.setattr(voice_routes, "_has", lambda _m: True)

    # The disk-only loader must NOT be called when the feature models are absent.
    def _should_not_run(*_a, **_k):
        raise AssertionError("get_oww_model_from_disk called while feature models absent")

    monkeypatch.setattr(voice_routes, "get_oww_model_from_disk", _should_not_run)

    triggered: list[str] = []
    monkeypatch.setattr(
        voice_routes,
        "trigger_feature_models_download",
        lambda framework: triggered.append(framework),
    )

    cfg = client.get("/api/v1/voice/wake/config", headers=AUTH).json()
    assert cfg["enabled"] is False
    assert cfg["status"] == "preparing"
    assert cfg["preparing"] == "feature"  # feature models are the pending download
    assert triggered == ["onnx"]  # background fetch fired exactly once for the framework


def test_wake_config_reports_error_when_feature_download_repeatedly_failed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A feature-model download parked on backoff after repeated failures (offline) is
    reported as status=error + load_error, NOT perpetual "preparing" — so the frontend
    stops polling forever and surfaces the browser fallback."""
    import akana_server.api.routes.voice as voice_routes

    monkeypatch.setattr(voice_routes, "feature_models_ready", lambda *_a, **_k: False)
    monkeypatch.setattr(voice_routes, "wake_model_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(voice_routes, "_has", lambda _m: True)
    monkeypatch.setattr(
        voice_routes, "trigger_feature_models_download", lambda _framework: None
    )
    # Simulate a background fetch that has already failed past the threshold.
    wake._feature_state.failures = voice_routes._WAKE_DOWNLOAD_FAIL_THRESHOLD
    wake._feature_state.last_error = "could not download the wake feature models (offline)"

    cfg = client.get("/api/v1/voice/wake/config", headers=AUTH).json()
    assert cfg["enabled"] is False
    assert cfg["status"] == "error"
    assert cfg["preparing"] is None
    assert "offline" in cfg["load_error"]


def test_wake_config_preparing_hint_names_wake_model_when_only_it_downloads(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Feature models on disk but the (bare pretrained) wake model still downloading:
    the hint must name the WAKE model, not the feature models (the old single hint always
    said 'feature models', which was misleading in this case)."""
    import akana_server.api.routes.voice as voice_routes

    monkeypatch.setattr(voice_routes, "feature_models_ready", lambda *_a, **_k: True)
    monkeypatch.setattr(voice_routes, "wake_model_ready", lambda *_a, **_k: False)
    monkeypatch.setattr(voice_routes, "_has", lambda _m: True)
    monkeypatch.setattr(
        voice_routes, "trigger_wake_model_download", lambda _m: None
    )

    # The disk-only load path (feat_ready + not wake_ready branch) reports the wake model
    # as still preparing (503, not a hard 400).
    def _preparing(*_a, **_k):
        raise wake.WakeError("wake model not on disk yet (preparing).", status_code=503)

    monkeypatch.setattr(voice_routes, "get_oww_model_from_disk", _preparing)

    cfg = client.get("/api/v1/voice/wake/config", headers=AUTH).json()
    assert cfg["enabled"] is False
    assert cfg["status"] == "preparing"
    assert cfg["preparing"] == "wake"
    assert "wake model" in cfg["hint"]
    assert "feature models" not in cfg["hint"]


def test_trigger_feature_models_download_single_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A poll storm spawns at most ONE background fetch while one is in flight.

    The download step is blocked on an event so it stays 'in flight'; a second trigger
    while it runs must be a no-op. Releasing the event lets the thread finish and clear
    the flag so a later trigger can fire again."""
    import threading as _threading

    started = _threading.Event()
    release = _threading.Event()
    calls = {"n": 0}

    def _blocking_ensure(_framework):
        calls["n"] += 1
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr(wake, "_ensure_oww_feature_models", _blocking_ensure)

    wake.trigger_feature_models_download("onnx")
    assert started.wait(timeout=5), "first background fetch never started"
    # Second trigger WHILE the first is in flight → no-op (single-flight).
    wake.trigger_feature_models_download("onnx")
    assert calls["n"] == 1

    release.set()
    # Wait for the in-flight flag to clear (thread finally-block).
    for _ in range(500):
        if not wake._feature_bg_inflight:
            break
        time.sleep(0.01)
    assert not wake._feature_bg_inflight


def test_trigger_feature_models_download_parked_by_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a prior failure parked retries (negative-cache), no new thread is spawned."""
    called = {"n": 0}

    def _ensure(_framework):
        called["n"] += 1

    monkeypatch.setattr(wake, "_ensure_oww_feature_models", _ensure)
    wake._feature_state.next_retry = time.monotonic() + 300.0  # parked

    wake.trigger_feature_models_download("onnx")
    time.sleep(0.05)  # give any (wrongly) spawned thread a chance to run
    assert called["n"] == 0


def test_get_oww_model_from_disk_503s_when_features_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The disk-only loader must NOT download: absent feature models → immediate 503."""
    # Treat the wake model itself as on-disk so this isolates the feature-model branch
    # (the loader now checks wake-model readiness independently — see VB-5).
    monkeypatch.setattr(wake, "_classify_wake_model", lambda _m: "file")
    monkeypatch.setattr(wake, "_feature_models_present", lambda _md, _fw: False)
    with mock.patch("openwakeword.utils.download_models") as dl:
        with pytest.raises(wake.WakeError) as ei:
            wake.get_oww_model_from_disk("hey_akana.onnx", "onnx")
    assert ei.value.status_code == 503
    dl.assert_not_called()  # disk-only path never hits the network
