/**
 * Akana chat threads — orchestrates store, archive, and conversation lifecycle.
 */
(() => {
  const baseUrl = () => window.AkanaCore.baseUrl();
  const authHeaders = (j) => window.AkanaCore.authHeaders(j);

  // Server NL-command actions (chat.py) → minimal visible notification.
  // Response text is already visible in the bubble; these toasts ensure actions
  // are not silently swallowed (task_route / teach flows).
  const CHAT_ACTION_NOTICES = {
    task_route: { msg: () => window.AkanaI18n.t("thread.action.task_route"), kind: "info" },
    teach_draft: { msg: () => window.AkanaI18n.t("thread.action.teach_draft"), kind: "success" },
    teach_failed: { msg: () => window.AkanaI18n.t("thread.action.teach_failed"), kind: "err" },
  };

  // When "+/new chat" is opened, the displayed empty thread has no conv_id yet.
  // If we left the foreground pointer null, transport's fallback rule
  // "if foreground is unknown, the single registered stream is foreground"
  // (isForegroundStream/isForegroundConv) would mistakenly treat a background
  // stream as foreground → cross-conv leak (A's meta/teardown overwrites the new
  // empty chat's global state). Instead of null, this non-null sentinel matches
  // no real conv_id and no stream key → background streams safely stay background.
  // When the first message is sent, ensureConversationIdReady→setConversationId
  // replaces the sentinel with the real id.
  const EMPTY_THREAD_FOREGROUND = Symbol("akana.empty-thread-foreground");

  function create(bridge) {
    const store = window.AkanaChatStore.createStore();
    const {
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
      getChatStore,
      getChatPersistPaused,
      setChatPersistPaused,
    } = store;

    const chatStore = () => getChatStore();

    // Default title for an empty chat (until the server derives the real title
    // from the first message). isDefaultNewChatTitle uses this to detect "not yet named".
    // NOT a const: captured at module init, this can run BEFORE akana-i18n's async
    // reconcileWithBackend() flips the boot language to the backend value (fresh
    // browser: localStorage empty → boots "en", backend says "tr"). Re-derive on
    // akana:languagechange so new threads and the default-title check track the
    // reconciled/live language instead of staying stuck on the boot-time guess.
    let NEW_THREAD_TITLE = window.AkanaI18n.t("thread.new_chat_title");
    window.addEventListener("akana:languagechange", () => {
      NEW_THREAD_TITLE = window.AkanaI18n.t("thread.new_chat_title");
    });

    /** Create a new empty thread, make it ACTIVE, persist to store; returns tid. */
    function createActiveThread(profile, conversationId) {
      const tid = newChatThreadId();
      chatStore().threads[tid] = {
        id: tid,
        profile,
        conversationId,
        title: NEW_THREAD_TITLE,
        updatedAt: Date.now(),
        messages: [],
      };
      chatStore().activeByProfile[profile] = tid;
      saveChatStore();
      return tid;
    }

    let archive;
    let chatNewThreadInFlight = null;
    // Conversation-switch generation counter: in rapid successive switchChatConversation
    // calls, prevents a stale hydrate from the OLD switch from writing into the NEW
    // chat's log (after each await, stale switch aborts if myGen !== _switchGen).
    let _switchGen = 0;
    let conversationId = sessionStorage.getItem("akana.conversationId") || null;

    // ── Archive/meta refresh storm shield ─────────────────────────────────────
    // setConversationId used to trigger loadChatArchiveList + refreshActive
    // ConversationMeta on every call; called multiple times within a turn this
    // piled up `/conversations` and `/conversations/{id}` requests.
    // Coalesce: schedule a single deferred refresh for the latest convId (dedupe).
    let _archiveRefreshTimer = null;
    let _lastArchiveRefreshConv = Symbol("init");
    function scheduleArchiveRefresh(convId) {
      if (convId === _lastArchiveRefreshConv && _archiveRefreshTimer) return;
      _lastArchiveRefreshConv = convId;
      if (_archiveRefreshTimer) clearTimeout(_archiveRefreshTimer);
      _archiveRefreshTimer = setTimeout(() => {
        _archiveRefreshTimer = null;
        void archive.loadChatArchiveList();
        void archive.refreshActiveConversationMeta();
      }, 120);
    }

    const CHAT_DELETE_COMMAND_RE =
      /^(?:sohbet(?:i)?\s+sil|sil\s+sohbet(?:i)?|bu\s+sohbet(?:i)?\s+sil|chat\s+sil|delete\s+(?:this\s+)?(?:chat|conversation))$/i;

    function conversationIdForMemory() {
      const fromThread = chatActiveThread()?.conversationId;
      if (fromThread) return fromThread;
      return conversationId || "";
    }

    function resolveConversationId(preferred) {
      if (preferred) return preferred;
      return conversationIdForMemory() || null;
    }

    function isEmptyChatSurface() {
      if (bridge.hooks.log && bridge.hooks.log.children.length > 0) return false;
      const thread = chatActiveThread();
      return !thread?.messages?.length;
    }

    function isDefaultNewChatTitle(title) {
      const t = (title || "").trim();
      return !t || t === NEW_THREAD_TITLE;
    }

    function canReuseCurrentEmptyThread() {
      if (!isEmptyChatSurface()) return false;
      const thread = chatActiveThread();
      if (!thread) return false;
      if (!isDefaultNewChatTitle(thread?.title)) return false;
      if (archive.getActiveConversationMeta()?.title && !isDefaultNewChatTitle(archive.getActiveConversationMeta().title)) {
        return false;
      }
      return true;
    }

    function isConversationDisplayed(convId) {
      if (!convId) return false;
      const thread = chatActiveThread();
      return Boolean(
        thread?.conversationId === convId && bridge.hooks.log && bridge.hooks.log.children.length > 0,
      );
    }

    async function chatHydrateFromServer(convId, targetThread = null) {
      if (!convId) return false;
      try {
        // Turns + meta(title) are INDEPENDENT reads → start in PARALLEL (not sequential).
        // Previously meta fetch waited for turns (extra 1 round-trip latency);
        // since conversation switching is a hot path, fire both simultaneously.
        const turnsP = bridge.fetchConversationTurns(convId);
        const metaP = fetch(
          `${baseUrl()}/api/v1/conversations/${encodeURIComponent(convId)}`,
          { headers: authHeaders() },
        ).catch(() => null);
        const { status, turns } = await turnsP;
        if (status === 404) {
          void metaP.catch?.(() => {});
          const thread = targetThread || chatActiveThread() || ensureChatThread(chatProfile());
          // NEW-CHAT GUARD (user report: "send in A → open new chat B + send →
          // switch to A → RETURN to B; B missing Jarvis's message + crashes after").
          // The turns of a freshly created conv may not yet be queryable on the server
          // (404: early-persist / eventual-consistency window). If this conv is LIVE
          // IN MEMORY or has local/pending messages, do NOT destroy the thread —
          // otherwise conversationId=null + messages are wiped → the live row is left
          // orphaned and the next send goes to the wrong/new conv ("crash"). The stream
          // comes back via reattachLiveRow/resume; once the server persists,
          // syncConversationLogFromServer reflects it. (Real deletes go through
          // deleteConversationById/purge, NOT this path.)
          // Note: syncConversationLogFromServer's 404 also leaves the thread untouched
          // — this branch is now consistent with that.
          const liveStream = Boolean(bridge.isConversationStreamActive?.(convId));
          const hasLocal = Boolean(thread && thread.messages && thread.messages.length);
          if (liveStream || hasLocal) return false;
          thread.conversationId = null;
          thread.messages = [];
          saveChatStore();
          setConversationId(null);
          // A2: as in the switch-failure path — leaving the foreground gate null lets a lone
          // background stream be treated as foreground and rebind the active thread on `done`.
          // Park the gate on the sentinel instead (matches the new-chat path).
          try { bridge.setForegroundConversation?.(EMPTY_THREAD_FOREGROUND); } catch { /* ignore */ }
          return false;
        }
        const thread = targetThread || chatActiveThread() || ensureChatThread(chatProfile());
        // MERGE (not a blind replace): the server snapshot may not yet contain the
        // message the user just wrote → preserve the trailing pending message
        // (multi-chat loss shield). Once the server reflects it, merge collapses to one copy.
        thread.messages = mergeServerMessages(thread, bridge.mapServerMessagesToThread(turns));
        thread.conversationId = convId;
        const metaR = await metaP;
        if (metaR && metaR.ok) {
          const meta = await metaR.json();
          if (meta.title) thread.title = meta.title;
        }
        saveChatStore();
        return true;
      } catch {
        return false;
      }
    }

    /** Find the thread bound to `convId` in the local store (need not be active). */
    function threadForConversation(convId) {
      if (!convId) return null;
      const profile = chatProfile();
      for (const t of Object.values(chatStore().threads)) {
        if (t && t.profile === profile && t.conversationId === convId) return t;
      }
      return null;
    }

    /** A1: persist an error card to the thread OF A SPECIFIC conversation (not the
     * active thread). Parallel-chat lets the user switch chats mid-stream, so
     * chatRecordMessage's active-thread ensureChatThread would write the failed
     * turn's error (with conversation A's userText) into the WRONG thread. Resolve
     * the target thread by convId and write the same _localError shape as
     * chatRecordMessage; bail if the conversation has no local thread. */
    function recordErrorForConversation(convId, msg) {
      const thread = threadForConversation(convId);
      if (!thread) return false;
      if (!Array.isArray(thread.messages)) thread.messages = [];
      thread.messages.push({
        kind: "error",
        text: msg && typeof msg.text === "string" ? msg.text : "",
        // Kept so the persisted error card's Retry can re-send the failed turn's text.
        userText: msg && typeof msg.userText === "string" ? msg.userText : "",
        ts: "",
        _localError: true,
      });
      thread.updatedAt = Date.now();
      if (!getChatPersistPaused()) saveChatStore();
      return true;
    }

    /** Update thread store from server without touching the live chat log DOM. */
    async function syncConversationLogFromServer(convId) {
      if (!convId) return false;
      try {
        const { status, turns } = await bridge.fetchConversationTurns(convId);
        if (status === 404) return false;
        // This sync must write to the thread BELONGING to `convId`. Callers are
        // fire-and-forget (end of streamChat/resume) — the user may have switched to
        // another chat during the fetch await. Blindly overwriting the active thread
        // would corrupt the NEW chat: A's turns get written to B + B.conversationId=A
        // → the next message goes to the wrong conversation. Resolve by convId; bail if not found.
        const thread = threadForConversation(convId);
        if (!thread) return false;
        // MERGE: preserve the trailing pending user message (if server hasn't reflected
        // it yet); if it has, the merge collapses to one copy — see chatHydrateFromServer.
        thread.messages = mergeServerMessages(thread, bridge.mapServerMessagesToThread(turns));
        thread.conversationId = convId;
        flushChatStore();
        return true;
      } catch {
        return false;
      }
    }

    function scrollLogToEnd() {
      bridge.hooks.scrollLogToBottom?.(bridge.hooks.logScroll || bridge.hooks.log);
    }

    function beginLogHydrate() {
      bridge.hooks.log.innerHTML = "";
      bridge.hooks.setLogLoading?.(true);
    }

    function finishLogHydrate() {
      bridge.hooks.setLogLoading?.(false);
      bridge.hooks.updateEmptyState();
      scrollLogToEnd();
    }

    /** Rebuild the log from server if the turn is finished or no resume is pending (F5 / tab switch).
     *  expectedTurnId (optional): the turn this refresh was asked to render (WS
     *  assistant_turn_id of the COMPLETED turn). */
    async function refreshConversationLogAfterTurn(convId, expectedTurnId) {
      if (!convId || !bridge.hooks.log) return false;
      try {
        const active = await bridge.probeActiveTurn?.(convId);
        if (active) {
          // probeActiveTurn returns a live SSE follower Response on an active turn —
          // only used here as a boolean; release the body so it doesn't leak an open
          // follower connection for the rest of the turn (same fix as transport's
          // resumeActiveTurn b17 comment).
          try {
            await active.body?.cancel();
          } catch {
            /* ignore */
          }
          // A3: skipping the reload on ANY active turn permanently dropped a just-completed
          // turn's answer from the DOM when its completion DRAINED the next turn in the same
          // conversation (T1 done → T2 becomes active). When we know WHICH turn we were asked
          // to render (expectedTurnId), only skip if the currently-active turn IS that same
          // turn; if it is a DIFFERENT (drained) turn, fall through and reload so the completed
          // turn's answer is rendered (the live turn's row is kept unique by tagStreamRowTurnId).
          // Without an expectedTurnId (switch/restore recovery callers) keep the original
          // conservative behaviour: any active turn → do not reload (avoid clobbering it).
          if (!expectedTurnId) return false;
          const liveTid = bridge.activeStreamTurnId?.(convId);
          const sameTurn = liveTid && String(liveTid) === String(expectedTurnId);
          if (sameTurn) return false;
        }
      } catch {
        /* ignore */
      }
      return reloadConversationLogFromServer(convId);
    }

    /** Rebuild chat log from server (e.g. switching conversations). */
    async function reloadConversationLogFromServer(convId) {
      if (!convId || !bridge.hooks.log) return false;
      const { status, turns } = await bridge.fetchConversationTurns(convId);
      if (status === 404) return false;
      const thread = chatActiveThread();
      // Stale-reload guard: if the user switched to ANOTHER chat during the fetch
      // await (active thread is now bound to a different conversation), convId is
      // no longer on screen — do not inject A's turns into B's live log.
      // Binding to an unbound (null) active thread is allowed (initial load / new empty chat path).
      if (!thread || (thread.conversationId && thread.conversationId !== convId)) {
        return false;
      }
      // MERGE: preserve the trailing pending user message (if server hasn't reflected
      // it yet); once it has, merge collapses to one copy — see chatHydrateFromServer.
      thread.messages = mergeServerMessages(thread, bridge.mapServerMessagesToThread(turns));
      thread.conversationId = convId;
      setChatPersistPaused(true);
      bridge.showConversation?.(convId); // PARALLEL-CHAT: hydrate should only affect this conv's pane
      beginLogHydrate();
      try {
        for (const m of thread?.messages || []) bridge.chatRenderMessage(m);
        flushChatStore();
        return true;
      } finally {
        setChatPersistPaused(false);
        finishLogHydrate();
      }
    }

    function conversationDeleteLabel(convId) {
      const fromArchive = archive.getChatArchiveItems().find((c) => c.id === convId);
      if (fromArchive?.title) return fromArchive.title;
      if (convId === conversationIdForMemory() && archive.getActiveConversationMeta()?.title) {
        return archive.getActiveConversationMeta().title;
      }
      const thread = Object.values(chatStore().threads).find((t) => t.conversationId === convId);
      if (thread?.title) return thread.title;
      return bridge.hooks.shortConversationId(convId);
    }

    async function deleteConversationById(convId, opts = {}) {
      const { confirm = true, quiet = false } = opts;
      _switchGen += 1; // stale any in-flight conversation switch (don't let it clobber the new thread)
      convId = resolveConversationId(convId);
      if (!convId) {
        if (!isEmptyChatSurface()) {
          if (confirm) {
            const ok = window.confirm(window.AkanaI18n.t("thread.confirm.clear_chat"));
            if (!ok) return false;
          }
          bridge.hooks.log.innerHTML = "";
          const thread = chatActiveThread();
          if (thread) thread.messages = [];
          saveChatStore();
          bridge.hooks.updateEmptyState();
          archive.syncChatThreadBar();
          if (!quiet) bridge.hooks.showToast(window.AkanaI18n.t("thread.toast.chat_cleared"));
          return true;
        }
        if (canReuseCurrentEmptyThread()) {
          if (!quiet) bridge.hooks.showToast(window.AkanaI18n.t("thread.toast.already_new_chat"));
          return true;
        }
        await chatStartNewThread({ force: true });
        if (!quiet) bridge.hooks.showToast(window.AkanaI18n.t("thread.toast.session_started"));
        return true;
      }
      if (confirm) {
        const ok = window.confirm(
          window.AkanaI18n.t("thread.confirm.delete_chat", { title: conversationDeleteLabel(convId) }),
        );
        if (!ok) return false;
      }
      // Is the deleted chat the DISPLAYED one? Determine BEFORE the mutation:
      // deleting a background chat from the archive must not abort the live stream of the active chat.
      const isActiveConv = convId === conversationIdForMemory();
      try {
        // ALWAYS abort the client stream for the deleted conv — even if it is a
        // background chat. audit C2: previously only `if (isActiveConv) abortStream()`
        // was called → when deleting a background chat while it was streaming, the
        // client SSE reader + _streamsByConv entry leaked and kept writing to the
        // detached (pane-deleted) bubble. abortStream(convId) only aborts THAT conv;
        // it does not touch other streams (concurrent n-chat safe).
        bridge.abortStream(convId);
        // ROOT CAUSE OF SLOW DELETES: previously `await cancelActiveTurnOnServer` was here
        // → the server cancel endpoint WAITED for the turn's finally (mid-LLM abort)
        // up to 15 s → deletes took 5-6 s. Now FIRE-AND-FORGET (void): the server still
        // cancels the turn (background chat's turn also stops) but the frontend does NOT
        // await the response → delete returns instantly. The DELETE route additionally
        // cancels the turn without awaiting + blocks late tombstone writes (order-safe).
        if (convId) void bridge.cancelActiveTurnOnServer?.(convId);
        // OPTIMISTIC REMOVAL: synchronously drop the row WITHOUT waiting for the network
        // DELETE — deletion feels "instant". The old path `setChatArchiveItems(filter)`
        // only updated the array (no DOM touch) → visual removal depended solely on the
        // fire-and-forget loadChatArchiveList; if that refresh was suppressed by the
        // generation guard during heavy navigation/search, the row stayed visible and
        // the delete appeared SLOW/broken.
        archive.removeArchiveRow?.(convId);
        await archive.deleteConversationApi(convId);
        archive.clearConvActivity?.(convId);
        purgeConversationFromChatStore(convId);
        bridge.removeConversation?.(convId); // PARALLEL-CHAT: remove the deleted chat's pane from DOM
        if (isActiveConv) {
          setConversationId(null);
          archive.setActiveConversationMeta(null);
          // Clear the log and show the hero (empty state) — adding a permanent
          // "deleted" row after the deletion would hide the hero. A toast is enough.
          await chatStartNewThread({ force: true, localOnly: true });
        }
        if (!quiet) bridge.hooks.showToast(window.AkanaI18n.t("thread.toast.chat_deleted"));
        void archive.loadChatArchiveList();
        return true;
      } catch (e) {
        // Optimistic removal: if API fails after removal, reload authoritative list
        // so the row comes back — user should not mistakenly think it was deleted.
        void archive.loadChatArchiveList();
        bridge.hooks.showToast(e.message || String(e), "err");
        return false;
      }
    }

    async function archiveConversationById(convId, opts = {}) {
      const { quiet = false } = opts;
      _switchGen += 1; // stale any in-flight conversation switch (don't let it clobber the new thread)
      convId = resolveConversationId(convId);
      if (!convId) return false;
      // Is the archived chat the DISPLAYED one? Determine BEFORE the mutation:
      // archiving the active chat MID-STREAM must not leave the live stream orphaned
      // (a new thread is opened, but the old SSE keeps pumping in the background and
      // the STOP state/foreground gate could leak into the new empty thread).
      // Same teardown as delete: if active, abort client stream first, then
      // fire-and-forget cancel the server turn (don't AWAIT the response → archiving feels instant).
      const isActiveConv = convId === conversationIdForMemory();
      try {
        // Targeted abort of the client stream (archived conv — active or background).
        // The pane is about to be removed; don't let it write to a detached node
        // (audit M1 leak). abortStream(convId) only aborts THAT conv; it leaves
        // other chats' streams untouched.
        bridge.abortStream(convId);
        // Server turn: cancel ONLY for the active chat (archive is REVERSIBLE → let a
        // background turn finish, it will be visible after unarchive; this distinguishes
        // archive from delete).
        if (isActiveConv) void bridge.cancelActiveTurnOnServer?.(convId);
        // Optimistic: immediately drop the row from the active list (moving to archive).
        // tombstone:false — archive is a MOVE, not a delete; the conv must still render
        // in the archived tab (tombstoning hid it there until an F5).
        archive.removeArchiveRow?.(convId, { tombstone: false });
        await archive.patchConversationApi(convId, { archived: true });
        bridge.removeConversation?.(convId); // remove archived pane from DOM (no orphan/leak)
        if (isActiveConv) {
          setConversationId(null);
          archive.setActiveConversationMeta(null);
          await chatStartNewThread({ force: true, localOnly: true });
        }
        if (!quiet) bridge.hooks.showToast(window.AkanaI18n.t("thread.toast.chat_archived"));
        void archive.loadChatArchiveList();
        return true;
      } catch (e) {
        // If the API fails after an optimistic removal, reload the authoritative list.
        void archive.loadChatArchiveList();
        bridge.hooks.showToast(e.message || String(e), "err");
        return false;
      }
    }

    function tryHandleChatDeleteCommand(text) {
      if (!CHAT_DELETE_COMMAND_RE.test(text.trim())) return false;
      void deleteConversationById(conversationIdForMemory() || null);
      return true;
    }

    function setConversationId(id) {
      conversationId = id || null;
      if (id) sessionStorage.setItem("akana.conversationId", id);
      else sessionStorage.removeItem("akana.conversationId");
      // DISPLAYED chat = canonical active conv. Bind the foreground UI gate (transport)
      // to it: switch/new/restore/delete all go through setConversationId →
      // foreground is synced from a single point. NOTE: BACKGROUND streams do NOT call
      // setConversationId (adoptStreamConversationId only calls it in the foreground stream)
      // → a background stream never shifts the displayed chat; this call is safe.
      try {
        bridge.setForegroundConversation?.(id || null);
      } catch {
        /* ignore */
      }
      syncThreadConversationId(id, { save: !getChatPersistPaused() });
      bridge.hooks.updateSettingsHero(null, null);
      const convEl = document.getElementById("memory-stat-conv");
      if (convEl) convEl.textContent = id ? bridge.hooks.shortConversationId(id) : window.AkanaI18n.t("thread.memory_stat.none");
      if (document.body.getAttribute("data-settings-tab") === "memory") {
        void bridge.hooks.loadMemoryConversations();
      }
      scheduleArchiveRefresh(id || null);
      archive.syncChatThreadBar();
      // PARALLEL-CHAT: immediately update sidebar highlight to the displayed chat
      // (WITHOUT waiting for the async loadChatArchiveList re-render) →
      // fixes "highlight for the chat I selected is buggy". Full reload arrives
      // later with the same value, keeping things consistent.
      archive.setActiveConversationHighlight?.(id || null);
    }

    async function switchChatConversation(convId) {
      if (!convId || isConversationDisplayed(convId)) return;
      bridge.clearPendingAttachments?.(); // EC2: don't carry pending attachments into another chat
      // CONNECTION-LIMIT FIX: capture the leaving (currently displayed) chat BEFORE
      // the mutation → to release its POST stream once the switch completes
      // (see abortStream call below + rationale). Skip if "" (empty surface).
      const _leavingConvId = conversationIdForMemory();
      const myGen = ++_switchGen; // generation counter for this switch
      // OPTIMISTIC NAVIGATION (claude-code "view ≠ IO" principle): do NOT tie the
      // VISIBLE transition to any REST/IO. Previously `await restoreConversationLlm`
      // was here → under heavy streaming its GET/PUT/loadModelPill chain queued for
      // SECONDS and blocked the synchronous showConversation() below →
      // "clicking another chat while a response is streaming doesn't switch" bug.
      // Solution: defer conv-model loading to BELOW, AFTER the visible transition
      // (pane + foreground + tts) completes — void, never awaited. This way the
      // pane switches INSTANTLY even under heavy streaming.
      // Flip the foreground UI gate to the new chat FIRST (composer/"Responding"/TTS
      // now locks to B's stream; A becomes background and its POST is released below).
      try {
        bridge.setForegroundConversation?.(convId);
      } catch {
        /* ignore */
      }
      // === CONCURRENT N-CHAT — CONNECTION-LIMIT FIX ===
      // RELEASE the POST stream of the leaving chat (A). Browsers hold ~6 connections
      // per host (HTTP/1.1); every LIVE chat held one SSE POST for the full turn →
      // with 3-4 parallel chats the pool filled and new streams got stuck "pending"
      // (user: ">3 parallel chats don't work / stream not visible"). Previously the
      // stream was NOT aborted on switch (held POST → background live but already
      // INVISIBLE in single-pane mode; other panes hidden via showConversation).
      // The cost was this connection cap.
      // NOW abort the leaving conv's POST — but ONLY the client fetch:
      // abortStream does NOT cancel the server turn (cancelActiveTurnOnServer is a
      // separate call; see deleteConversationById). The turn continues DETACHED on
      // the server; when it finishes, WS turn_completed → onBackgroundTurnCompleted
      // "response ready" toast; returning to the chat, hydrate (if still running,
      // resumeActiveTurn LIVE) retrieves the full response. This way only the
      // DISPLAYED conv holds a POST at any time → pool never exhausted, N parallel
      // chats work. NOTE: called AFTER setForegroundConversation(convId) → the leaving
      // conv is no longer foreground → abortStream does not reset its composer/STOP
      // state (only releases the connection). abortStream is a no-op if no record (safe
      // for non-streaming chats).
      // PARALLEL-CHAT FIX (#1) — THE "connection cap" abort ABOVE WAS REVERTED:
      // do NOT abort the leaving chat's live stream. abortStream was killing background
      // streams and causing "response disappears from screen when switching chat" bug:
      // resume doesn't reliably retrieve a detached turn + with early-abort the server
      // turn dies before persisting → on return stream-active=false → wipe branch +
      // blank re-render from server.
      // PaneManager keeps each chat in its OWN hidden pane → stream finishes in the
      // background, it's there when you return. Trade-off: ~3-4 concurrent live stream
      // cap on HTTP/1.1 (rare; will be resolved with HTTP/2 / queue in the future).
      void _leavingConvId; // background stream preserved (no abort)
      // PARALLEL-CHAT PANES: SHOW the target chat's pane (others are hidden,
      // NO wipe). From here hooks.log resolves to this pane → hydrate/append
      // only affects this pane; background chats' panes + live streams are untouched.
      bridge.showConversation?.(convId);
      // #15: STOP playing/queued TTS — otherwise the old conv's response keeps
      // speaking while a new chat is open. NOTE: this only silences the PLAYING audio
      // (player reset); it does NOT abort A's stream (the stream continues, background
      // TTS chunks are already gated by the foreground gate).
      try {
        window.AkanaVoice?.ttsPlayer?.reset?.();
      } catch {
        /* ignore */
      }
      // VISIBLE TRANSITION COMPLETE (pane + foreground + tts synchronous) → now load
      // the conv MODEL in the background. Replaces the blocking await from before:
      // void → the transition does NOT wait for this; the pane already changed INSTANTLY
      // even under heavy streaming. restoreConversationLlm swallows errors via its own
      // try/catch and depends on no sub-step result; it is triggered here before
      // whichever branch below runs (live fast-path / hydrate / reattach) →
      // model pill is refreshed consistently on every switch.
      void window.AkanaSettings?.restoreConversationLlm?.(convId);
      // PARALLEL-CHAT: if the target chat is ALREADY streaming live, its OWN pane
      // already holds the history + live row (never wiped — panes are persistent).
      // Do NOT perform a destructive hydrate (beginLogHydrate wipe + redraw from server)
      // → the old model deleted the pane here and relied on reattach, which often
      // failed ("returning to a new chat, Jarvis's message is missing + crash" root bug).
      // Only sync active-thread + global conv + composer/sidebar.
      // (Idle chats are hydrated normally below; wipe is safe there because no live row.)
      if (bridge.isConversationStreamActive?.(convId)) {
        activateThreadForConversation(convId);
        setConversationId(convId);
        try { bridge.syncComposerForDisplayed?.(convId); } catch { /* ignore */ }
        void window.AkanaChat?.refreshQueueState?.(convId);
        void archive.refreshConvActivityFromServer?.(convId);
        void archive.loadChatArchiveList();
        void archive.refreshActiveConversationMeta();
        flushChatStore();
        return;
      }
      setChatPersistPaused(true);
      beginLogHydrate();
      let hydrateOk = false;
      let hadLocal = false; // diagnostic: was the local cache rendered immediately
      try {
        const thread = activateThreadForConversation(convId);
        setConversationId(convId);
        // INSTANT LOCAL CACHE: if thread.messages is non-empty, render it IMMEDIATELY.
        // Under server load (concurrent streams) the hydrate fetch can take SECONDS
        // (measured: hydrateMs ~2000ms). Since beginLogHydrate empties the log, the
        // screen would be BLANK for ~2 s, feeling like a crash (user report).
        // If a cache exists, show it instead of a blank screen; replace with fresh
        // data when the server responds. mergeServerMessages already prevents loss/duplication.
        hadLocal = !!(thread && thread.messages && thread.messages.length);
        if (hadLocal) {
          for (const m of thread.messages) bridge.chatRenderMessage(m);
          bridge.hooks.setLogLoading?.(false);
        }
        hydrateOk = await chatHydrateFromServer(convId, thread);
        // If a newer switch has started, this (stale) switch must not write to the log —
        // otherwise A's messages land in B's log (state/DOM desync).
        if (myGen !== _switchGen) return;
        if (hydrateOk && thread) {
          // Rebuild with server data (replace the cache render with fresh/merged data).
          if (hadLocal) bridge.hooks.log.innerHTML = "";
          for (const m of thread.messages) bridge.chatRenderMessage(m);
        } else if (!hadLocal) {
          // Hydrate FAILED + NO cache → show error. If cache EXISTS, keep it
          // (transient server slowness/error must not wipe what's already displayed).
          bridge.hooks.log.innerHTML = "";
          if (thread) {
            thread.messages = [];
            thread.conversationId = null;
          }
          setConversationId(null);
          // A2: setConversationId(null) left the transport foreground gate null, so a lone
          // BACKGROUND stream would be treated as foreground (isForegroundStream's
          // "_streamsByConv.size <= 1 → true" fallback) and its `done` would rebind the
          // active thread to the wrong conversation. Park the gate on the sentinel (as the
          // new-chat path does) so no background stream is mistaken for the foreground.
          try { bridge.setForegroundConversation?.(EMPTY_THREAD_FOREGROUND); } catch { /* ignore */ }
          bridge.hooks.appendSystemNotice(window.AkanaI18n.t("thread.notice.load_failed"));
        }
      } finally {
        // Only the MOST RECENT switch should finalize the log (a stale switch
        // must not corrupt the new switch's hydrate/persist state).
        if (myGen === _switchGen) {
          finishLogHydrate();
          setChatPersistPaused(false);
        }
        flushChatStore();
      }
      if (myGen !== _switchGen) return;
      // CONCURRENT N-STREAMS: sync the composer (SEND↔STOP / "Responding") to the
      // DISPLAYED conv's stream state. Switching from A to B while A is streaming
      // must NOT leave B's composer stuck on STOP because of A's (global) stream state;
      // if B is streaming, STOP is shown.
      try {
        bridge.syncComposerForDisplayed?.(convId);
      } catch {
        /* ignore */
      }
      // CONCURRENT N-STREAMS: if this conv's IN-MEMORY live stream (continuing in the
      // background, row detached from DOM when log was cleared on switch) exists,
      // reattach it to the fresh log → A's accumulated live text + tool cards appear
      // INSTANTLY and the stream continues live (no new follower → no double-render).
      let reattached = false;
      try {
        reattached = Boolean(bridge.reattachLiveRow?.(convId));
      } catch {
        /* ignore */
      }
      // If reattach SUCCEEDED, the turn is already streaming live in memory —
      // SKIP resume/refresh (resume doesn't create a double-follower but is pointless;
      // refresh could paint a stale snapshot in front of the live row). Otherwise
      // (no in-memory stream: F5 / turn on another device) fall back: server resume;
      // if none, refresh the log.
      if (reattached) {
        void window.AkanaChat?.refreshQueueState?.(convId);
        void archive.refreshConvActivityFromServer?.(convId);
        void archive.loadChatArchiveList();
        void archive.refreshActiveConversationMeta();
        return;
      }
      // Returning from another page: if a turn is running, resume it live; otherwise
      // if the turn finished in the meantime, refresh the log from the server
      // (prevents stale/incomplete DOM).
      const resumed = await resumeActiveTurnIfAny(convId);
      if (myGen !== _switchGen) return;
      // If hydrate SUCCEEDED, the log is already fresh and rendered; resume handled
      // the active turn → only run refreshConversationLogAfterTurn on the RECOVERY
      // (hydrate FAILED) path. Previously it ran on every switch, fetching turns a
      // SECOND time and rebuilding the log a SECOND time (unnecessary 2-3 round-trips
      // + flicker). Late-finishing turns are caught via WS turn_completed +
      // reattachLiveRow anyway (no loss).
      if (!resumed && !hydrateOk) await refreshConversationLogAfterTurn(convId);
      void window.AkanaChat?.refreshQueueState?.(convId);
      void archive.refreshConvActivityFromServer?.(convId);
      void archive.loadChatArchiveList();
      void archive.refreshActiveConversationMeta();
    }

    /** Resume a running turn in the opened conversation if one exists — defensive, swallows errors. */
    async function resumeActiveTurnIfAny(convId) {
      if (!convId || typeof bridge.resumeActiveTurn !== "function") return false;
      try {
        return await bridge.resumeActiveTurn(convId);
      } catch {
        return false;
      }
    }

    async function chatRestoreActiveThread() {
      setChatPersistPaused(true);
      // PARALLEL-CHAT: after F5/restore, FIRST show the active chat's pane
      // (keyed to the correct conv-id) → beginLogHydrate only wraps that pane and
      // subsequent chat switches can find it. (Restore starts with a single 'default' pane.)
      try {
        const _rp = chatProfile();
        ensureChatThread(_rp);
        const _rt = chatStore().threads[chatStore().activeByProfile[_rp]];
        if (_rt && _rt.conversationId) bridge.showConversation?.(_rt.conversationId);
      } catch { /* ignore */ }
      // PROVISIONAL PAINT (mobile/remote latency win): WITHOUT waiting for the server
      // fetch, paint the localStorage snapshot INSTANTLY → the last chat appears
      // immediately instead of a blank screen/spinner. This is PIXELS ONLY; when hydrate
      // returns, thread.messages is updated with the authoritative server data and fully
      // re-rendered below (never merged → the stale snapshot does NOT become authoritative).
      // If no snapshot, fall back to the spinner as before.
      const profile = chatProfile();
      ensureChatThread(profile);
      const thread = chatStore().threads[chatStore().activeByProfile[profile]];
      const provisional = Array.isArray(thread.messages) ? thread.messages.slice() : [];
      if (provisional.length) {
        bridge.hooks.log.innerHTML = "";
        for (const m of provisional) bridge.chatRenderMessage(m);
        scrollLogToEnd();
      } else {
        beginLogHydrate();
      }
      try {
        if (thread.conversationId) {
          conversationId = thread.conversationId;
          sessionStorage.setItem("akana.conversationId", thread.conversationId);
          const ok = await chatHydrateFromServer(thread.conversationId, thread);
          if (!ok) thread.conversationId = null;
        } else {
          const legacy = sessionStorage.getItem("akana.conversationId");
          if (legacy) {
            thread.conversationId = legacy;
            const ok = await chatHydrateFromServer(legacy, thread);
            if (!ok) {
              thread.conversationId = null;
              sessionStorage.removeItem("akana.conversationId");
              conversationId = null;
            }
          }
        }
        // AUTHORITATIVE RE-PAINT: hydrate updated thread.messages with the server
        // authoritative data → replace the provisional render entirely (no double render).
        bridge.hooks.log.innerHTML = "";
        for (const m of thread.messages) bridge.chatRenderMessage(m);
        if (thread.conversationId) setConversationId(thread.conversationId);
        else setConversationId(null);
      } finally {
        finishLogHydrate();
        setChatPersistPaused(false);
        flushChatStore();
      }
      // After returning to the page / F5: if a turn is running, retrieve the accumulated response.
      const restoredConvId = chatActiveThread()?.conversationId;
      if (restoredConvId) {
        const resumed = await resumeActiveTurnIfAny(restoredConvId);
        if (!resumed) await refreshConversationLogAfterTurn(restoredConvId);
      }
      void window.AkanaChat?.refreshQueueState?.(restoredConvId);
      if (restoredConvId) void archive.refreshConvActivityFromServer?.(restoredConvId);
      void archive.refreshActiveConversationMeta();
      archive.syncChatThreadBar();
      void archive.loadChatArchiveList();
    }

    async function chatStartNewThread(opts = {}) {
      const { force = false, localOnly = false } = opts;
      // STALE any in-flight conversation switch (switchChatConversation): without this,
      // when that switch's hydrate await returns (myGen===_switchGen passes), the OLD
      // conv's messages would be injected into this NEW thread's log and conv_id would
      // be bound incorrectly (user report: "old messages in new chat + next message goes to wrong conv").
      _switchGen += 1;
      // CONNECTION-LIMIT FIX: capture the leaving (displayed) chat BEFORE switching to
      // the new empty chat → release its stream AFTER returning to the sentinel
      // (symmetric with switchChatConversation; held POST must not exhaust the pool).
      // If "" (already on an empty surface) the guard below skips.
      const _leavingConvId = conversationIdForMemory();
      bridge.clearPendingAttachments?.(); // EC2: don't carry old attachments into the new chat
      // CONCURRENT N-STREAMS: "+/new chat" does NOT abort the previous chat's LIVE STREAM.
      // Previously bridge.abortStream() was here → opening a new chat while A was streaming
      // killed A's stream (user: "type in A, open new chat, both should stream" was broken).
      // Now only flip the foreground gate to the NEW (empty) chat — A keeps streaming in the
      // background, and on return reattachLiveRow makes it visible live again.
      // (The DELETE path for the active chat ALREADY aborts the stream itself:
      // deleteConversationById/applyChatServerAction → bridge.abortStream(); this change
      // does not affect those paths.)
      try {
        // Flip foreground to the empty-thread sentinel (NOT null → closes the background
        // stream leak; see EMPTY_THREAD_FOREGROUND). The server-create path will
        // replace it with the real id via setConversationId(serverConvId) shortly.
        bridge.setForegroundConversation?.(EMPTY_THREAD_FOREGROUND);
        // Sync the composer to the displayed (empty, non-streaming) chat → button = SEND.
        // Previously new-thread did NOT reset chatInFlight → opening a new chat while A
        // was streaming left the button stuck on A's stale STOP, and the user couldn't
        // send to the new chat normally (missing counterpart of switchChatConversation's
        // syncComposerForDisplayed call on the new-thread path).
        bridge.syncComposerForDisplayed?.(EMPTY_THREAD_FOREGROUND);
        // PARALLEL-CHAT PANES: show a FRESH pane for the NEW (empty) chat. The
        // currently displayed chat's pane (e.g. A) is HIDDEN but PRESERVED — A
        // keeps streaming in the background. The innerHTML="" below (hooks.log →
        // displayed pane) now only targets this new empty pane, NOT A's pane →
        // prevents the regression "opening a new chat deletes the old chat/stream".
        bridge.showConversation?.(null);
      } catch {
        /* ignore */
      }
      // === CONNECTION-LIMIT FIX (symmetric with switchChatConversation) ===
      // New empty chat opened + foreground returned to sentinel → RELEASE the leaving
      // chat's POST stream. abortStream only cuts the client fetch; the turn continues
      // DETACHED on the server; when finished, WS turn_completed → "response ready"
      // toast; returning to the chat, hydrate/resume retrieves it in FULL. This way
      // only the DISPLAYED conv holds a POST at any time → HTTP/1.1 ~6-connection pool
      // never exhausted, N parallel chats work. Called AFTER
      // setForegroundConversation(sentinel) → the leaving conv is not foreground →
      // abortStream does not reset its composer/STOP state.
      // PARALLEL-CHAT FIX (#1) — symmetric: also do NOT abort the leaving stream when
      // opening a new chat (same rationale as switchChatConversation; background stream
      // is preserved in its hidden pane).
      void _leavingConvId; // background stream preserved (no abort)
      if (!force && canReuseCurrentEmptyThread()) {
        bridge.hooks.log.innerHTML = "";
        const thread = chatActiveThread();
        if (thread) thread.messages = [];
        saveChatStore();
        bridge.hooks.updateEmptyState();
        archive.syncChatThreadBar();
        return { reused: true };
      }
      // ── SYNCHRONOUS ACTIVATION (ROOT FIX — "4th chat" bug) ──────────────────────
      // Create the new empty thread IMMEDIATELY (synchronously) + make it ACTIVE
      // (conversationId=null). This makes conversationIdForMemory() switch to this
      // empty chat INSTANTLY. Previously activation happened inside an async IIFE —
      // AFTER the server POST — so the pane (displayedConvId) was updated synchronously
      // but the active thread was async → DESYNC. Under server load (3+ concurrent
      // streams), if the user typed in the new chat right away, conversationIdForMemory()
      // STILL pointed to the old STREAMING chat →
      // ensureConversationIdReady's `if (existing) return existing` sent the message to
      // that streaming chat → backend 409 TURN_BUSY (user: "4th chat doesn't work /
      // wait for the response to finish"). [JSEND] diagnostic: dispConv="(empty)" but
      // fromMem=streaming-conv.
      // Conv id is now created LAZILY on first send via ensureConversationIdReady
      // (eager POST removed → no double-create race + no orphan empty conv; the empty
      // chat doesn't open a server conv unnecessarily, it enters the list on first message).
      const _profile = chatProfile();
      const _tid = createActiveThread(_profile, null);
      // Also null out the global conversationId — conversationIdForMemory() on an
      // unbound active thread falls back to `chatActiveThread()?.conversationId || conversationId`
      // and would hit the global; if not nulled it would fall to the old streaming conv
      // (the other half of the bug).
      setConversationId(null);
      // setConversationId(null) sets foreground to null → restore it to the empty-new-chat
      // sentinel (close the background stream leak — see EMPTY_THREAD_FOREGROUND).
      try { bridge.setForegroundConversation?.(EMPTY_THREAD_FOREGROUND); } catch { /* ignore */ }
      bridge.hooks.log.innerHTML = "";
      bridge.hooks.updateEmptyState();
      archive.setActiveConversationMeta(null);
      archive.syncChatThreadBar();

      // ── BACKGROUND EAGER-CREATE (sidebar list fix) ───────────────────────────
      // Eager-create a server conv so the new empty chat appears in the SIDEBAR.
      // BUT activation was already done synchronously above (conversationId=null) →
      // the "4th chat goes to streaming chat" desync cannot return. Send awaits this
      // promise (transport ensureConversationIdReady → chatCtx.pendingNewThread) →
      // a SECOND conv POST is never made (no double-create + no orphan conv).
      // Skipped in localOnly mode.
      if (!localOnly) {
        const _np = (async () => {
          let serverConvId = null;
          try {
            // audit H2: POST with EMPTY body (do NOT send a title). When a title is sent,
            // the server sets title_source="user"/"manual" → the automatic first-message
            // title NEVER fires, sidebar stays "New chat" forever. Empty body →
            // title_source="auto" → server derives the title from the first message.
            const r = await fetch(`${baseUrl()}/api/v1/conversations`, {
              method: "POST",
              headers: authHeaders(true),
              body: JSON.stringify({}),
            });
            if (r.ok) {
              const meta = await r.json();
              serverConvId = meta.id || null;
            }
          } catch {
            /* offline — conv will be created on first send */
          }
          if (serverConvId) {
            const t = chatStore().threads[_tid];
            // Thread still exists + unbound? (do NOT overwrite if first message already created a lazy conv).
            if (t && !t.conversationId) {
              // ROOT FIX (audit BUG-1): determine "still active + unbound?" BEFORE assigning
              // t.conversationId. Previously the check `!conversationIdForMemory()` was done
              // AFTER the assignment — but conversationIdForMemory() reads this thread, so
              // it now returned serverConvId and the condition was ALWAYS false →
              // setConversationId/rekey/highlight was DEAD CODE → on the first send in the
              // new chat, the foreground gate (stuck on EMPTY_THREAD_FOREGROUND Symbol) stayed
              // closed: button never switches to STOP, "Responding" never appears, pane stays "".
              const wasActiveUnbound =
                chatStore().activeByProfile[_profile] === _tid && !conversationIdForMemory();
              t.conversationId = serverConvId;
              saveChatStore();
              if (wasActiveUnbound) {
                setConversationId(serverConvId);
                bridge.rekeyConversation?.(null, serverConvId);
                archive.setActiveConversationMeta({ id: serverConvId, title: NEW_THREAD_TITLE, pinned: false });
                archive.syncChatThreadBar();
              }
            }
            // Add to sidebar locally (instead of a full refresh; fall back to refresh if not possible).
            if (!archive.insertConversationLocally?.({ id: serverConvId, title: NEW_THREAD_TITLE })) {
              void archive.loadChatArchiveList();
            }
          }
          return serverConvId;
        })();
        chatNewThreadInFlight = _np;
        // Release the latch when done — BUT ONLY if it still points to THIS promise
        // (audit H1: in rapid double new-chat, the old promise's finally would null the
        // NEW pending latch, causing ensureConversationIdReady to fall through to a SECOND
        // conv POST → duplicate server convs). Ownership check prevents the spurious null.
        try { _np.finally?.(() => { if (chatNewThreadInFlight === _np) chatNewThreadInFlight = null; }); } catch { /* ignore */ }
      }
      return { reused: false, conversationId: null };
    }

    /** Pending new-chat eager-create promise (transport ensureConversationIdReady
     *  awaits this → send uses the eager conv id, no second conv POST). */
    function getPendingNewThread() {
      return chatNewThreadInFlight;
    }

    /** Sync UI after server NL commands (`action` on chat / stream done). */
    async function applyChatServerAction(action, payload, opts = {}) {
      if (!action || !payload) return;
      _switchGen += 1; // stale any in-flight conversation switch (don't clobber the thread setup)
      const newId = payload.conversation_id;
      const oldId = opts.priorConversationId || conversationIdForMemory();
      if (action === "conversation_new" && newId) {
        if (oldId && oldId !== newId) {
          purgeConversationFromChatStore(oldId);
          bridge.removeConversation?.(oldId); // remove old conv pane from DOM
        }
        createActiveThread(chatProfile(), newId);
        setConversationId(newId);
        bridge.showConversation?.(newId); // PARALLEL-CHAT: show new conv pane (wipe only targets it)
        bridge.hooks.log.innerHTML = "";
        bridge.hooks.updateEmptyState();
        archive.setActiveConversationMeta({ id: newId, title: NEW_THREAD_TITLE, pinned: false });
        archive.syncChatThreadBar();
        bridge.hooks.showToast(window.AkanaI18n.t("thread.toast.new_chat_started"));
        return;
      }
      if (action === "conversation_delete") {
        bridge.abortStream(oldId); // audit M2: hedefli kes (oldId arka-plansa bile leak yok)
        if (oldId) {
          purgeConversationFromChatStore(oldId);
          bridge.removeConversation?.(oldId); // remove deleted conv pane from DOM
        }
        archive.setChatArchiveItems(archive.getChatArchiveItems().filter((c) => c.id !== oldId));
        if (newId) {
          createActiveThread(chatProfile(), newId);
          setConversationId(newId);
          bridge.showConversation?.(newId); // PARALLEL-CHAT: show new conv pane
          bridge.hooks.log.innerHTML = "";
          bridge.hooks.updateEmptyState();
          archive.setActiveConversationMeta({ id: newId, title: NEW_THREAD_TITLE, pinned: false });
          archive.syncChatThreadBar();
          // Show hero — do NOT add a "deleted — new session" row to the log
          // (adding one would hide the hero); notify the user via toast.
        } else {
          await chatStartNewThread({ force: true });
        }
        bridge.hooks.showToast(window.AkanaI18n.t("thread.toast.chat_deleted"));
        void archive.loadChatArchiveList();
        return;
      }
      const notice = CHAT_ACTION_NOTICES[action];
      if (notice) bridge.hooks.showToast(typeof notice.msg === "function" ? notice.msg() : notice.msg, notice.kind);
    }

    archive = window.AkanaChatArchive.createArchive({
      bridge,
      conversationIdForMemory,
      chatActiveThread,
      setConversationId,
      switchChatConversation,
      deleteConversationById,
      archiveConversationById,
      chatStartNewThread,
    });

    return {
      chatProfile,
      newChatThreadId,
      chatActiveThread,
      ensureChatThread,
      chatRecordMessage,
      recordErrorForConversation,
      recordPendingUserMessage,
      reloadConversationLogFromServer,
      refreshConversationLogAfterTurn,
      syncConversationLogFromServer,
      syncThreadConversationId,
      activateThreadForConversation,
      isConversationDisplayed,
      chatHydrateFromServer,
      patchConversationApi: (...a) => archive.patchConversationApi(...a),
      deleteConversationApi: (...a) => archive.deleteConversationApi(...a),
      purgeConversationFromChatStore,
      conversationDeleteLabel,
      deleteConversationById,
      archiveConversationById,
      tryHandleChatDeleteCommand,
      loadChatArchiveList: () => archive.loadChatArchiveList(),
      refreshArchiveActivity: (id) => archive.refreshConvActivityFromServer?.(id),
      refreshActiveConversationMeta: () => archive.refreshActiveConversationMeta(),
      syncChatThreadBar: () => archive.syncChatThreadBar(),
      exportConversationMarkdown: (...a) => archive.exportConversationMarkdown(...a),
      openArchiveDrawer: () => archive.openArchiveDrawer(),
      closeArchiveDrawer: () => archive.closeArchiveDrawer(),
      switchChatConversation,
      chatRestoreActiveThread,
      chatStartNewThread,
      getPendingNewThread,
      conversationIdForMemory,
      resolveConversationId,
      isEmptyChatSurface,
      canReuseCurrentEmptyThread,
      setConversationId,
      applyChatServerAction,
      getChatStore,
      getChatArchiveItems: () => archive.getChatArchiveItems(),
      setChatArchiveItems: (v) => archive.setChatArchiveItems(v),
      getActiveConversationMeta: () => archive.getActiveConversationMeta(),
      setActiveConversationMeta: (v) => archive.setActiveConversationMeta(v),
      getChatPersistPaused,
      setChatPersistPaused,
      wireArchiveChrome: () => archive.wireArchiveChrome(),
      wireThreadBar: () => archive.wireThreadBar(),
    };
  }

  window.AkanaChatThreads = { create };
})();
