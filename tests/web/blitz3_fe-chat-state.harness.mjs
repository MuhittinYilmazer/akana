/**
 * fe-chat-state regression harness (blitz-3) — no backend, node-vm + fake DOM.
 *
 * Loads the REAL static modules web_ui/static/akana-chat-archive.js and
 * web_ui/static/akana-core.js and asserts five behaviour contracts:
 *
 *   1  A failed DELETE un-tombstones the id → the authoritative reload can restore the
 *      row (removeArchiveRow tombstones optimistically; without this the row is
 *      invisible in the sidebar + search for the rest of the session).
 *   2  Search in the ARCHIVED tab does NOT hit /conversations/search (which excludes
 *      archived rows) — it filters the archived listing client-side → matches show.
 *   3  akana-core.js's launch-param handler strips ONLY the params it consumes,
 *      preserving unknown params (e.g. /memory?view=facts) and the hash.
 *   4  Inline rename: Escape neutralizes the blur-save handler (no PATCH of a cancelled
 *      rename) and a background list re-render does not destroy an in-progress edit.
 *   5  Search-result rows carry the real pinned state (enriched from the last full
 *      load) so unpinning from search results is possible.
 *
 * Run: node tests/web/blitz3_fe-chat-state.harness.mjs
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
const CORE_SRC = readFileSync(path.join(REPO, "web_ui/static/akana-core.js"), "utf8");

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

// ════════════════════════════════════════════════════════════════════════════
// ARCHIVE module loader + app-context
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

  const state = {
    fetchHandler: async () => ({ ok: false, status: 500, json: async () => ({}) }),
    currentConvId: "",
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
    fetch: (url, opts) => state.fetchHandler(url, opts),
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

const rowPresent = (list, id) =>
  list.querySelectorAll(`.chat-archive-item[data-conversation-id="${id}"]`).length > 0;

// ════════════════════════════════════════════════════════════════════════════
// 1. FAILED DELETE UN-TOMBSTONES — the rollback reload can restore the row
// ════════════════════════════════════════════════════════════════════════════
await check("F1a a failed DELETE un-tombstones the id → the reload renders the row again", async () => {
  const h = loadArchive();
  const item = { id: "c1", title: "Chat one" };
  h.archive.renderChatArchiveList([item]);
  assert.ok(rowPresent(h.list, "c1"), "precondition: c1 is rendered");

  // Optimistic removal tombstones c1 → it must not render.
  h.archive.removeArchiveRow("c1");
  h.archive.renderChatArchiveList([item]);
  assert.ok(!rowPresent(h.list, "c1"), "after removeArchiveRow c1 is tombstoned (hidden)");

  // DELETE fails (500) → the caller reloads. Without the fix the tombstone survives and
  // the live server-side conv stays invisible for the session.
  h.state.fetchHandler = async () => ({ ok: false, status: 500, json: async () => ({}) });
  await assert.rejects(() => h.archive.deleteConversationApi("c1"), "a 500 DELETE should throw");
  h.archive.renderChatArchiveList([item]);
  assert.ok(rowPresent(h.list, "c1"), "after a FAILED delete the reload should bring c1 back (un-tombstoned)");
});

await check("F1b a failed DELETE from a network error also un-tombstones", async () => {
  const h = loadArchive();
  const item = { id: "c2", title: "Chat two" };
  h.archive.removeArchiveRow("c2");
  h.state.fetchHandler = async () => { throw new Error("network down"); };
  await assert.rejects(() => h.archive.deleteConversationApi("c2"), "a network error should propagate");
  h.archive.renderChatArchiveList([item]);
  assert.ok(rowPresent(h.list, "c2"), "a network-failed delete should also un-tombstone");
});

await check("F1c a SUCCESSFUL delete keeps the tombstone (no regression of the stale-render fix)", async () => {
  const h = loadArchive();
  const item = { id: "c3", title: "Chat three" };
  h.archive.removeArchiveRow("c3");
  h.state.fetchHandler = async () => ({ ok: true, status: 204, json: async () => ({}) });
  await h.archive.deleteConversationApi("c3"); // resolves
  h.archive.renderChatArchiveList([item]); // a stale fetch trying to bring c3 back
  assert.ok(!rowPresent(h.list, "c3"), "a deleted chat must stay tombstoned even if a stale render includes it");
});

// ════════════════════════════════════════════════════════════════════════════
// 2. ARCHIVED-TAB SEARCH — no server search, client-side match shows results
// ════════════════════════════════════════════════════════════════════════════
await check("F2 search in the ARCHIVED tab does not hit /conversations/search and shows the match", async () => {
  const h = loadArchive();
  const fetchCalls = [];
  h.state.fetchHandler = async (url) => {
    fetchCalls.push(url);
    if (url.includes("/conversations/search")) {
      return { ok: true, status: 200, json: async () => ({ results: [{ conversation_id: "a1", title: "project plan", preview: "" }] }) };
    }
    if (url.includes("/conversations?") && url.includes("archived=true")) {
      return {
        ok: true, status: 200,
        json: async () => ({ conversations: [{ id: "a1", title: "project plan", pinned: false, archived_at: "2026-01-01T00:00:00Z", message_count: 2, last_message_at: "2026-01-01T00:00:00Z" }] }),
      };
    }
    return { ok: false, status: 404, json: async () => ({}) };
  };

  // Flip to the archived view via the real tab wiring, then run the search.
  const tabActive = makeEl("button"); tabActive.className = "chat-archive-tab"; tabActive.dataset.archiveView = "active";
  const tabArchived = makeEl("button"); tabArchived.className = "chat-archive-tab"; tabArchived.dataset.archiveView = "archived";
  h.documentRoot.appendChild(tabActive);
  h.documentRoot.appendChild(tabArchived);
  h.archive.wireArchiveChrome();
  h.search.value = ""; // no query while flipping the tab
  tabArchived.dispatch("click"); // chatArchiveView := "archived" + a background load
  await Promise.resolve(); await Promise.resolve(); await Promise.resolve();

  fetchCalls.length = 0;
  h.search.value = "proj";
  await h.archive.loadChatArchiveList();

  assert.ok(
    !fetchCalls.some((u) => u.includes("/conversations/search")),
    `archived-view search must NOT hit /conversations/search. calls=${JSON.stringify(fetchCalls)}`,
  );
  assert.ok(
    fetchCalls.some((u) => u.includes("archived=true")),
    "archived-view search should load the archived listing",
  );
  assert.ok(rowPresent(h.list, "a1"), "the matching archived chat 'project plan' should render for query 'proj'");
});

// ════════════════════════════════════════════════════════════════════════════
// 4. INLINE RENAME — Escape neutralizes blur-save; background render preserves edit
// ════════════════════════════════════════════════════════════════════════════
function beginRename(h, item) {
  h.archive.renderChatArchiveList([item]);
  const btn = h.list.querySelector(`.chat-archive-item[data-conversation-id="${item.id}"]`);
  assert.ok(btn, "the chat-archive-item button should be rendered");
  btn.dispatch("dblclick"); // → beginArchiveInlineRename replaces the title span with an input
  return h.list.querySelector(".chat-archive-rename-input");
}

await check("F4a Escape then blur does NOT PATCH the cancelled rename", async () => {
  const h = loadArchive();
  const patchBodies = [];
  h.state.fetchHandler = async (url, opts) => {
    if (opts && opts.method === "PATCH") { patchBodies.push(JSON.parse(opts.body)); return { ok: true, status: 200, json: async () => ({}) }; }
    return { ok: true, status: 200, json: async () => ({ conversations: [] }) };
  };
  h.archive.setChatArchiveItems([{ id: "r1", title: "Original" }]);
  const input = beginRename(h, { id: "r1", title: "Original" });
  assert.ok(input, "a rename input should be present after dblclick");
  input.value = "Edited";
  input.dispatch("keydown", { key: "Escape" }); // cancel
  input.dispatch("blur"); // focus loss AFTER cancel — must be a no-op
  await Promise.resolve(); await Promise.resolve();
  assert.equal(
    patchBodies.filter((b) => b && b.title === "Edited").length,
    0,
    "Escape must neutralize the blur-save: the cancelled title must NOT be PATCHed",
  );
});

await check("F4b a background list re-render does NOT destroy an in-progress rename edit", async () => {
  const h = loadArchive();
  h.state.fetchHandler = async () => ({ ok: true, status: 200, json: async () => ({ conversations: [] }) });
  h.archive.setChatArchiveItems([{ id: "r2", title: "Keep" }]);
  const input = beginRename(h, { id: "r2", title: "Keep" });
  assert.ok(input, "a rename input should be present");
  input.value = "Typing in progress";

  // A background refresh (post-turn/titler) re-renders the whole list.
  h.archive.renderChatArchiveList([{ id: "r2", title: "Keep" }]);

  const stillThere = h.list.querySelector(".chat-archive-rename-input");
  assert.ok(stillThere, "the rename input must survive a background re-render");
  assert.equal(stillThere.value, "Typing in progress", "the in-progress edit text must be preserved");
});

// ════════════════════════════════════════════════════════════════════════════
// 5. SEARCH-RESULT PINNED STATE — enriched from the last full load
// ════════════════════════════════════════════════════════════════════════════
await check("F5 a pinned chat found via search keeps pinned:true (unpin possible)", async () => {
  const h = loadArchive();
  h.state.fetchHandler = async (url) => {
    if (url.includes("/conversations/search")) {
      return { ok: true, status: 200, json: async () => ({ results: [{ conversation_id: "p1", title: "Pinned chat", preview: "p" }] }) };
    }
    if (url.includes("/conversations?")) {
      return {
        ok: true, status: 200,
        json: async () => ({ conversations: [{ id: "p1", title: "Pinned chat", pinned: true, archived_at: null, message_count: 1, last_message_at: "2026-01-01T00:00:00Z" }] }),
      };
    }
    return { ok: false, status: 404, json: async () => ({}) };
  };

  // Full (non-search) load first → caches p1 as pinned.
  h.search.value = "";
  await h.archive.loadChatArchiveList();
  assert.equal(h.archive.getChatArchiveItems()[0].pinned, true, "precondition: the full load has p1 pinned");

  // Now search for it → the row must carry the real pinned state.
  h.search.value = "pin";
  await h.archive.loadChatArchiveList();
  const items = h.archive.getChatArchiveItems();
  assert.equal(items.length, 1, "the search should return p1");
  assert.equal(items[0].id, "p1");
  assert.equal(
    items[0].pinned,
    true,
    "a pinned chat found via search must keep pinned:true (so the pin action can unpin)",
  );
  assert.ok(rowPresent(h.list, "p1"), "the search row should render");
});

// ════════════════════════════════════════════════════════════════════════════
// 6. PIN TOGGLE FROM A SEARCH ROW updates _convMetaCache (review finding 3)
//    Toggling pin from a search result re-runs the search branch, which re-enriches
//    pinned from _convMetaCache. If the toggle does not update that cache, the pin
//    appears dead (stuck on the old label) while a query is active.
// ════════════════════════════════════════════════════════════════════════════
await check("F6 pinning from a search result updates the cache so re-enrichment shows pinned:true", async () => {
  const h = loadArchive();
  h.state.fetchHandler = async (url, opts) => {
    if (opts && opts.method === "PATCH") return { ok: true, status: 200, json: async () => ({}) };
    if (url.includes("/conversations/search")) {
      return { ok: true, status: 200, json: async () => ({ results: [{ conversation_id: "q1", title: "Quarterly plan", preview: "" }] }) };
    }
    if (url.includes("/conversations?")) {
      return {
        ok: true, status: 200,
        json: async () => ({ conversations: [{ id: "q1", title: "Quarterly plan", pinned: false, archived_at: null, message_count: 3, last_message_at: "2026-01-01T00:00:00Z" }] }),
      };
    }
    return { ok: false, status: 404, json: async () => ({}) };
  };

  // Full (non-search) load first → caches q1 as NOT pinned.
  h.search.value = "";
  await h.archive.loadChatArchiveList();
  // Search for it → the row renders with a Pin action (pinned:false from cache).
  h.search.value = "plan";
  await h.archive.loadChatArchiveList();
  assert.equal(h.archive.getChatArchiveItems()[0].pinned, false, "precondition: the search row starts unpinned");

  const pinBtn = h.list.querySelector(".chat-archive-action"); // first action = pin
  assert.ok(pinBtn, "the pin action button must be rendered on the search row");
  pinBtn.click(); // → PATCH pinned:true → cache update → loadChatArchiveList (still searching) re-enriches

  // Let the PATCH + fire-and-forget reload settle.
  for (let i = 0; i < 12; i += 1) await new Promise((r) => setTimeout(r, 0));

  assert.equal(
    h.archive.getChatArchiveItems()[0].pinned,
    true,
    "after pinning from a search result the re-enriched row must reflect pinned:true (bug: stale cache → dead toggle)",
  );
});

// ════════════════════════════════════════════════════════════════════════════
// 3. CORE launch-param handler — strip only consumed params, keep the rest + hash
// ════════════════════════════════════════════════════════════════════════════
function loadCore({ pathname = "/chat", search = "", hash = "" }) {
  const replaceCalls = [];
  const byId = {};
  const mkBtn = (id) => { const el = makeEl("button"); el.id = id; byId[id] = el; return el; };
  const btnNew = mkBtn("btn-new-conv");
  const btnWake = mkBtn("btn-wake");
  const msg = makeEl("input"); msg.id = "msg"; byId.msg = msg;

  const location = { pathname, search, hash, protocol: "http:", host: "localhost" };
  const ctxVm = {
    console,
    setTimeout: (fn) => { fn(); return 0; },
    clearTimeout: () => {},
    queueMicrotask: (fn) => fn(),
    requestAnimationFrame: (fn) => fn(),
    URLSearchParams,
    Event: class { constructor(t) { this.type = t; } },
    location,
    history: { replaceState: (s, t, url) => replaceCalls.push(url) },
    document: {
      readyState: "complete",
      getElementById: (id) => byId[id] || null,
      createElement: (t) => makeEl(t),
      addEventListener: () => {},
      body: {},
    },
    window: { addEventListener: () => {}, location },
    localStorage: makeStorage(),
    fetch: async () => ({ ok: false, json: async () => ({}) }),
  };
  ctxVm.window.window = ctxVm.window;
  vm.createContext(ctxVm);
  vm.runInContext(CORE_SRC, ctxVm); // readyState complete → init() runs synchronously
  return { replaceCalls, btnNew, btnWake, msg };
}

await check("F3a /memory?view=facts is NOT stripped (unknown param preserved, no replaceState)", () => {
  const c = loadCore({ pathname: "/memory", search: "?view=facts", hash: "" });
  assert.deepEqual(
    c.replaceCalls,
    [],
    "the launch handler must not rewrite a URL that has no action/text/url/title param (?view= must survive)",
  );
});

await check("F3b ?action=new is consumed: the button fires + the param is stripped", () => {
  // Pre-attach the click listener BEFORE the handler runs (init() fires synchronously
  // during vm load), so we can observe the btn-new-conv click.
  const replaceCalls = [];
  const btnNew = makeEl("button"); btnNew.id = "btn-new-conv";
  let clicks = 0; btnNew.addEventListener("click", () => { clicks += 1; });
  const location = { pathname: "/chat", search: "?action=new", hash: "", protocol: "http:", host: "localhost" };
  const ctxVm = {
    console, setTimeout: (fn) => { fn(); return 0; }, clearTimeout: () => {},
    queueMicrotask: (fn) => fn(), requestAnimationFrame: (fn) => fn(), URLSearchParams,
    Event: class { constructor(t) { this.type = t; } }, location,
    history: { replaceState: (s, t, url) => replaceCalls.push(url) },
    document: { readyState: "complete", getElementById: (id) => (id === "btn-new-conv" ? btnNew : null), createElement: (t) => makeEl(t), addEventListener: () => {}, body: {} },
    window: { addEventListener: () => {}, location }, localStorage: makeStorage(), fetch: async () => ({ ok: false, json: async () => ({}) }),
  };
  ctxVm.window.window = ctxVm.window;
  vm.createContext(ctxVm);
  vm.runInContext(CORE_SRC, ctxVm);
  assert.equal(clicks, 1, "?action=new should click btn-new-conv");
  assert.deepEqual(replaceCalls, ["/chat"], "the consumed action param should be stripped to the bare path");
});

await check("F3c consumed + unknown param + hash: strip action, keep ?view= and #frag", () => {
  const c = loadCore({ pathname: "/chat", search: "?action=new&view=facts", hash: "#frag" });
  assert.deepEqual(
    c.replaceCalls,
    ["/chat?view=facts#frag"],
    "only the consumed param should be dropped; unknown params and the hash must be preserved",
  );
});

// ── Summary ──────────────────────────────────────────────────────────────────
if (failures) {
  console.error(`\nblitz3_fe-chat-state: ${passed} passed, ${failures} FAILED`);
  process.exit(1);
}
console.log(`blitz3_fe-chat-state: ${passed} contracts passed ✓`);
process.exit(0);
