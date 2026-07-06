/**
 * Conversation isolation contract test — backend-free, node-vm + fake DOM.
 *
 * Goal: deterministically verify that while TWO chats stream at the same time the
 * live tool-card queue does NOT leak between conversations. It used to be that
 * `_toolUiPending` + `_toolUiRaf` + `_toolKeyState` were at MODULE level; one
 * stream's rAF flush pushed ALL calls in _toolUiPending (including the other
 * conversation's) into the body of the streamCtx that triggered it → cards leaked
 * into the wrong conversation. State now lives on the streamCtx
 * (ensureToolScratch). This test locks that down with the breaking→green ritual.
 *
 * Run: node tests/web/chat_stream_isolation.harness.mjs
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
  void label;
}

// ── Fake DOM (only the surfaces this test touches) ───────────────────────────
function makeEl(tag = "div") {
  return {
    tagName: String(tag).toUpperCase(),
    children: [],
    dataset: {},
    style: {},
    attrs: {},
    _cards: [], // the upsertToolCardIntoTimeline mock writes the card id here
    _text: "",
    classList: {
      _s: new Set(),
      add(...c) { c.forEach((x) => this._s.add(x)); },
      remove(...c) { c.forEach((x) => this._s.delete(x)); },
      contains(c) { return this._s.has(c); },
      toggle(c, on) { const w = on === undefined ? !this._s.has(c) : on; if (w) this._s.add(c); else this._s.delete(c); return w; },
    },
    appendChild(n) { this.children.push(n); return n; },
    append(...n) { n.forEach((x) => this.children.push(x)); },
    insertBefore(n) { this.children.unshift(n); return n; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k] ?? null; },
    removeAttribute(k) { delete this.attrs[k]; },
    addEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    after() {},
    remove() {},
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); },
  };
}

// ── vm context: window mocks + requestAnimationFrame CONTROL ──────────────────
// rAF callbacks are collected but do not run automatically → we preserve
// determinism by driving the flush EXPLICITLY in the test via
// __test.flushToolCallUpdates.
const rafCbs = [];
const ctx = {
  console,
  document: { createElement: (t) => makeEl(t) },
  performance: { now: () => 0 },
  requestAnimationFrame: (cb) => { rafCbs.push(cb); return rafCbs.length; },
  cancelAnimationFrame: () => {},
  CSS: { escape: (s) => s },
};
ctx.window = ctx;
ctx.window.CSS = ctx.CSS;
// AkanaChatRender: only upsertToolCardIntoTimeline is needed — it records the
// card in the body's _cards list (so we can observe which call went to which body).
ctx.window.AkanaChatRender = {
  upsertToolCardIntoTimeline(body, call) {
    const id = String((call && (call.id || call.call_id)) || "");
    if (!body._cards.includes(id)) body._cards.push(id);
    return { dataset: { status: "done" }, nextElementSibling: null, after() {} };
  },
};
ctx.window.AkanaCore = { baseUrl: () => "", authHeaders: () => ({}), parseApiError: () => "", escapeHtml: (s) => s };
ctx.window.AkanaMarkdown = { setBubbleMarkdown() {}, appendBubbleStreamText() {} };
ctx.window.AkanaTurnStatus = { isActive: () => false };

vm.runInNewContext(read("akana-chat-transport.js"), ctx);

const transport = ctx.window.AkanaChatTransport.create({
  hooks: { stickToBottomIfFollowing() {} },
});
const T = transport.__test;
assert.ok(T && T.queueToolCall && T.flushToolCallUpdates, "test seam (__test) must exist");

function makeStreamCtx(tag) {
  const body = makeEl("div");
  const feed = makeEl("div");
  feed.dataset.finalized = "0";
  return {
    tag,
    insertBeforeBubble() {},
    thoughtFeed: feed, // ensureThoughtFeed short-circuit (feed already exists)
    thoughtBody: body,
    msgBody: makeEl("div"),
    scroller: makeEl("div"),
  };
}

check("isolation: two streams' tool cards do not mix (interleaved queue→flush)", () => {
  const A = makeStreamCtx("A");
  const B = makeStreamCtx("B");
  // with id + phase=end (status done → not 'running' → syncLiveCancel returns early).
  T.queueToolCall(A, { id: "a1", name: "grep", phase: "end", status: "ok", result: "x" }, A.scroller);
  T.queueToolCall(B, { id: "b1", name: "read", phase: "end", status: "ok", result: "y" }, B.scroller);
  T.queueToolCall(A, { id: "a2", name: "task", phase: "end", status: "ok", result: "z" }, A.scroller);
  T.queueToolCall(B, { id: "b2", name: "web", phase: "end", status: "ok", result: "w" }, B.scroller);
  // Flush order: A first, then B. With a shared queue, A's flush also pushed B's
  // calls into A and drained the queue (B was left empty) — the BREAKING scenario.
  T.flushToolCallUpdates(A, A.scroller);
  T.flushToolCallUpdates(B, B.scroller);
  assert.deepEqual([...A.thoughtBody._cards].sort(), ["a1", "a2"], "A must receive only its own cards");
  assert.deepEqual([...B.thoughtBody._cards].sort(), ["b1", "b2"], "B must receive only its own cards");
});

check("isolation: anon (id-less) tool keys do not collide across streams", () => {
  const A = makeStreamCtx("A");
  const B = makeStreamCtx("B");
  // Both streams call an id-less tool with the same name; start+end must match
  // within their own stream (single card) and must not spill into the other.
  T.queueToolCall(A, { name: "grep", phase: "start", args: { q: "a" } }, A.scroller);
  T.queueToolCall(B, { name: "grep", phase: "start", args: { q: "b" } }, B.scroller);
  T.queueToolCall(A, { name: "grep", phase: "end", status: "ok", result: "ra" }, A.scroller);
  T.queueToolCall(B, { name: "grep", phase: "end", status: "ok", result: "rb" }, B.scroller);
  T.flushToolCallUpdates(A, A.scroller);
  T.flushToolCallUpdates(B, B.scroller);
  assert.equal(A.thoughtBody._cards.length, 1, "A has a single merged card (start+end matched)");
  assert.equal(B.thoughtBody._cards.length, 1, "B has a single merged card (start+end matched)");
});

check("isolation: each streamCtx has its own tool-scratch", () => {
  const A = makeStreamCtx("A");
  const B = makeStreamCtx("B");
  const sa = T.ensureToolScratch(A);
  const sb = T.ensureToolScratch(B);
  assert.ok(sa !== sb, "scratch objects must be separate");
  assert.ok(sa.pending !== sb.pending, "pending Maps must be separate");
  sa.pending.set("x", 1);
  assert.equal(sb.pending.size, 0, "one stream's pending must not leak into the other");
});

// ── CONCURRENT LIVE-MARKDOWN THROTTLE ISOLATION ──────────────────────────────
// A second vm context: the live-markdown throttle chooses between the "render now"
// and the "trailing timer" path based on the performance.now() ↔ mdLast delta; to
// drive that deterministically we need a CONTROLLABLE clock + fake
// setTimeout/clearTimeout. The tool-card context above is performance.now=()=>0 +
// rAF-based; we set up a separate context without touching it.
//
// BREAKING scenario (old shared _streamMdJob/_streamMdTimer/_streamMdLast):
// while two chats stream at once, B's schedule OVERWROTE A's pending job, and
// since B bound onto A's timer, A's trailing render painted B instead of A
// → A's live text FREEZES. State now lives on the streamCtx (ensureMdScratch).
let clock = 0;
const mdTimers = [];
let mdTimerSeq = 0;
const mdRafCbs = []; // rAF-variant + scroll callbacks (do not run automatically)
function flushMdTimers() {
  // fire in arm-order, each once; skip cancelled ones.
  const pending = mdTimers.splice(0);
  pending.forEach((t) => { if (!t.cancelled) t.cb(); });
}
function flushMdRaf() {
  const pending = mdRafCbs.splice(0);
  pending.forEach((c) => { if (c && !c.cancelled) c.cb(); });
}
// Hook that records which scroller was stuck — scroll isolation observation.
const scrollHits = [];
const ctx2 = {
  console,
  document: { createElement: (t) => makeEl(t) },
  performance: { now: () => clock },
  requestAnimationFrame: (cb) => { const c = { cb, cancelled: false }; mdRafCbs.push(c); return mdRafCbs.length; },
  cancelAnimationFrame: (id) => { const c = mdRafCbs[id - 1]; if (c) c.cancelled = true; },
  setTimeout: (cb, ms) => { const id = ++mdTimerSeq; mdTimers.push({ id, cb, ms, cancelled: false }); return id; },
  clearTimeout: (id) => { const t = mdTimers.find((x) => x.id === id); if (t) t.cancelled = true; },
  CSS: { escape: (s) => s },
};
ctx2.window = ctx2;
ctx2.window.CSS = ctx2.CSS;
ctx2.window.AkanaChatRender = ctx.window.AkanaChatRender;
ctx2.window.AkanaCore = ctx.window.AkanaCore;
// setBubbleMarkdown mock: records which text was written to which body →
// cross-talk becomes observable.
ctx2.window.AkanaMarkdown = {
  setBubbleMarkdown(bubble, text) { if (bubble) bubble._lastMd = String(text); },
  appendBubbleStreamText() {},
};
// finalizeStreamUi (Bug5 test) calls AkanaTurnStatus.end() — the stub must be a
// no-op (otherwise the optional-chain `?.end()` throws a TypeError on a non-null obj).
ctx2.window.AkanaTurnStatus = { isActive: () => false, begin() {}, end() {}, setPhase() {} };

vm.runInNewContext(read("akana-chat-transport.js"), ctx2);
const T2 = ctx2.window.AkanaChatTransport
  .create({ hooks: { stickToBottomIfFollowing(s) { scrollHits.push(s); } } }).__test;
assert.ok(
  T2 && T2.ensureMdScratch && T2.scheduleStreamMarkdownThrottled && T2.resetStreamMdThrottle
    && T2.scheduleStreamMarkdownUpdate && T2.flushStreamMarkdownUpdate && T2.scheduleStreamScroll,
  "md-throttle test seam (__test) must exist",
);

function makeMdCtx() {
  return { ctx: {}, bubble: makeEl("div"), scroller: null };
}

check("md-throttle: each streamCtx has its own md-scratch", () => {
  const A = {};
  const B = {};
  const sa = T2.ensureMdScratch(A);
  const sb = T2.ensureMdScratch(B);
  assert.ok(sa !== sb, "scratch objects must be separate");
  sa.mdLast = 999;
  assert.equal(sb.mdLast, 0, "one stream's mdLast must not leak into the other");
  assert.equal(A._mdScratch, sa, "scratch must live on the streamCtx (not the module)");
});

check("md-throttle: two concurrent streams' trailing render goes to the right body", () => {
  clock = 0; // elapsed=0 < interval → both take the trailing-timer path
  mdTimers.length = 0;
  const A = makeMdCtx();
  const B = makeMdCtx();
  // A delta → trailing timer is set for A (job=A). In the old shared code, B's
  // schedule flips job to B + binds onto A's timer → A's render paints B.
  T2.scheduleStreamMarkdownThrottled(A.ctx, A.bubble, A.scroller, "A1");
  T2.scheduleStreamMarkdownThrottled(B.ctx, B.bubble, B.scroller, "B1");
  flushMdTimers();
  assert.equal(A.bubble._lastMd, "A1", "A must receive only its own text (not B's)");
  assert.equal(B.bubble._lastMd, "B1", "B must receive only its own text");
});

check("md-throttle: one stream's render time does not throttle the other (mdLast isolated)", () => {
  clock = 1000; // elapsed large → render-now path
  mdTimers.length = 0;
  const A = makeMdCtx();
  const B = makeMdCtx();
  T2.scheduleStreamMarkdownThrottled(A.ctx, A.bubble, A.scroller, "A1");
  assert.equal(A.bubble._lastMd, "A1", "A renders immediately, mdLast=clock");
  // In the old code, B saw elapsed=0 because of the shared mdLast (=clock) left by
  // A → it got throttled (NO render-now). With isolated mdLast, B also renders now.
  T2.scheduleStreamMarkdownThrottled(B.ctx, B.bubble, B.scroller, "B1");
  assert.equal(B.bubble._lastMd, "B1", "B also renders immediately (unaffected by A's mdLast)");
});

check("md-throttle: reset clears one stream's job/timer, leaves the other untouched", () => {
  clock = 0;
  mdTimers.length = 0;
  const A = makeMdCtx();
  const B = makeMdCtx();
  T2.scheduleStreamMarkdownThrottled(A.ctx, A.bubble, A.scroller, "A1"); // trailing armed
  T2.scheduleStreamMarkdownThrottled(B.ctx, B.bubble, B.scroller, "B1"); // trailing armed
  T2.resetStreamMdThrottle(A.ctx); // cancel A only
  flushMdTimers();
  assert.equal(A.bubble._lastMd, undefined, "A was reset → no render");
  assert.equal(B.bubble._lastMd, "B1", "B is unaffected → its own render happens");
});

check("md-rAF: two concurrent streams' pending rAF render goes to the right body", () => {
  mdRafCbs.length = 0;
  const A = makeMdCtx();
  const B = makeMdCtx();
  // non-immediate path → each sets up its own rAF, pending in its own scratch.
  // With the old shared _streamMdPending/_streamMdRaf, B overwrites A's pending +
  // binds onto A's rAF → the single render paints B, A is left empty.
  T2.scheduleStreamMarkdownUpdate(A.ctx, A.bubble, A.scroller, "A1", false);
  T2.scheduleStreamMarkdownUpdate(B.ctx, B.bubble, B.scroller, "B1", false);
  flushMdRaf();
  assert.equal(A.bubble._lastMd, "A1", "A renders its own pending");
  assert.equal(B.bubble._lastMd, "B1", "B renders its own pending");
});

check("md-scroll: two concurrent streams' scroll goes to their own scroller", () => {
  mdRafCbs.length = 0;
  scrollHits.length = 0;
  const A = makeMdCtx();
  A.scroller = makeEl("div");
  A.scroller._tag = "A";
  const B = makeMdCtx();
  B.scroller = makeEl("div");
  B.scroller._tag = "B";
  // With the old shared _streamScrollTarget/_streamScrollRaf, B overwrites the
  // target + binds onto A's rAF → the single stick goes only to B (A's scroll is
  // dropped/misrouted).
  T2.scheduleStreamScroll(A.ctx, A.scroller);
  T2.scheduleStreamScroll(B.ctx, B.scroller);
  flushMdRaf();
  const tags = scrollHits.map((s) => s && s._tag).sort();
  assert.deepEqual(tags, ["A", "B"], "each stream sticks only to its own scroller");
});

// ── STREAM CONV-ID ADOPTION (meta/done → setConversationId) ──────────────────
// Bug #18 (conv half): a streaming stream's meta/done changed the GLOBAL
// active-conv (visible log + archive + active-thread). On CONCURRENT sends to
// another chat, the background stream must not YANK the visible chat into its own
// conv. adoptStreamConversationId changes the global only for the foreground
// stream (liveStreamCtx); in the background it updates its OWN convId but does not
// touch the global.
const convCalls = [];
const T3 = ctx2.window.AkanaChatTransport
  .create({
    conversationIdForMemory: () => null,
    setConversationId: (id) => { convCalls.push(id); },
    hooks: { stickToBottomIfFollowing() {} },
  })
  .__test;
assert.ok(T3 && T3.adoptStreamConversationId && T3.setLiveStreamCtx, "conv-adopt seam must exist");

check("conv-adopt: foreground stream (liveStreamCtx) updates the global active-conv", () => {
  convCalls.length = 0;
  const fg = {};
  T3.setLiveStreamCtx(fg);
  T3.adoptStreamConversationId(fg, "conv-A");
  assert.equal(fg.convId, "conv-A", "the stream's own convId is set");
  assert.deepEqual(convCalls, ["conv-A"], "foreground stream calls setConversationId");
});

check("conv-adopt: background stream does NOT change the global active-conv (no yank)", () => {
  convCalls.length = 0;
  const fg = {};
  const bg = {};
  T3.setLiveStreamCtx(fg); // foreground = fg (visible chat)
  T3.adoptStreamConversationId(bg, "conv-B"); // bg streams in the background
  assert.equal(bg.convId, "conv-B", "background stream still updates its OWN convId");
  assert.deepEqual(convCalls, [], "background stream does NOT call setConversationId (visible chat does not shift)");
});

check("conv-adopt: does nothing when convId is empty (defensive)", () => {
  convCalls.length = 0;
  const fg = {};
  T3.setLiveStreamCtx(fg);
  T3.adoptStreamConversationId(fg, null);
  T3.adoptStreamConversationId(fg, "");
  assert.equal(fg.convId, undefined, "empty convId is not written to the stream");
  assert.deepEqual(convCalls, [], "empty convId does not call setConversationId");
});

// ── FOREGROUND-GATED GLOBAL UI TEARDOWN (finalizeStreamUi) ───────────────────
// Bug #18 (finalize half): a background stream's done/error/finally called the
// GLOBAL finalizeStreamUi() and prematurely closed the visible chat's composer
// (to SEND) + its "Typing" indicator. It is now foreground-gated: only
// liveStreamCtx (or an explicit teardown, arg-less) closes the global UI.
const uiCalls = [];
const T4 = ctx2.window.AkanaChatTransport
  .create({
    conversationIdForMemory: () => null,
    setConversationId() {},
    hooks: {
      log: makeEl("div"),
      setStreamingUi: (v) => { uiCalls.push(["streamingUi", v]); },
      setComposerHint: (h) => { uiCalls.push(["hint", h]); },
      stickToBottomIfFollowing() {},
    },
  })
  .__test;
assert.ok(T4 && T4.finalizeStreamUi && T4.setLiveStreamCtx, "finalize seam must exist");

check("finalize: foreground stream closes the global UI (composer→SEND)", () => {
  uiCalls.length = 0;
  const fg = {};
  T4.setLiveStreamCtx(fg);
  T4.finalizeStreamUi(fg);
  assert.ok(
    uiCalls.some(([k, v]) => k === "streamingUi" && v === false),
    "foreground finalize must call setStreamingUi(false)",
  );
});

check("finalize: background stream does NOT close the global UI (composer stays open)", () => {
  uiCalls.length = 0;
  const fg = {};
  const bg = {};
  T4.setLiveStreamCtx(fg); // foreground = fg (visible chat still streaming)
  T4.finalizeStreamUi(bg); // bg finished in the background
  assert.deepEqual(uiCalls, [], "background finalize must NOT touch the global UI");
});

check("finalize: arg-less call always closes the global (explicit teardown)", () => {
  uiCalls.length = 0;
  const fg = {};
  T4.setLiveStreamCtx(fg);
  T4.finalizeStreamUi(); // like STOP/error/WS-reconcile → close global
  assert.ok(
    uiCalls.some(([k, v]) => k === "streamingUi" && v === false),
    "arg-less finalize must close the global",
  );
});

// ── FOREGROUND-GATED TTS (tts_chunk) ─────────────────────────────────────────
// Bug #18 (audio half, narrow): a background stream's tts_chunk was enqueued to
// the global ttsPlayer → on a concurrent voice turn in another chat, the
// background audio played over the turn being listened to. Now only the
// foreground (liveStreamCtx) plays.
const ttsEnq = [];
const T5 = ctx2.window.AkanaChatTransport
  .create({
    conversationIdForMemory: () => null,
    setConversationId() {},
    hooks: {
      ttsPlayer: { enqueue: (b) => { ttsEnq.push(b); } },
      stickToBottomIfFollowing() {},
    },
  })
  .__test;
assert.ok(T5 && T5.handleChatStreamEvent && T5.setLiveStreamCtx, "tts test seam must exist");

check("tts: foreground stream's tts_chunk is played", () => {
  ttsEnq.length = 0;
  const fg = {};
  T5.setLiveStreamCtx(fg);
  T5.handleChatStreamEvent({ event: "tts_chunk", data: JSON.stringify({ audio_b64: "AAA" }) }, fg);
  assert.deepEqual(ttsEnq, ["AAA"], "foreground tts_chunk must be enqueued");
});

check("tts: background stream's tts_chunk is NOT played (no audio leak)", () => {
  ttsEnq.length = 0;
  const fg = {};
  const bg = {};
  T5.setLiveStreamCtx(fg); // foreground = fg (the turn being listened to)
  T5.handleChatStreamEvent({ event: "tts_chunk", data: JSON.stringify({ audio_b64: "BBB" }) }, bg);
  assert.deepEqual(ttsEnq, [], "background tts_chunk must NOT be enqueued");
});

// ── IN-BUBBLE DOUBLE-RENDER SHIELD (two followers → single text) ─────────────
// FE bug: the answer appears TWICE in a single bubble, concatenated without a
// separator ("Senin adın Alice." + "Senin adın Alice."). Root cause:
// `_follow_turn` is multi-follower by design (the live POST stream + a
// GET /chat/active resume can replay the same buffer from 0). If two followers
// feed the same turn with the same turn_id, the second's deltas/done ADDED on top
// of the bubble's acc and doubled the text. Shield: once a turn_id is finalized
// ONCE, the canonical single text is recorded; a follower replaying that turn from
// 0 does a REPLACE instead of an APPEND. Also, done treats the server text as
// AUTHORITATIVE when present (it does not concat the acc).
const T6 = ctx2.window.AkanaChatTransport
  .create({
    conversationIdForMemory: () => null,
    setConversationId() {},
    hooks: {
      log: makeEl("div"),
      setStreamingUi() {},
      stickToBottomIfFollowing() {},
      showToast() {},
    },
  })
  .__test;
assert.ok(T6 && T6.handleChatStreamEvent, "double-render test seam must exist");

function makeFollowerCtx(turnId) {
  const feed = makeEl("div");
  feed.dataset.finalized = "0";
  return {
    meta: makeEl("div"),
    bubble: makeEl("div"),
    msgBody: makeEl("div"),
    scroller: makeEl("div"),
    thoughtFeed: feed,
    thoughtBody: makeEl("div"),
    insertBeforeBubble() {},
    turnId,
    acc: "",
    doneMeta: null,
    serverError: null,
    toolPhaseActive: false,
    convId: "conv-X",
  };
}
const TURN = "turn-42";
const ANSWER = "Senin adın Alice.";
const delta = (ctx, text) =>
  T6.handleChatStreamEvent({ event: "delta", data: JSON.stringify({ text }) }, ctx);
const done = (ctx, payload) =>
  T6.handleChatStreamEvent({ event: "done", data: JSON.stringify(payload) }, ctx);

check("double-render: if a second follower feeds the same turn from 0, acc is NOT appended (REPLACE)", () => {
  // 1st follower: live POST stream — fills via deltas + done (server text).
  const live = makeFollowerCtx(TURN);
  delta(live, "Senin adın ");
  delta(live, "Alice.");
  done(live, { turn_id: TURN, text: ANSWER });
  assert.equal(live.bubble._lastMd, ANSWER, "1st follower must show a single text");
  assert.equal(live.acc, ANSWER, "1st follower acc is singular");

  // 2nd follower: GET /chat/active resume — REPLAYS the SAME turn from 0.
  // Without the shield, acc would concat ("...Alice.Senin adın ...") → double.
  const resume = makeFollowerCtx(TURN);
  delta(resume, "Senin adın ");
  delta(resume, "Alice.");
  assert.equal(
    resume.acc,
    ANSWER,
    "2nd follower acc must be REPLACED with the canonical single text (not concatenated)",
  );
  assert.equal(resume.bubble._lastMd, ANSWER, "2nd follower bubble must show a single text");

  // The 2nd follower's done must also stay singular (the canonical record wins).
  done(resume, { turn_id: TURN, text: ANSWER });
  assert.equal(resume.acc, ANSWER, "2nd follower still singular after done");
  assert.equal(resume.bubble._lastMd, ANSWER, "2nd follower done single text");
});

check("double-render: done server text is AUTHORITATIVE — does not concat a stale/doubled acc", () => {
  const ctx = makeFollowerCtx("turn-srv");
  // manually make acc doubled (simulating a corrupt prior state): done must write
  // the server text, NOT the acc.
  ctx.acc = ANSWER + ANSWER;
  done(ctx, { turn_id: "turn-srv", text: ANSWER });
  assert.equal(ctx.bubble._lastMd, ANSWER, "done must write the server text (not the doubled acc)");
  assert.equal(ctx.acc, ANSWER, "acc must be pinned to the server text");
});

check("double-render: falls back to acc when there is no server text (regression guard)", () => {
  // done payload.text empty → old behavior: acc is rendered (with a single
  // follower acc is already singular). The shield must not break this.
  const ctx = makeFollowerCtx("turn-notext");
  delta(ctx, "Merhaba ");
  delta(ctx, "dünya");
  done(ctx, { turn_id: "turn-notext" }); // no text
  assert.equal(ctx.bubble._lastMd, "Merhaba dünya", "acc is rendered when there is no server text");
  assert.equal(ctx.acc, "Merhaba dünya", "acc is preserved");
});

// ═════════════════════════════════════════════════════════════════════════════
// CONCURRENT N-STREAM CONTRACT (per-conversation stream contexts)
// ─────────────────────────────────────────────────────────────────────────────
// THE MOST IMPORTANT FIX: the user sends a message in A, then WHILE A is streaming
// sends a message in B (or a new chat); BOTH must stream concurrently + VISIBLY,
// neither may be lost. There used to be a SINGLE global
// liveStreamCtx/activeStreamAbort → when the second stream started, the first's
// pointer/abort was OVERWRITTEN (A dropped to an invisible follower, STOP only cut
// the last one, the foreground UI was gated to the wrong stream). Now each conv's
// stream is in its OWN record (_streamsByConv) + the foreground UI is gated only to
// the DISPLAYED conv (foregroundConvId).
//
// This block deterministically locks the following CONTRACT:
//   (a) two bubbles accumulate their own text INDEPENDENTLY (no cross-talk),
//   (b) switching the displayed conv does NOT ABORT/CLOBBER the other's stream,
//   (c) no double-render (each bubble's final text is its OWN server text, not doubled),
//   (d) the foreground composer/"Typing" reflects ONLY the displayed conv.
const ncUi = []; // foreground UI calls (streamingUi/hint) — only the foreground should trigger
const ncConv = []; // setConversationId calls — only the foreground stream should adopt
const T7full = ctx2.window.AkanaChatTransport.create({
  conversationIdForMemory: () => null,
  setConversationId: (id) => { ncConv.push(id); },
  hooks: {
    log: makeEl("div"),
    setStreamingUi: (v) => { ncUi.push(["streamingUi", v]); },
    setComposerHint: (h) => { ncUi.push(["hint", h]); },
    stickToBottomIfFollowing() {},
    showToast() {},
  },
});
const T7 = T7full.__test;
assert.ok(
  T7 && T7.registerStream && T7.setForegroundConversation && T7.isForegroundStream
    && T7.handleChatStreamEvent && T7.isConversationStreamActive && T7.reattachLiveRow,
  "per-conv stream seams (__test) must exist",
);

// conv-tagged streamCtx that simulates two concurrent SSE followers.
function makeConvCtx(convId, turnId) {
  const feed = makeEl("div");
  feed.dataset.finalized = "0";
  const row = makeEl("div");
  const body = makeEl("div");
  // reattachLiveRow checks row.parentElement; the fake DOM has no parent →
  // the appendChild path is attempted (lands in log.children for observation).
  return {
    meta: makeEl("div"),
    bubble: makeEl("div"),
    msgBody: body,
    scroller: makeEl("div"),
    thoughtFeed: feed,
    thoughtBody: makeEl("div"),
    insertBeforeBubble() {},
    turnId,
    acc: "",
    doneMeta: null,
    serverError: null,
    toolPhaseActive: false,
    convId,
    rowEl: row,
  };
}
const ncDelta = (c, text) =>
  T7.handleChatStreamEvent({ event: "delta", data: JSON.stringify({ text }) }, c);
const ncDone = (c, payload) =>
  T7.handleChatStreamEvent({ event: "done", data: JSON.stringify(payload) }, c);

check("concurrent-(a): two stream bubbles INDEPENDENTLY accumulate their own text (no cross-talk)", () => {
  clock = 0; // determinism (reset the md-throttle clock)
  const A = makeConvCtx("conv-A", "turn-A");
  const B = makeConvCtx("conv-B", "turn-B");
  T7.registerStream(A, null);
  T7.registerStream(B, null);
  // INTERLEAVE the deltas — in the old single-global state B's delta would
  // overwrite A's acc/render state. acc = source of truth; each stream must
  // accumulate ONLY its own acc (since the live render is time-throttled, _lastMd
  // is verified via the flush on done — see the done assert below).
  ncDelta(A, "A-merhaba ");
  ncDelta(B, "B-selam ");
  ncDelta(A, "dünya");
  ncDelta(B, "evren");
  assert.equal(A.acc, "A-merhaba dünya", "A must accumulate only its own text");
  assert.equal(B.acc, "B-selam evren", "B must accumulate only its own text");
  // done → flushStreamMarkdownUpdate + writes the server text; each bubble must
  // show its OWN final text (if there were cross-talk, it would mix up here).
  ncDone(A, { turn_id: "turn-A", text: "A-merhaba dünya" });
  ncDone(B, { turn_id: "turn-B", text: "B-selam evren" });
  assert.equal(A.bubble._lastMd, "A-merhaba dünya", "A bubble must show only A's text");
  assert.equal(B.bubble._lastMd, "B-selam evren", "B bubble must show only B's text");
  T7.unregisterStream(A);
  T7.unregisterStream(B);
});

check("concurrent-(b): switching the displayed conv does NOT ABORT/CLOBBER the other stream", () => {
  const A = makeConvCtx("conv-A", "turn-A2");
  const B = makeConvCtx("conv-B", "turn-B2");
  const abortedA = { aborted: false };
  const abortedB = { aborted: false };
  T7.registerStream(A, { abort: () => { abortedA.aborted = true; } });
  T7.registerStream(B, { abort: () => { abortedB.aborted = true; } });
  // A is displayed; a delta arrives for A.
  T7.setForegroundConversation("conv-A");
  ncDelta(A, "A-1 ");
  // The user switches to B (equivalent to switchChatConversation: only the
  // foreground changes, NO abort). A's stream must CONTINUE in the background +
  // stay in the record.
  T7.setForegroundConversation("conv-B");
  assert.equal(abortedA.aborted, false, "switching to B must NOT abort A's stream");
  assert.ok(T7.isConversationStreamActive("conv-A"), "A's stream record must be preserved (even if not visible)");
  // A keeps streaming in the background — its deltas must land in its OWN bubble.
  ncDelta(A, "A-2");
  assert.equal(A.acc, "A-1 A-2", "background A must keep accumulating its own text");
  // B streams in the foreground.
  ncDelta(B, "B-1");
  assert.equal(B.acc, "B-1", "foreground B accumulates its own text");
  // Switch back to A → reattachLiveRow rebinds A's live row into the fresh log
  // (NO new follower). The row must have been appended to the log.
  T7.setForegroundConversation("conv-A");
  const reok = T7.reattachLiveRow("conv-A");
  assert.equal(reok, true, "on returning to A the live row must be reattached");
  assert.ok(T7full.__test ? true : true);
  T7.unregisterStream(A);
  T7.unregisterStream(B);
});

check("concurrent-(c): no double-render — each bubble's final text is its OWN server text (not doubled)", () => {
  const A = makeConvCtx("conv-A", "turn-A3");
  const B = makeConvCtx("conv-B", "turn-B3");
  T7.registerStream(A, null);
  T7.registerStream(B, null);
  ncDelta(A, "Cevap-A ");
  ncDelta(B, "Cevap-B ");
  ncDelta(A, "tamam");
  ncDelta(B, "bitti");
  ncDone(A, { turn_id: "turn-A3", text: "Cevap-A tamam" });
  ncDone(B, { turn_id: "turn-B3", text: "Cevap-B bitti" });
  // Each bubble must show its OWN server text; it must neither double its own text
  // nor take the other's text.
  assert.equal(A.bubble._lastMd, "Cevap-A tamam", "A final text is singular + its own");
  assert.equal(B.bubble._lastMd, "Cevap-B bitti", "B final text is singular + its own");
  assert.equal(A.acc, "Cevap-A tamam", "A acc is singular (no concat)");
  assert.equal(B.acc, "Cevap-B bitti", "B acc is singular (no concat)");
  T7.unregisterStream(A);
  T7.unregisterStream(B);
});

check("concurrent-(c2): turn_id globally unique → the shield still works per-conv (prevents resume doubling)", () => {
  // A's turn streams live + done (records canonically). Then a second follower
  // (resume) arrives replaying the SAME turn from 0 → REPLACE, not APPEND.
  // While B streams concurrently, this shield must not affect B (different turn_id).
  const A = makeConvCtx("conv-A", "turn-dup");
  const B = makeConvCtx("conv-B", "turn-other");
  T7.registerStream(A, null);
  T7.registerStream(B, null);
  ncDelta(A, "X ");
  ncDelta(B, "Y ");
  ncDelta(A, "Z");
  ncDone(A, { turn_id: "turn-dup", text: "X Z" });
  // resume follower (same turn_id, fresh ctx) — replays from 0.
  const Aresume = makeConvCtx("conv-A", "turn-dup");
  ncDelta(Aresume, "X ");
  ncDelta(Aresume, "Z");
  assert.equal(Aresume.acc, "X Z", "resume follower acc is REPLACED with the canonical single text (not concatenated)");
  // B must not be affected.
  ncDelta(B, "W");
  assert.equal(B.acc, "Y W", "B must independently continue its own text (unaffected by A's shield)");
  T7.unregisterStream(A);
  T7.unregisterStream(B);
});

check("concurrent-(d): conv-adopt changes the global active-conv only for the DISPLAYED stream", () => {
  ncConv.length = 0;
  const A = makeConvCtx("conv-A", "turn-A4"); // pretend convId is not yet adopted
  const B = makeConvCtx("conv-B", "turn-B4");
  // A starts anon (no convId) and DISPLAYED; B is background, with a convId.
  const Aanon = makeConvCtx(null, "turn-A4");
  T7.registerStream(Aanon, null);
  T7.registerStream(B, null);
  // Displayed = Aanon (no conv yet → with foreground null it's not single-
  // foreground; we can't explicitly set the foreground to the conv Aanon will
  // adopt, so we test the rekey+foreground migration during adopt).
  T7.setForegroundConversation(null);
  // Aanon learns its own conv → foreground (foreground null + ...) but multi-stream:
  // foregroundConvId null + 2 records → isForegroundStream false. So adopt must NOT
  // change the global (ambiguous foreground, accidentally scrolling the visible one).
  T7.adoptStreamConversationId(Aanon, "conv-A");
  assert.equal(Aanon.convId, "conv-A", "the stream updates its own convId in any case");
  assert.deepEqual(ncConv, [], "with an ambiguous/multi-stream foreground, adopt does NOT change the global active-conv");
  // Now explicitly display A → adopt must now set the global to A.
  ncConv.length = 0;
  T7.setForegroundConversation("conv-A");
  T7.adoptStreamConversationId(Aanon, "conv-A");
  assert.deepEqual(ncConv, ["conv-A"], "when A is displayed, adopt calls setConversationId(A)");
  // If B (background) tries to adopt its conv, it must NOT shift the global to B.
  ncConv.length = 0;
  T7.adoptStreamConversationId(B, "conv-B");
  assert.deepEqual(ncConv, [], "background B must not shift the global active-conv (displayed A stays)");
  T7.unregisterStream(Aanon);
  T7.unregisterStream(B);
});

check("concurrent-(d2): when a background stream FINISHES the foreground composer/'Typing' is UNTOUCHED", () => {
  ncUi.length = 0;
  const A = makeConvCtx("conv-A", "turn-A5"); // displayed (foreground)
  const B = makeConvCtx("conv-B", "turn-B5"); // background
  T7.registerStream(A, null);
  T7.registerStream(B, null);
  T7.setForegroundConversation("conv-A");
  // Background B finishes → finalizeStreamUi(B) must NOT touch the foreground UI.
  T7.finalizeStreamUi(B);
  assert.deepEqual(ncUi, [], "background B finishing must not close displayed A's composer/'Typing'");
  // Foreground A finishes → finalizeStreamUi(A) must close the global UI (composer→SEND).
  ncUi.length = 0;
  T7.finalizeStreamUi(A);
  assert.ok(
    ncUi.some(([k, v]) => k === "streamingUi" && v === false),
    "foreground A finishing must return the composer to SEND",
  );
  T7.unregisterStream(A);
  T7.unregisterStream(B);
});

check("concurrent-(d3): STOP cuts only the displayed conv's stream (the other continues)", () => {
  const A = makeConvCtx("conv-A", "turn-A6"); // displayed
  const B = makeConvCtx("conv-B", "turn-B6"); // background
  const aborts = { A: false, B: false };
  T7.registerStream(A, { abort: () => { aborts.A = true; } });
  T7.registerStream(B, { abort: () => { aborts.B = true; } });
  T7.setForegroundConversation("conv-A");
  // STOP (no argument) → cuts only the displayed A.
  T7.abortActiveChatStream();
  assert.equal(aborts.A, true, "STOP must cut the displayed A");
  assert.equal(aborts.B, false, "STOP must NOT cut background B");
  assert.ok(T7.isConversationStreamActive("conv-B"), "B stream must continue");
  assert.ok(!T7.isConversationStreamActive("conv-A"), "A stream must drop from the record");
  T7.unregisterStream(B);
});

check("concurrent-(d4): convId-targeted STOP cuts only that conv (delete a background chat)", () => {
  const A = makeConvCtx("conv-A", "turn-A7"); // displayed
  const B = makeConvCtx("conv-B", "turn-B7"); // background (to be deleted)
  const aborts = { A: false, B: false };
  T7.registerStream(A, { abort: () => { aborts.A = true; } });
  T7.registerStream(B, { abort: () => { aborts.B = true; } });
  T7.setForegroundConversation("conv-A");
  // Delete background B → abortActiveChatStream("conv-B") must cut only B, without
  // touching the visible A's stream.
  T7.abortActiveChatStream("conv-B");
  assert.equal(aborts.B, true, "targeted STOP must cut B");
  assert.equal(aborts.A, false, "targeted STOP must NOT cut the displayed A");
  assert.ok(T7.isConversationStreamActive("conv-A"), "A stream must continue");
  T7.unregisterStream(A);
});

// ── Parallel-chat voice fixes (per-conv preserve + balanced ttsStreamOpen latch) ─────
check("concurrent-(d5): a NEW-chat send (foreground has no stream) must NOT abort a background stream", () => {
  // FIX 1: streamChat's preserveLiveStream is per-conversation now, so a fresh turn no longer
  // inherits an unrelated conv's state (skipping abort+ttsPlayer.reset). The safety of the no-arg
  // abort in the new-chat path relies on foregroundConvId being a NON-null value with NO registered
  // stream (the EMPTY_THREAD_FOREGROUND sentinel) → the argless abort finds no foreground record and
  // cuts nothing. This locks that invariant so a future new-chat foreground refactor can't regress it.
  const B = makeConvCtx("conv-B", "turn-B8"); // background stream
  let bAborted = false;
  T7.registerStream(B, { abort: () => { bAborted = true; } });
  T7.setForegroundConversation("new-empty-chat"); // non-null, but has NO registered stream
  T7.abortActiveChatStream(); // no arg → foreground (new-empty-chat) has no rec → abort nothing
  assert.equal(bAborted, false, "a new-chat send must NOT abort the unrelated background stream");
  assert.ok(T7.isConversationStreamActive("conv-B"), "background B must keep streaming");
  T7.unregisterStream(B);
});

// Source-contracts for the two fixes (streamChat/consumeSseResponse internals not driven here).
{
  const TSRC = read("akana-chat-transport.js");
  check("src: preserveLiveStream is scoped to the TARGET conversation (not global isStreamActive)", () => {
    assert.match(
      TSRC,
      /preserveLiveStream\s*=[\s\S]*?isConversationStreamActive\(targetConvId\)/,
      "preserveLiveStream must use isConversationStreamActive(targetConvId), not the global isStreamActive()",
    );
  });
  check("src: a voice-turn stream's own end clears its ttsStreamOpen latch regardless of foreground", () => {
    assert.match(TSRC, /voiceTurn:\s*!!opts\.voiceTurn/, "streamCtx must carry voiceTurn");
    assert.match(TSRC, /wasForeground \|\| streamCtx\.voiceTurn/, "finally streamEnd must be voiceTurn-exempt");
    assert.match(TSRC, /isForegroundStream\(streamCtx\) \|\| streamCtx\.voiceTurn/, "tts_end streamEnd must be voiceTurn-exempt");
  });
}

// ── MID-TURN MEMORY-CAPTURE BADGE SIGNAL (U4) ────────────────────────────────
// Bug: the yellow Inbox badge (#memory-nav-badge) did not refresh WHILE a turn
// streamed; an agent memory-write tool (memory_remember / save_memory) commits its
// row to the staging inbox the moment the tool ENDS, but the frontend had no
// mid-turn signal — the badge only reconciled AFTER the turn (via /stats). Fix:
// on the live tool_call `end` frame for a memory-write tool, emit "memory:staged"
// (the same optimistic-bump event the done-branch uses). This test drives the new
// __test.maybeSignalMemoryWriteTool through a recording bus stub and asserts the
// mid-turn contract: emit on END of a memory-write tool, never on start/error, and
// exactly once per call.
{
  const busEvents = [];
  const memCtx = {
    console,
    document: { createElement: (t) => makeEl(t) },
    performance: { now: () => 0 },
    requestAnimationFrame: (cb) => { rafCbs.push(cb); return rafCbs.length; },
    cancelAnimationFrame: () => {},
    CSS: { escape: (s) => s },
  };
  memCtx.window = memCtx;
  memCtx.window.CSS = memCtx.CSS;
  memCtx.window.AkanaChatRender = ctx.window.AkanaChatRender;
  memCtx.window.AkanaCore = ctx.window.AkanaCore;
  memCtx.window.AkanaMarkdown = ctx.window.AkanaMarkdown;
  memCtx.window.AkanaTurnStatus = ctx.window.AkanaTurnStatus;
  // Recording bus stub — capture every emit so we can assert count + payload.
  memCtx.window.AkanaBus = { on() {}, emit: (n, p) => busEvents.push([n, p]) };

  vm.runInNewContext(read("akana-chat-transport.js"), memCtx);
  const TM = memCtx.window.AkanaChatTransport
    .create({ hooks: { stickToBottomIfFollowing() {} } }).__test;
  assert.ok(TM && TM.maybeSignalMemoryWriteTool, "maybeSignalMemoryWriteTool seam must exist");

  const staged = () => busEvents.filter(([n]) => n === "memory:staged");

  check("mem-badge: memory-write tool START does NOT signal (write not committed)", () => {
    busEvents.length = 0;
    const S = {};
    TM.maybeSignalMemoryWriteTool(S, {
      id: "t1",
      name: "mcp__akana_memory__memory_remember",
      phase: "start",
    });
    assert.equal(staged().length, 0, "start frame must not emit memory:staged");
  });

  check("mem-badge: memory-write tool END emits exactly one memory:staged {count:1}", () => {
    busEvents.length = 0;
    const S = {};
    TM.maybeSignalMemoryWriteTool(S, {
      id: "t1",
      name: "mcp__akana_memory__memory_remember",
      phase: "end",
      status: "completed",
    });
    const ev = staged();
    assert.equal(ev.length, 1, "one memory:staged on end");
    // Cross-realm object → compare the field, not object identity/prototype.
    assert.equal(ev[0][1] && ev[0][1].count, 1, "payload is {count:1}");
  });

  check("mem-badge: replaying the same end frame stays deduped (one signal per call id)", () => {
    busEvents.length = 0;
    const S = {};
    const frame = { id: "t1", name: "memory_remember", phase: "end", status: "completed" };
    TM.maybeSignalMemoryWriteTool(S, frame);
    TM.maybeSignalMemoryWriteTool(S, frame);
    assert.equal(staged().length, 1, "same call id → still exactly one emit");
  });

  check("mem-badge: gemini/openai save_memory END also emits (native decl name)", () => {
    busEvents.length = 0;
    const S = {};
    TM.maybeSignalMemoryWriteTool(S, { id: "t2", name: "save_memory", phase: "end" });
    assert.equal(staged().length, 1, "save_memory end must emit");
  });

  check("mem-badge: errored memory-write END does NOT signal (write did not land)", () => {
    busEvents.length = 0;
    const S = {};
    TM.maybeSignalMemoryWriteTool(S, {
      id: "t3",
      name: "memory_remember",
      phase: "end",
      status: "error",
    });
    assert.equal(staged().length, 0, "errored write must not emit");
  });

  check("mem-badge: a non-memory tool END does NOT signal (name gate)", () => {
    busEvents.length = 0;
    const S = {};
    TM.maybeSignalMemoryWriteTool(S, {
      id: "t4",
      name: "web_search",
      phase: "end",
      status: "completed",
    });
    assert.equal(staged().length, 0, "non-memory tool must not emit");
  });

  check("mem-badge: full stream contract — signal fires MID-turn (before done)", () => {
    // Drive the real handleChatStreamEvent tool_call branch: a memory-write tool
    // END must emit memory:staged BEFORE any done frame arrives.
    busEvents.length = 0;
    const S = makeStreamCtx("mem");
    S.msgBody = makeEl("div");
    S.bubble = makeEl("div");
    S.meta = makeEl("div");
    S.turnId = "turn-mem";
    S.acc = "";
    S.convId = "conv-mem";
    const evt = (name, data) =>
      TM.handleChatStreamEvent({ event: name, data: JSON.stringify(data) }, S);
    evt("tool_call", { call: { id: "m1", name: "mcp__akana_memory__memory_remember", phase: "start" } });
    assert.equal(staged().length, 0, "no signal on the start frame");
    evt("tool_call", { call: { id: "m1", name: "mcp__akana_memory__memory_remember", phase: "end", status: "completed" } });
    assert.equal(staged().length, 1, "signal fires on the tool END — mid-turn, before done");
  });
}

console.log(`chat_stream_isolation.harness: ${passed} isolation contracts PASSED ✓`);
