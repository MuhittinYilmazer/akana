/**
 * Memory Studio ↔ /api/v1/memory contract conformance test (runs backend-free).
 * - does the akana-memory-api.js PATHS constant exactly match the contract paths?
 * - is there a leftover old /api/v1/memory call or direct fetch in Studio?
 * - is memory.html free of dead panel/script references, is the load order correct?
 * - is Studio's public API (the one app.js expects) in place?
 * Run: node tests/web/memory_studio_contract.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const API_PATH = path.join(REPO, "web_ui/static/akana-memory-api.js");
const RENDER_PATH = path.join(REPO, "web_ui/static/akana-memory-render.js");
const STUDIO_PATH = path.join(REPO, "web_ui/static/akana-memory-studio.js");
const HTML_PATH = path.join(REPO, "web_ui/memory.html");

const apiSrc = readFileSync(API_PATH, "utf8");
const renderSrc = readFileSync(RENDER_PATH, "utf8");
const studioSrc = readFileSync(STUDIO_PATH, "utf8");
const htmlSrc = readFileSync(HTML_PATH, "utf8");

// ── 1. Contract path list (the parallel backend agent's contract) ──────────
const CONTRACT_PATHS = [
  "/api/v1/memory/staging",
  "/api/v1/memory/staging/{id}/approve",
  "/api/v1/memory/staging/{id}/reject",
  "/api/v1/memory/facts",
  "/api/v1/memory/facts/{id}",
  "/api/v1/memory/recall",
  "/api/v1/memory/settings",
  "/api/v1/memory/stats",
  "/api/v1/memory/timeline",
].sort();

const apiCtx = { window: {} };
vm.runInNewContext(apiSrc, apiCtx);
const memApi = apiCtx.window.AkanaMemoryApi;
assert.ok(memApi, "AkanaMemoryApi failed to load");
assert.deepEqual(
  Object.values(memApi.PATHS).sort(),
  CONTRACT_PATHS,
  "PATHS constant does not exactly match the contract paths",
);

// Is the API client's surface complete?
for (const fn of [
  "listStaging", "approveStaging", "rejectStaging",
  "listFacts", "createFact", "updateFact", "deleteFact",
  "recall", "getSettings", "putSettings", "getStats", "getTimeline",
]) {
  assert.equal(typeof memApi[fn], "function", `AkanaMemoryApi.${fn} missing`);
}

// ── 2. Check for old v1 call / stray path / direct fetch ──────────────
const findApiPaths = (src) => src.match(/\/api\/v[0-9]+\/[a-z0-9/_{}-]+/gi) || [];

for (const [name, src] of [["studio", studioSrc], ["render", renderSrc]]) {
  assert.equal(
    findApiPaths(src).length, 0,
    `${name} must not contain an API path literal (they all live in akana-memory-api.js): ${findApiPaths(src)}`,
  );
  assert.ok(!src.includes("fetch("), `${name} must not call fetch directly — use AkanaMemoryApi`);
}
for (const p of findApiPaths(apiSrc)) {
  assert.ok(CONTRACT_PATHS.includes(p), `path outside the contract: ${p}`);
}
// Memory is now on the same version as the rest of the API (/api/v1/memory) — a
// single unified memory; the old anomaly /api/v2 must be entirely gone (is the scrub complete).
assert.ok(!apiSrc.includes("/api/v2"), "old /api/v2 memory path still present in the API client");
assert.ok(!htmlSrc.includes("/api/v2"), "old /api/v2 memory path still present in memory.html");

// ── 3. Dead panel/script cleanup + load order ─────────────────────────
// NOTE: "memory-timeline-list" is no longer dead — it's the live recent-activity
// list on the Overview (/api/v1/memory/timeline). The old standalone
// memory_timeline.js file is still forbidden; only the element id was revived.
for (const dead of [
  "memory_graph.js", "memory_timeline.js",
  "memory-compile-preview", "memory-graph-list",
  "memory-conv-select", "memory-sources-list",
  "memory-staging-batch", "memory-reset-all",
]) {
  assert.ok(!htmlSrc.includes(dead), `dead reference still present in memory.html: ${dead}`);
}
const apiIdx = htmlSrc.indexOf("akana-memory-api.js");
const renderIdx = htmlSrc.indexOf("akana-memory-render.js");
const studioIdx = htmlSrc.indexOf("akana-memory-studio.js");
assert.ok(apiIdx > -1 && renderIdx > -1 && studioIdx > -1, "memory.html must load all three memory scripts");
assert.ok(apiIdx < studioIdx && renderIdx < studioIdx, "api + render must load before studio");
assert.ok(htmlSrc.includes("akana-memory-studio.css"), "memory.html must load the new Studio css");

// The render module must load in the VM and its surface must be complete (a document stub suffices).
const renderCtx = { window: {}, document: { createElement: () => ({}) } };
vm.runInNewContext(renderSrc, renderCtx);
for (const fn of [
  "setListState", "renderInboxItem", "renderFactCard",
  "buildFactEditor", "renderRecallItem", "renderRecallTrace", "sourceBadge",
]) {
  assert.equal(typeof renderCtx.window.AkanaMemoryRender?.[fn], "function", `AkanaMemoryRender.${fn} missing`);
}

// Bi-temporal recall contract: the API client must send observed_from/observed_to
// alongside as_of, and the Studio Recall view must have an observed-range control.
for (const param of ["as_of", "observed_from", "observed_to"]) {
  assert.ok(apiSrc.includes(param), `API client must send the '${param}' parameter on recall`);
}
for (const id of ["memory-recall-observed-from", "memory-recall-observed-to", "memory-recall-observed-clear"]) {
  assert.ok(htmlSrc.includes(`id="${id}"`), `observed-range element missing in memory.html: #${id}`);
  assert.ok(studioSrc.includes(id), `studio must use the observed-range element: #${id}`);
}

// Provenance contract: the source badge must mark the origin via data-origin (css
// token color) and the click-to-verify popover ("Where did this come from?") must be in render.
assert.ok(renderSrc.includes('"memory.provenance_heading"'), "provenance popover heading (i18n key) missing in render");
assert.ok(renderSrc.includes("memory-chip-source"), "source badge class missing in render");
// Popover close: outside-click (onDocClick) AND Esc (onDocKey) — both must be
// detached from the document on close (no listener leak).
assert.ok(renderSrc.includes("onDocClick"), "popover outside-click listener missing");
assert.ok(renderSrc.includes("onDocKey"), "popover Esc listener missing");
assert.ok(
  renderSrc.includes('document.removeEventListener("keydown", onDocKey, true)'),
  "the Esc listener must be detached when the popover closes",
);
for (const origin of ["user_statement", "inferred", "tool_output", "synthesis", "legacy"]) {
  assert.ok(renderSrc.includes(origin), `render SOURCE_ORIGIN_META must recognize the '${origin}' origin`);
}

// Is every element id Studio uses present in the HTML?
const usedIds = [...studioSrc.matchAll(/\$\("([a-z0-9-]+)"\)/g)].map((m) => m[1]);
// Chat-page (index.html) elements — Studio's bridge functions ($ them there, not on the
// memory page): the "capture to memory" button and the Inbox pending-count nav badge.
const CHAT_PAGE_IDS = new Set(["btn-capture-memory", "memory-nav-badge"]);
for (const id of new Set(usedIds)) {
  if (CHAT_PAGE_IDS.has(id)) continue;
  assert.ok(htmlSrc.includes(`id="${id}"`), `missing element in memory.html: #${id}`);
}

// ── 4. Studio must also load error-free on the chat page + public API ──────────
const studioCtx = {
  window: {},
  sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  document: {
    body: { classList: { contains: () => false } },
    getElementById: () => null,
    querySelectorAll: () => [],
  },
};
studioCtx.window.document = studioCtx.document;
vm.runInNewContext(studioSrc, studioCtx);
const studio = studioCtx.window.AkanaMemoryStudio;
assert.ok(studio, "AkanaMemoryStudio failed to load");
for (const fn of [
  "init", "loadMemoryPane", "loadMemoryConversations",
  "openCompilePreviewFromChat", "openMemoryStudio",
  "applyMemoryStudioRouteFromUrl", "captureChatMessageToMemory",
]) {
  assert.equal(typeof studio[fn], "function", `AkanaMemoryStudio.${fn} missing (app.js/akana-settings.js calls this)`);
}
studio.init({}); // init must not blow up in chat-page mode

// ── 5. Advanced views (Map/Insight) REMOVED — no graph + insight modules.

console.log("memory_studio_contract.harness: ok");
