/**
 * Chat stream resilience (D14) contract test — no backend, runs with node-vm.
 * Covers:
 *  1. Message-storm shield: fetchConversationTurnsFromServer issues a SINGLE
 *     IN-FLIGHT request per convId (dedupe) + cancelable via AbortController.
 *  2. Resume contract: probeActiveTurn 204 → null (no active turn),
 *     200+body → returns Response; transport calls the correct path
 *     (GET /api/v1/chat/active/{cid}).
 *  3. Tool card compact render contract: duration badge + single-line detail,
 *     <details> default CLOSED, .tool-call + [data-tool-call-id] preserved.
 *  4. Cache-bust ui15 on all /static links in index.html + memory.html.
 *  5. Send↔stop button state machine: submit→STOP, STOP→abort+SEND, busy guard.
 *  6. Tool card: localized label + start→end status update (✓) + dedupe.
 * Run: node tests/web/chat_stream_resilience.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { makeI18nStub } from "./_i18n_stub.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const TRANSPORT_PATH = path.join(REPO, "web_ui/static/akana-chat-transport.js");
const RENDER_PATH = path.join(REPO, "web_ui/static/akana-chat-render.js");
const CSS_PATH = path.join(REPO, "web_ui/static/akana-chat.css");

const transportSrc = readFileSync(TRANSPORT_PATH, "utf8");
const renderSrc = readFileSync(RENDER_PATH, "utf8");
const css = readFileSync(CSS_PATH, "utf8");

// ── Minimal DOM stub (only the surfaces that are used) ──────────────────────────
function makeEl(tag = "div") {
  const el = {
    tagName: String(tag).toUpperCase(),
    children: [],
    childNodes: [],
    dataset: {},
    classList: {
      _s: new Set(),
      add(...c) { c.forEach((x) => this._s.add(x)); },
      remove(...c) { c.forEach((x) => this._s.delete(x)); },
      toggle(c, on) { if (on) this._s.add(c); else this._s.delete(c); },
      contains(c) { return this._s.has(c); },
    },
    style: {},
    attrs: {},
    _text: "",
    _open: false,
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); },
    get className() { return [...this.classList._s].join(" "); },
    set className(v) { this.classList._s = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get open() { return this._open; },
    set open(v) { this._open = !!v; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k] ?? null; },
    removeAttribute(k) { delete this.attrs[k]; },
    appendChild(c) { this.children.push(c); this.childNodes.push(c); c.parentNode = this; return c; },
    append(...cs) { cs.forEach((c) => this.appendChild(c)); },
    insertBefore(node, ref) {
      const i = this.children.indexOf(ref);
      if (i < 0) this.children.push(node);
      else this.children.splice(i, 0, node);
      this.childNodes = this.children;
      node.parentNode = this;
      return node;
    },
    addEventListener() {},
    remove() {},
    querySelector(sel) { return findOne(this, sel); },
    querySelectorAll(sel) { return findAll(this, sel); },
    matches() { return false; },
    closest() { return null; },
  };
  return el;
}

function selMatch(el, sel) {
  if (!el || !el.classList) return false;
  if (sel.startsWith(".")) return el.classList.contains(sel.slice(1));
  if (sel.startsWith("[")) {
    const m = sel.match(/^\[([\w-]+)(?:[~^]?=["']?([^"'\]]*)["']?)?\]$/);
    if (!m) return false;
    const attr = m[1];
    if (attr.startsWith("data-")) {
      const key = attr.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      return el.dataset[key] != null && (m[2] == null || el.dataset[key] === m[2]);
    }
    return el.attrs[attr] != null;
  }
  return false;
}
function walk(el, fn) {
  for (const c of el.children || []) { fn(c); walk(c, fn); }
}
function findOne(root, sel) {
  let out = null;
  walk(root, (n) => { if (!out && selMatch(n, sel)) out = n; });
  return out;
}
function findAll(root, sel) {
  const out = [];
  walk(root, (n) => { if (selMatch(n, sel)) out.push(n); });
  return out;
}

function makeCtx(fetchImpl) {
  const doc = {
    createElement: (t) => makeEl(t),
    createElementNS: (_ns, t) => {
      const el = makeEl(t);
      el.innerHTML = "";
      return el;
    },
    addEventListener: () => {},
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    body: makeEl("body"),
  };
  return {
    window: {
      AkanaCore: {}, AkanaMarkdown: {}, AkanaChatRender: {}, AkanaI18n: makeI18nStub(), CSS: { escape: (s) => s },
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
    setTimeout,
    clearTimeout,
    fetch: fetchImpl,
    AbortController,
    TextDecoder,
    console,
  };
}

// ── Load render first (transport reads window.AkanaChatRender) ──────────────
const renderCtx = makeCtx(async () => ({}));
renderCtx.window.AkanaCore = { escapeHtml: (s) => s };
renderCtx.window.AkanaMarkdown = { setBubbleMarkdown: () => {}, appendBubbleStreamText: (b, p) => { if (b) b._text = (b._text || '') + p; } };
vm.runInNewContext(renderSrc, renderCtx);
const Render = renderCtx.window.AkanaChatRender;
assert.ok(Render, "AkanaChatRender failed to load");

// ── 3. Tool card RADICALLY COMPACT render contract ─────────────────────────────
{
  const card = Render.renderToolCall({
    id: "call-1",
    name: "read_file",
    phase: "end",
    args: { file_path: "/etc/hosts" },
    result: "ok",
    duration_ms: 1500,
  });
  assert.equal(card.tagName, "DETAILS", "tool card must be <details>");
  assert.equal(card.open, false, "tool card must default to CLOSED (detail hidden)");
  assert.ok(card.classList.contains("tool-call"), ".tool-call class must be preserved");
  assert.ok(card.classList.contains("tool-call--compact"), ".tool-call--compact class must be added");
  assert.equal(card.dataset.toolCallId, "call-1", "[data-tool-call-id] must be preserved");
  // Title = HUMAN-READABLE ACTION sentence (not the static 'Dosya okuma').
  const title = findOne(card, ".action-card-title");
  assert.ok(title, "title (.action-card-title) missing");
  assert.equal(title.textContent, "hosts read", "title must be a human-readable action sentence (i18n EN)");
  // Raw command/JSON HIDDEN BY DEFAULT: NO inline arg chip / result preview.
  assert.equal(findOne(card, ".tool-call-chip"), null, "compact card must not have an inline arg chip (hidden)");
  assert.equal(findOne(card, ".tool-call-result-preview"), null, "compact card must not have an inline result preview");
  // Duration badge shows in the single-line summary (1500ms → "1.5 s", i18n EN).
  const dur = findOne(card, ".action-card-duration");
  assert.ok(dur, "duration badge (.action-card-duration) missing");
  assert.equal(dur.textContent, "1.5 s", "duration 1500ms → '1.5 s' must be formatted (i18n EN)");
  // Short-duration ms format.
  const card2 = Render.renderToolCall({ id: "c2", name: "grep", phase: "end", duration_ms: 120 });
  assert.equal(findOne(card2, ".action-card-duration").textContent, "120 ms", "120ms → '120 ms'");
  // No badge at all when the call has no duration.
  const card3 = Render.renderToolCall({ id: "c3", name: "grep", phase: "start" });
  assert.equal(findOne(card3, ".action-card-duration"), null, "no badge for a call without duration");
}

// ── 3d. Tool card detail: MCP unwrap + memory hit cards ───────────────────
{
  const mcpResult = JSON.stringify({
    status: "success",
    value: {
      content: [
        {
          text: JSON.stringify({
            items: [
              { id: "01ABC", type: "Fact", summary: "Kullanıcı İstanbul'da yaşıyor", score: 0.91, trust: "verified" },
              { id: "01DEF", type: "Preference", summary: "Koyu tema tercih ediyor", score: 0.82, trust: "inferred" },
            ],
            explain_id: "01EXPLAIN99",
          }),
        },
      ],
    },
  });
  const card = Render.renderToolCall({
    id: "mem-det",
    name: "akana/memory_search",
    phase: "end",
    args: { query: "instagram dm skill" },
    result: mcpResult,
  });
  card.open = true;
  Render.materializeToolCallDetail(card);
  const title = findOne(card, ".action-card-title");
  assert.equal(title.textContent, "searched memory: instagram dm skill", "provider/tool normalize + action sentence (i18n EN)");
  assert.ok(findOne(card, ".action-card-hit"), "memory hit cards");
  assert.ok(findOne(card, ".action-card-pill"), "query pill");
  assert.equal(findOne(card, ".action-card-raw"), null, "raw JSON not required after MCP unwrap");
}

{
  const card = Render.renderToolCall({
    id: "sh-det",
    name: 'xdg-open "https://www.youtube.com/watch?v=abc"',
    phase: "end",
    result: "ok",
  });
  assert.equal(findOne(card, ".action-card-title").textContent, "opened YouTube video", "shell-as-name must be interpreted (i18n EN)");
}

// SDK shape: empty name + nested args (providerIdentifier / toolName / args.query)
{
  const card = Render.renderToolCall({
    id: "sdk-mem",
    name: "",
    phase: "end",
    args: {
      providerIdentifier: "akana",
      toolName: "memory_search",
      args: { query: "Akana test" },
    },
    result: JSON.stringify({ items: [], explain_id: "01X" }),
  });
  const title = findOne(card, ".action-card-title");
  assert.equal(title.textContent, "searched memory: Akana test", "SDK args → normalize + action sentence (i18n EN)");
  assert.equal(findOne(card, ".action-card-subtitle"), null, "there must be no duplicate subtitle");
  card.open = true;
  Render.materializeToolCallDetail(card);
  const kvKeys = findAll(card, ".action-card-kv dt").map((n) => n.textContent || "");
  assert.ok(!kvKeys.some((k) => /provider|toolname|timeout/i.test(k)), "SDK identity fields must not be printed as KV");
  assert.ok(findOne(card, ".action-card-pill"), "query pill must be shown");
}

// CSS: compact list + duration badge + compact-row rules exist in the source.
assert.ok(css.includes(".action-card-duration"), "CSS .action-card-duration missing");
assert.ok(css.includes(".tool-call + .tool-call"), "CSS consecutive tool-call compact spacing missing");
assert.ok(css.includes(".tool-call--compact"), "CSS .tool-call--compact compact rule missing");

// ── 3b. Human-readable ACTION sentence mapping (derived from tool+input) ─────────────
{
  const act = (call) => Render.toolCallActionSentence(call);
  assert.equal(act({ name: "read_file", args: { file_path: "/a/b/foo.py" } }).text,
    "foo.py read", "read_file → '<file> read' (i18n EN)");
  assert.equal(act({ name: "read_file" }).icon, "📄", "read_file icon 📄");
  assert.equal(act({ name: "run_terminal_cmd", args: { command: "ls -la" } }).text,
    "ran command: ls -la", "terminal → 'ran command: <cmd>' (i18n EN)");
  assert.equal(act({ name: "run_terminal_cmd" }).text, "ran command", "terminal without args → generic verb (i18n EN)");
  assert.equal(act({ name: "web_search", args: { query: "hava durumu" } }).text,
    "searched 'hava durumu'", "web_search → \"searched '<q>'\" (i18n EN)");
  assert.equal(act({ name: "write_file", args: { path: "/x/out.txt" } }).text,
    "wrote to out.txt", "write → 'wrote to <file>' (i18n EN)");
  assert.equal(act({ name: "mcp__akana_memory__memory_search", args: { query: "x" } }).text,
    "searched memory: x", "mcp__-prefixed memory_search must normalize into the memory sentence (i18n EN)");
  assert.equal(act({ name: "codebase_search", args: { query: "foo" } }).text,
    "searched code: foo", "codebase_search → 'searched code: <q>' (i18n EN)");
  // Unknown tool → raw name (no sentence-building), icon 🔧.
  const unk = act({ name: "acme_custom_tool" });
  assert.equal(unk.text, "acme_custom_tool", "unknown tool must be the raw name");
  assert.equal(unk.icon, "🔧", "unknown tool icon 🔧");
}

// ── 3c. Status update: start→end in the SAME batch → ✓ (does not get stuck on running) ──
{
  const body = makeEl("div");
  const bubble = makeEl("div");
  body.appendChild(bubble);
  const ins = (n) => body.insertBefore(n, bubble);
  // start: phase=start, args present, no result → running.
  Render.upsertToolCallCard(body, { id: "tk", name: "read_file", phase: "start", args: { file_path: "/x/foo.py" }, result: null, status: null }, ins);
  let card = findOne(body, "[data-tool-call-id]");
  assert.equal(card.dataset.status, "running", "start event → running");
  assert.equal(findOne(card, ".action-card-title").textContent, "foo.py read", "start action sentence");
  // end: phase=end, result present, status ok → done; PATCH the same id (no new card).
  Render.upsertToolCallCard(body, { id: "tk", name: "read_file", phase: "end", args: null, result: "OK", status: "ok", duration_ms: 50 }, ins);
  const cards = findAll(body, "[data-tool-call-id]");
  assert.equal(cards.length, 1, "same id → single card (dedupe, no second card opened)");
  card = cards[0];
  assert.equal(card.dataset.status, "done", "end event must turn the card to done (BUG fix)");
  assert.equal(findOne(card, ".action-card-status").dataset.status, "done", "status dot must be ✓");
  // Even if end arrives with null args, the action sentence set up at start must NOT be LOST.
  assert.equal(findOne(card, ".action-card-title").textContent, "foo.py read", "end null args must not erase the action sentence");
  // Duration badge must be added at end.
  assert.equal(findOne(card, ".action-card-duration")?.textContent, "50 ms", "end must add the duration badge");
}

// ── 3d. toolCallStatus: done when result is present, even if phase is still 'start' ─────────
{
  assert.equal(Render.toolCallStatus({ phase: "start", result: "X" }), "done", "done when result is present");
  assert.equal(Render.toolCallStatus({ phase: "start" }), "running", "start only → running");
  assert.equal(Render.toolCallStatus({ phase: "start", status: "error" }), "error", "status error → error");
  assert.equal(Render.toolCallStatus({ phase: "end" }), "done", "phase end → done");
}

// ── 3e. appendMemorySources: post-turn "saved" (staging) chips ────────
// When capture is moved to the background, staging sources do not arrive on done;
// they come via WS and are appended under the turn by this function (create-or-extend + dedup + limit of 6).
{
  // 1) Empty msg-body → new .aur-sources + single staging chip (label=key, trust=tool).
  const body = makeEl("div");
  Render.appendMemorySources(body, [{ id: "1", kind: "staging", key: "soyad" }]);
  const row = findOne(body, ".aur-sources");
  assert.ok(row, "appendMemorySources must set up the .aur-sources row");
  const chip0 = findAll(row, ".aur-source-chip");
  assert.equal(chip0.length, 1, "there must be a single staging chip");
  assert.equal(findOne(chip0[0], ".aur-source-text").textContent, "soyad", "chip label must be the key");
  assert.equal(chip0[0].dataset.trust, "tool", "staging trust=tool");
  // 2) Same key again → dedup (no new chip).
  Render.appendMemorySources(body, [{ id: "2", kind: "staging", key: "soyad" }]);
  assert.equal(findAll(body, ".aur-source-chip").length, 1, "same key must be deduped");
  // 3) NEW key on the existing row → extends it, does not create a new row.
  Render.appendMemorySources(body, [{ id: "3", kind: "staging", key: "email" }]);
  assert.equal(findAll(body, ".aur-sources").length, 1, "single .aur-sources (create-or-extend)");
  assert.equal(findAll(body, ".aur-source-chip").length, 2, "new key must be added");
  // 4) Empty input → does not create a row.
  const empty = makeEl("div");
  Render.appendMemorySources(empty, []);
  assert.equal(findOne(empty, ".aur-sources"), null, "no row must be set up for empty input");
}

// ── Load transport ──────────────────────────────────────────────────────────
function loadTransport(fetchImpl) {
  const ctx = makeCtx(fetchImpl);
  ctx.window.AkanaCore = {
    baseUrl: () => "http://x",
    authHeaders: () => ({}),
    parseApiError: (b, s) => String(s),
    escapeHtml: (s) => s,
  };
  ctx.window.AkanaMarkdown = { setBubbleMarkdown: () => {}, appendBubbleStreamText: (b, p) => { if (b) b._text = (b._text || '') + p; } };
  ctx.window.AkanaChatRender = Render;
  vm.runInNewContext(transportSrc, ctx);
  const log = makeEl("div");
  const chatCtx = {
    hooks: {
      log,
      logScroll: log,
      updateEmptyState: () => {},
      stickToBottomIfFollowing: () => {},
      ttsPlayer: null,
      streamTtsParam: () => "",
      showToast: () => {},
      sendBtn: { disabled: false },
    },
    chatInFlight: false,
    conversationIdForMemory: () => "conv-1",
    setConversationId: () => {},
    syncConversationLogFromServer: () => {},
    applyChatServerAction: () => {},
    consumePendingImageIds: () => [],
  };
  const inst = ctx.window.AkanaChatTransport.create(chatCtx);
  // Attach the sandbox document so the test can toggle the token/cost gate
  // (show-usage body class) — usageDisplayEnabled() reads this.
  inst.__doc = ctx.document;
  return inst;
}

// ── 1. Message-storm shield: dedupe + AbortController ──────────────────────
{
  let calls = 0;
  let resolveFetch;
  const gate = new Promise((res) => { resolveFetch = res; });
  const fetchImpl = async (url, opts) => {
    calls += 1;
    assert.ok(/\/messages\?limit=500$/.test(url), "messages path expected");
    assert.ok(opts && opts.signal, "AbortController signal must be passed (cancelable)");
    await gate;
    return { ok: true, status: 200, json: async () => ({ messages: [{ role: "user", content: "hi" }] }) };
  };
  const t = loadTransport(fetchImpl);
  // 5 concurrent calls for the same convId → a SINGLE fetch (dedupe).
  const ps = [t.fetchConversationTurnsFromServer("conv-1"), t.fetchConversationTurnsFromServer("conv-1"),
    t.fetchConversationTurnsFromServer("conv-1"), t.fetchConversationTurnsFromServer("conv-1"),
    t.fetchConversationTurnsFromServer("conv-1")];
  resolveFetch();
  const results = await Promise.all(ps);
  assert.equal(calls, 1, `5 concurrent calls → 1 fetch expected, got ${calls} (storm!)`);
  for (const r of results) assert.equal(r.status, 200, "shared result must be 200");
}

// Second round: after the in-flight one finishes, a re-call issues a new request + abort works.
{
  let calls = 0;
  let aborted = false;
  const fetchImpl = async (url, opts) => {
    calls += 1;
    return await new Promise((resolve, reject) => {
      if (opts?.signal) {
        opts.signal.addEventListener("abort", () => { aborted = true; const e = new Error("abort"); e.name = "AbortError"; reject(e); });
      }
      setTimeout(() => resolve({ ok: true, status: 200, json: async () => ({ messages: [] }) }), 50);
    });
  };
  const t = loadTransport(fetchImpl);
  const p = t.fetchConversationTurnsFromServer("conv-9");
  t.abortConversationTurnsFetch("conv-9");
  const r = await p;
  assert.equal(aborted, true, "abortConversationTurnsFetch must cancel the fetch");
  assert.equal(r.aborted, true, "cancel result must return { aborted:true }");
}

// ── 2. Resume contract: probeActiveTurn 204 → null, 200 → Response ──────────
{
  const seen = [];
  const fetchImpl = async (url) => {
    seen.push(url);
    return { ok: false, status: 204, body: null, json: async () => ({}) };
  };
  const t = loadTransport(fetchImpl);
  const res = await t.probeActiveTurn("conv-1");
  assert.equal(res, null, "204 → no active turn (null)");
  assert.ok(
    seen.some((u) => /\/api\/v1\/chat\/active\/conv-1$/.test(u)),
    "probeActiveTurn must call the correct path (GET /api/v1/chat/active/{cid})",
  );
}
{
  // 404/405 (endpoint not there yet) also null → resume passes silently.
  const t404 = loadTransport(async () => ({ ok: false, status: 404, body: null, json: async () => ({}) }));
  assert.equal(await t404.probeActiveTurn("c"), null, "404 → null (silent when endpoint absent)");
  const fakeBody = { getReader: () => ({ read: async () => ({ done: true }), releaseLock: () => {} }) };
  const t200 = loadTransport(async () => ({ ok: true, status: 200, body: fakeBody, json: async () => ({}) }));
  const r200 = await t200.probeActiveTurn("c");
  assert.ok(r200 && r200.body, "200+body → returns a live SSE Response");
}

// ── resumeActiveTurn: false when no active turn; when present, consumes the SSE and returns true ────────
{
  const t = loadTransport(async () => ({ ok: false, status: 204, body: null, json: async () => ({}) }));
  assert.equal(await t.resumeActiveTurn("conv-1"), false, "resume must return false when there is no active turn");
}
{
  // Active turn present: replay chunks (delta + done) → bubble fills, returns true.
  const sse =
    'event: delta\ndata: {"text":"Merhaba"}\n\n' +
    'event: done\ndata: {"text":"Merhaba dünya","turn_id":"t1"}\n\n';
  const enc = new TextEncoder().encode(sse);
  let read = 0;
  const body = {
    getReader: () => ({
      read: async () => (read++ === 0 ? { value: enc, done: false } : { done: true }),
      releaseLock: () => {},
    }),
  };
  let synced = false;
  const ctxFetch = async () => ({ ok: true, status: 200, body, json: async () => ({}) });
  const tctx = makeCtx(ctxFetch);
  tctx.window.AkanaCore = { baseUrl: () => "http://x", authHeaders: () => ({}), parseApiError: (b, s) => String(s), escapeHtml: (s) => s };
  tctx.window.AkanaMarkdown = { setBubbleMarkdown: (b, txt) => { b._text = txt; }, appendBubbleStreamText: (b, p) => { if (b) b._text = (b._text || '') + p; } };
  tctx.window.AkanaChatRender = Render;
  vm.runInNewContext(transportSrc, tctx);
  const log = makeEl("div");
  const t = tctx.window.AkanaChatTransport.create({
    hooks: { log, logScroll: log, updateEmptyState: () => {}, stickToBottomIfFollowing: () => {}, ttsPlayer: null, streamTtsParam: () => "", showToast: () => {}, sendBtn: { disabled: false } },
    conversationIdForMemory: () => "conv-1",
    setConversationId: () => {},
    syncConversationLogFromServer: () => { synced = true; },
    applyChatServerAction: () => {},
    consumePendingImageIds: () => [],
  });
  const ok = await t.resumeActiveTurn("conv-1");
  assert.equal(ok, true, "resume must return true when there is an active turn");
  // Assistant row must be appended to the log (replay visible).
  assert.ok(log.children.length >= 1, "resume must append the assistant row to the log");
  // After done the store must be refreshed once.
  assert.equal(synced, true, "syncConversationLogFromServer must be called after turn_completed");
}

// ── 2b. STOP server-side cancel endpoint: POST /chat/active/{cid}/cancel ────────────────
{
  const seen = [];
  const fetchImpl = async (url, opts) => {
    seen.push({ url, method: opts?.method });
    return { ok: true, status: 200, json: async () => ({ cancelled: true, conversation_id: "conv-7" }) };
  };
  const t = loadTransport(fetchImpl);
  const ok = await t.cancelActiveTurnOnServer("conv-7");
  assert.equal(ok, true, "cancelled:true → must return true");
  assert.ok(
    seen.some((s) => /\/api\/v1\/chat\/active\/conv-7\/cancel$/.test(s.url) && s.method === "POST"),
    "cancel must call the correct path with POST",
  );
}
{
  // cancelled:false → false; no endpoint (404) → false; network error → false (swallowed).
  const tFalse = loadTransport(async () => ({ ok: true, status: 200, json: async () => ({ cancelled: false }) }));
  assert.equal(await tFalse.cancelActiveTurnOnServer("c"), false, "cancelled:false → false");
  const t404 = loadTransport(async () => ({ ok: false, status: 404, json: async () => ({}) }));
  assert.equal(await t404.cancelActiveTurnOnServer("c"), false, "404 (no endpoint) → false");
  const tErr = loadTransport(async () => { throw new Error("net down"); });
  assert.equal(await tErr.cancelActiveTurnOnServer("c"), false, "network error must be swallowed → false");
  // empty convId → false without making a call.
  let called = false;
  const tEmpty = loadTransport(async () => { called = true; return { ok: true, status: 200, json: async () => ({ cancelled: true }) }; });
  assert.equal(await tEmpty.cancelActiveTurnOnServer(""), false, "empty convId → false (no fetch)");
  assert.equal(called, false, "empty convId must not trigger a fetch");
}

// ── 2c. Auto-scroll: when a tool card is added, stick to the bottom if in follow mode ──
// The flushToolCallUpdates / done / delta paths must all call stickToBottomIfFollowing
// (so the LLM answer + tool card stay visible when the user is at the bottom).
{
  assert.ok(
    transportSrc.includes("stickToBottomIfFollowing"),
    "transport must use stickToBottomIfFollowing (auto-scroll)",
  );
  // Prove that the tool-card flush calls stick: run a tool_call → done flow via
  // SSE replay and count the stick calls.
  const sse =
    'event: tool_call\ndata: {"call":{"id":"x1","name":"read_file","phase":"start","args":{"file_path":"/a"}}}\n\n' +
    'event: tool_call\ndata: {"call":{"id":"x1","name":"read_file","phase":"end","result":"OK","status":"ok"}}\n\n' +
    'event: delta\ndata: {"text":"cevap"}\n\n' +
    'event: done\ndata: {"text":"cevap tam","turn_id":"t9"}\n\n';
  const enc = new TextEncoder().encode(sse);
  let read = 0;
  const body = {
    getReader: () => ({
      read: async () => (read++ === 0 ? { value: enc, done: false } : { done: true }),
      releaseLock: () => {},
    }),
  };
  const ctxFetch = async () => ({ ok: true, status: 200, body, json: async () => ({}) });
  const tctx = makeCtx(ctxFetch);
  tctx.window.AkanaCore = { baseUrl: () => "http://x", authHeaders: () => ({}), parseApiError: (b, s) => String(s), escapeHtml: (s) => s };
  tctx.window.AkanaMarkdown = { setBubbleMarkdown: (b, txt) => { b._text = txt; }, appendBubbleStreamText: (b, p) => { if (b) b._text = (b._text || '') + p; } };
  tctx.window.AkanaChatRender = Render;
  vm.runInNewContext(transportSrc, tctx);
  let stickCalls = 0;
  const log = makeEl("div");
  const t = tctx.window.AkanaChatTransport.create({
    hooks: { log, logScroll: log, updateEmptyState: () => {}, stickToBottomIfFollowing: () => { stickCalls += 1; }, ttsPlayer: null, streamTtsParam: () => "", showToast: () => {}, sendBtn: { disabled: false }, setStreamingUi: () => {}, cancelVoiceActivity: () => {} },
    chatInFlight: false,
    conversationIdForMemory: () => "conv-1",
    setConversationId: () => {},
    syncConversationLogFromServer: () => {},
    applyChatServerAction: () => {},
    consumePendingImageIds: () => [],
  });
  await t.resumeActiveTurn("conv-1");
  assert.ok(stickCalls >= 1, "stickToBottomIfFollowing must be called at least once during the tool card + answer stream (auto-scroll)");
}

// ── 2d. LIVE throttled markdown: render is NOT per-frame during the stream ─────────
// The user wants to see **bold**/code/table formatted AS IT FORMS; but a full
// markdown parse on every frame → freeze. Solution: TIME throttle. This test drives
// 10 fast deltas with a fake clock; the streaming setBubbleMarkdown call count must be
// MUCH LESS than 10 but > 0; and exactly 1 final (non-streaming) render on done.
{
  // Fake clock — performance.now returns this; we keep it ALMOST constant between
  // deltas (within the throttle window) → render is suppressed.
  let clock = 1000;
  // 10 deltas in a single SSE chunk (all drained in the same rAF batch).
  let sse = "";
  for (let i = 0; i < 10; i++) sse += `event: delta\ndata: {"text":"x${i} "}\n\n`;
  sse += 'event: done\ndata: {"text":"x0 x1 x2 x3 x4 x5 x6 x7 x8 x9 ","turn_id":"tT"}\n\n';
  const enc = new TextEncoder().encode(sse);
  let read = 0;
  const body = {
    getReader: () => ({
      read: async () => (read++ === 0 ? { value: enc, done: false } : { done: true }),
      releaseLock: () => {},
    }),
  };
  let streamRenders = 0;
  let finalRenders = 0;
  const tctx = makeCtx(async () => ({ ok: true, status: 200, body, json: async () => ({}) }));
  tctx.performance = { now: () => clock };
  tctx.window.performance = tctx.performance;
  tctx.window.AkanaCore = { baseUrl: () => "http://x", authHeaders: () => ({}), parseApiError: (b, s) => String(s), escapeHtml: (s) => s };
  tctx.window.AkanaMarkdown = {
    setBubbleMarkdown: (b, txt, opts) => {
      if (opts && opts.streaming) streamRenders += 1;
      else finalRenders += 1;
      if (b) b._text = txt;
    },
    appendBubbleStreamText: (b, p) => { if (b) b._text = (b._text || "") + p; },
  };
  tctx.window.AkanaChatRender = Render;
  vm.runInNewContext(transportSrc, tctx);
  const log = makeEl("div");
  const t = tctx.window.AkanaChatTransport.create({
    hooks: { log, logScroll: log, updateEmptyState: () => {}, stickToBottomIfFollowing: () => {}, ttsPlayer: null, streamTtsParam: () => "", showToast: () => {}, sendBtn: { disabled: false }, setStreamingUi: () => {}, cancelVoiceActivity: () => {} },
    chatInFlight: false,
    conversationIdForMemory: () => "conv-1",
    setConversationId: () => {},
    syncConversationLogFromServer: () => {},
    applyChatServerAction: () => {},
    consumePendingImageIds: () => [],
  });
  await t.resumeActiveTurn("conv-1");
  // Clock constant (within the throttle window) → of 10 deltas only the first
  // renders immediately, the rest are suppressed (trailing timer canceled by done reset). So
  // streaming render << 10.
  assert.ok(streamRenders > 0, "there must be at least one markdown render during the live stream (live format)");
  assert.ok(streamRenders < 10, `streaming render must not be per-frame (throttle): ${streamRenders} < 10`);
  // Exactly one full (non-streaming) final render on done.
  assert.equal(finalRenders, 1, "there must be exactly 1 final markdown render on done");
  // Final text is complete.
  assert.equal(log.children[0]?.children[1]?.children[1]?._text, "x0 x1 x2 x3 x4 x5 x6 x7 x8 x9", "done must format the final text");
}

// ── 2e. Adaptive throttle: render interval grows for large responses (freeze shield) ─
// Source check: the throttle interval formula grows proportionally to acc length
// (so the frame budget does not overflow even on a very long response).
assert.ok(
  /Math\.floor\(\s*\(?[\w.]*length[\w.\s|]*\)?\s*\/\s*120\s*\)/.test(transportSrc) ||
    transportSrc.includes("streamMdInterval"),
  "transport must contain an adaptive throttle interval (length-proportional)",
);

// ── 2f. Phase strip GUARANTEE: if the stream closes WITHOUT done, "Typing" does not get stuck ─
// BUG: the "Typing" strip is normally closed only by a done/error SSE event. If the server
// closes the connection without sending done (or a pre-done backend step blows up),
// deltas have arrived (text is visible) but the strip stays stuck forever on its timer.
// finalizeStreamUi() now runs guaranteed in the consumeSseResponse finally.
{
  // SSE: delta only — NO done (server sends the text and closes the connection).
  const sse = 'event: delta\ndata: {"text":"Tamamlanmış yanıt"}\n\n';
  const enc = new TextEncoder().encode(sse);
  let read = 0;
  const body = {
    getReader: () => ({
      read: async () => (read++ === 0 ? { value: enc, done: false } : { done: true }),
      releaseLock: () => {},
    }),
  };
  let tsActive = false;
  let endCalls = 0;
  const tctx = makeCtx(async () => ({ ok: true, status: 200, body, json: async () => ({}) }));
  tctx.window.AkanaTurnStatus = {
    mount() {}, setPhase() {},
    begin() { tsActive = true; },
    end() { tsActive = false; endCalls += 1; },
    isActive() { return tsActive; },
  };
  tctx.window.AkanaCore = { baseUrl: () => "http://x", authHeaders: () => ({}), parseApiError: (b, s) => String(s), escapeHtml: (s) => s };
  tctx.window.AkanaMarkdown = { setBubbleMarkdown: (b, txt) => { b._text = txt; }, appendBubbleStreamText: (b, p) => { if (b) b._text = (b._text || '') + p; } };
  tctx.window.AkanaChatRender = Render;
  vm.runInNewContext(transportSrc, tctx);
  const log = makeEl("div");
  const t = tctx.window.AkanaChatTransport.create({
    hooks: { log, logScroll: log, updateEmptyState: () => {}, stickToBottomIfFollowing: () => {}, ttsPlayer: null, streamTtsParam: () => "", showToast: () => {}, sendBtn: { disabled: false }, setStreamingUi: () => {}, setComposerHint: () => {} },
    conversationIdForMemory: () => "conv-1",
    setConversationId: () => {},
    syncConversationLogFromServer: () => {},
    applyChatServerAction: () => {},
    consumePendingImageIds: () => [],
  });
  await t.resumeActiveTurn("conv-1");
  // Text arrived (bubble filled) but no done → the strip must still be idle.
  assert.equal(log.children[0]?.children[1]?.children[1]?._text, "Tamamlanmış yanıt", "delta text must be written to the bubble");
  assert.equal(tsActive, false, "phase strip must be idle when the stream closes without done (Typing must not get stuck)");
  assert.ok(endCalls >= 1, "AkanaTurnStatus.end() must be called on stream close (finally guarantee)");
}

// ── 2f-2. SILENT-LOSS SHIELD: 200 opens but closes CLEANLY without ANY event ─
// BUG: the server sends the 200 header and closes the connection BEFORE the FIRST SSE event
// (turn handler crash / proxy drop / pre-done persist blow-up) → acc empty
// + no done + no exception. Previously streamChat silently did `return ""` → an empty
// "pending" bubble stays hung in the DOM, and the user never realizes the turn was LOST.
// Fix: synthetic EMPTY error → visible error-path (bubble becomes an error + throw → caller
// writes an "Error" row). DIFFERENCE from 2f: there a delta ARRIVES (acc filled, partial success);
// here NOTHING arrives (a real silent loss).
{
  const emptyBody = { getReader: () => ({ read: async () => ({ done: true }), releaseLock: () => {} }) };
  const tctx = makeCtx(async () => ({ ok: true, status: 200, body: emptyBody, json: async () => ({}) }));
  tctx.window.AkanaCore = { baseUrl: () => "http://x", authHeaders: () => ({}), parseApiError: (b, s) => String(s), escapeHtml: (s) => s };
  tctx.window.AkanaMarkdown = { setBubbleMarkdown: (b, txt) => { if (b) b._text = txt; }, appendBubbleStreamText: (b, p) => { if (b) b._text = (b._text || '') + p; } };
  tctx.window.AkanaChatRender = Render;
  vm.runInNewContext(transportSrc, tctx);
  const log = makeEl("div");
  const t = tctx.window.AkanaChatTransport.create({
    hooks: { log, logScroll: log, updateEmptyState: () => {}, stickToBottomIfFollowing: () => {}, ttsPlayer: null, streamTtsParam: () => "", showToast: () => {}, sendBtn: { disabled: false }, setStreamingUi: () => {}, setComposerHint: () => {}, cancelVoiceActivity: () => {} },
    conversationIdForMemory: () => "conv-1",
    setConversationId: () => {},
    syncConversationLogFromServer: () => {},
    applyChatServerAction: () => {},
    consumePendingImageIds: () => [],
  });
  let threw = null;
  try {
    await t.streamChat("merhaba");
  } catch (e) {
    threw = e;
  }
  assert.ok(threw, "empty-clean-close must not pass SILENTLY — streamChat must throw (so the caller writes an 'Error' row)");
  // The assistant bubble must turn into an error indicator (no empty 'pending' bubble left hanging).
  const bubble = log.children[0]?.children[1]?.children[1];
  assert.ok(bubble, "assistant row/bubble must be set up");
  assert.ok(/EMPTY|Yanıt alınamadı/.test(bubble._text || ""), `bubble must show error text on empty-close: ${bubble._text}`);
  assert.ok(bubble.classList.contains("bubble-bot-err"), "bubble must get the error class (bubble-bot-err)");
  assert.ok(!bubble.classList.contains("bubble-bot-pending"), "empty 'pending' bubble must not remain hanging");
}

// ── 2g. WS GUARANTEE: if SSE stalls, the server's `turn_completed` closes "Typing" ─
// reconcileServerCompletedTurn: if the live stream is wedged without delivering its own `done`
// (half-open TCP / stuck follower) and the server finished the turn, after a grace period it
// closes the stream and lowers the strip. Does not touch anything if there is no live stream / a different turn.
{
  // Wedged SSE: meta+delta arrive, the next read hangs FOREVER (no done).
  const enc = new TextEncoder().encode(
    'event: meta\ndata: {"turn_id":"tW","conversation_id":"conv-1"}\n\n' +
      'event: delta\ndata: {"text":"yarım"}\n\n',
  );
  let read = 0;
  let rejectRead = null;
  const body = {
    getReader: () => ({
      read: () =>
        read++ === 0
          ? Promise.resolve({ value: enc, done: false })
          : new Promise((_res, rej) => { rejectRead = rej; }),
      releaseLock: () => {},
    }),
  };
  let tsActive = false;
  let endCalls = 0;
  const tctx = makeCtx(async () => ({ ok: true, status: 200, body, json: async () => ({}) }));
  // Speed up the grace timer (2000ms) — async ordering is preserved, no real-time.
  tctx.window.setTimeout = (fn, ms) => setTimeout(fn, Math.min(Number(ms) || 0, 5));
  tctx.window.AkanaTurnStatus = {
    mount() {}, setPhase() {},
    begin() { tsActive = true; },
    end() { tsActive = false; endCalls += 1; },
    isActive() { return tsActive; },
  };
  tctx.window.AkanaCore = { baseUrl: () => "http://x", authHeaders: () => ({}), parseApiError: (b, s) => String(s), escapeHtml: (s) => s };
  tctx.window.AkanaMarkdown = { setBubbleMarkdown: (b, t2) => { if (b) b._text = t2; }, appendBubbleStreamText: (b, p) => { if (b) b._text = (b._text || "") + p; } };
  tctx.window.AkanaChatRender = Render;
  vm.runInNewContext(transportSrc, tctx);
  const log = makeEl("div");
  const t = tctx.window.AkanaChatTransport.create({
    hooks: { log, logScroll: log, updateEmptyState() {}, stickToBottomIfFollowing() {}, ttsPlayer: null, streamTtsParam: () => "", showToast() {}, sendBtn: { disabled: false }, setStreamingUi() {}, setComposerHint() {} },
    conversationIdForMemory: () => "conv-1",
    setConversationId() {}, syncConversationLogFromServer() {}, applyChatServerAction() {}, consumePendingImageIds: () => [],
  });
  // With NO live stream, reconcile is a no-op (false).
  assert.equal(await t.reconcileServerCompletedTurn("conv-1", "tW"), false, "rescue must be false when there is no live stream");
  // Start the wedged stream (do NOT await — done never arrives).
  const wedged = t.resumeActiveTurn("conv-1");
  await new Promise((r) => setTimeout(r, 40)); // let meta+delta process, so liveStreamCtx+turnId get set
  assert.equal(tsActive, true, "AkanaTurnStatus must be active on a wedged stream (begin)");
  // Different turId → do not close the wrong turn (false).
  assert.equal(await t.reconcileServerCompletedTurn("conv-1", "BASKA"), false, "different turId → no rescue");
  assert.equal(tsActive, true, "a different turId must not close the strip");
  // Correct turn + wedged → rescue (true) + "Typing" lowers.
  const rescued = await t.reconcileServerCompletedTurn("conv-1", "tW");
  assert.equal(rescued, true, "wedged stream + correct turn → must be rescued (true)");
  assert.equal(tsActive, false, "rescue must close 'Typing' (end)");
  // Cleanup: reject the hanging read → let the wedged promise resolve (no leak).
  if (rejectRead) { const e = new Error("abort"); e.name = "AbortError"; rejectRead(e); }
  await wedged.catch(() => {});
}

// ── 5. Send ↔ Stop button state machine (akana-chat.js) ────────────────────
// init() runs the real wireChatForm; submit → STOP, click STOP → abort + SEND,
// a new submit is blocked while busy. Threads/Transport/Render are stubbed.
{
  const CHAT_PATH = path.join(REPO, "web_ui/static/akana-chat.js");
  const chatSrc = readFileSync(CHAT_PATH, "utf8");

  // Dispatch-capable minimal element (addEventListener + dispatch).
  function makeListenEl(tag = "div") {
    const el = makeEl(tag);
    el._listeners = {};
    el.addEventListener = (type, fn, opts) => {
      (el._listeners[type] ||= []).push({ fn, capture: !!(opts === true || opts?.capture) });
    };
    el.dispatch = (type, ev = {}) => {
      const e = { type, preventDefault() {}, stopPropagation() {}, ...ev };
      for (const cap of [true, false]) {
        for (const l of el._listeners[type] || []) {
          if (l.capture === cap) l.fn(e);
        }
      }
      return e;
    };
    return el;
  }

  let streamResolve = null;
  let streamCalls = 0;
  let abortCalls = 0;
  let cancelCalls = 0;
  let lastCancelConv = null;
  // PER-CONV SINGLE-TURN guard test: WHICH conv's stream is active (null = none).
  // Previously there was a boolean streamActiveFlag + global isStreamActive(); after the
  // per-conv refactor the guard is convId-targeted (isConversationStreamActive).
  let convStreamActive = null;
  const transportStub = {
    streamChat: () => { streamCalls += 1; return new Promise((res) => { streamResolve = res; }); },
    isStreamActive: () => convStreamActive != null,
    isConversationStreamActive: (cid) => convStreamActive != null && cid === convStreamActive,
    humanizeChatError: (e) => String(e),
    abortActiveChatStream: () => { abortCalls += 1; },
    cancelActiveTurnOnServer: async (cid) => { cancelCalls += 1; lastCancelConv = cid; return true; },
    fetchConversationTurnsFromServer: async () => ({ status: 0, turns: [] }),
    abortConversationTurnsFetch: () => {},
    resumeActiveTurn: async () => false,
    ensureConversationIdReady: async () => "conv-1",
  };
  const threadsStub = {
    getChatStore: () => ({}),
    getChatArchiveItems: () => [],
    setChatArchiveItems: () => {},
    getActiveConversationMeta: () => null,
    setActiveConversationMeta: () => {},
    conversationIdForMemory: () => "conv-1",
    setConversationId: () => {},
    reloadConversationLogFromServer: () => {},
    syncConversationLogFromServer: () => {},
    applyChatServerAction: () => {},
    purgeConversationFromChatStore: () => {},
    chatProfile: () => "default",
    newChatThreadId: () => "t",
    chatStartNewThread: () => {},
    syncChatThreadBar: () => {},
    loadChatArchiveList: () => {},
    wireArchiveChrome: () => {},
    wireThreadBar: () => {},
    tryHandleChatDeleteCommand: () => false,
    chatRecordMessage: () => {},
  };

  const ctx = makeCtx(async () => ({}));
  ctx.window.AkanaCore = { escapeHtml: (s) => s };
  ctx.window.AkanaChatRender = { createRenderer: () => ({ chatRenderMessage: () => {} }), mapServerMessagesToThread: () => [] };
  ctx.window.AkanaChatThreads = { create: () => threadsStub };
  ctx.window.AkanaChatTransport = { create: () => transportStub };
  ctx.window.AkanaVoice = {};
  vm.runInNewContext(chatSrc, ctx);
  const Chat = ctx.window.AkanaChat;
  assert.ok(Chat && typeof Chat.init === "function", "AkanaChat.init failed to load");

  const form = makeListenEl("form");
  form.requestSubmit = () => form.dispatch("submit");
  const msg = makeEl("textarea");
  msg.value = "merhaba";
  msg.focus = () => {};
  const sendBtn = makeListenEl("button");
  sendBtn.dataset.mode = "send";

  Chat.init({
    form, msg, sendBtn,
    log: makeEl("div"),
    appendUserMessage: () => {},
    appendRow: () => {},
    resizeComposer: () => {},
    setOrb: () => {},
    setComposerHint: () => {},
    syncOrbWithVoice: () => {},
    isChatPage: true,
  });

  assert.equal(sendBtn.dataset.mode, "send", "send mode at start");

  // PER-CONV SINGLE-TURN GUARD: a new turn is blocked ONLY when the SAME (displayed) chat
  // is already streaming; it is caught by isConversationStreamActive even if chatInFlight is
  // false. STOP→send (forceImmediate) and voiceTurn are exempt.
  //
  // (a) SAME chat (conv-1) streaming → normal send is BLOCKED (streamCalls 0).
  convStreamActive = "conv-1";
  msg.value = "ayni-conv-deneme";
  form.dispatch("submit");
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(streamCalls, 0, "a normal send must be blocked by the PER-CONV guard while the same chat is streaming");

  // (b) ANOTHER chat (conv-2) streaming while the displayed conv-1 is idle → send is FREE
  // (parallel n-chat design). The old global isStreamActive() guard would WRONGLY block this;
  // this assert locks the per-conv semantics against regression.
  convStreamActive = "conv-2";
  msg.value = "capraz-conv-deneme";
  form.dispatch("submit");
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(streamCalls, 1, "a send to this chat must NOT be blocked while ANOTHER chat is streaming (parallel n-chat)");

  // (b) started a real stream → clear it with STOP + reset counters so the main
  // flow below (first submit = streamCalls 1) starts from the same absolute count as today.
  sendBtn.dispatch("click");
  await new Promise((r) => setTimeout(r, 0));
  streamCalls = 0; abortCalls = 0; cancelCalls = 0; lastCancelConv = null;
  convStreamActive = null; // start the real-stream scenario clean
  msg.value = "merhaba";

  // Submit → stream starts, the button switches to STOP mode, stays clickable.
  form.dispatch("submit");
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(streamCalls, 1, "submit must call streamChat");
  assert.equal(sendBtn.dataset.mode, "stop", "button must be STOP while streaming");
  assert.equal(sendBtn.disabled, false, "button must stay clickable in STOP mode");
  assert.equal(Chat.getChatInFlight(), true, "chatInFlight true while streaming");

  // b27: during the setup window (isConversationStreamActive still false, streamChat promise
  // pending), a NON-forceImmediate double-submit to the SAME conv must NOT START a 2nd turn — the
  // per-conv setup latch prevents it (otherwise the message runs twice).
  msg.value = "cift-gonderim";
  form.dispatch("submit");
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(streamCalls, 1, "b27: a double-submit to the same conv during the setup window must be blocked");

  // STOP + message (click while there is text in the composer): cancel → send immediately.
  msg.value = "ikinci";
  sendBtn.dispatch("click");
  await new Promise((r) => setTimeout(r, 0));
  await new Promise((r) => setTimeout(r, 0));
  assert.ok(cancelCalls >= 1, "STOP+text send must call the server cancel");
  assert.equal(lastCancelConv, "conv-1", "auto-cancel must be called with the correct conversation_id");
  assert.equal(streamCalls, 2, "after auto-cancel the new message must be sent via streamChat (no TURN_BUSY)");
  assert.equal(sendBtn.dataset.mode, "stop", "button must be STOP again once the new stream starts");

  // Click STOP → client abort + SERVER cancel + return to SEND mode.
  const cancelsBefore = cancelCalls;
  sendBtn.dispatch("click");
  await new Promise((r) => setTimeout(r, 0));
  assert.ok(abortCalls >= 1, "clicking STOP must call abortActiveChatStream");
  assert.ok(cancelCalls > cancelsBefore, "clicking STOP must call the server cancel endpoint");
  assert.equal(sendBtn.dataset.mode, "send", "button must return to SEND after cancel");
  assert.equal(Chat.getChatInFlight(), false, "chatInFlight must be cleared after cancel");

  // After STOP, sending a new message must have no TURN_BUSY guard left (free).
  const streamsBefore = streamCalls;
  msg.value = "üçüncü";
  form.dispatch("submit");
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(streamCalls, streamsBefore + 1, "a new message must be sendable after STOP");

  // Even if the streamChat promise resolves late, state must not be corrupted (after abort).
  if (streamResolve) streamResolve("");
  await new Promise((r) => setTimeout(r, 0));
}

// ── 5b. Queue via Enter: while a live SSE is running, enqueue must not abort ─────────────
{
  let fetchN = 0;
  let firstSignal = null;
  const fetchImpl = async (_url, opts) => {
    fetchN += 1;
    if (fetchN === 1) {
      firstSignal = opts?.signal || null;
      const body = {
        getReader: () => ({
          read: () => new Promise(() => {}),
          releaseLock: () => {},
        }),
      };
      return { ok: true, status: 200, body, json: async () => ({}) };
    }
    return {
      ok: true,
      status: 202,
      json: async () => ({ queued: true, depth: 1, item_id: "q1" }),
    };
  };
  const tctx = makeCtx(fetchImpl);
  tctx.window.AkanaCore = { baseUrl: () => "http://x", authHeaders: () => ({}), parseApiError: (b, s) => String(s), escapeHtml: (s) => s };
  tctx.window.AkanaMarkdown = { setBubbleMarkdown: () => {}, appendBubbleStreamText: () => {} };
  tctx.window.AkanaChatRender = Render;
  vm.runInNewContext(transportSrc, tctx);
  const log = makeEl("div");
  let toastMsg = null;
  const t = tctx.window.AkanaChatTransport.create({
    hooks: {
      log,
      logScroll: log,
      updateEmptyState: () => {},
      stickToBottomIfFollowing: () => {},
      ttsPlayer: null,
      streamTtsParam: () => "",
      showToast: (m) => { toastMsg = m; },
      sendBtn: { disabled: false },
      setStreamingUi: () => {},
      cancelVoiceActivity: () => {},
      setQueueDepth: () => {},
    },
    chatInFlight: false,
    conversationIdForMemory: () => "conv-1",
    setConversationId: () => {},
    syncConversationLogFromServer: () => {},
    applyChatServerAction: () => {},
    consumePendingImageIds: () => [],
  });
  void t.streamChat("ilk");
  await new Promise((r) => setTimeout(r, 0));
  assert.ok(t.isStreamActive(), "first stream must be active");
  const second = await t.streamChat("ikinci");
  assert.equal(second?.queued, true, "second message must be queued with 202");
  assert.ok(firstSignal && !firstSignal.aborted, "the live SSE must not be aborted during enqueue");
  assert.equal(toastMsg, "Message queued", "the enqueue toast must be shown (i18n EN)");
  t.abortActiveChatStream();
}

// ── 4. Cache-bust ritual: transport.js must carry the SAME ?v= on every page ──
// Every page that LOADS akana-chat-transport.js (index + memory) must fetch it with the SAME ?v=
// version; if one lags behind, that page serves the stale (broken) transport
// from cache → the fix never reaches that page. The version is NOT HARD-CODED (the ritual
// requires a bump on every change; hard-coding would break the test on every bump) — only
// PRESENCE + CONSISTENCY across the two pages is verified.
{
  const verRe = /akana-chat-transport\.js\?v=([^"']+)/;
  const idxVer = (readFileSync(path.join(REPO, "web_ui", "index.html"), "utf8").match(verRe) || [])[1];
  const memVer = (readFileSync(path.join(REPO, "web_ui", "memory.html"), "utf8").match(verRe) || [])[1];
  assert.ok(idxVer, "index.html must contain akana-chat-transport.js?v=<bust>");
  assert.ok(memVer, "memory.html must contain akana-chat-transport.js?v=<bust>");
  assert.equal(
    idxVer, memVer,
    `transport cache-bust must be the SAME on both pages (index=${idxVer} memory=${memVer}) — otherwise one page serves the stale transport`,
  );
}

// ── 7. Assistant meta row: token + cost badges (Token/cost surface) ────
// The tokens block in the done SSE ({prompt, completion, cost_usd?}) is rendered into the meta row
// as "Akana · {ms} ms · {tokens} tokens · ${cost}". cost_usd only comes when
// the provider supplies it (claude total_cost_usd) → the $ badge NEVER shows for other providers
// (no misleading "0$"). The old behavior (no tokens → "Akana"/"Akana ·
// N ms") must stay backward-compatible.
{
  const tinst = loadTransport(async () => ({ ok: false, status: 0 }));
  const T = tinst.__test;
  const fmt = T.formatAssistantStreamMeta;
  assert.ok(typeof fmt === "function", "formatAssistantStreamMeta __test seam must be found");

  // Backward-compat: no tokens (independent of the toggle).
  assert.equal(fmt("t1", null), "Akana", "no doneMeta → Akana only");
  assert.equal(fmt("t1", { latency_ms: 1500 }), "Akana · 1.5s", "duration only");

  // ── OFF BY DEFAULT: when the "Show tokens & cost" setting is off (no show-usage
  // body class) the token/cost segments are HIDDEN — only duration remains.
  assert.ok(
    !tinst.__doc.body.classList.contains("show-usage"),
    "default: no show-usage class (toggle OFF)",
  );
  assert.equal(
    fmt("t1", { latency_ms: 1500, tokens: { prompt: 800, completion: 400 } }),
    "Akana · 1.5s",
    "toggle OFF → tokens hidden (duration only)",
  );
  assert.equal(
    fmt("t1", { latency_ms: 1500, tokens: { prompt: 800, completion: 400, cost_usd: 0.0123 } }),
    "Akana · 1.5s",
    "toggle OFF → cost also hidden",
  );

  // ── ON: enable the setting → token + cost segments become visible.
  tinst.__doc.body.classList.add("show-usage");

  // Token total (prompt+completion) in compact form.
  assert.equal(
    fmt("t1", { latency_ms: 1500, tokens: { prompt: 800, completion: 400 } }),
    "Akana · 1.5s · 1.2k tok",
    "token total compact (1.2k, i18n EN)",
  );
  // Cost badge (claude total_cost_usd).
  assert.equal(
    fmt("t1", { latency_ms: 1500, tokens: { prompt: 800, completion: 400, cost_usd: 0.0123 } }),
    "Akana · 1.5s · 1.2k tok · $0.012",
    "cost badge must be added",
  );
  // If there is no duration the ms segment is skipped but token/cost remain.
  assert.equal(
    fmt("t1", { tokens: { prompt: 100, completion: 50, cost_usd: 0.05 } }),
    "Akana · 150 tok · $0.050",
    "ms skipped when absent, tokens+cost remain",
  );
  // Zero tokens → no token segment; no cost_usd → the $ segment never appears.
  assert.equal(
    fmt("t1", { latency_ms: 90, tokens: { prompt: 0, completion: 0 } }),
    "Akana · 90 ms",
    "zero tokens and no cost → duration only",
  );
  assert.equal(
    fmt("t1", { latency_ms: 90, tokens: { prompt: 10, completion: 5 } }),
    "Akana · 90 ms · 15 tok",
    "no $ badge when there is no cost",
  );
  // Format boundaries: <1000 raw, 45678→46k; cost tiers (>=1 → 2 decimals).
  assert.equal(
    fmt("t1", { tokens: { prompt: 980, completion: 0 } }),
    "Akana · 980 tok",
    "<1000 raw number",
  );
  assert.equal(
    fmt("t1", { tokens: { prompt: 45000, completion: 678, cost_usd: 2.5 } }),
    "Akana · 46k tok · $2.50",
    "large token 46k + >=1$ two decimals",
  );
}

// ── Inline ask_user card: the preamble text must NOT be DUPLICATED BELOW the card (regression) ─
// Bug: when the model writes a preamble first and then asks an AskUserQuestion, `done` was also
// writing the ENTIRE turn text (preamble included) into a FRESH post-card bubble → the preamble showed twice.
// Fix: the sealed prefix (stripSealedPrefix) is stripped; if there is no post-card text
// the empty live bubble is removed. This test proves the preamble stays in a SINGLE bubble.
{
  const PREAMBLE = "Onsoz metni burada — once acikla sonra sor.";
  const Q = {
    id: "qa",
    questions: [
      {
        question: "Secimin ne?",
        header: "Hedef",
        multiSelect: false,
        options: [{ label: "A", description: "ilk" }, { label: "B" }],
      },
    ],
  };
  const sse =
    `event: delta\ndata: ${JSON.stringify({ text: PREAMBLE })}\n\n` +
    `event: ask_user\ndata: ${JSON.stringify({ question: Q })}\n\n` +
    `event: done\ndata: ${JSON.stringify({ text: PREAMBLE, turn_id: "tq", ask_user: Q })}\n\n`;
  const enc = new TextEncoder().encode(sse);
  let read = 0;
  const body = {
    getReader: () => ({
      read: async () => (read++ === 0 ? { value: enc, done: false } : { done: true }),
      releaseLock: () => {},
    }),
  };
  const tctx = makeCtx(async () => ({ ok: true, status: 200, body, json: async () => ({}) }));
  tctx.window.AkanaCore = { baseUrl: () => "http://x", authHeaders: () => ({}), parseApiError: (b, s) => String(s), escapeHtml: (s) => s };
  tctx.window.AkanaMarkdown = { setBubbleMarkdown: (b, txt) => { if (b) b._text = String(txt); }, appendBubbleStreamText: (b, p) => { if (b) b._text = (b._text || "") + p; } };
  tctx.window.AkanaChatRender = Render;
  vm.runInNewContext(transportSrc, tctx);
  const log = makeEl("div");
  const t = tctx.window.AkanaChatTransport.create({
    hooks: { log, logScroll: log, updateEmptyState: () => {}, stickToBottomIfFollowing: () => {}, ttsPlayer: null, streamTtsParam: () => "", showToast: () => {}, sendBtn: { disabled: false } },
    conversationIdForMemory: () => "conv-1",
    setConversationId: () => {},
    syncConversationLogFromServer: () => {},
    applyChatServerAction: () => {},
    consumePendingImageIds: () => [],
  });
  await t.resumeActiveTurn("conv-1");
  assert.ok(findOne(log, ".aur-ask"), "ask_user card must be set up (preamble + question turn)");
  // The FULL preamble text must be in only ONE node (not duplicated into a post-card bubble).
  let withPreamble = 0;
  walk(log, (n) => { if (String(n._text || "").includes(PREAMBLE)) withPreamble += 1; });
  assert.equal(
    withPreamble,
    1,
    `the full preamble text must be in a single bubble (no post-card duplication) — found: ${withPreamble}`,
  );
}

console.log("chat_stream_resilience.harness: ALL CONTRACTS PASSED ✓");

// A dangling timer (live throttle setTimeout, etc.) must not keep the node process alive —
// the CLI harness exits for sure on success (an assert failure already yields a non-zero exit).
if (typeof process !== "undefined" && process.exit) process.exit(0);
