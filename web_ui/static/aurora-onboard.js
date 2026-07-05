/* ═══════════════════════════════════════════════════════════════════════════
   AURORA ONBOARD — first-run wizard (drop-in, self-contained)
   ─────────────────────────────────────────────────────────────────────────
   Loads LAST (after aurora-ui.js). Injects its OWN `aur-onb-*` DOM — touches
   no existing markup, ids, or classes. Styles live in aurora-onboard.css and
   use --j-* tokens so they follow the active theme/accent.

   GOAL: a brand-new user with NO model provider configured can go from zero →
   CONNECTED → into a working chat WITHOUT hunting through Settings.

   Premium 6-step flow (every step does something REAL):
     1. welcome   → warm hero: what Akana is + an HONEST data note (storage is
        local; a cloud model still receives that turn to write the reply; Ollama
        stays fully offline). No "your data never leaves" overclaim.
     2. connect   → THE HEART. Pick a provider; for key-based providers
        (gemini→GEMINI_API_KEY, openai→OPENAI_API_KEY, cursor→cursor_api_key)
        show an INLINE password input + Save that persists via the SAME
        credential API the settings panel uses (PUT /api/v1/system/credentials)
        and then switches the active provider (PUT /api/v1/system/llm-settings)
        — NO new endpoints. claude (CLI/OAuth) + ollama (local) show a short
        setup instruction instead of a key field. After saving we re-check
        /api/v1/system/status and show a green "Connected · <provider> · <model>".
     3. personalize → the user's name (how Akana addresses them) → localStorage
        'akana.userName' (+ best-effort memory fact). Theme + accent live here.
     4. inside    → ONE consolidated feature tour: memory, secure vault, packs,
        personas — each a card with a "where to find it" hint. Educational only.
     5. voice     → optional "Hey Akana" autostart toggle (localStorage
        'akana.wakeAutostart' — the SAME key akana-voice-settings.js reads).
     6. start     → use-case → 3 sample prompts that fill the composer (#msg).

   Persistence keys (mirror the rest of the app):
     • theme  → documentElement.dataset.theme + localStorage 'akana.theme'.
     • accent → documentElement.dataset.accent + localStorage 'cockpit:accent'.
     • use-case → localStorage 'akana.usecase' (seeds the sample prompts).
     • name → localStorage 'akana.userName'.
     • wake → localStorage 'akana.wakeAutostart' ("1"/"0").
   Important:
     • 'akana.onboarded' is set ONLY on explicit Skip/Finish — a stray
       backdrop/Esc click no longer marks the user onboarded forever.
     • Step strings are rebuilt inside open() (not cached in build()), so a
       language switch between sessions shows fresh, correct copy.
   Re-openable programmatically: window.auroraOnboard.open().
   Defensive: every step no-ops if its target is absent.
   ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  // Inline ENGLISH fallback for the highest-visibility onboard.* strings shown
  // BEFORE AkanaI18n is guaranteed loaded (hero titles/leads + primary buttons).
  // English only — it's the product default; the full bilingual dictionary lives
  // in akana-i18n-strings-misc.js and wins whenever it's present. Without this,
  // a late/absent AkanaI18n leaks raw keys like "onboard.step1_title" as the hero.
  var _FALLBACK_EN = {
    "onboard.step1_title": "Welcome to Akana",
    "onboard.step1_lead": "Let me get to know you in a few seconds — then we'll work together.",
    "onboard.connect_title": "Connect a model",
    "onboard.connect_lead": "Pick a provider and connect it right here — you'll be chatting in seconds.",
    "onboard.person_title": "Make it yours",
    "onboard.person_lead": "Tell me your name and pick a look — you can change these any time.",
    "onboard.inside_title": "What's inside Akana",
    "onboard.inside_lead": "A quick tour of the main features — you can explore each later from the top bar or Settings. None of it is required to start.",
    "onboard.voice_title": "Talk to Akana",
    "onboard.voice_lead": "Prefer hands-free? Turn on wake-word listening — or skip and do it later.",
    "onboard.start_title": "Let's begin",
    "onboard.start_lead": "Pick what you're here for, then tap a prompt to drop it into the composer.",
    "onboard.next": "Continue",
    "onboard.start": "Get started",
    "onboard.skip": "Skip",
    "onboard.back": "Back",
  };

  var _t = function (k, p) {
    if (window.AkanaI18n && window.AkanaI18n.t) return window.AkanaI18n.t(k, p);
    // i18n not ready yet — use the inline English fallback so we never leak the
    // raw key. {token} placeholders are substituted the same way the dictionary does.
    var s = _FALLBACK_EN[k];
    if (s == null) return k;
    if (p) {
      s = s.replace(/\{(\w+)\}/g, function (m, name) {
        return Object.prototype.hasOwnProperty.call(p, name) ? String(p[name]) : m;
      });
    }
    return s;
  };

  // ids must match the accent vocabulary that has real --j-* remaps
  // (tokens.css + aurora-settings.css). "azure" = base bucket (no override);
  // this module + aurora-ui.js read/write the SAME 'cockpit:accent' key.
  // hex = dark-theme primary tone, used only for the live preview dot.
  var ACCENTS = [
    { id: "azure",   hex: "#5aa9ff", tk: "onboard.accent_azure" },
    { id: "violet",  hex: "#a98bff", tk: "onboard.accent_violet" },
    { id: "teal",    hex: "#3ddbd0", tk: "onboard.accent_teal" },
    { id: "emerald", hex: "#3fd99a", tk: "onboard.accent_emerald" },
    { id: "sunset",  hex: "#ff8a5c", tk: "onboard.accent_sunset" },
  ];

  // ── Provider catalogue (the connect step) ──────────────────────────────────
  // kind="key"  → inline password field; `credKey` is the credentials-API field
  //               (matches secret_store.ALLOWED_KEYS + akana-settings.js).
  // kind="local"/"cli" → short setup instruction, no key field.
  // `tk`/`dk` are i18n keys for the title/description; `hk` is the instruction.
  var PROVIDERS = [
    { id: "cursor", kind: "key", credKey: "cursor_api_key", label: "Cursor", ic: "▟",
      tk: "onboard.prov_cursor_t", dk: "onboard.prov_cursor_d", ph: "onboard.prov_cursor_ph" },
    { id: "gemini", kind: "key", credKey: "gemini_api_key", label: "Gemini", ic: "✦",
      tk: "onboard.prov_gemini_t", dk: "onboard.prov_gemini_d", ph: "onboard.prov_gemini_ph" },
    { id: "openai", kind: "key", credKey: "openai_api_key", label: "OpenAI", ic: "◎",
      tk: "onboard.prov_openai_t", dk: "onboard.prov_openai_d", ph: "onboard.prov_openai_ph" },
    { id: "claude", kind: "cli", label: "Claude", ic: "✸",
      tk: "onboard.prov_claude_t", dk: "onboard.prov_claude_d", hk: "onboard.prov_claude_hint" },
    { id: "ollama", kind: "local", label: "Ollama", ic: "◍",
      tk: "onboard.prov_ollama_t", dk: "onboard.prov_ollama_d", hk: "onboard.prov_ollama_hint" },
  ];

  // Use-case catalogue. `id` persists to localStorage; `prompts` are the i18n
  // keys whose strings seed the finish step (and land in the composer).
  var USECASES = [
    { id: "dev",       ic: "⚡",  tk: "onboard.usecase1_t", dk: "onboard.usecase1_d",
      prompts: ["onboard.seed_dev1", "onboard.seed_dev2", "onboard.seed_dev3"] },
    { id: "assistant", ic: "🗂️", tk: "onboard.usecase2_t", dk: "onboard.usecase2_d",
      prompts: ["onboard.seed_assistant1", "onboard.seed_assistant2", "onboard.seed_assistant3"] },
    { id: "writing",   ic: "✍️", tk: "onboard.usecase3_t", dk: "onboard.usecase3_d",
      prompts: ["onboard.seed_writing1", "onboard.seed_writing2", "onboard.seed_writing3"] },
  ];

  var LS_USECASE = "akana.usecase";
  var LS_NAME = "akana.userName";
  var LS_WAKE = "akana.wakeAutostart";

  var PROV_LABEL = { cursor: "Cursor", claude: "Claude", ollama: "Ollama", gemini: "Gemini", openai: "OpenAI" };

  var root = document.documentElement;
  var STEPS = [];
  var step = 0, ov = null, modal = null, titleEl, leadEl, bodyEl, dotsEl, nextBtn;
  var keydownHandler = null;          // tracked so it can be torn down (no leak)
  var selectedUsecase = "dev";        // mirrors localStorage; drives sample prompts
  var selectedProvider = "cursor";    // which provider card is expanded on the connect step
  var connectState = null;            // last known status: { ok, provider, model } or null
  var credState = null;               // masked credentials map {<credKey>:{set,hint}} or null
  var serverWakeEnabled = null;       // /voice/wake/config `enabled` (server model live) — null until probed

  function makeSteps() {
    return [
      { title: _t("onboard.step1_title"), lead: _t("onboard.step1_lead"), kind: "welcome" },
      { title: _t("onboard.connect_title"), lead: _t("onboard.connect_lead"), kind: "connect" },
      { title: _t("onboard.person_title"), lead: _t("onboard.person_lead"), kind: "personalize" },
      { title: _t("onboard.inside_title"), lead: _t("onboard.inside_lead"), kind: "inside" },
      { title: _t("onboard.voice_title"), lead: _t("onboard.voice_lead"), kind: "voice" },
      { title: _t("onboard.start_title"), lead: _t("onboard.start_lead"), kind: "start" },
    ];
  }

  // ── persistence helpers ─────────────────────────────────────────────────────
  function readTheme() {
    try { return localStorage.getItem("akana.theme") || "light"; } catch (e) { return "light"; }
  }
  function setTheme(t) {
    root.dataset.theme = t;
    try { localStorage.setItem("akana.theme", t); } catch (e) {}
  }
  function setAccent(a) {
    root.dataset.accent = a;
    try { localStorage.setItem("cockpit:accent", a); } catch (e) {}
  }
  function readUsecase() {
    try { return localStorage.getItem(LS_USECASE) || "dev"; } catch (e) { return "dev"; }
  }
  function setUsecase(id) {
    selectedUsecase = id;
    try { localStorage.setItem(LS_USECASE, id); } catch (e) {}
  }
  function usecaseById(id) {
    for (var i = 0; i < USECASES.length; i++) if (USECASES[i].id === id) return USECASES[i];
    return USECASES[0];
  }
  function providerById(id) {
    for (var i = 0; i < PROVIDERS.length; i++) if (PROVIDERS[i].id === id) return PROVIDERS[i];
    return PROVIDERS[0];
  }
  function readName() {
    try { return localStorage.getItem(LS_NAME) || ""; } catch (e) { return ""; }
  }
  function setName(v) {
    try {
      if (v) localStorage.setItem(LS_NAME, v);
      else localStorage.removeItem(LS_NAME);
    } catch (e) {}
  }
  function readWake() {
    try { return localStorage.getItem(LS_WAKE) === "1"; } catch (e) { return false; }
  }
  function setWake(on) {
    try { localStorage.setItem(LS_WAKE, on ? "1" : "0"); } catch (e) {}
  }

  function el(tag, cls, html) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  }

  /** Fill the composer (#msg) with a sample prompt and focus it. Returns success. */
  function fillComposer(text) {
    var msg = document.getElementById("msg");
    if (!msg) return false;
    msg.value = text;
    try { msg.dispatchEvent(new Event("input", { bubbles: true })); } catch (e) {}
    try { msg.focus(); } catch (e) {}
    return true;
  }

  // ── server plumbing (REUSES AkanaCore base/auth — no new endpoints) ──────────
  function apiBase() {
    try {
      if (window.AkanaCore && typeof window.AkanaCore.baseUrl === "function") return window.AkanaCore.baseUrl();
    } catch (e) {}
    return "";
  }
  function authHeaders(json) {
    try {
      if (window.AkanaCore && typeof window.AkanaCore.authHeaders === "function") return window.AkanaCore.authHeaders(!!json);
    } catch (e) {}
    return {};
  }
  function parseApiError(body, status) {
    try {
      if (window.AkanaCore && typeof window.AkanaCore.parseApiError === "function") return window.AkanaCore.parseApiError(body, status);
    } catch (e) {}
    return "HTTP " + status;
  }

  // Server probes return a stable, language-neutral `error_code` alongside the raw
  // English `error` string. Map the codes we know to bilingual dictionary keys so a
  // TR-mode banner shows Turkish instead of the verbatim English probe text; fall
  // back to the raw `error` (still honest, just untranslated) for unknown codes and
  // for providers whose catalog doesn't emit a code yet. `dep` is the per-provider
  // dependency object (e.g. status.dependencies.cursor_api).
  var ERR_CODE_KEY = {
    no_key: null,                                  // handled upstream as a CTA (no reason line)
    bridge_missing: "onboard.connect_err_bridge_missing",
    unreachable: "onboard.connect_err_unreachable",
    auth_rejected: "onboard.connect_err_auth_rejected",
    no_session: "onboard.connect_claude_nologin",
    token_expired: "onboard.connect_claude_unreachable",
    sdk_missing: "onboard.connect_err_sdk_missing",
  };
  // AUTH-CERTAIN codes: the probe got a definitive "this credential is bad" answer
  // (a bad key / expired session / no session), so a just-saved provider must be
  // reverted to the prior one. Every OTHER live:false code (unreachable — a network
  // blip or a bridge still warming up) is transient: keep the keyed provider selected
  // with an amber warning, so a momentary blip never un-selects a valid key.
  var AUTH_CERTAIN_CODES = { auth_rejected: 1, no_session: 1, token_expired: 1 };
  function isAuthCertain(dep) {
    return !!(dep && dep.error_code && AUTH_CERTAIN_CODES[dep.error_code]);
  }
  function probeReason(dep) {
    if (!dep) return null;
    var code = dep.error_code;
    if (code && Object.prototype.hasOwnProperty.call(ERR_CODE_KEY, code)) {
      var key = ERR_CODE_KEY[code];
      return key ? _t(key) : null;
    }
    return dep.error || null;   // unknown/absent code → raw English (better than nothing)
  }

  // Maps the active provider to its /system/status dependency probe and reports a
  // rich health verdict: { ready, live, reason }.
  //   • ready  — the provider is usable (key present / local) → banner may go green.
  //   • live   — a real reachability probe confirmed the provider answers (claude
  //              queries Anthropic /v1/models; cursor/gemini/openai carry .reachable).
  //   • reason — a human-readable failure/limbo message when NOT ready (or when the
  //              key is set but the live probe can't reach the provider yet).
  // ollama is local (no key) → always treated as ready. This is what lets the
  // "Re-check connection" button report the TRUTH instead of silently no-op'ing.
  function providerHealth(status, provider) {
    var deps = (status && status.dependencies) || {};
    if (provider === "ollama") {
      // Ollama runs locally with no API key. /system/status exposes NO ollama
      // reachability signal today (no `dependencies.ollama`, no reachable/
      // model_count), so we CANNOT prove `ollama serve` is actually running.
      // Rather than claim a false green, report a NEUTRAL "can't verify" state:
      // not ready/live (no evidence), with a reason nudging the user to start the
      // server. If a future status payload adds ollama reachability, honor it here.
      var od = deps.ollama || {};
      if (Object.prototype.hasOwnProperty.call(od, "reachable")) {
        if (od.reachable) return { ready: true, live: true, reason: null };
        return { ready: false, live: false, reason: od.error || _t("onboard.connect_ollama_unreachable"), unverifiable: true };
      }
      return { ready: false, live: false, reason: _t("onboard.connect_ollama_unverifiable"), unverifiable: true };
    }
    if (provider === "claude") {
      var c = deps.claude_cli || {};
      var tokenSet = !!(c.token_set || c.oauth_token_set);
      var reachable = !!c.reachable;
      if (reachable) return { ready: true, live: true, reason: null };
      if (tokenSet) {
        // Session token exists but Anthropic didn't answer — surface WHY (expired
        // token, offline, …). Route through probeReason() so a TR banner localizes
        // the language-neutral error_code (token_expired → …) instead of showing the
        // verbatim English c.error; falls back to c.error for an unknown/absent code.
        return {
          ready: false, live: false,
          reason: probeReason(c) || _t("onboard.connect_claude_unreachable"),
          authCertain: isAuthCertain(c),
        };
      }
      // No session at all — auth-certain (there is no credential to reach with).
      return { ready: false, live: false, reason: _t("onboard.connect_claude_nologin"), authCertain: true };
    }
    // Key-based providers (cursor/gemini/openai): keyed dependency probe.
    var depKey = provider === "cursor" ? "cursor_api"
      : provider === "gemini" ? "gemini_api"
      : provider === "openai" ? "openai_api" : null;
    var d = (depKey && deps[depKey]) || {};
    var keySet = !!d.key_set;
    if (!keySet) return { ready: false, live: false, reason: null };
    // Key is set. If the dep exposes reachability, honor it for a truthful "live".
    if (Object.prototype.hasOwnProperty.call(d, "reachable")) {
      if (d.reachable) return { ready: true, live: true, reason: null };
      // Keyed but not reachable → still "ready" to save-and-chat, but flag the reason
      // (localized via error_code when the probe supplies one). authCertain marks a
      // definitive auth rejection (auth_rejected) so the save flow reverts a bad key,
      // vs a transient 'unreachable' blip that keeps the keyed provider selected.
      return { ready: true, live: false, reason: probeReason(d), authCertain: isAuthCertain(d) };
    }
    return { ready: true, live: false, reason: null };
  }

  /** GET /api/v1/system/status → resolves to the parsed JSON (or rejects). */
  function fetchStatus() {
    return fetch(apiBase() + "/api/v1/system/status", { headers: authHeaders(false) })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); });
  }

  /** GET /api/v1/system/credentials → caches the masked {<credKey>:{set,hint}} map.
   *  Lets the connect step skip re-asking for a key that `akana.py setup` already
   *  wrote (.env) — the backend reports it `set` via the store→env fallback. Never
   *  rejects; on any error credState stays null and the empty field shows as before. */
  function fetchCredentials() {
    return fetch(apiBase() + "/api/v1/system/credentials", { headers: authHeaders(false) })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) { credState = (j && j.credentials) || null; return credState; })
      .catch(function () { credState = null; return null; });
  }

  /** GET /api/v1/voice/wake/config → resolves to the server-model `enabled` flag,
   *  cached in serverWakeEnabled. The default wake source is this on-server model
   *  (bundled hey_akana), which needs only getUserMedia — so onboarding can offer
   *  wake on non-Chromium browsers when it's live. Never rejects; on any error the
   *  flag stays false so gating falls back to browser SpeechRecognition support. */
  function fetchServerWakeEnabled() {
    if (serverWakeEnabled !== null) return Promise.resolve(serverWakeEnabled);
    return fetch(apiBase() + "/api/v1/voice/wake/config", { headers: authHeaders(false) })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) { serverWakeEnabled = !!(j && j.enabled); return serverWakeEnabled; })
      .catch(function () { serverWakeEnabled = false; return false; });
  }

  /** PUT /api/v1/system/credentials — SAME endpoint + field names as the
   *  settings panel (akana-settings.js saveCredentials). Returns the masked
   *  payload {credentials:{...}} so the caller can confirm `set`. */
  function saveCredential(credKey, value) {
    var patch = {};
    patch[credKey] = value;
    return fetch(apiBase() + "/api/v1/system/credentials", {
      method: "PUT",
      headers: authHeaders(true),
      body: JSON.stringify(patch),
    }).then(function (r) {
      if (!r.ok) {
        return r.json().catch(function () { return {}; }).then(function (err) {
          throw new Error(parseApiError(err, r.status) || "HTTP " + r.status);
        });
      }
      return r.json().catch(function () { return {}; });
    });
  }

  /** PUT /api/v1/system/llm-settings — switch the ACTIVE provider so a freshly
   *  saved key actually drives chat. Sends ONLY {provider} (the settings-panel
   *  contract: empty fields = "keep" on the backend merge). Best-effort. */
  function switchProvider(provider) {
    return fetch(apiBase() + "/api/v1/system/llm-settings", {
      method: "PUT",
      headers: authHeaders(true),
      body: JSON.stringify({ settings: { provider: provider } }),
    }).then(function (r) { return r.ok; }).catch(function () { return false; });
  }

  function openSettingsKeys() {
    if (window.AkanaSettings && typeof window.AkanaSettings.openSettings === "function") {
      window.AkanaSettings.openSettings("credentials");
      return true;
    }
    return false;
  }

  // ── connect step (THE HEART) ────────────────────────────────────────────────
  function renderConnectPane() {
    bodyEl.innerHTML = "";

    // Live status banner — reflects the current connection at a glance.
    var status = el("div", "aur-onb-status aur-onb-status-loading",
      '<span class="aur-onb-status-dot"></span><span class="aur-onb-status-text">' + _t("onboard.setup_checking") + "</span>");
    bodyEl.appendChild(status);

    // Provider picker (segmented chips).
    var pickBlock = el("div", "aur-onb-block", '<div class="aur-onb-h3">' + _t("onboard.connect_pick") + "</div>");
    var chips = el("div", "aur-onb-provs");
    PROVIDERS.forEach(function (p) {
      var on = p.id === selectedProvider;
      var c = el("button", "aur-onb-prov" + (on ? " on" : ""),
        '<span class="aur-onb-prov-ic">' + p.ic + '</span><span class="aur-onb-prov-name">' + p.label + "</span>");
      c.type = "button";
      c.addEventListener("click", function () {
        selectedProvider = p.id;
        chips.querySelectorAll(".aur-onb-prov").forEach(function (x) { x.classList.remove("on"); });
        c.classList.add("on");
        renderProviderDetail(detail, status);
      });
      chips.appendChild(c);
    });
    pickBlock.appendChild(chips);
    bodyEl.appendChild(pickBlock);

    // Per-provider detail: key field (key) or setup instruction (cli/local).
    var detail = el("div", "aur-onb-block aur-onb-prov-detail");
    bodyEl.appendChild(detail);
    renderProviderDetail(detail, status);

    // Initial status probe (so the banner is truthful on entry).
    refreshConnectStatus(status);
    // Learn which keys are already stored (e.g. written by `akana.py setup`) and
    // re-render so a configured provider shows "connected" instead of a blank field.
    fetchCredentials().then(function () { renderProviderDetail(detail, status); });
  }

  function renderProviderDetail(detail, statusCard) {
    detail.innerHTML = "";
    var p = providerById(selectedProvider);

    var head = el("div", "aur-onb-prov-head",
      '<div class="aur-onb-prov-headic">' + p.ic + "</div>" +
      '<div class="aur-onb-prov-headtext"><div class="aur-onb-prov-headt">' + _t(p.tk) +
      '</div><div class="aur-onb-prov-headd">' + _t(p.dk) + "</div></div>");
    detail.appendChild(head);

    if (p.kind === "key") {
      // Already stored (e.g. by `akana.py setup`)? Show "connected" + a Replace
      // affordance instead of an empty field, so the key isn't asked for twice.
      var cred = credState && credState[p.credKey];
      var alreadySet = !!(cred && cred.set);

      var row = el("div", "aur-onb-keyrow");
      var input = el("input", "aur-onb-key");
      input.type = "password";
      input.autocomplete = "off";
      input.spellcheck = false;
      input.placeholder = _t(p.ph);
      input.setAttribute("aria-label", _t(p.tk));
      var saveBtn = el("button", "aur-onb-keysave", _t("onboard.connect_save"));
      saveBtn.type = "button";
      row.appendChild(input);
      row.appendChild(saveBtn);

      if (alreadySet) {
        var done = el("div", "aur-onb-keydone",
          _t("onboard.connect_already", { hint: cred.hint || _t("onboard.connect_hint_set") }));
        var replace = el("button", "aur-onb-keyreplace", _t("onboard.connect_replace"));
        replace.type = "button";
        row.style.display = "none";
        replace.addEventListener("click", function () {
          done.style.display = "none";
          replace.style.display = "none";
          row.style.display = "";
          input.focus();
        });
        detail.appendChild(done);
        detail.appendChild(replace);
      }
      detail.appendChild(row);

      var note = el("div", "aur-onb-keynote", _t("onboard.connect_keynote"));
      detail.appendChild(note);

      var doSave = function () {
        var val = (input.value || "").trim();
        if (!val) { input.focus(); return; }
        saveBtn.disabled = true;
        input.disabled = true;
        setStatus(statusCard, "loading", _t("onboard.connect_saving"));
        // MIRRORS the recheck path: capture the CURRENTLY-active provider first so
        // we can revert if the just-saved key turns out to be invalid. We save the
        // credential, THEN probe /system/status — and only flip the active provider
        // when the derived state is genuinely usable (ok, or warn WITH a key set).
        // A typo'd key that yields a cta/failed verdict must NOT hijack the active
        // provider away from a previously-working one.
        var priorProvider = null;
        saveCredential(p.credKey, val)
          .then(function (resp) {
            // Refresh the cached masked map so a later re-render reflects the save.
            if (resp && resp.credentials) credState = resp.credentials;
            return fetchStatus();
          })
          .then(function (j) {
            priorProvider = String((j && j.active_provider) || (j && j.model && j.model.provider) || "").toLowerCase();
            // Switch to THIS provider so the status probe reflects the key we just
            // saved (the keyed dependency is evaluated per active provider), then
            // read the verdict. If it's not usable we revert below.
            return switchProvider(p.id);
          })
          .then(function () { return refreshConnectStatus(statusCard); })
          .then(function (state) {
            // refreshConnectStatus resolves to connectState = {ok,live,reason,…}.
            // A verified-live verdict (green) is the only unconditional keep — clear
            // the field and leave THIS provider active.
            if (state && state.ok && state.live) {
              input.value = "";
              return null;
            }
            // Key saved but the live probe reported a failure. Only a DEFINITIVE auth
            // rejection (authCertain — a bad/invalid key or expired/no session) should
            // un-select this provider: revert to priorProvider and surface the reason.
            // A transient 'unreachable' blip (network down, bridge still warming up) on
            // a plausibly-valid key must KEEP this provider selected with an amber
            // warning — reverting there would discard a good key over a momentary blip.
            // Same when there is no reason yet or no prior provider to fall back to.
            var authFailed = !!(state && state.ok && !state.live && state.authCertain);
            if (state && state.ok && !authFailed) {
              // Saved and plausibly usable (no hard auth rejection) → keep active. When
              // the probe DID report a transient reason, keep it visible as an amber
              // warning so the user knows to re-check, but don't un-select the provider.
              input.value = "";
              if (state.reason) {
                setStatus(statusCard, "warn", state.reason);
              }
              return null;
            }
            // authFailed OR a cta/failed verdict (ok:false) → don't leave the broken
            // provider wired up. Revert to whatever was active before, and surface the
            // concrete reason so the user knows the key didn't take.
            setStatus(statusCard, "warn",
              (state && state.reason) || _t("onboard.connect_result_fail", { provider: (state && state.provider) || p.label }));
            if (priorProvider && priorProvider !== p.id) {
              return switchProvider(priorProvider).then(function () { return null; });
            }
            return null;
          })
          .catch(function (e) {
            setStatus(statusCard, "warn", _t("onboard.connect_save_failed", { error: (e && e.message) || e }));
          })
          .then(function () {
            saveBtn.disabled = false;
            input.disabled = false;
          });
      };
      saveBtn.addEventListener("click", doSave);
      input.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); doSave(); } });
    } else {
      // claude (CLI/OAuth) + ollama (local): short setup instruction, no key.
      var hint = el("div", "aur-onb-prov-hint", _t(p.hk));
      detail.appendChild(hint);

      var recheck = el("button", "aur-onb-keysave aur-onb-keysave-ghost", _t("onboard.connect_recheck"));
      recheck.type = "button";
      // Inline verdict line right under the button — the recheck now SAYS what
      // happened (previously it silently no-op'd and the user saw nothing move).
      var result = el("div", "aur-onb-recheck-result");
      result.setAttribute("role", "status");
      result.setAttribute("aria-live", "polite");
      // Inline spacing/legibility only (no CSS file changes): the color is set per
      // verdict below so success/failure read distinctly even without a stylesheet.
      result.style.display = "none";
      result.style.marginTop = "8px";
      result.style.fontSize = "0.9em";
      result.style.lineHeight = "1.45";

      var RESULT_COLOR = { ok: "var(--ok, #3fd99a)", warn: "var(--warn, #ff8a5c)", loading: "" };
      var setResult = function (kind, text) {
        result.className = "aur-onb-recheck-result aur-onb-recheck-" + kind;
        result.style.color = RESULT_COLOR[kind] || "";
        result.style.opacity = kind === "loading" ? "0.75" : "";
        result.textContent = text;   // untrusted probe/error text → textContent only
        result.style.display = "";
      };

      recheck.addEventListener("click", function () {
        recheck.disabled = true;
        recheck.textContent = _t("onboard.connect_rechecking");
        setResult("loading", _t("onboard.connect_rechecking"));
        // For local/cli providers, switch to them first so the probe reflects THIS
        // provider, then re-check /system/status and report the concrete verdict.
        // If the verdict comes back not-ready, revert the active provider so a mere
        // curiosity click on a card doesn't leave a broken provider wired up.
        var priorProvider = null;
        fetchStatus()
          .then(function (j) {
            priorProvider = String((j && j.active_provider) || (j && j.model && j.model.provider) || "").toLowerCase();
            return switchProvider(p.id);
          })
          .then(function (switched) {
            if (!switched) {
              // Couldn't even flip the active provider — say so instead of pretending.
              return { ok: false, live: false, reason: _t("onboard.connect_switch_failed"), _switchFailed: true };
            }
            return refreshConnectStatus(statusCard);
          })
          .then(function (state) {
            var label = (state && state.provider) || p.label;
            if (state && state.ok) {
              // Ready → connected. Distinguish a live-verified reach from a
              // ready-but-not-yet-probed state so the message stays honest.
              if (state.live) {
                setResult("ok", _t("onboard.connect_result_ok", { provider: label }));
              } else {
                setResult("ok", _t("onboard.connect_result_ready", { provider: label }));
              }
              return null;
            }
            // Not reachable — show the concrete reason when the probe gave one, and
            // revert the active provider back to what it was before this recheck.
            var reason = state && state.reason;
            setResult("warn", reason || _t("onboard.connect_result_fail", { provider: label }));
            if (priorProvider && priorProvider !== p.id) return switchProvider(priorProvider);
            return null;
          })
          .catch(function () {
            setResult("warn", _t("onboard.connect_result_fail", { provider: p.label }));
          })
          .then(function () {
            recheck.disabled = false;
            recheck.textContent = _t("onboard.connect_recheck");
          });
      });
      detail.appendChild(recheck);
      detail.appendChild(result);
    }
  }

  function setStatus(card, kind, text) {
    if (!card || !card.isConnected) return;
    card.className = "aur-onb-status aur-onb-status-" + kind;
    // Build the dot + text nodes directly so `text` (which may embed an untrusted
    // backend error string via {error}/{reason}) is set as textContent, never HTML.
    card.innerHTML = '<span class="aur-onb-status-dot"></span><span class="aur-onb-status-text"></span>';
    card.querySelector(".aur-onb-status-text").textContent = text;
  }

  /** PURE: map a /system/status payload to how the connect banner should read.
   *  Kept side-effect-free (no DOM, no _t) so it is unit-testable and is the single
   *  source of truth for the three honest states:
   *    kind "ok"   → provider VERIFIED reachable — the ONLY state that earns green.
   *    kind "warn" → key saved / provider selected but NOT verified (an invalid or
   *                  typo'd key, a missing SDK/bridge, or simply offline). `reason`
   *                  carries the concrete probe error when the backend gave one.
   *    kind "cta"  → not set up (no key / not logged in) → call-to-action.
   *  The old code collapsed "ok" and "warn" into one green "Connected", so an invalid
   *  Cursor key read as connected and chat then 401'd. Exposed for tests. */
  function deriveConnectState(j) {
    var provider = String((j && j.active_provider) || (j && j.model && j.model.provider) || selectedProvider).toLowerCase();
    if (!PROV_LABEL[provider]) provider = "cursor";
    var model = (j && j.model && j.model.active_tag) || "";
    var label = PROV_LABEL[provider];
    var h = providerHealth(j, provider);
    if (h.ready && h.live) return { kind: "ok", ok: true, live: true, reason: null, provider: label, model: model || label };
    // authCertain: the live probe gave a definitive auth rejection (bad key / expired /
    // no session), as opposed to a transient 'unreachable' blip. The connect-save flow
    // reverts a just-saved provider ONLY when authCertain — a blip keeps it selected.
    if (h.ready) return { kind: "warn", ok: true, live: false, reason: h.reason || null, authCertain: !!h.authCertain, provider: label, model: model };
    // `unverifiable` (ollama: local, no probe available) is a NEUTRAL "can't
    // verify" — not a "needs setup" CTA. Route it to warn so the banner reads
    // honestly ("make sure `ollama serve` is running") instead of a false green
    // or a misleading needs-a-key nudge.
    if (h.unverifiable) return { kind: "warn", ok: false, live: false, reason: h.reason || null, provider: label, model: model };
    // authCertain flows onto the cta verdict too (e.g. claude token_expired / no
    // session): the connect-save revert only reads it on the warn/ready path, but
    // exposing it keeps the field consistent and the contract test honest.
    return { kind: "cta", ok: false, live: false, reason: h.reason || null, authCertain: !!h.authCertain, provider: label, model: model };
  }

  /** Probe /system/status and paint the banner. Resolves to the derived
   *  connectState (never rejects) so callers (e.g. the recheck button) can react
   *  to the exact verdict — ready/live/reason — instead of guessing. */
  function refreshConnectStatus(card) {
    setStatus(card, "loading", _t("onboard.setup_checking"));
    return fetchStatus()
      .then(function (j) {
        var s = deriveConnectState(j);
        // authCertain is carried so the connect-save flow can tell a DEFINITIVE auth
        // rejection (revert the just-saved provider) from a transient blip (keep it).
        connectState = { ok: s.ok, live: s.live, reason: s.reason, authCertain: !!s.authCertain, provider: s.provider, model: s.model };
        if (s.kind === "ok") {
          setStatus(card, "ok", _t("onboard.setup_connected", { provider: s.provider, model: s.model || s.provider }));
        } else if (s.kind === "warn") {
          // Two flavors of warn: (a) a key WAS saved but isn't verified yet
          // (ok:true) → "Key saved for {provider}…"; (b) a local provider we
          // simply can't verify (ollama, ok:false) → show its self-contained
          // reason directly (no misleading "Key saved" — ollama takes no key).
          if (!s.ok && s.reason) {
            setStatus(card, "warn", s.reason);
          } else {
            setStatus(card, "warn", s.reason
              ? _t("onboard.setup_saved_unverified_reason", { provider: s.provider, reason: s.reason })
              : _t("onboard.setup_saved_unverified", { provider: s.provider }));
          }
        } else {
          renderSetupCta(card, s.provider, s.reason);
        }
        return connectState;
      })
      .catch(function () {
        connectState = { ok: false, live: false, reason: null, provider: null, model: "" };
        renderSetupCta(card, null, null);
        return connectState;
      });
  }

  function renderSetupCta(card, label, reason) {
    if (!card || !card.isConnected) return;
    card.className = "aur-onb-status aur-onb-status-warn";
    // Prefer a concrete probe reason (e.g. "session token expired") over the
    // generic "needs a key" — a truthful message beats a vague nudge.
    var msg = reason
      ? reason
      : label
        ? _t("onboard.setup_needs_key", { provider: label })
        : _t("onboard.setup_unknown");
    card.innerHTML = '<span class="aur-onb-status-dot"></span><span class="aur-onb-status-text"></span>';
    // textContent (not innerHTML) for the message: a backend error string is
    // untrusted text and must never be interpreted as markup.
    card.querySelector(".aur-onb-status-text").textContent = msg;
    var btn = el("button", "aur-onb-status-btn", _t("onboard.setup_open"));
    btn.type = "button";
    btn.addEventListener("click", function () { openSettingsKeys(); });
    card.appendChild(btn);
  }

  // ── personalize step (name + theme + accent) ────────────────────────────────
  function renderPersonalizePane() {
    bodyEl.innerHTML = "";

    var nameBlock = el("div", "aur-onb-block", '<div class="aur-onb-h3">' + _t("onboard.person_name_label") + "</div>");
    var nameInput = el("input", "aur-onb-name");
    nameInput.type = "text";
    nameInput.autocomplete = "off";
    nameInput.placeholder = _t("onboard.person_name_ph");
    nameInput.value = readName();
    nameInput.setAttribute("aria-label", _t("onboard.person_name_label"));
    // Persist live (debounced via blur + each finish). localStorage is cheap → write on input.
    nameInput.addEventListener("input", function () { setName((nameInput.value || "").trim()); });
    nameBlock.appendChild(nameInput);
    bodyEl.appendChild(nameBlock);

    // Theme.
    var th = readTheme();
    var themeBlock = el("div", "aur-onb-block", '<div class="aur-onb-h3">' + _t("onboard.theme_label") + "</div>");
    var radio = el("div", "aur-onb-theme");
    [["light", _t("onboard.theme_light")], ["dark", _t("onboard.theme_dark")]].forEach(function (pair) {
      var b = el("button", "aur-onb-th" + (th === pair[0] ? " on" : ""),
        '<span class="aur-onb-sw aur-onb-sw-' + pair[0] + '"></span><span>' + pair[1] + "</span>");
      b.type = "button";
      b.addEventListener("click", function () {
        setTheme(pair[0]);
        radio.querySelectorAll(".aur-onb-th").forEach(function (x) { x.classList.remove("on"); });
        b.classList.add("on");
      });
      radio.appendChild(b);
    });
    themeBlock.appendChild(radio);
    bodyEl.appendChild(themeBlock);

    // Accent. 'cyan' is the legacy default for the base bucket — normalize
    // to 'azure' so the base swatch reads as pressed (matches aurora-ui.js).
    var curAcc = root.dataset.accent || "azure";
    if (curAcc === "cyan") curAcc = "azure";
    var accBlock = el("div", "aur-onb-block", '<div class="aur-onb-h3">' + _t("onboard.accent_label") + "</div>");
    var accRow = el("div", "aur-onb-accents");
    ACCENTS.forEach(function (a) {
      var b = el("button", "aur-onb-acc" + (curAcc === a.id ? " on" : ""),
        '<span class="aur-onb-dot" style="background:' + a.hex + '"></span>' + _t(a.tk));
      b.type = "button";
      b.addEventListener("click", function () {
        setAccent(a.id);
        accRow.querySelectorAll(".aur-onb-acc").forEach(function (x) { x.classList.remove("on"); });
        b.classList.add("on");
      });
      accRow.appendChild(b);
    });
    accBlock.appendChild(accRow);
    bodyEl.appendChild(accBlock);
  }

  // ── voice step (optional Hey Akana autostart) ───────────────────────────────
  // The shipped DEFAULT wake source is the on-server openWakeWord model (bundled
  // hey_akana.onnx) — it needs only getUserMedia, which every browser has. Browser
  // SpeechRecognition is a secondary path (Chromium-only). So the toggle is usable
  // whenever EITHER the server model is enabled OR SpeechRecognition is present;
  // only when BOTH are unavailable is wake truly out of reach.
  //   • serverWakeEnabled — null until /voice/wake/config resolves; cached after.
  function renderVoicePane() {
    bodyEl.innerHTML = "";
    var block = el("div", "aur-onb-block", '<div class="aur-onb-h3">' + _t("onboard.voice_h3") + "</div>");
    bodyEl.appendChild(block);

    var speechSupported = ("SpeechRecognition" in window) || ("webkitSpeechRecognition" in window);

    // Paint the toggle for a given wake-usable verdict; re-called when the async
    // wake/config probe lands so a slow endpoint doesn't leave the toggle wrongly
    // disabled on a non-Chromium browser whose server model IS available.
    var paint = function (wakeUsable) {
      block.innerHTML = '<div class="aur-onb-h3">' + _t("onboard.voice_h3") + "</div>";

      var on = readWake() && wakeUsable;
      var toggle = el("button", "aur-onb-toggle" + (on ? " on" : "") + (wakeUsable ? "" : " disabled"),
        '<span class="aur-onb-toggle-text"><span class="aur-onb-toggle-t">' + _t("onboard.voice_toggle_t") +
        '</span><span class="aur-onb-toggle-d">' + _t("onboard.voice_toggle_d") + "</span></span>" +
        '<span class="aur-onb-switch" aria-hidden="true"><span class="aur-onb-switch-knob"></span></span>');
      toggle.type = "button";
      toggle.setAttribute("role", "switch");
      toggle.setAttribute("aria-checked", on ? "true" : "false");
      if (!wakeUsable) {
        // Neither the server model nor SpeechRecognition is available → disable and
        // DON'T enable autostart. Clear any stale flag so such a browser never boots
        // with a wake listener it can't run.
        toggle.disabled = true;
        toggle.setAttribute("aria-disabled", "true");
        setWake(false);
      } else {
        toggle.addEventListener("click", function () {
          var next = !toggle.classList.contains("on");
          toggle.classList.toggle("on", next);
          toggle.setAttribute("aria-checked", next ? "true" : "false");
          setWake(next);
        });
      }
      block.appendChild(toggle);

      // Usable → the mic-permission note. Not usable → explain why (no server model
      // and no Chromium SpeechRecognition to fall back to).
      var note = el("div", "aur-onb-prov-hint",
        wakeUsable ? _t("onboard.voice_note") : _t("onboard.voice_unsupported"));
      block.appendChild(note);
    };

    // First paint uses whatever we already know: if the server model is confirmed
    // enabled (cached), it's usable regardless of the browser; otherwise fall back
    // to SpeechRecognition support until the probe answers.
    paint((serverWakeEnabled === true) || speechSupported);
    fetchServerWakeEnabled().then(function (enabled) {
      // Re-paint only if the pane is still the voice step (user may have navigated).
      if (STEPS[step] && STEPS[step].kind === "voice") paint(enabled || speechSupported);
    });
  }

  // ── feature-card panes (welcome + the educational tour: memory/vault/packs) ──
  // `feats` = [{ic,tk,dk}]; optional `hintKey` renders a "where to find it" line.
  function renderFeaturePane(feats, hintKey) {
    bodyEl.innerHTML = "";
    var grid = el("div", "aur-onb-feats");
    feats.forEach(function (f) {
      grid.appendChild(el("div", "aur-onb-feat",
        '<span class="aur-onb-feat-ic">' + f.ic + "</span>" +
        '<span class="aur-onb-feat-text"><span class="aur-onb-feat-t">' + _t(f.tk) +
        '</span><span class="aur-onb-feat-d">' + _t(f.dk) + "</span></span>"));
    });
    bodyEl.appendChild(grid);
    if (hintKey) bodyEl.appendChild(el("div", "aur-onb-prov-hint", _t(hintKey)));
  }

  function renderWelcomePane() {
    renderFeaturePane([
      { ic: "🖥️", tk: "onboard.welcome_f1_t", dk: "onboard.welcome_f1_d" },
      { ic: "🧠", tk: "onboard.welcome_f2_t", dk: "onboard.welcome_f2_d" },
      { ic: "🔀", tk: "onboard.welcome_f3_t", dk: "onboard.welcome_f3_d" },
    ], "onboard.welcome_data_note");
  }

  // Consolidated feature tour — ONE screen introducing Akana's core features
  // (memory · vault · packs · personas · connectors · voice) as calm, factual
  // per-feature blurbs. Each card says what it does + why it's useful; no hype.
  function renderInsidePane() {
    renderFeaturePane([
      { ic: "🧠", tk: "onboard.inside_mem_t", dk: "onboard.inside_mem_d" },
      { ic: "🔐", tk: "onboard.inside_vault_t", dk: "onboard.inside_vault_d" },
      { ic: "🧩", tk: "onboard.inside_packs_t", dk: "onboard.inside_packs_d" },
      { ic: "🎭", tk: "onboard.inside_persona_t", dk: "onboard.inside_persona_d" },
      { ic: "🔌", tk: "onboard.inside_connectors_t", dk: "onboard.inside_connectors_d" },
      { ic: "🎙️", tk: "onboard.inside_voice_t", dk: "onboard.inside_voice_d" },
    ], "onboard.inside_hint");
  }

  // ── start step (use-case → sample prompts) ──────────────────────────────────
  function renderStartPane() {
    bodyEl.innerHTML = "";

    // Use-case selector (drives which prompts seed the composer).
    var ucBlock = el("div", "aur-onb-block", '<div class="aur-onb-h3">' + _t("onboard.start_usecase_label") + "</div>");
    USECASES.forEach(function (u) {
      var on = u.id === selectedUsecase;
      var c = el("button", "aur-onb-choice" + (on ? " on" : ""),
        '<span class="aur-onb-cic">' + u.ic + '</span><span class="aur-onb-ctext"><span class="aur-onb-ct">' +
        _t(u.tk) + '</span><span class="aur-onb-cd">' + _t(u.dk) + "</span></span>");
      c.type = "button";
      c.addEventListener("click", function () {
        setUsecase(u.id);
        ucBlock.querySelectorAll(".aur-onb-choice").forEach(function (x) { x.classList.remove("on"); });
        c.classList.add("on");
        renderStartPrompts(promptsBlock);
      });
      ucBlock.appendChild(c);
    });
    bodyEl.appendChild(ucBlock);

    // Sample prompts seeded from the chosen use-case — clicking one fills the
    // composer so "Get started" lands the user in a real first message.
    var promptsBlock = el("div", "aur-onb-block", '<div class="aur-onb-h3">' + _t("onboard.try_label") + "</div>");
    bodyEl.appendChild(promptsBlock);
    renderStartPrompts(promptsBlock);
  }

  function renderStartPrompts(block) {
    if (!block) return;
    // Keep the h3 header, replace the prompt buttons.
    var head = block.querySelector(".aur-onb-h3");
    block.innerHTML = "";
    if (head) block.appendChild(head);
    var uc = usecaseById(selectedUsecase);
    uc.prompts.forEach(function (pk) {
      var text = _t(pk);
      var b = el("button", "aur-onb-prompt", "<span>" + text + "</span>");
      b.type = "button";
      b.addEventListener("click", function () {
        if (fillComposer(text)) close(true);
      });
      block.appendChild(b);
    });
  }

  function build() {
    ov = el("div", "aur-onb-backdrop");
    modal = el("div", "aur-onb");
    modal.setAttribute("role", "dialog");
    modal.setAttribute("aria-modal", "true");

    var hero = el("div", "aur-onb-hero",
      '<div class="aur-onb-orb"><span class="aur-onb-orb-core"></span></div>' +
      '<h2 class="aur-onb-title"></h2><p class="aur-onb-lead"></p>');
    modal.appendChild(hero);

    bodyEl = el("div", "aur-onb-body");
    modal.appendChild(bodyEl);

    var foot = el("div", "aur-onb-foot");
    var skip = el("button", "aur-onb-skip");
    skip.type = "button";
    dotsEl = el("div", "aur-onb-dots");  // filled in open() to match STEPS.length
    var back = el("button", "aur-onb-back");
    back.type = "button";
    nextBtn = el("button", "aur-onb-next");
    nextBtn.type = "button";
    var g1 = el("span", "aur-onb-grow"), g2 = el("span", "aur-onb-grow");
    foot.appendChild(skip); foot.appendChild(g1); foot.appendChild(dotsEl); foot.appendChild(g2);
    foot.appendChild(back); foot.appendChild(nextBtn);
    modal.appendChild(foot);
    modal._skipBtn = skip;
    modal._backBtn = back;

    titleEl = hero.querySelector(".aur-onb-title");
    leadEl = hero.querySelector(".aur-onb-lead");

    // Backdrop / Skip / Esc = cancel: restore theme+accent. Skip is a deliberate
    // decline (always marks onboarded); a stray backdrop click or Esc is NOT
    // deliberate unless it happens to land on the last step.
    skip.addEventListener("click", function () { close(false, true); });
    ov.addEventListener("click", function () { close(false); });
    back.addEventListener("click", function () { if (step > 0) show(step - 1); });
    nextBtn.addEventListener("click", function () {
      if (step >= STEPS.length - 1) close(true);
      else show(step + 1);
    });
    keydownHandler = function (e) {
      if (e.key === "Escape" && ov && ov.classList.contains("open")) close(false);
    };
    document.addEventListener("keydown", keydownHandler);

    document.body.appendChild(ov);
    document.body.appendChild(modal);
  }

  function renderPane(kind) {
    if (kind === "welcome") renderWelcomePane();
    else if (kind === "connect") renderConnectPane();
    else if (kind === "personalize") renderPersonalizePane();
    else if (kind === "inside") renderInsidePane();
    else if (kind === "voice") renderVoicePane();
    else renderStartPane();
  }

  function show(i) {
    step = Math.max(0, Math.min(i, STEPS.length - 1));
    var s = STEPS[step];
    titleEl.textContent = s.title;
    leadEl.textContent = s.lead;
    renderPane(s.kind);
    Array.prototype.forEach.call(dotsEl.children, function (d, k) {
      d.className = k === step ? "on" : "";
    });
    if (modal._backBtn) modal._backBtn.style.visibility = step === 0 ? "hidden" : "visible";
    nextBtn.textContent = step === STEPS.length - 1 ? _t("onboard.start") : _t("onboard.next");
  }

  // Steps preview theme/accent LIVE (setTheme/setAccent write to localStorage immediately).
  // "Skip"/Esc/backdrop = cancel → restore the theme and accent that were active on open;
  // only "Get started" (last step) / explicit Skip makes the choice permanent.
  var snapTheme = "light", snapAccent = "azure";
  function snapshotThemeAccent() {
    snapTheme = readTheme();
    var lsAcc = null;
    try { lsAcc = localStorage.getItem("cockpit:accent"); } catch (e) {}
    snapAccent = root.dataset.accent || lsAcc || "azure";
  }
  function restoreThemeAccent() {
    setTheme(snapTheme);
    setAccent(snapAccent);
  }

  /** Best-effort: seed the user's name as a memory fact (localStorage is the
   *  source of truth; this just helps Akana address them). Never throws. */
  function seedNameMemory(name) {
    if (!name) return;
    try {
      if (window.AkanaMemoryApi && typeof window.AkanaMemoryApi.createFact === "function") {
        window.AkanaMemoryApi.createFact({
          // Must land as `preference:preferred_name` — the exact key the greeting
          // reader (akana-shell.js fetchUserFirstName) + recall look for; the
          // «{name}» value lets its pickName() extract the name.
          key: "preferred_name",
          value: _t("onboard.person_memory_value", { name: name }),
          kind: "preference",
        }).catch(function () {});
      }
    } catch (e) {}
  }

  function open() {
    if (!modal) build();
    // Fresh strings every open (language may have changed since build()).
    STEPS = makeSteps();
    // Progress dots track the step count (the tour adds steps) — rebuild to match.
    dotsEl.innerHTML = "";
    for (var di = 0; di < STEPS.length; di++) dotsEl.appendChild(el("span"));
    if (modal._skipBtn) modal._skipBtn.textContent = _t("onboard.skip");
    if (modal._backBtn) modal._backBtn.textContent = _t("onboard.back");
    modal.setAttribute("aria-label", _t("onboard.modal_aria"));
    selectedUsecase = readUsecase();
    snapshotThemeAccent();
    show(0);
    ov.classList.add("open");
    modal.classList.add("open");
  }

  function close(commit, skipped) {
    if (!commit) restoreThemeAccent();
    if (ov) ov.classList.remove("open");
    if (modal) modal.classList.remove("open");
    // Mark onboarded ONLY on a deliberate finish/skip — early backdrop/Esc
    // dismissals (commit === false, skipped !== true, on a non-final step) leave
    // 'akana.onboarded' unset so the wizard can re-auto-open.
    var deliberate = commit || skipped || step >= STEPS.length - 1;
    if (deliberate) {
      // Persist the name + best-effort seed memory on a real finish.
      var nm = readName();
      if (commit && nm) seedNameMemory(nm);
      try { localStorage.setItem("akana.onboarded", "1"); } catch (e) {}
    }
  }

  /** Full teardown — removes DOM + the document keydown listener (no leak). */
  function destroy() {
    if (keydownHandler) {
      document.removeEventListener("keydown", keydownHandler);
      keydownHandler = null;
    }
    if (ov && ov.parentNode) ov.parentNode.removeChild(ov);
    if (modal && modal.parentNode) modal.parentNode.removeChild(modal);
    ov = modal = null;
  }

  // Re-render an OPEN wizard when the language changes underneath it. The boot-time
  // backend reconcile (akana-i18n.js) can flip en↔tr AFTER the wizard opened on a
  // fresh browser whose localStorage cache was empty — without this the cached STEPS
  // hero copy + the skip/back/aria stay in the stale language for the whole session.
  function onLanguageChange() {
    if (!ov || !ov.classList.contains("open")) return;
    STEPS = makeSteps();
    if (modal && modal._skipBtn) modal._skipBtn.textContent = _t("onboard.skip");
    if (modal && modal._backBtn) modal._backBtn.textContent = _t("onboard.back");
    if (modal) modal.setAttribute("aria-label", _t("onboard.modal_aria"));
    show(step);   // repaints title/lead + the active pane body in the new language
  }

  function init() {
    window.addEventListener("akana:languagechange", onLanguageChange);
    var seen = "1";
    try { seen = localStorage.getItem("akana.onboarded"); } catch (e) {}
    if (seen) return;
    // Auto-open only once the language is settled, so a fresh browser after a
    // Turkish CLI setup opens the wizard directly in the CLI-chosen language
    // instead of flashing English and then reconciling mid-flow. Cap the wait so a
    // slow/absent backend still opens the wizard (in the localStorage/default lang).
    var ready = window.AkanaI18n && window.AkanaI18n.ready;
    if (ready && typeof ready.then === "function") {
      var opened = false;
      var openOnce = function () { if (!opened) { opened = true; open(); } };
      var timer = setTimeout(openOnce, 1500);
      ready.then(function () { clearTimeout(timer); openOnce(); });
    } else {
      setTimeout(open, 450);
    }
  }

  // _deriveConnectState is exposed for the connect-state contract test (pure, DOM-free).
  // _show lets the i18n render harness walk every pane (open() only shows step 0) so
  // its leak scan reaches the personalize/voice panes where hardcoded copy hid.
  window.auroraOnboard = {
    open: open, close: close, destroy: destroy,
    _deriveConnectState: deriveConnectState,
    _show: function (i) { if (modal) show(i); },
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init, { once: true });
  } else {
    init();
  }
})();
