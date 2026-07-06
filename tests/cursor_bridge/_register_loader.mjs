/** Registers the @cursor/sdk redirect loader (used via node --import). */
import { register } from "node:module";

// Resolve the loader relative to THIS module's URL (a proper file:// URL) so it
// works regardless of the process cwd and on Windows (raw drive paths are not
// valid ESM URL schemes).
register("./_sdk_loader.mjs", import.meta.url);
