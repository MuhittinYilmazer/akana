"""chat_context — locking the agent/session id to its provider.

Live bug: when a conversation opened with cursor switched to provider=claude,
its agent UUID leaked through as ``claude --resume <cursor-uuid>`` and the run
blew up with "No conversation found with session ID". Now the stored identity
is only returned while its own provider is active.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from akana_server import chat_context
from akana_server.config import load_settings
from akana_server.llm_settings import LlmSettings, save_llm_settings
from akana_server.conversation_service import ConversationService

CONV = "conv-provider-scope"


def _make_request(monkeypatch: pytest.MonkeyPatch, tmp_path, provider: str):
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    save_llm_settings(tmp_path, LlmSettings(provider=provider))
    settings = load_settings()
    svc = ConversationService.for_data_dir(tmp_path)
    svc.ensure(CONV)
    app = SimpleNamespace(state=SimpleNamespace(settings=settings, conversation_service=svc))
    return SimpleNamespace(app=app), svc


def test_persist_stores_provider_and_roundtrips(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    request, svc = _make_request(monkeypatch, tmp_path, "cursor")
    chat_context.persist_agent_id(request, CONV, "agent-cursor-1")
    meta = svc.get_json_metadata(CONV)
    assert meta["agent_id"] == "agent-cursor-1"
    assert meta["agent_provider"] == "cursor"
    assert chat_context.get_agent_id(request, CONV) == "agent-cursor-1"


def test_persist_tags_provider_from_per_turn_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """b15: persist_agent_id must tag the id with the provider the turn DISPATCHED with (the
    per-turn ContextVar snapshot set by bind_conversation_llm), NOT a fresh live/global read —
    else a mid-turn model switch stores the wrong provider and the next turn's leak-guard rejects
    the id ('could not resume' / empty response)."""
    from akana_server.llm_context import reset_conversation_llm, set_conversation_llm

    request, svc = _make_request(monkeypatch, tmp_path, "cursor")  # global provider = cursor
    # The turn actually dispatched on CLAUDE (per-conversation effective LLM bound for the turn).
    token = set_conversation_llm(LlmSettings(provider="claude"))
    try:
        chat_context.persist_agent_id(request, CONV, "agent-claude-1")
    finally:
        reset_conversation_llm(token)
    meta = svc.get_json_metadata(CONV)
    assert meta["agent_id"] == "agent-claude-1"
    # Tagged from the snapshot (claude), NOT the live/global provider (cursor).
    assert meta["agent_provider"] == "claude"


def test_agent_id_not_leaked_to_claude(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Global claude + only an agent hint → the leak-gate returns no agent id."""
    request, svc = _make_request(monkeypatch, tmp_path, "cursor")
    chat_context.persist_agent_id(request, CONV, "agent-cursor-1")

    save_llm_settings(tmp_path, LlmSettings(provider="claude"))
    assert chat_context.get_agent_id(request, CONV) is None


def test_agent_id_resumes_when_llm_provider_saved(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    request, svc = _make_request(monkeypatch, tmp_path, "cursor")
    chat_context.persist_agent_id(request, CONV, "agent-cursor-1")
    svc.merge_json_metadata(CONV, {"llm_provider": "cursor"})

    save_llm_settings(tmp_path, LlmSettings(provider="claude"))
    assert chat_context.get_agent_id(request, CONV) == "agent-cursor-1"


def test_agent_id_hidden_when_conversation_provider_differs(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """If the conversation is explicitly claude, the cursor agent id is not returned."""
    request, svc = _make_request(monkeypatch, tmp_path, "cursor")
    chat_context.persist_agent_id(request, CONV, "agent-cursor-1")
    svc.merge_json_metadata(CONV, {"llm_provider": "claude"})

    save_llm_settings(tmp_path, LlmSettings(provider="cursor"))
    assert chat_context.get_agent_id(request, CONV) is None


def test_claude_session_id_not_leaked_to_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    request, svc = _make_request(monkeypatch, tmp_path, "claude")
    chat_context.persist_agent_id(request, CONV, "sess-claude-1")
    meta = svc.get_json_metadata(CONV)
    assert meta["agent_provider"] == "claude"
    assert chat_context.get_agent_id(request, CONV) == "sess-claude-1"

    save_llm_settings(tmp_path, LlmSettings(provider="cursor"))
    assert chat_context.get_agent_id(request, CONV) is None


def test_claude_session_resumes_when_llm_provider_saved(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    request, svc = _make_request(monkeypatch, tmp_path, "claude")
    chat_context.persist_agent_id(request, CONV, "sess-claude-1")
    svc.merge_json_metadata(CONV, {"llm_provider": "claude"})

    save_llm_settings(tmp_path, LlmSettings(provider="cursor"))
    assert chat_context.get_agent_id(request, CONV) == "sess-claude-1"


def test_claude_session_hidden_when_conversation_provider_is_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    request, svc = _make_request(monkeypatch, tmp_path, "claude")
    chat_context.persist_agent_id(request, CONV, "sess-claude-1")
    svc.merge_json_metadata(CONV, {"llm_provider": "cursor"})

    save_llm_settings(tmp_path, LlmSettings(provider="claude"))
    assert chat_context.get_agent_id(request, CONV) is None


def test_legacy_record_without_provider_counts_as_cursor(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Old records (no provider field) count as cursor: they work under cursor,
    not under claude. The legacy ``cursor_agent_id`` key is read backward-compatibly."""
    request, svc = _make_request(monkeypatch, tmp_path, "cursor")
    svc.merge_json_metadata(CONV, {"cursor_agent_id": "legacy-agent"})  # legacy key
    assert chat_context.get_agent_id(request, CONV) == "legacy-agent"

    save_llm_settings(tmp_path, LlmSettings(provider="claude"))
    assert chat_context.get_agent_id(request, CONV) is None


def test_clear_wipes_provider_too(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    request, svc = _make_request(monkeypatch, tmp_path, "cursor")
    chat_context.persist_agent_id(request, CONV, "agent-cursor-1")
    chat_context.clear_agent_id(request, CONV)
    assert chat_context.get_agent_id(request, CONV) is None
    meta = svc.get_json_metadata(CONV)
    assert not meta.get("agent_id")
    assert not meta.get("agent_provider")


def test_bootstrap_needed_without_agent_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    request, _svc = _make_request(monkeypatch, tmp_path, "cursor")
    assert chat_context.llm_history_bootstrap_needed_sync(request, CONV) is True


def test_bootstrap_skipped_when_agent_id_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    request, _svc = _make_request(monkeypatch, tmp_path, "cursor")
    chat_context.persist_agent_id(request, CONV, "agent-cursor-1")
    assert chat_context.llm_history_bootstrap_needed_sync(request, CONV) is False


def test_bootstrap_always_needed_for_gemini(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Gemini is STATELESS (like ollama): even if a stored agent id looks like a
    "resume", history must ALWAYS be flattened into the prompt. The old code did not
    include gemini in the statelessness list, so with an agent id present it could skip
    bootstrap (False) and drop history; now it is symmetric with ollama (True)."""
    request, _svc = _make_request(monkeypatch, tmp_path, "gemini")
    chat_context.persist_agent_id(request, CONV, "agent-stale")
    assert chat_context.llm_history_bootstrap_needed_sync(request, CONV) is True


def test_assemble_skips_episodic_read_on_resume(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    request, svc = _make_request(monkeypatch, tmp_path, "cursor")
    chat_context.persist_agent_id(request, CONV, "agent-cursor-1")
    _seed_turns(svc, [("user", "q1"), ("assistant", "a1")])
    msgs, dropped, skipped = chat_context._llm_history_for_assemble_sync(request, CONV)
    assert msgs == []
    assert skipped is True
    assert dropped == 0


def test_assemble_loads_history_when_bootstrap_needed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    request, svc = _make_request(monkeypatch, tmp_path, "cursor")
    _seed_turns(svc, [("user", "q1"), ("assistant", "a1")])
    msgs, dropped, skipped = chat_context._llm_history_for_assemble_sync(request, CONV)
    assert len(msgs) == 2
    assert skipped is False
    assert dropped == 0


def test_dropped_turns_without_message_load(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    request, svc = _make_request(monkeypatch, tmp_path, "cursor")
    save_llm_settings(tmp_path, LlmSettings(provider="cursor", chat_max_turns=2))
    _seed_turns(
        svc,
        [("user", "q1"), ("assistant", "a1"), ("user", "q2"), ("assistant", "a2")],
    )
    chat_context.persist_agent_id(request, CONV, "agent-cursor-1")
    dropped = chat_context._llm_dropped_turns_sync(request, CONV)
    # A direct episodic seed does not update the meta counter; on the live path turn_writer bumps it.
    assert dropped == 0
    msgs, _dropped, skipped = chat_context._llm_history_for_assemble_sync(request, CONV)
    assert msgs == []
    assert skipped is True


def _bump_turns(svc: ConversationService, pairs: int) -> int:
    """Bump the META message counter by ``pairs`` user+assistant turns; return the count.

    Uses the production meta store the way ``turn_writer`` does (user then assistant)."""
    for _i in range(pairs):
        svc._meta_store.on_user_message(CONV, "q")
        svc._meta_store.on_assistant_message(CONV)
    meta = svc.get(CONV)
    return int(meta.message_count) if meta else 0


def test_dropped_turns_zero_on_resume_even_over_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Resume (cursor + agent id): the model keeps the FULL history in its own agent
    session, so even with message_count WELL OVER chat_max_turns the 'old messages dropped'
    warning must NOT fire — dropped == 0. (Fix for the misleading warning on cursor.)"""
    request, svc = _make_request(monkeypatch, tmp_path, "cursor")
    save_llm_settings(tmp_path, LlmSettings(provider="cursor", chat_max_turns=2))
    chat_context.persist_agent_id(request, CONV, "agent-cursor-1")  # resume active
    count = _bump_turns(svc, 3)  # 6 messages ≫ window of 2
    assert count >= 6
    assert chat_context._llm_dropped_turns_sync(request, CONV) == 0
    msgs, dropped, skipped = chat_context._llm_history_for_assemble_sync(request, CONV)
    assert (msgs, dropped, skipped) == ([], 0, True)


def test_dropped_turns_counted_for_stateless_over_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Stateless (gemini): history IS truncated to the window, so out-of-window turns are
    genuinely gone from the prompt → the warning is ACCURATE (dropped > 0), even with a
    stale agent id present (gemini ignores resume)."""
    request, svc = _make_request(monkeypatch, tmp_path, "gemini")
    save_llm_settings(tmp_path, LlmSettings(provider="gemini", chat_max_turns=2))
    chat_context.persist_agent_id(request, CONV, "agent-stale")  # ignored — stateless
    count = _bump_turns(svc, 3)
    assert chat_context._llm_dropped_turns_sync(request, CONV) == max(0, count - 2)


def test_record_context_assemble_metrics(monkeypatch: pytest.MonkeyPatch) -> None:
    from akana_server.observability.metrics import MetricsRegistry

    reg = MetricsRegistry()
    monkeypatch.setattr("akana_server.observability.metrics.registry", reg)
    assert chat_context.record_context_assemble_metrics(skipped_resume=True) == "resume"
    assert chat_context.record_context_assemble_metrics(skipped_resume=False) == "bootstrap"
    snap = reg.snapshot()
    assert snap["counters"]["llm_history_skipped_resume"]["value"] == 1
    assert snap["counters"]["llm_history_bootstrap"]["value"] == 1


def test_record_agent_timing_metric(monkeypatch: pytest.MonkeyPatch) -> None:
    from akana_server.observability.metrics import MetricsRegistry

    reg = MetricsRegistry()
    monkeypatch.setattr("akana_server.observability.metrics.registry", reg)
    chat_context.record_agent_timing_metric("resume")
    chat_context.record_agent_timing_metric("resume_failed")
    chat_context.record_agent_timing_metric("create")
    chat_context.record_agent_timing_metric("session")
    chat_context.record_agent_timing_metric("unknown")
    snap = reg.snapshot()
    assert snap["counters"]["llm_session_resume_ok"]["value"] == 1
    assert snap["counters"]["llm_session_resume_failed"]["value"] == 1
    assert snap["counters"]["llm_session_created"]["value"] == 1
    assert snap["counters"]["llm_session_cache_hit"]["value"] == 1


# -- QUALITY turn: LLM history window + dropped boundary values ----------------------


def _seed_turns(svc: ConversationService, rows: list[tuple[str, str]]) -> None:
    ep = svc._episodic  # noqa: SLF001 - writing to episodic directly is cleanest in the test
    for i, (role, text) in enumerate(rows):
        ep.append_turn(
            turn_id=f"t{i}",
            conversation_id=CONV,
            role=role,  # type: ignore[arg-type]
            text=text,
            ts=f"2026-06-01T10:00:{i:02d}.000Z",
        )


def test_history_filters_tool_and_system_roles(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """The LLM window carries only user/assistant rows; tool/system are dropped."""
    request, svc = _make_request(monkeypatch, tmp_path, "cursor")
    save_llm_settings(tmp_path, LlmSettings(provider="cursor", chat_max_turns=12))
    _seed_turns(
        svc,
        [
            ("user", "q1"),
            ("assistant", "a1"),
            ("tool", "araç çıktısı"),
            ("system", "sistem notu"),
            ("user", "q2"),
            ("assistant", "a2"),
        ],
    )
    msgs, _dropped = chat_context._llm_history_and_dropped_sync(request, CONV)
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert [m["content"] for m in msgs] == ["q1", "a1", "q2", "a2"]


def test_history_only_assistant_message_survives(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """If history has only a single assistant message (no matching user), it is preserved."""
    request, svc = _make_request(monkeypatch, tmp_path, "cursor")
    save_llm_settings(tmp_path, LlmSettings(provider="cursor", chat_max_turns=12))
    _seed_turns(svc, [("assistant", "yalnız asistan")])
    msgs, _dropped = chat_context._llm_history_and_dropped_sync(request, CONV)
    assert msgs == [{"role": "assistant", "content": "yalnız asistan"}]


def test_history_max_turns_two_keeps_latest_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """chat_max_turns=2 (lower bound) → only the last 2 rows are returned chronologically."""
    request, svc = _make_request(monkeypatch, tmp_path, "cursor")
    save_llm_settings(tmp_path, LlmSettings(provider="cursor", chat_max_turns=2))
    _seed_turns(
        svc,
        [("user", "q1"), ("assistant", "a1"), ("user", "q2"), ("assistant", "a2")],
    )
    msgs, _dropped = chat_context._llm_history_and_dropped_sync(request, CONV)
    assert msgs == [
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
    ]


def test_history_without_service_is_empty_not_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """If there is no conversation_service → ([], 0) — the hot path never blows up."""
    from types import SimpleNamespace

    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    settings = load_settings()
    app = SimpleNamespace(
        state=SimpleNamespace(settings=settings, conversation_service=None)
    )
    request = SimpleNamespace(app=app)
    assert chat_context._llm_history_and_dropped_sync(request, CONV) == ([], 0)


def test_chat_max_turns_clamped_to_valid_range(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Behavior-fixing: chat_max_turns is clamped to [2,64] when read from the settings file.

    A negative/0 value must not lead to the SQLite ``LIMIT -1`` (all rows) foot-gun;
    invalid input falls back to the default (12)."""
    from akana_server.llm_settings import load_llm_settings

    save_llm_settings(tmp_path, LlmSettings(provider="cursor", chat_max_turns=2))
    # Write invalid values to the file by hand and verify the merge clamp.
    import json

    path = tmp_path / "llm_settings.json"
    raw = json.loads(path.read_text("utf-8"))
    # Numeric but out-of-range → clamped to the nearest bound (lo=2, hi=64);
    # unparseable (str) → default 12.
    for bad, expected in [(-5, 2), (0, 2), (1, 2), (999, 64), ("abc", 12)]:
        raw["chat_max_turns"] = bad
        path.write_text(json.dumps(raw), "utf-8")
        settings = load_settings()
        loaded = load_llm_settings(tmp_path, settings)
        assert loaded.chat_max_turns == expected, f"{bad!r} → {loaded.chat_max_turns}"
