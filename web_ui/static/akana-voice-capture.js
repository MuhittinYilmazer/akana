/**
 * Akana voice capture — mic graph, resample, WAV encode.
 */
// i18n helper (bilingual — loaded before this module)
const _captureT = (k) => (typeof window !== "undefined" && window.AkanaI18n?.t ? window.AkanaI18n.t(k) : k);
(() => {
  function downsampleFloat32(buf, inRate, outRate) {
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

  function rms(chunk) {
    let s = 0;
    for (let i = 0; i < chunk.length; i++) s += chunk[i] * chunk[i];
    return Math.sqrt(s / Math.max(1, chunk.length));
  }

  function encodeWavPcm16Mono(float32At16k) {
    const sampleRate = 16000;
    const n = float32At16k.length;
    const buf = new ArrayBuffer(44 + n * 2);
    const view = new DataView(buf);
    const writeStr = (off, str) => {
      for (let i = 0; i < str.length; i++) view.setUint8(off + i, str.charCodeAt(i));
    };
    writeStr(0, "RIFF");
    view.setUint32(4, 36 + n * 2, true);
    writeStr(8, "WAVE");
    writeStr(12, "fmt ");
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeStr(36, "data");
    view.setUint32(40, n * 2, true);
    const pcm = new Int16Array(buf, 44, n);
    for (let i = 0; i < n; i++) {
      const x = Math.max(-1, Math.min(1, float32At16k[i]));
      pcm[i] = x < 0 ? x * 0x8000 : x * 0x7fff;
    }
    return new Blob([buf], { type: "audio/wav" });
  }
  function mergeChunks(chunks) {
    const total = chunks.reduce((a, p) => a + p.length, 0);
    const merged = new Float32Array(total);
    let o = 0;
    for (const p of chunks) {
      merged.set(p, o);
      o += p.length;
    }
    return merged;
  }

  function createCapture(bridge) {
      function appendRawToBuffer(chunk) {
        const nb = new Float32Array(bridge.voice.rawBuffer.length + chunk.length);
        nb.set(bridge.voice.rawBuffer);
        nb.set(chunk, bridge.voice.rawBuffer.length);
        bridge.voice.rawBuffer = nb;
        const maxKeep = Math.floor(bridge.voice.inSampleRate * bridge.voice.maxRawSeconds);
        if (bridge.voice.rawBuffer.length > maxKeep) bridge.voice.rawBuffer = bridge.voice.rawBuffer.slice(-maxKeep);
      }

      function stopAudioGraph() {
        const keepWakeArmed = bridge.session.isWakeArmed();
        bridge.stopBrowserLiveTranscript();
        try {
          bridge.voice.processor?.disconnect();
        } catch {
          /* ignore */
        }
        try {
          bridge.voice.source?.disconnect();
        } catch {
          /* ignore */
        }
        try {
          bridge.voice.mute?.disconnect();
        } catch {
          /* ignore */
        }
        bridge.voice.processor = null;
        try {
          bridge.voice.worklet?.port?.close();
        } catch {
          /* ignore */
        }
        try {
          bridge.voice.worklet?.disconnect();
        } catch {
          /* ignore */
        }
        bridge.voice.worklet = null;
        bridge.voice.workletModuleLoaded = false;
        bridge.voice.source = null;
        bridge.voice.mute = null;
        if (bridge.voice.stream) {
          bridge.voice.stream.getTracks().forEach((t) => t.stop());
          bridge.voice.stream = null;
        }
        if (bridge.voice.audioCtx) {
          bridge.voice.audioCtx.close().catch(() => {});
          bridge.voice.audioCtx = null;
        }
        clearInterval(bridge.voice.wakeInterval);
        bridge.voice.wakeInterval = null;
        bridge.stopSpeechWakeFallback();
        bridge.voice.rawBuffer = new Float32Array(0);
        bridge.voice.utterChunks = [];
        bridge.voice.hadSpeech = false;
        bridge.voice.silenceMs = 0;
        bridge.voice.utterFinishing = false;
        bridge.voice.cancelled = false;
        bridge.voice.ambientRms = 0;
        bridge.voice.ambientSamplesCollected = 0;
        bridge.voice.liveTranscriptUserEdit = false;
        if (!keepWakeArmed) {
          bridge.session.resetHardware("stopAudioGraph");
        }
      }
      function handleAudioChunk(copy, chunkMs) {
        appendRawToBuffer(copy);
        // Liveness stamp: the capture-phase watchdog uses this to tell a turn that has gone
        // deaf mid-utterance (mic revoked without an `ended` event) from one still receiving audio.
        bridge.voice.lastChunkTs = Date.now();
        // Barge-in guard: don't run wake polling while TTS is playing or cancel pending.
        if (
          bridge.voice.wakeEnabled &&
          !bridge.voice.utteranceActive &&
          !bridge.voice.micManual &&
          !bridge.ttsPlayer.playing
        ) {
          bridge.voice.wakeMeterTick += chunkMs;
          if (bridge.voice.wakeMeterTick > 250) {
            bridge.voice.wakeMeterTick = 0;
            const need = Math.floor(bridge.voice.inSampleRate * bridge.voice.wakeWindowSec);
            const slice =
              bridge.voice.rawBuffer.length >= need
                ? bridge.voice.rawBuffer.slice(-need)
                : bridge.voice.rawBuffer;
            bridge.updateWakeMeter({ rmsVal: rms(slice) });
          }
        }
        const capturing = bridge.voice.utteranceActive || bridge.voice.micManual;
        // NOTE: there is intentionally NO raw-mic barge-in here. Without AEC, Akana would hear
        // its own TTS on the raw mic and cut itself off. Real barge-in lives in akana-voice.js
        // via a separate AEC mic stream + AnalyserNode (voice.bargeInEnabled → bargeDetector).
        if (capturing) {
          bridge.voice.utterChunks.push(new Float32Array(copy));
          // Hard safety cap: in the browser-SR conversation path the RMS-VAD auto-finalize
          // (which would otherwise bound this via noSpeechTimeoutMs/utterMaxMs) is disabled
          // below and end-of-turn is driven solely by browser SR — a listening turn where SR
          // never delivers a result (silence, or muted via the Aurora button) would otherwise
          // buffer unbounded audio for as long as the turn stays open. (In the Whisper path the
          // VAD does bound the turn, but this ceiling is a harmless backstop there too.) Drop
          // the oldest chunks once the buffer exceeds a generous ceiling so memory stays bounded.
          const maxChunks = Math.ceil(
            ((bridge.voice.utterMaxSeconds || 120) * bridge.voice.inSampleRate) /
              Math.max(1, copy.length),
          );
          if (bridge.voice.utterChunks.length > maxChunks) {
            bridge.voice.utterChunks.splice(0, bridge.voice.utterChunks.length - maxChunks);
          }
        }
        // CONVERSATION MODE end-of-turn detection depends on the STT source:
        //   • browser SR (DEFAULT, convVadEnabled=false): utterChunks buffer (above) but
        //     the worklet RMS-VAD/auto-finalize path does NOT run — end-of-turn is driven
        //     by browser SR (finalizeConversationFromSR). Byte-for-byte the old behaviour.
        //   • Whisper (convVadEnabled=true): the RMS-VAD auto-finalize below runs IN
        //     conversation mode too, so acoustic silence detection ends the turn and feeds
        //     the buffered audio through finalizeUtterance → postConversationBlob
        //     (/voice/transcribe). finalizeConversationFromSR is suppressed in this mode,
        //     so exactly one submission fires per utterance.
        const convVadOn = bridge.voice.conversationMode && bridge.voice.convVadEnabled;
        if (
          bridge.voice.utteranceActive &&
          !bridge.voice.micManual &&
          (!bridge.voice.conversationMode || convVadOn)
        ) {
          const r = rms(copy);

          // Calibrate ambient noise floor from the first ambientSamplesNeeded chunks of the
          // utterance (~213ms at the worklet's 2048-sample batch, NOT the ~400ms an older
          // 80ms-chunk assumption implied). CRITICAL: do NOT let the user's own speech ONSET
          // poison the floor. Conversation capture opens exactly at the user's turn, so those
          // first chunks are frequently the loud onset of their first word; folding that in
          // calibrates ambientRms to SPEECH level, which inflates speechThr/silenceThr below so
          // subsequent normal speech reads as "silence" and the RMS-VAD finalizes mid-utterance
          // (premature end-of-turn). Only fold a chunk into the floor when it is below the static
          // voice floor (plausibly non-speech). If every calibration chunk is already speech-loud
          // (immediate talker), ambientRms stays 0 and the thresholds fall back to the safe static
          // voiceRms floor instead of inflating. Trade-off: a room noisier than voiceRms loses
          // adaptive calibration (falls back to the static floor) — acceptable on the opt-in
          // whisper path, and strictly better than mis-calibrating on speech.
          if (bridge.voice.ambientSamplesCollected < bridge.voice.ambientSamplesNeeded) {
            if (r < bridge.voice.voiceRms) {
              bridge.voice.ambientRms = Math.max(bridge.voice.ambientRms, r);
            }
            bridge.voice.ambientSamplesCollected += 1;
          }
          // Adaptive thresholds: speech needs to noticeably exceed ambient,
          // silence is "close to ambient or quieter".
          const speechThr = Math.max(bridge.voice.voiceRms, bridge.voice.ambientRms * 2.2);
          const silenceThr = Math.max(bridge.voice.voiceRms * 0.45, bridge.voice.ambientRms * 1.4);
          if (r > speechThr) bridge.voice.hadSpeech = true;
          if (bridge.voice.hadSpeech && r < silenceThr) bridge.voice.silenceMs += chunkMs;
          else bridge.voice.silenceMs = 0;

          // Diagnostics: log conversation-mode VAD state every ~500ms when
          // localStorage.akana_wake_debug="1" (zero cost when off).
          if (bridge.voice.conversationMode && bridge.wakeDebugEnabled?.()) {
            bridge.voice._convDbgMs = (bridge.voice._convDbgMs || 0) + chunkMs;
            if (bridge.voice._convDbgMs > 500) {
              bridge.voice._convDbgMs = 0;
              console.debug(
                `[conv-vad] rms=${r.toFixed(4)} speechThr=${speechThr.toFixed(4)} ` +
                  `silenceThr=${silenceThr.toFixed(4)} hadSpeech=${bridge.voice.hadSpeech} ` +
                  `silenceMs=${bridge.voice.silenceMs | 0} elapsed=${(Date.now() - bridge.voice.utterStartTs) | 0}`,
              );
            }
          }

          if (!bridge.voice.utterFinishing) {
            const elapsed = Date.now() - bridge.voice.utterStartTs;
            if (!bridge.voice.hadSpeech && elapsed > bridge.voice.noSpeechTimeoutMs) {
              // Wake mis-fire: no speech detected at all → quietly drop.
              bridge.voice.cancelled = true;
              void bridge.finalizeUtterance();
            } else if (elapsed > bridge.voice.utterMaxMs) {
              void bridge.finalizeUtterance();
            } else if (
              bridge.voice.hadSpeech &&
              // Conversation (whisper) mode gets a more forgiving hold than wake capture so a
              // brief pre-verb pause / quiet trailing word is not clipped; reaching here with
              // conversationMode set implies convVadEnabled (the gate above). Wake keeps 650 ms.
              bridge.voice.silenceMs >
                (bridge.voice.conversationMode
                  ? bridge.voice.convSilenceHoldMs || 900
                  : bridge.voice.silenceHoldMs)
            ) {
              void bridge.finalizeUtterance();
            }
          }
        }
      }

      /** A capture track ended unexpectedly (mic unplugged / OS revoked it mid-session). The
       *  graph is now feeding nothing but rawBuffer still holds the last window, so wake would
       *  re-POST stale audio forever and a whisper turn would hang. Tear the stale graph down,
       *  surface a one-time notice, and re-acquire — preferring the default device if the chosen
       *  one is gone. No-op during an intentional teardown (stream already cleared). */
      async function handleDeviceLoss() {
        // Intentional teardown already ran (stopAudioGraph nulled the stream) → nothing to do.
        if (!bridge.voice.stream) return;
        // Only the ACTIVE stream's track loss matters; ignore late events from a superseded one.
        if (bridge.voice.stream.getTracks().every((t) => t.readyState === "live")) return;
        if (bridge.voice._deviceLossHandling) return; // one recovery at a time (multi-track streams)
        // Re-acquire cap: if getUserMedia keeps resolving with a track that dies moments later
        // (device truly gone / a flapping Bluetooth mic), the freshly-acquired track's own
        // `ended` re-enters here after the finally clears _deviceLossHandling → an unbounded
        // event-driven re-acquire loop. Count consecutive losses and stop re-arming after a
        // hard cap; a stream that survives past _DEVICE_LOSS_SURVIVE_MS resets the counter so
        // ordinary, well-spaced unplugs still recover every time. The one-time notice stays as
        // the terminal state.
        const DEVICE_LOSS_MAX = 3;
        const DEVICE_LOSS_SURVIVE_MS = 5000;
        const _now = Date.now();
        if (_now - (bridge.voice._lastDeviceLossTs || 0) > DEVICE_LOSS_SURVIVE_MS) {
          bridge.voice._deviceLossCount = 0; // previous acquire survived long enough → healthy
        }
        bridge.voice._lastDeviceLossTs = _now;
        bridge.voice._deviceLossCount = (bridge.voice._deviceLossCount || 0) + 1;
        if (bridge.voice._deviceLossCount > DEVICE_LOSS_MAX) {
          // The device is genuinely gone (or flapping too fast to use) → stop re-arming and tear
          // the stale graph down so the frozen rawBuffer can't keep re-POSTing; leave the notice.
          try { stopAudioGraph(); } catch { /* ignore */ }
          bridge.voice.rawBuffer = new Float32Array(0);
          return;
        }
        bridge.voice._deviceLossHandling = true;
        try {
          const wasWakeArmed = bridge.session.isWakeArmed();
          const wasConversation = bridge.voice.conversationMode;
          stopAudioGraph();
          bridge.voice.rawBuffer = new Float32Array(0);
          if (!bridge.voice._micDisconnectNoticeShown) {
            bridge.voice._micDisconnectNoticeShown = true;
            try {
              bridge.hooks.appendRow(
                `<div class="meta">${_captureT("voice.meta_voice")}</div>` +
                  `<div class="bubble-bot">${_captureT("voice.mic_disconnected")}</div>`,
              );
            } catch { /* ignore — notice is non-fatal */ }
          }
          // The chosen mic is likely gone → re-acquire on the default device (exact deviceId would
          // throw OverconstrainedError). ensureAudioInner honors this flag for the next acquire.
          bridge.voice._micDeviceLost = true;
          try {
            if (wasWakeArmed) {
              await bridge.setWakeListening(true, { silent: true });
            } else if (wasConversation) {
              try { bridge.maybeReArmConversation?.("deviceLoss"); } catch { /* ignore */ }
            }
          } finally {
            bridge.voice._micDeviceLost = false;
          }
        } finally {
          bridge.voice._deviceLossHandling = false;
        }
      }

      async function ensureAudio() {
        // In-flight guard: two concurrent callers (e.g. syncOrbWithVoice's
        // resumeWakeListeningIfIdle firing back-to-back with another resumeWakeListeningIfIdle
        // in the same tick) can both pass the stream-null check below before either awaits
        // getUserMedia, double-acquiring the mic and leaking the first stream/worklet.
        // Latch on a shared in-flight promise so late callers await the same graph.
        if (bridge.voice._ensureAudioInFlight) return bridge.voice._ensureAudioInFlight;
        const p = ensureAudioInner();
        bridge.voice._ensureAudioInFlight = p;
        try {
          await p;
        } finally {
          if (bridge.voice._ensureAudioInFlight === p) bridge.voice._ensureAudioInFlight = null;
        }
      }

      async function ensureAudioInner() {
        const AC = window.AudioContext || window.webkitAudioContext;
        if (!bridge.voice.audioCtx) {
          bridge.voice.audioCtx = new AC();
          bridge.voice.inSampleRate = bridge.voice.audioCtx.sampleRate;
          bridge.voice.workletModuleLoaded = false;
        }
        if (bridge.voice.audioCtx.state === "suspended") {
          await bridge.voice.audioCtx.resume();
        }
        if (bridge.voice.stream && (bridge.voice.worklet || bridge.voice.processor)) return;

        // Echo/noise suppression can distort the mic signal and break VAD (it was
        // also stripping wake energy) → disabled across ALL modes = same as
        // wake/PTT, proven raw-mic path. Conversation mode is half-duplex: the
        // microphone is silent while Akana speaks, so AEC is not needed (barge-in PR4).
        const preferredMic = localStorage.getItem("akana.micDevice") || "";
        const audioConstraints = {
          channelCount: 1,
          echoCancellation: false,
          noiseSuppression: false,
          autoGainControl: true,
        };
        // Skip the exact-deviceId constraint when recovering from a device loss: the preferred
        // mic is gone, so { exact } would throw OverconstrainedError — fall back to the default.
        if (preferredMic && !bridge.voice._micDeviceLost) audioConstraints.deviceId = { exact: preferredMic };
        bridge.voice.stream = await navigator.mediaDevices.getUserMedia({
          audio: audioConstraints,
        });
        // Device-loss recovery: a USB/Bluetooth mic unplug (or OS revoke) mid-session silently
        // ENDS the track — handleAudioChunk stops firing, but rawBuffer keeps its last ~4s so the
        // wake poll re-POSTs the SAME frozen window every 300ms forever, and a whisper turn hangs
        // on "Listening". Attach an `ended` handler to tear the stale graph down and re-acquire
        // (falling back to the default device if the preferred one is gone). Guard against the
        // multiple `ended` events a multi-track stream can emit.
        for (const track of bridge.voice.stream.getTracks()) {
          track.addEventListener("ended", () => void handleDeviceLoss());
        }
        // Permission is now granted — labels become available; refresh the picker.
        void bridge.refreshMicDeviceList();
        bridge.voice.source = bridge.voice.audioCtx.createMediaStreamSource(bridge.voice.stream);
        bridge.voice.mute = bridge.voice.audioCtx.createGain();
        bridge.voice.mute.gain.value = 0;

        if (bridge.voice.audioCtx.audioWorklet) {
          try {
            if (!bridge.voice.workletModuleLoaded) {
              await bridge.voice.audioCtx.audioWorklet.addModule("/static/audio-capture-processor.js");
              bridge.voice.workletModuleLoaded = true;
            }
            bridge.voice.worklet = new AudioWorkletNode(bridge.voice.audioCtx, "akana-capture");
            bridge.voice.worklet.port.onmessage = (ev) => {
              // PERF: the worklet now batches ~2048 samples → compute chunkMs from
              // the REAL chunk length (assuming a fixed 128-sample quantum would
              // skew silence counting 16×). Robust to variable length; VAD timing
              // is preserved.
              if (ev.data instanceof Float32Array) {
                const ms = (ev.data.length / bridge.voice.inSampleRate) * 1000;
                handleAudioChunk(ev.data, ms);
              }
            };
            bridge.voice.source.connect(bridge.voice.worklet);
            bridge.voice.worklet.connect(bridge.voice.mute);
            bridge.voice.mute.connect(bridge.voice.audioCtx.destination);
            return;
          } catch (e) {
            console.warn(_captureT("voice.warn_worklet_unavailable"), e);
            bridge.voice.worklet = null;
          }
        }

        const bufferSize = 4096;
        const legacyChunkMs = (bufferSize / bridge.voice.inSampleRate) * 1000;
        const proc = bridge.voice.audioCtx.createScriptProcessor(bufferSize, 1, 1);
        proc.onaudioprocess = (ev) => {
          const ch = ev.inputBuffer.getChannelData(0);
          handleAudioChunk(new Float32Array(ch), legacyChunkMs);
        };
        bridge.voice.source.connect(proc);
        proc.connect(bridge.voice.mute);
        bridge.voice.mute.connect(bridge.voice.audioCtx.destination);
        bridge.voice.processor = proc;
      }

    return { appendRawToBuffer, stopAudioGraph, handleAudioChunk, ensureAudio };
  }

  window.AkanaVoiceCapture = {
    downsampleFloat32,
    rms,
    encodeWavPcm16Mono,
    mergeChunks,
    createCapture,
  };
})();
