/**
 * WS live-event contract (akana-settings.js) — backend-free, with node-vm.
 * The server broadcasts turn_active / turn_completed / queue_updated over
 * /ws/events (chat/chat_detached.py + chat/chat_state.py). The pre-OSS cleanup
 * removed the PolicyEngine / task-runner / scheduler, so policy_update /
 * task_update / reminder_fire can no longer be emitted and carry no toast branch
 * (regression guard: they reach the bus but produce no toast). This harness:
 * - does akana-settings.js load in the stub DOM, is _handleWsEvent exported?
 * - is ws.onmessage wired in the source (events not silently swallowed)?
 * - does every event reach AkanaBus as `ws:<type>`?
 * - do the real turn/queue events drive the chat surface?
 * - do the removed types reach the bus WITHOUT a toast?
 * - is a malformed JSON frame silently swallowed (no exception)?
 * Run: node tests/web/settings_ws_contract.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { makeI18nStub } from "./_i18n_stub.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const SETTINGS_PATH = path.join(REPO, "web_ui/static/akana-settings.js");
const src = readFileSync(SETTINGS_PATH, "utf8");

// ── source level: onmessage wired, real event types recognized ────────────────
assert.ok(src.includes("ws.onmessage"), "connectWs must bind ws.onmessage (events must not be swallowed)");
for (const marker of ["turn_active", "turn_completed", "queue_updated", "ws:${type}"]) {
  assert.ok(src.includes(marker), `missing WS marker in akana-settings.js: ${marker}`);
}

// ── load in the stub DOM ───────────────────────────────────────────────────────
const toasts = [];
const busEvents = [];
const chatCalls = [];
const ctx = {
  window: {
    AkanaCore: {
      LS_BASE: "akana.baseUrl",
      LS_TOKEN: "akana.token",
      showToast: (m, k) => toasts.push({ m: String(m), k }),
      escapeHtml: (s) => String(s),
      baseUrl: () => "http://x",
      authHeaders: () => ({}),
      parseApiError: () => "",
      configure: () => {},
    },
    AkanaBus: { emit: (e, p) => busEvents.push({ e, p }) },
    AkanaChat: {
      conversationIdForMemory: () => "C1",
      setQueueDepth: (d) => chatCalls.push(["setQueueDepth", d]),
      onTurnCompletedRemote: (cid) => chatCalls.push(["onTurnCompletedRemote", cid]),
      onBackgroundTurnCompleted: (cid) => chatCalls.push(["onBackgroundTurnCompleted", cid]),
    },
    AkanaI18n: makeI18nStub(),
  },
  document: {
    body: { classList: { contains: () => false } },
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {},
    documentElement: { dataset: {} },
  },
  navigator: {},
};
ctx.window.document = ctx.document;
vm.runInNewContext(src, ctx);

const settings = ctx.window.AkanaSettings;
assert.ok(settings, "window.AkanaSettings failed to load");
assert.equal(typeof settings._handleWsEvent, "function", "_handleWsEvent must be exported");

// ── 1. queue_updated (current conv) → bus + setQueueDepth ────────────────────
settings._handleWsEvent(JSON.stringify({ type: "queue_updated", conversation_id: "C1", depth: 2 }));
assert.ok(busEvents.some((x) => x.e === "ws:queue_updated"), "ws:queue_updated must reach the bus");
assert.ok(chatCalls.some(([m, d]) => m === "setQueueDepth" && d === 2), "queue_updated(current) must call setQueueDepth");

// ── 2. turn_completed: current conv → onTurnCompletedRemote; other → background ─
settings._handleWsEvent(JSON.stringify({ type: "turn_completed", conversation_id: "C1" }));
assert.ok(chatCalls.some(([m, c]) => m === "onTurnCompletedRemote" && c === "C1"), "turn_completed(current) must call onTurnCompletedRemote");
settings._handleWsEvent(JSON.stringify({ type: "turn_completed", conversation_id: "C9" }));
assert.ok(chatCalls.some(([m, c]) => m === "onBackgroundTurnCompleted" && c === "C9"), "turn_completed(other) must call onBackgroundTurnCompleted");

// ── 3. removed types: reach the bus, produce NO toast (subsystems deleted) ─────
for (const frame of [
  { type: "reminder_fire", text: "drink water" },
  { type: "policy_update", policy: { decision: "deny", enforced: true, action_type: "command" } },
  { type: "task_update", task: { id: "T1", status: "paused", title: "Build" } },
]) {
  const before = toasts.length;
  settings._handleWsEvent(JSON.stringify(frame));
  assert.ok(busEvents.some((x) => x.e === `ws:${frame.type}`), `ws:${frame.type} must reach the bus`);
  assert.equal(toasts.length, before, `${frame.type} must not produce a toast (server can no longer emit it)`);
}

// ── 4. unknown type (plan_update) reaches the bus, produces no toast ─────────────
const t1 = toasts.length;
settings._handleWsEvent(JSON.stringify({ type: "plan_update", plan: { status: "proposed" } }));
assert.ok(busEvents.some((x) => x.e === "ws:plan_update"), "ws:plan_update must reach the bus");
assert.equal(toasts.length, t1, "plan_update must not produce a toast (the chat surface shows it)");

// ── 5. malformed frame → no exception ───────────────────────────────────────────
settings._handleWsEvent("{bozuk json");
settings._handleWsEvent(null);
settings._handleWsEvent(JSON.stringify({ no_type: true }));

console.log("settings WS contract test: OK");
