"""Provider-neutral voice-session helpers: prompt assembly + persona/directive resolution.

These helpers build the system instruction that BOTH realtime bridges
(:mod:`akana_server.voice.gemini_live` / :mod:`akana_server.voice.openai_realtime`)
and the turn-based ``/voice`` endpoint feed to their models. They contain no
provider SDK dependency — only Akana's persona registry, memory core and the
runtime ``language`` setting — so they live in a neutral module rather than inside
the Gemini-specific bridge.

Language: every label and the voice-mode hint follow the active ``language``
runtime setting (``en`` | ``tr``); the default is English (project mandate).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from akana_server.orchestrator.chat_persona import CHAT_SYSTEM_PREFIX
from akana_server.persona.builtin import builtin_personas

if TYPE_CHECKING:
    from akana_server.config import Settings

log = logging.getLogger(__name__)

#: Voice-mode directive — embedded into the realtime ``system_instruction`` (the
#: audio counterpart of the ``[mode: voice]`` hint appended to the text chat's user
#: message). Bilingual: chosen by the active ``language`` (en|tr) like the persona.
_VOICE_MODE_HINT_EN = (
    "[mode: voice_live] You are in a real-time VOICE conversation. Reply briefly, "
    "naturally and in a conversational tone; do NOT use bullet points, markdown, "
    "headings or emoji. Be warm and fluent; wrap it up in one or two sentences "
    "when you can."
)
_VOICE_MODE_HINT_TR = (
    "[mode: voice_live] Gerçek-zamanlı SESLİ konuşmadasın. Kısa, doğal ve "
    "konuşma diliyle yanıt ver; madde işareti, markdown, başlık ya da emoji "
    "KULLANMA. Sıcak ve akıcı ol; mümkünse tek-iki cümlede topla."
)


def _voice_language(settings: Settings) -> str:
    """Active prompt language (``en`` | ``tr``) from the runtime ``language``
    setting; any failure → ``"en"`` (English-first default).

    Thin alias over the canonical :func:`resolve_language` (same runtime >
    env > default chain every other language reader uses).
    """
    from akana_server.runtime_settings import resolve_language

    return resolve_language(settings)


def _voice_mode_hint(language: str) -> str:
    return _VOICE_MODE_HINT_TR if language == "tr" else _VOICE_MODE_HINT_EN


def _language_default_prefix(settings: Settings) -> str:
    """builtin akana prompt for the active language (registry-failure fallback)."""
    try:
        return builtin_personas(_voice_language(settings))[0].system_prompt
    except Exception:  # pragma: no cover - builtin always resolves
        return CHAT_SYSTEM_PREFIX


def resolve_voice_persona_prefix(
    settings: Settings,
    *,
    app: Any = None,
    conv_id: str | None = None,
    channel: str | None = None,
) -> str:
    """Configured persona system prompt for voice (the SAME source text chat uses).

    Mirrors the text path (``ContextAssembler._resolve_persona``): resolves the
    registry persona — which already folds in the user's ``base_prompt`` core
    override and any conversation/channel binding — and returns its
    ``system_prompt``. Pack personas are discovered duck-typed from
    ``app.state.pack_host`` (same seam as the text path). ANY failure → the
    language-default builtin prefix (voice must never break on a persona/DB error).
    """
    try:
        from akana_server.persona.registry import get_persona_registry

        reg = get_persona_registry(settings.data_dir)
        host = getattr(getattr(app, "state", None), "pack_host", None)
        adapter = getattr(host, "personas_adapter", None)
        if adapter is not None:
            reg.attach_pack_source(adapter)
        persona = reg.resolve(
            channel=(channel or "").strip() or None,
            conversation_id=(conv_id or "").strip() or None,
        )
        prompt = (getattr(persona, "system_prompt", "") or "").strip()
        if prompt:
            return prompt
    except Exception:  # persona is an enhancement — never break the voice session
        log.warning("voice persona resolution failed; using builtin", exc_info=True)
    return _language_default_prefix(settings)


def resolve_voice_directive(settings: Settings) -> str:
    """Effective voice-mode directive (user override or language default).

    Appended to the persona for voice turns so spoken replies stay short and
    markdown-free. ANY failure → "" (the persona's own [mode: voice] hint still
    applies, so voice never breaks on a persona/DB error)."""
    try:
        from akana_server.persona.registry import get_persona_registry

        return (get_persona_registry(settings.data_dir).get_voice_directive() or "").strip()
    except Exception:
        log.warning("voice directive resolution failed; skipping", exc_info=True)
        return ""


def build_system_instruction(
    settings: Settings,
    *,
    persona_prefix: str | None = None,
    memory_snapshot: str = "",
    conv_id: str | None = None,
    app: Any = None,
) -> str:
    """Build the realtime ``system_instruction`` text.

    Order: ``[CURRENT TIME]`` + persona + ``[mode: voice_live]`` hint + (if any)
    memory snapshot. Labels and the voice hint follow the active ``language``
    (en|tr). ``persona_prefix`` defaults to the CONFIGURED persona — the user's
    ``base_prompt`` core override plus conversation/channel bindings, resolved
    from the registry (the same prompt the text chat path sends); pass ``conv_id``
    / ``app`` so per-conversation + pack bindings resolve. Tests may pass an
    explicit ``persona_prefix`` string to bypass resolution. The ``memory_snapshot``
    seam holds the session-start top-K recall (empty = no block).
    """
    language = _voice_language(settings)
    now = datetime.now().astimezone()
    time_label = "[CURRENT TIME]" if language == "en" else "[ŞU ANKİ ZAMAN]"
    time_line = f"{time_label} {now:%Y-%m-%d %H:%M %Z}".strip()
    prefix = (
        persona_prefix
        if persona_prefix is not None
        else resolve_voice_persona_prefix(settings, app=app, conv_id=conv_id)
    )
    parts = [time_line, prefix.strip(), _voice_mode_hint(language)]
    snap = (memory_snapshot or "").strip()
    if snap:
        snap_label = "[memory summary]" if language == "en" else "[hafıza özeti]"
        parts.append(f"{snap_label}\n{snap}")
    return "\n\n".join(p for p in parts if p)


def build_memory_snapshot(
    settings: Settings, conv_id: str | None = None, *, limit: int = 8, max_chars: int = 600
) -> str:
    """Session-start memory summary (Phase 3, §3.4 layer 1) — query-independent top-K facts.

    Reduces the user's most important persistent facts (``Memory.list_facts`` →
    ordered by importance) to compact ``- key: value`` lines; embedded into
    ``system_instruction`` (see :func:`build_system_instruction`). DUAL limit for
    token budget + privacy: ``limit`` (lines) + ``max_chars`` (total). DEFENSIVE:
    a memory/DB failure NEVER breaks the session → every error returns ``""``
    (no summary; conversation continues).

    Contains I/O (sqlite) → callers must wrap with ``_off_loop`` (do not block the loop)."""
    try:
        from akana_server.memory_core import get_memory_core

        mem = get_memory_core(settings.data_dir)
        facts = mem.list_facts(min_trust="inferred", limit=limit)
    except Exception:  # pragma: no cover - memory failure must leave summary empty
        return ""
    lines: list[str] = []
    for f in facts:
        value = (getattr(f, "value", "") or "").strip()
        if not value:
            continue
        key = (getattr(f, "key", "") or "").strip()
        lines.append(f"- {key}: {value}" if key else f"- {value}")
    text = "\n".join(lines).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


__all__ = [
    "build_memory_snapshot",
    "build_system_instruction",
    "resolve_voice_directive",
    "resolve_voice_persona_prefix",
]
