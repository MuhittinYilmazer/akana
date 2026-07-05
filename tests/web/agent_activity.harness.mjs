/**
 * Agent activity (Batch 1) contract test — backend-free, node-vm. Covers the
 * subagent (Task) timeline group + the TODO progress wiring:
 *  1. renderSubagentGroup: .aur-subagent-group root + data-subagent-id +
 *     data-status="running" + head (icon/title/status) + body; title uses i18n
 *     "Subagent · {name}"; a description line renders (and is skipped when it
 *     merely repeats the label / is absent).
 *  2. upsertSubagentGroup: start creates ONE group; a repeat start (same id) does
 *     NOT duplicate; end flips data-status to done / error + the status icon.
 *  3. Nesting: a tool card whose parent_id matches an open group lands INSIDE
 *     .aur-subagent-body (not the top-level timeline); its `end` patches the SAME
 *     nested node.
 *  4. Graceful degrade: parent_id with NO matching group → the card stays top-level.
 *  5. Export surface (renderSubagentGroup, upsertSubagentGroup) + CSS classes.
 *  6. Transport source contract: todo/subagent dispatch branches + handlers +
 *     the TODO progress pill math (done/total, tasks_n, data-complete).
 *  7. i18n: msg.subagent_title / msg.subagent_fallback / transport.process.tasks_n.
 *  8. Batch 2 — streaming tool input: upsertToolInputStream creates a running card
 *     (by id) with the partial input streamed into the subtitle (data-streaming);
 *     repeat deltas update the same card; the real tool_call patches it and clears
 *     the streaming flag (live-preview → final-args transition, no dup); no-id
 *     delta is a no-op; export + CSS + transport dispatch/handler contract.
 * Run: node tests/web/agent_activity.harness.mjs
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

// ── Minimal DOM stub — attribute ([data-x="y"]) + :scope> selector support ────
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

// ── load the render module ────────────────────────────────────────────────────
// Elapsed-time tickers call `window.setInterval` (subagent group + term card). In
// this backend-free node-vm harness there is no browser `window`, and node's vm does
// not surface a `setInterval` global, so the shim provides no-op timer stubs — the
// tickers only repaint an elapsed label, which these contract tests do not assert on.
const noopTimers = {
  setInterval: () => 0,
  clearInterval: () => {},
  setTimeout: () => 0,
  clearTimeout: () => {},
};
const ctx = {
  window: { AkanaCore: { escapeHtml: (s) => s }, AkanaMarkdown: {}, AkanaI18n: makeI18nStub(), CSS: { escape: (s) => s }, ...noopTimers },
  // The render module uses the bare browser global `CSS.escape` (not window.CSS) in
  // the id-selector paths this harness exercises (subagent nesting) → define it.
  CSS: { escape: (s) => s },
  document: { createElement: (t) => makeEl(t), createElementNS: (_n, t) => makeEl(t) },
  console,
  setTimeout,
  clearTimeout,
};
vm.runInNewContext(renderSrc, ctx);
const Render = ctx.window.AkanaChatRender;
assert.ok(Render, "AkanaChatRender failed to load");
for (const fn of ["renderSubagentGroup", "upsertSubagentGroup", "upsertToolCardIntoTimeline"]) {
  assert.equal(typeof Render[fn], "function", `${fn} should be exported`);
}

// ── 1. renderSubagentGroup structure ─────────────────────────────────────────
{
  const g = Render.renderSubagentGroup({
    id: "task1", name: "Explore", description: "Search the codebase", phase: "start",
  });
  assert.ok(g, "group should be created");
  assert.ok(g.classList.contains("aur-subagent-group"), ".aur-subagent-group class");
  assert.ok(g.classList.contains("aur-timeline-tool"), "timeline class");
  assert.equal(g.dataset.subagentId, "task1", "data-subagent-id");
  assert.equal(g.dataset.status, "running", "initial status running");
  assert.ok(findOne(g, ".aur-subagent-ic"), ".aur-subagent-ic should exist");
  assert.ok(findOne(g, ".aur-subagent-body"), ".aur-subagent-body should exist");
  const title = findOne(g, ".aur-subagent-title");
  assert.equal(title.textContent, "Subagent · Explore", "title i18n (name interpolation)");
  const desc = findOne(g, ".aur-subagent-desc");
  assert.ok(desc && desc.textContent === "Search the codebase", "description line");
}
// Fallback when there is no name; when description = label there is NO description line.
{
  const g = Render.renderSubagentGroup({ id: "t2", phase: "start" });
  assert.equal(findOne(g, ".aur-subagent-title").textContent, "Subagent", "fallback label when no name");
  assert.equal(findOne(g, ".aur-subagent-desc"), null, "no desc line when no description");
  const g2 = Render.renderSubagentGroup({ id: "t3", name: "Explore", description: "Explore", phase: "start" });
  assert.equal(findOne(g2, ".aur-subagent-desc"), null, "no desc line when description=label");
}

// ── 2. upsertSubagentGroup: create single + idempotent + end phase ────────────
{
  const tl = makeEl("div");
  const g1 = Render.upsertSubagentGroup(tl, { id: "task1", name: "Explore", phase: "start" });
  assert.equal(findAll(tl, ".aur-subagent-group").length, 1, "start → 1 group");
  // A repeat start (same id) does NOT duplicate.
  const g1b = Render.upsertSubagentGroup(tl, { id: "task1", name: "Explore", phase: "start" });
  assert.equal(g1b, g1, "same id → same group node");
  assert.equal(findAll(tl, ".aur-subagent-group").length, 1, "still 1 group");
  // end → done.
  Render.upsertSubagentGroup(tl, { id: "task1", phase: "end", status: "ok" });
  assert.equal(g1.dataset.status, "done", "end/ok → done");
  // end/error → error.
  const g2 = Render.upsertSubagentGroup(tl, { id: "task2", name: "Task", phase: "start" });
  Render.upsertSubagentGroup(tl, { id: "task2", phase: "end", status: "error" });
  assert.equal(g2.dataset.status, "error", "end/error → error");
  // null when there is no id (defensive).
  assert.equal(Render.upsertSubagentGroup(tl, { phase: "start" }), null, "no id → null");
}

// ── 3. Nesting: a card whose parent_id matches lands INSIDE the group BODY ─────
{
  const tl = makeEl("div");
  Render.upsertSubagentGroup(tl, { id: "task1", name: "Explore", phase: "start" });
  const group = findOne(tl, ".aur-subagent-group");
  const body = findOne(group, ".aur-subagent-body");
  // The subagent's Read call (parent_id=task1).
  const card = Render.upsertToolCardIntoTimeline(tl, {
    id: "c1", name: "Read", phase: "start", args: { file_path: "/x" }, parent_id: "task1",
  });
  assert.ok(card, "card should be created");
  // The card must be in the group's body, NOT at the timeline root.
  assert.equal(findAll(body, ".tool-call").length, 1, "card placed into group body");
  assert.equal(body.children.includes(card) || findAll(body, ".tool-call").includes(card), true, "card is a body child");
  // No direct tool-call at the root level (excluding the group).
  const topLevelTools = (tl.children || []).filter((c) => c.classList?.contains("tool-call"));
  assert.equal(topLevelTools.length, 0, "no direct tool-call at root timeline (card nested)");
  // end patches the same nested node (does not create a new card).
  const card2 = Render.upsertToolCardIntoTimeline(tl, {
    id: "c1", name: "Read", phase: "end", result: "ok", status: "ok", parent_id: "task1",
  });
  assert.equal(card2, card, "end patches the same nested card");
  assert.equal(findAll(body, ".tool-call").length, 1, "still 1 nested card");
}

// ── 4. Race-safe placeholder: a child arriving BEFORE its parent's start event ─
// creates a PLACEHOLDER group and nests inside it (live streaming can deliver a
// child's tool_call before the parent Task/subagent start lands). A later matching
// start MERGES the placeholder in place — same node, no dup, the child is not lost.
{
  const tl = makeEl("div");
  const card = Render.upsertToolCardIntoTimeline(tl, {
    id: "c9", name: "Read", phase: "start", args: { file_path: "/y" }, parent_id: "orphan",
  });
  assert.ok(card, "card should be created (does not crash when the parent is unseen)");
  const groups = findAll(tl, ".aur-subagent-group");
  assert.equal(groups.length, 1, "an unseen parent creates a placeholder group (race-safe)");
  assert.equal(groups[0].dataset.placeholder, "1", "group is flagged as a placeholder");
  assert.equal(groups[0].dataset.subagentId, "orphan", "placeholder keyed by the parent id");
  const body = findOne(groups[0], ".aur-subagent-body");
  assert.ok(findAll(body, ".tool-call").includes(card), "child nests in the placeholder body");
  assert.equal((tl.children || []).includes(card), false, "child is NOT left at the timeline root");
  // The parent's real start now lands → merge in place (same node, placeholder cleared,
  // the already-nested child is preserved).
  const merged = Render.upsertSubagentGroup(tl, { id: "orphan", name: "Explore", phase: "start" });
  assert.equal(merged, groups[0], "start merges the SAME placeholder node (no new group)");
  assert.equal(findAll(tl, ".aur-subagent-group").length, 1, "still a single group after merge");
  assert.ok(!merged.dataset.placeholder, "placeholder flag cleared once the real start lands");
  assert.equal(findAll(findOne(merged, ".aur-subagent-body"), ".tool-call").length, 1, "the nested child survives the merge");
}

// ── 5. Export + CSS ───────────────────────────────────────────────────────────
for (const exp of ["renderSubagentGroup,", "upsertSubagentGroup,"]) {
  assert.ok(renderSrc.includes(exp), `${exp} missing from render export list`);
}
for (const cls of [".aur-subagent-group", ".aur-subagent-body", ".aur-subagent-head", ".aur-process-todo"]) {
  assert.ok(css.includes(cls), `CSS ${cls} missing`);
}
// upsertToolCardIntoTimeline does the nesting via subagentBodyFor. (Bound is loose:
// the Task-call subagent-boundary short-circuit + its doc comment precede the call.)
assert.ok(
  /function upsertToolCardIntoTimeline[\s\S]{0,900}subagentBodyFor\(/.test(renderSrc),
  "upsertToolCardIntoTimeline subagent nesting routing missing",
);

// ── 6. Transport source contract: dispatch branches + handlers + pill math ────
assert.ok(/f\.event === "todo"/.test(transportSrc), "transport todo dispatch branch missing");
assert.ok(/f\.event === "subagent"/.test(transportSrc), "transport subagent dispatch branch missing");
assert.ok(transportSrc.includes("function handleTodoEvent"), "handleTodoEvent missing");
assert.ok(transportSrc.includes("function handleSubagentEvent"), "handleSubagentEvent missing");
assert.ok(transportSrc.includes("upsertSubagentGroup"), "transport should call upsertSubagentGroup");
// Pill: done/total count + tasks_n + data-complete.
assert.ok(/transport\.process\.tasks_n/.test(transportSrc), "tasks_n i18n usage missing");
assert.ok(/status === "completed"/.test(transportSrc), "pill completed count missing");
assert.ok(/aur-process-todo/.test(transportSrc), "pill .aur-process-todo element missing");

// ── 7. i18n keys present (EN + TR) ────────────────────────────────────────────
const { DICT } = makeI18nStub();
for (const key of ["msg.subagent_title", "msg.subagent_fallback", "transport.process.tasks_n"]) {
  assert.ok(DICT[key] && DICT[key].en && DICT[key].tr, `i18n ${key} EN+TR missing`);
}

// ── 8. Batch 2 — live tool input (tool_call_delta → subtitle stream) ──────────
assert.equal(typeof Render.upsertToolInputStream, "function", "upsertToolInputStream should be exported");
{
  const tl = makeEl("div");
  // First delta → running card (by id) + streamed input in the subtitle + streaming flag.
  const n1 = Render.upsertToolInputStream(tl, { id: "b1", name: "Bash", text: '{"command":"ls' });
  assert.ok(n1, "delta card should be created");
  assert.equal(n1.dataset.streaming, "1", "streaming flag set");
  assert.equal(n1.dataset.toolCallId, "b1", "card is keyed by tool id");
  assert.equal(n1.dataset.status, "running", "running status while streaming");
  const sub1 = findOne(n1, ".action-card-subtitle");
  assert.ok(sub1 && sub1.textContent.includes("command"), "streamed input preview in the subtitle");
  // Second delta (same id) → the SAME card, preview updates, does NOT duplicate.
  const n2 = Render.upsertToolInputStream(tl, { id: "b1", name: "Bash", text: '{"command":"ls -la"}' });
  assert.equal(n2, n1, "same id → same card");
  assert.equal(findAll(tl, ".tool-call").length, 1, "single card (deltas do not stack)");
  assert.ok(findOne(n1, ".action-card-subtitle").textContent.includes("-la"), "preview updated");
  // The real tool_call (start, with args) → patches the SAME card + clears streaming.
  const patched = Render.upsertToolCardIntoTimeline(tl, {
    id: "b1", name: "Bash", phase: "start", args: { command: "ls -la" },
  });
  assert.equal(patched, n1, "real tool_call patches the same card (no dup)");
  assert.ok(!patched.dataset.streaming, "streaming flag drops when real args arrive");
  assert.equal(findAll(tl, ".tool-call").length, 1, "still a single card");
}
// no-op when there is no id (correlation impossible → does not throw, and creates no card).
{
  const tl = makeEl("div");
  assert.equal(Render.upsertToolInputStream(tl, { name: "X", text: "y" }), null, "no id → null");
  assert.equal(findAll(tl, ".tool-call").length, 0, "no card created when no id");
}
// Export + CSS + transport source contract.
assert.ok(renderSrc.includes("upsertToolInputStream,"), "upsertToolInputStream missing from render export list");
assert.ok(css.includes('[data-streaming="1"]'), "CSS streaming style missing");
assert.ok(/f\.event === "tool_call_delta"/.test(transportSrc), "transport tool_call_delta dispatch branch missing");
assert.ok(transportSrc.includes("function handleToolCallDeltaEvent"), "handleToolCallDeltaEvent missing");
assert.ok(transportSrc.includes("upsertToolInputStream"), "transport should call upsertToolInputStream");
assert.ok(/toolInputStream/.test(transportSrc), "transport per-id accumulator missing");
// patchToolCallCard clears streaming when the real args arrive.
assert.ok(
  /delete node\.dataset\.streaming/.test(renderSrc),
  "patchToolCallCard streaming cleanup missing",
);

console.log("agent_activity.harness: OK");
process.exit(0);
