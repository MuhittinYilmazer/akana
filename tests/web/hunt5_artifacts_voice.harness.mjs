/**
 * HUNT 5 — "live state" family, artifacts + voice group. node-vm + fake DOM, backend-free.
 *
 * Both contracts locked here are the SAME root cause: UI state that is a MODULE SINGLETON
 * (one panel, one shared pane/composer/active-thread) being written by a producer that
 * belongs to a DIFFERENT conversation than the one on screen right now.
 *
 *  voice-vs-chat-3 (akana-voice-pipeline.js) — the single-shot wake POST /api/v1/voice takes
 *    seconds; parallel-chat lets the user open another chat meanwhile. On arrival the handler
 *    appended its rows into the DISPLAYED pane, recorded "[voice] …" into the ACTIVE thread,
 *    REBOUND that thread to the voice turn's conversation via setConversationId (so the next
 *    typed message went to the wrong conversation) and wiped the shared composer. Contract:
 *    snapshot the displayed conversation at POST time; every shared-state write is gated on it
 *    still being displayed. Only the conversation-targeted server sync stays unconditional.
 *
 *  artifacts-singleton (akana-artifacts.js + akana-shell.js) — the artifact panel keeps
 *    module-level els/current/lastTrigger and nothing closed or rebound it on a conversation
 *    switch, so chat A's artifact stayed open over chat B (its Copy/Download still served A's
 *    code) and lastTrigger pinned a node in a pane the LRU may have evicted. Contract:
 *    showConversation dismisses it — same site that already dismisses the msg action bar and
 *    the code-copy capsule — and the dismiss drops current/lastTrigger WITHOUT stealing focus
 *    back into the chat being left.
 *
 * Run: node tests/web/hunt5_artifacts_voice.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const read = (rel) => readFileSync(path.join(REPO, "web_ui/static", rel), "utf8");

let passed = 0;
const check = (label, fn) => { fn(); passed += 1; void label; };
const tick = () => new Promise((r) => setTimeout(r, 0));

/* ══════════════════════════════════════════════════════════════════════════════
   A. voice-vs-chat-3 — a mid-flight chat switch must not hijack the new chat
   ══════════════════════════════════════════════════════════════════════════════ */

class FormDataStub {
  constructor() { this.parts = []; }
  append(k, v, n) { this.parts.push([k, v, n]); }
}

function makeVoiceRig({ displayed: displayedAtStart = "convA", threadId = "th-1" } = {}) {
  const rec = { rows: [], recorded: [], setConvIds: [], synced: [] };
  let displayed = displayedAtStart;
  // The ACTIVE thread is the only identity an UNBOUND chat has: displayedConvId() is "" for
  // every new chat (there is one shared new-chat pane), so "+" changes this and nothing else.
  let activeThread = { id: threadId };
  let releaseFetch = null;
  const fetchGate = new Promise((r) => { releaseFetch = r; });
  let response = {
    ok: true,
    status: 200,
    body: {
      transcript: "summarize my notes",
      text: "here are your notes",
      latency_ms: 120,
      conversation_id: "convA",
    },
  };

  const fakeRow = () => ({
    querySelector: () => null,
    querySelectorAll: () => [],
  });

  const ctx = {
    console,
    setTimeout,
    clearTimeout,
    AbortController,
    FormData: FormDataStub,
    Promise,
    fetch: async () => {
      await fetchGate;
      return { ok: response.ok, status: response.status, statusText: "err", json: async () => response.body };
    },
  };
  ctx.window = ctx;
  ctx.window.AkanaI18n = { t: (k) => k, getLanguage: () => "en" };
  ctx.window.AkanaCore = {
    baseUrl: () => "http://test",
    authHeaders: () => ({}),
    authHeadersMultipart: () => ({}),
    escapeHtml: (s) => String(s ?? ""),
  };
  ctx.window.AkanaShell = { displayedConvId: () => displayed };
  ctx.window.AkanaChat = {
    conversationIdForMemory: () => "convA",
    chatActiveThread: () => activeThread,
    cancelActiveTurnOnServer: async () => {},
    attachmentsUploading: () => false,
    consumePendingFileIds: () => [],
    syncConversationLogFromServer: (id) => { rec.synced.push(id); },
  };
  ctx.window.AkanaMarkdown = { applyMarkdownToRow: () => {} };
  vm.createContext(ctx);
  vm.runInContext(read("akana-voice-pipeline.js"), ctx);

  const VPhase = { IDLE: "idle", PROCESSING: "processing", WAKE_ARMED: "wake_armed", CAPTURE_WAKE: "capture_wake" };
  const bridge = {
    VPhase,
    voice: { postInFlight: false, cancelled: false, voiceFetchAbort: null, conversationMode: false },
    session: {
      getEpoch: () => 1,
      transition: () => {},
      isWakeArmed: () => false,
      isCaptureWake: () => false,
      isCaptureMic: () => false,
    },
    voiceEpochMatches: () => true,
    hooks: {
      abortActiveChatStream: () => {},
      appendRow: (html) => { rec.rows.push(html); return fakeRow(); },
      chatRecordMessage: (m) => rec.recorded.push(m),
      setConversationId: (id) => rec.setConvIds.push(id),
      setOrb: () => {},
    },
    speechLang: () => "en-US",
    getTtsEnabled: () => false,
    ttsLangFromSpeech: () => "en",
    ttsPlayer: { playing: false, enqueue: async () => {}, reset: () => {} },
    msg: { value: "" },
    syncVoiceUi: () => {},
    maybeReArmConversation: () => {},
  };
  const pipeline = ctx.window.AkanaVoicePipeline.create(bridge);
  return {
    rec,
    bridge,
    pipeline,
    setResponse: (r) => { response = r; },
    switchTo: (id) => { displayed = id; },
    /** "+" from an unbound chat: a brand-new thread in the SAME (shared) new-chat pane, so
     *  displayedConvId() stays "" — the only thing that changes is the thread. */
    plusNewChat: (id) => { activeThread = { id }; },
    release: () => releaseFetch(),
  };
}

// A1 — the user switches chats while the wake reply is in flight.
{
  const rig = makeVoiceRig();
  rig.bridge.msg.value = "a draft the user is typing in chat B";
  const post = rig.pipeline.postVoiceBlob({ size: 4 });
  await tick();
  rig.switchTo("convB");        // user clicks chat B in the sidebar mid-flight
  rig.release();
  await post;

  check("voice: no phantom rows are painted into the chat the user switched to", () =>
    assert.equal(rig.rec.rows.length, 0,
      `appendRow writes into the DISPLAYED pane — got ${rig.rec.rows.length} row(s) in chat B`));
  check("voice: the '[voice] …' user record does not land in the other chat's thread", () =>
    assert.equal(rig.rec.recorded.length, 0,
      `chatRecordMessage writes to the ACTIVE thread — got ${JSON.stringify(rig.rec.recorded)}`));
  check("voice: the displayed chat is NOT rebound to the voice turn's conversation", () =>
    assert.equal(rig.rec.setConvIds.length, 0,
      "setConversationId rebound chat B's thread to convA — the next typed message would go to convA"));
  check("voice: the shared composer draft survives a mid-flight switch", () =>
    assert.equal(rig.bridge.msg.value, "a draft the user is typing in chat B"));
  check("voice: the turn is still persisted into ITS OWN conversation (server sync)", () =>
    assert.deepEqual(rig.rec.synced, ["convA"],
      "the conversation-targeted sync must stay unconditional or the turn is lost"));
}

// A2 — no switch: the normal single-shot wake path is untouched.
{
  const rig = makeVoiceRig();
  rig.bridge.msg.value = "leftover";
  const post = rig.pipeline.postVoiceBlob({ size: 4 });
  await tick();
  rig.release();
  await post;

  check("voice (control): the transcript + reply rows render when the chat is unchanged", () =>
    assert.equal(rig.rec.rows.length, 2, `expected transcript+assistant rows, got ${rig.rec.rows.length}`));
  check("voice (control): the user message is recorded locally", () =>
    assert.equal(rig.rec.recorded.length, 1));
  check("voice (control): setConversationId still binds the conversation", () =>
    assert.deepEqual(rig.rec.setConvIds, ["convA"]));
  check("voice (control): the composer is cleared", () =>
    assert.equal(rig.bridge.msg.value, ""));
}

// A3 — an ERROR reply must not paint into the other chat either.
{
  const rig = makeVoiceRig();
  rig.setResponse({ ok: false, status: 500, body: { detail: "engine down" } });
  const post = rig.pipeline.postVoiceBlob({ size: 4 });
  await tick();
  rig.switchTo("convB");
  rig.release();
  await post;

  check("voice: a voice ERROR bubble is not stranded in the chat switched to", () =>
    assert.equal(rig.rec.rows.length, 0,
      `error rows also go through the displayed pane — got ${JSON.stringify(rig.rec.rows)}`));
}

// A4 — the cross-pane gate keys on displayedConvId(), which is "" for EVERY unbound chat.
// The user wakes Akana on a brand-new chat and hits "+" while the POST is in flight: the
// conv id is "" on both sides, so an equality gate passes and the hijack it exists to stop
// lands anyway — on the chat the user just created.
{
  const rig = makeVoiceRig({ displayed: "", threadId: "th-1" });
  rig.setResponse({
    ok: true,
    status: 200,
    body: { transcript: "summarize my notes", text: "here are your notes", latency_ms: 120, conversation_id: "conv-new" },
  });
  rig.bridge.msg.value = "a draft the user is typing in the NEW chat";
  const post = rig.pipeline.postVoiceBlob({ size: 4 });
  await tick();
  rig.plusNewChat("th-2"); // "+" mid-flight — still unbound, still displayedConvId() === ""
  rig.release();
  await post;

  check("voice: an unbound surface is not matched by equality on \"\"", () =>
    assert.equal(rig.rec.rows.length, 0,
      `the voice turn painted into the chat created by "+" — got ${rig.rec.rows.length} row(s)`));
  check("voice: the '[voice] …' record does not land in the new chat's thread", () =>
    assert.equal(rig.rec.recorded.length, 0,
      `chatRecordMessage wrote into the new thread — got ${JSON.stringify(rig.rec.recorded)}`));
  check("voice: the new chat's thread is NOT rebound to the voice turn's conversation", () =>
    assert.equal(rig.rec.setConvIds.length, 0,
      "setConversationId bound the '+' chat to conv-new — its first typed message would be hijacked"));
  check("voice: the draft typed in the new chat survives", () =>
    assert.equal(rig.bridge.msg.value, "a draft the user is typing in the NEW chat"));
  check("voice: the turn is still persisted into its own conversation", () =>
    assert.deepEqual(rig.rec.synced, ["conv-new"]));
}

// A5 (control) — the SAME unbound surface with no "+" is the ordinary "wake on a fresh
// chat" flow, which is the most common wake path of all: it must be completely unaffected.
{
  const rig = makeVoiceRig({ displayed: "", threadId: "th-1" });
  rig.setResponse({
    ok: true,
    status: 200,
    body: { transcript: "summarize my notes", text: "here are your notes", latency_ms: 120, conversation_id: "conv-new" },
  });
  rig.bridge.msg.value = "leftover";
  const post = rig.pipeline.postVoiceBlob({ size: 4 });
  await tick();
  rig.release();
  await post;

  check("voice (control): waking on a brand-new chat still renders its rows", () =>
    assert.equal(rig.rec.rows.length, 2, `expected transcript+assistant rows, got ${rig.rec.rows.length}`));
  check("voice (control): the new chat is bound to the conversation the turn created", () =>
    assert.deepEqual(rig.rec.setConvIds, ["conv-new"]));
  check("voice (control): the composer is cleared on the unbound chat too", () =>
    assert.equal(rig.bridge.msg.value, ""));
}

/* ══════════════════════════════════════════════════════════════════════════════
   B. artifacts-singleton — the panel is dismissed on a conversation switch
   ══════════════════════════════════════════════════════════════════════════════ */

class ArtEl {
  constructor(tag = "div") {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.style = { setProperty() {}, removeProperty() {} };
    this.dataset = {};
    this.attrs = {};
    this.hidden = false;
    this.textContent = "";
    this.focusCount = 0;
    this._listeners = {};
    const s = new Set();
    this.classList = {
      add: (...c) => c.forEach((x) => s.add(x)),
      remove: (...c) => c.forEach((x) => s.delete(x)),
      toggle: (c, on) => { const w = on === undefined ? !s.has(c) : on; if (w) s.add(c); else s.delete(c); return w; },
      contains: (c) => s.has(c),
    };
  }
  set className(v) { String(v).split(/\s+/).filter(Boolean).forEach((c) => this.classList.add(c)); }
  get className() { return ""; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  removeAttribute(k) { delete this.attrs[k]; }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
  replaceChildren(...c) { this.children = c; }
  remove() {}
  focus() { this.focusCount += 1; }
  addEventListener(t, fn) { (this._listeners[t] ||= []).push(fn); }
  fire(t, ev) { for (const fn of this._listeners[t] || []) fn(ev); }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  closest() { return null; }
}

function makeArtifactsRig() {
  const panel = new ArtEl("section");
  const backdrop = new ArtEl("div");
  const preview = new ArtEl("div");
  const codeHost = new ArtEl("code");
  const title = new ArtEl("h2");
  const langTag = new ArtEl("span");
  const body = new ArtEl("body");
  panel.querySelector = (sel) => (sel === "#artifact-code code" ? codeHost : null);
  panel.querySelectorAll = () => [];

  const byId = {
    "artifact-panel": panel,
    "artifact-backdrop": backdrop,
    "artifact-preview": preview,
    "artifact-title": title,
    "artifact-lang": langTag,
    "artifact-close": null,
    "artifact-resize": null,
    "settings-artifact-autoopen": null,
  };
  const copied = [];
  const ctx = {
    console,
    setTimeout,
    clearTimeout,
    Blob: class { constructor(p) { this.parts = p; } },
    URL: { createObjectURL: () => "blob:x", revokeObjectURL: () => {} },
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    navigator: { clipboard: { writeText: async (t) => { copied.push(t); } } },
    matchMedia: () => ({ matches: false, addEventListener: () => {} }),
    addEventListener: () => {},
  };
  ctx.document = {
    readyState: "complete",
    body,
    documentElement: { style: { setProperty() {}, removeProperty() {} } },
    getElementById: (id) => (id in byId ? byId[id] : null),
    createElement: (t) => new ArtEl(t),
    addEventListener: () => {},
    // Only elements still attached to the live tree count (an LRU-evicted pane is not).
    contains: (n) => !!n && n._attached === true,
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(read("akana-artifacts.js"), ctx);
  return { A: ctx.window.AkanaArtifacts, panel, backdrop, preview, body, copied, ctx };
}

{
  const rig = makeArtifactsRig();
  const trigger = new ArtEl("button");
  trigger._attached = true;

  rig.A.open({ code: "<h1>chat A artifact</h1>", lang: "html", trigger });
  assert.equal(rig.body.classList.contains("artifacts-open"), true, "precondition: panel opened");
  assert.equal(rig.panel.getAttribute("aria-hidden"), "false", "precondition: panel visible");

  check("artifacts: a conversation switch dismiss is exported", () =>
    assert.equal(typeof rig.A.dismiss, "function",
      "AkanaArtifacts.dismiss is missing — nothing can close the singleton panel on a chat switch"));

  rig.A.dismiss();

  check("artifacts: the panel is closed when the user opens another chat", () => {
    assert.equal(rig.body.classList.contains("artifacts-open"), false,
      "chat A's artifact panel stayed open over chat B");
    assert.equal(rig.panel.getAttribute("aria-hidden"), "true");
    assert.equal(rig.backdrop.getAttribute("aria-hidden"), "true");
  });
  check("artifacts: the sandboxed iframe is torn down (scripts/timers stop)", () =>
    assert.equal(rig.preview.children.length, 0));
  check("artifacts: the switch dismiss does NOT yank focus back into the chat being left", () =>
    assert.equal(trigger.focusCount, 0,
      "focus was restored to chat A's Preview button after the user opened chat B"));

  // `current` must be dropped: the panel's own Copy action would otherwise still serve the
  // previous chat's code (and keep it alive) after the panel is gone.
  rig.panel.fire("click", { target: { closest: () => ({ dataset: { artifactAct: "copy" } }) } });
  await tick();
  check("artifacts: the dismissed panel no longer holds the previous chat's code", () =>
    assert.deepEqual(rig.copied, [],
      `stale artifact state survived the dismiss: ${JSON.stringify(rig.copied)}`));

  // Idempotent + still reusable for the next chat.
  rig.A.dismiss();
  rig.A.open({ code: "<h1>chat B artifact</h1>", lang: "html" });
  check("artifacts: the panel still opens normally in the next chat", () =>
    assert.equal(rig.body.classList.contains("artifacts-open"), true));
}

{
  // The a11y contract for the USER'S OWN close is unchanged: focus returns to the trigger.
  const rig = makeArtifactsRig();
  const trigger = new ArtEl("button");
  trigger._attached = true;
  rig.A.open({ code: "<h1>x</h1>", lang: "html", trigger });
  rig.A.close();
  check("artifacts: an explicit close still returns focus to the trigger (a11y unchanged)", () =>
    assert.equal(trigger.focusCount, 1));
}

/* ══════════════════════════════════════════════════════════════════════════════
   C. the shell's conversation-switch dismiss site actually calls it
   ══════════════════════════════════════════════════════════════════════════════ */

class ShellEl {
  constructor(tag = "div") {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.style = {};
    this.attrs = {};
    this.dataset = {};
    this.hidden = false;
    this._listeners = {};
    this._html = "";
    this._scrollTop = 0;
    this.contentHeight = 0;
    this.clientHeight = 500;
    const s = new Set();
    this.classList = {
      add: (...c) => c.forEach((x) => s.add(x)),
      remove: (...c) => c.forEach((x) => s.delete(x)),
      toggle: (c, on) => { const w = on === undefined ? !s.has(c) : on; if (w) s.add(c); else s.delete(c); return w; },
      contains: (c) => s.has(c),
    };
  }
  get scrollHeight() { return Math.max(this.contentHeight, this.clientHeight); }
  get scrollTop() { return this._scrollTop; }
  set scrollTop(v) {
    const max = Math.max(0, this.scrollHeight - this.clientHeight);
    this._scrollTop = Math.min(Math.max(0, Number(v) || 0), max);
  }
  get innerHTML() { return this._html; }
  set innerHTML(v) { this._html = String(v); if (v === "") this.children = []; }
  set className(v) { String(v).split(/\s+/).filter(Boolean).forEach((c) => this.classList.add(c)); }
  get className() { return ""; }
  get parentElement() { return this.parentNode; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  removeAttribute(k) { delete this.attrs[k]; }
  appendChild(c) { this.children.push(c); c.parentNode = this; return c; }
  remove() { const p = this.parentNode; if (p) p.children = p.children.filter((c) => c !== this); this.parentNode = null; }
  addEventListener(t, fn) { (this._listeners[t] ||= []).push(fn); }
  fire(type, ev) { for (const fn of this._listeners[type] || []) fn(ev); }
  getBoundingClientRect() { return { top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 }; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  contains() { return false; }
}

{
  const logEl = new ShellEl("div");
  const scroller = new ShellEl("div");
  const byId = { log: logEl, "log-scroll": scroller };
  let rafQueue = [];
  const flush = () => {
    for (let i = 0; i < 8 && rafQueue.length; i++) {
      const q = rafQueue;
      rafQueue = [];
      q.forEach((fn) => fn());
    }
  };
  const doc = {
    getElementById: (id) => byId[id] || null,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: (t) => new ShellEl(t),
    addEventListener: () => {},
  };
  const ctx = {
    window: {
      AkanaCore: { baseUrl: () => "http://x", authHeaders: () => ({}), escapeHtml: (s) => String(s ?? "") },
      AkanaI18n: { t: (k) => k, getLanguage: () => "en" },
      addEventListener: () => {},
    },
    document: doc,
    console,
    setTimeout,
    clearTimeout,
    AbortController,
    Element: ShellEl,
    requestAnimationFrame: (fn) => { rafQueue.push(fn); return rafQueue.length; },
    cancelAnimationFrame: () => {},
    fetch: async () => ({ ok: true, json: async () => ({ items: [] }) }),
  };
  ctx.window.window = ctx.window;
  ctx.window.document = doc;
  vm.runInNewContext(read("akana-chat-panes.js"), ctx);
  vm.runInNewContext(read("akana-shell.js"), ctx);

  const Shell = ctx.window.AkanaShell;
  Shell.init({ log: logEl, logScroll: scroller, logEmpty: null, msg: null, form: null, orb: null,
    escapeHtml: (s) => String(s ?? "") });

  let dismissed = 0;
  ctx.window.AkanaArtifacts = { dismiss: () => { dismissed += 1; } };

  Shell.showConversation("convA");
  flush();
  const afterFirst = dismissed;
  Shell.showConversation("convB");
  flush();

  check("shell: opening another chat dismisses the artifact panel (same site as the action bar)", () =>
    assert.ok(dismissed > afterFirst,
      "showConversation never told AkanaArtifacts to dismiss — chat A's panel survives the switch"));

  const stable = dismissed;
  Shell.showConversation("convB"); // same conversation → not a switch
  flush();
  check("shell: re-showing the SAME chat does not dismiss the panel", () =>
    assert.equal(dismissed, stable));
}

console.log(`hunt5_artifacts_voice.harness: ${passed} artifacts/voice live-state contracts PASSED ✓`);
if (typeof process !== "undefined" && process.exit) process.exit(0);
