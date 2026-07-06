"""Persistent Node bridge daemon — one subprocess, many CONCURRENT chat turns.

Concurrency model (BUG 2 fix): the daemon (``bridge_daemon.mjs``) writes
``id``-tagged NDJSON to stdout and multiplexes requests by ``id`` — different
conversations can run at the same time. The old design held a single global
``asyncio.Lock`` for the ENTIRE stream, which serialized conversations and made
the second conversation wait until the first finished. The new design:

* **Single demultiplexer**: a single background ``_reader`` task reads the
  daemon stdout and dispatches each line to that request's queue by ``id``.
* **stdin writes** are serialized with a short ``_write_lock`` (interleaved
  writes would corrupt the NDJSON line) — but the LONG read loop holds no lock;
  conversations progress in parallel.
* **spawn** is serialized with ``_spawn_lock`` (one daemon, one ping).

Process lifecycle (BUG 1 fix): the daemon starts as the leader of its own
process group via ``start_new_session=True`` (pgid == pid) and a pid file is
written; on shutdown ``terminate_process_group`` (SIGTERM→SIGKILL) takes down
the ENTIRE tree (including the real cursor/node child processes).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

from akana_server.config import Settings
from akana_server.orchestrator import base
from akana_server.orchestrator.cursor_provider import (
    bridge_env,
    runtime_cursor_key,
    usage_to_tokens,
)
from akana_server.orchestrator.errors import LLMCallError, friendly_provider_error
from akana_server.orchestrator.llm_process import (
    executable_argv,
    node_missing_error,
    register_llm_process,
    release_llm_process,
    terminate_process_group,
)
from akana_server.secret_store import mask_hint

log = logging.getLogger(__name__)

_pool: BridgePool | None = None


# Timeout resolvers now live in :mod:`.base` (dispatch:smell:2 — they were
# duplicated verbatim here and in ``llm_dispatch``). ``base.idle_timeout`` is the
# per-turn idle ceiling ``min(bridge_timeout, llm_idle_timeout)``; the counter
# reset lives in ``_stream_run_once``'s read loop (every chunk starts a new
# ``wait_for(q.get(), idle)`` round → only REAL silence trips it, a slow-but-live
# stream is never cut off). The daemon is the MOST COMMON path, so this brings the
# same ceiling the direct cursor/claude paths already enforce. Thin delegations are
# kept under the historical names for existing imports (test_bridge_daemon_hang).
_bridge_timeout = base.bridge_timeout
_idle_timeout = base.idle_timeout


#: Sentinel marking the end of a single stream (reader → queue).
_STREAM_EOF = object()

#: Maximum number of distinct rids held in the ``_pending`` buffer. Legitimate
#: use (an early event arriving before the queue is registered) is momentary;
#: this cap prevents late done/error events after a cancel — over the persistent
#: daemon's lifetime — from growing the buffer without bound (the oldest rid is
#: dropped).
_PENDING_RID_CAP = 256


class BridgePool:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._proc: asyncio.subprocess.Process | None = None
        self._proc_token: str | None = None
        # spawn/ping serialization — not the ENTIRE stream, only process setup.
        self._spawn_lock = asyncio.Lock()
        # stdin write serialization — interleaved writes corrupt the NDJSON.
        self._write_lock = asyncio.Lock()
        self._req_counter = 0
        self._spawn_key = ""
        # id → line queue for this request (demultiplexer target).
        self._queues: dict[str, asyncio.Queue[Any]] = {}
        # Events arriving BEFORE the queue is registered (the rid is known but
        # the consumer has not registered yet) are buffered here; flushed once
        # the queue is opened. The daemon response comes after the request, but
        # this prevents early-event loss under fast/racy scheduling (and in tests).
        self._pending: dict[str, list[Any]] = {}
        self._reader: asyncio.Task[None] | None = None
        # Turns that are about to claim / have claimed the current proc but have
        # not YET registered their (numeric) rid under ``_write_lock`` — the gap
        # between ``_ensure_proc`` returning and ``_register_queue`` in
        # ``_stream_run_once``. The rotation guard below only sees numeric rids in
        # ``_queues``, so a turn still in this gap is invisible to it; without this
        # counter a concurrent key-change turn could kill+respawn the daemon out
        # from under a turn that already has a stdin handle to the old process.
        # OWNERSHIP: ``_stream_run_once`` is the sole owner — it increments before
        # calling ``_ensure_proc`` (so a concurrent rotation defers) and decrements
        # once its rid is registered (or the claim aborts). ``_ensure_proc`` itself
        # never mutates it, so a standalone ``_ensure_proc`` call (probes/tests) does
        # not leak a phantom claim that would wedge deferred rotation forever.
        self._claims_pending = 0
        # Has the reader seen EOF for the current process? (so a late-registered
        # queue gets _STREAM_EOF immediately — otherwise a late consumer waits
        # forever.)
        self._eof_seen = False
        # Rolling tail of the daemon's recent stderr lines. When the daemon dies
        # mid-response (no terminal event), this is the only record of WHY (a Node
        # crash stack, an SDK throw) — without it the user only ever sees the opaque
        # "closed mid-response". Bounded so a chatty daemon can't grow it unbounded.
        self._stderr_tail: deque[str] = deque(maxlen=40)

    def _daemon_args(self) -> list[str]:
        script = self._settings.bridge_dir / "bridge_daemon.mjs"
        if not script.is_file():
            raise LLMCallError(f"bridge daemon missing: {script}", status_code=503)
        # Windows: resolve ``node`` → ``node.exe`` for the shell-less spawn (no-op on
        # POSIX); requests reach the daemon as stdin NDJSON, so no ``cmd /c`` wrap.
        return executable_argv(["node", str(script)])

    async def _ensure_proc(self, *, own_claim: bool = False) -> asyncio.subprocess.Process:
        # Daemon env is frozen at spawn; a runtime key change (dashboard
        # credentials PUT) must rotate the process or it keeps the stale key.
        # HEALTH NOTE: a dead/exited daemon must not be handed back out — the
        # ``returncode is None`` condition already ensures this (once asyncio
        # reaps the process, returncode is set → fresh spawn below). A daemon
        # that dies mid-turn (stdout EOF) is additionally group-killed and set to
        # ``_proc=None`` on ``_stream_run_once``'s ``not saw_terminal`` path; the
        # next turn opens a fresh one. (An additional ``_eof_seen``/``os.kill``
        # health probe was considered but not added: it broke the existing
        # reuse-after-error contract of the persistent shared daemon; rationale
        # in the report + tests.)
        async with self._spawn_lock:
            current_key = runtime_cursor_key(self._settings)
            if self._proc is not None and self._proc.returncode is None:
                if current_key == self._spawn_key:
                    return self._proc
                # b12: the cursor key changed. Killing the daemon NOW would EOF every OTHER
                # conversation's in-flight stream on the shared daemon ("bridge process closed
                # mid-response"). Defer the rotation while any run queue is active (numeric rids;
                # "ping" excluded) or a turn has claimed the proc but not yet registered its
                # rid (``_claims_pending`` — the gap between this return and
                # ``_stream_run_once``'s ``_register_queue`` under ``_write_lock``) → those
                # streams finish on the old key; the next idle _ensure_proc (key still
                # differs, no active runs/claims) performs the kill+respawn.
                #
                # ``own_claim``: the CALLING turn (``_stream_run_once``) increments
                # ``_claims_pending`` BEFORE calling us, so its own claim is already
                # counted here. Rotation is safe to do FOR the caller (it gets the
                # fresh daemon and writes to it), so exclude the caller's own claim
                # from the defer test — otherwise the guard always sees >=1 and the
                # kill+respawn is NEVER reached on the production stream path (a saved
                # new key stays dead until a full server restart). Only OTHER pending
                # claims / active numeric-rid runs must defer the rotation.
                other_claims = self._claims_pending - (1 if own_claim else 0)
                if other_claims > 0 or any(rid.isdigit() for rid in self._queues):
                    return self._proc
                log.info("bridge daemon restarting: cursor api key changed")
                await self._kill_proc_unlocked()
            # Self-exit path: the previous daemon died on its own (returncode set —
            # idle crash, or the stdin-write BrokenPipe path that raises without
            # cleanup) so ``_kill_proc_unlocked`` (which releases the token) was NOT
            # run. Release the OLD token's pid file before ``_proc_token`` is
            # overwritten below — otherwise a stale ``run/llm/<token>.json`` leaks
            # for the server session and the next boot's reaper can force-kill an
            # unrelated process on a recycled pid.
            if self._proc_token is not None:
                release_llm_process(self._settings.data_dir, self._proc_token)
                self._proc_token = None
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    *self._daemon_args(),
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=bridge_env(self._settings),
                    cwd=str(self._settings.bridge_dir),
                    start_new_session=True,  # own process group → killpg takes down the whole tree
                )
            except (FileNotFoundError, NotADirectoryError) as e:
                # `node` is not on PATH → a raw WinError 2 / ENOENT otherwise. Name
                # the real missing dependency instead (mirrors claude_provider's
                # "claude CLI not found" 503).
                raise node_missing_error() from e
            self._spawn_key = current_key
            self._proc_token = uuid.uuid4().hex
            register_llm_process(
                self._settings.data_dir, self._proc_token, self._proc.pid, "cursor_bridge"
            )
            assert self._proc.stdin and self._proc.stdout
            # Masked cursor key in the start log: lets the user confirm WHICH key
            # the daemon launched with when diagnosing "Invalid User API Key" — the
            # raw value never appears (mask_hint → "…last4" or "set").
            log.info(
                "bridge daemon started pid=%s cursor_key=%s",
                self._proc.pid,
                mask_hint(current_key) if current_key else "MISSING",
            )
            # (Re)start the demultiplexer for this process.
            await self._start_reader(self._proc)
            # P0: DRAIN stderr — when unread stderr fills the 64KB pipe, the
            # daemon blocks INDEFINITELY on the write syscall → stdout stalls →
            # ALL turns fall into idle-timeout (the "hangs every day" profile).
            # Fire-and-forget; once the process dies, the task ends on its own at
            # stderr EOF.
            if self._proc.stderr is not None:
                asyncio.create_task(self._drain_stderr(self._proc.stderr, self._proc.pid))
            try:
                await self._ping_unlocked()
            except Exception:
                # The freshly spawned daemon never answered its ping → it is wedged or
                # half-up. Tear it down (group-kill + cancel the reader + clear the pid)
                # BEFORE re-raising, so the next turn spawns a clean daemon instead of
                # reusing one that leaks its proc/reader/pid and never responds.
                await self._kill_proc_unlocked()
                raise
            return self._proc

    def _register_queue(self, rid: str) -> asyncio.Queue[Any]:
        """Open a queue for rid + flush any pending events that arrived early for it.

        If the reader has already seen EOF for this process (e.g. the daemon died
        mid-response and this rid's consumer registered late), ``_STREAM_EOF`` is
        pushed onto the queue immediately — so the late consumer does not wait
        forever.
        """
        q: asyncio.Queue[Any] = asyncio.Queue()
        self._queues[rid] = q
        for ev in self._pending.pop(rid, []):
            q.put_nowait(ev)
        if self._eof_seen:
            q.put_nowait(_STREAM_EOF)
        return q

    def _release_queue(self, rid: str) -> None:
        self._queues.pop(rid, None)
        self._pending.pop(rid, None)

    async def _start_reader(self, proc: asyncio.subprocess.Process) -> None:
        """Task that reads the daemon stdout and dispatches lines to id-based queues."""
        # Cancel the previous reader (if any) — a new process means new stdout.
        # AWAIT it before resetting state below: its ``finally`` unconditionally
        # sets ``_eof_seen = True`` and pushes ``_STREAM_EOF`` into every queue
        # (bridge_pool.py _read_loop) — if that runs AFTER the reset it poisons
        # the freshly spawned daemon's state (a stray ping-queue EOF makes
        # ``_ping_unlocked`` fail and ``_ensure_proc`` kills the healthy daemon).
        # Mirrors ``_kill_proc_unlocked``, which already awaits the reader.
        if self._reader is not None and not self._reader.done():
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):  # pragma: no cover
                pass
        # New process → reset the EOF flag + stale pending.
        self._eof_seen = False
        self._pending.clear()
        self._reader = asyncio.create_task(self._read_loop(proc))

    async def _drain_stderr(self, stream: asyncio.StreamReader, pid: int) -> None:
        """Continuously drain the daemon stderr (so the pipe does not fill and block the daemon).

        Cursor SDK/Node warnings flow to stderr; if no one reads them, the 64KB
        pipe fills, the daemon's write blocks indefinitely → stdout stalls, every
        turn falls into idle-timeout. Uses ``read(4096)`` (which also removes
        readline's line-limit risk); ends silently at EOF once the process dies.
        """
        try:
            while True:
                chunk = await stream.read(4096)
                if not chunk:
                    break
                text = chunk.decode("utf-8", "replace")
                for raw_line in text.splitlines():
                    line = raw_line.strip()
                    if line:
                        # Keep a tail for the "died mid-response" path AND log it.
                        self._stderr_tail.append(line)
                        log.debug("bridge daemon stderr [pid=%s]: %s", pid, line)
        except Exception:  # drain best-effort — must never break the turn
            pass

    async def _read_loop(self, proc: asyncio.subprocess.Process) -> None:
        """Single reader: dispatches each NDJSON line to its queue by ``id``.

        If the process dies (stdout EOF), it pushes an EOF sentinel onto every
        waiting queue so each stream falls into the "died without a terminal
        event" path.
        """
        assert proc.stdout
        try:
            while True:
                try:
                    # Wait INDEFINITELY: the persistent daemon may stay silent for
                    # a long time BETWEEN turns; the per-turn timeout lives in the
                    # queue ``wait_for`` in stream_run. The reader exits only at a
                    # REAL EOF (process died) or on cancel (process rotation) — it
                    # does not exit on idle timeout and leave the pool without a
                    # reader.
                    line = await base.read_ndjson_line(proc.stdout, timeout=None)
                except asyncio.LimitOverrunError:
                    # A single line exceeded the limit: skip it, keep the stream going.
                    continue
                if not line:
                    break  # EOF: daemon died
                try:
                    ev = json.loads(line.decode("utf-8", errors="replace").strip() or "null")
                except json.JSONDecodeError:
                    continue
                if not isinstance(ev, dict):
                    continue
                rid = ev.get("id")
                if rid is None:
                    continue
                rid = str(rid)
                q = self._queues.get(rid)
                if q is not None:
                    q.put_nowait(ev)
                elif ev.get("ev") != "pong":
                    # ``pong`` = control ack (abort_run/close_session/shutdown):
                    # these ops do NOT REGISTER a queue → they have no consumer,
                    # so don't buffer them (previously they fell into _pending and
                    # were never drained → leak).
                    # Others: an early event arriving before the queue is opened
                    # (a legitimate race). Buffer it but CAP THE TOTAL — late
                    # done/error events for an rid released after a cancel must
                    # not grow _pending without bound (persistent daemon = server
                    # lifetime).
                    pend = self._pending
                    if rid not in pend and len(pend) >= _PENDING_RID_CAP:
                        pend.pop(next(iter(pend)), None)
                    pend.setdefault(rid, []).append(ev)
        except asyncio.CancelledError:  # pragma: no cover - process rotation
            raise
        except Exception:  # pragma: no cover - the reader must never die silently
            log.exception("bridge daemon reader loop crashed")
        finally:
            # This process died: late-registered queues should get EOF immediately too.
            self._eof_seen = True
            # Wake every waiting stream: it ended without a terminal event.
            for q in list(self._queues.values()):
                q.put_nowait(_STREAM_EOF)

    async def _kill_proc_unlocked(self) -> None:
        """Kill the current daemon with its process group + cancel the reader + clear the pid."""
        proc = self._proc
        if self._reader is not None and not self._reader.done():
            self._reader.cancel()
            try:
                await self._reader
            except (asyncio.CancelledError, Exception):  # pragma: no cover
                pass
        self._reader = None
        if proc is not None and proc.returncode is None:
            # killpg takes down the ENTIRE tree (the real cursor/node child
            # processes); also close the asyncio handle so proc.wait() resolves
            # and transport fds don't leak.
            await terminate_process_group(proc.pid)
            try:
                proc.kill()
            except (ProcessLookupError, Exception):  # pragma: no cover - already dead
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except (TimeoutError, Exception):  # pragma: no cover - already dead/race
                pass
        if self._proc_token is not None:
            release_llm_process(self._settings.data_dir, self._proc_token)
            self._proc_token = None
        self._proc = None

    async def _ping_unlocked(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdin and proc.stdout
        rid = "ping"
        q = self._register_queue(rid)
        try:
            proc.stdin.write((json.dumps({"id": rid, "op": "ping"}) + "\n").encode("utf-8"))
            await proc.stdin.drain()
            try:
                ev = await asyncio.wait_for(q.get(), timeout=30.0)
            except TimeoutError as e:
                raise LLMCallError(await self._ping_failure_msg(), status_code=503) from e
            if ev is _STREAM_EOF or not isinstance(ev, dict) or ev.get("ev") != "pong":
                raise LLMCallError(await self._ping_failure_msg(), status_code=503)
        finally:
            self._release_queue(rid)

    async def _ping_failure_msg(self) -> str:
        """The freshly spawned daemon never answered its ping — most often because
        node crashed at import (e.g. ``cursor_bridge/node_modules`` missing →
        ERR_MODULE_NOT_FOUND). Route through the shared mapper so that cause surfaces
        as the actionable ``cd cursor_bridge && npm install`` hint instead of the
        opaque generic line; the drained stderr already holds the crash message."""
        # Give stderr a beat to drain the crash line if the proc just died.
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except (TimeoutError, Exception):
                pass
        exit_code = proc.returncode if proc is not None else None
        return _died_midresponse_error(exit_code, list(self._stderr_tail))

    async def _write_bridge_op(self, op: str, session_key: str, req_id: str) -> None:
        async with self._write_lock:
            if self._proc is None or self._proc.returncode is not None:
                return
            assert self._proc.stdin
            self._proc.stdin.write(
                (
                    json.dumps(
                        {"id": req_id, "op": op, "session_key": session_key}
                    )
                    + "\n"
                ).encode("utf-8")
            )
            await self._proc.stdin.drain()

    async def abort_run(self, session_key: str) -> None:
        """Interrupt the running Cursor run; agent + agent_id are preserved (like IDE STOP)."""
        sk = (session_key or "").strip()
        if not sk:
            return
        await self._write_bridge_op("abort_run", sk, "abort")

    async def close_session(self, session_key: str) -> None:
        """Hard reset: cancel the run + close the bridge agent session."""
        sk = (session_key or "").strip()
        if not sk:
            return
        await self._write_bridge_op("close_session", sk, "close")

    async def stream_run(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        """Stream one bridge run; on orphan active-run, close session and retry once."""
        attempt = 0
        while attempt < 2:
            attempt += 1
            try:
                async for ev in self._stream_run_once(payload):
                    yield ev
                return
            except LLMCallError as e:
                sk = _payload_session_key(payload)
                if attempt >= 2 or not _is_active_run_message(e.message):
                    raise
                if sk:
                    try:
                        await self.abort_run(sk)
                    except Exception:  # pragma: no cover - cleanup best-effort
                        log.warning(
                            "abort_run after active-run failed (conv=%s)",
                            sk,
                            exc_info=True,
                        )
                log.info("bridge active-run aborted; retrying conv=%s", sk or "?")

    async def _stream_run_once(self, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        # NetworkEngine F0: circuit breaker for the daemon stream (no retry). If
        # open, fast-fail WITHOUT WRITING the request to the daemon; when the
        # stream finishes it counts as success, on error (daemon death/timeout)
        # as failure.
        from akana_server.network import load_network_config
        from akana_server.network.guard import global_registry

        _net_cfg = load_network_config(self._settings)
        _breaker = None
        if _net_cfg.breaker_enabled:
            # get_or_create binds threshold/cooldown at creation without mutating the
            # shared registry defaults (the old configure()+get() pair flip-flopped the
            # defaults across providers and never retuned the existing breaker).
            _breaker = global_registry().get_or_create(
                "bridge_daemon",
                threshold=_net_cfg.breaker_threshold,
                cooldown=_net_cfg.breaker_cooldown,
            )
            _breaker.before_call()  # BreakerOpenError if open

        # The process is ready (spawn is serialized); then the queue for this
        # request is set up and the stdin write is done under a short lock. The
        # LONG read loop lives in the demultiplexer — other conversations can
        # progress in parallel.
        #
        # Register the claim BEFORE ``_ensure_proc`` so that a concurrent
        # key-change ``_ensure_proc`` (which checks ``_claims_pending`` under the
        # ``_spawn_lock``) defers its kill+respawn until this turn has finished
        # writing — the daemon is never rotated out from under a turn that already
        # holds a stdin handle to the old process.
        self._claims_pending += 1
        try:
            proc = await self._ensure_proc(own_claim=True)
            assert proc.stdin and proc.stdout
            async with self._write_lock:
                self._req_counter += 1
                rid = str(self._req_counter)
                q = self._register_queue(rid)
                body = {**payload, "id": rid, "op": "run", "stream": True}
                try:
                    proc.stdin.write((json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8"))
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionResetError, OSError) as e:
                    # The daemon died before/while we handed it this request (the pipe is
                    # gone). Release this rid's queue and surface the REAL cause via the
                    # same mapper the mid-response death path uses — otherwise a raw
                    # OSError bubbles up uncaught and the queue leaks. ``_ensure_proc``
                    # respawns a fresh daemon on the next turn (returncode is set).
                    self._release_queue(rid)
                    if _breaker is not None:
                        _breaker.record_failure()
                    raise LLMCallError(
                        _died_midresponse_error(proc.returncode, list(self._stderr_tail)),
                        status_code=503,
                    ) from e
        finally:
            # The rid is now registered in ``self._queues`` (visible to the rotation
            # guard in ``_ensure_proc``) or the claim failed outright — either way this
            # turn no longer needs the ``_claims_pending`` placeholder.
            self._claims_pending -= 1

        # Single decode implementation shared with the DIRECT cursor path
        # (cursor_provider.run_stream). This loop owns only the daemon transport:
        # id-multiplexed queue reads, the EOF sentinel, and the shared-daemon
        # idle-timeout → abort_run behaviour. The per-event decode + terminal
        # assembly (delta / tool merge / usage_live / done / error / need_history)
        # lives in the decoder. The daemon path is CANONICAL: agent_id is embedded
        # in the terminal done event and the loop BREAKS on the terminal event.
        # ``_payload_model`` drives the live cost estimate (Cursor usage carries no
        # cost field, so it is estimated from the active model tag).
        decoder = base.CursorStreamDecoder(model=payload.get("model"))

        # Hang protection: the per-turn idle ceiling is ``min(bridge_timeout,
        # llm_idle_timeout)``. Because wait_for is re-established AFTER EVERY
        # chunk, the counter resets naturally — only REAL silence between two
        # chunks (the daemon stopped producing chunks and hung) triggers it. A
        # ``0`` knob ⇒ ceiling disabled ⇒ the old generous ``bridge_timeout``
        # still applies as-is.
        idle_timeout = base.idle_timeout(self._settings)
        try:
            while True:
                try:
                    # A non-positive ``idle_timeout`` (0/negative) means "no idle
                    # ceiling": ``combine_cap`` yields 0 for the disabled knob
                    # (e.g. ``CURSOR_BRIDGE_TIMEOUT=0``), and passing 0 straight
                    # to ``wait_for`` would time out INSTANTLY → every turn would
                    # fail with a false 504 on the first chunk. Wait indefinitely
                    # instead (mirror ``base.read_ndjson_line`` /
                    # ``claude_provider._read_line``).
                    if idle_timeout and idle_timeout > 0:
                        item = await asyncio.wait_for(q.get(), timeout=idle_timeout)
                    else:
                        item = await q.get()
                except TimeoutError as e:
                    if _breaker is not None:
                        _breaker.record_failure()
                    # The idle ceiling was exceeded (the daemon stopped producing
                    # chunks and hung). The PERSISTENT daemon is SHARED (other
                    # conversations may be running concurrently) → we do NOT kill
                    # the process group; only the existing ``abort_run`` path that
                    # interrupts THIS run (like IDE STOP: the agent is preserved so
                    # the next message doesn't get an "already-running response"
                    # error). On the DIRECT cursor/claude paths the process is
                    # singular so it's killpg'd; here it's shared, so cancelling
                    # the run is the correct equivalent.
                    session_key = _payload_session_key(payload)
                    if session_key:
                        try:
                            await self.abort_run(session_key)
                        except Exception:  # pragma: no cover - cleanup best-effort
                            log.warning(
                                "abort_run after bridge timeout failed (conv=%s)",
                                session_key,
                                exc_info=True,
                            )
                    raise LLMCallError(
                        "LLM_TIMEOUT: bridge daemon timed out", status_code=504
                    ) from e
                if item is _STREAM_EOF:
                    break  # daemon died / no terminal event arrived
                for out in decoder.feed(item):
                    yield out
                if decoder.terminal is not None:
                    break  # done / error / need_history — break-on-done (canonical)
        except asyncio.CancelledError:
            # STOP / turn cancel: like the Cursor IDE, only the run is interrupted; the agent stays.
            session_key = _payload_session_key(payload)
            if session_key:
                try:
                    await self.abort_run(session_key)
                except Exception:  # pragma: no cover
                    log.warning(
                        "abort_run after stream cancel failed (conv=%s)",
                        session_key,
                        exc_info=True,
                    )
            raise
        finally:
            self._release_queue(rid)

        if decoder.bridge_error is not None:
            if _breaker is not None:
                _breaker.record_failure()
            if _is_active_run_bridge_error(decoder.bridge_error):
                sk = _payload_session_key(payload)
                if sk:
                    try:
                        await self.abort_run(sk)
                    except Exception:  # pragma: no cover
                        log.warning(
                            "abort_run after active-run error failed (conv=%s)",
                            sk,
                            exc_info=True,
                        )
            raise LLMCallError(_friendly_bridge_error(decoder.bridge_error))

        if decoder.terminal == "need_history":
            # The decoder already yielded ``{"need_history_bootstrap": True}`` and the
            # aggregator breaks on it (retries with bootstrapped history) — do NOT also
            # emit a trailing empty ``done`` (unified with cursor_provider.run_stream).
            if _breaker is not None:
                _breaker.record_success()
            return

        if decoder.terminal is None:
            # No EOF/terminal: the daemon died mid-response. Don't fabricate a
            # fake "empty success" — clean up the process so the next turn starts
            # with a fresh daemon; return an explanation to the user.
            log.warning(
                "bridge daemon died mid-stream (no terminal event); exit=%s stderr_tail=%r",
                proc.returncode,
                list(self._stderr_tail)[-3:],
            )
            # Snapshot the cause BEFORE killing the process (returncode + the stderr
            # tail the drainer captured) so the user sees WHY it died, not the opaque
            # generic line. Read returncode before _kill_proc_unlocked resets state.
            exit_code = proc.returncode
            stderr_tail = list(self._stderr_tail)
            async with self._spawn_lock:
                if self._proc is proc:
                    # The daemon feeding this stream died/was left half-done —
                    # group-kill and clean up so the next turn opens a fresh daemon.
                    await self._kill_proc_unlocked()
            if _breaker is not None:
                _breaker.record_failure()
            raise LLMCallError(
                _died_midresponse_error(exit_code, stderr_tail), status_code=503
            )

        if _breaker is not None:
            _breaker.record_success()  # the stream finished cleanly
        # Daemon-path ``done.usage`` is coerced PLAIN (no model) → no ``cost_usd``:
        # backward-compat with the token-coercion contract. Cost is estimated live
        # during the stream (``usage_live`` above uses the payload model) and, on the
        # DIRECT ``run_stream`` path, added to the terminal usage there. The daemon-path
        # cost gap is documented in the CUR-4 report. ``done_event`` embeds agent_id
        # in the terminal event (the canonical semantics both transports now share).
        yield decoder.done_event(usage_to_tokens(decoder.usage))

    async def aclose(self) -> None:
        """Close the daemon with its process group (lifespan shutdown / rotation)."""
        async with self._spawn_lock:
            proc = self._proc
            if proc is not None and proc.stdin and proc.returncode is None:
                try:
                    proc.stdin.write(
                        (json.dumps({"id": "shutdown", "op": "shutdown"}) + "\n").encode(
                            "utf-8"
                        )
                    )
                    await proc.stdin.drain()
                except Exception:  # pragma: no cover - pipe already broken
                    pass
                if bridge_soft_shutdown_enabled():
                    for _ in range(50):
                        if proc.returncode is not None:
                            break
                        await asyncio.sleep(0.1)
                    if proc.returncode is None:
                        log.warning(
                            "bridge soft shutdown timed out — falling back to killpg"
                        )
                        await self._kill_proc_unlocked()
                    else:
                        log.info(
                            "bridge daemon soft shutdown complete (pid=%s code=%s)",
                            proc.pid,
                            proc.returncode,
                        )
                        self._proc = None
                        # Clean exit still leaves the pid file on disk; delete it
                        # like _kill_proc_unlocked so the next boot's reaper never
                        # acts on this (possibly recycled) pid.
                        if self._proc_token is not None:
                            release_llm_process(self._settings.data_dir, self._proc_token)
                            self._proc_token = None
                        if self._reader is not None and not self._reader.done():
                            self._reader.cancel()
                        self._reader = None
                        return
            await self._kill_proc_unlocked()


def _payload_session_key(payload: dict[str, Any]) -> str:
    return str(payload.get("session_key") or payload.get("conversation_id") or "").strip()


def _is_active_run_bridge_error(bridge_error: dict[str, Any]) -> bool:
    raw = str(bridge_error.get("error") or "").lower()
    return "already has active run" in raw or "active run" in raw


def _is_active_run_message(message: str) -> bool:
    low = (message or "").lower()
    return (
        "a response is already in progress" in low
        or "already has active run" in low
        or "active run" in low
    )


def _friendly_bridge_error(bridge_error: dict[str, Any]) -> str:
    """Map the daemon error event to a CLEAR + actionable message.

    Delegates to the shared :func:`errors.friendly_provider_error` — active-run,
    MODULE_NOT_FOUND (npm install), connection/timeout, etc. all in one place; the
    raw Node stack trace never leaks to the user. No fallback, only an understandable
    error (the user's decision).

    The daemon's ``normalizeError`` enriches the event with ``error_code`` and HTTP
    ``status`` (e.g. an auth failure carries ``status=401``); forward them so the
    mapper classifies by structural hint even when the raw text is ambiguous.
    """
    raw = str(bridge_error.get("error") or "cursor run failed")
    code = bridge_error.get("error_code")
    status = bridge_error.get("status")
    return friendly_provider_error(
        raw,
        provider="cursor",
        error_code=str(code) if code else None,
        status=status if isinstance(status, int) else None,
    )


def _stderr_has_known_cause(text: str) -> bool:
    """Does the daemon stderr carry a recognizable, user-actionable failure?

    Conservative on purpose: only STRONG markers (auth / missing module /
    connection) route to the friendly mapper. Anything else falls back to the
    generic message + last stderr line, so a benign Node warning is never
    mis-reported as the cause.
    """
    low = text.lower()
    return any(
        k in low
        for k in (
            "invalid user api key",
            "unauthorized",
            "forbidden",
            "api key",
            "module_not_found",
            "cannot find module",
            "econnrefused",
            "econnreset",
        )
    )


def _died_midresponse_error(exit_code: int | None, stderr_tail: list[str]) -> str:
    """Explain a daemon that died WITHOUT a terminal event.

    If the daemon left a recognizable cause on stderr (a Node crash carrying an
    auth / missing-module / connection error), surface THAT via the shared mapper —
    the real reason the user needs. Otherwise a short generic line, appended with the
    exit code + last stderr line for diagnosis (never a full Node stack trace).
    """
    tail_text = "\n".join(stderr_tail).strip()
    if tail_text and _stderr_has_known_cause(tail_text):
        return friendly_provider_error(tail_text, provider="cursor")
    # Genuine transient death (no recognizable cause): keep the original advice —
    # "send your message again" is correct here because the next turn opens a fresh
    # daemon (and the circuit breaker caps repeated hammering). Append the exit code
    # + last stderr line for diagnosis without leaking a full Node stack trace.
    exit_note = f" (exit code {exit_code})" if exit_code is not None else ""
    msg = (
        f"The Cursor bridge process closed mid-response{exit_note}; the bridge will"
        " restart automatically on the next message. Please send your message again."
    )
    last = stderr_tail[-1] if stderr_tail else ""
    if last:
        msg += f"\nLast bridge output: {last}"
    return msg


def bridge_daemon_enabled() -> bool:
    return os.environ.get("AKANA_BRIDGE_DAEMON", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def cursor_reuse_agent_enabled() -> bool:
    return os.environ.get("AKANA_REUSE_AGENT", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def bridge_soft_shutdown_enabled() -> bool:
    """Graceful shutdown: send a shutdown op to the daemon, don't force-fall to killpg.

    Enabled with ``AKANA_BRIDGE_SOFT_SHUTDOWN=1``. If the daemon closes via
    ``process.exit(0)``, the orphan reaper finds no live process on the next
    boot; Cursor cloud sessions continue to be resumed via the agent id in the
    DB.
    """
    return os.environ.get("AKANA_BRIDGE_SOFT_SHUTDOWN", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def get_bridge_pool(settings: Settings) -> BridgePool:
    """Return the module-singleton bridge pool (bound to the first caller's ``settings``).

    CAUTION: the pool permanently keeps the ``settings`` from the FIRST call.
    ``_ensure_proc`` only rotates the daemon when ``cursor_api_key`` changes;
    OTHER settings like ``bridge_dir`` / ``data_dir`` / timeout stay FROZEN until
    :func:`shutdown_bridge_pool` is called (and the pool is rebuilt). A caller
    that changes these fields must call ``shutdown_bridge_pool()`` first so the
    new ``settings`` take effect.
    """
    global _pool
    if _pool is None:
        _pool = BridgePool(settings)
    return _pool


async def shutdown_bridge_pool() -> None:
    global _pool
    if _pool is None:
        return
    try:
        await _pool.aclose()
    except Exception:  # pragma: no cover - shutdown must never raise
        log.warning("bridge pool shutdown encountered an error", exc_info=True)
    _pool = None


__all__ = [
    "BridgePool",
    "bridge_daemon_enabled",
    "bridge_soft_shutdown_enabled",
    "cursor_reuse_agent_enabled",
    "get_bridge_pool",
    "shutdown_bridge_pool",
]
