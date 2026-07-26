/**
 * hunt-6 · front-end contracts — the composer's SEND/STOP re-entrancy and the
 * language-reach of the chat surface. node-vm, real static JS, no DOM library.
 *
 * Contracts locked here:
 *
 *   A. Stop→send is not re-entrant (akana-chat.js)
 *      · POST /chat/active/{id}/cancel waits for the cancelled turn to unwind (seconds),
 *        and across that window the composer still holds the draft and the button still
 *        reads Stop. forceImmediate is exempt from BOTH send guards (#16 new-conv and the
 *        per-conv busy latch) and the setup latch is only taken AFTER the await, so a second
 *        click sent the SAME message twice: two echoes, two persisted user turns, two
 *        billed LLM turns. Exactly ONE send per typed message.
 *      · the latch is per CONVERSATION (a Stop→send in another chat is not collateral) and
 *        is released on EVERY exit path — including a cancel that rejects, or a single
 *        network error would disable Stop→send for the rest of the session.
 *
 *   B. A Stop click always stops (akana-chat.js)
 *      · Stop-with-a-draft is routed through the form; when the send is then rejected
 *        (nothing to send / over the provider's per-message attachment budget) the running
 *        turn used to keep streaming with only an attachment toast as feedback,
 *      · and the force-immediate latch leaked: the NEXT ordinary Enter ran as forceImmediate
 *        and silently aborted + server-cancelled whatever turn was running in the displayed
 *        conversation, skipping the single-turn guards.
 *
 *   C. Stop is never a silent no-op (akana-chat.js)
 *      · the button is forced into Stop mode by chatInFlight OR queueDepth > 0, but only a
 *        live stream can be cancelled: with a queued (202) message and no stream, clicking
 *        Stop did nothing at all — no abort, no cancel, no toast, no state change.
 *
 *   D. Runtime settings i18n coverage (akana-i18n-strings-runtime.js)
 *      · every non-hidden key in akana_server/runtime_settings/schema.py needs a
 *        runtime.<key>.label AND .desc with BOTH languages filled — the form falls back to
 *        the schema's English string, so a missing entry renders English rows inside an
 *        otherwise Turkish form (schedule_tools_enabled, whose description explains that the
 *        model may create reminders on the user's behalf, was one of them).
 *
 *   E. Wake-threshold default drift (web_ui/index.html · akana-voice-settings.js)
 *      · the slider's markup default must equal DEFAULTS["wake_threshold"], and a failed
 *        GET /voice/config must not leave a number on screen that was never verified.
 *
 *   F. Timestamps follow the APP language (akana-chat-render.js)
 *      · formatClock hardcoded "tr-TR", so every English user read the Turkish 24-hour form
 *        on every message forever — the one place the language picker did not reach.
 *
 *   G. Tool-card argument labels are resolved PER RENDER (akana-chat-render.js)
 *      · resolving them at script-eval time froze them in the pre-reconcile language: on a
 *        first visit in a fresh browser (empty localStorage → "en") every tool card kept
 *        English labels inside an otherwise Turkish UI until the next reload.
 *
 * Run: node tests/web/hunt6_frontend.harness.mjs
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

// ── fake DOM ────────────────────────────────────────────────────────────────
// `Element` is a real class so the render module's `node instanceof Element` guard accepts
// our nodes. Selector support is deliberately small: tag / .class / [attr="v"] / :not().
class Element {}
function makeEl(tag = "div") {
  const el = Object.create(Element.prototype);
  Object.assign(el, {
    tagName: String(tag).toUpperCase(),
    children: [],
    childNodes: [],
    dataset: {},
    attrs: {},
    _listeners: {},
    _text: "",
    style: { setProperty() {}, removeProperty() {} },
    id: "",
    value: "",
    title: "",
    type: "",
    innerHTML: "",
    hidden: false,
    disabled: false,
    parentNode: null,
  });
  el.classList = {
    _s: new Set(),
    add(...c) { c.forEach((x) => this._s.add(x)); },
    remove(...c) { c.forEach((x) => this._s.delete(x)); },
    toggle(c, on) { const w = on === undefined ? !this._s.has(c) : !!on; if (w) this._s.add(c); else this._s.delete(c); return w; },
    contains(c) { return this._s.has(c); },
  };
  Object.defineProperties(el, {
    textContent: {
      get() { return this._text; },
      set(v) { this._text = String(v); if (v === "") { this.children = []; this.childNodes = []; } },
    },
    className: {
      get() { return [...this.classList._s].join(" "); },
      set(v) { this.classList._s = new Set(String(v).split(/\s+/).filter(Boolean)); },
    },
    firstChild: { get() { return this.children[0] || null; } },
    isConnected: { get() { return true; } },
  });
  el.setAttribute = function (k, v) { this.attrs[k] = String(v); };
  el.getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
  el.removeAttribute = function (k) { delete this.attrs[k]; };
  el.appendChild = function (c) { this.children.push(c); this.childNodes = this.children; c.parentNode = this; return c; };
  el.append = function (...cs) { cs.forEach((c) => this.appendChild(c)); };
  el.insertBefore = function (node, ref) {
    const i = this.children.indexOf(ref);
    if (i < 0) this.children.push(node);
    else this.children.splice(i, 0, node);
    this.childNodes = this.children;
    node.parentNode = this;
    return node;
  };
  el.remove = function () {
    if (!this.parentNode) return;
    const i = this.parentNode.children.indexOf(this);
    if (i >= 0) this.parentNode.children.splice(i, 1);
    this.parentNode = null;
  };
  el.addEventListener = function (t, f) { (this._listeners[t] ||= []).push(f); };
  el.removeEventListener = function () {};
  el.dispatchEvent = function () { return true; };
  /** Fire the handlers the module really registered (no synthetic re-implementation). */
  el.fire = function (type, evt = {}) {
    for (const fn of this._listeners[type] || []) fn({ preventDefault() {}, stopPropagation() {}, ...evt });
  };
  el.click = function () { this.fire("click"); };
  el.focus = function () {};
  el.select = function () {};
  el.requestSubmit = function () { this.fire("submit"); };
  el.contains = function (n) { let c = n; while (c) { if (c === this) return true; c = c.parentNode; } return false; };
  el.closest = function (sel) { let n = this; while (n) { if (selMatch(n, sel)) return n; n = n.parentNode; } return null; };
  el.querySelector = function (sel) { return findOne(this, sel); };
  el.querySelectorAll = function (sel) { return findAll(this, sel); };
  el.matches = function (sel) { return selMatch(this, sel); };
  return el;
}

function selMatch(el, sel) {
  if (!el || !el.classList) return false;
  const tokens = String(sel).match(/^[a-z][\w-]*|\.[\w-]+|\[[\w-]+(?:="[^"]*")?\]|:not\([^)]*\)/gi) || [];
  if (!tokens.length) return false;
  for (const t of tokens) {
    if (t.startsWith(":not(")) {
      if (selMatch(el, t.slice(5, -1).trim())) return false;
    } else if (t.startsWith(".")) {
      if (!el.classList.contains(t.slice(1))) return false;
    } else if (t.startsWith("[")) {
      const m = t.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/);
      if (!m) return false;
      const attr = m[1];
      let val;
      if (attr.startsWith("data-")) {
        const camel = attr.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
        val = el.dataset ? el.dataset[camel] : undefined;
      } else val = el.attrs ? el.attrs[attr] : undefined;
      if (m[2] === undefined) { if (val == null) return false; }
      else if (String(val) !== m[2]) return false;
    } else if (String(el.tagName).toLowerCase() !== t.toLowerCase()) return false;
  }
  return true;
}
function walk(el, fn) { for (const c of el.children || []) { fn(c); walk(c, fn); } }
function findOne(root, sel) { let out = null; walk(root, (n) => { if (!out && selMatch(n, sel)) out = n; }); return out; }
function findAll(root, sel) { const out = []; walk(root, (n) => { if (selMatch(n, sel)) out.push(n); }); return out; }
/** Every string the card carries, wherever it landed (textContent / innerHTML / dataset). */
function allText(root) {
  const parts = [];
  const visit = (n) => {
    parts.push(n._text || "", n.innerHTML || "");
    for (const v of Object.values(n.dataset || {})) parts.push(String(v));
    for (const c of n.children || []) visit(c);
  };
  visit(root);
  return parts.join("\n");
}

// ═════════════════════════════════════════════════════════════════════════════
// A/B/C. akana-chat.js — the composer's send/stop paths
// ═════════════════════════════════════════════════════════════════════════════
function loadChat() {
  const els = {
    log: makeEl("div"),
    "log-scroll": makeEl("div"),
    "chat-form": makeEl("form"),
    msg: makeEl("textarea"),
    "btn-send": makeEl("button"),
    "composer-attachments": makeEl("div"),
    "log-empty": makeEl("div"),
    // value "" → activeProviderName falls through to /system/status, which answers with no
    // provider → DEFAULT_ATTACH_LIMITS (8 images per message).
    "llm-provider": makeEl("select"),
  };
  const doc = {
    readyState: "complete",
    body: makeEl("body"),
    getElementById: (id) => els[id] || null,
    createElement: (t) => makeEl(t),
    addEventListener: () => {},
    querySelector: () => null,
    querySelectorAll: () => [],
  };

  const state = {
    displayed: "",
    streamingConvs: new Set(),
    echoes: [],
    streamCalls: [],
    cancelCalls: [],
    abortCalls: [],
    toasts: [],
    queueFetches: [],
    // The cancel endpoint is SLOW by contract (it waits for the cancelled turn's finally).
    // Every scenario controls exactly when it answers; nothing is instant by default.
    cancelGate: null,
    releaseCancel: null,
    cancelRejects: false,
  };

  const threadsStub = {
    conversationIdForMemory: () => state.displayed,
    chatActiveThread: () => ({ conversationId: state.displayed, messages: [] }),
    switchChatConversation: async () => {},
    recordPendingUserMessage: () => {},
    tryHandleChatDeleteCommand: () => false,
    chatProfile: () => "cursor",
    newChatThreadId: () => "t",
    chatStartNewThread: () => {},
    wireArchiveChrome: () => {},
    wireThreadBar: () => {},
    getChatStore: () => ({ threads: {}, activeByProfile: {} }),
    chatRestoreActiveThread: () => {},
    loadChatArchiveList: () => {},
    openArchiveDrawer: () => {},
    closeArchiveDrawer: () => {},
    setConversationId: () => {},
    recordErrorForConversation: () => true,
    chatRecordMessage: () => {},
    syncChatThreadBar: () => {},
    refreshActiveConversationMeta: () => {},
    refreshArchiveActivity: () => {},
    refreshConversationLogAfterTurn: async () => {},
    syncConversationLogFromServer: async () => {},
    getChatArchiveItems: () => [],
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
      return {};
    },
    isConversationStreamActive: (id) => state.streamingConvs.has(String(id || "")),
    abortActiveChatStream: (id) => { state.abortCalls.push(String(id ?? "")); },
    cancelActiveTurnOnServer: async (id) => {
      state.cancelCalls.push(String(id ?? ""));
      if (state.cancelGate) await state.cancelGate;
      if (state.cancelRejects) throw new Error("cancel endpoint unreachable");
      return true;
    },
    humanizeChatError: (e) => String(e),
    ensureConversationIdReady: async () => state.displayed,
    fetchConversationTurnsFromServer: async () => ({ status: 404, turns: null }),
    setForegroundConversation: () => {},
    abortConversationTurnsFetch: () => {},
    resumeActiveTurn: async () => true,
    probeActiveTurn: async () => null,
    reconcileServerCompletedTurn: async () => false,
    isForegroundTurnFinalized: () => false,
    reattachLiveRow: () => false,
    activeStreamTurnId: () => null,
  };

  const fetchImpl = async (url) => {
    const u = String(url);
    if (u.includes("/chat/queue/")) {
      state.queueFetches.push(u);
      // Answer with the depth the module already believes: a refresh must not be what
      // clears the queue in these scenarios (that would hide the toast contract).
      return { ok: true, status: 200, json: async () => ({ depth: 1 }) };
    }
    if (u.includes("/system/status")) return { ok: true, status: 200, json: async () => ({}) };
    return { ok: false, status: 404, json: async () => ({}) };
  };

  const win = {
    AkanaI18n: { t: (k, p) => (p ? `${k}:${JSON.stringify(p)}` : k), getLanguage: () => "en" },
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
    AkanaTurnStatus: {
      mount: () => {}, begin: () => {}, resume: () => {}, end: () => {},
      clear: () => {}, noteClock: () => {}, setPhase: () => {}, isActive: () => false,
    },
    AkanaBus: { on: () => {}, emit: () => {} },
    AkanaVoice: { isConversationMode: () => false },
    document: doc,
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (h) => clearTimeout(h),
    setInterval: (fn, ms) => { const h = setInterval(fn, ms); h.unref?.(); return h; },
    clearInterval: (h) => clearInterval(h),
    fetch: fetchImpl,
    addEventListener: () => {},
  };
  win.window = win;
  const ctx = {
    console,
    document: doc,
    window: win,
    Element,
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
    appendUserMessage: (text) => { state.echoes.push({ text, conv: state.displayed }); return makeEl("div"); },
    appendSystemNotice: () => {},
    updateEmptyState: () => {},
    resizeComposer: () => {},
    setOrb: () => {},
    setComposerHint: () => {},
    stickToBottomIfFollowing: () => {},
    scrollLogToBottom: () => {},
    scrollNewTurnToTop: () => {},
    setLogLoading: () => {},
    showToast: (m, kind) => state.toasts.push([m, kind]),
    streamTtsParam: () => "",
    syncOrbWithVoice: () => {},
    updateSettingsHero: () => {},
    loadMemoryConversations: () => {},
    shortConversationId: (id) => id || "none",
    closeSettings: () => {},
  });
  return {
    Chat,
    state,
    els,
    sendBtn: els["btn-send"],
    msg: els.msg,
    mode: () => els["btn-send"].dataset.mode,
    /** Put the composer in the state a live foreground turn leaves it in. */
    startStream: (convId) => {
      state.displayed = String(convId);
      state.streamingConvs.add(String(convId));
      Chat.setChatInFlight(true);
      Chat.setQueueDepth(0); // repaints the button from the module's own sync
    },
    endStream: (convId) => {
      state.streamingConvs.delete(String(convId));
      Chat.setChatInFlight(false);
      Chat.setQueueDepth(0);
    },
    /** Hold every cancel answer until releaseCancel() — the real endpoint waits seconds. */
    gateCancel: () => { state.cancelGate = new Promise((r) => { state.releaseCancel = r; }); },
    releaseCancel: () => { const r = state.releaseCancel; state.cancelGate = null; state.releaseCancel = null; r?.(); },
    settle: () => new Promise((r) => setTimeout(r, 0)),
  };
}

// ── Harness integrity (run FIRST) ───────────────────────────────────────────
// Every scenario below drives the composer through the listeners the MODULE registered.
// If it stops registering them (rename, refactor, an init that bails early) the clicks
// would land nowhere and each "must not happen twice" check would pass vacuously.
await check("H0 integrity — the module wires the composer and the stubs are really reached", async () => {
  const h = loadChat();
  assert.ok((h.sendBtn._listeners.click || []).length >= 1, "akana-chat.js must register the send button's click handler");
  assert.ok((h.els["chat-form"]._listeners.submit || []).length >= 1, "…and the form's submit handler");
  h.startStream("A");
  assert.equal(h.mode(), "stop", "a live foreground turn must put the button in Stop mode");
  // The stubs the negative checks rely on must actually be wired to the module.
  h.msg.value = "";
  h.sendBtn.fire("click");
  await h.settle();
  assert.deepEqual(h.state.abortCalls, ["A"], "a plain Stop must reach abortActiveChatStream");
  assert.deepEqual(h.state.cancelCalls, ["A"], "…and cancelActiveTurnOnServer");
});

await check("A1 two clicks during the server-cancel await send the message ONCE", async () => {
  const h = loadChat();
  h.startStream("A");
  h.msg.value = "second question";
  h.gateCancel(); // the cancel endpoint is unwinding the turn — seconds, nothing visible
  h.sendBtn.fire("click");
  await h.settle();
  // Precondition: the UI really does look dead across the window — that is WHY the user
  // clicks again. If any of this stops being true the scenario is no longer the bug.
  assert.equal(h.msg.value, "second question", "precondition: the composer is not cleared during the await");
  assert.equal(h.state.echoes.length, 0, "precondition: no bubble is echoed yet");
  assert.equal(h.mode(), "stop", "precondition: the button still reads Stop");
  h.sendBtn.fire("click"); // the user clicks the dead-looking button again
  await h.settle();
  h.releaseCancel();
  await h.settle();
  await h.settle();
  assert.deepEqual(
    h.state.echoes.map((e) => e.text),
    ["second question"],
    "one typed message must be echoed ONCE — a duplicated user turn is persisted history",
  );
  assert.deepEqual(
    h.state.streamCalls.map((c) => c.text),
    ["second question"],
    "…and sent ONCE — a second streamChat is a second answered, billed LLM turn",
  );
});

await check("A2 the block is per CONVERSATION — a Stop→send in another chat is not swallowed", async () => {
  const h = loadChat();
  h.startStream("A");
  h.msg.value = "in A";
  h.gateCancel();
  h.sendBtn.fire("click");
  await h.settle();
  // The user switches to B (also streaming) while A's cancel is still in flight.
  h.startStream("B");
  h.msg.value = "in B";
  h.sendBtn.fire("click");
  await h.settle();
  assert.deepEqual(
    h.state.cancelCalls,
    ["A", "B"],
    "B's own Stop→send must issue its own cancel — the re-entrancy latch is not global",
  );
  h.releaseCancel();
  await h.settle();
  await h.settle();
  assert.deepEqual(h.state.streamCalls.map((c) => c.text).sort(), ["in A", "in B"]);
});

await check("A3 the latch is released — a later Stop→send in the same chat still works", async () => {
  const h = loadChat();
  h.startStream("A");
  h.msg.value = "first";
  h.gateCancel();
  h.sendBtn.fire("click");
  await h.settle();
  h.releaseCancel();
  await h.settle();
  await h.settle();
  assert.deepEqual(h.state.streamCalls.map((c) => c.text), ["first"], "precondition: the first Stop→send went out");
  // A new turn is streaming; the user stops it with a new draft.
  h.startStream("A");
  h.msg.value = "second";
  h.sendBtn.fire("click");
  await h.settle();
  await h.settle();
  assert.deepEqual(
    h.state.streamCalls.map((c) => c.text),
    ["first", "second"],
    "the window is over — the next Stop→send must not be swallowed",
  );
});

await check("A4 a cancel that REJECTS still releases the latch (one network error ≠ dead Stop)", async () => {
  const h = loadChat();
  h.startStream("A");
  h.state.cancelRejects = true;
  h.msg.value = "during outage";
  h.sendBtn.fire("click");
  await h.settle();
  await h.settle();
  assert.deepEqual(h.state.streamCalls.map((c) => c.text), ["during outage"], "precondition: the send still happened");
  h.state.cancelRejects = false;
  h.startStream("A");
  h.msg.value = "after outage";
  h.sendBtn.fire("click");
  await h.settle();
  await h.settle();
  assert.deepEqual(
    h.state.streamCalls.map((c) => c.text),
    ["during outage", "after outage"],
    "a rejected cancel must not leave Stop→send latched shut for the rest of the session",
  );
});

// ── B. a Stop click always stops ────────────────────────────────────────────
function seedImages(h, n) {
  for (let i = 0; i < n; i += 1) h.Chat._test.seedPendingAttachment({ id: `img${i}`, kind: "image", previewUrl: "" });
}

await check("B1 a Stop swallowed by the attachment-limit early return still stops the stream", async () => {
  const h = loadChat();
  h.startStream("A");
  seedImages(h, 9); // default per-message budget is 8 images
  h.msg.value = "and one more thing";
  h.sendBtn.fire("click");
  await h.settle();
  await h.settle();
  assert.ok(
    h.state.toasts.some(([m]) => String(m).includes("attach_limit_images")),
    `precondition: the send is rejected by the attachment budget. toasts=${JSON.stringify(h.state.toasts)}`,
  );
  assert.deepEqual(h.state.streamCalls, [], "precondition: nothing is sent");
  assert.deepEqual(h.state.abortCalls, ["A"], "the user pressed Stop — the running stream must be aborted");
  assert.deepEqual(h.state.cancelCalls, ["A"], "…and the server turn cancelled, not left running");
});

await check("B2 a rejected Stop does not leak force-immediate into the next ordinary send", async () => {
  const h = loadChat();
  h.startStream("A");
  seedImages(h, 9);
  h.msg.value = "and one more thing";
  h.sendBtn.fire("click");
  await h.settle();
  await h.settle();
  h.Chat.consumePendingFileIds(); // the user removes the extra images
  h.endStream("A");
  h.state.abortCalls.length = 0;
  h.state.cancelCalls.length = 0;
  // A turn is running in the displayed conversation that is NOT this client's stream
  // (a background job, or a turn resumed in another tab). An ordinary Enter must not kill it.
  h.msg.value = "an ordinary later message";
  h.els["chat-form"].fire("submit");
  await h.settle();
  await h.settle();
  assert.deepEqual(h.state.streamCalls.map((c) => c.text), ["an ordinary later message"], "precondition: it is sent");
  assert.deepEqual(h.state.abortCalls, [], "a plain Enter must not abort anything");
  assert.deepEqual(
    h.state.cancelCalls,
    [],
    "…and must not server-cancel the displayed conversation's running turn behind the user's back",
  );
});

await check("B3 an ordinary Stop-with-draft is unaffected (the latch still reaches submitChatText)", async () => {
  const h = loadChat();
  h.startStream("A");
  h.msg.value = "stop and ask this instead";
  h.sendBtn.fire("click");
  await h.settle();
  await h.settle();
  assert.deepEqual(h.state.cancelCalls, ["A"], "Stop→send must still cancel the running turn first");
  assert.deepEqual(h.state.streamCalls.map((c) => c.text), ["stop and ask this instead"], "…then send the draft");
});

// ── C. Stop is never a silent no-op ─────────────────────────────────────────
await check("C1 Stop with a queued message and no live stream says why it cannot stop", async () => {
  const h = loadChat();
  h.state.displayed = "A";
  h.Chat.setQueueDepth(1); // the 202 the transport recorded for the user's second message
  assert.equal(h.mode(), "stop", "precondition: the queue alone forces the button into Stop mode");
  h.msg.value = "";
  h.sendBtn.fire("click");
  await h.settle();
  assert.ok(
    h.state.toasts.length > 0,
    "Stop is the emergency brake — with nothing to cancel it must SAY so, not do nothing",
  );
  assert.ok(
    h.state.toasts.some(([m]) => String(m).includes("stop_nothing_running")),
    `…with the queue-specific explanation. toasts=${JSON.stringify(h.state.toasts)}`,
  );
  assert.ok(
    h.state.queueFetches.some((u) => u.includes("/chat/queue/A")),
    "…and re-read the queue, so a queue that already drained releases the button by itself",
  );
});

await check("C2 with a LIVE stream Stop still cancels silently (no spurious queue toast)", async () => {
  const h = loadChat();
  h.startStream("A");
  h.Chat.setQueueDepth(1); // a queued follow-up behind the running turn
  h.msg.value = "";
  h.sendBtn.fire("click");
  await h.settle();
  assert.deepEqual(h.state.abortCalls, ["A"], "the live stream is what Stop cancels");
  assert.deepEqual(
    h.state.toasts.filter(([m]) => String(m).includes("stop_nothing_running")),
    [],
    "a Stop that DID stop something must not also claim nothing was running",
  );
});

// ═════════════════════════════════════════════════════════════════════════════
// D. runtime-settings i18n coverage (schema ↔ dictionary drift guard)
// ═════════════════════════════════════════════════════════════════════════════
function runtimeDict() {
  const ctx = { window: {} };
  vm.runInNewContext(read("web_ui/static/akana-i18n-strings-runtime.js"), ctx);
  return ctx.window.AkanaI18nStrings || {};
}

/** Non-hidden runtime keys, read out of the backend schema (the single source of truth). */
function schemaKeys({ includeHidden = false } = {}) {
  const src = read("akana_server/runtime_settings/schema.py");
  const marker = "RuntimeSettingSpec(";
  const starts = [];
  for (let i = src.indexOf(marker); i >= 0; i = src.indexOf(marker, i + 1)) starts.push(i);
  const out = [];
  for (let i = 0; i < starts.length; i += 1) {
    const chunk = src.slice(starts[i], i + 1 < starts.length ? starts[i + 1] : src.length);
    const key = /\bkey="([^"]+)"/.exec(chunk);
    if (!key) continue;
    // Comments mention ``hidden=True``; only a real assignment line counts.
    if (!includeHidden && /^\s*hidden=True,/m.test(chunk)) continue;
    out.push(key[1]);
  }
  return out;
}

await check("D0 integrity — the schema and the dictionary are both really parsed", () => {
  const keys = schemaKeys();
  assert.ok(keys.length > 30, `schema parse produced ${keys.length} keys — the spec shape drifted`);
  assert.ok(keys.includes("network_max_retries"), "a known non-hidden key must be found");
  assert.ok(!keys.includes("language"), "a hidden key (language) must be excluded");
  const dict = runtimeDict();
  assert.ok(Object.keys(dict).length > 50, "the runtime string file must expose its dictionary");
});

await check("D1 every non-hidden runtime setting has a label AND a desc in BOTH languages", () => {
  const dict = runtimeDict();
  const missing = [];
  for (const k of schemaKeys()) {
    for (const part of ["label", "desc"]) {
      const entry = dict[`runtime.${k}.${part}`];
      if (!entry) missing.push(`runtime.${k}.${part} (absent → the form falls back to the schema's ENGLISH string)`);
      else if (!entry.en || !entry.tr) missing.push(`runtime.${k}.${part} (en=${JSON.stringify(entry.en)} tr=${JSON.stringify(entry.tr)})`);
    }
  }
  assert.deepEqual(missing, [], `runtime settings rendered in the wrong language:\n  ${missing.join("\n  ")}`);
});

await check("D2 no stale runtime.<key> entries for settings the schema no longer has", () => {
  const dict = runtimeDict();
  // Hidden settings are edited from their own panels but keep their dictionary entries —
  // "stale" means the SCHEMA no longer has the key at all, hidden or not.
  const known = new Set(schemaKeys({ includeHidden: true }));
  const stale = Object.keys(dict)
    .filter((k) => k.startsWith("runtime.") && !k.startsWith("runtime.cat.") && !k.startsWith("runtime.unit."))
    .map((k) => k.replace(/^runtime\./, "").replace(/\.(label|desc)$/, ""))
    .filter((k) => !known.has(k));
  assert.deepEqual([...new Set(stale)], [], "dictionary entries for keys the schema dropped");
});

// ═════════════════════════════════════════════════════════════════════════════
// E. wake-threshold default drift
// ═════════════════════════════════════════════════════════════════════════════
await check("E1 the wake-threshold slider's markup default equals DEFAULTS['wake_threshold']", () => {
  const defaults = read("akana_server/settings_defaults.py");
  const m = /"wake_threshold":\s*([0-9.]+)/.exec(defaults);
  assert.ok(m, "DEFAULTS['wake_threshold'] must be readable — the guard has no source of truth otherwise");
  const want = Number(m[1]);
  const html = read("web_ui/index.html");
  const input = /<input id="wake-threshold"[^>]*value="([^"]+)"/.exec(html);
  assert.ok(input, "the wake-threshold slider must still carry a value attribute");
  assert.equal(
    Number(input[1]),
    want,
    "the slider shows this number until GET /voice/config answers (and forever if it fails) — a user tuning false wakes reads it as the live threshold",
  );
  const out = /<output id="wake-threshold-out"[^>]*>([^<]+)<\/output>/.exec(html);
  assert.ok(out, "the readout element must still exist");
  assert.equal(Number(out[1].trim()), want, "the readout must not disagree with the slider or the server default");
});

await check("E2 a failed /voice/config leaves NO unverified number in the wake readout", async () => {
  const els = {
    "wake-threshold": makeEl("input"),
    "wake-threshold-out": makeEl("output"),
    "wake-min-frames": makeEl("input"),
    "wake-min-frames-out": makeEl("output"),
    "voice-status": makeEl("div"),
  };
  // The markup values are on screen before the fetch resolves.
  els["wake-threshold"].value = "0.5";
  els["wake-threshold-out"].textContent = "0.5";
  els["wake-min-frames-out"].textContent = "3";
  const fetches = [];
  const doc = {
    readyState: "complete",
    body: makeEl("body"),
    getElementById: (id) => els[id] || null,
    createElement: (t) => makeEl(t),
    addEventListener: () => {},
    querySelector: () => null,
    querySelectorAll: () => [],
  };
  const win = {
    AkanaI18n: { t: (k) => k, getLanguage: () => "en" },
    AkanaCore: { baseUrl: () => "", authHeaders: () => ({}), showToast: () => {}, escapeHtml: (s) => s, parseApiError: () => "" },
    AkanaSettings: { baseUrl: () => "", authHeaders: () => ({}) },
    AkanaBus: { on: () => {}, emit: () => {} },
    document: doc,
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    fetch: async (u) => { fetches.push(String(u)); throw new Error("offline"); },
    addEventListener: () => {},
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (h) => clearTimeout(h),
  };
  win.window = win;
  const ctx = {
    console, document: doc, window: win,
    setTimeout, clearTimeout, setInterval, clearInterval,
    fetch: win.fetch, localStorage: win.localStorage,
    navigator: { mediaDevices: {} },
    URL: { createObjectURL: () => "", revokeObjectURL: () => {} },
  };
  vm.createContext(ctx);
  vm.runInContext(read("web_ui/static/akana-voice-settings.js"), ctx);
  const VS = win.AkanaVoiceSettings;
  assert.ok(VS && typeof VS.createSettings === "function", "AkanaVoiceSettings.createSettings must be reachable");
  const settings = VS.createSettings({
    setTtsEnabled() {}, getTtsEnabled: () => false, ttsToggle: null,
    ttsPlayer: { queue: [], playing: false }, hooks: { isChatPage: false },
    speechLang: () => "en", loadVoicePreferences: async () => {}, saveVoicePreferences: async () => {},
    setWakeListening: async () => true, syncWakeButtonUi() {}, stopAudioGraph() {}, voice: {},
  });
  assert.equal(typeof settings.loadVoiceConfig, "function", "loadVoiceConfig must be part of the settings API");
  await settings.loadVoiceConfig();
  assert.ok(
    fetches.some((u) => u.includes("/voice/config")),
    "harness integrity: the load must really have attempted (and failed) GET /voice/config",
  );
  assert.equal(
    els["wake-threshold-out"].textContent,
    "—",
    "the markup's placeholder is still on screen: the panel keeps stating a threshold it never read from the server",
  );
});

// ═════════════════════════════════════════════════════════════════════════════
// F/G. akana-chat-render.js — the language the chat surface actually renders in
// ═════════════════════════════════════════════════════════════════════════════
function loadRender({ langAtLoad = "en", strings = {} } = {}) {
  let lang = langAtLoad;
  const evalTimeKeys = [];
  let loaded = false;
  const i18n = {
    getLanguage: () => lang,
    t: (key, p) => {
      if (!loaded) evalTimeKeys.push(key);
      const s = strings[key];
      const base = s ? s[lang] || s.en : key;
      return p ? `${base}:${JSON.stringify(p)}` : base;
    },
  };
  const rafQueue = [];
  const win = {
    AkanaCore: { escapeHtml: (s) => String(s) },
    AkanaMarkdown: { render: (s) => String(s) },
    AkanaI18n: i18n,
    CSS: { escape: (s) => s },
    requestAnimationFrame: (cb) => { rafQueue.push(cb); return rafQueue.length; },
    setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {},
  };
  win.window = win;
  const doc = {
    createElement: (t) => makeEl(t),
    createElementNS: (_n, t) => makeEl(t),
    getElementById: () => null,
    addEventListener: () => {},
  };
  const ctx = {
    console, window: win, document: doc, Element,
    CSS: { escape: (s) => s },
    requestAnimationFrame: (cb) => { rafQueue.push(cb); return rafQueue.length; },
    setTimeout, clearTimeout,
    MutationObserver: class { observe() {} disconnect() {} },
    navigator: { clipboard: { writeText: async () => {} } },
  };
  vm.createContext(ctx);
  vm.runInContext(read("web_ui/static/akana-chat-render.js"), ctx);
  loaded = true;
  return {
    Render: win.AkanaChatRender,
    evalTimeKeys,
    setLanguage: (l) => { lang = l; },
  };
}

await check("F1 message timestamps follow the APP language, not a hardcoded locale", () => {
  const en = loadRender({ langAtLoad: "en" });
  const rows = [];
  const renderer = en.Render.createRenderer({
    log: makeEl("div"),
    appendUserMessage: () => { const r = makeEl("div"); rows.push(r); return r; },
    appendSystemNotice: () => {},
  });
  const ts = "2026-07-26T14:30:00";
  renderer.chatRenderMessage({ kind: "user", text: "hi", ts });
  const stamped = rows[0].dataset.time;
  // Compare against the platform's own answer for each locale rather than a literal, so the
  // check states the CONTRACT (which locale is used) and not an ICU formatting detail.
  const enWant = new Date(ts).toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" });
  const trWant = new Date(ts).toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit" });
  assert.notEqual(enWant, trWant, "harness precondition: the two locales must render this time differently");
  assert.equal(
    stamped, enWant,
    `an English user must not read the Turkish clock form on every message. got=${JSON.stringify(stamped)}`,
  );

  const tr = loadRender({ langAtLoad: "tr" });
  const trRows = [];
  const trRenderer = tr.Render.createRenderer({
    log: makeEl("div"),
    appendUserMessage: () => { const r = makeEl("div"); trRows.push(r); return r; },
    appendSystemNotice: () => {},
  });
  trRenderer.chatRenderMessage({ kind: "user", text: "selam", ts });
  assert.equal(trRows[0].dataset.time, trWant, "…and a Turkish user must still get the Turkish form");
});

await check("G1 no tool-card argument label is resolved at script-eval time", () => {
  // akana-i18n.js only reconciles with the backend AFTER every module has evaluated, so a
  // label resolved during eval is captured in the pre-reconcile language for the whole
  // session — the mixed-language tool cards on a first visit in a fresh browser.
  const r = loadRender({ langAtLoad: "en" });
  const argKeys = r.evalTimeKeys.filter((k) => /^msg\.arg_/.test(k));
  assert.deepEqual(
    argKeys, [],
    `${argKeys.length} argument label(s) were frozen at module-eval time (before the language is known): ${JSON.stringify([...new Set(argKeys)])}`,
  );
});

await check("G2 a language flip after load reaches the NEXT tool card's argument labels", () => {
  const strings = { "msg.arg_recursive": { en: "Recursive", tr: "Özyinelemeli" } };
  const r = loadRender({ langAtLoad: "en", strings });
  // The backend says Turkish; akana-i18n.js flips the language after module eval.
  r.setLanguage("tr");
  const card = r.Render.renderToolCall({
    name: "custom_thing", // not shell / not a curated tool → the generic arg-label path
    args: { recursive: true },
    phase: "end",
  });
  const text = allText(card);
  assert.ok(
    text.includes("Özyinelemeli"),
    `the card must use the CURRENT language for argument labels. rendered=${JSON.stringify(text.slice(0, 500))}`,
  );
  assert.ok(!/Recursive/.test(text), "…and must not still be carrying the pre-reconcile English label");
});

// ── Summary ─────────────────────────────────────────────────────────────────
if (failures) {
  console.error(`\nhunt6_frontend: ${passed} passed, ${failures} FAILED`);
  process.exit(1);
}
console.log(`hunt6_frontend: ${passed} front-end contracts passed ✓`);
process.exit(0);
