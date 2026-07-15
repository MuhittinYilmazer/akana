"""OpenAI backend dispatch — native text provider (the ``gemini_provider`` counterpart).

``llm_dispatch.stream_user_chat`` / ``complete_chat`` delegate here when
``provider=openai`` (the same pattern as the cursor↔claude↔ollama↔gemini routing).
OpenAI goes **DIRECTLY to OpenAI** via raw ``httpx`` (``OpenAIDriver``; NO openai
SDK, same pattern as the ollama driver) — NOT Cursor's key but the user's own
``openai_api_key`` (secret_store > env). It is a RICH text provider:
``thinking_mode`` is HONORED (reasoning models → ``reasoning_effort``; skipped for
plain chat models that may reject the field), native function-calling (``tools`` +
``tool_calls``) is ON, and native VISION input is supported: IMAGES (``image_url``
data-URI; the OpenAI counterpart to Gemini's ``inline_data``) AND PDFs (``file``
content part, ``file_data`` data-URI; the OpenAI Chat API accepts PDFs inline via
this field). External MCP servers (``mcp_servers.yaml``) ARE reached too: the
``mcp_bridge`` connects to them in-process and their tools join the native FC surface
(``mcp__<server>__<tool>``) — Claude/Cursor parity for the external-tool case (the
same wiring as ollama). What it does NOT support: the caller's ``mcp_servers`` CLI
dict (Claude/Cursor-specific; openai loads its own from yaml via the bridge) and
agent-reuse (cursor-specific).

If there is no key, ``_driver`` raises a CLEAR :class:`LLMCallError` (never a raw
exception) — symmetric with gemini's ``make_client``-None path (NO SDK gate, only a
key gate).

Function-calling is ON: the model can call ``memory_search`` / ``save_memory``
(shared ``llm_tools`` tools, derived from ``GEMINI_TOOL_DECLS``) plus any bridged
``mcp__…`` external tool; the provider dispatches them, appends the response to
messages, and loops until the final text is produced.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from akana.driver.base import DriverError
from akana.driver.openai import OpenAIDriver

from akana_server.concurrency import off_loop
from akana_server.orchestrator import attachments
from akana_server.orchestrator import base
from akana_server.orchestrator import modes
from akana_server.orchestrator import toolloop
from akana_server.orchestrator.chat_persona import CHAT_SYSTEM_PREFIX
from akana_server.orchestrator.errors import LLMCallError
from akana_server.orchestrator.llm_tools import OPENAI_TOOL_DECLS, dispatch_llm_tool
from akana_server.orchestrator.mcp_bridge import McpToolBridge, external_mcp_bridge
from akana_server.orchestrator.openai_shared import (
    resolve_openai_base_url,
    resolve_openai_key,
)

if TYPE_CHECKING:
    from akana_server.config import Settings

#: ``stateless=False`` PRESERVES the historical chat_context behaviour: openai was
#: NOT in the open-coded ``("ollama", "gemini")`` statelessness list, so it fell
#: through to the reuse/agent-id path. openai persists no agent id, so that path
#: still yields "bootstrap needed" (``get_agent_id`` is None) — the observable
#: result is identical — but the declared value mirrors the old list exactly
#: rather than reclassifying the provider.
CAPABILITIES = base.ProviderCapabilities(stateless=False)

#: Upper bound for the function-calling loop (guards against an infinite tool-call loop).
#: Identical to gemini_provider._MAX_TOOL_ROUNDS — both surfaces share the same budget.
_MAX_TOOL_ROUNDS = 5

#: akana ``thinking_mode`` → OpenAI ``reasoning_effort`` mapping. OpenAI accepts
#: three levels (low/medium/high); akana's canonical level names (chat_producer
#: ``body.thinking_mode``) are mapped to these. An unknown value → medium (safe).
#: The Akana canonical level names (chat_producer: hizli/normal/derin/yogun/azami/
#: ultra) are DERIVED from the single ``modes`` source (``modes.tier_map``) → one edit
#: adds a mode, and the drift guard keeps every provider in sync. The OpenAI native
#: level names (low/medium/high) are direct pass-through aliases on top; ``minimal``
#: has no OpenAI level so it maps to the lowest (low). An unknown value → medium (safe;
#: symmetric with gemini). "ultra" is claude/fable-only; on openai it tops out at high.
_REASONING_EFFORTS: dict[str, str] = {
    # OpenAI native level names (direct pass-through) — the composer sends these VERBATIM
    # when openai is the active provider (no Akana-tier mapping). GPT-5 exposes the full
    # minimal…xhigh ladder; ``xhigh`` (extra-high) is native-only (no Akana tier maps to it).
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    # Akana canonical level names, derived from the shared tier table (drift-guarded);
    # accepted so a non-native sender still resolves — they top out at high.
    **modes.tier_map(low="low", medium="medium", high="high"),
}


def _resolve_openai_model(settings: Settings) -> str:
    """The concrete NATIVE OpenAI model name for the call (persist setting > env > default).

    The dispatch ``model`` argument (the provider-agnostic *cursor* tag that
    chat_producer passes to ALL providers — e.g. ``composer-2`` / ``default`` / or
    even a Cursor-routed ``gpt-5.4-mini``) NEVER reaches here: openai always uses
    its own ``openai_model`` setting (the exact counterpart to gemini's foreign-tag
    guard — because OpenAI names syntactically collide with cursor aliases)."""
    from akana_server.llm_context import load_effective_llm_settings
    from akana_server.llm_settings import resolve_openai_model_tag

    return resolve_openai_model_tag(
        settings, load_effective_llm_settings(settings.data_dir, settings)
    )


def _driver(settings: Settings) -> OpenAIDriver:
    """Return an ``OpenAIDriver`` — a CLEAR ``LLMCallError`` if there is no key (no raw blowup).

    Symmetric with gemini's ``make_client``-None path: there the distinction was
    SDK-not-installed vs. key-missing; for OpenAI the transport (httpx) is a hard
    dependency, so there is ONLY a key gate (only the key is required). If the key is
    absent, a user-facing Settings hint is raised (same tone as gemini's key-missing
    message)."""
    from akana_server.runtime_settings import get_runtime

    key = resolve_openai_key(settings)
    if not key:
        raise LLMCallError(
            "No OpenAI API key configured — enter openai_api_key under "
            "Settings → Identity (or the OPENAI_API_KEY environment variable).",
            status_code=503,
        )
    # Runtime-tunable generation ceiling (0 = no timeout; default keeps the
    # historical 300 s). Resolved live per request so a change in the settings
    # panel applies to the next message (the ollama_timeout pattern).
    return OpenAIDriver(
        base_url=resolve_openai_base_url(settings),
        api_key=key,
        model=_resolve_openai_model(settings),
        timeout=float(get_runtime("openai_timeout", settings)),
    )


def _supports_reasoning(model: str) -> bool:
    """Does the model accept the ``reasoning_effort`` field?

    Reasoning models honor it: the o-series (o1/o3/o4/o5…) and GPT-5+ families.
    Classic chat models (gpt-4*/gpt-3.5*) REJECT ``reasoning_effort`` → it is only
    sent for reasoning families (the OpenAI counterpart of gemini's
    ``_supports_thinking_level`` 3+ gate). An unknown/unversioned name is False on
    the safe side (reasoning is not sent).

    E.g.: o5-mini / o3 / o1-preview / gpt-5.4 / gpt-6 → True; gpt-4o / gpt-4.1 /
    gpt-3.5-turbo → False."""
    name = model.lower().strip()
    if re.match(r"o\d", name):  # o1/o3/o4/o5… reasoning series
        return True
    match = re.match(r"gpt-(\d+)", name)
    return bool(match) and int(match.group(1)) >= 5


def _reasoning_effort(thinking_mode: str | None, model: str) -> str | None:
    """akana ``thinking_mode`` → OpenAI ``reasoning_effort`` (empty/unsupported → None).

    If empty/None, returns ``None`` → ``reasoning_effort`` is NOT added to the body
    at all. If the model does NOT support reasoning (a classic chat model), also
    returns ``None`` → the 400 is avoided. Only when the model is a reasoning model
    AND the mode is set: low/medium/high; an unknown mode value falls to medium (safe;
    symmetric with gemini's ``_thinking_config``)."""
    mode = (thinking_mode or "").strip().lower()
    if not mode or not _supports_reasoning(model):
        return None
    return _REASONING_EFFORTS.get(mode, "medium")


#: OpenAI vision request size practical limit is ~20MB; we leave headroom and cut off at
#: 18MB (the shared ``attachments`` default). An attachment that would exceed this
#: CUMULATIVE budget is SILENTLY skipped (better than breaking the turn with a 400).
#: Kept as a module-level name so the budget can be monkeypatched in tests.
_MAX_INLINE_TOTAL_BYTES = attachments.MAX_INLINE_TOTAL_BYTES


def _image_parts(settings: Settings, file_ids: list[str] | None) -> list[dict[str, Any]]:
    """file_ids → OpenAI vision content parts: IMAGES + PDFs (base64 data-URI).

    The OpenAI counterpart to Gemini's ``_add_turn_images``: the selection + cumulative
    budget + defensive-skip logic is shared via
    ``attachments.iter_embeddable_attachments`` (only images and PDFs; unreadable /
    disabled / over-budget attachments are silently skipped; a single attachment error
    does not break the turn). Only the final part SHAPE is openai-specific: IMAGE →
    ``image_url`` (the OpenAI equivalent of gemini's ``inline_data``); PDF → a ``file``
    content part (``{"type":"file","file":{"filename":..,"file_data":"data:
    application/pdf;base64,.."}}``) — the OpenAI Chat Completions API accepts PDFs
    inline ONLY via this field (NOT via image_url)."""
    parts: list[dict[str, Any]] = []
    for rec, data in attachments.iter_embeddable_attachments(
        settings, file_ids, max_total_bytes=_MAX_INLINE_TOTAL_BYTES
    ):
        b64 = base64.b64encode(data).decode("ascii")
        if rec.media_type == "application/pdf":
            # PDF: OpenAI ``file`` part (filename REQUIRED; a reasonable fallback if absent).
            filename = rec.file_name or rec.original_name or "document.pdf"
            parts.append(
                {
                    "type": "file",
                    "file": {
                        "filename": filename,
                        "file_data": f"data:application/pdf;base64,{b64}",
                    },
                }
            )
        else:
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{rec.media_type};base64,{b64}"},
                }
            )
    return parts


def _messages(
    system_prompt: str | None,
    history: list[dict[str, str]] | None,
    user_text: str,
    settings: Settings | None = None,
    file_ids: list[str] | None = None,
) -> list[dict[str, Any]]:
    """(system?) + history + the latest user turn → OpenAI message dicts.

    OpenAI Chat format: the ``system`` role is a single message at the START of the
    message list (unlike gemini's ``system_instruction``; similar to ollama's
    system-message pattern). History roles are carried as-is (assistant/user); empty
    content is skipped. VISION: if ``file_ids`` carry images, the last user turn
    becomes an ARRAY ``content`` of text + ``image_url`` parts (otherwise a plain
    text string)."""
    msgs: list[dict[str, Any]] = []
    sp = (system_prompt or "").strip()
    if sp:
        msgs.append({"role": "system", "content": sp})
    for h in history or []:
        if not isinstance(h, dict):
            continue
        content = str(h.get("content") or "")
        if not content:
            continue
        role = str(h.get("role") or "user")
        msgs.append({"role": role, "content": content})
    img_parts = _image_parts(settings, file_ids) if settings is not None else []
    if img_parts:
        msgs.append(
            {"role": "user", "content": [{"type": "text", "text": user_text}, *img_parts]}
        )
    else:
        msgs.append({"role": "user", "content": user_text})
    return msgs


def _usage_dict(
    usage: dict[str, Any] | None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """OpenAIDriver usage → Akana tokens dict (the claude/ollama/gemini shape).

    Token counts are safely coerced to int with ``base.coerce_token_count`` so that
    a malformed external field does not crash the ``done`` event. ``tool_calls`` carries
    the round's start/end tool-call wire records (parity with ollama/claude → the audit
    ledger + UI cards) — empty when the turn called no tool. OpenAI does not return a
    cost and the price table is Anthropic-specific → ``cost_usd`` is NOT added (same as
    gemini)."""
    usage = usage or {}
    return {
        "prompt_tokens": base.coerce_token_count(usage.get("prompt_tokens")),
        "completion_tokens": base.coerce_token_count(usage.get("completion_tokens")),
        "tool_calls": list(tool_calls or []),
    }


def _assistant_tool_call_message(tool_calls: list[dict[str, Any]]) -> dict[str, Any]:
    """Append the model's ``tool_calls`` round back to the messages (role=assistant).

    OpenAI contract: BEFORE sending tool results, the assistant message that carries
    ``tool_calls`` must be present in the message history; otherwise the next request
    returns 400 ("messages with role 'tool' must be a response to a preceding message
    with 'tool_calls'"). ``arguments`` is preserved as a raw JSON string (as the model
    produced it)."""
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": tc.get("id") or f"call_{i}",
                "type": "function",
                "function": {
                    "name": tc.get("name") or "",
                    "arguments": tc.get("arguments") or "{}",
                },
            }
            for i, tc in enumerate(tool_calls)
        ],
    }


def _openai_call_args(tc: dict[str, Any]) -> dict[str, Any]:
    """OpenAI ``tool_call.arguments`` (raw JSON string) → an args dict (DEFENSIVE).

    OpenAI returns ``arguments`` as a JSON STRING (unlike Ollama's dict); a malformed
    / non-object payload falls back to an empty dict so the tool still runs and the
    turn is not broken. Shared by ``_dispatch_tool_results`` (dispatch) and
    ``_tool_call_events`` (wire cards) so both read the SAME parsed args."""
    try:
        args = json.loads(tc.get("arguments") or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return args if isinstance(args, dict) else {}


async def _dispatch_tool_results(
    settings: Settings,
    conversation_id: str | None,
    tool_calls: list[dict[str, Any]],
    bridge: McpToolBridge,
) -> list[dict[str, Any]]:
    """Dispatch each ``tool_call`` → a list of ``role=tool`` result messages.

    Routes by name: a bridged ``mcp__…`` tool is awaited on its in-process MCP session
    (already async); a native tool (``memory_search``/``save_memory``/vault) goes through
    the synchronous ``dispatch_llm_tool`` off-loaded to a worker thread (sqlite/file side
    effects). Both paths are DEFENSIVE — every error converts to clean text, so the turn
    is not broken. ``arguments`` is a raw JSON string → safely parsed (empty dict if
    malformed). Each result is paired with its ``tool_call_id`` (OpenAI contract); calls
    are dispatched in order so the result list lines up with ``tool_calls`` for the wire
    events."""
    out: list[dict[str, Any]] = []
    for i, tc in enumerate(tool_calls):
        name = tc.get("name") or ""
        args = _openai_call_args(tc)
        if bridge.handles(name):
            result = await bridge.dispatch(name, args)
        else:
            result = await off_loop(dispatch_llm_tool, settings, conversation_id, name, args)
        out.append(
            {
                "role": "tool",
                "tool_call_id": tc.get("id") or f"call_{i}",
                "content": result,
            }
        )
    return out


def _tool_call_events(
    tool_calls: list[dict[str, Any]], results: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Wire ``tool_call`` events for a tool round (start + end) — the ollama/gemini shape.

    Extracts the openai-specific ``(id, name, args)`` per call (OpenAI always supplies an
    ``id``; an index-based synthetic id is the fallback so start/end still pair) plus the
    result text from each ``role=tool`` message, then delegates the wire-shape
    construction to the shared :func:`toolloop.tool_call_events` builder. ``results`` lines
    up with ``tool_calls`` (built in the same order by ``_dispatch_tool_results``)."""
    calls: list[tuple[str, str, dict[str, Any]]] = []
    for idx, tc in enumerate(tool_calls):
        name = tc.get("name") or ""
        cid = tc.get("id") or f"openai-tool-{idx}"
        calls.append((cid, name, _openai_call_args(tc)))
    contents = [r.get("content") for r in results]
    return toolloop.tool_call_events(calls, contents)


def _as_llm_error(exc: DriverError) -> LLMCallError:
    """DriverError → ``LLMCallError`` (a clear message the callers' except can catch).

    Delegates to the shared ``base.driver_error_to_llm_error`` with ``pass_status=True``:
    openai forwards the synthesized status into ``friendly_provider_error`` (structural
    hint classifies auth/rate-limit/timeout even on ambiguous text) — the deliberate
    difference from ollama's variant. Status taxonomy: timeout→504, unavailable→503,
    other→502 (driver status honored if present)."""
    return base.driver_error_to_llm_error(exc, "openai", pass_status=True)


async def stream_user_chat(
    settings: Settings,
    user_text: str,
    *,
    history: list[dict[str, str]] | None = None,
    model: str | None = None,  # openai: ignored (cursor tag; _resolve is used instead)
    conversation_id: str | None = None,  # openai: used for scope in tool dispatch
    agent_id: str | None = None,  # openai: ignored (no agent-reuse)
    reuse_agent: bool = True,  # openai: ignored
    mcp_servers: dict[str, Any] | None = None,  # openai: CLI dict ignored; MCP tools load from yaml via the bridge
    system_prompt: str | None = None,
    thinking_mode: str | None = None,
    file_ids: list[str] | None = None,  # openai: NATIVE vision input (image_url data-URI)
    plan_mode: bool = False,  # openai: accepted-and-ignored (claude-only ExitPlanMode)
    auto_continue: bool = False,  # openai: accepted-and-ignored (claude-only continuation)
) -> AsyncIterator[dict[str, Any]]:
    """Translate the OpenAI stream into Akana wire events ({delta|thinking|done|usage}).

    Native function-calling (STREAMING): stream the round; accumulate incoming
    ``tool_calls`` frames. If the round called a tool, we do NOT emit text (it is an
    intermediate round), dispatch the tools off-loop, append the call+results to
    messages, and start a NEW ``stream_chat`` — until the model produces final text
    (without calling a tool), for at most ``_MAX_TOOL_ROUNDS`` rounds. Reasoning text
    is emitted as the SAME ``{"thinking":...}`` event as gemini. DEFENSIVE: tool
    results are text, the turn is not broken."""
    driver = _driver(settings)
    model_name = _resolve_openai_model(settings)
    # Default persona → system_prompt=None; fall back to CHAT_SYSTEM_PREFIX so the tool-use directives reach the model (claude/cursor parity).
    effective_system = system_prompt or CHAT_SYSTEM_PREFIX
    messages = _messages(effective_system, history, user_text, settings, file_ids)
    effort = _reasoning_effort(thinking_mode, model_name)
    # Tokens from ALL rounds are ACCUMULATED (not overwritten): each API call is
    # billed separately → intermediate tool rounds also enter the final usage.
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0}
    all_tool_calls: list[dict[str, Any]] = []  # start/end records → done.usage.tool_calls (audit)
    try:
        async with external_mcp_bridge(settings) as bridge:
            adapter = _OpenAIStreamAdapter(
                settings, driver, messages, usage_totals, bridge, effort, conversation_id
            )
            async for ev in toolloop.run_stream_loop(
                adapter,
                max_rounds=_MAX_TOOL_ROUNDS,
                on_tool_call=all_tool_calls.append,
            ):
                yield ev
    except LLMCallError:
        raise
    except DriverError as exc:
        raise _as_llm_error(exc) from exc
    # Canonical terminal event (base.stream_done_event): tool_calls at the top level,
    # text="" because the whole answer was streamed as ``delta`` events (the aggregator
    # falls back to the accumulated deltas). OpenAI has no agent reuse → no agent_id.
    # ``usage`` keeps its own tool_calls copy (unchanged shape) for any usage reader.
    yield base.stream_done_event(
        usage=_usage_dict(usage_totals, all_tool_calls),
        tool_calls=all_tool_calls,
    )


class _OpenAIStreamAdapter:
    """``toolloop.ToolLoopAdapter`` for the OpenAI streaming surface.

    One round is a ``driver.stream_chat`` pass; text deltas + reasoning deltas are
    buffered (the engine replays them), and the terminal chunk carries the round's usage
    + accumulated ``tool_calls`` frames. The round-limit final round is a tool-less
    ``stream_chat`` (unified round-limit behavior)."""

    def __init__(
        self, settings, driver, messages, usage_totals, bridge, effort, conversation_id
    ) -> None:
        self._settings = settings
        self._driver = driver
        self._messages = messages
        self._usage = usage_totals
        self._bridge = bridge
        self._effort = effort
        self._conversation_id = conversation_id
        # Native FC surface + any bridged ``mcp__…`` external tools (empty yaml → no-op).
        self._tools = OPENAI_TOOL_DECLS + bridge.decls

    async def run_round(self) -> toolloop.RoundResult:
        result = toolloop.RoundResult()
        tool_calls: list[dict[str, Any]] = []
        async for chunk in self._driver.stream_chat(
            self._messages, tools=self._tools, reasoning_effort=self._effort
        ):
            if chunk.delta:
                result.answer_deltas.append({"delta": chunk.delta, "done": False})
            raw = chunk.raw or {}
            reasoning = raw.get("reasoning")
            if reasoning:
                # Reasoning is part of the thinking stream (same semantics as gemini's
                # include_thoughts delta); the wire contract requires a DICT.
                result.has_thinking = True
                result.thinking_deltas.append(
                    {"thinking": {"phase": "delta", "text": reasoning}}
                )
            if chunk.done:
                if chunk.usage:
                    base.accumulate_usage(self._usage, chunk.usage)
                tcs = raw.get("tool_calls")
                if isinstance(tcs, list) and tcs:
                    tool_calls = tcs
        result.pending = tool_calls
        return result

    async def dispatch_and_append(self, pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results = await _dispatch_tool_results(
            self._settings, self._conversation_id, pending, self._bridge
        )
        events = _tool_call_events(pending, results)
        self._messages.append(_assistant_tool_call_message(pending))
        self._messages.extend(results)
        return events

    async def run_final_round(self) -> AsyncIterator[dict[str, Any]]:
        # Round limit reached: force one final TOOL-LESS stream so the model answers with
        # real text instead of a truncation notice (unified with gemini/ollama).
        thinking_open = False
        async for chunk in self._driver.stream_chat(
            self._messages, reasoning_effort=self._effort
        ):
            raw = chunk.raw or {}
            reasoning = raw.get("reasoning")
            if reasoning:
                thinking_open = True
                yield {"thinking": {"phase": "delta", "text": reasoning}}
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
    file_ids: list[str] | None = None,  # openai: NATIVE vision input (image_url data-URI)
    plan_mode: bool = False,  # openai: accepted-and-ignored (claude-only)
) -> tuple[str, str, dict[str, Any]]:
    """One-shot completion → (text, status, raw) — the ``gemini_provider.complete_chat`` shape.

    Native function-calling: if the response contains ``tool_calls``, each one is
    dispatched off-loop, the call+results are appended to messages, and
    ``complete_once`` is called again until the final text is produced (without calling
    a tool), for at most ``_MAX_TOOL_ROUNDS`` rounds. DEFENSIVE: tool results are text
    → the turn is not broken.

    When the round limit is exhausted (the model called a tool on EVERY round and
    produced no text), the old behaviour returned an empty answer (``""``) — the user
    would see an empty bubble. Instead, a single FINAL completion is made after the
    loop: ``tools`` is deliberately omitted → the model can no longer call a tool and
    is FORCED to respond with text (symmetric with the streaming path that emits any
    accumulated text on exhaustion). Tokens are ACCUMULATED across all rounds (not
    overwritten; each API call is billed separately)."""
    driver = _driver(settings)
    model_name = _resolve_openai_model(settings)
    # Default persona → system_prompt=None; in chat context fall back to CHAT_SYSTEM_PREFIX (claude parity).
    effective_system = system_prompt or (CHAT_SYSTEM_PREFIX if chat_mode else None)
    messages = _messages(effective_system, history, user_text, settings, file_ids)
    effort = _reasoning_effort(thinking_mode, model_name)
    # Tokens from ALL rounds are ACCUMULATED (not overwritten): intermediate tool
    # rounds also enter the final usage (each complete_once call is billed separately).
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0}
    all_tool_calls: list[dict[str, Any]] = []  # start/end records → usage.tool_calls (audit)
    try:
        async with external_mcp_bridge(settings) as bridge:
            adapter = _OpenAICompleteAdapter(
                settings, driver, messages, usage_totals, bridge, effort, conversation_id
            )
            text = await toolloop.run_complete_loop(
                adapter,
                max_rounds=_MAX_TOOL_ROUNDS,
                on_tool_call=all_tool_calls.append,
            )
    except LLMCallError:
        raise
    except DriverError as exc:
        raise _as_llm_error(exc) from exc
    return text, "finished", _usage_dict(usage_totals, all_tool_calls)


class _OpenAICompleteAdapter:
    """``toolloop.CompleteLoopAdapter`` for the OpenAI one-shot surface.

    Each round is a ``driver.complete_once`` pass; a tool round dispatches + appends the
    call+results to the message list and continues. The round-limit final round is a
    tool-less ``complete_once`` (unified round-limit behavior — the model is forced to
    answer with text when it can no longer call a tool). Mirrors ``_OllamaCompleteAdapter``
    (and the streaming ``_OpenAIStreamAdapter``); the manual loop this replaced carried a
    dead ``if not text:`` guard on the exhaustion path."""

    def __init__(
        self, settings, driver, messages, usage_totals, bridge, effort, conversation_id
    ) -> None:
        self._settings = settings
        self._driver = driver
        self._messages = messages
        self._usage = usage_totals
        self._bridge = bridge
        self._effort = effort
        self._conversation_id = conversation_id
        # Native FC surface + any bridged ``mcp__…`` external tools (empty yaml → no-op).
        self._tools = OPENAI_TOOL_DECLS + bridge.decls

    async def complete_round(self) -> tuple[str, Any]:
        result = await self._driver.complete_once(
            self._messages, tools=self._tools, reasoning_effort=self._effort
        )
        base.accumulate_usage(self._usage, result.get("usage"))
        tool_calls = result.get("tool_calls") or []
        return (result.get("text") or ""), tool_calls

    async def dispatch_and_append(self, pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
        results = await _dispatch_tool_results(
            self._settings, self._conversation_id, pending, self._bridge
        )
        events = _tool_call_events(pending, results)
        self._messages.append(_assistant_tool_call_message(pending))
        self._messages.extend(results)
        return events

    async def complete_final_round(self) -> str:
        # Round limit reached: one final TOOL-LESS completion forces a real answer.
        final = await self._driver.complete_once(
            self._messages, reasoning_effort=self._effort
        )
        base.accumulate_usage(self._usage, final.get("usage"))
        return final.get("text") or ""


__all__ = ["complete_chat", "stream_user_chat"]
