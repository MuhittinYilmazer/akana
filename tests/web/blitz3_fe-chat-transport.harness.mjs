/**
 * Bug-blitz 3 — fe-chat-transport regression harness (backend-free, node-vm + fake DOM).
 * Loads the REAL web_ui/static/akana-chat-transport.js in a VM with a fake DOM and
 * drives streamChat / finalizeThoughtFeed to lock down four verified fixes. Each
 * finding is proved by a discriminating contract: the REAL source must PASS while a
 * synthetic variant that string-reverts ONLY that fix must FAIL (exhibit the bug) —
 * so the test is shown to actually catch the regression (RED) and the fix cures it (GREEN).
 *
 *  1. streamChat samples the conv id AFTER `await fetch` → a mid-connect chat switch
 *     routes the stream row into the wrong pane. Fix: sample once BEFORE the fetch.
 *  3. Turn HUD pill is never removed on transport-level (CONN/EMPTY) serverError paths.
 *  4. finalizeThoughtFeed hardcodes the Turkish "sn" seconds abbreviation.
 *  5. Memory-toast key-list fallback is the hardcoded Turkish word "bilgi".
 *
 * Run: node tests/web/blitz3_fe-chat-transport.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { makeI18nStub } from "./_i18n_stub.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const TRANSPORT_SRC = readFileSync(path.join(REPO, "web_ui/static/akana-chat-transport.js"), "utf8");

let passed = 0;
const failures = [];
function check(label, fn) {
  try {
    fn();
    passed += 1;
  } catch (e) {
    failures.push(`${label}: ${e && e.message ? e.message : e}`);
  }
}
async function checkAsync(label, fn) {
  try {
    await fn();
    passed += 1;
  } catch (e) {
    failures.push(`${label}: ${e && e.message ? e.message : e}`);
  }
}

/** String-revert a single fix so the variant exhibits the original bug. Asserts the
 *  anchor exists first, so drift in the product code fails loudly instead of silently
 *  producing an unmodified copy. */
function patch(src, from, to) {
  assert.ok(src.includes(from), `patch anchor not found (code drifted): ${JSON.stringify(from.slice(0, 60))}`);
  return src.split(from).join(to);
}

// ── Fake DOM (only the surfaces the driven paths touch) ─────────────────────────
function selMatch(el, sel) {
  if (!el || !el.classList) return false;
  const s = sel.trim();
  if (s.startsWith(".")) return el.classList.contains(s.slice(1));
  if (s.startsWith("[")) {
    const m = s.match(/^\[([\w-]+)(?:=["']?([^"'\]]*)["']?)?\]$/);
    if (!m) return false;
    const attr = m[1];
    if (attr.startsWith("data-")) {
      const key = attr.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      return el.dataset[key] != null && (m[2] == null || el.dataset[key] === m[2]);
    }
    return el.attrs[attr] != null && (m[2] == null || el.attrs[attr] === m[2]);
  }
  return el.tagName === s.toUpperCase();
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
    innerHTML: "",
    classList: {
      _s: new Set(),
      add(...c) { c.forEach((x) => this._s.add(x)); },
      remove(...c) { c.forEach((x) => this._s.delete(x)); },
      toggle(c, on) { const w = on === undefined ? !this._s.has(c) : !!on; if (w) this._s.add(c); else this._s.delete(c); return w; },
      contains(c) { return this._s.has(c); },
    },
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); if (v === "") { this.children = []; this.childNodes = []; } },
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
    closest() { return null; },
    contains(node) { let n = node; while (n) { if (n === this) return true; n = n.parentNode; } return false; },
    querySelector(sel) { let out = null; walk(this, (n) => { if (!out && selMatch(n, sel)) out = n; }); return out; },
    querySelectorAll(sel) { const out = []; walk(this, (n) => { if (selMatch(n, sel)) out.push(n); }); return out; },
  };
  return el;
}

/** SSE body whose reader yields each utf8 chunk once, then done. */
function makeSseBody(chunks) {
  const enc = new TextEncoder();
  const frames = chunks.map((c) => enc.encode(c));
  let i = 0;
  return {
    getReader: () => ({
      read: async () => (i < frames.length ? { value: frames[i++], done: false } : { done: true }),
      releaseLock: () => {},
    }),
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
      // rAF MUST invoke the callback (the SSE drain / reveal schedule via it) — a no-op
      // leaves flushSseQueue's promise pending forever (streamChat never settles).
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

/** Instantiate a transport from `src` with a chatCtx. Returns handles for assertions. */
function loadTransport(src, fetchImpl, overrides = {}, lang = "en") {
  const ctx = makeCtx(fetchImpl, lang);
  vm.runInNewContext(src, ctx);
  const log = makeEl("div");
  const toasts = [];
  const chatCtx = {
    hooks: {
      log,
      logScroll: log,
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
    syncConversationLogFromServer: () => {},
    applyChatServerAction: () => {},
    consumePendingImageIds: () => [],
    consumePendingFileIds: () => [],
    ...overrides.chatCtx,
  };
  const inst = ctx.window.AkanaChatTransport.create(chatCtx);
  return { inst, ctx, log, toasts, chatCtx, document: ctx.document };
}

// ──────────────────────────────────────────────────────────────────────────────
// Finding 1: mid-connect chat switch must NOT reroute the stream row into the
// pane of whatever chat is displayed when the fetch headers land. The conv id must
// be sampled ONCE before the fetch and used for pane routing.
// ──────────────────────────────────────────────────────────────────────────────
async function runFinding1(src) {
  const paneA = makeEl("div");
  const paneB = makeEl("div");
  const panes = { A: paneA, B: paneB };
  let cur = "A"; // displayed conv at send time
  let sentBody = null;
  // fetch resolves AFTER a synchronous switch A→B (models the connect window race).
  const fetchImpl = async (url, opts) => {
    if (String(url).includes("/chat/stream")) {
      sentBody = JSON.parse(opts.body);
      cur = "B"; // user clicked chat B while the connection was being established
    }
    // Empty clean-close body → EMPTY serverError → streamChat throws (row already placed).
    return { ok: true, status: 200, body: makeSseBody([]), json: async () => ({}) };
  };
  const { inst } = loadTransport(src, fetchImpl, {
    hooks: { paneFor: (id) => panes[id] || null },
    chatCtx: { conversationIdForMemory: () => cur },
  });
  try {
    await inst.streamChat("hello");
  } catch {
    /* EMPTY serverError is expected — the row placement is what we assert */
  }
  return { paneA, paneB, sentBody };
}

await checkAsync("f1: REAL — row routes to the SENDING conv's pane (A), not the switched-to pane (B)", async () => {
  const { paneA, paneB, sentBody } = await runFinding1(TRANSPORT_SRC);
  assert.equal(sentBody.conversation_id, "A", "POST body conversation_id must be the sending conv A");
  assert.ok(paneA.querySelector(".row-assistant"), "assistant row must land in the sending conv's pane (A)");
  assert.equal(paneB.querySelector(".row-assistant"), null, "assistant row must NOT land in the switched-to pane (B)");
});

await checkAsync("f1: RED variant (streamConvId re-read after await) routes the row into the WRONG pane (B)", async () => {
  const buggy = patch(
    TRANSPORT_SRC,
    "const streamConvId = boundConvId;",
    "const streamConvId = chatCtx.conversationIdForMemory() || null;",
  );
  const { paneA, paneB } = await runFinding1(buggy);
  assert.ok(paneB.querySelector(".row-assistant"), "buggy code must route the row into pane B (proves the test discriminates)");
  assert.equal(paneA.querySelector(".row-assistant"), null, "buggy code leaves the sending pane A empty");
});

// ──────────────────────────────────────────────────────────────────────────────
// Finding 3: on a transport-level failure (clean close with no `done`/text → EMPTY
// serverError) the live usage HUD pill must be removed, not left dangling.
// ──────────────────────────────────────────────────────────────────────────────
async function runFinding3(src) {
  // usage event (creates the HUD pill) then clean close with no done → EMPTY serverError.
  const body = makeSseBody(['event: usage\ndata: {"prompt":100,"completion":50}\n\n']);
  const fetchImpl = async () => ({ ok: true, status: 200, body, json: async () => ({}) });
  const pane = makeEl("div");
  const { inst, document: doc } = loadTransport(src, fetchImpl, {
    hooks: { paneFor: () => pane },
    chatCtx: { conversationIdForMemory: () => "conv-1" },
  });
  doc.body.classList.add("show-usage"); // usageDisplayEnabled() gate
  try {
    await inst.streamChat("hi");
  } catch {
    /* EMPTY serverError is expected */
  }
  return pane;
}

await checkAsync("f3: REAL — HUD pill is removed on EMPTY serverError (transport-level failure)", async () => {
  const pane = await runFinding3(TRANSPORT_SRC);
  assert.ok(pane.querySelector(".turn-hud") == null, "the live .turn-hud pill must NOT linger after a transport-level failure");
});

await checkAsync("f3: RED variant (no removeTurnHud in serverError branch) leaves the HUD pill dangling", async () => {
  // CRLF-tolerant removal of ONLY the streamChat serverError-branch removeTurnHud (keyed
  // to its unique comment) — reverts finding-3's fix on that path.
  const re = /[ \t]*\/\/ Transport-level failure \(CONN\/EMPTY\) never fires[^\n]*\r?\n[ \t]*\/\/ remove the live usage HUD pill[^\n]*\r?\n[ \t]*removeTurnHud\(streamCtx\);\r?\n/;
  assert.ok(re.test(TRANSPORT_SRC), "f3 revert anchor not found (code drifted)");
  const buggy = TRANSPORT_SRC.replace(re, "");
  const pane = await runFinding3(buggy);
  const hud = pane.querySelector(".turn-hud");
  assert.ok(hud, "buggy code must leave the .turn-hud pill attached (proves the test discriminates)");
});

// ──────────────────────────────────────────────────────────────────────────────
// Finding 4: finalizeThoughtFeed's duration subtitle must use "s" (matching the meta
// line), not the Turkish "sn", in the default English UI.
// ──────────────────────────────────────────────────────────────────────────────
function buildThoughtFeed(doc) {
  // Minimal process-card shape finalizeThoughtFeed walks: a body with one thought line
  // (so it is not removed as empty) + the label/sub spans it rewrites.
  const feed = makeEl("div");
  feed.classList.add("akana-thought-feed");
  feed.dataset.finalized = "0";
  const fbody = makeEl("div"); fbody.classList.add("akana-thought-feed-body");
  const line = makeEl("div"); line.classList.add("akana-thought-line", "akana-thought-line--think");
  fbody.appendChild(line);
  const label = makeEl("div"); label.classList.add("aur-process-label");
  const sub = makeEl("div"); sub.classList.add("aur-process-sub");
  const head = makeEl("div"); head.classList.add("akana-thought-feed-head");
  feed.append(head, label, sub, fbody);
  void doc;
  return { feed, sub };
}

function runFinding4(src) {
  const { inst, document: doc } = loadTransport(src, async () => ({}));
  const T = inst.__test;
  assert.ok(T && typeof T.finalizeThoughtFeed === "function", "__test.finalizeThoughtFeed seam must exist");
  const { feed, sub } = buildThoughtFeed(doc);
  T.finalizeThoughtFeed({ thoughtFeed: feed, turnId: null }, { latency_ms: 2000 });
  return sub.textContent;
}

check("f4: REAL — duration subtitle uses 's' (English default), not the Turkish 'sn'", () => {
  const txt = runFinding4(TRANSPORT_SRC);
  assert.equal(txt, "2.0s", `subtitle must read '2.0s' (got ${JSON.stringify(txt)})`);
  assert.ok(!/\bsn\b/.test(txt), "subtitle must not contain the Turkish 'sn'");
});

check("f4: RED variant (' sn' literal) leaks Turkish into the subtitle", () => {
  const buggy = patch(TRANSPORT_SRC, "(elapsed / 1000).toFixed(1)}s", "(elapsed / 1000).toFixed(1)} sn");
  const txt = runFinding4(buggy);
  assert.equal(txt, "2.0 sn", "buggy code must produce '2.0 sn' (proves the test discriminates)");
});

// ──────────────────────────────────────────────────────────────────────────────
// Finding 5: the memory-toast key-list fallback for a keyless memory_writes entry must
// be i18n'd (English 'info' by default), not the hardcoded Turkish 'bilgi'.
// ──────────────────────────────────────────────────────────────────────────────
async function runFinding5(src, lang) {
  // done event carrying a keyless staging memory write → memory_staged toast.
  const done = 'event: done\ndata: {"text":"ok","turn_id":"t1","memory_writes":[{"id":"m1","kind":"staging"}]}\n\n';
  const body = makeSseBody([done]);
  const fetchImpl = async () => ({ ok: true, status: 200, body, json: async () => ({}) });
  const pane = makeEl("div");
  const { inst, toasts } = loadTransport(src, fetchImpl, {
    hooks: { paneFor: () => pane },
    chatCtx: { conversationIdForMemory: () => "conv-1" },
  }, lang);
  await inst.streamChat("remember this");
  return toasts;
}

await checkAsync("f5: REAL (EN) — keyless memory toast falls back to 'info', not 'bilgi'", async () => {
  const toasts = await runFinding5(TRANSPORT_SRC, "en");
  const staged = toasts.find((t) => /Inbox/i.test(t.msg));
  assert.ok(staged, "a memory_staged toast must be shown");
  assert.ok(staged.msg.includes("info"), `EN toast must splice the English fallback 'info' (got ${JSON.stringify(staged.msg)})`);
  assert.ok(!staged.msg.includes("bilgi"), "EN toast must not contain the Turkish 'bilgi'");
});

await checkAsync("f5: REAL (TR) — Turkish UI still shows 'bilgi' (fallback is localized, not dropped)", async () => {
  const toasts = await runFinding5(TRANSPORT_SRC, "tr");
  const staged = toasts.find((t) => /Inbox/i.test(t.msg));
  assert.ok(staged, "a memory_staged toast must be shown (TR)");
  assert.ok(staged.msg.includes("bilgi"), `TR toast must keep 'bilgi' (got ${JSON.stringify(staged.msg)})`);
});

await checkAsync("f5: RED variant (hardcoded 'bilgi') leaks Turkish into the English toast", async () => {
  const buggy = patch(
    TRANSPORT_SRC,
    'w.key || window.AkanaI18n.t("transport.toast.memory_key_fallback")',
    'w.key || "bilgi"',
  );
  const toasts = await runFinding5(buggy, "en");
  const staged = toasts.find((t) => /Inbox/i.test(t.msg));
  assert.ok(staged && staged.msg.includes("bilgi"), "buggy code must leak 'bilgi' into the EN toast (proves the test discriminates)");
});

// ──────────────────────────────────────────────────────────────────────────────
// Finding (review) 1: the per-pane chat-streaming flag must be cleared UNCONDITIONALLY
// when a stream ends. The old wasForeground||!fgRec gate guarded the SHARED #log; now
// that logRoot is a per-CONVERSATION pane it left a BACKGROUND-finished pane stuck at
// data-chat-streaming="1" forever (panes persist), which permanently skipped code-block
// decoration for that conversation on switch-back.
// ──────────────────────────────────────────────────────────────────────────────
async function runReviewFinding1(src) {
  // conv A streams in the BACKGROUND while conv B is the displayed (foreground) chat with
  // its OWN live stream → A finishes with wasForeground=false AND fgRec non-null: the exact
  // gate that left A's pane flag stuck under HEAD.
  const paneA = makeEl("div");
  const done = 'event: done\ndata: {"text":"ok","turn_id":"t1"}\n\n';
  const fetchImpl = async () => ({ ok: true, status: 200, body: makeSseBody([done]), json: async () => ({}) });
  const { inst } = loadTransport(src, fetchImpl, {
    hooks: { paneFor: (id) => (id === "A" ? paneA : makeEl("div")) },
    chatCtx: { conversationIdForMemory: () => "A" },
  });
  const T = inst.__test;
  // A already has a live stream (models the real flow: A was sent first, then B) so the
  // send's foreground-scoped abortActiveChatStream() is skipped (preserve same-conv) and
  // does NOT tear down B's record. B is the DISPLAYED chat with its own live stream.
  T.registerStream({ convId: "A", _streamKey: "A" }, {});
  T.setForegroundConversation("B");
  T.registerStream({ convId: "B", _streamKey: "B" }, {}); // fgRec ≠ null when A finishes
  await inst.streamChat("hi");                            // background send to A → completes cleanly
  return paneA;
}

await checkAsync("f1(review): REAL — a background-finished stream clears its OWN pane's chat-streaming flag", async () => {
  const paneA = await runReviewFinding1(TRANSPORT_SRC);
  assert.equal(paneA.dataset.chatStreaming, undefined,
    "the per-pane streaming flag must be cleared unconditionally when THIS stream ends (per-pane → no cross-clear risk)");
});

await checkAsync("f1(review): RED variant (foreground-gated clear) leaves a background pane's flag stuck at '1'", async () => {
  // CRLF-tolerant revert of ONLY the braced finally clear (the two single-line
  // `if (logRoot) delete …;` sites elsewhere lack the brace, so this stays unique).
  const re = /if \(logRoot\) \{\r?\n\s*delete logRoot\.dataset\.chatStreaming;\r?\n\s*\}/;
  assert.ok(re.test(TRANSPORT_SRC), "f1(review) revert anchor not found (code drifted)");
  const buggy = TRANSPORT_SRC.replace(
    re,
    "const fgRec = foregroundStreamRecord();\n      if (logRoot && (wasForeground || !fgRec)) {\n        delete logRoot.dataset.chatStreaming;\n      }",
  );
  const paneA = await runReviewFinding1(buggy);
  assert.equal(paneA.dataset.chatStreaming, "1",
    "the gated clear leaves the background-finished pane's flag stuck (proves the test discriminates)");
});

// ── Summary ─────────────────────────────────────────────────────────────────
if (failures.length) {
  console.error(`FAIL blitz3_fe-chat-transport — ${failures.length} failure(s):`);
  for (const f of failures) console.error("  - " + f);
  process.exit(1);
}
console.log(`PASS blitz3_fe-chat-transport — ${passed} checks green (findings 1, 3, 4, 5 + review-1 per-pane flag clear; each proved RED→GREEN)`);
process.exit(0);
