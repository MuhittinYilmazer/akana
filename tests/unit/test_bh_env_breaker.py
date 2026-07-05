"""Bug-hunt regressions: foreign-provider env leak + breaker disconnect miscount.

Two independent fixes, mirroring existing unit-test patterns (SimpleNamespace
Settings double + monkeypatch.setenv from test_driver_cursor.py; breaker
snapshot()["failures"] assertions from test_network_engine.py). No real network
/ subprocess is spawned.

1. ``_bridge_env`` must NOT forward the user's foreign-provider API keys
   (OPENAI_API_KEY / GEMINI_API_KEY / GOOGLE_API_KEY) into the third-party
   Cursor Node bridge or the tool/MCP subprocesses it spawns (SEC2).
2. ``stream_breaker.__exit__`` must treat a consumer disconnect (GeneratorExit,
   like the existing CancelledError exclusion) as NOT a provider failure, so a
   disconnect burst never trips / re-opens the shared breaker.
"""

from __future__ import annotations

from types import SimpleNamespace

from akana_server.network.breaker import BreakerRegistry
from akana_server.network.config import NetworkConfig
from akana_server.network.guard import stream_breaker
from akana_server.orchestrator import cursor_provider


# --------------------------------------------------------------------------- #
# 1. Foreign-provider secrets are stripped from the Cursor bridge environment
# --------------------------------------------------------------------------- #
def test_bridge_env_strips_foreign_provider_keys(monkeypatch):
    """SEC2: OPENAI/GEMINI/GOOGLE keys the user set for their own tooling must
    not leak into the third-party Cursor bridge; CURSOR_API_KEY / PATH stay."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-xxx")
    monkeypatch.setenv("GEMINI_API_KEY", "gm-xxx")
    monkeypatch.setenv("GOOGLE_API_KEY", "goog-xxx")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    env = cursor_provider.bridge_env(
        SimpleNamespace(data_dir=None, cursor_api_key="ck-123")
    )

    assert "OPENAI_API_KEY" not in env, "OpenAI key must not leak into the Cursor SDK"
    assert "GEMINI_API_KEY" not in env, "Gemini key must not leak into the Cursor SDK"
    assert "GOOGLE_API_KEY" not in env, "Google key must not leak into the Cursor SDK"
    # Sanity: legitimate env survives the strip (denylist, not allowlist).
    assert env["CURSOR_API_KEY"] == "ck-123"
    assert env.get("PATH") == "/usr/bin:/bin"


# --------------------------------------------------------------------------- #
# 2. stream_breaker: a consumer disconnect (GeneratorExit) is not a failure
# --------------------------------------------------------------------------- #
def _cfg() -> NetworkConfig:
    # breaker_enabled is a derived property (breaker_threshold > 0), not a field.
    return NetworkConfig(breaker_threshold=10, breaker_cooldown=100.0)


def test_stream_breaker_generatorexit_does_not_count_failure():
    """A client disconnect (GeneratorExit) inside the stream context must NOT
    increment the breaker failure count — same as the existing CancelledError
    exclusion (a disconnect burst must never trip the breaker)."""
    reg = BreakerRegistry(threshold=10, cooldown=100.0)

    try:
        with stream_breaker("cursor", _cfg(), registry=reg):
            raise GeneratorExit()
    except GeneratorExit:
        pass  # re-raised (not swallowed) — expected

    assert reg.get("cursor").snapshot()["failures"] == 0


def test_stream_breaker_cancellederror_does_not_count_failure():
    """Mirror of the existing CancelledError exclusion (baseline the GeneratorExit
    fix is modelled on): still zero failures."""
    import asyncio

    reg = BreakerRegistry(threshold=10, cooldown=100.0)

    try:
        with stream_breaker("cursor", _cfg(), registry=reg):
            raise asyncio.CancelledError()
    except asyncio.CancelledError:
        pass

    assert reg.get("cursor").snapshot()["failures"] == 0


def test_stream_breaker_real_error_still_counts_failure():
    """Regression guard: a genuine provider error (not a disconnect) MUST still
    be recorded — the GeneratorExit exclusion must not swallow real faults."""
    reg = BreakerRegistry(threshold=10, cooldown=100.0)

    try:
        with stream_breaker("cursor", _cfg(), registry=reg):
            raise RuntimeError("provider blew up")
    except RuntimeError:
        pass

    assert reg.get("cursor").snapshot()["failures"] == 1
