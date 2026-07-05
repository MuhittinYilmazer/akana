/**
 * Akana chat store — localStorage thread persistence (survives F5).
 */
(() => {
  const LS_CHAT_STORE = "akana.chatStore.v1";
  // SLIM PERSIST: localStorage does NOT mirror the full server state. The server
  // (GET /conversations/{id}/messages) is the source of truth; localStorage only keeps
  // a fast-resume snapshot → the last SNAPSHOT_MAX messages of the active thread
  // (provisional paint) + for inactive threads ONLY the local-only tail (unconfirmed
  // pending / local error cards; body is re-fetched from server on switch). This keeps
  // the store tiny and eliminates the old "quota giving up" + aggressive text-truncation
  // bug class at the root. (The old model mirrored ALL threads with full bodies and
  // DESTRUCTIVELY pruned the in-memory store when the quota was full — new persist is non-destructive.)
  const SNAPSHOT_MAX = 30; // provisional snapshot ceiling for the active thread
  const THREAD_PERSIST_MAX = 24; // soft ceiling for the number of persisted threads
  const CHAT_MAX_MSGS = 60; // defensive cap on load (sanitizeLoadedThreads)

  function createStore() {
    let chatPersistPaused = false;

    // Pending user message texts actually sent in THIS PAGE SESSION (trimmed).
    // Lives in memory only — NOT written to localStorage; reset on every page load.
    // Memory Studio is a SEPARATE page (/memory): navigating there and back is a FULL
    // reload → chatStore is re-read from disk; a frozen _pendingUser resurrects as a GHOST.
    // mergeServerMessages uses this set to distinguish "genuinely sent this session (keep)"
    // from "stale ghost revived from disk (server is source of truth → drop)"
    // (user report: "sometimes the first message I sent reappears as the last message").
    const _sessionPendingTexts = new Set();

    function chatProfile() {
      return "cursor";
    }

    function newChatThreadId() {
      if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
      return `t-${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
    }

    // Single message text must not exceed this: a very large streamed message left
    // incomplete during a crash may have landed in localStorage (even without a server record)
    // and would freeze the main thread on boot render. Trimmed on load (recurrence
    // protection; extra defense on top of the render-side markdown cap).
    const MSG_TEXT_MAX = 16000;

    function sanitizeLoadedThreads(threads) {
      for (const t of Object.values(threads)) {
        if (!t || !Array.isArray(t.messages)) {
          if (t) t.messages = [];
          continue;
        }
        if (t.messages.length > CHAT_MAX_MSGS) t.messages = t.messages.slice(-CHAT_MAX_MSGS);
        for (const m of t.messages) {
          if (m && typeof m.text === "string" && m.text.length > MSG_TEXT_MAX) {
            m.text = m.text.slice(0, MSG_TEXT_MAX) + "\n\n… (stored message truncated)";
          }
        }
      }
      return threads;
    }

    function loadChatStore() {
      try {
        const raw = localStorage.getItem(LS_CHAT_STORE);
        if (!raw) return { version: 1, activeByProfile: {}, threads: {} };
        const o = JSON.parse(raw);
        if (!o || typeof o !== "object") return { version: 1, activeByProfile: {}, threads: {} };
        const threads = o.threads && typeof o.threads === "object" ? o.threads : {};
        return {
          version: 1,
          activeByProfile: o.activeByProfile && typeof o.activeByProfile === "object" ? o.activeByProfile : {},
          threads: sanitizeLoadedThreads(threads),
        };
      } catch {
        return { version: 1, activeByProfile: {}, threads: {} };
      }
    }

    let chatStore = loadChatStore();
    let _chatStoreSaveTimer = null;

    function isQuotaError(e) {
      // Browsers use different names/codes for QuotaExceededError:
      // Chrome/Firefox: name === "QuotaExceededError"; old Firefox: code === 22;
      // old WebKit: name === "NS_ERROR_DOM_QUOTA_REACHED" or code === 1014.
      if (!e) return false;
      return (
        e.name === "QuotaExceededError" ||
        e.name === "NS_ERROR_DOM_QUOTA_REACHED" ||
        e.code === 22 ||
        e.code === 1014
      );
    }

    /** Trailing "local-only" tail: _pendingUser / _localError messages not yet on the
     *  server. In slim persist, ONLY this tail is stored for inactive threads
     *  (body is re-fetched from server on switch). */
    function localOnlyTail(messages) {
      let start = messages.length;
      while (
        start > 0 &&
        messages[start - 1] &&
        (messages[start - 1]._pendingUser || messages[start - 1]._localError)
      ) {
        start--;
      }
      return messages.slice(start);
    }

    function capText(m, textCap) {
      if (m && typeof m.text === "string" && m.text.length > textCap) {
        return { ...m, text: m.text.slice(0, textCap) + "\n\n… (truncated in cache; full version on server)" };
      }
      return m;
    }

    /** Produce the SLIM persist view of chatStore — does NOT mutate the in-memory store.
     *  Active thread: last `activeCap` messages (provisional snapshot). Inactive thread:
     *  local-only tail only. Each message text is capped at `textCap` (full text on server).
     *  When thread count exceeds THREAD_PERSIST_MAX, active threads + most recent are kept. */
    function serializeForPersist(activeCap, textCap) {
      const activeIds = new Set(Object.values(chatStore.activeByProfile));
      let ids = Object.keys(chatStore.threads);
      if (ids.length > THREAD_PERSIST_MAX) {
        ids = ids
          .slice()
          .sort((a, b) => (chatStore.threads[b]?.updatedAt || 0) - (chatStore.threads[a]?.updatedAt || 0))
          .filter((id, i) => i < THREAD_PERSIST_MAX || activeIds.has(id));
      }
      const threads = {};
      for (const id of ids) {
        const t = chatStore.threads[id];
        if (!t) continue;
        const msgs = Array.isArray(t.messages) ? t.messages : [];
        const kept = activeIds.has(id) ? msgs.slice(-activeCap) : localOnlyTail(msgs);
        threads[id] = {
          id: t.id,
          profile: t.profile,
          conversationId: t.conversationId ?? null,
          title: t.title,
          updatedAt: t.updatedAt || 0,
          messages: kept.map((m) => capText(m, textCap)),
        };
      }
      return { version: 1, activeByProfile: { ...chatStore.activeByProfile }, threads };
    }

    function persistChatStoreNow() {
      // The slim snapshot comfortably fits within the quota on its own. In extreme cases
      // the last-resort ladder: (1) drop the oldest INACTIVE thread from the persist view;
      // (2) if none remain (pathological large active thread), geometrically shrink the
      // active snapshot. The in-memory chatStore is NEVER touched — only the view written
      // to disk is reduced.
      let activeCap = SNAPSHOT_MAX;
      let textCap = MSG_TEXT_MAX;
      const snap = serializeForPersist(activeCap, textCap);
      const activeIds = new Set(Object.values(snap.activeByProfile));
      for (let attempt = 0; attempt < 12; attempt++) {
        try {
          localStorage.setItem(LS_CHAT_STORE, JSON.stringify(snap));
          return;
        } catch (e) {
          if (!isQuotaError(e)) {
            console.warn("chat store save failed", e);
            return;
          }
          const inactive = Object.keys(snap.threads).filter((id) => !activeIds.has(id));
          if (inactive.length) {
            inactive.sort((a, b) => (snap.threads[a].updatedAt || 0) - (snap.threads[b].updatedAt || 0));
            // Drop a BATCH of the oldest inactive snapshots per attempt (not just one):
            // with a large conversation backlog, one-per-attempt exhausts the 12-attempt
            // budget and the save "gives up" → localStorage stays full and chat history
            // stops persisting. The in-memory chatStore is untouched; dropped threads
            // re-hydrate from the server on switch.
            const batch = Math.max(1, Math.ceil(inactive.length / 4));
            for (let i = 0; i < batch; i++) delete snap.threads[inactive[i]];
            continue;
          }
          // No inactive threads left → shrink the active snapshot (on snap directly;
          // do NOT re-serialize so previous inactive-drops are preserved).
          // Trailing pending/error tail is ALWAYS preserved (loss guard).
          if (activeCap <= 6 && textCap <= 2000) break;
          activeCap = Math.max(6, Math.floor(activeCap / 2));
          textCap = Math.max(2000, Math.floor(textCap / 2));
          for (const id of activeIds) {
            const t = snap.threads[id];
            if (!t || !Array.isArray(t.messages)) continue;
            const tail = localOnlyTail(t.messages);
            const head = t.messages.slice(0, t.messages.length - tail.length).slice(-activeCap);
            t.messages = head.concat(tail).map((m) => capText(m, textCap));
          }
        }
      }
      console.warn("chat store save failed after trim — giving up this round");
    }

    function saveChatStore() {
      if (_chatStoreSaveTimer) clearTimeout(_chatStoreSaveTimer);
      _chatStoreSaveTimer = setTimeout(() => {
        _chatStoreSaveTimer = null;
        persistChatStoreNow();
      }, 350);
    }

    function flushChatStore() {
      if (_chatStoreSaveTimer) {
        clearTimeout(_chatStoreSaveTimer);
        _chatStoreSaveTimer = null;
      }
      persistChatStoreNow();
    }

    function chatActiveThread() {
      const profile = chatProfile();
      const tid = chatStore.activeByProfile[profile];
      return tid && chatStore.threads[tid] ? chatStore.threads[tid] : null;
    }

    function ensureChatThread(profile) {
      let tid = chatStore.activeByProfile[profile];
      if (tid && chatStore.threads[tid]) return chatStore.threads[tid];
      tid = newChatThreadId();
      chatStore.threads[tid] = {
        id: tid,
        profile,
        conversationId: null,
        title: window.AkanaI18n.t("chat.new_thread_title"),
        updatedAt: Date.now(),
        messages: [],
      };
      chatStore.activeByProfile[profile] = tid;
      saveChatStore();
      return chatStore.threads[tid];
    }

    function chatRecordMessage(msg) {
      const profile = chatProfile();
      const thread = ensureChatThread(profile);
      // Local error cards ARE written to thread.messages with a "_localError" flag. Root cause:
      // server auth failure does NOT persist the error (partial empty → _persist_user_once
      // is not called) → the card lives only here. Without writing it, WS turn_completed →
      // log re-draw (reloadConversationLogFromServer) would delete it → "error text flashes
      // for milliseconds then disappears". mergeServerMessages preserves the trailing _localError
      // tail; a new send (recordPendingUserMessage) sweeps it.
      if (msg && msg.kind === "error") {
        if (!Array.isArray(thread.messages)) thread.messages = [];
        thread.messages.push({
          kind: "error",
          text: typeof msg.text === "string" ? msg.text : "",
          // Kept so the persisted error card's Retry can re-send the failed turn's text.
          userText: typeof msg.userText === "string" ? msg.userText : "",
          ts: "",
          _localError: true,
        });
        thread.updatedAt = Date.now();
        if (!chatPersistPaused) saveChatStore();
        return;
      }
      if (chatPersistPaused) return;
      thread.updatedAt = Date.now();
      if (msg.kind === "user" && thread.title === window.AkanaI18n.t("chat.new_thread_title")) {
        thread.title = (msg.text || "").trim().slice(0, 48) || thread.title;
      }
      saveChatStore();
    }

    // ── Local user-message source of truth (multi-chat loss guard) ──
    // Bug: type in A → open B → type in B → return to A; message typed in A is GONE. Root:
    // the typed user message was ONLY added to the DOM, NEVER written to thread.messages →
    // thread.messages was a pure server-snapshot cache; on switch the server snapshot
    // (which may not yet contain that message) was blindly loaded.
    // Fix: on send, write the message to the ACTIVE thread as "pending";
    // server-snapshot writers should MERGE this (not blind-replace).

    /** Same shape as render/server-sync: {kind:"user", text, ts, fileIds}. */
    function _normalizePendingFileIds(fileIds) {
      return Array.isArray(fileIds) ? fileIds.slice() : [];
    }

    /** Save the typed user message to the active thread as pending.
     *
     * ``_pendingUser`` flag: mergeServerMessages treats this as "not yet confirmed by server".
     * Only the single most-recent pending message per conversation is tracked
     * (invariant: one latest unconfirmed user message). If a previous pending exists at the
     * tail of the same thread (rare — user sent a second time before the first was confirmed),
     * its flag is cleared so at most ONE pending remains at the tail → no duplicate user
     * messages ever created. */
    function recordPendingUserMessage(text, fileIds) {
      const profile = chatProfile();
      const thread = ensureChatThread(profile);
      if (!Array.isArray(thread.messages)) thread.messages = [];
      // A new send "closes" any previous local error cards: sweep the trailing _localError
      // cards. Otherwise, after a successful retry, merge would move those cards BELOW the
      // server turn (order broken). The error card lives until the user reads it and resends
      // — that is the desired persistence.
      while (
        thread.messages.length &&
        thread.messages[thread.messages.length - 1] &&
        thread.messages[thread.messages.length - 1]._localError
      ) {
        thread.messages.pop();
      }
      // Demote the previous trailing pending to a normal message (single-pending invariant).
      const tail = thread.messages[thread.messages.length - 1];
      if (tail && tail._pendingUser) delete tail._pendingUser;
      const msg = {
        kind: "user",
        text: typeof text === "string" ? text : "",
        ts: "",
        fileIds: _normalizePendingFileIds(fileIds),
        _pendingUser: true,
      };
      thread.messages.push(msg);
      _sessionPendingTexts.add((msg.text || "").trim());
      thread.updatedAt = Date.now();
      if (thread.title === window.AkanaI18n.t("chat.new_thread_title")) {
        thread.title = msg.text.trim().slice(0, 48) || thread.title;
      }
      if (!chatPersistPaused) saveChatStore();
      return msg;
    }

    /** MERGE a thread's messages with the server snapshot (not a blind replace).
     *
     * Single responsibility: KEEP the trailing "pending" user message if the server
     * snapshot does NOT yet include it; DROP it (let the server be the source of truth →
     * no duplicate messages) if it does. Deduplication is conservative: only the single
     * most-recent unconfirmed user message is checked; pending is cleared when the text
     * of the server's LAST user turn matches. Text comparison is trimmed (server may strip
     * whitespace; trim only HELPS matching → never retains extra copies; worst case clears
     * pending one turn early, but the real message is already on the server so there is no loss). */
    function mergeServerMessages(thread, serverMessages) {
      const server = Array.isArray(serverMessages) ? serverMessages : [];
      const prev = Array.isArray(thread?.messages) ? thread.messages : [];
      // Find the trailing "local-only" tail: messages that are held locally and not yet
      // on the server. Two types: _pendingUser (unconfirmed user message) and _localError
      // (local error card — server never persists these). This tail is PRESERVED; the rest
      // is server source-of-truth (blind snapshot).
      let start = prev.length;
      while (
        start > 0 &&
        prev[start - 1] &&
        (prev[start - 1]._pendingUser || prev[start - 1]._localError)
      ) {
        start--;
      }
      if (start >= prev.length) return server; // no local tail → blind snapshot
      const localTail = prev.slice(start);
      // Text set of ALL user turns on the server (for pending dedup).
      // Looking only at the LAST turn is NOT enough: if the local store is STALE
      // (chat-store quota write failure → local freezes behind server), the first message
      // stays as _pendingUser; the server's LAST turn is now a DIFFERENT message → first
      // message does not match → no dedup → gets appended via server.concat at the very
      // end and the first message re-renders as the last (user report: "when I go to Memory
      // and come back, the first message I sent reappears as the latest message").
      // If a pending exists ANYWHERE on the server = confirmed → DROP (double-press guard).
      // _localError is always preserved (server never reflects it).
      const serverUserTexts = new Set();
      // Error turns are now PERSISTED server-side (role="error" → kind:"error"); the
      // server snapshot reflects a failed turn like any message. Collect their texts so
      // a local optimistic `_localError` whose error the server already stored is DROPPED
      // (otherwise the card would render twice after a reload). The persisted error text
      // equals the SSE `error` frame `message` the local card was built from → exact match.
      const serverErrorTexts = new Set();
      for (const m of server) {
        if (m && m.kind === "user") {
          serverUserTexts.add((typeof m.text === "string" ? m.text : "").trim());
        } else if (m && m.kind === "error") {
          serverErrorTexts.add((typeof m.text === "string" ? m.text : "").trim());
        }
      }
      const serverEmpty = server.length === 0;
      const preserved = [];
      for (const m of localTail) {
        if (m._pendingUser) {
          const txt = (m.text || "").trim();
          if (serverUserTexts.has(txt)) continue; // on server → confirmed, double-press guard
          // Unmatched pending. Keep it if it was GENUINELY sent this session (server hasn't
          // reflected it yet) or if the server snapshot is EMPTY (new/unpersisted chat) —
          // otherwise "message disappears in new chat" regression returns. Otherwise this is
          // a STALE GHOST revived from localStorage on return from a separate page (/memory)
          // (e.g. quota truncation corrupted the text or a clean merge record was lost) →
          // server has content + no match → would be appended at the end, DROP.
          if (!serverEmpty && !_sessionPendingTexts.has(txt)) continue;
        } else if (m._localError) {
          // The server now persists error turns: if this optimistic card's error is
          // already in the snapshot, DROP the local copy (server is source of truth →
          // no duplicate). If not yet persisted (background write still in flight), KEEP
          // it so the error never flashes away — a later reload dedups it.
          if (serverErrorTexts.has((m.text || "").trim())) continue;
        }
        preserved.push(m); // unmatched _localError preserved until the server reflects it
      }
      return preserved.length ? server.concat(preserved) : server;
    }

    function syncThreadConversationId(id, { save = true } = {}) {
      const thread = chatActiveThread() || ensureChatThread(chatProfile());
      thread.conversationId = id || null;
      if (save && !chatPersistPaused) saveChatStore();
    }

    function activateThreadForConversation(convId) {
      const profile = chatProfile();
      for (const [tid, thread] of Object.entries(chatStore.threads)) {
        if (thread.profile === profile && thread.conversationId === convId) {
          chatStore.activeByProfile[profile] = tid;
          return thread;
        }
      }
      // No local thread bound to this conversation (opened from archive / store was pruned /
      // fresh session). If the active thread is EMPTY and UNBOUND (default title, no
      // conversationId, no messages), reuse it — otherwise create a NEW thread.
      // Old behavior blindly took over the active thread via ensureChatThread and overwrote
      // its conversationId → an active thread bound to another conversation (Y) with content
      // would destroy Y's local record and re-bind it to X (conversation loss).
      const active = chatActiveThread();
      const activeReusable =
        active &&
        active.profile === profile &&
        !active.conversationId &&
        (!Array.isArray(active.messages) || active.messages.length === 0);
      if (activeReusable) {
        active.conversationId = convId;
        return active;
      }
      const tid = newChatThreadId();
      chatStore.threads[tid] = {
        id: tid,
        profile,
        conversationId: convId,
        title: window.AkanaI18n.t("chat.new_thread_title"),
        updatedAt: Date.now(),
        messages: [],
      };
      chatStore.activeByProfile[profile] = tid;
      saveChatStore();
      return chatStore.threads[tid];
    }

    function purgeConversationFromChatStore(convId) {
      if (!convId) return;
      const profile = chatProfile();
      for (const [tid, thread] of Object.entries(chatStore.threads)) {
        if (thread.conversationId === convId) {
          delete chatStore.threads[tid];
          if (chatStore.activeByProfile[profile] === tid) delete chatStore.activeByProfile[profile];
        }
      }
      saveChatStore();
    }

    if (typeof window !== "undefined") {
      window.addEventListener("pagehide", flushChatStore);
      window.addEventListener("beforeunload", flushChatStore);
    }

    return {
      chatProfile,
      newChatThreadId,
      chatActiveThread,
      ensureChatThread,
      chatRecordMessage,
      recordPendingUserMessage,
      mergeServerMessages,
      syncThreadConversationId,
      activateThreadForConversation,
      purgeConversationFromChatStore,
      saveChatStore,
      flushChatStore,
      getChatStore: () => chatStore,
      getChatPersistPaused: () => chatPersistPaused,
      setChatPersistPaused: (v) => {
        chatPersistPaused = !!v;
      },
    };
  }

  window.AkanaChatStore = { createStore };
})();
