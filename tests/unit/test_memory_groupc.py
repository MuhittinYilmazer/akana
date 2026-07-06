"""Group C — remaining audit fixes (vector reindex-on-recovery, conversation
history windowing + soft-delete search, orchestrator/vector-health locking)."""

from __future__ import annotations

from pathlib import Path

import pytest

from akana.memory import EmbeddingError, HashingEmbedder, VectorIndexer, VectorStore
from akana.memory.conversations import ConversationStore
from akana.memory.episodic import EpisodicStore
from akana.memory.events import MemoryEvent
from akana.memory.vector_recall import VectorHealth


@pytest.fixture()
def episodic(tmp_path: Path) -> EpisodicStore:
    return EpisodicStore(tmp_path / "memory.db")


@pytest.fixture()
def convos(tmp_path: Path, episodic: EpisodicStore) -> ConversationStore:
    return ConversationStore(tmp_path / "memory.db", episodic=episodic)


class _FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


# ── C24: recent_llm_messages is not starved by a long tool/system tail ────────
def test_recent_llm_messages_survives_long_tool_tail(
    convos: ConversationStore, episodic: EpisodicStore
) -> None:
    episodic.append_turn(turn_id="u1", conversation_id="c1", role="user", text="soru")
    episodic.append_turn(turn_id="a1", conversation_id="c1", role="assistant", text="cevap")
    for i in range(1100):  # a long agentic tail AFTER the last exchange (> the old 1000 cap)
        episodic.append_turn(turn_id=f"tool{i:04d}", conversation_id="c1", role="tool", text=f"t{i}")

    msgs = convos.recent_llm_messages("c1", max_turns=5)
    # The user/assistant messages survive (old code fetched the newest 1000 turns — all
    # tool — and filtered to [], starving the model of prior context).
    assert [(m["role"], m["content"]) for m in msgs] == [("user", "soru"), ("assistant", "cevap")]


def test_recent_llm_messages_returns_newest_cap(
    convos: ConversationStore, episodic: EpisodicStore
) -> None:
    for i in range(10):
        episodic.append_turn(turn_id=f"u{i}", conversation_id="c1", role="user", text=f"m{i}")
    msgs = convos.recent_llm_messages("c1", max_turns=3)
    assert [m["content"] for m in msgs] == ["m7", "m8", "m9"]  # newest 3, chronological


# ── C31: store-level search excludes soft-deleted conversations ───────────────
def test_search_excludes_soft_deleted(convos: ConversationStore) -> None:
    convos.on_user_message("c1", "Berlin uçuşu planı")  # creates + auto-titles
    assert [m.id for m in convos.search("Berlin")] == ["c1"]  # found while live
    convos.merge_json_metadata("c1", {"deleted": True})  # soft-delete (no archived flag)
    assert convos.search("Berlin") == []  # store-level search now hides it


# ── C11: VectorIndexer replays facts skipped during a health cooldown ─────────
def test_vector_indexer_replays_dirty_fact_after_cooldown(tmp_path: Path) -> None:
    clock = _FakeClock()
    health = VectorHealth(cooldown_s=100.0, clock=clock)
    store = VectorStore(tmp_path / "c11.db")
    indexer = VectorIndexer(store, HashingEmbedder(), health=health)

    health.record_failure(EmbeddingError("backend down"))  # trip the breaker
    assert not health.active()

    # A fact event during cooldown is skipped (not indexed) but remembered as dirty.
    indexer.on_event(MemoryEvent(kind="fact", ts="t0", data={"fact_id": "f1", "key": "sehir", "value": "izmir"}))
    assert store.count() == 0

    clock.t = 200.0  # cooldown expires
    assert health.active()
    # A new fact event now indexes itself AND replays the skipped f1.
    indexer.on_event(MemoryEvent(kind="fact", ts="t1", data={"fact_id": "f2", "key": "renk", "value": "mavi"}))
    assert store.count() == 2  # both f2 (live) and f1 (replayed) landed


def test_vector_indexer_drops_dirty_on_invalidate(tmp_path: Path) -> None:
    clock = _FakeClock()
    health = VectorHealth(cooldown_s=100.0, clock=clock)
    store = VectorStore(tmp_path / "c11b.db")
    indexer = VectorIndexer(store, HashingEmbedder(), health=health)

    health.record_failure(EmbeddingError("down"))
    indexer.on_event(MemoryEvent(kind="fact", ts="t0", data={"fact_id": "f1", "key": "k", "value": "v"}))
    # The fact is invalidated while still dirty → it must NOT be re-embedded on recovery.
    indexer.on_event(MemoryEvent(kind="fact_invalidated", ts="t1", data={"fact_id": "f1"}))

    clock.t = 200.0
    indexer.on_event(MemoryEvent(kind="fact", ts="t2", data={"fact_id": "f2", "key": "k2", "value": "v2"}))
    assert store.count() == 1  # only f2; the invalidated f1 was not resurrected


# ── C28: VectorHealth breaker state is lock-guarded (smoke) ───────────────────
def test_vector_health_lock_guards_transitions() -> None:
    clock = _FakeClock()
    health = VectorHealth(cooldown_s=50.0, clock=clock)
    assert health.active()
    health.record_failure(EmbeddingError("x"))
    assert not health.active()  # cooldown engaged
    health.record_success()
    assert health.active()  # cleared


# ── C18/C19: graph relink is atomic + scoped ──────────────────────────────────
def test_graph_relink_replaces_edge(tmp_path: Path) -> None:
    from akana.memory.graph import GraphStore

    g = GraphStore(tmp_path / "g.db")
    g.link_fact(key="sehir", value="ankara", mem_id="f1")
    g.relink_fact(fact_id="f1", key="sehir", value="izmir")  # correct/supersede
    values = [n.label for n in g.neighbors("sehir", kind="fact_key")]
    assert values == ["izmir"]  # old 'ankara' edge dropped, new 'izmir' present


def test_graph_relink_is_atomic_on_link_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from akana.memory.graph import GraphStore

    g = GraphStore(tmp_path / "g2.db")
    g.link_fact(key="sehir", value="ankara", mem_id="f1")
    original = g._upsert_node

    def boom(conn, *, label, kind):  # fail the INSERT half, after the purge DELETE
        if kind == "fact_value":
            raise RuntimeError("link failed mid-relink")
        return original(conn, label=label, kind=kind)

    monkeypatch.setattr(g, "_upsert_node", boom)
    with pytest.raises(RuntimeError):
        g.relink_fact(fact_id="f1", key="sehir", value="izmir")
    # The purge must have rolled back — the old edge survives (no torn projection).
    values = [n.label for n in g.neighbors("sehir", kind="fact_key")]
    assert values == ["ankara"]


# ── C30: capture doesn't suppress a user-directed re-add of a rejected value ──
def test_capture_rejected_readd_respects_user_text(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from akana.memory import FactCandidate, Memory
    from akana_server.api.routes.chat.persist import _stage_candidates

    memory = Memory.for_data_dir(tmp_path)
    rejected = memory.staging.stage(
        FactCandidate(key="renk", value="mavi", extractor="llm_capture")
    )
    memory.staging.mark_rejected(rejected.id)  # value now in the recently-rejected set

    cand = SimpleNamespace(key="renk", value="mavi", reason="capture")
    # Model re-proposes it and the user did NOT restate it → suppressed (old behavior).
    assert _stage_candidates(memory, [cand], conversation_id="c1", user_text="başka bir konu") == []
    # The user restated the value THIS turn → it's a user-directed re-add, not suppressed.
    out = _stage_candidates(
        memory,
        [SimpleNamespace(key="renk", value="mavi", reason="capture")],
        conversation_id="c1",
        user_text="favori rengim mavi olsun",
    )
    assert len(out) == 1 and out[0]["kind"] == "staging"


# ── C30 (streaming/background path): the rejected-re-add rescue must fire there too ──
def test_capture_background_respects_user_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The STREAMING (default web-UI) background capture threads user_text into staging.

    Old bug: _capture_memory_background called _stage_candidates WITHOUT user_text, so
    user_fold folded to "" and the C30 rescue never fired on the primary path — a value
    the user just restated but that was previously rejected was silently dropped for ~30
    days, while the same sentence on the voice/blocking path staged."""
    import asyncio
    from types import SimpleNamespace

    from akana.memory import FactCandidate
    from akana_server.api.routes.chat.persist import _capture_memory_background
    from akana_server.config import load_settings
    from akana_server.memory_core import get_memory_core

    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    settings = load_settings()  # real Settings — the capture path isinstance-checks it

    # Stage into the SAME store the background capture reads (get_memory_core(settings.data_dir)).
    memory = get_memory_core(settings.data_dir)
    rejected = memory.staging.stage(
        FactCandidate(key="renk", value="mavi", extractor="llm_capture")
    )
    memory.staging.mark_rejected(rejected.id)  # value is now in the recently-rejected set

    async def _fake_propose(*_args, **_kwargs):
        return [SimpleNamespace(key="renk", value="mavi", reason="capture")]

    monkeypatch.setattr(
        "akana_server.api.routes.chat.propose_memory_captures", _fake_propose, raising=False
    )

    app = SimpleNamespace(state=SimpleNamespace(settings=settings, event_hub=None))

    async def _run() -> None:
        await _capture_memory_background(
            app,
            conversation_id="c1",
            user_text="favori rengim mavi olsun",  # user restated the value THIS turn
            assistant_text="tamam",
            model=None,
        )

    asyncio.run(_run())
    # The user-directed re-add was staged (NOT suppressed by the recently-rejected set).
    pending = memory.staging.list_pending(limit=10)
    assert any(p.key == "renk" and p.value == "mavi" for p in pending), (
        "background capture dropped a user-directed re-add (user_text not threaded to staging)"
    )


# ── D1: a FAILED memory_remember must not suppress the fallback auto-capture ──
def test_turn_wrote_memory_ignores_failed_call() -> None:
    from akana_server.api.routes.chat.chat_state import _turn_wrote_memory

    # A successful write counts (suppress the redundant fallback capture).
    assert _turn_wrote_memory([{"name": "memory_remember", "status": "ok"}]) is True
    assert _turn_wrote_memory([{"name": "save_memory"}]) is True  # no status = not errored
    # A FAILED write stored nothing → must NOT count, so the fallback capture still runs.
    assert _turn_wrote_memory([{"name": "memory_remember", "status": "error"}]) is False
    assert (
        _turn_wrote_memory(
            [{"name": "mcp__akana_memory__memory_remember", "status": "error"}]
        )
        is False
    )
