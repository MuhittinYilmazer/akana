/* akana-observability.js — Observability panel (self-contained).
 *
 * Renders into #observability-root inside the "Observability" settings pane
 * (#settings-pane-observability). Self-loads when the pane becomes visible
 * (observes its `hidden` attribute) → no hook needed in akana-settings.js
 * (same conflict-free pattern as akana-packs.js / vault / persona).
 *
 * Source: GET /api/v1/observability/summary (akana_server/api/routes/observability.py) —
 * a single read-side aggregation of four already-existing backend sources:
 *   - metrics  → the in-process counters/timers registry (also at /system/metrics)
 *   - usage    → provider-usage totals from persisted conversations (bounded scan;
 *                see the `note` field — persisted turns carry NO per-turn provider,
 *                so this is a total across all providers, not a per-provider split)
 *   - health   → circuit breaker states (also at /network/status) + active provider
 *   - audit    → the last N audit events (also at /system/audit/tail)
 *
 * Auto-refresh: polls every OBS_REFRESH_MS while the pane is visible AND the
 * document itself is visible (tab focused) — stops the interval the moment
 * either goes hidden, so a backgrounded browser tab or an inactive settings
 * tab never wastes a request.
 */
(function () {
  "use strict";

  const PANE_ID = "settings-pane-observability";
  const ROOT_ID = "observability-root";
  const OBS_REFRESH_MS = 10000;

  const apiBase = () => `${window.AkanaCore.baseUrl()}/api/v1/observability`;
  const t = (k, v) => window.AkanaI18n.t(k, v);
  const esc = (value) => window.AkanaCore.escapeHtml(value);
  const api = (method, path, body) => window.AkanaCore.apiJson(apiBase, method, path, body);

  const $ = (id) => document.getElementById(id);
  const root = () => $(ROOT_ID);

  let busy = false;
  let reloadPending = false; // a load() requested while another was in flight — coalesce
  let pollTimer = null;

  // -- formatting ---------------------------------------------------------------

  function fmtInt(n) {
    const v = Number(n);
    // Fixed "en-US" grouping regardless of the host OS locale (thousands via ",")
    // — the UI's EN/TR toggle is a separate, explicit i18n concern (akana-i18n.js);
    // letting the number grouping silently follow the machine's OS locale would
    // make a stat tile render differently on two identically-configured servers.
    return Number.isFinite(v) ? Math.round(v).toLocaleString("en-US") : "—";
  }

  function fmtMs(n) {
    const v = Number(n);
    if (!Number.isFinite(v)) return "—";
    return v < 1000 ? `${Math.round(v)} ms` : `${(v / 1000).toFixed(2)} s`;
  }

  function fmtCost(n) {
    const v = Number(n);
    if (!Number.isFinite(v) || v <= 0) return "$0.00";
    return `$${v.toFixed(v < 1 ? 4 : 2)}`;
  }

  function fmtTime(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? String(iso) : d.toLocaleTimeString();
  }

  // -- render: stat tiles ---------------------------------------------------------

  function statTile(label, value, hint, tone) {
    const cls = `stat-card stat-card-rich${tone === "ok" ? " stat-ok" : tone === "bad" ? " stat-bad" : ""}`;
    const hintHtml = hint ? `<span class="stat-card-desc">${esc(hint)}</span>` : "";
    return (
      `<div class="${cls}">` +
        `<span class="stat-card-label">${esc(label)}</span>` +
        `<span class="stat-card-value">${esc(value)}${hintHtml}</span>` +
      `</div>`
    );
  }

  function renderStats(data) {
    const usage = data.usage || {};
    const tokens = usage.tokens || {};
    const provider = (data.health && data.health.active_provider) || "";
    const tiles = [
      statTile(
        t("observability.stat.turns_total"),
        fmtInt(usage.turns_total),
        t("observability.stat.turns_hint", {
          days: usage.window_days ?? 7,
          n: usage.conversations_in_window ?? 0,
        }),
      ),
      statTile(t("observability.stat.prompt_tokens"), fmtInt(tokens.prompt)),
      statTile(t("observability.stat.completion_tokens"), fmtInt(tokens.completion)),
      statTile(t("observability.stat.total_tokens"), fmtInt(tokens.total)),
      statTile(t("observability.stat.cost"), fmtCost(usage.cost_usd)),
      statTile(
        t("observability.stat.active_provider"),
        provider || t("observability.stat.unconfigured"),
        null,
        provider ? "ok" : "bad",
      ),
    ];
    // Per-provider token breakdown (present once turns carry the provider stamp).
    // When absent (all-legacy history) we keep the aggregate-only note instead.
    const perProvider = usage.per_provider;
    let breakdown = "";
    if (perProvider && typeof perProvider === "object") {
      const names = Object.keys(perProvider).sort();
      const rows = names
        .map((name) => {
          const b = perProvider[name] || {};
          const label = name === "unknown"
            ? t("observability.provider_unknown")
            : name;
          const val = t("observability.provider_tokens", {
            total: fmtInt((b.prompt || 0) + (b.completion || 0)),
            turns: fmtInt(b.turns),
          });
          return `<dt>${esc(label)}</dt><dd>${esc(val)}</dd>`;
        })
        .join("");
      breakdown =
        `<p class="observability-subhead">${esc(t("observability.per_provider_title"))}</p>` +
        `<dl class="settings-meta-list observability-metrics-list">${rows}</dl>`;
    }
    const note = usage.provider_attribution
      ? ""
      : `<p class="field-hint observability-note">${esc(t("observability.usage_note"))}</p>`;
    return (
      `<div class="stat-grid stat-grid-rich">${tiles.join("")}</div>` +
      breakdown +
      note
    );
  }

  // -- render: breaker / provider health -------------------------------------------

  function breakerTone(state) {
    if (state === "closed") return "is-ok";
    if (state === "half_open") return "is-warn";
    return "is-bad"; // "open"
  }

  function breakerStateLabel(state) {
    // t() returns the KEY itself on a miss (never ""), so `|| String(state)` was
    // dead code — an unknown breaker state would render the raw i18n key. Compare
    // against the key so a state without a translation falls back to the raw value.
    const key = `observability.breaker_state.${state}`;
    const label = t(key);
    return label === key ? String(state || "?") : label;
  }

  function renderHealth(data) {
    const health = data.health || {};
    const breakers = Array.isArray(health.breakers) ? health.breakers : [];
    const rows = breakers.length
      ? breakers
          .map((b) => {
            const pill =
              `<span class="settings-health-pill ${breakerTone(b.state)}">` +
              `${esc(b.name)} · ${esc(breakerStateLabel(b.state))}</span>`;
            const detail = t("observability.breaker_failures", {
              failures: fmtInt(b.failures),
              threshold: fmtInt(b.threshold),
              retry: fmtInt(b.retry_after),
            });
            return `<div class="observability-breaker-row">${pill}<span class="field-hint">${esc(detail)}</span></div>`;
          })
          .join("")
      : `<p class="field-hint">${esc(t("observability.breaker_empty"))}</p>`;
    return (
      `<div class="settings-block observability-block">` +
        `<h3 class="settings-block-title">${esc(t("observability.health_title"))}</h3>` +
        `<p class="field-hint">${esc(
          t("observability.health_active", {
            provider: health.active_provider || t("observability.stat.unconfigured"),
          }),
        )}</p>` +
        `<div class="observability-breaker-list">${rows}</div>` +
      `</div>`
    );
  }

  // -- render: metrics counters/timers table -----------------------------------------

  function renderMetrics(data) {
    const metrics = data.metrics || {};
    const counters = metrics.counters || {};
    const timers = metrics.timers || {};
    const counterNames = Object.keys(counters).sort();
    const timerNames = Object.keys(timers).sort();

    if (!counterNames.length && !timerNames.length) {
      return (
        `<div class="settings-block observability-block">` +
          `<h3 class="settings-block-title">${esc(t("observability.metrics_title"))}</h3>` +
          `<p class="field-hint">${esc(t("observability.metrics_empty"))}</p>` +
        `</div>`
      );
    }

    const counterRows = counterNames
      .map((name) => `<dt>${esc(name)}</dt><dd>${fmtInt((counters[name] || {}).value)}</dd>`)
      .join("");
    const timerRows = timerNames
      .map((name) => {
        const tm = timers[name] || {};
        const value = t("observability.metrics_timer_value", {
          avg: fmtMs(tm.avg_ms),
          count: fmtInt(tm.count),
        });
        return `<dt>${esc(name)}</dt><dd>${esc(value)}</dd>`;
      })
      .join("");

    return (
      `<div class="settings-block observability-block">` +
        `<h3 class="settings-block-title">${esc(t("observability.metrics_title"))}</h3>` +
        (counterNames.length
          ? `<p class="observability-subhead">${esc(t("observability.metrics_counter"))}</p>` +
            `<dl class="settings-meta-list observability-metrics-list">${counterRows}</dl>`
          : "") +
        (timerNames.length
          ? `<p class="observability-subhead">${esc(t("observability.metrics_timer"))}</p>` +
            `<dl class="settings-meta-list observability-metrics-list">${timerRows}</dl>`
          : "") +
      `</div>`
    );
  }

  // -- render: audit tail (monospace, newest first) ------------------------------------

  function renderAudit(data) {
    const audit = data.audit || {};
    // read_tail (backend) returns chronological (oldest→newest) — reverse here so
    // the panel reads newest-first, a display concern kept out of the API contract.
    const events = Array.isArray(audit.events) ? audit.events.slice().reverse() : [];
    const rows = events.length
      ? events
          .map((e) => {
            const parts = [fmtTime(e.ts), String(e.kind || "?")];
            if (e.conv_id) parts.push(`conv=${e.conv_id}`);
            if (e.turn_id) parts.push(`turn=${e.turn_id}`);
            return `<li class="observability-audit-row">${esc(parts.join("  ·  "))}</li>`;
          })
          .join("")
      : "";
    const body = events.length
      ? `<ul class="observability-audit-list">${rows}</ul>`
      : `<p class="field-hint">${esc(t("observability.audit_empty"))}</p>`;
    return (
      `<div class="settings-block observability-block">` +
        `<h3 class="settings-block-title">${esc(t("observability.audit_title"))}</h3>` +
        `<p class="field-hint">${esc(t("observability.audit_count", { n: audit.count ?? events.length }))}</p>` +
        body +
      `</div>`
    );
  }

  // -- top-level render + load ------------------------------------------------------

  function toolbarHtml() {
    return (
      `<div class="settings-row-actions observability-toolbar">` +
        `<button type="button" class="btn-ghost btn-sm" data-action="refresh">${t("observability.refresh_btn")}</button>` +
        `<p class="field-hint" id="observability-status" role="status"></p>` +
      `</div>`
    );
  }

  function setStatus(message, isError) {
    const statusEl = $("observability-status");
    if (!statusEl) return false;
    statusEl.textContent = message;
    statusEl.style.color = isError ? "var(--danger,#c0392b)" : "";
    return true;
  }

  function render(r, data) {
    r.innerHTML =
      toolbarHtml() +
      renderStats(data) +
      renderHealth(data) +
      renderMetrics(data) +
      renderAudit(data);
    setStatus(
      t("observability.status_updated", { time: new Date().toLocaleTimeString() }),
      false,
    );
  }

  function renderError(r, message) {
    // A refresh failure must NOT wipe the whole panel (that would destroy the
    // refresh button the user needs to retry). Write the error into the status
    // line and keep the last-good content. Only on a first-ever failure (no toolbar
    // rendered yet) do we lay down a minimal shell so the refresh button exists.
    if (!setStatus(message, true)) {
      r.innerHTML = toolbarHtml();
      setStatus(message, true);
    }
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
      const data = await api("GET", "/summary");
      render(r, data);
    } catch (err) {
      renderError(r, t("observability.load_failed", { error: err.message }));
    } finally {
      busy = false;
    }
    if (reloadPending) {
      reloadPending = false;
      await load();
    }
  }

  async function onClick(event) {
    const btn = event.target.closest('[data-action="refresh"]');
    const r = root();
    if (!btn || !r || !r.contains(btn)) return;
    void load();
  }

  // -- polling: only while the pane AND the document are both visible ----------------

  function settingsOverlayOpen() {
    // The Settings overlay is toggled by akana-settings.js via `body.settings-open`
    // (open/closeSettings). Closing it (Escape / backdrop / close button) removes
    // that class but NEVER sets the observability pane's `hidden` attribute — so a
    // `hidden`-only visibility gate would leave the 10s poll running for the whole
    // session after the user closes Settings. Gate on the overlay class too.
    const body = document.body;
    return !!body && body.classList.contains("settings-open");
  }

  function isPaneVisible() {
    const pane = $(PANE_ID);
    return !!pane && !pane.hidden && settingsOverlayOpen();
  }

  function stopPolling() {
    // `!== null`, NOT a truthy check: a timer handle of 0 is falsy but still a
    // real, live interval that must be cleared (setInterval never returns 0 in
    // a real browser/Node, but the polling gate must not silently leak one on
    // the day that assumption stops holding).
    if (pollTimer !== null) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(() => {
      void load();
    }, OBS_REFRESH_MS);
  }

  function syncPolling() {
    const shouldPoll = isPaneVisible() && document.visibilityState !== "hidden";
    if (shouldPoll) startPolling();
    else stopPolling();
  }

  // -- scoped styles (no new CSS file — a handful of layout-only rules not covered
  // by an existing utility class; every color comes from an existing --j-* token,
  // never --j-accent* per the premium-skin retone contract) --------------------------

  function ensureStyles() {
    if ($("observability-styles")) return;
    const style = document.createElement("style");
    style.id = "observability-styles";
    style.textContent =
      ".observability-toolbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:0 0 10px}" +
      ".observability-block{margin-top:12px}" +
      ".observability-note{font-style:italic;opacity:.85}" +
      ".observability-subhead{margin:8px 0 4px;font-size:.76em;font-weight:700;text-transform:uppercase;" +
      "letter-spacing:.06em;color:var(--j-ink-faint,inherit);opacity:.8}" +
      ".observability-breaker-list{display:flex;flex-direction:column;gap:6px;margin-top:6px}" +
      ".observability-breaker-row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}" +
      ".observability-metrics-list{margin:4px 0 10px}" +
      ".observability-audit-list{list-style:none;margin:6px 0 0;padding:0;max-height:280px;" +
      "overflow-y:auto;display:flex;flex-direction:column;gap:2px}" +
      ".observability-audit-row{font-family:var(--j-font-mono,monospace);font-size:.78em;" +
      "padding:3px 6px;border-radius:6px;background:color-mix(in srgb,var(--j-ink,#888) 6%,transparent);" +
      "white-space:pre-wrap;word-break:break-word}";
    document.head.appendChild(style);
  }

  function init() {
    const pane = $(PANE_ID);
    const r = root();
    if (!pane || !r) return;
    ensureStyles();
    r.addEventListener("click", onClick);
    const observer = new MutationObserver(() => {
      syncPolling();
      if (isPaneVisible()) void load();
    });
    observer.observe(pane, { attributes: true, attributeFilter: ["hidden"] });
    // Also watch `body.settings-open`: closing the Settings overlay (Escape /
    // backdrop) toggles that class WITHOUT touching pane.hidden, so without this
    // the poll (and its server-side scan) would never stop. Re-sync on every class
    // flip; only kick a load when the pane actually becomes visible.
    if (document.body) {
      const overlayObserver = new MutationObserver(() => {
        syncPolling();
        if (isPaneVisible()) void load();
      });
      overlayObserver.observe(document.body, {
        attributes: true,
        attributeFilter: ["class"],
      });
    }
    document.addEventListener("visibilitychange", syncPolling);
    if (isPaneVisible()) void load();
    syncPolling();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.AkanaObservability = {
    load,
    // Test-only seam (node-vm contract harness): exercise the visibility-gated
    // polling logic + pure render() directly, without a real MutationObserver.
    _test: {
      render,
      syncPolling,
      isPaneVisible,
      isPolling: () => pollTimer !== null,
    },
  };
})();
