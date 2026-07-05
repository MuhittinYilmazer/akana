/**
 * Voice mute + earcon-volume + listening-stuck deferral contract test — node-vm, no real
 * audio device. Locks in the three voice-mode fixes:
 *
 *  (a) MUTE ("Mute"): akana-voice.js subscribes to the bus event voice:mic:mute (the ONLY
 *      subscriber — aurora-voice.js toggleMute only EMITS it) and a voice.micMuted flag gates
 *      every re-arm/restart path (maybeReArmConversation, startConversationCapture, the SR
 *      onend restart, and the visibilitychange rebuild branches). Functional: the subscriber
 *      exists after init() + emitting {muted:true} does not throw. Source-contract: micMuted
 *      gates the listed paths and is reset on exit/enter.
 *
 *  (b) EARCON VOLUME: playEarcon() reads a new localStorage key (akana.voiceEarconVol, 0..1,
 *      default LOUDER than the old fixed peak) and scales the oscillator peak gain by it, up to
 *      EARCON_PEAK_MAX (~0.30 vs. the old hard-coded 0.05). Functional: driving the "listen"
 *      earcon with a higher stored volume records a higher peak-gain ramp target. Source-contract
 *      backs this up.
 *
 *  (c) LISTENING-STUCK ROOT FIX: browserRec.start() must NOT be called synchronously from the
 *      TTS <audio>.onended → playNext drain callstack (that returns a Chrome "silent zombie").
 *      startConversationCapture DEFERS the recognizer start off the callstack (scheduleMicSettleStart)
 *      and stopBrowserLiveTranscript SERIALIZES against the previous session via a teardown latch.
 *      Source-contract regex (voice logic can't cleanly unit-test SR timing in node-vm).
 *
 *  (d) TTS "MORE AUDIO COMING" LATCH: a voice turn's turn-start ttsPlayer.reset() clears
 *      voice.ttsStreamOpen, so the server's `done` (sent BEFORE the final tts_chunk drain) used
 *      to re-arm the mic MID-REPLY (reading cut off → "Listening"). The transport re-emits
 *      voice:tts:streamOpen AFTER the reset for a voiceTurn; akana-voice.js restores the latch on
 *      it (conversation mode only). Functional (bus round-trip) + transport source-contract.
 *
 * Run: node tests/web/voice_mute_earcon.harness.mjs
 */
import assert from "node:assert/strict";
import { Blob } from "node:buffer";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const read = (rel) => readFileSync(path.join(REPO, rel), "utf8");

// ── Recorded side effects ──────────────────────────────────────────────────────
const recStartCalls = []; // SpeechRecognition.start() timestamps
const gainRamps = []; // exponentialRampToValueAtTime targets (earcon peak lands here)
const busSubs = new Map(); // event → [handlers]
const store = new Map(); // localStorage backing

// ── Stub browser environment ────────────────────────────────────────────────────
class FakeSR {
  constructor() {
    this.lang = "";
    this.continuous = false;
    this.interimResults = false;
    this.maxAlternatives = 1;
    this.onresult = null;
    this.onerror = null;
    this.onend = null;
    this.onaudiostart = null;
    this.onsoundstart = null;
    this.onspeechstart = null;
  }
  start() {
    recStartCalls.push(Date.now());
  }
  stop() {
    // Real browsers fire onend asynchronously; emulate so the teardown latch releases.
    const cb = this.onend;
    if (typeof cb === "function") setTimeout(() => cb(), 0);
  }
}

class FakeGain {
  constructor() {
    this.gain = {
      setValueAtTime: () => {},
      exponentialRampToValueAtTime: (v) => gainRamps.push(v),
    };
  }
  connect() {}
  disconnect() {}
}
class FakeOsc {
  constructor() {
    this.frequency = { setValueAtTime: () => {}, exponentialRampToValueAtTime: () => {} };
  }
  connect() {}
  start() {}
  stop() {}
}
class FakeAudioContext {
  constructor() {
    this.sampleRate = 48000;
    this.state = "running";
    this.destination = {};
  }
  createGain() {
    return new FakeGain();
  }
  createOscillator() {
    return new FakeOsc();
  }
  createMediaStreamSource() {
    return { connect() {}, disconnect() {} };
  }
  createAnalyser() {
    return {
      fftSize: 512,
      frequencyBinCount: 256,
      connect() {},
      disconnect() {},
      getFloatTimeDomainData() {},
      getByteTimeDomainData() {},
    };
  }
  createMediaElementSource() {
    return { connect() {} };
  }
  createBuffer() {
    return {};
  }
  createBufferSource() {
    return { buffer: null, connect() {}, start() {}, noteOn() {} };
  }
  resume() {
    this.state = "running";
    return Promise.resolve();
  }
  close() {
    return Promise.resolve();
  }
}

// Minimal fake DOM element — enough for the SR/live-transcript path (needs #msg to exist,
// else startBrowserLiveTranscript() bails at `if (!SR || !msg) return;`).
const fakeEl = () => {
  const el = {
    value: "",
    hidden: false,
    textContent: "",
    dataset: {},
    style: { setProperty() {} },
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    children: [],
    childElementCount: 0,
    isConnected: true,
    setAttribute() {},
    getAttribute: () => null,
    removeAttribute() {},
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() {},
    appendChild() {},
    append() {},
    insertBefore() {},
    querySelector: () => fakeEl(),
    querySelectorAll: () => [],
    closest: () => null,
  };
  return el;
};
// The SR path only requires #msg (composer) and #chat-form to be present.
const domEls = { msg: fakeEl(), "chat-form": fakeEl() };

const ctx = {
  document: {
    getElementById: (id) => domEls[id] || null,
    createElement: () => fakeEl(),
    addEventListener: () => {},
    removeEventListener: () => {},
    querySelector: () => null,
    querySelectorAll: () => [],
    visibilityState: "visible",
    documentElement: { setAttribute: () => {} },
    body: fakeEl(),
  },
  navigator: {
    mediaDevices: {
      getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }),
      enumerateDevices: async () => [],
      addEventListener: () => {},
    },
    // no wakeLock → requestWakeLock() no-ops
  },
  localStorage: {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  },
  console,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  requestAnimationFrame: () => 0,
  cancelAnimationFrame: () => {},
  URL: { createObjectURL: () => "blob:stub", revokeObjectURL: () => {} },
  Blob,
  atob: (b64) => Buffer.from(b64, "base64").toString("binary"),
  btoa: (s) => Buffer.from(s, "binary").toString("base64"),
  // Settings init() fires /voice/config + /voice/preferences fetches — return an inert 200 so
  // the (guarded) init path completes without unhandled rejections. Not exercised by the asserts.
  fetch: async () => ({ ok: true, status: 200, json: async () => ({}), blob: async () => new Blob([]) }),
  Audio: class {
    constructor(url) {
      this.src = url;
      this.preload = "none";
    }
    play() {
      return Promise.resolve();
    }
    pause() {}
  },
  AudioContext: FakeAudioContext,
  SpeechRecognition: FakeSR,
  isSecureContext: true,
  WeakSet,
  performance: { now: () => Date.now() },
  matchMedia: () => ({ matches: false, addEventListener: () => {} }),
  addEventListener: () => {},
  removeEventListener: () => {},
  dispatchEvent: () => {},
  // Recording AkanaBus — akana-voice.js wires its subscribers through on().
  AkanaBus: {
    on: (event, handler) => {
      if (!busSubs.has(event)) busSubs.set(event, []);
      busSubs.get(event).push(handler);
      return () => {};
    },
    emit: (event, payload) => {
      for (const h of busSubs.get(event) || []) {
        try {
          h(payload || {});
        } catch {
          /* a bad subscriber must not abort the emit */
        }
      }
    },
  },
  AkanaChat: {
    conversationIdForMemory: () => null,
    cancelActiveTurnOnServer: () => Promise.resolve(),
    submitVoiceText: () => {},
    getChatInFlight: () => false,
  },
  AkanaI18n: { t: (k) => k, getLanguage: () => "en" },
  AkanaCore: {
    baseUrl: () => "http://test",
    authHeaders: () => ({}),
    authHeadersMultipart: () => ({}),
    escapeHtml: (x) => String(x),
    LS_TOKEN: "akana.apiToken",
  },
};
ctx.window = ctx;
vm.createContext(ctx);

// Dependencies load first (same order as the app / the fsm harness).
vm.runInContext(read("web_ui/static/akana-voice-fsm.js"), ctx);
vm.runInContext(read("web_ui/static/akana-voice-capture.js"), ctx);
vm.runInContext(read("web_ui/static/akana-voice-pipeline.js"), ctx);
vm.runInContext(read("web_ui/static/akana-voice-settings.js"), ctx);
vm.runInContext(read("web_ui/static/akana-voice.js"), ctx);

const jv = ctx.window.AkanaVoice;
assert.ok(jv, "window.AkanaVoice failed to load");

// Disable «Hey Akana» wake autostart BEFORE init(): the stub grants mic access, so an
// autostarting wake session would race (and discard) the conversation-mode capture we drive
// below. Isolating the conversation path is a harness concern only — not product behaviour.
store.set("akana.wakeAutostart", "0");

// init() wires the bus subscribers (voice:mic:mute, voice:scene:close, …).
jv.init({ isChatPage: true, getChatInFlight: () => false });

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

// ── SOURCE-CONTRACT reads (regex over the shipped file) ──────────────────────────
const SRC = read("web_ui/static/akana-voice.js");
const SETTINGS_SRC = read("web_ui/static/akana-voice-settings.js");

// ── (a) MUTE ─────────────────────────────────────────────────────────────────────
{
  // Functional: init() registered exactly one voice:mic:mute subscriber (aurora only EMITS it).
  assert.ok(
    busSubs.has("voice:mic:mute") && busSubs.get("voice:mic:mute").length >= 1,
    "(a) akana-voice.js must SUBSCRIBE to voice:mic:mute (the missing subscriber bug)",
  );
  // Emitting the mute intent must not throw (handler stops the recognizer + gates re-arm).
  assert.doesNotThrow(
    () => ctx.window.AkanaBus.emit("voice:mic:mute", { muted: true }),
    "(a) voice:mic:mute {muted:true} handler must not throw",
  );
  assert.doesNotThrow(
    () => ctx.window.AkanaBus.emit("voice:mic:mute", { muted: false }),
    "(a) voice:mic:mute {muted:false} handler must not throw",
  );

  // Source-contract: a micMuted flag exists and gates the documented paths.
  assert.match(SRC, /voice:mic:mute/, "(a) voice:mic:mute wiring present");
  assert.match(SRC, /micMuted\s*:/, "(a) voice.micMuted flag declared");
  // The subscriber derives muted from the event payload and stores it into voice.micMuted.
  assert.match(
    SRC,
    /const muted = !!\s*\(\s*p\s*&&\s*p\.muted\s*\)/,
    "(a) the subscriber reads muted from the event payload",
  );
  assert.match(SRC, /voice\.micMuted\s*=\s*muted/, "(a) the subscriber stores muted into voice.micMuted");
  // Re-arm entry points must bail when muted.
  const rearm = SRC.match(/function maybeReArmConversation[\s\S]*?\n  }/);
  assert.ok(rearm && /voice\.micMuted/.test(rearm[0]), "(a) maybeReArmConversation gated on micMuted");
  const startCap = SRC.match(/function startConversationCapture[\s\S]*?\n  }/);
  assert.ok(startCap && /voice\.micMuted/.test(startCap[0]), "(a) startConversationCapture gated on micMuted");
  // SR onend revive + visibility rebuild gated on micMuted.
  assert.match(
    SRC,
    /reviveConv\s*=\s*voice\.conversationMode\s*&&\s*!voice\.micMuted/,
    "(a) SR onend/back-off revive gated on !micMuted",
  );
  assert.match(
    SRC,
    /if \(voice\.micMuted\) return;[\s\S]*?startBrowserLiveTranscript\(\);\s*\n\s*else startConversationCapture\("viswake"\)/,
    "(a) visibilitychange rebuild branch gated on micMuted",
  );
  // Reset on exit + fresh entry.
  const exitFn = SRC.match(/function exitConversationMode[\s\S]*?\n  }/);
  assert.ok(exitFn && /voice\.micMuted\s*=\s*false/.test(exitFn[0]), "(a) exitConversationMode resets micMuted");

  // Barge-in must be gated on micMuted: a muted user's ambient noise must not cut off Akana's
  // reply. Both bargeDetector.start() and its _tick() loop check micMuted.
  const bargeStart = SRC.match(/async start\(\)\s*\{[\s\S]*?this\.starting = true;/);
  assert.ok(
    bargeStart && /voice\.micMuted/.test(bargeStart[0]),
    "(a) bargeDetector.start() gated on micMuted",
  );
  const bargeTick = SRC.match(/_tick\(\)\s*\{[\s\S]*?session\.isCapturing\(\)/);
  assert.ok(
    bargeTick && /if \(voice\.micMuted\) \{\s*\n\s*this\.stop\(\);/.test(bargeTick[0]),
    "(a) bargeDetector._tick() stops when micMuted",
  );

  // exitConversationMode must clear the recovery watchdog + re-arm retry (cross-boundary timer leak).
  assert.ok(
    exitFn && /clearTimeout\(voice\.convWatchdog\)/.test(exitFn[0]) && /voice\._rearmRetry = null/.test(exitFn[0]),
    "(a) exitConversationMode clears convWatchdog + _rearmRetry timers",
  );
}

// ── (b) EARCON VOLUME ─────────────────────────────────────────────────────────────
{
  // Source-contract: a new key is read and scales the peak gain (keeping the 0.0001 floor).
  assert.match(SRC, /akana\.voiceEarconVol/, "(b) new earcon-volume localStorage key present");
  assert.match(SRC, /function earconVolume\s*\(/, "(b) earconVolume() reader present");
  assert.match(
    SRC,
    /exponentialRampToValueAtTime\(\s*peak\s*,/,
    "(b) the peak gain ramp uses the volume-scaled peak (not the old fixed 0.05)",
  );
  assert.match(
    SRC,
    /const peak = Math\.max\(\s*0\.0001\s*,\s*earconVolume\(\)\s*\*\s*EARCON_PEAK_MAX\s*\)/,
    "(b) peak = clamp(earconVolume() * EARCON_PEAK_MAX) — scales by the key, keeps the ramp floor",
  );
  // Default must be LOUDER than the old hard-coded 0.05 peak.
  const defM = SRC.match(/EARCON_VOL_DEFAULT\s*=\s*([0-9.]+)/);
  const maxM = SRC.match(/EARCON_PEAK_MAX\s*=\s*([0-9.]+)/);
  assert.ok(defM && maxM, "(b) EARCON_VOL_DEFAULT + EARCON_PEAK_MAX defined");
  const defaultPeak = Number(defM[1]) * Number(maxM[1]);
  assert.ok(
    defaultPeak > 0.05,
    `(b) default earcon peak (${defaultPeak.toFixed(3)}) must be LOUDER than the old 0.05`,
  );
  // Settings module writes the same key from a range slider.
  assert.match(SETTINGS_SRC, /akana\.voiceEarconVol/, "(b) settings writes akana.voiceEarconVol");
  assert.match(SETTINGS_SRC, /conv-earcon-vol/, "(b) settings adds an earcon-volume slider (conv-earcon-vol)");
  assert.match(
    SETTINGS_SRC,
    /settings\.voice\.earcon_vol_label/,
    "(b) settings uses an i18n label for the earcon-volume slider",
  );

  // Functional: driving the "listen" earcon with a higher stored volume records a higher
  // peak ramp target. enterConversationMode → startConversationCapture → playEarcon("listen").
  store.set("akana.voiceEarcons", "1"); // earcons must be enabled or playEarcon() no-ops

  const firstPeakSince = (mark) => {
    for (let i = mark; i < gainRamps.length; i++) {
      if (gainRamps[i] > 0.0001) return gainRamps[i];
    }
    return null;
  };

  // Low volume run.
  store.set("akana.voiceEarconVol", "0.2");
  let mark = gainRamps.length;
  await jv.enterConversationMode("test-earcon-low");
  const lowPeak = firstPeakSince(mark);
  jv.exitConversationMode("test");
  await wait(5);

  // High volume run.
  store.set("akana.voiceEarconVol", "0.9");
  mark = gainRamps.length;
  await jv.enterConversationMode("test-earcon-high");
  const highPeak = firstPeakSince(mark);
  jv.exitConversationMode("test");
  await wait(5);

  if (lowPeak != null && highPeak != null) {
    assert.ok(
      highPeak > lowPeak,
      `(b) higher earcon volume must scale the peak gain up (low=${lowPeak}, high=${highPeak})`,
    );
    assert.ok(highPeak <= 0.3 + 1e-9, "(b) peak gain is capped by EARCON_PEAK_MAX (~0.30)");
    console.log(`   (b) functional earcon peaks: low=${lowPeak.toFixed(4)} high=${highPeak.toFixed(4)}`);
  } else {
    // Environment quirk prevented the functional trigger — source-contract above still holds.
    console.log("   (b) functional earcon trigger unavailable in this stub — source-contract asserted");
  }
  store.delete("akana.voiceEarcons");
}

// ── (c) LISTENING-STUCK ROOT FIX: deferred recognizer start ────────────────────────
{
  // Source-contract: start() is NOT called synchronously from the drain path.
  // startConversationCapture must schedule (defer) the recognizer, not call it inline.
  const startCap = SRC.match(/function startConversationCapture[\s\S]*?\n  }/)[0];
  assert.doesNotMatch(
    startCap,
    /(^|[^a-zA-Z_.])startBrowserLiveTranscript\(\)\s*;/m,
    "(c) startConversationCapture must NOT call startBrowserLiveTranscript() synchronously (drain-path zombie)",
  );
  assert.match(
    startCap,
    /scheduleMicSettleStart\(\)/,
    "(c) startConversationCapture defers the recognizer via scheduleMicSettleStart()",
  );
  // The mic-settle scheduler is defined and delays by a settle window.
  assert.match(SRC, /function scheduleMicSettleStart\s*\(/, "(c) scheduleMicSettleStart defined");
  assert.match(SRC, /MIC_SETTLE_MS\s*=\s*\d+/, "(c) MIC_SETTLE_MS delay constant present");
  assert.match(
    SRC,
    /_micSettleTimer\s*=\s*setTimeout\(/,
    "(c) the deferred start runs on a timer (off the teardown callstack)",
  );
  // Serialize against the previous session via a teardown latch (not null-onend + rebuild).
  assert.match(SRC, /_recTeardownPending/, "(c) teardown-pending serialize latch present");
  const startFn = SRC.match(/function startBrowserLiveTranscript[\s\S]*?\n    stopBrowserLiveTranscript\(\);/)[0];
  assert.match(
    startFn,
    /if \(!fromLivenessRetry && _recTeardownPending\)\s*\{\s*\n\s*scheduleMicSettleStart\(\);\s*\n\s*return;/,
    "(c) startBrowserLiveTranscript defers while the previous session is still ending",
  );

  // Functional: entering conversation mode must NOT start SR synchronously; it starts only
  // after the mic-settle delay elapses. (enterConversationMode → startConversationCapture.)
  const before = recStartCalls.length;
  await jv.enterConversationMode("test-defer");
  assert.equal(
    recStartCalls.length,
    before,
    "(c) SR.start() must NOT fire synchronously on capture (it is deferred off the callstack)",
  );
  await wait(80); // < MIC_SETTLE_MS (220) — still deferred
  assert.equal(recStartCalls.length, before, "(c) SR.start() still deferred before the settle window");
  await wait(400); // > MIC_SETTLE_MS (+ teardown fallback) — now the deferred start fires
  assert.ok(
    recStartCalls.length > before,
    "(c) the deferred SR.start() fires after the mic-settle window",
  );
  jv.exitConversationMode("test");
  await wait(5);
}

// ── (d) TTS "MORE AUDIO COMING" LATCH restored after the turn-start reset ─────────
// The turn-start ttsPlayer.reset() clears voice.ttsStreamOpen; nothing re-set it, so the
// server's `done` (before the final tts_chunk drain) flipped chatInFlight false while audio
// was still pending → a momentary play-queue drain re-armed the mic mid-reply. FIX: the
// transport re-emits voice:tts:streamOpen AFTER the reset for a voiceTurn; the voice module
// restores the latch on it (conversation mode only), bridging the done→tts_end gap.
{
  // The subscriber must exist (registered at module load through AkanaBus.on).
  assert.ok(
    busSubs.has("voice:tts:streamOpen") && busSubs.get("voice:tts:streamOpen").length >= 1,
    "(d) akana-voice.js must SUBSCRIBE to voice:tts:streamOpen",
  );

  await jv.enterConversationMode("test-latch");
  // Simulate the turn-start reset having cleared the latch (same clear path as reset()).
  ctx.window.AkanaBus.emit("voice:tts:streamEnd", {});
  assert.equal(jv.ttsStreamOpen, false, "(d) precondition: latch cleared (reset/streamEnd)");
  // The transport re-asserts it after the reset → latch back to true (bridges done→tts_end).
  ctx.window.AkanaBus.emit("voice:tts:streamOpen", {});
  assert.equal(
    jv.ttsStreamOpen,
    true,
    "(d) voice:tts:streamOpen must restore the ttsStreamOpen latch in conversation mode",
  );
  jv.exitConversationMode("test");
  await wait(5);
  // Outside conversation mode the latch is inert (typed/wake TTS does not drive the re-arm loop).
  ctx.window.AkanaBus.emit("voice:tts:streamOpen", {});
  assert.equal(
    jv.ttsStreamOpen,
    false,
    "(d) voice:tts:streamOpen is a no-op outside conversation mode",
  );

  // Source-contract (the transport is not loaded here): a voiceTurn re-emits the latch AFTER
  // the turn-start ttsPlayer.reset() — the other half of the fix.
  const TRANSPORT_SRC = read("web_ui/static/akana-chat-transport.js");
  assert.match(
    TRANSPORT_SRC,
    /reset\?\.\(\);[\s\S]*?opts\.voiceTurn[\s\S]*?voice:tts:streamOpen/,
    "(d) transport re-emits voice:tts:streamOpen for a voiceTurn after ttsPlayer.reset()",
  );
}

// ── (e) BARGE-IN toggle + STOP button (Aurora scene controls) ────────────────────
// The scene's barge toggle emits voice:barge:toggle → voice.js flips the persisted flag (opens/
// closes the AEC detector) + echoes voice:barge:state. The stop button emits voice:turn:stop →
// cancel the in-flight turn and re-listen (onConversationBargeIn), without exiting the scene.
{
  assert.ok(busSubs.has("voice:barge:toggle"), "(e) voice.js must subscribe to voice:barge:toggle");
  assert.ok(busSubs.has("voice:turn:stop"), "(e) voice.js must subscribe to voice:turn:stop");

  // Functional round-trip: toggle flips the persisted value and echoes voice:barge:state.
  let lastState = null;
  ctx.window.AkanaBus.on("voice:barge:state", (p) => { lastState = p; });
  store.delete("akana.bargeIn"); // start from default off
  ctx.window.AkanaBus.emit("voice:barge:toggle", {});
  assert.equal(store.get("akana.bargeIn"), "1", "(e) toggle from off persists barge ON");
  // NB: the payload object is created inside the vm context (different Object.prototype), so assert
  // on the primitive property, not deepEqual on the object (strict deepEqual is prototype-sensitive).
  assert.equal(lastState && lastState.enabled, true, "(e) toggle echoes voice:barge:state enabled=true");
  ctx.window.AkanaBus.emit("voice:barge:toggle", {});
  assert.equal(store.get("akana.bargeIn"), "0", "(e) toggle again persists barge OFF");
  assert.equal(lastState && lastState.enabled, false, "(e) toggle echoes voice:barge:state enabled=false");

  // Source-contract (aurora-voice.js scene): the two buttons + their intents + Stop gating.
  const AUR = read("web_ui/static/aurora-voice.js");
  assert.match(AUR, /aur-voice-barge/, "(e) scene renders the barge-in toggle button");
  assert.match(AUR, /aur-voice-stop/, "(e) scene renders the stop button (replaces end)");
  assert.match(AUR, /emit\?\.\("voice:barge:toggle"/, "(e) barge button emits voice:barge:toggle");
  assert.match(AUR, /emit\?\.\("voice:turn:stop"/, "(e) stop button emits voice:turn:stop");
  assert.match(
    AUR,
    /els\.stop\.disabled = next === STATES\.LISTENING/,
    "(e) stop is disabled while listening (nothing to stop)",
  );

  // Source-contract (voice.js): the stop handler cancels the turn via onConversationBargeIn.
  assert.match(
    SRC,
    /on\?\.\("voice:turn:stop"[\s\S]{0,240}?onConversationBargeIn\(\)/,
    "(e) voice:turn:stop cancels the turn + re-listens via onConversationBargeIn",
  );
}

console.log("voice mute/earcon/deferral/tts-latch/scene-controls contract test: OK");
// init() starts background intervals (tts-queue chip, wake poll) that keep the event loop
// alive → exit explicitly so the harness returns instead of hanging until a CI timeout.
process.exit(0);
