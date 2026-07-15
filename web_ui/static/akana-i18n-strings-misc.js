/**
 * Akana UI i18n strings — MISC area (shell, mobile-nav, artifacts, cockpit,
 * aurora-ui/onboard, turn-status). Merges into window.AkanaI18nStrings.
 * { en, tr }, English-first. Keys: shell.* / nav.* / ui.* / onboard.*
 */
window.AkanaI18nStrings = Object.assign(window.AkanaI18nStrings || {}, {

  // ── akana-shell: greeting ────────────────────────────────────────────────
  "shell.greeting_default":   { en: "Hello, I'm Akana", tr: "Merhaba, ben Akana" },
  "shell.greeting_morning":   { en: "Good morning",   tr: "Günaydın" },
  "shell.greeting_afternoon": { en: "Good afternoon", tr: "İyi günler" },
  "shell.greeting_evening":   { en: "Good evening",   tr: "İyi akşamlar" },
  "shell.greeting_night":     { en: "Good night",     tr: "İyi geceler" },
  "shell.greeting_base":      { en: "— I'm Akana",    tr: "— ben Akana" },

  // ── akana-shell: scroll FAB ──────────────────────────────────────────────
  "shell.fab_scroll_down_aria": { en: "Scroll to bottom", tr: "En alta in" },

  // ── akana-shell: composer hint ───────────────────────────────────────────
  "shell.hint_listening": { en: "Listening — send when you stop talking, Esc: cancel", tr: "Dinliyor — konuşmayı bitirince gönderir, Esc: vazgeç" },
  "shell.hint_thinking":  { en: "Thinking…",   tr: "Düşünüyor…" },
  "shell.hint_speaking":  { en: "Speaking — Esc: stop", tr: "Sesli yanıt çalıyor — Esc: durdur" },

  // ── akana-shell: message row labels ─────────────────────────────────────
  "shell.msg_label_you":    { en: "You",    tr: "Sen" },
  "shell.msg_label_system": { en: "System", tr: "Sistem" },

  // ── akana-shell: attachments ─────────────────────────────────────────────
  "shell.attach_image_alt": { en: "Attachment image",  tr: "Ek görsel" },
  "shell.attach_file_chip": { en: "📄 Attachment file", tr: "📄 Ek dosya" },
  "shell.pdf_preview_alt":  { en: "PDF preview",        tr: "PDF önizleme" },

  // ── akana-shell: code copy ───────────────────────────────────────────────
  "shell.code_copy":       { en: "Copy",      tr: "Kopyala" },
  "shell.code_copy_aria":  { en: "Copy code block", tr: "Kod bloğunu kopyala" },
  "shell.code_copied":     { en: "Copied ✓",  tr: "Kopyalandı ✓" },

  // ── akana-shell: prompt suggestions ─────────────────────────────────────
  "shell.ps_morning_title":  { en: "Start the day",     tr: "Güne başla" },
  "shell.ps_morning_sub":    { en: "A quick plan for today", tr: "Bugün için kısa bir plan" },
  "shell.ps_morning_prompt": { en: "Give me a short, clear plan for today.", tr: "Bugün için bana kısa, net bir plan çıkar." },
  "shell.ps_plan_title":     { en: "Suggest a plan",    tr: "Plan öner" },
  "shell.ps_plan_sub":       { en: "Prioritise tasks",  tr: "Öncelikleri sırala" },
  "shell.ps_plan_prompt":    { en: "What do you recommend I do today?", tr: "Bugün ne yapmamı önerirsin?" },
  "shell.ps_system_title":   { en: "System summary",    tr: "Sistem özeti" },
  "shell.ps_system_sub":     { en: "Status at a glance", tr: "Durumu tek bakışta" },
  "shell.ps_system_prompt":  { en: "Summarise the system status.", tr: "Sistem durumunu özetle." },
  "shell.ps_evening_title":  { en: "Wrap up the day",   tr: "Günü topla" },
  "shell.ps_evening_sub":    { en: "What did we discuss today?", tr: "Bugün ne konuştuk?" },
  "shell.ps_evening_prompt": { en: "Briefly summarise what we talked about today.", tr: "Bugün konuştuklarımızı kısaca özetle." },
  "shell.ps_night_title":    { en: "Take a break",      tr: "Mola ver" },
  "shell.ps_night_sub":      { en: "A quick smile",     tr: "Kısa bir gülümseme" },
  "shell.ps_night_prompt":   { en: "Tell me a short, clever joke.", tr: "Kısa ve zekice bir şaka anlat." },
  "shell.ps_idea_title":     { en: "Give me ideas",     tr: "Fikir ver" },
  "shell.ps_idea_sub":       { en: "Where I'm stuck",   tr: "Takıldığım yerde" },
  "shell.ps_idea_prompt":    { en: "Give me three ideas about what I'm currently working on.", tr: "Şu an üzerinde çalıştığım konuya dair üç fikir ver." },
  "shell.ps_learn_title":    { en: "Teach me something", tr: "Bir şey öğret" },
  "shell.ps_learn_sub":      { en: "Things I'm curious about", tr: "Merak ettiklerim" },
  "shell.ps_learn_prompt":   { en: "Teach me something interesting and useful.", tr: "Bana ilginç ve yararlı bir şey öğret." },

  // ── akana-shell: short conv ID ───────────────────────────────────────────
  "shell.conv_id_none": { en: "none", tr: "yok" },

  // ── akana-mobile-nav ────────────────────────────────────────────────────
  "nav.mobile_aria":      { en: "Mobile navigation", tr: "Mobil gezinme" },
  "nav.tab_chat":         { en: "Chat",     tr: "Sohbet" },
  "nav.tab_chat_aria":    { en: "Chat",     tr: "Sohbet" },
  "nav.tab_voice":        { en: "Voice",    tr: "Ses" },
  "nav.tab_voice_aria":   { en: "Voice call", tr: "Sesli konuşma" },
  "nav.tab_memory":       { en: "Memory",   tr: "Hafıza" },
  "nav.tab_memory_aria":  { en: "Memory",   tr: "Hafıza" },
  "nav.tab_settings":     { en: "Settings", tr: "Ayarlar" },
  "nav.tab_settings_aria":{ en: "Settings", tr: "Ayarlar" },

  // ── akana-artifacts ──────────────────────────────────────────────────────
  "ui.artifact_preview_title": { en: "Preview",          tr: "Önizleme" },
  "ui.artifact_doc_title":     { en: "Document",         tr: "Belge" },
  "ui.artifact_label_suffix":  { en: " preview",         tr: " önizleme" },
  "ui.artifact_copied":        { en: "Copied ✓",         tr: "Kopyalandı ✓" },
  "ui.artifact_copy_failed":   { en: "Copy failed",      tr: "Kopyalanamadı" },

  // ── aurora-ui ────────────────────────────────────────────────────────────
  "ui.aurora_accent_label_prefix": { en: "Accent colour: ", tr: "Vurgu rengi: " },
  "ui.aurora_custom_swatch_aria":  { en: "Open custom theme studio", tr: "Özel tema stüdyosunu aç" },
  "ui.aurora_custom_label":        { en: "Custom",   tr: "Özel" },
  "ui.aurora_studio_title":        { en: "Custom theme studio", tr: "Özel tema stüdyosu" },
  "ui.aurora_studio_preview_title":{ en: "Akana",    tr: "Akana" },
  "ui.aurora_studio_primary_btn":  { en: "Primary",  tr: "Birincil" },
  "ui.aurora_studio_primary_field":{ en: "Primary colour", tr: "Ana renk" },
  "ui.aurora_studio_secondary_field":{ en: "Secondary colour (gradient)", tr: "İkincil renk (gradyan)" },
  "ui.aurora_studio_primary_aria":   { en: "Primary colour picker", tr: "Ana renk seçici" },
  "ui.aurora_studio_secondary_aria": { en: "Secondary colour picker", tr: "İkincil renk seçici" },
  "ui.aurora_studio_primary_hex_aria":   { en: "Primary hex code", tr: "Ana hex kodu" },
  "ui.aurora_studio_secondary_hex_aria": { en: "Secondary hex code", tr: "İkincil hex kodu" },
  "ui.aurora_studio_surprise":  { en: "Surprise",  tr: "Sürpriz" },
  "ui.aurora_studio_reset":     { en: "Reset",     tr: "Sıfırla" },
  "ui.aurora_aa_pass":          { en: "Accent text contrast meets WCAG AA", tr: "Aksan üstündeki metin kontrastı WCAG AA seviyesini karşılıyor" },
  "ui.aurora_aa_fail":          { en: "Low contrast — text on accent may be hard to read", tr: "Kontrast düşük — aksan üstündeki metin zor okunabilir" },
  "ui.aurora_seg_atmos_title":  { en: "Atmosphere",  tr: "Atmosfer" },
  "ui.aurora_seg_atmos_hint":   { en: "Background nebula density.", tr: "Arka plan nebula yoğunluğu." },
  "ui.aurora_seg_shape_title":  { en: "Shape",       tr: "Şekil" },
  "ui.aurora_seg_shape_hint":   { en: "Corner rounding / interface character.", tr: "Köşe yuvarlaklığı / arayüz karakteri." },
  "ui.aurora_seg_density_title":{ en: "Density",     tr: "Yoğunluk" },
  "ui.aurora_seg_density_hint": { en: "Chat spacing and bubble padding.", tr: "Sohbet aralığı ve balon dolgusu." },
  "ui.aurora_seg_calm":         { en: "Calm",        tr: "Sakin" },
  "ui.aurora_seg_balanced":     { en: "Balanced",    tr: "Dengeli" },
  "ui.aurora_seg_cinematic":    { en: "Cinematic",   tr: "Sinematik" },
  "ui.aurora_seg_soft":         { en: "Soft",        tr: "Yumuşak" },
  "ui.aurora_seg_sharp":        { en: "Sharp",       tr: "Keskin" },
  "ui.aurora_seg_spacious":     { en: "Spacious",    tr: "Ferah" },
  "ui.aurora_seg_compact":      { en: "Compact",     tr: "Sıkı" },

  // ── aurora-onboard ───────────────────────────────────────────────────────
  "onboard.modal_aria":       { en: "Welcome",       tr: "Karşılama" },
  "onboard.skip":             { en: "Skip",          tr: "Atla" },
  "onboard.next":             { en: "Continue",      tr: "Devam" },
  "onboard.start":            { en: "Get started",   tr: "Başla" },
  "onboard.step1_title":      { en: "Welcome to Akana", tr: "Akana'ya hoş geldin" },
  "onboard.step1_lead":       { en: "Let me get to know you in a few seconds — then we'll work together.", tr: "Birkaç saniyede seni tanıyayım — sonra hep birlikte çalışırız." },
  "onboard.usecase1_t":       { en: "System & development", tr: "Sistem & geliştirme" },
  "onboard.usecase1_d":       { en: "Terminal, files, tests, deploy — with transparent tool steps", tr: "Terminal, dosya, test, deploy — şeffaf araç adımlarıyla" },
  "onboard.usecase2_t":       { en: "Personal assistant", tr: "Kişisel asistan" },
  "onboard.usecase2_d":       { en: "Calendar, notes, reminders, daily plan", tr: "Takvim, notlar, hatırlatıcılar, günlük plan" },
  "onboard.usecase3_t":       { en: "Writing & content", tr: "Yazım & içerik" },
  "onboard.usecase3_d":       { en: "Drafts, edits, social media packs", tr: "Taslak, düzeltme, sosyal medya pack'leri" },
  "onboard.theme_label":      { en: "Theme",   tr: "Tema" },
  "onboard.accent_label":     { en: "Accent",  tr: "Vurgu" },
  "onboard.theme_light":      { en: "Light",   tr: "Aydınlık" },
  "onboard.theme_dark":       { en: "Dark",    tr: "Koyu" },
  // Accent colour names — plain colour words, localized so the picker isn't half
  // English inside a Turkish personalize step ("Vurgu": Gök mavisi / Mor / …).
  "onboard.accent_azure":     { en: "Azure",   tr: "Gök mavisi" },
  "onboard.accent_violet":    { en: "Violet",  tr: "Mor" },
  "onboard.accent_teal":      { en: "Teal",    tr: "Turkuaz" },
  "onboard.accent_emerald":   { en: "Emerald", tr: "Zümrüt" },
  "onboard.accent_sunset":    { en: "Sunset",  tr: "Gün batımı" },
  "onboard.back":             { en: "Back",    tr: "Geri" },

  // ── 6-step flow: welcome (with an honest data note) ──────────────────────
  "onboard.welcome_f1_t":     { en: "Yours to run", tr: "Sen çalıştırırsın" },
  "onboard.welcome_f1_d":     { en: "You self-host Akana — it runs on your own machine, and you bring your own model.", tr: "Akana'yı kendin barındırırsın — kendi makinende çalışır ve modelini sen getirirsin." },
  "onboard.welcome_f2_t":     { en: "Remembers what matters", tr: "Önemli olanı hatırlar" },
  "onboard.welcome_f2_d":     { en: "It learns your preferences and recalls them when useful.", tr: "Tercihlerini öğrenir, gerektiğinde hatırlar." },
  "onboard.welcome_f3_t":     { en: "Your choice of model", tr: "Modeli sen seçersin" },
  "onboard.welcome_f3_d":     { en: "Use Cursor, Claude, and more — switch any time without redoing your setup.", tr: "Cursor, Claude ve daha fazlasını kullan — kurulumunu yeniden yapmadan istediğin zaman değiştir." },
  "onboard.welcome_data_note": { en: "Your chats, memory, and keys are stored locally. When you chat with a cloud model, that turn is sent to its provider to write the reply — choose Ollama to stay fully offline.", tr: "Sohbetlerin, hafızan ve anahtarların yerelde saklanır. Bir bulut modeliyle konuştuğunda o tur, yanıtı yazması için sağlayıcısına gönderilir — tamamen çevrimdışı kalmak için Ollama'yı seç." },

  // ── inside step: consolidated tour (memory · vault · packs · personas ·
  //    connectors · voice) — neutral, factual per-feature blurbs (no hype) ────
  "onboard.inside_title":     { en: "What's inside Akana", tr: "Akana'nın içinde ne var" },
  "onboard.inside_lead":      { en: "A quick tour of the main features — you can explore each later from the top bar or Settings. None of it is required to start.", tr: "Ana özelliklerin kısa turu — her birini sonra üst bardan ya da Ayarlar'dan inceleyebilirsin. Başlamak için hiçbiri zorunlu değil." },
  "onboard.inside_mem_t":     { en: "Memory", tr: "Hafıza" },
  "onboard.inside_mem_d":     { en: "Akana can remember facts you tell it — your name, preferences, ongoing projects — so it doesn't ask twice. New entries wait for your approval in Memory Studio, where you can review, edit, or delete anything.", tr: "Akana ona söylediğin bilgileri hatırlayabilir — adın, tercihlerin, süregelen projelerin — böylece iki kez sormaz. Yeni kayıtlar Memory Studio'da onayını bekler; orada her şeyi gözden geçirebilir, düzenleyebilir veya silebilirsin." },
  "onboard.inside_vault_t":   { en: "Vault", tr: "Kasa" },
  "onboard.inside_vault_d":   { en: "An encrypted local store for secrets like API keys. Values are kept on your machine and only ever shown masked; each key is sent solely to the provider it belongs to.", tr: "API anahtarları gibi gizli değerler için şifreli, yerel bir depo. Değerler makinende tutulur ve yalnızca maskeli gösterilir; her anahtar sadece ait olduğu sağlayıcıya gönderilir." },
  "onboard.inside_packs_t":   { en: "Packs", tr: "Pack'ler" },
  "onboard.inside_packs_d":   { en: "Installable bundles of skills that add new abilities on demand. Install the ones you need and leave the rest out — you decide what Akana can do.", tr: "İstendiğinde yeni yetenekler ekleyen, kurulabilir beceri paketleri. İhtiyacın olanları kur, gerisini bırak — Akana'nın ne yapabileceğine sen karar verirsin." },
  "onboard.inside_persona_t": { en: "Personas", tr: "Personalar" },
  "onboard.inside_persona_d": { en: "Switchable assistant styles. A persona sets Akana's tone and system prompt — pick one that fits the task, or write your own, in Settings → Persona.", tr: "Değiştirilebilir asistan stilleri. Bir persona, Akana'nın tonunu ve sistem komutunu belirler — göreve uygun birini seç ya da Ayarlar → Persona'da kendininkini yaz." },
  "onboard.inside_connectors_t": { en: "Connectors", tr: "Bağlantılar" },
  "onboard.inside_connectors_d": { en: "Bridges that let you reach Akana from other apps — for example the Telegram connector, so you can chat with it from your phone. Enable connectors you want in Settings.", tr: "Akana'ya başka uygulamalardan ulaşmanı sağlayan köprüler — örneğin Telegram bağlantısı, telefonundan sohbet edebilmen için. İstediğin bağlantıları Ayarlar'dan etkinleştir." },
  "onboard.inside_voice_t":   { en: "Voice", tr: "Ses" },
  "onboard.inside_voice_d":   { en: "Speak to Akana and hear replies out loud, hands-free. Turn it on in the next step or later from voice settings — it needs microphone access the first time.", tr: "Akana ile konuş ve yanıtları sesli dinle, eller serbest. Bir sonraki adımda ya da sonra ses ayarlarından aç — ilk seferinde mikrofon erişimi ister." },
  "onboard.inside_hint":      { en: "All of these live in the top bar and Settings — nothing here is required to start chatting.", tr: "Hepsi üst barda ve Ayarlar'da — sohbete başlamak için buradaki hiçbir şey gerekli değil." },

  // ── connect step (THE HEART) ─────────────────────────────────────────────
  "onboard.connect_title":    { en: "Connect a model", tr: "Bir model bağla" },
  "onboard.connect_lead":     { en: "Pick a provider and connect it right here — you'll be chatting in seconds.", tr: "Bir sağlayıcı seç ve tam burada bağla — saniyeler içinde sohbet edersin." },
  "onboard.connect_pick":     { en: "Choose a provider", tr: "Sağlayıcı seç" },
  "onboard.connect_save":     { en: "Save", tr: "Kaydet" },
  "onboard.connect_saving":   { en: "Saving your key…", tr: "Anahtarın kaydediliyor…" },
  "onboard.connect_save_failed": { en: "Couldn't save: {error}", tr: "Kaydedilemedi: {error}" },
  "onboard.connect_recheck":  { en: "Re-check connection", tr: "Bağlantıyı yeniden denetle" },
  "onboard.connect_rechecking": { en: "Checking…", tr: "Denetleniyor…" },
  // Recheck verdicts — shown inline right under the button (claude/ollama step).
  "onboard.connect_result_ok":   { en: "Connected to {provider}.", tr: "{provider} bağlandı." },
  "onboard.connect_result_ready":{ en: "{provider} is set up — you're ready to chat.", tr: "{provider} ayarlandı — sohbete hazırsın." },
  "onboard.connect_result_fail": { en: "Couldn't reach {provider} yet — follow the steps above, then re-check.", tr: "{provider} henüz erişilemedi — yukarıdaki adımları izle, sonra yeniden denetle." },
  "onboard.connect_switch_failed": { en: "Couldn't switch the active provider. Check that the server is running, then try again.", tr: "Aktif sağlayıcı değiştirilemedi. Sunucunun çalıştığını kontrol edip yeniden dene." },
  // Localized from the server probe's language-neutral error_code, so a TR banner
  // doesn't show verbatim English. Unknown codes fall back to the raw probe text.
  "onboard.connect_err_bridge_missing": { en: "Cursor bridge not installed — run: python akana.py add cursor", tr: "Cursor köprüsü kurulu değil — çalıştır: python akana.py add cursor" },
  "onboard.connect_err_unreachable": { en: "Couldn't reach the provider — check your key and connection, then re-check.", tr: "Sağlayıcıya erişilemedi — anahtarını ve bağlantını kontrol edip yeniden denetle." },
  "onboard.connect_err_auth_rejected": { en: "The provider rejected this key — check that it's correct, then re-check.", tr: "Sağlayıcı bu anahtarı reddetti — doğru olduğunu kontrol edip yeniden denetle." },
  "onboard.connect_err_sdk_missing": { en: "The provider SDK isn't installed — install it, then re-check.", tr: "Sağlayıcı SDK'sı kurulu değil — kur, sonra yeniden denetle." },
  // Claude-specific reasons surfaced from the /system/status claude_cli probe.
  "onboard.connect_claude_unreachable": { en: "Claude session token didn't answer — send one message to refresh the CLI token, or run `claude login` again, then re-check.", tr: "Claude oturum jetonu yanıt vermedi — CLI jetonunu tazelemek için bir mesaj gönder ya da yeniden `claude login` çalıştır, sonra yeniden denetle." },
  "onboard.connect_claude_nologin": { en: "No Claude session found. Install the CLI and run `claude login`, then re-check.", tr: "Claude oturumu bulunamadı. CLI'ı kur ve `claude login` çalıştır, sonra yeniden denetle." },
  "onboard.connect_keynote":  { en: "Your key is stored locally on this machine and sent only to the provider.", tr: "Anahtarın bu makinede yerel olarak saklanır ve yalnızca sağlayıcıya gönderilir." },
  "onboard.connect_already":  { en: "Connected during setup ({hint}). You're all set.", tr: "Kurulumda bağlandı ({hint}). Her şey hazır." },
  // Fallback for {hint} when the masked credential has no hint fragment — avoids
  // leaking the English literal "set" into the translated parenthetical.
  "onboard.connect_hint_set": { en: "saved", tr: "kayıtlı" },
  "onboard.connect_replace":  { en: "Replace key", tr: "Anahtarı değiştir" },
  "onboard.prov_cursor_t":    { en: "Cursor", tr: "Cursor" },
  "onboard.prov_cursor_d":    { en: "Use your Cursor API key for a wide model catalog.", tr: "Geniş model kataloğu için Cursor API anahtarını kullan." },
  "onboard.prov_cursor_ph":   { en: "Paste your Cursor API key", tr: "Cursor API anahtarını yapıştır" },
  "onboard.prov_gemini_t":    { en: "Google Gemini", tr: "Google Gemini" },
  "onboard.prov_gemini_d":    { en: "Fast, capable models from Google AI Studio.", tr: "Google AI Studio'dan hızlı, yetenekli modeller." },
  "onboard.prov_gemini_ph":   { en: "Paste your Gemini API key", tr: "Gemini API anahtarını yapıştır" },
  "onboard.prov_openai_t":    { en: "OpenAI", tr: "OpenAI" },
  "onboard.prov_openai_d":    { en: "GPT models via your OpenAI API key.", tr: "OpenAI API anahtarınla GPT modelleri." },
  "onboard.prov_openai_ph":   { en: "Paste your OpenAI API key", tr: "OpenAI API anahtarını yapıştır" },
  "onboard.prov_claude_t":    { en: "Claude", tr: "Claude" },
  "onboard.prov_claude_d":    { en: "Use the Claude Code CLI — no API key needed here.", tr: "Claude Code CLI'ı kullan — burada API anahtarı gerekmez." },
  "onboard.prov_claude_hint": { en: "Install the Claude CLI and run `claude login` in your terminal, then re-check below.", tr: "Claude CLI'ı kur ve terminalde `claude login` çalıştır, sonra aşağıdan yeniden denetle." },
  "onboard.prov_codex_t":     { en: "Codex", tr: "Codex" },
  "onboard.prov_codex_d":     { en: "Use the OpenAI Codex CLI with your ChatGPT plan — no API key needed here.", tr: "OpenAI Codex CLI'ı ChatGPT aboneliğinle kullan — burada API anahtarı gerekmez." },
  "onboard.prov_codex_hint":  { en: "Install the Codex CLI (`npm i -g @openai/codex`) and run `codex login`, then re-check below.", tr: "Codex CLI'ı kur (`npm i -g @openai/codex`) ve `codex login` çalıştır, sonra aşağıdan yeniden denetle." },
  "onboard.connect_codex_unverifiable": { en: "Codex login can't be verified from here yet — make sure `codex login` succeeds in your terminal.", tr: "Codex girişi buradan henüz doğrulanamıyor — terminalde `codex login`'in başarılı olduğundan emin ol." },
  "onboard.prov_ollama_t":    { en: "Ollama (local)", tr: "Ollama (yerel)" },
  "onboard.prov_ollama_d":    { en: "Run open models fully offline on your machine.", tr: "Açık modelleri makinende tamamen çevrimdışı çalıştır." },
  "onboard.prov_ollama_hint": { en: "Install Ollama and pull a model (e.g. `ollama pull llama3`), then re-check below.", tr: "Ollama'yı kur ve bir model indir (örn. `ollama pull llama3`), sonra aşağıdan yeniden denetle." },

  // ── personalize step (name + look) ───────────────────────────────────────
  "onboard.person_title":     { en: "Make it yours", tr: "Sana göre ayarla" },
  "onboard.person_lead":      { en: "Tell me your name and pick a look — you can change these any time.", tr: "Adını söyle ve bir görünüm seç — istediğin an değiştirebilirsin." },
  "onboard.person_name_label":{ en: "What should I call you?", tr: "Sana nasıl hitap edeyim?" },
  "onboard.person_name_ph":   { en: "Your name", tr: "Adın" },
  "onboard.person_memory_value": { en: "Address the user as «{name}».", tr: "Kullanıcıya «{name}» diye hitap edilmeli." },

  // ── voice step (optional) ────────────────────────────────────────────────
  "onboard.voice_title":      { en: "Talk to Akana", tr: "Akana ile konuş" },
  "onboard.voice_lead":       { en: "Prefer hands-free? Turn on wake-word listening — or skip and do it later.", tr: "Eller serbest mi istersin? Uyandırma sözcüğü dinlemeyi aç — ya da atla, sonra yaparsın." },
  "onboard.voice_h3":         { en: "Voice mode", tr: "Sesli mod" },
  "onboard.voice_toggle_t":   { en: "«Hey Akana» listening", tr: "«Hey Akana» dinleme" },
  "onboard.voice_toggle_d":   { en: "Start listening for the wake word automatically.", tr: "Uyandırma sözcüğünü otomatik dinlemeye başla." },
  "onboard.voice_note":       { en: "Needs microphone access — your browser will ask the first time.", tr: "Mikrofon erişimi gerekir — tarayıcın ilk seferinde sorar." },
  "onboard.voice_unsupported": { en: "Needs a Chromium-based browser (Chrome, Edge, Brave).", tr: "Chromium tabanlı bir tarayıcı gerekir (Chrome, Edge, Brave)." },
  "onboard.connect_ollama_unverifiable": { en: "Can't verify Ollama — make sure `ollama serve` is running.", tr: "Ollama doğrulanamıyor — `ollama serve` çalıştığından emin ol." },
  "onboard.connect_ollama_unreachable": { en: "Ollama isn't responding — is `ollama serve` running?", tr: "Ollama yanıt vermiyor — `ollama serve` çalışıyor mu?" },

  // ── start step (use-case → sample prompts) ───────────────────────────────
  "onboard.start_title":      { en: "Let's begin", tr: "Hadi başlayalım" },
  "onboard.start_lead":       { en: "Pick what you're here for, then tap a prompt to drop it into the composer.", tr: "Ne için burada olduğunu seç, sonra bir öneriye dokunup yazma alanına ekle." },
  "onboard.start_usecase_label": { en: "What brings you here?", tr: "Seni buraya getiren ne?" },

  "onboard.setup_checking":   { en: "Checking your setup…", tr: "Kurulumun kontrol ediliyor…" },
  "onboard.setup_connected":  { en: "Connected · {provider} · {model}", tr: "Bağlı · {provider} · {model}" },
  "onboard.setup_saved_unverified_reason": { en: "Key saved for {provider}, but it isn't reachable yet: {reason}", tr: "{provider} için anahtar kaydedildi ama henüz erişilemiyor: {reason}" },
  "onboard.setup_saved_unverified": { en: "Key saved for {provider}, but the connection isn't verified yet.", tr: "{provider} için anahtar kaydedildi ama bağlantı henüz doğrulanmadı." },
  "onboard.setup_needs_key":  { en: "{provider} needs an API key before you can chat.", tr: "Sohbet edebilmek için {provider} bir API anahtarı gerektiriyor." },
  "onboard.setup_unknown":    { en: "No model provider is set up yet.", tr: "Henüz bir model sağlayıcısı ayarlı değil." },
  "onboard.setup_open":       { en: "Open Settings → add your key", tr: "Ayarları aç → anahtarını ekle" },
  "onboard.try_label":        { en: "Try one of these:", tr: "Şunlardan birini dene:" },
  "onboard.seed_dev1":        { en: "Run the tests and show me what fails", tr: "Testleri çalıştır ve neyin başarısız olduğunu göster" },
  "onboard.seed_dev2":        { en: "Summarize the changes in my last commit", tr: "Son commit'imdeki değişiklikleri özetle" },
  "onboard.seed_dev3":        { en: "Open my project's README", tr: "Projemin README dosyasını aç" },
  "onboard.seed_assistant1":  { en: "Remember that my favorite color is teal", tr: "En sevdiğim rengin teal olduğunu hatırla" },
  "onboard.seed_assistant2":  { en: "What do you remember about me?", tr: "Benim hakkımda ne hatırlıyorsun?" },
  "onboard.seed_assistant3":  { en: "Remind me to take a break in 30 minutes", tr: "30 dakika sonra mola vermemi hatırlat" },
  "onboard.seed_writing1":    { en: "Help me write a short, friendly email", tr: "Kısa, samimi bir e-posta yazmama yardım et" },
  "onboard.seed_writing2":    { en: "Rewrite this more clearly: ", tr: "Şunu daha anlaşılır yaz: " },
  "onboard.seed_writing3":    { en: "Brainstorm 5 ideas for ", tr: "Şunun için 5 fikir üret: " },

  // ── akana-turn-status ────────────────────────────────────────────────────
  "ui.turn_preparing":  { en: "Preparing",  tr: "Hazırlanıyor" },
  "ui.turn_connecting": { en: "Connecting", tr: "Bağlanıyor" },
  "ui.turn_thinking":   { en: "Thinking",   tr: "Düşünüyor" },
  "ui.turn_writing":    { en: "Writing",    tr: "Yazıyor" },
  "ui.turn_tool_default": { en: "tool",     tr: "araç" },

  // ── app.js error strings ─────────────────────────────────────────────────
  "ui.app_shell_missing": { en: "akana-shell.js failed to load — refresh with Ctrl+Shift+R.", tr: "akana-shell.js yüklenemedi — sayfayı Ctrl+Shift+R ile yenileyin." },
  "ui.app_chat_missing":  { en: "akana-chat.js failed to start — chat is disabled.", tr: "akana-chat.js başlatılamadı — sohbet devre dışı." },

  // ── akana-markdown ───────────────────────────────────────────────────────
  "ui.md_truncated": {
    en: "… (message too large: {total} characters — showing first {shown})",
    tr: "… (mesaj çok büyük: {total} karakter — ilk {shown} gösterildi)"
  },
});
