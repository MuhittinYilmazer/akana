"""Curator: stage -> promote/reject, with contradiction supersede.

Candidates are staged directly via ``memory.staging.stage(FactCandidate(...))``
(the production path — see ``akana_server/api/routes/chat/persist.py``), not
through a ``Curator.capture``/extractor seam (removed: production never called
that path).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from akana.memory import FactCandidate, Memory, MemoryEvent
from akana.memory.curator import Curator


@pytest.fixture()
def mem(tmp_path: Path) -> Memory:
    return Memory.for_data_dir(tmp_path)


@pytest.fixture()
def curator(mem: Memory) -> Curator:
    return mem.make_curator()


def _stage(mem: Memory, key: str, value: str, *, turn_id: str | None = None) -> "object":
    return mem.staging.stage(
        FactCandidate(key=key, value=value, trust="user_statement", source_turn_id=turn_id)
    )


def test_stage_and_inbox(mem: Memory, curator: Curator) -> None:
    staged = _stage(mem, "ad", "Alice", turn_id="t1")
    assert mem.staging.count_pending() == 1
    assert mem.staging.list_pending()[0].value == "Alice"
    assert staged.key == "ad"


def test_promote_writes_durable_fact(mem: Memory, curator: Curator) -> None:
    staged = _stage(mem, "ad", "Alice", turn_id="t1")
    fact = curator.promote(staged.id)
    assert fact is not None
    assert fact.key == "ad"
    assert fact.value == "Alice"
    assert fact.trust == "user_statement"
    assert fact.source_turn_id == "t1"  # evidence carried through
    # now durable + searchable, and the inbox is clear
    assert {f.value for f in mem.list_facts()} == {"Alice"}
    assert mem.staging.count_pending() == 0


def test_promote_contradiction_lands_new_value_atomically(
    mem: Memory, curator: Curator
) -> None:
    """audit C0/C2/C7: promoting a value that contradicts an existing fact invalidates
    the old and lands the new in ONE atomic assert_fact transaction. The value can never
    be dropped on contention (the old b20 retry / mark_rejected-on-None fallback is gone),
    and exactly one value stays valid under the key. (Was a retry test against a mocked
    supersede_fact, which promote no longer calls — the write is now atomic.)"""
    curator.promote(_stage(mem, "ad", "Alice", turn_id="t1").id)
    staged = _stage(mem, "ad", "Bob", turn_id="t2")  # contradicts Alice

    fact = curator.promote(staged.id)
    assert fact is not None, "user-approved value must never be dropped"
    assert fact.value == "Bob"
    # Exactly one valid row under the key, and it is the new (last-writer) value.
    assert [f.value for f in mem._semantic.facts_for_key("ad")] == ["Bob"]
    assert {f.value for f in mem.list_facts()} == {"Bob"}


def test_promote_supersedes_contradiction(mem: Memory, curator: Curator) -> None:
    first = _stage(mem, "ad", "Alice")
    curator.promote(first.id)

    second = _stage(mem, "ad", "Mehmet")
    new_fact = curator.promote(second.id)
    assert new_fact is not None and new_fact.value == "Mehmet"

    # only the new value is valid; the old one is invalidated but preserved
    assert {f.value for f in mem.list_facts()} == {"Mehmet"}
    all_ad = mem.semantic.facts_for_key("ad", include_invalidated=True)
    assert {f.value for f in all_ad} == {"Alice", "Mehmet"}
    assert any(not f.is_valid for f in all_ad)


def test_promote_same_value_is_not_a_contradiction(mem: Memory, curator: Curator) -> None:
    """Contradiction detection now lives in ``SemanticStore.assert_fact`` (driven by
    ``promote``): a differing value supersedes the old row, but re-promoting the SAME
    value must not open a second valid row under the key."""
    curator.promote(_stage(mem, "ad", "Alice").id)

    # Same value again → no contradiction, still exactly one valid row.
    curator.promote(_stage(mem, "ad", "Alice").id)
    assert [f.value for f in mem.semantic.facts_for_key("ad")] == ["Alice"]

    # Differing value → the old row is superseded, the new value wins.
    curator.promote(_stage(mem, "ad", "Mehmet").id)
    assert [f.value for f in mem.semantic.facts_for_key("ad")] == ["Mehmet"]


def test_reject_drops_candidate(mem: Memory, curator: Curator) -> None:
    staged = _stage(mem, "ad", "Alice")
    assert curator.reject(staged.id) is True
    assert mem.staging.count_pending() == 0
    assert mem.list_facts() == []
    # rejected can't be promoted
    assert curator.promote(staged.id) is None


def test_promote_unknown_returns_none(curator: Curator) -> None:
    assert curator.promote("does-not-exist") is None


def test_promote_emits_event(mem: Memory) -> None:
    events: list[MemoryEvent] = []
    mem.subscribe(events.append)
    curator = mem.make_curator()
    staged = _stage(mem, "ad", "Alice")
    curator.promote(staged.id)
    assert [e.kind for e in events] == ["fact"]
    assert events[0].data["key"] == "ad"


def test_promote_then_recall(mem: Memory, curator: Curator) -> None:
    # end-to-end: stage a fact, promote it, and recall surfaces it
    staged = _stage(mem, "ad", "Alice")
    curator.promote(staged.id)
    result = mem.recall("adım ne")
    assert any("Alice" in b.text for b in result.blocks)


def test_promote_contradiction_emits_fact_invalidated(mem: Memory) -> None:
    """A contradiction supersede emits fact_invalidated to the event seam.

    The curator writes to the store directly; without this event the vector index
    kept the old embedding, the graph kept the old node, and the ledger never saw
    the invalidation.
    """
    _closed, old = mem.assert_fact_direct(key="tema", value="koyu", trust="user_statement")
    staged = mem.staging.stage(
        FactCandidate(key="tema", value="açık", trust="user_statement")
    )

    events: list[MemoryEvent] = []
    mem.subscribe(events.append)
    fact = mem.make_curator().promote(staged.id)

    assert fact is not None and fact.value == "açık"
    inv = [e for e in events if e.kind == "fact_invalidated"]
    assert len(inv) == 1
    assert inv[0].data["fact_id"] == old.id
    assert inv[0].data["superseded_by"] == fact.id
    assert inv[0].data["key"] == "tema"
    assert inv[0].data["value"] == "koyu"
