"""MemoryOrchestrator — the ``memory.*`` tool surface (Vision §8 + §11)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from akana.memory import (
    MEMORY_TOOLS,
    Memory,
    OrchestratorSettings,
    tool_schemas,
)
from akana.memory.tools import (
    derive_key,
    kind_from_key,
    parse_time_bound,
    parse_time_point,
    parse_time_range,
)


@pytest.fixture()
def memory(tmp_path):
    return Memory.for_data_dir(tmp_path)


@pytest.fixture()
def orch(memory):
    return memory.make_orchestrator()


@pytest.fixture()
def orch_direct(memory):
    """Orchestrator with the K30 clamp deliberately opened (direct-write tests)."""
    return memory.make_orchestrator(settings=OrchestratorSettings(allow_direct=True))


def _seed_fact(memory, key="kedi adı", value="Pamuk", **kw):
    kw.setdefault("trust", "user_statement")
    return memory.assert_fact_direct(key=key, value=value, **kw)[1]


# -- contracts ------------------------------------------------------------------


def test_tool_schemas_shape():
    schemas = tool_schemas()
    names = [s["name"] for s in schemas]
    assert names == list(MEMORY_TOOLS)
    search = schemas[0]
    assert search["input_schema"]["required"] == ["query"]
    assert "intent" in search["input_schema"]["properties"]
    # schemas() returns a static copy; mutation does not corrupt the original
    search["input_schema"]["required"].append("hacked")
    assert tool_schemas()[0]["input_schema"]["required"] == ["query"]


def test_unknown_tool(orch):
    out = orch.handle_tool_call("memory.nope", {})
    assert out["error"]["code"] == "unknown_tool"


def test_invalid_request_missing_query(orch):
    out = orch.handle_tool_call("memory.search", {})
    assert out["error"]["code"] == "invalid_request"
    assert "query" in out["error"]["message"]


def test_internal_error_is_enveloped(memory):
    orch = memory.make_orchestrator()
    orch.register_strategy("rrf", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    out = orch.handle_tool_call("memory.search", {"query": "kedi"})
    assert out["error"]["code"] == "internal_error"


# -- memory.search ----------------------------------------------------------------


def test_search_returns_items_explain_id_and_trace(memory, orch):
    _seed_fact(memory)
    memory.remember_turn(role="user", conversation_id="c1", text="Kedimin adı Pamuk olsun")
    out = orch.handle_tool_call("memory.search", {"query": "kedi"})
    assert "error" not in out
    assert out["items"], "seeded fact should be recalled"
    kinds = {i["type"] for i in out["items"]}
    assert "Fact" in kinds
    assert out["explain_id"]
    assert out["trace"]["strategy"] == "fts_first"
    assert set(out["trace"]["weights"]) == {"graph", "vector", "fts"}
    # The orchestrator retains the last trace_cap traces internally (bounded
    # OrderedDict) even though there is no public reader for them.
    stored = orch._traces.get(out["explain_id"])
    assert stored is not None
    assert stored["result_ids"] == [i["id"] for i in out["items"]]
    assert any(s["stage"] == "budget_trim" for s in stored["stages"])


def test_search_intent_budget_defaults(memory, orch):
    _seed_fact(memory)
    out = orch.handle_tool_call("memory.search", {"query": "kedi", "intent": "fact_lookup"})
    assert out["trace"]["budget_tokens"] == 200  # §11.2
    out2 = orch.handle_tool_call("memory.search", {"query": "kedi", "budget_tokens": 1500})
    assert out2["trace"]["budget_tokens"] == 1500


def test_search_min_trust_gate(memory, orch):
    _seed_fact(memory, key="tahmin", value="belki", trust="tool_output")
    out = orch.handle_tool_call("memory.search", {"query": "tahmin"})
    assert out["items"] == []  # default floor: inferred
    out2 = orch.handle_tool_call("memory.search", {"query": "tahmin", "min_trust": "tool_output"})
    assert out2["items"]


def test_search_types_filter(memory, orch):
    _seed_fact(memory, key="kedi adı", value="Pamuk")
    memory.remember_turn(role="user", conversation_id="c1", text="kedi maması aldım")
    out = orch.handle_tool_call("memory.search", {"query": "kedi", "types": ["Episode"]})
    assert out["items"]
    assert {i["type"] for i in out["items"]} == {"Episode"}


def test_search_time_range(memory, orch):
    _seed_fact(memory)
    fresh = orch.handle_tool_call(
        "memory.search", {"query": "kedi", "time_range": {"from": "relative:1h"}}
    )
    assert fresh["items"]
    future = orch.handle_tool_call(
        "memory.search", {"query": "kedi", "time_range": {"from": "2999-01-01"}}
    )
    assert future["items"] == []


def test_search_invalid_time_range_is_explicit_error(memory, orch):
    """A broken time_range is not silently swallowed — same contract as as_of/observed_*:
    an explicit ``invalid_request`` envelope (becomes 400 on the recall path, isError in MCP)."""
    _seed_fact(memory)
    bad_from = orch.handle_tool_call(
        "memory.search", {"query": "kedi", "time_range": {"from": "saçma ifade"}}
    )
    assert bad_from["error"]["code"] == "invalid_request"
    assert "time_range.from" in bad_from["error"]["message"]
    bad_to = orch.handle_tool_call(
        "memory.search", {"query": "kedi", "time_range": {"to": "öyle bir zaman yok"}}
    )
    assert bad_to["error"]["code"] == "invalid_request"
    assert "time_range.to" in bad_to["error"]["message"]
    # An empty time_range object is not broken — behaves as if no filter is set.
    empty = orch.handle_tool_call("memory.search", {"query": "kedi", "time_range": {}})
    assert "error" not in empty and empty["items"]


def test_search_warnings_for_unsupported(memory, orch):
    # as_of is now supported (M3.1) — no longer warns here; covered in separate tests.
    _seed_fact(memory)
    out = orch.handle_tool_call(
        "memory.search",
        {"query": "kedi", "intent": "explore", "rerank": "cross_encoder"},
    )
    text = " ".join(out["warnings"])
    assert "fell back" in text  # vector_first is not registered yet
    assert "rerank" in text
    assert "as_of" not in text
    assert out["trace"]["strategy"] == "fts_first"
    assert out["trace"]["requested_strategy"] == "vector_first"


def test_register_strategy_door(memory, orch):
    _seed_fact(memory)
    orch.register_strategy(
        "vector_first", lambda **kw: memory.recall(kw.pop("query"), **kw)
    )
    out = orch.handle_tool_call("memory.search", {"query": "kedi", "intent": "explore"})
    assert out["trace"]["strategy"] == "vector_first"
    assert not any("fell back" in w for w in out["warnings"])


# -- memory.remember ---------------------------------------------------------------


def test_remember_stages_by_default(memory, orch):
    out = orch.handle_tool_call(
        "memory.remember", {"content": "Alice koyu tema sever", "kind": "preference"}
    )
    assert out["status"] == "staged"
    assert memory.staging.count_pending() == 1
    assert out["key"].startswith("preference:")
    # staged ≠ durable: does not enter recall (K30)
    search = orch.handle_tool_call("memory.search", {"query": "koyu tema"})
    assert search["items"] == []


def test_remember_direct_clamped_by_default(memory, orch):
    """K30 inbox_only: with default settings, direct/supersede requests fall through to staging."""
    out = orch.handle_tool_call(
        "memory.remember",
        {"content": "Pamuk", "kind": "fact", "key": "kedi adı", "policy": "direct"},
    )
    assert out["status"] == "staged"
    assert out["requested_policy"] == "direct"
    assert memory.staging.count_pending() == 1
    fact = _seed_fact(memory, key="şehir", value="Ankara")
    out2 = orch.handle_tool_call(
        "memory.remember", {"content": "İzmir", "kind": "fact", "supersedes": fact.id}
    )
    assert out2["status"] == "staged"
    assert out2["requested_policy"] == "supersede"
    assert memory.get_fact(fact.id).is_valid is True  # target was not touched


def test_like_wildcards_literal(memory, orch):
    """LLM-controlled % / _ wildcards stay literal in LIKE (escaped)."""
    _seed_fact(memory, key="kedi adı", value="Pamuk")
    out = orch.handle_tool_call("memory.search", {"query": "Pa%uk"})
    assert out["items"] == []


def test_remember_direct_stores_fact(memory, orch_direct):
    out = orch_direct.handle_tool_call(
        "memory.remember",
        {"content": "Pamuk", "kind": "fact", "key": "kedi adı", "policy": "direct"},
    )
    assert out["status"] == "stored"
    fact = memory.get_fact(out["fact_id"])
    assert fact is not None and fact.value == "Pamuk" and fact.key == "kedi adı"
    assert fact.extractor == "memory.remember"


def test_remember_promotes_stage_to_direct_when_allowed(memory, orch_direct):
    """allow_direct ON + default request (policy unspecified = stage) → SKIP the
    inbox, write directly. User decision: 'if unconfirmed remembering is on, don't
    let it fall into the inbox; if it's off, everything goes to the inbox'
    (opposite of: test_remember_stages_by_default)."""
    out = orch_direct.handle_tool_call(
        "memory.remember", {"content": "Alice koyu tema sever", "kind": "preference"}
    )
    assert out["status"] == "stored"  # NOT 'staged' — it was promoted
    assert memory.staging.count_pending() == 0  # inbox empty
    search = orch_direct.handle_tool_call("memory.search", {"query": "koyu tema"})
    assert search["items"], "directly written fact should enter recall"


def test_remember_kind_prefix_searchable(memory, orch_direct):
    orch_direct.handle_tool_call(
        "memory.remember",
        {
            "content": "FTS5 bazı SQLite build'lerinde yok; LIKE fallback şart",
            "kind": "lesson",
            "key": "sqlite fts",
            "policy": "direct",
        },
    )
    out = orch_direct.handle_tool_call("memory.search", {"query": "sqlite", "types": ["Lesson"]})
    assert out["items"]
    assert out["items"][0]["type"] == "Lesson"


def test_remember_supersedes(memory, orch_direct):
    old = _seed_fact(memory, key="şehir", value="Ankara")
    out = orch_direct.handle_tool_call(
        "memory.remember",
        {"content": "İstanbul", "kind": "fact", "supersedes": old.id},
    )
    assert out["status"] == "superseded"
    assert memory.get_fact(old.id).is_valid is False
    assert memory.get_fact(out["fact_id"]).value == "İstanbul"


def test_remember_supersedes_missing_target(orch_direct):
    out = orch_direct.handle_tool_call(
        "memory.remember", {"content": "x", "kind": "fact", "supersedes": "yok-boyle-id"}
    )
    assert out["error"]["code"] == "not_found"


# -- memory.forget -----------------------------------------------------------------


def test_forget_retract_and_audit(memory, orch):
    fact = _seed_fact(memory)
    out = orch.handle_tool_call(
        "memory.forget", {"target_id": fact.id, "reason": "yanlış bilgiydi"}
    )
    assert out["status"] == "forgotten"
    assert memory.get_fact(fact.id).is_valid is False
    audits = memory.ledger.read_all(kind="memory.forget")
    assert audits and audits[-1].data["reason"] == "yanlış bilgiydi"
    # the second call behaves idempotently
    again = orch.handle_tool_call("memory.forget", {"target_id": fact.id})
    assert again["status"] == "already_inactive"


def test_forget_supersede_requires_new_value(orch):
    out = orch.handle_tool_call("memory.forget", {"target_id": "x", "mode": "supersede"})
    assert out["error"]["code"] == "invalid_request"


def test_forget_supersede(memory, orch):
    fact = _seed_fact(memory, key="şehir", value="Ankara")
    out = orch.handle_tool_call(
        "memory.forget", {"target_id": fact.id, "mode": "supersede", "new_value": "İzmir"}
    )
    assert out["status"] == "superseded"
    assert memory.get_fact(out["new_id"]).value == "İzmir"


def test_forget_staged_candidate(memory, orch):
    staged = orch.handle_tool_call("memory.remember", {"content": "geçici", "kind": "fact"})
    out = orch.handle_tool_call("memory.forget", {"target_id": staged["staged_id"]})
    assert out["status"] == "rejected_staged"
    assert memory.staging.get(staged["staged_id"]).status == "rejected"


def test_forget_not_found(orch):
    out = orch.handle_tool_call("memory.forget", {"target_id": "hayalet"})
    assert out["error"]["code"] == "not_found"


# -- rate limit + helpers ------------------------------------------------------------


def test_rate_limit(memory):
    orch = memory.make_orchestrator(
        settings=OrchestratorSettings(rate_limits={"memory.search": 2})
    )
    assert "error" not in orch.handle_tool_call("memory.search", {"query": "a"})
    assert "error" not in orch.handle_tool_call("memory.search", {"query": "b"})
    out = orch.handle_tool_call("memory.search", {"query": "c"})
    assert out["error"]["code"] == "rate_limited"


def test_key_helpers():
    assert derive_key("Kullanıcı koyu tema sever", "preference").startswith("preference:")
    assert derive_key("Pamuk", "fact") == "pamuk"
    assert kind_from_key("lesson:sqlite fts") == "lesson"
    assert kind_from_key("kedi adı") == "fact"


def test_parse_time_point():
    assert parse_time_point(None) is None
    assert parse_time_point("garbage") is None
    assert parse_time_point("2026-06-01T00:00:00+00:00") == "2026-06-01T00:00:00.000Z"
    # input with an offset is normalized to UTC (+03:00 — Istanbul)
    assert parse_time_point("2026-06-01T03:00:00+03:00") == "2026-06-01T00:00:00.000Z"
    assert parse_time_point("2026-06-01") == "2026-06-01T00:00:00.000Z"
    rel = parse_time_point("relative:7d")
    assert rel is not None and rel.endswith("Z")


def test_parse_time_range_turkish():
    """Turkish natural-language expressions: TR-local (+03:00) day boundaries → ISO-UTC pair."""
    # Thursday, 11 June 2026, 15:00 Istanbul (12:00 UTC)
    now = datetime(2026, 6, 11, 12, 0, tzinfo=UTC)
    assert parse_time_range(None) is None
    assert parse_time_range("saçma ifade", now=now) is None
    assert parse_time_range("dün", now=now) == (
        "2026-06-09T21:00:00.000Z",  # 10 June 00:00 TR
        "2026-06-10T20:59:59.999Z",  # 10 June 23:59:59.999 TR
    )
    # The accent-free spelling yields the same result
    assert parse_time_range("Dun", now=now) == parse_time_range("dün", now=now)
    assert parse_time_range("bugün", now=now) == (
        "2026-06-10T21:00:00.000Z",
        "2026-06-11T20:59:59.999Z",
    )
    # Last week: Monday 1 June – Sunday 7 June (TR)
    assert parse_time_range("geçen hafta", now=now) == (
        "2026-05-31T21:00:00.000Z",
        "2026-06-07T20:59:59.999Z",
    )
    # Month name (+ suffix/+'ayında' forms) — March of the current year
    mart = ("2026-02-28T21:00:00.000Z", "2026-03-31T20:59:59.999Z")
    assert parse_time_range("mart ayında", now=now) == mart
    assert parse_time_range("Martta", now=now) == mart
    assert parse_time_range("mart", now=now) == mart
    # A month that hasn't arrived yet falls into last year (in June, "aralık" → December 2025)
    assert parse_time_range("aralık", now=now)[0].startswith("2025-11-30")
    # An explicit year always wins
    assert parse_time_range("mart 2025", now=now)[0].startswith("2025-02-28")
    # Sliding windows are anchored to now
    assert parse_time_range("son 7 gün", now=now) == (
        "2026-06-04T12:00:00.000Z",
        "2026-06-11T12:00:00.000Z",
    )
    assert parse_time_range("3 gün önce", now=now) == (
        "2026-06-07T21:00:00.000Z",
        "2026-06-08T20:59:59.999Z",
    )
    assert parse_time_range("geçen ay", now=now) == (
        "2026-04-30T21:00:00.000Z",
        "2026-05-31T20:59:59.999Z",
    )
    assert parse_time_range("relative:1d", now=now) == (
        "2026-06-10T12:00:00.000Z",
        "2026-06-11T12:00:00.000Z",
    )


def test_parse_time_bound_edges():
    now = datetime(2026, 6, 11, 12, 0, tzinfo=UTC)
    assert parse_time_bound(None) is None
    assert parse_time_bound("garbage", now=now) is None
    # An ISO instant stays an instant at both edges…
    assert parse_time_bound("2026-06-01T00:00:00+00:00", edge="end") == "2026-06-01T00:00:00.000Z"
    # …but a date-only value spans the whole day at the end edge (as_of=date → end of day)
    assert parse_time_bound("2026-03-05", edge="start") == "2026-03-05T00:00:00.000Z"
    assert parse_time_bound("2026-03-05", edge="end") == "2026-03-05T23:59:59.999Z"
    # Turkish expression: from = start of range, to = end of range
    assert parse_time_bound("dün", edge="start", now=now) == "2026-06-09T21:00:00.000Z"
    assert parse_time_bound("dün", edge="end", now=now) == "2026-06-10T20:59:59.999Z"


def test_parse_time_range_curly_apostrophe():
    """A curly apostrophe (’, iOS/smart keyboard) is recognized like a straight one (').

    Previously only the straight apostrophe was stripped; "Mart’ta" → None was returned
    and the orchestrator gave invalid_request for a valid Turkish expression.
    """
    now = datetime(2026, 6, 11, 12, 0, tzinfo=UTC)
    expected = parse_time_range("Mart'ta", now=now)
    assert expected is not None
    assert parse_time_range("Mart’ta", now=now) == expected
    assert parse_time_range("mayıs’ta", now=now) == parse_time_range("mayısta", now=now)
    # the as_of path also goes through the same normalization
    assert parse_time_bound("Mart’ta", edge="end", now=now) == expected[1]


# -- B: recall also sees pending ('I don't know but it's in your memory' fix) --------------


def test_search_surfaces_pending_inbox_facts(orch, memory):
    """PENDING (unconfirmed) info matching the query is returned in the 'pending' field
    with the 'onay_bekliyor' label + a warning — instead of 'I don't know', the assistant
    reports what is awaiting confirmation."""
    from akana.memory.staging import FactCandidate

    memory.staging.stage(FactCandidate(key="kedi_adi", value="Pamuk", extractor="llm"))
    out = orch.handle_tool_call("memory.search", {"query": "kedi"})

    assert out["pending"], "pending matching the query was expected"
    p = out["pending"][0]
    assert p["status"] == "onay_bekliyor"
    assert p["key"] == "kedi_adi" and p["value"] == "Pamuk"
    assert any("AWAITING APPROVAL" in w for w in out["warnings"])


def test_search_pending_filters_unmatched(orch, memory):
    """Pending returns only what matches the query."""
    from akana.memory.staging import FactCandidate

    memory.staging.stage(FactCandidate(key="kedi_adi", value="Pamuk", extractor="llm"))
    memory.staging.stage(FactCandidate(key="araba_rengi", value="Mavi", extractor="llm"))
    out = orch.handle_tool_call("memory.search", {"query": "kedi"})
    keys = {p["key"] for p in out["pending"]}
    assert keys == {"kedi_adi"}  # the car does not match


def test_search_no_pending_when_nothing_staged(orch, memory):
    """If the inbox is empty, pending returns empty with no warning (existing behavior is preserved)."""
    _seed_fact(memory, key="sehir", value="İstanbul")
    out = orch.handle_tool_call("memory.search", {"query": "sehir"})
    assert out["pending"] == []
    assert not any("AWAITING APPROVAL" in w for w in out["warnings"])
