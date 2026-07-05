/**
 * Per-conversation DOM pane manager contract — backend-free, node-vm + fake DOM.
 *
 * Core invariant (the root of the parallel-chat bug class): DELETING/rebuilding
 * one conversation's pane must NOT TOUCH OTHER conversations' panes (including
 * live stream rows). The old single-`#log` model wiped them all on every switch
 * → the background stream was lost. This test locks pane isolation + persistence
 * across switch + empty-new conversation rekey.
 *
 * Run: node tests/web/chat_panes.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const PANES_SRC = readFileSync(path.join(REPO, "web_ui/static/akana-chat-panes.js"), "utf8");

let passed = 0;
function check(label, fn) {
  fn();
  passed += 1;
  void label;
}

// ── Fake DOM node (only the surfaces PaneManager touches) ───────────────
function makeEl(tag = "div") {
  return {
    tagName: String(tag).toUpperCase(),
    children: [],
    attrs: {},
    hidden: false,
    _html: null,
    className: "",
    get innerHTML() { return this._html; },
    set innerHTML(v) { this._html = String(v); if (v === "") this.children = []; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k] ?? null; },
    get dataset() { const a = this.attrs; return { get convId() { return a["data-conv-id"]; } }; },
    appendChild(n) { this.children.push(n); n._parent = this; return n; },
    remove() { const p = this._parent; if (p) p.children = p.children.filter((c) => c !== this); this._parent = null; },
  };
}

const ctx = { console };
ctx.window = ctx;
vm.runInNewContext(PANES_SRC, ctx);
assert.ok(ctx.window.AkanaChatPanes?.createPaneManager, "AkanaChatPanes.createPaneManager should load");

function setup() {
  const container = makeEl("div");
  const pm = ctx.window.AkanaChatPanes.createPaneManager({
    container,
    createEl: (t) => makeEl(t),
  });
  return { container, pm };
}

// ── 1. paneFor: create + key + start hidden ────────────────────────────────
check("paneFor: creates conv pane, appends to container, hidden + data-conv-id set", () => {
  const { container, pm } = setup();
  const a = pm.paneFor("conv-A");
  assert.ok(a, "pane should be returned");
  assert.equal(a.className, "conv-pane", "class should be conv-pane");
  assert.equal(a.getAttribute("data-conv-id"), "conv-A", "data-conv-id should be filled with the key");
  assert.equal(a.hidden, true, "pane should start hidden (opened via show)");
  assert.ok(container.children.includes(a), "pane should be appended to container");
  // A second call must return the SAME element (doesn't recreate).
  assert.equal(pm.paneFor("conv-A"), a, "same pane for the same conv (idempotent)");
  assert.equal(pm.count(), 1, "there should be a single pane");
});

// ── 2. show: show the target, hide the others, update displayed ─────────────
check("show: target pane visible, others hidden, displayedConvId correct", () => {
  const { pm } = setup();
  const a = pm.show("conv-A");
  const b = pm.paneFor("conv-B");
  assert.equal(a.hidden, false, "A should be visible once shown");
  assert.equal(b.hidden, true, "B still hidden");
  assert.equal(pm.displayedConvId(), "conv-A", "displayedConvId should be A");
  pm.show("conv-B");
  assert.equal(a.hidden, true, "A should be hidden after switching to B");
  assert.equal(b.hidden, false, "B should be visible");
  assert.equal(pm.displayedConvId(), "conv-B", "displayedConvId should be B");
  assert.equal(pm.displayedPane(), b, "displayedPane should be B");
});

// ── 3. CORE: pane PERSISTS across switch (same element, content preserved) ────────────
check("PERSISTENCE: on A→B→A switch, A's pane + content are preserved as-is", () => {
  const { pm } = setup();
  const a = pm.show("conv-A");
  const liveRow = makeEl("div"); // simulate A's live stream row
  a.appendChild(liveRow);
  assert.equal(a.children.length, 1, "A has one row");
  pm.show("conv-B"); // switch to another conversation
  // OLD BUG: here #log.innerHTML="" would have deleted A's row.
  assert.equal(a.children.length, 1, "switching to B must NOT delete A's row (pane persists)");
  const a2 = pm.show("conv-A"); // switch back
  assert.equal(a2, a, "the SAME pane element on switching back (no recreation)");
  assert.equal(a2.children[0], liveRow, "A's live row is in place — reattach NOT required");
  assert.equal(a.hidden, false, "A visible again");
});

// ── 4. CORE: clear ISOLATION — clear one pane, the other unaffected ───────
check("ISOLATION: clear(A) empties only A, B's live row is UNTOUCHED", () => {
  const { pm } = setup();
  const a = pm.show("conv-A");
  const b = pm.paneFor("conv-B");
  a.appendChild(makeEl("div"));
  const bRow = makeEl("div");
  b.appendChild(bRow); // B is streaming in the background
  pm.clear("conv-A"); // rebuild A (hydrate)
  assert.equal(a.children.length, 0, "clear(A) should empty A");
  assert.equal(b.children.length, 1, "B's row must be PRESERVED (no cross-conv deletion — bug fix)");
  assert.equal(b.children[0], bRow, "B's live row is the same");
});

// ── 5. rekey: pane is moved when an empty-new conversation gets a server id ─────────────
check("rekey: empty-new conversation pane (null) is moved to a real conv-id, content preserved", () => {
  const { pm } = setup();
  const empty = pm.show(null); // empty-new conversation (no id yet)
  const row = makeEl("div");
  empty.appendChild(row);
  assert.equal(pm.displayedConvId(), "", "empty-new conversation displayedConvId is empty string");
  const ok = pm.rekey(null, "conv-NEW");
  assert.equal(ok, true, "rekey should succeed");
  assert.equal(pm.paneFor("conv-NEW"), empty, "the same pane is now under the conv-NEW key");
  assert.equal(empty.children[0], row, "rekey should preserve content (live row not lost)");
  assert.equal(empty.getAttribute("data-conv-id"), "conv-NEW", "data-conv-id should be updated to the real id");
  assert.equal(pm.displayedConvId(), "conv-NEW", "displayed should also move to the new id");
  assert.equal(pm.has(""), false, "the old empty-new key should not remain");
});

check("rekey: if the target id already has a pane, the old one is dropped (single-pane invariant)", () => {
  const { pm, container } = setup();
  const stale = pm.paneFor("conv-X");
  const empty = pm.show(null);
  empty.appendChild(makeEl("div"));
  pm.rekey(null, "conv-X"); // X already has a pane
  assert.equal(pm.paneFor("conv-X"), empty, "the new (empty-sourced) pane should take over X");
  assert.equal(container.children.includes(stale), false, "the old X pane should be removed from the DOM");
  assert.equal(pm.count(), 1, "a single pane should remain (no duplicate X)");
});

// ── 6. remove: the pane goes away when the conversation is deleted ───────────────────────────────────
check("remove: pane goes away from the DOM and the map; displayed is cleared if it was displayed", () => {
  const { container, pm } = setup();
  pm.show("conv-A");
  pm.paneFor("conv-B");
  assert.equal(pm.count(), 2);
  const removed = pm.remove("conv-A");
  assert.equal(removed, true, "remove should return true");
  assert.equal(pm.has("conv-A"), false, "A should be gone from the map");
  assert.equal(pm.count(), 1, "a single pane should remain");
  assert.equal(container.children.length, 1, "A should be gone from the DOM");
  assert.equal(pm.displayedPane(), null, "displayed is cleared if the displayed one was removed");
});

// ── 7. multiple panes concurrent (parallel stream) grow independently ─────────────────────
check("parallel: 3 conversation panes are independent, only one visible", () => {
  const { pm } = setup();
  pm.paneFor("A"); pm.paneFor("B"); pm.paneFor("C");
  pm.show("B");
  assert.equal(pm.count(), 3, "three panes coexist");
  const vis = ["A", "B", "C"].filter((id) => !pm.paneFor(id).hidden);
  assert.deepEqual(vis, ["B"], "only the displayed pane should be open");
});

// ── 8. R4-G #2: LRU ceiling — old panes are evicted when many conversations are visited ─────────
function setupLru(maxPanes, isProtected) {
  const container = makeEl("div");
  const pm = ctx.window.AkanaChatPanes.createPaneManager({
    container, createEl: (t) => makeEl(t), maxPanes, isProtected,
  });
  return { container, pm };
}

check("LRU: when the ceiling is exceeded the oldest (LRU) pane is evicted; count stays at the ceiling", () => {
  const { pm } = setupLru(3);
  pm.show("A"); pm.show("B"); pm.show("C"); pm.show("D"); pm.show("E");
  assert.ok(pm.count() <= 3, `pane count should stay at the ceiling (<=3), got ${pm.count()}`);
  assert.equal(pm.has("A"), false, "the oldest A should be evicted");
  assert.equal(pm.has("B"), false, "B should also be evicted");
  assert.equal(pm.has("E"), true, "the newest (displayed) E should stay");
});

check("LRU: the DISPLAYED pane is never evicted (recency moved to the end)", () => {
  const { pm } = setupLru(2);
  pm.show("A");
  pm.show("B");
  pm.show("A"); // switch back to A → A becomes the freshest
  pm.show("C"); // ceiling exceeded → the oldest (B) should be evicted, A (just visited) should stay
  assert.equal(pm.has("B"), false, "the intervening B (LRU) should be evicted");
  assert.equal(pm.has("A"), true, "the re-visited A should be preserved (recency)");
  assert.equal(pm.has("C"), true, "the displayed C should be preserved");
});

check("LRU: a protected (actively streaming) pane is NOT evicted", () => {
  const streaming = new Set(["A"]); // A is streaming in the background → protected
  const { pm } = setupLru(2, (id) => streaming.has(id));
  pm.show("A");
  const aPane = pm.paneFor("A");
  const liveRow = makeEl("div");
  aPane.appendChild(liveRow); // A's live row
  pm.show("B"); pm.show("C"); pm.show("D"); // visit a lot
  assert.equal(pm.has("A"), true, "streaming A is protected → should not be evicted");
  assert.equal(pm.paneFor("A").children[0], liveRow, "A's live row should be preserved");
  // When the stream ends the protection lifts → can be evicted normally.
  streaming.delete("A");
  pm.show("E"); pm.show("F");
  assert.equal(pm.has("A"), false, "once the stream ends A can be evicted");
});

console.log(`chat_panes.harness: ${passed} pane contracts PASSED ✓`);
