"""Drive the local ``claude`` CLI (Claude Code, subscription auth — NO API key).

This is a drop-in alternative to :mod:`.llm_dispatch`. It mirrors the public
async API (``stream_user_chat`` async generator + ``complete_chat``) and the
event dict shapes so a dispatcher can call either provider interchangeably.

The ``claude`` CLI is invoked with ``--output-format stream-json --verbose
--include-partial-messages``, emitting NDJSON (one JSON object per line). We
translate those objects into Akana wire events:

  - {"agent_id": "<session_id>"}        (once, from system/init)
  - {"delta": "<chunk>", "done": False}
  - {"tool_call": {...}}                        (start / end phases)
  - {"done": True, "usage": {...}, "text": "...", "status": "...",
     "tool_calls": [...]}                        (exactly one, terminal)

Auth is forced to subscription OAuth by stripping ``ANTHROPIC_*`` API-key env
vars before spawning, so the CLI reads ``~/.claude/.credentials.json``.

``LLMCallError`` comes from the leaf :mod:`.errors` module (the canonical
cross-provider error home), so this provider imports it at module load with no
circular dependency on :mod:`.llm_dispatch`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
import uuid
from collections.abc import AsyncIterator
from typing import Any

from akana_server.config import LEGACY_ENV_PREFIX, Settings
from akana_server.orchestrator import base
from akana_server.orchestrator.chat_persona import CHAT_SYSTEM_PREFIX
from akana_server.orchestrator.claude_continuation import (
    _CONTINUE_INSTRUCTION,
    run_with_continuation,
)
from akana_server.orchestrator.claude_events import ClaudeEventTranslator
from akana_server.orchestrator.errors import LLMCallError
from akana_server.orchestrator.claude_protocol import (  # noqa: F401 — re-exported for callers/tests
    _ASK_CLOSE,
    _ASK_OPEN,
    _AskBlockStripper,
    _coerce_tool_input,
    _extract_ask_block,
    _extract_assistant_text,
    _marker_overlap,
    _normalize_ask_user,
    _normalize_plan,
    _normalize_todo,
    _strip_ask_block,
)
from akana_server.orchestrator.llm_process import (
    executable_argv,
    needs_cmd_wrapper,
    register_llm_process,
    release_llm_process,
    terminate_process_group,
)

log = logging.getLogger(__name__)

#: Claude supports session resume (``claude --resume <session-id>``) — a stored
#: session id lets the model keep the full conversation, so history is NOT re-sent
#: when one is present. Hence ``stateless=False`` (queried via
#: llm_dispatch.provider_capabilities; consumed by chat_context).
CAPABILITIES = base.ProviderCapabilities(stateless=False)

# claude stream-json lines can include large tool payloads (default readline cap is 64 KiB).
# Canonical value lives in :mod:`.base`; alias kept for existing references.
CLAUDE_STDOUT_LINE_LIMIT = base.STDOUT_LINE_LIMIT

#: Tools the agent must NEVER run from the chat/voice channel (filesystem,
#: shell, web, sub-agents). Read/Grep/Glob are added for non-chat mode.
_BASE_DISALLOWED = [
    "Bash",
    "Edit",
    "Write",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Task",
    "KillShell",
    "SlashCommand",
]
_READONLY_TOOLS = ["Read", "Grep", "Glob"]

#: The built-in tool with which Claude asks the user a multiple-choice question.
#: It is INTERACTIVE-ONLY: in headless (``-p``) mode it is absent from the init
#: ``tools`` list, so the model's call is rejected with ``tool_result is_error:
#: "No such tool available: AskUserQuestion. AskUserQuestion exists but is not
#: enabled in this context."`` — and the rejected tool's ``input`` arrives as an
#: UNPARSED JSON string (not a dict). Akana catches the ``tool_use`` regardless of
#: the reject text and converts it into a structured ``ask_user`` event (see
#: :func:`_normalize_ask_user`, which coerces the string input); when the user
#: sends their choice as the next message, the session resumes via ``--resume``.
#: (Older CLIs auto-rejected with ``is_error:"Answer questions?"`` and a dict
#: input — both shapes are still handled.)
_ASK_USER_TOOL = "AskUserQuestion"

#: The built-in tool with which Claude, in plan mode (``--permission-mode
#: plan``), presents the plan and waits for the user's approval. Like
#: AskUserQuestion it is interactive-only — absent from the headless ``-p``
#: ``tools`` list — so the model's call is rejected ("No such tool available …
#: not enabled in this context") with the input delivered as an UNPARSED JSON
#: string. Akana catches the ``tool_use`` and converts it into a structured
#: ``plan`` event (see :func:`_normalize_plan`, which coerces the string input);
#: when the user says "Apply", the session resumes via ``--resume`` with plan mode
#: OFF and applies the plan. (Older CLIs auto-rejected with ``is_error:"Exit plan
#: mode?"`` and a dict input — both shapes are still handled.)
_EXIT_PLAN_TOOL = "ExitPlanMode"

#: The built-in tool the model uses to maintain a live checklist across a turn. Surfaced as a
#: turn-level ``todo`` progress event IN ADDITION to the normal tool card (NOT a turn boundary).
_TODO_TOOL = "TodoWrite"
#: The built-in tool that spawns a subagent. Surfaced as ``subagent`` start/end boundary events so
#: the UI can group the subagent's own nested tool steps (which carry ``parent_id`` = the Task id).
_TASK_TOOL = "Task"

_KNOWN_MODEL_ALIASES = {"sonnet", "opus", "haiku"}
_DEFAULT_CLAUDE_MODEL = "claude-sonnet-4-6"

#: ThinkingMode → native ``--effort`` level. Instead of forcing
#: ``MAX_THINKING_TOKENS`` by hand, we pass the claude CLI's own effort flag: the
#: model allocates its thinking budget by level itself (future-proof, no
#: magic-number rot). An unknown/None mode → the flag is not added at all (the
#: CLI default is preserved).
#:
#: The KEYS are the canonical mode names from :data:`modes.THINKING_MODES` (the
#: single source ``chat_producer`` chooses from). claude keeps its OWN mapping
#: table here rather than deriving it from :func:`modes.tier_map`: the shared
#: ``tier_map`` exposes only three tiers (low/medium/high), but claude honours the
#: CLI's finer 5-level scale (low/medium/high/xhigh/max), so ``derin``/``yogun``/
#: ``azami`` map to DISTINCT levels here where the three-level providers collapse
#: them all to ``high``. A drift guard (:func:`test_claude_provider` →
#: ``test_effort_levels_cover_canonical_modes``) asserts these keys stay exactly
#: :data:`modes.THINKING_MODES`, so adding a new canonical mode can't silently
#: leave claude behind.
#:
#: "normal" targets ``medium`` (the CLI's own everyday default) and "derin" now
#: means something distinct from it (``high``) — before this mapping, "normal"
#: and "derin" both resolved to ``high``, making the "derin" tier a no-op.
#: "ultra" is the 6th tier (fable-persona only — see ``_ULTRACODE_KEYWORD``
#: below): it maps to the same ``max`` level as "azami" here; the extra
#: "ultracode" keyword (fable models only) is what makes it stronger, not the
#: effort flag itself.
_EFFORT_LEVELS = {
    "hizli": "low",
    "normal": "medium",
    "derin": "high",
    "yogun": "xhigh",
    "azami": "max",
    "ultra": "max",
}

#: Prompt keyword that opts Claude Code into its own multi-agent orchestration
#: mode. This is NOT a CLI flag/value (``--effort`` has no "ultracode" level) —
#: it is appended to the PROMPT TEXT sent to the CLI, and only when BOTH:
#:   (1) thinking_mode == "ultra", AND
#:   (2) the resolved ``--model`` tag is a "fable" persona model.
#: Non-fable models with thinking_mode=="ultra" still get ``--effort max`` (via
#: _EFFORT_LEVELS above) but NOT the keyword — "ultra" degrades to behaving like
#: "azami" for them.
_ULTRACODE_KEYWORD = " ultracode"


def _effort_level(thinking_mode: str | None) -> str | None:
    """Map a ThinkingMode tag to a native ``--effort`` level (None = leave off)."""
    return _EFFORT_LEVELS.get((thinking_mode or "").strip())


def _is_fable_model(model_tag: str) -> bool:
    """Is the resolved ``--model`` tag a "fable" persona model (case-insensitive)?"""
    return "fable" in (model_tag or "").strip().lower()


def _apply_ultracode_keyword(
    prompt: str, thinking_mode: str | None, resolved_model: str
) -> str:
    """Append the ``ultracode`` keyword to the CLI-bound prompt text (fable + ultra only).

    This runs AFTER history/turn framing (:func:`_build_prompt`) and BEFORE the
    prompt reaches ``_build_args``/the CLI argv — it never touches the persisted
    user turn (``body.text`` written by ``_persist_user_once`` in chat_producer)
    or the episodic/memory store, both of which are built from the ORIGINAL user
    text, not from this provider-local prompt string.
    """
    if (thinking_mode or "").strip() != "ultra":
        return prompt
    if not _is_fable_model(resolved_model):
        return prompt
    return f"{prompt}{_ULTRACODE_KEYWORD}"


def _idle_timeout(settings: Settings) -> float:
    """Stream idle-hang ceiling: ``min(claude_bridge_timeout, llm_idle_timeout)``.

    If the claude CLI stops producing response/tool output and hangs (network
    stall, frozen child process), the turn ends cleanly with «LLM_TIMEOUT» after
    this interval instead of 30 min. Each line read resets the counter → a slow
    but progressing stream does not trigger it. Same ``base.combine_cap`` logic
    as llm_dispatch (0 = disabled, never loosen).
    """
    from akana_server.runtime_settings import get_runtime

    base_timeout = float(get_runtime("claude_bridge_timeout", settings))
    return base.combine_cap(
        base_timeout, float(get_runtime("llm_idle_timeout", settings))
    )


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _settings_claude_model(settings: Settings) -> str:
    """Fallback tag: persisted llm settings (dashboard) win over env settings."""
    try:
        from akana_server.llm_context import load_effective_llm_settings
        from akana_server.llm_settings import (
            resolve_claude_model_tag,
        )

        return resolve_claude_model_tag(
            settings, load_effective_llm_settings(settings.data_dir, settings)
        )
    except Exception:  # settings-like test doubles / unreadable file → env
        return (getattr(settings, "claude_model", "") or "").strip()


def _resolve_claude_model(settings: Settings, model: str | None) -> str:
    """Pick a concrete model tag for ``--model``.

    A caller-supplied ``claude-*`` id or a bare alias (sonnet/opus/haiku) is
    used as-is; anything else (e.g. a cursor ``composer-*`` tag arriving
    through the provider dispatch) can NEVER leak to the claude CLI — it falls
    back to the persisted dashboard choice, then env, then the default.
    """
    tag = (model or "").strip()
    if tag.startswith("claude-"):
        return tag
    if tag in _KNOWN_MODEL_ALIASES:
        return tag
    fallback = _settings_claude_model(settings) or _DEFAULT_CLAUDE_MODEL
    if tag:
        log.warning(
            "claude provider: foreign model tag %r not passed to the claude CLI → %s",
            tag,
            fallback,
        )
    return fallback


def _claude_env(settings: Settings) -> dict[str, str]:
    """A copy of ``os.environ`` with API-key vars stripped.

    Removing ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_BASE_URL`` /
    ``ANTHROPIC_AUTH_TOKEN`` forces the CLI onto subscription OAuth. If the
    runtime secret-store holds a ``claude setup-token`` output, it is exported
    as ``CLAUDE_CODE_OAUTH_TOKEN`` so the CLI can auth without a login flow.

    Akana-side secrets are stripped too: the claude process (and the MCP
    subprocesses it spawns) has no business seeing the Cursor API key or the
    server bearer token.

    Thinking effort is now passed via the native ``--effort`` flag, not through
    env (see :func:`_build_args`).
    """
    env = dict(os.environ)
    for key in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "CURSOR_API_KEY",
        "AKANA_TOKEN",
        # Foreign-provider keys must not ride into the spawned claude CLI + its MCP
        # subprocesses (mirrors the _bridge_env denylist on the cursor path).
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        LEGACY_ENV_PREFIX + "TOKEN",  # don't leak even if the old name is still in the environment
    ):
        env.pop(key, None)
    data_dir = getattr(settings, "data_dir", None)
    if data_dir is not None:
        try:
            from akana_server.secret_store import get_secret

            token = get_secret(data_dir, "claude_oauth_token")
            if token:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        except Exception:  # pragma: no cover - store unreadable → plain env
            pass
    return env


def _resolve_prompt_language(settings: Settings) -> str:
    """Active prompt language (``en`` | ``tr``) from the runtime ``language``
    setting; any failure → ``"en"`` (English-first default).

    Delegates to the canonical :func:`runtime_settings.resolve_language` — language
    resolution is a past bug source here (stuck-language incidents), so this path must
    not carry its own copy that a future fix (e.g. a third language) could miss. The
    module-level name is kept because :mod:`claude_continuation` imports it."""
    from akana_server.runtime_settings import resolve_language

    return resolve_language(settings)


#: History-framing wrapper around prior turns — bilingual, follows the active
#: ``language`` (en|tr). The label leaks into EVERY multi-turn prompt, so a
#: hardcoded language here biases replies toward that language regardless of the
#: user's setting (the bug: Turkish frame → Turkish replies in English mode).
_HISTORY_FRAME = {
    "en": ("[Previous conversation — context only; do not continue/re-answer]", "[/Previous conversation]"),
    "tr": ("[Önceki konuşma — yalnızca bağlam; sürdürme/yeniden yanıtlama]", "[/Önceki konuşma]"),
}


def _build_prompt(
    user_text: str, history: list[dict[str, str]] | None, language: str = "en"
) -> str:
    """Flatten chat history + the new turn into one prompt string.

    Prior turns are wrapped in a delimited *context* block so the model reads
    them as past background, not a transcript to keep writing; the new turn
    follows as the single message to answer. Without this framing the bare
    ``User:``/``Akana:`` alternation invites runaway self-dialogue — the model
    finishes its answer then fabricates further ``User:``/``Akana:`` turns. The
    system prompt also forbids that continuation. The framing label follows the
    active ``language`` (en|tr) so it does not bias the reply language.

    Mirrors ``cursor_bridge/lib.mjs`` ``buildUserMessage`` (minus the System
    block — the system prompt is passed via ``--append-system-prompt``).
    """
    past: list[str] = []
    for m in history or []:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if not isinstance(content, str):
            continue
        role = m.get("role")
        label = "Akana" if role == "assistant" else "User" if role == "user" else "System"
        past.append(f"{label}: {content}")
    if not past:
        return user_text
    block = "\n".join(past)
    open_tag, close_tag = _HISTORY_FRAME.get(language, _HISTORY_FRAME["en"])
    return f"{open_tag}\n{block}\n{close_tag}\n\n{user_text}"


def _history_for_prompt(
    history: list[dict[str, str]] | None, *, resuming: bool
) -> list[dict[str, str]] | None:
    """History to flatten into the prompt — empty when resuming a live session.

    Mirrors ``cursor_bridge/bridge_daemon.mjs`` ``historyForAgent``: when the
    claude CLI resumes a session (``--resume <id>``) it already holds every
    prior turn as proper role-separated messages, so re-flattening the server's
    history window into the prompt would (a) double-feed those turns (wasted
    input tokens), and (b) re-introduce the ``User:``/``Akana:`` transcript the
    model is tempted to keep writing. Resume → only the new turn; a fresh
    session (no resume) → full history to bootstrap context.

    Safe because :func:`get_agent_id` is provider-scoped: ``--resume`` is
    only added when the stored session id belongs to the active provider, so a
    resumed claude session always contains this conversation's prior turns
    (first turn / provider-switch / reset → id is ``None`` → ``resuming`` False →
    full bootstrap).
    """
    return None if resuming else history


def _full_tools_enabled(settings: Settings) -> bool:
    """Is full capability (bypassPermissions) enabled?

    Source: the ``claude_full_tools`` setting persisted in the dashboard
    (default ON; the ``AKANA_CLAUDE_FULL_TOOLS`` env is also folded in here). For
    settings-like test doubles whose settings file is unreadable, it falls back
    to the env flag.
    """
    try:
        from akana_server.llm_settings import (
            load_llm_settings,
            resolve_claude_full_tools,
        )

        return resolve_claude_full_tools(
            settings, load_llm_settings(settings.data_dir, settings)
        )
    except Exception:  # settings-like test doubles / unreadable file → env
        return os.environ.get("AKANA_CLAUDE_FULL_TOOLS", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }


def _tool_flags(
    full_tools: bool,
    mcp_servers: dict[str, Any] | None,
    plan_mode: bool = False,
) -> list[str]:
    """Build the permission-mode + allow/disallow CLI flags.

    Akana is a full-capability assistant: ``mcp__<server>`` (memory) + the
    read-only trio (``Read Grep Glob``) are allowed in every mode. When
    ``full_tools`` is on (the default — see :func:`_full_tools_enabled`) the CLI
    runs with ``--permission-mode bypassPermissions`` and nothing is blocked, so
    write/shell run unsupervised. When off, ``_BASE_DISALLOWED`` (Bash/Edit/Write
    …) is blocked and the mode drops to ``default``.

    ``plan_mode`` (per-turn, user-triggered) overrides BOTH: the mode becomes
    ``plan`` — the CLI only researches/reads, performs no write/shell, and calls
    ``ExitPlanMode`` to present the plan. In plan mode the CLI itself blocks
    writes, so ``--disallowedTools`` is unnecessary (not added).
    """
    allowed: list[str] = [f"mcp__{name}" for name in (mcp_servers or {})]
    allowed.extend(_READONLY_TOOLS)

    disallowed = [] if (full_tools or plan_mode) else list(_BASE_DISALLOWED)

    if plan_mode:
        mode = "plan"
    elif full_tools:
        mode = "bypassPermissions"
    else:
        mode = "default"
    flags: list[str] = ["--permission-mode", mode]
    if allowed:
        flags += ["--allowedTools", ",".join(allowed)]
    if disallowed:
        flags += ["--disallowedTools", ",".join(disallowed)]
    return flags


def _mcp_config_has_env(mcp_servers: dict[str, Any] | None) -> bool:
    """Whether any MCP server carries a non-empty ``env`` block.

    A server ``env`` can hold the vault master key (``AKANA_VAULT_KEY``, forwarded to the
    ``akana_vault`` child). Inlined into ``--mcp-config <json>`` on argv, that key becomes
    visible in ``ps`` / ``/proc/<pid>/cmdline`` to every local user for the life of each
    claude subprocess. When True, the config JSON is spilled to a 0600 temp file instead
    (see ``_build_args``), so the secret never rides the command line.
    """
    for server in (mcp_servers or {}).values():
        env = server.get("env") if isinstance(server, dict) else None
        if isinstance(env, dict) and any(str(v).strip() for v in env.values()):
            return True
    return False


class _ClaudeSpill:
    """Holds the per-turn temp files used ONLY on the Windows ``cmd /c`` path.

    When ``claude`` is an npm ``.cmd`` shim it must launch via ``cmd /c``, which
    re-parses the command line (``%VAR%`` expansion, ``&|<>^`` metacharacters,
    quote toggling) — so arbitrary/user content must NOT ride on the argv. The
    prompt goes to stdin; the system prompt and MCP config go to these temp files
    (``--append-system-prompt-file`` / ``--mcp-config <file>``). All are removed in
    the stream's ``finally``. POSIX/``.exe`` never construct one (``spill is None``).
    """

    def __init__(self) -> None:
        self._dir = tempfile.mkdtemp(prefix="akana-claude-")

    def write(self, label: str, text: str) -> str:
        path = os.path.join(self._dir, f"{label}.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        # The mcp spill can hold the vault master key; keep the file owner-only (0600) so
        # it never widens past the 0700 mkdtemp dir. No-op-ish on Windows (chmod maps to
        # read-only), best-effort — the temp dir already restricts access.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path

    def cleanup(self) -> None:
        shutil.rmtree(self._dir, ignore_errors=True)


def _build_args(
    settings: Settings,
    prompt: str,
    *,
    model: str | None,
    chat_mode: bool,
    agent_id: str | None,
    reuse_agent: bool,
    mcp_servers: dict[str, Any] | None,
    system_prompt: str | None = None,
    thinking_mode: str | None = None,
    plan_mode: bool = False,
    continue_sentinel: bool = False,
    spill: _ClaudeSpill | None = None,
    cmd_wrapper: bool = False,
) -> list[str]:
    """Assemble the full argv for ``claude`` (no shell).

    ``cmd_wrapper`` is the Windows ``cmd /c`` path: the prompt is delivered via stdin
    (``-p`` with no positional → claude reads stdin) and the arbitrary system-prompt /
    MCP-config strings are spilled to temp files instead of riding on the cmd.exe-reparsed
    command line. POSIX/``.exe`` keeps the prompt positional and the system prompt inline —
    byte-for-byte the original argv — EXCEPT the MCP config, which is spilled to a 0600
    temp file whenever it carries a secret ``env`` block (``spill`` present), so the vault
    master key never appears in ``ps``/``/proc``. ``spill`` is the temp-file holder; it is
    present for the cmd-wrapper path OR the env-bearing MCP config, so a file sink exists.
    """
    args: list[str] = [settings.claude_bin, "-p"]
    if not cmd_wrapper:
        # POSIX / ``.exe``: prompt is a positional arg (unchanged).
        args.append(prompt)
    args += [
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--model",
        _resolve_claude_model(settings, model),
    ]
    effort = _effort_level(thinking_mode)
    if effort:
        args += ["--effort", effort]
    if reuse_agent and agent_id:
        args += ["--resume", agent_id]
    # WI-2 agent-work-mode: the caller can supply its own system prompt (skill
    # body + pack persona); if none is supplied, the classic persona is used in
    # chat mode.
    effective_system = system_prompt or (CHAT_SYSTEM_PREFIX if chat_mode else None)
    # Auto-continue: append the continuation directive (emit the sentinel when done)
    # ON TOP of whatever the effective system prompt is — so it rides over the
    # persona / skill body rather than replacing it.
    if continue_sentinel:
        instr = _CONTINUE_INSTRUCTION.get(
            _resolve_prompt_language(settings), _CONTINUE_INSTRUCTION["en"]
        )
        effective_system = f"{effective_system}\n\n{instr}" if effective_system else instr
    if effective_system:
        # System prompt spills to a file only on the cmd path (cmd.exe reparse hazard);
        # POSIX/``.exe`` keep it inline even when a spill exists for the MCP config.
        if cmd_wrapper:
            args += ["--append-system-prompt-file", spill.write("system", effective_system)]
        else:
            args += ["--append-system-prompt", effective_system]
    if mcp_servers:
        config = json.dumps({"mcpServers": mcp_servers})
        if spill is None:
            args += ["--mcp-config", config]
        else:
            # ``--mcp-config`` accepts a file path OR inline JSON. Spill to a 0600 file when
            # a spill exists — on the cmd path the JSON (quotes, possible ``%``) must not hit
            # cmd.exe, and on ANY platform an ``env`` block may hold the vault master key,
            # which must not appear inline on argv (``ps``/``/proc`` leak).
            args += ["--mcp-config", spill.write("mcp", config)]
    # Only ever use the MCP servers Akana passes in — never the ones registered at
    # user/project/local Claude Code scope (``~/.claude.json``, ``.mcp.json`` in the
    # workspace). Without this, a browser (or any) MCP the user added out-of-band
    # would ride into every turn and survive an Akana pack disable, since Akana only
    # manages ``<data_dir>/mcp_servers.yaml``. Added even when ``mcp_servers`` is
    # empty so a turn with no Akana servers inherits nothing.
    args += ["--strict-mcp-config"]
    args += _tool_flags(_full_tools_enabled(settings), mcp_servers, plan_mode=plan_mode)
    return args


def _claude_cwd(settings: Settings, chat_mode: bool) -> str:
    """Working directory for the claude CLI — ALWAYS the real project workspace.

    The old chat sandbox (``<data_dir>/agent_chat``, no codebase access) is gone:
    Akana is one unified agent that works directly in the user's project, so a
    coding request reaches the actual repo instead of an empty sandbox. ``chat_mode``
    no longer selects the cwd (it only gates the persona); the parameter is kept for
    signature stability across providers.
    """
    return str(settings.workspace)


def _usage_to_tokens(
    usage: dict[str, Any] | None, cost_usd: float | None = None
) -> dict[str, Any]:
    """Normalize claude ``result.usage`` → Akana tokens dict.

    Token counts are safely coerced to int with ``base.coerce_token_count``:
    claude stream-json is external input; a malformed/float-string token field
    must not swallow the ``done`` event and crash the whole turn (same strictness
    as llm_dispatch).

    ``cost_usd`` is claude's ``result.total_cost_usd`` (a SIBLING of usage, not
    inside it) — when supplied and >0 it is added to the dict as ``cost_usd``;
    the frontend shows it as "$0.0123" in the meta line.
    """
    cost = base.coerce_cost_usd(cost_usd)
    if isinstance(usage, dict):
        out = {
            "prompt_tokens": base.coerce_token_count(usage.get("input_tokens")),
            "completion_tokens": base.coerce_token_count(usage.get("output_tokens")),
            "tool_calls": [],
            "cache_read_tokens": base.coerce_token_count(
                usage.get("cache_read_input_tokens")
            ),
            "cache_write_tokens": base.coerce_token_count(
                usage.get("cache_creation_input_tokens")
            ),
        }
    else:
        out = {"prompt_tokens": 0, "completion_tokens": 0, "tool_calls": []}
    if cost > 0:
        out["cost_usd"] = cost
    return out


async def _read_line(reader: asyncio.StreamReader, timeout: float) -> bytes:
    """Read one NDJSON line, tolerating large tool payloads.

    The reader's buffer ``limit`` is raised at subprocess-creation time
    (:data:`CLAUDE_STDOUT_LINE_LIMIT`); a truncated final line without a
    trailing newline still surfaces via :class:`asyncio.IncompleteReadError`.
    """

    async def _read() -> bytes:
        try:
            return await reader.readuntil(b"\n")
        except asyncio.IncompleteReadError as e:
            return bytes(e.partial)

    # ``combine_cap`` yields 0 to mean "no idle ceiling" (disabled, e.g.
    # ``CLAUDE_BRIDGE_TIMEOUT=0``); passing 0 straight to ``wait_for`` would
    # instead time out INSTANTLY → every stream dies on the first read. Map a
    # non-positive value to "wait indefinitely" (the per-turn timeout still bounds
    # the whole run elsewhere).
    if timeout and timeout > 0:
        return await asyncio.wait_for(_read(), timeout=timeout)
    return await _read()


_AUTH_USER_MESSAGE = (
    "Could not authenticate the Claude session — run `claude login` in the terminal "
    "or enter claude_oauth_token under Settings → Identity."
)


def _classify_claude_failure(
    *,
    result_error: dict[str, Any] | None,
    stderr_text: str,
    model_tag: str,
) -> str:
    """Map a failed claude run to a user-facing message.

    Distinguishes authentication (401/403), a stale ``--resume`` session, an
    unknown model, and the ``error_max_turns`` and ``error_during_execution``
    subtypes; as a last resort it falls back to the meaningful portion of
    result/stderr.
    """
    result_msg = ""
    api_status: Any = None
    result_subtype = ""
    if isinstance(result_error, dict):
        result_msg = str(result_error.get("result") or "").strip()
        api_status = result_error.get("api_error_status")
        result_subtype = str(result_error.get("subtype") or "").strip()
    combined = f"{result_msg}\n{stderr_text}".lower()

    if api_status in (401, 403) or "failed to authenticate" in combined or (
        "authentication" in combined and ("invalid" in combined or "expired" in combined)
    ) or "oauth token has expired" in combined:
        return _AUTH_USER_MESSAGE
    if "no conversation found with session id" in combined:
        return (
            "Claude could not find the previous session (provider switch/stale session) — "
            "send the message again; if the problem persists, start a new chat."
        )
    if api_status == 404 or ("model" in combined and "not_found" in combined):
        return (
            f"Claude model not found: {model_tag} — "
            "select a valid Claude model under Settings → Provider."
        )
    # B1: error_max_turns — Claude reached the maximum allowed number of turns
    if result_subtype == "error_max_turns" or "max_turns" in combined:
        return (
            "Claude reached the maximum tool-round limit — "
            "break the task into smaller steps or try again."
        )
    # B1: error_during_execution — an unexpected error during task execution
    if result_subtype == "error_during_execution" or "error_during_execution" in combined:
        return (
            "Claude hit an unexpected error while running the task — "
            "try again; if the problem persists, simplify the task."
        )
    meaningful = result_msg or stderr_text.strip()
    if meaningful:
        return meaningful[:800]
    return "claude run failed"


def _is_stale_claude_resume_failure(
    *,
    result_error: dict[str, Any] | None,
    stderr_text: str,
) -> bool:
    """Was the session_id passed via ``--resume`` not found on the CLI side?"""
    result_msg = ""
    if isinstance(result_error, dict):
        result_msg = str(result_error.get("result") or "").strip()
    combined = f"{result_msg}\n{stderr_text}".lower()
    return "no conversation found with session id" in combined


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
async def _stream_single_run(
    settings: Settings,
    user_text: str,
    *,
    history: list[dict[str, str]] | None = None,
    model: str | None = None,
    conversation_id: str | None = None,
    agent_id: str | None = None,
    reuse_agent: bool = True,
    mcp_servers: dict[str, Any] | None = None,
    chat_mode: bool = True,
    system_prompt: str | None = None,
    thinking_mode: str | None = None,
    plan_mode: bool = False,
    continue_sentinel: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """ONE ``claude`` CLI invocation, translated into Akana wire events.

    Yields (in order): an optional ``{"agent_id": ...}``, then
    ``{"delta": ..., "done": False}``, ``{"thinking": {...}}`` and
    ``{"tool_call": {...}}`` events as they arrive, and finally exactly one
    terminal ``{"done": True, ...}``. ``thinking_mode`` (hizli/normal/derin)
    drives extended-thinking via the native ``--effort`` flag (see
    :func:`_build_args`).

    This is the single-shot core. The public :func:`stream_user_chat` wraps it to
    add the multi-turn auto-continue loop; ``continue_sentinel`` (set by that
    wrapper) appends the continuation directive to the system prompt.
    """
    # If resuming, the session already holds the prior turns → don't re-send the
    # history (double feeding + transcript trigger). Same condition as _build_args.
    resuming = bool(reuse_agent and agent_id)
    prompt = _build_prompt(
        user_text,
        _history_for_prompt(history, resuming=resuming),
        _resolve_prompt_language(settings),
    )
    # "ultra" + fable model → append the ultracode keyword to the CLI-bound prompt
    # text ONLY (never persisted — see _apply_ultracode_keyword docstring).
    prompt = _apply_ultracode_keyword(
        prompt, thinking_mode, _resolve_claude_model(settings, model)
    )
    # NetworkEngine F0: circuit breaker for the claude provider (no retry — stream
    # deltas can't be re-emitted). Checked HERE, before the per-turn temp spill is
    # created, so an open breaker fast-fails WITHOUT leaking a temp dir (the
    # try/finally that cleans the spill starts much further down). Auth errors still
    # raise immediately.
    from akana_server.network import load_network_config
    from akana_server.network.guard import global_registry

    _net_cfg = load_network_config(settings)
    _breaker = None
    if _net_cfg.breaker_enabled:
        # get_or_create binds the threshold/cooldown at creation WITHOUT mutating the
        # shared registry defaults — the old configure()+get() pair changed the shared
        # defaults on every call (concurrent providers flip-flopped them) and never
        # retuned the already-created breaker anyway.
        _breaker = global_registry().get_or_create(
            "claude",
            threshold=_net_cfg.breaker_threshold,
            cooldown=_net_cfg.breaker_cooldown,
        )
        _breaker.before_call()  # BreakerOpenError if open
    # BUG 3 (Windows): an npm ``claude.cmd`` shim can't be exec'd directly — it must
    # run via ``cmd /c``, which re-parses the command line. In that mode spill the prompt
    # to stdin and the system-prompt / MCP-config to temp files so no arbitrary content
    # rides on the cmd.exe-reparsed argv (BatBadBut).
    cmd_wrapper = needs_cmd_wrapper(settings.claude_bin)
    # SECURITY: the MCP config may carry the vault master key in a server ``env`` block.
    # Inlined into ``--mcp-config <json>`` on argv it would be world-readable via ``ps`` /
    # ``/proc/<pid>/cmdline`` for the life of the subprocess. So on ANY platform, spill the
    # config to a 0600 temp file when it has a secret env. POSIX/``.exe`` with a plain
    # (env-less) config → ``spill is None`` and argv/stdin are exactly as before.
    spill = (
        _ClaudeSpill()
        if (cmd_wrapper or _mcp_config_has_env(mcp_servers))
        else None
    )
    try:
        args = _build_args(
            settings,
            prompt,
            model=model,
            chat_mode=chat_mode,
            agent_id=agent_id,
            reuse_agent=reuse_agent,
            mcp_servers=mcp_servers,
            system_prompt=system_prompt,
            thinking_mode=thinking_mode,
            plan_mode=plan_mode,
            continue_sentinel=continue_sentinel,
            spill=spill,
            cmd_wrapper=cmd_wrapper,
        )
        if cmd_wrapper:
            # Resolve ``claude.cmd`` → ``cmd /c <abs path>`` (PATHEXT-aware). POSIX argv is
            # left untouched so the working Linux/macOS path never changes.
            args = executable_argv(args)
        env = _claude_env(settings)
        cwd = _claude_cwd(settings, chat_mode)
        # Hang protection: every line read is bounded by the idle ceiling (a TIGHTER
        # ceiling on top of the existing claude_bridge_timeout; 0 = disabled). On
        # exceed, the existing ``except TimeoutError`` below kills the process group →
        # no new leak.
        timeout = _idle_timeout(settings)

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                # Windows cmd path delivers the prompt via stdin (``-p`` with no positional);
                # POSIX/``.exe`` keep DEVNULL (prompt is a positional arg) — even when a spill
                # exists purely to move the MCP config off argv.
                stdin=(asyncio.subprocess.PIPE if cmd_wrapper else asyncio.subprocess.DEVNULL),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
                limit=CLAUDE_STDOUT_LINE_LIMIT,
                # BUG 1: own process group (pgid == pid) → on shutdown, killpg kills
                # ALL of the claude CLI's child processes (including MCP servers); a
                # bare proc.kill() would leave them orphaned.
                start_new_session=True,
            )
        except (FileNotFoundError, NotADirectoryError) as e:
            raise LLMCallError(
                f"claude CLI not found ({settings.claude_bin}) — "
                "install: npm install -g @anthropic-ai/claude-code",
                status_code=503,
            ) from e
    except BaseException:
        # Anything between spill creation and a live proc (bad shim / AV-locked
        # cmd.exe raising a plain OSError, a disk-full spill.write() in
        # _build_args, an _idle_timeout failure, the FileNotFoundError/
        # NotADirectoryError translated above, …) must still clean up the temp
        # dir (it holds the system prompt / MCP config) and count the failure
        # on the breaker exactly once.
        if _breaker is not None:
            _breaker.record_failure()
        if spill is not None:
            spill.cleanup()
        raise
    assert proc.stdout and proc.stderr  # noqa: S101 - pipes always present

    # BUG 1: pid file — so on a SIGKILL abrupt shutdown the bootstrap reaper can
    # clean up this claude subtree with killpg (best-effort, doesn't break the stream).
    # Registered BEFORE the Windows stdin drain below: on the cmd path the flattened
    # bootstrap prompt can exceed the ~64KB pipe buffer, so ``drain()`` SUSPENDS until
    # the CLI starts reading. A turn cancelled at that await (STOP / client disconnect
    # right at turn start) must still be reapable — if the pid were registered only
    # after the drain, the orphaned claude.cmd process would be invisible to both the
    # shutdown path and the next-boot reaper.
    _proc_token = uuid.uuid4().hex
    register_llm_process(
        getattr(settings, "data_dir", None) or ".", _proc_token, proc.pid, "claude_cli"
    )

    # Windows cmd path: feed the prompt over stdin, then EOF (close). Wrapped so a
    # broken pipe (claude already exited) never crashes the turn — the stream/stderr
    # below surface the real error. Keyed on ``cmd_wrapper``, not the spill: an env-only
    # spill (POSIX/``.exe``) still passes the prompt as a positional arg with DEVNULL stdin.
    #
    # The ``drain()`` can SUSPEND (prompt > pipe buffer, CLI not yet reading). This region
    # is OUTSIDE the main try/finally below, so a CancelledError delivered here (STOP /
    # disconnect at turn start) would otherwise leak the live claude process, its spill
    # temp dir (system prompt / MCP config, possibly the vault key) AND the pid file.
    # Guard it: on ANY exception (incl. CancelledError) kill the process group, release
    # the pid, clean the spill, then re-raise.
    if cmd_wrapper and proc.stdin is not None:
        try:
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError, OSError):  # pragma: no cover
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:  # pragma: no cover
                    pass
        except BaseException as _setup_exc:
            # Cancellation (STOP / disconnect) is not a provider fault → don't taint
            # the breaker (mirrors the main-body handler below); any real error is
            # counted once.
            if _breaker is not None and not isinstance(
                _setup_exc, (asyncio.CancelledError, GeneratorExit)
            ):
                _breaker.record_failure()
            try:
                await terminate_process_group(proc.pid)
            except Exception:  # pragma: no cover - already dead / race
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            release_llm_process(
                getattr(settings, "data_dir", None) or ".", _proc_token
            )
            if spill is not None:
                spill.cleanup()
            raise

    async def _drain_stderr() -> bytes:
        try:
            return await proc.stderr.read()
        except Exception:  # pragma: no cover
            return b""

    stderr_task = asyncio.create_task(_drain_stderr())

    # Event translation + terminal-``done`` accumulation live in a single stateful
    # object (see :mod:`.claude_events`) — this generator owns only the subprocess
    # lifecycle. The translator holds the ~40 accumulators that used to be inline
    # locals here; the wire-event contract is byte-identical.
    tr = ClaudeEventTranslator(_resolve_claude_model(settings, model))

    try:
        try:
            _stop_loop = False
            while True:
                line = await _read_line(proc.stdout, timeout)
                if not line:
                    break
                raw = line.decode("utf-8", errors="replace").strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue

                # Delegate this line to the event translator; it yields ordinary
                # wire events plus two control records: ``{"_terminate": pid}`` (kill
                # the process group — early AskUserQuestion/ExitPlanMode) and
                # ``{"_stop": True}`` (break the read loop; the terminal result event
                # and early-termination both use it). The generator owns the
                # process, so the translator asks us to kill rather than killing.
                async for out in tr.feed(ev, proc.pid):
                    if "_terminate" in out:
                        await terminate_process_group(out["_terminate"])
                        continue
                    if out.get("_stop"):
                        _stop_loop = True
                        continue
                    yield out
                if _stop_loop:
                    break
        except TimeoutError as e:
            # The stream exceeded the idle ceiling (hung). BUG 1: kill the process
            # GROUP (including child MCP processes); a bare proc.kill() would leave
            # them orphaned. finally deletes the pid file.
            await terminate_process_group(proc.pid)
            raise LLMCallError("LLM_TIMEOUT: claude CLI timed out", status_code=504) from e
        except asyncio.LimitOverrunError as e:
            # A single line exceeded 8MiB (huge tool output): a clean error
            # instead of dropping the stream with a generic Exception (same
            # tolerance intent as llm_dispatch/bridge_pool). Kill the process
            # group; finally deletes the pid.
            await terminate_process_group(proc.pid)
            raise LLMCallError(
                "a single line in the claude CLI response exceeded the 8MiB limit",
                status_code=502,
            ) from e

        await proc.wait()

        # Early termination: after AskUserQuestion or ExitPlanMode we killed the
        # process ourselves → returncode won't be zero, but this is NOT AN ERROR;
        # the asked_user/planned flag yields the correct done event.
        if tr.early_terminated:
            tr.result_seen = True  # don't fall into the error path

        if tr.result_error is not None or (
            not tr.result_seen and proc.returncode not in (0, None)
        ):
            err_bytes = await stderr_task
            stderr_text = err_bytes.decode("utf-8", errors="replace").strip()
            if _is_stale_claude_resume_failure(
                result_error=tr.result_error,
                stderr_text=stderr_text,
            ) and resuming:
                from akana_server.chat_context import record_agent_timing_metric
                from akana_server.observability.metrics import registry

                registry.incr("llm_session_resume_failed")
                record_agent_timing_metric("resume_failed")
                log.warning(
                    "claude stale resume session — will retry with history bootstrap "
                    "(conv=%s session=%s)",
                    conversation_id,
                    agent_id,
                )
                yield {"need_history_bootstrap": True}
                return
            msg = _classify_claude_failure(
                result_error=tr.result_error,
                stderr_text=stderr_text,
                model_tag=_resolve_claude_model(settings, model),
            )
            log.warning(
                "claude run failed (rc=%s): %s | stderr: %s",
                proc.returncode,
                (tr.result_error or {}).get("result") or (tr.result_error or {}).get("subtype"),
                stderr_text[:400],
            )
            raise LLMCallError(msg, status_code=503)

        # Flush any text the ask-block stripper held back as a partial-marker tail
        # that never became a block (real prose ending in e.g. "[["), then read the
        # final answer text (see ClaudeEventTranslator.final_text for the
        # delta-vs-fallback selection, including the malformed-block case).
        tr.flush_tail()
        text = tr.final_text()

        if _breaker is not None:
            _breaker.record_success()  # the stream finished cleanly
        # asked_user/planned → the turn is "awaiting user response" (not the normal
        # "finished"); the producer/frontend sees this and persists the question/plan
        # card. When the response (an answer or "Apply") arrives as the next message,
        # the session resumes via --resume. Canonical terminal shape via
        # base.stream_done_event (tool_calls top-level; ask_user/plan optional).
        done_event = base.stream_done_event(
            usage=_usage_to_tokens(tr.usage, tr.cost_usd),
            text=text,
            status="awaiting_user" if (tr.asked_user or tr.planned) else tr.final_status,
            tool_calls=tr.tool_calls,
            ask_user=tr.ask_user_payload,
            plan=tr.plan_payload,
        )
        yield done_event
    except BaseException as _net_exc:  # noqa: BLE001 - report to the breaker, don't swallow
        # Cancellation doesn't taint the breaker; every real error (auth/timeout/
        # run failed) is counted consecutively so a continually crashing provider
        # can open the circuit. A consumer disconnect (GeneratorExit) is likewise
        # not a provider fault — excluded alongside CancelledError so a disconnect
        # burst can't trip/re-open the breaker (mirrors llm_dispatch + network/guard).
        if _breaker is not None and not isinstance(
            _net_exc, (asyncio.CancelledError, GeneratorExit)
        ):
            _breaker.record_failure()
        raise
    finally:
        if not stderr_task.done():
            stderr_task.cancel()
        # BUG 1: kill the process group (claude CLI + child MCP processes) — a
        # bare proc.kill() would leave the grandchildren orphaned. No-op if the
        # process has already finished.
        if proc.returncode is None:
            try:
                await terminate_process_group(proc.pid)
            except Exception:  # pragma: no cover - already dead / race
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        release_llm_process(
            getattr(settings, "data_dir", None) or ".", _proc_token
        )
        # Remove the per-turn system-prompt / MCP-config temp files (present on the Windows
        # cmd path OR when the MCP config was spilled off argv; no-op when ``spill is None``).
        if spill is not None:
            spill.cleanup()


async def stream_user_chat(
    settings: Settings,
    user_text: str,
    *,
    history: list[dict[str, str]] | None = None,
    model: str | None = None,
    conversation_id: str | None = None,
    agent_id: str | None = None,
    reuse_agent: bool = True,
    mcp_servers: dict[str, Any] | None = None,
    chat_mode: bool = True,
    system_prompt: str | None = None,
    thinking_mode: str | None = None,
    plan_mode: bool = False,
    auto_continue: bool = False,
    file_ids: list[str] | None = None,  # claude: accepted-and-ignored (CLI has no native file_ids vision input)
) -> AsyncIterator[dict[str, Any]]:
    """Stream a chat turn — with autonomous multi-turn continuation.

    Thin provider entry point over :func:`claude_continuation.run_with_continuation`. It
    supplies the two provider primitives the wrapper needs — ``_stream_single_run`` (the
    single-shot CLI driver) and ``_resolve_prompt_language`` — read HERE at call time so a
    test monkeypatch of ``claude_provider._stream_single_run`` is honored (ARCH-06: the
    dependency now points one way, provider → continuation, with no back-import). With
    ``auto_continue`` off (the default) it is a transparent single-run passthrough."""
    async for ev in run_with_continuation(
        settings,
        user_text,
        stream_single_run=_stream_single_run,
        resolve_language=_resolve_prompt_language,
        history=history,
        model=model,
        conversation_id=conversation_id,
        agent_id=agent_id,
        reuse_agent=reuse_agent,
        mcp_servers=mcp_servers,
        chat_mode=chat_mode,
        system_prompt=system_prompt,
        thinking_mode=thinking_mode,
        plan_mode=plan_mode,
        auto_continue=auto_continue,
        file_ids=file_ids,
    ):
        yield ev


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
    plan_mode: bool = False,
    file_ids: list[str] | None = None,  # claude: accepted-and-ignored (CLI has no native file_ids vision input)
    auto_continue: bool = False,  # claude: accepted-and-ignored on the one-shot path (streaming-only continuation)
) -> tuple[str, str, dict[str, Any]]:
    """One-shot chat: consume :func:`stream_user_chat`, return (text, status, raw).

    ``raw`` is the terminal done event (carries usage + tool_calls).
    """
    text = ""
    status = "finished"
    raw: dict[str, Any] = {}
    async for ev in stream_user_chat(
        settings,
        user_text,
        history=history,
        model=model,
        conversation_id=conversation_id,
        agent_id=agent_id,
        reuse_agent=reuse_agent,
        mcp_servers=mcp_servers,
        chat_mode=chat_mode,
        system_prompt=system_prompt,
        thinking_mode=thinking_mode,
        plan_mode=plan_mode,
    ):
        if ev.get("done"):
            text = str(ev.get("text") or "")
            status = str(ev.get("status") or "finished")
            raw = ev
    return text, status, raw
