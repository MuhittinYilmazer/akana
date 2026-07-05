/**
 * Akana chat transport — SSE stream, blocking POST /chat, conversation fetch.
 */
(() => {
  const baseUrl = () => window.AkanaCore.baseUrl();
  const authHeaders = (j) => window.AkanaCore.authHeaders(j);
  const parseApiError = (b, s) => window.AkanaCore.parseApiError(b, s);
  const escapeHtml = (s) => window.AkanaCore.escapeHtml(s);
  const setBubbleMarkdown = (b, t, o) => window.AkanaMarkdown.setBubbleMarkdown(b, t, o);
  // LIVE markdown — time-throttled setBubbleMarkdown on delta
  // (scheduleStreamMarkdownThrottled below). Previously, to prevent freeze, delta
  // used O(1) plain-text append and full markdown was only rendered on `done`;
  // the user wanted to see **bold**/code/tables formatted AS THEY STREAM.
  // Solution: time-throttle instead of per-frame (≈8 renders/s, adaptively slows
  // on large responses) → live formatting visible BUT freeze doesn't return. The
  // plain-text append helper is kept for backwards compatibility (voice/other paths).
  const appendBubbleStreamText = (b, p) => window.AkanaMarkdown.appendBubbleStreamText(b, p);

  const SSE_FRAME_BUDGET = 24;

  /** The reference bubble may have been removed from the DOM: on a pure-question or
   *  preamble+card turn, `done` deletes the empty live bubble after the card
   *  (insertBeforeBubble uses that bubble as its reference). insertBefore with a
   *  detached reference in a real DOM throws NotFoundError → if ref is no longer a
   *  child of msgBody, append instead. Since the card is already the last sibling,
   *  the warning/skill node lands in the right place (BELOW the card). */
  function insertBeforeOrAppend(msgBody, node, ref) {
    if (ref && ref.parentNode === msgBody) msgBody.insertBefore(node, ref);
    else msgBody.appendChild(node);
  }

  function create(chatCtx) {
    const renderToolCall = (c) => window.AkanaChatRender.renderToolCall(c);
    const upsertToolCallCard = (...a) => window.AkanaChatRender.upsertToolCallCard(...a);
    const renderMemoryUse = (p) => window.AkanaChatRender.renderMemoryUse(p);

    // Target THIS conv's OWN pane (not the displayed-pane getter) → even if called
    // while another chat is displayed, the row never goes to the wrong pane. Falls
    // back to hooks.log if paneFor is absent (tests).
    const paneForConv = (convId) =>
      (chatCtx.hooks.paneFor && convId) ? chatCtx.hooks.paneFor(convId) : chatCtx.hooks.log;

    // ── Live (streaming) markdown — TIME throttle ────────────────────────────
    // The user wants **bold**/```code```/|tables| visible live; but re-parsing
    // the full acc and destroying/rebuilding innerHTML on every rAF frame (60/s)
    // is O(N)/frame as the response grows → freeze. Solution: time-throttle —
    // render immediately if ≥ interval has elapsed since the last render, otherwise
    // set a trailing timer (one render interval after the last delta). ~8 renders/s
    // (≈15× cheaper). Interval grows adaptively on large responses (frame budget safe).
    // CONCURRENT MULTI-STREAM ISOLATION: ALL live-render state (throttle:
    // mdLast/mdTimer/mdJob; rAF-variant: mdPending/mdRaf; scroll:
    // scrollTarget/scrollRaf) lives ON each streamCtx, NOT at module level.
    // If shared, with two chats streaming simultaneously, B's job OVERWRITES A's →
    // A's live text freezes; A's pending render/scroll leaks into B
    // (same rationale as ensureToolScratch). Lazily initialized; GC'd with streamCtx.
    function ensureMdScratch(streamCtx) {
      const host = streamCtx || {};
      if (!host._mdScratch) {
        host._mdScratch = {
          mdLast: 0,        // last throttle render (ms)
          mdTimer: null,    // throttle trailing timer
          mdJob: null,      // throttle pending job { bubble, scroller, text }
          mdPending: null,  // rAF-variant pending job (isFirst/immediate)
          mdRaf: null,      // rAF-variant pending rAF handle
          scrollTarget: null, // pending scroll target (scroller)
          scrollRaf: null,    // pending scroll rAF handle
          shown: 0,           // smooth-reveal: number of characters SHOWN right now
          revealRaf: null,    // reveal rAF handle (advances `shown` per frame)
          revealJob: null,    // reveal target { bubble, scroller }
          revealStopped: false, // silence late reveal frames after teardown
        };
      }
      return host._mdScratch;
    }
    const STREAM_MD_BASE_MS = 50;
    // For responses exceeding this length, live markdown rendering is abandoned
    // (falls back to plain-text append) — prevents O(N) full-render freeze on very long text.
    const LIVE_MD_MAX = 12000;
    const _now = () =>
      typeof performance !== "undefined" && performance.now
        ? performance.now()
        : Date.now();
    function streamMdInterval(len) {
      // interval = max(120, floor(len/120)) → 120ms for small responses, grows
      // gradually for long ones (e.g. 36000 chars → 300ms), capped at 400ms.
      return Math.min(220, Math.max(STREAM_MD_BASE_MS, Math.floor((len || 0) / 160)));
    }
    function renderStreamMdNow(streamCtx) {
      const s = ensureMdScratch(streamCtx);
      if (s.mdTimer != null) {
        clearTimeout(s.mdTimer);
        s.mdTimer = null;
      }
      const job = s.mdJob;
      if (!job || !job.bubble) return;
      s.mdLast = _now();
      setBubbleMarkdown(job.bubble, job.text, { streaming: true });
      scheduleStreamScroll(streamCtx, job.scroller);
    }
    function scheduleStreamMarkdownThrottled(streamCtx, bubble, scroller, text) {
      const s = ensureMdScratch(streamCtx);
      s.mdJob = { bubble, scroller, text };
      const elapsed = _now() - s.mdLast;
      const interval = streamMdInterval(text.length);
      if (elapsed >= interval) {
        renderStreamMdNow(streamCtx);
        return;
      }
      if (s.mdTimer != null) return; // trailing timer already set
      s.mdTimer = setTimeout(() => {
        s.mdTimer = null;
        renderStreamMdNow(streamCtx);
      }, interval - elapsed);
    }
    function resetStreamMdThrottle(streamCtx) {
      const s = ensureMdScratch(streamCtx);
      if (s.mdTimer != null) {
        clearTimeout(s.mdTimer);
        s.mdTimer = null;
      }
      // Also cancel any pending rAF-variant + scroll handles: a late rAF after done/finally
      // must not overwrite the final text or scroll the wrong chat.
      if (s.mdRaf != null) {
        cancelAnimationFrame(s.mdRaf);
        s.mdRaf = null;
      }
      if (s.scrollRaf != null) {
        cancelAnimationFrame(s.scrollRaf);
        s.scrollRaf = null;
      }
      // Also stop the smooth-reveal loop: a late reveal frame after done/abort must
      // not overwrite the final FULL render (setBubbleMarkdown(acc)) with a short slice.
      if (s.revealRaf != null) {
        cancelAnimationFrame(s.revealRaf);
        s.revealRaf = null;
      }
      s.revealStopped = true;
      s.revealJob = null;
      s.mdLast = 0;
      s.mdJob = null;
      s.mdPending = null;
      s.scrollTarget = null;
    }
    let _sseQueue = [];
    let _sseDrainRaf = null;
    let _sseDrainWaiters = [];
    // A4: when the tab is hidden the browser SUSPENDS requestAnimationFrame, so an
    // rAF-only drain freezes ALL SSE frame delivery (tts_chunk/tts_end never reach the
    // player) and — once SSE_FRAME_BUDGET frames buffer — the read loop parks on
    // flushSseQueue forever. Track HOW the pending drain was scheduled so it can be
    // cancelled with the matching API, and schedule with setTimeout while hidden (voice
    // streams are deliberately NOT aborted on hidden). _sseDrainRaf stays the single
    // "a drain is pending" flag (non-null ⇒ scheduled) to preserve the flush/waiter contract.
    let _sseDrainKind = null; // "raf" | "timeout"
    const _docHidden = () =>
      typeof document !== "undefined" && document.hidden === true;
    function scheduleSseDrain() {
      if (_sseDrainRaf != null) return; // already scheduled
      const cb = () => {
        _sseDrainRaf = null;
        _sseDrainKind = null;
        drainSseQueue();
      };
      if (_docHidden() || typeof requestAnimationFrame !== "function") {
        _sseDrainKind = "timeout";
        _sseDrainRaf = setTimeout(cb, 0);
      } else {
        _sseDrainKind = "raf";
        _sseDrainRaf = requestAnimationFrame(cb);
      }
    }
    function cancelSseDrain() {
      if (_sseDrainRaf == null) return;
      if (_sseDrainKind === "timeout") clearTimeout(_sseDrainRaf);
      else if (typeof cancelAnimationFrame === "function") cancelAnimationFrame(_sseDrainRaf);
      _sseDrainRaf = null;
      _sseDrainKind = null;
    }

    // ── CONCURRENT DUAL-STREAM ISOLATION ─────────────────────────────────────
    // The live tool-card queue (pending + rAF) and anonymous tool-key state live
    // ON each streamCtx, NOT at module level. Two chats can stream simultaneously
    // (different conversations; a second message in the same conversation is queued
    // on the server with 202). With a shared queue, one stream's rAF flush would
    // inject ALL calls in _toolUiPending (including the other conversation's) into
    // the calling streamCtx's body → cards leaked into the wrong conversation; and
    // anon key seq/queues collided across streams. Lazily initialized;
    // GC'd with streamCtx — no explicit reset needed.
    function ensureToolScratch(streamCtx) {
      const host = streamCtx || {};
      if (!host._toolScratch) {
        host._toolScratch = {
          pending: new Map(),
          raf: null,
          keySeq: 0,
          openByName: new Map(),
        };
      }
      return host._toolScratch;
    }

    function resolveToolCallKey(streamCtx, call) {
      const direct = call && (call.id || call.call_id);
      if (direct) return String(direct);
      const fn = (call && call.function) || {};
      const raw =
        (call && call.name) || fn.name || (call && call.tool) || (call && call.toolName) || "tool";
      const name = String(raw || "tool");
      const phase = String((call && call.phase) || "").toLowerCase();
      const scratch = ensureToolScratch(streamCtx);
      const queue = scratch.openByName.get(name) || [];
      if (phase === "end" && queue.length) {
        const key = queue.shift();
        scratch.openByName.set(name, queue);
        return key;
      }
      const key = `anon:${name}:${++scratch.keySeq}`;
      if (phase === "start" || phase === "") {
        queue.push(key);
        scratch.openByName.set(name, queue);
      }
      return key;
    }

    function scheduleStreamMarkdownUpdate(streamCtx, bubble, scroller, text, immediate = false) {
      const s = ensureMdScratch(streamCtx);
      s.mdPending = { bubble, scroller, text };
      if (immediate) {
        if (s.mdRaf != null) {
          cancelAnimationFrame(s.mdRaf);
          s.mdRaf = null;
        }
        setBubbleMarkdown(bubble, text, { streaming: true });
        scheduleStreamScroll(streamCtx, scroller);
        return;
      }
      if (s.mdRaf != null) return;
      s.mdRaf = requestAnimationFrame(() => {
        s.mdRaf = null;
        const job = s.mdPending;
        s.mdPending = null;
        if (!job?.bubble) return;
        setBubbleMarkdown(job.bubble, job.text, { streaming: true });
        scheduleStreamScroll(streamCtx, job.scroller);
      });
    }

    function flushStreamMarkdownUpdate(streamCtx) {
      const s = ensureMdScratch(streamCtx);
      if (s.mdRaf != null) {
        cancelAnimationFrame(s.mdRaf);
        s.mdRaf = null;
      }
      const job = s.mdPending;
      s.mdPending = null;
      if (!job?.bubble) return;
      setBubbleMarkdown(job.bubble, job.text, { streaming: true });
      scheduleStreamScroll(streamCtx, job.scroller);
    }

    // ── Smooth reveal pacing ──────────────────────────────────────────────────
    // Problem: LLM/server deltas arrive in IRREGULAR bursts (claude CLI can buffer
    // output and push large blocks every few hundred ms) → text appears "jumping".
    // Solution: decouple ARRIVAL (bursty) from DISPLAY (smooth).
    // `shown` advances toward acc.length in small steps per frame;
    // render draws acc.slice(0, shown). Markdown parsing stays in the EXISTING
    // time-throttle (≤~20fps) → freeze risk is unchanged; only WHAT we draw is smoother.
    // A burst is spread evenly across the next few frames. When the loop catches up
    // (shown==target) it SUSPENDS; a new delta wakes it via scheduleStreamReveal.
    // On finish (done → resetStreamMdThrottle) the loop stops and the small remaining
    // backlog is completed by the final FULL render (setBubbleMarkdown(acc)).
    const REVEAL_CATCHUP_FRAMES = 8; // drain backlog in ~8 frames (adaptive speed)
    const REVEAL_MIN_STEP = 2;       // minimum step per frame (small backlog stays smooth)
    function stepStreamReveal(streamCtx) {
      const s = ensureMdScratch(streamCtx);
      s.revealRaf = null;
      if (s.revealStopped) return;
      const job = s.revealJob;
      if (!job || !job.bubble) return;
      // The post-card bubble must never re-draw the sealed preamble (see
      // sealBubbleAndAppendCard / stripSealedPrefix) — strip it from the reveal target too.
      const full = stripSealedPrefix(streamCtx.acc || "", streamCtx.sealedText);
      const target = full.length;
      const shown = s.shown || 0;
      if (shown < target) {
        const backlog = target - shown;
        const step = Math.max(REVEAL_MIN_STEP, Math.ceil(backlog / REVEAL_CATCHUP_FRAMES));
        s.shown = Math.min(target, shown + step);
        scheduleStreamMarkdownThrottled(streamCtx, job.bubble, job.scroller, full.slice(0, s.shown));
      }
      // If backlog remains, continue; suspend when caught up (new delta will wake it).
      if ((s.shown || 0) < target) {
        s.revealRaf = requestAnimationFrame(() => stepStreamReveal(streamCtx));
      }
    }
    function scheduleStreamReveal(streamCtx, bubble, scroller) {
      const s = ensureMdScratch(streamCtx);
      if (s.shown == null) s.shown = 0;
      s.revealStopped = false;
      s.revealJob = { bubble, scroller };
      if (s.revealRaf == null) {
        s.revealRaf = requestAnimationFrame(() => stepStreamReveal(streamCtx));
      }
    }
    function stopStreamReveal(streamCtx) {
      const s = ensureMdScratch(streamCtx);
      s.revealStopped = true;
      if (s.revealRaf != null) {
        cancelAnimationFrame(s.revealRaf);
        s.revealRaf = null;
      }
    }

    function scheduleStreamScroll(streamCtx, scroller) {
      if (!scroller) return;
      const s = ensureMdScratch(streamCtx);
      s.scrollTarget = scroller;
      if (s.scrollRaf != null) return;
      s.scrollRaf = requestAnimationFrame(() => {
        s.scrollRaf = null;
        if (s.scrollTarget) {
          chatCtx.hooks.stickToBottomIfFollowing(s.scrollTarget);
        }
      });
    }

    function flushStreamScroll(streamCtx) {
      const s = ensureMdScratch(streamCtx);
      if (s.scrollRaf != null) {
        cancelAnimationFrame(s.scrollRaf);
        s.scrollRaf = null;
      }
      if (s.scrollTarget) {
        chatCtx.hooks.stickToBottomIfFollowing(s.scrollTarget);
      }
    }

  // AURORA unified timeline: on a live turn, tool cards are added chronologically
  // to the thought-stream (process) body instead of a separate `.tool-calls-group`
  // → thinking steps + tools form a SINGLE readable timeline. Falls back to the
  // old group path defensively if the timeline body is absent.
  /** Removes the "current" marker from any open (in-progress) thinking/shell rows.
   *  Called when a tool card ENTERS the timeline: the first delta after the tool
   *  opens a new row → tool ↔ thinking steps stay in chronological order (otherwise
   *  the delta would write into the still-open --thinking-current ABOVE the tool). */
  function closeOpenThoughtLines(body) {
    if (!body) return;
    body
      .querySelector(".akana-thought-line--thinking-current")
      ?.classList.remove("akana-thought-line--thinking-current");
    body
      .querySelector(".akana-thought-line--shell-current")
      ?.classList.remove("akana-thought-line--shell-current");
  }

  function upsertLiveToolCard(streamCtx, call) {
    // Even if the tool is the first event (BEFORE thinking/activity), set up the
    // process card → it always goes into the UNIFIED timeline. Falls back to the
    // old group path defensively if the render helper is absent.
    if (streamCtx && window.AkanaChatRender?.upsertToolCardIntoTimeline) {
      const feed = ensureThoughtFeed(streamCtx);
      const body =
        streamCtx.thoughtBody || feed.querySelector(".akana-thought-feed-body");
      // Is this a NEW card? (patching an existing id doesn't disrupt chronology;
      // only the FIRST insertion should close open thought rows.)
      const id = call && (call.id || call.call_id);
      const isNew = !(
        id && body?.querySelector(`[data-tool-call-id="${CSS.escape(String(id))}"]`)
      );
      const node = window.AkanaChatRender.upsertToolCardIntoTimeline(body, call);
      if (isNew) closeOpenThoughtLines(body);
      syncLiveCancel(streamCtx, node);
      bumpProcessCounts(streamCtx);
      return node;
    }
    return upsertToolCallCard(streamCtx.msgBody, call, streamCtx.insertBeforeBubble);
  }

  /** "■ Cancel" button for a running tool card (.tool-cancel). Added immediately
   *  BELOW the card while it is running; removed when done. Cancel = abort the
   *  current stream + server-side turn cancellation (no new endpoint needed). */
  function syncLiveCancel(streamCtx, node) {
    if (!node) return;
    const running = node.dataset.status === "running";
    const next = node.nextElementSibling;
    const existing = next && next.classList?.contains("aur-tool-cancel-row") ? next : null;
    if (!running) {
      if (existing) existing.remove();
      return;
    }
    if (existing) return;
    const row = document.createElement("div");
    row.className = "aur-tool-cancel-row";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "aur-tool-cancel";
    btn.innerHTML =
      '<svg width="11" height="11" viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2" fill="currentColor"/></svg>';
    const lbl = document.createElement("span");
    lbl.textContent = window.AkanaI18n.t("transport.tool.cancel");
    btn.appendChild(lbl);
    btn.addEventListener("click", () => {
      btn.disabled = true;
      lbl.textContent = window.AkanaI18n.t("transport.tool.cancelling");
      try {
        abortActiveChatStream();
      } catch {
        /* ignore */
      }
      const cid =
        streamCtx.convId ||
        chatCtx.conversationIdForMemory?.() ||
        chatCtx.getConversationId?.();
      if (cid) void cancelActiveTurnOnServer(cid);
    });
    row.appendChild(btn);
    node.after(row);
  }

  function flushToolCallUpdates(streamCtx, scroller) {
    const scratch = ensureToolScratch(streamCtx);
    if (scratch.raf != null) {
      cancelAnimationFrame(scratch.raf);
      scratch.raf = null;
    }
    if (!scratch.pending.size) return;
    for (const call of scratch.pending.values()) {
      upsertLiveToolCard(streamCtx, call);
    }
    scratch.pending.clear();
    chatCtx.hooks.stickToBottomIfFollowing(scroller);
  }

  function scheduleToolCallFlush(streamCtx, scroller) {
    const scratch = ensureToolScratch(streamCtx);
    if (scratch.raf != null) return;
    scratch.raf = requestAnimationFrame(() => {
      scratch.raf = null;
      flushToolCallUpdates(streamCtx, scroller);
    });
  }

  // The `start` and `end` events of the same tool call can arrive in the SAME rAF frame.
  // A plain Map `set` would OVERWRITE the start payload (args) with the end payload
  // (args:null, result/status) → arg chip disappears and the status update appears
  // broken ("stuck on running" bug).
  // Solution: field-level MERGE — start's args are preserved, end's
  // result/status/phase overwrite (nulls do not erase existing values).
  function mergeToolCallPayloads(prev, next) {
    if (!prev) return next;
    const merged = { ...prev };
    for (const [k, v] of Object.entries(next)) {
      if (v == null && merged[k] != null) continue; // null must not erase existing value
      merged[k] = v;
    }
    // phase only moves forward: start→end (once end arrives, it stays).
    if (prev.phase === "end" && next.phase === "start") merged.phase = "end";
    return merged;
  }

  function queueToolCall(streamCtx, call, scroller) {
    const scratch = ensureToolScratch(streamCtx);
    const key = resolveToolCallKey(streamCtx, call || {});
    const incoming = { ...(call || {}), id: key, call_id: key };
    const payload = mergeToolCallPayloads(scratch.pending.get(key), incoming);
    scratch.pending.set(key, payload);
    // Accumulate in-flight tools on streamCtx for persistence. Even if the `done`
    // event's tool_calls is empty (some paths stream via live `tool_call` events and
    // do NOT resend the list in done), we store them here so cards survive F5.
    // Merge logic is the same as the UI (args preserved, result/status overwritten).
    if (streamCtx) {
      if (!streamCtx.liveToolCalls) streamCtx.liveToolCalls = new Map();
      streamCtx.liveToolCalls.set(
        key,
        mergeToolCallPayloads(streamCtx.liveToolCalls.get(key), payload),
      );
    }
    scheduleToolCallFlush(streamCtx, scroller);
  }

  /** Tool list to persist for this turn: returns the done payload if it is non-empty,
   *  otherwise returns the live-accumulated cards (empty array if both are empty). */
  function toolCallsToPersist(streamCtx, payload) {
    const fromDone =
      payload && Array.isArray(payload.tool_calls) ? payload.tool_calls : [];
    if (fromDone.length) return fromDone;
    return streamCtx && streamCtx.liveToolCalls
      ? Array.from(streamCtx.liveToolCalls.values())
      : [];
  }

  function notifySseDrainWaiters() {
    if (_sseQueue.length || _sseDrainRaf != null) return;
    const waiters = _sseDrainWaiters.splice(0);
    for (const resolve of waiters) resolve();
  }

  function drainSseQueue() {
    let budget = SSE_FRAME_BUDGET;
    while (_sseQueue.length && budget > 0) {
      const job = _sseQueue.shift();
      if (job) handleChatStreamEvent(job.frame, job.streamCtx);
      budget -= 1;
    }
    if (_sseQueue.length) {
      // A4: reschedule via the visibility-aware scheduler (setTimeout while hidden).
      scheduleSseDrain();
      return;
    }
    notifySseDrainWaiters();
  }

  function enqueueSseFrame(frame, streamCtx) {
    _sseQueue.push({ frame, streamCtx });
    // A4: visibility-aware scheduling (setTimeout while hidden; rAF while visible).
    scheduleSseDrain();
  }

  function flushSseQueue() {
    return new Promise((resolve) => {
      if (!_sseQueue.length && _sseDrainRaf == null) {
        resolve();
        return;
      }
      _sseDrainWaiters.push(resolve);
      if (_sseDrainRaf == null) drainSseQueue();
    });
  }

  // If streamCtx is provided, drop ONLY that stream's frames (concurrent other
  // stream's frames are preserved); otherwise do a full reset. Stream-scoped so
  // that one chat's start/end reset doesn't wipe the other chat's SSE frames
  // when two chats are streaming simultaneously.
  function resetSseQueue(streamCtx) {
    _sseQueue = streamCtx
      ? _sseQueue.filter((job) => job.streamCtx !== streamCtx)
      : [];
    if (!_sseQueue.length) {
      // A4: cancel via the scheduler-aware helper (pending drain may be a timeout, not rAF).
      cancelSseDrain();
      // b28: the queue is now empty → RESOLVE the drain waiters instead of silently dropping
      // them (`= []`). The waiter list is SHARED across concurrent streams, so discarding it
      // orphaned another stream's flushSseQueue promise — its `done` hung forever + leaked its
      // registry entry. An empty queue means there is nothing left to drain, so resolving is
      // correct.
      const waiters = _sseDrainWaiters.splice(0);
      for (const resolve of waiters) resolve();
    }
  }

    // ── Message-storm shield ──────────────────────────────────────────────
    // ROOT CAUSE: setConversationId → loadChatArchiveList + refreshMeta +
    // syncConversationLogFromServer chains were triggering
    // `GET …/messages?limit=500` dozens of times per second for the same convId
    // (no debounce/dedupe, in-flight requests not cancelled). Browser locked up.
    // Solution: ONE in-flight per convId — if a request is already running for the
    // same convId, that promise is shared; when the convId changes, the old one is
    // cancelled via AbortController.
    const _turnsInFlight = new Map(); // convId → { promise, abort }

    function abortConversationTurnsFetch(convId) {
      if (convId == null) {
        for (const entry of _turnsInFlight.values()) {
          try {
            entry.abort?.abort();
          } catch {
            /* ignore */
          }
        }
        _turnsInFlight.clear();
        return;
      }
      const entry = _turnsInFlight.get(convId);
      if (entry) {
        try {
          entry.abort?.abort();
        } catch {
          /* ignore */
        }
        _turnsInFlight.delete(convId);
      }
    }

    async function _doFetchConversationTurns(convId, abort) {
      const mr = await fetch(
        `${baseUrl()}/api/v1/conversations/${encodeURIComponent(convId)}/messages?limit=500`,
        { headers: authHeaders(), signal: abort?.signal },
      );
      if (mr.status === 404) return { status: 404, turns: null };
      if (!mr.ok) return { status: mr.status, turns: [] };
      const mj = await mr.json();
      const turns = Array.isArray(mj.messages) ? mj.messages : [];
      return { status: mr.status, turns };
    }

    async function fetchConversationTurnsFromServer(convId) {
      if (!convId) return { status: 0, turns: [] };
      // If a request is already in-flight for the same convId, share it (dedupe).
      const existing = _turnsInFlight.get(convId);
      if (existing) return existing.promise;
      const abort =
        typeof AbortController !== "undefined" ? new AbortController() : null;
      const promise = (async () => {
        try {
          return await _doFetchConversationTurns(convId, abort);
        } catch (e) {
          if (e && e.name === "AbortError") return { status: 0, turns: [], aborted: true };
          throw e;
        } finally {
          if (_turnsInFlight.get(convId)?.promise === promise) {
            _turnsInFlight.delete(convId);
          }
        }
      })();
      _turnsInFlight.set(convId, { promise, abort });
      return promise;
    }

    // ── On-return turn resume ──────────────────────────────────────────────
    // BACKEND CONTRACT: GET /api/v1/chat/active/{cid}
    //   • NO active turn → 204 No Content
    //   • Active turn EXISTS → replays the SSE chunks accumulated so far and
    //     returns a live continuing SSE stream (text/event-stream).
    // 404/405 (old server / route missing) is also silently treated as "no active turn" (null).
    let _activeProbeInFlight = null;

    async function probeActiveTurn(convId) {
      if (!convId) return null;
      if (_activeProbeInFlight) {
        try {
          await _activeProbeInFlight;
        } catch {
          /* ignore */
        }
      }
      const run = (async () => {
        try {
          const r = await fetch(
            `${baseUrl()}/api/v1/chat/active/${encodeURIComponent(convId)}`,
            { headers: authHeaders() },
          );
          if (r.status === 204 || r.status === 404 || r.status === 405) {
            return null; // no active turn (or endpoint not yet available)
          }
          if (!r.ok || !r.body) return null;
          return r; // live SSE Response — caller reads it
        } catch {
          return null;
        }
      })();
      _activeProbeInFlight = run;
      try {
        return await run;
      } finally {
        if (_activeProbeInFlight === run) _activeProbeInFlight = null;
      }
    }

    /** Ensure a stable server conversation id before streaming (avoids one-turn-per-ulid). */
  async function ensureConversationIdReady() {
    let existing = chatCtx.conversationIdForMemory();
    if (existing) return existing;
    // NEW-CHAT COORDINATION: chatStartNewThread may be doing a conv eager-create in the
    // background. Wait for it to finish so IT assigns the conv id — otherwise a SECOND
    // conv POST is made here (double-create + orphan empty conv + wrong conv in sidebar).
    // After waiting, use the conv id if it arrived.
    try {
      const pending = chatCtx.pendingNewThread?.();
      if (pending && typeof pending.then === "function") await pending;
    } catch {
      /* ignore — even if eager-create fails, we have our own POST below */
    }
    existing = chatCtx.conversationIdForMemory();
    if (existing) return existing;
    try {
      const r = await fetch(`${baseUrl()}/api/v1/conversations`, {
        method: "POST",
        headers: authHeaders(true),
        body: JSON.stringify({}), // audit H2: do NOT send title → server title_source="auto" → first-message auto-title
      });
      if (r.ok) {
        const meta = await r.json();
        if (meta?.id) {
          chatCtx.setConversationId(meta.id);
          // PARALLEL-CHAT: IMMEDIATELY bind the displayed (empty/new) pane to the real
          // conv-id — otherwise the streamChat `paneForConv(meta.id)` below would open a
          // HIDDEN new pane, the stream would render there, and adoptStreamConversationId's
          // rekey would delete that hidden pane via the single-pane invariant →
          // the first message's response becomes INVISIBLE (persisted on the server,
          // comes back on switch/F5). The eager-create path already does this via
          // rekeyConversation(null, id); this was the missing counterpart for the
          // fallback-POST path (rekeyDisplayedPane protects the real displayed pane from
          // being clobbered → no-op in background/other-chat situations).
          chatCtx.rekeyDisplayedPane?.(meta.id);
          return meta.id;
        }
      }
    } catch {
      /* stream meta may still assign an id */
    }
    return chatCtx.conversationIdForMemory() || null;
  }

  /** Show token count in compact form: 980→"980", 1234→"1.2k", 45678→"46k". */
  function formatTokenCount(n) {
    const v = Number(n) || 0;
    if (v < 1000) return String(v);
    if (v < 10000) return (v / 1000).toFixed(1).replace(/\.0$/, "") + "k";
    return Math.round(v / 1000) + "k";
  }

  /** USD cost in readable form: more decimal places for small amounts. "" → hide. */
  function formatCostUsd(cost) {
    const v = Number(cost);
    if (!Number.isFinite(v) || v <= 0) return "";
    if (v >= 1) return `$${v.toFixed(2)}`;
    if (v >= 0.01) return `$${v.toFixed(3)}`;
    return `$${v.toFixed(4)}`;
  }

  /** Is token & cost display enabled in settings? The ``show-usage`` body class
   *  is managed by ``applyShowUsage`` in ``akana-settings.js`` (default OFF).
   *  When off, token/cost segments in the meta row are hidden. */
  function usageDisplayEnabled() {
    return document.body.classList.contains("show-usage");
  }

  /** Duration segment: "N ms" below 1s, "X.Xs" at/above 1s.
   *  SYMMETRIC with formatMetaDuration in akana-chat-render.js. */
  function formatStreamDuration(ms) {
    if (ms >= 1000) return `${(ms / 1000).toFixed(1)}s`;
    return `${ms} ms`;
  }

  /** "provider/model" segment read from the top-bar pill — neither the SSE `done`
   *  payload (ChatResponse: text/latency_ms/tokens/tool_calls/…) nor doneMeta carry
   *  a model/provider field, so this reads the same live pill aurora-voice.js already
   *  reads for its header cluster. Pill text "Claude · opus" → "claude/opus".
   *  Returns "" when the pill is absent/empty (segment omitted). */
  function formatStreamModelSeg() {
    try {
      const pill = document.getElementById("model-pill");
      if (!pill) return "";
      // data-state warn/bad = pill unresolved/errored ("select model",
      // "no connection", "Claude · select model") — NOT a model, skip the
      // segment entirely. Resolved state = attribute ABSENT (akana-settings
      // contract). Also, since the real label always has the form
      // "Provider · tag", text without '·' is filtered out too (review finding).
      if (pill.dataset.state) return "";
      const raw = (pill.querySelector(".status-text") || pill).textContent || "";
      const txt = raw.trim();
      if (!txt || txt === "…" || txt.indexOf("·") === -1) return "";
      const parts = txt.split("·").map((p) => p.trim()).filter(Boolean);
      if (parts.length < 2) return "";
      // Claude & Cursor providers: omit the provider/model segment entirely (user preference).
      const prov = parts[0].toLowerCase();
      if (prov === "claude" || prov === "cursor") return "";
      return parts.join("/").toLowerCase();
    } catch {
      return "";
    }
  }

  /** Safe i18n access — the meta path runs on EVERY done event; in contexts
   *  where the i18n module isn't loaded (e.g. node-vm contract harnesses) a
   *  bare window.AkanaI18n.t call throws. Falls back to the fallback text. */
  function metaT(key, fallback, params) {
    const i18n = window.AkanaI18n;
    if (i18n && typeof i18n.t === "function") return i18n.t(key, params);
    let s = fallback || key;
    if (params) for (const k in params) s = s.split(`{${k}}`).join(String(params[k]));
    return s;
  }

  /** Build (but do not insert) the "tamam/hata" status chip span for the meta line.
   *  SYMMETRIC with buildMetaStatusChip in akana-chat-render.js. */
  function buildStreamStatusChip(state) {
    const chip = document.createElement("span");
    chip.className = "turn-status-chip";
    chip.dataset.state = state;
    chip.textContent =
      state === "err"
        ? metaT("chat.turn_status_err", "error")
        : metaT("chat.turn_status_done", "done");
    return chip;
  }

  /** Write the meta line's text AND its trailing status chip together — a plain
   *  ``metaEl.textContent = text`` wipes any previously appended chip, so every
   *  call site that finalizes the meta line (there are several: first-delta,
   *  live-usage, done, redundant post-consume overwrite) must go through this
   *  helper instead of assigning textContent directly, or the chip silently
   *  disappears on the next overwrite. `state` omitted → no chip (mid-stream). */
  function setStreamMetaText(metaEl, text, state) {
    metaEl.textContent = text;
    if (state) metaEl.appendChild(buildStreamStatusChip(state));
  }

  /** Assistant meta row: "Akana · {n} tool · {ms} ms · {tokens} tok · ${cost} · provider/model".
   *  Backwards-compatible — without tokens, just "Akana" / "Akana · N ms".
   *  ``doneMeta.tokens`` is the {prompt, completion, cost_usd?} block from the
   *  producer's done SSE; cost_usd only arrives when the provider supplies it (claude).
   *  Token/cost segments are only added when the ``show-usage`` setting is enabled.
   *  Tool count: ``toolCountOverride`` (caller passes the DOM ``.tool-call`` count from
   *  the finalized process card — the accurate, deduplicated number) wins when given;
   *  otherwise falls back to ``doneMeta.tool_calls.length`` (raw done-payload list, may
   *  double-count start+end events per tool — same caveat noted on renderToolProcessCard
   *  in akana-chat-render.js). Absent entirely during earlier live calls (first-delta /
   *  live-usage, before `done` arrives) — those simply omit the segment until finalize. */
  function formatAssistantStreamMeta(turnId, doneMeta, toolCountOverride) {
    const segs = ["Akana"];
    const calls = doneMeta && Array.isArray(doneMeta.tool_calls) ? doneMeta.tool_calls : null;
    const toolCount =
      typeof toolCountOverride === "number" ? toolCountOverride : calls ? calls.length : 0;
    if (toolCount > 0) {
      segs.push(metaT("msg.n_tools", "{n} tools", { n: toolCount }));
    }
    const ms = doneMeta && doneMeta.latency_ms;
    if (typeof ms === "number") segs.push(formatStreamDuration(ms));
    const tok = doneMeta && doneMeta.tokens;
    if (tok && typeof tok === "object" && usageDisplayEnabled()) {
      const total = (Number(tok.prompt) || 0) + (Number(tok.completion) || 0);
      if (total > 0) segs.push(`${formatTokenCount(total)} ${window.AkanaI18n.t("transport.tokens.label")}`);
      const cost = formatCostUsd(tok.cost_usd);
      if (cost) segs.push(cost);
    }
    const modelSeg = formatStreamModelSeg();
    if (modelSeg) segs.push(modelSeg);
    return segs.join(" · ");
  }

  /* ── Turn HUD (Feature 3): a quiet mono pill near the meta line — LIVE total
     tokens (count-up), tokens/sec (from consecutive usage events), and live cost
     when cost_usd is present. Same usageDisplayEnabled() gate as the meta line;
     removed (folded into the final meta line) once the turn completes. */

  /** Create (once) and return the `.turn-hud` pill, inserted right after the
   *  meta line so it reads naturally with the "Akana · …" row above the bubble. */
  function ensureTurnHud(streamCtx) {
    if (streamCtx.hud) return streamCtx.hud;
    if (!streamCtx.meta || !streamCtx.meta.parentNode) return null;
    const pill = document.createElement("div");
    pill.className = "turn-hud";
    const tokEl = document.createElement("span");
    tokEl.className = "turn-hud-tokens";
    const tpsEl = document.createElement("span");
    tpsEl.className = "turn-hud-tps";
    const costEl = document.createElement("span");
    costEl.className = "turn-hud-cost";
    pill.append(tokEl, tpsEl, costEl);
    streamCtx.meta.after(pill);
    streamCtx.hud = { pill, tokEl, tpsEl, costEl, shownTotal: 0, raf: null };
    return streamCtx.hud;
  }

  /** Smoothly count `hud.shownTotal` up toward `target` (short tween, ~300ms worth
   *  of frames) instead of jumping — same "count-up" feel as other live counters
   *  in this codebase (see stepStreamReveal). */
  function tweenHudTokens(hud, target) {
    if (hud.raf != null) cancelAnimationFrame(hud.raf);
    const step = () => {
      hud.raf = null;
      const cur = hud.shownTotal;
      if (cur === target) return;
      const diff = target - cur;
      const inc = Math.sign(diff) * Math.max(1, Math.ceil(Math.abs(diff) / 6));
      hud.shownTotal = Math.abs(inc) >= Math.abs(diff) ? target : cur + inc;
      hud.tokEl.textContent = `${formatTokenCount(hud.shownTotal)} ${window.AkanaI18n.t("transport.tokens.label")}`;
      if (hud.shownTotal !== target) hud.raf = requestAnimationFrame(step);
    };
    step();
  }

  /** Update the HUD from a live `usage` SSE payload {prompt, completion, cost_usd?}.
   *  Gated by usageDisplayEnabled() — identical contract to the meta line. */
  function updateTurnHud(streamCtx, usage) {
    if (!usageDisplayEnabled()) return;
    if (!usage || typeof usage.prompt !== "number") return;
    const hud = ensureTurnHud(streamCtx);
    if (!hud) return;
    const total = (Number(usage.prompt) || 0) + (Number(usage.completion) || 0);
    const now = _now();
    if (hud.lastTotal != null && hud.lastAt != null && now > hud.lastAt) {
      const dTok = total - hud.lastTotal;
      const dSec = (now - hud.lastAt) / 1000;
      if (dTok > 0 && dSec > 0) {
        hud.tpsEl.textContent = window.AkanaI18n.t("transport.hud.tps", {
          n: Math.round(dTok / dSec),
        });
      }
    }
    hud.lastTotal = total;
    hud.lastAt = now;
    tweenHudTokens(hud, total);
    const cost = formatCostUsd(usage.cost_usd);
    hud.costEl.textContent = cost || "";
  }

  /** Remove the HUD pill — the turn's final tally already lives in the meta line
   *  (formatAssistantStreamMeta), so the live pill is redundant once done. */
  function removeTurnHud(streamCtx) {
    const hud = streamCtx.hud;
    if (!hud) return;
    if (hud.raf != null) cancelAnimationFrame(hud.raf);
    hud.pill.remove();
    streamCtx.hud = null;
  }

  const THOUGHT_FEED_MAX_LINES = 96;

  /** Persist the open/collapsed state of the process panel by turn_id (so a panel
   *  the user collapsed stays collapsed after F5/resume). turn_id is known from meta. */
  function persistFeedCollapse(streamCtx, feed) {
    const tid =
      (streamCtx && streamCtx.turnId) || feed.closest?.(".row")?.dataset?.turnId;
    if (tid) {
      window.AkanaChatRender?.setPanelCollapsed?.(
        tid,
        feed.classList.contains("is-collapsed"),
      );
    }
  }

  /** Apply the saved open/collapsed preference to a live/resume feed (when turn_id is known). */
  function applyFeedCollapsePref(streamCtx) {
    const feed = streamCtx && streamCtx.thoughtFeed;
    if (!feed) return;
    const tid = streamCtx.turnId || feed.closest?.(".row")?.dataset?.turnId;
    if (!tid) return;
    const pref = window.AkanaChatRender?.getPanelCollapsed?.(tid);
    if (pref === true) feed.classList.add("is-collapsed");
    else if (pref === false) feed.classList.remove("is-collapsed");
  }

  // AURORA: thinking steps + tool cards in a SINGLE chronological "process card".
  // Class names (akana-thought-feed/-head/-body, is-live, is-collapsed) are
  // PRESERVED — only `aur-process` + structured head spans are ADDED.
  // Head: [spark] [label] [sub: duration/live] [chevron]. Label shimmers when live.
  function ensureThoughtFeed(streamCtx) {
    if (streamCtx.thoughtFeed) return streamCtx.thoughtFeed;
    const feed = document.createElement("div");
    feed.className = "akana-thought-feed aur-process is-live is-collapsed";
    const head = document.createElement("div");
    head.className = "akana-thought-feed-head aur-process-head";
    const spark = document.createElement("span");
    spark.className = "aur-process-spark";
    spark.setAttribute("aria-hidden", "true");
    const lbl = document.createElement("span");
    lbl.className = "aur-process-label";
    lbl.textContent = window.AkanaI18n.t("transport.process.working");
    const sub = document.createElement("span");
    sub.className = "aur-process-sub";
    sub.textContent = window.AkanaI18n.t("transport.process.live");
    const chev = document.createElement("span");
    chev.className = "aur-process-chev";
    chev.setAttribute("aria-hidden", "true");
    head.append(spark, lbl, sub, chev);
    // Click header → collapse/expand the process panel. Works on LIVE turns too
    // (user may want to hide the tool stream while the LLM is running). The same
    // toggle is not re-wired by finalizeThoughtFeed (guarded by `toggleWired`).
    head.dataset.toggleWired = "1";
    head.style.cursor = "pointer";
    head.setAttribute("role", "button");
    head.setAttribute("tabindex", "0");
    head.addEventListener("click", () => {
      feed.classList.toggle("is-collapsed");
      persistFeedCollapse(streamCtx, feed);
    });
    head.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        feed.classList.toggle("is-collapsed");
        persistFeedCollapse(streamCtx, feed);
      }
    });
    const body = document.createElement("div");
    body.className = "akana-thought-feed-body aur-timeline";
    feed.appendChild(head);
    feed.appendChild(body);
    streamCtx.insertBeforeBubble(feed);
    streamCtx.thoughtFeed = feed;
    streamCtx.thoughtBody = body;
    streamCtx.thoughtLineCount = 0;
    streamCtx.processStartedAt = streamCtx.processStartedAt || _now();
    // If turn_id is known (meta has arrived), apply the saved collapsed/open preference.
    applyFeedCollapsePref(streamCtx);
    return feed;
  }

  /** Live-refresh the step/tool count in the process card head label. */
  function refreshProcessHead(streamCtx) {
    const feed = streamCtx.thoughtFeed;
    if (!feed || feed.dataset.finalized === "1") return;
    const body = streamCtx.thoughtBody;
    if (!body) return;
    const steps = body.querySelectorAll(
      ".akana-thought-line--think, .akana-thought-line--step, .akana-thought-line--summary, .akana-thought-line--note",
    ).length;
    const tools = body.querySelectorAll(".tool-call").length;
    const lbl = feed.querySelector(".aur-process-label");
    if (!lbl) return;
    const running = body.querySelector('.tool-call[data-status="running"]');
    if (running) {
      lbl.textContent = window.AkanaI18n.t("transport.process.working");
      feed.classList.add("is-running");
      return;
    }
    feed.classList.remove("is-running");
    const parts = [];
    if (steps) parts.push(window.AkanaI18n.t("transport.process.thought_n", { n: steps }));
    if (tools) parts.push(window.AkanaI18n.t("transport.process.tool_n", { n: tools }));
    lbl.textContent = parts.length ? parts.join(" · ") : window.AkanaI18n.t("transport.process.working");
  }

  /** Update the head count when a tool card enters the timeline. */
  function bumpProcessCounts(streamCtx) {
    refreshProcessHead(streamCtx);
  }

  function trimThoughtFeed(body) {
    while (body.children.length > THOUGHT_FEED_MAX_LINES) {
      body.firstChild?.remove();
    }
  }

  function appendThoughtFeedLine(streamCtx, text, kind) {
    const lineText = String(text || "").trim();
    if (!lineText) return;
    const body = ensureThoughtFeed(streamCtx).querySelector(".akana-thought-feed-body");
    const line = document.createElement("div");
    line.className = `akana-thought-line akana-thought-line--${kind || "note"}`;
    line.textContent = lineText;
    body.appendChild(line);
    streamCtx.thoughtLineCount = (streamCtx.thoughtLineCount || 0) + 1;
    trimThoughtFeed(body);
    refreshProcessHead(streamCtx);
    chatCtx.hooks.stickToBottomIfFollowing?.(streamCtx.scroller);
  }

  function appendThinkingDelta(streamCtx, piece) {
    const text = String(piece || "");
    if (!text) return;
    const body = ensureThoughtFeed(streamCtx).querySelector(".akana-thought-feed-body");
    let cur = body.querySelector(".akana-thought-line--thinking-current");
    if (!cur) {
      cur = document.createElement("div");
      cur.className = "akana-thought-line akana-thought-line--think akana-thought-line--thinking-current";
      body.appendChild(cur);
    }
    cur.textContent = (cur.textContent || "") + text;
    trimThoughtFeed(body);
    chatCtx.hooks.stickToBottomIfFollowing?.(streamCtx.scroller);
  }

  function syncTurnStatusTool(call, streamCtx) {
    const ts = window.AkanaTurnStatus;
    if (!ts?.isActive()) return;
    // FOREGROUND GATE: the strip is a singleton reflecting the DISPLAYED conversation's
    // turn. Background streams keep running (parallel-chat) and must not overwrite the
    // visible chat's phase/tool label with their own tool events.
    if (streamCtx && !isForegroundStream(streamCtx)) return;
    const status = window.AkanaChatRender?.toolCallStatus?.(call);
    if (status === "running") {
      streamCtx.toolPhaseActive = true;
      const action = window.AkanaChatRender?.toolCallActionSentence?.(call);
      ts.setPhase("tool", action?.text || window.AkanaI18n.t("transport.tool.fallback"));
      return;
    }
    streamCtx.toolPhaseActive = false;
    if (streamCtx.acc && streamCtx.acc.length > 0) ts.setPhase("writing");
    else ts.setPhase("thinking");
  }

  /** Set UI to idle when SSE `done`/`error` arrives, without waiting for the
   *  pending connection close. */
  // The global UI (composer SEND↔STOP / "Responding" bar / chatStreaming flag /
  // composer hint) is a SINGLETON and reflects the FOREGROUND (displayed) stream.
  // If a streamCtx is provided AND that stream is NOT the DISPLAYED chat's stream
  // (background: concurrent send to another chat), do NOT touch the global UI —
  // otherwise when a background stream finishes it would prematurely flip the visible
  // chat's composer to SEND / close "Responding" (FE bug #18 finalize half).
  // If no streamCtx is provided (explicit teardowns like STOP/error/WS-reconcile)
  // always do a global close.
  function finalizeStreamUi(streamCtx) {
    // FOREGROUND GATE: if a streamCtx is provided AND that stream is NOT the
    // DISPLAYED chat's stream (background: concurrent send to another chat), do NOT
    // touch the global UI. If no streamCtx (STOP/error/WS-reconcile explicit teardown),
    // always do a global close.
    if (streamCtx && !isForegroundStream(streamCtx)) return;
    const logRoot = chatCtx.hooks.log;
    if (logRoot) delete logRoot.dataset.chatStreaming;
    window.AkanaTurnStatus?.end();
    chatCtx.hooks.setStreamingUi?.(false);
    chatCtx.hooks.setComposerHint?.("idle");
  }

  /** RESOURCES row (.cites): coloured source chips BELOW the bubble.
   *  Sources = memory_use items collected during the stream + staging
   *  memory_writes from done. Nothing is added if there is no data (no fabrication). */
  function maybeRenderSourcesRow(streamCtx, payload) {
    const render = window.AkanaChatRender?.renderSourcesRow;
    if (typeof render !== "function") return;
    const { msgBody } = streamCtx;
    if (!msgBody || msgBody.querySelector(".aur-sources")) return;
    const items = [];
    if (Array.isArray(streamCtx.memorySources)) items.push(...streamCtx.memorySources);
    const writes = (payload && payload.memory_writes) || [];
    for (const w of writes) {
      if (w && (w.kind === "staging" || w.kind === "stored")) items.push(w);
    }
    if (!items.length) return;
    const row = render(items);
    if (row) {
      msgBody.appendChild(row); // AFTER the bubble → below the answer
      chatCtx.hooks.stickToBottomIfFollowing?.(streamCtx.scroller);
    }
  }

  /** Approval card (.approval): when meta.approval_required=true, insert an
   *  inline permission card ABOVE the bubble. Button actions send "approve"/"deny"
   *  through the composer (existing Plan→Approve→Apply text reflection). No
   *  structured command/endpoint on the backend → short description instead of
   *  command box; button behaviour is tied to the existing send path. */
  function maybeRenderApprovalCard(streamCtx, metaPayload) {
    const render = window.AkanaChatRender?.renderApprovalCard;
    if (typeof render !== "function") return;
    if (!metaPayload || !metaPayload.approval_required) return;
    if (streamCtx.approvalShown) return;
    streamCtx.approvalShown = true;
    const intent = String(metaPayload.intent || "").trim();
    const isSystem = intent === "system_action";
    const card = render({
      icon: isSystem ? "⚡" : "🛡️",
      title: isSystem ? window.AkanaI18n.t("transport.approval.intent_system") : window.AkanaI18n.t("transport.approval.intent_other"),
      badge: window.AkanaI18n.t("transport.approval.badge"),
      detail: window.AkanaI18n.t("transport.approval.detail"),
      onAllow: () => submitApprovalReply(window.AkanaI18n.t("transport.approval.allow_reply")),
      onDeny: () => submitApprovalReply(window.AkanaI18n.t("transport.approval.deny_reply")),
    });
    if (card) streamCtx.insertBeforeBubble(card);
  }

  /** INLINE CARD PLACEMENT: "seals" the accumulated stream text into a completed
   *  bubble, appends the card as the next sibling in msgBody, then opens a FRESH
   *  bubble to hold the delta text that follows. This way the card appears exactly
   *  where it occurred in the stream (not before the text). If the current bubble
   *  is empty (pure-question turn) it is removed — the card is never orphaned.
   *
   *  streamCtx.bubble and streamCtx.insertBeforeBubble ARE UPDATED; subsequent
   *  handleChatStreamEvent calls write to the new bubble. */
  function sealBubbleAndAppendCard(streamCtx, card) {
    const { msgBody, bubble, scroller } = streamCtx;
    if (!msgBody || !card) return;

    // Seal the current bubble: write accumulated text if any, remove "pending" classes.
    const hasText = (streamCtx.acc || "").trim().length > 0;
    if (hasText) {
      setBubbleMarkdown(bubble, streamCtx.acc);
      bubble.classList.remove("bubble-bot-pending", "bubble-bot-stream");
      bubble.removeAttribute("aria-busy");
      // Mark that this bubble is no longer the live-markdown target.
      streamCtx._sealedBubbles = streamCtx._sealedBubbles || [];
      streamCtx._sealedBubbles.push(bubble);
      // Remember the sealed text → `done` must NOT write this prefix again to
      // the fresh post-card bubble (preamble must not duplicate below the card).
      // The full-turn text stays in `acc`; only the RENDER target is trimmed.
      // See stripSealedPrefix.
      streamCtx.sealedText = streamCtx.acc;
    } else {
      // Empty bubble (no text yet) — remove from DOM so the card isn't left dangling at the top.
      bubble.remove();
    }

    // Append the card to msgBody (AFTER the sealed bubble).
    msgBody.appendChild(card);

    // Set up a fresh bubble for subsequent deltas.
    const newBubble = document.createElement("div");
    newBubble.className = "bubble-assistant bubble-bot bubble-bot-pending bubble-bot-stream";
    newBubble.setAttribute("aria-busy", "true");
    msgBody.appendChild(newBubble);

    // Update streamCtx: write to the fresh bubble from now on.
    streamCtx.bubble = newBubble;
    streamCtx.insertBeforeBubble = (node) => insertBeforeOrAppend(msgBody, node, newBubble);

    // Reset the current markdown scratch for the fresh bubble.
    // Also stop the smooth-reveal loop: it targets streamCtx.acc, which still holds
    // the sealed preamble text; a pending/late reveal frame would either re-type the
    // preamble into the OLD (now sealed) bubble or, once a later delta wakes it with
    // the fresh bubble, render the preamble a second time BELOW the card.
    stopStreamReveal(streamCtx);
    const s = ensureMdScratch(streamCtx);
    s.mdJob = null;
    s.mdPending = null;
    s.shown = 0;
    s.mdLast = 0;

    chatCtx.hooks.stickToBottomIfFollowing?.(scroller);
  }

  /** Inline card prefix-stripping: when an ask_user/plan card arrives the preamble
   *  text is written to the PREVIOUS (sealed) bubble and a fresh post-card bubble is
   *  opened. The `done` event carries the FULL turn text (including preamble) → we
   *  strip the sealed prefix before writing to the post-card bubble; otherwise the
   *  preamble would appear AGAIN below the card. If there is no sealed text (no card)
   *  the full text is returned — no stripping. If the prefix does not match, safe
   *  fallback: all text was already sealed → "" (leave empty rather than duplicate the
   *  preamble; the caller removes the empty bubble). */
  function stripSealedPrefix(fullText, sealedText) {
    const full = String(fullText || "");
    const sealed = String(sealedText || "");
    if (!sealed) return full;
    if (full.startsWith(sealed)) return full.slice(sealed.length);
    // Backend may trim done.text; sealing uses raw `acc` → compare trimmed.
    const fullT = full.trim();
    const sealedT = sealed.trim();
    if (sealedT && fullT.startsWith(sealedT)) return fullT.slice(sealedT.length);
    return "";
  }

  /** AskUserQuestion card — shown when Claude (headless) asks a structured question
   *  (backend `ask_user` event + done.ask_user). The card is shown exactly once
   *  (live event OR done, whichever arrives first); the same question-id is never
   *  rendered twice. The answer goes as the NEXT user message → `--resume` lets
   *  Claude see the question as answered. */
  function maybeRenderAskUserCard(streamCtx, askPayload) {
    if (!askPayload || typeof askPayload !== "object") return;
    const render = window.AkanaChatRender?.renderAskUserCard;
    if (typeof render !== "function") return;
    const askId = askPayload.id != null ? String(askPayload.id) : "";
    // Double-emit guard: the same question arrives in both the live event and done.
    if (streamCtx.askUserShownId && streamCtx.askUserShownId === askId) return;
    if (streamCtx.askUserShown && !askId) return;
    const { msgBody } = streamCtx;
    if (msgBody && askId && msgBody.querySelector(`.aur-ask[data-ask-id="${cssEscape(askId)}"]`)) {
      streamCtx.askUserShown = true;
      streamCtx.askUserShownId = askId;
      return;
    }
    const card = render({
      question: askPayload,
      onSubmit: (answer) => submitAnswerReply(answer),
    });
    if (!card) return;
    streamCtx.askUserShown = true;
    streamCtx.askUserShownId = askId;
    // INLINE: seal text → append card at the correct position in the stream.
    sealBubbleAndAppendCard(streamCtx, card);
  }

  /** Plan card — shown when Claude (plan-mode / ExitPlanMode) presents a plan
   *  (backend `plan_review` event + done.plan_review). Same double-emit guard as
   *  AskUser (live event OR done, whichever arrives first); the same plan-id is
   *  never rendered twice. "Apply" → apply the plan (resume plan OFF); "Revise" →
   *  re-plan with free text (resume plan ON). The decision goes as the NEXT user
   *  message → `--resume` lets Claude retain context. */
  function maybeRenderPlanCard(streamCtx, planPayload) {
    if (!planPayload || typeof planPayload !== "object") return;
    const render = window.AkanaChatRender?.renderPlanCard;
    if (typeof render !== "function") return;
    const planId = planPayload.id != null ? String(planPayload.id) : "";
    // Double-emit guard: the same plan arrives in both the live event and done.
    if (streamCtx.planShownId && streamCtx.planShownId === planId) return;
    if (streamCtx.planShown && !planId) return;
    const { msgBody } = streamCtx;
    if (msgBody && planId && msgBody.querySelector(`.aur-plan[data-plan-id="${cssEscape(planId)}"]`)) {
      streamCtx.planShown = true;
      streamCtx.planShownId = planId;
      return;
    }
    const card = render({
      plan: planPayload,
      onApprove: () => submitPlanReply(window.AkanaI18n.t("transport.plan.approve_reply"), { keepPlanning: false }),
      onRevise: (text) => submitPlanReply(text, { keepPlanning: true }),
    });
    if (!card) return;
    streamCtx.planShown = true;
    streamCtx.planShownId = planId;
    // INLINE: seal text → append card at the correct position in the stream.
    sealBubbleAndAppendCard(streamCtx, card);
  }

  /** ID escaping for querySelector (simple fallback when CSS.escape is unavailable). */
  function cssEscape(s) {
    if (window.CSS && typeof window.CSS.escape === "function") return window.CSS.escape(s);
    return String(s).replace(/["\\\]]/g, "\\$&");
  }

  /** Send the AskUserQuestion answer as the next user message →
   *  `--resume` shows Claude the question as answered. Falls back to the
   *  composer/form path if the AkanaChat general API is unavailable (symmetric
   *  with approval). */
  function submitAnswerReply(text) {
    const t = String(text || "").trim();
    if (!t) return;
    if (typeof window.AkanaChat?.submitAnswerText === "function") {
      window.AkanaChat.submitAnswerText(t);
      return;
    }
    submitApprovalReply(t);
  }

  /** Send the plan decision as the next user message → `--resume` tells Claude to
   *  apply or revise the plan. keepPlanning=true (Revise) keeps plan mode ON
   *  (re-plan); false (Apply) turns it OFF (apply the plan). Falls back to the
   *  composer/form path if the AkanaChat general API is unavailable (symmetric
   *  with askUser). */
  function submitPlanReply(text, opts = {}) {
    const t = String(text || "").trim();
    if (!t) return;
    if (typeof window.AkanaChat?.submitPlanText === "function") {
      window.AkanaChat.submitPlanText(t, { keepPlanning: opts.keepPlanning === true });
      return;
    }
    submitApprovalReply(t);
  }

  /** Send the approval reply through the existing composer/form (without inventing
   *  a new endpoint). Silently no-ops if the form is absent (the card still
   *  resolves visually).
   *  TODO(backend): wire up the structured approval endpoint when it arrives. */
  function submitApprovalReply(text) {
    const form = chatCtx.hooks.form;
    const msg = chatCtx.hooks.msg;
    if (msg && form && typeof form.requestSubmit === "function") {
      msg.value = text;
      try {
        msg.dispatchEvent(new Event("input", { bubbles: true }));
      } catch {
        /* ignore */
      }
      form.requestSubmit();
    }
  }

  function finalizeThoughtFeed(streamCtx, doneMeta) {
    const feed = streamCtx.thoughtFeed;
    if (!feed || feed.dataset.finalized === "1") return;
    feed.dataset.finalized = "1";
    feed.classList.remove("is-live", "is-running");
    feed.classList.add("is-done");
    const body = feed.querySelector(".akana-thought-feed-body");
    // Stream ended/cancelled: if a tool is aborted while still "running" the final
    // upsert that would flip the card to done never arrives, leaving the "Cancel"
    // row orphaned in the DOM → clean it up on every finalize.
    body?.querySelectorAll(".aur-tool-cancel-row").forEach((r) => r.remove());
    // Same for a subagent group stuck "running" (its own `end` event never arrived,
    // e.g. cancelled mid-subagent) — freeze the elapsed-time ticker so it doesn't
    // keep firing forever on a finalized (dead) turn.
    body?.querySelectorAll('.aur-subagent-group[data-status="running"]').forEach((g) => {
      g.dataset.status = "done";
      const headEl = g.querySelector(":scope > .aur-subagent-head");
      const stEl = headEl ? headEl.querySelector(":scope > .aur-subagent-status") : null;
      if (stEl) window.AkanaChatRender?.setStatusIcon?.(stEl, "done");
      if (g._aurTicker) {
        window.clearInterval(g._aurTicker);
        g._aurTicker = null;
      }
    });
    const lines = body ? body.querySelectorAll(".akana-thought-line").length : 0;
    const tools = body ? body.querySelectorAll(".tool-call").length : 0;
    // If there are no thinking lines AND no tool cards, hide the process card entirely.
    if (!lines && !tools) {
      feed.remove();
      streamCtx.thoughtFeed = null;
      return;
    }
    body?.querySelector(".akana-thought-line--thinking-current")?.classList.remove(
      "akana-thought-line--thinking-current",
    );
    // Combined header: "Thought in N steps · M tools · X.X s".
    const steps = body
      ? body.querySelectorAll(
          ".akana-thought-line--think, .akana-thought-line--step, .akana-thought-line--summary, .akana-thought-line--note",
        ).length
      : 0;
    const parts = [];
    if (steps) parts.push(window.AkanaI18n.t("transport.process.thought_n", { n: steps }));
    if (tools) parts.push(window.AkanaI18n.t("transport.process.tool_n", { n: tools }));
    if (!parts.length) parts.push(window.AkanaI18n.t("transport.process.label"));
    const lbl = feed.querySelector(".aur-process-label");
    if (lbl) lbl.textContent = parts.join(" · ");
    const sub = feed.querySelector(".aur-process-sub");
    if (sub) {
      const ms = doneMeta && typeof doneMeta.latency_ms === "number" ? doneMeta.latency_ms : null;
      const elapsed = ms != null ? ms : Math.max(0, _now() - (streamCtx.processStartedAt || _now()));
      sub.textContent =
        elapsed < 1000 ? `${Math.round(elapsed)} ms` : `${(elapsed / 1000).toFixed(1)} sn`;
    }
    // Default COLLAPSED; but if the user left this turn's panel OPEN
    // (turn_id preference saved) finalize it open — F5/resume preserves the pref.
    const tid = streamCtx.turnId || feed.closest?.(".row")?.dataset?.turnId;
    const pref = tid ? window.AkanaChatRender?.getPanelCollapsed?.(tid) : null;
    if (pref === false) feed.classList.remove("is-collapsed");
    else feed.classList.add("is-collapsed");
    // Toggle is wired to the head ONLY — clicking tool cards inside the body
    // (expand/collapse) must not close the process card.
    const headEl = feed.querySelector(".akana-thought-feed-head");
    if (headEl && headEl.dataset.toggleWired !== "1") {
      headEl.dataset.toggleWired = "1";
      headEl.style.cursor = "pointer";
      headEl.addEventListener("click", () => {
        feed.classList.toggle("is-collapsed");
        persistFeedCollapse(streamCtx, feed);
      });
    }
  }

  function handleThinkingEvent(streamCtx, payload) {
    const phase = payload.phase || "delta";
    if (phase === "completed") {
      const body = streamCtx.thoughtBody || streamCtx.thoughtFeed?.querySelector(".akana-thought-feed-body");
      body?.querySelector(".akana-thought-line--thinking-current")?.classList.remove(
        "akana-thought-line--thinking-current",
      );
      return;
    }
    appendThinkingDelta(streamCtx, payload.text || "");
  }

  function handleActivityEvent(streamCtx, payload) {
    const kind = payload.kind || "status";
    if (kind === "heartbeat") return;
    const text = String(payload.text || "").trim();
    if (kind === "shell") {
      const chunk = text.replace(/\s+$/, "");
      if (!chunk) return;
      const body = ensureThoughtFeed(streamCtx).querySelector(".akana-thought-feed-body");
      let cur = body.querySelector(".akana-thought-line--shell-current");
      if (!cur) {
        cur = document.createElement("div");
        cur.className = "akana-thought-line akana-thought-line--shell akana-thought-line--shell-current";
        cur.textContent = "▸ ";
        body.appendChild(cur);
      }
      cur.textContent = (cur.textContent || "▸ ") + chunk;
      if (cur.textContent.length > 2400) {
        cur.classList.remove("akana-thought-line--shell-current");
      }
      trimThoughtFeed(body);
      chatCtx.hooks.stickToBottomIfFollowing?.(streamCtx.scroller);
      return;
    }
    if (kind === "summary") {
      appendThoughtFeedLine(streamCtx, text || (payload.phase === "start" ? window.AkanaI18n.t("transport.activity.summary_start") : window.AkanaI18n.t("transport.activity.summary_done")), "summary");
      return;
    }
    if (kind === "step") {
      appendThoughtFeedLine(streamCtx, text || (payload.phase === "start" ? window.AkanaI18n.t("transport.activity.step_start") : window.AkanaI18n.t("transport.activity.step_done")), "step");
      return;
    }
    if (text) appendThoughtFeedLine(streamCtx, text, "note");
  }

  /** AGENT ACTIVITY (Batch 1): turn-level TODO progress. The checklist card itself
   *  still renders via the generic `tool_call` (TodoWrite) → this only drives the
   *  compact "N/M tasks" pill in the process head and pins the checklist to the top
   *  of the timeline so the plan stays visible. Best-effort: if the card has not been
   *  flushed yet (tool cards flush on rAF), a later todo event pins it. */
  function handleTodoEvent(streamCtx, todo) {
    const items = todo && Array.isArray(todo.items) ? todo.items : [];
    const feed = ensureThoughtFeed(streamCtx);
    const head = feed.querySelector(".aur-process-head");
    if (head) {
      let pill = head.querySelector(".aur-process-todo");
      if (!pill) {
        pill = document.createElement("span");
        pill.className = "aur-process-todo";
        pill.setAttribute("aria-hidden", "true");
        const chev = head.querySelector(".aur-process-chev");
        if (chev) head.insertBefore(pill, chev);
        else head.appendChild(pill);
      }
      const total = items.length;
      const done = items.filter((it) => it && it.status === "completed").length;
      if (total) {
        pill.textContent = window.AkanaI18n.t("transport.process.tasks_n", {
          done,
          total,
        });
        pill.dataset.complete = done >= total ? "1" : "0";
        pill.hidden = false;
      } else {
        pill.hidden = true;
      }
    }
    // Pin the plan checklist to the top of the timeline (idempotent — only a
    // TOP-LEVEL todo card, never a subagent's nested one).
    const body = streamCtx.thoughtBody;
    const card = body && body.querySelector(':scope > [data-todo-card="1"]');
    if (card && body.firstChild !== card) body.insertBefore(card, body.firstChild);
    refreshProcessHead(streamCtx);
  }

  /** AGENT ACTIVITY (Batch 1): Claude subagent (Task) boundary. `start` opens a
   *  labelled group in the timeline; the subagent's nested tool cards fall inside it
   *  via `parent_id` (see upsertToolCardIntoTimeline). `end` flips the group state. */
  function handleSubagentEvent(streamCtx, sub) {
    if (!sub || !sub.id) return;
    const body = ensureThoughtFeed(streamCtx).querySelector(
      ".akana-thought-feed-body",
    );
    if (!body) return;
    const isStart = (sub.phase || "start") === "start";
    // Opening a new group is a NEW timeline entry → close any open thought/shell line
    // (same contract as a fresh tool card) so the group starts on its own row.
    if (isStart) closeOpenThoughtLines(body);
    window.AkanaChatRender?.upsertSubagentGroup?.(body, sub);
    bumpProcessCounts(streamCtx);
    chatCtx.hooks.stickToBottomIfFollowing?.(streamCtx.scroller);
  }

  /** AGENT ACTIVITY (Batch 2): streaming tool INPUT. Claude streams a tool's input
   *  JSON in chunks (`tool_call_delta {id, name, partial}`) BEFORE the full
   *  `tool_call` (start) lands. Accumulate the chunks per tool id and stream them
   *  into the (running) tool card's subtitle so the input is visible building live;
   *  the real tool_call then patches the SAME card (by id) and the preview gives way
   *  to the final args. Degrades gracefully: without an id (can't correlate) it's a
   *  no-op — the full tool_call still renders the card. */
  function handleToolCallDeltaEvent(streamCtx, d) {
    if (!d || !d.id || typeof d.partial !== "string" || !d.partial) return;
    const buf = (streamCtx.toolInputStream = streamCtx.toolInputStream || {});
    // Bound the accumulator (huge inputs must not grow the buffer unboundedly; the
    // preview only shows the tail anyway).
    buf[d.id] = (buf[d.id] || "").slice(-8000) + d.partial;
    const feed = ensureThoughtFeed(streamCtx);
    const body =
      streamCtx.thoughtBody || feed.querySelector(".akana-thought-feed-body");
    if (!body) return;
    let existed = false;
    try {
      existed = !!body.querySelector(
        `[data-tool-call-id="${CSS.escape(String(d.id))}"]`,
      );
    } catch {
      /* invalid id selector → treat as new */
    }
    const node = window.AkanaChatRender?.upsertToolInputStream?.(body, {
      id: d.id,
      name: d.name,
      text: buf[d.id],
    });
    // A newly-created card is a fresh timeline entry → close any open thought/shell
    // line (same contract as a normal tool card entering the timeline).
    if (node && !existed) closeOpenThoughtLines(body);
    bumpProcessCounts(streamCtx);
    chatCtx.hooks.stickToBottomIfFollowing?.(streamCtx.scroller);
  }

  /** Tag the live/resume row with the turn's id and remove any OTHER rows with the
   *  same id (stale copies left by F5 restore). Double-render guard: when resume
   *  re-renders an active turn, restore may have already drawn the same turn,
   *  causing a "writing twice" bug. The live row (full of tools + latency) survives;
   *  the stale copy is removed.
   *
   *  INVARIANT (intra-bubble double-render): This function operates at ROW (.row)
   *  level — it removes ALL excess rows with the same turn_id, leaving exactly one;
   *  it NEVER moves a bubble's content into another bubble. Therefore the surviving
   *  row's bubble contains exactly one streamCtx.acc, and that acc is unique via the
   *  Parts-B/C guard (server-authoritative or canonical record). Result: a
   *  half-duplicated single bubble is structurally impossible. */
  function tagStreamRowTurnId(streamCtx, turnId) {
    if (!turnId || !streamCtx || !streamCtx.msgBody) return;
    const row = streamCtx.msgBody.closest?.(".row") || streamCtx.msgBody.parentElement;
    if (!row) return;
    row.dataset.turnId = String(turnId);
    const log = chatCtx.hooks.log;
    if (!log || typeof log.querySelectorAll !== "function") return;
    let sel;
    try {
      sel = `.row[data-turn-id="${CSS.escape(String(turnId))}"]`;
    } catch {
      return;
    }
    for (const other of log.querySelectorAll(sel)) {
      if (other !== row) other.remove();
    }
  }

  // Adopt the server-side conv id for the stream. Always updates the stream's OWN
  // identity (streamCtx.convId); but changes the GLOBAL active-conv (setConversationId →
  // visible log + archive + active-thread) ONLY if this stream is in the FOREGROUND.
  // Foreground = the stream of the DISPLAYED chat (isForegroundStream). During
  // CONCURRENT sends to another chat the background stream's meta/done must NOT yank
  // the visible chat to its own conv (root of cross-conv DOM/state desync — FE bug #18
  // conv half).
  function adoptStreamConversationId(streamCtx, convId) {
    if (!convId) return;
    streamCtx.convId = convId;
    // Move the stream's record to the real conv id (anon→id): per-conv STOP/reconcile
    // and foreground comparison now work with the correct key.
    rekeyStream(streamCtx, convId);
    if (!isForegroundStream(streamCtx)) return; // background stream → do not touch global
    // PARALLEL-CHAT: BIND the displayed pane to this conv — when the first/default chat
    // learns its server id from the stream it transitions from the empty-sentinel pane
    // to the real conv-id; without this, showConversation would open a blank new pane
    // on return ("transitions before refresh were buggy" root cause).
    chatCtx.rekeyDisplayedPane?.(convId);
    chatCtx.setConversationId(convId);
  }

  function handleChatStreamEvent(f, streamCtx) {
    let payload;
    try {
      payload = JSON.parse(f.data);
    } catch {
      return;
    }
    const { meta, bubble, msgBody, scroller, insertBeforeBubble } = streamCtx;
    if (f.event === "meta") {
      streamCtx.turnId = payload.turn_id;
      tagStreamRowTurnId(streamCtx, payload.turn_id);
      applyFeedCollapsePref(streamCtx);
      if (payload.conversation_id) {
        adoptStreamConversationId(streamCtx, payload.conversation_id);
      }
      const intentTag = payload.intent === "system_action" ? " · system" : "";
      const approvalTag = payload.approval_required ? window.AkanaI18n.t("transport.approval.meta_tag") : "";
      meta.textContent = `Akana${intentTag}${approvalTag}`;
      // In approval mode, render the inline approval card (.approval).
      maybeRenderApprovalCard(streamCtx, payload);
    } else if (f.event === "status") {
      const phase = payload.phase || "";
      // FOREGROUND GATE: the strip is a singleton reflecting the DISPLAYED conversation's
      // turn — a background stream's status must not overwrite it (parallel-chat).
      if (isForegroundStream(streamCtx)) {
        if (phase === "preparing") window.AkanaTurnStatus?.setPhase("preparing");
        else if (phase === "model") window.AkanaTurnStatus?.setPhase("connecting");
      }
    } else if (f.event === "thinking") {
      if (isForegroundStream(streamCtx)) window.AkanaTurnStatus?.setPhase("thinking");
      handleThinkingEvent(streamCtx, payload);
    } else if (f.event === "activity") {
      handleActivityEvent(streamCtx, payload);
    } else if (f.event === "todo") {
      // Batch 1: turn-level TODO progress (from TodoWrite). Additive — the checklist
      // card still renders via the generic tool_call; this drives the head pill.
      handleTodoEvent(streamCtx, payload);
    } else if (f.event === "subagent") {
      // Batch 1: Claude subagent (Task) start/end boundary → timeline group.
      handleSubagentEvent(streamCtx, payload);
    } else if (f.event === "tool_call_delta") {
      // Batch 2: streaming tool INPUT → stream the partial into the tool card live.
      handleToolCallDeltaEvent(streamCtx, payload);
    } else if (f.event === "delta") {
      // A late delta arriving after done must not overwrite the final text.
      if (streamCtx.doneMeta || streamCtx.serverError) return;
      const piece = payload.text || "";
      if (!piece) return;
      // DOUBLE-RENDER GUARD: if this turn_id has already been finalised in ANOTHER
      // follower (canonical single text recorded), this delta sequence is a replay
      // from index 0 → do NOT append to this bubble's acc, REPLACE with the canonical
      // text. Otherwise the same answer would be doubled (FE bug: double in one bubble,
      // unbroken concat). If this row's acc is filling for the first time, start from
      // canonical; if already full, overwrite.
      const finalizedText =
        streamCtx.turnId ? _turnFinalText.get(String(streamCtx.turnId)) : null;
      if (finalizedText) {
        streamCtx.acc = finalizedText;
        bubble.classList.remove("bubble-bot-pending");
        bubble.removeAttribute("aria-busy");
        // Resume/dual-follower: canonical text is shown in ONE shot (no typewriter —
        // rewriting is pointless). Stop the running reveal loop + catch up.
        stopStreamReveal(streamCtx);
        ensureMdScratch(streamCtx).shown = streamCtx.acc.length;
        setBubbleMarkdown(bubble, streamCtx.acc);
        scheduleStreamScroll(streamCtx, scroller);
        return;
      }
      const isFirst = streamCtx.acc.length === 0;
      streamCtx.acc += piece;
      // Aurora voice scene shows the response live (fullscreen overlay hides the chat
      // log; the scene is fed from AkanaBus). Send the accumulated FULL text.
      // Only the FOREGROUND stream feeds it (symmetric with chat:stream:start + TTS
      // audio gate, see isForegroundStream) → a background chat's delta must NOT
      // overwrite the wrong chat's/voice scene's text (cross-conv scene leak).
      if (isForegroundStream(streamCtx)) {
        try {
          window.AkanaBus?.emit?.("chat:stream:delta", { text: streamCtx.acc });
        } catch {
          /* ignore */
        }
      }
      if (isFirst) {
        // Mid-stream (not yet done) → no status chip.
        setStreamMetaText(meta, formatAssistantStreamMeta(streamCtx.turnId, null));
      }
      bubble.classList.remove("bubble-bot-pending");
      bubble.removeAttribute("aria-busy");
      if (!streamCtx.toolPhaseActive && isForegroundStream(streamCtx)) {
        window.AkanaTurnStatus?.setPhase("writing");
      }
      // LIVE markdown — TIME-throttled (not per-frame). **bold**/code/tables
      // are formatted while streaming; render rate capped at ~8/s.
      // HARD FREEZE GUARANTEE: the throttle re-parses the full text O(N) on every
      // render; for VERY LONG answers (autonomous research replies + many tool cards)
      // even a single render can exceed the frame budget and freeze. Once the threshold
      // (LIVE_MD_MAX) is crossed, ABANDON live markdown and switch to plain-text append
      // (O(1) — freeze is structurally impossible); the final FULL markdown render
      // happens at done.
      if (streamCtx.plainStreamMode) {
        appendBubbleStreamText(bubble, piece);
        scheduleStreamScroll(streamCtx, scroller);
      } else if (streamCtx.acc.length > LIVE_MD_MAX) {
        streamCtx.plainStreamMode = true;
        resetStreamMdThrottle(streamCtx); // also stops the reveal loop
        appendBubbleStreamText(bubble, streamCtx.acc);
        scheduleStreamScroll(streamCtx, scroller);
      } else {
        // SMOOTH REVEAL: instead of dumping the full acc on every delta, advance
        // `shown` per-frame and draw acc.slice(0, shown) → bursts spread smoothly.
        if (isFirst) {
          // Show the first slice IMMEDIATELY to prevent an empty-bubble wait.
          const sc = ensureMdScratch(streamCtx);
          sc.shown = Math.min(streamCtx.acc.length, REVEAL_MIN_STEP);
          sc.mdLast = _now();
          setBubbleMarkdown(bubble, streamCtx.acc.slice(0, sc.shown), { streaming: true });
          scheduleStreamScroll(streamCtx, scroller);
        }
        scheduleStreamReveal(streamCtx, bubble, scroller);
      }
    } else if (f.event === "memory_use") {
      const memNode = renderMemoryUse(payload);
      if (memNode) insertBeforeBubble(memNode);
      // Collect sources for the RESOURCES row (rendered at done).
      if (Array.isArray(payload.items) && payload.items.length) {
        streamCtx.memorySources = (streamCtx.memorySources || []).concat(payload.items);
      }
      chatCtx.hooks.stickToBottomIfFollowing(scroller);
    } else if (f.event === "tool_call") {
      queueToolCall(streamCtx, payload.call || {}, scroller);
      syncTurnStatusTool(payload.call || {}, streamCtx);
      // Aurora voice scene: reflect the tool card too (fullscreen overlay hides the
      // chat log → tool cards must appear in the scene). Send the raw call;
      // the overlay derives the same action sentence via AkanaChatRender helpers.
      try {
        window.AkanaBus?.emit?.("voice:tool", { call: payload.call || {} });
      } catch {
        /* ignore */
      }
    } else if (f.event === "ask_user") {
      // Claude (headless) asked a structured question → interactive card.
      // The answer becomes the next user message (`--resume`). Bubble stays empty;
      // the empty-answer error at done is suppressed via askUserShown.
      maybeRenderAskUserCard(streamCtx, payload.question || payload.ask_user || payload);
    } else if (f.event === "plan_review") {
      // Claude (plan-mode / ExitPlanMode) presented a plan → approval card. "Apply"/
      // "Revise" becomes the next user message (`--resume`). Bubble stays empty;
      // the empty-answer error at done is suppressed via planShown.
      maybeRenderPlanCard(streamCtx, payload.plan || payload.plan_review || payload);
    } else if (f.event === "usage") {
      // LIVE USAGE (SSE contract 1): backend sends {prompt, completion, cost_usd?}
      // during the stream → update the meta line LIVE.
      // When done arrives, done.tokens overwrites with the correct total_cost_usd.
      if (payload && typeof payload.prompt === "number") {
        streamCtx.liveUsage = {
          prompt: Number(payload.prompt) || 0,
          completion: Number(payload.completion) || 0,
          cost_usd: typeof payload.cost_usd === "number" ? payload.cost_usd : undefined,
        };
        // Only show the live value if doneMeta has not arrived yet — done will write
        // the definitive value and must not be overwritten.
        if (!streamCtx.doneMeta) {
          const liveDoneMeta = {
            tokens: streamCtx.liveUsage,
            // latency_ms not yet known → omit (comes with done).
          };
          // Mid-stream (not yet done) → no status chip.
          setStreamMetaText(
            streamCtx.meta,
            formatAssistantStreamMeta(streamCtx.turnId, liveDoneMeta),
          );
          // Turn HUD (Feature 3): richer live pill (count-up + tok/s + cost) next
          // to the meta line, gated by the same usageDisplayEnabled() setting.
          updateTurnHud(streamCtx, streamCtx.liveUsage);
        }
      }
    } else if (f.event === "tts_chunk") {
      // Only the FOREGROUND (displayed) stream plays audio: during a concurrent turn
      // to another chat, the background stream's TTS chunk must not overlay the audio
      // of the currently-heard turn (cross-conv audio leak). No-op in single-stream
      // voice mode.
      // ABORT GATE: if this stream was cancelled (barge-in / STOP / new turn →
      // abortActiveChatStream) late frames that buffered before the last successful
      // read drain here via the catch's `await flushSseQueue()` AFTER
      // streamCtx.aborted=true. Do NOT play audio from an aborted stream — otherwise
      // the cancelled answer re-speaks over the new listening turn.
      if (payload.audio_b64 && !streamCtx.aborted && isForegroundStream(streamCtx)) {
        // GEN GATE (defence in depth): on the FIRST tts frame of this stream, capture
        // the ttsPlayer's current accept-gen and carry it with all subsequent chunks.
        // reset() (every cancel path) increments accept-gen → late frames from this
        // stream are now OLD-generation and ttsPlayer.enqueue drops them.
        // The race where aborted hasn't been written to streamCtx yet (done-path read
        // buffered before the abort) is also closed by this generation gate.
        if (streamCtx._ttsAcceptGen == null) {
          streamCtx._ttsAcceptGen = chatCtx.hooks.ttsPlayer?.acceptGen?.() ?? null;
        }
        void chatCtx.hooks.ttsPlayer?.enqueue(
          payload.audio_b64,
          payload.mime,
          streamCtx._ttsAcceptGen,
        );
      }
    } else if (f.event === "tts_error") {
      console.warn("TTS error:", payload);
    } else if (f.event === "tts_end") {
      // Backend signalled "no more audio chunks" → the audio module re-opens
      // listening only after this + the queue draining (prevents early-mic-open /
      // hearing-yourself bug). Only sent during voice turns.
      // b5: FOREGROUND-GATED — a BACKGROUND (non-voice) stream's tts_end must NOT re-arm the mic /
      // end the foreground voice turn. EXCEPTION: THIS stream's OWN voiceTurn tts_end always clears
      // its latch (balances streamOpen) even if no longer foreground — single-conversation voice.
      if (isForegroundStream(streamCtx) || streamCtx.voiceTurn) {
        try {
          window.AkanaBus?.emit?.("voice:tts:streamEnd", {});
        } catch {
          /* ignore */
        }
      }
    } else if (f.event === "done") {
      streamCtx.doneMeta = payload;
      // First place ALL tool cards in the timeline (queued ones + fast-path done list),
      // THEN finalise the process card → head count + collapse sees the full card set.
      flushToolCallUpdates(streamCtx, scroller);
      if (Array.isArray(payload.tool_calls) && payload.tool_calls.length) {
        const alreadyShown = msgBody.querySelectorAll(".tool-call, .memory-use").length;
        if (!alreadyShown) {
          // No live tool_call events arrived (e.g. fast-path) — build cards from
          // the done list into the timeline.
          for (const c of payload.tool_calls) {
            upsertLiveToolCard(streamCtx, c);
          }
        }
      }
      finalizeThoughtFeed(streamCtx, payload);
      if (payload.turn_id) streamCtx.turnId = payload.turn_id;
      tagStreamRowTurnId(streamCtx, streamCtx.turnId);
      // Keep tool cards after F5: persist the turn_id → tool_calls mapping.
      // The server /messages endpoint does not return this; restore via
      // mapServerMessagesToThread re-attaches it by turn_id. Even if the done
      // payload is empty we recover from live-accumulated cards (toolCallsToPersist).
      const persistCalls = toolCallsToPersist(streamCtx, payload);
      if (streamCtx.turnId && persistCalls.length) {
        window.AkanaChatRender?.putToolCallsForTurn?.(streamCtx.turnId, persistCalls);
      }
      // DOM-based tool count (accurate/deduplicated — see formatAssistantStreamMeta's
      // toolCountOverride doc) now that the process card is finalized above.
      const doneToolCount = msgBody.querySelectorAll(".tool-call").length;
      setStreamMetaText(
        meta,
        formatAssistantStreamMeta(streamCtx.turnId, payload, doneToolCount),
        "ok",
      );
      // Turn HUD (Feature 3): the final tally is now in the meta line above —
      // the live pill is redundant, fold it away.
      removeTurnHud(streamCtx);
      flushStreamMarkdownUpdate(streamCtx);
      // Cancel the live throttle timer — a late render after done must not
      // overwrite the final text.
      resetStreamMdThrottle(streamCtx);
      bubble.classList.remove("bubble-bot-pending");
      bubble.removeAttribute("aria-busy");
      // AskUserQuestion: the question is also carried in done (fast-path / if the
      // live event was missed). Set up the card here (if not yet present) —
      // askUserShown prevents double-render. A question turn returns empty text;
      // the empty-answer error below is suppressed by this flag (question ≠ empty
      // answer).
      if (payload.ask_user && typeof payload.ask_user === "object") {
        maybeRenderAskUserCard(streamCtx, payload.ask_user);
      }
      // Plan-mode (ExitPlanMode): the plan is also carried in done (if the live
      // plan_review event was missed). Set up the card here (if not yet present) —
      // planShown prevents double-render. A plan turn returns empty text; the
      // empty-answer error is suppressed by this flag.
      // CONFLICT GUARD (SSE contract 3): if ask_user is present, SKIP the plan card —
      // ask_user wins (the backend applies the same rule; showing both simultaneously
      // results in two conflicting cards).
      const hasAskUser = (payload.ask_user && typeof payload.ask_user === "object") ||
        streamCtx.askUserShown;
      if (!hasAskUser && payload.plan_review && typeof payload.plan_review === "object") {
        maybeRenderPlanCard(streamCtx, payload.plan_review);
      }
      // DOUBLE-RENDER GUARD (final): if server text is present it is AUTHORITATIVE
      // and UNIQUE — we NEVER concat acc (which a dual-follower may have inflated);
      // we write the server text directly. If this turn_id was previously finalised
      // in another follower, prefer the canonical record (even if this bubble's acc
      // is stale/doubled, the canonical single text wins). Priority: record >
      // server text > acc.
      // NOTE: trim whitespace (old behaviour: trimmed selection) — if trim gives
      // empty, fall back to the raw value (whitespace-only text must not be deleted).
      //
      // POST-INLINE-CARD BUBBLE: sealBubbleAndAppendCard may update streamCtx.bubble
      // (a fresh bubble is opened when a card arrives). Final text and error paths
      // must work with the CURRENT bubble — otherwise they would write to the old
      // (sealed) bubble again.
      const liveBubble = streamCtx.bubble;
      const serverText =
        typeof payload.text === "string" && payload.text.trim() ? payload.text.trim() : "";
      const finalizedText =
        streamCtx.turnId ? _turnFinalText.get(String(streamCtx.turnId)) : null;
      const finalText =
        finalizedText || serverText || (streamCtx.acc || "").trim() || streamCtx.acc || payload.text || "";
      // INLINE CARD PREFIX-STRIP: if an ask_user/plan card arrived, the preamble
      // text was written to the PREVIOUS (sealed) bubble; `liveBubble` is now the
      // post-card bubble. finalText is the FULL turn text (including preamble) →
      // strip the sealed prefix, otherwise the preamble is drawn AGAIN below the
      // card (double-text bug). If there is no card, sealedText is empty →
      // no stripping, full text is written.
      const liveText = stripSealedPrefix(finalText, streamCtx.sealedText);
      if (finalText) {
        streamCtx.acc = finalText;
        // Store the canonical single text by turn_id → any resume/follower that
        // replays this turn from index 0 REPLACES instead of appending.
        rememberTurnFinalText(streamCtx.turnId, finalText);
      }
      if (liveText) {
        // Write ONLY the post-seal remainder to the post-card bubble (not the preamble).
        setBubbleMarkdown(liveBubble, liveText);
        scheduleStreamScroll(streamCtx, scroller);
      } else if (
        streamCtx.askUserShown || payload.ask_user ||
        streamCtx.planShown || payload.plan_review
      ) {
        // Question/plan turn, NO post-card text: remove the empty live bubble (the
        // card sits AFTER the sealed preamble bubble → bubble must not be left orphaned).
        liveBubble.remove();
        streamCtx.bubbleRemoved = true;
      } else if (
        !finalText &&
        !streamCtx.askUserShown && !payload.ask_user &&
        !streamCtx.planShown && !payload.plan_review
      ) {
        liveBubble.classList.add("bubble-bot-err");
        // Stream `done` arrived but no text at all (neither delta nor done.text) →
        // the model genuinely returned empty. Provider-neutral, honest message: the
        // old text incorrectly blamed "CURSOR_API_KEY" regardless of provider.
        // NOTE: AskUserQuestion turns also return empty text but that is NOT an error
        // (the model asked the user a question) → exempted via askUserShown/ask_user.
        liveBubble.textContent = window.AkanaI18n.t("transport.stream.empty_response");
      }
      // Notify the Aurora scene of the final answer (in voice mode the RESPONDING text
      // is frozen). Symmetric with delta/start: ONLY the foreground stream feeds the
      // scene → when a background chat finishes, its text must NOT bleed into the voice
      // scene (R4-A #3).
      if (isForegroundStream(streamCtx)) {
        try {
          window.AkanaBus?.emit?.("chat:stream:done", { text: streamCtx.acc || "" });
        } catch {
          /* ignore */
        }
      }
      if (payload.dropped_turns && payload.dropped_turns > 0) {
        const warn = document.createElement("div");
        warn.className = "history-warn";
        warn.textContent = window.AkanaI18n.t("transport.stream.history_drop", { n: payload.dropped_turns });
        insertBeforeBubble(warn);
      }
      if (payload.context_mode === "bootstrap_retry") {
        const info = document.createElement("div");
        info.className = "history-warn";
        const n = Number(payload.history_bootstrap_turns) || 0;
        info.textContent =
          n > 0
            ? window.AkanaI18n.t("transport.stream.session_renewed_n", { n })
            : window.AkanaI18n.t("transport.stream.session_renewed");
        insertBeforeBubble(info);
      }
      if (Array.isArray(payload.skill_used) && payload.skill_used.length) {
        const skillNode = window.AkanaChatRender.renderSkillUse?.(payload.skill_used);
        if (skillNode && !msgBody.querySelector(".skill-use")) insertBeforeBubble(skillNode);
      }
      const _mw = payload.memory_writes || [];
      const _mwKeys = (arr) =>
        arr.map((w) => w.key || "bilgi").filter(Boolean).slice(0, 3).join(", ");
      const staged = _mw.filter((w) => w && w.kind === "staging");
      if (staged.length) {
        chatCtx.hooks.showToast(window.AkanaI18n.t("transport.toast.memory_staged", { keys: _mwKeys(staged) }), "success");
        // Live Inbox badge on the Memory nav button (akana-memory-studio.js).
        window.AkanaBus?.emit?.("memory:staged", { count: staged.length });
      }
      // Items captured while "remember without approval" is on are permanently stored
      // directly (promoted) → "saved" (no inbox/approval). Previously kind="stored"
      // was filtered out and NO feedback was emitted.
      const stored = _mw.filter((w) => w && w.kind === "stored");
      if (stored.length) {
        chatCtx.hooks.showToast(window.AkanaI18n.t("transport.toast.memory_stored", { keys: _mwKeys(stored) }), "success");
      }
      // RESOURCES row (.cites): coloured source chips BELOW the bubble. Only from
      // backend data (memory_use items + staging writes) — row is not added if there
      // is no data (no fabrication).
      maybeRenderSourcesRow(streamCtx, payload);
      finalizeStreamUi(streamCtx);
    } else if (f.event === "error") {
      streamCtx.serverError = payload;
      finalizeThoughtFeed(streamCtx);
      removeTurnHud(streamCtx);
      maybeRenderErrorCard(streamCtx, payload);
      finalizeStreamUi(streamCtx);
    }
  }

  /** Error card (.err-card): red-toned card for pack/permission errors with
   *  "Retry" / "Open pack settings". Only added when a meaningful message is present;
   *  otherwise the existing history-warn path (consumeSseResponse) handles it. Retry
   *  uses the existing send path (writes the user's last text to the composer and
   *  submits); no new endpoint is invented. */
  function maybeRenderErrorCard(streamCtx, payload) {
    const render = window.AkanaChatRender?.renderErrorCard;
    if (typeof render !== "function") return;
    const { msgBody } = streamCtx;
    if (!msgBody || msgBody.querySelector(".aur-err-card")) return;
    const code = String((payload && payload.code) || "").trim();
    const message = String((payload && (payload.message || payload.detail)) || "").trim();
    if (!code && !message) return; // no meaningful information → leave to default path
    const haystack = `${code} ${message}`.toLowerCase();
    const isPack = /pack|yetki|permission|oauth|403|forbidden|token/.test(haystack);
    const opts = {
      title: isPack ? window.AkanaI18n.t("transport.err_card.pack_title") : window.AkanaI18n.t("transport.err_card.generic_title"),
      detail: message || code || "Unknown error.",
    };
    const userText = (streamCtx.userText || "").trim();
    if (userText) {
      opts.onRetry = () => submitApprovalReply(userText);
    }
    if (isPack && chatCtx.hooks.openSettings) {
      opts.secondaryLabel = window.AkanaI18n.t("transport.err_card.pack_settings");
      opts.onSecondary = () => chatCtx.hooks.openSettings?.("packs");
    }
    const card = render(opts);
    if (card) {
      streamCtx.insertBeforeBubble(card);
      streamCtx.errorCardShown = true;
      chatCtx.hooks.stickToBottomIfFollowing?.(streamCtx.scroller);
    }
  }

  function cleanupStreamRow(streamCtx) {
    finalizeThoughtFeed(streamCtx);
    removeTurnHud(streamCtx);
    finalizeStreamUi(streamCtx);
    const { bubble } = streamCtx;
    if (bubble) {
      bubble.classList.remove("bubble-bot-pending");
      bubble.removeAttribute("aria-busy");
    }
  }

  function parseSseFrames(buf) {
    const frames = [];
    let rest = buf;
    while (true) {
      const sep = rest.indexOf("\n\n");
      if (sep < 0) break;
      const block = rest.slice(0, sep);
      rest = rest.slice(sep + 2);
      let evt = "message";
      let dat = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) evt = line.slice(6).trim();
        else if (line.startsWith("data:")) dat = (dat ? dat + "\n" : "") + line.slice(5).trim();
      }
      if (dat) frames.push({ event: evt, data: dat });
    }
    return { frames, rest };
  }

  // ── CONCURRENT N-STREAM REGISTRY (per-conversation stream contexts) ──────────
  // DECISION: when a new stream starts we do NOT forcibly abort the previous one.
  // N chats can stream simultaneously (different conversations; a second message in
  // the same conversation is queued on the server with 202) — each stream's state is
  // isolated in streamCtx (see ensureToolScratch + resetSseQueue(streamCtx)), so
  // concurrent streams do not interfere with each other.
  // PREVIOUSLY a single global `activeStreamAbort`/`liveStreamCtx` existed → when a
  // second chat started the FIRST's abort/pointer was OVERWRITTEN: A fell into an
  // invisible follower (STOP only cut the last one), the foreground UI was gated on
  // the wrong stream.
  // NOW each conv's stream has its OWN record: `_streamsByConv` (convId → record).
  // Record = { abort, streamCtx }. For anonymous streams (no conv_id yet) a symbol
  // key is generated; when meta/done brings a conv_id the record is re-keyed to it.
  const _streamsByConv = new Map(); // convId|symbol → { abort, streamCtx }

  // DISPLAYED conversation — determines which stream gates the foreground UI
  // (composer SEND↔STOP / "Responding" bar / orb / chatStreaming flag / TTS /
  // global active-conv adoption). NOT "the most recently started stream"; rather,
  // the conversation the user is looking at. Updated by setForegroundConversation
  // on chat-threads switch/restore. Every foreground gate checks this value so
  // that when a background stream (concurrent send to another chat) finishes, the
  // visible chat's composer does not prematurely flip to SEND / close "Responding"
  // (replaces the old liveStreamCtx comparison with isForegroundStream).
  let foregroundConvId = null;
  let _streamSeq = 0; // incrementing counter for anonymous (no conv_id yet) stream keys

  /** Register a stream's conv / re-key it (anonymous → real id transition). */
  function registerStream(streamCtx, abort) {
    if (!streamCtx) return;
    // Keep a stable key for anonymous streams (no convId until meta arrives).
    if (!streamCtx._streamKey) {
      streamCtx._streamKey =
        streamCtx.convId || Symbol(`stream-${_streamSeq++}`);
    }
    _streamsByConv.set(streamCtx._streamKey, { abort, streamCtx });
  }

  /** Move the stream's record to its real convId once it is known (anon → real). */
  function rekeyStream(streamCtx, convId) {
    if (!streamCtx || !convId) return;
    const oldKey = streamCtx._streamKey;
    if (oldKey === convId) return;
    const rec = oldKey != null ? _streamsByConv.get(oldKey) : null;
    if (oldKey != null) _streamsByConv.delete(oldKey);
    streamCtx._streamKey = convId;
    if (rec) _streamsByConv.set(convId, rec);
    // If this stream IS the foreground stream, migrate the foreground pointer to
    // the real id: when a new-chat stream learns its conv_id from meta/done it
    // must still be the foreground stream (otherwise it cannot adopt its own conv / UI breaks).
    if (oldKey != null && foregroundConvId === oldKey) {
      foregroundConvId = convId;
    }
  }

  /** Remove the record (if it still belongs to THIS streamCtx) — race-safe. */
  function unregisterStream(streamCtx) {
    if (!streamCtx) return;
    const key = streamCtx._streamKey;
    if (key == null) return;
    const rec = _streamsByConv.get(key);
    if (rec && rec.streamCtx === streamCtx) _streamsByConv.delete(key);
  }

  /** Return the live stream record for the DISPLAYED conv (null if none). */
  function foregroundStreamRecord() {
    if (foregroundConvId == null) return null;
    const rec = _streamsByConv.get(foregroundConvId);
    return rec || null;
  }

  /** Is streamCtx the foreground (displayed) stream? Foreground UI gate.
   *  If foregroundConvId is NOT set (single-chat common case) fall back to the
   *  old behaviour: if there is exactly ONE live stream, it is the foreground
   *  (backwards compatibility — composer / "Responding" bar should still work). */
  function isForegroundStream(streamCtx) {
    if (!streamCtx) return false;
    if (foregroundConvId != null) {
      const rec = _streamsByConv.get(foregroundConvId);
      if (rec) return rec.streamCtx === streamCtx;
      // The displayed conv has no record yet (stream may be receiving its conv_id now):
      // if the stream's own convId matches the displayed conv, it is the foreground.
      return streamCtx.convId != null && streamCtx.convId === foregroundConvId;
    }
    // foregroundConvId unknown: if only one stream is registered it is considered
    // foreground (single-chat path — same as the old single-liveStreamCtx behaviour).
    if (_streamsByConv.size <= 1) return true;
    return false;
  }

  /** chat-threads switch/restore: notify which conversation is displayed (foreground UI gate). */
  function setForegroundConversation(convId) {
    foregroundConvId = convId || null;
  }

  /** Is convId the DISPLAYED (foreground) conv? If foreground is unknown (single-chat
   *  common path) returns true — same as the old single-stream behaviour.
   *  Called with a convId instead of a streamCtx (foreground composer/orb open decisions). */
  function isForegroundConv(convId) {
    return foregroundConvId == null || foregroundConvId === convId;
  }

  /** Is there already a FINALIZED (not pending) assistant row for this turn in the
   *  FOREGROUND log? The live SSE `done` event renders the turn in place and removes
   *  the `bubble-bot-pending` / `aria-busy` markers (see done handler).
   *  true → the WS `turn_completed` handler must NOT rebuild the log from scratch
   *  (innerHTML="" + re-render all messages + snap to bottom was the root cause of
   *  the "entire conversation re-renders and scrolls from the top" complaint).
   *  false → the row is absent or incomplete (stalled SSE / F5 / tab switch) →
   *  a real recovery (rebuild/resume) is needed.
   *  turnId is the SSE `meta`/`done` `turn_id` (= WS `assistant_turn_id`) and
   *  the `data-turn-id` stamped on the row; all three share the same id space. */
  function isForegroundTurnFinalized(convId, turnId) {
    if (!isForegroundConv(convId)) return false;
    const log = chatCtx.hooks.log;
    const tid = String(turnId || "").trim();
    if (!log || !tid || typeof log.querySelector !== "function") return false;
    let row;
    try {
      row = log.querySelector(`.row[data-turn-id="${CSS.escape(tid)}"]`);
    } catch {
      return false;
    }
    if (!row) return false;
    const bubble = row.querySelector(".bubble-assistant, .bubble-bot");
    if (!bubble) return false;
    return (
      !bubble.classList.contains("bubble-bot-pending") &&
      bubble.getAttribute("aria-busy") !== "true"
    );
  }

  // Backwards-compatibility view: `liveStreamCtx` is now derived — it is the
  // streamCtx of the DISPLAYED conv (null if none). Old consumers
  // (reconcileServerCompletedTurn, tests) continue to work through this getter.
  function getLiveStreamCtxView() {
    const rec = foregroundStreamRecord();
    return rec ? rec.streamCtx : null;
  }

  /**
   * Re-attach the live row on return to a conversation. Switching to another
   * chat clears the log (beginLogHydrate → innerHTML=""), which detaches the
   * ACTIVE row of this conv from the DOM (the stream continues writing to an
   * invisible node). When the user comes back, this call — made AFTER hydrate
   * completes — appends that stream's live row to the fresh log → A's
   * accumulated live text + tool cards become visible immediately and the stream
   * continues uninterrupted. No re-render / new follower (no double-render risk)
   * — only the existing node is re-attached.
   * @returns {boolean} true if the live row was successfully re-attached.
   */
  function reattachLiveRow(convId) {
    if (!convId) return false;
    const rec = _streamsByConv.get(convId);
    const ctx = rec ? rec.streamCtx : null;
    const row = ctx && ctx.rowEl;
    // audit B3: target THIS conv's OWN pane → even when called while another chat
    // is displayed, A's row is NOT moved to the wrong pane.
    const log = paneForConv(convId);
    if (!ctx || !row || !log) return false;
    // Stream already finished (done/error) → store hydrate brings the final text;
    // do not re-attach the live row (no duplicate row).
    if (ctx.doneMeta || ctx.serverError || ctx.aborted) return false;
    // Is the row still in this log? (parentElement) — if not, re-append it.
    if (row.parentElement === log) return true;
    try {
      log.appendChild(row);
      chatCtx.hooks.updateEmptyState?.();
      chatCtx.hooks.stickToBottomIfFollowing?.(chatCtx.hooks.logScroll || log);
      return true;
    } catch {
      return false;
    }
  }

  // ── INTRA-BUBBLE DOUBLE-RENDER GUARD (turn_id → single canonical text) ────────
  // INVARIANT: once a turn_id has been finalized, any follower/resume that replays
  // that turn's deltas from index 0 must NOT append to the bubble's acc — it must
  // REPLACE acc with the canonical text. `_follow_turn` is multi-follower by design
  // (the live POST stream + GET /chat/active resume can both replay the same buffer
  // from index 0). If two followers feed the same turn into the same bubble
  // the acc would double (e.g. "Your name is Alice." twice).
  // This map holds the final server-authoritative single text keyed by turn_id;
  // the second feeder REPLACES instead of appending. Frontend-only, idempotent.
  const _turnFinalText = new Map();
  const TURN_FINAL_CAP = 64; // cap to prevent unbounded growth (oldest entry is evicted)
  function rememberTurnFinalText(turnId, text) {
    if (!turnId || typeof text !== "string" || !text) return;
    const key = String(turnId);
    if (_turnFinalText.has(key)) _turnFinalText.delete(key);
    _turnFinalText.set(key, text);
    while (_turnFinalText.size > TURN_FINAL_CAP) {
      const oldest = _turnFinalText.keys().next().value;
      _turnFinalText.delete(oldest);
    }
  }

  function isStreamActive() {
    return _streamsByConv.size > 0;
  }

  /** Is a stream currently running for the given conversation (even if not displayed)? */
  function isConversationStreamActive(convId) {
    if (!convId) return false;
    return _streamsByConv.has(convId);
  }

  /** A3: the turn_id of the conv's live in-memory stream, if any (null otherwise).
   *  Used by refreshConversationLogAfterTurn to tell whether the currently-active
   *  turn is the SAME turn it was asked to render, or a NEXT (drained) turn — in the
   *  latter case the completed turn's answer must still be reloaded into the DOM. */
  function activeStreamTurnId(convId) {
    if (!convId) return null;
    const rec = _streamsByConv.get(convId);
    return (rec && rec.streamCtx && rec.streamCtx.turnId) || null;
  }

  /**
   * STOP / teardown. Previously always aborted "the last stream" → could kill
   * a background conversation's turn or cut the wrong one. Default now: abort
   * only the DISPLAYED conv's (foregroundConvId) stream. If an explicit convId
   * is given, abort that conv's stream; if no foreground is known (old single-chat
   * path) abort the single registered stream.
   *   abortActiveChatStream()            → displayed conv's stream
   *   abortActiveChatStream(convId)      → that conv's stream
   *   abortActiveChatStream(null, {all}) → ALL streams (page unload etc.)
   */
  function abortActiveChatStream(convId, opts = {}) {
    if (opts && opts.all) {
      for (const rec of Array.from(_streamsByConv.values())) {
        try {
          rec.abort?.abort();
        } catch {
          /* ignore */
        }
      }
      _streamsByConv.clear();
    } else {
      let rec = null;
      if (convId) {
        rec = _streamsByConv.get(convId) || null;
      } else if (foregroundConvId != null) {
        rec = _streamsByConv.get(foregroundConvId) || null;
      } else if (_streamsByConv.size === 1) {
        // foreground unknown + single stream → use that one (backwards compat: single-chat STOP).
        rec = _streamsByConv.values().next().value || null;
      }
      if (rec) {
        try {
          rec.abort?.abort();
        } catch {
          /* ignore */
        }
        unregisterStream(rec.streamCtx);
      }
    }
    // Reset the foreground composer/orb UI (STOP→SEND) ONLY if the foreground
    // stream was affected. When aborting a background conv's stream (e.g. delete/
    // archive → abortStream(bgConvId)) and the DISPLAYED chat is still streaming,
    // do NOT flip the composer to SEND — otherwise deleting B while A is streaming
    // would break A's button (reconcileServerCompletedTurn guards the same asymmetry
    // with `if (wasForeground)`; this gate was previously missing here).
    // all / argless-STOP / foreground-conv → considered foreground;
    // isForegroundConv returns true when foreground is unknown.
    const wasForeground =
      !!(opts && opts.all) || convId == null || isForegroundConv(convId);
    if (wasForeground) {
      chatCtx.chatInFlight = false;
      // Flip SEND↔STOP from a single point (every cancel path — resume + safety timer —
      // should clear the button).
      if (chatCtx.hooks.setStreamingUi) chatCtx.hooks.setStreamingUi(false);
      else if (chatCtx.hooks.sendBtn) chatCtx.hooks.sendBtn.disabled = false;
    }
  }

  // ── RELEASE STREAMS ON PAGE UNLOAD (hard-refresh fix) ──────────────────────
  // User report: "Ctrl+Shift+R does NOT reload when 2 chats are streaming — it
  // just keeps trying." Root cause: each live stream waits on a
  // `response.body.getReader().read()` loop; if those readers are NOT cancelled
  // on page close the browser waits for all N pending fetches before navigating →
  // reload hangs (gets worse as the stream count increases). Abort ALL streams in
  // pagehide+beforeunload: AbortController.abort() resolves reader.read() with
  // AbortError and navigation can proceed. Only the client-side fetch is cut —
  // the detached turn on the server keeps running and is recovered by
  // resumeActiveTurn on reload (no response loss). store.js's flushChatStore
  // (pagehide/beforeunload) wiring is independent — this only releases stream readers.
  if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
    const releaseStreamsOnUnload = () => {
      try {
        // (1) STOP THE rAF STORM — the root cause. While a response is streaming
        // every `delta` fires a reveal/markdown-throttle rAF + SSE-drain rAF;
        // those perform O(N) full-markdown re-parse + innerHTML mutations saturating
        // the main thread (during the thinking phase only textContent+= → O(1),
        // which is why refresh works while thinking but NOT while the answer is
        // streaming — user report). Aborting the fetch alone is NOT enough: this
        // rAF chain keeps the event-loop busy and blocks navigation. Cancel every
        // live stream's reveal/md/scroll rAF+timers and the global SSE-drain rAF
        // first, then flush the queue → event-loop is free, hard-refresh proceeds.
        for (const rec of _streamsByConv.values()) {
          try { resetStreamMdThrottle(rec.streamCtx); } catch { /* ignore */ }
        }
        if (_sseDrainRaf != null) {
          try { cancelAnimationFrame(_sseDrainRaf); } catch { /* ignore */ }
          _sseDrainRaf = null;
        }
        _sseQueue.length = 0;
        _sseDrainWaiters.length = 0;
        // (2) Abort the client-side fetch readers (also clears the registry).
        abortActiveChatStream(null, { all: true });
      } catch {
        /* ignore — errors on unload must not block navigation */
      }
    };
    window.addEventListener("pagehide", releaseStreamsOnUnload);
    window.addEventListener("beforeunload", releaseStreamsOnUnload);
  }

  // MOBILE LIFECYCLE (screen-lock / background ↔ return).
  // Problem: on mobile, `pagehide` does NOT fire when the screen turns off
  // (it only fires on real navigation / bfcache) — only `visibilitychange→hidden`
  // arrives. When the OS suspends the radio, the in-flight SSE `reader.read()`
  // is rejected with a NETWORK ERROR (not AbortError) → consumeSseResponse writes
  // serverError=CONN → "⚠ Disconnected". But the turn on the server keeps running
  // DETACHED (no response loss) and is recovered by resumeActiveTurn on return.
  // Two-part fix:
  //  (1) ON HIDE: mark streams as live; and ONLY on mobile + NOT in voice
  //      conversation mode, proactively + cleanly abort client streams (like
  //      pagehide) → controlled AbortError instead of a network error →
  //      no "⚠ Disconnected". Do NOT touch hands-free voice turns (wake-lock +
  //      TTS streaming); desktop tab switches are frequent and the connection
  //      survives in the background → do NOT affect desktop either.
  //  (2) ON VISIBLE / bfcache pageshow: if a stream was live before hiding,
  //      resume the displayed conversation's turn (replay the detached turn) →
  //      seamless "it kept working in the background" feel. If the turn finished
  //      while we were away, refresh the log.
  if (typeof document !== "undefined" && typeof document.addEventListener === "function") {
    const _isLikelyMobile =
      typeof navigator !== "undefined" &&
      (/Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent || "") ||
        (navigator.maxTouchPoints || 0) > 1);
    let _streamsWereLiveOnHide = false;
    let _resumeOnVisibleTimer = null;

    async function _resumeForegroundTurnAfterReturn() {
      if (!_streamsWereLiveOnHide) return;
      _streamsWereLiveOnHide = false;
      const cid = foregroundConvId;
      if (!cid) return;
      try {
        // resumeActiveTurn returns true immediately if the stream is STILL live
        // (desktop: connection survived in the background) → no-op. On mobile it
        // replays the aborted turn from the server. false → the turn finished (or the
        // connection dropped) while we were away.
        const resumed = await resumeActiveTurn(cid);
        // On `false` we must RE-RENDER the log from the server — the SAME recovery F5
        // and chat-switch use (refreshConversationLogAfterTurn → reloadConversationLogFromServer).
        // The old call here was syncConversationLogFromServer, which only rewrites
        // thread.messages + the store and NEVER repaints the DOM (it assumes the live
        // `done` already rendered). But when the turn completes while the tab is hidden
        // the DOM keeps the half/pending bubble, so the finished last answer stayed
        // INVISIBLE until a manual F5 (user report: switching tabs hides Akana's last reply).
        // reloadConversationLogFromServer clears + re-renders → the answer shows on return.
        if (!resumed) await chatCtx.reloadConversationLogFromServer?.(cid);
      } catch {
        /* ignore — return recovery is silent */
      }
    }

    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "hidden") {
        if (_streamsByConv.size > 0) _streamsWereLiveOnHide = true;
        // A4: rAF is suspended while hidden. Cancel any pending rAF-scheduled drain and
        // flush the queue SYNCHRONOUSLY now, so frames buffered up to this moment reach the
        // player and the read loop's flush waiter resolves. Frames arriving later while
        // hidden are handled by scheduleSseDrain's setTimeout path. Runs even when a stream
        // is aborted below (there may be a non-mobile/voice stream still delivering TTS).
        try {
          cancelSseDrain();
          if (_sseQueue.length) drainSseQueue();
        } catch { /* ignore */ }
        // If voice conversation mode is active (hands-free, wake-lock) do NOT abort the stream.
        const inVoiceConv = !!window.AkanaVoice?.isConversationMode?.();
        if (_isLikelyMobile && !inVoiceConv && _streamsByConv.size > 0) {
          try { abortActiveChatStream(null, { all: true }); } catch { /* ignore */ }
        }
        return;
      }
      // visible: small debounce to absorb double-trigger with pageshow.
      if (_resumeOnVisibleTimer) return;
      _resumeOnVisibleTimer = setTimeout(() => {
        _resumeOnVisibleTimer = null;
        void _resumeForegroundTurnAfterReturn();
      }, 60);
    });

    // bfcache restore (app-switch on mobile): some browsers do NOT fire
    // visibilitychange; only pageshow(persisted=true) arrives.
    window.addEventListener("pageshow", (e) => {
      if (e && e.persisted) void _resumeForegroundTurnAfterReturn();
    });
  }

  // Grace period allowed for the live SSE `done` to arrive after the server
  // broadcasts `turn_completed`. If it does NOT arrive within this window the
  // stream is considered stalled — in a normal turn `done` arrives well within
  // this period, so no false/premature recovery occurs.
  const TURN_DONE_GRACE_MS = 2000;

  /** WS SAFETY NET: the server completed the turn but the live SSE `done` never
   *  arrived (half-open TCP / stalled follower) → "Responding" would hang forever.
   *  After the short grace period, if the stream is STILL stalled (the same turn
   *  has not received its own done/error), close the stalled SSE and clear
   *  "Responding". Pure fallback: if SSE `done` arrives within the grace (doneMeta
   *  is set) or the stream changed, does NOTHING; if a new turn is in flight
   *  (turnId mismatch), does not touch it — never closes the wrong turn's indicator.
   *  @returns {Promise<boolean>} true if a stalled stream was recovered. */
  function reconcileServerCompletedTurn(convId, assistantTurnId) {
    return new Promise((resolve) => {
      const cid = String(convId || "").trim();
      if (!cid) return resolve(false);
      // Look up THIS conversation's stream record directly (per-conv) — previously
      // only "the last foreground stream" (liveStreamCtx) was compared; now a
      // stalled stream for any conv can be recovered even if it is not displayed.
      const rec = _streamsByConv.get(cid);
      const ctx = rec ? rec.streamCtx : null;
      if (!ctx || ctx.convId !== cid) return resolve(false);
      if (ctx.doneMeta || ctx.serverError) return resolve(false);
      const atid = String(assistantTurnId || "").trim();
      if (atid && ctx.turnId && atid !== ctx.turnId) return resolve(false);
      window.setTimeout(() => {
        // SSE done arrived / stream ended during the grace → fallback not needed.
        const stillRec = _streamsByConv.get(cid);
        if (
          !stillRec ||
          stillRec.streamCtx !== ctx ||
          ctx.doneMeta ||
          ctx.serverError
        ) {
          return resolve(false);
        }
        const wasForeground = isForegroundStream(ctx);
        abortActiveChatStream(cid); // close ONLY this conv's stalled SSE
        // Clear "Responding" ONLY if this stream is the displayed one — a
        // background recovery must not close the visible chat's composer/bar.
        if (wasForeground) finalizeStreamUi();
        resolve(true);
      }, TURN_DONE_GRACE_MS);
    });
  }

  /** WS: post-turn background memory capture sent "saved" (staging) sources.
   *  Because capture now runs AFTER `done`, these chips are NOT in the done
   *  payload; they are appended here under the relevant turn (turn_id)
   *  (if renderSourcesRow already built a row from recall items at done, those
   *  chips are added to that row — appendMemorySources creates-or-expands + dedupes).
   *  Only works when the DISPLAYED conversation + the relevant turn are in the DOM;
   *  otherwise silently no-ops (best-effort). */
  function onServerMemoryStaged(evt) {
    const cid = String((evt && evt.conversation_id) || "").trim();
    if (!cid || cid !== chatCtx.conversationIdForMemory()) return;
    const writes = Array.isArray(evt && evt.writes) ? evt.writes : [];
    const staging = writes.filter((w) => w && (w.kind === "staging" || w.kind === "stored"));
    if (!staging.length) return;
    const append = window.AkanaChatRender?.appendMemorySources;
    const log = chatCtx.hooks.log;
    if (typeof append !== "function" || !log) return;
    // Look up the relevant turn's row by turn_id. If not found, silently skip —
    // falling back to "the last assistant row" would cause wrong attribution
    // (if the user sent a new message meanwhile, the old turn's chip would attach
    // to the new bubble). The backend always sends a turn_id; the row is tagged
    // at done/meta → found on the normal path.
    const tid = String((evt && evt.turn_id) || "").trim();
    if (!tid) return;
    const row = log.querySelector(`.row[data-turn-id="${CSS.escape(tid)}"]`);
    const msgBody = row && row.querySelector(".msg-body");
    if (!msgBody) return;
    append(msgBody, staging);
    chatCtx.hooks.stickToBottomIfFollowing?.(chatCtx.hooks.logScroll || log);
  }
  window.AkanaBus?.on?.("ws:memory_staged", onServerMemoryStaged);

  // ── STOP — cancel the detached turn on the server ──────────────────────────
  // A client-side SSE abort cuts the stream but the turn keeps running on the
  // SERVER → the next message gets a `TURN_BUSY` (409). This cancels the turn on
  // the server and removes it from the registry so a new message can be sent immediately.
  // BACKEND CONTRACT: POST /api/v1/chat/active/{cid}/cancel (bearer) →
  //   { cancelled: true|false }. If the endpoint is absent (404/405), silently returns false.
  // Bug-free: network error → false (swallowed), double-click → single in-flight shared.
  // b4: PER-CONVERSATION in-flight cancel dedup (was a single global → a STOP/delete on conv B
  // returned conv A's in-flight cancel and never actually cancelled B). Mirrors _turnsInFlight.
  const _cancelInFlight = new Map(); // convId → Promise<boolean>
  /** Once per convId: LLM_UNAVAILABLE "ongoing response" → server-side cancel. */
  const _activeRunAutoCancelDone = new Set();

  async function recoverStuckBridgeOnServer(convId, { hard = false } = {}) {
    const id = (convId || "").trim();
    if (!id) return false;
    try {
      const qs = hard ? "?hard=1" : "";
      const r = await fetch(
        `${baseUrl()}/api/v1/chat/active/${encodeURIComponent(id)}/recover${qs}`,
        { method: "POST", headers: authHeaders() },
      );
      if (r.status === 404 || r.status === 405) return false;
      if (!r.ok) return false;
      const body = await r.json().catch(() => ({}));
      return Boolean(body && body.recovered);
    } catch {
      return false;
    }
  }

  async function maybeRecoverStuckActiveRun(serverError, convId) {
    const id = (convId || "").trim();
    if (!id || !serverError) return false;
    const isStuck =
      serverError.code === "LLM_UNAVAILABLE" &&
      (String(serverError.message || "").includes("süren bir yanıt") ||
       String(serverError.message || "").includes("ongoing response") ||
       String(serverError.message || "").includes("already in progress"));
    if (!isStuck || _activeRunAutoCancelDone.has(id)) return false;
    _activeRunAutoCancelDone.add(id);
    let recovered = await cancelActiveTurnOnServer(id);
    if (!recovered) {
      recovered = await recoverStuckBridgeOnServer(id);
    }
    if (!recovered) {
      recovered = await recoverStuckBridgeOnServer(id, { hard: true });
    }
    chatCtx.hooks.showToast?.(
      recovered
        ? window.AkanaI18n.t("transport.stuck.recovered")
        : window.AkanaI18n.t("transport.stuck.may_still_run"),
      recovered ? "info" : "warn",
    );
    return recovered;
  }

  async function cancelActiveTurnOnServer(convId) {
    const id = (convId || "").trim();
    if (!id) return false;
    const existing = _cancelInFlight.get(id);
    if (existing) {
      // Same-conversation double-click dedup only — a DIFFERENT conversation falls through
      // and issues its OWN cancel.
      try {
        return await existing;
      } catch {
        return false;
      }
    }
    const run = (async () => {
      try {
        const r = await fetch(
          `${baseUrl()}/api/v1/chat/active/${encodeURIComponent(id)}/cancel`,
          { method: "POST", headers: authHeaders() },
        );
        if (r.status === 404 || r.status === 405) return false; // endpoint not available
        if (!r.ok) return false;
        const body = await r.json().catch(() => ({}));
        return Boolean(body && body.cancelled);
      } catch {
        return false; // network error — swallowed (UI is still released)
      }
    })();
    _cancelInFlight.set(id, run);
    try {
      return await run;
    } finally {
      if (_cancelInFlight.get(id) === run) _cancelInFlight.delete(id);
    }
  }

  async function sendChatBlocking(text) {
    const abort = new AbortController();
    // The blocking (non-stream) path should also be cancellable via STOP:
    // keep a lightweight record (no streamCtx; abort only). Key = current conv (or symbol).
    const pseudoCtx = { convId: chatCtx.conversationIdForMemory?.() || null };
    registerStream(pseudoCtx, abort);
    let r;
    try {
      r = await fetch(`${baseUrl()}/api/v1/chat`, {
        method: "POST",
        headers: authHeaders(true),
        body: JSON.stringify(chatPayload(text)),
        signal: abort.signal,
      });
    } catch (e) {
      unregisterStream(pseudoCtx);
      throw e;
    }
    try {
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      throw new Error(parseApiError(body, r.statusText));
    }
    if (body.action) await chatCtx.applyChatServerAction(body.action, body);
    else if (body.conversation_id) chatCtx.setConversationId(body.conversation_id);

    // Render: meta + (optional) eviction warning + tool_calls + bubble.
    const wrap = document.createElement("div");
    wrap.className = "row";
    const meta = document.createElement("div");
    meta.className = "meta";
    const intentTag = body.intent === "system_action" ? " · system" : "";
    const approvalTag = body.approval_required ? window.AkanaI18n.t("transport.approval.meta_tag") : "";
    const lat = typeof body.latency_ms === "number" ? ` · ${body.latency_ms} ms` : "";
    meta.textContent = `Akana${intentTag}${approvalTag}${lat}`;
    wrap.appendChild(meta);

    if (body.dropped_turns && body.dropped_turns > 0) {
      const warn = document.createElement("div");
      warn.className = "history-warn";
      warn.textContent = window.AkanaI18n.t("transport.stream.history_drop", { n: body.dropped_turns });
      wrap.appendChild(warn);
    }
    if (Array.isArray(body.skill_used) && body.skill_used.length) {
      const skillNode = window.AkanaChatRender.renderSkillUse?.(body.skill_used);
      if (skillNode) wrap.appendChild(skillNode);
    }

    const bubble = document.createElement("div");
    bubble.className = "bubble-bot";
    setBubbleMarkdown(bubble, body.text || window.AkanaI18n.t("transport.blocking.empty"));
    wrap.appendChild(bubble);
    // Tool calls go ABOVE the bubble — consistent aurora process card with live/restore
    // (default collapsed). Persist turn_id→tool_calls for F5 restore.
    if (Array.isArray(body.tool_calls) && body.tool_calls.length) {
      const card = window.AkanaChatRender.renderToolProcessCard?.(body.tool_calls, body.turn_id);
      if (card) wrap.insertBefore(card, bubble);
      else window.AkanaChatRender.appendToolCallsGrouped?.(wrap, body.tool_calls, body.turn_id);
      window.AkanaChatRender.putToolCallsForTurn?.(body.turn_id, body.tool_calls);
    }
    chatCtx.hooks.log.appendChild(wrap);
    (chatCtx.hooks.logScroll || chatCtx.hooks.log).scrollTop = (chatCtx.hooks.logScroll || chatCtx.hooks.log).scrollHeight;
    return body.text;
    } finally {
      unregisterStream(pseudoCtx);
    }
  }

  // ── Shared SSE consumer ────────────────────────────────────────────────────
  // Both the live `streamChat` path and the on-return-resume path share this reader.
  // Contract: streamCtx { meta, bubble, msgBody, scroller, insertBeforeBubble,
  //   turnId, acc, doneMeta, serverError } — handleChatStreamEvent mutates this object.
  // On disconnect/parse error, serverError is populated (bubble does not stay "pending");
  // user aborts are re-thrown to the caller when opts.rethrowAbort is set.
  async function consumeSseResponse(response, streamCtx, logRoot, abort, opts = {}) {
    const { rethrowAbort = false } = opts;
    if (logRoot) logRoot.dataset.chatStreaming = "1";
    resetSseQueue(streamCtx);
    resetStreamMdThrottle(streamCtx);
    // Register in the per-conv record (caller may have already registered —
    // registerStream is idempotent: re-writes the same streamCtx under the same key).
    // WS safety-net (reconcileServerCompletedTurn) + STOP find the stream via this
    // record; it is removed in the finally block.
    registerStream(streamCtx, abort);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    const processSseBuffer = () => {
      const { frames, rest } = parseSseFrames(buf);
      buf = rest;
      for (const f of frames) enqueueSseFrame(f, streamCtx);
    };
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (value) buf += decoder.decode(value, { stream: true });
        if (buf) processSseBuffer();
        if (done) {
          if (buf.trim()) {
            buf = `${buf.trim()}\n\n`;
            processSseBuffer();
          }
          await flushSseQueue();
          break;
        }
        if (_sseQueue.length >= SSE_FRAME_BUDGET) {
          await flushSseQueue();
        }
      }
    } catch (e) {
      if (e && e.name === "AbortError") {
        streamCtx.aborted = true;
        if (rethrowAbort) {
          if (logRoot) delete logRoot.dataset.chatStreaming;
          resetSseQueue(streamCtx);
          try {
            reader.releaseLock?.();
          } catch {
            /* ignore */
          }
          throw e;
        }
      }
      await flushSseQueue().catch(() => {});
      // DO NOT treat an intentional abort as an error: abortStream (navigation /
      // new-thread / mobile release on page hide) resolves the reader with AbortError
      // → streamCtx.aborted. Recording this as serverError=CONN would cause resume/
      // caller to show "⚠ Disconnected"; but the turn on the server keeps running
      // DETACHED (no loss, resume recovers on return). Only write an error for
      // REAL disconnects (not aborted).
      if (!streamCtx.serverError && !streamCtx.aborted) {
        streamCtx.serverError = { code: "CONN", message: humanizeChatError(e) };
      }
    } finally {
      if (!streamCtx.doneMeta) finalizeThoughtFeed(streamCtx);
      // LOCK GUARANTEE (phase bar): the "Responding" bar is normally closed only
      // by the done/error SSE event. If the stream closes WITHOUT those events (server
      // closes without sending `done`, disconnects after the last delta, or the
      // backend's pre-done persist step throws), the bar would be stuck in
      // "Responding" indefinitely via its timer. However the stream loop exits,
      // set idle here — end() is idempotent. FOREGROUND-GATED: when a background
      // stream finishes, do not close the visible chat's "Responding"/composer.
      finalizeStreamUi(streamCtx);
      // chatStreaming flag = is the visible log currently streaming. NOTE: check foreground
      // BEFORE unregistering (unregister changes isForeground). Reused for both the mic-reopen
      // emit and the chatStreaming clear below.
      const wasForeground = isForegroundStream(streamCtx);
      // AUDIO SAFETY (mic-reopen): in a voice turn the mic is re-opened only by the tts_end SSE
      // event (→ voice:tts:streamEnd → ttsStreamOpen=false). If the stream ends WITHOUT that
      // event (server error, SSE disconnect, pre-done persist throw), emit it so the flag
      // resets (idempotent; re-arms listening after the queue drains).
      // b5: FOREGROUND-GATED — a BACKGROUND (non-voice) stream finishing must NOT emit streamEnd;
      // that would re-arm the mic / end the FOREGROUND voice turn mid-reply. EXCEPTION: THIS
      // stream's OWN voiceTurn end must always clear its latch (balances the unconditional
      // streamOpen emit) even if it is no longer foreground — single-conversation voice, so it
      // can only clear its own latch.
      if (wasForeground || streamCtx.voiceTurn) {
        try {
          window.AkanaBus?.emit?.("voice:tts:streamEnd", {});
        } catch {
          /* ignore */
        }
      }
      // Remove this stream from the per-conv registry (race-safe: only if it still owns the record).
      unregisterStream(streamCtx);
      const fgRec = foregroundStreamRecord();
      if (logRoot && (wasForeground || !fgRec)) {
        delete logRoot.dataset.chatStreaming;
      }
      resetSseQueue(streamCtx);
      // Cancel pending live-markdown timer (stream has ended; no late render needed).
      resetStreamMdThrottle(streamCtx);
      // CLEAN CLOSE WITHOUT `done` (delta-only stream): the reveal loop may not
      // have advanced `shown` to the full acc yet (it increments per frame, no
      // done-snap) and resetStreamMdThrottle just killed the loop. Synchronously
      // render the final FULL text → if `shown` lagged, the user does not see
      // truncated text. The done/serverError/abort/plain paths already did their
      // own final render; do not overwrite them.
      if (
        !streamCtx.doneMeta &&
        !streamCtx.serverError &&
        !streamCtx.aborted &&
        !streamCtx.plainStreamMode &&
        streamCtx.bubble &&
        (streamCtx.acc || "").length
      ) {
        setBubbleMarkdown(streamCtx.bubble, streamCtx.acc);
      }
      try {
        reader.releaseLock?.();
      } catch {
        /* ignore */
      }
    }
  }

  /**
   * Resume an active turn on return: if a turn is running when the conversation
   * is opened, replay the accumulated response + tool cards and continue live.
   * Returns false if there is no active turn (caller performs the normal messages
   * fetch). When turn_completed (done) arrives, caller refreshes the store once.
   * @returns {Promise<boolean>} true if an active turn was connected and consumed.
   */
  async function resumeActiveTurn(convId) {
    if (!convId || !chatCtx.hooks.log) return false;
    // DOUBLE-RENDER GUARD (resume): if this conversation ALREADY has a live follower
    // (POST stream running), a second GET /chat/active follower would replay the same
    // buffer from index 0 → two followers feeding the same bubble. _turnFinalText
    // prevents the double text but creates unnecessary work + DOM collision; the
    // cleanest fix is simply not resuming. Caller treats "already live" as true.
    if (isConversationStreamActive(convId)) return true;
    const response = await probeActiveTurn(convId);
    if (!response) return false; // 204/404 → no active turn
    // Race: if a POST stream started for this conv during the probe await, exit
    // again (avoid entering a double-follower situation).
    // b17: release the already-opened SSE Response body first — only the consumeSseResponse
    // fall-through below attaches a reader that closes it, so returning here without cancelling
    // leaks the follower stream.
    if (isConversationStreamActive(convId)) {
      try {
        await response.body?.cancel();
      } catch {
        /* ignore */
      }
      return true;
    }

    // Resume should abort ONLY this conv's (possibly stalled) stream — do NOT
    // touch other conversations' concurrent streams (old abortActiveChatStream()
    // aborted all; now it is conv-targeted).
    abortActiveChatStream(convId);
    // A3: /chat/active returns whatever turn is active NOW. If T1 completed and drained
    // the NEXT turn T2 in this conversation, probeActiveTurn above found T2 — attaching a
    // follower for T2 alone would leave T1's completed answer permanently out of the DOM
    // (the caller sees resume==true and never reloads the log). Reload the persisted log
    // FIRST so T1's answer is rendered, THEN attach the live T2 follower below;
    // tagStreamRowTurnId dedups T2's static row against the live one when its meta arrives.
    // Only for the DISPLAYED conv — a background resume must not repaint the visible pane.
    if (isForegroundConv(convId)) {
      try {
        await chatCtx.reloadConversationLogFromServer?.(convId);
      } catch {
        /* ignore — reload is best-effort; the follower still attaches below */
      }
    }
    const abort =
      typeof AbortController !== "undefined" ? new AbortController() : null;
    // Resume is also a live stream — the send button should switch to STOP (user
    // should be able to stop the resumed turn too). Only flip the composer to STOP
    // if this conv is displayed (foreground); a background resume must not break
    // the visible chat.
    if (isForegroundConv(convId)) {
      chatCtx.hooks.setStreamingUi?.(true);
    }
    window.AkanaTurnStatus?.begin();

    const wrap = document.createElement("div");
    wrap.className = "row row-assistant";
    const avatar = document.createElement("div");
    avatar.className = "msg-avatar";
    avatar.setAttribute("aria-hidden", "true");
    avatar.textContent = "A";
    const msgBody = document.createElement("div");
    msgBody.className = "msg-body";
    const meta = document.createElement("div");
    meta.className = "msg-label meta";
    meta.textContent = "Akana";
    const bubble = document.createElement("div");
    bubble.className = "bubble-assistant bubble-bot bubble-bot-pending bubble-bot-stream";
    bubble.setAttribute("aria-busy", "true");
    msgBody.appendChild(meta);
    msgBody.appendChild(bubble);
    wrap.appendChild(avatar);
    wrap.appendChild(msgBody);
    // audit B5: the resumed turn must render into THIS conv's OWN pane (NOT the
    // displayed pane getter) → a resume while another conversation is visible must
    // NOT bleed into that conversation. logRoot=pane means the chatStreaming flag
    // is also set+cleared on the correct pane (B1).
    const _pane = paneForConv(convId);
    _pane.appendChild(wrap);
    chatCtx.hooks.updateEmptyState?.();
    const scroller = chatCtx.hooks.logScroll || _pane;
    const logRoot = _pane;
    if (logRoot) logRoot.dataset.chatStreaming = "1";
    function insertBeforeBubble(node) {
      insertBeforeOrAppend(msgBody, node, bubble);
    }
    const streamCtx = {
      meta,
      bubble,
      msgBody,
      scroller,
      insertBeforeBubble,
      turnId: null,
      acc: "",
      doneMeta: null,
      serverError: null,
      userText: "",
      toolPhaseActive: false,
      convId: convId || null,
      rowEl: wrap, // see streamChat: for reattachLiveRow after a tab switch
    };

    try {
      await consumeSseResponse(response, streamCtx, logRoot, abort, { rethrowAbort: false });
      const { acc, doneMeta, serverError, aborted } = streamCtx;
      if (aborted && !doneMeta) {
        cleanupStreamRow(streamCtx);
        if (!acc) wrap.remove();
        chatCtx.hooks.updateEmptyState?.();
        return false;
      }
      if (serverError) {
        await maybeRecoverStuckActiveRun(serverError, convId);
        flushStreamMarkdownUpdate(streamCtx);
        bubble.classList.remove("bubble-bot-pending");
        bubble.removeAttribute("aria-busy");
        if (acc) setBubbleMarkdown(bubble, acc);
        const warn = document.createElement("div");
        warn.className = "history-warn";
        warn.textContent = window.AkanaI18n.t("transport.stream.disconnected", { msg: serverError.message || serverError.code || "stream error" });
        insertBeforeBubble(warn);
        // Append-only (do not clobber whatever segments the meta line already has):
        // guard against double-chip if a `done` event slipped in just before the error.
        if (meta && !meta.querySelector(".turn-status-chip")) {
          meta.appendChild(buildStreamStatusChip("err"));
        }
      }
      // If turn_completed (done) arrived, refresh the store once (DOM is already up to date).
      if (doneMeta || serverError) {
        void chatCtx.syncConversationLogFromServer?.(convId);
      }
      return true;
    } finally {
      // LOCK GUARANTEE: even if consumeSseResponse or the render throws, the
      // send button MUST flip back to SEND. Otherwise chatInFlight=true gets
      // stuck, input is locked — and since resume runs on every page load
      // (chat-threads.js restore), even a reload cannot recover. This was the
      // root cause of the "live lock". consumeSseResponse already unregistered the
      // stream; here only flip the composer to SEND if this conv IS DISPLAYED
      // (foreground) — a background resume must not flip the visible chat's STOP to SEND.
      if (isForegroundConv(convId)) {
        chatCtx.hooks.setStreamingUi?.(false);
      }
    }
  }

  async function streamChat(text, opts = {}) {
    // Voice conversation mode turn (voiceTurn): input already comes from voice —
    // do not cancel capture / handoff, otherwise conversation mode would cut its
    // own turn the moment it starts.
    if (!opts.voiceTurn) {
      if (window.AkanaVoice?.handoffToTextChat?.()) {
        /* wake/mic capture dropped for typed message — no "Cancelled" notification */
      } else {
        chatCtx.hooks.cancelVoiceActivity?.();
      }
    }
    // PER-CONVERSATION preserve: are we sending INTO a conversation that ALREADY has a live
    // stream (a parallel same-conv turn to queue, not replace)? This MUST be scoped to the target
    // conversation. The old GLOBAL isStreamActive() made a fresh turn in conv A skip
    // abortActiveChatStream() + ttsPlayer.reset() merely because an UNRELATED conv B was streaming
    // in the background, so A's new turn inherited B-era stale foreground TTS state. A brand-new
    // conversation has no id/stream yet → preserve=false → clean reset. abortActiveChatStream() is
    // foreground-scoped (background streams untouched) and only the FOREGROUND conv plays audio, so
    // ttsPlayer.reset() never cuts a background conversation's audio.
    const targetConvId = chatCtx.conversationIdForMemory?.() || null;
    const preserveLiveStream =
      !opts.forceImmediate && !!targetConvId && isConversationStreamActive(targetConvId);
    if (!preserveLiveStream) {
      abortActiveChatStream();
      chatCtx.hooks.ttsPlayer?.reset?.();
    }
    // VOICE HALF-DUPLEX LATCH: ttsPlayer.reset() above clears voice.ttsStreamOpen ("more
    // audio is coming"), but for a voice turn we are about to STREAM this turn's audio — the
    // latch must stay set until the backend `tts_end` (voice:tts:streamEnd). Without this the
    // server's `done` (which fires BEFORE the post-done tts_chunk drain) flips chatInFlight
    // false while audio is still pending, and a momentary play-queue drain in that window
    // re-arms the mic MID-REPLY (reading cut off → "Listening"). Re-assert it AFTER the reset
    // (order-robust vs. the finalize→submit→reset sync chain) so the drain→re-arm path waits
    // for tts_end. Foreground stream end / pre-stream error / 15s watchdog all clear it later.
    if (opts.voiceTurn) {
      try {
        window.AkanaBus?.emit?.("voice:tts:streamOpen", {});
      } catch {
        /* ignore */
      }
    }

    await ensureConversationIdReady();

    const abort = new AbortController();
    let r;
    try {
      r = await fetch(`${baseUrl()}/api/v1/chat/stream${chatCtx.hooks.streamTtsParam()}`, {
        method: "POST",
        headers: authHeaders(true),
        body: JSON.stringify(chatPayload(text, opts.voiceTurn, opts)),
        signal: abort.signal,
      });
    } catch (e) {
      const msg = humanizeChatError(e);
      if (preserveLiveStream) {
        throw new Error(
          msg.includes(window.AkanaI18n.t("transport.queue.no_connection").slice(0, 16))
            ? window.AkanaI18n.t("transport.queue.cannot_enqueue")
            : window.AkanaI18n.t("transport.queue.cannot_enqueue_msg", { msg }),
        );
      }
      throw new Error(msg);
    }

    if (r.status === 202) {
      const body = await r.json().catch(() => ({}));
      if (body && body.queued) {
        chatCtx.hooks.setQueueDepth?.(body.depth);
        chatCtx.hooks.showToast?.(window.AkanaI18n.t("transport.toast.queued"), "info");
        return { queued: true, depth: body.depth, item_id: body.item_id };
      }
    }

    if (!r.ok || !r.body) {
      const body = await r.json().catch(() => ({}));
      throw new Error(parseApiError(body, r.statusText));
    }

    // This stream's conv (ensureConversationIdReady ran → now known). The record is
    // registered inside consumeSseResponse with the same abort once streamCtx is built
    // (no AWAIT between here and consumeSseResponse → STOP cannot race before streamCtx).
    const streamConvId = chatCtx.conversationIdForMemory() || null;
    // Open the foreground UI (composer→STOP / "Responding" / orb / scene) ONLY if
    // this stream belongs to the DISPLAYED conversation — a concurrent send to another
    // chat must not flip the visible chat's composer to STOP / trigger the voice scene.
    if (isForegroundConv(streamConvId)) {
      chatCtx.hooks.setStreamingUi?.(true);
      window.AkanaTurnStatus?.begin();
      // Aurora voice scene: turn started (no tokens yet) → "Thinking".
      try {
        window.AkanaBus?.emit?.("chat:stream:start", {});
      } catch {
        /* ignore */
      }
    }

    const wrap = document.createElement("div");
    wrap.className = "row row-assistant";
    const avatar = document.createElement("div");
    avatar.className = "msg-avatar";
    avatar.setAttribute("aria-hidden", "true");
    avatar.textContent = "A";
    const msgBody = document.createElement("div");
    msgBody.className = "msg-body";
    const meta = document.createElement("div");
    meta.className = "msg-label meta";
    meta.textContent = "Akana";
    const bubble = document.createElement("div");
    bubble.className = "bubble-assistant bubble-bot bubble-bot-pending bubble-bot-stream";
    bubble.setAttribute("aria-busy", "true");
    msgBody.appendChild(meta);
    msgBody.appendChild(bubble);
    wrap.appendChild(avatar);
    wrap.appendChild(msgBody);
    // audit B6: the stream row must go into THIS conv's OWN pane (NOT the displayed
    // pane getter) → if the pane changes during the ensureConversationIdReady await
    // or in a queued send, it must NOT render into the wrong conversation.
    // logRoot=pane → the chatStreaming flag is also set+cleared on the correct pane (B1).
    const _pane = paneForConv(streamConvId);
    _pane.appendChild(wrap);
    chatCtx.hooks.updateEmptyState();
    const scroller = chatCtx.hooks.logScroll || _pane;
    const logRoot = _pane;
    if (logRoot) logRoot.dataset.chatStreaming = "1";
    // Tool-queue + SSE-queue reset is streamCtx-scoped and happens inside
    // consumeSseResponse (NO GLOBAL reset here to avoid disrupting concurrent streams).
    window.AkanaTurnStatus?.setPhase("connecting");

    /** Tool/warning nodes live in msg-body, not the row wrapper. */
    function insertBeforeBubble(node) {
      insertBeforeOrAppend(msgBody, node, bubble);
    }

    const streamStartConvId = chatCtx.conversationIdForMemory();
    const streamCtx = {
      meta,
      bubble,
      msgBody,
      scroller,
      insertBeforeBubble,
      turnId: null,
      acc: "",
      doneMeta: null,
      serverError: null,
      userText: text,
      toolPhaseActive: false,
      // Whether this stream is a voice-conversation turn — used to balance the ttsStreamOpen latch:
      // voice:tts:streamOpen is emitted for every voiceTurn, so its matching streamEnd must also
      // fire on THIS stream's own end even if it is no longer the foreground stream (else the latch
      // sticks until the 15s watchdog). Safe: voice conversation mode is single-conversation, so a
      // voiceTurn stream can never clear a DIFFERENT foreground voice turn's latch.
      voiceTurn: !!opts.voiceTurn,
      convId: streamStartConvId || null,
      // Live row reference: switching to another chat clears the log
      // (beginLogHydrate → innerHTML=""), detaching this node from the DOM while
      // the stream keeps writing to it (invisible). On return, reattachLiveRow
      // appends it to the fresh log → A's live text remains immediately visible.
      rowEl: wrap,
    };

    try {
      await consumeSseResponse(r, streamCtx, logRoot, abort, { rethrowAbort: true });
    } catch (e) {
      if (e && e.name === "AbortError") {
        cleanupStreamRow(streamCtx);
        if (!streamCtx.acc) {
          wrap.remove();
          chatCtx.hooks.updateEmptyState?.();
        }
        throw e;
      }
      throw e;
    }
    const { turnId, acc, doneMeta } = streamCtx;
    // (Record was already removed in consumeSseResponse's finally — no additional
    // global cleanup needed here; each stream manages its own per-conv record.)

    // SILENT LOSS GUARD (network drop / pre-done disconnect): stream closed CLEANLY
    // (reader done, NO exception → serverError null) but there are NO tokens (acc
    // empty) AND NO `done` event. Happens when the server sends the 200 header
    // then closes the connection BEFORE the first SSE event (turn handler crash,
    // proxy drop, pre-done persist throw). Previously this silently returned "" →
    // an empty "pending" bubble would hang in the DOM; the user would not know their
    // turn was LOST. Route through the visible error path below via a synthetic error:
    // same treatment as a case-b CONN disconnect (bubble becomes error + caller
    // records an "Error" row). Partial delivery (delta present, no done → acc non-empty)
    // does NOT pass through this gate; the success path below shows the partial text
    // and refreshes from the server.
    const serverError =
      streamCtx.serverError ||
      (!doneMeta && !acc
        ? {
            code: "EMPTY",
            message: window.AkanaI18n.t("transport.queue.no_response"),
          }
        : null);

    if (serverError) {
      // Target THIS stream's conv (not the global active-conv) — when a background
      // stream errors, it must not try to recover the visible chat's run / refresh
      // the visible chat's log.
      const convIdErr = streamCtx.convId || chatCtx.conversationIdForMemory();
      await maybeRecoverStuckActiveRun(serverError, convIdErr);
      flushStreamMarkdownUpdate(streamCtx);
      stopStreamReveal(streamCtx); // prevent a late reveal frame from overwriting the final render
      bubble.classList.remove("bubble-bot-pending");
      bubble.removeAttribute("aria-busy");
      // Append-only (do not clobber whatever segments the meta line already has):
      // guard against double-chip if a `done` event slipped in just before the error.
      if (meta && !meta.querySelector(".turn-status-chip")) {
        meta.appendChild(buildStreamStatusChip("err"));
      }
      if (acc) {
        setBubbleMarkdown(bubble, acc);
        if (convIdErr) void chatCtx.syncConversationLogFromServer(convIdErr);
        const warn = document.createElement("div");
        warn.className = "history-warn";
        warn.textContent = window.AkanaI18n.t("transport.stream.disconnected", { msg: serverError.message || serverError.code || "stream error" });
        insertBeforeBubble(warn);
        return acc;
      }
      if (streamCtx.errorCardShown) {
        // The error card (maybeRenderErrorCard) already shows this turn's error with a
        // Retry action → drop the empty pending bubble instead of duplicating the error
        // text as a second red bubble.
        bubble.remove();
        streamCtx.bubbleRemoved = true;
      } else {
        bubble.classList.add("bubble-bot-err");
        bubble.textContent = `${serverError.code || "ERR"}: ${serverError.message || "stream error"}`;
      }
      // Aurora voice scene: when the turn ends with an error before producing any text,
      // it used to freeze on "Thinking" forever (chat:stream:done is NOT emitted on
      // this path). Signal a terminal error → scene shows a warning and returns to listening.
      try {
        window.AkanaBus?.emit?.("chat:stream:error", {
          code: serverError.code,
          message: serverError.message,
        });
      } catch {
        /* ignore */
      }
      const streamErr = new Error(serverError.message || "stream error");
      // Signal to the send-path catch (chat.js) that the error is already on screen as a
      // card → it must not append a duplicate "Error" row (it still persists + sets orb).
      streamErr.errorCardShown = Boolean(streamCtx.errorCardShown);
      throw streamErr;
    }
    if (doneMeta && doneMeta.action) {
      await chatCtx.applyChatServerAction(doneMeta.action, doneMeta, {
        priorConversationId: streamStartConvId,
      });
    } else if (doneMeta && doneMeta.conversation_id) {
      adoptStreamConversationId(streamCtx, doneMeta.conversation_id);
    }
    // Store/log sync must target THIS stream's conv (NOT the global active-conv):
    // when a background stream finishes it must not overwrite the visible chat (B)'s
    // log with A's data. syncConversationLogFromServer is convId-targeted (does not
    // bleed into the active thread) → A's store thread is silently refreshed while
    // B is displayed.
    const convIdAfter = streamCtx.convId || chatCtx.conversationIdForMemory();
    if (convIdAfter && (acc || doneMeta)) {
      // Reached only on success (the serverError branch above always throws/returns
      // first) → "ok". Redundant with the `done` SSE handler's own write (belt-and-
      // suspenders); DOM tool count is recomputed the same way for consistency.
      const doneToolCount = msgBody.querySelectorAll(".tool-call").length;
      setStreamMetaText(
        meta,
        formatAssistantStreamMeta(turnId, doneMeta, doneToolCount),
        "ok",
      );
      // DOM already reflects the streamed turn — sync store in background only.
      void chatCtx.syncConversationLogFromServer(convIdAfter);
    }
    return acc;
  }


  function humanizeChatError(err) {
    const name = err && err.name ? String(err.name) : "";
    const msg = String((err && err.message) || err || "");
    const low = `${name} ${msg}`.toLowerCase();
    if (
      low.includes("failed to fetch") ||
      low.includes("networkerror") ||
      low.includes("network error") ||
      (name === "TypeError" && low.includes("fetch"))
    ) {
      return window.AkanaI18n.t("transport.queue.no_connection");
    }
    return msg || window.AkanaI18n.t("transport.unknown_error");
  }

    function chatPayload(text, voice, opts = {}) {
      const id = chatCtx.conversationIdForMemory();
      const payload = { text, conversation_id: id || null };
      // Voice conversation mode turn → tell the backend to keep the response short
      // (LLM prompt only; the stored user message is unchanged).
      if (voice) payload.voice = true;
      // Composer attachments (uploads API ids) — Phase-2 contract: field name is
      // `file_ids` (all types: image/pdf/text/…); omitted when empty.
      // b30: if the caller already consumed the ids up front (opts.fileIds, even an empty array),
      // use THAT exact set — do not re-consume here (the late consume diverged from the echo).
      // Backwards compat: paths that don't pre-consume fall back to consuming now.
      const fileIds = Array.isArray(opts.fileIds)
        ? opts.fileIds
        : chatCtx.consumePendingFileIds?.() ||
          chatCtx.consumePendingImageIds?.() ||
          [];
      if (fileIds.length) payload.file_ids = fileIds;
      // Thinking mode (fast/normal/deep) — from the composer segment; backend default
      // is "normal" so the field is omitted on missing/old ctx.
      const mode = chatCtx.thinkingMode?.();
      if (mode) payload.thinking_mode = mode;
      // Plan mode (claude plan-mode / ExitPlanMode). If opts.planMode is boolean it
      // is AUTHORITATIVE (plan card approve/revise per-turn decision: approve→false,
      // revise→true); otherwise use the composer toggle state (chatCtx.planMode).
      // Default false → field is only sent when true (never sent for old ctx/cursor).
      const planOn =
        typeof opts.planMode === "boolean" ? opts.planMode : Boolean(chatCtx.planMode?.());
      if (planOn) payload.plan_mode = true;
      return payload;
    }

    return {
      streamChat,
      sendChatBlocking,
      abortActiveChatStream,
      cancelActiveTurnOnServer,
      isStreamActive,
      isConversationStreamActive,
      activeStreamTurnId,
      setForegroundConversation,
      reattachLiveRow,
      fetchConversationTurnsFromServer,
      abortConversationTurnsFetch,
      resumeActiveTurn,
      reconcileServerCompletedTurn,
      probeActiveTurn,
      isForegroundTurnFinalized,
      ensureConversationIdReady,
      humanizeChatError,
      chatPayload,
      // Test-only seam (conversation-isolation harness): drives the live tool-card
      // queue + live-markdown throttle through two streamCtx instances to verify
      // that each receives only its own cards/text. Not used by production code.
      __test: {
        formatAssistantStreamMeta,
        queueToolCall,
        flushToolCallUpdates,
        ensureToolScratch,
        ensureMdScratch,
        scheduleStreamMarkdownThrottled,
        renderStreamMdNow,
        resetStreamMdThrottle,
        scheduleStreamMarkdownUpdate,
        flushStreamMarkdownUpdate,
        scheduleStreamScroll,
        flushStreamScroll,
        adoptStreamConversationId,
        finalizeStreamUi,
        handleChatStreamEvent,
        // ── Per-conv stream record seams (concurrent N-stream contract) ─────────
        registerStream,
        unregisterStream,
        rekeyStream,
        isForegroundStream,
        isConversationStreamActive,
        setForegroundConversation,
        getForegroundConversation: () => foregroundConvId,
        streamCount: () => _streamsByConv.size,
        recordForConv: (cid) => _streamsByConv.get(cid) || null,
        reattachLiveRow,
        abortActiveChatStream,
        // Backwards compat: old tests set up the "foreground stream" as a single
        // streamCtx. setLiveStreamCtx(ctx) → register ctx + make it foreground
        // (foregroundConvId = ctx.convId if present; otherwise mark ctx as the sole
        // record). getLiveStreamCtx → derived streamCtx of the displayed conv.
        setLiveStreamCtx: (ctx) => {
          _streamsByConv.clear();
          if (ctx) {
            registerStream(ctx, ctx._abort || null);
            foregroundConvId = ctx.convId != null ? ctx.convId : ctx._streamKey;
          } else {
            foregroundConvId = null;
          }
        },
        getLiveStreamCtx: () => getLiveStreamCtxView(),
      },
    };
  }

  window.AkanaChatTransport = { create };
})();
