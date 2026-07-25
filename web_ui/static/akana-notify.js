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

  // Permission is asked IN CONTEXT, never on arrival: prompting every visitor on their
  // first click is the classic hostile pattern (and gets the site blocked by the browser).
  // Instead the ask is armed only once a real background result has actually landed —
  // i.e. the user demonstrably uses the feature — and then fires on their next gesture,
  // because browsers reject a request that is not gesture-bound.
  let _armed = false;

  function armPermissionAsk() {
    if (!supported() || _armed || _asked) return;
    if (Notification.permission !== "default") return;
    _armed = true;
    const ask = () => {
      void ensurePermission();
    };
    for (const type of ["pointerdown", "keydown"]) {
      window.addEventListener(type, ask, { once: true, passive: true });
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
    // Announce ONLY work that arrived on its own. The server stamps `source`:
    // "user" = the reply they are waiting for (never announced — otherwise every reply
    // finishing in a hidden tab pops a notification), "background" = a background_run
    // job or scheduled fire. An UNSTAMPED event is treated as the user's own: a missing
    // marker must fail quiet, not spam.
    const src = String((evt && evt.source) || "user").toLowerCase();
    if (src !== "background") return false;
    // A failed background turn is not "finished work" — the engine reports the failure
    // into the chat itself; announcing "your result is ready" would be a lie.
    const status = String((evt && evt.status) || "ok").toLowerCase();
    if (status !== "ok") return false;
    const isCurrent = Boolean(opts && opts.isCurrent);
    const hidden = typeof document !== "undefined" && document.hidden;
    if (isCurrent && !hidden) return false; // watching it happen — nothing to announce
    if (Notification.permission !== "granted") {
      // A real background result just landed but we may ask: arm the contextual prompt.
      armPermissionAsk();
      return false;
    }
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
    // Nothing to wire on load — the permission ask is armed by the first real background
    // result (see armPermissionAsk), so a user who never uses background work is never
    // prompted at all.
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
