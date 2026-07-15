"""Drive the OpenAI ``codex`` CLI (Codex, ChatGPT-subscription auth — NO API key).

A sixth Akana provider, the CLI twin of :mod:`.claude_provider`: it bridges the
non-interactive ``codex exec`` command the same way claude_provider bridges the
``claude`` CLI. The owner authenticates once with ``codex login`` (ChatGPT sign-in,
stored in ``~/.codex/auth.json``); this provider spawns ``codex exec`` and never
touches an API key. It is DELIBERATELY independent from the ``openai`` provider —
that one is API-key/platform-billed (``OPENAI_API_KEY`` + the platform HTTP API);
this one is subscription-billed through the ChatGPT session.

Protocol (Codex CLI ``exec`` in JSONL mode, verified against v0.144 docs + binary):

    codex exec --json -m <model> [-c <toml-override> ...] \
        (--dangerously-bypass-approvals-and-sandbox | --sandbox read-only) \
        --skip-git-repo-check -C <workspace> -

``--json`` streams newline-delimited JSON events on stdout:

  - ``{"type":"thread.started","thread_id":"<uuid>"}``       (once — our ``agent_id``)
  - ``{"type":"turn.started"}``
  - ``{"type":"item.started|updated|completed","item":{...}}`` with ``item.type`` in
    {``agent_message``, ``reasoning``, ``command_execution``, ``mcp_tool_call``,
    ``file_change``, ``todo_list``, ``error``}
  - ``{"type":"turn.completed","usage":{"input_tokens","cached_input_tokens","output_tokens"}}``
  - ``{"type":"turn.failed","error":{"message":...}}`` / ``{"type":"error","message":...}``

We translate those into the SAME Akana wire events every provider yields:

  - ``{"agent_id": "<thread_id>"}``            (once, from ``thread.started``)
  - ``{"delta": "<chunk>", "done": False}``    (from ``agent_message`` text)
  - ``{"thinking": {...}}``                    (from ``reasoning`` items)
  - ``{"tool_call": {...}}``                   (command_execution / mcp_tool_call / file_change)
  - ``{"todo": {...}}``                        (todo_list items)
  - ``{"done": True, "usage": {...}, "text": "...", "status": "...",
     "tool_calls": [...]}``                    (exactly one, terminal — base.stream_done_event)

RESUME: Codex persists the thread; on the next turn we pass ``codex exec resume
<thread_id> …`` so the model keeps the full conversation (``stateless=False``), exactly
like claude's ``--resume``. The stored id is provider-scoped by chat_context so a cursor/
claude id never leaks into a codex resume.

MCP: Codex configures MCP servers through its TOML config, overridable per-run with
repeatable ``-c mcp_servers.<name>.<key>=<toml-value>`` flags. We wire Akana's
``mcp_servers`` payload (akana_memory / akana_vault / external yaml packs) through so the
memory/vault/pack tools are available. Secret-bearing env values are kept OFF the argv
(they would be world-readable via ``ps``/``tasklist``) and forwarded through the child's
inherited process environment instead — see :func:`_mcp_overrides` / :func:`_codex_env`.

AUTH: API-key env (``CODEX_API_KEY`` / ``OPENAI_API_KEY``) is stripped so the CLI uses the
ChatGPT OAuth session. A missing CLI or a not-logged-in session surfaces as a clear
:class:`LLMCallError` 503 with an actionable ``codex login`` message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

from akana_server.config import LEGACY_ENV_PREFIX, Settings
from akana_server.orchestrator import base, modes
from akana_server.orchestrator.chat_persona import CHAT_SYSTEM_PREFIX
from akana_server.orchestrator.errors import LLMCallError
from akana_server.orchestrator.llm_process import (
    executable_argv,
    needs_cmd_wrapper,
    register_llm_process,
    release_llm_process,
    terminate_process_group,
)

log = logging.getLogger(__name__)

#: Codex supports session resume (``codex exec resume <thread_id>``), so a stored id
#: lets the model keep the full conversation → history is NOT re-sent when one is
#: present. ``stateless=False`` (queried via llm_dispatch.provider_capabilities;
#: consumed by chat_context — same path as claude/cursor).
CAPABILITIES = base.ProviderCapabilities(stateless=False)

#: Codex stdout JSONL lines can carry large tool payloads (command output up to 64 KiB,
#: mcp results) — raise the readline cap far above asyncio's 64 KiB default (canonical
#: value in :mod:`.base`).
CODEX_STDOUT_LINE_LIMIT = base.STDOUT_LINE_LIMIT

#: akana ``thinking_mode`` → Codex ``model_reasoning_effort`` (minimal/low/medium/high/xhigh).
#: The composer sends Codex's OWN native level VERBATIM (minimal…xhigh) — those are the
#: direct pass-through aliases below. The Akana canonical tiers (hizli/normal/derin/yogun/
#: azami/ultra) are ALSO accepted (DERIVED from the shared ``modes.tier_map``, drift-guarded)
#: so a non-native sender still resolves; on the 3-tier map they top out at high. ``xhigh``
#: (extra-high, Codex's max reasoning level) is native-only — no Akana tier maps to it, it is
#: reached solely by the native composer selection. An unknown/empty mode → None (flag
#: omitted, CLI default preserved).
_REASONING_EFFORTS: dict[str, str] = {
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    **modes.tier_map(low="low", medium="medium", high="high"),
}

_KNOWN_MODEL_HINT = "codex"
_DEFAULT_CODEX_MODEL = "gpt-5-codex"


def _reasoning_effort(thinking_mode: str | None) -> str | None:
    """Map a ThinkingMode tag to a Codex ``model_reasoning_effort`` (None = leave off)."""
    return _REASONING_EFFORTS.get((thinking_mode or "").strip().lower())


# --------------------------------------------------------------------------- #
# Model resolution
# --------------------------------------------------------------------------- #
def _settings_codex_model(settings: Settings) -> str:
    """Fallback tag: persisted llm settings (dashboard) win over env."""
    try:
        from akana_server.llm_context import load_effective_llm_settings
        from akana_server.llm_settings import resolve_codex_model_tag

        return resolve_codex_model_tag(
            settings, load_effective_llm_settings(settings.data_dir, settings)
        )
    except Exception:  # settings-like test doubles / unreadable file → env/default
        return (getattr(settings, "codex_model", "") or "").strip()


def _resolve_codex_model(settings: Settings, model: str | None) -> str:
    """Pick a concrete model tag for ``-m``.

    A caller-supplied Codex-family tag (anything containing "codex") is used as-is;
    anything else (a cursor ``composer-*`` tag, ``claude-*``, the plain openai
    ``gpt-5.4`` that arrives through the provider-agnostic dispatch ``model`` kwarg)
    can NEVER leak to the codex CLI — it falls back to the persisted dashboard choice,
    then env, then the default. Mirrors ``claude_provider._resolve_claude_model``'s
    foreign-tag guard (adapted to the codex naming family)."""
    tag = (model or "").strip()
    if tag and _KNOWN_MODEL_HINT in tag.lower():
        return tag
    fallback = _settings_codex_model(settings) or _DEFAULT_CODEX_MODEL
    if tag:
        log.warning(
            "codex provider: foreign model tag %r not passed to the codex CLI → %s",
            tag,
            fallback,
        )
    return fallback


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #
def _codex_env(settings: Settings, mcp_env: dict[str, str]) -> dict[str, str]:
    """A copy of ``os.environ`` with API-key vars stripped + MCP env forwarded.

    Removing ``CODEX_API_KEY`` / ``OPENAI_API_KEY`` forces the CLI onto the ChatGPT
    subscription OAuth session (``~/.codex/auth.json``) rather than API-key billing —
    the whole point of this provider vs. the ``openai`` one. Akana-side and
    foreign-provider secrets are stripped too (the codex process + its MCP children have
    no business seeing them), mirroring the claude env denylist.

    ``mcp_env`` is merged in LAST: the MCP server env dicts are forwarded through the
    child's inherited process environment (Codex stdio MCP servers inherit the parent
    environment) so secret-bearing values (e.g. the vault master key) never have to ride
    the ``-c`` overrides on argv. Non-secret keys are ALSO passed via ``-c`` for
    determinism (see :func:`_mcp_overrides`); this env path is the belt-and-suspenders
    that also covers the secret ones.
    """
    # Auth-defeating keys that MUST NOT survive into the codex process env: the CLI
    # reads OPENAI_API_KEY/CODEX_API_KEY from its own environment and would silently
    # switch from the ChatGPT-subscription OAuth session to API-key billing — the exact
    # costly failure this provider exists to prevent. These are re-stripped AFTER the
    # MCP-env merge below (an external mcp_servers.yaml server commonly forwards its own
    # OPENAI_API_KEY, which would otherwise re-introduce the key we just removed).
    _AUTH_DENYLIST = ("CODEX_API_KEY", "OPENAI_API_KEY")
    env = dict(os.environ)
    for key in (
        *_AUTH_DENYLIST,
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "CURSOR_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "AKANA_TOKEN",
        LEGACY_ENV_PREFIX + "TOKEN",
    ):
        env.pop(key, None)
    # Forward every MCP server env var so a Codex-spawned stdio child inherits it...
    for key, value in mcp_env.items():
        if value:
            env[key] = value
    # ...but NEVER let the merge re-introduce an auth-defeating key. A stdio MCP child
    # that genuinely needs OPENAI_API_KEY shares this one process env with codex, so we
    # cannot both give the child the key AND hide it from codex here — codex's own auth
    # (no key present → subscription session) wins, and a key-needing external MCP tool
    # degrades rather than flipping the whole turn to API billing. Built-in Akana
    # servers (memory/vault/schedule/tasks) forward only AKANA_* vars, so they are
    # unaffected.
    for key in _AUTH_DENYLIST:
        env.pop(key, None)
    return env


# --------------------------------------------------------------------------- #
# Prompt assembly (system prompt + history + turn → one stdin payload)
# --------------------------------------------------------------------------- #
def _resolve_prompt_language(settings: Settings) -> str:
    """Active prompt language (``en`` | ``tr``) — any failure → ``"en"`` (English-first)."""
    from akana_server.runtime_settings import resolve_language

    return resolve_language(settings)


#: History-framing wrapper — bilingual, follows the active ``language`` (en|tr) so it
#: does not bias the reply language (the same fix as claude's _HISTORY_FRAME).
_HISTORY_FRAME = {
    "en": ("[Previous conversation — context only; do not continue/re-answer]", "[/Previous conversation]"),
    "tr": ("[Önceki konuşma — yalnızca bağlam; sürdürme/yeniden yanıtlama]", "[/Önceki konuşma]"),
}


def _frame_history(history: list[dict[str, str]] | None, language: str) -> str:
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
        return ""
    open_tag, close_tag = _HISTORY_FRAME.get(language, _HISTORY_FRAME["en"])
    return f"{open_tag}\n" + "\n".join(past) + f"\n{close_tag}"


def _build_prompt(
    settings: Settings,
    user_text: str,
    *,
    history: list[dict[str, str]] | None,
    system_prompt: str | None,
    chat_mode: bool,
    resuming: bool,
) -> str:
    """Flatten (system prompt + framed history + the new turn) into ONE stdin payload.

    Codex ``exec`` has no ``--append-system-prompt`` channel (claude does), so Akana's
    persona / skill body is prepended to the prompt text. When resuming a live thread the
    prior turns already live in the Codex thread → history is NOT re-flattened (double
    feeding + transcript-continuation trigger); a fresh thread gets the full history to
    bootstrap context (same rule as claude's ``_history_for_prompt``).
    """
    language = _resolve_prompt_language(settings)
    effective_system = system_prompt or (CHAT_SYSTEM_PREFIX if chat_mode else None)
    parts: list[str] = []
    if effective_system:
        parts.append(effective_system.strip())
    frame = "" if resuming else _frame_history(history, language)
    if frame:
        parts.append(frame)
    parts.append(user_text)
    return "\n\n".join(p for p in parts if p)


# --------------------------------------------------------------------------- #
# Sandbox / approvals + MCP config → argv
# --------------------------------------------------------------------------- #
def _full_tools_enabled(settings: Settings) -> bool:
    """Is full capability enabled? Reuses the shared ``claude_full_tools`` dashboard
    setting (default ON) — one "let the agent write/run unsupervised" switch governs
    both CLI providers, so the owner does not configure two independent toggles.
    ``AKANA_CLAUDE_FULL_TOOLS`` is folded in for settings-less test doubles."""
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


def _sandbox_flags(full_tools: bool) -> list[str]:
    """Map Akana's full-tools mode to Codex sandbox/approval flags.

    ``full_tools`` ON (default, the bypassPermissions analogue) →
    ``--dangerously-bypass-approvals-and-sandbox`` ("run every command without approvals
    or sandboxing"). OFF → ``--sandbox read-only`` (the agent may read but performs no
    writes/shell side effects; ``exec`` is non-interactive so it can never prompt for an
    approval, matching claude's write/shell block)."""
    if full_tools:
        return ["--dangerously-bypass-approvals-and-sandbox"]
    return ["--sandbox", "read-only"]


def _toml_value(value: Any) -> str:
    """Encode a Python value as a TOML scalar/array for a ``-c key=<value>`` override.

    Codex parses ``-c`` values as TOML (falling back to a bare string on parse failure).
    ``json.dumps`` of a str/list yields text that is ALSO valid TOML: a JSON string is a
    valid TOML basic string (``\\\\`` / ``\\"`` / ``\\uXXXX`` escapes overlap, and JSON
    never emits the bare ``\\U`` that would trip TOML) — so even a Windows path
    ``C:\\Users\\x`` round-trips correctly, and a JSON string array ``["a","b"]`` is a
    valid TOML array. Bools/ints are emitted as TOML literals."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return json.dumps("" if value is None else str(value))


def _looks_secret_env_key(key: str) -> bool:
    """Whether an MCP env var name likely carries a secret VALUE.

    Secret-bearing values (the vault master key, external-server API keys/tokens) must
    NOT be inlined into a ``-c`` override on argv, where ``ps``/``tasklist`` exposes them
    to every local user for the life of the codex subprocess (the leak claude_provider
    spills to a 0600 temp file to avoid — Codex's ``-c`` mechanism is argv-only, so we
    keep secrets off it entirely and rely on process-env inheritance instead). Anything
    whose name contains KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL is treated as secret."""
    upper = key.upper()
    return any(t in upper for t in ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "CREDENTIAL"))


def _mcp_overrides(mcp_servers: dict[str, Any] | None) -> tuple[list[str], dict[str, str]]:
    """Build the ``-c`` MCP override argv + the env to forward via the process env.

    Returns ``(argv_overrides, process_env)``:

      * ``argv_overrides`` — repeatable ``["-c", "mcp_servers.<name>.command=…", "-c",
        "mcp_servers.<name>.args=[…]", "-c", "mcp_servers.<name>.env.<KEY>=…", …]``. The
        command + args + NON-secret env keys are inlined here (deterministic — memory's
        ``AKANA_DATA_DIR`` works even if Codex does not propagate the process env to the
        child). Codex's ``type`` field is not emitted (stdio is inferred from ``command``).
      * ``process_env`` — EVERY server env var (secret + non-secret), for
        :func:`_codex_env` to merge into the codex subprocess environment so a
        Codex-spawned stdio child inherits it. Secret-bearing values reach the child ONLY
        through this path (never argv).

    LIMITATION (documented): a server whose env carries a secret (e.g. akana_vault's
    ``AKANA_VAULT_KEY``) therefore works only if Codex forwards its process environment to
    the stdio MCP child — the common MCP convention, but unverified here without a live
    login. Memory tools (non-secret env) work unconditionally. ``AKANA_VAULT_TOOLS=0``
    sidesteps the vault path entirely.
    """
    overrides: list[str] = []
    process_env: dict[str, str] = {}
    for name, cfg in (mcp_servers or {}).items():
        if not isinstance(cfg, dict):
            continue
        command = cfg.get("command")
        if command:
            overrides += ["-c", f"mcp_servers.{name}.command={_toml_value(command)}"]
        args = cfg.get("args")
        if isinstance(args, (list, tuple)) and args:
            overrides += ["-c", f"mcp_servers.{name}.args={_toml_value(list(args))}"]
        cwd = cfg.get("cwd")
        if cwd:
            overrides += ["-c", f"mcp_servers.{name}.cwd={_toml_value(cwd)}"]
        env = cfg.get("env")
        if isinstance(env, dict):
            for key, value in env.items():
                if value is None:
                    continue
                process_env[str(key)] = str(value)
                # Secret-bearing values stay OFF argv → process-env inheritance only.
                if not _looks_secret_env_key(str(key)):
                    overrides += [
                        "-c",
                        f"mcp_servers.{name}.env.{key}={_toml_value(value)}",
                    ]
    return overrides, process_env


def _codex_cwd(settings: Settings) -> str:
    """Working directory / ``-C`` root — ALWAYS the real project workspace (claude parity)."""
    return str(settings.workspace)


def _build_args(
    settings: Settings,
    *,
    model: str | None,
    agent_id: str | None,
    reuse_agent: bool,
    thinking_mode: str | None,
    mcp_overrides: list[str],
    cmd_wrapper: bool,
) -> list[str]:
    """Assemble the full argv for ``codex exec`` (no shell). The prompt is delivered on
    stdin via the ``-`` positional (uniform across platforms), so no user/prompt text
    ever rides the argv — which also neutralises the Windows ``cmd /c`` reparse hazard for
    the prompt. Only flags + ``-c`` MCP overrides (paths, non-secret env) ride argv."""
    codex_bin = getattr(settings, "codex_bin", "") or "codex"
    args: list[str] = [codex_bin, "exec"]
    # Resume the persisted Codex thread → the model keeps the full conversation.
    if reuse_agent and agent_id:
        args += ["resume", agent_id]
    args += ["--json", "-m", _resolve_codex_model(settings, model)]
    effort = _reasoning_effort(thinking_mode)
    if effort:
        args += ["-c", f"model_reasoning_effort={_toml_value(effort)}"]
    args += mcp_overrides
    args += _sandbox_flags(_full_tools_enabled(settings))
    # Akana's workspace is not always a git repo → skip the safety check that would
    # otherwise abort the run; -C pins the workspace root.
    args += ["--skip-git-repo-check", "-C", _codex_cwd(settings)]
    # ``-`` → read the prompt from stdin (keeps it off argv on every platform).
    args += ["-"]
    _ = cmd_wrapper  # argv is identical on both paths; prompt always via stdin
    return args


# --------------------------------------------------------------------------- #
# Usage mapping
# --------------------------------------------------------------------------- #
def _usage_to_tokens(usage: dict[str, Any] | None) -> dict[str, Any]:
    """Normalize Codex ``turn.completed.usage`` → Akana tokens dict.

    ``input_tokens`` → prompt, ``output_tokens`` → completion, ``cached_input_tokens`` →
    cache_read. Codex is subscription-billed and returns no per-call cost → no
    ``cost_usd`` (same as openai/gemini). All counts are coerced defensively so a
    malformed external field never swallows the ``done`` event."""
    if isinstance(usage, dict):
        return {
            "prompt_tokens": base.coerce_token_count(usage.get("input_tokens")),
            "completion_tokens": base.coerce_token_count(usage.get("output_tokens")),
            "tool_calls": [],
            "cache_read_tokens": base.coerce_token_count(usage.get("cached_input_tokens")),
        }
    return {"prompt_tokens": 0, "completion_tokens": 0, "tool_calls": []}


# --------------------------------------------------------------------------- #
# Event translation
# --------------------------------------------------------------------------- #
class _CodexEventTranslator:
    """Turn ``codex exec --json`` JSONL events into Akana wire events + accumulate the
    terminal ``done`` payload.

    Codex emits item LIFECYCLE events (started/updated/completed) rather than token
    deltas, and the ``agent_message`` / ``reasoning`` text arrives cumulatively on the
    item. We diff each item's text against what we have already emitted, so the visible
    delta stream matches whether Codex sends one ``item.completed`` (the common case →
    one chunk) or several cumulative ``item.updated`` frames (→ progressive streaming).
    """

    def __init__(self) -> None:
        self.thread_id: str | None = None
        self.tool_calls: list[dict[str, Any]] = []
        self.usage: dict[str, Any] | None = None
        self.error: str | None = None
        self.terminal_error = False
        self._delta_text: list[str] = []
        self._seg_last_char = ""
        self._seg_gap_pending = False
        #: item id → chars already emitted (for text/reasoning diffing).
        self._msg_emitted: dict[str, int] = {}
        self._reasoning_emitted: dict[str, int] = {}
        self._reasoning_open: set[str] = set()
        #: mcp/command tool ids we have already opened a start card for (dedup).
        self._tool_started: set[str] = set()

    def final_text(self) -> str:
        return "".join(self._delta_text)

    def feed(self, ev: dict[str, Any]) -> list[dict[str, Any]]:
        etype = ev.get("type")
        if etype == "thread.started":
            tid = ev.get("thread_id")
            if tid:
                self.thread_id = str(tid)
                return [{"agent_id": self.thread_id}]
            return []
        if etype == "turn.completed":
            if isinstance(ev.get("usage"), dict):
                self.usage = ev.get("usage")
            return []
        if etype == "turn.failed":
            err = ev.get("error")
            self.error = str((err or {}).get("message") or "codex turn failed").strip()
            self.terminal_error = True
            return []
        if etype == "error":
            self.error = str(ev.get("message") or "codex error").strip()
            self.terminal_error = True
            return []
        if etype in ("item.started", "item.updated", "item.completed"):
            item = ev.get("item")
            if isinstance(item, dict):
                return self._feed_item(etype, item)
        return []

    def _emit_delta(self, text: str) -> list[dict[str, Any]]:
        """Emit visible answer text, welding a paragraph gap across a thinking/tool seam."""
        if not text:
            return []
        if self._seg_gap_pending:
            text = base.segment_gap(self._seg_last_char, text) + text
            self._seg_gap_pending = False
        self._delta_text.append(text)
        self._seg_last_char = text[-1]
        return [{"delta": text, "done": False}]

    def _feed_item(self, etype: str, item: dict[str, Any]) -> list[dict[str, Any]]:
        itype = item.get("type")
        iid = str(item.get("id") or "")
        if itype == "agent_message":
            return self._feed_text(iid, str(item.get("text") or ""))
        if itype == "reasoning":
            return self._feed_reasoning(etype, iid, str(item.get("text") or ""))
        if itype == "command_execution":
            return self._feed_command(etype, iid, item)
        if itype == "mcp_tool_call":
            return self._feed_mcp(etype, iid, item)
        if itype == "file_change":
            return self._feed_file_change(etype, iid, item)
        if itype == "todo_list":
            return self._feed_todo(item)
        return []

    def _feed_text(self, iid: str, text: str) -> list[dict[str, Any]]:
        already = self._msg_emitted.get(iid, 0)
        if len(text) <= already:
            return []
        self._msg_emitted[iid] = len(text)
        return self._emit_delta(text[already:])

    def _feed_reasoning(self, etype: str, iid: str, text: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        already = self._reasoning_emitted.get(iid, 0)
        if len(text) > already:
            self._reasoning_emitted[iid] = len(text)
            self._reasoning_open.add(iid)
            # Reasoning splits the answer → the next answer segment needs a break.
            self._seg_gap_pending = True
            out.append({"thinking": {"phase": "delta", "text": text[already:]}})
        if etype == "item.completed" and iid in self._reasoning_open:
            self._reasoning_open.discard(iid)
            out.append({"thinking": {"phase": "completed"}})
        return out

    def _tool_start(self, iid: str, name: str, args: Any) -> list[dict[str, Any]]:
        if iid in self._tool_started:
            return []
        self._tool_started.add(iid)
        call = {
            "id": iid,
            "name": name,
            "phase": "start",
            "args": args,
            "result": None,
            "status": None,
        }
        self.tool_calls.append(call)
        self._seg_gap_pending = True  # a tool splits the answer
        return [{"tool_call": call}]

    def _tool_end(
        self, iid: str, name: str, result: Any, status: str
    ) -> list[dict[str, Any]]:
        call = {
            "id": iid,
            "name": name,
            "phase": "end",
            "args": None,
            "result": result,
            "status": status,
        }
        for existing in self.tool_calls:
            if existing.get("id") == iid:
                existing.update({"result": result, "status": status})
                break
        else:
            self.tool_calls.append(call)
        return [{"tool_call": call}]

    def _feed_command(
        self, etype: str, iid: str, item: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if etype == "item.started":
            return self._tool_start(iid, "shell", {"command": item.get("command")})
        if etype == "item.completed":
            exit_code = item.get("exit_code")
            failed = item.get("status") == "failed" or (
                isinstance(exit_code, int) and exit_code != 0
            )
            return self._tool_end(
                iid,
                "shell",
                item.get("aggregated_output"),
                "error" if failed else "ok",
            )
        return []

    def _feed_mcp(
        self, etype: str, iid: str, item: dict[str, Any]
    ) -> list[dict[str, Any]]:
        server = str(item.get("server") or "")
        tool = str(item.get("tool") or "")
        name = f"mcp__{server}__{tool}" if server else tool
        if etype == "item.started":
            return self._tool_start(iid, name, item.get("arguments"))
        if etype == "item.completed":
            failed = item.get("status") == "failed" or item.get("error") is not None
            result = item.get("error") if failed else item.get("result")
            return self._tool_end(iid, name, result, "error" if failed else "ok")
        return []

    def _feed_file_change(
        self, etype: str, iid: str, item: dict[str, Any]
    ) -> list[dict[str, Any]]:
        # file_change arrives only as item.completed → emit a start+end pair so the UI
        # renders one complete card (the shared start-then-end contract).
        if etype != "item.completed":
            return []
        changes = item.get("changes") if isinstance(item.get("changes"), list) else []
        failed = item.get("status") == "failed"
        out = self._tool_start(iid, "apply_patch", {"changes": changes})
        out += self._tool_end(iid, "apply_patch", changes, "error" if failed else "ok")
        return out

    def _feed_todo(self, item: dict[str, Any]) -> list[dict[str, Any]]:
        rows = item.get("items") if isinstance(item.get("items"), list) else []
        items = [
            {
                "text": str(r.get("text") or ""),
                "done": bool(r.get("completed")),
            }
            for r in rows
            if isinstance(r, dict)
        ]
        return [{"todo": {"items": items}}] if items else []


# --------------------------------------------------------------------------- #
# Failure classification
# --------------------------------------------------------------------------- #
_AUTH_USER_MESSAGE = (
    "Could not authenticate the Codex session — run `codex login` in the terminal "
    "(ChatGPT sign-in). Codex is subscription-billed and uses no API key."
)


def _classify_codex_failure(*, error_text: str, stderr_text: str, model_tag: str) -> str:
    """Map a failed codex run to a user-facing message (auth / model / generic)."""
    combined = f"{error_text}\n{stderr_text}".lower()
    if any(
        k in combined
        for k in (
            "not logged in",
            "please run codex login",
            "run `codex login`",
            "unauthorized",
            "401",
            "authentication",
            "no auth",
            "logged out",
        )
    ):
        return _AUTH_USER_MESSAGE
    if ("model" in combined and ("not found" in combined or "not_found" in combined or "unknown" in combined)):
        return (
            f"Codex model not available: {model_tag} — "
            "select a different Codex model under Settings → Provider."
        )
    if "rate limit" in combined or "429" in combined or "quota" in combined:
        return "Codex rate limit reached (too many requests). Wait a moment and try again."
    meaningful = error_text.strip() or stderr_text.strip()
    return meaningful[:800] if meaningful else "codex run failed"


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
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
    plan_mode: bool = False,  # codex: accepted-and-ignored (claude-only ExitPlanMode)
    file_ids: list[str] | None = None,  # codex: accepted-and-ignored (CLI has no file_ids vision input)
    auto_continue: bool = False,  # codex: accepted-and-ignored (claude-only continuation)
) -> AsyncIterator[dict[str, Any]]:
    """ONE ``codex exec`` invocation, translated into Akana wire events.

    Yields (in order): an optional ``{"agent_id": <thread_id>}``, then
    ``{"delta": …}`` / ``{"thinking": …}`` / ``{"tool_call": …}`` / ``{"todo": …}``
    events as items complete, and finally exactly one terminal
    ``{"done": True, …}`` (base.stream_done_event). ``thinking_mode`` drives Codex's
    ``model_reasoning_effort`` override; ``plan_mode`` / ``file_ids`` / ``auto_continue``
    are claude-only and accepted-and-ignored here for the provider-neutral seam.
    """
    resuming = bool(reuse_agent and agent_id)
    prompt = _build_prompt(
        settings,
        user_text,
        history=history,
        system_prompt=system_prompt,
        chat_mode=chat_mode,
        resuming=resuming,
    )
    mcp_overrides, mcp_env = _mcp_overrides(mcp_servers)

    # NetworkEngine circuit breaker for the codex provider (no retry — stream deltas
    # can't be re-emitted). Checked BEFORE spawning so an open breaker fast-fails.
    from akana_server.network import load_network_config
    from akana_server.network.guard import global_registry

    _net_cfg = load_network_config(settings)
    _breaker = None
    if _net_cfg.breaker_enabled:
        _breaker = global_registry().get_or_create(
            "codex",
            threshold=_net_cfg.breaker_threshold,
            cooldown=_net_cfg.breaker_cooldown,
        )
        _breaker.before_call()  # BreakerOpenError if open

    codex_bin = getattr(settings, "codex_bin", "") or "codex"
    cmd_wrapper = needs_cmd_wrapper(codex_bin)
    args = _build_args(
        settings,
        model=model,
        agent_id=agent_id,
        reuse_agent=reuse_agent,
        thinking_mode=thinking_mode,
        mcp_overrides=mcp_overrides,
        cmd_wrapper=cmd_wrapper,
    )
    # Windows: an npm ``codex.cmd`` shim can't be exec'd directly → wrap with ``cmd /c``
    # (PATHEXT-aware). POSIX argv is returned untouched. The prompt is on stdin, so no
    # arbitrary content rides the cmd.exe-reparsed command line.
    args = executable_argv(args)
    env = _codex_env(settings, mcp_env)
    cwd = _codex_cwd(settings)
    timeout = base.idle_timeout(settings)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
            limit=CODEX_STDOUT_LINE_LIMIT,
            # Own process group (pgid == pid) so killpg takes down the codex CLI AND its
            # child MCP processes on shutdown/cancel; a bare kill would orphan them.
            start_new_session=True,
        )
    except (FileNotFoundError, NotADirectoryError) as e:
        if _breaker is not None:
            _breaker.record_failure()
        raise LLMCallError(
            f"Codex CLI not found ({codex_bin}) — install: npm install -g @openai/codex, "
            "then run `codex login`",
            status_code=503,
        ) from e
    except BaseException:
        if _breaker is not None:
            _breaker.record_failure()
        raise
    assert proc.stdout and proc.stderr and proc.stdin  # noqa: S101 - pipes present

    _proc_token = uuid.uuid4().hex
    register_llm_process(
        getattr(settings, "data_dir", None) or ".", _proc_token, proc.pid, "codex_cli"
    )

    # Feed the prompt over stdin then EOF. Guarded so a broken pipe (codex already
    # exited) never crashes the turn, and a cancellation right at turn start still
    # kills the process group + releases the pid (mirrors claude's stdin drain guard).
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
        if _breaker is not None and not isinstance(
            _setup_exc, (asyncio.CancelledError, GeneratorExit)
        ):
            _breaker.record_failure()
        try:
            await terminate_process_group(proc.pid)
        except Exception:  # pragma: no cover
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        release_llm_process(getattr(settings, "data_dir", None) or ".", _proc_token)
        raise

    async def _drain_stderr() -> bytes:
        try:
            return await proc.stderr.read()
        except Exception:  # pragma: no cover
            return b""

    stderr_task = asyncio.create_task(_drain_stderr())
    tr = _CodexEventTranslator()

    try:
        try:
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
                for out in tr.feed(ev):
                    yield out
                if tr.terminal_error:
                    break
        except TimeoutError as e:
            await terminate_process_group(proc.pid)
            raise LLMCallError("LLM_TIMEOUT: codex CLI timed out", status_code=504) from e
        except asyncio.LimitOverrunError as e:
            await terminate_process_group(proc.pid)
            raise LLMCallError(
                "a single line in the codex CLI response exceeded the 8MiB limit",
                status_code=502,
            ) from e

        await proc.wait()

        err_bytes = await stderr_task
        stderr_text = err_bytes.decode("utf-8", errors="replace").strip()

        # Failure: an explicit error event, OR the process died without a terminal
        # turn.completed and a non-zero exit (mirrors claude's subprocess-death guard →
        # raise instead of a fake empty success; deltas already reached the user).
        if tr.terminal_error or (
            tr.usage is None and proc.returncode not in (0, None)
        ):
            msg = _classify_codex_failure(
                error_text=tr.error or "",
                stderr_text=stderr_text,
                model_tag=_resolve_codex_model(settings, model),
            )
            log.warning(
                "codex run failed (rc=%s): %s | stderr: %s",
                proc.returncode,
                tr.error,
                stderr_text[:400],
            )
            raise LLMCallError(msg, status_code=503)

        if _breaker is not None:
            _breaker.record_success()
        yield base.stream_done_event(
            usage=_usage_to_tokens(tr.usage),
            text=tr.final_text(),
            status="finished",
            tool_calls=tr.tool_calls,
        )
    except BaseException as _net_exc:  # noqa: BLE001 - report to the breaker, don't swallow
        if _breaker is not None and not isinstance(
            _net_exc, (asyncio.CancelledError, GeneratorExit)
        ):
            _breaker.record_failure()
        raise
    finally:
        if not stderr_task.done():
            stderr_task.cancel()
        if proc.returncode is None:
            try:
                await terminate_process_group(proc.pid)
            except Exception:  # pragma: no cover
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        release_llm_process(getattr(settings, "data_dir", None) or ".", _proc_token)


async def _read_line(reader: asyncio.StreamReader, timeout: float) -> bytes:
    """Read one JSONL line, tolerating large tool payloads (see claude_provider._read_line).

    A non-positive ``timeout`` means "wait indefinitely" (``combine_cap`` yields 0 for a
    disabled idle ceiling; passing 0 to ``wait_for`` would time out INSTANTLY → every
    stream would die on the first read)."""

    async def _read() -> bytes:
        try:
            return await reader.readuntil(b"\n")
        except asyncio.IncompleteReadError as e:
            return bytes(e.partial)

    if timeout and timeout > 0:
        return await asyncio.wait_for(_read(), timeout=timeout)
    return await _read()


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
    plan_mode: bool = False,  # codex: accepted-and-ignored (claude-only)
    file_ids: list[str] | None = None,  # codex: accepted-and-ignored (no CLI vision input)
    auto_continue: bool = False,  # codex: accepted-and-ignored (claude-only continuation)
) -> tuple[str, str, dict[str, Any]]:
    """One-shot chat: consume :func:`stream_user_chat`, return (text, status, raw).

    ``raw`` is the terminal done event (carries usage + tool_calls)."""
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
    ):
        if ev.get("done"):
            text = str(ev.get("text") or "")
            status = str(ev.get("status") or "finished")
            raw = ev
    return text, status, raw


__all__ = ["CAPABILITIES", "complete_chat", "stream_user_chat"]
