# Browser: autonomous setup (Playwright MCP)

You set up everything the browser pack needs, END TO END, yourself. The user says
"set up browser pack" and waits. The `browse` skill may also call this as a PRELIMINARY
STEP before it runs. Backend reality: Microsoft Playwright MCP (`@playwright/mcp`) —
headful + persistent profile; runs via npx, needs chromium.

> PRINCIPLE: This pack is FULLY AUTONOMOUS (no approval gate, `requires_approval: false`).
> Still, be TRANSPARENT: briefly say what you're downloading (an npm package, ~150 MB
> of chromium). `sudo` is NOT required (chromium installs into the user directory). Only
> at a step that GENUINELY needs the user's hand (e.g. Node missing + the package
> manager wants sudo) should you stop and give the exact command.

## Prerequisites (probe)
- Node 18+ and `npx` on PATH.
- Network access (the `@playwright/mcp` package + chromium ~150 MB are downloaded).
- A desktop session for headful: `DISPLAY` (X11) or `WAYLAND_DISPLAY` set.
- The repo venv (`<repo>/venv/bin/python`) — install.sh uses it for the canonical mount.

## Step 0 — Detection (preflight probe)
Probe each in order, tracking ✓/✗. If all are ✓, say "Browser pack is already ready";
don't install, hand off to `browse`.
1. **Node/npx**: do `node -v` (>=18) and `npx -v` work?
2. **@playwright/mcp**: does `npx -y @playwright/mcp@latest --help` produce output?
   (The first run downloads the package = also the "warm-up" step.)
3. **chromium**: does `npx playwright install --dry-run chromium` say "already installed",
   or will it download?
4. **headful environment**: is `echo "$DISPLAY$WAYLAND_DISPLAY"` empty? If empty, no GUI →
   you need to fall back to headless (see below).
5. **MCP entry**: is `servers.browser` present in `~/.akana/mcp_servers.yaml`
   and stamped `managed_by: pack:user/browser-pack`?

## Step 1 — Autonomous setup
After each step, run the relevant probe AGAIN and verify. If a step can't be automated,
instead of stopping, give the user the exact command and explain why.

### 1a — Node/npx (if missing)
On an apt-based system:
```
sudo apt-get update && sudo apt-get install -y nodejs npm
```
If `sudo` needs a password, STOP and have the user run this command. nvm is also an
option. Verify: `node -v` >= 18.

### 1b — Warm up the @playwright/mcp package (timeout prevention)
```
npx -y @playwright/mcp@latest --help
```
This downloads the package into the npx cache. IMPORTANT: the in-process MCP bridge's
cold-start timeout is ~60 s; if the package isn't pre-downloaded, the first chat may
time out. This step prevents that.

### 1c — Download chromium
```
npx playwright install chromium
```
~150 MB. No sudo needed (installs into the user cache). Verify:
`npx playwright install --dry-run chromium` → "already installed".

### 1d — headful / headless decision
If `DISPLAY`/`WAYLAND_DISPLAY` is set, headful (the default) works; pack.yaml already
forwards these envs to the MCP subprocess. If there is NO GUI (server/SSH): tell the
user and say that `--headless` needs to be added to `args` in pack.yaml (or suggest a
headless profile). In that case screenshots work but there is no visible window.

### 1e — Mount the MCP entry (canonical consent)
The pack's `browser` MCP entry is written to `mcp_servers.yaml` the canonical way
(managed_by-stamped, idempotent, never overwriting user entries). install.sh does this:
```
bash <repo>/packs/browser-pack/install.sh
```
The output should contain `mounted: [browser]`. If already mounted it is idempotent
(won't rewrite). If `conflicts: [browser]` appears: the user has a hand-placed `browser`
entry — DON'T overwrite it, tell the user.

## Step 2 — Final report (✓/✗ table)
Re-run all probes and print the table:
| Component | Status |
|---|---|
| Node 18+ / npx | ✓/✗ |
| @playwright/mcp (warmed up) | ✓/✗ |
| chromium | ✓/✗ |
| headful environment (DISPLAY) | ✓/✗ (headless note if absent) |
| mcp_servers.yaml `browser` | ✓/✗ (managed_by-stamped) |

IMPORTANT final note: if the MCP entry was just mounted, the **server must be restarted**
for providers to see the new server (`python akana.py stop` then `start`). Tell the user.
If everything is ✓ and the restart is done, hand off to `browse`.

## Error cases
- npx/network error: relay the error, suggest a retry; behind a corporate proxy you may
  need access to the npm registry.
- chromium download error: retry `npx playwright install chromium` on its own; check
  disk/network.
- `mcp_servers.yaml` broken/parse error: install.sh aborts the mount (preserving the
  user's file) — fix the yaml, run again.
- No GUI but headful requested: the browser won't open; switch to the headless path in 1d.

## Notes
- This is the pack's only side-effecting (install) skill; but there is no approval gate
  (pack design).
- Versions/URLs aren't pinned: `@playwright/mcp@latest` always pulls the current release.
- install.sh calls the canonical `ToolsAdapter.consent()`; avoid hand-writing yaml
  (the managed_by/conflict/idempotent logic lives there).
