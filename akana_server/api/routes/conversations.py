"""REST API for persistent conversation archive."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from akana_server.api.deps import require_akana_bearer
from akana_server.api.routes.chat._base import _off_loop
from akana_server.api.services import AppServices, get_services
from akana_server.chat_context import (
    effective_llm_settings,
    persist_conversation_llm,
    restore_llm_settings,
)
from akana_server.conversation_service import ConversationService
from akana_server.llm_settings import (
    _VALID_PROVIDERS,
    conversation_llm_patch_from_meta,
    public_llm_payload,
    resolve_cursor_model_tag,
)
from akana_server.memory_core import get_memory_core

router = APIRouter(tags=["conversations"])


class ConversationCreate(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class ConversationPatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    pinned: bool | None = None
    archived: bool | None = None


class ConversationLlmPatch(BaseModel):
    provider: str | None = None
    cursor_model: str | None = None
    claude_model: str | None = None
    ollama_model: str | None = None
    gemini_model: str | None = None
    openai_model: str | None = None

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, v: str | None) -> str | None:
        # Reject an out-of-enum provider (422) instead of persisting a bogus
        # override to json_metadata. _merge keeps the base value on a bad value,
        # but a silent override would confuse the "which model does this
        # conversation use?" panel — surface the error at the boundary.
        if v is not None and v.strip() and v.strip().lower() not in _VALID_PROVIDERS:
            raise ValueError(f"must be one of: {', '.join(sorted(_VALID_PROVIDERS))}")
        return v


class ConversationMetaOut(BaseModel):
    id: str
    title: str
    title_source: str
    preview: str | None = None
    pinned: bool = False
    archived_at: str | None = None
    created_at: str
    updated_at: str
    last_message_at: str | None = None
    message_count: int = 0


def _svc(request: Request) -> ConversationService:
    """The single unified conversation service (``ConversationService`` → ``memory.db``)."""
    svc = getattr(request.app.state, "conversation_service", None)
    if not isinstance(svc, ConversationService):
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "code": "CONVERSATIONS_UNAVAILABLE",
                    "message": "The conversation service is not ready yet; please try again shortly.",
                }
            },
        )
    return svc


def _meta_out(m: Any) -> ConversationMetaOut:
    return ConversationMetaOut(
        id=m.id,
        title=m.title,
        title_source=m.title_source,
        preview=m.preview,
        pinned=m.pinned,
        archived_at=m.archived_at,
        created_at=m.created_at,
        updated_at=m.updated_at,
        last_message_at=m.last_message_at,
        message_count=m.message_count,
    )


@router.get("/conversations", dependencies=[Depends(require_akana_bearer)])
async def list_conversations(
    request: Request,
    limit: int = 50,
    archived: bool = False,
    pinned: bool | None = None,
) -> dict[str, Any]:
    svc = _svc(request)
    # A synchronous sqlite read BLOCKS the event loop: until the list query (+ a
    # competing writer's WAL lock) finishes, every SSE/WS/endpoint freezes — the
    # "2-3 sec stall" on list refresh after a new chat grows from here. Move it to
    # a worker thread.
    #
    # ?archived=true is the Archived TAB (an archived-ONLY view), not "active plus
    # archived": route it to archived_only so the SQL filter runs before the store's
    # 200-row ceiling. Otherwise an archived conversation older than the newest 200
    # active ones falls out of the mixed window and — search excludes archived rows —
    # becomes invisible and un-unarchivable from the UI.
    items = await _off_loop(
        svc.list_conversations,
        limit=limit,
        archived_only=bool(archived),
        pinned_only=bool(pinned),
    )
    return {"conversations": [_meta_out(m) for m in items]}


@router.post("/conversations", dependencies=[Depends(require_akana_bearer)])
async def create_conversation(
    request: Request,
    body: ConversationCreate | None = None,
) -> ConversationMetaOut:
    svc = _svc(request)
    title = body.title if body else None
    # create() performs two synchronous sqlite writes (conversations INSERT +
    # ledger). Since this is the new-chat hot path, move it off the loop — so the
    # server doesn't freeze while the POST response waits on the disk/WAL lock.
    meta = await _off_loop(svc.create, title=title)
    return _meta_out(meta)


@router.get("/conversations/search", dependencies=[Depends(require_akana_bearer)])
async def search_conversations(
    request: Request,
    q: str,
    limit: int = 30,
) -> dict[str, Any]:
    svc = _svc(request)
    if not q.strip():
        return {"results": []}
    return {"results": await _off_loop(svc.search, q.strip(), limit=limit)}


@router.get(
    "/conversations/{conversation_id}",
    dependencies=[Depends(require_akana_bearer)],
)
async def get_conversation_meta(
    conversation_id: str,
    request: Request,
) -> ConversationMetaOut:
    svc = _svc(request)
    meta = await _off_loop(svc.get, conversation_id)
    if meta is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Conversation not found."}},
        )
    return _meta_out(meta)


@router.get(
    "/conversations/{conversation_id}/llm-settings",
    dependencies=[Depends(require_akana_bearer)],
)
async def get_conversation_llm_settings(
    conversation_id: str,
    request: Request,
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    svc = _svc(request)
    if await _off_loop(svc.get, conversation_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Conversation not found."}},
        )
    settings = services.settings
    llm = restore_llm_settings(request, conversation_id)
    meta = svc.get_json_metadata(conversation_id)
    return {
        **public_llm_payload(
            llm,
            settings=settings,
            active_tag=resolve_cursor_model_tag(settings, llm),
        ),
        "has_override": bool(conversation_llm_patch_from_meta(meta)),
    }


@router.put(
    "/conversations/{conversation_id}/llm-settings",
    dependencies=[Depends(require_akana_bearer)],
)
async def put_conversation_llm_settings(
    conversation_id: str,
    body: ConversationLlmPatch,
    request: Request,
    services: AppServices = Depends(get_services),
) -> dict[str, Any]:
    svc = _svc(request)
    # Eager-create race / offline: if the row does NOT exist yet, CREATE it
    # instead of returning 404, so a model selected BEFORE sending is persisted AT
    # SELECTION TIME (the root edge of the "different chat = different model isn't
    # remembered" complaint: when a PUT arrived before the conv id existed, the old
    # path returned 404 and dropped the selection). persist → merge_json_metadata
    # → ensure creates the row. BUT do NOT resurrect an intentionally DELETED
    # conversation (tombstone: json_metadata.deleted) — so it doesn't come back as
    # a hidden record in the list (see the ConversationService.soft_delete /
    # _conversation_exists contract).
    meta0 = await _off_loop(svc.get_json_metadata, conversation_id)
    if meta0.get("deleted"):
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Conversation not found."}},
        )
    patch = body.model_dump(exclude_unset=True)
    if patch:
        await _off_loop(persist_conversation_llm, request, conversation_id, patch)
    settings = services.settings
    llm = effective_llm_settings(request, conversation_id)
    meta = await _off_loop(svc.get_json_metadata, conversation_id)
    return {
        **public_llm_payload(
            llm,
            settings=settings,
            active_tag=resolve_cursor_model_tag(settings, llm),
        ),
        "has_override": bool(conversation_llm_patch_from_meta(meta)),
    }


@router.patch(
    "/conversations/{conversation_id}",
    dependencies=[Depends(require_akana_bearer)],
)
async def patch_conversation(
    conversation_id: str,
    body: ConversationPatch,
    request: Request,
) -> ConversationMetaOut:
    svc = _svc(request)
    meta = await _off_loop(
        svc.patch,
        conversation_id,
        title=body.title,
        pinned=body.pinned,
        archived=body.archived,
    )
    if meta is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Conversation not found."}},
        )
    return _meta_out(meta)


@router.delete(
    "/conversations/{conversation_id}",
    dependencies=[Depends(require_akana_bearer)],
    status_code=204,
)
async def delete_conversation(
    conversation_id: str,
    request: Request,
    services: AppServices = Depends(get_services),
) -> Response:
    svc = _svc(request)
    from akana_server.chat_context import clear_agent_id
    from akana_server.orchestrator.bridge_pool import (
        bridge_daemon_enabled,
        get_bridge_pool,
    )

    if await _off_loop(svc.get, conversation_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Conversation not found."}},
        )
    await _off_loop(clear_agent_id, request, conversation_id)
    from akana_server.api.routes.chat import cleanup_conversation_chat_state

    await cleanup_conversation_chat_state(request.app, conversation_id)
    if not await _off_loop(svc.soft_delete, conversation_id):
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Conversation not found."}},
        )
    # DELETE the turns — a single set-based path. There USED to be a double
    # delete: mem.reset_conversation (DELETE FROM turns + per-row FTS trigger)
    # followed by a second pass scanning the SAME turns again — in a long chat the
    # FTS trigger costs ~0.34 ms/row, and doing both + ON the event loop = seconds
    # of freezing. reset_conversation alone is enough (deletes the turns + logs the
    # conversation_reset event); the second is unnecessary. Additionally, set-based
    # FTS deletion (episodic.delete_conversation) is ~20× faster. All in a worker
    # thread → the loop keeps serving SSE/WS.
    await _off_loop(
        get_memory_core(services.settings.data_dir).reset_conversation, conversation_id
    )
    settings = services.settings
    if bridge_daemon_enabled():
        await get_bridge_pool(settings).close_session(conversation_id)
    return Response(status_code=204)


@router.get(
    "/conversations/{conversation_id}/messages",
    dependencies=[Depends(require_akana_bearer)],
)
async def list_messages(
    conversation_id: str,
    request: Request,
    limit: int = 500,
    before: str | None = None,
    before_id: str | None = None,
) -> dict[str, Any]:
    svc = _svc(request)
    if await _off_loop(svc.get, conversation_id) is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "NOT_FOUND", "message": "Conversation not found."}},
        )
    # before + before_id together → keyset cursor (no turn loss at a same-ms
    # boundary, R4-C #2); before alone → backward-compatible ts<? window.
    messages = await _off_loop(
        svc.list_messages,
        conversation_id,
        limit=limit,
        before_ts=before,
        before_id=before_id,
    )
    from akana_server.orchestrator.base import coerce_token_count, coerce_cost_usd

    def _safe_usage(raw: dict | None) -> dict | None:
        """Contract v2 clause 4: safely normalize the usage field.

        usage data read from disk is external JSON; it is hardened via coerce so
        corrupt/old rows don't crash the frontend.
        """
        if not isinstance(raw, dict):
            return None
        prompt = coerce_token_count(raw.get("prompt"))
        completion = coerce_token_count(raw.get("completion"))
        if prompt == 0 and completion == 0:
            return None  # don't show a meaningless row
        out: dict = {"prompt": prompt, "completion": completion}
        cost = coerce_cost_usd(raw.get("cost_usd"))
        if cost > 0:
            out["cost_usd"] = cost
        return out

    return {
        "conversation_id": conversation_id,
        "messages": [
            {
                "id": m.id,
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at,
                "file_ids": m.file_ids,
                "tool_calls": m.tool_calls,
                # Contract v2 clause 4: the usage field on assistant turns
                # (token/cost info after a page refresh). None on user turns or
                # when the info is missing.
                **( {"usage": _safe_usage(m.usage)} if m.role == "assistant" and m.usage else {} ),
                # Structured AskUser payload on a question turn → the interactive card
                # re-renders on a chat switch / reload (not just the summary text).
                **( {"ask_user": m.ask_user} if isinstance(m.ask_user, dict) and m.ask_user else {} ),
            }
            for m in messages
        ],
    }
