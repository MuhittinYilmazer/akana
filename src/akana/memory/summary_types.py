"""Shared types for session summaries (single-paragraph form).

Leaf module — imported by the producer (``session_closer``) and the consumers
(context assembler, eval) without import cycles. A session summary is ONE
flowing prose paragraph (no structured decision/open-item lists); this module
owns the language-agnostic value object and the ONE tolerant text-cleaner every
call site shares (so "what a summary is" can't drift per producer).
"""

from __future__ import annotations

import json
from dataclasses import dataclass

__all__ = [
    "SummaryView",
    "clean_summary_text",
]


def clean_summary_text(raw: str) -> str:
    """Normalize a model's summary into ONE clean paragraph.

    Tolerant by design:

    * strips a surrounding fenced code block (```), keeping the inner body;
    * if the body is (or wraps) a JSON object carrying a ``summary``/``ozet``
      field — a model that ignored the plain-prose instruction, or a *legacy*
      stored v2 payload — that field is pulled out;
    * otherwise the whole text is taken.

    All internal whitespace runs (including newlines/bullets-as-newlines)
    collapse to single spaces, so the result is always a single paragraph and a
    multi-line model reply can't smuggle structure back in.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    if s.startswith("```"):
        s = s.strip("`").strip()
        # Drop an optional language tag left on the first line (e.g. ```json).
        nl = s.find("\n")
        if nl != -1 and " " not in s[:nl]:
            s = s[nl + 1 :].strip()
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(s[start : end + 1])
        except (json.JSONDecodeError, ValueError):
            obj = None
        if isinstance(obj, dict):
            field = obj.get("summary", obj.get("ozet"))
            if isinstance(field, str) and field.strip():
                s = field
    return " ".join(s.split())


@dataclass(frozen=True, slots=True)
class SummaryView:
    """A conversation's session summary — one prose paragraph.

    Producers (``session_closer``) build it; consumers (assembler injection,
    eval) read it. Pure data: rendering for prompt injection lives at the call
    site (the assembler), not here.
    """

    conversation_id: str
    summary: str = ""
    updated_at: str | None = None

    @property
    def is_empty(self) -> bool:
        """No durable content — nothing worth injecting or storing."""
        return not self.summary.strip()

    @classmethod
    def from_payload(
        cls, conversation_id: str, payload: dict[str, object], *, updated_at: str | None = None
    ) -> SummaryView:
        """Build from a stored ``last_summary_struct`` dict.

        Only ``summary`` is read; legacy v2 keys (title/decisions/open_items/
        entities/follow_ups) on an already-stored payload are ignored on read so
        old conversations keep loading without a migration.
        """
        return cls(
            conversation_id=conversation_id,
            summary=str(payload.get("summary") or "").strip(),
            updated_at=updated_at,
        )

    def to_payload(self) -> dict[str, object]:
        """Round-trip back to a JSON-serializable dict (for json_metadata storage)."""
        return {"summary": self.summary}
