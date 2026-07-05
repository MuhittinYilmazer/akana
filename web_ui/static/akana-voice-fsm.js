/**
 * Akana voice session — explicit finite-state machine.
 * All capture / wake / mic / cancel paths must go through transition().
 */
// i18n helper (bilingual — loaded before this module)
const _fsmT = (k) => (typeof window !== "undefined" && window.AkanaI18n?.t ? window.AkanaI18n.t(k) : k);
(() => {
  const Phase = Object.freeze({
    IDLE: "idle",
    WAKE_ARMED: "wake_armed",
    CAPTURE_WAKE: "capture_wake",
    CAPTURE_MIC: "capture_mic",
    PROCESSING: "processing",
    SPEAKING: "speaking",
  });

  /** @typedef {'idle'|'wake_armed'|'capture_wake'|'capture_mic'|'processing'|'speaking'} VoicePhase */
  // Phase strings are FSM identity keys — do NOT translate them.

  const ALLOWED = {
    idle: ["wake_armed", "capture_wake", "capture_mic", "processing", "speaking"],
    wake_armed: ["idle", "capture_wake", "capture_mic", "processing", "speaking"],
    capture_wake: ["wake_armed", "idle", "capture_mic", "processing", "speaking"],
    capture_mic: ["wake_armed", "idle", "processing", "speaking"],
    processing: ["wake_armed", "idle", "speaking", "capture_wake", "capture_mic"],
    speaking: ["wake_armed", "idle", "processing", "capture_wake", "capture_mic"],
  };

  function createVoiceSession(options = {}) {
    const { onTransition = () => {}, onEpochBump = () => {} } = options;

    /** User intent: Hey Akana listening enabled (armed). */
    let wakeArmed = false;
    /** Runtime phase (exclusive). */
    let phase = Phase.IDLE;
    let epoch = 0;

    function bumpEpoch() {
      epoch += 1;
      onEpochBump(epoch);
      return epoch;
    }

    function getPhase() {
      return phase;
    }

    function isWakeArmed() {
      return wakeArmed;
    }

    function getEpoch() {
      return epoch;
    }

    function epochMatches(e) {
      return e === epoch;
    }

    function isCapturing() {
      return phase === Phase.CAPTURE_WAKE || phase === Phase.CAPTURE_MIC;
    }

    function isCaptureWake() {
      return phase === Phase.CAPTURE_WAKE;
    }

    function isCaptureMic() {
      return phase === Phase.CAPTURE_MIC;
    }

    function canTransition(to) {
      const allowed = ALLOWED[phase];
      return !!allowed && allowed.includes(to);
    }

    /**
     * @param {VoicePhase} to
     * @param {string} reason
     * @param {{ force?: boolean }} [opts]
     */
    function transition(to, reason, opts = {}) {
      if (!Object.prototype.hasOwnProperty.call(ALLOWED, to)) {
        // Unknown phase is rejected even with force — otherwise the FSM
        // would enter a phase with no table entry, blocking all future transitions.
        console.warn(`${_fsmT("voice.fsm_unknown_phase")} ${to} (${reason})`);
        return false;
      }
      if (phase === to) {
        return true;
      }
      if (!opts.force && !canTransition(to)) {
        console.warn(`${_fsmT("voice.fsm_reject")} ${phase} → ${to} (${reason})`);
        return false;
      }
      const from = phase;
      phase = to;
      // Capture buffers are cleared by the host onTransition handler (not here).
      if (to === Phase.IDLE || to === Phase.CAPTURE_MIC || to === Phase.PROCESSING) {
        if (from === Phase.CAPTURE_WAKE || from === Phase.CAPTURE_MIC) {
          bumpEpoch();
        }
      }
      if (opts.bumpEpoch) bumpEpoch();
      onTransition(from, to, reason);
      return true;
    }

    function setWakeArmed(on, reason = "setWakeArmed") {
      wakeArmed = !!on;
      if (!wakeArmed && phase === Phase.WAKE_ARMED) {
        transition(Phase.IDLE, `${reason}:wakeOff`, { force: true });
      } else if (wakeArmed && phase === Phase.IDLE) {
        transition(Phase.WAKE_ARMED, `${reason}:wakeOn`, { force: true });
      } else {
        onTransition(phase, phase, reason);
      }
    }

    /** Cancels everything except the wakeArmed preference; returns true if capture was active. */
    function cancelAll(reason = "cancel") {
      const wasCapturing = isCapturing();
      bumpEpoch();
      if (wakeArmed) {
        transition(Phase.WAKE_ARMED, reason, { force: true });
      } else {
        transition(Phase.IDLE, reason, { force: true });
      }
      return wasCapturing;
    }

    function resetHardware(reason = "resetHardware") {
      wakeArmed = false;
      bumpEpoch();
      transition(Phase.IDLE, reason, { force: true });
    }

    /**
     * Returns the effective UI phase, overlaying processing/speaking from external flags.
     * @param {{ postInFlight?: boolean, ttsPlaying?: boolean, chatInFlight?: boolean }} ext
     */
    function getUiPhase(ext = {}) {
      if (ext.ttsPlaying) return Phase.SPEAKING;
      if (ext.postInFlight || phase === Phase.PROCESSING) return Phase.PROCESSING;
      if (phase === Phase.CAPTURE_MIC) return Phase.CAPTURE_MIC;
      if (phase === Phase.CAPTURE_WAKE) return Phase.CAPTURE_WAKE;
      if (phase === Phase.WAKE_ARMED || wakeArmed) return Phase.WAKE_ARMED;
      return Phase.IDLE;
    }

    function showCancelButton(ext = {}) {
      const p = getUiPhase(ext);
      return (
        p === Phase.CAPTURE_MIC ||
        p === Phase.CAPTURE_WAKE ||
        p === Phase.PROCESSING ||
        p === Phase.SPEAKING
      );
    }

    return {
      Phase,
      getPhase,
      isWakeArmed,
      setWakeArmed,
      getEpoch,
      epochMatches,
      bumpEpoch,
      isCapturing,
      isCaptureWake,
      isCaptureMic,
      canTransition,
      transition,
      cancelAll,
      resetHardware,
      getUiPhase,
      showCancelButton,
    };
  }

  window.AkanaVoiceFsm = { Phase, createVoiceSession };
})();
