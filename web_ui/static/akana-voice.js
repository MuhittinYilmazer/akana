/**
 * Akana voice — wake, PTT, STT upload, streaming TTS player (loaded before app.js).
 */
(() => {
  // i18n helper (bilingual — loaded before this module)
  const _voiceT = (k, vars) => {
    const base = (typeof window !== "undefined" && window.AkanaI18n?.t) ? window.AkanaI18n.t(k) : k;
    if (!vars) return base;
    return base.replace(/\{(\w+)\}/g, (_, v) => (vars[v] !== undefined ? vars[v] : `{${v}}`));
  };
  const LS_SPEECH_LANG = "akana.speechLang";
  // Which app-language the STT pick was made under — lets a manual STT choice persist
  // until the app language changes, at which point STT realigns to the new language.
  const LS_SPEECH_LANG_BASIS = "akana.speechLang.basis";
  const LS_WAKE_AUTOSTART = "akana.wakeAutostart";
  const LS_TTS = "akana.streamTts";
  const LS_MIC_DEVICE = "akana.micDevice";
  const LS_TTS_LANG = "akana.ttsLang";
  const LS_SETUP_BANNER = "akana.setupBannerDismissed";
  // WAKE DETECTION SOURCE — which engine listens for "Hey Akana".
  //   "model"   (DEFAULT) = server openWakeWord scoring (custom WAKE_MODEL). The 300ms
  //             /voice/wake poll is the SOLE trigger and the wake_threshold gates it.
  //             Falls back to "browser" automatically if server scoring is unavailable.
  //   "browser" = browser SpeechRecognition phrase-match ("Hey Akana"). No server poll;
  //             the wake_threshold does not apply.
  // The two paths are MUTUALLY EXCLUSIVE (see setWakeListening) — running both in parallel
  // made the threshold feel inert (the browser SR fired regardless of the server score).
  const LS_WAKE_SOURCE = "akana.wakeSource";

  // SPEECH-TO-TEXT SOURCE for conversation-mode turns — which engine transcribes
  // the user's utterance and drives end-of-turn.
  //   "browser" (DEFAULT) = browser SpeechRecognition. SR silence timer detects the
  //             end of the turn and the SR transcript is submitted directly to chat
  //             (no server round-trip). This is the historical behaviour, unchanged.
  //   "whisper" = server faster-whisper (/api/v1/voice/transcribe). The Worklet RMS-VAD
  //             silence auto-finalize detects the end of the turn acoustically and the
  //             buffered audio is POSTed for transcription; the Whisper transcript is
  //             submitted. SR (if present) only powers the live-transcript PREVIEW strip
  //             and MUST NOT submit (finalizeConversationFromSR is a no-op in this mode).
  // EXACTLY ONE submission per utterance is guaranteed by making the two end-of-turn
  // detectors mutually exclusive per mode: browser → SR path only (VAD off in conv);
  // whisper → VAD path only (SR submit suppressed).
  const LS_STT_SOURCE = "akana.sttSource";

  let hooks = {
    isChatPage: false,
    appendRow: () => {},
    chatRecordMessage: () => {},
    setConversationId: () => {},
    setOrb: () => {},
    setComposerHint: () => {},
    getChatInFlight: () => false,
    abortActiveChatStream: () => {},
    setChatInFlight: () => {},
    getWsReadyState: () => 0,
    showToast: () => {},
    saveLlmSettings: async () => {},
  };

  const baseUrl = () => window.AkanaCore.baseUrl();
  const authHeaders = (j) => window.AkanaCore.authHeaders(j);
  const authHeadersMultipart = () => window.AkanaCore.authHeadersMultipart();
  const escapeHtml = (s) => window.AkanaCore.escapeHtml(s);


  const btnWake = document.getElementById("btn-wake");
  const wakeMeter = document.getElementById("wake-meter");
  const btnWakeTest = document.getElementById("btn-wake-test");
  const btnMic = document.getElementById("btn-mic");
  const speechLangSelect = document.getElementById("speech-lang");
  const msg = document.getElementById("msg");

  /* ---------- WAV / resample (16 kHz mono for Akana API) ---------- */


  /* ---------- Live transcript strip (glass strip above composer capsule) ---------- */

  const LIVE_STRIP_MAX_CHARS = 140;
  let liveStripEl = null;
  let liveStripHideTimer = null;

  function ensureLiveTranscriptStrip() {
    // No chat form (e.g. memory.html) — silently no-op.
    const form = document.getElementById("chat-form");
    if (!form) return null;
    if (liveStripEl && liveStripEl.isConnected) return liveStripEl;
    const el = document.createElement("div");
    el.className = "akana-live-transcript";
    el.setAttribute("aria-hidden", "true");
    el.innerHTML =
      `<span class="jlt-label"><span class="jlt-dot">●</span> ${_voiceT("voice.rec_label")}</span>` +
      '<span class="jlt-bars" hidden><i></i><i></i><i></i></span>' +
      '<span class="jlt-text"><span class="jlt-final"></span><span class="jlt-interim"></span></span>';
    form.appendChild(el);
    liveStripEl = el;
    return el;
  }

  /** Bar indicator: listening animation when there is no live typing (SR not supported). */
  function setLiveStripBarsMode(on) {
    if (!liveStripEl) return;
    const bars = liveStripEl.querySelector(".jlt-bars");
    if (bars) bars.hidden = !on;
  }

  function showLiveTranscriptStrip(hasInterim) {
    const el = ensureLiveTranscriptStrip();
    if (!el) return;
    clearTimeout(liveStripHideTimer);
    liveStripHideTimer = null;
    setLiveStripBarsMode(!hasInterim);
    el.querySelector(".jlt-final").textContent = "";
    el.querySelector(".jlt-interim").textContent = "";
    el.classList.add("jlt-visible");
  }

  /** Committed words in ink, interim part in faint italic. */
  function updateLiveTranscriptStrip(finalText, interimText) {
    if (!liveStripEl || !liveStripEl.classList.contains("jlt-visible")) return;
    let f = finalText || "";
    let i = interimText || "";
    // Single line: trim total to last ~140 chars; prepend … for overflow.
    if (i.length >= LIVE_STRIP_MAX_CHARS) {
      f = "";
      i = "…" + i.slice(i.length - LIVE_STRIP_MAX_CHARS + 1);
    } else if (f.length + i.length > LIVE_STRIP_MAX_CHARS) {
      f = "…" + f.slice(f.length - (LIVE_STRIP_MAX_CHARS - i.length) + 1);
    }
    liveStripEl.querySelector(".jlt-final").textContent = f;
    liveStripEl.querySelector(".jlt-interim").textContent = (f && i ? " " : "") + i;
    if (f || i) setLiveStripBarsMode(false); // text is flowing — bars no longer needed
  }

  /** Recording ended: fade out over 300 ms, then clear the text. */
  function hideLiveTranscriptStrip() {
    if (!liveStripEl || !liveStripEl.classList.contains("jlt-visible")) return;
    liveStripEl.classList.remove("jlt-visible"); // CSS: 300 ms opacity transition
    clearTimeout(liveStripHideTimer);
    liveStripHideTimer = setTimeout(() => {
      if (!liveStripEl) return;
      liveStripEl.querySelector(".jlt-final").textContent = "";
      liveStripEl.querySelector(".jlt-interim").textContent = "";
    }, 320);
  }

  /* ---------- Browser live transcript (Web Speech API, optional) ---------- */

  let browserRec = null;
  let browserRecShouldRun = false;
  // MICROPHONE RESTART BACK-OFF (mobile "continuous listen/unlisten" thrashing).
  // Mobile Web Speech API rounds can end every second due to silence/system events;
  // UNCONDITIONAL start() in onend → mic indicator flash + battery + echo risk.
  // Solution: do not restart until at least _recRestartBackoff ms have elapsed since
  // the last start (single pending timer). In fast-fail loops back-off DOUBLES
  // (600 ms → 4 s); healthy turns (> 1.5 s listened) reset to base → re-arm NEVER
  // shuts off, only the cadence stays stable. Desktop (continuous=true) rarely ends
  // → no visible difference.
  const MIN_REC_RESTART_MS = 600;
  const MAX_REC_RESTART_MS = 4000;
  let _recRestartTimer = null;
  let _lastRecStartAt = 0;
  let _recRestartBackoff = MIN_REC_RESTART_MS;

  // LISTENING-STUCK ROOT FIX (intermittent silent recognizer after a reply).
  // ROOT CAUSE: browserRec.start() used to be called SYNCHRONOUSLY from inside the TTS
  // <audio>.onended → playNext drain callstack (playNext → finishConversationTurnIfTtsDone →
  // maybeReArmConversation → startConversationCapture → startBrowserLiveTranscript → start()).
  // Starting SR in the audio-teardown window makes Chrome return a "silent zombie" (start()
  // succeeds, no audiostart/onresult ever fires, and continuous=true means onend never fires
  // either → the recognizer sits deaf forever). Compounded by (b) building+starting a NEW
  // recognizer before the PREVIOUS session's async onend has completed, and (c) ttsPlayer.playing
  // being cleared before the <audio> element is fully released.
  // FIX: (i) DEFER the recognizer start OFF the teardown callstack by MIC_SETTLE_MS
  // (startConversationCapture schedules startBrowserLiveTranscript instead of calling it
  // synchronously); (ii) SERIALIZE against the previous session via a teardown-pending latch
  // (wait for the old recognizer's onend, or REC_TEARDOWN_FALLBACK_MS, before building a new
  // one) instead of null-onend + immediate rebuild. The liveness watchdog above stays a BACKSTOP.
  const MIC_SETTLE_MS = 220; // let the <audio> teardown callstack unwind + audio path release
  const REC_TEARDOWN_FALLBACK_MS = 400; // if the stopped session's onend never lands, start anyway
  let _micSettleTimer = null;
  let _recTeardownPending = false;
  let _recTeardownFallbackTimer = null;

  // RECOGNIZER LIVENESS WATCHDOG. A healthy SpeechRecognition fires `audiostart` within a few
  // hundred ms of start() — on EVERY working session, even if the user stays silent. When
  // start() is called right after TTS <audio> playback finishes (or right after a tab-visibility
  // return), Chrome frequently returns a SILENT ZOMBIE: start() succeeds, no audio is ever
  // captured, and because continuous=true `onend` never fires either → the recognizer sits deaf
  // forever and the user has to toggle voice mode (or, as observed, switch tabs to force a fresh
  // recognizer). If `audiostart` does not arrive within REC_ENGAGE_TIMEOUT_MS we recreate the
  // recognizer — the same thing the manual tab-switch did, but automatic.
  const REC_ENGAGE_TIMEOUT_MS = 1200;
  const MAX_REC_ENGAGE_RETRIES = 5;
  let _recLivenessTimer = null;
  let _recEngageRetries = 0;

  /** True when the tab is backgrounded. The browser withholds mic audio from
   *  SpeechRecognition while hidden, so any start()/restart just spins into a dead
   *  ("zombie") recognizer that stays deaf even after the tab returns — the user then has
   *  to toggle voice mode off/on. We gate SR start() on this and rebuild a fresh recognizer
   *  from the visibilitychange handler the moment the tab is foregrounded again. */
  function pageHidden() {
    try {
      return typeof document !== "undefined" && document.visibilityState === "hidden";
    } catch {
      return false;
    }
  }

  /** Pure decision for the conversation-mode visibilitychange handler — kept separate so
   *  the recovery contract is unit-testable (see voice_fsm_contract.harness). Returns:
   *   • "none"          — not in conversation mode, or hidden while not listening → do nothing.
   *   • "stop-sr"       — hidden while listening → tear the recognizer down cleanly so the
   *                       browser's silent SR-kill + our restart back-off don't leave a zombie.
   *   • "reply-live"    — visible, but a reply is still streaming / TTS still playing → resume
   *                       the audio context but DO NOT touch the mic (half-duplex).
   *   • "rebuild-sr"    — visible, FSM still in a capture phase → the recognizer was torn down
   *                       while hidden; rebuild a fresh one in place.
   *   • "start-capture" — visible, idle → open a fresh listening turn.
   *  The old handler only ever did "start-capture" and only when NOT capturing, so the common
   *  stuck state (capturing===true with a dead recognizer under it) was never recovered. */
  function decideConvVisibilityAction(st) {
    if (!st.conversationMode) return "none";
    if (st.liveActive) return "none"; // Live (Gemini/OpenAI realtime) owns its own WS/audio session
    if (!st.visible) return st.capturing ? "stop-sr" : "none";
    // Include the awaiting-reply / TTS-stream-open flags: in the dead window between
    // chat:stream:done and the first tts_chunk, chatInFlight/ttsPlaying/ttsQueued are all
    // false even though the turn is still alive — without this a tab return here would
    // wrongly report "start-capture" and clobber the still-live turn's flags.
    // utterFinishing/postInFlight cover the whisper-STT finalize window (/voice/transcribe
    // in flight): chatInFlight/convAwaitingReply are still false there, so without them a
    // tab return during finalize would report "start-capture" and open the mic mid-finalize.
    const replyLive =
      !!st.chatInFlight ||
      !!st.ttsPlaying ||
      !!st.ttsQueued ||
      !!st.convAwaitingReply ||
      !!st.ttsStreamOpen ||
      !!st.utterFinishing ||
      !!st.postInFlight;
    if (replyLive) return "reply-live";
    return st.capturing ? "rebuild-sr" : "start-capture";
  }

  /** Pure gate for the recognizer liveness watchdog — kept separate so the recreate contract
   *  is unit-testable (see voice_fsm_contract.harness). Returns true when a recognizer that
   *  never reported `audiostart` should be torn down and recreated. Every guard must hold:
   *   • !engaged     — audiostart/sound/speech/result never arrived (silent zombie).
   *   • !replaced    — this is still the current recognizer (a newer one didn't supersede it).
   *   • shouldRun    — we still intend to be recording (not intentionally stopped).
   *   • !hidden      — a hidden tab can't engage the mic anyway (visibilitychange rebuilds).
   *   • !ttsBusy     — Akana is not speaking (half-duplex; the TTS drain re-arms listening).
   *   • listening    — the FSM/PTT still expects us to be listening.
   *   • retries left  — bounded so a permanently-unavailable mic can't loop forever. */
  function shouldRecreateRecognizer(st) {
    if (st.engaged) return false;
    if (st.replaced) return false;
    if (!st.shouldRun) return false;
    if (st.hidden) return false;
    if (st.ttsBusy) return false;
    if (!st.listening) return false;
    if (st.retries >= st.maxRetries) return false;
    return true;
  }

  // STT / live-transcript locale follows the unified app language (en→en-US, tr→tr-TR).
  function _langLocale() {
    const lang =
      (window.AkanaI18n && window.AkanaI18n.getLanguage && window.AkanaI18n.getLanguage()) || "en";
    return lang === "tr" ? "tr-TR" : "en-US";
  }

  function speechLang() {
    if (speechLangSelect && speechLangSelect.value) return speechLangSelect.value.trim();
    return localStorage.getItem(LS_SPEECH_LANG) || _langLocale();
  }

  /** SINGLE SOURCE OF TRUTH for the TTS synthesis language ("en"/"tr"). Resolve "auto"
   *  (Whisper auto-detect) BEFORE the en/tr decision: `startsWith("en") ? "en" : "tr"` maps
   *  "auto" to "tr", so an English user who merely picks "Auto-detect" STT would get a Turkish
   *  voice reading English replies (violates the English-default mandate). When the speech
   *  language is "auto" or empty, derive the TTS language from the UI language instead. Shared
   *  with the wake path (pipeline tts_lang) and settings (ttsPreferredLang) via the bridge. */
  function ttsLangFromSpeech() {
    const sl = (speechLang() || "").trim().toLowerCase();
    if (!sl || sl === "auto") return _langLocale().startsWith("en") ? "en" : "tr";
    return sl.startsWith("en") ? "en" : "tr";
  }

  /** SINGLE SOURCE OF TRUTH for the conversation voice-exit phrase test. Both the browser-SR
   *  path (finalizeConversationFromSR) and the Whisper submit path (postConversationBlob in
   *  akana-voice-pipeline.js, via the bridge) call this so "dur"/"stop"/"goodbye"/"exit" close
   *  conversation mode identically in both STT modes. Only a STANDALONE exit phrase counts —
   *  if it is part of a longer command ("turn off the lights") it does not match (exact match).
   *  Follows the same language setting as the SR locale (_langLocale) so English users also
   *  have a voice exit path, not just Turkish. Returns true iff `text` is an exit phrase. */
  function isConversationExitPhrase(text) {
    const t = (text || "").trim();
    if (!t) return false;
    const exitLocale = _langLocale() === "tr-TR" ? "tr" : "en";
    const exitNorm = t.toLocaleLowerCase(exitLocale).replace(/[.!?,…]+$/u, "").trim();
    const exitRe =
      exitLocale === "tr"
        ? /^(dur|bitir|yeter|çık|çıkış|görüşürüz|konuşmayı bitir|sohbeti bitir|sesli modu kapat|sesli moddan çık)$/u
        : /^(stop|end|enough|goodbye|bye|exit|end conversation|stop conversation|end voice mode|exit voice mode)$/u;
    return exitRe.test(exitNorm);
  }

  function stopBrowserLiveTranscript() {
    browserRecShouldRun = false;
    cancelPendingRecRestart();
    cancelRecLivenessWatchdog();
    cancelMicSettleStart(); // a pending deferred start is stale once we tear down
    hideLiveTranscriptStrip();
    if (msg) msg.classList.remove("input-live");
    if (!browserRec) return;
    // (ii) SERIALIZE teardown: do NOT null onend + let a new recognizer be built immediately.
    // Chrome fires the stopped session's `onend` ASYNChronously; building+starting the next
    // recognizer before that lands returns a silent zombie. Detach the transcript/liveness
    // handlers (so a late result from the old session can't leak), keep a dedicated onend that
    // releases the teardown latch, and arm a fallback timer in case onend never arrives.
    const stopping = browserRec;
    browserRec = null;
    _recTeardownPending = true;
    if (_recTeardownFallbackTimer) {
      try { clearTimeout(_recTeardownFallbackTimer); } catch { /* ignore */ }
    }
    const releaseLatch = () => {
      if (_recTeardownFallbackTimer) {
        try { clearTimeout(_recTeardownFallbackTimer); } catch { /* ignore */ }
        _recTeardownFallbackTimer = null;
      }
      _recTeardownPending = false;
    };
    _recTeardownFallbackTimer = setTimeout(releaseLatch, REC_TEARDOWN_FALLBACK_MS);
    try {
      stopping.onresult = null;
      stopping.onerror = null;
      stopping.onaudiostart = stopping.onsoundstart = stopping.onspeechstart = null;
    } catch { /* ignore */ }
    stopping.onend = releaseLatch; // the old session's real end releases the serialize latch
    try {
      stopping.stop();
    } catch {
      releaseLatch(); // stop() threw → onend won't fire; release now so we don't wedge
    }
  }

  /** Cancel a pending mic-settle deferred start (see MIC_SETTLE_MS). */
  function cancelMicSettleStart() {
    if (_micSettleTimer) {
      try { clearTimeout(_micSettleTimer); } catch { /* ignore */ }
      _micSettleTimer = null;
    }
  }

  /** (i)+(ii) Schedule startBrowserLiveTranscript OFF the current callstack after a short
   *  mic-settle delay, re-checking gates when it fires (state may change during the wait).
   *  Used by startConversationCapture (drain/re-arm path) and by startBrowserLiveTranscript's
   *  own teardown-latch deferral. Single pending timer. */
  function scheduleMicSettleStart() {
    if (_micSettleTimer) return; // already scheduled
    _micSettleTimer = setTimeout(() => {
      _micSettleTimer = null;
      // Re-validate: state may have changed during the settle window. Half-duplex — if TTS
      // is speaking again (new turn / next sentence) the tts drain will re-arm later; muted /
      // user-edit / no-longer-listening → skip. Conversation mode must still be actively
      // capturing (phase preserved by startConversationCapture); wake/PTT keep their own flags.
      if (voice.liveTranscriptUserEdit) return;
      if (ttsPlayer.playing || ttsPlayer.queue.length > 0) {
        // BUG E1 (deadlock): startConversationCapture flipped the FSM to CAPTURE_WAKE then
        // deferred SR here; if TTS is still speaking we bail. But if we stay in CAPTURE_WAKE
        // the TTS drain's maybeReArmConversation early-returns on isCapturing() and never
        // re-opens the mic → permanently deaf. Mirror the voice:mic:mute handler: leave
        // CAPTURE_WAKE so isCapturing() is false, letting the drain re-arm us later.
        if (voice.conversationMode && session.isCapturing()) {
          try {
            session.transition(
              session.isWakeArmed() ? VPhase.WAKE_ARMED : VPhase.IDLE,
              "mic:settle-tts-busy",
              { force: true }
            );
          } catch { /* ignore */ }
        }
        return;
      }
      const stillListening =
        voice.micManual ||
        voice.utteranceActive ||
        (voice.conversationMode && !voice.micMuted && session.isCapturing());
      if (!stillListening) return;
      startBrowserLiveTranscript();
    }, MIC_SETTLE_MS);
  }

  /** Cancel a pending liveness-watchdog recreate (see REC_ENGAGE_TIMEOUT_MS). Called on any
   *  teardown so a stale recreate can't fire after an intentional stop / recognizer swap. */
  function cancelRecLivenessWatchdog() {
    if (_recLivenessTimer) {
      try { clearTimeout(_recLivenessTimer); } catch { /* ignore */ }
      _recLivenessTimer = null;
    }
  }

  /** Cancel pending restart timer and reset back-off (on intentional stop).
   *  Otherwise a delayed start() could fire AFTER the user has stopped. */
  function cancelPendingRecRestart() {
    if (_recRestartTimer) {
      try { clearTimeout(_recRestartTimer); } catch { /* ignore */ }
      _recRestartTimer = null;
    }
    _recRestartBackoff = MIN_REC_RESTART_MS;
  }

  /** Wrap browserRec.start() and record the last-start timestamp.
   *  Swallows "already started" / transient errors (onend will re-schedule). */
  function startBrowserRecNow() {
    if (!browserRec) return;
    // A RESTART (onend → scheduleBrowserRecRestart) reuses the SAME recognizer instance, so its
    // event.results list restarts EMPTY — the committed-final length tracker must reset to 0 too.
    // Otherwise it keeps the prior session's larger length and the restarted session's first final
    // segment computes committedGrew=false, losing the post-final grace extension (the trailing
    // word gets truncated in Chrome's post-final quiet window — the exact bug the grace fixes).
    // The build path resets this at construction; the restart path (no rebuild) needs it here.
    voice._srPrevFinalLen = 0;
    try {
      browserRec.start();
      _lastRecStartAt = Date.now();
    } catch {
      /* ignore — transient; next onend will re-schedule */
    }
  }

  /** SCHEDULE the onend restart according to the back-off window (storm smoothing).
   *  If the window has passed, fire immediately; otherwise DEFER by the remaining time —
   *  single pending timer. In a fast-fail loop (SR listened < 1.5 s then ended) DOUBLE
   *  the back-off; on a healthy turn reset to base. Re-check ALL gates before firing
   *  (shouldRun / half-duplex TTS / mode) — state may have changed during the delay.
   *  Does NOT permanently disable re-arm: if conditions don't hold, the next onend
   *  re-schedules. */
  function scheduleBrowserRecRestart() {
    if (_recRestartTimer) return; // already scheduled
    const ranFor = Date.now() - _lastRecStartAt;
    if (_lastRecStartAt > 0 && ranFor < 1500) {
      _recRestartBackoff = Math.min(_recRestartBackoff * 2, MAX_REC_RESTART_MS);
    } else {
      _recRestartBackoff = MIN_REC_RESTART_MS;
    }
    const wait = Math.max(0, _recRestartBackoff - ranFor);
    const fire = () => {
      _recRestartTimer = null;
      if (!browserRec || !browserRecShouldRun) return;
      if (ttsPlayer.playing || ttsPlayer.queue.length > 0) return; // half-duplex
      const reviveConv = voice.conversationMode && !voice.micMuted && session.isCapturing();
      if (!(voice.micManual || voice.utteranceActive || reviveConv)) return;
      startBrowserRecNow();
    };
    if (wait === 0) fire();
    else _recRestartTimer = setTimeout(fire, wait);
  }

  /** Merge two committed (final) segments, deduplicating overlaps. Android Chrome may
   *  repeat the same final, or send a short segment followed later by the full version;
   *  this fold is idempotent: discards already-contained segments, replaces with the
   *  extension of the accumulator, appends genuinely new segments with a space.
   *  Compares using tr-locale lower-case. */
  function mergeFinal(acc, seg) {
    const a = (acc || "").trim();
    const s = (seg || "").trim();
    if (!s) return a;
    if (!a) return s;
    const al = a.toLocaleLowerCase("tr");
    const sl = s.toLocaleLowerCase("tr");
    if (al.includes(sl)) return a; // seg already in accumulator (exact repeat / nested)
    if (sl.startsWith(al)) return s; // seg is an extension of accumulator → replace all
    return a + " " + s; // genuinely new segment
  }

  /** Return only the portion of the cumulative interim that is BEYOND the final
   *  (Android interim often includes the full final text → prevents double display/send). */
  function interimBeyondFinal(finalLine, interimRaw) {
    const f = (finalLine || "").trim();
    const ir = (interimRaw || "").trim();
    if (!ir) return "";
    if (!f) return ir;
    const fl = f.toLocaleLowerCase("tr");
    const il = ir.toLocaleLowerCase("tr");
    if (il.startsWith(fl)) return ir.slice(f.length).trim(); // strip the final prefix
    if (fl.includes(il)) return ""; // interim is already inside the final
    return ir;
  }

  function startBrowserLiveTranscript(fromLivenessRetry) {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (voice.liveTranscriptUserEdit) return;
    // (ii) SERIALIZE: if the previous recognizer session is still ending (its onend has not
    // landed yet), do NOT build+start a new one now — Chrome returns a silent zombie. Defer
    // off this callstack; the teardown latch is cleared by the old session's onend (or the
    // fallback timer), after which the deferred start proceeds. (Liveness-watchdog retries
    // bypass this — they intentionally recreate a recognizer that already gave up.)
    if (!fromLivenessRetry && _recTeardownPending) {
      scheduleMicSettleStart();
      return;
    }
    stopBrowserLiveTranscript();
    // A fresh (non-retry) start is a new listening turn → reset the recreate budget; retries
    // chain through fromLivenessRetry=true so the budget survives across recreations.
    if (!fromLivenessRetry) _recEngageRetries = 0;
    // Strip is visible on every recording: live text if SR is present, bars otherwise.
    showLiveTranscriptStrip(!!(SR && msg));
    if (!SR || !msg) return;
    browserRecShouldRun = true;
    browserRec = new SR();
    // Fresh SR session → event.results restarts empty, so the committed-final length tracker
    // (post-final grace, see onresult) must reset or a stale length would suppress the grace.
    voice._srPrevFinalLen = 0;
    // Resolve "auto" before assigning SR.lang: "auto" is a Whisper directive, NOT a valid
    // BCP-47 tag — strict engines reject it (error "language-not-supported") and the onend/
    // back-off loop then silently rebuilds a failing recognizer forever. Mirror the wake path
    // (startSpeechWakeFallback), which resolves "auto" from the UI language for exactly this reason.
    const _srLang = speechLang();
    browserRec.lang = _srLang && _srLang !== "auto" ? _srLang : _langLocale();
    browserRec.continuous = true;
    browserRec.interimResults = true;
    browserRec.maxAlternatives = 1;
    // Liveness signal: the first sign the mic is actually engaged (audiostart fires on every
    // healthy session; sound/speech/result confirm audio is flowing). Clears the recreate
    // watchdog and resets the retry budget. A silent zombie fires none of these.
    const recRef = browserRec;
    const markRecEngaged = () => {
      if (recRef) recRef._engaged = true;
      _recEngageRetries = 0;
      cancelRecLivenessWatchdog();
    };
    browserRec.onaudiostart = markRecEngaged;
    browserRec.onsoundstart = markRecEngaged;
    browserRec.onspeechstart = markRecEngaged;
    // Transcript is rebuilt from scratch from event.results on every event, with overlap
    // deduplication. Why index 0: desktop Chrome advances resultIndex correctly, BUT Android
    // Chrome often keeps resultIndex at 0 and sends the same final repeatedly (or a short
    // segment followed by the full sentence) → naïve accumulation produces "what what what
    // what are you doing". Folding all finals with mergeFinal on every event is idempotent
    // (same input → same output): repeats/overlaps are discarded, genuinely new segments
    // are appended. This is the only correct approach for both desktop and mobile.
    browserRec.onresult = (event) => {
      markRecEngaged(); // results mean the mic is live → healthy
      if (voice.liveTranscriptUserEdit) return;
      let finalLine = "";
      let interimRaw = "";
      for (let i = 0; i < event.results.length; i++) {
        const r = event.results[i];
        const seg = (r[0] && r[0].transcript) || "";
        // isFinal: committed portion switches to solid colour; the rest is interim.
        if (r.isFinal) finalLine = mergeFinal(finalLine, seg);
        else interimRaw += seg;
      }
      // On Android, interim can also be cumulative (includes the final text) → strip the final prefix.
      const interimLine = interimBeyondFinal(finalLine, interimRaw);
      const lineTrim = (finalLine + (finalLine && interimLine ? " " : "") + interimLine).trim();
      // In conversation mode do NOT write to the composer (hands-free — text box is unused);
      // deliver the transcript only to the live strip + the Aurora scene (bus).
      if (!voice.conversationMode) {
        msg.value = lineTrim;
        msg.dispatchEvent(new Event("input", { bubbles: true, cancelable: false }));
      }
      updateLiveTranscriptStrip(finalLine.trim(), interimLine.trim());
      emitBus("voice:transcript", { text: lineTrim });
      // CONVERSATION MODE end-of-turn detection via browser SR. Reset silence timer on
      // every result; if no new result arrives for convSilenceMs, send the transcript.
      // CAUTION — this measures the gap between SR *events*, NOT acoustic silence: Chrome
      // does not emit onresult per word, and right after it commits a FINAL segment
      // ("bugünü ve dünü") it can go quiet in its event stream for over a second while the
      // user is still speaking the trailing word ("özetle"). If this window is too short the
      // timer fires INTO that gap, finalizeConversationFromSR stops the recognizer, and the
      // not-yet-recognized tail is discarded — a lost last word with no real pause. The
      // window is therefore deliberately generous (default 1700 ms); it is the only end-of-
      // turn trigger for this path (no reliable onspeechend gate exists cross-engine), so it
      // must out-wait SR's post-final quiet window. NOT tied to Worklet/RMS-VAD.
      //
      // POST-FINAL GRACE (premature end-of-utterance fix): the danger window above is
      // specifically the moment AFTER Chrome commits a FINAL segment — it then stalls its event
      // stream while the user keeps talking. Detect exactly that case (this event GREW the
      // committed/final text) and extend the silence window by convPostFinalGraceMs so the timer
      // out-waits the post-final quiet gap. An interim-only event (user mid-word, no new final)
      // keeps the snappy base window. The timer always fires eventually (no wedge); the moment
      // Chrome resumes emitting, the next onresult re-arms normally. This targets the proven
      // "SR event-gap ≠ acoustic silence" mechanism without a second mic / onspeechend gate.
      const committedGrew = finalLine.length > (voice._srPrevFinalLen || 0);
      voice._srPrevFinalLen = finalLine.length;
      if (voice.conversationMode && lineTrim) {
        voice.convTranscript = lineTrim;
        clearTimeout(voice.convSilenceTimer);
        const baseMs = voice.convSilenceMs || 1700;
        const win = committedGrew ? baseMs + (voice.convPostFinalGraceMs || 800) : baseMs;
        voice.convSilenceTimer = setTimeout(() => {
          const t = voice.convTranscript;
          voice.convTranscript = "";
          void finalizeConversationFromSR(t);
        }, win);
      }
    };
    browserRec.onerror = (event) => {
      // network, no-speech, aborted → silently continue (legacy behaviour: onend restarts).
      // BUT mic permission denial (not-allowed/service-not-allowed) or no microphone
      // (audio-capture) is a PERMANENT error: restarting causes an infinite silent loop.
      // IN CONVERSATION MODE surface this with a visible bubble and exit cleanly
      // (otherwise the user is stuck with an orb that looks "on" but hears nothing — Bug #6).
      const err = (event && event.error) || "";
      const fatalPerm = err === "not-allowed" || err === "service-not-allowed" || err === "audio-capture";
      if (fatalPerm && voice.conversationMode) {
        browserRecShouldRun = false; // prevent onend from restarting
        if (!voice.convPermErrShown) {
          voice.convPermErrShown = true;
          const msgText =
            err === "audio-capture"
              ? _voiceT("voice.err_mic_not_found")
              : _voiceT("voice.err_mic_denied");
          try {
            hooks.appendRow(`<div class="meta">${_voiceT("voice.meta_voice")}</div><div class="bubble-bot">${msgText}</div>`);
          } catch {
            /* ignore */
          }
          try { hooks.setOrb("err"); } catch { /* ignore */ }
        }
        try { exitConversationMode("mic-perm"); } catch { /* ignore */ }
      }
      /* other errors: network/no-speech/aborted — silently continue */
    };
    browserRec.onend = () => {
      // Mobile: screen sleep / system can end SR on its own. Restart not only for
      // manual/PTT and utterance-active states, but also when CONVERSATION MODE is
      // actively listening (conversationMode + capture) — otherwise the loop silently stops.
      // Muted (Aurora "Mute" button) → never revive the conversation recognizer.
      const reviveConv = voice.conversationMode && !voice.micMuted && session.isCapturing();
      // HALF-DUPLEX: do NOT restart mic if audio is actively playing / chunk pending in
      // queue (Akana would hear its own voice → echo loop). We do NOT check ttsStreamOpen
      // here: when the backend tts_end SSE is lost (deadline-flush/abort scenarios) that
      // flag stays stuck and permanently closes the mic. Active drain check is sufficient;
      // re-arm gating is done on the main path.
      const ttsBusy = ttsPlayer.playing || ttsPlayer.queue.length > 0;
      if (ttsBusy) return;
      if (browserRecShouldRun && (voice.micManual || voice.utteranceActive || reviveConv)) {
        // Instead of UNCONDITIONAL start(), use the back-off scheduler: dampens the
        // mobile onend storm (end→start thrashing every second) to a stable cadence.
        scheduleBrowserRecRestart();
      }
    };
    msg.classList.add("input-live");
    // Do not start SpeechRecognition while the tab is hidden: the browser withholds mic
    // audio and immediately ends it, and the onend back-off just spins a dead recognizer.
    // Stay armed (browserRecShouldRun stays true, recognizer built) — the visibilitychange
    // handler rebuilds and starts a fresh one the moment the tab returns to the foreground.
    if (pageHidden()) {
      setLiveStripBarsMode(true);
      return;
    }
    try {
      browserRec.start();
      _lastRecStartAt = Date.now();
    } catch {
      browserRec = null;
      browserRecShouldRun = false;
      msg.classList.remove("input-live");
      // SR could not start — strip continues with bar indicator (final-only).
      setLiveStripBarsMode(true);
      return;
    }
    // Arm the liveness watchdog: if this recognizer never reports audiostart (silent zombie —
    // common when start() lands right after TTS playback / a visibility return), recreate it.
    cancelRecLivenessWatchdog();
    _recLivenessTimer = setTimeout(() => {
      _recLivenessTimer = null;
      const st = {
        engaged: !!(recRef && recRef._engaged),
        replaced: browserRec !== recRef,
        shouldRun: browserRecShouldRun,
        hidden: pageHidden(),
        ttsBusy: ttsPlayer.playing || ttsPlayer.queue.length > 0,
        listening:
          voice.micManual ||
          voice.utteranceActive ||
          (voice.conversationMode && session.isCapturing()),
        retries: _recEngageRetries,
        maxRetries: MAX_REC_ENGAGE_RETRIES,
      };
      if (!shouldRecreateRecognizer(st)) {
        // Distinct diagnostic when we gave up only because the retry budget is spent and the
        // mic is still dead (all other guards passed) — the actionable "mic never engaged" case.
        if (
          !st.engaged && !st.replaced && st.shouldRun && !st.hidden &&
          !st.ttsBusy && st.listening && st.retries >= st.maxRetries
        ) {
          try { console.warn("voice: recognizer never engaged the mic after retries"); } catch { /* ignore */ }
        }
        return;
      }
      _recEngageRetries += 1;
      try { console.debug?.("[sr] no audiostart — recreating recognizer", _recEngageRetries); } catch { /* ignore */ }
      startBrowserLiveTranscript(true); // recreate; preserve the retry budget
    }, REC_ENGAGE_TIMEOUT_MS);
  }

  /* ---------- Voice capture (wake + PTT mic) ---------- */

  // Local flag with the same name as the autostart flag in the settings module —
  // when moved to settings during a refactor it caused a ReferenceError here.
  // Only read for button CSS class ("pending" display).
  let wakeAutostartPending = false;

  const voice = {
    audioCtx: null,
    stream: null,
    processor: null,
    worklet: null,
    workletModuleLoaded: false,
    source: null,
    mute: null,
    inSampleRate: 48000,
    rawBuffer: new Float32Array(0),
    maxRawSeconds: 4.0,
    wakeInterval: null,
    // wakeEnabled / micManual / utteranceActive are NOT stored here — they are DERIVED getters
    // installed right after the session is created (single source of truth = FSM phase).
    utterChunks: [],
    hadSpeech: false,
    silenceMs: 0,
    utterStartTs: 0,
    // Timestamp of the last audio chunk delivered by the worklet (handleAudioChunk). Reset to 0
    // at the start of each whisper listening turn; the capture-phase watchdog treats a stale
    // value as a dead mic feed (revoked mid-turn) and recovers the FSM.
    lastChunkTs: 0,
    wakePollMs: 300,
    wakeWindowSec: 3.0,
    wakeMinRms: 0.001,
    wakeThreshold: 0.25,
    // Server-side openWakeWord scoring is OFF unless /voice/wake/config reports
    // enabled (a custom WAKE_MODEL is configured). Default false → the 300ms server
    // poll is skipped and browser SpeechRecognition ("Hey Akana" phrase) carries wake.
    wakeServerEnabled: false,
    // True while the server is downloading the shared feature models in the background
    // (config reports status:"preparing"). enabled is false meanwhile but flips true on
    // a later poll; used to suppress the "browser fallback" notice during the warm-up.
    wakeServerPreparing: false,
    // Bounded re-poll timer used ONLY while model wake was requested but the server is still
    // preparing: it re-fetches /voice/wake/config until scoring flips ready, then hands wake
    // back to the server poll (stopping the browser-SR warm-up fallback). Cleared on wake-off.
    wakePrepareRepoll: null,
    lastWakeScore: null,
    wakeMeterTick: 0,
    wakeErrShown: false,
    utterMaxMs: 12000,
    // Hard ceiling (seconds of audio) for utterChunks when conversation mode disables the
    // RMS-VAD auto-finalize cap (see akana-voice-capture.js handleAudioChunk): bounds memory
    // growth during a stuck/silent/muted listening turn where SR never delivers a result.
    utterMaxSeconds: 120,
    // End-of-utterance silence threshold: the turn closes after this much silence.
    // Lowered (900→650) to speed up turn-taking; a trade-off between the risk of early
    // cuts on intra-sentence pauses and immediate responsiveness. Used by WAKE capture.
    silenceHoldMs: 650,
    // Whisper-STT CONVERSATION mode uses a more forgiving hold than wake: Turkish sentences
    // often draw breath before the sentence-final verb, and a trailing-off quiet last word
    // can read as silence — 650 ms clips it (same lost-trailing-word symptom as browser SR).
    // Only applies when sttSource="whisper" (convVadEnabled). See akana-voice-capture.js.
    convSilenceHoldMs: 900,
    noSpeechTimeoutMs: 4000,
    voiceRms: 0.02,
    ambientRms: 0,
    ambientSamplesNeeded: 5, // ~213ms at the worklet's 2048-sample batch (48kHz)
    ambientSamplesCollected: 0,
    cancelled: false,
    wakeCooldownUntil: 0,
    wakeInFlight: false,
    postInFlight: false,
    voiceFetchAbort: null,
    utterFinishing: false,
    ttsPlaying: false,
    wakeMeterHideTimer: null,
    /** User edited composer during capture; stop live transcript overwrite. */
    liveTranscriptUserEdit: false,
    /** Hands-free conversation mode: toggled by button; independent of wake/PTT. */
    conversationMode: false,
    /** Was «Hey Akana» listening active when conversation mode was entered? Because
     *  conversation mode is the sole owner of the microphone, we suspend wake on entry;
     *  on exit we restore the user's state via this flag — exiting voice mode must NOT
     *  silently turn off wake listening (the user only closed conversation). If entered
     *  via mic (with wake off), false → do not re-enable. */
    wakeBeforeConversation: false,
    /** Is barge-in (interrupting Akana while it is speaking) ENABLED? Set in init()
     *  from localStorage akana.bargeIn ("1" default). When off, conversation mode
     *  remains UNCHANGED half-duplex (detector is never opened). */
    bargeInEnabled: false,
    /** Conversation mode end-of-turn detection via browser SR (not Worklet/RMS-VAD).
     *  DEFAULT path (sttSource="browser"): SR silence timer drives the turn; the Worklet
     *  RMS-VAD auto-finalize is OFF (convVadEnabled=false). Whisper path
     *  (sttSource="whisper"): convVadEnabled is set true at capture time so
     *  akana-voice-capture.js runs the RMS-VAD auto-finalize → /voice/transcribe. */
    convVadEnabled: false,
    convSilenceTimer: null,
    // Browser-SR end-of-turn window (ms). Measures the gap between SR *events*, not acoustic
    // silence, so it must out-wait Chrome's post-final quiet window (see onresult) — 1300 was
    // short enough to truncate the trailing word of a continuously-spoken sentence. Configurable
    // via localStorage akana.convSilenceMs (1000..4000); lower for snappier turn-taking.
    convSilenceMs: 1700,
    // Extra grace (ms) added to the SR silence window ONLY when the arming onresult just
    // committed a new FINAL segment — Chrome's post-final quiet window is exactly when the
    // trailing word gets truncated (premature end-of-utterance). Interim-ending arming keeps the
    // base convSilenceMs. See browserRec.onresult.
    convPostFinalGraceMs: 800,
    convTranscript: "",
    /** Turn was sent, awaiting response + TTS — do NOT re-enter listening during this
     *  period (otherwise the scene was falling back to "Listening" instead of "Thinking").
     *  Cleared when TTS finishes. */
    convAwaitingReply: false,
    /** Recovery watchdog: if the turn gets stuck (network/hang), rescue the loop on timeout. */
    /** Mobile: SR onerror "not-allowed/service-not-allowed/audio-capture" (mic permission
     *  denied / no microphone) was silently swallowed → mode looks "on" but hears nothing,
     *  no feedback to the user. This flag shows the bubble ONCE per entry (prevents spam in
     *  the onend→restart loop) and exits the mode cleanly. */
    convPermErrShown: false,
    /** Whisper-mode capture-phase safety timeout: if a listening turn produces ZERO audio
     *  chunks within a bounded window (worklet mic never opened — e.g. ensureAudio rejected),
     *  the RMS-VAD auto-finalize never runs and finalizeConversationFromSR is suppressed, so
     *  nothing finalizes and the FSM stays stuck on "Listening". This timer force-recovers.
     *  Only armed in whisper mode (convVadEnabled); browser mode leaves it null. */
    convCaptureWatchdog: null,
    /** Mic muted by the Aurora "Mute"/"Sustur" button (bus event voice:mic:mute).
     *  Conversation mode is browser SpeechRecognition which OWNS the mic — the voice.mute
     *  GainNode does NOT affect SR — so muting must STOP the recognizer and every re-arm /
     *  restart path is gated on !micMuted. Reset on exitConversationMode / unmute. */
    micMuted: false,
  };

  const { Phase: VPhase } = window.AkanaVoiceFsm;
  const session = window.AkanaVoiceFsm.createVoiceSession({
    onTransition(from, to) {
      if (
        (from === VPhase.CAPTURE_WAKE || from === VPhase.CAPTURE_MIC) &&
        to !== VPhase.CAPTURE_WAKE &&
        to !== VPhase.CAPTURE_MIC
      ) {
        voice.utterChunks = [];
        voice.hadSpeech = false;
        voice.silenceMs = 0;
        voice.ambientRms = 0;
        voice.ambientSamplesCollected = 0;
        voice.utterFinishing = false;
        stopBrowserLiveTranscript();
        resetMicButtonUi();
      }
      if (to === VPhase.CAPTURE_WAKE || to === VPhase.CAPTURE_MIC) {
        stopSpeechWakeFallback();
      } else if (to === VPhase.WAKE_ARMED && session.isWakeArmed()) {
        void ensureWakePipeline();
      }
      syncVoiceUi();
    },
  });

  // Legacy capture flags are DERIVED from the FSM phase (the single source of truth) via getters,
  // so they can NEVER drift from session.getPhase(). Previously they were plain fields mirrored by
  // applySessionToLegacyFlags() after every transition — drift-prone: a missed call, or a direct
  // write (setWakeListening used to set micManual=false without a transition), left the flag lying
  // about the real phase. Getters remove that whole class of bug. Audited: no code writes these;
  // every reader (voice.js / capture.js / pipeline.js / settings.js) hits the getter live.
  Object.defineProperty(voice, "wakeEnabled", {
    get: () => session.isWakeArmed(),
    enumerable: true,
    configurable: true,
  });
  Object.defineProperty(voice, "utteranceActive", {
    get: () => session.isCaptureWake(),
    enumerable: true,
    configurable: true,
  });
  Object.defineProperty(voice, "micManual", {
    get: () => session.isCaptureMic(),
    enumerable: true,
    configurable: true,
  });

  let voiceCapture = null;
  let voicePipeline = null;

  function buildVoiceBridge() {
    return {
      get hooks() { return hooks; },
      get voice() { return voice; },
      get session() { return session; },
      get VPhase() { return VPhase; },
      get ttsPlayer() { return ttsPlayer; },
      get msg() { return msg; },
      get ttsToggle() { return ttsToggle; },
      LS_MIC_DEVICE: LS_MIC_DEVICE,
      speechLang,
      ttsLangFromSpeech,
      startBrowserLiveTranscript,
      stopBrowserLiveTranscript,
      syncVoiceUi,
      syncOrbWithVoice,
      updateWakeMeter,
      scheduleSpeechWakeRestart,
      stopSpeechWakeFallback,
      resetMicButtonUi,
      voiceEpochMatches,
      getTtsEnabled: () => ttsEnabled,
      setTtsEnabled: (v) => {
        ttsEnabled = !!v;
      },
      applyVoicePreferencesFromServer: (p) => ensureVoiceSettings().applyVoicePreferencesFromServer(p),
      loadVoicePreferences: () => loadVoicePreferences(),
      saveVoicePreferences: (p) => saveVoicePreferences(p),
      syncWakeButtonUi: (on) => syncWakeButtonUi(on),
      voice,
      ttsPlayer,
      ttsToggle,
      refreshMicDeviceList: () => ensureVoiceSettings().refreshMicDeviceList(),
      getWakeAutostartEnabled: () => ensureVoiceSettings().getWakeAutostartEnabled(),
      setWakeListening: (on, opts) => setWakeListening(on, opts),
      stopAudioGraph: () => stopAudioGraph(),
      wakeDebugEnabled: () => {
        try { return localStorage.getItem("akana_wake_debug") === "1"; } catch { return false; }
      },
      finalizeUtterance: () => voicePipeline.finalizeUtterance(),
      postVoiceBlob: (b) => voicePipeline.postVoiceBlob(b),
      emitBus,
      maybeReArmConversation,
      armConvWatchdog: () => armConvWatchdog(),
      playEarcon: (name) => playEarcon(name),
      onConversationBargeIn,
      enterConversationMode: (reason) => enterConversationMode(reason),
      exitConversationMode: (reason) => exitConversationMode(reason),
      isConversationExitPhrase: (text) => isConversationExitPhrase(text),
      isConversationMode,
    };
  }

  function ensureVoiceModules() {
    if (!voicePipeline) {
      voicePipeline = window.AkanaVoicePipeline.create(buildVoiceBridge());
    }
    if (!voiceCapture) {
      voiceCapture = window.AkanaVoiceCapture.createCapture(buildVoiceBridge());
    }
    return { capture: voiceCapture, pipeline: voicePipeline };
  }

  const ensureAudio = () => ensureVoiceModules().capture.ensureAudio();
  const stopAudioGraph = () => ensureVoiceModules().capture.stopAudioGraph();
  const loadWakeConfig = () => ensureVoiceModules().pipeline.loadWakeConfig();
  const onWakeTriggered = (s) => ensureVoiceModules().pipeline.onWakeTriggered(s);
  const pollWakeOnce = () => ensureVoiceModules().pipeline.pollWakeOnce();
  const postVoiceBlob = (b) => ensureVoiceModules().pipeline.postVoiceBlob(b);
  const finalizeUtterance = () => ensureVoiceModules().pipeline.finalizeUtterance();

  let voiceSettings = null;

  function ensureVoiceSettings() {
    if (!voiceSettings) voiceSettings = window.AkanaVoiceSettings.create(buildVoiceBridge());
    return voiceSettings;
  }

  const saveVoicePreferences = (p) => ensureVoiceModules().pipeline.saveVoicePreferences(p);
  const loadVoicePreferences = () => ensureVoiceModules().pipeline.loadVoicePreferences();
  const formatApiError = (body, fb) => ensureVoiceModules().pipeline.formatApiError(body, fb);


  function voiceEpochMatches(epoch) {
    return session.epochMatches(epoch);
  }

  function resetMicButtonUi() {
    if (!btnMic) return;
    btnMic.classList.remove("active");
    btnMic.setAttribute("aria-pressed", "false");
  }

  function discardWakeCaptureSilently(reason = "discardWake") {
    if (!session.isCaptureWake() && !session.isCaptureMic()) return false;
    voice.cancelled = true;
    voice.utterFinishing = false;
    voice.hadSpeech = false;
    voice.silenceMs = 0;
    voice.ambientRms = 0;
    voice.ambientSamplesCollected = 0;
    voice.utterChunks = [];
    stopBrowserLiveTranscript();
    session.transition(
      session.isWakeArmed() ? VPhase.WAKE_ARMED : VPhase.IDLE,
      reason,
      { force: true },
    );
    resetMicButtonUi();
    if (session.isWakeArmed()) void ensureWakePipeline();
    syncVoiceUi();
    return true;
  }

  /** Text chat starting while wake/mic capture is open — no "Cancelled" bubble, no wake cooldown. */
  function handoffToTextChat() {
    if (discardWakeCaptureSilently("textChat")) return true;
    if (voice.postInFlight || voice.utterFinishing) {
      voice.cancelled = true;
      voice.utterFinishing = false;
      if (voice.voiceFetchAbort) {
        try {
          voice.voiceFetchAbort.abort();
        } catch {
          /* ignore */
        }
        voice.voiceFetchAbort = null;
      }
      voice.postInFlight = false;
      session.cancelAll("textChat:voicePost");
      if (session.isWakeArmed()) void ensureWakePipeline();
      syncVoiceUi();
      return true;
    }
    if (ttsPlayer.playing) {
      try {
        ttsPlayer.reset();
      } catch {
        /* ignore */
      }
      syncVoiceUi();
      return true;
    }
    return false;
  }

  let ttsEnabled = false;
  const ttsToggle = document.getElementById("tts-stream-toggle");
  // PROGRESS WATCHDOG threshold: if a TTS <audio> chunk reports NO progress (timeupdate)
  // for this long it is considered "stalled" and the next chunk is advanced. Chunks are
  // Blob-URLs (fully downloaded) so currentTime normally advances every frame;
  // 2.5 s of silence is therefore a genuine stall (not network buffering).
  const TTS_STALL_MS = 2500;

  const ttsPlayer = {
    queue: [],
    playing: false,
    audio: null,
    nextUrl: null,
    nextAudio: null,
    // True while the current chunk is paused because the tab went to the background
    // (holdForHidden). resumeAfterVisible resumes it and clears this on return.
    _pausedForHidden: false,
    /* ---------- Real-audio energy tap (for Aurora waveform) ----------
       Connects the playing TTS <audio> element to a WebAudio AnalyserNode
       (createMediaElementSource → analyser → destination) and emits the RMS level
       (0‥1) on every frame as a `voice:energy` event. The Aurora scene drives its
       waveform amplitude from this live level (falls back to a synthetic envelope
       if no event arrives).

       CRITICAL: createMediaElementSource REROUTES the output — the analyser MUST
       be connected to destination or TTS audio will be silenced. Also, a media
       element can only be sourced ONCE → mark per element (_energySourced).
       Silently disabled if WebAudio is unavailable / fails (playback unaffected). */
    _energy: {
      ctx: null,
      analyser: null,
      buf: null,
      raf: null,
      sourced: null, // WeakSet: <audio> elements that have been sourced once
      failed: false,
      _ensureCtx() {
        if (this.failed) return false;
        if (this.ctx && this.analyser) return true;
        try {
          const AC = window.AudioContext || window.webkitAudioContext;
          if (!AC) {
            this.failed = true;
            return false;
          }
          this.ctx = new AC();
          this.analyser = this.ctx.createAnalyser();
          this.analyser.fftSize = 512;
          // Analyser → destination: the measurement path is also the route by which
          // audio reaches the speaker (media-element source output is directed here).
          this.analyser.connect(this.ctx.destination);
          this.buf = new Float32Array(this.analyser.fftSize);
          this.sourced = typeof WeakSet === "function" ? new WeakSet() : null;
          return true;
        } catch (e) {
          this.failed = true;
          this.ctx = null;
          this.analyser = null;
          try { console.debug?.("[energy] tap init failed:", e?.name || e); } catch { /* ignore */ }
          return false;
        }
      },
      /** Attach the playing <audio> element to the analyser and start the measurement loop. */
      attach(audioEl) {
        if (!audioEl || !this._ensureCtx()) return;
        try {
          if (this.ctx.state === "suspended") this.ctx.resume().catch(() => {});
          // A media element can only be sourced ONCE; trying again throws.
          const already = this.sourced ? this.sourced.has(audioEl) : audioEl._energySourced === true;
          if (!already) {
            const src = this.ctx.createMediaElementSource(audioEl);
            src.connect(this.analyser);
            // Keep a handle so onDone can disconnect this per-chunk source when the
            // chunk finishes — otherwise every spoken sentence leaks a source node
            // (and its backing <audio>) into the analyser graph for the page lifetime.
            audioEl._energySrc = src;
            if (this.sourced) this.sourced.add(audioEl);
            else audioEl._energySourced = true;
          }
          this._startLoop();
        } catch (e) {
          // This element may already be sourced, or WebAudio failed → drop measurement
          // but NEVER block playback.
          try { console.debug?.("[energy] attach failed:", e?.name || e); } catch { /* ignore */ }
        }
      },
      /** Disconnect and drop a finished chunk's media-element source node so the node
       *  (and the <audio> element it keeps referenced in the audio graph) can be GC'd.
       *  A media element can be sourced only once, so a released element is never
       *  re-attached — the chunk is done for good when this runs. */
      release(audioEl) {
        if (!audioEl || !audioEl._energySrc) return;
        try { audioEl._energySrc.disconnect(); } catch { /* ignore */ }
        audioEl._energySrc = null;
      },
      _measureRms() {
        const a = this.analyser;
        if (!a || !this.buf) return 0;
        if (typeof a.getFloatTimeDomainData === "function") {
          a.getFloatTimeDomainData(this.buf);
          let s = 0;
          for (let i = 0; i < this.buf.length; i++) s += this.buf[i] * this.buf[i];
          return Math.sqrt(s / this.buf.length);
        }
        const bytes = new Uint8Array(a.fftSize);
        a.getByteTimeDomainData(bytes);
        let s = 0;
        for (let i = 0; i < bytes.length; i++) {
          const v = (bytes[i] - 128) / 128;
          s += v * v;
        }
        return Math.sqrt(s / bytes.length);
      },
      _startLoop() {
        if (this.raf != null) return;
        const tick = () => {
          if (!ttsPlayer.playing) {
            this.raf = null;
            return;
          }
          // RMS is in the ~0–0.4 range; scale speech energy to a visible 0–1
          // amplitude (soft saturation).
          const rms = this._measureRms();
          let level = rms * 2.6;
          if (level > 1) level = 1;
          else if (level < 0) level = 0;
          emitBus("voice:energy", { level });
          this.raf = requestAnimationFrame(tick);
        };
        this.raf = requestAnimationFrame(tick);
      },
      stop() {
        if (this.raf != null) {
          try { cancelAnimationFrame(this.raf); } catch { /* ignore */ }
          this.raf = null;
        }
      },
    },
    _prepareNext() {
      if (this.nextAudio || !this.queue.length) return;
      this.nextUrl = this.queue[0];
      this.nextAudio = new Audio(this.nextUrl);
      this.nextAudio.preload = "auto";
    },
    /** Snapshot of the accept-gen — a stream captures this on the FIRST tts frame
     *  and carries it through subsequent enqueue calls (see transport tts_chunk). */
    acceptGen() {
      return this._acceptGen || 0;
    },
    async enqueue(b64, mime, gen) {
      // GEN GATE: if the caller provided a feed generation and it does not match the
      // current _acceptGen, this frame is from a cancelled turn (barge-in/STOP/new turn
      // reset() bumped the generation) → DROP. This prevents a single late SSE chunk from
      // triggering playNext and restarting the old reply. If gen is not provided
      // (legacy path / stream-agnostic call), old behaviour: unconditional accept.
      if (gen != null && gen !== (this._acceptGen || 0)) return;
      try {
        const bin = atob(b64);
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        const blob = new Blob([bytes], { type: mime || "audio/wav" });
        const url = URL.createObjectURL(blob);
        this.queue.push(url);
        this._prepareNext();
        if (!this.playing) this.playNext();
      } catch (e) {
        console.warn("TTS chunk decode failed:", e);
      }
    },
    playNext() {
      if (!this.queue.length) {
        const wasPlaying = this.playing;
        this.playing = false;
        this.nextUrl = null;
        this.nextAudio = null;
        try { this._energy.stop(); } catch { /* ignore */ }
        try { updateVoiceStopButton(); } catch { /* ignore */ }
        if (wasPlaying) emitBus("voice:tts:end");
        // Queue drained — but this does NOT mean "turn is done": the queue also drains
        // in the gap between sentences while the reply is still streaming. Re-enter
        // listening only when the backend sends `tts_end` (voice:tts:streamEnd); wait otherwise.
        finishConversationTurnIfTtsDone("ttsDrain");
        return;
      }
      // HOLD WHILE HIDDEN: a backgrounded tab suspends/throttles audio, so a chunk started
      // here never actually plays (no `timeupdate`) and the stall watchdog would skip the
      // whole reply unheard — the user returns to a mic already re-armed to "Listening"
      // without ever hearing the answer. Instead leave the chunks queued (a non-empty queue
      // keeps convAwaitingReply set: finishConversationTurnIfTtsDone / maybeReArmConversation
      // both wait on queue.length) and let resumeAfterVisible() kick playback the moment the
      // tab is foregrounded, so the reply is spoken to a user who is actually there. Do NOT
      // flip `playing` or emit tts:start yet — speaking begins only once visible.
      if (pageHidden()) return;
      const wasPlaying = this.playing;
      this.playing = true;
      try { updateVoiceStopButton(); } catch { /* ignore */ }
      if (!wasPlaying) emitBus("voice:tts:start");
      const url = this.queue.shift();
      if (this.nextAudio && this.nextUrl === url) {
        this.audio = this.nextAudio;
        this.nextAudio = null;
        this.nextUrl = null;
      } else {
        if (this.nextAudio) {
          try {
            this.nextAudio.pause();
          } catch {
            /* ignore */
          }
        }
        this.nextAudio = null;
        this.nextUrl = null;
        this.audio = new Audio(url);
      }
      this._prepareNext();
      // Capture THIS chunk's element: this.audio is reassigned to the next chunk by
      // playNext(), so onDone must release the element it actually attached.
      const chunkAudio = this.audio;
      const gen = this._gen || 0;
      let finished = false;
      let stallTimer = null;
      let lastProgressTs = Date.now();
      const clearStall = () => {
        if (stallTimer != null) {
          try { clearInterval(stallTimer); } catch { /* ignore */ }
          stallTimer = null;
        }
      };
      const onDone = () => {
        // IDEMPOTENT: onended / onerror / play().catch / progress watchdog
        // can all race — only the FIRST trigger counts, the rest are no-ops.
        if (finished) return;
        finished = true;
        clearStall();
        try { URL.revokeObjectURL(url); } catch { /* ignore */ }
        // Release this chunk's energy-tap source node so the node + its <audio> can be
        // GC'd (unconditional, like the URL revoke — an abandoned old-gen chunk leaks too).
        try { this._energy.release(chunkAudio); } catch { /* ignore */ }
        // Stale callback guard: if reset() (new turn / barge-in) bumped _gen,
        // this onDone belongs to an OLD generation. Do NOT call playNext — otherwise
        // the stopped old audio restarts on top of the new turn (Akana keeps talking
        // after the user interrupted).
        if (gen !== (this._gen || 0)) return;
        this.playNext();
      };
      this.audio.onended = this.audio.onerror = onDone;
      // PROGRESS WATCHDOG: while a Blob-URL chunk is playing, currentTime advances
      // every frame → timeupdate fires regularly. When the tab is backgrounded, the
      // decoder stalls, or autoplay is silently paused, onended/onerror NEVER fires;
      // playing=true then hangs forever → mic never reopens (scene freezes on
      // "Responding") AND remaining chunks never play (reply cuts off halfway).
      // Refresh the timestamp on every timeupdate/playing event; if TTS_STALL_MS
      // passes with no progress, count it as stalled and advance via onDone.
      const bumpProgress = () => { lastProgressTs = Date.now(); };
      this.audio.ontimeupdate = this.audio.onplaying = bumpProgress;
      stallTimer = setInterval(() => {
        if (finished) { clearStall(); return; }
        // If reset() moved to a new generation, this chunk is abandoned → close the
        // watchdog (reset() handles its own teardown; we only prevent interval leaks).
        if (gen !== (this._gen || 0)) { clearStall(); return; }
        // Tab hidden: the browser pauses/suspends this element so `timeupdate` stops firing.
        // That is NOT a real stall — skipping here would drop the chunk unheard. Keep the
        // progress baseline fresh; holdForHidden paused it and resumeAfterVisible restarts it.
        if (pageHidden()) { lastProgressTs = Date.now(); return; }
        const a = this.audio;
        if (a && a.ended) { clearStall(); return; }
        if (Date.now() - lastProgressTs > TTS_STALL_MS) {
          try { console.debug?.("[tts] chunk stalled, skipping to next"); } catch { /* ignore */ }
          onDone();
        }
      }, 700);
      // Attach the playing element to the real-audio energy tap (Aurora waveform).
      // Never blocks playback — silently skips on failure.
      try { this._energy.attach(this.audio); } catch { /* ignore */ }
      void this.audio.play().catch(onDone);
    },
    reset() {
      // Bump the generation: in-flight onDone callbacks (onended/onerror/play
      // catch) now belong to the old generation and cannot trigger playNext (see playNext).
      this._gen = (this._gen || 0) + 1;
      // ACCEPT generation (accept-gen): bumped whenever barge-in / STOP / new turn
      // calls reset(). If the stream feed generation (captured in streamCtx) does not
      // match, the enqueue is dropped → a LATE SSE tts_chunk from a cancelled reply
      // (landed in the read buffer + catch's await flushSseQueue ASYNC drain) cannot
      // restart old audio on top of the new listening turn.
      // _gen only stops the stale onDone chain; this covers new enqueue calls too.
      this._acceptGen = (this._acceptGen || 0) + 1;
      if (this.audio) {
        try {
          this.audio.onended = this.audio.onerror = null;
          this.audio.pause();
        } catch {
          /* ignore */
        }
        // onDone won't fire for an abandoned in-flight chunk (handlers nulled above,
        // watchdog bails on the gen bump) → release its energy-tap node here so it leaks.
        try { this._energy.release(this.audio); } catch { /* ignore */ }
        this.audio = null;
      }
      if (this.nextAudio) {
        try {
          this.nextAudio.pause();
        } catch {
          /* ignore */
        }
        this.nextAudio = null;
        this.nextUrl = null;
      }
      this.queue.forEach((u) => URL.revokeObjectURL(u));
      const wasPlaying = this.playing;
      this.queue = [];
      this.playing = false;
      try { this._energy.stop(); } catch { /* ignore */ }
      try { updateVoiceStopButton(); } catch { /* ignore */ }
      if (wasPlaying) emitBus("voice:tts:end");
      // This turn's TTS was abandoned (barge-in / new turn / cancel) →
      // Clear the "more audio coming" expectation so the next re-arm doesn't get stuck on the flag.
      voice.ttsStreamOpen = false;
      try { resumeWakeListeningIfIdle(); } catch { /* ignore */ }
      try { maybeReArmConversation("ttsReset"); } catch { /* ignore */ }
    },
    /** Tab went to the background: pause the current chunk so the reply is not spoken to an
     *  unattended tab, and can resume from the same spot on return. `playing` stays true so the
     *  turn's re-arm remains blocked; chunks that arrive later are held by playNext's hidden gate. */
    holdForHidden() {
      if (this.playing && this.audio) {
        try { this.audio.pause(); } catch { /* ignore */ }
        this._pausedForHidden = true;
      }
    },
    /** Tab returned to the foreground: reacquire a suspended AudioContext, resume the paused
     *  chunk, and kick any queue that playNext held while hidden — so a reply that landed while
     *  the user was away is spoken now instead of being silently skipped straight into "Listening". */
    resumeAfterVisible() {
      // Safari/iOS (and some desktop cases) suspend the AudioContext in the background →
      // resume it or the reply stays silent even once we call play().
      try {
        const ctx = this._energy?.ctx;
        if (ctx && ctx.state === "suspended") void ctx.resume().catch(() => {});
      } catch { /* ignore */ }
      const wasPaused = this._pausedForHidden;
      this._pausedForHidden = false;
      // Do NOT resume+return on a FINISHED element: if a chunk's `ended` fired while hidden,
      // playNext hit the hidden gate and returned with playing=true and this.audio still pointing
      // at that finished chunk (whose handlers are the already-consumed onDone no-op and whose blob
      // URL is revoked). play()+return here would replay a dead element and never reach the queue
      // kick, wedging playing=true with queued chunks forever (convWatchdog treats playing=true as
      // busy → no rescue). Only resume a genuinely paused, unfinished chunk; otherwise fall through
      // to advance the queue.
      if (wasPaused && this.audio && !this.audio.ended) {
        try { void this.audio.play().catch(() => {}); } catch { /* ignore */ }
        return;
      }
      // The held/finished chunk is done → advance. If playNext left playing=true on a finished
      // element (its `ended` fired under the hidden gate), drop it so playNext isn't short-circuited
      // by the stale playing=true and runs its drain-or-play decision: play the next queued chunk,
      // or (empty queue) finish the turn (finishConversationTurnIfTtsDone) — instead of hanging on
      // "Responding" forever with convWatchdog treating playing=true as busy.
      const stuckFinished = this.audio && this.audio.ended;
      if (stuckFinished) {
        this.playing = false;
        this.audio = null;
      }
      // Chunks arrived while hidden and playNext held them (playing never flipped), OR we just
      // cleared a wedged finished chunk → advance the queue / drain the turn.
      if (!this.playing && (this.queue.length || stuckFinished)) this.playNext();
    },
  };

  /* ---------- Barge-in detector (separate AEC mic stream) ----------
     A dedicated getUserMedia stream CANNOT be fed to browser SpeechRecognition,
     so conversation mode is half-duplex (SR does not listen while Akana speaks).
     To enable barge-in: while Akana TTS is playing, a SEPARATE mic stream with
     AEC/NS/AGC enabled + a WebAudio AnalyserNode measures mic RMS energy.
     AEC cancels Akana's own speaker output → only the USER's voice raises energy.
     When ~250 ms of continuous energy above threshold is detected, onConversationBargeIn()
     is called (cut TTS + cancel turn + open new capture). Stream is released when TTS ends.

     SELF-INTERRUPT PROTECTION: the key is AEC (Akana cannot hear itself) +
     sustain window (single click noise doesn't count). The raw-mic dormant path
     in akana-voice-capture.js handleAudioChunk is therefore left DISABLED. */
  const bargeDetector = {
    stream: null,
    audioCtx: null,
    source: null,
    analyser: null,
    buf: null,
    raf: null,
    starting: false,
    active: false,
    voiceMs: 0,
    lastTs: 0,
    startTs: 0,
    /** Energy threshold (RMS). After AEC, own TTS SHOULD drop to ~0.005, but on SPEAKERS
     *  (vs headphones) browser AEC frequently fails to cancel Akana's own output → it leaks
     *  ABOVE a low threshold and self-interrupts the answer mid-sentence. History:
     *  0.045→0.03→0.025 chased "too hard to trigger" and caused exactly that. Barge-in is now
     *  OPT-IN (default off), so the threshold is biased UP (0.05) to resist self-barge; users
     *  who enable it can lower it via Settings → Voice / localStorage akana.bargeRms (lower =
     *  more sensitive, raise if it still self-interrupts). */
    rms: 0.05,
    /** Barge-in is triggered when energy stays above threshold for this long (ms).
     *  250→150: in real data the user's interruption voice reached voiceMs≈160 and was
     *  reset at the next tts:start (before reaching 250) → barge never triggered.
     *  150 catches natural interruptions; residual (~0.005) is MUCH below threshold (0.045)
     *  so false-trigger risk is low. Adjustable via localStorage akana.bargeHoldMs. */
    holdMs: 150,
    /** Grace window after TTS start (ms): ignore barge until browser AEC converges and the
     *  TTS onset transient passes — otherwise the detector self-triggers on Akana's own first
     *  syllables before echo cancellation settles. Override via localStorage akana.bargeGraceMs. */
    graceMs: 600,
    _threshold() {
      try {
        const v = Number(localStorage.getItem("akana.bargeRms"));
        if (v > 0 && v < 1) return v;
      } catch { /* ignore */ }
      return this.rms;
    },
    _holdMs() {
      try {
        const v = Number(localStorage.getItem("akana.bargeHoldMs"));
        if (v >= 50 && v <= 2000) return v;
      } catch { /* ignore */ }
      return this.holdMs;
    },
    _graceMs() {
      try {
        const v = Number(localStorage.getItem("akana.bargeGraceMs"));
        if (v >= 0 && v <= 3000) return v;
      } catch { /* ignore */ }
      return this.graceMs;
    },
    /** Called when TTS starts in conversation mode with barge-in enabled. */
    async start() {
      // Gate snapshot: check BEFORE the getUserMedia await (including ttsPlayer.playing and
      // micMuted — a muted user must not have the barge mic opened). Rechecked after resolution.
      if (!voice.bargeInEnabled || !voice.conversationMode || !ttsPlayer.playing || voice.micMuted) return;
      if (this.active || this.starting) return;
      this.starting = true;
      // Supersession snapshot (V6): if a stop() runs during the getUserMedia await (e.g. barge
      // turned OFF, or teardown), it bumps _startToken → this resolution is stale and must self-release.
      const myToken = this._startToken || 0;
      try {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!AC || !navigator.mediaDevices?.getUserMedia) {
          this.starting = false;
          return;
        }
        const baseConstraints = {
          channelCount: 1,
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        };
        const preferredMic = localStorage.getItem(LS_MIC_DEVICE) || "";
        let stream;
        try {
          const c = preferredMic
            ? { ...baseConstraints, deviceId: { exact: preferredMic } }
            : baseConstraints;
          stream = await navigator.mediaDevices.getUserMedia({ audio: c });
        } catch (errPref) {
          // Preferred device may be disconnected/inaccessible (OverconstrainedError etc.)
          // → try once more with the default microphone; throw if that also fails.
          if (!preferredMic) throw errPref;
          try { console.debug?.("[barge] preferred mic failed, falling back to default:", errPref?.name || errPref); } catch { /* ignore */ }
          stream = await navigator.mediaDevices.getUserMedia({ audio: baseConstraints });
        }
        // If TTS ended / mode closed / muted / SUPERSEDED by a later stop+start in the meantime,
        // release this just-resolved stream immediately (don't clobber a newer detector's stream).
        if (
          !voice.bargeInEnabled ||
          !voice.conversationMode ||
          !ttsPlayer.playing ||
          voice.micMuted ||
          myToken !== this._startToken
        ) {
          stream.getTracks().forEach((t) => t.stop());
          if (myToken === this._startToken) this.starting = false; // only if still the current start
          return;
        }
        this.stream = stream;
        this.audioCtx = new AC();
        if (this.audioCtx.state === "suspended") {
          try { await this.audioCtx.resume(); } catch { /* ignore */ }
        }
        this.source = this.audioCtx.createMediaStreamSource(stream);
        this.analyser = this.audioCtx.createAnalyser();
        this.analyser.fftSize = 1024;
        // Connect AnalyserNode to source ONLY; do NOT connect to destination
        // (no echo / double output — measurement only).
        this.source.connect(this.analyser);
        this.buf = new Float32Array(this.analyser.fftSize);
        this.voiceMs = 0;
        this._silenceMs = 0;
        this.lastTs = (typeof performance !== "undefined" ? performance.now() : Date.now());
        this.startTs = this.lastTs;
        this.active = true;
        this.starting = false;
        try {
          if (localStorage.getItem("akana.bargeDebug") === "1") {
            console.info("[barge] mic ACQUIRED — detector active (now attempting barge-in)");
          }
        } catch { /* ignore */ }
        this._tick();
      } catch (e) {
        // Permission denied / no device → silently fall back to half-duplex behaviour.
        this.starting = false;
        this.active = false;
        if (this.stream) {
          try { this.stream.getTracks().forEach((t) => t.stop()); } catch { /* ignore */ }
          this.stream = null;
        }
        // Barge could not start = barge DOES NOT WORK. Most common cause: a SECOND
        // getUserMedia stream cannot be opened while the main capture mic is active.
        // Left as visible (warn) — the critical evidence for "barge never works"
        // diagnosis (was console.debug, too hidden).
        try { console.warn("[barge] detector start FAILED:", e?.name || e?.message || e); } catch { /* ignore */ }
      }
    },
    _measureRms() {
      const a = this.analyser;
      if (!a || !this.buf) return 0;
      // getFloatTimeDomainData is not available in every engine; fall back to byte path.
      if (typeof a.getFloatTimeDomainData === "function") {
        a.getFloatTimeDomainData(this.buf);
        let s = 0;
        for (let i = 0; i < this.buf.length; i++) s += this.buf[i] * this.buf[i];
        return Math.sqrt(s / this.buf.length);
      }
      const bytes = new Uint8Array(a.fftSize);
      a.getByteTimeDomainData(bytes);
      let s = 0;
      for (let i = 0; i < bytes.length; i++) {
        const v = (bytes[i] - 128) / 128;
        s += v * v;
      }
      return Math.sqrt(s / bytes.length);
    },
    _tick() {
      if (!this.active) return;
      // If mode/TTS conditions dropped, stop immediately (TTS ended but tts:end race etc.).
      if (!voice.bargeInEnabled || !voice.conversationMode || !ttsPlayer.playing) {
        this.stop();
        return;
      }
      // Muted (Aurora "Mute") → the user chose not to be heard; ambient noise must NOT barge
      // in and cut off Akana's reply. Mirror every other re-arm/capture path gated on micMuted.
      if (voice.micMuted) {
        this.stop();
        return;
      }
      // User is already speaking (new capture opened) → barge not needed.
      if (session.isCapturing()) {
        this.stop();
        return;
      }
      const now = (typeof performance !== "undefined" ? performance.now() : Date.now());
      // Startup grace: during AEC convergence + the TTS onset, do NOT let Akana's own
      // output accumulate toward a (self-)barge trigger.
      if (now - this.startTs < this._graceMs()) {
        this.voiceMs = 0;
        this._silenceMs = 0;
        this.lastTs = now;
        this.raf = requestAnimationFrame(() => this._tick());
        return;
      }
      const dt = Math.max(0, Math.min(100, now - this.lastTs));
      this.lastTs = now;
      const r = this._measureRms();
      const thr = this._threshold();
      try {
        if (localStorage.getItem("akana.bargeDebug") === "1" && now - (this._dbgTs || 0) > 400) {
          this._dbgTs = now;
          console.info(`[barge] rms=${r.toFixed(4)} threshold=${thr.toFixed(4)} voiceMs=${Math.round(this.voiceMs)}`);
        }
      } catch { /* ignore */ }
      if (r > thr) {
        this.voiceMs += dt;
        this._silenceMs = 0;
      } else {
        // Speech naturally dips below the threshold between syllables (0.0285 dip
        // observed in real data); resetting voiceMs on every dip prevented accumulation
        // and made barge never trigger. TOLERATE short dips — only CONTINUOUS ~120 ms
        // of silence resets the accumulator.
        this._silenceMs = (this._silenceMs || 0) + dt;
        if (this._silenceMs >= 120) this.voiceMs = 0;
      }
      if (this.voiceMs >= this._holdMs()) {
        this.voiceMs = 0;
        this.stop();
        try { onConversationBargeIn(); } catch { /* ignore */ }
        return;
      }
      this.raf = requestAnimationFrame(() => this._tick());
    },
    /** Tear the detector down. Returns a promise that resolves when the AudioContext close
     *  settles, so the barge re-arm can AWAIT the AEC mic release before starting SpeechRecognition
     *  (else SR races the still-releasing second mic → silent zombie / deaf, B1). A follow-up stop()
     *  after the audioCtx is already nulled (spoken-barge _tick stops first, then onConversationBargeIn
     *  stops again) returns the SAME pending close promise. */
    stop() {
      // Supersession token (V6): bump so any in-flight start() awaiting getUserMedia self-releases
      // its just-resolved stream instead of clobbering this.stream after an OFF/teardown.
      this._startToken = (this._startToken || 0) + 1;
      this.active = false;
      this.starting = false;
      this.voiceMs = 0;
      if (this.raf != null) {
        try { cancelAnimationFrame(this.raf); } catch { /* ignore */ }
        this.raf = null;
      }
      try { this.source?.disconnect(); } catch { /* ignore */ }
      this.source = null;
      try { this.analyser?.disconnect(); } catch { /* ignore */ }
      this.analyser = null;
      this.buf = null;
      if (this.stream) {
        try { this.stream.getTracks().forEach((t) => t.stop()); } catch { /* ignore */ }
        this.stream = null;
      }
      if (this.audioCtx) {
        try {
          this._closing = Promise.resolve(this.audioCtx.close()).catch(() => {});
        } catch {
          this._closing = Promise.resolve();
        }
        this.audioCtx = null;
      }
      return this._closing || Promise.resolve();
    },
  };

  /** Is barge-in enabled? Gated by localStorage akana.bargeIn — DEFAULT OFF
   *  (on only when "1" is written), consistent with the UI checkbox (akana-voice-settings.js
   *  default false). Rationale: with imperfect AEC on speakers, Akana's own TTS leaks into the
   *  barge mic and self-interrupts the answer mid-sentence (audio + text both cut). Barge is
   *  now opt-in: enable it in Settings → Voice to interrupt Akana by speaking. */
  function bargeInSettingEnabled() {
    try {
      return localStorage.getItem("akana.bargeIn") === "1";
    } catch {
      return false;
    }
  }

  /** Apply a barge-in enable/disable LIVE (Aurora scene toggle / settings): flip the runtime
   *  flag, persist it, open/close the AEC detector immediately (open only if Akana is speaking
   *  now), and echo voice:barge:state so any UI reflects it. Dynamic — no restart. */
  function applyBargeInEnabled(next) {
    voice.bargeInEnabled = !!next;
    try {
      localStorage.setItem("akana.bargeIn", voice.bargeInEnabled ? "1" : "0");
    } catch {
      /* ignore */
    }
    if (voice.bargeInEnabled) {
      // Turned ON mid-reply → open the detector for THIS in-flight reply, not just the exact
      // toggle instant. Gate on the authoritative in-flight latch (ttsStreamOpen), not the
      // instantaneous ttsPlayer.playing (which is briefly false during thinking / inter-sentence
      // gaps / the drained tail) — else enabling barge-in mid-reply is silently dropped (B2).
      // start() still hard-gates on ttsPlayer.playing, so priming during a gap is a safe no-op
      // that attaches when audio resumes; skip when muted (consistent with every re-arm path).
      if (voice.conversationMode && (ttsPlayer.playing || voice.ttsStreamOpen) && !voice.micMuted) {
        try { void bargeDetector.start(); } catch { /* ignore */ }
      }
    } else {
      // Turned OFF → close the detector immediately (drop any pending debounced teardown too).
      try { cancelPendingBargeStop(); } catch { /* ignore */ }
      try { bargeDetector.stop(); } catch { /* ignore */ }
    }
    emitBus("voice:barge:state", { enabled: voice.bargeInEnabled });
  }

  // Open the detector when TTS starts, close when TTS ends / queue drains. ttsPlayer
  // already emits these events (playNext start, drain/reset end) → we hook in via events
  // WITHOUT modifying ttsPlayer internals (isolation).
  //
  // DEBOUNCED TEARDOWN (#8): `voice:tts:end` is also emitted when ttsPlayer.playNext
  // instantly drains the queue (inter-sentence gap) — NOT "turn done". In multi-sentence
  // edge-TTS the queue drains between every sentence; if we stop() immediately
  // (mic stream + AudioContext torn down) the next `voice:tts:start` needs a new
  // getUserMedia → mic gain pumping, ~100–250 ms barge deafness, glitch.
  // Instead, delay stop(); if `voice:tts:start` arrives in this window (queue refilled),
  // CANCEL the pending stop → detector stays alive across inter-sentence gaps and is
  // torn down only at the TRUE end.
  let bargeStopTimer = null;
  const BARGE_STOP_DEBOUNCE_MS = 500;
  const cancelPendingBargeStop = () => {
    if (bargeStopTimer != null) {
      try { clearTimeout(bargeStopTimer); } catch { /* ignore */ }
      bargeStopTimer = null;
    }
  };
  const scheduleBargeStop = () => {
    cancelPendingBargeStop();
    bargeStopTimer = setTimeout(() => {
      bargeStopTimer = null;
      // If new audio started playing in this window (queue refilled), do not tear down —
      // start should have already cancelled the timer; this is a final safety check.
      if (ttsPlayer.playing) return;
      bargeDetector.stop();
    }, BARGE_STOP_DEBOUNCE_MS);
  };
  try {
    window.AkanaBus?.on?.("voice:tts:start", () => {
      // HALF-DUPLEX: browser SR MUST be stopped when TTS starts in conversation mode.
      // Otherwise the mic converts Akana's own speaker output to text and starts a new turn
      // (echo loop). Re-arm responsibility is in the finishConversationTurnIfTtsDone
      // → maybeReArmConversation → startConversationCapture chain.
      if (voice.conversationMode) {
        try { stopBrowserLiveTranscript(); } catch { /* ignore */ }
        // BUG E1 (deadlock): if capture was just entered (CAPTURE_WAKE) and TTS starts, we
        // stop the recognizer but MUST also leave CAPTURE_WAKE — otherwise isCapturing() stays
        // true and the TTS drain's maybeReArmConversation early-returns, so the mic never
        // re-opens (permanently deaf). Mirror the voice:mic:mute handler's FSM-exit.
        if (session.isCapturing()) {
          try {
            session.transition(
              session.isWakeArmed() ? VPhase.WAKE_ARMED : VPhase.IDLE,
              "tts:start",
              { force: true }
            );
          } catch { /* ignore */ }
        }
      }
      // Queue refilled (after inter-sentence gap) → cancel pending debounced teardown
      // so the detector is not torn down and rebuilt unnecessarily.
      cancelPendingBargeStop();
      try {
        if (localStorage.getItem("akana.bargeDebug") === "1") {
          console.info(
            `[barge] tts:start — enabled=${voice.bargeInEnabled} conv=${voice.conversationMode} ttsPlaying=${ttsPlayer.playing}`,
          );
        }
      } catch { /* ignore */ }
      if (!voice.bargeInEnabled || !voice.conversationMode) return;
      void bargeDetector.start();
    });
    window.AkanaBus?.on?.("voice:tts:end", () => {
      // No immediate stop(): queue may be draining between sentences → debounce;
      // cancelled if `voice:tts:start` arrives (see above).
      scheduleBargeStop();
    });
    // A voice turn's TTS stream is (re)opening. The turn-start ttsPlayer.reset() cleared the
    // "more audio coming" latch (ttsStreamOpen); restore it here — emitted by the transport
    // AFTER that reset — so the drain→re-arm chain waits for the backend `tts_end`
    // (voice:tts:streamEnd) instead of re-listening in the done→tts_end gap (which cut the
    // reply off mid-sentence and flipped the scene to "Listening"). Conversation mode only —
    // this latch drives the hands-free re-arm loop; typed/wake TTS does not use it.
    window.AkanaBus?.on?.("voice:tts:streamOpen", () => {
      if (voice.conversationMode) voice.ttsStreamOpen = true;
    });
    // Backend said "audio stream done": queue drain now means the turn is TRULY over.
    // Also triggers re-arm for replies that produced no audio (empty / tool-only).
    // NOTE: this latch is GLOBAL (no conversation id). It is safe because voice conversation mode
    // is SINGLE-conversation — the serial convAwaitingReply capture gate prevents two voiceTurn
    // streams coexisting, so the transport's voiceTurn-exempt streamEnd (fired on a voice turn's
    // OWN non-foreground end) can only ever clear its OWN latch. If voice is ever made multi-
    // conversation / parallel-voice, this handler + the transport emit must carry a conv id so a
    // background voice turn's end cannot clear a different foreground voice turn's latch.
    window.AkanaBus?.on?.("voice:tts:streamEnd", () => {
      voice.ttsStreamOpen = false;
      try { finishConversationTurnIfTtsDone("ttsStreamEnd"); } catch { /* ignore */ }
    });
  } catch {
    /* ignore */
  }

  function streamTtsParam() {
    // In conversation mode TTS is always on (hands-free voice response); otherwise
    // follows the user's stream-TTS preference.
    if (!ttsEnabled && !voice.conversationMode) return "";
    return `?tts=${encodeURIComponent(ttsLangFromSpeech())}`;
  }

  if (ttsToggle) {
    ttsToggle.checked = ttsEnabled;
    ttsToggle.addEventListener("change", () => {
      ttsEnabled = ttsToggle.checked;
      localStorage.setItem(LS_TTS, ttsEnabled ? "1" : "0");
      if (!ttsEnabled) ttsPlayer.reset();
      void saveVoicePreferences({ stream_tts: ttsEnabled }).catch(() => {});
    });
  }

  function voiceWakeActive() {
    return voice.wakeEnabled;
  }

  function voiceMicRecording() {
    return voice.micManual;
  }


  function hardStopVoiceActivity(opts = {}) {
    const skipChatAbort = !!opts.skipChatAbort;
    voice.cancelled = true;
    voice.utterFinishing = false;
    voice.hadSpeech = false;
    voice.silenceMs = 0;
    voice.ambientRms = 0;
    voice.ambientSamplesCollected = 0;
    voice.liveTranscriptUserEdit = false;
    session.cancelAll("hardStop");
    try {
      ttsPlayer.reset();
    } catch {
      /* ignore */
    }
    if (!skipChatAbort) {
      try {
        hooks.abortActiveChatStream?.();
      } catch {
        /* ignore */
      }
    }
    if (voice.voiceFetchAbort) {
      try {
        voice.voiceFetchAbort.abort();
      } catch {
        /* ignore */
      }
      voice.voiceFetchAbort = null;
    }
    stopBrowserLiveTranscript();
    voice.postInFlight = false;
    voice.wakeCooldownUntil = Date.now() + 1500;
    if (voice.wakeEnabled) {
      try {
        scheduleSpeechWakeRestart();
      } catch {
        /* ignore */
      }
    }
    updateVoiceStopButton();
    syncOrbWithVoice();
  }

  /** Single entry point for cancelling any active voice activity:
   *  wake-triggered utterance, manual mic, in-flight finalize,
   *  or active TTS playback. Drops captured audio, never POSTs. */
  function cancelVoiceActivity() {
    // If a Gemini Live session is open, Cancel/Esc closes it. Turn-based "active"
    // flags (utteranceActive/ttsPlayer/postInFlight) are NEVER set in Live mode →
    // wasActive below would stay false, leaving the session (WS+mic) hanging.
    if (voice.liveActive) {
      exitConversationMode("cancel");
      return true;
    }
    const wasCapturing = voice.utteranceActive || voice.micManual;
    // getChatInFlight() should count as "active" ONLY in the voice context (conversation
    // mode / awaiting reply) — the turn flowing there is a VOICE turn, and cancelling
    // it is correct (barge-in). OUTSIDE conversation mode getChatInFlight() = the user's
    // regular keyboard-typed turn; cutting it via the voice-cancel path (hardStop →
    // abortActiveChatStream + TTS reset + wake cooldown) was a bug. This function is also
    // called by the Esc handler (akana-shell.js) and the typed-turn start (akana-chat-
    // transport.js streamChat) → formerly pressing Esc or sending new text would silently
    // kill the pending typed reply (server turn remains → WS turn_completed → re-render).
    // The real "stop generating" path is STOP (with cancelActiveTurnOnServer).
    const voiceChatActive =
      (voice.conversationMode || voice.convAwaitingReply) && !!hooks.getChatInFlight?.();
    const wasActive =
      wasCapturing ||
      voice.utterFinishing ||
      voice.postInFlight ||
      ttsPlayer.playing ||
      voiceChatActive;
    if (!wasActive) return false;
    // Abort the active chat ONLY for a genuine VOICE turn (barge-in). When stream-TTS
    // is reading a TYPED reply aloud (voiceChatActive=false but ttsPlayer.playing=true),
    // Esc/"Hey Akana" should stop only the AUDIO; it must NOT cut the typed stream mid-
    // way via abortActiveChatStream (the reply breaks locally and later "jumps" in via
    // WS reconcile — the stream-TTS residue of the complaint R3 tried to close).
    hardStopVoiceActivity({ skipChatAbort: !voiceChatActive });
    if (wasCapturing) {
      hooks.appendRow(`<div class="meta">${_voiceT("voice.meta_voice")}</div><div class="bubble-bot">${_voiceT("voice.cancelled")}</div>`);
    }
    return true;
  }

  const btnVoiceStop = document.getElementById("btn-voice-stop");

  function voiceUiContext() {
    return {
      postInFlight: voice.postInFlight || voice.utterFinishing,
      ttsPlaying: ttsPlayer.playing,
      chatInFlight: !!hooks.getChatInFlight?.(),
    };
  }

  function syncVoiceUi() {
    const phase = session.getUiPhase(voiceUiContext());
    if (btnVoiceStop) {
      if (session.showCancelButton(voiceUiContext())) btnVoiceStop.removeAttribute("hidden");
      else btnVoiceStop.setAttribute("hidden", "");
    }
    if (voice.postInFlight || hooks.getChatInFlight()) hooks.setOrb("send");
    else if (phase === VPhase.CAPTURE_MIC || phase === VPhase.CAPTURE_WAKE) hooks.setOrb("listen");
    else if (phase === VPhase.SPEAKING) hooks.setOrb("send");
    else if (phase === VPhase.WAKE_ARMED) hooks.setOrb("listen");
    else hooks.setOrb(hooks.getWsReadyState() === 1 ? "ok" : "idle");
    if (phase === VPhase.CAPTURE_MIC || phase === VPhase.CAPTURE_WAKE) {
      hooks.setComposerHint("listening");
    }
    else if (phase === VPhase.PROCESSING || hooks.getChatInFlight()) {
      hooks.setComposerHint("thinking");
    } else if (phase === VPhase.SPEAKING) hooks.setComposerHint("speaking");
    else hooks.setComposerHint("idle");
  }

  function updateVoiceStopButton() {
    syncVoiceUi();
  }

  function syncOrbWithVoice() {
    syncVoiceUi();
    resumeWakeListeningIfIdle();
    // Clear the waiting flag here ONLY if the TTS stream is CLOSED: the `done` event
    // arrives BEFORE the TTS tail (chatInFlight becomes false while audio is still playing);
    // without this flag the turn would be considered done prematurely in inter-sentence gaps.
    if (
      voice.convAwaitingReply &&
      voice.conversationMode &&
      !voice.ttsStreamOpen &&
      !hooks.getChatInFlight?.() &&
      !ttsPlayer.playing &&
      !ttsPlayer.queue.length
    ) {
      voice.convAwaitingReply = false;
    }
    maybeReArmConversation("syncOrb");
  }

  async function ensureWakePipeline() {
    if (!session.isWakeArmed()) return;
    try {
      await ensureAudio();
      // Keep the two wake sources mutually exclusive on re-arm too (see setWakeListening):
      // run EITHER the server poll OR the browser SpeechRecognition fallback — never both.
      const wantModel = wakeSourcePref() === "model";
      const useModel = wantModel && voice.wakeServerEnabled;
      if (useModel) {
        stopSpeechWakeFallback();
        if (!voice.wakeInterval) {
          voice.wakeInterval = setInterval(() => void pollWakeOnce(), voice.wakePollMs);
        }
      } else {
        if (voice.wakeInterval) {
          clearInterval(voice.wakeInterval);
          voice.wakeInterval = null;
        }
        scheduleSpeechWakeRestart();
      }
    } catch (e) {
      console.warn("ensureWakePipeline:", e);
    }
  }

  function stopWakePrepareRepoll() {
    if (voice.wakePrepareRepoll) {
      clearInterval(voice.wakePrepareRepoll);
      voice.wakePrepareRepoll = null;
    }
  }

  /** Model wake was requested but the server reported status:"preparing" (downloading the
   *  shared feature models) — loadWakeConfig is called only from setWakeListening(true), so
   *  without this nothing ever re-checks the config and the browser-SR warm-up fallback is
   *  latched for the whole session. Re-poll the config on a bounded interval; when server
   *  scoring flips ready, stop the SR fallback and hand wake to the server poll (mirrors the
   *  useModel branch in setWakeListening/ensureWakePipeline). */
  function startWakePrepareRepoll() {
    stopWakePrepareRepoll();
    // Bound the repoll: the server reports status:"preparing" on EVERY poll while the wake/
    // feature-model download is parked on backoff (offline machine, repeated 503s), so without
    // a cap this fires a GET every 7 s for the whole session and the SR-fallback notice the
    // warm-up suppressed never surfaces. Give up after WAKE_PREPARE_MAX_ATTEMPTS (~10 min) and
    // fall through to the fallback notice, exactly like the "no longer preparing" branch below.
    const WAKE_PREPARE_MAX_ATTEMPTS = 85; // ~10 min at 7 s
    let _prepareAttempts = 0;
    voice.wakePrepareRepoll = setInterval(() => {
      // Wake turned off, mode changed, or the user no longer wants model wake → stop re-polling.
      if (!voice.wakeEnabled || wakeSourcePref() !== "model") {
        stopWakePrepareRepoll();
        return;
      }
      if (++_prepareAttempts > WAKE_PREPARE_MAX_ATTEMPTS) {
        // Download never landed (permanently offline / backoff-parked) → stop polling and
        // surface the fallback notice; the browser-SR warm-up remains the working wake path.
        stopWakePrepareRepoll();
        maybeShowWakeModelFallbackNotice();
        return;
      }
      void loadWakeConfig().then(() => {
        if (!voice.wakeEnabled || wakeSourcePref() !== "model") {
          stopWakePrepareRepoll();
          return;
        }
        if (voice.wakeServerEnabled) {
          // Warm-up finished → switch from the browser-SR fallback to the server poll.
          stopWakePrepareRepoll();
          stopSpeechWakeFallback();
          if (!voice.wakeInterval) {
            voice.wakeInterval = setInterval(() => void pollWakeOnce(), voice.wakePollMs);
          }
        } else if (!voice.wakeServerPreparing) {
          // No longer preparing and still not enabled → a real fallback; give up re-polling and
          // surface the notice the warm-up had suppressed.
          stopWakePrepareRepoll();
          maybeShowWakeModelFallbackNotice();
        }
      });
    }, 7000);
  }

  function resumeWakeListeningIfIdle() {
    if (!session.isWakeArmed()) return;
    if (
      voice.micManual ||
      voice.utteranceActive ||
      voice.postInFlight ||
      voice.utterFinishing ||
      hooks.getChatInFlight?.()
    ) {
      return;
    }
    if (ttsPlayer.playing) return;
    void ensureWakePipeline();
  }

  /* ---------- Hands-free conversation mode ----------
     Toggle with button/Esc. Loop: LISTEN (capture+VAD) → /voice/transcribe →
     AkanaChat.submitVoiceText (same conversation, TTS on) → stream+TTS → queue
     drains → LISTEN again. Aurora scene is driven by AkanaBus events.
     Reuses the CAPTURE_WAKE phase from the wake path (VAD already runs there);
     wake-specific side-effects (poll/meter/fallback) are off because voice.wakeEnabled=false. */

  function emitBus(event, payload) {
    try {
      window.AkanaBus?.emit?.(event, payload || {});
    } catch {
      /* ignore */
    }
  }

  /* ---------- Mobile robustness: audio unlock (user gesture) ----------
     On mobile (Android Chrome / installed PWA) AudioContext and <audio> elements
     can only be "unlocked" inside a user gesture chain (autoplay policy).
     The ttsPlayer energy ctx, earcon ctx, and first <audio> play are lazily
     initialised inside SSE callbacks (OUTSIDE the gesture stack) → first TTS may
     be silent. Therefore, call this SYNCHRONOUSLY on #btn-mic click and at the
     start of enterConversationMode (BEFORE any await): it resumes existing ctxs and
     plays ~1 frame of silent buffer to "warm up" the output graph. Never throws. */
  let _unlockCtx = null;
  function unlockAudioOnGesture() {
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      // Resume existing energy/earcon contexts inside the gesture (if lazily
      // initialised they may be suspended; on mobile resume only sticks within a gesture).
      try {
        const ectx = ttsPlayer?._energy?.ctx;
        if (ectx && ectx.state === "suspended") ectx.resume().catch(() => {});
      } catch {
        /* ignore */
      }
      try {
        if (_earconCtx && _earconCtx.state === "suspended") _earconCtx.resume().catch(() => {});
      } catch {
        /* ignore */
      }
      if (!AC) return;
      // Play ~1 frame of silent buffer through a shared tiny context —
      // this "unlocks" the output path inside the gesture chain.
      if (!_unlockCtx) {
        try {
          _unlockCtx = new AC();
        } catch {
          _unlockCtx = null;
        }
      }
      if (!_unlockCtx) return;
      if (_unlockCtx.state === "suspended") _unlockCtx.resume().catch(() => {});
      try {
        const buf = _unlockCtx.createBuffer(1, 1, _unlockCtx.sampleRate || 22050);
        const src = _unlockCtx.createBufferSource();
        src.buffer = buf;
        src.connect(_unlockCtx.destination);
        if (typeof src.start === "function") src.start(0);
        else if (typeof src.noteOn === "function") src.noteOn(0);
      } catch {
        /* ignore — unlock attempt must never block playback */
      }
    } catch {
      /* ignore */
    }
  }

  /* ---------- Mobile robustness: screen wake lock ----------
     If the screen turns off in conversation mode, SR is suspended and the loop stops.
     We hold a Screen Wake Lock API lock while active; silently no-op if unavailable.
     The system can release the lock (e.g. when the tab is hidden) → reacquired on
     visibilitychange (see init). */
  let _wakeLock = null;
  async function requestWakeLock() {
    try {
      if (!navigator.wakeLock || typeof navigator.wakeLock.request !== "function") return;
      if (_wakeLock) return; // already held
      _wakeLock = await navigator.wakeLock.request("screen");
      try {
        _wakeLock.addEventListener?.("release", () => {
          // System released the lock (e.g. tab hidden). Clear the sentinel so
          // it can be reacquired when the tab becomes visible again.
          _wakeLock = null;
        });
      } catch {
        /* ignore */
      }
    } catch {
      // Permission denied / not supported / hidden tab → silently ignore (conversation mode still works).
      _wakeLock = null;
    }
  }
  function releaseWakeLock() {
    const wl = _wakeLock;
    _wakeLock = null;
    if (!wl) return;
    try {
      wl.release?.();
    } catch {
      /* ignore */
    }
  }

  /* Auditory cues (earcon): "listen" = your turn (short rising blip),
     "done" = got it / thinking (short falling blip). DEFAULT OFF — only enabled when
     localStorage akana.voiceEarcons="1" (from settings). Separate small AudioContext
     (independent of mic, speaker output only). */
  let _earconCtx = null;
  function earconsEnabled() {
    try {
      return localStorage.getItem("akana.voiceEarcons") === "1";
    } catch {
      return false;
    }
  }
  // User-adjustable earcon volume (0..1). Maps to the oscillator peak gain up to
  // EARCON_PEAK_MAX — the old hard-coded peak was 0.05 (far too quiet, no control).
  // Default is intentionally LOUDER than the old constant; the settings slider
  // (akana-voice-settings.js #conv-earcon-vol) writes akana.voiceEarconVol.
  const LS_EARCON_VOL = "akana.voiceEarconVol";
  const EARCON_VOL_DEFAULT = 0.6; // → peak gain ~0.18 (vs. old fixed 0.05)
  const EARCON_PEAK_MAX = 0.3; // vol=1 → peak gain 0.30
  function earconVolume() {
    try {
      const v = Number(localStorage.getItem(LS_EARCON_VOL));
      if (Number.isFinite(v) && v >= 0 && v <= 1) return v;
    } catch {
      /* ignore */
    }
    return EARCON_VOL_DEFAULT;
  }
  function playEarcon(kind) {
    if (!earconsEnabled()) return;
    try {
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      if (!_earconCtx) _earconCtx = new AC();
      if (_earconCtx.state === "suspended") _earconCtx.resume().catch(() => {});
      const ctx = _earconCtx;
      const t0 = ctx.currentTime;
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      o.type = "sine";
      o.connect(g);
      g.connect(ctx.destination);
      if (kind === "done") {
        o.frequency.setValueAtTime(520, t0);
        o.frequency.exponentialRampToValueAtTime(390, t0 + 0.08);
      } else {
        o.frequency.setValueAtTime(660, t0);
        o.frequency.exponentialRampToValueAtTime(990, t0 + 0.08);
      }
      // Scale the peak gain by the user's earcon volume; keep the tiny 0.0001 ramp
      // floors (exponentialRamp cannot target 0). peak==0 → floor stays inaudible.
      const peak = Math.max(0.0001, earconVolume() * EARCON_PEAK_MAX);
      g.gain.setValueAtTime(0.0001, t0);
      g.gain.exponentialRampToValueAtTime(peak, t0 + 0.012);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.12);
      o.start(t0);
      o.stop(t0 + 0.15);
    } catch {
      /* ignore */
    }
  }

  function isConversationMode() {
    return voice.conversationMode;
  }

  /** Whisper-mode capture recovery: force the FSM out of the stuck CAPTURE_WAKE phase and
   *  re-enter listening. maybeReArmConversation early-returns while isCapturing() is true, so
   *  we must leave the capture phase first (mirrors the empty-utterance recovery in
   *  finalizeConversationFromSR). No submission happens — this only unsticks a deaf turn. */
  function recoverConvCaptureAndReArm(reason) {
    if (!voice.conversationMode) return;
    clearTimeout(voice.convCaptureWatchdog);
    voice.convCaptureWatchdog = null;
    clearTimeout(voice.convSilenceTimer);
    voice.convSilenceTimer = null;
    voice.convTranscript = "";
    try { stopBrowserLiveTranscript(); } catch { /* ignore */ }
    if (session.isCapturing()) {
      session.transition(VPhase.IDLE, `conv:${reason || "captureRecover"}`, { force: true });
    }
    try { maybeReArmConversation(reason || "captureRecover"); } catch { /* ignore */ }
    syncVoiceUi();
  }

  /** Start a new listening turn (capture). The audio graph must already be open. */
  function startConversationCapture(reason) {
    if (!voice.conversationMode) return;
    if (voice.liveActive) return; // Live (Gemini/OpenAI realtime) mode owns its own audio session
    if (voice.micMuted) return; // mic muted by the Aurora "Mute" button → do not open the recognizer
    // A whisper-STT turn's /voice/transcribe is in flight (utterFinishing/postInFlight) → the mic
    // must stay closed until finalizeUtterance's finally re-arms. The direct callers (visibility
    // return, "online", unmute) bypass maybeReArmConversation's guard, so mirror it here too;
    // without this a tab-return during the ~0.5–2 s transcription window opens the mic mid-finalize
    // and the finalize teardown immediately tears it back down.
    if (voice.utterFinishing || voice.postInFlight) return;
    if (session.isCapturing()) return;
    clearTimeout(voice.convWatchdog); // re-arm succeeded → turn watchdog no longer needed
    voice.convWatchdog = null;
    clearTimeout(voice._rearmRetry); // stop any pending re-arm retry
    voice._rearmRetry = null;
    clearTimeout(voice.convSilenceTimer);
    voice.convSilenceTimer = null;
    clearTimeout(voice.convCaptureWatchdog); // stale capture-phase timeout from a prior turn
    voice.convCaptureWatchdog = null;
    voice.convTranscript = "";
    voice.rawBuffer = new Float32Array(0);
    voice.cancelled = false;
    voice.liveTranscriptUserEdit = false;
    voice.utterChunks = [];
    voice.hadSpeech = false;
    voice.silenceMs = 0;
    voice.ambientRms = 0;
    voice.ambientSamplesCollected = 0;
    voice.utterStartTs = Date.now();
    voice.lastChunkTs = 0; // turn-local liveness stamp (see the capture-phase watchdog below)
    // Decide the STT source ONCE per listening turn so it can't flip mid-utterance
    // (which would risk a double- or zero-submit). Whisper → run the Worklet RMS-VAD;
    // browser → SR silence timer only (VAD stays off, historical default).
    const useWhisper = convUsesWhisperStt();
    voice.convVadEnabled = useWhisper;
    session.transition(VPhase.CAPTURE_WAKE, `conv:${reason || "capture"}`, { force: true });
    if (useWhisper) {
      // Whisper path needs the worklet mic graph feeding handleAudioChunk's RMS-VAD.
      // enterConversationMode released it (browser-SR default), so (re)open it here.
      // SR can still run concurrently for the live-transcript preview (proven by the
      // browser-wake path which runs SR + worklet together); its submit is suppressed.
      // If ensureAudio() REJECTS (AudioContext resume / worklet-module load / getUserMedia),
      // no chunks ever flow → the RMS-VAD auto-finalize never runs and (whisper mode)
      // finalizeConversationFromSR is suppressed, so the turn would hang forever on
      // "Listening". AWAIT the rejection and recover the FSM instead of hanging.
      const captureEpoch = session.getEpoch();
      ensureAudio().catch((e) => {
        try { console.warn("voice: ensureAudio failed for whisper STT, recovering:", e); } catch { /* ignore */ }
        if (!voice.conversationMode || !voiceEpochMatches(captureEpoch)) return; // moved on already
        clearTimeout(voice.convCaptureWatchdog);
        voice.convCaptureWatchdog = null;
        // LATCH THE FAILURE ONCE (mirror the browser-SR onerror path's convPermErrShown +
        // exitConversationMode). A denied / absent mic rejects getUserMedia PROMPTLY and on
        // microtasks only — recoverConvCaptureAndReArm → maybeReArm → startConversationCapture →
        // ensureAudio again with no macrotask in the cycle, so without a latch this spins the
        // microtask queue forever (tab freeze, thousands of mic-denied bubbles + earcons). The
        // first failure surfaces the bubble and exits conversation mode; a re-entry (fresh gesture)
        // resets convPermErrShown and lets the user try again once the mic is fixed.
        if (!voice.convPermErrShown) {
          voice.convPermErrShown = true;
          try {
            hooks.appendRow(
              `<div class="meta">${_voiceT("voice.meta_voice")}</div>` +
                `<div class="bubble-bot">${_voiceT("voice.err_mic_denied")}</div>`,
            );
          } catch { /* ignore */ }
          try { hooks.setOrb("err"); } catch { /* ignore */ }
          try { exitConversationMode("whisper-mic-perm"); } catch { /* ignore */ }
          return;
        }
        // Already latched (should not recur in the same session) — leave the capture phase so a
        // stale timer can't re-enter the loop, but do NOT re-arm (exit already fired above).
        recoverConvCaptureAndReArm("whisperMicFailed");
      });
      // Capture-phase safety timeout: force-recover so the FSM cannot stick on "Listening" when
      // the mic feed is dead. Two dead-feed shapes: (a) ZERO chunks ever (worklet never opened),
      // and (b) chunks flowed then STOPPED (mic revoked mid-turn without an `ended` event — the
      // worklet emits continuously even during silence, so a stale lastChunkTs means a dead feed,
      // not a quiet user). Only whisper mode arms this; the browser path (convVadEnabled=false)
      // and a live feed (recent lastChunkTs) are unaffected.
      const CONV_CAPTURE_TIMEOUT_MS = Math.max(
        3000,
        Math.min((voice.utterMaxSeconds || 120) * 1000, 20000),
      );
      // A live worklet delivers a chunk every ~130ms; anything older than this means the feed died.
      const CHUNK_STALE_MS = 2000;
      clearTimeout(voice.convCaptureWatchdog);
      voice.convCaptureWatchdog = setTimeout(() => {
        voice.convCaptureWatchdog = null;
        if (!voice.conversationMode || !voice.convVadEnabled) return; // exited / mode changed
        if (!voiceEpochMatches(captureEpoch)) return; // a newer turn owns capture now
        if (!session.isCapturing()) return; // already finalized / left the capture phase
        // Live feed (a chunk arrived recently) → VAD/finalize will handle it; do not disturb.
        if (voice.lastChunkTs && Date.now() - voice.lastChunkTs < CHUNK_STALE_MS) return;
        try { console.warn("voice: whisper capture feed is dead — recovering FSM"); } catch { /* ignore */ }
        recoverConvCaptureAndReArm("whisperCaptureTimeout");
      }, CONV_CAPTURE_TIMEOUT_MS);
    }
    // (i) ROOT FIX: DEFER the recognizer start off this callstack. Re-arm after a reply runs
    // inside the TTS <audio>.onended → playNext drain (playNext already cleared
    // ttsPlayer.playing before this, so a `playing` guard can't catch it); starting SR there
    // returns a silent zombie. The FSM/scene flip to "Listening" stays synchronous (below);
    // only browserRec.start() is pushed past the audio-teardown window via MIC_SETTLE_MS.
    // isCapturing() is already true now, so a racing re-arm is a no-op — no duplicate recognizer.
    scheduleMicSettleStart();
    emitBus("voice:utterance:start");
    playEarcon("listen"); // "your turn" cue
    syncVoiceUi();
  }

  /** If the TTS stream is CLOSED (backend `tts_end`) and all audio has played, end the
   *  turn and re-enter listening. Prevents PREMATURE re-listen while the stream is still
   *  open (temporary inter-sentence queue drain): that was the root of the "mic opens before
   *  reading finishes / Akana hears itself" bug. Called from `voice:tts:streamEnd` and
   *  from `playNext` drain; whichever completes last triggers the re-arm. */
  function finishConversationTurnIfTtsDone(reason) {
    if (voice.ttsStreamOpen) return; // more audio is coming → wait
    if (ttsPlayer.playing || ttsPlayer.queue.length) return; // still playing / queued
    voice.convAwaitingReply = false;
    try { resumeWakeListeningIfIdle(); } catch { /* ignore */ }
    try { maybeReArmConversation(reason); } catch { /* ignore */ }
    // If re-arm is blocked by a chatInFlight RACE (in a no-TTS turn, tts_end arrives
    // before the stream fully closes → maybeReArm gets stuck on chatInFlight): don't
    // leave it to the 15 s watchdog — retry at short intervals so listening resumes as
    // soon as the stream closes (~< 1 s). Turn watchdog (armed) stays as backstop;
    // successful re-arm clears it in startConversationCapture.
    scheduleRearmRetryIfBlocked(reason);
  }

  /** Retries re-arm at short intervals when it is blocked by a chatInFlight race in a
   *  no-TTS turn. Runs ONLY after TTS finishes + when there is a chatInFlight blockage
   *  → reply is already done (no interrupt risk). When the stream closes, maybeReArm
   *  succeeds → capture → the next call sees isCapturing and stops. If chatInFlight
   *  stays stuck permanently: harmless poll + 15 s watchdog backstop takes over. */
  function scheduleRearmRetryIfBlocked(reason) {
    clearTimeout(voice._rearmRetry);
    voice._rearmRetry = null;
    if (!voice.conversationMode || session.isCapturing()) return; // exited / re-arm done
    if (voice.convAwaitingReply || voice.utterFinishing || voice.postInFlight) return; // new turn / other state
    if (voice.ttsStreamOpen || ttsPlayer.playing || ttsPlayer.queue.length) return; // TTS active → ttsDrain will re-arm
    if (!hooks.getChatInFlight?.()) return; // no chatInFlight blockage
    voice._rearmRetry = setTimeout(() => {
      voice._rearmRetry = null;
      try { maybeReArmConversation(reason); } catch { /* ignore */ }
      scheduleRearmRetryIfBlocked(reason); // keep retrying until re-arm succeeds or stream closes
    }, 350);
  }

  /** Re-enter listening when turn + TTS are done (event-driven; safe to call from anywhere). */
  function maybeReArmConversation(reason) {
    if (!voice.conversationMode) return;
    if (voice.liveActive) return; // Live (Gemini/OpenAI realtime) mode owns its own audio session
    if (voice.micMuted) return; // muted → stay silent until the user un-mutes
    if (session.isCapturing()) return;
    if (voice.utterFinishing || voice.postInFlight) return;
    // If awaiting a reply (turn flowing / detached), do NOT re-enter listening —
    // otherwise the scene drops to "Listening" instead of "Thinking". Cleared when TTS finishes.
    if (voice.convAwaitingReply) return;
    if (hooks.getChatInFlight?.()) return;
    if (ttsPlayer.playing || ttsPlayer.queue.length) return;
    startConversationCapture(reason || "rearm");
  }

  // RECOVERY WATCHDOG (control P0): if after a turn is sent the backend `tts_end` SSE
  // NEVER arrives (network drop / server exception / tool-only turn tts_active=false),
  // ttsStreamOpen+convAwaitingReply stay stuck at true → mic never reopens, scene
  // permanently FREEZES on "Thinking".
  // INACTIVITY-based (not total turn time): a slow-but-progressing turn — model still
  // streaming text, or the first TTS chunk not produced yet — is NOT stuck. The rescue
  // only fires after CONV_WATCHDOG_MS of TRUE silence (no chat/tts activity, tracked in
  // lastConvActivityTs). Earlier this mis-fired in the dead window between
  // chat:stream:done and the first tts chunk on slow models, re-arming the mic mid-turn
  // (scene flipped to "Listening" ~0.5 s before TTS even started).
  const CONV_WATCHDOG_MS = 15000;
  function armConvWatchdog() {
    clearTimeout(voice.convWatchdog);
    voice.lastConvActivityTs = Date.now(); // arm = activity baseline (turn just submitted)
    voice.convWatchdog = setTimeout(convWatchdogTick, CONV_WATCHDOG_MS);
  }
  function convWatchdogTick() {
    if (!voice.conversationMode) return;
    if (!voice.convAwaitingReply && !voice.ttsStreamOpen) return; // temiz bitti
    // Recent turn progress (chat deltas/done, tts start/chunk-drain) counts as activity:
    // a quiet gap shorter than the window means the turn is still alive (slow model / TTS
    // not produced yet) → wait, do NOT rescue.
    const quietMs = Date.now() - (voice.lastConvActivityTs || 0);
    const busy =
      hooks.getChatInFlight?.() ||
      ttsPlayer.playing ||
      ttsPlayer.queue.length ||
      session.isCapturing() ||
      quietMs < CONV_WATCHDOG_MS;
    // Diagnostics (akana.voiceDebug="1"): when frozen on "responding", show which flag the
    // watchdog is treating as "legitimate activity" and delaying rescue → makes the root
    // cause (stuck chatInFlight? ttsStreamOpen? still-progressing?) clear.
    try {
      if (localStorage.getItem("akana.voiceDebug") === "1") {
        console.info(
          `[conv-wd] bekliyor=${!!busy} chatInFlight=${!!hooks.getChatInFlight?.()} ` +
            `tts=${ttsPlayer.playing} queue=${ttsPlayer.queue.length} ` +
            `capturing=${session.isCapturing()} awaitingReply=${voice.convAwaitingReply} ` +
            `ttsStreamOpen=${voice.ttsStreamOpen} quietMs=${quietMs}`,
        );
      }
    } catch {
      /* ignore */
    }
    if (busy) {
      // Re-check near the moment the quiet gap would actually reach the window (so a genuine
      // freeze is still rescued promptly), but never busy-loop faster than ~1 s.
      const next = Math.max(1000, CONV_WATCHDOG_MS - quietMs);
      voice.convWatchdog = setTimeout(convWatchdogTick, next); // still progressing → wait
      return;
    }
    try { console.warn("voice: conversation watchdog — clearing stuck flags + re-arm"); } catch { /* ignore */ }
    voice.ttsStreamOpen = false;
    voice.convAwaitingReply = false;
    try { maybeReArmConversation("watchdog"); } catch { /* ignore */ }
  }

  /** Mark turn progress so the recovery watchdog measures INACTIVITY, not total turn time.
   *  A slow-but-streaming reply — or a turn whose first TTS chunk hasn't arrived yet —
   *  keeps bumping this; the watchdog only rescues after a true CONV_WATCHDOG_MS quiet gap
   *  (e.g. tts_end never arrived). */
  function noteConvActivity() {
    if (!voice.conversationMode) return;
    voice.lastConvActivityTs = Date.now();
  }
  try {
    window.AkanaBus?.on?.("chat:stream:start", noteConvActivity);
    window.AkanaBus?.on?.("chat:stream:delta", noteConvActivity);
    window.AkanaBus?.on?.("chat:stream:done", noteConvActivity);
    window.AkanaBus?.on?.("voice:tool", noteConvActivity);
    window.AkanaBus?.on?.("voice:tts:start", noteConvActivity);
    window.AkanaBus?.on?.("voice:tts:end", noteConvActivity);
  } catch {
    /* ignore */
  }

  /** Conversation mode end-of-utterance: browser SR silence timer expired → deliver SR
   *  transcript to the chat pipeline (without going through Whisper; TTS on). Then when
   *  turn + TTS are done, maybeReArmConversation re-enters listening.
   *  STT is browser-SR-only: the SR transcript is sent directly to chat. */
  function finalizeConversationFromSR(transcript) {
    if (!voice.conversationMode) return;
    // Whisper STT mode: the server transcript is authoritative and the Worklet RMS-VAD
    // ends the turn (finalizeUtterance → postConversationBlob). SR here is a live-preview
    // signal ONLY — it MUST NOT submit, or the utterance would be sent twice (once by SR,
    // once by Whisper). The pending silence timer that called us is dropped; leave capture
    // open so the VAD path finalizes. This is the ONE-submission guard for whisper mode.
    if (voice.convVadEnabled) {
      clearTimeout(voice.convSilenceTimer);
      voice.convSilenceTimer = null;
      voice.convTranscript = "";
      return;
    }
    if (!session.isCapturing()) return;
    clearTimeout(voice.convSilenceTimer);
    voice.convSilenceTimer = null;
    voice.convTranscript = "";
    stopBrowserLiveTranscript();
    const t = (transcript || "").trim();
    session.transition(VPhase.PROCESSING, "convSR", { force: true });
    emitBus("voice:utterance:end");
    if (t.length < 2) {
      // Nothing meaningful → don't start a turn, re-enter listening.
      session.transition(VPhase.IDLE, "convSR:empty", { force: true });
      maybeReArmConversation("convEmpty");
      syncVoiceUi();
      return;
    }
    // Voice exit: only a standalone exit phrase closes conversation mode (scene closes too).
    // Shared with the Whisper submit path via isConversationExitPhrase (single source of truth).
    if (isConversationExitPhrase(t)) {
      exitConversationMode("voice-exit");
      return;
    }
    playEarcon("done"); // "got it, thinking" cue
    voice.convAwaitingReply = true; // do not re-enter listening until reply + TTS are done
    voice.ttsStreamOpen = true; // "more audio coming" until backend sends `tts_end`
    armConvWatchdog(); // prevent permanent freeze if tts_end never arrives (rescue + re-arm)
    void submitConversationTurn(t);
    session.transition(VPhase.IDLE, "convSR:done", { force: true });
    syncVoiceUi();
  }

  /** Submit the turn to chat: the browser-SR transcript goes directly to the chat pipeline. */
  async function submitConversationTurn(srText) {
    const text = srText;
    // If barge-in cancellation is in-flight, wait BEFORE POST (R4-B #2) → new turn must
    // not be sent before the old turn is cancelled on the server or it hits the busy-guard.
    // Mic capture cancellation did NOT wait (full barge audio); wait is only here, at the
    // submission boundary.
    if (voice._bargeCancelPromise) {
      const p = voice._bargeCancelPromise;
      voice._bargeCancelPromise = null;
      try { await p; } catch { /* cancel error must not block submission */ }
      if (!voice.conversationMode) return; // mode closed while waiting
    }
    try {
      window.AkanaChat?.submitVoiceText?.(text);
    } catch (e) {
      // Turn NEVER started (transport threw synchronously) → `tts_end` SSE will never
      // arrive → ttsStreamOpen/convAwaitingReply would stay stuck at true and mic
      // would never reopen. Reset flags and re-enter listening.
      try { console.warn("voice: submitVoiceText failed, re-arming:", e); } catch { /* ignore */ }
      voice.ttsStreamOpen = false;
      voice.convAwaitingReply = false;
      try { maybeReArmConversation("submitFailed"); } catch { /* ignore */ }
    }
  }

  /** User interrupted while Akana was speaking: cut TTS, cancel turn, open new capture. */
  function onConversationBargeIn() {
    if (!voice.conversationMode) return;
    if (session.isCapturing()) return;
    // WHISPER-TRANSCRIBE WINDOW: during the ~0.5–2 s /voice/transcribe fetch the FSM phase is
    // PROCESSING (isCapturing()=false, so we reach here), the turn has NOT been submitted yet, and
    // TTS/chat aren't running. Aurora Stop / a spoken barge must CANCEL that pending utterance:
    // abort the in-flight transcribe AND bump the epoch so postConversationBlob's voiceEpochMatches
    // check drops the submit — otherwise the stopped utterance still fires a full LLM+TTS turn.
    if (voice.utterFinishing || voice.voiceFetchAbort) {
      voice.cancelled = true;
      if (voice.voiceFetchAbort) {
        try { voice.voiceFetchAbort.abort(); } catch { /* ignore */ }
        voice.voiceFetchAbort = null;
      }
      try { session.bumpEpoch(); } catch { /* ignore */ }
      // finalizeUtterance's finally only clears utterFinishing when the epoch still matches (it no
      // longer does after the bump), so clear it here or the re-arm below (and every later re-arm)
      // stays blocked on utterFinishing and the scene goes deaf after a stopped transcribe.
      voice.utterFinishing = false;
    }
    try {
      ttsPlayer.reset();
    } catch {
      /* ignore */
    }
    // EXPLICITLY clear the "more audio coming" expectation (#13): reset() already
    // does this (stream gen + ttsStreamOpen=false) but if reset() throws mid-way
    // the flag could stay stuck and prevent mic from reopening.
    voice.ttsStreamOpen = false;
    try {
      hooks.abortActiveChatStream?.();
    } catch {
      /* ignore */
    }
    try {
      const convId = window.AkanaChat?.conversationIdForMemory?.();
      // START server cancellation but do NOT wait for mic capture (to avoid clipping
      // the start of the barge audio). Save the Promise → post-barge submission
      // (submitConversationTurn) waits for this BEFORE POST; otherwise the new turn
      // would be POST'd before the old turn is cancelled on the server, hitting the
      // busy-guard and delaying (R4-B #2).
      voice._bargeCancelPromise = convId
        ? (window.AkanaChat?.cancelActiveTurnOnServer?.(convId) || null)
        : null;
    } catch {
      voice._bargeCancelPromise = null;
    }
    try {
      hooks.setChatInFlight?.(false);
    } catch {
      /* ignore */
    }
    voice.convAwaitingReply = false;
    // B1 fix: do NOT start SpeechRecognition until the barge detector's AEC getUserMedia mic +
    // AudioContext have actually released — otherwise SR races the still-open second mic on the
    // same device and comes up a SILENT ZOMBIE (start() succeeds but audiostart never fires and
    // continuous=true means onend never fires → deaf until a tab-switch rebuilds it). Cancel the
    // self-inflicted debounced teardown (the ttsPlayer.reset() above emitted voice:tts:end, which
    // armed a ~500ms scheduleBargeStop), tear the detector down once (awaitable), then defer the
    // capture until its close settles. Bounded by a 500ms cap so a hung close() can never wedge the
    // re-arm. startConversationCapture's own MIC_SETTLE_MS defer still adds its margin on top. Covers
    // BOTH a spoken barge and the Aurora Stop button (both route through here); with barge-in OFF the
    // detector was never open so stop() resolves immediately (Stop is then no worse than a re-arm).
    let bargeTeardown;
    try {
      cancelPendingBargeStop();
      bargeTeardown = bargeDetector.stop() || Promise.resolve();
    } catch {
      bargeTeardown = Promise.resolve();
    }
    Promise.race([bargeTeardown, new Promise((r) => setTimeout(r, 500))]).finally(() => {
      if (!voice.conversationMode) return; // exited during the wait
      if (session.isCapturing()) return; // already re-armed elsewhere
      try {
        startConversationCapture("barge");
      } catch {
        /* ignore */
      }
    });
  }

  /* ── Gemini Live (full-duplex) path ────────────────────────────────────────
     When provider==gemini + Live flag + key + user toggle are all on, the voice
     button branches to AkanaVoiceLive instead of turn-based. Aurora orb/transcript
     are driven via the EXISTING AkanaBus contract (NO new overlay code): scene:open,
     voice:transcript (user), chat:stream:delta/done (assistant, cumulative FULL text),
     voice:tts:start/end (orb state). All other providers/conditions keep the turn-based
     path VERBATIM (this block is only active when shouldUseLiveMode returns true). */
  function shouldUseLiveMode() {
    const L = window.AkanaVoiceLive;
    if (!L || typeof L.shouldUseLive !== "function") return false;
    let cfg = null;
    try {
      cfg = window.AkanaVoiceLiveCfg || null;
    } catch {
      cfg = null;
    }
    let toggleOn = false;
    try {
      toggleOn = localStorage.getItem("akana.voice.liveMode") === "1";
    } catch {
      toggleOn = false;
    }
    return L.shouldUseLive(cfg, toggleOn);
  }

  async function enterLiveConversationMode(reason) {
    const L = window.AkanaVoiceLive;
    unlockAudioOnGesture();
    emitBus("voice:scene:open");
    if (!window.isSecureContext) {
      emitBus("voice:scene:close");
      hooks.appendRow(
        `<div class="meta">${_voiceT("voice.meta_voice")}</div><div class="bubble-bot">${_voiceT("voice.err_live_https")}</div>`,
      );
      hooks.setOrb("err");
      return false;
    }
    // If wake/PTT is active, close it — conversation mode (Live included) is the sole
    // owner. Remember whether wake was on so exitConversationMode can restore it, mirroring
    // the turn-based enterConversationMode below (otherwise the raw wake worklet keeps
    // polling during the Live session and can self-trigger on Akana's own live speech).
    voice.wakeBeforeConversation = voice.wakeEnabled;
    if (voice.wakeEnabled) {
      try {
        await setWakeListening(false, { silent: true });
      } catch {
        /* ignore */
      }
    }
    voice.conversationMode = true;
    voice.liveActive = true;
    if (btnMic) {
      btnMic.classList.add("active");
      btnMic.setAttribute("aria-pressed", "true");
    }
    void requestWakeLock();
    let token = "";
    try {
      token = (localStorage.getItem(window.AkanaCore?.LS_TOKEN || "akana.apiToken") || "").trim();
    } catch {
      token = "";
    }
    const convId = window.AkanaChat?.conversationIdForMemory?.() || null;
    let asstAcc = "";
    let userAcc = "";
    const ok = await L.start({
      conversationId: convId,
      token,
      // Pass the FULL /voice/config so pickVoiceMode can select the active voice mode
      // (Gemini Live / OpenAI Realtime) and determine the WS path + input rate.
      config: window.AkanaVoiceLiveCfg || null,
      onReady: () => {
        hooks.setOrb("listen");
        emitBus("voice:wake:trigger");
      },
      onState: (state) => {
        if (state === L.STATES.SPEAKING) {
          hooks.setOrb("send");
          emitBus("voice:tts:start");
        } else if (state === L.STATES.LISTENING) {
          // turn_complete / interrupt → end the turn, reset accumulators
          if (asstAcc) emitBus("chat:stream:done", { text: asstAcc });
          asstAcc = "";
          userAcc = "";
          hooks.setOrb("listen");
          emitBus("voice:tts:end");
        } else if (state === L.STATES.ERROR) {
          hooks.setOrb("err");
        }
      },
      onTranscript: (role, text) => {
        // Aurora chat:stream:delta REPLACES the full text (not appended) → accumulate.
        if (role === "user") {
          userAcc += text;
          emitBus("voice:transcript", { text: userAcc });
        } else {
          asstAcc += text;
          emitBus("chat:stream:delta", { text: asstAcc });
        }
      },
      onError: (kind, detail) => {
        let msg;
        if (kind === "unavailable") {
          msg = detail || _voiceT("voice.err_live_unavailable");
        } else if (kind === "mic-permission") {
          msg = _voiceT("voice.err_mic_denied");
        } else {
          msg = _voiceT("voice.err_live_start_failed");
        }
        hooks.appendRow(
          `<div class="meta">${_voiceT("voice.meta_voice")}</div><div class="bubble-bot">${escapeHtml(msg)}</div>`,
        );
        hooks.setOrb("err");
        exitConversationMode("live-error");
      },
    });
    if (!ok) {
      exitConversationMode("live-start-failed");
      return false;
    }
    return true;
  }

  async function enterConversationMode(reason) {
    if (voice.conversationMode) {
      // Already in mode but scene may be closed (e.g. Esc closed the scene and raced
      // with exitConversationMode, or mobile tab re-entered) → reopen the scene;
      // aurora open() is idempotent (no-op if already open).
      emitBus("voice:scene:open");
      return true;
    }
    // Gemini Live path (only when provider+flag+key+toggle are all on). In every other
    // case the turn-based path below is preserved VERBATIM.
    if (shouldUseLiveMode()) {
      return enterLiveConversationMode(reason);
    }
    // Mobile: first TTS audio is initialised inside an SSE callback (outside the gesture),
    // so open the audio output here — BEFORE any await — while still inside the gesture chain.
    unlockAudioOnGesture();
    // OPEN the Aurora fullscreen scene. On desktop the #btn-mic's own extra listener
    // (aurora-voice.js) already opens the scene; but on mobile the "Voice" tab calls
    // enterConversationMode DIRECTLY (does not click the button) → scene would otherwise
    // never open (Bug #5). The bus event works for both paths; open() is idempotent so
    // double-opening on desktop is harmless. On early-return errors below we close the
    // scene again so a failed entry doesn't leave an empty orb.
    emitBus("voice:scene:open");
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    // In WHISPER STT mode the turn loop is SR-free by design: the worklet RMS-VAD ends the
    // turn and /voice/transcribe produces the transcript; SR (if present) only powers the
    // OPTIONAL live-transcript preview and degrades gracefully when absent. So only block
    // entry on a missing SR for the browser-SR default — Whisper works on SR-free engines
    // (e.g. Firefox) once the pipeline is installed and selected.
    if (!SR && !convUsesWhisperStt()) {
      emitBus("voice:scene:close"); // failed entry → close the empty scene
      hooks.appendRow(
        `<div class="meta">${_voiceT("voice.meta_voice")}</div><div class="bubble-bot">${_voiceT("voice.err_no_sr")}</div>`,
      );
      hooks.setOrb("err");
      return false;
    }
    // Secure context required: mic (getUserMedia / SpeechRecognition) only works over
    // HTTPS or localhost. Otherwise the browser silently rejects and the only trace
    // would be in onerror → inform the user with a visible bubble and exit.
    if (!window.isSecureContext) {
      emitBus("voice:scene:close"); // failed entry → close the empty scene
      hooks.appendRow(
        `<div class="meta">${_voiceT("voice.meta_voice")}</div><div class="bubble-bot">${_voiceT("voice.err_conv_https")}</div>`,
      );
      hooks.setOrb("err");
      return false;
    }
    // If wake/PTT is active, close it — conversation mode is the sole owner.
    // Remember whether wake was on so we can restore it on exit (exitConversationMode reads this).
    voice.wakeBeforeConversation = voice.wakeEnabled;
    if (voice.wakeEnabled) {
      try {
        await setWakeListening(false, { silent: true });
      } catch {
        /* ignore */
      }
    }
    if (voice.micManual) {
      voice.cancelled = true;
      resetMicButtonUi();
      stopBrowserLiveTranscript();
    }
    // Conversation mode runs entirely through the browser SR (SR manages its own
    // microphone). ensureAudio (getUserMedia/worklet) is NOT called — Worklet/RMS-VAD are
    // not used, which eliminates the "stuck on Listening" problem (worklet not fed /
    // microphone contention) at the root.
    voice.conversationMode = true;
    voice.convPermErrShown = false; // new entry → allow mic-permission bubble to show again
    voice.micMuted = false; // new entry always starts un-muted (scene resets its Mute button too)
    // Silence threshold is configurable: localStorage akana.convSilenceMs (1000–4000 ms).
    // Raise for slow speakers, lower for faster turn-taking. Floor raised 600→1000: values
    // below ~1000 ms sit inside Chrome's post-final quiet window and reliably truncate speech
    // mid-utterance (premature end-of-utterance); 600 was an unsafe self-inflicted footgun.
    try {
      const ms = Number(localStorage.getItem("akana.convSilenceMs"));
      if (ms >= 1000 && ms <= 4000) voice.convSilenceMs = ms;
    } catch {
      /* ignore */
    }
    {
      // Prevent mic contention: release any existing worklet graph so the browser SR
      // can acquire the microphone alone (SR + parallel getUserMedia conflicts on some systems).
      try {
        stopAudioGraph();
      } catch {
        /* ignore */
      }
    }
    if (btnMic) {
      btnMic.classList.add("active");
      btnMic.setAttribute("aria-pressed", "true");
    }
    // Conversation mode started successfully → keep screen awake (on mobile SR is
    // suspended when the screen turns off). No-op if not supported.
    void requestWakeLock();
    startConversationCapture(reason || "enter");
    return true;
  }

  function exitConversationMode(reason) {
    if (!voice.conversationMode) return;
    // If a Gemini Live session is open, close it cleanly first (WS + mic + playback).
    // All exit paths (Esc/End → voice:scene:close → here) pass through this.
    if (voice.liveActive) {
      voice.liveActive = false;
      try {
        window.AkanaVoiceLive?.stop?.();
      } catch {
        /* ignore */
      }
    }
    voice.conversationMode = false;
    voice.cancelled = true;
    voice.utterFinishing = false;
    voice.convVadEnabled = false; // reset the whisper-STT VAD flag on exit (fresh entry re-decides)
    voice.convAwaitingReply = false;
    voice.micMuted = false; // fresh entry starts un-muted
    try { bargeDetector.stop(); } catch { /* ignore */ }
    releaseWakeLock(); // release screen wake lock (mobile)
    clearTimeout(voice.convSilenceTimer);
    voice.convSilenceTimer = null;
    clearTimeout(voice.convCaptureWatchdog);
    voice.convCaptureWatchdog = null;
    // Also clear the recovery watchdog + re-arm retry (startConversationCapture clears both on
    // entry; exit must too, or a self-rescheduling timer armed in the closing turn keeps a live
    // handle after the mode is gone — self-guarded, but a genuine cross-boundary timer leak).
    clearTimeout(voice.convWatchdog);
    voice.convWatchdog = null;
    clearTimeout(voice._rearmRetry);
    voice._rearmRetry = null;
    voice.convTranscript = "";
    if (voice.voiceFetchAbort) {
      try {
        voice.voiceFetchAbort.abort();
      } catch {
        /* ignore */
      }
      voice.voiceFetchAbort = null;
    }
    try {
      ttsPlayer.reset();
    } catch {
      /* ignore */
    }
    try {
      hooks.abortActiveChatStream?.();
    } catch {
      /* ignore */
    }
    try {
      const convId = window.AkanaChat?.conversationIdForMemory?.();
      if (convId) void window.AkanaChat?.cancelActiveTurnOnServer?.(convId);
    } catch {
      /* ignore */
    }
    try {
      hooks.setChatInFlight?.(false);
    } catch {
      /* ignore */
    }
    session.cancelAll(`conv:exit:${reason || ""}`);
    stopBrowserLiveTranscript();
    if (btnMic) {
      btnMic.classList.remove("active");
      btnMic.setAttribute("aria-pressed", "false");
    }
    stopAudioGraph();
    // Close Aurora scene (also close on voiced / programmatic exit — button/Esc
    // already closes it; close() is no-op if not open, no loop).
    emitBus("voice:mode:exit");
    // We suspended wake when entering conversation mode → restore the user's state.
    // Only conversation closed; «Hey Akana» listening must stay on unless the user
    // explicitly turned it off. (silent: silently bail on error / no permission.)
    const restoreWake = voice.wakeBeforeConversation;
    voice.wakeBeforeConversation = false;
    if (restoreWake && !voice.wakeEnabled) {
      void setWakeListening(true, { silent: true });
    }
    syncVoiceUi();
  }

  async function toggleConversationMode() {
    if (voice.conversationMode) {
      exitConversationMode("toggle");
      return;
    }
    await enterConversationMode("toggle");
  }

  let speechWakeRec = null;
  let speechWakeRestartTimer = null;
  // Restart back-off for the wake fallback recognizer. The old flat 300ms restart churned a new
  // SpeechRecognition ~3×/s forever on engines where SR always errors (service-not-allowed /
  // persistent network). Fast-fail loops double the delay (300ms → 4s); a session that stays up
  // long enough resets to base. Mirrors the browser-transcript recognizer's MIN/MAX pattern.
  const MIN_SPEECH_WAKE_RESTART_MS = 300;
  const MAX_SPEECH_WAKE_RESTART_MS = 4000;
  let _speechWakeBackoff = MIN_SPEECH_WAKE_RESTART_MS;
  let _lastSpeechWakeStartAt = 0;
  // Set true when SR reports a fatal, non-recoverable error (mic permission denied / no mic):
  // restarting would just spin a dead recognizer, so we stop and show a one-time notice.
  let _speechWakeFatal = false;
  let _speechWakeFatalNoticeShown = false;

  function updateWakeMeter({ rmsVal, score, threshold }) {
    if (!wakeMeter) return;
    if (!voice.wakeEnabled) {
      wakeMeter.hidden = true;
      wakeMeter.classList.remove("wake-meter-hot");
      return;
    }
    // Hide meter when ambient is silent — only show while there's actual signal.
    const HIDE_RMS = 0.005;
    const audible = rmsVal != null && rmsVal > HIDE_RMS;
    if (audible || typeof score === "number") {
      wakeMeter.hidden = false;
    }
    const thr = threshold ?? voice.wakeThreshold;
    const rmsPart = rmsVal != null ? _voiceT("voice.meter_rms", { rms: rmsVal.toFixed(4) }) : "";
    const sc =
      typeof score === "number"
        ? score.toFixed(2)
        : voice.lastWakeScore != null
          ? voice.lastWakeScore.toFixed(2)
          : "—";
    if (typeof score === "number") voice.lastWakeScore = score;
    wakeMeter.textContent = [rmsPart, _voiceT("voice.meter_score", { score: sc, threshold: thr })]
      .filter(Boolean)
      .join(" · ");
    wakeMeter.classList.toggle(
      "wake-meter-hot",
      typeof score === "number" && score >= thr,
    );
    // Inactivity hide: after 3s without a fresh score AND quiet audio, fade.
    if (voice.wakeMeterHideTimer) clearTimeout(voice.wakeMeterHideTimer);
    voice.wakeMeterHideTimer = setTimeout(() => {
      if (!voice.wakeEnabled) return;
      if (voice.utteranceActive || voice.micManual) return;
      wakeMeter.hidden = true;
      wakeMeter.classList.remove("wake-meter-hot");
    }, 3000);
  }

  // ── "Hey Akana" fuzzy matcher ───────────────────────────────────────────────
  // "Akana" is a rare proper noun → browser SpeechRecognition frequently
  // mistranscribes it: "a kana", "okana", "hakana", "akkana", "ekana", "a cana"…
  // The old literal /(hey )?akana/ match was therefore UNSTABLE (only fired when
  // ASR produced exact "akana" = "sometimes easy, sometimes hard"). Note: this
  // path does NOT use a threshold (WAKE_THRESHOLD only affects the server
  // openWakeWord path, which is OFF by default) — stability depends entirely on
  // this matcher. Prioritize recall.
  const _WAKE_TARGET = "akana";
  // Known phonetically-equivalent mistranscriptions ASR produces for "Hey Akana"
  // that pass NEITHER the exact "akana" substring NOR Lev<=1. Intentionally a
  // STRICT list (to avoid colliding with common TR/EN words) — safely extendable
  // from real-device [wake] debug logs.
  const _WAKE_ALIASES = ["arcana", "achana", "aghana", "akhana"];
  function _wakeLev(a, b) {
    const m = a.length;
    const n = b.length;
    if (Math.abs(m - n) > 2) return 3; // we only care about <=1 → early exit
    const dp = new Array(n + 1);
    for (let j = 0; j <= n; j += 1) dp[j] = j;
    for (let i = 1; i <= m; i += 1) {
      let prev = dp[0];
      dp[0] = i;
      for (let j = 1; j <= n; j += 1) {
        const tmp = dp[j];
        dp[j] = Math.min(
          dp[j] + 1,
          dp[j - 1] + 1,
          prev + (a[i - 1] === b[j - 1] ? 0 : 1),
        );
        prev = tmp;
      }
    }
    return dp[n];
  }
  function matchesWakePhrase(raw) {
    if (!raw) return false;
    let norm;
    try {
      norm = raw.toLowerCase().replace(/[^\p{L}\s]/gu, " ").replace(/\s+/g, " ").trim();
    } catch {
      // Fallback for very old engines without \p{L} support: simple ASCII
      norm = raw.toLowerCase().replace(/[^a-zçğıöşü\s]/g, " ").replace(/\s+/g, " ").trim();
    }
    if (!norm) return false;
    const joined = norm.replace(/\s+/g, "");
    // 1) direct substring match on the joined form ("a kana" → "akana", "hey akana" → …)
    if (joined.includes(_WAKE_TARGET)) return true;
    // 1b) known phonetic mistranscriptions (exact substring; strict list)
    for (let k = 0; k < _WAKE_ALIASES.length; k += 1) {
      if (joined.includes(_WAKE_ALIASES[k])) return true;
    }
    // 2) edit distance <= 1 over words and adjacent bigrams
    const words = norm.split(" ");
    for (let i = 0; i < words.length; i += 1) {
      const w = words[i];
      if (w.length >= 4 && _wakeLev(w, _WAKE_TARGET) <= 1) return true;
      if (i + 1 < words.length) {
        const bg = w + words[i + 1];
        if (bg.length >= 4 && bg.length <= 7 && _wakeLev(bg, _WAKE_TARGET) <= 1) return true;
      }
    }
    return false;
  }

  function stopSpeechWakeFallback() {
    clearTimeout(speechWakeRestartTimer);
    speechWakeRestartTimer = null;
    _speechWakeBackoff = MIN_SPEECH_WAKE_RESTART_MS; // intentional teardown → fresh budget on next arm
    if (!speechWakeRec) return;
    // DETACH the handlers BEFORE stop(): Chrome fires onend of a stopped session ASYNCHRONOUSLY.
    // In a stop-then-start cycle (wake-source change, device-loss re-arm) a NEW recognizer is built
    // before the old session's onend lands; that onend closes over the MODULE variable and runs
    // `speechWakeRec = null; scheduleSpeechWakeRestart()`, nulling the reference to the running NEW
    // recognizer (orphaning it — this fn early-returns on !speechWakeRec so it can never be stopped
    // again) and spawning a duplicate. Nulling onend/onerror/onresult on the captured instance first
    // makes the stale onend inert (mirrors stopBrowserLiveTranscript's handler-detach).
    const rec = speechWakeRec;
    speechWakeRec = null;
    try {
      rec.onend = null;
      rec.onerror = null;
      rec.onresult = null;
    } catch {
      /* ignore */
    }
    try {
      rec.stop();
    } catch {
      /* ignore */
    }
  }

  /** True when the browser SpeechRecognition phrase-match is the ACTIVE wake source:
   *  either the user chose "browser", or they chose "model" but server scoring is
   *  unavailable (auto-fallback). In pure "model" mode this is false → the browser SR
   *  fallback must never start (mutual exclusivity — only the server poll listens). */
  function browserWakeSourceActive() {
    return wakeSourcePref() === "browser" || !voice.wakeServerEnabled;
  }

  function scheduleSpeechWakeRestart() {
    clearTimeout(speechWakeRestartTimer);
    if (!browserWakeSourceActive()) return; // model mode → server poll owns wake
    if (_speechWakeFatal) return; // permanent mic error → do not spin a dead recognizer
    if (pageHidden()) return; // hidden tab → the browser withholds mic audio; the visibilitychange
    //                            handler rebuilds a fresh recognizer when the tab returns
    if (
      !voice.wakeEnabled ||
      voice.utteranceActive ||
      voice.micManual ||
      voice.postInFlight ||
      voice.utterFinishing ||
      ttsPlayer.playing ||
      ttsPlayer.queue.length > 0 ||
      hooks.getChatInFlight?.()
    ) {
      return;
    }
    // A recognizer that stayed up past the base window was healthy → reset the budget; only a
    // FAST-fail loop (start→error/end well under the base delay) grows the back-off, so a normal
    // quiet-room "no-speech" cycle keeps the snappy ~300ms restart while a broken engine that
    // errors instantly is damped to 4s instead of churning ~3 recognizers/second.
    if (_lastSpeechWakeStartAt && Date.now() - _lastSpeechWakeStartAt > 1500) {
      _speechWakeBackoff = MIN_SPEECH_WAKE_RESTART_MS;
    }
    const delay = _speechWakeBackoff;
    _speechWakeBackoff = Math.min(_speechWakeBackoff * 2, MAX_SPEECH_WAKE_RESTART_MS);
    speechWakeRestartTimer = setTimeout(() => startSpeechWakeFallback(), delay);
  }

  function startSpeechWakeFallback() {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!browserWakeSourceActive()) return; // model mode → server poll owns wake
    if (_speechWakeFatal) return; // permanent mic error → do not rebuild a dead recognizer
    // Hidden tab: the browser withholds mic audio from SpeechRecognition, so start() here just
    // spins a silent zombie that stays deaf even after the tab returns. Stay armed (wakeEnabled
    // true) — the visibilitychange handler rebuilds a fresh recognizer when the tab is foregrounded.
    if (pageHidden()) return;
    if (
      !SR ||
      speechWakeRec ||
      !voice.wakeEnabled ||
      voice.utteranceActive ||
      voice.micManual ||
      voice.postInFlight ||
      voice.utterFinishing ||
      ttsPlayer.playing ||
      ttsPlayer.queue.length > 0 ||
      hooks.getChatInFlight?.()
    ) {
      return;
    }
    speechWakeRec = new SR();
    speechWakeRec.continuous = true;
    speechWakeRec.interimResults = true;
    // ASR often ranks "akana" as the 2nd/3rd alternative because it's a rare
    // word; looking only at top-1 was the largest source of misses. Scanning a
    // few alternatives noticeably improves recall — no false-positive risk,
    // because each alternative still has to pass the same strict matchesWakePhrase.
    speechWakeRec.maxAlternatives = 6;
    // Set recognizer language to the user's SPEAKING language: hard-coded "en-US"
    // transcribed Turkish-accented "Hey Akana" badly; tr-TR produces "akana" much
    // more cleanly. "auto" is not a locale → resolve from the UI language.
    const _wkLang = speechLang();
    speechWakeRec.lang = _wkLang && _wkLang !== "auto" ? _wkLang : _langLocale();
    speechWakeRec.onresult = (ev) => {
      if (
        !voice.wakeEnabled ||
        voice.utteranceActive ||
        voice.micManual ||
        voice.postInFlight ||
        voice.utterFinishing ||
        ttsPlayer.playing ||
        hooks.getChatInFlight?.()
      ) {
        return;
      }
      if (Date.now() < voice.wakeCooldownUntil) return;
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const res = ev.results[i];
        const nAlt = Math.min(res.length || 1, 6);
        let hit = false;
        let hitText = "";
        for (let a = 0; a < nAlt; a += 1) {
          const t = ((res[a] && res[a].transcript) || "").toLowerCase();
          if (t && matchesWakePhrase(t)) {
            hit = true;
            hitText = t;
            break;
          }
        }
        if (wakeDebugEnabled()) {
          try {
            const top = ((res[0] && res[0].transcript) || "").toLowerCase();
            console.debug(
              "[wake] transcript:", JSON.stringify(top),
              nAlt > 1 ? `(+${nAlt - 1} alt)` : "",
              "→", hit ? `MATCH(${JSON.stringify(hitText)})` : "no",
            );
          } catch { /* ignore */ }
        }
        if (hit) {
          void onWakeTriggered(_voiceT("voice.wake_src_browser"));
          return;
        }
      }
    };
    speechWakeRec.onerror = (event) => {
      // Mic permission denied / no microphone is PERMANENT — restarting just spins a dead
      // recognizer forever. Latch it, show a one-time notice, and stop. network/no-speech/
      // aborted stay recoverable (the back-off scheduler handles the churn).
      const err = (event && event.error) || "";
      if (err === "not-allowed" || err === "service-not-allowed" || err === "audio-capture") {
        _speechWakeFatal = true;
        clearTimeout(speechWakeRestartTimer);
        speechWakeRestartTimer = null;
        if (!_speechWakeFatalNoticeShown) {
          _speechWakeFatalNoticeShown = true;
          try {
            hooks.appendRow(
              `<div class="meta">${_voiceT("voice.meta_voice")}</div><div class="bubble-bot">${
                err === "audio-capture"
                  ? _voiceT("voice.err_mic_not_found")
                  : _voiceT("voice.err_mic_denied")
              }</div>`,
            );
            hooks.setOrb("err");
          } catch { /* ignore */ }
        }
        return;
      }
      scheduleSpeechWakeRestart();
    };
    speechWakeRec.onend = () => {
      speechWakeRec = null;
      scheduleSpeechWakeRestart();
    };
    try {
      speechWakeRec.start();
      _lastSpeechWakeStartAt = Date.now();
    } catch {
      speechWakeRec = null;
      scheduleSpeechWakeRestart();
    }
  }



  // Whisper common hallucinations — when VAD cuts on empty audio, outputs like
  // "Subtitles by..." / "Thanks for watching" are produced. We drop these silently
  // without opening a chat bubble in the UI.

  function wakeDebugEnabled() {
    try {
      return localStorage.getItem("akana_wake_debug") === "1";
    } catch {
      return false;
    }
  }


  function syncWakeButtonUi(on) {
    if (!btnWake) return;
    btnWake.classList.toggle("active", on);
    btnWake.setAttribute("aria-pressed", on ? "true" : "false");
    btnWake.classList.toggle("wake-autostart-pending", !on && !!wakeAutostartPending);
  }

  /** Configured wake-detection source: "model" (DEFAULT) or "browser". */
  function wakeSourcePref() {
    try {
      return localStorage.getItem(LS_WAKE_SOURCE) === "browser" ? "browser" : "model";
    } catch {
      return "model";
    }
  }

  /** Configured conversation STT source: "browser" (DEFAULT) or "whisper".
   *  Mirrors wakeSourcePref: any value other than the explicit "whisper" opt-in
   *  (including a read error / unset key) resolves to the byte-for-byte-unchanged
   *  browser-SR default. */
  function sttSourcePref() {
    try {
      return localStorage.getItem(LS_STT_SOURCE) === "whisper" ? "whisper" : "browser";
    } catch {
      return "browser";
    }
  }

  /** True when conversation turns should be transcribed by the server (Whisper) instead
   *  of the browser SR — i.e. the Worklet RMS-VAD end-of-turn + /voice/transcribe path.
   *  Read at capture time (startConversationCapture) so the choice is stable for the
   *  duration of one listening turn. */
  function convUsesWhisperStt() {
    return sttSourcePref() === "whisper";
  }

  /** One-time notice (per page session) shown when the user asked for the server "model"
   *  wake source but server scoring is unavailable (no WAKE_MODEL / openwakeword) → we
   *  fall back to browser recognition. Reuses the same appendRow bubble pattern as the
   *  other voice notices; silent no-op off the chat page. */
  let _wakeModelFallbackNoticeShown = false;
  function maybeShowWakeModelFallbackNotice() {
    if (_wakeModelFallbackNoticeShown) return;
    _wakeModelFallbackNoticeShown = true;
    try {
      hooks.appendRow(
        `<div class="meta">${_voiceT("voice.meta_voice")}</div>` +
          `<div class="bubble-bot">${_voiceT("voice.wake_model_fallback")}</div>`,
      );
    } catch {
      /* ignore — notice is non-fatal */
    }
  }

  async function setWakeListening(on, opts = {}) {
    const silent = !!opts.silent;
    if (on) {
      if (voice.utteranceActive) discardWakeCaptureSilently();
      if (voice.micManual) {
        // Cancel an active manual-mic capture before arming wake. Transition the FSM out of
        // CAPTURE_MIC (→ IDLE; setWakeArmed below then moves IDLE→WAKE_ARMED) — NOT a direct
        // flag write: micManual is a derived getter now, and setWakeArmed does NOT move the phase
        // out of CAPTURE_MIC on its own, so a bare write would leave phase=CAPTURE_MIC (the old
        // latent desync). The transition also bumps the epoch (invalidates the cancelled capture's
        // in-flight finalize) and its onTransition clears buffers + mic UI.
        voice.cancelled = true;
        session.transition(VPhase.IDLE, "setWakeListening:cancelMic", { force: true });
        resetMicButtonUi();
        stopBrowserLiveTranscript();
      }
      voice.wakeErrShown = false;
      voice.lastWakeScore = null;
      _speechWakeFatal = false; // re-arming wake → give the fallback recognizer a fresh chance
      try {
        await loadWakeConfig();
        await ensureAudio();
        session.setWakeArmed(true, "setWakeListening:on");
        wakeAutostartPending = false;
        syncWakeButtonUi(true);
        // MUTUALLY EXCLUSIVE wake sources — running the server poll AND the browser
        // SpeechRecognition phrase-match together made the wake_threshold feel inert
        // (the browser SR fired regardless of the server score). Pick exactly one:
        //   • "model" + server scoring available → server poll ONLY.
        //   • "browser", OR "model" requested but server scoring unavailable → browser SR ONLY.
        clearInterval(voice.wakeInterval);
        voice.wakeInterval = null;
        stopSpeechWakeFallback();
        const wantModel = wakeSourcePref() === "model";
        const useModel = wantModel && voice.wakeServerEnabled;
        if (useModel) {
          voice.wakeInterval = setInterval(() => void pollWakeOnce(), voice.wakePollMs);
        } else {
          // Suppress the "using browser instead" notice while the server is still
          // preparing (downloading feature models) — it's a warm-up, not a real fallback,
          // and a later poll will enable server scoring automatically.
          if (wantModel && !voice.wakeServerEnabled && !voice.wakeServerPreparing)
            maybeShowWakeModelFallbackNotice();
          startSpeechWakeFallback();
          // Warm-up in progress: loadWakeConfig runs only from here, so nothing else would ever
          // re-check the config — poll it until server scoring is ready, then swap to the server
          // poll (otherwise the SR fallback stays latched for the whole session). Otherwise stop
          // any stale re-poll (source changed to browser, or server already unavailable).
          if (wantModel && voice.wakeServerPreparing) startWakePrepareRepoll();
          else stopWakePrepareRepoll();
        }
        updateWakeMeter({});
        return true;
      } catch (e) {
        session.setWakeArmed(false, "setWakeListening:fail");
        syncWakeButtonUi(false);
        if (!silent) {
          hooks.appendRow(`<div class="meta">${_voiceT("voice.meta_voice")}</div><div class="bubble-bot">${escapeHtml(String(e))}</div>`);
          hooks.setOrb("err");
        } else {
          syncVoiceUi();
        }
        return false;
      }
    }
    session.setWakeArmed(false, "setWakeListening:off");
    wakeAutostartPending = false;
    syncWakeButtonUi(false);
    clearInterval(voice.wakeInterval);
    voice.wakeInterval = null;
    stopWakePrepareRepoll();
    stopSpeechWakeFallback();
    updateWakeMeter({});
    if (!voice.micManual && !voice.utteranceActive && !voice.postInFlight) {
      if (!btnMic || !btnMic.classList.contains("active")) stopAudioGraph();
      syncOrbWithVoice();
    }
    return true;
  }

  async function runWakeTest() {
    // Only a diagnostic click opened this graph (wake listening off, not in conversation
    // mode / manual mic) → tear it back down when the test is done, otherwise the mic stays
    // live (OS indicator on, continuous capture) for the rest of the page session.
    const openedForTest =
      !voice.stream && !session.isWakeArmed() && !voice.conversationMode && !voice.micManual;
    try {
      await ensureAudio();
      const need = Math.floor(voice.inSampleRate * voice.wakeWindowSec);
      if (voice.rawBuffer.length < need) {
        hooks.appendRow(
          `<div class="meta">${_voiceT("voice.meta_wake_test")}</div><div class="bubble-bot">${_voiceT("voice.wake_test_no_audio")}</div>`,
        );
        return;
      }
      const slice = voice.rawBuffer.slice(-need);
      const at16 = AkanaVoiceCapture.downsampleFloat32(slice, voice.inSampleRate, 16000);
      const fd = new FormData();
      fd.append("audio", AkanaVoiceCapture.encodeWavPcm16Mono(at16), "wake-test.wav");
      const r = await fetch(`${baseUrl()}/api/v1/voice/wake`, {
        method: "POST",
        headers: authHeadersMultipart(),
        body: fd,
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        hooks.appendRow(
          `<div class="meta">Wake test</div><div class="bubble-bot">${escapeHtml(formatApiError(body, r.status))}</div>`,
        );
        return;
      }
      updateWakeMeter({
        rmsVal: AkanaVoiceCapture.rms(slice),
        score: body.max_score,
        threshold: body.threshold,
      });
      hooks.appendRow(
        `<div class="meta">${_voiceT("voice.meta_wake_test")}</div><div class="bubble-bot">${_voiceT("voice.wake_test_result", { score: body.max_score?.toFixed?.(3) ?? body.max_score, threshold: body.threshold, status: body.triggered ? _voiceT("voice.wake_test_triggered") : _voiceT("voice.wake_test_not_yet") })}</div>`,
      );
    } catch (e) {
      hooks.appendRow(
        `<div class="meta">Wake test</div><div class="bubble-bot">${escapeHtml(String(e))}</div>`,
      );
    } finally {
      if (
        openedForTest &&
        !session.isWakeArmed() &&
        !voice.conversationMode &&
        !voice.micManual &&
        !voice.postInFlight
      ) {
        try { stopAudioGraph(); } catch { /* ignore */ }
      }
    }
  }


  function wireComposerVoiceGuards() {
    if (!msg) return;
    msg.addEventListener("input", (e) => {
      if (!e.isTrusted) return;
      if (!(voice.utteranceActive || voice.micManual)) return;
      voice.liveTranscriptUserEdit = true;
      stopBrowserLiveTranscript();
    });
  }

  function wireVoiceControls() {
    wireComposerVoiceGuards();
    if (btnWake) {
      btnWake.addEventListener("click", () => {
        // "Hey Akana" button: CANCELS while voice activity is in progress, otherwise
        // TOGGLES wake listening. getChatInFlight() should count as "busy" ONLY in the
        // voice context (conversation mode / awaiting reply) — the turn flowing there is
        // a VOICE turn, pressing wake cancels it (barge-in). OUTSIDE conversation mode
        // getChatInFlight() = the user's regular keyboard-typed turn; previously this
        // also counted as "busy", and cancelVoiceActivity → hardStopVoiceActivity →
        // abortActiveChatStream() would CUT the typed reply MID-STREAM. (This was the
        // root cause of the "pressing Hey Akana while waiting for a reply" bug.)
        // Now typed turns are not interrupted; button only toggles wake listening.
        const voiceChatBusy =
          (voice.conversationMode || voice.convAwaitingReply) && !!hooks.getChatInFlight?.();
        const busy =
          voice.utteranceActive ||
          voice.micManual ||
          voice.postInFlight ||
          voice.utterFinishing ||
          ttsPlayer.playing ||
          voiceChatBusy;
        if (busy) {
          cancelVoiceActivity();
          return;
        }
        // Hey Akana button = ONLY toggles wake-word listening. Switching to conversation
        // mode is done automatically by onWakeTriggered (akana-voice-pipeline.js) when
        // "Hey Akana" is heard. Use the mic button (#btn-mic) to start hands-free
        // conversation mode directly.
        unlockAudioOnGesture();
        void setWakeListening(!voice.wakeEnabled);
      });
    }
    if (btnWakeTest) btnWakeTest.addEventListener("click", () => void runWakeTest());
    // Mic button is now a HANDS-FREE conversation mode toggle (not old PTT).
    // Mobile: open the audio lock SYNCHRONOUSLY at the START of the click gesture
    // (before await) — first TTS audio is initialised in an SSE callback, so the
    // gesture chain is needed here.
    if (btnMic) {
      btnMic.addEventListener("click", () => {
        unlockAudioOnGesture();
        void toggleConversationMode();
      });
    }
    if (btnVoiceStop) {
      btnVoiceStop.addEventListener("click", () => void cancelVoiceActivity());
    }
    // When the Aurora scene closes (Esc/End/background), exit conversation mode too.
    try {
      window.AkanaBus?.on?.("voice:scene:close", () => exitConversationMode("scene"));
    } catch {
      /* ignore */
    }
    // Aurora "Mute"/"Sustur" button (aurora-voice.js toggleMute) → the ONLY subscriber of
    // voice:mic:mute. Conversation mode is browser SpeechRecognition (SR OWNS the mic; the
    // voice.mute GainNode does NOT gate SR), so muting must STOP the recognizer and gate the
    // re-arm paths; un-muting reopens listening when the turn+TTS are idle.
    try {
      window.AkanaBus?.on?.("voice:mic:mute", (p) => {
        const muted = !!(p && p.muted);
        voice.micMuted = muted;
        // Live (Gemini/OpenAI realtime) mode streams PCM from its own mic stream — the turn-based
        // objects below (SpeechRecognition, convSilenceTimer, FSM capture) are all unused there, so
        // muting must gate the live stream directly, otherwise the UI says "Muted" while the mic
        // keeps streaming to the cloud provider (privacy bug VF-1).
        if (voice.liveActive) {
          try { window.AkanaVoiceLive?.setMuted?.(muted); } catch { /* ignore */ }
          return;
        }
        if (muted) {
          // Stop the recognizer, drop the pending end-of-turn timer, and let the gated
          // re-arm/restart paths keep it closed until the user un-mutes.
          try { stopBrowserLiveTranscript(); } catch { /* ignore */ }
          clearTimeout(voice.convSilenceTimer);
          voice.convSilenceTimer = null;
          voice.convTranscript = "";
          // Leave CAPTURE_WAKE/CAPTURE_MIC so isCapturing() is false while muted —
          // otherwise startConversationCapture()/maybeReArmConversation() early-return
          // on unmute and the recognizer never restarts (permanently deaf).
          if (session.isCapturing()) {
            try {
              session.transition(
                session.isWakeArmed() ? VPhase.WAKE_ARMED : VPhase.IDLE,
                "mic:mute",
                { force: true }
              );
            } catch { /* ignore */ }
          }
        } else if (
          voice.conversationMode &&
          !voice.convAwaitingReply &&
          !hooks.getChatInFlight?.() &&
          !ttsPlayer.playing &&
          !ttsPlayer.queue.length
        ) {
          // Un-muted while idle (not awaiting a reply / TTS not speaking) → resume listening.
          // If a reply/TTS is still active, the normal drain → re-arm chain reopens the mic.
          try { startConversationCapture("unmute"); } catch { /* ignore */ }
        }
      });
    } catch {
      /* ignore */
    }
    // Aurora "Barge-in" toggle (aurora-voice.js) → flip barge-in LIVE. Read the persisted value
    // fresh (immune to a stale flag if it was changed in Settings) and apply the inverse.
    try {
      window.AkanaBus?.on?.("voice:barge:toggle", () => {
        try { applyBargeInEnabled(!bargeInSettingEnabled()); } catch { /* ignore */ }
      });
    } catch {
      /* ignore */
    }
    // Aurora "Stop" button (aurora-voice.js) → cancel the in-flight turn (thinking/responding/
    // speaking) and return to listening WITHOUT exiting conversation mode. Same mechanism as a
    // spoken barge-in (cut TTS + abort chat + cancel the server turn + re-arm capture), just
    // user-initiated by tap. No-op if already listening (onConversationBargeIn guards isCapturing).
    try {
      window.AkanaBus?.on?.("voice:turn:stop", () => {
        if (!voice.conversationMode) return;
        try { onConversationBargeIn(); } catch { /* ignore */ }
      });
    } catch {
      /* ignore */
    }
  }

  function init(opts = {}) {
    hooks = { ...hooks, ...opts };
    voiceCapture = null;
    voicePipeline = null;
    voiceSettings = null;
    // Barge-in (interrupting Akana while it speaks): gated by localStorage akana.bargeIn
    // ("1" default). When off, conversation mode remains UNCHANGED half-duplex
    // (detector never opens). Separate AEC mic stream + AnalyserNode RMS path
    // replaces the dormant raw-mic path.
    voice.bargeInEnabled = bargeInSettingEnabled();
    if (speechLangSelect) {
      // STT follows the unified app language. If the app language changed since the
      // STT pick was made (basis mismatch), realign the locale — EXCEPT "auto"
      // (Whisper auto-detect is language-agnostic). A manual STT pick persists only
      // until the next app-language change.
      const appLang = (window.AkanaI18n && window.AkanaI18n.getLanguage && window.AkanaI18n.getLanguage()) || "en";
      let sttVal = localStorage.getItem(LS_SPEECH_LANG) || _langLocale();
      if (sttVal !== "auto" && localStorage.getItem(LS_SPEECH_LANG_BASIS) !== appLang) {
        sttVal = _langLocale();
      }
      localStorage.setItem(LS_SPEECH_LANG, sttVal);
      localStorage.setItem(LS_SPEECH_LANG_BASIS, appLang);
      speechLangSelect.value = sttVal;
      speechLangSelect.addEventListener("change", () => {
        localStorage.setItem(LS_SPEECH_LANG, speechLangSelect.value);
        localStorage.setItem(
          LS_SPEECH_LANG_BASIS,
          (window.AkanaI18n && window.AkanaI18n.getLanguage && window.AkanaI18n.getLanguage()) || "en",
        );
        if (browserRecShouldRun) {
          stopBrowserLiveTranscript();
          startBrowserLiveTranscript();
        }
      });
      // Live realign when the app language changes WITHOUT a reload (engine reconcile /
      // runtime-pane). Preserves "auto"; restarts the live recognizer with the new lang.
      window.addEventListener("akana:languagechange", () => {
        if (!speechLangSelect || speechLangSelect.value === "auto") return;
        const loc = _langLocale();
        if (speechLangSelect.value === loc) return;
        speechLangSelect.value = loc;
        localStorage.setItem(LS_SPEECH_LANG, loc);
        localStorage.setItem(
          LS_SPEECH_LANG_BASIS,
          (window.AkanaI18n && window.AkanaI18n.getLanguage && window.AkanaI18n.getLanguage()) || "en",
        );
        if (browserRecShouldRun) {
          stopBrowserLiveTranscript();
          startBrowserLiveTranscript();
        }
      });
    }
    wireVoiceControls();
    // Mobile screen-sleep / SR-suspend recovery: when the tab becomes visible again
    // (phone woke / returned to app) and we are still in conversation mode:
    // (a) reacquire the screen wake lock (system may have released it),
    // (b) if idle-listening is stuck (not awaiting reply + no capture), revive the
    //     loop by starting a new listening turn.
    try {
      document.addEventListener("visibilitychange", () => {
        const visible = document.visibilityState === "visible";
        // TTS survives tab switches independently of the conversation FSM: a backgrounded tab
        // suspends/throttles audio, so a reply that lands while away would be skipped unheard.
        // Pause the current chunk on hide; on return, resume it + kick any queue playNext held.
        // Runs BEFORE the FSM decision below (which may early-return on "none"/"reply-live").
        try {
          if (visible) ttsPlayer.resumeAfterVisible();
          else ttsPlayer.holdForHidden();
        } catch { /* ignore */ }
        // WAKE FALLBACK RECOGNIZER recovery (independent of conversation mode). Backgrounding the
        // tab while wake listening in browser-SR mode leaves the recognizer a silent zombie after
        // the tab returns (Chrome withholds mic audio when hidden, and continuous=true means onend
        // never fires to restart it) — "Hey Akana" is then deaf until the user toggles wake. On
        // return, rebuild a fresh recognizer if wake is armed, browser SR is the active source, and
        // nothing else owns the mic (a conversation/manual capture or TTS is not in progress).
        if (
          visible &&
          voice.wakeEnabled &&
          !_speechWakeFatal &&
          browserWakeSourceActive() &&
          !session.isCapturing() &&
          !voice.micManual &&
          !voice.utteranceActive &&
          !ttsPlayer.playing &&
          !ttsPlayer.queue.length &&
          !hooks.getChatInFlight?.()
        ) {
          try { stopSpeechWakeFallback(); } catch { /* ignore */ }
          try { startSpeechWakeFallback(); } catch { /* ignore */ }
        }
        const action = decideConvVisibilityAction({
          visible,
          conversationMode: voice.conversationMode,
          liveActive: voice.liveActive,
          capturing: session.isCapturing(),
          chatInFlight: !!hooks.getChatInFlight?.(),
          ttsPlaying: ttsPlayer.playing,
          ttsQueued: ttsPlayer.queue.length > 0,
          convAwaitingReply: voice.convAwaitingReply,
          ttsStreamOpen: voice.ttsStreamOpen,
          utterFinishing: voice.utterFinishing,
          postInFlight: voice.postInFlight,
        });
        if (action === "none") return;
        if (action === "stop-sr") {
          // Tab going to the background while listening. The browser silently ends
          // conversation-mode SpeechRecognition and every restart we attempt while hidden
          // spins into a dead recognizer (mic stays "armed" but deaf → user has to toggle
          // voice mode). Tear it down cleanly; the FSM stays in its capture phase so the
          // visible branch rebuilds a fresh recognizer on return.
          try { stopBrowserLiveTranscript(); } catch { /* ignore */ }
          return;
        }
        // --- tab returned to the foreground (all remaining actions are "visible") ---
        void requestWakeLock();
        // A reply is still streaming / TTS still playing → let the normal chat/TTS → drain →
        // re-arm chain finish; resumeAfterVisible() above already resumed the AudioContext and
        // restarted the held/paused speech, so it is audible. Do not touch the mic (half-duplex).
        if (action === "reply-live") return;
        // No reply in flight. If the turn flags are stuck at "awaiting reply / TTS open"
        // (the tts_end → drain → re-arm chain wedged while hidden — audio routed through a
        // suspended context never fired `ended`, and background timer throttling delayed the
        // stall watchdog), clear them now instead of waiting ~15 s for the recovery watchdog.
        if (voice.convAwaitingReply || voice.ttsStreamOpen) {
          voice.ttsStreamOpen = false;
          voice.convAwaitingReply = false;
        }
        // "rebuild-sr": FSM still thinks we are capturing but the recognizer was torn down
        // while hidden → rebuild it in place. "start-capture": idle → open a fresh turn.
        // Muted (Aurora "Mute" button) → do NOT reopen the recognizer on tab return.
        if (voice.micMuted) return;
        try {
          if (action === "rebuild-sr") startBrowserLiveTranscript();
          else startConversationCapture("viswake");
        } catch {
          /* ignore */
        }
      });
    } catch {
      /* ignore */
    }
    // Mobile network change (wifi↔cellular handoff, signal loss): active turn SSE
    // silently drops → UI gets stuck on "thinking" forever.
    // On "offline": abort the active chat stream and return to listening.
    // On "online": revive if capture is stuck.
    try {
      window.addEventListener("offline", () => {
        if (!voice.conversationMode) return;
        // Voice turn (from mic) or a keyboard-typed turn while conversation mode is open?
        // convAwaitingReply is true ONLY for voice turns. For typed turns it is false but
        // getChatInFlight() is true → the old single message ("I'll continue when you speak
        // again", "Voice" label) was misleading for typed turns (user typed, not spoke).
        // Abort is correct (SSE is already dead); separate the message by turn type.
        const wasVoiceTurn = voice.convAwaitingReply;
        const wasTypedTurn = !wasVoiceTurn && !!hooks.getChatInFlight?.();
        if (!wasVoiceTurn && !wasTypedTurn) return;
        try { hooks.abortActiveChatStream?.(); } catch { /* ignore */ }
        try { ttsPlayer.reset(); } catch { /* ignore */ }
        voice.convAwaitingReply = false;
        voice.ttsStreamOpen = false;
        try { hooks.setChatInFlight?.(false); } catch { /* ignore */ }
        try {
          const meta = wasVoiceTurn ? _voiceT("voice.meta_voice") : _voiceT("voice.meta_connection");
          const offlineMsg = wasVoiceTurn
            ? _voiceT("voice.offline_voice")
            : _voiceT("voice.offline_typed");
          hooks.appendRow(
            `<div class="meta">${meta}</div><div class="bubble-bot">${offlineMsg}</div>`,
          );
        } catch { /* ignore */ }
        try { maybeReArmConversation("offline"); } catch { /* ignore */ }
      });
      window.addEventListener("online", () => {
        if (!voice.conversationMode) return;
        if (!voice.convAwaitingReply && !session.isCapturing()) {
          try { startConversationCapture("online"); } catch { /* ignore */ }
        }
      });
    } catch {
      /* ignore */
    }
    syncVoiceUi();
    void ensureVoiceSettings().initVoiceUx();
  }

  function voicePostInFlight() {
    return voice.postInFlight;
  }

  window.AkanaVoice = {
    init,
    handoffToTextChat,
    cancelVoiceActivity,
    resumeWakeListeningIfIdle,
    syncOrbWithVoice,
    voiceWakeActive,
    voiceMicRecording,
    voicePostInFlight,
    streamTtsParam,
    enterConversationMode,
    exitConversationMode,
    isConversationMode,
    // Exposed for the visibility-recovery + recognizer-liveness contract tests
    // (pure decisions, no side effects).
    _decideConvVisibilityAction: decideConvVisibilityAction,
    _shouldRecreateRecognizer: shouldRecreateRecognizer,
    get ttsPlayer() {
      return ttsPlayer;
    },
    // Authoritative "backend still sending audio chunks" flag (true from turn submit
    // until the `tts_end` SSE). The scene reads this to avoid flipping to LISTENING
    // during an inter-sentence queue drain while the reply is still being spoken.
    get ttsStreamOpen() {
      return !!voice.ttsStreamOpen;
    },
    setChatInFlight(on) {
      hooks.setChatInFlight(!!on);
    },
    saveVoicePreferences,
    persistVoiceSettings: async () => {
      if (speechLangSelect) {
        try {
          localStorage.setItem(LS_SPEECH_LANG, speechLangSelect.value);
        } catch {
          /* ignore */
        }
      }
      try {
        await saveVoicePreferences({
          wake_autostart: ensureVoiceSettings().getWakeAutostartEnabled(),
          stream_tts: ttsEnabled,
        });
      } catch {
        /* local-only */
      }
    },
  };
})(); // akana-voice module
