"""Built-in Cursor SDK provider (Node bridge).

This is the full Cursor provider implementation — symmetric with
``claude_provider`` / ``gemini_provider`` / ``openai_provider`` / ``ollama_provider``.
It owns:

* payload building (:func:`build_payload`), the bridge environment
  (:func:`bridge_env`), args (:func:`bridge_args`) and API-key resolution
  (:func:`runtime_cursor_key` / :func:`ensure_api_key`);
* one-shot completion (:func:`complete_chat`) — spawns ``run_prompt.mjs`` once and
  parses the terminal NDJSON line;
* real streaming (:func:`stream_user_chat`) — the DIRECT (daemon-less) spawn path,
  used only when ``AKANA_BRIDGE_DAEMON=0``; the default path is the persistent
  daemon in :mod:`bridge_pool`. Both transports decode the NDJSON contract through
  the single :class:`akana_server.orchestrator.base.CursorStreamDecoder`.

HISTORICAL NAME: this code used to live inside ``llm_dispatch`` (which was itself
once called ``cursor_client``). ``llm_dispatch`` is now a thin router that
delegates the cursor path here and re-exports these helpers under their historical
private names (``_build_payload``/``_bridge_env``/``_usage_to_tokens`` …) for
backward compatibility. Behaviour is unchanged — this is a move, not a rewrite.
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
from akana_server.orchestrator import base
from akana_server.orchestrator.chat_persona import CHAT_SYSTEM_PREFIX
from akana_server.orchestrator.errors import LLMCallError, LLMResult, friendly_provider_error
from akana_server.orchestrator.llm_process import executable_argv

log = logging.getLogger(__name__)

#: Cursor supports agent reuse (a stored agent id resumes the cloud session), so a
#: present agent id lets history be skipped rather than re-sent — ``stateless=False``
#: (queried via llm_dispatch.provider_capabilities; consumed by chat_context).
CAPABILITIES = base.ProviderCapabilities(stateless=False)


def _resolve_prompt_language(settings: Settings) -> str:
    """Active prompt language (``en`` | ``tr``) from the runtime ``language`` setting;
    any failure → ``"en"`` (English-first default). Delegates to the canonical
    :func:`runtime_settings.resolve_language` (which is itself fully failure-guarded, so
    no extra try/except is needed here). The module-level name is kept because
    :mod:`llm_dispatch` re-exports it for the cursor payload path + tests."""
    from akana_server.runtime_settings import resolve_language

    return resolve_language(settings)


def resolve_model(settings: Settings, model: str | None) -> str:
    tag = (model or "").strip() or settings.cursor_model.strip()
    return tag or "composer-2"


def runtime_cursor_key(settings: Settings) -> str:
    """Resolve the Cursor API key: runtime secret-store wins, env settings fallback.

    Defensive ``getattr`` so SimpleNamespace test doubles without ``data_dir``
    or ``cursor_api_key`` keep working.
    """
    data_dir = getattr(settings, "data_dir", None)
    if data_dir is not None:
        try:
            from akana_server.secret_store import get_secret

            stored = get_secret(data_dir, "cursor_api_key")
            if stored:
                return stored
        except Exception:  # pragma: no cover - store unreadable → env fallback
            pass
    # The .env fallback can still be the shipped .env.example placeholder
    # (CURSOR_API_KEY=your-cursor-api-key-here). Treat a placeholder as "no key" —
    # otherwise the health probe reports key_set:true, the onboarding banner reads
    # "key saved but not reachable", and chat 401s on a bogus bearer. Placeholder-only
    # (no length floor): a real-but-short user value still passes; the floor is enforced
    # on the credentials WRITE path, not here.
    from akana_server.secret_store import looks_like_placeholder

    env_key = getattr(settings, "cursor_api_key", None) or ""
    return "" if looks_like_placeholder(env_key) else env_key


def bridge_env(settings: Settings) -> dict[str, str]:
    """Environment for the Cursor SDK Node bridge — server bearer + foreign provider
    secrets are NOT leaked (SEC2).

    Previously the entire ``os.environ`` was forwarded → the Cursor SDK (third-party)
    and every tool it spawned could see ``AKANA_TOKEN`` (server bearer) + Claude
    secrets. The claude path (``_claude_env``) already applies the same denylist;
    this is symmetric. DENYLIST approach (not allowlist): ``PATH``/``HOME``/``NODE_*``
    etc. are preserved (so the node process runs), only known secrets are dropped.
    ``CURSOR_API_KEY`` is legitimate (the SDK's own key) → set explicitly."""
    env = dict(os.environ)
    for key in (
        "AKANA_TOKEN",
        LEGACY_ENV_PREFIX + "TOKEN",  # legacy name — drop if still in environment
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        # SEC2: foreign-provider secrets the user set for their own tooling must NOT
        # leak into the third-party Cursor bridge or the tool/MCP subprocesses it
        # spawns — the Cursor SDK only needs CURSOR_API_KEY (set explicitly below).
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    ):
        env.pop(key, None)
    env["CURSOR_API_KEY"] = runtime_cursor_key(settings)
    return env


def bridge_args(settings: Settings) -> list[str]:
    script = settings.bridge_dir / "run_prompt.mjs"
    if not script.is_file():
        raise LLMCallError(f"cursor bridge missing: {script}", status_code=503)
    # Windows: resolve ``node`` → ``node.exe`` so the shell-less ``create_subprocess_exec``
    # finds it (no-op on POSIX). Bridge input travels as stdin JSON, so no ``cmd /c`` wrap.
    return executable_argv(["node", str(script)])


def ensure_api_key(settings: Settings) -> None:
    if not runtime_cursor_key(settings):
        raise LLMCallError(
            "CURSOR_API_KEY is not set — add it to .env",
            status_code=503,
        )


def scan_one_shot_need_history(raw_out: str) -> bool:
    """Check whether the one-shot bridge stdout contains a ``{"ev":"need_history"}`` event."""
    for line in raw_out.splitlines():
        chunk = line.strip()
        if not chunk.startswith("{"):
            continue
        try:
            ev = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(ev, dict) and ev.get("ev") == "need_history":
            return True
    return False


def build_payload(
    settings: Settings,
    user_text: str,
    *,
    history: list[dict[str, str]] | None,
    model: str | None,
    stream: bool,
    chat_mode: bool = True,
    conversation_id: str | None = None,
    agent_id: str | None = None,
    reuse_agent: bool = True,
    mcp_servers: dict[str, Any] | None = None,
    system_prompt: str | None = None,
) -> dict[str, Any]:
    # Unified agent: the cwd is ALWAYS the real project workspace (the old chat
    # sandbox is gone). chat_mode now only gates the persona, not the cwd.
    cwd = settings.workspace
    payload: dict[str, Any] = {
        "prompt": user_text,
        "cwd": str(cwd),
        "model": resolve_model(settings, model),
        "history": history or [],
        "stream": bool(stream),
        "reuse_agent": bool(reuse_agent),
    }
    if conversation_id:
        payload["conversation_id"] = conversation_id
        payload["session_key"] = conversation_id
    elif agent_id:
        # No conversation but bound to a specific agent: pin session_key to the agent —
        # stable reuse + correct serialisation; avoids falling into "default" collision.
        payload["session_key"] = f"agent:{agent_id}"
    else:
        # Fully independent one-shot (e.g. concurrent memory recall/capture): unique
        # ephemeral key + reuse disabled → NO "default" serialisation in the daemon and
        # NO agent cache leakage (reuse=false ⟹ sessions.set is skipped).
        payload["session_key"] = f"oneshot:{uuid.uuid4().hex}"
        payload["reuse_agent"] = False
    if agent_id:
        # WIRE: the cursor bridge ``.mjs`` protocol reads ``input.cursor_agent_id``
        # (lib.mjs acquireAgent → Agent.resume). The Python variable is neutral
        # (``agent_id``) but this payload KEY is a cursor-private wire contract →
        # cannot be neutralised.
        payload["cursor_agent_id"] = agent_id
    # WI-2 agent-work-mode: the caller may supply its own system prompt (skill body +
    # pack persona); if not provided in chat mode the classic persona is used.
    if system_prompt:
        payload["system"] = system_prompt
    elif chat_mode:
        payload["system"] = CHAT_SYSTEM_PREFIX
    # Cursor bridge: the multi-turn history-frame label follows this language
    # (en|tr) so a hardcoded Turkish frame no longer biases English-mode chats
    # toward Turkish replies (mirrors claude_provider._HISTORY_FRAME). Resolved
    # defensively — any failure collapses to "en" (English-first default).
    payload["language"] = _resolve_prompt_language(settings)
    if mcp_servers:
        payload["mcp_servers"] = mcp_servers
    payload["chat_mode"] = bool(chat_mode)
    # NOTE: the Cursor SDK exposes NO effort/reasoning-level input knob — the only
    # reasoning control would be a model-declared parameter surfaced via
    # ModelSelection.params ({id, value}, discovered from Cursor.models.list()),
    # which is a different shape entirely. A bare ``thinking_mode`` string is not a
    # Cursor concept, so it is deliberately NOT written into the payload (the effort
    # toggle is a no-op on Cursor; the model may still emit ``thinking`` events on
    # its own, which is not user-controlled).
    return payload


def encode_payload(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def usage_to_tokens(
    usage: dict[str, Any] | None,
    *,
    model: str | None = None,
    cost_usd: float | None = None,
    estimate_cost: bool = False,
) -> dict[str, Any]:
    """Normalize Cursor SDK usage payload → Akana tokens dict.

    Single-argument calls (including bridge_pool) are UNCHANGED: a plain token block
    with NO cost field. Cost is added only when EXPLICITLY requested (backward-
    compatible, opt-in) — the caller either supplies a ready ``cost_usd`` or passes
    ``estimate_cost=True``/``model``. The Cursor SDK usage payload does not carry
    cost (no field like Anthropic's ``total_cost_usd``), so for parity with Claude's
    ``_usage_to_tokens(usage, cost_usd)`` we ESTIMATE cost from token counts via
    ``base.estimate_cost_usd`` (no public Cursor price list → sonnet default is
    accepted, LIVE estimate). ``cost_usd`` is added to the dict only when > 0 —
    so the front-end does not show "0.000$" when the value is uncertain."""
    if isinstance(usage, dict):
        input_tokens = base.coerce_token_count(
            usage.get("inputTokens") or usage.get("input_tokens")
        )
        output_tokens = base.coerce_token_count(
            usage.get("outputTokens") or usage.get("output_tokens")
        )
        cache_read = base.coerce_token_count(
            usage.get("cacheReadTokens") or usage.get("cache_read_tokens")
        )
        cache_write = base.coerce_token_count(
            usage.get("cacheWriteTokens") or usage.get("cache_write_tokens")
        )
        out: dict[str, Any] = {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "tool_calls": [],
            "cache_read_tokens": cache_read,
            "cache_write_tokens": cache_write,
        }
        # Cost is OPT-IN: only when an explicit cost_usd or estimate_cost/model is provided.
        want_cost = cost_usd is not None or estimate_cost or model is not None
        if want_cost:
            cost = base.coerce_cost_usd(cost_usd)
            if cost <= 0:
                cost = base.estimate_cost_usd(
                    model,
                    input_tokens,
                    output_tokens,
                    cache_read=cache_read,
                    cache_write=cache_write,
                )
            if cost > 0:
                out["cost_usd"] = cost
        return out
    return {"prompt_tokens": 0, "completion_tokens": 0, "tool_calls": []}


async def run_one_shot(
    settings: Settings,
    *,
    args: list[str],
    env: dict[str, str],
    payload: dict[str, Any],
    call_timeout: float,
) -> LLMResult:
    """Spawn ``run_prompt.mjs`` once with a pre-built payload/env/args; parse the last line.

    The dispatch hub resolves ``args``/``env``/``payload``/``call_timeout`` through its
    own (patchable) helpers and hands them in, so the spawn+parse machinery lives here
    without duplicating the payload/env resolution. :func:`complete_chat` is the
    self-contained convenience wrapper that resolves everything itself.
    """
    # Hang guard: wall-clock ceiling for the one-shot call (min(bridge, total)).
    # ``asyncio.wait_for(call_timeout)`` cancels the coroutine (including communicate)
    # on timeout; the ``finally`` below kills the process GROUP → same outcome as the
    # old blocking "abort/cleanup" path, but orphan child processes are not left behind.

    def _parse_bridge_output(returncode: int | None, raw_out: str, raw_err: str) -> dict[str, Any]:
        # Post-run parsing (mirrors the old subprocess.run path — kept verbatim):
        # returncode != 0 → catch structured error / else tail stderr; success →
        # parse the last NDJSON line. ``need_history`` (same wire as streaming) is
        # also scanned in one-shot mode — the last line may be ``{"ev":"need_history"}``
        # with no ``ok``.
        if scan_one_shot_need_history(raw_out):
            raise LLMCallError(
                "could not load history for the resume session",
                status_code=503,
            )
        if returncode != 0:
            parsed: dict[str, Any] | None = None
            for chunk in (raw_out, raw_err):
                line = chunk.splitlines()[-1].strip() if chunk else ""
                if line.startswith("{"):
                    try:
                        parsed = json.loads(line)
                        break
                    except json.JSONDecodeError:
                        pass
            if parsed and not parsed.get("ok"):
                # CUR-3: route structured bridge errors through the classifier too
                # (previously the raw message leaked) — error_code/status hints
                # clarify the auth/rate-limit/timeout/resume sub-type.
                _st = parsed.get("status") if isinstance(parsed.get("status"), int) else None
                raise LLMCallError(
                    friendly_provider_error(
                        str(parsed.get("error") or "cursor run failed"),
                        provider="cursor",
                        error_code=str(parsed.get("error_code") or "") or None,
                        status=_st,
                    )
                )
            err = (raw_err or raw_out or str(returncode))[:800]
            raise LLMCallError(friendly_provider_error(err, provider="cursor"))
        try:
            return json.loads(raw_out.splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as e:
            raise LLMCallError(f"invalid bridge output: {raw_out[:400]}") from e

    async def _run() -> dict[str, Any]:
        # BUG (leak): the old path ran ``subprocess.run`` blocking in a
        # ``run_in_executor`` THREAD; on cancel/timeout the thread could not be
        # interrupted, so the child process kept running until its own internal
        # timeout (wasted tokens + dangling process). We now apply the streaming
        # path's proven cleanup pattern exactly: native cancellable async spawn +
        # process-group kill.
        from akana_server.orchestrator.llm_process import (
            node_missing_error,
            register_llm_process,
            release_llm_process,
            terminate_process_group,
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=str(settings.bridge_dir),
                # Own process group → on cancel/timeout killpg takes down ALL child
                # processes of the node bridge (no orphans); same as the streaming path.
                start_new_session=True,
            )
        except (FileNotFoundError, NotADirectoryError) as e:
            # `node` absent → raw WinError 2 / ENOENT otherwise; name the dependency.
            raise node_missing_error() from e
        _proc_token = uuid.uuid4().hex
        register_llm_process(
            getattr(settings, "data_dir", None) or ".", _proc_token, proc.pid, "cursor_bridge"
        )
        try:
            # ``communicate`` writes + closes stdin, accumulates stdout+stderr to
            # completion (same full-buffer behaviour as the old ``capture_output=True``);
            # on CancelledError (external cancel or wait_for timeout) the finally
            # below killpg's the process. A broken pipe on early child exit is
            # normal → swallow.
            try:
                out_b, err_b = await proc.communicate(encode_payload(payload))
            except (BrokenPipeError, ConnectionResetError):
                out_b, err_b = b"", b""
            raw_out = (out_b or b"").decode("utf-8", errors="replace").strip()
            raw_err = (err_b or b"").decode("utf-8", errors="replace").strip()
            return _parse_bridge_output(proc.returncode, raw_out, raw_err)
        finally:
            # Cancel/timeout/early-return: if the process is still alive, kill its GROUP
            # (node bridge + descendants). On normal completion returncode is set → no-op.
            if proc.returncode is None:
                try:
                    await terminate_process_group(proc.pid)
                except Exception:  # pragma: no cover - already dead / race
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
            release_llm_process(getattr(settings, "data_dir", None) or ".", _proc_token)

    try:
        # NetworkEngine F0: wrap the subprocess call with the per-provider circuit
        # breaker + timeout. A one-shot LLM completion is NOT idempotent, so retry is
        # DISABLED (max_retries=1): retrying a transient failure would re-run the whole
        # generation → duplicate tokens + side-effects on the blocking callers
        # (connectors, memory-capture, session-closer). The streaming path doesn't retry
        # either. ``guard`` takes a factory ``_run()`` (a fresh coroutine per attempt).
        import dataclasses

        from akana_server.network import guard, load_network_config
        from akana_server.network.timeout import NetworkTimeoutError

        cfg = dataclasses.replace(load_network_config(settings), max_retries=1)
        out = await guard(
            _run,
            provider="cursor",
            cfg=cfg,
            timeout=call_timeout,
        )
    except (TimeoutError, NetworkTimeoutError) as e:
        # One-shot timeout surfaces here: guard's with_timeout (asyncio.wait_for,
        # call_timeout) cancels ``_run`` → finally killpg's the child → raises
        # NetworkTimeoutError (TransientError). The «504 LLM_TIMEOUT» contract is
        # preserved; bare TimeoutError is also handled.
        raise LLMCallError("LLM_TIMEOUT: cursor bridge timed out", status_code=504) from e

    if not out.get("ok"):
        # dispatch:smell:3 — route the raw ok:false bridge error through the classifier
        # (like every other cursor error path) so the user never sees raw bridge text.
        raise LLMCallError(
            friendly_provider_error(
                str(out.get("error") or "cursor run failed"), provider="cursor"
            ),
            status_code=503,
        )

    text = str(out.get("text") or "").strip()
    return LLMResult(
        text=text,
        status=str(out.get("status") or "completed"),
        raw=out,
    )


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
) -> LLMResult:
    """Self-contained one-shot Cursor completion (resolves payload/env/args itself)."""
    ensure_api_key(settings)
    args = bridge_args(settings)
    payload = build_payload(
        settings,
        user_text,
        history=history,
        model=model,
        stream=False,
        chat_mode=chat_mode,
        conversation_id=conversation_id,
        agent_id=agent_id,
        reuse_agent=reuse_agent,
        mcp_servers=mcp_servers,
        system_prompt=system_prompt,
    )
    env = bridge_env(settings)
    return await run_one_shot(
        settings,
        args=args,
        env=env,
        payload=payload,
        call_timeout=base.total_timeout(settings),
    )


async def run_stream(
    settings: Settings,
    *,
    args: list[str],
    env: dict[str, str],
    payload: dict[str, Any],
    model: str | None,
) -> AsyncIterator[dict[str, Any]]:
    """DIRECT (daemon-less) Cursor stream from a pre-built payload/env/args.

    Used only when ``AKANA_BRIDGE_DAEMON=0``; the default path is the persistent
    daemon (:meth:`bridge_pool.BridgePool.stream_run`). Both decode the NDJSON
    contract through the shared
    :class:`akana_server.orchestrator.base.CursorStreamDecoder`; this path owns only
    its own I/O (a per-turn subprocess spawn + circuit breaker) and reads to the
    terminal event (break-on-done — canonical daemon semantics). The dispatch hub
    resolves ``args``/``env``/``payload`` through its own (patchable) helpers and
    hands them in; :func:`stream_user_chat` is the self-contained convenience wrapper.
    """
    # NetworkEngine F0: circuit breaker for the direct (daemon-less) cursor stream
    # (no retry — deltas cannot be replayed). Fast-fail before spawn if open;
    # record_success when the stream finishes, record_failure on error.
    from akana_server.network import load_network_config
    from akana_server.network.guard import global_registry

    _net_cfg = load_network_config(settings)
    _breaker = None
    if _net_cfg.breaker_enabled:
        # get_or_create binds threshold/cooldown at creation without mutating the
        # shared registry defaults (the old configure()+get() pair flip-flopped the
        # defaults across providers and never retuned the existing breaker).
        _breaker = global_registry().get_or_create(
            "cursor",
            threshold=_net_cfg.breaker_threshold,
            cooldown=_net_cfg.breaker_cooldown,
        )
        _breaker.before_call()  # raises BreakerOpenError if open

    from akana_server.orchestrator.llm_process import (
        node_missing_error,
        register_llm_process,
        release_llm_process,
        terminate_process_group,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=str(settings.bridge_dir),
            # BUG 1: own process group → killpg in finally takes down ALL child
            # processes of the node bridge (no orphans).
            start_new_session=True,
        )
    except (FileNotFoundError, NotADirectoryError) as e:
        # `node` absent → raw WinError 2 / ENOENT otherwise; name the dependency.
        raise node_missing_error() from e
    assert proc.stdin and proc.stdout and proc.stderr  # noqa: S101 - subprocess pipes are always present

    _proc_token = uuid.uuid4().hex
    register_llm_process(
        getattr(settings, "data_dir", None) or ".", _proc_token, proc.pid, "cursor_bridge"
    )

    async def _drain_stderr() -> bytes:
        try:
            return await proc.stderr.read()
        except Exception:  # pragma: no cover
            return b""

    stderr_task = asyncio.create_task(_drain_stderr())
    try:
        proc.stdin.write(encode_payload(payload))
        await proc.stdin.drain()
        proc.stdin.close()
    except (BrokenPipeError, ConnectionResetError):
        pass

    # Active model tag: used for live + terminal cost estimation (sonnet default).
    _active_model = resolve_model(settings, model)
    decoder = base.CursorStreamDecoder(model=_active_model)

    try:
        # Hang guard: every line read is bounded by the idle ceiling. The counter
        # resets whenever a new chunk arrives (delta/tool/done) → a slow but
        # progressing stream is never cut off; only a genuine hang (stopped
        # producing chunks) is caught.
        idle = base.idle_timeout(settings)
        try:
            while True:
                line = await base.read_ndjson_line(proc.stdout, idle)
                if not line:
                    break
                try:
                    ev = json.loads(line.decode("utf-8", errors="replace").strip() or "null")
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue
                for out in decoder.feed(ev):
                    yield out
                if decoder.terminal is not None:
                    # break-on-done (canonical daemon semantics): stop reading once
                    # the terminal event arrives instead of draining to EOF.
                    break
        except TimeoutError as e:
            # Stream exceeded the idle ceiling (hang). BUG 1: kill the process group
            # (including node bridge children) — existing cleanup path; no new leak.
            await terminate_process_group(proc.pid)
            raise LLMCallError("LLM_TIMEOUT: cursor bridge timed out", status_code=504) from e

        if decoder.terminal == "need_history":
            # ``{"need_history_bootstrap": True}`` was already yielded by the decoder;
            # nothing more to emit on this path.
            return

        # Reap the process now that the run terminated (or the stream ended without a
        # terminal event → returncode is set on natural EOF).
        await proc.wait()
        if decoder.bridge_error is not None or proc.returncode not in (0, None):
            be = decoder.bridge_error or {}
            err = be.get("error")
            if not err:
                err_bytes = await stderr_task
                err = err_bytes.decode("utf-8", errors="replace").strip() or "cursor run failed"
            # CUR-3: forward the bridge's structural hints (error_code/status) to the
            # classifier → auth/rate-limit/timeout sub-type is correct even when the
            # raw text is ambiguous.
            _status = be.get("status") if isinstance(be.get("status"), int) else None
            raise LLMCallError(
                friendly_provider_error(
                    str(err),
                    provider="cursor",
                    error_code=str(be.get("error_code") or "") or None,
                    status=_status,
                )
            )

        # CUR-2: the terminal done.usage carries cost. Cursor's done usage does not
        # provide cost → it is estimated from token counts using the active model tag
        # (sonnet default); symmetric with Claude's ``cost_usd`` in done (the
        # generator persists + forwards to SSE when cost_usd > 0 in
        # ``_done_tokens_block``).
        tokens = usage_to_tokens(decoder.usage, model=_active_model)
        if _breaker is not None:
            _breaker.record_success()  # stream finished cleanly
        yield decoder.done_event(tokens)
    except BaseException as _net_exc:  # noqa: BLE001 - notify breaker, do not swallow
        # BH: a consumer disconnect (GeneratorExit, e.g. the SSE client went away or a
        # history-bootstrap break) is NOT a provider fault — counting it (like
        # CancelledError) would let a disconnect burst trip/re-open the shared 'cursor'
        # breaker for unrelated requests.
        if _breaker is not None and not isinstance(
            _net_exc, (asyncio.CancelledError, GeneratorExit)
        ):
            _breaker.record_failure()
        raise
    finally:
        if not stderr_task.done():
            stderr_task.cancel()
        # BUG 1: kill the process group (node bridge + children) — a plain
        # proc.kill() would orphan descendants.
        if proc.returncode is None:
            try:
                await terminate_process_group(proc.pid)
            except Exception:  # pragma: no cover - already dead / race
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        release_llm_process(getattr(settings, "data_dir", None) or ".", _proc_token)


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
    system_prompt: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Self-contained direct Cursor stream (resolves payload/env/args itself)."""
    ensure_api_key(settings)
    payload = build_payload(
        settings,
        user_text,
        history=history,
        model=model,
        stream=True,
        conversation_id=conversation_id,
        agent_id=agent_id,
        reuse_agent=reuse_agent,
        mcp_servers=mcp_servers,
        system_prompt=system_prompt,
    )
    async for ev in run_stream(
        settings,
        args=bridge_args(settings),
        env=bridge_env(settings),
        payload=payload,
        model=model,
    ):
        yield ev


__all__ = [
    "LLMCallError",
    "LLMResult",
    "bridge_args",
    "bridge_env",
    "build_payload",
    "complete_chat",
    "encode_payload",
    "ensure_api_key",
    "friendly_provider_error",
    "resolve_model",
    "run_one_shot",
    "run_stream",
    "runtime_cursor_key",
    "scan_one_shot_need_history",
    "stream_user_chat",
    "usage_to_tokens",
]
