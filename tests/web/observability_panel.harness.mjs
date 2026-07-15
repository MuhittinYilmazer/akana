/**
 * Observability panel (akana-observability.js) contract test — backend-free,
 * with node-vm. Mirrors the settings_ws_contract.harness.mjs pattern: load the
 * REAL module source in a minimal fake-DOM context, stub `window.AkanaCore` /
 * `window.AkanaI18n` (the shared helpers every settings-tab module uses), feed a
 * canned GET /api/v1/observability/summary payload, and assert on the rendered
 * innerHTML.
 *
 * Covers:
 *  1. load() renders the toolbar + all four sections (stat tiles, breaker health,
 *     metrics counters/timers, audit tail) from a populated summary payload.
 *  2. Audit events render NEWEST FIRST (client-side reverse of read_tail's
 *     chronological order — see the module docstring for why that split exists).
 *  3. Breaker state maps to the right settings-health-pill tone (open → is-bad).
 *  4. Empty-state payload (fresh data_dir, mirrors the backend's own empty-state
 *     contract) renders zeros/empty-state copy, never throws.
 *  5. A rejected fetch renders the load-failed message instead of throwing.
 *  6. The refresh button (data-action="refresh") is wired via delegated click.
 *  7. Polling gate (_test.syncPolling): visible pane + visible document → polling
 *     ON; either goes hidden → polling OFF (10s auto-refresh, stop when hidden).
 * Run: node tests/web/observability_panel.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { makeI18nStub } from "./_i18n_stub.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const SRC_PATH = path.join(REPO, "web_ui/static/akana-observability.js");
const src = readFileSync(SRC_PATH, "utf8");

// ── minimal fake DOM ──────────────────────────────────────────────────────────
const PANE_ID = "settings-pane-observability";
const ROOT_ID = "observability-root";

function makeEl(id) {
  const el = {
    id,
    innerHTML: "",
    hidden: true, // real page: every non-active settings pane starts hidden
    dataset: {},
    _listeners: {},
    addEventListener(type, fn) {
      (this._listeners[type] = this._listeners[type] || []).push(fn);
    },
    removeEventListener() {},
    contains() {
      return true; // shallow stub: the delegated click target is always "inside"
    },
  };
  return el;
}

const STATUS_ID = "observability-status";

// A minimal DOMTokenList (classList) stub — the module gates polling on
// `document.body.classList.contains("settings-open")` (the Settings overlay flag)
// and observes class flips on <body>, so the fake body needs a real-ish classList.
function makeClassList(initial) {
  const set = new Set(initial || []);
  return {
    add: (c) => set.add(c),
    remove: (c) => set.delete(c),
    contains: (c) => set.has(c),
    toggle: (c) => (set.has(c) ? (set.delete(c), false) : (set.add(c), true)),
  };
}

const pane = makeEl(PANE_ID);
const rootEl = makeEl(ROOT_ID);
// A persistent status element: render()/renderError() write the "updated"/error line
// here via getElementById, so the harness can assert last-good-preserving error render
// (L3) — render sets rootEl.innerHTML as an opaque string, so the status node has to
// exist independently for setStatus() to find it.
const statusEl = makeEl(STATUS_ID);
statusEl.textContent = "";
statusEl.style = {};
const elementsById = { [PANE_ID]: pane, [ROOT_ID]: rootEl, [STATUS_ID]: statusEl };

const docListeners = {};
const fakeBody = { classList: makeClassList([]) };
const fakeDocument = {
  readyState: "complete",
  visibilityState: "visible",
  body: fakeBody,
  getElementById: (id) => elementsById[id] || null,
  addEventListener(type, fn) {
    (docListeners[type] = docListeners[type] || []).push(fn);
  },
  removeEventListener() {},
  createElement: () => ({ id: "", textContent: "" }),
  head: { appendChild() {} },
};

class FakeMutationObserver {
  observe() {}
  disconnect() {}
}

// ── stub window.AkanaCore / AkanaI18n ─────────────────────────────────────────
const i18n = makeI18nStub("en");
let nextApiResult = { ok: true, data: null };
const apiCalls = [];

const ctx = {
  window: {
    AkanaCore: {
      baseUrl: () => "http://x",
      authHeaders: () => ({}),
      escapeHtml: (s) =>
        String(s)
          .replaceAll("&", "&amp;")
          .replaceAll("<", "&lt;")
          .replaceAll(">", "&gt;")
          .replaceAll('"', "&quot;"),
      apiJson: async (_apiBaseFn, method, path, body) => {
        apiCalls.push({ method, path, body });
        if (!nextApiResult.ok) throw new Error(nextApiResult.error || "boom");
        return nextApiResult.data;
      },
    },
    AkanaI18n: { t: i18n.t },
  },
  document: fakeDocument,
  MutationObserver: FakeMutationObserver,
  // Inert timers: init() runs with pane.hidden=true so the polling gate never
  // actually starts a real interval, but these stand in defensively so a bug in
  // that gating can never hang this script.
  setInterval: () => 0,
  clearInterval: () => {},
};
ctx.window.document = ctx.document;
ctx.window.MutationObserver = ctx.MutationObserver;
ctx.window.setInterval = ctx.setInterval;
ctx.window.clearInterval = ctx.clearInterval;

vm.createContext(ctx);
vm.runInContext(src, ctx);

const mod = ctx.window.AkanaObservability;
assert.ok(mod, "window.AkanaObservability failed to load");
assert.equal(typeof mod.load, "function", "load() must be exported");
assert.equal(typeof mod._test.syncPolling, "function", "_test.syncPolling must be exported");

// ── fixtures ───────────────────────────────────────────────────────────────────
const FULL_PAYLOAD = {
  metrics: {
    counters: { llm_errors: { value: 3 }, queue_depth: { value: 0 } },
    timers: { turn_latency_ms: { count: 2, sum_ms: 900, min_ms: 400, max_ms: 500, avg_ms: 450 } },
  },
  usage: {
    window_days: 7,
    conversations_in_window: 3,
    conversations_scanned_for_tokens: 3,
    turns_total: 12,
    tokens: { prompt: 1500, completion: 800, total: 2300 },
    cost_usd: 0.0456,
    per_provider: {
      claude: { prompt: 1000, completion: 600, cost_usd: 0.04, turns: 8 },
      codex: { prompt: 500, completion: 200, cost_usd: 0.0, turns: 4 },
    },
    provider_attribution: true,
    note: "",
  },
  health: {
    active_provider: "cursor",
    breakers: [
      { name: "cursor", state: "open", failures: 5, threshold: 5, cooldown: 30, retry_after: 12.5 },
      { name: "claude", state: "closed", failures: 0, threshold: 5, cooldown: 30, retry_after: 0 },
      // An UNKNOWN state (no i18n entry) — exercises the L2 fallback: t() returns the
      // key on a miss, so breakerStateLabel must fall back to the raw state, never leak
      // the i18n key into the DOM.
      { name: "ollama", state: "melted", failures: 1, threshold: 5, cooldown: 30, retry_after: 0 },
    ],
  },
  audit: {
    count: 2,
    events: [
      { ts: "2026-07-11T10:00:00.000Z", kind: "chat", conv_id: "conv-1" },
      { ts: "2026-07-11T10:05:00.000Z", kind: "voice", turn_id: "turn-9" },
    ],
  },
};

const EMPTY_PAYLOAD = {
  metrics: { counters: {}, timers: {} },
  usage: {
    window_days: 7,
    conversations_in_window: 0,
    conversations_scanned_for_tokens: 0,
    turns_total: 0,
    tokens: { prompt: 0, completion: 0, total: 0 },
    cost_usd: 0,
    per_provider: null,
    provider_attribution: false,
    note: "Persisted turn usage does not carry a provider field...",
  },
  health: { active_provider: "", breakers: [] },
  audit: { count: 0, events: [] },
};

// ── 1. populated payload renders tiles + breakers + metrics + audit ────────────
nextApiResult = { ok: true, data: FULL_PAYLOAD };
await mod.load();
assert.equal(apiCalls.length, 1, "load() must call the summary endpoint exactly once");
assert.equal(apiCalls[0].path, "/summary");
let html = rootEl.innerHTML;

// stat tiles (English labels, real i18n text)
for (const label of [
  "Total turns",
  "Prompt tokens",
  "Completion tokens",
  "Total tokens",
  "Estimated cost",
  "Active provider",
]) {
  assert.ok(html.includes(label), `stat tile label missing: ${label}`);
}
assert.ok(html.includes("12"), "turns_total value (12) missing from tiles");
assert.ok(html.includes("1,500"), "prompt token count (1,500, en-US grouping) missing");
assert.ok(html.includes("2,300"), "total token count (2,300, en-US grouping) missing");
assert.ok(html.includes("cursor"), "active provider name missing");
assert.ok(html.includes("$0.0456"), "cost tile missing (< $1 → 4 decimals)");

// breaker health: two rows, correct state tone
assert.ok(html.includes("Provider health"), "breaker section heading missing");
assert.ok(html.includes("claude"), "closed breaker (claude) missing");
assert.ok(
  /settings-health-pill is-bad">cursor/.test(html),
  "open breaker (cursor) must render with the is-bad tone",
);
assert.ok(
  /settings-health-pill is-ok">claude/.test(html),
  "closed breaker (claude) must render with the is-ok tone",
);

// L2: an unknown breaker state falls back to the RAW state, never the i18n key.
assert.ok(html.includes("· melted"), "unknown breaker state must render its raw value");
assert.ok(
  !/observability\.breaker_state\.melted/.test(html),
  "unknown breaker state must NOT leak the raw i18n key into the DOM",
);

// metrics counters/timers table
assert.ok(html.includes("llm_errors"), "counter name missing from metrics table");
assert.ok(html.includes("turn_latency_ms"), "timer name missing from metrics table");

// audit tail — newest first (client-side reverse of read_tail's chronological order)
assert.ok(html.includes("conv=conv-1"), "audit row (chat/conv-1) missing");
assert.ok(html.includes("turn=turn-9"), "audit row (voice/turn-9) missing");
{
  const voiceIdx = html.indexOf("turn=turn-9");
  const chatIdx = html.indexOf("conv=conv-1");
  assert.ok(voiceIdx >= 0 && chatIdx >= 0 && voiceIdx < chatIdx, "audit tail must render newest-first (voice before chat)");
}

// per-provider breakdown surfaces when turns are stamped (attribution=true): the
// section heading + each provider row appear, and the aggregate-only note is gone.
assert.ok(html.includes("Tokens by provider"), "per-provider breakdown heading missing");
assert.ok(html.includes("claude"), "claude provider row missing from breakdown");
assert.ok(html.includes("codex"), "codex provider row missing from breakdown");
assert.ok(!html.includes("Older turns predate"), "aggregate-only note must be hidden when attributed");

// ── 2. refresh button is wired via delegated click ──────────────────────────────
{
  apiCalls.length = 0;
  const listeners = rootEl._listeners.click || [];
  assert.ok(listeners.length > 0, "root must have a delegated click listener");
  const fakeBtn = { dataset: { action: "refresh" }, closest: (sel) => (sel === '[data-action="refresh"]' ? fakeBtn : null) };
  for (const fn of listeners) await fn({ target: fakeBtn });
  assert.equal(apiCalls.length, 1, "clicking the refresh button must trigger exactly one reload");
}

// ── 3. empty-state payload → zeros + empty-state copy, never throws ────────────
nextApiResult = { ok: true, data: EMPTY_PAYLOAD };
await mod.load();
html = rootEl.innerHTML;
assert.ok(html.includes("Unconfigured"), "unconfigured provider copy missing in empty state");
assert.ok(html.includes("No circuit breakers have tripped yet."), "breaker empty-state copy missing");
assert.ok(html.includes("No metrics recorded yet."), "metrics empty-state copy missing");
assert.ok(html.includes("No audit events recorded today."), "audit empty-state copy missing");

// ── 4. a rejected fetch shows the error in the STATUS LINE and keeps last-good ──
//    content (L3: a render error must not destroy the panel + its refresh button).
//    Section 3 left a fully-rendered panel behind, so the toolbar/status node exist.
statusEl.textContent = "";
nextApiResult = { ok: false, error: "network down" };
await mod.load();
assert.ok(
  statusEl.textContent.includes("network down"),
  "load-failure message must surface the underlying error in the status line",
);
assert.ok(
  rootEl.innerHTML.includes('data-action="refresh"'),
  "a load failure must keep the last-good panel (incl. the refresh button), not wipe it",
);

// ── 5. polling gate: pane + document + the Settings overlay must ALL be open ──────
fakeBody.classList.add("settings-open");
pane.hidden = false;
fakeDocument.visibilityState = "visible";
mod._test.syncPolling();
assert.equal(mod._test.isPolling(), true, "polling must start when pane+document+overlay are all visible");

// H2: closing the Settings overlay (Escape/backdrop) removes body.settings-open but
// leaves pane.hidden untouched — the poll (and its server-side scan) must STILL stop.
fakeBody.classList.remove("settings-open");
mod._test.syncPolling();
assert.equal(
  mod._test.isPolling(),
  false,
  "polling must stop when the Settings overlay closes (settings-open removed) even though pane.hidden is untouched",
);

// reopening the overlay resumes polling
fakeBody.classList.add("settings-open");
mod._test.syncPolling();
assert.equal(mod._test.isPolling(), true, "polling resumes when the Settings overlay reopens");

pane.hidden = true;
mod._test.syncPolling();
assert.equal(mod._test.isPolling(), false, "polling must stop when the pane is hidden");

pane.hidden = false;
fakeDocument.visibilityState = "hidden";
mod._test.syncPolling();
assert.equal(mod._test.isPolling(), false, "polling must stop when the document itself is hidden (backgrounded tab)");

// restore + stop for a clean exit (defensive — setInterval/clearInterval are inert
// stubs here, but this keeps the contract explicit if that ever changes).
fakeDocument.visibilityState = "hidden";
pane.hidden = true;
fakeBody.classList.remove("settings-open");
mod._test.syncPolling();
assert.equal(mod._test.isPolling(), false, "final state: polling stopped");

console.log("observability panel contract test: OK");
