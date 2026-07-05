/**
 * Shared helpers for the Cursor SDK stdio bridges:
 *   - run_prompt.mjs    (one-shot process, fallback path)
 *   - bridge_daemon.mjs (persistent daemon, main path)
 *
 * Pure extraction — NDJSON event shapes, field names and error normalization
 * must stay byte-compatible with what the Python side parses.
 */
import "./dispose-polyfill.mjs"; // Node 18: define Symbol.dispose BEFORE the SDK loads
import { Agent, CursorAgentError } from "@cursor/sdk";

export function sessionKeyFrom(input) {
  return String(input.session_key || input.conversation_id || "default");
}

/** Explicit session key only — abort/close must not fall back to ``default``. */
export function explicitSessionKeyFrom(input) {
  const sk =
    typeof input.session_key === "string" ? input.session_key.trim() : "";
  if (sk) return sk;
  const cid =
    typeof input.conversation_id === "string" ? input.conversation_id.trim() : "";
  return cid || "";
}

export function historyForAgent(input, needsHistoryBootstrap) {
  if (!needsHistoryBootstrap) return [];
  return Array.isArray(input.history) ? input.history : [];
}

/** Active prompt language (``en`` | ``tr``) from the bridge payload; English-first. */
export function languageFrom(input) {
  const l = typeof input?.language === "string" ? input.language.trim().toLowerCase() : "";
  return l === "tr" ? "tr" : "en";
}

/**
 * History-framing wrapper around prior turns — bilingual, follows the active
 * ``language`` (en|tr). The label leaks into EVERY multi-turn prompt, so a
 * hardcoded language here biases replies toward that language regardless of the
 * user's setting (the bug: Turkish frame → Turkish replies in English mode).
 * MUST mirror ``claude_provider._HISTORY_FRAME`` byte-for-byte so both providers
 * frame history identically.
 */
const HISTORY_FRAME = {
  en: ["[Previous conversation — context only; do not continue/re-answer]", "[/Previous conversation]"],
  tr: ["[Önceki konuşma — yalnızca bağlam; sürdürme/yeniden yanıtlama]", "[/Önceki konuşma]"],
};

/** Model id from bridge payload (``thinking_mode`` is NOT a Cursor SDK knob). */
export function modelSelectionFromInput(input) {
  return { id: String(input.model || "composer-2") };
}

/**
 * Flatten system + chat history + prompt into a single user message.
 *
 * Prior turns go into a delimited *context* block (not a bare User:/Akana:
 * transcript) so the model treats them as past background; the new prompt
 * follows as the single message to answer. The open alternation otherwise
 * invites runaway self-dialogue — the model finishes then fabricates more
 * User:/Akana: turns. Mirrors claude_client._build_prompt (plus the System
 * block, which the Cursor SDK has no separate channel for).
 *
 * ``language`` (en|tr) selects the history-frame label so it does not bias the
 * reply language — a hardcoded Turkish frame made English-mode multi-turn chats
 * answer in Turkish (mirrors the claude_provider._HISTORY_FRAME fix).
 */
export function buildUserMessage(prompt, history, system, language = "en") {
  const lines = [];
  const sys = typeof system === "string" ? system.trim() : "";
  if (sys) lines.push(`System:\n${sys}`);
  const past = [];
  for (const m of history || []) {
    if (!m || typeof m.content !== "string") continue;
    const role = m.role === "assistant" ? "Akana" : m.role === "user" ? "User" : "System";
    past.push(`${role}: ${m.content}`);
  }
  if (past.length) {
    const [open, close] = HISTORY_FRAME[language] || HISTORY_FRAME.en;
    lines.push(`${open}\n${past.join("\n")}\n${close}`);
  }
  lines.push(prompt);
  return lines.join("\n\n");
}

/** Map an exception to the wire error fields ({ error, retryable? }). */
export function normalizeError(e) {
  if (e instanceof CursorAgentError) {
    // ``code``/``type`` (if present) is carried to the Python side for classification;
    // the raw ``message`` is preserved (if the subtype is unrecognized the friendly fallback uses it).
    const out = { error: e.message, retryable: Boolean(e.isRetryable) };
    const code = e.code ?? e.type ?? e.name;
    if (code) out.error_code = String(code);
    if (typeof e.status === "number") out.status = e.status;
    return out;
  }
  return { error: e?.message || String(e) };
}

/**
 * Wire shape for a streaming usage event — identical for the live estimate and
 * the real turn-ended usage so the Python side parses one shape. Mirrors the
 * Claude provider's per-delta ``usage_live`` cadence (run_prompt/bridge_daemon
 * both reuse this via :func:`makeOnDelta`).
 */
export function usageWireEvent(usage) {
  return { ev: "usage", usage: usage && typeof usage === "object" ? usage : {} };
}

/**
 * Find a usage-bearing object on an SDK update, tolerating shape drift.
 *
 * The Cursor SDK reliably attaches ``usage`` to the ``turn-ended`` update; some
 * builds also surface it on other updates. We accept ``update.usage`` and a few
 * snake/camel aliases so a future SDK that streams usage mid-turn lights up the
 * live counter without a bridge change. Returns ``null`` when none present.
 */
export function extractUsage(update) {
  if (!update || typeof update !== "object") return null;
  const u = update.usage ?? update.tokenUsage ?? update.token_usage;
  return u && typeof u === "object" ? u : null;
}

/** Extract mcp_servers from a request; undefined unless a plain object. */
export function mcpServersFrom(input) {
  const m = input.mcp_servers;
  return m && typeof m === "object" && !Array.isArray(m) ? m : undefined;
}

/**
 * Option object for Agent.create / Agent.resume / Agent.prompt.
 *
 * Legacy positional form (``{ apiKey, model, cwd, mcpServers }``) is kept for
 * callers that already resolved the model string. Prefer ``agentOptionsFromInput``
 * when the full bridge payload is available.
 */
export function agentOptions({ apiKey, model, cwd, mcpServers }) {
  return {
    apiKey,
    model: typeof model === "string" ? { id: model } : model,
    local: { cwd },
    ...(mcpServers ? { mcpServers } : {}),
  };
}

/** Full bridge payload → AgentOptions. */
export function agentOptionsFromInput(input, { apiKey }) {
  const cwd = String(input.cwd || process.cwd());
  const mcpServers = mcpServersFrom(input);
  return agentOptions({
    apiKey,
    model: modelSelectionFromInput(input),
    cwd,
    mcpServers,
  });
}

/**
 * Resume or create an agent for one run. When ``sessions`` is a Map (daemon),
 * in-memory reuse matches ``bridge_daemon.mjs``; when null (run_prompt) only
 * ``Agent.resume`` / create applies for this process lifetime.
 */
export async function acquireAgent(input, { sessions = null, emitTiming = () => {} } = {}) {
  const apiKey = process.env.CURSOR_API_KEY;
  const model = String(input.model || "composer-2");
  const cwd = String(input.cwd || process.cwd());
  const sessionKey = sessionKeyFrom(input);
  const reuse = input.reuse_agent !== false;
  const cursorAgentId =
    typeof input.cursor_agent_id === "string" ? input.cursor_agent_id.trim() : "";
  const mcpServers = mcpServersFrom(input);
  const mcpKey = JSON.stringify(mcpServers || null);
  const options = agentOptionsFromInput(input, { apiKey });

  if (sessions) {
    const cached = sessions.get(sessionKey);
    if (cached && cached.model === model && cached.cwd === cwd && cached.mcpKey === mcpKey && reuse) {
      emitTiming({ ev: "timing", phase: "agent_ready_ms", ms: 0, reused: "session" });
      return {
        agent: cached.agent,
        sessionKey,
        reuse,
        needsHistoryBootstrap: false,
      };
    }
    if (cached) {
      await closeCachedSession(sessions, sessionKey);
    }
  }

  const t0 = Date.now();
  let agent;
  let reused = "create";
  let needsHistoryBootstrap = true;

  if (reuse && cursorAgentId) {
    try {
      agent = await Agent.resume(cursorAgentId, options);
      reused = "resume";
      needsHistoryBootstrap = false;
    } catch {
      const hist = Array.isArray(input.history) ? input.history : [];
      if (hist.length === 0) {
        emitTiming({
          ev: "timing",
          phase: "agent_ready_ms",
          ms: Date.now() - t0,
          reused: "resume_failed",
        });
        return { needHistory: true, sessionKey, reuse, needsHistoryBootstrap: true };
      }
      agent = await Agent.create(options);
    }
  } else {
    agent = await Agent.create(options);
  }

  emitTiming({ ev: "timing", phase: "agent_ready_ms", ms: Date.now() - t0, reused });

  if (reuse && sessions) {
    sessions.set(sessionKey, { agent, agentId: agent.agentId, model, cwd, mcpKey });
  }

  return { agent, sessionKey, reuse, needsHistoryBootstrap };
}

export async function closeCachedSession(sessions, sessionKey) {
  if (!sessions || !sessionKey) return;
  const cached = sessions.get(sessionKey);
  if (!cached) return;
  try {
    cached.agent.close?.();
  } catch {
    /* ignore */
  }
  sessions.delete(sessionKey);
}

/** Emit a heartbeat during ``run.wait()`` to avoid falling into the idle timeout. */
export async function waitWithHeartbeat(run, emit, intervalMs = 15_000) {
  let settled = false;
  let result;
  const waiter = run.wait().then((r) => {
    result = r;
    settled = true;
    return r;
  });
  while (!settled) {
    // Capture the timer handle and ALWAYS clear it: when ``waiter`` wins the race the
    // pending ``setTimeout`` was previously left referenced, keeping the event loop
    // alive for up to ``intervalMs`` — a daemon-less run "hung" ~15 s after its text
    // had already finished.
    let timer;
    const tick = new Promise((resolve) => {
      timer = setTimeout(() => resolve(null), intervalMs);
    });
    try {
      const raced = await Promise.race([waiter, tick]);
      if (raced !== null) return raced;
      emit({ ev: "heartbeat", phase: "run_wait" });
    } finally {
      clearTimeout(timer);
    }
  }
  return result;
}

/**
 * Returns the subagent delegation args (``{description, prompt, subagentType, ...}``)
 * when ``update`` is a "task" tool call, else null. ``args`` is normally an object but
 * defensively handled as a JSON string too.
 */
export function subagentArgsOf(update) {
  if (!update || typeof update !== "object") return null;
  let args = update.toolCall?.args ?? update.args;
  if (typeof args === "string") {
    try {
      args = JSON.parse(args);
    } catch {
      return null;
    }
  }
  if (args && typeof args === "object" && args.subagentType) return args;
  return null;
}

/** Resolve tool display name from Cursor SDK delta shapes (toolCall.toolName, etc.). */
export function extractToolName(update) {
  if (!update || typeof update !== "object") return undefined;
  if (subagentArgsOf(update)) return "task";
  const tc = update.toolCall || update.tool_call || update.tool;
  if (tc && typeof tc === "object") {
    const provider = tc.providerIdentifier || tc.provider_identifier;
    const tool = tc.toolName || tc.tool_name;
    if (provider && tool) return `${provider}/${tool}`;
    const direct = tc.name || tool;
    if (direct) return String(direct);
    const fn = tc.function || tc.fn;
    if (fn && typeof fn === "object" && fn.name) return String(fn.name);
  }
  const top = update.toolName || update.tool_name || update.name;
  return top ? String(top) : undefined;
}

function toolWireEvent(update, phase) {
  const name = extractToolName(update);
  const base = {
    ev: "tool",
    phase,
    call_id: update.callId || update.call_id,
    ...(name ? { name } : {}),
  };
  if (phase === "start") {
    return {
      ...base,
      args: update.toolCall?.args ?? update.tool_call?.args ?? update.args,
    };
  }
  return {
    ...base,
    args: update.toolCall?.args ?? update.tool_call?.args ?? update.args,
    result: update.toolCall?.result ?? update.tool_call?.result ?? update.result,
    status: update.toolCall?.status ?? update.tool_call?.status ?? update.status,
  };
}

/**
 * Flatten a completed subagent's inner conversation steps into child wire events
 * nested under ``taskCallId`` via ``parent_id``. Each ``toolCall`` step becomes a
 * start+end pair; ``thinkingMessage``/``assistantMessage`` steps are skipped (no card).
 * Fully defensive — malformed steps are skipped, never thrown.
 */
export function expandSubagentSteps(taskCallId, resultObj) {
  const steps = resultObj?.value?.conversationSteps;
  if (!Array.isArray(steps)) return [];
  const events = [];
  steps.forEach((step, i) => {
    if (!step || typeof step !== "object") return;
    const wrapper = step.toolCall;
    if (!wrapper || typeof wrapper !== "object") return;
    const key = Object.keys(wrapper).find((k) => k.endsWith("ToolCall"));
    if (!key) return;
    const inner = wrapper[key];
    const name = key.slice(0, -"ToolCall".length).toLowerCase();
    const callId = `${taskCallId}::c${i}`;
    events.push({
      ev: "tool",
      phase: "start",
      call_id: callId,
      parent_id: String(taskCallId),
      name,
      args: inner?.args,
    });
    events.push({
      ev: "tool",
      phase: "end",
      call_id: callId,
      parent_id: String(taskCallId),
      name,
      result: inner?.result,
      status: "completed",
    });
  });
  return events;
}

function pickUpdateText(update) {
  if (!update || typeof update !== "object") return "";
  const raw =
    update.text ??
    update.delta ??
    update.content ??
    update.message ??
    update.summary ??
    update.output ??
    "";
  return String(raw || "");
}

/** Rough char→token estimate for the LIVE counter (≈4 chars/token, like Claude). */
function estimateTokens(chars) {
  return chars > 0 ? Math.ceil(chars / 4) : 0;
}

/** Emit a live usage line at most every ``LIVE_USAGE_TOKEN_STEP`` output tokens. */
const LIVE_USAGE_TOKEN_STEP = 8;

/**
 * Build the onDelta handler shared by both bridges.
 *
 * @param emit  (obj) => void — wire emitter; the daemon binds the request id,
 *              run_prompt writes the object as-is.
 * @param state { onText, onUsage } callbacks owning accumulator/usage state:
 *              onText(text)  runs BEFORE the delta event is emitted (the daemon
 *              uses this to emit its ttft timing event first and to grow acc);
 *              onUsage(usage) stores turn-ended usage.
 *
 * LIVE USAGE (Claude parity): as text streams we emit ``{ev:"usage"}`` lines so
 * the UI sees the completion-token count grow during generation; the Cursor SDK
 * only reports real usage at ``turn-ended`` (and historically only inside the
 * final ``done``), so the streamed value is a char-based ESTIMATE. The moment
 * real usage arrives it is emitted verbatim (overriding the estimate) AND stored
 * via ``onUsage`` for the terminal ``done`` event.
 */
export function makeOnDelta(emit, { onText, onUsage }) {
  let liveChars = 0; // accumulated output characters (for the live estimate)
  let lastLiveTokens = 0; // the last emitted estimated token count (throttle)

  const maybeEmitRealUsage = (update) => {
    const real = extractUsage(update);
    if (real) emit(usageWireEvent(real));
    return real;
  };

  return ({ update }) => {
    if (!update || typeof update !== "object") return;
    const t = update.type;
    if (t === "text-delta") {
      const text = String(update.text || "");
      if (!text) return;
      onText(text);
      emit({ ev: "delta", text });
      // Live counter: as output grows, emit the estimated completion token count (throttled).
      liveChars += text.length;
      const estTokens = estimateTokens(liveChars);
      if (estTokens - lastLiveTokens >= LIVE_USAGE_TOKEN_STEP) {
        lastLiveTokens = estTokens;
        emit(usageWireEvent({ outputTokens: estTokens }));
      }
      return;
    }
    if (
      t === "tool-call-started" ||
      t === "tool_call_started" ||
      t === "partial-tool-call" ||
      t === "partial_tool_call"
    ) {
      emit(toolWireEvent(update, "start"));
      return;
    }
    if (t === "tool-call-completed" || t === "tool_call_completed") {
      if (subagentArgsOf(update)) {
        const taskCallId = update.callId || update.call_id || update.toolCall?.callId;
        for (const childEvent of expandSubagentSteps(taskCallId, update.toolCall?.result)) {
          emit(childEvent);
        }
      }
      emit(toolWireEvent(update, "end"));
      return;
    }
    if (t === "thinking-delta" || t === "thinking_delta") {
      const text = pickUpdateText(update);
      if (text) emit({ ev: "thinking", phase: "delta", text });
      return;
    }
    if (t === "thinking-completed" || t === "thinking_completed") {
      emit({ ev: "thinking", phase: "completed" });
      return;
    }
    if (t === "summary-started" || t === "summary_started") {
      const text = pickUpdateText(update);
      emit({ ev: "activity", kind: "summary", phase: "start", text: text || "Özet hazırlanıyor…" });
      return;
    }
    if (t === "summary-completed" || t === "summary_completed" || t === "summary") {
      const text = pickUpdateText(update);
      if (text) emit({ ev: "activity", kind: "summary", phase: "end", text });
      return;
    }
    if (t === "shell-output-delta" || t === "shell_output_delta" || t === "shellOutputDelta") {
      const text = pickUpdateText(update);
      if (text) emit({ ev: "activity", kind: "shell", text });
      return;
    }
    if (t === "step-started" || t === "step_started") {
      const label = pickUpdateText(update) || extractToolName(update) || "Adım başladı";
      emit({ ev: "activity", kind: "step", phase: "start", text: label });
      return;
    }
    if (t === "step-completed" || t === "step_completed") {
      const text = pickUpdateText(update);
      if (text) emit({ ev: "activity", kind: "step", phase: "end", text });
      return;
    }
    if (t === "turn-ended") {
      const real = extractUsage(update);
      if (real) {
        // Real usage arrived: both store it for the terminal ``done`` and emit it
        // as a live ``usage`` line BEFORE ``done`` (so the exact value overrides the
        // estimate; same intent as Claude's final ``message_delta``).
        onUsage(real);
        emit(usageWireEvent(real));
      }
    }
  };
}
