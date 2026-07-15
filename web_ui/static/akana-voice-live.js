/**
 * Akana Live Voice — full-duplex real-time voice chat client (Phase 2).
 *
 * Covers TWO providers (browser↔server wire protocol is identical for both):
 *   • Gemini Live     → `/ws/voice/live`,     input PCM16@16k
 *   • OpenAI Realtime → `/ws/voice/realtime`, input PCM16@24k
 * The active mode is selected by `pickVoiceMode()` from `/voice/config` capabilities
 * (`{live, realtime}`); only one provider is active at a time. When `provider==gemini`
 * or `provider==openai` plus the relevant flag/key is set, the voice button routes to
 * this module instead of the turn-based `/voice` path: the microphone streams
 * continuously (`[0x01]+pcm` frame), the provider streams back PCM@24k for immediate
 * playback; barge-in is supported (model `interrupted` → playback queue flush).
 * No Whisper/TTS in the middle.
 *
 * Architecture: PURE helpers (`floatTo16BitPCM` / `encodeAudioFrame` / `nextState`)
 * do NOT call any browser API at load time → they are testable in a node-vm harness
 * (`tests/web/voice_live_contract.harness.mjs`). Browser APIs are used only inside
 * `start()`/`stop()`, at call time.
 *
 * Public API (window.AkanaVoiceLive):
 *   start({ conversationId, token, onState, onTranscript, onReady, onError }) -> Promise
 *   stop()
 *   isActive() -> bool
 */
(function () {
  "use strict";

  // ── Pure helpers (SDK/browser-independent — tested by the harness) ────────────

  const AUDIO_TAG = 0x01; // browser→server audio frame tag (matches gemini_live.py)
  const INPUT_RATE = 16000; // default/fallback input rate (Gemini Live); realtime=24k
  const OUTPUT_RATE = 24000; // output rate for BOTH providers (native-audio)
  const LIVE_PATH = "/ws/voice/live"; // Gemini Live WS path
  const REALTIME_PATH = "/ws/voice/realtime"; // OpenAI Realtime WS path
  const REALTIME_INPUT_RATE = 24000; // OpenAI Realtime input rate

  /** Float32 [-1,1] → Int16 PCM (clamp + scale; identical to capture WAV logic). */
  function floatTo16BitPCM(float32) {
    const out = new Int16Array(float32.length);
    for (let i = 0; i < float32.length; i++) {
      const x = Math.max(-1, Math.min(1, float32[i]));
      out[i] = x < 0 ? x * 0x8000 : x * 0x7fff;
    }
    return out;
  }

  /** Int16 PCM → `[0x01] + little-endian bytes` frame (Uint8Array). */
  function encodeAudioFrame(pcm16) {
    const bytes = new Uint8Array(pcm16.buffer, pcm16.byteOffset, pcm16.byteLength);
    const frame = new Uint8Array(bytes.length + 1);
    frame[0] = AUDIO_TAG;
    frame.set(bytes, 1);
    return frame;
  }

  /** Local resample fallback (when AkanaVoiceCapture is absent) — mirrors capture logic. */
  function downsample(buf, inRate, outRate) {
    const cap = (typeof window !== "undefined" && window.AkanaVoiceCapture) || null;
    if (cap && typeof cap.downsampleFloat32 === "function") {
      return cap.downsampleFloat32(buf, inRate, outRate);
    }
    if (inRate === outRate) return buf.slice();
    const ratio = inRate / outRate;
    const outLen = Math.max(1, Math.floor(buf.length / ratio));
    const out = new Float32Array(outLen);
    for (let i = 0; i < outLen; i++) {
      const p = i * ratio;
      const i0 = Math.floor(p);
      const f = p - i0;
      const a = buf[i0] ?? 0;
      const b = buf[i0 + 1] ?? a;
      out[i] = a + (b - a) * f;
    }
    return out;
  }

  // Orb/state machine (pure): IDLE→CONNECTING→LISTENING↔SPEAKING, ERROR.
  const STATES = {
    IDLE: "idle",
    CONNECTING: "connecting",
    LISTENING: "listening",
    SPEAKING: "speaking",
    ERROR: "error",
  };

  /** Returns true if a single capability block (live/realtime) is reachable: available+enabled+match. */
  function _blockOk(block, matchKey) {
    return !!(block && block.available && block.enabled && block[matchKey]);
  }

  /**
   * PURE: select the active voice mode from the full `/voice/config` (`{live, realtime}`)
   * or a backwards-compatible single Gemini-style block (carries `provider_is_gemini`).
   * Both provider blocks are checked; only one can be active at a time.
   *   Gemini → { kind:"live",     path:"/ws/voice/live",     inputRate:16000 }
   *   OpenAI → { kind:"realtime", path:"/ws/voice/realtime", inputRate:24000 }
   * If neither is reachable → { active:false }.
   */
  function pickVoiceMode(cfg) {
    if (!cfg || typeof cfg !== "object") return { active: false };
    // Backwards compat: if a bare Gemini block is passed directly, treat it as `live`.
    const live = cfg.live || (("provider_is_gemini" in cfg) ? cfg : null);
    const realtime = cfg.realtime || (("provider_is_openai" in cfg) ? cfg : null);
    if (_blockOk(live, "provider_is_gemini")) {
      return { active: true, kind: "live", path: LIVE_PATH, inputRate: INPUT_RATE };
    }
    if (_blockOk(realtime, "provider_is_openai")) {
      return {
        active: true,
        kind: "realtime",
        path: REALTIME_PATH,
        inputRate: REALTIME_INPUT_RATE,
      };
    }
    return { active: false };
  }

  /**
   * PURE: should live mode be used — full `/voice/config` (or single block) + toggle.
   * Returns true when a provider block is reachable (available+enabled+match) AND the
   * user toggle is on. Gemini or OpenAI — does not matter (only one is active).
   */
  function shouldUseLive(cfg, toggleOn) {
    return pickVoiceMode(cfg).active && !!toggleOn;
  }

  /**
   * Should the toggle UI be visible (reachability, INDEPENDENT of user choice) —
   * true when any provider (Gemini Live or OpenAI Realtime) is reachable.
   */
  function liveToggleVisible(cfg) {
    return pickVoiceMode(cfg).active;
  }

  /** (state, event) → next state. Barge-in: interrupt/turn_complete → LISTENING. */
  function nextState(state, event) {
    switch (event) {
      case "connecting":
      case "reconnecting":
        return STATES.CONNECTING;
      case "ready":
        return STATES.LISTENING;
      case "assistant_audio":
        return STATES.SPEAKING;
      case "interrupt":
      case "turn_complete":
        return STATES.LISTENING;
      case "error":
        return STATES.ERROR;
      case "stop":
        return STATES.IDLE;
      default:
        return state;
    }
  }

  // ── Reconnect (pure decisions — client-side 3-attempt pattern) ────────────────

  const RECONNECT_MAX = 3; // total attempt ceiling
  const RECONNECT_BASE_MS = 500; // initial back-off base
  const RECONNECT_CAP_MS = 8000; // exponential growth cap
  // The attempt counter is reset ONLY after the connection stays up for this long.
  // Resetting immediately on onopen allowed a server that drops right away (flapping:
  // quota/keepalive) to nullify the 3-attempt ceiling → infinite reconnect. The
  // stability window ensures only a truly stable connection refreshes the budget.
  const RECONNECT_STABLE_MS = 10000;

  // Reconnect is NOT attempted for intentional/normal closes (user expects the stop):
  //  1000 = normal, 1008 = policy (auth rejection), 1011 = server gate
  //  (flag/SDK/key missing → retrying is pointless; surface the error).
  // BUG B2 fix: 4001 (mid-session provider transient error, see gemini_live.py /
  // openai_realtime.py) is intentionally NOT in this set — those drops ARE reconnectable.
  const RECONNECT_BLOCK_CODES = new Set([1000, 1008, 1011]);

  /**
   * PURE: should reconnect on this close?
   * Only when: NOT intentional stop + attempt < ceiling + code not blocked.
   * Abnormal closes (1006 network drop, 1001 going-away, etc.) → true.
   */
  function shouldReconnect(closeCode, intentionalStop, attempt, maxAttempts) {
    if (intentionalStop) return false;
    const max = typeof maxAttempts === "number" ? maxAttempts : RECONNECT_MAX;
    if (typeof attempt === "number" && attempt >= max) return false;
    if (RECONNECT_BLOCK_CODES.has(closeCode)) return false;
    return true;
  }

  /** Exponential back-off delay (ms): 500·2^attempt, capped at ~8000 (0-based). */
  function reconnectDelayMs(attempt) {
    const n = typeof attempt === "number" && attempt > 0 ? attempt : 0;
    return Math.min(RECONNECT_BASE_MS * Math.pow(2, n), RECONNECT_CAP_MS);
  }

  // ── Runtime state (single session) ──────────────────────────────────────────

  let _active = false;
  let _ws = null;
  let _micStream = null;
  let _captureCtx = null;
  let _playbackCtx = null;
  let _playbackNode = null;
  let _state = STATES.IDLE;
  let _cb = {};
  let _opts = {}; // token/conversationId kept for _connect() reuse
  let _mode = { active: false, kind: "live", path: LIVE_PATH, inputRate: INPUT_RATE }; // active voice mode (set by pickVoiceMode in start())
  let _intentionalStop = false; // set by stop() to suppress reconnect
  // Session/supersession token: bumped by start() and stop(). Each async acquire (getUserMedia,
  // audioWorklet.addModule, WS open) re-checks it after awaiting; a mismatch means stop() (or a
  // newer start) ran during the await, so the just-resolved resource must be released instead of
  // adopted. Without this, a stop() during getUserMedia's permission prompt leaves the mic live
  // for the page lifetime (OS indicator on) with no way to release it short of a reload.
  let _startToken = 0;
  let _muted = false; // Live-mode mic mute (Aurora "Mute" button): gates outbound PCM to the provider
  // Barge latch: set by interrupt() (client "Stop"). The provider streams the whole turn
  // faster than realtime and the backend does NOT forward our control frame, so the
  // interrupted turn's tail audio keeps arriving after the flush → it would re-open playback
  // and flip the orb back to SPEAKING. While latched, _onServerMessage DROPS incoming
  // assistant audio (and does not flip state); the next turn boundary (turn_complete/ready)
  // clears it so the following turn plays normally.
  let _interruptedUntilTurn = false;
  let _established = false; // whether onopen fired at least once this session (first connect vs reconnect)
  let _attempt = 0; // consumed reconnect attempts (reset only after a STABLE connection)
  let _reconnectTimer = null; // pending setTimeout handle
  let _stableTimer = null; // stability window after onopen (fires when complete → _attempt=0)

  function _setState(event) {
    _state = nextState(_state, event);
    if (typeof _cb.onState === "function") {
      try {
        _cb.onState(_state, event);
      } catch (_e) {
        /* UI hook error must not crash the session */
      }
    }
  }

  function _wsUrl(token, conversationId) {
    // Honor the configured API base URL (localStorage akana.baseUrl / Settings connection
    // field), same as every other transport (AkanaCore.baseUrl() for REST, akana-settings.js
    // wsUrl() for the events socket) — falls back to window.location only when unset.
    const base = (window.AkanaCore && window.AkanaCore.baseUrl && window.AkanaCore.baseUrl()) ||
      `${window.location.protocol}//${window.location.host}`;
    const u = new URL(base);
    const proto = u.protocol === "https:" ? "wss:" : "ws:";
    const qs = new URLSearchParams();
    if (token) qs.set("token", token);
    if (conversationId) qs.set("conversation_id", conversationId);
    const q = qs.toString();
    // Path depends on active mode: gemini → /ws/voice/live, openai → /ws/voice/realtime.
    const path = (_mode && _mode.path) || LIVE_PATH;
    return `${proto}//${u.host}${path}${q ? "?" + q : ""}`;
  }

  function _flushPlayback() {
    if (_playbackNode) {
      try {
        _playbackNode.port.postMessage({ type: "flush" });
      } catch (_e) {
        /* ignore */
      }
    }
  }

  function _onServerMessage(ev) {
    // Binary (ArrayBuffer) = assistant audio → enqueue for playback (transfer).
    if (ev.data instanceof ArrayBuffer) {
      // Post-barge: drop the interrupted turn's still-in-flight tail (see _interruptedUntilTurn).
      // Do NOT flip to SPEAKING — that would audibly resume the reply the user just stopped.
      if (_interruptedUntilTurn) return;
      if (_playbackNode && ev.data.byteLength) {
        _setState("assistant_audio");
        try {
          _playbackNode.port.postMessage(ev.data, [ev.data]);
        } catch (_e) {
          /* ignore */
        }
      }
      return;
    }
    // Text (JSON) = control: ready/transcript/interrupt/turn_complete.
    let msg = null;
    try {
      msg = JSON.parse(ev.data);
    } catch (_e) {
      return;
    }
    if (!msg || typeof msg !== "object") return;
    switch (msg.type) {
      case "ready":
        _interruptedUntilTurn = false; // turn boundary → next turn's audio may play
        _setState("ready");
        // BUG B3 fix: adopt the server-minted conversation_id so a later reconnect
        // rebuilds the URL with it instead of the stale (often null) _opts value,
        // which would otherwise make the server mint a brand-new conversation.
        if (msg.conversation_id) _opts.conversationId = msg.conversation_id;
        if (typeof _cb.onReady === "function") _cb.onReady(msg.conversation_id || null);
        break;
      case "transcript":
        if (typeof _cb.onTranscript === "function") {
          _cb.onTranscript(msg.role || "assistant", msg.text || "");
        }
        break;
      case "interrupt":
        // The server interrupt frame IS a turn boundary: the bridge sends it AFTER it
        // stopped forwarding the old (barged) turn's audio, and no turn_complete follows
        // a cancelled turn — so bytes after this frame belong to the NEXT turn. Clear the
        // barge latch here too, otherwise a client Stop then a new question would drop the
        // whole next reply's audio (it stays latched until that turn's own turn_complete).
        _interruptedUntilTurn = false;
        _flushPlayback();
        _setState("interrupt");
        break;
      case "turn_complete":
        _interruptedUntilTurn = false; // the interrupted turn ended → clear the barge latch
        _setState("turn_complete");
        break;
      case "tool":
        // Both realtime bridges send {type:"tool",name} after dispatching a model tool
        // call, purely for UI feedback. Emit the same voice:tool bus event the classic
        // (SSE) path emits so Live mode shows the aurora tool chip too — otherwise the
        // scene sits silent through a multi-second tool dispatch. Shape matches the
        // aurora consumer (upsertVoiceTool reads call.name).
        try {
          if (typeof window !== "undefined" && window.AkanaBus) {
            window.AkanaBus.emit("voice:tool", { call: { name: msg.name || "" } });
          }
        } catch (_e) {
          /* UI feedback only — never let a bus hiccup break the message loop */
        }
        break;
      default:
        break;
    }
  }

  function _clearReconnectTimer() {
    if (_reconnectTimer !== null) {
      try {
        clearTimeout(_reconnectTimer);
      } catch (_e) {
        /* ignore */
      }
      _reconnectTimer = null;
    }
  }

  function _clearStableTimer() {
    if (_stableTimer !== null) {
      try {
        clearTimeout(_stableTimer);
      } catch (_e) {
        /* ignore */
      }
      _stableTimer = null;
    }
  }

  /** Close WS + mic/capture; KEEP the playback worklet alive (for reconnect). */
  function _teardownTransport() {
    // Connection dropped → cancel the pending stability window (otherwise it would
    // fire after close and reset _attempt incorrectly → defeats flapping protection).
    _clearStableTimer();
    if (_ws) {
      try {
        _ws.onopen = _ws.onmessage = _ws.onerror = _ws.onclose = null;
        _ws.close();
      } catch (_e) {
        /* ignore */
      }
      _ws = null;
    }
    if (_micStream) {
      try {
        _micStream.getTracks().forEach((t) => t.stop());
      } catch (_e) {
        /* ignore */
      }
      _micStream = null;
    }
    if (_captureCtx) {
      try {
        _captureCtx.close();
      } catch (_e) {
        /* ignore */
      }
      _captureCtx = null;
    }
  }

  async function _startPlayback() {
    const token = _startToken;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const ctx = new Ctx({ sampleRate: OUTPUT_RATE });
    _playbackCtx = ctx;
    await ctx.audioWorklet.addModule("/static/pcm-playback-worklet.js");
    // stop() ran during the worklet-module load → do NOT build a node on the now-closed context
    // (constructing an AudioWorkletNode on it throws a spurious "start-failed" after the user exited).
    if (_intentionalStop || !_active || token !== _startToken) {
      try { ctx.close(); } catch (_e) { /* ignore */ }
      if (_playbackCtx === ctx) _playbackCtx = null;
      return;
    }
    _playbackNode = new AudioWorkletNode(ctx, "akana-pcm-playback");
    _playbackNode.connect(ctx.destination);
  }

  // If getUserMedia rejects due to mic permission/device, attach a portable flag instead
  // of a generic error: the reconnect catch surfaces it as "mic-permission"
  // (otherwise the user sees "disconnected" and can't diagnose the permission issue).
  function _isMicPermError(e) {
    const n = e && e.name;
    return n === "NotAllowedError" || n === "NotFoundError";
  }

  async function _startCapture() {
    const token = _startToken;
    // True when stop()/a newer start() ran during an await → abort and release what we got.
    const superseded = () => _intentionalStop || !_active || token !== _startToken;
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (e) {
      if (_isMicPermError(e)) {
        const err = new Error(String((e && e.message) || e));
        err.micPermission = true;
        throw err;
      }
      throw e;
    }
    // stop() during the permission prompt / device acquisition → the just-granted stream would
    // otherwise stay live for the page lifetime. Release it and bail (mirrors the barge detector).
    if (superseded()) {
      try { stream.getTracks().forEach((t) => t.stop()); } catch (_e) { /* ignore */ }
      return;
    }
    _micStream = stream;
    const Ctx = window.AudioContext || window.webkitAudioContext;
    const captureCtx = new Ctx();
    const inRate = captureCtx.sampleRate || 48000;
    await captureCtx.audioWorklet.addModule("/static/audio-capture-processor.js");
    if (superseded()) {
      // Torn down while the worklet module loaded → release the stream + context we just built.
      try { stream.getTracks().forEach((t) => t.stop()); } catch (_e) { /* ignore */ }
      try { captureCtx.close(); } catch (_e) { /* ignore */ }
      if (_micStream === stream) _micStream = null;
      return;
    }
    _captureCtx = captureCtx;
    const src = _captureCtx.createMediaStreamSource(_micStream);
    const node = new AudioWorkletNode(_captureCtx, "akana-capture");
    node.port.onmessage = (e) => {
      const float32 = e.data;
      if (!float32 || !float32.length) return;
      if (_muted) return; // muted (Aurora "Mute") → do NOT stream mic PCM to the cloud provider
      if (!_ws || _ws.readyState !== 1) return; // 1 = OPEN
      // Target input rate depends on mode: gemini=16k, openai realtime=24k (output is always 24k).
      const targetRate = (_mode && _mode.inputRate) || INPUT_RATE;
      const at16 = downsample(float32, inRate, targetRate);
      const frame = encodeAudioFrame(floatTo16BitPCM(at16));
      try {
        _ws.send(frame);
      } catch (_e) {
        /* socket closing — ignore */
      }
    };
    src.connect(node);
    // Connect to a silent sink to keep the worklet alive (output is inaudible: gain 0).
    const sink = _captureCtx.createGain();
    sink.gain.value = 0;
    node.connect(sink);
    sink.connect(_captureCtx.destination);
  }

  /**
   * Core WS+capture connection — called by both `start()` and the reconnect timer.
   * `established=false` (first connect): if closed before onopen, rejects
   * (start()'s catch surfaces the error and calls stop(); NO infinite loop).
   * `established=true` (reconnect): `onclose` makes the reconnect decision on close.
   */
  async function _connect() {
    const token = _startToken;
    // Set up the playback worklet if not already alive (usually preserved across reconnects).
    if (!_playbackNode) await _startPlayback();
    // stop() ran during the worklet load → do not open a socket for a dead session.
    if (_intentionalStop || !_active || token !== _startToken) return;
    const ws = new WebSocket(_wsUrl(_opts.token, _opts.conversationId));
    _ws = ws;
    ws.binaryType = "arraybuffer";
    ws.onmessage = _onServerMessage;
    ws.onerror = () => {
      // BUG B1 fix: do NOT call _cb.onError here — an abnormal closure fires 'error'
      // BEFORE 'close', and the app-level onError treated it as fatal (stop() nulls
      // _ws.onclose before _onWsClose can run its reconnect/error decision). Leave
      // _onWsClose as the single decision point for reconnect vs surfaced error.
      _setState("error");
    };
    ws.onclose = _onWsClose;
    await new Promise((resolve, reject) => {
      ws.onopen = () => {
        _established = true;
        // Do NOT reset counter immediately (flapping server causes infinite loop): only
        // replenish the budget if the connection survives the stability window. If it
        // drops before the window expires, _attempt is preserved → stops after 3 tries.
        _clearStableTimer();
        _stableTimer = setTimeout(() => {
          _attempt = 0;
          _stableTimer = null;
        }, RECONNECT_STABLE_MS);
        resolve();
      };
      // If WS closes before onopen, reject the Promise (else it hangs) + reconnect logic.
      const prevClose = ws.onclose;
      ws.onclose = (ev) => {
        reject(new Error("ws closed before open"));
        if (prevClose) prevClose(ev);
      };
      // stop() nulls ws.onopen/onclose then calls ws.close() during teardown, so neither the
      // resolve nor the reject above would ever fire → this promise (and L.start) would hang.
      // A close listener added via addEventListener is NOT cleared by the onclose=null teardown,
      // so it still settles the promise when the socket closes.
      ws.addEventListener("close", () => reject(new Error("ws closed before open")));
    });
    await _startCapture();
  }

  /**
   * Persistent onclose — only decides on reconnect for closes that happen AFTER the
   * session is established (first-connect errors are handled by the Promise reject above).
   * Intentional stop() suppresses reconnect; blocked codes (1000/1008/1011) surface the error.
   */
  function _onWsClose(ev) {
    const code = ev ? ev.code : 0;
    if (_intentionalStop || !_active) return; // stop() teardown is running

    if (!_established) {
      // Closed before first connection was established: surface code 1011; start() handles the rest.
      if (code === 1011 && typeof _cb.onError === "function") {
        _cb.onError("unavailable", (ev && ev.reason) || "");
      }
      return;
    }

    if (shouldReconnect(code, _intentionalStop, _attempt, RECONNECT_MAX)) {
      _attempt += 1;
      _teardownTransport(); // close WS+mic, preserve playback worklet (barge-in keeps running)
      _setState("reconnecting");
      const delay = reconnectDelayMs(_attempt - 1);
      _reconnectTimer = setTimeout(() => {
        _reconnectTimer = null;
        if (_intentionalStop || !_active) return;
        _connect().catch((e) => {
          // If mic permission/device was lost during reconnect, surface the
          // permission-specific code instead of generic "disconnected" so the user can fix it.
          if (e && e.micPermission && typeof _cb.onError === "function") {
            _cb.onError("mic-permission", String(e));
            stop();
            return;
          }
          // This attempt's onclose may have already scheduled a new reconnect;
          // if not (budget exhausted), surface the error and clean up.
          if (_reconnectTimer === null && !_intentionalStop) {
            if (typeof _cb.onError === "function") {
              _cb.onError("disconnected", String(e));
            }
            stop();
          }
        });
      }, delay);
      return;
    }

    // No reconnect (blocked code or budget exhausted) → surface error and close.
    if (typeof _cb.onError === "function") {
      _cb.onError(code === 1011 ? "unavailable" : "disconnected", (ev && ev.reason) || "");
    }
    stop();
  }

  async function start(opts) {
    opts = opts || {};
    if (_active) return false;
    _active = true;
    _intentionalStop = false;
    _established = false;
    _attempt = 0;
    _muted = false; // fresh session starts unmuted
    _interruptedUntilTurn = false; // fresh session: no pending barge latch
    _startToken += 1; // new session → supersede any in-flight acquire from a prior start/stop
    _clearReconnectTimer();
    // Pick the active voice mode (Gemini Live / OpenAI Realtime) from /voice/config →
    // determines _wsUrl path + input sample rate. Falls back to gemini-live if config is absent.
    _mode = pickVoiceMode(opts.config);
    if (!_mode.active) _mode = { active: false, kind: "live", path: LIVE_PATH, inputRate: INPUT_RATE };
    _opts = { token: opts.token, conversationId: opts.conversationId, config: opts.config };
    _cb = {
      onState: opts.onState,
      onTranscript: opts.onTranscript,
      onReady: opts.onReady,
      onError: opts.onError,
    };
    _setState("connecting");
    try {
      await _connect();
      return true;
    } catch (e) {
      // If mic permission is denied on the first connect, surface the permission-specific
      // code (instead of generic "start-failed"; consistent with the reconnect path).
      const kind = e && e.micPermission ? "mic-permission" : "start-failed";
      if (typeof _cb.onError === "function") _cb.onError(kind, String(e));
      stop();
      return false;
    }
  }

  function stop() {
    _intentionalStop = true; // prevent pending/future onclose handlers from reconnecting
    _active = false;
    _startToken += 1; // supersede any acquire (getUserMedia/worklet/WS open) still awaiting
    _clearReconnectTimer();
    _teardownTransport(); // WS + mic + capture
    if (_playbackCtx) {
      try {
        _playbackCtx.close();
      } catch (_e) {
        /* ignore */
      }
    }
    _playbackCtx = null;
    _playbackNode = null;
    _setState("stop");
  }

  function isActive() {
    return _active;
  }

  /** Live-mode mic mute (Aurora "Mute" button). The turn-based path stops SpeechRecognition, but
   *  Live mode streams PCM from its own _micStream — so muting must gate that stream, or the UI
   *  says "Muted" while the mic keeps flowing to Gemini/OpenAI (privacy bug). Disable the tracks
   *  (OS-level: nothing is captured) AND gate the outbound send as belt-and-braces. */
  function setMuted(muted) {
    _muted = !!muted;
    if (_micStream) {
      try {
        _micStream.getTracks().forEach((t) => { t.enabled = !_muted; });
      } catch (_e) {
        /* ignore — the send-gate above still blocks outbound PCM */
      }
    }
  }

  /** Client-initiated barge (Aurora "Stop" button in Live mode). The turn-based Stop path
   *  (onConversationBargeIn) touches only SSE/TTS objects Live mode never uses, so live playback
   *  would keep talking. Mirror the server ``interrupt`` frame client-side: flush the buffered PCM
   *  (the provider streams the whole turn faster than realtime, so the tail is already queued in
   *  the playback worklet) and drive the state machine to LISTENING so the orb/accumulators reset.
   *  No-op when no session is active. */
  function interrupt() {
    if (!_active) return;
    // Latch until the next turn boundary so the interrupted turn's tail audio (already
    // queued ahead of realtime; the backend ignores our control frame) is dropped instead
    // of re-opening playback and flipping the orb back to SPEAKING.
    _interruptedUntilTurn = true;
    _flushPlayback();
    _setState("interrupt");
  }

  const api = {
    AUDIO_TAG,
    INPUT_RATE,
    OUTPUT_RATE,
    STATES,
    floatTo16BitPCM,
    encodeAudioFrame,
    nextState,
    shouldUseLive,
    liveToggleVisible,
    shouldReconnect,
    reconnectDelayMs,
    start,
    stop,
    isActive,
    setMuted,
    interrupt,
  };
  if (typeof window !== "undefined") window.AkanaVoiceLive = api;
  if (typeof globalThis !== "undefined") globalThis.AkanaVoiceLive = api;
})();
