/**
 * Hunt-5 "foreground gate" contract — backend-free, node-vm + fake DOM.
 *
 * The transport writes several SINGLETON UI surfaces (the turn-status strip, the
 * composer queue chip, the voice bus) while N conversations can stream at once.
 * Every such write must be gated on isForegroundConv / isForegroundStream, or a
 * background (or raced) stream paints over the DISPLAYED conversation.
 *
 * Locked here (one check per confirmed finding):
 *   resume:begin      resumeActiveTurn must not bind the strip to a non-displayed conv —
 *                     while still retaining that conversation's real turn start
 *   queue:202         a queued send must not write the displayed chat's queue chip
 *   phase:connecting  streamChat's setPhase must not repaint another chat's strip
 *   bus:error         chat:stream:error must not reach the voice scene from background
 *   bus:tool          voice:tool must not reach the voice scene from background
 *   tts:replay        a GET /chat/active follower replays the buffer from index 0 →
 *                     its tts_chunk frames are already-heard audio, never re-speak them —
 *                     but ONLY the replayed prefix: the live tail must still be spoken
 *   turn:rescue       the WS "server completed the turn" safety net ABORTS a live stream,
 *                     so it may only fire on POSITIVE identity evidence
 *   strip:handback    a stream ending must not drop the clock of a conversation whose
 *                     background job is still running
 *
 * Run: node tests/web/hunt5_transport_gates.harness.mjs
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
const failures = [];
function check(label, fn) {
  try {
    fn();
    passed += 1;
  } catch (e) {
    failures.push(`${label}: ${e && e.message}`);
  }
}
async function checkAsync(label, fn) {
  try {
    await fn();
    passed += 1;
  } catch (e) {
    failures.push(`${label}: ${e && e.message}`);
  }
}

// ── Fake DOM ─────────────────────────────────────────────────────────────────
function makeEl(tag = "div") {
  return {
    tagName: String(tag).toUpperCase(),
    children: [],
    dataset: {},
    style: {},
    attrs: {},
    _cards: [],
    _text: "",
    parentNode: null,
    parentElement: null,
    classList: {
      _s: new Set(),
      add(...c) { c.forEach((x) => this._s.add(x)); },
      remove(...c) { c.forEach((x) => this._s.delete(x)); },
      contains(c) { return this._s.has(c); },
      toggle(c, on) { const w = on === undefined ? !this._s.has(c) : on; if (w) this._s.add(c); else this._s.delete(c); return w; },
    },
    appendChild(n) { this.children.push(n); if (n) n.parentNode = this; return n; },
    append(...n) { n.forEach((x) => this.appendChild(x)); },
    insertBefore(n) { this.children.unshift(n); if (n) n.parentNode = this; return n; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k] ?? null; },
    removeAttribute(k) { delete this.attrs[k]; },
    addEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    closest() { return null; },
    after() {},
    remove() {},
    scrollTo() {},
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); },
  };
}

// ── vm context ───────────────────────────────────────────────────────────────
const turnStatusCalls = []; // ["begin"|"end"|"setPhase"|"clear", ...args]
const busEvents = [];       // [name, payload]
const ttsEnq = [];          // audio_b64 handed to the player
const queueDepthCalls = []; // setQueueDepth(depth)
const toasts = [];
let fetchImpl = async () => { throw new Error("fetch not configured"); };

const ctx = {
  console,
  TextDecoder,
  TextEncoder,
  AbortController,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  performance: { now: () => 0 },
  // Real (macrotask) scheduling: the SSE drain awaits its own rAF callback, so a
  // no-op stub would hang consumeSseResponse forever.
  requestAnimationFrame: (cb) => setTimeout(cb, 0),
  cancelAnimationFrame: (id) => clearTimeout(id),
  CSS: { escape: (s) => s },
  fetch: (...a) => fetchImpl(...a),
  addEventListener() {},
  removeEventListener() {},
  document: { createElement: (t) => makeEl(t), getElementById: () => null, addEventListener() {} },
};
// sessionStorage survives F5 in the same tab — the only place a reloaded page can learn
// what it already spoke of a turn that is STILL running.
const _session = new Map();
ctx.sessionStorage = {
  getItem: (k) => (_session.has(k) ? _session.get(k) : null),
  setItem: (k, v) => _session.set(k, String(v)),
  removeItem: (k) => _session.delete(k),
  _dump: () => Array.from(_session.values()),
};
ctx.window = ctx;
ctx.window.CSS = ctx.CSS;
ctx.window.AkanaCore = {
  baseUrl: () => "",
  authHeaders: () => ({}),
  parseApiError: (b, s) => `HTTP ${s}`,
  escapeHtml: (s) => s,
};
ctx.window.AkanaI18n = { t: (k) => k };
ctx.window.AkanaMarkdown = {
  setBubbleMarkdown(bubble, text) { if (bubble) bubble._lastMd = String(text); },
  appendBubbleStreamText() {},
};
ctx.window.AkanaChatRender = {
  renderToolCall: () => makeEl("div"),
  upsertToolCallCard: () => makeEl("div"),
  renderMemoryUse: () => null,
  upsertToolCardIntoTimeline(body, call) {
    const id = String((call && (call.id || call.call_id)) || "");
    if (!body._cards.includes(id)) body._cards.push(id);
    return { dataset: { status: "done" }, nextElementSibling: null, after() {} };
  },
  setStatusIcon() {},
};
ctx.window.AkanaTurnStatus = {
  isActive: () => false,
  begin: (...a) => { turnStatusCalls.push(["begin", ...a]); },
  end: (...a) => { turnStatusCalls.push(["end", ...a]); },
  resume: (...a) => { turnStatusCalls.push(["resume", ...a]); },
  setPhase: (...a) => { turnStatusCalls.push(["setPhase", ...a]); },
  clear: (...a) => { turnStatusCalls.push(["clear", ...a]); },
  noteClock: (...a) => { turnStatusCalls.push(["noteClock", ...a]); },
  mount() {},
};
ctx.window.AkanaBus = { on() {}, emit: (n, p) => { busEvents.push([n, p]); } };
// The chat module owns the background-work markers; the transport only asks.
ctx.window.AkanaChat = { hasLiveBackgroundTurn: () => false };

vm.runInNewContext(read("akana-chat-transport.js"), ctx);

let displayedConvId = null;
const transport = ctx.window.AkanaChatTransport.create({
  conversationIdForMemory: () => displayedConvId,
  setConversationId(id) { displayedConvId = id; },
  syncConversationLogFromServer() {},
  reloadConversationLogFromServer() {},
  applyChatServerAction() {},
  hooks: {
    log: makeEl("div"),
    updateEmptyState() {},
    stickToBottomIfFollowing() {},
    setStreamingUi() {},
    setComposerHint() {},
    setQueueDepth: (d) => { queueDepthCalls.push(d); },
    showToast: (m) => { toasts.push(m); },
    streamTtsParam: () => "",
    ttsPlayer: { enqueue: (b) => { ttsEnq.push(b); }, reset() {}, acceptGen: () => 1 },
  },
});
const T = transport.__test;
// INTEGRITY (must run before anything else): almost every contract below is NEGATIVE — "the
// strip must not be painted", "this audio must never reach the player", "this stream must
// not be aborted". Each of them passes vacuously the moment the harness stops reaching the
// code it is judging, so assert the seams and the driven entry points exist FIRST; drift
// then fails loudly here instead of turning the whole file green for the wrong reason.
for (const seam of [
  "setForegroundConversation",
  "handleChatStreamEvent",
  "registerStream",
  "unregisterStream",
]) {
  assert.equal(typeof T?.[seam], "function", `test seam __test.${seam} must exist`);
}
for (const api of ["resumeActiveTurn", "streamChat", "reconcileServerCompletedTurn"]) {
  assert.equal(typeof transport[api], "function", `driven entry point ${api} must exist`);
}

function resetProbes() {
  turnStatusCalls.length = 0;
  busEvents.length = 0;
  ttsEnq.length = 0;
  queueDepthCalls.length = 0;
  toasts.length = 0;
}

/** A Response whose SSE body yields `frames` then closes (no `done` frame). */
function sseResponse(frames, headers = {}) {
  const enc = new TextEncoder();
  const parts = frames.map((f) => enc.encode(`event: ${f.event}\ndata: ${JSON.stringify(f.data)}\n\n`));
  let i = 0;
  return {
    ok: true,
    status: 200,
    headers: { get: (k) => headers[k] ?? null },
    body: {
      getReader: () => ({
        read: async () => (i < parts.length ? { value: parts[i++], done: false } : { value: undefined, done: true }),
        releaseLock() {},
        cancel: async () => {},
      }),
      cancel: async () => {},
    },
  };
}

// ═════════════════════════════════════════════════════════════════════════════
// resume:begin — resumeActiveTurn must not bind the singleton strip to a
// conversation the user is no longer looking at (concurrent-turns-1,
// singleton-ui-across-chats-2). The user can switch chats DURING the
// probeActiveTurn await, so the switch is simulated inside the fetch mock.
// ═════════════════════════════════════════════════════════════════════════════
await checkAsync("resume:begin — a resume whose conv lost the foreground must NOT touch the strip", async () => {
  resetProbes();
  displayedConvId = "conv-A";
  T.setForegroundConversation("conv-A");
  const started = Date.now() - 300000; // the turn has been running for 5 minutes
  fetchImpl = async () => {
    // The user clicks chat C while the probe is in flight.
    T.setForegroundConversation("conv-C");
    return sseResponse([], { "X-Akana-Turn-Started": String(started) });
  };
  await transport.resumeActiveTurn("conv-A");
  assert.deepEqual(
    turnStatusCalls.filter(([k]) => k === "begin"),
    [],
    "background resume must not call AkanaTurnStatus.begin (would paint conv-A's clock over conv-C)",
  );
  // …but NOT painting is not the same as forgetting: the server just told us when this turn
  // really started, and switching back to conv-A later must not read "Preparing · 0:00".
  assert.deepEqual(
    turnStatusCalls.filter(([k]) => k === "noteClock"),
    [["noteClock", "conv-A", started]],
    "the resumed turn's real start must be retained for its own conversation",
  );
});

// ═════════════════════════════════════════════════════════════════════════════
// resume:202 — GET /chat/active answers 202 (+JSON) for a turn with NO follower
// buffer (background_run / schedule fire / blocking / voice). 202 is 2xx AND has a
// body, so a bare `!r.ok || !r.body` test hands it to the resume path, which then
// attaches an SSE consumer to JSON: an assistant bubble that never fills, and the
// normal log refresh skipped because the caller was told a turn "resumed".
// ═════════════════════════════════════════════════════════════════════════════
function acceptedResponse(payload = { running: true, kind: "background", started_at: Date.now() }) {
  let cancelled = false;
  return {
    ok: true, // 202 IS 2xx — this is exactly why the old guard let it through
    status: 202,
    headers: { get: () => null },
    json: async () => payload,
    body: {
      cancel: async () => {
        cancelled = true;
      },
      getReader: () => ({
        read: async () => ({ value: undefined, done: true }),
        releaseLock() {},
        cancel: async () => {},
      }),
    },
    _wasCancelled: () => cancelled,
  };
}

await checkAsync("resume:202 — a non-followable (background) turn must not be resumed as a stream", async () => {
  resetProbes();
  displayedConvId = "conv-A";
  T.setForegroundConversation("conv-A");
  const accepted = acceptedResponse();
  fetchImpl = async () => accepted;
  const resumed = await transport.resumeActiveTurn("conv-A");
  assert.equal(resumed, false, "202 means 'running but nothing to follow' — never a resume");
  assert.deepEqual(
    turnStatusCalls.filter(([k]) => k === "begin"),
    [],
    "a 202 must not start the strip (the WS turn_active pair owns the background indicator)",
  );
  assert.ok(accepted._wasCancelled(), "the JSON body must be released, not left open");
});

await checkAsync("resume:begin — a resume for the DISPLAYED conv still seeds the strip clock", async () => {
  resetProbes();
  const started = Date.now() - 5000;
  displayedConvId = "conv-A";
  T.setForegroundConversation("conv-A");
  fetchImpl = async () => sseResponse([], { "X-Akana-Turn-Started": String(started) });
  await transport.resumeActiveTurn("conv-A");
  const begins = turnStatusCalls.filter(([k]) => k === "begin");
  assert.equal(begins.length, 1, "foreground resume must still begin the strip");
  assert.equal(begins[0][1], "conv-A", "bound to its own conversation");
  assert.equal(begins[0][2], started, "seeded with the server's real turn start");
});

// ═════════════════════════════════════════════════════════════════════════════
// tts:replay — the resume follower replays the server buffer from index 0, so
// its tts_chunk frames are audio the user ALREADY heard (voice-vs-chat-1).
// ═════════════════════════════════════════════════════════════════════════════
await checkAsync("tts:replay — resumeActiveTurn must NOT re-speak replayed tts_chunk audio", async () => {
  resetProbes();
  displayedConvId = "conv-A";
  T.setForegroundConversation("conv-A");
  fetchImpl = async () => sseResponse([{ event: "tts_chunk", data: { audio_b64: "REPLAYED", mime: "audio/mp3" } }]);
  await transport.resumeActiveTurn("conv-A");
  assert.deepEqual(ttsEnq, [], "a replayed tts_chunk must never reach the player (already heard)");
});

check("tts:replay — a LIVE (non-follower) foreground stream still plays its audio", () => {
  resetProbes();
  T.setForegroundConversation("conv-A");
  const live = { convId: "conv-A", acc: "", msgBody: makeEl("div"), bubble: makeEl("div"), meta: makeEl("div"), scroller: makeEl("div"), insertBeforeBubble() {} };
  T.registerStream(live, null);
  T.handleChatStreamEvent({ event: "tts_chunk", data: JSON.stringify({ audio_b64: "LIVE" }) }, live);
  T.unregisterStream(live);
  assert.deepEqual(ttsEnq, ["LIVE"], "the live foreground stream must still play");
});

/** A stream record with only the fields the driven paths read. */
function makeStreamCtx(convId, turnId = null) {
  return {
    convId,
    turnId,
    acc: "",
    doneMeta: null,
    serverError: null,
    msgBody: makeEl("div"),
    bubble: makeEl("div"),
    meta: makeEl("div"),
    scroller: makeEl("div"),
    insertBeforeBubble() {},
  };
}

await checkAsync("tts:replay — the LIVE tail of a resumed turn is still spoken", async () => {
  // The gate was per-STREAM: after F5 with read-aloud on, the remainder of a turn that is
  // STILL RUNNING was never spoken — the point of resuming is to hear the rest. The server
  // stamps every tts_chunk with a per-turn `seq`, so the boundary is per FRAME: sentences
  // at or below what this tab already spoke are replay, the ones above it are the live tail.
  resetProbes();
  displayedConvId = "conv-A";
  T.setForegroundConversation("conv-A");
  const live = makeStreamCtx("conv-A", "turn-9");
  T.registerStream(live, null);
  T.handleChatStreamEvent({ event: "tts_chunk", data: JSON.stringify({ audio_b64: "S1", seq: 1 }) }, live);
  T.handleChatStreamEvent({ event: "tts_chunk", data: JSON.stringify({ audio_b64: "S2", seq: 2 }) }, live);
  T.unregisterStream(live);
  assert.deepEqual(ttsEnq, ["S1", "S2"], "precondition: the user heard the first two sentences");
  ttsEnq.length = 0;
  // F5 / tab-return: the follower replays the buffer from index 0 and continues live.
  fetchImpl = async () =>
    sseResponse([
      { event: "meta", data: { turn_id: "turn-9", conversation_id: "conv-A" } },
      { event: "tts_chunk", data: { audio_b64: "S1", seq: 1 } },
      { event: "tts_chunk", data: { audio_b64: "S2", seq: 2 } },
      { event: "tts_chunk", data: { audio_b64: "S3", seq: 3 } },
    ]);
  await transport.resumeActiveTurn("conv-A");
  assert.deepEqual(
    ttsEnq,
    ["S3"],
    "already-heard sentences must stay silent, but the part the user is still waiting for must play",
  );
});

check("tts:replay — the spoken mark is persisted, so a RELOADED page can still tell replay from live", () => {
  // In-memory state does not survive F5 — which is precisely when a resume follower attaches.
  assert.ok(
    ctx.sessionStorage._dump().some((v) => String(v).includes("turn-9")),
    "the highest spoken sentence must outlive the page, or every reload re-speaks or drops the whole tail",
  );
});

await checkAsync("tts:replay — a follower for a turn this tab never heard stays silent", async () => {
  // No mark for this turn: the frames are indistinguishable from a full replay, and audio
  // is irreversible once spoken — never risk re-speaking a whole answer.
  resetProbes();
  displayedConvId = "conv-Z";
  T.setForegroundConversation("conv-Z");
  fetchImpl = async () =>
    sseResponse([
      { event: "meta", data: { turn_id: "turn-Z", conversation_id: "conv-Z" } },
      { event: "tts_chunk", data: { audio_b64: "Z1", seq: 1 } },
    ]);
  await transport.resumeActiveTurn("conv-Z");
  assert.deepEqual(ttsEnq, [], "an unknown turn's audio must never be replayed at the user");
});

// ═════════════════════════════════════════════════════════════════════════════
// turn:rescue — the WS "server says the turn completed" safety net ABORTS a live
// stream, so it may fire only on POSITIVE identity evidence that the completed
// turn IS this stream's turn. `ctx.turnId` is written by the `meta` frame, which
// may not have landed when the completion was broadcast — so it must be re-read
// INSIDE the grace timer, not snapshotted at entry.
// ═════════════════════════════════════════════════════════════════════════════
async function driveRescue({ turnId = null, atid = "", duringGrace = null } = {}) {
  const cid = "conv-A";
  const s = makeStreamCtx(cid, turnId);
  let aborted = false;
  T.setForegroundConversation(cid);
  T.registerStream(s, { abort() { aborted = true; } });
  // The 2 s grace is driven by hand: capture its callback instead of waiting for it, so
  // "meta arrived during the grace" is deterministic.
  const realSetTimeout = ctx.setTimeout;
  let graceCb = null;
  ctx.setTimeout = (cb) => { graceCb = cb; return 0; };
  const p = transport.reconcileServerCompletedTurn(cid, atid);
  ctx.setTimeout = realSetTimeout;
  if (duringGrace) duringGrace(s);
  if (graceCb) graceCb();
  const rescued = await p;
  T.unregisterStream(s);
  return { rescued, aborted };
}

await checkAsync("turn:rescue — a completion carrying NO turn id must not abort a live stream", async () => {
  resetProbes();
  const r = await driveRescue({ turnId: "t1", atid: "" });
  assert.equal(r.aborted, false, "no assistant_turn_id identifies no turn — aborting on that froze the user's answer");
  assert.equal(r.rescued, false, "…and the caller must not be told a stream was recovered");
});

await checkAsync("turn:rescue — a completion must not abort a stream that has no turn id yet", async () => {
  resetProbes();
  const r = await driveRescue({ turnId: null, atid: "t-other" });
  assert.equal(r.aborted, false, "a stream still waiting for its own `meta` cannot be proven to be this turn");
  assert.equal(r.rescued, false);
});

await checkAsync("turn:rescue — the id is re-read INSIDE the grace: a matching meta still rescues", async () => {
  resetProbes();
  const r = await driveRescue({ turnId: null, atid: "t7", duringGrace: (s) => { s.turnId = "t7"; } });
  assert.equal(r.aborted, true, "the stalled stream is provably this turn — the safety net must still fire");
  assert.equal(r.rescued, true);
});

await checkAsync("turn:rescue — a meta arriving during the grace that MISMATCHES stops the abort", async () => {
  resetProbes();
  const r = await driveRescue({ turnId: null, atid: "t7", duringGrace: (s) => { s.turnId = "t8"; } });
  assert.equal(r.aborted, false, "the entry-time snapshot could not see this — the live turn is a different one");
  assert.equal(r.rescued, false);
});

await checkAsync("turn:rescue — a stalled stream with a MATCHING id is still recovered", async () => {
  resetProbes();
  const r = await driveRescue({ turnId: "t5", atid: "t5" });
  assert.equal(r.aborted, true, "the half-open-TCP safety net must survive the tightening");
  assert.equal(r.rescued, true);
});

// ═════════════════════════════════════════════════════════════════════════════
// strip:handback — the strip's clocks are per CONVERSATION, not per turn, so the
// user's own stream ending must not take the clock of a background job that is
// still running in the same chat (it would restart its elapsed at 0:00).
// ═════════════════════════════════════════════════════════════════════════════
check("strip:handback — a stream ending must not drop the clock of a conv with live background work", () => {
  resetProbes();
  ctx.window.AkanaChat = { hasLiveBackgroundTurn: (id) => id === "conv-BG" };
  const bg = makeStreamCtx("conv-BG", "t1");
  T.registerStream(bg, null);
  T.unregisterStream(bg);
  assert.deepEqual(
    turnStatusCalls.filter(([k]) => k === "clear"),
    [],
    "the job still holds this conversation — dropping its clock restarts its elapsed on the hand-back",
  );
  const plain = makeStreamCtx("conv-A", "t2");
  T.registerStream(plain, null);
  T.unregisterStream(plain);
  assert.deepEqual(
    turnStatusCalls.filter(([k]) => k === "clear"),
    [["clear", "conv-A"]],
    "with no background work the finished turn's clock must still be dropped (no dead 'working since…')",
  );
  ctx.window.AkanaChat = { hasLiveBackgroundTurn: () => false };
});

// ═════════════════════════════════════════════════════════════════════════════
// queue:202 — a queued send must not write the DISPLAYED chat's queue chip
// (singleton-ui-across-chats-4, concurrent-turns-2).
// ═════════════════════════════════════════════════════════════════════════════
await checkAsync("queue:202 — a queued send whose conv lost the foreground must NOT write the queue chip", async () => {
  resetProbes();
  displayedConvId = "conv-A";
  T.setForegroundConversation("conv-A");
  fetchImpl = async () => {
    // The user clicks chat B during the POST connect await.
    T.setForegroundConversation("conv-B");
    return { ok: false, status: 202, json: async () => ({ queued: true, depth: 3, item_id: "q1" }) };
  };
  const res = await transport.streamChat("hi");
  assert.equal(res.queued, true, "the send still reports queued to its caller");
  assert.deepEqual(queueDepthCalls, [], "conv-A's depth must not land in conv-B's composer");
});

await checkAsync("queue:202 — a queued send for the DISPLAYED conv still writes the chip", async () => {
  resetProbes();
  displayedConvId = "conv-A";
  T.setForegroundConversation("conv-A");
  fetchImpl = async () => ({ ok: false, status: 202, json: async () => ({ queued: true, depth: 2, item_id: "q2" }) });
  const res = await transport.streamChat("hi");
  assert.equal(res.queued, true);
  assert.deepEqual(queueDepthCalls, [2], "foreground queue depth must reach the composer");
});

// ═════════════════════════════════════════════════════════════════════════════
// phase:connecting (concurrent-turns-3) + bus:error (voice-vs-chat-5).
// One drive covers both: streamChat connects, loses the foreground mid-connect,
// and the stream closes with no frames (the EMPTY silent-loss guard → error).
// ═════════════════════════════════════════════════════════════════════════════
async function driveBackgroundStreamFailure() {
  resetProbes();
  displayedConvId = "conv-A";
  T.setForegroundConversation("conv-A");
  fetchImpl = async () => {
    T.setForegroundConversation("conv-B");
    return sseResponse([]);
  };
  await assert.rejects(() => transport.streamChat("hi"), /.*/, "the EMPTY guard still surfaces the error to the caller");
}

await checkAsync("phase:connecting — a stream that lost the foreground must NOT repaint the strip's phase", async () => {
  await driveBackgroundStreamFailure();
  assert.deepEqual(
    turnStatusCalls.filter(([k, v]) => k === "setPhase" && v === "connecting"),
    [],
    "background stream must not repaint the displayed conversation's phase",
  );
});

await checkAsync("bus:error — a background stream's failure must NOT emit chat:stream:error", async () => {
  await driveBackgroundStreamFailure();
  assert.deepEqual(
    busEvents.filter(([n]) => n === "chat:stream:error"),
    [],
    "a background failure must not flip the voice scene to 'no response' / Listening",
  );
});

await checkAsync("phase/bus — the FOREGROUND stream still sets the phase and reports its error", async () => {
  resetProbes();
  displayedConvId = "conv-A";
  T.setForegroundConversation("conv-A");
  fetchImpl = async () => sseResponse([]);
  await assert.rejects(() => transport.streamChat("hi"), /.*/);
  assert.ok(
    turnStatusCalls.some(([k, v]) => k === "setPhase" && v === "connecting"),
    "foreground stream must still set the connecting phase",
  );
  assert.ok(
    busEvents.some(([n]) => n === "chat:stream:error"),
    "foreground failure must still reach the voice scene",
  );
});

// ═════════════════════════════════════════════════════════════════════════════
// bus:tool — voice:tool feeds the Aurora scene AND the voice conversation
// watchdog's activity clock; a background chat's tools must not reach either
// (voice-vs-chat-2).
// ═════════════════════════════════════════════════════════════════════════════
function makeToolCtx(convId) {
  const feed = makeEl("div");
  feed.dataset.finalized = "0";
  return {
    convId,
    meta: makeEl("div"),
    bubble: makeEl("div"),
    msgBody: makeEl("div"),
    scroller: makeEl("div"),
    thoughtFeed: feed,
    thoughtBody: makeEl("div"),
    insertBeforeBubble() {},
    acc: "",
    toolPhaseActive: false,
  };
}

check("bus:tool — a background stream's tool_call must NOT emit voice:tool", () => {
  resetProbes();
  const fg = makeToolCtx("conv-A");
  const bg = makeToolCtx("conv-B");
  T.registerStream(fg, null);
  T.registerStream(bg, null);
  T.setForegroundConversation("conv-A");
  T.handleChatStreamEvent(
    { event: "tool_call", data: JSON.stringify({ call: { id: "b1", name: "grep", phase: "end", status: "ok" } }) },
    bg,
  );
  T.unregisterStream(fg);
  T.unregisterStream(bg);
  assert.deepEqual(
    busEvents.filter(([n]) => n === "voice:tool"),
    [],
    "background tool cards must not leak into the voice scene / reset its activity clock",
  );
});

check("bus:tool — the foreground stream's tool_call still emits voice:tool", () => {
  resetProbes();
  const fg = makeToolCtx("conv-A");
  T.registerStream(fg, null);
  T.setForegroundConversation("conv-A");
  T.handleChatStreamEvent(
    { event: "tool_call", data: JSON.stringify({ call: { id: "a1", name: "grep", phase: "end", status: "ok" } }) },
    fg,
  );
  T.unregisterStream(fg);
  assert.equal(
    busEvents.filter(([n]) => n === "voice:tool").length,
    1,
    "foreground tool cards must still reach the voice scene",
  );
});

if (failures.length) {
  for (const f of failures) console.error(`FAIL ${f}`);
  console.error(`hunt5_transport_gates.harness: ${failures.length} FAILED, ${passed} passed`);
  process.exit(1);
}
console.log(`hunt5_transport_gates.harness: ${passed} foreground-gate contracts PASSED ✓`);
process.exit(0);
