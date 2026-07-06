"""ConnectorEngine REST surface — channel status + live Telegram management.

* ``GET  /connectors``                 (bearer): a ``status()`` dict per channel.
  Telegram is ALWAYS present (synthesized from settings when disabled) so the
  dashboard panel has a stable shape whether or not the bridge is running.
* ``GET  /connectors/telegram``        (bearer): the Telegram detail snapshot
  (adds ``token_hint`` + the actual ``allowed_chat_ids`` list — neither secret).
* ``PUT  /connectors/telegram``        (bearer): partial update of
  ``{enabled?, allowed_chat_ids?, bot_token?}`` → validate → persist (runtime
  store + secret store) → ``rebuild_app_settings`` → ``reload_connectors`` →
  fresh snapshot. This is the live enable/disable seam: no process restart.
* ``POST /connectors/telegram/test``   (bearer): ``getMe`` against the resolved
  token — a side-effect-free "is this token live?" check.
* ``POST /connectors/telegram/discover`` (bearer): chats that have messaged the
  bot, for one-click allowlisting (reads the live poll buffer when running, else
  a one-shot non-consuming ``getUpdates``). Each is annotated ``allowed``.
* ``POST /connectors/telegram/bind``    (bearer): "Continue on Telegram" —
  ``{conversation_id, chat_id}`` binds an existing web conversation to a Telegram
  chat (same ``ChannelBindingStore`` the inbound router reads) and best-effort
  sends a bilingual confirmation to that chat.

A secret is never returned as a value; only ``token_set`` + a ``…last4`` hint.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request

from akana_server.api.deps import require_akana_bearer
from akana_server.api.errors import http_error
from akana_server.config import clean_secret_value
from akana_server.connectors.base import ConnectorSendError
from akana_server.connectors.conversation import ChannelBindingStore
from akana_server.connectors.service import reload_connectors
from akana_server.connectors.telegram import (
    TelegramConnector,
    discover_chats,
    resolve_bot_token,
    verify_bot_token,
)
from akana_server.conversation_service import ConversationService
from akana_server.runtime_settings import (
    SCHEMA,
    RuntimeSettingError,
    get_store,
    rebuild_app_settings,
    validate_value,
)
from akana_server.secret_store import is_real_secret, mask_hint, set_secrets

router = APIRouter(tags=["connectors"])


def _telegram_snapshot(settings: Any, registry: Any) -> dict[str, Any]:
    """Telegram status for the management panel — never carries the raw token.

    Prefers the LIVE connector's ``status()`` (so ``running``/``last_error``
    reflect the actual poll task); when the bridge is disabled it is absent from
    the registry, so a throwaway :class:`TelegramConnector` is built purely to
    read ``status()`` — that call is cheap and does no network I/O. Adds two
    panel-only fields: ``token_hint`` (``…last4``) and the real
    ``allowed_chat_ids`` list so the editor can be populated. ``token_set`` is
    tightened to :func:`is_real_secret` (matching the credentials badge) so a
    leftover ``.env.example`` placeholder reports UNSET instead of "configured".
    """
    connector = registry.get("telegram") if registry is not None else None
    if connector is None:
        connector = TelegramConnector(settings)
    snap = connector.status()
    token = resolve_bot_token(settings)
    real = is_real_secret(token)
    snap["token_set"] = real
    snap["token_hint"] = mask_hint(token) if real else None
    snap["allowed_chat_ids"] = [
        str(c) for c in (getattr(settings, "telegram_allowed_chat_ids", ()) or ())
    ]
    return snap


@router.get("/connectors", dependencies=[Depends(require_akana_bearer)])
async def list_connectors(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    registry = getattr(request.app.state, "connector_registry", None)
    live = list(registry.status()) if registry is not None else []
    # Telegram is ALWAYS present with the enriched panel shape (token_hint +
    # allowed_chat_ids), whether the bridge is enabled or not — a disabled channel
    # is absent from the registry, so the snapshot is synthesized from settings.
    others = [c for c in live if not (isinstance(c, dict) and c.get("id") == "telegram")]
    connectors = [*others, _telegram_snapshot(settings, registry)]
    return {"connectors": connectors, "count": len(connectors)}


@router.get("/connectors/telegram", dependencies=[Depends(require_akana_bearer)])
async def get_telegram(request: Request) -> dict[str, Any]:
    settings = request.app.state.settings
    registry = getattr(request.app.state, "connector_registry", None)
    return _telegram_snapshot(settings, registry)


@router.put("/connectors/telegram", dependencies=[Depends(require_akana_bearer)])
async def update_telegram(request: Request) -> dict[str, Any]:
    """Partial update of the Telegram bridge, applied live (no restart).

    Body keys (all optional): ``enabled`` (bool), ``allowed_chat_ids`` (list or
    comma string), ``bot_token`` (string; empty clears). Validation reuses the
    runtime-settings specs and the secret-store real-secret gate, so the panel
    enforces exactly the same rules as the generic Settings form. On success the
    runtime store + secret store are written, the live settings snapshot is
    rebuilt, and the connector registry is torn down and rebuilt from it — so a
    toggle/token/allowlist change takes effect immediately.
    """
    settings = request.app.state.settings
    try:
        raw = await request.json()
    except Exception as e:
        raise http_error(400, "INVALID_JSON", f"Invalid JSON body: {e}") from e
    if not isinstance(raw, dict) or not raw:
        raise http_error(
            422,
            "INVALID_BODY",
            'The body must be an object, e.g. {"enabled": true}',
        )

    validated: dict[str, Any] = {}
    secret_patch: dict[str, str] = {}
    field_errors: dict[str, str] = {}

    if "enabled" in raw:
        try:
            validated["telegram_enabled"] = validate_value(
                SCHEMA["telegram_enabled"], raw["enabled"]
            )
        except RuntimeSettingError as e:
            field_errors["enabled"] = str(e)
    if "allowed_chat_ids" in raw:
        try:
            validated["telegram_allowed_chat_ids"] = validate_value(
                SCHEMA["telegram_allowed_chat_ids"], raw["allowed_chat_ids"]
            )
        except RuntimeSettingError as e:
            field_errors["allowed_chat_ids"] = str(e)
    if "bot_token" in raw:
        tok = raw["bot_token"]
        if not isinstance(tok, str):
            field_errors["bot_token"] = "Bot token must be a string (or empty to clear)."
        elif clean_secret_value(tok) and not is_real_secret(tok):
            # A non-empty value that fails the real-secret gate is a placeholder or
            # truncated paste — reject now instead of "saving" a token that never works.
            field_errors["bot_token"] = (
                "The bot token looks like a placeholder or is too short — paste the "
                "real value, or send an empty string to clear it."
            )
        else:
            secret_patch["telegram_bot_token"] = tok  # empty string clears

    if field_errors:
        raise http_error(
            422,
            "VALIDATION",
            "Some fields could not be validated; no changes were applied.",
            fields=field_errors,
        )
    if not validated and not secret_patch:
        raise http_error(422, "INVALID_BODY", "Nothing to update.")

    store = get_store(settings.data_dir)
    # Secrets FIRST (mirrors vault.py's 'keyfile FIRST' ordering): set_secrets can
    # raise beyond OSError — assert_writable rejects a corrupt/unreadable vault before
    # the merge, and the atomic write itself can fail. If the runtime store were written
    # first, a set_secrets failure would leave telegram_enabled durably persisted while
    # the request 500s implying nothing changed and no reload runs — the bridge then
    # comes up enabled with no/stale token on the next restart. Writing (and validating)
    # the vault before any durable runtime-store change makes the failure abort cleanly.
    if secret_patch:
        try:
            set_secrets(settings.data_dir, secret_patch)
        except Exception as e:
            raise http_error(
                500,
                "PERSIST_FAILED",
                "Settings could not be saved due to a storage error; no changes were applied.",
            ) from e
    if validated:
        try:
            # Atomic multi-key write: a per-key loop could persist telegram_enabled
            # and then fail on telegram_allowed_chat_ids (disk full / read-only /
            # AV lock on os.replace), leaving the bridge enabled with a stale
            # allowlist after the next restart. set_many is all-or-none; a storage
            # failure surfaces as the canonical envelope, not a bare 500.
            store.set_many(validated)
        except OSError as e:
            raise http_error(
                500,
                "PERSIST_FAILED",
                "Settings could not be saved due to a storage error; no changes were applied.",
            ) from e

    # Rebuild the live settings snapshot from the fresh store, then bounce the
    # connector registry so the change is live (build_registry only registers an
    # enabled channel → a disabled one stops, an enabled one (re)starts).
    rebuild_app_settings(request.app)
    await reload_connectors(request.app)

    settings = request.app.state.settings
    registry = getattr(request.app.state, "connector_registry", None)
    snap = _telegram_snapshot(settings, registry)
    snap["reloaded"] = True
    snap["changed"] = [*validated.keys(), *(["bot_token"] if secret_patch else [])]
    return snap


@router.post(
    "/connectors/telegram/test", dependencies=[Depends(require_akana_bearer)]
)
async def test_telegram(request: Request) -> dict[str, Any]:
    """Validate the resolved bot token via ``getMe`` (no side effects).

    Resolves the effective token (secret store → env), then calls Telegram's
    ``getMe``. Returns the bot identity on success; 400 ``NO_TOKEN`` when nothing
    is set, 400 ``TEST_FAILED`` (token-sanitized) when Telegram rejects it.
    """
    settings = request.app.state.settings
    token = resolve_bot_token(settings)
    if not token:
        raise http_error(400, "NO_TOKEN", "No bot token is set.")
    try:
        result = await verify_bot_token(token)
    except ConnectorSendError as e:
        raise http_error(400, "TEST_FAILED", str(e)) from e
    return {"ok": True, "bot": result}


@router.post(
    "/connectors/telegram/discover", dependencies=[Depends(require_akana_bearer)]
)
async def discover_telegram(request: Request) -> dict[str, Any]:
    """List chats that have messaged the bot — for one-click allowlisting.

    Solves the "what's my numeric chat id?" friction: the user sends ``/start`` to
    the bot, then scans. The source depends on whether the bridge is polling:

    * **poll loop running** (enabled + non-empty allowlist): reads the live
      connector's in-memory ``recent_chats`` buffer — the loop owns ``getUpdates``
      and a second concurrent call would get a 409 from Telegram.
    * **not polling** (disabled, or enabled with an empty allowlist): a one-shot
      ``getUpdates`` that does NOT consume the updates, so cold-start works.

    Each chat is annotated ``allowed`` against the CURRENT allowlist so the panel
    can disable "Add" for chats already on it. Never returns message text.
    """
    settings = request.app.state.settings
    token = resolve_bot_token(settings)
    if not token:
        raise http_error(400, "NO_TOKEN", "No bot token is set.")
    registry = getattr(request.app.state, "connector_registry", None)
    connector = registry.get("telegram") if registry is not None else None
    if connector is not None and connector.status().get("running"):
        # The poll loop is live → reading getUpdates again would 409; use its buffer.
        chats = connector.recent_chats()
        source = "buffer"
    else:
        try:
            chats = await discover_chats(token)
        except ConnectorSendError as e:
            raise http_error(400, "DISCOVER_FAILED", str(e)) from e
        source = "poll"
    allowed = {
        str(c).strip() for c in (getattr(settings, "telegram_allowed_chat_ids", ()) or ())
    }
    out = [{**c, "allowed": str(c.get("id", "")).strip() in allowed} for c in chats]
    return {"ok": True, "source": source, "chats": out, "count": len(out)}


def _bind_confirmation_text(settings: Any) -> str:
    """Bilingual confirmation sent to the chat after a bind — follows the runtime
    ``language`` setting, the same source the router's persona/title logic reads
    (mirrors ``chat_titler._title_language``: ``tr``/``en``, default ``en``)."""
    try:
        from akana_server.runtime_settings import get_runtime

        lang = str(get_runtime("language", settings) or "").strip().lower()
    except Exception:
        lang = "en"
    if lang == "tr":
        return "Bu sohbet artık web'deki bu konuşmayla bağlantılı. Buradan yazdığın mesajlar aynı konuşmaya eklenecek."
    return "This chat is now linked to that web conversation. Messages sent here will continue it."


@router.post(
    "/connectors/telegram/bind", dependencies=[Depends(require_akana_bearer)]
)
async def bind_telegram(request: Request) -> dict[str, Any]:
    """"Continue on Telegram": bind an existing web conversation to a Telegram chat_id.

    Body: ``{"conversation_id": str, "chat_id": str}``. Validates Telegram is
    enabled, ``chat_id`` is on the allowlist, and the conversation exists, then
    writes the binding (same store the inbound router reads/writes) and sends a
    short bilingual confirmation to the chat. A send failure does not fail the
    request — the binding is already durable — it is reported via
    ``notified: false`` so the UI can still show success.
    """
    settings = request.app.state.settings
    try:
        raw = await request.json()
    except Exception as e:
        raise http_error(400, "INVALID_JSON", f"Invalid JSON body: {e}") from e
    if not isinstance(raw, dict):
        raise http_error(422, "INVALID_BODY", "The body must be an object.")
    conversation_id = str(raw.get("conversation_id") or "").strip()
    chat_id = str(raw.get("chat_id") or "").strip()
    if not conversation_id or not chat_id:
        raise http_error(
            422, "INVALID_BODY", "Both conversation_id and chat_id are required."
        )

    if not getattr(settings, "telegram_enabled", False):
        raise http_error(400, "TELEGRAM_DISABLED", "Telegram is not enabled.")
    allowed = {
        str(c).strip() for c in (getattr(settings, "telegram_allowed_chat_ids", ()) or ())
    }
    if chat_id not in allowed:
        raise http_error(
            403,
            "CHAT_NOT_ALLOWED",
            "This chat_id is not on the Telegram allowlist.",
        )

    conversations = getattr(request.app.state, "conversation_service", None)
    if not isinstance(conversations, ConversationService):
        raise http_error(503, "CONVERSATIONS_UNAVAILABLE", "The conversation service is not ready yet.")
    meta = conversations.get(conversation_id)
    if meta is None:
        raise http_error(404, "CONVERSATION_NOT_FOUND", "No such conversation.")

    # Reuse the inbound router's OWN ChannelBindingStore when it is live. Constructing
    # a fresh store here is now safe for lost-update races (the file lock is keyed on the
    # path, cross-instance), but reusing the router's instance avoids a second store
    # object entirely. Run bind() off the event loop so its file I/O + lock wait do not
    # block the loop, mirroring the router's own to_thread writes.
    router = getattr(request.app.state, "connector_router", None)
    bindings = getattr(router, "_bindings", None)
    if bindings is None:
        bindings = ChannelBindingStore(Path(settings.data_dir))
    await asyncio.to_thread(bindings.bind, "telegram", chat_id, conversation_id)

    notified = True
    registry = getattr(request.app.state, "connector_registry", None)
    if registry is not None:
        try:
            await registry.send_to("telegram", chat_id, _bind_confirmation_text(settings))
        except Exception:
            notified = False
    else:
        notified = False

    return {
        "ok": True,
        "chat_id": chat_id,
        "conversation_id": conversation_id,
        "notified": notified,
    }
