"""ConnectorEngine F2 — inbound router: queue → conversation → LLM → reply.

Flow (F2 implementation of CONNECTOR_VISION_PLAN §10.1):

1. Pull an :class:`InboundMessage` from the registry's shared queue,
2. (FULL AUTONOMY: the old risk/approval policy gate has been removed — inbound
   messages are not blocked; they flow directly into the command/conversation path),
3. Telegram-local slash commands (``/yeni``, ``/durum``) — the connector's own
   explicit-slash-command surface (the web chat command short-circuit was removed),
4. the message is bound to its persistent conversation (ConversationService;
   ``channel="telegram"`` meta + «Telegram: <name>» title — visible in the web
   UI conversation list); the last N turns within the character budget are passed
   to ``complete_chat`` as history, and the turn pair is written to the episodic archive,
5. skill injection (WI-1 pure helpers): a strong-match skill's SKILL.md body is
   prepended to the prompt before the LLM call (FULL AUTONOMY — no approval gate),
6. the reply passes through the egress filter (OTP/credential masking + audit)
   and is split to the channel's message limit (Telegram 4096) before being sent back.

No exception propagates from any step: an LLM/send error becomes a short error
message to the user, and the loop keeps running. When conversation services are
absent (legacy setup / tests) the stateless single-shot behaviour from F1 is
preserved exactly.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from akana_server import audit
from akana_server.connectors.base import (
    InboundMessage,
    OutboundMessage,
    split_text,
)
from akana_server.connectors.conversation import (
    ChannelBindingStore,
    channel_title,
    parse_command,
    resolve_history_budget,
    trim_history,
)
from akana_server.connectors.egress_filter import filter_outbound
from akana_server.observability import begin_turn, capture_failure

if TYPE_CHECKING:
    from akana_server.config import Settings
    from akana_server.connectors.registry import ConnectorRegistry
    from akana_server.conversation_service import ConversationService

__all__ = [
    "EMPTY_REPLY",
    "LLM_ERROR_REPLY",
    "NEW_CONVERSATION_REPLY",
    "InboundRouter",
]

log = logging.getLogger(__name__)

#: Short message returned to the channel when an LLM call fails (no details leak).
LLM_ERROR_REPLY = "Sorry, I can't generate a reply right now. Please try again shortly."
#: Returned when the LLM produces empty text.
EMPTY_REPLY = "I couldn't find anything to say right now."
#: Message returned after the ``/yeni`` command.
NEW_CONVERSATION_REPLY = (
    "New conversation started. The previous chat is still in the list in the web UI."
)

#: Default turn window for ConversationService.recent_llm_messages.
_DEFAULT_MAX_TURNS = 12

#: Concurrency ceiling: at most this many DIFFERENT conversations send to the LLM
#: at once (provider / rate-limit protection). The same conversation is already serial (single worker).
_DEFAULT_MAX_CONCURRENCY = 8
#: Per-conversation pending message ceiling (OOM protection: a single fast user must
#: not be able to inflate it). When full the oldest pending message is dropped; in
#: practice this limit is never reached.
_PER_CONV_QUEUE_MAX = 50
#: Ceiling on the number of concurrent DIFFERENT-conversation workers. Workers delete
#: themselves when their queue drains; this ceiling only activates during a pathological
#: burst (many different chats arriving simultaneously) → the intake maxsize backpressure
#: is preserved (the semaphore limits only concurrent LLM calls, not the worker COUNT).
#: In a personal allowlisted setup this is never hit; it only closes the "unbounded
#: workers → OOM guard bypassed" hole.
_DEFAULT_MAX_WORKERS = 64


@asynccontextmanager
async def _noop_turn_guard(conversation_id: str | None):
    """Default turn gate (no-op): no busy-guard in F1/test setups.

    Yields ``None`` (no register-turn callback) → the router runs the LLM call inline,
    behaviour identical to before the per-turn child-task isolation."""
    yield


async def _default_complete(
    settings: Settings,
    text: str,
    *,
    history: list[dict[str, str]] | None = None,
    conversation_id: str | None = None,
    system_prompt: str | None = None,
) -> str:
    """Default LLM path — provider-aware, with history (cursor/claude)."""
    from akana_server.orchestrator import llm_dispatch

    result = await llm_dispatch.complete_chat(
        settings,
        text,
        history=history,
        chat_mode=True,
        conversation_id=conversation_id,
        reuse_agent=False,
        system_prompt=system_prompt,
    )
    return (getattr(result, "text", "") or "").strip()


@dataclass(slots=True)
class _TurnOutcome:
    """Result of a single inbound turn: text to return to the channel + persistence decision."""

    text: str
    conversation_id: str | None = None
    persist: bool = False


@dataclass(slots=True)
class _ConvWorker:
    """FIFO processor for a single conversation: its own queue + running task.

    Different conversations = different workers = parallel; messages for the
    same conversation pass through this single worker in order (history
    consistency + ordering preserved)."""

    queue: asyncio.Queue[InboundMessage]
    task: asyncio.Task[None] | None = None


class InboundRouter:
    """Sole consumer of the shared inbound queue.

    ``complete`` and ``skill_planner`` are injectable (test swap); defaults are
    :func:`_default_complete` and ``skills.turn_injection.plan_skill_turn``
    respectively. When ``conversations``/``memory`` are provided (by the app
    lifespan) each chat_id is bound to a persistent conversation; otherwise
    stateless single-shot mode (F1 behaviour). FULL AUTONOMY: the old policy
    ``evaluate`` gate has been removed — inbound messages are not blocked.
    """

    def __init__(
        self,
        settings: Settings,
        registry: ConnectorRegistry,
        *,
        complete: Callable[..., Any] | None = None,
        conversations: ConversationService | None = None,
        skill_planner: Callable[..., Any] | None = None,
        bindings: ChannelBindingStore | None = None,
        max_turns: int = _DEFAULT_MAX_TURNS,
        max_concurrency: int = _DEFAULT_MAX_CONCURRENCY,
        max_workers: int = _DEFAULT_MAX_WORKERS,
        turn_guard: Callable[[str | None], Any] | None = None,
    ) -> None:
        self._settings = settings
        self._registry = registry
        self._complete = complete or _default_complete
        # Per-conversation turn gate (async CM factory). The connector turn awaits
        # this to prevent a concurrent second LLM turn in the SAME conversation
        # while a web/voice turn is running (daemon session_key=conv_id serialisation
        # → spurious LLM_TIMEOUT + history-read race). The app lifespan injects it
        # (bridge to the chat busy-registry); otherwise no-op (stateless F1/test).
        self._turn_guard = turn_guard or _noop_turn_guard
        self._conversations = conversations
        self._skill_planner = skill_planner
        self._max_turns = max(1, int(max_turns))
        data_dir = getattr(settings, "data_dir", None)
        if bindings is not None:
            self._bindings = bindings
        elif conversations is not None and data_dir is not None:
            self._bindings = ChannelBindingStore(Path(data_dir))
        else:
            self._bindings = None
        self._task: asyncio.Task[None] | None = None
        # Per-conversation workers for intra-conversation ordering + inter-conversation parallelism.
        self._max_concurrency = max(1, int(max_concurrency))
        self._max_workers = max(1, int(max_workers))
        self._workers: dict[str, _ConvWorker] = {}
        self._sem: asyncio.Semaphore | None = None  # created in _run (inside the running loop)
        # "Worker freed" signal (waited by new-chat dispatch when at the cap). Created in loop.
        self._worker_freed: asyncio.Event | None = None

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="connector-inbound-router")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # Intake stopped → no new workers will be born; bring down remaining workers.
        await self._shutdown_workers()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run(self) -> None:
        """Sole INTAKE of the shared queue — DISPATCHES each message to its conversation's
        worker without waiting.

        The old design processed every message inline with ``await self.handle``,
        making ALL conversations/channels serial: the next message waited in the queue
        until an LLM turn finished ("no parallel messages", "second message stalls").
        Intake normally only dispatches (non-blocking): DIFFERENT conversations are
        processed in parallel, the SAME conversation passes through its single worker
        in FIFO order. The ONE exception: when the worker ceiling (``_max_workers``) is
        full and a NEW conversation arrives, intake applies backpressure until a worker
        frees up (unbounded workers → intake maxsize OOM guard bypassed)."""
        self._sem = asyncio.Semaphore(self._max_concurrency)
        self._worker_freed = asyncio.Event()
        while True:
            msg = await self._registry.inbound.get()
            try:
                await self._dispatch(msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # dispatch error does not kill the intake loop
                capture_failure(e, where="connectors.InboundRouter._run")

    # -- concurrent dispatch (intra-conversation FIFO, inter-conversation parallel) ----------

    def _reap_dead_workers(self) -> None:
        """Drop workers whose task has died (cancelled/crashed).

        A dead worker left registered ZOMBIES its conversation — messages queue into a loop
        nobody drains (every future message silently lost) — and keeps consuming the worker
        ceiling, which eventually deadlocks dispatch for ALL connectors. Atomic sweep (no
        await); ``_worker_freed`` is signalled so any cap-waiting dispatch wakes."""
        dead = [k for k, w in self._workers.items() if w.task is not None and w.task.done()]
        for k in dead:
            self._workers.pop(k, None)
        if dead and self._worker_freed is not None:
            self._worker_freed.set()

    async def _dispatch(self, msg: InboundMessage) -> None:
        """Place the message in its conversation's FIFO worker (spawning it if absent).

        Does not block for existing conversations. ONLY waits when a new conversation
        arrives while the worker ceiling is full (backpressure → OOM guard preserved)."""
        self._reap_dead_workers()
        key = self._dispatch_key(msg)
        worker = self._workers.get(key)
        if worker is None:
            # Worker ceiling: if there is no room for a new conversation, wait until
            # a worker retires. Workers delete themselves when their queue drains and
            # set _worker_freed → we can proceed.
            while (
                len(self._workers) >= self._max_workers
                and key not in self._workers
                and self._worker_freed is not None
            ):
                self._worker_freed.clear()
                await self._worker_freed.wait()
            worker = self._workers.get(key)  # may have been spawned while we waited
        if worker is None:
            worker = _ConvWorker(queue=asyncio.Queue(maxsize=_PER_CONV_QUEUE_MAX))
            self._workers[key] = worker
            worker.task = asyncio.create_task(
                self._worker_loop(key, worker), name=f"conv-worker:{key}"
            )
        try:
            worker.queue.put_nowait(msg)
        except asyncio.QueueFull:
            # Single-conversation flood (OOM protection): drop the oldest pending message
            # and accept the new one.
            try:
                worker.queue.get_nowait()
                worker.queue.task_done()
            except asyncio.QueueEmpty:  # pragma: no cover - race edge
                pass
            log.warning(
                "connector %s chat=%s: conversation queue full (cap=%d), oldest message dropped",
                msg.connector_id,
                msg.chat_id,
                _PER_CONV_QUEUE_MAX,
            )
            worker.queue.put_nowait(msg)

    @staticmethod
    def _dispatch_key(msg: InboundMessage) -> str:
        """Ordering key = channel + chat. Same conversation → same worker → FIFO."""
        return f"{msg.connector_id}\x1f{msg.chat_id}"

    async def _worker_loop(self, key: str, worker: _ConvWorker) -> None:
        """Process a single conversation's messages in FIFO order; retire when the queue drains.

        The retirement RACE is intentionally closed: once ``get_nowait`` raises
        QueueEmpty, there is NO ``await`` until the worker is removed from
        ``self._workers`` → the step is atomic in the event loop. So the dispatcher
        either finds the still-registered worker and adds a message, or spawns a new
        worker in place of the deleted one; neither message loss nor duplicate workers
        can occur."""
        sem = self._sem
        assert sem is not None  # _run sets up sem before _dispatch  # noqa: S101
        while True:
            try:
                msg = worker.queue.get_nowait()
            except asyncio.QueueEmpty:
                if self._workers.get(key) is worker:
                    del self._workers[key]
                    # Signal any cap-waiting dispatch that a slot has opened (E3).
                    if self._worker_freed is not None:
                        self._worker_freed.set()
                return
            try:
                async with sem:  # inter-conversation concurrency ceiling (provider protection)
                    await self.handle(msg)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # single-message error does not take down the worker or other conversations
                capture_failure(e, where="connectors.InboundRouter._worker_loop")
            finally:
                worker.queue.task_done()

    async def _shutdown_workers(self) -> None:
        """Cancel all remaining conversation workers and await them (idempotent)."""
        workers = list(self._workers.values())
        self._workers.clear()
        for w in workers:
            if w.task is not None and not w.task.done():
                w.task.cancel()
        for w in workers:
            if w.task is None:
                continue
            try:
                await w.task
            except asyncio.CancelledError:
                pass
            except Exception as e:
                capture_failure(e, where="connectors.InboundRouter._shutdown_workers")

    # -- single message flow ------------------------------------------------------

    async def handle(self, msg: InboundMessage) -> str:
        """Process a single inbound message end-to-end; returns the final text sent to the channel."""
        begin_turn(None, mode="connector", reuse=False)  # each message = fresh trace_id
        started = time.monotonic()
        outcome = await self._build_outcome(msg)
        filtered = filter_outbound(outcome.text)
        if filtered.redacted:
            self._audit_egress(msg, filtered.matched)
        if outcome.persist and outcome.conversation_id:
            # The text VISIBLE on the channel (including egress masking) is written to
            # the archive — the web UI record matches exactly what was sent to the channel.
            await self._persist_turn_pair(outcome.conversation_id, msg.text, filtered.text)
        await self._send_chunks(msg, filtered.text)
        # Latency observation: queue-to-reply time — the first log to check for slowness.
        log.info(
            "connector %s: inbound turn %.2fs (chat=%s, %d char reply)",
            msg.connector_id,
            time.monotonic() - started,
            msg.chat_id,
            len(filtered.text),
        )
        return filtered.text

    async def _send_chunks(self, msg: InboundMessage, text: str) -> None:
        connector = self._registry.get(msg.connector_id)
        limit = int(getattr(connector, "max_message_len", 0) or 0)
        chunks = split_text(text, limit) if limit else [text]
        for chunk in chunks:
            try:
                await self._registry.send(
                    OutboundMessage(
                        connector_id=msg.connector_id, chat_id=msg.chat_id, text=chunk
                    )
                )
            except Exception as e:
                capture_failure(
                    e, where="connectors.InboundRouter._send_chunks"
                )
                return  # if the first chunk failed, don't force the rest

    async def _build_outcome(self, msg: InboundMessage) -> _TurnOutcome:
        # FULL AUTONOMY: inbound risk/approval policy gate removed — every message
        # flows directly into the command/conversation path.

        # Telegram-local explicit slash commands (connector-only surface).
        command = parse_command(msg.text)
        if command == "yeni":
            return _TurnOutcome(text=await self._cmd_new_conversation(msg))
        if command == "durum":
            return _TurnOutcome(text=self._cmd_status(msg))
        if command == "baglan":
            return _TurnOutcome(text=await self._cmd_bind(msg))

        conversation_id = await self._conversation_for(msg)

        # Skill injection (FULL AUTONOMY — no approval gate): a strong-match skill's
        # SKILL.md body is prepended to the prompt before the LLM call.
        plan = await self._plan_skills(msg.text)
        user_for_llm = msg.text
        if plan is not None and getattr(plan, "prompt_block", ""):
            user_for_llm = f"{plan.prompt_block}\n\n{user_for_llm}"

        # Turn gate: serialise history-read + LLM call per conversation → the connector
        # turn WAITS while a web/voice turn is running in the SAME conversation (no
        # concurrent 2nd LLM + no history race). Without a gate (no-op) old behaviour
        # is preserved exactly.
        async with self._turn_guard(conversation_id) as register_turn:
            history = self._history_for(conversation_id)
            system_prompt = self._system_prompt_for(msg, conversation_id)
            turn_task: "asyncio.Task[str] | None" = None
            try:
                if callable(register_turn):
                    # Run the LLM call as a per-TURN child task and register IT as the cancel
                    # handle (see _make_turn_guard): an external STOP/reset then cancels only
                    # this turn — NOT the long-lived conversation worker (cancelling the worker
                    # zombied the chat: all future messages silently dropped + ceiling deadlock).
                    turn_task = asyncio.create_task(
                        self._invoke_complete(
                            user_for_llm,
                            history=history,
                            conversation_id=conversation_id,
                            system_prompt=system_prompt,
                        )
                    )
                    register_turn(turn_task)
                    text = await turn_task
                else:
                    # No-op guard (F1/tests) → run inline, behaviour unchanged.
                    text = await self._invoke_complete(
                        user_for_llm,
                        history=history,
                        conversation_id=conversation_id,
                        system_prompt=system_prompt,
                    )
            except asyncio.CancelledError:
                ct = asyncio.current_task()
                worker_cancelled = turn_task is None or (ct is not None and ct.cancelling() > 0)
                if worker_cancelled:
                    # The WORKER itself is being cancelled (shutdown / no child to isolate) →
                    # cancel the orphan child and propagate so the worker actually stops.
                    if turn_task is not None and not turn_task.done():
                        turn_task.cancel()
                    raise
                # Only the per-turn child was cancelled (external STOP) → swallow so the worker
                # survives and processes the next queued message.
                log.info("connector turn cancelled by STOP (conv=%s)", conversation_id)
                return _TurnOutcome(text=LLM_ERROR_REPLY)
            except Exception as e:
                capture_failure(e, where="connectors.InboundRouter._build_outcome.complete")
                return _TurnOutcome(text=LLM_ERROR_REPLY)
        reply = (text or "").strip() or EMPTY_REPLY
        return _TurnOutcome(text=reply, conversation_id=conversation_id, persist=True)

    # -- LLM call (backward-compatible signature) ---------------------------------------

    async def _invoke_complete(
        self,
        text: str,
        *,
        history: list[dict[str, str]],
        conversation_id: str | None,
        system_prompt: str | None = None,
    ) -> str:
        """``complete`` call — also supports the legacy ``(settings, text)`` signature."""
        fn = self._complete
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):  # builtin/Mock edge case
            params = {}
        has_var_kw = any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
        kwargs: dict[str, Any] = {}
        if history and (has_var_kw or "history" in params):
            kwargs["history"] = history
        if conversation_id and (has_var_kw or "conversation_id" in params):
            kwargs["conversation_id"] = conversation_id
        if system_prompt and (has_var_kw or "system_prompt" in params):
            kwargs["system_prompt"] = system_prompt
        return await fn(self._settings, text, **kwargs)

    # -- persona (per channel) -------------------------------------------------

    def _system_prompt_for(
        self, msg: InboundMessage, conversation_id: str | None
    ) -> str | None:
        """Channel persona + installed skill catalog (WI-2).

        Persona: ``persona.resolve(channel=...)`` (conversation > channel). When
        the default ``akana`` persona resolves, the base is ``None`` (the LLM
        client inserts ``CHAT_SYSTEM_PREFIX`` itself). When the catalog is
        non-empty, base (or ``CHAT_SYSTEM_PREFIX``) + catalog are returned
        explicitly — same inventory source as the web-side ContextAssembler, so
        «Can you do X?» from Telegram is answered correctly too.
        Persona/catalog are enhancements — ANY failure falls back to base (None/plain).
        """
        data_dir = getattr(self._settings, "data_dir", None)
        if data_dir is None:
            return None
        from akana_server.persona.builtin import CHAT_SYSTEM_PREFIX, DEFAULT_PERSONA_ID
        from akana_server.skills.catalog import resolve_catalog

        base: str | None = None  # None → the client inserts CHAT_SYSTEM_PREFIX itself
        try:
            from akana_server.persona.registry import get_persona_registry

            persona = get_persona_registry(Path(data_dir)).resolve(
                channel=msg.connector_id, conversation_id=conversation_id
            )
            if not (
                persona.id == DEFAULT_PERSONA_ID
                and persona.system_prompt == CHAT_SYSTEM_PREFIX
            ):
                base = persona.system_prompt
        except Exception as e:
            capture_failure(
                e,
                where="connectors.InboundRouter._system_prompt_for",
                level=logging.WARNING,
            )
            base = None
        catalog = resolve_catalog(self._settings)  # empty/disabled/error → ""
        if catalog:
            return f"{base if base is not None else CHAT_SYSTEM_PREFIX}\n\n{catalog}"
        return base

    # -- conversation persistence -----------------------------------------------------

    async def _conversation_for(self, msg: InboundMessage) -> str | None:
        """The conversation bound to this chat_id; creates a new one if none exists."""
        if self._conversations is None or self._bindings is None:
            return None
        try:
            cid = await asyncio.to_thread(
                self._bindings.get, msg.connector_id, msg.chat_id
            )
            if cid and await asyncio.to_thread(self._conversations.get, cid) is not None:
                return cid
            return await self._create_conversation(msg)
        except Exception as e:  # persistence failure keeps the turn stateless
            capture_failure(e, where="connectors.InboundRouter._conversation_for")
            return None

    async def _create_conversation(self, msg: InboundMessage) -> str:
        """Create + bind a conversation. The sqlite writes (create, merge_json_metadata,
        bind — up to three locked write transactions with busy_timeout up to 10s) run OFF
        the event loop, mirroring ``_persist_turn_pair`` (b26): on the loop they froze the
        whole server whenever a web turn held the memory.db write lock.
        """
        assert self._conversations is not None and self._bindings is not None  # noqa: S101

        def _write() -> str:
            assert self._conversations is not None and self._bindings is not None  # noqa: S101
            meta = self._conversations.create(
                title=channel_title(msg.connector_id, msg.sender_name, msg.chat_id)
            )
            self._conversations.merge_json_metadata(
                meta.id,
                {
                    "channel": msg.connector_id,
                    "channel_chat_id": msg.chat_id,
                    "channel_peer": msg.sender_name or None,
                },
            )
            self._bindings.bind(msg.connector_id, msg.chat_id, meta.id)
            return meta.id

        return await asyncio.to_thread(_write)

    def _history_for(self, conversation_id: str | None) -> list[dict[str, str]]:
        """Last N turns (LLM setting) within the character budget — the current turn has NOT been written yet."""
        if conversation_id is None or self._conversations is None:
            return []
        try:
            msgs = self._conversations.recent_llm_messages(
                conversation_id, max_turns=self._resolve_max_turns()
            )
            return trim_history(msgs, max_chars=resolve_history_budget())
        except Exception as e:
            capture_failure(e, where="connectors.InboundRouter._history_for")
            return []

    def _resolve_max_turns(self) -> int:
        try:
            from akana_server.llm_settings import load_llm_settings

            llm = load_llm_settings(Path(self._settings.data_dir), self._settings)
            return max(1, int(llm.chat_max_turns))
        except Exception:
            return self._max_turns

    async def _persist_turn_pair(
        self, conversation_id: str, user_text: str, assistant_text: str
    ) -> None:
        """Write the turn pair through the SAME writer as chat.py (turn_writer).

        b26: the sqlite writes run OFF the event loop (asyncio.to_thread). On the loop they
        blocked the whole server under DB-lock contention (busy_timeout up to 10s + blocking
        backoff) whenever a web turn held the memory.db write lock — "the page froze while
        streaming". Every other persist path already offloads; the connector did not.
        """
        if self._conversations is None:
            return
        try:
            from akana_server.orchestrator.turn_writer import (
                persist_assistant_turn,
                persist_user_turn,
            )

            data_dir = getattr(self._settings, "data_dir", None)
            dd = Path(data_dir) if data_dir is not None else None
            uid = await asyncio.to_thread(
                persist_user_turn,
                conversation_id=conversation_id,
                user_text=user_text,
                data_dir=dd,
            )
            await asyncio.to_thread(
                persist_assistant_turn,
                conversation_id=conversation_id,
                assistant_text=assistant_text,
                user_turn_id=uid,
                data_dir=dd,
            )
        except Exception as e:  # archive error does not break the reply flow
            capture_failure(e, where="connectors.InboundRouter._persist_turn_pair")

    # -- skill injection -------------------------------------------------------

    async def _plan_skills(self, text: str) -> Any | None:
        """WI-1 pure helpers — error / absence keeps the turn skill-free."""
        try:
            if self._skill_planner is not None:
                return await self._skill_planner(self._settings, text)
            from akana_server.skills.turn_injection import plan_skill_turn

            return await plan_skill_turn(self._settings, text)
        except Exception as e:
            capture_failure(e, where="connectors.InboundRouter._plan_skills")
            return None

    # -- commands -----------------------------------------------------------------

    async def _cmd_new_conversation(self, msg: InboundMessage) -> str:
        if self._conversations is None or self._bindings is None:
            return "Conversation archive is off in this setup; every message is already independent."
        try:
            await asyncio.to_thread(self._bindings.clear, msg.connector_id, msg.chat_id)
            await self._create_conversation(msg)
        except Exception as e:
            capture_failure(e, where="connectors.InboundRouter._cmd_new_conversation")
            return LLM_ERROR_REPLY
        return NEW_CONVERSATION_REPLY

    def _cmd_status(self, msg: InboundMessage) -> str:
        lines = ["Akana status:"]  # user-facing string
        try:
            from akana_server.llm_settings import load_llm_settings, resolve_provider

            llm = load_llm_settings(Path(self._settings.data_dir), self._settings)
            lines.append(f"• LLM provider: {resolve_provider(self._settings, llm)}")  # user-facing
        except Exception as e:
            capture_failure(
                e,
                where="connectors.InboundRouter._cmd_status.provider",
                level=logging.WARNING,
            )
        try:
            if self._conversations is not None and self._bindings is not None:
                cid = self._bindings.get(msg.connector_id, msg.chat_id)
                meta = self._conversations.get(cid) if cid else None
                if meta is not None:
                    lines.append(f"• Conversation: «{meta.title}» ({meta.message_count} messages)")  # user-facing
        except Exception as e:
            capture_failure(
                e,
                where="connectors.InboundRouter._cmd_status.conversation",
                level=logging.WARNING,
            )
        try:
            running = [s["id"] for s in self._registry.status() if s.get("running")]
            if running:
                lines.append("• Active channels: " + ", ".join(running))
        except Exception as e:
            capture_failure(
                e,
                where="connectors.InboundRouter._cmd_status.channels",
                level=logging.WARNING,
            )
        return "\n".join(lines)

    async def _cmd_bind(self, msg: InboundMessage) -> str:
        """``/baglan`` — bind this chat to the most recently updated web conversation.

        "Continue on Telegram": the user was chatting in the web UI and wants this
        Telegram chat to pick up the SAME conversation (instead of its own channel
        thread). Ordering mirrors ``ConversationService.list_conversations`` (most
        recently updated first, archived/deleted excluded) — the same source
        ``_cmd_status`` reads for the bound conversation.
        """
        if self._conversations is None or self._bindings is None:
            return "Conversation archive is off in this setup; there is nothing to connect to."
        try:
            convs = await asyncio.to_thread(self._conversations.list_conversations, limit=1)
        except Exception as e:
            capture_failure(e, where="connectors.InboundRouter._cmd_bind.list")
            return LLM_ERROR_REPLY
        if not convs:
            return "No conversation found yet to connect to. Start one in the web UI first."
        target = convs[0]
        try:
            await asyncio.to_thread(
                self._bindings.bind, msg.connector_id, msg.chat_id, target.id
            )
        except Exception as e:
            capture_failure(e, where="connectors.InboundRouter._cmd_bind.bind")
            return LLM_ERROR_REPLY
        return f"Connected to conversation «{target.title}». Messages here now continue it."

    # -- audit ---------------------------------------------------------------------

    def _audit_egress(self, msg: InboundMessage, matched: tuple[str, ...]) -> None:
        data_dir = getattr(self._settings, "data_dir", None)
        if data_dir is None:
            return
        audit.write_event(
            data_dir,
            "connector_egress_filtered",
            data={
                "connector": msg.connector_id,
                "chat_id": msg.chat_id,
                "patterns": list(matched),
            },
        )
