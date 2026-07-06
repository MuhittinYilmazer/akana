/**
 * Fake ``@cursor/sdk`` for the bridge_daemon stdio harnesses.
 *
 * Redirected in place of the real SDK via ``_sdk_loader.mjs`` (an ESM resolve
 * hook) so the real ``bridge_daemon.mjs`` runs end-to-end over stdio WITHOUT a
 * Cursor account. All observable effects (agents created, runs cancelled) are
 * appended to the JSON-lines file named by ``AKANA_FAKE_SDK_LOG`` so the Python/
 * node driver can assert them after the daemon exits.
 *
 * Timing knobs (env, ms):
 *   AKANA_FAKE_CREATE_DELAY  — delay inside Agent.create (opens the mid-setup
 *                              window an abort_run must land in).
 *   AKANA_FAKE_WAIT_DELAY    — delay inside run.wait() before it resolves.
 */
import { appendFileSync } from "node:fs";

const LOG = process.env.AKANA_FAKE_SDK_LOG || "";
const CREATE_DELAY = Number(process.env.AKANA_FAKE_CREATE_DELAY || "0");
const WAIT_DELAY = Number(process.env.AKANA_FAKE_WAIT_DELAY || "0");

let agentSeq = 0;
let runSeq = 0;

function record(entry) {
  if (!LOG) return;
  try {
    appendFileSync(LOG, JSON.stringify(entry) + "\n");
  } catch {
    /* best effort */
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, Math.max(0, ms)));

export class CursorAgentError extends Error {
  constructor(message, opts = {}) {
    super(message);
    this.name = "CursorAgentError";
    this.code = opts.code;
    this.isRetryable = Boolean(opts.isRetryable);
    this.status = opts.status;
  }
}

class FakeRun {
  constructor(agentId) {
    this.id = `run-${++runSeq}`;
    this.agentId = agentId;
    this._cancelled = false;
    this._done = false;
  }

  async cancel() {
    this._cancelled = true;
    record({ event: "run.cancel", run_id: this.id, agent_id: this.agentId });
  }

  async wait() {
    // Resolve after a delay; if cancelled, resolve with status "cancelled"
    // (the SDK RESOLVES rather than rejecting — matching run.d.ts).
    const step = 10;
    let waited = 0;
    while (!this._cancelled && waited < WAIT_DELAY) {
      await sleep(step);
      waited += step;
    }
    this._done = true;
    const status = this._cancelled ? "cancelled" : "finished";
    record({ event: "run.wait", run_id: this.id, status });
    return { id: this.id, agentId: this.agentId, status, result: this._cancelled ? "" : "ok" };
  }
}

class FakeAgent {
  constructor() {
    this.agentId = `agent-${++agentSeq}`;
    record({ event: "agent.create", agent_id: this.agentId });
  }

  async send(_message, opts = {}) {
    record({ event: "agent.send", agent_id: this.agentId });
    // A tiny bit of streamed text so the acc path is exercised.
    try {
      opts?.onDelta?.({ update: { type: "text-delta", text: "hi" } });
    } catch {
      /* ignore */
    }
    return new FakeRun(this.agentId);
  }

  async close() {
    record({ event: "agent.close", agent_id: this.agentId });
  }
}

export const Agent = {
  async create(_options) {
    await sleep(CREATE_DELAY);
    return new FakeAgent();
  },
  async resume(_agentId, _options) {
    // Force the create path (no resume in these harnesses).
    throw new CursorAgentError("resume unsupported in fake");
  },
};
