/**
 * blitz3 — fe-shell-ui + fe-chat-core regression harness (node-vm + fake DOM, no backend).
 *
 * Loads the REAL static modules and asserts the behaviour contracts for the 12 verified
 * bugs fixed in this batch:
 *
 *   akana-turn-status.js
 *     · fe-shell-ui-6  resume() re-attaches to a running turn WITHOUT restarting the clock
 *                      (begin() would show "Preparing · 0:00" for a 45s-old turn).
 *   akana-mobile-nav.js
 *     · fe-shell-ui-1  tab labels follow a late language switch (relabelNav).
 *     · fe-shell-ui-2  chat:conversation:changed → the strip returns to the Chat tab.
 *   aurora-ui.js
 *     · fe-shell-ui-3  the custom-accent theme watcher is installed app-wide (init), so a
 *                      live theme flip on a page WITHOUT the picker re-derives the family.
 *     · fe-shell-ui-1  injected segmented controls follow a language switch.
 *   akana-shell.js
 *     · fe-shell-ui-5  the attachment cache coalesces concurrent same-id fetches (one blob).
 *     · fe-shell-ui-4  the LRU in-use guard treats a PDF entry (thumbUrlResolved) as in-use.
 *     · fe-shell-ui-1  suggestion chips follow a late language switch.
 *   akana-chat-threads.js
 *     · fe-chat-core-1 a non-404 (5xx/401) read is NOT treated as an empty conversation.
 *     · fe-chat-core-2 a stale 404-destroy does NOT clobber a newer switch's global state.
 *     · fe-chat-core-3 a superseded switch's persist-pause / log-loading flags are reset.
 *     · fe-shell-ui-2  setConversationId emits chat:conversation:changed on a real change.
 *   akana-chat.js
 *     · fe-chat-core-4 the 10-min safety timer aborts THIS turn's stream, never the fg.
 *     · fe-chat-core-5 a pre-stream failure returns the consumed attachments to the composer.
 *     · fe-chat-core-6 flashOk does not stick the button on "✓" on a rapid double-click.
 *
 * Run: node tests/web/blitz3_fe-shell-chat-core.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { makeI18nStub } from "./_i18n_stub.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const read = (rel) => readFileSync(path.join(REPO, rel), "utf8");

// ── CSS selector engine: compound (tag#id.class[attr="v"]) + descendant (space) ──────
function matchCompound(node, sel) {
  let s = String(sel).trim();
  const attrs = [];
  const attrRe = /\[([^\]=]+)(?:=["']?([^"'\]]*)["']?)?\]/g;
  let m;
  while ((m = attrRe.exec(s))) attrs.push([m[1], m[2]]);
  s = s.replace(attrRe, "");
  let id = null;
  s = s.replace(/#([A-Za-z0-9_-]+)/g, (_, i) => { id = i; return ""; });
  const classes = [];
  s = s.replace(/\.([A-Za-z0-9_-]+)/g, (_, c) => { classes.push(c); return ""; });
  const tag = s.trim();
  if (tag && tag !== "*" && node.tagName !== tag.toUpperCase()) return false;
  if (id && node.id !== id) return false;
  for (const c of classes) if (!node._classes.has(c)) return false;
  for (const [k, v] of attrs) {
    let actual;
    if (k === "class") actual = node.className;
    else if (k.startsWith("data-")) {
      const camel = k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      actual = node.dataset[camel] != null ? node.dataset[camel] : node.getAttribute(k);
    } else actual = node.getAttribute(k);
    if (v !== undefined) {
      if (String(actual) !== v) return false;
    } else if (actual == null) return false;
  }
  return true;
}
function ancestorsMatch(node, parts) {
  // parts: descendant combinator list, rightmost already matched `node`. Walk up.
  let idx = parts.length - 2;
  let cur = node.parentNode;
  while (idx >= 0 && cur) {
    if (matchCompound(cur, parts[idx])) idx--;
    cur = cur.parentNode;
  }
  return idx < 0;
}
function collectAll(root, sel) {
  const parts = String(sel).trim().split(/\s+/);
  const last = parts[parts.length - 1];
  const out = [];
  const walk = (n) => {
    for (const c of n.children || []) {
      if (matchCompound(c, last) && (parts.length === 1 || ancestorsMatch(c, parts))) out.push(c);
      walk(c);
    }
  };
  walk(root);
  return out;
}

// ── Fake DOM element ─────────────────────────────────────────────────────────
function makeEl(tag = "div") {
  const el = {
    tagName: String(tag).toUpperCase(),
    children: [], parentNode: null, ownerRoot: null,
    _text: "", _html: "", dataset: {}, _attrs: {}, _listeners: {},
    _classes: new Set(), style: makeStyle(), hidden: false, id: "",
    value: "", maxLength: 0, type: "", spellcheck: false, title: "",
    loading: "", decoding: "", src: "", alt: "", placeholder: "",
    scrollTop: 0, scrollHeight: 0, clientHeight: 0,
  };
  el.classList = {
    add: (...cs) => cs.forEach((c) => el._classes.add(c)),
    remove: (...cs) => cs.forEach((c) => el._classes.delete(c)),
    toggle: (c, on) => { const w = on === undefined ? !el._classes.has(c) : !!on; if (w) el._classes.add(c); else el._classes.delete(c); return w; },
    contains: (c) => el._classes.has(c),
  };
  Object.defineProperties(el, {
    className: {
      get() { return [...el._classes].join(" "); },
      set(v) { el._classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    },
    textContent: { get() { return el._text; }, set(v) { el._text = String(v); el.children = []; } },
    innerHTML: {
      get() { return el._html; },
      set(v) { el._html = String(v); if (v === "") { for (const c of el.children) c.parentNode = null; el.children = []; } },
    },
    firstChild: { get() { return el.children[0] || null; } },
    isConnected: { get() { let n = el; while (n.parentNode) n = n.parentNode; return n === (el.ownerRoot || n) && n.tagName === "ROOT"; } },
  });
  el.setAttribute = (k, v) => {
    el._attrs[k] = String(v);
    if (k === "id") el.id = String(v);
    if (k === "src") el.src = String(v);
    if (k.startsWith("data-")) el.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())] = String(v);
  };
  el.getAttribute = (k) => (k in el._attrs ? el._attrs[k] : null);
  el.hasAttribute = (k) => k in el._attrs;
  el.removeAttribute = (k) => { delete el._attrs[k]; };
  const adopt = (c) => { c.parentNode = el; c.ownerRoot = el.ownerRoot; };
  el.appendChild = (c) => { adopt(c); el.children.push(c); return c; };
  el.append = (...cs) => cs.forEach((c) => { adopt(c); el.children.push(c); });
  el.prepend = (...cs) => cs.forEach((c) => { adopt(c); el.children.unshift(c); });
  el.replaceChildren = (...cs) => { for (const c of el.children) c.parentNode = null; el.children = []; cs.forEach((c) => { adopt(c); el.children.push(c); }); };
  el.insertBefore = (node, ref) => { adopt(node); const i = el.children.indexOf(ref); if (i < 0) el.children.push(node); else el.children.splice(i, 0, node); return node; };
  el.remove = () => { if (el.parentNode) { const i = el.parentNode.children.indexOf(el); if (i >= 0) el.parentNode.children.splice(i, 1); el.parentNode = null; } };
  el.addEventListener = (t, f) => { (el._listeners[t] ||= []).push(f); };
  el.removeEventListener = (t, f) => { const a = el._listeners[t]; if (a) { const i = a.indexOf(f); if (i >= 0) a.splice(i, 1); } };
  el.dispatchEvent = (evt) => { const t = evt && evt.type; for (const fn of (el._listeners[t] || []).slice()) fn(evt); return true; };
  el.dispatch = (t, evt) => { const e = Object.assign({ type: t, preventDefault() {}, stopPropagation() {} }, evt || {}); for (const fn of (el._listeners[t] || []).slice()) fn(e); };
  el.click = () => el.dispatch("click");
  el.focus = () => {};
  el.select = () => {};
  el.closest = (sel) => { let n = el; while (n) { if (matchCompound(n, sel)) return n; n = n.parentNode; } return null; };
  el.matches = (sel) => String(sel).split(",").some((s) => matchCompound(el, s.trim()));
  el.querySelectorAll = (sel) => collectAll(el, sel);
  el.querySelector = (sel) => collectAll(el, sel)[0] || null;
  el.getBoundingClientRect = () => ({ top: 0, left: 0, width: 0, height: 0, bottom: 0, right: 0 });
  return el;
}
function makeStyle() {
  const props = {};
  return {
    setProperty: (k, v) => { props[k] = String(v); },
    getPropertyValue: (k) => props[k] || "",
    removeProperty: (k) => { delete props[k]; },
    _props: props,
  };
}

function makeDoc() {
  const root = makeEl("root");
  root.ownerRoot = root;
  const byId = {};
  const doc = {
    _root: root,
    documentElement: root,
    body: makeEl("body"),
    readyState: "complete",
    getElementById: (id) => byId[id] || collectAll(root, `#${id}`)[0] || null,
    createElement: (t) => { const e = makeEl(t); e.ownerRoot = root; return e; },
    createElementNS: (_ns, t) => { const e = makeEl(t); e.ownerRoot = root; return e; },
    createDocumentFragment: () => { const f = makeEl("fragment"); f.ownerRoot = root; return f; },
    querySelector: (sel) => collectAll(root, sel)[0] || null,
    querySelectorAll: (sel) => collectAll(root, sel),
    addEventListener: () => {},
    _register: (id, el) => { byId[id] = el; },
  };
  root.appendChild(doc.body);
  doc.body.ownerRoot = root;
  return doc;
}

function makeStorage() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k), clear: () => m.clear() };
}

// A window whose addEventListener/dispatch we control (for akana:languagechange etc).
function makeWindow() {
  const listeners = {};
  const win = {
    addEventListener: (t, f) => { (listeners[t] ||= []).push(f); },
    removeEventListener: (t, f) => { const a = listeners[t]; if (a) { const i = a.indexOf(f); if (i >= 0) a.splice(i, 1); } },
    dispatchEvent: (evt) => { for (const fn of (listeners[evt.type] || []).slice()) fn(evt); return true; },
    _fire: (type, detail) => { const evt = { type, detail }; for (const fn of (listeners[type] || []).slice()) fn(evt); },
    matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {}, removeEventListener() {} }),
    navigator: {},
  };
  return win;
}

// Switchable i18n (so a language flip yields new strings). Real dictionary via the stub.
function makeSwitchableI18n() {
  const en = makeI18nStub("en");
  const tr = makeI18nStub("tr");
  const state = { lang: "en" };
  return {
    state,
    api: {
      t: (k, p) => (state.lang === "tr" ? tr.t(k, p) : en.t(k, p)),
      getLanguage: () => state.lang,
    },
  };
}

// ── Test runner ───────────────────────────────────────────────────────────────
let failures = 0;
let passed = 0;
async function check(label, fn) {
  try { await fn(); passed += 1; }
  catch (e) { failures += 1; console.error(`✗ ${label}`); console.error(`   ${e && e.message ? e.message : e}`); }
}

// A controllable timer factory: records (delay, cb) and lets a test fire by delay.
function makeTimers() {
  const timers = new Map();
  let id = 1;
  const setTimeout_ = (cb, delay) => { const t = id++; timers.set(t, { cb, delay }); return t; };
  const clearTimeout_ = (t) => { timers.delete(t); };
  const setInterval_ = (cb, delay) => { const t = id++; timers.set(t, { cb, delay, interval: true }); return t; };
  const clearInterval_ = (t) => { timers.delete(t); };
  const fireDelay = (delay) => { for (const [t, o] of [...timers]) { if (o.delay === delay) { if (!o.interval) timers.delete(t); o.cb(); } } };
  return { setTimeout: setTimeout_, clearTimeout: clearTimeout_, setInterval: setInterval_, clearInterval: clearInterval_, fireDelay, size: () => timers.size };
}

// ══════════════════════════════════════════════════════════════════════════════
// A. akana-turn-status.js — fe-shell-ui-6
// ══════════════════════════════════════════════════════════════════════════════
function loadTurnStatus() {
  const doc = makeDoc();
  const form = doc.createElement("form"); form.id = "chat-form";
  const inner = doc.createElement("div"); inner.className = "composer-inner";
  form.appendChild(inner);
  doc.body.appendChild(form);
  doc._register("chat-form", form);
  const now = { v: 1000 };
  const win = makeWindow();
  const ctx = {
    console,
    Date: { now: () => now.v },
    Math, String,
    setInterval: () => 1, clearInterval: () => {}, setTimeout: () => 1, clearTimeout: () => {},
    document: doc,
    window: Object.assign(win, { AkanaI18n: makeI18nStub(), setInterval: () => 1, clearInterval: () => {} }),
  };
  ctx.window.window = ctx.window;
  ctx.window.document = doc;
  vm.createContext(ctx);
  vm.runInContext(read("web_ui/static/akana-turn-status.js"), ctx);
  const strip = form.querySelector(".akana-flow-strip");
  const label = () => strip.querySelector(".jfs-label");
  return { TS: ctx.window.AkanaTurnStatus, now, label, strip };
}

await check("fe-shell-ui-6 · resume() re-attaches to a running turn WITHOUT restarting the clock", () => {
  const h = loadTurnStatus();
  h.TS.begin();                 // turn starts at now=1000
  h.TS.setPhase("writing");
  h.now.v = 46000;              // 45s of streaming
  h.TS.end();                   // switch AWAY → strip hidden, state retained
  assert.equal(h.strip.hidden, true, "end() hides the strip");
  assert.equal(typeof h.TS.resume, "function", "resume() must exist (switch-back API)");
  h.TS.resume();                // switch BACK to the still-running turn
  const txt = h.label().textContent;
  assert.ok(/0:45/.test(txt), `elapsed should be the real 0:45, got "${txt}"`);
  assert.ok(!/0:00/.test(txt), `elapsed must NOT restart at 0:00 (got "${txt}")`);
});

await check("fe-shell-ui-6 · begin() still resets the clock for a genuinely NEW turn", () => {
  const h = loadTurnStatus();
  h.TS.begin();
  h.now.v = 46000;
  h.TS.end();
  h.now.v = 100000;
  h.TS.begin();                 // a NEW turn → 0:00 again
  const txt = h.label().textContent;
  assert.ok(/0:00/.test(txt), `a new turn should start at 0:00, got "${txt}"`);
});

await check("fe-shell-ui-6 (review) · resume(convId) refuses a CONCURRENT turn's clock/phase (conv mismatch → fresh)", () => {
  const h = loadTurnStatus();
  h.TS.begin("A");              // A's turn starts at now=1000
  h.TS.setPhase("writing");
  h.now.v = 46000;             // 45s of A
  h.TS.end();                  // switch AWAY from A (snapshot retained = A's)
  // A CONCURRENT turn in conv B begins → overwrites the single retained snapshot with B's.
  h.TS.begin("B");             // now=46000
  h.TS.setPhase("tool", "grep foo");
  h.now.v = 70000;
  h.TS.end();                  // switch away from B (retained snapshot is now B's)
  // Switch BACK to A while A is still streaming.
  h.now.v = 80000;
  h.TS.resume("A");            // requested id A ≠ retained id B → must NOT show B's data
  const txt = h.label().textContent;
  assert.ok(!/grep/.test(txt), `must NOT attribute B's tool label to A (got "${txt}")`);
  assert.ok(/0:00/.test(txt), `a conv mismatch must fall back to a FRESH clock (got "${txt}")`);
  assert.ok(!/0:34/.test(txt), `must NOT show B's elapsed (46000→80000 = 0:34) (got "${txt}")`);
});

await check("fe-shell-ui-6 (review) · resume(convId) PRESERVES the clock when the id matches (real switch-back)", () => {
  const h = loadTurnStatus();
  h.TS.begin("A");
  h.TS.setPhase("writing");
  h.now.v = 46000;             // 45s of A
  h.TS.end();                  // switch away from A (no other turn overwrites the snapshot)
  h.now.v = 47000;
  h.TS.resume("A");            // same conv → preserve the true elapsed (not restart at 0:00)
  const txt = h.label().textContent;
  assert.ok(/0:46/.test(txt), `matching id must preserve A's real elapsed (got "${txt}")`);
  assert.ok(!/0:00/.test(txt), `matching id must NOT restart the clock (got "${txt}")`);
});

// ══════════════════════════════════════════════════════════════════════════════
// B. akana-mobile-nav.js — fe-shell-ui-1 (relabel) + fe-shell-ui-2 (return-to-chat)
// ══════════════════════════════════════════════════════════════════════════════
function loadMobileNav() {
  const doc = makeDoc();
  const win = makeWindow();
  const i18n = makeSwitchableI18n();
  const busHandlers = {};
  const bus = {
    on: (e, h) => { (busHandlers[e] ||= []).push(h); },
    emit: (e, p) => { for (const h of busHandlers[e] || []) h(p); },
  };
  Object.assign(win, {
    AkanaI18n: i18n.api, AkanaBus: bus,
    navigator: {}, matchMedia: win.matchMedia, document: doc,
  });
  win.window = win;
  const ctx = { console, document: doc, window: win, setTimeout: () => 1, clearTimeout: () => {} };
  vm.createContext(ctx);
  vm.runInContext(read("web_ui/static/akana-mobile-nav.js"), ctx);
  const nav = doc.getElementById("mnav") || doc.querySelector("#mnav");
  const tabByKey = (k) => nav.querySelector(`.mnav-tab[data-mnav="${k}"]`);
  return { doc, win, i18n, bus, nav, tabByKey };
}

await check("fe-shell-ui-1 · mobile tab labels follow a late language switch", () => {
  const h = loadMobileNav();
  const chatLabel = () => h.tabByKey("sohbet").querySelector(".mnav-label").textContent;
  const en = chatLabel();
  assert.ok(en && en.length, "the Chat tab should have a boot label");
  h.i18n.state.lang = "tr";                 // backend reconcile flips language
  h.win._fire("akana:languagechange", { lang: "tr" });
  const tr = chatLabel();
  assert.equal(tr, h.i18n.api.t("nav.tab_chat"), "the label must be re-read in the new language");
  assert.notEqual(tr, en, "the tab label must actually change on a language switch");
});

await check("fe-shell-ui-2 · chat:conversation:changed returns the strip to the Chat tab", () => {
  const h = loadMobileNav();
  const isActive = (k) => h.tabByKey(k)._classes.has("is-active");
  h.tabByKey("ayarlar").dispatch("click");   // user taps Settings
  assert.ok(isActive("ayarlar"), "Settings should be the active tab after the tap");
  assert.ok(!isActive("sohbet"), "Chat should not be active");
  h.bus.emit("chat:conversation:changed", { conversationId: "c1" });
  assert.ok(isActive("sohbet"), "chat:conversation:changed must return the highlight to the Chat tab");
  assert.ok(!isActive("ayarlar"), "Settings should no longer be active");
});

// ══════════════════════════════════════════════════════════════════════════════
// C. aurora-ui.js — fe-shell-ui-3 (theme watcher app-wide) + fe-shell-ui-1 (segments)
// ══════════════════════════════════════════════════════════════════════════════
function loadAurora({ withPane = false, accentPref = null } = {}) {
  const doc = makeDoc();
  const root = doc.documentElement;
  root.setAttribute("data-theme", "light");
  const localStorage = makeStorage();
  if (accentPref) {
    localStorage.setItem("cockpit:accent", "custom");
    localStorage.setItem("cockpit:accentCustom", accentPref);
  }
  if (withPane) {
    const pane = doc.createElement("div"); pane.id = "settings-pane-appearance";
    const block = doc.createElement("div"); block.className = "settings-block";
    const cb = doc.createElement("input"); cb.id = "settings-compact-log";
    block.appendChild(cb); pane.appendChild(block);
    doc.body.appendChild(pane);
    doc._register("settings-pane-appearance", pane);
  }
  const win = makeWindow();
  const i18n = makeSwitchableI18n();
  const observers = [];
  class FakeMO {
    constructor(cb) { this.cb = cb; observers.push(this); }
    observe() { this.observing = true; }
    disconnect() {}
  }
  Object.assign(win, { AkanaI18n: i18n.api, document: doc, localStorage });
  win.window = win;
  const ctx = {
    console, document: doc, window: win, localStorage,
    MutationObserver: FakeMO, setTimeout: () => 1, clearTimeout: () => {},
    JSON,
  };
  vm.createContext(ctx);
  vm.runInContext(read("web_ui/static/aurora-ui.js"), ctx);
  return { doc, root, win, i18n, observers };
}

await check("fe-shell-ui-3 · the custom-accent theme watcher is installed app-wide (no picker page)", () => {
  // memory.html: NO .accent-swatch-row → wireAccentPicker returns early. A custom accent is
  // active; a live OS/system theme flip must still re-derive the family for the new theme.
  const h = loadAurora({ withPane: false, accentPref: "#6c8cff" });
  const accentLight = h.root.style.getPropertyValue("--j-accent");
  assert.ok(accentLight, "a custom accent should be applied on boot (light family)");
  assert.ok(h.observers.length >= 1, "the data-theme MutationObserver must be installed even without the picker");
  h.root.setAttribute("data-theme", "dark");
  for (const o of h.observers) o.cb();     // fire the theme MutationObserver
  const accentDark = h.root.style.getPropertyValue("--j-accent");
  assert.notEqual(accentDark, accentLight, "flipping the theme must re-derive the accent family (dark != light)");
});

await check("fe-shell-ui-1 · injected segmented controls follow a language switch", () => {
  const h = loadAurora({ withPane: true });
  const firstOpt = () => h.doc.querySelector('.aur-seg-btn[data-aur-seg="atmos"][data-aur-value="calm"]');
  const en = firstOpt().textContent;
  assert.ok(en && en.length, "the atmosphere 'calm' option should have a boot label");
  h.i18n.state.lang = "tr";
  h.win._fire("akana:languagechange", { lang: "tr" });
  const tr = firstOpt().textContent;
  assert.equal(tr, h.i18n.api.t("ui.aurora_seg_calm"), "the option label must be re-read in the new language");
  assert.notEqual(tr, en, "the segmented-control label must change on a language switch");
});

// ══════════════════════════════════════════════════════════════════════════════
// D. akana-shell.js — fe-shell-ui-5 (in-flight coalesce), fe-shell-ui-4 (PDF guard),
//    fe-shell-ui-1 (suggestion chips relabel)
// ══════════════════════════════════════════════════════════════════════════════
function loadShell({ withPromptChips = false } = {}) {
  const doc = makeDoc();
  const win = makeWindow();
  const i18n = makeSwitchableI18n();
  if (withPromptChips) {
    const empty = doc.createElement("div"); empty.id = "log-empty";
    const chips = doc.createElement("div"); chips.className = "prompt-chips";
    empty.appendChild(chips);
    doc.body.appendChild(empty);
    doc._register("log-empty", empty);
  }
  const fetchCalls = [];
  let urlSeq = 0;
  const createdUrls = [];
  const revokedUrls = [];
  const fetchState = { handler: null };
  Object.assign(win, {
    AkanaI18n: i18n.api,
    AkanaCore: { baseUrl: () => "", authHeaders: () => ({}), escapeHtml: (s) => s },
    document: doc,
  });
  win.window = win;
  const ctx = {
    console, document: doc, window: win,
    setTimeout: (fn) => { return 1; }, clearTimeout: () => {},
    setInterval: () => 1, clearInterval: () => {},
    requestAnimationFrame: () => {}, cancelAnimationFrame: () => {},
    queueMicrotask, Promise,
    URL: {
      createObjectURL: () => { const u = `blob:mock/${urlSeq++}`; createdUrls.push(u); return u; },
      revokeObjectURL: (u) => { revokedUrls.push(u); },
    },
    fetch: async (url, opts) => { fetchCalls.push(url); return fetchState.handler(url, opts); },
  };
  ctx.window.fetch = ctx.fetch;
  vm.createContext(ctx);
  vm.runInContext(read("web_ui/static/akana-shell.js"), ctx);
  return { doc, win, i18n, fetchCalls, createdUrls, revokedUrls, fetchState, Shell: win.AkanaShell };
}

await check("fe-shell-ui-5 · concurrent renders of the same attachment share ONE fetch/object-URL", async () => {
  const h = loadShell();
  let resolveFetch;
  const gate = new Promise((r) => { resolveFetch = r; });
  h.fetchState.handler = async () => { await gate; return { ok: true, blob: async () => ({ type: "image/png" }) }; };
  // Two concurrent renders of the SAME id (optimistic echo + server sync re-render).
  const p1 = h.Shell._test.fetchMsgAttachment("A1");
  const p2 = h.Shell._test.fetchMsgAttachment("A1");
  resolveFetch();
  const [e1, e2] = await Promise.all([p1, p2]);
  assert.equal(h.fetchCalls.filter((u) => String(u).includes("A1")).length, 1, "the id must be fetched exactly once");
  assert.equal(h.createdUrls.length, 1, "exactly one object URL must be created (no leak)");
  assert.equal(e1, e2, "both concurrent callers must receive the SAME shared entry");
});

await check("fe-shell-ui-4 · a PDF entry whose thumb <img> is on-screen is NOT evicted/revoked", async () => {
  const h = loadShell();
  h.fetchState.handler = async () => ({ ok: true, blob: async () => ({ type: "application/pdf" }) });
  const entry = await h.Shell._test.fetchMsgAttachment("PDF1");
  assert.ok(entry && entry.type === "application/pdf", "the PDF entry should be cached");
  // Simulate the render: the displayed <img> src is the SEPARATE page-1 thumb URL, recorded
  // on the entry as thumbUrlResolved (entry.url is the raw PDF blob, never shown).
  const thumbUrl = "blob:mock/pdf-thumb";
  entry.thumbUrl = Promise.resolve(thumbUrl); // (what renderPdfThumb would set)
  entry.thumbUrlResolved = thumbUrl;
  const box = h.doc.createElement("div"); h.doc.body.appendChild(box);
  const img = h.doc.createElement("img"); img.setAttribute("src", thumbUrl); box.appendChild(img);
  // The in-use guard must see the entry via thumbUrlResolved (not entry.url).
  assert.equal(h.Shell._test.attachEntryInUse(entry), true, "the PDF entry must read as IN-USE (thumb on screen)");
  // Overflow the cache with 120 other attachments → the eviction pass runs; the on-screen PDF survives.
  h.fetchState.handler = async () => ({ ok: true, blob: async () => ({ type: "image/png" }) });
  for (let i = 0; i < 120; i++) await h.Shell._test.fetchMsgAttachment(`img${i}`);
  assert.ok(h.Shell._test.attachCache.has("PDF1"), "the on-screen PDF must NOT be evicted");
  assert.ok(!h.revokedUrls.includes(thumbUrl), "the on-screen PDF thumb URL must NOT be revoked");
});

await check("fe-shell-ui-1 · suggestion chips follow a late language switch", () => {
  const h = loadShell({ withPromptChips: true });
  h.i18n.state.lang = "tr";
  h.win._fire("akana:languagechange", { lang: "tr" });
  const chips = h.doc.querySelector("#log-empty .prompt-chips");
  const titles = chips.querySelectorAll(".prompt-chip-title").map((n) => n.textContent);
  assert.ok(titles.length > 0, "the suggestion chips should have been rendered on the language switch");
  // At least one rendered chip title must equal a Turkish suggestion string.
  const anyTr = titles.some((tx) => [
    h.i18n.api.t("shell.ps_morning_title"), h.i18n.api.t("shell.ps_plan_title"),
    h.i18n.api.t("shell.ps_system_title"), h.i18n.api.t("shell.ps_idea_title"),
    h.i18n.api.t("shell.ps_learn_title"),
  ].includes(tx));
  assert.ok(anyTr, `chips must render in the new language, got ${JSON.stringify(titles)}`);
});

// ══════════════════════════════════════════════════════════════════════════════
// E. akana-chat-threads.js — fe-chat-core-1/2/3 + fe-shell-ui-2 (emit)
// ══════════════════════════════════════════════════════════════════════════════
function loadThreads() {
  const localStorage = makeStorage();
  const sessionStorage = makeStorage();
  const busEmits = [];
  const win = {
    addEventListener: () => {},
    localStorage, sessionStorage,
    AkanaCore: { baseUrl: () => "", authHeaders: () => ({}) },
    AkanaI18n: makeI18nStub(),
    AkanaBus: { emit: (e, p) => busEmits.push([e, p]), on: () => {} },
    AkanaChat: {},
  };
  win.window = win;
  const ctx = {
    console, setTimeout, clearTimeout, crypto: globalThis.crypto,
    localStorage, sessionStorage,
    document: { getElementById: () => null, body: { getAttribute: () => null }, addEventListener: () => {} },
    fetch: async () => ({ ok: false, json: async () => ({}) }),
    window: win,
  };
  vm.createContext(ctx);
  vm.runInContext(read("web_ui/static/akana-chat-store.js"), ctx);
  vm.runInContext(read("web_ui/static/akana-chat-threads.js"), ctx);
  vm.runInContext(read("web_ui/static/akana-chat-panes.js"), ctx);
  return { ctx, win, busEmits };
}

function setupThreads() {
  const { ctx, win, busEmits } = loadThreads();
  const paneContainer = { children: [] };
  const pm = ctx.window.AkanaChatPanes.createPaneManager({
    container: makeEl("div"),
    createEl: (t) => makeEl(t),
  });
  pm.show(null);
  const turnsByConv = new Map();
  const statusByConv = new Map(); // convId → forced status (overrides has/404)
  const deferred = new Map();
  const log = makeEl("div");
  const loadingCalls = [];
  const calls = { setForeground: [], showConversation: [], syncComposer: [] };
  const liveConvs = new Set();
  const bridge = {
    hooks: {
      log, logScroll: null,
      setLogLoading: (v) => loadingCalls.push(v),
      updateEmptyState: () => {}, scrollLogToBottom: () => {}, updateSettingsHero: () => {},
      shortConversationId: (id) => id || "none", loadMemoryConversations: () => {},
      appendSystemNotice: () => {}, showToast: () => {},
    },
    async fetchConversationTurns(convId) {
      if (deferred.has(convId)) await deferred.get(convId).promise;
      if (statusByConv.has(convId)) return { status: statusByConv.get(convId), turns: [] };
      const has = turnsByConv.has(convId);
      return { status: has ? 200 : 404, turns: has ? turnsByConv.get(convId) : [] };
    },
    abortConversationTurnsFetch: () => {},
    mapServerMessagesToThread: (turns) => (Array.isArray(turns) ? turns.slice() : []),
    chatRenderMessage: () => { log.appendChild(makeEl("div")); },
    abortStream: () => {},
    setForegroundConversation: (c) => calls.setForeground.push(c),
    showConversation: (c) => { calls.showConversation.push(c); pm.show(c); },
    removeConversation: (c) => pm.remove(c),
    rekeyConversation: (a, b) => pm.rekey(a, b),
    reattachLiveRow: () => false,
    isConversationStreamActive: (c) => liveConvs.has(c),
    syncComposerForDisplayed: (c) => calls.syncComposer.push(c),
    resumeActiveTurn: async () => false,
    probeActiveTurn: async () => null,
    cancelActiveTurnOnServer: async () => {},
  };
  const archiveStub = {
    createArchive() {
      let items = []; let meta = null;
      return {
        loadChatArchiveList: () => {}, insertConversationLocally: () => true,
        refreshActiveConversationMeta: () => {}, refreshConvActivityFromServer: () => {},
        clearConvActivity: () => {}, getChatArchiveItems: () => items, setChatArchiveItems: (v) => { items = v; },
        setActiveConversationHighlight: () => {}, getActiveConversationMeta: () => meta, setActiveConversationMeta: (v) => { meta = v; },
        syncChatThreadBar: () => {}, deleteConversationApi: async () => {}, patchConversationApi: async () => {},
        exportConversationMarkdown: () => {}, openArchiveDrawer: () => {}, closeArchiveDrawer: () => {},
        wireArchiveChrome: () => {}, wireThreadBar: () => {},
      };
    },
  };
  ctx.window.AkanaChatArchive = archiveStub;
  const T = ctx.window.AkanaChatThreads.create(bridge);
  return {
    T, bridge, busEmits, turnsByConv, statusByConv, deferred, loadingCalls, calls, liveConvs,
    markLive: (c) => liveConvs.add(c),
    store: () => T.getChatStore(),
    seedThread(convId, { active = false, messages = [], title = "New chat" } = {}) {
      const s = T.getChatStore();
      const tid = `seed-${convId || "null"}-${Object.keys(s.threads).length}`;
      s.threads[tid] = { id: tid, profile: "cursor", conversationId: convId || null, title, updatedAt: Date.now(), messages: messages.slice() };
      if (active) s.activeByProfile.cursor = tid;
      return s.threads[tid];
    },
  };
}
function deferral() { let resolve; const promise = new Promise((r) => { resolve = r; }); return { promise, resolve }; }

await check("fe-chat-core-1 · sync(A) on a 500 does NOT wipe A's confirmed messages", async () => {
  const h = setupThreads();
  const a = h.seedThread("A", { active: true, messages: [{ kind: "user", text: "u1" }, { kind: "assistant", text: "a1" }] });
  h.turnsByConv.set("A", [{ kind: "user", text: "u1" }, { kind: "assistant", text: "a1" }]);
  h.statusByConv.set("A", 500);   // transient server error → transport returns turns:[]
  const ok = await h.T.syncConversationLogFromServer("A");
  assert.equal(ok, false, "a 5xx sync must fail (not report success)");
  assert.equal(a.messages.length, 2, "A's confirmed messages must NOT be wiped by an empty 5xx snapshot");
});

await check("fe-chat-core-1 · hydrate(A) on a 503 keeps local (returns false, no wipe)", async () => {
  const h = setupThreads();
  const a = h.seedThread("A", { active: true, messages: [{ kind: "user", text: "keep" }] });
  h.turnsByConv.set("A", [{ kind: "user", text: "keep" }]);
  h.statusByConv.set("A", 503);
  const ok = await h.T.chatHydrateFromServer("A", a);
  assert.equal(ok, false, "a 503 hydrate must return false");
  assert.equal(a.messages.length, 1, "the confirmed message must be preserved");
  assert.equal(a.conversationId, "A", "the conv binding must be preserved");
});

await check("fe-chat-core-1 · reload(A) on a 500 does NOT wipe the visible history", async () => {
  const h = setupThreads();
  const a = h.seedThread("A", { active: true, messages: [{ kind: "user", text: "x" }, { kind: "assistant", text: "y" }] });
  h.statusByConv.set("A", 500);
  const ok = await h.T.reloadConversationLogFromServer("A");
  assert.equal(ok, false, "a 500 reload must bail");
  assert.equal(a.messages.length, 2, "the visible history must be preserved on a transient 500");
});

await check("fe-chat-core-2 · a stale 404-destroy does NOT clobber a newer switch's global conv", async () => {
  const h = setupThreads();
  // C is live (fast-path switch, no hydrate). B is unknown server-side (404) with no local cache.
  h.seedThread("C", { active: true, messages: [{ kind: "user", text: "c" }] });
  h.markLive("C");
  // Switch to B (no local, deferred fetch) → in-flight hydrate; then switch to C (live fast-path).
  const dB = deferral();
  h.deferred.set("B", dB);           // B's turns fetch is suspended
  const pB = h.T.switchChatConversation("B");   // gen=1, creates empty B thread, awaits hydrate
  await h.T.switchChatConversation("C");         // gen=2, C live → completes; active=C
  assert.equal(h.T.conversationIdForMemory(), "C", "precondition: C is the active conv after the 2nd switch");
  dB.resolve();                       // B's fetch resolves 404 → destroy branch runs LATE
  await pB;
  assert.equal(h.T.conversationIdForMemory(), "C", "the stale 404 must NOT null the newer C conv");
  assert.equal(h.T.chatActiveThread().conversationId, "C", "C's thread must keep its conversationId");
});

await check("fe-chat-core-3 · a superseded switch's persist-pause + log-loading are reset", async () => {
  const h = setupThreads();
  h.seedThread("Z", { active: true, messages: [] });
  // Switch to B (no local cache → loading stays true; deferred → stays in-flight).
  const dB = deferral();
  h.deferred.set("B", dB);
  const pB = h.T.switchChatConversation("B");
  assert.equal(h.T.getChatPersistPaused(), true, "the in-flight switch should have paused persistence");
  h.loadingCalls.length = 0;
  // The user opens a NEW chat, superseding the in-flight switch.
  await h.T.chatStartNewThread({ force: true, localOnly: true });
  assert.equal(h.T.getChatPersistPaused(), false, "the superseding op must reset chatPersistPaused");
  assert.ok(h.loadingCalls.includes(false), "the superseding op must clear the log-loading flag");
  dB.resolve();       // B's stale hydrate resolves → its finally is skipped (gen mismatch)
  await pB;
  assert.equal(h.T.getChatPersistPaused(), false, "persistence must stay un-paused after the stale switch resolves");
});

await check("fe-shell-ui-2 · setConversationId emits chat:conversation:changed only when leaving a REAL conv", () => {
  const h = setupThreads();
  h.seedThread("A", { active: true, messages: [] });
  // review finding 4: a null→id transition (mid-turn server-id ADOPTION, or first open
  // from an empty surface) must NOT emit — it would yank the mobile bottom-tab highlight
  // back to Chat while the user sits on the Settings tab.
  h.busEmits.length = 0;
  h.T.setConversationId("A"); // prev = null (adoption)
  assert.ok(
    !h.busEmits.some(([e]) => e === "chat:conversation:changed"),
    "a null→id adoption must NOT emit chat:conversation:changed",
  );
  // A real switch (id → other-id) DOES emit.
  h.busEmits.length = 0;
  h.T.setConversationId("B");
  const evt = h.busEmits.find(([e]) => e === "chat:conversation:changed");
  assert.ok(evt, "a real id→other-id switch must emit chat:conversation:changed");
  assert.equal(evt[1].conversationId, "B", "the emitted payload carries the new conversationId");
  // New-chat / delete (id → null) DOES emit (returns the highlight to Chat).
  h.busEmits.length = 0;
  h.T.setConversationId(null);
  const evt2 = h.busEmits.find(([e]) => e === "chat:conversation:changed");
  assert.ok(evt2, "id→null (new-chat/delete) must emit");
  assert.equal(evt2[1].conversationId, null, "the payload carries null on new-chat/delete");
  // No emit when the id does not change.
  h.busEmits.length = 0;
  h.T.setConversationId(null);
  assert.ok(!h.busEmits.some(([e]) => e === "chat:conversation:changed"), "no re-emit when the id is unchanged (null→null)");
});

// ══════════════════════════════════════════════════════════════════════════════
// F. akana-chat.js — fe-chat-core-4 (safety timer), fe-chat-core-5 (attach restore),
//    fe-chat-core-6 (flashOk)
// ══════════════════════════════════════════════════════════════════════════════
function loadChat({ timers, threadsStub, transportStub } = {}) {
  const doc = makeDoc();
  const mk = (id, tag = "div") => { const e = doc.createElement(tag); e.id = id; doc.body.appendChild(e); doc._register(id, e); return e; };
  mk("log"); mk("log-scroll"); const form = mk("chat-form", "form"); const msg = mk("msg", "textarea");
  const sendBtn = mk("btn-send", "button"); mk("composer-attachments"); mk("log-empty");
  form.querySelector = (sel) => collectAll(form, sel)[0] || null;
  const win = makeWindow();
  const i18n = makeSwitchableI18n();
  const t2 = timers || makeTimers();
  Object.assign(win, {
    AkanaI18n: i18n.api,
    AkanaCore: { baseUrl: () => "", authHeaders: () => ({}), authHeadersMultipart: () => ({}), escapeHtml: (s) => s, parseApiError: (b, s) => `HTTP ${s}` },
    AkanaChatRender: { createRenderer: () => ({ chatRenderMessage: () => {} }), mapServerMessagesToThread: (m) => m },
    AkanaChatThreads: { create: () => threadsStub },
    AkanaChatTransport: { create: () => transportStub },
    AkanaShell: { displayedPane: () => doc.getElementById("log"), paneFor: () => doc.getElementById("log"), displayedConvId: () => "" },
    AkanaTurnStatus: { mount: () => {}, begin: () => {}, resume: () => {}, end: () => {}, isActive: () => false, setPhase: () => {} },
    AkanaBus: { emit: () => {}, on: () => {} },
    AkanaVoice: {},
    document: doc,
    setTimeout: t2.setTimeout, clearTimeout: t2.clearTimeout,
    localStorage: makeStorage(),
  });
  win.window = win;
  const ctx = {
    console, document: doc, window: win,
    setTimeout: t2.setTimeout, clearTimeout: t2.clearTimeout, setInterval: t2.setInterval, clearInterval: t2.clearInterval,
    queueMicrotask, Promise, URLSearchParams, FormData: class { append() {} },
    Event: class { constructor(t) { this.type = t; } },
    navigator: { clipboard: { writeText: async () => {} } },
    localStorage: win.localStorage,
    requestAnimationFrame: (fn) => { fn(); }, cancelAnimationFrame: () => {},
  };
  vm.createContext(ctx);
  vm.runInContext(read("web_ui/static/akana-chat.js"), ctx);
  return { doc, win, i18n, timers: t2, form, msg, sendBtn, Chat: win.AkanaChat };
}

// Minimal hooks so submitChatText can run without the real shell.
function chatHooks(doc) {
  return {
    log: doc.getElementById("log"), logScroll: doc.getElementById("log-scroll"),
    form: doc.getElementById("chat-form"), msg: doc.getElementById("msg"), sendBtn: doc.getElementById("btn-send"),
    logEmpty: doc.getElementById("log-empty"),
    appendRow: () => null, appendUserMessage: () => null, appendSystemNotice: () => {},
    updateEmptyState: () => {}, resizeComposer: () => {}, setOrb: () => {}, setComposerHint: () => {},
    stickToBottomIfFollowing: () => {}, scrollLogToBottom: () => {}, scrollNewTurnToTop: () => {}, setLogLoading: () => {},
    showToast: () => {}, streamTtsParam: () => "", syncOrbWithVoice: () => {}, updateSettingsHero: () => {},
    loadMemoryConversations: () => {}, shortConversationId: (id) => id || "none", closeSettings: () => {},
  };
}

await check("fe-chat-core-4 · the 10-min safety timer aborts THIS turn's own stream, never the foreground", async () => {
  const timers = makeTimers();
  const abortArgs = [];
  // Brand-new chat: conversationIdForMemory()="" at capture; the turn's thread gets bound to NEW1.
  const turnThread = { conversationId: "NEW1" };
  const threadsStub = {
    conversationIdForMemory: () => "", chatActiveThread: () => turnThread,
    recordPendingUserMessage: () => {}, tryHandleChatDeleteCommand: () => false,
    chatProfile: () => "cursor", newChatThreadId: () => "t", chatStartNewThread: () => {},
    wireArchiveChrome: () => {}, wireThreadBar: () => {}, getChatStore: () => ({ threads: {}, activeByProfile: {} }),
    chatRestoreActiveThread: () => {}, loadChatArchiveList: () => {}, openArchiveDrawer: () => {}, closeArchiveDrawer: () => {},
    setConversationId: () => {}, recordErrorForConversation: () => true, chatRecordMessage: () => {},
    syncChatThreadBar: () => {}, refreshActiveConversationMeta: () => {},
  };
  let hangResolve;
  const transportStub = {
    streamChat: () => new Promise((r) => { hangResolve = r; }),  // never resolves during the test
    isConversationStreamActive: () => false,
    abortActiveChatStream: (c) => abortArgs.push(c),
    cancelActiveTurnOnServer: async () => {}, humanizeChatError: (e) => String(e),
    ensureConversationIdReady: async () => "NEW1", fetchConversationTurnsFromServer: async () => ({ status: 404, turns: null }),
    setForegroundConversation: () => {}, abortConversationTurnsFetch: () => {},
    resumeActiveTurn: async () => false, probeActiveTurn: async () => null,
  };
  const h = loadChat({ timers, threadsStub, transportStub });
  h.Chat.init(chatHooks(h.doc));
  void h.Chat.submitAnswerText("hello");    // fire the turn (streamChat hangs)
  await Promise.resolve(); await Promise.resolve();
  timers.fireDelay(10 * 60 * 1000);          // fire the 10-min safety timer
  assert.ok(abortArgs.length >= 1, "the safety timer should abort a stream");
  assert.equal(abortArgs[0], "NEW1", "it must abort THIS turn's own conv (NEW1), NOT undefined (the foreground)");
  assert.ok(!abortArgs.includes(undefined), "undefined (→ transport foreground fallback) must never be passed");
  if (hangResolve) hangResolve({});
});

await check("fe-chat-core-5 · a pre-stream failure returns the consumed attachments to the composer", async () => {
  const abortArgs = [];
  const turnThread = { conversationId: "" };
  const threadsStub = {
    conversationIdForMemory: () => "", chatActiveThread: () => turnThread,
    recordPendingUserMessage: () => {}, tryHandleChatDeleteCommand: () => false,
    chatProfile: () => "cursor", newChatThreadId: () => "t", chatStartNewThread: () => {},
    wireArchiveChrome: () => {}, wireThreadBar: () => {}, getChatStore: () => ({ threads: {}, activeByProfile: {} }),
    chatRestoreActiveThread: () => {}, loadChatArchiveList: () => {}, openArchiveDrawer: () => {}, closeArchiveDrawer: () => {},
    setConversationId: () => {}, recordErrorForConversation: () => true, chatRecordMessage: () => {},
    syncChatThreadBar: () => {}, refreshActiveConversationMeta: () => {},
  };
  const transportStub = {
    streamChat: async () => { const e = new Error("TURN_BUSY"); throw e; },   // pre-stream throw (no errorCardShown)
    isConversationStreamActive: () => false,
    abortActiveChatStream: (c) => abortArgs.push(c),
    cancelActiveTurnOnServer: async () => {}, humanizeChatError: (e) => String(e && e.message || e),
    ensureConversationIdReady: async () => "", fetchConversationTurnsFromServer: async () => ({ status: 404, turns: null }),
    setForegroundConversation: () => {}, abortConversationTurnsFetch: () => {},
    resumeActiveTurn: async () => false, probeActiveTurn: async () => null,
  };
  const h = loadChat({ threadsStub, transportStub });
  h.Chat.init(chatHooks(h.doc));
  h.Chat._test.seedPendingAttachment({ id: "F1", name: "pic.png", kind: "image", size: 10, previewUrl: "blob:x" });
  assert.equal(h.Chat._test.getPendingAttachments().length, 1, "precondition: one pending attachment");
  await h.Chat.submitAnswerText("here is a pic");   // consumes attachment, streamChat throws pre-stream
  const restored = h.Chat._test.getPendingAttachments();
  assert.equal(restored.length, 1, "the attachment must be RETURNED to the composer after a pre-stream failure");
  assert.equal(restored[0].id, "F1", "the same file id must be restored (Retry re-sends it)");
});

await check("fe-chat-core-6 · flashOk does not stick a button on '✓' after a rapid double-click", () => {
  const timers = makeTimers();
  const threadsStub = {
    conversationIdForMemory: () => "", chatActiveThread: () => null, recordPendingUserMessage: () => {},
    tryHandleChatDeleteCommand: () => false, chatProfile: () => "cursor", newChatThreadId: () => "t",
    chatStartNewThread: () => {}, wireArchiveChrome: () => {}, wireThreadBar: () => {},
    getChatStore: () => ({ threads: {}, activeByProfile: {} }), chatRestoreActiveThread: () => {},
    loadChatArchiveList: () => {}, openArchiveDrawer: () => {}, closeArchiveDrawer: () => {},
    setConversationId: () => {}, recordErrorForConversation: () => true, chatRecordMessage: () => {},
    syncChatThreadBar: () => {}, refreshActiveConversationMeta: () => {},
  };
  const transportStub = {
    streamChat: async () => ({}), isConversationStreamActive: () => false, abortActiveChatStream: () => {},
    cancelActiveTurnOnServer: async () => {}, humanizeChatError: (e) => String(e),
    ensureConversationIdReady: async () => "", fetchConversationTurnsFromServer: async () => ({ status: 404, turns: null }),
    setForegroundConversation: () => {}, abortConversationTurnsFetch: () => {},
    resumeActiveTurn: async () => false, probeActiveTurn: async () => null,
  };
  const h = loadChat({ timers, threadsStub, transportStub });
  const flashOk = h.win.AkanaMsgActionBar._flashOk;   // reentrancy-guarded flash (test seam)
  assert.equal(typeof flashOk, "function", "the flashOk seam should be exposed");
  const btn = h.doc.createElement("button");
  btn.textContent = "Copy";                            // the real, localized label
  // Two rapid flashes within the 1.2s window (the natural double-click gesture).
  flashOk(btn, true);
  assert.equal(btn.textContent, "✓", "the first flash shows the checkmark");
  flashOk(btn, true);                                  // second click BEFORE the first timer fires
  h.timers.fireDelay(1200);                            // first restore timer
  h.timers.fireDelay(1200);                            // second restore timer
  assert.notEqual(btn.textContent, "✓", "the button must NOT be stuck on '✓' after a double-click");
  assert.equal(btn.textContent, "Copy", "the button must restore to its TRUE original label");
});

// ── Summary ──────────────────────────────────────────────────────────────────
if (failures) {
  console.error(`\nblitz3_fe-shell-chat-core: ${passed} passed, ${failures} FAILED`);
  process.exit(1);
}
console.log(`blitz3_fe-shell-chat-core: ${passed} contracts passed ✓`);
process.exit(0);
