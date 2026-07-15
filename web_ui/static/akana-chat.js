/**
 * Akana chat — threads, archive, SSE stream, conversation APIs (loaded before app.js).
 */
(() => {
  let hooks = {
    log: null,
    logScroll: null,
    form: null,
    msg: null,
    sendBtn: null,
    appendRow: () => {},
    appendUserMessage: () => {},
    appendSystemNotice: () => {},
    updateEmptyState: () => {},
    resizeComposer: () => {},
    setOrb: () => {},
    setComposerHint: () => {},
    stickToBottomIfFollowing: () => {},
    scrollLogToBottom: () => {},
    scrollNewTurnToTop: () => {},
    setLogLoading: () => {},
    logEmpty: null,
    showToast: () => {},
    streamTtsParam: () => "",
    ttsPlayer: null,
    syncOrbWithVoice: () => {},
    cancelVoiceActivity: () => window.AkanaVoice?.cancelVoiceActivity?.() ?? false,
    updateSettingsHero: () => {},
    loadMemoryConversations: () => {},
    shortConversationId: (id) => id || "none",
    closeSettings: () => {},
    isChatPage: false,
    // Called when a stream starts/ends — drives the send↔stop button mode (triggered
    // from every stream path including resume). init() binds this to setSendButtonMode.
    setStreamingUi: () => {},
  };

  let chatInFlight = false;
  let queueDepth = 0;
  let stopThenSendRequested = false;
  let _chatPageWired = false;
  // b27: conversations whose submit is in its synchronous SETUP window (before the stream
  // registers, so isConversationStreamActive is not yet true). PER-CONVERSATION (chatInFlight
  // is global → would wrongly block parallel chats), so a rapid double-submit to the SAME
  // existing conversation is caught during the upload/provider await window and does not run twice.
  const _submitSetupConvs = new Set();

  // ── Reasoning-effort control (provider-aware, two vocabularies) ─────────────
  // The composer's effort menu speaks ONE of two vocabularies, chosen by the active
  // provider; each keeps its OWN persisted selection so switching providers never sends
  // a level the target can't use:
  //   • "akana"  — canonical tiers (hizli/normal/derin/yogun/azami/ultra). claude & gemini
  //     map these to their native knob server-side. "hizli" forces the fast-path; "ultra"
  //     is a 6th, claude-only tier (appends the "ultracode" keyword on fable models).
  //   • "native" — the provider's OWN reasoning levels (minimal/low/medium/high/xhigh).
  //     codex & openai show these directly and send the chosen level VERBATIM (no mapping),
  //     so the user sees and selects the REAL effort level. "xhigh" (extra-high) is the
  //     native-only top level — no Akana tier reaches it.
  // Providers without a reasoning knob (cursor/ollama) hide the whole #effort-menu.
  const EFFORT_VOCABS = {
    akana: {
      storageKey: "akana:thinking-mode",
      def: "normal",
      modes: ["hizli", "normal", "derin", "yogun", "azami", "ultra"],
    },
    native: {
      storageKey: "akana:thinking-mode-native",
      def: "medium",
      modes: ["minimal", "low", "medium", "high", "xhigh"],
    },
  };
  const _EFFORT_LABEL_KEYS = {
    hizli: "chat.effort_fast", normal: "chat.effort_normal", derin: "chat.effort_deep",
    yogun: "chat.effort_intense", azami: "chat.effort_max", ultra: "chat.effort_ultra",
    minimal: "chat.effort_minimal", low: "chat.effort_low", medium: "chat.effort_medium",
    high: "chat.effort_high", xhigh: "chat.effort_xhigh",
  };
  // Resolved LIVE (not cached at load) so a language flip relabels the menu.
  const effortLabel = (mode) =>
    window.AkanaI18n.t(_EFFORT_LABEL_KEYS[mode] || "chat.effort_normal");

  let thinkingProvider = "";

  // provider → vocabulary key (null = provider has no reasoning knob → menu hidden).
  function vocabKeyForProvider(p) {
    if (p === "claude" || p === "gemini") return "akana";
    if (p === "codex" || p === "openai") return "native";
    return null;
  }
  function currentVocabKey() {
    return vocabKeyForProvider(thinkingProvider);
  }
  function thinkingEnabled() {
    return currentVocabKey() != null;
  }
  function thinkingLocked() {
    return !thinkingEnabled();
  }

  // Per-vocabulary persisted selection, loaded lazily + validated against the mode list
  // (a stale/invalid stored value falls back to the vocab default).
  const _effortSelected = {};
  function selectedFor(vk) {
    if (_effortSelected[vk] != null) return _effortSelected[vk];
    const v = EFFORT_VOCABS[vk];
    let stored = null;
    try {
      stored = localStorage.getItem(v.storageKey);
    } catch {
      /* storage unavailable — in-memory selection still valid */
    }
    _effortSelected[vk] = v.modes.includes(stored) ? stored : v.def;
    return _effortSelected[vk];
  }

  // "ultra" (6th akana tier) is claude-only. On gemini it is hidden and a persisted "ultra"
  // collapses to "azami" so the composer never shows/sends it on a non-claude provider.
  function ultraVisible() {
    return thinkingProvider === "claude";
  }
  function effectiveMode(vk) {
    const m = selectedFor(vk);
    if (vk === "akana" && m === "ultra" && !ultraVisible()) return "azami";
    return m;
  }

  // The value SENT for the active provider — its vocabulary's current selection. When no
  // provider knob is active, "normal" (a harmless no-op for cursor/ollama).
  function currentEffortMode() {
    const vk = currentVocabKey();
    return vk ? effectiveMode(vk) : "normal";
  }

  // (Re)build the popover option buttons for the active vocabulary. "ultra" is filtered
  // out for non-claude providers. Options are rendered in JS (not fixed HTML) precisely
  // because the option SET now depends on the provider.
  function renderEffortOptions() {
    const group = document.getElementById("thinking-mode");
    const vk = currentVocabKey();
    if (!group || !vk) return;
    const modes = EFFORT_VOCABS[vk].modes.filter((m) => m !== "ultra" || ultraVisible());
    group.textContent = "";
    for (const m of modes) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "effort-opt";
      b.dataset.mode = m;
      b.setAttribute("role", "option");
      b.textContent = effortLabel(m);
      group.appendChild(b);
    }
  }

  function syncThinkingModeUi() {
    const menu = document.getElementById("effort-menu");
    const group = document.getElementById("thinking-mode");
    const trigger = document.getElementById("btn-effort");
    const label = document.getElementById("effort-btn-label");
    if (!group) return;
    const enabled = thinkingEnabled();
    if (menu) menu.hidden = !enabled;
    if (!enabled) {
      // Provider has no thinking knob → the whole #effort-menu is hidden above;
      // just make sure an open popover is dismissed.
      closeEffortMenu();
      return;
    }
    renderEffortOptions();
    const mode = currentEffortMode();
    if (trigger) {
      trigger.title = window.AkanaI18n.t("chat.effort_open_title");
      trigger.setAttribute("aria-label", window.AkanaI18n.t("chat.effort_aria", { label: effortLabel(mode) }));
      trigger.setAttribute("data-mode", mode);
    }
    if (label) label.textContent = effortLabel(mode);
    group.querySelectorAll(".effort-opt").forEach((b) => {
      const on = b.dataset.mode === mode;
      b.classList.toggle("is-active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
  }

  function closeEffortMenu() {
    const group = document.getElementById("thinking-mode");
    const trigger = document.getElementById("btn-effort");
    if (!group || group.hidden) return;
    group.hidden = true;
    if (trigger) trigger.setAttribute("aria-expanded", "false");
  }

  function toggleEffortMenu() {
    if (thinkingLocked()) return;
    const group = document.getElementById("thinking-mode");
    const trigger = document.getElementById("btn-effort");
    if (!group || !trigger) return;
    const open = group.hidden;
    group.hidden = !open;
    trigger.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      const active = group.querySelector(".effort-opt.is-active");
      active?.focus?.();
    }
  }

  function setThinkingMode(mode, { closeMenu = true } = {}) {
    const vk = currentVocabKey();
    if (!vk || !EFFORT_VOCABS[vk].modes.includes(mode)) return;
    if (mode === selectedFor(vk)) return;
    _effortSelected[vk] = mode;
    try {
      localStorage.setItem(EFFORT_VOCABS[vk].storageKey, mode);
    } catch {
      /* storage unavailable — in-memory selection still valid */
    }
    syncThinkingModeUi();
    if (closeMenu) closeEffortMenu();
  }

  // Called when the provider changes (settings → loadModelPill); switches the vocabulary
  // (and re-renders the menu options). Each vocabulary keeps its own selection, so no
  // cross-provider "mode rides along" fixup is needed here.
  function setThinkingProvider(provider) {
    const p = String(provider || "").trim().toLowerCase();
    const next =
      p === "claude"
        ? "claude"
        : p === "gemini"
          ? "gemini"
          : p === "openai"
            ? "openai"
            : p === "codex"
              ? "codex"
              : p === "cursor"
                ? "cursor"
                : p === "ollama"
                  ? "ollama"
                  : "";
    if (next === thinkingProvider) return;
    thinkingProvider = next;
    syncThinkingModeUi();
  }

  // One-shot on first paint (before settings panel opens) to set correct lock state.
  async function initThinkingProvider() {
    try {
      const p = await activeProviderName();
      if (p) setThinkingProvider(p);
    } catch {
      /* provider unknown — control stays open (safe default) */
    }
  }

  function wireThinkingMode() {
    const group = document.getElementById("thinking-mode");
    const trigger = document.getElementById("btn-effort");
    if (!group || !trigger) return;
    trigger.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleEffortMenu();
    });
    group.addEventListener("click", (e) => {
      if (thinkingLocked()) return;
      const btn = e.target.closest(".effort-opt");
      if (btn && btn.dataset.mode) setThinkingMode(btn.dataset.mode);
    });
    document.addEventListener("click", (e) => {
      if (e.target.closest("#effort-menu")) return;
      closeEffortMenu();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeEffortMenu();
    });
    syncThinkingModeUi();
    void initThinkingProvider();
  }

  function syncSendButtonMode() {
    if (chatInFlight || queueDepth > 0) setSendButtonMode("stop");
    else setSendButtonMode("send");
  }

  /**
   * CONCURRENT N-STREAMS: composer (SEND↔STOP / "Thinking") must reflect the
   * DISPLAYED conversation's stream state. Called on conversation switch: chatInFlight
   * is no longer a GLOBAL "is any stream active" flag but "is the displayed conv streaming".
   * While A streams, switching to B must not leave B's composer stuck at Stop
   * (because of A's stream); returning to A while A streams must show Stop correctly.
   */
  function syncComposerForDisplayedConversation(convId) {
    let active = false;
    try {
      active = Boolean(ensureTransport().isConversationStreamActive?.(convId));
    } catch {
      active = false;
    }
    chatInFlight = active;
    syncSendButtonMode();
    try {
      hooks.setComposerHint?.(active ? "thinking" : "idle");
    } catch {
      /* ignore */
    }
    // AkanaTurnStatus ("Thinking" strip) closes via finalizeStreamUi, gated on foreground plan;
    // skipped when a background stream ends. On conversation switch chatInFlight is reset,
    // but if the strip persists it stays stuck on "Thinking" forever in the visible (idle) chat.
    try {
      if (active) {
        // resume() (NOT begin()) — switching BACK to a conversation whose turn is still
        // running must preserve the true elapsed/phase; begin() would restart it at 0:00.
        // Pass the displayed conv id so resume() refuses to attribute a concurrent turn's
        // clock/phase to this conversation (falls back to a fresh clock on a mismatch).
        if (!window.AkanaTurnStatus?.isActive?.()) window.AkanaTurnStatus?.resume?.(convId);
      } else {
        window.AkanaTurnStatus?.end?.();
      }
    } catch {
      /* ignore */
    }
  }

  function renderQueueChip() {
    let host = document.getElementById("composer-queue");
    if (!host && hooks.form) {
      host = document.createElement("div");
      host.id = "composer-queue";
      host.className = "composer-queue";
      host.setAttribute("role", "status");
      const attachments = document.getElementById("composer-attachments");
      if (attachments?.parentNode) attachments.parentNode.insertBefore(host, attachments);
      else hooks.form.insertBefore(host, hooks.form.firstChild);
    }
    if (!host) return;
    if (queueDepth <= 0) {
      host.hidden = true;
      host.innerHTML = "";
      return;
    }
    host.hidden = false;
    host.innerHTML = "";
    const chip = document.createElement("span");
    chip.className = "composer-queue-chip";
    chip.textContent =
      queueDepth === 1
        ? window.AkanaI18n.t("chat.queue_one")
        : window.AkanaI18n.t("chat.queue_many", { n: queueDepth });
    host.appendChild(chip);
  }

  function setQueueDepth(n) {
    queueDepth = Math.max(0, Number(n) || 0);
    renderQueueChip();
    syncSendButtonMode();
  }

  async function refreshQueueState(convId) {
    const id = (convId || conversationIdForMemory() || "").trim();
    if (!id) {
      setQueueDepth(0);
      return;
    }
    const base = window.AkanaSettings?.baseUrl?.() || "";
    const headers = window.AkanaSettings?.authHeaders?.() || {};
    try {
      const r = await fetch(`${base}/api/v1/chat/queue/${encodeURIComponent(id)}`, { headers });
      if (r.ok) {
        const body = await r.json();
        // The queue chip is a single global element showing the DISPLAYED conversation's
        // queue. This fetch is fire-and-forget; if the user switched to another conversation
        // during the await, `id` is no longer active — don't write a stale count
        // (the active conversation updates itself via its own refresh).
        // Otherwise A's queue count would appear in B's composer.
        if (id !== (conversationIdForMemory() || "").trim()) return;
        setQueueDepth(body.depth || 0);
      }
    } catch {
      /* ignore */
    }
  }

  // Conversations with a LIVE background turn (schedule fire / task) running on the
  // server. Populated by turn_active, cleared by turn_completed. Lets a thread opened
  // AFTER the turn started still show the "working…" strip (open-late case).
  const bgActiveTurns = new Set();

  /** Show the composer "working…" strip for the displayed conversation if a
   *  background turn is live on it and the user isn't already streaming a turn. */
  function maybeShowBgWorking(convId) {
    const id = (convId || "").trim();
    if (!id || !bgActiveTurns.has(id)) return;
    if (chatInFlight) return; // a foreground turn already owns the strip
    if (conversationIdForMemory() !== id) return;
    try {
      window.AkanaTurnStatus?.begin?.(id);
      window.AkanaTurnStatus?.setPhase?.("preparing");
    } catch {
      /* ignore */
    }
  }

  // A background turn STARTED in the conversation currently on screen → working strip.
  async function onTurnActiveRemote(convId, evt) {
    void evt;
    const id = (convId || "").trim();
    if (!id) return;
    bgActiveTurns.add(id);
    maybeShowBgWorking(id);
  }

  // A background turn started in a NON-displayed conversation → make its (possibly
  // brand-new) thread appear in the sidebar so the user can open it while it works.
  async function onBackgroundTurnActive(convId, evt) {
    void evt;
    const id = (convId || "").trim();
    if (!id) return;
    bgActiveTurns.add(id);
    void loadChatArchiveList();
  }

  async function onTurnCompletedRemote(convId, evt) {
    // A live background turn on this conversation just ended → drop the working strip.
    const _cid = (convId || "").trim();
    if (_cid && bgActiveTurns.delete(_cid) && !chatInFlight) {
      try {
        window.AkanaTurnStatus?.end?.();
      } catch {
        /* ignore */
      }
    }
    return onTurnCompletedRemoteInner(convId, evt);
  }

  async function onTurnCompletedRemoteInner(convId, evt) {
    await refreshQueueState(convId);
    if (conversationIdForMemory() !== convId) return;
    // BUG (root of corruption): `getChatInFlight()` is UNDEFINED in this scope — it is
    // only a method on the export object (AkanaChat.getChatInFlight). The local
    // `chatInFlight` variable (let above) is correct. A bare call threw ReferenceError
    // on every WS turn_completed (when a background/detached turn finished →
    // concurrent/multi-chat scenario), silently breaking turn-completion handling
    // (silent because it was "(in promise)").
    if (chatInFlight) {
      // Stream in flight: normally the SSE `done` line closes "Thinking". But if the
      // SSE is stalled (half-open TCP / stuck follower) done never arrives and the
      // indicator would stay forever. Since the server signals turn completion via a
      // separate WS channel, rescue the stalled stream (after grace; if SSE done
      // arrives within grace this is a no-op).
      try {
        const rescued = await ensureTransport().reconcileServerCompletedTurn?.(
          convId,
          evt && evt.assistant_turn_id,
        );
        if (rescued) {
          // A3: forward the completed turn's id (see onTurnCompletedRemote below).
          await ensureThreads().refreshConversationLogAfterTurn?.(
            convId,
            evt && evt.assistant_turn_id,
          );
        } else if (!ensureTransport().isConversationStreamActive?.(convId)) {
          // reconcile no-op (SSE done arrived within grace / no stream) but chatInFlight
          // was true on entry → stale composer + strip. If no live stream, bring it down.
          chatInFlight = false;
          syncSendButtonMode();
          try {
            hooks.setComposerHint?.("idle");
          } catch {
            /* ignore */
          }
          try {
            window.AkanaTurnStatus?.end?.();
          } catch {
            /* ignore */
          }
        }
      } catch {
        /* ignore */
      }
      return;
    }
    try {
      // If the turn was ALREADY live-rendered in the foreground (SSE `done` line
      // finalized it in place), neither resume nor rebuilding the log is needed.
      // Previously this path always called refreshConversationLogAfterTurn → set
      // log.innerHTML to "" + re-render all messages + snap to bottom on every normal
      // completion; the user saw "the whole conversation re-renders and scrolls from top".
      // Instead, silently sync the store with the server (WITHOUT touching the DOM).
      // If a row is missing/partial (stalled SSE / F5 / tab switch) the old rescue
      // path (resume; if not available, rebuild from server) still applies.
      const atid = evt && evt.assistant_turn_id;
      if (atid && ensureTransport().isForegroundTurnFinalized?.(convId, atid)) {
        await ensureThreads().syncConversationLogFromServer?.(convId);
        return;
      }
      const resumed = await ensureTransport().resumeActiveTurn(convId);
      if (!resumed) {
        // A3: pass the completed turn's id so refresh reloads (renders this answer) even
        // when its completion drained the NEXT turn (which is now the active one).
        await ensureThreads().refreshConversationLogAfterTurn?.(convId, atid);
      }
    } catch {
      /* ignore */
    }
  }

  async function onBackgroundTurnCompleted(convId, evt) {
    const id = (convId || "").trim();
    if (!id || conversationIdForMemory() === id) return;
    bgActiveTurns.delete(id); // the background turn finished
    void ensureThreads().refreshArchiveActivity?.(id);
    const items = ensureThreads().getChatArchiveItems?.() || [];
    const meta = items.find((c) => c.id === id);
    const title = (meta && meta.title) || "Chat";
    const status = String((evt && evt.status) || "ok");
    if (status === "ok") {
      hooks.showToast?.(window.AkanaI18n.t("chat.bg_response_ready", { title }), "info");
    }
    void loadChatArchiveList();
  }

  // ─── Composer file attachments — POST /api/v1/uploads → ChatRequest.file_ids ───
  // Files selected via the attach button or drag-and-drop — Phase 2: ANY type
  // (image/pdf/text/...). Sent to the uploads API first; on success a type-icon chip
  // appears above the composer. On send the ids go into `file_ids` via transport.chatPayload
  // (field omitted when empty). When cursor provider is active and a file is selected,
  // a "switch to Claude" warning appears (provider read from system/status).
  let pendingAttachments = []; // {id, name, kind, size, providerNative, previewUrl}
  //: Attachment generation (EC5): cleared by clearPendingAttachments; if an in-flight
  //: upload finishes after the generation changed (conversation switched), it is NOT
  //: added to pending — prevents old conversation's attachment from leaking into the new one.
  let _attachGen = 0;

  /** Chip icon by file kind (📄/🖼/📦). */
  function attachmentIcon(kind) {
    if (kind === "image") return "🖼";
    if (kind === "pdf" || kind === "docx" || kind === "xlsx" || kind === "text") {
      return "📄";
    }
    return "📦";
  }

  function renderAttachmentChips() {
    const host = document.getElementById("composer-attachments");
    if (!host) return;
    host.innerHTML = "";
    for (const att of pendingAttachments) {
      const chip = document.createElement("span");
      chip.className = att.previewUrl ? "composer-chip has-thumb" : "composer-chip";
      chip.setAttribute("role", "listitem");
      let icon;
      if (att.previewUrl) {
        // Image preview — local object URL shown as a small <img>.
        icon = document.createElement("img");
        icon.className = "composer-chip-thumb";
        icon.src = att.previewUrl;
        icon.alt = att.name;
        icon.decoding = "async";
        // EC7: broken/unloadable preview → fall back to type icon (instead of broken-image).
        icon.addEventListener("error", () => {
          if (att.previewUrl) {
            try {
              URL.revokeObjectURL(att.previewUrl);
            } catch {
              /* ignore */
            }
            att.previewUrl = "";
          }
          renderAttachmentChips();
        });
      } else {
        icon = document.createElement("span");
        icon.className = "composer-chip-icon";
        icon.setAttribute("aria-hidden", "true");
        icon.textContent = attachmentIcon(att.kind);
      }
      const name = document.createElement("span");
      name.className = "composer-chip-name";
      name.textContent = att.name;
      name.title = att.name;
      const rm = document.createElement("button");
      rm.type = "button";
      rm.className = "composer-chip-remove";
      rm.setAttribute("aria-label", window.AkanaI18n.t("chat.attach_remove_aria", { name: att.name }));
      rm.title = window.AkanaI18n.t("chat.attach_remove_title");
      rm.textContent = "✕";
      rm.addEventListener("click", () => {
        if (att.previewUrl) {
          try {
            URL.revokeObjectURL(att.previewUrl);
          } catch {}
        }
        pendingAttachments = pendingAttachments.filter((a) => a.id !== att.id);
        renderAttachmentChips();
      });
      chip.appendChild(icon);
      chip.appendChild(name);
      chip.appendChild(rm);
      host.appendChild(chip);
    }
    host.hidden = pendingAttachments.length === 0;
    void maybeWarnAttachments();
  }

  //: Provider image size limits (research 2026-06-13). claude: Anthropic API allows
  //: 10MB *base64* per image (~7.5MB raw); Bedrock/Vertex 5MB.
  //: Conservative 5MB RAW threshold → covers both channels (SOFT warning, NOT a
  //: block; claude-code can resize large images itself). cursor: model-dependent,
  //: no client-side image gate (backend 10MB upload gate applies).
  const PROVIDER_IMAGE_MAX_BYTES = {
    claude: 5 * 1024 * 1024,
    gemini: 20 * 1024 * 1024,
    openai: 20 * 1024 * 1024,
  };

  /** Best-effort resolve of the active provider. Prefers the #llm-provider select
   *  in the DOM (when settings are open); otherwise reads a one-shot from /api/v1/system/status. */
  async function activeProviderName() {
    const sel = document.getElementById("llm-provider");
    if (sel && sel.value) return String(sel.value).trim().toLowerCase();
    try {
      const core = window.AkanaCore;
      const r = await fetch(`${core.baseUrl()}/api/v1/system/status`, {
        headers: core.authHeaders(),
      });
      const j = await r.json().catch(() => ({}));
      const m = j.model || {};
      return String(j.active_provider || m.provider || j.chat_path || "").toLowerCase();
    } catch {
      return "";
    }
  }

  /** Validate attached files against the active provider's CAPABILITY + size limit.
   *
   * Two warnings: (1) if the backend reported ``provider_native=false`` for this provider
   * the file is unreadable — capability single-source-of-truth is the BACKEND; front-end
   * does not guess (warning auto-corrects if parity changes). (2) if an image exceeds
   * the provider's size limit (e.g. claude 5MB). Same attachment set is not re-evaluated
   * (avoids redundant fetches / duplicate toasts). */
  let _attachEvaluatedKey = "";
  async function maybeWarnAttachments() {
    if (!pendingAttachments.length) {
      _attachEvaluatedKey = "";
      return;
    }
    const idsKey = pendingAttachments.map((a) => a.id).join(",");
    if (idsKey === _attachEvaluatedKey) return; // same set — no re-evaluation
    _attachEvaluatedKey = idsKey;
    const provider = await activeProviderName();
    if (!provider) {
      _attachEvaluatedKey = ""; // could not resolve — retry on next render
      return;
    }
    const unreadable = pendingAttachments.filter(
      (a) => a.providerNative && a.providerNative[provider] === false,
    );
    if (unreadable.length) {
      hooks.showToast?.(
        window.AkanaI18n.t("chat.attach_unreadable", { provider, name: unreadable[0].name }),
        "warn",
      );
    }
    const cap = PROVIDER_IMAGE_MAX_BYTES[provider];
    const tooBig = cap
      ? pendingAttachments.filter((a) => a.kind === "image" && a.size > cap)
      : [];
    if (tooBig.length) {
      hooks.showToast?.(
        window.AkanaI18n.t("chat.attach_too_big", { name: tooBig[0].name, provider, mb: Math.round(cap / 1048576) }),
        "warn",
      );
    }
  }

  /** Called at send time: returns the id list and clears the chips. */
  function consumePendingFileIds() {
    if (!pendingAttachments.length) return [];
    const ids = pendingAttachments.map((a) => a.id);
    for (const a of pendingAttachments) {
      if (a.previewUrl) {
        try {
          URL.revokeObjectURL(a.previewUrl);
        } catch {}
      }
    }
    pendingAttachments = [];
    renderAttachmentChips();
    return ids;
  }

  /** Clear pending attachments (EC2: conversation switch / prevents attaching to
   *  wrong conversation on new chat). Also revokes preview object URLs. */
  function clearPendingAttachments() {
    if (!pendingAttachments.length) return;
    for (const a of pendingAttachments) {
      if (a.previewUrl) {
        try {
          URL.revokeObjectURL(a.previewUrl);
        } catch {
          /* ignore */
        }
      }
    }
    pendingAttachments = [];
    _attachGen += 1; // EC5: in-flight uploads must not be added to this (old) conversation
    renderAttachmentChips();
  }

  async function uploadAttachmentFile(file) {
    const core = window.AkanaCore;
    const fd = new FormData();
    fd.append("file", file, file.name);
    const r = await fetch(`${core.baseUrl()}/api/v1/uploads`, {
      method: "POST",
      headers: core.authHeadersMultipart(),
      body: fd,
    });
    const body = await r.json().catch(() => ({}));
    if (r.status === 413) {
      throw new Error(window.AkanaI18n.t("chat.upload_too_large", { name: file.name }));
    }
    if (!r.ok) throw new Error(core.parseApiError(body, r.status));
    const img = body?.image || {};
    if (!img.id) throw new Error(window.AkanaI18n.t("chat.upload_invalid_resp"));
    // provider_native: capability DIRECTLY reported by the backend ({claude, cursor}
    // → bool). The warning reads this; the front-end does NOT guess capability
    // (backend is the single source of truth; front-end auto-follows if parity changes).
    return {
      id: img.id,
      kind: img.kind || "file",
      providerNative: img.provider_native || null,
      mediaType: img.media_type || null,
    };
  }

  //: Provider attachment limits (research 2026-06-13; easy to tune). Our images go
  //: via agent file-read, NOT native attach UI → ceiling is the model-API limit; we
  //: still behave conservatively. msgImages / msgFiles are SEPARATE budgets (image/*
  //: vs other). convFiles = total attachments per conversation (WARNING; since history
  //: is text-only this is mostly precautionary for claude, meaningful for cursor-reuse).
  //: Cursor IDE 1-image limit is too restrictive for file-reads → 4. Backend ceiling
  //: (max_length=30) overrides these values.
  const PROVIDER_ATTACH_LIMITS = {
    claude: { msgImages: 20, msgFiles: 10, convFiles: 100 },
    cursor: { msgImages: 4, msgFiles: 8, convFiles: 30 },
  };
  const DEFAULT_ATTACH_LIMITS = { msgImages: 8, msgFiles: 8, convFiles: 40 };
  function attachLimitsFor(provider) {
    return PROVIDER_ATTACH_LIMITS[provider] || DEFAULT_ATTACH_LIMITS;
  }

  /** Total attachments SENT so far in the active conversation (sum of fileIds across
   *  user messages in the thread). Error → 0 (warning is precautionary). */
  function conversationFileCount() {
    try {
      const msgs = chatActiveThread?.()?.messages || [];
      let n = 0;
      for (const m of msgs) {
        if (m && m.kind === "user" && Array.isArray(m.fileIds)) n += m.fileIds.length;
      }
      return n;
    } catch {
      return 0;
    }
  }

  /** Do the pending attachments exceed the active provider's per-message limit?
   *  If yes, shows toasts + returns true; send is blocked (EC6: provider-switch drift —
   *  limit is enforced not only at attachment time but also at send time). */
  async function pendingExceedsMsgLimits() {
    if (!pendingAttachments.length) return false;
    const provider = (await activeProviderName()) || "";
    const lim = attachLimitsFor(provider);
    const label = provider || "provider";
    const imgs = pendingAttachments.filter((a) => a.kind === "image").length;
    const files = pendingAttachments.filter((a) => a.kind !== "image").length;
    if (imgs > lim.msgImages) {
      hooks.showToast?.(
        window.AkanaI18n.t("chat.attach_limit_images", { provider: label, max: lim.msgImages, n: imgs }),
        "warn",
      );
      return true;
    }
    if (files > lim.msgFiles) {
      hooks.showToast?.(
        window.AkanaI18n.t("chat.attach_limit_files", { provider: label, max: lim.msgFiles, n: files }),
        "warn",
      );
      return true;
    }
    return false;
  }

  // ── Upload in-flight tracking (EC1): send waits for in-flight uploads so attachments
  //    are not dropped. Counter covers the full handleAttachmentFiles call (gating+upload);
  //    when it reaches 0 the pending sends are released.
  let _attachUploadsInFlight = 0;
  const _attachReadyWaiters = [];
  function attachmentsUploading() {
    return _attachUploadsInFlight > 0;
  }
  function whenAttachmentsReady() {
    if (_attachUploadsInFlight <= 0) return Promise.resolve();
    return new Promise((resolve) => _attachReadyWaiters.push(resolve));
  }
  function _releaseAttachWaiters() {
    _attachUploadsInFlight = Math.max(0, _attachUploadsInFlight - 1);
    if (_attachUploadsInFlight === 0 && _attachReadyWaiters.length) {
      for (const r of _attachReadyWaiters.splice(0)) {
        try {
          r();
        } catch {
          /* ignore */
        }
      }
    }
  }

  /** Update the PDF chip with a page-1 preview IN THE BACKGROUND (EC4: non-blocking;
   *  renderPdfThumb is timeout-guarded). If the chip was removed in the meantime, do not leak the URL. */
  async function attachPdfPreview(att, file) {
    const url = await window.AkanaShell?.renderPdfThumb?.(file);
    if (!url) return;
    if (pendingAttachments.includes(att)) {
      att.previewUrl = url;
      renderAttachmentChips();
    } else {
      try {
        URL.revokeObjectURL(url);
      } catch {
        /* ignore */
      }
    }
  }

  //: Suggestion-1: downscale image before uploading to fit provider size limits —
  //: if the long edge > IMAGE_MAX_EDGE, scale down proportionally via canvas (Claude
  //: already downsizes to ~1568px; also prevents >8000px rejections + most size warnings).
  //: GIFs are skipped (preserve animation); small/undecodable images (e.g. HEIC) pass
  //: through unchanged; original is kept when there is no size gain. EXIF orientation embedded.
  const IMAGE_MAX_EDGE = 1568;
  async function downscaleImageFile(file) {
    const type = file?.type || "";
    if (!/^image\//.test(type) || type === "image/gif") return file;
    let bmp;
    try {
      bmp = await createImageBitmap(file, { imageOrientation: "from-image" });
    } catch {
      return file;
    }
    const longEdge = Math.max(bmp.width, bmp.height);
    if (longEdge <= IMAGE_MAX_EDGE) {
      bmp.close?.();
      return file;
    }
    try {
      const scale = IMAGE_MAX_EDGE / longEdge;
      const w = Math.max(1, Math.round(bmp.width * scale));
      const h = Math.max(1, Math.round(bmp.height * scale));
      const canvas = document.createElement("canvas");
      canvas.width = w;
      canvas.height = h;
      canvas.getContext("2d").drawImage(bmp, 0, 0, w, h);
      bmp.close?.();
      const outType = type === "image/png" || type === "image/webp" ? type : "image/jpeg";
      const quality = outType === "image/png" ? undefined : 0.85;
      const blob = await new Promise((res) => canvas.toBlob(res, outType, quality));
      if (!blob || blob.size >= file.size) return file; // no gain — keep original
      const ext = { "image/png": "png", "image/webp": "webp", "image/jpeg": "jpg" }[outType] || "jpg";
      const base = (file.name || "image").replace(/\.[^.]+$/, "") || "image";
      return new File([blob], `${base}.${ext}`, { type: outType });
    } catch {
      bmp.close?.();
      return file;
    }
  }

  async function handleAttachmentFiles(fileList) {
    const incoming = Array.from(fileList || []);
    if (!incoming.length) return;
    _attachUploadsInFlight += 1;
    const myGen = _attachGen; // EC5: conversation generation for this batch
    const btn = document.getElementById("btn-attach");
    btn?.classList.add("is-busy");
    try {
      const provider = (await activeProviderName()) || "";
      const lim = attachLimitsFor(provider);
      const label = provider || "provider";

      // Per-MESSAGE hard block — image/* and other files have SEPARATE budgets.
      // Pending are classified by server kind; incoming by file.type (counted BEFORE
      // upload so excess files are not uploaded needlessly).
      let curImages = pendingAttachments.filter((a) => a.kind === "image").length;
      let curFiles = pendingAttachments.filter((a) => a.kind !== "image").length;
      const files = [];
      let blockedImg = 0;
      let blockedFile = 0;
      for (const file of incoming) {
        if (/^image\//.test(file.type || "")) {
          if (curImages >= lim.msgImages) {
            blockedImg += 1;
            continue;
          }
          curImages += 1;
        } else {
          if (curFiles >= lim.msgFiles) {
            blockedFile += 1;
            continue;
          }
          curFiles += 1;
        }
        files.push(file);
      }
      if (blockedImg) {
        hooks.showToast?.(
          window.AkanaI18n.t("chat.attach_blocked_images", { provider: label, max: lim.msgImages, n: blockedImg }),
          "warn",
        );
      }
      if (blockedFile) {
        hooks.showToast?.(
          window.AkanaI18n.t("chat.attach_blocked_files", { provider: label, max: lim.msgFiles, n: blockedFile }),
          "warn",
        );
      }
      if (!files.length) return;

      // Per-CONVERSATION total — WARNING (no block): sent so far + this message.
      const projected = conversationFileCount() + curImages + curFiles;
      if (projected > lim.convFiles) {
        hooks.showToast?.(
          window.AkanaI18n.t("chat.attach_conv_limit", { n: projected, max: lim.convFiles, provider: label }),
          "warn",
        );
      }

      for (const file of files) {
        // No type gating — backend determines allowed types (rejections toast).
        try {
          // Suggestion-1: downscale large images BEFORE uploading (long edge > 1568px);
          // leaves non-images/small images/HEIC unchanged, uploads original.
          const sendFile = await downscaleImageFile(file);
          const up = await uploadAttachmentFile(sendFile);
          if (myGen !== _attachGen) break; // EC5: conversation changed — discard this batch
          if (!pendingAttachments.some((a) => a.id === up.id)) {
            // Image: local object URL (immediate). PDF: chip appears FIRST as 📄,
            // page-1 preview is rendered IN THE BACKGROUND (large/broken PDF does not
            // block the chip/send). revokeObjectURL on remove/send.
            let previewUrl = "";
            if (up.kind === "image" && /^image\//.test(sendFile.type || "")) {
              try {
                previewUrl = URL.createObjectURL(sendFile);
              } catch {
                previewUrl = "";
              }
            }
            const att = {
              id: up.id,
              name: sendFile.name,
              kind: up.kind,
              size: sendFile.size || 0,
              providerNative: up.providerNative,
              previewUrl,
            };
            pendingAttachments.push(att);
            if (up.kind === "pdf") void attachPdfPreview(att, sendFile);
          }
          renderAttachmentChips();
        } catch (e) {
          hooks.showToast(e.message || String(e), "err");
        }
      }
    } finally {
      btn?.classList.remove("is-busy");
      _releaseAttachWaiters();
    }
  }

  /** Assign a safe name to a pasted image. Paste image names are often empty or
   *  extension-less; a name is derived from the MIME type so the upload passes
   *  extension validation (e.g. ``paste-2026-06-13T10-20-30.png``). */
  function pastedImageWithName(file) {
    const hasGoodName = file.name && /\.(png|jpe?g|webp|gif)$/i.test(file.name);
    if (hasGoodName) return file;
    const extByType = {
      "image/png": "png",
      "image/jpeg": "jpg",
      "image/webp": "webp",
      "image/gif": "gif",
    };
    const ext = extByType[file.type] || "png";
    const ts = new Date().toISOString().replace(/[:.]/g, "-").slice(0, 19);
    try {
      return new File([file], `paste-${ts}.${ext}`, {
        type: file.type || "image/png",
      });
    } catch {
      return file; // File ctor unavailable — pass original through
    }
  }

  /** Return #drop-overlay; create and append to body if absent. Markup-independent
   *  (self-contained) — uses the element from index.html if present, otherwise creates it. */
  function ensureDropOverlay() {
    let el = document.getElementById("drop-overlay");
    if (el) return el;
    el = document.createElement("div");
    el.id = "drop-overlay";
    el.className = "drop-overlay";
    el.hidden = true;
    el.setAttribute("aria-hidden", "true");
    const card = document.createElement("div");
    card.className = "drop-overlay-card";
    const label = document.createElement("span");
    label.className = "drop-overlay-text";
    label.textContent = window.AkanaI18n.t("chat.drop_overlay_text");
    card.appendChild(label);
    el.appendChild(card);
    document.body.appendChild(el);
    return el;
  }

  /** Wire drag-and-drop + Ctrl+V to the WINDOW level (one-shot).
   *
   * When a file is dragged anywhere on the page, a full-screen "Drop" overlay
   * (#drop-overlay) appears; it is uploaded as an attachment wherever dropped.
   * preventDefault on dragover is REQUIRED — otherwise the browser opens the dropped file.
   * Overlay visibility is managed with dragover + a timer (instead of an enter/leave
   * counter): stays open while dragging, closes ~150ms after dragging stops (exit /
   * cancel / drop) — no flickering, no stuck overlay.
   * Ctrl+V captures a pasted image regardless of focus; text paste passes through. */
  let _windowDndWired = false;
  function wireWindowDropAndPaste() {
    if (_windowDndWired) return;
    _windowDndWired = true;
    const overlay = ensureDropOverlay();
    const hasFiles = (e) =>
      !!e.dataTransfer &&
      Array.from(e.dataTransfer.types || []).includes("Files");
    let hideTimer = null;
    const hideOverlay = () => {
      if (hideTimer) {
        clearTimeout(hideTimer);
        hideTimer = null;
      }
      if (overlay) overlay.hidden = true;
    };
    // dragover fires continuously → each time the hide is deferred; when dragging stops
    // (exit / cancel / drop) the overlay closes after the last deferral.
    window.addEventListener("dragover", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault(); // REQUIRED: otherwise the browser opens the dropped file
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
      if (overlay) overlay.hidden = false;
      if (hideTimer) clearTimeout(hideTimer);
      hideTimer = setTimeout(hideOverlay, 150);
    });
    window.addEventListener("drop", (e) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      hideOverlay();
      if (e.dataTransfer?.files?.length) {
        void handleAttachmentFiles(e.dataTransfer.files);
      }
    });
    window.addEventListener("paste", (e) => {
      const items = Array.from(e.clipboardData?.items || []);
      const pastedImgs = [];
      for (const it of items) {
        if (it.kind === "file" && /^image\//.test(it.type || "")) {
          const f = it.getAsFile();
          if (f) pastedImgs.push(pastedImageWithName(f));
        }
      }
      if (!pastedImgs.length) return; // no image — let text paste through
      e.preventDefault();
      void handleAttachmentFiles(pastedImgs);
    });
  }

  function wireComposerAttachments() {
    const btn = document.getElementById("btn-attach");
    const input = document.getElementById("attach-input");
    if (btn && input) {
      btn.addEventListener("click", () => input.click());
      input.addEventListener("change", () => {
        void handleAttachmentFiles(input.files);
        input.value = "";
      });
    }
    // Drag-and-drop + Ctrl+V are now WINDOW-level: files can be dropped/pasted
    // anywhere on the page, not just into the composer. Old composer-scoped
    // (.is-dragover) and #msg paste listeners have been removed.
    wireWindowDropAndPaste();
  }

  const escapeHtml = (s) => window.AkanaCore.escapeHtml(s);
  const mapServerMessagesToThread = (m) => window.AkanaChatRender.mapServerMessagesToThread(m);

  let chatRenderer = null;
  function chatRenderMessage(m) {
    if (!chatRenderer) return;
    chatRenderer.chatRenderMessage(m);
  }


  let chatThreads = null;
  let chatTransport = null;

  function buildBridge() {
    return {
      get hooks() {
        return hooks;
      },
      chatRenderMessage,
      mapServerMessagesToThread,
      fetchConversationTurns: (convId) =>
        ensureTransport().fetchConversationTurnsFromServer(convId),
      abortConversationTurnsFetch: (convId) =>
        ensureTransport().abortConversationTurnsFetch(convId),
      resumeActiveTurn: (convId) => ensureTransport().resumeActiveTurn(convId),
      probeActiveTurn: (convId) => ensureTransport().probeActiveTurn(convId),
      // Stop/teardown: no arg → aborts the DISPLAYED conv's stream; with convId aborts
      // that conv's stream (path for background conversation deletion). Concurrent N-streams:
      // other conversations' live streams are NOT affected.
      abortStream: (convId) => ensureTransport().abortActiveChatStream(convId),
      // Notify transport of the displayed conversation (foreground plan UI gate).
      // Called on conversation switch/restore so the foreground plan UI latches to the right stream.
      setForegroundConversation: (convId) =>
        ensureTransport().setForegroundConversation(convId),
      isConversationStreamActive: (convId) =>
        ensureTransport().isConversationStreamActive(convId),
      // A3: turn_id of the conv's live in-memory stream (for refreshConversationLogAfterTurn's
      // "same turn vs next drained turn" check).
      activeStreamTurnId: (convId) => ensureTransport().activeStreamTurnId?.(convId),
      reattachLiveRow: (convId) => ensureTransport().reattachLiveRow(convId),
      // ── PARALLEL-CHAT PANES: conversation visibility/lifecycle (delegated to AkanaShell).
      //    showConversation = show that conversation's pane (others hidden, NO wipe);
      //    clearConversation = clear only that pane (hydrate);
      //    removeConversation = delete; rekeyConversation = remap pane when a new-empty
      //    conversation acquires a server id. ──────────────────────────────────────────
      showConversation: (convId) => window.AkanaShell?.showConversation?.(convId),
      clearConversation: (convId) => window.AkanaShell?.clearConversation?.(convId),
      removeConversation: (convId) => window.AkanaShell?.removeConversation?.(convId),
      rekeyConversation: (oldId, newId) => window.AkanaShell?.rekeyConversation?.(oldId, newId),
      // Sync composer to the displayed conversation's stream state (on switch).
      syncComposerForDisplayed: (convId) =>
        syncComposerForDisplayedConversation(convId),
      cancelActiveTurnOnServer: (convId) =>
        ensureTransport().cancelActiveTurnOnServer(convId),
      clearPendingAttachments,
    };
  }

  function ensureThreads() {
    if (!chatThreads) chatThreads = window.AkanaChatThreads.create(buildBridge());
    return chatThreads;
  }

  function buildChatCtx() {
    const t = ensureThreads();
    return {
      get hooks() {
        return hooks;
      },
      get chatInFlight() {
        return chatInFlight;
      },
      set chatInFlight(v) {
        chatInFlight = !!v;
      },
      get chatStore() {
        return t.getChatStore();
      },
      get chatArchiveItems() {
        return t.getChatArchiveItems();
      },
      set chatArchiveItems(v) {
        t.setChatArchiveItems(v);
      },
      get activeConversationMeta() {
        return t.getActiveConversationMeta();
      },
      set activeConversationMeta(v) {
        t.setActiveConversationMeta(v);
      },
      conversationIdForMemory: () => t.conversationIdForMemory(),
      setConversationId: (id) => t.setConversationId(id),
      // PARALLEL-CHAT: when a stream first learns its conv-id (first/default conversation,
      // sitting in a blank-sentinel pane until the server id arrives), bind the DISPLAYED
      // pane to the real conv-id → when the user returns to this conversation,
      // showConversation will find it. (Otherwise a blank new pane would open =
      // "pre-refresh switches are buggy" report.)
      rekeyDisplayedPane: (convId) => {
        if (!convId) return;
        const cur = window.AkanaShell?.displayedConvId?.();
        // Only bind the BLANK/new displayed pane (cur empty) to the real id.
        // If the displayed pane ALREADY belongs to a different real conv (user may have
        // switched during POST/stream), do NOT rekey it → prevents cross-conv pane clobber
        // (response landing in the wrong conversation/blank pane). cur===convId → rekey no-op.
        if (cur && cur !== convId) return;
        window.AkanaShell?.rekeyConversation?.(cur, convId);
      },
      // Pending new-conversation eager-create promise. ensureConversationIdReady awaits this
      // → send on a new empty conversation uses the eager-create conv id
      // (does NOT POST a SECOND conv → no double-create / orphan conversation).
      pendingNewThread: () => t.getPendingNewThread?.(),
      reloadConversationLogFromServer: (id) => t.reloadConversationLogFromServer(id),
      syncConversationLogFromServer: (id) => t.syncConversationLogFromServer(id),
      applyChatServerAction: (a, p, o) => t.applyChatServerAction(a, p, o),
      purgeConversationFromChatStore: (id) => t.purgeConversationFromChatStore(id),
      consumePendingFileIds: () => consumePendingFileIds(),
      thinkingMode: () => currentEffortMode(),
      // Plan mode: the composer toggle was removed (ExitPlanMode is interactive-only
      // in current headless `claude -p`, so plan mode can't run) → normal turns never
      // request it. A plan card's Apply/Revise still passes a per-turn override.
      planMode: () => false,
      chatProfile: () => t.chatProfile(),
      newChatThreadId: () => t.newChatThreadId(),
      chatStartNewThread: (o) => t.chatStartNewThread(o),
      syncChatThreadBar: () => t.syncChatThreadBar(),
      loadChatArchiveList: () => t.loadChatArchiveList(),
    };
  }

  function ensureTransport() {
    if (!chatTransport) chatTransport = window.AkanaChatTransport.create(buildChatCtx());
    return chatTransport;
  }

  const fetchConversationTurnsFromServer = (id) =>
    ensureTransport().fetchConversationTurnsFromServer(id);
  const ensureConversationIdReady = () => ensureTransport().ensureConversationIdReady();
  const streamChat = (text, opts) => ensureTransport().streamChat(text, opts);
  const humanizeChatError = (err) => ensureTransport().humanizeChatError(err);
  const abortActiveChatStream = (convId) => ensureTransport().abortActiveChatStream(convId);
  const cancelActiveTurnOnServer = (convId) =>
    ensureTransport().cancelActiveTurnOnServer(convId);

  // Thin delegates for app.js / wire handlers
  const t = () => ensureThreads();
  const chatRecordMessage = (msg) => t().chatRecordMessage(msg);
  const recordPendingUserMessage = (text, fileIds) =>
    t().recordPendingUserMessage(text, fileIds);
  const reloadConversationLogFromServer = (id) => t().reloadConversationLogFromServer(id);
  const syncConversationLogFromServer = (id) => t().syncConversationLogFromServer(id);
  const setConversationId = (id) => t().setConversationId(id);
  const conversationIdForMemory = () => t().conversationIdForMemory();
  const chatActiveThread = () => t().chatActiveThread();
  const chatRestoreActiveThread = () => t().chatRestoreActiveThread();
  const chatStartNewThread = (o) => t().chatStartNewThread(o);
  const loadChatArchiveList = () => t().loadChatArchiveList();
  const openArchiveDrawer = () => t().openArchiveDrawer();
  const closeArchiveDrawer = () => t().closeArchiveDrawer();
  const deleteConversationById = (id, o) => t().deleteConversationById(id, o);
  const switchChatConversation = (id) => t().switchChatConversation(id);
  const tryHandleChatDeleteCommand = (text) => t().tryHandleChatDeleteCommand(text);

  // ─── Send ↔ Stop button mode ────────────────────────────────────────────────
  // While a response streams, the send button switches to Stop (red square). Clicking
  // Stop aborts the stream (client-side SSE abort). New messages cannot be sent while
  // Stop is active (submit guard). Reverts to Send when the stream ends/is aborted.
  // NOTE: server-side detached-turn cancellation requires a backend endpoint (F2) —
  // for now the client abort cuts the stream; the server turn keeps running until done
  // (response stays coherent; can be rejoined via resume).
  function setSendButtonMode(mode) {
    const { sendBtn } = hooks;
    if (!sendBtn) return;
    if (mode === "stop") {
      sendBtn.dataset.mode = "stop";
      sendBtn.disabled = false; // Stop must remain clickable
      sendBtn.setAttribute("aria-label", window.AkanaI18n.t("chat.send_btn_stop_aria"));
      sendBtn.title = window.AkanaI18n.t("chat.send_btn_stop_title");
    } else {
      sendBtn.dataset.mode = "send";
      sendBtn.disabled = false;
      sendBtn.setAttribute("aria-label", window.AkanaI18n.t("chat.send_btn_send_aria"));
      sendBtn.title = "";
    }
  }

  /** Stop: cancel the running turn of the DISPLAYED conversation; queue is preserved.
   *  Cross-conversation protection comes from chatInFlight being CORRECT (reflecting the
   *  displayed conversation — see submitChatText finally + syncComposerForDisplayed):
   *  if the displayed conversation is not streaming the composer is already in SEND → Stop is not shown. */
  function requestStopActiveStream() {
    const convId = conversationIdForMemory();
    // b27: STOP frees the conversation for a fresh send → drop its setup latch immediately
    // (do not wait for the aborted stream's promise to settle).
    if (convId) _submitSetupConvs.delete(convId);
    if (!chatInFlight) {
      syncSendButtonMode();
      return;
    }
    try {
      abortActiveChatStream(convId); // target the ACTIVE conv's stream, NOT the foreground plan
    } catch {
      /* ignore */
    }
    try {
      void cancelActiveTurnOnServer(convId);
    } catch {
      /* ignore */
    }
    chatInFlight = false;
    syncSendButtonMode();
    void refreshQueueState(convId);
    hooks.syncOrbWithVoice?.();
    try {
      hooks.setComposerHint?.("idle");
    } catch {
      /* ignore */
    }
  }

  /**
   * Single send body: both typed form submit and voice conversation mode (voiceTurn)
   * flow through here. In voiceTurn the composer (msg) is not read/cleared and focus
   * is not stolen; streamChat skips voice-cancellation (see transport), and TTS is
   * forced by streamTtsParam in conversation mode.
   */
  async function submitChatText(text, opts = {}) {
    const { forceImmediate = false, voiceTurn = false, planMode } = opts;
    // b32: the voice path must also wait for in-flight uploads before it consumes the pending
    // attachments (the typed form handler already does). Otherwise a voice turn sent while an
    // upload is still running drops the attachment from THIS turn and leaks it into the next one.
    if (voiceTurn && attachmentsUploading()) {
      await Promise.race([
        whenAttachmentsReady(),
        new Promise((r) => setTimeout(r, 10000)),
      ]);
    }
    // #16: NEW-conversation double-send guard (synchronous). Without a conv_id, rapid
    // double Enter would slip the second call in before the first call's
    // ensureConversationIdReady await resolved, opening TWO separate convs (server-side
    // race guard only works within the SAME conv_id). Since chatInFlight is set
    // synchronously below, the second call drops here.
    // Only drop when conv_id is ABSENT — the 2nd message in an existing conversation
    // must go to the server queue (do not drop it). forceImmediate (Stop→send) / voiceTurn exempt.
    if (chatInFlight && !forceImmediate && !voiceTurn && !conversationIdForMemory()) {
      return;
    }
    // SINGLE-TURN MODEL: only ONE response may be produced at a time. Do not start a new
    // turn while a live turn exists in any visible or BACKGROUND conversation. Parallel
    // stream + queue confusion (blank screen / conversation switch / cross-conv bug class)
    // originated exactly here: switching to B while A streams and sending in B opened a 2nd stream.
    // While the displayed conversation streams, the button is already Stop → forceImmediate, exempt;
    // this guard only blocks normal sends while ANOTHER conversation is streaming. voiceTurn exempt.
    // Do NOT drop the message: return early and PRESERVE the composer text (user can wait
    // and resend or press Stop).
    if (!forceImmediate && !voiceTurn) {
      // PER-CONV SINGLE-TURN (parallel-chat): do not start a new turn only while the
      // DISPLAYED conversation's own turn is running. SINGLE SOURCE OF TRUTH = displayedConvId (pane).
      // Previously busy was also computed from the GLOBAL `chatInFlight` → in concurrent n-chat
      // that flag stayed stale-true / `conversationIdForMemory()` lagged to the streaming
      // conversation, blocking sends to the NEW (empty) chat with "wait for response"
      // (user: "I can't open the 4th chat"). Now look ONLY at the displayed conv's stream →
      // sending to a new/empty chat while other conversation(s) stream in the background is FREE.
      // (While the same chat streams the button is already Stop → forceImmediate, exempt;
      // rapid double-send without conv_id is caught by the #16 guard above.)
      let dispConv = conversationIdForMemory();
      try {
        const d = window.AkanaShell?.displayedConvId?.();
        if (d != null) dispConv = d;
      } catch {
        /* AkanaShell absent — fall back to conversationIdForMemory */
      }
      let busy = false;
      try {
        // b27: also treat the conv as busy while a prior submit for it is still in its setup
        // window (isConversationStreamActive lags behind chatInFlight during upload/provider
        // awaits) → a same-conv double-submit is blocked (composer preserved) instead of
        // running the turn twice.
        busy = Boolean(
          dispConv &&
            (ensureTransport().isConversationStreamActive?.(dispConv) ||
              _submitSetupConvs.has(dispConv)),
        );
      } catch {
        /* ignore */
      }
      if (busy) {
        hooks.showToast?.(window.AkanaI18n.t("chat.stream_busy"), "info");
        return;
      }
    }
    if (forceImmediate) {
      const convId = conversationIdForMemory();
      try {
        abortActiveChatStream(convId); // abort the ACTIVE conv's stream, not the foreground plan
      } catch {
        /* ignore */
      }
      chatInFlight = false;
      if (convId) _submitSetupConvs.delete(convId); // b27: STOP→send frees the prior setup latch

      try {
        await cancelActiveTurnOnServer(convId);
      } catch {
        /* ignore */
      }
    }
    if (!voiceTurn && tryHandleChatDeleteCommand(text)) {
      hooks.msg.value = "";
      hooks.resizeComposer();
      return;
    }
    chatInFlight = true;
    // b27: mark this EXISTING conversation as "setup in flight" (synchronously, before the
    // streamChat await) so a concurrent same-conv submit is blocked until the stream registers.
    // (A NEW conv has no id yet → covered by the synchronous chatInFlight #16 guard above.)
    const _setupConvKey = conversationIdForMemory();
    if (_setupConvKey) _submitSetupConvs.add(_setupConvKey);
    syncSendButtonMode();
    hooks.setOrb("send");
    hooks.setComposerHint("thinking");
    // Optimistic echo: show the sent attachments (not yet consumed) in the bubble;
    // server sync later returns the same ids (cached → no re-fetch needed).
    // b30: consume the pending attachment ids EXACTLY ONCE, up front — the echo/thread-record
    // below and the wire payload must use the identical set. Previously the echo snapshotted them
    // here (map) while the wire consumed them LATE inside chatPayload (after the network await in
    // streamChat) → the two could diverge and an attachment bled across the send window.
    // Snapshot the attachment objects BEFORE consume (which revokes previews + clears the
    // strip) → on a PRE-stream failure the turn never persisted, so restore them to the
    // composer instead of silently dropping the files from a Retry/re-send (fe-chat-core-5).
    const _sentAttachments = pendingAttachments.slice();
    const sentFileIds = consumePendingFileIds();
    const userRow = hooks.appendUserMessage(text, sentFileIds);
    // On send, scroll the new user message to the TOP of the viewport → the response
    // streams into the space below (does not stick to the composer). Send path only (not hydrate).
    hooks.scrollNewTurnToTop?.(userRow);
    // Local source of truth: save the typed message as "pending" in the ACTIVE thread.
    // Previously the message was only added to the DOM, NEVER written to thread.messages →
    // switching to another conversation and returning caused the server-snapshot (which might
    // not yet include this message) to delete it. Server-snapshot writers now MERGE this;
    // when the server reflects the message, the pending entry collapses to a single copy
    // (no duplicate user messages).
    try {
      recordPendingUserMessage(text, sentFileIds);
    } catch {
      /* record failure must never break the send/stream */
    }
    if (!voiceTurn) {
      hooks.msg.value = "";
      hooks.resizeComposer();
    }
    // The conversation for THIS turn (used by the safety timer + finally). May be empty
    // on a new conversation → the thread active at send gets its id bound by
    // ensureConversationIdReady during the turn, so capture the THREAD too and re-read its
    // conversationId when the timer fires (survives a mid-turn conversation switch).
    const turnConvId = conversationIdForMemory();
    let turnThread = null;
    try { turnThread = chatActiveThread(); } catch { /* threads seam may not expose it */ }
    const safetyTimer = window.setTimeout(
      () => {
        // CROSS-CONVERSATION GUARD: abort THIS TURN's own stream (NOT the foreground plan).
        // For a brand-new chat turnConvId was "" at capture time; passing undefined makes
        // transport fall back to the FOREGROUND (currently displayed) conversation →
        // A's 10-min safety timer would KILL B's live stream. Resolve this turn's real id
        // from its own thread and NEVER pass undefined; if still unknown, abort nothing.
        const ownConvId = turnConvId || turnThread?.conversationId || "";
        if (!ownConvId) return;
        try {
          abortActiveChatStream(ownConvId);
        } catch {
          /* ignore */
        }
        const dispConv = conversationIdForMemory();
        chatInFlight = Boolean(
          ensureTransport().isConversationStreamActive?.(dispConv),
        );
        syncSendButtonMode();
        hooks.syncOrbWithVoice();
      },
      10 * 60 * 1000,
    );
    let queued = false;
    try {
      const result = await streamChat(text, {
        forceImmediate,
        voiceTurn,
        planMode,
        fileIds: sentFileIds, // b30: use the ids consumed up front (no late re-consume divergence)
      });
      if (result && result.queued) {
        queued = true;
        hooks.setOrb("ok");
        // b7: a QUEUED (202) voice turn produces no stream → the transport finally that emits
        // voice:tts:streamEnd never runs, so the voice loop's latch (ttsStreamOpen/awaitingReply)
        // stays stuck and the mic only re-arms after the ~15s watchdog. Release it now
        // (idempotent, same as the pre-stream-error rescue below).
        if (voiceTurn) {
          try {
            window.AkanaBus?.emit?.("voice:tts:streamEnd", {});
          } catch {
            /* ignore */
          }
        }
      } else {
        hooks.setOrb("ok");
      }
    } catch (err) {
      const name = err && err.name ? err.name : "";
      if (name !== "AbortError") {
        const errText = humanizeChatError(err);
        // When the live stream already rendered an error card (with Retry), don't append
        // a duplicate "Error" row. Still persist it (the card is live-only) so the failure
        // stays visible after reload, and still drive orb/voice state below.
        // A1: hooks.appendRow writes into the DISPLAYED pane and chatRecordMessage into the
        // ACTIVE thread — but parallel-chat lets the user switch chats mid-stream, so both
        // may belong to a DIFFERENT conversation than this turn's (turnConvId). Only append
        // the DOM row when this turn's conversation is still the displayed one (the transport
        // already renders the error into the correct pane otherwise), and persist the error
        // to turnConvId's own thread instead of the active thread.
        const dispConvNow = window.AkanaShell?.displayedConvId?.();
        const turnIsDisplayed = !turnConvId || dispConvNow === turnConvId;
        if (!err?.errorCardShown && turnIsDisplayed) {
          hooks.appendRow(
            `<div class="meta">${window.AkanaI18n.t("msg.err_label")}</div><div class="bubble-bot">${escapeHtml(errText)}</div>`,
          );
        }
        // A1: scope the persisted error to turnConvId's thread when known; fall back to the
        // active-thread writer only for a brand-new conversation with no bound thread yet.
        const errRecord = { kind: "error", text: errText, userText: text };
        if (!turnConvId || !ensureThreads().recordErrorForConversation?.(turnConvId, errRecord)) {
          chatRecordMessage(errRecord);
        }
        // fe-chat-core-5: a PRE-stream failure (409 TURN_BUSY / 503 / network before the SSE
        // opened → !errorCardShown) never persisted the turn, so return the consumed
        // attachments to the composer — otherwise the error card's text-only Retry (and a
        // manual re-send) silently drops the file_ids and the image just vanishes. Only when
        // this turn is the displayed conversation (the composer is shared) and not a voice turn.
        if (
          !voiceTurn &&
          !err?.errorCardShown &&
          turnIsDisplayed &&
          _sentAttachments.length &&
          !pendingAttachments.length
        ) {
          for (const a of _sentAttachments) a.previewUrl = ""; // preview URL was revoked on consume
          pendingAttachments = _sentAttachments;
          try {
            renderAttachmentChips();
          } catch {
            /* ignore */
          }
        }
        hooks.setOrb("err");
        // VOICE LATCH SAFETY: in voiceTurn, a PRE-stream HTTP error (409 TURN_BUSY / 503)
        // throws before streamChat enters consumeSseResponse → transport's finally block that
        // emits `voice:tts:streamEnd` NEVER runs → ttsStreamOpen/convAwaitingReply stays
        // stuck at true, mic never re-opens, scene freezes at "Thinking".
        // In-stream errors (200 + error SSE: LLM_RATE_LIMITED) already go through that finally;
        // here we only rescue the pre-stream throw. streamEnd handler is idempotent +
        // re-arm only happens when the TTS queue is empty → does not affect text chat.
        if (voiceTurn) {
          try {
            window.AkanaBus?.emit?.("voice:tts:streamEnd", {});
          } catch {
            /* ignore */
          }
        }
      }
    } finally {
      window.clearTimeout(safetyTimer);
      // b27: release the per-conv setup latch (the stream has registered or the submit failed).
      if (_setupConvKey) _submitSetupConvs.delete(_setupConvKey);
      // CROSS-CONVERSATION GUARD: set the GLOBAL composer state (chatInFlight/"Thinking"/
      // SEND-STOP) according to the DISPLAYED conversation's REAL stream state.
      // Previously it was set unconditionally via isStreamActive() (ANY stream) + queued
      // logic → a turn completing in A would prematurely flip B's "Thinking" to SEND
      // while the displayed B was still streaming (user: "can't see what's happening in
      // the parallel stream").
      const dispConv = conversationIdForMemory();
      const dispStreaming = Boolean(
        ensureTransport().isConversationStreamActive?.(dispConv),
      );
      chatInFlight = dispStreaming;
      hooks.setComposerHint?.(dispStreaming ? "thinking" : "idle");
      syncSendButtonMode();
      if (!queued) void refreshQueueState();
      hooks.syncOrbWithVoice();
      window.AkanaVoice?.resumeWakeListeningIfIdle?.();
      if (!voiceTurn) {
        try {
          hooks.msg.focus();
        } catch {
          /* ignore */
        }
      }
    }
  }

  function wireChatForm() {
    const { form, msg, sendBtn } = hooks;
    if (!form || !msg || !sendBtn) return;
    // In Stop mode, clicking triggers cancellation, not submit. Capture phase
    // so the form submit never starts (safe for rapid successive clicks).
    sendBtn.addEventListener(
      "click",
      (e) => {
        if (sendBtn.dataset.mode === "stop") {
          e.preventDefault();
          e.stopPropagation();
          const draft = msg.value.trim();
          if (draft || pendingAttachments.length) {
            stopThenSendRequested = true;
            form.requestSubmit();
          } else requestStopActiveStream();
        }
      },
      true,
    );
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = msg.value.trim();
      // EC1: if submitted while uploading, do not drop attachments — wait for uploads to
      // finish so pending is populated (10 s max safety). NOTE: empty-check must come AFTER
      // the wait; otherwise a text-free attachment-only message (uploading → pending still
      // empty) would be silently dropped.
      if (attachmentsUploading()) {
        hooks.showToast?.(window.AkanaI18n.t("chat.attachments_uploading"), "info");
        await Promise.race([
          whenAttachmentsReady(),
          new Promise((r) => setTimeout(r, 10000)),
        ]);
      }
      // EC3: attachment-only message — can be sent if text is empty but an attachment
      // exists; drop if both are absent.
      if (!text && !pendingAttachments.length) return;
      // EC6: re-validate the active provider's per-message limit at send time
      // (pending attachments may exceed the limit after a provider switch).
      if (await pendingExceedsMsgLimits()) return;
      const forceImmediate = stopThenSendRequested;
      stopThenSendRequested = false;
      await submitChatText(text, { forceImmediate });
    });
  }

  function init(opts = {}) {
    hooks = { ...hooks, ...opts };
    // PARALLEL-CHAT PANES: hooks.log now resolves to the DISPLAYED conversation's pane
    // (AkanaShell.displayedPane). All existing hooks.log.appendChild / innerHTML="" /
    // .children calls automatically target the displayed conversation's pane;
    // background conversations' panes + live streams are UNTOUCHED (replaces the old
    // single-#log wipe+reattach model). Falls back to raw #log when PaneManager is absent
    // (backward compat). paneFor(convId) returns a specific conversation's pane
    // (resume / background render target).
    {
      const _rawLog =
        hooks.log || (typeof document !== "undefined" && document.getElementById?.("log")) || null;
      try {
        Object.defineProperty(hooks, "log", {
          configurable: true,
          enumerable: true,
          get() { return window.AkanaShell?.displayedPane?.() || _rawLog; },
        });
      } catch { /* getter cannot be defined — keep raw log */ }
      hooks.paneFor = (convId) => window.AkanaShell?.paneFor?.(convId) || _rawLog;
    }
    // Stream UI bridge: every stream path (live stream + resume) drives the button mode
    // from here. Also syncs chatInFlight so the submit guard stays consistent.
    hooks.setStreamingUi = (active) => {
      chatInFlight = !!active;
      syncSendButtonMode();
    };
    hooks.setQueueDepth = setQueueDepth;
    chatRenderer = window.AkanaChatRender.createRenderer(hooks);
    chatThreads = null;
    chatTransport = null;
    ensureThreads();
    if (_chatPageWired) return;
    _chatPageWired = true;
    ensureThreads().wireArchiveChrome();
    ensureThreads().wireThreadBar();
    wireChatForm();
    wireComposerAttachments();
    wireThinkingMode();
  }

  window.AkanaChat = {
    init,
    // Voice conversation mode turn: writes to the same chat pipeline with the voiceTurn
    // flag, without touching the composer (shows in log, TTS active).
    submitVoiceText: (text) => submitChatText(text, { voiceTurn: true }),
    // AskUserQuestion answer: when the user clicks an option on the question card,
    // the selection is sent as a NORMAL typed turn (without touching the composer).
    // Session continues via ``--resume`` → Claude reads the answer as a reply to its question.
    submitAnswerText: (text) => submitChatText(text, {}),
    // Plan card decision (claude plan-mode). "Apply" → execute the plan: resume plan
    // OFF (planMode:false). "Revise" → re-plan with free text: resume plan
    // ON (planMode:true → keepPlanning). Session continues via ``--resume``.
    submitPlanText: (text, opts = {}) =>
      submitChatText(text, { planMode: opts.keepPlanning === true }),
    chatRecordMessage,
    reloadConversationLogFromServer,
    syncConversationLogFromServer,
    setConversationId,
    conversationIdForMemory,
    // Get + clear the composer's pending upload ids (so voice turns can also attach
    // images/PDFs; per-turn /voice reads this and puts them in the ``file_ids`` form field).
    consumePendingFileIds: () => consumePendingFileIds(),
    // b32: let the voice single-shot path wait for in-flight uploads before it consumes the
    // pending attachments (symmetric with the typed/conversation-mode gate).
    attachmentsUploading: () => attachmentsUploading(),
    whenAttachmentsReady: () => whenAttachmentsReady(),
    // Read-only: is a specific conv's stream active (even if not displayed)?
    // For parallel-chat diagnostics + e2e evidence (is A still streaming while B is sent?).
    isConversationStreamActive: (convId) => {
      try {
        return Boolean(ensureTransport().isConversationStreamActive?.(convId));
      } catch {
        return false;
      }
    },
    chatActiveThread,
    chatRestoreActiveThread,
    chatStartNewThread,
    loadChatArchiveList,
    openArchiveDrawer,
    closeArchiveDrawer,
    getChatInFlight: () => chatInFlight,
    setChatInFlight: (on) => {
      chatInFlight = !!on;
    },
    deleteConversationById,
    switchChatConversation,
    abortActiveChatStream,
    cancelActiveTurnOnServer,
    refreshQueueState,
    onTurnCompletedRemote,
    onBackgroundTurnCompleted,
    onTurnActiveRemote,
    onBackgroundTurnActive,
    maybeShowBgWorking,
    setQueueDepth,
    setThinkingProvider,
    // Test-only seam (node-vm contract harness): seed/read the composer's pending
    // attachments so the pre-stream-failure restore path (fe-chat-core-5) is drivable.
    _test: {
      seedPendingAttachment: (att) => { pendingAttachments.push(att); },
      getPendingAttachments: () => pendingAttachments.slice(),
    },
  };
})();

/* ═══════════════════════════════════════════════════════════════════════════
   MESSAGE HOVER ACTION BAR + STREAMING PHASE STRIP (append-only module)
   ─────────────────────────────────────────────────────────────────────────
   F1 — Hover action bar: SINGLE delegation listener on #log; no per-row wiring,
   render JS is untouched. One repositioned glass bar sits absolute inside
   #log-scroll (CSS position context there).
   Actions: Copy / Quote (both row types) in the top-right pill.
     • "Read aloud" — a separate small speaker button anchored to the BOTTOM-LEFT
       of assistant bubbles only. It STREAMS per-sentence audio from
       /api/v1/voice/tts/stream (SSE) and plays chunks in order, so the first
       sentence is audible almost immediately instead of after the whole message
       synthesizes. Uses a plain queue of <audio> elements, deliberately NOT the
       voice-conversation ttsPlayer (which is wired into the barge-in / re-arm
       state machine and must not be disturbed). Falls back to the one-shot
       /api/v1/voice/tts blob if streaming is unavailable. Click again to stop.
   F2 — Live turn status: akana-turn-status.js renders a single strip above
   the composer (phase + elapsed). Driven by transport SSE events via
   setPhase/begin/end; old 250ms DOM polling removed. mount() is called here
   too (once the form is ready).
   ═══════════════════════════════════════════════════════════════════════════ */
(() => {
  "use strict";

  function setup() {
    if (typeof document === "undefined" || typeof document.getElementById !== "function") return;
    const log = document.getElementById("log");
    const logScroll = document.getElementById("log-scroll");
    const form = document.getElementById("chat-form");
    const msg = document.getElementById("msg");
    if (!log || !msg || !form) return; // not the chat page (or test stub)
    const host = logScroll || log; // position context for the bar (CSS: relative)

    /* ── F1 · Hover action bar ───────────────────────────────────────────── */
    let currentRow = null;

    const bar = document.createElement("div");
    bar.className = "msg-actionbar";
    bar.setAttribute("role", "toolbar");
    bar.setAttribute("aria-label", window.AkanaI18n.t("chat.actionbar_aria"));
    bar.hidden = true;

    function mkBtn(label, title) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "msg-action-btn";
      b.textContent = label;
      b.title = title;
      b.setAttribute("aria-label", title);
      return b;
    }
    const copyBtn = mkBtn(window.AkanaI18n.t("chat.copy_btn"), window.AkanaI18n.t("chat.copy_btn_title"));
    const quoteBtn = mkBtn(window.AkanaI18n.t("chat.quote_btn"), window.AkanaI18n.t("chat.quote_btn_title"));
    bar.appendChild(copyBtn);
    bar.appendChild(quoteBtn);
    host.appendChild(bar);

    /* ── Read-aloud speaker (assistant rows only, bottom-left) ─────────────── */
    // A second floating element sharing the bar's hover lifecycle. Icons: speaker
    // (idle) ⇄ stop (playing); .is-loading marks the synth request in flight.
    const TTS_SPEAKER_SVG =
      '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 9.5v5h3.2L12 18.5v-13L7.2 9.5H4Z" fill="currentColor"/><path d="M15.5 8.6a4 4 0 0 1 0 6.8" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><path d="M17.7 6a7 7 0 0 1 0 12" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>';
    const TTS_STOP_SVG =
      '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2.5" fill="currentColor"/></svg>';
    const ttsFab = document.createElement("button");
    ttsFab.type = "button";
    ttsFab.className = "msg-tts-fab";
    ttsFab.hidden = true;
    ttsFab.innerHTML = TTS_SPEAKER_SVG;
    ttsFab.title = window.AkanaI18n.t("chat.tts_btn_title");
    ttsFab.setAttribute("aria-label", window.AkanaI18n.t("chat.tts_btn_title"));
    host.appendChild(ttsFab);

    /** Paint the fab to match the read-aloud controller's state. Single source of
     *  truth: called on every state change and from showBarFor. While ANY read-aloud
     *  is active the fab is a Stop control (the pointer may drift off the reading row
     *  onto a neighbour as it moves down to the fab, so state is global, not per-row).
     *  Active → stop icon (pulsing while still buffering the first sentence). */
    function renderFab() {
      if (readAloud.active) {
        ttsFab.classList.add("is-playing");
        ttsFab.classList.toggle("is-loading", !readAloud.playing);
        ttsFab.innerHTML = TTS_STOP_SVG;
        ttsFab.title = window.AkanaI18n.t("chat.tts_btn_stop_title");
        ttsFab.setAttribute("aria-label", window.AkanaI18n.t("chat.tts_btn_stop_title"));
      } else {
        ttsFab.classList.remove("is-playing", "is-loading");
        ttsFab.innerHTML = TTS_SPEAKER_SVG;
        ttsFab.title = window.AkanaI18n.t("chat.tts_btn_title");
        ttsFab.setAttribute("aria-label", window.AkanaI18n.t("chat.tts_btn_title"));
      }
    }

    // Isolated STREAMING read-aloud player. It streams per-sentence audio from
    // /voice/tts/stream (SSE) and plays chunks in order, so the FIRST sentence is
    // audible almost immediately instead of waiting for the WHOLE message to
    // synthesize (the long-message latency the one-shot path suffered from).
    // Deliberately separate from the voice-conversation ttsPlayer, which is wired
    // into the barge-in/re-arm state machine and must not be disturbed. Falls back
    // to the one-shot /voice/tts blob if streaming is unavailable (e.g. the server
    // predates this endpoint).
    const readAloud = {
      row: null,
      gen: 0, // bumped on every stop → invalidates all in-flight async work
      abort: null, // AbortController for the SSE/one-shot fetch
      queue: [], // pending blob URLs, in playback order
      audio: null, // currently-playing <audio>
      streamDone: false, // server said "end" (no more chunks coming)
      playing: false, // a chunk is currently playing
      active: false, // a session is live (loading or playing)

      start(row) {
        const text = bubbleTextOf(row);
        if (!text) return;
        this.stop(); // tear down any prior session first (single playback)
        const gen = ++this.gen;
        this.row = row;
        this.active = true;
        this.streamDone = false;
        this.playing = false;
        this.queue = [];
        this.audio = null;
        this.abort = typeof AbortController === "function" ? new AbortController() : null;
        renderFab(); // → loading (pulsing stop icon)
        void this._stream(text, gen);
      },

      stop() {
        this.gen++; // any pending callbacks/awaits are now stale
        if (this.abort) {
          try { this.abort.abort(); } catch { /* ignore */ }
          this.abort = null;
        }
        if (this.audio) {
          // Pausing does NOT fire onended → the playing chunk's `done` never runs,
          // so revoke its blob URL here or it leaks on stop-mid-playback.
          try {
            this.audio.pause();
            if (this.audio.src) URL.revokeObjectURL(this.audio.src);
          } catch { /* ignore */ }
          this.audio = null;
        }
        for (const u of this.queue) {
          try { URL.revokeObjectURL(u); } catch { /* ignore */ }
        }
        this.queue = [];
        this.active = false;
        this.playing = false;
        this.streamDone = false;
        this.row = null;
        renderFab(); // → speaker
      },

      _decode(b64, mime) {
        const bin = atob(b64);
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        return URL.createObjectURL(new Blob([bytes], { type: mime || "audio/mpeg" }));
      },

      _push(url, gen) {
        if (gen !== this.gen) {
          try { URL.revokeObjectURL(url); } catch { /* ignore */ }
          return;
        }
        this.queue.push(url);
        if (!this.playing) this._playNext(gen);
      },

      _playNext(gen) {
        if (gen !== this.gen) return;
        if (!this.queue.length) {
          this.playing = false;
          if (this.streamDone) this.stop(); // drained + nothing more coming → done
          else renderFab(); // between chunks: keep the buffering look
          return;
        }
        this.playing = true;
        renderFab(); // → playing (solid stop icon)
        const url = this.queue.shift();
        const audio = new Audio(url);
        this.audio = audio;
        const done = () => {
          try { URL.revokeObjectURL(url); } catch { /* ignore */ }
          if (gen !== this.gen) return;
          if (this.audio === audio) this.audio = null;
          this._playNext(gen);
        };
        audio.onended = audio.onerror = done;
        const p = audio.play();
        if (p && typeof p.catch === "function") p.catch(done);
      },

      async _stream(text, gen) {
        let received = 0;
        try {
          const base = window.AkanaCore?.baseUrl?.() || "";
          const headers = window.AkanaCore?.authHeaders?.(true) || { "Content-Type": "application/json" };
          const res = await fetch(`${base}/api/v1/voice/tts/stream`, {
            method: "POST",
            headers,
            body: JSON.stringify({ text }),
            signal: this.abort ? this.abort.signal : undefined,
          });
          if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
          const reader = res.body.getReader();
          const decoder = new TextDecoder();
          let buf = "";
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            if (gen !== this.gen) return; // superseded / stopped
            buf += decoder.decode(value, { stream: true });
            let nl;
            while ((nl = buf.indexOf("\n\n")) >= 0) {
              const frame = buf.slice(0, nl);
              buf = buf.slice(nl + 2);
              const dataLine = frame.split("\n").find((l) => l.startsWith("data:"));
              if (!dataLine) continue;
              let obj;
              try { obj = JSON.parse(dataLine.slice(5).trim()); } catch { continue; }
              if (obj.type === "end") { this.streamDone = true; continue; }
              if (obj.type === "error") throw new Error(obj.message || "tts error");
              if (obj.audio_b64) {
                received++;
                this._push(this._decode(obj.audio_b64, obj.mime), gen);
              }
            }
          }
          if (gen !== this.gen) return;
          this.streamDone = true;
          // Stream closed producing nothing → fall back to the one-shot blob.
          if (received === 0 && !this.queue.length && !this.playing) {
            await this._oneShot(text, gen);
            return;
          }
          if (!this.playing && !this.queue.length) this.stop();
        } catch (e) {
          if (gen !== this.gen) return; // stopped → not an error
          if (e && e.name === "AbortError") return;
          if (received === 0) { await this._oneShot(text, gen); return; } // nothing played → try one-shot
          this.streamDone = true; // some audio played; let the rest drain, then reset
          if (!this.playing && !this.queue.length) this.stop();
        }
      },

      async _oneShot(text, gen) {
        try {
          const base = window.AkanaCore?.baseUrl?.() || "";
          const headers = window.AkanaCore?.authHeaders?.(true) || { "Content-Type": "application/json" };
          const res = await fetch(`${base}/api/v1/voice/tts`, {
            method: "POST",
            headers,
            body: JSON.stringify({ text }),
            signal: this.abort ? this.abort.signal : undefined,
          });
          if (!res.ok) throw new Error(`HTTP ${res.status}`);
          const blob = await res.blob();
          if (gen !== this.gen) return;
          this.streamDone = true;
          this._push(URL.createObjectURL(blob), gen);
        } catch (e) {
          if (gen !== this.gen) return;
          if (e && e.name === "AbortError") return;
          this.stop(); // give up → reset the fab
        }
      },
    };

    ttsFab.addEventListener("click", (e) => {
      e.stopPropagation();
      // While a read-aloud is active the button ALWAYS stops it — independent of
      // which row the pointer drifted onto on the way to the fab. Only when idle
      // does a click start reading the currently-hovered message.
      if (readAloud.active) { readAloud.stop(); return; }
      const row = currentRow;
      if (!row) return;
      readAloud.start(row);
    });

    /** Bubble text — tool cards are SIBLINGS of the bubble (already outside);
     *  the code-block strip inside the bubble (lang label + Copy chrome) is
     *  removed from the clone so the copied text stays clean. */
    function bubbleTextOf(row) {
      const bubble = row.querySelector(".bubble-user, .bubble-assistant, .bubble-bot");
      if (!bubble) return "";
      const clone = bubble.cloneNode(true);
      for (const n of clone.querySelectorAll(".md-code-head, .tool-call, .memory-use, .tool-calls-group")) {
        n.remove();
      }
      return (clone.textContent || "").trim();
    }

    function hideBar() {
      currentRow = null;
      bar.classList.remove("is-visible");
      bar.hidden = true;
      // The speaker only floats on hover; hiding it does NOT stop playback
      // (the readAloud controller runs independently). Re-hovering the row
      // re-shows it in the correct (Stop) state via renderFab.
      ttsFab.classList.remove("is-visible");
      ttsFab.hidden = true;
    }

    /** Position the bar above-right of the row's bubble (in host coordinates). */
    function positionBar(row) {
      const bubble = row.querySelector(".bubble-user, .bubble-assistant, .bubble-bot");
      if (!bubble || typeof bubble.getBoundingClientRect !== "function") return false;
      const hostRect = host.getBoundingClientRect();
      const bRect = bubble.getBoundingClientRect();
      bar.hidden = false; // visible for measurement (absolute → no layout shift)
      const w = bar.offsetWidth;
      const h = bar.offsetHeight;
      let top = bRect.top - hostRect.top + host.scrollTop - h - 4;
      const minTop = host.scrollTop + 2;
      if (top < minTop) top = minTop; // prevent overflow on first message
      let left = bRect.right - hostRect.left + host.scrollLeft - w;
      if (left < 2) left = 2;
      bar.style.top = `${Math.round(top)}px`;
      bar.style.left = `${Math.round(left)}px`;
      return true;
    }

    /** Anchor the speaker at the row's bottom-left corner — INSIDE the row's box
     *  (the empty left gutter below the avatar), not in the margin below it.
     *  The row is the hover-trigger; the margin gap under it fires no mouseover, so
     *  a fab placed there stayed invisible until the message text itself was hovered
     *  (reported bug: "moving straight to the icon spot shows nothing"). Keeping the
     *  fab inside the row's box makes the icon and its trigger zone coincide. */
    function positionTtsFab(row) {
      if (typeof row.getBoundingClientRect !== "function") return false;
      const hostRect = host.getBoundingClientRect();
      const rRect = row.getBoundingClientRect();
      ttsFab.hidden = false; // visible for measurement (absolute → no layout shift)
      const h = ttsFab.offsetHeight || 28;
      let top = rRect.bottom - hostRect.top + host.scrollTop - h - 2;
      const minTop = rRect.top - hostRect.top + host.scrollTop + 2;
      if (top < minTop) top = minTop; // very short row → keep the fab inside the box
      let left = rRect.left - hostRect.left + host.scrollLeft;
      if (left < 2) left = 2;
      ttsFab.style.top = `${Math.round(top)}px`;
      ttsFab.style.left = `${Math.round(left)}px`;
      return true;
    }

    function showBarFor(row) {
      // Bar is hidden while that row is streaming (pending bubble / live markdown).
      if (row.querySelector(".bubble-bot-pending, .md-content--stream")) {
        hideBar();
        return;
      }
      currentRow = row;
      if (!positionBar(row)) {
        hideBar();
        return;
      }
      // Read-aloud speaker: assistant rows only, bottom-left.
      if (row.classList.contains("row-assistant") && positionTtsFab(row)) {
        renderFab();
        requestAnimationFrame(() => {
          if (currentRow === row) ttsFab.classList.add("is-visible");
        });
      } else {
        ttsFab.classList.remove("is-visible");
        ttsFab.hidden = true;
      }
      // Wait one frame for the opacity transition from display:none.
      requestAnimationFrame(() => {
        if (currentRow === row) bar.classList.add("is-visible");
      });
    }

    // SINGLE delegation: #log mouseover → nearest .row.
    log.addEventListener("mouseover", (e) => {
      const t = e.target;
      const row = t && typeof t.closest === "function" ? t.closest(".row") : null;
      if (!row || !log.contains(row)) return;
      if (row === currentRow && !bar.hidden) return;
      showBarFor(row);
    });
    // TOUCH ACCESS (audit C2): mouseover does not fire on touch → Copy/Quote/Regenerate
    // were inaccessible on mobile. On hover-none devices, tap a row to open/close the bar
    // (toggle). Desktop (hover available) is unaffected; tapping the bar's OWN buttons
    // goes to their handler (bar.contains check).
    const _isCoarse = () => typeof matchMedia === "function" && matchMedia("(hover: none)").matches;
    log.addEventListener("click", (e) => {
      if (!_isCoarse()) return;
      const t = e.target;
      if (bar.contains(t)) return; // bar button → its own handler
      const row = t && typeof t.closest === "function" ? t.closest(".row") : null;
      if (!row || !log.contains(row)) return;
      if (row === currentRow && bar.classList.contains("is-visible") && !bar.hidden) {
        hideBar(); // tap same row again → close
      } else {
        showBarFor(row);
      }
    });
    // Tap outside → hide (touch; except bar/speaker/active row). The speaker fab is
    // a SIBLING of #log (appended to host), so it must be excluded here explicitly —
    // otherwise this capture-phase listener nulls currentRow before the fab's own
    // click handler runs, swallowing the tap on touch devices.
    document.addEventListener(
      "click",
      (e) => {
        if (bar.hidden || !_isCoarse()) return;
        const t = e.target;
        if (bar.contains(t) || ttsFab.contains(t) || (currentRow && currentRow.contains(t))) return;
        hideBar();
      },
      true,
    );
    // Position drifts on scroll → hide (passive, cheap). Also hide on mouseleave
    // (keep open if keyboard focus is on the bar — tab accessibility must not break).
    host.addEventListener("scroll", hideBar, { passive: true });
    host.addEventListener("mouseleave", () => {
      if (!bar.contains(document.activeElement)) hideBar();
    });

    /** Show ✓ feedback on the button (1.2 s). */
    function flashOk(btn, ok) {
      // Reentrancy guard: a second click within the 1.2s window must NOT capture the
      // transient "✓"/"×" glyph as the "original" label (that leaves the button stuck on
      // "✓" for the page lifetime — the bar's Copy/Quote buttons are created once). Persist
      // the TRUE label once in dataset, and clear the previous restore timer before re-flashing.
      if (btn.dataset.flashOrig == null) btn.dataset.flashOrig = btn.textContent;
      if (btn._flashTimer) window.clearTimeout(btn._flashTimer);
      btn.textContent = ok ? "✓" : "×";
      btn.classList.add(ok ? "is-ok" : "is-err");
      btn._flashTimer = window.setTimeout(() => {
        btn.textContent = btn.dataset.flashOrig;
        btn.classList.remove("is-ok", "is-err");
        delete btn.dataset.flashOrig;
        btn._flashTimer = null;
      }, 1200);
    }

    async function copyToClipboard(text) {
      try {
        await navigator.clipboard.writeText(text);
        return true;
      } catch {
        // Old browser / permission denied: hidden textarea fallback.
        try {
          const ta = document.createElement("textarea");
          ta.value = text;
          ta.setAttribute("readonly", "");
          ta.style.position = "fixed";
          ta.style.opacity = "0";
          document.body.appendChild(ta);
          ta.select();
          const ok = document.execCommand("copy");
          ta.remove();
          return ok;
        } catch {
          return false;
        }
      }
    }

    copyBtn.addEventListener("click", async () => {
      if (!currentRow) return;
      const text = bubbleTextOf(currentRow);
      if (!text) return;
      flashOk(copyBtn, await copyToClipboard(text));
    });

    quoteBtn.addEventListener("click", () => {
      if (!currentRow) return;
      const text = bubbleTextOf(currentRow);
      if (!text) return;
      let snippet = text.replace(/\s+/g, " ").slice(0, 200);
      if (text.length > 200) snippet += "…";
      // Quote prepended; existing draft preserved and appended.
      msg.value = `> ${snippet}\n\n${msg.value}`;
      try {
        msg.focus();
      } catch {
        /* ignore */
      }
      msg.dispatchEvent(new Event("input", { bubbles: true })); // autosize trigger
    });

    // Expose a dismiss handle so the pane manager can drop the hover bar + speaker
    // when the displayed conversation changes. The bar and fab are siblings of #log
    // (absolutely positioned in #log-scroll), NOT children of the conv-pane — so
    // switching panes (which only HIDES the leaving pane) leaves them floating over
    // the new chat. Without this, hovering a message in chat A then pressing Alt+N
    // (or switching chats) bled A's speaker/Quote/Copy controls onto the new chat
    // (user report).
    // _flashOk: test-only seam (node-vm contract harness) for the reentrancy-guarded button
    // feedback flash (fe-chat-core-6) — flashOk is a closure with no other reachable caller.
    window.AkanaMsgActionBar = { hide: hideBar, _flashOk: flashOk };

    window.AkanaTurnStatus?.mount?.();
  }

  if (typeof document !== "undefined" && document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", setup, { once: true });
  } else {
    setup();
  }
})();
