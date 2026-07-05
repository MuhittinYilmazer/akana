"""PersonaEngine F0 — data model.

Single record type: :class:`Persona`. Source (``source``) is one of three values:

* ``"builtin"``     — code-defined (e.g. ``akana``, ``persona/builtin.py``)
* ``"pack:<id>"``   — a persona from an active pack (PersonasAdapter discovery)
* ``"user"``        — user-defined, persisted in ``db/persona.db``
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

#: Persona id format — narrow charset for safe use in routes, paths and env values.
PERSONA_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_\-]{0,63}$")

#: Limits (input validation — validate at system boundaries).
MAX_NAME = 100
MAX_PROMPT = 20_000
MAX_TONE = 2_000


class PersonaError(ValueError):
    """Invalid persona input / conflict (mapped to 4xx in routes)."""


@dataclass(frozen=True, slots=True)
class Persona:
    """A persona record — system prompt + tone notes + source."""

    id: str
    name: str
    system_prompt: str
    tone: str = ""  # tone notes (language, address style, register) — free text
    source: str = "user"  # "builtin" | "pack:<id>" | "user"
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "system_prompt": self.system_prompt,
            "tone": self.tone,
            "source": self.source,
        }


def validate_persona_fields(persona_id: str, name: str, system_prompt: str, tone: str) -> None:
    """System boundary validation — raises :class:`PersonaError` on invalid input."""
    if not PERSONA_ID_RE.fullmatch(persona_id or ""):
        raise PersonaError(
            "invalid persona id: must start with a lowercase letter/digit, [a-z0-9_-], max 64 characters"
        )
    if not (name or "").strip() or len(name) > MAX_NAME:
        raise PersonaError(f"name is required, max {MAX_NAME} characters")
    if not (system_prompt or "").strip() or len(system_prompt) > MAX_PROMPT:
        raise PersonaError(f"system_prompt is required, max {MAX_PROMPT} characters")
    if len(tone or "") > MAX_TONE:
        raise PersonaError(f"tone: max {MAX_TONE} characters")


__all__ = [
    "MAX_NAME",
    "MAX_PROMPT",
    "MAX_TONE",
    "PERSONA_ID_RE",
    "Persona",
    "PersonaError",
    "validate_persona_fields",
]
