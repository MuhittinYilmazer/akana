/**
 * Akana shell — orb, composer hint, log append, sticky scroll, keyboard help (loaded before voice/chat).
 */
const _t = (k, p) => window.AkanaI18n?.t(k, p) ?? k;
(() => {
  let hooks = {
    log: null,
    logScroll: null,
    logEmpty: null,
    msg: null,
    form: null,
    orb: null,
    escapeHtml: (s) => window.AkanaCore.escapeHtml(s),
    cancelVoiceActivity: () => window.AkanaVoice?.cancelVoiceActivity?.() ?? false,
    openSettings: () => window.AkanaSettings?.openSettings?.(),
  };

  let _activeCursorModel = "?";
  let _composerHintState = "idle";
  let _wired = false;
  let _userFirstName = ""; // cached display name from memory ("" = unknown)
  let _userNamePromise = null; // de-dupes the one-shot memory fetch

  // ── PER-CONVERSATION PANE (parallel-chat core) ──────────────────────────────
  // #log is now the PANE CONTAINER; each conversation lives in its own <div.conv-pane>.
  // Foreground append + empty-state target the DISPLAYED pane; switching = show/hide
  // (NO innerHTML="" ever). Replaces the old single-log + wipe + reattach model
  // (bug: "no response when returning to new chat"). Falls back to #log when
  // PaneManager is not loaded → backwards compatible.
  let _panes = null;
  function ensurePanes() {
    if (_panes || !hooks.log || !window.AkanaChatPanes?.createPaneManager) return;
    _panes = window.AkanaChatPanes.createPaneManager({
      container: hooks.log,
      createEl: (t) => document.createElement(t),
      // A pane with an ACTIVE stream in the background is EXEMPT from LRU eviction —
      // keep live rows in the DOM (eviction OK once the stream ends;
      // on revisit the pane is re-hydrated from store/server).
      isProtected: (convId) =>
        Boolean(convId && window.AkanaChat?.isConversationStreamActive?.(convId)),
    });
    _panes.show(null); // empty-new conversation pane — always have a displayed pane
  }
  /** Foreground render target: the displayed pane (falls back to container). */
  function currentPane() {
    return (_panes && _panes.displayedPane()) || hooks.log;
  }

  const kbdHelpEl = () => document.getElementById("kbd-help");
  const kbdHelpBackdrop = () => document.getElementById("kbd-help-backdrop");
  const kbdHelpClose = () => document.getElementById("kbd-help-close");

  function init(h) {
    hooks = { ...hooks, ...h };
    ensurePanes();
    if (!_wired) {
      _wired = true;
      wireKbdHelp();
      wirePromptChips();
      wireShortcutKeys();
      wireComposer();
      wireDraftPersistence();
      wireGreeting();
      wireScrollFab();
      wireCodeCopy();
    }
  }

  // Time-based greeting — only runs while the default title is still shown, so
  // server-customised titles are not overwritten. No #log-empty in memory.html → silent no-op.
  function wireGreeting() {
    const title = document.querySelector("#log-empty .log-empty-title");
    if (!title || title.textContent.trim() !== _t("shell.greeting_default")) return;
    const h = new Date().getHours();
    let selam;
    if (h >= 5 && h < 12) selam = _t("shell.greeting_morning");
    else if (h >= 12 && h < 17) selam = _t("shell.greeting_afternoon");
    else if (h >= 17 && h < 22) selam = _t("shell.greeting_evening");
    else selam = _t("shell.greeting_night");
    // If the name is unknown, start with Akana's intro; once the name arrives,
    // greet the user by name ("Good evening Alice") and drop the intro.
    const base = `${selam} ${_t("shell.greeting_base")}`;
    title.textContent = base;
    // Personal greeting: first name from the "name" fact in memory. Does not
    // block boot — if a name arrives and the title is still ours, upgrade it;
    // any error is a silent no-op.
    ensureUserFirstName()
      .then((name) => {
        if (name && title.textContent === base) {
          title.textContent = `${selam} ${name}`;
        }
      })
      .catch(() => {});
  }

  // User's name: GET /api/v1/memory/facts (term LIKE search, NOT vector).
  // The name can be stored several ways depending on how it was captured:
  //   • onboarding  → key="preference:preferred_name", value="… «Alice» …"
  //   • chat capture → key="ad", value="Alice"
  //   • chat remember → key="kullanıcının adı alice", value="Kullanıcının adı Alice."
  // So we must be robust about BOTH which fact we pick AND how we read the name.
  // 20-character cap; returns empty string if not found → caller falls back to "I'm Akana".
  async function fetchUserFirstName() {
    const core = window.AkanaCore;
    if (!core || typeof core.baseUrl !== "function") return "";
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), 5000);
    // Two precise-first queries, merged — robust in BOTH languages without the
    // loose "ad" substring flooding the 50-row window. The server processes a
    // query's expanded terms (terms.py) in order until the window fills, so the
    // LEADING term must be the precise one or the name fact gets crowded out by
    // junk ("read", "address", "kadar"…). • "name" → the canonical
    //   preference:preferred_name (its key contains "name") + English facts.
    //   • "adı" → Turkish chat-captured facts ("Kullanıcının adı Alex").
    // A single multi-word query like "ad isim name" instead drops the 2-char
    // "ad" and skips alias expansion, so it matched nothing at all.
    const fetchFacts = async (q) => {
      try {
        const url =
          `${core.baseUrl()}/api/v1/memory/facts` +
          `?q=${encodeURIComponent(q)}&limit=50`;
        const r = await fetch(url, { headers: core.authHeaders(false), signal: ctl.signal });
        if (!r.ok) return [];
        const data = await r.json();
        return Array.isArray(data?.items) ? data.items : [];
      } catch {
        return [];
      }
    };
    try {
      // Language-gate the startup query: run ONLY the active language's precise
      // term (EN → "name", TR → "adı") instead of both unconditionally. Default to
      // English when i18n is unavailable. The canonical preference:preferred_name
      // (checked first below) is language-neutral, so a single term still finds it.
      const lang = window.AkanaI18n?.getLanguage?.() || "en";
      const queries = lang === "tr" ? ["adı"] : ["name"];
      const lists = await Promise.all(queries.map(fetchFacts));
      const seen = new Set();
      const items = [];
      for (const list of lists) {
        for (const it of list) {
          if (it && it.is_valid !== false && !seen.has(it.id)) {
            seen.add(it.id);
            items.push(it);
          }
        }
      }
      if (!items.length) return "";
      // Read a first name out of a value that may be a bare name ("Alice"), a
      // quoted directive ("Address user as «Alice».") or a full sentence
      // ("Kullanıcının adı Alice." / "The user's name is Alice").
      const pickName = (raw) => {
        const v = String(raw || "").trim();
        if (!v) return "";
        // 1) Quoted name wins: «Alice» / "Alice" / 'Alice'.
        const quoted = v.match(/[«"'“‘]\s*([^»"'”’]+?)\s*[»"'”’]/);
        if (quoted && quoted[1].trim()) return quoted[1].trim().split(/\s+/)[0];
        // 2) Single bare token → that's the name.
        const words = v.replace(/[.,;:!?]+$/g, "").split(/\s+/).filter(Boolean);
        if (words.length === 1) return words[0];
        // 3) Token right after a name-indicator ("…adı/adım/ismim/isim X",
        //    "name is X", "named X", "call me X").
        const after = v.match(
          /(?:ad[ıi]m|ad[ıi]|ismim|isim|name['’]?s|name\s+is|named|call\s+me)\s*[:=]?\s+([^\s.,;:!?"'«»“”‘’]+)/iu,
        );
        if (after && after[1]) return after[1];
        // 4) Names are capitalized and usually trail the sentence → last Capitalized word.
        const caps = words.filter((w) => /^[\p{Lu}]/u.test(w));
        if (caps.length) return caps[caps.length - 1];
        // 5) Fallback: first word.
        return words[0] || "";
      };
      const keyOf = (it) => String(it.key || "").trim().toLowerCase();
      // Language-gate the fact-key/value matching to the SAME lang as the query
      // above: Turkish → Turkish name keys/patterns; English → English ("name",
      // keeping "ad"/"isim" as legacy fallbacks). Default English when i18n absent.
      const nameKeys =
        lang === "tr"
          ? new Set(["ad", "adı", "adım", "isim", "ismim"])
          : new Set(["name", "ad", "isim"]);
      // A name word appearing as a token inside the key or value (covers
      // sentence-derived keys like "kullanıcının adı alice").
      const nameish =
        lang === "tr"
          ? /(?:^|[:\s])(?:ad[ıi]m|ad[ıi]|ismim|isim|ad)(?:[:\s]|$)/u
          : /(?:^|[:\s])(?:name|ad|isim)(?:[:\s]|$)/u;
      // Priority: canonical preferred_name → exact name key → any name-ish fact.
      const chosen =
        items.find((it) => /(?:^|:)preferred_name$/.test(keyOf(it))) ||
        items.find((it) => nameKeys.has(keyOf(it))) ||
        items.find(
          (it) => nameish.test(keyOf(it)) || nameish.test(String(it.value || "").toLowerCase()),
        );
      if (!chosen) return "";
      const first = pickName(chosen.value);
      if (!first) return "";
      const t = first.slice(0, 20);
      return t.charAt(0).toLocaleUpperCase(lang === "tr" ? "tr-TR" : undefined) + t.slice(1);
    } finally {
      clearTimeout(timer);
    }
  }

  // Label + avatar initial for the local user's chat rows. Known name →
  // {label:"Alice", initial:"M"}. Unknown → language-driven fallback
  // {label:"You"/"Sen", initial:"Y"/"S"}: the initial is just the first letter
  // of the localized label, so it tracks the active language automatically.
  function userIdentity() {
    if (_userFirstName) {
      const lang = window.AkanaI18n?.getLanguage?.() || "en";
      return {
        label: _userFirstName,
        initial: _userFirstName.charAt(0).toLocaleUpperCase(lang === "tr" ? "tr-TR" : undefined),
      };
    }
    const label = _t("shell.msg_label_you");
    return { label, initial: (label.charAt(0) || "?").toLocaleUpperCase() };
  }

  // Fetch the name once and cache it. Restored history renders before the async
  // name resolves, so on success retro-fit any user rows already in the DOM.
  function ensureUserFirstName() {
    if (_userNamePromise) return _userNamePromise;
    _userNamePromise = fetchUserFirstName()
      .then((name) => {
        _userFirstName = name || "";
        if (_userFirstName) refreshUserRowsIdentity();
        return _userFirstName;
      })
      .catch(() => "");
    return _userNamePromise;
  }

  // Update avatar letter + label on existing ".row-user" rows once the name lands.
  function refreshUserRowsIdentity() {
    const id = userIdentity();
    document.querySelectorAll(".row-user").forEach((row) => {
      const av = row.querySelector(".msg-avatar");
      const lbl = row.querySelector(".msg-label");
      if (av) av.textContent = id.initial;
      if (lbl) lbl.textContent = id.label;
    });
  }

  // "Scroll down" floating arrow — appears when the user scrolls up, smoothly
  // scrolls to bottom on click. Not mounted when #log-scroll is absent (memory.html).
  function wireScrollFab() {
    const scroller = hooks.logScroll || document.getElementById("log-scroll");
    const panel = scroller?.parentElement;
    if (!scroller || !panel) return;
    if (panel.querySelector(".akana-fab-down")) return;

    const fab = document.createElement("button");
    fab.type = "button";
    fab.className = "akana-fab-down";
    fab.setAttribute("aria-label", _t("shell.fab_scroll_down_aria"));
    fab.innerHTML =
      '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" aria-hidden="true">' +
      '<path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>' +
      "</svg>";
    panel.appendChild(fab);

    // rAF-throttle: at most one measurement per scroll; visible above ~240px from bottom
    let fabRaf = null;
    const updateFab = () => {
      fabRaf = null;
      const away = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
      fab.classList.toggle("is-visible", away > 240);
    };
    scroller.addEventListener(
      "scroll",
      () => {
        if (fabRaf == null) fabRaf = requestAnimationFrame(updateFab);
      },
      { passive: true },
    );

    // Consuming pointerdown prevents stealing focus from the textarea; we don't
    // touch follow logic — scrolling to the bottom already restores "following" state.
    fab.addEventListener("pointerdown", (e) => e.preventDefault());
    fab.addEventListener("click", () => {
      scroller.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" });
      fab.classList.remove("is-visible");
    });
  }

  function isTypingShortcutTarget(el) {
    if (!el || !(el instanceof Element)) return false;
    const tag = el.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
    return el.isContentEditable;
  }

  function setOrb(state) {
    const orb = hooks.orb;
    if (!orb) return;
    orb.classList.remove("orb-idle", "orb-send", "orb-ok", "orb-err", "orb-listen");
    orb.classList.add(`orb-${state}`);
  }

  // Active model LABEL shown in the composer hint. Settings fills this in a
  // provider-aware way (e.g. "Claude · opus-4" / "Cursor · …") — previously
  // "Cursor ·" was hardcoded here, showing the wrong provider when Claude/Ollama
  // was selected. Empty/unknown → "?" (caller falls back to "pick a model").
  function setActiveCursorModel(m) {
    _activeCursorModel = m || "?";
    setComposerHint(_composerHintState);
  }

  function setComposerHint(state) {
    const el = document.getElementById("composer-hint");
    if (!el) return;
    _composerHintState = state || "idle";
    const m = _activeCursorModel;
    const known = m && m !== "?";
    el.classList.remove("composer-hint--thinking", "composer-hint--listening", "composer-hint--speaking");
    switch (_composerHintState) {
      case "listening":
        el.textContent = _t("shell.hint_listening");
        el.classList.add("composer-hint--listening");
        break;
      case "thinking":
        el.textContent = known ? `${m} · ${_t("shell.hint_thinking")}` : _t("shell.hint_thinking");
        el.classList.add("composer-hint--thinking");
        break;
      case "speaking":
        el.textContent = _t("shell.hint_speaking");
        el.classList.add("composer-hint--speaking");
        break;
      case "idle":
      default:
        el.textContent = known ? m : _t("composer.hint_pick_model");
        break;
    }
  }

  function _isNearBottom(scroller, slackPx = 80) {
    if (!scroller) return true;
    return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight <= slackPx;
  }

  let _stickScrollRaf = null;
  let _stickScrollTarget = null;

  function stickToBottomIfFollowing(scroller) {
    if (!scroller) return;
    _stickScrollTarget = scroller;
    if (_stickScrollRaf != null) return;
    _stickScrollRaf = requestAnimationFrame(() => {
      _stickScrollRaf = null;
      const el = _stickScrollTarget;
      if (el && _isNearBottom(el)) {
        el.scrollTop = el.scrollHeight;
      }
    });
  }

  function updateEmptyState() {
    const log = currentPane();
    const logEmpty = hooks.logEmpty;
    if (!logEmpty) return;
    if (logEmpty.dataset.loading === "1") {
      logEmpty.hidden = true;
      return;
    }
    logEmpty.hidden = !log || log.children.length > 0;
  }

  function setLogLoading(loading) {
    const logEmpty = hooks.logEmpty;
    if (!logEmpty) return;
    if (loading) {
      logEmpty.dataset.loading = "1";
      logEmpty.hidden = true;
    } else {
      delete logEmpty.dataset.loading;
      updateEmptyState();
    }
  }

  function scrollLogToBottom(scroller) {
    const el = scroller || hooks.logScroll || hooks.log;
    if (!el) return;
    _clearTailGap(); // reset the send-pin padding + anchor (normal scroll-to-bottom)
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        el.scrollTop = el.scrollHeight;
      });
    });
  }

  // ── On send: "pin message to top + exact-fit gap at bottom" ─────────────────
  // User feedback: a fixed 1-viewport padding was too much and the gap was scrollable.
  // Fix: DYNAMICALLY compute the gap → total scrollHeight is exactly
  // "anchorTop + 1 viewport". So the message lands at most at the top (no overflow
  // into the gap) and as the response streams (content grows) the gap SHRINKS ON
  // ITS OWN → zero for a long response. ResizeObserver watches stream growth.
  let _topAnchorRow = null;
  let _tailGapObserver = null;

  function _resizeTailGap() {
    const scroller = hooks.logScroll || document.getElementById("log-scroll");
    if (!scroller || !_topAnchorRow || !_topAnchorRow.isConnected) return;
    const prevPad = parseFloat(scroller.style.paddingBottom) || 0;
    const naturalHeight = scroller.scrollHeight - prevPad; // content height without padding
    const anchorTop =
      _topAnchorRow.getBoundingClientRect().top -
      scroller.getBoundingClientRect().top +
      scroller.scrollTop; // anchor distance from content top (scroll-independent)
    const gap = Math.max(0, anchorTop + scroller.clientHeight - naturalHeight);
    scroller.style.paddingBottom = gap ? `${gap}px` : "";
  }

  function _clearTailGap() {
    _topAnchorRow = null;
    if (_tailGapObserver) _tailGapObserver.disconnect();
    const scroller = hooks.logScroll || document.getElementById("log-scroll");
    if (scroller && scroller.style) scroller.style.paddingBottom = "";
  }

  function scrollNewTurnToTop(row) {
    const scroller = hooks.logScroll || document.getElementById("log-scroll");
    if (!scroller || !row || typeof row.getBoundingClientRect !== "function") return;
    _topAnchorRow = row;
    if (typeof ResizeObserver !== "undefined") {
      if (!_tailGapObserver) _tailGapObserver = new ResizeObserver(() => _resizeTailGap());
      else _tailGapObserver.disconnect();
      const pane = row.parentElement || currentPane();
      if (pane) _tailGapObserver.observe(pane); // shrink gap as the response streams in
    }
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (_topAnchorRow !== row) return; // another turn/reset happened in between — bail
        _resizeTailGap(); // set exact-fit gap so the message can reach the top
        const delta = row.getBoundingClientRect().top - scroller.getBoundingClientRect().top;
        scroller.scrollTop += delta; // align the user row to the top of the scroller
      });
    });
  }

  function resizeComposer() {
    const msg = hooks.msg;
    if (!msg) return;
    // Empty textarea: don't let the placeholder (especially a long mobile hint)
    // inflate scrollHeight and grow the box to 2 lines → leave rows=1 at natural height.
    if (!msg.value) {
      msg.style.height = "";
      return;
    }
    msg.style.height = "auto";
    msg.style.height = `${Math.min(msg.scrollHeight, 160)}px`;
  }

  function appendRow(html, rowClass = "") {
    const log = currentPane();
    if (!log) return null;
    const scroller = hooks.logScroll || log;
    const wasFollowing = _isNearBottom(scroller);
    const wrap = document.createElement("div");
    wrap.className = rowClass ? `row ${rowClass}` : "row";
    wrap.innerHTML = html;
    log.appendChild(wrap);
    updateEmptyState();
    if (wasFollowing) scroller.scrollTop = scroller.scrollHeight;
    return wrap;
  }

  // Message bubble attachments — ids are fetched as AUTHORISED blobs from
  // /uploads/{id}/raw (<img> cannot send Authorization headers), then converted
  // to object URLs. image/* → thumbnail; otherwise a file badge. URLs are cached
  // per id (no re-fetch on sync re-render). The cache is a BOUNDED LRU: object URLs
  // pin their blob in memory until revoked, so an unbounded session cache leaked
  // memory steadily. On eviction (and on pagehide) the URL(s) are revoked; an evicted
  // attachment is simply re-fetched on the next render (cache miss → new URL).
  const _MSG_ATTACH_CACHE_MAX = 100;
  const _msgAttachUrlCache = new Map(); // id -> {url, type, thumbUrl?}

  function _revokeAttachEntry(entry) {
    if (!entry) return;
    try {
      if (entry.url) URL.revokeObjectURL(entry.url);
    } catch {
      /* ignore */
    }
    // thumbUrl is a Promise<string|null> (a SEPARATE object URL from renderPdfThumb).
    if (entry.thumbUrl) {
      Promise.resolve(entry.thumbUrl)
        .then((u) => {
          if (u) URL.revokeObjectURL(u);
        })
        .catch(() => {});
    }
  }

  function _attachEntryInUse(entry) {
    // b29: is this cached object URL still displayed by a CONNECTED <img>? If so, evicting +
    // revoking it would break the visible image. (blob: URLs contain no '"', so the quoted
    // attribute selector is safe.)
    if (!entry || !entry.url) return false;
    try {
      return !!document.querySelector(`img[src="${entry.url}"]`);
    } catch {
      return false;
    }
  }

  async function fetchMsgAttachment(id) {
    const cached = _msgAttachUrlCache.get(id);
    if (cached) {
      // LRU touch: re-insert so the most-recently used entry is last (evicted last).
      _msgAttachUrlCache.delete(id);
      _msgAttachUrlCache.set(id, cached);
      return cached;
    }
    const core = window.AkanaCore;
    if (!core?.baseUrl || !core?.authHeaders) return null;
    try {
      const r = await fetch(
        `${core.baseUrl()}/api/v1/uploads/${encodeURIComponent(id)}/raw`,
        { headers: core.authHeaders() },
      );
      if (!r.ok) return null;
      const blob = await r.blob();
      const entry = { url: URL.createObjectURL(blob), type: blob.type || "" };
      _msgAttachUrlCache.set(id, entry);
      // Evict + revoke the oldest object URL(s) once over the cap — but NEVER revoke a URL still
      // referenced by a connected <img> (b29: revoking it would break the visible image). Skip
      // in-use entries (oldest-first); if all remaining are in use, the cache stays slightly over
      // cap until one frees (bounded — in-use entries are, by definition, currently rendered).
      if (_msgAttachUrlCache.size > _MSG_ATTACH_CACHE_MAX) {
        for (const [key, ent] of [..._msgAttachUrlCache]) {
          if (_msgAttachUrlCache.size <= _MSG_ATTACH_CACHE_MAX) break;
          if (key === id || _attachEntryInUse(ent)) continue;
          _msgAttachUrlCache.delete(key);
          _revokeAttachEntry(ent);
        }
      }
      return entry;
    } catch {
      return null;
    }
  }

  // Revoke ALL cached object URLs when the page is being unloaded/hidden (the strong
  // session-leak guard — the cache otherwise outlives nothing useful on navigation).
  window.addEventListener("pagehide", () => {
    for (const entry of _msgAttachUrlCache.values()) _revokeAttachEntry(entry);
    _msgAttachUrlCache.clear();
  });

  // pdf.js (vendored) is lazy-loaded only when a PDF preview is needed.
  // .js extension is MIME-safe (comes via ES module import()); worker is also vendored.
  // Load/processing error → null → caller falls back to the 📄 badge.
  let _pdfLibPromise = null;
  function loadPdfLib() {
    if (_pdfLibPromise) return _pdfLibPromise;
    _pdfLibPromise = import("/static/vendor/pdfjs/pdf.min.js")
      .then((lib) => {
        try {
          lib.GlobalWorkerOptions.workerSrc = "/static/vendor/pdfjs/pdf.worker.min.js";
        } catch {
          /* ignore */
        }
        return lib;
      })
      .catch((e) => {
        _pdfLibPromise = null; // retry on next attempt
        throw e;
      });
    return _pdfLibPromise;
  }

  /** Render page 1 of a PDF to a thumbnail → object URL (or null). ``source``:
   *  URL string / Blob / ArrayBuffer. All errors return null silently (caller
   *  falls back to the 📄 badge) — a pdf.js API mismatch must not break the whole render. */
  // PDF render is bounded by a TIMEOUT (EC4: dev/broken PDF must not hang). If the
  // timeout fires, returns null (caller falls back to 📄); late-arriving URLs are not leaked.
  const PDF_RENDER_TIMEOUT_MS = 8000;
  function renderPdfThumb(source, maxPx = 240) {
    let timedOut = false;
    const inner = _renderPdfThumbInner(source, maxPx).then((url) => {
      if (timedOut && url) {
        try {
          URL.revokeObjectURL(url);
        } catch {
          /* ignore */
        }
        return null;
      }
      return url;
    });
    const timeout = new Promise((resolve) =>
      setTimeout(() => {
        timedOut = true;
        resolve(null);
      }, PDF_RENDER_TIMEOUT_MS),
    );
    return Promise.race([inner, timeout]);
  }

  async function _renderPdfThumbInner(source, maxPx = 240) {
    try {
      const lib = await loadPdfLib();
      let params;
      if (typeof source === "string") params = { url: source };
      else if (source instanceof Blob) params = { data: new Uint8Array(await source.arrayBuffer()) };
      else if (source instanceof ArrayBuffer) params = { data: new Uint8Array(source) };
      else return null;
      const doc = await lib.getDocument(params).promise;
      try {
        const page = await doc.getPage(1);
        const base = page.getViewport({ scale: 1 });
        const scale = Math.min(maxPx / base.width, maxPx / base.height) || 1;
        const viewport = page.getViewport({ scale: scale > 0 ? scale : 1 });
        const canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.ceil(viewport.width));
        canvas.height = Math.max(1, Math.ceil(viewport.height));
        await page.render({ canvasContext: canvas.getContext("2d"), viewport }).promise;
        const blob = await new Promise((res) => canvas.toBlob(res, "image/png"));
        return blob ? URL.createObjectURL(blob) : null;
      } finally {
        try {
          doc.destroy();
        } catch {
          /* ignore */
        }
      }
    } catch {
      return null;
    }
  }

  function appendThumbImage(box, url, alt) {
    const img = document.createElement("img");
    img.className = "msg-image";
    img.src = url;
    img.alt = alt || _t("shell.attach_image_alt");
    img.loading = "lazy";
    img.decoding = "async";
    // EC7: broken/unloadable image → fall back to file badge (instead of broken-image icon).
    img.addEventListener("error", () => {
      img.remove();
      appendFileChip(box);
    });
    box.appendChild(img);
  }

  function appendFileChip(box) {
    const chip = document.createElement("span");
    chip.className = "msg-file-chip";
    chip.textContent = _t("shell.attach_file_chip");
    box.appendChild(chip);
  }

  function renderMessageAttachments(msgBody, fileIds) {
    if (!msgBody || !Array.isArray(fileIds) || !fileIds.length) return;
    const box = document.createElement("div");
    box.className = "msg-attachments";
    msgBody.appendChild(box);
    for (const id of fileIds) {
      void fetchMsgAttachment(id).then(async (entry) => {
        if (!entry) return;
        if (entry.type.startsWith("image/")) {
          appendThumbImage(box, entry.url);
        } else if (entry.type === "application/pdf") {
          // Store the page-1 preview PROMISE synchronously → concurrent renders
          // (optimistic echo + sync re-render) share the same promise; no double
          // render / leak. renderPdfThumb never rejects (error/timeout → null → 📄 badge).
          if (entry.thumbUrl === undefined) entry.thumbUrl = renderPdfThumb(entry.url);
          const thumb = await entry.thumbUrl;
          if (thumb) appendThumbImage(box, thumb, _t("shell.pdf_preview_alt"));
          else appendFileChip(box);
        } else {
          appendFileChip(box);
        }
      });
    }
  }

  function appendUserMessage(text, fileIds) {
    const escapeHtml = hooks.escapeHtml;
    ensureUserFirstName(); // lazy one-shot; refreshes this row if the name lands later
    const id = userIdentity();
    const row = appendRow(
      `<div class="msg-avatar" aria-hidden="true">${escapeHtml(id.initial)}</div>
       <div class="msg-body">
         <div class="msg-label">${escapeHtml(id.label)}</div>
         <div class="bubble bubble-user">${escapeHtml(text)}</div>
       </div>`,
      "row-user",
    );
    const body = row?.querySelector(".msg-body");
    if (body && Array.isArray(fileIds) && fileIds.length) {
      renderMessageAttachments(body, fileIds);
    }
    window.AkanaChat?.chatRecordMessage?.({ kind: "user", text, fileIds: fileIds || [] });
    return row;
  }

  function appendSystemNotice(text) {
    const escapeHtml = hooks.escapeHtml;
    const row = appendRow(
      `<div class="msg-avatar" aria-hidden="true">·</div>
       <div class="msg-body">
         <div class="msg-label">${_t("shell.msg_label_system")}</div>
         <div class="bubble bubble-assistant">${escapeHtml(text)}</div>
       </div>`,
      "row-assistant",
    );
    window.AkanaChat?.chatRecordMessage?.({ kind: "system", text });
    return row;
  }

  function shortConversationId(id) {
    if (!id) return _t("shell.conv_id_none");
    if (id.length <= 14) return id;
    return `${id.slice(0, 8)}…${id.slice(-4)}`;
  }

  function openKbdHelp() {
    const el = kbdHelpEl();
    if (!el) return;
    try {
      if (typeof el.showModal === "function") el.showModal();
      else el.setAttribute("open", "");
    } catch {
      el.setAttribute("open", "");
    }
    const backdrop = kbdHelpBackdrop();
    if (backdrop) backdrop.classList.add("is-open");
  }

  function closeKbdHelp() {
    const el = kbdHelpEl();
    if (!el) return;
    try {
      if (typeof el.close === "function") el.close();
      else el.removeAttribute("open");
    } catch {
      el.removeAttribute("open");
    }
    const backdrop = kbdHelpBackdrop();
    if (backdrop) backdrop.classList.remove("is-open");
  }

  function wireKbdHelp() {
    const closeBtn = kbdHelpClose();
    const backdrop = kbdHelpBackdrop();
    if (closeBtn) closeBtn.addEventListener("click", closeKbdHelp);
    if (backdrop) backdrop.addEventListener("click", closeKbdHelp);
  }

  // Hero suggestion cards — icon + title + subtitle. Chosen by time of day
  // (morning/day/evening/night), always 3 cards; padded from "any" if the pool is short.
  const PROMPT_ICONS = {
    sun: '<svg viewBox="0 0 24 24" fill="none"><circle cx="12" cy="12" r="4" stroke="currentColor" stroke-width="1.7"/><path stroke="currentColor" stroke-width="1.7" stroke-linecap="round" d="M12 2v2.5M12 19.5V22M2 12h2.5M19.5 12H22M4.9 4.9l1.8 1.8M17.3 17.3l1.8 1.8M19.1 4.9l-1.8 1.8M6.7 17.3l-1.8 1.8"/></svg>',
    check: '<svg viewBox="0 0 24 24" fill="none"><path stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M4 12.5 9 17.5 20 6.5"/></svg>',
    pulse: '<svg viewBox="0 0 24 24" fill="none"><path stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" d="M3 12h3.5l2-6 4 12 2.2-6H21"/></svg>',
    moon: '<svg viewBox="0 0 24 24" fill="none"><path stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" d="M20.5 14.6A8.2 8.2 0 0 1 9.4 3.5 7.3 7.3 0 1 0 20.5 14.6Z"/></svg>',
    sparkle: '<svg viewBox="0 0 24 24" fill="none"><path stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" d="M12 3.5 13.8 9 19.5 10.8 13.8 12.6 12 18 10.2 12.6 4.5 10.8 10.2 9Z"/></svg>',
    bulb: '<svg viewBox="0 0 24 24" fill="none"><path stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" d="M9.5 18h5m-4 3h3M12 3a6 6 0 0 0-3.8 10.6c.5.5.8 1.1.8 1.8v.6h6v-.6c0-.7.3-1.3.8-1.8A6 6 0 0 0 12 3Z"/></svg>',
    book: '<svg viewBox="0 0 24 24" fill="none"><path stroke="currentColor" stroke-width="1.7" stroke-linejoin="round" d="M4 5.6A1.8 1.8 0 0 1 5.8 4H11v15H5.8A1.8 1.8 0 0 0 4 20.2V5.6Zm16 0A1.8 1.8 0 0 0 18.2 4H13v15h5.2a1.8 1.8 0 0 1 1.8 1.2V5.6Z"/></svg>',
  };
  const PROMPT_SUGGESTIONS = [
    { slot: "morning", icon: "sun",     title: _t("shell.ps_morning_title"), sub: _t("shell.ps_morning_sub"), prompt: _t("shell.ps_morning_prompt") },
    { slot: "day",     icon: "check",   title: _t("shell.ps_plan_title"),    sub: _t("shell.ps_plan_sub"),    prompt: _t("shell.ps_plan_prompt") },
    { slot: "day",     icon: "pulse",   title: _t("shell.ps_system_title"),  sub: _t("shell.ps_system_sub"),  prompt: _t("shell.ps_system_prompt") },
    { slot: "evening", icon: "moon",    title: _t("shell.ps_evening_title"), sub: _t("shell.ps_evening_sub"), prompt: _t("shell.ps_evening_prompt") },
    { slot: "night",   icon: "sparkle", title: _t("shell.ps_night_title"),   sub: _t("shell.ps_night_sub"),   prompt: _t("shell.ps_night_prompt") },
    { slot: "any",     icon: "bulb",    title: _t("shell.ps_idea_title"),    sub: _t("shell.ps_idea_sub"),    prompt: _t("shell.ps_idea_prompt") },
    { slot: "any",     icon: "book",    title: _t("shell.ps_learn_title"),   sub: _t("shell.ps_learn_sub"),   prompt: _t("shell.ps_learn_prompt") },
  ];
  const SLOT_PREF = {
    morning: ["morning", "day", "any"],
    day: ["day", "any", "morning"],
    evening: ["evening", "day", "any"],
    night: ["night", "any", "evening"],
  };

  function pickPromptSuggestions(n) {
    const h = new Date().getHours();
    const bucket =
      h >= 5 && h < 11 ? "morning" : h >= 11 && h < 17 ? "day" : h >= 17 && h < 22 ? "evening" : "night";
    const pref = SLOT_PREF[bucket] || ["any", "day"];
    const out = [];
    const seen = new Set();
    const take = (s) => {
      if (!seen.has(s.title)) {
        out.push(s);
        seen.add(s.title);
      }
    };
    for (const slot of pref) {
      for (const s of PROMPT_SUGGESTIONS) if (s.slot === slot) take(s);
      if (out.length >= n) return out.slice(0, n);
    }
    for (const s of PROMPT_SUGGESTIONS) take(s);
    return out.slice(0, n);
  }

  function renderPromptSuggestions() {
    const wrap = document.querySelector("#log-empty .prompt-chips");
    if (!wrap) return;
    const frag = document.createDocumentFragment();
    for (const s of pickPromptSuggestions(3)) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "prompt-chip";
      btn.setAttribute("data-prompt", s.prompt);
      const icon = document.createElement("span");
      icon.className = "prompt-chip-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.innerHTML = PROMPT_ICONS[s.icon] || "";
      const body = document.createElement("span");
      body.className = "prompt-chip-body";
      const title = document.createElement("span");
      title.className = "prompt-chip-title";
      title.textContent = s.title;
      const sub = document.createElement("span");
      sub.className = "prompt-chip-sub";
      sub.textContent = s.sub;
      body.append(title, sub);
      btn.append(icon, body);
      frag.append(btn);
    }
    wrap.replaceChildren(frag);
  }

  function wirePromptChips() {
    renderPromptSuggestions();
    const wrap = document.querySelector("#log-empty .prompt-chips");
    if (!wrap) return;
    wrap.addEventListener("click", (e) => {
      const chip = e.target.closest(".prompt-chip");
      if (!chip || !wrap.contains(chip)) return;
      const text = chip.getAttribute("data-prompt") || "";
      const msg = hooks.msg;
      if (!text || !msg) return;
      msg.value = text;
      resizeComposer();
      msg.focus();
    });
  }

  function wireShortcutKeys() {
    document.addEventListener("keydown", (e) => {
      if (
        e.key === "?" &&
        !e.ctrlKey &&
        !e.metaKey &&
        !e.altKey &&
        !isTypingShortcutTarget(e.target)
      ) {
        e.preventDefault();
        if (kbdHelpEl()) openKbdHelp();
        else hooks.openSettings?.();
      }
    });

    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      const help = kbdHelpEl();
      if (help && help.hasAttribute("open")) {
        closeKbdHelp();
        e.preventDefault();
        return;
      }
      if (document.body.classList.contains("settings-open")) return;
      if (document.body.classList.contains("archive-open")) return;
      if (hooks.cancelVoiceActivity()) {
        e.preventDefault();
        e.stopPropagation();
      }
    });
  }


  function wireComposer() {
    const msg = hooks.msg;
    const form = hooks.form;
    if (!msg) return;
    msg.addEventListener("input", resizeComposer);
    if (!form) return;
    msg.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        if (typeof form.requestSubmit === "function") form.requestSubmit();
        else form.dispatchEvent(new Event("submit", { cancelable: true }));
      }
    });
  }

  // ── Composer draft persistence ─────────────────────────────────────────────
  // Typed text is debounce-written to localStorage; restored into an empty
  // textarea on page reload. Draft is deleted after submission (when chat.js
  // clears the textarea). No #msg in memory.html → silent no-op.
  const LS_DRAFT = "akana.composerDraft";
  const DRAFT_MAX = 32000;

  function wireDraftPersistence() {
    const msg = hooks.msg || document.getElementById("msg");
    if (!msg) return;
    const form = hooks.form || document.getElementById("chat-form");

    // Restore: only when the textarea is empty — don't overwrite if server/another module filled it.
    try {
      if (!msg.value) {
        const saved = localStorage.getItem(LS_DRAFT) || "";
        if (saved.trim()) {
          msg.value = saved.slice(0, DRAFT_MAX);
          // trigger autosize + other input listeners
          msg.dispatchEvent(new Event("input", { bubbles: true }));
        }
      }
    } catch {
      /* localStorage unavailable → silent no-op */
    }

    let saveTimer = null;
    msg.addEventListener("input", () => {
      if (saveTimer != null) clearTimeout(saveTimer);
      saveTimer = setTimeout(() => {
        saveTimer = null;
        try {
          const v = msg.value;
          if (v.trim()) localStorage.setItem(LS_DRAFT, v.slice(0, DRAFT_MAX));
          else localStorage.removeItem(LS_DRAFT);
        } catch {
          /* quota/access error → silent */
        }
      }, 400);
    });

    if (!form) return;
    // Submit hook: chat.js clears the textarea inside submit, but when an active
    // turn is being cancelled this can be async-delayed → two checks, short + long.
    form.addEventListener("submit", () => {
      for (const wait of [120, 900]) {
        setTimeout(() => {
          if (!msg.value.trim()) {
            if (saveTimer != null) {
              clearTimeout(saveTimer);
              saveTimer = null;
            }
            try {
              localStorage.removeItem(LS_DRAFT);
            } catch {
              /* silent */
            }
          }
        }, wait);
      }
    });
  }

  // ── Code block copy + language badge ────────────────────────────────────
  // On hover over a bot-bubble <pre> block inside #log, a SINGLE floating capsule
  // (badge + "Copy") anchored to #log-scroll moves to the top-right of that block.
  // Language is read from data-lang written by the markdown renderer
  // (akana-markdown.js: <pre class="md-code" data-lang="…">). No #log/#log-scroll
  // in memory.html → silent no-op.
  // Conversation-switch dismiss hook: the capsule is a SIBLING of #log floating in
  // #log-scroll (like the msg action bar), so hiding the leaving pane leaves it
  // stranded at its old absolute `top`. An opacity-0 element still counts toward
  // scrollable overflow → the NEW chat inherited the OLD chat's scroll extent
  // (user report: empty chat scrollable as far as the previous long chat).
  let _dismissCodeTools = null;

  function wireCodeCopy() {
    // audit B7: prefer the CONTAINER (#log), NOT hooks.log (= displayed pane)
    // → click-delegate covers all conversation panes (panes are descendants of #log).
    const log = document.getElementById("log") || hooks.log;
    const scroller = hooks.logScroll || document.getElementById("log-scroll");
    if (!log || !scroller) return;
    if (scroller.querySelector(".akana-code-tools")) return;

    const tools = document.createElement("div");
    tools.className = "akana-code-tools";
    const badge = document.createElement("span");
    badge.className = "akana-code-lang";
    badge.hidden = true;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "akana-code-copy";
    btn.textContent = _t("shell.code_copy");
    btn.setAttribute("aria-label", _t("shell.code_copy_aria"));
    tools.appendChild(badge);
    tools.appendChild(btn);
    // Born [hidden]: display:none until the first hover, so the capsule's box never
    // pads the scroll extent of a chat it has not been shown in (fresh/empty chats).
    tools.hidden = true;
    scroller.appendChild(tools);

    let currentPre = null;
    let resetTimer = null;

    const codeLang = (pre) => {
      const dl = (pre.getAttribute("data-lang") || "").trim();
      if (dl) return dl;
      // fallback: for renderers that emit <code class="language-x">
      const code = pre.querySelector('code[class*="language-"]');
      const m = code && /(?:^|\s)language-([\w#+-]+)/.exec(code.className);
      return m ? m[1] : "";
    };

    // Floating capsule is absolutely positioned inside the scroller → scrolls with content,
    // so scrollTop is added to top and it stays aligned during scrolling.
    const showFor = (pre) => {
      currentPre = pre;
      tools.hidden = false; // undo a conversation-switch dismiss
      const pr = pre.getBoundingClientRect();
      const sr = scroller.getBoundingClientRect();
      tools.style.top = `${pr.top - sr.top + scroller.scrollTop + 6}px`;
      tools.style.right = `${Math.max(6, sr.right - pr.right + 6)}px`;
      const lang = codeLang(pre);
      badge.textContent = lang;
      badge.hidden = !lang;
      tools.classList.add("is-visible");
    };

    const hideTools = () => {
      currentPre = null;
      tools.classList.remove("is-visible");
    };

    // Full dismiss (conversation switch): hover-out only fades (keeps `top` for the
    // transition), which is fine WITHIN a chat — the anchor block is part of the
    // content, so the capsule never exceeds it. Across a switch the old `top` can
    // point far beyond the new chat's content → also clear the offsets and go
    // display:none ([hidden]) so the box stops contributing to scroll overflow.
    _dismissCodeTools = () => {
      hideTools();
      tools.hidden = true;
      tools.style.top = "";
      tools.style.right = "";
    };

    const preFrom = (target) => {
      if (!(target instanceof Element)) return null;
      const pre = target.closest("pre");
      if (!pre || !log.contains(pre)) return null;
      return pre.closest(".bubble-bot, .bubble-assistant") ? pre : null;
    };

    const onPoint = (e) => {
      if (tools.contains(e.target)) return; // keep open while hovering the capsule
      const pre = preFrom(e.target);
      if (pre) showFor(pre);
      else hideTools();
    };
    scroller.addEventListener("mouseover", onPoint);
    scroller.addEventListener("focusin", onPoint);

    btn.addEventListener("click", () => {
      const pre = currentPre;
      if (!pre || !log.contains(pre)) {
        hideTools();
        return;
      }
      if (!navigator.clipboard || typeof navigator.clipboard.writeText !== "function") return;
      const code = pre.querySelector("code");
      const text = (code || pre).textContent || "";
      navigator.clipboard
        .writeText(text)
        .then(() => {
          btn.textContent = _t("shell.code_copied");
          btn.classList.add("is-copied");
          if (resetTimer != null) clearTimeout(resetTimer);
          resetTimer = setTimeout(() => {
            resetTimer = null;
            btn.textContent = _t("shell.code_copy");
            btn.classList.remove("is-copied");
          }, 1200);
        })
        .catch(() => {
          /* no clipboard permission → silent */
        });
    });
  }


  window.AkanaShell = {
    init,
    setOrb,
    setComposerHint,
    setActiveCursorModel,
    stickToBottomIfFollowing,
    scrollLogToBottom,
    scrollNewTurnToTop,
    setLogLoading,
    appendRow,
    appendUserMessage,
    appendSystemNotice,
    renderPdfThumb,
    updateEmptyState,
    resizeComposer,
    shortConversationId,
    // ── Pane ops (parallel-chat): chat bridge calls these on conversation switch/create/delete;
    //    safe no-op/fallback when PaneManager is absent. ─────────────────────
    paneFor: (id) => (_panes ? _panes.paneFor(id) : hooks.log),
    showConversation: (id) => {
      if (!_panes) return null;
      const prevKey = _panes.displayedConvId();
      const pane = _panes.show(id);
      // Pane changed → drop the leaving chat's "pin-to-top" tail gap; it lives on the
      // shared scroller and would inflate the new chat's scroll height if carried over.
      if (prevKey !== _panes.displayedConvId()) {
        _clearTailGap();
        // Dismiss the hover action bar + read-aloud speaker anchored to the LEAVING
        // pane. They are siblings of #log (floating in #log-scroll), so hiding the
        // pane does not remove them — without this they persist onto the new chat
        // (user report: old chat's speaker/Quote/Copy show up in a fresh chat).
        window.AkanaMsgActionBar?.hide?.();
        // Same family: the code-copy capsule — stranded at the OLD chat's absolute
        // `top` it kept the old scroll extent alive (empty chat scrollable for
        // thousands of px) and floated its Copy button over the new chat.
        _dismissCodeTools?.();
      }
      return pane;
    },
    clearConversation: (id) => (_panes ? _panes.clear(id) : (hooks.log && (hooks.log.innerHTML = ""))),
    removeConversation: (id) => (_panes ? _panes.remove(id) : false),
    rekeyConversation: (a, b) => (_panes ? _panes.rekey(a, b) : false),
    displayedConvId: () => (_panes ? _panes.displayedConvId() : null),
    displayedPane: () => currentPane(),
  };
})();
