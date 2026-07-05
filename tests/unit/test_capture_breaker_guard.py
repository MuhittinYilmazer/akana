"""audit C31: the inline/voice memory-capture path must honor the provider circuit
breaker, exactly like the streaming and background capture paths do.

When the active provider's breaker is OPEN/HALF_OPEN (from parallel rate-limited
turns), a voice/blocking turn must NOT fire the 2nd-pass capture LLM call — doing so
hammers the half-recovered provider and burns its single recovery probe.
"""

from __future__ import annotations

import asyncio
import types
from pathlib import Path

import pytest

from akana_server.api.routes.chat import chat_state
from akana_server.api.routes.chat import persist as persist_mod
from akana_server.config import load_settings


def _request(tmp_path: Path) -> types.SimpleNamespace:
    settings = load_settings()  # real Settings — _stage_memory_captures isinstance-checks it
    app = types.SimpleNamespace(state=types.SimpleNamespace(settings=settings))
    return types.SimpleNamespace(app=app)


def _install_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("CURSOR_API_KEY", "")


def test_capture_skipped_when_breaker_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _install_env(monkeypatch, tmp_path)
    request = _request(tmp_path)

    calls: list[int] = []

    async def _fake_propose(*_a: object, **_k: object) -> list:
        calls.append(1)
        return []

    monkeypatch.setattr(
        "akana_server.api.routes.chat.propose_memory_captures",
        _fake_propose,
        raising=False,
    )
    # Breaker OPEN → the 2nd LLM call must be skipped entirely.
    monkeypatch.setattr(chat_state, "_cursor_breaker_open", lambda _s=None: True)

    out = asyncio.run(
        persist_mod._stage_memory_captures(
            request, conversation_id="c1", user_text="u", assistant_text="a"
        )
    )
    assert out == []
    assert calls == []  # propose_memory_captures (the 2nd LLM call) did NOT fire


def test_capture_runs_when_breaker_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Control: with the breaker CLOSED the guard is inert and capture proceeds
    (here the stubbed proposer returns no candidates, so nothing is staged)."""
    _install_env(monkeypatch, tmp_path)
    request = _request(tmp_path)

    calls: list[int] = []

    async def _fake_propose(*_a: object, **_k: object) -> list:
        calls.append(1)
        return []

    monkeypatch.setattr(
        "akana_server.api.routes.chat.propose_memory_captures",
        _fake_propose,
        raising=False,
    )
    monkeypatch.setattr(chat_state, "_cursor_breaker_open", lambda _s=None: False)

    out = asyncio.run(
        persist_mod._stage_memory_captures(
            request, conversation_id="c1", user_text="u", assistant_text="a"
        )
    )
    assert out == []
    assert calls == [1]  # breaker closed → the capture path ran
