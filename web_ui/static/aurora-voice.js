/**
 * Akana · Aurora — Wave 5 · Fullscreen voice scene (self-contained overlay).
 *
 * Injects an `aur-`-prefixed overlay into <body> and binds EXTRA (additive)
 * open-listeners to the existing #btn-wake / #btn-mic buttons. It never removes,
 * wraps, or replaces those buttons' own handlers — it only adds its own. Esc and
 * a backdrop click close the scene; when closed the overlay is fully hidden and
 * intercepts no pointer events.
 *
 * State feed is OPTIONAL and fully guarded: if window.AkanaBus?.on exists we
 * subscribe to the documented voice/chat events to drive the
 * listening / thinking / responding state + transcript. Nothing in this layer
 * throws if an API is missing — it degrades to a calm visual scene defaulting
 * to "listening". Owns ONLY this file + aurora-voice.css; edits no existing DOM.
 */
(() => {
  "use strict";

  // i18n helper — forward-declared so every function below can call it.
  const _auroraT = (k, vars) => {
    const base = (typeof window !== "undefined" && window.AkanaI18n?.t) ? window.AkanaI18n.t(k) : k;
    if (!vars) return base;
    return base.replace(/\{(\w+)\}/g, (_, v) => (vars[v] !== undefined ? vars[v] : `{${v}}`));
  };

  const OVERLAY_ID = "aur-voice-overlay";
  const STATES = { LISTENING: "listening", THINKING: "thinking", RESPONDING: "responding" };
  // Accept the reference's "speaking" vocabulary as an alias of "responding".
  const STATE_ALIAS = { speaking: STATES.RESPONDING, idle: STATES.LISTENING };
  const STATE_LABEL = {
    listening: "voice.state_listening",
    thinking: "voice.state_thinking",
    responding: "voice.state_responding",
  };
  const STATE_SUB = {
    listening: "voice.sub_listening",
    thinking: "voice.sub_thinking",
    responding: "voice.sub_responding",
  };
  // Linear equalizer: a row of thin bars BELOW the orb ("Direction A · Scene"
  // design decision — not the old radial ring). Base heights define the reference
  // silhouette; rAF scales each bar with --aur-amp (live audio amplitude).
  const BAR_COUNT = 13;
  const BAR_HEIGHTS = [14, 24, 36, 20, 44, 28, 16, 34, 46, 26, 38, 18, 30];
  // Transcript scrolls internally (overflow fix); trim very long bodies from the front.
  const TRANSCRIPT_MAX = 4000;

  /** Single reused instance — never recreated per open (no listener leaks). */
  let overlay = null;
  let els = null;
  let isOpen = false;
  // Transcript follows like a live subtitle: DEFAULT is pinned to the bottom
  // (tracks the latest line). Goes false when the user scrolls up; back to true at the bottom.
  let transcriptFollow = true;

  // Conversation duration timer (right-panel header) — starts in open(), stops in close().
  let timerId = 0;
  let timerStart = 0;
  // "Mute" visual state. Emits intent to the bus (voice:mic:mute); actual
  // mic pause is driven by akana-voice.js if it wires up the event.
  let muted = false;
  // "No response" notice (error/empty turn) → show briefly, then return to listening.
  let noAnswerTimer = 0;

  // akana-voice.js now drives the scene via the BUS (voice:scene:open/close)
  // on the same #btn-mic/#btn-wake click — it runs BEFORE this button listener
  // (script order: akana-voice → aurora-voice). The two triggers on the same
  // click would open then immediately close. Fix: IGNORE the button toggle
  // for a short window after a bus scene-event (the bus already set the right state).
  let lastBusSceneTs = 0;
  const BUS_TOGGLE_GUARD_MS = 500;

  // rAF waveform driver state.
  let rafId = 0;
  let rafPhase = 0;
  // Smoothed energy envelope (0‥1) — eased toward a per-state target, written
  // to the overlay's --aur-amp so the CSS glow/rings breathe with it and used
  // to scale the orb. Mirrors the reference's amp/target smoothing.
  let amp = 0.35;

  // Live audio energy (real RMS) — fed from the "voice:energy" event emitted
  // by akana-voice.js's ttsPlayer. When fresh (≤ENERGY_STALE_MS) it drives
  // waveform amplitude instead of the synthetic envelope; reverts silently when stale.
  let liveEnergy = 0;
  let liveEnergyTs = 0;
  const ENERGY_STALE_MS = 150;

  // The first real bus event flips `liveFeed` true; the scene then shows only
  // real AkanaBus data.
  let liveFeed = false;

  // Listeners that live only while the overlay is OPEN (added on open, removed
  // on close) so nothing leaks across open/close cycles.
  let onKeydown = null;
  let onBackdropClick = null;

  // AkanaBus unsubscribe thunks — wired once at init, kept for the lifetime of
  // the page (the bus feed drives state regardless of open/closed).
  const busUnsubs = [];

  const reducedMotion = () => {
    try {
      return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    } catch {
      return false;
    }
  };

  // The scene close-fade is driven by a CSS opacity transition (--j-dur-slow);
  // setting hidden=true (display:none) must WAIT for that duration — otherwise
  // the fade is cut short mid-way. The old 240ms constant clipped the 420ms
  // transition at ~57% (visible pop). Read the token from one source;
  // in reduced-motion the token is already 0ms → closes instantly.
  const sceneFadeMs = () => {
    try {
      const raw = getComputedStyle(overlay).getPropertyValue("--j-dur-slow").trim();
      const ms = raw.endsWith("ms")
        ? parseFloat(raw)
        : raw.endsWith("s")
          ? parseFloat(raw) * 1000
          : parseFloat(raw);
      if (Number.isFinite(ms) && ms >= 0) return ms;
    } catch {
      /* ignore — fall back below */
    }
    return 420;
  };

  /* ── markup ────────────────────────────────────────────────────────────── */

  function buildOverlay() {
    const root = document.createElement("div");
    root.id = OVERLAY_ID;
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-modal", "true");
    root.setAttribute("aria-label", _auroraT("voice.overlay_aria_label"));
    root.dataset.state = STATES.LISTENING;
    root.hidden = true;
    // Two regions: left "presence" (orb + linear equalizer + state + controls),
    // right "conversation" (header + current user utterance + reply + tools).
    // els.* selector NAMES are preserved (state-machine + AkanaBus wiring unchanged);
    // only those nodes are DISTRIBUTED across right/left panels.
    root.innerHTML = [
      '<div class="aur-voice-backdrop" aria-hidden="true"></div>',
      `<button class="aur-voice-close" type="button" data-aur-close aria-label="${_auroraT("voice.close_btn_label")}">`,
      '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M18 6L6 18M6 6l12 12"/></svg>',
      "</button>",
      '<div class="aur-voice-stage">',

      // ── left: presence ─────────────────────────────────────────────
      '<section class="aur-voice-presence">',
      '<div class="aur-voice-presence-main">',
      '<div class="aur-voice-viz">',
      '<span class="aur-ring aur-r1" aria-hidden="true"></span>',
      '<span class="aur-ring aur-r2" aria-hidden="true"></span>',
      '<span class="aur-orb" aria-hidden="true"></span>',
      "</div>",
      '<div class="aur-bars" aria-hidden="true"></div>',
      '<div class="aur-voice-status">',
      `<span class="aur-voice-state"><span class="aur-sdot" aria-hidden="true"></span><span class="aur-stext" role="status" aria-live="polite">${_auroraT("voice.state_listening")}</span></span>`,
      `<p class="aur-voice-sub">${_auroraT("voice.sub_listening")}</p>`,
      "</div>",
      "</div>",
      '<div class="aur-voice-controls">',
      // Barge-in toggle — interrupt Akana by speaking. Dynamic: flips voice.bargeInEnabled
      // + opens/closes the AEC detector live (voice.js voice:barge:toggle handler).
      '<button class="aur-vbtn aur-voice-barge" type="button" aria-pressed="false">',
      `<span class="aur-barge-dot" aria-hidden="true"></span><span class="aur-barge-label">${_auroraT("voice.barge_btn")}</span>`,
      "</button>",
      '<button class="aur-vbtn aur-voice-mute" type="button" aria-pressed="false">',
      `<span class="aur-mute-dot" aria-hidden="true"></span><span class="aur-mute-label">${_auroraT("voice.mute_btn")}</span>`,
      "</button>",
      // Stop — like the text-mode STOP: cancels the in-flight turn (thinking/responding/
      // speaking) and returns to listening WITHOUT closing the scene (exit is the top ✕).
      // Disabled while listening (nothing to stop). NO data-aur-close.
      `<button class="aur-vbtn aur-voice-stop" type="button" disabled title="${_auroraT("voice.stop_btn_title")}">`,
      `<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="7" y="7" width="10" height="10" rx="2" fill="currentColor"/></svg>${_auroraT("voice.stop_btn")}`,
      "</button>",
      "</div>",
      "</section>",

      // ── right: conversation ───────────────────────────────────────
      '<section class="aur-voice-convo">',
      '<div class="aur-voice-cohead">',
      `<span class="aur-voice-cotitle">${_auroraT("voice.convo_title")}</span>`,
      '<span class="aur-voice-status-cluster">',
      `<span class="aur-voice-model" title="${_auroraT("voice.model_chip_title")}"><span class="aur-voice-model-dot" aria-hidden="true"></span><span class="aur-voice-model-text">—</span></span>`,
      `<span class="aur-voice-ws" title="${_auroraT("voice.server_conn_title")}"><span class="aur-voice-ws-dot" aria-hidden="true"></span><span class="aur-voice-ws-text">—</span></span>`,
      '<span class="aur-voice-timer">00:00</span>',
      "</span>",
      "</div>",
      // Current user utterance (this turn only; no old "history"). Hidden when empty.
      '<div class="aur-voice-user" hidden>',
      `<span class="aur-voice-user-label">${_auroraT("voice.user_label")}</span>`,
      '<span class="aur-voice-user-dot" aria-hidden="true"></span>',
      '<span class="aur-voice-usertext"></span>',
      "</div>",
      // Reply area: one of three content variants shows based on data-state.
      '<div class="aur-voice-answer">',
      `<div class="aur-voice-hero">${_auroraT("voice.hero_listening")}</div>`,
      '<div class="aur-voice-shimmer" aria-hidden="true"><span></span><span></span><span></span></div>',
      '<div class="aur-voice-transcript"><span class="aur-tline"></span><span class="aur-caret" aria-hidden="true">▌</span></div>',
      // Notice shown when no response arrives (error / empty turn) — then returns to listening.
      `<div class="aur-voice-notice" role="status">${_auroraT("voice.no_answer_notice")}</div>`,
      "</div>",
      // Tool-card list (chat parity). Hidden when empty; populated by AkanaBus.
      '<div class="aur-voice-activity" hidden>',
      `<span class="aur-voice-tools-head">${_auroraT("voice.tools_head")}</span>`,
      `<ul class="aur-voice-tools" aria-label="${_auroraT("voice.tools_running_label")}" hidden></ul>`,
      "</div>",
      "</section>",

      "</div>",
    ].join("");
    return root;
  }

  function ensureOverlay() {
    if (overlay && overlay.isConnected) return overlay;
    overlay = buildOverlay();
    document.body.appendChild(overlay);
    els = {
      backdrop: overlay.querySelector(".aur-voice-backdrop"),
      bars: overlay.querySelector(".aur-bars"),
      orb: overlay.querySelector(".aur-orb"),
      stateText: overlay.querySelector(".aur-stext"),
      sub: overlay.querySelector(".aur-voice-sub"),
      transcript: overlay.querySelector(".aur-tline"),
      transcriptBox: overlay.querySelector(".aur-voice-transcript"),
      caret: overlay.querySelector(".aur-caret"),
      activity: overlay.querySelector(".aur-voice-activity"),
      tools: overlay.querySelector(".aur-voice-tools"),
      // New layout nodes (right panel) — selector names remain the same as above.
      userLine: overlay.querySelector(".aur-voice-user"),
      userText: overlay.querySelector(".aur-voice-usertext"),
      timer: overlay.querySelector(".aur-voice-timer"),
      mute: overlay.querySelector(".aur-voice-mute"),
      barge: overlay.querySelector(".aur-voice-barge"),
      stop: overlay.querySelector(".aur-voice-stop"),
      toolsHead: overlay.querySelector(".aur-voice-tools-head"),
      notice: overlay.querySelector(".aur-voice-notice"),
      modelText: overlay.querySelector(".aur-voice-model-text"),
      wsDot: overlay.querySelector(".aur-voice-ws-dot"),
      wsText: overlay.querySelector(".aur-voice-ws-text"),
    };
    // Transcript follow state: enable when the user is near the bottom, disable when scrolled up.
    if (els.transcriptBox) {
      els.transcriptBox.addEventListener("scroll", () => {
        const box = els.transcriptBox;
        transcriptFollow = box.scrollHeight - box.scrollTop - box.clientHeight < 48;
      });
    }
    // "Mute" — does NOT close the scene (no data-aur-close); only drives mute intent.
    if (els.mute) els.mute.addEventListener("click", toggleMute);
    // "Barge-in" toggle + "Stop" (cancel turn → listen). Neither closes the scene.
    if (els.barge) els.barge.addEventListener("click", toggleBarge);
    if (els.stop) els.stop.addEventListener("click", stopTurn);
    buildBars();
    return overlay;
  }

  /* ── linear equalizer bars ────────────────────────────────────────────────
     A horizontal bar array BELOW the orb (transform-origin:bottom). Bars are
     scaleY-animated via rAF; amplitude comes from live audio energy (--aur-amp).
     In reduced-motion mode: static (no rAF, orb not scaled). */
  function buildBars() {
    if (!els || !els.bars || els.bars.childElementCount) return;
    const frag = document.createDocumentFragment();
    for (let i = 0; i < BAR_COUNT; i++) {
      const bar = document.createElement("span");
      bar.className = "aur-bar";
      bar.style.height = (BAR_HEIGHTS[i] || 24) + "px";
      frag.appendChild(bar);
    }
    els.bars.appendChild(frag);
  }

  function setBarsStatic() {
    if (!els || !els.bars) return;
    const bars = els.bars.children;
    for (let i = 0; i < bars.length; i++) bars[i].style.transform = "scaleY(0.7)";
    if (overlay) overlay.style.setProperty("--aur-amp", "0.4");
    if (els.orb) els.orb.style.transform = "scale(1)";
  }

  // Per-state target energy (0‥1). Listening gently breathes, thinking is a
  // calm low ripple, responding swells. Eased toward via `amp`.
  function targetEnergy(state, phase) {
    if (state === STATES.THINKING) return 0.24;
    if (state === STATES.RESPONDING) return 0.72 + Math.sin(phase * 2.1) * 0.2;
    return 0.46 + Math.sin(phase * 1.3) * 0.16; // listening
  }

  function animateBars() {
    if (!els || !els.bars) return;
    const bars = els.bars.children;
    rafPhase += 0.06;
    const state = overlay ? overlay.dataset.state : STATES.LISTENING;

    // Amplitude target: if live audio energy is FRESH (TTS RMS while Akana speaks)
    // use it — the wave plays with the real audio; otherwise use the synthetic per-state envelope.
    // Stale energy (older than ENERGY_STALE_MS) silently falls back to synthetic.
    const now = (typeof performance !== "undefined" ? performance.now() : Date.now());
    const energyFresh = liveEnergyTs && now - liveEnergyTs <= ENERGY_STALE_MS;
    let target;
    let ease;
    if (energyFresh) {
      // Add a small floor to the silent base so the wave never goes completely flat;
      // snap to live energy faster than to synthetic (preserve the real dynamic).
      target = 0.18 + liveEnergy * 0.82;
      ease = 0.35;
    } else {
      target = targetEnergy(state, rafPhase);
      ease = 0.12;
    }
    amp += (target - amp) * ease;
    const ampClamped = amp < 0 ? 0 : amp > 1 ? 1 : amp;
    overlay.style.setProperty("--aur-amp", ampClamped.toFixed(3));

    const thinking = state === STATES.THINKING;
    for (let i = 0; i < bars.length; i++) {
      let k;
      if (thinking) {
        // calm, flowing low-amplitude wave
        k = 0.3 + (Math.sin(rafPhase * 1.6 + i * 0.5) * 0.5 + 0.5) * 0.5 * (0.45 + ampClamped);
      } else {
        const wob =
          Math.sin(rafPhase * 2.2 + i * 0.55) * 0.5 +
          Math.sin(rafPhase * 3.1 + i * 0.9) * 0.5;
        k = 0.26 + Math.abs(wob) * (0.5 + ampClamped * 0.95);
      }
      k = k < 0.12 ? 0.12 : k > 1.7 ? 1.7 : k;
      bars[i].style.transform = `scaleY(${k.toFixed(3)})`;
    }

    // Orb breathes with amplitude on top of its CSS breathing animation.
    if (els.orb) els.orb.style.transform = `scale(${(1 + ampClamped * 0.12).toFixed(3)})`;

    rafId = window.requestAnimationFrame(animateBars);
  }

  function startBars() {
    if (reducedMotion()) {
      setBarsStatic();
      return;
    }
    if (rafId) return;
    rafId = window.requestAnimationFrame(animateBars);
  }

  function stopBars() {
    if (rafId) {
      window.cancelAnimationFrame(rafId);
      rafId = 0;
    }
    if (els && els.orb) els.orb.style.transform = "";
  }

  /* ── state + transcript ──────────────────────────────────────────────────── */

  /** Resolve a raw state/alias string to one of the canonical STATES. */
  function resolveState(state) {
    const raw = String(state || "").toLowerCase();
    if (STATE_ALIAS[raw]) return STATE_ALIAS[raw];
    return STATES[raw.toUpperCase()] ? raw : STATES.LISTENING;
  }

  function setState(state) {
    if (!overlay) return;
    const next = resolveState(state);
    // Diagnostic: log state transitions (low-frequency). If we see
    // "thinking suddenly becomes listening" this is where to trace the cause.
    try {
      if (overlay.dataset.state !== next) console.debug("[aur] state:", overlay.dataset.state, "→", next, `(${state})`);
    } catch {
      /* ignore */
    }
    overlay.dataset.state = next;
    if (els && els.stateText) els.stateText.textContent = _auroraT(STATE_LABEL[next] || STATE_LABEL.listening);
    if (els && els.sub) els.sub.textContent = _auroraT(STATE_SUB[next] || STATE_SUB.listening);
    // "Stop" is meaningful only while a turn is in flight (thinking/responding/speaking);
    // disable it while listening (nothing to stop).
    if (els && els.stop) els.stop.disabled = next === STATES.LISTENING;
  }

  function setTranscript(text) {
    if (!els || !els.transcript) return;
    let t = typeof text === "string" ? text : "";
    if (t.length > TRANSCRIPT_MAX) t = "…" + t.slice(t.length - TRANSCRIPT_MAX + 1);
    els.transcript.textContent = t;
    if (t) clearNoAnswer(); // real response arrived → clear the "no response" notice
    const box = els.transcriptBox;
    if (box && transcriptFollow) box.scrollTop = box.scrollHeight;
  }

  /** Current user utterance (right panel, top). Row is hidden when empty. */
  function setUserText(text) {
    if (!els || !els.userText) return;
    const t = (typeof text === "string" ? text : "").trim();
    els.userText.textContent = t;
    if (els.userLine) els.userLine.hidden = !t;
  }

  /* ── duration timer (right-panel header) ────────────────────────────────── */
  function fmtTime(ms) {
    const s = ms > 0 ? Math.floor(ms / 1000) : 0;
    const m = Math.floor(s / 60);
    return String(m).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
  }

  function startTimer() {
    timerStart = Date.now();
    if (els && els.timer) els.timer.textContent = "00:00";
    window.clearInterval(timerId);
    timerId = window.setInterval(() => {
      if (els && els.timer) els.timer.textContent = fmtTime(Date.now() - timerStart);
      updateHeaderStatus(); // model + WS durumunu da tazele
    }, 1000);
  }

  function stopTimer() {
    window.clearInterval(timerId);
    timerId = 0;
  }

  /* ── header status cluster: active model + WS connection ────────────────────
     Data is READ from existing modules (no new source): model = #model-pill text
     (driven by akana-settings); WS = AkanaSettings.getWsReadyState(). Refreshed
     once per second (with timer) + on ws:* events while the scene is open. "—" when absent. */
  function updateHeaderStatus() {
    if (!els) return;
    if (els.modelText) {
      let m = "";
      try {
        const pill = document.getElementById("model-pill");
        m = (pill && (pill.querySelector(".status-text") || pill).textContent || "").trim();
      } catch {
        /* ignore */
      }
      els.modelText.textContent = m || "—";
    }
    if (els.wsText && els.wsDot) {
      let rs = null;
      try {
        rs = window.AkanaSettings?.getWsReadyState?.();
      } catch {
        /* ignore */
      }
      // WebSocket: 1=OPEN, 0=CONNECTING, 2/3=CLOSING/CLOSED.
      let label = "—";
      let st = "off";
      if (rs === 1) { label = _auroraT("voice.ws_connected"); st = "on"; }
      else if (rs === 0) { label = _auroraT("voice.ws_connecting"); st = "wait"; }
      else if (rs === 2 || rs === 3) { label = _auroraT("voice.ws_closed"); st = "off"; }
      els.wsText.textContent = label;
      els.wsDot.dataset.ws = st;
    }
  }

  /* ── "No response" notice ────────────────────────────────────────────────────
     If a turn ends with an error or empty reply (chat:stream:error or empty
     chat:stream:done) the scene must not stay stuck on "Thinking" forever:
     show a brief notice, then return to calm listening. Cleared on new content
     (open / new turn / delta). */
  function clearNoAnswer() {
    window.clearTimeout(noAnswerTimer);
    noAnswerTimer = 0;
    if (overlay) overlay.classList.remove("aur-noanswer");
  }

  function showNoAnswer(message) {
    if (!overlay) return;
    if (els && els.notice && typeof message === "string" && message) {
      els.notice.textContent = message;
    }
    overlay.classList.add("aur-noanswer");
    window.clearTimeout(noAnswerTimer);
    noAnswerTimer = window.setTimeout(() => {
      if (overlay) overlay.classList.remove("aur-noanswer");
      setState(STATES.LISTENING);
    }, 3500);
  }

  /* ── "Mute" — visual mute + bus intent (real mic pause wired externally) ── */
  function setMuted(next) {
    muted = !!next;
    if (overlay) overlay.classList.toggle("aur-muted", muted);
    if (els && els.mute) {
      els.mute.setAttribute("aria-pressed", muted ? "true" : "false");
      const lbl = els.mute.querySelector(".aur-mute-label");
      if (lbl) lbl.textContent = muted ? _auroraT("voice.muted_btn") : _auroraT("voice.mute_btn");
    }
  }

  function toggleMute() {
    setMuted(!muted);
    try {
      window.AkanaBus?.emit?.("voice:mic:mute", { muted });
    } catch {
      /* ignore — intent broadcast is best-effort */
    }
  }

  /* ── "Barge-in" toggle — display only; voice.js owns the flag + AEC detector ── */
  function bargeOn() {
    try {
      return localStorage.getItem("akana.bargeIn") === "1";
    } catch {
      return false;
    }
  }
  function setBargeVisual(on) {
    if (!els || !els.barge) return;
    els.barge.setAttribute("aria-pressed", on ? "true" : "false");
    els.barge.setAttribute(
      "aria-label",
      _auroraT(on ? "voice.barge_on" : "voice.barge_off"),
    );
  }
  function toggleBarge() {
    // Intent only. voice.js flips voice.bargeInEnabled, persists, opens/closes the detector,
    // and echoes voice:barge:state (the AkanaBus is synchronous, so the visual updates now).
    try {
      window.AkanaBus?.emit?.("voice:barge:toggle", {});
    } catch {
      /* ignore — intent broadcast is best-effort */
    }
  }

  /* ── "Stop" — cancel the in-flight turn and return to listening (scene stays open) ── */
  function stopTurn() {
    if (els && els.stop && els.stop.disabled) return; // nothing to stop while listening
    try {
      window.AkanaBus?.emit?.("voice:turn:stop", {});
    } catch {
      /* ignore — intent broadcast is best-effort */
    }
  }

  /* ── server-status + tool-call panel (chat parity) ──────────────────────────
     Because the chat log sits below the scene, we show server-status and
     tool cards HERE. Card content is derived via AkanaChatRender (global)
     helpers → the SAME action sentence/icon/status as in the chat (no invention). */

  // DOM nodes for tool cards in the active turn: tool-call id → <li>.
  const toolNodes = new Map();

  function activityVisible() {
    if (!els || !els.activity) return;
    // Activity panel is visible only when tool cards are present. Server state
    // (thinking/writing) is already shown in the .aur-voice-state strip above,
    // so no separate status strip here → eliminated the double indicator.
    const n = toolNodes.size;
    els.activity.hidden = n === 0;
    // Header counter: show "· N steps" when there are more than 3 tools (reference design).
    if (els.toolsHead) {
      els.toolsHead.textContent = n > 3
          ? _auroraT("voice.tools_head_n", { n })
          : _auroraT("voice.tools_head");
    }
  }

  function clearActivity() {
    toolNodes.clear();
    if (els && els.tools) {
      els.tools.textContent = "";
      els.tools.hidden = true;
    }
    if (els && els.activity) els.activity.hidden = true;
    // New turn → re-enable transcript follow (pin to latest line).
    transcriptFollow = true;
    clearNoAnswer(); // new turn starting → clear any stale "no response" notice
  }

  /** Build/update a compact card from a raw tool-call payload. If the same id
   *  arrives again (start→end) it updates the existing card (status + text). */
  function upsertVoiceTool(call) {
    if (!els || !els.tools || !call || typeof call !== "object") return;
    const render = window.AkanaChatRender;
    // Action sentence + status: use chat helpers when available, otherwise
    // derive a safe summary from the raw name/args (degrade — never throws).
    let icon = "🔧";
    let text = _auroraT("voice.tool_called");
    let status = "running";
    try {
      const action = render?.toolCallActionSentence?.(call);
      if (action) {
        if (action.icon) icon = action.icon;
        if (action.text) text = action.text;
      } else {
        const name = call.name || call.tool || call.toolName || "tool";
        text = String(name);
      }
      const st = render?.toolCallStatus?.(call);
      if (st) status = st;
    } catch {
      /* degrade to defaults */
    }

    const id =
      (call.id != null && String(call.id)) ||
      (call.call_id != null && String(call.call_id)) ||
      (call.tool_call_id != null && String(call.tool_call_id)) ||
      "";
    let li = id ? toolNodes.get(id) : null;
    if (!li) {
      li = document.createElement("li");
      li.className = "aur-voice-tool";
      const ic = document.createElement("span");
      ic.className = "aur-vt-icon";
      ic.setAttribute("aria-hidden", "true");
      const tx = document.createElement("span");
      tx.className = "aur-vt-text";
      const dot = document.createElement("span");
      dot.className = "aur-vt-state";
      dot.setAttribute("aria-hidden", "true");
      li.append(ic, tx, dot);
      els.tools.appendChild(li);
      if (id) toolNodes.set(id, li);
    }
    li.dataset.status = status;
    const ic = li.querySelector(".aur-vt-icon");
    const tx = li.querySelector(".aur-vt-text");
    if (ic) ic.textContent = icon;
    if (tx) {
      tx.textContent = text;
      tx.title = text;
    }
    els.tools.hidden = false;
    // New tool goes to the bottom → scroll list to end.
    els.tools.scrollTop = els.tools.scrollHeight;
    activityVisible();
  }

  /** First real bus event retires the visual-only baseline for the page's lifetime. */
  function goLive() {
    if (liveFeed) return;
    liveFeed = true;
  }

  /* ── open / close ────────────────────────────────────────────────────────── */

  function open() {
    ensureOverlay();
    if (isOpen) return;
    isOpen = true;
    overlay.hidden = false;
    // Begin each open from a calm listening baseline; fresh start on every open
    // (no mock — real events will fill the transcript).
    setState(STATES.LISTENING);
    setTranscript("");
    setUserText("");
    setMuted(false);
    setBargeVisual(bargeOn()); // reflect the persisted barge-in state on every open
    clearActivity();
    startTimer();
    updateHeaderStatus();
    // next frame so the un-hidden element transitions in
    window.requestAnimationFrame(() => {
      if (overlay) overlay.classList.add("aur-open");
    });

    onKeydown = (e) => {
      if (e.key === "Escape" || e.key === "Esc") {
        e.stopPropagation();
        close();
      }
    };
    onBackdropClick = (e) => {
      const t = e.target;
      if (t && typeof t.closest === "function" && t.closest("[data-aur-close]")) {
        close();
      }
    };
    document.addEventListener("keydown", onKeydown, true);
    overlay.addEventListener("click", onBackdropClick);

    startBars();
  }

  /** Trigger buttons (#btn-mic/#btn-wake) are the real mic toggle; the scene
   *  must MIRROR them. A second click while open should CLOSE the scene (old
   *  behaviour returned early in open() doing nothing → "won't close" bug).
   *  Stopping the real mic is the button's OWN handler's job; here only the visual scene. */
  function toggle() {
    // If the bus (akana-voice.js) already drove the scene on this same click,
    // skip the button toggle — otherwise we get open-immediately-close (or vice
    // versa). Without the bus (plain page) this window never fires → button acts alone.
    const now = (typeof performance !== "undefined" ? performance.now() : Date.now());
    if (now - lastBusSceneTs < BUS_TOGGLE_GUARD_MS) return;
    if (isOpen) close();
    else open();
  }

  function close() {
    if (!isOpen || !overlay) return;
    isOpen = false;
    overlay.classList.remove("aur-open");
    // Scene closed via Esc/End/backdrop: if conversation mode is listening, it
    // should also exit (stopping the real mic is the event listener's job, not the scene's).
    try {
      window.AkanaBus?.emit?.("voice:scene:close");
    } catch {
      /* ignore */
    }

    if (onKeydown) {
      document.removeEventListener("keydown", onKeydown, true);
      onKeydown = null;
    }
    if (onBackdropClick) {
      overlay.removeEventListener("click", onBackdropClick);
      onBackdropClick = null;
    }
    stopBars();
    stopTimer();

    // Fully hide after the fade so it can't intercept pointer events. Guard for
    // the (rare) re-open before the timeout fires. Delay = CSS fade duration + small
    // buffer → display:none only after the transition fully completes (no mid-fade cut).
    const delay = reducedMotion() ? 0 : sceneFadeMs() + 40;
    window.setTimeout(() => {
      if (!isOpen && overlay) overlay.hidden = true;
    }, delay);
  }

  /* ── optional AkanaBus feed (fully guarded; visual-only if absent) ────────
     The bus is infrastructure; voice modules may or may not emit on it. We
     subscribe to the documented event names and let them drive the scene when
     present. Missing bus or silent events → calm cinematic demo (which retires
     the instant any real event arrives, via goLive()). */
  function wireBus() {
    const bus = window.AkanaBus;
    if (!bus || typeof bus.on !== "function") return; // visual-only + demo

    const sub = (event, handler) => {
      try {
        // Any real bus event retires the demo before driving the real scene.
        const off = bus.on(event, (payload) => {
          goLive();
          handler(payload);
        });
        if (typeof off === "function") busUnsubs.push(off);
      } catch {
        /* ignore a single bad subscription */
      }
    };

    // Listening (capture) phases.
    sub("voice:wake:trigger", () => setState(STATES.LISTENING));
    sub("voice:utterance:start", () => {
      // BUG FIX ("sometimes clears while showing a response"): we do NOT call
      // setTranscript("") here. utterance:start fires on mic RE-ARM
      // (startConversationCapture) — i.e. the moment Akana finishes speaking.
      // The transcript carries both the user utterance and the assistant REPLY,
      // so clearing it here erased the displayed reply too early (happened more
      // often once mic-reopen became reliable). The transcript is shared: when
      // the user speaks again, the first voice:transcript already replaces it;
      // new-turn delta does the same. The reply stays visible until new content
      // arrives (open() clears on a fresh open).
      clearActivity();
      // New turn conversation starting → clear the old "You ·" line (reply stays;
      // the first new voice:transcript line will fill it).
      setUserText("");
      setState(STATES.LISTENING);
    });
    // voice:transcript = USER'S speech (STT). In the new layout this is written
    // to the "You ·" line at the top of the right panel (separate from the reply area).
    // Assistant reply goes to .aur-tline below via chat:stream:delta → no conflict.
    sub("voice:transcript", (p) => {
      if (p && typeof p.text === "string") setUserText(p.text);
    });
    sub("voice:utterance:end", () => {
      setState(STATES.THINKING);
    });

    // Turn started, no token yet → "Thinking" (scene stays here for long replies).
    sub("chat:stream:start", () => {
      // Text-triggered turn has no utterance:start → also reset the panel here.
      clearActivity();
      setState(STATES.THINKING);
    });

    // Thinking — model is producing a turn.
    sub("chat:stream:delta", (p) => {
      setState(STATES.RESPONDING);
      // Transport sends the FULL accumulated text → REPLACE, not append (tail-cap).
      if (p && typeof p.text === "string") setTranscript(p.text);
    });
    sub("chat:stream:done", (p) => {
      const txt = p && typeof p.text === "string" ? p.text : "";
      if (txt) {
        setTranscript(txt);
        setState(STATES.RESPONDING);
      } else if (!(els && els.transcript && (els.transcript.textContent || "").trim())) {
        // Turn ended with NO text (empty reply) → don't let the scene get stuck.
        showNoAnswer();
      } else {
        setState(STATES.RESPONDING);
      }
    });

    // Turn ended with error (transport serverError → throw; chat:stream:done never
    // fires). Scene must not stay stuck on "Thinking": brief notice + return to listening.
    sub("chat:stream:error", () => {
      showNoAnswer(_auroraT("voice.no_answer_notice"));
    });

    // Tool call (chat parity): transport emits on every tool_call event.
    // Card content derived via AkanaChatRender; same id updates existing card.
    // State text already visible in .aur-voice-state above (THINKING) → no separate strip.
    sub("voice:tool", (p) => {
      if (p && p.call) upsertVoiceTool(p.call);
    });

    // Responding — TTS playback.
    sub("voice:tts:start", () => setState(STATES.RESPONDING));
    sub("voice:tts:end", () => {
      // tts:end fires on every chunk drain (including inter-sentence gaps).
      // BUG FIX: transitioning to LISTENING here caused flickering in multi-sentence
      // replies — the scene dropped to "Listening" while the next sentence was still
      // playing (user saw "listening" while Akana was speaking). Real LISTENING arrives
      // via mic re-arm (voice:utterance:start) once the full turn+TTS completes — OR via
      // voice:tts:streamEnd below (authoritative "audio fully done" signal).
      // Anti-flicker is preserved: we only leave RESPONDING for a CALM idle state when
      // both the chat turn is finished AND no audio is queued/playing; if either is true
      // we keep showing the spoken/thinking state until streamEnd/re-arm lands.
      const chatBusy = !!window.AkanaChat?.getChatInFlight?.();
      // TTS chunk done → immediately stale the live energy (clean return to synthetic envelope).
      liveEnergy = 0;
      liveEnergyTs = 0;
      if (chatBusy) {
        // Model is still calling tools / producing more of the turn → THINKING.
        setState(STATES.THINKING);
        return;
      }
      // Chat turn is done. If no further audio is pending (ttsPlayer drained), this
      // chunk-drain is the genuine end → fall back to LISTENING instead of latching
      // on RESPONDING forever (typed/non-conversation TTS, or when no mic re-arm fires).
      let audioPending = false;
      let streamOpen = false;
      try {
        const v = window.AkanaVoice;
        const p = v?.ttsPlayer;
        if (p) audioPending = !!p.playing || (p.queue && p.queue.length > 0);
        streamOpen = !!v?.ttsStreamOpen;
      } catch {
        /* no player handle → treat as not pending */
      }
      // Genuine end ONLY when the backend stream is closed (`tts_end`) AND nothing is
      // left to play. While the stream is still open, an inter-sentence queue drain is
      // NOT the end — staying put avoids the "mic/Listening shows while Akana is still
      // reading" flicker; streamEnd / the final drain / mic re-arm will land LISTENING.
      if (!audioPending && !streamOpen) setState(STATES.LISTENING);
    });
    // Authoritative "audio stream fully done" signal from the backend (tts_end SSE →
    // emitted by akana-chat-transport.js / akana-chat.js). akana-voice.js uses this to
    // re-arm the mic; the scene must ALSO leave RESPONDING here, otherwise a turn that
    // ends without a mic re-arm (chat-in-flight cleared, typed/non-conversation TTS,
    // residual ttsPlayer.queue, awaiting-reply) latches "Responding"/"Speaking" forever.
    // Anti-flicker: only drop to LISTENING when the chat turn is truly finished; if a
    // chat turn is still in flight we wait (chat:stream:* will drive the next state).
    sub("voice:tts:streamEnd", () => {
      liveEnergy = 0;
      liveEnergyTs = 0;
      // Backend sends `tts_end` AFTER enqueuing the last chunk, so the final audio is
      // usually still playing here. Don't drop to LISTENING mid-playback — when audio
      // is still pending, defer: the final queue drain re-emits voice:tts:end
      // (audioPending=false, streamOpen=false) which lands LISTENING. Empty / tool-only
      // replies have no audio → transition immediately.
      let audioPending = false;
      try {
        const p = window.AkanaVoice?.ttsPlayer;
        if (p) audioPending = !!p.playing || (p.queue && p.queue.length > 0);
      } catch {
        /* no player handle → treat as not pending */
      }
      if (!window.AkanaChat?.getChatInFlight?.() && !audioPending) setState(STATES.LISTENING);
    });

    // Real audio energy (RMS 0‥1) — emitted every frame by akana-voice.js's ttsPlayer.
    // Stored with a timestamp to drive waveform amplitude from live audio;
    // the rAF loop checks freshness and decides when to fall back to synthetic.
    sub("voice:energy", (p) => {
      if (!p) return;
      const lvl = Number(p.level);
      if (!Number.isFinite(lvl)) return;
      liveEnergy = lvl < 0 ? 0 : lvl > 1 ? 1 : lvl;
      liveEnergyTs = (typeof performance !== "undefined" ? performance.now() : Date.now());
    });

    // Conversation mode programmatic/verbal exit ("stop/end/...") → close the scene.
    // (Button/Esc already close on their own; close() is a no-op when already closed.)
    sub("voice:mode:exit", () => close());

    // Barge-in state echo (voice.js is the authority; it flips the flag + AEC detector and
    // emits this). Keeps the scene toggle in sync with the settings checkbox / voice mode.
    sub("voice:barge:state", (p) => setBargeVisual(!!(p && p.enabled)));

    // Scene OPEN/CLOSE commands. enterConversationMode (akana-voice.js) emits
    // these: on desktop the #btn-mic extra listener already opens the scene, but
    // on MOBILE the "Voice" tab calls enterConversationMode DIRECTLY (doesn't click
    // the button) → without this event the scene would never open (Bug #5).
    // open()/close() are idempotent so the double-trigger from the button path is harmless.
    const stampBus = () => {
      lastBusSceneTs = (typeof performance !== "undefined" ? performance.now() : Date.now());
    };
    sub("voice:scene:open", () => {
      stampBus();
      open();
    });
    sub("voice:scene:close", () => {
      stampBus();
      close();
    });
  }

  /* ── boot ───────────────────────────────────────────────────────────────── */

  function init() {
    const btnWake = document.getElementById("btn-wake");
    const btnMic = document.getElementById("btn-mic");
    // Defensively no-op if neither trigger exists (e.g. non-chat pages).
    if (!btnWake && !btnMic) return;

    // Build the (hidden) overlay up front so the first open is instant.
    ensureOverlay();

    // ADDITIVE listener — for #btn-mic only. #btn-wake no longer opens the
    // scene directly; when "Hey Akana" is heard, the `voice:wake:trigger` BUS
    // event transitions the scene to LISTENING state (subscribed above).
    const toggleHandler = () => toggle();
    if (btnMic) btnMic.addEventListener("click", toggleHandler);

    wireBus();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
