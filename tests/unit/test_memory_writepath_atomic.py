"""Group B — atomic write-path regression tests (audit C0/C1/C2/C4/C5/C6/C7/C8/C14).

The durable-write path (Curator.promote, mutate.remember(direct), _create_fact_sync)
now funnels through one atomic primitive, ``SemanticStore.assert_fact`` — find
contradictions + invalidate + upsert in a single BEGIN IMMEDIATE transaction, backed
by a partial UNIQUE index that makes 'one valid row per folded (key_norm,value_norm)'
a cross-process invariant. These tests pin the guarantees that fix used to break.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from akana.memory import FactCandidate, Memory
from akana.memory.curator import Curator
from akana.memory.semantic import SemanticStore
from akana.memory.terms import fold_text


@pytest.fixture()
def store(tmp_path: Path) -> SemanticStore:
    return SemanticStore(tmp_path / "memory.db")


@pytest.fixture()
def mem(tmp_path: Path) -> Memory:
    return Memory.for_data_dir(tmp_path)


# ── C8: timestamp canonicalization ────────────────────────────────────────────
def test_canon_ts_normalizes_offset_and_precision() -> None:
    canon = "2026-01-01T00:00:00.000Z"
    assert SemanticStore._canon_ts("2026-01-01T00:00:00+00:00") == canon
    assert SemanticStore._canon_ts("2026-01-01T00:00:00.000Z") == canon
    assert SemanticStore._canon_ts("2026-01-01T00:00:00Z") == canon
    # '+' (0x2B) sorts before 'Z' (0x5A); after canon they compare correctly.
    assert SemanticStore._canon_ts("2026-01-01T00:00:00+00:00") < "2026-06-01T00:00:00.000Z"


# ── C6: a future valid_from is clamped so the fact stays visible now ───────────
def test_upsert_clamps_future_valid_from(store: SemanticStore) -> None:
    fact = store.upsert_fact(
        fact_id="f-future", key="sehir", value="izmir",
        valid_from="2999-01-01T00:00:00.000Z",
    )
    # The window can't start in the future (that would hide it from facts_as_of(now)).
    assert fact.valid_from <= store._iso_now()


# ── C7: supersede onto a pre-existing valid row still tiles gaplessly ──────────
def test_supersede_onto_preexisting_row_tiles_gaplessly(store: SemanticStore) -> None:
    # Two distinct valid values under one key (only possible via raw upsert — assert_fact
    # would collapse them): ankara + izmir.
    store.upsert_fact(fact_id="r-ank", key="sehir", value="ankara")
    store.upsert_fact(fact_id="r-izm", key="sehir", value="izmir")
    # Supersede ankara INTO izmir; the replacement dedups onto the pre-existing izmir row.
    result = store.supersede_fact("r-ank", new_value="izmir")
    assert result is not None
    old, new = result
    assert old.id == "r-ank" and old.invalidated_at is not None
    # The surviving izmir row must carry the supersede instant (not its stale original
    # valid_from) so [old.valid_from, old.invalidated_at) tiles with [new.valid_from, ...).
    assert new.value == "izmir"
    assert new.valid_from == old.invalidated_at
    # Exactly one valid value remains under the key.
    assert [f.value for f in store.facts_for_key("sehir")] == ["izmir"]


# ── C5: correct_fact refuses a collision instead of duplicating ───────────────
def test_correct_fact_refuses_collision(store: SemanticStore) -> None:
    store.upsert_fact(fact_id="c-a", key="sehir", value="ankara")
    store.upsert_fact(fact_id="c-b", key="sehir", value="izmir")
    # Correcting ankara -> izmir would create two valid rows sharing folded key+value.
    assert store.correct_fact("c-a", new_value="izmir") is None
    # Nothing changed; both stay valid with their own values.
    assert store.get_fact("c-a").value == "ankara"
    assert store.get_fact("c-b").value == "izmir"
    # A non-colliding correction still works.
    fixed = store.correct_fact("c-a", new_value="Ankara ili")
    assert fixed is not None and fixed.value == "Ankara ili"


# ── assert_fact core semantics: plain / dedup / contradiction ─────────────────
def test_assert_fact_plain_dedup_and_contradiction(store: SemanticStore) -> None:
    closed, new = store.assert_fact(key="sehir", value="ankara")
    assert closed == [] and new.value == "ankara"

    closed, again = store.assert_fact(key="sehir", value="ankara")  # dedup
    assert closed == [] and again.id == new.id
    assert len(store.facts_for_key("sehir")) == 1

    closed, izmir = store.assert_fact(key="sehir", value="izmir")  # contradiction
    assert [c.value for c in closed] == ["ankara"]
    assert izmir.value == "izmir"
    assert [f.value for f in store.facts_for_key("sehir")] == ["izmir"]  # one valid


# ── C0/C4: two stores (two processes) converge to one valid row ───────────────
def _join_all(threads: list[threading.Thread], timeout: float = 20.0) -> None:
    for t in threads:
        t.join(timeout=timeout)
    assert all(not t.is_alive() for t in threads), "a writer hung — possible deadlock"


def test_assert_fact_concurrent_same_folded_value_one_row(tmp_path: Path) -> None:
    """Two SemanticStore instances on one memory.db (the server + MCP subprocess case)
    asserting the SAME folded value ('izmir' vs 'İzmir') must leave exactly one valid
    row — the BEGIN IMMEDIATE + partial UNIQUE index prevent the two-process double-insert."""
    path = tmp_path / "memory.db"
    s1, s2 = SemanticStore(path), SemanticStore(path)
    errors: list[str] = []

    def worker(store: SemanticStore, val: str) -> None:
        try:
            for _ in range(12):
                store.assert_fact(key="sehir", value=val)
        except Exception as e:  # noqa: BLE001 - surface any escape
            errors.append(repr(e))

    threads = [
        threading.Thread(target=worker, args=(s1 if i % 2 == 0 else s2, "izmir" if i % 2 == 0 else "İzmir"))
        for i in range(8)
    ]
    for t in threads:
        t.start()
    _join_all(threads)
    assert errors == [], errors
    assert fold_text("İzmir") == fold_text("izmir")  # precondition: same folded key
    assert len(s1.facts_for_key("sehir")) == 1


def test_assert_fact_concurrent_contradiction_keeps_one_valid(tmp_path: Path) -> None:
    """Concurrent asserts of DIFFERENT values under one key, across two stores, converge
    to the invariant 'at most one valid value per key' with no errors/deadlock (C0/C14)."""
    path = tmp_path / "memory.db"
    s1, s2 = SemanticStore(path), SemanticStore(path)
    errors: list[str] = []

    def worker(store: SemanticStore, val: str) -> None:
        try:
            for _ in range(10):
                store.assert_fact(key="sehir", value=val, supersede=True)
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [
        threading.Thread(target=worker, args=(s1 if i % 2 == 0 else s2, "ankara" if i % 2 == 0 else "izmir"))
        for i in range(6)
    ]
    for t in threads:
        t.start()
    _join_all(threads)
    assert errors == [], errors
    assert len(s1.facts_for_key("sehir")) == 1  # never two conflicting valid values


# ── Migration: legacy valid duplicates are collapsed on open ──────────────────
def test_migration_dedups_legacy_valid_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    SemanticStore(path)  # creates schema + the unique-valid index
    kn, vn = fold_text("sehir"), fold_text("izmir")
    conn = sqlite3.connect(path)
    conn.execute("DROP INDEX IF EXISTS idx_facts_valid_uniq")  # simulate a pre-index legacy DB
    for fid, ts in (("dup-old", "2020-01-01T00:00:00.000Z"), ("dup-new", "2026-01-01T00:00:00.000Z")):
        conn.execute(
            "INSERT INTO facts (id, ts_first, ts_last, key, value, trust, valid_from, "
            "invalidated_at, key_norm, value_norm, confidence, importance) "
            "VALUES (?,?,?,?,?,?,?,NULL,?,?,?,?)",
            (fid, ts, ts, "sehir", "izmir", "user_statement", ts, kn, vn, 0.9, 0.7),
        )
    conn.commit()
    conn.close()

    store = SemanticStore(path)  # reopen → migration dedups + recreates the index
    assert [f.id for f in store.facts_for_key("sehir")] == ["dup-new"]  # newest survives
    older = store.get_fact("dup-old")
    assert older is not None and not older.is_valid  # older invalidated, history kept

    # The index is back: a third valid duplicate now fails loudly.
    conn = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO facts (id, ts_first, ts_last, key, value, trust, key_norm, value_norm) "
                "VALUES ('dup-3', ?, ?, 'sehir', 'izmir', 'inferred', ?, ?)",
                ("2026-02-01T00:00:00.000Z", "2026-02-01T00:00:00.000Z", kn, vn),
            )
            conn.commit()
    finally:
        conn.close()


# ── C0/C1: Curator.promote is a single-winner claim-first election ────────────
def _counting_curator(mem: Memory) -> tuple[Curator, list[str], list[tuple]]:
    promotes: list[str] = []
    invalidates: list[tuple] = []
    cur = Curator(
        mem._semantic,
        mem.staging,
        on_promote=lambda f: promotes.append(f.id),
        on_invalidate=lambda old, by: invalidates.append((old.id, by)),
    )
    return cur, promotes, invalidates


def test_promote_double_promote_single_winner(mem: Memory) -> None:
    cur, promotes, _ = _counting_curator(mem)
    staged = mem.staging.stage(FactCandidate(key="ad", value="Alice", trust="user_statement"))
    results: list[object] = []

    def go() -> None:
        results.append(cur.promote(staged.id))

    threads = [threading.Thread(target=go) for _ in range(2)]
    for t in threads:
        t.start()
    _join_all(threads)

    assert len([r for r in results if r is not None]) == 1  # exactly one winner
    assert len(promotes) == 1  # on_promote fired exactly once (no stray embedding)
    assert len(mem.list_facts()) == 1  # exactly one durable fact


def test_promote_lost_claim_writes_and_emits_nothing(
    mem: Memory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: if the staging claim is lost (a concurrent reject won), promote must write NO
    durable fact and emit NOTHING — no committed-but-unannounced trust-ladder divergence."""
    cur, promotes, invalidates = _counting_curator(mem)
    staged = mem.staging.stage(FactCandidate(key="ad", value="Alice", trust="user_statement"))
    monkeypatch.setattr(mem.staging, "mark_promoted", lambda *a, **k: False)

    assert cur.promote(staged.id) is None
    assert promotes == [] and invalidates == []
    assert mem.list_facts() == []  # claim-first → no orphan durable write


def test_promote_dedup_repoints_promoted_fact_id(mem: Memory) -> None:
    """Group B review: when the promoted value already exists as a valid fact, assert_fact
    dedups onto it and returns its (different) id — the staging row's promoted_fact_id must
    be re-pointed to that real durable id, never left dangling on the minted claim id."""
    cur = mem.make_curator()
    first = mem.staging.stage(FactCandidate(key="ad", value="Alice", trust="user_statement"))
    existing = cur.promote(first.id)
    assert existing is not None
    # Stage + promote a DUPLICATE candidate (same key+value) — assert_fact will dedup.
    dup = mem.staging.stage(FactCandidate(key="ad", value="Alice", trust="user_statement"))
    fact = cur.promote(dup.id)

    assert fact is not None and fact.id == existing.id  # deduped onto the existing fact
    staged_row = mem.staging.get(dup.id)
    assert staged_row is not None and staged_row.status == "promoted"
    assert staged_row.promoted_fact_id == fact.id  # re-pointed, not the minted claim id
    assert mem.get_fact(staged_row.promoted_fact_id) is not None  # resolves (no dangling ref)
    assert len(mem.list_facts()) == 1  # still exactly one durable fact


# ── C14: direct-write contradiction resolves atomically (façade seam) ─────────
def test_assert_fact_direct_contradiction_keeps_one_valid(mem: Memory) -> None:
    closed, first = mem.assert_fact_direct(key="sehir", value="ankara", trust="user_statement")
    assert closed == [] and first.value == "ankara"
    closed, second = mem.assert_fact_direct(key="sehir", value="izmir", trust="user_statement")
    assert [c.value for c in closed] == ["ankara"]
    assert second.value == "izmir"
    assert [f.value for f in mem._semantic.facts_for_key("sehir")] == ["izmir"]


# ── Liveness: mixed writers across two stores terminate + hold the invariant ──
def test_mixed_writers_no_deadlock(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    s1, s2 = SemanticStore(path), SemanticStore(path)
    errors: list[str] = []

    def asserter(store: SemanticStore, val: str) -> None:
        try:
            for _ in range(8):
                store.assert_fact(key="k", value=val, supersede=True)
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    def corrector(store: SemanticStore) -> None:
        try:
            for _ in range(8):
                valid = store.facts_for_key("k")
                if valid:
                    store.correct_fact(valid[0].id, new_value=valid[0].value + "!")
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [
        threading.Thread(target=asserter, args=(s1, "ankara")),
        threading.Thread(target=asserter, args=(s2, "izmir")),
        threading.Thread(target=asserter, args=(s1, "bursa")),
        threading.Thread(target=corrector, args=(s2,)),
    ]
    for t in threads:
        t.start()
    _join_all(threads, timeout=30.0)
    assert errors == [], errors
    assert len(s1.facts_for_key("k")) <= 1  # invariant holds throughout
