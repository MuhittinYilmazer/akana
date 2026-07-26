/**
 * Hunt-5 "F5 / restore correctness" contract — backend-free, node-vm.
 *
 * Loads the REAL akana-chat-store.js + akana-chat-threads.js (+ akana-chat.js for the
 * composer's attachment parking) in a VM with a fake DOM and drives the reload/restore
 * paths. Contracts locked here:
 *
 *   1. reload-restore-1  restore is a NAVIGATION: a "+"/switch during its hydrate await
 *                        supersedes it — the post-await repaint must not wipe the pane the
 *                        user is looking at nor rebind that thread to the restored conv.
 *   2. reload-restore-3  a TRANSIENT hydrate failure (5xx / network blip) must NOT unbind
 *                        the thread from its conversation while a local snapshot exists —
 *                        otherwise the next send forks a brand-new server conversation.
 *   3. reload-restore-2  a message the server still holds QUEUED (202, absent from
 *                        /messages) must survive the post-F5 merge instead of being dropped
 *                        as a stale ghost.
 *   4. singleton-…-5     a conversation switch PARKS the composer's pending attachments per
 *                        conversation and restores them on return (EC2 without input loss).
 *   5. background-…-6    after F5 the background-work marker is rebuilt from the server's
 *                        view of the conversation (no live turn_active frame ever arrives),
 *                        stamped source:"background" per the turn-lifecycle contract.
 *
 * Every contract is proved RED by a synthetic string-revert of ONLY that fix: the shipped
 * source must pass and the reverted variant must fail.
 *
 * Run: node tests/web/hunt5_threads_restore.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { makeI18nStub } from "./_i18n_stub.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const read = (rel) => readFileSync(path.join(REPO, "web_ui/static", rel), "utf8").replace(/\r\n/g, "\n");

const STORE_SRC = read("akana-chat-store.js");
const THREADS_SRC = read("akana-chat-threads.js");
const PANES_SRC = read("akana-chat-panes.js");
const CHAT_SRC = read("akana-chat.js");

/** String-revert one fix so the variant exhibits the original bug; a missing anchor means
 *  the product code drifted and the RED proof would silently stop proving anything. */
function patch(src, from, to) {
  assert.ok(src.includes(from), `revert anchor not found (code drifted): ${JSON.stringify(from.slice(0, 80))}`);
  return src.split(from).join(to);
}

// ── The synthetic reverts (one per finding) ─────────────────────────────────────
const REVERTS = {
  // reload-restore-1: restore had no _switchGen participation at all.
  "reload-restore-1": ({ threads }) => ({
    threads: patch(
      patch(
        threads,
        "        if (myGen !== _switchGen) return;\n        // AUTHORITATIVE RE-PAINT",
        "        // AUTHORITATIVE RE-PAINT",
      ),
      "      if (myGen !== _switchGen) return;\n      // After returning to the page / F5:",
      "      // After returning to the page / F5:",
    ),
  }),
  // reload-restore-3: any hydrate failure unbound the thread.
  "reload-restore-3": ({ threads }) => ({
    threads: patch(
      threads,
      "if (!ok && !hadLocal) thread.conversationId = null;",
      "if (!ok) thread.conversationId = null;",
    ),
  }),
  // reload-restore-2: the merge dropped every pending the session set could not vouch for.
  "reload-restore-2": ({ store }) => ({
    store: patch(store, " && !isServerQueued(thread, txt)", ""),
  }),
  // singleton-ui-across-chats-5: a switch destroyed the pending attachments.
  "singleton-ui-across-chats-5": ({ threads }) => ({
    threads: patch(
      threads,
      "      swapPendingAttachments(_leavingConvId, convId);",
      "      bridge.clearPendingAttachments?.();",
    ),
  }),
  // background-lifecycle-6seam: the seam existed but was DEAD — read under a name the chat
  // module never exported (plural) and called with no convId (a no-op for ""), so production
  // always fell through to the source-blind fallback, which stamped EVERY running turn —
  // including the user's own — source:"background".
  "background-lifecycle-6seam": ({ threads }) => ({
    threads: patch(
      threads,
      `        const reconcile = window.AkanaChat?.reconcileBgActiveTurn;
        if (typeof reconcile !== "function") return false;
        await reconcile(convId);
        return true;`,
      `        const reconcile = window.AkanaChat?.reconcileBgActiveTurns;
        if (typeof reconcile === "function") {
          await reconcile();
          return true;
        }
        const active = await bridge.probeActiveTurn?.(convId);
        if (!active) return false;
        try {
          await active.body?.cancel();
        } catch {
          /* ignore */
        }
        await window.AkanaChat?.onTurnActiveRemote?.(convId, { source: "background" });
        return true;`,
    ),
  }),
  // background-lifecycle-6: restore never rebuilt (nor showed) the background-work marker.
  "background-lifecycle-6": ({ threads }) => ({
    threads: patch(
      patch(
        threads,
        "          await seedBackgroundWorkingFromServer(restoredConvId);\n",
        "",
      ),
      "        window.AkanaChat?.maybeShowBgWorking?.(restoredConvId);\n",
      "",
    ),
  }),
};

// ── Minimal DOM node (only the surfaces the driven paths touch) ─────────────────
function makeEl(tag = "div") {
  return {
    tagName: String(tag).toUpperCase(),
    children: [],
    _html: "",
    dataset: {},
    style: {},
    attrs: {},
    _text: "",
    hidden: false,
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); if (v === "") this.children = []; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k] ?? null; },
    removeAttribute(k) { delete this.attrs[k]; },
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.children.push(c)); },
    remove() {},
    addEventListener() {},
  };
}

function makeStorage() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
    clear: () => m.clear(),
  };
}

function deferral() {
  let resolve;
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
}

// ── The REAL akana-chat.js consumer surface, in its own VM ─────────────────────
// The restore/switch paths rebuild the background-work marker through ONE seam on the chat
// module. A harness that INSTALLS that seam itself proves nothing: the name threads actually
// called (`reconcileBgActiveTurns`, plural) existed only inside the test, so the preferred
// branch passed here while production always fell through to the source-blind fallback.
// These scenarios drive the REAL export over a fake GET /chat/active that answers with the
// shapes the server really produces:
//   204 → idle · 200 → a followable SSE turn (the user's own) ·
//   202 {kind:"background"} → a schedule fire / background_run ·
//   202 {kind:"nonstreaming"} → the user's own voice/blocking/connector turn.
function loadRealChat({ conversationIdForMemory, activity, delayMs = 0 }) {
  const ts = { begin: [], resume: [], end: 0, clear: [] };
  const probes = [];
  const refreshes = [];
  const doc = {
    readyState: "complete",
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: (t) => makeEl(t),
    addEventListener: () => {},
    body: { getAttribute: () => null, classList: { add() {}, remove() {}, contains: () => false } },
  };
  const fetchImpl = async (url) => {
    const m = /\/chat\/active\/([^/?]+)/.exec(String(url));
    if (!m) return { ok: false, status: 404, json: async () => ({}) };
    const id = decodeURIComponent(m[1]);
    probes.push(id);
    // A real probe is a round-trip; the delay is what makes "was the seam AWAITED?" provable.
    if (delayMs) await new Promise((r) => setTimeout(r, delayMs));
    const a = activity.get(id) || { status: 204 };
    return {
      ok: a.status >= 200 && a.status < 300,
      status: a.status,
      body: { cancel: async () => { a.cancelled = true; } },
      json: async () => a.body || {},
    };
  };
  const win = {
    addEventListener: () => {},
    AkanaI18n: makeI18nStub(),
    AkanaCore: { baseUrl: () => "", authHeaders: () => ({}), authHeadersMultipart: () => ({}), escapeHtml: (s) => String(s ?? ""), parseApiError: (_b, s) => `HTTP ${s}` },
    AkanaSettings: { baseUrl: () => "", authHeaders: () => ({}) },
    AkanaTurnStatus: {
      mount: () => {},
      begin: (id) => ts.begin.push(id),
      resume: (id) => ts.resume.push(id),
      end: () => { ts.end += 1; },
      clear: (id) => ts.clear.push(id),
      setPhase: () => {},
      isActive: () => false,
    },
    AkanaChatRender: { createRenderer: () => ({ chatRenderMessage: () => {} }), mapServerMessagesToThread: () => [] },
    AkanaChatTransport: { create: () => ({ isConversationStreamActive: () => false }) },
    AkanaChatThreads: {
      create: () => ({
        // The chat module asks the REAL threads instance what is displayed.
        conversationIdForMemory,
        refreshArchiveActivity: (id) => { refreshes.push(id); },
        getChatArchiveItems: () => [],
        getChatStore: () => ({ threads: {}, activeByProfile: {} }),
      }),
    },
    document: doc,
    localStorage: makeStorage(),
    sessionStorage: makeStorage(),
    fetch: fetchImpl,
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
    fetch: fetchImpl,
    localStorage: win.localStorage,
    sessionStorage: win.sessionStorage,
    URL: { createObjectURL: () => "blob:x", revokeObjectURL: () => {} },
    FormData: class { append() {} },
    requestAnimationFrame: (fn) => fn(),
  };
  vm.createContext(ctx);
  vm.runInContext(CHAT_SRC, ctx);
  assert.ok(win.AkanaChat, "akana-chat.js did not export AkanaChat");
  return { AkanaChat: win.AkanaChat, ts, probes, refreshes };
}

// ── Harness: store + threads in one VM, with a controllable bridge ──────────────
function setup(revertKey = null) {
  let sources = { store: STORE_SRC, threads: THREADS_SRC };
  if (revertKey) sources = { ...sources, ...REVERTS[revertKey](sources) };

  const localStorage = makeStorage();
  const sessionStorage = makeStorage();
  // Server-side queue snapshots per conversation (GET /chat/queue/{id}).
  const queueByConv = new Map();
  let queueFetches = 0;
  const ctx = {
    console,
    setTimeout,
    clearTimeout,
    crypto: globalThis.crypto,
    localStorage,
    sessionStorage,
    document: {
      getElementById: () => null,
      body: { getAttribute: () => null },
      addEventListener: () => {},
    },
    async fetch(url) {
      const u = String(url);
      const q = u.match(/\/chat\/queue\/([^/?]+)$/);
      if (q) {
        queueFetches += 1;
        const previews = queueByConv.get(decodeURIComponent(q[1]));
        if (!previews) return { ok: false, json: async () => ({}) };
        return {
          ok: true,
          json: async () => ({ depth: previews.length, items: previews.map((p, i) => ({ id: `q${i}`, text_preview: p })) }),
        };
      }
      return { ok: false, json: async () => ({}) };
    },
  };
  ctx.window = {
    addEventListener: () => {},
    localStorage,
    sessionStorage,
    AkanaCore: { baseUrl: () => "", authHeaders: () => ({}) },
    AkanaI18n: makeI18nStub(),
  };
  vm.createContext(ctx);
  vm.runInContext(sources.store, ctx);
  vm.runInContext(sources.threads, ctx);
  vm.runInContext(PANES_SRC, ctx);

  // AkanaChat consumer surface (the real one lives in akana-chat.js) — record what the
  // restore/switch paths drive so the turn-lifecycle contract can be asserted.
  const chatCalls = { turnActive: [], bgWorking: [], queueState: [] };
  ctx.window.AkanaChat = {
    onTurnActiveRemote: async (convId, evt) => { chatCalls.turnActive.push([convId, evt]); },
    maybeShowBgWorking: (convId) => { chatCalls.bgWorking.push(convId); },
    refreshQueueState: (convId) => { chatCalls.queueState.push(convId); },
  };

  const paneContainer = makeEl("div");
  const pm = ctx.window.AkanaChatPanes.createPaneManager({ container: paneContainer, createEl: (t) => makeEl(t) });
  pm.show(null);

  const turnsByConv = new Map();
  const statusByConv = new Map(); // convId → forced HTTP status (transient-failure tests)
  const deferred = new Map();
  const liveStreams = new Set();
  const log = makeEl("div");
  let renderCount = 0;
  const calls = {
    clearAttachments: 0,
    parkAttachments: [],
    restoreAttachments: [],
    dropAttachments: [],
    probe: [],
  };
  let probeResult = null; // fake live-turn Response (or null = 204/no active turn)

  const bridge = {
    hooks: {
      log,
      logScroll: null,
      setLogLoading: () => {},
      updateEmptyState: () => {},
      scrollLogToBottom: () => {},
      updateSettingsHero: () => {},
      shortConversationId: (id) => id || "-",
      loadMemoryConversations: () => {},
      appendSystemNotice: () => {},
      showToast: () => {},
    },
    async fetchConversationTurns(convId) {
      if (deferred.has(convId)) await deferred.get(convId).promise;
      if (statusByConv.has(convId)) return { status: statusByConv.get(convId), turns: [] };
      const has = turnsByConv.has(convId);
      return { status: has ? 200 : 404, turns: has ? turnsByConv.get(convId) : [] };
    },
    abortConversationTurnsFetch: () => {},
    mapServerMessagesToThread: (turns) => (Array.isArray(turns) ? turns.slice() : []),
    chatRenderMessage: () => { renderCount += 1; log.appendChild(makeEl("div")); },
    abortStream: () => {},
    setForegroundConversation: () => {},
    showConversation: (convId) => pm.show(convId),
    removeConversation: (convId) => pm.remove(convId),
    rekeyConversation: (a, b) => pm.rekey(a, b),
    reattachLiveRow: () => false,
    isConversationStreamActive: (convId) => liveStreams.has(convId),
    syncComposerForDisplayed: () => {},
    resumeActiveTurn: async () => false,
    probeActiveTurn: async (convId) => { calls.probe.push(convId); return probeResult; },
    cancelActiveTurnOnServer: async () => {},
    clearPendingAttachments: () => { calls.clearAttachments += 1; },
    parkPendingAttachments: (convId) => { calls.parkAttachments.push(convId); },
    restorePendingAttachments: (convId) => { calls.restoreAttachments.push(convId); },
    dropParkedAttachments: (convId) => { calls.dropAttachments.push(convId); },
  };

  let archiveItems = [];
  ctx.window.AkanaChatArchive = {
    createArchive: () => ({
      loadChatArchiveList: () => {},
      insertConversationLocally: () => true,
      refreshActiveConversationMeta: () => {},
      refreshConvActivityFromServer: () => {},
      clearConvActivity: () => {},
      getChatArchiveItems: () => archiveItems,
      setChatArchiveItems: (v) => { archiveItems = v; },
      setActiveConversationHighlight: () => {},
      getActiveConversationMeta: () => null,
      setActiveConversationMeta: () => {},
      syncChatThreadBar: () => {},
      deleteConversationApi: async () => {},
      patchConversationApi: async () => {},
      exportConversationMarkdown: () => {},
      openArchiveDrawer: () => {},
      closeArchiveDrawer: () => {},
      wireArchiveChrome: () => {},
      wireThreadBar: () => {},
    }),
  };

  const T = ctx.window.AkanaChatThreads.create(bridge);

  return {
    T,
    bridge,
    calls,
    chatCalls,
    /** Replace the recording stub with a DELEGATING spy over the REAL akana-chat.js exports:
     *  the calls are recorded here, but the work is done by the module production ships (a
     *  seam the test invents cannot prove the branch is reachable). */
    useRealChat({ activity = new Map(), delayMs = 0 } = {}) {
      const real = loadRealChat({
        conversationIdForMemory: () => T.conversationIdForMemory(),
        activity,
        delayMs,
      });
      const spy = { reconciles: 0, order: [], showSawStrip: null };
      ctx.window.AkanaChat = {
        reconcileBgActiveTurn: (id) => {
          spy.reconciles += 1;
          spy.order.push(`reconcile(${id === undefined ? "<no-arg>" : JSON.stringify(id)})`);
          return real.AkanaChat.reconcileBgActiveTurn(id);
        },
        maybeShowBgWorking: (id) => {
          spy.order.push("show");
          // Snapshot at CALL time: if the seam were fired-and-forgotten the strip would not
          // be up yet when the (synchronous) show runs.
          spy.showSawStrip = real.ts.resume.includes(id);
          chatCalls.bgWorking.push(id);
          return real.AkanaChat.maybeShowBgWorking(id);
        },
        onTurnActiveRemote: async (convId, evt) => {
          chatCalls.turnActive.push([convId, evt]);
          return real.AkanaChat.onTurnActiveRemote(convId, evt);
        },
        refreshQueueState: (convId) => { chatCalls.queueState.push(convId); },
      };
      return { ...real, spy };
    },
    log,
    turnsByConv,
    statusByConv,
    deferred,
    queueByConv,
    queueFetches: () => queueFetches,
    win: () => ctx.window,
    markLiveStream: (id) => liveStreams.add(id),
    setProbeResult: (r) => { probeResult = r; },
    setResume: (fn) => { bridge.resumeActiveTurn = fn; },
    getRenderCount: () => renderCount,
    resetRenderCount: () => { renderCount = 0; },
    store: () => T.getChatStore(),
    seedThread(convId, { active = false, messages = [], title = "New chat" } = {}) {
      const s = T.getChatStore();
      const tid = `seed-${convId || "null"}-${Object.keys(s.threads).length}`;
      s.threads[tid] = {
        id: tid,
        profile: "cursor",
        conversationId: convId || null,
        title,
        updatedAt: Date.now(),
        messages: messages.map((m) => ({ ...m })),
      };
      if (active) s.activeByProfile.cursor = tid;
      return s.threads[tid];
    },
  };
}

// ── The answers GET /api/v1/chat/active/{id} really gives ──────────────────────
/** 200 — a followable SSE turn: the user's OWN detached turn. */
const FOLLOWABLE = () => ({ status: 200 });
/** 202 — running, nothing to follow, and it IS a schedule fire / background_run. */
const BG_RUNNING = () => ({
  status: 202,
  body: { running: true, followable: false, kind: "background", started_at: Date.now() - 60000 },
});
/** 202 — running, nothing to follow, but it is the user's own voice/blocking/connector turn. */
const NONSTREAMING = () => ({
  status: 202,
  body: { running: true, followable: false, kind: "nonstreaming", started_at: Date.now() - 5000 },
});

// ── Scenarios (each one asserts a contract; the reverted variant must fail it) ───
const SCENARIOS = {
  // 1. reload-restore-1 — a "+" during the hydrate await supersedes the restore.
  "reload-restore-1": async (h) => {
    h.seedThread("A", { active: true, messages: [{ kind: "user", text: "a-1" }, { kind: "assistant", text: "a-2" }] });
    h.turnsByConv.set("A", [{ kind: "user", text: "a-1" }, { kind: "assistant", text: "a-2" }]);
    const d = deferral();
    h.deferred.set("A", d);
    const p = h.T.chatRestoreActiveThread(); // suspended inside hydrate
    await h.T.chatStartNewThread({ force: true, localOnly: true }); // user hits "+"
    const newThread = h.T.chatActiveThread();
    const rendersBefore = h.getRenderCount();
    d.resolve();
    await p;
    assert.equal(
      h.T.chatActiveThread().id, newThread.id,
      "the new chat should stay active (restore must not re-activate the restored conversation)",
    );
    assert.equal(
      newThread.conversationId, null,
      "the new empty chat must NOT be rebound to the restored conv id (the next send would go to the wrong conversation)",
    );
    assert.equal(
      h.T.conversationIdForMemory(), "",
      "the displayed conversation must stay the new empty chat",
    );
    assert.equal(
      h.getRenderCount(), rendersBefore,
      "the stale restore must not repaint the restored chat's messages into the new chat's pane",
    );
  },

  // 2. reload-restore-3 — a transient hydrate failure keeps the conversation binding.
  "reload-restore-3": async (h) => {
    const a = h.seedThread("A", {
      active: true,
      messages: [{ kind: "user", text: "q" }, { kind: "assistant", text: "answer" }],
    });
    h.statusByConv.set("A", 503); // server restarting / warming up
    await h.T.chatRestoreActiveThread();
    assert.equal(
      a.conversationId, "A",
      "a transient hydrate failure must NOT unbind the thread (the next send would fork a new conversation)",
    );
    assert.equal(a.messages.length, 2, "the local snapshot must be preserved on a transient failure");
    assert.equal(h.T.conversationIdForMemory(), "A", "the restored conversation must stay the displayed one");
  },

  // 2b. counterpart: an AUTHORITATIVE empty (404, nothing local) still unbinds.
  "reload-restore-3b": async (h) => {
    const a = h.seedThread("GONE", { active: true, messages: [] });
    await h.T.chatRestoreActiveThread(); // 404 (not in turnsByConv) + no local messages
    assert.equal(a.conversationId, null, "an authoritative 404 with no local snapshot must still unbind");
  },

  // 3. reload-restore-2 — a still-queued (202) message survives the post-F5 merge.
  "reload-restore-2": async (h) => {
    const a = h.seedThread("A", {
      active: true,
      // Loaded from localStorage (NOT sent in this page session) — exactly the post-F5 state.
      messages: [
        { kind: "user", text: "first question" },
        { kind: "assistant", text: "answer 1" },
        { kind: "user", text: "queued follow-up while busy", _pendingUser: true },
      ],
    });
    h.turnsByConv.set("A", [{ kind: "user", text: "first question" }, { kind: "assistant", text: "answer 1" }]);
    h.queueByConv.set("A", ["queued follow-up while busy"]); // the server still holds it
    await h.T.chatRestoreActiveThread();
    assert.equal(a.messages.length, 3, "the still-queued message must not be dropped as a stale ghost");
    assert.equal(a.messages[2].text, "queued follow-up while busy", "the queued message must stay at the tail");
    assert.ok(h.queueFetches() >= 1, "the server queue must actually be consulted");
  },

  // 3b. a LONG queued message: the server only exposes an 80-char preview.
  "reload-restore-2b": async (h) => {
    const long = `follow-up ${"x".repeat(200)}`;
    const preview = `${long.slice(0, 79)}…`;
    const a = h.seedThread("A", {
      active: true,
      messages: [
        { kind: "user", text: "first question" },
        { kind: "assistant", text: "answer 1" },
        { kind: "user", text: long, _pendingUser: true },
      ],
    });
    h.turnsByConv.set("A", [{ kind: "user", text: "first question" }, { kind: "assistant", text: "answer 1" }]);
    h.queueByConv.set("A", [preview]);
    await h.T.chatRestoreActiveThread();
    assert.equal(a.messages.length, 3, "the truncated preview must still match the local pending text");
    assert.equal(a.messages[2].text, long, "the FULL local text is kept (the preview is only a matcher)");
  },

  // 3c. counterpart: with an EMPTY server queue the stale-ghost guard still drops it.
  "reload-restore-2c": async (h) => {
    const a = h.seedThread("A", {
      active: true,
      messages: [
        { kind: "user", text: "first question" },
        { kind: "assistant", text: "answer 1" },
        { kind: "user", text: "ghost from a stale snapshot", _pendingUser: true },
      ],
    });
    h.turnsByConv.set("A", [{ kind: "user", text: "first question" }, { kind: "assistant", text: "answer 1" }]);
    h.queueByConv.set("A", []); // nothing queued server-side
    await h.T.chatRestoreActiveThread();
    assert.equal(a.messages.length, 2, "a ghost the server does not hold queued must still be dropped");
  },

  // 4. singleton-ui-across-chats-5 — attachments are parked per conversation, not destroyed.
  "singleton-ui-across-chats-5": async (h) => {
    h.seedThread("A", { active: true, messages: [{ kind: "user", text: "a" }] });
    h.seedThread("B", { active: false, messages: [] });
    h.turnsByConv.set("B", [{ kind: "user", text: "b" }]);
    h.turnsByConv.set("A", [{ kind: "user", text: "a" }]);
    await h.T.switchChatConversation("B");
    assert.deepEqual(
      h.calls.parkAttachments, ["A"],
      "the leaving chat's pending attachments must be PARKED (not destroyed)",
    );
    assert.deepEqual(h.calls.restoreAttachments, ["B"], "the opened chat's own attachments must be restored");
    assert.equal(h.calls.clearAttachments, 0, "the destructive clear must not be used on a switch");
  },

  // 4b. "+" from a real chat parks that chat's attachments; the new empty chat gets none.
  "singleton-ui-across-chats-5b": async (h) => {
    h.seedThread("A", { active: true, messages: [{ kind: "user", text: "a" }] });
    await h.T.chatStartNewThread({ force: true, localOnly: true });
    assert.deepEqual(h.calls.parkAttachments, ["A"], "the leaving chat's attachments are parked under its own id");
    assert.deepEqual(h.calls.restoreAttachments, [], "an empty new chat has no parking slot to restore from");
  },

  // 4c. leaving an UNBOUND surface clears: it has no stable key, so parking it would leak
  //     the files into the next empty chat (which restores from the very same slot).
  "singleton-ui-across-chats-5c": async (h) => {
    h.seedThread(null, { active: true, messages: [] });
    await h.T.chatStartNewThread({ force: true, localOnly: true });
    assert.deepEqual(h.calls.parkAttachments, [], "an unbound surface must not get a parking slot");
    assert.equal(h.calls.clearAttachments, 1, "EC2: its attachments are cleared instead");
    assert.deepEqual(h.calls.restoreAttachments, [], "nothing may be restored into the new empty chat");
  },

  // 4d. a DELETED conversation's parking slot must be dropped: there is no return trip, so
  //     the files (and their preview object URLs) would be pinned for the page's lifetime.
  "singleton-ui-across-chats-5d": async (h) => {
    h.seedThread("A", { active: true, messages: [{ kind: "user", text: "a" }] });
    h.seedThread("B", { active: false, messages: [] });
    h.turnsByConv.set("A", [{ kind: "user", text: "a" }]);
    h.turnsByConv.set("B", []);
    await h.T.switchChatConversation("B"); // A's attachments are parked under "A"
    assert.deepEqual(h.calls.parkAttachments, ["A"], "precondition: A has a parking slot");
    await h.T.deleteConversationById("A", { confirm: false, quiet: true });
    assert.deepEqual(h.calls.dropAttachments, ["A"], "the deleted chat's parked attachments must be purged");
  },

  // 5. background-lifecycle-6 — after F5 restore rebuilds the marker through the REAL
  //    chat.js seam, from the only answer a running background job ever gives (202).
  "background-lifecycle-6": async (h) => {
    h.seedThread("A", { active: true, messages: [{ kind: "user", text: "run the report" }] });
    h.turnsByConv.set("A", [{ kind: "user", text: "run the report" }]);
    const chat = h.useRealChat({ activity: new Map([["A", BG_RUNNING()]]), delayMs: 1 });
    h.setResume(async () => false); // a background job has no follower buffer to resume
    await h.T.chatRestoreActiveThread();
    assert.equal(chat.spy.reconciles, 1, "restore must rebuild the marker through the chat module's seam");
    assert.ok(
      chat.ts.resume.includes("A"),
      "after F5 the working strip must be rebuilt from the server (no turn_active frame ever arrives here)",
    );
    assert.ok(chat.refreshes.includes("A"), "…and the sidebar activity badge refreshed with it");
    // The log-freshness probe (refreshConversationLogAfterTurn) is a DIFFERENT caller and
    // legitimately runs on this path; what must never happen is the background decision
    // adding a second, source-blind probe of its own — that probe cannot read the kind.
    assert.deepEqual(h.calls.probe, ["A"], "the background decision must not probe /chat/active source-blind");
    assert.deepEqual(h.chatCalls.turnActive, [], "nothing may be stamped source:'background' without reading the kind");
  },

  // 5d. the seam is called WITH the conversation id and AWAITED: reconcileBgActiveTurn("")
  //     is an immediate no-op, and a fire-and-forget call paints one navigation late.
  "background-lifecycle-6d": async (h) => {
    h.seedThread("A", { active: true, messages: [] });
    h.seedThread("B", { active: false, messages: [] });
    h.turnsByConv.set("B", [{ kind: "user", text: "run the report" }]);
    const chat = h.useRealChat({ activity: new Map([["B", BG_RUNNING()]]), delayMs: 1 });
    h.setResume(async () => false);
    await h.T.switchChatConversation("B");
    assert.equal(chat.spy.reconciles, 1, "the source-aware seam must be used (a bare probe cannot read the kind)");
    assert.deepEqual(
      chat.spy.order, ['reconcile("B")', "show"],
      "the seam must be called with the OPENED conversation's id, before the strip is shown",
    );
    assert.equal(
      chat.spy.showSawStrip, true,
      "the rebuild must be AWAITED — the strip has to be up on THIS pass, not one navigation later",
    );
    assert.ok(chat.ts.resume.includes("B"), "the opened chat's running job must show its working strip");
  },

  // 5b. a turn this page RESUMED is the user's own — it must not drive the background marker.
  "background-lifecycle-6b": async (h) => {
    h.seedThread("A", { active: true, messages: [{ kind: "user", text: "hi" }] });
    h.turnsByConv.set("A", [{ kind: "user", text: "hi" }]);
    const chat = h.useRealChat({ activity: new Map([["A", FOLLOWABLE()]]), delayMs: 1 });
    h.setResume(async () => true); // resumed → local stream state already reflects it
    await h.T.chatRestoreActiveThread();
    assert.equal(chat.spy.reconciles, 0, "a resumed (user) turn must not be rebuilt as background work");
    assert.deepEqual(h.chatCalls.turnActive, [], "…and nothing may be stamped source:'background' (contract rule 4)");
    assert.deepEqual(chat.ts.resume, [], "no background strip for the user's own turn");
  },

  // 5c. a conversation streaming live in THIS page is the user's own turn by construction:
  //     the seed must not spend the AWAITED rebuild on it. (maybeShowBgWorking still runs its
  //     own fire-and-forget verification probe afterwards — that one can only DROP a stale
  //     marker, never create one, so it is not a source of a false background strip.)
  "background-lifecycle-6c": async (h) => {
    h.seedThread("A", { active: true, messages: [{ kind: "user", text: "hi" }] });
    h.turnsByConv.set("A", [{ kind: "user", text: "hi" }]);
    h.markLiveStream("A");
    const chat = h.useRealChat({ activity: new Map([["A", FOLLOWABLE()]]), delayMs: 1 });
    h.setResume(async () => false);
    await h.T.chatRestoreActiveThread();
    assert.equal(chat.spy.reconciles, 0, "a locally streaming conversation must not be rebuilt as background work");
    assert.deepEqual(h.chatCalls.turnActive, [], "…and nothing may be stamped source:'background' for it");
    assert.deepEqual(chat.ts.resume, [], "…nor may a background strip be painted over the user's own stream");
  },

  // 5e. a turn the seed cannot CLASSIFY must never be stamped background. The old fallback
  //     marked any running turn — including the user's own followable (200) turn, live in the
  //     window between resume's probe and this one — as background; only a source:"background"
  //     completion can drop that marker, so it leaked into a phantom "working…" strip.
  "background-lifecycle-6e": async (h) => {
    h.seedThread("A", { active: true, messages: [{ kind: "user", text: "hi" }] });
    h.turnsByConv.set("A", [{ kind: "user", text: "hi" }]);
    const chat = h.useRealChat({ activity: new Map([["A", FOLLOWABLE()]]), delayMs: 1 });
    h.setResume(async () => false); // resume's own probe ran a moment too early
    h.setProbeResult({ body: { cancel: async () => {} } }); // …what a source-blind fallback would see
    await h.T.chatRestoreActiveThread();
    assert.deepEqual(
      h.chatCalls.turnActive, [],
      "a turn that is not kind:'background' must never be stamped source:'background'",
    );
    assert.deepEqual(chat.ts.resume, [], "…and must not paint the background working strip");
  },

  // 5f. 202 kind:"nonstreaming" is the user's OWN voice/blocking/connector turn (e.g. started
  //     in another tab): running, unfollowable — and by contract it drives nothing.
  "background-lifecycle-6f": async (h) => {
    h.seedThread("A", { active: true, messages: [{ kind: "user", text: "hi" }] });
    h.turnsByConv.set("A", [{ kind: "user", text: "hi" }]);
    const chat = h.useRealChat({ activity: new Map([["A", NONSTREAMING()]]), delayMs: 1 });
    h.setResume(async () => false); // 202 is not followable → resume correctly declines
    await h.T.chatRestoreActiveThread();
    assert.equal(chat.spy.reconciles, 1, "the seam still runs — it is what reads the kind");
    assert.deepEqual(chat.ts.resume, [], "the user's own unfollowable turn must not paint a background strip");
    assert.deepEqual(h.chatCalls.turnActive, [], "…and must not be stamped source:'background'");
  },
};

// Which revert must break which scenarios (the RED proof).
const RED = {
  "reload-restore-1": ["reload-restore-1"],
  "reload-restore-3": ["reload-restore-3"],
  "reload-restore-2": ["reload-restore-2", "reload-restore-2b"],
  "singleton-ui-across-chats-5": ["singleton-ui-across-chats-5"],
  "background-lifecycle-6": ["background-lifecycle-6"],
  "background-lifecycle-6seam": [
    "background-lifecycle-6",
    "background-lifecycle-6d",
    "background-lifecycle-6e",
    "background-lifecycle-6f",
  ],
};

let passed = 0;
const failures = [];

/** Drift gate — runs BEFORE anything else and covers EVERY entry in REVERTS, including the
 *  ones no currently-listed RED exercises. A revert whose anchor no longer matches (or that
 *  produces an identical source) proves nothing; the failure has to surface here, loudly,
 *  instead of arriving as an exception inside a RED's try block where it reads as success. */
function auditReverts() {
  const base = { store: STORE_SRC, threads: THREADS_SRC };
  for (const key of Object.keys(REVERTS)) {
    let out;
    try {
      out = REVERTS[key](base);
    } catch (e) {
      failures.push(`REVERT ${key}: ${e && e.message ? e.message : e}`);
      continue;
    }
    for (const [file, src] of Object.entries(out)) {
      if (src === base[file]) {
        failures.push(`REVERT ${key}: the ${file} revert produced an IDENTICAL source — it reverts nothing`);
      }
    }
  }
  // A revert nothing exercises is a proof that quietly stopped running.
  const exercised = new Set(Object.keys(RED));
  for (const key of Object.keys(REVERTS)) {
    if (!exercised.has(key)) failures.push(`REVERT ${key}: no RED scenario exercises it`);
  }
}

async function green(name) {
  try {
    await SCENARIOS[name](setup(null));
    passed += 1;
  } catch (e) {
    failures.push(`GREEN ${name}: ${e && e.message ? e.message : e}`);
  }
}

async function red(revertKey, name) {
  // The reverted source is built OUTSIDE the try: patch() asserts its anchor still matches,
  // so building it inside would let "the code drifted" throw where a throw MEANS "the bug
  // reproduced" — the disarmed proof would be counted as a passing one.
  let h;
  try {
    h = setup(revertKey);
  } catch (e) {
    failures.push(`RED ${name}: could not build the reverted source — ${e && e.message ? e.message : e}`);
    return;
  }
  let err = null;
  try {
    await SCENARIOS[name](h);
  } catch (e) {
    err = e;
  }
  // Only a CONTRACT assertion counts. A TypeError/ReferenceError means the scenario broke on
  // the reverted source, not that it caught the bug the contract describes.
  if (err instanceof assert.AssertionError) {
    passed += 1;
  } else if (err) {
    failures.push(
      `RED ${name}: the reverted source failed with a non-assertion ${err.name || "error"} ` +
        `(${err.message || err}) — the scenario broke instead of proving the bug`,
    );
  } else {
    failures.push(`RED ${name}: the pre-fix (reverted) source PASSED — the test does not prove the bug`);
  }
}

auditReverts();
for (const name of Object.keys(SCENARIOS)) await green(name);
for (const [revertKey, names] of Object.entries(RED)) {
  for (const name of names) await red(revertKey, name);
}

// ═══════════════════════════════════════════════════════════════════════════════
// Composer attachment parking against the REAL akana-chat.js (the state itself).
// ═══════════════════════════════════════════════════════════════════════════════
{
  const revoked = []; // preview object URLs actually handed back to the browser
  const ctx = {
    console,
    setTimeout,
    clearTimeout,
    URL: { createObjectURL: () => "blob:x", revokeObjectURL: (u) => { revoked.push(u); } },
    document: {
      getElementById: () => null, // no chip host → renderAttachmentChips is a no-op
      querySelector: () => null,
      querySelectorAll: () => [],
      createElement: (t) => makeEl(t),
      addEventListener: () => {},
      body: { getAttribute: () => null, classList: { add() {}, remove() {}, contains: () => false } },
    },
    fetch: async () => ({ ok: false, json: async () => ({}) }),
    localStorage: makeStorage(),
    sessionStorage: makeStorage(),
  };
  let capturedBridge = null;
  ctx.window = {
    addEventListener: () => {},
    localStorage: ctx.localStorage,
    sessionStorage: ctx.sessionStorage,
    AkanaCore: { baseUrl: () => "", authHeaders: () => ({}), escapeHtml: (s) => String(s ?? "") },
    AkanaI18n: makeI18nStub(),
    AkanaChatRender: { createRenderer: () => ({ chatRenderMessage: () => {} }), mapServerMessagesToThread: () => [] },
    AkanaChatTransport: { create: () => ({}) },
    AkanaChatThreads: {
      create: (bridge) => {
        capturedBridge = bridge;
        return { getChatArchiveItems: () => [], getChatStore: () => ({ threads: {}, activeByProfile: {} }) };
      },
    },
  };
  ctx.window.window = ctx.window;
  ctx.window.document = ctx.document;
  vm.runInNewContext(CHAT_SRC, ctx);
  const Chat = ctx.window.AkanaChat;
  Chat.getChatArchiveItems(); // forces buildBridge()

  await (async () => {
    try {
      assert.ok(capturedBridge, "the chat bridge was not built");
      assert.equal(
        typeof capturedBridge.parkPendingAttachments, "function",
        "the composer must expose per-conversation attachment parking",
      );
      const att = { id: "up-1", name: "screenshot.png", kind: "image", size: 10, previewUrl: "" };
      Chat._test.seedPendingAttachment(att);
      assert.equal(Chat._test.getPendingAttachments().length, 1, "precondition: one attachment in the composer");
      capturedBridge.parkPendingAttachments("A"); // leave chat A
      assert.equal(
        Chat._test.getPendingAttachments().length, 0,
        "EC2: attachments must never be carried into another chat",
      );
      capturedBridge.restorePendingAttachments("B"); // open chat B
      assert.equal(Chat._test.getPendingAttachments().length, 0, "chat B has no attachments of its own");
      capturedBridge.parkPendingAttachments("B"); // leave B…
      capturedBridge.restorePendingAttachments("A"); // …and return to A
      const back = Chat._test.getPendingAttachments();
      assert.equal(back.length, 1, "A's uploaded attachment must come back on return (no silent input loss)");
      assert.equal(back[0].id, "up-1", "the SAME upload id must come back (no re-upload needed)");

      // A DELETED conversation has no return trip: its slot must be GONE, not merely
      // unreachable — nothing else can ever release the image blobs it pins.
      assert.equal(
        typeof capturedBridge.dropParkedAttachments, "function",
        "the composer must expose a purge hook for deleted conversations",
      );
      capturedBridge.parkPendingAttachments("A"); // park A's chip again
      Chat._test.seedPendingAttachment({ id: "up-2", name: "diagram.png", kind: "image", size: 20, previewUrl: "blob:img-2" });
      capturedBridge.parkPendingAttachments("D"); // …and one under the chat about to be deleted
      capturedBridge.dropParkedAttachments("D");
      assert.deepEqual(revoked, ["blob:img-2"], "the deleted chat's preview object URL must be revoked");
      capturedBridge.restorePendingAttachments("D");
      assert.equal(
        Chat._test.getPendingAttachments().length, 0,
        "a deleted chat's parked attachments must not be restorable",
      );
      capturedBridge.restorePendingAttachments("A");
      assert.equal(
        Chat._test.getPendingAttachments().length, 1,
        "purging one conversation's slot must not touch another's",
      );
      passed += 1;
    } catch (e) {
      failures.push(`GREEN attachment-parking(real akana-chat.js): ${e && e.message ? e.message : e}`);
    }
  })();
}

// ── Summary ────────────────────────────────────────────────────────────────────
if (failures.length) {
  console.error(`\nhunt5_threads_restore: ${passed} passed, ${failures.length} FAILED`);
  for (const f of failures) console.error(`  ✗ ${f}`);
  process.exit(1);
}
console.log(`hunt5_threads_restore.harness: ${passed} restore/F5 contracts PASSED ✓`);
process.exit(0);
