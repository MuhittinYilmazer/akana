/**
 * Bug-blitz-3 fe-voice contract test — node-vm, no real audio device.
 * Locks in two jury-CONFIRMED voice fixes:
 *
 *  fe-voice-2  MIC-DEVICE PICKER KILLS WAKE: the Settings mic-device <select> change handler only
 *    called bridge.stopAudioGraph() — which also clears the model-wake poll + browser-SR wake
 *    fallback while keepWakeArmed leaves the FSM in WAKE_ARMED (button lit, no audio graph) — with
 *    nothing re-arming on an idle page. The fix cycles setWakeListening(false/true) like the
 *    wake-source picker so "Hey Akana" survives a device switch.
 *
 *  fe-voice-3  LIVE "STOP" WAS A NO-OP: the voice:turn:stop handler forwarded to onConversationBargeIn
 *    for any conversationMode, but Live (Gemini/OpenAI realtime) mode uses none of that turn-based
 *    machinery, so live playback kept talking. The fix adds a liveActive branch that calls the new
 *    AkanaVoiceLive.interrupt() (flush buffered PCM + reset orb).
 *
 * Run: node tests/web/blitz3_fe-voice.harness.mjs
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
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

let passed = 0;
function check(label, fn) {
  fn();
  passed += 1;
  void label;
}

// ══════════════════════════════════════════════════════════════════════════════
// fe-voice-2 — mic-device picker re-arms wake (akana-voice-settings.js)
// ══════════════════════════════════════════════════════════════════════════════
{
  const SETTINGS_SRC = read("web_ui/static/akana-voice-settings.js");

  function makeEl(value = "") {
    const handlers = {};
    return {
      value,
      textContent: "",
      disabled: false,
      checked: false,
      innerHTML: "",
      hidden: false,
      style: {},
      options: [],
      addEventListener(ev, cb) {
        (handlers[ev] ||= []).push(cb);
      },
      closest() {
        return null;
      },
      appendChild() {},
      fire(ev) {
        (handlers[ev] || []).forEach((cb) => cb());
      },
    };
  }

  const els = { "mic-device": makeEl("") };
  const backing = {};
  const localStorage = {
    getItem: (k) => (k in backing ? backing[k] : null),
    setItem: (k, v) => { backing[k] = String(v); },
    removeItem: (k) => { delete backing[k]; },
  };

  const ctx = {
    console,
    localStorage,
    navigator: {},
    setInterval: () => 0,
    setTimeout: (cb) => { if (typeof cb === "function") cb(); return 0; },
    clearTimeout: () => {},
    document: { getElementById: (id) => els[id] || null, createElement: () => makeEl() },
    fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({ tts: {}, wake: {}, stt: {} }) }),
  };
  ctx.window = ctx;
  ctx.window.AkanaCore = { baseUrl: () => "", authHeaders: () => ({}) };
  ctx.window.AkanaI18n = { t: (k) => k, getLanguage: () => "en" };
  vm.runInNewContext(SETTINGS_SRC, ctx);

  // Bridge spies: record stopAudioGraph + setWakeListening(on) calls.
  const calls = { stopAudioGraph: 0, setWake: [] };
  const bridge = {
    setTtsEnabled() {},
    getTtsEnabled: () => false,
    ttsToggle: null,
    ttsPlayer: { queue: [], playing: false },
    hooks: { isChatPage: false },
    speechLang: () => "en",
    loadVoicePreferences: () => Promise.resolve(),
    saveVoicePreferences: () => Promise.resolve(),
    setWakeListening: (on) => { calls.setWake.push(!!on); return Promise.resolve(true); },
    syncWakeButtonUi() {},
    stopAudioGraph() { calls.stopAudioGraph += 1; },
    voice: { wakeEnabled: true },
  };

  // createSettings wires the mic-device change listener synchronously.
  ctx.window.AkanaVoiceSettings.createSettings(bridge);

  const mic = els["mic-device"];

  // Case 1: wake armed → changing the device must tear down AND re-arm wake (false→true).
  mic.value = "device-xyz";
  mic.fire("change");
  await wait(5);

  check("fe-voice-2: device change persists the new deviceId", () => {
    assert.equal(localStorage.getItem("akana.micDevice"), "device-xyz");
  });
  check("fe-voice-2: device change tears down the audio graph", () => {
    assert.ok(calls.stopAudioGraph >= 1, "stopAudioGraph must be called");
  });
  check("fe-voice-2: with wake armed the picker RE-ARMS wake (setWakeListening false→true)", () => {
    // This is the regression: the old handler never touched setWakeListening, so wake stayed
    // dead (FSM in WAKE_ARMED with no audio graph / no poll) after a device switch.
    assert.deepEqual(
      calls.setWake,
      [false, true],
      `wake must be cycled off→on after the teardown (got ${JSON.stringify(calls.setWake)})`,
    );
  });

  // Case 2: wake NOT armed → no wake cycle (only the teardown).
  bridge.voice.wakeEnabled = false;
  calls.setWake.length = 0;
  const stopBefore = calls.stopAudioGraph;
  mic.value = "";
  mic.fire("change");
  await wait(5);
  check("fe-voice-2: with wake off the picker does NOT cycle wake", () => {
    assert.equal(calls.setWake.length, 0, "no setWakeListening when wake is disabled");
    assert.ok(calls.stopAudioGraph > stopBefore, "still tears the graph down");
  });

  // Source-contract: the handler cycles wake gated on wakeEnabled.
  check("fe-voice-2 (source): mic-picker handler cycles setWakeListening gated on wakeEnabled", () => {
    const fn = SETTINGS_SRC.match(/micDeviceSelect\.addEventListener\("change",[\s\S]*?\n {8}\}\);/)[0];
    assert.match(fn, /bridge\.voice[\s\S]*?wakeEnabled/, "gated on wakeEnabled");
    assert.match(fn, /setWakeListening\(false[\s\S]*?setWakeListening\(true/, "cycles off then on");
  });
}

// ══════════════════════════════════════════════════════════════════════════════
// fe-voice-3 (part A) — AkanaVoiceLive.interrupt() exists + barge semantics (akana-voice-live.js)
// ══════════════════════════════════════════════════════════════════════════════
{
  const LIVE_SRC = read("web_ui/static/akana-voice-live.js");
  const lctx = { console, window: {}, globalThis: {} };
  lctx.window = lctx;
  lctx.globalThis = lctx;
  vm.runInNewContext(LIVE_SRC, lctx);
  const L = lctx.window.AkanaVoiceLive;
  assert.ok(L, "AkanaVoiceLive failed to load");

  check("fe-voice-3: AkanaVoiceLive exposes interrupt()", () => {
    assert.equal(typeof L.interrupt, "function", "public interrupt() must exist");
  });
  check("fe-voice-3: interrupt() is a safe no-op with no active session", () => {
    assert.doesNotThrow(() => L.interrupt(), "interrupt without a session must not throw");
  });
  check("fe-voice-3: barge semantics — interrupt drives SPEAKING → LISTENING", () => {
    assert.equal(L.nextState(L.STATES.SPEAKING, "interrupt"), L.STATES.LISTENING);
  });
  check("fe-voice-3 (source): interrupt flushes playback + drives the state machine", () => {
    const fn = LIVE_SRC.match(/function interrupt\(\)[\s\S]*?\n  \}/)[0];
    assert.match(fn, /_flushPlayback\(\)/, "flushes the buffered PCM");
    assert.match(fn, /_setState\("interrupt"\)/, "resets the orb via the interrupt state");
  });

  // ── review finding 2: interrupt() must ALSO stop a reply still being streamed ──
  // The provider streams the whole turn faster than realtime and the backend ignores
  // our control frame, so the interrupted turn's tail audio keeps arriving after the
  // flush → without a latch it re-opens playback and flips the orb back to SPEAKING.
  // (Source-contract: _onServerMessage is internal to the singleton, not a public seam;
  // a true behavioural RED would need to expose it — locked here on the source instead.)
  check("fe-voice-3 (review): interrupt() latches a barge flag (drop until next turn)", () => {
    const fn = LIVE_SRC.match(/function interrupt\(\)[\s\S]*?\n  \}/)[0];
    assert.match(fn, /_interruptedUntilTurn = true/, "interrupt() must set the barge latch");
  });
  check("fe-voice-3 (review): the barge latch DROPS assistant audio before the SPEAKING flip", () => {
    const region = LIVE_SRC.match(/instanceof ArrayBuffer\)[\s\S]*?_setState\("assistant_audio"\)/);
    assert.ok(region, "the binary/assistant-audio branch must exist");
    assert.match(region[0], /if \(_interruptedUntilTurn\) return;/,
      "incoming audio must be dropped (and the SPEAKING flip skipped) while the latch is set");
  });
  check("fe-voice-3 (review): the barge latch clears at the next turn boundary (turn_complete + ready)", () => {
    const tc = LIVE_SRC.match(/case "turn_complete":[\s\S]*?break;/)[0];
    assert.match(tc, /_interruptedUntilTurn = false/, "turn_complete must clear the latch so the next turn plays");
    const rd = LIVE_SRC.match(/case "ready":[\s\S]*?break;/)[0];
    assert.match(rd, /_interruptedUntilTurn = false/, "ready (reconnect/new session) must clear the latch");
  });
}

// ══════════════════════════════════════════════════════════════════════════════
// fe-voice-3 (part B) — voice:turn:stop routes to the live barge in Live mode (akana-voice.js)
// ══════════════════════════════════════════════════════════════════════════════
{
  const busSubs = new Map();
  const store = new Map();
  const appendRows = [];

  class FakeSR {
    constructor() {
      this.lang = ""; this.continuous = false; this.interimResults = false; this.maxAlternatives = 1;
      this.onresult = this.onerror = this.onend = null;
      this.onaudiostart = this.onsoundstart = this.onspeechstart = null;
    }
    start() {}
    stop() { const cb = this.onend; if (typeof cb === "function") setTimeout(() => cb(), 0); }
  }
  class FakeGain {
    constructor() { this.gain = { value: 0, setValueAtTime: () => {}, exponentialRampToValueAtTime: () => {} }; }
    connect() {} disconnect() {}
  }
  class FakeOsc {
    constructor() { this.frequency = { setValueAtTime: () => {}, exponentialRampToValueAtTime: () => {} }; }
    connect() {} start() {} stop() {}
  }
  class FakeWorkletNode { constructor() { this.port = { onmessage: null, close() {} }; } connect() {} disconnect() {} }
  class FakeAudioContext {
    constructor() { this.sampleRate = 48000; this.state = "running"; this.destination = {}; this.audioWorklet = { addModule: async () => {} }; }
    createGain() { return new FakeGain(); }
    createOscillator() { return new FakeOsc(); }
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    createAnalyser() { return { fftSize: 512, frequencyBinCount: 256, connect() {}, disconnect() {}, getFloatTimeDomainData() {}, getByteTimeDomainData() {} }; }
    resume() { this.state = "running"; return Promise.resolve(); }
    close() { return Promise.resolve(); }
  }
  const fakeEl = () => ({
    value: "", hidden: false, textContent: "", dataset: {},
    style: { setProperty() {} },
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    children: [], childElementCount: 0, isConnected: true,
    setAttribute() {}, getAttribute: () => null, removeAttribute() {},
    addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
    appendChild() {}, append() {}, insertBefore() {},
    querySelector: () => fakeEl(), querySelectorAll: () => [], closest: () => null,
  });
  const domEls = { msg: fakeEl(), "chat-form": fakeEl() };
  const makeTrack = () => ({ stop() {}, addEventListener() {} });

  // Spy live-mode module: records interrupt() calls; start() succeeds so liveActive latches on.
  const liveCalls = { interrupt: 0, start: 0, stop: 0, setMuted: 0 };
  const FakeLive = {
    STATES: { IDLE: "idle", CONNECTING: "connecting", LISTENING: "listening", SPEAKING: "speaking", ERROR: "error" },
    shouldUseLive: () => true,
    liveToggleVisible: () => true,
    start: async () => { liveCalls.start += 1; return true; },
    stop: () => { liveCalls.stop += 1; },
    isActive: () => true,
    setMuted: () => { liveCalls.setMuted += 1; },
    interrupt: () => { liveCalls.interrupt += 1; },
  };

  const ctx = {
    document: {
      getElementById: (id) => domEls[id] || null,
      createElement: () => fakeEl(),
      addEventListener: () => {}, removeEventListener: () => {},
      querySelector: () => null, querySelectorAll: () => [],
      visibilityState: "visible",
      documentElement: { setAttribute: () => {} },
      body: fakeEl(),
    },
    navigator: {
      mediaDevices: {
        getUserMedia: async () => ({ getTracks: () => [makeTrack()] }),
        enumerateDevices: async () => [],
        addEventListener: () => {},
      },
    },
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: (k) => store.delete(k),
    },
    console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    requestAnimationFrame: () => 0, cancelAnimationFrame: () => {},
    URL: { createObjectURL: () => "blob:stub", revokeObjectURL: () => {} },
    Blob,
    atob: (b64) => Buffer.from(b64, "base64").toString("binary"),
    btoa: (s) => Buffer.from(s, "binary").toString("base64"),
    fetch: async () => ({ ok: true, status: 200, json: async () => ({}), blob: async () => new Blob([]) }),
    Audio: class {
      constructor(url) { this.src = url; this.preload = "none"; this.ended = false; this.onended = this.onerror = this.ontimeupdate = this.onplaying = null; }
      play() { return Promise.resolve(); }
      pause() {}
    },
    AudioContext: FakeAudioContext,
    AudioWorkletNode: FakeWorkletNode,
    SpeechRecognition: FakeSR,
    isSecureContext: true,
    WeakSet,
    performance: { now: () => Date.now() },
    matchMedia: () => ({ matches: false, addEventListener: () => {} }),
    addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => {},
    AkanaBus: {
      on: (event, handler) => {
        if (!busSubs.has(event)) busSubs.set(event, []);
        busSubs.get(event).push(handler);
        return () => {};
      },
      emit: (event, payload) => {
        for (const h of busSubs.get(event) || []) {
          try { h(payload || {}); } catch { /* ignore */ }
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
    AkanaVoiceLive: FakeLive,
    AkanaVoiceLiveCfg: { live: { available: true, enabled: true, provider_is_gemini: true } },
  };
  ctx.window = ctx;
  vm.createContext(ctx);

  vm.runInContext(read("web_ui/static/akana-voice-fsm.js"), ctx);
  vm.runInContext(read("web_ui/static/akana-voice-capture.js"), ctx);
  vm.runInContext(read("web_ui/static/akana-voice-pipeline.js"), ctx);
  vm.runInContext(read("web_ui/static/akana-voice-settings.js"), ctx);
  vm.runInContext(read("web_ui/static/akana-voice.js"), ctx);

  const jv = ctx.window.AkanaVoice;
  assert.ok(jv, "window.AkanaVoice failed to load");
  store.set("akana.wakeAutostart", "0");
  store.set("akana.voice.liveMode", "1"); // enable Live routing so enterConversationMode picks Live
  jv.init({ isChatPage: true, getChatInFlight: () => false, appendRow: (html) => { appendRows.push(html); return fakeEl(); } });

  // Enter Live mode → voice.liveActive latches true (FakeLive.start resolves true).
  const entered = await jv.enterConversationMode("test-live");
  assert.equal(entered, true, "enterConversationMode should enter Live mode");
  assert.equal(liveCalls.start, 1, "Live session started");
  assert.equal(jv.isConversationMode(), true, "conversation mode active");

  // Tap Stop (Aurora) → must route to the LIVE barge, not the turn-based path.
  ctx.window.AkanaBus.emit("voice:turn:stop", {});
  await wait(5);

  check("fe-voice-3: voice:turn:stop in Live mode calls AkanaVoiceLive.interrupt()", () => {
    // Regression: the old handler forwarded to onConversationBargeIn (turn-based objects Live never
    // uses) and NEVER touched the live module, so live playback kept talking.
    assert.equal(liveCalls.interrupt, 1, "the live barge must fire on Stop in Live mode");
  });

  jv.exitConversationMode("test");
  await wait(5);

  // Turn-based conversation mode → Stop must NOT hit the live path.
  liveCalls.interrupt = 0;
  const enteredTurn = await jv.enterConversationMode("test-turn");
  // Turn-based entry needs SpeechRecognition (present) + live toggle off for this leg.
  void enteredTurn;
  ctx.window.AkanaBus.emit("voice:turn:stop", {});
  await wait(5);
  check("fe-voice-3 (source): the handler has a liveActive branch before onConversationBargeIn", () => {
    const SRC = read("web_ui/static/akana-voice.js");
    const fn = SRC.match(/window\.AkanaBus\?\.on\?\.\("voice:turn:stop",[\s\S]*?\n {6}\}\);/)[0];
    assert.match(fn, /if \(voice\.liveActive\)/, "liveActive branch present");
    assert.match(fn, /AkanaVoiceLive\?\.interrupt\?\.\(\)/, "routes to the live interrupt");
  });
}

console.log(`blitz3 fe-voice contract test: ${passed} contracts PASSED, OK`);
process.exit(0);
