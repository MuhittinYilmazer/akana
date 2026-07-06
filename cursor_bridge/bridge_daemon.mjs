#!/usr/bin/env node
/**
 * Long-lived Cursor SDK bridge — avoids per-message Node cold start.
 *
 * Protocol (stdin/stdout NDJSON, one JSON object per line):
 *   Request:  { id, op:"ping"|"run"|"shutdown", ...run fields }
 *   Response: { id, ev:"pong"|"meta"|"delta"|"tool"|"done"|"error"|"timing", ... }
 */
import readline from "node:readline";
import {
  acquireAgent,
  buildUserMessage,
  closeCachedSession,
  explicitSessionKeyFrom,
  historyForAgent,
  languageFrom,
  makeOnDelta,
  normalizeError,
  resultError,
  sessionKeyFrom,
  waitWithHeartbeat,
} from "./lib.mjs";

/** @type {Map<string, { agent: import("@cursor/sdk").SDKAgent, agentId: string, model: string, cwd: string, mcpKey: string }>} */
const sessions = new Map();
/** The run active per session — cancelled via closeSession. */
const sessionActiveRun = new Map();
/** The inflight gate per session — resolved via closeSession. */
const sessionInflight = new Map();
/**
 * STOP/abort that arrives WHILE ``agent.send()`` is still setting up — before the run
 * is registered in ``sessionActiveRun`` — would otherwise be lost. We record the intent
 * here TAGGED with the turn id it targets; the setup path cancels the run the moment it
 * exists. Keyed by session → the mid-setup turn's id. Tagging by turn id is load-bearing:
 * a later turn B must not delete an intent that targeted an earlier turn A (that would let
 * the STOPped prompt A run to completion). @type {Map<string, string>} */
const sessionAbortIntent = new Map();

function emit(id, obj) {
  process.stdout.write(JSON.stringify({ id, ...obj }) + "\n");
}

function clearSessionInflight(sessionKey) {
  if (!sessionKey) return;
  const inflight = sessionInflight.get(sessionKey);
  if (inflight?.settle) {
    try {
      inflight.settle();
    } catch {
      /* ignore */
    }
  }
  sessionInflight.delete(sessionKey);
}

/** Like the Cursor IDE STOP: cut only the running run; the agent session stays. */
async function abortActiveRun(sessionKey) {
  if (!sessionKey) return false;
  const activeRun = sessionActiveRun.get(sessionKey);
  if (activeRun) {
    // AWAIT the cancel: it was fire-and-forget before, so the pong (and any retry)
    // could race ahead of the SDK actually releasing the run → "agent already has an
    // active run" on the immediate retry. (``abort()`` never existed on a run — dead.)
    try {
      await activeRun.cancel?.();
    } catch {
      /* ignore */
    }
    sessionActiveRun.delete(sessionKey);
    // Run-registered branch: the run is torn down, so settle+drop the inflight gate
    // now — that is what serializes the immediate stream retry.
    clearSessionInflight(sessionKey);
  } else {
    const inflight = sessionInflight.get(sessionKey);
    if (inflight) {
      // A ``send()`` is mid-setup (the run is not registered yet). Record the intent
      // TAGGED with the setting-up turn's id so its C6 check cancels the run as soon as
      // it is created (see runStreaming). b13: gate on sessionInflight — a stray abort on
      // a fully-idle session must NOT leave a permanent marker (the map would grow
      // unbounded). Do NOT clearSessionInflight here: settling the gate would let an
      // immediately-resent turn B skip the :94 serialization loop and delete this intent,
      // so the STOPped turn A would still run to completion. Keep the gate live — B waits
      // until A's setup window resolves and A honors its own intent.
      if (inflight.id != null) sessionAbortIntent.set(sessionKey, inflight.id);
    }
  }
  return Boolean(activeRun);
}

/** Hard reset: cancel the run + close the agent (chat deletion / unrecoverable stuck). */
async function closeSession(sessionKey) {
  await abortActiveRun(sessionKey);
  // Hard reset (chat deletion / unrecoverable stuck): unlike a plain STOP, tear the
  // session down fully — settle any mid-setup inflight gate and drop lingering intent
  // (b13) so nothing waits on / references a session that no longer exists.
  clearSessionInflight(sessionKey);
  sessionAbortIntent.delete(sessionKey);
  closeCachedSession(sessions, sessionKey);
}

async function getOrCreateAgent(id, input) {
  return acquireAgent(input, {
    sessions,
    emitTiming: (obj) => emit(id, obj),
  });
}

async function runStreaming(id, input) {
  const sessionKey = sessionKeyFrom(input);
  while (sessionInflight.has(sessionKey)) {
    const prev = sessionInflight.get(sessionKey);
    try {
      await (prev?.promise ?? prev);
    } catch {
      /* the previous turn ended with an error */
    }
    if (!sessionInflight.has(sessionKey)) break;
  }

  let settleInflight;
  const inflightGate = new Promise((resolve) => {
    settleInflight = resolve;
  });
  // Tag the inflight gate with THIS turn's id so a mid-setup abort records an intent
  // that targets this turn specifically (not whichever turn later runs on the session).
  sessionInflight.set(sessionKey, { promise: inflightGate, settle: settleInflight, id });

  let run;
  try {
    // Fresh run for this session → drop any STALE abort-intent left by a PRIOR turn so
    // only a STOP that targets THIS run (during the setup window below) counts. Guard on
    // the turn id: an abort that arrived for THIS turn between the gate set above and here
    // must NOT be wiped (that intent targets our id and our C6 check below honors it).
    if (sessionAbortIntent.get(sessionKey) !== id) {
      sessionAbortIntent.delete(sessionKey);
    }
    const agentResult = await getOrCreateAgent(id, input);
    if (agentResult.needHistory) {
      emit(id, { ev: "need_history" });
      return;
    }
    const { agent, reuse, needsHistoryBootstrap } = agentResult;
    const message = buildUserMessage(
      String(input.prompt || ""),
      historyForAgent(input, needsHistoryBootstrap),
      typeof input.system === "string" ? input.system : "",
      languageFrom(input),
    );

    emit(id, { ev: "meta", run_id: null, agent_id: agent.agentId, model: input.model || "composer-2" });

    let acc = "";
    let usage = null;
    let runId = null;
    let agentId = agent.agentId;
    const tSend = Date.now();

    try {
      run = await agent.send(message, {
        onDelta: makeOnDelta((obj) => emit(id, obj), {
          onText: (text) => {
            if (!acc) {
              emit(id, { ev: "timing", phase: "ttft_ms", ms: Date.now() - tSend });
            }
            acc += text;
          },
          onUsage: (u) => {
            usage = u;
          },
          language: languageFrom(input),
        }),
      });
      sessionActiveRun.set(sessionKey, run);
      runId = run.id;
      agentId = run.agentId || agentId;
      emit(id, { ev: "meta", run_id: runId, agent_id: agentId, model: input.model || "composer-2" });
      // C6: a STOP/abort that landed while ``agent.send()`` was still setting up could
      // not see this run (it wasn't registered yet). Honor it now — but ONLY if the
      // recorded intent targets THIS turn's id (a later turn's intent must not cancel
      // this run, and vice versa). Cancel immediately; waitWithHeartbeat then returns
      // the cancelled result and the normal done/error path emits the terminal event.
      if (sessionAbortIntent.get(sessionKey) === id) {
        sessionAbortIntent.delete(sessionKey);
        try {
          await run.cancel?.();
        } catch {
          /* ignore */
        }
      }
      const result = await waitWithHeartbeat(run, (obj) => emit(id, obj));
      // run.wait() RESOLVES (not rejects) on a server/SDK-side failure with
      // status:"error"/"cancelled" — surface the real cause as an error event
      // instead of a fake ok:true done (which dropped result.error and reported
      // an empty/truncated turn as success).
      const runErr = resultError(result);
      if (runErr) {
        emit(id, { ev: "error", ok: false, ...runErr, run_id: result?.id || runId, agent_id: agentId });
      } else {
        const text = (acc || String(result?.result || "")).trim();
        emit(id, {
          ev: "done",
          ok: true,
          text,
          status: result?.status || "finished",
          run_id: result?.id || runId,
          usage,
          agent_id: agentId,
        });
      }
      if (reuse) {
        const cached = sessions.get(sessionKey);
        if (cached) cached.agentId = agentId;
      } else {
        try {
          agent.close?.();
        } catch {
          /* ignore */
        }
      }
    } catch (e) {
      const norm = normalizeError(e);
      emit(id, { ev: "error", ok: false, ...norm, run_id: runId, agent_id: agentId });
      if (!reuse) {
        try {
          agent.close?.();
        } catch {
          /* ignore */
        }
      }
    }
  } finally {
    if (sessionActiveRun.get(sessionKey) === run) {
      sessionActiveRun.delete(sessionKey);
    }
    settleInflight?.();
    const inflight = sessionInflight.get(sessionKey);
    if (inflight?.settle === settleInflight) {
      sessionInflight.delete(sessionKey);
    }
  }
}

async function runOneShot(id, input) {
  const agentResult = await getOrCreateAgent(id, input);
  if (agentResult.needHistory) {
    emit(id, { ev: "need_history" });
    return;
  }
  const { agent, reuse, needsHistoryBootstrap } = agentResult;
  const message = buildUserMessage(
    String(input.prompt || ""),
    historyForAgent(input, needsHistoryBootstrap),
    typeof input.system === "string" ? input.system : "",
    languageFrom(input),
  );
  const tSend = Date.now();
  let usage = null;
  let acc = "";

  try {
    const run = await agent.send(message, {
      onDelta: makeOnDelta(() => {}, {
        onText: (text) => {
          acc += text;
        },
        onUsage: (u) => {
          usage = u;
        },
        language: languageFrom(input),
      }),
    });
    const result = await run.wait();
    const runErr = resultError(result);
    if (runErr) {
      emit(id, {
        ev: "error",
        ok: false,
        ...runErr,
        run_id: result?.id || run.id,
        agent_id: result?.agentId || run.agentId || agent.agentId,
      });
    } else {
      const text = (acc || String(result?.result || "")).trim();
      emit(id, {
        ev: "done",
        ok: true,
        text,
        status: result?.status || "finished",
        run_id: result?.id || run.id,
        usage,
        agent_id: result?.agentId || run.agentId || agent.agentId,
        timing_ms: Date.now() - tSend,
      });
    }
  } catch (e) {
    emit(id, { ev: "error", ok: false, ...normalizeError(e) });
  } finally {
    if (!reuse) {
      try {
        agent.close?.();
      } catch {
        /* ignore */
      }
    }
  }
}

async function handleRequest(line) {
  let req;
  try {
    req = JSON.parse(line);
  } catch (e) {
    emit("?", { ev: "error", ok: false, error: `invalid json: ${e.message}` });
    return;
  }
  const id = req.id ?? "?";
  const op = req.op || "run";

  if (op === "ping") {
    emit(id, { ev: "pong" });
    return;
  }
  if (op === "shutdown") {
    // Best-effort cancel of any in-flight runs, then exit. NOT awaited: process.exit
    // tears everything down regardless, and awaiting would only delay shutdown.
    for (const key of [...sessions.keys()]) void closeSession(key);
    emit(id, { ev: "pong", shutting_down: true });
    process.exit(0);
  }
  if (op === "abort_run") {
    const sk = explicitSessionKeyFrom(req);
    // ACK synchronously: the pong is a courtesy (the Python side does not read it) and a
    // fast-following shutdown must not lose it. The real cancel is awaited below — inside
    // abortActiveRun the inflight gate is cleared only AFTER the run is torn down, which is
    // what actually serializes the stream retry (not the pong timing).
    const hadRun = sk ? sessionActiveRun.has(sk) : false;
    emit(id, { ev: "pong", aborted: sk || null, had_run: hadRun });
    if (sk) await abortActiveRun(sk);
    return;
  }
  if (op === "close_session") {
    const sk = explicitSessionKeyFrom(req);
    emit(id, { ev: "pong", closed: sk || null });
    if (sk) await closeSession(sk);
    return;
  }
  if (op === "run") {
    const prompt = String(req.prompt || "").trim();
    if (!prompt) {
      emit(id, { ev: "error", ok: false, error: "empty prompt" });
      return;
    }
    try {
      if (req.stream) {
        await runStreaming(id, req);
      } else {
        await runOneShot(id, req);
      }
    } catch (e) {
      // Errors that ESCAPE runStreaming/runOneShot — notably agent creation / auth
      // failures in getOrCreateAgent, which run OUTSIDE their inner try/catch — must
      // be emitted with the REAL request id so the Python reader routes them to the
      // waiting consumer. The old fallback (rl.on("line") → emit("?", …)) tagged such
      // errors id:"?", so the consumer never saw them and the turn degraded to the
      // generic "closed mid-response" instead of the real cause (e.g. "Invalid User
      // API Key" → "Cursor authentication failed — check your API key").
      emit(id, { ev: "error", ok: false, ...normalizeError(e) });
    }
    return;
  }
  emit(id, { ev: "error", ok: false, error: `unknown op: ${op}` });
}

// Last-resort process guards. An unhandled rejection or uncaught exception would
// otherwise take the daemon down SILENTLY (stdout EOF → the Python side reports the
// generic "closed mid-response" with no cause). Write a CLEAR stderr line — the bridge
// pool drains and now keeps a stderr tail, so the real reason reaches the user/log.
// unhandledRejection: log and STAY UP (one bad turn must not kill other sessions);
// installing the handler also prevents Node's default crash-on-unhandled-rejection.
process.on("unhandledRejection", (reason) => {
  const msg = reason?.stack || reason?.message || String(reason);
  process.stderr.write(`akana bridge_daemon unhandledRejection: ${msg}\n`);
});
process.on("uncaughtException", (err) => {
  // An uncaught exception leaves the process in an undefined state → exit after
  // surfacing it (Node best practice); the orphan reaper + next-turn respawn recover.
  process.stderr.write(`akana bridge_daemon uncaughtException: ${err?.stack || err?.message || err}\n`);
  process.exit(1);
});

const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on("line", (line) => {
  const trimmed = line.trim();
  if (!trimmed) return;
  handleRequest(trimmed).catch((e) => {
    emit("?", { ev: "error", ok: false, error: e?.message || String(e) });
  });
});
// stdin EOF = the parent Akana server is gone (it holds our only stdin writer).
// The daemon runs in its OWN process group (start_new_session) and caches live
// SDK agents that keep the Node event loop alive, so without this it survives a
// hard (non-aclose) server death — SIGKILL/OOM/crash — as an orphan until the
// NEXT boot's reaper finds it. Exit promptly on EOF so the parent's death takes
// the daemon (and its SDK children) with it. Canonical guard for a stdio daemon.
rl.on("close", () => process.exit(0));

process.stderr.write("akana bridge_daemon ready\n");
