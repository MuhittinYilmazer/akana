"""Cross-session summary consolidation (M3.3) — merge overlapping session summaries.

Each conversation produces its own session summary (staged as ``trust="synthesis"``
candidates with keys like ``session:<cid>`` by :mod:`session_closer`). Session
summaries are EXEMPT from inbox dedup, so over time recurring topics pile up as many
overlapping summaries with no consolidation. This module is the housekeeping pass:
group overlapping session summaries by topic, and per group ask the injected
``summarize`` callable to produce ONE higher-level "topic" memory. The consolidated
result is staged as a ``synthesis`` candidate carrying ``source_fact_ids`` (the staged
ids it merges) so it is dedup-exempt and traceable — never hard-deleted; superseding
the stale ones is left to the curator/inbox like the rest of the system.

The LLM is **injected**, not owned: ``SummaryConsolidator(memory, summarize)`` takes
any ``Callable[[str], str]`` (the server wires the provider; tests pass a fake). A
failing summarizer logs and is swallowed — consolidation must never raise into the
caller (mirrors :class:`SessionCloser`).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from akana.memory._time import iso_now
from akana.memory.staging import FactCandidate, StagedFact

if TYPE_CHECKING:
    from akana.memory import Memory

__all__ = ["SummaryConsolidator", "consolidation_key"]

log = logging.getLogger(__name__)

#: Staging marks for the consolidation pass (so its own output is recognisable and
#: never re-consolidated as if it were a raw session summary).
_SESSION_EXTRACTOR = "session_closer"
_CONSOLIDATION_EXTRACTOR = "summary_consolidation"

#: How many pending rows to scan when collecting session summaries.
_SCAN_LIMIT = 500

#: English stop-words dropped before token overlap (deterministic grouping). Turkish
#: text folds to bare tokens too; this is a small, language-agnostic-ish filter — the
#: heuristic only needs to surface the topical nouns, not perfect linguistics.
_STOP_WORDS = frozenset(
    {
        # en
        "the", "and", "for", "are", "was", "were", "with", "that", "this", "from",
        "user", "akana", "will", "have", "has", "had", "not", "but", "you", "your",
        "about", "into", "than", "then", "they", "their", "what", "when", "session",
        # tr (folded)
        "ve", "ile", "icin", "bir", "bu", "su", "da", "de", "ki", "mi", "ama", "veya",
        "kullanici", "var", "yok", "cok", "daha", "gibi", "ya",
    }
)

#: Minimum shared topical tokens for two summaries to land in the same group.
_OVERLAP_THRESHOLD = 2

# Bilingual, ``language``-keyed prompt heads (mirrors session_closer._PROMPT_HEADS).
# JSON keys stay English in both (the parser is language-agnostic).
_PROMPT_HEADS = {
    "en": (
        "MERGE the overlapping session summaries below into ONE higher-level topic "
        "summary for long-term memory. Return ONLY valid JSON — no markdown, "
        "explanation, or code block. Schema:\n"
        '{"summary": "<1-3 sentence English overview of the recurring topic>"}\n'
        "Rules:\n"
        "- Capture only what is COMMON / recurring across these summaries; drop "
        "one-off noise.\n"
        "- PRESERVE concrete details such as names, dates, numbers.\n"
        "- Do NOT invent facts that are not present in the summaries below.\n"
        "- If there is nothing worth a consolidated topic, return an EMPTY summary "
        '("").\n'
        "\n"
        "Session summaries:\n"
    ),
    "tr": (
        "Aşağıdaki örtüşen oturum özetlerini uzun süreli hafıza için TEK bir üst "
        "düzey konu özetinde BİRLEŞTİR. SADECE geçerli JSON döndür — markdown, "
        "açıklama, kod bloğu YOK. Şema:\n"
        '{"summary": "<tekrar eden konunun 1-3 cümlelik Türkçe özeti>"}\n'
        "Kurallar:\n"
        "- Yalnızca bu özetlerde ORTAK / tekrar eden noktayı yakala; tek seferlik "
        "gürültüyü at.\n"
        "- İsim, tarih, sayı gibi somut detayları KORU.\n"
        "- Aşağıdaki özetlerde olmayan bir bilgi UYDURMA.\n"
        '- Birleştirmeye değer bir konu yoksa BOŞ özet ("") döndür.\n'
        "\n"
        "Oturum özetleri:\n"
    ),
}


def consolidation_key(group_key: str) -> str:
    """Stable staging key for a consolidated topic summary."""
    return f"topic:{group_key}"


def _iso_now() -> str:
    return iso_now()


def _tokens(text: str) -> set[str]:
    """Lower-cased alphanumeric topical tokens (>=3 chars, stop-words dropped)."""
    out: set[str] = set()
    word = []
    for ch in text.lower():
        if ch.isalnum():
            word.append(ch)
        elif word:
            out.add("".join(word))
            word = []
    if word:
        out.add("".join(word))
    return {w for w in out if len(w) >= 3 and w not in _STOP_WORDS}


def _parse_consolidated(raw: str) -> str:
    """Pull the ``summary`` string out of the model's JSON (tolerant of fences/prose).

    A corrupt/non-JSON body GRACEFULLY degrades to the whole trimmed text — mirrors
    the single-blob fallback used elsewhere so consolidation never breaks on a stray
    response."""
    import json

    s = (raw or "").strip()
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(s[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            obj = None
        if isinstance(obj, dict):
            return str(obj.get("summary", obj.get("ozet")) or "").strip()
    return s


@dataclass(slots=True)
class _Group:
    """An overlapping cluster of staged session summaries to merge into one topic."""

    tokens: set[str] = field(default_factory=set)
    members: list[StagedFact] = field(default_factory=list)

    @property
    def source_ids(self) -> tuple[str, ...]:
        return tuple(m.id for m in self.members)

    @property
    def conversation_ids(self) -> list[str]:
        seen: list[str] = []
        for m in self.members:
            cid = m.conversation_id or ""
            if cid and cid not in seen:
                seen.append(cid)
        return seen

    def signature(self) -> str:
        """Deterministic group key from the shared topical tokens (sorted)."""
        return "-".join(sorted(self.tokens)[:5]) or "misc"


def _group_summaries(
    items: list[StagedFact], min_overlap: int = _OVERLAP_THRESHOLD
) -> list[_Group]:
    """Greedy single-link clustering by topical-token overlap (deterministic, v1).

    Stable order in (older staged rows first), so the same inbox always yields the
    same groups. A summary joins the first existing group it overlaps with by at
    least ``min_overlap`` tokens (runtime-tunable, default :data:`_OVERLAP_THRESHOLD`);
    otherwise it seeds a new group.
    """
    threshold = max(1, min_overlap)
    groups: list[_Group] = []
    for item in items:
        # Tokenize the summary VALUE only — the ``session:<cid>`` key would otherwise
        # leak the conversation id into the topic signature (noise, not topic).
        toks = _tokens(item.value)
        if not toks:
            continue
        placed = False
        for g in groups:
            if len(g.tokens & toks) >= threshold:
                g.members.append(item)
                g.tokens |= toks
                placed = True
                break
        if not placed:
            groups.append(_Group(tokens=set(toks), members=[item]))
    # audit C23: the greedy pass places each summary in the FIRST overlapping group, so a
    # summary that bridges groups A and B joins only A (unioning B-like tokens in) while A and
    # B stay fragmented even though they now share >= threshold tokens — a recurring cross-topic
    # thread would consolidate into two divergent topics. Coalesce overlapping groups to a
    # transitive closure before the >=2-conversation filter. The inbox is small, so O(n^2) per
    # merge round is fine.
    coalesced = True
    while coalesced:
        coalesced = False
        for i in range(len(groups)):
            j = i + 1
            while j < len(groups):
                if len(groups[i].tokens & groups[j].tokens) >= threshold:
                    groups[i].members.extend(groups[j].members)
                    groups[i].tokens |= groups[j].tokens
                    del groups[j]
                    coalesced = True
                else:
                    j += 1
            if coalesced:
                break
    # Only groups that actually MERGE something (>=2 distinct conversations) are worth
    # consolidating — a lone summary, or several summaries from one conversation, is
    # not cross-session overlap.
    return [g for g in groups if len(g.conversation_ids) >= 2]


class SummaryConsolidator:
    """Merge overlapping staged session summaries into consolidated topic candidates."""

    def __init__(
        self,
        memory: Memory,
        summarize: Callable[[str], str],
        *,
        language: str = "en",
        max_chars: int = 6000,
        min_overlap: int = _OVERLAP_THRESHOLD,
    ) -> None:
        self._memory = memory
        self._summarize = summarize
        self._language = language if language in ("en", "tr") else "en"
        self._max_chars = max(200, max_chars)
        self._min_overlap = max(1, min_overlap)

    # -- public ------------------------------------------------------------------

    def consolidate(self) -> list[StagedFact]:
        """Scan pending session summaries, group overlaps, stage one topic per group.

        Returns the staged consolidation candidates (``[]`` when there is nothing to
        merge or the summarizer fails). Never raises — a failing summarizer is logged
        and swallowed (mirrors :class:`SessionCloser`)."""
        try:
            summaries = self._pending_session_summaries()
        except Exception:  # reading the inbox must never raise into the caller
            log.exception("summary_consolidation: failed to read pending summaries")
            return []
        groups = _group_summaries(summaries, self._min_overlap)
        if not groups:
            return []
        staged: list[StagedFact] = []
        for group in groups:
            try:
                st = self._consolidate_group(group)
            except Exception:  # one bad group must not abort the whole pass
                log.exception(
                    "summary_consolidation: group %s failed", group.signature()
                )
                continue
            if st is not None:
                staged.append(st)
        if staged:
            log.info(
                "summary_consolidation: %d topic candidate(s) staged from %d group(s)",
                len(staged),
                len(groups),
            )
        return staged

    # -- internals ---------------------------------------------------------------

    def _pending_session_summaries(self) -> list[StagedFact]:
        """Pending ``session_closer`` candidates only (never the consolidator's own
        output, never CAPTURE facts). Older rows first → deterministic grouping."""
        rows = self._memory.staging.list_pending(limit=_SCAN_LIMIT)
        return [
            r
            for r in rows
            if r.extractor == _SESSION_EXTRACTOR and (r.value or "").strip()
        ]

    def _consolidate_group(self, group: _Group) -> StagedFact | None:
        """Ask the summarizer to merge one group; stage the result as synthesis."""
        prompt = self._build_prompt(group)
        merged = _parse_consolidated(self._summarize(prompt).strip())
        if not merged:
            log.debug(
                "summary_consolidation: empty merge for group %s; skipping",
                group.signature(),
            )
            return None
        cids = group.conversation_ids
        staged = self._memory.staging.stage(
            FactCandidate(
                key=consolidation_key(group.signature()),
                value=merged,
                reason="summary_consolidation",
                trust="synthesis",
                extractor=_CONSOLIDATION_EXTRACTOR,
                # M3.3: the staged session-summary ids this topic merges — makes the
                # candidate dedup-exempt (source_fact_ids set) and traceable.
                source_fact_ids=group.source_ids,
            ),
            conversation_id=cids[0] if cids else None,
        )
        # CONSUME the merged source summaries (mark them 'rejected'). Without this they
        # stay 'pending' forever, so every hourly pass re-groups, re-summarizes (LLM cost)
        # and re-stages the SAME topic → unbounded inbox growth + repeated cost + collateral
        # flood-eviction of unrelated candidates. The durable rolling summary lives in the
        # conversation's meta (last_summary_struct), not in this inbox row, so the context
        # assembler is unaffected. Best-effort: a failed mark only means a retry next pass.
        for src_id in group.source_ids:
            try:
                self._memory.staging.mark_rejected(src_id)
            except Exception:  # pragma: no cover - defensive; never abort the pass
                log.debug(
                    "summary_consolidation: could not consume source %s", src_id,
                    exc_info=True,
                )
        return staged

    def _build_prompt(self, group: _Group) -> str:
        """Bilingual head + the group's bulleted session summaries (clipped)."""
        lines: list[str] = []
        for m in group.members:
            line = "- " + " ".join((m.value or "").split())
            lines.append(line)
        body = "\n".join(lines)
        if len(body) > self._max_chars:
            body = body[: self._max_chars]
        return _PROMPT_HEADS[self._language] + body
