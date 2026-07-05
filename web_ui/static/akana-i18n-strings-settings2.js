/**
 * Akana UI i18n strings — SETTINGS area, part 2 (voice-settings, personas, vault, pair).
 * Merges into window.AkanaI18nStrings. { en, tr }, English-first. Keys: voicecfg.* / persona.* / vault.* / pair.*
 */
window.AkanaI18nStrings = Object.assign(window.AkanaI18nStrings || {}, {

  // ── akana-voice-settings.js ───────────────────────────────────────────────
  "voicecfg.cap.transcript_yes":  { en: "Live transcript: available.", tr: "Canlı transkript: var." },
  "voicecfg.cap.transcript_no":   { en: "Live transcript: not available in this browser.", tr: "Canlı transkript: bu tarayıcıda yok." },
  "voicecfg.cap.mic_yes":         { en: "Microphone API: ready.", tr: "Mikrofon API: hazır." },
  "voicecfg.cap.mic_no":          { en: "Microphone API: unavailable.", tr: "Mikrofon API: yok." },
  "voicecfg.mic.default":         { en: "System default", tr: "Sistem varsayılanı" },
  "voicecfg.mic.n":               { en: "Microphone {n}", tr: "Mikrofon {n}" },
  "voicecfg.wake.toast": {
    en: "Microphone permission required for Hey Akana — click once or press the headset button.",
    tr: "Hey Akana için mikrofon izni gerekli — bir kez tıklayın veya kulaklık düğmesine basın.",
  },
  "voicecfg.setup.need_wake":  { en: "«Hey Akana» server setup", tr: "«Hey Akana» için sunucu kurulumu" },
  "voicecfg.setup.need_tts":   { en: "voice model for spoken replies", tr: "sesli yanıt için ses modeli" },
  "voicecfg.setup.banner": {
    en: "{needs} required — configurable from Settings › Voice. Browser wake still works.",
    tr: "{needs} gerekiyor — Ayarlar › Ses'ten kurabilirsin. Tarayıcı wake yine çalışır.",
  },
  "voicecfg.chip.tts_missing":  { en: "TTS missing", tr: "TTS eksik" },
  "voicecfg.chip.wake_server":  { en: "Wake server", tr: "Wake sunucu" },
  "voicecfg.chip.wake_browser": { en: "Wake browser", tr: "Wake tarayıcı" },
  "voicecfg.chip.stt_server":   { en: "STT server", tr: "STT sunucu" },
  "voicecfg.chip.stt_browser":  { en: "STT browser", tr: "STT tarayıcı" },
  "voicecfg.status.output":     { en: "Output: {tts}", tr: "Çıkış: {tts}" },
  "voicecfg.status.wake":       { en: "Wake: {wake}", tr: "Wake: {wake}" },
  "voicecfg.status.stt":        { en: "STT: {stt}", tr: "STT: {stt}" },
  "voicecfg.status.tts_ready":     { en: "ready", tr: "hazır" },
  "voicecfg.status.tts_no_model":  { en: "model missing", tr: "model yok" },
  "voicecfg.status.tts_off":       { en: "off", tr: "kapalı" },
  "voicecfg.status.wake_server":   { en: "server", tr: "sunucu" },
  "voicecfg.status.wake_browser":  { en: "browser fallback", tr: "tarayıcı yedek" },
  "voicecfg.status.stt_server":    { en: "server", tr: "sunucu" },
  "voicecfg.status.stt_browser":   { en: "browser", tr: "tarayıcı" },
  "voicecfg.tts.no_model":  { en: "No voice model found — make install-voice", tr: "Ses modeli bulunamadı — make install-voice" },
  "voicecfg.tts.not_exists": { en: " (missing)", tr: " (yok)" },
  "voicecfg.wake.hint_server": {
    en: "Server scoring active ({model}). Browser «Hey Akana» always works.",
    tr: "Sunucu skoru aktif ({model}). Tarayıcı «Hey Akana» her zaman çalışır.",
  },
  "voicecfg.wake.hint_browser": {
    en: "If you get 503 run in terminal: make setup-full — then restart server. Browser recognition still works.",
    tr: "503 alıyorsan terminalde: make setup-full — ardından sunucuyu yeniden başlat. Tarayıcı tanıma yine çalışır.",
  },
  "voicecfg.tts.synthesizing": { en: "Synthesizing…", tr: "Sentezleniyor…" },
  "voicecfg.tts.playing":      { en: "Playing.", tr: "Çalıyor." },
  "voicecfg.tts.error":        { en: "Error: {error}", tr: "Hata: {error}" },
  "voicecfg.status.failed":    { en: "Voice status unavailable: {error}", tr: "Ses durumu alınamadı: {error}" },
  "voicecfg.save.failed":      { en: "Setting could not be saved: {error}", tr: "Ayar kaydedilemedi: {error}" },

  // ── akana-personas.js ─────────────────────────────────────────────────────
  // (persona.* keys already defined in akana-i18n-strings-settings.js — no new keys needed)

  // ── akana-vault.js ────────────────────────────────────────────────────────
  // (vault.* keys already defined in akana-i18n-strings-settings.js — no new keys needed)

  // ── akana-pair.js ─────────────────────────────────────────────────────────
  // (pair.* keys already defined in akana-i18n-strings-settings.js — no new keys needed)
});
