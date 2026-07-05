"""SessionCloser — end-of-session summaries into the staging inbox (M3.2).

When a conversation goes quiet, a single prose paragraph summarising the whole
conversation is produced and *staged* as ONE ``trust="synthesis"`` candidate —
never written to durable semantic memory automatically (K30 ``inbox_only``): the
user promotes or rejects it from the inbox like any other candidate.

The LLM is **injected**, not owned: ``SessionCloser(memory, summarize)`` takes
any ``Callable[[str], str]`` (the server wires Cursor/Claude later; tests pass
a fake). A failing summarizer logs and yields ``[]`` — closing a session must
never raise into the caller.

Idempotency rides on conversation metadata: the last *user* turn id is stored
as ``last_summary_turn_id`` (plus ``last_summary_at`` and the summary paragraph
under ``last_summary_struct``). Closing again without a new user turn is a no-op,
so a cron loop calling :func:`find_idle_conversations` +
:meth:`SessionCloser.close` cannot flood the inbox with duplicate summaries.

**Rolling/incremental.** When a previous summary exists, the closer feeds
``previous_summary + ONLY the new turns since last_summary_turn_id`` to the model
with an *update* prompt and gets back the rewritten whole paragraph (a reversed
decision is dropped, not appended). This avoids re-summarizing the entire history
every trigger (O(n²) tokens) and fixes cross-chunk incoherence. The first-ever
summary — or one whose anchor rotated out of the fetch window — falls back to a
full multi-chunk pass with an LLM *reduce* merge.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from akana.memory._time import iso_now
from akana.memory.staging import FactCandidate, StagedFact
from akana.memory.summary_types import SummaryView, clean_summary_text

if TYPE_CHECKING:
    from akana.memory import Memory
    from akana.memory.conversations import ConversationStore
    from akana.memory.episodic import EpisodicTurn

__all__ = ["SessionCloser", "find_idle_conversations", "get_session_summary"]

log = logging.getLogger(__name__)

#: Metadata keys on the conversation row (``json_metadata``).
_META_LAST_SUMMARY_AT = "last_summary_at"
_META_LAST_SUMMARY_TURN_ID = "last_summary_turn_id"
#: The summary paragraph payload (``SummaryView.to_payload()`` → ``{"summary": ...}``).
#: This is BOTH the rolling-read source (the "summary so far" fed back into the update
#: prompt) AND the consumer read source (``get_session_summary`` / the context assembler).
_META_LAST_SUMMARY_STRUCT = "last_summary_struct"

#: How many turns to pull per conversation (episodic store caps at 1000).
_TURN_FETCH_LIMIT = 1000
#: How many conversations to scan when looking for idle ones (store caps at 200).
_IDLE_SCAN_LIMIT = 200

# The model reads the summary prompt + role-labelled transcript, so both are
# BILINGUAL and follow the ``language`` setting.
_ROLE_LABELS = {
    "en": {"user": "User", "assistant": "Akana"},
    "tr": {"user": "Kullanıcı", "assistant": "Akana"},
}

# Single-paragraph summary. The full-pass prompt WRITES a fresh paragraph from a
# transcript; the rolling prompt UPDATEs a prior paragraph with only the new
# turns; the reduce prompt MERGEs several chunk paragraphs into one. All three
# share the "empty if nothing durable" gate and demand PLAIN PROSE — no JSON,
# markdown, bullets, or headings — so the staged summary is one flowing paragraph.
# Bilingual (``language``-keyed): the model reads the prompt + role-labelled
# transcript, both following the ``language`` setting.

#: Full pass: write a fresh paragraph from scratch (cold start / no prior summary).
_PROMPT_HEADS = {
    "en": (
        "WRITE a single, rich paragraph summarising the conversation below for "
        "long-term memory. Plain prose only — no JSON, no markdown, no bullet "
        "lists, no headings. In ONE flowing paragraph capture the durable "
        "substance: what the topic was, the decisions made, the open items and "
        "next actions, and the salient names, dates and numbers. PRESERVE concrete "
        "details. Do NOT add greetings, code, or speculation. If the conversation "
        "has NOTHING worth long-term memory (only greetings/small talk, or a "
        "transient question already answered), return an EMPTY string — never "
        "invent a summary just to fill the space.\n\nConversation:\n"
    ),
    "tr": (
        "Aşağıdaki konuşmayı uzun süreli hafıza için TEK, zengin bir paragraf "
        "olarak özetle. Yalnızca düz metin — JSON, markdown, madde imi, başlık "
        "YOK. TEK akıcı bir paragrafta kalıcı özü yakala: konu neydi, hangi "
        "kararlar alındı, açık kalan işler ve sonraki adımlar, önemli isim, tarih "
        "ve sayılar. Somut detayları KORU. Selamlaşma, kod, spekülasyon EKLEME. "
        "Sohbette uzun süreli hafızaya değer KALICI bir şey yoksa (sadece "
        "selamlaşma/muhabbet ya da o an cevaplanmış geçici soru), BOŞ metin "
        "döndür — alanı doldurmak için özet UYDURMA.\n\nKonuşma:\n"
    ),
}

#: Rolling pass: rewrite the WHOLE paragraph from the prior summary + only the new
#: turns. A contradiction (a reversed/cancelled decision) is removed, not appended.
_PROMPT_ROLLING = {
    "en": (
        "UPDATE the running paragraph summary of a conversation for long-term "
        "memory. You are given the SUMMARY SO FAR and ONLY the NEW turns since "
        "then. Rewrite the WHOLE summary as ONE flowing plain-prose paragraph — no "
        "JSON, markdown, bullets, or headings. RECONCILE contradictions: if a new "
        "turn reverses or cancels something in the summary, drop or change it — do "
        "not keep both. Carry forward everything still valid and fold in what the "
        "new turns add. PRESERVE concrete details. If nothing durable remains, "
        "return an EMPTY string.\n\nSUMMARY SO FAR:\n"
    ),
    "tr": (
        "Bir konuşmanın yürüyen paragraf özetini uzun süreli hafıza için GÜNCELLE. "
        "Sana ŞİMDİYE KADARKİ ÖZET ve sadece o andan sonraki YENİ turlar veriliyor. "
        "Tüm özeti TEK akıcı düz-metin paragraf olarak yeniden yaz — JSON, "
        "markdown, madde imi, başlık YOK. ÇELİŞKİLERİ UZLAŞTIR: bir yeni tur "
        "özetteki bir şeyi tersine çevirir veya iptal ederse, onu kaldır ya da "
        "değiştir — ikisini birden tutma. Hâlâ geçerli olan her şeyi taşı; yeni "
        "turların eklediğini birleştir. Somut detayları KORU. Geriye kalıcı bir şey "
        "kalmıyorsa BOŞ metin döndür.\n\nŞİMDİYE KADARKİ ÖZET:\n"
    ),
}

_ROLLING_NEW_TURNS_LABEL = {
    "en": "\n\nNEW turns since then:\n",
    "tr": "\n\nO andan sonraki YENİ turlar:\n",
}

#: Reduce pass: merge several chunk paragraphs (consecutive slices of one
#: conversation) into one paragraph (cold-start multi-chunk path — rolling makes
#: this rare).
_PROMPT_REDUCE = {
    "en": (
        "MERGE the partial paragraph summaries below (each covers a CONSECUTIVE "
        "slice of one conversation) into ONE coherent plain-prose paragraph for "
        "long-term memory — no JSON, markdown, bullets, or headings. DEDUPE "
        "repeated points and RECONCILE contradictions (a later part that reverses "
        "an earlier point wins). PRESERVE concrete details.\n\n"
        "Partial summaries (in order):\n"
    ),
    "tr": (
        "Aşağıdaki kısmi paragraf özetlerini (her biri tek bir konuşmanın ARDIŞIK "
        "bir dilimini kapsar) uzun süreli hafıza için TEK tutarlı düz-metin "
        "paragrafta BİRLEŞTİR — JSON, markdown, madde imi, başlık YOK. Tekrar eden "
        "noktaları TEKİLLEŞTİR, çelişkileri UZLAŞTIR (önceki bir noktayı tersine "
        "çeviren sonraki parça kazanır). Somut detayları KORU.\n\n"
        "Kısmi özetler (sırayla):\n"
    ),
}


def _iso_now() -> str:
    return iso_now()


def _meta_store(memory: Memory) -> ConversationStore | None:
    """The conversation metadata store, or ``None`` when this memory has none."""
    try:
        return memory.conversations_meta
    except Exception:  # bare-store Memory without a db_path
        return None


def _last_user_turn_id(turns: list[EpisodicTurn]) -> str | None:
    return next((t.id for t in reversed(turns) if t.role == "user"), None)


def _summary_view_from_meta(
    conversation_id: str, jm: dict[str, object]
) -> SummaryView | None:
    """Build a :class:`SummaryView` from a conversation's stored ``last_summary_struct``
    (the ``{"summary": ...}`` payload), or ``None`` when there is no usable prior summary."""
    raw = jm.get(_META_LAST_SUMMARY_STRUCT)
    if not isinstance(raw, dict):
        return None
    updated = jm.get(_META_LAST_SUMMARY_AT)
    view = SummaryView.from_payload(
        conversation_id, raw, updated_at=str(updated) if updated else None
    )
    return None if view.is_empty else view


def get_session_summary(memory: Memory, conversation_id: str) -> SummaryView | None:
    """The conversation's latest summary paragraph, or ``None``.

    Reads ``last_summary_struct`` from the conversation's ``json_metadata`` and
    returns a :class:`SummaryView` (empty/missing → ``None``). This is the stable
    consumer entry point (context assembler injection, cross-session
    consolidation): the producer (:meth:`SessionCloser.close`) writes the same key
    on every summary. Never raises — a memory without a metadata store yields
    ``None``."""
    meta_store = _meta_store(memory)
    if meta_store is None:
        return None
    try:
        jm = meta_store.get_json_metadata(conversation_id)
    except Exception:  # reads must never break the consumer
        log.debug("get_session_summary: metadata read failed for %s", conversation_id, exc_info=True)
        return None
    return _summary_view_from_meta(conversation_id, jm)


class SessionCloser:
    """Summarize a finished conversation into a staged ``synthesis`` candidate."""

    def __init__(
        self,
        memory: Memory,
        summarize: Callable[[str], str],
        *,
        min_turns: int = 4,
        max_chars: int = 6000,
        language: str = "en",
    ) -> None:
        self._memory = memory
        self._summarize = summarize
        self._min_turns = max(1, min_turns)
        self._max_chars = max(200, max_chars)
        self._language = language if language in ("en", "tr") else "en"

    # -- public ------------------------------------------------------------------

    def close(self, conversation_id: str) -> list[StagedFact]:
        """Summarize ``conversation_id`` into ONE staged candidate; ``[]`` on no-op.

        Produces a single prose paragraph capturing the durable substance of the
        conversation (topic, decisions, open items, salient names/dates/numbers) and
        stages it as ONE ``synthesis`` candidate at key ``session:<cid12>``. The same
        paragraph is persisted under ``last_summary_struct`` for rolling reads +
        consumers (``get_session_summary`` / the context assembler).

        Rolling: when a prior ``last_summary_struct`` exists (and its anchor is still in
        the fetch window), only the NEW turns since ``last_summary_turn_id`` are fed to
        an update prompt with the prior paragraph → O(n) instead of O(n²). First-ever
        summary (or a rotated-out anchor) falls back to a full multi-chunk pass + an LLM
        reduce merge.

        No-op (``[]``) cases: too few user/assistant turns (< ``min_turns``), no user
        turn at all, no new user turn since the last summary, a failing summarizer, or an
        EMPTY paragraph (nothing durable — only greetings/small talk).
        """
        turns = [
            t
            for t in self._memory.recent_turns(conversation_id, limit=_TURN_FETCH_LIMIT)
            if t.role in ("user", "assistant")
        ]
        if len(turns) < self._min_turns:
            return []
        anchor = _last_user_turn_id(turns)
        if anchor is None:
            return []

        meta_store = _meta_store(self._memory)
        previous_view: SummaryView | None = None
        previous_anchor: str | None = None
        if meta_store is not None:
            jm = meta_store.get_json_metadata(conversation_id)
            # Soft-deleted conversation (#11): the turns remain in episodic, but the
            # user deleted the conversation → don't produce a zombie summary (defensive;
            # the cron should already filter it out).
            if jm.get("deleted"):
                log.debug("session_closer: %s is deleted; skipping summarization", conversation_id)
                return []
            previous_anchor = jm.get(_META_LAST_SUMMARY_TURN_ID)
            if previous_anchor == anchor:
                log.debug(
                    "session_closer: %s already summarized at turn %s", conversation_id, anchor
                )
                return []
            previous_view = _summary_view_from_meta(conversation_id, jm)

        try:
            paragraph = self._summarize_text(turns, previous_view, previous_anchor)
        except Exception:  # the closer must never raise into the caller
            log.exception("session_closer: summarize failed for %s", conversation_id)
            return []

        view = SummaryView(conversation_id=conversation_id, summary=paragraph)
        if view.is_empty:
            log.debug("session_closer: empty summary for %s; skipping", conversation_id)
            return []

        # b19: the deleted flag was read BEFORE the (multi-second) summarizer ran. A concurrent
        # soft_delete during summarization would otherwise leave a ZOMBIE summary (and, with
        # allow_direct, a promoted durable fact) for a just-deleted conversation. Re-read it right
        # before the writes and bail if it flipped (lock-free WAL read sees the committed delete).
        if meta_store is not None and meta_store.get_json_metadata(conversation_id).get("deleted"):
            log.debug(
                "session_closer: %s deleted during summarization; skipping stage", conversation_id
            )
            return []

        # Re-summary cleanup: reject this conversation's previous PENDING session
        # candidate → the new summary REPLACES the old one and doesn't pile up in the
        # inbox. An already-approved (promoted) old item is no longer pending → untouched.
        # No-op on the first summary.
        self._reject_stale_session_candidates(conversation_id)

        st = self._memory.staging.stage(
            FactCandidate(
                key=f"session:{conversation_id[:12]}",
                value=view.summary,
                reason="session_closer",
                trust="synthesis",
                source_turn_id=anchor,
                extractor="session_closer",
            ),
            conversation_id=conversation_id,
        )
        if st is None:
            return []
        if meta_store is not None:
            meta_store.merge_json_metadata(
                conversation_id,
                {
                    _META_LAST_SUMMARY_AT: _iso_now(),
                    _META_LAST_SUMMARY_TURN_ID: anchor,
                    _META_LAST_SUMMARY_STRUCT: view.to_payload(),
                },
            )
        return [st]

    # -- internals ---------------------------------------------------------------

    def _summarize_text(
        self,
        turns: list[EpisodicTurn],
        previous_view: SummaryView | None,
        previous_anchor: str | None,
    ) -> str:
        """The summary paragraph for this conversation — rolling when a usable prior
        summary exists, else a full (multi-chunk + reduce) cold-start pass.

        Rolling requires BOTH a prior summary AND that its anchor is still inside the
        fetched turns (so "new turns since anchor" is well-defined). If the anchor
        rotated out of the 1000-turn window, fall back to the full pass."""
        new_turns = self._turns_after(turns, previous_anchor)
        if previous_view is not None and previous_anchor is not None and new_turns is not None:
            return self._rolling_text(previous_view, new_turns)
        return self._full_text(turns)

    def _rolling_text(self, previous_view: SummaryView, new_turns: list[EpisodicTurn]) -> str:
        """Rewrite the prior paragraph reconciled with ONLY the new turns (one LLM call)."""
        prompt = (
            _PROMPT_ROLLING[self._language]
            + previous_view.summary
            + _ROLLING_NEW_TURNS_LABEL[self._language]
            + self._transcript(new_turns)
        )
        return clean_summary_text(self._summarize(prompt))

    def _full_text(self, turns: list[EpisodicTurn]) -> str:
        """Cold-start full pass: one call per chunk, then a reduce merge.
        A conversation that fits in one chunk is a single call (old behaviour)."""
        parts = [
            clean_summary_text(self._summarize(_PROMPT_HEADS[self._language] + chunk))
            for chunk in self._chunks(turns)
        ]
        parts = [p for p in parts if p]
        if not parts:
            return ""
        if len(parts) == 1:
            return parts[0]
        return self._reduce_text(parts)

    def _reduce_text(self, parts: list[str]) -> str:
        """Merge several chunk paragraphs into one (cold-start multi-chunk path). An LLM
        reduce pass reconciles/dedupes; on any failure or empty result, fall back to the
        order-preserving join (so multi-chunk merging never loses content)."""
        joined = "\n\n".join(parts)
        body = joined if len(joined) <= self._max_chars else joined[: self._max_chars]
        try:
            merged = clean_summary_text(self._summarize(_PROMPT_REDUCE[self._language] + body))
        except Exception:  # reduce is best-effort; never lose the partials
            log.warning("session_closer: reduce pass failed; joining partials", exc_info=True)
            return " ".join(parts)
        # A reduce that collapsed everything to empty is a regression — keep the join.
        return merged or " ".join(parts)

    def _turns_after(
        self, turns: list[EpisodicTurn], last_summary_turn_id: str | None
    ) -> list[EpisodicTurn] | None:
        """The user/assistant turns recorded AFTER ``last_summary_turn_id``, or ``None``
        when that anchor is NOT in the fetched window (so rolling can't be done and the
        caller must take the full pass)."""
        if not last_summary_turn_id:
            return None
        for i, t in enumerate(turns):
            if t.id == last_summary_turn_id:
                return turns[i + 1 :]
        return None

    def _labelled_lines(self, turns: list[EpisodicTurn]) -> list[str]:
        """Chronological ``<role label>: <collapsed text>`` lines, each clipped to
        ``max_chars`` so one giant message can't blow the budget. The shared building
        block for both the rolling transcript and the cold-start chunker."""
        roles = _ROLE_LABELS[self._language]
        lines: list[str] = []
        for t in turns:
            label = roles.get(t.role, t.role)
            line = f"{label}: " + " ".join(t.text.split())
            if len(line) > self._max_chars:
                line = line[: self._max_chars]
            lines.append(line)
        return lines

    def _transcript(self, turns: list[EpisodicTurn]) -> str:
        """The labelled lines joined for a (small) turn list — the rolling pass body."""
        return "\n".join(self._labelled_lines(turns))

    def _chunks(self, turns: list[EpisodicTurn]) -> list[str]:
        """Split the chronological labelled turn lines into ``max_chars``-sized chunks.
        If the conversation fits in one chunk, return a SINGLE element (old single-pass
        behaviour). If not, the WHOLE conversation is distributed across chunks in order
        → the beginning is not lost (the old ``_transcript`` kept only the tail). A
        single line exceeding max_chars is clipped (so a giant single message doesn't
        fill a chunk on its own and overflow)."""
        lines = self._labelled_lines(turns)
        chunks: list[str] = []
        cur: list[str] = []
        cur_len = 0
        for line in lines:
            if cur and cur_len + len(line) + 1 > self._max_chars:
                chunks.append("\n".join(cur))
                cur, cur_len = [], 0
            cur.append(line)
            cur_len += len(line) + 1
        if cur:
            chunks.append("\n".join(cur))
        return chunks or [""]

    def _reject_stale_session_candidates(self, conversation_id: str) -> int:
        """Reject this conversation's previous PENDING session summary candidates (so a
        re-summary REPLACES the old one; an unapproved old summary doesn't pile up in
        the inbox). Already-approved (promoted) items are no longer pending → untouched.
        Returns the number rejected (for logging/tests)."""
        rejected = 0
        for old in self._memory.staging.list_pending(limit=500):
            if old.extractor == "session_closer" and old.conversation_id == conversation_id:
                if self._memory.staging.mark_rejected(old.id):
                    rejected += 1
        return rejected


def _content_chars_after(
    turns: list[EpisodicTurn], last_summary_turn_id: str | None
) -> int:
    """Total user/assistant text length accumulated AFTER the last-summary anchor (all
    of it if never summarized / the anchor rotated out). Char-based so dense turns
    count more than the turn-count trigger (which is content-blind)."""
    relevant = [t for t in turns if t.role in ("user", "assistant")]
    if last_summary_turn_id:
        for i, t in enumerate(relevant):
            if t.id == last_summary_turn_id:
                relevant = relevant[i + 1 :]
                break
    return sum(len(t.text) for t in relevant)


def _new_turn_count(turns: list[EpisodicTurn], last_summary_turn_id: str | None) -> int:
    """User/assistant turns recorded AFTER the last-summary anchor (all of them if the
    conversation was never summarized / the anchor rotated out of the fetch window)."""
    relevant = [t for t in turns if t.role in ("user", "assistant")]
    if not last_summary_turn_id:
        return len(relevant)
    for i, t in enumerate(relevant):
        if t.id == last_summary_turn_id:
            return len(relevant) - i - 1
    return len(relevant)


def find_idle_conversations(
    memory: Memory,
    *,
    idle_minutes: int = 30,
    turn_threshold: int = 0,
    char_threshold: int = 0,
    limit: int = 20,
) -> list[str]:
    """Conversation ids ready for a summary, with a STALE summary (no
    ``last_summary_turn_id`` or a newer user turn). A conversation qualifies when it is
    ANY of:

    * idle — its last turn is older than ``idle_minutes`` (the classic trigger), OR
    * busy-but-long — ``turn_threshold > 0`` and it has accumulated at least
      ``turn_threshold`` new user/assistant turns since the last summary (so a long,
      still-active chat is captured EARLY instead of waiting to go idle), OR
    * busy-but-dense — ``char_threshold > 0`` and the new user/assistant CONTENT since
      the last summary exceeds ``char_threshold`` characters (turn count is content-
      blind; a few dense turns should trigger too).

    The server cron calls this and feeds each id to :meth:`SessionCloser.close`.
    Conversations without any user turn are skipped (``close`` would no-op forever).
    Unparseable timestamps are skipped (conservative: don't summarize what we can't date).
    """
    threshold = datetime.now(UTC) - timedelta(minutes=max(0, idle_minutes))
    meta_store = _meta_store(memory)
    out: list[str] = []
    for row in memory.conversations(limit=_IDLE_SCAN_LIMIT):
        cid = str(row.get("conversation_id") or "")
        if not cid:
            continue
        # Don't let a soft-deleted conversation become a ZOMBIE (#11): because
        # ``memory.conversations`` is turn-based (episodic GROUP BY) it also returns
        # rows stamped ``json_metadata.deleted``; the cron must not summarize a deleted one.
        if meta_store is not None and meta_store.get_json_metadata(cid).get("deleted"):
            continue
        try:
            last_ts = datetime.fromisoformat(str(row.get("last_ts") or "").replace("Z", "+00:00"))
            if last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=UTC)
        except ValueError:
            log.debug("find_idle_conversations: unparseable ts for %s; skipping", cid)
            continue
        is_idle = last_ts <= threshold
        if not is_idle and turn_threshold <= 0 and char_threshold <= 0:
            continue  # fast path: still active + both content triggers disabled
        turns = memory.recent_turns(cid, limit=_TURN_FETCH_LIMIT)
        anchor = _last_user_turn_id(turns)
        if anchor is None:
            continue
        previous = (
            meta_store.get_json_metadata(cid).get(_META_LAST_SUMMARY_TURN_ID)
            if meta_store is not None
            else None
        )
        if previous == anchor:
            continue  # summary already covers the latest user turn
        if not is_idle:
            turn_hit = turn_threshold > 0 and _new_turn_count(turns, previous) >= turn_threshold
            char_hit = char_threshold > 0 and _content_chars_after(turns, previous) >= char_threshold
            if not (turn_hit or char_hit):
                continue  # active and neither content trigger has fired yet
        out.append(cid)
        if len(out) >= max(1, limit):
            break
    return out
