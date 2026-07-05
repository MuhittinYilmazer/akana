/**
 * Akana Memory API client — single source of truth for the memory API contract.
 * All fetch paths are collected in the PATHS constant (tests/web contract test reads it).
 * Studio communicates through this file and never calls fetch directly.
 */
(() => {
  // Contract: akana_server memory API (see tests/web/memory_studio_contract.harness.mjs)
  const PATHS = Object.freeze({
    staging: "/api/v1/memory/staging",
    stagingApprove: "/api/v1/memory/staging/{id}/approve",
    stagingReject: "/api/v1/memory/staging/{id}/reject",
    facts: "/api/v1/memory/facts",
    fact: "/api/v1/memory/facts/{id}",
    recall: "/api/v1/memory/recall",
    settings: "/api/v1/memory/settings",
    stats: "/api/v1/memory/stats",
    timeline: "/api/v1/memory/timeline",
  });

  const core = () => window.AkanaCore;
  const fill = (tpl, id) => tpl.replace("{id}", encodeURIComponent(id));

  function buildUrl(path, params) {
    const url = `${core().baseUrl()}${path}`;
    if (!params) return url;
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v === undefined || v === null || v === "") continue;
      qs.set(k, String(v));
    }
    const s = qs.toString();
    return s ? `${url}?${s}` : url;
  }

  async function request(method, path, { params, body } = {}) {
    const hasBody = body !== undefined;
    const r = await fetch(buildUrl(path, params), {
      method,
      headers: core().authHeaders(hasBody),
      body: hasBody ? JSON.stringify(body) : undefined,
    });
    if (!r.ok) {
      const errBody = await r.json().catch(() => null);
      throw new Error(core().parseApiError(errBody, r.status));
    }
    if (r.status === 204) return null;
    return r.json().catch(() => null);
  }

  /** Response may be a bare array or {items|facts|results:[...]} — accept both. */
  function asList(data) {
    if (Array.isArray(data)) return data;
    if (data && typeof data === "object") {
      for (const k of ["items", "facts", "results"]) {
        if (Array.isArray(data[k])) return data[k];
      }
    }
    return [];
  }

  const api = {
    PATHS,

    // ─── Staging (Inbox) ───
    async listStaging(status = "pending") {
      return asList(await request("GET", PATHS.staging, { params: { status } }));
    },
    approveStaging: (id) => request("POST", fill(PATHS.stagingApprove, id)),
    rejectStaging: (id) => request("POST", fill(PATHS.stagingReject, id)),

    // ─── Facts ───
    /**
     * Browse/search facts. Returns {items, total, offset, limit} so the Studio
     * can drive a Prev/Next pager (total = full match count in browse mode).
     */
    async listFacts({ q, limit, offset, includeInvalidated } = {}) {
      const data = await request("GET", PATHS.facts, {
        params: {
          q,
          limit,
          offset,
          include_invalidated: includeInvalidated ? "true" : undefined,
        },
      });
      const items = asList(data);
      const total =
        data && typeof data.total === "number" ? data.total : items.length;
      const off = data && typeof data.offset === "number" ? data.offset : offset || 0;
      const lim = data && typeof data.limit === "number" ? data.limit : limit;
      return { items, total, offset: off, limit: lim };
    },
    /** body: {value, key?, kind?, trust?} */
    createFact: (body) => request("POST", PATHS.facts, { body }),
    /** mode: "supersede" | "correct" */
    updateFact: (id, newValue, mode) =>
      request("PATCH", fill(PATHS.fact, id), { body: { new_value: newValue, mode } }),
    deleteFact: (id, { hard = false } = {}) =>
      request("DELETE", fill(PATHS.fact, id), { params: { hard: hard ? "true" : "false" } }),

    // ─── Recall / Settings / Stats / Timeline ───
    /**
     * → {items:[...], trace:{strategy,...}, warnings:[...]}
     * asOf: ISO date (time-travel) — tests what was valid at a past moment;
     * omitted when empty (defaults to now).
     * observedFrom/observedTo: bi-temporal observation range — only records
     * observed (learned) within that range; empty values are omitted.
     */
    recall: (q, k, asOf, observedFrom, observedTo) =>
      request("GET", PATHS.recall, {
        params: { q, k, as_of: asOf, observed_from: observedFrom, observed_to: observedTo },
      }),
    getSettings: () => request("GET", PATHS.settings),
    /** body: {allow_direct, auto_capture, session_summary, vector, embed_backend, ollama_url, embed_model} */
    putSettings: (body) => request("PUT", PATHS.settings, { body }),
    getStats: () => request("GET", PATHS.stats),
    /** → [{ts,kind,title,detail,ref_id}] newest-first (Overview recent activity). */
    async getTimeline(limit = 8) {
      return asList(await request("GET", PATHS.timeline, { params: { limit } }));
    },
  };

  window.AkanaMemoryApi = api;
})();
