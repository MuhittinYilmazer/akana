/**
 * Akana UI i18n strings — VOICE area. Merges into window.AkanaI18nStrings.
 * { en, tr }, English-first. Keys: voice.*
 */
window.AkanaI18nStrings = Object.assign(window.AkanaI18nStrings || {}, {
  // ── Live transcript strip ─────────────────────────────────────────────────
  "voice.rec_label": { en: "REC", tr: "KAYIT" },

  // ── aurora-voice: state labels ───────────────────────────────────────────
  "voice.state_listening": { en: "Listening", tr: "Dinliyorum" },
  "voice.state_thinking": { en: "Thinking", tr: "Düşünüyorum" },
  "voice.state_responding": { en: "Responding", tr: "Yanıtlıyorum" },

  // ── aurora-voice: state sub-labels ───────────────────────────────────────
  "voice.sub_listening": { en: "Listening to you — I'll understand automatically when you finish", tr: "Seni dinliyorum — bitince otomatik anlarım" },
  "voice.sub_thinking": { en: "Scanning your context and memory", tr: "Bağlamı ve hafızanı tarıyorum" },
  "voice.sub_responding": { en: "Response incoming — listen or jump in", tr: "Yanıt geliyor — dinle ya da araya gir" },

  // ── aurora-voice: overlay aria-label ─────────────────────────────────────
  "voice.overlay_aria_label": { en: "Voice mode", tr: "Sesli mod" },

  // ── aurora-voice: close button aria-label ────────────────────────────────
  "voice.close_btn_label": { en: "Close", tr: "Kapat" },

  // ── aurora-voice: mute button ────────────────────────────────────────────
  "voice.mute_btn": { en: "Mute", tr: "Sustur" },
  "voice.muted_btn": { en: "Muted", tr: "Susturuldu" },

  // ── aurora-voice: end-call button ────────────────────────────────────────

  // ── aurora-voice: barge-in toggle + stop button ──────────────────────────
  "voice.barge_btn": { en: "Barge-in", tr: "Araya gir" },
  "voice.barge_on": { en: "Barge-in on", tr: "Araya girme açık" },
  "voice.barge_off": { en: "Barge-in off", tr: "Araya girme kapalı" },
  "voice.stop_btn": { en: "Stop", tr: "Durdur" },
  "voice.stop_btn_title": { en: "Stop the reply and listen", tr: "Yanıtı durdur ve dinle" },

  // ── aurora-voice: conversation panel title ───────────────────────────────
  "voice.convo_title": { en: "Voice chat", tr: "Sesli sohbet" },

  // ── aurora-voice: server connection tooltip ───────────────────────────────
  "voice.server_conn_title": { en: "Server connection", tr: "Sunucu bağlantısı" },

  // ── aurora-voice: WS status labels ───────────────────────────────────────
  "voice.ws_connected": { en: "connected", tr: "bağlı" },
  "voice.ws_connecting": { en: "connecting", tr: "bağlanıyor" },
  "voice.ws_closed": { en: "closed", tr: "kapalı" },

  // ── aurora-voice: user turn label ────────────────────────────────────────
  "voice.user_label": { en: "You", tr: "Sen" },

  // ── aurora-voice: hero text ───────────────────────────────────────────────
  "voice.hero_listening": { en: "Listening to you…", tr: "Seni dinliyorum…" },

  // ── aurora-voice: no-answer notice ───────────────────────────────────────
  "voice.no_answer_notice": { en: "No response received — check connection.", tr: "Yanıt alınamadı — bağlantıyı kontrol et." },

  // ── aurora-voice: tool-call panel header ─────────────────────────────────
  "voice.tools_head": { en: "What Akana is doing", tr: "Akana ne yapıyor" },
  "voice.tools_head_n": { en: "What Akana is doing · {n} steps", tr: "Akana ne yapıyor · {n} adım" },
  "voice.tools_running_label": { en: "Running tools", tr: "Çalışan araçlar" },

  // ── aurora-voice: fallback tool text ─────────────────────────────────────
  "voice.tool_called": { en: "called a tool", tr: "araç çağırdı" },

  // ── akana-voice.js: mic permission errors ────────────────────────────────
  "voice.err_mic_not_found": { en: "Microphone not found. Check that a microphone is connected to your device.", tr: "Mikrofon bulunamadı. Cihazına bir mikrofon bağlı mı kontrol et." },
  "voice.err_mic_denied": { en: "Microphone permission denied. Allow microphone access in your browser/app settings for conversation mode, then tap Voice again.", tr: "Mikrofon izni verilmedi. Konuşma modu için tarayıcı/uygulama ayarlarından mikrofon erişimine izin ver, sonra Ses'e tekrar dokun." },

  // ── akana-voice.js: cancelled utterance ──────────────────────────────────
  "voice.cancelled": { en: "Cancelled.", tr: "Vazgeçildi." },

  // ── akana-voice-capture.js: microphone disconnected / device lost mid-session ──
  "voice.mic_disconnected": { en: "The microphone was disconnected. Reconnecting to the default device…", tr: "Mikrofon bağlantısı kesildi. Varsayılan cihaza yeniden bağlanılıyor…" },

  // ── akana-voice.js: live mode — HTTPS required ────────────────────────────
  "voice.err_live_https": { en: "Secure context (HTTPS) is required for live voice — open the site from an HTTPS address.", tr: "Canlı ses için güvenli bağlam (HTTPS) gerekiyor — siteyi HTTPS adresinden aç." },

  // ── akana-voice.js: live mode — Gemini unavailable ───────────────────────
  "voice.err_live_unavailable": { en: "Live voice is unavailable — enable it in Settings → Voice or enter your API key.", tr: "Gemini Live kullanılamıyor — Ayarlar → Ses'ten aç ya da API anahtarını gir." },

  // ── akana-voice.js: live mode — generic start failure ────────────────────
  "voice.err_live_start_failed": { en: "Live voice could not start; please try again.", tr: "Canlı ses başlatılamadı; lütfen tekrar dene." },

  // ── akana-voice.js: conversation mode — no SR ────────────────────────────
  "voice.err_no_sr": { en: "Conversation mode is not supported in this browser (SpeechRecognition missing). Try Chrome or Edge.", tr: "Konuşma modu bu tarayıcıda desteklenmiyor (SpeechRecognition yok). Chrome/Edge dene." },

  // ── akana-voice.js: conversation mode — HTTPS required ───────────────────
  "voice.err_conv_https": { en: "Secure context (HTTPS) is required for conversation mode. Microphone only works over HTTPS or localhost — open the site from an HTTPS address (e.g. Tailscale https).", tr: "Konuşma modu için güvenli bağlam (HTTPS) gerekiyor. Mikrofon yalnız HTTPS ya da localhost üzerinden açılır — siteyi HTTPS adresinden (ör. Tailscale https) aç." },

  // ── akana-voice.js: offline — voice turn ─────────────────────────────────
  "voice.offline_voice": { en: "Connection lost — I'll continue when you speak again.", tr: "Bağlantı koptu — tekrar konuştuğunda devam ederim." },

  // ── akana-voice.js: offline — typed turn ─────────────────────────────────
  "voice.offline_typed": { en: "Connection lost — you can resend when reconnected.", tr: "Bağlantı koptu — yeniden bağlanınca tekrar gönderebilirsin." },

  // ── akana-voice.js: offline meta labels ──────────────────────────────────
  "voice.meta_voice": { en: "Voice", tr: "Ses" },
  "voice.meta_connection": { en: "Connection", tr: "Bağlantı" },

  // ── akana-voice.js: wake meter (RMS / score readout) ─────────────────────
  "voice.meter_rms": { en: "rms {rms}", tr: "ses {rms}" },
  "voice.meter_score": { en: "score {score} / {threshold}", tr: "skor {score} / {threshold}" },

  // ── aurora-voice.js: active-model chip tooltip ───────────────────────────
  "voice.model_chip_title": { en: "Active model", tr: "Etkin model" },

  // ── akana-voice.js: wake test messages ───────────────────────────────────
  "voice.wake_test_no_audio": { en: "First enable Hey Akana and speak for 2–3 s, then try again.", tr: "Önce Hey Akana'i aç ve 2–3 sn konuş, sonra tekrar dene." },
  "voice.wake_test_result": { en: "Score {score} / threshold {threshold} — {status}. Try «hey akana» in English.", tr: "Skor {score} / eşik {threshold} — {status}. «hey akana» İngilizce dene." },
  "voice.wake_test_triggered": { en: "TRIGGERED", tr: "TETİKLENDİ" },
  "voice.wake_test_not_yet": { en: "not yet", tr: "henüz değil" },

  // ── akana-voice-pipeline.js: Hey Akana triggered ─────────────────────────
  "voice.wake_triggered_msg": { en: "Hey Akana ({source}) — I'm listening, say your command. <em>Press Esc to cancel.</em>", tr: "Hey Akana ({source}) — dinliyorum, komutunu söyle. <em>Vazgeçmek için Esc.</em>" },

  // ── akana-voice-pipeline.js: one-shot voice reply — TTS failed after the turn persisted ──
  "voice.tts_failed_meta": {
    en: "Read-aloud failed — the reply is shown as text. Tap Speak to retry.",
    tr: "Sesli okuma başarısız — yanıt metin olarak gösteriliyor. Tekrar denemek için Seslendir'e dokun.",
  },
  "voice.tts_retry_btn": { en: "Speak", tr: "Seslendir" },

  // ── akana-voice-pipeline.js: meta labels ─────────────────────────────────
  "voice.meta_you_voice": { en: "You (voice)", tr: "Sen (ses)" },
  "voice.meta_akana_latency": { en: "Akana · {ms} ms", tr: "Akana · {ms} ms" },
  "voice.meta_wake": { en: "Voice / wake", tr: "Ses / wake" },
  "voice.meta_wake_test": { en: "Wake test", tr: "Wake test" },

  // ── wake trigger source labels (shown in bubble: "Hey Akana (server)") ───
  "voice.wake_src_server": { en: "server", tr: "sunucu" },
  "voice.wake_src_browser": { en: "browser", tr: "tarayıcı" },

  // ── wake source fallback notice (model requested but server scoring unavailable) ──
  "voice.wake_model_fallback": {
    en: "The «Hey Akana» model isn't set up on the server — using browser recognition instead. Run voice setup to enable the model detector.",
    tr: "Sunucuda «Hey Akana» modeli kurulu değil — bunun yerine tarayıcı tanıması kullanılıyor. Model algılayıcısını etkinleştirmek için ses kurulumunu çalıştır.",
  },

  // ── akana-voice-pipeline.js: wake error hints ────────────────────────────
  "voice.wake_err_hint_503": {
    en: " — Server: python akana.py add voice-full",
    tr: " — Sunucuda: python akana.py add voice-full",
  },
  "voice.wake_err_hint_401": { en: " — Settings → Connection → API token", tr: " — Ayarlar → Bağlantı → API token" },

  // ── akana-voice-capture.js: AudioWorklet fallback warning (console only) ──
  // (console.warn — not user-facing UI; translated for consistency)
  "voice.warn_worklet_unavailable": { en: "AudioWorklet unavailable, falling back to legacy audio path:", tr: "AudioWorklet kullanılamadı, eski ses yolu deneniyor:" },

  // ── akana-voice-fsm.js: FSM console warnings (not user-facing UI) ─────────
  "voice.fsm_unknown_phase": { en: "[voice-fsm] unknown phase", tr: "[voice-fsm] bilinmeyen faz" },
  "voice.fsm_reject": { en: "[voice-fsm] reject", tr: "[voice-fsm] reddedildi" },
});
