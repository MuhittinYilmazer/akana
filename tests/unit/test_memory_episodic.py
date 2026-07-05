"""EpisodicStore: append, ordering, FTS5 keyword search + LIKE fallback.

Hermetic — every test builds a fresh store against a ``tmp_path`` SQLite file,
so there is no shared state and no dependency on the user's data dir.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import ulid

from akana.memory.episodic import EpisodicStore


@pytest.fixture()
def store(tmp_path: Path) -> EpisodicStore:
    return EpisodicStore(tmp_path / "memory.db")


def test_for_data_dir_uses_unified_db(tmp_path: Path) -> None:
    s = EpisodicStore.for_data_dir(tmp_path)
    # K11: episodic + semantic share one file under <data_dir>/db/memory.db.
    assert (tmp_path / "db" / "memory.db").exists()
    s.append_turn(turn_id="t1", conversation_id="c", role="user", text="hi")
    assert len(s.list_conversation_recent("c")) == 1


def test_append_turn_roundtrip(store: EpisodicStore) -> None:
    turn = store.append_turn(
        turn_id="t1",
        conversation_id="c1",
        role="user",
        text="merhaba",
        lang="tr",
    )
    assert turn.id == "t1"
    assert turn.role == "user"
    assert turn.text == "merhaba"
    assert turn.lang == "tr"

    rows = store.list_conversation_recent("c1")
    assert [r.id for r in rows] == ["t1"]
    assert rows[0].text == "merhaba"


def test_list_conversation_recent_orders_by_ts_and_scopes(store: EpisodicStore) -> None:
    store.append_turn(turn_id="b", conversation_id="c1", role="user", text="2", ts="2026-01-01T00:00:02Z")
    store.append_turn(turn_id="a", conversation_id="c1", role="user", text="1", ts="2026-01-01T00:00:01Z")
    store.append_turn(turn_id="c", conversation_id="c1", role="user", text="3", ts="2026-01-01T00:00:03Z")
    store.append_turn(turn_id="x", conversation_id="other", role="user", text="nope")

    rows = store.list_conversation_recent("c1")
    assert [r.id for r in rows] == ["a", "b", "c"]  # ts ASC, other convo excluded


def test_newest_turn_returns_latest_and_scopes(store: EpisodicStore) -> None:
    store.append_turn(turn_id="a", conversation_id="c1", role="user", text="1", ts="2026-01-01T00:00:01Z")
    store.append_turn(turn_id="c", conversation_id="c1", role="assistant", text="3", ts="2026-01-01T00:00:03Z")
    store.append_turn(turn_id="b", conversation_id="c1", role="user", text="2", ts="2026-01-01T00:00:02Z")
    store.append_turn(turn_id="x", conversation_id="other", role="user", text="nope", ts="2026-01-09T00:00:00Z")

    newest = store.newest_turn("c1")
    assert newest is not None
    assert (newest.id, newest.text) == ("c", "3")  # max ts within convo; other excluded
    # Unknown/empty conversation → None (no crash).
    assert store.newest_turn("does-not-exist") is None


def test_newest_turn_matches_list_conversation_last(store: EpisodicStore) -> None:
    # Cheap single-row path (sidebar preview) must agree with the old full-list
    # ``[-1]`` behaviour it replaced (B N+1 fix).
    for i in range(1, 6):
        store.append_turn(
            turn_id=f"t{i}", conversation_id="c1", role="user", text=str(i),
            ts=f"2026-01-01T00:00:0{i}Z",
        )
    newest = store.newest_turn("c1")
    assert newest is not None
    assert newest.id == store.list_conversation_recent("c1")[-1].id


def test_newest_turn_ts_tie_breaks_by_id_matches_list_last(store: EpisodicStore) -> None:
    # Same ms ts (concurrent writes): without a secondary id ordering, newest_turn
    # (ts DESC) and list_conversation_recent[-1] (ts ASC) could pick different rows.
    tie = "2026-01-01T00:00:05Z"
    store.append_turn(turn_id="01A", conversation_id="c1", role="user", text="a", ts=tie)
    store.append_turn(turn_id="01C", conversation_id="c1", role="assistant", text="c", ts=tie)
    store.append_turn(turn_id="01B", conversation_id="c1", role="user", text="b", ts=tie)

    newest = store.newest_turn("c1")
    assert newest is not None
    # list_conversation_recent (ts ASC, id ASC) → largest id last; newest_turn picks the same row.
    assert newest.id == store.list_conversation_recent("c1")[-1].id == "01C"


def test_rapid_appends_stay_in_creation_order(store: EpisodicStore) -> None:
    """ulid.new() is NOT monotonic within a millisecond, so a burst of same-ms turns would be
    tie-broken by (random) id and scramble. append_turn keeps AUTO timestamps strictly increasing
    per instance → creation order is always preserved (was an intermittent-ordering flake)."""
    for i in range(30):
        store.append_turn(
            turn_id=str(ulid.new()), conversation_id="c1", role="user", text=f"m{i}"
        )
    rows = store.list_conversation_recent("c1")
    assert [r.text for r in rows] == [f"m{i}" for i in range(30)]  # creation order preserved
    tss = [r.ts for r in rows]
    assert tss == sorted(tss)  # strictly non-decreasing
    assert len(set(tss)) == len(tss), "auto timestamps must be distinct (strictly increasing)"


def test_insert_or_replace_is_idempotent(store: EpisodicStore) -> None:
    store.append_turn(turn_id="t1", conversation_id="c1", role="user", text="first")
    store.append_turn(turn_id="t1", conversation_id="c1", role="user", text="second")
    rows = store.list_conversation_recent("c1")
    assert len(rows) == 1
    assert rows[0].text == "second"


def test_search_keyword_finds_turn(store: EpisodicStore) -> None:
    store.append_turn(turn_id="t1", conversation_id="c1", role="user", text="kahve sevdiğimi unutma")
    store.append_turn(turn_id="t2", conversation_id="c1", role="assistant", text="tamam, not aldım")

    hits = store.search_keyword("kahve")
    assert [h.id for h in hits] == ["t1"]


def test_search_keyword_handles_turkish_tokens(store: EpisodicStore) -> None:
    store.append_turn(
        turn_id="t1", conversation_id="c1", role="user", text="İstanbul'da yaşıyorum şu an"
    )
    assert {h.id for h in store.search_keyword("İstanbul")} == {"t1"}
    assert {h.id for h in store.search_keyword("yaşıyorum")} == {"t1"}


def test_search_keyword_scopes_by_conversation(store: EpisodicStore) -> None:
    store.append_turn(turn_id="t1", conversation_id="c1", role="user", text="python projesi")
    store.append_turn(turn_id="t2", conversation_id="c2", role="user", text="python projesi")

    scoped = store.search_keyword("python", conversation_id="c1")
    assert [h.id for h in scoped] == ["t1"]


def test_search_keyword_zero_fts_hits_skips_like_double_scan(store: EpisodicStore) -> None:
    # "disestab" is not a whole token: FTS5 legitimately finds nothing, and a
    # legitimate zero-hit must NOT trigger a second full LIKE table scan — the
    # LIKE fallback is reserved for FTS *errors* (sqlite3.OperationalError).
    store.append_turn(
        turn_id="t1", conversation_id="c1", role="user", text="antidisestablishmentarianism"
    )
    assert store.search_keyword("disestab") == []
    # Queries with no FTS-usable token (too short to tokenize) still use LIKE.
    assert [h.id for h in store.search_keyword("a")] == ["t1"]


def test_search_keyword_empty_query_returns_empty(store: EpisodicStore) -> None:
    store.append_turn(turn_id="t1", conversation_id="c1", role="user", text="x")
    assert store.search_keyword("") == []
    assert store.search_keyword("   ") == []


def test_list_conversation_ids_aggregates(store: EpisodicStore) -> None:
    store.append_turn(turn_id="t1", conversation_id="c1", role="user", text="a", ts="2026-01-01T00:00:01Z")
    store.append_turn(turn_id="t2", conversation_id="c1", role="user", text="b", ts="2026-01-01T00:00:02Z")
    store.append_turn(turn_id="t3", conversation_id="c2", role="user", text="c", ts="2026-01-02T00:00:00Z")

    convos = store.list_conversation_ids()
    by_id = {c["conversation_id"]: c for c in convos}
    assert by_id["c1"]["turn_count"] == 2
    assert by_id["c2"]["turn_count"] == 1
    # ordered by last activity desc -> c2 (later) first
    assert convos[0]["conversation_id"] == "c2"


def test_delete_conversation_returns_rowcount(store: EpisodicStore) -> None:
    store.append_turn(turn_id="t1", conversation_id="c1", role="user", text="a")
    store.append_turn(turn_id="t2", conversation_id="c1", role="user", text="b")
    store.append_turn(turn_id="t3", conversation_id="c2", role="user", text="c")

    assert store.delete_conversation("c1") == 2
    assert store.list_conversation_recent("c1") == []
    assert len(store.list_conversation_recent("c2")) == 1
    # deleted turns are dropped from the FTS index too
    assert store.search_keyword("a", conversation_id="c1") == []


def test_list_conversation_recent_returns_newest_not_oldest(store: EpisodicStore) -> None:
    """R2-B4: in a 1000+ turn conversation the NEWEST messages must be returned (not the old ts ASC LIMIT)."""
    for i in range(1100):
        store.append_turn(
            turn_id=f"t{i:05d}", conversation_id="c", role="user", text=f"m{i}",
            ts=f"2026-06-16T00:00:00.{i:06d}Z",
        )
    recent = store.list_conversation_recent("c", limit=10)
    assert [t.text for t in recent] == [f"m{i}" for i in range(1090, 1100)]


def test_list_conversation_recent_before_ts_paginates(store: EpisodicStore) -> None:
    """The window BEFORE that moment via the ``before_ts`` SQL predicate (chronological)."""
    for i in range(20):
        store.append_turn(
            turn_id=f"t{i:02d}", conversation_id="c", role="user", text=f"m{i}",
            ts=f"2026-06-16T00:00:00.{i:06d}Z",
        )
    mid = f"2026-06-16T00:00:00.{10:06d}Z"
    before = store.list_conversation_recent("c", limit=50, before_ts=mid)
    assert [t.text for t in before] == [f"m{i}" for i in range(0, 10)]
    assert all(t.ts < mid for t in before)


def test_keyset_pagination_no_loss_on_same_ts_boundary(store: EpisodicStore) -> None:
    """R4-C #2: even when same-ms ``ts`` turns are split at a page boundary by ``LIMIT``,
    the keyset cursor (``before_ts``+``before_id``) skips/repeats NONE of them. With
    ``before_ts`` (``ts<?``) alone the sibling at the boundary would drop (data-visibility loss)."""
    # b1 & b2 have the SAME ts; id order = creation order (ORDER BY ts DESC, id DESC).
    store.append_turn(turn_id="a1", conversation_id="c", role="user", text="1", ts="2026-01-01T00:00:01Z")
    store.append_turn(turn_id="b1", conversation_id="c", role="user", text="2", ts="2026-01-01T00:00:02Z")
    store.append_turn(turn_id="b2", conversation_id="c", role="user", text="3", ts="2026-01-01T00:00:02Z")
    store.append_turn(turn_id="c1", conversation_id="c", role="user", text="4", ts="2026-01-01T00:00:03Z")

    page1 = store.list_conversation_recent("c", limit=2)  # newest 2 (returned ASC)
    assert [t.id for t in page1] == ["b2", "c1"]

    oldest = page1[0]  # b2 (ts=...02, id=b2)
    page2 = store.list_conversation_recent(
        "c", limit=2, before_ts=oldest.ts, before_id=oldest.id
    )
    assert [t.id for t in page2] == ["a1", "b1"]  # b1 (same ts, id<b2) was NOT skipped
    # When the two pages are combined, ALL 4 turns are visible — no loss/repeat.
    assert sorted(t.id for t in page2) + sorted(t.id for t in page1) == ["a1", "b1", "b2", "c1"]

    # Counter-evidence: before_ts alone (no id tiebreaker) → the boundary b1 DROPS (old bug).
    ts_only = store.list_conversation_recent("c", limit=2, before_ts=oldest.ts)
    assert "b1" not in {t.id for t in ts_only}


# -- audit fixes (Group A) --------------------------------------------------------


def _trigger_names(path: Path) -> set[str]:
    import sqlite3

    conn = sqlite3.connect(path)
    try:
        return {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
    finally:
        conn.close()


def test_count_conversations_and_turns_are_uncapped(store: EpisodicStore) -> None:
    """audit C27: list_conversation_ids clamps to 200 for the dashboard list, so
    memory_stats froze at 200 conversations and dropped older turns. The dedicated
    count methods must return the true, uncapped totals."""
    for i in range(205):
        store.append_turn(turn_id=f"t{i}", conversation_id=f"c{i}", role="user", text="x")
    store.append_turn(turn_id="extra", conversation_id="c0", role="assistant", text="y")

    assert len(store.list_conversation_ids(limit=1000)) == 200  # still capped (list)
    assert store.count_conversations() == 205  # uncapped distinct conversations
    assert store.count_turns() == 206  # 205 + the extra reply


def test_ensure_fts_backfills_insert_delete_triggers_on_degraded_db(
    tmp_path: Path,
) -> None:
    """audit C21: a DB that has turns_fts but is missing the INSERT/DELETE sync
    triggers (the old code only backfilled UPDATE) must be fully repaired on open,
    so new appends index into FTS again and deletes don't orphan FTS rows."""
    import sqlite3

    path = tmp_path / "memory.db"
    EpisodicStore(path)  # creates schema + all three triggers
    conn = sqlite3.connect(path)
    conn.execute("DROP TRIGGER IF EXISTS turns_fts_ai")  # simulate a degraded schema
    conn.execute("DROP TRIGGER IF EXISTS turns_fts_ad")
    conn.commit()
    conn.close()

    store = EpisodicStore(path)  # _ensure_fts must recreate ai/ad (IF NOT EXISTS)
    assert {"turns_fts_ai", "turns_fts_ad", "turns_fts_au"} <= _trigger_names(path)

    # The INSERT trigger now works: a new append lands in the FTS shadow table.
    store.append_turn(turn_id="t1", conversation_id="c1", role="user", text="zürafa")
    conn = sqlite3.connect(path)
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM turns_fts WHERE turn_id = 't1'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert n == 1
