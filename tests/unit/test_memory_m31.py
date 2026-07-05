"""M3.1 — A (salience columns) and D (as_of time-travel).

The salience write path (``SemanticStore.bump_usage``) was removed as dead
code (never wired to a production caller — ``used_ids``/``miss_ids`` always
arrive empty from the orchestrator). The ``hit_count``/``miss_count``/
``last_hit_at`` columns themselves remain: they are migrated on every DB
open and displayed by the ``/facts`` route, so schema coverage stays here.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from akana.memory import Memory
from akana.memory.semantic import SemanticStore
from akana.memory.tools import tool_schemas


@pytest.fixture()
def memory(tmp_path: Path) -> Memory:
    return Memory.for_data_dir(tmp_path)


@pytest.fixture()
def orch(memory):
    return memory.make_orchestrator()


@pytest.fixture()
def store(tmp_path: Path) -> SemanticStore:
    return SemanticStore(tmp_path / "memory.db")


# -- A: salience columns -----------------------------------------------------------


def test_salience_columns_default_zero(store: SemanticStore) -> None:
    store.upsert_fact(fact_id="a", key="kedi adı", value="Pamuk")
    fact = store.get_fact("a")
    assert fact is not None
    assert (fact.hit_count, fact.miss_count, fact.last_hit_at) == (0, 0, None)


def test_salience_schema_migration_on_old_db(tmp_path: Path) -> None:
    """When an old (pre-M3.1) db is opened, salience columns are added via ALTER."""
    db = tmp_path / "memory.db"
    conn = sqlite3.connect(db)
    conn.executescript(
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
        INSERT INTO facts (id, ts_first, ts_last, key, value, confidence, importance)
        VALUES ('eski', '2025-01-01T00:00:00.000Z', '2025-01-01T00:00:00.000Z',
                'şehir', 'Ankara', 0.9, 0.7);
        """
    )
    conn.commit()
    conn.close()

    store = SemanticStore(db)  # __init__ migrates
    check = sqlite3.connect(db)
    cols = {row[1] for row in check.execute("PRAGMA table_info(facts)")}
    check.close()
    assert {"hit_count", "miss_count", "last_hit_at"} <= cols

    fact = store.get_fact("eski")
    assert fact is not None
    assert (fact.hit_count, fact.miss_count, fact.last_hit_at) == (0, 0, None)


# -- D: as_of time-travel ---------------------------------------------------------------


def _seed_superseded_city(memory: Memory) -> tuple[str, str]:
    """v1 (Ankara since 2020) → superseded to İstanbul today."""
    v1 = memory.semantic.upsert_fact(
        fact_id="sehir-v1",
        key="şehir",
        value="Ankara",
        trust="user_statement",
        valid_from="2020-01-01T00:00:00.000Z",
    )
    old, new = memory.supersede_fact(v1.id, new_value="İstanbul")
    return old.id, new.id


def test_as_of_returns_old_value_current_returns_new(memory: Memory, orch) -> None:
    old_id, new_id = _seed_superseded_city(memory)
    memory.remember_turn(role="user", conversation_id="c1", text="şehir değişti bu arada")

    past = orch.handle_tool_call("memory.search", {"query": "şehir", "as_of": "2023-06-15"})
    assert "error" not in past
    summaries = [i["summary"] for i in past["items"]]
    assert any("Ankara" in s for s in summaries), summaries
    assert not any("İstanbul" in s for s in summaries)
    assert [i["id"] for i in past["items"] if i["type"] == "Fact"] == [old_id]
    # today's turn ts > as_of → episodic post-filter drops it
    assert not any(i["type"] == "Episode" for i in past["items"])
    assert past["trace"]["strategy"] == "as_of"
    assert not any("as_of" in w for w in past["warnings"])

    today = orch.handle_tool_call("memory.search", {"query": "şehir"})
    ids = [i["id"] for i in today["items"]]
    assert new_id in ids and old_id not in ids
    assert any("İstanbul" in i["summary"] for i in today["items"])


def test_as_of_before_fact_existed_is_empty(memory: Memory, orch) -> None:
    _seed_superseded_city(memory)
    out = orch.handle_tool_call("memory.search", {"query": "şehir", "as_of": "2019-01-01"})
    assert "error" not in out
    assert out["items"] == []


def test_as_of_relative_form_uses_current_state(memory: Memory, orch) -> None:
    """'relative:1h' is a valid as_of — it sees the current state (the new value)."""
    old_id, new_id = _seed_superseded_city(memory)
    out = orch.handle_tool_call("memory.search", {"query": "şehir", "as_of": "relative:0h"})
    assert "error" not in out
    fact_ids = [i["id"] for i in out["items"] if i["type"] == "Fact"]
    assert fact_ids == [new_id]


def test_as_of_invalid_is_invalid_request(memory: Memory, orch) -> None:
    out = orch.handle_tool_call("memory.search", {"query": "şehir", "as_of": "saçma-tarih"})
    assert out["error"]["code"] == "invalid_request"
    assert "as_of" in out["error"]["message"]


def test_facts_as_of_window_edges(store: SemanticStore) -> None:
    """Window: valid_from <= as_of (inclusive) and invalidated_at > as_of (exclusive)."""
    t1 = "2024-01-01T00:00:00.000Z"
    t2 = "2024-06-01T00:00:00.000Z"
    store.upsert_fact(fact_id="f1", key="tema", value="koyu", valid_from=t1)
    store.invalidate_fact("f1", at=t2)

    assert [f.id for f in store.facts_as_of("tema", t1)] == ["f1"]  # exactly the valid_from moment
    assert [f.id for f in store.facts_as_of("tema", "2024-03-01T00:00:00.000Z")] == ["f1"]
    assert store.facts_as_of("tema", t2) == []  # gone at exactly the invalidated_at moment
    assert store.facts_as_of("tema", "2023-12-31T23:59:59.999Z") == []


def test_as_of_turkish_phrase(memory: Memory, orch) -> None:
    """as_of accepts a Turkish natural phrase: 'bugün' = end of day → current state."""
    old_id, new_id = _seed_superseded_city(memory)
    out = orch.handle_tool_call("memory.search", {"query": "şehir", "as_of": "bugün"})
    assert "error" not in out
    assert out["trace"]["strategy"] == "as_of"
    fact_ids = [i["id"] for i in out["items"] if i["type"] == "Fact"]
    assert fact_ids == [new_id]


# -- bi-temporal: observed_from / observed_to observation filter ---------------------------


def _seed_observed_pair(memory: Memory) -> tuple[str, str]:
    """Two facts on the same topic: one with an OLD observation (observed in March 2026), one with a NEW observation."""
    eski = memory.semantic.upsert_fact(
        fact_id="kahve-eski",
        key="kahve notu",
        value="filtre kahve sever",
        trust="user_statement",
        observed_at="2026-03-10T09:00:00.000Z",
    )
    yeni = memory.semantic.upsert_fact(
        fact_id="kahve-yeni",
        key="kahve makinesi",
        value="kahve makinesi espresso yapıyor",
        trust="user_statement",
    )  # observed_at = now
    return eski.id, yeni.id


def test_observed_range_separates_old_from_new(memory: Memory, orch) -> None:
    eski_id, yeni_id = _seed_observed_pair(memory)

    # No filter: both are returned.
    both = orch.handle_tool_call("memory.search", {"query": "kahve"})
    assert {eski_id, yeni_id} <= {i["id"] for i in both["items"]}

    # March window: only the old observation.
    march = orch.handle_tool_call(
        "memory.search",
        {"query": "kahve", "observed_from": "2026-03-01", "observed_to": "2026-03-31"},
    )
    assert [i["id"] for i in march["items"]] == [eski_id]

    # Observed from today onward: only the new record.
    recent = orch.handle_tool_call(
        "memory.search", {"query": "kahve", "observed_from": "relative:1h"}
    )
    assert [i["id"] for i in recent["items"]] == [yeni_id]

    # if observed_to ends in the past, the new record is filtered out.
    until_april = orch.handle_tool_call(
        "memory.search", {"query": "kahve", "observed_to": "2026-04-01"}
    )
    assert [i["id"] for i in until_april["items"]] == [eski_id]

    # Trace reports from a single point: the observed_filter stage counts what was dropped.
    stage = next(s for s in march["trace"]["stages"] if s["stage"] == "observed_filter")
    assert stage["dropped"] == 1
    assert stage["from"] == "2026-03-01T00:00:00.000Z"
    assert stage["to"] == "2026-03-31T23:59:59.999Z"  # a date-only 'to' covers the whole day


def test_observed_turkish_phrase_and_episodes(memory: Memory, orch) -> None:
    """'bugün' observation range: the fact learned today + today's turn remain, the old one drops."""
    eski_id, yeni_id = _seed_observed_pair(memory)
    memory.remember_turn(role="user", conversation_id="c1", text="kahve çekirdeği bitti")

    out = orch.handle_tool_call(
        "memory.search",
        {"query": "kahve", "observed_from": "bugün", "observed_to": "bugün"},
    )
    assert "error" not in out
    ids = [i["id"] for i in out["items"]]
    assert yeni_id in ids and eski_id not in ids
    assert any(i["type"] == "Episode" for i in out["items"])  # turn ts = observation moment


def test_observed_invalid_is_invalid_request(orch) -> None:
    out = orch.handle_tool_call(
        "memory.search", {"query": "x", "observed_from": "saçma-tarih"}
    )
    assert out["error"]["code"] == "invalid_request"
    assert "observed_from" in out["error"]["message"]
    out2 = orch.handle_tool_call(
        "memory.search", {"query": "x", "observed_to": "saçma-tarih"}
    )
    assert out2["error"]["code"] == "invalid_request"
    assert "observed_to" in out2["error"]["message"]


def test_observed_works_combined_with_as_of(memory: Memory, orch) -> None:
    """as_of (validity) + observed (observation) work together in the same call."""
    v1 = memory.semantic.upsert_fact(
        fact_id="sehir-v1",
        key="şehir",
        value="Ankara",
        trust="user_statement",
        valid_from="2020-01-01T00:00:00.000Z",
        observed_at="2020-01-01T00:00:00.000Z",
    )
    memory.supersede_fact(v1.id, new_value="İstanbul")

    past = orch.handle_tool_call(
        "memory.search",
        {
            "query": "şehir",
            "as_of": "2023-06-15",
            "observed_from": "2019-01-01",
            "observed_to": "2021-01-01",
        },
    )
    assert [i["id"] for i in past["items"]] == [v1.id]
    # If the observation window excludes 2020, the as_of result also becomes empty.
    empty = orch.handle_tool_call(
        "memory.search",
        {"query": "şehir", "as_of": "2023-06-15", "observed_from": "2021-01-01"},
    )
    assert empty["items"] == []


def test_search_schema_has_observed_params() -> None:
    schema = next(s for s in tool_schemas() if s["name"] == "memory.search")
    props = schema["input_schema"]["properties"]
    assert "observed_from" in props and "observed_to" in props
    assert "as_of" in props
