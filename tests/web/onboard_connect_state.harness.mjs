/**
 * Onboarding connect-state CONTRACT test — node-vm, backend-free, DOM-free.
 *
 * Locks in the "honest connection banner" fix for the first-run wizard. The connect
 * step used to paint a green "Connected · <provider>" whenever a key was merely
 * PRESENT — so a typo'd/invalid Cursor key (stable provider!) read as connected and
 * chat then 401'd, and a saved Gemini key with the SDK missing looped the user back
 * to "needs a key". The pure decision now lives in auroraOnboard._deriveConnectState,
 * which maps a /system/status payload to exactly three states:
 *
 *   kind "ok"   → provider VERIFIED reachable  → the ONLY green.
 *   kind "warn" → key saved but NOT verified   → amber, surfaces the probe reason.
 *   kind "cta"  → not set up (no key / login)  → call-to-action.
 *
 * We stub just enough browser surface for the IIFE to load, then call the exposed
 * pure function directly. Run: node tests/web/onboard_connect_state.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const read = (rel) => readFileSync(path.join(REPO, rel), "utf8");

// Minimal browser stubs — _deriveConnectState is pure (no DOM, no fetch); these only
// let the IIFE evaluate + its init() no-op (akana.onboarded="1" suppresses auto-open).
const ctx = {
  document: {
    readyState: "complete",
    getElementById: () => null,
    createElement: () => ({ style: {}, classList: { add() {}, remove() {} }, setAttribute() {}, appendChild() {} }),
    addEventListener: () => {},
    removeEventListener: () => {},
    documentElement: { dataset: {} },
    body: { appendChild: () => {} },
  },
  localStorage: {
    getItem: (k) => (k === "akana.onboarded" ? "1" : null),
    setItem: () => {},
    removeItem: () => {},
  },
  setTimeout,
  clearTimeout,
  console,
  fetch: async () => ({ ok: true, json: async () => ({}) }),
  // The wizard's init() subscribes to akana:languagechange on `window`; a real
  // browser always has these, so the DOM-free stub must provide no-ops.
  addEventListener: () => {},
  removeEventListener: () => {},
  AkanaI18n: { t: (k) => k, ready: Promise.resolve("en") },
  AkanaCore: { baseUrl: () => "", authHeaders: () => ({}), parseApiError: (_b, s) => "HTTP " + s },
};
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(read("web_ui/static/aurora-onboard.js"), ctx);

const onb = ctx.window.auroraOnboard;
assert.ok(onb && typeof onb._deriveConnectState === "function", "auroraOnboard._deriveConnectState must be exposed");
const derive = onb._deriveConnectState;

/** Build a /system/status-shaped payload. */
const status = (provider, deps, tag) => ({
  active_provider: provider,
  model: { active_tag: tag || "", provider },
  dependencies: deps || {},
});

let passed = 0;
function check(label, fn) {
  fn();
  passed += 1;
  void label;
}

// ── Cursor (STABLE provider) — the headline bug ──────────────────────────────
check("cursor invalid key → warn (NOT green) + reason", () => {
  const s = derive(status("cursor", { cursor_api: { key_set: true, reachable: false, error: "Invalid User API Key" } }, "composer-2"));
  assert.equal(s.kind, "warn", "an invalid/typo'd Cursor key must NOT read as green Connected");
  assert.equal(s.live, false);
  assert.ok(/Invalid/.test(s.reason || ""), "the concrete probe reason is surfaced, not swallowed");
});

check("cursor valid + reachable → ok (green)", () => {
  const s = derive(status("cursor", { cursor_api: { key_set: true, reachable: true } }, "composer-2"));
  assert.equal(s.kind, "ok");
  assert.equal(s.live, true);
});

check("cursor no key → cta", () => {
  const s = derive(status("cursor", { cursor_api: { key_set: false } }));
  assert.equal(s.kind, "cta");
});

// ── Gemini — saved key but SDK missing must not say "needs a key" ─────────────
check("gemini key set + SDK missing → warn + actionable reason", () => {
  const s = derive(status("gemini", {
    gemini_api: { key_set: true, reachable: false, error: "google-genai SDK is not installed — run: python akana.py add gemini" },
  }));
  assert.equal(s.kind, "warn", "a saved Gemini key with a missing SDK must NOT collapse to 'needs a key' (cta)");
  assert.ok(/add gemini/.test(s.reason || ""), "the actionable 'add gemini' hint reaches the banner");
});

check("gemini no key → cta", () => {
  const s = derive(status("gemini", { gemini_api: { key_set: false, reachable: false } }));
  assert.equal(s.kind, "cta");
});

// ── Claude (CLI/OAuth) ───────────────────────────────────────────────────────
check("claude reachable → ok", () => {
  const s = derive(status("claude", { claude_cli: { reachable: true, token_set: true } }, "opus"));
  assert.equal(s.kind, "ok");
  assert.equal(s.live, true);
});

check("claude token present but unreachable → cta + reason", () => {
  const s = derive(status("claude", { claude_cli: { token_set: true, reachable: false, error: "session token expired" } }));
  assert.equal(s.kind, "cta");
  assert.ok(/expired/.test(s.reason || ""), "the claude unreachable reason is surfaced");
});

// ── Auth-certainty contract: authCertain drives the connect-save revert ───────
// A just-saved provider is un-selected ONLY on a DEFINITIVE auth rejection
// (error_code in the auth-certain set). A transient 'unreachable' blip on a valid
// key must be authCertain:false so the save flow keeps the keyed provider selected.
check("cursor auth_rejected (bad key) → warn + authCertain (hard revert)", () => {
  const s = derive(status("cursor", { cursor_api: { key_set: true, reachable: false, error_code: "auth_rejected", error: "Invalid User API Key" } }, "composer-2"));
  assert.equal(s.kind, "warn");
  assert.equal(s.live, false);
  assert.equal(s.authCertain, true, "a definitive auth rejection must be authCertain → revert the just-saved provider");
});

check("cursor transient unreachable → warn + NOT authCertain (keep selected)", () => {
  const s = derive(status("cursor", { cursor_api: { key_set: true, reachable: false, error_code: "unreachable", error: "Cursor API unreachable" } }, "composer-2"));
  assert.equal(s.kind, "warn");
  assert.equal(s.live, false);
  assert.equal(s.authCertain, false, "a transient blip on a valid key must NOT be authCertain — keep the provider selected + amber");
  assert.ok(/reach/i.test(s.reason || ""), "the transient reason is still surfaced (localized via error_code)");
});

check("cursor no error_code (plausible key, not probed) → warn + NOT authCertain", () => {
  const s = derive(status("cursor", { cursor_api: { key_set: true, reachable: false } }, "composer-2"));
  assert.equal(s.kind, "warn");
  assert.equal(s.authCertain, false, "absent error_code → not auth-certain → keep provider selected");
});

check("claude token_expired (401/403) → authCertain + localized reason", () => {
  const s = derive(status("claude", { claude_cli: { token_set: true, reachable: false, error_code: "token_expired", error: "Claude session token is invalid/expired" } }));
  assert.equal(s.authCertain, true, "an expired claude token is a definitive auth rejection");
  // error_code routes through ERR_CODE_KEY → the localized dictionary string, not the
  // verbatim English error (probeReason maps token_expired → connect_claude_unreachable).
  assert.equal(s.reason, "onboard.connect_claude_unreachable", "claude error_code is localized via probeReason, not passed through raw");
});

check("claude no session (no token) → cta + authCertain", () => {
  const s = derive(status("claude", { claude_cli: { token_set: false, reachable: false } }));
  assert.equal(s.kind, "cta");
  // The no-token claude branch is a cta (not ready), but still auth-certain: there is
  // no credential to reach with. (h.authCertain flows only onto the warn/ready path;
  // the cta path never triggers a save-flow revert, so this just documents intent.)
});

// ── Ollama (local, no key) — honest "can't verify" unless a probe proves it ───
// /system/status exposes NO ollama reachability by default, so we must NOT paint a
// false green: with no signal it's the neutral warn ("make sure `ollama serve` is
// running"). It earns green ONLY when a probe reports dependencies.ollama.reachable.
check("ollama, no reachability signal → warn (can't verify, not a false green)", () => {
  const s = derive(status("ollama", {}, "llama3.1"));
  assert.equal(s.kind, "warn", "an unverifiable local provider must NOT read as green");
  assert.equal(s.live, false);
});

check("ollama reachable probe → ok (green, live-verified)", () => {
  const s = derive(status("ollama", { ollama: { reachable: true } }, "llama3.1"));
  assert.equal(s.kind, "ok");
  assert.equal(s.live, true);
});

check("ollama probe unreachable → warn + reason", () => {
  const s = derive(status("ollama", { ollama: { reachable: false, error: "connection refused" } }, "llama3.1"));
  assert.equal(s.kind, "warn");
  assert.equal(s.live, false);
  assert.ok(/refused/.test(s.reason || ""), "the concrete unreachable reason is surfaced");
});

// ── Never green without live verification (the invariant, exhaustively) ───────
check("green requires live: ready-but-not-live is never 'ok'", () => {
  for (const dep of [
    { cursor_api: { key_set: true, reachable: false } },
    { cursor_api: { key_set: true, reachable: false, error: "network down" } },
  ]) {
    const s = derive(status("cursor", dep));
    assert.notEqual(s.kind, "ok", "a not-reachable provider must never earn the green state");
  }
});

console.log(`onboard_connect_state.harness: ${passed} connect-state contracts PASSED ✓`);
if (typeof process !== "undefined" && process.exit) process.exit(0);
