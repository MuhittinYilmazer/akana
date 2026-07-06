/**
 * Akana UI i18n engine — bilingual (en|tr), English-first.
 *
 * Loaded right after akana-core.js so `AkanaI18n.t()` is available to every later
 * module. The dictionary itself lives in `akana-i18n-strings.js` (loaded BEFORE
 * this file) as `window.AkanaI18nStrings = { key: { en, tr }, … }` — keeping the
 * (large, agent-fillable) string table separate from this (small, stable) engine.
 *
 * How it swaps:
 *  - Static HTML carries `data-i18n="key"` (textContent) and attribute variants
 *    `data-i18n-placeholder|title|aria-label|html`. `apply(root)` walks them.
 *  - JS-rendered strings call `AkanaI18n.t("key", {param})`.
 *  - `setLanguage(lang)` persists to localStorage (instant next-load), flips
 *    `<html lang>`, re-applies the DOM, fires `akana:languagechange` (so modules
 *    can re-render dynamic text), and — when asked — PUTs the `language` runtime
 *    setting so voice + persona follow the same picker.
 *
 * Source of truth: the backend `language` runtime setting is authoritative (it
 * also drives voice/persona). localStorage is a first-paint cache reconciled
 * against the backend on boot, so there is no flash of the wrong language.
 */
(() => {
  const LS_LANG = "akana.lang";
  const SUPPORTED = ["en", "tr"];
  const DEFAULT_LANG = "en";

  /** Merge the external string table; missing → empty (t() falls back to key). */
  const DICT = Object.assign(Object.create(null), window.AkanaI18nStrings || {});

  function normalize(lang) {
    const l = String(lang || "").trim().toLowerCase();
    return SUPPORTED.includes(l) ? l : DEFAULT_LANG;
  }

  let current = normalize(localStorage.getItem(LS_LANG));

  /** Translate `key` for the active language; interpolate `{name}` from params. */
  function t(key, params) {
    const entry = DICT[key];
    let s = entry ? entry[current] ?? entry.en ?? key : key;
    if (params) {
      for (const k in params) s = s.split(`{${k}}`).join(String(params[k]));
    }
    return s;
  }

  const SELECTOR =
    "[data-i18n],[data-i18n-placeholder],[data-i18n-title]," +
    "[data-i18n-aria-label],[data-i18n-html]";

  function applyOne(el) {
    const txt = el.getAttribute("data-i18n");
    // textContent REPLACES children — tag leaf elements only (icon+text → wrap
    // the text in its own <span data-i18n>). Honoured by the tagging convention.
    if (txt) el.textContent = t(txt);
    const ph = el.getAttribute("data-i18n-placeholder");
    if (ph) el.setAttribute("placeholder", t(ph));
    const ti = el.getAttribute("data-i18n-title");
    if (ti) el.setAttribute("title", t(ti));
    const al = el.getAttribute("data-i18n-aria-label");
    if (al) el.setAttribute("aria-label", t(al));
    const html = el.getAttribute("data-i18n-html");
    if (html) el.innerHTML = t(html); // dictionary-authored markup only
  }

  /** Apply translations to `root` (default: whole document) and its matches. */
  function apply(root) {
    const scope = root || document;
    if (scope.matches && scope.matches(SELECTOR)) applyOne(scope);
    const list = scope.querySelectorAll ? scope.querySelectorAll(SELECTOR) : [];
    for (const el of list) applyOne(el);
    document.documentElement.setAttribute("lang", current);
  }

  function getLanguage() {
    return current;
  }

  // Returns true on a confirmed backend write, false on any failure (network or
  // non-2xx). The fire-and-forget caller (setLanguage opts.backend) ignores the
  // result — the boot reconcile converges it — but setLanguagePersisted uses it so
  // the picker can surface a failed sync instead of a mute UI/backend mismatch.
  async function persistBackend(lang) {
    try {
      const base = window.AkanaCore?.baseUrl?.() || "";
      const r = await fetch(`${base}/api/v1/settings/runtime`, {
        method: "PUT",
        headers: window.AkanaCore.authHeaders(true),
        body: JSON.stringify({ language: lang }),
      });
      return !!r && r.ok;
    } catch (_) {
      return false;
    }
  }

  /**
   * Switch the active language. `opts.backend` PUTs the runtime setting too
   * (use it for the i18n's own picker; pass false when the settings panel has
   * already saved it, to avoid a double write).
   */
  function setLanguage(lang, opts = {}) {
    const next = normalize(lang);
    const changed = next !== current;
    current = next;
    try {
      localStorage.setItem(LS_LANG, next);
    } catch (_) {
      /* private mode / quota — in-memory value still applies for this session */
    }
    apply(document);
    if (changed) {
      window.dispatchEvent(
        new CustomEvent("akana:languagechange", { detail: { lang: next } }),
      );
    }
    if (opts.backend) void persistBackend(next); // fire-and-forget: reconcile converges it
    return next;
  }

  /**
   * Switch language, AWAIT the backend write, then resolve — for the settings
   * picker, which reloads the page right after so every JS-rendered string is
   * re-emitted via `t()` in the new language. Awaiting avoids the boot reconcile
   * reverting to the old backend value before the PUT lands.
   */
  async function setLanguagePersisted(lang) {
    const next = normalize(lang);
    const ok = await persistBackend(next);
    if (!ok) {
      // U5: do NOT flip localStorage/UI when the backend write failed — otherwise the UI
      // switches but voice/persona stay in the old language with zero feedback (silent
      // divergence). Reject so the picker can revert and toast the failure.
      throw new Error("language sync failed");
    }
    current = next;
    try {
      localStorage.setItem(LS_LANG, next);
    } catch (_) {
      /* private mode / quota */
    }
    return next;
  }

  /** Pull the authoritative `language` from the backend and converge if needed. */
  async function reconcileWithBackend() {
    try {
      const base = window.AkanaCore?.baseUrl?.() || "";
      // include_hidden=1: `language` is hidden from the runtime form (the Overview
      // tab owns its picker), so the default payload omits it — ask for hidden specs
      // so boot can still converge to the backend language.
      const r = await fetch(`${base}/api/v1/settings/runtime?include_hidden=1`, {
        headers: window.AkanaCore.authHeaders(),
      });
      if (!r.ok) return;
      const body = await r.json().catch(() => null);
      const item = body?.settings?.find((s) => s && s.key === "language");
      if (!item) return;
      const backendLang = normalize(item.value);
      if (backendLang !== current) setLanguage(backendLang, { backend: false });
    } catch (_) {
      /* offline / unauthorized — keep the localStorage value */
    }
  }

  // Auto-translate dynamically inserted nodes (chat panes, settings forms) without
  // every module opting in. Cheap: only react to added element subtrees that
  // actually carry data-i18n*, batched to one frame.
  let pending = false;
  const queued = new Set();
  function flush() {
    pending = false;
    const nodes = [...queued];
    queued.clear();
    for (const n of nodes) {
      if (n.isConnected) apply(n);
    }
  }
  function observe() {
    const obs = new MutationObserver((records) => {
      for (const rec of records) {
        for (const node of rec.addedNodes) {
          if (node.nodeType !== 1) continue;
          if (node.matches?.(SELECTOR) || node.querySelector?.(SELECTOR)) {
            queued.add(node);
          }
        }
      }
      if (queued.size && !pending) {
        pending = true;
        requestAnimationFrame(flush);
      }
    });
    obs.observe(document.body, { childList: true, subtree: true });
  }

  // Resolves once the boot-time backend reconcile has settled (converged to the
  // authoritative `language` or given up). Consumers that must render in the
  // CLI-chosen language on first paint — e.g. the first-run onboarding wizard,
  // whose localStorage cache is empty on a fresh browser — await this instead of
  // racing the un-awaited reconcile. Always resolves (never rejects).
  let _resolveReady;
  const ready = new Promise((res) => { _resolveReady = res; });

  function boot() {
    apply(document);
    observe();
    reconcileWithBackend().finally(() => _resolveReady(current));
  }

  // Set <html lang> immediately (pre-DOM) to avoid a flash; full apply on ready.
  document.documentElement.setAttribute("lang", current);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }

  window.AkanaI18n = {
    t,
    apply,
    getLanguage,
    setLanguage,
    setLanguagePersisted,
    ready,
    SUPPORTED,
    DICT,
  };
})();
