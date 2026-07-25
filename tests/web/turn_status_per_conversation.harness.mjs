/**
 * Turn-status strip — PER-CONVERSATION clock/phase + resume seeding. node-vm, no DOM lib.
 *
 * There is ONE strip above the composer but several conversations can be working at once.
 * Live bugs this locks:
 *   • switching back to a chat that was still working showed the OTHER chat's elapsed time
 *     and phase (the strip kept only the last-started turn's clock — reproduced in the real
 *     app: A working 3s, B started 0s ago → returning to A read "0:01"),
 *   • F5 restarted the clock at 0:00, so a turn that had run for minutes looked brand new
 *     (the resume endpoint now reports the real start via X-Akana-Turn-Started and
 *     begin(convId, startedAtMs) seeds from it),
 *   • a finished turn's clock must be dropped, so a later visit cannot restore a dead one.
 *
 * Run: node tests/web/turn_status_per_conversation.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const SRC = readFileSync(path.join(REPO, "web_ui/static/akana-turn-status.js"), "utf8");

let passed = 0;
const check = (label, fn) => { fn(); passed += 1; void label; };

// ── minimal DOM: only what the strip touches ────────────────────────────────
function makeEl(tag = "div") {
  return {
    tagName: String(tag).toUpperCase(),
    children: [],
    hidden: false,
    textContent: "",
    className: "",
    attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; },
    appendChild(c) { this.children.push(c); return c; },
    append(...cs) { cs.forEach((c) => this.children.push(c)); },
    insertBefore(c) { this.children.push(c); return c; },
    querySelector() { return null; },
  };
}

function boot() {
  const form = makeEl("form");
  const doc = {
    readyState: "complete",
    getElementById: (id) => (id === "chat-form" ? form : null),
    createElement: (t) => makeEl(t),
    addEventListener: () => {},
  };
  let now = 1_000_000;
  // The strip repaints on a 1s interval; capture that callback so advancing the fake
  // clock also repaints (otherwise the label keeps whatever it showed when it was set).
  let painter = null;
  const ctx = {
    window: {
      AkanaI18n: { t: (k) => k },
      setInterval: (fn) => { painter = fn; return 1; },
      clearInterval: () => { painter = null; },
    },
    document: doc,
    console,
    Date: { now: () => now },
  };
  ctx.window.window = ctx.window;
  ctx.window.document = doc;
  vm.runInNewContext(SRC, ctx);
  const TS = ctx.window.AkanaTurnStatus;
  return {
    TS,
    advance: (ms) => { now += ms; if (painter) painter(); },
    text: () => {
      const strip = form.children[0];
      const label = strip && strip.children[0];
      return { hidden: strip ? strip.hidden : null, text: label ? label.textContent : "" };
    },
  };
}

// ── two conversations working at once keep their OWN clocks ────────────────
{
  const r = boot();
  r.TS.begin("A");
  r.advance(180_000); // A has been working 3 minutes
  check("A's own clock ticks", () => assert.match(r.text().text, /3:00/));

  r.TS.begin("B"); // a second chat starts a turn NOW
  check("B starts fresh at 0:00", () => assert.match(r.text().text, /0:00/));

  r.advance(5_000);
  r.TS.resume("A"); // user switches back to A
  check("returning to A restores A's elapsed, not B's", () => {
    assert.match(r.text().text, /3:05/, "A must keep counting from its own start");
  });
  r.TS.resume("B");
  check("and B still has its own, much younger clock", () =>
    assert.match(r.text().text, /0:05/));
}

// ── the phase/tool label is per conversation too ────────────────────────────
{
  const r = boot();
  r.TS.begin("A");
  r.TS.setPhase("tool", "grep the repo");
  r.TS.begin("B");
  r.TS.setPhase("writing");
  check("B shows its own phase", () => assert.match(r.text().text, /ui\.turn_writing/));
  r.TS.resume("A");
  check("A shows ITS tool label again, not B's phase", () =>
    assert.match(r.text().text, /grep the repo/));
}

// ── F5: the clock is seeded from the turn's real start ──────────────────────
{
  const r = boot();
  r.TS.begin("A", 1_000_000 - 137_000); // server said: started 137s ago
  check("a resumed turn shows its REAL age, not 0:00", () =>
    assert.match(r.text().text, /2:17/));
}
{
  const r = boot();
  r.TS.begin("A", 1_000_000 + 60_000); // absurd (clock skew / future stamp)
  check("an impossible start stamp falls back to now", () =>
    assert.match(r.text().text, /0:00/));
  r.TS.begin("B", "not-a-number");
  check("a garbage stamp falls back to now", () => assert.match(r.text().text, /0:00/));
}

// ── a finished turn's clock is dropped ─────────────────────────────────────
{
  const r = boot();
  r.TS.begin("A");
  r.advance(120_000);
  r.TS.clear("A"); // the stream for A ended
  r.advance(1_000);
  r.TS.resume("A"); // opening A again must NOT restore the dead 2-minute clock
  check("a cleared conversation starts fresh", () => assert.match(r.text().text, /0:00/));
}

// ── end() hides the strip but keeps the clocks for a switch-back ────────────
{
  const r = boot();
  r.TS.begin("A");
  r.advance(30_000);
  r.TS.end();
  check("end hides the strip", () => assert.equal(r.text().hidden, true));
  r.advance(2_000);
  r.TS.resume("A");
  check("switching back restores the still-running turn's real elapsed", () => {
    assert.equal(r.text().hidden, false);
    assert.match(r.text().text, /0:32/);
  });
}

console.log(`turn_status_per_conversation.harness: ${passed} strip contracts PASSED ✓`);
if (typeof process !== "undefined" && process.exit) process.exit(0);
