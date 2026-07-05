"""Hardening regressions for the memory stores (multi-process + Turkish + crash).

Each test pins one proven edge-case bug:

* episodic ``INSERT OR REPLACE`` duplicated ``turns_fts`` rows (REPLACE's
  implicit delete bypasses the AFTER DELETE trigger) — old text haunted search;
* a legitimate zero-hit FTS result fell through to a second LIKE table scan;
* ``supersede_fact`` committed invalidate and insert separately — a crash in
  between lost the fact;
* SQLite LIKE only case-folds ASCII, so ``'İstanbul'`` never matched
  ``'%istanbul%'`` (fixed via ``key_norm``/``value_norm`` shadow columns);
* the staging inbox grew without bound (now capped at 500 pending);
* no ``busy_timeout``: a write-txn held by the second process (server vs MCP
  subprocess) raised a raw ``database is locked`` after ~5s.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from akana.memory.episodic import EpisodicStore
from akana.memory.graph import GraphStore
from akana.memory.semantic import SemanticStore
from akana.memory.staging import FactCandidate, StagingStore

# ---------------------------------------------------------------------------
# busy_timeout — every store's connections must wait, not explode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("store_cls", [EpisodicStore, SemanticStore, StagingStore, GraphStore])
def test_busy_timeout_pragma_set(tmp_path: Path, store_cls: type) -> None:
    store = store_cls(tmp_path / "memory.db")
    conn = store._connect()  # noqa: SLF001 - pinning the pragma on raw connections
    try:
        timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        conn.close()
    assert int(timeout) == 10000


# ---------------------------------------------------------------------------
# episodic — FTS duplication + LIKE fallback discipline
# ---------------------------------------------------------------------------


def test_rewrite_same_turn_id_keeps_fts_single_and_fresh(tmp_path: Path) -> None:
    """Re-writing a turn_id must not duplicate turns_fts nor resurrect old text."""
    db = tmp_path / "memory.db"
    store = EpisodicStore(db)
    store.append_turn(turn_id="t1", conversation_id="c1", role="user", text="eski metin kahve")
    store.append_turn(turn_id="t1", conversation_id="c1", role="user", text="yeni metin portakal")

    hits = store.search_keyword("portakal")
    assert [h.id for h in hits] == ["t1"]
    assert hits[0].text == "yeni metin portakal"
    # the replaced text must be gone from the index, not haunting search
    assert store.search_keyword("kahve") == []

    raw = sqlite3.connect(db)
    try:
        n = raw.execute("SELECT COUNT(*) FROM turns_fts WHERE turn_id = 't1'").fetchone()[0]
    finally:
        raw.close()
    assert n == 1


def test_legacy_db_without_update_trigger_is_migrated(tmp_path: Path) -> None:
    """Existing DBs may predate turns_fts_au; _ensure_fts must re-add it."""
    db = tmp_path / "memory.db"
    store = EpisodicStore(db)
    store.append_turn(turn_id="t1", conversation_id="c1", role="user", text="eski metin")

    raw = sqlite3.connect(db)
    try:
        raw.execute("DROP TRIGGER turns_fts_au")
        raw.commit()
    finally:
        raw.close()

    reopened = EpisodicStore(db)  # init re-creates the trigger (IF NOT EXISTS)
    reopened.append_turn(turn_id="t1", conversation_id="c1", role="user", text="yeni portakal")
    hits = reopened.search_keyword("portakal")
    assert [h.id for h in hits] == ["t1"]
    assert reopened.search_keyword("eski") == []


def test_fts_operational_error_falls_back_to_like(tmp_path: Path) -> None:
    """LIKE fallback fires on sqlite3.OperationalError — and only then."""
    db = tmp_path / "memory.db"
    store = EpisodicStore(db)
    store.append_turn(turn_id="t1", conversation_id="c1", role="user", text="nadir kelime kavanoz")

    # Sabotage: swap the FTS5 table for a plain table of the same name, so
    # `... MATCH ?` raises OperationalError while _ensure_fts sees it "exists".
    raw = sqlite3.connect(db)
    try:
        raw.execute("DROP TABLE turns_fts")
        raw.execute("CREATE TABLE turns_fts (turn_id, conversation_id, text)")
        raw.commit()
    finally:
        raw.close()

    hits = store.search_keyword("kavanoz")
    assert [h.id for h in hits] == ["t1"]  # found via the LIKE fallback


# ---------------------------------------------------------------------------
# semantic — atomic supersede + Turkish-fold search + deterministic ordering
# ---------------------------------------------------------------------------


def test_supersede_is_atomic_when_insert_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash between invalidate and insert must not lose the old fact."""
    store = SemanticStore(tmp_path / "memory.db")
    store.upsert_fact(fact_id="f1", key="şehir", value="Ankara", trust="user_statement")

    def boom(self: SemanticStore, conn: sqlite3.Connection, **kwargs: object) -> None:
        raise RuntimeError("simulated crash mid-supersede")

    monkeypatch.setattr(SemanticStore, "_upsert_in_conn", boom)
    with pytest.raises(RuntimeError, match="mid-supersede"):
        store.supersede_fact("f1", new_value="İstanbul")

    monkeypatch.undo()
    old = store.get_fact("f1")
    assert old is not None
    assert old.is_valid, "invalidate must have been rolled back with the failed insert"
    assert old.value == "Ankara"
    assert [f.id for f in store.list_all_facts()] == ["f1"]  # no half-written new fact


def test_supersede_happy_path_still_one_old_one_new(tmp_path: Path) -> None:
    store = SemanticStore(tmp_path / "memory.db")
    store.upsert_fact(fact_id="f1", key="şehir", value="Ankara")
    result = store.supersede_fact("f1", new_value="İstanbul")
    assert result is not None
    old, new = result
    assert old.id == "f1" and not old.is_valid
    assert new.value == "İstanbul" and new.is_valid
    assert {f.value for f in store.list_facts()} == {"İstanbul"}


def test_turkish_case_insensitive_search_both_directions(tmp_path: Path) -> None:
    """'İstanbul' must be found via 'istanbul' and vice versa (SQLite LIKE is ASCII-only)."""
    store = SemanticStore(tmp_path / "memory.db")
    store.upsert_fact(fact_id="f1", key="şehir", value="İstanbul")
    store.upsert_fact(fact_id="f2", key="memleket", value="istanbul yakası")

    # lowercase query → uppercase-İ stored value
    assert {f.id for f in store.search("istanbul")} == {"f1", "f2"}
    # uppercase-İ query → lowercase stored value
    assert {f.id for f in store.search("İstanbul")} >= {"f2"}
    assert {f.id for f in store.search("İSTANBUL")} == {"f1", "f2"}
    # keys fold too
    assert {f.id for f in store.search("ŞEHİR")} == {"f1"}


def test_norm_columns_backfilled_on_existing_db(tmp_path: Path) -> None:
    """Opening a pre-migration DB adds + backfills key_norm/value_norm."""
    db = tmp_path / "memory.db"
    raw = sqlite3.connect(db)
    try:
        raw.executescript(
            """
            CREATE TABLE facts (
                id TEXT PRIMARY KEY,
                ts_first TEXT NOT NULL,
                ts_last TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                confidence REAL,
                importance REAL,
                anchored INTEGER DEFAULT 0,
                island TEXT,
                decay_rate REAL DEFAULT 0.01,
                trust TEXT NOT NULL DEFAULT 'inferred',
                source_turn_id TEXT,
                quote TEXT,
                extractor TEXT,
                valid_from TEXT,
                invalidated_at TEXT
            );
            """
        )
        raw.execute(
            "INSERT INTO facts (id, ts_first, ts_last, key, value) "
            "VALUES ('f1', '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z', "
            "'Şehir', 'İstanbul')"
        )
        raw.commit()
    finally:
        raw.close()

    store = SemanticStore(db)  # migration + backfill happen in _init_db
    assert {f.id for f in store.search("istanbul")} == {"f1"}
    assert {f.id for f in store.facts_for_key("şehir")} == {"f1"}


def test_facts_for_key_ts_tie_breaks_by_insert_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Equal ts_last (same ms; ULIDs not monotonic) → newest insert first via rowid."""
    store = SemanticStore(tmp_path / "memory.db")
    monkeypatch.setattr(
        SemanticStore, "_iso_now", staticmethod(lambda: "2026-01-01T00:00:00.000Z")
    )
    store.upsert_fact(fact_id="a", key="k", value="v1")
    store.upsert_fact(fact_id="b", key="k", value="v2")
    store.upsert_fact(fact_id="c", key="k", value="v3")
    assert [f.id for f in store.facts_for_key("k")] == ["c", "b", "a"]


# ---------------------------------------------------------------------------
# staging — pending cap (flood protection)
# ---------------------------------------------------------------------------


def test_staging_cap_rejects_oldest_pending(tmp_path: Path) -> None:
    """501st pending candidate auto-rejects the oldest; cap holds at 500."""
    store = StagingStore(tmp_path / "memory.db")
    first = store.stage(FactCandidate(key="k0", value="v0"))
    for i in range(1, 500):
        store.stage(FactCandidate(key=f"k{i}", value=f"v{i}"))
    assert store.count_pending() == 500

    newest = store.stage(FactCandidate(key="k500", value="v500"))

    assert store.count_pending() == 500
    dropped = store.get(first.id)
    assert dropped is not None
    assert dropped.status == "rejected"  # oldest pending was evicted
    accepted = store.get(newest.id)
    assert accepted is not None
    assert accepted.status == "pending"  # newest candidate won
