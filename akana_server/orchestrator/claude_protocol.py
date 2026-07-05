"""Claude headless text-protocol helpers — pure parsers/normalizers.

Extracted from :mod:`claude_provider` to keep that module under the repo's
god-file ceiling (``tests/architecture/test_repo_boundaries.py``). These are all
PURE functions/classes over the ``claude -p`` (headless) stream shapes — no
subprocess, no settings, no I/O — so they live cleanly on their own:

* assistant-message text extraction,
* tool-``input`` coercion (headless-rejected tools deliver an UNPARSED JSON string),
* the ``[[AKANA_ASK]]…[[/AKANA_ASK]]`` text-protocol (markers, extract, strip, and
  the streaming :class:`_AskBlockStripper` that holds the block back from deltas),
* normalizers for the ``AskUserQuestion`` / ``ExitPlanMode`` / ``TodoWrite`` tool
  inputs into stable card payloads.

``claude_provider`` re-imports every name below into its own namespace, so callers
(and the existing tests that reach them via ``claude_provider._name``) are
unchanged.
"""

from __future__ import annotations

import json
from typing import Any


def _extract_assistant_text(message: dict[str, Any]) -> str:
    """Concatenate text blocks from an assistant message's content."""
    parts: list[str] = []
    for block in message.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "".join(parts)


def _coerce_tool_input(raw_input: Any) -> Any:
    """Return the tool ``input`` as a dict when it arrives as a JSON-object string.

    ``AskUserQuestion``/``ExitPlanMode`` are NOT available in headless (``-p``)
    mode in current ``claude`` (they are interactive-only, absent from the init
    ``tools`` list). When the model calls one anyway the CLI rejects it with
    ``No such tool available: … not enabled in this context`` — and, crucially,
    delivers that rejected tool's ``input`` as an UNPARSED JSON string, unlike an
    executable tool whose input the CLI pre-parses into a dict. Without this
    coercion :func:`_normalize_ask_user`/:func:`_normalize_plan` see a ``str``,
    return ``None``, and the question/plan degrades into a generic red error card.
    A JSON string is parsed; anything else is returned unchanged.
    """
    if isinstance(raw_input, str):
        try:
            return json.loads(raw_input)
        except (ValueError, TypeError):
            return raw_input
    return raw_input


#: Text-protocol ask_user markers. The native ``AskUserQuestion`` tool is
#: interactive-only — unavailable in headless ``claude -p`` — and the model,
#: seeing it absent from its tool list, often refuses to call it at all (it checks
#: and gives up rather than emitting a rejected tool_use). So Akana instructs the
#: model (system prompt) to emit its multiple-choice question as a delimited JSON
#: block instead; this ALWAYS works, with no dependency on tool availability. The
#: markers are distinctive (near-zero collision with real prose) so they are
#: trivial to detect and strip.
_ASK_OPEN = "[[AKANA_ASK]]"
_ASK_CLOSE = "[[/AKANA_ASK]]"


def _marker_overlap(buf: str, marker: str) -> int:
    """Longest k (< len(marker)) such that ``buf`` ends with ``marker[:k]``.

    Used to hold back a partial opening marker split across stream chunks so it
    never flashes as chat text (same trick as ``_SentinelStripper._overlap``)."""
    for k in range(min(len(buf), len(marker) - 1), 0, -1):
        if buf.endswith(marker[:k]):
            return k
    return 0


def _extract_ask_block(text: Any) -> str | None:
    """Return the JSON payload inside an ``[[AKANA_ASK]]…[[/AKANA_ASK]]`` block, else None.

    Only a COMPLETE block (both markers, close after open) yields a payload — the
    raw JSON string between them (fed straight to :func:`_normalize_ask_user`, which
    parses strings). If either marker is missing the text is ordinary chat."""
    if not isinstance(text, str):
        return None
    i = text.find(_ASK_OPEN)
    if i == -1:
        return None
    j = text.find(_ASK_CLOSE, i + len(_ASK_OPEN))
    if j == -1:
        return None
    return text[i + len(_ASK_OPEN) : j].strip()


def _strip_ask_block(text: Any) -> Any:
    """Remove an ``[[AKANA_ASK]]…[[/AKANA_ASK]]`` block from answer text.

    Defensive: the block is held back from the live deltas, but if it lands in the
    complete-message fallback text (e.g. the JSON was malformed so it wasn't
    promoted to an ``ask_user`` card) it must still never appear as the visible
    answer. An unterminated block (open, no close) is dropped from the open onward."""
    if not isinstance(text, str):
        return text
    i = text.find(_ASK_OPEN)
    if i == -1:
        return text
    j = text.find(_ASK_CLOSE, i + len(_ASK_OPEN))
    if j == -1:
        return text[:i].rstrip()
    return (text[:i] + text[j + len(_ASK_CLOSE) :]).strip()


class _AskBlockStripper:
    """Hold back an ``[[AKANA_ASK]]…`` block from the streamed text deltas.

    ``feed`` returns the slice safe to emit now: text BEFORE the opening marker is
    passed through; from the marker onward nothing is emitted — that region is the
    structured ``ask_user`` card, not chat prose, so it must never flash in the live
    bubble. A partial opening marker split across chunks is held back too. ``flush``
    returns any held-back tail that turned out NOT to be a block (real text)."""

    def __init__(self) -> None:
        self._buf = ""  # pre-marker text not yet safe to emit (may end with a partial marker)
        self._blocked = False  # opening marker seen → swallow everything after (the card JSON)

    def feed(self, text: str) -> str:
        if self._blocked:
            return ""
        self._buf += text
        i = self._buf.find(_ASK_OPEN)
        if i != -1:
            out, self._buf, self._blocked = self._buf[:i], "", True
            return out
        hold = _marker_overlap(self._buf, _ASK_OPEN)
        if hold:
            out, self._buf = self._buf[:-hold], self._buf[-hold:]
        else:
            out, self._buf = self._buf, ""
        return out

    def flush(self) -> str:
        if self._blocked:
            return ""  # the held tail is the block → never real text
        out, self._buf = self._buf, ""
        return out

    @property
    def captured_block(self) -> bool:
        """True once an opening ``[[AKANA_ASK]]`` marker was seen (all later
        deltas are being/were swallowed) — regardless of whether the block was
        ever successfully promoted to an ``ask_user`` card."""
        return self._blocked


def _normalize_ask_user(tid: Any, raw_input: Any) -> dict[str, Any] | None:
    """Convert the ``AskUserQuestion`` tool's input into a stable ``ask_user`` payload.

    Live CLI shape (verified)::

        {"questions": [
            {"question": "...", "header": "...", "multiSelect": false,
             "options": [{"label": "...", "description": "..."}]}
        ]}

    Because it is external input, it is normalized defensively: the input is first
    coerced from a possible JSON string (:func:`_coerce_tool_input` — the rejected
    headless tool delivers a string), empty questions/labels are dropped,
    ``multiSelect`` is coerced to bool, and options are coerced into
    ``{label, description}`` dicts (or plain strings). If no valid question
    remains, ``None`` is returned → the caller falls back to a generic tool card
    (does not swallow the turn, no silent corruption).
    """
    raw_input = _coerce_tool_input(raw_input)
    if not isinstance(raw_input, dict):
        return None
    raw_questions = raw_input.get("questions")
    if not isinstance(raw_questions, list):
        return None
    questions: list[dict[str, Any]] = []
    for q in raw_questions:
        if not isinstance(q, dict):
            continue
        text = str(q.get("question") or "").strip()
        if not text:
            continue
        options: list[dict[str, str]] = []
        for opt in q.get("options") or []:
            if isinstance(opt, dict):
                label = str(opt.get("label") or "").strip()
                if not label:
                    continue
                options.append(
                    {"label": label, "description": str(opt.get("description") or "").strip()}
                )
            elif isinstance(opt, str) and opt.strip():
                options.append({"label": opt.strip(), "description": ""})
        if not options:
            continue
        questions.append(
            {
                "question": text,
                "header": str(q.get("header") or "").strip(),
                "multiSelect": bool(q.get("multiSelect")),
                "options": options,
            }
        )
    if not questions:
        return None
    return {"id": str(tid or ""), "questions": questions}


def _normalize_plan(tid: Any, raw_input: Any) -> dict[str, Any] | None:
    """Convert the ``ExitPlanMode`` tool's input into a stable ``plan`` payload.

    Live CLI shape (verified)::

        {"plan": "# Plan…\n\n## Approach\n…", "planFilePath": "/…/plan-….md"}

    ``plan`` is the markdown body (required, ``None`` if empty). ``planFilePath``
    (if present) is the path of the plan file written to disk — carried for
    informational purposes. If there is no plan text, ``None`` is returned → the
    caller falls back to a generic tool card (does not swallow the turn). The
    input is coerced from a possible JSON string first (:func:`_coerce_tool_input`
    — the rejected headless tool delivers a string, same as AskUserQuestion).
    """
    raw_input = _coerce_tool_input(raw_input)
    if not isinstance(raw_input, dict):
        return None
    plan = str(raw_input.get("plan") or "").strip()
    if not plan:
        return None
    return {
        "id": str(tid or ""),
        "plan": plan,
        "plan_file": str(raw_input.get("planFilePath") or "").strip(),
    }


def _normalize_todo(raw_input: Any) -> dict[str, Any] | None:
    """Convert a ``TodoWrite`` tool input into a compact live-checklist payload.

    Live CLI shape: ``{"todos": [{"content", "status", "activeForm"}, …]}`` where status is
    pending|in_progress|completed. Returns ``{"items": [{"content", "status"}, …]}``, or ``None``
    when there is nothing usable (the caller then only emits the generic tool card)."""
    if not isinstance(raw_input, dict):
        return None
    todos = raw_input.get("todos")
    if not isinstance(todos, list):
        return None
    items: list[dict[str, str]] = []
    for t in todos:
        if not isinstance(t, dict):
            continue
        content = str(t.get("content") or t.get("activeForm") or "").strip()
        if not content:
            continue
        status = str(t.get("status") or "pending").strip().lower()
        if status not in ("pending", "in_progress", "completed"):
            status = "pending"
        items.append({"content": content[:200], "status": status})
    return {"items": items} if items else None


__all__ = [
    "_ASK_CLOSE",
    "_ASK_OPEN",
    "_AskBlockStripper",
    "_coerce_tool_input",
    "_extract_ask_block",
    "_extract_assistant_text",
    "_marker_overlap",
    "_normalize_ask_user",
    "_normalize_plan",
    "_normalize_todo",
    "_strip_ask_block",
]
