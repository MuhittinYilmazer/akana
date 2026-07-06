#!/usr/bin/env node
/**
 * Cursor SDK bridge. Reads one JSON object on stdin, writes to stdout.
 *
 * Input: { prompt, cwd, model?, history?, stream?, system?, session_key?,
 *   conversation_id?, reuse_agent?, cursor_agent_id?, chat_mode? }
 *
 * One-shot mode (stream omitted/false): a single JSON line, e.g.
 *   { ok:true, text, status, run_id, usage?, agent_id? }
 *
 * Streaming mode (stream:true): NDJSON, one event per line:
 *   { ev:"meta",   run_id, agent_id, model }
 *   { ev:"delta",  text }
 *   { ev:"tool",   phase:"start"|"end", call_id, name, args?, result?, status? }
 *   { ev:"need_history" }  — stale resume; caller should bootstrap history and retry
 *   { ev:"done",   ok:true, text, status, run_id, usage, agent_id? }
 *   { ev:"error",  ok:false, error, retryable?, run_id? }
 */
import { readFileSync } from "node:fs";
import {
  acquireAgent,
  buildUserMessage,
  historyForAgent,
  languageFrom,
  makeOnDelta,
  normalizeError,
  resultError,
  waitWithHeartbeat,
} from "./lib.mjs";

function readStdin() {
  return readFileSync(0, "utf8");
}

function emitLine(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

async function runStreaming(input) {
  const agentResult = await acquireAgent(input, { emitTiming: emitLine });
  if (agentResult.needHistory) {
    emitLine({ ev: "need_history" });
    return;
  }

  const { agent, needsHistoryBootstrap } = agentResult;
  const message = buildUserMessage(
    String(input.prompt || ""),
    historyForAgent(input, needsHistoryBootstrap),
    typeof input.system === "string" ? input.system : "",
    languageFrom(input),
  );

  emitLine({ ev: "meta", run_id: null, agent_id: agent.agentId, model: input.model || "composer-2" });

  let acc = "";
  let usage = null;
  let runId = null;
  let agentId = agent.agentId;
  const tSend = Date.now();

  try {
    const run = await agent.send(message, {
      onDelta: makeOnDelta(emitLine, {
        onText: (text) => {
          if (!acc) {
            emitLine({ ev: "timing", phase: "ttft_ms", ms: Date.now() - tSend });
          }
          acc += text;
        },
        onUsage: (u) => {
          usage = u;
        },
        language: languageFrom(input),
      }),
    });
    runId = run.id;
    agentId = run.agentId || agentId;
    emitLine({ ev: "meta", run_id: runId, agent_id: agentId, model: input.model || "composer-2" });
    const result = await waitWithHeartbeat(run, emitLine);
    const runErr = resultError(result);
    if (runErr) {
      emitLine({ ev: "error", ok: false, ...runErr, run_id: result?.id || runId, agent_id: agentId });
      process.exitCode = 1;
    } else {
      const text = (acc || String(result?.result || "")).trim();
      emitLine({
        ev: "done",
        ok: true,
        text,
        status: result?.status || "finished",
        run_id: result?.id || runId,
        usage,
        agent_id: agentId,
      });
    }
  } catch (e) {
    emitLine({ ev: "error", ok: false, ...normalizeError(e), run_id: runId, agent_id: agentId });
    process.exitCode = 1;
  } finally {
    if (input.reuse_agent === false) {
      try {
        await agent.close?.();
      } catch {
        /* noop */
      }
    }
  }
}

async function runOneShot(input) {
  const agentResult = await acquireAgent(input, { emitTiming: emitLine });
  if (agentResult.needHistory) {
    emitLine({ ev: "need_history" });
    return;
  }

  const { agent, needsHistoryBootstrap } = agentResult;
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
      emitLine({ ok: false, ...runErr });
      process.exitCode = 1;
    } else {
      const text = (acc || String(result?.result || "")).trim();
      emitLine({
        ok: true,
        text,
        status: result?.status || "finished",
        run_id: result?.id || run.id,
        agent_id: result?.agentId || run.agentId || agent.agentId,
        usage,
        timing_ms: Date.now() - tSend,
      });
    }
  } catch (e) {
    emitLine({ ok: false, ...normalizeError(e) });
    process.exitCode = 1;
  } finally {
    if (input.reuse_agent === false) {
      try {
        await agent.close?.();
      } catch {
        /* noop */
      }
    }
  }
}

async function main() {
  let input;
  try {
    input = JSON.parse(readStdin());
  } catch (e) {
    emitLine({ ok: false, error: `invalid stdin json: ${e.message}` });
    process.exitCode = 1;
    return;
  }

  const apiKey = process.env.CURSOR_API_KEY;
  if (!apiKey) {
    emitLine({ ok: false, error: "CURSOR_API_KEY not set" });
    process.exitCode = 1;
    return;
  }

  const prompt = String(input.prompt || "").trim();
  if (!prompt) {
    emitLine({ ok: false, error: "empty prompt" });
    process.exitCode = 1;
    return;
  }

  if (Boolean(input.stream)) {
    await runStreaming(input);
  } else {
    await runOneShot(input);
  }
}

await main();
