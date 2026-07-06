/**
 * Akana UI i18n strings — RUNTIME SETTINGS schema labels (categories, field
 * labels/descriptions, units). Source of truth for the Turkish originals:
 * akana_server/runtime_settings/schema.py. Merges into window.AkanaI18nStrings.
 * Keys: runtime.cat.<id> / runtime.<key>.label / runtime.<key>.desc / runtime.unit.<u>
 */
window.AkanaI18nStrings = Object.assign(window.AkanaI18nStrings || {}, {

  // ── Categories ────────────────────────────────────────────────────────────
  "runtime.cat.genel":      { en: "General",           tr: "Genel" },
  "runtime.cat.zamanlama":  { en: "Session Maintenance", tr: "Oturum Bakımı" },
  "runtime.cat.ozet":       { en: "Session Summaries", tr: "Oturum Özetleri" },
  "runtime.cat.beceri":     { en: "Skill Injection",   tr: "Beceri Enjeksiyonu" },
  "runtime.cat.planlayici": { en: "Context",           tr: "Bağlam" },
  "runtime.cat.dosya":      { en: "File Roots",        tr: "Dosya Kökleri" },
  "runtime.cat.yukleme":    { en: "Image Upload",      tr: "Görüntü Yükleme" },
  "runtime.cat.telegram":   { en: "Telegram",          tr: "Telegram" },
  "runtime.cat.otonom":     { en: "Autonomous Mode (Claude)", tr: "Otonom Mod (Claude)" },
  "runtime.cat.ag":         { en: "Network Resilience", tr: "Ağ Dayanıklılığı" },
  "runtime.cat.araclar":    { en: "Tools",             tr: "Araçlar" },
  "runtime.cat.ses":        { en: "Audio & Wake",      tr: "Ses & Uyandırma" },
  "runtime.llm_chat_titles.label": { en: "AI chat titles", tr: "Yapay zekâ sohbet başlıkları" },
  "runtime.llm_chat_titles.desc": { en: "Summarize each new chat's title from your first message using the LLM, in your language. Off = the first line of your message, truncated (no LLM call).", tr: "Her yeni sohbetin başlığını ilk mesajından, senin dilinde, LLM ile özetle. Kapalı = mesajının ilk satırı, kısaltılmış (LLM çağrısı yok)." },

  // ── Units ─────────────────────────────────────────────────────────────────
  "runtime.unit.sn":      { en: "sec",   tr: "sn" },
  "runtime.unit.dk":      { en: "min",   tr: "dk" },
  "runtime.unit.karakter": { en: "chars", tr: "karakter" },
  "runtime.unit.MB":      { en: "MB",    tr: "MB" },
  "runtime.unit.tur":     { en: "turns", tr: "tur" },
  "runtime.unit.kelime":  { en: "words", tr: "kelime" },

  // ── language ──────────────────────────────────────────────────────────────
  "runtime.language.label": {
    en: "Language",
    tr: "Dil / Language",
  },
  "runtime.language.desc": {
    en: "The interface, voice, and Akana's default persona use this language: «en» (English, default) or «tr» (Turkish). The open-source release is English-first; selecting «tr» switches the UI, voice, and persona to Turkish.",
    tr: "Arayüz, ses ve Akana'nın varsayılan personası bu dilde olur: «en» (İngilizce, varsayılan) veya «tr» (Türkçe). Açık kaynak sürüm İngilizce-first; «tr» seçilince UI/ses/persona Türkçeye döner.",
  },

  // ── whisper_prompt ────────────────────────────────────────────────────────
  "runtime.whisper_prompt.label": {
    en: "Speech recognition term glossary",
    tr: "Ses tanıma terim sözlüğü",
  },
  "runtime.whisper_prompt.desc": {
    en: "Context for Whisper (initial_prompt): write the technical terms and names you use frequently here — in mixed Turkish-English speech it will transcribe these more ACCURATELY. The model and language do not change; speed is unaffected. Avoid LONG or keyword-heavy entries — they bias Whisper and break everyday speech. Empty = unbiased (recommended).",
    tr: "Whisper'a bağlam (initial_prompt): sık kullandığın teknik terim/isimleri buraya yaz — karışık Türkçe-İngilizce konuşmada bunları daha DOĞRU yazar. Model/dil değişmez, hız etkilenmez. UZUN/anahtar-kelime-yoğun yazma — Whisper'ı yanlı yapıp gündelik konuşmayı bozar. Boş = yansız (önerilen).",
  },

  // session_closer_enabled: hidden from the generic runtime form (session summarization is
  // gated by the memory-level `session_summary` flag, which defaults ON — its Memory Studio
  // toggle was removed; this spec is now only the env-level kill switch), so the
  // runtime.session_closer_enabled.{label,desc} keys are intentionally absent here — same
  // precedent as wake_threshold below.

  // ── session_closer_interval ───────────────────────────────────────────────
  "runtime.session_closer_interval.label": {
    en: "Session closing scan interval (seconds)",
    tr: "Oturum kapanış tarama aralığı (saniye)",
  },
  "runtime.session_closer_interval.desc": {
    en: "Idle conversation scan runs at this interval. 0 = disabled (minimum 30 sec).",
    tr: "Idle sohbet taraması bu aralıkla koşar. 0 = kapalı (en az 30 sn).",
  },

  // ── session_closer_idle_minutes ───────────────────────────────────────────
  "runtime.session_closer_idle_minutes.label": {
    en: "Idle conversation threshold (minutes)",
    tr: "Idle sohbet eşiği (dakika)",
  },
  "runtime.session_closer_idle_minutes.desc": {
    en: "A conversation that has received no messages for this long is considered 'idle' and is summarised.",
    tr: "Bu süredir mesaj almayan sohbet 'idle' sayılır ve özetlenir.",
  },
  "runtime.session_closer_turn_threshold.label": {
    en: "Long-conversation summary threshold (turns)",
    tr: "Uzun sohbet özet eşiği (tur)",
  },
  "runtime.session_closer_turn_threshold.desc": {
    en: "Once a conversation accumulates this many new turns it is summarised without waiting to go idle — catching long, still-active chats early. 0 = off (idle only).",
    tr: "Bir sohbet bu kadar yeni tur biriktirince idle olmasını beklemeden özetlenir — uzun, hâlâ aktif sohbeti erken yakalar. 0 = kapalı (yalnız idle tetikler).",
  },

  // ── session_closer_char_threshold ─────────────────────────────────────────
  "runtime.session_closer_char_threshold.label": {
    en: "Long-chat summary threshold (characters)",
    tr: "Uzun sohbet özet eşiği (karakter)",
  },
  "runtime.session_closer_char_threshold.desc": {
    en: "A content-aware companion to the turn threshold: once the NEW user/assistant text accumulated since the last summary exceeds this many characters, the chat is summarised without waiting for it to go idle — so a few dense turns trigger too (turn count is content-blind). 0 = off (turn/idle triggers only).",
    tr: "Tur eşiğinin içerik-duyarlı tamamlayıcısı: son özetten beri biriken YENİ kullanıcı/asistan metni bu kadar karakteri aşınca, sohbet idle olmasını beklemeden özetlenir — yoğun birkaç tur da tetikler (tur sayısı içeriğe kördür). 0 = kapalı (yalnız tur/idle tetikler).",
  },

  // ── session_closer_max_chars (Session Summaries) ──────────────────────────
  "runtime.session_closer_max_chars.label": {
    en: "Summarization chunk size (characters)",
    tr: "Özetleme parça boyutu (karakter)",
  },
  "runtime.session_closer_max_chars.desc": {
    en: "The transcript is fed to the summarizer in chunks of this many characters (and a single very long message is clipped to it). Larger = more context per LLM call but a heavier, slower call; smaller = cheaper calls but more of them on a cold-start multi-chunk pass.",
    tr: "Transkript özetleyiciye bu kadar karakterlik parçalar halinde verilir (ve tek bir çok uzun mesaj buna kırpılır). Daha büyük = LLM çağrısı başına daha çok bağlam ama daha ağır, daha yavaş çağrı; daha küçük = daha ucuz ama soğuk-başlangıç çok-parçalı geçişte daha çok çağrı.",
  },

  // ── session_summary_inject_enabled ────────────────────────────────────────
  "runtime.session_summary_inject_enabled.label": {
    en: "Prior-context recall enabled",
    tr: "Önceki-bağlam hatırlatma aktif",
  },
  "runtime.session_summary_inject_enabled.desc": {
    en: "At the start of each turn the rolling session summary for the active chat is folded back into the prompt as a compact «Prior context» block, so the model resumes a long chat with its earlier decisions/open items in hand even after older turns scroll out of the window.",
    tr: "Her turun başında aktif sohbetin yuvarlanan oturum özeti, kompakt bir «Önceki bağlam» bloğu olarak prompt'a geri katılır; böylece model, eski turlar pencereden kaysa bile uzun bir sohbete önceki kararları/açık maddeleri elinde tutarak devam eder.",
  },

  // ── session_summary_inject_max_chars ──────────────────────────────────────
  "runtime.session_summary_inject_max_chars.label": {
    en: "Prior-context recall budget (characters)",
    tr: "Önceki-bağlam hatırlatma bütçesi (karakter)",
  },
  "runtime.session_summary_inject_max_chars.desc": {
    en: "Hard cap on the «Prior context» block injected each turn — a long rolling summary is clipped to this many characters so recall can never silently eat the turn's context budget. 0 = no cap (inject the whole summary).",
    tr: "Her tur enjekte edilen «Önceki bağlam» bloğuna sert sınır — uzun bir yuvarlanan özet bu kadar karaktere kırpılır, böylece hatırlatma turun bağlam bütçesini sessizce yiyemez. 0 = sınırsız (özetin tamamını enjekte et).",
  },

  // ── summary_consolidation_enabled ─────────────────────────────────────────
  "runtime.summary_consolidation_enabled.label": {
    en: "Summary consolidation enabled",
    tr: "Özet birleştirme aktif",
  },
  "runtime.summary_consolidation_enabled.desc": {
    en: "A background pass clusters related session summaries and stages a single consolidated memory candidate, so recurring threads across many chats collapse into one durable note instead of N scattered ones.",
    tr: "Arka plan geçişi ilişkili oturum özetlerini kümeler ve tek bir birleştirilmiş hafıza adayı olarak sahneler; böylece birçok sohbete yayılan tekrar eden konular, N dağınık not yerine tek bir kalıcı notta toplanır.",
  },

  // ── summary_consolidation_interval ────────────────────────────────────────
  "runtime.summary_consolidation_interval.label": {
    en: "Summary consolidation interval (seconds)",
    tr: "Özet birleştirme aralığı (saniye)",
  },
  "runtime.summary_consolidation_interval.desc": {
    en: "The summary-clustering pass runs at this interval. 0 = off (minimum 300 sec).",
    tr: "Özet kümeleme geçişi bu aralıkla koşar. 0 = kapalı (en az 300 sn).",
  },

  // ── summary_consolidation_min_overlap ─────────────────────────────────────
  "runtime.summary_consolidation_min_overlap.label": {
    en: "Consolidation overlap threshold (shared tokens)",
    tr: "Birleştirme örtüşme eşiği (ortak kelime)",
  },
  "runtime.summary_consolidation_min_overlap.desc": {
    en: "How many shared topical words two session summaries must have in common before they are clustered into one consolidated topic. Higher = stricter (only very-related summaries merge); lower = more aggressive grouping.",
    tr: "İki oturum özetinin tek bir birleştirilmiş konuda kümelenmesi için ortak kaç konu kelimesine sahip olması gerektiği. Daha yüksek = daha katı (yalnız çok-ilişkili özetler birleşir); daha düşük = daha agresif gruplama.",
  },

  // ── skill_inject_enabled ──────────────────────────────────────────────────
  "runtime.skill_inject_enabled.label": {
    en: "Skill injection active",
    tr: "Skill enjeksiyonu aktif",
  },
  "runtime.skill_inject_enabled.desc": {
    en: "Automatic skill (SKILL.md) injection at the start of each turn (WI-1).",
    tr: "Tur başına otomatik yetenek (SKILL.md) enjeksiyonu (WI-1).",
  },

  // ── skill_catalog_enabled ─────────────────────────────────────────────────
  "runtime.skill_catalog_enabled.label": {
    en: "Skill catalog (system prompt)",
    tr: "Yetenek kataloğu (system prompt)",
  },
  "runtime.skill_catalog_enabled.desc": {
    en: "A compact inventory of installed skills/packages (title + triggers) is appended to the system prompt of each turn; «Can you do X?» is answered against the real inventory (WI-2). Nothing is added when the registry is empty.",
    tr: "Kurulu skill/paketlerin kompakt envanteri (başlık + tetikleyiciler) her turun system prompt'una eklenir; «X yapabilir misin?» gerçek envantere göre yanıtlanır (WI-2). Boş registry'de hiçbir şey eklenmez.",
  },

  // ── skill_inject_threshold ────────────────────────────────────────────────
  "runtime.skill_inject_threshold.label": {
    en: "Injection RRF threshold",
    tr: "Enjeksiyon RRF eşiği",
  },
  "runtime.skill_inject_threshold.desc": {
    en: "Minimum RRF score required for non-trigger matches (0.03 ≈ at least two search layers are surfacing the same skill).",
    tr: "Trigger dışı eşleşmelerde gereken minimum RRF skoru (0.03 ≈ en az iki arama katmanı aynı skill'i öne koyuyor).",
  },

  // ── skill_inject_max ──────────────────────────────────────────────────────
  "runtime.skill_inject_max.label": {
    en: "Maximum skills per turn",
    tr: "Tur başına en fazla skill",
  },
  "runtime.skill_inject_max.desc": {
    en: "Upper limit on the number of skills injected into the prompt in one conversation turn.",
    tr: "Bir sohbet turunda prompt'a enjekte edilecek skill üst sınırı.",
  },

  // ── skill_catalog_max_entries ─────────────────────────────────────────────
  "runtime.skill_catalog_max_entries.label": {
    en: "Catalog entry ceiling",
    tr: "Katalog girdi tavanı",
  },
  "runtime.skill_catalog_max_entries.desc": {
    en: "Maximum installed capabilities listed in the system-prompt catalog. The default (256) covers a large install; overflow is shown as a visible «(+N more)» note, never dropped silently.",
    tr: "System prompt kataloğunda listelenen en fazla kurulu yetenek sayısı. Varsayılan (256) büyük kurulumu kapsar; taşan kısım sessizce atılmaz, görünür «(+N daha)» notuyla belirtilir.",
  },

  // ── skill_catalog_max_chars ───────────────────────────────────────────────
  "runtime.skill_catalog_max_chars.label": {
    en: "Catalog character ceiling",
    tr: "Katalog karakter tavanı",
  },
  "runtime.skill_catalog_max_chars.desc": {
    en: "Maximum size (characters) of the installed-capabilities block in the system prompt. Overflow is summarized with a visible «(+N more)» note; raise it toward the context budget for very large installs.",
    tr: "System prompt'taki kurulu-yetenekler bloğunun en fazla boyutu (karakter). Taşan kısım görünür «(+N daha)» notuyla özetlenir; çok büyük kurulumlarda bağlam bütçesine doğru yükseltin.",
  },

  // ── skill_suggest_timeout_s ───────────────────────────────────────────────
  "runtime.skill_suggest_timeout_s.label": {
    en: "Suggestion search time budget (seconds)",
    tr: "Öneri arama zaman bütçesi (saniye)",
  },
  "runtime.skill_suggest_timeout_s.desc": {
    en: "If the skill suggestion search exceeds this time, the turn proceeds without injection.",
    tr: "Skill öneri araması bu süreyi aşarsa tur enjeksiyonsuz sürer.",
  },

  // ── context_max_chars ─────────────────────────────────────────────────────
  "runtime.context_max_chars.label": {
    en: "Context character budget",
    tr: "Bağlam karakter bütçesi",
  },
  "runtime.context_max_chars.desc": {
    en: "Total character limit for system + history + user text; when exceeded, history is trimmed first, then the skill block. 0 = unlimited.",
    tr: "System + geçmiş + kullanıcı metni toplam karakter sınırı; aşılırsa önce geçmiş, sonra skill bloğu kırpılır. 0 = sınırsız.",
  },

  // ── file_roots ────────────────────────────────────────────────────────────
  "runtime.file_roots.label": {
    en: "Permitted roots for file tools",
    tr: "Dosya araçları izinli kökleri",
  },
  "runtime.file_roots.desc": {
    en: "Roots that Akana's own file tools (list/read) can access, separated by ':'. Empty = FileEngine disabled.",
    tr: "Akana'in kendi dosya araçlarının (listele/oku) erişebildiği kökler, ':' ile ayrılır. Boş = FileEngine devre dışı.",
  },

  // ── uploads_enabled ───────────────────────────────────────────────────────
  "runtime.uploads_enabled.label": {
    en: "File upload active",
    tr: "Dosya yükleme aktif",
  },
  "runtime.uploads_enabled.desc": {
    en: "When disabled, POST /uploads is immediately rejected with 403.",
    tr: "Kapatılırsa POST /uploads anında 403 ile reddedilir.",
  },

  // ── upload_max_mb ─────────────────────────────────────────────────────────
  "runtime.upload_max_mb.label": {
    en: "Single file size limit (MB)",
    tr: "Tek dosya boyut sınırı (MB)",
  },
  "runtime.upload_max_mb.desc": {
    en: "Files exceeding this limit are rejected without being fully loaded into memory.",
    tr: "Sınırı aşan dosya belleğe tamamen alınmadan reddedilir.",
  },

  // ── telegram_enabled ──────────────────────────────────────────────────────
  "runtime.telegram_enabled.label": {
    en: "Telegram bridge active",
    tr: "Telegram köprüsü aktif",
  },
  "runtime.telegram_enabled.desc": {
    en: "Telegram bot polling. Because the connector lifecycle is set up at server startup, changes take effect ON RESTART.",
    tr: "Telegram bot polling'i. Connector yaşam döngüsü sunucu açılışında kurulduğu için değişiklik YENİDEN BAŞLATMADA uygulanır.",
  },

  // ── telegram_allowed_chat_ids ─────────────────────────────────────────────
  "runtime.telegram_allowed_chat_ids.label": {
    en: "Allowed Telegram chat IDs",
    tr: "İzinli Telegram chat id'leri",
  },
  "runtime.telegram_allowed_chat_ids.desc": {
    en: "Comma-separated allowlist; messages from chats not on the list are ignored. Empty = nobody can write. Applied on restart.",
    tr: "Virgülle ayrılmış allowlist; listede olmayan chat'lerin mesajları yok sayılır. Boş = kimse yazamaz. Yeniden başlatmada uygulanır.",
  },

  // ── bridge_timeout ────────────────────────────────────────────────────────
  "runtime.bridge_timeout.label": {
    en: "Cursor bridge idle timeout (seconds)",
    tr: "Cursor köprü boşta kalma süresi (saniye)",
  },
  "runtime.bridge_timeout.desc": {
    en: "Maximum seconds to wait between two events in a single LLM turn (long tool calls, Gemini pull, etc.). If exceeded: «bridge daemon timed out». The daemon sends heartbeats; if still insufficient, increase this value.",
    tr: "Bir LLM turunda iki event arasında en fazla bu kadar saniye beklenir (uzun araç çağrıları, Gemini pull vb.). Aşılırsa «bridge daemon timed out». Daemon heartbeat gönderir; yine de yetmezse değeri artır.",
  },

  // ── claude_bridge_timeout ─────────────────────────────────────────────────
  "runtime.claude_bridge_timeout.label": {
    en: "Claude CLI idle timeout (seconds)",
    tr: "Claude CLI boşta kalma süresi (saniye)",
  },
  "runtime.claude_bridge_timeout.desc": {
    en: "Turn timeout while waiting for a tool/response in the Claude provider.",
    tr: "Claude sağlayıcısında araç/yanıt beklerken tur zaman aşımı.",
  },

  // ── llm_idle_timeout ──────────────────────────────────────────────────────
  "runtime.llm_idle_timeout.label": {
    en: "LLM stream idle-hang ceiling (seconds)",
    tr: "LLM akış boşta-asılma tavanı (saniye)",
  },
  "runtime.llm_idle_timeout.desc": {
    en: "Maximum seconds to wait between two new chunks (delta/tool/heartbeat) within a single LLM STREAM. If the stream stops producing chunks and hangs, the turn ends cleanly with «LLM_TIMEOUT» (504); the bridge process group is killed. A slowly progressing stream is NOT affected (each chunk resets the counter). 0 = disabled (only the existing bridge_timeout applies).",
    tr: "Bir LLM AKIŞINDA iki yeni parça (delta/araç/heartbeat) arasında en fazla bu kadar saniye beklenir. Akış parça üretmeyi DURDURUP asılırsa tur temiz «LLM_TIMEOUT» (504) ile biter; köprü süreç grubu öldürülür. İlerleyen yavaş akış ETKİLENMEZ (her parça sayacı sıfırlar). 0 = kapalı (yalnız mevcut bridge_timeout geçerli).",
  },

  // ── llm_total_timeout ─────────────────────────────────────────────────────
  "runtime.llm_total_timeout.label": {
    en: "LLM blocking call total-time ceiling (seconds)",
    tr: "LLM bloklayan çağrı toplam-süre tavanı (saniye)",
  },
  "runtime.llm_total_timeout.desc": {
    en: "A non-streaming (single-shot) LLM call lasts at most this many seconds end-to-end. If exceeded: clean «LLM_TIMEOUT» (504); bridge process killed. Only affects the blocking path (complete_chat); streaming uses the idle timeout. 0 = disabled (only the existing bridge_timeout applies).",
    tr: "Akışsız (tek-atış) bir LLM çağrısı en fazla bu kadar saniye sürer (uçtan uca). Aşılırsa temiz «LLM_TIMEOUT» (504); köprü süreci öldürülür. Yalnız bloklayan yolu (complete_chat) etkiler; akış idle tavanını kullanır. 0 = kapalı (yalnız mevcut bridge_timeout geçerli).",
  },

  // ── network_max_retries ───────────────────────────────────────────────────
  "runtime.network_max_retries.label": {
    en: "Maximum retry attempts",
    tr: "En fazla deneme sayısı",
  },
  "runtime.network_max_retries.desc": {
    en: "Maximum number of times an LLM call is retried on transient network errors (timeout/5xx/429). 1 = no retry. Auth/permanent errors are never retried.",
    tr: "Geçici ağ hatasında (timeout/5xx/429) bir LLM çağrısı en fazla kaç kez denenir. 1 = retry yok. Auth/kalıcı hatalar asla denenmez.",
  },

  // ── network_base_delay ────────────────────────────────────────────────────
  "runtime.network_base_delay.label": {
    en: "Initial back-off delay (seconds)",
    tr: "İlk geri çekilme gecikmesi (saniye)",
  },
  "runtime.network_base_delay.desc": {
    en: "Initial wait time for exponential back-off; doubles with each attempt.",
    tr: "Üstel geri çekilmenin ilk bekleme süresi; her denemede 2 kat artar.",
  },

  // ── network_max_delay ─────────────────────────────────────────────────────
  "runtime.network_max_delay.label": {
    en: "Back-off ceiling (seconds)",
    tr: "Geri çekilme tavanı (saniye)",
  },
  "runtime.network_max_delay.desc": {
    en: "Maximum wait time per attempt in the exponential back-off.",
    tr: "Üstel geri çekilmenin tek deneme için en fazla bekleme süresi.",
  },

  // ── network_total_timeout ─────────────────────────────────────────────────
  "runtime.network_total_timeout.label": {
    en: "Total retry time budget (seconds)",
    tr: "Toplam retry süre bütçesi (saniye)",
  },
  "runtime.network_total_timeout.desc": {
    en: "The combined duration of all attempts cannot exceed this budget. 0 = unlimited.",
    tr: "Tüm denemelerin toplam süresi bu bütçeyi aşamaz. 0 = sınırsız.",
  },

  // ── network_jitter ────────────────────────────────────────────────────────
  "runtime.network_jitter.label": {
    en: "Back-off jitter ratio",
    tr: "Geri çekilme jitter oranı",
  },
  "runtime.network_jitter.desc": {
    en: "Randomness of ±this ratio is added to the delay (spreads thundering-herd bursts). 0 = no jitter.",
    tr: "Gecikmeye ±bu oranda rastgelelik eklenir (gürül-patlamayı dağıtır). 0 = jitter yok.",
  },

  // ── network_breaker_threshold ─────────────────────────────────────────────
  "runtime.network_breaker_threshold.label": {
    en: "Circuit breaker error threshold",
    tr: "Devre kesici hata eşiği",
  },
  "runtime.network_breaker_threshold.desc": {
    en: "When this many consecutive errors occur on a provider, the circuit 'opens' (no calls are made, fast-fail). 0 = circuit breaker disabled.",
    tr: "Bir sağlayıcıda bu kadar ardışık hata olunca devre 'açılır' (çağrı yapılmaz, hızlı-başarısız). 0 = devre kesici kapalı.",
  },

  // ── network_breaker_cooldown ──────────────────────────────────────────────
  "runtime.network_breaker_cooldown.label": {
    en: "Circuit breaker cooldown (seconds)",
    tr: "Devre kesici soğuma süresi (saniye)",
  },
  "runtime.network_breaker_cooldown.desc": {
    en: "Wait time after the circuit opens before a single probe attempt is allowed.",
    tr: "Devre açıldıktan sonra tek deneme penceresine kadar bekleme.",
  },

  // ── agent_autocontinue ────────────────────────────────────────────────────
  "runtime.agent_autocontinue.label": {
    en: "Autonomous continuation (Claude)",
    tr: "Otonom devam etme (Claude)",
  },
  "runtime.agent_autocontinue.desc": {
    en: "OFF by default: every message is a single run, so when Akana asks you something it stops and waits for your reply. Turn this ON only for deep, Claude-Code-style workflows where the Claude agent keeps working across multiple turns on its own. Other providers ignore this.",
    tr: "Varsayılan KAPALI: her mesaj tek çalıştırmadır, yani Akana bir şey sorduğunda durup cevabını bekler. Bunu yalnızca Claude ajanının kendi başına birden çok turda çalışmaya devam ettiği derin, Claude-Code tarzı iş akışları için AÇ. Diğer sağlayıcılar bunu yok sayar.",
  },

  // ── agent_max_continue_iters ──────────────────────────────────────────────
  "runtime.agent_max_continue_iters.label": {
    en: "Max continuation runs",
    tr: "En fazla devam çalıştırması",
  },
  "runtime.agent_max_continue_iters.desc": {
    en: "Upper bound on how many Claude runs a single message may chain through auto-continuation. The hard ceiling that stops a runaway loop.",
    tr: "Tek bir mesajın otomatik devam ile zincirleyebileceği en fazla Claude çalıştırma sayısı. Kaçak döngüyü durduran sert tavan.",
  },

  // ── agent_continue_deadline ───────────────────────────────────────────────
  "runtime.agent_continue_deadline.label": {
    en: "Continuation wall-clock budget (seconds)",
    tr: "Devam etme süre bütçesi (saniye)",
  },
  "runtime.agent_continue_deadline.desc": {
    en: "Total time across ALL auto-continuation runs for one message. When exceeded, the turn finishes at the next run boundary. 0 = off (only the run-count cap applies).",
    tr: "Bir mesajın TÜM otomatik devam çalıştırmaları için toplam süre. Aşılırsa tur, bir sonraki çalıştırma sınırında sona erer. 0 = kapalı (yalnız çalıştırma sayısı tavanı geçerli).",
  },

  // ── memory_tools_enabled ──────────────────────────────────────────────────
  "runtime.memory_tools_enabled.label": {
    en: "Memory tools (MCP) active",
    tr: "Hafıza araçları (MCP) aktif",
  },
  "runtime.memory_tools_enabled.desc": {
    en: "Exposes akana_memory MCP tools (memory_search/remember/forget/explain) to the model. When disabled, the model cannot access memory via tools.",
    tr: "akana_memory MCP araçlarını (memory_search/remember/forget/explain) modele sunar. Kapatılırsa model hafızaya araçla erişemez.",
  },

  // ── vault_tools_enabled ───────────────────────────────────────────────────
  "runtime.vault_tools_enabled.label": {
    en: "Secure-vault tools (MCP) active",
    tr: "Güvenli kasa araçları (MCP) aktif",
  },
  "runtime.vault_tools_enabled.desc": {
    en: "Exposes akana_vault MCP read tools (vault_list/vault_get/vault_get_credential) to the model so it can discover and use stored secrets. When disabled, the model cannot read the vault via tools.",
    tr: "akana_vault MCP okuma araçlarını (vault_list/vault_get/vault_get_credential) modele sunar; böylece saklanan sırları keşfedip kullanabilir. Kapatılırsa model kasayı araçla okuyamaz.",
  },

  // wake_threshold: hidden from the generic runtime form (voice panel has its own
  // «HEY AKANA» slider, strings live under settings.voice.wake_threshold_*), so the
  // runtime.wake_threshold.{label,desc} keys are intentionally absent here.

  // ── gemini_live_enabled ───────────────────────────────────────────────────
  "runtime.gemini_live_enabled.label": {
    en: "Gemini Live (real-time audio) active",
    tr: "Gemini Live (gerçek-zamanlı ses) aktif",
  },
  "runtime.gemini_live_enabled.desc": {
    en: "When the 'Gemini' provider is selected, the voice chat button switches to full-duplex Live mode (microphone → Google → audio, continuous). When DISABLED, voice stays classic turn-based (Whisper→text→TTS) for all providers. Audio flows to Google cloud — with your own gemini_api_key, opt-in.",
    tr: "Provider 'Gemini' seçiliyken sesli sohbet düğmesi tam-dupleks Live moduna geçer (mikrofon → Google → ses, kesintisiz). KAPALIYKEN ses her sağlayıcıda klasik tur-bazlı (Whisper→metin→TTS) kalır. Ses Google buluta akar — kendi gemini_api_key'inle, opt-in.",
  },

  // ── gemini_live_model ─────────────────────────────────────────────────────
  "runtime.gemini_live_model.label": {
    en: "Gemini Live model",
    tr: "Gemini Live modeli",
  },
  "runtime.gemini_live_model.desc": {
    en: "Live native-audio model name (preview). Empty = default 'models/gemini-2.5-flash-native-audio-latest'. Only affects the Live audio surface; text chat uses a separate 'Gemini model'.",
    tr: "Live native-audio model adı (preview). Boş = varsayılan 'models/gemini-2.5-flash-native-audio-latest'. Yalnız Live ses yüzeyini etkiler; metin sohbeti ayrı 'Gemini modeli'ni kullanır.",
  },

  // ── gemini_live_voice ─────────────────────────────────────────────────────
  "runtime.gemini_live_voice.label": {
    en: "Gemini Live voice",
    tr: "Gemini Live sesi",
  },
  "runtime.gemini_live_voice.desc": {
    en: "Preset voice name for Live responses. Choose from the list; all are multilingual (including Turkish). Empty = default 'Charon'.",
    tr: "Live yanıtının ön-tanımlı ses adı. Listeden seç; hepsi çok-dilli (Türkçe dahil) çalışır. Boş = varsayılan 'Charon'.",
  },

  // ── openai_realtime_enabled ───────────────────────────────────────────────
  "runtime.openai_realtime_enabled.label": {
    en: "OpenAI Realtime (real-time audio) active",
    tr: "OpenAI Realtime (gerçek-zamanlı ses) aktif",
  },
  "runtime.openai_realtime_enabled.desc": {
    en: "When the 'OpenAI' provider is selected, the voice chat button switches to full-duplex Realtime mode (microphone → OpenAI → audio, continuous). When DISABLED, voice stays classic turn-based (Whisper→text→TTS) for all providers. Audio flows to OpenAI cloud — with your own openai_api_key, opt-in.",
    tr: "Provider 'OpenAI' seçiliyken sesli sohbet düğmesi tam-dupleks Realtime moduna geçer (mikrofon → OpenAI → ses, kesintisiz). KAPALIYKEN ses her sağlayıcıda klasik tur-bazlı (Whisper→metin→TTS) kalır. Ses OpenAI buluta akar — kendi openai_api_key'inle, opt-in.",
  },

  // ── openai_realtime_model ─────────────────────────────────────────────────
  "runtime.openai_realtime_model.label": {
    en: "OpenAI Realtime model",
    tr: "OpenAI Realtime modeli",
  },
  "runtime.openai_realtime_model.desc": {
    en: "Realtime model name. Empty = default 'gpt-4o-realtime-preview'. Only affects the Realtime audio surface; text chat uses a separate 'OpenAI model'.",
    tr: "Realtime model adı. Boş = varsayılan 'gpt-4o-realtime-preview'. Yalnız Realtime ses yüzeyini etkiler; metin sohbeti ayrı 'OpenAI modeli'ni kullanır.",
  },

  // ── openai_realtime_voice ─────────────────────────────────────────────────
  "runtime.openai_realtime_voice.label": {
    en: "OpenAI Realtime voice",
    tr: "OpenAI Realtime sesi",
  },
  "runtime.openai_realtime_voice.desc": {
    en: "Preset voice name for Realtime responses. Choose from the list; all are multilingual (including Turkish). Empty = default 'alloy'. NOTE: 'marin'/'cedar' ONLY work with the GA model ('gpt-realtime'); they are invalid on the BETA preview model.",
    tr: "Realtime yanıtının ön-tanımlı ses adı. Listeden seç; hepsi çok-dilli (Türkçe dahil) çalışır. Boş = varsayılan 'alloy'. NOT: 'marin'/'cedar' YALNIZ GA modeli ('gpt-realtime') ile çalışır; BETA preview modelde geçersiz.",
  },

});
