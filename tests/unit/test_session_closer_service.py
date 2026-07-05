"""SessionCloser server cron — run_once bridge + lifespan wiring (M3.2)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from akana.memory import Memory
from akana_server.config import load_settings
from akana_server.orchestrator import session_closer_service


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    monkeypatch.setenv("AKANA_SESSION_CLOSER_IDLE_MINUTES", "0")
    return tmp_path


def _seed(mem: Memory, cid: str) -> None:
    mem.remember_turn(role="user", conversation_id=cid, text="yarın diş randevum var")
    mem.remember_turn(role="assistant", conversation_id=cid, text="not ettim")
    mem.remember_turn(role="user", conversation_id=cid, text="süt almayı unutma")
    mem.remember_turn(role="assistant", conversation_id=cid, text="ekledim")


def _wire_memory(monkeypatch: pytest.MonkeyPatch, mem: Memory) -> None:
    monkeypatch.setattr(session_closer_service, "get_memory_core", lambda _dir: mem)


def _wire_llm(monkeypatch: pytest.MonkeyPatch, reply: str = "Özet: diş randevusu.") -> list[str]:
    prompts: list[str] = []

    async def fake_complete(settings, prompt, **kwargs):
        prompts.append(prompt)
        return reply, {"prompt_tokens": 1}

    monkeypatch.setattr(
        session_closer_service.llm_dispatch, "complete_chat_with_usage", fake_complete
    )
    return prompts


def test_run_once_stages_summary_and_is_idempotent(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mem = Memory.for_data_dir(env)
    _seed(mem, "conv-cron-1")
    _wire_memory(monkeypatch, mem)
    prompts = _wire_llm(monkeypatch)
    settings = load_settings()

    async def run() -> None:
        assert await session_closer_service.run_once(settings) == 1
        pending = mem.staging.list_pending()
        assert len(pending) == 1
        assert pending[0].extractor == "session_closer"
        assert pending[0].value.startswith("Özet:")
        assert "diş randevum" in prompts[0]
        # A second scan over the same turns is a no-op — the inbox does not bloat.
        assert await session_closer_service.run_once(settings) == 0
        assert len(mem.staging.list_pending()) == 1

    asyncio.run(run())


def test_run_once_auto_promotes_when_allow_direct(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When approval-free remembering (allow_direct) is ON: the synthesis does not wait in
    the inbox, it is promoted directly to persistent knowledge. Regression lock for the toggle bug."""
    from akana.memory.settings import MemorySettings, save_memory_settings

    save_memory_settings(env, MemorySettings(allow_direct=True))
    mem = Memory.for_data_dir(env)
    _seed(mem, "conv-cron-direct")
    _wire_memory(monkeypatch, mem)
    _wire_llm(monkeypatch)
    settings = load_settings()

    async def run() -> None:
        # the close count (return) is unchanged; promote empties the inbox.
        assert await session_closer_service.run_once(settings) == 1
        assert mem.staging.list_pending() == []  # did not land in the inbox
        facts = mem.semantic.list_facts()
        assert any(f.value.startswith("Özet:") for f in facts)  # written to persistent store

    asyncio.run(run())


def test_run_once_respects_session_summary_toggle(
    env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Memory Studio 'session summarization' toggle OFF → the scan stages nothing and the
    summarizer is never called, independent of the runtime master switch."""
    from akana.memory.settings import MemorySettings, save_memory_settings

    save_memory_settings(env, MemorySettings(session_summary=False))
    mem = Memory.for_data_dir(env)
    _seed(mem, "conv-cron-sessum-off")
    _wire_memory(monkeypatch, mem)
    prompts = _wire_llm(monkeypatch)
    settings = load_settings()

    async def run() -> None:
        assert await session_closer_service.run_once(settings) == 0
        assert mem.staging.list_pending() == []
        assert prompts == []  # summarizer never invoked

    asyncio.run(run())


def test_run_once_empty_store_is_noop(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mem = Memory.for_data_dir(env)
    _wire_memory(monkeypatch, mem)
    _wire_llm(monkeypatch)

    async def run() -> None:
        assert await session_closer_service.run_once(load_settings()) == 0

    asyncio.run(run())


def test_run_once_swallows_llm_failure(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mem = Memory.for_data_dir(env)
    _seed(mem, "conv-cron-2")
    _wire_memory(monkeypatch, mem)

    async def boom(settings, prompt, **kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(
        session_closer_service.llm_dispatch, "complete_chat_with_usage", boom
    )

    async def run() -> None:
        assert await session_closer_service.run_once(load_settings()) == 0
        assert mem.staging.list_pending() == []

    asyncio.run(run())


def test_lifespan_starts_and_stops_task(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from akana_server.api.app import create_app

    monkeypatch.setenv("AKANA_SESSION_CLOSER_INTERVAL", "60")
    app = create_app()
    with TestClient(app):
        task = getattr(app.state, "session_closer_task", None)
        assert isinstance(task, asyncio.Task)
        assert not task.done()
    assert task.cancelled() or task.done()


def test_lifespan_respects_disable_flag(env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RuntimeSettings contract: when disabled in env the loop is still SET UP
    (so it can be enabled from settings without a restart) but the activity gate is
    closed — run_once is not called."""
    from akana_server.api.app import create_app

    monkeypatch.setenv("AKANA_SESSION_CLOSER_ENABLED", "0")
    app = create_app()
    with TestClient(app):
        task = getattr(app.state, "session_closer_task", None)
        assert isinstance(task, asyncio.Task)
        assert not session_closer_service.session_closer_active(app.state.settings)
