/**
 * hunt5 misc — conversation-switch LLM restore is generation-guarded (akana-settings.js).
 *
 * switchChatConversation fires `void restoreConversationLlm(convId)` un-awaited on EVERY
 * switch (optimistic navigation). That call MUTATES GLOBAL state: it PUTs the conversation's
 * provider/model onto /api/v1/system/llm-settings and then repaints the header pill +
 * thinking-provider (which is what the composer's effort vocabulary is keyed on). With two
 * rapid switches A→B the SLOW restore(A) can resolve after restore(B) finished and clobber
 * the global back to A's provider while B is on screen: wrong pill, wrong effort vocabulary
 * on B's next send, and a brand-new chat inherits A's model.
 *
 * Contract asserted here: whoever started LAST wins — a superseded restore must not PUT and
 * must not repaint the pill/thinking provider.
 *
 * Run: node tests/web/hunt5_misc.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { makeI18nStub } from "./_i18n_stub.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const readStatic = (rel) => readFileSync(path.join(REPO, "web_ui/static", rel), "utf8");
const tick = () => new Promise((r) => setImmediate(r));

const results = [];
async function section(id, fn) {
  try {
    await fn();
    results.push({ id, ok: true });
    console.log(`  [PASS] ${id}`);
  } catch (e) {
    results.push({ id, ok: false, err: e });
    console.log(`  [FAIL] ${id}: ${e && e.message}`);
  }
}

/** Minimal DOM: every element lookup is null (the module is null-safe by design). */
function makeCtx({ fetchImpl, chat }) {
  const busEvents = [];
  const thinkingProviders = [];
  // /ws/events socket double: the module only ever touches readyState + the four handlers,
  // so the test can decide exactly when a connection opens and when it drops.
  const sockets = [];
  class FakeWebSocket {
    constructor(url) {
      this.url = String(url);
      this.readyState = FakeWebSocket.CONNECTING;
      sockets.push(this);
    }
    close() { this.readyState = FakeWebSocket.CLOSED; }
    /** the server accepted the handshake */
    open() { this.readyState = FakeWebSocket.OPEN; this.onopen?.(); }
    /** laptop sleep / Wi-Fi drop / server restart */
    drop() { this.readyState = FakeWebSocket.CLOSED; this.onclose?.(); }
  }
  FakeWebSocket.CONNECTING = 0;
  FakeWebSocket.OPEN = 1;
  FakeWebSocket.CLOSING = 2;
  FakeWebSocket.CLOSED = 3;
  const win = {
    AkanaCore: {
      LS_BASE: "akana.baseUrl",
      LS_TOKEN: "akana.token",
      showToast: () => {},
      escapeHtml: (s) => String(s),
      baseUrl: () => "http://x",
      authHeaders: () => ({}),
      parseApiError: () => "",
      configure: () => {},
    },
    AkanaBus: { emit: (e, p) => busEvents.push({ e, p }) },
    AkanaChat: {
      conversationIdForMemory: () => "B",
      setThinkingProvider: (p) => thinkingProviders.push(String(p)),
      ...chat,
    },
    AkanaI18n: makeI18nStub(),
    matchMedia: () => ({ matches: false }),
  };
  const doc = {
    body: { classList: { contains: () => false } },
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {},
    createElement: () => ({ dataset: {}, style: {}, classList: { add() {}, remove() {} } }),
    documentElement: { dataset: {} },
  };
  win.document = doc;
  const ctx = {
    window: win,
    document: doc,
    navigator: {},
    console,
    fetch: fetchImpl,
    localStorage: {
      getItem: () => null,
      setItem: () => {},
      removeItem: () => {},
    },
    setTimeout,
    clearTimeout,
    URL,
    WebSocket: FakeWebSocket,
  };
  vm.runInNewContext(readStatic("akana-settings.js"), ctx);
  return { ctx, win, busEvents, thinkingProviders, sockets };
}

const json = (body) => ({ ok: true, status: 200, json: async () => body });

// ═══ hunt5-misc-1: a superseded conversation-LLM restore must not clobber the global ═══
await section("singleton-ui-across-chats-3 stale restore must not PUT/repaint", async () => {
  const puts = [];
  // The server's live truth: last PUT wins (this is what /system/status then reports).
  let activeProvider = "cursor";
  let releaseA;
  const gateA = new Promise((r) => (releaseA = r));

  const fetchImpl = async (url, opts) => {
    const u = String(url);
    const method = (opts && opts.method) || "GET";
    if (u.includes("/conversations/A/llm-settings")) {
      await gateA; // A's GET is slow (server busy streaming other chats)
      return json({ settings: { provider: "gemini", gemini_model: "gemini-pro" } });
    }
    if (u.includes("/conversations/B/llm-settings")) {
      return json({ settings: { provider: "codex", codex_model: "gpt-5-codex" } });
    }
    if (u.includes("/api/v1/system/llm-settings") && method === "PUT") {
      const patch = JSON.parse(opts.body).settings;
      puts.push(patch);
      activeProvider = patch.provider;
      return json({ settings: patch, active_provider: patch.provider, providers: [] });
    }
    if (u.includes("/api/v1/system/status")) {
      return json({ active_provider: activeProvider, active_codex_model_tag: "gpt-5-codex" });
    }
    return json({});
  };

  const { win, thinkingProviders } = makeCtx({ fetchImpl });
  const settings = win.AkanaSettings;
  assert.equal(typeof settings.restoreConversationLlm, "function", "restoreConversationLlm must be exported");

  const pA = settings.restoreConversationLlm("A"); // click A — its GET stalls
  await tick();
  const pB = settings.restoreConversationLlm("B"); // ~200ms later the user clicks B
  await pB;
  assert.deepEqual(
    puts.map((p) => p.provider),
    ["codex"],
    "the newest switch (B) must be the one that wrote the global settings",
  );

  releaseA();
  await pA;
  await tick();

  assert.deepEqual(
    puts.map((p) => p.provider),
    ["codex"],
    "a superseded restore (A) must NOT PUT its provider onto the global runtime settings",
  );
  assert.equal(activeProvider, "codex", "global runtime provider must still be the displayed chat's");
  assert.ok(
    thinkingProviders.length === 0 || thinkingProviders[thinkingProviders.length - 1] === "codex",
    `effort vocabulary must stay on the displayed chat's provider (got ${JSON.stringify(thinkingProviders)})`,
  );
});

// ═══ hunt5-misc-2: the normal single restore still applies (no over-guarding) ═══
await section("singleton-ui-across-chats-3 the winning restore still applies", async () => {
  const puts = [];
  let activeProvider = "cursor";
  const fetchImpl = async (url, opts) => {
    const u = String(url);
    const method = (opts && opts.method) || "GET";
    if (u.includes("/conversations/B/llm-settings")) {
      return json({ settings: { provider: "codex", codex_model: "gpt-5-codex" } });
    }
    if (u.includes("/api/v1/system/llm-settings") && method === "PUT") {
      const patch = JSON.parse(opts.body).settings;
      puts.push(patch);
      activeProvider = patch.provider;
      return json({ settings: patch, active_provider: patch.provider, providers: [] });
    }
    if (u.includes("/api/v1/system/status")) {
      return json({ active_provider: activeProvider, active_codex_model_tag: "gpt-5-codex" });
    }
    return json({});
  };
  const { win, thinkingProviders, busEvents } = makeCtx({ fetchImpl });
  await win.AkanaSettings.restoreConversationLlm("B");
  await tick();
  assert.deepEqual(puts.map((p) => p.provider), ["codex"], "a lone restore must still apply the conv's provider");
  assert.equal(thinkingProviders[thinkingProviders.length - 1], "codex", "pill/effort vocabulary must follow it");
  assert.ok(
    busEvents.some((e) => e.e === "llm:provider:changed" && e.p && e.p.provider === "codex"),
    "the provider-changed bus event must still fire for the winning restore",
  );
});

// ═══ hunt5-misc-3 (R5): a WS RE-connect must reconcile what the outage swallowed ═══
// /ws/events has NO missed-event replay. Across a laptop sleep / Wi-Fi drop / server
// restart the turn_active and turn_completed frames are simply gone: a job that FINISHED
// during the gap leaves the strip ticking and the sidebar badge lit forever, and one that
// STARTED during the gap never lights anything — until an F5 or a navigation. Only the
// re-open of the socket knows a gap happened, so the resync belongs there.
function wsRig() {
  const seen = { reconciled: [], archiveLoads: 0 };
  const rig = makeCtx({
    fetchImpl: async () => json({}),
    chat: {
      conversationIdForMemory: () => "displayed-conv",
      reconcileBgActiveTurn: (id) => { seen.reconciled.push(id); return Promise.resolve(true); },
      loadChatArchiveList: () => { seen.archiveLoads += 1; return Promise.resolve(); },
    },
  });
  return { ...rig, seen };
}

await section("ws-reconnect-resync the FIRST connect does not re-probe (boot already did)", async () => {
  const rig = wsRig();
  rig.win.AkanaSettings.connectWs(true);
  assert.equal(rig.sockets.length, 1, "precondition: one socket was created");
  rig.sockets[0].open();
  await tick();
  assert.deepEqual(rig.seen.reconciled, [], "the initial handshake must not duplicate the boot reconcile");
  assert.equal(rig.seen.archiveLoads, 0, "…nor re-sweep the sidebar the boot list load just built");
});

await section("ws-reconnect-resync a RE-connect reconciles the displayed chat + the sidebar", async () => {
  const rig = wsRig();
  rig.win.AkanaSettings.connectWs(true);
  rig.sockets[0].open();
  await tick();
  // The outage: every broadcast between here and the next open() is lost for good.
  rig.sockets[0].drop();
  rig.win.AkanaSettings.connectWs(true);
  assert.equal(rig.sockets.length, 2, "precondition: the reconnect opened a NEW socket");
  rig.sockets[1].open();
  await tick();
  assert.deepEqual(
    rig.seen.reconciled,
    ["displayed-conv"],
    "a re-connect must re-read the displayed conversation's background-work truth from the server",
  );
  assert.equal(
    rig.seen.archiveLoads,
    1,
    "…and force one sidebar activity resync (rows whose turn_completed was swallowed still read busy)",
  );
});

await section("ws-reconnect-resync an UNBOUND displayed chat resyncs the sidebar only", async () => {
  const seen = { reconciled: [], archiveLoads: 0 };
  const rig = makeCtx({
    fetchImpl: async () => json({}),
    chat: {
      conversationIdForMemory: () => "", // a brand-new chat has nothing to reconcile
      reconcileBgActiveTurn: (id) => { seen.reconciled.push(id); return Promise.resolve(true); },
      loadChatArchiveList: () => { seen.archiveLoads += 1; return Promise.resolve(); },
    },
  });
  rig.win.AkanaSettings.connectWs(true);
  rig.sockets[0].open();
  rig.sockets[0].drop();
  rig.win.AkanaSettings.connectWs(true);
  rig.sockets[1].open();
  await tick();
  assert.deepEqual(seen.reconciled, [], "reconcileBgActiveTurn('') is a no-op — do not spend a probe on it");
  assert.equal(seen.archiveLoads, 1, "the sidebar still has to be resynced");
});

const failed = results.filter((r) => !r.ok);
console.log(`hunt5 misc harness: ${results.length - failed.length}/${results.length} sections passed`);
if (failed.length) {
  for (const f of failed) console.error(f.err && f.err.stack ? f.err.stack : f.err);
  process.exit(1);
}
process.exit(0);
