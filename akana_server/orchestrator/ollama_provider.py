"""Ollama backend dispatch — local model provider (the ``gemini_provider`` counterpart).

``llm_dispatch.stream_user_chat`` / ``complete_chat`` delegate here when
``provider=ollama`` (the same pattern as the cursor↔claude↔gemini routing). Ollama
is a local LLM but is NOW a RICH text provider: it supports native function-calling
(``/api/chat`` with ``tools=[...]`` OpenAI schema → ``message.tool_calls``) and
thinking (``think: true`` → a separate ``message.thinking`` field) — on par with
gemini/openai. External MCP servers (``mcp_servers.yaml``) ARE reached too: the
``mcp_bridge`` connects to them in-process and their tools join the native FC surface
(``mcp__<server>__<tool>``) — Claude/Cursor parity for the external-tool case. What it
still does NOT support: the caller's ``mcp_servers`` CLI dict (Claude/Cursor-specific;
Ollama loads its own from yaml via the bridge), agent-reuse (cursor-specific), and image
input (out of scope). The actual HTTP/parse work lives in ``OllamaDriver``
(``src/akana/driver/ollama.py``); this module drives the tool loop and connects the
stream to the Akana wire-event format and ``LLMCallError`` taxonomy.

Native function-calling: the model can call ``memory_search`` / ``save_memory``
(shared ``llm_tools.OPENAI_TOOL_DECLS`` — identical two tools as gemini); the
provider dispatches each call with ``dispatch_llm_tool``, appends the assistant's
tool_calls round + each tool result (``role=tool``) to the message list, and loops
until the model produces final text without calling a tool (at most
``_MAX_TOOL_ROUNDS`` rounds). The tools are NOT MCP — Ollama native function-calling.
When ``thinking_mode`` is set, ``think`` is enabled and the thinking text is emitted
as a SEPARATE ``thinking`` wire event (same shape as gemini/claude → the frontend
renders it provider-agnostically); it does NOT bleed into the answer.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from akana.driver.base import DriverError

from akana_server.concurrency import off_loop
from akana_server.orchestrator import base
from akana_server.orchestrator import modes  # noqa: F401  (shared canonical thinking-mode source; see _think_flag)
from akana_server.orchestrator import toolloop
from akana_server.orchestrator.chat_persona import CHAT_SYSTEM_PREFIX
from akana_server.orchestrator.errors import LLMCallError
from akana_server.orchestrator.llm_tools import OPENAI_TOOL_DECLS, dispatch_llm_tool
from akana_server.orchestrator.mcp_bridge import McpToolBridge, external_mcp_bridge

if TYPE_CHECKING:
    from akana.driver.ollama import OllamaDriver
    from akana_server.config import Settings

log = logging.getLogger(__name__)

#: Ollama is STATELESS — no server-side session/agent to resume; every call is fresh,
#: so history is always flattened into the prompt (queried via
#: llm_dispatch.provider_capabilities; consumed by chat_context).
CAPABILITIES = base.ProviderCapabilities(stateless=True)

#: Upper bound for the function-calling loop (guards against an infinite tool-call loop)
#: — symmetric with gemini_provider._MAX_TOOL_ROUNDS.
_MAX_TOOL_ROUNDS = 5


def _messages(
    system_prompt: str | None,
    history: list[dict[str, str]] | None,
    user_text: str,
) -> list[dict[str, Any]]:
    """(system?) + history + the latest user turn → Ollama ``/api/chat`` message list.

    Returns a raw OpenAI-style dict list (NOT a neutral ``Message``) so the tool
    loop can append the assistant's ``tool_calls`` round and ``{"role":"tool",...}``
    results to this list (a neutral ``Message`` carries only role/content and cannot
    represent tool rounds). Ollama natively accepts the ``system`` role inside
    ``messages``."""
    msgs: list[dict[str, Any]] = []
    if system_prompt:
        msgs.append({"role": "system", "content": system_prompt})
    for h in history or []:
        content = str(h.get("content") or "")
        if content:
            msgs.append({"role": str(h.get("role") or "user"), "content": content})
    msgs.append({"role": "user", "content": user_text})
    return msgs


def _resolve_ollama_model(settings: Settings, model: str | None) -> str:
    """The concrete ollama model tag for the driver.

    chat_producer passes the provider-agnostic ``_active_cursor_model`` to ALL
    providers (e.g. ``'default'`` or ``'composer-2'`` for the user) → NEVER valid
    for ollama; passing it raw gives ``model 'default' not found`` (404). So the
    ollama model is resolved from the persisted setting (switcher choice →
    ``resolve_ollama_model_tag``). The dispatch's ``model`` is honored ONLY if it
    is an explicit ollama tag (``name:tag``, not cursor/claude) — symmetric with
    claude_provider._resolve_claude_model."""
    tag = (model or "").strip()
    if ":" in tag and not tag.startswith(("composer-", "claude-")):
        return tag  # explicit ollama tag (e.g. llama3.1:latest)
    from akana_server.llm_context import load_effective_llm_settings
    from akana_server.llm_settings import resolve_ollama_model_tag

    return resolve_ollama_model_tag(
        settings, load_effective_llm_settings(settings.data_dir, settings)
    )


def _driver(settings: Settings, model: str | None) -> OllamaDriver:
    from akana.driver.ollama import OllamaDriver
    from akana_server.runtime_settings import get_runtime

    # Runtime-tunable generation ceiling (0 = no timeout, the default). Resolved
    # live per request so a change in the settings panel applies to the next message.
    return OllamaDriver(
        url=getattr(settings, "ollama_url", None) or "http://localhost:11434",
        model=_resolve_ollama_model(settings, model),
        timeout=float(get_runtime("ollama_timeout", settings)),
    )


def _think_flag(thinking_mode: str | None) -> bool:
    """akana ``thinking_mode`` → Ollama ``think`` flag (empty/None → ``False``).

    Ollama ``think`` is a BOOLEAN (unlike gemini's graduated ``thinking_level`` /
    openai's ``reasoning_effort`` — those DERIVE their per-level tables from the shared
    canonical ``modes.THINKING_MODES`` source): on/off. If the user selected ANY
    thinking level (any of ``modes.THINKING_MODES`` — including ``hizli``) we enable
    thinking; if empty/None, ``think`` is NOT sent at all (the model uses its own
    default behaviour). Because Ollama has no graduated tiers there is no per-name
    mapping table to keep in sync — the canonical names collapse to a single boolean."""
    return bool((thinking_mode or "").strip())


# --- Native function-calling helpers ----------------------------------
#
# Ollama stream chunks carry ``message.tool_calls`` inside ``ChatChunk.raw["tool_calls"]``
# (the driver parks them there). The shape is OpenAI-style:
# ``[{"function": {"name": str, "arguments": dict|str}}]`` (optional ``id``).


def _tool_calls_from_chunk(chunk: Any) -> list[dict[str, Any]]:
    """Extract the raw ``tool_calls`` list from a stream chunk ([] = no tools)."""
    raw = getattr(chunk, "raw", None)
    if not isinstance(raw, dict):
        return []
    calls = raw.get("tool_calls")
    return list(calls) if isinstance(calls, list) else []


def _thinking_from_chunk(chunk: Any) -> str:
    """Extract the ``thinking`` text from a stream chunk ('' = no thinking)."""
    raw = getattr(chunk, "raw", None)
    if not isinstance(raw, dict):
        return ""
    think = raw.get("thinking")
    return think if isinstance(think, str) else ""


def _call_name_args(call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Single ``tool_call`` → (name, args dict). If ``arguments`` is a string it is JSON-parsed.

    Ollama generally returns ``arguments`` as a DICT (unlike OpenAI's str JSON),
    but we handle both shapes DEFENSIVELY: dict → use as-is; str → JSON parse,
    falling back to an empty dict if malformed (the tool still returns a safe result,
    the turn is not broken)."""
    fn = call.get("function") if isinstance(call, dict) else None
    fn = fn if isinstance(fn, dict) else {}
    name = str(fn.get("name") or "")
    raw_args = fn.get("arguments")
    if isinstance(raw_args, dict):
        return name, dict(raw_args)
    if isinstance(raw_args, str) and raw_args.strip():
        import json

        try:
            parsed = json.loads(raw_args)
            return name, dict(parsed) if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return name, {}
    return name, {}


def _assistant_tool_call_message(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Append the model's tool-call round back to the message list (``role=assistant``).

    Ollama expects the assistant's ``tool_calls`` to be present in history on the next
    request (OpenAI pattern). The raw calls are carried as-is (``content`` is empty)."""
    return {"role": "assistant", "content": "", "tool_calls": list(calls)}


def _tool_result_message(name: str, result: str, call_id: Any = None) -> dict[str, Any]:
    """Wrap a single tool result into a ``role=tool`` message (the model reads it next round).

    Ollama accepts the ``tool`` role with ``content`` (result text) + ``tool_name``;
    ``tool_call_id`` is also carried when present (some versions use it for matching)."""
    msg: dict[str, Any] = {"role": "tool", "content": result, "tool_name": name}
    if call_id is not None:
        msg["tool_call_id"] = call_id
    return msg


async def _dispatch_calls(
    settings: Settings,
    conversation_id: str | None,
    calls: list[dict[str, Any]],
    bridge: McpToolBridge,
) -> list[dict[str, Any]]:
    """Dispatch each tool call → a list of ``role=tool`` result messages.

    Routes by name: a bridged ``mcp__…`` tool is awaited on its in-process MCP
    session (already async); a native tool (``memory_search``/``save_memory``/vault)
    goes through the synchronous ``dispatch_llm_tool`` off-loaded to a worker thread
    (sqlite/file side effects). Both paths are DEFENSIVE — every error converts to
    clean text, so the turn is not broken. Calls are dispatched in order (sequential
    await) so the result list lines up with ``calls`` for the wire events."""
    out: list[dict[str, Any]] = []
    for call in calls:
        name, args = _call_name_args(call)
        if bridge.handles(name):
            result = await bridge.dispatch(name, args)
        else:
            result = await off_loop(dispatch_llm_tool, settings, conversation_id, name, args)
        cid = call.get("id") if isinstance(call, dict) else None
        out.append(_tool_result_message(name, result, cid))
    return out


def _tool_call_events(
    calls: list[dict[str, Any]], results: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Wire ``tool_call`` events for a tool round (start + end) — the gemini/claude shape.

    Extracts the ollama-specific ``(id, name, args)`` per call (an index-based synthetic
    id is the fallback when there is no ``id`` so start/end pairs can be matched) plus the
    result text from each ``role=tool`` message, then delegates the wire-shape
    construction to the shared :func:`toolloop.tool_call_events` builder."""
    parsed: list[tuple[str, str, dict[str, Any]]] = []
    for idx, call in enumerate(calls):
        name, args = _call_name_args(call)
        cid = (call.get("id") if isinstance(call, dict) else None) or f"ollama-tool-{idx}"
        parsed.append((cid, name, args))
    contents = [r.get("content") for r in results]
    return toolloop.tool_call_events(parsed, contents)


def _as_llm_error(exc: DriverError) -> LLMCallError:
    """DriverError → ``LLMCallError`` (a clear message the callers' except can catch).

    Delegates to the shared ``base.driver_error_to_llm_error`` with ``pass_status=False``:
    ollama does NOT forward the synthesized status into ``friendly_provider_error`` (it
    relies on text scanning), which is the one deliberate divergence from openai's
    variant. See that helper's docstring."""
    return base.driver_error_to_llm_error(exc, "ollama", pass_status=False)


# --- Graceful capability fallback (tools / thinking) ------------------
#
# Not every Ollama model supports native function-calling or thinking: base, embedding,
# and older chat models reject ``tools=[...]`` / ``think: true`` with HTTP 400 (e.g.
# ``"<model> does not support tools"``). The provider sends both by default (rich path);
# if the model can't take them it must DEGRADE, not fail the whole turn.


def _unsupported(exc: DriverError, keyword: str) -> bool:
    """True when an Ollama error means the model does not support a feature.

    Matches the 400 body Ollama returns for an unsupported ``tools``/``think`` request
    (``'support' + <keyword>``, status 400 or unset). Used to decide whether to retry the
    round WITHOUT that feature rather than surfacing the error."""
    msg = (getattr(exc, "message", "") or "").lower()
    if "support" not in msg or keyword not in msg:
        return False
    code = getattr(exc, "status_code", None)
    return code is None or code == 400


async def _stream_round(
    driver: OllamaDriver,
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None,
    think: bool,
) -> AsyncIterator[Any]:
    """One model round, with graceful capability fallback.

    Streams ``driver.stream_chat_messages``; if it fails BEFORE yielding any chunk with an
    Ollama 'does not support tools/thinking' 400, it retries the same round without the
    offending feature (tools first, then think). The 400 is raised on connect (before any
    token), so dropping the feature and retrying leaks no partial output. Once a chunk has
    streamed we never retry — a mid-stream error propagates untouched (the caller maps it
    to ``LLMCallError``). Models that DO support the features take the happy path with zero
    overhead (no extra request)."""
    attempt_tools, attempt_think = tools, think
    while True:
        yielded = False
        try:
            async for chunk in driver.stream_chat_messages(
                messages, tools=attempt_tools, think=attempt_think
            ):
                yielded = True
                yield chunk
            return
        except DriverError as exc:
            if yielded:
                raise
            if attempt_tools and _unsupported(exc, "tool"):
                # Model lacks tool support: retry WITHOUT tools so the turn still answers,
                # but warn — memory_search/save_memory/vault_* + bridged MCP tools silently
                # vanish this turn (the symptom behind "ollama can't reach memory/vault").
                log.warning(
                    "ollama model rejected tools; retrying without function-calling "
                    "(memory/vault/MCP tools unavailable this turn): %s",
                    exc.message,
                )
                attempt_tools = None
                continue
            if attempt_think and _unsupported(exc, "think"):
                attempt_think = False
                continue
            raise


async def stream_user_chat(
    settings: Settings,
    user_text: str,
    *,
    history: list[dict[str, str]] | None = None,
    model: str | None = None,
    conversation_id: str | None = None,  # ollama: used for scope in tool dispatch
    agent_id: str | None = None,  # ollama: ignored (no agent-reuse)
    reuse_agent: bool = True,  # ollama: ignored
    mcp_servers: dict[str, Any] | None = None,  # ollama: no MCP tools (uses native FC)
    system_prompt: str | None = None,
    thinking_mode: str | None = None,  # ollama: think bool (no low/medium/high levels)
    plan_mode: bool = False,  # ollama: accepted-and-ignored (claude-only ExitPlanMode)
    file_ids: list[str] | None = None,  # ollama: accepted-and-ignored (no native vision input)
    auto_continue: bool = False,  # ollama: accepted-and-ignored (claude-only continuation)
) -> AsyncIterator[dict[str, Any]]:
    """Translate the OllamaDriver stream into Akana wire events ({delta|thinking|tool_call|done}).

    Native function-calling (STREAMING): stream the round; accumulate incoming
    ``tool_calls``, emit thinking text as a separate ``thinking`` event. If the round
    called a tool we do NOT emit text (it is an intermediate round), dispatch tools
    off-loop, append the assistant tool_calls round + results to the message list, and
    re-stream — until the model produces final text without calling a tool (at most
    ``_MAX_TOOL_ROUNDS`` rounds). DEFENSIVE: tool results are text, the turn is not
    broken. ``think`` is enabled when ``thinking_mode`` is set."""
    driver = _driver(settings, model)
    # Default persona → system_prompt=None; fall back to CHAT_SYSTEM_PREFIX so the tool-use directives reach the model (claude/cursor parity).
    effective_system = system_prompt or CHAT_SYSTEM_PREFIX
    messages = _messages(effective_system, history, user_text)
    think = _think_flag(thinking_mode)
    # BUGFIX: ACCUMULATE tokens across rounds (each tool round is a separate billed
    # /api/chat POST); the old ``usage = dict(chunk.usage)`` overwrote intermediate
    # rounds → under-reported usage. See base.accumulate_usage.
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0}
    all_tool_calls: list[dict[str, Any]] = []
    try:
        # External MCP servers (mcp_servers.yaml) bridged in-process → their tools join
        # the native FC surface. Empty/missing yaml = zero-cost no-op (no subprocess).
        async with external_mcp_bridge(settings) as bridge:
            adapter = _OllamaStreamAdapter(
                settings, driver, messages, usage_totals, bridge, think, conversation_id
            )
            async for ev in toolloop.run_stream_loop(
                adapter,
                max_rounds=_MAX_TOOL_ROUNDS,
                on_tool_call=all_tool_calls.append,
            ):
                yield ev
    except DriverError as exc:
        raise _as_llm_error(exc) from exc
    # Canonical terminal event (base.stream_done_event): tool_calls at the top level,
    # text="" because the whole answer was streamed as ``delta`` events (the aggregator
    # falls back to the accumulated deltas). Ollama has no agent reuse → no agent_id.
    # ``usage`` keeps its own tool_calls copy (unchanged shape) for any usage reader.
    yield base.stream_done_event(
        usage={**usage_totals, "tool_calls": all_tool_calls},
        tool_calls=all_tool_calls,
    )


class _OllamaStreamAdapter:
    """``toolloop.ToolLoopAdapter`` for the Ollama streaming surface.

    Buffers each round's answer + thinking deltas (the engine replays them with the
    right ordering), collects the round's tool calls, and dispatches/appends them. The
    round-limit final round is a tool-less re-stream (unified round-limit behavior)."""

    def __init__(
        self, settings, driver, messages, usage_totals, bridge, think, conversation_id
    ) -> None:
        self._settings = settings
        self._driver = driver
        self._messages = messages
        self._usage = usage_totals
        self._bridge = bridge
        self._think = think
        self._conversation_id = conversation_id
        self._tools = OPENAI_TOOL_DECLS + bridge.decls

    async def run_round(self) -> toolloop.RoundResult:
        result = toolloop.RoundResult()
        pending: list[dict[str, Any]] = []
        # Do not pass model again — _driver already stored the resolved tag in self._model.
        async for chunk in _stream_round(
            self._driver, self._messages, tools=self._tools, think=self._think
        ):
            pending.extend(_tool_calls_from_chunk(chunk))
            think_text = _thinking_from_chunk(chunk)
            if think_text:
                result.has_thinking = True
                # Thinking text must NOT bleed into the answer → separate `thinking` event.
                result.thinking_deltas.append(
                    {"thinking": {"phase": "delta", "text": think_text}}
                )
            if chunk.delta:
                result.answer_deltas.append({"delta": chunk.delta, "done": False})
            if chunk.done and chunk.usage:
                # Accumulate (do NOT overwrite): sum this round's tokens so intermediate
                # tool rounds are counted.
                base.accumulate_usage(self._usage, chunk.usage)
        result.pending = pending
        return result

    async def dispatch_and_append(self, pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results = await _dispatch_calls(
            self._settings, self._conversation_id, pending, self._bridge
        )
        events = _tool_call_events(pending, results)
        self._messages.append(_assistant_tool_call_message(pending))
        self._messages.extend(results)
        return events

    async def run_final_round(self) -> AsyncIterator[dict[str, Any]]:
        # Round limit reached: force one final TOOL-LESS round so the model answers with
        # real text instead of an empty bubble (unified with gemini/openai).
        thinking_open = False
        async for chunk in _stream_round(
            self._driver, self._messages, tools=None, think=self._think
        ):
            think_text = _thinking_from_chunk(chunk)
            if think_text:
                thinking_open = True
                yield {"thinking": {"phase": "delta", "text": think_text}}
            if chunk.delta:
                yield {"delta": chunk.delta, "done": False}
            if chunk.done and chunk.usage:
                base.accumulate_usage(self._usage, chunk.usage)
        if thinking_open:
            yield {"thinking": {"phase": "completed"}}


async def complete_chat(
    settings: Settings,
    user_text: str,
    *,
    history: list[dict[str, str]] | None = None,
    model: str | None = None,
    chat_mode: bool = True,
    conversation_id: str | None = None,
    agent_id: str | None = None,
    reuse_agent: bool = True,
    mcp_servers: dict[str, Any] | None = None,
    system_prompt: str | None = None,
    thinking_mode: str | None = None,
    plan_mode: bool = False,  # ollama: accepted-and-ignored (claude-only)
    file_ids: list[str] | None = None,  # ollama: accepted-and-ignored (no native vision)
) -> tuple[str, str, dict[str, Any]]:
    """One-shot completion → (text, status, raw) — the ``gemini_provider.complete_chat`` shape.

    Native function-calling: if the response calls a tool, each one is dispatched
    off-loop, the assistant tool_calls round + results are appended to messages, and
    re-streaming continues until the model produces final text without calling a tool
    (at most ``_MAX_TOOL_ROUNDS`` rounds). Accumulated ``content`` parts in the stream
    give the final text; the last ``done`` chunk gives usage. DEFENSIVE: tool results
    are text → the turn is not broken."""
    driver = _driver(settings, model)
    # Default persona → system_prompt=None; in chat context fall back to CHAT_SYSTEM_PREFIX (claude parity).
    effective_system = system_prompt or (CHAT_SYSTEM_PREFIX if chat_mode else None)
    messages = _messages(effective_system, history, user_text)
    think = _think_flag(thinking_mode)
    # BUGFIX: ACCUMULATE tokens across rounds (see stream_user_chat) — the old
    # ``usage = dict(chunk.usage)`` overwrote intermediate tool rounds.
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0}
    all_tool_calls: list[dict[str, Any]] = []
    try:
        # External MCP servers (mcp_servers.yaml) bridged in-process; empty = no-op.
        async with external_mcp_bridge(settings) as bridge:
            adapter = _OllamaCompleteAdapter(
                settings, driver, messages, usage_totals, bridge, think, conversation_id
            )
            final_text = await toolloop.run_complete_loop(
                adapter,
                max_rounds=_MAX_TOOL_ROUNDS,
                on_tool_call=all_tool_calls.append,
            )
    except DriverError as exc:
        raise _as_llm_error(exc) from exc
    return final_text, "finished", {**usage_totals, "tool_calls": all_tool_calls}


class _OllamaCompleteAdapter:
    """``toolloop.CompleteLoopAdapter`` for the Ollama one-shot surface.

    Ollama has no separate non-stream endpoint — the "one-shot" round consumes the same
    stream and joins its content deltas into the answer text. The round-limit final
    round is a tool-less re-stream (unified round-limit behavior)."""

    def __init__(
        self, settings, driver, messages, usage_totals, bridge, think, conversation_id
    ) -> None:
        self._settings = settings
        self._driver = driver
        self._messages = messages
        self._usage = usage_totals
        self._bridge = bridge
        self._think = think
        self._conversation_id = conversation_id
        self._tools = OPENAI_TOOL_DECLS + bridge.decls

    async def _consume_round(self, *, tools) -> tuple[str, list[dict[str, Any]]]:
        pending: list[dict[str, Any]] = []
        text_parts: list[str] = []
        async for chunk in _stream_round(
            self._driver, self._messages, tools=tools, think=self._think
        ):
            pending.extend(_tool_calls_from_chunk(chunk))
            if chunk.delta:
                text_parts.append(chunk.delta)
            if chunk.done and chunk.usage:
                base.accumulate_usage(self._usage, chunk.usage)
        return "".join(text_parts), pending

    async def complete_round(self) -> tuple[str, Any]:
        return await self._consume_round(tools=self._tools)

    async def dispatch_and_append(self, pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results = await _dispatch_calls(
            self._settings, self._conversation_id, pending, self._bridge
        )
        events = _tool_call_events(pending, results)
        self._messages.append(_assistant_tool_call_message(pending))
        self._messages.extend(results)
        return events

    async def complete_final_round(self) -> str:
        # Round limit reached: one final TOOL-LESS round forces a real answer.
        text, _pending = await self._consume_round(tools=None)
        return text


__all__ = ["complete_chat", "stream_user_chat"]
