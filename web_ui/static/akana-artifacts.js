/**
 * Akana Artifacts — live code preview (Claude Artifacts style).
 *
 * Completed HTML/SVG code blocks in assistant messages get a "Preview" button
 * (added by the render layer); clicking opens a SANDBOXED iframe in the right panel.
 * The panel does not close the chat — side-by-side (split) on wide screens,
 * full-screen overlay on narrow screens.
 *
 * SECURITY: iframe `sandbox="allow-scripts"` (NO allow-same-origin) → artifact
 * runs in an opaque origin; cannot access our localStorage/cookie/DOM.
 *
 * Extensibility: per-type renderer registry — new types are added with `register()`
 * without touching the render layer (mermaid/react in the next wave).
 */
(() => {
  const bus = () => window.AkanaBus;

  /* ── Renderer registry (extensibility gate) ───────────────────────────────
     Each renderer: { type, label, match(lang, code) -> bool,
                      toSrcdoc(code) -> string, ext, mime } */
  const renderers = [];
  function register(r) {
    if (r && typeof r.match === "function" && typeof r.toSrcdoc === "function") {
      renderers.unshift(r); // later registrations take priority
    }
  }
  function resolve(lang, code) {
    // Language tag may be "html", "HTML", or even "html title=x" → first token, lowercase.
    const l = (lang || "").toLowerCase().trim().split(/\s+/)[0];
    const c = (code || "").trim();
    for (const r of renderers) {
      try {
        if (r.match(l, c)) return r;
      } catch {
        /* skip if renderer match throws */
      }
    }
    return null;
  }
  function isPreviewable(lang, code) {
    return resolve(lang, code) != null;
  }

  // Skip leading whitespace / HTML comments / XML prologs to find the real start
  // (model output often begins with these → detection/wrapping must not be thrown off).
  function leadTrim(code) {
    let s = String(code || "");
    let prev;
    do {
      prev = s;
      s = s.replace(/^\s+/, "").replace(/^<!--[\s\S]*?-->/, "").replace(/^<\?xml\b[^>]*\?>/i, "");
    } while (s !== prev);
    return s;
  }
  function looksFullDoc(code) {
    const s = leadTrim(code);
    return /^<!doctype\s+html/i.test(s) || /^<html[\s>]/i.test(s);
  }
  function looksSvg(code) {
    return /^<svg[\s>]/i.test(leadTrim(code));
  }

  function wrapFragment(code) {
    return (
      "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">" +
      "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">" +
      "<meta name=\"color-scheme\" content=\"light dark\">" +
      "<style>:root{color-scheme:light dark}" +
      "body{margin:0;padding:16px;font-family:system-ui,-apple-system,sans-serif;line-height:1.5}</style>" +
      "</head><body>" +
      code +
      "</body></html>"
    );
  }

  register({
    type: "html",
    label: "HTML",
    ext: "html",
    mime: "text/html",
    match: (lang, code) =>
      lang === "html" || lang === "htm" || (!lang && looksFullDoc(code)),
    toSrcdoc: (code) => (looksFullDoc(code) ? code : wrapFragment(code)),
  });

  register({
    type: "svg",
    label: "SVG",
    ext: "svg",
    mime: "image/svg+xml",
    match: (lang, code) =>
      lang === "svg" || ((lang === "xml" || !lang) && looksSvg(code)),
    toSrcdoc: (code) =>
      "<!doctype html><html><head><meta charset=\"utf-8\"><style>" +
      "html,body{height:100%;margin:0}body{display:grid;place-items:center;" +
      "background:#fff;padding:12px;box-sizing:border-box}svg{max-width:100%;max-height:100%}" +
      "</style></head><body>" +
      code +
      "</body></html>",
  });

  // NOTE: Mermaid/diagram support removed (user request) — ```mermaid
  // blocks are shown as plain code blocks and do not enter the artifact panel.

  // Markdown "document" view: the existing safe AkanaMarkdown.render (HTML-escaped,
  // no raw HTML pass-through) converts to HTML on the main page and is shown as a
  // clean reading document inside a sandboxed iframe. Zero dependencies / offline.
  const MD_DOC_CSS =
    ":root{color-scheme:light dark;--fg:#1a1d24;--mut:#5b6573;--bd:#e2e6ee;--ac:#2f7fe6;--cbg:#f4f6fa}" +
    "@media(prefers-color-scheme:dark){:root{--fg:#e6e9f0;--mut:#9aa3b2;--bd:#2a2f3a;--ac:#5aa9ff;--cbg:#11151c}}" +
    "html,body{margin:0}body{background:Canvas;color:var(--fg);" +
    'font:16px/1.7 system-ui,-apple-system,"Segoe UI",sans-serif}' +
    ".doc{max-width:760px;margin:0 auto;padding:40px 28px 80px}" +
    ".doc h1,.doc h2,.doc h3,.doc h4{line-height:1.3;margin:1.6em 0 .6em;font-weight:650}" +
    ".doc h1{font-size:1.9em;margin-top:0}.doc h2{font-size:1.45em}.doc h3{font-size:1.2em}" +
    ".doc p{margin:.8em 0}.doc a,.doc .md-link{color:var(--ac);text-decoration:none}" +
    ".doc a:hover{text-decoration:underline}.doc ul,.doc ol{padding-left:1.5em;margin:.7em 0}" +
    ".doc li{margin:.3em 0}.doc blockquote,.doc .md-quote{margin:1em 0;padding:.4em 1em;" +
    "border-left:3px solid var(--ac);color:var(--mut)}" +
    ".doc code,.doc .md-inline-code{background:var(--cbg);padding:.12em .4em;border-radius:5px;" +
    "font:.88em ui-monospace,monospace}.doc pre,.doc pre.md-code{background:var(--cbg);" +
    "padding:14px 16px;border-radius:10px;overflow:auto}.doc pre code{background:none;padding:0}" +
    ".doc table,.doc .md-table{border-collapse:collapse;width:100%;margin:1em 0;font-size:.95em}" +
    ".doc th,.doc td{border:1px solid var(--bd);padding:8px 12px;text-align:left}" +
    ".doc th{background:var(--cbg)}.doc hr,.doc .md-hr{border:none;border-top:1px solid var(--bd);margin:2em 0}" +
    ".doc img{max-width:100%}.doc .md-task-list{list-style:none;padding-left:.2em}" +
    ".doc .md-task{display:flex;gap:.5em;align-items:baseline}" +
    ".doc .md-task-box{display:inline-block;width:1em;height:1em;border:1.5px solid var(--mut);" +
    "border-radius:3px;font-size:.8em;line-height:1;text-align:center}";

  function markdownDoc(code) {
    let body = "";
    try {
      body = window.AkanaMarkdown && window.AkanaMarkdown.render ? window.AkanaMarkdown.render(String(code)) : "";
    } catch {
      body = "";
    }
    if (!body) {
      body =
        "<pre>" +
        String(code).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;") +
        "</pre>";
    }
    return (
      '<!doctype html><html lang="en"><head><meta charset="utf-8">' +
      '<meta name="viewport" content="width=device-width,initial-scale=1">' +
      '<meta name="color-scheme" content="light dark"><style>' +
      MD_DOC_CSS +
      '</style></head><body><main class="doc">' +
      body +
      "</main></body></html>"
    );
  }

  register({
    type: "markdown",
    label: "Document",
    ext: "md",
    mime: "text/markdown",
    match: (lang) => lang === "markdown" || lang === "md",
    toSrcdoc: (code) => markdownDoc(code),
  });

  const _t = (k) => window.AkanaI18n?.t(k) ?? k;

  /* ── Panel controller ─────────────────────────────────────────────────── */
  let els = null;
  let current = null; // { code, lang, renderer, title }
  let lastTrigger = null; // element to return focus to on close (a11y)

  function dom() {
    if (els) return els;
    const panel = document.getElementById("artifact-panel");
    if (!panel) return null;
    els = {
      panel,
      backdrop: document.getElementById("artifact-backdrop"),
      title: document.getElementById("artifact-title"),
      langTag: document.getElementById("artifact-lang"),
      preview: document.getElementById("artifact-preview"),
      codeEl: panel.querySelector("#artifact-code code"),
      tabs: Array.from(panel.querySelectorAll("[data-artifact-tab]")),
      close: document.getElementById("artifact-close"),
      resize: document.getElementById("artifact-resize"),
    };
    wire();
    reflow();
    return els;
  }

  /* ── Width: --artifact-w drives both the panel and chat padding ─────────── */
  const LS_WIDTH = "akana.artifactWidth";
  const MIN_W = 320;
  const isWide = () => window.matchMedia("(min-width: 901px)").matches;
  const maxW = () => Math.max(MIN_W, Math.round(Math.min(window.innerWidth * 0.92, window.innerWidth - 40)));

  function applyWidth(px) {
    // On narrow screens, don't let the inline variable override the 100vw CSS rule → remove it.
    if (!isWide()) {
      document.documentElement.style.removeProperty("--artifact-w");
      return;
    }
    const w = Math.max(MIN_W, Math.min(Math.round(px), maxW()));
    document.documentElement.style.setProperty("--artifact-w", w + "px");
  }

  function savedWidth() {
    const v = parseInt(localStorage.getItem(LS_WIDTH) || "", 10);
    return Number.isFinite(v) ? v : null;
  }

  // Re-apply width on screen size / orientation change (mobile ↔ desktop).
  function reflow() {
    if (!isWide()) {
      document.documentElement.style.removeProperty("--artifact-w");
      return;
    }
    const w = savedWidth();
    if (w) applyWidth(w);
  }
  window.addEventListener("resize", reflow);

  function wire() {
    const e = els;
    e.tabs.forEach((t) =>
      t.addEventListener("click", () => setTab(t.dataset.artifactTab)),
    );
    e.close?.addEventListener("click", close);
    e.backdrop?.addEventListener("click", close);
    e.panel.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape") {
        ev.stopPropagation();
        close();
      }
    });
    e.panel.addEventListener("click", (ev) => {
      const btn = ev.target?.closest?.("[data-artifact-act]");
      if (!btn) return;
      const act = btn.dataset.artifactAct;
      if (act === "copy") void copyCode(btn);
      else if (act === "download") downloadCode();
      else if (act === "newtab") openInNewTab();
      else if (act === "refresh") renderPreview();
      else if (act === "maximize") toggleMax(btn);
    });
    wireResize();
  }

  /* ── Drag-resize (from the left edge) ──────────────────────────────────── */
  let dragging = false;
  function wireResize() {
    const e = els;
    if (!e.resize) return;
    e.resize.addEventListener("pointerdown", (ev) => {
      if (!isWide()) return;
      dragging = true;
      maximized = false;
      e.panel.classList.add("is-resizing");
      try {
        e.resize.setPointerCapture(ev.pointerId);
      } catch {
        /* pointermove still reaches the panel if setPointerCapture is unsupported */
      }
      ev.preventDefault();
    });
    e.resize.addEventListener("pointermove", (ev) => {
      if (!dragging) return;
      applyWidth(window.innerWidth - ev.clientX);
    });
    const end = (ev) => {
      if (!dragging) return;
      dragging = false;
      e.panel.classList.remove("is-resizing");
      try {
        e.resize.releasePointerCapture(ev.pointerId);
      } catch {
        /* ignore */
      }
      const cur = parseInt(getComputedStyle(e.panel).width, 10);
      if (Number.isFinite(cur)) localStorage.setItem(LS_WIDTH, String(cur));
    };
    e.resize.addEventListener("pointerup", end);
    e.resize.addEventListener("pointercancel", end);
  }

  /* ── Maximise/restore toggle ───────────────────────────────────────────── */
  let maximized = false;
  let restoreW = null;
  function toggleMax(btn) {
    if (!isWide()) return;
    const e = els;
    if (maximized) {
      maximized = false;
      applyWidth(restoreW || savedWidth() || 520);
    } else {
      maximized = true;
      restoreW = parseInt(getComputedStyle(e.panel).width, 10) || null;
      applyWidth(maxW());
    }
    btn?.setAttribute("aria-pressed", maximized ? "true" : "false");
    e.panel.classList.toggle("is-maximized", maximized);
  }

  function setTab(tab) {
    const e = dom();
    if (!e) return;
    const t = tab === "code" ? "code" : "preview";
    e.panel.dataset.tab = t;
    e.tabs.forEach((btn) => {
      const active = btn.dataset.artifactTab === t;
      btn.classList.toggle("is-active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    });
  }

  function renderPreview() {
    const e = dom();
    if (!e || !current) return;
    // Fresh iframe: kills the previous artifact's scripts/timers.
    const frame = document.createElement("iframe");
    frame.className = "artifact-frame";
    // NO allow-same-origin → opaque origin (isolated). NO allow-modals → artifact's
    // alert/confirm CANNOT block the main tab (no freeze when generated demo code calls alert).
    // NO allow-popups → window.open spam is blocked.
    frame.setAttribute("sandbox", "allow-scripts allow-forms");
    frame.setAttribute("referrerpolicy", "no-referrer");
    frame.setAttribute("title", current.title || _t("ui.artifact_preview_title"));
    frame.srcdoc = current.renderer.toSrcdoc(current.code);
    e.preview.replaceChildren(frame);
  }

  function extractTitle(code, renderer) {
    if (renderer.type === "markdown") {
      // First heading line becomes the document title (e.g. "# Report" → "Report").
      const h = /^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$/m.exec(code || "");
      if (h && h[1].trim()) return h[1].trim().slice(0, 80);
      return _t("ui.artifact_doc_title");
    }
    const m = /<title[^>]*>([^<]+)<\/title>/i.exec(code || "");
    if (m && m[1].trim()) return m[1].trim().slice(0, 80);
    return renderer.label + _t("ui.artifact_label_suffix");
  }

  function open(opts) {
    const e = dom();
    if (!e) return;
    const code = String(opts?.code || "");
    const lang = String(opts?.lang || "");
    const renderer = resolve(lang, code);
    if (!renderer) return;
    lastTrigger = opts?.trigger || null;
    current = { code, lang, renderer, title: opts?.title || extractTitle(code, renderer) };

    e.title.textContent = current.title;
    if (e.langTag) e.langTag.textContent = renderer.label;
    if (e.codeEl) e.codeEl.textContent = code;
    renderPreview();
    setTab("preview");

    document.body.classList.add("artifacts-open");
    e.panel.setAttribute("aria-hidden", "false");
    e.backdrop?.setAttribute("aria-hidden", "false");
    e.panel.focus({ preventScroll: true });
    bus()?.emit("artifact:open", { type: renderer.type, lang });
  }

  function openFromButton(btn) {
    const shell = btn?.closest?.(".md-code-shell");
    const pre = shell?.querySelector("pre.md-code");
    if (!pre) return;
    const codeEl = pre.querySelector("code");
    const code = (codeEl || pre).textContent || "";
    open({ code, lang: pre.dataset.lang || "", trigger: btn });
  }

  function close() {
    const e = dom();
    if (!e) return;
    document.body.classList.remove("artifacts-open");
    e.panel.setAttribute("aria-hidden", "true");
    e.backdrop?.setAttribute("aria-hidden", "true");
    // Remove iframe → stop running scripts/timers/audio.
    e.preview.replaceChildren();
    // Return focus to the triggering button (keyboard user must not get lost).
    if (lastTrigger && document.contains(lastTrigger)) {
      lastTrigger.focus({ preventScroll: true });
    }
    lastTrigger = null;
    bus()?.emit("artifact:close", {});
  }

  /* ── Actions ───────────────────────────────────────────────────────────── */
  function flash(btn, label) {
    if (!btn) return;
    const prev = btn.dataset.label || btn.textContent;
    btn.dataset.label = prev;
    btn.textContent = label;
    if (btn._t) clearTimeout(btn._t);
    btn._t = setTimeout(() => {
      btn.textContent = btn.dataset.label;
      btn._t = null;
    }, 1400);
  }

  async function copyCode(btn) {
    if (!current) return;
    try {
      await navigator.clipboard.writeText(current.code);
      flash(btn, _t("ui.artifact_copied"));
    } catch {
      flash(btn, _t("ui.artifact_copy_failed"));
    }
  }

  function slugify(s) {
    return (
      (s || "artifact")
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-+|-+$/g, "")
        .slice(0, 40) || "artifact"
    );
  }

  function downloadCode() {
    if (!current) return;
    const r = current.renderer;
    const blob = new Blob([current.code], { type: r.mime || "text/plain" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${slugify(current.title)}.${r.ext || "txt"}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }

  function openInNewTab() {
    if (!current) return;
    // SECURITY: a blob: URL inherits the CREATOR page's origin, so opening the
    // artifact document directly as a top-level page would run its scripts
    // same-origin with the app (localStorage/apiToken/authenticated fetch) —
    // defeating the sandbox model this panel exists to enforce. Instead the new
    // tab loads a tiny host page that embeds the artifact in the SAME sandboxed
    // iframe used in-panel (allow-scripts, NO allow-same-origin → opaque origin).
    const doc = current.renderer.toSrcdoc(current.code);
    const srcdoc = doc
      .replace(/&/g, "&amp;")
      .replace(/"/g, "&quot;");
    const host =
      "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">" +
      "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">" +
      "<title></title><style>html,body{margin:0;height:100%}" +
      "iframe{border:0;width:100%;height:100%;display:block}</style></head><body>" +
      "<iframe sandbox=\"allow-scripts allow-forms\" referrerpolicy=\"no-referrer\" " +
      "srcdoc=\"" + srcdoc + "\"></iframe></body></html>";
    const blob = new Blob([host], { type: "text/html" });
    const url = URL.createObjectURL(blob);
    window.open(url, "_blank", "noopener");
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  }

  /* ── Auto-open ─────────────────────────────────────────────────────────
     When a live response ends (chat:stream:done), automatically opens the last
     previewable block — user doesn't need to press "Preview". IMPORTANT: done is
     emitted only on LIVE streams; not on F5/history load → old artifacts don't
     fling the panel on page load.
     Default ON; disable with localStorage("akana.artifactAutoOpen"="0"). */
  const LS_AUTO = "akana.artifactAutoOpen";
  const autoOpenEnabled = () => localStorage.getItem(LS_AUTO) !== "0";
  function setAutoOpen(on) {
    localStorage.setItem(LS_AUTO, on ? "1" : "0");
  }

  function autoOpenLatest() {
    const log = document.getElementById("log");
    if (!log) return;
    const rows = log.querySelectorAll(".row-assistant");
    const row = rows[rows.length - 1];
    if (!row) return;
    // Open the last previewable (completed) block — scan from the end.
    const pres = row.querySelectorAll("pre.md-code:not(.md-code--partial)");
    for (let i = pres.length - 1; i >= 0; i -= 1) {
      const pre = pres[i];
      const code = (pre.querySelector("code") || pre).textContent || "";
      if (isPreviewable(pre.dataset.lang || "", code)) {
        open({ code, lang: pre.dataset.lang || "" });
        return;
      }
    }
  }

  window.AkanaBus?.on?.("chat:stream:done", () => {
    if (autoOpenEnabled()) autoOpenLatest();
  });

  // Settings › Appearance toggle (if present): reflect state + save on change.
  function wireAutoOpenToggle() {
    const cb = document.getElementById("settings-artifact-autoopen");
    if (!cb) return;
    cb.checked = autoOpenEnabled();
    cb.addEventListener("change", () => setAutoOpen(cb.checked));
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wireAutoOpenToggle, { once: true });
  } else {
    wireAutoOpenToggle();
  }

  window.AkanaArtifacts = {
    register,
    resolve,
    isPreviewable,
    open,
    openFromButton,
    close,
  };
})();
