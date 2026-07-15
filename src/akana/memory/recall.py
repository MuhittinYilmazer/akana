"""Recall — fuse semantic facts + episodic turns into a budgeted context.

Ported from ``akana_server.memory.recall`` with two upgrades the vision
demands:

* **Trust filter (P6/K15).** Recall applies a ``min_trust`` floor (default
  ``inferred``); the stores stay policy-free. Low-trust facts (``tool_output`` /
  ``synthesis``) do not surface unless a caller explicitly lowers the floor.
* **Explain trace.** Every recall returns a :class:`RecallTrace` — the terms it
  searched, how many candidates each language produced, what was merged, and what
  the token budget dropped. Recall is meant to be *debuggable*, not magic.

The pipeline: tokenize → search both languages per term → score → sort →
dedup → trim to budget. Pure data in, pure data out; no driver import.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Literal

from akana.memory.episodic import EpisodicStore
from akana.memory.semantic import SemanticStore
from akana.memory.terms import fold_text, recall_search_terms

RecallKind = Literal["episodic", "semantic"]

_DEFAULT_MIN_TRUST = "inferred"  # K15
_EPISODIC_SCORE = 0.55
# Bilingual (``language``-keyed), matching the pattern in session_closer.py.
# English is the default for every caller that does not pass a language.
_ROLE_LABELS = {
    "en": {"user": "You", "assistant": "Akana", "system": "System"},
    "tr": {"user": "Sen", "assistant": "Akana", "system": "Sistem"},
}
_DEFAULT_LANGUAGE = "en"
# Memory recall surfaces what the user said, not Akana's operational chatter
# ("I'm looking into it", "I'm sending it" …) which pollutes episodic search.
_EPISODIC_RECALL_ROLES = frozenset({"user"})


def episodic_turn_eligible(role: str) -> bool:
    """Whether a turn role may appear in hybrid memory recall."""
    return role in _EPISODIC_RECALL_ROLES


@dataclass(frozen=True, slots=True)
class RecallBlock:
    """One recalled item, ready to render into a context prefix."""

    kind: RecallKind
    text: str
    ts: str | None = None
    conversation_id: str | None = None
    score: float = 0.0
    trust: str | None = None  # semantic only
    source_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RecallTrace:
    """Why recall returned what it did (the transparency record)."""

    query: str
    terms: tuple[str, ...]
    min_trust: str | None
    conversation_id: str | None
    semantic_candidates: int
    episodic_candidates: int
    merged: int
    returned: int
    dropped_for_budget: int
    total_tokens: int
    budget_tokens: int


@dataclass(frozen=True, slots=True)
class RecallResult:
    blocks: list[RecallBlock]
    trace: RecallTrace

    def __bool__(self) -> bool:
        return bool(self.blocks)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class Recall:
    """Hybrid recall over the episodic + semantic stores."""

    def __init__(self, episodic: EpisodicStore, semantic: SemanticStore) -> None:
        self._episodic = episodic
        self._semantic = semantic

    def recall(
        self,
        query: str,
        *,
        conversation_id: str | None = None,
        min_trust: str | None = _DEFAULT_MIN_TRUST,
        limit: int = 12,
        budget_tokens: int = 2000,
        language: str = _DEFAULT_LANGUAGE,
    ) -> RecallResult:
        terms = recall_search_terms(query) or [query]
        blocks: list[RecallBlock] = []

        sem_candidates = self._collect_semantic(
            blocks, terms, min_trust=min_trust, limit=limit
        )
        ep_candidates = self._collect_episodic(
            blocks, terms, conversation_id=conversation_id, limit=limit, language=language
        )

        unique = self._fuse(blocks)
        merged = len(unique)
        considered = unique[: limit * 2]
        out, total_tokens = self._apply_budget(considered, budget_tokens)

        trace = RecallTrace(
            query=query,
            terms=tuple(terms),
            min_trust=min_trust,
            conversation_id=conversation_id,
            semantic_candidates=sem_candidates,
            episodic_candidates=ep_candidates,
            merged=merged,
            returned=len(out),
            dropped_for_budget=max(0, len(considered) - len(out)),
            total_tokens=total_tokens,
            budget_tokens=budget_tokens,
        )
        return RecallResult(blocks=out, trace=trace)

    def _collect_semantic(
        self,
        blocks: list[RecallBlock],
        terms: list[str],
        *,
        min_trust: str | None,
        limit: int,
    ) -> int:
        seen: set[str] = set()
        # Fold each term once (not per fact): the key-match test below compares
        # every surviving fact key against all terms.
        folded_terms = [fold_text(t) for t in terms if len(t) >= 2]
        for term in terms:
            for fact in self._semantic.search(
                term, min_trust=min_trust, limit=limit
            ):
                if fact.id in seen:
                    continue
                seen.add(fact.id)
                # Turkish-aware fold, not bare lower(): "İsim".lower() is 'i̇sim'
                # (combining dot), so "isim" in it is False and the İ/I fact never
                # earns the key-match tier. fold_text is the store's own compare rule.
                fk = fold_text(fact.key)
                key_match = any(
                    ft in fk or fk in ft
                    for ft in folded_terms
                )
                if key_match:
                    score = 0.82 + min(0.15, fact.importance * 0.15)
                else:
                    score = 0.7 + min(0.25, fact.importance * 0.25)
                blocks.append(
                    RecallBlock(
                        kind="semantic",
                        text=f"{fact.key}: {fact.value}",
                        ts=fact.ts_last,
                        score=score,
                        trust=fact.trust,
                        source_ids=(fact.id,),
                    )
                )
        return len(seen)

    def _collect_episodic(
        self,
        blocks: list[RecallBlock],
        terms: list[str],
        *,
        conversation_id: str | None,
        limit: int,
        language: str = _DEFAULT_LANGUAGE,
    ) -> int:
        seen: set[str] = set()
        labels = _ROLE_LABELS.get(language, _ROLE_LABELS[_DEFAULT_LANGUAGE])
        for term in terms:
            # Push the role window into SQL (BEFORE the LIMIT): otherwise the top-`limit`
            # bm25 rows can be all-assistant and get discarded below, starving eligible
            # user turns ranked just past the cutoff (they'd never be fetched at all).
            for turn in self._episodic.search_keyword(
                term,
                conversation_id=conversation_id,
                limit=limit,
                roles=tuple(_EPISODIC_RECALL_ROLES),
            ):
                if not episodic_turn_eligible(turn.role):
                    continue
                if turn.id in seen:
                    continue
                seen.add(turn.id)
                role = labels.get(turn.role, turn.role)
                blocks.append(
                    RecallBlock(
                        kind="episodic",
                        text=f"[{role}] {turn.text[:600]}",
                        ts=turn.ts,
                        conversation_id=turn.conversation_id,
                        score=_EPISODIC_SCORE,
                        source_ids=(turn.id,),
                    )
                )
        return len(seen)

    @staticmethod
    def _fuse(blocks: list[RecallBlock]) -> list[RecallBlock]:
        blocks.sort(key=lambda b: b.score, reverse=True)
        seen_sig: set[str] = set()
        unique: list[RecallBlock] = []
        for b in blocks:
            sig = b.text[:120]
            if sig in seen_sig:
                continue
            seen_sig.add(sig)
            unique.append(b)
        return unique

    @staticmethod
    def _apply_budget(
        blocks: list[RecallBlock], budget_tokens: int
    ) -> tuple[list[RecallBlock], int]:
        """Greedy budget fill: an oversized block is skipped, not a dam.

        The old ``break`` let one bloated block starve every smaller block
        ranked behind it (a 3000-char fact under a 200-token budget returned
        *nothing*). Overflowing blocks are now dropped individually and the
        rest still compete. If no block fits at all, the top-ranked block is
        clipped to the budget (~4 chars/token) and returned alone, so recall
        with candidates never comes back empty-handed; with no candidates (or
        a non-positive budget) the empty result stays as before.
        """
        total = 0
        out: list[RecallBlock] = []
        for b in blocks:
            t = _estimate_tokens(b.text)
            if total + t > budget_tokens:
                continue
            out.append(b)
            total += t
        if out or not blocks or budget_tokens <= 0:
            return out, total
        first = blocks[0]
        clipped = replace(first, text=first.text[: budget_tokens * 4])
        return [clipped], _estimate_tokens(clipped.text)
