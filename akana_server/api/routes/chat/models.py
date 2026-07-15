"""Chat turn API schemas — request/response pydantic models.

The first seam split from the `chat/__init__.py` god-file (Step B2). These models
are NOT dependent on the chat internals (only pydantic/typing) → an isolated, safe
extraction. `__init__.py` re-imports them; external accesses like
`routes.chat.ChatRequest` and FastAPI body-annotations keep working unchanged.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class ChatRequest(BaseModel):
    text: str = Field(default="", max_length=32000)
    lang: str | None = Field(default=None, max_length=16)
    conversation_id: str | None = Field(default=None, max_length=64)
    #: MultimodalEngine F1: image ids uploaded via /api/v1/uploads.
    #: A BACKWARD-COMPATIBLE field — the new client sends ``file_ids``; the two
    #: merge in ``effective_file_ids`` (order preserved, de-duplicated).
    image_ids: list[str] = Field(default_factory=list, max_length=30)
    #: PHASE2 multi-type file binding: ids of ANY type (image/pdf/text/...) uploaded
    #: via /api/v1/uploads. Resolved per the active provider
    #: (claude → a provider-native [Dosya: <path>] block; cursor/unsupported →
    #: a Turkish "I can't read it" note without dropping the turn). max_length=30 is
    #: only a SECURITY CEILING — the actual provider-specific per-message/per-conversation
    #: limit is enforced on the frontend (PROVIDER_ATTACH_LIMITS); claude is 20 images +
    #: 10 files per message.
    file_ids: list[str] = Field(default_factory=list, max_length=30)
    #: Thinking mode (increasing effort). TWO vocabularies share this field:
    #:  • Akana canonical tiers (hizli/normal/derin/yogun/azami/ultra) — used by the
    #:    claude/gemini providers, which map them onto their native knob. "hizli" forces
    #:    the fast-path (planner.route skipped), "normal" = auto fast-path on short+simple
    #:    messages, "derin" and up always run the gates in full. "ultra" additionally
    #:    appends the "ultracode" keyword on fable models (claude only).
    #:  • Provider-NATIVE effort levels (minimal/low/medium/high/xhigh) — codex and openai
    #:    expose their own reasoning-effort names directly in the composer and send the
    #:    chosen level VERBATIM (no Akana-tier mapping); the provider passes it straight
    #:    to ``model_reasoning_effort`` / ``reasoning_effort``.
    #: The union is accepted here; each provider's table interprets the value and defaults
    #: safely on one it does not recognise. Cursor/Ollama receive the field but ignore it.
    thinking_mode: Literal[
        "hizli", "normal", "derin", "yogun", "azami", "ultra",
        "minimal", "low", "medium", "high", "xhigh",
    ] = "normal"
    #: Plan-mode turn (claude only): runs with ``--permission-mode plan`` — before
    #: writing/applying, the model produces a plan and presents it via ``ExitPlanMode``,
    #: and Akana converts it to a structured ``plan`` event. When the user says "Apply",
    #: the session continues with plan mode OFF via ``--resume`` and applies the plan.
    #: Cursor/Ollama ignore it.
    plan_mode: bool = False
    #: Voice conversation mode turn — the response should be SHORT and suited to spoken
    #: language (the user will listen). The displayed/stored user message is UNCHANGED;
    #: only a keep-it-short directive is added to the prompt going to the LLM.
    voice: bool = False
    #: b6: the resolved TTS language for a hands-free voice turn. Normally passed as a query
    #: param on the streaming request; carried on the BODY so a queued (202) voice turn keeps
    #: its TTS when later drained (otherwise the drained reply was computed but never spoken).
    tts: str | None = Field(default=None, max_length=16)

    @property
    def effective_file_ids(self) -> list[str]:
        """The union of ``file_ids`` + ``image_ids`` (order preserved, unique).

        In PHASE2 the client sends ``file_ids``; older clients may keep sending
        ``image_ids``. Both fields carry the same UploadStore ids; resolution goes
        through a single gate (``_files_gate``).
        """
        merged: list[str] = []
        seen: set[str] = set()
        for raw in [*(self.file_ids or []), *(self.image_ids or [])]:
            fid = str(raw).strip()
            if fid and fid not in seen:
                seen.add(fid)
                merged.append(fid)
        return merged

    @model_validator(mode="after")
    def _require_text_or_files(self) -> "ChatRequest":
        """Empty/whitespace-only text is accepted ONLY if there's an attachment (else 422).

        ``min_length`` was removed: an attachment-only message (sending an image/file
        without typing text) is now valid. The text is NOT trimmed; if ``strip()`` is
        empty AND there's no attachment (file_ids/image_ids), it's rejected so it doesn't
        leak into an empty LLM turn."""
        if not self.text.strip() and not self.effective_file_ids:
            raise ValueError("a non-empty 'text' field or at least one attachment is required")
        return self


class TokenUsage(BaseModel):
    prompt: int = 0
    completion: int = 0


class ChatResponse(BaseModel):
    turn_id: str
    text: str
    lang: str | None = None
    conversation_id: str
    history_turns: int = 0
    dropped_turns: int = 0
    intent: str = "chat"
    action: str | None = None
    approval_required: bool = False
    plan: dict[str, Any] | None = None
    tool_calls: list[Any] = Field(default_factory=list)
    memory_writes: list[Any] = Field(default_factory=list)
    # WI-1/WI-2: skills injected into / awaiting approval for / rejected for the turn
    # ({id, title, score, match_reason, status, ...}) — the UI will be wired later.
    skill_used: list[Any] = Field(default_factory=list)
    latency_ms: int = 0
    tokens: TokenUsage = Field(default_factory=TokenUsage)
