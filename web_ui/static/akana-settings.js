/**
 * Akana settings — theme, settings panel, LLM (provider+model), masked
 * credentials, policy mode + audit, WebSocket and approval policy
 * (loaded before app.js).
 */
(() => {
  const LS_THEME = "akana.theme";
  const LS_COMPACT_LOG = "akana.compactLog";
  const LS_SHOW_USAGE = "akana.showUsage";
  const LS_BASE = window.AkanaCore.LS_BASE;
  const LS_TOKEN = window.AkanaCore.LS_TOKEN;

  const isMemoryStudioPage = document.body.classList.contains("memory-studio-page");

  let hooks = {
    closeArchiveDrawer: () => {},
    setOrb: () => {},
    setComposerHint: () => {},
    setActiveCursorModel: () => {},
    showToast: (m, k) => window.AkanaCore.showToast(m, k),
    voiceWakeActive: () => window.AkanaVoice?.voiceWakeActive?.() ?? false,
    voiceMicRecording: () => window.AkanaVoice?.voiceMicRecording?.() ?? false,
    voicePostInFlight: () => window.AkanaVoice?.voicePostInFlight?.() ?? false,
    openMemoryCompilePreview: () => {},
  };

  let _settingsWired = false;
  let ws = null;
  let serverApiMarker = "";
  let llmSettingsLoadGen = 0;
  let llmPaneHydrated = false;
  // True only after the user actively picks a provider from the <select>; reset on
  // every (re)hydrate. Gates whether collectLlmSettings echoes `provider`, so an
  // unrelated save can't re-persist the display-only fallback and pin an
  // "unconfigured / follow-env" provider (see collectLlmSettings).
  let providerTouched = false;
  let reconnectTimer;
  const WS_RECONNECT_BASE_MS = 1000;
  const WS_RECONNECT_MAX_MS = 30000;
  let wsReconnectAttempt = 0;

  const baseUrl = () => window.AkanaCore.baseUrl();
  const authHeaders = (j) => window.AkanaCore.authHeaders(j);
  const parseApiError = (b, s) => window.AkanaCore.parseApiError(b, s);
  const escapeHtml = (s) => window.AkanaCore.escapeHtml(s);

  const wsStatusEl = document.getElementById("ws-status");
  const wsStatusText = wsStatusEl?.querySelector(".status-text");
  const modelProfileHint = document.getElementById("model-profile-hint");
  const modelPill = document.getElementById("model-pill");
  const modelPillText = modelPill?.querySelector(".status-text");
  const btnTheme = document.getElementById("btn-theme");
  const btnSettings = document.getElementById("btn-settings");
  const btnSettingsClose = document.getElementById("btn-settings-close");
  const settingsBackdrop = document.getElementById("settings-backdrop");
  const settingsPanel = document.getElementById("settings-panel");
  let baseUrlInput = document.getElementById("base-url");
  let tokenInput = document.getElementById("api-token");

  const t = (k, vars) => window.AkanaI18n ? window.AkanaI18n.t(k, vars) : (vars ? k : k);

  function setWsStatus(text, kind) {
    if (wsStatusText) wsStatusText.textContent = text;
    else if (wsStatusEl) wsStatusEl.textContent = text;
    if (!wsStatusEl) return;
    // `kind` drives the title + dot colour. It's explicit because `text` is
    // localized (e.g. "WS bağlı") and no longer matches English keywords.
    // Fall back to inferring from the English text for callers that omit it.
    if (!kind) {
      if (/connecting/i.test(text)) kind = "connecting";
      else if (/connected/i.test(text)) kind = "connected";
      else if (/closed/i.test(text)) kind = "closed";
      else if (/error/i.test(text)) kind = "error";
    }
    let title = t("settings.ws.title_default");
    if (kind === "connecting") title = t("settings.ws.title_connecting");
    else if (kind === "connected") title = t("settings.ws.title_connected");
    else if (kind === "closed") title = t("settings.ws.title_closed");
    else if (kind === "error") title = t("settings.ws.title_error");
    wsStatusEl.title = title;
    wsStatusEl.setAttribute("aria-label", t("settings.ws.aria_label", { state: text }));
    // Glanceable status dot: connected=green(default), connecting=amber,
    // closed/error=red.
    let state = "warn";
    if (kind === "closed" || kind === "error") state = "bad";
    else if (kind === "connected") state = "ok";
    if (state === "ok") wsStatusEl.removeAttribute("data-state");
    else wsStatusEl.setAttribute("data-state", state);
  }

  function setModelPillText(text) {
    if (modelPillText) modelPillText.textContent = text;
    else if (modelPill) modelPill.textContent = text;
  }

  function openSettings(tabId) {
    if (window.matchMedia("(max-width: 900px)").matches) hooks.closeArchiveDrawer();
    document.body.classList.add("settings-open");
    if (btnSettings) btnSettings.setAttribute("aria-expanded", "true");
    if (settingsBackdrop) settingsBackdrop.setAttribute("aria-hidden", "false");
    if (settingsPanel) settingsPanel.setAttribute("aria-hidden", "false");
    if (tabId) switchSettingsTab(tabId);
    updateConnectionEndpointCard();
    void loadHealth();
    void loadSettingsOverview();
    // LLM form is only loaded on first switch to the LLM tab (prevents race + lost edits).
  }

  function closeSettings() {
    document.body.classList.remove("settings-open");
    if (btnSettings) btnSettings.setAttribute("aria-expanded", "false");
    if (settingsBackdrop) settingsBackdrop.setAttribute("aria-hidden", "true");
    if (settingsPanel) settingsPanel.setAttribute("aria-hidden", "true");
    clearSettingsSearch();
  }
  function getPreferredTheme() {
    if (window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches) return "light";
    return "dark";
  }

  function resolveTheme(pref) {
    if (pref === "system") return getPreferredTheme();
    return pref === "light" ? "light" : "dark";
  }

  function getThemePreference() {
    const saved = localStorage.getItem(LS_THEME);
    if (saved === "light" || saved === "dark" || saved === "system") return saved;
    return "light"; // Aurora: light-theme-first default
  }

  function applyResolvedTheme(resolved) {
    const t = resolved === "light" ? "light" : "dark";
    document.documentElement.dataset.theme = t;
    const themeColor = document.querySelector('meta[name="theme-color"]');
    if (themeColor) themeColor.setAttribute("content", t === "light" ? "#f4f6fa" : "#080a0f");
    if (btnTheme) {
      btnTheme.setAttribute(
        "aria-label",
        t === "dark" ? window.AkanaI18n.t("settings.theme.aria_to_light") : window.AkanaI18n.t("settings.theme.aria_to_dark"),
      );
      btnTheme.title = t === "dark" ? window.AkanaI18n.t("settings.theme.title_light") : window.AkanaI18n.t("settings.theme.title_dark");
    }
    const hint = document.getElementById("theme-resolved-hint");
    if (hint) {
      const pref = getThemePreference();
      const resolvedWord = t === "light" ? window.AkanaI18n.t("settings.theme.light_word") : window.AkanaI18n.t("settings.theme.dark_word");
      hint.textContent =
        pref === "system"
          ? window.AkanaI18n.t("settings.theme.applied_system", { resolved: resolvedWord })
          : window.AkanaI18n.t("settings.theme.applied", { resolved: resolvedWord });
    }
  }

  function applyThemePreference(pref) {
    localStorage.setItem(LS_THEME, pref);
    applyResolvedTheme(resolveTheme(pref));
    syncThemePickerUi();
  }

  function applyTheme(theme) {
    applyThemePreference(theme === "light" ? "light" : "dark");
  }

  function syncThemePickerUi() {
    const pref = getThemePreference();
    document.querySelectorAll('input[name="theme-pref"]').forEach((el) => {
      el.checked = el.value === pref;
    });
  }

  function applyCompactLog(on) {
    document.body.classList.toggle("compact-log", !!on);
    localStorage.setItem(LS_COMPACT_LOG, on ? "1" : "0");
    const cb = document.getElementById("settings-compact-log");
    if (cb) cb.checked = !!on;
  }

  function applyShowUsage(on) {
    document.body.classList.toggle("show-usage", !!on);
    localStorage.setItem(LS_SHOW_USAGE, on ? "1" : "0");
    const cb = document.getElementById("settings-show-usage");
    if (cb) cb.checked = !!on;
  }


  function formatSaveError(status, message) {
    if (status === 401) {
      return t("settings.err.auth_401");
    }
    if (status === 404) {
      return t("settings.err.no_api_404");
    }
    if (status === 422) {
      return message
        ? t("settings.err.invalid_422", { message, restartHint: "" })
        : t("settings.err.invalid_422_bare", { restartHint: "" });
    }
    return message || `HTTP ${status}`;
  }


  function invalidateLlmSettingsLoads() {
    llmSettingsLoadGen += 1;
  }

  function readNumInput(el, fallback) {
    const raw = el ? String(el.value).trim() : "";
    if (!raw) return fallback;
    const n = Number(raw);
    return Number.isFinite(n) ? n : fallback;
  }
  async function loadHealth() {
    try {
      const r = await fetch(`${baseUrl()}/health`);
      const j = await r.json();
      serverApiMarker = j.api || "";
    } catch {
      serverApiMarker = "";
    }
  }

  // Provider + active model: pick the single correct field from the status payload
  // (model.active_tag is the new field; old servers fall back to tag by provider).
  function activeModelInfo(status) {
    const m = (status && status.model) || {};
    const raw = String(
      status?.active_provider ||
        m.provider ||
        (status && status.chat_path) ||
        m.agent_id ||
        "cursor"
    ).toLowerCase();
    // All providers (gemini included) — previously gemini fell to "cursor" and the pill
    // showed "Cursor · <cursor model>" (wrong provider + model).
    const PROV_TAGS = {
      cursor: m.cursor_tag,
      claude: m.claude_tag,
      ollama: m.ollama_tag,
      gemini: m.gemini_tag,
      openai: m.openai_tag,
    };
    const PROV_LABELS = {
      cursor: "Cursor",
      claude: "Claude",
      ollama: "Ollama",
      gemini: "Gemini",
      openai: "OpenAI",
    };
    const provider = PROV_LABELS[raw] ? raw : "cursor";
    const tag = m.active_tag || PROV_TAGS[provider] || "?";
    const label = PROV_LABELS[provider];
    return { provider, tag, label };
  }

  function formatModelUiLabel(label, tag) {
    return tag && tag !== "?" ? `${label} · ${tag}` : `${label} · ${t("settings.model.select_prompt")}`;
  }

  async function refreshModelUiFromStatus() {
    try {
      const r = await fetch(`${baseUrl()}/api/v1/system/status`, {
        headers: authHeaders(),
      });
      if (!r.ok) {
        if (modelPill) {
          setModelPillText(t("settings.model.select_prompt"));
          modelPill.dataset.state = "warn";
        }
        hooks.setActiveCursorModel("?");
        hooks.setComposerHint("idle");
        return;
      }
      const j = await r.json();
      const { provider, tag, label } = activeModelInfo(j);
      window.AkanaChat?.setThinkingProvider?.(provider);
      const uiLabel = formatModelUiLabel(label, tag);
      hooks.setActiveCursorModel(uiLabel);
      if (modelPill) {
        setModelPillText(uiLabel);
        if (tag && tag !== "?") modelPill.removeAttribute("data-state");
        else modelPill.dataset.state = "warn";
        modelPill.title = `provider=${provider}  model=${tag}`;
      }
      if (modelProfileHint) {
        modelProfileHint.textContent =
          tag && tag !== "?" ? t("settings.model.active", { model: uiLabel }) : t("settings.model.not_selected", { label });
      }
    } catch {
      if (modelPill) {
        setModelPillText(t("settings.model.no_connection"));
        modelPill.dataset.state = "bad";
      }
      if (modelProfileHint) modelProfileHint.textContent = t("settings.model.status_unavailable");
      hooks.setActiveCursorModel("?");
      hooks.setComposerHint("idle");
    }
  }

  // Pull active backend / model from /api/v1/system/status and reflect in header.
  async function loadModelPill() {
    await refreshModelUiFromStatus();
  }

  // ─── Model switcher (pill click → small popover) ─────────────────────────
  // The active model pill is no longer read-only: clicking fetches provider +
  // model list from GET /api/v1/system/llm-settings and opens a small selector;
  // selections are wired to the existing saveLlmSettings (PUT) path →
  // the running model changes without a restart. No new endpoints invented.
  let modelSwitcher = null;
  let modelSwitcherOpen = false;
  let modelSwitcherBusy = false;
  const CURSOR_MODELS_CACHE_TTL_MS = 10 * 60 * 1000;
  let cursorModelsCache = null;
  let claudeModelsCache = null;
  let geminiModelsCache = null;
  let openaiModelsCache = null;
  let onModelSwitcherDocClick = null;
  let onModelSwitcherKey = null;
  let _conversationLlmRestore = false;

  async function persistConversationLlm(convId, patch) {
    if (!convId || !patch || _conversationLlmRestore) return;
    const safe = patch && typeof patch === "object" ? patch : {};
    const body = {};
    for (const k of [
      "provider",
      "cursor_model",
      "claude_model",
      "ollama_model",
      "gemini_model",
      "openai_model",
    ]) {
      if (safe[k] !== undefined && safe[k] !== null && String(safe[k]).trim()) {
        body[k] = String(safe[k]).trim();
      }
    }
    if (!Object.keys(body).length) return;
    try {
      await fetch(
        `${baseUrl()}/api/v1/conversations/${encodeURIComponent(convId)}/llm-settings`,
        { method: "PUT", headers: authHeaders(true), body: JSON.stringify(body) },
      );
    } catch {
      /* best-effort */
    }
  }

  /** Restore saved provider/model to the global UI when switching conversations. */
  async function restoreConversationLlm(convId) {
    if (!convId) return;
    try {
      const r = await fetch(
        `${baseUrl()}/api/v1/conversations/${encodeURIComponent(convId)}/llm-settings`,
        { headers: authHeaders() },
      );
      if (!r.ok) return;
      const j = await r.json();
      const s = j.settings || {};
      const patch = {};
      if (s.provider) patch.provider = s.provider;
      if (s.cursor_model) patch.cursor_model = s.cursor_model;
      if (s.claude_model) patch.claude_model = s.claude_model;
      if (s.ollama_model) patch.ollama_model = s.ollama_model;
      if (s.gemini_model) patch.gemini_model = s.gemini_model;
      if (s.openai_model) patch.openai_model = s.openai_model;
      if (!Object.keys(patch).length) return;
      _conversationLlmRestore = true;
      try {
        invalidateLlmSettingsLoads();
        const pr = await fetch(`${baseUrl()}/api/v1/system/llm-settings`, {
          method: "PUT",
          headers: authHeaders(true),
          body: JSON.stringify({ settings: patch }),
        });
        if (!pr.ok) return;
        const pj = await pr.json();
        fillLlmForm(pj.settings || pj, pj);
        llmPaneHydrated = true;
        await loadModelPill();
        if (patch.provider) {
          // Provider changed → refresh voice Live capability (provider_is_gemini),
          // same contract as applyModelChoice()/saveLlmSettings() (see there for why).
          try {
            window.AkanaBus?.emit?.("llm:provider:changed", { provider: patch.provider });
          } catch (_e) {
            /* silent when bus is absent */
          }
        }
      } finally {
        _conversationLlmRestore = false;
      }
    } catch {
      /* ignore */
    }
  }

  function closeModelSwitcher() {
    if (!modelSwitcherOpen) return;
    modelSwitcherOpen = false;
    if (modelSwitcher) modelSwitcher.hidden = true;
    modelPill?.setAttribute("aria-expanded", "false");
    if (onModelSwitcherDocClick) {
      document.removeEventListener("pointerdown", onModelSwitcherDocClick, true);
      onModelSwitcherDocClick = null;
    }
    if (onModelSwitcherKey) {
      document.removeEventListener("keydown", onModelSwitcherKey, true);
      onModelSwitcherKey = null;
    }
  }

  function ensureModelSwitcher() {
    if (modelSwitcher && modelSwitcher.isConnected) return modelSwitcher;
    const host = modelPill?.parentElement || document.body;
    // Parent must be relative so the anchor can position itself (status-group).
    if (host && getComputedStyle(host).position === "static") {
      host.style.position = "relative";
    }
    modelSwitcher = document.createElement("div");
    modelSwitcher.className = "model-switcher";
    modelSwitcher.id = "model-switcher";
    modelSwitcher.setAttribute("role", "dialog");
    modelSwitcher.setAttribute("aria-label", t("settings.model.switcher_aria"));
    modelSwitcher.hidden = true;
    host.appendChild(modelSwitcher);
    return modelSwitcher;
  }

  function renderModelSwitcherError(msg) {
    if (!modelSwitcher) return;
    modelSwitcher.innerHTML = "";
    const p = document.createElement("p");
    p.className = "model-switcher-empty";
    p.textContent = msg || t("settings.model.unavailable");
    modelSwitcher.appendChild(p);
  }

  /** Apply a selection via PUT; on success the pill refreshes + popover closes.
   *  CRITICAL: saveLlmSettings(patch) sends the whole form (collectLlmSettings);
   *  if the settings panel was never opened those fields come back EMPTY/0 and the
   *  backend clamps chat_max_turns to 2 (_clamp_int(0)→2) and resets provider/model.
   *  The switcher therefore sends ONLY the changed field → other settings are
   *  preserved on the backend (_merge: empty field = base). Same endpoint contract. */
  async function applyModelChoice(patch) {
    if (modelSwitcherBusy) return;
    modelSwitcherBusy = true;
    modelSwitcher?.setAttribute("data-busy", "true");
    try {
      invalidateLlmSettingsLoads();
      const r = await fetch(`${baseUrl()}/api/v1/system/llm-settings`, {
        method: "PUT",
        headers: authHeaders(true),
        body: JSON.stringify({ settings: patch }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(formatSaveError(r.status, parseApiError(err, r.status)));
      }
      const j = await r.json();
      // Always update the form — even when the settings tab was never opened the HTML
      // select default (cursor) would flip the provider back on the next saveLlmSettings.
      fillLlmForm(j.settings || j, j);
      llmPaneHydrated = true;
      await loadModelPill(); // refresh header pill + thinking-provider lock
      // If the provider changed, DON'T close the popover → show the new provider's
      // models immediately (live list for ollama, no reopen needed). Model selection closes it.
      if (patch && patch.provider) {
        paintModelSwitcher(j);
        // Provider changed → refresh voice Live capability (provider_is_gemini):
        // switching to gemini makes the "Live" toggle visible/active immediately (no F5).
        try {
          window.AkanaBus?.emit?.("llm:provider:changed", { provider: patch.provider });
        } catch (_e) {
          /* silent when bus is absent */
        }
      } else closeModelSwitcher();
      const convId = window.AkanaChat?.conversationIdForMemory?.();
      if (convId) void persistConversationLlm(convId, patch);
    } catch (e) {
      renderModelSwitcherError(t("settings.model.cannot_change", { error: e.message || e }));
    } finally {
      modelSwitcherBusy = false;
      modelSwitcher?.removeAttribute("data-busy");
    }
  }

  // Bilingual provider label/badge: resolve from the i18n catalog by provider
  // value, falling back to the backend-sent label/badge (English-first source).
  function provLabel(p) {
    const k = `settings.model.prov_${p.value}`;
    return window.AkanaI18n?.DICT?.[k] ? t(k) : p.label || p.value;
  }
  function provBadge(p) {
    if (!p.badge) return "";
    return window.AkanaI18n?.DICT?.["settings.model.prov_badge_dev"]
      ? t("settings.model.prov_badge_dev")
      : p.badge;
  }

  /** Paint popover from GET schema: provider selection + active provider's
   *  model list (radio). Selection is PUT-ted immediately. */
  function paintModelSwitcher(data) {
    if (!modelSwitcher) return;
    const s = (data && data.settings) || data || {};
    const provider = String(s.provider || data.active_provider || "cursor").trim() || "cursor";
    const providers =
      Array.isArray(data.providers) && data.providers.length
        ? data.providers
        : [
            { value: "cursor", label: "Cursor" },
            { value: "claude", label: "Claude" },
          ];
    const cursorModels =
      Array.isArray(data.cursor_models) && data.cursor_models.length ? data.cursor_models : [];
    const claudeModels =
      Array.isArray(data.claude_models) && data.claude_models.length ? data.claude_models : [];
    const activeCursor = String(s.cursor_model || data.active_cursor_model_tag || "").trim();
    const activeClaude = String(s.claude_model || data.active_claude_model_tag || "").trim();

    modelSwitcher.innerHTML = "";

    // Provider row (segment).
    const provRow = document.createElement("div");
    provRow.className = "model-switcher-providers";
    provRow.setAttribute("role", "group");
    provRow.setAttribute("aria-label", t("settings.model.provider_group_aria"));
    for (const p of providers) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "model-switcher-prov";
      b.textContent = provLabel(p);
      // Mark "work-in-progress" providers (gemini/ollama/openai) with a small
      // beta chip; full text in tooltip. Source: provider.badge field.
      if (p.badge) {
        const beta = document.createElement("span");
        beta.className = "model-switcher-prov-beta";
        beta.textContent = "beta";
        beta.title = provBadge(p);
        b.appendChild(beta);
      }
      b.dataset.provider = p.value;
      const on = p.value === provider;
      b.classList.toggle("is-active", on);
      b.setAttribute("aria-pressed", on ? "true" : "false");
      b.addEventListener("click", () => {
        if (p.value !== provider) void applyModelChoice({ provider: p.value });
      });
      provRow.appendChild(b);
    }
    modelSwitcher.appendChild(provRow);

    // Active provider's models.
    const list = document.createElement("div");
    list.className = "model-switcher-list";
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", t("settings.model.list_aria"));
    modelSwitcher.appendChild(list);

    if (provider === "ollama") {
      // Ollama models are LIVE (installed ones; not in static payload) → fetch on-demand.
      const activeOllama = String(s.ollama_model || data.active_ollama_model_tag || "").trim();
      void _loadOllamaModels(list, activeOllama);
      return;
    }
    if (provider === "gemini") {
      // Gemini models are LIVE (/system/gemini/models — real list from Google API,
      // like cursor/claude). Falls back to static catalog when SDK/key is missing.
      // field=gemini_model. Previously this branch was absent → gemini fell to
      // "unknown → Claude list" and PUT the wrong models + claude_model (bug).
      const activeGemini = String(s.gemini_model || data.active_gemini_model_tag || "").trim();
      void _loadGeminiModels(list, activeGemini);
      return;
    }
    if (provider === "openai") {
      // OpenAI models are LIVE (/system/openai/models — real list from OpenAI API,
      // like gemini). Falls back to static catalog when SDK/key is missing.
      // field=openai_model. Without this branch openai fell to "unknown → Claude list"
      // and PUT the wrong models + claude_model (bug).
      const activeOpenai = String(s.openai_model || data.active_openai_model_tag || "").trim();
      void _loadOpenaiModels(list, activeOpenai);
      return;
    }
    if (provider === "cursor") {
      void _loadCursorModels(list, activeCursor);
      return;
    }
    if (provider === "claude") {
      void _loadClaudeModels(list, activeClaude);
      return;
    }
    // Unknown provider → static Claude list (last resort).
    const models = claudeModels;
    const activeTag = activeClaude;
    const field = "claude_model";
    _renderModelOptions(list, models, activeTag, field);
  }

  /** Paint model radio options into a list; selection immediately PUTs {field: value}. */
  function _renderModelOptions(list, models, activeTag, field) {
    if (!models.length) {
      const empty = document.createElement("p");
      empty.className = "model-switcher-empty";
      empty.textContent = activeTag
        ? t("settings.model.active_no_list", { tag: activeTag })
        : t("settings.model.no_list");
      list.appendChild(empty);
      return;
    }
    for (const m of models) {
      const opt = document.createElement("button");
      opt.type = "button";
      opt.className = "model-switcher-opt";
      opt.setAttribute("role", "option");
      const on = m.value === activeTag;
      opt.classList.toggle("is-active", on);
      opt.setAttribute("aria-selected", on ? "true" : "false");
      const name = document.createElement("span");
      name.className = "model-switcher-opt-name";
      name.textContent = m.label || m.value;
      opt.appendChild(name);
      if (on) {
        const tick = document.createElement("span");
        tick.className = "model-switcher-opt-tick";
        tick.setAttribute("aria-hidden", "true");
        tick.textContent = "✓";
        opt.appendChild(tick);
      }
      opt.addEventListener("click", () => {
        if (m.value !== activeTag) void applyModelChoice({ [field]: m.value });
      });
      list.appendChild(opt);
    }
  }

  // ─── Cached-catalog model loaders (cursor / claude / gemini / openai) ────────
  // These four providers share the same shape: a TTL'd client cache in front of
  // the per-provider /system/{provider}/models endpoint, painted into the model
  // switcher. They differ only in the cache slot, the endpoint segment, the i18n
  // key prefix, and the paint style — captured below as `cfg`. Ollama is separate
  // (LIVE only, no cache) and keeps its own hand-written loader.
  //
  // Paint style is one of:
  //   "fallback" (cursor/claude) — unreachable shows an error line, then keeps the
  //               static fallback list selectable; reachable renders + empty hint.
  //   "always"   (gemini/openai) — unreachable prepends a note but always renders
  //               the (fallback-carrying) model list; no empty hint.
  function _paintCatalogModelsList(list, j, activeTag, cfg) {
    list.innerHTML = "";
    if (cfg.style === "fallback") {
      if (!j || !j.reachable) {
        const err = (j && j.error) || t(`settings.model.${cfg.prefix}_unreachable_default`);
        const empty = document.createElement("p");
        empty.className = "model-switcher-empty";
        empty.textContent = t(`settings.model.${cfg.prefix}_unreachable`, { error: err });
        list.appendChild(empty);
        // Static fallback list remains selectable (offline usage when unreachable).
        if (j && Array.isArray(j.models) && j.models.length) {
          _renderModelOptions(list, j.models, String(activeTag || j.active || ""), cfg.field);
        }
        return;
      }
      const models = j.models || [];
      _renderModelOptions(list, models, String(activeTag || j.active || ""), cfg.field);
      if (!models.length) {
        const hint = document.createElement("p");
        hint.className = "model-switcher-empty";
        hint.textContent = t(`settings.model.${cfg.prefix}_empty`);
        list.appendChild(hint);
      }
      return;
    }
    // style === "always"
    if (j && !j.reachable) {
      const note = document.createElement("p");
      note.className = "model-switcher-empty";
      note.textContent = t(`settings.model.${cfg.prefix}_unreachable_fallback`, {
        error: (j && j.error) || t(`settings.model.${cfg.prefix}_unreachable_default`),
      });
      list.appendChild(note);
    }
    const models = j && Array.isArray(j.models) ? j.models : [];
    _renderModelOptions(list, models, String(activeTag || (j && j.active) || ""), cfg.field);
  }

  // Build a load fn for a cached-catalog provider. `cache` is a {get,set} pair over
  // the module-level *ModelsCache slot (kept as distinct lets so the key-change
  // invalidation at save time can null them individually).
  function _makeCatalogModelLoader(cfg) {
    return async function (list, activeTag, { force = false } = {}) {
      const now = Date.now();
      const cached = cfg.cache.get();
      if (!force && cached && now - cached.at < CURSOR_MODELS_CACHE_TTL_MS) {
        _paintCatalogModelsList(list, cached.payload, activeTag, cfg);
        return;
      }
      if (!cfg.cache.get()) {
        list.innerHTML = `<p class="model-switcher-empty">${t(`settings.model.${cfg.prefix}_loading`)}</p>`;
      }
      try {
        const url = `${baseUrl()}/api/v1/system/${cfg.endpoint}/models${force ? "?refresh=1" : ""}`;
        const r = await fetch(url, { headers: authHeaders() });
        if (!modelSwitcherOpen) return;
        const j = r.ok ? await r.json() : null;
        if (j) cfg.cache.set({ at: Date.now(), payload: j });
        _paintCatalogModelsList(list, j, activeTag, cfg);
      } catch (e) {
        if (!modelSwitcherOpen) return;
        const stale = cfg.cache.get();
        if (stale) {
          _paintCatalogModelsList(list, stale.payload, activeTag, cfg);
          return;
        }
        list.innerHTML = "";
        const empty = document.createElement("p");
        empty.className = "model-switcher-empty";
        empty.textContent = t(`settings.model.${cfg.prefix}_failed`, { error: e.message || e });
        list.appendChild(empty);
      }
    };
  }

  /** Cursor: fetch account models (/system/cursor/models) → dropdown.
   *  Server + client cache — Node bridge does not run on every open. */
  const _loadCursorModels = _makeCatalogModelLoader({
    endpoint: "cursor",
    prefix: "cursor",
    field: "cursor_model",
    style: "fallback",
    cache: { get: () => cursorModelsCache, set: (v) => { cursorModelsCache = v; } },
  });

  /** Claude: fetch subscription models (/system/claude/models) → dropdown.
   *  Live `/v1/models` (OAuth token); server + client cache (symmetric with
   *  Cursor) — API is not hit on every open. */
  const _loadClaudeModels = _makeCatalogModelLoader({
    endpoint: "claude",
    prefix: "claude",
    field: "claude_model",
    style: "fallback",
    cache: { get: () => claudeModelsCache, set: (v) => { claudeModelsCache = v; } },
  });

  /** Ollama: fetch installed models LIVE (/system/ollama/models) → dropdown.
   *  If daemon is down shows a clear "Ollama not running" message (not a fake empty list). */
  async function _loadOllamaModels(list, activeTag) {
    list.innerHTML = `<p class="model-switcher-empty">${t("settings.model.ollama_loading")}</p>`;
    try {
      const r = await fetch(`${baseUrl()}/api/v1/system/ollama/models`, {
        headers: authHeaders(),
      });
      if (!modelSwitcherOpen) return; // closed in the meantime
      const j = r.ok ? await r.json() : null;
      list.innerHTML = "";
      if (!j || !j.reachable) {
        const url = (j && j.url) || "http://localhost:11434";
        const empty = document.createElement("p");
        empty.className = "model-switcher-empty";
        empty.textContent = t("settings.model.ollama_down", { url });
        list.appendChild(empty);
        return;
      }
      const models = (j.models || []).map((m) => ({ value: m, label: m }));
      _renderModelOptions(list, models, String(activeTag || j.active || ""), "ollama_model");
      if (!models.length) {
        const hint = document.createElement("p");
        hint.className = "model-switcher-empty";
        hint.textContent = t("settings.model.ollama_empty");
        list.appendChild(hint);
      }
    } catch (e) {
      if (!modelSwitcherOpen) return;
      list.innerHTML = "";
      const empty = document.createElement("p");
      empty.className = "model-switcher-empty";
      empty.textContent = t("settings.model.ollama_failed", { error: e.message || e });
      list.appendChild(empty);
    }
  }

  /** Gemini: fetch account models LIVE (/system/gemini/models) → dropdown.
   *  Server + client cache (symmetric with Cursor/Claude). Painted in "always"
   *  style: reachable=false still carries fallback models so options show. */
  const _loadGeminiModels = _makeCatalogModelLoader({
    endpoint: "gemini",
    prefix: "gemini",
    field: "gemini_model",
    style: "always",
    cache: { get: () => geminiModelsCache, set: (v) => { geminiModelsCache = v; } },
  });

  /** OpenAI: fetch account models LIVE (/system/openai/models) → dropdown.
   *  Server + client cache (symmetric with Cursor/Claude). Painted in "always"
   *  style: reachable=false still carries fallback models so options show. */
  const _loadOpenaiModels = _makeCatalogModelLoader({
    endpoint: "openai",
    prefix: "openai",
    field: "openai_model",
    style: "always",
    cache: { get: () => openaiModelsCache, set: (v) => { openaiModelsCache = v; } },
  });

  async function openModelSwitcher() {
    ensureModelSwitcher();
    modelSwitcherOpen = true;
    modelSwitcher.hidden = false;
    modelPill?.setAttribute("aria-expanded", "true");
    // Loading skeleton.
    modelSwitcher.innerHTML = `<p class="model-switcher-empty">${t("settings.model.loading")}</p>`;

    onModelSwitcherDocClick = (e) => {
      if (
        modelSwitcher &&
        !modelSwitcher.contains(e.target) &&
        modelPill &&
        !modelPill.contains(e.target)
      ) {
        closeModelSwitcher();
      }
    };
    onModelSwitcherKey = (e) => {
      if (e.key === "Escape") {
        e.stopPropagation();
        closeModelSwitcher();
        modelPill?.focus?.();
      }
    };
    document.addEventListener("pointerdown", onModelSwitcherDocClick, true);
    document.addEventListener("keydown", onModelSwitcherKey, true);

    try {
      const r = await fetch(`${baseUrl()}/api/v1/system/llm-settings`, {
        headers: authHeaders(),
      });
      if (!modelSwitcherOpen) return; // closed in the meantime
      if (!r.ok) {
        renderModelSwitcherError(t("settings.model.settings_unavailable"));
        return;
      }
      const j = await r.json();
      if (!modelSwitcherOpen) return;
      paintModelSwitcher(j);
    } catch (e) {
      if (modelSwitcherOpen) renderModelSwitcherError(t("settings.model.no_connection_ws", { error: e.message || e }));
    }
  }

  function toggleModelSwitcher() {
    if (modelSwitcherOpen) closeModelSwitcher();
    else void openModelSwitcher();
  }

  function wireModelSwitcher() {
    if (!modelPill) return;
    modelPill.setAttribute("role", "button");
    modelPill.setAttribute("tabindex", "0");
    modelPill.setAttribute("aria-haspopup", "dialog");
    modelPill.setAttribute("aria-expanded", "false");
    modelPill.classList.add("status-pill-interactive");
    if (!modelPill.title || modelPill.title === t("settings.stat.active_model")) {
      modelPill.title = t("settings.model.pill_title");
    }
    modelPill.addEventListener("click", (e) => {
      e.preventDefault();
      toggleModelSwitcher();
    });
    modelPill.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggleModelSwitcher();
      }
    });
  }

  const llmEls = {
    provider: document.getElementById("llm-provider"),
    model: document.getElementById("llm-model"),
    chatTurns: document.getElementById("llm-chat-turns"),
    status: document.getElementById("llm-save-status"),
    save: document.getElementById("llm-save"),
  };

  // Provider → the llm-settings field that stores its active model. The settings
  // PUT merges (single-field patches work), so sending only the active provider's
  // field never clobbers the others' saved models.
  const PROVIDER_MODEL_FIELD = {
    cursor: "cursor_model",
    claude: "claude_model",
    gemini: "gemini_model",
    openai: "openai_model",
    ollama: "ollama_model",
  };

  // Active model tag for a provider, read from the llm-settings payload (settings
  // value first, then the resolved active_*_model_tag the server reports).
  function activeModelTag(provider, s, meta) {
    const field = PROVIDER_MODEL_FIELD[provider];
    const fromSettings = field && s ? s[field] : "";
    const fromMeta = meta ? meta[`active_${provider}_model_tag`] : "";
    return String(fromSettings || fromMeta || "").trim();
  }

  let llmModelLoadGen = 0;
  // Populate #llm-model live for the chosen provider, reusing the per-provider
  // /system/{provider}/models endpoints (same source as the header model pill).
  // Each returns {reachable, models, active, error}; cursor/claude/gemini/openai
  // carry {value,label} models (with fallbacks even when unreachable), ollama
  // returns plain strings. Static meta.{cursor,claude}_models is the seed/last resort.
  async function fillModelSelectForProvider(provider, activeTag, meta) {
    const sel = llmEls.model;
    if (!sel) return;
    if (!PROVIDER_MODEL_FIELD[provider]) {
      sel.innerHTML = "";
      return;
    }
    const gen = ++llmModelLoadGen;
    const staticOpts =
      provider === "cursor"
        ? (meta && meta.cursor_models) || []
        : provider === "claude"
          ? (meta && meta.claude_models) || []
          : [];
    // Seed immediately so the box is never empty while the live list loads.
    if (staticOpts.length) {
      fillSelect(sel, staticOpts, activeTag, []);
    } else if (activeTag) {
      fillSelect(sel, [{ value: activeTag, label: activeTag }], activeTag, []);
    } else {
      sel.innerHTML = `<option value="">${t("settings.llm.model_loading")}</option>`;
    }
    try {
      const r = await fetch(`${baseUrl()}/api/v1/system/${provider}/models`, {
        headers: authHeaders(),
      });
      if (gen !== llmModelLoadGen) return; // provider changed mid-flight → stale
      const j = r.ok ? await r.json() : null;
      let models = j && Array.isArray(j.models) ? j.models : [];
      if (provider === "ollama") {
        models = models.map((m) => (typeof m === "string" ? { value: m, label: m } : m));
      }
      const cur = activeTag || (j && j.active) || "";
      fillSelect(sel, models, String(cur), staticOpts);
    } catch (_e) {
      if (gen !== llmModelLoadGen) return;
      // Keep the seeded options already shown (offline / unreachable).
    }
  }

  // ─── Runtime + Channels tabs — injected without touching index.html ─────────
  // Nav button and pane skeleton are built here (minimal footprint); form
  // fields are generated from the backend schema (GET /api/v1/settings/runtime).
  // Must run BEFORE the settingsTabs/settingsPanes queries.
  function injectDynamicSettingsChrome() {
    if (isMemoryStudioPage) return;
    const nav = document.querySelector(".settings-nav");
    const scroll = document.querySelector(".settings-scroll");
    if (!nav || !scroll || document.getElementById("settings-pane-runtime")) return;

    // i18n: dynamic tabs/panes carry data-i18n so they swap on language toggle;
    // t() seeds the initial text for the current language.
    const t = (k) => (window.AkanaI18n ? window.AkanaI18n.t(k) : k);
    const mkTab = (id, icon, labelKey, descKey, search) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "settings-tab";
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", "false");
      b.dataset.tab = id;
      b.dataset.search = search;
      b.innerHTML =
        `<span class="settings-tab-icon" aria-hidden="true">${icon}</span>` +
        `<span class="settings-tab-text"><span class="settings-tab-label" data-i18n="${labelKey}">${escapeHtml(t(labelKey))}</span>` +
        `<span class="settings-tab-desc" data-i18n="${descKey}">${escapeHtml(t(descKey))}</span></span>` +
        `<span class="settings-tab-badge" data-nav-badge="${id}" hidden></span>`;
      return b;
    };
    const mkPane = (id, titleKey, descKey, bodyHtml) => {
      const s = document.createElement("section");
      s.className = "settings-pane";
      s.id = `settings-pane-${id}`;
      s.dataset.pane = id;
      s.dataset.toolbarDescKey = descKey;
      s.setAttribute("role", "tabpanel");
      s.hidden = true;
      s.innerHTML =
        `<header class="settings-pane-header settings-pane-header-compact">` +
        `<h3 data-i18n="${titleKey}">${escapeHtml(t(titleKey))}</h3>` +
        `<p class="settings-pane-desc" data-i18n="${descKey}">${escapeHtml(t(descKey))}</p></header>` +
        bodyHtml;
      return s;
    };

    const link = nav.querySelector(".settings-tab-link");
    nav.insertBefore(
      mkTab(
        "runtime",
        '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="21" x2="14" y1="4" y2="4"/><line x1="10" x2="3" y1="4" y2="4"/><line x1="21" x2="12" y1="12" y2="12"/><line x1="8" x2="3" y1="12" y2="12"/><line x1="21" x2="16" y1="20" y2="20"/><line x1="12" x2="3" y1="20" y2="20"/><line x1="14" x2="14" y1="2" y2="6"/><line x1="8" x2="8" y1="10" y2="14"/><line x1="16" x2="16" y1="18" y2="22"/></svg>',
        "settings.nav.runtime",
        "settings.nav.runtime_desc",
        "çalışma zamanı runtime ayar bütçe arama zamanlayıcı skill planlayıcı bağlam dosya kök yükleme telegram limit eşik runtime settings budget search scheduler planner context file root upload limit threshold restart",
      ),
      link,
    );
    nav.insertBefore(
      mkTab(
        "connectors",
        '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" x2="15.42" y1="13.51" y2="17.49"/><line x1="15.41" x2="8.59" y1="6.51" y2="10.49"/></svg>',
        "settings.nav.connectors",
        "settings.nav.connectors_desc",
        "kanal connector telegram köprü bağlayıcı durum bot channels bridge status",
      ),
      link,
    );
    scroll.appendChild(
      mkPane(
        "runtime",
        "settings.runtime.title",
        "settings.runtime.desc",
        '<p class="hint settings-lead" id="runtime-status" role="status"></p>' +
          '<div id="runtime-form" class="runtime-form" aria-live="polite"></div>',
      ),
    );
    scroll.appendChild(
      mkPane(
        "connectors",
        "settings.connectors.title",
        "settings.connectors.desc",
        '<p class="hint settings-lead" id="connectors-status" role="status"></p>' +
          '<div id="telegram-panel" class="telegram-panel">' +
          '<div class="settings-block tg-card">' +
          // -- header: status dot + name + live state --
          '<div class="connector-card-head telegram-head">' +
          '<span class="connector-dot is-off" id="tg-dot"></span>' +
          '<span class="connector-card-id">Telegram</span>' +
          '<span class="connector-card-state" id="tg-state">' + escapeHtml(t("settings.conn.loading")) + "</span>" +
          "</div>" +
          '<label class="toggle" for="tg-enabled">' +
          '<input id="tg-enabled" type="checkbox" />' +
          '<span class="toggle-track" aria-hidden="true"></span>' +
          '<span class="toggle-label" data-i18n="settings.tg.enabled_label">' + escapeHtml(t("settings.tg.enabled_label")) + "</span>" +
          "</label>" +
          '<p class="field-hint" id="tg-status-line"></p>' +
          '<p class="connector-card-error" id="tg-last-error" hidden></p>' +
          // -- collapsible: step-by-step setup guide --
          '<details class="settings-advanced conn-help tg-guide">' +
          '<summary data-i18n="settings.tg.guide_title">' + escapeHtml(t("settings.tg.guide_title")) + "</summary>" +
          '<div class="conn-help-body" data-i18n-html="settings.tg.guide_html"></div>' +
          "</details>" +
          // -- section: bot token --
          '<div class="tg-section">' +
          '<h3 class="settings-block-title" data-i18n="settings.tg.token_title">' + escapeHtml(t("settings.tg.token_title")) + "</h3>" +
          '<p class="field-hint cred-state" id="tg-token-state"></p>' +
          '<div class="cred-reveal-row">' +
          '<button type="button" id="tg-token-reveal" class="btn-ghost btn-sm" data-i18n="settings.cred.reveal_btn" hidden>' + escapeHtml(t("settings.cred.reveal_btn")) + "</button>" +
          '<code class="cred-revealed" id="tg-token-revealed" hidden></code>' +
          "</div>" +
          '<label for="tg-token"><span data-i18n="settings.tg.token_label">' + escapeHtml(t("settings.tg.token_label")) + '</span> <span class="label-optional">@BotFather</span></label>' +
          '<div class="input-row">' +
          '<input id="tg-token" type="password" autocomplete="off" spellcheck="false" placeholder="123456:ABC-DEF…" />' +
          '<button type="button" id="tg-token-toggle" class="btn-ghost btn-sm" data-i18n-title="settings.cred.show_hide_title" title="' + escapeHtml(t("settings.cred.show_hide_title")) + '">👁</button>' +
          "</div>" +
          '<p class="field-hint" data-i18n="settings.tg.token_help">' + escapeHtml(t("settings.tg.token_help")) + "</p>" +
          '<div class="settings-row-actions">' +
          '<button type="button" id="tg-token-save" class="btn-primary" data-i18n="settings.tg.token_save">' + escapeHtml(t("settings.tg.token_save")) + "</button>" +
          '<button type="button" id="tg-token-clear" class="btn-ghost btn-sm" data-i18n="settings.tg.token_clear">' + escapeHtml(t("settings.tg.token_clear")) + "</button>" +
          '<button type="button" id="tg-test" class="btn-ghost btn-sm" data-i18n="settings.tg.test">' + escapeHtml(t("settings.tg.test")) + "</button>" +
          "</div>" +
          "</div>" +
          // -- section: allowed chats (scan/connect + manual entry) --
          '<div class="tg-section">' +
          '<h3 class="settings-block-title" data-i18n="settings.tg.chatids_title">' + escapeHtml(t("settings.tg.chatids_title")) + "</h3>" +
          '<p class="field-hint" data-i18n="settings.tg.scan_hint">' + escapeHtml(t("settings.tg.scan_hint")) + "</p>" +
          '<div class="settings-row-actions">' +
          '<button type="button" id="tg-scan" class="btn-ghost btn-sm" data-i18n="settings.tg.scan">' + escapeHtml(t("settings.tg.scan")) + "</button>" +
          "</div>" +
          '<div id="tg-scan-results" class="tg-scan-results" hidden></div>' +
          '<label for="tg-chatids"><span data-i18n="settings.tg.chatids_label">' + escapeHtml(t("settings.tg.chatids_label")) + '</span> <span class="label-optional" data-i18n="settings.tg.chatids_optional">' + escapeHtml(t("settings.tg.chatids_optional")) + "</span></label>" +
          '<div class="input-row inline-save-row" id="tg-chatids-row">' +
          '<input id="tg-chatids" type="text" autocomplete="off" spellcheck="false" placeholder="123456789, -1001234567890" />' +
          '<button type="button" id="tg-chatids-save" class="btn-primary btn-sm inline-save" data-i18n="settings.runtime.save_btn">' + escapeHtml(t("settings.runtime.save_btn")) + "</button>" +
          "</div>" +
          '<p class="field-hint" data-i18n="settings.tg.chatids_help">' + escapeHtml(t("settings.tg.chatids_help")) + "</p>" +
          "</div>" +
          // -- footer: refresh + transient panel status --
          '<div class="settings-row-actions tg-card-foot">' +
          '<button type="button" id="connectors-refresh" class="btn-ghost btn-sm" data-i18n="settings.connectors.refresh">' + escapeHtml(t("settings.connectors.refresh")) + "</button>" +
          '<p class="field-hint" id="tg-panel-status" role="status"></p>' +
          "</div>" +
          "</div>" +
          "</div>",
          // NOTE: the Tailscale remote-access card used to live here as a sibling of
          // #telegram-panel. It now lives as STATIC markup in the Connection pane
          // (web_ui/index.html #settings-pane-connection) so all remote-access UI is
          // in one place. loadTailscale() is triggered from switchSettingsTab()'s
          // "connection" branch, not from loadConnectors().
      ),
    );
  }
  injectDynamicSettingsChrome();

  const settingsTabs = document.querySelectorAll(".settings-tab");
  const settingsPanes = document.querySelectorAll(".settings-pane");

  // Resolved FRESH on each call (not cached at module-eval) so a live language
  // switch is reflected immediately — a cached object kept showing the old
  // language's tab names until a full page reload.
  const settingsTabLabels = () => ({
    overview: t("settings.tab.overview"),
    llm: t("settings.tab.llm"),
    credentials: t("settings.tab.credentials"),
    security: t("settings.tab.security"),
    runtime: t("settings.tab.runtime"),
    connectors: t("settings.tab.connectors"),
    packs: t("settings.tab.packs"),
    voice: t("settings.tab.voice"),
    appearance: t("settings.tab.appearance"),
    connection: t("settings.tab.connection"),
    vault: t("settings.nav.vault"),
    persona: t("settings.nav.persona"),
  });

  function setSettingsNavBadge(tabId, text, kind) {
    const badge = document.querySelector(`[data-nav-badge="${tabId}"]`);
    if (!badge) return;
    if (!text) {
      badge.hidden = true;
      badge.textContent = "";
      badge.classList.remove("is-ok", "is-warn", "is-bad");
      return;
    }
    badge.hidden = false;
    badge.textContent = text;
    badge.classList.remove("is-ok", "is-warn", "is-bad");
    if (kind) badge.classList.add(kind);
  }

  function updateSettingsHero(meta, s) {
    const hm = document.getElementById("settings-hero-model");
    const hp = document.getElementById("settings-hero-profile");
    const rawHero = String(
      (meta && meta.active_provider) || (s && s.provider) || "cursor"
    ).toLowerCase();
    // All providers (including gemini) — old ternary only recognised claude/ollama
    // and fell back gemini to "cursor" (wrong model + "Cursor SDK" label).
    const HERO_LABELS = {
      cursor: "Cursor SDK",
      claude: "Claude CLI",
      ollama: "Ollama (local)",
      gemini: "Gemini (API)",
      openai: "OpenAI (API)",
    };
    const heroProvider = HERO_LABELS[rawHero] ? rawHero : "cursor";
    const HERO_TAGS = {
      cursor: (meta && meta.active_cursor_model_tag) || (s && s.cursor_model),
      claude: (meta && meta.active_claude_model_tag) || (s && s.claude_model),
      ollama: (meta && meta.active_ollama_model_tag) || (s && s.ollama_model),
      gemini: (meta && meta.active_gemini_model_tag) || (s && s.gemini_model),
      openai: (meta && meta.active_openai_model_tag) || (s && s.openai_model),
    };
    if (hm) hm.textContent = HERO_TAGS[heroProvider] || "—";
    if (hp) hp.textContent = HERO_LABELS[heroProvider];
  }

  function updateSettingsHealthStrip(okCount, warnCount, badCount) {
    const pill = document.getElementById("settings-health-overall");
    if (!pill) return;
    pill.classList.remove("is-ok", "is-warn", "is-bad");
    if (badCount > 0) {
      pill.textContent = t("settings.health.critical", { n: badCount });
      pill.classList.add("is-bad");
    } else if (warnCount > 0) {
      pill.textContent = t("settings.health.warning", { n: warnCount });
      pill.classList.add("is-warn");
    } else {
      pill.textContent = t("settings.health.ok", { n: okCount });
      pill.classList.add("is-ok");
    }
  }

  function wsStateLabel() {
    if (!ws) return t("settings.ws.state_closed");
    if (ws.readyState === WebSocket.OPEN) return t("settings.ws.state_connected");
    if (ws.readyState === WebSocket.CONNECTING) return t("settings.ws.state_connecting");
    return t("settings.ws.state_closed");
  }

  function updateConnectionEndpointCard() {
    const urlEl = document.getElementById("connection-effective-url");
    const metaEl = document.getElementById("connection-effective-meta");
    if (!urlEl) return;
    const url = baseUrl();
    urlEl.textContent = url;
    const token = (tokenInput && tokenInput.value.trim()) || "";
    if (metaEl) {
      metaEl.textContent = t("settings.conn.meta", {
        tokenState: token ? t("settings.conn.token_set") : t("settings.conn.token_unset"),
        wsState: wsStateLabel(),
      });
    }
  }

  function renderOverviewMeta(rows) {
    const dl = document.getElementById("settings-overview-meta");
    if (!dl) return;
    dl.innerHTML = "";
    for (const [k, v] of rows) {
      const dt = document.createElement("dt");
      dt.textContent = k;
      const dd = document.createElement("dd");
      dd.textContent = v;
      dl.appendChild(dt);
      dl.appendChild(dd);
    }
    dl.hidden = rows.length === 0;
  }

  function renderStatCard(label, value, ok, desc) {
    const card = document.createElement("div");
    card.className = `stat-card stat-card-rich${ok === false ? " stat-bad" : ok === true ? " stat-ok" : ""}`;
    const descHtml = desc ? `<span class="stat-card-desc">${escapeHtml(desc)}</span>` : "";
    card.innerHTML = `<span class="stat-card-label">${escapeHtml(label)}</span><span class="stat-card-value">${escapeHtml(value)}${descHtml}</span>`;
    return card;
  }

  async function loadSettingsOverview() {
    const grid = document.getElementById("settings-status-grid");
    if (!grid) return;
    grid.innerHTML = "";
    const loading = document.createElement("p");
    loading.className = "hint";
    loading.textContent = t("settings.stat.status_loading");
    grid.appendChild(loading);
    try {
      const r = await fetch(`${baseUrl()}/api/v1/system/status`, { headers: authHeaders() });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j = await r.json();
      grid.innerHTML = "";
      const deps = j.dependencies || {};
      const cursor = deps.cursor_api || {};
      const model = j.model || {};
      const wsOk = !!(ws && ws.readyState === WebSocket.OPEN);
      const cursorOk = !!cursor.reachable;
      let okN = 0;
      let warnN = 0;
      let badN = 0;
      const track = (ok) => {
        if (ok) okN += 1;
        else badN += 1;
      };
      const trackWarn = (ok) => {
        if (ok) okN += 1;
        else warnN += 1;
      };
      const active = activeModelInfo(j);
      const claudeTokenSet = !!(deps.claude_cli && deps.claude_cli.oauth_token_set);
      // Show credentials for the ACTIVE provider only — a "no key" from the wrong
      // provider must not create a false critical. Ollama is LOCAL → no key needed (non-critical).
      let authOk;
      let authTitle;
      let authVal;
      let authDesc;
      if (active.provider === "claude") {
        authOk = claudeTokenSet;
        authTitle = t("settings.stat.claude_auth");
        authVal = claudeTokenSet ? t("settings.stat.token_loaded") : t("settings.stat.token_missing");
        authDesc = t("settings.stat.claude_desc");
      } else if (active.provider === "ollama") {
        authOk = true;
        authTitle = t("settings.stat.ollama_auth");
        authVal = t("settings.stat.ollama_no_key");
        authDesc = t("settings.stat.ollama_desc");
      } else if (active.provider === "gemini") {
        // Reachability-aware (like cursor): a key that is SET but not reachable —
        // missing google-genai SDK, invalid key, or offline — must NOT read as a
        // green "loaded". Keying off key_set alone showed a false green when the SDK
        // was absent (chat can't work). The model panel surfaces deps.gemini_api.error.
        const geminiReachable = !!(deps.gemini_api && deps.gemini_api.reachable);
        authOk = geminiReachable;
        authTitle = t("settings.stat.gemini_auth");
        authVal = geminiReachable ? t("settings.stat.key_loaded") : t("settings.stat.key_missing");
        authDesc = t("settings.stat.gemini_desc");
      } else if (active.provider === "openai") {
        // Reachability-aware, same as gemini/cursor: a set-but-unreachable key
        // (invalid, or offline) is not a green "loaded".
        const openaiReachable = !!(deps.openai_api && deps.openai_api.reachable);
        authOk = openaiReachable;
        authTitle = t("settings.stat.openai_auth");
        authVal = openaiReachable ? t("settings.stat.key_loaded") : t("settings.stat.key_missing");
        authDesc = t("settings.stat.openai_desc");
      } else {
        authOk = cursorOk;
        authTitle = t("settings.stat.cursor_auth");
        authVal = cursorOk ? t("settings.stat.key_loaded") : t("settings.stat.key_missing");
        authDesc = t("settings.stat.cursor_desc");
      }
      grid.appendChild(renderStatCard(authTitle, authVal, authOk, authDesc));
      track(authOk);
      grid.appendChild(
        renderStatCard(
          t("settings.stat.active_model"),
          `${active.label} · ${active.tag}`,
          true,
          t("settings.stat.change_provider"),
        ),
      );
      okN += 1;
      grid.appendChild(
        renderStatCard(
          t("settings.stat.chat_history"),
          t("settings.hero.turns", { n: j.chat_max_turns || "?" }),
          true,
          t("settings.stat.chat_history_desc"),
        ),
      );
      okN += 1;
      grid.appendChild(
        renderStatCard(
          t("settings.stat.websocket"),
          wsStateLabel(),
          wsOk,
          t("settings.stat.ws_desc"),
        ),
      );
      trackWarn(wsOk);
      const srv = j.server || j.akana || {};
      renderOverviewMeta([
        [t("settings.meta.server"), `${srv.host || "?"}:${srv.port || "?"}`],
        [t("settings.meta.python"), j.python || "—"],
        [t("settings.meta.phase"), j.phase || "—"],
        [t("settings.meta.chat_path"), j.chat_path || "cursor"],
        [t("settings.meta.data"), j.data_dir || "—"],
      ]);
      updateSettingsHealthStrip(okN, warnN, badN);
      setSettingsNavBadge("overview", badN ? "!" : "✓", badN ? "is-bad" : "is-ok");
      setSettingsNavBadge("connection", authOk ? "OK" : "!", authOk ? "is-ok" : "is-bad");
      setSettingsNavBadge("llm", active.tag !== "?" ? "●" : "", active.tag !== "?" ? "is-ok" : "");
      updateSettingsHero(
        {
          active_cursor_model_tag: model.cursor_tag,
          active_claude_model_tag: model.claude_tag,
          active_ollama_model_tag: model.ollama_tag,
          active_gemini_model_tag: model.gemini_tag,
          active_openai_model_tag: model.openai_tag,
          active_provider: active.provider,
        },
        {
          cursor_model: model.cursor_tag,
          gemini_model: model.gemini_tag,
          openai_model: model.openai_tag,
          chat_max_turns: j.chat_max_turns,
          cursor_reachable: cursorOk,
          cursor_tag: model.cursor_tag,
          claude_token_set: claudeTokenSet,
        },
      );
      updateConnectionEndpointCard();
    } catch (e) {
      grid.innerHTML = "";
      const err = document.createElement("p");
      err.className = "hint";
      err.style.color = "var(--err)";
      err.textContent = t("settings.stat.status_failed", { error: e.message || e });
      grid.appendChild(err);
    }
  }

  // Breadcrumb + toolbar description for a tab, resolved through i18n and set
  // IMPERATIVELY. We drop the breadcrumb's static data-i18n so a live language
  // switch (which re-applies [data-i18n] across the whole document) can no longer
  // force it back to the default "General" while another tab is active.
  function applySettingsChrome(id) {
    const crumb = document.getElementById("settings-breadcrumb-current");
    if (crumb) {
      crumb.textContent = settingsTabLabels()[id] || id;
      crumb.removeAttribute("data-i18n");
    }
    const toolbarDesc = document.getElementById("settings-toolbar-desc");
    const pane = document.getElementById(`settings-pane-${id}`);
    if (toolbarDesc && pane) {
      // Resolve the toolbar description through i18n (bilingual, re-evaluated on
      // each tab switch so it follows the language picker). Static panes carry
      // `data-toolbar-desc-key`; dynamic panes (mkPane) set the same dataset key.
      const descKey = pane.dataset.toolbarDescKey;
      const desc =
        descKey && window.AkanaI18n
          ? window.AkanaI18n.t(descKey)
          : pane.dataset.toolbarDesc;
      if (desc) toolbarDesc.textContent = desc;
    }
  }

  // On a live language switch, re-assert the ACTIVE tab's chrome in the new
  // language (labels are now resolved fresh, and the breadcrumb no longer carries
  // data-i18n so apply(document) alone would leave it stale/wrong). Optional-chained
  // so the module still loads in the DOM-less contract harness (node-vm window stub).
  window.addEventListener?.("akana:languagechange", () => {
    const activeId = document.body.getAttribute("data-settings-tab");
    if (activeId) applySettingsChrome(activeId);
  });

  function switchSettingsTab(tabId, { scrollMain = true } = {}) {
    const id = tabId || "overview";
    applySettingsChrome(id);
    settingsTabs.forEach((tab) => {
      const on = tab.dataset.tab === id;
      tab.classList.toggle("is-active", on);
      tab.setAttribute("aria-selected", on ? "true" : "false");
      if (on) {
        tab.scrollIntoView({ inline: "nearest", block: "nearest", behavior: "smooth" });
      }
    });
    settingsPanes.forEach((pane) => {
      const on = pane.dataset.pane === id;
      pane.classList.toggle("is-active", on);
      pane.hidden = !on;
    });
    document.body.setAttribute("data-settings-tab", id);
    const scrollEl = document.querySelector(".settings-scroll");
    if (scrollEl) scrollEl.scrollTop = 0;
    if (id === "overview") void loadSettingsOverview();
    else if (id === "llm" && !llmPaneHydrated) {
      llmPaneHydrated = true;
      void loadLlmSettings();
    }
    else if (id === "connection") { updateConnectionEndpointCard(); void loadTailscale(); }
    else if (id === "appearance") syncThemePickerUi();
    else if (id === "credentials") void loadCredentials();
    else if (id === "runtime") void loadRuntimeSettings();
    else if (id === "connectors") void loadConnectors();
  }
  const STATUS_CLEAR_MS = 2600;
  const statusClearTimers = new WeakMap();
  // Writes status text; temporary confirmations (e.g. "Saved") auto-clear after a delay.
  // Persistent info (e.g. "Source: …") and errors stay.
  function flashStatus(el, msg, color, autoClear) {
    if (!el) return;
    const prev = statusClearTimers.get(el);
    if (prev) {
      clearTimeout(prev);
      statusClearTimers.delete(el);
    }
    el.textContent = msg || "";
    el.style.color = color || "";
    if (msg && autoClear) {
      statusClearTimers.set(
        el,
        setTimeout(() => {
          el.textContent = "";
          el.style.color = "";
          statusClearTimers.delete(el);
        }, STATUS_CLEAR_MS),
      );
    }
  }
  function setLlmStatus(msg, isErr) {
    flashStatus(llmEls.status, msg, isErr ? "var(--err)" : "", !isErr);
  }

  function fillSelect(el, options, current, fallbackOptions) {
    if (!el) return;
    const opts = Array.isArray(options) && options.length ? options : fallbackOptions || [];
    el.innerHTML = "";
    let matched = false;
    for (const opt of opts) {
      const o = document.createElement("option");
      o.value = opt.value;
      o.textContent = opt.label || opt.value;
      if (opt.value === current) matched = true;
      el.appendChild(o);
    }
    if (!matched && current) {
      const o = document.createElement("option");
      o.value = current;
      o.textContent = t("settings.llm.custom_option", { value: current });
      el.appendChild(o);
    }
    if (current) el.value = current;
  }

  function fillLlmForm(s, meta) {
    if (!s) return;
    if (llmEls.chatTurns) {
      llmEls.chatTurns.value = String(s.chat_max_turns ?? 12);
      const turnsRow = document.getElementById("llm-chat-turns-row");
      if (turnsRow) turnsRow.classList.remove("is-dirty"); // saved/loaded → hide Save
    }

    const provider = (s.provider || (meta && meta.active_provider) || "cursor").trim() || "cursor";
    // <select> cannot hold a separate badge span → append the "work-in-progress" label
    // into the option text (map to new objects to avoid mutating the original).
    const providerOpts = ((meta && meta.providers) || []).map((p) => {
      const label = provLabel(p);
      const badge = provBadge(p);
      return { ...p, label: badge ? `${label} — ${badge}` : label };
    });
    fillSelect(llmEls.provider, providerOpts, provider, [
      { value: "cursor", label: "Cursor (API)" },
      { value: "claude", label: "Claude CLI (subscription)" },
    ]);
    const activeTag = activeModelTag(provider, s, meta);
    void fillModelSelectForProvider(provider, activeTag, meta);

    if (activeTag && modelProfileHint) {
      const provObj =
        ((meta && meta.providers) || []).find((p) => p.value === provider) || { value: provider };
      modelProfileHint.textContent = t("settings.model.active_short", {
        provider: provLabel(provObj),
        tag: activeTag,
      });
    }
    updateSettingsHero(meta, s);
    // The <select> now reflects the SERVER's provider (or the display fallback),
    // not a user choice — so a subsequent unrelated save must not echo it back.
    providerTouched = false;
  }

  function collectLlmSettings() {
    const provider = (llmEls.provider && llmEls.provider.value.trim()) || "";
    const out = {
      chat_max_turns: readNumInput(llmEls.chatTurns, 12),
    };
    // Echo `provider` ONLY when the user actually changed the provider control.
    // fillLlmForm paints a "cursor" fallback into the <select> even when the
    // persisted provider is "" (deliberate "follow env / not configured" state);
    // echoing that on an unrelated save (e.g. editing chat_max_turns) would
    // hard-pin the provider and silently defeat the refuse-until-configured guard.
    if (providerTouched && provider) out.provider = provider;
    // Only the active provider's model field — the PUT merges, so the others'
    // saved models are preserved. The field is keyed off the DISPLAYED provider
    // even when `provider` itself is not sent.
    const field = PROVIDER_MODEL_FIELD[provider];
    const model = (llmEls.model && llmEls.model.value.trim()) || "";
    if (field && model) out[field] = model;
    return out;
  }

  async function saveLlmSettings(patch) {
    invalidateLlmSettingsLoads();
    const safePatch = patch && typeof patch === "object" && !Array.isArray(patch) ? patch : {};
    const settingsBody = llmPaneHydrated
      ? { ...collectLlmSettings(), ...safePatch }
      : { ...safePatch };
    const body = { settings: settingsBody };
    const r = await fetch(`${baseUrl()}/api/v1/system/llm-settings`, {
      method: "PUT",
      headers: authHeaders(true),
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(formatSaveError(r.status, parseApiError(err, r.status)));
    }
    const j = await r.json();
    fillLlmForm(j.settings || j, j);
    llmPaneHydrated = true;
    setLlmStatus(t("settings.llm.saved"), false);
    await loadModelPill();
    if (safePatch.provider) {
      // Provider changed → refresh voice Live capability (provider_is_gemini),
      // same contract as the header pill's applyModelChoice() (see there for why).
      try {
        window.AkanaBus?.emit?.("llm:provider:changed", { provider: safePatch.provider });
      } catch (_e) {
        /* silent when bus is absent */
      }
    }
    return j;
  }

  async function loadLlmSettings() {
    if (!llmEls.save && !settingsTabs.length) return;
    const gen = ++llmSettingsLoadGen;
    try {
      const r = await fetch(`${baseUrl()}/api/v1/system/llm-settings`, {
        headers: authHeaders(),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(formatSaveError(r.status, parseApiError(err, r.status)));
      }
      const j = await r.json();
      if (gen !== llmSettingsLoadGen) return;
      fillLlmForm(j.settings || j, j);
    } catch (e) {
      if (gen !== llmSettingsLoadGen) return;
      llmPaneHydrated = false;
      setLlmStatus(t("settings.llm.load_failed", { error: e.message || e }), true);
    }
  }

  function resetWsReconnectBackoff() {
    wsReconnectAttempt = 0;
  }

  function scheduleWsReconnect() {
    clearTimeout(reconnectTimer);
    const delay = Math.min(
      WS_RECONNECT_BASE_MS * 2 ** wsReconnectAttempt,
      WS_RECONNECT_MAX_MS,
    );
    wsReconnectAttempt += 1;
    reconnectTimer = setTimeout(connectWs, delay);
  }

  function wsUrl() {
    const u = new URL(baseUrl());
    const proto = u.protocol === "https:" ? "wss:" : "ws:";
    const t = ((tokenInput && tokenInput.value) || localStorage.getItem(LS_TOKEN) || "").trim();
    const q = t ? `?token=${encodeURIComponent(t)}` : "";
    return `${proto}//${u.host}/ws/events${q}`;
  }

  // ─── Live events (/ws/events) — server broadcast → bus + minimal visible response ──
  // Server emits task_update / policy_update / reminder_fire
  // (see tasks/runner.py, policy/live.py, schedule/service.py). Each event is
  // forwarded to AkanaBus as `ws:<type>` (unknown types forwarded silently too);
  // a small set of status-affecting events are surfaced via toast.
  const WS_TASK_NOTIFY_STATUSES = {
    paused:    () => t("settings.ws.task_paused"),
    cancelled: () => t("settings.ws.task_cancelled"),
    aborted:   () => t("settings.ws.task_aborted"),
    failed:    () => t("settings.ws.task_failed"),
  };
  const _wsTaskNotified = new Map(); // task id → last notified status (prevents duplicate toasts)

  function handleWsEvent(raw) {
    let evt;
    try {
      evt = typeof raw === "string" ? JSON.parse(raw) : raw;
    } catch {
      return; // non-JSON frame — skip silently
    }
    if (!evt || typeof evt !== "object" || !evt.type) return;
    const type = String(evt.type);
    window.AkanaBus?.emit?.(`ws:${type}`, evt);
    if (type === "turn_active" || type === "turn_completed" || type === "queue_updated") {
      const cid = String(evt.conversation_id || "");
      const isCurrent = cid && window.AkanaChat?.conversationIdForMemory?.() === cid;
      if (cid && isCurrent) {
        if (type === "queue_updated") {
          window.AkanaChat?.setQueueDepth?.(evt.depth);
        } else if (type === "turn_completed") {
          void window.AkanaChat?.onTurnCompletedRemote?.(cid, evt);
        }
      } else if (type === "turn_completed" && cid) {
        void window.AkanaChat?.onBackgroundTurnCompleted?.(cid, evt);
      }
    } else if (type === "reminder_fire") {
      hooks.showToast(t("settings.ws.reminder_toast", { text: String(evt.text || "").trim() || "—" }), "info");
    } else if (type === "policy_update") {
      const p = evt.policy || {};
      if (p.decision === "deny" && p.enforced) {
        hooks.showToast(
          t("settings.ws.policy_blocked", {
            action: p.action_type || "action",
            rationale: p.rationale ? ` — ${p.rationale}` : "",
          }),
          "err",
        );
      }
    } else if (type === "task_update") {
      const tk = evt.task || {};
      const statusKey = String(tk.status || "");
      const labelFn = WS_TASK_NOTIFY_STATUSES[statusKey];
      const label = labelFn ? labelFn() : null;
      if (label && tk.id && _wsTaskNotified.get(tk.id) !== tk.status) {
        if (_wsTaskNotified.size > 200) _wsTaskNotified.clear();
        _wsTaskNotified.set(tk.id, tk.status);
        hooks.showToast(
          t("settings.ws.task_toast", { status: label, title: tk.title || tk.id }),
          tk.status === "paused" ? "info" : "err",
        );
      }
    }
    // plan_update: the chat surface already shows the plan text — bus is sufficient.
  }

  function connectWs(force) {
    if (!force && ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    if (ws) {
      try {
        // Detach listeners first: otherwise this stale socket's onclose fires and
        // calls scheduleWsReconnect() → an unnecessary reconnect queues on top of the
        // freshly created socket (see akana-voice-live.js _teardownTransport).
        ws.onopen = ws.onmessage = ws.onerror = ws.onclose = null;
        ws.close();
      } catch {
        /* ignore */
      }
    }
    setWsStatus(t("settings.ws.connecting_label"), "connecting");
    if (!hooks.voiceWakeActive() && !hooks.voiceMicRecording() && !hooks.voicePostInFlight())
      hooks.setOrb("idle");
    ws = new WebSocket(wsUrl());
    ws.onopen = () => {
      resetWsReconnectBackoff();
      setWsStatus(t("settings.ws.connected_label"), "connected");
      if (!hooks.voiceWakeActive() && !hooks.voiceMicRecording() && !hooks.voicePostInFlight())
        hooks.setOrb("ok");
      updateConnectionEndpointCard();
    };
    ws.onclose = () => {
      setWsStatus(t("settings.ws.closed_label"), "closed");
      if (!hooks.voiceWakeActive() && !hooks.voiceMicRecording() && !hooks.voicePostInFlight())
      hooks.setOrb("idle");
      updateConnectionEndpointCard();
      scheduleWsReconnect();
    };
    ws.onerror = () => {
      setWsStatus(t("settings.ws.error_label"), "error");
      hooks.setOrb("err");
    };
    ws.onmessage = (ev) => handleWsEvent(ev && ev.data);
  }
  // ─── Credentials (masked) ──────────────────────────────────────────────────
  const credEls = {
    cursorKey: document.getElementById("cred-cursor-key"),
    claudeToken: document.getElementById("cred-claude-token"),
    geminiKey: document.getElementById("cred-gemini-key"),
    openaiKey: document.getElementById("cred-openai-key"),
    cursorState: document.getElementById("cred-cursor-state"),
    claudeState: document.getElementById("cred-claude-state"),
    geminiState: document.getElementById("cred-gemini-state"),
    openaiState: document.getElementById("cred-openai-state"),
    cursorReveal: document.getElementById("btn-reveal-cred-cursor"),
    claudeReveal: document.getElementById("btn-reveal-cred-claude"),
    geminiReveal: document.getElementById("btn-reveal-cred-gemini"),
    openaiReveal: document.getElementById("btn-reveal-cred-openai"),
    cursorRevealed: document.getElementById("cred-cursor-revealed"),
    claudeRevealed: document.getElementById("cred-claude-revealed"),
    geminiRevealed: document.getElementById("cred-gemini-revealed"),
    openaiRevealed: document.getElementById("cred-openai-revealed"),
    save: document.getElementById("cred-save"),
    status: document.getElementById("cred-status"),
  };

  function setCredStatus(msg, kind) {
    flashStatus(
      credEls.status,
      msg,
      kind === "err" ? "var(--err)" : kind === "ok" ? "var(--ok)" : "",
      kind === "ok",
    );
  }

  function renderCredentialState(el, input, entry, labelKey, revealBtn, revealOut) {
    const isSet = !!(entry && entry.set);
    const label = t(labelKey);
    if (el) {
      // BUG 1: show WHICH layer the effective value came from (runtime store vs .env)
      // so "the single source of truth" is visible. ``source`` is "store" | "env" | null.
      let sourceSuffix = "";
      if (isSet && entry.source === "store") sourceSuffix = " · " + t("settings.cred.source_store");
      else if (isSet && entry.source === "env") sourceSuffix = " · " + t("settings.cred.source_env");
      el.textContent = isSet
        ? t("settings.cred.state_set", { hint: entry.hint || t("settings.cred.state_masked") }) + sourceSuffix
        : t("settings.cred.state_unset", { label });
      el.classList.toggle("is-set", isSet);
    }
    if (input) {
      input.placeholder = isSet ? t("settings.cred.placeholder_set", { hint: entry.hint || t("settings.cred.state_masked") }) : input.placeholder;
    }
    // Reveal affordance: button only for a set key; any reload drops a previously
    // revealed plaintext from the DOM (never persist a shown value across renders).
    if (revealBtn) {
      revealBtn.hidden = !isSet;
      revealBtn.textContent = t("settings.cred.reveal_btn");
    }
    if (revealOut) {
      revealOut.hidden = true;
      revealOut.textContent = "";
      revealOut.dataset.revealed = "0";
    }
  }

  function applyCredentialsPayload(j) {
    const creds = (j && j.credentials) || {};
    renderCredentialState(
      credEls.cursorState, credEls.cursorKey, creds.cursor_api_key, "settings.cred.cursor_label",
      credEls.cursorReveal, credEls.cursorRevealed,
    );
    renderCredentialState(
      credEls.claudeState, credEls.claudeToken, creds.claude_oauth_token, "settings.cred.claude_label",
      credEls.claudeReveal, credEls.claudeRevealed,
    );
    renderCredentialState(
      credEls.geminiState, credEls.geminiKey, creds.gemini_api_key, "settings.cred.gemini_label",
      credEls.geminiReveal, credEls.geminiRevealed,
    );
    renderCredentialState(
      credEls.openaiState, credEls.openaiKey, creds.openai_api_key, "settings.cred.openai_label",
      credEls.openaiReveal, credEls.openaiRevealed,
    );
    const anySet =
      creds.cursor_api_key?.set ||
      creds.claude_oauth_token?.set ||
      creds.gemini_api_key?.set ||
      creds.openai_api_key?.set;
    setSettingsNavBadge("credentials", anySet ? "✓" : "!", anySet ? "is-ok" : "is-warn");
  }

  async function loadCredentials() {
    if (!credEls.cursorState && !credEls.claudeState) return;
    try {
      const r = await fetch(`${baseUrl()}/api/v1/system/credentials`, {
        headers: authHeaders(),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      applyCredentialsPayload(await r.json());
      setCredStatus("");
    } catch (e) {
      if (credEls.cursorState) credEls.cursorState.textContent = t("settings.cred.status_unavailable");
      if (credEls.claudeState) credEls.claudeState.textContent = t("settings.cred.status_unavailable");
      setCredStatus(t("settings.cred.load_failed", { error: e.message || e }), "err");
    }
  }

  async function saveCredentials() {
    const patch = {};
    const key = credEls.cursorKey ? credEls.cursorKey.value.trim() : "";
    const token = credEls.claudeToken ? credEls.claudeToken.value.trim() : "";
    const geminiKey = credEls.geminiKey ? credEls.geminiKey.value.trim() : "";
    const openaiKey = credEls.openaiKey ? credEls.openaiKey.value.trim() : "";
    if (key) patch.cursor_api_key = key;
    if (token) patch.claude_oauth_token = token;
    if (geminiKey) patch.gemini_api_key = geminiKey;
    if (openaiKey) patch.openai_api_key = openaiKey;
    if (!Object.keys(patch).length) {
      setCredStatus(t("settings.cred.no_change"));
      return;
    }
    setCredStatus(t("settings.cred.saving"));
    try {
      const r = await fetch(`${baseUrl()}/api/v1/system/credentials`, {
        method: "PUT",
        headers: authHeaders(true),
        body: JSON.stringify(patch),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(parseApiError(err, r.status) || `HTTP ${r.status}`);
      }
      applyCredentialsPayload(await r.json());
      if (patch.cursor_api_key) cursorModelsCache = null;
      if (patch.claude_oauth_token) claudeModelsCache = null;
      if (patch.gemini_api_key) geminiModelsCache = null;  // new key → refresh live catalog
      if (patch.openai_api_key) openaiModelsCache = null;  // new key → refresh live catalog
      if (credEls.cursorKey) credEls.cursorKey.value = "";
      if (credEls.claudeToken) credEls.claudeToken.value = "";
      if (credEls.geminiKey) credEls.geminiKey.value = "";
      if (credEls.openaiKey) credEls.openaiKey.value = "";
      setCredStatus(t("settings.cred.saved"), "ok");
      hooks.showToast(t("settings.cred.saved_toast"), "success");
      void loadSettingsOverview();
    } catch (e) {
      setCredStatus(t("settings.cred.save_failed", { error: e.message || e }), "err");
    }
  }

  // ─── Runtime settings (schema → form; backend is the single source of truth) ──
  const RUNTIME_SOURCE_LABELS = {
    runtime: t("settings.runtime.source.setting"),
    env: t("settings.runtime.source.env"),
    default: t("settings.runtime.source.default"),
  };
  let runtimePayload = null;

  function runtimeInputDescriptor(item) {
    // Pure: derives input type + attributes from backend type (harness contract).
    if (item.type === "bool") return { kind: "checkbox", checked: item.value === true };
    // Enum setting (e.g. gemini_live_voice): fixed-option <select>. Regardless of type,
    // select wins when options are present (prevents invalid values from being typed).
    if (Array.isArray(item.options) && item.options.length) {
      return {
        kind: "select",
        value: item.value == null ? "" : String(item.value),
        options: item.options.map((o) => String(o)),
      };
    }
    if (item.type === "int" || item.type === "float") {
      return {
        kind: "number",
        step: item.type === "int" ? "1" : "any",
        min: item.min,
        max: item.max,
        value: item.value == null ? "" : String(item.value),
      };
    }
    const joiner = item.type === "paths" ? "; " : ", ";
    const v = Array.isArray(item.value) ? item.value.join(joiner) : item.value == null ? "" : String(item.value);
    return { kind: "text", value: v, placeholder: item.type === "paths" ? t("settings.runtime.paths_placeholder") : "" };
  }

  function buildRuntimeFormModel(payload) {
    // Pure function: GET /settings/runtime body → per-category form model.
    // Labels/descriptions/units/categories come from the backend schema (Turkish
    // source). Localize via the i18n dict (runtime.*) when a key exists, else fall
    // back to the API value — so the form follows the language toggle.
    const loc = (key, fallback) =>
      window.AkanaI18n && window.AkanaI18n.DICT && window.AkanaI18n.DICT[key]
        ? window.AkanaI18n.t(key)
        : fallback;
    const cats = Array.isArray(payload && payload.categories) ? payload.categories : [];
    const items = Array.isArray(payload && payload.settings) ? payload.settings : [];
    return cats
      .map((c) => ({
        id: String(c.id || ""),
        label: loc("runtime.cat." + (c.id || ""), String(c.label || c.id || "")),
        fields: items
          .filter((s) => s && s.category === c.id)
          .map((s) => ({
            key: String(s.key || ""),
            label: loc("runtime." + (s.key || "") + ".label", String(s.label || s.key || "")),
            description: loc("runtime." + (s.key || "") + ".desc", String(s.description || "")),
            type: String(s.type || "str"),
            unit: loc("runtime.unit." + (s.unit || ""), String(s.unit || "")),
            envVar: String(s.env_var || ""),
            source: String(s.source || "default"),
            sourceLabel: RUNTIME_SOURCE_LABELS[s.source] || String(s.source || "?"),
            restartRequired: !!s.restart_required,
            input: runtimeInputDescriptor(s),
          })),
      }))
      .filter((c) => c.fields.length > 0);
  }

  function setRuntimeStatus(msg, kind) {
    const el = document.getElementById("runtime-status");
    if (!el) return;
    el.textContent = msg || "";
    el.style.color = kind === "err" ? "var(--err)" : kind === "ok" ? "var(--ok)" : "";
  }

  function updateRuntimeNavBadge(payload) {
    const items = Array.isArray(payload && payload.settings) ? payload.settings : [];
    const n = items.filter((s) => s.source === "runtime").length;
    setSettingsNavBadge("runtime", n ? String(n) : "", n ? "is-ok" : "");
  }

  function readRuntimeInput(field, input) {
    if (field.input.kind === "checkbox") return { ok: true, value: !!input.checked };
    if (field.input.kind === "number") {
      const raw = String(input.value || "").trim();
      const n = Number(raw.replace(",", "."));
      if (raw === "" || !Number.isFinite(n)) {
        return { ok: false, error: t("settings.runtime.invalid_number", { label: field.label }) };
      }
      // Coerce/validate client-side to match the backend so a bad value doesn't
      // cost a 422 round trip: int fields reject fractions, and (when both bounds
      // are known) the schema min/max is enforced here too.
      if (field.type === "int" && !Number.isInteger(n)) {
        return { ok: false, error: t("settings.runtime.invalid_integer", { label: field.label }) };
      }
      const lo = field.input.min, hi = field.input.max;
      if (lo != null && hi != null && (n < lo || n > hi)) {
        return {
          ok: false,
          error: t("settings.runtime.out_of_range", { label: field.label, min: lo, max: hi }),
        };
      }
      return { ok: true, value: n };
    }
    if (field.type === "paths") {
      // Send an ARRAY, not a delimited string: the backend splits string input on
      // os.pathsep (';' on Windows, ':' on POSIX), which mis-parses ':'-joined paths
      // and Windows drive letters. Splitting here on ';'/newline/comma (never ':')
      // and handing the backend a list sidesteps the OS-dependent separator entirely.
      const parts = String(input.value || "")
        .split(/[;\n,]+/)
        .map((p) => p.trim())
        .filter(Boolean);
      return { ok: true, value: parts };
    }
    return { ok: true, value: String(input.value || "").trim() };
  }

  function renderRuntimeField(field) {
    const row = document.createElement("div");
    row.className = "runtime-field";
    row.dataset.key = field.key;

    const head = document.createElement("div");
    head.className = "runtime-field-head";
    const label = document.createElement("label");
    label.textContent = field.label;
    label.htmlFor = `rt-${field.key}`;
    head.appendChild(label);
    const srcBadge = document.createElement("span");
    srcBadge.className = `runtime-badge is-${field.source}`;
    srcBadge.textContent = field.sourceLabel;
    srcBadge.title = t("settings.runtime.badge_source", { source: field.sourceLabel, envPart: field.envVar ? t("settings.runtime.badge_env_part", { var: field.envVar }) : "" });
    head.appendChild(srcBadge);
    if (field.restartRequired) {
      const rb = document.createElement("span");
      rb.className = "runtime-badge is-restart";
      rb.textContent = t("settings.runtime.restart_badge");
      rb.title = t("settings.runtime.restart_title");
      head.appendChild(rb);
    }
    row.appendChild(head);

    const control = document.createElement("div");
    control.className = "runtime-field-control";
    // Enum (select) is a separate element; others use <input>. readRuntimeInput reads
    // a select like text (input.value = selected option).
    const input =
      field.input.kind === "select"
        ? document.createElement("select")
        : document.createElement("input");
    input.id = `rt-${field.key}`;
    if (field.input.kind === "select") {
      // Friendly labels for the language picker (value stays the lang code).
      const LABELS = field.key === "language" ? { en: "English", tr: "Türkçe" } : null;
      let matched = false;
      for (const opt of field.input.options) {
        const o = document.createElement("option");
        o.value = opt;
        o.textContent = (LABELS && LABELS[opt]) || opt;
        if (opt === field.input.value) { o.selected = true; matched = true; }
        input.appendChild(o);
      }
      // Resolved value is out of the schema's option list (e.g. an out-of-enum
      // value baked in from env): show it explicitly instead of letting the browser
      // silently select the FIRST option and misrepresent the real active value.
      if (!matched && field.input.value) {
        const o = document.createElement("option");
        o.value = field.input.value;
        o.textContent = t("settings.runtime.invalid_option", { value: field.input.value });
        o.selected = true;
        o.disabled = true;
        input.appendChild(o);
      }
    } else if (field.input.kind === "checkbox") {
      input.type = "checkbox";
      input.checked = field.input.checked;
    } else if (field.input.kind === "number") {
      input.type = "number";
      input.step = field.input.step;
      if (field.input.min != null) input.min = String(field.input.min);
      if (field.input.max != null) input.max = String(field.input.max);
      input.value = field.input.value;
    } else {
      input.type = "text";
      input.value = field.input.value;
      if (field.input.placeholder) input.placeholder = field.input.placeholder;
      input.spellcheck = false;
    }

    // Unified control model: booleans/selects apply instantly (no button); free
    // text/number commit via a single Save that only appears once the value is
    // dirty (Enter commits too). Keeps every row buttonless until you actually edit.
    const isInstant = field.input.kind === "checkbox" || field.input.kind === "select";
    if (field.input.kind === "checkbox") {
      // Boolean → the same gradient switch used everywhere else (Telegram, voice…).
      const sw = document.createElement("label");
      sw.className = "toggle runtime-switch";
      const track = document.createElement("span");
      track.className = "toggle-track";
      track.setAttribute("aria-hidden", "true");
      sw.appendChild(input);
      sw.appendChild(track);
      control.appendChild(sw);
    } else {
      control.appendChild(input);
    }
    if (field.unit) {
      const unit = document.createElement("span");
      unit.className = "runtime-field-unit";
      unit.textContent = field.unit;
      control.appendChild(unit);
    }
    if (isInstant) {
      input.addEventListener("change", () => void saveRuntimeField(field, input, row));
    } else {
      const save = document.createElement("button");
      save.type = "button";
      save.className = "btn-primary btn-sm runtime-save";
      save.textContent = t("settings.runtime.save_btn");
      save.addEventListener("click", () => void saveRuntimeField(field, input, row, save));
      input.addEventListener("input", () => row.classList.add("is-dirty"));
      input.addEventListener("change", () => row.classList.add("is-dirty"));
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          void saveRuntimeField(field, input, row, save);
        }
      });
      control.appendChild(save);
    }
    if (field.source === "runtime") {
      const reset = document.createElement("button");
      reset.type = "button";
      reset.className = "btn-ghost btn-sm runtime-reset-link";
      reset.textContent = t("settings.runtime.reset_btn");
      reset.title = t("settings.runtime.reset_title");
      reset.addEventListener("click", () => void resetRuntimeField(field, reset));
      control.appendChild(reset);
    }
    row.appendChild(control);

    const hint = document.createElement("p");
    hint.className = "field-hint";
    hint.textContent = field.envVar ? `${field.description} (env: ${field.envVar})` : field.description;
    row.appendChild(hint);

    const status = document.createElement("p");
    status.className = "runtime-field-status";
    status.setAttribute("role", "status");
    row.appendChild(status);
    return row;
  }

  function renderRuntimeForm(payload) {
    const formEl = document.getElementById("runtime-form");
    if (!formEl) return;
    runtimePayload = payload;
    updateRuntimeNavBadge(payload);
    const model = buildRuntimeFormModel(payload);
    // Preserve which categories the user expanded across a re-render — save/reset
    // re-renders the whole form, and without this manually-opened sections snap shut.
    const openCats = new Set(
      [...formEl.querySelectorAll("details.runtime-cat[open]")].map((d) => d.dataset.cat),
    );
    // Preserve UNSAVED edits in other fields too: a single-field save/reset rebuilds
    // the whole form from the server payload, which would otherwise silently discard
    // any value the user had typed (and marked dirty) but not yet saved.
    const dirtyValues = new Map();
    for (const r of formEl.querySelectorAll(".runtime-field.is-dirty")) {
      const inp = r.querySelector("input, select");
      if (r.dataset.key && inp) dirtyValues.set(r.dataset.key, inp.value);
    }
    formEl.innerHTML = "";
    for (const cat of model) {
      // Collapsible category (native <details>): the form reads as a tidy list of
      // section headers that expand on click — instead of one long wall of fields.
      const block = document.createElement("details");
      block.className = "settings-block runtime-cat";
      block.dataset.cat = cat.id;
      // Open only if the user explicitly expanded it this session (preserved
      // across save/reset re-renders); otherwise stay collapsed — details on click.
      block.open = openCats.has(cat.id);
      const summary = document.createElement("summary");
      summary.className = "runtime-cat-summary";
      const title = document.createElement("span");
      title.className = "runtime-cat-title";
      title.textContent = cat.label;
      summary.appendChild(title);
      const count = document.createElement("span");
      count.className = "runtime-cat-count";
      count.textContent = String(cat.fields.length);
      summary.appendChild(count);
      block.appendChild(summary);
      for (const field of cat.fields) block.appendChild(renderRuntimeField(field));
      formEl.appendChild(block);
    }
    // Re-apply the unsaved edits captured above onto the freshly rendered rows.
    for (const [key, val] of dirtyValues) {
      const r = formEl.querySelector(`.runtime-field[data-key="${CSS.escape(key)}"]`);
      const inp = r && r.querySelector("input, select");
      if (inp) {
        inp.value = val;
        r.classList.add("is-dirty");
      }
    }
  }

  async function loadRuntimeSettings() {
    const formEl = document.getElementById("runtime-form");
    if (!formEl) return;
    setRuntimeStatus(t("settings.runtime.loading"));
    try {
      const r = await fetch(`${baseUrl()}/api/v1/settings/runtime`, { headers: authHeaders() });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error(parseApiError(err, r.status) || `HTTP ${r.status}`);
      }
      renderRuntimeForm(await r.json());
      setRuntimeStatus(t("settings.runtime.source_legend"));
    } catch (e) {
      setRuntimeStatus(t("settings.runtime.load_failed", { error: e.message || e }), "err");
    }
  }

  function setRuntimeFieldStatus(row, msg, kind) {
    const el = row ? row.querySelector(".runtime-field-status") : null;
    if (!el) return;
    el.textContent = msg || "";
    el.style.color = kind === "err" ? "var(--err)" : kind === "ok" ? "var(--ok)" : "";
  }

  async function saveRuntimeField(field, input, row, btn) {
    const parsed = readRuntimeInput(field, input);
    if (!parsed.ok) {
      setRuntimeFieldStatus(row, parsed.error, "err");
      return;
    }
    if (btn) btn.disabled = true;
    setRuntimeFieldStatus(row, t("settings.runtime.saving"));
    try {
      const r = await fetch(`${baseUrl()}/api/v1/settings/runtime`, {
        method: "PUT",
        headers: authHeaders(true),
        body: JSON.stringify({ [field.key]: parsed.value }),
      });
      const body = await r.json().catch(() => ({}));
      if (!r.ok) {
        const fieldErr = body?.detail?.error?.fields?.[field.key];
        throw new Error(fieldErr || parseApiError(body, r.status) || `HTTP ${r.status}`);
      }
      row.classList.remove("is-dirty");
      // Language picker: swap the UI live (backend already saved → no re-PUT).
      if (field.key === "language") {
        window.AkanaI18n?.setLanguage(parsed.value, { backend: false });
      }
      const needsRestart = Array.isArray(body.restart_required) && body.restart_required.includes(field.key);
      hooks.showToast(
        needsRestart
          ? t("settings.runtime.saved_restart", { label: field.label })
          : t("settings.runtime.saved_live", { label: field.label }),
        needsRestart ? "info" : "success",
      );
      renderRuntimeForm(body);
    } catch (e) {
      const msg = t("settings.runtime.save_failed", { error: e.message || e });
      if (btn) {
        setRuntimeFieldStatus(row, msg, "err");
        btn.disabled = false;
      } else {
        // Instant control (switch/select) already flipped the UI optimistically —
        // a re-render would wipe the inline status, so toast the error and revert
        // the whole form to the last known-good payload to undo the flip.
        hooks.showToast(msg, "error");
        if (runtimePayload) renderRuntimeForm(runtimePayload);
      }
    }
  }

  async function resetRuntimeField(field, btn) {
    if (btn) btn.disabled = true;
    try {
      const r = await fetch(
        `${baseUrl()}/api/v1/settings/runtime/reset/${encodeURIComponent(field.key)}`,
        { method: "POST", headers: authHeaders(true), body: "{}" },
      );
      const body = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(parseApiError(body, r.status) || `HTTP ${r.status}`);
      // Language reset → swap UI to the value the reset landed on (env/default).
      if (field.key === "language") {
        const item = Array.isArray(body.settings)
          ? body.settings.find((s) => s && s.key === "language")
          : null;
        if (item) window.AkanaI18n?.setLanguage(item.value, { backend: false });
      }
      hooks.showToast(t("settings.runtime.reset_toast", { label: field.label }), "info");
      renderRuntimeForm(body);
    } catch (e) {
      setRuntimeStatus(t("settings.runtime.reset_failed", { error: e.message || e }), "err");
      if (btn) btn.disabled = false;
    }
  }

  // ─── Channels: live Telegram management — /api/v1/connectors/telegram ──────
  function tgPanelStatus(msg, kind) {
    flashStatus(
      document.getElementById("tg-panel-status"),
      msg,
      kind === "err" ? "var(--err)" : kind === "ok" ? "var(--ok)" : "",
      kind === "ok",
    );
  }

  function renderTelegramSnapshot(s) {
    s = s || {};
    const enabled = s.enabled === true;
    const running = s.running === true;
    const dot = document.getElementById("tg-dot");
    if (dot) dot.className = `connector-dot ${running ? "is-on" : "is-off"}`;
    const state = document.getElementById("tg-state");
    if (state) {
      state.textContent = running
        ? t("settings.conn.running")
        : enabled
          ? t("settings.conn.stopped")
          : t("settings.conn.disabled");
    }
    const toggle = document.getElementById("tg-enabled");
    if (toggle) toggle.checked = enabled;

    const ids = Array.isArray(s.allowed_chat_ids) ? s.allowed_chat_ids : [];
    const statusLine = document.getElementById("tg-status-line");
    if (statusLine) {
      const parts = [
        s.token_set
          ? t("settings.tg.token_state_set", { hint: s.token_hint || "" })
          : t("settings.tg.token_state_unset"),
        t("settings.tg.allowed_count", { count: ids.length }),
      ];
      if (s.last_message_at) parts.push(t("settings.tg.last_message", { at: s.last_message_at }));
      statusLine.textContent = parts.join(" · ");
    }
    const lastErr = document.getElementById("tg-last-error");
    if (lastErr) {
      if (s.last_error) {
        lastErr.textContent = t("settings.conn.last_error", { error: String(s.last_error) });
        lastErr.hidden = false;
      } else {
        lastErr.textContent = "";
        lastErr.hidden = true;
      }
    }
    const tokenState = document.getElementById("tg-token-state");
    if (tokenState) {
      tokenState.textContent = s.token_set
        ? t("settings.tg.token_state_set", { hint: s.token_hint || "" })
        : t("settings.tg.token_state_unset");
    }
    const reveal = document.getElementById("tg-token-reveal");
    if (reveal) reveal.hidden = !s.token_set;
    // Re-rendering drops any plaintext that was revealed (don't leave it in the DOM).
    const revealed = document.getElementById("tg-token-revealed");
    if (revealed && revealed.dataset.revealed === "1") {
      revealed.textContent = "";
      revealed.hidden = true;
      revealed.dataset.revealed = "0";
      if (reveal) reveal.textContent = t("settings.cred.reveal_btn");
    }
    // Don't clobber the field while the user is editing it.
    const chatids = document.getElementById("tg-chatids");
    if (chatids && document.activeElement !== chatids) {
      chatids.value = ids.join(", ");
      const chatRow = document.getElementById("tg-chatids-row");
      if (chatRow) chatRow.classList.remove("is-dirty"); // back in sync → hide Save
    }

    if (running) setSettingsNavBadge("connectors", "1", "is-ok");
    else if (enabled) setSettingsNavBadge("connectors", "!", "is-warn");
    else setSettingsNavBadge("connectors", "", "");
  }

  async function loadConnectors() {
    const lead = document.getElementById("connectors-status");
    if (!document.getElementById("telegram-panel")) return;
    if (lead) lead.textContent = t("settings.conn.loading");
    try {
      const r = await fetch(`${baseUrl()}/api/v1/connectors/telegram`, { headers: authHeaders() });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      renderTelegramSnapshot(await r.json());
      if (lead) lead.textContent = "";
    } catch (e) {
      if (lead) lead.textContent = t("settings.conn.load_failed", { error: e.message || e });
    }
  }

  // ── Tailscale remote-access card ───────────────────────────────────────────
  function tsReadToken() {
    try {
      return (localStorage.getItem(LS_TOKEN) || "").trim();
    } catch (e) {
      return "";
    }
  }

  let tsQrInstance = null;
  let tsQrUrl = "";

  // Render (or clear) the phone QR encoding the tailnet URL + the bearer token in
  // the URL hash — same scheme as akana-pair.js (index.html early-boot reads it
  // into localStorage and strips the fragment). Token is embedded, so ONLY the
  // vendored local QR lib is used; nothing is sent to a remote QR service.
  function tsRenderQr(httpsUrl) {
    const host = document.getElementById("ts-qr");
    const hint = document.getElementById("ts-qr-hint");
    if (!host) return;
    host.innerHTML = "";
    tsQrInstance = null;
    tsQrUrl = "";
    const token = tsReadToken();
    if (!httpsUrl) {
      host.hidden = true;
      if (hint) hint.hidden = true;
      return;
    }
    if (!token) {
      host.hidden = true;
      if (hint) {
        hint.hidden = false;
        hint.textContent = t("settings.ts.qr_no_token");
      }
      return;
    }
    const url = httpsUrl.replace(/\/+$/, "") + "/#token=" + encodeURIComponent(token);
    if (typeof window.QRCode !== "function") {
      host.hidden = true;
      if (hint) hint.hidden = true;
      return;
    }
    tsQrUrl = url;
    host.hidden = false;
    tsQrInstance = new window.QRCode(host, {
      text: url,
      width: 200,
      height: 200,
      colorDark: "#0b0f17",
      colorLight: "#ffffff",
      correctLevel: window.QRCode.CorrectLevel.M,
    });
    if (hint) {
      hint.hidden = false;
      hint.textContent = t("settings.ts.qr_hint");
    }
  }

  function tsSetStatus(msg, kind) {
    const el = document.getElementById("ts-status");
    if (!el) return;
    el.textContent = msg || "";
    el.style.color = kind === "err" ? "var(--err)" : kind === "ok" ? "var(--ok)" : "";
  }

  function renderTailscaleSnapshot(s) {
    s = s || {};
    const dot = document.getElementById("ts-dot");
    const state = document.getElementById("ts-state");
    const guidance = document.getElementById("ts-guidance");
    const installLink = document.getElementById("ts-install-link");
    const controls = document.getElementById("ts-controls");
    const urlRow = document.getElementById("ts-url-row");
    const urlEl = document.getElementById("ts-url");
    const modeSel = document.getElementById("ts-mode");
    const funnelWarn = document.getElementById("ts-funnel-warning");
    const funnelNeedsToken = document.getElementById("ts-funnel-needs-token");
    const funnelOpt = document.getElementById("ts-mode-funnel-opt");

    const installed = s.installed === true;
    const loggedIn = s.logged_in === true;
    const hasToken = !!tsReadToken();

    // Reset conditional regions.
    if (guidance) { guidance.hidden = true; guidance.textContent = ""; }
    if (installLink) installLink.hidden = true;
    if (controls) controls.hidden = true;
    if (urlRow) urlRow.hidden = true;
    if (funnelWarn) funnelWarn.hidden = true;
    if (funnelNeedsToken) funnelNeedsToken.hidden = true;
    tsRenderQr(null);

    // State machine: not installed -> logged out -> ready.
    let dotClass = "is-off";
    let stateText = t("settings.ts.state.not_installed");
    if (!installed) {
      if (guidance) { guidance.hidden = false; guidance.textContent = t("settings.ts.install_hint"); }
      if (installLink) {
        installLink.hidden = false;
        installLink.href = "https://tailscale.com/download";
      }
    } else if (!loggedIn) {
      stateText = t("settings.ts.state.logged_out");
      if (guidance) { guidance.hidden = false; guidance.textContent = t("settings.ts.login_hint"); }
    } else {
      // Logged in → show the mode selector + URL + QR.
      dotClass = "is-on";
      stateText = s.funnel_active
        ? t("settings.ts.state.funnel")
        : s.serve_active
          ? t("settings.ts.state.serving")
          : t("settings.ts.state.ready");
      if (controls) controls.hidden = false;
      // Funnel option is disabled without a token (the server also refuses it).
      if (funnelOpt) funnelOpt.disabled = !hasToken;
      if (!hasToken && funnelNeedsToken) funnelNeedsToken.hidden = false;
      const currentMode = s.funnel_active ? "funnel" : s.serve_active ? "serve" : "off";
      if (modeSel && document.activeElement !== modeSel) modeSel.value = currentMode;
      if (funnelWarn) funnelWarn.hidden = currentMode !== "funnel";
      if (s.https_url && urlRow && urlEl) {
        urlRow.hidden = false;
        urlEl.textContent = s.https_url;
      }
      if (s.https_url) tsRenderQr(s.https_url);
    }

    if (dot) dot.className = `connector-dot ${dotClass}`;
    if (state) state.textContent = stateText;
  }

  async function loadTailscale() {
    if (!document.getElementById("tailscale-panel")) return;
    try {
      const r = await fetch(`${baseUrl()}/api/v1/system/tailscale`, { headers: authHeaders() });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      renderTailscaleSnapshot(await r.json());
      tsSetStatus("", "");
    } catch (e) {
      tsSetStatus(t("settings.ts.load_failed", { error: e.message || e }), "err");
    }
  }

  async function tsSetMode(mode) {
    tsSetStatus(t("settings.ts.applying"), "");
    try {
      const r = await fetch(`${baseUrl()}/api/v1/system/tailscale/serve`, {
        method: "POST",
        headers: authHeaders(true),
        body: JSON.stringify({ mode }),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(parseApiError(j, r.status) || `HTTP ${r.status}`);
      renderTailscaleSnapshot(j);
      const modeLabel =
        mode === "funnel" ? t("settings.ts.mode_funnel")
        : mode === "serve" ? t("settings.ts.mode_serve")
        : t("settings.ts.mode_off");
      tsSetStatus(t("settings.ts.applied", { mode: modeLabel }), "ok");
    } catch (e) {
      tsSetStatus(t("settings.ts.apply_failed", { error: e.message || e }), "err");
      void loadTailscale();
    }
  }

  async function tsCopyUrl() {
    const urlEl = document.getElementById("ts-url");
    const url = urlEl ? urlEl.textContent : "";
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      tsSetStatus(t("settings.ts.url_copied"), "ok");
    } catch (e) {
      tsSetStatus(t("settings.ts.apply_failed", { error: e.message || e }), "err");
    }
  }

  // PUT a partial Telegram change; the server validates + persists + reloads the
  // connector LIVE (no restart) and returns the fresh snapshot. ``okKey`` is the
  // i18n key for the success line.
  async function tgUpdate(patch, okKey) {
    tgPanelStatus(t("settings.tg.saving"), "");
    try {
      const r = await fetch(`${baseUrl()}/api/v1/connectors/telegram`, {
        method: "PUT",
        headers: authHeaders(true),
        body: JSON.stringify(patch),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(parseApiError(j, r.status) || `HTTP ${r.status}`);
      renderTelegramSnapshot(j);
      tgPanelStatus(t(okKey), "ok");
      return j;
    } catch (e) {
      tgPanelStatus(t("settings.tg.save_failed", { error: e.message || e }), "err");
      // Re-sync the toggle/fields with the real server state after a failure.
      void loadConnectors();
      return null;
    }
  }

  async function tgTest() {
    tgPanelStatus(t("settings.tg.testing"), "");
    try {
      const r = await fetch(`${baseUrl()}/api/v1/connectors/telegram/test`, {
        method: "POST",
        headers: authHeaders(true),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(parseApiError(j, r.status) || `HTTP ${r.status}`);
      const bot = j.bot || {};
      tgPanelStatus(t("settings.tg.test_ok", { username: bot.username || "?", id: bot.id ?? "?" }), "ok");
    } catch (e) {
      tgPanelStatus(t("settings.tg.test_failed", { error: e.message || e }), "err");
    }
  }

  // Render the discover results as add-to-allowlist rows. Chat titles/usernames
  // come from external Telegram data, so they go in via textContent (never HTML).
  function renderScanResults(chats) {
    const box = document.getElementById("tg-scan-results");
    if (!box) return;
    box.hidden = false;
    box.textContent = "";
    if (!chats.length) {
      const empty = document.createElement("p");
      empty.className = "field-hint tg-scan-empty";
      empty.textContent = t("settings.tg.scan_empty");
      box.appendChild(empty);
      return;
    }
    for (const c of chats) {
      const row = document.createElement("div");
      row.className = "tg-chat-row";
      const meta = document.createElement("div");
      meta.className = "tg-chat-meta";
      const name = document.createElement("span");
      name.className = "tg-chat-name";
      name.textContent = c.title || c.username || c.id || "?";
      const sub = document.createElement("span");
      sub.className = "tg-chat-sub";
      const bits = [c.id];
      if (c.username) bits.push("@" + c.username);
      if (c.type) bits.push(c.type);
      sub.textContent = bits.filter(Boolean).join(" · ");
      meta.appendChild(name);
      meta.appendChild(sub);
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn-ghost btn-sm tg-chat-add";
      if (c.allowed) {
        btn.disabled = true;
        btn.textContent = t("settings.tg.already_allowed");
      } else {
        btn.textContent = t("settings.tg.add");
        btn.addEventListener("click", () => void tgAddChat(String(c.id)));
      }
      row.appendChild(meta);
      row.appendChild(btn);
      box.appendChild(row);
    }
  }

  // Ask the bot who has messaged it, so the user can one-click-allow a chat
  // instead of hunting for a numeric id. The server reads the live poll buffer
  // when the bridge is running, else does a one-shot non-consuming getUpdates.
  async function tgScan() {
    const box = document.getElementById("tg-scan-results");
    if (box) {
      box.hidden = false;
      box.textContent = "";
      const wait = document.createElement("p");
      wait.className = "field-hint";
      wait.textContent = t("settings.tg.scanning");
      box.appendChild(wait);
    }
    tgPanelStatus(t("settings.tg.scanning"), "");
    try {
      const r = await fetch(`${baseUrl()}/api/v1/connectors/telegram/discover`, {
        method: "POST",
        headers: authHeaders(true),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(parseApiError(j, r.status) || `HTTP ${r.status}`);
      const chats = Array.isArray(j.chats) ? j.chats : [];
      renderScanResults(chats);
      tgPanelStatus(t("settings.tg.scan_found", { count: chats.length }), "ok");
    } catch (e) {
      if (box) box.textContent = "";
      tgPanelStatus(t("settings.tg.scan_failed", { error: e.message || e }), "err");
    }
  }

  // Append a discovered chat id to the allowlist and persist it LIVE, then
  // re-scan so the row flips to "already allowed".
  async function tgAddChat(id) {
    const cid = String(id || "").trim();
    if (!cid) return;
    const input = document.getElementById("tg-chatids");
    const current = input
      ? input.value.split(",").map((s) => s.trim()).filter(Boolean)
      : [];
    if (current.includes(cid)) {
      tgPanelStatus(t("settings.tg.already_allowed"), "");
      return;
    }
    current.push(cid);
    const ok = await tgUpdate({ allowed_chat_ids: current.join(", ") }, "settings.tg.chatids_saved");
    if (ok) void tgScan();
  }

  function persistAndReconnect() {
    if (baseUrlInput) localStorage.setItem(LS_BASE, baseUrlInput.value.trim());
    if (tokenInput) localStorage.setItem(LS_TOKEN, tokenInput.value.trim());
    clearTimeout(reconnectTimer);
    resetWsReconnectBackoff();
    reconnectTimer = setTimeout(() => {
      loadHealth();
      connectWs(true);
    }, 250);
  }

  async function persistAllSettings() {
    persistAndReconnect();
    try {
      await window.AkanaVoice.persistVoiceSettings();
    } catch {
      /* local-only fallback */
    }
    flashStatus(
      document.getElementById("connection-save-status"),
      t("settings.save.saved"),
      "var(--ok)",
      true,
    );
  }

  // ─── Settings search — a command-palette over every pane ───────────────────
  // Harvests a live index of each setting block/field (with bilingual i18n text
  // so an English query matches a Turkish UI and vice-versa), renders a results
  // dropdown as the user types, and jumps to + flashes the setting on select.
  // Lazy panes (Runtime) are pre-warmed on first focus so their fields index too.
  const settingsSearchEls = {
    root: document.getElementById("settings-search"),
    input: document.getElementById("settings-search-input"),
    clear: document.getElementById("settings-search-clear"),
    results: document.getElementById("settings-search-results"),
  };
  let settingsSearchHits = [];
  let settingsSearchActiveIdx = -1;
  let settingsSearchPrewarmed = false;
  let settingsSearchAnchorSeq = 0;

  function cssEscapeAttr(v) {
    return String(v).replace(/["\\]/g, "\\$&");
  }

  // Both languages for a key, so search is language-agnostic regardless of UI.
  function settingsSearchAltStrings(key) {
    const dict = (window.AkanaI18n && window.AkanaI18n.DICT) || window.AkanaI18nStrings || {};
    const e = dict[key];
    if (!e) return "";
    return `${e.en || ""} ${e.tr || ""}`;
  }

  function settingsSearchHaystack(el) {
    const parts = [el.textContent || ""];
    el.querySelectorAll(
      "[data-i18n],[data-i18n-placeholder],[data-i18n-title],[data-i18n-aria-label]",
    ).forEach((n) => {
      ["i18n", "i18nPlaceholder", "i18nTitle", "i18nAriaLabel"].forEach((k) => {
        const key = n.dataset[k];
        if (key) parts.push(settingsSearchAltStrings(key));
      });
    });
    if (el.dataset.key) parts.push(el.dataset.key);
    return parts.join(" ").replace(/\s+/g, " ").toLowerCase();
  }

  function settingsSearchTitle(el) {
    const t2 = el.querySelector(
      ".settings-block-title, .runtime-field-head label, summary, label, h3",
    );
    let title = (t2 && t2.textContent ? t2.textContent : "").trim();
    if (!title) title = (el.textContent || "").trim();
    return title.replace(/\s+/g, " ").slice(0, 80);
  }

  function buildSettingsSearchIndex() {
    const index = [];
    const GRAN = ".settings-block, .runtime-field, .tg-section";
    document.querySelectorAll("#settings-panel .settings-pane").forEach((pane) => {
      const tab = pane.dataset.pane;
      if (!tab) return;
      const nodes = Array.from(pane.querySelectorAll(GRAN));
      // Keep only leaf blocks (skip a container that wraps another indexed block).
      const leaves = nodes.filter((n) => !nodes.some((o) => o !== n && n.contains(o)));
      leaves.forEach((el) => {
        let anchor;
        if (el.dataset.key) {
          anchor = `[data-key="${cssEscapeAttr(el.dataset.key)}"]`;
        } else {
          if (!el.dataset.searchAnchor) el.dataset.searchAnchor = `ss${settingsSearchAnchorSeq++}`;
          anchor = `[data-search-anchor="${el.dataset.searchAnchor}"]`;
        }
        index.push({
          kind: "field",
          tab,
          title: settingsSearchTitle(el),
          haystack: settingsSearchHaystack(el),
          anchor,
        });
      });
    });
    // Section-level fallback — reachable even before a lazy pane (Runtime) is built.
    document.querySelectorAll("#settings-panel .settings-nav .settings-tab").forEach((tabEl) => {
      const labelEl = tabEl.querySelector(".settings-tab-label");
      const title = (labelEl && labelEl.textContent ? labelEl.textContent : "").trim();
      const isLink = tabEl.classList.contains("settings-tab-link");
      index.push({
        kind: "section",
        tab: isLink ? null : tabEl.dataset.tab || null,
        href: isLink ? tabEl.getAttribute("href") : null,
        title,
        haystack: `${title} ${tabEl.dataset.search || ""}`.toLowerCase(),
        anchor: null,
      });
    });
    return index;
  }

  function scoreSettingsSearchEntry(entry, query, tokens) {
    const hay = entry.haystack;
    for (const tk of tokens) if (!hay.includes(tk)) return -1;
    const title = entry.title.toLowerCase();
    let score;
    if (title === query) score = 100;
    else if (title.startsWith(query)) score = 80;
    else if (title.includes(query)) score = 60;
    else if (hay.includes(query)) score = 40;
    else score = 20; // matched on individual tokens only
    if (entry.kind === "section") score -= 15; // concrete fields rank above sections
    return score;
  }

  function querySettingsSearch(raw) {
    const query = raw.trim().toLowerCase();
    if (!query) return [];
    const tokens = query.split(/\s+/).filter(Boolean);
    // Rebuilt fresh each query: cheap over ~70 blocks, and a lazily-loaded pane
    // (Runtime) that populated after the last build is then always included.
    const idx = buildSettingsSearchIndex();
    const fields = [];
    const sections = [];
    idx.forEach((entry) => {
      const s = scoreSettingsSearchEntry(entry, query, tokens);
      if (s < 0) return;
      (entry.kind === "field" ? fields : sections).push({ entry, score: s });
    });
    // Only surface a section when none of its own fields already matched.
    const matchedTabs = new Set(fields.map((h) => h.entry.tab));
    const keptSections = sections.filter((h) => !h.entry.tab || !matchedTabs.has(h.entry.tab));
    return fields
      .concat(keptSections)
      .sort((a, b) => b.score - a.score)
      .slice(0, 24)
      .map((h) => h.entry);
  }

  function settingsSearchHighlight(title, query) {
    const esc = (s) =>
      s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
    const i = query ? title.toLowerCase().indexOf(query) : -1;
    if (i < 0) return esc(title);
    return (
      esc(title.slice(0, i)) +
      "<mark>" +
      esc(title.slice(i, i + query.length)) +
      "</mark>" +
      esc(title.slice(i + query.length))
    );
  }

  function setSettingsSearchActive(i) {
    const rows = settingsSearchEls.results
      ? settingsSearchEls.results.querySelectorAll(".settings-search-result")
      : [];
    if (!rows.length) return;
    settingsSearchActiveIdx = Math.max(0, Math.min(i, rows.length - 1));
    rows.forEach((r, idx) => {
      const on = idx === settingsSearchActiveIdx;
      r.classList.toggle("is-active", on);
      r.setAttribute("aria-selected", on ? "true" : "false");
      if (on) r.scrollIntoView({ block: "nearest" });
    });
  }

  function renderSettingsSearch() {
    const els = settingsSearchEls;
    if (!els.results || !els.input) return;
    const raw = els.input.value;
    const query = raw.trim();
    if (els.clear) els.clear.hidden = !raw;
    if (!query) {
      closeSettingsSearch();
      return;
    }
    const hits = querySettingsSearch(raw);
    settingsSearchHits = hits;
    settingsSearchActiveIdx = hits.length ? 0 : -1;
    els.results.innerHTML = "";
    if (!hits.length) {
      const empty = document.createElement("p");
      empty.className = "settings-search-empty";
      empty.textContent = t("settings.search.empty", { q: query });
      els.results.appendChild(empty);
    } else {
      const qlc = query.toLowerCase();
      const tabLabels = settingsTabLabels();
      hits.forEach((entry, i) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "settings-search-result" + (i === 0 ? " is-active" : "");
        b.setAttribute("role", "option");
        b.setAttribute("aria-selected", i === 0 ? "true" : "false");
        b.dataset.idx = String(i);
        const meta =
          entry.kind === "section"
            ? t("settings.search.section")
            : tabLabels[entry.tab] || entry.tab || "";
        b.innerHTML =
          `<span class="settings-search-result-title">${settingsSearchHighlight(entry.title, qlc)}</span>` +
          `<span class="settings-search-result-tab">${escapeHtml(meta)}</span>`;
        b.addEventListener("mousemove", () => setSettingsSearchActive(i));
        b.addEventListener("click", () => activateSettingsSearchResult(i));
        els.results.appendChild(b);
      });
    }
    els.results.hidden = false;
    els.input.setAttribute("aria-expanded", "true");
  }

  function flashSettingsAnchor(pane, anchor) {
    const el = pane.querySelector(anchor);
    if (!el) return false;
    const det = el.closest("details");
    if (det && !det.open) det.open = true; // reveal a collapsed Runtime category
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    el.classList.remove("settings-search-hit");
    void el.offsetWidth; // restart the flash animation
    el.classList.add("settings-search-hit");
    setTimeout(() => el.classList.remove("settings-search-hit"), 1800);
    return true;
  }

  function locateSettingsAnchor(tab, anchor) {
    const pane = document.getElementById(`settings-pane-${tab}`);
    if (!pane || !anchor) return;
    if (tab !== "runtime") {
      // Static panes are already in the DOM — flash right away, retry a few frames
      // in case the pane is mid-hydration.
      let tries = 0;
      const attempt = () => {
        if (!flashSettingsAnchor(pane, anchor) && tries++ < 30) setTimeout(attempt, 80);
      };
      attempt();
      return;
    }
    // switchSettingsTab("runtime") reloads + rebuilds #runtime-form asynchronously.
    // Flashing now would highlight the node that's about to be replaced, so wait
    // for the rebuild to settle (mutations stop) before flashing the fresh node.
    let flashed = false;
    let settle;
    const obs = new MutationObserver(() => {
      clearTimeout(settle);
      settle = setTimeout(() => {
        if (!flashed && flashSettingsAnchor(pane, anchor)) {
          flashed = true;
          obs.disconnect();
        }
      }, 150);
    });
    obs.observe(pane, { childList: true, subtree: true });
    // Fallback: if no rebuild is observed (form already current), flash after a grace.
    setTimeout(() => {
      if (!flashed && flashSettingsAnchor(pane, anchor)) {
        flashed = true;
        obs.disconnect();
      }
    }, 900);
    setTimeout(() => obs.disconnect(), 3500);
  }

  function activateSettingsSearchResult(i) {
    const entry = settingsSearchHits[i];
    if (!entry) return;
    if (entry.kind === "section" && entry.href) {
      window.location.href = entry.href;
      return;
    }
    const tab = entry.tab || "overview";
    closeSettingsSearch();
    switchSettingsTab(tab); // reveals the pane + kicks off any lazy load
    if (entry.anchor) locateSettingsAnchor(tab, entry.anchor);
  }

  function closeSettingsSearch() {
    const els = settingsSearchEls;
    if (!els.results) return;
    els.results.hidden = true;
    els.results.innerHTML = "";
    settingsSearchHits = [];
    settingsSearchActiveIdx = -1;
    if (els.input) els.input.setAttribute("aria-expanded", "false");
  }

  function clearSettingsSearch() {
    const els = settingsSearchEls;
    if (els.input) els.input.value = "";
    if (els.clear) els.clear.hidden = true;
    closeSettingsSearch();
  }

  async function prewarmSettingsSearch() {
    if (settingsSearchPrewarmed) return;
    settingsSearchPrewarmed = true;
    try {
      const rf = document.getElementById("runtime-form");
      if (rf && !rf.querySelector(".runtime-field")) await loadRuntimeSettings();
    } catch (_) {
      /* the index still works over the static panes */
    }
    buildSettingsSearchIndex();
    if (settingsSearchEls.input && settingsSearchEls.input.value.trim()) renderSettingsSearch();
  }

  function wireSettingsSearch() {
    const els = settingsSearchEls;
    if (!els.input) return;
    let debounce;
    els.input.addEventListener("focus", () => {
      void prewarmSettingsSearch();
      if (els.input.value.trim()) renderSettingsSearch();
    });
    els.input.addEventListener("input", () => {
      void prewarmSettingsSearch(); // ensure the Runtime pane loads so its fields index
      clearTimeout(debounce);
      debounce = setTimeout(renderSettingsSearch, 90);
    });
    els.input.addEventListener("keydown", (e) => {
      const open = els.results && !els.results.hidden;
      if (e.key === "ArrowDown") {
        e.preventDefault();
        if (!open) renderSettingsSearch();
        else setSettingsSearchActive(settingsSearchActiveIdx + 1);
      } else if (e.key === "ArrowUp") {
        if (!open) return;
        e.preventDefault();
        setSettingsSearchActive(settingsSearchActiveIdx - 1);
      } else if (e.key === "Enter") {
        if (open && settingsSearchActiveIdx >= 0) {
          e.preventDefault();
          activateSettingsSearchResult(settingsSearchActiveIdx);
        }
      } else if (e.key === "Escape") {
        if (els.input.value || open) {
          e.stopPropagation(); // keep the settings panel open; just reset the search
          clearSettingsSearch();
        }
      }
    });
    if (els.clear) {
      els.clear.addEventListener("click", () => {
        clearSettingsSearch();
        els.input.focus();
      });
    }
    // Click elsewhere in the panel closes the dropdown (the panel stops clicks
    // from reaching document, so we listen on the panel itself).
    if (settingsPanel) {
      settingsPanel.addEventListener("click", (e) => {
        if (els.root && !els.root.contains(e.target) && els.results && !els.results.hidden) {
          closeSettingsSearch();
        }
      });
    }
  }

  function wireSettingsChrome() {
    if (btnSettings) {
      btnSettings.addEventListener("click", (e) => {
        e.stopPropagation();
        if (document.body.classList.contains("settings-open")) closeSettings();
        else openSettings();
      });
    }
    if (btnSettingsClose) btnSettingsClose.addEventListener("click", closeSettings);
    if (settingsBackdrop) settingsBackdrop.addEventListener("click", closeSettings);
    if (settingsPanel) {
      settingsPanel.addEventListener("click", (e) => e.stopPropagation());
    }

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && document.body.classList.contains("archive-open")) {
        hooks.closeArchiveDrawer();
      } else if (e.key === "Escape" && document.body.classList.contains("settings-open")) {
        closeSettings();
      } else if (
        (e.ctrlKey || e.metaKey) &&
        !e.shiftKey &&
        !e.altKey &&
        e.key === ","
      ) {
        e.preventDefault();
        openSettings();
      }
    });

    if (btnTheme) {
      btnTheme.addEventListener("click", () => {
        const next = document.documentElement.dataset.theme === "light" ? "dark" : "light";
        applyTheme(next);
      });
    }

    // Language picker (overview pane): swap UI live + persist backend so voice +
    // persona follow. Kept in sync with the runtime-pane dropdown via the event.
    const langSelect = document.getElementById("settings-language-select");
    if (langSelect && window.AkanaI18n) {
      langSelect.value = window.AkanaI18n.getLanguage();
      langSelect.addEventListener("change", async () => {
        const lang = langSelect.value;
        if (lang === window.AkanaI18n.getLanguage()) return;
        langSelect.disabled = true;
        // Persist (localStorage + backend) BEFORE reloading so every JS-rendered
        // string re-emits via t() in the new language and the boot reconcile agrees.
        try {
          await window.AkanaI18n.setLanguagePersisted(lang);
        } catch (_) {
          /* persisted to localStorage regardless; reload still applies it */
        }
        window.location.reload();
      });
    }

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") void persistAllSettings();
    });

    if (baseUrlInput) baseUrlInput.value = localStorage.getItem(LS_BASE) || window.location.origin;
    if (tokenInput) tokenInput.value = localStorage.getItem(LS_TOKEN) || "";
    if (!isMemoryStudioPage) {
      updateConnectionEndpointCard();
      syncThemePickerUi();
      switchSettingsTab("overview");
      settingsTabs.forEach((tab) => {
        if (tab.classList.contains("settings-tab-link")) return;
        tab.addEventListener("click", () => {
          switchSettingsTab(tab.dataset.tab || "overview");
        });
      });
      wireSettingsSearch();
    }
    if (llmEls.provider) {
      llmEls.provider.addEventListener("change", () => {
        providerTouched = true; // explicit user pick → allowed to be persisted
        const v = llmEls.provider.value.trim();
        // Clear the model select so collectLlmSettings doesn't ship the previous
        // provider's model under the new provider's field; saveLlmSettings →
        // fillLlmForm then repopulates it live for the new provider.
        if (llmEls.model) llmEls.model.innerHTML = "";
        void saveLlmSettings({ provider: v }).catch((e) =>
          setLlmStatus(e.message || String(e), true),
        );
      });
    }
    if (llmEls.model) {
      llmEls.model.addEventListener("change", () => {
        const provider = (llmEls.provider && llmEls.provider.value.trim()) || "";
        const field = PROVIDER_MODEL_FIELD[provider];
        const v = llmEls.model.value.trim();
        if (!field || !v) return;
        const hint = document.getElementById("llm-model-hint");
        if (hint) hint.textContent = t("settings.llm.selected", { model: v });
        void saveLlmSettings({ [field]: v }).catch((e) =>
          setLlmStatus(e.message || String(e), true),
        );
      });
    }

    // Credentials: show/hide toggle + save (empty fields are not sent in PUT).
    const wireSecretToggle = (btnId, input) => {
      const btn = document.getElementById(btnId);
      if (btn && input) {
        btn.addEventListener("click", () => {
          input.type = input.type === "password" ? "text" : "password";
        });
      }
    };
    wireSecretToggle("btn-toggle-cred-cursor", credEls.cursorKey);
    wireSecretToggle("btn-toggle-cred-claude", credEls.claudeToken);
    wireSecretToggle("btn-toggle-cred-gemini", credEls.geminiKey);
    wireSecretToggle("btn-toggle-cred-openai", credEls.openaiKey);
    // Reveal the STORED key (not the input you're typing) from the audited
    // /reveal endpoint — parity with what the model reads via the vault MCP.
    const wireCredReveal = (btn, key, out) => {
      if (!btn || !out) return;
      btn.addEventListener("click", async () => {
        if (out.dataset.revealed === "1") {  // toggle off — drop plaintext from the DOM
          out.textContent = "";
          out.hidden = true;
          out.dataset.revealed = "0";
          btn.textContent = t("settings.cred.reveal_btn");
          return;
        }
        try {
          const r = await fetch(
            `${baseUrl()}/api/v1/system/credentials/${encodeURIComponent(key)}/reveal`,
            { headers: authHeaders() },
          );
          if (!r.ok) {
            const err = await r.json().catch(() => ({}));
            throw new Error(parseApiError(err, r.status) || `HTTP ${r.status}`);
          }
          const j = await r.json();
          out.textContent = (j && j.value) || "";
          out.hidden = false;
          out.dataset.revealed = "1";
          btn.textContent = t("settings.cred.hide_btn");
        } catch (e) {
          setCredStatus(t("settings.cred.reveal_failed", { error: e.message || e }), "err");
        }
      });
    };
    wireCredReveal(credEls.cursorReveal, "cursor_api_key", credEls.cursorRevealed);
    wireCredReveal(credEls.claudeReveal, "claude_oauth_token", credEls.claudeRevealed);
    wireCredReveal(credEls.geminiReveal, "gemini_api_key", credEls.geminiRevealed);
    wireCredReveal(credEls.openaiReveal, "openai_api_key", credEls.openaiRevealed);
    if (credEls.save) credEls.save.addEventListener("click", () => void saveCredentials());

    const btnConnectorsRefresh = document.getElementById("connectors-refresh");
    if (btnConnectorsRefresh) {
      btnConnectorsRefresh.addEventListener("click", () => void loadConnectors());
    }
    // Tailscale card wiring: mode selector (off/serve/funnel), copy URL, refresh.
    const tsModeSel = document.getElementById("ts-mode");
    if (tsModeSel) {
      tsModeSel.addEventListener("change", () => {
        const mode = tsModeSel.value;
        // Funnel exposes the public internet — surface the warning immediately,
        // even though the server also enforces the token rule.
        const funnelWarn = document.getElementById("ts-funnel-warning");
        if (funnelWarn) funnelWarn.hidden = mode !== "funnel";
        void tsSetMode(mode);
      });
    }
    const tsCopyBtn = document.getElementById("ts-copy-url");
    if (tsCopyBtn) tsCopyBtn.addEventListener("click", () => void tsCopyUrl());
    const tsRefreshBtn = document.getElementById("ts-refresh");
    if (tsRefreshBtn) tsRefreshBtn.addEventListener("click", () => void loadTailscale());
    // Telegram management panel — enable toggle / token (save·clear·reveal·test) / chat-ids.
    const tgEnabled = document.getElementById("tg-enabled");
    if (tgEnabled) {
      tgEnabled.addEventListener("change", () => {
        void tgUpdate(
          { enabled: tgEnabled.checked },
          tgEnabled.checked ? "settings.tg.enabled_on" : "settings.tg.enabled_off",
        );
      });
    }
    const tgTokenInput = document.getElementById("tg-token");
    const tgTokenToggle = document.getElementById("tg-token-toggle");
    if (tgTokenToggle && tgTokenInput) {
      tgTokenToggle.addEventListener("click", () => {
        tgTokenInput.type = tgTokenInput.type === "password" ? "text" : "password";
      });
    }
    const tgTokenSave = document.getElementById("tg-token-save");
    if (tgTokenSave && tgTokenInput) {
      tgTokenSave.addEventListener("click", async () => {
        const v = tgTokenInput.value.trim();
        if (!v) {
          tgPanelStatus(t("settings.tg.token_empty"), "err");
          return;
        }
        const ok = await tgUpdate({ bot_token: v }, "settings.tg.token_saved");
        if (ok) tgTokenInput.value = ""; // never leave the secret in the field
      });
    }
    const tgTokenClear = document.getElementById("tg-token-clear");
    if (tgTokenClear) {
      tgTokenClear.addEventListener("click", async () => {
        const ok = await tgUpdate({ bot_token: "" }, "settings.tg.token_cleared");
        if (ok && tgTokenInput) tgTokenInput.value = "";
      });
    }
    const tgTokenReveal = document.getElementById("tg-token-reveal");
    const tgTokenRevealed = document.getElementById("tg-token-revealed");
    if (tgTokenReveal && tgTokenRevealed) {
      tgTokenReveal.addEventListener("click", async () => {
        if (tgTokenRevealed.dataset.revealed === "1") {
          tgTokenRevealed.textContent = "";
          tgTokenRevealed.hidden = true;
          tgTokenRevealed.dataset.revealed = "0";
          tgTokenReveal.textContent = t("settings.cred.reveal_btn");
          return;
        }
        try {
          const r = await fetch(
            `${baseUrl()}/api/v1/system/credentials/telegram_bot_token/reveal`,
            { headers: authHeaders() },
          );
          const j = await r.json().catch(() => ({}));
          if (!r.ok) throw new Error(parseApiError(j, r.status) || `HTTP ${r.status}`);
          tgTokenRevealed.textContent = (j && j.value) || "";
          tgTokenRevealed.hidden = false;
          tgTokenRevealed.dataset.revealed = "1";
          tgTokenReveal.textContent = t("settings.cred.hide_btn");
        } catch (e) {
          tgPanelStatus(t("settings.cred.reveal_failed", { error: e.message || e }), "err");
        }
      });
    }
    const tgChatInput = document.getElementById("tg-chatids");
    const tgChatSave = document.getElementById("tg-chatids-save");
    if (tgChatSave && tgChatInput) {
      // Non-secret free-text → runtime tab's dirty-gated inline Save (the bot
      // token above stays an explicit credential save). Enter commits too.
      const tgChatRow = document.getElementById("tg-chatids-row");
      const commitChatIds = () => {
        if (tgChatRow) tgChatRow.classList.remove("is-dirty");
        void tgUpdate({ allowed_chat_ids: tgChatInput.value }, "settings.tg.chatids_saved");
      };
      tgChatInput.addEventListener("input", () => tgChatRow && tgChatRow.classList.add("is-dirty"));
      tgChatInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          commitChatIds();
        }
      });
      tgChatSave.addEventListener("click", commitChatIds);
    }
    const tgTestBtn = document.getElementById("tg-test");
    if (tgTestBtn) tgTestBtn.addEventListener("click", () => void tgTest());
    const tgScanBtn = document.getElementById("tg-scan");
    if (tgScanBtn) tgScanBtn.addEventListener("click", () => void tgScan());

    const btnToggleToken = document.getElementById("btn-toggle-token");
    if (btnToggleToken && tokenInput) {
      btnToggleToken.addEventListener("click", () => {
        tokenInput.type = tokenInput.type === "password" ? "text" : "password";
      });
    }
    const btnTestConn = document.getElementById("btn-test-connection");
    const connResult = document.getElementById("connection-test-result");
    if (btnTestConn) {
      btnTestConn.addEventListener("click", async () => {
        if (connResult) connResult.textContent = t("settings.conn.testing");
        localStorage.setItem(LS_BASE, baseUrlInput.value.trim());
        localStorage.setItem(LS_TOKEN, tokenInput.value.trim());
        try {
          const hr = await fetch(`${baseUrl()}/health`);
          const sr = await fetch(`${baseUrl()}/api/v1/system/status`, { headers: authHeaders() });
          if (!hr.ok) throw new Error(`health ${hr.status}`);
          if (!sr.ok) throw new Error(`status ${sr.status}`);
          const j = await sr.json();
          // Provider-agnostic: the server is reachable (health + status both OK).
          // Success is keyed on the ACTIVE provider's live probe — not cursor_api
          // (a leftover from the single-provider era). Ollama is local → no key.
          const active = activeModelInfo(j);
          const deps = j.dependencies || {};
          const PROBE = {
            cursor: deps.cursor_api,
            claude: deps.claude_cli,
            gemini: deps.gemini_api,
            openai: deps.openai_api,
          };
          const providerReachable =
            active.provider === "ollama"
              ? true
              : !!(PROBE[active.provider] && PROBE[active.provider].reachable);
          if (connResult) {
            connResult.textContent = providerReachable
              ? t("settings.conn.ok_provider", { provider: active.label })
              : t("settings.conn.up_no_provider", { provider: active.label });
            connResult.style.color = providerReachable ? "var(--ok)" : "var(--warn)";
          }
          connectWs(true);
        } catch (e) {
          if (connResult) {
            connResult.textContent = t("settings.conn.error", { error: e.message || e });
            connResult.style.color = "var(--err)";
          }
        }
      });
    }
    const btnReconnectWs = document.getElementById("btn-reconnect-ws");
    if (btnReconnectWs) {
      btnReconnectWs.addEventListener("click", () => {
        clearTimeout(reconnectTimer);
        resetWsReconnectBackoff();
        connectWs(true);
        if (connResult) connResult.textContent = t("settings.conn.ws_reconnecting");
        updateConnectionEndpointCard();
      });
    }
    const btnCopyBase = document.getElementById("btn-copy-base-url");
    if (btnCopyBase) {
      btnCopyBase.addEventListener("click", async () => {
        const url = baseUrl();
        try {
          await navigator.clipboard.writeText(url);
          if (connResult) {
            connResult.textContent = t("settings.conn.copied");
            connResult.style.color = "var(--ok)";
          }
        } catch {
          if (connResult) {
            connResult.textContent = t("settings.conn.copy_failed");
            connResult.style.color = "var(--err)";
          }
        }
      });
    }
    document.querySelectorAll('input[name="theme-pref"]').forEach((el) => {
      el.addEventListener("change", () => {
        if (el.checked) applyThemePreference(el.value);
      });
    });
    const compactLogCb = document.getElementById("settings-compact-log");
    if (compactLogCb) {
      compactLogCb.addEventListener("change", () => applyCompactLog(compactLogCb.checked));
    }
    const showUsageCb = document.getElementById("settings-show-usage");
    if (showUsageCb) {
      showUsageCb.addEventListener("change", () => applyShowUsage(showUsageCb.checked));
    }
    // chat-turns is the only free-form LLM field (provider/model save instantly on
    // change above) — so it follows the runtime tab's dirty-gated inline Save:
    // the button appears only once you edit, Enter commits, save clears dirty.
    if (llmEls.save && llmEls.chatTurns) {
      const turnsRow = document.getElementById("llm-chat-turns-row");
      const commitTurns = () =>
        void saveLlmSettings({}).catch((e) => setLlmStatus(e.message || String(e), true));
      const markTurnsDirty = () => turnsRow && turnsRow.classList.add("is-dirty");
      llmEls.chatTurns.addEventListener("input", markTurnsDirty);
      llmEls.chatTurns.addEventListener("change", markTurnsDirty);
      llmEls.chatTurns.addEventListener("keydown", (e) => {
        if (e.key === "Enter") {
          e.preventDefault();
          commitTurns();
        }
      });
      llmEls.save.addEventListener("click", commitTurns);
    }

    // Base URL + token apply live (persisted on input; WS reconnect debounced) —
    // so the pane carries no save button. A debounced inline "Saved" gives
    // the same instant-apply confirmation the toggles get from flipping.
    let connSavedTimer = 0;
    const flashConnSaved = () => {
      clearTimeout(connSavedTimer);
      connSavedTimer = setTimeout(() => {
        flashStatus(
          document.getElementById("connection-save-status"),
          t("settings.save.saved"),
          "var(--ok)",
          true,
        );
      }, 600);
    };
    if (baseUrlInput) {
      baseUrlInput.addEventListener("input", () => {
        persistAndReconnect();
        updateConnectionEndpointCard();
        flashConnSaved();
      });
    }
    if (tokenInput) {
      tokenInput.addEventListener("input", () => {
        persistAndReconnect();
        updateConnectionEndpointCard();
        flashConnSaved();
      });
    }
  }

  function init(opts = {}) {
    hooks = { ...hooks, ...opts };
    if (opts.baseUrlInput) baseUrlInput = opts.baseUrlInput;
    if (opts.tokenInput) tokenInput = opts.tokenInput;
    window.AkanaCore.configure({ baseUrlInput, tokenInput });
    applyThemePreference(getThemePreference());
    applyCompactLog(localStorage.getItem(LS_COMPACT_LOG) === "1");
    applyShowUsage(localStorage.getItem(LS_SHOW_USAGE) === "1");
    if (window.matchMedia) {
      window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
        if (getThemePreference() === "system") applyResolvedTheme(resolveTheme("system"));
      });
    }
    if (!_settingsWired) {
      _settingsWired = true;
      wireSettingsChrome();
      wireModelSwitcher();
      window.addEventListener("pageshow", () => void loadModelPill());
    }
  }

  function getWsReadyState() {
    return (ws && ws.readyState) || 0;
  }

  window.AkanaSettings = {
    init,
    openSettings,
    closeSettings,
    loadHealth,
    loadModelPill,
    restoreConversationLlm,
    connectWs,
    updateSettingsHero,
    saveLlmSettings,
    saveCredentials,
    loadRuntimeSettings,
    loadConnectors,
    switchSettingsTab,
    applyThemePreference,
    getWsReadyState,
    /** testability: WS event handler exposed publicly (harness contract) */
    _handleWsEvent: handleWsEvent,
    /** testability: schema → form model PURE function (harness contract) */
    _runtimeFormModel: buildRuntimeFormModel,
    get serverApiMarker() {
      return serverApiMarker;
    },
  };
})();
