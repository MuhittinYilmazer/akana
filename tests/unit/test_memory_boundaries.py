"""Boundary-value regressions — memory layer quality pass.

Three real bugs were proven in this pass → failing test → minimal fix → green:

1. **Supersede coverage hole.** ``supersede_fact`` invalidates the old row at
   the ``ts`` instant, but the new row produced its ``valid_from`` from
   ``_upsert_in_conn``'s own (one tick later) ts. Half-open windows
   ``[valid_from, invalidated_at)`` were not tiled over that one millisecond:
   an ``as_of == old.invalidated_at`` query returned neither the old nor the
   new row. Fix: the new row's ``valid_from`` is set exactly equal to the old
   row's ``invalidated_at``.
2. **Inverted time window silent gap.** ``observed_from > observed_to`` (or
   ``time_range`` from>to) is parseable but logically an empty intersection;
   the orchestrator returned this silently as 0 results — the model could not
   tell "memory is empty" apart from "your window is nonsense". Fix: a warning
   is surfaced (consistent with the code's own "a broken bound is not silently
   swallowed" principle).
3. **Unbounded manual title.** ``ConversationStore.patch`` clipped the
   auto-title to 60 but wrote the manual title unbounded — a runaway title
   could bloat the conversation list payload. Fix: a ``_TITLE_MAX`` (200) cap.

The rest are characterization tests (locking down the existing sound behavior).
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
import ulid

from akana.memory import Memory
from akana.memory.conversations import ConversationStore
from akana.memory.episodic import EpisodicStore
from akana.memory.semantic import SemanticStore
from akana.memory.staging import FactCandidate, StagingStore
from akana.memory.terms import fold_text, recall_search_terms
from akana.memory.tools import parse_time_bound, parse_time_point


@pytest.fixture()
def semantic(tmp_path: Path) -> SemanticStore:
    return SemanticStore(tmp_path / "memory.db")


# ---------------------------------------------------------------------------
# FIX 1 — supersede validity window: no coverage hole at the supersede instant
# ---------------------------------------------------------------------------


def test_supersede_new_valid_from_equals_old_invalidated_at(semantic: SemanticStore) -> None:
    """The new row's valid_from matches the old row's invalidated_at EXACTLY (no gap)."""
    semantic.upsert_fact(fact_id="f0", key="şehir", value="Ankara", trust="user_statement")
    result = semantic.supersede_fact("f0", new_value="İzmir")
    assert result is not None
    old, new = result
    assert old.invalidated_at is not None
    assert new.valid_from == old.invalidated_at


def test_as_of_at_supersede_instant_returns_exactly_one(semantic: SemanticStore) -> None:
    """as_of == old.invalidated_at: neither a hole (0) nor a double count (2) — exactly 1."""
    semantic.upsert_fact(fact_id="f0", key="şehir", value="Ankara", trust="user_statement")
    old, new = semantic.supersede_fact("f0", new_value="İzmir")  # type: ignore[misc]
    at = old.invalidated_at
    assert at is not None
    rows = semantic.facts_as_of("şehir", at)
    assert [r.value for r in rows] == ["İzmir"]  # the new row is covered, not the old


def test_as_of_before_and_after_supersede_window(semantic: SemanticStore) -> None:
    """Window edges: old at the start, new in the middle, nothing in the distant past."""
    semantic.upsert_fact(fact_id="f0", key="şehir", value="Ankara", trust="user_statement")
    old, new = semantic.supersede_fact("f0", new_value="İzmir")  # type: ignore[misc]
    assert [r.value for r in semantic.facts_as_of("şehir", old.valid_from)] == ["Ankara"]
    assert [r.value for r in semantic.facts_as_of("şehir", new.valid_from)] == ["İzmir"]
    assert semantic.facts_as_of("şehir", "1990-01-01T00:00:00.000Z") == []


def test_deep_supersede_chain_keeps_single_valid_and_full_history(
    semantic: SemanticStore,
) -> None:
    """100-deep supersede chain: one valid row, 101 rows of history, no holes."""
    semantic.upsert_fact(fact_id="f0", key="şehir", value="v0", trust="user_statement")
    cur = "f0"
    instants: list[str] = []
    for i in range(1, 101):
        res = semantic.supersede_fact(cur, new_value=f"v{i}")
        assert res is not None
        old, new = res
        instants.append(old.invalidated_at or "")
        cur = new.id
    valid = semantic.list_facts()
    assert [f.value for f in valid] == ["v100"]
    assert len(semantic.facts_for_key("şehir", include_invalidated=True)) == 101
    # exactly 1 row is covered at each supersede instant — no hole along the chain
    for at in instants:
        assert len(semantic.facts_as_of("şehir", at)) == 1


# ---------------------------------------------------------------------------
# FIX 2 — inverted time window: surfaced as a warning, not a silent empty
# ---------------------------------------------------------------------------


def test_inverted_observed_window_warns(tmp_path: Path) -> None:
    """observed_from > observed_to: not a silent 0, but an explicit warning."""
    memory = Memory.for_data_dir(tmp_path)
    memory.assert_fact_direct(
        key="şehir", value="Ankara", trust="user_statement",
        observed_at="2026-03-01T00:00:00.000Z",
    )
    orch = memory.make_orchestrator()
    res = orch.handle_tool_call(
        "memory.search",
        {"query": "şehir", "observed_from": "2026-06-01", "observed_to": "2026-01-01"},
    )
    assert res.get("error") is None
    assert res["items"] == []
    assert any("observed window inverted" in w for w in res["warnings"])


def test_inverted_time_range_warns(tmp_path: Path) -> None:
    """time_range from > to: a warning is surfaced in the same way."""
    memory = Memory.for_data_dir(tmp_path)
    memory.assert_fact_direct(key="şehir", value="Ankara", trust="user_statement")
    orch = memory.make_orchestrator()
    res = orch.handle_tool_call(
        "memory.search",
        {"query": "şehir", "time_range": {"from": "2026-06-01", "to": "2026-01-01"}},
    )
    assert res.get("error") is None
    assert any("time_range inverted" in w for w in res["warnings"])


def test_well_ordered_observed_window_no_inversion_warning(tmp_path: Path) -> None:
    """A well-ordered window produces no warning (no false positive)."""
    memory = Memory.for_data_dir(tmp_path)
    memory.assert_fact_direct(
        key="şehir", value="Ankara", trust="user_statement",
        observed_at="2026-03-01T00:00:00.000Z",
    )
    orch = memory.make_orchestrator()
    res = orch.handle_tool_call(
        "memory.search",
        {"query": "şehir", "observed_from": "2026-01-01", "observed_to": "2026-06-01"},
    )
    assert not any("inverted" in w for w in res["warnings"])


def test_malformed_iso_window_still_400_loud(tmp_path: Path) -> None:
    """A malformed format is still invalid_request as before — not downgraded to a warning."""
    memory = Memory.for_data_dir(tmp_path)
    orch = memory.make_orchestrator()
    res = orch.handle_tool_call(
        "memory.search", {"query": "x", "observed_from": "2026-13-45"}
    )
    err = res.get("error")
    assert err is not None
    assert err["code"] == "invalid_request"


# ---------------------------------------------------------------------------
# FIX 3 — manual conversation title is length-capped like the auto-title
# ---------------------------------------------------------------------------


def test_patch_title_is_length_capped(tmp_path: Path) -> None:
    """The manual title is capped too (previously unbounded → payload bloat)."""
    db = tmp_path / "memory.db"
    store = ConversationStore(db, episodic=EpisodicStore(db))
    store.ensure("c1")
    meta = store.patch("c1", title="İ" * 5000)
    assert meta is not None
    assert len(meta.title or "") == 200


def test_patch_short_title_unchanged(tmp_path: Path) -> None:
    """A short title stays as-is (the cap does not damage innocent input)."""
    db = tmp_path / "memory.db"
    store = ConversationStore(db, episodic=EpisodicStore(db))
    store.ensure("c1")
    meta = store.patch("c1", title="  Kısa Başlık  ")
    assert meta is not None
    assert meta.title == "Kısa Başlık"


def test_auto_title_unicode_truncation_no_crash(tmp_path: Path) -> None:
    """An emoji/long first line is clipped to 60, with no crash/broken surrogate."""
    db = tmp_path / "memory.db"
    store = ConversationStore(db, episodic=EpisodicStore(db))
    store.on_user_message("c1", "x" * 59 + "😀🎉 kalan metin buraya kadar uzar")
    meta = store.get("c1")
    assert meta is not None
    assert len(meta.title or "") <= 60


# ---------------------------------------------------------------------------
# semantic value/key boundaries
# ---------------------------------------------------------------------------


def test_one_megabyte_value_is_truncated_to_8000(semantic: SemanticStore) -> None:
    """A 1 MB value is clipped to 8000 characters (so the store doesn't bloat), no crash."""
    fact = semantic.upsert_fact(fact_id="big", key="blob", value="ç" * 1_000_000)
    stored = semantic.get_fact(fact.id)
    assert stored is not None
    assert len(stored.value) == 8000


def test_whitespace_only_key_strips_to_empty(semantic: SemanticStore) -> None:
    """A whitespace-only key becomes an empty string after strip (no crash)."""
    fact = semantic.upsert_fact(fact_id="ws", key="   \t\n ", value="değer")
    assert fact.key == ""
    assert semantic.get_fact("ws") is not None


def test_emoji_and_unicode_key_value_round_trip(semantic: SemanticStore) -> None:
    """An emoji + unicode key/value is stored without issue and is searchable when folded."""
    semantic.upsert_fact(
        fact_id="em", key="favori 😀", value="parti 🎉🎊", trust="user_statement"
    )
    assert {f.id for f in semantic.search("favori")} == {"em"}


def test_invalid_provenance_origin_rejected(semantic: SemanticStore) -> None:
    """An invalid source_origin value is rejected with ValueError (no silent acceptance)."""
    with pytest.raises(ValueError, match="source_origin must be one of"):
        semantic.upsert_fact(fact_id="bad", key="k", value="v", source_origin="hacker")


def test_negative_and_huge_search_limit_clamped(semantic: SemanticStore) -> None:
    """A negative limit is at least 1, a huge limit is clipped to the upper bound (500) — no crash."""
    for i in range(5):
        semantic.upsert_fact(fact_id=f"f{i}", key="şehir", value=f"Ankara {i}")
    assert semantic.search("şehir", limit=-10) != []  # max(1, ...) path
    assert len(semantic.search("şehir", limit=10_000_000)) <= 500


def test_list_facts_negative_limit_returns_at_least_one(semantic: SemanticStore) -> None:
    semantic.upsert_fact(fact_id="f1", key="şehir", value="Ankara")
    assert len(semantic.list_facts(limit=-5)) >= 1


# ---------------------------------------------------------------------------
# Turkish fold edge cases (İ/I/ı, ğ, combining)
# ---------------------------------------------------------------------------


def test_dotless_and_dotted_i_fold_search(semantic: SemanticStore) -> None:
    """İ/I/ı folding: 'IŞIK' and 'ışık' collapse to the same folded value."""
    semantic.upsert_fact(fact_id="f1", key="kelime", value="IŞIK", trust="user_statement")
    assert {f.id for f in semantic.search("ışık")} == {"f1"}
    assert {f.id for f in semantic.search("IŞIK")} == {"f1"}


def test_combining_dot_above_folds_to_precomposed(semantic: SemanticStore) -> None:
    """I + U+0307 (combining dot) folds the same as the precomposed İ."""
    combining = "I" + "̇" + "stanbul"  # İstanbul (decomposed)
    assert fold_text(combining) == fold_text("İstanbul")
    semantic.upsert_fact(fact_id="f1", key="şehir", value=combining)
    assert {f.id for f in semantic.search("istanbul")} == {"f1"}


def test_soft_g_fold_preserved(semantic: SemanticStore) -> None:
    """'ğ' upper/lower folding: 'DAĞ' and 'dağ' match."""
    semantic.upsert_fact(fact_id="f1", key="coğrafya", value="DAĞ", trust="user_statement")
    assert {f.id for f in semantic.search("dağ")} == {"f1"}


# ---------------------------------------------------------------------------
# recall term expansion — FTS-special chars + degenerate queries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    ['"', "*", "(test)", "NEAR", '" OR 1=1 --', "AND OR NOT", "%_\\"],
)
def test_recall_terms_never_crash_on_special_chars(query: str) -> None:
    """FTS/LIKE special characters produce no crash/injection during term expansion."""
    terms = recall_search_terms(query)
    assert isinstance(terms, list)


def test_semantic_search_special_chars_no_injection(semantic: SemanticStore) -> None:
    """LIKE special characters (%/_) match literally, no wildcard explosion."""
    semantic.upsert_fact(fact_id="f1", key="ipucu", value="yüzde 50 indirim", trust="user_statement")
    semantic.upsert_fact(fact_id="f2", key="kod", value="a_b_c değeri", trust="user_statement")
    # '%' is searched literally: it must not match anything like a wildcard
    assert all(f.id != "f1" for f in semantic.search("%%%%"))


def test_ten_thousand_char_query_capped_no_crash(semantic: SemanticStore) -> None:
    """10K query: the term count/length is bounded, no crash."""
    semantic.upsert_fact(fact_id="f1", key="kelime", value="kavanoz", trust="user_statement")
    big = "kavanoz " * 2000  # ~16K characters
    terms = recall_search_terms(big)
    assert len(terms) <= 12  # _MAX_TERMS
    assert all(len(t) <= 80 for t in terms)
    assert semantic.search(big) != []


def test_empty_and_whitespace_query_returns_empty(semantic: SemanticStore) -> None:
    semantic.upsert_fact(fact_id="f1", key="şehir", value="Ankara")
    assert semantic.search("") == []
    assert semantic.search("   ") == []


# ---------------------------------------------------------------------------
# concurrency — same-key remember race + concurrent supersede
# ---------------------------------------------------------------------------


def test_concurrent_same_key_value_remember_dedups(semantic: SemanticStore) -> None:
    """20 concurrent upserts to the same key+value → one valid row (race)."""
    def go() -> None:
        semantic.upsert_fact(
            fact_id=str(ulid.new()), key="ad", value="Alice", trust="user_statement"
        )

    threads = [threading.Thread(target=go) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(semantic.facts_for_key("ad")) == 1


def test_concurrent_supersede_exactly_one_winner(semantic: SemanticStore) -> None:
    """Concurrent supersede on the same fact: exactly one winner, the rest no-op (None)."""
    semantic.upsert_fact(fact_id="f1", key="şehir", value="Ankara", trust="user_statement")
    outcomes: list[str] = []

    def go() -> None:
        res = semantic.supersede_fact("f1", new_value="İstanbul")
        outcomes.append("ok" if res else "none")

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("ok") == 1
    assert len(semantic.list_facts()) == 1


# ---------------------------------------------------------------------------
# staging — idempotency on a decided row + flood cap interaction
# ---------------------------------------------------------------------------


def test_restage_promoted_id_is_noop(tmp_path: Path) -> None:
    """Re-staging a decided (promoted) row does not overwrite the decision."""
    store = StagingStore(tmp_path / "memory.db")
    store.stage(FactCandidate(key="k", value="v"), staged_id="S1")
    store.mark_promoted("S1", "fact1")
    again = store.stage(FactCandidate(key="k", value="v2"), staged_id="S1")
    assert again.status == "promoted"
    assert again.value == "v"  # the old value is preserved, does not revert to pending


def test_restage_pending_id_does_not_trigger_flood_eviction(tmp_path: Path) -> None:
    """Refreshing an existing pending row does not evict innocent rows via flood."""
    store = StagingStore(tmp_path / "memory.db")
    first = store.stage(FactCandidate(key="k0", value="v0"))
    for i in range(1, 500):
        store.stage(FactCandidate(key=f"k{i}", value=f"v{i}"))
    assert store.count_pending() == 500
    # refresh with the same id — must not trigger the cap
    store.stage(FactCandidate(key="k0", value="v0-fresh"), staged_id=first.id)
    assert store.count_pending() == 500
    assert store.get(first.id) is not None
    assert store.get(first.id).status == "pending"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# time parsing — out-of-range ISO + boundary years
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["2026-13-45", "2026-02-30", "2026-00-10", "2026-01-32", "Z", "garbage", ""],
)
def test_parse_time_point_rejects_invalid(value: str) -> None:
    assert parse_time_point(value) is None


def test_parse_time_bound_far_past_and_future(semantic: SemanticStore) -> None:
    """A very old / very far-future date is parsed, the as_of logic is not broken."""
    assert parse_time_bound("0001-01-01", edge="end") == "0001-01-01T23:59:59.999Z"
    assert parse_time_point("9999-12-31") == "9999-12-31T00:00:00.000Z"
    semantic.upsert_fact(fact_id="f1", key="şehir", value="Ankara", trust="user_statement")
    assert [f.value for f in semantic.facts_as_of("şehir", "9999-12-31T00:00:00.000Z")] == ["Ankara"]
    assert semantic.facts_as_of("şehir", "0001-01-01T00:00:00.000Z") == []
