"""Fusion helpers — shape recall output into the §8 ``memory.search`` items.

The orchestrator's O5 half: mapping :class:`RecallBlock`s to contract items
(with graph ``related`` enrichment) and the type/time post-filters. Pure-ish
functions with explicit dependencies, so they stay testable and the
orchestrator stays a thin router.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from akana.memory.recall import RecallResult
from akana.memory.semantic import SemanticFact
from akana.memory.terms import fold_text
from akana.memory.tools import kind_from_key

if TYPE_CHECKING:
    from akana.memory import Memory

__all__ = ["blocks_to_items", "filter_types", "filter_time", "filter_observed"]

log = logging.getLogger(__name__)

ItemPair = tuple[dict[str, Any], SemanticFact | None]


def blocks_to_items(
    memory: Memory,
    result: RecallResult,
    *,
    include_related: bool,
    related_cap: int,
) -> list[ItemPair]:
    """Map recall blocks to §8.1 output items, keeping the fact for later gates."""
    pairs: list[ItemPair] = []
    for block in result.blocks:
        item_id = block.source_ids[0] if block.source_ids else ""
        if block.kind == "semantic":
            fact = memory.get_fact(item_id) if item_id else None
            item = {
                "id": item_id,
                "type": kind_from_key(fact.key if fact else "").capitalize(),
                "summary": block.text,
                "quote": fact.quote if fact else None,
                "ts": block.ts,
                "score": round(block.score, 4),
                "scopes": [],
                "trust": block.trust,
                # Provenance (citation-native): {origin, detail, observed_at}
                "source": fact.source if fact else None,
                "related": _related_for(memory, fact, include_related=include_related, cap=related_cap),
            }
            pairs.append((item, fact))
        else:
            item = {
                "id": item_id,
                "type": "Episode",
                "summary": block.text,
                "quote": None,
                "ts": block.ts,
                "score": round(block.score, 4),
                "scopes": [],
                "trust": None,
                "source": None,  # episodic turns are the raw record, not a derived fact
                "related": [],
                "conversation_id": block.conversation_id,
            }
            pairs.append((item, None))
    return pairs


def _related_for(
    memory: Memory, fact: SemanticFact | None, *, include_related: bool, cap: int
) -> list[str]:
    """Best-effort 1-hop graph neighbours of a fact's key node."""
    if fact is None or not include_related:
        return []
    try:
        nodes = memory.graph.neighbors(fact.key, kind="fact_key")
    except Exception:  # graph hiccups must never break search
        log.exception("related lookup failed for %s", fact.id)
        return []
    return [n.label for n in nodes if n.label != fact.value][: max(0, cap)]


def filter_types(pairs: list[ItemPair], types: list[str]) -> tuple[list[ItemPair], int]:
    # fold_text: type labels can be Turkish (derived from fact keys), and bare
    # lower() breaks İ/I matching.
    allowed = {fold_text(t.strip()) for t in types if t.strip()}
    if not allowed:
        return pairs, 0
    kept = [(i, f) for i, f in pairs if fold_text(i["type"]) in allowed]
    return kept, len(pairs) - len(kept)


def _in_window(ts: str | None, frm: str | None, to: str | None) -> bool:
    if not ts:
        return False
    return not ((frm and ts < frm) or (to and ts > to))


def filter_time(
    pairs: list[ItemPair], frm: str | None, to: str | None
) -> tuple[list[ItemPair], int]:
    """Time-window post-filter — the bounds are ISO-UTC strings already RESOLVED
    by the caller (the orchestrator, via ``parse_time_bound``). Resolution and the
    malformed-input error live in one place, the orchestrator: there is no silent
    swallowing here."""
    if frm is None and to is None:
        return pairs, 0
    kept = [(i, f) for i, f in pairs if _in_window(i.get("ts"), frm, to)]
    return kept, len(pairs) - len(kept)


def filter_observed(
    pairs: list[ItemPair], frm: str | None, to: str | None
) -> tuple[list[ItemPair], int]:
    """Bi-temporal observation filter: was the record OBSERVED (learned) within
    that window?

    For facts, the provenance ``observed_at`` (or ``ts_first`` if absent — the
    same honest fallback as the migration backfill); for episodic turns, the
    moment of observation is the turn's ``ts``. The bounds are ISO-UTC strings
    resolved by the caller (the orchestrator); in the millisecond-Z format a
    lexicographic comparison equals a time comparison.
    """
    if frm is None and to is None:
        return pairs, 0

    def _observed(item: dict[str, Any], fact: SemanticFact | None) -> str | None:
        if fact is not None:
            return fact.observed_at or fact.ts_first
        return item.get("ts")

    kept = [(i, f) for i, f in pairs if _in_window(_observed(i, f), frm, to)]
    return kept, len(pairs) - len(kept)
