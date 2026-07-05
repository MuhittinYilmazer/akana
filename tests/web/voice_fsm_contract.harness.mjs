/**
 * Voice FSM + capture/pipeline contract test — node-vm, NO REAL audio device.
 * - akana-voice-fsm.js: transition table, unknown-phase rejection (even with force),
 *   epoch bump semantics, cancelAll return phase.
 * - akana-voice-capture.js: pure functions (downsample, RIFF WAV, merge) +
 *   error propagation on the mic permission denial path (stream is not left open).
 * - akana-voice-pipeline.js: STT hallucination filter + API error formatter.
 * - akana-voice.js: loads without crashing in the stub DOM; handoffToTextChat returns
 *   false when idle; while TTS is playing, handoff resets the queue/playback (half-queue
 *   cleanup — request cancellation contract).
 * Run: node tests/web/voice_fsm_contract.harness.mjs
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

// ── Stub browser environment ──────────────────────────────────────────────────
const permissionError = () => {
  const e = new Error("Permission denied");
  e.name = "NotAllowedError";
  return e;
};
const ctx = {
  document: {
    getElementById: () => null,
    addEventListener: () => {},
    querySelector: () => null,
  },
  navigator: {
    mediaDevices: {
      getUserMedia: async () => {
        throw permissionError();
      },
      enumerateDevices: async () => [],
    },
  },
  localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  console,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  URL: { createObjectURL: () => "blob:stub", revokeObjectURL: () => {} },
  Blob,
  // ttsPlayer.enqueue decodes base64; in the gen-gate test the ACCEPTED frame takes
  // this path (a DROPPED frame never reaches atob). Simple decode via node Buffer.
  atob: (b64) => Buffer.from(b64, "base64").toString("binary"),
  btoa: (s) => Buffer.from(s, "binary").toString("base64"),
  // Minimal <audio> + rAF stubs: in the gen-gate test, let the ACCEPTED frame run the
  // playNext → new Audio(url).play() path smoothly (no real audio).
  // rAF no-op: energy/barge _ticks aren't set up here anyway (they require playing+rAF).
  Audio: class {
    constructor(url) { this.src = url; this.preload = "none"; }
    play() { return Promise.resolve(); }
    pause() {}
  },
  requestAnimationFrame: () => 0,
  cancelAnimationFrame: () => {},
  AudioContext: class {
    constructor() {
      this.sampleRate = 48000;
      this.state = "running";
    }
    close() {
      return Promise.resolve();
    }
  },
};
ctx.window = ctx;
vm.createContext(ctx);

vm.runInContext(read("web_ui/static/akana-voice-fsm.js"), ctx);

// ── 1. FSM: phase table + unknown-phase rejection ────────────────────────────
const { Phase, createVoiceSession } = ctx.window.AkanaVoiceFsm;
assert.ok(Phase && typeof createVoiceSession === "function", "FSM export surface missing");

const seen = [];
const s = createVoiceSession({ onTransition: (f, t, r) => seen.push([f, t, r]) });
assert.equal(s.getPhase(), Phase.IDLE);

// idle → capture_wake allowed
assert.equal(s.transition(Phase.CAPTURE_WAKE, "test:wake"), true);
assert.equal(s.getPhase(), Phase.CAPTURE_WAKE);

// Unknown phase: rejected even with force, and the current phase is not corrupted.
assert.equal(s.transition("bogus_phase", "test:unknown", { force: true }), false);
assert.equal(s.getPhase(), Phase.CAPTURE_WAKE, "an unknown phase must not corrupt the FSM");
assert.equal(s.canTransition("bogus_phase"), false);
// and subsequent valid transitions still work (old bug: lockup after landing on an off-table phase).
assert.equal(s.transition(Phase.CAPTURE_MIC, "test:mic"), true);

// Text-message-while-listening contract: capture → idle/processing bumps the epoch,
// so old finalize handlers go stale (the basis of handoffToTextChat).
const e0 = s.getEpoch();
assert.equal(s.transition(Phase.IDLE, "textChat"), true);
assert.ok(s.getEpoch() > e0, "leaving capture must bump the epoch");

// cancelAll: the wake preference is preserved — while armed, it returns to wake_armed.
s.setWakeArmed(true, "test");
assert.equal(s.getPhase(), Phase.WAKE_ARMED);
assert.equal(s.transition(Phase.CAPTURE_WAKE, "wake:test"), true);
const e1 = s.getEpoch();
assert.equal(s.cancelAll("esc"), true, "cancelAll must return true while capture is active");
assert.equal(s.getPhase(), Phase.WAKE_ARMED);
assert.ok(s.getEpoch() > e1, "cancelAll must bump the epoch");

// resetHardware: everything shuts down.
s.resetHardware("test");
assert.equal(s.getPhase(), Phase.IDLE);
assert.equal(s.isWakeArmed(), false);

// getUiPhase with external flags: ttsPlaying > postInFlight > phase.
assert.equal(s.getUiPhase({ ttsPlaying: true }), Phase.SPEAKING);
assert.equal(s.getUiPhase({ postInFlight: true }), Phase.PROCESSING);
assert.equal(s.getUiPhase({}), Phase.IDLE);

// ── 2. Capture: pure functions ───────────────────────────────────────────────
vm.runInContext(read("web_ui/static/akana-voice-capture.js"), ctx);
const cap = ctx.window.AkanaVoiceCapture;

const ds = cap.downsampleFloat32(new Float32Array(4800).fill(0.25), 48000, 16000);
assert.equal(ds.length, 1600, "48k→16k downsample length");

assert.ok(Math.abs(cap.rms(new Float32Array([0.5, -0.5])) - 0.5) < 1e-6, "rms");

const wavBlob = cap.encodeWavPcm16Mono(new Float32Array(160));
assert.equal(wavBlob.size, 44 + 320, "WAV: 44-byte header + 16-bit PCM body");
assert.equal(wavBlob.type, "audio/wav");

const merged = cap.mergeChunks([new Float32Array([1]), new Float32Array([2, 3])]);
assert.deepEqual(Array.from(merged), [1, 2, 3], "mergeChunks must preserve order");

// ── 3. Capture: mic permission denial — error propagates up, stream not opened ──
const denySession = createVoiceSession({});
const denyVoice = {
  audioCtx: null,
  stream: null,
  processor: null,
  worklet: null,
  workletModuleLoaded: false,
  source: null,
  mute: null,
  inSampleRate: 48000,
  rawBuffer: new Float32Array(0),
  maxRawSeconds: 4,
};
const denyCapture = cap.createCapture({
  voice: denyVoice,
  session: denySession,
  stopBrowserLiveTranscript: () => {},
  refreshMicDeviceList: () => {},
});
await assert.rejects(
  denyCapture.ensureAudio(),
  (e) => e && e.name === "NotAllowedError",
  "a permission denial must propagate up as NotAllowedError (the caller surfaces the error)",
);
assert.equal(denyVoice.stream, null, "on a permission denial the stream must not stay open");

// ── 3b. Capturer must NEVER self-interrupt on the raw mic ────────────────────
// Real barge-in is done with a separate AEC mic (akana-voice.js bargeDetector). The raw-mic
// barge branch was REMOVED from handleAudioChunk (it could self-interrupt without AEC). This
// guards against reintroduction: feeding high-energy audio while TTS plays must NOT trigger a
// barge from the capturer, even with bargeInEnabled=true.
{
  let bargeCalls = 0;
  const bargeVoice = {
    rawBuffer: new Float32Array(0),
    inSampleRate: 48000,
    maxRawSeconds: 4,
    wakeEnabled: false,
    utteranceActive: false,
    micManual: false,
    conversationMode: true,
    bargeInEnabled: true, // AEC path is on; the capturer itself must still never barge
    utterChunks: [],
    hadSpeech: false,
    silenceMs: 0,
    ambientRms: 0,
    ambientSamplesCollected: 0,
    ambientSamplesNeeded: 5,
    utterFinishing: false,
    utterStartTs: Date.now(),
    voiceRms: 0.02,
    noSpeechTimeoutMs: 4000,
    utterMaxMs: 12000,
    silenceHoldMs: 650,
  };
  const bargeCapture = cap.createCapture({
    voice: bargeVoice,
    session: createVoiceSession({}),
    ttsPlayer: { playing: true },
    stopBrowserLiveTranscript: () => {},
    refreshMicDeviceList: () => {},
    updateWakeMeter: () => {},
    onConversationBargeIn: () => {
      bargeCalls += 1;
    },
  });
  // Feed high-energy fake audio: the capturer must not call onConversationBargeIn at all.
  const loud = new Float32Array(128).fill(0.5);
  for (let i = 0; i < 20; i++) bargeCapture.handleAudioChunk(loud, 50);
  assert.equal(
    bargeCalls,
    0,
    "the capturer must never self-interrupt on the raw mic (no raw-mic barge branch)",
  );
}

// ── 3c. Whisper RMS-VAD: speech ONSET must not poison the ambient noise floor ──────
// Premature end-of-utterance (whisper path): conversation capture opens at the user's turn, so
// the first chunks are often their own loud word onset. If those are folded into the ambient
// floor, speechThr/silenceThr inflate and normal speech reads as "silence" → the RMS-VAD
// finalizes mid-utterance. The calibration must fold ONLY sub-voiceRms (non-speech) chunks.
{
  const mkVoice = () => ({
    rawBuffer: new Float32Array(0),
    inSampleRate: 48000,
    maxRawSeconds: 4,
    wakeEnabled: false,
    utteranceActive: true,
    micManual: false,
    conversationMode: true,
    convVadEnabled: true, // whisper conversation path → RMS-VAD auto-finalize runs
    utterChunks: [],
    utterMaxSeconds: 120,
    hadSpeech: false,
    silenceMs: 0,
    ambientRms: 0,
    ambientSamplesCollected: 0,
    ambientSamplesNeeded: 5,
    voiceRms: 0.02,
    utterFinishing: false,
    utterStartTs: Date.now(),
    noSpeechTimeoutMs: 4000,
    utterMaxMs: 12000,
    convSilenceHoldMs: 900,
    silenceHoldMs: 650,
  });
  const mkCapture = (voice, onFinalize) =>
    cap.createCapture({
      voice,
      session: createVoiceSession({}),
      ttsPlayer: { playing: false },
      stopBrowserLiveTranscript: () => {},
      refreshMicDeviceList: () => {},
      updateWakeMeter: () => {},
      wakeDebugEnabled: () => false,
      finalizeUtterance: async () => {
        onFinalize();
      },
    });

  // (i) Loud onset during the calibration window must NOT inflate the ambient floor.
  {
    const voice = mkVoice();
    const capr = mkCapture(voice, () => {});
    const loud = new Float32Array(128).fill(0.5); // rms 0.5 ≫ voiceRms
    for (let i = 0; i < 6; i++) capr.handleAudioChunk(loud, 43);
    assert.equal(
      voice.ambientRms,
      0,
      "speech-loud onset chunks must NOT be folded into the ambient floor (else silenceThr inflates → premature finalize)",
    );
  }

  // (ii) Genuinely quiet chunks DO calibrate the floor (adaptive behaviour preserved).
  {
    const voice = mkVoice();
    const capr = mkCapture(voice, () => {});
    const quiet = new Float32Array(128).fill(0.01); // rms 0.01 < voiceRms 0.02
    for (let i = 0; i < 6; i++) capr.handleAudioChunk(quiet, 43);
    assert.ok(
      Math.abs(voice.ambientRms - 0.01) < 1e-6,
      `quiet chunks must still calibrate the ambient floor (got ${voice.ambientRms})`,
    );
  }

  // (iii) End-to-end: a loud onset followed by steady MODERATE speech must NOT finalize
  // mid-utterance (the onset no longer inflates silenceThr, so speech stays above it).
  {
    const voice = mkVoice();
    let finals = 0;
    const capr = mkCapture(voice, () => (finals += 1));
    const onset = new Float32Array(128).fill(0.5);
    for (let i = 0; i < 5; i++) capr.handleAudioChunk(onset, 43); // calibration window (loud)
    const speech = new Float32Array(128).fill(0.05); // moderate steady speech > silenceThr
    for (let i = 0; i < 40; i++) capr.handleAudioChunk(speech, 43); // ~1.7s of continuous speech
    assert.equal(
      finals,
      0,
      "continuous moderate speech after a loud onset must NOT trigger a premature RMS-VAD finalize",
    );
  }
}

// ── 3d. Browser-SR post-final grace (timer window is not observable in node-vm) ─────
// Source-contract for the premature end-of-utterance fix on the DEFAULT browser path: the SR
// silence timer must extend its window when an onresult just grew the committed FINAL text
// (Chrome's post-final quiet window), instead of finalizing into that gap mid-speech.
{
  const SRC = read("web_ui/static/akana-voice.js");
  assert.match(
    SRC,
    /committedGrew\s*=\s*finalLine\.length\s*>\s*\(voice\._srPrevFinalLen/,
    "browser-SR onresult must detect a freshly-grown FINAL segment (post-final grace)",
  );
  assert.match(
    SRC,
    /const win = committedGrew \? baseMs \+ \(voice\.convPostFinalGraceMs/,
    "the SR silence timer must extend the window when a fresh final was just committed",
  );
  // The tracker must reset on a fresh recognizer session (else a stale length suppresses grace).
  assert.match(
    SRC,
    /browserRec = new SR\(\);[\s\S]{0,200}?voice\._srPrevFinalLen = 0/,
    "a fresh SR session must reset the committed-final tracker",
  );
}

// ── 3e. FSM/legacy-flag duality collapsed to DERIVED getters ────────────────────────
// The legacy capture flags must be getters derived from the FSM phase (single source of truth),
// never mirrored fields — so they can't drift. applySessionToLegacyFlags + its call sites are gone,
// and setWakeListening's old direct `micManual = false` write is now a real FSM transition.
{
  const SRC = read("web_ui/static/akana-voice.js");
  assert.match(SRC, /Object\.defineProperty\(voice, "wakeEnabled",[\s\S]{0,120}?session\.isWakeArmed\(\)/,
    "voice.wakeEnabled must be a getter deriving from session.isWakeArmed()");
  assert.match(SRC, /Object\.defineProperty\(voice, "utteranceActive",[\s\S]{0,120}?session\.isCaptureWake\(\)/,
    "voice.utteranceActive must be a getter deriving from session.isCaptureWake()");
  assert.match(SRC, /Object\.defineProperty\(voice, "micManual",[\s\S]{0,120}?session\.isCaptureMic\(\)/,
    "voice.micManual must be a getter deriving from session.isCaptureMic()");
  // The mirror function and all its call sites are removed (only an explanatory comment may remain).
  assert.doesNotMatch(SRC, /function applySessionToLegacyFlags/,
    "applySessionToLegacyFlags() must be deleted (getters replace it)");
  assert.doesNotMatch(SRC, /^\s*applySessionToLegacyFlags\(\);\s*$/m,
    "no applySessionToLegacyFlags() call sites may remain");
  // The former direct-write drift point is now a transition, not a bare flag write.
  assert.doesNotMatch(SRC, /voice\.micManual\s*=\s*false/,
    "setWakeListening must transition the FSM, not write micManual directly");
  // capture.js / pipeline.js must no longer call the removed bridge passthrough.
  for (const mod of ["akana-voice-capture.js", "akana-voice-pipeline.js"]) {
    assert.doesNotMatch(read(`web_ui/static/${mod}`), /applySessionToLegacyFlags/,
      `${mod} must not reference the removed applySessionToLegacyFlags`);
  }
}

// ── 4. Pipeline: hallucination filter + error formatter ──────────────────────
ctx.window.AkanaCore = {
  baseUrl: () => "http://test",
  authHeaders: () => ({}),
  authHeadersMultipart: () => ({}),
  escapeHtml: (x) => String(x),
};
vm.runInContext(read("web_ui/static/akana-voice-pipeline.js"), ctx);
const pipe = ctx.window.AkanaVoicePipeline.create({});

for (const halluc of ["Thanks for watching.", "Altyazı çeviren biri", "...", "uhh", "a"]) {
  assert.equal(pipe.looksLikeSttHallucination(halluc), true, `hallucination: ${halluc}`);
}
for (const real of ["Yarın hava nasıl olacak?", "Işıkları kapat", "3.5 oranını hesapla", "2"]) {
  assert.equal(pipe.looksLikeSttHallucination(real), false, `real speech: ${real}`);
}
// A single DIGIT is a legitimate numbered-menu answer ("1, 2, or 3?") — must NOT be dropped,
// while a single letter (noise-prone) still is.
assert.equal(pipe.looksLikeSttHallucination("2"), false, "single digit is a valid answer");
assert.equal(pipe.looksLikeSttHallucination("a"), true, "single letter is still dropped as noise");

assert.equal(
  pipe.formatApiError({ detail: { error: { message: "yetki yok" } } }, "fb"),
  "yetki yok",
);
assert.equal(pipe.formatApiError({ detail: "düz metin" }, "fb"), "düz metin");
assert.equal(pipe.formatApiError({}, "fb"), "fb");
assert.ok(
  pipe.formatApiError({ detail: [{ loc: ["body", "text"], msg: "kısa" }] }, "fb").includes("kısa"),
);

// ── 5. akana-voice.js: loads in the stub DOM; handoff + TTS queue cleanup ────
vm.runInContext(read("web_ui/static/akana-voice.js"), ctx);
const jv = ctx.window.AkanaVoice;
assert.ok(jv, "window.AkanaVoice failed to load");
for (const fn of ["handoffToTextChat", "cancelVoiceActivity", "streamTtsParam", "init"]) {
  assert.equal(typeof jv[fn], "function", `AkanaVoice.${fn} missing`);
}

// With no voice activity, handoff is not needed (false) and produces no side effects.
assert.equal(jv.handoffToTextChat(), false);

// Request cancellation contract: a half TTS queue is fully drained by reset.
jv.ttsPlayer.queue.push("blob:a", "blob:b");
jv.ttsPlayer.playing = true;
jv.ttsPlayer.reset();
assert.equal(jv.ttsPlayer.queue.length, 0, "reset must drain a half queue");
assert.equal(jv.ttsPlayer.playing, false);

// Text message while TTS is playing: handoff returns true and playback stops.
jv.ttsPlayer.queue.push("blob:c");
jv.ttsPlayer.playing = true;
assert.equal(jv.handoffToTextChat(), true, "handoff must engage while TTS is playing");
assert.equal(jv.ttsPlayer.playing, false);
assert.equal(jv.ttsPlayer.queue.length, 0);

// ── 6. GEN GATE (BUG #3): a late tts_chunk arriving AFTER reset is DROPPED ────
// Barge-in / STOP / a new turn calls ttsPlayer.reset() → accept-gen increments. A LATE
// frame that dropped into the buffer from the last successful SSE read (drained
// asynchronously via the catch's await flushSseQueue) arrives with the old feed
// generation → enqueue must drop it; otherwise the cancelled response's audio restarts
// on top of the new turn.
{
  const tp = jv.ttsPlayer;
  tp.reset(); // clean start (queue empty, playing false)
  const genAtStreamStart = tp.acceptGen(); // generation captured on the stream's FIRST tts frame
  assert.equal(typeof genAtStreamStart, "number", "acceptGen must return a number");

  // Barge-in: a new turn's reset() increments the accept generation (old stream is now invalid).
  tp.reset();
  assert.ok(tp.acceptGen() > genAtStreamStart, "reset must increment accept-gen");

  // LATE frame of the cancelled stream (with the old genAtStreamStart) → must be DROPPED:
  // it must not enter the queue nor start playback (it doesn't even reach atob/playNext).
  const b64 = ctx.btoa("RIFF....fakeaudio");
  await tp.enqueue(b64, "audio/wav", genAtStreamStart);
  assert.equal(tp.queue.length, 0, "a stale-gen late frame must be DROPPED (does not enter the queue)");
  assert.equal(tp.playing, false, "a stale-gen frame must NOT start playback");

  // A call with no gen given (old path / stream-context-free) is ACCEPTED UNCONDITIONALLY (backward compat):
  // accept → enqueue immediately triggers playNext (the queue is shifted, playing=true).
  await tp.enqueue(b64, "audio/wav");
  assert.equal(tp.playing, true, "a gen-less enqueue must be ACCEPTED per the old behavior (starts playing)");
  tp.reset();
  assert.equal(tp.playing, false, "playing must be cleared after reset");

  // Current generation (if the stream captured its first frame AFTER reset) is ACCEPTED:
  const curGen = tp.acceptGen();
  await tp.enqueue(b64, "audio/wav", curGen);
  assert.equal(tp.playing, true, "a current-gen frame must be ACCEPTED (starts playing)");
  tp.reset();
}

// ── 7. VISIBILITY RECOVERY (switch tab → come back): voice mode must keep
// listening. Old bug: the handler started capture only "WHILE NOT capturing" → the
// common stuck state (capturing===true + a DEAD recognizer underneath) was never
// recovered and the user had to toggle voice mode off and on. decideConvVisibilityAction
// is the pure core of this decision; the contract is locked here.
{
  const decide = jv._decideConvVisibilityAction;
  assert.equal(typeof decide, "function", "AkanaVoice._decideConvVisibilityAction missing");
  const base = {
    visible: true,
    conversationMode: true,
    capturing: false,
    chatInFlight: false,
    ttsPlaying: false,
    ttsQueued: false,
  };
  // If voice mode is off, always a no-op.
  assert.equal(decide({ ...base, conversationMode: false }), "none", "conv off → none");
  assert.equal(
    decide({ ...base, conversationMode: false, visible: false }),
    "none",
    "conv off + hidden → none",
  );
  // If we ARE LISTENING when hidden, cleanly stop the recognizer (prevent a zombie restart storm).
  assert.equal(
    decide({ ...base, visible: false, capturing: true }),
    "stop-sr",
    "hidden + capture → stop-sr (prevent a zombie recognizer)",
  );
  // If we're not listening when hidden (a response/TTS is streaming), don't touch it.
  assert.equal(
    decide({ ...base, visible: false, capturing: false }),
    "none",
    "hidden + no capture → none",
  );
  // MAIN BUG: on returning, the FSM is still "capturing" (dead recognizer underneath) → rebuild.
  assert.equal(
    decide({ ...base, visible: true, capturing: true }),
    "rebuild-sr",
    "visible + capture (dead recognizer) → rebuild-sr — the OLD BUG left the mic dead here",
  );
  // If we're idle on returning, open a fresh listening turn.
  assert.equal(
    decide({ ...base, visible: true, capturing: false }),
    "start-capture",
    "visible + idle → start-capture",
  );
  // If a response is still streaming on returning (chat/TTS), don't touch the mic (half-duplex).
  for (const live of [
    { chatInFlight: true },
    { ttsPlaying: true },
    { ttsQueued: true },
  ]) {
    assert.equal(
      decide({ ...base, visible: true, capturing: false, ...live }),
      "reply-live",
      `visible + reply streaming (${Object.keys(live)[0]}) → reply-live (do not touch the mic)`,
    );
  }
  // A streaming response SHADOWS the capture flag: even if capturing=true, if the response is live it's reply-live.
  assert.equal(
    decide({ ...base, visible: true, capturing: true, ttsPlaying: true }),
    "reply-live",
    "even with capture set while the reply is live it is reply-live (not rebuild-sr)",
  );
}

// ── 8. RECOGNIZER LIVENESS (zombie recognizer) RECOVERY: a SpeechRecognition
// start()ed right after TTS playback / on tab return often turns into a SILENT ZOMBIE
// (start succeeds, audiostart NEVER fires, and since continuous=true onend doesn't fire
// either → the mic is deaf forever). The user manually switching tabs and coming back
// used to fix it by building a fresh recognizer; the watchdog does this automatically.
// shouldRecreateRecognizer is the pure core of this "rebuild" decision.
{
  const should = jv._shouldRecreateRecognizer;
  assert.equal(typeof should, "function", "AkanaVoice._shouldRecreateRecognizer missing");
  // Zombie: no audiostart + still listening + visible + no TTS + budget left → REBUILD.
  const zombie = {
    engaged: false,
    replaced: false,
    shouldRun: true,
    hidden: false,
    ttsBusy: false,
    listening: true,
    retries: 0,
    maxRetries: 3,
  };
  assert.equal(should(zombie), true, "silent zombie → recreate (MAIN RECOVERY)");
  // audiostart arrived (engaged) → don't touch it.
  assert.equal(should({ ...zombie, engaged: true }), false, "engaged → no recreate");
  // A newer recognizer took over → the old watchdog is a no-op.
  assert.equal(should({ ...zombie, replaced: true }), false, "replaced → no recreate");
  // Intentionally stopped (shouldRun=false) → don't touch it.
  assert.equal(should({ ...zombie, shouldRun: false }), false, "shouldRun=false → no recreate");
  // Tab hidden → the mic doesn't run anyway; visibilitychange rebuilds it.
  assert.equal(should({ ...zombie, hidden: true }), false, "hidden → no recreate");
  // Akana is speaking (half-duplex) → drain will re-arm, the watchdog stays out of it.
  assert.equal(should({ ...zombie, ttsBusy: true }), false, "ttsBusy → no recreate");
  // Listening is no longer expected (turn ended/idle) → don't touch it.
  assert.equal(should({ ...zombie, listening: false }), false, "not listening → no recreate");
  // Budget exhausted → prevent an infinite loop (persistent mic failure).
  assert.equal(should({ ...zombie, retries: 3 }), false, "retries==max → no recreate (loop guard)");
  assert.equal(should({ ...zombie, retries: 2 }), true, "retries<max → still recreate");
}

// ── 9. TAB-SWITCH TTS HOLD (bug: a reply that lands while the tab is HIDDEN gets skipped
// unheard — a backgrounded tab suspends audio so `timeupdate` stops and the stall watchdog
// force-advances every chunk — so on return the mic is already re-armed to "Listening" and
// the answer was never spoken). Contract: while hidden, playNext HOLDS chunks in the queue
// (playing stays false, nothing is skipped); on return, resumeAfterVisible starts playback so
// the held reply is actually spoken. pageHidden() reads document.visibilityState live.
{
  const tp = jv.ttsPlayer;
  tp.reset();
  const b64 = ctx.btoa("RIFF....fakeaudio");
  const curGen = tp.acceptGen();

  // Tab hidden: enqueued chunks are HELD, not played, and NOT skipped.
  ctx.document.visibilityState = "hidden";
  await tp.enqueue(b64, "audio/wav", curGen);
  await tp.enqueue(b64, "audio/wav", curGen);
  assert.equal(tp.playing, false, "while hidden, TTS must NOT start playing");
  assert.equal(
    tp.queue.length,
    2,
    "while hidden, chunks are held in the queue (a non-empty queue blocks the turn re-arm)",
  );

  // Tab returns to the foreground → playback starts, draining the held reply.
  ctx.document.visibilityState = "visible";
  tp.resumeAfterVisible();
  assert.equal(tp.playing, true, "on return, the held reply must start speaking (not be skipped)");

  // Pause/resume of an already-playing chunk across a hide→show round-trip: `playing` is
  // preserved (re-arm stays blocked) and the paused element is resumed on return.
  tp.holdForHidden();
  assert.equal(tp.playing, true, "holdForHidden must keep `playing` set (re-arm stays blocked)");
  assert.equal(tp._pausedForHidden, true, "holdForHidden must mark the current chunk paused");
  tp.resumeAfterVisible();
  assert.equal(tp._pausedForHidden, false, "resumeAfterVisible must clear the paused flag");

  tp.reset();
  ctx.document.visibilityState = "visible";
}

console.log("voice fsm/capture/pipeline contract test: OK");
