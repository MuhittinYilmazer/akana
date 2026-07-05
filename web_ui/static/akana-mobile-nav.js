/**
 * Akana — MOBILE BOTTOM TAB STRIP + device-class detector
 * ─────────────────────────────────────────────────────────────────────────
 * Single-handed bottom navigation for phone/standalone PWA. VANILLA, defer.
 *
 * TWO JOBS:
 *  1) Detector — updates body.is-mobile / body.is-standalone on load AND on
 *     matchMedia change (SINGLE source of truth for CSS + JS). Same
 *     matchMedia(...).addEventListener("change") pattern as akana-settings.js:1770.
 *  2) Strip — <nav class="mnav"> 4 tabs: Chat · Voice · Memory · Settings.
 *     Each tab CALLS existing public APIs (invents no new behaviour):
 *       • Chat    → window.AkanaChat.closeArchiveDrawer()  (close drawer)
 *                 + window.AkanaShell.scrollLogToBottom()   (scroll log to bottom)
 *                 + window.AkanaShell.resizeComposer()       (fix composer)
 *       • Voice   → window.AkanaVoice.enterConversationMode("mnav")
 *                   fallback: document.getElementById("btn-mic").click()
 *       • Memory  → location.href = "/memory"   (full page — decided)
 *       • Settings → window.AkanaSettings.openSettings()
 *
 * Active state: set on touch (no reliable view/route event).
 * Also: AkanaBus "voice:mode:exit" → Voice tab deactivates, returns to Chat;
 * "chat:conversation:changed" → returns to Chat (chat-context return signal).
 *
 * VISIBILITY IS IN CSS: this JS ALWAYS builds the strip and LEAVES it with the
 * `hidden` attribute. On desktop UA [hidden]→display:none hides it; aurora-mobile.css
 * @media (≤560px coarse / standalone) overrides with `body .mnav{display:grid}`.
 * This gates visibility without adding any rule to the base CSS layer.
 */
(() => {
  "use strict";

  // ── 1 · DEVICE-CLASS DETECTOR ────────────────────────────────────────────
  // Single source of truth: the matchMedias below reflect both body classes and
  // (indirectly) CSS gates. CSS works independently via its own @media; these
  // classes are the single-truth read for any other JS modules that need them.
  const mqMobile = window.matchMedia
    ? window.matchMedia("(max-width: 560px) and (pointer: coarse)")
    : null;
  const mqStandalone = window.matchMedia
    ? window.matchMedia("(display-mode: standalone)")
    : null;

  function syncDeviceClasses() {
    const body = document.body;
    if (!body) return;
    const isStandalone =
      (mqStandalone && mqStandalone.matches) ||
      // iOS Safari standalone flag (may not support display-mode).
      window.navigator.standalone === true;
    const isMobile = (mqMobile && mqMobile.matches) || isStandalone;
    body.classList.toggle("is-mobile", !!isMobile);
    body.classList.toggle("is-standalone", !!isStandalone);
  }

  // Listen for matchMedia changes (akana-settings.js:1770 pattern). Older Safari
  // may not support addEventListener → fall back to addListener.
  function bindMql(mql) {
    if (!mql) return;
    if (mql.addEventListener) mql.addEventListener("change", syncDeviceClasses);
    else if (mql.addListener) mql.addListener(syncDeviceClasses);
  }
  bindMql(mqMobile);
  bindMql(mqStandalone);

  const _t = (k) => window.AkanaI18n?.t(k) ?? k;

  // ── 2 · TAB ACTIONS (wired to existing APIs) ──────────────────────────────
  function goChat() {
    // MINIMAL: only close an open overlay to reveal the chat. Do NOT scroll chat
    // content, resize the composer, or focus #msg (keyboard would open) —
    // "only the bottom strip should react". Active tab state is already set via
    // setActive in the click handler.
    try {
      window.AkanaChat?.closeArchiveDrawer?.();
    } catch (e) {
      /* ignore */
    }
    // Close settings panel if open (make chat visible). Not a toggle — close only.
    try {
      window.AkanaSettings?.closeSettings?.();
    } catch (e) {
      /* ignore */
    }
  }

  function goVoice() {
    // Call ONLY the canonical API that opens the full-screen voice scene. If it
    // is absent do NOTHING (mic-listen fallback was INTENTIONALLY removed —
    // it opened plain listening without the orb scene).
    const enter = window.AkanaVoice?.enterConversationMode;
    if (typeof enter !== "function") return;
    try {
      const r = enter("mnav");
      // enterConversationMode may be async → silently swallow rejection.
      if (r && typeof r.then === "function") r.catch(() => {});
    } catch (e) {
      /* ignore */
    }
  }

  function goMemory() {
    // Decision: full-page /memory (not an SPA route).
    location.href = "/memory";
  }

  function goSettings() {
    try {
      window.AkanaSettings?.openSettings?.();
    } catch (e) {
      /* ignore */
    }
  }

  // ── Tab definitions ──────────────────────────────────────────────────────
  // SVG icons are drawn with currentColor → active/inactive colour comes from CSS.
  const TABS = [
    {
      key: "sohbet",
      label: _t("nav.tab_chat"),
      action: goChat,
      ariaLabel: _t("nav.tab_chat_aria"),
      icon:
        '<svg class="mnav-ico" viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
        '<path d="M12 3c4.97 0 9 3.13 9 7s-4.03 7-9 7c-.6 0-1.18-.05-1.74-.13L5 21l1.4-3.5C4.5 16.2 3 14.3 3 12c0-3.87 4.03-7 9-7z" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/>' +
        "</svg>",
    },
    {
      key: "ses",
      label: _t("nav.tab_voice"),
      action: goVoice,
      ariaLabel: _t("nav.tab_voice_aria"),
      icon:
        '<svg class="mnav-ico" viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
        '<rect x="9" y="2.5" width="6" height="11" rx="3" fill="currentColor"/>' +
        '<path d="M5 11a7 7 0 0014 0M12 18v3M9 21h6" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>' +
        "</svg>",
    },
    {
      key: "hafiza",
      label: _t("nav.tab_memory"),
      action: goMemory,
      ariaLabel: _t("nav.tab_memory_aria"),
      icon:
        '<svg class="mnav-ico" viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
        '<path d="M12 4c-2.2 0-4 1.6-4 3.6 0 .5.1 1 .3 1.4C7 9.7 6 11 6 12.6 6 15 8 17 10.5 17H12V4z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>' +
        '<path d="M12 4c2.2 0 4 1.6 4 3.6 0 .5-.1 1-.3 1.4C17 9.7 18 11 18 12.6 18 15 16 17 13.5 17H12V4z" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/>' +
        "</svg>",
    },
    {
      key: "ayarlar",
      label: _t("nav.tab_settings"),
      action: goSettings,
      ariaLabel: _t("nav.tab_settings_aria"),
      icon:
        '<svg class="mnav-ico" viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
        '<circle cx="12" cy="12" r="3" stroke="currentColor" stroke-width="1.7"/>' +
        '<path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 11-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 11-2.83-2.83l.06-.06A1.65 1.65 0 004.6 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 112.83-2.83l.06.06A1.65 1.65 0 009 4.6a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 112.83 2.83l-.06.06A1.65 1.65 0 0019.4 9c.14.31.22.65.22 1z" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/>' +
        "</svg>",
    },
  ];

  let navEl = null;
  /** @type {Map<string, HTMLButtonElement>} */
  const tabBtns = new Map();

  function setActive(key) {
    tabBtns.forEach((btn, k) => {
      const on = k === key;
      btn.classList.toggle("is-active", on);
      btn.setAttribute("aria-current", on ? "page" : "false");
    });
  }

  function buildNav() {
    // Prefer the existing placeholder (<nav id="mnav" hidden> in index.html);
    // create one if absent. Guard against double-init (defer + possible re-call).
    if (navEl) return;
    let el = document.getElementById("mnav");
    if (!el) {
      el = document.createElement("nav");
      el.id = "mnav";
      // Start with `hidden` — visibility is managed entirely by aurora-mobile.css @media
      // (desktop = UA [hidden]; mobile = @media display:grid override).
      el.hidden = true;
      document.body.appendChild(el);
    }
    el.className = "mnav";
    el.setAttribute("aria-label", _t("nav.mobile_aria"));
    // NOTE: `hidden` attribute is NOT removed — on desktop UA [hidden]→display:none hides it;
    // aurora-mobile.css @media overrides with `body .mnav{display:grid}` only on
    // mobile/standalone (visibility gate without adding a CSS rule to the base layer).
    el.innerHTML = "";

    for (const t of TABS) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "mnav-tab";
      btn.dataset.mnav = t.key;
      btn.setAttribute("aria-label", t.ariaLabel);
      btn.setAttribute("aria-current", "false");
      btn.innerHTML =
        '<span class="mnav-ind" aria-hidden="true"></span>' +
        t.icon +
        '<span class="mnav-label">' +
        t.label +
        "</span>";
      btn.addEventListener("click", () => {
        setActive(t.key);
        try {
          t.action();
        } catch (e) {
          /* action errors must not break the strip */
        }
      });
      tabBtns.set(t.key, btn);
      el.appendChild(btn);
    }

    navEl = el;
    setActive("sohbet"); // default view
  }

  // ── Bus subscriptions: mirror active state (if available) ────────────────
  function wireBus() {
    const bus = window.AkanaBus;
    if (!bus || typeof bus.on !== "function") return;
    // Return to Chat tab when exiting voice mode.
    bus.on("voice:mode:exit", () => setActive("sohbet"));
    // Conversation change = chat-context return signal.
    bus.on("chat:conversation:changed", () => setActive("sohbet"));
  }

  function start() {
    syncDeviceClasses();
    buildNav();
    wireBus();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
