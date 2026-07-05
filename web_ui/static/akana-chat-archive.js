/**
 * Akana chat archive — sidebar list, thread bar, conversation PATCH/DELETE/export.
 */
(() => {
  const LS_ARCHIVE_OPEN = "akana.archiveOpen";

  const baseUrl = () => window.AkanaCore.baseUrl();
  const authHeaders = (j) => window.AkanaCore.authHeaders(j);
  const parseApiError = (b, s) => window.AkanaCore.parseApiError(b, s);

  function createArchive(ctx) {
    const { bridge } = ctx;

    let chatArchiveItems = [];
    let chatArchiveView = "active";
    let activeConversationMeta = null;
    // DELETE TOMBSTONE: when a chat is deleted its id is added here; every subsequent
    // render FILTERS OUT these ids. Root cause: after deletion an IN-FLIGHT/scheduled
    // loadChatArchiveList (from navigation/scheduleArchiveRefresh, which had fetched
    // the list BEFORE the delete) resolved AFTER removeArchiveRow and re-painted the
    // chat → user saw "I deleted it but it's still there; only gone on 2nd delete"
    // (confirm blocks while the in-flight fetch resolves; the generation guard lets the
    // MOST-RECENTLY-STARTED loader win, not the most-recently-FINISHED one → a stale
    // render could win). Tombstone filter is race-independent: a deleted chat is NEVER
    // rendered again.
    const _deletedConvIds = new Set();
    const _DELETED_CAP = 500;
    function tombstoneConv(id) {
      const cid = String(id || "").trim();
      if (!cid) return;
      _deletedConvIds.add(cid);
      while (_deletedConvIds.size > _DELETED_CAP) {
        _deletedConvIds.delete(_deletedConvIds.values().next().value);
      }
    }
    let archiveSearchTimer = null;
    /** WS: per-conv running turn + queue (multi-tab / background sync). */
    const conversationActivity = new Map();
    let _activityWsWired = false;

    function getConvActivity(convId) {
      const id = (convId || "").trim();
      if (!id) return { running: false, queueDepth: 0 };
      return conversationActivity.get(id) || { running: false, queueDepth: 0 };
    }

    function patchConvActivity(convId, patch) {
      const id = (convId || "").trim();
      if (!id) return;
      const cur = getConvActivity(id);
      conversationActivity.set(id, { ...cur, ...patch });
      paintArchiveActivityBadge(id);
    }

    function clearConvActivity(convId) {
      const id = (convId || "").trim();
      if (!id) return;
      conversationActivity.delete(id);
      paintArchiveActivityBadge(id);
    }

    function activityBadgeLabel(act) {
      const q = Math.max(0, Number(act.queueDepth) || 0);
      if (act.running && q > 0) return window.AkanaI18n.t("archive.activity.responding_queued", { n: q });
      if (act.running) return window.AkanaI18n.t("archive.activity.responding");
      if (q > 0) return window.AkanaI18n.t("archive.activity.queued", { n: q });
      return "";
    }

    function paintArchiveActivityBadge(convId) {
      const id = (convId || "").trim();
      if (!id) return;
      const act = getConvActivity(id);
      const btn = document.querySelector(
        `.chat-archive-item[data-conversation-id="${CSS.escape(id)}"]`,
      );
      if (!btn) return;
      const label = activityBadgeLabel(act);
      let badge = btn.querySelector(".chat-archive-activity-badge");
      if (!label) {
        badge?.remove();
        btn.classList.remove("has-remote-activity");
        btn.removeAttribute("data-activity");
        return;
      }
      if (!badge) {
        badge = document.createElement("span");
        badge.className = "chat-archive-activity-badge";
        badge.setAttribute("aria-hidden", "true");
        const metaEl = btn.querySelector(".chat-archive-item-meta");
        if (metaEl) btn.insertBefore(badge, metaEl);
        else btn.appendChild(badge);
      }
      badge.textContent = label;
      btn.classList.add("has-remote-activity");
      btn.setAttribute("data-activity", label);
    }

    function paintAllArchiveActivityBadges() {
      for (const id of conversationActivity.keys()) paintArchiveActivityBadge(id);
    }

    function handleChatActivityEvent(type, evt) {
      const cid = String((evt && evt.conversation_id) || "").trim();
      if (!cid) return;
      if (type === "turn_active") {
        patchConvActivity(cid, { running: true });
      } else if (type === "queue_updated") {
        patchConvActivity(cid, { queueDepth: Math.max(0, Number(evt.depth) || 0) });
      } else if (type === "turn_completed") {
        patchConvActivity(cid, { running: false });
        void refreshConvActivityFromServer(cid);
      }
    }

    async function refreshConvActivityFromServer(convId) {
      const id = (convId || "").trim();
      if (!id) return;
      try {
        const headers = authHeaders();
        const [qr, ar] = await Promise.all([
          fetch(`${baseUrl()}/api/v1/chat/queue/${encodeURIComponent(id)}`, { headers }),
          fetch(`${baseUrl()}/api/v1/chat/active/${encodeURIComponent(id)}`, { headers }),
        ]);
        const patch = {};
        if (qr.ok) {
          const body = await qr.json().catch(() => ({}));
          patch.queueDepth = Math.max(0, Number(body.depth) || 0);
        }
        patch.running = ar.status === 200;
        // GET /chat/active is a live SSE follower on an active turn (buffer replay +
        // live stream); we only need the status code here — releasing the body avoids
        // leaking an open follower connection for the remainder of the turn (same class
        // of leak transport's resumeActiveTurn fixed: see the b17 comment there).
        if (ar.status === 200) {
          try {
            await ar.body?.cancel();
          } catch {
            /* ignore */
          }
        }
        patchConvActivity(id, patch);
      } catch {
        /* ignore */
      }
    }

    async function refreshKnownArchiveActivity() {
      paintAllArchiveActivityBadges();
      const cur = ctx.conversationIdForMemory();
      if (cur) await refreshConvActivityFromServer(cur);
    }

    function wireChatActivityWs() {
      if (_activityWsWired) return;
      const bus = window.AkanaBus;
      if (!bus || typeof bus.on !== "function") return;
      _activityWsWired = true;
      for (const t of ["turn_active", "queue_updated", "turn_completed"]) {
        bus.on(`ws:${t}`, (evt) => handleChatActivityEvent(t, evt));
      }
      // LLM chat title: the server summarizes a new chat's title from the first message in
      // the background and broadcasts ``conversation_updated`` (see chat_titler.py). The
      // post-turn refresh reloads TURNS only, so this WS nudge is what makes the title go
      // live in the sidebar + thread bar (no F5).
      bus.on("ws:conversation_updated", (evt) => handleConversationUpdatedEvent(evt));
    }

    /** Live-update a conversation's title from a ``conversation_updated`` WS broadcast. */
    function handleConversationUpdatedEvent(evt) {
      const cid = String((evt && evt.conversation_id) || "").trim();
      const title = evt && typeof evt.title === "string" ? evt.title.trim() : "";
      if (!cid || !title) return;
      // Update the in-memory sidebar item + re-render the row so the new title shows.
      let changed = false;
      chatArchiveItems = chatArchiveItems.map((c) => {
        if (c.id === cid && c.title !== title) {
          changed = true;
          return { ...c, title, title_source: "auto" };
        }
        return c;
      });
      if (changed) renderChatArchiveList(chatArchiveItems);
      // If it's the active conversation, update the active meta + thread bar / active thread
      // title too (the thread bar reads activeConversationMeta.title first).
      if (cid === ctx.conversationIdForMemory()) {
        activeConversationMeta = { ...(activeConversationMeta || { id: cid }), title, title_source: "auto" };
        const thread = ctx.chatActiveThread?.();
        if (thread) thread.title = title;
        syncChatThreadBar();
      }
    }

    function formatChatArchiveDate(iso) {
      if (!iso) return "";
      try {
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return "";
        return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
      } catch {
        return "";
      }
    }

    function formatRelativeTr(iso) {
      if (!iso) return "";
      try {
        const d = new Date(iso);
        if (Number.isNaN(d.getTime())) return "";
        const sec = Math.round((Date.now() - d.getTime()) / 1000);
        if (sec < 60) return window.AkanaI18n.t("archive.time.just_now");
        if (sec < 3600) return window.AkanaI18n.t("archive.time.minutes", { n: Math.floor(sec / 60) });
        if (sec < 86400) return window.AkanaI18n.t("archive.time.hours", { n: Math.floor(sec / 3600) });
        if (sec < 604800) return window.AkanaI18n.t("archive.time.days", { n: Math.floor(sec / 86400) });
        const locale = window.AkanaI18n.getLanguage?.() === "en" ? "en-US" : "tr-TR";
        return d.toLocaleDateString(locale, { day: "numeric", month: "short" });
      } catch {
        return "";
      }
    }

    async function patchConversationApi(convId, patch) {
      const r = await fetch(`${baseUrl()}/api/v1/conversations/${encodeURIComponent(convId)}`, {
        method: "PATCH",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(err?.error?.message || err?.detail || `HTTP ${r.status}`);
      }
      return r.json();
    }

    async function deleteConversationApi(convId) {
      const r = await fetch(`${baseUrl()}/api/v1/conversations/${encodeURIComponent(convId)}`, {
        method: "DELETE",
        headers: authHeaders(),
      });
      if (r.ok || r.status === 204) return;
      const err = await r.json().catch(() => ({}));
      throw new Error(parseApiError(err, r.status) || window.AkanaI18n.t("archive.error.delete_failed", { code: r.status }));
    }

    function downloadTextFile(filename, text, mime = "text/plain;charset=utf-8") {
      const blob = new Blob([text], { type: mime });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
    }

    function archiveActionIcon(name) {
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("width", "14");
      svg.setAttribute("height", "14");
      svg.setAttribute("viewBox", "0 0 24 24");
      svg.setAttribute("fill", "none");
      svg.setAttribute("aria-hidden", "true");
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      path.setAttribute("stroke", "currentColor");
      path.setAttribute("stroke-width", "2");
      path.setAttribute("stroke-linecap", "round");
      path.setAttribute("stroke-linejoin", "round");
      const d = {
        pin: "M12 17v5M9 10.76a2 2 0 01-1.11 1.79l-1.78.9A2 2 0 005 15.24V16a1 1 0 001 1h12a1 1 0 001-1v-.76a2 2 0 00-1.11-1.79l-1.78-.9A2 2 0 0115 10.76V7a1 1 0 011-1 2 2 0 000-4H8a2 2 0 000 4 1 1 0 011 1z",
        edit: "M12 20h9M16.5 3.5a2.12 2.12 0 013 3L7 19l-4 1 1-4 12.5-12.5z",
        archive: "M3 4h18v4H3zM5 8v11a2 2 0 002 2h10a2 2 0 002-2V8M10 12h4",
        restore: "M3 12a9 9 0 101.5-5.5M3 4v5h5",
        trash: "M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2M10 11v6M14 11v6",
      }[name];
      path.setAttribute(
        "d",
        d || "M3 6h18M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2M10 11v6M14 11v6",
      );
      svg.appendChild(path);
      return svg;
    }

    function closeArchiveRowMenus(exceptRow) {
      document.querySelectorAll(".chat-archive-row.is-menu-open").forEach((row) => {
        if (row !== exceptRow) row.classList.remove("is-menu-open");
      });
    }

    function makeArchiveActionBtn(label, iconName, onClick) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chat-archive-action";
      b.title = label;
      b.setAttribute("aria-label", label);
      b.appendChild(archiveActionIcon(iconName));
      b.addEventListener("click", (e) => {
        e.stopPropagation();
        closeArchiveRowMenus();
        void onClick();
      });
      return b;
    }

    function beginArchiveInlineRename(convId, titleEl) {
      const conv = chatArchiveItems.find((c) => c.id === convId);
      if (!conv || !titleEl) return;
      const input = document.createElement("input");
      input.type = "text";
      input.className = "chat-archive-rename-input";
      input.value = conv.title || "";
      input.maxLength = 200;
      titleEl.replaceWith(input);
      input.focus();
      input.select();
      const finish = async (save) => {
        if (save) {
          const t = input.value.trim();
          if (t && t !== conv.title) {
            try {
              await patchConversationApi(convId, { title: t });
              bridge.hooks.showToast(window.AkanaI18n.t("archive.toast.title_updated"));
              if (convId === ctx.conversationIdForMemory()) {
                activeConversationMeta = { ...activeConversationMeta, title: t };
                syncChatThreadBar();
              }
            } catch (e) {
              bridge.hooks.showToast(e.message || String(e), "err");
            }
          }
        }
        void loadChatArchiveList();
      };
      input.addEventListener("blur", () => void finish(true));
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          input.blur();
        }
        if (e.key === "Escape") {
          e.preventDefault();
          void finish(false);
        }
      });
    }

    function appendArchiveSection(list, label, items, active, archivedView) {
      if (!items.length) return;
      const head = document.createElement("li");
      head.className = "chat-archive-section";
      head.textContent = label;
      list.appendChild(head);
      for (const c of items) {
        const li = document.createElement("li");
        li.className = "chat-archive-li";
        const row = document.createElement("div");
        row.className = "chat-archive-row";

        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "chat-archive-item";
        btn.setAttribute("role", "option");
        if (c.id === active) btn.classList.add("is-active");
        if (c.pinned) btn.classList.add("is-pinned");
        if (c.archived_at) btn.classList.add("is-archived");
        btn.dataset.conversationId = c.id;

        const title = document.createElement("span");
        title.className = "chat-archive-item-title";
        title.textContent = c.title || bridge.hooks.shortConversationId(c.id);
        btn.appendChild(title);
        if (c.preview) {
          const prev = document.createElement("span");
          prev.className = "chat-archive-item-preview";
          prev.textContent = c.preview;
          btn.appendChild(prev);
        }
        const meta = document.createElement("span");
        meta.className = "chat-archive-item-meta";
        const when = formatRelativeTr(c.last_message_at || c.updated_at) || formatChatArchiveDate(c.last_message_at || c.updated_at);
        const count = c.message_count != null ? window.AkanaI18n.t("archive.msg_count", { n: c.message_count }) : "";
        meta.textContent = [when, count].filter(Boolean).join(" · ");
        btn.appendChild(meta);
        paintArchiveActivityBadge(c.id);
        btn.addEventListener("click", () => {
          if (window.matchMedia("(max-width: 900px)").matches) closeArchiveDrawer();
          void ctx.switchChatConversation(c.id);
        });
        btn.addEventListener("dblclick", (e) => {
          e.preventDefault();
          e.stopPropagation();
          beginArchiveInlineRename(c.id, title);
        });

        const actions = document.createElement("div");
        actions.className = "chat-archive-actions";
        actions.appendChild(
          makeArchiveActionBtn(c.pinned ? window.AkanaI18n.t("archive.btn.unpin") : window.AkanaI18n.t("archive.btn.pin"), "pin", async () => {
            try {
              await patchConversationApi(c.id, { pinned: !c.pinned });
              bridge.hooks.showToast(c.pinned ? window.AkanaI18n.t("archive.toast.unpinned") : window.AkanaI18n.t("archive.toast.pinned"));
              void loadChatArchiveList();
              if (c.id === ctx.conversationIdForMemory()) void refreshActiveConversationMeta();
            } catch (e) {
              bridge.hooks.showToast(e.message || String(e), "err");
            }
          }),
        );
        actions.appendChild(
          makeArchiveActionBtn(window.AkanaI18n.t("archive.btn.rename"), "edit", async () => beginArchiveInlineRename(c.id, title)),
        );
        if (archivedView || c.archived_at) {
          actions.appendChild(
            makeArchiveActionBtn(window.AkanaI18n.t("archive.btn.restore"), "restore", async () => {
              try {
                await patchConversationApi(c.id, { archived: false });
                bridge.hooks.showToast(window.AkanaI18n.t("archive.toast.restored"));
                chatArchiveView = "active";
                syncArchiveViewTabs();
                void loadChatArchiveList();
              } catch (e) {
                bridge.hooks.showToast(e.message || String(e), "err");
              }
            }),
          );
        } else {
          actions.appendChild(
            makeArchiveActionBtn(window.AkanaI18n.t("archive.btn.archive"), "archive", async () => {
              // Teardown delegated to threads.js to prevent the live stream from being
              // orphaned when archiving an active chat mid-stream (symmetric with delete:
              // abort client stream + fire-and-forget cancel the server turn).
              await ctx.archiveConversationById(c.id);
            }),
          );
        }
        actions.appendChild(
          makeArchiveActionBtn(window.AkanaI18n.t("archive.btn.delete"), "trash", async () => {
            await ctx.deleteConversationById(c.id);
          }),
        );

        const menuBtn = document.createElement("button");
        menuBtn.type = "button";
        menuBtn.className = "chat-archive-menu-btn";
        menuBtn.title = window.AkanaI18n.t("archive.btn.menu");
        menuBtn.setAttribute("aria-label", window.AkanaI18n.t("archive.btn.menu_aria"));
        menuBtn.setAttribute("aria-haspopup", "true");
        menuBtn.setAttribute("aria-expanded", "false");
        const menuIcon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
        menuIcon.setAttribute("width", "16");
        menuIcon.setAttribute("height", "16");
        menuIcon.setAttribute("viewBox", "0 0 24 24");
        menuIcon.setAttribute("fill", "currentColor");
        menuIcon.setAttribute("aria-hidden", "true");
        menuIcon.innerHTML =
          '<circle cx="12" cy="5" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="12" cy="19" r="1.5"/>';
        menuBtn.appendChild(menuIcon);
        menuBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          const open = !row.classList.contains("is-menu-open");
          closeArchiveRowMenus(open ? row : null);
          row.classList.toggle("is-menu-open", open);
          menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
        });

        row.appendChild(btn);
        row.appendChild(menuBtn);
        row.appendChild(actions);
        li.appendChild(row);
        list.appendChild(li);
      }
    }

    /**
     * Bucket active chats by date: TODAY / THIS WEEK / (OLDER → month-year headings).
     * Falls back to `updated_at` if `last_message_at` is absent; if both are absent,
     * the chat goes into the "OLDER" bucket. The list already arrives from the server
     * newest-first so bucket ORDER and within-bucket order are preserved (Map insertion
     * order). Returns: [{ label, items }] — empty buckets are not emitted.
     */
    function bucketArchiveByDate(items) {
      const now = new Date();
      const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
      const startOfWeek = startOfToday - 6 * 86400000; // last 7 days including today
      const OLDER = "__older__";
      const buckets = new Map();
      const pushBucket = (key, label, conv) => {
        let b = buckets.get(key);
        if (!b) {
          b = { label, items: [] };
          buckets.set(key, b);
        }
        b.items.push(conv);
      };
      for (const c of items) {
        const iso = c.last_message_at || c.updated_at;
        let t = NaN;
        if (iso) {
          const d = new Date(iso);
          if (!Number.isNaN(d.getTime())) t = d.getTime();
        }
        if (Number.isNaN(t)) {
          pushBucket(OLDER, window.AkanaI18n.t("archive.section.older"), c);
        } else if (t >= startOfToday) {
          pushBucket("today", window.AkanaI18n.t("archive.section.today"), c);
        } else if (t >= startOfWeek) {
          pushBucket("week", window.AkanaI18n.t("archive.section.this_week"), c);
        } else {
          const d = new Date(t);
          const key = `m-${d.getFullYear()}-${d.getMonth()}`;
          const locale = window.AkanaI18n.getLanguage?.() === "en" ? "en-US" : "tr-TR";
          const label = d
            .toLocaleDateString(locale, { month: "long", year: "numeric" })
            .toLocaleUpperCase(locale);
          pushBucket(key, label, c);
        }
      }
      return Array.from(buckets.values());
    }

    function renderChatArchiveList(items, opts = {}) {
      const list = document.getElementById("chat-archive-list");
      if (!list) return;
      // Preserve scroll position during re-render: clicking a chat or a background
      // refresh must not jump the list to the top. Use opts.preserveScroll if provided
      // (the value captured BEFORE loadChatArchiveList's "Loading…" placeholder);
      // otherwise snapshot the current scrollTop.
      const savedScrollTop = opts.preserveScroll ?? list.scrollTop;
      const active = ctx.conversationIdForMemory();
      const q = (document.getElementById("chat-archive-search")?.value || "").trim().toLowerCase();
      list.innerHTML = "";
      // TOMBSTONE FILTER: deleted chats are never rendered again, even if a stale
      // fetch brings them back (root of the "needs 2nd delete" bug).
      let filtered = _deletedConvIds.size ? items.filter((c) => !_deletedConvIds.has(c.id)) : items;
      if (q) {
        // Keep the tombstone filter active in search too: chain from `filtered` (not
        // items) → a deleted chat never comes back in search results either.
        filtered = filtered.filter((c) => {
          const hay = `${c.title || ""} ${c.preview || ""} ${c.id || ""}`.toLowerCase();
          return hay.includes(q);
        });
      }
      if (chatArchiveView === "archived") {
        filtered = filtered.filter((c) => c.archived_at);
      } else {
        filtered = filtered.filter((c) => !c.archived_at);
      }
      if (!filtered.length) {
        const li = document.createElement("li");
        li.className = "chat-archive-empty";
        if (q) li.textContent = window.AkanaI18n.t("archive.empty.no_results");
        else if (chatArchiveView === "archived") li.textContent = window.AkanaI18n.t("archive.empty.archived");
        else li.textContent = window.AkanaI18n.t("archive.empty.none");
        list.appendChild(li);
        return;
      }
      if (chatArchiveView === "active" && q) {
        // Search mode: flat single list. Server search results don't return last_message_at
        // so date-bucketing would incorrectly classify everything as "OLDER"; also, a flat
        // result list is the expected behaviour for search.
        appendArchiveSection(list, window.AkanaI18n.t("archive.section.search_results"), filtered, active, false);
      } else if (chatArchiveView === "active") {
        // Pinned chats always go in a separate bucket at the top; the rest
        // are split into date buckets (TODAY / THIS WEEK / month-year).
        const pinned = filtered.filter((c) => c.pinned);
        const rest = filtered.filter((c) => !c.pinned);
        if (pinned.length) appendArchiveSection(list, window.AkanaI18n.t("archive.section.pinned"), pinned, active, false);
        for (const bucket of bucketArchiveByDate(rest)) {
          appendArchiveSection(list, bucket.label, bucket.items, active, false);
        }
      } else {
        appendArchiveSection(list, window.AkanaI18n.t("archive.section.archived"), filtered, active, true);
      }
      paintAllArchiveActivityBadges();
      // After layout settles, restore scroll. First apply the saved scrollTop;
      // then if the active chat element exists, keep it visible with `block: 'nearest'`
      // — this keeps the clicked chat on screen even if successive render calls pull
      // savedScrollTop back to 0. `nearest` is a no-op if the element is already visible
      // and does not disrupt other scrolling.
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          const maxScroll = Math.max(0, list.scrollHeight - list.clientHeight);
          if (savedScrollTop > 0) {
            list.scrollTop = Math.min(savedScrollTop, maxScroll);
          }
          if (active) {
            const activeEl = list.querySelector(
              `[data-conversation-id="${(window.CSS && CSS.escape) ? CSS.escape(active) : active}"]`,
            );
            activeEl?.scrollIntoView?.({ block: "nearest" });
          }
        });
      });
    }

    /** Optimistically (instantly) remove a deleted chat from the list — WITHOUT waiting
     *  for the fire-and-forget loadChatArchiveList network round-trip or potential
     *  generation-guard suppression. setChatArchiveItems only updates the array (no DOM
     *  touch) → the old path left the row visible until an async refresh; under heavy
     *  navigation/search that refresh could be suppressed by the generation guard, leaving
     *  the row "stuck" and making the delete appear SLOW/broken.
     *  This updates both the array and the DOM synchronously; also clears any emptied
     *  date-section headers. */
    function removeArchiveRow(convId, opts = {}) {
      const id = String(convId || "").trim();
      if (!id) return;
      // DELETE tombstones (permanent → never render again). ARCHIVE passes
      // { tombstone: false }: it's a MOVE (active→archived), not a removal — the conv
      // must still render in the ARCHIVED view. Tombstoning it here made the archived
      // chat invisible in the archive tab until an F5 reset the in-memory set.
      if (opts.tombstone === false) {
        // Reversible move: if this id was tombstoned earlier this session, clear it so
        // the archived view (and a later restore) can render it again.
        _deletedConvIds.delete(id);
      } else {
        tombstoneConv(id); // prevent a stale render from bringing a DELETED chat back
      }
      chatArchiveItems = chatArchiveItems.filter((c) => c.id !== id);
      const list = document.getElementById("chat-archive-list");
      if (!list) return;
      const esc = window.CSS && CSS.escape ? CSS.escape(id) : id;
      // Remove ALL matching rows (also clears any accidental duplicates), not just the first.
      for (const btn of Array.from(list.querySelectorAll(`.chat-archive-item[data-conversation-id="${esc}"]`))) {
        const li = btn.closest(".chat-archive-li");
        if (li) li.remove();
      }
      // Clean up emptied section headers (e.g. "TODAY"): a header left with nothing
      // after it, or immediately followed by another header, is removed (no dangling headers).
      for (const head of Array.from(list.querySelectorAll(".chat-archive-section"))) {
        const n = head.nextElementSibling;
        if (!n || n.classList.contains("chat-archive-section")) head.remove();
      }
      if (!list.querySelector(".chat-archive-li")) {
        list.innerHTML = "";
        const empty = document.createElement("li");
        empty.className = "chat-archive-empty";
        empty.textContent =
          chatArchiveView === "archived"
            ? window.AkanaI18n.t("archive.empty.archived")
            : window.AkanaI18n.t("archive.empty.none");
        list.appendChild(empty);
      }
    }

    function syncArchiveViewTabs() {
      document.querySelectorAll(".chat-archive-tab").forEach((tab) => {
        const on = tab.dataset.archiveView === chatArchiveView;
        tab.classList.toggle("is-active", on);
        tab.setAttribute("aria-selected", on ? "true" : "false");
      });
    }

    // GENERATION guard across concurrent loaders: search-debounce, conversation-switch,
    // new-chat, delete, and meta-refresh all call loadChatArchiveList. Without the guard,
    // a slow IN-FLIGHT response (e.g. search) would OVERWRITE the result of a MORE RECENT
    // load (e.g. clearing the search) → list appears stale/not updating.
    let _archiveListGen = 0;
    async function loadChatArchiveList() {
      const list = document.getElementById("chat-archive-list");
      if (!list) return;
      const myGen = ++_archiveListGen; // generation counter for this load
      // Save scrollTop BEFORE resetting the placeholder DOM — it will be forwarded to
      // the render so clicking a chat / background refresh doesn't jump to the top.
      const savedScrollTop = list.scrollTop;
      // Show the "Loading…" flash ONLY on a genuinely empty/initial load. When the list
      // is already populated (new-chat, meta-refresh, conversation-switch triggered
      // refreshes) blanking it with a placeholder makes the screen flicker "refreshing"
      // and the old content disappears. Leave existing items in place; renderChatArchiveList
      // swaps everything in one go when the new data arrives (no visual jump).
      const hasCachedRows = list.querySelector(".chat-archive-li") != null;
      if (!hasCachedRows) {
        list.innerHTML = `<li class="chat-archive-empty">${window.AkanaI18n.t("archive.empty.loading")}</li>`;
      }
      const q = (document.getElementById("chat-archive-search")?.value || "").trim();
      try {
        if (q.length >= 2) {
          const sr = await fetch(
            `${baseUrl()}/api/v1/conversations/search?q=${encodeURIComponent(q)}&limit=40`,
            { headers: authHeaders() },
          );
          if (myGen !== _archiveListGen) return; // stale → a newer load owns the list
          if (sr.ok) {
            const data = await sr.json();
            if (myGen !== _archiveListGen) return;
            chatArchiveItems = (data.results || []).map((r) => ({
              id: r.conversation_id,
              title: r.title,
              preview: r.preview,
              pinned: false,
              archived_at: null,
              message_count: null,
              last_message_at: null,
              updated_at: null,
            }));
            renderChatArchiveList(chatArchiveItems, { preserveScroll: savedScrollTop });
            void refreshKnownArchiveActivity();
            return;
          }
        }
        const archivedParam = chatArchiveView === "archived" ? "true" : "false";
        const r = await fetch(
          `${baseUrl()}/api/v1/conversations?limit=50&archived=${archivedParam}`,
          { headers: authHeaders() },
        );
        if (myGen !== _archiveListGen) return; // bayat
        if (!r.ok) {
          list.innerHTML = "";
          const li = document.createElement("li");
          li.className = "chat-archive-empty";
          li.textContent = window.AkanaI18n.t("archive.empty.load_error");
          list.appendChild(li);
          return;
        }
        const data = await r.json();
        if (myGen !== _archiveListGen) return;
        chatArchiveItems = data.conversations || [];
        renderChatArchiveList(chatArchiveItems, { preserveScroll: savedScrollTop });
        void refreshKnownArchiveActivity();
      } catch {
        if (myGen !== _archiveListGen) return; // stale — let a newer load write
        list.innerHTML = "";
        const li = document.createElement("li");
        li.className = "chat-archive-empty";
        li.textContent = window.AkanaI18n.t("archive.empty.conn_error");
        list.appendChild(li);
      }
    }

    /**
     * Add a single item INSTANTLY for a new chat, without a server round-trip.
     *
     * Old path: opening a new chat called `loadChatArchiveList()` →
     * (1) list reset with `Loading…` — visual flash, (2) ALL 50 chats re-fetched,
     * (3) list DOM rebuilt from scratch. Total: 2-3 s "refreshing" feel.
     * But a new chat is EMPTY and the `meta` we already have (`{id, title}`) is
     * sufficient to list it — no network round-trip, no `Loading…` flash needed.
     * Prepend the item to the in-memory `chatArchiveItems` (server sorts newest-first;
     * new chat is freshest) and re-render from the same list (NO fetch).
     * Silently skip when in search/archive view (item doesn't belong there).
     * Returns: true if the item was inserted.
     */
    function insertConversationLocally(meta) {
      const id = (meta && meta.id ? String(meta.id) : "").trim();
      if (!id) return false;
      // New empty chat belongs only to the "active/flat" view; don't pollute the
      // in-memory list while a search query or archive tab is open (the next full
      // load will bring the correct data).
      if (chatArchiveView !== "active") return false;
      if ((document.getElementById("chat-archive-search")?.value || "").trim()) return false;
      const item = {
        id,
        title: meta.title || window.AkanaI18n.t("archive.default_title"),
        title_source: meta.title_source || "auto",
        preview: meta.preview ?? null,
        pinned: Boolean(meta.pinned),
        archived_at: meta.archived_at ?? null,
        created_at: meta.created_at ?? null,
        updated_at: meta.updated_at ?? new Date().toISOString(),
        last_message_at: meta.last_message_at ?? null,
        message_count: meta.message_count ?? 0,
      };
      const rest = chatArchiveItems.filter((c) => c.id !== id);
      chatArchiveItems = [item, ...rest];
      renderChatArchiveList(chatArchiveItems);
      void refreshKnownArchiveActivity();
      return true;
    }

    async function refreshActiveConversationMeta() {
      const id = ctx.conversationIdForMemory();
      if (!id) {
        activeConversationMeta = null;
        syncChatThreadBar();
        return;
      }
      try {
        const r = await fetch(`${baseUrl()}/api/v1/conversations/${encodeURIComponent(id)}`, {
          headers: authHeaders(),
        });
        if (!r.ok) {
          activeConversationMeta = null;
          syncChatThreadBar();
          return;
        }
        activeConversationMeta = await r.json();
        const thread = ctx.chatActiveThread();
        if (thread && activeConversationMeta?.title) thread.title = activeConversationMeta.title;
        syncChatThreadBar();
      } catch {
        activeConversationMeta = null;
        syncChatThreadBar();
      }
    }

    function syncChatThreadBar() {
      const bar = document.getElementById("chat-thread-bar");
      const titleEl = document.getElementById("chat-thread-title");
      const pinBtn = document.getElementById("btn-thread-pin");
      const id = ctx.conversationIdForMemory();
      const thread = ctx.chatActiveThread();
      const title =
        activeConversationMeta?.title ||
        thread?.title ||
        (id ? bridge.hooks.shortConversationId(id) : window.AkanaI18n.t("thread.new_chat_title"));
      if (!bar || !titleEl) return;
      // Single compact row: hamburger + title always visible (empty chat shows "New chat")
      // — the old two-layer header was merged into this.
      bar.hidden = false;
      titleEl.textContent = title;
      titleEl.title = id || title;
      // Context-aware top bar: conversation-bound actions (rename / export / pin /
      // delete) are only meaningful when a SAVED chat exists. Hidden on an empty
      // "New chat" → no pointless no-op button clutter.
      const hasConv = Boolean(id);
      for (const bid of [
        "btn-thread-rename",
        "btn-thread-export",
        "btn-thread-pin",
        "btn-thread-telegram",
        "btn-thread-delete",
      ]) {
        const b = document.getElementById(bid);
        if (b) b.hidden = !hasConv;
      }
      if (pinBtn) {
        const pinned = Boolean(activeConversationMeta?.pinned);
        pinBtn.setAttribute("aria-pressed", pinned ? "true" : "false");
        pinBtn.classList.toggle("is-on", pinned);
        pinBtn.title = pinned ? window.AkanaI18n.t("archive.pin_btn.unpin") : window.AkanaI18n.t("archive.pin_btn.pin");
      }
    }

    async function exportConversationMarkdown(convId) {
      const meta = convId === ctx.conversationIdForMemory() ? activeConversationMeta : null;
      let title = meta?.title || bridge.hooks.shortConversationId(convId);
      const r = await fetch(
        `${baseUrl()}/api/v1/conversations/${encodeURIComponent(convId)}/messages?limit=500`,
        { headers: authHeaders() },
      );
      if (!r.ok) throw new Error(window.AkanaI18n.t("archive.error.messages_load"));
      const data = await r.json();
      const lines = [`# ${title}`, "", `> conversation_id: \`${convId}\``, ""];
      for (const m of data.messages || []) {
        const role = m.role === "user" ? window.AkanaI18n.t("archive.export.role_user") : m.role === "assistant" ? window.AkanaI18n.t("archive.export.role_assistant") : m.role;
        lines.push(`## ${role}`, "", m.content || "", "", "---", "");
      }
      const safe = title.replace(/[^\w\u00C0-\u024f\-]+/gi, "_").slice(0, 48) || "sohbet";
      downloadTextFile(`${safe}.md`, lines.join("\n"), "text/markdown;charset=utf-8");
      bridge.hooks.showToast(window.AkanaI18n.t("archive.toast.markdown_saved"));
    }

    function syncArchiveToggleUi() {
      const open = document.body.classList.contains("archive-open");
      const btn = document.getElementById("btn-toggle-archive");
      if (btn) {
        btn.setAttribute("aria-expanded", open ? "true" : "false");
        btn.title = open ? window.AkanaI18n.t("archive.toggle.close") : window.AkanaI18n.t("archive.toggle.open");
        btn.setAttribute("aria-label", btn.title);
      }
    }

    function readArchiveOpenPref() {
      try {
        const v = localStorage.getItem(LS_ARCHIVE_OPEN);
        if (v === "1") return true;
        if (v === "0") return false;
      } catch {
        /* ignore */
      }
      return window.matchMedia("(min-width: 901px)").matches;
    }

    function openArchiveDrawer() {
      document.body.classList.add("archive-open");
      syncArchiveToggleUi();
      void loadChatArchiveList();
      try {
        localStorage.setItem(LS_ARCHIVE_OPEN, "1");
      } catch {
        /* ignore */
      }
    }

    function closeArchiveDrawer() {
      document.body.classList.remove("archive-open");
      syncArchiveToggleUi();
      try {
        localStorage.setItem(LS_ARCHIVE_OPEN, "0");
      } catch {
        /* ignore */
      }
    }

    function wireArchiveChrome() {
      wireChatActivityWs();
      const archiveSearch = document.getElementById("chat-archive-search");
      const btnToggleArchive = document.getElementById("btn-toggle-archive");
      const btnArchiveClose = document.getElementById("btn-archive-close");
      const archiveBackdrop = document.getElementById("chat-archive-backdrop");
      if (btnToggleArchive) {
        btnToggleArchive.addEventListener("click", () => {
          if (document.body.classList.contains("archive-open")) closeArchiveDrawer();
          else openArchiveDrawer();
        });
      }
      if (btnArchiveClose) btnArchiveClose.addEventListener("click", closeArchiveDrawer);
      if (archiveBackdrop) archiveBackdrop.addEventListener("click", closeArchiveDrawer);
      if (readArchiveOpenPref()) openArchiveDrawer();
      else syncArchiveToggleUi();
      if (archiveSearch) {
        archiveSearch.addEventListener("input", () => {
          clearTimeout(archiveSearchTimer);
          archiveSearchTimer = setTimeout(() => void loadChatArchiveList(), 280);
        });
      }
      document.querySelectorAll(".chat-archive-tab").forEach((tab) => {
        tab.addEventListener("click", () => {
          const view = tab.dataset.archiveView || "active";
          if (view === chatArchiveView) return;
          chatArchiveView = view;
          syncArchiveViewTabs();
          void loadChatArchiveList();
        });
      });
      syncArchiveViewTabs();
      document.addEventListener("click", (e) => {
        if (e.target.closest(".chat-archive-row")) return;
        closeArchiveRowMenus();
      });
    }

    async function fetchTelegramStatus() {
      const r = await fetch(`${baseUrl()}/api/v1/connectors/telegram`, { headers: authHeaders() });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    }

    async function bindTelegramChat(convId, chatId) {
      const r = await fetch(`${baseUrl()}/api/v1/connectors/telegram/bind`, {
        method: "POST",
        headers: { ...authHeaders(), "Content-Type": "application/json" },
        body: JSON.stringify({ conversation_id: convId, chat_id: chatId }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(parseApiError(err, r.status) || `HTTP ${r.status}`);
      }
      return r.json();
    }

    async function continueOnTelegram(convId) {
      if (!convId) return;
      let status;
      try {
        status = await fetchTelegramStatus();
      } catch (e) {
        bridge.hooks.showToast(e.message || String(e), "err");
        return;
      }
      const allowedIds = Array.isArray(status?.allowed_chat_ids) ? status.allowed_chat_ids : [];
      // token_set is also required: if the token was cleared but enabled+allowlist
      // remain, bind SUCCEEDS but delivery doesn't work (the poller won't start
      // without a token) → the user thinks they're "bound" and nothing arrives.
      // The GET snapshot returns token_set.
      if (!status?.enabled || !status?.token_set || allowedIds.length === 0) {
        bridge.hooks.showToast(window.AkanaI18n.t("archive.telegram.toast.not_configured"), "err");
        return;
      }
      let chatId = allowedIds[0];
      if (allowedIds.length > 1) {
        const picked = window.prompt(
          window.AkanaI18n.t("archive.telegram.prompt.choose_chat", { ids: allowedIds.join(", ") }),
          allowedIds[0],
        );
        if (!picked || !picked.trim()) return;
        chatId = picked.trim();
        if (!allowedIds.includes(chatId)) {
          bridge.hooks.showToast(window.AkanaI18n.t("archive.telegram.toast.invalid_chat"), "err");
          return;
        }
      }
      try {
        const res = await bindTelegramChat(convId, chatId);
        bridge.hooks.showToast(
          res?.notified === false
            ? window.AkanaI18n.t("archive.telegram.toast.bound_not_notified")
            : window.AkanaI18n.t("archive.telegram.toast.bound"),
        );
      } catch (e) {
        bridge.hooks.showToast(e.message || String(e), "err");
      }
    }

    function wireThreadBar() {
      const btnThreadRename = document.getElementById("btn-thread-rename");
      if (btnThreadRename) {
        btnThreadRename.addEventListener("click", () => {
          const id = ctx.conversationIdForMemory();
          if (!id) return;
          const titleEl = document.querySelector(
            `.chat-archive-item[data-conversation-id="${CSS.escape(id)}"] .chat-archive-item-title`,
          );
          if (titleEl) beginArchiveInlineRename(id, titleEl);
          else {
            const t = window.prompt(window.AkanaI18n.t("archive.prompt.new_title"), activeConversationMeta?.title || "");
            if (t && t.trim()) {
              void patchConversationApi(id, { title: t.trim() })
                .then(() => {
                  bridge.hooks.showToast(window.AkanaI18n.t("archive.toast.title_updated"));
                  void refreshActiveConversationMeta();
                  void loadChatArchiveList();
                })
                .catch((e) => bridge.hooks.showToast(e.message || String(e), "err"));
            }
          }
        });
      }
      const btnThreadExport = document.getElementById("btn-thread-export");
      if (btnThreadExport) {
        btnThreadExport.addEventListener("click", () => {
          const id = ctx.conversationIdForMemory();
          if (!id) {
            bridge.hooks.showToast(window.AkanaI18n.t("archive.toast.no_saved_chat"), "err");
            return;
          }
          void exportConversationMarkdown(id).catch((e) => bridge.hooks.showToast(e.message || String(e), "err"));
        });
      }
      const btnThreadPin = document.getElementById("btn-thread-pin");
      if (btnThreadPin) {
        btnThreadPin.addEventListener("click", () => {
          const id = ctx.conversationIdForMemory();
          if (!id) return;
          const next = !activeConversationMeta?.pinned;
          void patchConversationApi(id, { pinned: next })
            .then(() => {
              bridge.hooks.showToast(next ? window.AkanaI18n.t("archive.toast.pinned") : window.AkanaI18n.t("archive.toast.unpinned"));
              void refreshActiveConversationMeta();
              void loadChatArchiveList();
            })
            .catch((e) => bridge.hooks.showToast(e.message || String(e), "err"));
        });
      }
      const btnThreadTelegram = document.getElementById("btn-thread-telegram");
      if (btnThreadTelegram) {
        btnThreadTelegram.addEventListener("click", () => {
          void continueOnTelegram(ctx.conversationIdForMemory());
        });
      }
      const btnThreadDelete = document.getElementById("btn-thread-delete");
      if (btnThreadDelete) {
        btnThreadDelete.addEventListener("click", () => {
          void ctx.deleteConversationById(ctx.conversationIdForMemory() || null);
        });
      }
      const btnNewConv = document.getElementById("btn-new-conv");
      if (btnNewConv) {
        let btnNewConvBusy = false;
        btnNewConv.addEventListener("click", async () => {
          if (btnNewConvBusy) return;
          btnNewConvBusy = true;
          try {
            // Clear log and show hero (empty state) — adding a permanent
            // "started" row to the chat would hide the hero again.
            await ctx.chatStartNewThread();
            try {
              bridge.hooks.msg?.focus();
            } catch {
              /* ignore */
            }
          } finally {
            btnNewConvBusy = false;
          }
        });
      }
      document.addEventListener("keydown", (e) => {
        // New chat: Alt+N. Ctrl/Cmd+N (new window) and Ctrl+Shift+N (incognito)
        // are reserved by the browser and never reach the page, so we use Alt.
        if (
          e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey &&
          (e.key === "n" || e.key === "N")
        ) {
          e.preventDefault();
          document.getElementById("btn-new-conv")?.click();
          return;
        }
        // Toggle archive: Ctrl/Cmd+B.
        if (
          (e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey &&
          (e.key === "b" || e.key === "B")
        ) {
          e.preventDefault();
          document.getElementById("btn-toggle-archive")?.click();
        }
      });
    }

    // PARALLEL-CHAT: update the DISPLAYED chat's sidebar highlight INSTANTLY (synchronously)
    // WITHOUT waiting for a full list reload (async fetch). The old model set the highlight
    // only in renderChatArchiveList's async re-render → lag on switch + race with background
    // repaint = "the chat I selected doesn't get highlighted" (user report). setConversationId
    // calls this on every conv change → highlight is always on the displayed chat, instantly.
    function setActiveConversationHighlight(convId) {
      const list = document.getElementById("chat-archive-list");
      if (!list || typeof list.querySelectorAll !== "function") return;
      const id = convId == null ? "" : String(convId);
      for (const btn of list.querySelectorAll(".chat-archive-item")) {
        btn.classList.toggle("is-active", (btn.dataset?.conversationId || "") === id);
      }
    }

    return {
      patchConversationApi,
      deleteConversationApi,
      loadChatArchiveList,
      setActiveConversationHighlight,
      removeArchiveRow,
      insertConversationLocally,
      renderChatArchiveList,
      openArchiveDrawer,
      closeArchiveDrawer,
      wireArchiveChrome,
      wireThreadBar,
      exportConversationMarkdown,
      refreshActiveConversationMeta,
      syncChatThreadBar,
      refreshConvActivityFromServer,
      clearConvActivity,
      getChatArchiveItems: () => chatArchiveItems,
      setChatArchiveItems: (v) => {
        chatArchiveItems = v;
      },
      getActiveConversationMeta: () => activeConversationMeta,
      setActiveConversationMeta: (v) => {
        activeConversationMeta = v;
      },
    };
  }

  window.AkanaChatArchive = { createArchive };
})();
