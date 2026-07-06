/**
 * Contract harness (BUG 4): makeOnDelta activity fallbacks follow the active
 * language. summary-started with empty text and step-started with no label used
 * hardcoded Turkish defaults ("Özet hazırlanıyor…" / "Adım başladı") regardless
 * of the setting — an English-mode Cursor user saw Turkish in the status line.
 * Defaults must be English (English-default mandate); Turkish only when
 * language:"tr" is explicitly set.
 *
 * Imports the REAL exported makeOnDelta (no SDK needed). Exits 0 on success.
 */
import { makeOnDelta } from "../../cursor_bridge/lib.mjs";

function fail(msg) {
  console.error("FAIL: " + msg);
  process.exit(1);
}

/** Collect the activity events makeOnDelta emits for one update at a given language. */
function activityFor(update, language) {
  const events = [];
  const emit = (obj) => events.push(obj);
  const handler = makeOnDelta(emit, { onText: () => {}, onUsage: () => {}, language });
  handler({ update });
  return events.filter((e) => e.ev === "activity");
}

// --- summary-started with EMPTY text ---
const enSummary = activityFor({ type: "summary-started" }, "en");
if (enSummary.length !== 1) fail(`en summary: expected 1 activity, got ${enSummary.length}`);
if (enSummary[0].text.includes("Özet")) fail("en summary fallback leaked Turkish 'Özet'");
if (!/summary/i.test(enSummary[0].text)) fail(`en summary fallback unexpected: ${enSummary[0].text}`);

const trSummary = activityFor({ type: "summary-started" }, "tr");
if (!trSummary[0].text.includes("Özet")) {
  fail(`tr summary fallback should be Turkish: ${JSON.stringify(trSummary[0].text)}`);
}

// Default language (unspecified) must be English, not Turkish.
const defSummary = (() => {
  const events = [];
  const handler = makeOnDelta((o) => events.push(o), { onText: () => {}, onUsage: () => {} });
  handler({ update: { type: "summary-started" } });
  return events.filter((e) => e.ev === "activity");
})();
if (defSummary[0].text.includes("Özet")) fail("default (no language) summary fallback leaked Turkish");

// --- step-started with NO label ---
const enStep = activityFor({ type: "step-started" }, "en");
if (enStep.length !== 1) fail(`en step: expected 1 activity, got ${enStep.length}`);
if (enStep[0].text.includes("Adım")) fail("en step fallback leaked Turkish 'Adım'");
if (!/step/i.test(enStep[0].text)) fail(`en step fallback unexpected: ${enStep[0].text}`);

const trStep = activityFor({ type: "step-started" }, "tr");
if (!trStep[0].text.includes("Adım")) {
  fail(`tr step fallback should be Turkish: ${JSON.stringify(trStep[0].text)}`);
}

// Real text on the update is always honored regardless of language.
const withText = activityFor({ type: "summary-started", text: "Custom summary" }, "en");
if (withText[0].text !== "Custom summary") fail("explicit summary text was overridden by the fallback");

console.log("OK lib_activity_lang.harness.mjs");
process.exit(0);
