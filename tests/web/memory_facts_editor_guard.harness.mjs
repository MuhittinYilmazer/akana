/**
 * Memory Studio — unsaved fact-editor guard (regression, backend-free, node-vm + fake-DOM).
 *
 * Bug: loadFacts() runs on every search keystroke (debounced), filter, paging, and any
 * background approve/reject/create/delete. It immediately does setListState(list,"loading")
 * + innerHTML="" which wipes the WHOLE facts list — including an OPEN .memory-fact-editor
 * with unsaved textarea content — silently, losing the user's in-progress edit.
 *
 * This drives the REAL render module (setListState wipe + buildFactEditor) and the REAL
 * studio loadFacts/openFactEditor/hasDirtyFactEditor via the _test seam. A background
 * (unforced) reload while a dirty editor is open must NOT destroy it; a forced reload
 * (explicit save/refresh) still replaces the list.
 *
 * Run: node tests/web/memory_facts_editor_guard.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { makeI18nStub } from "./_i18n_stub.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const read = (rel) => readFileSync(path.join(REPO, "web_ui/static", rel), "utf8");

// ───────────────────────── Fake DOM (facts-flow surfaces only) ─────────────
function makeEl(tag = "div") {
  const el = {
    tagName: String(tag).toUpperCase(),
    children: [],
    dataset: {},
    attrs: {},
    _listeners: {},
    value: "",
    checked: false,
    hidden: false,
    disabled: false,
    _text: "",
    _html: "",
    classList: {
      _s: new Set(),
      add(...c) { c.forEach((x) => this._s.add(x)); },
      remove(...c) { c.forEach((x) => this._s.delete(x)); },
      contains(c) { return this._s.has(c); },
    },
    style: {},
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); this.children = []; },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); if (v === "") this.children = []; },
    get className() { return [...this.classList._s].join(" "); },
    set className(v) { this.classList._s = new Set(String(v).split(/\s+/).filter(Boolean)); },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k] ?? null; },
    removeAttribute(k) { delete this.attrs[k]; },
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    append(...cs) { cs.forEach((c) => (c && typeof c === "object" ? this.appendChild(c) : null)); },
    removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); return c; },
    remove() { const p = this.parentNode; if (p) p.removeChild(this); },
    addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); },
    focus() {},
    querySelector(sel) { return findAll(this, sel)[0] || null; },
    querySelectorAll(sel) { return findAll(this, sel); },
  };
  return el;
}

/** Match one compound simple selector: "tag", ".class", "tag.class", ".a.b". */
function matchesSimple(el, sel) {
  // Split "li.memory-fact-card" → ["li", ".memory-fact-card"]; ".x.y" → ["", ".x", ".y"].
  const tokens = sel.split(/(?=\.)/).filter((s) => s !== "");
  for (const tok of tokens) {
    if (tok.startsWith(".")) { if (!el.classList.contains(tok.slice(1))) return false; }
    else if (el.tagName !== tok.toUpperCase()) return false;
  }
  return true;
}
function descendants(el, out = []) {
  for (const c of el.children || []) { if (!c || !c.tagName) continue; out.push(c); descendants(c, out); }
  return out;
}
function findAll(root, sel) {
  const parts = sel.trim().split(/\s+/);
  let scope = [root];
  for (const part of parts) {
    const next = [];
    for (const node of scope) for (const d of descendants(node)) if (matchesSimple(d, part)) next.push(d);
    scope = next;
  }
  return scope;
}

const { t } = makeI18nStub("en");

// A minimal document; getElementById reads from a registry the test controls.
const registry = new Map();
const document = {
  createElement: (tag) => makeEl(tag),
  createElementNS: (_ns, tag) => makeEl(tag),
  createTextNode: (text) => ({ nodeType: 3, textContent: String(text) }),
  getElementById: (id) => registry.get(id) || null,
  body: { classList: { contains: () => false } },
  querySelectorAll: () => [],
};

const win = {
  AkanaI18n: { t },
  AkanaCore: { showToast: () => {}, escapeHtml: (s) => String(s ?? "") },
  sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  requestAnimationFrame: () => 0,
  setTimeout: (fn) => fn && 0,
  clearTimeout: () => {},
  confirm: () => true,
};
win.window = win;

const ctx = { window: win, document, sessionStorage: win.sessionStorage, console };
vm.createContext(ctx);
vm.runInContext(read("akana-memory-render.js"), ctx);
vm.runInContext(read("akana-memory-studio.js"), ctx);

const render = win.AkanaMemoryRender;
const studio = win.AkanaMemoryStudio;
assert.ok(render && studio && studio._test, "modules + _test seam must load");
const { loadFacts, openFactEditor, hasDirtyFactEditor } = studio._test;

// Register the DOM elements the facts flow reads.
const list = makeEl("ul");
registry.set("memory-facts-list", list);
for (const id of [
  "memory-facts-count", "memory-facts-pager", "memory-facts-page-status",
  "memory-facts-prev", "memory-facts-next", "memory-facts-q", "memory-facts-history",
]) registry.set(id, makeEl(id.includes("-q") || id.includes("history") ? "input" : "div"));

// Stub the memory API: two facts, no search query.
let listCalls = 0;
const FACTS = [
  { id: "f1", key: "coffee", value: "likes espresso", ts_last: 0 },
  { id: "f2", key: "city", value: "Osmaniye", ts_last: 0 },
];
win.AkanaMemoryApi = {
  listFacts: async () => { listCalls += 1; return { items: FACTS, total: FACTS.length }; },
};

let passed = 0;
async function check(label, fn) { await fn(); passed += 1; void label; }

await check("initial load renders fact cards", async () => {
  await loadFacts();
  const cards = list.querySelectorAll(".memory-fact-card");
  assert.equal(cards.length, 2, "two fact cards rendered");
});

await check("background reload wipes list when NO editor is open", async () => {
  const before = listCalls;
  await loadFacts(); // unforced, nothing dirty → proceeds
  assert.ok(listCalls > before, "unforced reload with no editor must proceed (list rebuilt)");
});

await check("dirty open editor SURVIVES a background (unforced) reload", async () => {
  const firstCard = list.querySelectorAll(".memory-fact-card")[0];
  openFactEditor(firstCard, FACTS[0]);
  const ta = firstCard.querySelector(".memory-fact-editor textarea");
  assert.ok(ta, "editor textarea must exist after openFactEditor");
  ta.value = "a long unsaved rewrite the user is in the middle of typing";
  assert.equal(hasDirtyFactEditor(), true, "editor with changed textarea is dirty");

  const before = listCalls;
  await loadFacts(); // background/automatic reload (debounced search, bg approve, paging)
  // The bug: setListState('loading')+innerHTML='' wiped the editor. The fix bails instead.
  assert.equal(listCalls, before, "dirty editor → unforced reload must NOT hit the network");
  const stillThere = list.querySelectorAll(".memory-fact-editor textarea");
  assert.equal(stillThere.length, 1, "the open editor must NOT be destroyed");
  assert.equal(stillThere[0].value, "a long unsaved rewrite the user is in the middle of typing",
    "the unsaved textarea content must survive");
});

await check("a PRISTINE open editor does not block reload", async () => {
  // Reset: rebuild the list clean (force past the dirty one from the previous case).
  await loadFacts({ force: true });
  const firstCard = list.querySelectorAll(".memory-fact-card")[0];
  openFactEditor(firstCard, FACTS[0]); // opened but untouched → not dirty
  assert.equal(hasDirtyFactEditor(), false, "untouched editor is not dirty");
  const before = listCalls;
  await loadFacts(); // unforced; not dirty → should proceed
  assert.ok(listCalls > before, "pristine editor must not block an automatic reload");
});

await check("forced reload replaces the list even with a dirty editor", async () => {
  const firstCard = list.querySelectorAll(".memory-fact-card")[0];
  openFactEditor(firstCard, FACTS[0]);
  const ta = firstCard.querySelector(".memory-fact-editor textarea");
  ta.value = "dirty again";
  assert.equal(hasDirtyFactEditor(), true);
  await loadFacts({ force: true }); // explicit save/refresh path
  assert.equal(list.querySelectorAll(".memory-fact-editor textarea").length, 0,
    "a forced reload (explicit user action) still replaces the list");
});

console.log(`memory_facts_editor_guard.harness: ok (${passed} checks)`);
