/**
 * Blitz 4 — voice-e2e frontend contract test (node-vm, no real audio/WS device).
 *
 * Drives the REAL web_ui/static/akana-voice-live.js through a fake WebSocket so the
 * internal _onServerMessage handler (assigned to ws.onmessage in _connect) can be
 * exercised behaviourally — no need to expose it.
 *
 *  voice-e2e-1  SERVER 'interrupt' FRAME NEVER CLEARS THE CLIENT BARGE LATCH: after a
 *    client Stop (interrupt()) sets _interruptedUntilTurn, the server's own interrupt
 *    frame (a turn boundary) only flushed playback and left the latch set — so every
 *    audio byte of the NEXT reply was dropped (silent answer) until that turn's own
 *    turn_complete. The fix clears the latch in case "interrupt".
 *
 *  fe-be-contract-1  LIVE 'tool' FRAME HAS NO FE CONSUMER: both realtime bridges send
 *    {type:"tool",name} after a tool dispatch, but _onServerMessage had no "tool" case,
 *    so Live mode showed no tool chip (the classic SSE path does). The fix emits the same
 *    voice:tool bus event the aurora consumer already subscribes to.
 *
 * Run: node tests/web/blitz4_voice-e2e.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const read = (rel) => readFileSync(path.join(REPO, rel), "utf8");
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

let passed = 0;
function check(label, fn) {
  fn();
  passed += 1;
  void label;
}

const LIVE_SRC = read("web_ui/static/akana-voice-live.js");

// ── Fakes shared across the behavioural sections ──────────────────────────────
const busEmits = []; // [event, payload] captured from AkanaBus.emit
const playbackPosts = []; // everything posted to the playback worklet's port

let lastWs = null; // most recently constructed fake WebSocket

class FakeWebSocket {
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.binaryType = "";
    this.onopen = this.onmessage = this.onerror = this.onclose = null;
    this._closeListeners = [];
    lastWs = this;
    // onopen is assigned synchronously by _connect right after construction; fire it on
    // the next tick so the connect promise resolves and ws.onmessage (=_onServerMessage)
    // is live.
    setTimeout(() => {
      this.readyState = 1;
      if (typeof this.onopen === "function") this.onopen();
    }, 0);
  }
  addEventListener(ev, cb) {
    if (ev === "close") this._closeListeners.push(cb);
  }
  send() {}
  close() {
    this.readyState = 3;
  }
}

class FakeWorkletNode {
  constructor(_ctx, name) {
    this._name = name;
    this.port = {
      onmessage: null,
      postMessage: (data) => {
        if (name === "akana-pcm-playback") playbackPosts.push(data);
      },
      close() {},
    };
  }
  connect() {}
  disconnect() {}
}

class FakeAudioContext {
  constructor() {
    this.sampleRate = 48000;
    this.state = "running";
    this.destination = {};
    this.audioWorklet = { addModule: async () => {} };
  }
  createGain() {
    return { gain: { value: 0 }, connect() {}, disconnect() {} };
  }
  createMediaStreamSource() {
    return { connect() {}, disconnect() {} };
  }
  resume() {
    return Promise.resolve();
  }
  close() {
    return Promise.resolve();
  }
}

function makeCtx() {
  const ctx = {
    console,
    // Share Node's ArrayBuffer so `ev.data instanceof ArrayBuffer` inside the vm matches
    // the buffers we construct out here (cross-realm instanceof would otherwise be false).
    ArrayBuffer,
    URL,
    URLSearchParams,
    setTimeout,
    clearTimeout,
    setInterval: () => 0,
    clearInterval: () => {},
    WebSocket: FakeWebSocket,
    AudioContext: FakeAudioContext,
    AudioWorkletNode: FakeWorkletNode,
    navigator: {
      mediaDevices: {
        getUserMedia: async () => ({ getTracks: () => [{ stop() {} }] }),
      },
    },
    location: { protocol: "http:", host: "localhost:8766" },
    AkanaBus: {
      emit: (event, payload) => busEmits.push([event, payload]),
      on: () => () => {},
    },
  };
  ctx.window = ctx;
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(LIVE_SRC, ctx);
  return ctx;
}

// Drive one server frame through the live handler.
function feed(ws, data) {
  ws.onmessage({ data });
}

// ══════════════════════════════════════════════════════════════════════════════
// voice-e2e-1 — the server interrupt frame clears the client barge latch
// ══════════════════════════════════════════════════════════════════════════════
{
  const ctx = makeCtx();
  const L = ctx.window.AkanaVoiceLive;
  assert.ok(L, "AkanaVoiceLive failed to load");

  const states = [];
  const started = await L.start({
    conversationId: "c",
    config: { live: { available: true, enabled: true, provider_is_gemini: true } },
    onState: (s) => states.push(s),
  });
  assert.equal(started, true, "live session should start against the fake WS");
  const ws = lastWs;
  assert.ok(ws && typeof ws.onmessage === "function", "_onServerMessage must be wired to ws.onmessage");

  // Baseline: with no barge latch, an audio frame plays (sanity that the rig enqueues).
  playbackPosts.length = 0;
  feed(ws, new ArrayBuffer(8));
  assert.ok(
    playbackPosts.some((p) => p instanceof ArrayBuffer),
    "baseline: assistant audio must reach the playback worklet",
  );

  // Client Stop → latch set; then the server's interrupt frame (the barged turn boundary);
  // then the NEXT turn's first audio frame.
  L.interrupt(); // sets _interruptedUntilTurn = true
  feed(ws, JSON.stringify({ type: "interrupt" })); // server turn boundary
  playbackPosts.length = 0;
  states.length = 0;
  feed(ws, new ArrayBuffer(8)); // NEXT turn's audio

  check("voice-e2e-1: audio AFTER the server interrupt frame is played (latch cleared)", () => {
    // Before the fix the latch survived the interrupt frame → this ArrayBuffer was dropped
    // at `if (_interruptedUntilTurn) return;` and the whole next reply was silent.
    assert.ok(
      playbackPosts.some((p) => p instanceof ArrayBuffer),
      "the next turn's audio must reach the playback worklet after a server interrupt",
    );
  });
  check("voice-e2e-1: the orb flips to SPEAKING on the post-interrupt audio", () => {
    assert.ok(states.includes(L.STATES.SPEAKING), `expected SPEAKING, got ${JSON.stringify(states)}`);
  });

  // Regression guard: a NATURAL barge (server interrupt with no prior client Stop) must not
  // wrongly resume the OLD turn's tail — but since the bridge stops forwarding before the
  // interrupt frame, clearing the latch is safe. Latch was never set here, so audio plays.
  L.stop();
}

// ══════════════════════════════════════════════════════════════════════════════
// fe-be-contract-1 — the live 'tool' frame emits voice:tool on the bus
// ══════════════════════════════════════════════════════════════════════════════
{
  busEmits.length = 0;
  const ctx = makeCtx();
  const L = ctx.window.AkanaVoiceLive;
  const started = await L.start({
    conversationId: "c",
    config: { live: { available: true, enabled: true, provider_is_gemini: true } },
  });
  assert.equal(started, true);
  const ws = lastWs;

  feed(ws, JSON.stringify({ type: "tool", name: "memory_remember" }));
  await wait(1);

  check("fe-be-contract-1: a 'tool' frame emits voice:tool with {call:{name}}", () => {
    const toolEmits = busEmits.filter(([e]) => e === "voice:tool");
    assert.equal(toolEmits.length, 1, `exactly one voice:tool emit expected, got ${busEmits.length} bus emits`);
    const payload = toolEmits[0][1];
    assert.ok(payload && payload.call, "payload must carry a call object (aurora upsertVoiceTool reads call.name)");
    assert.equal(payload.call.name, "memory_remember", "the tool name must be forwarded");
  });
  L.stop();
}

// ══════════════════════════════════════════════════════════════════════════════
// Source-contract belt-and-braces (the handler is an internal singleton closure)
// ══════════════════════════════════════════════════════════════════════════════
{
  check("voice-e2e-1 (source): case \"interrupt\" clears the barge latch", () => {
    const branch = LIVE_SRC.match(/case "interrupt":[\s\S]*?break;/)[0];
    assert.match(branch, /_interruptedUntilTurn = false/, "interrupt frame must clear the latch");
  });
  check("fe-be-contract-1 (source): case \"tool\" emits voice:tool", () => {
    const branch = LIVE_SRC.match(/case "tool":[\s\S]*?break;/)[0];
    assert.match(branch, /voice:tool/, "tool frame must emit the voice:tool bus event");
  });
}

console.log(`blitz4 voice-e2e contract test: ${passed} contracts PASSED, OK`);
process.exit(0);
