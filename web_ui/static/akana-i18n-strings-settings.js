/**
 * Akana UI i18n strings — SETTINGS area (settings panel, personas, vault, pair).
 * Merges into window.AkanaI18nStrings. { en, tr }, English-first. Keys: settings.* / vault.* / pair.* / persona.*
 */
window.AkanaI18nStrings = Object.assign(window.AkanaI18nStrings || {}, {

  // ── WebSocket status titles / aria-labels ─────────────────────────────────
  "settings.ws.title_default": {
    en: "Live event stream (WebSocket /ws/events) — for approvals and system notifications.",
    tr: "Canlı olay akışı (WebSocket /ws/events) — onaylar ve sistem bildirimleri için.",
  },
  "settings.ws.title_connecting": {
    en: "WebSocket: connecting to server…",
    tr: "WebSocket: sunucuya bağlanıyor…",
  },
  "settings.ws.title_connected": {
    en: "WebSocket connected — live notifications active.",
    tr: "WebSocket bağlı — canlı bildirimler aktif.",
  },
  "settings.ws.title_closed": {
    en: "WebSocket closed — will retry with exponential back-off.",
    tr: "WebSocket kapalı — üstel geri çekilme ile yeniden denenecek.",
  },
  "settings.ws.title_error": {
    en: "WebSocket error — check API address and token in Settings → Connection.",
    tr: "WebSocket hatası — API adresi ve token'ı Ayarlar → Bağlantı'dan kontrol edin.",
  },
  "settings.ws.aria_label": {
    en: "Connection: {state}",
    tr: "Bağlantı: {state}",
  },

  // ── Settings search (command-palette in the panel header) ─────────────────
  "settings.search.placeholder": { en: "Search settings…", tr: "Ayarlarda ara…" },
  "settings.search.aria": { en: "Search settings", tr: "Ayarlarda ara" },
  "settings.search.clear": { en: "Clear search", tr: "Aramayı temizle" },
  "settings.search.results_aria": { en: "Search results", tr: "Arama sonuçları" },
  "settings.search.section": { en: "Section", tr: "Bölüm" },
  "settings.search.empty": {
    en: "No settings match “{q}”",
    tr: "“{q}” ile eşleşen ayar yok",
  },

  // ── Theme button aria/title ────────────────────────────────────────────────
  "settings.theme.aria_to_light": { en: "Theme: switch to light mode", tr: "Tema: açık moda geç" },
  "settings.theme.aria_to_dark":  { en: "Theme: switch to dark mode",  tr: "Tema: koyu moda geç" },
  "settings.theme.title_light":   { en: "Light theme", tr: "Açık tema" },
  "settings.theme.title_dark":    { en: "Dark theme",  tr: "Koyu tema" },
  "settings.theme.applied_system": {
    en: "Applied: {resolved} (system preference)",
    tr: "Uygulanan: {resolved} (sistem tercihi)",
  },
  "settings.theme.applied": { en: "Applied: {resolved}", tr: "Uygulanan: {resolved}" },
  "settings.theme.light_word":    { en: "light", tr: "açık" },
  "settings.theme.dark_word":     { en: "dark",  tr: "koyu" },

  // ── Auth / save error messages ────────────────────────────────────────────
  "settings.err.auth_401": {
    en: "Authorization error (401). Paste the AKANA_TOKEN value from .env into Settings → Connection → API token (if blank the server has tokens disabled).",
    tr: "Yetkilendirme hatası (401). Ayarlar → Bağlantı → API token alanına .env içindeki AKANA_TOKEN değerini yapıştırın (boşsa sunucuda token kapalı olmalı).",
  },
  "settings.err.no_api_404": {
    en: "LLM settings API not found (404). The server may be running an old version — restart it (python akana.py start).",
    tr: "LLM ayarları API'si bulunamadı (404). Sunucu eski bir sürümü çalıştırıyor olabilir — yeniden başlat (python akana.py start).",
  },
  "settings.err.invalid_422": {
    en: "Invalid request (422): {message}{restartHint}",
    tr: "Geçersiz istek (422): {message}{restartHint}",
  },
  "settings.err.invalid_422_bare": {
    en: "Invalid request (422). Refresh the page with Ctrl+Shift+R.{restartHint}",
    tr: "Geçersiz istek (422). Sayfayı Ctrl+Shift+R ile yenileyin.{restartHint}",
  },

  // ── Model pill / profile hint ─────────────────────────────────────────────
  "settings.model.select_prompt": { en: "select model", tr: "model seç" },
  "settings.model.no_connection": { en: "no connection", tr: "bağlantı yok" },
  "settings.model.status_unavailable": { en: "Status unavailable.", tr: "Durum alınamadı." },
  "settings.model.active": { en: "Active model: {model}", tr: "Aktif model: {model}" },
  "settings.model.not_selected": { en: "{label}: no model selected", tr: "{label}: model seçili değil" },
  "settings.model.active_short": { en: "Active: {provider} · {tag}", tr: "Aktif: {provider} · {tag}" },

  // ── Model pill interactive tooltip ───────────────────────────────────────
  "settings.model.pill_title": { en: "Active model — click to change", tr: "Aktif model — tıkla ve değiştir" },
  "settings.model.switcher_aria": { en: "Change model", tr: "Model değiştir" },
  "settings.model.provider_group_aria": { en: "Provider", tr: "Sağlayıcı" },
  "settings.model.list_aria": { en: "Model", tr: "Model" },
  "settings.model.prov_cursor": { en: "Cursor (API)", tr: "Cursor (API)" },
  "settings.model.prov_claude": { en: "Claude CLI (subscription)", tr: "Claude CLI (abonelik)" },
  "settings.model.prov_ollama": { en: "Ollama (local model)", tr: "Ollama (yerel model)" },
  "settings.model.prov_gemini": { en: "Gemini (direct API)", tr: "Gemini (doğrudan API)" },
  "settings.model.prov_openai": { en: "OpenAI (direct API)", tr: "OpenAI (doğrudan API)" },
  "settings.model.prov_badge_dev": { en: "in development", tr: "geliştirme aşamasında" },
  "settings.model.unavailable": { en: "Models unavailable.", tr: "Modeller alınamadı." },

  // ── Model switcher per-provider messages ─────────────────────────────────
  "settings.model.loading": { en: "Loading…", tr: "Yükleniyor…" },
  "settings.model.settings_unavailable": { en: "Settings unavailable.", tr: "Ayarlar alınamadı." },
  "settings.model.cannot_change": { en: "Could not change: {error}", tr: "Değiştirilemedi: {error}" },
  "settings.model.no_connection_ws": { en: "No connection: {error}", tr: "Bağlantı yok: {error}" },

  "settings.model.cursor_loading": { en: "Loading Cursor models…", tr: "Cursor modelleri yükleniyor…" },
  "settings.model.cursor_unreachable": {
    en: "{error} — check CURSOR_API_KEY and network connection.",
    tr: "{error} — CURSOR_API_KEY ve ağ bağlantısını kontrol edin.",
  },
  "settings.model.cursor_unreachable_default": { en: "Cursor API unreachable", tr: "Cursor API erişilemiyor" },
  "settings.model.cursor_empty": { en: "Model list empty — check your Cursor account.", tr: "Model listesi boş — Cursor hesabınızı kontrol edin." },
  "settings.model.cursor_failed": { en: "Cursor models unavailable: {error}", tr: "Cursor modelleri alınamadı: {error}" },
  "settings.model.active_no_list": { en: "Active: {tag} · list not returned by server", tr: "Aktif: {tag} · seçenek listesi sunucudan gelmedi" },
  "settings.model.no_list": { en: "No model list", tr: "Model listesi yok" },

  "settings.model.claude_loading": { en: "Loading Claude models…", tr: "Claude modelleri yükleniyor…" },
  "settings.model.claude_unreachable": {
    en: "{error} — a token is refreshed with each chat message.",
    tr: "{error} — bir sohbet gönderince token tazelenir.",
  },
  "settings.model.claude_unreachable_default": { en: "Claude API unreachable", tr: "Claude API erişilemiyor" },
  "settings.model.claude_empty": { en: "Model list empty — check your Claude session.", tr: "Model listesi boş — Claude oturumunu kontrol edin." },
  "settings.model.claude_failed": { en: "Claude models unavailable: {error}", tr: "Claude modelleri alınamadı: {error}" },

  "settings.model.ollama_loading": { en: "Loading Ollama models…", tr: "Ollama modelleri yükleniyor…" },
  "settings.model.ollama_down": {
    en: "Ollama not running ({url}) — start with `ollama serve`.",
    tr: "Ollama çalışmıyor ({url}) — `ollama serve` başlatın.",
  },
  "settings.model.ollama_empty": { en: "No installed models — pull one with `ollama pull llama3.1`.", tr: "Kurulu model yok — `ollama pull llama3.1` ile indirin." },
  "settings.model.ollama_failed": { en: "Ollama models unavailable: {error}", tr: "Ollama modelleri alınamadı: {error}" },

  "settings.model.gemini_loading": { en: "Loading Gemini models…", tr: "Gemini modelleri yükleniyor…" },
  "settings.model.gemini_unreachable_fallback": {
    en: "{error} — fallback list.",
    tr: "{error} — yedek liste.",
  },
  "settings.model.gemini_unreachable_default": { en: "Gemini API unreachable", tr: "Gemini API erişilemiyor" },
  "settings.model.gemini_failed": { en: "Gemini models unavailable: {error}", tr: "Gemini modelleri alınamadı: {error}" },

  "settings.model.openai_loading": { en: "Loading OpenAI models…", tr: "OpenAI modelleri yükleniyor…" },
  "settings.model.openai_unreachable_fallback": {
    en: "{error} — fallback list.",
    tr: "{error} — yedek liste.",
  },
  "settings.model.openai_unreachable_default": { en: "OpenAI API unreachable", tr: "OpenAI API erişilemiyor" },
  "settings.model.openai_failed": { en: "OpenAI models unavailable: {error}", tr: "OpenAI modelleri alınamadı: {error}" },

  // ── Tab labels (SETTINGS_TAB_LABELS) ─────────────────────────────────────
  "settings.tab.overview":    { en: "General",       tr: "Genel" },
  "settings.tab.llm":         { en: "Provider",      tr: "Sağlayıcı" },
  "settings.tab.credentials": { en: "Credentials",   tr: "Kimlik" },
  "settings.tab.security":    { en: "Security",      tr: "Güvenlik" },
  "settings.tab.runtime":     { en: "Runtime",       tr: "Çalışma Zamanı" },
  "settings.tab.connectors":  { en: "Channels",      tr: "Kanallar" },
  "settings.tab.packs":       { en: "Packs",         tr: "Pack'ler" },
  "settings.tab.voice":       { en: "Voice",         tr: "Ses" },
  "settings.tab.appearance":  { en: "Appearance",    tr: "Görünüm" },
  "settings.tab.connection":  { en: "Connection",    tr: "Bağlantı" },

  // ── Overview hero ─────────────────────────────────────────────────────────
  "settings.hero.turns": { en: "{n} turns", tr: "{n} tur" },

  // ── Health strip ──────────────────────────────────────────────────────────
  "settings.health.critical": {
    en: "{n} critical issue(s) — check Connection or API key",
    tr: "{n} kritik sorun — Bağlantı veya API anahtarını kontrol edin",
  },
  "settings.health.warning": {
    en: "{n} warning(s) — some features may be limited",
    tr: "{n} uyarı — bazı özellikler sınırlı çalışabilir",
  },
  "settings.health.ok": {
    en: "All systems operational ({n} checks passed)",
    tr: "Tüm sistemler çalışıyor ({n} kontrol geçti)",
  },

  // ── WebSocket state labels (wsStateLabel) ────────────────────────────────
  "settings.ws.state_closed":     { en: "closed",      tr: "kapalı" },
  "settings.ws.state_connected":  { en: "connected",   tr: "bağlı" },
  "settings.ws.state_connecting": { en: "connecting",  tr: "bağlanıyor" },

  // ── Connection endpoint card ──────────────────────────────────────────────
  "settings.conn.meta": {
    en: "Token: {tokenState} · WS: {wsState}",
    tr: "Token: {tokenState} · WS: {wsState}",
  },
  "settings.conn.token_set":   { en: "set",   tr: "ayarlı" },
  "settings.conn.token_unset": { en: "none",  tr: "yok" },

  // ── Overview meta dl ─────────────────────────────────────────────────────
  "settings.meta.server":    { en: "Server",     tr: "Sunucu" },
  "settings.meta.python":    { en: "Python",     tr: "Python" },
  "settings.meta.phase":     { en: "Phase",      tr: "Faz" },
  "settings.meta.chat_path": { en: "Chat path",  tr: "Sohbet yolu" },
  "settings.meta.data":      { en: "Data",       tr: "Veri" },

  // ── Status grid stat cards ─────────────────────────────────────────────
  "settings.stat.claude_auth":       { en: "Claude credentials",  tr: "Claude kimliği" },
  "settings.stat.token_loaded":      { en: "Token loaded",        tr: "Token yüklü" },
  "settings.stat.token_missing":     { en: "No token",            tr: "Token yok" },
  "settings.stat.claude_desc":       { en: "claude_oauth_token (subscription) — entered in Credentials tab", tr: "claude_oauth_token (abonelik) — Kimlik sekmesinden girilir" },
  "settings.stat.ollama_auth":       { en: "Ollama (local)",      tr: "Ollama (yerel)" },
  "settings.stat.ollama_no_key":     { en: "No key needed",       tr: "Anahtar gerekmez" },
  "settings.stat.ollama_desc":       { en: "Local model — no API key; select model from the model badge above", tr: "Yerel model — API anahtarı yok; modeli üstteki model rozetinden seçin" },
  "settings.stat.gemini_auth":       { en: "Gemini API",          tr: "Gemini API" },
  "settings.stat.key_loaded":        { en: "Key loaded",          tr: "Anahtar yüklü" },
  "settings.stat.key_missing":       { en: "No key",              tr: "Anahtar yok" },
  "settings.stat.gemini_desc":       { en: "gemini_api_key (direct Google API) — entered in Credentials tab", tr: "gemini_api_key (doğrudan Google API) — Kimlik sekmesinden girilir" },
  "settings.stat.openai_auth":       { en: "OpenAI API",          tr: "OpenAI API" },
  "settings.stat.openai_desc":       { en: "openai_api_key (direct OpenAI API) — entered in Credentials tab", tr: "openai_api_key (doğrudan OpenAI API) — Kimlik sekmesinden girilir" },
  "settings.stat.cursor_auth":       { en: "Cursor API",          tr: "Cursor API" },
  "settings.stat.cursor_desc":       { en: "CURSOR_API_KEY on the server — required for chat and tools", tr: "Sunucudaki CURSOR_API_KEY — sohbet ve araçlar için gerekli" },
  "settings.stat.active_model":      { en: "Active model",        tr: "Aktif model" },
  "settings.stat.change_provider":   { en: "Change from the Provider tab", tr: "Sağlayıcı sekmesinden değiştirebilirsiniz" },
  "settings.stat.chat_history":      { en: "Chat history",        tr: "Sohbet geçmişi" },
  "settings.stat.chat_history_desc": { en: "Recent turns Akana keeps — sent to the LLM only when a session can't be resumed", tr: "Akana'nın tuttuğu son tur sayısı — LLM'e yalnızca oturum sürdürülemediğinde gönderilir" },
  "settings.stat.websocket":         { en: "WebSocket",           tr: "WebSocket" },
  "settings.stat.ws_desc":           { en: "For live status, TTS and tool events", tr: "Canlı durum, TTS ve araç olayları için" },
  "settings.stat.status_loading":    { en: "Loading status…",     tr: "Durum yükleniyor…" },
  "settings.stat.status_failed":     { en: "Status unavailable: {error}", tr: "Durum alınamadı: {error}" },

  // ── LLM form ─────────────────────────────────────────────────────────────
  "settings.llm.saved":          { en: "Saved.", tr: "Kaydedildi." },
  "settings.llm.load_failed":    { en: "Settings could not be loaded: {error}", tr: "Ayarlar yüklenemedi: {error}" },
  "settings.llm.custom_option":  { en: "{value} (custom)", tr: "{value} (özel)" },
  "settings.llm.selected":       { en: "Selected: {model}", tr: "Seçili: {model}" },

  // ── Ollama provider label ─────────────────────────────────────────────────

  // ── Credentials panel ────────────────────────────────────────────────────
  "settings.cred.state_set": {
    en: "Set — {hint}. Writing a new value overwrites it.",
    tr: "Ayarlı — {hint}. Yeni değer girerseniz üzerine yazılır.",
  },
  "settings.cred.state_masked": { en: "masked", tr: "maskeli" },
  "settings.cred.state_unset": { en: "{label} not set yet.", tr: "{label} henüz ayarlı değil." },
  // Which layer the effective value comes from (BUG 1: single source of truth).
  // The runtime store overrides .env; surfacing it explains why a key may differ
  // from the one in the .env file.
  "settings.cred.source_store": { en: "from runtime store", tr: "çalışma zamanı deposundan" },
  "settings.cred.source_env":   { en: "from .env",          tr: ".env dosyasından" },
  "settings.cred.placeholder_set": {
    en: "Set ({hint}) — type to change",
    tr: "Ayarlı ({hint}) — değiştirmek için yaz",
  },
  "settings.cred.reveal_btn":    { en: "Show", tr: "Göster" },
  "settings.cred.hide_btn":      { en: "Hide", tr: "Gizle" },
  "settings.cred.reveal_failed": { en: "Could not reveal: {error}", tr: "Görüntülenemedi: {error}" },
  "settings.cred.cursor_label":  { en: "Cursor API key",    tr: "Cursor API anahtarı" },
  "settings.cred.claude_label":  { en: "Claude setup-token", tr: "Claude setup-token" },
  "settings.cred.gemini_label":  { en: "Gemini API key",    tr: "Gemini API anahtarı" },
  "settings.cred.openai_label":  { en: "OpenAI API key",    tr: "OpenAI API anahtarı" },
  "settings.cred.no_change":     { en: "No changes — empty fields are not sent.", tr: "Değişiklik yok — boş alanlar gönderilmez." },
  "settings.cred.saving":        { en: "Saving…", tr: "Kaydediliyor…" },
  "settings.cred.saved":         { en: "Saved — key stored masked on server.", tr: "Kaydedildi — anahtar sunucuda maskeli saklanıyor." },
  "settings.cred.saved_toast":   { en: "Credentials saved", tr: "Kimlik bilgileri kaydedildi" },
  "settings.cred.save_failed":   { en: "Could not save: {error}", tr: "Kaydedilemedi: {error}" },
  "settings.cred.load_failed":   { en: "Credentials could not be loaded: {error}", tr: "Kimlik bilgileri yüklenemedi: {error}" },
  "settings.cred.status_unavailable": { en: "Status unavailable.", tr: "Durum alınamadı." },

  // ── Runtime settings ─────────────────────────────────────────────────────
  "settings.runtime.source.setting": { en: "setting", tr: "ayar" },
  "settings.runtime.source.env":     { en: "env",     tr: "env" },
  "settings.runtime.source.default": { en: "default", tr: "varsayılan" },
  "settings.runtime.badge_source":   { en: "Value source: {source}{envPart}", tr: "Değerin kaynağı: {source}{envPart}" },
  "settings.runtime.badge_env_part": { en: " (env: {var})", tr: " (env: {var})" },
  "settings.runtime.restart_badge":  { en: "restart required", tr: "restart gerekli" },
  "settings.runtime.restart_title":  { en: "This setting is applied when the server restarts.", tr: "Bu ayar sunucu yeniden başlatıldığında uygulanır." },
  "settings.runtime.save_btn":       { en: "Save",  tr: "Kaydet" },
  "settings.runtime.reset_btn":      { en: "Reset", tr: "Sıfırla" },
  "settings.runtime.reset_title":    { en: "Remove runtime value — env/default chain takes effect.", tr: "Runtime değerini kaldır — env/varsayılan zinciri geçerli olur." },
  "settings.runtime.invalid_number": { en: "Enter a valid number for «{label}».", tr: "«{label}» için geçerli bir sayı girin." },
  "settings.runtime.invalid_integer": { en: "«{label}» must be a whole number.", tr: "«{label}» tam sayı olmalıdır." },
  "settings.runtime.out_of_range":   { en: "«{label}» must be between {min} and {max}.", tr: "«{label}» {min} ile {max} arasında olmalıdır." },
  "settings.runtime.invalid_option": { en: "{value} (invalid)", tr: "{value} (geçersiz)" },
  "settings.runtime.saving":         { en: "Saving…", tr: "Kaydediliyor…" },
  "settings.runtime.saved_restart":  { en: "«{label}» saved — applied on restart", tr: "«{label}» kaydedildi — yeniden başlatmada uygulanır" },
  "settings.runtime.saved_live":     { en: "«{label}» saved — effective immediately", tr: "«{label}» kaydedildi — anında geçerli" },
  "settings.runtime.save_failed":    { en: "Could not save: {error}", tr: "Kaydedilemedi: {error}" },
  "settings.runtime.reset_toast":    { en: "«{label}» reset — env/default in effect", tr: "«{label}» sıfırlandı — env/varsayılan geçerli" },
  "settings.runtime.reset_failed":   { en: "Could not reset: {error}", tr: "Sıfırlanamadı: {error}" },
  "settings.runtime.loading":        { en: "Loading settings…", tr: "Ayarlar yükleniyor…" },
  "settings.runtime.load_failed":    { en: "Settings could not be loaded: {error}", tr: "Ayarlar yüklenemedi: {error}" },
  "settings.runtime.source_legend": {
    en: "Value source badges: «setting» = saved here, «env» = .env, «default» = built-in.",
    tr: "Değer kaynağı rozetleri: «ayar» = buradan kaydedildi, «env» = .env, «varsayılan» = yerleşik.",
  },
  "settings.runtime.paths_placeholder": { en: "/path/one; /path/two", tr: "/yol/bir; /yol/iki" },

  // ── Connectors panel ─────────────────────────────────────────────────────
  "settings.conn.running":   { en: "Running",   tr: "Çalışıyor" },
  "settings.conn.disabled":  { en: "Disabled",  tr: "Devre dışı" },
  "settings.conn.stopped":   { en: "Stopped",   tr: "Durdu" },
  "settings.conn.token_set":     { en: "set",     tr: "ayarlı" },
  "settings.conn.token_unset":   { en: "none",    tr: "yok" },
  "settings.conn.last_error":    { en: "Last error: {error}", tr: "Son hata: {error}" },
  "settings.conn.loading":       { en: "Loading channel status…", tr: "Kanal durumu yükleniyor…" },
  "settings.conn.none": {
    en: "No registered channels. Enable Telegram from the Runtime tab and restart the server.",
    tr: "Kayıtlı kanal yok. Telegram'ı Çalışma Zamanı sekmesinden etkinleştirip sunucuyu yeniden başlatın.",
  },
  "settings.conn.count": {
    en: "{total} channel(s) registered, {running} running.",
    tr: "{total} kanal kayıtlı, {running} çalışıyor.",
  },
  "settings.conn.load_failed": { en: "Channel status unavailable: {error}", tr: "Kanal durumu alınamadı: {error}" },

  // ── Telegram management panel (live; PUT /connectors/telegram) ─────────────
  "settings.tg.enabled_label": { en: "Telegram bridge enabled", tr: "Telegram köprüsü etkin" },
  "settings.tg.token_title":   { en: "Bot token", tr: "Bot token" },
  "settings.tg.token_label":   { en: "New token", tr: "Yeni token" },
  "settings.tg.token_help": {
    en: "Create a bot with @BotFather → /newbot, then paste the token here. Stored encrypted; empty = leave unchanged.",
    tr: "@BotFather → /newbot ile bot oluştur, token'ı buraya yapıştır. Şifreli saklanır; boş = değiştirme.",
  },
  "settings.tg.token_save":  { en: "Save token", tr: "Token'ı kaydet" },
  "settings.tg.token_clear": { en: "Clear", tr: "Temizle" },
  "settings.tg.test":        { en: "Test connection", tr: "Bağlantıyı test et" },
  "settings.tg.chatids_title": { en: "Allowed chat IDs", tr: "İzinli sohbet ID'leri" },
  "settings.tg.chatids_label": { en: "Chat IDs", tr: "Sohbet ID'leri" },
  "settings.tg.chatids_optional": { en: "comma-separated", tr: "virgülle ayrılmış" },
  "settings.tg.chatids_help": {
    en: "Only messages from these chat IDs are answered. Empty = nobody can write. Message @userinfobot to find an ID.",
    tr: "Yalnız bu sohbet ID'lerinden gelen mesajlar yanıtlanır. Boş = kimse yazamaz. ID öğrenmek için @userinfobot'a yaz.",
  },
  "settings.tg.chatids_save": { en: "Save chat IDs", tr: "ID'leri kaydet" },
  "settings.tg.token_state_set":   { en: "Token: set ({hint})", tr: "Token: ayarlı ({hint})" },
  "settings.tg.token_state_unset": { en: "Token: not set", tr: "Token: ayarlı değil" },
  "settings.tg.allowed_count": { en: "{count} allowed chat(s)", tr: "{count} izinli sohbet" },
  "settings.tg.last_message":  { en: "last message {at}", tr: "son mesaj {at}" },
  "settings.tg.saving": { en: "Saving…", tr: "Kaydediliyor…" },
  "settings.tg.save_failed": { en: "Could not save: {error}", tr: "Kaydedilemedi: {error}" },
  "settings.tg.enabled_on":  { en: "Telegram enabled — applied live.", tr: "Telegram etkin — canlı uygulandı." },
  "settings.tg.enabled_off": { en: "Telegram disabled — applied live.", tr: "Telegram devre dışı — canlı uygulandı." },
  "settings.tg.token_saved":   { en: "Token saved — applied live.", tr: "Token kaydedildi — canlı uygulandı." },
  "settings.tg.token_cleared": { en: "Token cleared.", tr: "Token temizlendi." },
  "settings.tg.token_empty":   { en: "Enter a token first.", tr: "Önce bir token gir." },
  "settings.tg.chatids_saved": { en: "Allowed chat IDs saved — applied live.", tr: "İzinli sohbet ID'leri kaydedildi — canlı uygulandı." },
  "settings.tg.testing":   { en: "Testing token…", tr: "Token test ediliyor…" },
  "settings.tg.test_ok":   { en: "Token is live: @{username} (id {id}).", tr: "Token canlı: @{username} (id {id})." },
  "settings.tg.test_failed": { en: "Test failed: {error}", tr: "Test başarısız: {error}" },
  "settings.tg.scan_hint": {
    en: "Don't know the chat ID? Send /start to your bot, then scan — pick the chat to allow with one click.",
    tr: "Sohbet ID'sini bilmiyor musun? Bota /start gönder, sonra tara — izin vereceğin sohbeti tek tıkla seç.",
  },
  "settings.tg.scan":      { en: "Scan for chats", tr: "Sohbetleri tara" },
  "settings.tg.scanning":  { en: "Scanning…", tr: "Taranıyor…" },
  "settings.tg.scan_found": { en: "Found {count} chat(s).", tr: "{count} sohbet bulundu." },
  "settings.tg.scan_empty": {
    en: "No chats yet. Send /start (or any message) to your bot, then scan again.",
    tr: "Henüz sohbet yok. Bota /start (ya da herhangi bir mesaj) gönder, sonra tekrar tara.",
  },
  "settings.tg.scan_failed": { en: "Scan failed: {error}", tr: "Tarama başarısız: {error}" },
  "settings.tg.add":       { en: "Allow", tr: "İzin ver" },
  "settings.tg.already_allowed": { en: "Allowed", tr: "İzinli" },

  // ── Telegram: comprehensive setup explainer (collapsible help) ─────────────
  "settings.tg.guide_title": {
    en: "How to connect Telegram (step by step)",
    tr: "Telegram nasıl bağlanır (adım adım)",
  },
  // Body — dictionary-authored markup (rendered as HTML via data-i18n-html).
  "settings.tg.guide_html": {
    en:
      "<p>Connecting Telegram lets you chat with Akana from the Telegram app — text it from your phone and it answers with the active model.</p>" +
      "<ol class=\"conn-help-steps\">" +
      "<li><strong>Create a bot.</strong> In Telegram, open a chat with <strong>@BotFather</strong>, send <code>/newbot</code>, follow the prompts, and copy the <strong>bot token</strong> it gives you (looks like <code>123456:ABC-DEF…</code>).</li>" +
      "<li><strong>Save the token here.</strong> Paste it into <em>Bot token</em> above, press <em>Save token</em>, then turn on <em>Telegram bridge enabled</em>. The token is stored encrypted on the server.</li>" +
      "<li><strong>Allow your chat.</strong> Open your bot in Telegram and send it any message (e.g. <code>/start</code>). Back here, press <em>Scan for chats</em>, then <em>Allow</em> the chat you want. No message yet? You can also paste the numeric chat ID into <em>Allowed chat IDs</em>.</li>" +
      "<li><strong>Talk to it.</strong> Message the bot from Telegram — Akana replies there with the model selected in the Provider tab.</li>" +
      "</ol>" +
      "<p><strong>How it maps to a conversation:</strong> each allowed Telegram chat binds to its own Akana conversation, kept separate from the web chat. History for that Telegram chat is remembered on its own thread, so a follow-up message continues where you left off.</p>" +
      "<p><strong>Security:</strong> only the chats you allow can reach Akana. If the allow-list is empty, nobody can write to the bot. Remove a chat ID to revoke its access.</p>",
    tr:
      "<p>Telegram'ı bağlamak, Akana ile Telegram uygulamasından sohbet etmeni sağlar — telefondan yaz, etkin modelle yanıtlasın.</p>" +
      "<ol class=\"conn-help-steps\">" +
      "<li><strong>Bot oluştur.</strong> Telegram'da <strong>@BotFather</strong> ile sohbet aç, <code>/newbot</code> gönder, adımları izle ve verilen <strong>bot token</strong>'ını kopyala (<code>123456:ABC-DEF…</code> gibi görünür).</li>" +
      "<li><strong>Token'ı buraya kaydet.</strong> Yukarıdaki <em>Bot token</em> alanına yapıştır, <em>Token'ı kaydet</em>'e bas, sonra <em>Telegram köprüsü etkin</em>'i aç. Token sunucuda şifreli saklanır.</li>" +
      "<li><strong>Sohbetine izin ver.</strong> Botunu Telegram'da açıp herhangi bir mesaj gönder (ör. <code>/start</code>). Buraya dönüp <em>Sohbetleri tara</em>'ya bas, istediğin sohbete <em>İzin ver</em>. Henüz mesaj yok mu? Sayısal sohbet ID'sini <em>İzinli sohbet ID'leri</em> alanına da yapıştırabilirsin.</li>" +
      "<li><strong>Konuş.</strong> Bota Telegram'dan yaz — Akana, Sağlayıcı sekmesinde seçili modelle orada yanıtlar.</li>" +
      "</ol>" +
      "<p><strong>Sohbetle nasıl eşleşir:</strong> izin verilen her Telegram sohbeti kendi Akana konuşmasına bağlanır ve web sohbetinden ayrı tutulur. O Telegram sohbetinin geçmişi kendi başlığında saklanır; sonraki mesaj kaldığın yerden devam eder.</p>" +
      "<p><strong>Güvenlik:</strong> yalnızca izin verdiğin sohbetler Akana'ya erişebilir. İzin listesi boşsa bota kimse yazamaz. Erişimi kaldırmak için sohbet ID'sini sil.</p>",
  },

  // ── Tailscale remote-access card (GET/POST /system/tailscale) ──────────────
  "settings.ts.title": { en: "Tailscale remote access", tr: "Tailscale uzaktan erişim" },
  "settings.ts.desc": {
    en: "Reach Akana from your phone or another device over your private tailnet — no port-forwarding.",
    tr: "Akana'ya telefonundan veya başka bir cihazdan özel tailnet üzerinden eriş — port yönlendirme yok.",
  },
  "settings.ts.load_failed": { en: "Tailscale status unavailable: {error}", tr: "Tailscale durumu alınamadı: {error}" },
  "settings.ts.state.not_installed": { en: "Not installed", tr: "Kurulu değil" },
  "settings.ts.state.logged_out": { en: "Installed, not logged in", tr: "Kurulu, giriş yapılmadı" },
  "settings.ts.state.ready": { en: "Connected", tr: "Bağlı" },
  "settings.ts.state.serving": { en: "Serving (tailnet)", tr: "Yayında (tailnet)" },
  "settings.ts.state.funnel": { en: "Public (Funnel)", tr: "Herkese açık (Funnel)" },
  "settings.ts.install_hint": {
    en: "Tailscale is not installed on this machine. Install it, then reopen this panel.",
    tr: "Bu makinede Tailscale kurulu değil. Kur, sonra bu paneli yeniden aç.",
  },
  "settings.ts.install_link": { en: "Download Tailscale", tr: "Tailscale'i indir" },
  "settings.ts.login_hint": {
    en: "Tailscale is installed but not logged in. Run `tailscale up` in a terminal, then refresh.",
    tr: "Tailscale kurulu ama giriş yapılmamış. Terminalde `tailscale up` çalıştır, sonra yenile.",
  },
  "settings.ts.mode_label": { en: "Exposure", tr: "Erişim modu" },
  "settings.ts.mode_off": { en: "Off", tr: "Kapalı" },
  "settings.ts.mode_serve": { en: "Tailnet only (private)", tr: "Sadece tailnet (özel)" },
  "settings.ts.mode_funnel": { en: "Public internet (Funnel)", tr: "Herkese açık internet (Funnel)" },
  "settings.ts.url_label": { en: "Tailnet address", tr: "Tailnet adresi" },
  "settings.ts.copy_url": { en: "Copy", tr: "Kopyala" },
  "settings.ts.url_copied": { en: "Address copied.", tr: "Adres kopyalandı." },
  "settings.ts.qr_hint": {
    en: "Scan with your phone to open Akana with the access key already applied.",
    tr: "Erişim anahtarı hazır şekilde Akana'yı açmak için telefonunla tara.",
  },
  "settings.ts.qr_no_token": {
    en: "Set an access token in the API token field above to generate a phone QR.",
    tr: "Telefon QR'ı oluşturmak için yukarıdaki API token alanından bir erişim anahtarı ayarla.",
  },
  "settings.ts.funnel_warning": {
    en: "⚠ Funnel publishes this instance on the PUBLIC internet. Anyone with the URL can reach the login — keep your access token strong.",
    tr: "⚠ Funnel bu örneği HERKESE AÇIK internette yayınlar. URL'yi bilen herkes giriş ekranına ulaşır — erişim anahtarını güçlü tut.",
  },
  "settings.ts.funnel_needs_token": {
    en: "Funnel is disabled because no access token is set. Add one in the API token field above first.",
    tr: "Erişim anahtarı ayarlı olmadığı için Funnel devre dışı. Önce yukarıdaki API token alanından bir tane ekle.",
  },
  "settings.ts.applying": { en: "Applying…", tr: "Uygulanıyor…" },
  "settings.ts.applied": { en: "Applied — {mode}.", tr: "Uygulandı — {mode}." },
  "settings.ts.apply_failed": { en: "Could not apply: {error}", tr: "Uygulanamadı: {error}" },
  "settings.ts.refresh": { en: "Refresh", tr: "Yenile" },

  // ── Connection save / test ────────────────────────────────────────────────
  "settings.save.saved": { en: "Settings saved.", tr: "Ayarlar kaydedildi." },
  "settings.conn.testing": { en: "Testing…", tr: "Test ediliyor…" },
  // Provider-neutral success (server reachable). The cursor-only wording was a
  // leftover from the single-provider era — Akana now supports several providers.
  "settings.conn.ok":   { en: "Connection successful.", tr: "Bağlantı başarılı." },
  // Same, but names the active provider when its live probe reports reachable.
  "settings.conn.ok_provider": {
    en: "Connection successful — {provider} reachable.",
    tr: "Bağlantı başarılı — {provider} erişilebilir.",
  },
  // Server answered but the active provider's key/credentials look unset.
  "settings.conn.up_no_provider": {
    en: "Akana is up, but the active provider ({provider}) is not reachable — check its key in the Credentials tab.",
    tr: "Akana ayakta ama etkin sağlayıcı ({provider}) erişilemiyor — anahtarını Kimlik sekmesinden kontrol edin.",
  },
  "settings.conn.error": { en: "Connection error: {error}", tr: "Bağlantı hatası: {error}" },
  "settings.conn.ws_reconnecting": { en: "WebSocket reconnecting…", tr: "WebSocket yeniden bağlanıyor…" },
  "settings.conn.copied": { en: "Address copied to clipboard.", tr: "Adres panoya kopyalandı." },
  "settings.conn.copy_failed": { en: "Copy failed.", tr: "Kopyalama başarısız." },

  // ── Connection: API token + Tailscale explainer (collapsible help) ─────────
  // What the API token is for — shown as a hint under the token field.
  "settings.conn.token_help_html": {
    en: "The API token gates who may talk to this server. It is the <code>AKANA_TOKEN</code> value from your <code>.env</code> file — the browser sends it with every request. Leave it blank only if the server was started with tokens disabled. On another device (e.g. your phone) enter the same value here.",
    tr: "API token, bu sunucuyla kimin konuşabileceğini belirler. <code>.env</code> dosyanızdaki <code>AKANA_TOKEN</code> değeridir — tarayıcı her istekte gönderir. Yalnızca sunucu token'lar kapalı başlatıldıysa boş bırakın. Başka bir cihazda (ör. telefon) buraya aynı değeri girin.",
  },
  // Collapsible explainer title — sits under the live Tailscale card in the
  // Connection pane, so it reads as "learn more / manual setup", not a competing header.
  "settings.conn.tailscale_title": {
    en: "How Tailscale works & manual setup",
    tr: "Tailscale nasıl çalışır & elle kurulum",
  },
  // The body of the explainer — dictionary-authored markup (rendered as HTML).
  "settings.conn.tailscale_html": {
    en:
      "<p>Akana runs locally on this machine — it is not published to the public internet. " +
      "<strong>Tailscale</strong> is a private, encrypted network (a personal VPN) that lets your phone and other devices reach this machine directly, as if they were on the same home network — no port-forwarding or exposing the server.</p>" +
      "<p class=\"conn-help-steps-label\">One-time setup:</p>" +
      "<ol class=\"conn-help-steps\">" +
      "<li>Install Tailscale on <strong>this machine</strong> and on <strong>your phone</strong>, then sign in to the <strong>same account</strong> on both.</li>" +
      "<li>Find this machine's Tailscale address — its name (e.g. <code>your-host.tailnet.ts.net</code>) or its <code>100.x.y.z</code> IP, shown in the Tailscale app.</li>" +
      "<li>On the phone's browser open <code>http://&lt;tailscale-address&gt;:&lt;port&gt;</code> (the port is shown in the effective endpoint above).</li>" +
      "<li>When asked to authenticate, enter the <strong>API token</strong> from the field above.</li>" +
      "<li>Tip: from the browser menu choose <em>Add to Home Screen</em> so Akana opens like an app.</li>" +
      "</ol>" +
      "<p>Prefer not to type the token on the phone? Use <strong>Connect your phone</strong> below to scan a QR that loads it automatically.</p>",
    tr:
      "<p>Akana bu makinede yerel çalışır — genel internete açılmaz. " +
      "<strong>Tailscale</strong>, telefonunuzun ve diğer cihazların bu makineye — aynı ev ağındaymış gibi — doğrudan erişmesini sağlayan özel, şifreli bir ağdır (kişisel VPN); port yönlendirme ya da sunucuyu dışarı açma gerekmez.</p>" +
      "<p class=\"conn-help-steps-label\">Tek seferlik kurulum:</p>" +
      "<ol class=\"conn-help-steps\">" +
      "<li>Tailscale'i <strong>bu makineye</strong> ve <strong>telefonunuza</strong> kurun, ardından her ikisinde de <strong>aynı hesaba</strong> giriş yapın.</li>" +
      "<li>Bu makinenin Tailscale adresini bulun — adı (ör. <code>your-host.tailnet.ts.net</code>) veya <code>100.x.y.z</code> IP'si; Tailscale uygulamasında görünür.</li>" +
      "<li>Telefonun tarayıcısında <code>http://&lt;tailscale-adresi&gt;:&lt;port&gt;</code> açın (port yukarıdaki etkin uç noktada yazar).</li>" +
      "<li>Kimlik sorulduğunda yukarıdaki alandaki <strong>API token</strong>'ı girin.</li>" +
      "<li>İpucu: tarayıcı menüsünden <em>Ana ekrana ekle</em>'yi seçin; böylece Akana uygulama gibi açılır.</li>" +
      "</ol>" +
      "<p>Telefonda token yazmak istemiyor musunuz? Aşağıdaki <strong>Telefonu bağla</strong> ile QR'ı tarayın, token otomatik yüklenir.</p>",
  },

  // ── WS connect status texts ───────────────────────────────────────────────
  "settings.ws.connecting_label": { en: "WS connecting…", tr: "WS bağlanıyor…" },
  "settings.ws.connected_label":  { en: "WS connected",   tr: "WS bağlı" },
  "settings.ws.closed_label":     { en: "WS closed",      tr: "WS kapalı" },
  "settings.ws.error_label":      { en: "WS error",       tr: "WS hata" },

  // ── WS task notifications ─────────────────────────────────────────────────
  "settings.ws.task_paused":    { en: "paused",    tr: "duraklatıldı" },
  "settings.ws.task_cancelled": { en: "cancelled", tr: "iptal edildi" },
  "settings.ws.task_aborted":   { en: "aborted",   tr: "yarıda kesildi" },
  "settings.ws.task_failed":    { en: "failed",    tr: "başarısız oldu" },
  "settings.ws.task_toast":     { en: "Task {status}: {title}", tr: "Görev {status}: {title}" },
  "settings.ws.reminder_toast": { en: "⏰ Reminder: {text}", tr: "⏰ Hatırlatma: {text}" },
  "settings.ws.policy_blocked": {
    en: "Policy blocked: {action}{rationale}",
    tr: "Politika engelledi: {action}{rationale}",
  },

  // ── Voice settings ────────────────────────────────────────────────────────
  "settings.voice.mic_default":      { en: "System default", tr: "Sistem varsayılanı" },
  "settings.voice.wake_toast": {
    en: "Microphone permission required for Hey Akana — click once or press the headset button.",
    tr: "Hey Akana için mikrofon izni gerekli — bir kez tıklayın veya kulaklık düğmesine basın.",
  },
  // NOTE: "settings.voice.stt_browser" is intentionally NOT defined here — the
  // voice-capability chip uses "voicecfg.chip.stt_browser" instead. (The old hybrid-STT
  // <select> that consumed a settings.voice.stt_browser option label was removed.)
  "settings.voice.setup_banner": {
    en: "{needs} required — configurable from Settings › Voice. Browser wake still works.",
    tr: "{needs} gerekiyor — Ayarlar › Ses'ten kurabilirsin. Tarayıcı wake yine çalışır.",
  },
  "settings.voice.wake_hint_server": {
    en: "Server scoring active ({model}). Browser «Hey Akana» always works.",
    tr: "Sunucu skoru aktif ({model}). Tarayıcı «Hey Akana» her zaman çalışır.",
  },
  "settings.voice.wake_hint_browser": {
    en: "If you get 503 run in terminal: make setup-full — then restart server. Browser recognition still works.",
    tr: "503 alıyorsan terminalde: make setup-full — ardından sunucuyu yeniden başlat. Tarayıcı tanıma yine çalışır.",
  },

  // ── Packs panel ───────────────────────────────────────────────────────────
  "pack.callout": {
    en: "A pack bundles skills, personas and the external tools they need. Disabling a pack hot-removes its content from the assistant — including its skills from the capability catalog. The source folder under <code>packs/</code> is never touched, so it is fully reversible. To install a new pack, drop its folder into <code>packs/</code> and press <strong>Refresh</strong>.",
    tr: "Bir pack; beceri, persona ve ihtiyaç duydukları dış araçları bir arada sunar. Bir pack'i kapatmak içeriğini asistandan anında kaldırır — becerilerini yetenek kataloğundan da düşürür. <code>packs/</code> altındaki kaynak klasöre dokunulmaz, yani tamamen geri alınabilir. Yeni pack kurmak için klasörünü <code>packs/</code> içine koyup <strong>Yenile</strong>'ye basın.",
  },
  "pack.refresh_btn": { en: "Refresh", tr: "Yenile" },
  "pack.search_ph":   { en: "Search packs…", tr: "Pack ara…" },
  "pack.search_empty": { en: "No packs match your search.", tr: "Aramayla eşleşen pack yok." },
  "pack.detail.skills":   { en: "Skills", tr: "Beceriler" },
  "pack.detail.personas": { en: "Personas", tr: "Personalar" },
  "pack.detail.tools":    { en: "Tools", tr: "Araçlar" },
  "pack.state.enabled":  { en: "enabled",  tr: "açık" },
  "pack.state.disabled": { en: "disabled", tr: "kapalı" },
  "pack.state.needs_consent": { en: "needs approval", tr: "onay bekliyor" },
  "pack.toggle.enable":  { en: "Enable",  tr: "Aç" },
  "pack.toggle.disable": { en: "Disable", tr: "Kapat" },
  "pack.consent.approve": { en: "Approve MCP", tr: "MCP'yi onayla" },
  "pack.consent.pending_note": {
    en: "This pack's tools need your approval before it can use them: {servers}.",
    tr: "Bu pack'in araçlarını kullanabilmesi için onayın gerekiyor: {servers}.",
  },
  "pack.status.consented": { en: "Approved: {servers}", tr: "Onaylandı: {servers}" },
  "pack.status.consent_pending": {
    en: "Still needs configuration — nothing was mounted.",
    tr: "Hâlâ yapılandırma gerekiyor — hiçbir şey bağlanmadı.",
  },
  "pack.contains.skills":   { en: "skills",   tr: "beceri" },
  "pack.contains.personas": { en: "personas", tr: "persona" },
  "pack.contains.tools":    { en: "tools",    tr: "araç" },
  "pack.contains.empty":    { en: "no content", tr: "içerik yok" },
  "pack.missing_tools_title": { en: "Missing tools:", tr: "Eksik araçlar:" },
  "pack.catalog_note": {
    en: "Disabling also removes {n} skill(s) from the capability catalog.",
    tr: "Kapatınca {n} beceri yetenek kataloğundan da düşer.",
  },
  "pack.list.empty":  { en: "No packs found.", tr: "Pack bulunamadı." },
  "pack.load_failed": { en: "Could not load packs: {error}", tr: "Pack'ler yüklenemedi: {error}" },
  "pack.status.enabled":  { en: "Enabled: {id}",  tr: "Açıldı: {id}" },
  "pack.status.disabled": {
    en: "Disabled: {id} — takes effect from your next message; a reply already in progress keeps its current tools.",
    tr: "Kapatıldı: {id} — bir sonraki mesajından itibaren geçerli; hâlihazırda süren bir yanıt mevcut araçlarını korur.",
  },
  "pack.status.rescan_found": { en: "{n} new pack(s) found.", tr: "{n} yeni pack bulundu." },
  "pack.status.rescan_removed": { en: "{n} pack(s) removed.", tr: "{n} pack kaldırıldı." },
  "pack.status.rescan_changed": { en: "{added} added, {removed} removed.", tr: "{added} eklendi, {removed} kaldırıldı." },
  "pack.status.rescan_none":  { en: "No changes.", tr: "Değişiklik yok." },

  // ── Persona panel ─────────────────────────────────────────────────────────
  "persona.badge.builtin": { en: "built-in", tr: "yerleşik" },
  "persona.badge.pack":    { en: "pack",     tr: "pack" },
  "persona.badge.user":    { en: "custom",   tr: "özel" },
  "persona.card.default_star": { en: "★ default", tr: "★ varsayılan" },
  "persona.card.set_default":  { en: "Set as default", tr: "Varsayılan yap" },
  "persona.card.edit":         { en: "Edit",           tr: "Düzenle" },
  "persona.card.delete":       { en: "Delete",         tr: "Sil" },
  "persona.card.fork":         { en: "Copy & edit",    tr: "Kopyala &amp; düzenle" },
  "persona.card.prompt_summary": { en: "system prompt", tr: "sistem promptu" },
  "persona.card.tone":         { en: "tone: {tone}", tr: "ton: {tone}" },
  "persona.callout": {
    en: "System prompt = <strong>core</strong> (below) + selected persona text + <strong>capability catalog</strong> (at the bottom). All three are edited here.",
    tr: "Sistem promptu = <strong>çekirdek</strong> (aşağıda) + seçili persona metni + <strong>yetenek kataloğu</strong> (en altta). Üçü de buradan düzenlenir.",
  },
  "persona.base.title": { en: "Core system prompt", tr: "Çekirdek sistem promptu" },
  "persona.base.badge_edited":  { en: "edited",  tr: "düzenlendi" },
  "persona.base.badge_default": { en: "default", tr: "varsayılan" },
  "persona.base.hint": {
    en: "Default Akana identity + language lock + behaviour rules. <strong>If a custom persona is set as default its text REPLACES this one</strong> — to prevent identity leaks (e.g. \"Claude Code\") fork the custom persona from akana via «Copy &amp; edit».",
    tr: "Varsayılan Akana kimliği + dil kilidi + davranış kuralları. <strong>Özel bir persona varsayılansa onun metni bunun YERİNE geçer</strong> — kimlik sızıntısını (ör. \"Claude Code\") önlemek için özel personayı akana'dan «Kopyala &amp; düzenle» ile çıkar.",
  },
  "persona.base.save_btn":   { en: "Save core", tr: "Çekirdeği kaydet" },
  "persona.base.reset_btn":  { en: "Reset to default", tr: "Varsayılana sıfırla" },
  "persona.voice.title": { en: "Voice-mode directive", tr: "Sesli mod direktifi" },
  "persona.voice.badge_edited":  { en: "edited",  tr: "düzenlendi" },
  "persona.voice.badge_default": { en: "default", tr: "varsayılan" },
  "persona.voice.hint": {
    en: "Injected on top of the persona for voice turns (short, markdown-free spoken replies). Bilingual: the default follows your language — English in English, Turkish in Turkish. Edit to change how Akana talks in voice.",
    tr: "Sesli turlarda personanın üzerine eklenir (kısa, markdown'sız sesli yanıtlar). İki dilli: varsayılan diline uyar — İngilizce'de İngilizce, Türkçe'de Türkçe. Akana'nın seste nasıl konuştuğunu değiştirmek için düzenle.",
  },
  "persona.voice.save_btn":   { en: "Save voice directive", tr: "Sesli direktifi kaydet" },
  "persona.voice.reset_btn":  { en: "Reset to default", tr: "Varsayılana sıfırla" },
  "persona.form.title_new":  { en: "New persona",  tr: "Yeni persona" },
  "persona.form.title_edit": { en: "Edit persona", tr: "Personayı düzenle" },
  "persona.form.name_label": { en: "Name", tr: "Ad" },
  "persona.form.name_ph":    { en: "e.g. Formal Akana", tr: "örn. Resmî Akana" },
  "persona.form.prompt_label": { en: "System prompt", tr: "Sistem promptu" },
  "persona.form.prompt_ph": {
    en: "Tell the assistant who it is and how to speak…",
    tr: "Asistana kim olduğunu, nasıl konuşacağını anlat…",
  },
  "persona.form.tone_label":    { en: "Tone", tr: "Ton" },
  "persona.form.tone_optional": { en: "optional", tr: "opsiyonel" },
  "persona.form.tone_ph":       { en: "e.g. short, formal, witty", tr: "örn. kısa, resmî, esprili" },
  "persona.form.save_btn":      { en: "Save",   tr: "Kaydet" },
  "persona.form.cancel_btn":    { en: "Cancel", tr: "İptal" },
  "persona.list.title":         { en: "Personas", tr: "Personalar" },
  "persona.list.empty":         { en: "No personas yet.", tr: "Henüz persona yok." },
  "persona.list.refresh":       { en: "Refresh", tr: "Yenile" },
  "persona.catalog.title":      { en: "Capability catalog", tr: "Yetenek kataloğu" },
  "persona.catalog.hint": {
    en: "The inventory of installed skills is added to the system prompt each turn (\"Can you do X?\" is answered based on this). Check which to include.",
    tr: "Kurulu skill'lerin envanteri her turun system promptuna eklenir («X yapabilir misin?» buna göre yanıtlanır). Hangileri dahil olsun işaretle.",
  },
  "persona.catalog.toggle_label": { en: "Add to system prompt", tr: "Sistem promptuna ekle" },
  "persona.catalog.all_btn":     { en: "All",  tr: "Hepsi" },
  "persona.catalog.none_btn":    { en: "None", tr: "Hiçbiri" },
  "persona.catalog.save_btn":    { en: "Save selection", tr: "Seçimi kaydet" },
  "persona.catalog.reset_btn":   { en: "Include all (auto)", tr: "Hepsini dahil et (oto)" },
  "persona.catalog.empty":       { en: "No installed skills.", tr: "Kurulu skill yok." },
  "persona.status.name_required":   { en: "name and system prompt required", tr: "ad ve sistem promptu gerekli" },
  "persona.status.updated":         { en: "«{name}» updated", tr: "«{name}» güncellendi" },
  "persona.status.created":         { en: "«{name}» created", tr: "«{name}» oluşturuldu" },
  "persona.status.fork_ready":      { en: "copy form filled — edit and Save", tr: "kopya formu dolduruldu — düzenleyip Kaydet" },
  "persona.status.deleted":         { en: "«{name}» deleted", tr: "«{name}» silindi" },
  "persona.status.activated":       { en: "Default persona set", tr: "varsayılan persona ayarlandı" },
  "persona.status.base_empty":      { en: "core prompt cannot be empty", tr: "çekirdek prompt boş olamaz" },
  "persona.status.base_saved":      { en: "core prompt saved", tr: "çekirdek prompt kaydedildi" },
  "persona.status.base_reset":      { en: "reset to default", tr: "varsayılana sıfırlandı" },
  "persona.status.voice_empty":     { en: "voice directive cannot be empty", tr: "sesli direktif boş olamaz" },
  "persona.status.voice_saved":     { en: "voice directive saved", tr: "sesli direktif kaydedildi" },
  "persona.status.voice_reset":     { en: "reset to default", tr: "varsayılana sıfırlandı" },
  "persona.status.catalog_saved":   { en: "selection saved ({n} skills)", tr: "seçim kaydedildi ({n} yetenek)" },
  "persona.status.catalog_reset":   { en: "all included (auto)", tr: "hepsi dahil (otomatik)" },
  "persona.status.catalog_on":      { en: "catalog enabled", tr: "katalog açık" },
  "persona.status.catalog_off":     { en: "catalog disabled", tr: "katalog kapalı" },
  "persona.status.catalog_toggle_fail": { en: "toggle could not be saved", tr: "toggle kaydedilemedi" },
  "persona.load_failed":            { en: "Personas could not be loaded: {error}", tr: "Personalar yüklenemedi: {error}" },
  "persona.confirm.delete":         { en: "Delete persona «{name}»?", tr: "«{name}» personasını sil?" },
  "persona.confirm.reset_base":     { en: "Reset core prompt to code default?", tr: "Çekirdek promptu kod varsayılanına sıfırla?" },
  "persona.confirm.reset_voice":    { en: "Reset voice directive to code default?", tr: "Sesli direktifi kod varsayılanına sıfırla?" },

  // ── Vault panel ───────────────────────────────────────────────────────────
  "vault.enc.unavailable": {
    en: "⚠ Encryption OFF — values stored as plain text (cryptography package not found).",
    tr: "⚠ Şifreleme KAPALI — değerler düz metin saklanıyor (cryptography paketi bulunamadı).",
  },
  "vault.enc.broken": {
    en: "⚠ Some encrypted records could not be decrypted — wrong/missing master key (source: {source}). Vault may appear empty; restore the key, do not re-save.",
    tr: "⚠ Bazı şifreli kayıtlar açılamadı — yanlış/eksik master anahtar (kaynak: {source}). Kasa boş görünebilir; anahtarı geri yükleyin, yeniden kaydetmeyin.",
  },
  "vault.callout": {
    en: "Values are stored <strong>encrypted</strong> on the server and shown masked. Click <strong>Show</strong> to reveal a value on demand — every reveal is logged to the audit trail. Saving an empty value deletes that entry.",
    tr: "Değerler sunucuda <strong>şifreli</strong> saklanır ve maskeli gösterilir. Bir değeri görmek için <strong>Göster</strong>'e basın — her görüntüleme denetim günlüğüne işlenir. Boş değer kaydetmek o kaydı siler.",
  },
  "vault.accounts.title": { en: "Account credentials", tr: "Hesap kimlikleri" },
  "vault.accounts.hint": {
    en: "E.g. <code>reddit / default</code> → <code>username</code>, <code>password</code>. Packs read these values at runtime.",
    tr: "Örn. <code>reddit / default</code> → <code>username</code>, <code>password</code>. Pack'ler çalışırken bu değerleri buradan çeker.",
  },
  "vault.accounts.ns_ph":      { en: "namespace (e.g. reddit)", tr: "namespace (örn. reddit)" },
  "vault.accounts.profile_ph": { en: "profile", tr: "profile" },
  "vault.accounts.key_ph":     { en: "field (e.g. password)", tr: "alan (örn. password)" },
  "vault.accounts.val_ph":     { en: "value", tr: "değer" },
  "vault.accounts.save_btn":   { en: "Save", tr: "Kaydet" },
  "vault.accounts.empty":      { en: "No account records yet.", tr: "Henüz hesap kaydı yok." },
  "vault.scalars.title":       { en: "Single secrets", tr: "Tekil sırlar" },
  "vault.scalars.hint": {
    en: "One key → one value: any standalone secret such as an API key or token (e.g. <code>gemini_api_key</code>). Provider keys (Cursor/Claude) are managed in the <strong>Credentials</strong> tab.",
    tr: "Bir anahtar → bir değer: API anahtarı, token vb. tekil herhangi bir sır (örn. <code>gemini_api_key</code>). Sağlayıcı anahtarları (Cursor/Claude) <strong>Kimlik</strong> sekmesinden yönetilir.",
  },
  "vault.scalars.key_ph":  { en: "key name",  tr: "anahtar adı" },
  "vault.scalars.val_ph":  { en: "value",     tr: "değer" },
  "vault.scalars.save_btn":{ en: "Save",      tr: "Kaydet" },
  "vault.row.reveal_btn":  { en: "Show",      tr: "Göster" },
  "vault.row.hide_btn":    { en: "Hide",      tr: "Gizle" },
  "vault.row.delete_btn":  { en: "Delete",    tr: "Sil" },
  "vault.row.empty":       { en: "— empty —", tr: "— boş —" },
  "vault.group.delete_btn":{ en: "Delete profile", tr: "Profili sil" },
  "vault.refresh_btn":     { en: "Refresh",   tr: "Yenile" },
  "vault.status.key_val_required":  { en: "key and value required", tr: "anahtar ve değer gerekli" },
  "vault.status.scalar_saved":      { en: "«{key}» saved", tr: "«{key}» kaydedildi" },
  "vault.status.scalar_deleted":    { en: "«{key}» deleted", tr: "«{key}» silindi" },
  "vault.status.ns_key_val_required": { en: "namespace, field and value required", tr: "namespace, alan ve değer gerekli" },
  "vault.status.field_saved":       { en: "{ns}/{profile} · «{key}» saved", tr: "{ns}/{profile} · «{key}» kaydedildi" },
  "vault.status.field_deleted":     { en: "«{key}» deleted", tr: "«{key}» silindi" },
  "vault.status.profile_deleted":   { en: "{ns}/{profile} deleted", tr: "{ns}/{profile} silindi" },
  "vault.load_failed":              { en: "Vault could not be loaded: {error}", tr: "Vault yüklenemedi: {error}" },
  "vault.confirm.del_scalar":       { en: "Delete key «{key}»?", tr: "«{key}» anahtarını sil?" },
  "vault.confirm.del_field":        { en: "{ns}/{profile} · delete field «{key}»?", tr: "{ns}/{profile} · «{key}» alanını sil?" },
  "vault.confirm.del_profile":      { en: "{ns}/{profile} · delete the whole profile?", tr: "{ns}/{profile} · tüm profili sil?" },

  // ── Pair modal ────────────────────────────────────────────────────────────
  "pair.modal.title":      { en: "Connect your phone", tr: "Telefonu bağla" },
  "pair.modal.close_aria": { en: "Close", tr: "Kapat" },
  "pair.modal.desc": {
    en: "Scan this QR with your phone camera — token loads automatically, no typing needed.",
    tr: "Telefonun kamerasıyla bu QR'ı tarayın — token otomatik yüklenir, hiçbir şey yazmanıza gerek yok.",
  },
  "pair.modal.host_label": { en: "Tailscale address", tr: "Tailscale adresi" },
  "pair.modal.copy_btn":   { en: "Copy",   tr: "Kopyala" },
  "pair.modal.copy_title": { en: "Copy link", tr: "Bağlantıyı kopyala" },
  "pair.toast.no_token":   { en: "Set a token first", tr: "Önce token ayarlayın" },
  "pair.toast.no_server_token": { en: "Set an API access token (AKANA_TOKEN) to pair — Settings → Connection", tr: "Eşleştirmek için bir API erişim token'ı (AKANA_TOKEN) ayarlayın — Ayarlar → Bağlantı" },
  "pair.qr.failed": {
    en: "QR could not be generated — copy the link and paste it on your phone manually.",
    tr: "QR oluşturulamadı — bağlantıyı kopyalayıp telefona elle yapıştırın.",
  },
  "pair.status.copied":      { en: "Link copied to clipboard.", tr: "Bağlantı panoya kopyalandı." },
  "pair.status.copy_failed": {
    en: "Copy failed — select and copy the link manually.",
    tr: "Kopyalama başarısız — bağlantıyı elle seçip kopyalayın.",
  },
  "pair.status.no_token":  { en: "Set a token first.", tr: "Önce token ayarlayın." },
});
