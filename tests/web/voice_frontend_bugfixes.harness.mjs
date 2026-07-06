/**
 * Voice front-end bug-fix contract test — node-vm, no real audio device.
 * Locks in ten jury-CONFIRMED voice fixes (find→verify→fix workflow):
 *
 *  1. WHISPER MIC-DENY LOOP: startConversationCapture's whisper ensureAudio().catch must LATCH
 *     the failure once (convPermErrShown) + exitConversationMode instead of spinning a
 *     microtask-only recover/re-arm loop (tab freeze + thousands of mic-denied bubbles).
 *  2. ENSUREAUDIO MIC LEAK: ensureAudioInner re-checks a supersession token after getUserMedia;
 *     a stopAudioGraph() during the mic prompt stops the just-acquired stream instead of leaking it.
 *  3. ENTER-DURING-WAKE-POST: postVoiceBlob's finally re-arms conversation mode when the epoch
 *     moved on (user entered conv mode during the POST) so the scene isn't left deaf.
 *  4. WHISPER SR-FREE: enterConversationMode does NOT block on missing SpeechRecognition in
 *     whisper STT mode (SR-free by design; Firefox + Whisper works).
 *  5. STOP DURING TRANSCRIBE: onConversationBargeIn aborts the in-flight /voice/transcribe +
 *     bumps the epoch so the stopped utterance is NOT submitted.
 *  6. POST-FINAL GRACE ON RESTART: startBrowserRecNow resets voice._srPrevFinalLen=0 so a
 *     same-instance SR restart keeps the post-final grace window.
 *  7. WAKE-FALLBACK TEARDOWN: stopSpeechWakeFallback detaches onend/onerror/onresult before
 *     stop() so a stale onend can't null the reference to a newly-built recognizer.
 *  8. TTS LANG AUTO: streamTtsParam / pipeline tts_lang / settings ttsPreferredLang resolve
 *     "auto" from the UI language (English → "en"), never the hardcoded "tr" fallback.
 *  9. SR LANG AUTO: startBrowserLiveTranscript resolves "auto" to a concrete locale for SR.lang.
 * 10. TTS ENDED RACING HIDE: resumeAfterVisible does not replay a FINISHED chunk and return; it
 *     advances/drains so ttsPlayer can't wedge playing=true forever.
 *
 * Run: node tests/web/voice_frontend_bugfixes.harness.mjs
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

// ── Recorded side effects / knobs ───────────────────────────────────────────────
const appendRows = []; // hooks.appendRow HTML strings
const busSubs = new Map();
const store = new Map();
const gumStopped = []; // MediaStreamTrack.stop() calls on acquired streams
let gumMode = "ok"; // "ok" | "reject-notallowed"
let gumResolvers = null; // when set to a fn, getUserMedia defers until released

class FakeSR {
  constructor() {
    this.lang = "";
    this.continuous = false;
    this.interimResults = false;
    this.maxAlternatives = 1;
    this.onresult = this.onerror = this.onend = null;
    this.onaudiostart = this.onsoundstart = this.onspeechstart = null;
  }
  start() {}
  stop() {
    const cb = this.onend;
    if (typeof cb === "function") setTimeout(() => cb(), 0);
  }
}

class FakeGain {
  constructor() {
    this.gain = { value: 0, setValueAtTime: () => {}, exponentialRampToValueAtTime: () => {} };
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
class FakeWorkletNode {
  constructor() {
    this.port = { onmessage: null, close() {} };
  }
  connect() {}
  disconnect() {}
}
class FakeAudioContext {
  constructor() {
    this.sampleRate = 48000;
    this.state = "running";
    this.destination = {};
    this.audioWorklet = { addModule: async () => {} };
  }
  createGain() { return new FakeGain(); }
  createOscillator() { return new FakeOsc(); }
  createMediaStreamSource() {
    // Bug 2: if audioCtx were nulled, the app must NOT reach here (else this throws in prod).
    return { connect() {}, disconnect() {} };
  }
  createAnalyser() {
    return { fftSize: 512, frequencyBinCount: 256, connect() {}, disconnect() {}, getFloatTimeDomainData() {}, getByteTimeDomainData() {} };
  }
  resume() { this.state = "running"; return Promise.resolve(); }
  close() { return Promise.resolve(); }
}
// AudioWorkletNode global (ensureAudioInner constructs one)
const AudioWorkletNode = FakeWorkletNode;

const fakeEl = () => {
  const el = {
    value: "", hidden: false, textContent: "", dataset: {},
    style: { setProperty() {} },
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    children: [], childElementCount: 0, isConnected: true,
    setAttribute() {}, getAttribute: () => null, removeAttribute() {},
    addEventListener() {}, removeEventListener() {}, dispatchEvent() {},
    appendChild() {}, append() {}, insertBefore() {},
    querySelector: () => fakeEl(), querySelectorAll: () => [], closest: () => null,
  };
  return el;
};
const domEls = { msg: fakeEl(), "chat-form": fakeEl() };

const makeTrack = () => ({ stop() { gumStopped.push(Date.now()); }, addEventListener() {} });

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
      getUserMedia: async () => {
        if (gumMode === "reject-notallowed") {
          const e = new Error("denied");
          e.name = "NotAllowedError";
          throw e;
        }
        if (gumResolvers) {
          // Defer: the test releases the promise after running stopAudioGraph().
          await new Promise((r) => { gumResolvers = r; });
        }
        return { getTracks: () => [makeTrack()] };
      },
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
    constructor(url) {
      this.src = url;
      this.preload = "none";
      this.ended = false;
      this.onended = this.onerror = this.ontimeupdate = this.onplaying = null;
    }
    play() { return Promise.resolve(); }
    pause() {}
  },
  AudioContext: FakeAudioContext,
  AudioWorkletNode,
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

store.set("akana.wakeAutostart", "0"); // isolate the conversation path (no wake race)
// hooks.appendRow records into appendRows so we can bound the mic-denied bubbles.
jv.init({ isChatPage: true, getChatInFlight: () => false, appendRow: (html) => { appendRows.push(html); return fakeEl(); } });

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const SRC = read("web_ui/static/akana-voice.js");
const CAP_SRC = read("web_ui/static/akana-voice-capture.js");
const PIPE_SRC = read("web_ui/static/akana-voice-pipeline.js");
const SETTINGS_SRC = read("web_ui/static/akana-voice-settings.js");

// ── Bug 1: WHISPER MIC-DENY does not spin a microtask loop ────────────────────────
{
  // Source-contract: the whisper ensureAudio().catch latches convPermErrShown + exits (no
  // unbounded recover/re-arm). The old code appended a bubble + recoverConvCaptureAndReArm
  // unconditionally on every rejection.
  const cap = SRC.match(/function startConversationCapture[\s\S]*?\n  }/)[0];
  assert.match(cap, /voice\.convPermErrShown/, "(1) whisper catch latches convPermErrShown");
  assert.match(cap, /exitConversationMode\(/, "(1) whisper catch exits conversation mode on the latched failure");

  // Functional: whisper STT + getUserMedia rejecting NotAllowedError → entering conv mode must
  // NOT append thousands of bubbles. Old behaviour: unbounded loop before any macrotask.
  store.set("akana.sttSource", "whisper");
  gumMode = "reject-notallowed";
  const before = appendRows.length;
  await jv.enterConversationMode("test-mic-deny");
  // Let several macrotasks elapse — the loop would have fired thousands of appendRow by now.
  await wait(60);
  const denied = appendRows.length - before;
  assert.ok(
    denied <= 1,
    `(1) mic-denied must append at MOST one bubble (got ${denied}) — no microtask re-arm loop`,
  );
  assert.equal(jv.isConversationMode(), false, "(1) a latched whisper mic failure exits conversation mode");
  // Reset knobs.
  gumMode = "ok";
  store.delete("akana.sttSource");
  jv.exitConversationMode("test");
  await wait(5);
}

// ── Bug 4: WHISPER SR-FREE entry ──────────────────────────────────────────────────
{
  assert.match(
    SRC,
    /if \(!SR && !convUsesWhisperStt\(\)\)/,
    "(4) enterConversationMode only blocks on missing SR when NOT in whisper mode",
  );

  // Functional: no SpeechRecognition + whisper STT → entry succeeds (does not err_no_sr).
  const savedSR = ctx.SpeechRecognition;
  const savedWSR = ctx.webkitSpeechRecognition;
  ctx.SpeechRecognition = undefined;
  ctx.webkitSpeechRecognition = undefined;
  store.set("akana.sttSource", "whisper");
  const before = appendRows.length;
  const ok = await jv.enterConversationMode("test-nosr-whisper");
  assert.equal(ok, true, "(4) whisper conversation mode starts without SpeechRecognition");
  assert.equal(jv.isConversationMode(), true, "(4) conversationMode set with whisper + no SR");
  // No err_no_sr bubble was appended.
  const added = appendRows.slice(before).join("");
  assert.doesNotMatch(added, /err_no_sr/, "(4) no 'no speech recognition' error bubble in whisper mode");
  jv.exitConversationMode("test");
  await wait(5);

  // And the browser-SR default STILL blocks without SR.
  store.delete("akana.sttSource"); // → browser default
  const before2 = appendRows.length;
  const ok2 = await jv.enterConversationMode("test-nosr-browser");
  assert.equal(ok2, false, "(4) browser-SR default still refuses without SpeechRecognition");
  assert.match(appendRows.slice(before2).join(""), /err_no_sr/, "(4) browser default shows err_no_sr");

  ctx.SpeechRecognition = savedSR;
  ctx.webkitSpeechRecognition = savedWSR;
}

// ── Bug 8: TTS LANG resolves "auto" from UI language (English → "en") ──────────────
{
  // Source-contract: a shared ttsLangFromSpeech helper resolves "auto"; the three sites use it.
  assert.match(SRC, /function ttsLangFromSpeech\s*\(/, "(8) ttsLangFromSpeech helper defined");
  assert.match(SRC, /return `\?tts=\$\{encodeURIComponent\(ttsLangFromSpeech\(\)\)\}`/, "(8) streamTtsParam uses the helper");
  assert.match(PIPE_SRC, /bridge\.ttsLangFromSpeech/, "(8) pipeline tts_lang uses the helper");
  assert.match(SETTINGS_SRC, /bridge\.ttsLangFromSpeech/, "(8) settings ttsPreferredLang uses the helper");
  // The raw unguarded fallback must be gone from streamTtsParam.
  assert.doesNotMatch(SRC, /\?tts=\$\{encodeURIComponent\(speechLang\(\)\.startsWith\("en"\)/, "(8) old unguarded streamTtsParam fallback removed");

  // Functional: UI language English + speech-lang "auto" → streamTtsParam yields tts=en.
  store.set("akana.speechLang", "auto");
  // Force conversation mode so streamTtsParam always emits (independent of ttsEnabled).
  await jv.enterConversationMode("test-tts-auto");
  const param = jv.streamTtsParam();
  assert.equal(param, "?tts=en", `(8) English + auto STT must yield tts=en (got ${param}) — not the Turkish fallback`);
  jv.exitConversationMode("test");
  await wait(5);
  store.delete("akana.speechLang");
}

// ── Bug 9: SR.lang resolves "auto" to a concrete locale ────────────────────────────
{
  const build = SRC.match(/voice\._srPrevFinalLen = 0;[\s\S]*?browserRec\.lang =[^\n]*/)[0];
  assert.match(build, /_srLang !== "auto"/, "(9) startBrowserLiveTranscript resolves 'auto' before SR.lang");
  assert.doesNotMatch(build, /browserRec\.lang = speechLang\(\);/, "(9) the old unguarded browserRec.lang = speechLang() is gone");
}

// ── Bug 6: post-final grace survives a same-instance SR restart ─────────────────────
{
  const fn = SRC.match(/function startBrowserRecNow[\s\S]*?\n  }/)[0];
  assert.match(fn, /voice\._srPrevFinalLen = 0;/, "(6) startBrowserRecNow resets _srPrevFinalLen so a restart keeps the post-final grace");
}

// ── Bug 7: wake-fallback teardown detaches onend before stop ────────────────────────
{
  const fn = SRC.match(/function stopSpeechWakeFallback[\s\S]*?\n  }/)[0];
  assert.match(fn, /const rec = speechWakeRec;\s*\n\s*speechWakeRec = null;/, "(7) captures instance + nulls the module var before stop()");
  assert.match(fn, /rec\.onend = null;/, "(7) detaches onend so a stale async onend can't null the NEW recognizer ref");
  assert.match(fn, /rec\.onresult = null;/, "(7) detaches onresult");
  // The stale-onend-nulls-live-var pattern is gone (no `speechWakeRec.stop()` then `speechWakeRec = null` after).
  assert.doesNotMatch(fn, /speechWakeRec\.stop\(\);/, "(7) does not call stop() on the still-attached module var");
}

// ── Bug 2: ensureAudioInner supersession token releases a leaked stream ─────────────
{
  // Source-contract.
  assert.match(CAP_SRC, /_audioStartToken/, "(2) supersession token present in capture module");
  assert.match(CAP_SRC, /const superseded = \(\) =>/, "(2) ensureAudioInner defines a superseded() check");
  assert.match(CAP_SRC, /if \(superseded\(\) \|\| !bridge\.voice\.audioCtx\)/, "(2) re-checks supersession/audioCtx after getUserMedia");
  assert.match(CAP_SRC, /acquiredStream\.getTracks\(\)\.forEach\(\(t\) => t\.stop\(\)\)/, "(2) stops the acquired stream on supersession");
  assert.match(CAP_SRC, /function stopAudioGraph[\s\S]*?_audioStartToken = \(bridge\.voice\._audioStartToken \|\| 0\) \+ 1/, "(2) stopAudioGraph bumps the token");
}

// ── Bug 5: Stop during transcribe aborts fetch + bumps epoch ────────────────────────
{
  const fn = SRC.match(/function onConversationBargeIn[\s\S]*?ttsPlayer\.reset\(\)/)[0];
  assert.match(fn, /voice\.utterFinishing \|\| voice\.voiceFetchAbort/, "(5) barge detects an in-flight whisper finalize");
  assert.match(fn, /voice\.voiceFetchAbort\.abort\(\)/, "(5) aborts the in-flight /voice/transcribe fetch");
  assert.match(fn, /session\.bumpEpoch\(\)/, "(5) bumps the epoch so postConversationBlob drops the submit");
  assert.match(fn, /voice\.cancelled = true/, "(5) sets cancelled");
}

// ── Bug 3: enter-during-wake-post re-arms conversation mode ─────────────────────────
{
  const fin = PIPE_SRC.match(/if \(bridge\.voiceEpochMatches\(epoch\)\) \{[\s\S]*?maybeReArmConversation\?\.\("postDone"\)/);
  assert.ok(fin, "(3) postVoiceBlob's finally re-arms conversation mode when the epoch moved on (enter-during-POST)");
  assert.match(PIPE_SRC, /else if \(bridge\.voice\.conversationMode\)/, "(3) the re-arm branch is gated on conversationMode");
}

// ── Bug 10: resumeAfterVisible does not wedge on a finished chunk ────────────────────
{
  const fn = SRC.match(/resumeAfterVisible\(\)\s*\{[\s\S]*?\n    },/)[0];
  assert.match(fn, /wasPaused && this\.audio && !this\.audio\.ended/, "(10) only resumes a genuinely paused, UNFINISHED chunk");
  assert.match(fn, /this\.audio && this\.audio\.ended/, "(10) detects a wedged finished chunk");
  assert.match(fn, /this\.queue\.length \|\| stuckFinished/, "(10) advances/drains instead of returning on a finished chunk");

  // Functional: simulate the wedge — playing=true, this.audio = a finished chunk, empty queue.
  // The old code (play()+return on _pausedForHidden) would leave playing=true forever; the fix
  // must run the drain so playing flips false.
  const p = jv.ttsPlayer;
  p.playing = true;
  p.queue = [];
  p.audio = { ended: true, play: () => Promise.resolve(), pause() {}, onended: null, onerror: null };
  p._pausedForHidden = true;
  p.resumeAfterVisible();
  await wait(5);
  assert.equal(p.playing, false, "(10) a finished-chunk wedge must be drained (playing=false), not left stuck true");
}

console.log("voice front-end bug-fix contract test: OK");
process.exit(0);
