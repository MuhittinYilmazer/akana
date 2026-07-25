/**
 * Akana — desktop notifications for work that finishes while you're not looking.
 *
 * Akana can now do real background work (`background_run`, scheduled turns): the result
 * is injected into its chat by itself. Without this module that result is silent unless
 * the tab happens to be open on that exact conversation — so the smooth half of the
 * feature is telling the user it landed.
 *
 * Fires ONLY when the user could not have seen it anyway: the tab is hidden, or the
 * result belongs to a conversation other than the displayed one. A result you are
 * watching live is never announced twice.
 *
 * Permission: browsers require a USER GESTURE to ask. We never prompt on load; the ask is
 * deferred to the first click/keypress in the app, and only while notifications are armed
 * (localStorage `akana.notifications`, on by default — per-browser, because the OS-level
 * permission is per-browser too). A denial is final and never re-asked.
 */
(() => {
  const LS_KEY = "akana.notifications";
  const _t = (k, p) => window.AkanaI18n?.t(k, p) ?? k;

  const supported = () => typeof window !== "undefined" && "Notification" in window;

  function enabled() {
    try {
      return localStorage.getItem(LS_KEY) !== "0"; // armed unless explicitly turned off
    } catch {
      return true; // storage unavailable → keep the feature usable
    }
  }

  function setEnabled(on) {
    try {
      localStorage.setItem(LS_KEY, on ? "1" : "0");
    } catch {
      /* storage unavailable — in-memory only for this page */
    }
    if (on) void ensurePermission();
  }

  //: One-shot: a second requestPermission() while the first is pending throws in some
  //: engines, and re-asking after a denial is both futile and hostile.
  let _asked = false;

  async function ensurePermission() {
    if (!supported() || !enabled() || _asked) return Notification.permission;
    if (Notification.permission !== "default") return Notification.permission;
    _asked = true;
    try {
      return await Notification.requestPermission();
    } catch {
      return Notification.permission;
    }
  }

  // The gesture hook: the FIRST interaction arms permission (browsers reject a
  // permission request that is not gesture-bound). Passive + once — zero cost afterwards.
  function wireGestureArm() {
    if (!supported()) return;
    const arm = () => {
      void ensurePermission();
    };
    for (const type of ["pointerdown", "keydown"]) {
      window.addEventListener(type, arm, { once: true, passive: true });
    }
  }

  /** Human title for a conversation id, from the archive list the sidebar already holds. */
  function convTitle(convId) {
    try {
      const items = window.AkanaChat?.getChatArchiveItems?.() || [];
      const hit = items.find((c) => String(c.id) === String(convId));
      const title = (hit && hit.title ? String(hit.title) : "").trim();
      return title || _t("notify.untitled_chat");
    } catch {
      return _t("notify.untitled_chat");
    }
  }

  /**
   * Announce a completed turn. `isCurrent` = it landed in the conversation on screen.
   * Silent when the user is already looking at it (visible tab + current conversation).
   */
  function onTurnCompleted(convId, evt, opts) {
    if (!supported() || !enabled()) return false;
    if (Notification.permission !== "granted") return false;
    const isCurrent = Boolean(opts && opts.isCurrent);
    const hidden = typeof document !== "undefined" && document.hidden;
    if (isCurrent && !hidden) return false; // watching it happen — nothing to announce
    // Only announce work the user did NOT type: a background job / scheduled fire. A turn
    // the user themselves sent is theirs to follow (they know it is running).
    const src = String((evt && (evt.source || evt.kind)) || "").toLowerCase();
    if (src === "user") return false;
    let n;
    try {
      n = new Notification(_t("notify.done_title", { chat: convTitle(convId) }), {
        body: _t("notify.done_body"),
        tag: `akana-conv-${convId}`, // a newer result REPLACES the old one per chat
        renotify: false,
        silent: false,
      });
    } catch {
      return false; // some engines throw when constructing without a service worker
    }
    n.onclick = () => {
      try {
        window.focus();
        if (convId) window.AkanaChat?.switchChatConversation?.(String(convId));
      } catch {
        /* ignore */
      }
      try {
        n.close();
      } catch {
        /* ignore */
      }
    };
    return true;
  }

  function init() {
    wireGestureArm();
  }

  window.AkanaNotify = {
    init,
    onTurnCompleted,
    setEnabled,
    isEnabled: enabled,
    permission: () => (supported() ? Notification.permission : "unsupported"),
    ensurePermission,
  };

  if (typeof document !== "undefined" && document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
