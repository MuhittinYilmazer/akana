"""Bug-blitz-4 regression tests for area memory-e2e.

One test per verified finding. Each asserts the behavior contract the fix restores;
before the fix the corresponding assertion fails for the documented reason.

* memory-e2e-1 — time_range/observed_* filters ran AFTER the strategy's k-truncation,
  so a time-scoped search returned [] despite matching records ranking past the top-k.
* memory-e2e-2 — episodic recall's user-role eligibility filter ran AFTER the SQL LIMIT,
  so assistant turns filled the bm25 window and starved the user's own statement.
* memory-e2e-3 — a dedup-hit returned/emitted the INCOMING value while the DB row kept
  the STORED value → API/graph/vector/ledger diverged from sqlite on a fold-equal write.
* memory-e2e-4 — /memory/stats reported vector active/available=true while the shared
  health breaker had permanently disabled the vector layer.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from akana.memory import Memory
from akana.memory.recall import Recall


@pytest.fixture()
def mem(tmp_path: Path) -> Memory:
    return Memory.for_data_dir(tmp_path)


# -- memory-e2e-1: window filter must see candidates past the top-k window ----------


def test_observed_window_reaches_records_below_top_k(mem: Memory) -> None:
    orch = mem.make_orchestrator()
    # A busy recent history on the topic (high importance) plus ONE older on-topic
    # record inside the requested observation window (low importance → ranks last).
    for i in range(14):
        mem.assert_fact_direct(
            key=f"kedi konu {i}",
            value=f"guncel deger {i}",
            trust="user_statement",
            importance=0.9,
            observed_at="2026-07-10T00:00:00.000Z",
        )
    mem.assert_fact_direct(
        key="kedi eski kayit",
        value="mart ayinda soylenen",
        trust="user_statement",
        importance=0.05,
        observed_at="2026-03-15T00:00:00.000Z",
    )
    out = orch.handle_tool_call(
        "memory.search",
        {
            "query": "kedi",
            "observed_from": "2026-03-01",
            "observed_to": "2026-03-31",
        },
    )
    assert "error" not in out
    summaries = " || ".join(i["summary"] for i in out["items"])
    # The March record matches the query AND the window; pre-fix the recency/importance
    # LIMIT dropped it before the observed filter ran, returning [].
    assert "mart ayinda soylenen" in summaries, (
        f"March in-window record must be reachable; got items={out['items']!r}"
    )


# -- memory-e2e-2: episodic role window must be applied before the LIMIT ------------


def test_user_turn_surfaces_despite_assistant_domination(mem: Memory) -> None:
    conv = "c1"
    # Assistant restates the topic many times (short, term-dense → strong bm25),
    # filling the top-`limit` window that the role filter would then discard.
    for _ in range(15):
        mem.remember_turn(role="assistant", conversation_id=conv, text="beta launch")
    # The user's own statement (longer → weaker bm25), inserted LAST (highest id).
    mem.remember_turn(
        role="user",
        conversation_id=conv,
        text="beta launch is scheduled for may fifth of this year",
    )
    res = Recall(mem.episodic, mem.semantic).recall(
        "beta launch", limit=12, min_trust=None
    )
    texts = " || ".join(b.text for b in res.blocks if b.kind == "episodic")
    assert "may fifth" in texts, (
        f"user's beta-launch statement must surface, not be starved; got {texts!r}"
    )


# -- memory-e2e-3: dedup-hit must echo the STORED value, not the incoming one -------


def test_dedup_hit_returns_stored_value_casing(mem: Memory) -> None:
    _closed, first = mem.assert_fact_direct(
        key="sehir", value="İstanbul", trust="user_statement"
    )
    assert first.value == "İstanbul"
    # A fold-equal re-assertion ('İstanbul' vs 'istanbul' fold identically) dedups onto
    # the existing row; the UPDATE leaves value/value_norm untouched, so the returned
    # fact must reflect the STORED spelling — the emit/graph/vector/ledger read this.
    _closed2, again = mem.assert_fact_direct(
        key="sehir", value="istanbul", trust="user_statement"
    )
    assert again.id == first.id, "fold-equal re-assertion should dedup onto the same row"
    assert again.value == "İstanbul", (
        f"dedup-hit must return the STORED value, not the incoming; got {again.value!r}"
    )
    # And the store itself agrees (no divergence between the emitted object and sqlite).
    stored = mem.get_fact(first.id)
    assert stored is not None and stored.value == "İstanbul"


# -- memory-e2e-4: /memory/stats vector health must consult the breaker -------------


def _fake_request(indexer: object) -> object:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(memory_indexer=indexer)))


def test_vector_health_reflects_permanent_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    from akana.memory.embed import ModelNotFoundError
    from akana.memory.vector_recall import VectorHealth
    from akana_server.api.routes import memory as memroutes

    ms = SimpleNamespace(vector="on", embed_backend="local", ollama_url="http://x")
    monkeypatch.setattr(memroutes, "get_memory_settings", lambda _req: ms)
    monkeypatch.setattr(memroutes, "_data_dir", lambda _req: Path("."))
    monkeypatch.setattr(
        memroutes, "VectorStore",
        SimpleNamespace(for_data_dir=lambda _d: SimpleNamespace(distinct_models=lambda: [])),
    )

    # Healthy breaker → active/available true (the honest baseline).
    healthy = SimpleNamespace(_health=VectorHealth())
    ok = memroutes._vector_health(_fake_request(healthy), 0)
    assert ok["active"] is True and ok["available"] is True and ok["status"] == "active"

    # Permanently tripped (missing/typo'd model, failed download) → degraded, NOT active.
    health = VectorHealth()
    health.record_failure(ModelNotFoundError("model gone"))
    dead = SimpleNamespace(_health=health)
    out = memroutes._vector_health(_fake_request(dead), 0)
    assert out["active"] is False, "permanently disabled vector layer must not report active"
    assert out["available"] is False
    assert out["status"] == "degraded"


def test_vector_health_reflects_cooldown_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    from akana.memory.embed import EmbeddingError
    from akana.memory.vector_recall import VectorHealth
    from akana_server.api.routes import memory as memroutes

    ms = SimpleNamespace(vector="on", embed_backend="local", ollama_url="http://x")
    monkeypatch.setattr(memroutes, "get_memory_settings", lambda _req: ms)
    monkeypatch.setattr(memroutes, "_data_dir", lambda _req: Path("."))
    monkeypatch.setattr(
        memroutes, "VectorStore",
        SimpleNamespace(for_data_dir=lambda _d: SimpleNamespace(distinct_models=lambda: [])),
    )
    health = VectorHealth()
    health.record_failure(EmbeddingError("transient blip"))
    out = memroutes._vector_health(_fake_request(SimpleNamespace(_health=health)), 0)
    assert out["active"] is False, "in-cooldown vector layer must not report active"
    # A transient cooldown is not a permanent loss: the embedder is still 'available'.
    assert out["available"] is True
    assert out["status"] == "cooldown"
