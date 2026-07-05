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
 * here; the setup path cancels the run the moment it exists. Cleared at each run start.
 */
const sessionAbortIntent = new Set();

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
  } else if (sessionInflight.has(sessionKey)) {
    // A ``send()`` is mid-setup (the run is not registered yet). Record the intent so the setup
    // path cancels the run as soon as it is created (see runStreaming). b13: gate on
    // sessionInflight — a stray abort on a fully-idle session must NOT leave a permanent marker
    // (sessionAbortIntent would grow unbounded over the daemon's lifetime).
    sessionAbortIntent.add(sessionKey);
  }
  clearSessionInflight(sessionKey);
  return Boolean(activeRun);
}

/** Hard reset: cancel the run + close the agent (chat deletion / unrecoverable stuck). */
async function closeSession(sessionKey) {
  await abortActiveRun(sessionKey);
  sessionAbortIntent.delete(sessionKey); // b13: drop any lingering intent for a closed session
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
  sessionInflight.set(sessionKey, { promise: inflightGate, settle: settleInflight });

  let run;
  try {
    // Fresh run for this session → drop any stale abort-intent so only a STOP that
    // targets THIS run (during the setup window below) counts.
    sessionAbortIntent.delete(sessionKey);
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
        }),
      });
      sessionActiveRun.set(sessionKey, run);
      runId = run.id;
      agentId = run.agentId || agentId;
      emit(id, { ev: "meta", run_id: runId, agent_id: agentId, model: input.model || "composer-2" });
      // C6: a STOP/abort that landed while ``agent.send()`` was still setting up could
      // not see this run (it wasn't registered yet). Honor it now — cancel immediately;
      // waitWithHeartbeat then returns the cancelled result and the normal done/error
      // path emits the terminal event.
      if (sessionAbortIntent.delete(sessionKey)) {
        try {
          await run.cancel?.();
        } catch {
          /* ignore */
        }
      }
      const result = await waitWithHeartbeat(run, (obj) => emit(id, obj));
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
      }),
    });
    const result = await run.wait();
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

process.stderr.write("akana bridge_daemon ready\n");
