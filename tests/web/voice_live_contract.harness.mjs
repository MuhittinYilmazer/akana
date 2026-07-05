/**
 * Gemini Live client contract — node-vm, NO REAL audio/WS (Phase 2).
 *
 * Exercises the PURE helpers of akana-voice-live.js (at load time it doesn't
 * call any browser API → a minimal sandbox suffices):
 *  - encodeAudioFrame: [0x01] + little-endian PCM16 bytes (one-to-one with the
 *    server frame codec `gemini_live.parse_browser_frame`; if they diverge audio
 *    is silently dropped).
 *  - floatTo16BitPCM: clamp [-1,1] + scale (same logic as capture WAV).
 *  - nextState: orb/barge-in state machine (interrupt/turn_complete → LISTENING).
 * Run: node tests/web/voice_live_contract.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const read = (rel) => readFileSync(path.join(REPO, rel), "utf8");

// ── Minimal sandbox (the pure helpers need no browser API) ─────────────
const ctx = { console };
ctx.window = ctx;
vm.createContext(ctx);
vm.runInContext(read("web_ui/static/akana-voice-live.js"), ctx);

const L = ctx.window.AkanaVoiceLive;
assert.ok(L && typeof L === "object", "AkanaVoiceLive export surface missing");

// ── 1. Frame codec: tag + little-endian PCM ───────────────────────────────
assert.equal(L.AUDIO_TAG, 0x01, "audio tag must match gemini_live.FRAME_AUDIO");
const pcm = Int16Array.from([1, -2, 256]);
const frame = L.encodeAudioFrame(pcm);
assert.equal(frame[0], 0x01, "frame must start with 0x01");
assert.equal(frame.length, pcm.byteLength + 1, "frame = tag + pcm bytes");
// little-endian: 1 → [01,00], -2 → [FE,FF], 256 → [00,01]
assert.deepEqual(
  Array.from(frame.slice(1)),
  [0x01, 0x00, 0xfe, 0xff, 0x00, 0x01],
  "PCM16 little-endian bytes must be preserved",
);

// ── 2. floatTo16BitPCM: clamp + scale ────────────────────────────────────────
const q = L.floatTo16BitPCM(Float32Array.from([0, 1, -1, 0.5, 2, -2]));
assert.equal(q[0], 0);
assert.equal(q[1], 32767); // +1 → 0x7fff
assert.equal(q[2], -32768); // -1 → -0x8000
assert.ok(Math.abs(q[3] - 16383) <= 1, "0.5 → ~16383");
assert.equal(q[4], 32767, "overflow clamp");
assert.equal(q[5], -32768, "underflow clamp");

// ── 3. State machine (including barge-in) ───────────────────────────────────────
const S = L.STATES;
assert.equal(L.nextState(S.IDLE, "connecting"), S.CONNECTING);
assert.equal(L.nextState(S.CONNECTING, "ready"), S.LISTENING);
assert.equal(L.nextState(S.LISTENING, "assistant_audio"), S.SPEAKING);
// barge-in: return to listening when the model is interrupted or the turn ends
assert.equal(L.nextState(S.SPEAKING, "interrupt"), S.LISTENING);
assert.equal(L.nextState(S.SPEAKING, "turn_complete"), S.LISTENING);
// error + stop from any state
assert.equal(L.nextState(S.SPEAKING, "error"), S.ERROR);
assert.equal(L.nextState(S.LISTENING, "stop"), S.IDLE);
// an unknown event preserves the state (no-op)
assert.equal(L.nextState(S.LISTENING, "noise"), S.LISTENING);

// ── 4. Toggle decision: three gates + user preference ────────────────────────────
const OK = { available: true, enabled: true, provider_is_gemini: true };
assert.equal(L.shouldUseLive(OK, true), true, "all gates open + toggle on → Live");
assert.equal(L.shouldUseLive(OK, false), false, "toggle off → turn-based");
assert.equal(L.liveToggleVisible(OK), true, "all gates open → toggle visible");
// if any gate is closed, NO Live + toggle hidden (regression guard)
for (const k of ["available", "enabled", "provider_is_gemini"]) {
  const cfg = { ...OK, [k]: false };
  assert.equal(L.shouldUseLive(cfg, true), false, `${k}=false → no Live`);
  assert.equal(L.liveToggleVisible(cfg), false, `${k}=false → toggle hidden`);
}
assert.equal(L.shouldUseLive(null, true), false, "no cfg → safe off");
assert.equal(L.liveToggleVisible(null), false);

// ── 5. Reconnect decision: only abnormal close + non-intentional + attempt < cap ─
const MAX = 3;
// Abnormal closes (network drop/going-away) → reconnect as long as attempts remain.
assert.equal(L.shouldReconnect(1006, false, 0, MAX), true, "1006 network drop → reconnect");
assert.equal(L.shouldReconnect(1001, false, 1, MAX), true, "1001 going-away → reconnect");
assert.equal(L.shouldReconnect(1012, false, 2, MAX), true, "1012 service-restart → reconnect");
// Intentional/normal/gated closes → NEVER reconnect (the user/server expects it).
assert.equal(L.shouldReconnect(1000, false, 0, MAX), false, "1000 normal → no reconnect");
assert.equal(L.shouldReconnect(1008, false, 0, MAX), false, "1008 auth rejection → no reconnect");
assert.equal(L.shouldReconnect(1011, false, 0, MAX), false, "1011 server gate → no reconnect");
// An intentional stop() overrides every code.
assert.equal(L.shouldReconnect(1006, true, 0, MAX), false, "intentionalStop → no reconnect");
// Stop when attempts reach the cap (3-attempt pattern: attempt>=max → false).
assert.equal(L.shouldReconnect(1006, false, MAX, MAX), false, "attempts exhausted → no reconnect");
assert.equal(L.shouldReconnect(1006, false, MAX - 1, MAX), true, "last attempt still open");

// ── 6. Exponential backoff: monotonically increasing, 0 smallest, capped ─────────
const d0 = L.reconnectDelayMs(0);
const d1 = L.reconnectDelayMs(1);
const d2 = L.reconnectDelayMs(2);
assert.ok(d0 > 0, "delay positive");
assert.ok(d0 < d1 && d1 < d2, "backoff monotonically increasing (0 smallest)");
assert.equal(d0, 500, "attempt 0 → 500ms base");
assert.equal(d1, 1000, "attempt 1 → 2× base");
// At high attempt counts it pins to the ceiling (no unbounded growth).
const dHi = L.reconnectDelayMs(99);
assert.ok(dHi <= 8000, "delay capped by ceiling (~8000ms)");
assert.equal(dHi, L.reconnectDelayMs(50), "stays constant once ceiling reached");

console.log("voice_live_contract.harness: OK");
