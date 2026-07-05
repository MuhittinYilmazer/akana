"""Vector recall — embeddings, indexer events and the vector_first/rrf strategies."""

from __future__ import annotations

import pytest

from akana.memory import (
    HashingEmbedder,
    Memory,
    VectorIndexer,
    VectorStore,
    enable_vector_recall,
    make_rrf_strategy,
    make_vector_strategy,
)

RRF_RANK1 = 1.0 / (60 + 1)  # max contribution a single ranking can give


class _RenamedEmbedder:
    """Same vector space (HashingEmbedder), different model name.

    For model-filter tests: the two "models" produce vectors of the same
    dimension — without the filter there is no way to tell them apart.
    """

    def __init__(self, name: str, *, dim: int = 256) -> None:
        self._name = name
        self._base = HashingEmbedder(dim=dim)

    @property
    def name(self) -> str:
        return self._name

    def embed(self, texts: list[str]) -> list[list[float]]:
        return self._base.embed(texts)


class _CountingEmbedder:
    """Counts ``embed()`` call count — for the subscriber-leak (#19) test."""

    def __init__(self) -> None:
        self._base = HashingEmbedder()
        self.calls = 0

    @property
    def name(self) -> str:
        return self._base.name

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return self._base.embed(texts)


@pytest.fixture()
def memory(tmp_path):
    return Memory.for_data_dir(tmp_path)


@pytest.fixture()
def embedder():
    return HashingEmbedder()


@pytest.fixture()
def store(tmp_path):
    return VectorStore.for_data_dir(tmp_path)  # same memory.db (K11)


# -- embedder + store ---------------------------------------------------------------


def test_hashing_embedder_deterministic_unit_vectors(embedder):
    v1, v2 = embedder.embed(["kedi maması"]), embedder.embed(["kedi maması"])
    assert v1 == v2  # crc32-based: stable across processes
    assert len(v1[0]) == 256
    assert abs(sum(x * x for x in v1[0]) - 1.0) < 1e-6


def test_index_and_search_finds_similar_text(store, embedder):
    store.index_fact("f1", "kedi adı: Pamuk", embedder)
    store.index_fact("f2", "favori dil: Python", embedder)
    assert store.count() == 2
    hits = store.search(embedder.embed(["kedimin adı neydi"])[0], limit=2)
    assert hits[0][0] == "f1"
    assert hits[0][1] > hits[1][1]
    assert store.delete("f1") is True
    assert store.count() == 1


def test_search_separates_same_dim_models(store):
    """Same-dimension vectors from different embedders are separated by the model filter."""
    a, b = _RenamedEmbedder("model-a"), _RenamedEmbedder("model-b")
    store.index_fact("fa", "kedi adı: Pamuk", a)
    store.index_fact("fb", "kedi adı: Pamuk", b)

    q = a.embed(["kedi adı"])[0]
    assert [h[0] for h in store.search(q, limit=5, model="model-a")] == ["fa"]
    assert [h[0] for h in store.search(q, limit=5, model="model-b")] == ["fb"]
    assert store.search(q, limit=5, model="model-yok") == []
    # model=None legacy behavior: the whole pool is scanned as if it were a single space
    assert {h[0] for h in store.search(q, limit=5)} == {"fa", "fb"}


# -- indexer (event seam) -------------------------------------------------------------


def test_indexer_follows_fact_events(memory, store, embedder):
    indexer = VectorIndexer(store, embedder)
    memory.subscribe(indexer.on_event)

    _closed, fact = memory.assert_fact_direct(key="kedi adı", value="Pamuk", trust="user_statement")
    assert store.count() == 1
    assert store.search(embedder.embed(["kedi adı"])[0], limit=1)[0][0] == fact.id

    # supersede: the old vector is dropped, the new one is written
    old, new = memory.supersede_fact(fact.id, new_value="Boncuk")
    assert store.count() == 1
    assert store.search(embedder.embed(["kedi adı"])[0], limit=1)[0][0] == new.id

    memory.forget_fact(new.id)
    assert store.count() == 0


def test_indexer_detach_unsubscribes_and_is_idempotent(memory, store, embedder):
    indexer = VectorIndexer(store, embedder)
    indexer._unsubscribe = memory.subscribe(indexer.on_event)
    memory.assert_fact_direct(key="k1", value="v1", trust="user_statement")
    assert store.count() == 1

    indexer.detach()  # remove the subscription
    memory.assert_fact_direct(key="k2", value="v2", trust="user_statement")
    assert store.count() == 1  # a detached indexer does not embed the new fact
    indexer.detach()  # second call is safe (idempotent) — does not blow up


def test_detach_prevents_duplicate_embeds_on_rebuild(memory, store):
    """#19: when the stack is rebuilt on the same in-process Memory (a settings change),
    if the old VectorIndexer is not detached every fact is embedded twice (subscriber leak)."""
    emb = _CountingEmbedder()
    idx1 = VectorIndexer(store, emb)
    idx1._unsubscribe = memory.subscribe(idx1.on_event)
    # If a second indexer subscribes WITHOUT detaching → double embed (proof of the leak).
    idx_leak = VectorIndexer(store, emb)
    idx_leak._unsubscribe = memory.subscribe(idx_leak.on_event)
    memory.assert_fact_direct(key="leak", value="v", trust="user_statement")
    assert emb.calls == 2  # two subscribers → two embeds

    # Correct rebuild: detach the old ones → one subscriber, one embed.
    idx1.detach()
    idx_leak.detach()
    emb.calls = 0
    idx2 = VectorIndexer(store, emb)
    idx2._unsubscribe = memory.subscribe(idx2.on_event)
    memory.assert_fact_direct(key="fixed", value="v", trust="user_statement")
    assert emb.calls == 1  # one subscriber → one embed (leak closed)


def test_reindex_backfills_existing_facts(memory, store, embedder):
    memory.assert_fact_direct(key="kedi adı", value="Pamuk")
    memory.assert_fact_direct(key="favori dil", value="Python")
    indexer = VectorIndexer(store, embedder)
    assert store.count() == 0  # indexer joined late, missed the event
    assert indexer.reindex(memory) == 2
    assert store.count() == 2


# -- enable_vector_recall + orchestrator ----------------------------------------------


def test_enable_without_embedder_is_noop(memory):
    orch = memory.make_orchestrator()
    assert enable_vector_recall(memory, orch, None) is None
    memory.assert_fact_direct(key="kedi adı", value="Pamuk", trust="user_statement")
    out = orch.handle_tool_call("memory.search", {"query": "kedi", "intent": "explore"})
    assert out["trace"]["strategy"] == "fts_first"  # the existing fallback is preserved
    assert any("fell back" in w for w in out["warnings"])


def test_enable_registers_vector_first_no_fallback(memory):
    orch = memory.make_orchestrator()
    indexer = enable_vector_recall(memory, orch, HashingEmbedder())
    assert isinstance(indexer, VectorIndexer)

    _closed, fact = memory.assert_fact_direct(key="kedi adı", value="Pamuk", trust="user_statement")
    out = orch.handle_tool_call("memory.search", {"query": "kedi adı", "intent": "explore"})
    assert "error" not in out
    assert out["trace"]["strategy"] == "vector_first"
    assert out["trace"]["requested_strategy"] == "vector_first"
    assert not any("fell back" in w for w in out["warnings"])
    assert [i["id"] for i in out["items"]] == [fact.id]
    assert out["items"][0]["type"] == "Fact"

    stored = orch._traces.get(out["explain_id"])
    assert stored is not None and stored["strategy"] == "vector_first"


def test_vector_first_respects_min_trust_floor(memory):
    orch = memory.make_orchestrator()
    enable_vector_recall(memory, orch, HashingEmbedder())
    memory.assert_fact_direct(key="tahmin", value="belki yağmur", trust="tool_output")
    out = orch.handle_tool_call("memory.search", {"query": "tahmin", "intent": "explore"})
    assert out["items"] == []  # default floor: inferred
    out2 = orch.handle_tool_call(
        "memory.search", {"query": "tahmin", "intent": "explore", "min_trust": "tool_output"}
    )
    assert out2["items"]


def test_vector_strategy_pins_search_to_own_model(memory, store):
    """The strategy passes embedder.name to store.search: a foreign model's row is not mixed in."""
    a, b = _RenamedEmbedder("model-a"), _RenamedEmbedder("model-b")
    _closed, fact = memory.assert_fact_direct(key="kedi adı", value="Pamuk", trust="user_statement")
    store.index_fact(fact.id, "kedi adı: Pamuk", b)  # indexed with the old/foreign model

    vector_first = make_vector_strategy(memory, store, a)
    assert vector_first(query="kedi adı").blocks == []  # no row in a's space

    store.index_fact(fact.id, "kedi adı: Pamuk", a)  # re-index with its own model
    assert [bl.source_ids[0] for bl in vector_first(query="kedi adı").blocks] == [fact.id]


# -- rrf -------------------------------------------------------------------------------


def test_rrf_strategy_fuses_keyword_and_vector(memory, store, embedder):
    indexer = VectorIndexer(store, embedder)
    memory.subscribe(indexer.on_event)
    _closed, fact = memory.assert_fact_direct(key="kedi adı", value="Pamuk", trust="user_statement")
    memory.remember_turn(role="user", conversation_id="c1", text="kedi maması almam lazım")

    rrf = make_rrf_strategy(memory, store, embedder)
    result = rrf(query="kedi", conversation_id="c1", limit=12, budget_tokens=1200)

    by_id = {b.source_ids[0]: b for b in result.blocks}
    assert fact.id in by_id  # both sources found it, a single block remained
    episodic = [b for b in result.blocks if b.kind == "episodic"]
    assert episodic, "the keyword side's episodic result must enter the fusion"
    # the fact got a contribution from both rankings: it exceeds the max a single list can give
    assert by_id[fact.id].score > RRF_RANK1 + 1e-9
    assert episodic[0].score <= RRF_RANK1
    assert result.blocks[0].source_ids[0] == fact.id  # a double source moves it to the front
    assert result.trace.episodic_candidates >= 1
    assert result.trace.semantic_candidates >= 2  # keyword + vector candidates


def test_rrf_budget_skips_bloated_block(memory, store, embedder):
    """Budget behavior on the rrf path too: a bloated block is skipped; if none fit it is clipped."""
    indexer = VectorIndexer(store, embedder)
    memory.subscribe(indexer.on_event)
    _closed, big = memory.assert_fact_direct(key="kedi tarihçesi", value="x" * 3000, trust="user_statement")

    rrf = make_rrf_strategy(memory, store, embedder)
    # there is a single candidate and it does not fit: it is clipped and returned on its own
    clipped = rrf(query="kedi", budget_tokens=20)
    assert clipped.trace.returned == 1
    assert len(clipped.blocks[0].text) <= 20 * 4
    assert clipped.trace.total_tokens <= 20

    # when a small candidate is added: the bloated one is skipped, the small one fits the budget
    _closed, small = memory.assert_fact_direct(key="kedi adı", value="Pamuk", trust="user_statement")
    result = rrf(query="kedi", budget_tokens=200)
    ids = {b.source_ids[0] for b in result.blocks}
    assert small.id in ids  # the old `break` used to drop this one too
    assert big.id not in ids
    assert result.trace.dropped_for_budget >= 1
    assert result.trace.total_tokens <= 200


def test_rrf_via_orchestrator_intent(memory):
    orch = memory.make_orchestrator()
    enable_vector_recall(memory, orch, HashingEmbedder())
    memory.assert_fact_direct(key="kedi adı", value="Pamuk", trust="user_statement")
    memory.remember_turn(role="user", conversation_id="c1", text="kedi maması almam lazım")

    out = orch.handle_tool_call(
        "memory.search",
        {"query": "kedi", "intent": "timeline"},
        conversation_id="c1",
    )
    assert "error" not in out
    assert out["trace"]["strategy"] == "rrf"
    assert not any("fell back" in w for w in out["warnings"])
    kinds = {i["type"] for i in out["items"]}
    assert {"Fact", "Episode"} <= kinds  # the two sources merged
