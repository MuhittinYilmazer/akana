"""PersonaEngine F0 — registration, persistence, and context resolution.

Module map:

* ``models.py``   — :class:`Persona` + boundary validation
* ``builtin.py``  — ``CHAT_SYSTEM_PREFIX`` (single source; ``orchestrator/chat_persona``
  now re-exports from here) + builtin ``akana`` persona
* ``store.py``    — ``<data_dir>/db/persona.db`` (user personas + bindings,
  append-only event log)
* ``registry.py`` — :class:`PersonaRegistry` (merged view of builtin + pack + user,
  ``resolve()`` priority chain)
* REST surface    — ``api/routes/personas.py`` (bearer-protected)

INTEGRATION CONTRACT (F1 — contract only, no code changes in this phase):

1. **Chat turn** (``api/routes/chat.py``): at the start of a turn, instead of reading
   the system prompt directly from ``CHAT_SYSTEM_PREFIX``, make this call::

       reg = get_persona_registry(settings.data_dir)
       persona = reg.resolve(
           channel="web",                      # channel carrying the turn
           conversation_id=conversation_id,    # active conversation (for override)
           skill=active_skill_id,              # if this is a skill_run/work-mode turn
       )
       system_prompt = persona.system_prompt

   Backward compatibility: when there is no binding, ``resolve()`` returns the builtin
   ``akana`` (exactly ``CHAT_SYSTEM_PREFIX`` itself) — behavior does not change.
   Wrappers like ``wrap_chat_user_message`` should use ``persona.system_prompt`` as
   the prefix.

2. **Connectors** (``connectors/router.py`` F2 TODO): while processing a channel
   message, call ``resolve(channel=<connector name: "telegram"...>, conversation_id=...)``;
   the per-channel persona is bound either via REST ``PUT /personas/{id}/bind``
   (persistent, ``db/persona.db``) or env ``AKANA_PERSONA_<CHANNEL>``
   (e.g. ``AKANA_PERSONA_TELEGRAM=official``). The env is only read when there is
   NO store binding for the channel (persistent binding > config).

3. **Pack surface**: when the pack host is set up during app lifecycle,
   ``reg.attach_pack_source(host.personas_adapter)`` is called (the route layer
   does this lazily today); the contract is duck-typed —
   any object that provides ``get_active_personas() -> list[dict]`` is a valid source.

4. **Error contract**: ``resolve()`` never raises and never returns ``None``;
   in the worst case it returns the builtin ``akana``. Persona resolution
   must never break a chat/connector turn under any circumstances.
"""

from __future__ import annotations

from akana_server.persona.builtin import (
    CHAT_SYSTEM_PREFIX,
    DEFAULT_PERSONA_ID,
    builtin_personas,
)
from akana_server.persona.models import Persona, PersonaError
from akana_server.persona.registry import (
    PersonaRegistry,
    get_persona_registry,
    reset_persona_registries,
)
from akana_server.persona.store import PersonaStore

__all__ = [
    "CHAT_SYSTEM_PREFIX",
    "DEFAULT_PERSONA_ID",
    "Persona",
    "PersonaError",
    "PersonaRegistry",
    "PersonaStore",
    "builtin_personas",
    "get_persona_registry",
    "reset_persona_registries",
]
