"""Shared native-function-calling tool-loop engine for the text providers.

gemini / openai / ollama each drive an identical native function-calling loop — a
round budget, suppress-intermediate-text buffering, per-round thinking replay with a
``completed`` marker, two-phase ``tool_call`` start/end wire events, usage accumulation
across rounds, and bridge-vs-native dispatch routing — and within each module the
``stream`` and ``complete`` surfaces are near-copies of each other. That is ~6 copies of
the same control flow (providers:arch:1). The per-round work genuinely differs (gemini
carries thought-signature parts and reads google-genai chunks, openai parses SSE
tool-call frames, ollama parses NDJSON), but the LOOP around it does not.

This module is that loop, parameterized by a small provider :class:`ToolLoopAdapter`.
The engine owns:

* the round budget and the ``for round in range(max_rounds): ... else: ...`` skeleton,
* buffering the round's answer deltas (suppressed on tool rounds so intermediate
  reasoning never bleeds into the final answer) and emitting them only on the final
  round,
* replaying the round's thinking deltas + a ``{"thinking": {"phase": "completed"}}``
  marker,
* accumulating the ``tool_call`` start/end wire records into the terminal usage,
* the UNIFIED round-limit-exhaustion behavior (see below).

The adapter owns everything provider-specific: how to run one round, how the pending
tool-call state is represented, how to dispatch + append it, and how to run the final
tool-less round.

**Round-limit exhaustion is unified (providers:smell:3).** Previously the three
diverged: gemini silently issued one final tool-less request; openai/ollama emitted a
visible ``_[tool-round limit reached; response truncated]_`` marker (and ollama did not
make a final request at all). All three now use gemini's forced-final-completion: when
the model calls a tool on every round, the engine runs ONE final tool-less round so the
model is forced to answer with real text instead of a truncation notice or an empty
bubble. The visible-notice constants are gone.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class RoundResult:
    """Normalized outcome of one streamed model round.

    ``answer_deltas`` / ``thinking_deltas`` are the buffered wire events for this round
    (``{"delta": str, "done": False}`` and ``{"thinking": {"phase": "delta", "text":
    str}}`` respectively). ``pending`` is the provider's OPAQUE tool-call state — the
    engine only tests its truthiness (a tool round vs. the final text round) and hands
    it back to :meth:`ToolLoopAdapter.dispatch_and_append`. ``has_thinking`` records
    whether any thinking delta was produced this round so the engine can close the
    thinking block with a ``completed`` marker."""

    answer_deltas: list[dict[str, Any]] = field(default_factory=list)
    thinking_deltas: list[dict[str, Any]] = field(default_factory=list)
    pending: Any = None
    has_thinking: bool = False


class ToolLoopAdapter(Protocol):
    """Provider-specific hooks the tool-loop engine drives.

    The engine calls these; each provider implements the google-genai / OpenAI-SSE /
    Ollama-NDJSON specifics behind them. All tool-call accumulation, usage
    accumulation, and message/contents mutation live inside the adapter — the engine
    never touches provider state directly."""

    async def run_round(self) -> RoundResult:
        """Stream one model round to completion; return its buffered :class:`RoundResult`.

        Must accumulate this round's usage into the adapter's running total and collect
        the round's tool calls into ``RoundResult.pending`` (falsy → final text round)."""
        ...

    async def dispatch_and_append(self, pending: Any) -> list[dict[str, Any]]:
        """Dispatch the round's tool calls, append the call+results to the message state,
        and return the two-phase ``tool_call`` start/end wire events (in order)."""
        ...

    async def run_final_round(self) -> AsyncIterator[dict[str, Any]]:
        """Run ONE final TOOL-LESS round (round-limit reached) yielding its wire events
        (``delta`` / ``thinking``), forcing the model to answer with text.

        Must accumulate the final round's usage into the running total too."""
        ...
        yield {}  # pragma: no cover - Protocol stub


def tool_call_events(
    calls: list[tuple[str, str, dict[str, Any]]], results: list[str | None]
) -> list[dict[str, Any]]:
    """Two-phase ``tool_call`` start/end wire events for a tool round (the shared shape).

    Two events are emitted per tool (frontend/persist + audit contract): ``phase="start"``
    (args populated) and ``phase="end"`` (result/status populated), both carrying the
    same ``id`` → ``persist._accumulate_tool_call`` updates the start card in place (no
    duplicate). ``calls`` is a list of pre-extracted ``(id, name, args)`` tuples (each
    provider owns its own name/args extraction and its synthetic-id prefix); ``results``
    lines up with ``calls`` (the dispatch pairs are built in the same order).

    Previously gemini/ollama/openai each carried a byte-identical copy of this builder;
    only the per-item extraction and id prefix genuinely differed, so those stay in the
    providers and the wire-shape construction lives here."""
    events: list[dict[str, Any]] = []
    for idx, (cid, name, args) in enumerate(calls):
        result = results[idx] if idx < len(results) else None
        events.append(
            {
                "tool_call": {
                    "id": cid,
                    "name": name,
                    "phase": "start",
                    "args": args,
                    "result": None,
                    "status": None,
                }
            }
        )
        events.append(
            {
                "tool_call": {
                    "id": cid,
                    "name": name,
                    "phase": "end",
                    "args": None,
                    "result": result,
                    "status": "ok",
                }
            }
        )
    return events


async def run_stream_loop(
    adapter: ToolLoopAdapter,
    *,
    max_rounds: int,
    on_tool_call: Callable[[dict[str, Any]], None],
) -> AsyncIterator[dict[str, Any]]:
    """Drive the streaming tool loop, yielding Akana wire events (delta/thinking/tool_call).

    Does NOT yield the terminal ``{"done": ...}`` event — the caller owns that (the
    usage shape differs per provider). ``on_tool_call`` is invoked with each emitted
    ``tool_call`` record so the caller can accumulate them into the terminal usage."""
    for _round in range(max_rounds):
        result = await adapter.run_round()
        # Replay this round's thinking (every round, so intermediate-round reasoning is
        # visible), then close the block with a completed marker.
        for ev in result.thinking_deltas:
            yield ev
        if result.has_thinking:
            yield {"thinking": {"phase": "completed"}}
        if not result.pending:
            # No tool → final round: emit the buffered answer deltas and finish.
            for ev in result.answer_deltas:
                yield ev
            return
        # Tool round: dispatch + append to state, emit start/end tool_call events, and
        # DROP this round's answer deltas (intermediate reasoning must not leak).
        for ev in await adapter.dispatch_and_append(result.pending):
            on_tool_call(ev["tool_call"])
            yield ev
    else:
        # Round limit reached and the model is STILL calling tools. UNIFIED behavior
        # (providers:smell:3): force ONE final tool-less round so the model answers with
        # real text (no truncation notice, no empty bubble).
        async for ev in adapter.run_final_round():
            yield ev


async def run_complete_loop(
    adapter: "CompleteLoopAdapter",
    *,
    max_rounds: int,
    on_tool_call: Callable[[dict[str, Any]], None],
) -> str:
    """Drive the one-shot (non-stream) tool loop; return the final answer text.

    Symmetric with :func:`run_stream_loop` but for the blocking surface: each round
    returns ``(text, pending)``; a tool round dispatches + appends and continues, the
    final text round returns the answer. On round-limit exhaustion it runs ONE final
    tool-less round (unified with the streaming path). ``on_tool_call`` accumulates each
    ``tool_call`` wire record into the terminal usage."""
    for _round in range(max_rounds):
        text, pending = await adapter.complete_round()
        if not pending:
            return text  # no tool → final answer
        for ev in await adapter.dispatch_and_append(pending):
            on_tool_call(ev["tool_call"])
    else:
        # Round limit reached with no final text: one tool-less completion forces a real
        # answer (unified with streaming; no empty bubble).
        return await adapter.complete_final_round()


class CompleteLoopAdapter(Protocol):
    """Provider hooks for the one-shot (non-stream) tool loop."""

    async def complete_round(self) -> tuple[str, Any]:
        """Run one one-shot round; return ``(answer_text, pending_tool_calls)``.

        ``pending`` falsy → this is the final text round and ``answer_text`` is the
        answer. Must accumulate the round's usage into the running total."""
        ...

    async def dispatch_and_append(self, pending: Any) -> list[dict[str, Any]]:
        """Dispatch the round's tool calls, append call+results to the message state,
        and return the ``tool_call`` start/end wire records (for usage accumulation)."""
        ...

    async def complete_final_round(self) -> str:
        """Run ONE final tool-less completion (round-limit reached); return its text.

        Must accumulate the final round's usage into the running total."""
        ...


__all__ = [
    "RoundResult",
    "ToolLoopAdapter",
    "CompleteLoopAdapter",
    "run_stream_loop",
    "run_complete_loop",
    "tool_call_events",
]
