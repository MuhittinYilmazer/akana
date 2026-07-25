/**
 * Desktop notifications for background results — node-vm + fake DOM, backend-free.
 *
 * Background work (`background_run`, scheduled turns) posts its result into a chat by
 * itself. If the user is not looking at that chat, the result is silent — which defeats
 * the point (walk away, get told when it's done). This module announces it to the OS.
 *
 * The wire contract (verified against the server): every `turn_completed` carries
 * `{type, conversation_id, status, source}` where `source` is "user" for the reply the
 * user is waiting for (chat_detached) and "background" for a scheduled fire / a
 * background_run result (conversation_events.broadcast_turn_completed).
 *
 * Contract locked here (akana-notify.js):
 *   1. SILENT when the user is already watching: current conversation + visible tab,
 *   2. FIRES when the tab is hidden, or when the result belongs to another conversation,
 *   3. never fires without permission, and never when the user turned it off,
 *   4. never fires for the user's OWN turn — including an UNSTAMPED event, which must
 *      fail QUIET rather than spam (an earlier version filtered on a field the server
 *      never sent, so every hidden-tab reply popped a notification),
 *   5. never announces a FAILED background turn as finished work,
 *   6. permission is asked only IN CONTEXT (after a real background result landed) and
 *      only on a user gesture, once — never on page load,
 *   7. clicking the notification focuses the window and opens that conversation,
 *   8. repeats for one chat REPLACE each other (tag) instead of stacking.
 *
 * Run: node tests/web/notify_background_result.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const SRC = readFileSync(path.join(REPO, "web_ui/static/akana-notify.js"), "utf8");

let passed = 0;
const check = (label, fn) => { fn(); passed += 1; void label; };

/** Build a fresh sandbox with controllable permission / visibility / storage. */
function boot({ permission = "granted", hidden = false, stored = null, switchSpy = [] } = {}) {
  const shown = [];
  const gestureListeners = {};
  let requested = 0;

  class FakeNotification {
    constructor(title, opts) {
      this.title = title;
      Object.assign(this, opts || {});
      this.onclick = null;
      this.closed = false;
      shown.push(this);
    }
    close() { this.closed = true; }
    static permission = permission;
    static async requestPermission() { requested += 1; return "granted"; }
  }

  const store = { value: stored };
  const focus = [];
  const ctx = {
    window: {
      Notification: FakeNotification,
      AkanaI18n: { t: (k, p) => (p ? `${k}:${JSON.stringify(p)}` : k) },
      AkanaChat: {
        getChatArchiveItems: () => [{ id: "c1", title: "Rapor sohbeti" }],
        switchChatConversation: (id) => switchSpy.push(id),
      },
      addEventListener: (type, fn) => { gestureListeners[type] = fn; },
      focus: () => focus.push(1),
    },
    document: { readyState: "complete", hidden, addEventListener: () => {} },
    localStorage: {
      getItem: () => store.value,
      setItem: (_k, v) => { store.value = v; },
    },
    console,
    // In a browser `Notification` and `window.Notification` are the SAME global; the
    // module reads the bare name, so the sandbox must expose it at top level too.
    Notification: FakeNotification,
  };
  ctx.window.window = ctx.window;
  ctx.window.document = ctx.document;
  ctx.window.localStorage = ctx.localStorage;
  vm.runInNewContext(SRC, ctx);
  return {
    N: ctx.window.AkanaNotify,
    shown,
    focus,
    switchSpy,
    fireGesture: (type = "pointerdown") => gestureListeners[type] && gestureListeners[type](),
    requested: () => requested,
    stored: () => store.value,
  };
}

//: The real server payload for a finished background job.
const BG = { type: "turn_completed", status: "ok", source: "background" };

// ── 1. silent while the user is watching it happen ──────────────────────────
{
  const r = boot({ hidden: false });
  const fired = r.N.onTurnCompleted("c1", BG, { isCurrent: true });
  check("no notification for the chat on screen in a visible tab", () => {
    assert.equal(fired, false);
    assert.equal(r.shown.length, 0);
  });
}

// ── 2. fires when the user could NOT have seen it ───────────────────────────
{
  const r = boot({ hidden: true });
  check("fires when the tab is hidden (even for the current chat)", () => {
    assert.equal(r.N.onTurnCompleted("c1", BG, { isCurrent: true }), true);
    assert.equal(r.shown.length, 1);
  });
  check("title carries the chat name, body explains the result landed", () => {
    assert.match(r.shown[0].title, /Rapor sohbeti/);
    assert.equal(r.shown[0].body, "notify.done_body");
  });
}
{
  const r = boot({ hidden: false });
  check("fires for a result in ANOTHER conversation", () =>
    assert.equal(r.N.onTurnCompleted("c9", BG, { isCurrent: false }), true));
}

// ── 3. permission / opt-out gates ───────────────────────────────────────────
{
  const r = boot({ permission: "denied", hidden: true });
  check("never fires without permission", () => {
    assert.equal(r.N.onTurnCompleted("c1", BG, { isCurrent: false }), false);
    assert.equal(r.shown.length, 0);
  });
}
{
  const r = boot({ hidden: true, stored: "0" }); // user turned notifications off
  check("respects the user's off switch", () => {
    assert.equal(r.N.isEnabled(), false);
    assert.equal(r.N.onTurnCompleted("c1", BG, { isCurrent: false }), false);
  });
}

// ── 4. the user's own turn is never announced ───────────────────────────────
{
  const r = boot({ hidden: true });
  check("a turn the user themselves sent is not announced", () =>
    assert.equal(
      r.N.onTurnCompleted("c1", { ...BG, source: "user" }, { isCurrent: false }),
      false,
    ));
  // REGRESSION: the first version filtered on a field no producer sent, so an unstamped
  // event fell through and every hidden-tab reply popped "background work finished".
  check("an UNSTAMPED event fails quiet (treated as the user's own)", () => {
    assert.equal(r.N.onTurnCompleted("c1", { type: "turn_completed" }, { isCurrent: false }), false);
    assert.equal(r.shown.length, 0);
  });
}

// ── 5. a FAILED background turn is not announced as finished work ───────────
{
  const r = boot({ hidden: true });
  check("status != ok is not announced (the engine reports the failure in-chat)", () => {
    assert.equal(
      r.N.onTurnCompleted("c1", { ...BG, status: "error" }, { isCurrent: false }),
      false,
    );
    assert.equal(r.shown.length, 0);
  });
}

// ── 6. permission is asked IN CONTEXT, gesture-bound, once ──────────────────
{
  const r = boot({ permission: "default", hidden: true });
  r.fireGesture();
  check("no prompt on load — a gesture alone must not ask", () => assert.equal(r.requested(), 0));
  // A real background result lands → now there is a REASON to ask.
  check("a background result arms the ask but shows nothing yet", () => {
    assert.equal(r.N.onTurnCompleted("c1", BG, { isCurrent: false }), false);
    assert.equal(r.requested(), 0);
  });
  r.fireGesture();
  check("the next gesture after that asks", () => assert.equal(r.requested(), 1));
  r.fireGesture("keydown");
  check("it is never asked twice", () => assert.equal(r.requested(), 1));
}
{
  const r = boot({ permission: "denied", hidden: true });
  r.N.onTurnCompleted("c1", BG, { isCurrent: false });
  r.fireGesture();
  check("a denial is final — never re-asked", () => assert.equal(r.requested(), 0));
}

// ── 7. click focuses the window and opens that conversation ─────────────────
{
  const r = boot({ hidden: true });
  r.N.onTurnCompleted("c9", BG, { isCurrent: false });
  r.shown[0].onclick();
  check("click focuses the app and switches to the chat", () => {
    assert.equal(r.focus.length, 1);
    assert.deepEqual(r.switchSpy, ["c9"]);
    assert.equal(r.shown[0].closed, true);
  });
}

// ── 8. repeats replace instead of stacking ──────────────────────────────────
{
  const r = boot({ hidden: true });
  r.N.onTurnCompleted("c1", BG, { isCurrent: false });
  r.N.onTurnCompleted("c1", BG, { isCurrent: false });
  check("both use the same per-chat tag so the OS replaces the old one", () => {
    assert.equal(r.shown[0].tag, r.shown[1].tag);
    assert.match(r.shown[0].tag, /c1/);
  });
}

// ── setEnabled round-trip ───────────────────────────────────────────────────
{
  const r = boot({ hidden: true });
  r.N.setEnabled(false);
  check("turning it off persists and silences", () => {
    assert.equal(r.stored(), "0");
    assert.equal(r.N.onTurnCompleted("c1", BG, { isCurrent: false }), false);
  });
  r.N.setEnabled(true);
  check("turning it back on restores it", () => {
    assert.equal(r.N.isEnabled(), true);
    assert.equal(r.N.onTurnCompleted("c1", BG, { isCurrent: false }), true);
  });
}

console.log(`notify_background_result.harness: ${passed} notification contracts PASSED ✓`);
if (typeof process !== "undefined" && process.exit) process.exit(0);
