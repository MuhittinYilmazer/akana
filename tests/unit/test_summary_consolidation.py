"""M3.3 SummaryConsolidator: overlapping session summaries → one staged topic
candidate (synthesis) carrying ``source_fact_ids``; non-overlapping left alone; a
failing summarizer swallowed.

Uses a FAKE in-memory staging (no sqlite/driver) + a deterministic FAKE summarizer,
so the grouping heuristic and the staging contract are tested in isolation.
"""

from __future__ import annotations

import pytest

from akana.memory.staging import FactCandidate, StagedFact
from akana.memory.summary_consolidation import SummaryConsolidator, consolidation_key


# -- fakes -----------------------------------------------------------------------


class FakeStaging:
    """In-memory stand-in for ``StagingStore`` — only the surface the consolidator uses.

    ``list_pending`` returns seeded rows (insertion order); ``stage`` records the
    candidate and echoes a :class:`StagedFact` with a synthetic id.
    """

    def __init__(self) -> None:
        self._pending: list[StagedFact] = []
        self.staged: list[FactCandidate] = []
        self.rejected: list[str] = []
        self._seq = 0

    def seed_summary(
        self, *, key: str, value: str, conversation_id: str, sid: str | None = None
    ) -> StagedFact:
        self._seq += 1
        row = StagedFact(
            id=sid or f"seed-{self._seq}",
            ts=f"2026-01-01T00:00:0{self._seq}Z",
            key=key,
            value=value,
            reason="session_closer",
            status="pending",
            trust="synthesis",
            source_turn_id=None,
            quote=None,
            extractor="session_closer",
            conversation_id=conversation_id,
        )
        self._pending.append(row)
        return row

    def list_pending(self, *, limit: int = 50) -> list[StagedFact]:
        # Mirror the real store: 'rejected' rows are no longer pending.
        return [r for r in self._pending if r.id not in self.rejected][:limit]

    def mark_rejected(self, staged_id: str) -> bool:
        if staged_id in self.rejected:
            return False
        self.rejected.append(staged_id)
        return True

    def stage(
        self,
        candidate: FactCandidate,
        *,
        conversation_id: str | None = None,
        staged_id: str | None = None,
    ) -> StagedFact:
        self.staged.append(candidate)
        self._seq += 1
        return StagedFact(
            id=staged_id or f"staged-{self._seq}",
            ts=f"2026-02-01T00:00:0{self._seq}Z",
            key=candidate.key,
            value=candidate.value,
            reason=candidate.reason,
            status="pending",
            trust=candidate.trust,
            source_turn_id=candidate.source_turn_id,
            quote=candidate.quote,
            extractor=candidate.extractor,
            conversation_id=conversation_id,
            source_fact_ids=candidate.source_fact_ids,
        )


class FakeMemory:
    """Minimal façade — only exposes ``.staging``."""

    def __init__(self, staging: FakeStaging) -> None:
        self.staging = staging


class FakeSummarize:
    """Deterministic summarizer; records prompts, returns a fixed JSON merge."""

    def __init__(self, summary: str = "Dentist appointments recur every month.") -> None:
        self.prompts: list[str] = []
        self._summary = summary

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return f'{{"summary": "{self._summary}"}}'


@pytest.fixture()
def staging() -> FakeStaging:
    return FakeStaging()


@pytest.fixture()
def memory(staging: FakeStaging) -> FakeMemory:
    return FakeMemory(staging)


# -- overlapping → one consolidation candidate -----------------------------------


def test_overlapping_summaries_make_one_consolidation_candidate(
    memory: FakeMemory, staging: FakeStaging
) -> None:
    a = staging.seed_summary(
        key="session:conv-aaaa",
        value="Dentist appointment scheduled tomorrow; user prefers morning slots.",
        conversation_id="conv-aaaa",
        sid="sum-a",
    )
    b = staging.seed_summary(
        key="session:conv-bbbb",
        value="Rescheduled the dentist appointment; morning slots discussed again.",
        conversation_id="conv-bbbb",
        sid="sum-b",
    )
    fake = FakeSummarize("Recurring dentist scheduling, morning preferred.")

    out = SummaryConsolidator(memory, fake).consolidate()  # type: ignore[arg-type]

    assert len(out) == 1
    assert len(staging.staged) == 1
    cand = staging.staged[0]
    # source_fact_ids references the merged session-summary ids (dedup-exempt + traceable)
    assert cand.source_fact_ids is not None
    assert set(cand.source_fact_ids) == {a.id, b.id}
    assert cand.trust == "synthesis"
    assert cand.extractor == "summary_consolidation"
    assert cand.key.startswith("topic:") == True  # noqa: E712 - explicit
    assert cand.value == "Recurring dentist scheduling, morning preferred."
    # one model call for the single group
    assert len(fake.prompts) == 1
    assert "dentist" in fake.prompts[0].lower()


def test_sources_consumed_so_next_pass_does_not_restage(
    memory: FakeMemory, staging: FakeStaging
) -> None:
    """D3: after a group is consolidated its source session-summaries are marked rejected
    (consumed). A second pass must find nothing → no re-stage, no extra LLM call. Previously
    the sources stayed pending and the SAME topic was re-staged + re-summarized every hour."""
    a = staging.seed_summary(
        key="session:conv-aaaa",
        value="Dentist appointment scheduled tomorrow; user prefers morning slots.",
        conversation_id="conv-aaaa",
        sid="sum-a",
    )
    b = staging.seed_summary(
        key="session:conv-bbbb",
        value="Rescheduled the dentist appointment; morning slots discussed again.",
        conversation_id="conv-bbbb",
        sid="sum-b",
    )
    fake = FakeSummarize("Recurring dentist scheduling, morning preferred.")
    consolidator = SummaryConsolidator(memory, fake)  # type: ignore[arg-type]

    first = consolidator.consolidate()
    assert len(first) == 1
    assert set(staging.rejected) == {a.id, b.id}  # both sources consumed
    assert len(fake.prompts) == 1

    # Second hourly pass: sources are gone → nothing to do (no re-stage, no new LLM call).
    second = consolidator.consolidate()
    assert second == []
    assert len(staging.staged) == 1  # still just the one from the first pass
    assert len(fake.prompts) == 1  # summarizer NOT called again


def test_consolidation_key_is_deterministic_from_shared_tokens(
    memory: FakeMemory, staging: FakeStaging
) -> None:
    staging.seed_summary(
        key="session:c1",
        value="Project Apollo budget review with finance team.",
        conversation_id="c1",
    )
    staging.seed_summary(
        key="session:c2",
        value="Apollo budget approved by finance after review.",
        conversation_id="c2",
    )
    out = SummaryConsolidator(memory, FakeSummarize()).consolidate()  # type: ignore[arg-type]
    assert len(out) == 1
    # key derives from the sorted shared topical tokens → stable across runs
    assert staging.staged[0].key == consolidation_key(staging.staged[0].key[len("topic:") :])
    assert "apollo" in staging.staged[0].key and "budget" in staging.staged[0].key


# -- non-overlapping → left alone ------------------------------------------------


def test_non_overlapping_summaries_are_left_alone(
    memory: FakeMemory, staging: FakeStaging
) -> None:
    staging.seed_summary(
        key="session:c1",
        value="User booked a flight to Berlin for the conference.",
        conversation_id="c1",
    )
    staging.seed_summary(
        key="session:c2",
        value="Discussed sourdough bread recipe and oven temperature.",
        conversation_id="c2",
    )
    fake = FakeSummarize()

    out = SummaryConsolidator(memory, fake).consolidate()  # type: ignore[arg-type]

    assert out == []
    assert staging.staged == []
    assert fake.prompts == []  # no group → summarizer never called


def test_same_conversation_overlap_is_not_cross_session(
    memory: FakeMemory, staging: FakeStaging
) -> None:
    # Two summaries from the SAME conversation overlap heavily, but that is not
    # cross-session — must not be consolidated.
    staging.seed_summary(
        key="session:c1",
        value="Tax filing deadline and document checklist reviewed.",
        conversation_id="c1",
    )
    staging.seed_summary(
        key="session:c1:karar:1",
        value="Tax filing checklist finalized; deadline confirmed.",
        conversation_id="c1",
    )
    out = SummaryConsolidator(memory, FakeSummarize()).consolidate()  # type: ignore[arg-type]
    assert out == []
    assert staging.staged == []


def test_consolidator_output_is_not_re_consolidated(
    memory: FakeMemory, staging: FakeStaging
) -> None:
    # A prior consolidation candidate (extractor='summary_consolidation') sitting in
    # the inbox must be ignored by a later pass (only raw session summaries are input).
    staging._pending.append(
        StagedFact(
            id="topic-old",
            ts="2026-01-01T00:00:00Z",
            key="topic:dentist",
            value="Recurring dentist topic.",
            reason="summary_consolidation",
            status="pending",
            trust="synthesis",
            source_turn_id=None,
            quote=None,
            extractor="summary_consolidation",
            conversation_id="conv-aaaa",
            source_fact_ids=("x", "y"),
        )
    )
    staging.seed_summary(
        key="session:c2", value="A one-off unrelated note.", conversation_id="c2"
    )
    out = SummaryConsolidator(memory, FakeSummarize()).consolidate()  # type: ignore[arg-type]
    assert out == []
    assert staging.staged == []


# -- failing summarizer is swallowed ---------------------------------------------


def test_failing_summarizer_is_swallowed(
    memory: FakeMemory, staging: FakeStaging
) -> None:
    staging.seed_summary(
        key="session:c1",
        value="Quarterly sales report figures discussed in detail.",
        conversation_id="c1",
    )
    staging.seed_summary(
        key="session:c2",
        value="Sales report quarterly figures revised upward.",
        conversation_id="c2",
    )

    def boom(_prompt: str) -> str:
        raise RuntimeError("LLM exploded")

    out = SummaryConsolidator(memory, boom).consolidate()  # type: ignore[arg-type]

    assert out == []  # never raises into the caller
    assert staging.staged == []  # nothing staged on failure


def test_empty_merge_stages_nothing(memory: FakeMemory, staging: FakeStaging) -> None:
    staging.seed_summary(
        key="session:c1",
        value="Gym membership renewal options compared.",
        conversation_id="c1",
    )
    staging.seed_summary(
        key="session:c2",
        value="Membership renewal at the gym confirmed.",
        conversation_id="c2",
    )
    # Summarizer returns an empty summary → no topic worth keeping.
    out = SummaryConsolidator(memory, FakeSummarize("")).consolidate()  # type: ignore[arg-type]
    assert out == []
    assert staging.staged == []


def test_service_run_once_respects_session_summary_toggle(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Memory Studio 'session summarization' toggle also gates cross-session
    consolidation: OFF → the pass short-circuits before touching memory/LLM at all."""
    import asyncio

    from akana.memory.settings import MemorySettings, save_memory_settings
    from akana_server.config import load_settings
    from akana_server.orchestrator import summary_consolidation_service as svc

    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("CURSOR_API_KEY", "test-key")
    save_memory_settings(tmp_path, MemorySettings(session_summary=False))

    def _boom(_data_dir):  # get_memory_core must NOT be reached when the toggle is OFF
        raise AssertionError("consolidation ran despite session_summary OFF")

    monkeypatch.setattr(svc, "get_memory_core", _boom)

    assert asyncio.run(svc.run_once(load_settings())) == 0
