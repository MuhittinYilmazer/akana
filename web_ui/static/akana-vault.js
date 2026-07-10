/* akana-vault.js — SecureVault management UI (self-contained).
 *
 * Renders into #vault-root inside the "Vault" settings pane. Self-loads when the
 * pane becomes visible (observes its `hidden` attribute), so it needs no hook in
 * akana-settings.js. Listing stays write-only (no masked previews); a per-row
 * "Show" button fetches the raw value on demand from the audited /reveal
 * endpoint, so the owner can verify exactly what they stored.
 *
 * Backend: /api/v1/system/vault {GET}, /scalars {GET,PUT,DELETE},
 *          /scalars/{key}/reveal {GET — raw value, audited},
 *          /{namespace}/{profile} {DELETE — whole profile},
 *          /{namespace}/{profile}/fields {GET,PUT,DELETE},
 *          /{namespace}/{profile}/fields/{key}/reveal {GET — raw value, audited}.
 */
(function () {
  "use strict";

  const PANE_ID = "settings-pane-vault";
  const ROOT_ID = "vault-root";
  const apiBase = () => `${window.AkanaCore.baseUrl()}/api/v1/system/vault`;

  let busy = false;
  let reloadPending = false; // a load() requested while another was in flight — coalesce

  const $ = (id) => document.getElementById(id);

  const esc = (value) => window.AkanaCore.escapeAttr(value);
  const api = (method, path, body) => window.AkanaCore.apiJson(apiBase, method, path, body);

  function setStatus(message, isError) {
    const el = $("vault-status");
    if (!el) return;
    el.textContent = message || "";
    el.style.color = isError ? "var(--danger, #c0392b)" : "";
  }

  // System-credential badge label. The i18n strings file is not editable here, so
  // derive the label from the active locale directly (mirrors its en/tr pattern).
  function systemBadgeLabel() {
    const lang = (window.AkanaI18n.getLanguage && window.AkanaI18n.getLanguage()) || "en";
    return lang === "tr" ? "sistem" : "system";
  }

  function maskRows(map, action, extra) {
    const keys = Object.keys(map || {});
    if (!keys.length) return `<p class="field-hint">${window.AkanaI18n.t("vault.row.empty")}</p>`;
    return keys.sort().map((key) => {
      const data = Object.entries(extra || {})
        .map(([k, v]) => `data-${k}="${esc(v)}"`).join(" ");
      // Provider keys (Cursor/Claude/…) live in the secret store; the listing tags
      // them so we can badge the row. Reveal/delete still hit the same endpoints —
      // the backend dual-routes those by key name.
      const isSystem = !!(map[key] && map[key].is_system_credential);
      const badge = isSystem
        ? ` <span class="vault-row-badge" title="${esc(systemBadgeLabel())}">${esc(systemBadgeLabel())}</span>`
        : "";
      // No masked preview: the value slot stays empty until the owner clicks "Show",
      // which fetches the raw value from the audited /reveal endpoint.
      return (
        `<div class="vault-row">` +
          `<code class="vault-row-key">${esc(key)}</code>${badge}` +
          `<span class="vault-row-val" data-revealed="0"></span>` +
          `<button type="button" class="btn-ghost btn-sm" data-action="reveal" data-key="${esc(key)}" ${data}>${window.AkanaI18n.t("vault.row.reveal_btn")}</button>` +
          `<button type="button" class="btn-ghost btn-sm" data-action="${action}" data-key="${esc(key)}" ${data}>${window.AkanaI18n.t("vault.row.delete_btn")}</button>` +
        `</div>`
      );
    }).join("");
  }

  function encryptionBanner(enc) {
    if (!enc) return "";
    const danger = "border-color:var(--danger,#c0392b);color:var(--danger,#c0392b)";
    if (enc.available === false) {
      return `<div class="info-callout" style="${danger}">${window.AkanaI18n.t("vault.enc.unavailable")}</div>`;
    }
    if (enc.healthy === false || (enc.decrypt_failures | 0) > 0) {
      return `<div class="info-callout" style="${danger}">${window.AkanaI18n.t("vault.enc.broken", { source: esc(enc.key_source || "?") })}</div>`;
    }
    return "";
  }

  function render(root, groups, scalars, encryption) {
    const accountBlocks = groups.length
      ? groups.map((g) => (
          `<div class="settings-block vault-group">` +
            `<div class="vault-group-head">` +
              `<h3 class="settings-block-title">${esc(g.namespace)} / ${esc(g.profile)}</h3>` +
              `<button type="button" class="btn-ghost btn-sm" data-action="del-profile" data-ns="${esc(g.namespace)}" data-profile="${esc(g.profile)}">${window.AkanaI18n.t("vault.group.delete_btn")}</button>` +
            `</div>` +
            maskRows(g.fields, "del-field", { ns: g.namespace, profile: g.profile }) +
          `</div>`
        )).join("")
      : `<p class="field-hint">${window.AkanaI18n.t("vault.accounts.empty")}</p>`;

    root.innerHTML =
      encryptionBanner(encryption) +
      `<div class="info-callout settings-info-callout">` +
        window.AkanaI18n.t("vault.callout") +
      `</div>` +

      `<div class="settings-block">` +
        `<h3 class="settings-block-title">${window.AkanaI18n.t("vault.accounts.title")}</h3>` +
        `<p class="field-hint">${window.AkanaI18n.t("vault.accounts.hint")}</p>` +
        `<div class="vault-form">` +
          `<input id="vault-f-ns" placeholder="${window.AkanaI18n.t("vault.accounts.ns_ph")}" autocomplete="off" spellcheck="false" />` +
          `<input id="vault-f-profile" placeholder="${window.AkanaI18n.t("vault.accounts.profile_ph")}" value="default" autocomplete="off" spellcheck="false" />` +
          `<input id="vault-f-key" placeholder="${window.AkanaI18n.t("vault.accounts.key_ph")}" autocomplete="off" spellcheck="false" />` +
          `<input id="vault-f-val" type="password" placeholder="${window.AkanaI18n.t("vault.accounts.val_ph")}" autocomplete="off" spellcheck="false" />` +
          `<button type="button" class="btn-primary btn-sm" data-action="add-field">${window.AkanaI18n.t("vault.accounts.save_btn")}</button>` +
        `</div>` +
        accountBlocks +
      `</div>` +

      `<div class="settings-block">` +
        `<h3 class="settings-block-title">${window.AkanaI18n.t("vault.scalars.title")}</h3>` +
        `<p class="field-hint">${window.AkanaI18n.t("vault.scalars.hint")}</p>` +
        `<div class="vault-form">` +
          `<input id="vault-s-key" placeholder="${window.AkanaI18n.t("vault.scalars.key_ph")}" autocomplete="off" spellcheck="false" />` +
          `<input id="vault-s-val" type="password" placeholder="${window.AkanaI18n.t("vault.scalars.val_ph")}" autocomplete="off" spellcheck="false" />` +
          `<button type="button" class="btn-primary btn-sm" data-action="add-scalar">${window.AkanaI18n.t("vault.scalars.save_btn")}</button>` +
        `</div>` +
        maskRows(scalars, "del-scalar", {}) +
      `</div>` +

      `<div class="settings-row-actions">` +
        `<button type="button" class="btn-ghost btn-sm" data-action="refresh">${window.AkanaI18n.t("vault.refresh_btn")}</button>` +
        `<p class="hint" id="vault-status" role="status"></p>` +
      `</div>`;
  }

  async function load() {
    const root = $(ROOT_ID);
    if (!root) return;
    // A load() requested while one is in flight must NOT be dropped: the dropped
    // request is usually the one carrying post-mutation truth (e.g. the row a
    // just-confirmed Delete removed), so dropping it re-renders from GETs issued
    // BEFORE the mutation and resurrects the deleted secret. Coalesce — re-run once
    // when the current load settles so the freshest server state is the one rendered.
    if (busy) {
      reloadPending = true;
      return;
    }
    busy = true;
    try {
      const [summary, scalarsResp] = await Promise.all([api("GET", ""), api("GET", "/scalars")]);
      const groups = [];
      for (const ns of summary.namespaces || []) {
        for (const pf of ns.profiles || []) {
          let fields = {};
          try {
            fields = (await api("GET", `/${encodeURIComponent(ns.namespace)}/${encodeURIComponent(pf.profile)}/fields`)).fields || {};
          } catch (_) { /* skip unreadable profile */ }
          groups.push({ namespace: ns.namespace, profile: pf.profile, fields });
        }
      }
      render(root, groups, (scalarsResp && scalarsResp.scalars) || {}, summary.encryption);
    } catch (err) {
      root.innerHTML = `<p class="field-hint" style="color:var(--danger,#c0392b)">${window.AkanaI18n.t("vault.load_failed", { error: esc(err.message) })}</p>`;
    } finally {
      busy = false;
    }
    if (reloadPending) {
      reloadPending = false;
      await load();
    }
  }

  async function onClick(event) {
    const btn = event.target.closest("[data-action]");
    if (!btn || !$(ROOT_ID).contains(btn)) return;
    const action = btn.dataset.action;
    try {
      if (action === "refresh") {
        await load();
      } else if (action === "reveal") {
        const valEl = btn.parentElement.querySelector(".vault-row-val");
        if (!valEl) return;
        if (valEl.dataset.revealed === "1") { // toggle off — drop the plaintext from the DOM
          valEl.textContent = "";
          valEl.dataset.revealed = "0";
          btn.textContent = window.AkanaI18n.t("vault.row.reveal_btn");
          return;
        }
        const { key, ns, profile } = btn.dataset;
        const path = ns
          ? `/${encodeURIComponent(ns)}/${encodeURIComponent(profile)}/fields/${encodeURIComponent(key)}/reveal`
          : `/scalars/${encodeURIComponent(key)}/reveal`;
        const resp = await api("GET", path);
        valEl.textContent = (resp && resp.value) || "";
        valEl.dataset.revealed = "1";
        btn.textContent = window.AkanaI18n.t("vault.row.hide_btn");
      } else if (action === "add-scalar") {
        const key = ($("vault-s-key").value || "").trim();
        const val = $("vault-s-val").value || "";
        if (!key || !val) return setStatus(window.AkanaI18n.t("vault.status.key_val_required"), true);
        await api("PUT", "/scalars", { scalars: { [key]: val } });
        await load();
        setStatus(window.AkanaI18n.t("vault.status.scalar_saved", { key }), false);
      } else if (action === "del-scalar") {
        if (!confirm(window.AkanaI18n.t("vault.confirm.del_scalar", { key: btn.dataset.key }))) return;
        await api("DELETE", `/scalars/${encodeURIComponent(btn.dataset.key)}`);
        await load();
        setStatus(window.AkanaI18n.t("vault.status.scalar_deleted", { key: btn.dataset.key }), false);
      } else if (action === "add-field") {
        const ns = ($("vault-f-ns").value || "").trim();
        const profile = ($("vault-f-profile").value || "default").trim() || "default";
        const key = ($("vault-f-key").value || "").trim();
        const val = $("vault-f-val").value || "";
        if (!ns || !key || !val) return setStatus(window.AkanaI18n.t("vault.status.ns_key_val_required"), true);
        await api("PUT", `/${encodeURIComponent(ns)}/${encodeURIComponent(profile)}/fields`, { fields: { [key]: val } });
        await load();
        setStatus(window.AkanaI18n.t("vault.status.field_saved", { ns, profile, key }), false);
      } else if (action === "del-field") {
        const { ns, profile, key } = btn.dataset;
        if (!confirm(window.AkanaI18n.t("vault.confirm.del_field", { ns, profile, key }))) return;
        await api("DELETE", `/${encodeURIComponent(ns)}/${encodeURIComponent(profile)}/fields/${encodeURIComponent(key)}`);
        await load();
        setStatus(window.AkanaI18n.t("vault.status.field_deleted", { key }), false);
      } else if (action === "del-profile") {
        const { ns, profile } = btn.dataset;
        if (!confirm(window.AkanaI18n.t("vault.confirm.del_profile", { ns, profile }))) return;
        await api("DELETE", `/${encodeURIComponent(ns)}/${encodeURIComponent(profile)}`);
        await load();
        setStatus(window.AkanaI18n.t("vault.status.profile_deleted", { ns, profile }), false);
      }
    } catch (err) {
      setStatus(err.message, true);
    }
  }

  function ensureStyles() {
    if ($("vault-styles")) return;
    const style = document.createElement("style");
    style.id = "vault-styles";
    style.textContent =
      ".vault-form{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 12px}" +
      ".vault-form input{flex:1 1 140px;min-width:120px}" +
      ".vault-form .btn-primary{flex:0 0 auto}" +
      ".vault-row{display:flex;align-items:center;gap:10px;padding:6px 0;border-top:1px solid var(--border,rgba(128,128,128,.18))}" +
      ".vault-row-key{flex:0 0 auto;font-weight:600}" +
      ".vault-row-badge{flex:0 0 auto;font-size:.72em;text-transform:uppercase;letter-spacing:.04em;padding:1px 6px;border-radius:4px;border:1px solid var(--border,rgba(128,128,128,.35));opacity:.75}" +
      ".vault-row-val{flex:1 1 auto;min-width:0;text-align:right;font-family:var(--mono,ui-monospace,monospace);font-size:.9em;opacity:.85;word-break:break-all}" +
      ".vault-group{margin-top:10px}" +
      ".vault-group-head{display:flex;align-items:center;justify-content:space-between;gap:10px}";
    document.head.appendChild(style);
  }

  function init() {
    const pane = $(PANE_ID);
    const root = $(ROOT_ID);
    if (!pane || !root) return;
    ensureStyles();
    root.addEventListener("click", onClick);
    const observer = new MutationObserver(() => { if (!pane.hidden) void load(); });
    observer.observe(pane, { attributes: true, attributeFilter: ["hidden"] });
    if (!pane.hidden) void load();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.AkanaVault = { load };
})();
