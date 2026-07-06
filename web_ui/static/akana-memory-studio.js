/**
 * Akana Memory Studio — UI for the unified memory layer (memory API).
 * Tabs: Inbox (approval queue) · Facts (fact CRUD) · Recall (test) · Settings.
 * HTTP: akana-memory-api.js (AkanaMemoryApi) · DOM generation: akana-memory-render.js.
 * This file is also loaded on the chat page; only bridge functions run there.
 */
(() => {
  const t = (key, vars) => window.AkanaI18n ? window.AkanaI18n.t(key, vars) : key;

  const LS_MEMORY_PREFILL = "akana.memoryPrefill";
  const isMemoryStudioPage = document.body.classList.contains("memory-studio-page");
  const VIEWS = ["overview", "inbox", "facts", "recall", "settings"];
  // Legacy route names are redirected to the new simple views (graph/insight removed).
  const LEGACY_VIEWS = { knowledge: "facts", tools: "settings", inbox: "inbox" };

  let hooks = {
    conversationIdForMemory: () => "",
    chatActiveThread: () => null,
    shortConversationId: (id) => (id ? String(id) : t("memory.short_id_none")),
    msg: null,
    showToast: (m, k) => window.AkanaCore.showToast(m, k),
  };
  let _wired = false;
  let currentView = "overview";
  let factsQueryTimer = null;
  const FACTS_PAGE_SIZE = 50; // browse-mode page size for the Prev/Next pager
  let factsOffset = 0; // current page offset (browse mode only; search ignores it)
  let factsRequestSeq = 0; // guards against an older overlapping loadFacts() response winning the race

  const api = () => window.AkanaMemoryApi;
  const ui = () => window.AkanaMemoryRender;
  const showToast = (m, k) => hooks.showToast(m, k);
  const $ = (id) => document.getElementById(id);

  function init(opts = {}) {
    Object.assign(hooks, opts);
    if (!_wired) {
      _wired = true;
      if (isMemoryStudioPage) wireStudioDom();
      else initChatInboxBadge();
    }
    if (isMemoryStudioPage) applyMemoryStudioRouteFromUrl();
  }

  // ─── Chat page: Inbox pending-count badge on the "Memory" nav button ───────
  // A yellow circle on the "Memory" (Hafıza) button shows how many items are
  // waiting in the Inbox. It updates instantly when new captures land during a
  // chat (ws:memory_staged for background capture, memory:staged for in-turn
  // tool writes) and always reconciles to the authoritative /stats count.
  let _navPending = 0;
  let _navReconcileTimer = null;
  let _navTurnTimers = [];

  function setNavBadge(n) {
    const el = $("memory-nav-badge");
    if (!el) return;
    const val = Math.max(0, Math.floor(Number(n) || 0));
    const changed = val !== _navPending;
    _navPending = val;
    el.textContent = val > 99 ? "99+" : String(val);
    el.hidden = val <= 0;
    if (changed && val > 0) {
      el.classList.remove("is-bump");
      // reflow so the animation restarts even on back-to-back increments
      void el.offsetWidth;
      el.classList.add("is-bump");
    }
  }

  async function reconcileNavBadge() {
    if (!api()) return;
    try {
      const s = (await api().getStats()) || {};
      if (typeof s.staging_pending === "number") setNavBadge(s.staging_pending);
    } catch {
      /* cosmetic — ignore */
    }
  }

  function scheduleNavReconcile() {
    if (_navReconcileTimer) clearTimeout(_navReconcileTimer);
    _navReconcileTimer = setTimeout(() => {
      _navReconcileTimer = null;
      void reconcileNavBadge();
    }, 700);
  }

  // Optimistic instant bump, then reconcile to the true count from /stats.
  function bumpNavBadge(delta) {
    if (delta > 0) setNavBadge(_navPending + delta);
    scheduleNavReconcile();
  }

  // Backstop: after a turn ends, an item may have been staged via a path that
  // fired no instant signal (the STOP/cancel tail carries no `done`, no WS
  // broadcast). In-turn agent memory-write tools now emit "memory:staged" the
  // moment the tool ends (see maybeSignalMemoryWriteTool in the transport), so the
  // badge already refreshes MID-turn; this backstop still reconciles from /stats
  // twice — soon (in-turn tool staging is committed BEFORE `done`, and self-
  // corrects the optimistic mid-turn bump) and again a few seconds later (background
  // auto-capture finishes after `done`). Timers coalesce across back-to-back
  // turns; skipped while hidden (the focus handler covers the return).
  function scheduleTurnReconcile() {
    for (const id of _navTurnTimers) clearTimeout(id);
    const run = () => {
      if (!document.hidden) void reconcileNavBadge();
    };
    _navTurnTimers = [setTimeout(run, 1200), setTimeout(run, 4500)];
  }

  function initChatInboxBadge() {
    if (isMemoryStudioPage || !$("memory-nav-badge")) return;
    const bus = window.AkanaBus;
    // ── Instant paths (optimistic bump + quick reconcile) ──
    // Background auto-capture staged new items (server WS broadcast).
    bus?.on?.("ws:memory_staged", (evt) => {
      const writes = Array.isArray(evt && evt.writes) ? evt.writes : [];
      const n = writes.filter((w) => w && w.kind === "staging").length;
      if (n > 0) bumpNavBadge(n);
    });
    // Agent staged via a memory tool during the turn (done-payload writes).
    bus?.on?.("memory:staged", (evt) => {
      const n = Number(evt && evt.count) || 0;
      if (n > 0) bumpNavBadge(n);
    });
    // ── Backstop: reconcile after every turn regardless of staging path ──
    // ws:turn_completed is the generic WS emission (fires for the user's own
    // turn, NOT gated by the current-conversation check); chat:stream:done is
    // the local fallback if the WS frame is dropped/late; chat:stream:error
    // covers the STOP/cancel tail where no other signal fires.
    bus?.on?.("ws:turn_completed", scheduleTurnReconcile);
    bus?.on?.("chat:stream:done", scheduleTurnReconcile);
    bus?.on?.("chat:stream:error", scheduleTurnReconcile);
    // Approve/reject happens on the Memory page → re-read the true count when
    // the chat tab regains focus.
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) void reconcileNavBadge();
    });
    window.addEventListener("focus", () => void reconcileNavBadge());
    void reconcileNavBadge(); // initial count on load
  }

  function setStatus(id, msg, isErr) {
    const el = $(id);
    if (!el) return;
    el.textContent = msg || "";
    el.classList.toggle("is-error", !!isErr);
    el.classList.toggle("is-ok", !!msg && !isErr);
  }

  // ─── Stats ─────────────────────────────────────────────────────────────────
  async function loadStats() {
    if (!api()) return;
    try {
      const s = (await api().getStats()) || {};
      const set = (id, v) => {
        const el = $(id);
        if (el) el.textContent = v === null || v === undefined ? "—" : String(v);
      };
      set("memory-stat-facts", s.facts ?? s.valid_facts);
      set("memory-stat-valid", s.valid_facts ?? s.facts);
      set("memory-stat-pending", s.staging_pending);
      set("memory-stat-turns", s.turns);
      set("memory-stat-vector", s.vector_embeddings);
      // Vector health at a glance: ON/OFF + backend (fastembed/ollama).
      const vnote = $("memory-stat-vector-note");
      if (vnote) {
        const v = s.vector || {};
        const label = v.backend === "local" ? "fastembed" : v.backend || "vector";
        if (v.active) vnote.textContent = t("memory.vector_active", { backend: label });
        else if (v.available) vnote.textContent = t("memory.vector_off_mode");
        else vnote.textContent = t("memory.vector_inactive", { backend: label });
      }
      const ledger = $("memory-ledger-path");
      if (ledger) ledger.textContent = s.ledger_path || "—";
      updateInboxCount(typeof s.staging_pending === "number" ? s.staging_pending : null);
    } catch {
      /* stats are cosmetic — fail silently */
    }
  }

  function updateInboxCount(n) {
    const show = typeof n === "number" && n > 0;
    const el = $("memory-inbox-count");
    if (el) {
      el.textContent = show ? String(n) : "";
      el.hidden = !show;
    }
    // Bulk actions only make sense with pending items. A cosmetic stats refresh
    // that returns null (unknown count) leaves the buttons untouched.
    if (typeof n === "number") {
      for (const id of ["memory-inbox-approve-all", "memory-inbox-reject-all"]) {
        const b = $(id);
        if (b) b.hidden = n <= 0;
      }
    }
  }

  // ─── Overview: recent-activity timeline ───────────────────────────────────
  async function loadTimeline() {
    const list = $("memory-timeline-list");
    if (!list || !api()) return;
    ui().setListState(list, "loading", t("memory.state_loading"));
    try {
      const items = await api().getTimeline(8);
      list.innerHTML = "";
      if (!items.length) {
        ui().setListState(list, "empty", t("memory.timeline_empty"));
        return;
      }
      for (const it of items) list.appendChild(ui().renderTimelineItem(it));
    } catch (e) {
      ui().setListState(list, "error", t("memory.timeline_error", { err: e.message || e }));
    }
  }

  // ─── Inbox (staging approval queue) ──────────────────────────────────────
  async function loadInbox() {
    const list = $("memory-inbox-list");
    if (!list || !api()) return;
    ui().setListState(list, "loading", t("memory.state_loading"));
    try {
      const items = await api().listStaging("pending");
      updateInboxCount(items.length);
      list.innerHTML = "";
      if (!items.length) {
        ui().setListState(list, "empty", t("memory.inbox_empty_msg"));
        return;
      }
      for (const item of items) {
        list.appendChild(
          ui().renderInboxItem(item, {
            shortId: hooks.shortConversationId,
            onApprove: (it, li) => void resolveInboxItem(it.id, li, "approve"),
            onReject: (it, li) => void resolveInboxItem(it.id, li, "reject"),
          }),
        );
      }
    } catch (e) {
      ui().setListState(list, "error", t("memory.inbox_error", { err: e.message || e }));
    }
  }

  async function resolveInboxItem(id, li, action) {
    // Optimistic: remove card immediately; reload on error.
    const list = li.parentElement;
    li.remove();
    const left = list ? list.querySelectorAll(".memory-inbox-item").length : 0;
    updateInboxCount(left);
    if (list && !left) ui().setListState(list, "empty", t("memory.inbox_empty_after"));
    try {
      if (action === "approve") {
        await api().approveStaging(id);
        showToast(t("memory.toast_approved"), "success");
        void loadFacts();
      } else {
        await api().rejectStaging(id);
        showToast(t("memory.toast_rejected"), "success");
      }
      void loadStats();
    } catch (e) {
      showToast(t("memory.toast_item_failed", { err: e.message || e }), "err");
      void loadInbox();
    }
  }

  // Approve / reject every pending candidate in one shot. There is no bulk
  // endpoint — we loop the vetted single-item path (each approve embeds server
  // side), sequentially so we never hammer the embedder or double-fire. Reject
  // is irreversible → it confirms first.
  async function resolveAllInbox(action) {
    if (!api()) return;
    let items;
    try {
      items = await api().listStaging("pending");
    } catch (e) {
      showToast(t("memory.toast_item_failed", { err: e.message || e }), "err");
      return;
    }
    if (!items.length) return;
    if (
      action === "reject" &&
      !window.confirm(t("memory.inbox_reject_all_confirm", { n: items.length }))
    ) {
      return;
    }
    setInboxBulkBusy(true);
    const list = $("memory-inbox-list");
    if (list) ui().setListState(list, "loading", t("memory.state_loading"));
    let ok = 0;
    let fail = 0;
    for (const it of items) {
      try {
        if (action === "approve") await api().approveStaging(it.id);
        else await api().rejectStaging(it.id);
        ok += 1;
      } catch {
        fail += 1;
      }
    }
    setInboxBulkBusy(false);
    if (fail) {
      showToast(t("memory.toast_bulk_partial", { ok, fail }), "err");
    } else if (action === "approve") {
      showToast(t("memory.toast_approved_all", { n: ok }), "success");
    } else {
      showToast(t("memory.toast_rejected_all", { n: ok }), "success");
    }
    void loadInbox();
    void loadStats();
    if (action === "approve") void loadFacts();
  }

  function setInboxBulkBusy(busy) {
    for (const id of ["memory-inbox-approve-all", "memory-inbox-reject-all", "memory-inbox-refresh"]) {
      const b = $(id);
      if (b) b.disabled = busy;
    }
  }

  // ─── Facts ────────────────────────────────────────────────────────────────
  function factsFilters() {
    const q = ($("memory-facts-q")?.value || "").trim();
    const includeInvalidated = !!$("memory-facts-history")?.checked;
    // Search is relevance-ranked top-N (offset is meaningless → pager hidden);
    // browse walks the full set one FACTS_PAGE_SIZE page at a time.
    if (q) return { q, includeInvalidated, limit: 100, offset: 0 };
    return { q, includeInvalidated, limit: FACTS_PAGE_SIZE, offset: factsOffset };
  }

  function updateFactsPager({ isSearch, total, shown }) {
    const pager = $("memory-facts-pager");
    if (!pager) return;
    // Hide the pager during search or when everything fits on a single page.
    if (isSearch || total <= FACTS_PAGE_SIZE) {
      pager.hidden = true;
      return;
    }
    pager.hidden = false;
    const from = total ? factsOffset + 1 : 0;
    const to = factsOffset + shown;
    const status = $("memory-facts-page-status");
    if (status) status.textContent = t("memory.facts_page_status", { from, to, total });
    const prev = $("memory-facts-prev");
    const next = $("memory-facts-next");
    if (prev) prev.disabled = factsOffset <= 0;
    if (next) next.disabled = to >= total;
  }

  async function loadFacts({ force = false } = {}) {
    const list = $("memory-facts-list");
    if (!list || !api()) return;
    // A background/automatic reload (debounced search keystroke, or an approve/reject
    // elsewhere in the Studio) must NOT wipe an open editor with unsaved changes —
    // setListState("loading") + innerHTML="" below would destroy the textarea content
    // silently. Explicit user actions that intend to replace the list (save, delete,
    // manual refresh, paging, submit) pass force:true. Skip the automatic refresh instead.
    if (!force && hasDirtyFactEditor()) return;
    const seq = ++factsRequestSeq;
    ui().setListState(list, "loading", t("memory.state_loading"));
    try {
      const filters = factsFilters();
      const isSearch = !!filters.q;
      const { items, total } = await api().listFacts(filters);
      if (seq !== factsRequestSeq) return; // a newer loadFacts() call superseded this one
      const count = $("memory-facts-count");
      if (count) {
        count.textContent = items.length ? String(total) : "";
        count.hidden = !items.length;
      }
      list.innerHTML = "";
      if (!items.length) {
        // An out-of-range page (e.g. facts deleted) — step back to the last page.
        if (!isSearch && factsOffset > 0 && total > 0) {
          factsOffset = Math.max(0, Math.floor((total - 1) / FACTS_PAGE_SIZE) * FACTS_PAGE_SIZE);
          await loadFacts({ force });
          return;
        }
        const { q, includeInvalidated } = filters;
        let msg;
        if (q) {
          msg = t("memory.facts_empty_query", { q });
        } else if (includeInvalidated) {
          msg = t("memory.facts_empty_filtered");
        } else {
          msg = t("memory.facts_empty_none");
        }
        ui().setListState(list, "empty", msg);
        updateFactsPager({ isSearch, total: 0, shown: 0 });
        return;
      }
      for (const f of items) {
        list.appendChild(
          ui().renderFactCard(f, {
            onEdit: (fact, li) => openFactEditor(li, fact),
            onDelete: (fact) => void deleteFact(fact),
          }),
        );
      }
      updateFactsPager({ isSearch, total, shown: items.length });
    } catch (e) {
      if (seq !== factsRequestSeq) return; // a newer loadFacts() call superseded this one
      ui().setListState(list, "error", t("memory.facts_error", { err: e.message || e }));
    }
  }

  function gotoFactsPage(delta) {
    const next = factsOffset + delta * FACTS_PAGE_SIZE;
    factsOffset = Math.max(0, next);
    void loadFacts();
    // Scroll the studio's own scroll container (not the list) so the header +
    // search box stay visible; the sticky pager keeps Prev/Next reachable.
    const scroller = $("memory-facts-list")?.closest(".mem-content");
    if (scroller) scroller.scrollTo({ top: 0, behavior: "smooth" });
    else $("memory-facts-list")?.scrollIntoView({ block: "start", behavior: "smooth" });
  }

  function openFactEditor(li, f) {
    if (li.querySelector(".memory-fact-editor")) return;
    const { editor, focus } = ui().buildFactEditor(f, {
      onSave: async (newValue, mode, saveBtn) => {
        if (!newValue) {
          showToast(t("memory.status_value_empty_toast"), "err");
          return;
        }
        saveBtn.disabled = true;
        try {
          await api().updateFact(f.id, newValue, mode);
          showToast(mode === "correct" ? t("memory.toast_correct_done") : t("memory.toast_supersede_done"), "success");
          await loadFacts({ force: true });
          void loadStats();
        } catch (e) {
          saveBtn.disabled = false;
          showToast(t("memory.toast_update_failed", { err: e.message || e }), "err");
        }
      },
    });
    li.appendChild(editor);
    // Stamp the original value so loadFacts() can tell a dirty (in-progress) edit from a
    // pristine one and avoid wiping unsaved work on a background/debounced reload.
    const ta = editor.querySelector("textarea");
    if (ta) ta.dataset.originalValue = f.value || "";
    focus();
  }

  /** True when an open fact editor holds unsaved changes (textarea differs from original). */
  function hasDirtyFactEditor() {
    const list = $("memory-facts-list");
    if (!list) return false;
    for (const ta of list.querySelectorAll(".memory-fact-editor textarea")) {
      if (ta.value !== (ta.dataset.originalValue || "")) return true;
    }
    return false;
  }

  async function deleteFact(f) {
    const label = f.key || f.value?.slice(0, 40) || f.id;
    if (!window.confirm(t("memory.confirm_delete_fact", { label }))) return;
    try {
      await api().deleteFact(f.id, { hard: false });
      showToast(t("memory.toast_deleted"), "success");
      await loadFacts();
      void loadStats();
    } catch (e) {
      showToast(t("memory.toast_delete_failed", { err: e.message || e }), "err");
    }
  }

  function updateFactValueCount() {
    const v = $("memory-fact-value");
    const c = $("memory-fact-value-count");
    if (v && c) c.textContent = t("memory.facts_value_count", { n: v.value.length });
  }

  function resetNewFactForm() {
    const v = $("memory-fact-value"); if (v) v.value = "";
    const k = $("memory-fact-key"); if (k) k.value = "";
    const s = $("memory-fact-source"); if (s) s.value = "";
    const kind = $("memory-fact-kind"); if (kind) kind.value = "fact";
    const trust = $("memory-fact-trust"); if (trust) trust.value = "user_statement";
    updateFactValueCount();
  }

  async function saveNewFact() {
    const value = ($("memory-fact-value")?.value || "").trim();
    if (!value) {
      setStatus("memory-fact-status", t("memory.status_value_empty"), true);
      return;
    }
    const body = { value };
    const key = ($("memory-fact-key")?.value || "").trim();
    if (key) body.key = key;
    const kind = $("memory-fact-kind")?.value;
    if (kind) body.kind = kind;
    const trust = $("memory-fact-trust")?.value;
    if (trust) body.trust = trust;
    const source = ($("memory-fact-source")?.value || "").trim();
    if (source) body.source_detail = source;
    setStatus("memory-fact-status", t("memory.status_saving"), false);
    try {
      await api().createFact(body);
      setStatus("memory-fact-status", t("memory.status_saved"), false);
      showToast(t("memory.toast_fact_saved"), "success");
      resetNewFactForm();
      await loadFacts();
      void loadStats();
    } catch (e) {
      setStatus("memory-fact-status", `${t("memory.toast_update_failed", { err: e.message || e })}`, true);
      showToast(e.message || String(e), "err");
    }
  }

  // ─── Recall test ──────────────────────────────────────────────────────────
  async function runRecall() {
    const list = $("memory-recall-list");
    const traceEl = $("memory-recall-trace");
    const q = ($("memory-recall-q")?.value || "").trim();
    if (!list || !api()) return;
    if (traceEl) traceEl.innerHTML = "";
    if (!q) {
      ui().setListState(list, "empty", t("memory.recall_empty_hint"));
      return;
    }
    ui().setListState(list, "loading", t("memory.state_searching"));
    try {
      const k = Number($("memory-recall-k")?.value || 8);
      const asOf = ($("memory-recall-asof")?.value || "").trim() || undefined;
      const obsFrom = ($("memory-recall-observed-from")?.value || "").trim() || undefined;
      const obsTo = ($("memory-recall-observed-to")?.value || "").trim() || undefined;
      const data = (await api().recall(q, k, asOf, obsFrom, obsTo)) || {};
      ui().renderRecallTrace(traceEl, data);
      const items = Array.isArray(data.items) ? data.items : [];
      list.innerHTML = "";
      if (!items.length) {
        ui().setListState(list, "empty", t("memory.recall_empty_noresult", { q }));
        return;
      }
      for (const it of items) list.appendChild(ui().renderRecallItem(it));
    } catch (e) {
      ui().setListState(list, "error", t("memory.recall_error", { err: e.message || e }));
    }
  }

  // ─── Settings ─────────────────────────────────────────────────────────────
  function _syncOllamaFields() {
    // Show Ollama fields only when backend is "ollama" (hide for local/fastembed).
    const backend = $("memory-embed-backend")?.value || "local";
    const box = $("memory-ollama-fields");
    if (box) box.hidden = backend !== "ollama";
  }

  async function loadSettings() {
    if (!api()) return;
    try {
      const s = (await api().getSettings()) || {};
      const allow = $("memory-allow-direct");
      if (allow) allow.checked = !!s.allow_direct;
      const summary = $("memory-session-summary");
      // Default ON: a missing/undefined value reads as enabled (mirrors the server default).
      if (summary) summary.checked = s.session_summary !== false;
      // auto_capture is still not a UI toggle (always ON by default); saveSettings omits it,
      // so the PUT partial-merge preserves its stored value.
      const vector = $("memory-vector-mode");
      if (vector && typeof s.vector === "string") vector.value = s.vector;
      const backend = $("memory-embed-backend");
      if (backend && typeof s.embed_backend === "string") backend.value = s.embed_backend;
      const url = $("memory-ollama-url");
      if (url) url.value = s.ollama_url || "";
      const model = $("memory-embed-model");
      if (model) model.value = s.embed_model || "";
      _syncOllamaFields();
      setStatus("memory-settings-status", "", false);
    } catch (e) {
      setStatus("memory-settings-status", t("memory.status_settings_loaded_err", { err: e.message || e }), true);
    }
  }

  async function saveSettings() {
    if (!api()) return;
    const body = {
      allow_direct: !!$("memory-allow-direct")?.checked,
      session_summary: !!$("memory-session-summary")?.checked,
      vector: $("memory-vector-mode")?.value || "auto",
      embed_backend: $("memory-embed-backend")?.value || "local",
      ollama_url: ($("memory-ollama-url")?.value || "").trim(),
      embed_model: ($("memory-embed-model")?.value || "").trim(),
    };
    setStatus("memory-settings-status", t("memory.status_saving"), false);
    try {
      await api().putSettings(body);
      setStatus("memory-settings-status", t("memory.status_settings_saved"), false);
      showToast(t("memory.toast_save_settings"), "success");
      void loadStats();
    } catch (e) {
      setStatus("memory-settings-status", t("memory.status_settings_save_err", { err: e.message || e }), true);
      showToast(e.message || String(e), "err");
    }
  }

  // ─── Navigation rail + view-switch ────────────────────────────────────────
  function setView(view) {
    currentView = VIEWS.includes(view) ? view : "overview";
    document.querySelectorAll(".memory-studio-tab").forEach((btn) => {
      const on = btn.dataset.memoryView === currentView;
      btn.classList.toggle("is-active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
    document.querySelectorAll(".memory-studio-pane").forEach((pane) => {
      const on = pane.dataset.memoryView === currentView;
      pane.classList.toggle("is-active", on);
      pane.hidden = !on;
    });
  }

  function refreshView(view) {
    if (view === "overview") {
      void loadStats();
      void loadTimeline();
    } else if (view === "inbox") {
      void loadInbox();
    } else if (view === "facts") void loadFacts();
    else if (view === "recall") void runRecall();
    else if (view === "settings") void loadSettings();
  }

  async function loadMemoryPane() {
    if (!isMemoryStudioPage || !api()) return;
    setView(currentView);
    await Promise.all([
      loadStats(),
      loadTimeline(),
      loadInbox(),
      loadFacts({ force: true }),
      loadSettings(),
    ]);
  }

  function memoryStudioHref({ view } = {}) {
    const u = new URL("/memory", window.location.origin);
    if (view) u.searchParams.set("view", view);
    return u.pathname + u.search;
  }

  function openMemoryStudio({ view, prefill } = {}) {
    if (prefill) {
      try {
        sessionStorage.setItem(LS_MEMORY_PREFILL, JSON.stringify(prefill));
      } catch {
        /* quota — skip */
      }
    }
    if (isMemoryStudioPage) {
      if (view) setView(view);
      applyPrefillFromStorage();
      void loadMemoryPane();
      return;
    }
    window.location.assign(memoryStudioHref({ view }));
  }

  function applyPrefillFromStorage() {
    let prefill = null;
    try {
      const raw = sessionStorage.getItem(LS_MEMORY_PREFILL);
      if (raw) {
        sessionStorage.removeItem(LS_MEMORY_PREFILL);
        prefill = JSON.parse(raw);
      }
    } catch {
      prefill = null;
    }
    if (!prefill || (!prefill.value && !prefill.key)) return;
    setView("facts");
    const keyEl = $("memory-fact-key");
    const valEl = $("memory-fact-value");
    if (keyEl) keyEl.value = prefill.key || "";
    if (valEl) valEl.value = prefill.value || "";
    updateFactValueCount(); // setting .value fires no 'input' event → refresh the counter
    const formPanel = $("memory-fact-save")?.closest("details");
    if (formPanel) formPanel.open = true;
    setStatus("memory-fact-status", t("memory.status_prefill_hint"), false);
    valEl?.focus();
  }

  function applyMemoryStudioRouteFromUrl() {
    if (!isMemoryStudioPage) return;
    const raw = new URLSearchParams(window.location.search).get("view") || "";
    const view = VIEWS.includes(raw) ? raw : LEGACY_VIEWS[raw] || null;
    if (view) setView(view);
    applyPrefillFromStorage();
  }

  // ─── Chat page bridge ──────────────────────────────────────────────────────
  function lastUserChatMessageText() {
    const thread = hooks.chatActiveThread();
    if (thread?.messages?.length) {
      for (let i = thread.messages.length - 1; i >= 0; i--) {
        const m = thread.messages[i];
        if (m.kind === "user" && (m.text || "").trim()) return m.text.trim();
      }
    }
    return (hooks.msg && hooks.msg.value.trim()) || "";
  }

  function captureChatMessageToMemory() {
    const text = lastUserChatMessageText();
    if (!text) {
      showToast(t("memory.toast_no_chat_msg"), "err");
      return;
    }
    openMemoryStudio({
      view: "facts",
      prefill: { value: text.length > 8000 ? text.slice(0, 8000) : text },
    });
  }

  // ─── DOM wiring (memory page only) ────────────────────────────────────────
  function gotoView(view) {
    setView(view);
    refreshView(view);
  }

  function wireStudioDom() {
    // Left nav rail (every rail item carries the .memory-studio-tab class)
    document.querySelectorAll(".memory-studio-tab").forEach((btn) => {
      btn.addEventListener("click", () => gotoView(btn.dataset.memoryView || "overview"));
    });

    // Global search: Enter → run query in Recall view
    const globalSearch = $("memory-global-search");
    if (globalSearch) {
      globalSearch.addEventListener("keydown", (e) => {
        if (e.key !== "Enter") return;
        e.preventDefault();
        const q = globalSearch.value.trim();
        if (!q) return;
        const recallQ = $("memory-recall-q");
        if (recallQ) recallQ.value = q;
        gotoView("recall");
      });
    }

    $("memory-refresh-all")?.addEventListener("click", () => void loadMemoryPane());
    $("memory-embed-backend")?.addEventListener("change", _syncOllamaFields);
    $("memory-timeline-refresh")?.addEventListener("click", () => void loadTimeline());
    $("memory-inbox-refresh")?.addEventListener("click", () => void loadInbox());
    $("memory-inbox-approve-all")?.addEventListener("click", () => void resolveAllInbox("approve"));
    $("memory-inbox-reject-all")?.addEventListener("click", () => void resolveAllInbox("reject"));

    const factsQ = $("memory-facts-q");
    if (factsQ) {
      factsQ.addEventListener("input", () => {
        clearTimeout(factsQueryTimer);
        factsQueryTimer = setTimeout(() => {
          factsOffset = 0; // new query → back to the first page
          void loadFacts();
        }, 300);
      });
      factsQ.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          clearTimeout(factsQueryTimer);
          factsOffset = 0;
          void loadFacts();
        }
      });
    }
    $("memory-facts-history")?.addEventListener("change", () => {
      factsOffset = 0; // filter change → back to the first page
      void loadFacts();
    });
    $("memory-facts-prev")?.addEventListener("click", () => gotoFactsPage(-1));
    $("memory-facts-next")?.addEventListener("click", () => gotoFactsPage(1));
    $("memory-fact-save")?.addEventListener("click", () => void saveNewFact());
    $("memory-fact-reset")?.addEventListener("click", resetNewFactForm);
    $("memory-fact-value")?.addEventListener("input", updateFactValueCount);
    updateFactValueCount();

    $("memory-recall-btn")?.addEventListener("click", () => void runRecall());
    $("memory-recall-q")?.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        void runRecall();
      }
    });
    $("memory-recall-asof")?.addEventListener("change", () => {
      if (($("memory-recall-q")?.value || "").trim()) void runRecall();
    });
    $("memory-recall-asof-clear")?.addEventListener("click", () => {
      const d = $("memory-recall-asof");
      if (d) d.value = "";
      if (($("memory-recall-q")?.value || "").trim()) void runRecall();
    });
    // Observation range (bi-temporal observed_from/observed_to) — same pattern as as_of.
    for (const id of ["memory-recall-observed-from", "memory-recall-observed-to"]) {
      $(id)?.addEventListener("change", () => {
        if (($("memory-recall-q")?.value || "").trim()) void runRecall();
      });
    }
    $("memory-recall-observed-clear")?.addEventListener("click", () => {
      const from = $("memory-recall-observed-from");
      const to = $("memory-recall-observed-to");
      if (from) from.value = "";
      if (to) to.value = "";
      if (($("memory-recall-q")?.value || "").trim()) void runRecall();
    });

    $("memory-settings-save")?.addEventListener("click", () => void saveSettings());
    $("memory-stats-refresh")?.addEventListener("click", () => void loadStats());
  }

  window.AkanaMemoryStudio = {
    init,
    loadMemoryPane,
    /** Legacy chat hook — conversation list no longer in Studio; backward-compatible no-op. */
    loadMemoryConversations: () => {},
    /** «Context preview» button: closest equivalent is the Recall test. */
    openCompilePreviewFromChat: () => openMemoryStudio({ view: "recall" }),
    openMemoryStudio,
    applyMemoryStudioRouteFromUrl,
    captureChatMessageToMemory,
    // Test-only seam: the facts reload path + its unsaved-editor guard, driven directly
    // by tests/web/memory_facts_editor_guard.harness.mjs without wiring the whole Studio.
    _test: { loadFacts, openFactEditor, hasDirtyFactEditor },
  };
})();
