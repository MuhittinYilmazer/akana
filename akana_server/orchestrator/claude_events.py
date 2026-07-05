"""NDJSON → Akana wire-event translation for the ``claude`` CLI stream.

:mod:`.claude_provider` owns the *subprocess lifecycle* (spawn, stdin/stdout
plumbing, pid registry, breaker bookkeeping, temp-file spill, timeout kills). The
*event-translation* concern — turning each ``claude`` ``stream-json`` line into
Akana wire events and accumulating the terminal-``done`` payload — lives here as a
single stateful object, :class:`ClaudeEventTranslator`.

Before this split the translation loop was ~500 lines of inline ``while``/``yield``
inside :func:`claude_provider._stream_single_run` with ~40 mutable locals threaded
through it (see the ``providers:arch:4`` audit finding). Moving the state onto one
object with an explicit ``feed`` step makes the parser inspectable in isolation and
keeps the generator focused on process management. **The wire-event contract is
byte-identical to the previous inline loop** — this is a mechanical move, not a
rewrite.

The translator NEVER spawns or kills the process itself: when an event requires
early termination (AskUserQuestion / ExitPlanMode), it records the intent and lets
the caller do the actual :func:`terminate_process_group` (so the process lifecycle
stays entirely in :mod:`.claude_provider`). ``feed`` is an async generator; on an
early-termination event it also asks the caller — via the yielded
``{"_terminate": pid}`` control record — to kill the group before the next line.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

from akana_server.orchestrator import base
from akana_server.orchestrator.claude_protocol import (
    _AskBlockStripper,
    _extract_ask_block,
    _extract_assistant_text,
    _normalize_ask_user,
    _normalize_plan,
    _normalize_todo,
    _strip_ask_block,
)

#: Tool names that carry special turn semantics — mirrored from
#: :mod:`.claude_provider` so both modules agree on the vocabulary. Kept here (not
#: imported from claude_provider) to avoid an import cycle: claude_provider imports
#: this module at load time.
_ASK_USER_TOOL = "AskUserQuestion"
_EXIT_PLAN_TOOL = "ExitPlanMode"
_TODO_TOOL = "TodoWrite"
_TASK_TOOL = "Task"


class ClaudeEventTranslator:
    """Turn ``claude`` stream-json events into Akana wire events, holding all the
    per-turn accumulator state that the terminal ``done`` event is built from.

    Usage (in :func:`claude_provider._stream_single_run`)::

        tr = ClaudeEventTranslator(active_model)
        async for out in tr.feed(ev):
            if "_terminate" in out:
                await terminate_process_group(out["_terminate"])
                continue
            yield out

    After the loop, the caller reads the public attributes (``usage``, ``cost_usd``,
    ``tool_calls``, ``final_status``, ``result_error``, ``result_seen``,
    ``asked_user``/``planned`` and their payloads, ``early_terminated``, …) plus
    :meth:`final_text` / :meth:`flush_tail` to assemble the terminal event.
    """

    def __init__(self, active_model: str) -> None:
        #: Live cost estimate keyed on this resolved model tag.
        self._active_model = active_model

        # --- answer/text accumulation ---------------------------------------- #
        self.tool_calls: list[dict[str, Any]] = []
        self._tool_names: dict[str, str] = {}
        self._delta_text: list[str] = []
        self._fallback_text: list[str] = []
        self._thinking_blocks: set[int] = set()
        self.saw_delta = False
        # `assistant` text block and terminal `result.result` carry the SAME
        # answer; track whether assistant text already fed fallback_text so we
        # don't double it.
        self._saw_assistant_text = False

        # --- segment-gap welding --------------------------------------------- #
        # Claude streams answer segments split by a thinking block / tool call with
        # NO whitespace at the seam ("…buluyorum.Pack…"). Track the last emitted
        # char + a pending interruption → weld a break before the next.
        self._seg_last_char = ""
        self._seg_gap_pending = False

        # --- terminal result ------------------------------------------------- #
        self.usage: dict[str, Any] | None = None
        #: claude ``result.total_cost_usd`` (a sibling of usage) → tokens.cost_usd.
        self.cost_usd: float | None = None
        self.final_status = "finished"
        self.result_seen = False
        self.result_error: dict[str, Any] | None = None

        # --- AskUserQuestion / ExitPlanMode capture -------------------------- #
        self._ask_user_ids: set[str] = set()
        self.ask_user_payload: dict[str, Any] | None = None
        self.asked_user = False
        # Text-protocol ask_user: holds back the [[AKANA_ASK]]…[[/AKANA_ASK]] block
        # from the live text deltas (the block is the structured card, not chat
        # prose).
        self._ask_stripper = _AskBlockStripper()
        self._plan_ids: set[str] = set()
        self.plan_payload: dict[str, Any] | None = None
        self.planned = False
        #: AFTER AskUserQuestion / ExitPlanMode we ask the caller to kill the
        #: process and stop the loop → no need to kill again in ``finally``.
        self.early_terminated = False

        # --- live token counter ---------------------------------------------- #
        self._live_prompt: int = 0
        self._live_completion: int = 0
        self._live_cache_read: int = 0
        self._live_cache_write: int = 0

        # --- B2 tool-input streaming ----------------------------------------- #
        # Accumulate input_json_delta chunks by the tool block index (for the
        # tool_call_delta event). Since the full input already arrives in the
        # assistant event, this is a BACKUP — NEVER break the text stream.
        self._tool_input_parts: dict[int, list[str]] = {}  # index → chunks
        # Map the streaming content-block index → the tool's id/name (captured at
        # content_block_start). The input_json_delta is keyed by INDEX, but the
        # tool_call start/end events are keyed by ID; carrying the id on the delta
        # lets the UI stream the input INTO the same card.
        self._tool_block_ids: dict[int, dict[str, str | None]] = {}  # index → {id, name}

    # ------------------------------------------------------------------ #
    # Terminal-event assembly (read after the feed loop finishes)
    # ------------------------------------------------------------------ #
    def flush_tail(self) -> None:
        """Flush any ask-block-stripper tail into the delta stream state.

        Real prose the stripper held back as a partial-marker tail that never
        became a block (e.g. prose ending in "[["). When a block WAS captured,
        flush returns "" (that tail is the card, already surfaced). Call after the
        stream ends and before :meth:`final_text`.
        """
        tail = self._ask_stripper.flush()
        if tail and not self.asked_user and not self.planned:
            self.saw_delta = True
            self._delta_text.append(tail)

    def final_text(self) -> str:
        """The user-visible answer text for the terminal ``done`` event.

        A malformed [[AKANA_ASK]] block (bad JSON → not promoted to a card) leaves
        the stripper permanently blocked even though the turn kept running: every
        delta after the marker was swallowed live, so ``_delta_text`` is truncated
        at the marker. ``_fallback_text`` (built from the complete assistant
        message) has the full pre+post-block prose in that case — prefer it.
        """
        if (
            self.saw_delta
            and self._ask_stripper.captured_block
            and not self.asked_user
            and not self.planned
        ):
            return "".join(t for t in self._fallback_text if t)
        if self.saw_delta:
            return "".join(self._delta_text)
        return "".join(t for t in self._fallback_text if t)

    # ------------------------------------------------------------------ #
    # Live cost helper
    # ------------------------------------------------------------------ #
    def _live_block(self) -> dict[str, Any]:
        cost = base.estimate_cost_usd(
            self._active_model,
            self._live_prompt,
            self._live_completion,
            cache_read=self._live_cache_read,
            cache_write=self._live_cache_write,
        )
        block: dict[str, Any] = {
            "prompt": self._live_prompt,
            "completion": self._live_completion,
        }
        if cost > 0:
            block["cost_usd"] = cost
        return block

    # ------------------------------------------------------------------ #
    # Per-line translation
    # ------------------------------------------------------------------ #
    async def feed(
        self, ev: dict[str, Any], proc_pid: int
    ) -> AsyncIterator[dict[str, Any]]:
        """Translate ONE parsed stream-json object into zero-or-more wire events.

        Yields ordinary Akana wire events (``agent_id`` / ``delta`` / ``thinking``
        / ``tool_call`` / ``usage_live`` / …). To request early process
        termination (AskUserQuestion / ExitPlanMode) it yields the control record
        ``{"_terminate": <pid>}`` — the caller kills the group and must then stop
        the loop (check :attr:`early_terminated` / the ``result``-break semantics
        via the ``_stop`` control record).

        A ``{"_stop": True}`` control record means "break the outer read loop"
        (early-termination outer break, or a terminal ``result`` event).
        """
        etype = ev.get("type")

        if etype == "system" and ev.get("subtype") == "init":
            sid = ev.get("session_id")
            if sid:
                yield {"agent_id": str(sid)}
            return

        if etype == "stream_event":
            async for out in self._feed_stream_event(ev):
                yield out
            return

        if etype == "assistant":
            async for out in self._feed_assistant(ev, proc_pid):
                yield out
            return

        if etype == "user":
            async for out in self._feed_user(ev):
                yield out
            return

        if etype == "result":
            for out in self._feed_result(ev):
                yield out
            return

    async def _feed_stream_event(
        self, ev: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        inner = ev.get("event")
        if not isinstance(inner, dict):
            return
        itype = inner.get("type")
        if itype == "message_start":
            # message_start arrives at the VERY START of the stream.
            # usage.input_tokens is the fresh, cache-EXCLUSIVE input token count;
            # cache_read/cache_write are SEPARATE fields. The live "prompt" header
            # is read from the SAME base (input_tokens) as
            # _usage_to_tokens.prompt_tokens in the done event — so the number does
            # not JUMP when done arrives.
            msg = inner.get("message") or {}
            mu = msg.get("usage") or {}
            self._live_prompt = base.coerce_token_count(mu.get("input_tokens"))
            self._live_cache_read = base.coerce_token_count(
                mu.get("cache_read_input_tokens")
            )
            self._live_cache_write = base.coerce_token_count(
                mu.get("cache_creation_input_tokens")
            )
            yield {"usage_live": self._live_block()}
        elif itype == "message_delta":
            # The output token count is updated CUMULATIVELY; each message_delta
            # replaces the previous (it grows until the end of the stream).
            mu2 = inner.get("usage") or {}
            self._live_completion = base.coerce_token_count(mu2.get("output_tokens"))
            yield {"usage_live": self._live_block()}
        elif itype == "content_block_start":
            block = inner.get("content_block")
            idx = inner.get("index")
            if (
                isinstance(block, dict)
                and block.get("type") == "thinking"
                and isinstance(idx, int)
            ):
                self._thinking_blocks.add(idx)
            # Remember this tool block's id/name so the input deltas (keyed by
            # index) can carry the tool id → the UI streams the input into the SAME
            # card the tool_call start/end events target.
            elif (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and isinstance(idx, int)
            ):
                self._tool_block_ids[idx] = {
                    "id": block.get("id"),
                    "name": block.get("name"),
                }
        elif itype == "content_block_delta":
            async for out in self._feed_content_block_delta(inner):
                yield out
        elif itype == "content_block_stop":
            idx = inner.get("index")
            if idx in self._thinking_blocks:
                self._thinking_blocks.discard(idx)
                yield {"thinking": {"phase": "completed"}}

    async def _feed_content_block_delta(
        self, inner: dict[str, Any]
    ) -> AsyncIterator[dict[str, Any]]:
        delta = inner.get("delta")
        if not isinstance(delta, dict):
            return
        dtype = delta.get("type")
        if dtype == "text_delta":
            text = delta.get("text")
            # Deltas arriving after asked_user/planned are only the CLI's
            # "question/plan rejected" apology → swallowed (not leaked to the live
            # stream, not added to the answer).
            if (
                isinstance(text, str)
                and text
                and not self.asked_user
                and not self.planned
            ):
                # Hold back any [[AKANA_ASK]] question block — it renders as the
                # structured card, not chat text (only the pre-block prose is
                # emitted).
                safe = self._ask_stripper.feed(text)
                if safe:
                    # Weld a break if a thinking/tool interruption split this
                    # segment from the previous one.
                    if self._seg_gap_pending:
                        safe = base.segment_gap(self._seg_last_char, safe) + safe
                        self._seg_gap_pending = False
                    self.saw_delta = True
                    self._delta_text.append(safe)
                    self._seg_last_char = safe[-1]
                    yield {"delta": safe, "done": False}
        elif dtype == "thinking_delta":
            # Extended-thinking text does NOT mix into the answer: a separate
            # `thinking` wire event (Cursor shape) — not added to
            # delta_text/fallback_text.
            think = delta.get("thinking")
            if isinstance(think, str) and think:
                # Thinking splits the answer → the next text segment needs a break
                # (don't glue onto this one).
                self._seg_gap_pending = True
                yield {"thinking": {"phase": "delta", "text": think}}
        elif dtype == "input_json_delta":
            # B2 (optional): emit the tool input JSON chunk by chunk. Any error
            # only skips this event, it NEVER breaks the main stream.
            try:
                t_idx = inner.get("index")
                partial = delta.get("partial_json")
                if isinstance(t_idx, int) and isinstance(partial, str):
                    if t_idx not in self._tool_input_parts:
                        self._tool_input_parts[t_idx] = []
                    self._tool_input_parts[t_idx].append(partial)
                    _meta = self._tool_block_ids.get(t_idx) or {}
                    # AskUserQuestion / ExitPlanMode become a structured
                    # ask_user/plan event and end the turn early — they must NOT
                    # render a generic tool card, so their streamed input is never
                    # surfaced as a tool_call_delta (otherwise a half-built card is
                    # orphaned when the turn terminates before the tool_call
                    # start/end).
                    if _meta.get("name") not in (_ASK_USER_TOOL, _EXIT_PLAN_TOOL):
                        yield {
                            "tool_call_delta": {
                                "index": t_idx,
                                "id": _meta.get("id"),
                                "name": _meta.get("name"),
                                "partial": partial,
                            }
                        }
            except Exception:  # pragma: no cover - defensive
                pass  # TODO: tool input streaming: unexpected error skipped

    async def _feed_assistant(
        self, ev: dict[str, Any], proc_pid: int
    ) -> AsyncIterator[dict[str, Any]]:
        message = ev.get("message")
        # Subagent nesting: a Task subagent's own events carry the parent Task's id
        # here (None at the top level). Threaded onto tool_call so the UI can group.
        parent_tid = ev.get("parent_tool_use_id")
        if not isinstance(message, dict):
            return
        stop_outer = False  # early-termination signal (mirrors the old `_stop_outer`)
        msg_text = _extract_assistant_text(message)
        # Text-protocol ask_user: the model emits its multiple-choice question as an
        # [[AKANA_ASK]]{json}[[/AKANA_ASK]] block (the native AskUserQuestion tool
        # is unavailable headless and the model tends to refuse it). Parse it into
        # the SAME structured ask_user event + early terminate as the native path —
        # the block itself was held back from the live deltas, so only a preamble
        # (if any) remains as the visible answer.
        #
        # NOTE: like the original inline loop, this does NOT stop the tool_use scan
        # below — a generic tool_use block in the SAME assistant message is still
        # surfaced. The stop signal is deferred to the end of this method.
        ask_inner = (
            None if (self.asked_user or self.planned) else _extract_ask_block(msg_text)
        )
        if ask_inner is not None:
            payload = _normalize_ask_user(uuid.uuid4().hex[:12], ask_inner)
            if payload is not None:
                self.asked_user = True
                self.ask_user_payload = payload
                yield {"ask_user": payload}
                if not self.early_terminated:
                    self.early_terminated = True
                    yield {"_terminate": proc_pid}
                stop_outer = True
        # The assistant body arriving after asked_user/planned (apology/summary, or
        # the held-back ask block) is NOT added to the answer; a preamble before it
        # accumulates normally. Any stray ask block (e.g. malformed JSON not
        # promoted to a card) is stripped so it never surfaces as the answer.
        if not self.asked_user and not self.planned:
            _stripped = _strip_ask_block(msg_text)
            self._fallback_text.append(_stripped)
            if _stripped:
                self._saw_assistant_text = True
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            tid = block.get("id")
            name = block.get("name") or ""
            # AskUserQuestion → a structured ask_user event; NO generic tool card,
            # and the auto-reject is suppressed (the tid is tracked). Kill the
            # process IMMEDIATELY and break the loop → the CLI stops without asking
            # a second question or creating a plan; only a SINGLE question is shown.
            if name == _ASK_USER_TOOL:
                payload = _normalize_ask_user(tid, block.get("input"))
                if payload is not None:
                    if tid:
                        self._ask_user_ids.add(str(tid))
                    self.asked_user = True
                    self.ask_user_payload = payload
                    yield {"ask_user": payload}
                    if not self.early_terminated:
                        self.early_terminated = True
                        yield {"_terminate": proc_pid}
                    stop_outer = True
                    break  # exit the inner for-loop
            # ExitPlanMode → a structured plan event; NO generic tool card, the
            # auto-reject ("Exit plan mode?") is suppressed.
            if name == _EXIT_PLAN_TOOL:
                plan_p = _normalize_plan(tid, block.get("input"))
                if plan_p is not None:
                    if tid:
                        self._plan_ids.add(str(tid))
                    self.planned = True
                    self.plan_payload = plan_p
                    yield {"plan": plan_p}
                    if not self.early_terminated:
                        self.early_terminated = True
                        yield {"_terminate": proc_pid}
                    stop_outer = True
                    break  # exit the inner for-loop
            if tid:
                self._tool_names[str(tid)] = str(name)
            call = {
                "id": tid,
                "name": name,
                "phase": "start",
                "args": block.get("input"),
                "result": None,
                "status": None,
                "parent_id": parent_tid,
            }
            self.tool_calls.append(call)
            yield {"tool_call": call}
            # A tool call splits the answer → weld a break before the next text
            # segment (else post-tool narration glues on).
            self._seg_gap_pending = True
            # TodoWrite → ALSO a typed todo-progress event (the checklist card still
            # renders via the generic tool_call above; this drives the turn-level
            # progress). NOT a turn boundary — the turn continues.
            if name == _TODO_TOOL:
                todo_p = _normalize_todo(block.get("input"))
                if todo_p is not None:
                    yield {"todo": todo_p}
            # Task → subagent START boundary (the generic tool_call is the group
            # anchor; the subagent's nested steps carry parent_id=tid).
            elif name == _TASK_TOOL:
                tin = (
                    block.get("input")
                    if isinstance(block.get("input"), dict)
                    else {}
                )
                yield {
                    "subagent": {
                        "id": str(tid or ""),
                        "name": str(
                            tin.get("subagent_type")
                            or tin.get("description")
                            or "Task"
                        )[:80],
                        "description": str(tin.get("description") or "")[:200],
                        "phase": "start",
                    }
                }
        # Deferred stop: break the outer read loop AFTER the whole content scan
        # (mirrors the old `if _stop_outer: break` — a done event is then produced
        # normally). Set by the text-protocol ask block or a native ask/plan tool.
        if stop_outer:
            yield {"_stop": True}

    async def _feed_user(self, ev: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        message = ev.get("message")
        parent_tid = ev.get("parent_tool_use_id")
        if not isinstance(message, dict):
            return
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_result":
                continue
            tid = block.get("tool_use_id")
            # The AskUserQuestion ("Answer questions?") and ExitPlanMode ("Exit plan
            # mode?") auto-rejects (is_error) are NOT SHOWN to the user as a red
            # card.
            if str(tid) in self._ask_user_ids or str(tid) in self._plan_ids:
                continue
            is_error = bool(block.get("is_error"))
            call = {
                "id": tid,
                "name": self._tool_names.get(str(tid), ""),
                "phase": "end",
                "args": None,
                "result": block.get("content"),
                "status": "error" if is_error else "ok",
                "parent_id": parent_tid,
            }
            for existing in self.tool_calls:
                if existing.get("id") == tid:
                    existing.update(
                        {"result": call["result"], "status": call["status"]}
                    )
                    break
            else:
                self.tool_calls.append(call)
            yield {"tool_call": call}
            # Task finished → subagent END boundary (flip the group state).
            if self._tool_names.get(str(tid)) == _TASK_TOOL:
                yield {
                    "subagent": {
                        "id": str(tid or ""),
                        "phase": "end",
                        "status": call["status"],
                    }
                }

    def _feed_result(self, ev: dict[str, Any]) -> list[dict[str, Any]]:
        self.result_seen = True
        if isinstance(ev.get("usage"), dict):
            self.usage = ev.get("usage")
        if "total_cost_usd" in ev:
            self.cost_usd = base.coerce_cost_usd(ev.get("total_cost_usd")) or self.cost_usd
        if ev.get("is_error") or ev.get("subtype") == "error":
            self.result_error = ev
        else:
            self.final_status = "finished"
            # Skip result text when asked_user/planned (it's the apology/summary) or
            # when assistant text already supplied it (avoid doubling).
            if (
                isinstance(ev.get("result"), str)
                and not self.asked_user
                and not self.planned
                and not self._saw_assistant_text
            ):
                self._fallback_text.append(ev["result"])
        return [{"_stop": True}]
