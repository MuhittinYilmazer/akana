/**
 * Page-close stream-release contract — backend-free, node-vm.
 *
 * Root scenario (user report): "when 2 conversations are streaming and I hit
 * Ctrl+Shift+R it DOESN'T REFRESH, it tries to refresh." Root cause: open SSE
 * readers (response.body.getReader().read()) are NOT CANCELED on page close;
 * since the browser waits for the N pending reader.read()s before navigating,
 * the hard-refresh hangs. beforeunload/pagehide only flushed localStorage
 * (akana-chat-store.js), it did NOT RELEASE the streams.
 *
 * Fix: transport.create() binds abortActiveChatStream(null, {all:true}) to
 * pagehide+beforeunload → all AbortControllers abort, the readers resolve, and
 * navigation completes. NOTE: it only cuts the CLIENT fetch; the server-side
 * detached turn continues and is recovered via resumeActiveTurn on reload (no
 * answer loss).
 *
 * Run: node tests/web/chat_unload_release.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const TRANSPORT_SRC = readFileSync(path.join(REPO, "web_ui/static/akana-chat-transport.js"), "utf8");

let passed = 0;
function check(label, fn) {
  fn();
  passed += 1;
  void label;
}

function makeEl(tag = "div") {
  return {
    tagName: String(tag).toUpperCase(),
    children: [],
    dataset: {},
    style: {},
    attrs: {},
    classList: { add() {}, remove() {}, contains() { return false; }, toggle() {} },
    appendChild(n) { this.children.push(n); return n; },
    append(...n) { n.forEach((x) => this.children.push(x)); },
    insertBefore(n) { this.children.unshift(n); return n; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k] ?? null; },
    removeAttribute(k) { delete this.attrs[k]; },
    addEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
    remove() {},
    get textContent() { return ""; },
    set textContent(_v) {},
  };
}

// ── vm context: window mock + pagehide/beforeunload CAPTURE ────────────────
// We capture the window.addEventListener("pagehide"/"beforeunload") calls inside
// transport.create() and fire them EXPLICITLY in the test (no real browser event).
const winHandlers = {};
// Capture document.addEventListener (visibilitychange — mobile screen-lock).
const docHandlers = {};
// Capture ctx.setTimeout (to flush the resume-on-visible debounce in the test).
const pendingTimeouts = [];
// Track probeActiveTurn fetch + reloadConversationLogFromServer (resume proof).
const fetchCalls = [];
const reloadCalls = [];
// Voice conversation mode flag — mutable in the test (exercises the hands-free guard).
let voiceConvMode = false;
// rAF tracking: the refresh-while-answering fix must CANCEL the pending
// reveal/md/drain rAFs on unload (to free the main thread and let navigation
// proceed). We track the handles + cancel state and verify them in the test.
const rafs = [];
const ctx = {
  console,
  // visibilityState is mutated to "hidden"/"visible" in the test; addEventListener
  // captures the visibilitychange handler into docHandlers (mobile screen-lock recovery).
  document: {
    createElement: (t) => makeEl(t),
    visibilityState: "visible",
    addEventListener: (ev, cb) => { (docHandlers[ev] ||= []).push(cb); },
    removeEventListener: (ev, cb) => {
      const a = docHandlers[ev];
      if (a) docHandlers[ev] = a.filter((x) => x !== cb);
    },
  },
  // Make _isLikelyMobile=true (abort-on-hidden is mobile-ONLY): mobile UA + touch.
  navigator: { userAgent: "Mozilla/5.0 (Linux; Android 13; Pixel) Mobile", maxTouchPoints: 5 },
  // probeActiveTurn fetch: 204 → "no active turn" → resume false → log refreshes.
  fetch: (url) => { fetchCalls.push(String(url)); return Promise.resolve({ status: 204, ok: false, body: null }); },
  performance: { now: () => 0 },
  requestAnimationFrame: (cb) => { rafs.push({ cb, cancelled: false }); return rafs.length; },
  cancelAnimationFrame: (id) => { const r = rafs[id - 1]; if (r) r.cancelled = true; },
  // CAPTURE, not no-op: we flush the debounce cb in the test (id returns truthy).
  setTimeout: (cb) => { pendingTimeouts.push(cb); return pendingTimeouts.length; },
  clearTimeout: () => {},
  CSS: { escape: (s) => s },
  addEventListener: (ev, cb) => { (winHandlers[ev] ||= []).push(cb); },
  removeEventListener: (ev, cb) => {
    const a = winHandlers[ev];
    if (a) winHandlers[ev] = a.filter((x) => x !== cb);
  },
};
ctx.window = ctx;
ctx.window.CSS = ctx.CSS;
ctx.window.AkanaChatRender = { upsertToolCardIntoTimeline: () => ({ dataset: {}, after() {} }) };
ctx.window.AkanaCore = { baseUrl: () => "", authHeaders: () => ({}), parseApiError: () => "", escapeHtml: (s) => s };
ctx.window.AkanaMarkdown = { setBubbleMarkdown() {}, appendBubbleStreamText() {} };
ctx.window.AkanaTurnStatus = { isActive: () => false, begin() {}, end() {}, setPhase() {} };
// Voice conversation mode seam — abort-on-hidden must only cut when NOT in voice.
ctx.window.AkanaVoice = { isConversationMode: () => voiceConvMode };

vm.runInNewContext(TRANSPORT_SRC, ctx);

const chatCtx = {
  chatInFlight: false,
  hooks: { setStreamingUi() {}, log: makeEl("div") },
  // resume-on-visible: probe 204 → resume false → the log is RE-RENDERED from the server
  // via reloadConversationLogFromServer (the same recovery F5 / chat-switch use; tracked).
  // syncConversationLogFromServer only rewrites the data model + store and never repaints
  // the DOM, so the return path must call reload — otherwise the finished last answer stays
  // invisible until a manual F5.
  reloadConversationLogFromServer: (id) => { reloadCalls.push(id); return Promise.resolve(); },
  syncConversationLogFromServer: () => Promise.resolve(),
};
const transport = ctx.window.AkanaChatTransport.create(chatCtx);
const T = transport.__test;
assert.ok(T && T.registerStream && T.streamCount && T.abortActiveChatStream, "test seam (__test) must be present");

function makeStream(convId) {
  return { convId, _streamKey: convId, rowEl: makeEl("div"), bubble: makeEl("div"), acc: "" };
}

// ── 1. pagehide aborts all streams ──────────────────────────────────────
check("unload: transport registers the pagehide handler", () => {
  assert.ok(
    Array.isArray(winHandlers.pagehide) && winHandlers.pagehide.length >= 1,
    "transport.create() must bind abort-all to pagehide (hard-refresh fix)",
  );
});

check("unload: also registers the beforeunload handler", () => {
  assert.ok(
    Array.isArray(winHandlers.beforeunload) && winHandlers.beforeunload.length >= 1,
    "transport.create() must also bind abort-all to beforeunload",
  );
});

check("unload: pagehide cancels BOTH concurrent streams + deregisters them", () => {
  const A = makeStream("conv-A");
  const B = makeStream("conv-B");
  const aborted = { A: false, B: false };
  T.registerStream(A, { abort: () => { aborted.A = true; } });
  T.registerStream(B, { abort: () => { aborted.B = true; } });
  assert.equal(T.streamCount(), 2, "two streams must be registered");
  // Simulate the browser's pagehide.
  for (const cb of winHandlers.pagehide) cb({ type: "pagehide" });
  assert.equal(aborted.A, true, "pagehide must cancel A's reader");
  assert.equal(aborted.B, true, "pagehide must cancel B's reader");
  assert.equal(T.streamCount(), 0, "all stream registrations must be cleared (so navigation can complete)");
});

check("unload: beforeunload also cancels all streams", () => {
  const A = makeStream("conv-A");
  const B = makeStream("conv-B");
  const aborted = { A: false, B: false };
  T.registerStream(A, { abort: () => { aborted.A = true; } });
  T.registerStream(B, { abort: () => { aborted.B = true; } });
  for (const cb of winHandlers.beforeunload) cb({ type: "beforeunload" });
  assert.equal(aborted.A && aborted.B, true, "beforeunload must cancel both streams");
  assert.equal(T.streamCount(), 0, "registrations must be cleared");
});

// ── 1b. Stops the rAF STORM (refresh-while-answering fix) ─────────────────
// User: "it refreshes while thinking but doesn't refresh right while the ANSWER
// streams." Root: in the answer phase every delta triggers a reveal/markdown rAF
// (O(N) re-render); if unload does NOT CANCEL them the rAF storm keeps navigation
// open. Aborting the fetch alone isn't enough → the handler must also stop each
// live stream's reveal/md/scroll rAFs.
check("unload: pagehide CANCELS pending reveal/md rAFs (frees the main thread)", () => {
  const A = makeStream("conv-A");
  T.registerStream(A, { abort: () => {} });
  // Simulate 'answer streaming': A's reveal rAF + md rAF are pending.
  const scratch = T.ensureMdScratch(A);
  scratch.revealStopped = false;
  scratch.revealRaf = ctx.requestAnimationFrame(() => {});
  scratch.mdRaf = ctx.requestAnimationFrame(() => {});
  const revealId = scratch.revealRaf;
  const mdId = scratch.mdRaf;
  for (const cb of winHandlers.pagehide) cb({ type: "pagehide" });
  assert.equal(scratch.revealRaf, null, "pagehide must null out the reveal rAF handle");
  assert.equal(scratch.mdRaf, null, "pagehide must null out the md-throttle rAF handle");
  assert.equal(rafs[revealId - 1].cancelled, true, "the reveal rAF must be cancelAnimationFrame'd");
  assert.equal(rafs[mdId - 1].cancelled, true, "the md rAF must be cancelAnimationFrame'd");
  assert.equal(T.streamCount(), 0, "the stream must also be aborted and deregistered");
});

// ── 2. Safe when there are no streams (no-op, no throw) ────────────────────────────────
check("unload: pagehide does NOT throw when there are no streams (idempotent)", () => {
  assert.equal(T.streamCount(), 0, "precondition: no registrations");
  for (const cb of winHandlers.pagehide) cb({ type: "pagehide" });
  assert.equal(T.streamCount(), 0, "safe even with no streams");
});

// ── 3. MOBILE LIFECYCLE: screen-lock (hidden→abort) / return (visible→resume) ─
// User report: "if I close the screen while a conv is going it errors, I want it
// to run in the background." Root: on mobile pagehide DOESN'T fire; only
// visibilitychange→hidden does. When the OS radio sleeps the SSE reader is
// rejected with a NETWORK ERROR → serverError=CONN → "⚠ Interrupted". Fix: a
// CLEAN abort on hide (mobile + non-voice only), resume the detached turn when
// visible again.
function fireVisibility(state) {
  ctx.document.visibilityState = state;
  for (const cb of docHandlers.visibilitychange || []) cb({ type: "visibilitychange" });
}

check("lifecycle: visibilitychange + pageshow handlers are registered", () => {
  assert.ok(
    Array.isArray(docHandlers.visibilitychange) && docHandlers.visibilitychange.length >= 1,
    "transport must bind to visibilitychange (screen-lock recovery)",
  );
  assert.ok(
    Array.isArray(winHandlers.pageshow) && winHandlers.pageshow.length >= 1,
    "transport must bind to pageshow (bfcache restore)",
  );
});

check("lifecycle: WHEN HIDDEN (mobile, NOT voice) the live stream is CLEANLY aborted", () => {
  voiceConvMode = false;
  const A = makeStream("conv-A");
  let aborted = false;
  T.registerStream(A, { abort: () => { aborted = true; } });
  assert.equal(T.streamCount(), 1, "precondition: stream registered");
  fireVisibility("hidden");
  assert.equal(aborted, true, "screen-lock must CLEANLY abort the client stream (instead of a network error)");
  assert.equal(T.streamCount(), 0, "the registration must be dropped after abort");
});

check("lifecycle: WHEN HIDDEN in voice conversation mode the stream is NOT cut (hands-free preserved)", () => {
  voiceConvMode = true;
  const A = makeStream("conv-A");
  let aborted = false;
  T.registerStream(A, { abort: () => { aborted = true; } });
  fireVisibility("hidden");
  assert.equal(aborted, false, "in voice mode (wake-lock + TTS streaming) the stream must not be cut");
  assert.equal(T.streamCount(), 1, "the stream registration must be preserved");
  T.abortActiveChatStream(null, { all: true }); // cleanup
  voiceConvMode = false;
});

// The VISIBLE return is async (probe→fetch 204→resume false→reload). We drain the
// vm promise chain via a real (outer-realm) setTimeout(0).
await (async () => {
  fetchCalls.length = 0;
  reloadCalls.length = 0;
  voiceConvMode = false;
  const A = makeStream("conv-A");
  T.registerStream(A, { abort: () => {} });
  transport.setForegroundConversation("conv-A");
  fireVisibility("hidden");  // _streamsWereLiveOnHide=true, A abort
  fireVisibility("visible"); // resume debounce timer → pendingTimeouts
  for (const cb of pendingTimeouts.splice(0)) cb(); // flush the debounce
  for (let i = 0; i < 6; i += 1) await new Promise((r) => setTimeout(r, 0));
  check("lifecycle: on VISIBLE return the foreground turn is probed and the log is RE-RENDERED", () => {
    assert.ok(
      fetchCalls.some((u) => u.includes("/api/v1/chat/active/conv-A")),
      "resume must probe the foreground conv (recover the detached turn)",
    );
    assert.deepEqual(
      reloadCalls,
      ["conv-A"],
      "probe 204 (no active turn) → the log must be RE-RENDERED from the server (reload; no half/pending DOM left behind, no F5 needed)",
    );
  });
})();

console.log(`chat_unload_release.harness: ${passed} contracts PASSED ✓`);
