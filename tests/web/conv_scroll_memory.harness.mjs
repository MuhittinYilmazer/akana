/**
 * PER-CONVERSATION SCROLL MEMORY contract — node-vm + fake-DOM, backend-free.
 *
 * User report: "switching between chats starts where the PREVIOUS chat was, not where we
 * left off." Root cause: #log-scroll is ONE scroller shared by every conversation pane
 * (switching only shows/hides panes), and nothing saved/restored a per-conversation
 * offset — so the leaving chat's scrollTop carried over verbatim (browser only clamps it
 * when the target is shorter), and the reading position in a chat was lost on every switch.
 *
 * Contract locked here (akana-shell.js):
 *   1. opening another chat does NOT inherit the previous chat's offset,
 *   2. returning to a chat restores the position it was left at,
 *   3. a chat left AT THE BOTTOM restores to the NEW bottom after its content grew
 *      (follow semantics — not a stale pixel offset),
 *   4. a deleted chat's remembered offset is dropped,
 *   5. programmatic scrolls are INSTANT (the CSS `scroll-behavior: smooth` on the scroller
 *      would otherwise animate them: stream-follow lags and a mid-animation scrollTop read
 *      is stale, which silently stops the follow).
 *
 * Run: node tests/web/conv_scroll_memory.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const read = (rel) => readFileSync(path.join(REPO, "web_ui/static", rel), "utf8");

let passed = 0;
const check = (label, fn) => { fn(); passed += 1; void label; };

// ── Fake DOM ────────────────────────────────────────────────────────────────
class FakeEl {
  constructor(tag = "div") {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.style = {};
    this.attrs = {};
    this.dataset = {};
    this.hidden = false;
    this._listeners = {};
    this._html = "";
    this._scrollTop = 0;
    this.contentHeight = 0; // test-controlled: drives scrollHeight
    this.clientHeight = 500;
    const s = new Set();
    this.classList = {
      add: (...c) => c.forEach((x) => s.add(x)),
      remove: (...c) => c.forEach((x) => s.delete(x)),
      toggle: (c, on) => { const w = on === undefined ? !s.has(c) : on; if (w) s.add(c); else s.delete(c); return w; },
      contains: (c) => s.has(c),
    };
  }
  get scrollHeight() { return Math.max(this.contentHeight, this.clientHeight); }
  // Browsers CLAMP scrollTop to [0, scrollHeight - clientHeight]; the fake must too, or a
  // "scroll to the bottom" (scrollTop = scrollHeight) would read back over-scrolled and
  // the shared-scroller carry-over would look different here than in the real app.
  get scrollTop() { return this._scrollTop; }
  set scrollTop(v) {
    const max = Math.max(0, this.scrollHeight - this.clientHeight);
    this._scrollTop = Math.min(Math.max(0, Number(v) || 0), max);
  }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = String(v); if (v === "") this.children = []; }
  set className(v) { String(v).split(/\s+/).filter(Boolean).forEach((c) => this.classList.add(c)); }
  get className() { return ""; }
  get parentElement() { return this.parentNode; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  removeAttribute(k) { delete this.attrs[k]; }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
  remove() { const p = this.parentNode; if (p) p.children = p.children.filter((c) => c !== this); this.parentNode = null; }
  addEventListener(t, fn) { (this._listeners[t] ||= []).push(fn); }
  fire(type, ev) { for (const fn of this._listeners[type] || []) fn(ev); }
  getBoundingClientRect() { return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  contains() { return false; }
}

const logEl = new FakeEl("div");
const scroller = new FakeEl("div");
scroller.clientHeight = 500;
const byId = { log: logEl, "log-scroll": scroller };

// rAF shim we can drain deterministically (the real double-rAF restore must settle).
let rafQueue = [];
const flush = () => {
  for (let i = 0; i < 8 && rafQueue.length; i++) {
    const q = rafQueue;
    rafQueue = [];
    q.forEach((fn) => fn());
  }
};

const doc = {
  getElementById: (id) => byId[id] || null,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: (t) => new FakeEl(t),
  addEventListener: () => {},
};
const ctx = {
  window: {
    AkanaCore: { baseUrl: () => "http://x", authHeaders: () => ({}), escapeHtml: (s) => String(s ?? "") },
    AkanaI18n: { t: (k) => k, getLanguage: () => "en" },
    addEventListener: () => {},
  },
  document: doc,
  console,
  setTimeout,
  clearTimeout,
  AbortController,
  Element: FakeEl,
  requestAnimationFrame: (fn) => { rafQueue.push(fn); return rafQueue.length; },
  cancelAnimationFrame: () => {},
  fetch: async () => ({ ok: true, json: async () => ({ items: [] }) }),
};
ctx.window.window = ctx.window;
ctx.window.document = doc;
vm.runInNewContext(read("akana-chat-panes.js"), ctx);
vm.runInNewContext(read("akana-shell.js"), ctx);

const Shell = ctx.window.AkanaShell;
Shell.init({ log: logEl, logScroll: scroller, logEmpty: null, msg: null, form: null, orb: null,
  escapeHtml: (s) => String(s ?? "") });

/** Show a conversation and let the restore's double-rAF settle. */
const open = (id, contentHeight) => {
  Shell.showConversation(id);
  if (contentHeight !== undefined) scroller.contentHeight = contentHeight;
  flush();
};
/** Simulate the user scrolling (the shell records on the scroll event). */
const userScrollTo = (top) => { scroller.scrollTop = top; scroller.fire("scroll", {}); flush(); };

// ── 1. another chat must NOT open at the previous chat's offset ──────────────
open("convA", 3000);
userScrollTo(1500);                       // reading mid-way through the long chat A
check("A holds the user's offset", () => assert.equal(scroller.scrollTop, 1500));

open("convC", 3000);                      // a DIFFERENT long chat (enough extent to keep 1500)
check("opening another chat does not inherit the previous chat's offset", () =>
  assert.notEqual(scroller.scrollTop, 1500,
    "convC opened at convA's offset — the shared scroller carried it over"));
check("a first-visit chat opens at its own bottom", () =>
  assert.equal(scroller.scrollTop, 3000 - 500));

// ── 2. returning to a chat restores where it was left ───────────────────────
open("convA");
check("returning to A restores its reading position", () =>
  assert.equal(scroller.scrollTop, 1500, "A's reading position was lost on the round-trip"));

// ── 3. a chat left AT THE BOTTOM follows the new bottom after content grew ──
open("convB", 1200);
userScrollTo(1200 - 500);                 // sitting at the bottom of B
open("convA");                            // leave…
scroller.contentHeight = 4000;            // …B receives a new turn while away
open("convB");
check("a chat left at the bottom restores to the NEW bottom, not a stale offset", () =>
  assert.equal(scroller.scrollTop, 4000 - 500));

// ── 4. a deleted chat's memory is dropped ──────────────────────────────────
open("convA");
userScrollTo(900);
Shell.removeConversation("convA");
open("convA", 3000);                      // re-created/visited again → no stale offset
check("a deleted chat's remembered offset is forgotten", () =>
  assert.equal(scroller.scrollTop, 3000 - 500));

// ── 5. programmatic scrolls are instant (CSS smooth defeated) ──────────────
check("restore leaves scroll-behavior restored after forcing an instant jump", () =>
  assert.ok(!scroller.style.scrollBehavior,
    `scroll-behavior must be cleared again after the instant jump, got ${String(scroller.style.scrollBehavior)}`));

const before = scroller.scrollTop;
scroller.contentHeight = 5000;
Shell.scrollLogToBottom(scroller);
flush();
check("scrollLogToBottom lands exactly at the bottom in one jump", () => {
  assert.notEqual(scroller.scrollTop, before);
  assert.equal(scroller.scrollTop, 5000 - 500);
});

console.log(`conv_scroll_memory.harness: ${passed} scroll-memory contracts PASSED ✓`);
if (typeof process !== "undefined" && process.exit) process.exit(0);
