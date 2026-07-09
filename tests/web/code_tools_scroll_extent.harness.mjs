/**
 * Code-copy capsule ↔ conversation-switch SCROLL-EXTENT contract — node-vm + fake-DOM.
 *
 * User report: after a LONG chat, switching to a new/other chat let the page
 * scroll thousands of px into EMPTY space (old chat's scroll extent survived).
 * Root cause: the `.akana-code-tools` capsule (code-block "Copy" + lang badge)
 * floats ABSOLUTELY in #log-scroll as a SIBLING of #log. Hover set its `top` to
 * the hovered block's depth; hover-out only faded it (opacity, `top` kept) and a
 * conversation switch never dismissed it → an invisible box stranded at e.g.
 * top:2888px kept the scroller's scrollable overflow at the OLD chat's height
 * (and the capsule itself floated visibly over the new chat).
 *
 * Contract locked here (akana-shell.js):
 *   1. the capsule is born [hidden] (no phantom scroll before first hover),
 *   2. hover on a bot-bubble <pre> shows + positions it (feature intact),
 *   3. showConversation() to ANOTHER conversation fully dismisses it:
 *      [hidden] + cleared top/right + .is-visible removed,
 *   4. hover after a dismiss shows it again (dismiss is recoverable).
 *
 * Hermetic: real akana-chat-panes.js + akana-shell.js in node-vm; DOM/events faked.
 * Run: node tests/web/code_tools_scroll_extent.harness.mjs
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
function check(label, fn) {
  fn();
  passed += 1;
  void label; // silent: summary at the end; label only surfaces in assert messages
}

// ───────────────────────── Fake-DOM (only used surfaces) ────────────────────
// A real class so `target instanceof Element` inside the shell's hover delegate
// works (ctx.Element is set to this class below).
class FakeEl {
  constructor(tag = "div") {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.dataset = {};
    this.style = {};
    this.attrs = {};
    this._text = "";
    this._html = "";
    this.hidden = false;
    this._listeners = {};
    this._rect = { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
    this.scrollTop = 0;
    this.scrollLeft = 0;
    const s = new Set();
    this.classList = {
      _s: s,
      add(...c) { c.forEach((x) => s.add(x)); },
      remove(...c) { c.forEach((x) => s.delete(x)); },
      toggle(c, on) { const w = on === undefined ? !s.has(c) : on; if (w) s.add(c); else s.delete(c); return w; },
      contains(c) { return s.has(c); },
    };
  }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = String(v); if (v === "") this.children = []; }
  get className() { return [...this.classList._s].join(" "); }
  set className(v) { this.classList._s.clear(); String(v).split(/\s+/).filter(Boolean).forEach((c) => this.classList._s.add(c)); }
  get parentElement() { return this.parentNode; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  removeAttribute(k) { delete this.attrs[k]; }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
  append(...cs) { cs.forEach((c) => (typeof c === "object" ? this.appendChild(c) : null)); }
  remove() { const p = this.parentNode; if (p) p.children = p.children.filter((c) => c !== this); this.parentNode = null; }
  addEventListener(t, fn) { (this._listeners[t] ||= []).push(fn); }
  fire(type, ev) { for (const fn of this._listeners[type] || []) fn(ev); }
  getBoundingClientRect() { return this._rect; }
  _matches(sel) {
    return sel.split(",").map((x) => x.trim()).some((tok) =>
      tok.startsWith(".") ? this.classList.contains(tok.slice(1)) : this.tagName === tok.toUpperCase(),
    );
  }
  closest(sel) {
    let n = this;
    while (n) { if (n._matches?.(sel)) return n; n = n.parentNode; }
    return null;
  }
  contains(node) {
    let n = node;
    while (n) { if (n === this) return true; n = n.parentNode; }
    return false;
  }
  querySelector(sel) {
    for (const c of this.children) {
      if (c._matches?.(sel)) return c;
      const hit = c.querySelector?.(sel);
      if (hit) return hit;
    }
    return null;
  }
  querySelectorAll() { return []; }
}

// ───────────────────────── Boot the real shell + panes ──────────────────────
const logEl = new FakeEl("div");
const scroller = new FakeEl("div");
scroller._rect = { top: 40, left: 0, right: 840, bottom: 573, width: 840, height: 533 };
logEl.parentNode = scroller; // #log lives inside #log-scroll
scroller.children.push(logEl);

const byId = { log: logEl, "log-scroll": scroller };
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
  Element: FakeEl, // hover delegate: `target instanceof Element`
  requestAnimationFrame: (fn) => { setTimeout(fn, 0); return 1; },
  cancelAnimationFrame: () => {},
  fetch: async () => ({ ok: true, json: async () => ({ items: [] }) }),
};
ctx.window.window = ctx.window;
ctx.window.document = doc;
vm.runInNewContext(read("akana-chat-panes.js"), ctx); // real pane manager
vm.runInNewContext(read("akana-shell.js"), ctx);

const Shell = ctx.window.AkanaShell;
Shell.init({
  log: logEl,
  logScroll: scroller,
  logEmpty: null,
  msg: null,
  form: null,
  orb: null,
  escapeHtml: (s) => String(s ?? ""),
});

const tools = scroller.querySelector(".akana-code-tools");
check("capsule exists as a scroller child", () => assert.ok(tools, ".akana-code-tools must be wired into #log-scroll"));

// ── 1. Born hidden: no phantom scroll contribution before the first hover ───
check("capsule is born [hidden]", () =>
  assert.equal(tools.hidden, true, "capsule must start [hidden] — its box would otherwise pad every fresh chat's scroll extent"));

// ── 2. Hover a bot-bubble <pre> deep in a LONG chat → capsule shows there ───
// DOM: displayedPane > row > .bubble-assistant > pre[data-lang] (markdown shape).
const pane = Shell.displayedPane();
check("pane manager provides the displayed pane", () => assert.ok(pane, "displayed pane must exist after init"));
const row = new FakeEl("div");
const bubble = new FakeEl("div");
bubble.className = "bubble-assistant";
const pre = new FakeEl("pre");
pre.className = "md-code";
pre.setAttribute("data-lang", "python");
pre._rect = { top: 2400, left: 60, right: 700, bottom: 2560, width: 640, height: 160 };
bubble.appendChild(pre);
row.appendChild(bubble);
pane.appendChild(row);
scroller.scrollTop = 1800; // user scrolled deep into the long chat

scroller.fire("mouseover", { target: pre });
check("hover shows the capsule (feature intact)", () => {
  assert.equal(tools.hidden, false, "hover must unhide the capsule");
  assert.ok(tools.classList.contains("is-visible"), "hover must add .is-visible");
});
// top = preTop(2400) - scrollerTop(40) + scrollTop(1800) + 6 = 4166 → deep anchor set
check("hover anchors the capsule at the block's depth", () =>
  assert.equal(tools.style.top, "4166px", `expected deep top anchor, got "${tools.style.top}"`));

// ── 3. Conversation switch → FULL dismiss (the regression under test) ───────
Shell.showConversation("other-conv");
check("switch really changed the displayed conversation", () =>
  assert.equal(Shell.displayedConvId(), "other-conv"));
check("switch dismisses the capsule with [hidden]", () =>
  assert.equal(tools.hidden, true,
    "capsule must be [hidden] after a conversation switch — an opacity-0 box stranded at a deep `top` keeps the OLD chat's scroll extent alive on the new chat"));
check("switch clears the stale deep `top` anchor", () =>
  assert.equal(tools.style.top, "",
    `capsule top must be cleared on switch (was left at "${tools.style.top}") — it inflates the new chat's scrollHeight`));
check("switch removes .is-visible (no Copy button floating over the new chat)", () =>
  assert.equal(tools.classList.contains("is-visible"), false));

// ── 4. Dismiss is recoverable: hover in the new chat shows the capsule again ─
const pane2 = Shell.displayedPane();
const row2 = new FakeEl("div");
const bubble2 = new FakeEl("div");
bubble2.className = "bubble-assistant";
const pre2 = new FakeEl("pre");
pre2.setAttribute("data-lang", "js");
pre2._rect = { top: 140, left: 60, right: 700, bottom: 260, width: 640, height: 120 };
bubble2.appendChild(pre2);
row2.appendChild(bubble2);
pane2.appendChild(row2);
scroller.scrollTop = 0;

scroller.fire("mouseover", { target: pre2 });
check("hover after a dismiss shows the capsule again", () => {
  assert.equal(tools.hidden, false, "post-dismiss hover must unhide the capsule");
  assert.ok(tools.classList.contains("is-visible"));
  assert.equal(tools.style.top, "106px", `capsule must re-anchor to the new block, got "${tools.style.top}"`);
});

console.log(`code_tools_scroll_extent.harness: ${passed} scroll-extent contracts PASSED ✓`);

// Dangling timer node must not hang the process — hard exit on success.
if (typeof process !== "undefined" && process.exit) process.exit(0);
