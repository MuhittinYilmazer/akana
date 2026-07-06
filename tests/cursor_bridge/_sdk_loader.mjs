/**
 * ESM resolve hook: redirect ``@cursor/sdk`` imports to the local fake so the
 * real ``bridge_daemon.mjs`` can run over stdio in tests with no Cursor account.
 * Registered via ``node --import ./_register_loader.mjs bridge_daemon.mjs``.
 */
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FAKE = pathToFileURL(path.join(__dirname, "_fake_cursor_sdk.mjs")).href;

export async function resolve(specifier, context, nextResolve) {
  if (specifier === "@cursor/sdk") {
    return { url: FAKE, shortCircuit: true };
  }
  return nextResolve(specifier, context);
}
