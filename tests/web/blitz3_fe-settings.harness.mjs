/**
 * blitz3 fe-settings contract harness — backend-free, node-vm + fake-DOM.
 *
 * Six verified fixes in the settings-area static JS, each an isolated section that
 * loads the REAL module from web_ui/static in a bare VM with a minimal DOM and
 * asserts a behavior contract (RED on the pre-fix code, GREEN after the fix):
 *
 *  1. akana-pair.js  — editing the host must recompose the QR from the server-issued
 *     pair_url token (loopback owner has NO localStorage token), not wipe it.
 *  2. akana-pair.js  — token set server-side but no pair_url (Tailscale Serve off) must
 *     name the real missing piece, not the misleading "Set a token first" toast.
 *  3. akana-settings.js — visibilitychange-hidden must flush WITHOUT force-reconnecting
 *     the healthy /ws/events socket; the input-driven path still reconnects.
 *  4. akana-vault.js  — load() must coalesce a reload requested while one is in flight
 *     (post-mutation truth), not silently drop it.
 *  5. akana-settings.js — runtime source badges must re-resolve through t() per render
 *     so a live language flip retones them (not a stale module-eval constant).
 *  6. akana-personas.js — Fork name suffix must come from i18n (EN "(copy)"), not a
 *     hardcoded Turkish "(kopya)".
 *
 * Each section is self-contained (own VM realm) so state never bleeds between them.
 * Run: node tests/web/blitz3_fe-settings.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { makeI18nStub } from "./_i18n_stub.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const readStatic = (rel) => readFileSync(path.join(REPO, "web_ui/static", rel), "utf8");
const tick = () => new Promise((r) => setImmediate(r));

// ───────────────────────── shared fake DOM ─────────────────────────
function matchesSimple(el, sel) {
  const tokens = sel.split(/(?=\.)/).filter((s) => s !== "");
  for (const tok of tokens) {
    if (tok.startsWith(".")) { if (!el.classList.contains(tok.slice(1))) return false; }
    else if (String(el.tagName) !== tok.toUpperCase()) return false;
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
function makeEl(tag = "div") {
  const el = {
    tagName: String(tag).toUpperCase(),
    id: "",
    children: [],
    parentNode: null,
    dataset: {},
    attrs: {},
    _listeners: {},
    _qr: null,
    value: "",
    checked: false,
    hidden: false,
    disabled: false,
    title: "",
    _text: "",
    _html: "",
    classList: {
      _s: new Set(),
      add(...c) { c.forEach((x) => this._s.add(x)); },
      remove(...c) { c.forEach((x) => this._s.delete(x)); },
      contains(c) { return this._s.has(c); },
      toggle(c, on) { const has = this._s.has(c); const want = on === undefined ? !has : !!on; if (want) this._s.add(c); else this._s.delete(c); return want; },
    },
    style: {},
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); this.children = []; },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); if (v === "") this.children = []; },
    get className() { return [...this.classList._s].join(" "); },
    set className(v) { this.classList._s = new Set(String(v).split(/\s+/).filter(Boolean)); },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k] ?? null; },
    removeAttribute(k) { delete this.attrs[k]; },
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    append(...cs) { cs.forEach((c) => (c && typeof c === "object" ? this.appendChild(c) : null)); },
    removeChild(c) { const i = this.children.indexOf(c); if (i >= 0) this.children.splice(i, 1); return c; },
    remove() { const p = this.parentNode; if (p) p.removeChild(this); },
    addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); },
    dispatch(type, ev) { (this._listeners[type] || []).forEach((fn) => fn(ev)); },
    focus() {},
    blur() {},
    scrollIntoView() {},
    querySelector(sel) { return findAll(this, sel)[0] || null; },
    querySelectorAll(sel) { return findAll(this, sel); },
    contains(node) { return node === this || descendants(this).includes(node); },
    closest(sel) {
      let n = this;
      const isAttrDataAction = sel === "[data-action]";
      while (n) {
        if (isAttrDataAction) { if (n.dataset && n.dataset.action != null) return n; }
        else if (n.tagName && matchesSimple(n, sel)) return n;
        n = n.parentNode;
      }
      return null;
    },
  };
  return el;
}
function makeDoc({ studioBody = false } = {}) {
  const registry = new Map();
  const body = makeEl("body");
  if (studioBody) body.classList.add("memory-studio-page");
  const doc = {
    _listeners: {},
    readyState: "complete",
    visibilityState: "visible",
    body,
    head: makeEl("head"),
    documentElement: makeEl("html"),
    createElement: (tag) => makeEl(tag),
    createElementNS: (_ns, tag) => makeEl(tag),
    createTextNode: (text) => ({ nodeType: 3, textContent: String(text) }),
    getElementById: (id) => registry.get(id) || null,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener(type, fn) { (this._listeners[type] ||= []).push(fn); },
    dispatch(type, ev) { (this._listeners[type] || []).forEach((fn) => fn(ev)); },
  };
  doc.documentElement.dataset = {};
  const register = (id, el) => { const e = el || makeEl("div"); e.id = id; registry.set(id, e); return e; };
  return { doc, registry, register, body };
}
function makeLocalStorage(seed = {}) {
  const store = { ...seed };
  return {
    _store: store,
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
    clear: () => { for (const k of Object.keys(store)) delete store[k]; },
  };
}
/** Mutable-language i18n stub: t() re-resolves at call time so a live flip retones. */
function makeMutI18n(initial = "en") {
  const DICT = makeI18nStub().DICT;
  let lang = initial;
  return {
    DICT,
    setLang: (l) => { lang = l; },
    api: {
      DICT,
      getLanguage: () => lang,
      t: (k, p) => {
        const e = DICT[k];
        let s = e ? (e[lang] ?? e.en ?? k) : k;
        if (p) for (const kk in p) s = s.split(`{${kk}}`).join(String(p[kk]));
        return s;
      },
    },
  };
}

// ───────────────────────── runner ─────────────────────────
const results = [];
async function section(id, fn) {
  try { await fn(); results.push({ id, ok: true }); console.log(`  [PASS] ${id}`); }
  catch (e) { results.push({ id, ok: false, err: e }); console.log(`  [FAIL] ${id}: ${e && e.message}`); }
}

// ═══════════════ fe-settings-1: pair host edit recomposes QR ═══════════════
await section("fe-settings-1 pair host-edit recomposes QR (no wipe)", async () => {
  const { doc, register, body } = makeDoc();
  register("pair-backdrop");
  register("pair-host-confirm", makeEl("input"));
  register("pair-qr");
  register("pair-modal-foot");

  const qrTexts = [];
  // The real vendored lib appends a canvas/img into the host → container is non-empty.
  function QRCode(host, opts) { qrTexts.push(opts.text); host._qr = opts.text; host.innerHTML = "<canvas data-qr></canvas>"; }
  QRCode.CorrectLevel = { M: 0 };

  const PAIR_URL = "https://self.tailnet.ts.net/#token=ABC123enc";
  const win = {
    QRCode,
    AkanaCore: { LS_TOKEN: "akana.apiToken", showToast: () => {}, baseUrl: () => "http://127.0.0.1:8766", authHeaders: () => ({}) },
    AkanaI18n: makeI18nStub(),
  };
  win.window = win;
  const localStorage = makeLocalStorage({ "akana.apiToken": "" }); // loopback owner: NO localStorage token
  const fetch = async () => ({ ok: true, json: async () => ({ pair_url: PAIR_URL, self_dns_name: "self.tailnet.ts.net", token_set: true, https_url: "https://self.tailnet.ts.net" }) });
  const ctx = { window: win, document: doc, localStorage, fetch, navigator: {}, console, location: { host: "127.0.0.1:8766" } };
  ctx.document.defaultView = win;
  vm.runInNewContext(readStatic("akana-pair.js"), ctx);

  await win.AkanaPair.openPairModal();
  assert.equal(qrTexts.length, 1, "server-first path renders the QR from pair_url");
  assert.equal(qrTexts[0], PAIR_URL);

  // User corrects the host (phone can't resolve the MagicDNS name → Tailscale IP).
  const hostInput = doc.getElementById("pair-host-confirm");
  hostInput.value = "100.101.102.103";
  doc.dispatch("change", { target: hostInput });

  // The QR must be RECOMPOSED onto the corrected host with the SAME server token —
  // NOT wiped (HEAD's buildPairUrl reads only the empty localStorage → null → wipe).
  assert.equal(qrTexts.length, 2, "host edit must rebuild the QR, not drop it");
  assert.equal(qrTexts[1], "https://100.101.102.103/#token=ABC123enc",
    "recomposed URL = corrected host + verbatim server token");
  assert.notEqual(doc.getElementById("pair-qr").innerHTML, "", "the QR container must not be emptied");
});

// ═══════════ fe-settings-2: pair 'serve inactive' vs 'no token' toast ═══════════
await section("fe-settings-2 pair serve-inactive names the real missing piece", async () => {
  const { doc, register } = makeDoc();
  const toasts = [];
  const i18n = makeI18nStub();
  const win = {
    AkanaCore: { LS_TOKEN: "akana.apiToken", showToast: (m, k) => toasts.push({ m: String(m), k }), baseUrl: () => "http://127.0.0.1:8766", authHeaders: () => ({}) },
    AkanaI18n: i18n,
  };
  win.window = win;
  const localStorage = makeLocalStorage({ "akana.apiToken": "" }); // loopback owner: no localStorage token
  // token IS set server-side but Tailscale Serve is off → https_url/pair_url null.
  const fetch = async () => ({ ok: true, json: async () => ({ token_set: true, https_url: null, pair_url: null, serve_active: false }) });
  const ctx = { window: win, document: doc, localStorage, fetch, navigator: {}, console, location: { host: "127.0.0.1:8766" } };
  vm.runInNewContext(readStatic("akana-pair.js"), ctx);

  await win.AkanaPair.openPairModal();
  assert.equal(toasts.length, 1, "one toast on the token-set/serve-off dead-end");
  const msg = toasts[0].m;
  assert.equal(msg, i18n.t("pair.toast.serve_inactive"),
    "must name Tailscale Serve as the missing piece");
  assert.notEqual(msg, i18n.t("pair.toast.no_token"),
    "must NOT show the misleading 'Set a token first' (the token IS set)");
});

// ═══════ fe-settings-3: visibilitychange must not force a WS reconnect ═══════
await section("fe-settings-3 tab-hide flush does not tear down the WS", async () => {
  const { doc, register, body } = makeDoc({ studioBody: true }); // skip the heavy chrome block
  const timers = [];
  class FakeWS {
    constructor(url) { this.url = url; this.readyState = FakeWS.CONNECTING; FakeWS.instances.push(this); }
    close() { this.readyState = FakeWS.CLOSED; this._closed = true; }
  }
  FakeWS.CONNECTING = 0; FakeWS.OPEN = 1; FakeWS.CLOSING = 2; FakeWS.CLOSED = 3;
  FakeWS.instances = [];

  const win = {
    AkanaCore: { LS_BASE: "akana.baseUrl", LS_TOKEN: "akana.token", showToast: () => {}, escapeHtml: (s) => String(s), baseUrl: () => "http://127.0.0.1:8766", authHeaders: () => ({}), parseApiError: () => "", configure: () => {} },
    AkanaBus: { emit: () => {} },
    AkanaI18n: makeI18nStub(),
    AkanaVoice: { persistVoiceSettings: async () => {} },
    location: { origin: "http://127.0.0.1:8766" },
    matchMedia: () => ({ matches: false, addEventListener: () => {} }),
    addEventListener: () => {},
    setTimeout: (fn, ms) => { timers.push(fn); return timers.length; },
    clearTimeout: () => {},
  };
  win.window = win;
  const localStorage = makeLocalStorage({ "akana.baseUrl": "http://127.0.0.1:8766", "akana.token": "" });
  const ctx = {
    window: win, document: doc, navigator: {}, console, location: win.location,
    localStorage, WebSocket: FakeWS, URL,
    setTimeout: win.setTimeout, clearTimeout: win.clearTimeout,
    fetch: async () => ({ ok: true, json: async () => ({}) }),
  };
  vm.runInNewContext(readStatic("akana-settings.js"), ctx);
  const settings = win.AkanaSettings;
  assert.ok(settings, "AkanaSettings must load");

  const baseUrlInput = makeEl("input"); baseUrlInput.value = "http://127.0.0.1:8766";
  const tokenInput = makeEl("input"); tokenInput.value = "";
  settings.init({ baseUrlInput, tokenInput });

  const flushTimers = () => { const batch = timers.splice(0); batch.forEach((fn) => fn()); };

  // Tab hide (screen-off): the visibilitychange handler must flush WITHOUT forcing a
  // reconnect. HEAD → persistAllSettings → persistAndReconnect → connectWs(true) opens
  // a socket; the fix persists locally only.
  const wsBefore = FakeWS.instances.length;
  doc.visibilityState = "hidden";
  doc.dispatch("visibilitychange", {});
  await tick();
  flushTimers();
  await tick();
  assert.equal(FakeWS.instances.length, wsBefore,
    "tab-hide must NOT create/force a WS reconnect (healthy socket survives backgrounding)");

  // Regression guard: the input-driven reconnect path is preserved (only meaningful
  // once the fix splits the persist paths, so guard on the seam).
  if (typeof settings._persistAndReconnect === "function") {
    const n = FakeWS.instances.length;
    settings._persistAndReconnect();
    flushTimers();
    await tick();
    assert.ok(FakeWS.instances.length > n, "an explicit base-url/token change must still reconnect");
  }
});

// ═══════════ fe-settings-4: vault load() coalesces in-flight reload ═══════════
await section("fe-settings-4 vault load() coalesces a post-mutation reload", async () => {
  const { doc, register } = makeDoc();
  register("vault-root");
  // pane intentionally unregistered → init() returns early (no auto-load / observer).

  let calls = 0;
  const pending = [];
  const win = {
    AkanaCore: {
      baseUrl: () => "http://127.0.0.1:8766",
      escapeAttr: (v) => String(v ?? ""),
      apiJson: (_baseFn, _method, p) => { calls += 1; return new Promise((resolve) => pending.push({ p, resolve })); },
    },
    AkanaI18n: makeI18nStub(),
  };
  win.window = win;
  const ctx = { window: win, document: doc, navigator: {}, console, MutationObserver: class { observe() {} disconnect() {} } };
  vm.runInNewContext(readStatic("akana-vault.js"), ctx);
  const vault = win.AkanaVault;
  assert.ok(vault && typeof vault.load === "function", "AkanaVault.load must load");

  const flush = async () => {
    const batch = pending.splice(0);
    for (const { p, resolve } of batch) {
      if (p === "") resolve({ namespaces: [], encryption: null });
      else if (p === "/scalars") resolve({ scalars: {} });
      else resolve({ fields: {} });
    }
    await tick();
  };

  const l1 = vault.load(); // busy=false→true; fires GET "" + GET "/scalars" (2 calls), then awaits
  const l2 = vault.load(); // requested while in flight → must be COALESCED, not dropped
  await flush();           // resolve round 1 → l1 renders → busy=false → coalesced reload fires round 2
  await flush();           // resolve round 2 (fix only)
  await Promise.all([l1, l2]);

  assert.equal(calls, 4,
    "the in-flight reload must be coalesced into a second load (HEAD drops it → only 2 calls)");
});

// ═══════ fe-settings-5: runtime source badge follows a live language flip ═══════
await section("fe-settings-5 runtime source badge re-resolves after language flip", async () => {
  const { doc } = makeDoc();
  const i18n = makeMutI18n("en");
  const win = {
    AkanaCore: { LS_BASE: "akana.baseUrl", LS_TOKEN: "akana.token", showToast: () => {}, escapeHtml: (s) => String(s), baseUrl: () => "http://x", authHeaders: () => ({}), parseApiError: () => "", configure: () => {} },
    AkanaBus: { emit: () => {} },
    AkanaI18n: i18n.api,
  };
  win.window = win;
  const localStorage = makeLocalStorage();
  const ctx = { window: win, document: doc, navigator: {}, console, localStorage, WebSocket: class {}, URL, location: { origin: "http://x" } };
  vm.runInNewContext(readStatic("akana-settings.js"), ctx);
  const settings = win.AkanaSettings;
  assert.equal(typeof settings._runtimeFormModel, "function", "_runtimeFormModel must be exported");

  const payload = {
    categories: [{ id: "c", label: "Cat" }],
    settings: [{ key: "k", label: "K", category: "c", type: "int", source: "runtime", value: 1 }],
  };
  const labelOf = () => settings._runtimeFormModel(payload)[0].fields[0].sourceLabel;

  assert.equal(labelOf(), "setting", "EN source badge (settings.runtime.source.setting)");
  i18n.setLang("tr"); // live flip (boot reconcile / another device) — no reload
  assert.equal(labelOf(), "ayar",
    "after the flip the source badge must re-resolve to Turkish (HEAD keeps the stale module-eval EN)");
  i18n.setLang("en");
  assert.equal(labelOf(), "setting", "flipping back restores EN");
});

// ═══════════ fe-settings-6: persona fork suffix comes from i18n ═══════════
await section("fe-settings-6 persona Fork suffix is i18n (EN '(copy)')", async () => {
  const { doc, register } = makeDoc();
  const pane = register("settings-pane-persona"); pane.hidden = true; // skip auto-load
  const root = register("persona-root");
  register("persona-f-name", makeEl("input"));
  register("persona-f-prompt", makeEl("textarea"));
  register("persona-f-tone", makeEl("input"));
  register("persona-form-title");
  register("persona-status");

  const i18n = makeMutI18n("en");
  const win = {
    AkanaCore: {
      baseUrl: () => "http://127.0.0.1:8766",
      escapeAttr: (v) => String(v ?? ""),
      authHeaders: () => ({}),
      apiJson: async (_baseFn, _method, _p) => ({
        personas: [{ id: "akana", name: "Akana", source: "builtin", system_prompt: "core", tone: "" }],
        bindings: [],
      }),
    },
    AkanaI18n: i18n.api,
  };
  win.window = win;
  const ctx = { window: win, document: doc, navigator: {}, console, MutationObserver: class { observe() {} disconnect() {} }, fetch: async () => ({ ok: true, json: async () => ({}) }) };
  vm.runInNewContext(readStatic("akana-personas.js"), ctx);
  const personas = win.AkanaPersonas;
  assert.ok(root._listeners.click && root._listeners.click.length, "init() must wire onClick on #persona-root");

  await personas.load(); // populate the in-memory persona list

  // Synthesize a click on the builtin persona's Fork button.
  const forkBtn = makeEl("button");
  forkBtn.dataset.action = "fork";
  forkBtn.dataset.id = "akana";
  root.appendChild(forkBtn);
  const clickFork = async () => { await root._listeners.click[0]({ target: forkBtn }); };

  await clickFork();
  assert.equal(doc.getElementById("persona-f-name").value, "Akana (copy)",
    "English UI: fork name suffix must be '(copy)', not the hardcoded Turkish '(kopya)'");

  i18n.setLang("tr");
  await clickFork();
  assert.equal(doc.getElementById("persona-f-name").value, "Akana (kopya)",
    "Turkish UI: the same i18n key yields '(kopya)'");
});

// ═══ fe-settings-7 (review): serve-inactive still opens the manual-host modal ═══
// HEAD dead-ended on the serve-inactive toast even with a local token. A self-proxied
// user with a localStorage token could previously type a custom host and get a QR — the
// fix shows the toast but falls THROUGH to the modal (only dead-ends with no local token).
await section("fe-settings-7 serve-inactive falls through to the manual-host modal when a local token exists", async () => {
  const { doc, register } = makeDoc();
  register("pair-backdrop");
  register("pair-host-confirm", makeEl("input"));
  register("pair-qr");
  register("pair-modal-foot");

  const toasts = [];
  const qrTexts = [];
  function QRCode(host, opts) { qrTexts.push(opts.text); host._qr = opts.text; host.innerHTML = "<canvas data-qr></canvas>"; }
  QRCode.CorrectLevel = { M: 0 };
  const i18n = makeI18nStub();
  const win = {
    QRCode,
    AkanaCore: { LS_TOKEN: "akana.apiToken", showToast: (m, k) => toasts.push({ m: String(m), k }), baseUrl: () => "http://127.0.0.1:8766", authHeaders: () => ({}) },
    AkanaI18n: i18n,
  };
  win.window = win;
  // Self-proxied user WITH a localStorage token + a saved (real) host.
  const localStorage = makeLocalStorage({ "akana.apiToken": "SELFTOKEN", "akana.pairHost": "100.64.1.2" });
  // token IS set server-side but Tailscale Serve is off (no pair_url) → the dead-end path.
  const fetch = async () => ({ ok: true, json: async () => ({ token_set: true, https_url: null, pair_url: null, serve_active: false }) });
  const ctx = { window: win, document: doc, localStorage, fetch, navigator: {}, console, location: { host: "127.0.0.1:8766" } };
  ctx.document.defaultView = win;
  vm.runInNewContext(readStatic("akana-pair.js"), ctx);

  await win.AkanaPair.openPairModal();

  // The informational serve-inactive toast is still shown...
  assert.ok(toasts.some((t) => t.m === i18n.t("pair.toast.serve_inactive")),
    "the serve-inactive toast must still name the missing piece");
  // ...but it must NOT dead-end: the manual-host modal opens with a QR built from the
  // local token + saved host (HEAD returned on the toast and never opened the modal).
  const backdrop = doc.getElementById("pair-backdrop");
  assert.equal(backdrop.getAttribute("aria-hidden"), "false",
    "the modal must open (fall through) so a self-proxied user can still pair via a manual host");
  assert.equal(qrTexts[qrTexts.length - 1], "https://100.64.1.2/#token=SELFTOKEN",
    "the QR is composed from the saved host + local token");
});

// ═══ fe-settings-7b: with NO local token the serve-inactive path STILL dead-ends ═══
await section("fe-settings-7b serve-inactive dead-ends (no modal) when there is no local token", async () => {
  const { doc, register } = makeDoc();
  register("pair-backdrop");
  register("pair-host-confirm", makeEl("input"));
  register("pair-qr");
  register("pair-modal-foot");
  const toasts = [];
  const qrTexts = [];
  function QRCode(host, opts) { qrTexts.push(opts.text); }
  QRCode.CorrectLevel = { M: 0 };
  const win = {
    QRCode,
    AkanaCore: { LS_TOKEN: "akana.apiToken", showToast: (m, k) => toasts.push({ m: String(m), k }), baseUrl: () => "http://127.0.0.1:8766", authHeaders: () => ({}) },
    AkanaI18n: makeI18nStub(),
  };
  win.window = win;
  const localStorage = makeLocalStorage({ "akana.apiToken": "" }); // loopback owner: no local token
  const fetch = async () => ({ ok: true, json: async () => ({ token_set: true, https_url: null, pair_url: null, serve_active: false }) });
  const ctx = { window: win, document: doc, localStorage, fetch, navigator: {}, console, location: { host: "127.0.0.1:8766" } };
  vm.runInNewContext(readStatic("akana-pair.js"), ctx);

  await win.AkanaPair.openPairModal();
  assert.equal(qrTexts.length, 0, "no QR is rendered without a local token");
  const backdrop = doc.getElementById("pair-backdrop");
  assert.notEqual(backdrop.getAttribute("aria-hidden"), "false",
    "the modal must NOT open when there is no local token to build a QR from (still a dead-end)");
});

// ───────────────────────── summary ─────────────────────────
const failed = results.filter((r) => !r.ok);
console.log(`\nblitz3_fe-settings.harness: ${results.length - failed.length}/${results.length} sections passed`);
if (failed.length) {
  for (const f of failed) console.error(`FAILED ${f.id}: ${f.err && f.err.stack ? f.err.stack : f.err}`);
  process.exit(1);
}
process.exit(0);
