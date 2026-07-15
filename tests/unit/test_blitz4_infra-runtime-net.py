"""Bug blitz 4 — infra-runtime-net area regression locks.

Three verified bugs:

1. session_closer cron holds a FROZEN startup Settings snapshot, so a runtime
   override baked in at boot survives a later Reset (get_runtime falls back to the
   stale snapshot attr) — the cron keeps the old cadence forever.
2. reload_connectors drains the router while the Telegram poller keeps running,
   so messages accepted (offset-confirmed) during the drain window are enqueued
   into the soon-to-be-discarded shared queue and silently lost.
3. WAKE_MIN_FRAMES env is not bounds-checked (unlike WAKE_THRESHOLD one line
   above), so an out-of-range value silently disables wake.
"""

from __future__ import annotations

import asyncio
import dataclasses
from types import SimpleNamespace

import pytest

from akana_server import config as cfg
from akana_server.config import load_settings
from akana_server.connectors import service as conn_service
from akana_server.connectors.registry import ConnectorRegistry
from akana_server.orchestrator import session_closer_service
from akana_server.runtime_settings.store import reset_runtime_stores


# -- 1. session_closer cron reads live app.state.settings, not a frozen snapshot --


def test_session_closer_cron_uses_live_settings_after_reset(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After an override that was baked into the startup snapshot is Reset, the cron
    must resolve against the freshly rebuilt app.state.settings — not the stale
    startup-stamped Settings it captured in its poll loop."""
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AKANA_SESSION_CLOSER_INTERVAL", raising=False)
    reset_runtime_stores()
    base = load_settings()
    stale = dataclasses.replace(base, session_closer_interval=3600.0)  # startup-baked override
    fresh = dataclasses.replace(base, session_closer_interval=300.0)  # post-reset env/default

    app = SimpleNamespace(state=SimpleNamespace(settings=stale))

    captured: dict = {}

    def fake_poll_loop(settings, **kwargs):
        captured["settings"] = settings
        captured.update(kwargs)

        async def _noop():
            return None

        return _noop()

    def fake_start_task(app_, attr, coro):
        coro.close()  # do not schedule; we only inspect the wiring

    monkeypatch.setattr(session_closer_service, "_poll_loop", fake_poll_loop)
    monkeypatch.setattr(session_closer_service, "_start_task", fake_start_task)

    session_closer_service.start_session_closer(app)
    interval_fn = captured["interval_seconds"]
    positional = captured["settings"]

    # Baseline: the cron sees the (baked) startup interval.
    assert interval_fn(positional) == 3600.0
    # Reset → rebuild_app_settings swaps in a fresh snapshot (store key removed).
    app.state.settings = fresh
    # The cron must now use the LIVE default, not the frozen 3600 snapshot.
    assert interval_fn(positional) == 300.0


# -- 2. reload_connectors must not drop poller messages during the drain window --


class _FakeConnector:
    connector_id = "fake"

    def __init__(self) -> None:
        self.produced = 0
        self.stopped = False
        self._task = None
        self._inbound = None

    async def start(self, inbound) -> None:
        self._inbound = inbound
        self._task = asyncio.create_task(self._produce())

    async def _produce(self) -> None:
        # Models the Telegram poller: keeps enqueuing offset-confirmed messages
        # until stopped.
        while True:
            await self._inbound.put(f"m{self.produced}")
            self.produced += 1
            await asyncio.sleep(0.001)

    async def stop(self) -> None:
        self.stopped = True
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


class _FakeRouter:
    def __init__(self, inbound) -> None:
        self._inbound = inbound
        self.processed: list = []
        self._task = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._consume())

    async def _consume(self) -> None:
        while True:
            self.processed.append(await self._inbound.get())

    async def drain(self, timeout: float = 30.0) -> bool:
        # Mirrors InboundRouter.drain: cancel the intake pump, ONE-TIME sweep of the
        # shared queue, then WAIT for an in-flight worker (modeled by the sleep). A
        # live poller keeps enqueuing during that wait.
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        try:
            while True:
                self.processed.append(self._inbound.get_nowait())
        except asyncio.QueueEmpty:
            pass
        await asyncio.sleep(0.05)  # in-flight LLM turn window
        return True

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def test_reload_connectors_does_not_drop_messages_during_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run() -> None:
        registry = ConnectorRegistry()
        conn = _FakeConnector()
        registry.register(conn)
        await registry.start_all()
        router = _FakeRouter(registry.inbound)
        router.start()
        app = SimpleNamespace(
            state=SimpleNamespace(
                settings=SimpleNamespace(data_dir=None),
                connector_registry=registry,
                connector_router=router,
            )
        )
        # start_connectors rebuild must be a no-op: a fresh empty registry has no
        # connectors, so start_connectors returns early after swapping app.state.
        monkeypatch.setattr(conn_service, "build_registry", lambda s: ConnectorRegistry())

        await asyncio.sleep(0.01)  # let the poller fill the queue a little
        await conn_service.reload_connectors(app)

        # The OLD shared queue must be fully drained. Any message the poller enqueued
        # during the drain window that is left behind is permanently lost when
        # start_connectors swaps in a brand-new queue.
        assert registry.inbound.qsize() == 0
        assert conn.stopped is True

    asyncio.run(run())


# -- 3. WAKE_MIN_FRAMES env must be bounds-checked like WAKE_THRESHOLD --


def test_wake_min_frames_env_out_of_range_falls_to_default(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WAKE_MIN_FRAMES", "50")  # acoustically unreachable → silent wake death
    assert load_settings().wake_min_frames == 3  # DEFAULTS["wake_min_frames"], not 50


def test_wake_min_frames_env_below_min_falls_to_default(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WAKE_MIN_FRAMES", "0")  # 0 < schema min=1
    assert load_settings().wake_min_frames == 3


def test_wake_min_frames_env_in_range_preserved(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid in-range value must still be honored (no regression)."""
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("WAKE_MIN_FRAMES", "5")
    assert load_settings().wake_min_frames == 5
