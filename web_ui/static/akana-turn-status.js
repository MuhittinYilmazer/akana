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
  // The conversation whose turn is currently PAINTED on the (singleton) strip.
  // null = unbound (new-chat before adoption / no turn).
  let turnConvId = null;
  // PER-CONVERSATION clocks. Several conversations can run turns at the same time, but
  // there is ONE strip: keeping only the last-started turn's clock meant switching back
  // to a chat that was still working showed the OTHER chat's elapsed time and phase
  // (proven: A working 2s, B started 1s ago → returning to A read "0:01"). Each turn's
  // own start/phase/tool label lives here and is restored when its chat is displayed.
  const byConv = new Map(); // convId -> {startedAt, phase, toolLabel}

  function snapshot() {
    if (turnConvId == null) return;
    byConv.set(turnConvId, { startedAt, phase, toolLabel });
  }

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

  /**
   * Start the strip for a new turn. ``startedAtMs`` (optional) is when the turn REALLY
   * began — passed when reconnecting to a turn that has been running since before this
   * page existed (F5 / tab restore, from the resume endpoint's X-Akana-Turn-Started).
   * Without it the clock would restart at 0:00 and a 5-minute turn would look brand new.
   * A missing/absurd value (clock skew, a future stamp) falls back to "now".
   */
  function begin(convId, startedAtMs) {
    mount();
    active = true;
    const now = Date.now();
    const stamp = Number(startedAtMs);
    startedAt = Number.isFinite(stamp) && stamp > 0 && stamp <= now ? stamp : now;
    phase = "preparing";
    toolLabel = "";
    turnConvId = convId || null;
    snapshot(); // this conversation's own clock, so a concurrent turn can't overwrite it
    paint();
    if (timer == null) timer = window.setInterval(paint, 1000);
  }

  // Re-attach the strip to an ALREADY-RUNNING turn (conversation switch-back) WITHOUT
  // restarting the clock or reverting the phase — begin() would reset startedAt to now
  // ("Preparing · 0:00") and lose the true elapsed of the in-flight turn.
  // CONV-SCOPED: each conversation's clock is kept separately (see byConv), so switching
  // between two chats that are BOTH working shows each one its own elapsed/phase instead
  // of whichever turn started last. A conversation we have no clock for starts fresh.
  function resume(convId) {
    mount();
    active = true;
    const wantId = convId || null;
    // Unbound caller (no id — a new chat before it has one, or a legacy call): there is
    // nothing to disambiguate, so just re-attach to whatever is retained rather than
    // wiping a running turn's clock.
    if (wantId === null) {
      if (!startedAt) startedAt = Date.now();
      paint();
      if (timer == null) timer = window.setInterval(paint, 1000);
      return;
    }
    if (wantId === turnConvId && startedAt) {
      paint(); // already showing this conversation's turn
      if (timer == null) timer = window.setInterval(paint, 1000);
      return;
    }
    const saved = wantId !== null ? byConv.get(wantId) : null;
    if (saved && saved.startedAt) {
      startedAt = saved.startedAt;
      phase = saved.phase || "preparing";
      toolLabel = saved.toolLabel || "";
    } else {
      startedAt = Date.now();
      phase = "preparing";
      toolLabel = "";
    }
    turnConvId = wantId;
    snapshot();
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
    snapshot(); // keep this conversation's phase, so returning to it restores the real one
    paint();
  }

  /** Forget a conversation's clock — its turn is over (or the chat is gone). */
  function clear(convId) {
    const id = convId || null;
    if (id !== null) byConv.delete(id);
    if (id === null || id === turnConvId) {
      startedAt = 0;
      phase = "preparing";
      toolLabel = "";
      turnConvId = null;
    }
  }

  function isActive() {
    return active;
  }

  window.AkanaTurnStatus = { mount, begin, resume, end, setPhase, isActive, clear };

  if (typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", mount, { once: true });
    } else {
      mount();
    }
  }
})();
