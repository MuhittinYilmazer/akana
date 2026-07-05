#!/usr/bin/env node
/**
 * Cursor model catalog bridge — calls Cursor.models.list() with CURSOR_API_KEY.
 * stdout: single JSON line { ok:true, models:[...] } or { ok:false, error }
 */
import "./dispose-polyfill.mjs"; // Node 18: define Symbol.dispose BEFORE the SDK loads
import { Cursor } from "@cursor/sdk";

async function main() {
  const apiKey = process.env.CURSOR_API_KEY || "";
  if (!apiKey) {
    console.log(JSON.stringify({ ok: false, error: "CURSOR_API_KEY is not set" }));
    process.exit(1);
  }
  try {
    const models = await Cursor.models.list({ apiKey });
    console.log(JSON.stringify({ ok: true, models: models ?? [] }));
  } catch (err) {
    const msg = err?.message || String(err);
    console.log(JSON.stringify({ ok: false, error: msg }));
    process.exit(1);
  }
}

await main();
