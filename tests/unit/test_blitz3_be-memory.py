"""Bug-blitz-3 regression tests for area be-memory.

One test per verified finding (curator / summary_consolidation / orchestrator /
semantic). Each asserts the behavior contract the fix restores; before the fix the
corresponding assertion fails for the documented reason.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from akana.memory import FactCandidate, Memory
from akana.memory.curator import Curator


@pytest.fixture()
def mem(tmp_path: Path) -> Memory:
    return Memory.for_data_dir(tmp_path)


@pytest.fixture()
def curator(mem: Memory) -> Curator:
    return mem.make_curator()


# -- be-memory-1: promote reverts the staging claim on durable-write failure --------


def test_promote_reverts_claim_when_durable_write_fails(
    mem: Memory, curator: Curator, monkeypatch: pytest.MonkeyPatch
) -> None:
    staged = mem.staging.stage(
        FactCandidate(key="ad", value="Alice", trust="user_statement")
    )

    def boom(*_a: object, **_k: object) -> object:
        raise sqlite3.OperationalError("database is locked")

    # The curator shares mem's SemanticStore instance → patching it patches the write.
    monkeypatch.setattr(mem._semantic, "assert_fact", boom)

    with pytest.raises(sqlite3.OperationalError):
        curator.promote(staged.id)

    row = mem.staging.get(staged.id)
    assert row is not None
    # Claim released: not stuck 'promoted' with a dangling fact_id.
    assert row.status == "pending"
    assert row.promoted_fact_id is None
    assert mem.list_facts() == []  # no durable fact was written

    # Re-approval now succeeds once the transient failure clears.
    monkeypatch.undo()
    fact = curator.promote(staged.id)
    assert fact is not None and fact.value == "Alice"
    assert mem.staging.get(staged.id).status == "promoted"


# -- be-memory-2: Turkish stop-words fold, İ tokenizes whole ------------------------


def test_turkish_stopwords_do_not_falsely_group(mem: Memory) -> None:
    from akana.memory.summary_consolidation import SummaryConsolidator

    a = mem.staging.stage(
        FactCandidate(
            key="session:c1",
            value="Kullanıcı proje planı için çok görev istedi",
            extractor="session_closer",
            trust="synthesis",
        ),
        conversation_id="c1",
    )
    b = mem.staging.stage(
        FactCandidate(
            key="session:c2",
            value="Kullanıcı tatil için çok para harcadı",
            extractor="session_closer",
            trust="synthesis",
        ),
        conversation_id="c2",
    )

    calls: list[str] = []

    def fake_summarize(prompt: str) -> str:
        calls.append(prompt)
        return '{"summary": "merged"}'

    cons = SummaryConsolidator(mem, fake_summarize, language="tr")
    result = cons.consolidate()

    # {kullanıcı, için, çok} are stop-words → zero topical overlap → no group.
    assert result == []
    assert calls == [], "summarizer must not be invoked for a non-overlap"
    # Neither session summary was consumed (both still pending).
    pending_ids = {r.id for r in mem.staging.list_pending()}
    assert {a.id, b.id} <= pending_ids


def test_tokens_folds_turkish_dotted_i() -> None:
    from akana.memory.summary_consolidation import _tokens

    toks = _tokens("İstanbul Kadıköy gezisi")
    # 'İ'.lower() emits a combining dot that splits the word; fold_text keeps it whole.
    assert "istanbul" in toks


# -- be-memory-3 / be-memory-5: pending scan window + English status ----------------


def test_pending_matches_surfaces_newest_row(mem: Memory) -> None:
    orch = mem.make_orchestrator()
    for i in range(205):
        mem.staging.stage(FactCandidate(key=f"fact{i}", value=f"deger{i}", extractor="llm"))
    # The just-stated fact is the NEWEST row (ts ASC + 500-row inbox → index > 200).
    target = mem.staging.stage(
        FactCandidate(key="hobisi", value="satranç", extractor="llm")
    )

    matches = orch._pending_matches("hobi", limit=5)
    assert target.id in [m["id"] for m in matches]


def test_pending_status_is_english(mem: Memory) -> None:
    orch = mem.make_orchestrator()
    mem.staging.stage(FactCandidate(key="kedi_adi", value="Pamuk", extractor="llm"))

    matches = orch._pending_matches("kedi", limit=5)
    assert matches, "expected the pending match"
    assert matches[0]["status"] == "pending_approval"
    assert matches[0]["status"] != "onay_bekliyor"


# -- be-memory-4: dedup-hit UPDATE must not downgrade an existing higher-trust fact --


def test_promote_duplicate_does_not_downgrade_trust(mem: Memory, curator: Curator) -> None:
    # A durable user-stated fact (created e.g. via POST /memory/facts).
    mem.assert_fact_direct(key="name", value="Ali Yılmaz", trust="user_statement")
    # memory.remember stages a duplicate as 'inferred' (no dedup against durable facts).
    staged = mem.staging.stage(
        FactCandidate(key="name", value="Ali Yılmaz", trust="inferred")
    )

    fact = curator.promote(staged.id)
    assert fact is not None

    facts = mem.semantic.facts_for_key("name")
    assert len(facts) == 1
    # Upgrade-only ladder: the inferred re-assertion must NOT demote the fact.
    assert facts[0].trust == "user_statement"
    assert facts[0].source_origin == "user_statement"
    assert fact.trust == "user_statement"
