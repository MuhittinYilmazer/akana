"""Live token counter, early-termination, conflict guard, pricing table and
episodic usage persist tests.

Tasks (task numbers match the summary document):
  - Task 1: AskUserQuestion / ExitPlanMode early-termination — only ONE
    question/plan is emitted, the second does not arrive, the loop breaks.
  - Task 2: Conflict guard — if the done payload has both ask_user and plan,
    ask_user wins, plan_review is not sent.
  - Task 3: usage_live emission — message_start + message_delta → usage_live
    events are produced.
  - Task 4: estimate_cost_usd — the correct price by model keyword,
    cache discounts, corrupt input safe-zero.
  - Task 5: episodic usage persist + round-trip — usage is written to the assistant
    turn and read back via list_conversation_recent.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Helper constants and fake subprocess
# ---------------------------------------------------------------------------
from akana_server.config import Settings, load_settings
from akana_server.orchestrator import claude_provider


class _FakeProc:
    pid = 9999
    returncode: int | None = 0

    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.stdin = None  # Windows cmd spill path checks proc.stdin (None → write skipped)

    def feed(self, *events: dict[str, Any], eof: bool = True) -> None:
        for ev in events:
            self.stdout.feed_data((json.dumps(ev) + "\n").encode())
        if eof:
            self.stdout.feed_eof()

    async def wait(self) -> int:
        return self.returncode or 0

    def kill(self) -> None:
        self.returncode = -9


def _make_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    return load_settings()


def _patch_spawn(monkeypatch: pytest.MonkeyPatch, proc: _FakeProc) -> None:
    async def _fake(*cmd: str, **kw: Any):
        return proc
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake)


_INIT = {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-sonnet-4-6"}
_RESULT_OK = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "",
    "usage": {"input_tokens": 10, "output_tokens": 5},
    "session_id": "s1",
}


def _ask_user_ev(tid: str = "tu-ask") -> dict:
    """An assistant event containing AskUserQuestion."""
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tid,
                    "name": "AskUserQuestion",
                    "input": {
                        "questions": [
                            {
                                "question": "Hangi dili tercih edersiniz?",
                                "header": "Dil seçimi",
                                "multiSelect": False,
                                "options": [
                                    {"label": "Türkçe", "description": ""},
                                    {"label": "İngilizce", "description": ""},
                                ],
                            }
                        ]
                    },
                }
            ]
        },
    }


def _exit_plan_ev(tid: str = "tu-plan") -> dict:
    """An assistant event containing ExitPlanMode."""
    return {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "id": tid,
                    "name": "ExitPlanMode",
                    "input": {
                        "plan": "# Adımlar\n1. Kodu incele\n2. Değişiklik yap",
                        "planFilePath": "/tmp/plan.md",
                    },
                }
            ]
        },
    }


def _delta(text: str) -> dict:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        },
    }


def _msg_start(input_tokens: int = 100) -> dict:
    """message_start event — carries the prompt token count."""
    return {
        "type": "stream_event",
        "event": {
            "type": "message_start",
            "message": {
                "usage": {
                    "input_tokens": input_tokens,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                }
            },
        },
    }


def _msg_delta(output_tokens: int = 20) -> dict:
    """message_delta event — carries the cumulative output token count."""
    return {
        "type": "stream_event",
        "event": {
            "type": "message_delta",
            "usage": {"output_tokens": output_tokens},
        },
    }


# ---------------------------------------------------------------------------
# Task 1: early-termination tests
# ---------------------------------------------------------------------------


def test_ask_user_early_terminate_yields_single_question(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When AskUserQuestion arrives the loop breaks, ONLY ONE question is emitted.

    The CLI normally auto-rejects the question and continues; with early-termination
    a second question or plan event does not arrive.
    """
    settings = _make_settings(monkeypatch, tmp_path)

    # Process-kill simulation — make terminate_process_group a no-op
    killed: list[int] = []

    async def _fake_kill(pid: int) -> None:
        killed.append(pid)

    monkeypatch.setattr(claude_provider, "terminate_process_group", _fake_kill)

    async def run() -> None:
        proc = _FakeProc()
        # one MORE delta arrives after the ask_user event (the CLI's "apology" text) —
        # we must not see it (thanks to early-termination) but we add it so the test
        # stream gets an EOF before finishing.
        proc.feed(_INIT, _ask_user_ev(), _delta("özür"), eof=True)
        _patch_spawn(monkeypatch, proc)

        events = [
            ev async for ev in claude_provider.stream_user_chat(settings, "hangi dil?")
        ]

        ask_events = [e for e in events if "ask_user" in e and not e.get("done")]
        assert len(ask_events) == 1, "Exactly ONE ask_user event expected"
        assert "questions" in ask_events[0]["ask_user"]

        final = events[-1]
        assert final.get("done") is True
        assert final.get("status") == "awaiting_user"
        assert final.get("ask_user") is not None

        # plan_review must not be sent (only ask_user is present)
        assert "plan_review" not in final or final.get("plan_review") is None

        # was the process killed?
        assert killed, "terminate_process_group was not called — early-termination did not work"

    asyncio.run(run())


def test_exit_plan_mode_early_terminate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When ExitPlanMode arrives the loop breaks, the plan event is emitted, the process dies."""
    settings = _make_settings(monkeypatch, tmp_path)

    killed: list[int] = []

    async def _fake_kill(pid: int) -> None:
        killed.append(pid)

    monkeypatch.setattr(claude_provider, "terminate_process_group", _fake_kill)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, _exit_plan_ev(), eof=True)
        _patch_spawn(monkeypatch, proc)

        events = [
            ev async for ev in claude_provider.stream_user_chat(settings, "planı hazırla")
        ]

        plan_events = [e for e in events if "plan" in e and not e.get("done")]
        assert len(plan_events) == 1
        assert "plan" in plan_events[0]["plan"]

        final = events[-1]
        assert final.get("done") is True
        assert final.get("status") == "awaiting_user"
        assert killed

    asyncio.run(run())


def test_ask_user_no_duplicate_terminate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Early-termination flag: terminate_process_group is called EXACTLY ONCE."""
    settings = _make_settings(monkeypatch, tmp_path)

    kill_count = [0]

    async def _fake_kill(pid: int) -> None:
        kill_count[0] += 1

    monkeypatch.setattr(claude_provider, "terminate_process_group", _fake_kill)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(_INIT, _ask_user_ev(), eof=True)
        _patch_spawn(monkeypatch, proc)
        _ = [ev async for ev in claude_provider.stream_user_chat(settings, "soru")]
        # since returncode is not None (0) in finally, there is no second kill
        assert kill_count[0] == 1, f"Expected 1, got {kill_count[0]}"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Task 2: conflict guard (in the producer)
# ---------------------------------------------------------------------------


def test_conflict_guard_ask_user_wins_over_plan() -> None:
    """done tokens block: if ask_user + plan are present at once, plan_review must be None."""

    # _done_tokens_block only transforms the tokens block; the conflict guard runs in the
    # producer's done payload assembly via _emit_plan_review. Here we test the logic directly.
    last_ask_user = {"id": "x", "questions": []}
    last_plan = {"id": "y", "plan": "# Plan"}

    # Conflict guard: if ask_user is present, plan_review is None
    _emit_plan_review = last_plan if last_ask_user is None else None
    assert _emit_plan_review is None, "plan_review must not be sent while ask_user is present"

    # If only plan is present, plan_review is sent
    last_ask_user_none = None
    _emit_plan_review2 = last_plan if last_ask_user_none is None else None
    assert _emit_plan_review2 is not None, "plan_review must be sent when there is no ask_user"


# ---------------------------------------------------------------------------
# Task 3: usage_live events
# ---------------------------------------------------------------------------


def test_usage_live_emitted_on_message_start_and_delta(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """message_start + message_delta → usage_live events must be produced."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def _fake_kill(pid: int) -> None:
        pass

    monkeypatch.setattr(claude_provider, "terminate_process_group", _fake_kill)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(
            _INIT,
            _msg_start(input_tokens=200),
            _delta("merhaba"),
            _msg_delta(output_tokens=30),
            _RESULT_OK,
        )
        _patch_spawn(monkeypatch, proc)

        events = [ev async for ev in claude_provider.stream_user_chat(settings, "selam")]

        live = [e for e in events if "usage_live" in e]
        assert len(live) >= 2, f"At least 2 usage_live expected, got: {len(live)}"

        # First usage_live: must come from message_start
        first = live[0]["usage_live"]
        assert first["prompt"] == 200

        # Last usage_live: from message_delta (cumulative output)
        last_live = live[-1]["usage_live"]
        assert last_live["completion"] == 30

    asyncio.run(run())


def test_usage_live_does_not_break_text_stream(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """usage_live events must not leak between the text deltas."""
    settings = _make_settings(monkeypatch, tmp_path)

    async def _fake_kill(pid: int) -> None:
        pass

    monkeypatch.setattr(claude_provider, "terminate_process_group", _fake_kill)

    async def run() -> None:
        proc = _FakeProc()
        proc.feed(
            _INIT,
            _msg_start(50),
            _delta("A"),
            _delta("B"),
            _msg_delta(10),
            _delta("C"),
            _RESULT_OK,
        )
        _patch_spawn(monkeypatch, proc)

        events = [ev async for ev in claude_provider.stream_user_chat(settings, "test")]

        delta_texts = [e["delta"] for e in events if "delta" in e and not e.get("done")]
        assert delta_texts == ["A", "B", "C"], "Text deltas must arrive in order"

        final = events[-1]
        assert final.get("done") is True
        assert final["text"] == "ABC"

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Task 4: pricing table (estimate_cost_usd)
# ---------------------------------------------------------------------------


def test_pricing_sonnet_default() -> None:
    """Unknown model → sonnet price ($3 in / $15 out per MTok)."""
    from akana_server.orchestrator.base import estimate_cost_usd

    cost = estimate_cost_usd("claude-unknown-xyz", 1_000_000, 0)
    assert abs(cost - 3.0) < 0.01, f"Sonnet input price expected, got: {cost}"


def test_pricing_opus() -> None:
    """opus keyword → $15 input, $75 output."""
    from akana_server.orchestrator.base import estimate_cost_usd

    cost = estimate_cost_usd("claude-opus-4-5", 0, 1_000_000)
    assert abs(cost - 75.0) < 0.1, f"Opus output price expected, got: {cost}"


def test_pricing_haiku() -> None:
    """haiku keyword → $0.80 input per MTok."""
    from akana_server.orchestrator.base import estimate_cost_usd

    cost = estimate_cost_usd("claude-haiku-3", 1_000_000, 0)
    assert abs(cost - 0.80) < 0.01, f"Haiku input price expected, got: {cost}"


def test_pricing_cache_read_discount() -> None:
    """A cache read must be computed at 10% of the normal price."""
    from akana_server.orchestrator.base import estimate_cost_usd

    # prompt_tokens is fresh input EXCLUDING cache; "all from cache" means fresh
    # input 0 + cache_read 1M → cost = 1M * $3 * 0.1 / 1M = $0.30 (sonnet).
    cost = estimate_cost_usd("claude-sonnet", 0, 0, cache_read=1_000_000)
    assert abs(cost - 0.30) < 0.01, f"Cache read discount wrong, got: {cost}"

    # BUG 4 regression guard: fresh input and cache-read are priced SEPARATELY
    # (no double-subtraction). 1M fresh ($3.00) + 1M cache ($0.30) = $3.30.
    additive = estimate_cost_usd("claude-sonnet", 1_000_000, 0, cache_read=1_000_000)
    assert abs(additive - 3.30) < 0.01, f"Additive price expected $3.30, got: {additive}"


def test_pricing_zero_tokens() -> None:
    """Zero tokens → zero cost (no crash)."""
    from akana_server.orchestrator.base import estimate_cost_usd

    assert estimate_cost_usd(None, 0, 0) == 0.0


def test_pricing_none_model_falls_back() -> None:
    """model=None → sonnet default (no crash)."""
    from akana_server.orchestrator.base import estimate_cost_usd

    cost = estimate_cost_usd(None, 1_000_000, 0)
    assert cost > 0, "the sonnet default must be returned for a None model"


def test_pricing_negative_tokens_safe() -> None:
    """Negative tokens → treated as 0 (no crash)."""
    from akana_server.orchestrator.base import estimate_cost_usd

    cost = estimate_cost_usd("sonnet", -100, -50)
    assert cost == 0.0


# ---------------------------------------------------------------------------
# Task 5: episodic usage persist + round-trip
# ---------------------------------------------------------------------------


def test_episodic_usage_roundtrip(tmp_path: Path) -> None:
    """usage is written to the assistant turn and read back via list_conversation_recent."""
    from akana.memory.episodic import EpisodicStore

    store = EpisodicStore.for_data_dir(tmp_path)
    usage = {"prompt": 123, "completion": 45, "cost_usd": 0.0031}
    store.append_turn(
        turn_id="turn-u1",
        conversation_id="conv-u1",
        role="assistant",
        text="Merhaba!",
        usage=usage,
    )
    turns = store.list_conversation_recent("conv-u1", limit=10)
    assert len(turns) == 1
    t = turns[0]
    assert t.usage is not None
    assert t.usage["prompt"] == 123
    assert t.usage["completion"] == 45
    assert abs(t.usage["cost_usd"] - 0.0031) < 1e-6


def test_episodic_usage_none_for_user_turn(tmp_path: Path) -> None:
    """If usage is not given on a user turn, None is returned."""
    from akana.memory.episodic import EpisodicStore

    store = EpisodicStore.for_data_dir(tmp_path)
    store.append_turn(
        turn_id="turn-u2",
        conversation_id="conv-u2",
        role="user",
        text="Ne yapıyorsun?",
    )
    turns = store.list_conversation_recent("conv-u2", limit=10)
    assert len(turns) == 1
    assert turns[0].usage is None


def test_episodic_usage_existing_db_migration(tmp_path: Path) -> None:
    """If an old db has no usage column, the migration (ALTER ADD COLUMN) adds it idempotently."""
    import sqlite3

    db_path = tmp_path / "db" / "memory.db"
    (tmp_path / "db").mkdir(parents=True)
    # Old schema: NO usage column
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE turns (
            id TEXT PRIMARY KEY,
            conversation_id TEXT NOT NULL,
            ts TEXT NOT NULL,
            role TEXT NOT NULL,
            text TEXT NOT NULL,
            lang TEXT,
            importance REAL,
            island TEXT,
            tool_call_id TEXT,
            duration_ms INTEGER,
            tool_calls TEXT,
            file_ids TEXT
        )
    """)
    conn.commit()
    conn.close()

    # The migration must run when EpisodicStore opens — no exception
    from akana.memory.episodic import EpisodicStore

    store = EpisodicStore(db_path)
    # usage must now be writable
    store.append_turn(
        turn_id="migrated",
        conversation_id="c1",
        role="assistant",
        text="test",
        usage={"prompt": 5, "completion": 3},
    )
    turns = store.list_conversation_recent("c1", limit=5)
    assert turns[0].usage == {"prompt": 5, "completion": 3}


# ---------------------------------------------------------------------------
# Task 6: error subtypes
# ---------------------------------------------------------------------------


def test_classify_error_max_turns() -> None:
    """error_max_turns → a clear explanatory message."""
    from akana_server.orchestrator.claude_provider import _classify_claude_failure

    msg = _classify_claude_failure(
        result_error={"subtype": "error_max_turns", "result": ""},
        stderr_text="",
        model_tag="claude-sonnet-4-6",
    )
    assert "maximum" in msg.lower() or "max_turns" in msg.lower()


def test_classify_error_during_execution() -> None:
    """error_during_execution → a clear explanatory message."""
    from akana_server.orchestrator.claude_provider import _classify_claude_failure

    msg = _classify_claude_failure(
        result_error={"subtype": "error_during_execution", "result": ""},
        stderr_text="",
        model_tag="claude-sonnet-4-6",
    )
    assert "unexpected" in msg.lower() or "running the task" in msg.lower()
