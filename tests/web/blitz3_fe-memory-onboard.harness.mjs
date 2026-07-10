/**
 * Blitz-3 fe-memory-onboard regression harness (node-vm + fake DOM, backend-free).
 *
 * Locks in six verified fixes:
 *  1. deleteFact / saveNewFact / gotoFactsPage reload the facts list with {force:true}
 *     so an open dirty editor no longer makes these explicit actions silently skip the
 *     reload (and gotoFactsPage's offset no longer drifts away from what is displayed).
 *  2. The onboarding voice pane's optimistic first paint must NOT clobber the saved
 *     `akana.wakeAutostart` flag before the /voice/wake/config probe resolves.
 *  3. AkanaMemoryApi.listStaging sends limit (server cap 500), so the Inbox + bulk
 *     approve/reject are not silently capped at the server default of 50.
 *  4. The Inbox empty-state action button is localized (follows the active language),
 *     not stuck on the hardcoded English label.
 *  6. saveSettings OMITS an empty ollama_url/embed_model (auto_capture "keep" convention)
 *     instead of sending a "" the server silently drops.
 *
 * (Finding 5 — the <title> tag — is a static-HTML change asserted in the pytest wrapper.)
 *
 * Run: node tests/web/blitz3_fe-memory-onboard.harness.mjs
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

// ───────────────────────── Fake DOM ────────────────────────────────────────
function makeEl(tag = "div") {
  const el = {
    tagName: String(tag).toUpperCase(),
    children: [],
    dataset: {},
    attrs: {},
    _listeners: {},
    value: "",
    checked: false,
    hidden: false,
    disabled: false,
    type: "",
    _text: "",
    _html: "",
    classList: {
      _s: new Set(),
      add(...c) { c.forEach((x) => this._s.add(x)); },
      remove(...c) { c.forEach((x) => this._s.delete(x)); },
      contains(c) { return this._s.has(c); },
      toggle(c, force) {
        const want = force === undefined ? !this._s.has(c) : !!force;
        if (want) this._s.add(c); else this._s.delete(c);
        return want;
      },
    },
    style: {},
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); this.children = []; },
    get innerHTML() { return this._html; },
    // Assigning innerHTML REPLACES the element's children (real DOM semantics). The
    // markup strings here carry no elements we track, so resetting to [] is faithful.
    set innerHTML(v) { this._html = String(v); this.children = []; },
    get className() { return [...this.classList._s].join(" "); },
    set className(v) { this.classList._s = new Set(String(v).split(/\s+/).filter(Boolean)); },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k] ?? null; },
    removeAttribute(k) { delete this.attrs[k]; },
    appendChild(c) { this.children.push(c); c.parentNode = this; c.parentElement = this; return c; },
    append(...cs) { cs.forEach((c) => (c && typeof c === "object" ? this.appendChild(c) : null)); },
    removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); return c; },
    remove() { const p = this.parentNode; if (p) p.removeChild(this); },
    addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); },
    closest() { return null; },
    scrollIntoView() {},
    scrollTo() {},
    focus() {},
    querySelector(sel) { return findAll(this, sel)[0] || null; },
    querySelectorAll(sel) { return findAll(this, sel); },
  };
  return el;
}

function matchesSimple(el, sel) {
  const tokens = sel.split(/(?=\.)/).filter((s) => s !== "");
  for (const tok of tokens) {
    if (tok.startsWith(".")) { if (!el.classList.contains(tok.slice(1))) return false; }
    else if (el.tagName !== tok.toUpperCase()) return false;
  }
  return true;
}
function descendants(el, out = []) {
  for (const c of el.children || []) { if (!c || !c.tagName) continue; out.push(c); descendants(c, out); }
  return out;
}
function findAll(root, sel) {
  const parts = sel.trim().split(/\s+/);
  let scope = [root];
  for (const part of parts) {
    const next = [];
    for (const node of scope) for (const d of descendants(node)) if (matchesSimple(d, part)) next.push(d);
    scope = next;
  }
  return scope;
}

const enT = makeI18nStub("en").t;
const trT = makeI18nStub("tr").t;

let passed = 0;
async function check(label, fn) { await fn(); passed += 1; void label; }
const tick = () => new Promise((r) => setTimeout(r, 10));

// ═══ Section A: Memory Studio (findings 1, 4, 6) ════════════════════════════
{
  const registry = new Map();
  const document = {
    createElement: (tag) => makeEl(tag),
    createElementNS: (_ns, tag) => makeEl(tag),
    createTextNode: (text) => ({ nodeType: 3, textContent: String(text) }),
    getElementById: (id) => {
      if (!registry.has(id)) registry.set(id, makeEl(id.includes("list") ? "ul" : "div"));
      return registry.get(id);
    },
    querySelectorAll: () => [],
  };
  document.body = makeEl("body"); // classList.contains("memory-studio-page") → false

  const win = {
    AkanaI18n: { t: enT },
    AkanaCore: { showToast: () => {}, escapeHtml: (s) => String(s ?? "") },
    sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    requestAnimationFrame: () => 0,
    setTimeout: (fn) => (fn && fn(), 0),
    clearTimeout: () => {},
    confirm: () => true,
  };
  win.window = win;

  const ctx = { window: win, document, sessionStorage: win.sessionStorage, console };
  vm.createContext(ctx);
  vm.runInContext(read("akana-memory-render.js"), ctx);
  vm.runInContext(read("akana-memory-studio.js"), ctx);

  const studio = win.AkanaMemoryStudio;
  assert.ok(studio && studio._test, "studio + _test seam must load");
  const S = studio._test;
  for (const fn of ["loadFacts", "openFactEditor", "deleteFact", "saveNewFact", "gotoFactsPage", "loadInbox", "saveSettings"]) {
    assert.equal(typeof S[fn], "function", `_test.${fn} must be exposed`);
  }

  const FACTS = [
    { id: "f1", key: "coffee", value: "likes espresso" },
    { id: "f2", key: "city", value: "Osmaniye" },
  ];
  let listFactsCalls = 0;
  let lastOffset = null;
  const apiStub = {
    listFacts: async (filters) => { listFactsCalls += 1; lastOffset = filters.offset; return { items: FACTS, total: 400, offset: filters.offset, limit: filters.limit }; },
    deleteFact: async () => ({}),
    createFact: async () => ({ id: "new" }),
    getStats: async () => ({ staging_pending: 0 }),
    listStaging: async () => [],
    putSettings: async () => ({}),
  };
  win.AkanaMemoryApi = apiStub;

  const list = document.getElementById("memory-facts-list");
  const openDirtyEditorOnFirstCard = () => {
    const card = list.querySelectorAll(".memory-fact-card")[0];
    S.openFactEditor(card, FACTS[0]);
    const ta = card.querySelector(".memory-fact-editor textarea");
    ta.value = "an unsaved rewrite the user is mid-typing";
    assert.equal(studio._test.hasDirtyFactEditor(), true, "editor must be dirty");
  };

  // ── Finding 1: deleteFact forces the reload past the dirty-editor guard ──────
  await check("deleteFact forces reload even with a dirty editor open", async () => {
    await S.loadFacts({ force: true });
    openDirtyEditorOnFirstCard();
    const before = listFactsCalls;
    await S.deleteFact(FACTS[0]);
    assert.ok(listFactsCalls > before, "delete must force the reload (bug: unforced → skipped)");
    assert.equal(list.querySelectorAll(".memory-fact-editor textarea").length, 0,
      "the deleted fact's stale editor must be gone after the forced reload");
  });

  // ── Finding 1: saveNewFact forces the reload ────────────────────────────────
  await check("saveNewFact forces reload even with a dirty editor open", async () => {
    await S.loadFacts({ force: true });
    document.getElementById("memory-fact-value").value = "a brand new fact";
    openDirtyEditorOnFirstCard();
    const before = listFactsCalls;
    await S.saveNewFact();
    assert.ok(listFactsCalls > before, "save-new must force the reload so the new fact appears");
  });

  // ── Finding 1: gotoFactsPage forces reload AND applies the new offset ────────
  await check("gotoFactsPage forces reload and applies the mutated offset", async () => {
    await S.loadFacts({ force: true });
    assert.equal(lastOffset, 0, "start at offset 0");
    openDirtyEditorOnFirstCard();
    const before = listFactsCalls;
    S.gotoFactsPage(1); // Next page → offset +50
    await tick();
    assert.ok(listFactsCalls > before, "paging must force the reload (bug: offset drifts, no reload)");
    assert.equal(lastOffset, 50, "the mutated offset must actually reach the server (no drift)");
  });

  // ── Finding 4: Inbox empty-state action button follows the active language ──
  await check("Inbox empty-state action label is localized (TR)", async () => {
    win.AkanaI18n = { t: trT };
    const inbox = document.getElementById("memory-inbox-list");
    inbox.dataset.emptyActionHref = "/memory?view=settings";
    inbox.dataset.emptyActionLabel = "Memory settings"; // hardcoded EN from the HTML attr
    inbox.dataset.i18nEmptyActionLabel = "memory.inbox_empty_action";
    await S.loadInbox();
    assert.equal(inbox.dataset.emptyActionLabel, "Hatırlama ayarları",
      "the action label must be resolved from the i18n key, not left as hardcoded English");
    const anchor = inbox.querySelector("a.memory-empty-action");
    assert.ok(anchor, "the empty-state action anchor must render");
    assert.equal(anchor.textContent, "Hatırlama ayarları", "the rendered button text is localized");
    win.AkanaI18n = { t: enT };
  });

  // ── Finding 6: saveSettings omits empty ollama_url/embed_model ───────────────
  await check("saveSettings omits cleared ollama_url/embed_model (keep convention)", async () => {
    let body = null;
    win.AkanaMemoryApi = { ...apiStub, putSettings: async (b) => { body = b; return {}; } };
    document.getElementById("memory-vector-mode").value = "auto";
    document.getElementById("memory-embed-backend").value = "local";
    // Cleared fields:
    document.getElementById("memory-ollama-url").value = "";
    document.getElementById("memory-embed-model").value = "";
    await S.saveSettings();
    assert.ok(!("ollama_url" in body), "an empty ollama_url must be OMITTED, not sent as ''");
    assert.ok(!("embed_model" in body), "an empty embed_model must be OMITTED, not sent as ''");
    // Non-empty fields are still sent:
    document.getElementById("memory-ollama-url").value = "http://127.0.0.1:11434";
    document.getElementById("memory-embed-model").value = "bge-m3";
    await S.saveSettings();
    assert.equal(body.ollama_url, "http://127.0.0.1:11434", "a non-empty ollama_url is sent");
    assert.equal(body.embed_model, "bge-m3", "a non-empty embed_model is sent");
    win.AkanaMemoryApi = apiStub;
  });
}

// ═══ Section B: API client listStaging limit (finding 3) ════════════════════
{
  let lastUrl = null;
  const ctx = {
    console,
    URL, URLSearchParams,
    fetch: async (url) => { lastUrl = url; return { ok: true, status: 200, json: async () => [] }; },
    window: {
      AkanaCore: { baseUrl: () => "http://h", authHeaders: () => ({}), parseApiError: (_b, s) => "HTTP " + s },
    },
  };
  ctx.window.window = ctx.window;
  vm.createContext(ctx);
  vm.runInContext(read("akana-memory-api.js"), ctx);
  const memApi = ctx.window.AkanaMemoryApi;

  await check("listStaging sends the server-cap limit (not the default 50)", async () => {
    await memApi.listStaging("pending");
    assert.ok(/[?&]status=pending(&|$)/.test(lastUrl), "status is sent");
    assert.ok(/[?&]limit=500(&|$)/.test(lastUrl),
      "listStaging must pass limit=500 so the Inbox + bulk actions aren't capped at 50");
  });

  await check("listStaging honors an explicit limit override", async () => {
    await memApi.listStaging("pending", 120);
    assert.ok(/[?&]limit=120(&|$)/.test(lastUrl), "an explicit limit is passed through");
  });
}

// ═══ Section C: Onboarding voice pane wake-flag guard (finding 2) ════════════
{
  const store = new Map([["akana.wakeAutostart", "1"], ["akana.onboarded", "1"]]);
  const localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  };
  const document = {
    readyState: "complete",
    getElementById: () => null,
    createElement: (tag) => makeEl(tag),
    addEventListener: () => {},
    removeEventListener: () => {},
    documentElement: { dataset: {} },
    body: makeEl("body"),
  };
  const win = {
    document, localStorage, setTimeout, clearTimeout, console,
    // Firefox/Safari: NO SpeechRecognition → speechSupported=false → wake hinges on the probe.
    fetch: async () => ({ ok: true, json: async () => ({ enabled: true }) }),
    addEventListener: () => {},
    removeEventListener: () => {},
    AkanaI18n: { t: (k) => k, ready: Promise.resolve("en") },
    AkanaCore: { baseUrl: () => "", authHeaders: () => ({}) },
  };
  win.window = win;
  // Delegate the global fetch to win.fetch so a per-test reassignment takes effect
  // (aurora-onboard calls the bare `fetch`, i.e. the context global, not window.fetch).
  const ctx = { window: win, document, localStorage, setTimeout, clearTimeout, console, fetch: (...a) => win.fetch(...a) };
  vm.createContext(ctx);
  vm.runInContext(read("aurora-onboard.js"), ctx);

  const onb = win.auroraOnboard;
  assert.ok(onb && typeof onb._renderVoiceForTest === "function", "_renderVoiceForTest must be exposed");

  await check("optimistic first paint does NOT clobber the saved wake flag on Firefox", async () => {
    assert.equal(store.get("akana.wakeAutostart"), "1", "precondition: user had autostart enabled");
    const body = makeEl("div");
    onb._renderVoiceForTest(body); // synchronous first paint (serverWakeEnabled = null)
    assert.equal(store.get("akana.wakeAutostart"), "1",
      "the saved autostart flag must survive the pre-probe paint (bug: setWake(false) clobbered it)");

    // Probe resolves enabled=true (bundled server wake model) → repaint usable + ON.
    await tick();
    const toggle = body.querySelector(".aur-onb-toggle");
    assert.ok(toggle, "the wake toggle must render after the probe");
    assert.ok(!toggle.classList.contains("disabled"), "wake is usable once the server model is confirmed");
    assert.ok(toggle.classList.contains("on"), "the preserved preference must show the toggle ON after the probe");
    assert.equal(toggle.getAttribute("aria-checked"), "true", "aria-checked reflects the preserved ON state");
    assert.equal(store.get("akana.wakeAutostart"), "1", "the flag is still enabled end-to-end");
  });

  await check("a genuinely unusable browser DOES clear the flag once the probe resolves", async () => {
    store.set("akana.wakeAutostart", "1");
    win.fetch = async () => ({ ok: true, json: async () => ({ enabled: false }) });
    const body = makeEl("div");
    onb._renderVoiceForTest(body);
    // First paint: still preserved (probe not resolved).
    assert.equal(store.get("akana.wakeAutostart"), "1", "not cleared before the probe resolves");
    await tick();
    // Probe says disabled AND no SpeechRecognition → wake truly out of reach → clear.
    assert.equal(store.get("akana.wakeAutostart"), "0",
      "once confirmed unusable, the flag is cleared so no dead wake listener boots");
    const toggle = body.querySelector(".aur-onb-toggle");
    assert.ok(toggle.classList.contains("disabled"), "the toggle is disabled on an unusable browser");
  });
}

// ═══ Section D: memory.html browser-tab <title> keeps the app name (finding 5) ═══
// The <title> shared memory.page_title with the h1, so the i18n engine rewrote the tab
// to a bare "Memory" (the h1's value), dropping "— Akana". A dedicated key restores it.
{
  await check("the browser <title> uses a dedicated i18n key that keeps '— Akana'", async () => {
    // The i18n stub loads the REAL strings table, so this resolves like the browser.
    assert.ok(enT("memory.page_title_tab").includes("Akana"), "EN tab title must keep the app name");
    assert.ok(enT("memory.page_title_tab").includes("Memory"), "EN tab title still reads Memory");
    assert.ok(trT("memory.page_title_tab").includes("Akana"), "TR tab title must keep the app name");
    assert.notEqual(
      enT("memory.page_title_tab"), enT("memory.page_title"),
      "the tab <title> key must DIFFER from the in-page h1 key (a bare 'Memory')",
    );
    // memory.html wires the <title> to the dedicated tab key and the h1 to the short key.
    const html = readFileSync(path.join(REPO, "web_ui/memory.html"), "utf8");
    assert.match(html, /<title data-i18n="memory\.page_title_tab">/, "the <title> must use data-i18n=memory.page_title_tab");
    assert.match(html, /<h1 data-i18n="memory\.page_title">/, "the h1 must keep the short memory.page_title");
  });
}

console.log(`blitz3_fe-memory-onboard.harness: ok (${passed} checks)`);
if (typeof process !== "undefined" && process.exit) process.exit(0);
