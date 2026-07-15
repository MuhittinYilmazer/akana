/**
 * Blitz 4 — FE↔BE contract drift (akana-settings.js WS branches).
 *
 * The PolicyEngine / task-runner / scheduler subsystems were removed pre-OSS, so
 * the server can no longer broadcast policy_update / task_update / reminder_fire
 * over /ws/events. handleWsEvent must therefore carry NO dead toast branch for
 * those types (a frame that can never arrive), while still:
 *   - forwarding every event to AkanaBus as `ws:<type>`, and
 *   - driving the chat surface from the real turn_active/turn_completed/queue_updated.
 *
 * Regression contract asserted here (fails on the pre-fix source, where a
 * deny(enforced) policy_update / paused task_update / reminder_fire produced a
 * toast):
 *   - policy_update deny+enforced → bus forward, NO toast
 *   - task_update paused          → bus forward, NO toast
 *   - reminder_fire               → bus forward, NO toast
 *   - source carries none of the removed WS_TASK_NOTIFY_STATUSES / dead-branch code
 *   - turn_completed (current conv) still drives AkanaChat, malformed frame no-throws
 *
 * Point AKANA_SETTINGS_SRC at an alternate file to run the RED demonstration.
 * Run: node tests/web/blitz4_fe-be-contract.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { makeI18nStub } from "./_i18n_stub.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const SETTINGS_PATH = process.env.AKANA_SETTINGS_SRC || path.join(REPO, "web_ui/static/akana-settings.js");
const src = readFileSync(SETTINGS_PATH, "utf8");

// the real turn/queue path must survive (passes before and after the fix)
for (const marker of ["turn_completed", "queue_updated", "ws:${type}"]) {
  assert.ok(src.includes(marker), `real WS marker missing from akana-settings.js: ${marker}`);
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

// ── 1. removed types: reach the bus, produce NO toast ──────────────────────────
const deadFrames = [
  { type: "reminder_fire", schedule_id: "01X", text: "drink water" },
  { type: "policy_update", policy: { decision: "deny", enforced: true, action_type: "command", rationale: "risky" } },
  { type: "task_update", task: { id: "T1", status: "paused", title: "Build" } },
];
for (const frame of deadFrames) {
  const before = toasts.length;
  settings._handleWsEvent(JSON.stringify(frame));
  assert.ok(busEvents.some((x) => x.e === `ws:${frame.type}`), `ws:${frame.type} must still reach the bus`);
  assert.equal(toasts.length, before, `${frame.type} must NOT produce a toast (server can no longer emit it)`);
}

// ── 2. the real turn/queue events still drive the chat surface ─────────────────
settings._handleWsEvent(JSON.stringify({ type: "queue_updated", conversation_id: "C1", depth: 3 }));
assert.ok(chatCalls.some(([m, d]) => m === "setQueueDepth" && d === 3), "queue_updated must call setQueueDepth for the current conv");
settings._handleWsEvent(JSON.stringify({ type: "turn_completed", conversation_id: "C1" }));
assert.ok(chatCalls.some(([m, c]) => m === "onTurnCompletedRemote" && c === "C1"), "turn_completed(current) must call onTurnCompletedRemote");
settings._handleWsEvent(JSON.stringify({ type: "turn_completed", conversation_id: "C9" }));
assert.ok(chatCalls.some(([m, c]) => m === "onBackgroundTurnCompleted" && c === "C9"), "turn_completed(other) must call onBackgroundTurnCompleted");

// ── 3. unknown type reaches bus silently; malformed frame no-throws ────────────
const t1 = toasts.length;
settings._handleWsEvent(JSON.stringify({ type: "plan_update", plan: { status: "proposed" } }));
assert.ok(busEvents.some((x) => x.e === "ws:plan_update"), "ws:plan_update must reach the bus");
assert.equal(toasts.length, t1, "plan_update must not produce a toast");
settings._handleWsEvent("{broken json");
settings._handleWsEvent(null);
settings._handleWsEvent(JSON.stringify({ no_type: true }));

// ── 4. source level: the removed subsystems leave no toast-branch residue ──────
for (const dead of ["WS_TASK_NOTIFY_STATUSES", "_wsTaskNotified", "settings.ws.policy_blocked", "settings.ws.task_toast", "settings.ws.reminder_toast"]) {
  assert.ok(!src.includes(dead), `dead WS toast residue must be removed from akana-settings.js: ${dead}`);
}

console.log("blitz4 fe-be-contract (settings WS) test: OK");
process.exit(0);
