/**
 * WS live-event contract (akana-settings.js) — backend-free, with node-vm.
 * The server broadcasts task_update / policy_update / reminder_fire over
 * /ws/events (tasks/runner.py, policy/live.py, schedule/service.py). This harness
 * also checks that an unknown non-toast event (plan_update) reaches the bus
 * silently. This harness:
 * - does akana-settings.js load in the stub DOM, is _handleWsEvent exported?
 * - is ws.onmessage wired in the source (events not silently swallowed)?
 * - does every event reach AkanaBus as `ws:<type>`?
 * - do reminder_fire / policy deny(enforced) / task paused-cancelled-aborted
 *   produce a toast; does a repeated task status NOT produce a SECOND toast?
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

// ── source level: onmessage wired, four event types recognized ────────────────
assert.ok(src.includes("ws.onmessage"), "connectWs must bind ws.onmessage (events must not be swallowed)");
for (const marker of ["reminder_fire", "policy_update", "task_update", "ws:${type}"]) {
  assert.ok(src.includes(marker), `missing WS marker in akana-settings.js: ${marker}`);
}

// ── load in the stub DOM ───────────────────────────────────────────────────────
const toasts = [];
const busEvents = [];
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

// ── 1. reminder_fire → bus + toast ──────────────────────────────────────────
settings._handleWsEvent(
  JSON.stringify({ type: "reminder_fire", schedule_id: "01X", text: "Su iç" }),
);
assert.ok(busEvents.some((x) => x.e === "ws:reminder_fire"), "ws:reminder_fire must reach the bus");
assert.ok(toasts.some((t) => t.m.includes("Reminder") && t.m.includes("Su iç")), "reminder toast missing (i18n EN)");

// ── 2. policy_update: deny+enforced → err toast; allow → no toast ──────────
const before = toasts.length;
settings._handleWsEvent(
  JSON.stringify({ type: "policy_update", policy: { decision: "allow", enforced: true } }),
);
assert.equal(toasts.length, before, "an allow decision must not produce a toast");
settings._handleWsEvent(
  JSON.stringify({
    type: "policy_update",
    policy: { decision: "deny", enforced: true, action_type: "command", rationale: "riskli" },
  }),
);
assert.ok(
  toasts.some((t) => t.k === "err" && t.m.includes("Policy blocked")),
  "deny(enforced) must produce an err toast (i18n EN)",
);
assert.ok(busEvents.some((x) => x.e === "ws:policy_update"), "ws:policy_update must reach the bus");

// ── 3. task_update: paused → one toast; same status again → no toast ──────
const t0 = toasts.length;
const paused = { type: "task_update", task: { id: "T1", status: "paused", title: "Derleme" } };
settings._handleWsEvent(JSON.stringify(paused));
settings._handleWsEvent(JSON.stringify(paused)); // repeated progress broadcast
assert.equal(toasts.length, t0 + 1, "the same task+status must not produce a second toast");
assert.ok(toasts[t0].m.includes("paused") && toasts[t0].m.includes("Derleme"));
settings._handleWsEvent(
  JSON.stringify({ type: "task_update", task: { id: "T1", status: "running" } }),
);
assert.equal(toasts.length, t0 + 1, "running status must not produce a toast");
settings._handleWsEvent(
  JSON.stringify({ type: "task_update", task: { id: "T1", status: "cancelled", title: "Derleme" } }),
);
assert.ok(toasts.at(-1).m.includes("cancelled"), "cancelled toast missing (i18n EN)");

// ── 4. plan_update: reaches the bus, produces no toast ──────────────────────────────
const t1 = toasts.length;
settings._handleWsEvent(JSON.stringify({ type: "plan_update", plan: { status: "proposed" } }));
assert.ok(busEvents.some((x) => x.e === "ws:plan_update"), "ws:plan_update must reach the bus");
assert.equal(toasts.length, t1, "plan_update must not produce a toast (the chat surface shows it)");

// ── 5. malformed frame → no exception ───────────────────────────────────────────
settings._handleWsEvent("{bozuk json");
settings._handleWsEvent(null);
settings._handleWsEvent(JSON.stringify({ no_type: true }));

console.log("settings WS contract test: OK");
