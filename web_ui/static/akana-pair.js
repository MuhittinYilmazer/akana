/**
 * Akana phone pairing — the "Connect phone" button in the desktop cockpit opens a QR
 * modal; scanning with the phone connects with ZERO typing. The bearer token is carried
 * in the URL hash (`/#token=…`); the phone-side index.html early-boot script reads it,
 * writes it to `localStorage["akana.apiToken"]`, and removes the fragment.
 *
 * SECURITY: token/URL is NEVER written to console; the QR is produced entirely
 * locally (vendored lib) — the token is NOT sent to a remote QR service.
 *
 * Parallel to akana-settings.js style (IIFE, AkanaCore bridge). Loaded with defer
 * before app.js.
 */
(() => {
  const LS_TOKEN = (window.AkanaCore && window.AkanaCore.LS_TOKEN) || "akana.apiToken";
  const LS_PAIR_HOST = "akana.pairHost";
  // Known Tailscale Serve address — used as default if the user has not saved a
  // separate host and the page is not opened via .ts.net.
  const DEFAULT_HOST = "your-host.tailnet.ts.net";

  const showToast = (m, k) =>
    (window.AkanaCore && window.AkanaCore.showToast
      ? window.AkanaCore.showToast(m, k)
      : undefined);

  function readToken() {
    try {
      return (localStorage.getItem(LS_TOKEN) || "").trim();
    } catch (e) {
      return "";
    }
  }

  // Host resolution: saved akana.pairHost > page already on .ts.net (location.host)
  // > known default.
  function resolveHost() {
    let saved = "";
    try {
      saved = (localStorage.getItem(LS_PAIR_HOST) || "").trim();
    } catch (e) {
      saved = "";
    }
    if (saved) return saved;
    const here = (location.host || "").trim();
    if (/\.ts\.net$/i.test(here)) return here;
    return DEFAULT_HOST;
  }

  // True while the host is still the unconfigured placeholder. We must NEVER put
  // the bearer token in a QR/URL pointing at a hostname the user doesn't own
  // (a public *.ts.net Funnel name serving that page could read location.hash
  // and steal the token) — treat the placeholder as "no host yet".
  function isPlaceholderHost(host) {
    return !host || host === DEFAULT_HOST;
  }

  // `https://<host>/#token=<encoded>` — returns null if no token OR the host is
  // still the placeholder (caller opens the modal so the user can enter their
  // real host, which rebuilds the URL once corrected).
  function buildPairUrl() {
    const token = readToken();
    if (!token) return null;
    const host = resolveHost();
    if (isPlaceholderHost(host)) return null;
    return "https://" + host + "/#token=" + encodeURIComponent(token);
  }

  let backdropEl = null;
  let qrInstance = null;
  let lastUrl = "";
  // Last successful /api/v1/system/pair response (loopback-only endpoint) —
  // reusable when the modal is reopened / the host field is populated, without
  // re-fetching.
  let lastPairInfo = null;

  // Fetches pair info from the server (loopback-only): {token_set, https_url,
  // self_dns_name, serve_active, pair_url}. Returns null on any error or
  // non-200 response (e.g. 404 loopback-gate behind a proxy) — the caller
  // silently falls back to the localStorage+placeholder-host path.
  async function fetchPairInfo() {
    if (!window.AkanaCore || !window.AkanaCore.baseUrl) return null;
    try {
      const r = await fetch(`${window.AkanaCore.baseUrl()}/api/v1/system/pair`, {
        headers: window.AkanaCore.authHeaders(),
      });
      if (!r.ok) return null;
      const data = await r.json();
      lastPairInfo = data;
      return data;
    } catch (e) {
      return null;
    }
  }

  function ensureModal() {
    backdropEl = document.getElementById("pair-backdrop");
    if (backdropEl) return backdropEl;
    // Markup not in index.html (defensive): build it at runtime.
    backdropEl = document.createElement("div");
    backdropEl.id = "pair-backdrop";
    backdropEl.className = "pair-backdrop";
    backdropEl.setAttribute("aria-hidden", "true");
    backdropEl.innerHTML =
      '<div class="pair-modal" role="dialog" aria-modal="true" aria-labelledby="pair-modal-title">' +
      '<header class="pair-modal-head">' +
      `<h3 id="pair-modal-title">${window.AkanaI18n.t("pair.modal.title")}</h3>` +
      `<button type="button" id="pair-modal-close" class="btn-icon pair-modal-close" aria-label="${window.AkanaI18n.t("pair.modal.close_aria")}">✕</button>` +
      "</header>" +
      `<p class="pair-modal-desc">${window.AkanaI18n.t("pair.modal.desc")}</p>` +
      '<div class="pair-qr" id="pair-qr" aria-hidden="true"></div>' +
      `<label class="pair-host-label" for="pair-host-confirm">${window.AkanaI18n.t("pair.modal.host_label")}</label>` +
      '<div class="pair-host-row">' +
      '<input id="pair-host-confirm" class="pair-host-input" type="text" autocomplete="off" spellcheck="false" />' +
      `<button type="button" id="pair-copy-url" class="btn-ghost btn-sm" title="${window.AkanaI18n.t("pair.modal.copy_title")}">${window.AkanaI18n.t("pair.modal.copy_btn")}</button>` +
      "</div>" +
      '<p class="pair-modal-foot field-hint" id="pair-modal-foot" role="status"></p>' +
      "</div>";
    document.body.appendChild(backdropEl);
    return backdropEl;
  }

  function closeModal() {
    if (!backdropEl) return;
    backdropEl.setAttribute("aria-hidden", "true");
    document.body.classList.remove("pair-open");
  }

  function renderQr(url) {
    const host = document.getElementById("pair-qr");
    if (!host) return false;
    host.innerHTML = "";
    qrInstance = null;
    if (typeof window.QRCode !== "function") {
      return false;
    }
    // Token is embedded, so QR is produced only with the local (vendored) library.
    qrInstance = new window.QRCode(host, {
      text: url,
      width: 232,
      height: 232,
      colorDark: "#0b0f17",
      colorLight: "#ffffff",
      correctLevel: window.QRCode.CorrectLevel.M,
    });
    return true;
  }

  async function openPairModal() {
    // Server-first: try the new loopback-only endpoint before falling back to
    // the localStorage-token + placeholder-host path.
    const info = await fetchPairInfo();

    if (info && info.pair_url) {
      const backdrop = ensureModal();
      lastUrl = info.pair_url;

      const hostInput = document.getElementById("pair-host-confirm");
      if (hostInput) hostInput.value = info.self_dns_name || info.https_url || "";
      const foot = document.getElementById("pair-modal-foot");
      if (foot) {
        foot.textContent = "";
        foot.style.color = "";
      }

      const ok = renderQr(info.pair_url);
      if (!ok && foot) {
        foot.textContent = window.AkanaI18n.t("pair.qr.failed");
        foot.style.color = "var(--err)";
      }

      backdrop.setAttribute("aria-hidden", "false");
      document.body.classList.add("pair-open");
      return;
    }

    if (info && info.token_set === false) {
      showToast(window.AkanaI18n.t("pair.toast.no_server_token"), "err");
      return;
    }

    // Token IS set server-side but no pair_url: the only reason the endpoint returns
    // that is a missing tailnet HTTPS URL (Tailscale Serve not active). Name the real
    // missing piece (not the misleading 'set a token first'). But do NOT dead-end when a
    // LOCAL token exists: a self-proxied user can still type a custom host and get a QR
    // from the manual-host modal below — only abort when there is no local token either.
    if (info && info.token_set && !info.pair_url) {
      showToast(window.AkanaI18n.t("pair.toast.serve_inactive"), "err");
      if (!readToken()) return;
      // else: fall through to the manual-host modal (buildPairUrl uses the local token).
    }

    // info === null (endpoint unreachable / proxied) → fall back unchanged.
    // No token at all → nothing to pair; abort with a toast (host is irrelevant).
    if (!readToken()) {
      showToast(window.AkanaI18n.t("pair.toast.no_token"), "err");
      return;
    }
    const url = buildPairUrl(); // null here means the host is still the placeholder.
    lastUrl = url || "";
    const backdrop = ensureModal();

    const hostInput = document.getElementById("pair-host-confirm");
    // Don't prefill the placeholder — leave the field empty so the user enters
    // their real host (typing it triggers onHostChange → rebuilds the QR).
    if (hostInput) hostInput.value = isPlaceholderHost(resolveHost()) ? "" : resolveHost();
    const foot = document.getElementById("pair-modal-foot");
    if (foot) {
      foot.textContent = "";
      foot.style.color = "";
    }

    // With no valid host we must NOT render a token-bearing QR; prompt for the host.
    const ok = url ? renderQr(url) : false;
    if (!ok && foot) {
      foot.textContent = window.AkanaI18n.t("pair.qr.failed");
      foot.style.color = "var(--err)";
    }
    if (!url) {
      const qrHost = document.getElementById("pair-qr");
      if (qrHost) qrHost.innerHTML = "";
      hostInput?.focus();
    }

    backdrop.setAttribute("aria-hidden", "false");
    document.body.classList.add("pair-open");
  }

  // The token the /system/pair endpoint composed into pair_url is the ONLY token a
  // loopback owner has (their localStorage is empty — see the endpoint docstring), so a
  // host correction must recompose THAT url onto the new host, not fall back to the
  // localStorage-only buildPairUrl() (which returns null → wipes the working QR). The
  // token in pair_url is already percent-encoded (quote(token, safe='')) — reuse it
  // verbatim; do NOT re-encode. Returns null for a placeholder/empty host.
  function recomposePairUrl(host) {
    const h = (host || "").trim();
    if (isPlaceholderHost(h)) return null;
    const src = lastPairInfo && lastPairInfo.pair_url;
    if (!src) return null;
    const marker = "#token=";
    const i = src.indexOf(marker);
    if (i < 0) return null;
    const token = src.slice(i + marker.length);
    if (!token) return null;
    // Match buildPairUrl's shape exactly: https://<host>/#token=<token>.
    return "https://" + h + "/" + marker + token;
  }

  // Save + refresh QR whenever the host field changes (user can correct a wrong address).
  // If left empty, the saved value is removed → resolution falls back to the default.
  function onHostChange() {
    const hostInput = document.getElementById("pair-host-confirm");
    if (!hostInput) return;
    const val = (hostInput.value || "").trim();
    try {
      if (val) localStorage.setItem(LS_PAIR_HOST, val);
      else localStorage.removeItem(LS_PAIR_HOST);
    } catch (e) {
      /* localStorage inaccessible — ignore */
    }
    // Prefer the server-issued token (a loopback owner has none in localStorage, so
    // buildPairUrl() alone returns null and would wipe the working QR on any edit).
    const url = recomposePairUrl(val) || buildPairUrl();
    if (url) {
      lastUrl = url;
      renderQr(url);
    } else {
      // Host cleared / still placeholder → drop any stale token-bearing QR.
      lastUrl = "";
      const qrHost = document.getElementById("pair-qr");
      if (qrHost) qrHost.innerHTML = "";
    }
  }

  async function copyUrl() {
    const foot = document.getElementById("pair-modal-foot");
    const url = lastUrl || buildPairUrl();
    if (!url) {
      if (foot) {
        foot.textContent = window.AkanaI18n.t("pair.status.no_token");
        foot.style.color = "var(--err)";
      }
      return;
    }
    try {
      await navigator.clipboard.writeText(url);
      if (foot) {
        foot.textContent = window.AkanaI18n.t("pair.status.copied");
        foot.style.color = "var(--ok)";
      }
    } catch (e) {
      if (foot) {
        foot.textContent = window.AkanaI18n.t("pair.status.copy_failed");
        foot.style.color = "var(--err)";
      }
    }
  }

  function wire() {
    const btn = document.getElementById("btn-pair-phone");
    if (btn && !btn.dataset.pairWired) {
      btn.dataset.pairWired = "1";
      btn.addEventListener("click", openPairModal);
    }

    // Persistent host field in the settings panel (editable outside the modal too).
    const settingsHost = document.getElementById("pair-host");
    if (settingsHost && !settingsHost.dataset.pairWired) {
      settingsHost.dataset.pairWired = "1";
      try {
        const saved = (localStorage.getItem(LS_PAIR_HOST) || "").trim();
        settingsHost.value = saved || DEFAULT_HOST;
      } catch (e) {
        settingsHost.value = DEFAULT_HOST;
      }
      const persist = () => {
        const val = (settingsHost.value || "").trim();
        try {
          if (val) localStorage.setItem(LS_PAIR_HOST, val);
          else localStorage.removeItem(LS_PAIR_HOST);
        } catch (e) {
          /* ignore */
        }
      };
      settingsHost.addEventListener("change", persist);
      settingsHost.addEventListener("blur", persist);
    }

    // Modal controls wired via event delegation (modal markup may be added later).
    document.addEventListener("click", (ev) => {
      const t = ev.target;
      if (!t || !t.closest) return;
      if (t.closest("#pair-modal-close")) {
        closeModal();
      } else if (t.closest("#pair-copy-url")) {
        void copyUrl();
      } else if (t.id === "pair-backdrop") {
        // Click on backdrop to close.
        closeModal();
      }
    });
    document.addEventListener("change", (ev) => {
      const t = ev.target;
      if (t && t.id === "pair-host-confirm") onHostChange();
    });
    document.addEventListener("keydown", (ev) => {
      if (ev.key === "Escape" && document.body.classList.contains("pair-open")) {
        closeModal();
      }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire, { once: true });
  } else {
    wire();
  }

  // Public API (other modules may call if needed).
  window.AkanaPair = { openPairModal };
})();
