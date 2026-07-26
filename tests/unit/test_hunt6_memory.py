"""hunt-6 group D — memory privacy + recall-relevance regressions.

Five findings, each with a test in BOTH directions where a threshold is involved:
a relevant query must still return its result, and an unrelated query must stop
returning everything.

* D-1 ``memory.forget`` left the evidence turn searchable, so a "forgotten"
  secret came straight back on the next recall.
* D-2 vector recall had no cosine floor — an off-topic query returned the whole
  fact table, negative-cosine rows included.
* D-3 episodic FTS OR'd every token including stopwords, so "how do I bake
  sourdough bread" matched any turn containing "how"/"do".
* D-4 an SQLite build without FTS5 crashed ``EpisodicStore`` at construction,
  contradicting the module's documented LIKE-degrade contract.
* D-5 the natural-language time filter only spoke Turkish, so English-mode
  "yesterday"/"last week" were rejected as unparseable.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from akana.memory import Memory, OrchestratorSettings
from akana.memory.episodic import EpisodicStore
from akana.memory.time_expressions import parse_time_range
from akana.memory.vector_recall import make_rrf_strategy, make_vector_strategy
from akana.memory.vector import VectorStore

_ADDRESS = "12 Rosewood Lane, Springfield"


@pytest.fixture()
def mem(tmp_path: Path) -> Memory:
    return Memory.for_data_dir(tmp_path)


def _orch(memory: Memory):
    return memory.make_orchestrator(
        settings=OrchestratorSettings(rate_limits={"memory.search": 10_000})
    )


# ---------------------------------------------------------------------------
# D-1 — forget must not hand the secret back through episodic recall
# ---------------------------------------------------------------------------


def _forgettable(memory: Memory) -> tuple[str, str]:
    """A stated-then-promoted secret: returns (turn_id, fact_id)."""
    turn = memory.remember_turn(
        conversation_id="c1",
        role="user",
        text=f"benim ev adresim {_ADDRESS}",
    )
    _closed, fact = memory.assert_fact_direct(
        key="ev adresi",
        value=_ADDRESS,
        trust="user_statement",
        source_turn_id=turn.id,
    )
    return turn.id, fact.id


def test_forget_stops_the_evidence_turn_resurfacing_in_recall(mem: Memory) -> None:
    _turn_id, fact_id = _forgettable(mem)
    assert any(_ADDRESS in b.text for b in mem.recall("ev adresim").blocks)

    out = _orch(mem).handle_tool_call("memory.forget", {"target_id": fact_id})
    assert out["status"] == "forgotten"

    leaked = [b.text for b in mem.recall("ev adresim").blocks if _ADDRESS in b.text]
    assert leaked == [], f"forgotten value came back through episodic recall: {leaked}"


def test_forget_stops_the_secret_resurfacing_through_memory_search(mem: Memory) -> None:
    _turn_id, fact_id = _forgettable(mem)
    orch = _orch(mem)
    orch.handle_tool_call("memory.forget", {"target_id": fact_id})

    found = orch.handle_tool_call("memory.search", {"query": "ev adresim"})
    blob = " ".join(str(i.get("summary", "")) for i in found["items"])
    assert _ADDRESS not in blob, f"memory.search still returns the forgotten value: {blob}"


def test_forget_without_evidence_link_still_redacts_by_value(mem: Memory) -> None:
    """The fact carries no source_turn_id — the value arm has to catch the turn."""
    mem.remember_turn(conversation_id="c1", role="user", text=f"ev adresim {_ADDRESS}")
    _closed, fact = mem.assert_fact_direct(
        key="ev adresi", value=_ADDRESS, trust="user_statement"
    )
    _orch(mem).handle_tool_call("memory.forget", {"target_id": fact.id})
    assert [b.text for b in mem.recall("ev adresim").blocks if _ADDRESS in b.text] == []


def test_forget_reports_the_episodic_scope_it_actually_applied(mem: Memory) -> None:
    """The tool answer must state what was done to the conversation turns."""
    _turn_id, fact_id = _forgettable(mem)
    out = _orch(mem).handle_tool_call("memory.forget", {"target_id": fact_id})
    assert out.get("episodic_turns_excluded") == 1
    assert "note" in out and "transcript" in out["note"].lower()


def _backdate(mem: Memory, fact_id: str, when: str) -> None:
    """Give a fact a real validity window.

    Everything a test creates is stamped "now", so ``[valid_from,
    invalidated_at)`` is milliseconds wide and no ``as_of`` can land inside it.
    A real user's facts have been true for days before they forget them — this
    reproduces that shape, which is where the time-travel leak lives.
    """
    conn = sqlite3.connect(mem._db_path)
    try:
        conn.execute(
            "UPDATE facts SET valid_from = ?, ts_first = ? WHERE id = ?",
            (when, when, fact_id),
        )
        conn.commit()
    finally:
        conn.close()


def test_forget_closes_the_time_travel_window_too(mem: Memory) -> None:
    """as_of must not hand back what the user asked us to forget."""
    _turn_id, fact_id = _forgettable(mem)
    _backdate(mem, fact_id, "2026-01-05T10:00:00.000Z")
    orch = _orch(mem)
    orch.handle_tool_call("memory.forget", {"target_id": fact_id})

    out = orch.handle_tool_call("memory.search", {"query": "ev adresim", "as_of": "2026-03-01"})
    blob = " ".join(str(i.get("summary", "")) for i in out["items"])
    assert _ADDRESS not in blob, f"time-travel resurrected the forgotten value: {blob}"


def test_superseded_history_is_still_time_travellable(mem: Memory) -> None:
    """The other direction — 'it changed' is not 'forget it'; as_of must still work."""
    _closed, old = mem.assert_fact_direct(key="memleket", value="İzmir", trust="user_statement")
    _backdate(mem, old.id, "2026-01-05T10:00:00.000Z")
    mem.supersede_fact(old.id, new_value="Ankara")

    out = _orch(mem).handle_tool_call(
        "memory.search", {"query": "memleket", "as_of": "2026-03-01"}
    )
    blob = " ".join(str(i.get("summary", "")) for i in out["items"])
    assert "İzmir" in blob, f"supersede history lost from time travel: {blob}"


def test_facade_forget_fact_carries_the_same_scope(mem: Memory) -> None:
    """The Studio delete button goes through the façade, not the tool — same promise."""
    _turn_id, fact_id = _forgettable(mem)
    assert mem.forget_fact(fact_id) is True
    assert [b.text for b in mem.recall("ev adresim").blocks if _ADDRESS in b.text] == []


def test_facade_hard_delete_carries_the_same_scope(mem: Memory) -> None:
    _turn_id, fact_id = _forgettable(mem)
    assert mem.forget_fact(fact_id, hard=True) is True
    assert mem.search_turns(_ADDRESS) == []


def test_forget_leaves_the_transcript_and_unrelated_recall_intact(mem: Memory) -> None:
    """The other direction: forget is a recall gate, not a history rewrite."""
    turn_id, fact_id = _forgettable(mem)
    mem.remember_turn(conversation_id="c1", role="user", text="sabahları filtre kahve içerim")

    _orch(mem).handle_tool_call("memory.forget", {"target_id": fact_id})

    # the user's own transcript is untouched — only recall stops surfacing it
    texts = [t.text for t in mem.recent_turns("c1")]
    assert any(_ADDRESS in t for t in texts), "forget must not rewrite the transcript"
    assert mem.episodic.get_turn(turn_id) is not None
    # an unrelated turn in the same conversation still recalls normally
    assert any("kahve" in b.text for b in mem.recall("kahve").blocks)


# ---------------------------------------------------------------------------
# D-2 — vector recall needs a cosine floor (both directions)
# ---------------------------------------------------------------------------


class _ScriptedEmbedder:
    """Table-driven vectors so every (query, fact) cosine is an exact known number.

    The three facts are unit basis vectors e1/e2/e3 of a 4-D space, so a unit
    query ``(c1, c2, c3, slack)`` has cosine exactly ``ci`` against fact ``i``
    (``slack`` only makes it unit-length). No real model's numbers involved.
    """

    def __init__(self, vectors: dict[str, list[float]], *, name: str) -> None:
        self._vectors = vectors
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(self._vectors[t]) for t in texts]


def _q(c1: float, c2: float, c3: float) -> list[float]:
    import math

    return [c1, c2, c3, math.sqrt(max(0.0, 1.0 - c1 * c1 - c2 * c2 - c3 * c3))]


# Query "kan grubum ne": a real hit, a weak one, and one pointing the other way.
_ON_TOPIC = _q(0.50, 0.10, -0.42)
# Query "kuantum kromodinamiği": nothing in this database is about it.
_OFF_TOPIC = _q(0.12, 0.05, -0.30)


def _vector_fixture(tmp_path: Path, *, embedder_name: str = "fastembed:test"):
    memory = Memory.for_data_dir(tmp_path)
    _c, hit = memory.assert_fact_direct(key="kan grubu", value="0 Rh negatif")
    _c, weak = memory.assert_fact_direct(key="maaş", value="ayda 82000 TRY")
    _c, far = memory.assert_fact_direct(key="terapist", value="Dr. Aylin Kaya, salı seansı")
    vectors = {
        "kan grubum ne": _ON_TOPIC,
        "kuantum kromodinamiği": _OFF_TOPIC,
        "kan grubu: 0 Rh negatif": [1.0, 0.0, 0.0, 0.0],
        "maaş: ayda 82000 TRY": [0.0, 1.0, 0.0, 0.0],
        "terapist: Dr. Aylin Kaya, salı seansı": [0.0, 0.0, 1.0, 0.0],
    }
    embedder = _ScriptedEmbedder(vectors, name=embedder_name)
    store = VectorStore(tmp_path / "db" / "memory.db")
    for fact in (hit, weak, far):
        store.index_fact(fact.id, f"{fact.key}: {fact.value}", embedder)
    return memory, store, embedder, hit.id


def test_vector_first_keeps_the_relevant_hit(tmp_path: Path) -> None:
    """Direction 1: a real hit (cos 0.50) must still come back."""
    memory, store, embedder, hit_id = _vector_fixture(tmp_path)
    blocks = make_vector_strategy(memory, store, embedder)(query="kan grubum ne").blocks
    assert [b.source_ids[0] for b in blocks] == [hit_id], [
        (b.text, b.score) for b in blocks
    ]


def test_vector_first_returns_nothing_for_an_off_topic_query(tmp_path: Path) -> None:
    """Direction 2: nothing in the database is related → return nothing."""
    memory, store, embedder, _hit_id = _vector_fixture(tmp_path)
    blocks = make_vector_strategy(memory, store, embedder)(query="kuantum kromodinamiği").blocks
    assert blocks == [], f"off-topic query returned the fact table: {[b.text for b in blocks]}"


def test_rrf_vector_leg_is_floored_too(tmp_path: Path) -> None:
    memory, store, embedder, _hit_id = _vector_fixture(tmp_path)
    blocks = make_rrf_strategy(memory, store, embedder)(query="kuantum kromodinamiği").blocks
    texts = " ".join(b.text for b in blocks)
    assert "Aylin" not in texts and "82000" not in texts, (
        f"unrelated facts fused into rrf output: {texts}"
    )


def test_uncalibrated_embedder_still_drops_negative_cosines(tmp_path: Path) -> None:
    """No calibrated floor for this model family → at minimum, drop cos <= 0."""
    memory, store, embedder, hit_id = _vector_fixture(tmp_path, embedder_name="hashing:3gram-256")
    blocks = make_vector_strategy(memory, store, embedder)(query="kan grubum ne").blocks
    ids = [b.source_ids[0] for b in blocks]
    assert hit_id in ids  # the weak-but-positive hit is kept for an uncalibrated model
    texts = " ".join(b.text for b in blocks)
    assert "Aylin" not in texts, "a negative cosine is never evidence of relatedness"


# ---------------------------------------------------------------------------
# D-3 — episodic FTS must not OR stopwords into a match-everything query
# ---------------------------------------------------------------------------


@pytest.fixture()
def chatty(mem: Memory) -> Memory:
    for text in (
        "how do I renew my passport",
        "do not tell my brother about the loan I took",
        "my therapist Dr Aylin said I should do the breathing exercise every night",
        "the wifi password is hunter2",
    ):
        mem.remember_turn(conversation_id="c1", role="user", text=text)
    return mem


def test_off_topic_query_stops_returning_unrelated_private_turns(chatty: Memory) -> None:
    blocks = chatty.recall("how do I bake sourdough bread").blocks
    assert blocks == [], f"off-topic query recalled unrelated turns: {[b.text for b in blocks]}"


def test_stopword_only_query_matches_nothing_instead_of_like_scanning(chatty: Memory) -> None:
    assert chatty.episodic.search_keyword("how do I") == []


def test_on_topic_query_still_finds_its_turn(chatty: Memory) -> None:
    """The other direction — the stopword filter must not break real queries."""
    hits = [t.text for t in chatty.episodic.search_keyword("how do I renew my passport")]
    assert any("passport" in t for t in hits), hits
    assert any(
        "hunter2" in b.text for b in chatty.recall("what is the wifi password").blocks
    ), "a content-word query must still recall its turn"


def test_turkish_episodic_recall_still_works(chatty: Memory) -> None:
    chatty.remember_turn(
        conversation_id="c2", role="user", text="dolmuş ücreti yirmi beş lira olmuş"
    )
    assert any(
        "yirmi beş lira" in b.text for b in chatty.recall("dolmuş ücreti ne kadardı").blocks
    )


# ---------------------------------------------------------------------------
# D-4 — an FTS5-less SQLite must degrade to LIKE, as the module docstring promises
# ---------------------------------------------------------------------------


class _NoFts5Connection:
    """A connection from a SQLite build compiled without the FTS5 module.

    Only ``CREATE VIRTUAL TABLE ... USING fts5`` fails; everything else behaves
    normally, which is exactly what such a build does. Queries against
    ``turns_fts`` then fail on their own ("no such table") — no need to fake that.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        object.__setattr__(self, "_conn", conn)

    def executescript(self, script: str):
        if "fts5" in script.lower():
            raise sqlite3.OperationalError("no such module: fts5")
        return self._conn.executescript(script)

    def __getattr__(self, name: str):
        return getattr(object.__getattribute__(self, "_conn"), name)

    def __setattr__(self, name: str, value) -> None:
        setattr(object.__getattribute__(self, "_conn"), name, value)


@pytest.fixture()
def no_fts5_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> EpisodicStore:
    def _connect(self: EpisodicStore) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=10000")
        conn.row_factory = sqlite3.Row
        return _NoFts5Connection(conn)  # type: ignore[return-value]

    monkeypatch.setattr(EpisodicStore, "_connect", _connect)
    return EpisodicStore(tmp_path / "memory.db")


def test_store_constructs_without_fts5(no_fts5_store: EpisodicStore) -> None:
    no_fts5_store.append_turn(
        turn_id="t1", conversation_id="c1", role="user", text="kahve sevdiğimi unutma"
    )
    assert [t.id for t in no_fts5_store.list_conversation_recent("c1")] == ["t1"]


def test_keyword_search_degrades_to_like_without_fts5(no_fts5_store: EpisodicStore) -> None:
    no_fts5_store.append_turn(
        turn_id="t1", conversation_id="c1", role="user", text="kahve sevdiğimi unutma"
    )
    no_fts5_store.append_turn(
        turn_id="t2", conversation_id="c1", role="user", text="çay istemiyorum"
    )
    assert [t.id for t in no_fts5_store.search_keyword("kahve")] == ["t1"]


def test_memory_stack_comes_up_without_fts5(tmp_path: Path, monkeypatch) -> None:
    def _connect(self: EpisodicStore) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=10000")
        conn.row_factory = sqlite3.Row
        return _NoFts5Connection(conn)  # type: ignore[return-value]

    monkeypatch.setattr(EpisodicStore, "_connect", _connect)
    memory = Memory.for_data_dir(tmp_path)
    memory.remember_turn(conversation_id="c1", role="user", text="kahve sevdiğimi unutma")
    assert any("kahve" in b.text for b in memory.recall("kahve").blocks)


# ---------------------------------------------------------------------------
# D-5 — the time filter ships bilingual (EN default, TR on explicit choice)
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)  # a Sunday


@pytest.fixture(autouse=True)
def _pinned_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every time assertion here is zone-sensitive — pin it, never inherit the host's."""
    monkeypatch.setenv("AKANA_TIMEZONE", "+03:00")


@pytest.mark.parametrize(
    "expr",
    [
        "today",
        "yesterday",
        "this week",
        "last week",
        "this month",
        "last month",
        "this year",
        "last year",
        "last 7 days",
        "last 3 hours",
        "3 days ago",
        "in March",
        "march 2025",
    ],
)
def test_english_time_expressions_parse(expr: str) -> None:
    assert parse_time_range(expr, now=_NOW) is not None, f"{expr!r} rejected in English mode"


def test_english_yesterday_matches_turkish_dun() -> None:
    assert parse_time_range("yesterday", now=_NOW) == parse_time_range("dün", now=_NOW)


def test_english_last_week_matches_turkish_gecen_hafta() -> None:
    assert parse_time_range("last week", now=_NOW) == parse_time_range("geçen hafta", now=_NOW)


@pytest.mark.parametrize(
    "expr", ["bugün", "dün", "geçen hafta", "gecen hafta", "son 7 gün", "3 gün önce", "mart ayında"]
)
def test_turkish_time_expressions_still_parse(expr: str) -> None:
    assert parse_time_range(expr, now=_NOW) is not None


def test_english_time_range_reaches_memory_search(mem: Memory) -> None:
    """The orchestrator rejected 'last week' with invalid_request before this fix."""
    out = _orch(mem).handle_tool_call(
        "memory.search", {"query": "project", "time_range": {"from": "last week"}}
    )
    assert "error" not in out, out.get("error")


# ---------------------------------------------------------------------------
# D-5b — "yesterday" is the USER's yesterday, not Istanbul's (handover item)
# ---------------------------------------------------------------------------


def test_relative_days_bucket_in_the_configured_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    """A +03:00 pin shifted every non-UTC+3 user's window by hours, silently."""
    monkeypatch.setenv("AKANA_TIMEZONE", "UTC")
    assert parse_time_range("yesterday", now=_NOW) == (
        "2026-07-25T00:00:00.000Z",
        "2026-07-25T23:59:59.999Z",
    )

    monkeypatch.setenv("AKANA_TIMEZONE", "+03:00")
    assert parse_time_range("yesterday", now=_NOW) == (
        "2026-07-24T21:00:00.000Z",  # 25 July 00:00 in Istanbul
        "2026-07-25T20:59:59.999Z",
    )


def test_negative_offset_zone_shifts_the_day_the_other_way(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AKANA_TIMEZONE", "-05:00")
    # 26 July 02:00 UTC is still 25 July 21:00 in New York → "today" there is the
    # 25th, while the old +03:00 pin would have called it the 26th.
    early = datetime(2026, 7, 26, 2, 0, tzinfo=UTC)
    assert parse_time_range("today", now=early) == (
        "2026-07-25T05:00:00.000Z",
        "2026-07-26T04:59:59.999Z",
    )
    monkeypatch.setenv("AKANA_TIMEZONE", "+03:00")
    assert parse_time_range("today", now=early)[0] == "2026-07-25T21:00:00.000Z"


def test_explicit_offsets_are_not_re_bucketed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only relative phrases move with the zone; a stated instant keeps its meaning."""
    from akana.memory.tools import parse_time_point

    for zone in ("UTC", "+03:00", "-05:00"):
        monkeypatch.setenv("AKANA_TIMEZONE", zone)
        assert parse_time_point("2026-06-01T03:00:00+03:00") == "2026-06-01T00:00:00.000Z"
