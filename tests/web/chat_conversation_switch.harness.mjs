/**
 * Chat-switch / conversation isolation contract test — no backend, node-vm.
 *
 * Root scenario (user report): "send a message from one chat → open a new chat →
 * send a message" → frontend/backend slightly breaks. Root cause: fire-and-forget
 * store syncs (end of streamChat/resume) were targeting the active thread, NOT
 * `convId`; when the user switched to a new conversation during the await, A's turns
 * were written to B and B.conversationId became A → the next message went to the
 * WRONG conversation (backend) + store/DOM desync (frontend).
 *
 * Units covered (real modules, archive STUBbed):
 *   akana-chat-store.js   → activateThreadForConversation (cannibalization)
 *   akana-chat-threads.js → syncConversationLogFromServer (active-thread leak)
 *                            reloadConversationLogFromServer (stale-reload shield)
 *                            switchChatConversation (gen guard + isolation)
 *
 * Run: node tests/web/chat_conversation_switch.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { makeI18nStub } from "./_i18n_stub.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const STORE_SRC = readFileSync(path.join(REPO, "web_ui/static/akana-chat-store.js"), "utf8");
const THREADS_SRC = readFileSync(path.join(REPO, "web_ui/static/akana-chat-threads.js"), "utf8");
// REAL PaneManager — to bind bridge.showConversation/rekey/remove to the real pane
// state instead of a stub (the "4th chat" bug: when a new chat opens, does
// displayedConvId actually switch to the new chat?). Without this the harness could
// not model the pane flow.
const PANES_SRC = readFileSync(path.join(REPO, "web_ui/static/akana-chat-panes.js"), "utf8");

// ── Minimal DOM node (only the surfaces used) ────────────────────────────────
function makeEl(tag = "div") {
  return {
    tagName: String(tag).toUpperCase(),
    children: [],
    _html: "",
    dataset: {},
    style: {},
    attrs: {},
    _text: "",
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

// ── localStorage / sessionStorage stub ───────────────────────────────────────
function makeStorage() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
    clear: () => m.clear(),
  };
}

// ── Test runner infrastructure ───────────────────────────────────────────────
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

// ── Load the modules in a single VM context (store → threads) ────────────────
function loadModules() {
  const localStorage = makeStorage();
  const sessionStorage = makeStorage();
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
    fetch: async () => ({ ok: false, json: async () => ({}) }),
  };
  ctx.window = {
    addEventListener: () => {},
    localStorage,
    sessionStorage,
    // the threads module calls baseUrl()/authHeaders() in its meta fetch; without a
    // stub, window.AkanaCore.baseUrl throws on undefined-access → chatHydrateFromServer
    // falls into catch early and returns false (the merge still runs but the return value misleads).
    AkanaCore: { baseUrl: () => "", authHeaders: () => ({}) },
    // threads/store produce user-facing strings via window.AkanaI18n.t.
    AkanaI18n: makeI18nStub(),
  };
  vm.createContext(ctx);
  vm.runInContext(STORE_SRC, ctx);
  vm.runInContext(THREADS_SRC, ctx);
  vm.runInContext(PANES_SRC, ctx);
  assert.ok(ctx.window.AkanaChatPanes, "AkanaChatPanes failed to load");
  assert.ok(ctx.window.AkanaChatStore, "AkanaChatStore failed to load");
  assert.ok(ctx.window.AkanaChatThreads, "AkanaChatThreads failed to load");
  return ctx;
}

// ── Archive STUB: threads `create` calls this; pure no-op + meta holder ───────
function makeArchiveStub() {
  let items = [];
  let meta = null;
  const inserted = []; // insertConversationLocally calls (locally added to the sidebar)
  let loadCalls = 0; // loadChatArchiveList call count
  return {
    items,
    inserted,
    getLoadCalls: () => loadCalls,
    createArchive() {
      return {
        loadChatArchiveList: () => { loadCalls += 1; },
        // PARALLEL-CHAT: locally add the new conv to the sidebar (eager-create path). This
        // being called = "new chat entered the list" (regression test E12).
        insertConversationLocally: (c) => { inserted.push(c); return true; },
        refreshActiveConversationMeta: () => {},
        refreshConvActivityFromServer: () => {},
        clearConvActivity: () => {},
        getChatArchiveItems: () => items,
        setChatArchiveItems: (v) => { items = v; },
        setActiveConversationHighlight: () => {}, // PARALLEL-CHAT: sidebar instant highlight (no-op stub)
        getActiveConversationMeta: () => meta,
        setActiveConversationMeta: (v) => { meta = v; },
        syncChatThreadBar: () => {},
        deleteConversationApi: async () => {},
        patchConversationApi: async () => {},
        exportConversationMarkdown: () => {},
        openArchiveDrawer: () => {},
        closeArchiveDrawer: () => {},
        wireArchiveChrome: () => {},
        wireThreadBar: () => {},
      };
    },
  };
}

// ── Build a threads instance + a controllable bridge ─────────────────────────
function setup() {
  const ctx = loadModules();
  // REAL PaneManager (identical to AkanaShell's pane state in the real app). The bridge
  // pane ops (show/rekey/remove) bind to it → displayedConvId is ACTUALLY updated, so we
  // can deterministically test the "4th chat" scenario (when a new chat opens, does
  // displayed actually switch to the new chat).
  const paneContainer = makeEl("div");
  const pm = ctx.window.AkanaChatPanes.createPaneManager({
    container: paneContainer,
    createEl: (t) => makeEl(t),
  });
  pm.show(null); // initial empty-new pane (real app: ensurePanes show(null))
  // Conversation → server turns map (source for fetchConversationTurns).
  const turnsByConv = new Map();
  // SUSPEND the fetch of a specific convId (gen-guard / mid-fetch tests).
  const deferred = new Map(); // convId → { promise, resolve }
  let renderCount = 0;
  const log = makeEl("div");
  // abortStream now takes an argument (convId|undefined) → also record the call
  // arguments (concurrent N-streams: a switch must NEVER abort; a delete aborts targeted).
  const calls = {
    abortStream: 0,
    abortStreamArgs: [],
    cancelTurn: [],
    setForeground: [],
    reattach: [],
    syncComposer: [],
    showConversation: [], // PARALLEL-CHAT PANES: which conv's pane was shown
    removeConversation: [], // which conv's pane was removed (delete/archive)
    rekey: [], // [oldId, newId] — when an empty-new chat gains a server id
  };
  // convs for which reattachLiveRow returns true (in-memory live-stream simulation).
  const liveReattachConvs = new Set();

  const bridge = {
    hooks: {
      log,
      logScroll: null,
      setLogLoading: () => {},
      updateEmptyState: () => {},
      scrollLogToBottom: () => {},
      updateSettingsHero: () => {},
      shortConversationId: (id) => id || "yok",
      loadMemoryConversations: () => {},
      appendSystemNotice: () => {},
      showToast: () => {},
    },
    async fetchConversationTurns(convId) {
      if (deferred.has(convId)) await deferred.get(convId).promise;
      const has = turnsByConv.has(convId);
      return { status: has ? 200 : 404, turns: has ? turnsByConv.get(convId) : [] };
    },
    abortConversationTurnsFetch: () => {},
    mapServerMessagesToThread: (turns) => (Array.isArray(turns) ? turns.slice() : []),
    chatRenderMessage: () => { renderCount += 1; log.appendChild(makeEl("div")); },
    abortStream: (convId) => { calls.abortStream += 1; calls.abortStreamArgs.push(convId); },
    setForegroundConversation: (convId) => { calls.setForeground.push(convId); },
    // PARALLEL-CHAT PANES: show the target chat's pane (AkanaShell in reality;
    // in the harness it only records the call). isConversationStreamActive is already
    // modeled via liveReattachConvs (markLiveStream).
    showConversation: (convId) => { calls.showConversation.push(convId); pm.show(convId); },
    removeConversation: (convId) => { calls.removeConversation.push(convId); pm.remove(convId); },
    rekeyConversation: (oldId, newId) => { calls.rekey.push([oldId, newId]); pm.rekey(oldId, newId); },
    reattachLiveRow: (convId) => {
      calls.reattach.push(convId);
      return liveReattachConvs.has(convId);
    },
    // Is there a live stream in memory? (transport seam) — same set as markLiveStream:
    // a live-streaming conv is both reattachable and considered stream-active.
    isConversationStreamActive: (convId) => liveReattachConvs.has(convId),
    syncComposerForDisplayed: (convId) => { calls.syncComposer.push(convId); },
    resumeActiveTurn: async () => false,
    probeActiveTurn: async () => null,
    cancelActiveTurnOnServer: async (id) => { calls.cancelTurn.push(id); },
  };

  const archiveStub = makeArchiveStub();
  ctx.window.AkanaChatArchive = archiveStub;
  const T = ctx.window.AkanaChatThreads.create(bridge);

  return {
    T,
    bridge,
    log,
    calls,
    turnsByConv,
    deferred,
    // fetch that returns a server conv id for new-chat eager-create (sidebar fix).
    setFetchConvId: (id) => {
      ctx.fetch = async (url) =>
        typeof url === "string" && /\/conversations$/.test(url)
          ? { ok: true, json: async () => ({ id }) }
          : { ok: false, json: async () => ({}) };
    },
    archiveInserted: () => archiveStub.inserted, // convs locally added to the sidebar
    archiveLoadCalls: () => archiveStub.getLoadCalls(),
    // Mark a conv as "has a live stream in memory" → reattachLiveRow returns true.
    markLiveStream: (convId) => liveReattachConvs.add(convId),
    // REAL pane state: displayed conv id (verification for the 4th-chat scenario).
    displayedConvId: () => pm.displayedConvId(),
    // VM window — the E13 optimistic-nav test uses it to stub
    // AkanaSettings.restoreConversationLlm (provided by akana-settings.js in the real app; absent in the harness).
    win: ctx.window,
    pm,
    getRenderCount: () => renderCount,
    resetRenderCount: () => { renderCount = 0; },
    store: () => T.getChatStore(),
    // Helper: add a thread directly to the store and optionally make it active.
    seedThread(convId, { active = false, messages = [], title = "Yeni sohbet" } = {}) {
      const s = T.getChatStore();
      const tid = `seed-${convId || "null"}-${Object.keys(s.threads).length}`;
      s.threads[tid] = {
        id: tid, profile: "cursor",
        conversationId: convId || null, title,
        updatedAt: Date.now(), messages: messages.slice(),
      };
      if (active) s.activeByProfile.cursor = tid;
      return s.threads[tid];
    },
  };
}

function deferral() {
  let resolve;
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
}

// ═════════════════════════════════════════════════════════════════════════════
// A. STORE · activateThreadForConversation — cannibalization shield (Bug 2)
// ═════════════════════════════════════════════════════════════════════════════
await check("A1 activates the existing convId thread (does not create a new thread)", () => {
  const h = setup();
  const t = h.seedThread("A", { active: false, messages: [{ kind: "user", text: "hi" }] });
  const before = Object.keys(h.store().threads).length;
  const got = h.T.activateThreadForConversation("A");
  assert.equal(got, t, "should return the same thread object");
  assert.equal(h.store().activeByProfile.cursor, t.id, "active pointer should move to the A thread");
  assert.equal(Object.keys(h.store().threads).length, before, "no new thread should be created");
});

await check("A2 convId not present locally · does NOT clobber a NON-EMPTY active thread → new thread", () => {
  const h = setup();
  const y = h.seedThread("Y", { active: true, messages: [{ kind: "user", text: "y-msg" }], title: "Y başlık" });
  const got = h.T.activateThreadForConversation("X");
  assert.notEqual(got.id, y.id, "a NEW thread should be created for X (Y must not be cannibalized)");
  assert.equal(got.conversationId, "X", "the new thread should be bound to X");
  // Y must be preserved: still in the store, messages and conversationId intact.
  assert.ok(h.store().threads[y.id], "the Y thread should stay in the store");
  assert.equal(h.store().threads[y.id].conversationId, "Y", "Y.conversationId must not be corrupted");
  assert.equal(h.store().threads[y.id].messages.length, 1, "Y messages should be preserved");
});

await check("A3 convId not present locally · reuses an EMPTY+UNBOUND active thread", () => {
  const h = setup();
  const empty = h.seedThread(null, { active: true, messages: [], title: "Yeni sohbet" });
  const before = Object.keys(h.store().threads).length;
  const got = h.T.activateThreadForConversation("X");
  assert.equal(got.id, empty.id, "an empty+unbound active thread should be reused (no orphan)");
  assert.equal(got.conversationId, "X", "should be bound to X");
  assert.equal(Object.keys(h.store().threads).length, before, "no new thread should be created");
});

await check("A4 convId not present locally · does NOT clobber an EMPTY but BOUND (Y) active thread", () => {
  const h = setup();
  // A thread bound to Y but with no messages yet (e.g. a freshly opened server chat).
  const y = h.seedThread("Y", { active: true, messages: [], title: "Yeni sohbet" });
  const got = h.T.activateThreadForConversation("X");
  assert.notEqual(got.id, y.id, "a thread bound to Y should not be reused for X");
  assert.equal(h.store().threads[y.id].conversationId, "Y", "the Y binding should be preserved");
});

// ═════════════════════════════════════════════════════════════════════════════
// B. STORE · purge + conversationId helpers
// ═════════════════════════════════════════════════════════════════════════════
await check("B1 purgeConversationFromChatStore removes the matching thread + clears the active pointer", () => {
  const h = setup();
  const a = h.seedThread("A", { active: true, messages: [{ kind: "user", text: "x" }] });
  h.T.purgeConversationFromChatStore("A");
  assert.ok(!h.store().threads[a.id], "the A thread should be removed");
  assert.ok(!h.store().activeByProfile.cursor, "the active pointer should be cleared");
});

await check("B2 conversationIdForMemory reflects the active thread's conversationId", () => {
  const h = setup();
  h.seedThread("A", { active: true });
  assert.equal(h.T.conversationIdForMemory(), "A");
  const b = h.seedThread("B", { active: false });
  h.store().activeByProfile.cursor = b.id;
  assert.equal(h.T.conversationIdForMemory(), "B", "should reflect the change when active changes");
});

// ═════════════════════════════════════════════════════════════════════════════
// C. THREADS · syncConversationLogFromServer — ROOT BUG repro (Bug 1)
// ═════════════════════════════════════════════════════════════════════════════
await check("C1 sync(A) · while B is active, does NOT corrupt B, updates A (root repro)", async () => {
  const h = setup();
  // A thread (exists locally, not active). B is a new empty chat (active).
  const a = h.seedThread("A", { active: false, messages: [] });
  const b = h.seedThread("B", { active: true, messages: [] });
  h.turnsByConv.set("A", [{ kind: "user", text: "A-1" }, { kind: "assistant", text: "A-2" }]);
  const ok = await h.T.syncConversationLogFromServer("A");
  assert.equal(ok, true, "sync should succeed for A");
  // CRITICAL: the active thread B must not be blindly overwritten.
  assert.equal(b.conversationId, "B", "B.conversationId must NOT leak into A (root bug)");
  assert.equal(b.messages.length, 0, "B messages must NOT be filled with A's turns");
  // A must be updated correctly.
  assert.equal(a.conversationId, "A");
  assert.equal(a.messages.length, 2, "A's messages should be written from the server");
});

await check("C2 sync(A) · while A is active, updates A normally", async () => {
  const h = setup();
  const a = h.seedThread("A", { active: true, messages: [] });
  h.turnsByConv.set("A", [{ kind: "user", text: "x" }]);
  const ok = await h.T.syncConversationLogFromServer("A");
  assert.equal(ok, true);
  assert.equal(a.messages.length, 1, "active A should be updated (no regression)");
});

await check("C3 sync(A) · A NOT present locally, B active → no-op, B preserved", async () => {
  const h = setup();
  const b = h.seedThread("B", { active: true, messages: [{ kind: "user", text: "b" }] });
  h.turnsByConv.set("A", [{ kind: "user", text: "a" }]);
  const ok = await h.T.syncConversationLogFromServer("A");
  assert.equal(ok, false, "should return false when the A thread does not exist");
  assert.equal(b.conversationId, "B", "B must not be corrupted");
  assert.equal(b.messages.length, 1, "B messages should be preserved");
});

await check("C4 sync(empty) → false", async () => {
  const h = setup();
  assert.equal(await h.T.syncConversationLogFromServer(""), false);
  assert.equal(await h.T.syncConversationLogFromServer(null), false);
});

await check("C5 sync(A) · 404 → false, touches no thread", async () => {
  const h = setup();
  const a = h.seedThread("A", { active: true, messages: [{ kind: "user", text: "keep" }] });
  // A was not added to turnsByConv → fetchConversationTurns returns 404.
  const ok = await h.T.syncConversationLogFromServer("A");
  assert.equal(ok, false);
  assert.equal(a.messages.length, 1, "existing messages should be preserved on 404");
});

// ═════════════════════════════════════════════════════════════════════════════
// D. THREADS · reloadConversationLogFromServer — stale-reload shield (Bug 3)
// ═════════════════════════════════════════════════════════════════════════════
await check("D1 reload(A) · active bound to B → false, A is not painted to the log", async () => {
  const h = setup();
  const b = h.seedThread("B", { active: true, messages: [] });
  h.turnsByConv.set("A", [{ kind: "user", text: "a" }, { kind: "assistant", text: "a2" }]);
  h.resetRenderCount();
  const ok = await h.T.reloadConversationLogFromServer("A");
  assert.equal(ok, false, "a stale reload should bail while active is bound to another conversation");
  assert.equal(h.getRenderCount(), 0, "A's turns should not be painted to the live log");
  assert.equal(b.conversationId, "B", "B.conversationId should not leak into A");
});

await check("D2 reload(A) · active A → renders, true", async () => {
  const h = setup();
  h.seedThread("A", { active: true, messages: [] });
  h.turnsByConv.set("A", [{ kind: "user", text: "a" }, { kind: "assistant", text: "a2" }]);
  h.resetRenderCount();
  const ok = await h.T.reloadConversationLogFromServer("A");
  assert.equal(ok, true);
  assert.equal(h.getRenderCount(), 2, "A's 2 messages should be rendered");
});

await check("D3 reload(A) · active UNBOUND (null) → bind + render (first-load path)", async () => {
  const h = setup();
  const fresh = h.seedThread(null, { active: true, messages: [] });
  h.turnsByConv.set("A", [{ kind: "user", text: "a" }]);
  const ok = await h.T.reloadConversationLogFromServer("A");
  assert.equal(ok, true, "binding to an unbound active thread should be allowed");
  assert.equal(fresh.conversationId, "A");
});

await check("D4 reload(A) · switch to B DURING the fetch → bail (deterministic race)", async () => {
  const h = setup();
  const a = h.seedThread("A", { active: true, messages: [] });
  const b = h.seedThread("B", { active: false, messages: [] });
  h.turnsByConv.set("A", [{ kind: "user", text: "a" }]);
  // Suspend A's fetch; during the await switch active to B, then resolve.
  const d = deferral();
  h.deferred.set("A", d);
  h.resetRenderCount();
  const p = h.T.reloadConversationLogFromServer("A");
  h.store().activeByProfile.cursor = b.id; // mid-fetch switch
  d.resolve();
  const ok = await p;
  assert.equal(ok, false, "reload should bail if a switch happened during the await");
  assert.equal(h.getRenderCount(), 0, "the wrong conversation's turns should not be painted");
  assert.equal(b.conversationId, "B", "B must not be corrupted");
  assert.equal(a.messages.length, 0, "A must not be written via this path either");
});

// ═════════════════════════════════════════════════════════════════════════════
// E. THREADS · switchChatConversation — isolation + gen guard (integration)
// ═════════════════════════════════════════════════════════════════════════════
await check("E1 switch(X) · X present locally → activate + render", async () => {
  const h = setup();
  h.seedThread("Y", { active: true, messages: [] });
  h.seedThread("X", { active: false, messages: [] });
  h.turnsByConv.set("X", [{ kind: "user", text: "x1" }]);
  h.resetRenderCount();
  await h.T.switchChatConversation("X");
  assert.equal(h.T.conversationIdForMemory(), "X", "X should be active");
  assert.ok(h.getRenderCount() >= 1, "X's turns should be rendered");
});

await check("E2 switch(X) · X NOT present locally, active Y(non-empty) → Y preserved, X new thread", async () => {
  const h = setup();
  const y = h.seedThread("Y", { active: true, messages: [{ kind: "user", text: "y" }] });
  h.turnsByConv.set("X", [{ kind: "user", text: "x" }]);
  await h.T.switchChatConversation("X");
  assert.equal(h.T.conversationIdForMemory(), "X", "X should be active");
  assert.ok(h.store().threads[y.id], "the Y thread should stay in the store (must not be cannibalized)");
  assert.equal(h.store().threads[y.id].conversationId, "Y", "the Y binding should be preserved");
  assert.equal(h.store().threads[y.id].messages.length, 1, "Y messages should be preserved");
});

await check("E3 rapid consecutive switch · a stale switch's late hydrate does NOT clobber the new chat", async () => {
  const h = setup();
  h.seedThread("Y", { active: true, messages: [] });
  h.turnsByConv.set("X", [{ kind: "user", text: "x1" }, { kind: "user", text: "x2" }]);
  h.turnsByConv.set("Z", [{ kind: "user", text: "z1" }]);
  // Suspend X's fetch → switch(X) waits in hydrate.
  const dx = deferral();
  h.deferred.set("X", dx);
  const pX = h.T.switchChatConversation("X"); // gen=1, pending
  await h.T.switchChatConversation("Z");      // gen=2, finishes immediately (Z active)
  dx.resolve();                                // X resolves late
  await pX;                                     // gen mismatch → returns early
  assert.equal(h.T.conversationIdForMemory(), "Z", "the last switch (Z) should stay active");
  // The Z thread must not be polluted with X's messages.
  const active = h.T.chatActiveThread();
  assert.equal(active.conversationId, "Z", "the active thread should be bound to Z");
  assert.ok(
    !active.messages.some((m) => String(m.text || "").startsWith("x")),
    "the Z thread should not contain X's messages (gen guard)",
  );
});

await check("E4 REPRO(user): NEW CHAT while switch(A) is in flight · A's late hydrate does NOT clobber", async () => {
  const h = setup();
  // A: a conversation not present locally, with messages on the server (the user's "summary" chat).
  h.turnsByConv.set("A", [{ kind: "user", text: "a-ozet" }, { kind: "assistant", text: "a-yanit" }]);
  const da = deferral();
  h.deferred.set("A", da); // A's hydrate fetch is SUSPENDED → switch waits in hydrate
  const pA = h.T.switchChatConversation("A"); // gen=1, pending (has NOT painted its messages yet)
  // The user clicks "+" / new chat (before A's hydrate returns).
  await h.T.chatStartNewThread({ force: true, localOnly: true });
  const renderBefore = h.getRenderCount();
  const newActive = h.T.chatActiveThread();
  da.resolve(); // A's hydrate resolves LATE → stale-switch clobber ATTEMPT
  await pA; // _switchGen bump (chatStartNewThread) → gen mismatch → returns early
  // The stale switch must NOT paint A's messages into the new chat's log (user
  // report: "old messages appear in the new chat"). The conv must not shift to A either.
  assert.equal(
    h.getRenderCount(), renderBefore,
    "A's messages should not be painted into the new chat's log (clobber prevented)",
  );
  assert.equal(
    h.T.chatActiveThread().id, newActive.id,
    "the active thread should be the new chat (should not switch to A)",
  );
});

// ═════════════════════════════════════════════════════════════════════════════
// E5–E8. THREADS · CONNECTION CEILING — a switch releases the departed chat's CLIENT FETCH
// ─────────────────────────────────────────────────────────────────────────────
// DESIGN EVOLUTION (user-confirmed — "the bug where I can create lots of chats is fixed"):
// The browser holds ~6 HTTP/1.1 connections per host; every live SSE-POST held one
// connection for the ENTIRE turn → with 3-4 parallel chats the pool was exhausted (">3
// parallel chats won't open / stream stays pending"). Solution: on switching to B, release
// the DEPARTED A's POST (abortStream(A)) → the pool frees up, N parallel chats possible.
// #1 FIX (UPDATE): The "abort on switch" design below was REVERTED. In practice the
// abort→detach→resume chain was not reliable (on an early abort the server turn dies
// without persisting → on return stream-active=false → wipe + empty re-render) and the
// user experienced "the response disappears when I switch to another chat". NOW a switch
// does NOT abort the stream; PaneManager keeps each chat alive in its own hidden pane. The
// cost: the ~3-4 parallel stream ceiling on HTTP/1.1 returns (rare; solved later via HTTP/2 / a queue).
await check("E5 switch(B) · switching to B while A is STREAMING does NOT abort A's stream (stays live in the background pane; #1 fix)", async () => {
  const h = setup();
  h.seedThread("A", { active: true, messages: [{ kind: "user", text: "a" }] });
  h.seedThread("B", { active: false, messages: [] });
  h.turnsByConv.set("B", [{ kind: "user", text: "b1" }]);
  await h.T.switchChatConversation("B");
  // #1 FIX: a switch does NOT abort the departed A's stream → A keeps streaming in ITS OWN hidden pane.
  // (the abortStream→detach→resume chain was unreliable: the "response disappears on switch" bug.)
  assert.deepEqual(h.calls.abortStreamArgs, [], "a switch must NOT abort the departed A's stream (#1 fix; stays live in the background pane)");
  // The server turn is not cancelled either (it completes in the background pane anyway).
  assert.deepEqual(h.calls.cancelTurn, [], "a switch must NOT cancel the server turn (it completes in the background)");
  assert.equal(h.T.conversationIdForMemory(), "B", "the displayed conv should be B");
  // The foreground gate must move to B (setConversationId + an early explicit call).
  assert.ok(h.calls.setForeground.includes("B"), "the foreground gate should switch to B");
  // OPTIMISTIC-NAV: the visible pane must switch to B synchronously (not tied to await IO).
  assert.ok(h.calls.showConversation.includes("B"), "B's pane should be shown (synchronous visible switch)");
});

await check("E6 switch(B) · the foreground gate moves to B + the composer syncs to B", async () => {
  const h = setup();
  h.seedThread("A", { active: true, messages: [] });
  h.seedThread("B", { active: false, messages: [] });
  h.turnsByConv.set("B", [{ kind: "user", text: "b" }]);
  await h.T.switchChatConversation("B");
  // setForegroundConversation must have been called with B (the foreground UI is now gated to B).
  assert.ok(
    h.calls.setForeground.filter((x) => x === "B").length >= 1,
    "setForegroundConversation(B) should be called",
  );
  // The composer must sync to the DISPLAYED B (A's global stream must not leave B stuck in
  // STOP) — it reflects the stream state of the chat the user is looking at.
  assert.ok(
    h.calls.syncComposer.includes("B"),
    "syncComposerForDisplayed(B) should be called (composer is foreground-gated)",
  );
});

await check("E7 switch(back to A) · if A is streaming live, the pane is PRESERVED (NO wipe/hydrate/resume)", async () => {
  const h = setup();
  // PARALLEL-CHAT PANES: A's pane is persistent + carries live content. On return there
  // is NO destructive hydrate (the old model did wipe+reattach here and often broke).
  const a = h.seedThread("A", {
    active: false,
    messages: [{ kind: "user", text: "a" }, { kind: "assistant", text: "akan-yanıt" }],
  });
  h.seedThread("B", { active: true, messages: [] });
  h.turnsByConv.set("A", [{ kind: "user", text: "a" }]);
  h.markLiveStream("A"); // A is live in memory → isConversationStreamActive(A) true
  let resumeCalls = 0;
  h.bridge.resumeActiveTurn = async () => { resumeCalls += 1; return false; };
  await h.T.switchChatConversation("A");
  assert.ok(h.calls.showConversation.includes("A"), "A's pane should be shown (showConversation)");
  assert.equal(h.T.conversationIdForMemory(), "A", "the displayed conv should be A");
  assert.equal(a.conversationId, "A", "A's conv should be preserved (the 404-nuke path is not entered)");
  assert.equal(a.messages.length, 2, "A's messages should be preserved (NO wipe)");
  assert.equal(resumeCalls, 0, "resume is NOT needed on a live pane (no double-follower)");
});

await check("E8 switch(C) · C has NO live stream in memory → reattach false → resume path", async () => {
  const h = setup();
  h.seedThread("B", { active: true, messages: [] });
  h.turnsByConv.set("C", [{ kind: "user", text: "c" }]);
  // No markLiveStream for C → reattach false.
  let resumeCalls = 0;
  h.bridge.resumeActiveTurn = async () => { resumeCalls += 1; return false; };
  await h.T.switchChatConversation("C");
  assert.ok(h.calls.reattach.includes("C"), "reattachLiveRow(C) should be attempted");
  assert.equal(resumeCalls, 1, "with no in-memory stream it should fall to the resumeActiveTurn path (F5/another device)");
});

await check("E9 new chat · '+' while A is STREAMING does NOT abort A's stream (stays live in the background pane; #1 fix)", async () => {
  const h = setup();
  h.seedThread("A", { active: true, messages: [{ kind: "user", text: "a" }] });
  await h.T.chatStartNewThread({ force: true, localOnly: true });
  // #1 FIX: '+/new chat' also does NOT abort the departed A's stream (it keeps streaming in the background pane).
  assert.deepEqual(h.calls.abortStreamArgs, [], "a new chat must NOT abort the departed A's stream (#1 fix)");
  // The SERVER turn continues DETACHED → cancelActiveTurnOnServer is NOT called (the response is not lost).
  assert.deepEqual(h.calls.cancelTurn, [], "a new chat must NOT cancel the server turn (it completes in the background)");
  // The foreground gate must first be switched to null (the new empty chat).
  assert.ok(h.calls.setForeground.includes(null), "a new chat should switch the foreground gate to null");
});

await check("E10 new chat · while A is STREAMING, showConversation(null) gives a FRESH pane + A's pane is PRESERVED (stream not aborted, continues; #1 fix)", async () => {
  const h = setup();
  h.seedThread("A", { active: true, messages: [{ kind: "user", text: "a" }] });
  h.markLiveStream("A"); // A is streaming in the background
  await h.T.chatStartNewThread({ force: true, localOnly: true });
  // PARALLEL-CHAT PANES: the new chat shows a FRESH empty pane → A's pane is hidden but
  // PRESERVED. (because hooks.log → displayed-pane getter, WITHOUT showConversation the
  // new chat's innerHTML="" call would clear A's pane — a regression.)
  assert.ok(
    h.calls.showConversation.includes(null),
    "a new chat should call showConversation(null) (a fresh empty pane; not A's)",
  );
  // #1 FIX: A's stream is NOT aborted + its pane is PRESERVED → A keeps streaming in ITS OWN
  // hidden pane; on return the response is there (no longer tied to the fragile abort→resume chain).
  assert.deepEqual(h.calls.abortStreamArgs, [], "A's stream must NOT be aborted (#1 fix; stays live in the background pane)");
  assert.deepEqual(h.calls.cancelTurn, [], "A's server turn must NOT be cancelled (it completes in the background)");
  assert.ok(
    !h.calls.removeConversation.includes("A"),
    "A's pane must NOT be removed (it keeps streaming in the background)",
  );
});

await check("E11 REPRO('4th chat'): new chat while 3 chats are STREAMING → active conv switches SYNCHRONOUSLY to the new chat (not to a streaming chat)", async () => {
  const h = setup();
  // 3 chats, all streaming live; C is displayed.
  h.seedThread("A", { active: false, messages: [{ kind: "user", text: "a" }] });
  h.seedThread("B", { active: false, messages: [{ kind: "user", text: "b" }] });
  h.seedThread("C", { active: true, messages: [{ kind: "user", text: "c" }] });
  h.markLiveStream("A"); h.markLiveStream("B"); h.markLiveStream("C");
  h.bridge.showConversation?.("C"); // C is displayed
  assert.equal(h.T.conversationIdForMemory(), "C", "precondition: C is active");
  // ROOT BUG: activation must be SYNCHRONOUS. The real "+new chat" button (no force).
  // Even WITHOUT awaiting the promise, conversationIdForMemory() must switch to the new empty chat.
  // The old code did activation in an async IIFE (after the eager POST) → without an await
  // it still stayed on C; when the server was slow and the user typed, the message went to C
  // (STREAMING) and got a 409 TURN_BUSY ([JSEND]: dispConv="(empty)" but fromMem=streaming-conv).
  const p = h.T.chatStartNewThread();
  const memSync = h.T.conversationIdForMemory();
  assert.ok(
    !["A", "B", "C"].includes(memSync),
    `the new chat should be active SYNCHRONOUSLY → conversationIdForMemory must NOT be a streaming chat (A/B/C) (BEFORE the await). got=${JSON.stringify(memSync)}`,
  );
  await p;
  // Send guard logic (akana-chat.js): dispConv = displayedConvId ?? mem;
  // busy = isStreamActive(dispConv). The new empty chat has no stream → busy=false →
  // the send goes to the NEW chat (ensureConversationIdReady lazily creates the conv).
  let dispConv = h.T.conversationIdForMemory();
  const d = h.displayedConvId();
  if (d != null) dispConv = d;
  const busy = Boolean(dispConv && h.bridge.isConversationStreamActive?.(dispConv));
  assert.equal(
    busy,
    false,
    `send guard busy should be false. dispConv=${JSON.stringify(dispConv)} displayed=${JSON.stringify(d)} mem=${JSON.stringify(h.T.conversationIdForMemory())}`,
  );
});

await check("E12 REPRO(sidebar): new chat eager-create → conv enters the SIDEBAR + binds to the active thread", async () => {
  const h = setup();
  h.setFetchConvId("NEW9"); // have the server return this conv id
  await h.T.chatStartNewThread(); // non-localOnly → runs the background eager-create
  const pending = h.T.getPendingNewThread?.();
  assert.ok(pending && typeof pending.then === "function", "there should be an eager-create promise");
  await pending; // let the eager-create finish
  // Must have been added to the sidebar (regression: "after the 3rd chat, new chats aren't in the list").
  const inserted = h.archiveInserted().map((c) => c.id);
  assert.ok(
    inserted.includes("NEW9"),
    `the new conv should enter the sidebar (insertConversationLocally). inserted=${JSON.stringify(inserted)}`,
  );
  assert.equal(h.T.conversationIdForMemory(), "NEW9", "eager-create should bind the conv to the active thread");
  // audit BUG-1: eager-create must also rekey the PANE + bind the global. In the old code
  // `wasUnbound` was read AFTER t.conversationId was assigned, so setConversationId/rekey
  // was DEAD CODE → the pane sentinel stayed "" (foreground gate closed).
  assert.equal(
    h.displayedConvId(),
    "NEW9",
    "eager-create should rekey the displayed pane to the real conv-id (the sentinel '' must not remain)",
  );
});

await check("E12b REPRO(coordination): send waits for eager-create → a SECOND conv is not POSTed", async () => {
  const h = setup();
  h.setFetchConvId("NEW7");
  await h.T.chatStartNewThread();
  const pending = h.T.getPendingNewThread?.();
  assert.ok(pending && typeof pending.then === "function", "getPendingNewThread should return a promise (ensureConversationIdReady awaits it)");
  await pending;
  // Eager-create assigned the conv → the send uses the same conv (no double-create/orphan).
  assert.equal(h.T.conversationIdForMemory(), "NEW7", "eager conv is active → the send uses it");
});

// ─────────────────────────────────────────────────────────────────────────────
// E13. THREADS · OPTIMISTIC NAVIGATION — the visible switch does NOT wait for the conv-model load
// ─────────────────────────────────────────────────────────────────────────────
// ROOT BUG (user): "when I click other chats while responses are being generated, it doesn't
// go to those chats". Root cause: at the START of switchChatConversation there was
// `await AkanaSettings.restoreConversationLlm(convId)` → under heavy streaming its
// GET/PUT/loadModelPill REST chain queued for seconds and kept the underlying synchronous
// showConversation() waiting → the visible pane switch STALLED. Solution (the claude-code
// principle "the foreground swap must be on a path independent of compute/IO"): the model
// load is triggered AFTER the visible switch, as a void (non-awaited) call.
// CONTRACT: at the MOMENT restoreConversationLlm is triggered, the pane must have ALREADY switched to the target.
// This test would FAIL on the OLD code (await first) → it locks in optimistic-nav.
await check("E13 OPTIMISTIC-NAV · the conv-model load is triggered AFTER the VISIBLE switch (the await does not block the pane)", async () => {
  const h = setup();
  h.seedThread("A", { active: true, messages: [] });
  h.seedThread("X", { active: false, messages: [] });
  h.turnsByConv.set("X", [{ kind: "user", text: "x" }]);
  // Take a showConversation snapshot at the MOMENT restoreConversationLlm is called → prove
  // the pane switch happened FIRST. async stub: its body runs synchronously up to the first await.
  let showAtRestore = null;
  let restoreArg = null;
  h.win.AkanaSettings = {
    restoreConversationLlm: async (id) => {
      restoreArg = id;
      showAtRestore = h.calls.showConversation.slice();
      return undefined;
    },
  };
  await h.T.switchChatConversation("X");
  assert.equal(restoreArg, "X", "restoreConversationLlm(X) should be triggered (refresh the model pill)");
  assert.notEqual(showAtRestore, null, "restoreConversationLlm should be called (void; not awaited but triggered)");
  assert.ok(
    showAtRestore.includes("X"),
    "OPTIMISTIC-NAV: when restoreConversationLlm is called the pane should ALREADY have switched to X (the await must not block the visible switch)",
  );
});

// ═════════════════════════════════════════════════════════════════════════════
// F. THREADS · deleteConversationById — a background delete must not abort the active stream
// ═════════════════════════════════════════════════════════════════════════════
await check("F1 delete a background chat · the active STREAM is not aborted, the server turn is cancelled", async () => {
  const h = setup();
  const a = h.seedThread("A", { active: true, messages: [{ kind: "user", text: "a-canlı" }] });
  h.seedThread("B", { active: false, messages: [{ kind: "user", text: "b" }] });
  const ok = await h.T.deleteConversationById("B", { confirm: false, quiet: true });
  assert.equal(ok, true);
  // audit C2: the deleted B's CLIENT stream is aborted TARGETED (so it doesn't write to a
  // detached node and leak _streamsByConv) — A's (displayed) stream is UNTOUCHED.
  assert.deepEqual(h.calls.abortStreamArgs, ["B"], "only the deleted B should be aborted, targeted (A untouched)");
  assert.deepEqual(h.calls.cancelTurn, ["B"], "the deleted B's server turn should be cancelled");
  assert.ok(h.calls.removeConversation.includes("B"), "PARALLEL-CHAT: the deleted B's pane should be removed from the DOM");
  // Active A must remain intact.
  assert.equal(h.T.conversationIdForMemory(), "A", "A should remain active");
  assert.equal(a.messages.length, 1, "A messages should be preserved");
  // B must drop out of the store.
  assert.equal(h.T.chatActiveThread().id, a.id, "the active pointer should stay on A");
});

await check("F2 delete the ACTIVE chat · the stream is aborted + returns to a new empty session", async () => {
  const h = setup();
  h.seedThread("A", { active: true, messages: [{ kind: "user", text: "a" }] });
  const ok = await h.T.deleteConversationById("A", { confirm: false, quiet: true });
  assert.equal(ok, true);
  assert.ok(h.calls.abortStream >= 1, "the stream should be aborted when deleting the active chat");
  assert.deepEqual(h.calls.cancelTurn, ["A"], "A's server turn should be cancelled");
  assert.equal(h.T.conversationIdForMemory(), "", "there should be an unbound new session after delete");
});

await check("F3 convId null · resolves to the active chat (not a background one)", async () => {
  const h = setup();
  h.seedThread("A", { active: true, messages: [{ kind: "user", text: "a" }] });
  // null → resolveConversationId resolves to the active A → active-delete path (stream is aborted).
  const ok = await h.T.deleteConversationById(null, { confirm: false, quiet: true });
  assert.equal(ok, true);
  assert.ok(h.calls.abortStream >= 1, "the stream should be aborted when the active chat (null→A) is deleted");
});

// ═════════════════════════════════════════════════════════════════════════════
// F' THREADS · archiveConversationById — ACTIVE archiving must not leave an orphan stream
//    Bug: archive the active chat MID-STREAM → it switches to a new thread but the old
//    SSE keeps pumping in the background (orphan). Symmetric teardown with delete:
//    if active, abort the stream + cancel the server turn. Because archive is REVERSIBLE
//    (not a delete), in BACKGROUND archiving the turn is UNTOUCHED (let the response finish).
// ═════════════════════════════════════════════════════════════════════════════
await check("F4 archive a background chat · the active STREAM is not aborted, the server turn is UNTOUCHED", async () => {
  const h = setup();
  const a = h.seedThread("A", { active: true, messages: [{ kind: "user", text: "a-canlı" }] });
  h.seedThread("B", { active: false, messages: [{ kind: "user", text: "b" }] });
  const ok = await h.T.archiveConversationById("B", { quiet: true });
  assert.equal(ok, true);
  // audit M1: the archived B's CLIENT stream is aborted TARGETED (so it doesn't write to a
  // detached node once the pane is removed) — but A's (displayed) stream is UNTOUCHED.
  assert.deepEqual(h.calls.abortStreamArgs, ["B"], "only the archived B should be aborted, targeted (A untouched)");
  // Archive is reversible → do NOT cancel the background SERVER turn (a delete would do that).
  assert.deepEqual(h.calls.cancelTurn, [], "archive should not cancel the background server turn");
  assert.ok(h.calls.removeConversation.includes("B"), "the archived B's pane should be removed (no orphan/leak)");
  assert.equal(h.T.conversationIdForMemory(), "A", "A should remain active");
  assert.equal(a.messages.length, 1, "A messages should be preserved");
});

await check("F5 archive the ACTIVE chat · the stream is aborted + returns to a new empty session (orphan fix)", async () => {
  const h = setup();
  h.seedThread("A", { active: true, messages: [{ kind: "user", text: "a" }] });
  const ok = await h.T.archiveConversationById("A", { quiet: true });
  assert.equal(ok, true);
  assert.ok(h.calls.abortStreamArgs.includes("A"), "A's stream should be aborted when archiving the active chat (so it is not orphaned)");
  assert.deepEqual(h.calls.cancelTurn, ["A"], "A's server turn should be cancelled fire-and-forget");
  assert.ok(h.calls.removeConversation.includes("A"), "the archived A's pane should be removed");
  assert.equal(h.T.conversationIdForMemory(), "", "there should be an unbound new session after archiving");
});

// ═════════════════════════════════════════════════════════════════════════════
// G. STORE/THREADS · local user-message source + MERGE (loss of the typed message)
//    Bug: type in A → open B → type in B → return to A; the message you typed in A is GONE. Root:
//    the typed message was never written to thread.messages + the server-snapshot writers
//    overwrote it blindly. Fix: recordPendingUserMessage + mergeServerMessages.
// ═════════════════════════════════════════════════════════════════════════════
await check("G1 recordPendingUserMessage · writes a pending user message to the active thread", () => {
  const h = setup();
  const a = h.seedThread("A", { active: true, messages: [] });
  const msg = h.T.recordPendingUserMessage("merhaba A", ["f1"]);
  assert.equal(a.messages.length, 1, "the message should be written to thread.messages (not just the DOM)");
  assert.equal(a.messages[0], msg);
  assert.equal(msg.kind, "user");
  assert.equal(msg.text, "merhaba A");
  assert.deepEqual(msg.fileIds, ["f1"]);
  assert.equal(msg._pendingUser, true, "the pending flag should be set (merge tracks it)");
});

await check("G2 switch(A) · if the server message is NOT there YET, the pending is PRESERVED (root repro)", async () => {
  const h = setup();
  // A: active, the user just typed (pending), but the server turns are still EMPTY
  // (early-persist not visible yet) → switch/hydrate must not delete the pending.
  h.seedThread("A", { active: true, messages: [] });
  h.T.recordPendingUserMessage("kaybolmamalı", []);
  h.turnsByConv.set("A", []); // server 200 but no messages
  const a = h.T.chatActiveThread();
  const ok = await h.T.chatHydrateFromServer("A", a);
  assert.equal(ok, true);
  assert.equal(a.messages.length, 1, "the pending message should be preserved in the merge (server did not reflect it)");
  assert.equal(a.messages[0].text, "kaybolmamalı");
});

await check("G3 sync(A) · when the server reflects the message, the pending is DROPPED → NO duplicate", async () => {
  const h = setup();
  const a = h.seedThread("A", { active: true, messages: [] });
  h.T.recordPendingUserMessage("merhaba", []);
  // The server now returns the same user turn (+ the assistant reply).
  h.turnsByConv.set("A", [{ kind: "user", text: "merhaba" }, { kind: "assistant", text: "selam" }]);
  const ok = await h.T.syncConversationLogFromServer("A");
  assert.equal(ok, true);
  // CRITICAL: a single "merhaba" user message (the pending copy was dropped).
  const users = a.messages.filter((m) => m.kind === "user");
  assert.equal(users.length, 1, "there must be NO duplicate user message (dedupe)");
  assert.equal(a.messages.length, 2, "the server snapshot should be the single source");
  assert.ok(!a.messages.some((m) => m._pendingUser), "the pending flag should not remain");
});

await check("G4 reload(A) · with a pending present and a different last-user turn on the server → the pending is preserved at the end", async () => {
  const h = setup();
  const a = h.seedThread("A", { active: true, messages: [] });
  // The server has an OLD completed turn; the user typed a NEW message (not persisted-visible yet).
  h.turnsByConv.set("A", [{ kind: "user", text: "eski" }, { kind: "assistant", text: "eski-yanit" }]);
  h.T.recordPendingUserMessage("yeni mesaj", []);
  h.resetRenderCount();
  const ok = await h.T.reloadConversationLogFromServer("A");
  assert.equal(ok, true);
  // 2 server + 1 pending = 3; pending last.
  assert.equal(a.messages.length, 3, "the server turns + the trailing pending should be preserved");
  assert.equal(a.messages[a.messages.length - 1].text, "yeni mesaj");
  assert.equal(h.getRenderCount(), 3, "all three messages should be rendered (including the pending)");
});

await check("G4b REPRO(stale local): a pending first message matches the server's EARLY turn → NO duplicate", async () => {
  // Root cause (user report: "after going into Memory and coming back, the first message I
  // sent gets rewritten as if it were the newest message"): due to a chat-store quota
  // write-error ("giving up") the local store goes STALE → it keeps only the first message
  // (_pendingUser); the server, however, is FULL of the later turns. The old merge looked
  // ONLY at the server's LAST user turn and couldn't match the first message → with
  // server.concat it copied the first message to the VERY END. Fix: match the pending against
  // ANY user turn on the server → if confirmed, DROP it.
  const h = setup();
  const a = h.seedThread("A", { active: true, messages: [] });
  h.T.recordPendingUserMessage("ilk mesaj", []); // the single pending frozen in local
  // The server contains the first message + the LATER turns (local is behind).
  h.turnsByConv.set("A", [
    { kind: "user", text: "ilk mesaj" },
    { kind: "assistant", text: "ilk yanıt" },
    { kind: "user", text: "ikinci mesaj" },
    { kind: "assistant", text: "ikinci yanıt" },
  ]);
  const ok = await h.T.reloadConversationLogFromServer("A");
  assert.equal(ok, true);
  const ilk = a.messages.filter((m) => m.kind === "user" && (m.text || "").trim() === "ilk mesaj");
  assert.equal(ilk.length, 1, "the first message must NOT be duplicated (already on the server → the pending is dropped)");
  assert.equal(a.messages.length, 4, "the server snapshot is the single source (4 messages, no copy)");
  assert.equal(
    a.messages[a.messages.length - 1].text,
    "ikinci yanıt",
    "the last message should be the server's, NOT the first message copied to the end",
  );
});

await check("G5 trimmed match · the pending is cleared even if the server text is trimmed (no duplicate)", async () => {
  const h = setup();
  const a = h.seedThread("A", { active: true, messages: [] });
  h.T.recordPendingUserMessage("  boşluklu  ", []);
  h.turnsByConv.set("A", [{ kind: "user", text: "boşluklu" }]);
  const ok = await h.T.syncConversationLogFromServer("A");
  assert.equal(ok, true);
  const users = a.messages.filter((m) => m.kind === "user");
  assert.equal(users.length, 1, "a single copy even on a trimmed match (no duplicate)");
});

await check("G8 REPRO(return from Memory): a STALE pending resurrected from disk is NOT copied to the very END if it doesn't match the server", async () => {
  // Root cause (user: "sometimes the first message I sent shows up as the newest message"):
  // Memory Studio is a SEPARATE page (/memory) → going there and back is a FULL page reload →
  // chatStore is read from scratch from localStorage. A _pendingUser frozen on disk
  // (e.g. quota truncation appended "… (truncated in cache)" to the text; or the post-turn
  // clean merge record never landed) is resurrected as a GHOST. Because its text is not an
  // EXACT match with the server's, the old merge blindly concatenated it to the END of the
  // server list → the first message reappears at the bottom. Fix: a non-matching pending is kept
  // ONLY if it was actually sent in this session (the session set) or if the server snapshot is
  // EMPTY; otherwise (stale ghost + full server) it is DROPPED.
  const h = setup();
  // seedThread writes directly to the store (NOT recordPendingUserMessage) → exactly
  // the "loaded from localStorage, not created in this session" stale pending.
  const a = h.seedThread("A", {
    active: true,
    messages: [
      { kind: "user", text: "ilk mesaj tam metin… (cache'te kırpıldı; tamı sunucuda)", _pendingUser: true },
    ],
  });
  // The server holds the FULL conversation (the untruncated form of the pending + the reply).
  h.turnsByConv.set("A", [
    { kind: "user", text: "ilk mesaj tam metin uzun uzun gerçek içerik" },
    { kind: "assistant", text: "ilk yanıt" },
  ]);
  const ok = await h.T.reloadConversationLogFromServer("A");
  assert.equal(ok, true);
  assert.equal(a.messages.length, 2, "the server snapshot is the single source — a stale pending must not be copied to the end");
  assert.equal(
    a.messages[a.messages.length - 1].text,
    "ilk yanıt",
    "the last message should be the server's (assistant), NOT the resurrected pending",
  );
  assert.ok(!a.messages.some((m) => m._pendingUser), "the stale pending flag should not remain");
});

await check("G8b BRAND-NEW conv · server EMPTY → this session's real pending is PRESERVED (no loss)", async () => {
  // The counterbalance to G8: when the server is empty (a not-yet-persisted new chat), a
  // pending typed in THIS session must NOT be dropped — otherwise the "message disappears in
  // the new chat" regression comes back. The session set + empty-server guard together protect this.
  const h = setup();
  const a = h.seedThread("A", { active: true, messages: [] });
  h.T.recordPendingUserMessage("kaybolmamalı", []); // sent in this session
  h.turnsByConv.set("A", []); // server 200 but still empty
  const ok = await h.T.reloadConversationLogFromServer("A");
  assert.equal(ok, true);
  assert.equal(a.messages.length, 1, "an in-session pending should be preserved on an empty server");
  assert.equal(a.messages[0].text, "kaybolmamalı");
});

await check("G9 provisional paint · on boot the cache is painted IMMEDIATELY, and re-painted WHOLESALE when the server returns", async () => {
  // Phone/remote win: chatRestoreActiveThread paints the localStorage snapshot WITHOUT
  // waiting for the server fetch (the last chat appears immediately instead of a blank screen);
  // when hydrate returns, thread.messages is re-painted WHOLESALE with the server's truth.
  const h = setup();
  const a = h.seedThread("A", {
    active: true,
    messages: [
      { kind: "user", text: "önbellek soru" },
      { kind: "assistant", text: "önbellek yanıt" },
    ],
  });
  // DELAY the server response → prove the provisional is painted BEFORE the await.
  const d = { promise: null, resolve: null };
  d.promise = new Promise((res) => (d.resolve = res));
  h.deferred.set("A", d);
  // The server's final truth: 1 message more than the cache (e.g. arrived from another device).
  h.turnsByConv.set("A", [
    { kind: "user", text: "önbellek soru" },
    { kind: "assistant", text: "önbellek yanıt" },
    { kind: "user", text: "başka cihazdan gelen" },
  ]);
  h.resetRenderCount();
  const p = h.T.chatRestoreActiveThread(); // do NOT await — hydrate is suspended on deferred
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(h.getRenderCount(), 2, "provisional: the 2 cached messages should be painted without waiting for the server");
  d.resolve(); // let the server response arrive
  await p;
  assert.ok(h.getRenderCount() > 2, "when the server returns there should be an authoritative re-paint (wholesale redraw)");
  assert.equal(a.messages.length, 3, "thread.messages should be the server's truth (3 messages)");
  assert.equal(
    a.messages[a.messages.length - 1].text,
    "başka cihazdan gelen",
    "the last message should be the server's (the provisional was replaced wholesale)",
  );
});

await check("G6 two consecutive pendings · only ONE pending at the end (no duplicate user is born)", () => {
  const h = setup();
  const a = h.seedThread("A", { active: true, messages: [] });
  h.T.recordPendingUserMessage("ilk", []);
  h.T.recordPendingUserMessage("ikinci", []);
  const pendings = a.messages.filter((m) => m._pendingUser);
  assert.equal(pendings.length, 1, "at most one pending at the end (single-pending invariant)");
  assert.equal(pendings[0].text, "ikinci", "the most-recent pending should be tracked");
});

await check("G7 REPRO(user): RETURN to the NEW chat B · the server turn is STILL 404 + a live stream in memory → B's message/conv is NOT deleted", async () => {
  const h = setup();
  // Scenario: send in A (streaming) → open NEW chat B + send in B (streaming) →
  // switch to A (okay) → return to B again. Because B is new, its server turns cannot
  // be queried YET (404: the early-persist/eventual-consistency window). Previously
  // chatHydrateFromServer 404 → B.conversationId=null + messages=[] → "no Jarvis message in
  // the new chat + the next send blows up" (user report).
  h.seedThread("A", { active: true, messages: [{ kind: "user", text: "a" }] });
  h.turnsByConv.set("A", [{ kind: "user", text: "a" }]); // A persisted (200)
  const b = h.seedThread("B", {
    active: false,
    messages: [
      { kind: "user", text: "b-soru" },
      { kind: "assistant", text: "B'nin akan yanıtı", _pendingUser: false },
    ],
  });
  h.markLiveStream("B"); // B is streaming LIVE in memory → isConversationStreamActive(B) true
  // "B" is NOT in turnsByConv → the OLD model's hydrate would 404 → and nuke the conv. The NEW
  // model: because B is live, switchChatConversation NEVER enters hydrate (pane persistent +
  // live) → the 404 path is never reached, the conv/messages are preserved.
  await h.T.switchChatConversation("B");
  assert.ok(h.calls.showConversation.includes("B"), "B's pane should be shown (showConversation)");
  assert.equal(
    b.conversationId,
    "B",
    "B.conversationId should be PRESERVED (live pane → the 404-nuke path is not entered; the next message does not 'blow up')",
  );
  assert.equal(
    b.messages.length,
    2,
    "B.messages should be PRESERVED (Jarvis's streaming reply is not lost — no wipe/hydrate)",
  );
  assert.equal(h.T.conversationIdForMemory(), "B", "the displayed conv should be B");
});

// ── Summary ──────────────────────────────────────────────────────────────────
if (failures) {
  console.error(`\nchat_conversation_switch: ${passed} passed, ${failures} FAILED`);
  process.exit(1);
}
console.log(`chat_conversation_switch: ${passed} contracts passed ✓`);
process.exit(0);
