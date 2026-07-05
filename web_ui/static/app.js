(() => {
  const isMemoryStudioPage = document.body.classList.contains("memory-studio-page");
  const isChatPage = !isMemoryStudioPage && !!document.getElementById("chat-form");

  const orb = document.getElementById("orb");
  const log = document.getElementById("log");
  const logEmpty = document.getElementById("log-empty");
  const logScroll = document.getElementById("log-scroll") || log?.closest(".log-scroll");
  const form = document.getElementById("chat-form");
  const msg = document.getElementById("msg");
  const sendBtn = document.getElementById("send");
  const baseUrlInput = document.getElementById("base-url");
  const tokenInput = document.getElementById("api-token");

  const showToast = (m, k) => window.AkanaCore.showToast(m, k);

  const _t = (k) => window.AkanaI18n?.t(k) ?? k;

  if (!window.AkanaShell?.init) {
    console.error(_t("ui.app_shell_missing"));
    return;
  }

  window.AkanaShell.init({
    log,
    logScroll,
    logEmpty,
    msg,
    form,
    orb,
  });

  const {
    setOrb,
    setComposerHint,
    setActiveCursorModel,
    appendRow,
    appendUserMessage,
    appendSystemNotice,
    updateEmptyState,
    resizeComposer,
    stickToBottomIfFollowing,
    scrollLogToBottom,
    scrollNewTurnToTop,
    setLogLoading,
    shortConversationId,
  } = window.AkanaShell;

  let _settingsBootstrapped = false;
  let _voiceBootstrapped = false;
  let _chatBootstrapped = false;
  let _memoryBootstrapped = false;

  const syncOrbWithVoice = () => window.AkanaVoice?.syncOrbWithVoice?.();
  const streamTtsParam = () => window.AkanaVoice?.streamTtsParam?.() ?? "";
  const ttsPlayer = window.AkanaVoice?.ttsPlayer;

  const conversationIdForMemory = () => window.AkanaChat?.conversationIdForMemory?.() ?? "";
  const chatActiveThread = () => window.AkanaChat?.chatActiveThread?.() ?? null;
  const closeArchiveDrawer = () => window.AkanaChat?.closeArchiveDrawer?.();

  function bootstrapAkanaSettings() {
    if (_settingsBootstrapped || !window.AkanaSettings) return;
    _settingsBootstrapped = true;
    window.AkanaSettings.init({
      baseUrlInput,
      tokenInput,
      closeArchiveDrawer,
      setOrb,
      setComposerHint,
      setActiveCursorModel,
      showToast,
      openMemoryCompilePreview: () => window.AkanaMemoryStudio?.openCompilePreviewFromChat?.(),
    });
  }

  function bootstrapAkanaChat() {
    if (_chatBootstrapped || !window.AkanaChat) return;
    _chatBootstrapped = true;
    window.AkanaChat.init({
      log,
      logScroll,
      form,
      msg,
      sendBtn,
      appendRow,
      appendUserMessage,
      appendSystemNotice,
      updateEmptyState,
      resizeComposer,
      setOrb,
      setComposerHint,
      stickToBottomIfFollowing,
      scrollLogToBottom,
      scrollNewTurnToTop,
      setLogLoading,
      showToast,
      streamTtsParam,
      ttsPlayer,
      syncOrbWithVoice,
      updateSettingsHero: () => window.AkanaSettings?.updateSettingsHero?.(),
      loadMemoryConversations: () => window.AkanaMemoryStudio?.loadMemoryConversations?.(),
      shortConversationId,
      closeSettings: () => window.AkanaSettings?.closeSettings?.(),
      isChatPage,
    });
  }

  function bootstrapAkanaMemoryStudio() {
    if (_memoryBootstrapped || !window.AkanaMemoryStudio) return;
    _memoryBootstrapped = true;
    window.AkanaMemoryStudio.init({
      conversationIdForMemory,
      chatActiveThread,
      shortConversationId,
      msg,
      showToast,
    });
  }

  function bootstrapAkanaVoice() {
    if (_voiceBootstrapped || !window.AkanaVoice) return;
    _voiceBootstrapped = true;
    window.AkanaVoice.init({
      isChatPage,
      appendRow,
      chatRecordMessage: (m) => window.AkanaChat?.chatRecordMessage?.(m),
      setConversationId: (id) => window.AkanaChat?.setConversationId?.(id),
      setOrb,
      setComposerHint,
      getChatInFlight: () => window.AkanaChat?.getChatInFlight?.() ?? false,
      setChatInFlight: (on) => window.AkanaChat?.setChatInFlight?.(!!on),
      abortActiveChatStream: () => window.AkanaChat?.abortActiveChatStream?.(),
      getWsReadyState: () => window.AkanaSettings?.getWsReadyState?.() ?? 0,
      showToast,
      saveLlmSettings: (patch) => window.AkanaSettings?.saveLlmSettings?.(patch),
    });
  }

  bootstrapAkanaMemoryStudio();
  bootstrapAkanaSettings();

  if (isMemoryStudioPage) {
    bootstrapAkanaVoice();
    void window.AkanaMemoryStudio.loadMemoryPane();
  } else if (isChatPage) {
    bootstrapAkanaChat();
    bootstrapAkanaVoice();
    if (!window.AkanaChat) {
      console.error(_t("ui.app_chat_missing"));
      return;
    }
    updateEmptyState();
    resizeComposer();
    void window.AkanaChat.chatRestoreActiveThread();
    void window.AkanaSettings.loadHealth();
    void window.AkanaSettings.loadModelPill();
    window.AkanaSettings.connectWs();
  }
})();
