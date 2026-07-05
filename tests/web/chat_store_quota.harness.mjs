/**
 * Chat store quota-fit contract — backend-free, node-vm.
 *
 * Root scenario (user report): "chat store save failed after aggressive trim —
 * giving up" was logged REPEATEDLY → localStorage quota full, chat cache can't
 * be saved. Root cause: aggressive trim cut the message COUNT but not the
 * message SIZE → a few huge messages (long reply/code) alone exceeded the quota
 * and dropped into "giving up".
 *
 * Fix: as a last resort persistChatStoreNow also shrinks the message TEXT
 * (geometric textCap) → it always fits. The server keeps the full text; the
 * cache is only for fast-resume.
 *
 * Run: node tests/web/chat_store_quota.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const SRC = readFileSync(path.join(REPO, "web_ui/static/akana-chat-store.js"), "utf8");

// ── Quota-limited fake localStorage: throw QuotaExceededError if value exceeds LIMIT ────
const LIMIT = 100_000; // 100KB simulated quota (real ~5MB; same ratio)
const backing = {};
const localStorage = {
  getItem: (k) => (k in backing ? backing[k] : null),
  setItem: (k, v) => {
    if (typeof v === "string" && v.length > LIMIT) {
      const e = new Error("quota exceeded");
      e.name = "QuotaExceededError";
      throw e;
    }
    backing[k] = String(v);
  },
  removeItem: (k) => {
    delete backing[k];
  },
};

const ctx = {
  console,
  localStorage,
  crypto: { randomUUID: () => "t-" + Math.random().toString(36).slice(2, 11) },
  setTimeout: () => 0, // debounce no-op; we call flushChatStore directly
  clearTimeout: () => {},
  addEventListener: () => {},
  removeEventListener: () => {},
};
ctx.window = ctx;
vm.runInNewContext(SRC, ctx);

const { createStore } = ctx.window.AkanaChatStore;
const s = createStore();

// Inject a huge conversation: 100 messages × 50KB = ~5MB (far above the quota).
const cs = s.getChatStore();
const huge = "x".repeat(50_000);
cs.threads["t1"] = {
  id: "t1",
  profile: "cursor",
  conversationId: "c1",
  title: "kocaman sohbet",
  updatedAt: 1,
  messages: Array.from({ length: 100 }, (_, i) => ({
    role: i % 2 ? "assistant" : "user",
    text: huge,
  })),
};
cs.activeByProfile["cursor"] = "t1";

// Persist: must FIT the quota via aggressive trim (NOT 'giving up').
s.flushChatStore();

const saved = localStorage.getItem("akana.chatStore.v1");
assert.ok(saved, "must be saved after aggressive trim — fits the quota, NOT 'giving up'");
assert.ok(saved.length <= LIMIT, `the saved value must be under the quota (${saved.length} <= ${LIMIT})`);

const parsed = JSON.parse(saved);
const msgs = parsed.threads["t1"]?.messages || [];
assert.ok(msgs.length >= 1, "the active thread must be preserved (at least 1 message)");
assert.ok(msgs.length <= 30, "the active thread snapshot must be capped at SNAPSHOT_MAX(30) (slim persist)");
assert.ok(
  msgs.every((m) => typeof m.text === "string" && m.text.length < 50_000),
  "huge messages must be trimmed in the cache (last-resort size cut ran)",
);

// ── SLIM PERSIST contract ───────────────────────────────────────────────────
// The server is the source of truth → a passive conversation's server-backed BODY
// is NOT written to disk (refetch on switch). Only the unapproved pending / local
// error card is kept. Also persist is NON-DESTRUCTIVE: the in-memory store is not
// pruned (the old model pruned it).
{
  const cs2 = s.getChatStore();
  for (const k of Object.keys(cs2.threads)) delete cs2.threads[k];
  cs2.threads["act"] = {
    id: "act", profile: "cursor", conversationId: "ca", title: "aktif", updatedAt: 2,
    messages: [{ kind: "user", text: "a-soru" }, { kind: "assistant", text: "a-yanit" }],
  };
  cs2.threads["pas"] = {
    id: "pas", profile: "cursor", conversationId: "cp", title: "pasif", updatedAt: 1,
    messages: [
      { kind: "user", text: "p-soru" },
      { kind: "assistant", text: "p-yanit" },
      { kind: "user", text: "p-pending", _pendingUser: true },
    ],
  };
  cs2.activeByProfile["cursor"] = "act";
  s.flushChatStore();
  const p2 = JSON.parse(localStorage.getItem("akana.chatStore.v1"));
  assert.equal(p2.threads["act"].messages.length, 2, "the active thread's last N messages (provisional snapshot) are preserved");
  assert.equal(p2.threads["pas"].messages.length, 1, "the passive thread's BODY is not mirrored, only the local-only queue");
  assert.equal(p2.threads["pas"].messages[0].text, "p-pending", "only the unapproved pending is persisted in a passive thread");
  assert.equal(cs2.threads["pas"].messages.length, 3, "persist must NOT prune the in-memory store (non-destructive)");
}

console.log("chat_store_quota.harness: OK");
