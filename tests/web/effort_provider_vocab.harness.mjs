/**
 * Provider-aware reasoning-effort vocabulary contract — backend-free, node-vm + fake DOM.
 *
 * The composer's effort menu speaks ONE of two vocabularies, chosen by the active provider:
 *   • "akana"  — canonical tiers (hizli/normal/derin/yogun/azami/ultra) for claude/gemini.
 *   • "native" — the provider's OWN reasoning levels (minimal/low/medium/high/xhigh) for
 *     codex/openai, shown + sent VERBATIM (no Akana-tier mapping). "xhigh" is native-only.
 * Each vocabulary keeps its OWN persisted selection so a provider switch never sends a level
 * the target can't use; ultra is claude-only and collapses to azami on gemini.
 *
 * Locks: setThinkingProvider(provider) → which option SET the menu renders, that codex/openai
 * expose the native ladder including xhigh, that cursor/ollama hide the menu, and that each
 * vocabulary's selection survives switching providers (per-vocab localStorage).
 *
 * Run: node tests/web/effort_provider_vocab.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const read = (rel) => readFileSync(path.join(REPO, rel), "utf8");

let passed = 0;
function check(label, fn) {
  fn();
  passed += 1;
  void label;
}

// ── Compact fake DOM (only the surfaces the effort selector touches) ─────────
function makeEl(tag = "div") {
  const el = {
    tagName: String(tag).toUpperCase(),
    children: [],
    parentNode: null,
    dataset: {},
    _attrs: {},
    _listeners: {},
    _classes: new Set(),
    _text: "",
    hidden: false,
    id: "",
    type: "",
    title: "",
    style: {},
    focus() {},
  };
  el.classList = {
    add: (...c) => c.forEach((x) => el._classes.add(x)),
    remove: (...c) => c.forEach((x) => el._classes.delete(x)),
    toggle: (c, on) => { const w = on === undefined ? !el._classes.has(c) : !!on; if (w) el._classes.add(c); else el._classes.delete(c); return w; },
    contains: (c) => el._classes.has(c),
  };
  Object.defineProperty(el, "className", {
    get() { return [...el._classes].join(" "); },
    set(v) { el._classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
  });
  Object.defineProperty(el, "textContent", {
    get() { return el._text; },
    set(v) { el._text = String(v); for (const c of el.children) c.parentNode = null; el.children = []; },
  });
  el.setAttribute = (k, v) => {
    el._attrs[k] = String(v);
    if (k === "id") el.id = String(v);
    if (k.startsWith("data-")) el.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(v);
  };
  el.getAttribute = (k) => (k in el._attrs ? el._attrs[k] : null);
  el.removeAttribute = (k) => { delete el._attrs[k]; };
  el.appendChild = (c) => { c.parentNode = el; el.children.push(c); return c; };
  el.append = (...cs) => cs.forEach((c) => { c.parentNode = el; el.children.push(c); });
  el.remove = () => { if (el.parentNode) el.parentNode.children = el.parentNode.children.filter((x) => x !== el); el.parentNode = null; };
  el.addEventListener = (t, fn) => { (el._listeners[t] ||= []).push(fn); };
  el.querySelector = (sel) => el.querySelectorAll(sel)[0] || null;
  el.querySelectorAll = (sel) => {
    const cls = sel.replace(/^\./, "").replace(/\[[^\]]*\]/g, "");
    const out = [];
    const walk = (n) => { for (const c of n.children) { if (c._classes.has(cls)) out.push(c); walk(c); } };
    walk(el);
    return out;
  };
  el.closest = (sel) => {
    const cls = sel.replace(/^\./, "");
    let n = el;
    while (n) { if (n._classes.has(cls)) return n; n = n.parentNode; }
    return null;
  };
  el.dispatchEvent = (ev) => { ev.target = ev.target || el; for (const fn of el._listeners[ev.type] || []) fn(ev); return true; };
  return el;
}

function makeStorage() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
    clear: () => m.clear(),
  };
}

// ── Build the DOM + window, load akana-chat.js (minimal stubs so setup() runs) ─
const byId = {};
function reg(id, tag = "div") { const e = makeEl(tag); e.id = id; byId[id] = e; return e; }
reg("effort-menu");
reg("thinking-mode");
reg("btn-effort", "button");
reg("effort-btn-label", "span");
reg("log"); reg("log-scroll"); reg("chat-form", "form"); reg("msg", "textarea");
reg("btn-send", "button"); reg("composer-attachments"); reg("log-empty");

const docListeners = {};
const doc = {
  getElementById: (id) => byId[id] || null,
  querySelector: (sel) => {
    if (sel.startsWith("#")) return byId[sel.slice(1)] || null;
    for (const e of Object.values(byId)) if (e.querySelector?.(sel)) return e.querySelector(sel);
    return null;
  },
  querySelectorAll: () => [],
  createElement: (t) => makeEl(t),
  addEventListener: (t, fn) => { (docListeners[t] ||= []).push(fn); },
  readyState: "complete",
  body: makeEl("body"),
};

const i18nTable = {
  "chat.effort_fast": "Fast", "chat.effort_normal": "Normal", "chat.effort_deep": "Deep",
  "chat.effort_intense": "Intense", "chat.effort_max": "Max", "chat.effort_ultra": "Ultra",
  "chat.effort_minimal": "Minimal", "chat.effort_low": "Low", "chat.effort_medium": "Medium",
  "chat.effort_high": "High", "chat.effort_xhigh": "Extra High",
  "chat.effort_open_title": "effort", "chat.effort_aria": "Reasoning effort: {label}",
};
const storage = makeStorage();
const win = {
  AkanaI18n: { t: (k, p) => { let s = i18nTable[k] ?? k; if (p) for (const [kk, vv] of Object.entries(p)) s = s.replace(`{${kk}}`, vv); return s; }, getLanguage: () => "en" },
  AkanaCore: { baseUrl: () => "", authHeaders: () => ({}), escapeHtml: (s) => s, parseApiError: (b, s) => `HTTP ${s}` },
  AkanaChatRender: { createRenderer: () => ({ chatRenderMessage: () => {} }), mapServerMessagesToThread: (m) => m },
  // The click delegation that drives setThinkingMode is wired in Chat.init → so init()
  // must run; these stubs are just enough for init's wire pass (no-op threads/transport).
  AkanaChatThreads: { create: () => ({
    wireArchiveChrome: () => {}, wireThreadBar: () => {},
    conversationIdForMemory: () => "", chatActiveThread: () => ({ conversationId: "" }),
    getChatStore: () => ({ threads: {}, activeByProfile: {} }), chatProfile: () => "cursor",
    chatRestoreActiveThread: () => {}, loadChatArchiveList: () => {}, syncChatThreadBar: () => {},
    setConversationId: () => {},
  }) },
  AkanaChatTransport: { create: () => ({
    isConversationStreamActive: () => false, setForegroundConversation: () => {},
  }) },
  AkanaShell: { displayedPane: () => byId["log"], paneFor: () => byId["log"], displayedConvId: () => "" },
  AkanaTurnStatus: { mount: () => {}, begin: () => {}, resume: () => {}, end: () => {}, isActive: () => false, setPhase: () => {} },
  AkanaBus: { emit: () => {}, on: () => {} },
  AkanaVoice: {},
  addEventListener: () => {},
  localStorage: storage,
  matchMedia: () => ({ matches: false, addEventListener() {} }),
};
win.window = win;
win.document = doc;

const ctx = {
  window: win,
  document: doc,
  console,
  localStorage: storage,
  setTimeout, clearTimeout, setInterval, clearInterval,
  queueMicrotask, Promise, URLSearchParams,
  FormData: class { append() {} },
  MouseEvent: class { constructor(t) { this.type = t; } },
  Event: class { constructor(t) { this.type = t; } },
  navigator: { clipboard: { writeText: async () => {} } },
  requestAnimationFrame: (fn) => { fn(); return 1; },
  cancelAnimationFrame: () => {},
};
vm.createContext(ctx);
vm.runInContext(read("web_ui/static/akana-chat.js"), ctx);

const Chat = win.AkanaChat;
assert.ok(Chat && typeof Chat.setThinkingProvider === "function", "AkanaChat.setThinkingProvider must be exported");

// Chat.init runs the wire pass that registers the effort-menu click delegation
// (setThinkingMode). Minimal hooks — only the composer surfaces init touches.
Chat.init({
  log: byId["log"], logScroll: byId["log-scroll"], form: byId["chat-form"], msg: byId["msg"],
  sendBtn: byId["btn-send"], logEmpty: byId["log-empty"],
  appendRow: () => null, appendUserMessage: () => null, appendSystemNotice: () => {},
  updateEmptyState: () => {}, resizeComposer: () => {}, setOrb: () => {}, setComposerHint: () => {},
  stickToBottomIfFollowing: () => {}, scrollLogToBottom: () => {}, scrollNewTurnToTop: () => {},
  setLogLoading: () => {}, showToast: () => {}, shortConversationId: (id) => id || "none",
});

// ── Helpers to read the rendered menu ────────────────────────────────────────
const group = byId["thinking-mode"];
const menu = byId["effort-menu"];
const optModes = () => [...group.querySelectorAll(".effort-opt")].map((b) => b.dataset.mode);
const activeMode = () => { const a = group.querySelectorAll(".effort-opt").find((b) => b._classes.has("is-active")); return a ? a.dataset.mode : null; };
const clickOpt = (mode) => {
  const b = group.querySelectorAll(".effort-opt").find((x) => x.dataset.mode === mode);
  assert.ok(b, `option ${mode} must exist to click`);
  group.dispatchEvent({ type: "click", target: b });
};

// ── 1. codex → native ladder (minimal…xhigh), default medium ─────────────────
Chat.setThinkingProvider("codex");
check("codex shows the native reasoning ladder", () =>
  assert.deepEqual(optModes(), ["minimal", "low", "medium", "high", "xhigh"]));
check("codex menu is visible", () => assert.equal(menu.hidden, false));
check("codex defaults to medium", () => assert.equal(activeMode(), "medium"));
check("codex exposes xhigh (native-only top level)", () => assert.ok(optModes().includes("xhigh")));

// ── 2. openai → the same native ladder ───────────────────────────────────────
Chat.setThinkingProvider("openai");
check("openai shows the native reasoning ladder", () =>
  assert.deepEqual(optModes(), ["minimal", "low", "medium", "high", "xhigh"]));

// ── 3. claude → Akana tiers incl. ultra ──────────────────────────────────────
Chat.setThinkingProvider("claude");
check("claude shows the Akana tiers with ultra", () =>
  assert.deepEqual(optModes(), ["hizli", "normal", "derin", "yogun", "azami", "ultra"]));

// ── 4. gemini → Akana tiers WITHOUT ultra (claude-only) ──────────────────────
Chat.setThinkingProvider("gemini");
check("gemini shows the Akana tiers without ultra", () =>
  assert.deepEqual(optModes(), ["hizli", "normal", "derin", "yogun", "azami"]));

// ── 5. cursor / ollama → no reasoning knob → menu hidden ─────────────────────
Chat.setThinkingProvider("cursor");
check("cursor hides the effort menu", () => assert.equal(menu.hidden, true));
Chat.setThinkingProvider("ollama");
check("ollama hides the effort menu", () => assert.equal(menu.hidden, true));

// ── 6. Per-vocabulary persistence: native + akana selections are independent ─
Chat.setThinkingProvider("codex");
clickOpt("xhigh");
check("selecting a native level persists to the native store", () =>
  assert.equal(storage.getItem("akana:thinking-mode-native"), "xhigh"));
Chat.setThinkingProvider("claude");
clickOpt("derin");
check("selecting an Akana tier persists to the akana store", () =>
  assert.equal(storage.getItem("akana:thinking-mode"), "derin"));
Chat.setThinkingProvider("codex");
check("codex retains its own last native level after a round-trip", () =>
  assert.equal(activeMode(), "xhigh"));
Chat.setThinkingProvider("claude");
check("claude retains its own last Akana tier after a round-trip", () =>
  assert.equal(activeMode(), "derin"));

// ── 7. ultra→azami collapse when leaving claude for gemini (same vocab) ──────
clickOpt("ultra");
check("claude can select ultra", () => assert.equal(activeMode(), "ultra"));
Chat.setThinkingProvider("gemini");
check("gemini collapses a persisted ultra to azami (ultra is claude-only)", () =>
  assert.equal(activeMode(), "azami"));

console.log(`effort_provider_vocab.harness: ${passed} contracts PASSED ✓`);
if (typeof process !== "undefined" && process.exit) process.exit(0);
