/* akana-packs.js — Capability Pack management (self-contained).
 *
 * Renders into #packs-root inside the "Packs" settings pane (#settings-pane-packs).
 * Self-loads when the pane becomes visible (observes its `hidden` attribute) →
 * no hook needed in akana-settings.js (persona/vault pattern; conflict-free file).
 *
 * A pack bundles skills / personas / memory types / plugins. Enable/disable
 * hot-reloads the pack's content at runtime; disabling also removes the pack's
 * skills from the capability catalog and its personas from the persona list
 * (both derive from the same registries). The source folder is never touched.
 *   - /packs {GET}                      → loaded packs + state + contents
 *   - /packs/enable, /packs/disable {POST} → hot-reload toggle (body {pack_id})
 *   - /packs/rescan {POST}              → reconcile with packs/ (add new, hot-delete vanished)
 *
 * Cards are collapsed by default (header + count chips + enable/disable only);
 * expanding reveals the description and the real content lists. A search box
 * filters by id / title / description / skill names. Expanded + query state is
 * kept in memory so it survives a reload (e.g. after a toggle).
 */
(function () {
  "use strict";

  const PANE_ID = "settings-pane-packs";
  const ROOT_ID = "packs-root";
  const apiBase = () => `${window.AkanaCore.baseUrl()}/api/v1/packs`;
  const t = (k, v) => window.AkanaI18n.t(k, v);

  let busy = false;
  let reloadPending = false; // a load() requested while another was in flight — coalesce
  let lastData = { packs: [] }; // last GET payload (for client-side filter/expand)
  const expanded = new Set(); // pack ids currently expanded
  let query = ""; // current search text

  const $ = (id) => document.getElementById(id);
  const root = () => $(ROOT_ID);

  const esc = (value) => window.AkanaCore.escapeAttr(value);
  const api = (method, path, body) => window.AkanaCore.apiJson(apiBase, method, path, body);

  function setStatus(message, isError) {
    const el = $("packs-status");
    if (!el) return;
    el.textContent = message || "";
    el.style.color = isError ? "var(--danger, #c0392b)" : "";
  }

  /** MCP servers the pack declares but the owner has not yet approved+mounted. */
  function pendingConsent(p) {
    return Array.isArray(p.mcp_pending) ? p.mcp_pending : [];
  }

  function stateBadge(p) {
    // An enabled pack whose MCP server still awaits approval is NOT fully live:
    // its tools never reach the LLM until consent. Surface that instead of a bare
    // "enabled" so the toggle state matches what the assistant can actually do.
    if (p.enabled && pendingConsent(p).length) {
      return `<span class="pack-badge pack-badge-pending">${t("pack.state.needs_consent")}</span>`;
    }
    const cls = p.enabled ? "pack-badge-enabled" : "pack-badge-disabled";
    const key = p.enabled ? "pack.state.enabled" : "pack.state.disabled";
    return `<span class="pack-badge ${cls}">${t(key)}</span>`;
  }

  /** Compact content chips (counts) — always visible, even when collapsed. */
  function containsChips(counts) {
    const chips = [];
    if (counts.skills) chips.push(`${counts.skills} ${t("pack.contains.skills")}`);
    if (counts.personas) chips.push(`${counts.personas} ${t("pack.contains.personas")}`);
    if (counts.tools) chips.push(`${counts.tools} ${t("pack.contains.tools")}`);
    if (!chips.length) return `<span class="pack-chip pack-chip-empty">${t("pack.contains.empty")}</span>`;
    return chips.map((x) => `<span class="pack-chip">${esc(x)}</span>`).join("");
  }

  function missingToolsBlock(missing) {
    if (!missing || !missing.length) return "";
    const rows = missing
      .map((m) => {
        const hint = m.install_hint ? ` — <code>${esc(m.install_hint)}</code>` : "";
        return `<li>${esc(m.name)}${hint}</li>`;
      })
      .join("");
    return (
      `<div class="pack-warn">` +
        `<strong>${t("pack.missing_tools_title")}</strong>` +
        `<ul>${rows}</ul>` +
      `</div>`
    );
  }

  /** A "label: a, b, c" detail row (only when the list is non-empty). */
  function detailRow(labelKey, items) {
    if (!items || !items.length) return "";
    return (
      `<div class="pack-detail-row">` +
        `<span class="pack-detail-k">${t(labelKey)}</span>` +
        `<span class="pack-detail-v">${items.map(esc).join(", ")}</span>` +
      `</div>`
    );
  }

  /** Search across id / title / description / skill + persona names. */
  function matchesQuery(p, q) {
    if (!q) return true;
    const c = p.contains || {};
    const hay = [
      p.id, p.title, p.description,
      ...(c.skills || []), ...(c.personas || []),
    ].join(" ").toLowerCase();
    return hay.includes(q.toLowerCase());
  }

  function packCard(p) {
    const counts = p.counts || {};
    const c = p.contains || {};
    const isOpen = expanded.has(p.id);
    const toggleLabel = p.enabled ? t("pack.toggle.disable") : t("pack.toggle.enable");
    const toggleCls = p.enabled ? "btn-ghost btn-sm" : "btn-primary btn-sm";

    const pending = pendingConsent(p);
    // Consent affordance: an enabled pack with pending MCP servers can be mounted
    // by the owner right here (the toggle alone never mounts — that is the
    // human-in-the-loop gate). Without this the card would read "enabled" while
    // every tool call fails with the server missing.
    const consentBlock = (p.enabled && pending.length)
      ? `<div class="pack-warn pack-consent">` +
          `<p>${t("pack.consent.pending_note", { servers: esc(pending.join(", ")) })}</p>` +
          `<button type="button" class="btn-primary btn-sm" data-action="consent" ` +
            `data-id="${esc(p.id)}">${t("pack.consent.approve")}</button>` +
        `</div>`
      : "";

    // Detail (only rendered when expanded) — the real content, not just counts.
    const detail = isOpen
      ? `<div class="pack-detail">` +
          (p.description ? `<p class="pack-desc">${esc(p.description)}</p>` : "") +
          detailRow("pack.detail.skills", c.skills) +
          detailRow("pack.detail.personas", c.personas) +
          detailRow("pack.detail.tools", c.tools) +
          missingToolsBlock(p.missing_tools) +
          consentBlock +
          (p.enabled && counts.skills
            ? `<p class="field-hint pack-catalog-note">${t("pack.catalog_note", { n: counts.skills })}</p>`
            : "") +
          `<code class="pack-id">${esc(p.id)}</code>` +
        `</div>`
      : "";

    return (
      `<div class="pack-card${p.enabled ? "" : " is-disabled"}${isOpen ? " is-open" : ""}">` +
        `<div class="pack-card-head" data-action="expand" data-id="${esc(p.id)}" ` +
          `role="button" tabindex="0" aria-expanded="${isOpen ? "true" : "false"}">` +
          `<span class="pack-caret" aria-hidden="true">${isOpen ? "▾" : "▸"}</span>` +
          `<span class="pack-name">${esc(p.title)}</span>` +
          `<span class="pack-version">v${esc(p.version)}</span>` +
          stateBadge(p) +
          `<button type="button" class="${toggleCls} pack-toggle-btn" data-action="toggle" ` +
            `data-id="${esc(p.id)}" data-enabled="${p.enabled ? "1" : "0"}">${toggleLabel}</button>` +
        `</div>` +
        `<div class="pack-meta">${containsChips(counts)}</div>` +
        detail +
      `</div>`
    );
  }

  /** Fill ONLY the list container (keeps the search box + focus intact). */
  function renderList() {
    const el = $("packs-list");
    if (!el) return;
    const list = (lastData.packs || []).filter((p) => matchesQuery(p, query));
    el.innerHTML = list.length
      ? list.map(packCard).join("")
      : `<p class="field-hint">${query ? t("pack.search_empty") : t("pack.list.empty")}</p>`;
  }

  function render(r) {
    r.innerHTML =
      `<div class="info-callout settings-info-callout">${t("pack.callout")}</div>` +
      `<div class="settings-row-actions pack-toolbar">` +
        `<input type="search" id="packs-search" class="pack-search" autocomplete="off" ` +
          `placeholder="${t("pack.search_ph")}" value="${esc(query)}" />` +
        `<button type="button" class="btn-ghost btn-sm" data-action="rescan">${t("pack.refresh_btn")}</button>` +
        `<p class="hint" id="packs-status" role="status"></p>` +
      `</div>` +
      `<div class="pack-list" id="packs-list"></div>`;
    renderList();
  }

  async function load() {
    const r = root();
    if (!r) return;
    if (busy) {
      reloadPending = true;
      return;
    }
    busy = true;
    try {
      lastData = await api("GET", "");
      render(r);
    } catch (err) {
      r.innerHTML = `<p class="field-hint" style="color:var(--danger,#c0392b)">${t("pack.load_failed", { error: esc(err.message) })}</p>`;
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
    const r = root();
    if (!btn || !r || !r.contains(btn)) return;
    const action = btn.dataset.action;
    try {
      if (action === "expand") {
        const id = btn.dataset.id;
        if (expanded.has(id)) expanded.delete(id);
        else expanded.add(id);
        renderList();
      } else if (action === "rescan") {
        // Single "refresh" button: rescan is a strict superset of a plain GET reload
        // (it returns the same {count,packs} PLUS reconciles with packs/ on disk and
        // reports the added/removed delta). Merged the old separate GET-only "refresh"
        // button into this one so a single click always yields the freshest, disk-
        // accurate view; when nothing changed on disk rescan is a no-op ("No changes").
        btn.disabled = true;
        lastData = await api("POST", "/rescan");
        render(r);
        const added = (lastData.added || []).length;
        const removed = (lastData.removed || []).length;
        if (added && removed) setStatus(t("pack.status.rescan_changed", { added, removed }), false);
        else if (added) setStatus(t("pack.status.rescan_found", { n: added }), false);
        else if (removed) setStatus(t("pack.status.rescan_removed", { n: removed }), false);
        else setStatus(t("pack.status.rescan_none"), false);
      } else if (action === "toggle") {
        const id = btn.dataset.id;
        const isEnabled = btn.dataset.enabled === "1";
        btn.disabled = true;
        await api("POST", isEnabled ? "/disable" : "/enable", { pack_id: id });
        await load();
        setStatus(isEnabled ? t("pack.status.disabled", { id }) : t("pack.status.enabled", { id }), false);
      } else if (action === "consent") {
        // Owner-approved MCP mount (the human-in-the-loop gate). The server uses
        // the manifest's own mcp config; a server with no usable config comes back
        // under needs_config/invalid and nothing is written.
        const id = btn.dataset.id;
        btn.disabled = true;
        const res = await api("POST", "/consent", { pack_id: id });
        await load();
        const mounted = (res && res.result && res.result.mounted) || [];
        if (mounted.length) setStatus(t("pack.status.consented", { servers: mounted.join(", ") }), false);
        else setStatus(t("pack.status.consent_pending"), false);
      }
    } catch (err) {
      btn.disabled = false; // re-enable on error (render() would otherwise replace it)
      setStatus(err.message, true);
    }
  }

  /** Keyboard support for the card header (Enter/Space toggles expand). */
  function onKeydown(event) {
    // A descendant control (e.g. the enable/disable button) handles its own
    // Enter/Space activation — don't let the ancestor header hijack it.
    if (event.target.closest("[data-action]") !== event.target.closest('[data-action="expand"]')) return;
    const head = event.target.closest('[data-action="expand"]');
    if (!head) return;
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      const id = head.dataset.id;
      if (expanded.has(id)) expanded.delete(id);
      else expanded.add(id);
      renderList();
    }
  }

  /** Live search — only re-renders the list, so the input keeps focus. */
  function onInput(event) {
    if (!event.target || event.target.id !== "packs-search") return;
    query = event.target.value || "";
    renderList();
  }

  function ensureStyles() {
    if ($("packs-styles")) return;
    const style = document.createElement("style");
    style.id = "packs-styles";
    style.textContent =
      ".pack-toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:8px 0}" +
      ".pack-search{flex:1 1 200px;min-width:160px;padding:6px 10px;border-radius:8px;border:1px solid var(--border,rgba(128,128,128,.25));background:transparent;color:inherit;font:inherit}" +
      ".pack-list{display:flex;flex-direction:column;gap:8px;margin:8px 0}" +
      ".pack-card{border:1px solid var(--border,rgba(128,128,128,.18));border-radius:var(--j-r-md,10px);padding:8px 12px}" +
      ".pack-card.is-disabled{opacity:.62}" +
      ".pack-card-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;cursor:pointer;user-select:none}" +
      ".pack-caret{opacity:.6;width:1em;display:inline-block;text-align:center;font-size:.8em}" +
      ".pack-name{font-weight:600}" +
      ".pack-version{font-size:.78em;opacity:.6;font-family:var(--j-font-mono,monospace)}" +
      ".pack-toggle-btn{margin-left:auto}" +
      ".pack-badge{font-size:.66em;font-weight:700;letter-spacing:.04em;text-transform:uppercase;padding:.05em .4em;border-radius:var(--j-r-pill,999px);background:color-mix(in srgb,var(--j-ink,#888) 14%,transparent);color:var(--j-ink-dim,#888)}" +
      ".pack-badge-enabled{background:color-mix(in srgb,var(--j-accent,#6aa3ff) 20%,transparent);color:var(--j-accent,#6aa3ff)}" +
      ".pack-badge-disabled{background:color-mix(in srgb,var(--j-ink,#888) 14%,transparent);color:var(--j-ink-dim,#888)}" +
      ".pack-badge-pending{background:color-mix(in srgb,#e0a106 22%,transparent);color:#b6820a}" +
      ".pack-consent{display:flex;flex-direction:column;gap:6px;align-items:flex-start}" +
      ".pack-consent p{margin:0}" +
      ".pack-meta{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 0}" +
      ".pack-chip{font-size:.72em;padding:.12em .5em;border-radius:var(--j-r-pill,999px);background:color-mix(in srgb,var(--j-ink,#888) 12%,transparent)}" +
      ".pack-chip-empty{opacity:.6}" +
      ".pack-detail{margin-top:8px;padding-top:8px;border-top:1px solid var(--border,rgba(128,128,128,.14))}" +
      ".pack-desc{margin:0 0 8px;font-size:.88em;opacity:.85}" +
      ".pack-detail-row{display:flex;gap:8px;font-size:.82em;margin:3px 0;align-items:baseline}" +
      ".pack-detail-k{flex:0 0 92px;font-weight:600;opacity:.7}" +
      ".pack-detail-v{flex:1 1 auto;word-break:break-word;font-family:var(--j-font-mono,monospace);font-size:.92em}" +
      ".pack-warn{margin:8px 0 0;padding:7px 9px;border-radius:8px;font-size:.82em;background:color-mix(in srgb,#e0a106 16%,transparent);border:1px solid color-mix(in srgb,#e0a106 36%,transparent)}" +
      ".pack-warn ul{margin:4px 0 0;padding-left:18px}" +
      ".pack-warn code{font-size:.92em}" +
      ".pack-catalog-note{margin:8px 0 0;font-size:.8em;opacity:.7}" +
      ".pack-id{display:block;margin-top:8px;font-size:.74em;opacity:.5;font-family:var(--j-font-mono,monospace)}";
    document.head.appendChild(style);
  }

  function init() {
    const pane = $(PANE_ID);
    const r = root();
    if (!pane || !r) return;
    ensureStyles();
    r.addEventListener("click", onClick);
    r.addEventListener("keydown", onKeydown);
    r.addEventListener("input", onInput);
    const observer = new MutationObserver(() => { if (!pane.hidden) void load(); });
    observer.observe(pane, { attributes: true, attributeFilter: ["hidden"] });
    if (!pane.hidden) void load();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.AkanaPacks = { load };
})();
