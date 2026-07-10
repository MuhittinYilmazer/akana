/**
 * Single live turn status — strip above the composer (phase + elapsed time).
 * Driven by transport events via setPhase; elapsed time ticks via a local 1 s timer.
 */
(() => {
  "use strict";

  const _t = (k) => window.AkanaI18n?.t(k) ?? k;

  function phaseLabel(key) {
    const map = {
      preparing: "ui.turn_preparing",
      connecting: "ui.turn_connecting",
      thinking:   "ui.turn_thinking",
      writing:    "ui.turn_writing",
    };
    return _t(map[key] || "ui.turn_thinking");
  }

  let strip = null;
  let stripLabel = null;
  let active = false;
  let startedAt = 0;
  let phase = "preparing";
  let toolLabel = "";
  let timer = null;
  // The conversation this retained clock/phase belongs to. The strip is a global
  // singleton but two conversations can stream concurrently; recording the id lets
  // resume() refuse to attribute another conversation's elapsed/phase to the one now
  // displayed (see resume()). null = unbound (new-chat before adoption / no turn).
  let turnConvId = null;

  function formatElapsed(ms) {
    const totalSec = Math.max(0, Math.floor(ms / 1000));
    const mins = Math.floor(totalSec / 60);
    const secs = totalSec % 60;
    if (mins > 0) return `${mins}:${String(secs).padStart(2, "0")}`;
    return `0:${String(secs).padStart(2, "0")}`;
  }

  function buildText() {
    const dur = formatElapsed(Date.now() - startedAt);
    if (phase === "tool" && toolLabel) {
      const short = toolLabel.length > 48 ? `${toolLabel.slice(0, 48)}…` : toolLabel;
      return `${short} · ${dur}`;
    }
    const label = phaseLabel(phase);
    return `${label} · ${dur}`;
  }

  function paint() {
    if (!strip || !stripLabel || !active) return;
    const text = buildText();
    if (stripLabel.textContent !== text) stripLabel.textContent = text;
    strip.hidden = false;
  }

  function mount() {
    const form = document.getElementById("chat-form");
    if (!form || strip) return;
    const inner = form.querySelector(".composer-inner");
    strip = document.createElement("div");
    strip.className = "akana-flow-strip";
    strip.hidden = true;
    strip.setAttribute("role", "status");
    strip.setAttribute("aria-live", "polite");
    stripLabel = document.createElement("span");
    stripLabel.className = "jfs-label";
    const stripDots = document.createElement("span");
    stripDots.className = "jfs-dots";
    stripDots.setAttribute("aria-hidden", "true");
    for (let i = 0; i < 3; i++) stripDots.appendChild(document.createElement("i"));
    strip.append(stripLabel, stripDots);
    form.insertBefore(strip, inner || null);
  }

  function begin(convId) {
    mount();
    active = true;
    startedAt = Date.now();
    phase = "preparing";
    toolLabel = "";
    turnConvId = convId || null;
    paint();
    if (timer == null) timer = window.setInterval(paint, 1000);
  }

  // Re-attach the strip to an ALREADY-RUNNING turn (conversation switch-back) WITHOUT
  // restarting the clock or reverting the phase — begin() would reset startedAt to now
  // ("Preparing · 0:00") and lose the true elapsed of the in-flight turn.
  // CONV-SCOPED: the retained clock/phase belongs to whatever conversation called begin()
  // LAST. With two concurrent streams (A then B) the snapshot is B's; resuming A must NOT
  // show B's elapsed/tool label. When the requested id does not match the retained one,
  // fall back to begin() semantics (fresh clock, generic phase). Also start fresh if no
  // turn time is retained. A null id (legacy caller / pre-adoption) skips the check.
  function resume(convId) {
    mount();
    active = true;
    const wantId = convId || null;
    const mismatch = wantId !== null && turnConvId !== null && wantId !== turnConvId;
    if (mismatch || !startedAt) {
      startedAt = Date.now();
      phase = "preparing";
      toolLabel = "";
      turnConvId = wantId;
    }
    paint();
    if (timer == null) timer = window.setInterval(paint, 1000);
  }

  function end() {
    active = false;
    if (timer != null) {
      window.clearInterval(timer);
      timer = null;
    }
    if (strip) {
      strip.hidden = true;
      if (stripLabel) stripLabel.textContent = "";
    }
    // startedAt/phase/toolLabel/turnConvId are RETAINED (not reset) so a later resume() —
    // switching BACK to a conversation whose turn is still running — can restore the real
    // elapsed + phase (and verify the id matches). The next begin() overwrites them.
  }

  function setPhase(next, detail) {
    if (!active) return;
    phase = next;
    if (next === "tool") toolLabel = detail ? String(detail) : _t("ui.turn_tool_default");
    else toolLabel = "";
    paint();
  }

  function isActive() {
    return active;
  }

  window.AkanaTurnStatus = { mount, begin, resume, end, setPhase, isActive };

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", mount, { once: true });
    } else {
      mount();
    }
  }
})();
