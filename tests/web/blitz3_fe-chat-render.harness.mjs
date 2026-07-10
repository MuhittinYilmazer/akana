/**
 * Bug-blitz 3 — fe-chat-render regression harness (backend-free, node-vm + fake DOM).
 * Loads the REAL web_ui/static/akana-markdown.js and akana-chat-render.js and asserts
 * the seven verified fixes hold. Each finding runs in its own block; failures are
 * collected and printed so RED (pre-fix) shows every still-broken finding at once.
 *
 *  1. Fence-blind preprocessing corrupted fenced code (rendered + copied).
 *  2. A lone mid-sentence '#' split prose into a spurious <h1>.
 *  3. looksLikeGrepPattern hijacked dotted tool names (memory.search → grep).
 *  4. Streaming decorate-guard read the flag off #log, not the streaming pane.
 *  5. renderTermCard dropped the rawName fallback for shell-as-toolname calls.
 *  6. History/F5 term cards + subagent groups showed a fabricated 0ms elapsed.
 *  7. A top-level TodoWrite hijacked a subagent's nested checklist card.
 * Run: node tests/web/blitz3_fe-chat-render.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { makeI18nStub } from "./_i18n_stub.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const MD_PATH = path.join(REPO, "web_ui/static/akana-markdown.js");
const RENDER_PATH = path.join(REPO, "web_ui/static/akana-chat-render.js");

const renderSrc = readFileSync(RENDER_PATH, "utf8");

// ── Fake DOM ──────────────────────────────────────────────────────────────────
// `Element` is a real class so the render module's `node instanceof Element` guard
// (MutationObserver path) accepts our nodes.
class Element {}
function makeEl(tag = "div") {
  const el = Object.create(Element.prototype);
  Object.assign(el, {
    tagName: String(tag).toUpperCase(),
    children: [],
    childNodes: [],
    dataset: {},
    _listeners: {},
    classList: {
      _s: new Set(),
      add(...c) { c.forEach((x) => this._s.add(x)); },
      remove(...c) { c.forEach((x) => this._s.delete(x)); },
      toggle(c, on) {
        const has = this._s.has(c);
        const want = on === undefined ? !has : !!on;
        if (want) this._s.add(c); else this._s.delete(c);
        return want;
      },
      contains(c) { return this._s.has(c); },
    },
    style: {},
    attrs: {},
    _text: "",
    disabled: false,
    hidden: false,
    value: "",
    type: "",
    placeholder: "",
    title: "",
    innerHTML: "",
    parentNode: null,
  });
  Object.defineProperties(el, {
    textContent: {
      get() { return this._text; },
      set(v) {
        this._text = String(v);
        if (v === "") { this.children = []; this.childNodes = []; }
      },
    },
    className: {
      get() { return [...this.classList._s].join(" "); },
      set(v) { this.classList._s = new Set(String(v).split(/\s+/).filter(Boolean)); },
    },
    isConnected: { get() { return true; }, configurable: true },
  });
  el.setAttribute = function (k, v) { this.attrs[k] = String(v); };
  el.getAttribute = function (k) { return this.attrs[k] ?? null; };
  el.removeAttribute = function (k) { delete this.attrs[k]; };
  el.appendChild = function (c) { this.children.push(c); this.childNodes.push(c); c.parentNode = this; return c; };
  el.append = function (...cs) { cs.forEach((c) => this.appendChild(c)); };
  el.insertBefore = function (node, ref) {
    const i = this.children.indexOf(ref);
    if (i < 0) this.children.push(node);
    else this.children.splice(i, 0, node);
    this.childNodes = this.children;
    node.parentNode = this;
    return node;
  };
  el.replaceWith = function (node) {
    if (!this.parentNode) return;
    const i = this.parentNode.children.indexOf(this);
    if (i >= 0) this.parentNode.children.splice(i, 1, node);
    node.parentNode = this.parentNode;
    this.parentNode = null;
    // keep childNodes mirror consistent
    if (node.parentNode) node.parentNode.childNodes = node.parentNode.children;
  };
  el.addEventListener = function (type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); };
  el.dispatch = function (type, evt = {}) { for (const fn of this._listeners[type] || []) fn({ preventDefault() {}, ...evt }); };
  el.click = function () { this.dispatch("click"); };
  el.focus = function () {};
  el.remove = function () {
    if (this.parentNode) {
      const i = this.parentNode.children.indexOf(this);
      if (i >= 0) this.parentNode.children.splice(i, 1);
      this.parentNode = null;
    }
  };
  el.contains = function (node) {
    let n = node;
    while (n) { if (n === this) return true; n = n.parentNode; }
    return false;
  };
  el.closest = function (sel) {
    const { simple } = parseSel(sel);
    let n = this;
    while (n) { if (selMatch(n, simple)) return n; n = n.parentNode; }
    return null;
  };
  el.querySelector = function (sel) {
    if (String(sel).includes(">")) return queryChain(this, sel)[0] || null;
    return findOne(this, parseSel(sel).simple);
  };
  el.querySelectorAll = function (sel) {
    if (String(sel).includes(">")) return queryChain(this, sel);
    return findAll(this, parseSel(sel).simple);
  };
  el.matches = function (sel) { return selMatch(this, parseSel(sel).simple); };
  return el;
}

function parseSel(sel) {
  let s = String(sel || "").trim();
  let scopeChild = false;
  if (s.startsWith(":scope >")) { scopeChild = true; s = s.slice(":scope >".length).trim(); }
  else if (s.startsWith(":scope>")) { scopeChild = true; s = s.slice(":scope>".length).trim(); }
  return { scopeChild, simple: s };
}
// Compound single-selector: optional tag + .class + [attr="v"] + :not(.class) (AND).
function selMatch(el, sel) {
  if (!el || !el.classList) return false;
  const s = String(sel);
  const tokens = s.match(/^[a-z][\w-]*|\.[\w-]+|\[[\w-]+(?:="[^"]*")?\]|:not\([^)]*\)/gi) || [];
  if (!tokens.length) return false;
  for (const t of tokens) {
    if (t.startsWith(":not(")) {
      const inner = t.slice(5, -1).trim();
      if (selMatch(el, inner)) return false;
    } else if (t.startsWith(".")) {
      if (!el.classList.contains(t.slice(1))) return false;
    } else if (t.startsWith("[")) {
      const m = t.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);
      if (!m) return false;
      const attr = m[1];
      const want = m[2];
      let val;
      if (attr.startsWith("data-")) {
        const camel = attr.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
        val = el.dataset ? el.dataset[camel] : undefined;
      } else {
        val = el.attrs ? el.attrs[attr] : undefined;
      }
      if (want === undefined) { if (val == null) return false; }
      else if (String(val) !== want) return false;
    } else {
      // tag name
      if (String(el.tagName).toLowerCase() !== t.toLowerCase()) return false;
    }
  }
  return true;
}
// Child-combinator chain: "[:scope] > A > B" (also plain "A > B" descendant-then-child).
function queryChain(root, sel) {
  const segs = String(sel).split(">").map((s) => s.trim()).filter(Boolean);
  if (!segs.length) return [];
  let scoped = false;
  if (segs[0] === ":scope") { scoped = true; segs.shift(); }
  if (!segs.length) return [];
  let current = scoped
    ? (root.children || []).filter((c) => selMatch(c, segs[0]))
    : findAll(root, segs[0]);
  for (let k = 1; k < segs.length; k += 1) {
    const next = [];
    for (const el of current) for (const c of el.children || []) if (selMatch(c, segs[k])) next.push(c);
    current = next;
  }
  return current;
}
function walk(el, fn) { for (const c of el.children || []) { fn(c); walk(c, fn); } }
function findOne(root, sel) { let out = null; walk(root, (n) => { if (!out && selMatch(n, sel)) out = n; }); return out; }
function findAll(root, sel) { const out = []; walk(root, (n) => { if (selMatch(n, sel)) out.push(n); }); return out; }

// ── result collector ────────────────────────────────────────────────────────
const results = [];
function check(id, fn) {
  try { fn(); results.push({ id, ok: true }); }
  catch (e) { results.push({ id, ok: false, err: e && e.message ? e.message : String(e) }); }
}

// ── load markdown module (its own ctx: needs AkanaCore.escapeHtml) ────────────
function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
const mdCtx = { window: { AkanaCore: { escapeHtml }, AkanaI18n: makeI18nStub() }, console };
vm.runInNewContext(readFileSync(MD_PATH, "utf8"), mdCtx);
const MD = mdCtx.window.AkanaMarkdown;
assert.ok(MD && typeof MD.render === "function", "AkanaMarkdown failed to load");

// ── load render module ───────────────────────────────────────────────────────
const noopTimers = { setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {} };
const rafQueue = [];
const rCtx = {
  window: {
    AkanaCore: { escapeHtml: (s) => s },
    AkanaMarkdown: {},
    AkanaI18n: makeI18nStub(),
    CSS: { escape: (s) => s },
    requestAnimationFrame: (cb) => { rafQueue.push(cb); return rafQueue.length; },
    ...noopTimers,
  },
  CSS: { escape: (s) => s },
  Element,
  requestAnimationFrame: (cb) => { rafQueue.push(cb); return rafQueue.length; },
  document: { createElement: (t) => makeEl(t), createElementNS: (_n, t) => makeEl(t), getElementById: () => null },
  console,
  setTimeout,
  clearTimeout,
};
vm.runInNewContext(renderSrc, rCtx);
const Render = rCtx.window.AkanaChatRender;
assert.ok(Render, "AkanaChatRender failed to load");

// ── Finding 1: fences are verbatim through preprocess/render ──────────────────
check("fe-chat-render-1", () => {
  // (a) tab inside a fence is NOT converted to 2 spaces (Makefile recipe)
  const make = MD.preprocess("```make\nall:\n\trecipe\n```");
  assert.ok(make.includes("\trecipe"), "tab inside fence must survive (got: " + JSON.stringify(make) + ")");
  // (b) a '# comment' line inside a fence is NOT split/blank-lined
  const bash = MD.preprocess("```bash\nls -la  # list files\n```");
  assert.ok(bash.includes("ls -la  # list files"), "inline '# comment' inside fence must stay on one line");
  assert.ok(!/ls -la\s*\n\s*\n\s*# list files/.test(bash), "no blank line injected inside fence");
  // (c) yaml list items inside a fence keep no injected blank lines
  const yaml = MD.preprocess("```yaml\nitems:\n- a\n- b\n```");
  assert.ok(!/-\sa\n\n-\sb/.test(yaml), "no blank line between fenced yaml list items");
  // (d) python comment indentation preserved, no dedent/blank-line
  const py = MD.preprocess("```python\ndef f():\n    # compute\n    return 1\n```");
  assert.ok(py.includes("    # compute"), "python comment indent inside fence preserved");
  // (e) '1)' step inside a fence is NOT rewritten to '1.'
  const steps = MD.preprocess("```text\n1) first\n2) second\n```");
  assert.ok(steps.includes("1) first"), "fenced '1)' step must not become '1.'");
  // Rendered code block emits the fenced content verbatim (copy button reads this).
  const html = MD.render("```bash\nls -la  # list files\n```");
  assert.ok(html.includes("ls -la  # list files"), "rendered <code> must contain the verbatim command");
  // Regression: transforms OUTSIDE fences still work.
  const outside = MD.preprocess("1) first\n2) second");
  assert.ok(outside.includes("1. first"), "'1)'→'1.' still applies outside fences");
});

// ── Finding 2: a lone mid-sentence '#' is NOT promoted to a heading ───────────
check("fe-chat-render-2", () => {
  for (const s of ["See item # 3 for details", "issue # 5 is open", "PR # 12 merged", "column # 2"]) {
    const html = MD.render(s);
    assert.ok(!/<h[1-6]/.test(html), `"${s}" must not produce a heading (got ${html})`);
  }
  // Genuine headings still render.
  assert.match(MD.render("## Section"), /<h2 class="md-h md-h2">/);
  assert.match(MD.render("# Title"), /<h1 class="md-h md-h1">/);
});

// ── Finding 3: dotted tool name routes to memory, not grep ────────────────────
check("fe-chat-render-3", () => {
  const card = Render.renderToolCall({ name: "memory.search", args: { query: "favorite color" } });
  assert.equal(card.dataset.toolFamily, "mem", "memory.search must resolve to the 'mem' family, not 'search'");
  const card2 = Render.renderToolCall({ name: "memory.remember", args: { text: "x" } });
  assert.equal(card2.dataset.toolFamily, "mem", "memory.remember must resolve to 'mem'");
  // A genuine grep-ish regex name still resolves to search.
  const g = Render.renderToolCall({ name: "foo.*bar", args: {} });
  assert.equal(g.dataset.toolFamily, "search", "a real regex-y name stays in 'search'");
});

// ── Finding 5: shell-as-toolname term card shows the command ──────────────────
check("fe-chat-render-5", () => {
  const card = Render.renderToolCall({ name: "find / -name foo 2>/dev/null" });
  assert.equal(card.dataset.toolFamily, "shell", "name-is-the-command → shell family");
  const cmdEl = findOne(card, ".term-card-cmd");
  assert.ok(cmdEl, "term card command element exists");
  assert.equal(cmdEl.textContent, "find / -name foo 2>/dev/null", "term card header shows the command, not empty");
});

// ── Finding 6a: a fresh already-done term card shows NO fabricated elapsed ─────
check("fe-chat-render-6a", () => {
  const card = Render.renderToolCall({ name: "ls -la /tmp", phase: "end", result: { exit_code: 0, stdout: "ok" } });
  assert.equal(card.dataset.toolFamily, "shell", "done shell call → shell family (term card)");
  const el = findOne(card, ".term-card-elapsed");
  assert.ok(el, "elapsed element exists");
  assert.equal(el.textContent, "", "a done card with no persisted start must not show '0ms'");
  assert.ok(!/\dms/.test(el.textContent), "no fabricated ms value");
});

// ── Finding 6b: a history-restored (born-done) subagent group shows no 0 ms ───
check("fe-chat-render-6b", () => {
  const tl = makeEl("div");
  const g = Render.upsertSubagentGroup(tl, { id: "t1", name: "Explore", phase: "end", status: "ok" });
  assert.ok(g, "group created");
  const summary = findOne(g, ".aur-subagent-summary");
  assert.ok(summary, "summary element exists");
  assert.ok(!/\bms\b/.test(summary.textContent), `no fabricated 'ms' in restored summary (got "${summary.textContent}")`);
});

// ── Finding 7: a top-level TodoWrite does NOT hijack a subagent's nested list ──
check("fe-chat-render-7", () => {
  const tl = makeEl("div");
  // Subagent runs its own TodoWrite, then ends (group auto-collapses).
  Render.upsertSubagentGroup(tl, { id: "task1", name: "Sub", phase: "start" });
  const subTodos = [{ content: "sub-step-A", status: "pending" }, { content: "sub-step-B", status: "pending" }];
  Render.upsertToolCardIntoTimeline(tl, { name: "TodoWrite", id: "s1", args: { todos: subTodos }, parent_id: "task1" });
  Render.upsertSubagentGroup(tl, { id: "task1", phase: "end", status: "ok" });
  const body = findOne(tl, ".aur-subagent-body");
  const nested = findOne(body, '[data-todo-card="1"]');
  assert.ok(nested, "subagent nested todo card exists");
  assert.equal(findAll(nested, ".ac-todo-text")[0].textContent, "sub-step-A", "nested list starts as sub items");

  // Main agent (no parent_id) issues its first TodoWrite.
  const mainTodos = [{ content: "MAIN-step-1", status: "in_progress" }];
  const mainCard = Render.upsertToolCardIntoTimeline(tl, { name: "TodoWrite", id: "m1", args: { todos: mainTodos } });
  assert.ok(mainCard, "main todo card returned");
  // The nested subagent card must be UNCHANGED (not hijacked).
  assert.equal(findAll(nested, ".ac-todo-text")[0].textContent, "sub-step-A", "subagent list must not be overwritten");
  // A distinct top-level card exists as a direct child of the timeline body.
  assert.notEqual(mainCard, nested, "main TodoWrite must not patch the nested card");
  const topLevel = tl.children.filter((c) => c.dataset && c.dataset.todoCard === "1");
  assert.equal(topLevel.length, 1, "exactly one top-level todo card exists");
  assert.equal(findAll(topLevel[0], ".ac-todo-text")[0].textContent, "MAIN-step-1", "top-level card shows the main items");
});

// ── Finding 4: streaming decorate-guard reads the flag off the PANE ───────────
check("fe-chat-render-4", () => {
  // Build #log container with a per-conversation pane carrying the streaming flag.
  const container = makeEl("div");
  container.setAttribute("id", "log");
  const pane = makeEl("div");
  pane.dataset.chatStreaming = "1";
  container.appendChild(pane);

  // A MutationObserver stub that captures the callback so we can fire it manually.
  let observerCb = null;
  class MutationObserver {
    constructor(cb) { observerCb = cb; }
    observe() {}
    disconnect() {}
  }
  rCtx.window.MutationObserver = MutationObserver;
  rCtx.MutationObserver = MutationObserver;
  rCtx.document.getElementById = (idv) => (idv === "log" ? container : null);
  rafQueue.length = 0;

  const hooks = { log: pane, ttsPlayer: null };
  const renderer = Render.createRenderer(hooks);
  assert.ok(renderer, "createRenderer returned a renderer");
  assert.ok(observerCb, "enhanceChatLog wired a MutationObserver");

  // A completed (non-partial) code block completes mid-stream and is added to the pane.
  const pre = makeEl("pre");
  pre.className = "md-code";
  pre.dataset.lang = "js";
  const code = makeEl("code");
  code.textContent = "const x = 1;";
  pre.appendChild(code);
  pane.appendChild(pre);

  // Fire the observer with the added node, then flush the scheduled rAF.
  observerCb([{ addedNodes: [pre] }]);
  while (rafQueue.length) rafQueue.shift()();

  // Because the PANE is still streaming, the block must NOT be decorated (no shell).
  assert.ok(!pre.closest(".md-code-shell"), "streaming pane's completed block must NOT be decorated (flicker guard)");

  // Now clear the streaming flag and fire again: decoration should happen.
  pane.dataset.chatStreaming = "0";
  observerCb([{ addedNodes: [pre] }]);
  while (rafQueue.length) rafQueue.shift()();
  assert.ok(pre.closest(".md-code-shell"), "once streaming ends the block IS decorated");
});

// ── Finding 1 (review): the streaming guard is PER-PANE ───────────────────────
// A NON-streaming pane still decorates while a DIFFERENT pane streams. This per-pane
// isolation is exactly what makes clearing a background-finished pane's
// data-chat-streaming flag UNCONDITIONALLY safe (transport-side fix): one conversation's
// flag never masks decoration in another, so an over-eager clear cannot leak across panes.
check("fe-chat-render-1-review", () => {
  const container = makeEl("div");
  container.setAttribute("id", "log");
  const paneStreaming = makeEl("div"); // B: still streaming
  paneStreaming.dataset.chatStreaming = "1";
  const paneIdle = makeEl("div"); // A: its background stream finished (flag cleared)
  container.appendChild(paneStreaming);
  container.appendChild(paneIdle);

  let observerCb = null;
  class MutationObserver {
    constructor(cb) { observerCb = cb; }
    observe() {}
    disconnect() {}
  }
  rCtx.window.MutationObserver = MutationObserver;
  rCtx.MutationObserver = MutationObserver;
  rCtx.document.getElementById = (idv) => (idv === "log" ? container : null);
  rafQueue.length = 0;

  const renderer = Render.createRenderer({ log: paneIdle, ttsPlayer: null });
  assert.ok(renderer && observerCb, "renderer + observer wired");

  // A completed code block lands in the IDLE pane (A) while pane B keeps streaming.
  const pre = makeEl("pre");
  pre.className = "md-code";
  pre.dataset.lang = "js";
  const code = makeEl("code");
  code.textContent = "const y = 2;";
  pre.appendChild(code);
  paneIdle.appendChild(pre);

  observerCb([{ addedNodes: [pre] }]);
  while (rafQueue.length) rafQueue.shift()();
  assert.ok(pre.closest(".md-code-shell"),
    "a NON-streaming pane must decorate even while another pane streams (per-pane guard)");
});

// ── summary ───────────────────────────────────────────────────────────────────
const failed = results.filter((r) => !r.ok);
for (const r of results) {
  if (r.ok) console.log(`  PASS ${r.id}`);
  else console.log(`  FAIL ${r.id}: ${r.err}`);
}
if (failed.length) {
  console.log(`blitz3_fe-chat-render.harness: ${failed.length}/${results.length} FAILED`);
  process.exit(1);
}
console.log(`blitz3_fe-chat-render.harness: OK (${results.length} findings)`);
process.exit(0);
