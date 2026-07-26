/**
 * hunt-5 "the sidebar must not lie" — archive-sidebar contracts. node-vm + fake DOM.
 *
 * Loads the REAL web_ui/static/akana-chat-archive.js and locks three live-state bugs:
 *
 *   archive-sidebar-2  After F5 `conversationActivity` is empty, yet turns keep running
 *                      DETACHED on the server. Only the displayed conversation was probed,
 *                      so every other row silently claimed "nothing is happening" for the
 *                      whole (multi-minute) duration of a background job. The list load
 *                      must reconcile the LISTED rows against the server — and it must
 *                      understand the answer a background job actually gives (202, never
 *                      200), or the reconcile erases the badge instead of rebuilding it.
 *   archive-sidebar-3  loadChatArchiveList's two failure paths blanked the list with
 *                      `innerHTML = ""`, bypassing renderChatArchiveList's rename guard —
 *                      a transient fetch failure (triggered by a BACKGROUND turn event)
 *                      destroyed a populated sidebar and an in-progress, unsaved rename.
 *   archive-sidebar-4  The thread-bar pin button PATCHed pinned without updating
 *                      _convMetaCache, so while a search query was active the reloaded
 *                      search row kept rendering the OLD pinned state.
 *
 * Run: node tests/web/hunt5_archive.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { makeI18nStub } from "./_i18n_stub.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const ARCHIVE_SRC = readFileSync(path.join(REPO, "web_ui/static/akana-chat-archive.js"), "utf8");

// ── Minimal single-compound CSS selector matcher (tag + .class + [attr="v"]) ────
function matchSel(node, sel) {
  let s = String(sel).trim();
  const attrs = [];
  const attrRe = /\[([^\]=]+)(?:=["']?([^"'\]]*)["']?)?\]/g;
  let m;
  while ((m = attrRe.exec(s))) attrs.push([m[1], m[2]]);
  s = s.replace(attrRe, "");
  const classes = [];
  const clsRe = /\.([A-Za-z0-9_-]+)/g;
  while ((m = clsRe.exec(s))) classes.push(m[1]);
  s = s.replace(clsRe, "");
  const tag = s.trim();
  if (tag && node.tagName !== tag.toUpperCase()) return false;
  for (const c of classes) if (!node._classes.has(c)) return false;
  for (const [k, v] of attrs) {
    let actual;
    if (k === "class") actual = node.className;
    else if (k.startsWith("data-")) actual = node.dataset[k.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase())];
    else actual = node.getAttribute(k);
    if (v !== undefined) {
      if (String(actual) !== v) return false;
    } else if (actual == null) return false;
  }
  return true;
}

function walk(node, fn) {
  for (const c of node.children || []) {
    fn(c);
    walk(c, fn);
  }
}

// ── Fake DOM element ────────────────────────────────────────────────────────
function makeEl(tag = "div") {
  const el = {
    tagName: String(tag).toUpperCase(),
    children: [],
    parentNode: null,
    _text: "",
    _html: "",
    dataset: {},
    _attrs: {},
    _listeners: {},
    _classes: new Set(),
    style: {},
    hidden: false,
    id: "",
    scrollTop: 0,
    scrollHeight: 0,
    clientHeight: 0,
    value: "",
    maxLength: 0,
    type: "",
  };
  el.classList = {
    add: (...cs) => cs.forEach((c) => el._classes.add(c)),
    remove: (...cs) => cs.forEach((c) => el._classes.delete(c)),
    toggle: (c, on) => {
      const want = on === undefined ? !el._classes.has(c) : !!on;
      if (want) el._classes.add(c);
      else el._classes.delete(c);
      return want;
    },
    contains: (c) => el._classes.has(c),
  };
  Object.defineProperties(el, {
    className: {
      get() { return [...el._classes].join(" "); },
      set(v) { el._classes = new Set(String(v).split(/\s+/).filter(Boolean)); },
    },
    textContent: {
      get() { return el._text; },
      set(v) { el._text = String(v); el.children = []; },
    },
    innerHTML: {
      get() { return el._html; },
      set(v) {
        el._html = String(v);
        if (v === "") { for (const c of el.children) c.parentNode = null; el.children = []; }
      },
    },
    firstChild: { get() { return el.children[0] || null; } },
    nextElementSibling: {
      get() {
        if (!el.parentNode) return null;
        const i = el.parentNode.children.indexOf(el);
        return el.parentNode.children[i + 1] || null;
      },
    },
  });
  el.setAttribute = (k, v) => { el._attrs[k] = String(v); if (k === "id") el.id = String(v); };
  el.getAttribute = (k) => (k in el._attrs ? el._attrs[k] : null);
  el.removeAttribute = (k) => { delete el._attrs[k]; };
  el.appendChild = (c) => { c.parentNode = el; el.children.push(c); return c; };
  el.append = (...cs) => cs.forEach((c) => { c.parentNode = el; el.children.push(c); });
  el.insertBefore = (node, ref) => {
    node.parentNode = el;
    const i = el.children.indexOf(ref);
    if (i < 0) el.children.push(node);
    else el.children.splice(i, 0, node);
    return node;
  };
  el.remove = () => {
    if (el.parentNode) {
      const i = el.parentNode.children.indexOf(el);
      if (i >= 0) el.parentNode.children.splice(i, 1);
      el.parentNode = null;
    }
  };
  el.replaceWith = (node) => {
    if (!el.parentNode) return;
    const i = el.parentNode.children.indexOf(el);
    if (i >= 0) { node.parentNode = el.parentNode; el.parentNode.children[i] = node; }
    el.parentNode = null;
  };
  el.addEventListener = (type, fn) => { (el._listeners[type] ||= []).push(fn); };
  el.removeEventListener = (type, fn) => {
    const a = el._listeners[type];
    if (a) { const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1); }
  };
  el.dispatch = (type, evt) => {
    const e = Object.assign({ preventDefault() {}, stopPropagation() {} }, evt || {});
    for (const fn of (el._listeners[type] || []).slice()) fn(e);
  };
  el.focus = () => {};
  el.select = () => {};
  el.blur = () => el.dispatch("blur");
  el.click = () => el.dispatch("click");
  el.closest = (sel) => { let n = el; while (n) { if (matchSel(n, sel)) return n; n = n.parentNode; } return null; };
  el.querySelectorAll = (sel) => { const out = []; walk(el, (n) => { if (matchSel(n, sel)) out.push(n); }); return out; };
  el.querySelector = (sel) => el.querySelectorAll(sel)[0] || null;
  return el;
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

// ── Test runner ─────────────────────────────────────────────────────────────
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

/** Drain the fire-and-forget promise chains (loadChatArchiveList `void`s its refreshes). */
const settle = async (n = 40) => {
  for (let i = 0; i < n; i++) await new Promise((r) => setTimeout(r, 0));
};

// ── Fetch response shapes ───────────────────────────────────────────────────
const jsonOk = (body, status = 200) => ({ ok: true, status, json: async () => body });
const httpErr = (status) => ({ ok: false, status, json: async () => ({}) });
// The three answers GET /api/v1/chat/active/{id} really gives. Modelling only 200/204 is a
// status world production never produces for a BACKGROUND job: such a job never registers a
// follower buffer, so 202 is the ONLY answer it ever gives — a sidebar that reads 202 as
// "idle" can never show one, and its idle write erases the badge the WS had just painted.
/** 200 — a live SSE follower (the user's own detached turn); its body must be released. */
const liveTurn = () => ({ ok: true, status: 200, body: { cancel: async () => {} }, json: async () => ({}) });
/** 202 — running, nothing to follow: a schedule fire / background_run, or the user's own
 *  voice/blocking/connector turn (kind:"nonstreaming"). Both are ACTIVITY for the sidebar. */
const acceptedTurn = (kind = "background") => ({
  ok: true,
  status: 202,
  json: async () => ({ running: true, followable: false, kind, started_at: Date.now() - 60000 }),
});
/** GET /chat/active with no turn. */
const noTurn = () => ({ ok: true, status: 204, json: async () => ({}) });

const lastPathSegment = (url) => String(url).split("?")[0].split("/").filter(Boolean).pop();

// ════════════════════════════════════════════════════════════════════════════
// Archive module loader + app-context
// ════════════════════════════════════════════════════════════════════════════
function loadArchive() {
  const i18n = makeI18nStub();
  const documentRoot = makeEl("root");
  const byId = {};
  const mkById = (id, tag = "div") => { const el = makeEl(tag); el.id = id; byId[id] = el; documentRoot.appendChild(el); return el; };

  const list = mkById("chat-archive-list", "ul");
  const search = mkById("chat-archive-search", "input");
  mkById("btn-toggle-archive", "button");
  mkById("btn-archive-close", "button");
  mkById("chat-archive-backdrop", "div");
  mkById("chat-thread-bar", "div");
  mkById("chat-thread-title", "span");
  mkById("btn-thread-pin", "button");

  const state = {
    fetchHandler: async () => httpErr(500),
    currentConvId: "",
    calls: [],
  };

  const doc = {
    getElementById: (id) => byId[id] || null,
    createElement: (t) => makeEl(t),
    createElementNS: (_ns, t) => makeEl(t),
    querySelector: (sel) => documentRoot.querySelector(sel),
    querySelectorAll: (sel) => documentRoot.querySelectorAll(sel),
    addEventListener: () => {},
    body: { classList: makeEl().classList },
  };

  const win = {
    addEventListener: () => {},
    matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
    AkanaBus: { on: () => {} },
    AkanaI18n: { t: i18n.t, getLanguage: () => "en" },
    AkanaCore: { baseUrl: () => "", authHeaders: () => ({}), parseApiError: (b, s) => `HTTP ${s}` },
    CSS: { escape: (s) => String(s) },
  };

  const ctxVm = {
    console,
    setTimeout,
    clearTimeout,
    queueMicrotask,
    requestAnimationFrame: () => {}, // no-op → skip scroll restore (needs no scrollHeight)
    URLSearchParams,
    CSS: { escape: (s) => String(s) }, // archive.js uses a bare CSS.escape (not window.CSS)
    document: doc,
    window: win,
    localStorage: makeStorage(),
    fetch: (url, opts) => { state.calls.push(String(url)); return state.fetchHandler(url, opts); },
  };
  ctxVm.window.window = win;
  vm.createContext(ctxVm);
  vm.runInContext(ARCHIVE_SRC, ctxVm);
  assert.ok(win.AkanaChatArchive, "AkanaChatArchive failed to load");

  const toasts = [];
  const appCtx = {
    bridge: { hooks: { showToast: (m, k) => toasts.push([m, k]), shortConversationId: (id) => id || "none" } },
    conversationIdForMemory: () => state.currentConvId,
    chatActiveThread: () => null,
    switchChatConversation: async () => {},
    archiveConversationById: async () => {},
    deleteConversationById: async () => {},
    chatStartNewThread: async () => {},
  };
  const archive = win.AkanaChatArchive.createArchive(appCtx);
  return { archive, list, search, byId, documentRoot, state, toasts, doc, win };
}

const row = (list, id) => list.querySelector(`.chat-archive-item[data-conversation-id="${id}"]`);
const conv = (id, extra = {}) => ({
  id,
  title: `Chat ${id}`,
  preview: "",
  pinned: false,
  archived_at: null,
  message_count: 3,
  last_message_at: "2026-07-20T10:00:00Z",
  updated_at: "2026-07-20T10:00:00Z",
  ...extra,
});

// ════════════════════════════════════════════════════════════════════════════
// archive-sidebar-2 — after F5 the sidebar reconciles EVERY listed row, not just
// the displayed one, so a detached background turn still shows its badge.
// ════════════════════════════════════════════════════════════════════════════
await check("S2a a list load seeds activity badges for NON-displayed conversations", async () => {
  const h = loadArchive();
  h.state.currentConvId = "c1"; // the user is looking at c1; c2/c3/c4 are busy elsewhere
  h.state.fetchHandler = async (url) => {
    const u = String(url);
    if (u.includes("/conversations?")) {
      return jsonOk({ conversations: [conv("c1"), conv("c2"), conv("c3"), conv("c4")] });
    }
    if (u.includes("/chat/queue/")) return jsonOk({ depth: lastPathSegment(u) === "c3" ? 2 : 0 });
    if (u.includes("/chat/active/")) {
      const id = lastPathSegment(u);
      if (id === "c2") return acceptedTurn("background"); // a schedule fire / background_run
      if (id === "c4") return liveTurn(); // the user's own detached (followable) turn
      return noTurn();
    }
    return httpErr(404);
  };

  await h.archive.loadChatArchiveList();
  await settle();

  assert.ok(
    row(h.list, "c2")?.classList.contains("has-remote-activity"),
    "c2 has a RUNNING background job (202 — the only answer such a job ever gives) but its row shows no badge",
  );
  assert.ok(
    row(h.list, "c3")?.classList.contains("has-remote-activity"),
    "c3 has queued messages on the server but its sidebar row shows no activity badge",
  );
  assert.ok(
    row(h.list, "c4")?.classList.contains("has-remote-activity"),
    "c4 has a live detached turn (200) but its sidebar row shows no activity badge",
  );
  assert.ok(
    !row(h.list, "c1")?.classList.contains("has-remote-activity"),
    "c1 is idle — it must not get a badge",
  );
});

await check("S2d the sweep must not ERASE the badge a WS turn_active just painted", async () => {
  const h = loadArchive();
  h.state.currentConvId = "c1";
  h.state.fetchHandler = async (url) => {
    const u = String(url);
    if (u.includes("/conversations?")) return jsonOk({ conversations: [conv("c1"), conv("c2")] });
    if (u.includes("/chat/queue/")) return jsonOk({ depth: 0 });
    // c2's background job is running; the server can only answer 202 for it.
    if (u.includes("/chat/active/")) return lastPathSegment(u) === "c2" ? acceptedTurn() : noTurn();
    return httpErr(404);
  };
  await h.archive.loadChatArchiveList();
  await settle();
  assert.ok(row(h.list, "c2")?.classList.contains("has-remote-activity"), "precondition: c2 reads as busy");
  // The same re-probe the WS turn_completed handler and every list load make: it must
  // CONFIRM the running job, not erase the badge within one cycle.
  await h.archive.refreshConvActivityFromServer("c2");
  await settle();
  assert.ok(
    row(h.list, "c2")?.classList.contains("has-remote-activity"),
    "a running background job was re-read as idle — the badge survives at most one sweep cycle",
  );
});

await check("S2e an UNREADABLE answer must not erase a known-busy row", async () => {
  const h = loadArchive();
  h.state.currentConvId = "c1";
  h.state.fetchHandler = async (url) => {
    const u = String(url);
    if (u.includes("/conversations?")) return jsonOk({ conversations: [conv("c1"), conv("c2")] });
    if (u.includes("/chat/queue/")) return jsonOk({ depth: 0 });
    if (u.includes("/chat/active/")) return lastPathSegment(u) === "c2" ? acceptedTurn() : noTurn();
    return httpErr(404);
  };
  await h.archive.loadChatArchiveList();
  await settle();
  assert.ok(row(h.list, "c2")?.classList.contains("has-remote-activity"), "precondition: c2 reads as busy");
  // The server restarts: /chat/active answers 503. "I cannot tell" is not "it is idle".
  h.state.fetchHandler = async (url) => {
    const u = String(url);
    if (u.includes("/conversations?")) return jsonOk({ conversations: [conv("c1"), conv("c2")] });
    if (u.includes("/chat/queue/")) return jsonOk({ depth: 0 });
    if (u.includes("/chat/active/")) return httpErr(503);
    return httpErr(404);
  };
  await h.archive.refreshConvActivityFromServer("c2");
  await settle();
  assert.ok(
    row(h.list, "c2")?.classList.contains("has-remote-activity"),
    "a 5xx probe erased a badge the server never said was gone",
  );
});

await check("S2b the sweep is bounded (a 50-row list does not fan out unboundedly)", async () => {
  const h = loadArchive();
  h.state.currentConvId = "c0";
  const rows = Array.from({ length: 50 }, (_, i) => conv(`c${i}`));
  h.state.fetchHandler = async (url) => {
    const u = String(url);
    if (u.includes("/conversations?")) return jsonOk({ conversations: rows });
    if (u.includes("/chat/queue/")) return jsonOk({ depth: 0 });
    if (u.includes("/chat/active/")) return noTurn();
    return httpErr(404);
  };
  await h.archive.loadChatArchiveList();
  await settle();
  const probed = new Set(
    h.state.calls.filter((u) => u.includes("/chat/active/")).map((u) => lastPathSegment(u)),
  );
  assert.ok(probed.size >= 2, "the sweep must probe more than just the displayed conversation");
  assert.ok(probed.size <= 24, `the sweep must stay bounded, probed ${probed.size} conversations`);
  assert.ok(probed.has("c0"), "the displayed conversation must always be probed");
});

await check("S2c an idle probe leaves no badge and no crowding entry behind", async () => {
  const h = loadArchive();
  h.state.currentConvId = "c1";
  h.state.fetchHandler = async (url) => {
    const u = String(url);
    if (u.includes("/conversations?")) return jsonOk({ conversations: [conv("c1"), conv("c2")] });
    if (u.includes("/chat/queue/")) return jsonOk({ depth: 0 });
    if (u.includes("/chat/active/")) return noTurn();
    return httpErr(404);
  };
  await h.archive.loadChatArchiveList();
  await settle();
  assert.equal(
    h.list.querySelectorAll(".chat-archive-activity-badge").length,
    0,
    "an all-idle server must produce no activity badges at all",
  );
});

// ════════════════════════════════════════════════════════════════════════════
// archive-sidebar-3 — a transient list-load failure must not wipe the sidebar
// nor an in-progress inline rename.
// ════════════════════════════════════════════════════════════════════════════
async function populated(h) {
  h.state.fetchHandler = async (url) => {
    const u = String(url);
    if (u.includes("/conversations?")) return jsonOk({ conversations: [conv("c1"), conv("c2")] });
    if (u.includes("/chat/queue/")) return jsonOk({ depth: 0 });
    if (u.includes("/chat/active/")) return noTurn();
    return httpErr(404);
  };
  await h.archive.loadChatArchiveList();
  await settle();
  assert.ok(row(h.list, "c1"), "precondition: the list is populated");
}

await check("S3a a failing background refresh does NOT destroy an in-progress rename", async () => {
  const h = loadArchive();
  await populated(h);
  row(h.list, "c1").dispatch("dblclick"); // inline rename opens
  const input = h.list.querySelector(".chat-archive-rename-input");
  assert.ok(input, "precondition: the rename input is mounted");
  input.value = "half-typed title";

  // A background turn_completed triggers a refresh that fails (server restarting).
  h.state.fetchHandler = async () => httpErr(503);
  await h.archive.loadChatArchiveList();
  await settle();

  const still = h.list.querySelector(".chat-archive-rename-input");
  assert.ok(still, "the mid-typing rename input was destroyed by a failed background refresh");
  assert.equal(still.value, "half-typed title", "the typed title must survive");
});

await check("S3b a failing background refresh does NOT blank a populated list", async () => {
  const h = loadArchive();
  await populated(h);
  h.state.fetchHandler = async () => httpErr(503);
  await h.archive.loadChatArchiveList();
  await settle();
  assert.ok(row(h.list, "c1") && row(h.list, "c2"), "a transient 503 blanked the populated sidebar");
});

await check("S3c a THROWN fetch also leaves the populated list and the rename alone", async () => {
  const h = loadArchive();
  await populated(h);
  row(h.list, "c2").dispatch("dblclick");
  assert.ok(h.list.querySelector(".chat-archive-rename-input"), "precondition: rename open");
  h.state.fetchHandler = async () => { throw new Error("network down"); };
  await h.archive.loadChatArchiveList();
  await settle();
  assert.ok(h.list.querySelector(".chat-archive-rename-input"), "a network error destroyed the rename input");
  assert.ok(row(h.list, "c1"), "a network error blanked the populated sidebar");
});

await check("S3d an EMPTY list still reports the failure (no silent blank sidebar)", async () => {
  const h = loadArchive();
  h.state.fetchHandler = async () => httpErr(500);
  await h.archive.loadChatArchiveList();
  await settle();
  const empty = h.list.querySelector(".chat-archive-empty");
  assert.ok(empty && String(empty.textContent).trim(), "an initial load failure must still show an error row");
});

// ════════════════════════════════════════════════════════════════════════════
// archive-sidebar-4 — the thread-bar pin path keeps _convMetaCache in step, so a
// search row does not keep rendering the OLD pinned state.
// ════════════════════════════════════════════════════════════════════════════
await check("S4 unpinning from the thread bar while a search is active updates the search row", async () => {
  const h = loadArchive();
  h.state.currentConvId = "c1";
  // 1. Full load caches pinned:true for c1.
  h.state.fetchHandler = async (url) => {
    const u = String(url);
    if (u.includes("/conversations?")) return jsonOk({ conversations: [conv("c1", { pinned: true, title: "Alpha plan" })] });
    if (u.includes("/chat/queue/")) return jsonOk({ depth: 0 });
    if (u.includes("/chat/active/")) return noTurn();
    return httpErr(404);
  };
  await h.archive.loadChatArchiveList();
  await settle();
  assert.ok(row(h.list, "c1")?.classList.contains("is-pinned"), "precondition: c1 renders pinned");

  h.archive.setActiveConversationMeta({ id: "c1", title: "Alpha plan", pinned: true });
  h.archive.wireThreadBar();

  // 2. A sidebar search query is active → the reload takes the SEARCH branch.
  h.search.value = "alpha";
  let pinnedOnServer = true;
  h.state.fetchHandler = async (url, opts) => {
    const u = String(url);
    if (opts && opts.method === "PATCH") {
      pinnedOnServer = Boolean(JSON.parse(opts.body).pinned);
      return jsonOk({ id: "c1", title: "Alpha plan", pinned: pinnedOnServer });
    }
    if (u.includes("/conversations/search")) {
      return jsonOk({ results: [{ conversation_id: "c1", title: "Alpha plan", preview: "" }] });
    }
    if (u.includes("/conversations/c1")) return jsonOk({ id: "c1", title: "Alpha plan", pinned: pinnedOnServer });
    if (u.includes("/chat/queue/")) return jsonOk({ depth: 0 });
    if (u.includes("/chat/active/")) return noTurn();
    return httpErr(404);
  };

  // 3. Unpin via the thread bar.
  h.byId["btn-thread-pin"].dispatch("click");
  await settle();

  assert.equal(pinnedOnServer, false, "precondition: the PATCH unpinned c1 on the server");
  assert.equal(
    h.archive.getChatArchiveItems()[0].pinned,
    false,
    "the search row still carries the STALE pinned state from _convMetaCache",
  );
  assert.ok(
    !row(h.list, "c1")?.classList.contains("is-pinned"),
    "the search row still renders as pinned after a thread-bar unpin",
  );
});

if (failures) {
  console.error(`hunt5_archive.harness: ${failures} FAILED, ${passed} passed`);
  process.exit(1);
}
console.log(`hunt5_archive.harness: ${passed} archive-sidebar contracts PASSED ✓`);
if (typeof process !== "undefined" && process.exit) process.exit(0);
