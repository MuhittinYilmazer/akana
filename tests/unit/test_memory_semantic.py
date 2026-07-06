"""SemanticStore: upsert/dedup, trust filtering, temporal validity, search."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from akana.memory.semantic import (
    LEGACY_ORIGIN,
    SOURCE_ORIGINS,
    TRUST_RANK,
    SemanticStore,
    trust_rank,
)


@pytest.fixture()
def store(tmp_path: Path) -> SemanticStore:
    return SemanticStore(tmp_path / "memory.db")


def test_for_data_dir_shares_unified_db(tmp_path: Path) -> None:
    SemanticStore.for_data_dir(tmp_path)
    assert (tmp_path / "db" / "memory.db").exists()


def test_upsert_roundtrip_with_evidence(store: SemanticStore) -> None:
    fact = store.upsert_fact(
        fact_id="f1",
        key="favori_içecek",
        value="kahve",
        trust="user_statement",
        source_turn_id="t42",
        quote="kahve içmeyi severim",
        extractor="curator.v1",
    )
    assert fact.id == "f1"
    assert fact.value == "kahve"
    assert fact.trust == "user_statement"
    assert fact.source_turn_id == "t42"
    assert fact.quote == "kahve içmeyi severim"
    assert fact.is_valid is True
    assert fact.valid_from is not None

    got = store.get_fact("f1")
    assert got is not None
    assert got.extractor == "curator.v1"


def test_default_trust_is_inferred(store: SemanticStore) -> None:
    fact = store.upsert_fact(fact_id="f1", key="k", value="v")
    assert fact.trust == "inferred"


def test_upsert_dedups_on_key_value(store: SemanticStore) -> None:
    store.upsert_fact(fact_id="a", key="şehir", value="Ankara", confidence=0.8)
    again = store.upsert_fact(fact_id="b", key="şehir", value="Ankara", confidence=0.95, importance=0.5)

    facts = store.list_all_facts()
    assert len(facts) == 1
    assert again.id == "a"  # matched the existing row, kept its id
    assert facts[0].confidence == pytest.approx(0.95)
    assert facts[0].importance == pytest.approx(0.5)


def test_search_matches_key_or_value(store: SemanticStore) -> None:
    store.upsert_fact(fact_id="f1", key="favori_renk", value="mavi")
    store.upsert_fact(fact_id="f2", key="hobi", value="fotoğrafçılık")

    assert {f.id for f in store.search("mavi")} == {"f1"}
    assert {f.id for f in store.search("favori")} == {"f1"}
    assert {f.id for f in store.search("fotoğraf")} == {"f2"}


def test_search_honors_requested_limit_up_to_500(store: SemanticStore) -> None:
    """Route promises up to 500 (list_facts q=); store must not apply the old 50
    clamp — the requested limit (cap 500) is actually honored."""
    for i in range(60):
        store.upsert_fact(
            fact_id=f"lim{i:03d}", key=f"deneme konusu {i}", value=f"limit deneme kaydı {i}"
        )
    assert len(store.search("limit deneme", limit=500)) == 60
    assert len(store.search("limit deneme", limit=10)) == 10
    # The cap stays at 500: a larger request is clamped to 500 (no crash).
    assert len(store.search("limit deneme", limit=10_000)) == 60


def test_search_empty_query_returns_empty(store: SemanticStore) -> None:
    store.upsert_fact(fact_id="f1", key="k", value="v")
    assert store.search("") == []
    assert store.search(" ") == []  # single char, below min term length


def test_min_trust_filters_below_floor(store: SemanticStore) -> None:
    store.upsert_fact(fact_id="u", key="renk", value="mavi der", trust="user_statement")
    store.upsert_fact(fact_id="i", key="renk", value="mavi olabilir", trust="inferred")
    store.upsert_fact(fact_id="t", key="renk", value="mavi tool", trust="tool_output")
    store.upsert_fact(fact_id="s", key="renk", value="mavi sentez", trust="synthesis")

    assert {f.id for f in store.search("mavi")} == {"u", "i", "t", "s"}
    # min_trust=inferred admits user_statement + inferred only (P6)
    assert {f.id for f in store.search("mavi", min_trust="inferred")} == {"u", "i"}
    assert {f.id for f in store.search("mavi", min_trust="user_statement")} == {"u"}


def test_list_facts_respects_min_trust(store: SemanticStore) -> None:
    store.upsert_fact(fact_id="u", key="a", value="1", trust="user_statement")
    store.upsert_fact(fact_id="s", key="b", value="2", trust="synthesis")
    ids = {f.id for f in store.list_facts(min_trust="inferred")}
    assert ids == {"u"}


def test_list_facts_offset_paginates_disjoint(store: SemanticStore) -> None:
    """offset walks disjoint, contiguous pages; past-the-end is an empty page."""
    for i in range(5):
        store.upsert_fact(fact_id=f"p{i}", key=f"k{i}", value=f"v{i}")

    seen: list[str] = []
    for off in (0, 2, 4):
        seen.extend(f.id for f in store.list_facts(limit=2, offset=off))
    assert len(seen) == 5
    assert len(set(seen)) == 5  # no row appears on two pages

    assert store.list_facts(limit=2, offset=99) == []  # no wrap, no crash


def test_count_facts_mirrors_list_facts_filters(store: SemanticStore) -> None:
    store.upsert_fact(fact_id="u", key="a", value="1", trust="user_statement")
    store.upsert_fact(fact_id="s", key="b", value="2", trust="synthesis")
    assert store.count_facts() == 2
    assert store.count_facts(min_trust="user_statement") == 1  # synthesis below floor

    store.invalidate_fact("u")
    assert store.count_facts() == 1  # default read hides invalidated
    assert store.count_facts(include_invalidated=True) == 2


def test_invalidate_excludes_from_default_reads(store: SemanticStore) -> None:
    store.upsert_fact(fact_id="f1", key="şehir", value="Ankara")
    closed = store.invalidate_fact("f1")
    assert closed is not None
    assert closed.is_valid is False
    assert closed.invalidated_at is not None

    assert store.search("Ankara") == []
    assert store.list_facts() == []
    # explicitly opt in to see the history
    assert {f.id for f in store.search("Ankara", include_invalidated=True)} == {"f1"}
    assert store.get_fact("f1") is not None  # row preserved (replay)


def test_invalidate_missing_or_twice_is_noop(store: SemanticStore) -> None:
    assert store.invalidate_fact("nope") is None
    store.upsert_fact(fact_id="f1", key="k", value="v")
    assert store.invalidate_fact("f1") is not None
    assert store.invalidate_fact("f1") is None  # already closed


def test_supersede_invalidates_old_and_inserts_new(store: SemanticStore) -> None:
    store.upsert_fact(fact_id="f1", key="şehir", value="Ankara", importance=0.9)
    result = store.supersede_fact("f1", new_value="İstanbul", source_turn_id="t99")
    assert result is not None
    old, new = result

    assert old.id == "f1"
    assert old.is_valid is False
    assert new.value == "İstanbul"
    assert new.key == "şehir"  # key carried over
    assert new.importance == pytest.approx(0.9)  # metadata carried over
    assert new.is_valid is True
    assert new.source_turn_id == "t99"

    assert {f.value for f in store.list_facts()} == {"İstanbul"}
    assert {f.id for f in store.search("Ankara", include_invalidated=True)} == {"f1"}


def test_supersede_missing_returns_none(store: SemanticStore) -> None:
    assert store.supersede_fact("nope", new_value="x") is None


def test_supersede_dedup_onto_other_fact_keeps_its_earlier_valid_from(
    store: SemanticStore,
) -> None:
    """Report #5: a supersede whose replacement dedups onto a DIFFERENT
    pre-existing valid fact must NOT rewrite that fact's valid_from forward — that
    erases the temporal coverage the row already had (facts_as_of returns nothing
    for the period it was genuinely valid). min(existing_vf, supersede_instant)
    tiles equally without moving valid_from forward.
    """
    # Fact A: city = Istanbul, genuinely valid since T0 (an early explicit window).
    t0 = "2026-01-01T00:00:00.000Z"
    store.upsert_fact(
        fact_id="A", key="city", value="Istanbul", valid_from=t0
    )
    # Fact B lives under a different key; superseding it into (city, Istanbul)
    # dedups onto A (cross-key path reachable via memory.remember / Studio edit).
    store.upsert_fact(fact_id="B", key="country", value="Turkey")

    result = store.supersede_fact("B", new_value="Istanbul", new_key="city")
    assert result is not None

    a = store.get_fact("A")
    assert a is not None and a.is_valid
    # A's valid_from must NOT have jumped forward to the supersede instant.
    assert a.valid_from == t0

    # A time-travel query for the period A was genuinely valid still returns A.
    mid = "2026-03-01T00:00:00.000Z"
    as_of = store.facts_as_of("Istanbul", as_of=mid)
    assert any(f.id == "A" and f.value == "Istanbul" for f in as_of)


def test_correct_fact_in_place(store: SemanticStore) -> None:
    store.upsert_fact(fact_id="f1", key="isim", value="Muhittn")  # typo
    fixed = store.correct_fact("f1", new_value="Alice")
    assert fixed is not None
    assert fixed.value == "Alice"
    assert fixed.is_valid is True  # in-place fix, not a temporal supersede
    assert len(store.list_all_facts()) == 1


def test_delete_and_clear(store: SemanticStore) -> None:
    store.upsert_fact(fact_id="f1", key="k1", value="v1")
    store.upsert_fact(fact_id="f2", key="k2", value="v2")
    assert store.delete_fact("f1") is True
    assert store.delete_fact("f1") is False
    assert store.get_fact("f1") is None
    assert store.clear_all() == 1
    assert store.list_all_facts() == []


def test_recall_is_always_global(store: SemanticStore) -> None:
    # The split/partition concept was removed: different values under the same key
    # are no longer filtered by ANY scope — recall is always global.
    store.upsert_fact(fact_id="w", key="proje", value="akana")
    store.upsert_fact(fact_id="h", key="proje", value="bahçe")
    assert {f.id for f in store.search("proje")} == {"w", "h"}
    assert {f.id for f in store.list_facts()} == {"w", "h"}


# -- trust ladder (single canonical definition) ---------------------------------


def test_trust_rank_is_comparable_single_definition() -> None:
    assert (
        trust_rank("user_statement")
        > trust_rank("inferred")
        > trust_rank("tool_output")
        > trust_rank("synthesis")
    )
    # Unknown/None is below everything — the comparison never crashes.
    assert trust_rank(None) < trust_rank("synthesis")
    assert trust_rank("bogus") == trust_rank(None) == -1
    # Origin enum is the ladder values + the legacy migration default.
    assert set(SOURCE_ORIGINS) == set(TRUST_RANK) | {LEGACY_ORIGIN}


# -- provenance: source is mandatory, migration falls back to legacy -------------


def test_new_fact_always_carries_derived_source(store: SemanticStore) -> None:
    fact = store.upsert_fact(
        fact_id="f1",
        key="şehir",
        value="İzmir",
        trust="user_statement",
        extractor="curator.v1",
    )
    assert fact.source_origin == "user_statement"  # origin derives from trust
    assert fact.source_detail == "curator.v1"  # detail falls back to extractor
    assert fact.observed_at is not None
    assert fact.source == {
        "origin": "user_statement",
        "detail": "curator.v1",
        "observed_at": fact.observed_at,
    }
    # Re-reading returns the same source (columns are persistent).
    got = store.get_fact("f1")
    assert got is not None and got.source == fact.source


def test_source_detail_falls_back_to_evidence_turn(store: SemanticStore) -> None:
    fact = store.upsert_fact(
        fact_id="f1", key="k", value="v", source_turn_id="t42"
    )
    assert fact.source_origin == "inferred"  # default trust → origin
    assert fact.source_detail == "turn:t42"


def test_explicit_source_wins_and_invalid_origin_rejected(store: SemanticStore) -> None:
    fact = store.upsert_fact(
        fact_id="f1",
        key="k",
        value="v",
        trust="inferred",
        source_origin="tool_output",
        source_detail="https://example.com/doc",
        observed_at="2026-06-01T00:00:00.000Z",
    )
    assert fact.source == {
        "origin": "tool_output",
        "detail": "https://example.com/doc",
        "observed_at": "2026-06-01T00:00:00.000Z",
    }
    with pytest.raises(ValueError, match="source_origin"):
        store.upsert_fact(fact_id="f2", key="k2", value="v2", source_origin="bogus")


def test_supersede_carries_fresh_source(store: SemanticStore) -> None:
    store.upsert_fact(fact_id="f1", key="şehir", value="Ankara", trust="user_statement")
    result = store.supersede_fact(
        "f1", new_value="İstanbul", source_detail="conv-99"
    )
    assert result is not None
    _, new = result
    assert new.source_origin == "user_statement"  # carried over from the old trust
    assert new.source_detail == "conv-99"
    assert new.observed_at is not None


def test_migration_backfills_legacy_origin(tmp_path: Path) -> None:
    """Opening a pre-source-columns DB makes old rows fall back to legacy."""
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE facts (
            id TEXT PRIMARY KEY, ts_first TEXT NOT NULL, ts_last TEXT NOT NULL,
            key TEXT NOT NULL, value TEXT NOT NULL, confidence REAL,
            importance REAL, anchored INTEGER DEFAULT 0, island TEXT,
            decay_rate REAL DEFAULT 0.01, trust TEXT NOT NULL DEFAULT 'inferred',
            source_turn_id TEXT, quote TEXT, extractor TEXT,
            valid_from TEXT, invalidated_at TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO facts (id, ts_first, ts_last, key, value, extractor) "
        "VALUES ('old1', '2025-01-01T00:00:00.000Z', '2025-01-01T00:00:00.000Z', "
        "'şehir', 'Ankara', 'curator.v1')"
    )
    conn.commit()
    conn.close()

    store = SemanticStore(db)  # the open-time migration adds columns + backfills
    fact = store.get_fact("old1")
    assert fact is not None
    assert fact.source_origin == LEGACY_ORIGIN
    assert fact.source_detail == "curator.v1"  # detail derived from extractor
    assert fact.observed_at == "2025-01-01T00:00:00.000Z"  # falls back to ts_first
    # Migration is idempotent: a second open changes nothing.
    again = SemanticStore(db).get_fact("old1")
    assert again is not None and again.source == fact.source


def test_invalid_trust_normalized_so_fact_stays_recallable(store: SemanticStore) -> None:
    """R2-B4: a fact written with an invalid ``trust`` MUST NOT vanish from recall.

    A trust outside ``TRUST_RANK`` (a typo, "explicit") entered no
    ``_trust_allowset``, so the fact was invisible in recall even at the loosest
    floor (silent loss). It is now normalized to a valid ladder value on write.
    """
    fact = store.upsert_fact(
        fact_id="f-bad-trust", key="favori_renk", value="mavi", trust="explicit"  # type: ignore[arg-type]
    )
    # Normalized to a valid trust on write.
    assert fact.trust in TRUST_RANK
    # Searchable even at the loosest floor (synthesis, rank 0) — used to return 0 results.
    found = store.search("mavi", min_trust="synthesis")
    assert any(f.value == "mavi" for f in found), "the normalized fact must appear in recall"


def test_valid_trust_preserved(store: SemanticStore) -> None:
    fact = store.upsert_fact(
        fact_id="f-good", key="sehir", value="izmir", trust="user_statement"
    )
    assert fact.trust == "user_statement"
