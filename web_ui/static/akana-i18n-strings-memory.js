/**
 * Akana UI i18n strings — MEMORY STUDIO area (memory.html + render/studio js).
 * Merges into window.AkanaI18nStrings. { en, tr }, English-first. Keys: memory.*
 */
window.AkanaI18nStrings = Object.assign(window.AkanaI18nStrings || {}, {

  // ── Render: trust badge labels ────────────────────────────────────────────
  "memory.trust_user_label":      { en: "user statement",  tr: "kullanıcı beyanı" },
  "memory.trust_user_title":      { en: "Directly stated by you", tr: "Doğrudan sizin söylediğiniz" },
  "memory.trust_inferred_label":  { en: "inferred",        tr: "çıkarım" },
  "memory.trust_inferred_title":  { en: "Inferred from conversation", tr: "Sohbetten çıkarıldı" },
  "memory.trust_tool_label":      { en: "tool output",     tr: "araç çıktısı" },
  "memory.trust_tool_title":      { en: "Came from a tool run", tr: "Bir araç çalıştırmasından geldi" },
  "memory.trust_synth_label":     { en: "synthesis",       tr: "sentez" },
  "memory.trust_synth_title":     { en: "Summarised from multiple sources", tr: "Birden çok kaynaktan özetlendi" },
  "memory.trust_pct_title":       { en: "Confidence in source", tr: "Kaynağa duyulan güven" },
  "memory.trust_pct_label":       { en: "confidence {n}%", tr: "güven %{n}" },

  // ── Render: source-origin badge (provenance) ──────────────────────────────
  "memory.origin_user_label":     { en: "statement",  tr: "beyan" },
  "memory.origin_user_title":     { en: "Directly stated by you", tr: "Doğrudan sizin söylediğiniz" },
  "memory.origin_inferred_label": { en: "inferred",   tr: "çıkarım" },
  "memory.origin_inferred_title": { en: "Inferred from conversation", tr: "Sohbetten çıkarıldı" },
  "memory.origin_tool_label":     { en: "tool",       tr: "araç" },
  "memory.origin_tool_title":     { en: "Came from a tool run", tr: "Bir araç çalıştırmasından geldi" },
  "memory.origin_synth_label":    { en: "synthesis",  tr: "sentez" },
  "memory.origin_synth_title":    { en: "Summarised from multiple sources", tr: "Birden çok kaynaktan özetlendi" },
  "memory.origin_legacy_label":   { en: "legacy",     tr: "eski kayıt" },
  "memory.origin_legacy_title":   { en: "Pre-source-field record — origin unknown", tr: "Kaynak alanı öncesi kayıt — kökeni bilinmiyor" },
  "memory.origin_generic_title":  { en: "Source type", tr: "Kaynak türü" },
  "memory.origin_source_btn_suffix": { en: " — click to see source", tr: " — kaynağı görmek için tıklayın" },

  // ── Render: provenance popover ────────────────────────────────────────────
  "memory.provenance_aria":       { en: "Source detail",      tr: "Kaynak detayı" },
  "memory.provenance_heading":    { en: "Where did this come from?", tr: "Bu nereden geldi?" },
  "memory.provenance_close_aria": { en: "Close",              tr: "Kapat" },
  "memory.provenance_row_origin": { en: "Origin",             tr: "Köken" },
  "memory.provenance_row_detail": { en: "Detail",             tr: "Detay" },
  "memory.provenance_row_observed": { en: "Observed",         tr: "Gözlem" },

  // ── Render: inline badges ─────────────────────────────────────────────────
  "memory.badge_invalid":         { en: "invalidated", tr: "geçersiz" },
  "memory.badge_invalid_title":   { en: "Invalidated: {ts}", tr: "Geçersiz kılındı: {ts}" },
  "memory.badge_salience_title":  { en: "Times recalled",     tr: "Recall'da kaç kez çağrıldı" },
  "memory.badge_salience_last":   { en: "Last used: {ts}",    tr: "Son kullanım: {ts}" },
  "memory.badge_salience_label":  { en: "↑{n} uses",          tr: "↑{n} kez kullanıldı" },
  "memory.badge_score_label":     { en: "score {n}",          tr: "skor {n}" },
  "memory.badge_score_bar_title": { en: "score {n}",          tr: "skor {n}" },

  // ── Render: recall trace badges ───────────────────────────────────────────
  "memory.trace_strategy_label":  { en: "strategy: {s}",      tr: "strateji: {s}" },
  "memory.trace_vector_on":       { en: "vector ✓",           tr: "vektör ✓" },
  "memory.trace_vector_off":      { en: "no vector",          tr: "vektör yok" },
  "memory.trace_vector_on_title": { en: "Semantic search active", tr: "Semantic arama devrede" },
  "memory.trace_vector_off_title":{ en: "Keyword / FTS only", tr: "Yalnızca anahtar kelime/FTS" },

  // ── Render: inbox item ────────────────────────────────────────────────────
  "memory.inbox_key_default":     { en: "note",    tr: "not" },
  "memory.inbox_source_label":    { en: "source: {id}", tr: "kaynak: {id}" },
  "memory.inbox_reason_prefix":   { en: "Reason: {reason}", tr: "Sebep: {reason}" },
  "memory.inbox_approve_btn":     { en: "Approve", tr: "Onayla" },
  "memory.inbox_reject_btn":      { en: "Reject",  tr: "Reddet" },

  // ── Render: fact card ─────────────────────────────────────────────────────
  "memory.fact_key_default":      { en: "note",    tr: "not" },
  "memory.fact_edit_btn":         { en: "Edit",    tr: "Düzenle" },
  "memory.fact_delete_btn":       { en: "Delete",  tr: "Sil" },

  // ── Render: fact editor ───────────────────────────────────────────────────
  "memory.editor_value_aria":     { en: "New value",    tr: "Yeni değer" },
  "memory.editor_supersede_label":{ en: "Replace",      tr: "Yerine yaz" },
  "memory.editor_supersede_hint": { en: "Fact changed — old record stays in history", tr: "Bilgi değişti — eski kayıt geçmişte kalır" },
  "memory.editor_correct_label":  { en: "Correct wording", tr: "Yazım düzelt" },
  "memory.editor_correct_hint":   { en: "Same fact, just rephrasing", tr: "Aynı bilgi, sadece ifade düzeltiliyor" },
  "memory.editor_save_btn":       { en: "Save",         tr: "Kaydet" },
  "memory.editor_cancel_btn":     { en: "Cancel",       tr: "Vazgeç" },

  // ── Render: timeline ──────────────────────────────────────────────────────
  "memory.timeline_title_default":{ en: "event",        tr: "olay" },
  "memory.timeline_session_title":{ en: "Session: {id}", tr: "Oturum: {id}" },

  // ── Studio: loading / empty / error states ────────────────────────────────
  "memory.state_loading":         { en: "Loading…",     tr: "Yükleniyor…" },
  "memory.state_searching":       { en: "Searching…",   tr: "Aranıyor…" },

  "memory.timeline_empty":        { en: "No activity yet. Start chatting and approving facts to see a feed here.", tr: "Henüz aktivite yok. Sohbet ettikçe ve bilgi onayladıkça burada akış belirir." },
  "memory.timeline_error":        { en: "Could not load activity: {err}", tr: "Aktivite yüklenemedi: {err}" },

  "memory.inbox_empty_msg":       { en: "No pending records. Say «remember …» in chat to add candidates; run «Memory Maintenance» for automatic suggestions.", tr: "Onay bekleyen kayıt yok. Sohbette «hatırla …» dediğinizde adaylar buraya düşer; otomatik öneri için «Hafıza Bakımı»nı çalıştırın." },
  "memory.inbox_error":           { en: "Could not load Inbox: {err}", tr: "Inbox yüklenemedi: {err}" },
  "memory.inbox_empty_after":     { en: "No pending records left.", tr: "Onay bekleyen kayıt kalmadı." },
  "memory.inbox_approve_all":     { en: "Approve all", tr: "Hepsini onayla" },
  "memory.inbox_reject_all":      { en: "Reject all", tr: "Hepsini reddet" },
  "memory.inbox_reject_all_confirm": { en: "Reject all {n} pending records? This can't be undone.", tr: "Bekleyen {n} kaydın hepsi reddedilsin mi? Bu geri alınamaz." },

  "memory.facts_empty_query":     { en: "No results for «{q}». Simplify your search.", tr: "«{q}» için sonuç yok. Aramayı sadeleştirin." },
  "memory.facts_empty_filtered":  { en: "No facts match this filter. Clear filters and try again.", tr: "Bu filtreyle eşleşen bilgi yok. Filtreleri temizleyip tekrar bakın." },
  "memory.facts_empty_none":      { en: "No permanent facts yet — add one below or approve from Inbox.", tr: "Henüz kalıcı bilgi yok — aşağıdaki «Add fact» ile ekleyin veya Inbox'tan onaylayın." },
  "memory.facts_error":           { en: "Could not load facts: {err}", tr: "Bilgiler yüklenemedi: {err}" },
  "memory.facts_prev":            { en: "Previous", tr: "Önceki" },
  "memory.facts_next":            { en: "Next", tr: "Sonraki" },
  "memory.facts_page_status":     { en: "{from}–{to} of {total}", tr: "{from}–{to} / {total}" },

  "memory.recall_empty_hint":     { en: "Enter a query — test what the assistant will recall from memory.", tr: "Sorgu yazın — asistanın hafızadan ne çağıracağını burada test edin." },
  "memory.recall_empty_noresult": { en: "No match for «{q}». Try different words or enable vector (semantic) search in Settings.", tr: "«{q}» için eşleşme yok. Farklı sözcüklerle deneyin ya da Ayarlar'dan vektör (semantik) aramayı açın." },
  "memory.recall_error":          { en: "Search failed: {err}", tr: "Arama başarısız: {err}" },

  // ── Studio: vector status note ────────────────────────────────────────────
  "memory.vector_active":         { en: "ON · {backend}",  tr: "AÇIK · {backend}" },
  "memory.vector_off_mode":       { en: "off (mode: off)", tr: "kapalı (mod: off)" },
  "memory.vector_inactive":       { en: "OFF · {backend} missing", tr: "KAPALI · {backend} yok" },

  // ── Studio: toast messages ────────────────────────────────────────────────
  "memory.toast_approved":            { en: "Approved — moved to permanent facts", tr: "Onaylandı — kalıcı bilgiye taşındı" },
  "memory.toast_rejected":            { en: "Rejected", tr: "Reddedildi" },
  "memory.toast_item_failed":         { en: "Could not process: {err}", tr: "İşlenemedi: {err}" },
  "memory.toast_approved_all":        { en: "Approved all — {n} moved to permanent facts", tr: "Hepsi onaylandı — {n} kayıt kalıcı bilgiye taşındı" },
  "memory.toast_rejected_all":        { en: "Rejected all — {n} dropped", tr: "Hepsi reddedildi — {n} kayıt atıldı" },
  "memory.toast_bulk_partial":        { en: "Done — {ok} succeeded, {fail} failed", tr: "Bitti — {ok} başarılı, {fail} başarısız" },
  "memory.toast_correct_done":        { en: "Wording corrected", tr: "Yazım düzeltildi" },
  "memory.toast_supersede_done":      { en: "Replaced", tr: "Yerine yazıldı" },
  "memory.toast_update_failed":       { en: "Could not update: {err}", tr: "Güncellenemedi: {err}" },
  "memory.toast_deleted":             { en: "Fact deleted", tr: "Bilgi silindi" },
  "memory.toast_delete_failed":       { en: "Could not delete: {err}", tr: "Silinemedi: {err}" },
  "memory.toast_fact_saved":          { en: "Fact saved", tr: "Bilgi kaydedildi" },
  "memory.toast_save_settings":       { en: "Memory settings saved", tr: "Hafıza ayarları kaydedildi" },
  "memory.toast_no_chat_msg":         { en: "No user message to capture", tr: "Eklenecek kullanıcı mesajı yok" },

  // ── Studio: status / form messages ───────────────────────────────────────
  "memory.status_saving":             { en: "Saving…",          tr: "Kaydediliyor…" },
  "memory.status_saved":              { en: "Saved.",            tr: "Kaydedildi." },
  "memory.status_value_empty":        { en: "Value cannot be empty.", tr: "Değer boş olamaz." },
  "memory.status_value_empty_toast":  { en: "Value cannot be empty", tr: "Değer boş olamaz" },
  "memory.status_settings_loaded_err":{ en: "Could not load settings: {err}", tr: "Ayarlar yüklenemedi: {err}" },
  "memory.status_settings_saved":     { en: "Settings saved.", tr: "Ayarlar kaydedildi." },
  "memory.status_settings_save_err":  { en: "Could not save: {err}", tr: "Kaydedilemedi: {err}" },
  "memory.status_prefill_hint":       { en: "Pre-filled from chat — edit and click «Save».", tr: "Sohbetten dolduruldu — düzenleyip «Kaydet» deyin." },

  // ── Studio: confirm dialogs ───────────────────────────────────────────────
  "memory.confirm_delete_fact":       { en: "Delete «{label}»?", tr: "«{label}» silinsin mi?" },

  // ── Studio: settings error via studio load ────────────────────────────────
  "memory.short_id_none":             { en: "none", tr: "yok" },
});
