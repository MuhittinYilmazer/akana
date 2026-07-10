/**
 * Akana UI core — HTTP helpers, storage keys, toast (loaded before app.js).
 */
(() => {
  const LS_BASE = "akana.baseUrl";
  const LS_TOKEN = "akana.apiToken";

  let baseUrlInput = null;
  let tokenInput = null;

  function configure(opts) {
    baseUrlInput = opts.baseUrlInput || null;
    tokenInput = opts.tokenInput || null;
  }

  function baseUrl() {
    const fromInput = baseUrlInput ? (baseUrlInput.value || "").trim() : "";
    const fromStorage = (localStorage.getItem(LS_BASE) || "").trim();
    const raw = (fromInput || fromStorage).replace(/\/+$/, "");
    return raw || `${location.protocol}//${location.host}`;
  }

  function authHeaders(includeJsonContentType) {
    const t = ((tokenInput && tokenInput.value) || localStorage.getItem(LS_TOKEN) || "").trim();
    const h = {};
    if (includeJsonContentType) h["Content-Type"] = "application/json";
    if (t) h.Authorization = `Bearer ${t}`;
    return h;
  }

  function authHeadersMultipart() {
    const t = ((tokenInput && tokenInput.value) || localStorage.getItem(LS_TOKEN) || "").trim();
    const h = {};
    if (t) h.Authorization = `Bearer ${t}`;
    return h;
  }

  function parseApiError(body, status) {
    if (!body || typeof body !== "object") return `HTTP ${status}`;
    const d = body.detail;
    if (typeof d === "string" && d.trim()) return d;
    if (Array.isArray(d)) {
      return d
        .map((x) => (x && x.msg) || "")
        .filter(Boolean)
        .join("; ");
    }
    if (d && typeof d === "object") {
      // Two shapes in this API: detail.error as a plain string (pack/setup
      // errors → {error, setup_skills}) or nested {error:{code,message}} (auth).
      if (typeof d.error === "string" && d.error.trim()) return d.error;
      if (d.error && typeof d.error === "object" && d.error.message) {
        return String(d.error.message);
      }
    }
    if (body.message) return String(body.message);
    return `HTTP ${status}`;
  }

  function escapeHtml(s) {
    return String(s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  // Attribute-safe escape: like escapeHtml but ALSO escapes the single quote, so
  // the result is safe inside single- or double-quoted HTML attributes. The
  // settings-tab modules (vault/packs/personas) build attributes by hand and rely
  // on the apostrophe entity — keep this separate from escapeHtml, which leaves
  // ' untouched.
  function escapeAttr(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
    ));
  }

  // Shared JSON fetch for the settings-tab modules. `apiBaseFn` returns the base
  // URL for the module's endpoint group; the extracted error message honours the
  // nested {detail:{error:{message}}} shape those endpoints use, falling back to
  // `HTTP <status>`. 204 → {}.
  async function apiJson(apiBaseFn, method, path, body) {
    const opts = { method, headers: authHeaders(Boolean(body)) };
    if (body) opts.body = JSON.stringify(body);
    const res = await fetch(`${apiBaseFn()}${path}`, opts);
    if (!res.ok) {
      let message = `HTTP ${res.status}`;
      try {
        const j = await res.json();
        message = (j && j.detail && j.detail.error && j.detail.error.message) || message;
      } catch (_) { /* non-JSON error body */ }
      throw new Error(message);
    }
    return res.status === 204 ? {} : res.json();
  }

  function showToast(message, kind = "info") {
    const stack = document.getElementById("ui-toast-stack");
    if (!stack || !message) return;
    const el = document.createElement("div");
    el.className = `ui-toast ui-toast--${kind}`;
    el.textContent = message;
    stack.appendChild(el);
    requestAnimationFrame(() => el.classList.add("is-visible"));
    // Error and warning toasts often carry action-oriented text ("…remove excess",
    // "…try another provider") — give the user time to read; info/success are short.
    const ttl = kind === "err" || kind === "warn" ? 5200 : 3400;
    setTimeout(() => {
      el.classList.remove("is-visible");
      setTimeout(() => el.remove(), 280);
    }, ttl);
  }

  window.AkanaCore = {
    LS_BASE,
    LS_TOKEN,
    configure,
    baseUrl,
    authHeaders,
    authHeadersMultipart,
    parseApiError,
    escapeHtml,
    escapeAttr,
    apiJson,
    showToast,
  };
})();

/**
 * PWA entrypoints — manifest shortcuts (?action=new|voice) and GET share_target
 * (?title/&text/&url). Runs after `load` so deferred modules (voice, app.js) are
 * wired, then defers one more microtask + frame so their DOMContentLoaded
 * handlers have bound listeners.
 */
(() => {
  function handleLaunchParams() {
    // Read the query string ONCE, then strip ONLY the params we consume so reloads /
    // shares don't re-trigger. akana-core.js is also loaded on /memory, so a blanket
    // strip would erase Memory Studio's ?view= router param (and the hash) on every
    // page. Leave unknown params (and the fragment) intact.
    const CONSUMED = ["action", "text", "url", "title"];
    const params = new URLSearchParams(location.search);
    const action = params.get("action");
    const text = params.get("text");
    const url = params.get("url");
    const title = params.get("title");
    if (!CONSUMED.some((k) => params.has(k))) return; // nothing for us — don't touch the URL
    for (const k of CONSUMED) params.delete(k);
    const rest = params.toString();
    history.replaceState({}, "", location.pathname + (rest ? `?${rest}` : "") + location.hash);

    if (action === "new") {
      document.getElementById("btn-new-conv")?.click();
      return;
    }
    if (action === "voice") {
      if (window.AkanaVoice?.enterConversationMode) {
        window.AkanaVoice.enterConversationMode("shortcut");
      } else {
        document.getElementById("btn-wake")?.click();
      }
      return;
    }

    // Share target: seed a fresh conversation with the shared content but never
    // auto-submit — the user reviews and sends.
    if (text || url || title) {
      document.getElementById("btn-new-conv")?.click();
      const msg = document.getElementById("msg");
      if (msg) {
        msg.value = [text, url].filter(Boolean).join(" ");
        msg.dispatchEvent(new Event("input", { bubbles: true }));
        try {
          msg.focus();
        } catch (_) {
          /* focus may throw if detached; ignore */
        }
      }
    }
  }

  function init() {
    // Two-step defer: load → microtask → frame, so other modules' init has run.
    queueMicrotask(() => {
      requestAnimationFrame(() => {
        handleLaunchParams();
      });
    });
  }

  if (document.readyState === "complete") {
    init();
  } else {
    window.addEventListener("load", init, { once: true });
  }
})();
