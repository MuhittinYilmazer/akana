"""ScheduleEngine REST surface — reminders + recurring scheduled prompts.

* ``GET    /schedule``            (bearer): every schedule (public shape).
* ``POST   /schedule``            (bearer): create one. Body:
  ``{title, prompt, kind, when, weekday?, delivery?{mode,channel,chat_id,
  conversation_id}, language?}``. Flat ``delivery_mode``/``channel``/``chat_id``/
  ``conversation_id`` keys are also accepted.
* ``PATCH  /schedule/{id}``       (bearer): partial update of the same fields.
* ``DELETE /schedule/{id}``       (bearer): cancel (delete) a schedule.
* ``POST   /schedule/{id}/run``   (bearer): fire it NOW regardless of timing
  (test / manual-run). Runs the LLM turn and delivers, returning the outcome.

Bearer-protected like the other management routes (loopback owner skips the token
when none is configured — see :func:`require_akana_bearer`). Validation errors
come back as a clean 422; an unknown id is a 404. Business logic (validation,
Turkish natural-language datetime, the connector-enabled safety gate) is reused
from :class:`akana_server.schedule.tools.ScheduleTools` so the REST and tool
surfaces enforce identical rules; REST-created schedules are tagged
``created_by="user"``.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from akana_server.api.deps import require_akana_bearer
from akana_server.api.errors import http_error
from akana_server.schedule import get_schedule_store
from akana_server.schedule.engine import run_schedule_now
from akana_server.schedule.tools import ScheduleTools

router = APIRouter(tags=["schedule"])


async def _json_object(request: Request) -> dict[str, Any]:
    try:
        raw = await request.json()
    except Exception as exc:  # noqa: BLE001 - any parse failure → 400
        raise http_error(400, "INVALID_JSON", f"Invalid JSON body: {exc}") from exc
    if not isinstance(raw, dict):
        raise http_error(422, "INVALID_BODY", "The request body must be a JSON object.")
    return raw


def _flatten_body(body: dict[str, Any]) -> dict[str, Any]:
    """Accept both a nested ``delivery`` object and flat delivery keys, and hand
    the ScheduleTools surface the flat args it expects."""
    flat: dict[str, Any] = {}
    for key in ("title", "prompt", "message", "kind", "when", "weekday", "language"):
        if key in body:
            flat[key] = body[key]
    for key in ("delivery_mode", "mode", "channel", "chat_id", "conversation_id"):
        if key in body:
            flat[key] = body[key]
    delivery = body.get("delivery")
    if isinstance(delivery, dict):
        if "mode" in delivery:
            flat["delivery_mode"] = delivery["mode"]
        for key in ("channel", "chat_id", "conversation_id"):
            if key in delivery:
                flat[key] = delivery[key]
    return flat


def _error_response(name: str, result: dict[str, Any]) -> None:
    """Map a ScheduleTools ``{"error": ...}`` to the right HTTP status."""
    message = str(result.get("error") or "invalid request")
    if "no schedule with id" in message:
        raise http_error(404, "NOT_FOUND", message)
    raise http_error(422, "VALIDATION", message)


@router.get("/schedule", dependencies=[Depends(require_akana_bearer)])
async def list_schedules(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    items = get_schedule_store(settings.data_dir).load()
    return {"schedules": [i.public_dict() for i in items], "count": len(items)}


@router.post("/schedule", dependencies=[Depends(require_akana_bearer)])
async def create_schedule(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    body = await _json_object(request)
    tools = ScheduleTools(settings.data_dir, created_by="user")
    result = tools.handle_tool_call("schedule_create", _flatten_body(body))
    if result.get("error"):
        _error_response("schedule_create", result)
    return result


@router.patch("/schedule/{schedule_id}", dependencies=[Depends(require_akana_bearer)])
async def update_schedule(schedule_id: str, request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    body = await _json_object(request)
    tools = ScheduleTools(settings.data_dir, created_by="user")
    args = {**_flatten_body(body), "id": schedule_id}
    result = tools.handle_tool_call("schedule_update", args)
    if result.get("error"):
        _error_response("schedule_update", result)
    return result


@router.delete("/schedule/{schedule_id}", dependencies=[Depends(require_akana_bearer)])
async def delete_schedule(schedule_id: str, request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    removed = get_schedule_store(settings.data_dir).cancel(schedule_id)
    if not removed:
        raise http_error(404, "NOT_FOUND", f"No schedule with id {schedule_id}.")
    return {"id": schedule_id, "removed": True, "status": "cancelled"}


@router.post("/schedule/{schedule_id}/run", dependencies=[Depends(require_akana_bearer)])
async def run_schedule(schedule_id: str, request: Request) -> dict[str, Any]:
    """Fire a schedule immediately (manual run / UI test button).

    Runs the LLM turn and delivers exactly as the background engine would, using
    the live connector registry + conversation service on ``app.state``. Returns
    the run outcome; a ``once`` schedule is disabled and a recurring one rolls
    forward, same as a scheduled fire."""
    settings = request.app.state.settings
    outcome = await run_schedule_now(
        settings,
        schedule_id,
        registry=getattr(request.app.state, "connector_registry", None),
        conversations=getattr(request.app.state, "conversation_service", None),
        app=request.app,
    )
    if outcome is None:
        raise http_error(404, "NOT_FOUND", f"No schedule with id {schedule_id}.")
    return {"ok": True, "run": outcome}
