"""Gemini backend dispatch — native text provider (the ``ollama_provider`` counterpart).

``llm_dispatch.stream_user_chat`` / ``complete_chat`` delegate here when
``provider=gemini`` (the same pattern as the cursor↔claude↔ollama routing).
Gemini goes **DIRECTLY to Google** through the google-genai SDK — NOT Cursor's
own key, but the user's own ``gemini_api_key`` (secret_store > env). It is a RICH
text provider: ``thinking_mode`` is HONORED (Gemini 3+ → ``thinking_level``;
skipped for 2.5 and earlier since they reject the field), native function-calling
is ON, and native multimodal image + PDF input (``inline_data``) is supported.
External MCP servers (``mcp_servers.yaml``) ARE reached too: the ``mcp_bridge``
connects to them in-process and their tools join the native FC surface
(``mcp__<server>__<tool>`` decls converted to Gemini ``function_declarations``) —
Claude/Cursor parity for the external-tool case (the same wiring as ollama/openai).
What it does NOT support: the caller's ``mcp_servers`` CLI dict (Claude/Cursor-specific;
gemini loads its own from yaml via the bridge) and agent-reuse (cursor-specific).
Full-duplex live voice is a SEPARATE surface (Phase 2: ``/ws/voice/live``); this
module is text-only.

google-genai is OPTIONAL: if it is not installed / there is no key,
``make_client`` returns ``None`` and a CLEAR :class:`LLMCallError` is raised here
(never a raw ``ImportError``). That is why this module does NOT import
``google.genai`` at the top level — it obtains the client only via
``gemini_shared``.

``thinking_mode`` is NOW honored (low/medium/high → ``thinking_config`` with
``include_thoughts``) and native function-calling is ON: the model can call
``memory_search`` / ``save_memory`` (the shared ``gemini_tools`` tools) plus any
bridged ``mcp__…`` external tool; the provider dispatches them, appends the response
to contents, and loops until the final text is produced. Native FC carries both the
built-in tools and the bridged MCP tools (converted to Gemini ``function_declarations``).
When the model returns thinking-summary parts (``part.thought``), they are surfaced
as a SEPARATE ``{"thinking": …}`` wire stream (openai/ollama parity) and kept OUT of
the answer — ``chunk.text`` would otherwise merge the reasoning into the reply.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from akana_server.concurrency import off_loop
from akana_server.orchestrator import attachments
from akana_server.orchestrator import base
from akana_server.orchestrator import modes
from akana_server.orchestrator import toolloop
from akana_server.orchestrator.chat_persona import CHAT_SYSTEM_PREFIX
from akana_server.orchestrator.errors import LLMCallError, friendly_provider_error
from akana_server.orchestrator.gemini_shared import genai_installed, make_client
from akana_server.orchestrator.gemini_tools import (
    GEMINI_TOOL_DECLS,
    _function_response,
    dispatch_gemini_tool,
)
from akana_server.orchestrator.mcp_bridge import McpToolBridge, external_mcp_bridge

if TYPE_CHECKING:
    from akana_server.config import Settings

#: Gemini is STATELESS — no server-side session/agent to resume; every call is fresh,
#: so history is always flattened into the prompt (queried via
#: llm_dispatch.provider_capabilities; consumed by chat_context).
CAPABILITIES = base.ProviderCapabilities(stateless=True)

#: Upper bound for the function-calling loop (guards against an infinite tool-call loop).
_MAX_TOOL_ROUNDS = 5

#: akana ``thinking_mode`` → Gemini ``thinking_config`` dict mapping. We supply
#: the SDK ``ThinkingLevel`` enum (MINIMAL/LOW/MEDIUM/HIGH) + ``include_thoughts``
#: fields as a plain dict (the config is already a dict → the SDK coerces it; no
#: need to import the optional ``types``). An unknown value → the medium level
#: (safe default).
#: The Akana canonical level names (hizli/normal/derin/yogun/azami/ultra) are DERIVED
#: from the single ``modes`` source (``modes.tier_map``) → adding a canonical mode is
#: one edit there, and the drift guard keeps every provider in sync. The English SDK
#: level names (low/medium/high/minimal) are kept as direct pass-through aliases on
#: top. An unknown value still falls to the safe MEDIUM (see ``_thinking_config``).
#: "ultra" is claude/fable-only (the "ultracode" prompt keyword); on gemini it tops out
#: at the existing HIGH tier.
_THINKING_LEVELS: dict[str, str] = {
    # English SDK level names (direct).
    "low": "LOW",
    "medium": "MEDIUM",
    "high": "HIGH",
    "minimal": "MINIMAL",
    # Akana canonical level names, derived from the shared tier table.
    **modes.tier_map(low="LOW", medium="MEDIUM", high="HIGH"),
}


def _client(settings: Settings) -> Any:
    """Get a ``genai.Client`` — a CLEAR ``LLMCallError`` if unusable (no raw blowup).

    When ``make_client`` returns ``None`` we distinguish the cause and give an
    actionable message: is the SDK not installed, or is the key missing."""
    client = make_client(settings)
    if client is not None:
        return client
    if not genai_installed():
        raise LLMCallError(
            "Gemini is unavailable — the google-genai SDK is not installed. "
            "Install: pip install -r requirements-gemini.txt",
            status_code=503,
        )
    raise LLMCallError(
        "No Gemini API key configured — enter gemini_api_key under "
        "Settings → Identity (or the GEMINI_API_KEY environment variable).",
        status_code=503,
    )


def _resolve_gemini_model(settings: Settings) -> str:
    """The concrete NATIVE gemini model name for the call (persist setting > env > default).

    The dispatch ``model`` argument (the provider-agnostic *cursor* tag that
    chat_producer passes to ALL providers — e.g. ``composer-2`` / ``default`` / or
    even a Cursor-routed ``gemini-3-flash``) NEVER reaches here: gemini always
    uses its own ``gemini_model`` setting. (A stricter version of ollama's
    foreign-tag guard, because gemini names syntactically collide with cursor
    aliases.)"""
    from akana_server.llm_context import load_effective_llm_settings
    from akana_server.llm_settings import resolve_gemini_model_tag

    return resolve_gemini_model_tag(
        settings, load_effective_llm_settings(settings.data_dir, settings)
    )


def _contents(
    history: list[dict[str, str]] | None, user_text: str
) -> list[dict[str, Any]]:
    """history + the latest user turn → a google-genai ``contents`` list.

    Gemini roles are ``user`` / ``model`` (NOT ``assistant``), and there is NO
    ``system`` role in contents — the system directive goes to
    ``config.system_instruction`` (see :func:`_config`). So past ``system`` turns
    (if any) are not downgraded to ``user``, nor skipped — in practice history
    carries only user/assistant; assistant → model, the rest is treated as
    user."""
    contents: list[dict[str, Any]] = []
    for h in history or []:
        if not isinstance(h, dict):
            continue
        text = str(h.get("content") or "")
        if not text:
            continue
        role = "model" if str(h.get("role")) == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": text}]})
    contents.append({"role": "user", "parts": [{"text": user_text}]})
    return contents


#: Gemini ``inline_data`` total request size is capped at ~20MB; we leave headroom
#: and cut off at 18MB (the shared ``attachments`` default). An attachment that would
#: exceed this CUMULATIVE budget is SILENTLY skipped — not sending that image/PDF is
#: better UX than breaking the turn with Google's 400 "request too large" error (for
#: large files we may switch to the Files API later). Kept as a module-level name so
#: the budget can be monkeypatched in tests.
_MAX_INLINE_TOTAL_BYTES = attachments.MAX_INLINE_TOTAL_BYTES


def _add_turn_images(
    contents: list[dict[str, Any]], settings: Settings, file_ids: list[str] | None
) -> None:
    """Add this turn's image/PDF attachments to the last user turn as ``inline_data``.

    Gemini is NATIVE multimodal: uploaded images AND PDFs (``file_ids``) are read
    from the UploadStore and embedded into the last ``user`` content's ``parts``
    → the model REALLY sees the content (unlike cursor/claude's text
    ``image_block`` approach; this is Gemini's strength). The selection + cumulative
    budget + defensive-skip logic is shared with openai via
    ``attachments.iter_embeddable_attachments`` (CONTRACT: only images and PDFs are
    embedded; unreadable / not-image-or-PDF / disabled / over-budget attachments are
    silently skipped and a single error does not break the turn); only the final part
    SHAPE (``inline_data``) is gemini-specific. Only the gemini text surface calls this
    (voice + other providers don't pass ``file_ids`` → no-op)."""
    if not file_ids or not contents:
        return
    parts = contents[-1].setdefault("parts", [])
    for rec, data in attachments.iter_embeddable_attachments(
        settings, file_ids, max_total_bytes=_MAX_INLINE_TOTAL_BYTES
    ):
        parts.append({"inline_data": {"mime_type": rec.media_type, "data": data}})


def _supports_thinking_level(model: str) -> bool:
    """Does the model accept the ``thinking_level`` field (Gemini 3+ only)?

    CRITICAL: ``thinking_level`` (LOW/MEDIUM/HIGH) is valid ONLY on Gemini 3+
    models. Gemini 2.5 and earlier REJECT it → ``400 INVALID_ARGUMENT: 'Thinking
    level is not supported for this model.'`` → the chat breaks with "Gemini
    returned an error: {…}". (2.5 uses an int ``thinking_budget``; but even if we
    don't send anything, the model does its own default dynamic thinking → safe.)
    So if it is not 3+, we send NO thinking_config at all. Names without a version
    number (``gemini-flash-latest``) are accepted on the safe side: no thinking is
    sent.

    E.g.: gemini-3-flash-preview / gemini-3.5-flash → True; gemini-2.5-flash /
    gemini-2.0-flash / gemini-flash-latest → False."""
    name = model.lower().rsplit("/", 1)[-1]  # drop the 'models/' prefix
    match = re.match(r"gemini-(\d+)", name)
    return bool(match) and int(match.group(1)) >= 3


def _thinking_config(thinking_mode: str | None, model: str = "") -> dict[str, Any] | None:
    """akana ``thinking_mode`` → Gemini ``thinking_config`` dict (``None`` if empty).

    If empty/None, returns ``None`` → ``thinking_config`` is NOT added to the
    config at all. If the model does NOT support ``thinking_level`` (Gemini 2.5
    and earlier), it also returns ``None`` → the 400 is avoided and the model uses
    its default thinking. Only when it is Gemini 3+ AND the mode is set,
    ``thinking_level`` (LOW/MEDIUM/HIGH/MINIMAL) + ``include_thoughts``; an unknown
    mode value falls to the medium level (safe)."""
    mode = (thinking_mode or "").strip().lower()
    if not mode or not _supports_thinking_level(model):
        return None
    level = _THINKING_LEVELS.get(mode, "MEDIUM")
    return {"thinking_level": level, "include_thoughts": True}


def _gemini_decls_from_bridge(decls: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """``bridge.decls`` (OpenAI shape) → Gemini ``function_declarations`` entries.

    The MCP bridge emits OpenAI-format decls
    (``{"type":"function","function":{name,description,parameters}}``); Gemini's
    ``function_declarations`` wants the INNER object only
    (``{name,description,parameters}``). Each decl's ``function`` body is taken as-is.
    DEFENSIVE: a malformed entry (not a dict / no name) is skipped so one bad bridged
    server cannot break the config (mirrors the bridge's own per-tool tolerance)."""
    out: list[dict[str, Any]] = []
    for d in decls or []:
        fn = d.get("function") if isinstance(d, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            out.append(fn)
    return out


def _config(
    system_prompt: str | None,
    thinking_mode: str | None = None,
    model: str = "",
    bridge_decls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The ``GenerateContentConfig`` dict: system_instruction + tools (+ thinking).

    We supply a plain dict; google-genai coerces it to ``GenerateContentConfig``
    → no need to import the optional SDK's ``types`` module. ``tools`` is ALWAYS
    added (native function-calling: memory_search + save_memory + any bridged
    ``mcp__…`` external tool — ``bridge_decls`` converted to Gemini
    ``function_declarations``); ``system_instruction`` only if set;
    ``thinking_config`` only if ``thinking_mode`` is set AND the model supports
    ``thinking_level`` (Gemini 3+) — otherwise 2.5 and earlier would give '400
    Thinking level is not supported' (see _thinking_config)."""
    decls = list(GEMINI_TOOL_DECLS) + _gemini_decls_from_bridge(bridge_decls)
    cfg: dict[str, Any] = {"tools": [{"function_declarations": decls}]}
    sp = (system_prompt or "").strip()
    if sp:
        cfg["system_instruction"] = sp
    tc = _thinking_config(thinking_mode, model)
    if tc is not None:
        cfg["thinking_config"] = tc
    return cfg


def _config_no_tools(config: dict[str, Any]) -> dict[str, Any]:
    """A copy of the ``_config`` output WITHOUT ``tools`` (for the final tool-less completion).

    On the FINAL call made when the round limit is exhausted and the model called
    a tool on EVERY round, ``tools`` is DELIBERATELY removed → the model can no
    longer call a tool and is FORCED to answer with text (instead of an empty
    answer; the openai ``complete_once(..., no tools)`` counterpart). Other fields
    like system_instruction + thinking_config are PRESERVED."""
    return {k: v for k, v in config.items() if k != "tools"}


def _usage_dict(
    totals: dict[str, int] | None,
    tool_calls: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The running token TOTAL → an Akana tokens dict (the claude/ollama/openai shape).

    It now takes the running ``{"prompt_tokens":.., "completion_tokens":..}`` total
    dict accumulated across the loop (see :func:`_accumulate_usage`), NOT a single
    ``usage_metadata`` object — so the tokens of intermediate tool rounds in
    function-calling also enter the final usage (previously ``usage_metadata`` was
    OVERWRITTEN each round → intermediate-round tokens were dropped). ``tool_calls``
    carries the round's start/end tool-call wire records (parity with ollama/claude →
    the audit ledger + UI cards) — empty when the turn called no tool. Gemini does
    not return a cost and the price table is Anthropic-specific → ``cost_usd`` is
    NOT added."""
    totals = totals or {}
    return {
        "prompt_tokens": base.coerce_token_count(totals.get("prompt_tokens")),
        "completion_tokens": base.coerce_token_count(totals.get("completion_tokens")),
        "tool_calls": list(tool_calls or []),
    }


def _accumulate_usage(
    totals: dict[str, int], usage_metadata: Any
) -> dict[str, int]:
    """Add one round's ``usage_metadata`` to the running TOTAL (no overwrite →
    intermediate-round tokens are NOT lost).

    In the function-calling loop every ``generate_content``/``..._stream`` call is
    billed SEPARATELY (including intermediate tool rounds); storing only the latest
    round's ``usage_metadata`` (old ``usage_metadata = um``) would drop intermediate
    prompt/completion tokens. Here ``prompt_token_count`` + ``candidates_token_count``
    are accumulated across all rounds — the sum is correct. ``base.coerce_token_count``
    safely coerces to int (a malformed/missing SDK field does not crash the
    accumulation); if ``um`` is None this is a no-op."""
    if usage_metadata is None:
        return totals
    totals["prompt_tokens"] += base.coerce_token_count(
        getattr(usage_metadata, "prompt_token_count", None)
    )
    totals["completion_tokens"] += base.coerce_token_count(
        getattr(usage_metadata, "candidates_token_count", None)
    )
    return totals


def _chunk_text(chunk: Any) -> str | None:
    """Safely extract text from a stream chunk.

    ``chunk.text`` may warn / return None on a chunk that contains a non-text part
    (e.g. a future function-call); wrapped defensively — in Phase 1 (no tools) it
    is always text in practice."""
    try:
        return chunk.text
    except Exception:  # pragma: no cover - defensive (non-text part)
        return None


def _split_parts_text(obj: Any) -> tuple[str, str, bool]:
    """Split a chunk/response into ``(answer_text, thought_text, saw_text_parts)``.

    With ``include_thoughts`` the SDK puts the model's thinking summary in parts whose
    ``thought`` flag is truthy; the answer is in the other text parts. ``chunk.text`` /
    ``resp.text`` would MERGE the two — so we scan ``candidates[0].content.parts`` and keep
    them apart (thoughts feed a separate ``thinking`` stream, never the answer).
    ``saw_text_parts`` is False when the object exposes no text parts (older SDK shape / a
    fake client) so the caller can fall back to the ``.text`` shortcut."""
    answer: list[str] = []
    thought: list[str] = []
    saw = False
    for cand in (getattr(obj, "candidates", None) or [])[:1]:  # only the first candidate, like the SDK
        content = getattr(cand, "content", None)
        for part in getattr(content, "parts", None) or []:
            txt = getattr(part, "text", None)
            if not txt:
                continue
            saw = True
            if getattr(part, "thought", False):
                thought.append(txt)
            else:
                answer.append(txt)
    return "".join(answer), "".join(thought), saw


def _chunk_answer_thought(chunk: Any) -> tuple[str, str]:
    """A stream chunk → ``(answer_text, thought_text)``, keeping thoughts OUT of the answer.

    DEFENSIVE: a chunk that exposes no text parts (older shape / fake client) falls back to
    the ``chunk.text`` shortcut as answer-only (no thoughts) — preserving Phase-1 behavior."""
    answer, thought, saw = _split_parts_text(chunk)
    if not saw:
        return (_chunk_text(chunk) or ""), ""
    return answer, thought


def _response_text(resp: Any) -> str:
    """Concatenated ANSWER text from a one-shot response (thought parts EXCLUDED; None → '').

    The non-stream path has no separate thinking channel, so a leaked thought part would
    dump the model's reasoning into the reply. Non-thought parts are the answer; if no text
    parts are exposed (fake client) we fall back to the ``resp.text`` shortcut."""
    answer, _thought, saw = _split_parts_text(resp)
    if saw:
        return answer
    try:
        return resp.text or ""
    except Exception:  # pragma: no cover - defensive
        return ""


# --- Native function-calling helpers ----------------------------------
#
# Function-calls are read from the response DEFENSIVELY: first the SDK's ready-made
# ``resp.function_calls`` property, then a scan of
# ``candidates[0].content.parts`` for ``part.function_call`` (for fake
# clients / shape differences).


def _extract_function_calls(resp: Any) -> list[Any]:
    """Collect function-call objects from the response ([] = no tools = round finished)."""
    fcs = getattr(resp, "function_calls", None)
    if fcs:
        return list(fcs)
    return [getattr(p, "function_call") for p in _fc_signature_parts(resp)]


def _fc_signature_parts(resp: Any) -> list[Any]:
    """Raw parts inside ``candidates[0].content.parts`` that CARRY a function_call.

    CRITICAL (Gemini 3 "thinking"): these parts carry a ``thought_signature``
    (thinking signature). The ``resp.function_calls`` shortcut DROPS this signature
    — so when appending the model's call round back to history we read the raw parts
    here to preserve the signature. Without it, the next request returns 400
    INVALID_ARGUMENT ("function call is missing a thought signature") → the chat
    breaks with "Gemini returned an error: {…}"."""
    out: list[Any] = []
    for cand in (getattr(resp, "candidates", None) or [])[:1]:  # only the first candidate, like the SDK does
        content = getattr(cand, "content", None)
        for part in getattr(content, "parts", None) or []:
            if getattr(part, "function_call", None) is not None:
                out.append(part)
    return out


def _fc_call_content(fcs: list[Any], parts: list[Any] | None = None) -> Any:
    """Append the model's call round back into ``contents`` (``role=model`` function_call).

    CRITICAL: a function_call part in Gemini 3 "thinking" models carries a
    ``thought_signature`` that MUST be preserved when feeding it back to the model;
    otherwise a re-call returns 400 INVALID_ARGUMENT. So when raw SDK parts
    (``parts``) are available we wrap them AS-IS in ``types.Content`` (signature +
    order are preserved exactly — Google's recommended
    ``contents.append(response.candidates[0].content)`` pattern). If the SDK is absent
    / a fake part arrives we fall back to a plain dict but still copy
    ``thought_signature``. If there are no raw parts at all (a client that only
    exposes the ``function_calls`` property) we build from ``fcs`` (no signature on
    that path).

    RECONCILIATION: in a stream some calls may have arrived ONLY from the
    ``function_calls`` property (no raw part) → ``fcs`` can be LONGER than ``parts``.
    These extra calls are dispatched and answered with a ``function_response``, but if
    they are not added to the model round, the call/response count mismatches → the
    next request returns 400. So for the surplus ``fcs[len(parts):]``, dict-form
    function_call parts are appended (without a signature)."""
    if parts:
        leftover = [_fc_dict_part(fc) for fc in fcs[len(parts):]]
        try:
            from google.genai import types

            return types.Content(role="model", parts=list(parts) + leftover)
        except Exception:  # pragma: no cover - SDK absent / fake part → fall back to dict
            pass
        dict_parts: list[dict[str, Any]] = []
        for p in parts:
            fc = getattr(p, "function_call", None)
            if fc is None:
                continue
            part: dict[str, Any] = _fc_dict_part(fc)
            sig = getattr(p, "thought_signature", None)
            if sig is not None:
                part["thought_signature"] = sig
            dict_parts.append(part)
        dict_parts.extend(leftover)
        return {"role": "model", "parts": dict_parts}
    return {"role": "model", "parts": [_fc_dict_part(fc) for fc in fcs]}


def _fc_dict_part(fc: Any) -> dict[str, Any]:
    """Convert a function_call object into a plain dict part (no signature; no-raw-part path)."""
    return {
        "function_call": {
            "name": getattr(fc, "name", "") or "",
            "args": dict(getattr(fc, "args", None) or {}),
        }
    }


def _fc_response_content(responses: list[Any]) -> dict[str, Any]:
    """Append tool results to ``contents`` (``role=user`` function_response).

    ``_function_response`` returns ``types.FunctionResponse`` if the SDK is installed,
    otherwise a plain dict; both are wrapped into a ``function_response`` part."""
    parts = [{"function_response": r} for r in responses]
    return {"role": "user", "parts": parts}


def _fc_name_args(fc: Any) -> tuple[str, dict[str, Any]]:
    """Single function-call → (name, args dict) — DEFENSIVE coercion of ``fc.args``.

    Gemini supplies ``args`` as a dict-like; a non-mapping payload falls back to an
    empty dict so the tool still runs and the turn is not broken. Shared by
    ``_dispatch_calls`` (dispatch) and ``_tool_call_events`` (wire cards) so both read
    the SAME parsed args."""
    name = getattr(fc, "name", "") or ""
    raw_args = getattr(fc, "args", None) or {}
    try:
        return name, dict(raw_args)
    except (TypeError, ValueError):
        return name, {}


async def _dispatch_calls(
    settings: Settings,
    conversation_id: str | None,
    fcs: list[Any],
    bridge: McpToolBridge,
) -> list[tuple[Any, str]]:
    """Dispatch each function-call → ``(FunctionResponse, result_text)`` pairs (bridge-aware).

    Routes by name: a bridged ``mcp__…`` tool is awaited on its in-process MCP session
    (already async); a native tool (``memory_search``/``save_memory``/vault) goes through
    the synchronous ``dispatch_gemini_tool`` off-loaded to a worker thread (sqlite/file
    side effects). Both paths are DEFENSIVE (every error converts to clean text) → the
    turn is not broken. The raw result text is surfaced ALONGSIDE the wrapped
    ``FunctionResponse`` so the caller can both (a) feed the response back to the model
    and (b) populate the ``tool_call`` wire event's ``result`` (UI card + audit) — the
    wrapped FunctionResponse alone would hide the text. Calls are dispatched in order
    (sequential await) so the result list lines up with ``fcs`` for the wire events."""
    out: list[tuple[Any, str]] = []
    for fc in fcs:
        name, args = _fc_name_args(fc)
        if bridge.handles(name):
            result = await bridge.dispatch(name, args)
        else:
            result = await off_loop(dispatch_gemini_tool, settings, conversation_id, name, args)
        out.append((_function_response(fc, result), result))
    return out


def _tool_call_events(fcs: list[Any], results: list[str]) -> list[dict[str, Any]]:
    """Wire ``tool_call`` events for a tool round (start + end) — the ollama/openai shape.

    Extracts the gemini-specific ``(id, name, args)`` per call (``FunctionCall`` may carry
    an ``id``; an index-based synthetic id is the fallback so start/end still pair) and
    hands them to the shared :func:`toolloop.tool_call_events` builder. ``results`` lines
    up with ``fcs`` (the ``_dispatch_calls`` pairs are dispatched in order)."""
    calls: list[tuple[str, str, dict[str, Any]]] = []
    for idx, fc in enumerate(fcs):
        name, args = _fc_name_args(fc)
        cid = getattr(fc, "id", None) or f"gemini-tool-{idx}"
        calls.append((cid, name, args))
    return toolloop.tool_call_events(calls, list(results))


def _as_llm_error(exc: Exception) -> LLMCallError:
    """google-genai ``APIError`` (or any exception) → ``LLMCallError``.

    Because the SDK is optional we duck-type instead of importing
    ``google.genai.errors``: reading ``code`` (HTTP status) + ``message``.
    ``friendly_provider_error`` translates auth/rate-limit/timeout subtypes from
    status+message into a user-facing string."""
    raw_status = getattr(exc, "code", None)
    if raw_status is None:
        raw_status = getattr(exc, "status_code", None)
    try:
        status_int = int(raw_status) if raw_status is not None else None
    except (TypeError, ValueError):
        status_int = None
    message = getattr(exc, "message", None) or str(exc)
    http_status = status_int if (status_int and 400 <= status_int <= 599) else 502
    return LLMCallError(
        friendly_provider_error(message, provider="gemini", status=status_int),
        status_code=http_status,
    )


async def _with_timeout(settings: Settings, awaitable):
    """Await a google-genai call under the SAME wall-clock ceiling the other providers enforce.

    BUGFIX: ``generate_content`` / ``generate_content_stream`` had no
    ``asyncio.wait_for`` and the text client is built with no ``http_options``
    timeout, so a stalled connection (Google holds the stream open without the
    terminal chunk) hung the turn far past the user's LLM ceiling — every other
    provider bounds this (cursor via ``base.total_timeout``; ollama/openai via their
    driver timeout). We reuse ``base.total_timeout`` (``min(bridge_timeout,
    llm_total_timeout)``) and map a timeout to ``LLMCallError(504)`` exactly like the
    cursor path's ``LLM_TIMEOUT`` contract. A non-positive timeout is the documented
    "disabled / no ceiling" sentinel (combine_cap → 0), so we await unbounded then —
    NOT ``wait_for(0)``, which would fire instantly (mirrors the claude reader guard)."""
    timeout = base.total_timeout(settings)
    if not timeout or timeout <= 0:
        return await awaitable
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout)
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise LLMCallError(
            "LLM_TIMEOUT: gemini request timed out", status_code=504
        ) from exc


#: Sentinel returned by the ``__anext__`` wrapper when the stream is exhausted.
#: ``StopAsyncIteration`` MUST NOT cross ``asyncio.wait_for`` — raised inside a
#: coroutine it is converted to ``RuntimeError`` (see the streaming_tts note) — so
#: normal exhaustion is translated to this sentinel and stops the loop cleanly.
_STREAM_DONE = object()


async def _iter_stream(settings: Settings, stream: Any) -> AsyncIterator[Any]:
    """Yield stream chunks under the SAME inter-chunk idle ceiling ollama/openai get.

    BUGFIX: :func:`_with_timeout` bounds only the awaitable that RETURNS the async
    iterator (stream creation), NOT the per-chunk ``await`` in ``async for``. The
    genai text client is built with no http read timeout, so a mid-stream stall
    (Google holds the connection open without the terminal chunk, AFTER the first
    chunk) would hang the turn unbounded — the exact scenario :func:`_with_timeout`'s
    docstring claims to fix but which happens during iteration. Here EACH
    ``__anext__`` is bounded by ``base.idle_timeout`` (``min(bridge_timeout,
    llm_idle_timeout)``), re-established after every chunk so only REAL silence
    between two chunks trips it — a slow-but-progressing stream is never cut (the
    ollama/openai per-read ``timeout=300`` counterpart; the claude/bridge_pool
    idle-read pattern). A non-positive ceiling is the documented "disabled" sentinel
    (combine_cap → 0), so we await unbounded then — NOT ``wait_for(0)``, which fires
    instantly. A stall maps to ``LLMCallError(504)`` like ``_with_timeout``."""
    timeout = base.idle_timeout(settings)
    iterator = stream.__aiter__()

    async def _anext_or_done() -> Any:
        try:
            return await iterator.__anext__()
        except StopAsyncIteration:
            return _STREAM_DONE

    while True:
        if timeout and timeout > 0:
            try:
                chunk = await asyncio.wait_for(_anext_or_done(), timeout=timeout)
            except (TimeoutError, asyncio.TimeoutError) as exc:
                raise LLMCallError(
                    "LLM_TIMEOUT: gemini stream stalled", status_code=504
                ) from exc
        else:
            chunk = await _anext_or_done()
        if chunk is _STREAM_DONE:
            return
        yield chunk


async def stream_user_chat(
    settings: Settings,
    user_text: str,
    *,
    history: list[dict[str, str]] | None = None,
    model: str | None = None,  # gemini: ignored (cursor tag; _resolve is used instead)
    conversation_id: str | None = None,  # gemini: used for scope in tool dispatch
    agent_id: str | None = None,  # gemini: ignored (no agent-reuse)
    reuse_agent: bool = True,  # gemini: ignored
    mcp_servers: dict[str, Any] | None = None,  # gemini: CLI dict ignored; MCP tools load from yaml via the bridge
    system_prompt: str | None = None,
    thinking_mode: str | None = None,
    file_ids: list[str] | None = None,  # gemini: NATIVE vision input (inline_data)
    plan_mode: bool = False,  # gemini: accepted-and-ignored (claude-only ExitPlanMode)
    auto_continue: bool = False,  # gemini: accepted-and-ignored (claude-only continuation)
) -> AsyncIterator[dict[str, Any]]:
    """Translate the google-genai stream into Akana wire events ({delta|done|usage}).

    Native function-calling (STREAMING): stream the round; accumulate incoming
    ``function_call`` parts. If the round called a tool we do NOT emit text (it is an
    intermediate round), dispatch tools off-loop, append the call+response to contents,
    and start a NEW ``generate_content_stream`` — until the model produces final text
    without calling a tool (at most ``_MAX_TOOL_ROUNDS`` rounds). The round budget +
    suppress-intermediate buffering + thinking replay + tool_call wire events + the
    round-limit forced-final-completion all live in the shared ``toolloop`` engine
    (openai/ollama parity); the ``_GeminiStreamAdapter`` owns the google-genai chunk
    reading (signed function_call parts, thought split). DEFENSIVE: tool results are
    text, the turn is not broken."""
    client = _client(settings)
    model_name = _resolve_gemini_model(settings)
    contents = _contents(history, user_text)
    _add_turn_images(contents, settings, file_ids)
    # Tokens from ALL rounds are ACCUMULATED (not overwritten): each API call is
    # billed separately → intermediate tool rounds also enter the final usage.
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0}
    all_tool_calls: list[dict[str, Any]] = []  # start/end records → done.usage.tool_calls (audit)
    # Default persona → system_prompt=None; fall back to CHAT_SYSTEM_PREFIX so the tool-use directives reach the model (claude/cursor parity).
    effective_system = system_prompt or CHAT_SYSTEM_PREFIX
    try:
        async with external_mcp_bridge(settings) as bridge:
            # Native FC surface + any bridged ``mcp__…`` external tools (empty yaml → no-op).
            config = _config(effective_system, thinking_mode, model_name, bridge.decls)
            adapter = _GeminiStreamAdapter(
                settings, client, model_name, contents, config, usage_totals, bridge,
                conversation_id,
            )
            async for ev in toolloop.run_stream_loop(
                adapter,
                max_rounds=_MAX_TOOL_ROUNDS,
                on_tool_call=all_tool_calls.append,
            ):
                yield ev
    except LLMCallError:
        raise
    except Exception as exc:  # google-genai APIError / network / configuration error
        raise _as_llm_error(exc) from exc
    # Canonical terminal event (base.stream_done_event): tool_calls at the top level,
    # text="" because the whole answer was streamed as ``delta`` events (the aggregator
    # falls back to the accumulated deltas). Gemini has no agent reuse → no agent_id.
    # ``usage`` keeps its own tool_calls copy (unchanged shape) for any usage reader.
    yield base.stream_done_event(
        usage=_usage_dict(usage_totals, all_tool_calls),
        tool_calls=all_tool_calls,
    )


class _GeminiStreamAdapter:
    """``toolloop.ToolLoopAdapter`` for the Gemini streaming surface.

    One round is a ``generate_content_stream`` pass (bounded by the wall-clock ceiling
    on creation + the inter-chunk idle ceiling on iteration); text/thought parts are
    split so intermediate reasoning never bleeds into the answer. ``RoundResult.pending``
    carries the ``(function_calls, signature_parts)`` pair so the thought_signature
    survives the re-call. The round-limit final round is a tool-less
    ``generate_content_stream`` (``_config_no_tools``), unified with openai/ollama."""

    def __init__(
        self, settings, client, model_name, contents, config, usage_totals, bridge,
        conversation_id,
    ) -> None:
        self._settings = settings
        self._client = client
        self._model = model_name
        self._contents = contents
        self._config = config
        self._usage = usage_totals
        self._bridge = bridge
        self._conversation_id = conversation_id

    async def run_round(self) -> toolloop.RoundResult:
        result = toolloop.RoundResult()
        pending_calls: list[Any] = []
        pending_parts: list[Any] = []  # raw function_call parts (carry thought_signature)
        round_usage: Any = None  # this round's usage_metadata (accumulated at round end)
        # google-genai async stream: AWAIT the coroutine → get an async iterator.
        # BUGFIX: bound stream CREATION with the wall-clock ceiling (_with_timeout
        # → 504); bound ITERATION with the inter-chunk idle ceiling (_iter_stream →
        # 504) so a mid-stream stall (a stall AFTER the first chunk, before the
        # terminal chunk) can't hang the turn either.
        stream = await _with_timeout(
            self._settings,
            self._client.aio.models.generate_content_stream(
                model=self._model, contents=self._contents, config=self._config
            ),
        )
        async for chunk in _iter_stream(self._settings, stream):
            # Prefer raw parts (signed); fall back to the property shortcut
            # (fake client / unsigned stream). Single source → no double-counting.
            new_parts = _fc_signature_parts(chunk)
            if new_parts:
                pending_parts.extend(new_parts)
                pending_calls.extend(getattr(p, "function_call") for p in new_parts)
            else:
                pending_calls.extend(_extract_function_calls(chunk))
            answer, thought = _chunk_answer_thought(chunk)
            if thought:
                # include_thoughts: the model's thinking summary — a SEPARATE wire
                # stream (rendered apart; must NOT bleed into the answer). Emitted
                # every round, so intermediate-round reasoning is visible too.
                result.has_thinking = True
                result.thinking_deltas.append(
                    {"thinking": {"phase": "delta", "text": thought}}
                )
            if answer:
                # Buffer answer text (the engine replays it only on the final round):
                # if this round calls a tool the intermediate reasoning must not bleed
                # into the final answer.
                result.answer_deltas.append({"delta": answer, "done": False})
            um = getattr(chunk, "usage_metadata", None)
            if um is not None:
                round_usage = um  # last valid usage frame (cumulative per round)
        _accumulate_usage(self._usage, round_usage)  # accumulate ONCE per round
        result.pending = (pending_calls, pending_parts) if pending_calls else None
        return result

    async def dispatch_and_append(self, pending: Any) -> list[dict[str, Any]]:
        pending_calls, pending_parts = pending
        dispatched = await _dispatch_calls(
            self._settings, self._conversation_id, pending_calls, self._bridge
        )
        responses = [d[0] for d in dispatched]
        results = [d[1] for d in dispatched]
        events = _tool_call_events(pending_calls, results)
        self._contents.append(_fc_call_content(pending_calls, pending_parts))
        self._contents.append(_fc_response_content(responses))
        return events

    async def run_final_round(self) -> AsyncIterator[dict[str, Any]]:
        # Round limit reached: the model is STILL calling tools. Force one final tool-less
        # stream (``_config_no_tools``): the model can no longer call a tool and is FORCED
        # to answer with text from the full contents (unified with openai/ollama).
        # BUGFIX: same wall-clock ceiling on creation AND inter-chunk idle ceiling on
        # iteration for the final tool-less stream.
        final_stream = await _with_timeout(
            self._settings,
            self._client.aio.models.generate_content_stream(
                model=self._model, contents=self._contents,
                config=_config_no_tools(self._config),
            ),
        )
        final_usage: Any = None
        final_thinking = False
        async for chunk in _iter_stream(self._settings, final_stream):
            answer, thought = _chunk_answer_thought(chunk)
            if thought:
                final_thinking = True
                yield {"thinking": {"phase": "delta", "text": thought}}
            if answer:
                yield {"delta": answer, "done": False}
            um = getattr(chunk, "usage_metadata", None)
            if um is not None:
                final_usage = um
        if final_thinking:
            yield {"thinking": {"phase": "completed"}}
        _accumulate_usage(self._usage, final_usage)


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
    file_ids: list[str] | None = None,  # gemini: NATIVE vision input (inline_data)
    plan_mode: bool = False,  # gemini: accepted-and-ignored (claude-only)
) -> tuple[str, str, dict[str, Any]]:
    """One-shot completion → (text, status, raw) — the ``ollama_provider.complete_chat`` shape.

    Native function-calling: if the response contains a function-call, each one is
    dispatched off-loop, the call+response is appended to contents, and
    ``generate_content`` is called again until the model produces final text without
    calling a tool (at most ``_MAX_TOOL_ROUNDS`` rounds). DEFENSIVE: tool results are
    text → the turn is not broken.

    When the round limit is exhausted (the model called a tool on EVERY round and
    produced no text), the old behaviour returned an empty answer (``""``) — the user
    would see an empty bubble. Instead, a single FINAL completion is made after the
    loop: ``_config_no_tools`` deliberately omits ``tools`` → the model can no longer
    call a tool and is FORCED to respond with text (unified with the openai/ollama/
    streaming path via ``toolloop``). Tokens are ACCUMULATED across all rounds (not
    overwritten; each API call is billed separately)."""
    client = _client(settings)
    model_name = _resolve_gemini_model(settings)
    contents = _contents(history, user_text)
    _add_turn_images(contents, settings, file_ids)
    # Tokens from ALL rounds are ACCUMULATED (not overwritten): intermediate tool
    # rounds also enter the final usage (each generate_content call is billed separately).
    usage_totals = {"prompt_tokens": 0, "completion_tokens": 0}
    all_tool_calls: list[dict[str, Any]] = []  # start/end records → usage.tool_calls (audit)
    # Default persona → system_prompt=None; in chat context fall back to CHAT_SYSTEM_PREFIX (claude parity).
    effective_system = system_prompt or (CHAT_SYSTEM_PREFIX if chat_mode else None)
    try:
        async with external_mcp_bridge(settings) as bridge:
            # Native FC surface + any bridged ``mcp__…`` external tools (empty yaml → no-op).
            config = _config(effective_system, thinking_mode, model_name, bridge.decls)
            adapter = _GeminiCompleteAdapter(
                settings, client, model_name, contents, config, usage_totals, bridge,
                conversation_id,
            )
            text = await toolloop.run_complete_loop(
                adapter,
                max_rounds=_MAX_TOOL_ROUNDS,
                on_tool_call=all_tool_calls.append,
            )
    except LLMCallError:
        raise
    except Exception as exc:
        raise _as_llm_error(exc) from exc
    return text, "finished", _usage_dict(usage_totals, all_tool_calls)


class _GeminiCompleteAdapter:
    """``toolloop.CompleteLoopAdapter`` for the Gemini one-shot surface.

    One round is a ``generate_content`` pass (bounded by the wall-clock ceiling); the
    thought parts are excluded from the answer. ``complete_round`` returns
    ``(answer_text, (function_calls, signature_parts))`` — the signature parts preserve
    the thought_signature on the re-call. The round-limit final round is a tool-less
    ``generate_content`` (``_config_no_tools``), unified with the streaming/openai/ollama
    path."""

    def __init__(
        self, settings, client, model_name, contents, config, usage_totals, bridge,
        conversation_id,
    ) -> None:
        self._settings = settings
        self._client = client
        self._model = model_name
        self._contents = contents
        self._config = config
        self._usage = usage_totals
        self._bridge = bridge
        self._conversation_id = conversation_id

    async def complete_round(self) -> tuple[str, Any]:
        # BUGFIX: bound the one-shot with the wall-clock ceiling (→ 504).
        resp = await _with_timeout(
            self._settings,
            self._client.aio.models.generate_content(
                model=self._model, contents=self._contents, config=self._config
            ),
        )
        _accumulate_usage(self._usage, getattr(resp, "usage_metadata", None))
        fcs = _extract_function_calls(resp)
        if not fcs:
            return _response_text(resp), None  # no tool → final answer
        sig_parts = _fc_signature_parts(resp)  # to preserve thought_signature
        return "", (fcs, sig_parts)

    async def dispatch_and_append(self, pending: Any) -> list[dict[str, Any]]:
        fcs, sig_parts = pending
        dispatched = await _dispatch_calls(
            self._settings, self._conversation_id, fcs, self._bridge
        )
        responses = [d[0] for d in dispatched]
        results = [d[1] for d in dispatched]
        events = _tool_call_events(fcs, results)
        self._contents.append(_fc_call_content(fcs, sig_parts))
        self._contents.append(_fc_response_content(responses))
        return events

    async def complete_final_round(self) -> str:
        # Round limit reached and still no text: final tool-less completion (the model is
        # forced to answer with text when it can no longer call tools) → no empty answer.
        # BUGFIX: same wall-clock ceiling on the final tool-less completion.
        final = await _with_timeout(
            self._settings,
            self._client.aio.models.generate_content(
                model=self._model, contents=self._contents,
                config=_config_no_tools(self._config),
            ),
        )
        _accumulate_usage(self._usage, getattr(final, "usage_metadata", None))
        return _response_text(final)


__all__ = ["complete_chat", "stream_user_chat"]
