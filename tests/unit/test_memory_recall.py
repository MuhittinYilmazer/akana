"""Recall engine: fusion, trust filter, budget, scope, and explain trace."""

from __future__ import annotations

from pathlib import Path

import pytest

from akana.memory.episodic import EpisodicStore
from akana.memory.recall import Recall
from akana.memory.semantic import SemanticStore


@pytest.fixture()
def stores(tmp_path: Path) -> tuple[EpisodicStore, SemanticStore]:
    db = tmp_path / "memory.db"
    return EpisodicStore(db), SemanticStore(db)


@pytest.fixture()
def recall(stores: tuple[EpisodicStore, SemanticStore]) -> Recall:
    return Recall(*stores)


def test_fuses_semantic_above_episodic(
    stores: tuple[EpisodicStore, SemanticStore], recall: Recall
) -> None:
    episodic, semantic = stores
    semantic.upsert_fact(fact_id="f1", key="favori_içecek", value="kahve", trust="user_statement")
    episodic.append_turn(turn_id="t1", conversation_id="c1", role="user", text="kahve içtim")

    result = recall.recall("kahve")
    kinds = [b.kind for b in result.blocks]
    assert "semantic" in kinds
    assert "episodic" in kinds
    # semantic key-match outranks an episodic mention
    assert result.blocks[0].kind == "semantic"
    assert result.blocks[0].score > result.blocks[-1].score


def test_min_trust_filters_low_trust_facts(
    stores: tuple[EpisodicStore, SemanticStore], recall: Recall
) -> None:
    _, semantic = stores
    semantic.upsert_fact(fact_id="u", key="renk", value="mavi user", trust="user_statement")
    semantic.upsert_fact(fact_id="s", key="renk", value="mavi sentez", trust="synthesis")

    # default floor = inferred -> synthesis excluded
    default = recall.recall("mavi")
    assert {b.source_ids[0] for b in default.blocks} == {"u"}
    # lower the floor to admit synthesis
    loose = recall.recall("mavi", min_trust="synthesis")
    assert {b.source_ids[0] for b in loose.blocks} == {"u", "s"}


def test_budget_trims_and_trace_reports(
    stores: tuple[EpisodicStore, SemanticStore], recall: Recall
) -> None:
    _, semantic = stores
    for i in range(8):
        semantic.upsert_fact(fact_id=f"f{i}", key=f"renk{i}", value="mavi" * 20)

    result = recall.recall("mavi", budget_tokens=30)
    assert result.trace.semantic_candidates == 8
    assert result.trace.returned == len(result.blocks)
    assert result.trace.returned < result.trace.merged  # budget dropped some
    assert result.trace.dropped_for_budget > 0
    assert result.trace.total_tokens <= 30


def test_budget_skips_bloated_block_keeps_smaller(
    stores: tuple[EpisodicStore, SemanticStore], recall: Recall
) -> None:
    """A single bloated block must not drown the smaller results behind it (continue, not break)."""
    _, semantic = stores
    # importance=1.0 -> the bloated fact ranks FIRST by score; the old `break` here
    # was dropping the small one too (kept:0).
    semantic.upsert_fact(
        fact_id="big",
        key="şişkin mavi",
        value="x" * 3000,
        trust="user_statement",
        importance=1.0,
    )
    semantic.upsert_fact(fact_id="small", key="mavi renk", value="küçük", trust="user_statement")

    result = recall.recall("mavi", budget_tokens=200)
    ids = {b.source_ids[0] for b in result.blocks}
    assert ids == {"small"}  # the small fact is returned, the bloated one is skipped
    assert result.trace.dropped_for_budget == 1
    assert result.trace.total_tokens <= 200


def test_budget_clips_top_block_when_nothing_fits(
    stores: tuple[EpisodicStore, SemanticStore], recall: Recall
) -> None:
    """If no block fits: the best block is clipped to the budget and returned on its own."""
    _, semantic = stores
    semantic.upsert_fact(fact_id="b1", key="mavi dev", value="a" * 2000, trust="user_statement")
    semantic.upsert_fact(fact_id="b2", key="mavi devasa", value="b" * 2000, trust="user_statement")

    result = recall.recall("mavi", budget_tokens=50)
    assert len(result.blocks) == 1
    assert len(result.blocks[0].text) <= 50 * 4  # clip at ~4 characters/token
    assert result.blocks[0].text  # not empty — a recall with candidates never comes back empty-handed
    assert result.trace.returned == 1
    assert result.trace.dropped_for_budget == 1
    assert result.trace.total_tokens <= 50


def test_trace_records_terms_and_scope(recall: Recall) -> None:
    result = recall.recall("kahve", conversation_id="c1")
    assert "kahve" in result.trace.terms
    assert result.trace.conversation_id == "c1"
    assert result.trace.min_trust == "inferred"


def test_episodic_scope_by_conversation(
    stores: tuple[EpisodicStore, SemanticStore], recall: Recall
) -> None:
    episodic, _ = stores
    episodic.append_turn(turn_id="t1", conversation_id="c1", role="user", text="python sevdim")
    episodic.append_turn(turn_id="t2", conversation_id="c2", role="user", text="python sevdim")

    scoped = recall.recall("python", conversation_id="c1")
    src = {sid for b in scoped.blocks for sid in b.source_ids}
    assert src == {"t1"}


def test_dedup_merges_identical_text(
    stores: tuple[EpisodicStore, SemanticStore], recall: Recall
) -> None:
    episodic, _ = stores
    # same text in two conversations -> one block after fusion dedup
    episodic.append_turn(turn_id="t1", conversation_id="c1", role="user", text="aynı metin burada")
    episodic.append_turn(turn_id="t2", conversation_id="c2", role="user", text="aynı metin burada")
    result = recall.recall("aynı metin burada")
    assert len(result.blocks) == 1


def test_episodic_recall_skips_assistant_turns(
    stores: tuple[EpisodicStore, SemanticStore], recall: Recall
) -> None:
    """Assistant operational chatter must not pollute memory recall."""
    episodic, _ = stores
    episodic.append_turn(
        turn_id="a1",
        conversation_id="c1",
        role="assistant",
        text="WhatsApp mesajını gönderiyorum ve inbox'ı kontrol ediyorum",
    )
    episodic.append_turn(
        turn_id="u1",
        conversation_id="c1",
        role="user",
        text="WhatsApp'tan Banu'ya selam yaz",
    )

    result = recall.recall("WhatsApp Banu")
    src = {sid for b in result.blocks for sid in b.source_ids}
    assert src == {"u1"}
    assert all(b.kind == "episodic" for b in result.blocks)


def test_empty_recall_has_trace(recall: Recall) -> None:
    result = recall.recall("hiçbirşeyeşlemeyenkelime")
    assert result.blocks == []
    assert bool(result) is False
    assert result.trace.returned == 0
