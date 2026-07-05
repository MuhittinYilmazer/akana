"""M3.2 SessionCloser: summary -> staging (synthesis), idempotency, idle scan.

The summary is now a SINGLE plain paragraph: close() stages a conversation as a
single ``synthesis`` candidate (``session:<cid12>``); the same paragraph is stored
under ``last_summary_struct`` for rolling reads + consumers.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from akana.memory import (
    Memory,
    SessionCloser,
    SummaryView,
    find_idle_conversations,
    get_session_summary,
)


@pytest.fixture()
def mem(tmp_path: Path) -> Memory:
    return Memory.for_data_dir(tmp_path)


class FakeSummarize:
    """Fake LLM that records prompts; returns the given replies in order."""

    def __init__(self, *replies: str) -> None:
        self.prompts: list[str] = []
        self._replies = list(replies) or ["Kullanıcı kahve sever. Toplantı yarın."]

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self._replies[min(len(self.prompts), len(self._replies)) - 1]


def _seed_session(mem: Memory, cid: str = "c1") -> None:
    """4 user/assistant turns (min_turns threshold) + 1 tool turn (excluded from summary)."""
    mem.remember_turn(role="user", conversation_id=cid, text="yarın diş randevum var")
    mem.remember_turn(role="assistant", conversation_id=cid, text="not ettim")
    mem.remember_turn(conversation_id=cid, role="tool", text="GIZLI-TOOL-CIKTISI")
    mem.remember_turn(role="user", conversation_id=cid, text="bir de süt almayı unutmayalım")
    mem.remember_turn(role="assistant", conversation_id=cid, text="listene ekledim")


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# -- close: summary lands in staging ---------------------------------------------


def test_close_stages_single_paragraph_summary(mem: Memory) -> None:
    fake = FakeSummarize("Diş randevusu yarın. Süt alınacak. Kullanıcı ev işleriyle ilgilendi.")
    _seed_session(mem, "conv-12345678901234")
    closer = SessionCloser(mem, fake)

    staged = closer.close("conv-12345678901234")
    # SINGLE paragraph -> SINGLE synthesis candidate
    assert len(staged) == 1
    s0 = staged[0]
    assert s0.key == "session:conv-1234567"  # cid[:12]
    assert s0.value.startswith("Diş randevusu")
    assert s0.trust == "synthesis"
    assert s0.status == "pending"  # K30: no automatic durable write
    assert s0.reason == "session_closer"
    assert s0.extractor == "session_closer"
    assert s0.conversation_id == "conv-12345678901234"
    assert [s.id for s in mem.staging.list_pending()] == [s0.id]

    # prompt: plain-paragraph template + chronological transcript; tool turns excluded
    assert len(fake.prompts) == 1
    prompt = fake.prompts[0]
    assert prompt.startswith("WRITE")  # default EN, paragraph prompt
    assert "diş randevum" in prompt and "süt almayı" in prompt
    assert "GIZLI-TOOL-CIKTISI" not in prompt
    assert prompt.index("diş randevum") < prompt.index("süt almayı")  # chronological

    # metadata: last_summary_at + last user-turn id + paragraph payload
    meta = mem.conversations_meta.get_json_metadata("conv-12345678901234")
    assert meta["last_summary_at"]
    assert meta["last_summary_struct"] == {"summary": s0.value}
    user_turns = [t for t in mem.recent_turns("conv-12345678901234") if t.role == "user"]
    assert meta["last_summary_turn_id"] == user_turns[-1].id
    assert s0.source_turn_id == user_turns[-1].id


def test_close_skips_stage_if_deleted_during_summarization(mem: Memory) -> None:
    """b19: a soft_delete that lands WHILE the summarizer runs must NOT leave a zombie summary —
    the deleted flag is re-read right before staging (it was only checked before the LLM call)."""
    cid = "conv-del-midsummary0"
    mem.conversations_meta.ensure(cid)
    _seed_session(mem, cid)

    class _DeletingSummarize:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, prompt: str) -> str:
            self.calls += 1
            # Simulate a concurrent soft_delete DURING the (long) summarize call.
            mem.conversations_meta.merge_json_metadata(cid, {"deleted": True})
            return "Silinme sırasında üretilen özet."

    fake = _DeletingSummarize()
    staged = SessionCloser(mem, fake).close(cid)
    assert fake.calls == 1  # the summarizer DID run (delete landed mid-flight)
    assert staged == [], "must not stage a summary for a conversation deleted mid-summarization"
    assert mem.staging.list_pending() == []


def test_close_plain_text_is_staged_verbatim(mem: Memory) -> None:
    """Plain text (not JSON/markdown) is staged verbatim as a single-paragraph candidate."""
    cid = "conv-plaintext00"
    _seed_session(mem, cid)
    staged = SessionCloser(mem, FakeSummarize("Bu düz metin, tek paragraf.")).close(cid)
    assert len(staged) == 1
    assert staged[0].key == f"session:{cid[:12]}"
    assert staged[0].value == "Bu düz metin, tek paragraf."


def test_close_json_reply_collapses_to_paragraph(mem: Memory) -> None:
    """If the model still returns JSON, clean_summary_text extracts the 'summary'/'ozet'
    field -> single-paragraph candidate (structure does not leak, no stacked keys)."""
    cid = "conv-jsonreply001"
    reply = '{"ozet": "Diş ve süt konuşuldu.", "kararlar": ["x"], "acik_isler": ["y"]}'
    _seed_session(mem, cid)
    staged = SessionCloser(mem, FakeSummarize(reply)).close(cid)
    assert len(staged) == 1
    assert staged[0].key == f"session:{cid[:12]}"
    assert staged[0].value == "Diş ve süt konuşuldu."  # only the 'ozet' field extracted
    # sub-keys (decision/item/follow-up) are NOT produced
    assert not any(":karar:" in s.key or ":is:" in s.key or ":takip:" in s.key for s in staged)


def test_close_strips_code_fences(mem: Memory) -> None:
    """A fenced code block (```...```) is stripped -> the inner paragraph is staged."""
    cid = "conv-fenced000001"
    _seed_session(mem, cid)
    staged = SessionCloser(mem, FakeSummarize("```\nDüz paragraf özet.\n```")).close(cid)
    assert len(staged) == 1
    assert staged[0].value == "Düz paragraf özet."


def test_close_below_min_turns_returns_none(mem: Memory) -> None:
    fake = FakeSummarize()
    mem.remember_turn(role="user", conversation_id="c1", text="selam")
    mem.remember_turn(role="assistant", conversation_id="c1", text="merhaba")
    assert SessionCloser(mem, fake).close("c1") == []
    assert SessionCloser(mem, fake).close("yok-boyle-konusma") == []
    assert mem.staging.count_pending() == 0
    assert fake.prompts == []  # LLM never called


def test_close_without_user_turn_returns_none(mem: Memory) -> None:
    fake = FakeSummarize()
    for i in range(4):
        mem.remember_turn(role="assistant", conversation_id="c1", text=f"monolog {i}")
    assert SessionCloser(mem, fake, min_turns=2).close("c1") == []
    assert fake.prompts == []


def test_close_chunks_long_session_covers_all(mem: Memory) -> None:
    """A long chat (exceeds max_chars) is CHUNKED -> the head is NOT LOST (the old
    behavior only kept the tail). Each chunk is summarized separately (WRITE), then
    combined into one paragraph via an LLM reduce (MERGE); the head (ESKI) and tail
    (YENI) chunk prompts are SEPARATE."""
    fake = FakeSummarize()
    mem.remember_turn(role="user", conversation_id="c1", text="ESKI-" + "a" * 300)
    mem.remember_turn(role="assistant", conversation_id="c1", text="tamam")
    mem.remember_turn(role="user", conversation_id="c1", text="YENI-soru")
    mem.remember_turn(role="assistant", conversation_id="c1", text="YENI-cevap")
    assert SessionCloser(mem, fake, max_chars=200).close("c1")
    # two chunks -> two WRITE calls + one reduce (MERGE) call = 3 prompts
    assert len(fake.prompts) == 3
    write_prompts = [p for p in fake.prompts if p.startswith("WRITE")]
    assert len(write_prompts) == 2  # each chunk written separately
    joined = " ".join(write_prompts)
    assert "ESKI-" in joined  # the head is now covered (previously dropped)
    assert "YENI-soru" in joined and "YENI-cevap" in joined
    # last prompt is the reduce/MERGE pass (reconciles both partial paragraphs)
    assert fake.prompts[-1].startswith("MERGE")


def test_close_merges_chunk_summaries_with_llm_reduce(mem: Memory) -> None:
    """Multi-chunk paragraph summaries are reconciled into one paragraph via an LLM
    reduce (MERGE) pass instead of naive concatenation. Both partial paragraphs are
    fed to the reduce prompt; the single paragraph the reduce returns is staged."""
    r1 = "Bölüm bir: A kararı alındı."
    r2 = "Bölüm iki: B kararı da eklendi."
    reduced = "Birleşik özet: A ve B kararları alındı."
    fake = FakeSummarize(r1, r2, reduced)
    mem.remember_turn(role="user", conversation_id="c1", text="ESKI-" + "a" * 300)
    mem.remember_turn(role="assistant", conversation_id="c1", text="orta uzunlukta bir yanıt burada")
    mem.remember_turn(role="user", conversation_id="c1", text="YENI-soru burada da var")
    mem.remember_turn(role="assistant", conversation_id="c1", text="YENI-cevap burada da var")
    staged = SessionCloser(mem, fake, max_chars=200).close("c1")
    assert len(fake.prompts) == 3  # two chunks + one reduce
    reduce_prompt = fake.prompts[-1]
    assert reduce_prompt.startswith("MERGE")
    # the reduce takes both partial paragraphs as input
    assert "Bölüm bir" in reduce_prompt and "Bölüm iki" in reduce_prompt
    # the reduce output is staged as a SINGLE candidate
    assert len(staged) == 1
    assert staged[0].value == "Birleşik özet: A ve B kararları alındı."


# -- idempotency -------------------------------------------------------------------


def test_close_is_idempotent_until_new_user_turn(mem: Memory) -> None:
    fake = FakeSummarize("Birinci özet.", "İkinci özet.")
    _seed_session(mem)
    closer = SessionCloser(mem, fake)

    first = closer.close("c1")
    assert first  # not an empty list
    assert closer.close("c1") == []  # same content is not staged again
    assert len(fake.prompts) == 1  # LLM not called a second time
    assert mem.staging.count_pending() == 1

    # runs again after a new user turn -> REPLACES the OLD summary
    mem.remember_turn(role="user", conversation_id="c1", text="bir konu daha: pasaport yenileme")
    second = closer.close("c1")
    assert second and second[0].id != first[0].id
    assert second[0].value == "İkinci özet."
    assert mem.staging.count_pending() == 1  # old rejected, new arrived (does not accumulate)


def test_close_resummary_rejects_stale_pending(mem: Memory) -> None:
    """Re-summary: the previous PENDING session candidate is rejected (replace; inbox does not accumulate)."""
    closer = SessionCloser(mem, FakeSummarize("İlk özet.", "Yeni özet."))
    _seed_session(mem, "c1")
    first = closer.close("c1")
    assert len(first) == 1
    assert mem.staging.count_pending() == 1

    mem.remember_turn(role="user", conversation_id="c1", text="yeni bir gelişme oldu burada")
    second = closer.close("c1")
    assert len(second) == 1
    # old PENDING rejected; new one staged -> pending=1 (did not accumulate)
    assert mem.staging.count_pending() == 1
    assert {s.value for s in mem.staging.list_pending()} == {"Yeni özet."}


def test_close_resummary_keeps_promoted(mem: Memory) -> None:
    """An old summary the user APPROVED (promote) is not pending -> re-summary does not
    touch it; the new summary is staged in addition."""
    closer = SessionCloser(mem, FakeSummarize("İlk özet.", "Yeni özet."))
    _seed_session(mem, "c1")
    first = closer.close("c1")
    mem.staging.mark_promoted(first[0].id, "fact-xyz")  # user approved the summary
    assert mem.staging.count_pending() == 0

    mem.remember_turn(role="user", conversation_id="c1", text="yeni bir gelişme oldu burada")
    second = closer.close("c1")
    assert len(second) == 1
    assert mem.staging.count_pending() == 1  # only the new summary is pending
    assert {s.value for s in mem.staging.list_pending()} == {"Yeni özet."}


def test_close_swallows_summarize_errors(mem: Memory) -> None:
    calls: list[str] = []

    def boom(prompt: str) -> str:
        calls.append(prompt)
        raise RuntimeError("LLM down")

    _seed_session(mem)
    closer = SessionCloser(mem, boom)
    assert closer.close("c1") == []  # never raises
    assert calls  # it tried but was swallowed
    assert mem.staging.count_pending() == 0
    # metadata not updated -> can be retried on the next cron run
    assert "last_summary_turn_id" not in mem.conversations_meta.get_json_metadata("c1")
    assert SessionCloser(mem, FakeSummarize("Geç gelen özet.")).close("c1")


def test_close_empty_summary_is_noop(mem: Memory) -> None:
    _seed_session(mem)
    assert SessionCloser(mem, FakeSummarize("   ")).close("c1") == []
    assert mem.staging.count_pending() == 0


# -- find_idle_conversations ---------------------------------------------------------


def test_find_idle_conversations_threshold_and_staleness(mem: Memory) -> None:
    now = datetime.now(UTC)
    old, fresh = _iso(now - timedelta(hours=2)), _iso(now - timedelta(minutes=1))

    # idle + not summarized -> candidate
    mem.episodic.append_turn(turn_id="u-old", conversation_id="c-old", role="user",
                             text="eski konu", ts=old)
    # fresh -> not a candidate
    mem.episodic.append_turn(turn_id="u-fresh", conversation_id="c-fresh", role="user",
                             text="yeni konu", ts=fresh)
    # idle but summary is up to date -> not a candidate
    mem.episodic.append_turn(turn_id="u-done", conversation_id="c-done", role="user",
                             text="bitmiş konu", ts=old)
    mem.conversations_meta.merge_json_metadata("c-done", {"last_summary_turn_id": "u-done"})
    # idle but no user turn at all -> not a candidate (close would be a no-op anyway)
    mem.episodic.append_turn(turn_id="a-solo", conversation_id="c-noUser", role="assistant",
                             text="monolog", ts=old)

    assert find_idle_conversations(mem, idle_minutes=30) == ["c-old"]
    # wide threshold -> the fresh conversation is included too (together with limit)
    wide = find_idle_conversations(mem, idle_minutes=0, limit=2)
    assert wide == ["c-fresh", "c-old"]  # last_ts DESC, cut off at limit=2


def test_find_idle_skips_summarized_after_close(mem: Memory) -> None:
    now = datetime.now(UTC)
    base = now - timedelta(hours=3)
    for i, (role, text) in enumerate(
        [("user", "plan yapalım"), ("assistant", "olur"),
         ("user", "yarın 9'da"), ("assistant", "ayarladım")]
    ):
        mem.episodic.append_turn(
            turn_id=f"t{i}", conversation_id="c1", role=role,  # type: ignore[arg-type]
            text=text, ts=_iso(base + timedelta(seconds=i)),
        )
    assert find_idle_conversations(mem) == ["c1"]
    assert SessionCloser(mem, FakeSummarize("Yarın 9'da plan var.")).close("c1")
    assert find_idle_conversations(mem) == []  # summary is up to date; cron does not pick it again


def test_find_idle_turn_threshold_catches_long_active_chat(mem: Memory) -> None:
    now = datetime.now(UTC)
    fresh = now - timedelta(minutes=1)  # NOT idle
    # 6 fresh turns (3 user + 3 assistant), never summarized → still ACTIVE
    for i, role in enumerate(["user", "assistant"] * 3):
        mem.episodic.append_turn(
            turn_id=f"L{i}", conversation_id="c-long", role=role,  # type: ignore[arg-type]
            text=f"turn {i}", ts=_iso(fresh + timedelta(seconds=i)),
        )
    # turn-count trigger disabled (default 0) + not idle → NOT picked
    assert find_idle_conversations(mem, idle_minutes=30) == []
    # turn_threshold reached (6 new turns) → picked EARLY despite being active
    assert find_idle_conversations(mem, idle_minutes=30, turn_threshold=6) == ["c-long"]
    # threshold higher than accumulated turns → still waits
    assert find_idle_conversations(mem, idle_minutes=30, turn_threshold=20) == []


def test_find_idle_skips_soft_deleted(mem: Memory) -> None:
    """#11: a soft-deleted conversation (json_metadata.deleted) is not picked by cron.

    The turns stay in episodic (soft delete does not remove them) but the user deleted
    the conversation -> even if ``memory.conversations`` (turn-based) returns it, find_idle
    filters it out.
    """
    old = _iso(datetime.now(UTC) - timedelta(hours=2))
    mem.episodic.append_turn(turn_id="u-del", conversation_id="c-del", role="user",
                             text="silinecek konu", ts=old)
    assert find_idle_conversations(mem, idle_minutes=30) == ["c-del"]  # candidate before deletion
    mem.conversations_meta.merge_json_metadata("c-del", {"deleted": True})
    assert find_idle_conversations(mem, idle_minutes=30) == []  # deleted -> no zombie


def test_close_skips_soft_deleted(mem: Memory) -> None:
    """Defensive: even if close() is called directly, it does not stage a deleted conversation."""
    _seed_session(mem, "c-del-close")
    mem.conversations_meta.merge_json_metadata("c-del-close", {"deleted": True})
    fake = FakeSummarize("olmamalı")
    assert SessionCloser(mem, fake).close("c-del-close") == []
    assert fake.prompts == []  # the summarizer was never reached
    assert mem.staging.count_pending() == 0  # no candidate was staged


# -- get_session_summary (consumer API) ----------------------------------------


def test_get_session_summary_roundtrips_paragraph(mem: Memory) -> None:
    """close() writes the paragraph to last_summary_struct; get_session_summary returns
    it as a SummaryView (the stable API consumers read)."""
    cid = "conv-getsummary01"
    assert get_session_summary(mem, cid) is None  # None when there is no summary
    _seed_session(mem, cid)
    SessionCloser(mem, FakeSummarize("Bir plan yapıldı; takvim paylaşılacak.")).close(cid)

    view = get_session_summary(mem, cid)
    assert isinstance(view, SummaryView)
    assert view.conversation_id == cid
    assert view.summary == "Bir plan yapıldı; takvim paylaşılacak."
    assert view.updated_at  # last_summary_at was carried over
    assert not view.is_empty


def test_get_session_summary_empty_struct_is_none(mem: Memory) -> None:
    """Empty struct (no durable content) -> get_session_summary returns None."""
    cid = "conv-emptystruct1"
    mem.conversations_meta.merge_json_metadata(
        cid,
        {"last_summary_struct": SummaryView(conversation_id=cid).to_payload()},
    )
    assert get_session_summary(mem, cid) is None


def test_get_session_summary_reads_legacy_v2_struct(mem: Memory) -> None:
    """Backward-compatible: the old v2 (title/decisions/... multi-field) struct still
    loads; only the 'summary' field is read, old keys are ignored (no migration needed)."""
    cid = "conv-legacyv2001"
    mem.conversations_meta.merge_json_metadata(
        cid,
        {
            "last_summary_at": "2026-01-01T00:00:00.000Z",
            "last_summary_struct": {
                "title": "Eski başlık",
                "summary": "Eski paragraf özeti.",
                "decisions": ["k1"],
                "open_items": ["i1"],
                "entities": ["e1"],
                "follow_ups": ["f1"],
            },
        },
    )
    view = get_session_summary(mem, cid)
    assert view is not None
    assert view.summary == "Eski paragraf özeti."
    assert not view.is_empty


# -- rolling/incremental summary (A) -------------------------------------------


def test_close_rolling_reconciles_prior_summary(mem: Memory) -> None:
    """The second summary is ROLLING: the previous paragraph + ONLY the new turns are
    fed to the update prompt (the whole history is not re-summarized)."""
    first_reply = "Diş randevusu planlandı: yarın 9'da dişçiye gidilecek."
    rolled_reply = "Randevu iptal edildi; yeni randevu alınacak."
    fake = FakeSummarize(first_reply, rolled_reply)
    cid = "conv-rolling00001"
    _seed_session(mem, cid)
    closer = SessionCloser(mem, fake)

    first = closer.close(cid)
    assert len(first) == 1 and "yarın 9'da" in first[0].value
    assert len(fake.prompts) == 1
    assert fake.prompts[0].startswith("WRITE")  # cold start = full pass

    # a new user turn -> the second close becomes rolling
    mem.remember_turn(role="user", conversation_id=cid, text="aslında o randevuyu iptal edelim")
    mem.remember_turn(role="assistant", conversation_id=cid, text="tamam iptal ettim")
    second = closer.close(cid)

    assert len(fake.prompts) == 2
    rolling_prompt = fake.prompts[1]
    assert rolling_prompt.startswith("UPDATE")  # reconcile prompt
    # the previous summary paragraph is embedded in the prompt
    assert "yarın 9'da dişçiye gidilecek" in rolling_prompt
    # ONLY the new turns are in the transcript; NOT the old seed turns
    assert "iptal edelim" in rolling_prompt
    assert "yarın diş randevum var" not in rolling_prompt  # old turns not sent
    # the reconcile result is staged: a single new paragraph
    assert len(second) == 1
    assert second[0].value == rolled_reply
    # struct updated
    view = get_session_summary(mem, cid)
    assert view is not None and view.summary == rolled_reply


def test_close_first_pass_is_full_when_no_prior_struct(mem: Memory) -> None:
    """The first summary (no struct) is a FULL pass -- WRITE when there is no prior summary."""
    _seed_session(mem, "conv-coldstart001")
    fake = FakeSummarize("İlk geçiş özeti.")
    SessionCloser(mem, fake).close("conv-coldstart001")
    assert len(fake.prompts) == 1
    assert fake.prompts[0].startswith("WRITE")
    assert "UPDATE" not in fake.prompts[0]


def test_close_rolling_falls_back_to_full_when_anchor_rotated_out(mem: Memory) -> None:
    """If the anchor has dropped out of the fetch window (struct exists but turn_id is
    not among the turns), rolling is impossible -> falls back to a FULL pass (the head is not lost)."""
    cid = "conv-anchorgone01"
    _seed_session(mem, cid)
    # place a struct + a MISSING anchor (this id is not among the turns)
    mem.conversations_meta.merge_json_metadata(
        cid,
        {
            "last_summary_turn_id": "ROTATED-OUT-ID",
            "last_summary_struct": {"summary": "Eski özet."},
        },
    )
    fake = FakeSummarize("Tam yeniden özet.")
    staged = SessionCloser(mem, fake).close(cid)
    assert staged
    assert len(fake.prompts) == 1
    assert fake.prompts[0].startswith("WRITE")  # not rolling, full pass
    # the whole transcript (including the old seed) was sent
    assert "yarın diş randevum var" in fake.prompts[0]


# -- smart trigger (F): char/token threshold -----------------------------------


def test_find_idle_char_threshold_catches_dense_active_chat(mem: Memory) -> None:
    """char_threshold: few but DENSE turns trigger without being idle (turn-count is
    unaware of content)."""
    now = datetime.now(UTC)
    fresh = now - timedelta(minutes=1)  # NOT idle
    # 4 turns but dense: ~2400 chars of new content (does not exceed turn_threshold=20)
    for i, role in enumerate(["user", "assistant", "user", "assistant"]):
        mem.episodic.append_turn(
            turn_id=f"D{i}", conversation_id="c-dense", role=role,  # type: ignore[arg-type]
            text="x" * 600, ts=_iso(fresh + timedelta(seconds=i)),
        )
    # both content triggers off + not idle -> not picked
    assert find_idle_conversations(mem, idle_minutes=30) == []
    # turn-count stalls at the high threshold (4 < 20) but the char threshold (2000) catches the density
    assert find_idle_conversations(mem, idle_minutes=30, turn_threshold=20) == []
    assert (
        find_idle_conversations(mem, idle_minutes=30, turn_threshold=20, char_threshold=2000)
        == ["c-dense"]
    )
    # if the char threshold is higher than the accumulated content it still waits
    assert find_idle_conversations(mem, idle_minutes=30, char_threshold=100_000) == []


def test_find_idle_char_threshold_counts_only_new_content(mem: Memory) -> None:
    """char_threshold counts only the content AFTER the last summary (rolling anchor)."""
    now = datetime.now(UTC)
    fresh = now - timedelta(minutes=1)
    for i, role in enumerate(["user", "assistant", "user", "assistant"]):
        mem.episodic.append_turn(
            turn_id=f"N{i}", conversation_id="c-new", role=role,  # type: ignore[arg-type]
            text="y" * 600, ts=_iso(fresh + timedelta(seconds=i)),
        )
    # the last user turn (N2) is already summarized; no new user turn after N2 ->
    # eliminated at the staleness gate (without looking at the content threshold)
    mem.conversations_meta.merge_json_metadata("c-new", {"last_summary_turn_id": "N2"})
    assert find_idle_conversations(mem, idle_minutes=30, char_threshold=500) == []
    # add a new user turn: content after N2 = N3(600) + N4(600) = 1200 chars
    mem.episodic.append_turn(turn_id="N4", conversation_id="c-new", role="user",
                             text="z" * 600, ts=_iso(fresh + timedelta(seconds=5)))
    assert find_idle_conversations(mem, idle_minutes=30, char_threshold=500) == ["c-new"]  # 1200>=500
    # threshold 5000 -> 1200 < 5000, still waits (proof that old N0/N1 content is not counted)
    assert find_idle_conversations(mem, idle_minutes=30, char_threshold=5000) == []


def test_summary_prompt_is_bilingual() -> None:
    """The summary prompts + role labels the model reads follow the language (default EN).

    Plain-prose paragraph design: no JSON/schema keys appear in the prompt; the EN
    heads use stable sentinel verbs (WRITE/UPDATE/MERGE) the tests anchor on."""
    from akana.memory.session_closer import (
        _PROMPT_HEADS,
        _PROMPT_REDUCE,
        _PROMPT_ROLLING,
        _ROLE_LABELS,
    )

    assert _PROMPT_HEADS["en"].startswith("WRITE")
    assert "paragraph" in _PROMPT_HEADS["en"] and "no JSON" in _PROMPT_HEADS["en"]
    assert _PROMPT_HEADS["tr"].startswith("Aşağıdaki")
    # rolling + reduce prompts are bilingual too (model-facing)
    assert _PROMPT_ROLLING["en"].startswith("UPDATE") and _PROMPT_ROLLING["tr"].startswith("Bir")
    assert _PROMPT_REDUCE["en"].startswith("MERGE") and _PROMPT_REDUCE["tr"].startswith("Aşağıdaki")
    assert _ROLE_LABELS["en"]["user"] == "User"
    assert _ROLE_LABELS["tr"]["user"] == "Kullanıcı"
