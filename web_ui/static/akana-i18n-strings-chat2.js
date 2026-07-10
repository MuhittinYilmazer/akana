/**
 * Akana UI i18n strings — CHAT (transport/threads/archive) area.
 * Merges into window.AkanaI18nStrings. { en, tr }, English-first. Keys: chat.* / thread.*
 */
window.AkanaI18nStrings = Object.assign(window.AkanaI18nStrings || {}, {
  // ── threads.js ───────────────────────────────────────────────────────────────
  "thread.new_chat_title":           { en: "New chat",      tr: "Yeni sohbet" },
  "thread.action.task_route":        { en: "Planner: request routed to background task",       tr: "Planner: istek arka plan görevine yönlendirildi" },
  "thread.action.teach_draft":       { en: "Teaching draft created — awaiting your approval",  tr: "Öğretme taslağı oluşturuldu — onayınız bekleniyor" },
  "thread.action.teach_failed":      { en: "Teaching failed — see response details",           tr: "Öğretme başarısız — yanıt detayına bakın" },
  "thread.toast.session_started":    { en: "New session started",   tr: "Yeni oturum başlatıldı" },
  "thread.toast.chat_archived":      { en: "Chat archived",         tr: "Sohbet arşivlendi" },
  "thread.toast.chat_deleted":       { en: "Chat deleted",          tr: "Sohbet silindi" },
  "thread.toast.chat_cleared":       { en: "Chat cleared",          tr: "Sohbet temizlendi" },
  "thread.toast.already_new_chat":   { en: "Already a new chat",    tr: "Zaten yeni sohbet" },
  "thread.toast.new_chat_started":   { en: "New chat started",      tr: "Yeni sohbet başlatıldı" },
  "thread.confirm.clear_chat":       { en: "Clear the current chat?",                          tr: "Mevcut sohbet temizlensin mi?" },
  "thread.confirm.delete_chat":      { en: "Permanently delete «{title}»?\nThis cannot be undone.", tr: "«{title}» kalıcı olarak silinsin mi?\nBu işlem geri alınamaz." },
  "thread.notice.load_failed":       { en: "Chat could not be loaded. Check connection and session.", tr: "Sohbet yüklenemedi. Bağlantıyı ve oturumu kontrol edin." },
  "thread.memory_stat.none":         { en: "none",   tr: "yok" },

  // ── archive.js ───────────────────────────────────────────────────────────────
  "archive.activity.responding_queued": { en: "{n} queued · responding",  tr: "{n} sırada · yanıtlıyor" },
  "archive.activity.responding":        { en: "Responding",               tr: "Yanıtlıyor" },
  "archive.activity.queued":            { en: "{n} queued",               tr: "{n} sırada" },

  "archive.time.just_now":   { en: "just now",      tr: "az önce" },
  "archive.time.minutes":    { en: "{n} min ago",   tr: "{n} dk önce" },
  "archive.time.hours":      { en: "{n} hr ago",    tr: "{n} sa önce" },
  "archive.time.days":       { en: "{n} days ago",  tr: "{n} gün önce" },

  "archive.msg_count":       { en: "{n} messages",  tr: "{n} mesaj" },

  "archive.toast.title_updated":      { en: "Title updated",           tr: "Başlık güncellendi" },
  "archive.toast.unpinned":           { en: "Pin removed",             tr: "Sabitleme kaldırıldı" },
  "archive.toast.pinned":             { en: "Chat pinned",             tr: "Sohbet sabitlendi" },
  "archive.toast.restored":           { en: "Chat restored",           tr: "Sohbet geri yüklendi" },
  "archive.toast.no_saved_chat":      { en: "No saved chat",           tr: "Kayıtlı sohbet yok" },
  "archive.toast.markdown_saved":     { en: "Markdown downloaded",     tr: "Markdown indirildi" },

  "archive.telegram.toast.not_configured":      { en: "Telegram isn't set up yet — add a chat in Settings first.", tr: "Telegram henüz kurulmadı — önce Ayarlar'dan bir sohbet ekleyin." },
  "archive.telegram.toast.invalid_chat":        { en: "That chat id isn't on the Telegram allowlist.",              tr: "Bu sohbet kimliği Telegram izin listesinde değil." },
  "archive.telegram.toast.bound":               { en: "Connected — continue this chat on Telegram.",                tr: "Bağlandı — bu sohbete Telegram'dan devam edebilirsiniz." },
  "archive.telegram.toast.bound_not_notified":  { en: "Connected, but the Telegram confirmation couldn't be sent.", tr: "Bağlandı, ancak Telegram onay mesajı gönderilemedi." },
  "archive.telegram.prompt.choose_chat":        { en: "Multiple Telegram chats are allowed. Enter the chat id to use:\n{ids}", tr: "Birden fazla Telegram sohbetine izin verilmiş. Kullanılacak sohbet kimliğini girin:\n{ids}" },

  "archive.btn.unpin":       { en: "Unpin",          tr: "Sabiti kaldır" },
  "archive.btn.pin":         { en: "Pin",             tr: "Sabitle" },
  "archive.btn.rename":      { en: "Rename",          tr: "Yeniden adlandır" },
  "archive.btn.restore":     { en: "Restore",         tr: "Geri yükle" },
  "archive.btn.archive":     { en: "Archive",         tr: "Arşivle" },
  "archive.btn.delete":      { en: "Delete",          tr: "Sil" },
  "archive.btn.menu":        { en: "Actions",         tr: "İşlemler" },
  "archive.btn.menu_aria":   { en: "Chat actions",    tr: "Sohbet işlemleri" },

  "archive.section.today":          { en: "TODAY",           tr: "BUGÜN" },
  "archive.section.this_week":      { en: "THIS WEEK",       tr: "BU HAFTA" },
  "archive.section.older":          { en: "OLDER",           tr: "DAHA ESKİ" },
  "archive.section.pinned":         { en: "Pinned",          tr: "Sabitlenen" },
  "archive.section.search_results": { en: "Search results",  tr: "Arama sonuçları" },
  "archive.section.archived":       { en: "Archive",         tr: "Arşiv" },

  "archive.empty.no_results":       { en: "No search results",                       tr: "Arama sonucu yok" },
  "archive.empty.archived":         { en: "No chats in archive",                     tr: "Arşivde sohbet yok" },
  "archive.empty.none":             { en: "No saved chats yet — start a new chat",   tr: "Henüz kayıtlı sohbet yok — yeni sohbet başlat" },
  "archive.empty.loading":          { en: "Loading…",                                tr: "Yükleniyor…" },
  "archive.empty.load_error":       { en: "Could not load list",                     tr: "Liste yüklenemedi" },
  "archive.empty.conn_error":       { en: "Connection error",                        tr: "Bağlantı hatası" },

  "archive.toggle.open":    { en: "Open chat list",    tr: "Sohbet listesini aç" },
  "archive.toggle.close":   { en: "Close chat list",   tr: "Sohbet listesini kapat" },

  "archive.prompt.new_title":   { en: "New title",      tr: "Yeni başlık" },

  "archive.pin_btn.unpin":  { en: "Unpin",  tr: "Sabiti kaldır" },
  "archive.pin_btn.pin":    { en: "Pin",    tr: "Sabitle" },

  "archive.error.messages_load": { en: "Could not load messages",  tr: "Mesajlar yüklenemedi" },
  "archive.error.delete_failed": { en: "Could not delete ({code})", tr: "Silinemedi ({code})" },

  "archive.export.role_user":      { en: "You",    tr: "Sen" },
  "archive.export.role_assistant": { en: "Akana",  tr: "Akana" },

  "archive.default_title":  { en: "New chat",  tr: "Yeni sohbet" },

  // ── transport.js ─────────────────────────────────────────────────────────────
  "transport.tool.cancel":          { en: "Cancel",       tr: "İptal et" },
  "transport.tool.cancelling":      { en: "Cancelling…",  tr: "İptal ediliyor…" },
  "transport.tool.fallback":        { en: "tool",         tr: "araç" },

  "transport.process.working":      { en: "Working…",   tr: "Çalışıyor…" },
  "transport.process.live":         { en: "live",        tr: "canlı" },
  "transport.process.label":        { en: "process",     tr: "süreç" },
  "transport.process.thought_n":    { en: "thought in {n} steps",  tr: "{n} adımda düşündü" },
  "transport.process.tool_n":       { en: "{n} tool",              tr: "{n} araç" },
  "transport.process.tasks_n":      { en: "{done}/{total} tasks",  tr: "{done}/{total} görev" },

  "transport.tokens.label":         { en: "tok",  tr: "tok" },
  "transport.hud.tps":              { en: "{n} tok/s", tr: "{n} tok/sn" },

  "transport.approval.intent_system": { en: "System action approval",  tr: "Sistem işlemi onayı" },
  "transport.approval.intent_other":  { en: "Action approval required", tr: "İşlem onayı gerekiyor" },
  "transport.approval.badge":         { en: "risky · approval",         tr: "riskli · onay" },
  "transport.approval.detail":        { en: "This request is asking for permission in Approved mode. Click «Allow» to proceed or «Deny» to cancel.", tr: "Bu istek Onaylı modda izin istiyor. Devam etmek için «İzin ver», vazgeçmek için «Reddet»." },
  "transport.approval.allow_reply":   { en: "approve",  tr: "onayla" },
  "transport.approval.deny_reply":    { en: "reject",   tr: "reddet" },
  "transport.approval.meta_tag":      { en: " · approval",  tr: " · onay" },

  "transport.plan.approve_reply":     { en: "Apply the plan.",  tr: "Planı uygula." },

  "transport.activity.summary_start":  { en: "Preparing summary…",   tr: "Özet hazırlanıyor…" },
  "transport.activity.summary_done":   { en: "Summary completed",     tr: "Özet tamamlandı" },
  "transport.activity.step_start":     { en: "Step started",          tr: "Adım başladı" },
  "transport.activity.step_done":      { en: "Step done",             tr: "Adım bitti" },

  "transport.err_card.pack_title":     { en: "Pack permission required",   tr: "Pack yetkisi gerekiyor" },
  "transport.err_card.generic_title":  { en: "Request could not be completed", tr: "İstek tamamlanamadı" },
  "transport.err_card.pack_settings":  { en: "Open Pack settings",          tr: "Pack ayarlarını aç" },

  "transport.toast.memory_staged":   { en: "Added to Inbox ({keys}) — approve in Memory",  tr: "Inbox'a eklendi ({keys}) — Hafıza'dan onaylayın" },
  "transport.toast.memory_stored":   { en: "Saved to memory ({keys})",                     tr: "Hafızaya kaydedildi ({keys})" },
  "transport.toast.memory_key_fallback": { en: "info", tr: "bilgi" },
  "transport.toast.queued":          { en: "Message queued",  tr: "Mesaj sıraya alındı" },

  "transport.stream.empty_response": { en: "Model returned an empty response (no text or tool call). Retry; if it persists, the active provider/session may be faulty (Settings → LLM).", tr: "Model boş yanıt döndürdü (metin de araç çağrısı da yok). Tekrar dene; sürerse aktif sağlayıcı/oturum sorunlu olabilir (Ayarlar → LLM)." },
  "transport.stream.history_drop":   { en: "⚠ {n} old messages dropped from history — model can no longer see that part", tr: "⚠ {n} eski mesaj geçmişten düştü — model artık o kısmı göremiyor" },
  "transport.stream.session_renewed_n": { en: "ℹ Session renewed — last {n} messages loaded as context", tr: "ℹ Oturum yenilendi — son {n} mesaj bağlam olarak yüklendi" },
  "transport.stream.session_renewed":   { en: "ℹ Session renewed — chat context rebuilt from server",   tr: "ℹ Oturum yenilendi — sohbet bağlamı sunucudan yeniden kuruldu" },
  "transport.stream.disconnected":   { en: "⚠ Disconnected: {msg}",  tr: "⚠ Kesildi: {msg}" },

  "transport.stuck.recovered":        { en: "Previous turn cancelled — you can send your message again.",                    tr: "Önceki tur iptal edildi — mesajınızı tekrar gönderebilirsiniz." },
  "transport.stuck.may_still_run":    { en: "Previous turn may still be running — wait a few seconds and try again.",        tr: "Önceki tur hâlâ sürebilir — birkaç saniye bekleyip tekrar deneyin." },

  "transport.queue.cannot_enqueue":   { en: "Message could not be queued — check server connection.", tr: "Mesaj sıraya alınamadı — sunucu bağlantısını kontrol edin." },
  "transport.queue.cannot_enqueue_msg": { en: "Message could not be queued: {msg}", tr: "Mesaj sıraya alınamadı: {msg}" },
  "transport.queue.no_connection":    { en: "Connection to server lost. Restart the Akana server (make start), refresh the page, and make sure CURSOR_API_KEY is set in .env.", tr: "Sunucu bağlantısı kesildi. Akana sunucusunu yeniden başlatın (make start), sayfayı yenileyin ve CURSOR_API_KEY .env dosyasında tanımlı olsun." },
  "transport.queue.no_response":      { en: "No response received — connection closed before a response arrived. Try again.", tr: "Yanıt alınamadı — bağlantı bir yanıt gelmeden kapandı. Tekrar deneyin." },
  "transport.unknown_error":          { en: "Unknown error",  tr: "Bilinmeyen hata" },

  "transport.blocking.empty":         { en: "(empty)",  tr: "(boş)" },
});
