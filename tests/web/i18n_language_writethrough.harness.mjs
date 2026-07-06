/**
 * i18n language write-through contract (akana-i18n.js) — backend-free, node-vm.
 *
 * U5: the UI language picker is the single source of truth that must ALSO drive the
 * server `language` runtime setting (voice + persona), and the UI must follow the
 * backend value on boot. This harness loads the REAL akana-i18n.js in a bare VM with a
 * scriptable `fetch` and asserts:
 *  - setLanguagePersisted PUTs { language: 'tr' } to /api/v1/settings/runtime and, on a
 *    2xx, flips localStorage + the active language.
 *  - a NON-OK PUT (e.g. 500) REJECTS and leaves localStorage/active language unchanged —
 *    so the picker can revert + toast instead of the UI diverging from voice/persona
 *    silently (the old code swallowed the failure and flipped the UI anyway).
 *  - boot reconcile GETs runtime?include_hidden=1 and converges the active language to
 *    the backend value even with an empty localStorage cache (backend is authoritative).
 *
 * Run: node tests/web/i18n_language_writethrough.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const src = readFileSync(path.join(REPO, "web_ui/static/akana-i18n.js"), "utf8");

// ── source-level markers: the write-through + reconcile contract must exist ──────
for (const marker of [
  "/api/v1/settings/runtime",
  "include_hidden=1",
  "setLanguagePersisted",
  "reconcileWithBackend",
]) {
  assert.ok(src.includes(marker), `missing marker in akana-i18n.js: ${marker}`);
}

/** Build a fresh VM realm running the real akana-i18n.js with a scriptable fetch. */
function loadI18n({ fetchImpl, initialLang = null } = {}) {
  const store = new Map();
  if (initialLang != null) store.set("akana.lang", initialLang);
  const calls = [];
  const listeners = {};
  const ctx = {
    console,
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: (k) => store.delete(k),
    },
    requestAnimationFrame: (fn) => { fn(); return 0; },
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    MutationObserver: class { observe() {} disconnect() {} },
    fetch: async (url, opts) => {
      calls.push({ url: String(url), opts: opts || {} });
      return fetchImpl(String(url), opts || {});
    },
  };
  ctx.window = {
    AkanaCore: {
      baseUrl: () => "",
      authHeaders: () => ({}),
    },
    addEventListener: (type, fn) => { (listeners[type] ||= []).push(fn); },
    dispatchEvent: (ev) => { (listeners[ev.type] || []).forEach((fn) => fn(ev)); return true; },
  };
  ctx.window.window = ctx.window;
  ctx.window.fetch = ctx.fetch;
  ctx.fetch = ctx.window.fetch;
  const elLang = { setAttribute: () => {}, getAttribute: () => null };
  ctx.document = {
    readyState: "complete",
    documentElement: elLang,
    body: {},
    querySelectorAll: () => [],
    matches: () => false,
    addEventListener: (type, fn) => { (listeners[type] ||= []).push(fn); },
  };
  ctx.window.document = ctx.document;
  ctx.window.localStorage = ctx.localStorage;
  ctx.window.requestAnimationFrame = ctx.requestAnimationFrame;
  ctx.window.CustomEvent = ctx.CustomEvent;
  ctx.window.MutationObserver = ctx.MutationObserver;
  vm.runInNewContext(src, ctx);
  return { i18n: ctx.window.AkanaI18n, calls, store };
}

// A green backend for the write-through happy path; scripts the reconcile GET too.
function okBackend(getLang) {
  return async (url, opts) => {
    const method = (opts.method || "GET").toUpperCase();
    if (method === "PUT") return { ok: true, status: 200, json: async () => ({}) };
    // GET runtime?include_hidden=1 → authoritative language
    return {
      ok: true,
      status: 200,
      json: async () => ({ settings: [{ key: "language", value: getLang() }] }),
    };
  };
}

// ── 1) write-through: setLanguagePersisted PUTs {language:'tr'} and flips on 2xx ──
{
  const { i18n, calls, store } = loadI18n({ fetchImpl: okBackend(() => "en") });
  await i18n.ready; // let the boot reconcile settle first (it would otherwise race the flip)
  assert.equal(i18n.getLanguage(), "en", "default is English");
  const out = await i18n.setLanguagePersisted("tr");
  assert.equal(out, "tr");
  const put = calls.find((c) => (c.opts.method || "").toUpperCase() === "PUT");
  assert.ok(put, "a PUT must be issued");
  assert.ok(put.url.includes("/api/v1/settings/runtime"), "PUT hits the runtime settings API");
  assert.equal(JSON.parse(put.opts.body).language, "tr", "PUT body carries {language:'tr'}");
  assert.equal(i18n.getLanguage(), "tr", "active language flipped after a green PUT");
  assert.equal(store.get("akana.lang"), "tr", "localStorage cache flipped after a green PUT");
}

// ── 2) silent-failure fix: a non-ok PUT REJECTS and leaves state untouched ────────
{
  const failBackend = async (url, opts) => {
    const method = (opts.method || "GET").toUpperCase();
    if (method === "PUT") return { ok: false, status: 500, json: async () => ({}) };
    return { ok: true, status: 200, json: async () => ({ settings: [{ key: "language", value: "en" }] }) };
  };
  const { i18n, store } = loadI18n({ fetchImpl: failBackend });
  await i18n.ready; // settle the boot reconcile first
  await assert.rejects(
    () => i18n.setLanguagePersisted("tr"),
    "a failed backend write must reject (old code swallowed it and flipped the UI anyway)",
  );
  assert.equal(i18n.getLanguage(), "en", "active language must NOT flip when the PUT failed");
  assert.equal(store.get("akana.lang"), undefined, "localStorage must NOT flip when the PUT failed");
}

// ── 3) boot reconcile: backend value wins even with an empty localStorage cache ───
{
  // No initialLang (empty cache); backend says 'tr'. The boot reconcile fires in the
  // IIFE (document.readyState === "complete" → boot() runs synchronously up to the async
  // GET). Await `ready` to let the reconcile settle, then assert convergence.
  const { i18n } = loadI18n({ fetchImpl: okBackend(() => "tr"), initialLang: null });
  const settled = await i18n.ready;
  assert.equal(settled, "tr", "ready resolves to the converged backend language");
  assert.equal(i18n.getLanguage(), "tr", "boot reconcile converged the UI to the backend value");
}

console.log("i18n language write-through contract test: OK");
