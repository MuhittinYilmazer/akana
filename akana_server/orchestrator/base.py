"""Provider-agnostic plumbing shared by the LLM clients.

``llm_dispatch`` (Cursor SDK via Node bridge) and ``claude_provider`` (local
``claude`` CLI) historically each carried their own byte-identical copies of a
few low-level mechanics: the stdout line limit, the usage→int token coercer,
the NDJSON line reader (with its Python 3.11 chunked fallback) and the
idle-timeout cap helper. ``bridge_pool`` imported some of them from
``llm_dispatch`` too.

This module is the single home for those primitives so a third client could
reuse them without copy-paste. The clients keep thin re-exports under their
historical names (``CLAUDE_STDOUT_LINE_LIMIT``,
``_coerce_token_count``, ``_read_bridge_line`` / ``_read_line``, ``_combine_cap``)
so every existing import keeps working unchanged. Behaviour is identical — these
are moves, not rewrites.

It also owns the timeout resolvers (:func:`bridge_timeout`/:func:`idle_timeout`/
:func:`total_timeout`) and the shared :class:`CursorStreamDecoder` — the single
implementation of the Cursor NDJSON event contract consumed by both the daemon
(:mod:`bridge_pool`) and the direct-spawn (:mod:`cursor_provider`) transports.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from akana_server.config import Settings


# --------------------------------------------------------------------------- #
# Provider convention (formal seam)
# --------------------------------------------------------------------------- #
# Every ``*_provider`` module (claude / gemini / openai / ollama / cursor) exposes
# the SAME two module-level coroutines. Historically this was a convention held
# together by copy-paste and docstrings; :class:`ChatProvider` makes it a checkable
# structural type. ``llm_dispatch`` resolves the active provider to one of these
# modules and delegates to it — the registry table there is typed as
# ``dict[str, ChatProvider]``.
#
# SIGNATURE CONVENTION (accepted-and-documented, not per-provider-pruned): both
# coroutines take the full provider-neutral keyword set. A provider that cannot act
# on a given kwarg (e.g. gemini has no ``agent_id`` reuse, ollama has no ``file_ids``
# vision input) still ACCEPTS it and documents the no-op inline, so the dispatch hub
# can forward one uniform kwarg set to every provider without branch-specific
# subsets. ``plan_mode``/``auto_continue`` are claude-only and live only on the
# streaming signature; other providers accept-and-ignore them.


@runtime_checkable
class ChatProvider(Protocol):
    """Structural type every ``*_provider`` module satisfies.

    A module (not a class) is the provider: ``claude_provider``,
    ``gemini_provider``, ``openai_provider``, ``ollama_provider`` and
    ``cursor_provider`` all expose these two coroutines at module scope. The
    Protocol documents the shared convention and lets the dispatch registry be
    typed; ``isinstance(module, ChatProvider)`` also gives a cheap runtime check
    that a module honours the seam.
    """

    async def stream_user_chat(
        self,
        settings: Settings,
        user_text: str,
        *,
        history: list[dict[str, str]] | None = ...,
        model: str | None = ...,
        conversation_id: str | None = ...,
        agent_id: str | None = ...,
        reuse_agent: bool = ...,
        mcp_servers: dict[str, Any] | None = ...,
        system_prompt: str | None = ...,
        thinking_mode: str | None = ...,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        """Async-generator of wire events; ends with exactly one terminal ``done``.

        The terminal event is the canonical shape built by :func:`stream_done_event`.
        """
        ...

    async def complete_chat(
        self,
        settings: Settings,
        user_text: str,
        *,
        history: list[dict[str, str]] | None = ...,
        model: str | None = ...,
        chat_mode: bool = ...,
        conversation_id: str | None = ...,
        agent_id: str | None = ...,
        reuse_agent: bool = ...,
        mcp_servers: dict[str, Any] | None = ...,
        system_prompt: str | None = ...,
        thinking_mode: str | None = ...,
        **kwargs: Any,
    ) -> tuple[str, str, dict[str, Any]]:
        """One-shot chat: ``(text, status, raw)``."""
        ...


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    """The provider traits the upper layers branch on, declared BY the provider.

    Historically these were name-lists hardcoded in the layers that consume them
    (e.g. ``chat_context`` open-coded ``if provider in ("ollama", "gemini")`` to
    decide statelessness). That leaked a provider property into the context layer
    and meant every new provider needed shotgun edits. Each ``*_provider`` module
    now declares its own ``CAPABILITIES = ProviderCapabilities(...)`` and the
    consumers query :func:`llm_dispatch.provider_capabilities` instead.

    ``stateless``: the provider has NO server-side session/agent to resume — every
    call is fresh, so the conversation history must ALWAYS be flattened into the
    prompt (ollama/gemini). ``False`` (claude/cursor) means a stored agent/session
    id CAN be resumed, so history need not be re-sent when one is present. The
    default for an unknown/unconfigured provider is ``False`` — matching the old
    behaviour where a name not in the stateless list fell through to the
    resume/agent-id path.
    """

    stateless: bool = False


#: Bridge/CLI NDJSON lines can include large tool payloads; the default asyncio
#: ``StreamReader`` readline cap is 64 KiB, far too small. Both providers raise
#: it to 8 MiB. Re-exported as ``CLAUDE_STDOUT_LINE_LIMIT`` (claude).
STDOUT_LINE_LIMIT = 8 * 1024 * 1024


def combine_cap(base: float, cap: float) -> float:
    """Tighten ``base`` with ``cap`` (never loosen).

    Hang-protection knobs (idle/total) can only lower the ceiling:
    if ``cap <= 0`` (disabled) or invalid, ``base`` is returned unchanged;
    otherwise the smaller of the two. This preserves the semantics of the
    long ``bridge_timeout`` the user configured, only adding an earlier
    hang ceiling on top.
    """
    try:
        cap_f = float(cap)
    except (TypeError, ValueError):  # pragma: no cover - schema already yields float
        return base
    if cap_f <= 0:
        return base
    if base <= 0:
        return cap_f
    return min(base, cap_f)


def coerce_token_count(value: Any) -> int:
    """Safely coerce the bridge/SDK usage field to int — NEVER break the hot path.

    The bridge stdout is external JSON: the token count may arrive as a
    float-string ("12.5"), a nonsense string ("x"), a list, or None. On those,
    ``int(...)`` would raise ``ValueError``/``TypeError`` and swallow the
    ``done`` event, crashing the whole turn. Anything that does not convert to a
    number/numeric-string is treated as 0.
    """
    if value is None or isinstance(value, bool):
        return int(value) if isinstance(value, bool) else 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except (ValueError, TypeError):
            return 0
    return 0


def accumulate_usage(
    totals: dict[str, int], usage: dict[str, Any] | None
) -> dict[str, int]:
    """Add one round's ``{prompt_tokens, completion_tokens}`` frame to the running TOTAL.

    In the function-calling loop every API call is billed SEPARATELY (including
    intermediate tool rounds); storing only the latest round's usage (the old
    ``usage = chunk.usage``) would drop intermediate prompt/completion tokens.
    Here both counts are ACCUMULATED across all rounds — the sum is correct. A
    missing/None frame is a no-op; :func:`coerce_token_count` safely coerces to
    int (a malformed field does not crash the accumulation).

    Dict-shaped providers (ollama/openai) call this directly. Gemini's SDK hands
    back a ``usage_metadata`` object with different attribute names, so it keeps
    its own attr-reading accumulator.
    """
    if not usage:
        return totals
    totals["prompt_tokens"] += coerce_token_count(usage.get("prompt_tokens"))
    totals["completion_tokens"] += coerce_token_count(usage.get("completion_tokens"))
    return totals


def coerce_cost_usd(value: Any) -> float:
    """Safely coerce Claude's ``result.total_cost_usd`` field to float.

    Same strictness as ``coerce_token_count``: the cost is external JSON (it may
    arrive as a float, a "0.0123" string, None, or a nonsense string). Anything
    negative/NaN/inf or that does not convert to a number is treated as 0.0 —
    NEVER crash the ``done`` event.
    """
    if value is None or isinstance(value, bool):
        return 0.0
    if isinstance(value, (int, float)):
        out = float(value)
    elif isinstance(value, str):
        try:
            out = float(value.strip())
        except (ValueError, TypeError):
            return 0.0
    else:
        return 0.0
    if out != out or out in (float("inf"), float("-inf")) or out < 0:
        return 0.0
    return out


# --------------------------------------------------------------------------- #
# Live cost estimation (pricing table)
# --------------------------------------------------------------------------- #
#: Per-model pricing table (per 1M tokens, $).
#: This is a LIVE ESTIMATE — once the exact ``total_cost_usd`` from the done event
#: (computed by Claude) arrives, the frontend replaces this estimate. The table
#: uses lowercase-name matching: if a keyword appears in the model tag, that price
#: applies.
_PRICING: list[tuple[str, float, float]] = [
    # (model_keyword, input_$/MTok, output_$/MTok)
    ("opus",   15.0, 75.0),
    ("haiku",   0.80,  4.0),
    ("sonnet",  3.0,  15.0),  # default / claude-sonnet-*
]
_PRICING_DEFAULT = (3.0, 15.0)  # sonnet price for unknown models


def estimate_cost_usd(
    model: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    cache_read: int = 0,
    cache_write: int = 0,
) -> float:
    """Compute a LIVE cost estimate from the model and token counts.

    Cache reads are priced at a 90% discount and cache writes at a 25% premium
    (Anthropic standard pricing). The computation safely handles malformed/
    negative tokens (treated as zero) and unknown models (sonnet default). The
    result is replaced by the exact ``total_cost_usd`` from the ``done`` event —
    this is only an indicator during streaming.
    """
    tag = (model or "").lower()
    in_price, out_price = _PRICING_DEFAULT
    for keyword, inp, outp in _PRICING:
        if keyword in tag:
            in_price, out_price = inp, outp
            break

    pt = max(0, int(prompt_tokens or 0))
    ct = max(0, int(completion_tokens or 0))
    cr = max(0, int(cache_read or 0))
    cw = max(0, int(cache_write or 0))

    # Tokens read from cache are priced at 10% of normal input.
    # Tokens written to cache are priced at 125% of normal input.
    # BUG 4: prompt_tokens is already cache-EXCLUSIVE (in Anthropic/our mapping,
    # cache_read and cache_write are separate fields, priced separately below) →
    # subtracting cr/cw again was double-counting. Normal input is directly the
    # prompt tokens.
    normal_in = max(0, pt)
    cost = (
        (normal_in * in_price + cr * in_price * 0.1 + cw * in_price * 1.25 + ct * out_price)
        / 1_000_000
    )
    return round(cost, 8)


def driver_error_to_llm_error(
    exc: Any, provider: str, *, pass_status: bool = True
) -> Any:
    """``DriverError`` → ``LLMCallError`` — the shared ollama/openai translation.

    Status taxonomy: an explicit ``exc.status_code`` wins; otherwise
    ``timeout→504, unavailable→503, other→502`` (keyed off ``exc.kind``). The resulting
    status is always the ``LLMCallError.status_code``.

    ``pass_status`` controls ONE deliberate behavioural difference between the two
    providers (providers:smell — the copies had silently drifted): openai forwards the
    synthesized ``status`` into :func:`friendly_provider_error` so an ambiguous-text
    timeout/auth error is still classified via the structural hint, while ollama does NOT
    (``pass_status=False``) — it relies purely on text scanning, because its
    ``DriverError.kind`` for a transport failure is already surfaced in the message text
    and forwarding a synthesized 502 could mislabel a plain error. Callers pick the flag
    explicitly rather than one copy quietly diverging from the other.

    gemini keeps its own Exception-shaped variant (it duck-types the optional google-genai
    ``APIError`` rather than a ``DriverError``) — not routed through here.
    """
    from akana_server.orchestrator.errors import LLMCallError, friendly_provider_error

    status = exc.status_code or (
        504 if exc.kind == "timeout" else 503 if exc.kind == "unavailable" else 502
    )
    message = friendly_provider_error(
        exc.message, provider=provider, status=status if pass_status else None
    )
    return LLMCallError(message, status_code=status)


#: Paragraph break welded between two answer segments that would otherwise collide.
SEGMENT_GAP = "\n\n"


def segment_gap(last_char: str, next_text: str) -> str:
    """Separator to weld a resumed answer segment onto the previous one.

    A provider that streams its answer in separate segments — split by a thinking
    block, a tool call, or a resumed run — puts NO whitespace at the seam, so the
    tail of one sentence and the head of the next collide ("...buluyorum.Pack
    bulundu."). Returns :data:`SEGMENT_GAP` when real text sits on both sides of the
    seam and neither side already supplies the whitespace; otherwise "".

    ``last_char`` is the last character already emitted ("" when nothing has been
    emitted yet → no leading gap); ``next_text`` is the segment about to be emitted
    (a leading space/newline on it also means the seam is already separated).
    """
    if not last_char or last_char.isspace():
        return ""
    if not next_text or next_text[:1].isspace():
        return ""
    return SEGMENT_GAP


def prepend_stream_buffer(reader: asyncio.StreamReader, data: bytes) -> None:
    """Put unconsumed bytes back for the next read (Py3.11 readuntil has no limit= kwarg)."""
    if not data:
        return
    buf = reader._buffer  # noqa: SLF001
    if isinstance(buf, bytearray):
        reader._buffer = bytearray(data) + buf
    else:
        reader._buffer = bytearray(data + bytes(buf))


async def read_ndjson_line(reader: asyncio.StreamReader, timeout: float | None) -> bytes:
    """Read one NDJSON line; tolerate large tool payloads (default asyncio cap is 64 KiB).

    asyncio's ``StreamReader.readuntil()`` takes NO ``limit=`` keyword on ANY Python
    version (the limit is a StreamReader *constructor* arg) — passing one raises
    ``TypeError``. So on every version we read in chunks up to :data:`STDOUT_LINE_LIMIT`,
    slice the first line at ``\\n`` and prepend the remainder back onto the reader's
    buffer for the next call. Over the
    limit (no newline) raises :class:`asyncio.LimitOverrunError` rather than
    silently truncating, so the chat layer surfaces a clean error instead of
    feeding half a JSON object downstream.

    ``timeout`` of ``None`` waits indefinitely (the bridge daemon reader sits
    idle between turns and enforces per-turn timeouts elsewhere). A non-positive
    ``timeout`` (``0``/negative) means the same thing: ``combine_cap`` yields 0
    to signal "no idle ceiling" (disabled, e.g. ``CURSOR_BRIDGE_TIMEOUT=0``), and
    passing 0 straight to ``wait_for`` would time out INSTANTLY → every stream
    would die on the first read (bug: false 504 on every cursor turn). Mirror the
    guard in ``claude_provider._read_line``.
    """

    async def _read() -> bytes:
        parts: list[bytes] = []
        total = 0
        while total < STDOUT_LINE_LIMIT:
            chunk = await reader.read(min(256 * 1024, STDOUT_LINE_LIMIT - total))
            if not chunk:
                return b"".join(parts)
            parts.append(chunk)
            total += len(chunk)
            data = b"".join(parts)
            nl = data.find(b"\n")
            if nl >= 0:
                line = data[: nl + 1]
                prepend_stream_buffer(reader, data[nl + 1 :])
                return line
        raise asyncio.LimitOverrunError(
            "bridge stdout line exceeds limit",
            data[: min(len(data), 65536)],
        )

    if timeout and timeout > 0:
        return await asyncio.wait_for(_read(), timeout=timeout)
    return await _read()


# --------------------------------------------------------------------------- #
# Timeout resolvers (runtime > env(Settings) > default)
# --------------------------------------------------------------------------- #
# These read the runtime-settings store and combine it with the configured
# ``bridge_timeout`` via :func:`combine_cap`. They historically lived in
# ``llm_dispatch`` (as ``_idle_timeout`` / ``_total_timeout``) and were
# duplicated verbatim in ``bridge_pool``; base.py is the neutral home. The
# imports are function-local so this module stays free of a top-level
# runtime_settings/config dependency (mirrors the original helpers).


def bridge_timeout(settings: Settings) -> float:
    """Configured bridge timeout: runtime > env(Settings) > default."""
    from akana_server.runtime_settings import get_runtime

    return float(get_runtime("bridge_timeout", settings))


def idle_timeout(settings: Settings) -> float:
    """Idle-hang ceiling for streaming: ``min(bridge_timeout, llm_idle_timeout)``.

    Every line read (delta/tool/done) resets this counter — only time elapsed
    with NO new chunk arriving is counted; a slow but progressing stream is
    never cut off. A non-positive knob disables the ceiling (see
    :func:`combine_cap`).
    """
    from akana_server.runtime_settings import get_runtime

    return combine_cap(
        bridge_timeout(settings), float(get_runtime("llm_idle_timeout", settings))
    )


def total_timeout(settings: Settings) -> float:
    """Wall-clock ceiling for blocking (non-streaming) calls.

    ``min(bridge_timeout, llm_total_timeout)`` — if the one-shot ``complete_chat``
    hangs it exits cleanly within this duration instead of 30 minutes.
    """
    from akana_server.runtime_settings import get_runtime

    return combine_cap(
        bridge_timeout(settings), float(get_runtime("llm_total_timeout", settings))
    )


# --------------------------------------------------------------------------- #
# Shared Cursor NDJSON stream decoder
# --------------------------------------------------------------------------- #
# The Cursor bridge speaks one NDJSON event contract (delta / tool start-end
# merge / live-usage estimate / done / error / need_history / heartbeat /
# thinking / activity / timing). It is consumed by TWO transports:
#   * the DEFAULT persistent daemon (``bridge_pool.BridgePool._stream_run_once``)
#   * the direct-spawn path (``cursor_provider.stream_user_chat`` when
#     ``AKANA_BRIDGE_DAEMON=0``)
# Previously each transport hand-maintained its own copy of the decode loop and
# they drifted (agent_id placement in the done event, break-on-done vs
# read-to-EOF). This class is the single decode implementation both consume; the
# transports keep only their own I/O (subprocess stdout vs id-multiplexed queue).
#
# CANONICAL SEMANTICS (unified to the default daemon path):
#   * agent_id is embedded INSIDE the terminal done event (not yielded separately
#     and omitted from done).
#   * the caller BREAKS on the terminal event (done/error/need_history) — it does
#     not read to EOF.


class CursorStreamDecoder:
    """Accumulates one Cursor NDJSON run and turns each event into wire events.

    Usage::

        dec = CursorStreamDecoder(model=active_model_tag)
        for ev in ndjson_events:
            for out in dec.feed(ev):
                yield out
            if dec.terminal:            # done / error / need_history
                break
        # then, depending on dec.terminal:
        #   "error"        -> raise via dec.bridge_error
        #   "need_history" -> already yielded {"need_history_bootstrap": True}
        #   "done"/None    -> yield dec.done_event()

    ``feed`` mutates the internal ``tool_calls`` accumulator and terminal state,
    and returns the list of wire-event dicts to yield for this input event.
    """

    def __init__(self, *, model: str | None) -> None:
        self._model = model
        self.tool_calls: list[dict[str, Any]] = []
        self.usage: dict[str, Any] | None = None
        self.final_text: str | None = None
        self.final_status: str | None = None
        self.agent_id: str | None = None
        self.bridge_error: dict[str, Any] | None = None
        #: One of ``None`` (still streaming), ``"done"``, ``"error"`` or
        #: ``"need_history"``. When non-None the caller must stop the read loop.
        self.terminal: str | None = None

    def _usage_live_events(self, ev: dict[str, Any]) -> Iterator[dict[str, Any]]:
        live = ev.get("usage")
        if not isinstance(live, dict):
            return
        live_prompt = coerce_token_count(
            live.get("inputTokens") or live.get("input_tokens")
        )
        live_completion = coerce_token_count(
            live.get("outputTokens") or live.get("output_tokens")
        )
        live_cache_read = coerce_token_count(
            live.get("cacheReadTokens") or live.get("cache_read_tokens")
        )
        live_cache_write = coerce_token_count(
            live.get("cacheWriteTokens") or live.get("cache_write_tokens")
        )
        live_cost = estimate_cost_usd(
            self._model,
            live_prompt,
            live_completion,
            cache_read=live_cache_read,
            cache_write=live_cache_write,
        )
        live_block: dict[str, Any] = {
            "prompt": live_prompt,
            "completion": live_completion,
        }
        if live_cost > 0:
            live_block["cost_usd"] = live_cost
        yield {"usage_live": live_block}

    def feed(self, ev: dict[str, Any]) -> list[dict[str, Any]]:
        """Consume one parsed NDJSON event; return the wire events to yield."""
        kind = ev.get("ev")
        out: list[dict[str, Any]] = []
        if kind == "delta":
            text = str(ev.get("text") or "")
            if text:
                out.append({"delta": text, "done": False})
        elif kind == "tool":
            phase = ev.get("phase")
            call = {
                "id": ev.get("call_id"),
                "name": ev.get("name"),
                "phase": phase,
                "args": ev.get("args"),
                "result": ev.get("result"),
                "status": ev.get("status"),
                "parent_id": ev.get("parent_id"),
            }
            if phase == "start":
                # Dedup by call id: the Cursor SDK streams ``partial-tool-call``
                # updates (growing args) plus ``tool-call-started`` for one call —
                # all map to phase "start" with the SAME call_id. Appending each
                # blindly left the aggregated (voice/blocking) done.tool_calls with
                # N-1 duplicate rows frozen at "start" (inflated tool count,
                # duplicate cards on reload, phantom entries for name-based checks).
                # Merge into the existing id (non-None fields only) like the end
                # phase already does; append only when the id is new.
                for existing in self.tool_calls:
                    if existing.get("id") == call["id"]:
                        for key, value in call.items():
                            if value is not None:
                                existing[key] = value
                        break
                else:
                    self.tool_calls.append(call)
                out.append({"tool_call": call})
            elif phase == "end":
                # The end phase MUST UPDATE the existing entry — otherwise the
                # terminal done event carries tool cards frozen at "start"
                # (result/status empty on persist/reload). Match by id → merge.
                # Merge EVERY non-None field, not just result/status: the cursor
                # bridge emits MCP tool `start` events without name/args (only the
                # `end` event reliably carries them), so a result/status-only merge
                # would persist nameless cards and defeat _turn_wrote_memory's
                # name-based dedup guard (post-turn capture double-writes the fact).
                for existing in self.tool_calls:
                    if existing.get("id") == call["id"]:
                        for key, value in call.items():
                            if value is not None:
                                existing[key] = value
                        break
                else:
                    self.tool_calls.append(call)
                out.append({"tool_call": call})
        elif kind == "usage":
            # CUR-1 live usage: the bridge emits ``{ev:"usage"}`` lines during
            # generation; convert to Claude's ``usage_live`` shape (cost estimated
            # with the active model tag; included only when > 0).
            out.extend(self._usage_live_events(ev))
        elif kind == "meta":
            if ev.get("agent_id"):
                self.agent_id = str(ev.get("agent_id"))
                out.append({"agent_id": self.agent_id})
        elif kind == "timing":
            out.append(
                {
                    "timing": {
                        "phase": ev.get("phase"),
                        "ms": ev.get("ms"),
                        "reused": ev.get("reused"),
                    }
                }
            )
        elif kind == "heartbeat":
            out.append(
                {
                    "activity": {
                        "kind": "heartbeat",
                        "phase": str(ev.get("phase") or "run_wait"),
                    }
                }
            )
        elif kind == "thinking":
            out.append(
                {
                    "thinking": {
                        "phase": str(ev.get("phase") or "delta"),
                        "text": str(ev.get("text") or ""),
                    }
                }
            )
        elif kind == "activity":
            out.append(
                {
                    "activity": {
                        "kind": str(ev.get("kind") or "status"),
                        "phase": ev.get("phase"),
                        "text": str(ev.get("text") or ""),
                    }
                }
            )
        elif kind == "done":
            self.final_text = str(ev.get("text") or "")
            self.final_status = str(ev.get("status") or "finished")
            if ev.get("agent_id"):
                self.agent_id = str(ev.get("agent_id"))
            if isinstance(ev.get("usage"), dict):
                self.usage = ev.get("usage")
            # A ``done`` whose status is ``error``/``cancelled`` is a FAILED run,
            # not a success — the Cursor SDK's ``run.wait()`` resolves (does not
            # reject) on a server/SDK-side failure. The bridge now emits ``error``
            # directly for these, but stay defensive so a stale daemon can't slip a
            # failure through as an empty/truncated success (which would also make
            # the breaker record success). Route it to the error terminal so the
            # real cause surfaces and the breaker records failure.
            if self.final_status in ("error", "cancelled"):
                cause = (
                    str(ev.get("error") or "").strip()
                    or (self.final_text or "").strip()
                    or f"Cursor run {self.final_status}"
                )
                self.bridge_error = {
                    "error": cause,
                    "status": self.final_status,
                }
                if ev.get("error_code"):
                    self.bridge_error["error_code"] = str(ev.get("error_code"))
                self.terminal = "error"
            else:
                self.terminal = "done"
        elif kind == "error":
            self.bridge_error = ev
            self.terminal = "error"
        elif kind == "need_history":
            self.terminal = "need_history"
            out.append({"need_history_bootstrap": True})
        return out

    def done_event(self, tokens: dict[str, Any]) -> dict[str, Any]:
        """The terminal ``done`` wire event (agent_id embedded — daemon semantics).

        ``tokens`` is the already-normalised usage block. The two transports differ
        only in whether they estimate ``cost_usd`` into it (the direct-spawn path
        passes the active model tag; the daemon path coerces PLAIN, preserving the
        documented CUR-4 daemon-cost gap), so the choice is made by the caller and
        the resulting block is handed in here.
        """
        event = stream_done_event(
            usage=tokens,
            text=self.final_text or "",
            status=self.final_status or "finished",
            tool_calls=self.tool_calls,
        )
        # The cursor transports ALWAYS carry the agent_id key (even when None) — the
        # daemon/direct callers and the aggregator's resume logic read it. Set it
        # unconditionally here to keep the cursor done event byte-identical.
        event["agent_id"] = self.agent_id
        return event


def stream_done_event(
    *,
    usage: dict[str, Any],
    text: str = "",
    status: str = "finished",
    tool_calls: list[dict[str, Any]] | None = None,
    agent_id: str | None = None,
    ask_user: dict[str, Any] | None = None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the ONE canonical terminal ``done`` wire event for every provider.

    Before this existed the five providers emitted two different terminal shapes:
    claude/cursor put ``text``/``status``/``tool_calls`` at the TOP LEVEL of the
    done event, while gemini/openai/ollama emitted only ``{done, usage}`` and buried
    ``tool_calls`` inside ``usage``. The aggregator
    (:func:`llm_dispatch.complete_chat_aggregated`) had to read both placements
    defensively (providers:arch:0). This helper is the single home of the shape, so
    every provider yields the same keys and the aggregator reads exactly one.

    Contract:
      * ``tool_calls`` lives at the TOP LEVEL (canonical). The streaming SSE producer
        builds its own tool-call ledger from the live ``{"tool_call": …}`` events and
        does NOT read this field; only the blocking/voice aggregator does.
      * ``text`` is the provider's AUTHORITATIVE final text. Providers that stream the
        whole answer as ``delta`` events (gemini/openai/ollama) pass ``""`` — the
        aggregator falls back to the accumulated deltas when this is empty; claude/
        cursor pass the welded final text here.
      * ``agent_id``/``ask_user``/``plan`` are optional. ``agent_id`` is always present
        (possibly ``None``) for the cursor path so its callers keep the key; for the
        text providers it is omitted (they have no agent reuse).
    """
    event: dict[str, Any] = {
        "done": True,
        "usage": usage,
        "text": text,
        "status": status,
        "tool_calls": list(tool_calls or []),
    }
    # agent_id: emit the key (even when None) only when the caller opts in — the cursor
    # transports rely on its presence; the text providers omit it entirely.
    if agent_id is not None:
        event["agent_id"] = agent_id
    if ask_user is not None:
        event["ask_user"] = ask_user
    if plan is not None:
        event["plan"] = plan
    return event


__all__ = [
    "STDOUT_LINE_LIMIT",
    "SEGMENT_GAP",
    "ChatProvider",
    "CursorStreamDecoder",
    "ProviderCapabilities",
    "bridge_timeout",
    "combine_cap",
    "coerce_cost_usd",
    "coerce_token_count",
    "driver_error_to_llm_error",
    "estimate_cost_usd",
    "idle_timeout",
    "prepend_stream_buffer",
    "read_ndjson_line",
    "segment_gap",
    "stream_done_event",
    "total_timeout",
]
