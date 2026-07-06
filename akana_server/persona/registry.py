"""PersonaEngine F0 — source merging + context resolution.

Three sources are merged into a single view (priority order on id conflict):

1. **builtin** — ``persona/builtin.py`` (``akana``; CHAT_SYSTEM_PREFIX is the single source)
2. **pack**    — duck-typed discovery: output of ``get_active_personas() -> list[dict]``
   from each object attached via :meth:`PersonaRegistry.attach_pack_source`
   (``PersonasAdapter`` already satisfies this contract; the ``_pack_id`` field
   maps to ``source="pack:<id>"``). A source error cannot break the persona surface.
3. **user**    — ``db/persona.db`` (append-only, :class:`PersonaStore`)

Context resolution (:meth:`PersonaRegistry.resolve`) priority matrix::

    skill persona  >  conversation override  >  channel binding  >  default akana

* skill: ``skills.skill_resolve.find_pack_persona`` (injectable) — the persona prompt
  of the pack containing the skill; if absent or an error occurs, the chain falls
  through (the persona is an enhancement and must never break the turn).
* conversation: ``conversation`` binding in the store (PUT /personas/{id}/bind).
* channel: ``channel`` binding in the store; if absent, env config
  ``AKANA_PERSONA_<CHANNEL>`` (e.g. ``AKANA_PERSONA_TELEGRAM=official_akana``).
* default: builtin ``akana``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from akana_server.persona.builtin import (
    DEFAULT_PERSONA_ID,
    _BY_LANGUAGE,
    _VOICE_DIRECTIVE_BY_LANGUAGE,
    builtin_personas,
    default_voice_directive,
)
from akana_server.persona.models import (
    MAX_PROMPT,
    Persona,
    PersonaError,
    validate_persona_fields,
)
from akana_server.persona.store import PersonaStore

#: Singleton override keys (persona store ``overrides`` table).
_BASE_PROMPT_KEY = "base_prompt"
_CATALOG_SELECTION_KEY = "catalog_selection"
_VOICE_DIRECTIVE_KEY = "voice_directive"


log = logging.getLogger(__name__)

#: Skill → pack persona prompt resolver signature (None = no persona).
SkillPersonaResolver = Callable[[str], "str | None"]


def _default_skill_resolver(skill_id: str) -> str | None:
    """Pack persona discovery — lazy import, error returns None."""
    from akana_server.skills.skill_resolve import find_pack_persona

    return find_pack_persona(skill_id)


def channel_env_var(channel: str) -> str:
    """Channel name → env variable (``telegram`` → ``AKANA_PERSONA_TELEGRAM``)."""
    return "AKANA_PERSONA_" + "".join(
        c if c.isalnum() else "_" for c in (channel or "").strip().upper()
    )


class PersonaRegistry:
    """Merged view of builtin + pack + user personas and context resolution."""

    def __init__(
        self,
        data_dir: Path,
        *,
        store: PersonaStore | None = None,
        skill_persona_resolver: SkillPersonaResolver | None = None,
    ) -> None:
        self._data_dir = Path(data_dir)
        self._store = store or PersonaStore(Path(data_dir) / "db" / "persona.db")
        self._skill_resolver = skill_persona_resolver or _default_skill_resolver
        self._pack_sources: list[Any] = []
        self._lock = threading.Lock()

    @property
    def store(self) -> PersonaStore:
        return self._store

    # -- pack source discovery (duck-typed) ---------------------------------- #

    def attach_pack_source(self, source: Any) -> None:
        """Attach any object that provides ``get_active_personas()``.

        Idempotent (the same object is not added twice); PersonasAdapter is the
        natural candidate, but the contract is duck-typed — tests may pass a plain stub.
        """
        if source is None or not callable(getattr(source, "get_active_personas", None)):
            return
        with self._lock:
            if not any(s is source for s in self._pack_sources):
                self._pack_sources.append(source)

    def _pack_personas(self) -> list[Persona]:
        """Personas from attached sources — each error is logged in isolation and skipped."""
        out: list[Persona] = []
        with self._lock:
            sources = list(self._pack_sources)
        for source in sources:
            try:
                items = source.get_active_personas() or []
            except Exception:  # defensive — a broken source cannot break the surface
                log.warning("pack persona source could not be read: %r", source, exc_info=True)
                continue
            for raw in items:
                persona = self._coerce_pack_persona(raw)
                if persona is not None:
                    out.append(persona)
        return out

    @staticmethod
    def _coerce_pack_persona(raw: Any) -> Persona | None:
        if not isinstance(raw, dict):
            return None
        pid = str(raw.get("id") or "").strip()
        prompt = raw.get("system_prompt")
        if not pid or not isinstance(prompt, str) or not prompt.strip():
            return None
        pack_id = str(raw.get("_pack_id") or "").strip()
        return Persona(
            id=pid,
            name=str(raw.get("name") or pid),
            system_prompt=prompt.strip(),
            tone=str(raw.get("tone") or ""),
            source=f"pack:{pack_id}" if pack_id else "pack:?",
        )

    def _language(self) -> str:
        """Active persona/voice language (en|tr) from the runtime setting (English default)."""
        from types import SimpleNamespace

        from akana_server.runtime_settings import resolve_language

        return resolve_language(SimpleNamespace(data_dir=self._data_dir))

    # -- merged view --------------------------------------------------------- #

    def _effective_builtins(self) -> list[Persona]:
        """Builtin personas, with akana's system_prompt replaced by the base override if set.

        The core prompt (CHAT_SYSTEM_PREFIX) lives in code; if the user has edited it,
        the ``base_prompt`` override in the store replaces akana's prompt (including the
        identity/language-lock section). Without an override the code default is returned
        as-is.
        """
        base = builtin_personas(self._language())
        override = self._store.get_override(_BASE_PROMPT_KEY)
        if not override:
            return base
        return [
            replace(p, system_prompt=override) if p.id == DEFAULT_PERSONA_ID else p
            for p in base
        ]

    def list(self) -> list[Persona]:
        """builtin + pack + user (first wins on id conflict — builtin > pack > user)."""
        merged: dict[str, Persona] = {}
        for persona in (*self._effective_builtins(), *self._pack_personas(), *self._store.list()):
            merged.setdefault(persona.id, persona)
        return list(merged.values())

    def get(self, persona_id: str) -> Persona | None:
        for persona in self.list():
            if persona.id == persona_id:
                return persona
        return None

    # -- user persona CRUD (F0: create; no delete — append-only) ------------- #

    def create_user_persona(
        self, *, persona_id: str, name: str, system_prompt: str, tone: str = ""
    ) -> Persona:
        validate_persona_fields(persona_id, name, system_prompt, tone)
        if self.get(persona_id) is not None:
            raise PersonaError(f"persona id already in use: {persona_id}")
        return self._store.create(
            Persona(
                id=persona_id,
                name=name.strip(),
                system_prompt=system_prompt,
                tone=tone,
                source="user",
            )
        )

    def update_user_persona(
        self, *, persona_id: str, name: str, system_prompt: str, tone: str = ""
    ) -> Persona:
        """Edit an existing USER persona.

        builtin/pack are read-only → :class:`PersonaError`; if not found at all → ``KeyError``.
        Only records with ``source="user"`` in the store may be edited.
        """
        validate_persona_fields(persona_id, name, system_prompt, tone)
        if self._store.get(persona_id) is None:
            if self.get(persona_id) is not None:
                raise PersonaError(f"persona is read-only (user only): {persona_id}")
            raise KeyError(persona_id)
        return self._store.update(
            Persona(
                id=persona_id,
                name=name.strip(),
                system_prompt=system_prompt,
                tone=tone,
                source="user",
            )
        )

    def delete_user_persona(self, persona_id: str) -> None:
        """Delete a USER persona.

        builtin/pack are read-only → :class:`PersonaError`; if not found at all → ``KeyError``.
        Also removes bindings (store.delete); resolve falls back to the builtin akana.
        """
        if self._store.get(persona_id) is None:
            if self.get(persona_id) is not None:
                raise PersonaError(f"persona cannot be deleted (user only): {persona_id}")
            raise KeyError(persona_id)
        self._store.delete(persona_id)

    # -- bindings ------------------------------------------------------------- #

    def bind(
        self,
        persona_id: str,
        *,
        channel: str | None = None,
        conversation_id: str | None = None,
    ) -> dict[str, str]:
        """Bind a persona to a channel and/or conversation; unknown persona → error."""
        if self.get(persona_id) is None:
            raise KeyError(f"unknown persona: {persona_id}")
        if not channel and not conversation_id:
            raise PersonaError("channel or conversation_id is required")
        bound: dict[str, str] = {}
        if channel:
            self._store.set_binding("channel", channel.strip().lower(), persona_id)
            bound["channel"] = channel.strip().lower()
        if conversation_id:
            self._store.set_binding("conversation", conversation_id, persona_id)
            bound["conversation_id"] = conversation_id
        return bound

    def list_bindings(self) -> list[dict[str, str]]:
        return self._store.list_bindings()

    # -- context resolution --------------------------------------------------- #

    def resolve(
        self,
        channel: str | None = None,
        conversation_id: str | None = None,
        skill: str | None = None,
    ) -> Persona:
        """Priority: skill persona > conversation override > channel binding > akana.

        Every step is defensive: a missing/broken candidate falls through to the
        next; the return value is never ``None`` (the builtin ``akana`` always exists).
        """
        # 1. Skill persona — the persona prompt of the pack containing the skill.
        if skill:
            try:
                prompt = self._skill_resolver(skill)
            except Exception:
                log.debug("skill persona resolution failed (%s)", skill, exc_info=True)
                prompt = None
            if isinstance(prompt, str) and prompt.strip():
                return Persona(
                    id=f"skill:{skill}",
                    name=f"{skill} pack persona",
                    system_prompt=prompt.strip(),
                    source="pack:skill",
                )

        # 2. Conversation override.
        if conversation_id:
            persona = self._binding_persona("conversation", conversation_id)
            if persona is not None:
                return persona

        # 3. Channel binding — store first, then env config.
        if channel:
            key = channel.strip().lower()
            persona = self._binding_persona("channel", key)
            if persona is None:
                env_id = (os.environ.get(channel_env_var(channel)) or "").strip()
                if env_id:
                    persona = self.get(env_id)
            if persona is not None:
                return persona

        # 4. Default akana.
        default = self.get(DEFAULT_PERSONA_ID)
        assert default is not None  # builtin is always registered
        return default

    def _binding_persona(self, scope: str, key: str) -> Persona | None:
        pid = self._store.get_binding(scope, key)
        return self.get(pid) if pid else None

    # -- core prompt (base) + catalog text overrides ------------------------- #

    def get_base_prompt(self) -> str:
        """Effective core prompt: the override if set, else the language default."""
        return self._store.get_override(_BASE_PROMPT_KEY) or self.base_prompt_default()

    def base_prompt_default(self) -> str:
        """Code default for the active language (reset target / comparison)."""
        return builtin_personas(self._language())[0].system_prompt

    def base_prompt_is_override(self) -> bool:
        return self._store.get_override(_BASE_PROMPT_KEY) is not None

    def set_base_prompt(self, text: str) -> None:
        """Override the core prompt (entire base including identity/language-lock).

        U5: saving text that equals ANY language's builtin default CLEARS the override
        instead of freezing it. The persona pane prefills the textarea with the current
        language-resolved default, so pressing Save without editing would otherwise store
        that default verbatim — and the override then wins over the language-resolved
        builtin forever, so switching the UI language no longer changes the core prompt
        ('UI dili değişince çekirdek prompt dili değişmiyor'). A genuine user edit is not a
        default and stays frozen (intended user content — we never auto-translate it).
        """
        t = (text or "").strip()
        if not t:
            raise PersonaError("core prompt cannot be empty")
        if len(t) > MAX_PROMPT:
            raise PersonaError(f"core prompt must be at most {MAX_PROMPT} characters")
        builtin_defaults = {prompt.strip() for prompt, _tone in _BY_LANGUAGE.values()}
        if t in builtin_defaults:
            self._store.clear_override(_BASE_PROMPT_KEY)
            return
        self._store.set_override(_BASE_PROMPT_KEY, t)

    def reset_base_prompt(self) -> None:
        """Remove the override → fall back to the code ``CHAT_SYSTEM_PREFIX``."""
        self._store.clear_override(_BASE_PROMPT_KEY)

    # -- voice-mode directive (override + language default) ------------------- #

    def get_voice_directive(self) -> str:
        """Effective voice directive: the override if set, else the language default."""
        return self._store.get_override(_VOICE_DIRECTIVE_KEY) or self.voice_directive_default()

    def voice_directive_default(self) -> str:
        """Code default voice directive for the active language (reset target)."""
        return default_voice_directive(self._language())

    def voice_directive_is_override(self) -> bool:
        return self._store.get_override(_VOICE_DIRECTIVE_KEY) is not None

    def set_voice_directive(self, text: str) -> None:
        """Override the voice-mode directive (injected on top of the persona).

        U5: like set_base_prompt, saving text equal to ANY language's builtin voice
        directive CLEARS the override so the directive keeps following the language picker
        (saving the unchanged prefilled default no longer freezes its language).
        """
        t = (text or "").strip()
        if not t:
            raise PersonaError("voice directive cannot be empty")
        if len(t) > MAX_PROMPT:
            raise PersonaError(f"voice directive must be at most {MAX_PROMPT} characters")
        builtin_defaults = {d.strip() for d in _VOICE_DIRECTIVE_BY_LANGUAGE.values()}
        if t in builtin_defaults:
            self._store.clear_override(_VOICE_DIRECTIVE_KEY)
            return
        self._store.set_override(_VOICE_DIRECTIVE_KEY, t)

    def reset_voice_directive(self) -> None:
        """Remove the override → fall back to the code default for the language."""
        self._store.clear_override(_VOICE_DIRECTIVE_KEY)

    def get_catalog_selection(self) -> list[str] | None:
        """Skill ids included in the catalog (None = all/auto). Returns None on error."""
        raw = self._store.get_override(_CATALOG_SELECTION_KEY)
        if raw is None:
            return None
        try:
            val = json.loads(raw)
        except Exception:
            return None
        return [str(x) for x in val] if isinstance(val, list) else None

    def set_catalog_selection(self, ids: list[str]) -> None:
        """Set the skill ids included in the catalog (empty list = none included)."""
        clean = [str(x).strip() for x in (ids or []) if str(x).strip()]
        self._store.set_override(_CATALOG_SELECTION_KEY, json.dumps(clean, ensure_ascii=False))

    def reset_catalog_selection(self) -> None:
        """Remove the selection → fall back to all/auto-generation."""
        self._store.clear_override(_CATALOG_SELECTION_KEY)


# -- module-level cache (same pattern as skills.registry.get_registry) --------- #

_REGISTRIES: dict[Path, PersonaRegistry] = {}
_REG_LOCK = threading.Lock()


def get_persona_registry(data_dir: Path) -> PersonaRegistry:
    """Singleton registry per data_dir (tests reset it via ``reset_persona_registries``)."""
    key = Path(data_dir).resolve()
    with _REG_LOCK:
        reg = _REGISTRIES.get(key)
        if reg is None:
            reg = PersonaRegistry(key)
            _REGISTRIES[key] = reg
        return reg


def reset_persona_registries() -> None:
    with _REG_LOCK:
        _REGISTRIES.clear()


__all__ = [
    "PersonaRegistry",
    "SkillPersonaResolver",
    "channel_env_var",
    "get_persona_registry",
    "reset_persona_registries",
]
