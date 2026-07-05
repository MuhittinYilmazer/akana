"""Memory façade: id minting, store wiring, and the event/ledger seam."""

from __future__ import annotations

from pathlib import Path

import pytest

from akana.memory import Memory, MemoryEvent


@pytest.fixture()
def mem(tmp_path: Path) -> Memory:
    return Memory.for_data_dir(tmp_path)


def test_for_data_dir_creates_single_db(tmp_path: Path) -> None:
    Memory.for_data_dir(tmp_path)
    # both stores live in one file (K11)
    assert (tmp_path / "db" / "memory.db").exists()
    assert not (tmp_path / "db" / "semantic.db").exists()


def test_remember_turns_mint_ids_and_order(mem: Memory) -> None:
    u = mem.remember_turn(role="user", conversation_id="c1", text="merhaba")
    a = mem.remember_turn(role="assistant", conversation_id="c1", text="selam")
    assert u.id and a.id and u.id != a.id  # auto-minted, distinct
    assert u.role == "user"
    assert a.role == "assistant"

    rows = mem.recent_turns("c1")
    assert [(r.role, r.text) for r in rows] == [("user", "merhaba"), ("assistant", "selam")]


def test_remember_turn_honors_explicit_id(mem: Memory) -> None:
    t = mem.remember_turn(conversation_id="c1", role="user", text="x", turn_id="fixed")
    assert t.id == "fixed"


def test_search_turns(mem: Memory) -> None:
    mem.remember_turn(role="user", conversation_id="c1", text="kahve sevdiğimi unutma")
    mem.remember_turn(role="user", conversation_id="c2", text="çay tercih ederim")
    assert {t.text for t in mem.search_turns("kahve")} == {"kahve sevdiğimi unutma"}
    assert mem.search_turns("kahve", conversation_id="c2") == []


def test_reset_conversation(mem: Memory) -> None:
    mem.remember_turn(role="user", conversation_id="c1", text="a")
    mem.remember_turn(role="user", conversation_id="c1", text="b")
    assert mem.reset_conversation("c1") == 2
    assert mem.recent_turns("c1") == []


def test_recent_turns_returns_newest_window_beyond_1000_cap(mem: Memory) -> None:
    """D1: recent_turns must be the NEWEST ``limit`` turns. The old impl (``list_conversation``:
    ``ts ASC LIMIT 1000`` then slice) cut off everything after turn 1000 → the newest messages
    never arrived and the session summarizer's anchor froze forever (silent memory loss past
    1000 turns). Regression: with 1002 turns, the most-recent turn must be present."""
    total = 1002
    for i in range(total):
        mem.remember_turn(role="user", conversation_id="c1", text=f"m{i}")
    rows = mem.recent_turns("c1", limit=3)
    texts = [r.text for r in rows]
    # newest 3, chronological — NOT the oldest-3-of-the-first-1000 the old slice returned.
    assert texts == ["m999", "m1000", "m1001"], texts


def test_assert_fact_defaults_and_search(mem: Memory) -> None:
    _closed, f = mem.assert_fact_direct(key="favori_renk", value="mavi")
    assert f.id  # minted
    assert f.trust == "inferred"  # K15 default
    assert {x.id for x in mem.semantic.search("mavi")} == {f.id}


def test_facts_min_trust(mem: Memory) -> None:
    mem.assert_fact_direct(key="a", value="1", trust="user_statement")
    mem.assert_fact_direct(key="b", value="2", trust="synthesis")
    ids = {f.key for f in mem.list_facts(min_trust="inferred")}
    assert ids == {"a"}


def test_supersede_fact_via_facade(mem: Memory) -> None:
    _closed, f = mem.assert_fact_direct(key="şehir", value="Ankara")
    result = mem.supersede_fact(f.id, new_value="İstanbul")
    assert result is not None
    old, new = result
    assert old.is_valid is False
    assert {x.value for x in mem.list_facts()} == {"İstanbul"}
    assert mem.get_fact(new.id) is not None


def test_forget_fact_soft_and_hard(mem: Memory) -> None:
    _closed, f1 = mem.assert_fact_direct(key="k1", value="v1")
    assert mem.forget_fact(f1.id) is True  # soft = invalidate
    assert mem.list_facts() == []
    assert mem.get_fact(f1.id) is not None  # row still there (replay-safe)

    _closed, f2 = mem.assert_fact_direct(key="k2", value="v2")
    assert mem.forget_fact(f2.id, hard=True) is True
    assert mem.get_fact(f2.id) is None  # gone


def test_event_seam_captures_mutations(mem: Memory) -> None:
    events: list[MemoryEvent] = []
    unsub = mem.subscribe(events.append)

    mem.remember_turn(role="user", conversation_id="c1", text="hi")
    _closed, f = mem.assert_fact_direct(key="k", value="v")
    mem.supersede_fact(f.id, new_value="v2")
    mem.reset_conversation("c1")

    kinds = [e.kind for e in events]
    assert kinds == ["turn", "fact", "fact_invalidated", "fact", "conversation_reset"]
    assert all(e.ts for e in events)
    assert events[0].data["conversation_id"] == "c1"

    unsub()
    mem.remember_turn(role="user", conversation_id="c1", text="after-unsub")
    assert len(events) == 5  # no new events after unsubscribe


def test_bad_subscriber_does_not_break_writes(mem: Memory) -> None:
    seen: list[MemoryEvent] = []

    def boom(_e: MemoryEvent) -> None:
        raise RuntimeError("subscriber blew up")

    mem.subscribe(boom)
    mem.subscribe(seen.append)

    # write still succeeds despite the raising subscriber
    turn = mem.remember_turn(role="user", conversation_id="c1", text="x")
    assert turn.id
    assert len(mem.recent_turns("c1")) == 1
    assert [e.kind for e in seen] == ["turn"]


def test_no_subscribers_is_a_noop(mem: Memory) -> None:
    # exercises the early-return path in _emit
    turn = mem.remember_turn(role="user", conversation_id="c1", text="x")
    assert turn.id
