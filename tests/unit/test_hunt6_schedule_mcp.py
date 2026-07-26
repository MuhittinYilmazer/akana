"""hunt-6 group E — schedule engine / store / orchestrator process + MCP regressions.

Every test here pins ONE contract that was broken and is cheap to break again:

* a delivered schedule run is never replayed because its bookkeeping write failed;
* the schedule store is never touched from the event loop (a peer process holding
  the cross-process lock must not freeze the server), and the off-loop wait is capped;
* the boot-time orphan reaper never kills a process it cannot identify as ours;
* wall-clock schedule math follows the HOST zone, not a hardcoded +03:00;
* the connector create-gate lets an explicit stored ``false`` beat the env kill switch;
* codex does not broadcast one MCP server's secrets into every other MCP child;
* the connector inbound turn does not read history/persona/catalog on the loop;
* the consolidation cron resolves its gate against LIVE settings, not the boot snapshot;
* the MCP bridge enters and exits its anyio scopes in ONE task.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from akana_server.orchestrator import llm_dispatch, memory_tools
from akana_server.schedule import engine
from akana_server.schedule.model import Delivery
from akana_server.schedule.store import TR_TZ, ScheduleStore, to_iso

T0 = datetime(2026, 7, 11, 10, 0, tzinfo=TR_TZ)


def _settings(tmp_path):
    return SimpleNamespace(data_dir=tmp_path)


def _stub_llm(monkeypatch, fn):
    monkeypatch.setattr(llm_dispatch, "complete_chat_aggregated", fn)
    monkeypatch.setattr(memory_tools, "mcp_servers_payload", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _clear_pending_advance():
    """The parked-advance registry is process-global; keep tests independent."""
    engine._PENDING_ADVANCE.clear()
    yield
    engine._PENDING_ADVANCE.clear()


# --------------------------------------------------------------------------- #
# E-2 — a delivered run must never be replayed because mark_ran could not write
# --------------------------------------------------------------------------- #


def test_failed_advance_does_not_replay_the_delivery(tmp_path, monkeypatch):
    """A store write that fails AFTER delivery must not re-fire the schedule.

    Before the fix ``mark_ran`` ran outside any try, so a PermissionError on
    schedules.json left ``enabled=True`` + ``next_run_at`` in the past: the same
    briefing was delivered — and a full LLM turn burned — on every 30s poll."""
    calls = {"llm": 0}

    async def fake(settings, prompt, **kw):
        calls["llm"] += 1
        return (f"result {calls['llm']}", {}, None)

    _stub_llm(monkeypatch, fake)
    delivered: list[tuple] = []
    monkeypatch.setattr(
        engine, "_append_turn_pair",
        lambda dd, cid, prompt, result: delivered.append((cid, result)),
    )
    store = ScheduleStore(tmp_path)
    store.create(
        title="Brief", prompt="brief me", kind="once", when=to_iso(T0),
        delivery=Delivery(mode="thread"), now=T0,
    )

    def boom(*a, **k):
        raise PermissionError("[WinError 5] Access is denied")

    monkeypatch.setattr(ScheduleStore, "_write", boom)

    convs = SimpleNamespace(
        create=lambda title=None: SimpleNamespace(id="conv-1"), get=lambda cid: None
    )
    s = _settings(tmp_path)
    for _ in range(3):
        asyncio.run(engine.run_due_schedules(s, conversations=convs, now=T0))

    assert calls["llm"] == 1, "the schedule re-fired after its advance failed to persist"
    assert len(delivered) == 1, "the same result was delivered more than once"


def test_parked_advance_is_retried_and_then_the_row_is_spent(tmp_path, monkeypatch):
    """Once the store recovers, the parked advance lands WITHOUT re-running the turn."""
    calls = {"llm": 0}

    async def fake(settings, prompt, **kw):
        calls["llm"] += 1
        return ("body", {}, None)

    _stub_llm(monkeypatch, fake)
    monkeypatch.setattr(engine, "_append_turn_pair", lambda *a: None)
    store = ScheduleStore(tmp_path)
    item = store.create(
        title="Brief", prompt="p", kind="once", when=to_iso(T0),
        delivery=Delivery(mode="thread"), now=T0,
    )
    convs = SimpleNamespace(
        create=lambda title=None: SimpleNamespace(id="conv-1"), get=lambda cid: None
    )
    s = _settings(tmp_path)

    real_write = ScheduleStore._write
    monkeypatch.setattr(
        ScheduleStore, "_write",
        lambda self, items: (_ for _ in ()).throw(PermissionError("locked")),
    )
    asyncio.run(engine.run_due_schedules(s, conversations=convs, now=T0))
    assert calls["llm"] == 1
    assert store.get(item.id).enabled is True  # the advance did not land

    monkeypatch.setattr(ScheduleStore, "_write", real_write)
    asyncio.run(engine.run_due_schedules(s, conversations=convs, now=T0))
    assert calls["llm"] == 1, "the recovered sweep re-ran a turn that already ran"
    assert store.get(item.id).enabled is False  # spent, recorded, never delivered twice


# --------------------------------------------------------------------------- #
# E-3 — the store's cross-process lock must never be taken on the event loop
# --------------------------------------------------------------------------- #


def test_store_calls_do_not_block_the_event_loop(tmp_path, monkeypatch):
    """A slow store call must not stall the loop (a peer process CAN hold its lock).

    ``ScheduleStore.due`` guards its read with ``json_store.cross_process_lock``,
    which spins on an OS advisory lock up to 10s. On the loop that is a whole-server
    freeze: every request, every SSE/WS stream."""
    hold = 0.4

    def slow_due(self, now):
        time.sleep(hold)
        return []

    monkeypatch.setattr(ScheduleStore, "due", slow_due)

    async def run():
        gaps: list[float] = []

        async def heartbeat():
            last = time.monotonic()
            while True:
                await asyncio.sleep(0.01)
                nownow = time.monotonic()
                gaps.append(nownow - last)
                last = nownow

        beat = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.05)
        await engine.run_due_schedules(_settings(tmp_path), now=T0)
        # Let the heartbeat run once more: a gap is only RECORDED on the tick after
        # the stall, so cancelling immediately would hide the very thing under test.
        await asyncio.sleep(0.05)
        beat.cancel()
        return max(gaps)

    worst = asyncio.run(run())
    assert worst < hold / 2, f"event loop was blocked for {worst * 1000:.0f}ms by a store read"


def test_off_loop_store_call_is_bounded(tmp_path, monkeypatch):
    """An unbounded off-loop wait only moves the freeze — the call has a ceiling."""
    monkeypatch.setattr(engine, "_STORE_CALL_TIMEOUT_S", 0.15)

    def wedged(*a, **k):
        time.sleep(3.0)

    async def run():
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await engine._off_loop_store(wedged)
        return time.monotonic() - started

    assert asyncio.run(run()) < 1.5


# --------------------------------------------------------------------------- #
# E-7 — wall-clock schedule math must follow the host zone, not a hardcoded +03:00
# --------------------------------------------------------------------------- #


def test_daily_hhmm_is_resolved_in_the_configured_zone(monkeypatch):
    """"daily 09:00" must mean 09:00 where the USER is, not 09:00 in Istanbul."""
    from akana_server.schedule import store as store_mod

    monkeypatch.setenv("AKANA_TIMEZONE", "UTC")
    store_mod.reset_timezone_cache()
    ref = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    nxt = store_mod.compute_next_run("daily", "09:00", weekday=None, after=ref)
    assert nxt.astimezone(timezone.utc).hour == 9
    store_mod.reset_timezone_cache()


def test_naive_iso_is_stamped_with_the_configured_zone(monkeypatch):
    """An offset-less datetime from the model is the user's local wall clock."""
    from akana_server.schedule import store as store_mod

    monkeypatch.setenv("AKANA_TIMEZONE", "UTC")
    store_mod.reset_timezone_cache()
    dt = store_mod.parse_iso("2026-07-27T09:00")
    assert dt.utcoffset() == timedelta(0)
    assert dt.astimezone(timezone.utc).hour == 9
    store_mod.reset_timezone_cache()


def test_already_stored_next_run_at_keeps_its_instant(monkeypatch):
    """MIGRATION: an existing row carries an explicit +03:00 offset — the same
    absolute instant must survive a timezone change (no silent shift)."""
    from akana_server.schedule import store as store_mod

    monkeypatch.setenv("AKANA_TIMEZONE", "UTC")
    store_mod.reset_timezone_cache()
    dt = store_mod.parse_iso("2026-07-27T09:00:00+03:00")
    assert dt == datetime(2026, 7, 27, 6, 0, tzinfo=timezone.utc)
    store_mod.reset_timezone_cache()


# --------------------------------------------------------------------------- #
# E-6 — the connector create-gate: an explicit stored false beats the env switch
# --------------------------------------------------------------------------- #


def test_stored_false_beats_the_env_kill_switch(tmp_path, monkeypatch):
    """Turning Telegram OFF in Settings must reject a connector schedule even when
    AKANA_TELEGRAM_ENABLED=1 is still in .env — otherwise the schedule is accepted,
    burns a turn on every fire and is thrown away undelivered."""
    from akana_server.runtime_settings import reset_runtime_stores
    from akana_server.schedule import tools as sched_tools

    (tmp_path / "runtime_settings.json").write_text(
        json.dumps({"telegram_enabled": False}), encoding="utf-8"
    )
    reset_runtime_stores()
    monkeypatch.setenv("AKANA_TELEGRAM_ENABLED", "1")
    try:
        assert sched_tools._connector_enabled(tmp_path, "telegram") is False
    finally:
        reset_runtime_stores()


def test_absent_key_still_falls_back_to_the_env_switch(tmp_path, monkeypatch):
    """A store with NO opinion still honours the .env kill switch (real fallback)."""
    from akana_server.runtime_settings import reset_runtime_stores
    from akana_server.schedule import tools as sched_tools

    reset_runtime_stores()
    monkeypatch.setenv("AKANA_TELEGRAM_ENABLED", "1")
    try:
        assert sched_tools._connector_enabled(tmp_path, "telegram") is True
    finally:
        reset_runtime_stores()


# --------------------------------------------------------------------------- #
# E-1 — the boot-time orphan reaper must verify identity before killing
# --------------------------------------------------------------------------- #


def test_reaper_refuses_a_record_from_a_previous_boot(tmp_path):
    """A pid file that survived a reboot names a pid the OS has since RECYCLED.
    Killing it takes down an unrelated program (and, via /T // killpg, its tree)."""
    from akana_server.orchestrator import llm_process

    victim = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        d = llm_process.llm_pid_dir(tmp_path)
        (d / "stale-token.json").write_text(
            json.dumps(
                {
                    "token": "stale-token",
                    "pid": victim.pid,
                    "pgid": victim.pid,
                    "kind": "claude_cli",
                    # 30 days ago — necessarily before the current boot.
                    "started_at": time.time() - 30 * 86400,
                }
            ),
            encoding="utf-8",
        )
        findings = llm_process.reap_orphan_llm_processes(tmp_path)
        assert findings and findings[0]["reaped"] is False
        assert victim.poll() is None, "the reaper killed an unrelated recycled pid"
    finally:
        victim.kill()
        victim.wait()


def test_reaper_refuses_when_the_recorded_creation_time_does_not_match(tmp_path):
    """Same boot, recycled pid: the live process's OS creation time is not ours."""
    from akana_server.orchestrator import llm_process

    victim = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if llm_process._process_start_time(victim.pid) is None:
            pytest.skip("no OS process-creation-time source on this platform")
        d = llm_process.llm_pid_dir(tmp_path)
        (d / "tok.json").write_text(
            json.dumps(
                {
                    "token": "tok",
                    "pid": victim.pid,
                    "pgid": victim.pid,
                    "kind": "claude_cli",
                    "started_at": time.time(),
                    "boot_ts": llm_process._boot_timestamp(),
                    "create_time": time.time() - 4000.0,  # a DIFFERENT process
                }
            ),
            encoding="utf-8",
        )
        findings = llm_process.reap_orphan_llm_processes(tmp_path)
        assert findings and findings[0]["reaped"] is False
        assert victim.poll() is None
    finally:
        victim.kill()
        victim.wait()


def test_reaper_still_kills_a_genuine_orphan_from_this_boot(tmp_path):
    """No regression: a record this process actually wrote IS reaped."""
    from akana_server.orchestrator import llm_process

    victim = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        llm_process.register_llm_process(tmp_path, "live-token", victim.pid, "claude_cli")
        findings = llm_process.reap_orphan_llm_processes(tmp_path)
        assert findings and findings[0]["reaped"] is True
        deadline = time.monotonic() + 8.0
        while victim.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert victim.poll() is not None, "a genuine orphan survived the reaper"
    finally:
        if victim.poll() is None:
            victim.kill()
        victim.wait()


# --------------------------------------------------------------------------- #
# E-4 — codex must not broadcast one MCP server's secrets to every other child
# --------------------------------------------------------------------------- #


def test_codex_does_not_flatten_external_server_secrets(caplog):
    """A third-party MCP child must not inherit another server's token.

    ``_mcp_overrides`` collapsed EVERY server's env into one process environment,
    which codex then hands to every stdio child it spawns."""
    from akana_server.orchestrator import codex_provider

    servers = {
        "github": {"command": "npx", "env": {"GITHUB_TOKEN": "ghp_secret"}},
        "filesystem": {"command": "npx", "args": ["-y", "server-filesystem"]},
    }
    with caplog.at_level(logging.WARNING):
        overrides, process_env = codex_provider._mcp_overrides(servers)
    assert "GITHUB_TOKEN" not in process_env
    assert not any("ghp_secret" in a for a in overrides), "secret leaked onto argv"


def test_codex_still_forwards_builtin_akana_server_env():
    """Built-in Akana children keep working (the vault key path is unchanged)."""
    from akana_server.orchestrator import codex_provider

    servers = {
        "akana_vault": {"command": "py", "env": {"AKANA_VAULT_KEY": "k", "AKANA_DATA_DIR": "/d"}},
    }
    overrides, process_env = codex_provider._mcp_overrides(servers)
    assert process_env.get("AKANA_VAULT_KEY") == "k"
    assert process_env.get("AKANA_DATA_DIR") == "/d"  # belt-and-suspenders kept
    assert any("AKANA_DATA_DIR" in a for a in overrides)


def test_codex_withholds_the_vault_key_when_an_external_server_is_mounted():
    """codex hands ONE process env to every child, so a third-party npx server would
    otherwise receive the key that decrypts every stored secret."""
    from akana_server.orchestrator import codex_provider

    servers = {
        "akana_vault": {"command": "py", "env": {"AKANA_VAULT_KEY": "k"}},
        "filesystem": {"command": "npx", "args": ["-y", "server-filesystem"]},
    }
    _overrides, process_env = codex_provider._mcp_overrides(servers)
    assert "AKANA_VAULT_KEY" not in process_env


def test_codex_drops_colliding_env_keys():
    """Two servers, one variable name: the shared process env silently gave BOTH
    children one value, so server A authenticated with server B's credential."""
    from akana_server.orchestrator import codex_provider

    servers = {
        "akana_memory": {"command": "py", "env": {"AKANA_VAULT_KEY": "a"}},
        "akana_vault": {"command": "py", "env": {"AKANA_VAULT_KEY": "b"}},
    }
    _overrides, process_env = codex_provider._mcp_overrides(servers)
    assert "AKANA_VAULT_KEY" not in process_env


# --------------------------------------------------------------------------- #
# E-5 — the connector inbound turn must not read history/persona on the loop
# --------------------------------------------------------------------------- #


def test_connector_turn_reads_history_off_the_loop(monkeypatch):
    """`_history_for` hits sqlite (busy_timeout 10s) and `_system_prompt_for` walks the
    persona registry + skill catalog — both froze the whole server on the loop."""
    from akana_server.connectors import router as router_mod

    seen: dict[str, int | None] = {}

    def slow_history(self, cid):
        time.sleep(0.3)
        seen["history_thread"] = 1
        return []

    def slow_prompt(self, msg, cid):
        time.sleep(0.3)
        return None

    monkeypatch.setattr(router_mod.InboundRouter, "_history_for", slow_history)
    monkeypatch.setattr(router_mod.InboundRouter, "_system_prompt_for", slow_prompt)

    async def _reply(settings, text, **kw):
        return "ok"

    async def _no_skills(settings, text):
        return None

    async def run():
        r = router_mod.InboundRouter(
            SimpleNamespace(data_dir=Path(".")),
            registry=SimpleNamespace(send=None),
            complete=_reply,
            skill_planner=_no_skills,
        )
        gaps: list[float] = []

        async def heartbeat():
            last = time.monotonic()
            while True:
                await asyncio.sleep(0.01)
                n = time.monotonic()
                gaps.append(n - last)
                last = n

        beat = asyncio.create_task(heartbeat())
        await asyncio.sleep(0.05)
        msg = router_mod.InboundMessage(
            connector_id="telegram", chat_id="1", text="merhaba", sender_name="u"
        )
        await r._build_outcome(msg)
        # A gap is only RECORDED on the tick AFTER the stall — cancelling straight
        # after the turn would hide it.
        await asyncio.sleep(0.05)
        beat.cancel()
        return max(gaps)

    worst = asyncio.run(run())
    assert worst < 0.25, f"event loop blocked {worst * 1000:.0f}ms by connector reads"


# --------------------------------------------------------------------------- #
# E-8 — the consolidation cron must resolve its gate against LIVE settings
# --------------------------------------------------------------------------- #


def test_consolidation_cron_reads_live_settings(monkeypatch):
    """After a per-key "Reset to default" the UI reports the change as applied live;
    the cron must not keep the pre-reset gate from the boot snapshot."""
    from akana_server.orchestrator import summary_consolidation_service as svc

    captured: dict[str, object] = {}

    def fake_poll_loop(settings, **kw):
        captured.update(kw)

        async def _noop():
            return None

        return _noop()

    monkeypatch.setattr(svc, "_poll_loop", fake_poll_loop)
    monkeypatch.setattr(svc, "_start_task", lambda app, attr, coro: coro.close())

    boot = SimpleNamespace(data_dir=Path("."), marker="boot")
    app = SimpleNamespace(state=SimpleNamespace(settings=boot))
    svc.start_summary_consolidation(app)

    live = SimpleNamespace(data_dir=Path("."), marker="live")
    app.state.settings = live

    seen: list[object] = []
    monkeypatch.setattr(svc, "consolidation_active", lambda s: seen.append(s) or True)
    monkeypatch.setattr(svc, "_interval_seconds", lambda s: seen.append(s) or 1.0)

    captured["is_active"](boot)
    captured["interval_seconds"](boot)
    assert [getattr(s, "marker", None) for s in seen] == ["live", "live"]


# --------------------------------------------------------------------------- #
# E-9 — the MCP bridge must enter and exit its anyio scopes in ONE task
# --------------------------------------------------------------------------- #


def test_bridge_teardown_survives_task_per_anext(monkeypatch, caplog):
    """``chat_producer`` drives the provider generator with a NEW task per
    ``__anext__``, so an ``async with`` held across a yield is entered in one task and
    exited in another — anyio then raises "cancel scope in a different task"."""
    from akana_server.orchestrator.mcp_bridge import McpToolBridge

    entered: dict[str, object] = {}

    class _Scoped:
        """Duck-types anyio's task-affinity rule: exit must be the entering task."""

        async def __aenter__(self):
            entered["task"] = asyncio.current_task()
            return self

        async def __aexit__(self, *exc):
            if asyncio.current_task() is not entered["task"]:
                raise RuntimeError(
                    "Attempted to exit cancel scope in a different task than it was entered in"
                )
            return False

    class _Session:
        async def initialize(self):
            return None

        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name="ping", description="", inputSchema={})])

    async def fake_open(self, cfg):
        await self._stack.enter_async_context(_Scoped())
        return _Session()

    monkeypatch.setattr(McpToolBridge, "_open_session", fake_open)

    async def gen():
        async with McpToolBridge({"srv": {"type": "stdio", "command": "x"}}) as bridge:
            yield [d["function"]["name"] for d in bridge.decls]
            yield "done"

    async def run():
        it = gen()
        out = []
        while True:
            step = asyncio.create_task(it.__anext__())  # a NEW task per step
            try:
                out.append(await step)
            except StopAsyncIteration:
                break
        return out

    with caplog.at_level(logging.WARNING):
        got = asyncio.run(run())
    assert got == [["mcp__srv__ping"], "done"]
    assert "error while closing MCP sessions" not in caplog.text
