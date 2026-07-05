/**
 * Onboarding i18n CONTRACT test — backend-free, node-vm.
 *
 * Guards the aurora-onboard.js wizard's user-facing copy: EVERY `onboard.*` i18n
 * key the wizard references must exist in BOTH languages (en + tr). A missing key
 * would surface the raw key string ("onboard.connect_recheck") to the user, so
 * this is a cheap regression net for the connect-step recheck feedback + the
 * expanded feature tour added alongside it.
 *
 * Two layers:
 *   1. Every `onboard.*` string literal read out of aurora-onboard.js resolves in
 *      both langs (drift-proof: new _t("onboard.x") calls are auto-covered).
 *   2. An explicit allow-list of the keys THIS change introduced, so a rename that
 *      also updates the JS still can't silently drop a translation.
 *
 * Run: node tests/web/onboard_i18n_contract.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { makeI18nStub } from "./_i18n_stub.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const STATIC = path.resolve(__dirname, "../../web_ui/static");

const { DICT } = makeI18nStub("en");

let passed = 0;
function check(label, fn) {
  fn();
  passed += 1;
  void label; // silent on success; label only surfaces in assert messages
}

/** A key must exist with a non-empty string in BOTH en and tr. */
function assertBilingual(key) {
  const entry = DICT[key];
  assert.ok(entry, `missing i18n key entirely: ${key}`);
  for (const lang of ["en", "tr"]) {
    assert.equal(typeof entry[lang], "string", `${key}.${lang} must be a string`);
    assert.ok(entry[lang].trim().length > 0, `${key}.${lang} must be non-empty`);
  }
}

// ── Layer 1: every onboard.* key referenced in aurora-onboard.js ─────────────
// Pull the literal keys out of the wizard source so any _t("onboard.…") the code
// actually uses is verified — this stays correct as the wizard evolves.
{
  const src = readFileSync(path.join(STATIC, "aurora-onboard.js"), "utf8");
  const referenced = new Set();
  const re = /["'](onboard\.[a-z0-9_]+)["']/gi;
  let m;
  while ((m = re.exec(src)) !== null) referenced.add(m[1]);

  assert.ok(referenced.size > 20, `expected many onboard.* keys, found ${referenced.size}`);
  for (const key of [...referenced].sort()) {
    check(`referenced ${key} is bilingual`, () => assertBilingual(key));
  }
}

// ── Layer 2: keys introduced by the recheck-feedback + feature-tour change ────
// Explicit so a future rename can't quietly drop one of these translations.
{
  const REQUIRED = [
    // recheck-connection feedback
    "onboard.connect_recheck",
    "onboard.connect_rechecking",
    "onboard.connect_result_ok",
    "onboard.connect_result_ready",
    "onboard.connect_result_fail",
    "onboard.connect_switch_failed",
    "onboard.connect_claude_unreachable",
    "onboard.connect_claude_nologin",
    // honest connect banner: key saved but not verified (invalid key / missing SDK / offline)
    "onboard.setup_saved_unverified",
    "onboard.setup_saved_unverified_reason",
    // expanded feature tour (memory · vault · packs · personas · connectors · voice)
    "onboard.inside_mem_t", "onboard.inside_mem_d",
    "onboard.inside_vault_t", "onboard.inside_vault_d",
    "onboard.inside_packs_t", "onboard.inside_packs_d",
    "onboard.inside_persona_t", "onboard.inside_persona_d",
    "onboard.inside_connectors_t", "onboard.inside_connectors_d",
    "onboard.inside_voice_t", "onboard.inside_voice_d",
    "onboard.inside_hint",
  ];
  for (const key of REQUIRED) {
    check(`required ${key} is bilingual`, () => assertBilingual(key));
  }
}

// ── Layer 3: parameterized messages keep their {placeholder} in both langs ────
// The recheck verdicts interpolate {provider}; a dropped token in one language
// would render a stray literal. Verify the placeholder survives translation.
{
  const PARAM = {
    "onboard.connect_result_ok": "provider",
    "onboard.connect_result_ready": "provider",
    "onboard.connect_result_fail": "provider",
    "onboard.setup_connected": "provider", // also carries {model}
    "onboard.setup_needs_key": "provider",
    "onboard.setup_saved_unverified": "provider",
    "onboard.setup_saved_unverified_reason": "provider", // also carries {reason}, checked below
  };
  for (const [key, token] of Object.entries(PARAM)) {
    check(`${key} keeps {${token}} in both langs`, () => {
      assertBilingual(key);
      for (const lang of ["en", "tr"]) {
        assert.ok(
          DICT[key][lang].includes(`{${token}}`),
          `${key}.${lang} must keep the {${token}} placeholder`,
        );
      }
    });
  }
}

// The reason-carrying variant must keep BOTH {provider} and {reason} in each language
// (a dropped {reason} would hide the concrete probe error the fix exists to surface).
check("setup_saved_unverified_reason keeps {reason} in both langs", () => {
  for (const lang of ["en", "tr"]) {
    assert.ok(
      DICT["onboard.setup_saved_unverified_reason"][lang].includes("{reason}"),
      `onboard.setup_saved_unverified_reason.${lang} must keep the {reason} placeholder`,
    );
  }
});

// ── Layer 4: RENDER-level leak scan (the classes layers 1–3 structurally miss) ─
// Layers 1–3 only prove the DICTIONARY is complete + bilingual. They cannot catch:
//   • hardcoded strings that never call _t()   (e.g. the old ACCENTS labels)
//   • an English literal fed as a {placeholder} (e.g. the old {hint:"set"})
//   • a stale wizard after akana:languagechange (no re-render → frozen language)
// So we actually RENDER the wizard in each language and scan the emitted text.
//
// Strategy: run aurora-onboard.js in a node-vm with a tiny DOM stub that records
// EVERY string assigned via textContent/innerHTML into a shared sink (tree shape
// doesn't matter — we only need the concatenated visible copy). A mutable-lang
// AkanaI18n stub backed by the real DICT lets us drive open()/show() per language.
// (vm + makeI18nStub are imported at the top of this file.)
{
  const src = readFileSync(path.join(STATIC, "aurora-onboard.js"), "utf8");

  // Shared sink of every rendered string fragment (tags stripped on read).
  let sink = [];
  const stripTags = (s) => String(s).replace(/<[^>]*>/g, " ");

  function makeNode() {
    const node = {
      children: [],
      style: {},
      dataset: {},
      _text: "",
      _html: "",
      value: "",
      type: "",
      placeholder: "",
      disabled: false,
      _qs: Object.create(null), // stable per-selector querySelector results
      classList: {
        _s: new Set(),
        add(...c) { c.forEach((x) => this._s.add(x)); },
        remove(...c) { c.forEach((x) => this._s.delete(x)); },
        toggle(c, on) { const w = on === undefined ? !this._s.has(c) : !!on; w ? this._s.add(c) : this._s.delete(c); return w; },
        contains(c) { return this._s.has(c); },
      },
      get textContent() { return this._text; },
      set textContent(v) { this._text = String(v); sink.push(String(v)); },
      get className() { return [...this.classList._s].join(" "); },
      set className(v) { this.classList._s = new Set(String(v).split(/\s+/).filter(Boolean)); },
      get innerHTML() { return this._html; },
      set innerHTML(v) { this._html = String(v); if (v) sink.push(stripTags(v)); },
      setAttribute(k, v) { if (k.startsWith("aria") || k === "title" || k === "placeholder") sink.push(String(v)); this[k] = String(v); },
      getAttribute() { return null; },
      appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
      addEventListener() {},
      removeEventListener() {},
      // Return a stable capturing node for any selector so titleEl/leadEl/bodyEl/
      // dotsEl are never null (the wizard queries them out of an innerHTML string).
      querySelector(sel) { return (this._qs[sel] ||= makeNode()); },
      querySelectorAll() { return []; },
      matches() { return false; },
      focus() {},
      dispatchEvent() {},
      isConnected: true,
      remove() {},
    };
    return node;
  }

  // Mutable-language i18n stub backed by the REAL dictionary.
  let LANG = "en";
  const { DICT: RD } = makeI18nStub("en");
  const langChangeListeners = [];
  const i18n = {
    t(key, params) {
      const entry = RD[key];
      let s = entry ? entry[LANG] ?? entry.en ?? key : key;
      if (params) for (const k in params) s = s.split(`{${k}}`).join(String(params[k]));
      return s;
    },
    ready: Promise.resolve("en"),
  };

  const ctx = {
    document: {
      readyState: "complete",
      getElementById: () => null,
      createElement: () => makeNode(),
      addEventListener: () => {},
      removeEventListener: () => {},
      documentElement: { dataset: {} },
      body: { appendChild: () => {} },
    },
    localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
    setTimeout: () => 0, // suppress the auto-open timer; we drive open() explicitly
    clearTimeout: () => {},
    console,
    fetch: async () => ({ ok: true, json: async () => ({}) }),
    AkanaI18n: i18n,
    AkanaCore: { baseUrl: () => "", authHeaders: () => ({}), parseApiError: (_b, s) => "HTTP " + s },
    addEventListener(type, fn) { if (type === "akana:languagechange") langChangeListeners.push(fn); },
    removeEventListener() {},
    dispatchLangChange(lang) { for (const fn of langChangeListeners) fn({ detail: { lang } }); },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(src, ctx);
  const onb = ctx.window.auroraOnboard;
  assert.ok(onb && typeof onb.open === "function", "auroraOnboard.open must be exposed");

  // English marker words that must NOT appear when the wizard renders in Turkish.
  // Kept to unambiguous, wizard-specific copy tokens (avoid brand names shared by
  // both languages like "Cursor", "Ollama", "API").
  const EN_MARKERS = [
    "Azure", "Violet", "Teal", "Emerald", "Sunset", // accent labels (ONB-I18N-3)
    "Connected during setup", "You're all set", "saved", "set)", // connect_already + {hint} (ONB-I18N-4)
    "Save", "Back", "Skip", "Continue", "Get started",
    "Choose a provider", "Make it yours", "What's inside", "Talk to Akana", "Let's begin",
    "microphone", // brand/family names (Chrome, Chromium, Brave) are shared across langs, so not markers
  ];
  const TR_CHARS = /[çğıöşüÇĞİÖŞÜ]/;

  // Render every step in a given language, return the concatenated visible copy.
  // open() renders step 0; the exposed _show hook walks the remaining panes so the
  // scan reaches the personalize (accents) and voice panes too.
  function renderAllSteps(lang) {
    LANG = lang;
    sink = [];
    onb.open();
    for (let i = 0; i < 6; i++) onb._show(i);
    return stripTags(sink.join(" ⁣ "));
  }

  // TR render must not contain English marker copy.
  check("TR render leaks no English wizard copy", () => {
    const tr = renderAllSteps("tr");
    for (const w of EN_MARKERS) {
      assert.ok(!tr.includes(w), `Turkish onboarding render leaked English literal "${w}"`);
    }
  });

  // EN render must not contain Turkish-only characters (symmetric guard).
  check("EN render leaks no Turkish characters", () => {
    const en = renderAllSteps("en");
    assert.ok(!TR_CHARS.test(en), "English onboarding render leaked Turkish-charset text");
  });

  // languagechange while OPEN must re-render (locks in the ONB-I18N-1 fix): open in
  // EN, flip to TR via the event, and assert the hero/skip/back copy is now Turkish.
  check("akana:languagechange re-renders an open wizard (ONB-I18N-1)", () => {
    LANG = "en";
    sink = [];
    onb.open();          // renders in English
    LANG = "tr";         // backend reconcile flips the language…
    sink = [];           // …and dispatches the event the wizard must listen for
    ctx.dispatchLangChange("tr");
    const after = stripTags(sink.join(" ⁣ "));
    assert.ok(after.length > 0, "the wizard must re-render on akana:languagechange (it emitted nothing → no listener)");
    // The re-rendered copy must be Turkish, not the stale English it opened with.
    assert.ok(TR_CHARS.test(after), "re-rendered wizard copy must be Turkish after languagechange");
    for (const w of ["Skip", "Back", "Continue", "Get started"]) {
      assert.ok(!after.includes(w), `re-rendered wizard still shows English "${w}" after languagechange`);
    }
  });
}

console.log(`onboard_i18n_contract.harness: ${passed} onboarding i18n contracts PASSED ✓`);

if (typeof process !== "undefined" && process.exit) process.exit(0);
