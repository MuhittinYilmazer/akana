"""ContextAssembler — turn context assembler (ContextEngine F0).

Behavior-neutral contract: the assembler preserves the EXISTING composition
shape exactly —

* system prompt  : ``persona.resolve(channel, conversation_id, skill)``;
  if the default ``akana`` is resolved, :attr:`AssembledContext.system_prompt_override`
  returns ``None`` and the LLM clients put ``CHAT_SYSTEM_PREFIX`` themselves as
  they do today (the payload stays byte-for-byte identical).
* user text      : ``[Yetenek: ...]`` skill block + (memory-injected) text —
  ``f"{skill_block}\\n\\n{user_text}"`` (the historical formula in chat.py).
* plan block     : the ``[Onaylı plan — uygulama]`` text has already REPLACED
  ``body.text`` at the plan gate (``build_execution_text``); the assembler does
  not rebuild it, it only records it in the trace (so the why-answer is not
  lost). (``[Yetenek: ...]`` and ``[Onaylı plan — uygulama]`` are load-bearing
  prompt markers shared with the rest of the pipeline — kept verbatim.)
* history        : the ``chat_max_turns`` window; when resume is active the
  episodic read is skipped (``async_llm_history_for_assemble``).

Budget (single place): :func:`context_budget_chars` — if the total characters
(system + history contents + user text) exceed the limit, the trimming order
is: history first (from the oldest message), then the skill block (dropped
entirely; a half ``[Yetenek]`` block misleads the LLM), system and user text
are NEVER trimmed. The default limit is intentionally high (it is not triggered
in the normal flow — behavior-neutrality); it is tuned via
``AKANA_CONTEXT_MAX_CHARS``, ``0`` = unlimited.

Error contract: a persona/memory failure CANNOT break the turn (it continues
with the builtin persona / raw text); a history failure propagates upward as it
does today (chat.py behavior).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

from akana_server.chat_context import async_llm_history_for_assemble
from akana_server.config import Settings
from akana_server.persona.builtin import CHAT_SYSTEM_PREFIX, DEFAULT_PERSONA_ID
from akana_server.persona.models import Persona
from akana_server.persona.registry import get_persona_registry
from akana_server.skills.catalog import resolve_catalog
from akana.memory.summary_types import SummaryView

log = logging.getLogger(__name__)

#: Injection seam type — the assembler CONSUMES this; the lead wires the real
#: implementation at integration (``get_session_summary``).
#: "The LLM is injected, not owned": it is optional and defaults to ``None`` →
#: behavior-neutral (no prior-context block).
SummaryProvider = Callable[[str], "SummaryView | None"]

#: Bilingual block labels (model-facing markers) — keyed by the ``language``
#: runtime setting. EN is the first-class default; ``tr`` mirrors the existing
#: ``[Yetenek]`` / ``[Onaylı plan]`` Turkish marker style.
_PRIOR_CONTEXT_LABEL = {"en": "[Prior context]", "tr": "[Önceki bağlam]"}

#: Minimum summary chars that must fit under the ``[Prior context]`` header +
#: newline before injection is worthwhile — a smaller budget yields a hollow
#: marker (a half-header misleads the LLM), so the block is dropped whole.
_MIN_PRIOR_CONTEXT_PAYLOAD = 20


def _resolve_label(table: dict[str, str], language: str) -> str:
    """Pick a bilingual marker for ``language`` (``en`` default on any miss)."""
    return table.get(language, table["en"])

#: Default total context budget (characters). Intentionally high: F0 is
#: behavior-neutral — normal chat does not hit this limit; real tightening is a
#: config job.
DEFAULT_MAX_CONTEXT_CHARS = 120_000


def context_budget_chars() -> int:
    """Total context character limit — SINGLE source.

    Resolution chain: runtime setting (``context_max_chars``) >
    ``AKANA_CONTEXT_MAX_CHARS`` > default. ``0`` = unlimited.

    Both layers go through the runtime-settings schema (min=0/max=2,000,000,
    NaN/Inf and out-of-range → default) so the effective budget here matches what
    ``GET /settings/runtime`` reports — a hand-rolled ``int(env)`` parse used to
    accept out-of-bounds values (e.g. ``-1`` as unlimited, ``3000000`` uncapped)
    that the settings endpoint clamped away, so the two disagreed.
    """
    try:
        from akana_server.runtime_settings import runtime_override
        from akana_server.runtime_settings.resolve import _env_fallback
        from akana_server.runtime_settings.schema import SCHEMA

        rt = runtime_override("context_max_chars")
        if rt is not None:
            return int(rt)
        _set, value = _env_fallback(SCHEMA["context_max_chars"])
        return int(value)
    except Exception:  # the runtime layer must never break context assembly
        return DEFAULT_MAX_CONTEXT_CHARS


@dataclass(slots=True)
class ContextRequest:
    """A turn's context inputs — everything chat.py hands to the assembler.

    ``text`` is the FINAL user text sent to the LLM (skill block + memory are
    composed on top of it inside the assembler).
    """

    text: str
    conversation_id: str
    channel: str = "web"
    skill: str | None = None  # skill_run/work-mode turn (D12.E contract)
    skill_block: str = ""  # SkillTurnPlan.prompt_block ([Yetenek: ...])
    skill_entries: list[dict[str, Any]] = field(default_factory=list)
    #: MultimodalEngine F1: `[Görsel: <path>]` lines — appended to the end of the
    #: user text; like the user text, NEVER trimmed in the budget.
    image_block: str = ""


@dataclass(slots=True)
class AssembledContext:
    """Assembled turn context + the "why it is this way" trace."""

    system_prompt: str
    history: list[dict[str, str]]
    user_text: str  # final user message sent to the LLM (incl. skill block + memory)
    injected_blocks: list[dict[str, Any]] = field(default_factory=list)
    trace: dict[str, Any] = field(default_factory=dict)
    memory_trace: list[dict[str, Any]] = field(default_factory=list)
    dropped_turns: int = 0
    history_skipped_resume: bool = False
    persona_id: str = DEFAULT_PERSONA_ID
    persona_source: str = "builtin"
    system_prompt_is_default: bool = True

    @property
    def system_prompt_override(self) -> str | None:
        """System prompt passed to the LLM client — ``None`` for the default akana.

        Returning ``None`` is the key to behavior-neutrality: when no system is
        given, the cursor/claude clients put ``CHAT_SYSTEM_PREFIX`` as they do
        today; the payload does not change. The override is sent only when a
        REAL persona binding is resolved.
        """
        return None if self.system_prompt_is_default else self.system_prompt


def _compose_user_text(skill_block: str, user_text: str, image_block: str = "") -> str:
    """Historical composition formula — the single source of the prepend in chat.py.

    The image block (F1) is appended to the END of the user text; the skill
    block to the front (the existing formula does not change — when image_block
    is empty the output is byte-for-byte identical).
    """
    out = user_text
    if image_block:
        out = f"{out}\n\n{image_block}"
    if skill_block:
        out = f"{skill_block}\n\n{out}"
    return out


def _render_prior_context(
    view: SummaryView, language: str, *, max_chars: int | None = None
) -> str:
    """Render a non-empty ``SummaryView`` as a compact, marked prior-context block.

    Shape: ``[Prior context]`` / ``[Önceki bağlam]`` header on its own line, then
    the summary paragraph. Returns ``""`` for an empty view so the caller never
    prepends a hollow marker (a half block misleads the LLM, like ``[Yetenek]``).

    ``max_chars`` (when truthy) hard-clips the SUMMARY so a long rolling summary
    can't silently eat the turn's context budget. The clip is marker-aware: it
    never cuts into the ``[Prior context]`` header (a budget of 1..14 would
    otherwise emit a broken ``[Prior c`` fragment) — a budget too small to hold
    the header plus a minimal payload drops the block entirely, like an empty view.
    """
    if view.is_empty:
        return ""
    header = _resolve_label(_PRIOR_CONTEXT_LABEL, language)
    # Newline + a minimal payload must fit under the header, or the block is a
    # hollow marker — skip injection rather than prepend a half marker.
    if max_chars and max_chars < len(header) + _MIN_PRIOR_CONTEXT_PAYLOAD:
        return ""
    block = f"{header}\n{view.summary}"
    if max_chars and len(block) > max_chars:
        block = block[:max_chars].rstrip()
    return block


class ContextAssembler:
    """Assembles the turn context through a single gate (persona + skill + plan + memory + history)."""

    def __init__(
        self,
        request: Request,
        *,
        summary_provider: SummaryProvider | None = None,
        summary_inject_max_chars: int | None = None,
    ) -> None:
        self._request = request
        #: (B) Prior-session summary lookup by conversation_id. ``None`` (default)
        #: → no prior-context injection (behavior-neutral). The chat routes wire the
        #: real ``get_session_summary`` via ``build_context_assembler`` (gated on the
        #: ``session_summary_inject_enabled`` runtime setting).
        self._summary_provider = summary_provider
        #: (B) Hard cap (chars) on the rendered «Prior context» block. ``None``/``0``
        #: → no cap (whole summary injected). The factory feeds the runtime value
        #: (``session_summary_inject_max_chars``) so a long rolling summary can't
        #: silently consume the turn's context budget.
        self._summary_inject_max_chars = summary_inject_max_chars

    # -- component readers (each defensive) ----------------------------------- #

    def _active_language(self) -> str:
        """Active prompt language (``en`` | ``tr``) from the ``language`` runtime
        setting; any failure → ``"en"`` (English-first default, same chain as the
        voice/persona/catalog readers)."""
        try:
            from akana_server.runtime_settings import get_runtime

            settings: Settings = self._request.app.state.settings
            lang = str(get_runtime("language", settings) or "en").strip().lower()
            return lang if lang in ("tr", "en") else "en"
        except Exception:  # the runtime/language layer must never break assembly
            return "en"

    def _prior_context_view(self, ctx: ContextRequest) -> SummaryView | None:
        """(B) Resolve the prior-session summary for this conversation (defensive).

        Returns ``None`` when no provider is injected, the conversation has no
        id, the provider yields nothing, or the view is empty — in every one of
        those cases NOTHING is injected (behavior-neutral)."""
        if self._summary_provider is None:
            return None
        conv = (ctx.conversation_id or "").strip()
        if not conv:
            return None
        try:
            view = self._summary_provider(conv)
        except Exception:  # a summary lookup failure must never break the turn
            log.warning("prior-context summary lookup failed", exc_info=True)
            return None
        if view is None or view.is_empty:
            return None
        return view

    def _resolve_persona(self, ctx: ContextRequest) -> Persona | None:
        """persona.resolve() — D12.E contract; ANY failure → ``None`` (fall back to builtin)."""
        try:
            settings: Settings = self._request.app.state.settings
            reg = get_persona_registry(settings.data_dir)
            # Duck-typed discovery of pack personas (same seam as routes/personas;
            # idempotent — if the host is not set up, continue with builtin + user).
            host = getattr(self._request.app.state, "pack_host", None)
            adapter = getattr(host, "personas_adapter", None)
            if adapter is not None:
                reg.attach_pack_source(adapter)
            return reg.resolve(
                channel=ctx.channel or None,
                conversation_id=(ctx.conversation_id or "").strip() or None,
                skill=ctx.skill,
            )
        except Exception:  # persona is an enhancement — it must never break the turn
            log.warning(
                "persona resolution failed; using builtin akana", exc_info=True
            )
            return None

    def _resolve_capability_catalog(self) -> str:
        """Installed capability inventory (WI-2) — block to add to the system prompt.

        ``catalog.resolve_catalog`` swallows the gate + registry + format in a
        single gate; empty registry / disabled toggle / failure → "" (the system
        prompt does not change)."""
        try:
            settings: Settings = self._request.app.state.settings
        except Exception:  # if the app.state seam is missing, skip the catalog
            return ""
        return resolve_catalog(settings)

    # -- assembly --------------------------------------------------------------- #

    async def assemble(self, ctx: ContextRequest) -> AssembledContext:
        """Assemble the turn context — components are read in parallel (the SSE-path pattern)."""
        history_task = asyncio.create_task(
            async_llm_history_for_assemble(self._request, ctx.conversation_id)
        )
        persona_task = asyncio.create_task(
            asyncio.to_thread(self._resolve_persona, ctx)
        )
        catalog_task = asyncio.create_task(
            asyncio.to_thread(self._resolve_capability_catalog)
        )
        # (B) prior-session summary lookup hits sqlite (json_metadata read), so it
        # rides the SAME off-loop fan-out as persona/memory/catalog — a per-turn DB
        # read on the event loop would violate the P0 stability pattern this path
        # follows. Provider None → returns None instantly (no DB hit).
        prior_task = asyncio.create_task(
            asyncio.to_thread(self._prior_context_view, ctx)
        )
        try:
            history, dropped, history_skipped_resume = await history_task
        except BaseException:
            # a history failure propagates upward (existing chat.py behavior) —
            # but the sibling tasks must not be left orphaned.
            persona_task.cancel()
            catalog_task.cancel()
            prior_task.cancel()
            raise
        persona = await persona_task
        # NOTE: the v1 in-prompt memory injection was retired — recall now comes
        # from the `memory_search` MCP tool the LLM calls mid-turn, not a prompt
        # block assembled here. `mem_text` is therefore always the raw user text
        # and `memory_trace` is always empty; both are kept only because
        # ``AssembledContext.memory_trace`` is still read downstream
        # (chat_producer._sse_memory_use, currently always a no-op).
        mem_text, memory_trace = ctx.text, []
        catalog_block = await catalog_task
        prior_view = await prior_task

        # System prompt: the resolved persona; on failure/default the builtin text.
        if persona is None:
            system_prompt = CHAT_SYSTEM_PREFIX
            persona_id, persona_source = DEFAULT_PERSONA_ID, "builtin"
        else:
            system_prompt = persona.system_prompt
            persona_id, persona_source = persona.id, persona.source
        is_default = (
            persona_id == DEFAULT_PERSONA_ID and system_prompt == CHAT_SYSTEM_PREFIX
        )

        # -- Capability catalog (WI-2): installed skill/pack inventory ---------- #
        # Added AFTER the persona base, BEFORE the tone hint (so the hint stays
        # last). Empty registry → "" → nothing is added (behavior-neutral); when
        # populated the override must REALLY be sent (otherwise the client puts a
        # plain prefix).
        catalog = catalog_block or ""
        if catalog:
            system_prompt = f"{system_prompt}\n\n{catalog}"
            is_default = False

        # Active language for the bilingual injected markers (resolved once).
        language = self._active_language()

        # -- (B) prior-session summary injection -------------------------------- #
        # ``prior_view`` was resolved off-loop above (parallel fan-out). When a
        # provider is wired AND returns a non-empty view for this conversation,
        # render an `[Prior context]` / `[Önceki bağlam]` block and prepend it to
        # the raw user text. Provider None → prior_view None → prior_block "" →
        # body_text == mem_text == ctx.text (behavior-neutral). The block is clipped
        # to the injection budget so a long rolling summary can't eat the turn.
        prior_block = (
            _render_prior_context(
                prior_view, language, max_chars=self._summary_inject_max_chars
            )
            if prior_view
            else ""
        )

        # -- budget: trim from a single place (history first, skill next, system never) -- #
        budget = context_budget_chars()
        history = list(history)
        skill_block = ctx.skill_block or ""
        image_block = ctx.image_block or ""
        trimmed: list[dict[str, Any]] = []

        # Final order: [Yetenek] → [Prior context] + [memory + raw text] → [Görsel].
        # (bracket tokens are load-bearing prompt markers — kept verbatim.)
        def _body() -> str:
            parts = [p for p in (prior_block, mem_text) if p]
            return "\n\n".join(parts) if parts else mem_text

        def _total() -> int:
            return (
                len(system_prompt)
                + sum(len(str(m.get("content") or "")) for m in history)
                + len(_compose_user_text(skill_block, _body(), image_block))
            )

        total_before = _total()
        if budget > 0:
            while history and _total() > budget:
                victim = history.pop(0)  # the oldest message drops first
                victim_content = str(victim.get("content") or "")
                trimmed.append(
                    {
                        "kind": "history",
                        "role": str(victim.get("role") or "?"),
                        "chars": len(victim_content),
                        "reason": "context budget exceeded — oldest message dropped",
                    }
                )
            if skill_block and _total() > budget:
                trimmed.append(
                    {
                        "kind": "skill",
                        "chars": len(skill_block),
                        "reason": (
                            "context budget still exceeded after trimming history —"
                            " skill block dropped entirely (a half block misleads the LLM)"
                        ),
                    }
                )
                skill_block = ""
        total_after = _total()

        body_text = _body()
        user_text = _compose_user_text(skill_block, body_text, image_block)

        # -- injected blocks + trace --------------------------------------------- #
        injected: list[dict[str, Any]] = []
        if prior_block and prior_view is not None:
            injected.append(
                {
                    "kind": "prior_context",
                    "chars": len(prior_block),
                    "conversation_id": prior_view.conversation_id,
                    "reason": (
                        "conversation start/resume — prior session summary"
                        " injected as the [Prior context] block (B)"
                    ),
                }
            )
        if image_block:
            injected.append(
                {
                    "kind": "image",
                    "chars": len(image_block),
                    "count": image_block.count("[Görsel:"),
                    "reason": (
                        "image input (image_ids) — [Görsel: <path>] lines"
                        " appended to the end of user text (MultimodalEngine F1)"
                    ),
                }
            )
        if skill_block:
            injected.append(
                {
                    "kind": "skill",
                    "chars": len(skill_block),
                    "reason": "strong skill match — [Yetenek] block prepended to prompt (WI-1)",
                    "entries": [
                        {"id": e.get("id"), "status": e.get("status")}
                        for e in ctx.skill_entries
                        if isinstance(e, dict)
                    ],
                }
            )
        if memory_trace:
            injected.append(
                {
                    "kind": "memory",
                    "chars": max(0, len(mem_text) - len(ctx.text)),
                    "count": len(memory_trace),
                    "reason": "hybrid memory recall appended to the user text",
                }
            )

        trace: dict[str, Any] = {
            "persona": {
                "id": persona_id,
                "source": persona_source,
                "default": is_default,
                "chars": len(system_prompt),
                "channel": ctx.channel,
                "reason": (
                    "no binding — builtin akana (CHAT_SYSTEM_PREFIX)"
                    if is_default
                    else "persona binding resolved (skill > conversation > channel priority)"
                ),
            },
            "history": {
                "turns": len(history),
                "chars": sum(len(str(m.get("content") or "")) for m in history),
                "dropped_turns": dropped,
                "skipped_resume": history_skipped_resume,
                "trimmed_for_budget": sum(1 for t in trimmed if t["kind"] == "history"),
                "reason": (
                    "session resume — history is in the agent; episodic read skipped"
                    if history_skipped_resume
                    else "chat_max_turns window (episodic archive source)"
                ),
            },
            "user_text": {"chars": len(user_text), "raw_chars": len(ctx.text)},
            "capability_catalog": {
                "applied": bool(catalog),
                "chars": len(catalog),
                "reason": (
                    "installed capability inventory (title+trigger) added to system prompt (WI-2)"
                    if catalog
                    else "catalog empty/disabled — system prompt unchanged"
                ),
            },
            "prior_context": {
                "applied": bool(prior_block),
                "chars": len(prior_block),
                "reason": (
                    "prior session summary injected as the [Prior context] block (B)"
                    if prior_block
                    else "no provider / empty summary — no prior-context injection (behavior-neutral)"
                ),
            },
            "injected_blocks": injected,
            "budget": {
                "max_chars": budget,
                "total_chars_before": total_before,
                "total_chars_after": total_after,
                "trimmed": trimmed,
            },
        }

        return AssembledContext(
            system_prompt=system_prompt,
            history=history,
            user_text=user_text,
            injected_blocks=injected,
            trace=trace,
            memory_trace=memory_trace,
            dropped_turns=dropped,
            history_skipped_resume=history_skipped_resume,
            persona_id=persona_id,
            persona_source=persona_source,
            system_prompt_is_default=is_default,
        )


__all__ = [
    "DEFAULT_MAX_CONTEXT_CHARS",
    "AssembledContext",
    "ContextAssembler",
    "ContextRequest",
    "SummaryProvider",
    "context_budget_chars",
]
