/**
 * UI edge-case contract test — no backend, node-vm + fake-DOM.
 *
 * Purpose: deterministically verifies fresh resume/tool-card code + markdown XSS
 * shield + SSE broken/out-of-order stream + cost empty data + FSM unknown event +
 * bus silent dispatch + archive search/empty states. All pure
 * (no real network/time); fits the breaking-test→green ritual.
 *
 * Run: node tests/web/ui_edge_cases.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { makeI18nStub } from "./_i18n_stub.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const read = (rel) => readFileSync(path.join(REPO, "web_ui/static", rel), "utf8");

let passed = 0;
function check(label, fn) {
  fn();
  passed += 1;
  // silent: summary at the end. (label only in assert messages for debugging.)
  void label;
}

// ───────────────────────── Fake-DOM (only the surfaces used) ─────────────
function makeEl(tag = "div") {
  const el = {
    tagName: String(tag).toUpperCase(),
    children: [],
    childNodes: [],
    dataset: {},
    _listeners: {},
    classList: {
      _s: new Set(),
      add(...c) { c.forEach((x) => this._s.add(x)); },
      remove(...c) { c.forEach((x) => this._s.delete(x)); },
      toggle(c, on) { const want = on === undefined ? !this._s.has(c) : on; if (want) this._s.add(c); else this._s.delete(c); return want; },
      contains(c) { return this._s.has(c); },
    },
    style: {},
    attrs: {},
    _text: "",
    _html: "",
    _open: false,
    hidden: false,
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); this.children = []; this.childNodes = []; },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); if (v === "") { this.children = []; this.childNodes = []; } },
    get className() { return [...this.classList._s].join(" "); },
    set className(v) { this.classList._s = new Set(String(v).split(/\s+/).filter(Boolean)); },
    get open() { return this._open; },
    set open(v) { this._open = !!v; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k] ?? null; },
    removeAttribute(k) { delete this.attrs[k]; },
    appendChild(c) { this.children.push(c); this.childNodes.push(c); c.parentNode = this; return c; },
    append(...cs) { cs.forEach((c) => (typeof c === "object" ? this.appendChild(c) : null)); },
    insertBefore(node, ref) {
      const i = this.children.indexOf(ref);
      if (i < 0) this.children.push(node);
      else this.children.splice(i, 0, node);
      this.childNodes = this.children;
      node.parentNode = this;
      return node;
    },
    addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); },
    dispatch(type, evt) { (this._listeners[type] || []).forEach((fn) => fn(evt)); },
    remove() {
      const p = this.parentNode;
      if (p) { const i = p.children.indexOf(this); if (i >= 0) { p.children.splice(i, 1); p.childNodes = p.children; } }
    },
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
    const m = sel.match(/^\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]$/);
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
function walk(el, fn) { for (const c of el.children || []) { fn(c); walk(c, fn); } }
function findOne(root, sel) { let out = null; walk(root, (n) => { if (!out && selMatch(n, sel)) out = n; }); return out; }
function findAll(root, sel) { const out = []; walk(root, (n) => { if (selMatch(n, sel)) out.push(n); }); return out; }

const ESCAPE = (s) =>
  String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");

// ═════════════════════════════════ MARKDOWN XSS ═════════════════════════════
{
  const ctx = { window: { AkanaCore: { escapeHtml: ESCAPE } }, console };
  ctx.window.window = ctx.window;
  vm.runInNewContext(read("akana-markdown.js"), ctx);
  const M = ctx.window.AkanaMarkdown;

  check("md: <script> escape", () => {
    const html = M.render("<script>alert(1)</script>");
    assert.ok(!/<script>/i.test(html), "raw <script> must not leak");
    assert.ok(html.includes("&lt;script&gt;"), "script must be escaped");
  });
  check("md: img/onerror escape", () => {
    const html = M.render("<img src=x onerror=alert(1)>");
    assert.ok(!/<img/i.test(html), "raw <img> tag must not leak");
    assert.ok(html.includes("&lt;img"), "<img> must be escaped");
  });
  check("md: javascript: link not linkified", () => {
    const html = M.render("[tıkla](javascript:alert(1))");
    assert.ok(!/href="javascript:/i.test(html), "javascript: protocol must not be an href");
    assert.ok(!html.includes("<a "), "javascript: link must not produce <a>");
  });
  check("md: safe http link with rel/target", () => {
    const html = M.render("git http://example.com/x yolu");
    assert.ok(html.includes('href="http://example.com/x"'), "http link must be built");
    assert.ok(html.includes('rel="noopener noreferrer"'), "rel security flag is required");
    assert.ok(html.includes('target="_blank"'), "target _blank");
  });
  check("md: code block content escape (XSS body)", () => {
    const html = M.render("```js\n<script>x</script>\n```");
    assert.ok(html.includes("md-code"), "code block class");
    assert.ok(!/<script>/i.test(html), "<script> in code body escaped");
  });
  check("md: table cell inline escape", () => {
    const html = M.render("| a | b |\n| --- | --- |\n| <b>x</b> | 2 |");
    assert.ok(html.includes("md-table"), "table render");
    assert.ok(!html.includes("<b>x</b>"), "cell HTML escape");
  });
  check("md: empty input empty output", () => {
    assert.equal(M.render(""), "");
    assert.equal(M.render(null), "");
  });
  check("md: setBubbleMarkdown wraps md-content without crashing", () => {
    const ctx2 = { window: { AkanaCore: { escapeHtml: ESCAPE } }, document: { createElement: (t) => makeEl(t) }, console };
    ctx2.window.window = ctx2.window;
    vm.runInNewContext(read("akana-markdown.js"), ctx2);
    const bubble = makeEl("div");
    ctx2.window.AkanaMarkdown.setBubbleMarkdown(bubble, "**kalın**");
    assert.ok(bubble.innerHTML.includes("md-content"), "md-content wrapper");
    assert.ok(bubble.innerHTML.includes("<strong>"), "bold render");
  });
}

// ═════════════════════════ CHAT RENDER — TOOL CARD ═════════════════════════
const Render = (() => {
  // Term/subagent elapsed tickers call `window.setInterval`; node's vm surfaces no
  // timer globals, so stub them as no-ops (the tickers only repaint an elapsed label).
  const noopTimers = { setInterval: () => 0, clearInterval: () => {}, setTimeout: () => 0, clearTimeout: () => {} };
  const ctx = {
    window: { AkanaCore: { escapeHtml: (s) => s }, AkanaMarkdown: { setBubbleMarkdown: () => {}, appendBubbleStreamText: (b, p) => { if (b) b._text = (b._text || '') + p; } }, AkanaI18n: makeI18nStub(), ...noopTimers },
    document: { createElement: (t) => makeEl(t) },
    CSS: { escape: (s) => s },
    console,
  };
  ctx.window.window = ctx.window;
  ctx.window.CSS = ctx.CSS;
  vm.runInNewContext(read("akana-chat-render.js"), ctx);
  return ctx.window.AkanaChatRender;
})();

check("tool: 0-arg call prints no chip but card is built", () => {
  const c = Render.renderToolCall({ id: "z0", name: "grep", phase: "end" });
  assert.equal(c.tagName, "DETAILS");
  assert.ok(c.classList.contains("tool-call"));
  assert.equal(c.dataset.status, "done");
  assert.equal(findOne(c, ".tool-call-chip"), null, "argument-less call must have no chip");
});
check("tool: dev arg is trimmed in compact card (…)", () => {
  // New compact design: no chip; arg is embedded in the human-readable action sentence
  // (action-card-title) and trimmed. A very long argument must not bloat the title.
  const big = "/very/long/" + "x".repeat(300);
  const c = Render.renderToolCall({ id: "z1", name: "read_file", phase: "end", args: { file_path: big } });
  const title = findOne(c, ".action-card-title");
  assert.ok(title, "action-card-title must exist");
  assert.ok(title.textContent.length <= 130, `title within reasonable bound, was ${title.textContent.length}`);
  assert.ok(title.textContent.includes("…"), "long arg must include … from trimming");
});
check("tool: unicode arg passes through intact", () => {
  const c = Render.renderToolCall({ id: "z2", name: "web_search", phase: "end", args: { query: "İstanbul 北京 🚀" } });
  const title = findOne(c, ".action-card-title");
  assert.ok(title.textContent.includes("İstanbul 北京 🚀"), "unicode arg must pass through intact in the title");
});
check("tool: unknown tool labels with raw name + 🔧", () => {
  const lbl = Render.toolCallLabelTr({ name: "tamamen_bilinmeyen_arac" });
  assert.equal(lbl.icon, "🔧");
  assert.equal(lbl.label, "tamamen_bilinmeyen_arac");
});
check("tool: error status reflected in state", () => {
  assert.equal(Render.toolCallStatus({ status: "denied" }), "error");
  assert.equal(Render.toolCallStatus({ error: "x" }), "error");
  assert.equal(Render.toolCallStatus({ phase: "start" }), "running");
  assert.equal(Render.toolCallStatus({ phase: "end" }), "done");
});
check("tool: too many tools dedupe by id (upsert single card)", () => {
  const body = makeEl("div");
  const bubble = makeEl("div");
  body.appendChild(bubble);
  const ins = (n) => body.insertBefore(n, bubble);
  for (let i = 0; i < 40; i++) {
    Render.upsertToolCallCard(body, { id: "k" + (i % 6), name: "grep", phase: i < 20 ? "start" : "end" }, ins);
  }
  assert.equal(findAll(body, ".tool-call").length, 6, "6 unique ids → 6 cards (storm dedupe)");
});
check("tool: process card title counts UNIQUE cards under start+end bloat (4 records → '2 tools')", () => {
  // The orchestrator emits TWO records per tool (phase=start then end, same id).
  // renderToolProcessCard dedups cards by id; the title must show the real card
  // count (2), NOT the raw array length (4) — otherwise an inconsistency arises
  // that writes "4 tools" but opens 2 cards (the two-chats-at-once bug).
  // Two DISTINCT flat tools (not `Task` — a Task call is the subagent boundary and
  // renders as its own group header, NOT a flat `.tool-call`, so it would not count).
  const calls = [
    { id: "a", name: "memory_recall", phase: "start", args: { q: "x" } },
    { id: "a", name: "memory_recall", phase: "end", result: "11 sonuç", status: "ok" },
    { id: "b", name: "grep", phase: "start", args: { pattern: "x" } },
    { id: "b", name: "grep", phase: "end", result: "0 kayıt", status: "ok" },
  ];
  const feed = Render.renderToolProcessCard(calls, "turn-x");
  assert.equal(findAll(feed, ".tool-call").length, 2, "4 records / 2 unique ids → 2 cards");
  assert.equal(
    findOne(feed, ".aur-process-label").textContent,
    "2 tools",
    "title must be the dedup card count (2 not 4, i18n EN)",
  );
});
check("tool: generic→shell family flip mounts term card (stuck 'waiting for result' fix)", () => {
  // Cursor / generic-MCP tools emit a `start` with no name and no command arg, so the
  // FE renders a GENERIC lazy Input/Output card (family ""). The command lands only at
  // `end`, flipping family to "shell". patchToolCallCard must UPGRADE that card to a
  // terminal body — the old code called renderTermCard() but discarded its return when
  // no .term-card existed, and skipped the panel sync, freezing the body forever on
  // "Running…/Waiting for result…" while the header showed done.
  const tl = makeEl("div");
  const start = Render.upsertToolCardIntoTimeline(tl, { id: "sh1", phase: "start" });
  assert.ok(start, "generic start card created");
  assert.equal(start.dataset.status, "running", "starts running");
  assert.equal(findOne(start, ".term-card"), null, "no term card at generic start (family unknown)");
  // End: args reveal a shell command + a multi-line result arrives → family flips to shell.
  const end = Render.upsertToolCardIntoTimeline(tl, {
    id: "sh1", phase: "end", args: { command: "echo hi" }, result: "hi\nbye", status: "ok",
  });
  assert.equal(end, start, "same id → same card patched (no duplicate)");
  assert.equal(findAll(tl, ".tool-call").length, 1, "still a single card");
  assert.equal(end.dataset.status, "done", "status flips to done");
  assert.ok(findOne(end, ".term-card"), "generic→shell flip must mount the term card (regression)");
  // The stale generic panels must be gone (no leftover 'waiting for result' placeholder).
  assert.equal(findOne(end, ".action-card-panel"), null, "stale Input/Output panels removed");
  assert.notEqual(end.dataset.lazyPanels, "1", "lazy-panel flag cleared after flip");
});
check("tool: shell-from-start still patches term card in place (no flip regression)", () => {
  // A card that is shell from the FIRST event must keep updating its single term-card in
  // place on `end` — the fix's else-branch — with no duplicate term body.
  const tl = makeEl("div");
  const s = Render.upsertToolCardIntoTimeline(tl, { id: "b1", name: "Bash", phase: "start", args: { command: "ls" } });
  assert.ok(findOne(s, ".term-card"), "shell start builds a term card immediately");
  const e = Render.upsertToolCardIntoTimeline(tl, { id: "b1", name: "Bash", phase: "end", result: "a\nb", status: "ok" });
  assert.equal(e, s, "same id → same card");
  assert.equal(findAll(e, ".term-card").length, 1, "exactly one term card (no duplicate on end)");
  assert.equal(e.dataset.status, "done", "done after end");
});
check("tool: no duration → no badge, 1500ms → '1.5 s', 120ms → '120 ms'", () => {
  assert.equal(findOne(Render.renderToolCall({ id: "d0", name: "x", phase: "start" }), ".action-card-duration"), null);
  assert.equal(findOne(Render.renderToolCall({ id: "d1", name: "x", phase: "end", duration_ms: 1500 }), ".action-card-duration").textContent, "1.5 s");
  assert.equal(findOne(Render.renderToolCall({ id: "d2", name: "x", phase: "end", duration_ms: 120 }), ".action-card-duration").textContent, "120 ms");
});
check("tool: renderSkillUse empty list → null", () => {
  assert.equal(Render.renderSkillUse([]), null);
  assert.equal(Render.renderSkillUse(null), null);
});
check("memory: renderMemoryUse empty items → null", () => {
  assert.equal(Render.renderMemoryUse({ items: [] }), null);
  assert.equal(Render.renderMemoryUse({}), null);
});

// ═══════════════════════ TRANSPORT — SSE RESILIENCE ═══════════════════════
function makeTransport(sse, hooksOverride = {}) {
  function reader() {
    const enc = new TextEncoder().encode(sse);
    let read = 0;
    return { getReader: () => ({ read: async () => (read++ === 0 ? { value: enc, done: false } : { done: true }), releaseLock() {} }) };
  }
  const ctx = {
    window: { AkanaCore: { baseUrl: () => "http://x", authHeaders: () => ({}), parseApiError: (b, s) => String(s), escapeHtml: (s) => s }, AkanaMarkdown: { setBubbleMarkdown: (b, t) => { b._text = t; }, appendBubbleStreamText: (b, p) => { if (b) b._text = (b._text || '') + p; } }, AkanaChatRender: Render, AkanaI18n: makeI18nStub(), CSS: { escape: (s) => s } },
    document: { createElement: (t) => makeEl(t), createElementNS: (_n, t) => makeEl(t), body: {} },
    navigator: {},
    CSS: { escape: (s) => s },
    requestAnimationFrame: (fn) => { setTimeout(fn, 0); return 1; },
    cancelAnimationFrame: () => {},
    setTimeout, clearTimeout, TextDecoder, TextEncoder, AbortController, console,
    fetch: async () => ({ ok: true, status: 200, body: reader(), json: async () => ({}) }),
  };
  ctx.window.window = ctx.window;
  vm.runInNewContext(read("akana-chat-transport.js"), ctx);
  const log = makeEl("div");
  let synced = 0;
  const t = ctx.window.AkanaChatTransport.create({
    hooks: { log, logScroll: log, updateEmptyState() {}, stickToBottomIfFollowing() {}, ttsPlayer: null, streamTtsParam: () => "", showToast() {}, sendBtn: { disabled: false }, ...hooksOverride },
    conversationIdForMemory: () => "c",
    setConversationId() {},
    syncConversationLogFromServer: () => { synced += 1; },
    applyChatServerAction() {},
    consumePendingImageIds: () => [],
  });
  return { t, log, getSynced: () => synced, bubbleText: () => log.children[0]?.children[1]?.children[1]?._text };
}

check("sse: unknown event type is silently skipped (delta+done preserved)", async () => {
  const { t, bubbleText } = makeTransport('event: delta\ndata: {"text":"Hi"}\n\nevent: bogus_evt\ndata: {"k":1}\n\nevent: done\ndata: {"text":"Hi there"}\n\n');
  assert.equal(await t.resumeActiveTurn("c"), true);
  assert.equal(bubbleText(), "Hi there", "unknown event must not break the stream");
});
check("sse: broken JSON frame skipped, next done recovers", async () => {
  const { t, bubbleText } = makeTransport('event: delta\ndata: {bozuk json\n\nevent: done\ndata: {"text":"Kurtarıldı"}\n\n');
  assert.equal(await t.resumeActiveTurn("c"), true);
  assert.equal(bubbleText(), "Kurtarıldı", "parse error must not drop the stream");
});
check("sse: out-of-order (done first, delta after) — done text stays", async () => {
  const { t, bubbleText, getSynced } = makeTransport('event: done\ndata: {"text":"Final"}\n\nevent: delta\ndata: {"text":"geç"}\n\n');
  assert.equal(await t.resumeActiveTurn("c"), true);
  assert.equal(bubbleText(), "Final", "late delta after done must not overwrite the done text");
  assert.equal(getSynced(), 1, "store must refresh exactly once after done");
});
check("sse: multi-line data field joins with \\n", async () => {
  const { t, bubbleText } = makeTransport('event: done\ndata: {"text":"a\\nb"}\n\n');
  assert.equal(await t.resumeActiveTurn("c"), true);
  assert.equal(bubbleText(), "a\nb");
});
check("sse: empty done (no text) → error bubble", async () => {
  const { t, log } = makeTransport("event: done\ndata: {}\n\n");
  assert.equal(await t.resumeActiveTurn("c"), true);
  const bubble = log.children[0].children[1].children[1];
  assert.ok(bubble.classList.contains("bubble-bot-err"), "empty response must get the error class");
});
check("sse: half frame (no trailing \\n\\n) is processed on the final read flush", async () => {
  const { t, bubbleText } = makeTransport('event: done\ndata: {"text":"Yarim"}');
  assert.equal(await t.resumeActiveTurn("c"), true);
  assert.equal(bubbleText(), "Yarim", "remaining buffer must be flushed on close");
});

check("transport: chatPayload NEVER sends an empty file_ids field", () => {
  const { t } = makeTransport("event: done\ndata: {}\n\n");
  const p = t.chatPayload("merhaba");
  assert.equal(p.text, "merhaba");
  assert.ok(!("file_ids" in p), "empty attachment must have no file_ids field");
  assert.ok(!("image_ids" in p), "empty attachment must have no image_ids field either");
});
check("transport: file_ids added to payload when present (PHASE2 multi-type)", () => {
  const ctx = {
    window: { AkanaCore: { baseUrl: () => "http://x", authHeaders: () => ({}), parseApiError: (b, s) => String(s), escapeHtml: (s) => s }, AkanaMarkdown: { setBubbleMarkdown: () => {}, appendBubbleStreamText: (b, p) => { if (b) b._text = (b._text || '') + p; } }, AkanaChatRender: Render, AkanaI18n: makeI18nStub(), CSS: { escape: (s) => s } },
    document: { createElement: (t) => makeEl(t), body: {} },
    navigator: {}, CSS: { escape: (s) => s }, requestAnimationFrame: (fn) => setTimeout(fn, 0), cancelAnimationFrame: () => {},
    setTimeout, clearTimeout, TextDecoder, AbortController, console, fetch: async () => ({}),
  };
  ctx.window.window = ctx.window;
  vm.runInNewContext(read("akana-chat-transport.js"), ctx);
  const t = ctx.window.AkanaChatTransport.create({ hooks: { log: makeEl("div") }, conversationIdForMemory: () => "c", setConversationId() {}, syncConversationLogFromServer() {}, applyChatServerAction() {}, consumePendingFileIds: () => ["file-1", "file-2"] });
  const p = t.chatPayload("selam");
  assert.deepEqual(p.file_ids, ["file-1", "file-2"], "attached file ids must enter the payload");
  assert.ok(!("image_ids" in p), "new path must not send image_ids");
});
check("transport: chatPayload uses opts.fileIds, does not re-consume (b30)", () => {
  const ctx = {
    window: { AkanaCore: { baseUrl: () => "http://x", authHeaders: () => ({}), parseApiError: (b, s) => String(s), escapeHtml: (s) => s }, AkanaMarkdown: { setBubbleMarkdown: () => {}, appendBubbleStreamText: (b, p) => { if (b) b._text = (b._text || '') + p; } }, AkanaChatRender: Render, AkanaI18n: makeI18nStub(), CSS: { escape: (s) => s } },
    document: { createElement: (t) => makeEl(t), body: {} },
    navigator: {}, CSS: { escape: (s) => s }, requestAnimationFrame: (fn) => setTimeout(fn, 0), cancelAnimationFrame: () => {},
    setTimeout, clearTimeout, TextDecoder, AbortController, console, fetch: async () => ({}),
  };
  ctx.window.window = ctx.window;
  vm.runInNewContext(read("akana-chat-transport.js"), ctx);
  let consumeCalls = 0;
  const t = ctx.window.AkanaChatTransport.create({ hooks: { log: makeEl("div") }, conversationIdForMemory: () => "c", setConversationId() {}, syncConversationLogFromServer() {}, applyChatServerAction() {}, consumePendingFileIds: () => { consumeCalls += 1; return ["LATE-should-not-be-used"]; } });
  // b30: the caller already consumed the ids up front → chatPayload must use THAT set verbatim
  // and NOT re-consume (which diverged from the optimistic echo across the send window).
  const p = t.chatPayload("selam", false, { fileIds: ["pre-1", "pre-2"] });
  assert.deepEqual(p.file_ids, ["pre-1", "pre-2"], "b30: pre-consumed ids must be used");
  assert.equal(consumeCalls, 0, "b30: chatPayload must NOT re-consume when opts.fileIds is present");
});
check("transport: humanizeChatError routes network error via i18n EN", () => {
  const { t } = makeTransport("event: done\ndata: {}\n\n");
  const msg = t.humanizeChatError(Object.assign(new TypeError("Failed to fetch")));
  assert.ok(msg.includes("Connection to server lost"), "network error must yield i18n EN routing");
});
check("transport: probeActiveTurn 204/404/405 → null", async () => {
  for (const status of [204, 404, 405]) {
    const ctx = {
      window: { AkanaCore: { baseUrl: () => "http://x", authHeaders: () => ({}), parseApiError: (b, s) => String(s), escapeHtml: (s) => s }, AkanaMarkdown: { setBubbleMarkdown: () => {}, appendBubbleStreamText: (b, p) => { if (b) b._text = (b._text || '') + p; } }, AkanaChatRender: Render, AkanaI18n: makeI18nStub(), CSS: { escape: (s) => s } },
      document: { createElement: (t) => makeEl(t), body: {} },
      navigator: {}, CSS: { escape: (s) => s }, requestAnimationFrame: (fn) => setTimeout(fn, 0), cancelAnimationFrame: () => {},
      setTimeout, clearTimeout, TextDecoder, AbortController, console,
      fetch: async () => ({ ok: false, status, body: null, json: async () => ({}) }),
    };
    ctx.window.window = ctx.window;
    vm.runInNewContext(read("akana-chat-transport.js"), ctx);
    const t = ctx.window.AkanaChatTransport.create({ hooks: { log: makeEl("div") }, conversationIdForMemory: () => "c", setConversationId() {}, syncConversationLogFromServer() {}, applyChatServerAction() {}, consumePendingImageIds: () => [] });
    assert.equal(await t.probeActiveTurn("c"), null, `${status} → null`);
    assert.equal(await t.resumeActiveTurn("c"), false, `${status} → resume false`);
  }
});

// ═════════════════════════════════ VOICE FSM ════════════════════════════════
{
  // The FSM prints console.warn on unknown-phase rejection; keep it from polluting the harness output.
  const ctx = { console: { ...console, warn: () => {} }, window: {} };
  ctx.window.window = ctx.window;
  vm.runInNewContext(read("akana-voice-fsm.js"), ctx);
  const { Phase, createVoiceSession } = ctx.window.AkanaVoiceFsm;

  check("fsm: unknown event rejected even with force, phase preserved", () => {
    const s = createVoiceSession({});
    s.transition(Phase.CAPTURE_WAKE, "t");
    assert.equal(s.transition("uydurma_faz", "x", { force: true }), false);
    assert.equal(s.getPhase(), Phase.CAPTURE_WAKE);
    assert.equal(s.transition(Phase.CAPTURE_MIC, "y"), true, "next valid transition must still work");
  });
  check("fsm: capture→idle epoch bump (stale finalize shield)", () => {
    const s = createVoiceSession({});
    s.transition(Phase.CAPTURE_MIC, "t");
    const e0 = s.getEpoch();
    s.transition(Phase.IDLE, "handoff");
    assert.ok(s.getEpoch() > e0);
  });
  check("fsm: cancelAll preserves the wake preference", () => {
    const s = createVoiceSession({});
    s.setWakeArmed(true, "t");
    s.transition(Phase.CAPTURE_WAKE, "t");
    assert.equal(s.cancelAll("esc"), true);
    assert.equal(s.getPhase(), Phase.WAKE_ARMED);
  });
}

// ═════════════════════════════════ BUS — SILENT DISPATCH ═════════════════════
{
  const ctx = { console: { error: () => {} }, window: {} };
  ctx.window.window = ctx.window;
  vm.runInNewContext(read("akana-bus.js"), ctx);
  const Bus = ctx.window.AkanaBus;

  check("bus: emit with no listeners is a silent no-op", () => {
    assert.doesNotThrow(() => Bus.emit("hic:kimse:yok", { a: 1 }));
  });
  check("bus: if one handler throws the others still run (isolated)", () => {
    let b = 0;
    Bus.on("x:y:z", () => { throw new Error("boom"); });
    Bus.on("x:y:z", () => { b += 1; });
    Bus.emit("x:y:z", {});
    assert.equal(b, 1, "a throwing handler must not block the other");
  });
  check("bus: off and invalid on arguments are safe", () => {
    assert.equal(typeof Bus.on(123, () => {}), "function");
    assert.doesNotThrow(() => Bus.off("yok", () => {}));
  });
  check("bus: once fires only once", () => {
    let n = 0;
    Bus.once("o:n:e", () => { n += 1; });
    Bus.emit("o:n:e", {});
    Bus.emit("o:n:e", {});
    assert.equal(n, 1);
  });
}

// ═══════════════════════════ ARCHIVE — SEARCH + EMPTY LIST ════════════════════
{
  const els = new Map();
  const listEl = makeEl("ul");
  listEl.id = "chat-archive-list";
  els.set("chat-archive-list", listEl);
  const searchEl = makeEl("input");
  searchEl.id = "chat-archive-search";
  searchEl.value = "";
  els.set("chat-archive-search", searchEl);

  const doc = {
    getElementById: (id) => els.get(id) || null,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: (t) => makeEl(t),
    createElementNS: (_ns, t) => makeEl(t),
    addEventListener: () => {},
  };
  const ctx = {
    window: {
      AkanaCore: { baseUrl: () => "http://x", authHeaders: () => ({}), parseApiError: (b, s) => String(s) },
      AkanaI18n: makeI18nStub(),
      matchMedia: () => ({ matches: false }),
      localStorage: { getItem: () => null, setItem: () => {} },
    },
    document: doc, console, setTimeout, clearTimeout,
    // renderChatArchiveList (v2 sidebar) wraps painting in requestAnimationFrame;
    // the browser global is absent in the vm context → stub (267/331/351 pattern).
    requestAnimationFrame: (fn) => { setTimeout(fn, 0); return 1; },
    cancelAnimationFrame: () => {},
    // Archive builds the archive-item selector with `CSS.escape(id)` (bare global);
    // window.CSS.escape exists in the browser but not in the vm context → stub required.
    CSS: { escape: (s) => s },
    fetch: async () => ({ ok: true, json: async () => ({}) }),
  };
  ctx.window.window = ctx.window;
  ctx.window.document = doc;
  ctx.window.CSS = ctx.CSS;
  vm.runInNewContext(read("akana-chat-archive.js"), ctx);
  const archive = ctx.window.AkanaChatArchive.createArchive({
    bridge: { hooks: { shortConversationId: (id) => String(id).slice(0, 6), showToast: () => {} } },
    conversationIdForMemory: () => "c-active",
    switchChatConversation: () => {},
  });

  const items = [
    { id: "c-active", title: "Hava durumu sohbeti", preview: "yarın yağmur", message_count: 3 },
    { id: "c2", title: "Kod incelemesi", preview: "PR #42", message_count: 8 },
  ];

  check("archive: no search match → 'No search results' (i18n EN)", () => {
    archive.setChatArchiveItems(items);
    searchEl.value = "zzz-bulunamaz";
    archive.renderChatArchiveList(items);
    const empty = findOne(listEl, ".chat-archive-empty");
    assert.ok(empty, "empty-state row is required");
    assert.equal(empty.textContent, "No search results");
  });
  check("archive: matching search lists the relevant chat", () => {
    searchEl.value = "kod";
    archive.renderChatArchiveList(items);
    const titles = findAll(listEl, ".chat-archive-item-title").map((t) => t.textContent);
    assert.ok(titles.includes("Kod incelemesi"), "matching title must be visible");
    assert.ok(!titles.includes("Hava durumu sohbeti"), "non-matching must be filtered out");
  });
  check("archive: fully empty list 'No saved chats yet' (active view, i18n EN)", () => {
    searchEl.value = "";
    archive.renderChatArchiveList([]);
    const empty = findOne(listEl, ".chat-archive-empty");
    assert.ok(empty.textContent.includes("No saved chats yet"));
  });
  check("archive: get/setChatArchiveItems round-trip", () => {
    archive.setChatArchiveItems(items);
    assert.equal(archive.getChatArchiveItems().length, 2);
  });

  // ── ARCHIVE vs DELETE · tombstone is ONLY for delete (archive shows without F5) ──
  // ROOT BUG (user): "archive a chat → switch to the archive tab → chat MISSING;
  // it shows up after F5". Root cause: archiveConversationById → removeArchiveRow →
  // tombstoneConv(id) → renderChatArchiveList filters out the tombstoned id in ALL
  // views (the tombstone filter runs BEFORE the view filter). Tombstone is specific
  // to DELETE (permanent); archive is a MOVE (active→archive) → must not be tombstoned.
  // The chat only appears once F5 resets the in-memory _deletedConvIds set.
  check("archive: removeArchiveRow(tombstone:false) does NOT drop the chat (archive shows without F5)", () => {
    searchEl.value = "";
    archive.setChatArchiveItems([]);
    archive.removeArchiveRow("arc-1", { tombstone: false }); // archive path
    archive.renderChatArchiveList([{ id: "arc-1", title: "Arşivlenen sohbet" }]);
    const titles = findAll(listEl, ".chat-archive-item-title").map((t) => t.textContent);
    assert.ok(
      titles.includes("Arşivlenen sohbet"),
      "archived chat must not be tombstoned → stale/new render must NOT drop it (no F5 needed)",
    );
  });
  check("archive: removeArchiveRow(default) drops the DELETED chat (tombstone permanent)", () => {
    searchEl.value = "";
    archive.setChatArchiveItems([]);
    archive.removeArchiveRow("del-1"); // delete path — default tombstone
    archive.renderChatArchiveList([{ id: "del-1", title: "Silinen sohbet" }]);
    const titles = findAll(listEl, ".chat-archive-item-title").map((t) => t.textContent);
    assert.ok(
      !titles.includes("Silinen sohbet"),
      "deleted chat is tombstoned → stale render must NOT bring it back (regression: 'gone on 2nd delete')",
    );
  });
}

console.log(`ui_edge_cases.harness: ${passed} edge-case contracts PASSED ✓`);

// Prevent a dangling timer from hanging the node process — hard exit on success.
if (typeof process !== "undefined" && process.exit) process.exit(0);
