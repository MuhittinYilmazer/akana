/**
 * Akana voice settings UX — capability chips, TTS picker, wake autostart, mic device.
 */
(() => {
  const LS_WAKE_AUTOSTART = "akana.wakeAutostart";
  const LS_TTS = "akana.streamTts";
  const LS_MIC_DEVICE = "akana.micDevice";
  const LS_TTS_LANG = "akana.ttsLang";
  const LS_SETUP_BANNER = "akana.setupBannerDismissed";
  // ─── Conversation voice mode — akana-voice.js reads these keys at runtime;
  // here we only write the same keys. ─────────────────────────────────────────
  const LS_WAKE_SOURCE = "akana.wakeSource"; // "model" (default) | "browser"
  const LS_STT_SOURCE = "akana.sttSource"; // "browser" (default) | "whisper" (conversation STT engine)
  const LS_BARGE_IN = "akana.bargeIn"; // "1" | "0" (DEFAULT OFF — opt-in; self-barge on speakers)
  const LS_BARGE_RMS = "akana.bargeRms"; // 0.015..0.10 (default 0.05); akana-voice.js _threshold() reads
  const LS_CONV_SILENCE_MS = "akana.convSilenceMs"; // 1000..4000 (default 1700)
  const LS_VOICE_EARCONS = "akana.voiceEarcons"; // "1" | "0" (default off)
  const LS_EARCON_VOL = "akana.voiceEarconVol"; // 0..1 (default 0.6); akana-voice.js earconVolume() reads
  const LS_WAKE_ENTERS_CONV = "akana.wakeEntersConversation"; // "1" (default) | "0"

  // Floor raised 600→1000: a window below ~1000 ms sits inside Chrome's post-final SR quiet
  // window and reliably truncates speech mid-utterance (premature end-of-utterance). The
  // runtime guard in akana-voice.js mirrors this floor.
  const CONV_SILENCE_MIN = 1000;
  const CONV_SILENCE_MAX = 4000;
  const CONV_SILENCE_DEFAULT = 1700;
  const BARGE_RMS_MIN = 0.015;
  const BARGE_RMS_MAX = 0.10;
  const BARGE_RMS_DEFAULT = 0.05; // same as akana-voice.js bargeDetector.rms
  // Earcon volume slider range (0..1); default matches akana-voice.js EARCON_VOL_DEFAULT.
  const EARCON_VOL_MIN = 0;
  const EARCON_VOL_MAX = 1;
  const EARCON_VOL_DEFAULT = 0.6;

  const baseUrl = () => window.AkanaCore.baseUrl();
  const authHeaders = (j) => window.AkanaCore.authHeaders(j);

  function createSettings(bridge) {
      const voiceCapHint = document.getElementById("voice-capability-hint");
      if (voiceCapHint) {
        const parts = [];
        parts.push(
          typeof window.SpeechRecognition !== "undefined" ||
            typeof window.webkitSpeechRecognition !== "undefined"
            ? window.AkanaI18n.t("voicecfg.cap.transcript_yes")
            : window.AkanaI18n.t("voicecfg.cap.transcript_no"),
        );
        parts.push(
          navigator.mediaDevices && navigator.mediaDevices.getUserMedia
            ? window.AkanaI18n.t("voicecfg.cap.mic_yes")
            : window.AkanaI18n.t("voicecfg.cap.mic_no"),
        );
        voiceCapHint.textContent = parts.join(" ");
      }
      // ─── Voice capability snapshot + TTS voice picker + wake threshold ──────
      const voiceStatusStrip = document.getElementById("voice-status-strip");
      const ttsVoiceSelect = document.getElementById("tts-voice");
      const ttsTestBtn = document.getElementById("btn-tts-test");
      const ttsTestStatus = document.getElementById("tts-test-status");
      const wakeStatusHint = document.getElementById("wake-status-hint");
      const wakeSourceSelect = document.getElementById("wake-source");
      const sttSourceSelect = document.getElementById("stt-source");
      const sttSourceHint = document.getElementById("stt-source-hint");
      const wakeThresholdInput = document.getElementById("wake-threshold");
      const wakeThresholdOut = document.getElementById("wake-threshold-out");
      const wakeMinFramesInput = document.getElementById("wake-min-frames");
      const wakeMinFramesOut = document.getElementById("wake-min-frames-out");
      const wakeAutostartCb = document.getElementById("settings-wake-autostart");
      const btnWakeTest = document.getElementById("btn-wake-test");

      // ─── Mic device picker (enumerateDevices populates after permission) ────
      const micDeviceSelect = document.getElementById("mic-device");

      async function refreshMicDeviceList() {
        if (!micDeviceSelect) return;
        if (!navigator.mediaDevices || !navigator.mediaDevices.enumerateDevices) return;
        let devices;
        try {
          devices = await navigator.mediaDevices.enumerateDevices();
        } catch {
          return;
        }
        const mics = devices.filter((d) => d.kind === "audioinput");
        const cur = localStorage.getItem(LS_MIC_DEVICE) || "";
        const prevValue = micDeviceSelect.value;
        micDeviceSelect.innerHTML = "";
        const def = document.createElement("option");
        def.value = "";
        def.textContent = window.AkanaI18n.t("voicecfg.mic.default");
        micDeviceSelect.appendChild(def);
        let i = 0;
        for (const d of mics) {
          const o = document.createElement("option");
          o.value = d.deviceId;
          // Labels are empty until getUserMedia() has been granted at least once.
          o.textContent = d.label || window.AkanaI18n.t("voicecfg.mic.n", { n: ++i });
          micDeviceSelect.appendChild(o);
        }
        const target = cur || prevValue || "";
        if ([...micDeviceSelect.options].some((o) => o.value === target)) {
          micDeviceSelect.value = target;
        }
      }

      if (micDeviceSelect) {
        micDeviceSelect.addEventListener("change", async () => {
          const v = micDeviceSelect.value;
          if (v) localStorage.setItem(LS_MIC_DEVICE, v);
          else localStorage.removeItem(LS_MIC_DEVICE);
          // Tear down so next ensureAudio() picks up the new device.
          try { bridge.stopAudioGraph(); } catch { /* ignore */ }
          // stopAudioGraph() ALSO clears the model-wake poll + stopSpeechWakeFallback() while
          // keepWakeArmed leaves the FSM in WAKE_ARMED (wake button lit, no audio graph) — and
          // nothing re-arms on an idle page (resumeWakeListeningIfIdle only fires on voice/chat
          // activity). Cycle wake like the wake-source picker below so "Hey Akana" keeps working
          // after a device switch (await the async release first, per the barge-in lifecycle).
          if (bridge.voice && bridge.voice.wakeEnabled) {
            try {
              await bridge.setWakeListening(false, { silent: true });
              await bridge.setWakeListening(true, { silent: true });
            } catch {
              /* best-effort live re-arm */
            }
          }
        });
        void refreshMicDeviceList();
        if (navigator.mediaDevices && typeof navigator.mediaDevices.addEventListener === "function") {
          navigator.mediaDevices.addEventListener("devicechange", () => void refreshMicDeviceList());
        }
      }

      // ─── TTS queue chip (composer) ─────────────────────────────────────────
      const ttsQueueChip = document.getElementById("tts-queue-chip");
      const ttsQueueCountEl = document.getElementById("tts-queue-count");
      function updateTtsQueueChip() {
        if (!ttsQueueChip) return;
        const n = (bridge.ttsPlayer && Array.isArray(bridge.ttsPlayer.queue)) ? bridge.ttsPlayer.queue.length : 0;
        const playing = !!(bridge.ttsPlayer && bridge.ttsPlayer.playing);
        if (!n && !playing) {
          ttsQueueChip.hidden = true;
          return;
        }
        ttsQueueChip.hidden = false;
        if (ttsQueueCountEl) ttsQueueCountEl.textContent = String(n + (playing ? 1 : 0));
      }
      // Tick chip every 250ms — cheap, predictable.
      setInterval(updateTtsQueueChip, 250);

      function readWakeAutostartLocal() {
        try {
          const v = localStorage.getItem(LS_WAKE_AUTOSTART);
          if (v === "0") return false;
          if (v === "1") return true;
        } catch {
          /* ignore */
        }
        return true;
      }

      function writeWakeAutostartLocal(on) {
        try {
          localStorage.setItem(LS_WAKE_AUTOSTART, on ? "1" : "0");
        } catch {
          /* ignore */
        }
      }

      let wakeAutostartEnabled = readWakeAutostartLocal();
      let wakeAutostartAttempted = false;
      let wakeAutostartPending = false;
      let wakeAutostartGestureArmed = false;

      function syncWakeAutostartUi() {
        if (wakeAutostartCb) wakeAutostartCb.checked = wakeAutostartEnabled;
      }

      function isWakeAvailable() {
        return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
      }

      function applyVoicePreferencesFromServer(prefs) {
        if (!prefs || typeof prefs !== "object") return;
        if ("wake_autostart" in prefs) {
          wakeAutostartEnabled = !!prefs.wake_autostart;
          writeWakeAutostartLocal(wakeAutostartEnabled);
        }
        if ("stream_tts" in prefs) {
          bridge.setTtsEnabled(!!prefs.stream_tts);
          try {
            localStorage.setItem(LS_TTS, bridge.getTtsEnabled() ? "1" : "0");
          } catch {
            /* ignore */
          }
          if (bridge.ttsToggle) bridge.ttsToggle.checked = bridge.getTtsEnabled();
        }
        syncWakeAutostartUi();
      }


      function armWakeAutostartOnGesture() {
        if (!bridge.hooks.isChatPage || !wakeAutostartEnabled || wakeAutostartGestureArmed) return;
        wakeAutostartGestureArmed = true;
        wakeAutostartPending = true;
        bridge.syncWakeButtonUi(false);
        const tryStart = () => {
          document.removeEventListener("pointerdown", tryStart, true);
          document.removeEventListener("keydown", tryStart, true);
          wakeAutostartGestureArmed = false;
          if (!wakeAutostartEnabled || bridge.voice.wakeEnabled) {
            wakeAutostartPending = false;
            bridge.syncWakeButtonUi(bridge.voice.wakeEnabled);
            return;
          }
          void bridge.setWakeListening(true, { silent: true }).then((ok) => {
            if (!ok && wakeAutostartEnabled) {
              wakeAutostartPending = true;
              bridge.syncWakeButtonUi(false);
              bridge.hooks.showToast(window.AkanaI18n.t("voicecfg.wake.toast"), "warn");
            }
          });
        };
        document.addEventListener("pointerdown", tryStart, true);
        document.addEventListener("keydown", tryStart, true);
      }

      async function maybeAutostartWake() {
        if (!bridge.hooks.isChatPage) return;
        if (wakeAutostartAttempted) return;
        wakeAutostartAttempted = true;
        if (!wakeAutostartEnabled || !isWakeAvailable()) return;
        const ok = await bridge.setWakeListening(true, { silent: true });
        if (!ok) armWakeAutostartOnGesture();
      }

      syncWakeAutostartUi();

      function ttsPreferredLang() {
        const cached = localStorage.getItem(LS_TTS_LANG);
        if (cached === "tr" || cached === "en") return cached;
        // Resolve "auto" via the shared helper — startsWith("en") ? "en" : "tr" would map
        // "auto" (Whisper auto-detect) to Turkish, giving an English user a Turkish voice.
        if (bridge.ttsLangFromSpeech) return bridge.ttsLangFromSpeech();
        return bridge.speechLang().startsWith("en") ? "en" : "tr";
      }

      // Option value is in "engine|voice|lang" format; resolve the language part.
      // Also handles legacy "tr"/"en" values and empty selection safely.
      function selectedTtsLang() {
        const raw = (ttsVoiceSelect && ttsVoiceSelect.value) || "";
        if (raw === "tr" || raw === "en") return raw;
        const parts = raw.split("|");
        const lang = parts.length === 3 ? parts[2] : "";
        if (lang === "tr" || lang === "en") return lang;
        return ttsPreferredLang();
      }

      const voiceCapChips = document.getElementById("voice-capability-chips");
      const setupBanner = document.getElementById("setup-banner");
      const setupBannerText = document.getElementById("setup-banner-text");
      const setupBannerDismiss = document.getElementById("setup-banner-dismiss");

      // BUG 4: the "Kapat"/Close button was rendered but never wired — clicking it
      // did nothing. Bind it once: hide the banner now AND persist the dismissal so
      // it stays hidden across reloads (maybeShowSetupBanner already honors the key,
      // but nothing ever set it). Bound here (init runs once) — no double-binding.
      if (setupBannerDismiss && setupBanner) {
        setupBannerDismiss.addEventListener("click", () => {
          setupBanner.hidden = true;
          try {
            localStorage.setItem(LS_SETUP_BANNER, "1");
          } catch {
            /* storage blocked (private mode) — session-level hide still applies */
          }
        });
      }

      function renderCapChip(label, ok, warn) {
        const span = document.createElement("span");
        span.className = `cap-chip${ok ? " is-ok" : warn ? " is-warn" : ""}`;
        const dot = document.createElement("span");
        dot.className = "cap-chip-dot";
        span.appendChild(dot);
        span.appendChild(document.createTextNode(label));
        return span;
      }

      function maybeShowSetupBanner(wake, tts) {
        if (!setupBanner || !setupBannerText) return;
        try {
          if (localStorage.getItem(LS_SETUP_BANNER) === "1") {
            setupBanner.hidden = true;
            return;
          }
        } catch {
          /* ignore */
        }
        const needs = [];
        if (!wake?.installed) {
          needs.push(window.AkanaI18n.t("voicecfg.setup.need_wake"));
        }
        if (!tts?.ready) {
          needs.push(window.AkanaI18n.t("voicecfg.setup.need_tts"));
        }
        if (!needs.length) {
          setupBanner.hidden = true;
          return;
        }
        setupBannerText.textContent = window.AkanaI18n.t("voicecfg.setup.banner", { needs: needs.join(" · ") });
        setupBanner.hidden = false;
      }

      // Live voice (Phase 2): stash the full /voice/config (both `live`=gemini and
      // `realtime`=openai blocks) into a global + show the "Live (realtime)" toggle ONLY
      // when any provider is reachable + persist the preference in localStorage.
      // enterConversationMode reads this global + localStorage to branch turn-based ↔ Live
      // (akana-voice.js); pickVoiceMode selects the active provider.
      function applyLiveCapability(cfg) {
        try {
          window.AkanaVoiceLiveCfg = cfg || null;
        } catch {
          /* ignore */
        }
        const block = document.getElementById("voice-live-block");
        const toggle = document.getElementById("voice-live-toggle");
        const L = window.AkanaVoiceLive;
        const visible = !!(
          L && typeof L.liveToggleVisible === "function" && L.liveToggleVisible(cfg)
        );
        if (block) block.hidden = !visible;
        if (toggle) {
          let on = false;
          try {
            on = localStorage.getItem("akana.voice.liveMode") === "1";
          } catch {
            on = false;
          }
          toggle.checked = on;
          if (!toggle.dataset.bound) {
            toggle.dataset.bound = "1";
            toggle.addEventListener("change", () => {
              try {
                localStorage.setItem("akana.voice.liveMode", toggle.checked ? "1" : "0");
              } catch {
                /* ignore */
              }
            });
          }
        }
      }

      async function loadVoiceConfig() {
        try {
          const r = await fetch(`${baseUrl()}/api/v1/voice/config`, { headers: authHeaders() });
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          const j = await r.json();
          const tts = j.tts || {};
          const wake = j.wake || {};
          const stt = j.stt || {};
          applyLiveCapability(j);  // full config (live + realtime) → pickVoiceMode selects
          // Gate the Whisper STT-source option on server availability (faster-whisper installed).
          applyWhisperSttInstalled(stt.installed);
          if (voiceCapChips) {
            voiceCapChips.innerHTML = "";
            voiceCapChips.appendChild(
              renderCapChip(
                `TTS ${tts.ready ? window.AkanaI18n.t("voicecfg.status.tts_ready") : window.AkanaI18n.t("voicecfg.chip.tts_missing")}`,
                !!tts.ready, !!tts.installed && !tts.ready,
              ),
            );
            voiceCapChips.appendChild(
              renderCapChip(
                `Wake ${wake.installed ? window.AkanaI18n.t("voicecfg.chip.wake_server") : window.AkanaI18n.t("voicecfg.chip.wake_browser")}`,
                !!wake.installed, !wake.installed,
              ),
            );
            voiceCapChips.appendChild(
              renderCapChip(
                `STT ${stt.installed ? window.AkanaI18n.t("voicecfg.chip.stt_server") : window.AkanaI18n.t("voicecfg.chip.stt_browser")}`,
                !!stt.installed, false,
              ),
            );
          }
          maybeShowSetupBanner(wake, tts);
          if (voiceStatusStrip) {
            const bits = [
              window.AkanaI18n.t("voicecfg.status.output", { tts: tts.ready ? window.AkanaI18n.t("voicecfg.status.tts_ready") : tts.installed ? window.AkanaI18n.t("voicecfg.status.tts_no_model") : window.AkanaI18n.t("voicecfg.status.tts_off") }),
              window.AkanaI18n.t("voicecfg.status.wake", { wake: wake.installed ? window.AkanaI18n.t("voicecfg.status.wake_server") : window.AkanaI18n.t("voicecfg.status.wake_browser") }),
              window.AkanaI18n.t("voicecfg.status.stt", { stt: stt.installed ? window.AkanaI18n.t("voicecfg.status.stt_server") : window.AkanaI18n.t("voicecfg.status.stt_browser") }),
            ];
            voiceStatusStrip.textContent = bits.join(" · ");
          }
          if (ttsVoiceSelect) {
            // List both edge (neural) and Piper voices. Each option carries
            // engine|voice|lang; on 'change' it is saved to the SERVER so the
            // selected voice is PERSISTENT (previously only language was written
            // to localStorage; engine/voice name was never saved → revert to old voice).
            const engineVoices = Array.isArray(tts.engine_voices) ? tts.engine_voices : [];
            const fallbackVoices = Array.isArray(tts.voices) ? tts.voices : [];
            const list = engineVoices.length
              ? engineVoices
              : fallbackVoices.map((v) => ({ ...v, engine: "piper" }));
            const selEngine = (tts.selected_engine || "auto").toLowerCase();
            const selTr = tts.selected_voice_tr || "";
            const selEn = tts.selected_voice_en || "";
            ttsVoiceSelect.innerHTML = "";
            let matched = "";
            for (const v of list) {
              const engine = (v.engine || "piper").toLowerCase();
              const lang = (v.lang || "?").toLowerCase();
              const voiceId = v.id || v.path || v.name || "";
              const o = document.createElement("option");
              // engine|voiceId|lang — non-edge voices carry a path, edge voices carry a name.
              o.value = `${engine}|${voiceId}|${lang}`;
              const tag = v.exists === false ? window.AkanaI18n.t("voicecfg.tts.not_exists") : "";
              o.textContent = `${engine.toUpperCase()} · ${lang.toUpperCase()} · ${v.name || voiceId}${tag}`;
              o.disabled = v.exists === false;
              ttsVoiceSelect.appendChild(o);
              // Select the entry matching the persistent preference: edge = language-based name match.
              if (engine === selEngine) {
                if (engine === "edge") {
                  if ((lang === "tr" && voiceId === selTr) || (lang === "en" && voiceId === selEn)) {
                    matched = o.value;
                  }
                } else if (!matched && v.configured) {
                  matched = o.value;
                }
              }
            }
            if (!list.length) {
              const o = document.createElement("option");
              o.textContent = window.AkanaI18n.t("voicecfg.tts.no_model");
              o.disabled = true;
              ttsVoiceSelect.appendChild(o);
            } else if (matched) {
              ttsVoiceSelect.value = matched;
            } else {
              // No preference match: select the first available voice for the current language.
              const preferLang = ttsPreferredLang();
              const firstForLang = [...ttsVoiceSelect.options].find(
                (o) => !o.disabled && o.value.endsWith(`|${preferLang}`),
              );
              if (firstForLang) ttsVoiceSelect.value = firstForLang.value;
            }
          }
          if (wakeStatusHint) {
            wakeStatusHint.textContent = wake.installed
              ? window.AkanaI18n.t("voicecfg.wake.hint_server", { model: wake.model })
              : window.AkanaI18n.t("voicecfg.wake.hint_browser");
          }
          if (btnWakeTest) btnWakeTest.hidden = !wake.installed;
          if (wakeThresholdInput && typeof wake.threshold === "number") {
            wakeThresholdInput.value = String(wake.threshold);
            if (wakeThresholdOut) wakeThresholdOut.textContent = String(wake.threshold);
          }
          if (wakeMinFramesInput && typeof wake.min_frames === "number") {
            wakeMinFramesInput.value = String(wake.min_frames);
            if (wakeMinFramesOut) wakeMinFramesOut.textContent = String(wake.min_frames);
          }
        } catch (e) {
          if (voiceStatusStrip) {
            voiceStatusStrip.textContent = window.AkanaI18n.t("voicecfg.status.failed", { error: e.message || e });
          }
        }
      }

      if (ttsVoiceSelect) {
        ttsVoiceSelect.addEventListener("change", () => {
          const raw = ttsVoiceSelect.value || "";
          // Backwards-compat: legacy "tr"/"en" values still select language.
          if (raw === "tr" || raw === "en") {
            localStorage.setItem(LS_TTS_LANG, raw);
            return;
          }
          const [engine, voiceId, lang] = raw.split("|");
          if (lang === "tr" || lang === "en") localStorage.setItem(LS_TTS_LANG, lang);
          const patch = { tts_engine: engine || "auto" };
          // Edge voices are saved per language; Piper language is resolved from path.
          if (engine === "edge" && voiceId) {
            if (lang === "tr") patch.tts_voice_tr = voiceId;
            else if (lang === "en") patch.tts_voice_en = voiceId;
          }
          void bridge.saveVoicePreferences(patch).catch(() => {});
        });
      }

      if (ttsTestBtn) {
        ttsTestBtn.addEventListener("click", async () => {
          const lang = selectedTtsLang();
          const phrase = lang === "en"
            ? "Hello, this is Akana. Voice output is working."
            : "Merhaba, ben Akana. Sesli çıkış çalışıyor.";
          if (ttsTestStatus) {
            ttsTestStatus.textContent = window.AkanaI18n.t("voicecfg.tts.synthesizing");
            ttsTestStatus.style.color = "";
          }
          try {
            const r = await fetch(`${baseUrl()}/api/v1/voice/tts`, {
              method: "POST",
              headers: authHeaders(true),
              body: JSON.stringify({ text: phrase, lang }),
            });
            if (!r.ok) {
              const err = await r.json().catch(() => ({}));
              throw new Error(err?.detail?.error?.message || `HTTP ${r.status}`);
            }
            const blob = await r.blob();
            const url = URL.createObjectURL(blob);
            const audio = new Audio(url);
            audio.onended = audio.onerror = () => URL.revokeObjectURL(url);
            await audio.play();
            if (ttsTestStatus) {
              ttsTestStatus.textContent = window.AkanaI18n.t("voicecfg.tts.playing");
              ttsTestStatus.style.color = "var(--ok)";
            }
          } catch (e) {
            if (ttsTestStatus) {
              ttsTestStatus.textContent = window.AkanaI18n.t("voicecfg.tts.error", { error: e.message || e });
              ttsTestStatus.style.color = "var(--err)";
            }
          }
        });
      }

      // Wake DETECTION SOURCE: "model" (server openWakeWord, DEFAULT) | "browser"
      // (SpeechRecognition phrase-match). akana-voice.js reads akana.wakeSource at runtime.
      // The threshold + sustain sliders only apply to MODEL mode → disable them in browser mode.
      const syncWakeThresholdDisabled = () => {
        const browserMode = wakeSourceSelect
          ? wakeSourceSelect.value === "browser"
          : false;
        if (wakeThresholdInput) wakeThresholdInput.disabled = browserMode;
        if (wakeMinFramesInput) wakeMinFramesInput.disabled = browserMode;
      };
      if (wakeSourceSelect) {
        let src = "model";
        try {
          src = localStorage.getItem(LS_WAKE_SOURCE) === "browser" ? "browser" : "model";
        } catch {
          /* ignore */
        }
        wakeSourceSelect.value = src;
        syncWakeThresholdDisabled();
        wakeSourceSelect.addEventListener("change", async () => {
          const v = wakeSourceSelect.value === "browser" ? "browser" : "model";
          try {
            localStorage.setItem(LS_WAKE_SOURCE, v);
          } catch {
            /* ignore */
          }
          syncWakeThresholdDisabled(); // threshold applies to model mode only
          // Live-apply: wakeSourcePref() is only read when wake is armed, so a
          // running detector keeps using the OLD source until re-armed. If wake is
          // currently listening, cycle it (awaiting the async release first, per the
          // barge-in lifecycle) so the new source takes effect without a reload.
          if (bridge.voice.wakeEnabled) {
            try {
              await bridge.setWakeListening(false, { silent: true });
              await bridge.setWakeListening(true, { silent: true });
            } catch {
              /* best-effort live re-arm */
            }
          }
        });
      }

      // Conversation STT source: "browser" (DEFAULT, SpeechRecognition) | "whisper"
      // (server faster-whisper). akana-voice.js reads akana.sttSource at capture time.
      // Whisper needs the voice extra installed; loadVoiceConfig() gates the option on
      // /voice/config stt.installed (whisperSttInstalled below).
      let whisperSttInstalled = true; // optimistic until /voice/config reports otherwise
      const syncSttSourceAvailability = () => {
        if (!sttSourceSelect) return;
        const whisperOpt = [...sttSourceSelect.options].find((o) => o.value === "whisper");
        if (whisperOpt) {
          whisperOpt.disabled = !whisperSttInstalled;
          if (!whisperSttInstalled) {
            whisperOpt.textContent = window.AkanaI18n.t("settings.voice.stt_source_whisper_unavailable");
          } else {
            whisperOpt.textContent = window.AkanaI18n.t("settings.voice.stt_source_whisper");
          }
        }
        // If whisper was previously selected but is now unavailable, fall back to browser
        // in the UI (the runtime reader already defaults to browser for any non-"whisper"
        // value, but keep the control honest and avoid a disabled option staying selected).
        if (!whisperSttInstalled && sttSourceSelect.value === "whisper") {
          sttSourceSelect.value = "browser";
        }
        if (sttSourceHint) {
          sttSourceHint.textContent = whisperSttInstalled
            ? window.AkanaI18n.t("settings.voice.stt_source_hint")
            : window.AkanaI18n.t("settings.voice.stt_source_hint_no_whisper");
        }
      };
      if (sttSourceSelect) {
        let sttSrc = "browser";
        try {
          sttSrc = localStorage.getItem(LS_STT_SOURCE) === "whisper" ? "whisper" : "browser";
        } catch {
          /* ignore */
        }
        sttSourceSelect.value = sttSrc;
        syncSttSourceAvailability();
        sttSourceSelect.addEventListener("change", () => {
          const v = sttSourceSelect.value === "whisper" ? "whisper" : "browser";
          try {
            localStorage.setItem(LS_STT_SOURCE, v);
          } catch {
            /* ignore */
          }
        });
      }
      // Exposed so loadVoiceConfig can refresh availability once /voice/config resolves.
      const applyWhisperSttInstalled = (installed) => {
        whisperSttInstalled = !!installed;
        syncSttSourceAvailability();
      };

      // Shared wake-slider save: unlike a fire-and-forget PUT, this surfaces
      // 401/422/network failures — the sliders would otherwise keep showing an
      // unsaved value with no feedback — and re-syncs the control to the real
      // server value (via loadVoiceConfig) when the save did not land.
      async function putWakeSetting(body) {
        try {
          const r = await fetch(`${baseUrl()}/api/v1/settings/runtime`, {
            method: "PUT",
            headers: authHeaders(true),
            body: JSON.stringify(body),
          });
          if (!r.ok) {
            const err = await r.json().catch(() => ({}));
            const msg = window.AkanaCore?.parseApiError?.(err, r.status) || `HTTP ${r.status}`;
            bridge.hooks.showToast(window.AkanaI18n.t("voicecfg.save.failed", { error: msg }), "error");
            void loadVoiceConfig();
            return false;
          }
          return true;
        } catch (e) {
          bridge.hooks.showToast(
            window.AkanaI18n.t("voicecfg.save.failed", { error: e.message || e }),
            "error",
          );
          void loadVoiceConfig();
          return false;
        }
      }

      if (wakeThresholdInput) {
        syncWakeThresholdDisabled();
        wakeThresholdInput.addEventListener("input", () => {
          if (wakeThresholdOut) wakeThresholdOut.textContent = wakeThresholdInput.value;
        });
        wakeThresholdInput.addEventListener("change", () => {
          const v = Number(wakeThresholdInput.value);
          if (Number.isFinite(v)) {
            // wake_threshold single source of truth: PUT /api/v1/settings/runtime.
            // Reflected in Settings on load, so /voice/* status read (populating the slider)
            // sees the same value.
            void putWakeSetting({ wake_threshold: v });
          }
        });
      }

      if (wakeMinFramesInput) {
        wakeMinFramesInput.addEventListener("input", () => {
          if (wakeMinFramesOut) wakeMinFramesOut.textContent = wakeMinFramesInput.value;
        });
        wakeMinFramesInput.addEventListener("change", () => {
          const v = Number(wakeMinFramesInput.value);
          // wake_min_frames rides the SAME runtime store as wake_threshold (store>env>
          // default), applied live to Settings.wake_min_frames — voice/wake.py reads it
          // per poll, so the sustain gate tightens without a restart.
          if (Number.isInteger(v)) {
            void putWakeSetting({ wake_min_frames: v });
          }
        });
      }

      if (wakeAutostartCb) {
        wakeAutostartCb.addEventListener("change", () => {
          wakeAutostartEnabled = wakeAutostartCb.checked;
          writeWakeAutostartLocal(wakeAutostartEnabled);
          void bridge.saveVoicePreferences({ wake_autostart: wakeAutostartEnabled }).catch(() => {});
          if (wakeAutostartEnabled && bridge.hooks.isChatPage) {
            armWakeAutostartOnGesture();
            void maybeAutostartWake();
          }
        });
      }

      // ─── Conversation voice mode controls ─────────────────────────────────
      // READs current localStorage values on startup, WRITEs on change.
      // Keys are read at runtime by akana-voice.js, so no other module needs
      // touching — just write the same keys.
      function readBoolPref(key, defaultOn) {
        try {
          const v = localStorage.getItem(key);
          if (v === "1") return true;
          if (v === "0") return false;
        } catch {
          /* ignore */
        }
        return defaultOn;
      }
      function writeBoolPref(key, on) {
        try {
          localStorage.setItem(key, on ? "1" : "0");
        } catch {
          /* ignore */
        }
      }

      function initConversationModeUx() {
        const wakeEntersCb = document.getElementById("conv-wake-enters");
        const bargeInCb = document.getElementById("conv-barge-in");
        const earconsCb = document.getElementById("conv-earcons");
        const silenceInput = document.getElementById("conv-silence-ms");
        const silenceOut = document.getElementById("conv-silence-ms-out");
        const bargeRmsInput = document.getElementById("conv-barge-rms");
        const bargeRmsOut = document.getElementById("conv-barge-rms-out");

        if (wakeEntersCb) {
          wakeEntersCb.checked = readBoolPref(LS_WAKE_ENTERS_CONV, true);
          wakeEntersCb.addEventListener("change", () => {
            writeBoolPref(LS_WAKE_ENTERS_CONV, wakeEntersCb.checked);
          });
        }

        if (bargeInCb) {
          bargeInCb.checked = readBoolPref(LS_BARGE_IN, false);
          const syncBargeRmsDisabled = () => {
            if (bargeRmsInput) bargeRmsInput.disabled = !bargeInCb.checked;
          };
          syncBargeRmsDisabled();
          bargeInCb.addEventListener("change", () => {
            writeBoolPref(LS_BARGE_IN, bargeInCb.checked);
            syncBargeRmsDisabled(); // threshold slider is meaningless when barge is off → lock it
          });
        }

        if (earconsCb) {
          earconsCb.checked = readBoolPref(LS_VOICE_EARCONS, false);
          earconsCb.addEventListener("change", () => {
            writeBoolPref(LS_VOICE_EARCONS, earconsCb.checked);
            syncEarconVolDisabled(); // volume slider is meaningless when earcons are off → lock it
          });
        }

        // Earcon VOLUME slider — akana-voice.js earconVolume() reads this key at RUNTIME.
        // No index.html/styles.css edit: the slider DOM is injected here reusing the SAME
        // markup/classes as #conv-silence-ms (label + <output> + range + field-hint). The
        // i18n MutationObserver auto-translates the data-i18n nodes on insert + on language change.
        let earconVolInput = document.getElementById("conv-earcon-vol");
        let earconVolOut = document.getElementById("conv-earcon-vol-out");
        const earconVolFromStore = () => {
          let vol = EARCON_VOL_DEFAULT;
          try {
            const raw = Number(localStorage.getItem(LS_EARCON_VOL));
            if (Number.isFinite(raw) && raw >= 0 && raw <= 1) {
              vol = Math.min(EARCON_VOL_MAX, Math.max(EARCON_VOL_MIN, raw));
            }
          } catch {
            /* ignore */
          }
          return vol;
        };
        const volToPct = (v) => String(Math.round(v * 100));
        const syncEarconVolDisabled = () => {
          if (earconVolInput && earconsCb) earconVolInput.disabled = !earconsCb.checked;
        };
        if (!earconVolInput) {
          // Insert after the earcons checkbox's field-hint (same block as the other conv prefs).
          const anchor =
            (earconsCb &&
              earconsCb.closest("label") &&
              earconsCb.closest("label").nextElementSibling) ||
            (earconsCb && earconsCb.closest("label")) ||
            null;
          if (anchor && anchor.parentNode) {
            const label = document.createElement("label");
            label.setAttribute("for", "conv-earcon-vol");
            const span = document.createElement("span");
            span.setAttribute("data-i18n", "settings.voice.earcon_vol_label");
            span.textContent = window.AkanaI18n.t("settings.voice.earcon_vol_label");
            const out = document.createElement("output");
            out.id = "conv-earcon-vol-out";
            out.setAttribute("for", "conv-earcon-vol");
            label.append(span, document.createTextNode(" "), out, document.createTextNode(" %"));
            const input = document.createElement("input");
            input.id = "conv-earcon-vol";
            input.type = "range";
            input.min = String(EARCON_VOL_MIN);
            input.max = String(EARCON_VOL_MAX);
            input.step = "0.05";
            input.value = String(EARCON_VOL_DEFAULT);
            const hint = document.createElement("p");
            hint.className = "field-hint";
            hint.setAttribute("data-i18n", "settings.voice.earcon_vol_hint");
            hint.textContent = window.AkanaI18n.t("settings.voice.earcon_vol_hint");
            anchor.parentNode.insertBefore(label, anchor.nextSibling);
            anchor.parentNode.insertBefore(input, label.nextSibling);
            anchor.parentNode.insertBefore(hint, input.nextSibling);
            earconVolInput = input;
            earconVolOut = out;
          }
        }
        if (earconVolInput) {
          const vol = earconVolFromStore();
          earconVolInput.value = String(vol);
          if (earconVolOut) earconVolOut.textContent = volToPct(vol);
          syncEarconVolDisabled();
          earconVolInput.addEventListener("input", () => {
            if (earconVolOut) earconVolOut.textContent = volToPct(Number(earconVolInput.value));
          });
          earconVolInput.addEventListener("change", () => {
            const v = Math.min(
              EARCON_VOL_MAX,
              Math.max(EARCON_VOL_MIN, Number(earconVolInput.value)),
            );
            earconVolInput.value = String(v);
            if (earconVolOut) earconVolOut.textContent = volToPct(v);
            try {
              localStorage.setItem(LS_EARCON_VOL, String(v));
            } catch {
              /* ignore */
            }
          });
        }

        if (silenceInput) {
          let ms = CONV_SILENCE_DEFAULT;
          try {
            const raw = Number(localStorage.getItem(LS_CONV_SILENCE_MS));
            if (Number.isFinite(raw) && raw > 0) {
              ms = Math.min(CONV_SILENCE_MAX, Math.max(CONV_SILENCE_MIN, Math.round(raw)));
            }
          } catch {
            /* ignore */
          }
          silenceInput.value = String(ms);
          if (silenceOut) silenceOut.textContent = String(ms);
          silenceInput.addEventListener("input", () => {
            if (silenceOut) silenceOut.textContent = silenceInput.value;
          });
          silenceInput.addEventListener("change", () => {
            const v = Math.min(
              CONV_SILENCE_MAX,
              Math.max(CONV_SILENCE_MIN, Number(silenceInput.value) || CONV_SILENCE_DEFAULT),
            );
            silenceInput.value = String(v);
            if (silenceOut) silenceOut.textContent = String(v);
            try {
              localStorage.setItem(LS_CONV_SILENCE_MS, String(v));
            } catch {
              /* ignore */
            }
          });
        }

        // Barge-in threshold — akana-voice.js _threshold() reads this key at RUNTIME
        // → change takes effect immediately (no restart). Lower = more sensitive.
        if (bargeRmsInput) {
          let rms = BARGE_RMS_DEFAULT;
          try {
            const raw = Number(localStorage.getItem(LS_BARGE_RMS));
            if (Number.isFinite(raw) && raw > 0 && raw < 1) {
              rms = Math.min(BARGE_RMS_MAX, Math.max(BARGE_RMS_MIN, raw));
            }
          } catch {
            /* ignore */
          }
          bargeRmsInput.value = String(rms);
          if (bargeRmsOut) bargeRmsOut.textContent = rms.toFixed(3);
          bargeRmsInput.addEventListener("input", () => {
            if (bargeRmsOut) bargeRmsOut.textContent = Number(bargeRmsInput.value).toFixed(3);
          });
          bargeRmsInput.addEventListener("change", () => {
            const v = Math.min(
              BARGE_RMS_MAX,
              Math.max(BARGE_RMS_MIN, Number(bargeRmsInput.value) || BARGE_RMS_DEFAULT),
            );
            bargeRmsInput.value = String(v);
            if (bargeRmsOut) bargeRmsOut.textContent = v.toFixed(3);
            try {
              localStorage.setItem(LS_BARGE_RMS, String(v));
            } catch {
              /* ignore */
            }
          });
        }
      }

      async function initVoiceUx() {
        bridge.setTtsEnabled(localStorage.getItem(LS_TTS) === "1");
        if (bridge.ttsToggle) bridge.ttsToggle.checked = bridge.getTtsEnabled();
        initConversationModeUx();
        // When provider changes in the model-switcher (e.g. cursor→gemini) refresh
        // /voice/config so AkanaVoiceLiveCfg.provider_is_gemini stays current; "Live (realtime)"
        // toggle visibility + shouldUseLiveMode work correctly without an F5.
        try {
          window.AkanaBus?.on?.("llm:provider:changed", () => void loadVoiceConfig());
        } catch (_e) {
          /* silent if bus is absent */
        }
        await Promise.all([loadVoiceConfig(), bridge.loadVoicePreferences()]);
        syncWakeAutostartUi();
        if (bridge.hooks.isChatPage && wakeAutostartEnabled) {
          armWakeAutostartOnGesture();
          void maybeAutostartWake();
        }
      }

    return {
      initVoiceUx,
      applyVoicePreferencesFromServer,
      refreshMicDeviceList,
      maybeAutostartWake,
      armWakeAutostartOnGesture,
      loadVoiceConfig,
      getWakeAutostartEnabled: () => wakeAutostartEnabled,
    };
  }

  window.AkanaVoiceSettings = { create: createSettings, createSettings };
})();
