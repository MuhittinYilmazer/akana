/**
 * Runtime settings form contract (akana-settings.js) — backend-free, node-vm.
 * The backend schema (GET /api/v1/settings/runtime) is the single source of
 * truth; the UI form is produced by the pure _runtimeFormModel function. This
 * harness:
 * - are the runtime REST paths + connectors path + dynamic-tab markers in the source?
 * - is _runtimeFormModel exported and PURE (same input → same model, no DOM)?
 * - type → input kind mapping: bool→checkbox, int→number(step 1),
 *   float→number(step any), csv→text(", " join), paths→text("; " join)?
 * - are the source badge labels (runtime→setting, env→env, default→default) and
 *   the restart_required flag carried into the model?
 * - is an empty category filtered out, does a malformed payload yield an empty model?
 * Run: node tests/web/runtime_settings_contract.harness.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";
import { makeI18nStub } from "./_i18n_stub.mjs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "../..");
const src = readFileSync(path.join(REPO, "web_ui/static/akana-settings.js"), "utf8");

// ── source level: REST paths + dynamic-tab markers ────────────────
for (const marker of [
  "/api/v1/settings/runtime",
  "/api/v1/settings/runtime/reset/",
  "/api/v1/connectors",
  "settings-pane-runtime",
  "injectDynamicSettingsChrome",
  "restart_required",
]) {
  assert.ok(src.includes(marker), `missing marker in akana-settings.js: ${marker}`);
}

// ── load in the stub DOM (same skeleton as settings_ws_contract) ───────────────
const ctx = {
  window: {
    AkanaCore: {
      LS_BASE: "akana.baseUrl",
      LS_TOKEN: "akana.token",
      showToast: () => {},
      escapeHtml: (s) => String(s),
      baseUrl: () => "http://x",
      authHeaders: () => ({}),
      parseApiError: () => "",
      configure: () => {},
    },
    AkanaBus: { emit: () => {} },
    AkanaI18n: makeI18nStub(),
  },
  document: {
    body: { classList: { contains: () => false } },
    getElementById: () => null,
    querySelector: () => null,
    querySelectorAll: () => [],
    addEventListener: () => {},
    documentElement: { dataset: {} },
  },
  navigator: {},
};
ctx.window.document = ctx.document;
vm.runInNewContext(src, ctx);

const settings = ctx.window.AkanaSettings;
assert.ok(settings, "window.AkanaSettings failed to load");
assert.equal(typeof settings._runtimeFormModel, "function", "_runtimeFormModel must be exported");
assert.equal(typeof settings.loadRuntimeSettings, "function", "loadRuntimeSettings must be exported");
assert.equal(typeof settings.loadConnectors, "function", "loadConnectors must be exported");

// ── pure model: schema → form fields per category ─────────────────────────
const payload = {
  categories: [
    { id: "maliyet", label: "Maliyet & Bütçe" },
    { id: "telegram", label: "Telegram" },
    { id: "bos", label: "Boş Kategori" },
  ],
  settings: [
    {
      key: "session_closer_interval",
      label: "Oturum kapatıcı aralığı",
      description: "Tick periyodu.",
      category: "maliyet",
      type: "float",
      env_var: "AKANA_SESSION_CLOSER_INTERVAL",
      min: 0,
      max: 200000,
      unit: "sn",
      restart_required: false,
      value: 25,
      source: "default",
    },
    {
      key: "session_closer_enabled",
      label: "Oturum kapatıcı",
      description: "Tick kapısı.",
      category: "maliyet",
      type: "bool",
      env_var: "AKANA_SESSION_CLOSER_ENABLED",
      restart_required: false,
      value: true,
      source: "env",
    },
    {
      key: "skill_inject_max",
      label: "Tur başına beceri",
      description: "Üst sınır.",
      category: "maliyet",
      type: "int",
      env_var: "AKANA_SKILL_INJECT_MAX",
      min: 1,
      max: 10,
      restart_required: false,
      value: 6,
      source: "runtime",
    },
    {
      key: "file_roots",
      label: "Dosya kökleri",
      description: "Allowlist.",
      category: "telegram",
      type: "paths",
      env_var: "AKANA_FILE_ROOTS",
      restart_required: false,
      value: ["/a", "/b"],
      source: "runtime",
    },
    {
      key: "telegram_allowed_chat_ids",
      label: "İzinli chat'ler",
      description: "Allowlist.",
      category: "telegram",
      type: "csv",
      env_var: "AKANA_TELEGRAM_ALLOWED_CHAT_IDS",
      restart_required: true,
      value: ["1", "2"],
      source: "default",
    },
    {
      // Enum setting (e.g. gemini_live_voice) → <select> (options win).
      key: "voice_pick",
      label: "Ses",
      description: "Enum.",
      category: "telegram",
      type: "str",
      env_var: "AKANA_VOICE_PICK",
      restart_required: false,
      options: ["Charon", "Puck"],
      value: "Puck",
      source: "runtime",
    },
  ],
};

// The vm context is a separate realm: compare via JSON instead of prototype-sensitive deepStrictEqual.
const asJson = (v) => JSON.stringify(v);
const model = settings._runtimeFormModel(payload);
assert.equal(model.length, 2, "empty category must be filtered out");
assert.equal(asJson(model.map((c) => c.id)), asJson(["maliyet", "telegram"]));
const maliyet = model[0];
assert.equal(maliyet.fields.length, 3);

const byKey = Object.fromEntries(model.flatMap((c) => c.fields).map((f) => [f.key, f]));

// type → input kind
assert.equal(byKey.session_closer_interval.input.kind, "number");
assert.equal(byKey.session_closer_interval.input.step, "any", "float → step any");
assert.equal(byKey.session_closer_interval.unit, "sec", "unit i18n EN (runtime.unit.sn)");
assert.equal(byKey.skill_inject_max.input.kind, "number");
assert.equal(byKey.skill_inject_max.input.step, "1", "int → step 1");
assert.equal(byKey.skill_inject_max.input.min, 1);
assert.equal(byKey.skill_inject_max.input.max, 10);
assert.equal(byKey.session_closer_enabled.input.kind, "checkbox");
assert.equal(byKey.session_closer_enabled.input.checked, true);
assert.equal(byKey.file_roots.input.kind, "text");
assert.equal(byKey.file_roots.input.value, "/a; /b", "paths must be joined with '; ' (OS-independent; backend receives an array)");
assert.equal(byKey.telegram_allowed_chat_ids.input.value, "1, 2", "csv must be joined with ', '");
// Enum → select (options win, a wrong value can't be written)
assert.equal(byKey.voice_pick.input.kind, "select", "options → select");
assert.equal(asJson(byKey.voice_pick.input.options), asJson(["Charon", "Puck"]));
assert.equal(byKey.voice_pick.input.value, "Puck", "selected value is preserved");

// source badges + restart flag
assert.equal(byKey.session_closer_interval.sourceLabel, "default", "source label i18n EN");
assert.equal(byKey.session_closer_enabled.sourceLabel, "env");
assert.equal(byKey.skill_inject_max.sourceLabel, "setting", "source label i18n EN");
assert.equal(byKey.telegram_allowed_chat_ids.restartRequired, true);
assert.equal(byKey.skill_inject_max.restartRequired, false);

// pure: same input → same model; malformed input → empty model (no exception)
assert.equal(asJson(settings._runtimeFormModel(payload)), asJson(model));
assert.equal(asJson(settings._runtimeFormModel(null)), "[]");
assert.equal(asJson(settings._runtimeFormModel({})), "[]");
assert.equal(asJson(settings._runtimeFormModel({ categories: [{ id: "x" }] })), "[]");

console.log("runtime settings contract test: OK");
