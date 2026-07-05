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

  function begin() {
    mount();
    active = true;
    startedAt = Date.now();
    phase = "preparing";
    toolLabel = "";
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
    phase = "preparing";
    toolLabel = "";
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

  window.AkanaTurnStatus = { mount, begin, end, setPhase, isActive };

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", mount, { once: true });
    } else {
      mount();
    }
  }
})();
