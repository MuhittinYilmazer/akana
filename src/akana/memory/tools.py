"""Tool contracts — the ``memory.*`` surface the LLM calls (Vision §8).

This module owns the *boundary*: pydantic request models that validate every
incoming tool call (no malformed args reach the stores) and the JSON Schema
definitions handed to the LLM SDK. The :class:`~akana.memory.orchestrator.
MemoryOrchestrator` is the single handler behind these contracts (§11).

Two deliberate, additive extensions over the locked §8 schemas:

* ``memory.remember`` accepts an optional ``key`` — the semantic store is
  key/value and a model-supplied key beats a derived one. Absent, we derive.
* ``kind`` rides as a ``<kind>:`` key prefix (``lesson:``, ``snippet:``…), so
  the richer entity catalogue (§4) has a queryable home *today* without a
  schema migration; F1's typed entities can lift the prefix into a column.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from akana.memory.semantic import Trust
from akana.memory.time_expressions import DATE_ONLY_RE, TimeEdge, parse_time_range

__all__ = [
    "MEMORY_TOOLS",
    "SearchIntent",
    "RememberKind",
    "RememberPolicy",
    "ForgetMode",
    "ToolScope",
    "TimeRange",
    "SearchRequest",
    "RememberEvidence",
    "RememberRequest",
    "ForgetRequest",
    "ToolValidationError",
    "error_envelope",
    "parse_tool_request",
    "tool_schemas",
    "derive_key",
    "ensure_kind_prefix",
    "kind_from_key",
    "parse_time_point",
    "parse_time_range",
    "parse_time_bound",
]

SearchIntent = Literal[
    "fact_lookup",
    "episodic",
    "timeline",
    "explore",
    "skill_context",
    "lesson_lookup",
    "concept_lookup",
]
RememberKind = Literal[
    "fact",
    "preference",
    "rule",
    "lesson",
    "playbook",
    "snippet",
    "concept",
    "technique",
    "finding",
    "hypothesis",
    "bookmark",
]
RememberPolicy = Literal["stage", "direct"]
ForgetMode = Literal["retract", "supersede", "soft_delete", "quarantine"]
RerankMode = Literal["off", "cross_encoder"]

_REMEMBER_KINDS: tuple[str, ...] = (
    "fact",
    "preference",
    "rule",
    "lesson",
    "playbook",
    "snippet",
    "concept",
    "technique",
    "finding",
    "hypothesis",
    "bookmark",
)


class ToolValidationError(ValueError):
    """A tool call with arguments the contract rejects (boundary guard)."""


def error_envelope(tool: str, code: str, message: str) -> dict[str, Any]:
    """The uniform tool-boundary error shape (the model reads and reacts to it)."""
    return {"error": {"tool": tool, "code": code, "message": message}}


class _ToolModel(BaseModel):
    """Lenient at the edge: unknown fields are ignored, known ones validated."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class ToolScope(_ToolModel):
    # topic is concatenated into the search query (orchestrator._search), which is
    # otherwise capped at 2000; without this bound a model-controlled topic bypasses
    # the query-size boundary and drives multi-MB LIKE/regex scans per call.
    conversation_id: str | None = Field(default=None, max_length=128)
    topic: str | None = Field(default=None, max_length=2000)


class TimeRange(_ToolModel):
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None


class SearchRequest(_ToolModel):
    query: str = Field(min_length=1, max_length=2000)
    intent: SearchIntent | None = None
    scope: ToolScope = Field(default_factory=ToolScope)
    time_range: TimeRange | None = None
    types: list[str] = Field(default_factory=list)
    min_trust: Trust = "inferred"
    k: int = Field(default=12, ge=1, le=50)
    budget_tokens: int | None = Field(default=None, ge=100, le=4000)
    as_of: str | None = Field(default=None, max_length=64)
    # Bi-temporal query (the Zep/Graphiti pattern): the window in which records were
    # OBSERVED — as_of is "what was valid at that date", while these are "what was learned in that window".
    observed_from: str | None = Field(default=None, max_length=64)
    observed_to: str | None = Field(default=None, max_length=64)
    rerank: RerankMode = "off"


class RememberEvidence(_ToolModel):
    source_turn_id: str | None = None
    quote: str | None = Field(default=None, max_length=2000)


class RememberRequest(_ToolModel):
    content: str = Field(min_length=1, max_length=8000)
    kind: RememberKind
    key: str | None = Field(default=None, max_length=256)  # additive extension
    scope: ToolScope = Field(default_factory=ToolScope)
    evidence: RememberEvidence = Field(default_factory=RememberEvidence)
    policy: RememberPolicy = "stage"  # K30: inbox by default
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    supersedes: str | None = None


class ForgetRequest(_ToolModel):
    target_id: str = Field(min_length=1)
    mode: ForgetMode = "retract"
    new_value: str | None = Field(default=None, max_length=8000)
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _supersede_needs_value(self) -> ForgetRequest:
        if self.mode == "supersede" and not (self.new_value or "").strip():
            raise ValueError("mode=supersede requires new_value")
        return self


_REQUEST_MODELS: dict[str, type[_ToolModel]] = {
    "memory.search": SearchRequest,
    "memory.remember": RememberRequest,
    "memory.forget": ForgetRequest,
}

MEMORY_TOOLS: tuple[str, ...] = tuple(_REQUEST_MODELS)


def parse_tool_request(
    name: str, args: dict[str, Any] | None
) -> (
    SearchRequest
    | RememberRequest
    | ForgetRequest
):
    """Validate raw tool-call args at the boundary; raise a clean error if bad."""
    model = _REQUEST_MODELS.get(name)
    if model is None:
        raise ToolValidationError(f"unknown tool: {name!r} (available: {', '.join(MEMORY_TOOLS)})")
    try:
        return model.model_validate(args or {})  # type: ignore[return-value]
    except ValidationError as e:
        issues = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in e.errors()[:5]
        )
        raise ToolValidationError(f"{name}: {issues}") from e


# -- kind <-> key helpers ------------------------------------------------------


def derive_key(content: str, kind: str) -> str:
    """A stable key from free content: first words, kind-prefixed (non-fact)."""
    words = re.findall(r"\w+", content.lower(), flags=re.UNICODE)[:6]
    base = " ".join(words)[:64] or "not"
    return ensure_kind_prefix(base, kind)


def ensure_kind_prefix(key: str, kind: str) -> str:
    """``kind`` rides in the key namespace; ``fact`` is the bare default."""
    key = key.strip()
    if kind == "fact" or key.lower().startswith(f"{kind}:"):
        return key
    return f"{kind}:{key}"


def kind_from_key(key: str) -> str:
    head, sep, _rest = key.partition(":")
    if sep and head.strip().lower() in _REMEMBER_KINDS:
        return head.strip().lower()
    return "fact"


# -- time helpers --------------------------------------------------------------

_RELATIVE_RE = re.compile(r"^relative:(\d+)([hdw])$", re.IGNORECASE)


def parse_time_point(value: str | None, *, now: datetime | None = None) -> str | None:
    """Normalize an ISO timestamp or ``relative:<n><h|d|w>`` to a comparable
    ISO-UTC string (the stores' millisecond-Z format). Offset-aware inputs
    (``+03:00``) are converted to UTC; naive inputs are assumed UTC.
    ``None``/unparseable → ``None``."""
    if not value:
        return None
    value = value.strip()
    m = _RELATIVE_RE.match(value)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
        point = (now or datetime.now(UTC)) - delta
        return point.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


# The Turkish natural-language time-expression parser (bugün/dün/geçen hafta/
# mart ayında/…) lives in time_expressions.py; parse_time_range is re-exported
# above (via the module import) so existing "from akana.memory.tools import
# parse_time_range" call sites are unaffected by the split.


def parse_time_bound(
    value: str | None, *, edge: TimeEdge = "start", now: datetime | None = None
) -> str | None:
    """A single time bound: an ISO / ``relative:`` point, or an end of a Turkish range.

    ``edge`` says which end of a Turkish expression to take — ``observed_from=
    "geçen hafta"`` the start of the range, ``observed_to="geçen hafta"`` the end.
    A date-only ISO (``2026-03-05``) expands at ``edge="end"`` to the last millisecond
    of the day (inclusive day; ``as_of=date`` = "as of the end of that day").
    ``None``/an unrecognized expression → ``None``.
    """
    if not value:
        return None
    v = value.strip()
    if edge == "end" and DATE_ONLY_RE.match(v):
        return parse_time_point(f"{v}T23:59:59.999Z", now=now)
    point = parse_time_point(v, now=now)
    if point is not None:
        return point
    rng = parse_time_range(v, now=now)
    if rng is None:
        return None
    return rng[0] if edge == "start" else rng[1]


# -- JSON Schema definitions (Vision §8, handed to the LLM SDK) -----------------

_SCOPE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "conversation_id": {"type": "string"},
        "topic": {"type": "string"},
    },
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "memory.search",
        "description": (
            "Search Akana's memory. Personal facts, past conversations, learned "
            "lessons, and concepts — all through a single tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "natural-language query"},
                "intent": {
                    "type": "string",
                    "enum": [
                        "fact_lookup",
                        "episodic",
                        "timeline",
                        "explore",
                        "skill_context",
                        "lesson_lookup",
                        "concept_lookup",
                    ],
                    "description": (
                        "fact_lookup: a single atomic fact. episodic: a specific conversation "
                        "moment. timeline: period-based. explore: free/discovery. skill_context: "
                        "before a skill run. lesson_lookup: which lessons I know."
                    ),
                },
                "scope": _SCOPE_SCHEMA,
                "time_range": {
                    "type": "object",
                    "properties": {
                        "from": {
                            "type": "string",
                            "description": "ISO, 'relative:7d', or Turkish natural language ('dün', 'geçen hafta', 'mart ayında')",
                        },
                        "to": {"type": "string"},
                    },
                },
                "types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "filter — e.g. [Fact, Lesson, Episode]",
                },
                "min_trust": {
                    "type": "string",
                    "enum": ["user_statement", "inferred", "tool_output", "synthesis"],
                    "default": "inferred",
                },
                "k": {"type": "integer", "default": 12, "minimum": 1, "maximum": 50},
                "budget_tokens": {
                    "type": "integer",
                    "minimum": 100,
                    "maximum": 4000,
                    "description": "token budget; if omitted the intent default is used (200-1500)",
                },
                "as_of": {
                    "type": "string",
                    "description": (
                        "time-travel — search memory AS OF this date (supported). "
                        "ISO date ('2026-01-01'), 'relative:7d', or Turkish natural language "
                        "('dün', 'geçen hafta', 'mart ayında'); returns the values that were "
                        "valid on that date (even if later changed/superseded)."
                    ),
                },
                "observed_from": {
                    "type": "string",
                    "description": (
                        "bi-temporal observation filter start — only records observed/learned "
                        "AFTER this moment. ISO, 'relative:7d', or Turkish natural language "
                        "('dün', 'geçen hafta', 'mart ayında')."
                    ),
                },
                "observed_to": {
                    "type": "string",
                    "description": (
                        "bi-temporal observation filter end — only records observed UP TO "
                        "this moment. ISO, 'relative:7d', or Turkish natural language; for "
                        "natural language the END of the range is used ('dün' → end of yesterday)."
                    ),
                },
                "rerank": {"type": "string", "enum": ["off", "cross_encoder"], "default": "off"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory.remember",
        "description": "Save a piece of information. Per policy it goes to staging (inbox) or directly to memory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string"},
                "kind": {
                    "type": "string",
                    "enum": list(_REMEMBER_KINDS),
                },
                "key": {"type": "string", "description": "optional key; derived from the content if omitted"},
                "scope": _SCOPE_SCHEMA,
                "evidence": {
                    "type": "object",
                    "properties": {
                        "source_turn_id": {"type": "string"},
                        "quote": {"type": "string"},
                    },
                },
                "policy": {
                    "type": "string",
                    "enum": ["stage", "direct"],
                    "default": "stage",
                    "description": "stage: lands in the approval inbox (default). direct: the server may still downgrade to stage per K30.",
                },
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "supersedes": {"type": "string", "description": "old entity id (if any)"},
            },
            "required": ["content", "kind"],
        },
    },
    {
        "name": "memory.forget",
        "description": (
            "Forget a piece of information. To delete the WHOLE record: retract / soft_delete. "
            "To forget ONLY PART of a record (when it holds several facts — e.g. value "
            "'Ali, 30 years old, Istanbul' but you only want to forget the city): "
            "mode=supersede + new_value=the remaining content ('Ali, 30 years old'). That way "
            "the other facts are KEPT and only that part is removed — do not delete the whole record."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "target_id": {"type": "string"},
                "mode": {
                    "type": "string",
                    "enum": ["retract", "supersede", "soft_delete", "quarantine"],
                    "default": "retract",
                },
                "new_value": {
                    "type": "string",
                    "description": (
                        "the NEW full content of the record in supersede mode. For partial "
                        "forgetting: remove only the forgotten part from the old value and keep the rest verbatim."
                    ),
                },
                "reason": {"type": "string"},
            },
            "required": ["target_id"],
        },
    },
]


def tool_schemas() -> list[dict[str, Any]]:
    """A fresh copy of the ``memory.*`` tool definitions (SDK-agnostic shape)."""
    import copy

    return copy.deepcopy(TOOL_SCHEMAS)
