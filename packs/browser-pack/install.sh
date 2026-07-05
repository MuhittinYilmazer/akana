#!/usr/bin/env bash
# Browser pack — canonical MCP mount.
#
# Writes the 'browser' MCP entry into ~/.akana/mcp_servers.yaml the CANONICAL way:
# sets up AkanaPackHost, registers the packs (declare + probe only; NO mount), then
# calls ToolsAdapter.consent("user/browser-pack"). consent() writes managed_by-stamped,
# idempotent entries and NEVER overwrites entries the user placed by hand. This is the
# one correct place — avoid hand-writing yaml.
#
# This script does NOT install the browser/chromium — the browser_setup skill does that
# (npx warm-up + `npx playwright install chromium`). This is mount only.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"          # .../packs/browser-pack
REPO="$(cd "$ROOT/../.." && pwd)"              # repo root
DATA_DIR="${AKANA_DATA_DIR:-$HOME/.akana}"
PACK_ID="user/browser-pack"

# Prefer the repo venv; fall back to python3.
PY="$REPO/venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3 || true)"
if [[ -z "$PY" ]]; then
  echo "ERROR: python not found (no venv, no python3)." >&2
  exit 1
fi

echo "Browser pack mount: data_dir=$DATA_DIR  pack=$PACK_ID"

AKANA_DATA_DIR="$DATA_DIR" PYTHONPATH="$REPO" "$PY" - "$PACK_ID" <<'PYEOF'
import sys
from pathlib import Path
import os

pack_id = sys.argv[1]
data_dir = Path(os.environ["AKANA_DATA_DIR"]).expanduser()

from akana_server.packs.host import AkanaPackHost

host = AkanaPackHost(data_dir, persist_state=False)
host.register_all()                       # declare + probe (NO mount)
res = host.tools_adapter.consent(pack_id)  # canonical, idempotent mount

mounted = res.get("mounted") or []
needs = res.get("needs_config") or []
conflicts = res.get("conflicts") or []
invalid = res.get("invalid") or []

print(f"  mounted:      {mounted}")
print(f"  needs_config: {needs}")
print(f"  conflicts:    {conflicts}")
print(f"  invalid:      {invalid}")

if conflicts:
    print(
        "WARNING: a 'browser' entry that does NOT belong to this pack already exists — "
        "it was not overwritten. Resolve it by hand or remove that entry.",
        file=sys.stderr,
    )
    sys.exit(2)
if invalid or needs:
    print("WARNING: some entries could not be mounted (see above).", file=sys.stderr)
    sys.exit(3)
if not mounted:
    print(
        "Nothing was mounted. Was the pack found? Check packs/browser-pack/pack.yaml "
        "and the repo packs/ discovery.",
        file=sys.stderr,
    )
    sys.exit(4)
PYEOF

echo ""
echo "Browser pack mount complete."
echo ""
echo "Next steps:"
echo "  1) Warm up the toolchain (browser_setup skill or by hand):"
echo "       npx -y @playwright/mcp@latest --help     # download the package into the cache"
echo "       npx playwright install chromium          # ~150 MB"
echo "  2) Restart the server so providers see the new MCP server:"
echo "       python akana.py stop && python akana.py start"
