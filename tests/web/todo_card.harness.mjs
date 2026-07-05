/**
 * Task list (TodoWrite) LIVE checklist card contract test — backend-free, with
 * node-vm. Covers (akana-chat-render.js):
 *  1. renderTodoCard: .aur-todo-card root (always OPEN; NO collapse) +
 *     data-todo-card="1" + data-tool-family="todo" + head (icon/title/counter) +
 *     ul.ac-todos > li.ac-todo--{status}; counter "completed/total".
 *  2. extractTodoItems: {todos:[…]} and {items:[…]} + content/activeForm/text +
 *     status normalization (in-progress → in_progress).
 *  3. isTodoCall: TodoWrite/todo_write → true; Read → false (routing gate).
 *  4. Family-dedup (upsertTodoCard): consecutive TodoWrites with different IDs
 *     update a SINGLE card in place — no stacking; the latest ID is adopted;
 *     the checklist refreshes.
 *  5. upsertToolCardIntoTimeline: todo family → .aur-todo-card (+aur-timeline-tool),
 *     a second call patches the same card.
 *  6. An empty/end-phase update (no args) does NOT DELETE the existing list (arg cache).
 *  7. Export surface + TODO_TOOL_RE single-source + family regex "todowrite" + CSS +
 *     the todo-family routing source contract of the upsert paths.
 * Run: node tests/web/todo_card.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { makeI18nStub } from "./_i18n_stub.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const RENDER_PATH = path.join(REPO, "web_ui/static/akana-chat-render.js");
const TRANSPORT_PATH = path.join(REPO, "web_ui/static/akana-chat-transport.js");
const CSS_PATH = path.join(REPO, "web_ui/static/aurora-chat.css");

const renderSrc = readFileSync(RENDER_PATH, "utf8");
const transportSrc = readFileSync(TRANSPORT_PATH, "utf8");
const css = readFileSync(CSS_PATH, "utf8");

// ── Minimal DOM stub — with attribute ([data-x="y"]) + :scope> selector support ───────
function makeEl(tag = "div") {
  const el = {
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
        if (want) this._s.add(c);
        else this._s.delete(c);
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
    innerHTML: "",
    get textContent() { return this._text; },
    set textContent(v) {
      this._text = String(v);
      // textContent="" → clear the children (fillTodoChecklist ul.textContent="").
      if (v === "") { this.children = []; this.childNodes = []; }
    },
    get className() { return [...this.classList._s].join(" "); },
    set className(v) { this.classList._s = new Set(String(v).split(/\s+/).filter(Boolean)); },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k] ?? null; },
    removeAttribute(k) { delete this.attrs[k]; },
    appendChild(c) { this.children.push(c); this.childNodes.push(c); c.parentNode = this; return c; },
    append(...cs) { cs.forEach((c) => this.appendChild(c)); },
    insertBefore(node, ref) {
      const i = this.children.indexOf(ref);
      if (i < 0) this.children.push(node);
      else this.children.splice(i, 0, node);
      this.childNodes = this.children;
      node.parentNode = this;
      return node;
    },
    addEventListener(type, fn) {
      (this._listeners[type] = this._listeners[type] || []).push(fn);
    },
    dispatch(type, evt = {}) {
      for (const fn of this._listeners[type] || []) fn({ preventDefault() {}, ...evt });
    },
    click() { this.dispatch("click"); },
    focus() {},
    remove() {
      if (this.parentNode) {
        const i = this.parentNode.children.indexOf(this);
        if (i >= 0) this.parentNode.children.splice(i, 1);
      }
    },
    querySelector(sel) {
      const { scopeChild, simple } = parseSel(sel);
      if (scopeChild) {
        for (const c of this.children || []) if (selMatch(c, simple)) return c;
        return null;
      }
      return findOne(this, simple);
    },
    querySelectorAll(sel) {
      const { scopeChild, simple } = parseSel(sel);
      if (scopeChild) return (this.children || []).filter((c) => selMatch(c, simple));
      return findAll(this, simple);
    },
    matches(sel) { return selMatch(this, parseSel(sel).simple); },
    closest() { return null; },
  };
  return el;
}

function parseSel(sel) {
  let s = String(sel || "").trim();
  let scopeChild = false;
  if (s.startsWith(":scope >")) { scopeChild = true; s = s.slice(":scope >".length).trim(); }
  else if (s.startsWith(":scope>")) { scopeChild = true; s = s.slice(":scope>".length).trim(); }
  return { scopeChild, simple: s };
}
// Single compound selector: .cls ... [data-x="y"] ... (chained AND).
function selMatch(el, sel) {
  if (!el || !el.classList) return false;
  const tokens = String(sel).match(/\.[\w-]+|\[[\w-]+(?:="[^"]*")?\]/g) || [];
  if (!tokens.length) return false;
  for (const t of tokens) {
    if (t.startsWith(".")) {
      if (!el.classList.contains(t.slice(1))) return false;
    } else {
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
    }
  }
  return true;
}
function walk(el, fn) {
  for (const c of el.children || []) { fn(c); walk(c, fn); }
}
function findOne(root, sel) {
  let out = null;
  walk(root, (n) => { if (!out && selMatch(n, sel)) out = n; });
  return out;
}
function findAll(root, sel) {
  const out = [];
  walk(root, (n) => { if (selMatch(n, sel)) out.push(n); });
  return out;
}

// ── load the render module ─────────────────────────────────────────────────────
const ctx = {
  window: { AkanaCore: { escapeHtml: (s) => s }, AkanaMarkdown: {}, AkanaI18n: makeI18nStub(), CSS: { escape: (s) => s } },
  document: { createElement: (t) => makeEl(t), createElementNS: (_n, t) => makeEl(t) },
  console,
  setTimeout,
  clearTimeout,
};
vm.runInNewContext(renderSrc, ctx);
const Render = ctx.window.AkanaChatRender;
assert.ok(Render, "AkanaChatRender failed to load");
for (const fn of ["renderTodoCard", "patchTodoCard", "upsertTodoCard", "isTodoCall", "extractTodoItems", "upsertToolCardIntoTimeline"]) {
  assert.equal(typeof Render[fn], "function", `${fn} should be exported`);
}

const todoCall = (todos, extra = {}) => ({ name: "TodoWrite", args: { todos }, phase: "end", result: "ok", ...extra });
const T1 = [
  { content: "Şemayı oku", status: "completed", activeForm: "Şema okunuyor" },
  { content: "Kartı yaz", status: "in_progress", activeForm: "Kart yazılıyor" },
  { content: "Testleri ekle", status: "pending", activeForm: "Test ekleniyor" },
];

// ── 1. Structure: root + attributes + head + checklist + counter ──────────────
{
  const card = Render.renderTodoCard(todoCall(T1));
  assert.ok(card, "card should be built");
  assert.ok(card.classList.contains("aur-todo-card"), ".aur-todo-card class");
  assert.ok(card.classList.contains("tool-call"), ".tool-call class (included in the process counter)");
  assert.equal(card.dataset.todoCard, "1", "data-todo-card=1 (family-dedup key)");
  assert.equal(card.dataset.toolFamily, "todo", "data-tool-family=todo");
  assert.ok(findOne(card, ".aur-todo-head"), ".aur-todo-head should exist");
  assert.ok(findOne(card, ".aur-todo-ic"), ".aur-todo-ic should exist");
  const title = findOne(card, ".aur-todo-title");
  assert.equal(title.textContent, "Task list", "title text (i18n EN)");
  const lis = findAll(card, ".ac-todo");
  assert.equal(lis.length, 3, "3 task rows");
  assert.ok(findOne(card, ".ac-todo--completed"), "completed row class");
  assert.ok(findOne(card, ".ac-todo--in_progress"), "in_progress row class");
  assert.ok(findOne(card, ".ac-todo--pending"), "pending row class");
  const count = findOne(card, ".aur-todo-count");
  assert.equal(count.textContent, "1/3", "counter completed/total");
  // The first row's text should come from the content field.
  const txt = findOne(card, ".ac-todo-text");
  assert.equal(txt.textContent, "Şemayı oku", "row text from the content field");
}

// ── 2. extractTodoItems: items[] + activeForm fallback + status normalize ─────
{
  const items = Render.extractTodoItems({
    name: "todo_write",
    args: { items: [{ activeForm: "İş A", status: "in-progress" }, { text: "İş B" }] },
  });
  assert.equal(items.length, 2, "items[] field is also accepted");
  assert.equal(items[0].text, "İş A", "activeForm fallback");
  assert.equal(items[0].status, "in_progress", "in-progress → in_progress normalize");
  assert.equal(items[1].status, "pending", "pending when status is missing");
}

// ── 3. isTodoCall routing gate ────────────────────────────────────────────────
{
  assert.equal(Render.isTodoCall({ name: "TodoWrite" }), true, "TodoWrite → todo family");
  assert.equal(Render.isTodoCall({ name: "todo_write" }), true, "todo_write → todo family");
  assert.equal(Render.isTodoCall({ name: "Read", args: { file_path: "/x" } }), false, "Read → not todo");
  assert.equal(Render.isTodoCall({ name: "Bash", args: { command: "ls" } }), false, "Bash → not todo");
}

// ── 4. Family-dedup: consecutive calls with different IDs update a SINGLE card ─
{
  const body = makeEl("div");
  const n1 = Render.upsertTodoCard(body, todoCall(T1, { id: "a1" }));
  assert.equal(findAll(body, ".aur-todo-card").length, 1, "first call → 1 card");
  assert.equal(n1.dataset.toolCallId, "a1", "first ID adopted");
  // Second call: DIFFERENT id + advanced list.
  const T2 = [
    { content: "Şemayı oku", status: "completed" },
    { content: "Kartı yaz", status: "completed" },
    { content: "Testleri ekle", status: "in_progress" },
  ];
  const n2 = Render.upsertTodoCard(body, todoCall(T2, { id: "a2" }));
  assert.equal(findAll(body, ".aur-todo-card").length, 1, "second call does NOT stack — still 1 card");
  assert.equal(n2, n1, "same node updated in place");
  assert.equal(n1.dataset.toolCallId, "a2", "latest ID adopted");
  assert.equal(findOne(body, ".aur-todo-count").textContent, "2/3", "counter refreshed (2/3)");
}

// ── 5. upsertToolCardIntoTimeline live path routes the todo family to the card ─
{
  const timeline = makeEl("div");
  const a = Render.upsertToolCardIntoTimeline(timeline, todoCall(T1, { id: "t1" }));
  assert.ok(a.classList.contains("aur-todo-card"), "timeline todo → .aur-todo-card");
  assert.ok(a.classList.contains("aur-timeline-tool"), "timeline class added");
  assert.equal(findAll(timeline, ".aur-todo-card").length, 1, "1 todo card");
  const b = Render.upsertToolCardIntoTimeline(timeline, todoCall(T1, { id: "t2" }));
  assert.equal(b, a, "second TodoWrite patches the same card");
  assert.equal(findAll(timeline, ".aur-todo-card").length, 1, "still 1 todo card in the timeline");
}

// ── 6. Empty/end-phase update (no args) does NOT DELETE the existing list ──────
{
  const body = makeEl("div");
  Render.upsertTodoCard(body, todoCall(T1, { id: "x1" }));
  // NO args, only the result/end phase (e.g. a tool_result event).
  const node = Render.upsertTodoCard(body, { name: "TodoWrite", id: "x2", phase: "end", result: "ok" });
  assert.equal(findAll(node, ".ac-todo").length, 3, "update without args preserves the list (arg cache)");
  assert.equal(node.dataset.status, "done", "status done");
}

// ── 7. Export + single-source regex + CSS + upsert routing source contract ────
for (const exp of ["renderTodoCard,", "patchTodoCard,", "upsertTodoCard,", "isTodoCall,", "extractTodoItems,"]) {
  assert.ok(renderSrc.includes(exp), `${exp} missing from the render export list`);
}
// SINGLE-source TODO_TOOL_RE + family regex covers Claude's "todowrite" norm.
assert.ok(renderSrc.includes("const TODO_TOOL_RE ="), "TODO_TOOL_RE single-source constant missing");
assert.ok(/TODO_TOOL_RE\s*=\s*\/\^\(todo_write\|todowrite/.test(renderSrc), "TODO_TOOL_RE should cover the 'todowrite' norm");
assert.ok(renderSrc.includes("if (TODO_TOOL_RE.test(norm)) return \"todo\";"), "toolCallFamily todo branch should use TODO_TOOL_RE");
// The upsert paths route the todo family to the checklist card. (Bound is loose:
// Batch 1 subagent nesting added a `subagentBodyFor` target computation, and the
// Task-call subagent-boundary short-circuit + its doc comment now precede isTodoCall.)
assert.ok(
  /function upsertToolCardIntoTimeline[\s\S]{0,1200}isTodoCall\(call\)/.test(renderSrc),
  "upsertToolCardIntoTimeline todo-family routing missing",
);
assert.ok(
  /function upsertToolCallCard[\s\S]{0,160}isTodoCall\(call\)/.test(renderSrc),
  "upsertToolCallCard todo-family routing missing",
);
assert.ok(renderSrc.includes("isTodoCall(c)"), "appendToolCallsGrouped todo-family routing missing");
// The live path uses transport's upsertLiveToolCard/upsertToolCardIntoTimeline.
assert.ok(transportSrc.includes("upsertToolCardIntoTimeline"), "transport live timeline path missing");
// CSS: distinct checklist card.
for (const cls of [".aur-todo-card", ".aur-todo-head", ".aur-todo-count"]) {
  assert.ok(css.includes(cls), `CSS ${cls} missing`);
}

console.log("todo_card.harness: OK");
process.exit(0);
