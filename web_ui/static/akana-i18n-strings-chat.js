/**
 * Akana UI i18n strings — CHAT (core) area. Merges into window.AkanaI18nStrings.
 * Same { en, tr } shape as akana-i18n-strings.js; English-first. Keys: chat.* / msg.*
 */
window.AkanaI18nStrings = Object.assign(window.AkanaI18nStrings || {}, {
  // ── Thinking-mode labels (composer effort selector) ──
  "chat.effort_fast":    { en: "Fast",    tr: "Hızlı"  },
  "chat.effort_normal":  { en: "Normal",  tr: "Normal" },
  "chat.effort_deep":    { en: "Deep",    tr: "Derin"  },
  "chat.effort_intense": { en: "Intense", tr: "Yoğun"  },
  "chat.effort_max":     { en: "Max",     tr: "Azami"  },
  "chat.effort_ultra":   { en: "Ultra",   tr: "Ultra"  },

  // ── Plan-mode button titles ──

  // ── Effort/thinking button aria-label and open-title ──
  "chat.effort_open_title": { en: "Thinking mode — sets the reasoning depth for Claude, Gemini 3+ and OpenAI (o-series/GPT-5+) models; Fast skips planning", tr: "Düşünme modu — Claude, Gemini 3+ ve OpenAI (o-serisi/GPT-5+) modellerinde düşünme/akıl-yürütme kademesini ayarlar; Hızlı planlamayı atlar" },
  "chat.effort_aria":       { en: "Thinking mode: {label}", tr: "Düşünme modu: {label}" },

  // ── Queue chip ──
  "chat.queue_one":  { en: "1 message queued — press Stop to send immediately", tr: "1 mesaj sırada — hemen göndermek için DUR" },
  "chat.queue_many": { en: "{n} messages queued — press Stop to send immediately", tr: "{n} mesaj sırada — hemen göndermek için DUR" },

  // ── Background conversation toast ──
  "chat.bg_response_ready": { en: "{title}: response ready", tr: "{title}: yanıt hazır" },

  // ── Attachment chip ──
  "chat.attach_remove_aria":  { en: "Remove: {name}", tr: "Kaldır: {name}" },
  "chat.attach_remove_title": { en: "Remove", tr: "Kaldır" },

  // ── Attachment provider / size warning toasts ──
  "chat.attach_unreadable": { en: "Active provider ({provider}) cannot read «{name}» — switch to a compatible provider in Settings → LLM", tr: "Aktif sağlayıcı ({provider}) «{name}» dosyasını okuyamıyor — Ayarlar → LLM'den uygun sağlayıcıya geç" },
  "chat.attach_too_big":    { en: "Image «{name}» may be too large for {provider} (>{mb}MB) — resize it or try another provider", tr: "Görsel «{name}» {provider} için büyük olabilir (>{mb}MB) — küçült ya da başka sağlayıcı dene" },

  // ── Upload errors ──
  "chat.upload_too_large":   { en: "«{name}» is too large — exceeds server size limit", tr: "«{name}» çok büyük — sunucu boyut sınırını aşıyor" },
  "chat.upload_invalid_resp":{ en: "Upload response is invalid (image.id missing)", tr: "Yükleme yanıtı geçersiz (image.id yok)" },

  // ── Per-message attachment limit toasts (send-time guard) ──
  "chat.attach_limit_images": { en: "{provider}: at most {max} images per message — {n} attached, remove the extras", tr: "{provider}: mesaj başına en fazla {max} görsel — {n} ekli, fazlasını kaldır" },
  "chat.attach_limit_files":  { en: "{provider}: at most {max} files per message — {n} attached, remove the extras", tr: "{provider}: mesaj başına en fazla {max} dosya — {n} ekli, fazlasını kaldır" },

  // ── Per-message attachment limit toasts (upload-time gate) ──
  "chat.attach_blocked_images": { en: "{provider}: at most {max} images per message — {n} image(s) skipped", tr: "{provider}: mesaj başına en fazla {max} görsel — {n} görsel atlandı" },
  "chat.attach_blocked_files":  { en: "{provider}: at most {max} files per message — {n} file(s) skipped", tr: "{provider}: mesaj başına en fazla {max} dosya — {n} dosya atlandı" },

  // ── Per-conversation attachment limit toast ──
  "chat.attach_conv_limit": { en: "Total attachments in this conversation ({n}/{max} for {provider}) exceed the limit — consider starting a new chat", tr: "Bu konuşmada toplam ek {n}/{max} ({provider}) sınırını aşıyor — yeni sohbet açmayı düşün" },

  // ── Drop overlay ──
  "chat.drop_overlay_text": { en: "Drop here — add to Akana", tr: "Bırak — Akana'ya ekle" },

  // ── Send button aria / title ──
  "chat.send_btn_stop_aria":  { en: "Stop", tr: "Durdur" },
  "chat.send_btn_stop_title": { en: "Stop response", tr: "Yanıtı durdur" },
  "chat.send_btn_send_aria":  { en: "Send", tr: "Gönder" },

  // ── Streaming guard toast ──
  "chat.stream_busy": { en: "Wait for this chat's response to finish — or press Stop and resend.", tr: "Bu sohbetin yanıtı bitsin — ya da DUR'a basıp yeniden gönder." },

  // ── Attachments loading toast ──
  "chat.attachments_uploading": { en: "Uploads in progress…", tr: "Ekler yükleniyor…" },

  // ── Hover action bar (F1 module) ──
  "chat.actionbar_aria":    { en: "Message actions", tr: "Mesaj eylemleri" },
  "chat.copy_btn":          { en: "Copy",        tr: "Kopyala" },
  "chat.copy_btn_title":    { en: "Copy message to clipboard", tr: "Mesajı panoya kopyala" },
  "chat.quote_btn":         { en: "Quote",       tr: "Alıntıla" },
  "chat.quote_btn_title":   { en: "Quote message", tr: "Mesajı alıntıla" },
  "chat.tts_btn_title":      { en: "Read aloud",  tr: "Sesli oku" },
  "chat.tts_btn_stop_title": { en: "Stop reading", tr: "Okumayı durdur" },

  // ── Chat store thread title default ──
  "chat.new_thread_title": { en: "New chat", tr: "Yeni sohbet" },

  // ── Render: arg-key display labels (ARG_KEY_TR) ──
  "msg.arg_file":        { en: "File",         tr: "Dosya" },
  "msg.arg_notebook":    { en: "Notebook",      tr: "Defter" },
  "msg.arg_command":     { en: "Command",       tr: "Komut" },
  "msg.arg_query":       { en: "Query",         tr: "Sorgu" },
  "msg.arg_search":      { en: "Search",        tr: "Arama" },
  "msg.arg_pattern":     { en: "Pattern",       tr: "Desen" },
  "msg.arg_glob":        { en: "Glob",          tr: "Glob" },
  "msg.arg_url":         { en: "URL",           tr: "Adres" },
  "msg.arg_description": { en: "Description",   tr: "Açıklama" },
  "msg.arg_old_text":    { en: "Old text",      tr: "Eski metin" },
  "msg.arg_new_text":    { en: "New text",      tr: "Yeni metin" },
  "msg.arg_input":       { en: "Input",         tr: "Girdi" },
  "msg.arg_server":      { en: "Server",        tr: "Sunucu" },
  "msg.arg_tool":        { en: "Tool",          tr: "Araç" },
  "msg.arg_key":         { en: "Key",           tr: "Anahtar" },
  "msg.arg_text":        { en: "Text",          tr: "Metin" },
  "msg.arg_content":     { en: "Content",       tr: "İçerik" },
  "msg.arg_directory":   { en: "Directory",     tr: "Dizin" },
  "msg.arg_target":      { en: "Target",        tr: "Hedef" },
  "msg.arg_recursive":   { en: "Recursive",     tr: "Özyinelemeli" },
  "msg.arg_background":  { en: "Background",    tr: "Arka planda" },

  // ── formatArgValue ──
  "msg.bool_yes":      { en: "yes",         tr: "evet" },
  "msg.bool_no":       { en: "no",          tr: "hayır" },
  "msg.empty_list":    { en: "(empty list)", tr: "(boş liste)" },
  "msg.empty_obj":     { en: "(empty)",      tr: "(boş)" },

  // ── formatToolArgsBlocks ──
  "msg.cmd_label":           { en: "Command",          tr: "Komut" },
  "msg.running_ellipsis":    { en: "Running…",          tr: "Çalıştırılıyor…" },
  "msg.no_params":           { en: "No parameters",     tr: "Parametre iletilmedi" },
  "msg.input_label":         { en: "Input",             tr: "Girdi" },
  "msg.record_label":        { en: "Record",            tr: "Kayıt" },
  "msg.line_range":          { en: "line {s}–{e}",      tr: "satır {s}–{e}" },
  "msg.line_from":           { en: "line {s}+",         tr: "satır {s}+" },
  "msg.write_content_label": { en: "Content to write",  tr: "Yazılacak içerik" },
  "msg.search_label":        { en: "Search",            tr: "Arama" },
  "msg.scope_label":         { en: "Scope",             tr: "Kapsam" },
  "msg.memory_label":        { en: "Memory",            tr: "Hafıza" },
  "msg.record_ids_label":    { en: "Record IDs",        tr: "Kayıt ID'leri" },
  "msg.record_ids_n":        { en: "{n} items",         tr: "{n} adet" },
  "msg.no_detail":           { en: "No additional detail", tr: "Ek ayrıntı yok" },

  // ── formatWorkspaceResultsBlocks ──
  "msg.no_files_found":   { en: "No matching files found.", tr: "Eşleşen dosya bulunamadı." },

  // ── formatMemoryHitBlocks ──
  "msg.mem_search_label": { en: "Search", tr: "Arama" },
  "msg.mem_no_hits":      { en: "No matching records found in memory.", tr: "Hafızada eşleşen kayıt bulunamadı." },
  "msg.mem_hit_record":   { en: "Record", tr: "Kayıt" },
  "msg.mem_trace":        { en: "{n} records · trace {id}…", tr: "{n} kayıt · izleme {id}…" },

  // ── formatToolResultBlocks ──
  "msg.result_pending":   { en: "Waiting for result…", tr: "Sonuç bekleniyor…" },
  "msg.output_label":     { en: "Output",      tr: "Çıktı" },
  "msg.err_output_label": { en: "Error output", tr: "Hata çıktısı" },

  // ── formatObjectAsKv / formatSearchResultList ──
  "msg.more_fields":   { en: "+{n} more fields", tr: "+{n} alan daha" },
  "msg.more_records":  { en: "… and {n} more records", tr: "… ve {n} kayıt daha" },

  // ── formatShellResult ──
  "msg.exit_code":     { en: "Exit code", tr: "Çıkış kodu" },

  // ── code block fallback lang label ──
  "msg.code_lang_text": { en: "text", tr: "metin" },

  // ── diff block ──
  "msg.diff_removed_label": { en: "Removed",  tr: "Çıkarılan" },
  "msg.diff_added_label":   { en: "Added",    tr: "Eklenen" },
  "msg.diff_more_lines":    { en: "… {n} more lines", tr: "… {n} satır daha" },

  // ── files block ──
  "msg.files_more": { en: "+{n} more", tr: "+{n} daha" },

  // ── hits block ──
  "msg.hits_more":    { en: "+{n} more records", tr: "+{n} kayıt daha" },
  "msg.hit_record_default": { en: "Record", tr: "Kayıt" },

  // ── raw block ──
  "msg.raw_dev_data": { en: "Developer data (JSON)", tr: "Geliştirici verisi (JSON)" },

  // ── toolCallSections labels ──
  "msg.section_input":  { en: "Input",  tr: "Girdi" },
  "msg.section_output": { en: "Output", tr: "Çıktı" },

  // ── toolCallResultChip ──
  "msg.chip_error":  { en: "error",  tr: "hata" },
  "msg.chip_n_results": { en: "{n} results", tr: "{n} sonuç" },
  "msg.chip_n_records": { en: "{n} records", tr: "{n} kayıt" },
  "msg.chip_ok":        { en: "ok",    tr: "tamam" },
  "msg.chip_exit":      { en: "exit {code}", tr: "çıkış {code}" },
  "msg.chip_n_lines":   { en: "{n} lines", tr: "{n} satır" },

  // ── toolCallStatus aria/title ──
  "msg.status_running":   { en: "running",   tr: "çalışıyor" },
  "msg.status_error":     { en: "error",     tr: "hata" },
  "msg.status_done":      { en: "completed", tr: "tamamlandı" },
  "msg.status_running_t": { en: "Running",   tr: "Çalışıyor" },
  "msg.status_error_t":   { en: "Error",     tr: "Hata" },
  "msg.status_done_t":    { en: "Completed", tr: "Tamamlandı" },

  // ── toolCallDurationLabel ──
  "msg.duration_ms": { en: "{n} ms", tr: "{n} ms" },
  "msg.duration_s":  { en: "{n} s",  tr: "{n} sn" },

  // ── TOOL_LABEL_RULES labels ──
  "msg.tool_mem_search":   { en: "Memory search",     tr: "Hafıza araması" },
  "msg.tool_mem_remember": { en: "Memory save",       tr: "Hafızaya kayıt" },
  "msg.tool_mem_forget":   { en: "Memory delete",     tr: "Hafızadan silme" },
  "msg.tool_mem_explain":  { en: "Memory explanation",tr: "Hafıza açıklaması" },
  "msg.tool_mem_mark":     { en: "Memory usage",      tr: "Hafıza kullanımı" },
  "msg.tool_code_search":  { en: "Code search",       tr: "Kod araması" },
  "msg.tool_text_search":  { en: "Text search",       tr: "Metin araması" },
  "msg.tool_file_search":  { en: "File search",       tr: "Dosya araması" },
  "msg.tool_file_read":    { en: "File read",         tr: "Dosya okuma" },
  "msg.tool_file_write":   { en: "File write",        tr: "Dosya yazma" },
  "msg.tool_file_edit":    { en: "File edit",         tr: "Dosya düzenleme" },
  "msg.tool_file_delete":  { en: "File delete",       tr: "Dosya silme" },
  "msg.tool_dir_list":     { en: "Directory listing", tr: "Dizin listeleme" },
  "msg.tool_terminal":     { en: "Terminal command",  tr: "Terminal komutu" },
  "msg.tool_web_search":   { en: "Web search",        tr: "Web araması" },
  "msg.tool_web_read":     { en: "Web page read",     tr: "Web sayfası okuma" },
  "msg.tool_todo":         { en: "Task list",         tr: "Görev listesi" },
  "msg.tool_call":         { en: "Tool call",         tr: "Araç çağrısı" },
  "msg.tool_mode_switch":  { en: "Mode switch",       tr: "Mod değişimi" },
  "msg.tool_image_gen":    { en: "Image generation",  tr: "Görsel üretimi" },
  "msg.tool_await":        { en: "Waiting",           tr: "Bekleme" },
  "msg.tool_mem_generic":  { en: "Memory tool",       tr: "Hafıza aracı" },

  // ── TOOL_ACTION_RULES action sentences ──
  "msg.action_file_read":      { en: "{a} read",          tr: "{a} dosyasını okudu" },
  "msg.action_file_read_gen":  { en: "read file",         tr: "dosya okudu" },
  "msg.action_file_write":     { en: "wrote to {a}",      tr: "{a} dosyasına yazdı" },
  "msg.action_file_write_gen": { en: "wrote file",        tr: "dosya yazdı" },
  "msg.action_file_edit":      { en: "edited {a}",        tr: "{a} dosyasını düzenledi" },
  "msg.action_file_edit_gen":  { en: "edited file",       tr: "dosya düzenledi" },
  "msg.action_file_delete":    { en: "deleted {a}",       tr: "{a} sildi" },
  "msg.action_file_delete_gen":{ en: "deleted file",      tr: "dosya sildi" },
  "msg.action_run_cmd":        { en: "ran command: {a}",  tr: "komut çalıştırdı: {a}" },
  "msg.action_run_cmd_gen":    { en: "ran command",       tr: "komut çalıştırdı" },
  "msg.action_web_search":     { en: "searched '{a}'",    tr: "'{a}' araması yaptı" },
  "msg.action_web_search_gen": { en: "searched the web",  tr: "web araması yaptı" },
  "msg.action_web_read":       { en: "read {a}",          tr: "{a} sayfasını okudu" },
  "msg.action_web_read_gen":   { en: "read web page",     tr: "web sayfası okudu" },
  "msg.action_code_search":    { en: "searched code: {a}",tr: "kod aradı: {a}" },
  "msg.action_code_search_gen":{ en: "searched code",     tr: "kod araması yaptı" },
  "msg.action_text_search":    { en: "searched text: {a}",tr: "metin aradı: {a}" },
  "msg.action_text_search_gen":{ en: "searched text",     tr: "metin araması yaptı" },
  "msg.action_file_search":    { en: "found file: {a}",   tr: "dosya aradı: {a}" },
  "msg.action_file_search_gen":{ en: "searched files",    tr: "dosya araması yaptı" },
  "msg.action_dir_list":       { en: "listed {a}",        tr: "{a} dizinini listeledi" },
  "msg.action_dir_list_gen":   { en: "listed directory",  tr: "dizin listeledi" },
  "msg.action_mem_search":     { en: "searched memory: {a}", tr: "hafızada aradı: {a}" },
  "msg.action_mem_search_gen": { en: "searched memory",   tr: "hafızada aradı" },
  "msg.action_mem_remember":   { en: "saved to memory",   tr: "hafızaya kaydetti" },
  "msg.action_mem_forget":     { en: "removed from memory", tr: "hafızadan sildi" },
  "msg.action_mem_explain":    { en: "explained memory trace", tr: "hafıza izini açıkladı" },
  "msg.action_todo":           { en: "updated task list", tr: "görev listesini güncelledi" },
  "msg.action_mode_switch":    { en: "switched mode",     tr: "mod değiştirdi" },
  "msg.action_image_gen":      { en: "generated image",   tr: "görsel üretti" },
  "msg.action_mem_used":       { en: "used memory: {a}",  tr: "hafızayı kullandı: {a}" },
  "msg.action_mem_used_gen":   { en: "used memory",       tr: "hafızayı kullandı" },
  "msg.action_tool_call_gen":  { en: "called tool",       tr: "araç çağırdı" },
  "msg.action_unknown_tool":   { en: "tool",              tr: "araç" },
  "msg.action_searched":       { en: "searched: {a}",     tr: "aradı: {a}" },
  "msg.action_read_url":       { en: "read {a}",          tr: "{a} sayfasını okudu" },

  // ── shellActionFromCommand sentences ──
  "msg.shell_youtube":    { en: "opened YouTube video", tr: "YouTube videosu açtı" },
  "msg.shell_open_url":   { en: "opened link",          tr: "Bağlantı açtı" },
  "msg.shell_find":       { en: "searched for file",    tr: "Dosya aradı" },
  "msg.shell_which":      { en: "found program path",   tr: "Program yolu aradı" },
  "msg.shell_sysinfo":    { en: "retrieved system info",tr: "Sistem bilgisi aldı" },
  "msg.shell_run_cmd":    { en: "ran command",          tr: "Komut çalıştırdı" },
  "msg.shell_terminal":   { en: "ran terminal command", tr: "Terminal komutu çalıştırdı" },
  "msg.shell_git":        { en: "ran git command",      tr: "git komutu çalıştırdı" },
  "msg.shell_kill":       { en: "stopped process",      tr: "süreç durdurdu" },
  "msg.shell_remove":     { en: "removed files",        tr: "dosya/klasör sildi" },
  "msg.shell_mkdir":      { en: "created folder",       tr: "klasör oluşturdu" },
  "msg.shell_copy":       { en: "copied files",         tr: "dosya kopyaladı" },
  "msg.shell_move":       { en: "moved files",          tr: "dosya taşıdı" },
  "msg.shell_list":       { en: "listed directory",     tr: "dizini listeledi" },
  "msg.shell_read":       { en: "read file",            tr: "dosya okudu" },
  "msg.shell_download":   { en: "fetched URL",          tr: "URL getirdi" },
  "msg.shell_chmod":      { en: "changed permissions",  tr: "izinleri değiştirdi" },
  "msg.shell_ps":         { en: "listed processes",     tr: "süreçleri listeledi" },
  "msg.shell_grep":       { en: "searched text",        tr: "metin aradı" },
  "msg.shell_pkg":        { en: "ran package command",  tr: "paket komutu çalıştırdı" },
  "msg.shell_pip":        { en: "ran pip",              tr: "pip komutu çalıştırdı" },
  "msg.shell_test":       { en: "ran tests",            tr: "testleri çalıştırdı" },
  "msg.shell_script":     { en: "ran script",           tr: "script çalıştırdı" },

  // ── toolCallSubtitle ──
  "msg.records_found": { en: "{n} records found", tr: "{n} kayıt bulundu" },

  // ── refreshToolGroup / renderToolProcessCard labels ──
  "msg.n_tools":        { en: "{n} tools",              tr: "{n} araç" },
  "chat.turn_status_done": { en: "done", tr: "tamam" },
  "chat.turn_status_err":  { en: "error", tr: "hata" },
  "msg.n_tools_errors": { en: "{n} tools · {e} errors", tr: "{n} araç · {e} hata" },

  // ── MEMORY_KIND_TR ──
  "msg.mem_kind_recall":  { en: "query",   tr: "sorgu" },
  "msg.mem_kind_context": { en: "context", tr: "bağlam" },
  "msg.mem_kind_staging": { en: "draft",   tr: "taslak" },

  // ── renderMemoryUse titles ──
  "msg.mem_from_one":  { en: "Response from memory",    tr: "Hafızadan yanıt" },
  "msg.mem_from_n":    { en: "{n} records from memory used", tr: "Hafızadan {n} kayıt kullanıldı" },

  // ── renderMemoryUse section labels ──
  "msg.mem_section_recall":   { en: "Query",         tr: "Sorgu" },
  "msg.mem_section_context":  { en: "Context {n}",   tr: "Bağlam {n}" },
  "msg.mem_section_record":   { en: "Record",        tr: "Kayıt" },
  "msg.mem_badge_record":     { en: "record",        tr: "kayıt" },

  // ── renderSkillUse titles ──
  "msg.skill_one":   { en: "Skill: {title}", tr: "Skill: {title}" },
  "msg.skill_n":     { en: "{n} skills used", tr: "{n} skill kullanıldı" },

  // ── SKILL_STATUS_TR ──
  "msg.skill_injected":         { en: "used",             tr: "kullanıldı" },

  // ── renderSourcesRow / appendMemorySources ──
  "msg.sources_label":      { en: "Sources",      tr: "Kaynaklar" },
  "msg.mem_recall_chip":    { en: "memory · {q}", tr: "anı · {q}" },
  "msg.mem_recall_chip_gen":{ en: "memory · query", tr: "anı · sorgu" },

  // ── renderApprovalCard ──
  "msg.approval_title":   { en: "Approval required", tr: "Onay gerekiyor" },
  "msg.approval_badge":   { en: "risky · approval",  tr: "riskli · onay" },
  "msg.approval_once":    { en: "Don't ask again this session", tr: "Bu oturumda bir daha sorma" },
  "msg.approval_deny":    { en: "Deny",    tr: "Reddet" },
  "msg.approval_allow":   { en: "Allow",   tr: "İzin ver" },
  "msg.approval_allowed": { en: "Allowed — operation in progress.", tr: "İzin verildi — işlem sürüyor." },
  "msg.approval_denied":  { en: "Denied — operation cancelled.",    tr: "Reddedildi — işlem iptal edildi." },

  // ── renderAskUserCard ──
  "msg.ask_title_one": { en: "Akana is asking a question",       tr: "Akana soruyor" },
  "msg.ask_title_n":   { en: "Akana is asking {n} questions",    tr: "Akana {n} soru soruyor" },
  "msg.ask_badge":     { en: "awaiting response",                tr: "yanıt bekleniyor" },
  "msg.ask_opt_empty": { en: "(option)",                         tr: "(seçenek)" },
  "msg.ask_free_multi":{ en: "Other (comma-separated)…",        tr: "Başka (virgülle ayır)…" },
  "msg.ask_free_single":{ en: "Your answer…",                   tr: "Kendi yanıtın…" },
  "msg.ask_hint_multi": { en: "You may choose multiple options or type your own answer.", tr: "Birden çok seçebilir veya kendi yanıtını yazabilirsin." },
  "msg.ask_hint_one":   { en: "Choose an option or type your own answer.", tr: "Bir seçenek seç veya kendi yanıtını yaz." },
  "msg.ask_submit":     { en: "Send",  tr: "Gönder" },
  "msg.ask_answered":   { en: "Your answer sent: {answer}", tr: "Yanıtın gönderildi: {answer}" },

  // ── renderPlanCard ──
  "msg.plan_title":        { en: "Akana is presenting a plan",      tr: "Akana bir plan sunuyor" },
  "msg.plan_badge":        { en: "awaiting approval",               tr: "onay bekleniyor" },
  "msg.plan_revise_ph":    { en: "What should we change in the plan?…", tr: "Planda neyi değiştirelim?…" },
  "msg.plan_hint":         { en: "Apply the plan or request a revision.", tr: "Planı uygula ya da düzeltme iste." },
  "msg.plan_revise_btn":   { en: "Revise",  tr: "Düzelt" },
  "msg.plan_apply_btn":    { en: "Apply",   tr: "Uygula" },
  "msg.plan_applying":     { en: "Applying plan…", tr: "Plan uygulanıyor…" },
  "msg.plan_revised":      { en: "Revision requested: {txt}", tr: "Düzeltme istendi: {txt}" },

  // ── renderErrorCard ──
  "msg.err_default_title": { en: "An error occurred", tr: "Bir hata oluştu" },
  "msg.err_retry":         { en: "Retry",              tr: "Yeniden dene" },

  // ── decorateCodeBlock ──
  "msg.code_copy_btn":       { en: "Copy",         tr: "Kopyala" },
  "msg.code_copy_aria":      { en: "Copy code to clipboard", tr: "Kodu panoya kopyala" },
  "msg.code_preview_title":  { en: "Live preview", tr: "Canlı önizleme" },
  "msg.code_preview_aria":   { en: "Live preview", tr: "Canlı önizleme" },
  "msg.code_preview_span":   { en: "Preview",      tr: "Önizle" },
  "msg.code_lang_fallback":  { en: "code",         tr: "kod" },

  // ── flashCopyFeedback ──
  "msg.copy_ok":   { en: "Copied ✓",     tr: "Kopyalandı ✓" },
  "msg.copy_fail": { en: "Copy failed",  tr: "Kopyalanamadı" },

  // ── tts wave chip ──
  "msg.tts_playing": { en: "Playing voice response", tr: "Sesli yanıt çalıyor" },

  // ── renderAssistantFromPersist ──
  "msg.dropped_turns": { en: "⚠ {n} old messages were dropped from history — the model can no longer see that part", tr: "⚠ {n} eski mesaj geçmişten düştü — model artık o kısmı göremiyor" },
  "msg.empty_bubble":  { en: "(empty)", tr: "(boş)" },

  // ── formatHistoryMeta token count ──
  "msg.history_tokens": { en: "{n} tok", tr: "{n} tok" },

  // ── deepUnwrapPayload error fallback ──
  "msg.err_tool_error": { en: "Tool error", tr: "Araç hatası" },

  // ── Akana native MCP tool action sentences ──
  "msg.action_reminder_set":    { en: "set reminder: {a}",    tr: "hatırlatma kurdu: {a}" },
  "msg.action_reminder_gen":    { en: "set reminder",         tr: "hatırlatma kurdu" },
  "msg.action_persona_switch":  { en: "switched persona: {a}", tr: "kişiliği değiştirdi: {a}" },
  "msg.action_persona_gen":     { en: "switched persona",     tr: "kişiliği değiştirdi" },
  "msg.action_flow_run":        { en: "ran flow: {a}",        tr: "akış çalıştırdı: {a}" },
  "msg.action_flow_gen":        { en: "ran flow",             tr: "akış çalıştırdı" },
  "msg.action_profile_show":    { en: "showed profile",       tr: "profili gösterdi" },
  "msg.action_trust_show":      { en: "showed trust level",   tr: "güven düzeyini gösterdi" },
  "msg.action_knowledge_teach": { en: "taught knowledge: {a}", tr: "bilgi öğretti: {a}" },
  "msg.action_knowledge_gen":   { en: "taught knowledge",     tr: "bilgi öğretti" },
  "msg.action_history_show":    { en: "showed history",       tr: "geçmişi gösterdi" },
  "msg.action_awaited":         { en: "waited",               tr: "bekledi" },

  // ── chatRenderMessage ──
  "msg.err_label": { en: "Error", tr: "Hata" },

  // ── attachCopyButton (action-card panel copy) ──
  "msg.panel_copy_btn":   { en: "Copy",         tr: "Kopyala" },
  "msg.panel_copy_title": { en: "Copy to clipboard", tr: "Panoya kopyala" },

  // ── makeCopyIconButton (code-shell copy icon) ──
  "msg.code_icon_copy_title": { en: "Copy to clipboard", tr: "Panoya kopyala" },
  "msg.code_icon_copy_aria":  { en: "Copy to clipboard", tr: "Panoya kopyala" },

  // ── Todo card ──
  "msg.todo_card_title": { en: "Task list", tr: "Görev listesi" },

  // ── Subagent (Task) group ──
  "msg.subagent_title":    { en: "Subagent · {name}", tr: "Alt ajan · {name}" },
  "msg.subagent_fallback": { en: "Subagent",          tr: "Alt ajan" },
  "msg.subagent_working":  { en: "Working…",          tr: "Çalışıyor…" },

  // ── dir listing meta ──
  "msg.dir_meta": { en: "directory", tr: "dizin" },
});
