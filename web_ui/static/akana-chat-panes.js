/**
 * Akana chat — PER-CONVERSATION DOM PANE MANAGER (parallel-chat core).
 *
 * Old model: a single `#log` was WIPED with `innerHTML=""` on every conversation
 * switch; the live row of a background stream was DETACHED by this wipe, then
 * `reattachLiveRow` tried (and usually failed — key mismatch) to re-insert it
 * → "no response after switching to new chat + crash" (user report).
 * Root cause: N live conversations were all projected into ONE deleted log.
 *
 * New model: each conversation lives in its OWN `<div class="conv-pane" data-conv-id>`
 * pane. `#log` is just the container that holds the panes. Switching = show/hide
 * a pane (CSS), NO `innerHTML=""` EVER. Even if one conversation's pane is cleared
 * (clear/rebuild), OTHER conversations' panes (and their live streams) are UNTOUCHED.
 * This makes detach/reattach, orphan-mutation, and the "foreground gate" family
 * unnecessary.
 *
 * Pure + dependency-free: createEl + container are injected → testable in node-vm
 * (see tests/web/chat_panes.harness.mjs). In production, wired with
 * document.createElement + #log (akana-shell.js).
 */
(() => {
  // Pane key for a conversation that does NOT yet have a server conv-id (empty/new).
  // Once a conversation acquires an id, rekey moves it to the real id. Only one
  // "new empty chat" exists at a time (canReuseCurrentEmptyThread), so one sentinel suffices.
  const NEW_CHAT_KEY = "\u0000new-chat";

  function normKey(convId) {
    const s = convId == null ? "" : String(convId);
    return s === "" ? NEW_CHAT_KEY : s;
  }

  //: LRU cap: if more conversations than this are visited in one session, the oldest
  //: hidden panes are evicted (prevents the DOM + hydration tree from growing unboundedly).
  const DEFAULT_MAX_PANES = 12;

  /**
   * @param {object} opts
   * @param {object} opts.container  Container that panes are appended to (#log).
   * @param {(tag:string)=>object} opts.createEl  document.createElement equivalent.
   * @param {number} [opts.maxPanes]  Maximum number of panes to keep in memory (LRU).
   * @param {(convId:string)=>boolean} [opts.isProtected]  Conversations whose pane is
   *   NEVER evicted (e.g. a conversation with an active background stream — must not
   *   detach the live row). Default: nothing is protected.
   */
  function createPaneManager({ container, createEl, maxPanes, isProtected }) {
    if (!container || typeof createEl !== "function") {
      throw new Error("createPaneManager: container + createEl gerekli");
    }
    const panes = new Map(); // normKey -> pane el (insertion order = LRU: oldest first)
    const _maxPanes = Math.max(2, Number(maxPanes) || DEFAULT_MAX_PANES);
    const _isProtected = typeof isProtected === "function" ? isProtected : () => false;
    let displayedKey = null;

    function makePane(key) {
      const el = createEl("div");
      el.className = "conv-pane";
      // data-conv-id: the real id if present; empty string for the new-chat pane.
      el.setAttribute("data-conv-id", key === NEW_CHAT_KEY ? "" : key);
      el.hidden = true;
      container.appendChild(el);
      return el;
    }

    /** Return the pane for convId (creating it if absent). null/empty → new-chat sentinel. */
    function paneFor(convId) {
      const key = normKey(convId);
      let el = panes.get(key);
      if (!el) {
        el = makePane(key);
        panes.set(key, el);
      }
      return el;
    }

    /**
     * LRU eviction: when the cap is exceeded, evict the oldest HIDDEN panes from
     * the DOM and the map. Never evicted: the displayed pane, the new-chat sentinel,
     * and ``isProtected`` convs (those with active background streams — must not
     * detach the live row). Evicted panes are re-hydrated from store/server on
     * re-visit → NO CONTENT LOSS.
     */
    function evictLru() {
      if (panes.size <= _maxPanes) return;
      for (const [k, el] of [...panes]) {
        if (panes.size <= _maxPanes) break;
        if (k === displayedKey || k === NEW_CHAT_KEY) continue;
        if (_isProtected(k)) continue;
        try { el.remove(); } catch { /* ignore */ }
        panes.delete(k);
      }
    }

    /** SHOW convId: hide all other panes, show this one. Returns the pane element. */
    function show(convId) {
      const key = normKey(convId);
      const target = paneFor(convId); // creates if absent
      // LRU recency: move the displayed key to the end (most recent) → eviction drops it last.
      if (panes.has(key)) {
        panes.delete(key);
        panes.set(key, target);
      }
      for (const [k, el] of panes) el.hidden = k !== key;
      displayedKey = key;
      evictLru();
      return target;
    }

    /** Currently displayed pane (null if none). Foreground append/wipe target. */
    function displayedPane() {
      return displayedKey != null ? panes.get(displayedKey) || null : null;
    }

    /** Displayed conv id (new-chat → "" ; nothing displayed → null). */
    function displayedConvId() {
      if (displayedKey == null) return null;
      return displayedKey === NEW_CHAT_KEY ? "" : displayedKey;
    }

    /**
     * Clear ONLY this conversation's pane content (for rebuild/hydrate). Does NOT
     * touch other panes — replaces the old `#log.innerHTML=""` global wipe;
     * background streams are now safe. The pane element ITSELF is preserved (only
     * its children are cleared) → existing references remain valid.
     */
    function clear(convId) {
      const el = paneFor(convId);
      el.innerHTML = "";
      return el;
    }

    /** Remove the pane from the DOM and the map (conversation deleted/archived). */
    function remove(convId) {
      const key = normKey(convId);
      const el = panes.get(key);
      if (!el) return false;
      try { el.remove(); } catch { /* ignore */ }
      panes.delete(key);
      if (displayedKey === key) displayedKey = null;
      return true;
    }

    /**
     * Move the pane key (called when a new-chat conversation receives a server conv-id:
     * rekey(null|"" , realId) ). oldKey's pane is RE-KEYED; content + element are
     * PRESERVED (including the live stream row). If a pane already exists at newKey,
     * drop the old one to prevent collisions (new key wins). data-conv-id is updated.
     * @returns {boolean} true if moved.
     */
    function rekey(oldConvId, newConvId) {
      const oldKey = normKey(oldConvId);
      const newKey = normKey(newConvId);
      if (oldKey === newKey) return false;
      const el = panes.get(oldKey);
      if (!el) return false;
      // If a different pane already exists at the target key, remove it (single-pane invariant).
      if (panes.has(newKey) && panes.get(newKey) !== el) {
        try { panes.get(newKey).remove(); } catch { /* ignore */ }
        panes.delete(newKey);
      }
      panes.delete(oldKey);
      panes.set(newKey, el);
      el.setAttribute("data-conv-id", newKey === NEW_CHAT_KEY ? "" : newKey);
      if (displayedKey === oldKey) displayedKey = newKey;
      return true;
    }

    function has(convId) { return panes.has(normKey(convId)); }
    function count() { return panes.size; }

    return {
      paneFor,
      show,
      displayedPane,
      displayedConvId,
      clear,
      remove,
      rekey,
      has,
      count,
      // Test/diagnostics: key list.
      _keys: () => [...panes.keys()].map((k) => (k === NEW_CHAT_KEY ? "" : k)),
      NEW_CHAT_KEY,
    };
  }

  window.AkanaChatPanes = { createPaneManager, NEW_CHAT_KEY };
})();
