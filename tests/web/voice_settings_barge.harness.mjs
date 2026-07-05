/**
 * Barge threshold settings control contract — backend-free, node-vm.
 *
 * User request: adjust barge sensitivity from Settings (instead of editing
 * localStorage from the console). Control: the conv-barge-rms slider →
 * localStorage "akana.bargeRms" (akana-voice.js _threshold() reads this).
 *
 * Tests: (1) init loads the current value, (2) change → clamp + localStorage write,
 * (3) out-of-range value is clamped, (4) slider disabled when barge-in is off.
 *
 * Run: node tests/web/voice_settings_barge.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const SRC = readFileSync(path.join(REPO, "web_ui/static/akana-voice-settings.js"), "utf8");

// ── DOM element mock: value/textContent/disabled/checked + handler capture ───
function makeEl(value = "") {
  const handlers = {};
  return {
    value,
    textContent: "",
    disabled: false,
    checked: false,
    innerHTML: "",
    hidden: false,
    style: {},
    addEventListener(ev, cb) {
      (handlers[ev] ||= []).push(cb);
    },
    // Earcon-volume slider is injected only when #conv-earcon-vol is absent AND an
    // anchor label is found; returning null here skips that DOM injection cleanly
    // (this harness only exercises the barge slider contract).
    closest() {
      return null;
    },
    appendChild() {},
    fire(ev) {
      (handlers[ev] || []).forEach((cb) => cb());
    },
  };
}

const els = {
  "conv-barge-rms": makeEl("0.05"),
  "conv-barge-rms-out": makeEl(),
  "conv-barge-in": makeEl(),
  "conv-wake-enters": makeEl(),
  "conv-earcons": makeEl(),
  "wake-source": makeEl("model"),
  "conv-silence-ms": makeEl("1300"),
  "conv-silence-ms-out": makeEl(),
};

const backing = {};
const localStorage = {
  getItem: (k) => (k in backing ? backing[k] : null),
  setItem: (k, v) => {
    backing[k] = String(v);
  },
  removeItem: (k) => {
    delete backing[k];
  },
};

const ctx = {
  console,
  localStorage,
  navigator: {},
  setInterval: () => 0,
  setTimeout: (cb) => {
    if (typeof cb === "function") cb();
    return 0;
  },
  clearTimeout: () => {},
  document: {
    getElementById: (id) => els[id] || null,
    createElement: () => makeEl(),
  },
  fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve({ tts: {}, wake: {}, stt: {} }) }),
};
ctx.window = ctx;
ctx.window.AkanaCore = { baseUrl: () => "", authHeaders: () => ({}) };
vm.runInNewContext(SRC, ctx);

const bridge = {
  setTtsEnabled() {},
  getTtsEnabled: () => false,
  ttsToggle: null,
  ttsPlayer: { queue: [], playing: false },
  hooks: { isChatPage: false },
  speechLang: () => "tr",
  loadVoicePreferences: () => Promise.resolve(),
  saveVoicePreferences: () => Promise.resolve(),
  setWakeListening: () => Promise.resolve(true),
  syncWakeButtonUi() {},
  stopAudioGraph() {},
  voice: {},
};

let passed = 0;
function check(label, fn) {
  fn();
  passed += 1;
  void label;
}

const settings = ctx.window.AkanaVoiceSettings.createSettings(bridge);
await settings.initVoiceUx(); // initConversationModeUx runs synchronously → sliders get wired

const inp = els["conv-barge-rms"];
const out = els["conv-barge-rms-out"];

check("init: default 0.05 is loaded + output reflects it", () => {
  assert.equal(inp.value, "0.05");
  assert.equal(out.textContent, "0.050");
});

check("barge-in default OFF (opt-in — don't self-interrupt on the speaker)", () => {
  assert.equal(els["conv-barge-in"].checked, false);
});

check("change: a valid value is written to localStorage", () => {
  inp.value = "0.04";
  inp.fire("change");
  assert.equal(localStorage.getItem("akana.bargeRms"), "0.04");
  assert.equal(out.textContent, "0.040");
});

check("change: above the upper bound it is clamped (0.5 → 0.10)", () => {
  inp.value = "0.5";
  inp.fire("change");
  assert.equal(localStorage.getItem("akana.bargeRms"), "0.1");
});

check("change: below the lower bound it is clamped (0.001 → 0.015)", () => {
  inp.value = "0.001";
  inp.fire("change");
  assert.equal(localStorage.getItem("akana.bargeRms"), "0.015");
});

check("when barge-in is off the threshold slider is disabled (meaningless)", () => {
  const cb = els["conv-barge-in"];
  cb.checked = false;
  cb.fire("change");
  assert.equal(inp.disabled, true);
  cb.checked = true;
  cb.fire("change");
  assert.equal(inp.disabled, false);
});

// ── Barge-in lifecycle race fixes (akana-voice.js) — source-contracts ────────────────
// Reported bugs: (B1) after a barge/Stop the re-arm SR came up a silent zombie because it raced
// the barge AEC mic's ASYNC release; (B2) enabling barge-in mid-reply was dropped; (V6) a
// concurrent OFF→ON re-opened the mic via a stale in-flight start().
{
  const VSRC = readFileSync(path.join(REPO, "web_ui/static/akana-voice.js"), "utf8");

  check("B1: bargeDetector.stop() returns an awaitable AudioContext-close promise", () => {
    assert.match(VSRC, /this\._closing = Promise\.resolve\(this\.audioCtx\.close\(\)\)/, "stop tracks the close promise");
    assert.match(VSRC, /return this\._closing \|\| Promise\.resolve\(\);/, "stop returns it");
  });

  check("B1: onConversationBargeIn defers the SR re-arm until the barge mic teardown settles (bounded)", () => {
    assert.match(
      VSRC,
      /cancelPendingBargeStop\(\);[\s\S]{0,160}?bargeDetector\.stop\(\)/,
      "cancels the debounced stop + tears the detector down",
    );
    assert.match(
      VSRC,
      /Promise\.race\(\[bargeTeardown[\s\S]{0,260}?startConversationCapture\("barge"\)/,
      "re-arm is deferred behind the teardown with a race cap",
    );
  });

  check("V6: bargeDetector.start() bails when superseded by a concurrent stop (start token)", () => {
    assert.match(VSRC, /const myToken = this\._startToken/, "start snapshots the supersession token");
    assert.match(VSRC, /myToken !== this\._startToken/, "post-getUserMedia gate rejects a stale start");
    assert.match(VSRC, /this\._startToken = \(this\._startToken \|\| 0\) \+ 1;/, "stop bumps the token");
  });

  check("B2: applyBargeInEnabled opens the detector for the whole in-flight reply (+mute guard)", () => {
    assert.match(
      VSRC,
      /voice\.conversationMode && \(ttsPlayer\.playing \|\| voice\.ttsStreamOpen\) && !voice\.micMuted/,
      "guard uses the in-flight latch + skips when muted",
    );
  });
}

console.log(`voice_settings_barge.harness: ${passed} contracts PASSED ✓`);
