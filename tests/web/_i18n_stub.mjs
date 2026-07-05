/**
 * Shared AkanaI18n stub for node-vm contract harnesses.
 *
 * Web modules (chat-render/transport/threads/settings…) call
 * `window.AkanaI18n.t("key", params)` for every user-facing string. The harnesses
 * load those modules in a bare VM, so they must provide an `AkanaI18n` or the
 * module throws «Cannot read properties of undefined (reading 't')».
 *
 * This builds the SAME dictionary the browser uses by eval-ing the real
 * `akana-i18n-strings*.js` tables (each merges into `window.AkanaI18nStrings`),
 * then exposes an English-first `t()` (engine default lang = "en"). Harness
 * assertions therefore see the real rendered English text — keeping the contract
 * meaningful instead of asserting raw i18n keys.
 */
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const STATIC = path.resolve(__dirname, "../../web_ui/static");

let CACHED_DICT = null;

/** Eval every akana-i18n-strings*.js into one merged { key: { en, tr } } map. */
function loadDict() {
  if (CACHED_DICT) return CACHED_DICT;
  const ctx = { window: {} };
  ctx.window.window = ctx.window;
  vm.createContext(ctx);
  for (const f of readdirSync(STATIC).filter((n) => /^akana-i18n-strings.*\.js$/.test(n)).sort()) {
    vm.runInContext(readFileSync(path.join(STATIC, f), "utf8"), ctx);
  }
  CACHED_DICT = ctx.window.AkanaI18nStrings || {};
  return CACHED_DICT;
}

/**
 * @param {"en"|"tr"} lang  active language (default "en", the product default)
 * @returns {{DICT: object, t: (key: string, params?: object) => string}}
 */
export function makeI18nStub(lang = "en") {
  const DICT = loadDict();
  const t = (key, params) => {
    const entry = DICT[key];
    let s = entry ? entry[lang] ?? entry.en ?? key : key;
    if (params) for (const k in params) s = s.split(`{${k}}`).join(String(params[k]));
    return s;
  };
  return { DICT, t };
}
