/**
 * Akana pub/sub event bus — module-to-module signalling without direct refs.
 *
 * Use this instead of adding new fields to the hooks object when one module
 * needs to react to events from another. Example events:
 *   - `chat:stream:delta`     { turnId, conversationId, text }
 *   - `chat:stream:done`      { turnId, conversationId, text, latencyMs }
 *   - `chat:conversation:changed` { conversationId, source }
 *   - `voice:wake:trigger`    { source }
 *   - `voice:utterance:start` / `voice:utterance:end`
 *   - `voice:tts:start` / `voice:tts:end`
 *   - `memory:capture:staged` { count, conversationId }
 *
 * Naming: `<module>:<topic>:<verb>`. Keep payloads small and JSON-safe.
 */
(() => {
  /** @type {Map<string, Set<Function>>} */
  const subscribers = new Map();

  function on(event, handler) {
    if (typeof event !== "string" || typeof handler !== "function") {
      return () => {};
    }
    let set = subscribers.get(event);
    if (!set) {
      set = new Set();
      subscribers.set(event, set);
    }
    set.add(handler);
    return () => off(event, handler);
  }

  function off(event, handler) {
    const set = subscribers.get(event);
    if (!set) return;
    set.delete(handler);
    if (set.size === 0) subscribers.delete(event);
  }

  function once(event, handler) {
    const wrapper = (payload) => {
      off(event, wrapper);
      try {
        handler(payload);
      } catch (e) {
        console.error(`AkanaBus once handler for "${event}" threw:`, e);
      }
    };
    return on(event, wrapper);
  }

  function emit(event, payload) {
    const set = subscribers.get(event);
    if (!set || set.size === 0) return;
    // Snapshot so handlers can on/off during dispatch without surprises.
    const snapshot = Array.from(set);
    for (const fn of snapshot) {
      try {
        fn(payload);
      } catch (e) {
        console.error(`AkanaBus handler for "${event}" threw:`, e);
      }
    }
  }

  function clear(event) {
    if (event === undefined) {
      subscribers.clear();
      return;
    }
    subscribers.delete(event);
  }

  window.AkanaBus = { on, off, once, emit, clear };
})();
