/**
 * AskUserQuestion card (Part A frontend) contract test — backend-free, with node-vm.
 * Covers (akana-chat-render.js::renderAskUserCard):
 *  1. Structure: .aur-ask root + data-ask-id + per-question .aur-ask-q text +
 *     .aur-ask-opt buttons (+ description) + free text + Submit.
 *  2. Single-select (multiSelect=false): selecting one option closes siblings (radio).
 *  3. Multi-select (multiSelect=true): multiple options stay open at once.
 *  4. Submit is DISABLED when there's no answer; active once a selection/free text arrives.
 *  5. Submit → onSubmit(answerText): for multiple questions "header: labels" lines,
 *     joined by newline; the card moves to data-state="answered" (foot hidden).
 *  6. Free text joins the answer (in single-select free text is authoritative).
 *  7. Invalid/empty input → null (the card is never built).
 *  8. Export surface + CSS classes + transport branch source contract.
 * Run: node tests/web/ask_user_card.harness.mjs
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

// ── Minimal DOM stub — with EVENT DISPATCH support (click/input are simulated) ─────
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
    value: "",
    type: "",
    placeholder: "",
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
class FakeMutationObserver {
  observe() {}
  disconnect() {}
}
const ctx = {
  window: {
    AkanaCore: { escapeHtml: (s) => s },
    AkanaMarkdown: { setBubbleMarkdown: (b, t) => { if (b) b._md = String(t); } },
    AkanaI18n: makeI18nStub(),
    CSS: { escape: (s) => s },
    // AkanaChat.submitAnswerText — observed by the persisted-card render test below.
    AkanaChat: { submitAnswerText: null },
    setInterval: () => 0,
  },
  document: {
    createElement: (t) => makeEl(t),
    createElementNS: (_n, t) => makeEl(t),
    // enhanceChatLog looks up #log; returning null → it falls back to hooks.log.
    getElementById: () => null,
  },
  console,
  setTimeout,
  clearTimeout,
  requestAnimationFrame: () => 0,
  cancelAnimationFrame: () => {},
  MutationObserver: FakeMutationObserver,
  Element: makeEl().constructor, // plain-object els; `instanceof Element` is just falsy (fine)
};
ctx.window.setInterval = () => 0;
vm.runInNewContext(renderSrc, ctx);
const Render = ctx.window.AkanaChatRender;
assert.ok(Render, "AkanaChatRender failed to load");
assert.equal(typeof Render.renderAskUserCard, "function", "renderAskUserCard should be exported");

const TWO_Q = {
  id: "tu-ask",
  questions: [
    {
      question: "Çay mı kahve mi?",
      header: "İçecek",
      multiSelect: false,
      options: [
        { label: "Çay", description: "demli" },
        { label: "Kahve", description: "filtre" },
      ],
    },
    {
      question: "Hangi boyları istersin?",
      header: "Boy",
      multiSelect: true,
      options: [{ label: "Küçük" }, { label: "Orta" }, { label: "Büyük" }],
    },
  ],
};

// ── 1. Structure: root, ask-id, question texts, options, description ────────────────
{
  let submitted = null;
  const card = Render.renderAskUserCard({ question: TWO_Q, onSubmit: (a) => (submitted = a) });
  assert.ok(card, "card should be built");
  assert.ok(card.classList.contains("aur-ask"), ".aur-ask class");
  assert.equal(card.dataset.askId, "tu-ask", "data-ask-id should be carried");
  assert.equal(card.dataset.state, "pending", "initial state=pending");
  const qBlocks = findAll(card, ".aur-ask-q");
  assert.equal(qBlocks.length, 2, "two question blocks");
  const qTexts = findAll(card, ".aur-ask-q-text").map((n) => n.textContent);
  assert.deepEqual(qTexts, ["Çay mı kahve mi?", "Hangi boyları istersin?"], "question texts");
  const opts = findAll(card, ".aur-ask-opt");
  assert.equal(opts.length, 5, "2+3 = 5 option buttons");
  assert.equal(findOne(card, ".aur-ask-opt-d").textContent, "demli", "first option description");
  // Submit is DISABLED at first (no answer).
  const submit = findOne(card, ".aur-ask-submit");
  assert.ok(submit, "Submit button");
  assert.equal(submit.disabled, true, "Submit disabled when there is no answer");
}

// ── 2. Single-select radio behavior + 3. multi-select toggle + 4/5. submit format ──────
{
  let submitted = null;
  const card = Render.renderAskUserCard({ question: TWO_Q, onSubmit: (a) => (submitted = a) });
  const opts = findAll(card, ".aur-ask-opt");
  const submit = findOne(card, ".aur-ask-submit");
  // q0 single-select: Çay(0), Kahve(1) — q1 multi-select: Küçük(2), Orta(3), Büyük(4).
  opts[0].click(); // Çay
  assert.ok(opts[0].classList.contains("is-on"), "Çay selected");
  assert.equal(submit.disabled, false, "one selection → Submit active");
  opts[1].click(); // Kahve → in single-select Çay must close
  assert.ok(opts[1].classList.contains("is-on"), "Kahve selected");
  assert.ok(!opts[0].classList.contains("is-on"), "single-select: Çay closed (radio)");
  // q1 multi-select: Küçük + Büyük stay open.
  opts[2].click();
  opts[4].click();
  assert.ok(opts[2].classList.contains("is-on") && opts[4].classList.contains("is-on"),
    "multi-select: two options open together");
  assert.ok(!opts[3].classList.contains("is-on"), "Orta not selected");
  // Submit → onSubmit format: for multiple questions "header: labels", joined by newline.
  submit.click();
  assert.ok(submitted, "onSubmit should be called");
  const lines = submitted.split("\n");
  assert.equal(lines.length, 2, "two questions → two lines");
  assert.equal(lines[0], "İçecek: Kahve", "single-select line 'header: label'");
  assert.equal(lines[1], "Boy: Küçük, Büyük", "multi-select line 'header: l1, l2'");
  assert.equal(card.dataset.state, "answered", "state=answered after submit");
  // Submitting again is a no-op (locked).
  submitted = null;
  submit.click();
  assert.equal(submitted, null, "submit is a no-op on an answered card");
}

// ── 6. Free text joins the answer (single question, single-select → free authoritative) ─────
{
  const ONE_Q = {
    id: "tu-1",
    questions: [
      { question: "Renk?", header: "Renk", multiSelect: false, options: [{ label: "Kırmızı" }] },
    ],
  };
  let submitted = null;
  const card = Render.renderAskUserCard({ question: ONE_Q, onSubmit: (a) => (submitted = a) });
  const free = findOne(card, ".aur-ask-free");
  const submit = findOne(card, ".aur-ask-submit");
  assert.ok(free, "free text input");
  free.value = "Mor";
  free.dispatch("input");
  assert.equal(submit.disabled, false, "free text → Submit active");
  submit.click();
  // Single question → NO header prefix (one line), free text authoritative.
  assert.equal(submitted, "Mor", "single question answer is a bare label; free text is authoritative");
}

// ── 7. Invalid input → null ──────────────────────────────────────────────────
{
  assert.equal(Render.renderAskUserCard({ question: null }), null, "null question → null");
  assert.equal(Render.renderAskUserCard({ question: { questions: [] } }), null, "empty question → null");
  assert.equal(
    Render.renderAskUserCard({ question: { questions: [{ question: "q" }] } }),
    null,
    "question without options → null",
  );
}

// ── 8. CSS + transport branch source contract ────────────────────────────────────
for (const cls of [".aur-ask", ".aur-ask-opt", ".aur-ask-submit", ".aur-ask-free"]) {
  assert.ok(css.includes(cls), `CSS ${cls} missing`);
}
assert.ok(css.includes('.aur-ask-opt.is-on'), "CSS selected-option rule missing");
assert.ok(css.includes('[data-state="answered"]'), "CSS answered state missing");
// Transport: ask_user SSE branch + done exemption + answer→resume send.
assert.ok(transportSrc.includes('f.event === "ask_user"'), "transport ask_user SSE branch missing");
assert.ok(transportSrc.includes("maybeRenderAskUserCard"), "transport maybeRenderAskUserCard missing");
assert.ok(transportSrc.includes("submitAnswerText"), "transport answer→resume send missing");
assert.ok(
  transportSrc.includes("!streamCtx.askUserShown && !payload.ask_user"),
  "transport empty-response error not exempt on the question turn",
);

// ── 9. Persistence (U3): mapServerMessagesToThread carries the structured ask_user ──
// A question turn stored server-side returns an `ask_user` payload on /messages. The
// interactive card must re-render on a chat switch / reload — not just the summary text.
{
  const askPayload = {
    id: "srv-ask",
    questions: [
      { question: "Çay mı kahve mi?", header: "İçecek", multiSelect: false,
        options: [{ label: "Çay" }, { label: "Kahve" }] },
    ],
  };
  // (a) the ask turn is the LAST message → mapped askUser + pending true.
  {
    const thread = Render.mapServerMessagesToThread([
      { role: "user", content: "sor bana bir şey", created_at: "t0" },
      { role: "assistant", id: "a1", content: "Çay mı kahve mi?", created_at: "t1", ask_user: askPayload },
    ]);
    const last = thread[thread.length - 1];
    assert.equal(last.kind, "assistant", "last mapped message is the assistant ask turn");
    assert.ok(last.askUser && Array.isArray(last.askUser.questions), "ask_user mapped to askUser");
    assert.equal(last.askUserPending, true, "trailing ask turn is pending");
  }
  // (b) an answer (a following user message) → NOT pending → summary text only.
  {
    const thread = Render.mapServerMessagesToThread([
      { role: "assistant", id: "a1", content: "Çay mı kahve mi?", created_at: "t1", ask_user: askPayload },
      { role: "user", content: "Kahve", created_at: "t2" },
    ]);
    const ask = thread.find((m) => m.kind === "assistant");
    assert.ok(ask.askUser, "answered ask turn still carries askUser data");
    assert.ok(!ask.askUserPending, "answered ask turn is NOT pending (no card, summary only)");
  }
  // (c) a turn WITHOUT ask_user → no askUser field (regression guard).
  {
    const thread = Render.mapServerMessagesToThread([
      { role: "assistant", id: "a1", content: "merhaba", created_at: "t1" },
    ]);
    assert.ok(!thread[0].askUser, "a normal assistant turn has no askUser");
  }

  // (d) createRenderer + chatRenderMessage on a PENDING ask message → the interactive
  //     .aur-ask card is rendered into the appended row, and its submit routes to
  //     window.AkanaChat.submitAnswerText (identical wiring to the persisted error card).
  {
    let sentAnswer = null;
    ctx.window.AkanaChat.submitAnswerText = (a) => { sentAnswer = a; };
    const log = makeEl("div");
    const renderer = Render.createRenderer({
      log,
      appendUserMessage: () => makeEl("div"),
      appendSystemNotice: () => {},
    });
    renderer.chatRenderMessage({
      kind: "assistant",
      turnId: "a1",
      text: "Çay mı kahve mi?", // == the ask summary → the bubble is suppressed
      askUser: askPayload,
      askUserPending: true,
    });
    assert.equal(log.children.length, 1, "one assistant row appended");
    const card = findOne(log.children[0], ".aur-ask");
    assert.ok(card, "an interactive .aur-ask card is rendered from the persisted ask turn");
    assert.equal(card.dataset.askId, "srv-ask", "the card carries the persisted ask id");
    // The summary bubble is suppressed (question not printed twice).
    assert.equal(findOne(log.children[0], ".bubble-assistant"), null, "summary bubble suppressed under the card");
    // Submit → window.AkanaChat.submitAnswerText.
    const opt = findAll(card, ".aur-ask-opt")[1]; // Kahve
    opt.click();
    findOne(card, ".aur-ask-submit").click();
    assert.equal(sentAnswer, "Kahve", "card submit routes to AkanaChat.submitAnswerText");
  }
}

console.log("ask_user_card.harness: OK");
process.exit(0);
