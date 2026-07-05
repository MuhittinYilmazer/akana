/**
 * Akana voice pipeline — STT/TTS HTTP, wake poll, finalize utterances.
 */
// i18n helper (bilingual — loaded before this module)
const _pipeT = (k, vars) => {
  const base = (typeof window !== "undefined" && window.AkanaI18n?.t) ? window.AkanaI18n.t(k) : k;
  if (!vars) return base;
  return base.replace(/\{(\w+)\}/g, (_, v) => (vars[v] !== undefined ? vars[v] : `{${v}}`));
};
(() => {
  const baseUrl = () => window.AkanaCore.baseUrl();
  const authHeaders = (j) => window.AkanaCore.authHeaders(j);
  const authHeadersMultipart = () => window.AkanaCore.authHeadersMultipart();
  const escapeHtml = (s) => window.AkanaCore.escapeHtml(s);

  function createPipeline(bridge) {
      // c2: Track the last "playing" state of TTS playback. The voice:tts:end bus
      // is listened to in voice.js (a different file), so we can't hook the drain/
      // end callback from here; instead pollWakeOnce watches the transition
      // (playing true→false) each poll and, when it happens, clears rawBuffer and
      // sets a cooldown (see below).
      let _wasTtsPlaying = false;
      function formatApiError(body, fallback) {
        const d = body?.detail;
        if (typeof d === "string") return d;
        if (d && typeof d === "object" && !Array.isArray(d) && d.error?.message) return String(d.error.message);
        if (Array.isArray(d)) {
          return d
            .map((x) => (x.msg ? `${x.loc?.join?.(".") || "?"}: ${x.msg}` : JSON.stringify(x)))
            .join("; ");
        }
        if (d && typeof d === "object") return JSON.stringify(d);
        return fallback;
      }
      async function loadWakeConfig() {
        try {
          const r = await fetch(`${baseUrl()}/api/v1/voice/wake/config`, {
            headers: authHeaders(),
          });
          if (!r.ok) return;
          const j = await r.json();
          if (typeof j.threshold === "number") bridge.voice.wakeThreshold = j.threshold;
          // Only poll the server wake endpoint when server-side scoring is actually
          // enabled (custom WAKE_MODEL). Otherwise the browser SpeechRecognition
          // "Hey Akana" phrase-match is the sole trigger and server polling would just
          // 503 every 300ms.
          bridge.voice.wakeServerEnabled = !!j.enabled;
          // "preparing" = the server is fetching the feature models in the background;
          // enabled is false FOR NOW but will flip true on a later poll, so we suppress
          // the "using browser instead" fallback notice (it isn't a real fallback, just
          // a warm-up) while the download is in flight.
          bridge.voice.wakeServerPreparing = j.status === "preparing";
        } catch {
          /* ignore */
        }
      }

      async function abortChatAndCancelTurn() {
        bridge.hooks.abortActiveChatStream?.();
        const convId = window.AkanaChat?.conversationIdForMemory?.();
        if (convId) {
          try {
            await window.AkanaChat?.cancelActiveTurnOnServer?.(convId);
          } catch {
            /* ignore */
          }
        }
      }

      function wakeEntersConversation() {
        // Default ON ("1"): on wake trigger, enter hands-free conversation mode.
        // Set localStorage "akana.wakeEntersConversation" to "0" for the old single-shot behavior.
        try {
          return localStorage.getItem("akana.wakeEntersConversation") !== "0";
        } catch {
          return true;
        }
      }

      async function onWakeTriggered(source) {
        if (bridge.voice.postInFlight || bridge.voice.utterFinishing) return;
        if (Date.now() < bridge.voice.wakeCooldownUntil) return;
        if (bridge.session.isCaptureWake()) return;
        // c3: If we're already in conversation/Live mode, block the fallthrough
        // single-shot branch. When a wake POST resolves asynchronously and re-enters
        // this (in Live mode conversationMode+liveActive are set before the FSM
        // capture phase opens), a second scorer result would land here and open a
        // parallel SR capture alongside the Live session.
        if (bridge.voice.conversationMode || bridge.voice.liveActive) return;
        // "Hey Akana" → enter hands-free conversation mode (can be disabled in settings).
        // If already in conversation mode, do not re-enter; skip single-shot capture.
        if (wakeEntersConversation() && !bridge.voice.conversationMode) {
          // c3: on accepted trigger, set cooldown → drop the second in-flight scorer result.
          bridge.voice.wakeCooldownUntil = Date.now() + 2000;
          await bridge.enterConversationMode?.(`wake:${source}`);
          return;
        }
        if (bridge.hooks.getChatInFlight() || bridge.ttsPlayer.playing) {
          try {
            bridge.ttsPlayer.reset();
          } catch {
            /* ignore */
          }
          await abortChatAndCancelTurn();
        }
        bridge.voice.rawBuffer = new Float32Array(0);
        bridge.voice.cancelled = false;
        bridge.voice.liveTranscriptUserEdit = false;
        bridge.voice.utterChunks = [];
        bridge.voice.hadSpeech = false;
        bridge.voice.silenceMs = 0;
        bridge.voice.ambientRms = 0;
        bridge.voice.ambientSamplesCollected = 0;
        bridge.voice.utterStartTs = Date.now();
        // c3: on accepted trigger, set cooldown → drop the second in-flight scorer result.
        bridge.voice.wakeCooldownUntil = Date.now() + 2000;
        bridge.session.transition(bridge.VPhase.CAPTURE_WAKE, `wake:${source}`, { force: true });
        bridge.hooks.appendRow(
          `<div class="meta">${_pipeT("voice.meta_voice")}</div><div class="bubble-bot">${_pipeT("voice.wake_triggered_msg", { source: escapeHtml(source) })}</div>`,
        );
        bridge.startBrowserLiveTranscript();
      }
      const _STT_HALLUCINATION_RES = [
        /^(thanks?( you)?( for watching)?)\.?$/i,
        /^(thank you[.! ]*)$/i,
        /^(altyaz[ıi].*çeviren.*)$/i,
        /^([\s.,!?…\-]+)$/,
        /^(uh+|um+|ah+|eh+|hm+)\.?$/i,
      ];
      function looksLikeSttHallucination(text) {
        const t = (text || "").trim();
        // Drop empty/single-character transcripts (Whisper silence noise is usually a lone
        // letter/punctuation) — EXCEPT a single DIGIT, which is a legitimate answer to a
        // numbered menu ("1, 2, or 3?") and is very unlikely to be a silence hallucination.
        if (t.length < 2 && !/^\d$/.test(t)) return true;
        return _STT_HALLUCINATION_RES.some((re) => re.test(t));
      }

      async function postVoiceBlob(blob) {
        if (bridge.voice.postInFlight) return;
        if (bridge.voice.cancelled) {
          bridge.voice.cancelled = false;
          return;
        }
        const epoch = bridge.session.getEpoch();
        await abortChatAndCancelTurn();
        bridge.voice.postInFlight = true;
        bridge.session.transition(bridge.VPhase.PROCESSING, "postVoice", { force: true });
        const fetchAbort = new AbortController();
        bridge.voice.voiceFetchAbort = fetchAbort;
        const fd = new FormData();
        fd.append("audio", blob, "konusma.wav");
        // Pass the configured STT language to Whisper (auto = let server detect).
        const sl = (bridge.speechLang() || "").trim().toLowerCase();
        if (sl && sl !== "auto") {
          // Whisper wants ISO-639-1 (tr, en) — strip the region suffix.
          const code = sl.split("-")[0];
          if (code === "tr" || code === "en") fd.append("lang", code);
        }
        // VOICE-OUT for the single-shot wake path: the server /voice route synthesizes the reply
        // to audio_wav_base64 when `tts` is set, but the client used to never request it → a spoken
        // "Hey Akana …" got a SILENT text-only answer. Request+play TTS following the user's
        // stream-TTS preference (conversation mode forces it separately via streamTtsParam).
        const wantTts = !!bridge.getTtsEnabled?.();
        if (wantTts) {
          fd.append("tts", "1");
          fd.append("tts_lang", sl.startsWith("en") ? "en" : "tr");
        }
        const convId = window.AkanaChat?.conversationIdForMemory?.();
        if (convId) fd.append("conversation_id", convId);
        // Forward any pending visual/PDF attachments from the composer to the voice turn
        // (Gemini/OpenAI see them natively). consumePendingFileIds clears the chips.
        try {
          // b32: wait for any in-flight uploads first so the attachment isn't dropped from this
          // turn (and leaked into the next) — mirrors the typed/conversation-mode gate.
          if (window.AkanaChat?.attachmentsUploading?.()) {
            await Promise.race([
              window.AkanaChat.whenAttachmentsReady(),
              new Promise((r) => setTimeout(r, 10000)),
            ]);
          }
          const fileIds = window.AkanaChat?.consumePendingFileIds?.() || [];
          if (Array.isArray(fileIds) && fileIds.length) {
            fd.append("file_ids", fileIds.join(","));
          }
        } catch (_e) {
          /* accessor missing — silent no-op */
        }
        try {
          const r = await fetch(`${baseUrl()}/api/v1/voice`, {
            method: "POST",
            headers: authHeadersMultipart(),
            body: fd,
            signal: fetchAbort.signal,
          });
          const body = await r.json().catch(() => ({}));
          if (!bridge.voiceEpochMatches(epoch)) return;
          if (!r.ok) {
            const errMsg = formatApiError(body, r.statusText);
            // STT_EMPTY (silence) is silently dropped.
            if (r.status === 400 && /STT_EMPTY|no speech/i.test(errMsg)) {
              bridge.hooks.setOrb("idle");
              return;
            }
            bridge.hooks.appendRow(
              `<div class="meta">${_pipeT("voice.meta_voice")} ${r.status}</div><div class="bubble-bot">${escapeHtml(errMsg)}</div>`,
            );
            bridge.hooks.setOrb("err");
            return;
          }
          const transcript = (body.transcript || "").trim();
          if (looksLikeSttHallucination(transcript)) {
            // Hallucination — drop silently; do not show in the UI.
            bridge.hooks.setOrb("idle");
            return;
          }
          bridge.hooks.appendRow(
            `<div class="meta">${_pipeT("voice.meta_you_voice")}</div><div class="bubble-user">${escapeHtml(transcript)}</div>`,
          );
          bridge.hooks.chatRecordMessage({ kind: "user", text: `[voice] ${transcript}` });
          const assistantRow = bridge.hooks.appendRow(
            `<div class="meta">${_pipeT("voice.meta_akana_latency", { ms: body.latency_ms })}</div><div class="bubble-bot bubble-assistant"></div>`,
          );
          window.AkanaMarkdown?.applyMarkdownToRow?.(assistantRow, ".bubble-bot", body.text || "");
          // Speak the reply if TTS was requested and the server returned synthesized audio
          // (single-shot wake voice-out; enqueue with no gen → accepted per the legacy path).
          if (body.audio_wav_base64) {
            try {
              void bridge.ttsPlayer.enqueue(body.audio_wav_base64, body.audio_mime || "audio/wav");
            } catch { /* playback failure must not break the turn */ }
          } else if (body.tts_error) {
            // VB-2: the server persisted the turn but TTS synthesis failed (edge/Piper down),
            // so it degraded to text-only with a tts_error hint instead of a 500. The reply text
            // already rendered above; surface a localized meta row (mirrors the streaming path's
            // tts_error frame) + a one-tap Speak retry that re-synthesizes via POST /voice/tts —
            // otherwise read-aloud fails silently with zero user feedback.
            const retryText = body.text || "";
            const retryLang = sl.startsWith("en") ? "en" : "tr";
            const failRow = bridge.hooks.appendRow(
              `<div class="meta">${_pipeT("voice.meta_voice")}</div>` +
                `<div class="bubble-bot">${escapeHtml(_pipeT("voice.tts_failed_meta"))} ` +
                (retryText
                  ? `<button type="button" class="btn-ghost btn-sm" data-voice-tts-retry="1">${escapeHtml(_pipeT("voice.tts_retry_btn"))}</button>`
                  : "") +
                `</div>`,
            );
            const retryBtn = failRow?.querySelector?.("[data-voice-tts-retry]");
            if (retryBtn && retryText) {
              retryBtn.addEventListener("click", async () => {
                retryBtn.disabled = true;
                try {
                  const rr = await fetch(`${baseUrl()}/api/v1/voice/tts`, {
                    method: "POST",
                    headers: authHeaders(true),
                    body: JSON.stringify({ text: retryText, lang: retryLang }),
                  });
                  if (!rr.ok) {
                    retryBtn.disabled = false;
                    return;
                  }
                  const bufAudio = await rr.arrayBuffer();
                  // /voice/tts returns raw audio bytes; ttsPlayer.enqueue wants base64 (same as
                  // the one-shot audio_wav_base64 path), so re-encode before handing it off.
                  let bin = "";
                  const bytes = new Uint8Array(bufAudio);
                  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
                  const b64 = btoa(bin);
                  const mime = rr.headers.get("content-type") || "audio/wav";
                  void bridge.ttsPlayer.enqueue(b64, mime);
                  retryBtn.remove();
                } catch {
                  retryBtn.disabled = false;
                }
              });
            }
          }
          if (body.conversation_id) bridge.hooks.setConversationId(body.conversation_id);
          const convId = body.conversation_id || window.AkanaChat?.conversationIdForMemory?.();
          if (convId) void window.AkanaChat?.syncConversationLogFromServer?.(convId);
          bridge.msg.value = "";
          bridge.hooks.setOrb("ok");
        } catch (err) {
          if (err && err.name === "AbortError") {
            bridge.hooks.setOrb("idle");
            return;
          }
          bridge.hooks.appendRow(`<div class="meta">${_pipeT("voice.meta_voice")}</div><div class="bubble-bot">${escapeHtml(String(err))}</div>`);
          bridge.hooks.setOrb("err");
        } finally {
          if (bridge.voice.voiceFetchAbort === fetchAbort) bridge.voice.voiceFetchAbort = null;
          bridge.voice.postInFlight = false;
          // Only force the FSM back to WAKE_ARMED/IDLE if nothing newer took over while this
          // POST was in flight (e.g. the user entered hands-free conversation mode, which
          // bumps the epoch and opens its own CAPTURE_WAKE) — otherwise this stale finally
          // would clobber that fresh capture and leave the scene open but deaf.
          if (bridge.voiceEpochMatches(epoch)) {
            if (bridge.session.isWakeArmed()) {
              bridge.session.transition(bridge.VPhase.WAKE_ARMED, "postVoice:done", { force: true });
            } else {
              bridge.session.transition(bridge.VPhase.IDLE, "postVoice:done", { force: true });
            }
          }
          bridge.syncVoiceUi();
        }
      }

      /** Conversation mode: transcribe audio ONLY (no LLM/TTS) and pass the text
       *  to the chat channel. Does NOT await the full turn — re-arm is event-driven (maybeReArm). */
      async function postConversationBlob(blob, epoch) {
        const fd = new FormData();
        fd.append("audio", blob, "konusma.wav");
        const sl = (bridge.speechLang() || "").trim().toLowerCase();
        if (sl && sl !== "auto") {
          const code = sl.split("-")[0];
          if (code === "tr" || code === "en") fd.append("lang", code);
        }
        const fetchAbort = new AbortController();
        bridge.voice.voiceFetchAbort = fetchAbort;
        let body = {};
        try {
          const r = await fetch(`${baseUrl()}/api/v1/voice/transcribe`, {
            method: "POST",
            headers: authHeadersMultipart(),
            body: fd,
            signal: fetchAbort.signal,
          });
          body = await r.json().catch(() => ({}));
          if (!bridge.voiceEpochMatches(epoch)) return;
          if (!r.ok) {
            const errMsg = formatApiError(body, r.statusText);
            // Silence (STT_EMPTY) is silently dropped; other errors show a bubble.
            if (!/STT_EMPTY|no speech/i.test(errMsg)) {
              bridge.hooks.appendRow(
                `<div class="meta">${_pipeT("voice.meta_voice")} ${r.status}</div><div class="bubble-bot">${escapeHtml(errMsg)}</div>`,
              );
            }
            return;
          }
        } catch (err) {
          if (err && err.name === "AbortError") return;
          bridge.hooks.appendRow(
            `<div class="meta">${_pipeT("voice.meta_voice")}</div><div class="bubble-bot">${escapeHtml(String(err))}</div>`,
          );
          return;
        } finally {
          if (bridge.voice.voiceFetchAbort === fetchAbort) bridge.voice.voiceFetchAbort = null;
        }
        const transcript = (body.transcript || "").trim();
        // Empty/hallucination → do not start a turn; finalize's finally will re-arm listening.
        if (!transcript || looksLikeSttHallucination(transcript)) return;
        // Voice exit: mirror the browser-SR path (finalizeConversationFromSR) so a standalone
        // exit phrase ("dur"/"stop"/"goodbye"/"exit") closes conversation mode in Whisper mode
        // too instead of being sent to the LLM as a normal turn. Uses the SAME helper on the
        // bridge (single source of truth). Do NOT submit — just exit.
        if (bridge.isConversationExitPhrase?.(transcript)) {
          try { bridge.exitConversationMode?.("voice-exit"); } catch { /* ignore */ }
          return;
        }
        bridge.emitBus?.("voice:utterance:end");
        // Half-duplex flags — mirror finalizeConversationFromSR (the browser-SR path): the
        // turn is now flowing, so do NOT let maybeReArmConversation reopen the mic until the
        // reply + TTS are done. Without these the whisper path could re-arm mid-reply (scene
        // flips to "Listening" and Akana hears itself). armConvWatchdog rescues a stuck turn
        // if `tts_end` never arrives.
        bridge.voice.convAwaitingReply = true;
        bridge.voice.ttsStreamOpen = true;
        try { bridge.playEarcon?.("done"); } catch { /* ignore */ }
        try { bridge.armConvWatchdog?.(); } catch { /* ignore */ }
        // Barge-in server-cancel in flight → wait for it BEFORE POST (mirror submitConversationTurn
        // on the SR path). Otherwise the new whisper turn is POSTed before the old turn is cancelled
        // server-side and hits the busy-guard. Consuming the promise also clears the stale handle.
        if (bridge.voice._bargeCancelPromise) {
          const p = bridge.voice._bargeCancelPromise;
          bridge.voice._bargeCancelPromise = null;
          try { await p; } catch { /* cancel error must not block submission */ }
          if (!bridge.voice.conversationMode) return; // mode closed while waiting (exit clears flags)
        }
        try {
          window.AkanaChat?.submitVoiceText?.(transcript);
        } catch {
          // Turn never started (transport threw) → tts_end will never arrive → clear the
          // half-duplex flags so the mic reopens instead of freezing on "Thinking".
          bridge.voice.convAwaitingReply = false;
          bridge.voice.ttsStreamOpen = false;
          try { bridge.maybeReArmConversation?.("whisperSubmitFailed"); } catch { /* ignore */ }
        }
      }

      async function finalizeUtterance() {
        if (bridge.voice.utterFinishing || bridge.session.isCaptureMic()) return;
        const conv = !!bridge.voice.conversationMode;
        bridge.stopBrowserLiveTranscript();
        bridge.voice.utterFinishing = true;
        // c1: Read chunks/cancelled BEFORE the PROCESSING transition — the transition
        // (CAPTURE_WAKE→PROCESSING) bumps the epoch and host onTransition sets
        // utterChunks=[]/utterFinishing=false; so snapshot the copy first, then take
        // the epoch snapshot AFTER the transition and re-set utterFinishing (mirrors
        // finalizeConversationFromSR's order) — otherwise the WAV is never POSTed.
        const chunks = bridge.voice.utterChunks.slice();
        const wasCancelled = bridge.voice.cancelled;
        bridge.session.transition(bridge.VPhase.PROCESSING, conv ? "finalizeConv" : "finalizeWake", {
          force: true,
        });
        const epoch = bridge.session.getEpoch();
        bridge.voice.utterFinishing = true;
        if (conv) bridge.emitBus?.("voice:utterance:end");
        bridge.voice.utterChunks = [];
        bridge.voice.hadSpeech = false;
        bridge.voice.silenceMs = 0;
        bridge.voice.ambientRms = 0;
        bridge.voice.ambientSamplesCollected = 0;
        bridge.voice.cancelled = false;
        try {
          if (!bridge.voiceEpochMatches(epoch) || wasCancelled || !chunks.length) {
            bridge.syncOrbWithVoice();
            return;
          }
          const merged = AkanaVoiceCapture.mergeChunks(chunks);
          const at16 = AkanaVoiceCapture.downsampleFloat32(merged, bridge.voice.inSampleRate, 16000);
          if (at16.length < 500) {
            bridge.syncOrbWithVoice();
            return;
          }
          if (!bridge.voiceEpochMatches(epoch)) return;
          const wav = AkanaVoiceCapture.encodeWavPcm16Mono(at16);
          if (conv) {
            await postConversationBlob(wav, epoch);
          } else {
            await postVoiceBlob(wav);
            bridge.voice.wakeCooldownUntil = Date.now() + 2000;
          }
        } finally {
          if (bridge.voiceEpochMatches(epoch)) bridge.voice.utterFinishing = false;
          if (conv) {
            bridge.session.transition(bridge.VPhase.IDLE, "finalizeConv:done", { force: true });
            bridge.maybeReArmConversation?.("finalizeConv");
            bridge.syncVoiceUi();
          } else if (bridge.session.isWakeArmed()) {
            bridge.session.transition(bridge.VPhase.WAKE_ARMED, "finalizeWake:done", { force: true });
            bridge.scheduleSpeechWakeRestart();
            bridge.syncVoiceUi();
          } else {
            bridge.session.transition(bridge.VPhase.IDLE, "finalizeWake:done", { force: true });
            bridge.syncVoiceUi();
          }
        }
      }

      async function pollWakeOnce() {
        // Server-side openWakeWord scoring disabled (no custom WAKE_MODEL) → skip the
        // POST entirely; the browser SpeechRecognition "Hey Akana" phrase-match handles
        // wake. Avoids a 503 on every poll now that no pretrained model ships.
        if (!bridge.voice.wakeServerEnabled) return;
        // c2: AEC is off on the wake mic (echoCancellation:false) → while Akana speaks,
        // rawBuffer fills with its own TTS audio. Clear the buffer while TTS is playing;
        // if playback ended in this poll (playing true→false) discard the buffer and set
        // a 1500 ms cooldown — otherwise the poll immediately after drain would send a
        // window of Akana's own voice and wake would self-trigger.
        const _ttsPlaying = !!bridge.ttsPlayer.playing;
        if (_ttsPlaying) {
          bridge.voice.rawBuffer = new Float32Array(0);
        } else if (_wasTtsPlaying) {
          bridge.voice.rawBuffer = new Float32Array(0);
          bridge.voice.wakeCooldownUntil = Date.now() + 1500;
        }
        _wasTtsPlaying = _ttsPlaying;
        if (
          bridge.voice.postInFlight ||
          bridge.voice.micManual ||
          bridge.voice.utteranceActive ||
          bridge.voice.utterFinishing ||
          bridge.ttsPlayer.playing ||
          bridge.hooks.getChatInFlight?.()
        ) {
          return;
        }
        if (Date.now() < bridge.voice.wakeCooldownUntil) return;
        if (bridge.voice.wakeInFlight) return;
        const need = Math.floor(bridge.voice.inSampleRate * bridge.voice.wakeWindowSec);
        if (bridge.voice.rawBuffer.length < need) return;
        const slice = bridge.voice.rawBuffer.slice(-need);
        const sliceRms = AkanaVoiceCapture.rms(slice);
        if (sliceRms < bridge.voice.wakeMinRms) {
          bridge.updateWakeMeter({ rmsVal: sliceRms });
          return;
        }
        const at16 = AkanaVoiceCapture.downsampleFloat32(slice, bridge.voice.inSampleRate, 16000);
        const blob = AkanaVoiceCapture.encodeWavPcm16Mono(at16);
        bridge.voice.wakeInFlight = true;
        // c4: Add a 5 s timeout to the wake POST. Otherwise a half-open TCP (sleep/
        // wifi handoff) never settles → the wakeInFlight lock is never cleared in
        // finally and every subsequent poll returns early, wake polling silently
        // dies. Fall back to a manual AbortController+setTimeout when
        // AbortSignal.timeout is unavailable.
        let _wakeAbortTimer = null;
        let _wakeSignal;
        if (typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function") {
          _wakeSignal = AbortSignal.timeout(5000);
        } else {
          const _ac = new AbortController();
          _wakeSignal = _ac.signal;
          _wakeAbortTimer = setTimeout(() => {
            try { _ac.abort(); } catch { /* ignore */ }
          }, 5000);
        }
        try {
          const fd = new FormData();
          fd.append("audio", blob, "wake.wav");
          const r = await fetch(`${baseUrl()}/api/v1/voice/wake`, {
            method: "POST",
            headers: authHeadersMultipart(),
            body: fd,
            signal: _wakeSignal,
          });
          const body = await r.json().catch(() => ({}));
          if (!r.ok) {
            if (!bridge.voice.wakeErrShown) {
              bridge.voice.wakeErrShown = true;
              const msg = formatApiError(body, r.status);
              bridge.hooks.appendRow(
                `<div class="meta">${_pipeT("voice.meta_wake")}</div><div class="bubble-bot">${escapeHtml(
                  msg +
                    (r.status === 503
                      ? _pipeT("voice.wake_err_hint_503")
                      : r.status === 401
                        ? _pipeT("voice.wake_err_hint_401")
                        : ""),
                )}</div>`,
              );
              bridge.hooks.setOrb("err");
            }
            // 503 = server-side scoring is unavailable (feature models missing, model
            // won't load). The wake sources are mutually exclusive, so leaving the poll
            // running would keep 503ing forever with the browser fallback never armed.
            // Degrade instead of dying: mark server scoring off, stop the poll, and hand
            // wake to the browser SpeechRecognition phrase-match. scheduleSpeechWakeRestart
            // gates on !wakeServerEnabled → it now starts the browser fallback.
            if (r.status === 503 && bridge.voice.wakeServerEnabled) {
              bridge.voice.wakeServerEnabled = false;
              clearInterval(bridge.voice.wakeInterval);
              bridge.voice.wakeInterval = null;
              bridge.scheduleSpeechWakeRestart?.();
            }
            return;
          }
          const thr = typeof body.threshold === "number" ? body.threshold : bridge.voice.wakeThreshold;
          if (typeof body.threshold === "number") bridge.voice.wakeThreshold = body.threshold;
          bridge.updateWakeMeter({
            rmsVal: sliceRms,
            score: body.max_score,
            threshold: thr,
          });
          if (bridge.wakeDebugEnabled() && typeof body.max_score === "number") {
            if (body.max_score > 0.03 || body.triggered) {
              console.debug(
                `[wake] score=${body.max_score.toFixed(3)} threshold=${thr} triggered=${body.triggered}`,
              );
            }
          }
          if (body.triggered) void onWakeTriggered(_pipeT("voice.wake_src_server"));
        } catch {
          /* ignore transient */
        } finally {
          // c4: clear the manual timeout timer (if any) and always release the lock.
          if (_wakeAbortTimer != null) {
            try { clearTimeout(_wakeAbortTimer); } catch { /* ignore */ }
          }
          bridge.voice.wakeInFlight = false;
        }
      }
      async function saveVoicePreferences(patch) {
        const r = await fetch(`${baseUrl()}/api/v1/voice/preferences`, {
          method: "PATCH",
          headers: authHeaders(true),
          body: JSON.stringify(patch),
        });
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          throw new Error(formatApiError(err, r.status));
        }
        const j = await r.json();
        bridge.applyVoicePreferencesFromServer(j);
        return j;
      }

      async function loadVoicePreferences() {
        try {
          const r = await fetch(`${baseUrl()}/api/v1/voice/preferences`, { headers: authHeaders() });
          if (!r.ok) return;
          const j = await r.json();
          bridge.applyVoicePreferencesFromServer(j);
        } catch {
          /* ignore — localStorage fallback */
          try {
            const on = localStorage.getItem("akana.streamTts") === "1";
            bridge.setTtsEnabled(on);
            if (bridge.ttsToggle) bridge.ttsToggle.checked = on;
          } catch {
            /* localStorage unavailable — silent fallback */
          }
        }
      }

    return {
      formatApiError,
      looksLikeSttHallucination,
      postVoiceBlob,
      finalizeUtterance,
      pollWakeOnce,
      loadWakeConfig,
      onWakeTriggered,
      saveVoicePreferences,
      loadVoicePreferences,
    };
  }

  window.AkanaVoicePipeline = { create: createPipeline, createPipeline };
})();
