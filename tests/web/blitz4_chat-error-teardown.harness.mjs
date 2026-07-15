/**
 * Bug-blitz 4 — chat-error-teardown regression harness (backend-free, node-vm + fake DOM).
 * Loads the REAL web_ui/static/akana-chat-transport.js and akana-chat-threads.js in a VM
 * with a fake DOM and drives the disconnect / cancel / clean-close teardown paths to lock
 * down six verified fixes. Each finding is proved by a discriminating contract: the REAL
 * source must PASS while a synthetic variant that string-reverts ONLY that fix must FAIL
 * (exhibit the bug) — so the test is shown to actually catch the regression (RED) and the
 * fix cures it (GREEN).
 *
 *  1. isForegroundTurnFinalized must NOT treat a disconnect-errored partial row as a
 *     done-finalized turn → a late WS turn_completed can repaint the full answer.
 *  2. Transport-error finalization must write to the CURRENT (post-inline-card) bubble,
 *     stripping the sealed preamble — not the stale pre-seal closure `bubble`.
 *  4. resumeActiveTurn must treat a clean SSE close with no done/error (server-side cancel
 *     from another client) as terminal — drop the empty pending bubble, don't return true.
 *  5. streamChat's clean-close-without-done tail must NOT stamp the green "done" chip and
 *     must fold away the live usage HUD pill.
 *  fe-be-contract-2. streamChat/sendChatBlocking must pass r.status (not r.statusText, ""
 *     over HTTP/2) to parseApiError so the error carries the numeric code.
 *  3. switchChatConversation's streaming fast-path must OWN the reset of the persist-pause
 *     + log-loading latches a superseded switch left set.
 *
 * Run: node tests/web/blitz4_chat-error-teardown.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { makeI18nStub } from "./_i18n_stub.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
// Normalize CRLF→LF so LF-only patch/regex anchors match regardless of the working
// tree's line endings (the source runs identically in the VM either way).
const readLF = (p) => readFileSync(path.join(REPO, p), "utf8").replace(/\r\n/g, "\n");
const TRANSPORT_SRC = readLF("web_ui/static/akana-chat-transport.js");
const STORE_SRC = readLF("web_ui/static/akana-chat-store.js");
const THREADS_SRC = readLF("web_ui/static/akana-chat-threads.js");
const PANES_SRC = readLF("web_ui/static/akana-chat-panes.js");

let passed = 0;
const failures = [];
async function checkAsync(label, fn) {
  try {
    await fn();
    passed += 1;
  } catch (e) {
    failures.push(`${label}: ${e && e.message ? e.message : e}`);
  }
}

/** String-revert a single fix so the variant exhibits the original bug. Asserts the
 *  anchor exists first, so drift in the product code fails loudly. */
function patch(src, from, to) {
  assert.ok(src.includes(from), `patch anchor not found (code drifted): ${JSON.stringify(from.slice(0, 70))}`);
  return src.split(from).join(to);
}

// ── Fake DOM (only the surfaces the driven paths touch) ─────────────────────────
function attrMatch(el, tk) {
  const m = tk.match(/^\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]$/);
  if (!m) return false;
  const attr = m[1];
  if (attr.startsWith("data-")) {
    const key = attr.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
    return el.dataset[key] != null && (m[2] == null || el.dataset[key] === m[2]);
  }
  return el.attrs[attr] != null && (m[2] == null || el.attrs[attr] === m[2]);
}
/** Supports comma lists + COMPOUND selectors (tag + .class + [attr] tokens, AND-ed),
 *  e.g. `.row[data-turn-id="t1"]` and `.bubble-assistant, .bubble-bot`. */
function selMatch(el, sel) {
  if (!el || !el.classList) return false;
  const s = sel.trim();
  if (s.includes(",")) return s.split(",").some((p) => selMatch(el, p));
  const tokens = s.match(/\.[\w-]+|\[[^\]]+\]|^[\w-]+/g);
  if (!tokens) return false;
  for (const tk of tokens) {
    if (tk.startsWith(".")) { if (!el.classList.contains(tk.slice(1))) return false; }
    else if (tk.startsWith("[")) { if (!attrMatch(el, tk)) return false; }
    else if (el.tagName !== tk.toUpperCase()) return false;
  }
  return true;
}
function walk(el, fn) {
  for (const c of el.children || []) { fn(c); walk(c, fn); }
}

function makeEl(tag = "div") {
  const el = {
    tagName: String(tag).toUpperCase(),
    children: [],
    childNodes: [],
    parentNode: null,
    dataset: {},
    attrs: {},
    style: {},
    _text: "",
    _open: false,
    _html: "",
    classList: {
      _s: new Set(),
      add(...c) { c.forEach((x) => this._s.add(x)); },
      remove(...c) { c.forEach((x) => this._s.delete(x)); },
      toggle(c, on) { const w = on === undefined ? !this._s.has(c) : !!on; if (w) this._s.add(c); else this._s.delete(c); return w; },
      contains(c) { return this._s.has(c); },
    },
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); if (v === "") { this.children = []; this.childNodes = []; } },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); if (v === "") { this.children = []; this.childNodes = []; } },
    get className() { return [...this.classList._s].join(" "); },
    set className(v) { this.classList._s = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get open() { return this._open; },
    set open(v) { this._open = !!v; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k] ?? null; },
    removeAttribute(k) { delete this.attrs[k]; },
    hasAttribute(k) { return this.attrs[k] != null; },
    appendChild(c) { c.parentNode = this; this.children.push(c); this.childNodes = this.children; return c; },
    append(...cs) { cs.forEach((c) => this.appendChild(c)); },
    insertBefore(node, ref) {
      const i = this.children.indexOf(ref);
      if (i < 0) this.children.push(node);
      else this.children.splice(i, 0, node);
      this.childNodes = this.children;
      node.parentNode = this;
      return node;
    },
    after(node) {
      const p = this.parentNode;
      if (!p) return;
      const i = p.children.indexOf(this);
      if (i < 0) p.children.push(node);
      else p.children.splice(i + 1, 0, node);
      p.childNodes = p.children;
      node.parentNode = p;
    },
    remove() {
      const p = this.parentNode;
      if (!p) return;
      const i = p.children.indexOf(this);
      if (i >= 0) p.children.splice(i, 1);
      p.childNodes = p.children;
      this.parentNode = null;
    },
    addEventListener() {},
    removeEventListener() {},
    click() {},
    focus() {},
    matches(sel) { return selMatch(this, sel); },
    closest(sel) { let n = this; while (n) { if (selMatch(n, sel)) return n; n = n.parentNode; } return null; },
    contains(node) { let n = node; while (n) { if (n === this) return true; n = n.parentNode; } return false; },
    querySelector(sel) { let out = null; walk(this, (n) => { if (!out && selMatch(n, sel)) out = n; }); return out; },
    querySelectorAll(sel) { const out = []; walk(this, (n) => { if (selMatch(n, sel)) out.push(n); }); return out; },
  };
  return el;
}

/** SSE body whose reader yields each utf8 chunk once, then closes CLEANLY (done). */
function makeSseBody(chunks) {
  const enc = new TextEncoder();
  const frames = chunks.map((c) => enc.encode(c));
  let i = 0;
  return {
    getReader: () => ({
      read: async () => (i < frames.length ? { value: frames[i++], done: false } : { done: true }),
      releaseLock: () => {},
    }),
    cancel: async () => {},
  };
}

/** SSE body whose reader yields each chunk, then THROWS a non-Abort error (models a
 *  mid-stream transport drop → CONN serverError, the unbreakable-response case). */
function makeThrowingSseBody(chunks) {
  const enc = new TextEncoder();
  const frames = chunks.map((c) => enc.encode(c));
  let i = 0;
  return {
    getReader: () => ({
      read: async () => {
        if (i < frames.length) return { value: frames[i++], done: false };
        const e = new Error("network dropped");
        e.name = "TypeError"; // NOT AbortError → real disconnect (CONN) path
        throw e;
      },
      releaseLock: () => {},
    }),
    cancel: async () => {},
  };
}

function makeCtx(fetchImpl, lang = "en") {
  const body = makeEl("body");
  const doc = {
    createElement: (t) => makeEl(t),
    createElementNS: (_ns, t) => { const el = makeEl(t); el.innerHTML = ""; return el; },
    addEventListener: () => {},
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    body,
  };
  const ctx = {
    window: {
      AkanaCore: { baseUrl: () => "http://x", authHeaders: () => ({}), parseApiError: (b, s) => String(s), escapeHtml: (s) => s },
      AkanaMarkdown: { setBubbleMarkdown: (b, txt) => { if (b) b._text = String(txt); }, appendBubbleStreamText: (b, p) => { if (b) b._text = (b._text || "") + p; } },
      AkanaChatRender: {},
      AkanaI18n: makeI18nStub(lang),
      CSS: { escape: (s) => s },
      setTimeout, clearTimeout, setInterval, clearInterval,
      requestAnimationFrame: (fn) => { setTimeout(fn, 0); return 1; },
      cancelAnimationFrame: () => {},
      addEventListener: () => {}, removeEventListener: () => {},
    },
    document: doc,
    navigator: {},
    CSS: { escape: (s) => s },
    requestAnimationFrame: (fn) => { setTimeout(fn, 0); return 1; },
    cancelAnimationFrame: () => {},
    setTimeout, clearTimeout,
    fetch: fetchImpl,
    AbortController,
    TextDecoder, TextEncoder,
    console,
  };
  ctx.window.window = ctx.window;
  return ctx;
}

/** Instantiate a transport from `src`. paneFor defaults to the log so error/finalize
 *  rows land where isForegroundTurnFinalized queries. Returns handles for assertions. */
function loadTransport(src, fetchImpl, overrides = {}, lang = "en") {
  const ctx = makeCtx(fetchImpl, lang);
  vm.runInNewContext(src, ctx);
  const log = makeEl("div");
  const toasts = [];
  const chatCtx = {
    hooks: {
      log,
      logScroll: log,
      paneFor: (id) => log,
      updateEmptyState: () => {},
      stickToBottomIfFollowing: () => {},
      ttsPlayer: null,
      streamTtsParam: () => "",
      showToast: (msg, kind) => toasts.push({ msg, kind }),
      sendBtn: { disabled: false },
      setStreamingUi: () => {},
      cancelVoiceActivity: () => {},
      ...(overrides.hooks || {}),
    },
    chatInFlight: false,
    conversationIdForMemory: () => "conv-1",
    setConversationId: () => {},
    syncConversationLogFromServer: async () => {},
    reloadConversationLogFromServer: async () => {},
    applyChatServerAction: () => {},
    consumePendingImageIds: () => [],
    consumePendingFileIds: () => [],
    ...overrides.chatCtx,
  };
  const inst = ctx.window.AkanaChatTransport.create(chatCtx);
  return { inst, ctx, log, toasts, chatCtx, document: ctx.document };
}

// ──────────────────────────────────────────────────────────────────────────────
// Finding fe-be-contract-2: streamChat passes r.status (numeric), NOT r.statusText
// ("" over HTTP/2), to parseApiError → the error carries the code, not a bare "".
// ──────────────────────────────────────────────────────────────────────────────
async function runFbc2(src) {
  // Proxy 502 with an empty statusText (HTTP/2) + non-JSON body → parseApiError fallback.
  const fetchImpl = async () => ({ ok: false, status: 502, statusText: "", body: null, json: async () => { throw new Error("not json"); } });
  const { inst } = loadTransport(src, fetchImpl);
  let msg = null;
  try {
    await inst.streamChat("hi");
  } catch (e) {
    msg = String(e && e.message);
  }
  return msg;
}

await checkAsync("fbc2: REAL — the thrown error carries the numeric status (502), not a bare empty ''", async () => {
  const msg = await runFbc2(TRANSPORT_SRC);
  assert.equal(msg, "502", `error message must be the numeric status (got ${JSON.stringify(msg)})`);
});

await checkAsync("fbc2: RED variant (statusText) yields a blank error message over HTTP/2", async () => {
  const buggy = patch(
    TRANSPORT_SRC,
    "      // parseApiError's fallback is `HTTP ${status}` — pass the numeric status, NOT\n      // statusText (empty over HTTP/2, e.g. Tailscale Serve → a bare \"HTTP \" message).\n      throw new Error(parseApiError(body, r.status));",
    "      throw new Error(parseApiError(body, r.statusText));",
  );
  const msg = await runFbc2(buggy);
  assert.equal(msg, "", "buggy code must throw a blank '' message (proves the test discriminates)");
});

// ──────────────────────────────────────────────────────────────────────────────
// Finding 1: a disconnect-errored partial row (pending/aria-busy stripped, but turn
// still running detached) must NOT read as "finalized" — else a late WS turn_completed
// skips the repaint and the "⚠ Disconnected" truncated text stays forever.
// ──────────────────────────────────────────────────────────────────────────────
async function runFinding1(src) {
  // meta(turn_id) + one delta, then the transport drops (CONN) mid-answer → the error
  // path finalizes the row with partial text + err chip (pending/aria-busy removed).
  const chunks = [
    'event: meta\ndata: {"turn_id":"t1"}\n\n',
    'event: delta\ndata: {"text":"partial answer"}\n\n',
  ];
  const fetchImpl = async () => ({ ok: true, status: 200, body: makeThrowingSseBody(chunks), json: async () => ({}) });
  const { inst, log } = loadTransport(src, fetchImpl);
  try {
    await inst.streamChat("hi");
  } catch {
    /* CONN serverError is expected — the row is what we assert */
  }
  const row = log.querySelector('[data-turn-id]');
  return { inst, row, finalized: inst.isForegroundTurnFinalized("conv-1", "t1") };
}

await checkAsync("f1: REAL — an error-finalized row is stamped data-turn-error and reads as NOT finalized", async () => {
  const { row, finalized } = await runFinding1(TRANSPORT_SRC);
  assert.ok(row, "the disconnect-finalized row must be in the log");
  assert.equal(row.dataset.turnError, "1", "the error path must stamp data-turn-error on the row");
  assert.equal(finalized, false, "isForegroundTurnFinalized must return false so turn_completed repaints");
});

await checkAsync("f1: RED variant (no data-turn-error guard) mistakes the error row for a done turn", async () => {
  const buggy = patch(
    TRANSPORT_SRC,
    '    if (row.dataset.turnError === "1") return false;\n',
    "",
  );
  const { finalized } = await runFinding1(buggy);
  assert.equal(finalized, true, "buggy code returns true (skips the repaint) — proves the test discriminates");
});

// ──────────────────────────────────────────────────────────────────────────────
// Finding 2: after an inline ask_user card seals the preamble bubble and opens a fresh
// post-card bubble, a transport-error finalization must act on the CURRENT bubble
// (streamCtx.bubble) — stripping the sealed prefix — not the stale pre-seal closure.
// ──────────────────────────────────────────────────────────────────────────────
async function runFinding2(src) {
  // Preamble delta seals bubble0 + opens a FRESH post-card bubble; then the connection
  // drops with NO post-card delta (exact failure_scenario) → the fresh bubble is still
  // pending. The fix un-pends streamCtx.bubble (the fresh one); the bug un-pends the
  // stale closure bubble0 (already un-pended) and leaves the fresh bubble shimmering.
  const chunks = [
    'event: meta\ndata: {"turn_id":"t2"}\n\n',
    'event: delta\ndata: {"text":"preamble "}\n\n',
    // ask_user payload is used as an OBJECT (handler passes payload.question || payload)
    // → an "options" key (not a string "question") keeps it an object so the card seals.
    'event: ask_user\ndata: {"id":"q1","options":["a","b"]}\n\n',
  ];
  const fetchImpl = async () => ({ ok: true, status: 200, body: makeThrowingSseBody(chunks), json: async () => ({}) });
  const { inst, ctx, log } = loadTransport(src, fetchImpl);
  // Provide an ask_user card renderer so maybeRenderAskUserCard → sealBubbleAndAppendCard runs.
  ctx.window.AkanaChatRender.renderAskUserCard = () => { const c = makeEl("div"); c.classList.add("aur-ask"); c.dataset.askId = "q1"; return c; };
  try {
    await inst.streamChat("hi");
  } catch {
    /* CONN serverError is expected */
  }
  const bubbles = log.querySelectorAll(".bubble-bot");
  return {
    pending: log.querySelectorAll(".bubble-bot-pending").length,
    sealedText: bubbles[0] ? bubbles[0]._text : null,
  };
}

await checkAsync("f2: REAL — post-card live bubble is un-pended; the sealed bubble keeps only the preamble", async () => {
  const { pending, sealedText } = await runFinding2(TRANSPORT_SRC);
  assert.equal(pending, 0, "no bubble may be left shimmering (bubble-bot-pending) after the error");
  assert.equal(sealedText, "preamble ", `the sealed bubble must keep only the preamble (got ${JSON.stringify(sealedText)})`);
});

await checkAsync("f2: RED variant (closure `bubble`) leaves the live post-card bubble shimmering + clobbers the sealed bubble", async () => {
  const buggy = patch(TRANSPORT_SRC, "const errBubble = streamCtx.bubble;", "const errBubble = bubble;");
  const { pending, sealedText } = await runFinding2(buggy);
  assert.ok(pending >= 1, "buggy code must leave the live post-card bubble pending (proves the test discriminates)");
  assert.notEqual(sealedText, "preamble ", "buggy code writes to (clobbers) the sealed bubble instead of the live one");
});

// ──────────────────────────────────────────────────────────────────────────────
// Finding 4: resumeActiveTurn on a clean SSE close with NO done/error (server-side
// cancel from another client) must drop the empty pending bubble, not return true.
// ──────────────────────────────────────────────────────────────────────────────
async function runFinding4(src) {
  // /chat/active returns a live SSE Response that closes cleanly with no done/error.
  const fetchImpl = async (url) => {
    if (String(url).includes("/chat/active/")) {
      return { ok: true, status: 200, body: makeSseBody([]), json: async () => ({}) };
    }
    return { ok: false, status: 404, body: null, json: async () => ({}) };
  };
  const { inst, log } = loadTransport(src, fetchImpl, {
    chatCtx: { reloadConversationLogFromServer: async () => {} },
  });
  const resumed = await inst.resumeActiveTurn("conv-1");
  return {
    resumed,
    row: log.querySelector(".row-assistant"),
    pending: log.querySelectorAll(".bubble-bot-pending").length,
  };
}

await checkAsync("f4: REAL — a clean-close cancel drops the empty resumed bubble (no stranded shimmer)", async () => {
  const { resumed, row, pending } = await runFinding4(TRANSPORT_SRC);
  assert.equal(pending, 0, "no empty pending bubble may be left after a clean-close cancel");
  assert.equal(row, null, "the empty resumed row must be removed");
  assert.equal(resumed, false, "resumeActiveTurn must NOT report a silent success for a cancelled turn");
});

await checkAsync("f4: RED variant (no clean-close branch) strands an empty aria-busy bubble forever", async () => {
  const re = /\n      \/\/ CLEAN CLOSE, NO done\/error\/abort[\s\S]*?return false;\r?\n      \}\n/;
  assert.ok(re.test(TRANSPORT_SRC), "f4 revert anchor not found (code drifted)");
  const buggy = TRANSPORT_SRC.replace(re, "\n");
  const { resumed, pending } = await runFinding4(buggy);
  assert.ok(pending >= 1, "buggy code leaves an empty pending bubble (proves the test discriminates)");
  assert.equal(resumed, true, "buggy code returns a silent success");
});

// ──────────────────────────────────────────────────────────────────────────────
// Finding 5: a clean SSE close AFTER partial deltas but WITHOUT `done` must NOT stamp
// the green "done" chip on the truncated text, and must fold away the live usage HUD.
// ──────────────────────────────────────────────────────────────────────────────
async function runFinding5(src) {
  const chunks = [
    'event: meta\ndata: {"turn_id":"t5"}\n\n',
    'event: delta\ndata: {"text":"half a sen"}\n\n',
    'event: usage\ndata: {"prompt":100,"completion":50}\n\n',
  ]; // clean close, NO done frame
  const fetchImpl = async () => ({ ok: true, status: 200, body: makeSseBody(chunks), json: async () => ({}) });
  const { inst, log, document: doc } = loadTransport(src, fetchImpl);
  doc.body.classList.add("show-usage"); // usageDisplayEnabled() gate → HUD pill is created
  await inst.streamChat("hi");
  return {
    doneChip: log.querySelector('[data-state="ok"]'),
    hud: log.querySelector(".turn-hud"),
  };
}

await checkAsync("f5: REAL — no 'done' chip on a truncated clean-close turn; HUD pill folded away", async () => {
  const { doneChip, hud } = await runFinding5(TRANSPORT_SRC);
  assert.equal(doneChip, null, "the green 'done' chip must NOT be stamped on a clean-close-without-done turn");
  assert.equal(hud, null, "the live usage HUD pill must be removed on this path");
});

await checkAsync("f5: RED variant (unconditional 'ok' + no removeTurnHud) stamps 'done' and strands the HUD", async () => {
  let buggy = patch(TRANSPORT_SRC, "      if (!doneMeta) removeTurnHud(streamCtx);\n", "");
  buggy = patch(buggy, "        doneMeta ? \"ok\" : undefined,", "        \"ok\",");
  const fetchImpl = async () => ({ ok: true, status: 200, body: makeSseBody([
    'event: meta\ndata: {"turn_id":"t5"}\n\n',
    'event: delta\ndata: {"text":"half a sen"}\n\n',
    'event: usage\ndata: {"prompt":100,"completion":50}\n\n',
  ]), json: async () => ({}) });
  const { inst, log, document: doc } = loadTransport(buggy, fetchImpl);
  doc.body.classList.add("show-usage");
  await inst.streamChat("hi");
  assert.ok(log.querySelector('[data-state="ok"]'), "buggy code stamps the 'done' chip (proves the test discriminates)");
  assert.ok(log.querySelector(".turn-hud"), "buggy code leaves the HUD pill attached");
});

// ──────────────────────────────────────────────────────────────────────────────
// Finding 3 (akana-chat-threads.js): switchChatConversation's streaming fast-path must
// OWN the reset of chatPersistPaused + log-loading that a SUPERSEDED slower switch left
// set (its gen-guarded finally is skipped). Otherwise every debounced save silently
// drops + the empty-state hero stays hidden until the next full switch.
// ──────────────────────────────────────────────────────────────────────────────
function makeStorage() {
  const m = new Map();
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v)),
    removeItem: (k) => m.delete(k),
    clear: () => m.clear(),
  };
}

function loadThreads(src) {
  const localStorage = makeStorage();
  const sessionStorage = makeStorage();
  const ctx = {
    console, setTimeout, clearTimeout, crypto: globalThis.crypto,
    localStorage, sessionStorage,
    document: { getElementById: () => null, body: { getAttribute: () => null }, addEventListener: () => {} },
    fetch: async () => ({ ok: false, json: async () => ({}) }),
  };
  ctx.window = {
    addEventListener: () => {},
    localStorage, sessionStorage,
    AkanaCore: { baseUrl: () => "", authHeaders: () => ({}) },
    AkanaI18n: makeI18nStub(),
  };
  vm.createContext(ctx);
  vm.runInContext(STORE_SRC, ctx);
  vm.runInContext(src, ctx);
  vm.runInContext(PANES_SRC, ctx);
  return ctx;
}

function setupThreads(src) {
  const ctx = loadThreads(src);
  const log = makeEl("div");
  let loading = false; // logEmpty.dataset.loading mirror (setLogLoading hook)
  const live = new Set(); // convs with a live stream (fast-path gate)
  const archiveStub = {
    createArchive: () => ({
      loadChatArchiveList: () => {},
      insertConversationLocally: () => true,
      refreshActiveConversationMeta: () => {},
      refreshConvActivityFromServer: () => {},
      clearConvActivity: () => {},
      getChatArchiveItems: () => [],
      setChatArchiveItems: () => {},
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
  ctx.window.AkanaChatArchive = archiveStub;

  const deferred = new Map(); // convId → { promise, resolve }
  const bridge = {
    hooks: {
      log, logScroll: null,
      setLogLoading: (v) => { loading = !!v; },
      updateEmptyState: () => {},
      updateSettingsHero: () => {},
      loadMemoryConversations: () => {},
      scrollLogToBottom: () => {},
      shortConversationId: (id) => id || "yok",
      appendSystemNotice: () => {},
      showToast: () => {},
    },
    clearPendingAttachments: () => {},
    async fetchConversationTurns(convId) {
      if (deferred.has(convId)) await deferred.get(convId).promise;
      return { status: 404, turns: [] };
    },
    abortConversationTurnsFetch: () => {},
    mapServerMessagesToThread: (t) => (Array.isArray(t) ? t.slice() : []),
    chatRenderMessage: () => { log.appendChild(makeEl("div")); },
    abortStream: () => {},
    setForegroundConversation: () => {},
    showConversation: () => {},
    removeConversation: () => {},
    rekeyConversation: () => {},
    reattachLiveRow: () => false,
    isConversationStreamActive: (convId) => live.has(convId),
    syncComposerForDisplayed: () => {},
    resumeActiveTurn: async () => false,
    probeActiveTurn: async () => null,
    cancelActiveTurnOnServer: async () => {},
  };
  const T = ctx.window.AkanaChatThreads.create(bridge);
  return {
    T,
    persistPaused: () => T.getChatPersistPaused(),
    loading: () => loading,
    markLive: (convId) => live.add(convId),
    defer: (convId) => {
      let resolve;
      const promise = new Promise((r) => { resolve = r; });
      deferred.set(convId, { promise, resolve });
      return () => resolve();
    },
  };
}

async function runFinding3(src) {
  const h = setupThreads(src);
  h.markLive("B"); // conv B is streaming → switch#2 takes the fast path
  const releaseA = h.defer("A"); // suspend switch#1's hydrate mid-flight
  const switch1 = h.T.switchChatConversation("A"); // idle → sets persistPaused + loading, awaits hydrate
  await Promise.resolve();
  await h.T.switchChatConversation("B"); // streaming fast-path — supersedes switch#1
  releaseA();
  await switch1;
  await new Promise((r) => setTimeout(r, 0));
  return { persistPaused: h.persistPaused(), loading: h.loading() };
}

await checkAsync("f3: REAL — the streaming fast-path clears the persist-pause + log-loading latches", async () => {
  const { persistPaused, loading } = await runFinding3(THREADS_SRC);
  assert.equal(persistPaused, false, "chatPersistPaused must be cleared by the superseding fast-path");
  assert.equal(loading, false, "the log-loading latch must be cleared by the superseding fast-path");
});

await checkAsync("f3: RED variant (fast-path returns without owning the reset) strands both latches", async () => {
  const buggy = patch(
    THREADS_SRC,
    "        // OWN THE RESET (invariant, see staleInFlightSwitch above): this streaming\n        // fast-path supersedes any in-flight switch by bumping _switchGen, so that\n        // switch's gen-guarded finally is SKIPPED. If it already set chatPersistPaused=true\n        // + logEmpty.loading=\"1\" (before its hydrate await), nobody else clears them →\n        // debounced saveChatStore silently drops + the empty-state hero stays hidden.\n        // Clear both latches here before returning (no-op if none were set).\n        try { setChatPersistPaused(false); } catch { /* ignore */ }\n        try { bridge.hooks.setLogLoading?.(false); } catch { /* ignore */ }\n",
    "",
  );
  const { persistPaused, loading } = await runFinding3(buggy);
  assert.equal(persistPaused, true, "buggy code strands chatPersistPaused=true (proves the test discriminates)");
  assert.equal(loading, true, "buggy code strands the log-loading latch");
});

// ── Summary ─────────────────────────────────────────────────────────────────
if (failures.length) {
  console.error(`FAIL blitz4_chat-error-teardown — ${failures.length} failure(s):`);
  for (const f of failures) console.error("  - " + f);
  process.exit(1);
}
console.log(`PASS blitz4_chat-error-teardown — ${passed} checks green (findings 1, 2, 4, 5, fe-be-contract-2, 3; each proved RED→GREEN)`);
process.exit(0);
