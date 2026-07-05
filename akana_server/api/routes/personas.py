"""Persona API (PersonaEngine F0) — bearer-protected.

- ``GET  /personas``            — builtin + pack + user unified list + bindings
- ``POST /personas``            — create a user persona (append-only; no DELETE)
- ``PUT  /personas/{id}/bind``  — channel and/or conversation binding

Pack personas are discovered duck-typed: if ``app.state.pack_host`` is set, the
``personas_adapter`` is attached to the registry as a source (idempotent) — if the
pack host was never set up, the list keeps working with builtin + user.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from akana_server.api.deps import require_akana_bearer
from akana_server.api.errors import http_error
from akana_server.api.services import AppServices, get_services
from akana_server.persona.models import MAX_NAME, MAX_PROMPT, MAX_TONE, PersonaError
from akana_server.persona.registry import PersonaRegistry, get_persona_registry

router = APIRouter(tags=["personas"])

_SLUG_RE = re.compile(r"[^a-z0-9]+")

#: Ids shadowed by literal routes below (registered before ``/personas/{persona_id}``
#: so a persona with one of these ids can never be reached through PUT/DELETE
#: ``/personas/{id}``) — reserved so ``create_persona`` never allows one to be minted.
_RESERVED_PERSONA_IDS = frozenset({"base", "voice-directive", "catalog"})


def _registry(services: AppServices) -> PersonaRegistry:
    reg = get_persona_registry(services.settings.data_dir)
    adapter = getattr(services.pack_host, "personas_adapter", None)
    if adapter is not None:
        reg.attach_pack_source(adapter)  # duck-typed, idempotent
    return reg


class PersonaCreateRequest(BaseModel):
    id: str | None = Field(default=None, max_length=64)
    name: str = Field(..., min_length=1, max_length=MAX_NAME)
    system_prompt: str = Field(..., min_length=1, max_length=MAX_PROMPT)
    tone: str = Field(default="", max_length=MAX_TONE)


class PersonaUpdateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=MAX_NAME)
    system_prompt: str = Field(..., min_length=1, max_length=MAX_PROMPT)
    tone: str = Field(default="", max_length=MAX_TONE)


class PersonaBindRequest(BaseModel):
    channel: str | None = Field(default=None, max_length=64)
    conversation_id: str | None = Field(default=None, max_length=128)


class BasePromptRequest(BaseModel):
    system_prompt: str = Field(..., min_length=1, max_length=MAX_PROMPT)


class VoiceDirectiveRequest(BaseModel):
    voice_directive: str = Field(..., min_length=1, max_length=MAX_PROMPT)


class CatalogSelectionRequest(BaseModel):
    selection: list[str] = Field(default_factory=list)


def _catalog_state(services: AppServices, reg: PersonaRegistry) -> dict[str, Any]:
    """Skill catalog state: on/off (runtime setting) + override + auto-preview."""
    enabled = True
    try:
        from akana_server.runtime_settings import get_runtime

        enabled = bool(get_runtime("skill_catalog_enabled", services.settings))
    except Exception:
        pass
    skills: list[dict[str, str]] = []
    try:
        from akana_server.skills.catalog import list_catalog_skills
        from akana_server.skills.registry import get_registry as _skill_reg

        skills = list_catalog_skills(_skill_reg(services.settings.data_dir))
    except Exception:
        pass
    selection = reg.get_catalog_selection()
    sel = set(selection) if selection is not None else None
    return {
        "enabled": enabled,
        "selection": selection,  # list[str] | None (None = all/auto)
        "skills": [
            {"id": s["id"], "label": s["label"], "included": sel is None or s["id"] in sel}
            for s in skills
        ],
    }


def _slug(name: str) -> str:
    return _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")[:64]


@router.get("/personas", dependencies=[Depends(require_akana_bearer)])
async def list_personas(
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    reg = _registry(services)
    items = [p.to_dict() for p in reg.list()]
    return {
        "count": len(items),
        "personas": items,
        "bindings": reg.list_bindings(),
        "base": {
            "is_override": reg.base_prompt_is_override(),
            "default": reg.base_prompt_default(),
        },
        "voice_directive": {
            "value": reg.get_voice_directive(),
            "is_override": reg.voice_directive_is_override(),
            "default": reg.voice_directive_default(),
        },
        "catalog": _catalog_state(services, reg),
    }


@router.post(
    "/personas", status_code=201, dependencies=[Depends(require_akana_bearer)]
)
async def create_persona(
    body: PersonaCreateRequest, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    reg = _registry(services)
    persona_id = (body.id or "").strip() or _slug(body.name)
    if persona_id in _RESERVED_PERSONA_IDS:
        # These ids are shadowed by the literal /personas/base|voice-directive|catalog
        # routes below — a persona minted with one of them could never be updated or
        # deleted through the API, and its DELETE/PUT would silently hit the wrong
        # (unrelated) override endpoint instead.
        raise http_error(
            409, "PERSONA_ID_RESERVED", f"persona id {persona_id!r} is reserved"
        )
    try:
        persona = reg.create_user_persona(
            persona_id=persona_id,
            name=body.name,
            system_prompt=body.system_prompt,
            tone=body.tone,
        )
    except PersonaError as e:
        msg = str(e)
        if "in use" in msg or "already exists" in msg:
            raise http_error(409, "PERSONA_EXISTS", msg) from None
        raise http_error(400, "PERSONA_INVALID", msg) from None
    return {"persona": persona.to_dict()}


# -- core prompt (base) + catalog overrides ----------------------------------- #
# CAUTION: the literal paths must be defined BEFORE the ``/{persona_id}`` routes;
# otherwise the ``/personas/base`` PUT falls to update_persona (persona_id="base")
# → 404.


@router.put("/personas/base", dependencies=[Depends(require_akana_bearer)])
async def set_base_prompt_route(
    body: BasePromptRequest, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    reg = _registry(services)
    try:
        reg.set_base_prompt(body.system_prompt)
    except PersonaError as e:
        raise http_error(400, "PERSONA_INVALID", str(e)) from None
    return {"base_prompt": reg.get_base_prompt(), "is_override": reg.base_prompt_is_override()}


@router.delete("/personas/base", dependencies=[Depends(require_akana_bearer)])
async def reset_base_prompt_route(
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    reg = _registry(services)
    reg.reset_base_prompt()
    return {"base_prompt": reg.get_base_prompt(), "is_override": reg.base_prompt_is_override()}


def _voice_directive_payload(reg: PersonaRegistry) -> dict[str, Any]:
    return {
        "value": reg.get_voice_directive(),
        "is_override": reg.voice_directive_is_override(),
        "default": reg.voice_directive_default(),
    }


@router.put("/personas/voice-directive", dependencies=[Depends(require_akana_bearer)])
async def set_voice_directive_route(
    body: VoiceDirectiveRequest, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    reg = _registry(services)
    try:
        reg.set_voice_directive(body.voice_directive)
    except PersonaError as e:
        raise http_error(400, "PERSONA_INVALID", str(e)) from None
    return _voice_directive_payload(reg)


@router.delete("/personas/voice-directive", dependencies=[Depends(require_akana_bearer)])
async def reset_voice_directive_route(
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    reg = _registry(services)
    reg.reset_voice_directive()
    return _voice_directive_payload(reg)


@router.put("/personas/catalog", dependencies=[Depends(require_akana_bearer)])
async def set_catalog_selection_route(
    body: CatalogSelectionRequest, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    reg = _registry(services)
    reg.set_catalog_selection(body.selection)
    return {"selection": reg.get_catalog_selection()}


@router.delete("/personas/catalog", dependencies=[Depends(require_akana_bearer)])
async def reset_catalog_selection_route(
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    reg = _registry(services)
    reg.reset_catalog_selection()
    return {"selection": reg.get_catalog_selection()}


@router.put("/personas/{persona_id}", dependencies=[Depends(require_akana_bearer)])
async def update_persona(
    persona_id: str,
    body: PersonaUpdateRequest,
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    reg = _registry(services)
    try:
        persona = reg.update_user_persona(
            persona_id=persona_id,
            name=body.name,
            system_prompt=body.system_prompt,
            tone=body.tone,
        )
    except KeyError:
        raise http_error(
            404, "PERSONA_NOT_FOUND", f"persona not found: {persona_id}"
        ) from None
    except PersonaError as e:
        msg = str(e)
        # builtin/pack personas are read-only → 403; validation failures → 400.
        # Match the registry's "read-only" marker (English, stable post-i18n).
        if "read-only" in msg:
            raise http_error(403, "PERSONA_READONLY", msg) from None
        raise http_error(400, "PERSONA_INVALID", msg) from None
    return {"persona": persona.to_dict()}


@router.delete("/personas/{persona_id}", dependencies=[Depends(require_akana_bearer)])
async def delete_persona(
    persona_id: str, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    reg = _registry(services)
    try:
        reg.delete_user_persona(persona_id)
    except KeyError:
        raise http_error(
            404, "PERSONA_NOT_FOUND", f"persona not found: {persona_id}"
        ) from None
    except PersonaError as e:
        raise http_error(403, "PERSONA_READONLY", str(e)) from None
    return {"deleted": persona_id}


@router.put(
    "/personas/{persona_id}/bind", dependencies=[Depends(require_akana_bearer)]
)
async def bind_persona(
    persona_id: str, body: PersonaBindRequest, services: AppServices = Depends(get_services)
) -> dict[str, Any]:
    reg = _registry(services)
    try:
        bound = reg.bind(
            persona_id, channel=body.channel, conversation_id=body.conversation_id
        )
    except KeyError:
        raise http_error(
            404, "PERSONA_NOT_FOUND", f"persona not found: {persona_id}"
        ) from None
    except PersonaError as e:
        raise http_error(400, "PERSONA_BIND_INVALID", str(e)) from None
    return {"persona_id": persona_id, "bound": bound, "bindings": reg.list_bindings()}
