"""WI-1 — per-turn skill injection (suggest → inject).

Flow (chat.py calls this at the start of every turn, BEFORE the LLM dispatch):

1. ``SkillRegistry.suggest_for_text(user_text)`` — in a short-timeout thread;
   ANY error/slowness lets the turn proceed without injection, never breaks it.
2. Strong-match filter: the ``match_reason == "trigger_exact"`` short-circuit OR
   those whose RRF score passes the config threshold (the top N).
3. Injection: the SKILL.md body (L2) is prepended to the agent prompt as a
   ``[Capability: <id> — <name>]`` block (bilingual; the ``[Yetenek: ...]`` form is
   used when ``language=tr``); MCP servers the skill requires but that are not
   mounted are noted in the block as a missing-tool signal.

FULL AUTONOMY (owner's decision): there is no approval gate. Every strong-match
skill — including any flagged ``requires_approval`` — is injected directly; that
flag is now inert advisory metadata, never a stop-and-confirm reflex.

Config (RuntimeSettings chain: runtime_settings.json > env > default —
changeable from settings without a restart):

* ``AKANA_SKILL_INJECT``            — "0/false/off" → fully disabled (default on).
* ``AKANA_SKILL_INJECT_THRESHOLD``  — min RRF score for a non-trigger match
  (default ``0.03`` ≈ at least two search layers rank the same skill highly).
* ``AKANA_SKILL_INJECT_MAX``        — max number of skills injected per turn (default 1).
* ``AKANA_SKILL_SUGGEST_TIMEOUT_S`` — time budget for the suggestion search, seconds (default 1.5).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from akana_server.skills.registry import SkillRegistry, get_registry
from akana_server.skills.skill_resolve import resolve_skill_servers

log = logging.getLogger(__name__)

__all__ = [
    "SkillTurnPlan",
    "plan_skill_turn",
]

_DEFAULT_THRESHOLD = 0.03
_DEFAULT_MAX = 1
_DEFAULT_TIMEOUT_S = 1.5


def _runtime(key: str, settings: Any, default: Any) -> Any:
    """RuntimeSettings chain (runtime > env > default) — never raises."""
    try:
        from akana_server.runtime_settings import get_runtime

        return get_runtime(key, settings)
    except Exception:
        log.warning("could not resolve skill runtime setting (%s); using default", key)
        return default


def _catalog_on(settings: Any) -> bool:
    """Master gate (owner decision): the catalog toggle also governs injection.

    When the catalog is OFF the model gets NO skill awareness (WI-2) AND no
    injection (WI-1) — nothing is sent. A resolution failure returns True so a
    transient error never silently suppresses injection."""
    try:
        from akana_server.skills.catalog import catalog_enabled

        return bool(catalog_enabled(settings))
    except Exception:
        return True


def _allowed_ids(settings: Any) -> set[str] | None:
    """Catalog selection that gates injection (None = all; the WI-2 source of truth).

    Read fresh every turn (no cache) so a mid-conversation enable/disable takes
    effect on the next turn. Any failure → None (no filter), never breaks the turn."""
    try:
        from akana_server.skills.catalog import catalog_include_ids

        return catalog_include_ids(settings)
    except Exception:
        return None


@dataclass(slots=True)
class SkillTurnPlan:
    """A turn's skill decision — the source of the ChatResponse/SSE ``skill_used`` payload.

    * ``injected``: skills that entered the prompt (``status="injected"``).
    * ``blocked``: those not injected due to an error (``status="error"``, with a ``reason``).
    * ``prompt_block``: the ``[Capability: ...]`` blocks to prepend to the agent prompt.
    """

    injected: list[dict[str, Any]] = field(default_factory=list)
    blocked: list[dict[str, Any]] = field(default_factory=list)
    prompt_block: str = ""

    @property
    def has_signal(self) -> bool:
        return bool(self.injected or self.blocked)

    def used_payload(self) -> list[dict[str, Any]]:
        """Contents of the ChatResponse.skill_used / SSE ``skill_used`` event."""
        out = [dict(e) for e in self.injected]
        out.extend(dict(e) for e in self.blocked)
        return out


def _entry_payload(suggestion: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(suggestion.get("id") or ""),
        "title": str(suggestion.get("title") or suggestion.get("id") or ""),
        "score": suggestion.get("score"),
        "match_reason": str(suggestion.get("match_reason") or ""),
        "requires_approval": bool(suggestion.get("requires_approval")),
        "risk": str(suggestion.get("risk") or "low"),
    }


# Skill-injection block is MODEL-FACING (prepended to the turn prompt), so it is
# BILINGUAL and follows the ``language`` runtime setting like the persona/catalog.
_SKILL_BLOCK = {
    "en": ("[Capability: {id} — {title}]", "[/Capability]"),
    "tr": ("[Yetenek: {id} — {title}]", "[/Yetenek]"),
}
_MISSING_TOOLS_NOTE = {
    "en": (
        "(Missing-tool signal: these MCP servers are NOT connected this session: {servers} "
        "— you cannot call these tools, do not fabricate results; if needed, suggest the "
        "relevant setup skill to the user for approval.)"
    ),
    "tr": (
        "(Eksik araç sinyali: şu MCP sunucuları bu oturumda bağlı değil: {servers} "
        "— bu araçları çağıramazsın, sonuç uydurma; gerekiyorsa kullanıcıya ilgili "
        "kurulum skill'ini onaylı şekilde öner.)"
    ),
}


def _format_block(
    entry: dict[str, Any], body: str, missing: list[str], language: str = "en"
) -> str:
    lang = language if language in ("en", "tr") else "en"
    open_mark, close_mark = _SKILL_BLOCK[lang]
    lines = [open_mark.format(id=entry["id"], title=entry["title"]), body.strip()]
    if missing:
        lines.append(_MISSING_TOOLS_NOTE[lang].format(servers=", ".join(missing)))
    lines.append(close_mark)
    return "\n".join(lines)


async def plan_skill_turn(
    settings: Any,
    user_text: str,
    *,
    registry: SkillRegistry | None = None,
    timeout_s: float | None = None,
) -> SkillTurnPlan:
    """Build the turn's skill injection plan (the WI-1 entry point).

    FULL AUTONOMY: every strong-match skill is injected; there is no approval gate.
    ``timeout_s`` overrides the suggestion-search budget per call (the chat
    fast-path gives 0.5s; None → env/default). Error guarantee: no exception
    leaks; at worst an empty plan is returned and the turn proceeds without skills.
    """
    plan = SkillTurnPlan()
    if not bool(_runtime("skill_inject_enabled", settings, True)):
        return plan
    # Catalog is the master gate: catalog OFF → no awareness AND no injection.
    if not _catalog_on(settings):
        return plan
    text = (user_text or "").strip()
    if not text:
        return plan

    # Honor the SAME selection as the WI-2 catalog (single source of truth):
    # None = all, empty = none, {ids} = only those. Resolved before the search so
    # an empty selection short-circuits and excluded skills never consume a slot.
    allowed = _allowed_ids(settings)
    if allowed is not None and not allowed:
        return plan

    # The injected skill block is model-facing → follow the language picker.
    language = str(_runtime("language", settings, "en") or "en").strip().lower()
    if language not in ("en", "tr"):
        language = "en"

    max_n = max(1, int(_runtime("skill_inject_max", settings, _DEFAULT_MAX)))
    threshold = float(_runtime("skill_inject_threshold", settings, _DEFAULT_THRESHOLD))
    # the timeout_s parameter (chat fast-path 0.5s) overrides the runtime setting per call.
    timeout = (
        timeout_s
        if timeout_s is not None
        else float(_runtime("skill_suggest_timeout_s", settings, _DEFAULT_TIMEOUT_S))
    )

    try:
        reg = registry or get_registry(Path(settings.data_dir))
        # Pass the catalog selection INTO the search so excluded skills are dropped
        # before the top-k cap — otherwise excluded skills with longer triggers can
        # fill every suggestion slot and the selected skill is never suggested. The
        # post-filter below (line ~215) stays as a belt-and-suspenders guard.
        suggestions = await asyncio.wait_for(
            asyncio.to_thread(reg.suggest_for_text, text, max(3, max_n), allowed=allowed),
            timeout=timeout,
        )
    except Exception as e:  # the suggestion search NEVER breaks the turn
        log.warning("skill suggestion failed, turn proceeds without injection: %s", e)
        return plan

    strong: list[dict[str, Any]] = []
    for s in suggestions or []:
        if not isinstance(s, dict) or not s.get("id"):
            continue
        if allowed is not None and str(s.get("id")) not in allowed:
            continue  # removed from the catalog selection → never injected
        reason = str(s.get("match_reason") or "")
        try:
            score = float(s.get("score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        if reason == "trigger_exact" or score >= threshold:
            strong.append(s)

    for suggestion in strong[:max_n]:
        entry = _entry_payload(suggestion)
        try:
            # SKILL.md body disk read — off the loop.
            body = await asyncio.to_thread(
                (registry or get_registry(Path(settings.data_dir))).load_body,
                entry["id"],
            )
        except Exception as e:  # if the body can't be read, that skill is skipped
            log.warning("could not load skill body (%s): %s", entry["id"], e)
            continue
        try:
            _, missing = resolve_skill_servers(
                settings, suggestion.get("tools_allowed") or ()
            )
        except Exception:  # the missing-tool signal is an enhancement, doesn't break
            missing = []
        entry["status"] = "injected"
        if missing:
            entry["missing_tools"] = missing
        plan.injected.append(entry)
        block = _format_block(entry, body, missing, language)
        plan.prompt_block = f"{plan.prompt_block}\n\n{block}".strip()
    return plan
