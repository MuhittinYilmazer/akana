/**
 * Startup name-query LANGUAGE-GATE contract test — backend-free, node-vm + fake-DOM.
 *
 * Guards the fix in akana-shell.js `fetchUserFirstName`: the boot-time memory
 * lookup must issue ONLY the active language's precise term —
 *   • English (or i18n unavailable) → GET …/facts?q=name   (NO q=adı)
 *   • Turkish                        → GET …/facts?q=adı    (NO q=name)
 * Previously BOTH queries fired unconditionally (Promise.all([name, adı])).
 *
 * Hermetic: `window.AkanaI18n.getLanguage` + `fetch` are stubbed; no real
 * network/timers. Follows the node-vm + fake-DOM ritual of the other web harnesses.
 *
 * Run: node tests/web/greeting_lang_gate.harness.mjs
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
function check(label, fn) {
  fn();
  passed += 1;
  void label; // silent: summary at the end; label only surfaces in assert messages
}

// ───────────────────────── Fake-DOM (only used surfaces) ────────────────────
function makeEl(tag = "div") {
  const el = {
    tagName: String(tag).toUpperCase(),
    children: [],
    dataset: {},
    style: {},
    attrs: {},
    _text: "",
    _html: "",
    hidden: false,
    _listeners: {},
    classList: {
      _s: new Set(),
      add(...c) { c.forEach((x) => this._s.add(x)); },
      remove(...c) { c.forEach((x) => this._s.delete(x)); },
      toggle(c, on) { const w = on === undefined ? !this._s.has(c) : on; if (w) this._s.add(c); else this._s.delete(c); return w; },
      contains(c) { return this._s.has(c); },
    },
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); this.children = []; },
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); },
    get className() { return [...this.classList._s].join(" "); },
    set className(v) { this.classList._s = new Set(String(v).split(/\s+/).filter(Boolean)); },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k] ?? null; },
    removeAttribute(k) { delete this.attrs[k]; },
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    append(...cs) { cs.forEach((c) => (typeof c === "object" ? this.appendChild(c) : null)); },
    addEventListener(t, fn) { (this._listeners[t] ||= []).push(fn); },
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  return el;
}

// Build a shell instance whose `getLanguage()` returns `lang`, wire it, then
// trigger the one-shot name fetch via the public `appendUserMessage` path.
// Returns every URL passed to fetch(), so the assertions inspect the query terms.
function bootAndFetchName(lang) {
  const fetched = [];
  const log = makeEl("div");
  const doc = {
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    createElement: (t) => makeEl(t),
    addEventListener: () => {},
  };
  const ctx = {
    window: {
      AkanaCore: {
        baseUrl: () => "http://x",
        authHeaders: () => ({}),
        escapeHtml: (s) => String(s ?? ""),
      },
      // The gate reads exactly this — `getLanguage` may be absent (undefined lang)
      // to exercise the English default branch.
      AkanaI18n: {
        t: (k) => k,
        ...(lang === undefined ? {} : { getLanguage: () => lang }),
      },
      addEventListener: () => {}, // module IIFE wires a "pagehide" listener at load
    },
    document: doc,
    console,
    setTimeout,
    clearTimeout,
    AbortController,
    requestAnimationFrame: (fn) => { setTimeout(fn, 0); return 1; },
    cancelAnimationFrame: () => {},
    fetch: async (url) => {
      fetched.push(String(url));
      return { ok: true, json: async () => ({ items: [] }) };
    },
  };
  ctx.window.window = ctx.window;
  ctx.window.document = doc;
  vm.runInNewContext(read("akana-shell.js"), ctx);

  const Shell = ctx.window.AkanaShell;
  Shell.init({
    log,
    logScroll: log,
    logEmpty: null,
    msg: null,
    form: null,
    orb: null,
    escapeHtml: (s) => String(s ?? ""),
  });
  // appendUserMessage() → ensureUserFirstName() → fetchUserFirstName() (one-shot).
  Shell.appendUserMessage("hi");
  return fetched;
}

// A URL requests a given term when its q= param decodes to exactly that term.
const queried = (urls, term) =>
  urls.some((u) => {
    const m = /[?&]q=([^&]*)/.exec(u);
    return m && decodeURIComponent(m[1]) === term;
  });

// Let the async fetch(...).then(...) chain in fetchUserFirstName settle. Two
// macrotask hops cover the awaits + the 0ms requestAnimationFrame shim.
const settle = () => new Promise((r) => setTimeout(r, 0)).then(() => new Promise((r) => setTimeout(r, 0)));

await (async () => {
  // ── English → q=name ONLY ──────────────────────────────────────────────────
  {
    const urls = bootAndFetchName("en");
    await settle();
    check("en: issues q=name", () => assert.ok(queried(urls, "name"), `expected q=name, got: ${urls.join(" | ")}`));
    check("en: does NOT issue q=adı", () => assert.ok(!queried(urls, "adı"), `q=adı must not fire in EN, got: ${urls.join(" | ")}`));
    check("en: exactly one facts query", () =>
      assert.equal(urls.filter((u) => u.includes("/memory/facts")).length, 1, "EN must fire a single facts query"));
  }

  // ── Turkish → q=adı ONLY ───────────────────────────────────────────────────
  {
    const urls = bootAndFetchName("tr");
    await settle();
    check("tr: issues q=adı", () => assert.ok(queried(urls, "adı"), `expected q=adı, got: ${urls.join(" | ")}`));
    check("tr: does NOT issue q=name", () => assert.ok(!queried(urls, "name"), `q=name must not fire in TR, got: ${urls.join(" | ")}`));
    check("tr: exactly one facts query", () =>
      assert.equal(urls.filter((u) => u.includes("/memory/facts")).length, 1, "TR must fire a single facts query"));
  }

  // ── i18n unavailable (no getLanguage) → English default (q=name) ────────────
  {
    const urls = bootAndFetchName(undefined);
    await settle();
    check("default: falls back to q=name when getLanguage absent", () =>
      assert.ok(queried(urls, "name") && !queried(urls, "adı"), `default must be EN q=name, got: ${urls.join(" | ")}`));
  }
})();

console.log(`greeting_lang_gate.harness: ${passed} language-gate contracts PASSED ✓`);

// Dangling timer node must not hang the process — hard exit on success.
if (typeof process !== "undefined" && process.exit) process.exit(0);
