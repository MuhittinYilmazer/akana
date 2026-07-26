/**
 * hunt-5 · chat core — the BACKGROUND-WORK indicator and the status strip. node-vm, no DOM lib.
 *
 * The "working…" strip above the composer is the only thing telling the user that a
 * schedule fire / background_run is still thinking. Contracts locked here:
 *
 *   event-contracts-1 / background-lifecycle-4 (akana-chat.js)
 *     · ONLY source:"background" turn events drive the indicator; an UNSTAMPED event is
 *       the user's own turn (fail quiet) — otherwise the user's quick reply completing in
 *       the same chat erased a job that kept running for minutes,
 *     · the marker is a COUNT, not a set membership: a background job and the user's own
 *       turn are live in the SAME conversation at once.
 *   reload-restore-4 / reload-restore-5 (akana-chat.js)
 *     · the indicator reconciles against GET /chat/active/{id} (204 idle · 202
 *       {running, kind:"background"} · 200 live SSE): a missed turn_completed (server
 *       restart / WS blip / OS sleep) must not leave a phantom strip, and after F5 a
 *       still-running job must become visible again,
 *     · an unreadable answer (network error, 5xx) → NO-OP; a reconcile that cannot see
 *       the truth must never erase a live job's indicator.
 *   voice-vs-chat-4 (akana-chat.js)
 *     · voice conversation mode is pinned to the chat the scene shows — a notification
 *       click retargeting the displayed conversation must not steal the next utterance.
 *   turn-status clear() / noteClock() (akana-turn-status.js)
 *     · clear() on the PAINTED conversation takes the strip down; it used to zero
 *       startedAt while the 1 s timer kept painting → an epoch-sized elapsed time,
 *     · noteClock() retains a NON-displayed turn's real start WITHOUT painting (a resume
 *       for a chat that is not on screen may not own the singleton strip, but its clock
 *       must survive: switching to it later read "Preparing · 0:00").
 *   endBgWorking × clear() (both real modules)
 *     · the two guards must AGREE: retiring a stale background marker may not take down
 *       the strip of the user's own live turn in the same conversation — end() is gated on
 *       !chatInFlight, but clear() force-ends the painted conversation, guard or not.
 *
 * Run: node tests/web/hunt5_chat_core.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const read = (rel) => readFileSync(path.join(REPO, rel), "utf8");

let failures = 0;
let passed = 0;
async function check(label, fn) {
  try {
    await fn();
    passed += 1;
  } catch (e) {
    failures += 1;
    console.error(`✗ ${label}`);
    console.error(`   ${e && e.message ? e.message : e}`);
  }
}

// ── minimal fake DOM ────────────────────────────────────────────────────────
function makeEl(tag = "div") {
  const el = {
    tagName: String(tag).toUpperCase(),
    children: [],
    dataset: {},
    style: { setProperty() {}, removeProperty() {} },
    attrs: {},
    _listeners: {},
    _classes: new Set(),
    hidden: false,
    disabled: false,
    id: "",
    value: "",
    title: "",
    textContent: "",
    innerHTML: "",
  };
  el.classList = {
    add: (...c) => c.forEach((x) => el._classes.add(x)),
    remove: (...c) => c.forEach((x) => el._classes.delete(x)),
    toggle: (c, on) => { const w = on === undefined ? !el._classes.has(c) : !!on; if (w) el._classes.add(c); else el._classes.delete(c); return w; },
    contains: (c) => el._classes.has(c),
  };
  el.setAttribute = (k, v) => { el.attrs[k] = String(v); };
  el.getAttribute = (k) => (k in el.attrs ? el.attrs[k] : null);
  el.removeAttribute = (k) => { delete el.attrs[k]; };
  el.appendChild = (c) => { el.children.push(c); return c; };
  el.append = (...cs) => cs.forEach((c) => el.children.push(c));
  el.insertBefore = (c) => { el.children.push(c); return c; };
  el.remove = () => {};
  el.addEventListener = (t, f) => { (el._listeners[t] ||= []).push(f); };
  el.removeEventListener = () => {};
  el.dispatchEvent = () => true;
  el.querySelector = () => null;
  el.querySelectorAll = () => [];
  el.closest = () => null;
  el.focus = () => {};
  el.select = () => {};
  el.click = () => {};
  el.requestSubmit = () => {};
  return el;
}

function makeDoc(ids = {}) {
  return {
    readyState: "complete",
    body: makeEl("body"),
    getElementById: (id) => ids[id] || null,
    createElement: (t) => makeEl(t),
    addEventListener: () => {},
    querySelector: () => null,
    querySelectorAll: () => [],
  };
}

function makeStorage() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
  };
}

function makeBus() {
  const handlers = {};
  return {
    on: (t, f) => { (handlers[t] ||= []).push(f); },
    emit: (t, e) => { for (const f of handlers[t] || []) f(e); },
  };
}

// ═════════════════════════════════════════════════════════════════════════════
// A. akana-turn-status.js — clear() must be safe while the strip is painted
// ═════════════════════════════════════════════════════════════════════════════
function bootTurnStatus() {
  const form = makeEl("form");
  const doc = {
    readyState: "complete",
    getElementById: (id) => (id === "chat-form" ? form : null),
    createElement: (t) => makeEl(t),
    addEventListener: () => {},
  };
  let now = 1_000_000;
  let painter = null;
  const ctx = {
    window: {
      AkanaI18n: { t: (k) => k },
      setInterval: (fn) => { painter = fn; return 1; },
      clearInterval: () => { painter = null; },
    },
    document: doc,
    console,
    Date: { now: () => now },
  };
  ctx.window.window = ctx.window;
  ctx.window.document = doc;
  vm.runInNewContext(read("web_ui/static/akana-turn-status.js"), ctx);
  return {
    TS: ctx.window.AkanaTurnStatus,
    advance: (ms) => { now += ms; if (painter) painter(); },
    // The module's clock is the VM's fake Date — a caller passing a REAL timestamp would
    // hand it a "future" stamp, which it (correctly) discards.
    now: () => now,
    ticking: () => painter !== null,
    view: () => {
      const strip = form.children[0];
      const label = strip && strip.children[0];
      return { hidden: strip ? strip.hidden : null, text: label ? label.textContent : "" };
    },
  };
}

await check("A1 clear() on the PAINTED conversation takes the strip down (no epoch clock)", () => {
  const r = bootTurnStatus();
  r.TS.begin("A");
  r.advance(5_000);
  assert.match(r.view().text, /0:05/, "precondition: A's clock is painted");
  r.TS.clear("A"); // the turn is over — the clock is dropped
  assert.equal(r.ticking(), false, "clear() must stop the 1s repaint timer for the painted conv");
  r.advance(1_000);
  assert.equal(r.view().hidden, true, "the strip must be hidden — it has no clock left to render");
  assert.ok(
    !/1[0-9]{2,}:/.test(r.view().text),
    `startedAt=0 must never reach paint() (epoch-sized elapsed). got=${JSON.stringify(r.view().text)}`,
  );
});

await check("A2 clear(null) (global drop) also takes the strip down", () => {
  const r = bootTurnStatus();
  r.TS.begin("A");
  r.advance(3_000);
  r.TS.clear(null);
  r.advance(1_000);
  assert.equal(r.view().hidden, true, "a global clear must not leave an active strip without a clock");
});

await check("A3 clear() of ANOTHER conversation leaves the painted strip alone", () => {
  const r = bootTurnStatus();
  r.TS.begin("A");
  r.advance(4_000);
  r.TS.clear("B"); // B's background job ended while A is on screen
  r.advance(1_000);
  assert.equal(r.view().hidden, false, "A's live strip must survive B's clear");
  assert.match(r.view().text, /0:05/, "A keeps its own elapsed");
});

await check("A4 noteClock retains a NON-displayed turn's real start without painting", () => {
  const r = bootTurnStatus();
  r.TS.begin("A"); // the user is looking at A
  r.advance(2_000);
  // A resume for the non-displayed conversation B: the strip is a singleton bound to A, so
  // B may not paint — but B's turn has been running for 5 minutes and switching to it must
  // not read "Preparing · 0:00".
  r.TS.noteClock("B", r.now() - 300_000);
  assert.match(r.view().text, /0:02/, "A's painted clock must be untouched");
  r.TS.resume("B"); // the user switches to B
  assert.match(r.view().text, /5:00/, "B must resume with the elapsed the server reported");
});

await check("A5 noteClock never overwrites the PAINTED conversation's own clock", () => {
  const r = bootTurnStatus();
  r.TS.begin("A");
  r.advance(3_000);
  r.TS.noteClock("A", r.now() - 600_000); // a late/duplicate resume answer for A
  assert.match(r.view().text, /0:03/, "the live strip must keep the clock it is painting");
});

// ═════════════════════════════════════════════════════════════════════════════
// B. akana-chat.js — background-work indicator
// ═════════════════════════════════════════════════════════════════════════════
function loadChat({ voiceMode = false, activity = null, realTurnStatus = false } = {}) {
  const els = {
    log: makeEl("div"),
    "log-scroll": makeEl("div"),
    "chat-form": makeEl("form"),
    msg: makeEl("textarea"),
    "btn-send": makeEl("button"),
    "composer-attachments": makeEl("div"),
    "log-empty": makeEl("div"),
  };
  const doc = makeDoc(els);
  const bus = makeBus();

  // Turn-status spy (the real module is exercised in section A).
  const ts = { resume: [], begin: [], end: 0, clear: [], noteClock: [] };
  const TurnStatus = {
    mount: () => {},
    begin: (id) => ts.begin.push(id),
    resume: (id) => ts.resume.push(id),
    end: () => { ts.end += 1; },
    clear: (id) => ts.clear.push(id),
    noteClock: (id, at) => ts.noteClock.push([id, at]),
    setPhase: () => {},
    isActive: () => false,
  };

  const state = {
    displayed: "",
    voiceMode,
    switches: [],
    streamCalls: [],
    toasts: [],
    archiveActivity: [],
    archiveLoads: 0,
    // GET /chat/active/{id} answers, per conversation. Default 503 = "cannot tell",
    // which is a NO-OP — it keeps the pure event-plumbing cases below deterministic.
    activity: new Map(),
    // Probe latency + stream lifetime the scenario controls: the guard-conflict case
    // needs a reconcile answer that lands AFTER the user's own turn is already live.
    probeGate: null,
    releaseProbe: null,
    releaseStream: null,
    holdStream: false,
    // The transport's view of THIS conversation: a live stream + the rescue seam the
    // WS turn_completed handler drives (reconcileServerCompletedTurn ABORTS a stream).
    streamActive: false,
    rescueCalls: [],
    // Every /chat/active URL the module actually requested — the negative checks below
    // ("the strip must stay hidden") pass vacuously if the probe never happens, so the
    // integrity check asserts the stub is really being hit, per conversation.
    probeUrls: [],
  };

  const threadsStub = {
    conversationIdForMemory: () => state.displayed,
    chatActiveThread: () => ({ conversationId: state.displayed }),
    switchChatConversation: async (id) => { state.switches.push(id); state.displayed = String(id || ""); },
    recordPendingUserMessage: () => {},
    tryHandleChatDeleteCommand: () => false,
    chatProfile: () => "cursor",
    newChatThreadId: () => "t",
    chatStartNewThread: () => {},
    wireArchiveChrome: () => {},
    wireThreadBar: () => {},
    getChatStore: () => ({ threads: {}, activeByProfile: {} }),
    chatRestoreActiveThread: () => {},
    loadChatArchiveList: () => { state.archiveLoads += 1; },
    openArchiveDrawer: () => {},
    closeArchiveDrawer: () => {},
    setConversationId: () => {},
    recordErrorForConversation: () => true,
    chatRecordMessage: () => {},
    syncChatThreadBar: () => {},
    refreshActiveConversationMeta: () => {},
    refreshArchiveActivity: (id) => { state.archiveActivity.push(id); },
    refreshConversationLogAfterTurn: async () => {},
    syncConversationLogFromServer: async () => {},
    getChatArchiveItems: () => [{ id: "B", title: "Rapor" }],
    getActiveConversationMeta: () => null,
    setActiveConversationMeta: () => {},
    setChatArchiveItems: () => {},
    applyChatServerAction: () => {},
    purgeConversationFromChatStore: () => {},
    getPendingNewThread: () => null,
  };

  const transportStub = {
    streamChat: async (text) => {
      state.streamCalls.push({ text, conv: state.displayed });
      // The REAL transport paints the strip for a foreground stream and stays in flight
      // until the SSE `done`; a scenario that needs a LIVE user turn models exactly that.
      if (state.holdStream) {
        win.AkanaTurnStatus?.begin?.(state.displayed);
        await new Promise((r) => { state.releaseStream = r; });
        // The real transport's finalize path ends the SINGLETON strip when the stream
        // closes (finalizeStreamUi → AkanaTurnStatus.end) — the moment a background job
        // that is still running in this chat loses its indicator.
        win.AkanaTurnStatus?.end?.();
      }
      return {};
    },
    isConversationStreamActive: (id) => state.streamActive && id === state.displayed,
    abortActiveChatStream: () => {},
    cancelActiveTurnOnServer: async () => {},
    humanizeChatError: (e) => String(e),
    ensureConversationIdReady: async () => state.displayed,
    fetchConversationTurnsFromServer: async () => ({ status: 404, turns: null }),
    setForegroundConversation: () => {},
    abortConversationTurnsFetch: () => {},
    resumeActiveTurn: async () => true,
    probeActiveTurn: async () => null,
    reconcileServerCompletedTurn: async (cid, atid) => {
      state.rescueCalls.push([cid, atid]);
      return false;
    },
    isForegroundTurnFinalized: () => false,
    reattachLiveRow: () => false,
    activeStreamTurnId: () => null,
  };

  if (activity) for (const [k, v] of Object.entries(activity)) state.activity.set(k, v);

  const fetchImpl = async (url) => {
    const m = /\/chat\/active\/([^/?]+)/.exec(String(url));
    if (m) {
      state.probeUrls.push(String(url));
      // The answer is taken when the request ARRIVES, not when it is released — a probe
      // held open while the world changes still returns the OLD truth. That staleness is
      // the whole point of the coalescing case below; reading the map after the gate
      // would silently hand every joiner a fresh answer and prove nothing.
      const a = state.activity.get(decodeURIComponent(m[1])) || { status: 503 };
      if (state.probeGate) await state.probeGate;
      return {
        ok: a.status >= 200 && a.status < 300,
        status: a.status,
        json: async () => a.body || {},
      };
    }
    return { ok: false, status: 404, json: async () => ({}) };
  };

  const win = {
    AkanaI18n: { t: (k, p) => (p ? `${k}:${JSON.stringify(p)}` : k) },
    AkanaCore: {
      baseUrl: () => "",
      authHeaders: () => ({}),
      authHeadersMultipart: () => ({}),
      escapeHtml: (s) => s,
      parseApiError: (_b, s) => `HTTP ${s}`,
    },
    AkanaSettings: { baseUrl: () => "", authHeaders: () => ({}) },
    AkanaChatRender: { createRenderer: () => ({ chatRenderMessage: () => {} }), mapServerMessagesToThread: (m) => m },
    AkanaChatThreads: { create: () => threadsStub },
    AkanaChatTransport: { create: () => transportStub },
    AkanaShell: { displayedPane: () => els.log, paneFor: () => els.log, displayedConvId: () => state.displayed },
    AkanaTurnStatus: TurnStatus,
    AkanaBus: bus,
    AkanaVoice: { isConversationMode: () => state.voiceMode },
    document: doc,
    localStorage: makeStorage(),
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (h) => clearTimeout(h),
    fetch: fetchImpl,
    addEventListener: () => {},
    // The real turn-status module ticks its own 1 s repaint timer (unref'd: a harness must
    // never be kept alive by it).
    setInterval: (fn, ms) => { const h = setInterval(fn, ms); h.unref?.(); return h; },
    clearInterval: (h) => clearInterval(h),
  };
  win.window = win;
  const ctx = {
    console,
    document: doc,
    window: win,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    queueMicrotask,
    Promise,
    URLSearchParams,
    FormData: class { append() {} },
    Event: class { constructor(t) { this.type = t; } },
    navigator: { clipboard: { writeText: async () => {} } },
    localStorage: win.localStorage,
    fetch: fetchImpl,
    requestAnimationFrame: (fn) => fn(),
    cancelAnimationFrame: () => {},
  };
  vm.createContext(ctx);
  if (realTurnStatus) {
    // Two modules' guards can only be proven to agree against BOTH real modules: the
    // stub's clear() just records, while the shipped clear() force-ends the strip whenever
    // it drops the PAINTED conversation's clock. Still record the calls the stub records.
    vm.runInContext(read("web_ui/static/akana-turn-status.js"), ctx);
    const realTS = win.AkanaTurnStatus;
    win.AkanaTurnStatus = {
      ...realTS,
      begin: (id, at) => { ts.begin.push(id); return realTS.begin(id, at); },
      resume: (id) => { ts.resume.push(id); return realTS.resume(id); },
      end: () => { ts.end += 1; return realTS.end(); },
      clear: (id) => { ts.clear.push(id); return realTS.clear(id); },
      noteClock: (id, at) => { ts.noteClock.push([id, at]); return realTS.noteClock(id, at); },
    };
  }
  vm.runInContext(read("web_ui/static/akana-chat.js"), ctx);
  const Chat = win.AkanaChat;
  Chat.init({
    log: els.log,
    logScroll: els["log-scroll"],
    form: els["chat-form"],
    msg: els.msg,
    sendBtn: els["btn-send"],
    logEmpty: els["log-empty"],
    appendRow: () => null,
    appendUserMessage: () => null,
    appendSystemNotice: () => {},
    updateEmptyState: () => {},
    resizeComposer: () => {},
    setOrb: () => {},
    setComposerHint: () => {},
    stickToBottomIfFollowing: () => {},
    scrollLogToBottom: () => {},
    scrollNewTurnToTop: () => {},
    setLogLoading: () => {},
    showToast: (msg, kind) => state.toasts.push([msg, kind]),
    streamTtsParam: () => "",
    syncOrbWithVoice: () => {},
    updateSettingsHero: () => {},
    loadMemoryConversations: () => {},
    shortConversationId: (id) => id || "none",
    closeSettings: () => {},
  });
  return {
    Chat,
    ts,
    // The strip API the chat module actually calls (spy, or the real module behind it).
    tsApi: win.AkanaTurnStatus,
    bus,
    state,
    setDisplayed: (id) => { state.displayed = String(id || ""); },
    setActivity: (id, status, body) => { state.activity.set(id, { status, body }); },
    /** What the user actually sees above the composer (realTurnStatus only). */
    stripView: () => {
      const strip = els["chat-form"].children[0];
      const label = strip && strip.children[0];
      return { hidden: strip ? strip.hidden : null, text: label ? label.textContent : "" };
    },
    /** Hold every /chat/active answer until releaseProbes() — models a slow server. */
    gateProbes: () => { state.probeGate = new Promise((r) => { state.releaseProbe = r; }); },
    releaseProbes: () => { const r = state.releaseProbe; state.probeGate = null; state.releaseProbe = null; r?.(); },
    holdStream: () => { state.holdStream = true; },
    releaseStream: () => { state.releaseStream?.(); state.holdStream = false; },
    // Opening a chat kicks a fire-and-forget reconcile; let it finish so the next
    // explicit reconcile is not joined to the previous (older-snapshot) run.
    settle: () => new Promise((r) => setTimeout(r, 0)),
  };
}

// ── Harness integrity (run FIRST) ───────────────────────────────────────────
// Most checks below are NEGATIVE ("the strip must stay hidden", "the marker must not be
// resurrected") and every one of them passes vacuously if the module stops talking to the
// stub — a drifted endpoint, a renamed export, a conversation-blind stub. Anchor all of
// that up front so drift fails LOUDLY here instead of quietly turning green everywhere.
await check("B0 integrity — the reconcile really probes GET /chat/active/{id}, per conversation", async () => {
  const h = loadChat({ activity: { A: { status: 204 }, B: { status: 204 } } });
  await h.settle();
  h.state.probeUrls.length = 0;
  for (const name of ["reconcileBgActiveTurn", "maybeShowBgWorking", "onTurnActiveRemote", "onTurnCompletedRemote", "setChatInFlight"]) {
    assert.equal(typeof h.Chat[name], "function", `AkanaChat.${name} must exist for these contracts to mean anything`);
  }
  await h.Chat.reconcileBgActiveTurn("A");
  assert.equal(h.state.probeUrls.length, 1, "one reconcile = exactly one probe");
  assert.match(h.state.probeUrls[0], /\/api\/v1\/chat\/active\/A$/, "the probe must address THIS conversation");
  await h.Chat.reconcileBgActiveTurn("B");
  assert.match(h.state.probeUrls[1] || "", /\/api\/v1\/chat\/active\/B$/, "a second conversation gets its OWN probe");
  // The REAL strip module must expose what the chat module reaches for; a silently absent
  // noteClock would make the clock contracts (C7/C7b) unprovable rather than failing.
  const real = loadChat({ realTurnStatus: true });
  for (const name of ["begin", "resume", "end", "clear", "noteClock"]) {
    assert.equal(typeof real.tsApi[name], "function", `AkanaTurnStatus.${name} must exist`);
  }
});

const BG = (cid) => ({ type: "turn_active", conversation_id: cid, source: "background" });
const BG_DONE = (cid, status = "ok") => ({ type: "turn_completed", conversation_id: cid, status, source: "background" });
const USER_DONE = (cid, status = "ok") => ({ type: "turn_completed", conversation_id: cid, status, source: "user" });

await check("B1 the user's own turn completing must NOT erase a live background job's strip", async () => {
  const h = loadChat();
  h.setDisplayed("A");
  await h.Chat.onTurnActiveRemote("A", BG("A"));
  assert.deepEqual(h.ts.resume, ["A"], "the background job shows the working strip");
  // The user asks a quick question in the SAME chat; its reply finishes in seconds.
  await h.Chat.onTurnCompletedRemote("A", USER_DONE("A"));
  assert.equal(h.ts.end, 0, "the user's own completion must not end the background strip");
  assert.deepEqual(h.ts.clear, [], "…nor drop the job's retained clock");
  h.ts.resume.length = 0;
  h.Chat.maybeShowBgWorking("A"); // switch away and back
  assert.deepEqual(h.ts.resume, ["A"], "the still-running job must be restorable after the user's turn ended");
  // Its OWN completion retires it.
  await h.Chat.onTurnCompletedRemote("A", BG_DONE("A"));
  assert.ok(h.ts.end >= 1, "the background completion ends the strip");
  assert.ok(h.ts.clear.includes("A"), "…and drops the finished job's clock");
  h.ts.resume.length = 0;
  h.Chat.maybeShowBgWorking("A");
  assert.deepEqual(h.ts.resume, [], "a finished job must not be resurrected");
});

await check("B2 the marker is a COUNT: two concurrent background turns need two completions", async () => {
  const h = loadChat();
  h.setDisplayed("A");
  await h.Chat.onTurnActiveRemote("A", BG("A"));
  await h.Chat.onTurnActiveRemote("A", BG("A"));
  await h.Chat.onTurnCompletedRemote("A", BG_DONE("A"));
  h.ts.resume.length = 0;
  h.Chat.maybeShowBgWorking("A");
  assert.deepEqual(h.ts.resume, ["A"], "one of two jobs finished → the strip must stay");
  assert.deepEqual(h.ts.clear, [], "the second job's clock must not be dropped");
  await h.Chat.onTurnCompletedRemote("A", BG_DONE("A"));
  h.ts.resume.length = 0;
  h.Chat.maybeShowBgWorking("A");
  assert.deepEqual(h.ts.resume, [], "the last completion retires the indicator");
});

await check("B3 an UNSTAMPED / user-source turn_active never drives the background indicator", async () => {
  const h = loadChat();
  h.setDisplayed("A");
  await h.Chat.onTurnActiveRemote("A", { type: "turn_active", conversation_id: "A" }); // no source
  h.ts.resume.length = 0;
  h.Chat.maybeShowBgWorking("A");
  assert.deepEqual(h.ts.resume, [], "a missing source is the user's own turn (fail quiet)");
  await h.Chat.onTurnActiveRemote("A", { type: "turn_active", conversation_id: "A", source: "user" });
  h.ts.resume.length = 0;
  h.Chat.maybeShowBgWorking("A");
  assert.deepEqual(h.ts.resume, [], "source:'user' is already reflected by local stream state");
});

await check("B4 a NON-displayed conversation: only a background completion retires the marker + toasts", async () => {
  const h = loadChat();
  h.setDisplayed("A");
  await h.Chat.onBackgroundTurnActive("B", BG("B"));
  await h.Chat.onBackgroundTurnCompleted("B", USER_DONE("B"));
  assert.deepEqual(h.ts.clear, [], "the user's own turn in B must not drop B's job clock");
  assert.deepEqual(h.state.toasts, [], "…and must not celebrate an unrelated turn as a background result");
  await h.Chat.onBackgroundTurnCompleted("B", BG_DONE("B"));
  assert.ok(h.ts.clear.includes("B"), "the background completion drops B's clock");
  assert.ok(h.state.toasts.some(([m]) => String(m).includes("bg_response_ready")), "…and toasts the ready result");
  h.setDisplayed("B");
  h.ts.resume.length = 0;
  h.Chat.maybeShowBgWorking("B");
  assert.deepEqual(h.ts.resume, [], "opening B later must not resurrect the finished job");
});

await check("B5 a FAILED background turn is not toasted as a ready result", async () => {
  const h = loadChat();
  h.setDisplayed("A");
  await h.Chat.onBackgroundTurnActive("B", BG("B"));
  await h.Chat.onBackgroundTurnCompleted("B", BG_DONE("B", "error"));
  assert.deepEqual(h.state.toasts, [], "status != ok must not toast");
});

// ── reconciliation with server truth (reload-restore-4 / -5) ────────────────
await check("C1 a missed turn_completed is reconciled away (no phantom working strip)", async () => {
  // The server restarted mid-job / the WS missed the frame: the chat is idle now.
  const h = loadChat({ activity: { A: { status: 204 } } });
  await h.settle();
  h.setDisplayed("A");
  assert.equal(typeof h.Chat.reconcileBgActiveTurn, "function", "the reconcile seam must be exposed");
  await h.Chat.onTurnActiveRemote("A", BG("A"));
  await h.Chat.reconcileBgActiveTurn("A");
  assert.ok(h.ts.end >= 1, "the phantom strip must come down");
  assert.ok(h.ts.clear.includes("A"), "…and its dead clock must be dropped");
  h.ts.resume.length = 0;
  h.Chat.maybeShowBgWorking("A");
  assert.deepEqual(h.ts.resume, [], "re-opening the chat must not resurrect the phantom");
});

await check("C2 after F5 a still-running background job becomes visible again", async () => {
  // The engine job survived the reload; the probe reports it (no follower buffer → 202).
  const h = loadChat({
    activity: { A: { status: 202, body: { running: true, kind: "background", started_at: 1 } } },
  });
  await h.settle(); // the page-load probe ran before the restored chat was displayed
  h.setDisplayed("A");
  await h.Chat.reconcileBgActiveTurn("A");
  assert.deepEqual(h.ts.resume, ["A"], "the live job's strip must be rebuilt from the server");
  assert.ok(h.state.archiveActivity.includes("A"), "…and the sidebar activity badge refreshed");
});

await check("C3 an unreadable answer → NO-OP (never erase a job we cannot verify)", async () => {
  const h = loadChat({ activity: { A: { status: 503 } } });
  await h.settle();
  h.setDisplayed("A");
  await h.Chat.onTurnActiveRemote("A", BG("A"));
  await h.Chat.reconcileBgActiveTurn("A");
  h.ts.resume.length = 0;
  h.Chat.maybeShowBgWorking("A");
  assert.deepEqual(h.ts.resume, ["A"], "a 5xx must leave the marker intact");
});

await check("C4 a live USER turn does not prove the background job ended", async () => {
  // While the user's own turn holds the registry slot the probe reports THAT turn (200),
  // which says nothing about the schedule job running alongside it.
  const h = loadChat({ activity: { A: { status: 200 } } });
  await h.settle();
  h.setDisplayed("A");
  await h.Chat.onTurnActiveRemote("A", BG("A"));
  await h.Chat.reconcileBgActiveTurn("A");
  assert.deepEqual(h.ts.clear, [], "a live foreground turn must not retire the background marker");
  h.ts.resume.length = 0;
  h.Chat.maybeShowBgWorking("A");
  assert.deepEqual(h.ts.resume, ["A"], "the job's strip must survive");
});

await check("C5 a job announced WHILE the probe was in flight is not erased by it", async () => {
  const h = loadChat({ activity: { A: { status: 204 } } });
  await h.settle();
  h.setDisplayed("A");
  const pending = h.Chat.reconcileBgActiveTurn("A"); // probe answered "idle" as of NOW
  await h.Chat.onTurnActiveRemote("A", BG("A")); // …the job starts a moment later
  await pending;
  h.ts.resume.length = 0;
  h.Chat.maybeShowBgWorking("A");
  assert.deepEqual(h.ts.resume, ["A"], "a stale probe must not erase a newer turn_active");
});

await check("C6 an idle reconcile must not kill the strip of the user's OWN live turn", async () => {
  // Both real modules, because this is where two guards meet: endBgWorking gates
  // AkanaTurnStatus.end() on !chatInFlight, but clear() force-ends whenever it drops the
  // PAINTED conversation's clock — so retiring a STALE background marker took the live
  // foreground turn's strip down behind that guard's back. setPhase cannot bring it back
  // (`if (!active) return`), so the turn ran to completion with no indicator at all.
  const h = loadChat({ activity: { A: { status: 204 } }, realTurnStatus: true });
  await h.settle(); // the page-load probe (nothing running yet)
  h.setDisplayed("A");
  // A background job whose turn_completed frame never arrived (server restart / WS blip).
  await h.Chat.onTurnActiveRemote("A", BG("A"));
  h.gateProbes(); // the conversation-open reconcile hits a slow server…
  const reconciled = h.Chat.reconcileBgActiveTurn("A");
  h.holdStream();
  const sent = h.Chat.submitVoiceText("hello"); // …meanwhile the user sends their own message
  await h.settle();
  assert.equal(h.stripView().hidden, false, "precondition: the user's live turn paints the strip");
  h.releaseProbes(); // the probe finally answers 204 — true, but only about the stale marker
  await reconciled;
  assert.equal(
    h.stripView().hidden,
    false,
    "retiring a stale BACKGROUND marker took down the strip of the user's LIVE foreground turn",
  );
  assert.match(h.stripView().text, /·/, "…and its clock must still be rendered, not blanked");
  h.releaseStream();
  await sent.catch(() => {});
});

await check("C6b with NO live user turn the same reconcile still retires the phantom strip", async () => {
  const h = loadChat({ activity: { A: { status: 204 } }, realTurnStatus: true });
  await h.settle();
  h.setDisplayed("A");
  await h.Chat.onTurnActiveRemote("A", BG("A"));
  assert.equal(h.stripView().hidden, false, "precondition: the background job paints the strip");
  await h.Chat.reconcileBgActiveTurn("A");
  assert.equal(h.stripView().hidden, true, "an idle answer with no foreground turn must take the phantom down");
});

await check("C7 the 202's started_at seeds the clock — a job running for minutes must not read 0:00", async () => {
  // After F5 the page has NO memory of the job, so the strip's only source for "working
  // since…" is the probe's own answer. Discarding started_at made every visit to the chat
  // restart the elapsed at 0:00 for a job the server said had been running for 2:05.
  const started = Date.now() - 125_000;
  const h = loadChat({
    activity: { A: { status: 202, body: { running: true, kind: "background", started_at: started } } },
    realTurnStatus: true,
  });
  await h.settle();
  h.setDisplayed("A");
  await h.Chat.reconcileBgActiveTurn("A");
  assert.equal(h.stripView().hidden, false, "precondition: the rebuilt marker paints the strip");
  assert.match(
    h.stripView().text,
    /2:0[45]/,
    `the strip must show the job's REAL elapsed, not a fresh clock. got=${JSON.stringify(h.stripView().text)}`,
  );
});

await check("C7b a nonstreaming turn must not MASK a background job running in the same chat", async () => {
  // The route reports ONE kind by priority (nonstreaming ahead of background), so with a
  // Telegram/blocking/voice turn live in the same conversation the job is invisible under
  // `kind`. After F5 (marker map empty) that answer left the reconcile in NEITHER branch:
  // nothing rebuilt, nothing cleared, and the job's strip never came back.
  const bgStarted = Date.now() - 90_000; // the JOB's clock — 1:30
  const h = loadChat({
    activity: {
      A: {
        status: 202,
        body: {
          running: true,
          kind: "nonstreaming", // the OTHER turn is what `kind` names…
          started_at: Date.now() - 3_000, // …and `started_at` is ITS clock
          background: true, // …while the independent flag reports the job
          background_started_at: bgStarted,
        },
      },
    },
    realTurnStatus: true,
  });
  await h.settle();
  h.setDisplayed("A");
  await h.Chat.reconcileBgActiveTurn("A");
  assert.equal(h.stripView().hidden, false, "the background job's strip must be rebuilt");
  assert.ok(h.state.archiveActivity.includes("A"), "…and the sidebar activity badge refreshed");
  assert.match(
    h.stripView().text,
    /1:3[01]/,
    `the clock must come from the JOB's own stamp, not the nonstreaming turn's. got=${JSON.stringify(h.stripView().text)}`,
  );
});

await check("C7c a nonstreaming turn ALONE still drives no background indicator", async () => {
  // Per the turn-lifecycle contract kind:"nonstreaming" is the user's OWN voice/blocking/
  // connector turn — never background work.
  const h = loadChat({
    activity: {
      A: { status: 202, body: { running: true, kind: "nonstreaming", started_at: Date.now() - 3_000 } },
    },
    realTurnStatus: true,
  });
  await h.settle();
  h.setDisplayed("A");
  await h.Chat.reconcileBgActiveTurn("A");
  assert.equal(h.stripView().hidden, true, "the user's own turn must not light the background strip");
  assert.deepEqual(h.ts.clear, [], "…and a running turn must not retire anything either");
});

await check("C8 an idle answer DECREMENTS the marker — one 204 must not erase two overlapping jobs", async () => {
  // The other half of this is server-side (background_activity.py registers with
  // setdefault/pop and has no refcount, so the FIRST of two overlapping jobs to finish
  // already makes /chat/active answer 204 while the second still runs). Decrementing is
  // correct regardless of what the server reports.
  const h = loadChat({ activity: { A: { status: 204 } } });
  await h.settle();
  h.setDisplayed("A");
  await h.Chat.onTurnActiveRemote("A", BG("A"));
  await h.Chat.onTurnActiveRemote("A", BG("A"));
  await h.Chat.reconcileBgActiveTurn("A");
  assert.equal(h.ts.end, 0, "one idle answer must not retire an indicator two jobs are holding");
  h.ts.resume.length = 0;
  h.Chat.maybeShowBgWorking("A", { reconcile: false });
  assert.deepEqual(h.ts.resume, ["A"], "the second job keeps the strip");
  await h.Chat.reconcileBgActiveTurn("A");
  h.ts.resume.length = 0;
  h.Chat.maybeShowBgWorking("A", { reconcile: false });
  assert.deepEqual(h.ts.resume, [], "the second idle answer retires the last marker");
});

await check("C9 a completion that lands WHILE a 202 probe is in flight must not be re-seeded by it", async () => {
  // The mark epoch protects a marker CREATED during a probe; nothing protected a marker
  // DROPPED during one — the 202 (taken before the job ended) re-created it and the
  // completion that could have retired it was already consumed → a phantom ticking strip.
  const h = loadChat({
    activity: { A: { status: 202, body: { running: true, kind: "background", started_at: Date.now() - 1000 } } },
  });
  await h.settle();
  h.setDisplayed("A");
  await h.Chat.onTurnActiveRemote("A", BG("A"));
  h.gateProbes();
  const probing = h.Chat.reconcileBgActiveTurn("A");
  await h.Chat.onTurnCompletedRemote("A", BG_DONE("A")); // the job finishes meanwhile
  h.releaseProbes();
  await probing;
  h.ts.resume.length = 0;
  h.Chat.maybeShowBgWorking("A", { reconcile: false });
  assert.deepEqual(h.ts.resume, [], "a 202 older than the completion must not resurrect the marker");
});

await check("C9b a completion in ANOTHER chat must not suppress this one's rebuild", async () => {
  // The drop epoch is per conversation: B's job ending says nothing about A's, and a shared
  // counter would swallow the rebuild A's probe legitimately asked for.
  const h = loadChat({
    activity: { A: { status: 202, body: { running: true, kind: "background", started_at: Date.now() - 1000 } } },
  });
  await h.settle();
  h.setDisplayed("A");
  await h.Chat.onBackgroundTurnActive("B", BG("B"));
  h.gateProbes();
  const probing = h.Chat.reconcileBgActiveTurn("A");
  await h.Chat.onBackgroundTurnCompleted("B", BG_DONE("B")); // B's job ends meanwhile
  h.releaseProbes();
  await probing;
  h.ts.resume.length = 0;
  h.Chat.maybeShowBgWorking("A", { reconcile: false });
  assert.deepEqual(h.ts.resume, ["A"], "A's still-running job must be rebuilt from its own answer");
});

await check("C10 a caller arriving during a probe must not be handed an answer older than its trigger", async () => {
  // The open-late case: a sweep probe is in flight and will answer 204 (taken before the
  // job started); the job starts; the user opens the chat. Joining the in-flight probe
  // hands the seed a STALE 204 → no marker, no strip, and nothing heals it until the chat
  // is re-opened by hand.
  const h = loadChat({ activity: { A: { status: 204 } } });
  await h.settle();
  h.setDisplayed("A");
  h.gateProbes();
  const sweep = h.Chat.reconcileBgActiveTurn("A");
  h.setActivity("A", 202, { running: true, kind: "background", started_at: Date.now() - 2000 });
  const openLate = h.Chat.reconcileBgActiveTurn("A"); // the user opens the chat AFTER the job started
  h.releaseProbes();
  await Promise.all([sweep, openLate]);
  h.ts.resume.length = 0;
  h.Chat.maybeShowBgWorking("A", { reconcile: false });
  assert.deepEqual(h.ts.resume, ["A"], "the open-late caller must get an answer newer than its own trigger");
});

await check("C11 the strip is handed BACK to a still-live background job when the user's own turn ends", async () => {
  const h = loadChat({
    activity: { A: { status: 202, body: { running: true, kind: "background", started_at: Date.now() - 30_000 } } },
    realTurnStatus: true,
  });
  await h.settle();
  h.setDisplayed("A");
  await h.Chat.onTurnActiveRemote("A", BG("A"));
  assert.equal(h.stripView().hidden, false, "precondition: the job's strip is up");
  h.holdStream();
  const sent = h.Chat.submitVoiceText("quick question"); // the user asks something in the same chat
  await h.settle();
  h.releaseStream();
  await sent;
  await h.settle();
  assert.equal(
    h.stripView().hidden,
    false,
    "the job kept running — the user's turn ending must hand the strip back, not tear it down",
  );
});

await check("C11b with NO background work the user's turn ending leaves the composer idle", async () => {
  const h = loadChat({ activity: { A: { status: 204 } }, realTurnStatus: true });
  await h.settle();
  h.setDisplayed("A");
  h.holdStream();
  const sent = h.Chat.submitVoiceText("hi");
  await h.settle();
  h.releaseStream();
  await sent;
  await h.settle();
  assert.equal(h.stripView().hidden, true, "nothing is working — the strip must stay down");
});

await check("C12 a BACKGROUND completion must not drive the stalled-stream rescue on the user's turn", async () => {
  // reconcileServerCompletedTurn ABORTS the stream it is given. A background completion
  // says nothing about the user's own stream — and the schedule/settle paths broadcast it
  // with NO assistant_turn_id, so the transport's mismatch guard cannot even fire: the
  // user's answer froze mid-sentence and the composer flipped back to SEND.
  const h = loadChat();
  h.setDisplayed("A");
  h.state.streamActive = true;
  h.Chat.setChatInFlight(true);
  await h.Chat.onTurnCompletedRemote("A", BG_DONE("A"));
  assert.deepEqual(h.state.rescueCalls, [], "a background completion must never abort the user's live stream");
});

await check("C12b the user's OWN completion still rescues a stalled stream", async () => {
  const h = loadChat();
  h.setDisplayed("A");
  h.state.streamActive = true;
  h.Chat.setChatInFlight(true);
  await h.Chat.onTurnCompletedRemote("A", { ...USER_DONE("A"), assistant_turn_id: "t9" });
  assert.deepEqual(h.state.rescueCalls, [["A", "t9"]], "the safety net for the user's own stalled turn must survive");
});

// ═════════════════════════════════════════════════════════════════════════════
// D. voice conversation mode is pinned to the chat the scene shows
// ═════════════════════════════════════════════════════════════════════════════
await check("D1 a notification click retargeting the displayed chat must not steal the next utterance", async () => {
  const h = loadChat({ voiceMode: true });
  h.setDisplayed("A");
  await h.Chat.submitVoiceText("first question");
  assert.deepEqual(
    h.state.streamCalls.map((c) => c.conv), ["A"],
    "precondition: the first utterance goes to the displayed chat",
  );
  // A background result lands in B → the desktop notification click switches the
  // displayed conversation underneath the fullscreen voice scene.
  h.setDisplayed("B");
  await h.Chat.submitVoiceText("second question");
  assert.ok(h.state.switches.includes("A"), "the voice turn must re-bind to the pinned conversation");
  assert.equal(
    h.state.streamCalls[1].conv, "A",
    "the utterance must be submitted in the chat the voice scene is showing, not the retargeted one",
  );
});

await check("D2 leaving voice mode drops the pin (a new session binds to its own chat)", async () => {
  const h = loadChat({ voiceMode: true });
  h.setDisplayed("A");
  await h.Chat.submitVoiceText("in A");
  h.bus.emit("voice:mode:exit", {});
  h.state.voiceMode = false;
  h.setDisplayed("B");
  h.state.voiceMode = true; // a NEW session, started from chat B
  await h.Chat.submitVoiceText("in B");
  assert.deepEqual(h.state.switches, [], "the stale pin must not drag the new session back to A");
  assert.equal(h.state.streamCalls[1].conv, "B", "the new session's turn stays in B");
});

await check("D3 outside conversation mode the voice turn follows the displayed chat", async () => {
  const h = loadChat({ voiceMode: false });
  h.setDisplayed("A");
  await h.Chat.submitVoiceText("single shot");
  h.setDisplayed("B");
  await h.Chat.submitVoiceText("another");
  assert.deepEqual(h.state.switches, [], "single-shot voice must never re-bind the conversation");
  assert.deepEqual(h.state.streamCalls.map((c) => c.conv), ["A", "B"]);
});

// ── Summary ─────────────────────────────────────────────────────────────────
if (failures) {
  console.error(`\nhunt5_chat_core: ${passed} passed, ${failures} FAILED`);
  process.exit(1);
}
console.log(`hunt5_chat_core: ${passed} chat-core contracts passed ✓`);
process.exit(0);
