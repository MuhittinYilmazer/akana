"""StagingStore: stage, list pending, promote/reject status transitions."""

from __future__ import annotations

from pathlib import Path

import pytest

from akana.memory.staging import FactCandidate, StagingStore


@pytest.fixture()
def store(tmp_path: Path) -> StagingStore:
    return StagingStore(tmp_path / "memory.db")


def _cand(key: str = "ad", value: str = "Alice", **kw: object) -> FactCandidate:
    return FactCandidate(key=key, value=value, **kw)  # type: ignore[arg-type]


def test_stage_returns_pending(store: StagingStore) -> None:
    s = store.stage(_cand(trust="user_statement", source_turn_id="t1"), conversation_id="c1")
    assert s.id
    assert s.status == "pending"
    assert s.key == "ad"
    assert s.value == "Alice"
    assert s.trust == "user_statement"
    assert s.conversation_id == "c1"
    assert store.count_pending() == 1


def test_list_pending_orders_oldest_first(store: StagingStore) -> None:
    a = store.stage(_cand(key="ka", value="A"))
    b = store.stage(_cand(key="kb", value="B"))  # different key → inbox dedup is not triggered
    pending = store.list_pending()
    assert [p.id for p in pending] == [a.id, b.id]


def test_inbox_dedup_is_turkish_fold_insensitive(store: StagingStore) -> None:
    """D6: inbox dedup must FOLD keys (Turkish-aware, same helper the fact store uses). 'İsim'
    and 'isim' are the same key → the newer candidate supersedes the older instead of both
    sitting side-by-side in the inbox (a user-facing duplicate)."""
    store.stage(_cand(key="İsim", value="Ali"))
    store.stage(_cand(key="isim", value="Veli"))
    pending = store.list_pending()
    assert len(pending) == 1, [p.key for p in pending]
    assert pending[0].value == "Veli"  # newest wins; the diacritic/case variant did not dupe


def test_mark_promoted_is_one_shot(store: StagingStore) -> None:
    s = store.stage(_cand())
    assert store.mark_promoted(s.id, "fact-123") is True
    got = store.get(s.id)
    assert got is not None
    assert got.status == "promoted"
    assert got.promoted_fact_id == "fact-123"
    # already resolved -> no second transition
    assert store.mark_promoted(s.id, "fact-999") is False
    assert store.count_pending() == 0


def test_mark_rejected(store: StagingStore) -> None:
    s = store.stage(_cand())
    assert store.mark_rejected(s.id) is True
    got = store.get(s.id)
    assert got is not None and got.status == "rejected"
    assert store.list_pending() == []


def test_list_all_filters_by_status(store: StagingStore) -> None:
    a = store.stage(_cand(value="A"))
    store.stage(_cand(value="B"))
    store.mark_rejected(a.id)
    assert {x.value for x in store.list_all(status="pending")} == {"B"}
    assert {x.value for x in store.list_all(status="rejected")} == {"A"}
    assert len(store.list_all()) == 2


def test_clear(store: StagingStore) -> None:
    store.stage(_cand(value="A"))
    store.stage(_cand(value="B"))
    assert store.clear() == 2
    assert store.count_pending() == 0


# -- stage(staged_id=...) does not overwrite a decision (idempotency) ----------------------------------


def test_stage_existing_decided_id_is_noop(
    store: StagingStore, caplog: pytest.LogCaptureFixture
) -> None:
    """A row already bound to a decision (promoted/rejected) MUST NOT be returned to
    pending via INSERT OR REPLACE: stage is a no-op + warning log; the existing row is returned as-is."""
    store.stage(_cand(value="A"), staged_id="fixed-1")
    assert store.mark_rejected("fixed-1") is True

    with caplog.at_level("WARNING", logger="akana.memory.staging"):
        again = store.stage(_cand(value="B"), staged_id="fixed-1")
    assert again.id == "fixed-1"
    assert again.status == "rejected"  # decision is preserved
    assert again.value == "A"  # content is not overwritten
    assert any("fixed-1" in r.message for r in caplog.records)

    row = store.get("fixed-1")
    assert row is not None and row.status == "rejected" and row.value == "A"
    assert store.count_pending() == 0


def test_stage_existing_promoted_id_keeps_promotion(store: StagingStore) -> None:
    store.stage(_cand(value="A"), staged_id="fixed-2")
    assert store.mark_promoted("fixed-2", "fact-1") is True
    again = store.stage(_cand(value="B"), staged_id="fixed-2")
    assert again.status == "promoted"
    assert again.promoted_fact_id == "fact-1"
    assert store.count_pending() == 0


def test_stage_existing_pending_id_refreshes(store: StagingStore) -> None:
    """For a row that is still pending, staging again refreshes it (existing contract)."""
    store.stage(_cand(value="A"), staged_id="fixed-3")
    again = store.stage(_cand(value="B"), staged_id="fixed-3")
    assert again.status == "pending" and again.value == "B"
    assert store.count_pending() == 1


def test_refresh_at_cap_does_not_reject_innocent_oldest(
    store: StagingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bonus: when capacity is full, refreshing an existing PENDING row does not add a net new
    row — flood protection MUST NOT reject the innocent oldest row."""
    import akana.memory.staging as staging_mod

    monkeypatch.setattr(staging_mod, "_MAX_PENDING", 3)
    a = store.stage(_cand(key="ka", value="A"), staged_id="cap-a")
    store.stage(_cand(key="kb", value="B"), staged_id="cap-b")
    store.stage(_cand(key="kc", value="C"), staged_id="cap-c")  # different keys
    assert store.count_pending() == 3

    refreshed = store.stage(_cand(key="kb", value="B2"), staged_id="cap-b")
    assert refreshed.status == "pending" and refreshed.value == "B2"
    assert store.count_pending() == 3  # a refresh is not net growth
    oldest = store.get(a.id)
    assert oldest is not None and oldest.status == "pending"  # no innocent victim


# -- inbox dedup (updating the same key supersedes the old pending) ----------------


def test_restage_same_key_supersedes_pending(store: StagingStore) -> None:
    """User bug: updating the same information (key) again without approving it left both
    old+new in the inbox. Now the newest candidate wins and the old one becomes 'rejected'."""
    old = store.stage(_cand(key="kedi_adi", value="Pamuk"))
    new = store.stage(_cand(key="kedi_adi", value="Boncuk"))
    assert [p.id for p in store.list_pending()] == [new.id]  # only the newest is pending
    superseded = store.get(old.id)
    assert superseded is not None and superseded.status == "rejected"
    assert new.value == "Boncuk"


def test_restage_different_key_keeps_both(store: StagingStore) -> None:
    """Different keys are separate facts — no dedup (different info/multi-value is preserved)."""
    a = store.stage(_cand(key="kedi_adi", value="Pamuk"))
    b = store.stage(_cand(key="kopek_adi", value="Karabaş"))
    assert {p.id for p in store.list_pending()} == {a.id, b.id}


def test_consolidation_candidate_not_deduped_by_capture(store: StagingStore) -> None:
    """A consolidation candidate (source_fact_ids) is unaffected by capture dedup —
    even if a capture arrives on the same key it is not rejected (it has its own idempotency)."""
    consol = store.stage(_cand(key="proje", value="birleşik", source_fact_ids=("f1", "f2")))
    capture = store.stage(_cand(key="proje", value="taze"))  # same key, but a capture
    ids = {p.id for p in store.list_pending()}
    assert consol.id in ids and capture.id in ids
    assert store.count_pending() == 2  # both pending — dedup was not applied


# -- audit fixes (Group A) --------------------------------------------------------


def test_prune_resolved_removes_old_decided_keeps_pending_and_recent(
    store: StagingStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """audit C34: promoted/rejected rows were only status-flipped, never deleted, so
    the table grew unbounded. The opportunistic ``stage()`` sweep deletes resolved rows
    past the retention window while leaving pending rows and recently-resolved rows
    intact — driven by production, not a public wrapper."""
    import sqlite3

    import akana.memory.staging as staging_mod

    monkeypatch.setattr(staging_mod, "_PRUNE_EVERY", 2)  # sweep on every 2nd insert

    pending = store.stage(_cand(key="k_pending", value="P"))  # stage #1
    promoted = store.stage(_cand(key="k_prom", value="A"))  # stage #2 → sweep (nothing old yet)
    store.mark_promoted(promoted.id, "fact-x")
    rejected = store.stage(_cand(key="k_rej", value="B"))  # stage #3
    store.mark_rejected(rejected.id)
    # A freshly-resolved row (ts = now) is inside the window → must survive the sweep.
    recent = store.stage(_cand(key="k_recent", value="C"))  # stage #4 → sweep
    store.mark_rejected(recent.id)

    # Age the two older resolved rows well past the retention window (ts is ms-Z ISO).
    conn = sqlite3.connect(tmp_path / "memory.db")
    conn.execute(
        "UPDATE staging SET ts = ? WHERE id IN (?, ?)",
        ("2000-01-01T00:00:00.000Z", promoted.id, rejected.id),
    )
    conn.commit()
    conn.close()

    # A further pair of inserts triggers a sweep that now finds the aged rows.
    store.stage(_cand(key="k_x", value="X"))  # stage #5
    store.stage(_cand(key="k_y", value="Y"))  # stage #6 → sweep fires

    assert store.get(promoted.id) is None  # aged resolved → pruned
    assert store.get(rejected.id) is None  # aged resolved → pruned
    assert store.get(pending.id) is not None  # pending is never pruned
    assert store.get(recent.id) is not None  # recently-resolved → inside window, kept


def test_stage_triggers_opportunistic_prune(
    store: StagingStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """audit C34: stage() opportunistically sweeps resolved rows every _PRUNE_EVERY
    inserts, so growth is bounded without a separate maintenance task."""
    import sqlite3

    import akana.memory.staging as staging_mod

    monkeypatch.setattr(staging_mod, "_PRUNE_EVERY", 3)  # sweep on every 3rd insert
    old = store.stage(_cand(key="k_old", value="X"))  # stage #1
    store.mark_rejected(old.id)
    conn = sqlite3.connect(tmp_path / "memory.db")
    conn.execute(
        "UPDATE staging SET ts = ? WHERE id = ?", ("2000-01-01T00:00:00.000Z", old.id)
    )
    conn.commit()
    conn.close()

    store.stage(_cand(key="k_a", value="A"))  # stage #2 → 2 % 3 != 0, no sweep
    assert store.get(old.id) is not None  # not swept yet
    store.stage(_cand(key="k_b", value="B"))  # stage #3 → 3 % 3 == 0, sweep fires
    assert store.get(old.id) is None  # old resolved row pruned by stage()
