/**
 * Plan card (claude plan-mode / ExitPlanMode frontend) contract test —
 * backend-free, with node-vm. Covers (akana-chat-render.js::renderPlanCard):
 *  1. Structure: .aur-plan root + data-plan-id + .aur-plan-body (plan markdown) +
 *     foot (Revise + Apply) + hidden free-text (.aur-plan-revise).
 *  2. "Apply" → onApprove() is called; the card moves to data-state="applied" (lock).
 *  3. "Revise" → the FIRST click opens the free text (NO onRevise); text + 2nd
 *     click (or Enter) → onRevise(text), card data-state="revised".
 *  4. Empty revise text → onRevise is not called (no send).
 *  5. Once decided, clicking again is a no-op (locked).
 *  6. Invalid/empty plan → null (the card is never built).
 *  7. Export surface + CSS classes + transport SSE branch/stream source contract +
 *     per-turn plan_mode payload source contract (the composer Plan toggle was
 *     removed — ExitPlanMode is interactive-only in headless `claude -p`).
 * Run: node tests/web/plan_card.harness.mjs
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
const CHAT_PATH = path.join(REPO, "web_ui/static/akana-chat.js");
const CSS_PATH = path.join(REPO, "web_ui/static/aurora-chat.css");
const STYLES_PATH = path.join(REPO, "web_ui/static/styles.css");
const HTML_PATH = path.join(REPO, "web_ui/index.html");

const renderSrc = readFileSync(RENDER_PATH, "utf8");
const transportSrc = readFileSync(TRANSPORT_PATH, "utf8");
const chatSrc = readFileSync(CHAT_PATH, "utf8");
const css = readFileSync(CSS_PATH, "utf8");
const stylesCss = readFileSync(STYLES_PATH, "utf8");
const html = readFileSync(HTML_PATH, "utf8");

// ── Minimal DOM stub — with EVENT DISPATCH support (click/keydown are simulated) ────
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
    set textContent(v) { this._text = String(v); },
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
    querySelector(sel) { return findOne(this, sel); },
    querySelectorAll(sel) { return findAll(this, sel); },
    matches() { return false; },
    closest() { return null; },
  };
  return el;
}

function selMatch(el, sel) {
  if (!el || !el.classList) return false;
  if (sel.startsWith(".")) return el.classList.contains(sel.slice(1));
  return false;
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
  // NO AkanaMarkdown.setBubbleMarkdown → renderPlanCard falls back to plain text
  // (body.textContent = plan markdown) — the body is directly verifiable.
  window: { AkanaCore: { escapeHtml: (s) => s }, AkanaMarkdown: {}, AkanaI18n: makeI18nStub(), CSS: { escape: (s) => s } },
  document: { createElement: (t) => makeEl(t), createElementNS: (_n, t) => makeEl(t) },
  console,
  setTimeout,
  clearTimeout,
};
vm.runInNewContext(renderSrc, ctx);
const Render = ctx.window.AkanaChatRender;
assert.ok(Render, "AkanaChatRender failed to load");
assert.equal(typeof Render.renderPlanCard, "function", "renderPlanCard should be exported");

const PLAN_MD = "## Plan\n1. Şunu yap\n2. Bunu yap";
const PLAN = { id: "tu-plan", plan: PLAN_MD, plan_file: "/home/u/.claude/plans/x.md" };

// ── 1. Structure: root, plan-id, body markdown, foot buttons ───────────────────
{
  const card = Render.renderPlanCard({ plan: PLAN, onApprove: () => {}, onRevise: () => {} });
  assert.ok(card, "card should be built");
  assert.ok(card.classList.contains("aur-plan"), ".aur-plan class");
  assert.equal(card.dataset.planId, "tu-plan", "data-plan-id should be carried");
  assert.equal(card.dataset.state, "pending", "initial state=pending");
  const body = findOne(card, ".aur-plan-body");
  assert.ok(body, ".aur-plan-body should exist");
  assert.equal(body.textContent, PLAN_MD, "body should carry the plan markdown (fallback)");
  assert.ok(findOne(card, ".aur-plan-apply"), "Apply button");
  assert.ok(findOne(card, ".aur-plan-revise-btn"), "Revise button");
  // Free text hidden at first.
  const reviseWrap = findOne(card, ".aur-plan-revise");
  assert.ok(reviseWrap, ".aur-plan-revise wrap should exist");
  assert.equal(reviseWrap.hidden, true, "free text hidden at first");
}

// ── 2. "Apply" → onApprove + state=applied (lock) ──────────────────────────
{
  let approved = 0;
  let revised = null;
  const card = Render.renderPlanCard({
    plan: PLAN,
    onApprove: () => (approved += 1),
    onRevise: (t) => (revised = t),
  });
  const apply = findOne(card, ".aur-plan-apply");
  apply.click();
  assert.equal(approved, 1, "onApprove should be called once");
  assert.equal(revised, null, "Apply should not trigger onRevise");
  assert.equal(card.dataset.state, "applied", "state=applied after Apply");
  assert.equal(apply.disabled, true, "button locked after Apply");
  // Clicking again is a no-op.
  apply.click();
  assert.equal(approved, 1, "Apply is a no-op on an applied card");
}

// ── 3. "Revise" two-stage: FIRST click opens, text + 2nd click → onRevise ──────────
{
  let approved = 0;
  let revised = null;
  const card = Render.renderPlanCard({
    plan: PLAN,
    onApprove: () => (approved += 1),
    onRevise: (t) => (revised = t),
  });
  const reviseBtn = findOne(card, ".aur-plan-revise-btn");
  const reviseWrap = findOne(card, ".aur-plan-revise");
  const reviseIn = findOne(card, ".aur-plan-revise-in");
  // FIRST click: opens the input, NO onRevise.
  reviseBtn.click();
  assert.equal(reviseWrap.hidden, false, "the first Revise click opens the free text");
  assert.equal(revised, null, "onRevise is not triggered on the first click");
  // Enter text + 2nd click → onRevise(text), state=revised.
  reviseIn.value = "Adım 2 yerine X yap";
  reviseBtn.click();
  assert.equal(revised, "Adım 2 yerine X yap", "onRevise carries the free text");
  assert.equal(card.dataset.state, "revised", "state=revised after Revise");
  assert.equal(approved, 0, "Revise should not trigger onApprove");
  // Clicking again is a no-op.
  revised = null;
  reviseBtn.click();
  assert.equal(revised, null, "Revise is a no-op on a revised card");
}

// ── 3b. The Enter key also sends the revision ─────────────────────────────────────
{
  let revised = null;
  const card = Render.renderPlanCard({ plan: PLAN, onApprove: () => {}, onRevise: (t) => (revised = t) });
  const reviseBtn = findOne(card, ".aur-plan-revise-btn");
  const reviseIn = findOne(card, ".aur-plan-revise-in");
  reviseBtn.click(); // open
  reviseIn.value = "kısalt";
  reviseIn.dispatch("keydown", { key: "Enter" });
  assert.equal(revised, "kısalt", "Enter sends the revision");
  assert.equal(card.dataset.state, "revised", "state=revised after Enter");
}

// ── 4. Empty revise text → onRevise is not called ────────────────────────────────
{
  let revised = 0;
  const card = Render.renderPlanCard({ plan: PLAN, onApprove: () => {}, onRevise: () => (revised += 1) });
  const reviseBtn = findOne(card, ".aur-plan-revise-btn");
  reviseBtn.click(); // open (text empty)
  reviseBtn.click(); // attempt to send with empty text
  assert.equal(revised, 0, "onRevise is not called on empty text");
  assert.equal(card.dataset.state, "pending", "card stays pending on an empty revision");
}

// ── 5. Invalid/empty plan → null ───────────────────────────────────────────────
{
  assert.equal(Render.renderPlanCard({ plan: null }), null, "null plan → null");
  assert.equal(Render.renderPlanCard({ plan: {} }), null, "no plan text → null");
  assert.equal(Render.renderPlanCard({ plan: { plan: "   " } }), null, "whitespace plan → null");
  assert.equal(Render.renderPlanCard({}), null, "no plan field → null");
}

// ── 6. Export + CSS + transport/chat source contract ──────────────────────────
assert.ok(renderSrc.includes("renderPlanCard,"), "renderPlanCard missing from the render export list");
for (const cls of [".aur-plan", ".aur-plan-body", ".aur-plan-apply", ".aur-plan-revise-btn"]) {
  assert.ok(css.includes(cls), `CSS ${cls} missing`);
}
assert.ok(
  css.includes('.aur-plan[data-state="applied"]') && css.includes('[data-state="revised"]'),
  "CSS applied/revised state rule missing",
);
// Transport: plan_review SSE branch + done exemption + card + plan→resume send.
assert.ok(transportSrc.includes('f.event === "plan_review"'), "transport plan_review SSE branch missing");
assert.ok(transportSrc.includes("maybeRenderPlanCard"), "transport maybeRenderPlanCard missing");
assert.ok(transportSrc.includes("submitPlanReply"), "transport plan→resume send missing");
assert.ok(
  transportSrc.includes("payload.plan_review"),
  "transport does not handle done.plan_review",
);
assert.ok(
  transportSrc.includes("!streamCtx.planShown && !payload.plan_review"),
  "transport empty-response error is not exempt on a plan turn",
);
// chat: a plan card's Apply/Revise still passes a per-turn plan_mode override, and
// the transport still carries the plan_mode payload field (backend-driven plan cards
// stay). The composer Plan TOGGLE was removed — ExitPlanMode is interactive-only in
// headless `claude -p`, so plan mode can't run; normal turns never request it.
assert.ok(chatSrc.includes("submitPlanText"), "chat submitPlanText public API missing");
assert.ok(chatSrc.includes("planMode:"), "chat buildChatCtx planMode hook missing");
assert.ok(transportSrc.includes("payload.plan_mode = true"), "transport plan_mode payload field missing");
// The composer Plan toggle button was REMOVED → it must NOT reappear in HTML/styles/chat.
assert.ok(!html.includes('id="btn-plan"'), "index.html #btn-plan toggle should be removed (must not reappear)");
assert.ok(!stylesCss.includes(".btn-plan"), "styles .btn-plan should be removed");
assert.ok(!chatSrc.includes("PLAN_MODE_KEY"), "chat plan toggle (PLAN_MODE_KEY) should be removed");

console.log("plan_card.harness: OK");
process.exit(0);
