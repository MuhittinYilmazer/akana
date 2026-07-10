/* akana-personas.js — Persona / system-prompt management (self-contained).
 *
 * Renders into #persona-root inside the "Persona" settings pane (#settings-pane-persona).
 * Self-loads when the pane becomes visible (observes its `hidden` attribute) →
 * no hook needed in akana-settings.js (vault pattern; conflict-free file).
 *
 * System prompt = CORE (base) + selected persona text + CAPABILITY CATALOG.
 * All three are edited here:
 *   - /personas {GET, POST}, /personas/{id} {PUT, DELETE}, /personas/{id}/bind {PUT}
 *   - /personas/base {PUT, DELETE}      → core prompt override (akana)
 *   - /personas/catalog {PUT, DELETE}   → capability catalog text override
 *   - /settings/runtime {PUT}           → skill_catalog_enabled toggle
 */
(function () {
  "use strict";

  const PANE_ID = "settings-pane-persona";
  const ROOT_ID = "persona-root";
  const apiBase = () => `${window.AkanaCore.baseUrl()}/api/v1/personas`;
  const runtimeUrl = () => `${window.AkanaCore.baseUrl()}/api/v1/settings/runtime`;
  const headers = (withJson) => window.AkanaCore.authHeaders(withJson);

  let busy = false;
  let reloadPending = false; // a load() requested while another was in flight — coalesce
  let personas = []; // last-loaded list (used to populate edit/fork form)
  let editingId = null; // null = create mode; otherwise the id of the persona being updated

  const $ = (id) => document.getElementById(id);
  const root = () => $(ROOT_ID);

  const esc = (value) => window.AkanaCore.escapeAttr(value);
  const api = (method, path, body) => window.AkanaCore.apiJson(apiBase, method, path, body);

  function setStatusEl(id, message, isError) {
    const el = $(id);
    if (!el) return;
    el.textContent = message || "";
    el.style.color = isError ? "var(--danger, #c0392b)" : "";
  }
  const setStatus = (m, e) => setStatusEl("persona-status", m, e);

  /** Active (default) persona = web channel binding; falls back to builtin akana. */
  function activePersonaId(bindings) {
    const web = (bindings || []).find((b) => b.scope === "channel" && b.key === "web");
    return web ? web.persona_id : "akana";
  }

  function sourceBadge(source) {
    if (source === "builtin")
      return `<span class="persona-badge persona-badge-builtin">${window.AkanaI18n.t("persona.badge.builtin")}</span>`;
    if (source && source.indexOf("pack:") === 0)
      return `<span class="persona-badge persona-badge-pack">${window.AkanaI18n.t("persona.badge.pack")}</span>`;
    return `<span class="persona-badge persona-badge-user">${window.AkanaI18n.t("persona.badge.user")}</span>`;
  }

  function personaCard(p, activeId) {
    const isUser = p.source === "user";
    const isActive = p.id === activeId;
    const actions = [];
    if (isActive) {
      actions.push(`<span class="persona-active">${window.AkanaI18n.t("persona.card.default_star")}</span>`);
    } else {
      actions.push(
        `<button type="button" class="btn-ghost btn-sm" data-action="activate" data-id="${esc(p.id)}">${window.AkanaI18n.t("persona.card.set_default")}</button>`,
      );
    }
    if (isUser) {
      actions.push(
        `<button type="button" class="btn-ghost btn-sm" data-action="edit" data-id="${esc(p.id)}">${window.AkanaI18n.t("persona.card.edit")}</button>`,
        `<button type="button" class="btn-ghost btn-sm" data-action="delete" data-id="${esc(p.id)}">${window.AkanaI18n.t("persona.card.delete")}</button>`,
      );
    } else {
      actions.push(
        `<button type="button" class="btn-ghost btn-sm" data-action="fork" data-id="${esc(p.id)}">${window.AkanaI18n.t("persona.card.fork")}</button>`,
      );
    }
    return (
      `<div class="persona-card${isActive ? " is-active" : ""}">` +
        `<div class="persona-card-head">` +
          `<span class="persona-name">${esc(p.name)}</span>${sourceBadge(p.source)}` +
        `</div>` +
        `<details class="persona-prompt"><summary>${window.AkanaI18n.t("persona.card.prompt_summary")}</summary>` +
          `<pre>${esc(p.system_prompt)}</pre></details>` +
        (p.tone ? `<p class="field-hint">${window.AkanaI18n.t("persona.card.tone", { tone: esc(p.tone) })}</p>` : "") +
        `<div class="persona-card-actions">${actions.join("")}</div>` +
      `</div>`
    );
  }

  function render(root, data) {
    const list = data.personas || [];
    const activeId = activePersonaId(data.bindings);
    const base = data.base || { is_override: false, default: "" };
    const voice = data.voice_directive || { is_override: false, value: "", default: "" };
    const voiceText = voice.value || voice.default || "";
    const catalog = data.catalog || { enabled: true, selection: null, skills: [] };
    const akanaP = list.find((p) => p.id === "akana");
    const baseText = akanaP ? akanaP.system_prompt : base.default || "";
    const cards = list.length
      ? list.map((p) => personaCard(p, activeId)).join("")
      : `<p class="field-hint">${window.AkanaI18n.t("persona.list.empty")}</p>`;

    root.innerHTML =
      `<div class="info-callout settings-info-callout">` +
        window.AkanaI18n.t("persona.callout") +
      `</div>` +

      // -- Core system prompt (base override) --
      `<div class="settings-block">` +
        `<h3 class="settings-block-title">${window.AkanaI18n.t("persona.base.title")} ` +
          (base.is_override
            ? `<span class="persona-badge persona-badge-user">${window.AkanaI18n.t("persona.base.badge_edited")}</span>`
            : `<span class="persona-badge">${window.AkanaI18n.t("persona.base.badge_default")}</span>`) +
        `</h3>` +
        `<p class="field-hint">${window.AkanaI18n.t("persona.base.hint")}</p>` +
        (base.is_override
          ? `<p class="field-hint field-hint-warn">${window.AkanaI18n.t("persona.base.override_lang_hint")}</p>`
          : "") +
        `<textarea id="persona-base-text" rows="8" spellcheck="false">${esc(baseText)}</textarea>` +
        `<div class="settings-row-actions">` +
          `<button type="button" class="btn-primary" data-action="save-base">${window.AkanaI18n.t("persona.base.save_btn")}</button>` +
          (base.is_override
            ? `<button type="button" class="btn-ghost btn-sm" data-action="reset-base">${window.AkanaI18n.t("persona.base.reset_btn")}</button>`
            : "") +
          `<p class="hint" id="persona-base-status" role="status"></p>` +
        `</div>` +
      `</div>` +

      // -- Voice-mode directive (override) --
      `<div class="settings-block">` +
        `<h3 class="settings-block-title">${window.AkanaI18n.t("persona.voice.title")} ` +
          (voice.is_override
            ? `<span class="persona-badge persona-badge-user">${window.AkanaI18n.t("persona.voice.badge_edited")}</span>`
            : `<span class="persona-badge">${window.AkanaI18n.t("persona.voice.badge_default")}</span>`) +
        `</h3>` +
        `<p class="field-hint">${window.AkanaI18n.t("persona.voice.hint")}</p>` +
        (voice.is_override
          ? `<p class="field-hint field-hint-warn">${window.AkanaI18n.t("persona.voice.override_lang_hint")}</p>`
          : "") +
        `<textarea id="persona-voice-text" rows="4" spellcheck="false">${esc(voiceText)}</textarea>` +
        `<div class="settings-row-actions">` +
          `<button type="button" class="btn-primary" data-action="save-voice">${window.AkanaI18n.t("persona.voice.save_btn")}</button>` +
          (voice.is_override
            ? `<button type="button" class="btn-ghost btn-sm" data-action="reset-voice">${window.AkanaI18n.t("persona.voice.reset_btn")}</button>`
            : "") +
          `<p class="hint" id="persona-voice-status" role="status"></p>` +
        `</div>` +
      `</div>` +

      // -- Persona create / edit form --
      `<div class="settings-block">` +
        `<h3 class="settings-block-title" id="persona-form-title">${window.AkanaI18n.t("persona.form.title_new")}</h3>` +
        `<label for="persona-f-name">${window.AkanaI18n.t("persona.form.name_label")}</label>` +
        `<input id="persona-f-name" autocomplete="off" placeholder="${window.AkanaI18n.t("persona.form.name_ph")}" />` +
        `<label for="persona-f-prompt">${window.AkanaI18n.t("persona.form.prompt_label")}</label>` +
        `<textarea id="persona-f-prompt" rows="6" placeholder="${window.AkanaI18n.t("persona.form.prompt_ph")}"></textarea>` +
        `<label for="persona-f-tone">${window.AkanaI18n.t("persona.form.tone_label")} <span class="label-optional">${window.AkanaI18n.t("persona.form.tone_optional")}</span></label>` +
        `<input id="persona-f-tone" autocomplete="off" placeholder="${window.AkanaI18n.t("persona.form.tone_ph")}" />` +
        `<div class="settings-row-actions">` +
          `<button type="button" class="btn-primary" data-action="save">${window.AkanaI18n.t("persona.form.save_btn")}</button>` +
          `<button type="button" class="btn-ghost btn-sm" data-action="cancel-edit" hidden>${window.AkanaI18n.t("persona.form.cancel_btn")}</button>` +
          `<p class="hint" id="persona-status" role="status"></p>` +
        `</div>` +
      `</div>` +

      // -- Persona list --
      `<div class="settings-block">` +
        `<h3 class="settings-block-title">${window.AkanaI18n.t("persona.list.title")}</h3>` +
        `<div class="persona-list">${cards}</div>` +
        `<div class="settings-row-actions">` +
          `<button type="button" class="btn-ghost btn-sm" data-action="refresh">${window.AkanaI18n.t("persona.list.refresh")}</button>` +
        `</div>` +
      `</div>` +

      // -- CAPABILITY CATALOG (toggle + skill selection) --
      `<div class="settings-block">` +
        `<h3 class="settings-block-title">${window.AkanaI18n.t("persona.catalog.title")}</h3>` +
        `<p class="field-hint">${window.AkanaI18n.t("persona.catalog.hint")}</p>` +
        `<label class="persona-toggle"><input type="checkbox" id="persona-catalog-enabled"` +
          `${catalog.enabled ? " checked" : ""} /> ` +
          `<span>${window.AkanaI18n.t("persona.catalog.toggle_label")}</span></label>` +
        `<div class="persona-skill-list">` +
          (catalog.skills && catalog.skills.length
            ? `<div class="persona-skill-actions">` +
                `<button type="button" class="btn-ghost btn-sm" data-action="skills-all">${window.AkanaI18n.t("persona.catalog.all_btn")}</button>` +
                `<button type="button" class="btn-ghost btn-sm" data-action="skills-none">${window.AkanaI18n.t("persona.catalog.none_btn")}</button>` +
              `</div>` +
              catalog.skills
                .map(
                  (s) =>
                    `<label class="persona-skill"><input type="checkbox" class="persona-skill-cb" ` +
                    `value="${esc(s.id)}"${s.included ? " checked" : ""} /> <span>${esc(s.label)}</span></label>`,
                )
                .join("")
            : `<p class="field-hint">${window.AkanaI18n.t("persona.catalog.empty")}</p>`) +
        `</div>` +
        `<div class="settings-row-actions">` +
          `<button type="button" class="btn-primary" data-action="save-catalog">${window.AkanaI18n.t("persona.catalog.save_btn")}</button>` +
          (catalog.selection !== null
            ? `<button type="button" class="btn-ghost btn-sm" data-action="reset-catalog">${window.AkanaI18n.t("persona.catalog.reset_btn")}</button>`
            : "") +
          `<p class="hint" id="persona-catalog-status" role="status"></p>` +
        `</div>` +
      `</div>`;
  }

  /** Put the form into edit/create mode (NO re-render; fill fields directly). */
  function fillForm({ name, system_prompt, tone }, { mode }) {
    const nameEl = $("persona-f-name");
    const promptEl = $("persona-f-prompt");
    const toneEl = $("persona-f-tone");
    if (!nameEl || !promptEl || !toneEl) return;
    nameEl.value = name || "";
    promptEl.value = system_prompt || "";
    toneEl.value = tone || "";
    const title = $("persona-form-title");
    const cancel = root().querySelector('[data-action="cancel-edit"]');
    if (mode === "edit") {
      if (title) title.textContent = window.AkanaI18n.t("persona.form.title_edit");
      if (cancel) cancel.hidden = false;
    } else {
      if (title) title.textContent = window.AkanaI18n.t("persona.form.title_new");
      if (cancel) cancel.hidden = true;
    }
    nameEl.focus();
    nameEl.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function resetForm() {
    editingId = null;
    const nameEl = $("persona-f-name");
    const promptEl = $("persona-f-prompt");
    const toneEl = $("persona-f-tone");
    if (nameEl) nameEl.value = "";
    if (promptEl) promptEl.value = "";
    if (toneEl) toneEl.value = "";
    const title = $("persona-form-title");
    if (title) title.textContent = window.AkanaI18n.t("persona.form.title_new");
    const cancel = root() && root().querySelector('[data-action="cancel-edit"]');
    if (cancel) cancel.hidden = true;
  }

  async function load() {
    const r = root();
    if (!r) return;
    // A load() requested while one is in flight must NOT be dropped: the dropped
    // request is usually the one carrying post-mutation truth (e.g. after a
    // reset, is_override=false), so dropping it leaves a stale render (the reset
    // button never disappears). Coalesce instead — re-run once when the current
    // load settles so the freshest server state is always the one rendered.
    if (busy) {
      reloadPending = true;
      return;
    }
    busy = true;
    try {
      const data = await api("GET", "");
      personas = data.personas || [];
      editingId = null;
      render(r, data);
    } catch (err) {
      r.innerHTML = `<p class="field-hint" style="color:var(--danger,#c0392b)">${window.AkanaI18n.t("persona.load_failed", { error: esc(err.message) })}</p>`;
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
    const byId = (id) => personas.find((p) => p.id === id);
    // Which status line should errors be shown in?
    let statusId = "persona-status";
    if (action === "save-base" || action === "reset-base") statusId = "persona-base-status";
    else if (action === "save-voice" || action === "reset-voice")
      statusId = "persona-voice-status";
    else if (action === "save-catalog" || action === "reset-catalog")
      statusId = "persona-catalog-status";
    try {
      if (action === "refresh") {
        await load();
      } else if (action === "save") {
        const name = ($("persona-f-name").value || "").trim();
        const system_prompt = ($("persona-f-prompt").value || "").trim();
        const tone = ($("persona-f-tone").value || "").trim();
        if (!name || !system_prompt) return setStatus(window.AkanaI18n.t("persona.status.name_required"), true);
        const wasEdit = !!editingId;
        if (editingId) {
          await api("PUT", `/${encodeURIComponent(editingId)}`, { name, system_prompt, tone });
        } else {
          await api("POST", "", { name, system_prompt, tone });
        }
        await load(); // re-render first (otherwise load() clears the status message), then set status
        setStatus(wasEdit ? window.AkanaI18n.t("persona.status.updated", { name }) : window.AkanaI18n.t("persona.status.created", { name }), false);
      } else if (action === "edit") {
        const p = byId(btn.dataset.id);
        if (!p) return;
        editingId = p.id;
        fillForm(p, { mode: "edit" });
      } else if (action === "fork") {
        const p = byId(btn.dataset.id);
        if (!p) return;
        editingId = null; // fork = create NEW persona
        fillForm({ name: `${p.name} ${window.AkanaI18n.t("persona.fork.name_suffix")}`, system_prompt: p.system_prompt, tone: p.tone }, { mode: "create" });
        setStatus(window.AkanaI18n.t("persona.status.fork_ready"), false);
      } else if (action === "cancel-edit") {
        resetForm();
      } else if (action === "delete") {
        const p = byId(btn.dataset.id);
        if (!p || !confirm(window.AkanaI18n.t("persona.confirm.delete", { name: p.name }))) return;
        await api("DELETE", `/${encodeURIComponent(p.id)}`);
        await load();
        setStatus(window.AkanaI18n.t("persona.status.deleted", { name: p.name }), false);
      } else if (action === "activate") {
        await api("PUT", `/${encodeURIComponent(btn.dataset.id)}/bind`, { channel: "web" });
        await load();
        setStatus(window.AkanaI18n.t("persona.status.activated"), false);
      } else if (action === "save-base") {
        const text = ($("persona-base-text").value || "").trim();
        if (!text) return setStatusEl(statusId, window.AkanaI18n.t("persona.status.base_empty"), true);
        await api("PUT", "/base", { system_prompt: text });
        await load();
        setStatusEl(statusId, window.AkanaI18n.t("persona.status.base_saved"), false);
      } else if (action === "reset-base") {
        if (!confirm(window.AkanaI18n.t("persona.confirm.reset_base"))) return;
        await api("DELETE", "/base");
        await load();
        setStatusEl(statusId, window.AkanaI18n.t("persona.status.base_reset"), false);
      } else if (action === "save-voice") {
        const text = ($("persona-voice-text").value || "").trim();
        if (!text) return setStatusEl(statusId, window.AkanaI18n.t("persona.status.voice_empty"), true);
        await api("PUT", "/voice-directive", { voice_directive: text });
        await load();
        setStatusEl(statusId, window.AkanaI18n.t("persona.status.voice_saved"), false);
      } else if (action === "reset-voice") {
        if (!confirm(window.AkanaI18n.t("persona.confirm.reset_voice"))) return;
        await api("DELETE", "/voice-directive");
        await load();
        setStatusEl(statusId, window.AkanaI18n.t("persona.status.voice_reset"), false);
      } else if (action === "save-catalog") {
        const ids = Array.prototype.map.call(
          r.querySelectorAll(".persona-skill-cb:checked"),
          (c) => c.value,
        );
        await api("PUT", "/catalog", { selection: ids });
        await load();
        setStatusEl(statusId, window.AkanaI18n.t("persona.status.catalog_saved", { n: ids.length }), false);
      } else if (action === "reset-catalog") {
        await api("DELETE", "/catalog");
        await load();
        setStatusEl(statusId, window.AkanaI18n.t("persona.status.catalog_reset"), false);
      } else if (action === "skills-all" || action === "skills-none") {
        const on = action === "skills-all";
        Array.prototype.forEach.call(r.querySelectorAll(".persona-skill-cb"), (c) => {
          c.checked = on;
        });
        return; // UI only; written when user clicks "Save selection"
      }
    } catch (err) {
      setStatusEl(statusId, err.message, true);
    }
  }

  /** Catalog enable/disable checkbox — 'change' captures label clicks too (click would
   *  only fire when the box itself is pressed). Writes the skill_catalog_enabled runtime setting. */
  async function onChange(event) {
    const el = event.target;
    if (!el || el.id !== "persona-catalog-enabled") return;
    const enabled = !!el.checked;
    try {
      const res = await fetch(runtimeUrl(), {
        method: "PUT",
        headers: headers(true),
        body: JSON.stringify({ settings: { skill_catalog_enabled: enabled } }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setStatusEl("persona-catalog-status", enabled ? window.AkanaI18n.t("persona.status.catalog_on") : window.AkanaI18n.t("persona.status.catalog_off"), false);
    } catch (err) {
      el.checked = !enabled; // revert UI
      setStatusEl("persona-catalog-status", window.AkanaI18n.t("persona.status.catalog_toggle_fail"), true);
    }
  }

  function ensureStyles() {
    if ($("persona-styles")) return;
    const style = document.createElement("style");
    style.id = "persona-styles";
    style.textContent =
      "#persona-root textarea{width:100%;resize:vertical;min-height:84px;font-family:var(--j-font-ui)}" +
      "#persona-root #persona-base-text{font-family:var(--j-font-mono,monospace);font-size:.85em}" +
      ".persona-toggle{display:flex;align-items:center;gap:8px;margin:6px 0 10px;cursor:pointer}" +
      ".persona-toggle input{width:auto;margin:0}" +
      ".persona-skill-list{margin:6px 0 10px;max-height:260px;overflow:auto;border:1px solid var(--border,rgba(128,128,128,.18));border-radius:8px;padding:8px}" +
      ".persona-skill-actions{display:flex;gap:8px;margin-bottom:6px}" +
      ".persona-skill{display:flex;align-items:center;gap:8px;padding:3px 0;cursor:pointer}" +
      ".persona-skill input{width:auto;margin:0}" +
      ".persona-list{display:flex;flex-direction:column;gap:10px;margin:8px 0}" +
      ".persona-card{border:1px solid var(--border,rgba(128,128,128,.18));border-radius:var(--j-r-md,10px);padding:10px 12px}" +
      ".persona-card.is-active{border-color:var(--j-accent,#6aa3ff);box-shadow:0 0 0 1px var(--j-accent,#6aa3ff) inset}" +
      ".persona-card-head{display:flex;align-items:center;gap:8px}" +
      ".persona-name{font-weight:600}" +
      ".persona-badge{font-size:.66em;font-weight:700;letter-spacing:.04em;text-transform:uppercase;padding:.05em .4em;border-radius:var(--j-r-pill,999px);background:color-mix(in srgb,var(--j-ink,#888) 14%,transparent);color:var(--j-ink-dim,#888)}" +
      ".persona-badge-user{background:color-mix(in srgb,var(--j-accent,#6aa3ff) 20%,transparent);color:var(--j-accent,#6aa3ff)}" +
      ".persona-prompt{margin:6px 0}" +
      ".persona-prompt summary{cursor:pointer;opacity:.7;font-size:.85em}" +
      ".persona-prompt pre{white-space:pre-wrap;word-break:break-word;margin:6px 0 0;padding:8px;border-radius:8px;background:color-mix(in srgb,var(--j-bg,#111) 50%,transparent);font-size:.85em;max-height:200px;overflow:auto}" +
      ".persona-card-actions{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:8px}" +
      ".persona-active{font-size:.8em;font-weight:700;color:var(--j-accent,#6aa3ff)}";
    document.head.appendChild(style);
  }

  function init() {
    const pane = $(PANE_ID);
    const r = root();
    if (!pane || !r) return;
    ensureStyles();
    r.addEventListener("click", onClick);
    r.addEventListener("change", onChange);
    const observer = new MutationObserver(() => { if (!pane.hidden) void load(); });
    observer.observe(pane, { attributes: true, attributeFilter: ["hidden"] });
    if (!pane.hidden) void load();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.AkanaPersonas = { load };
})();
