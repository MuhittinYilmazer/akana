"""ConnectorEngine F2.1 (MVP) — Telegram bot, long-polling.

No webhook (no public endpoint): the ``getUpdates`` long-poll loop runs as a
single background task. The bot token is resolved first from the secret store
(``telegram_bot_token``), falling back to the env var
(``AKANA_TELEGRAM_BOT_TOKEN``); it never leaks into status output.

Security boundary: ``AKANA_TELEGRAM_ALLOWED_CHAT_IDS`` allowlist. Messages
arriving from a chat not on the list are SILENTLY ignored (no reply — the
bot's existence is not confirmed), and a ``connector_chat_denied`` line is
written to the audit log.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx

from akana_server import audit
from akana_server.connectors.base import (
    ConnectorSendError,
    InboundMessage,
    OutboundMessage,
)

if TYPE_CHECKING:
    from akana_server.config import Settings

__all__ = [
    "MAX_MESSAGE_LEN",
    "TelegramConnector",
    "discover_chats",
    "resolve_bot_token",
    "verify_bot_token",
]

log = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
#: Telegram sendMessage text limit — the router splits long replies accordingly.
MAX_MESSAGE_LEN = 4096
#: Telegram getUpdates long-poll duration (seconds) — the request hangs for this long.
DEFAULT_POLL_TIMEOUT = 25
#: Backoff duration when the poll loop encounters an error (prevents a tight loop on API outage).
DEFAULT_ERROR_BACKOFF = 5.0
#: Max distinct chats kept in the in-memory discovery buffer (LRU by last-seen).
SEEN_CHATS_CAP = 50


#: ``/bot<token>/`` URL segment — used to mask tokens that leak into error messages.
#: Telegram token format is ``<bot_id>:<secret>``; httpx exceptions can embed the
#: request URL (``.../bot123456:ABC.../getUpdates``) in their message → would leak to dashboards.
_BOT_TOKEN_IN_URL = re.compile(r"/bot[^/\s]+")


def _sanitize_error(message: str) -> str:
    """Mask the ``/bot<token>/`` segment in error text with ``/bot***``.

    ``_last_error`` is returned to an authenticated caller via
    ``GET /api/v1/connectors``; the raw exception may carry the request URL
    (and therefore the bot token). Strip the token before storing it
    (defence in depth)."""
    return _BOT_TOKEN_IN_URL.sub("/bot***", message)


def _chat_descriptor(chat: dict[str, Any]) -> dict[str, Any]:
    """Compact, display-ready view of a Telegram ``chat`` object for discovery.

    Normalizes the varying chat shapes (private DM vs group/supergroup/channel)
    into one ``{id, type, title, username}`` record the dashboard renders as
    "Name (id) [Add]". Carries no message text."""
    title = (
        chat.get("title")  # group / supergroup / channel
        or " ".join(p for p in (chat.get("first_name"), chat.get("last_name")) if p)  # private
        or chat.get("username")
        or ""
    )
    return {
        "id": str(chat.get("id", "")).strip(),
        "type": str(chat.get("type") or ""),
        "title": str(title).strip(),
        "username": str(chat.get("username") or ""),
    }


def resolve_bot_token(settings: Settings) -> str:
    """Resolve bot token: runtime secret store wins, env (Settings) is the fallback."""
    data_dir = getattr(settings, "data_dir", None)
    if data_dir is not None:
        try:
            from akana_server.secret_store import get_secret

            stored = get_secret(data_dir, "telegram_bot_token")
            if stored:
                return stored
        except Exception:  # pragma: no cover - store unreadable → env fallback
            pass
    return getattr(settings, "telegram_bot_token", None) or ""


async def verify_bot_token(
    token: str,
    *,
    api_base: str = API_BASE,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    """Validate a bot token via ``getMe`` — returns the bot identity or raises.

    Powers the dashboard "Test connection" action: a quick, side-effect-free way
    to confirm the token is live before relying on it. On success returns
    ``{ok, id, username, first_name}``; on any failure raises
    :class:`ConnectorSendError` with a token-sanitized message (the request URL
    embeds the token, so it must never reach a dashboard/log verbatim)."""
    if not token:
        raise ConnectorSendError("telegram: no bot token")
    client = httpx.AsyncClient(transport=transport, timeout=timeout)
    try:
        resp = await client.get(f"{api_base.rstrip('/')}/bot{token}/getMe")
        data = resp.json()
        if not isinstance(data, dict) or not data.get("ok"):
            raise ConnectorSendError(f"getMe failed: {str(data)[:200]}")
        result = data.get("result")
        result = result if isinstance(result, dict) else {}
        return {
            "ok": True,
            "id": result.get("id"),
            "username": result.get("username"),
            "first_name": result.get("first_name"),
        }
    except ConnectorSendError:
        raise
    except Exception as e:
        raise ConnectorSendError(_sanitize_error(f"telegram getMe error: {e}")) from e
    finally:
        await client.aclose()


async def discover_chats(
    token: str,
    *,
    api_base: str = API_BASE,
    transport: httpx.AsyncBaseTransport | None = None,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """List the distinct chats that have messaged the bot — WITHOUT consuming them.

    Powers the dashboard "Scan for chats" action when the long-poll loop is NOT
    running. Telegram permits only ONE ``getUpdates`` consumer per token, so this
    must never run while the poll loop is active (the API answers 409); in that
    case the caller reads the live connector's :meth:`TelegramConnector.recent_chats`
    buffer instead. Passing no ``offset`` returns the currently-buffered updates and
    does NOT advance the server-side offset, so the real loop still receives them
    later. Most-recent occurrence wins per chat."""
    if not token:
        raise ConnectorSendError("telegram: no bot token")
    client = httpx.AsyncClient(transport=transport, timeout=timeout)
    seen: dict[str, dict[str, Any]] = {}
    try:
        resp = await client.get(
            f"{api_base.rstrip('/')}/bot{token}/getUpdates",
            params={"timeout": 0, "allowed_updates": json.dumps(["message"])},
        )
        data = resp.json()
        if not isinstance(data, dict) or not data.get("ok"):
            raise ConnectorSendError(f"getUpdates failed: {str(data)[:200]}")
        for update in data.get("result") or []:
            if not isinstance(update, dict):
                continue
            message = update.get("message")
            if not isinstance(message, dict):
                continue
            chat = message.get("chat")
            chat = chat if isinstance(chat, dict) else {}
            cid = str(chat.get("id", "")).strip()
            if cid:
                seen[cid] = _chat_descriptor(chat)  # latest occurrence wins
        return list(seen.values())
    except ConnectorSendError:
        raise
    except Exception as e:
        raise ConnectorSendError(_sanitize_error(f"telegram getUpdates error: {e}")) from e
    finally:
        await client.aclose()


class TelegramConnector:
    """Single Telegram bot account — long-poll receiver + sendMessage sender."""

    connector_id = "telegram"
    #: The router uses this limit to split long replies into chunks.
    max_message_len = MAX_MESSAGE_LEN

    def __init__(
        self,
        settings: Settings,
        *,
        api_base: str = API_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_timeout: int = DEFAULT_POLL_TIMEOUT,
        error_backoff: float = DEFAULT_ERROR_BACKOFF,
    ) -> None:
        self._settings = settings
        self._api_base = api_base.rstrip("/")
        self._transport = transport  # test: httpx.MockTransport (no real network)
        self._poll_timeout = max(0, int(poll_timeout))
        self._error_backoff = max(0.0, float(error_backoff))
        self._allowed: frozenset[str] = frozenset(
            str(c).strip()
            for c in (getattr(settings, "telegram_allowed_chat_ids", ()) or ())
            if str(c).strip()
        )
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[InboundMessage] | None = None
        self._offset = 0
        self._last_error = ""
        self._last_message_at: str | None = None
        #: Discovery buffer — chats seen since start (allowed OR denied), LRU by
        #: last-seen, so the dashboard can offer a new chat for one-click allow.
        self._seen_chats: dict[str, dict[str, Any]] = {}

    # -- lifecycle -----------------------------------------------------

    async def start(self, inbound: asyncio.Queue[InboundMessage]) -> None:
        if self._task is not None and not self._task.done():
            return  # idempotent
        token = resolve_bot_token(self._settings)
        if not token:
            self._last_error = "no bot token (secret_store: telegram_bot_token or AKANA_TELEGRAM_BOT_TOKEN)"
            log.warning("telegram: %s — polling not started", self._last_error)
            return
        if not self._allowed:
            # With an empty allowlist, EVERY chat is rejected; is listening worthwhile?
            # No — an empty allowlist is effectively closed; avoid burning the quota.
            self._last_error = "allowlist empty (AKANA_TELEGRAM_ALLOWED_CHAT_IDS) — polling not started"
            log.warning("telegram: %s", self._last_error)
            return
        self._queue = inbound
        self._client = httpx.AsyncClient(
            transport=self._transport, timeout=self._poll_timeout + 10
        )
        self._last_error = ""
        self._task = asyncio.create_task(self._supervised_poll(token), name="telegram-poll")

    async def stop(self) -> None:
        task, self._task = self._task, None
        client, self._client = self._client, None
        if task is not None and not task.done():
            task.cancel()
            # If the getUpdates long-poll (25 s) is in flight, cancel propagation
            # depends on httpx aborting the request. Closing the client FIRST
            # forcibly cuts the in-flight request; we then await the task with a
            # bounded timeout — this prevents the poll from continuing after
            # shutdown (observed: getUpdates kept running after shutdown).
            # Shutdown proceeds even if the timeout fires.
            if client is not None:
                try:
                    await client.aclose()
                except Exception:  # pragma: no cover - defensive on shutdown
                    log.debug("telegram client close raised an error", exc_info=True)
                client = None
            try:
                await asyncio.wait_for(task, timeout=10.0)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:  # pragma: no cover - shutdown edge
                log.warning("telegram poll task did not stop within 10 s; shutdown continues")
            except Exception:  # pragma: no cover - unexpected error inside task
                log.debug("telegram poll task close raised an error", exc_info=True)
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # pragma: no cover
                log.debug("telegram client close raised an error", exc_info=True)

    # -- receiver (long poll) ---------------------------------------------------

    async def _supervised_poll(self, token: str) -> None:
        """Watchdog: restart the poll loop if it dies UNEXPECTEDLY.

        Normal errors (Exception) are already handled inside ``_poll_loop``
        with a backoff. This layer only kicks in when something that
        ``_poll_loop`` does not catch (e.g. a ``MemoryError``-class
        BaseException) kills the loop entirely — otherwise the bot goes deaf
        silently with no crash in the log. CancelledError (shutdown) still
        propagates cleanly.
        """
        while True:
            try:
                await self._poll_loop(token)
                return  # _poll_loop does not normally return; if it does, treat it as clean exit
            except asyncio.CancelledError:
                raise
            except BaseException as e:  # noqa: BLE001 — intentional: keep bot alive
                self._last_error = _sanitize_error(
                    f"poll loop died, restarting: {str(e)[:200]}"
                )
                log.error("telegram: %s", self._last_error)
                await asyncio.sleep(5.0)

    async def _poll_loop(self, token: str) -> None:
        assert self._client is not None and self._queue is not None  # noqa: S101
        while True:
            try:
                resp = await self._client.get(
                    f"{self._api_base}/bot{token}/getUpdates",
                    params={
                        "timeout": self._poll_timeout,
                        "offset": self._offset,
                        "allowed_updates": json.dumps(["message"]),
                    },
                )
                data = resp.json()
                if not isinstance(data, dict) or not data.get("ok"):
                    raise RuntimeError(f"getUpdates failed: {str(data)[:200]}")
                for update in data.get("result") or []:
                    await self._handle_update(update)
                self._last_error = ""
                # The long-poll already waits on the server side; still yield to
                # the event loop (test transports return instantly — prevent starvation).
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._last_error = _sanitize_error(str(e)[:300])
                log.warning("telegram poll error (retrying in %.1f s): %s",
                            self._error_backoff, self._last_error)
                await asyncio.sleep(self._error_backoff)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        try:
            self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
        except (TypeError, ValueError):
            pass
        message = update.get("message")
        if not isinstance(message, dict):
            return
        # chat/from may not be a dict in a malformed body (could be str/None).
        # Without this guard, ``.get`` would raise AttributeError, dropping
        # ``_poll_loop`` into backoff and a single broken update would stall the
        # entire long-poll loop (offset already advanced, but the loop would
        # throw the same error on every iteration). Defensively fall back to an
        # empty dict.
        chat = message.get("chat")
        chat = chat if isinstance(chat, dict) else {}
        chat_id = str(chat.get("id", "")).strip()
        if chat_id:
            # Record EVERY chat (allowed or not) for discovery — a not-yet-allowed
            # chat is exactly what the dashboard "Scan for chats" needs to surface.
            self._record_seen_chat(chat)
        text = str(message.get("text") or "").strip()
        if not chat_id or not text:
            return  # non-text content (photo/audio) is F2+; ignore for now
        if chat_id not in self._allowed:
            # Silent reject: NO reply (bot's existence is not confirmed), audit only.
            self._audit_denied(chat_id, message)
            return
        sender = message.get("from")
        sender = sender if isinstance(sender, dict) else {}
        self._last_message_at = datetime.now(timezone.utc).isoformat()
        assert self._queue is not None  # noqa: S101 - populated after start()
        # Putting to the queue is IMMEDIATE: processing continues asynchronously
        # in the router. The getUpdates long-poll timeout (25 s) does NOT affect
        # reply latency — Telegram returns the request immediately on a new
        # message, and the loop moves on without waiting.
        await self._queue.put(
            InboundMessage(
                connector_id=self.connector_id,
                chat_id=chat_id,
                text=text,
                sender_id=str(sender.get("id", "")),
                sender_name=str(sender.get("first_name") or sender.get("username") or ""),
                message_id=str(message.get("message_id", "")),
                raw=update,
            )
        )

    def _audit_denied(self, chat_id: str, message: dict[str, Any]) -> None:
        data_dir = getattr(self._settings, "data_dir", None)
        if data_dir is None:
            return
        sender = message.get("from")
        sender = sender if isinstance(sender, dict) else {}
        audit.write_event(
            data_dir,
            "connector_chat_denied",
            data={
                "connector": self.connector_id,
                "chat_id": chat_id,
                "sender_id": str(sender.get("id", "")),
                "reason": "chat not in allowlist — silently ignored",
            },
        )

    # -- discovery buffer ------------------------------------------------------

    def _record_seen_chat(self, chat: dict[str, Any]) -> None:
        """Remember a chat that messaged the bot (allowed OR denied) for discovery.

        Feeds the dashboard "Scan for chats" buffer so a new chat can be added to
        the allowlist without hunting for its numeric id. Bounded LRU (most-recent
        last); re-seeing a chat refreshes its position and ``last_seen``."""
        desc = _chat_descriptor(chat)
        cid = desc["id"]
        if not cid:
            return
        desc["last_seen"] = datetime.now(timezone.utc).isoformat()
        self._seen_chats.pop(cid, None)  # move-to-end on refresh
        self._seen_chats[cid] = desc
        while len(self._seen_chats) > SEEN_CHATS_CAP:
            self._seen_chats.pop(next(iter(self._seen_chats)), None)

    def recent_chats(self) -> list[dict[str, Any]]:
        """Distinct chats seen since start, most-recent first (discovery buffer)."""
        return list(reversed(self._seen_chats.values()))

    # -- sender -----------------------------------------------------------

    async def send(self, message: OutboundMessage) -> None:
        token = resolve_bot_token(self._settings)
        if not token:
            raise ConnectorSendError("telegram: no bot token")
        client = self._client
        close_after = False
        if client is None:  # allow sending even when polling is disabled
            client = httpx.AsyncClient(transport=self._transport, timeout=30)
            close_after = True
        try:
            resp = await client.post(
                f"{self._api_base}/bot{token}/sendMessage",
                json={"chat_id": message.chat_id, "text": message.text},
            )
            data = resp.json()
            if not isinstance(data, dict) or not data.get("ok"):
                raise ConnectorSendError(f"sendMessage failed: {str(data)[:200]}")
        except ConnectorSendError:
            raise
        except Exception as e:
            raise ConnectorSendError(_sanitize_error(f"telegram send error: {e}")) from e
        finally:
            if close_after:
                await client.aclose()

    # -- status ----------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        return {
            "id": self.connector_id,
            "enabled": bool(getattr(self._settings, "telegram_enabled", False)),
            "running": self._task is not None and not self._task.done(),
            "mode": "long_poll",
            "token_set": bool(resolve_bot_token(self._settings)),
            "allowed_chat_count": len(self._allowed),
            "last_error": self._last_error or None,
            "last_message_at": self._last_message_at,
        }
