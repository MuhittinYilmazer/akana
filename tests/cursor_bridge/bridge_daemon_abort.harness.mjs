/**
 * Contract harness: the REAL bridge_daemon.mjs over stdio, with @cursor/sdk
 * replaced by a controllable fake (via the _register_loader.mjs resolve hook).
 *
 * Covers:
 *   BUG 3 — STOP during agent setup. A STOP that lands while turn A's Agent.create
 *           is still in flight must record an intent that cancels A's run when it
 *           is created; an immediately-resent turn B must NOT delete A's intent nor
 *           skip serialization, and B must reuse the SAME cached agent (no leaked
 *           second agent). Assert: A's run.cancel fired, exactly ONE agent created.
 *   BUG 8 — stdin EOF exits the daemon (the process ends when the parent's stdin
 *           writer is gone). Asserted implicitly: the daemon exits 0 after stdin end.
 *
 * Exits 0 on success, 1 with a message on failure. No Cursor account needed.
 */
import { spawn } from "node:child_process";
import { mkdtempSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(__dirname, "..", "..");
const DAEMON = path.join(REPO, "cursor_bridge", "bridge_daemon.mjs");
const REGISTER = pathToFileURL(path.join(__dirname, "_register_loader.mjs")).href;

function fail(msg) {
  console.error("FAIL: " + msg);
  process.exit(1);
}

function readLog(logPath) {
  if (!existsSync(logPath)) return [];
  return readFileSync(logPath, "utf8")
    .split("\n")
    .filter((l) => l.trim())
    .map((l) => JSON.parse(l));
}

/** Run the daemon, feed a scripted sequence of {delay, line} steps, then end stdin. */
function runDaemon({ env, steps, endAfterMs }) {
  return new Promise((resolve) => {
    const proc = spawn("node", ["--import", REGISTER, DAEMON], {
      env: { ...process.env, CURSOR_API_KEY: "x", ...env },
      stdio: ["pipe", "pipe", "pipe"],
    });
    const out = [];
    let buf = "";
    proc.stdout.on("data", (d) => {
      buf += d.toString();
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i).trim();
        buf = buf.slice(i + 1);
        if (line) {
          try {
            out.push(JSON.parse(line));
          } catch {
            /* ignore non-JSON */
          }
        }
      }
    });
    let elapsed = 0;
    for (const step of steps) {
      elapsed += step.delay;
      setTimeout(() => {
        try {
          proc.stdin.write(JSON.stringify(step.line) + "\n");
        } catch {
          /* pipe gone */
        }
      }, elapsed);
    }
    setTimeout(() => {
      try {
        proc.stdin.end();
      } catch {
        /* ignore */
      }
    }, endAfterMs);
    const killTimer = setTimeout(() => proc.kill("SIGKILL"), endAfterMs + 4000);
    proc.on("exit", (code) => {
      clearTimeout(killTimer);
      resolve({ code, out });
    });
  });
}

async function testStopMidSetup() {
  const logDir = mkdtempSync(path.join(tmpdir(), "akana-abort-"));
  const logPath = path.join(logDir, "sdk.log");
  const session = "conv-stop";

  // Agent.create is slow (300ms) → the abort_run at 60ms lands mid-setup (before
  // the run is registered). Turn B is sent at 120ms — while A is STILL setting up.
  const { code, out } = await runDaemon({
    env: {
      AKANA_FAKE_SDK_LOG: logPath,
      AKANA_FAKE_CREATE_DELAY: "300",
      AKANA_FAKE_WAIT_DELAY: "40",
    },
    steps: [
      { delay: 0, line: { id: "1", op: "run", stream: true, prompt: "stopped prompt", session_key: session } },
      { delay: 60, line: { id: "a", op: "abort_run", session_key: session } },
      { delay: 60, line: { id: "2", op: "run", stream: true, prompt: "corrected prompt", session_key: session } },
    ],
    endAfterMs: 1600,
  });

  if (code !== 0) fail(`daemon did not exit 0 on stdin EOF (BUG 8); code=${code}`);

  const log = readLog(logPath);
  const creates = log.filter((e) => e.event === "agent.create");
  const cancels = log.filter((e) => e.event === "run.cancel");

  // BUG 3 core: turn A's run must have been CANCELLED (the honored mid-setup intent).
  if (cancels.length < 1) {
    fail(
      "the STOPped turn A's run was never cancelled — abort intent was lost " +
        `(cancels=${JSON.stringify(cancels)}, log=${JSON.stringify(log)})`,
    );
  }

  // BUG 3 leak guard: only ONE agent for the session (B reused A's cached agent;
  // it did not race a second Agent.create that would orphan one uncached).
  if (creates.length !== 1) {
    fail(
      `expected exactly ONE agent for the session (B reuses A's), got ${creates.length}: ` +
        JSON.stringify(creates),
    );
  }

  // At least one turn must have reached a terminal (the daemon kept serving after abort).
  const terminals = out.filter((e) => e.ev === "done" || e.ev === "error");
  if (terminals.length < 1) fail("no terminal event emitted after the abort");
}

async function testStdinEofExits() {
  // No run at all — just open the daemon and close stdin. Must exit promptly (BUG 8).
  const { code } = await runDaemon({
    env: {},
    steps: [{ delay: 0, line: { id: "p", op: "ping" } }],
    endAfterMs: 150,
  });
  if (code !== 0) fail(`daemon did not exit 0 after stdin EOF with no work (BUG 8); code=${code}`);
}

await testStopMidSetup();
await testStdinEofExits();
console.log("OK bridge_daemon_abort.harness.mjs");
process.exit(0);
