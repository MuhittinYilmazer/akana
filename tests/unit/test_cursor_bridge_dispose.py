"""Guard the Symbol.dispose polyfill that keeps the Cursor bridge working on Node 18.

@cursor/sdk's local runtime compiles `using` (explicit resource management) down to
tslib's __addDisposableResource, which throws "Symbol.dispose is not defined" on
Node <20.4 — the run then ends with EMPTY text and the UI shows "The model returned
an empty response (no text and no tool call)". ``cursor_bridge/dispose-polyfill.mjs``
backfills the symbols and MUST be imported before ``@cursor/sdk`` in every module
that loads the SDK. These tests fail loudly if that wiring regresses.

The static checks run everywhere (no Node needed). The functional check spawns the
real Node + lib.mjs and is skipped when Node / @cursor/sdk are absent.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_BRIDGE = _REPO_ROOT / "cursor_bridge"
_POLYFILL = _BRIDGE / "dispose-polyfill.mjs"

# Every bridge module that imports @cursor/sdk at runtime must load the polyfill first.
_SDK_IMPORTERS = ("lib.mjs", "list_models.mjs")


def test_polyfill_module_exists_and_defines_both_symbols() -> None:
    assert _POLYFILL.is_file(), f"missing {_POLYFILL}"
    text = _POLYFILL.read_text(encoding="utf-8")
    assert "Symbol.dispose" in text
    assert "Symbol.asyncDispose" in text


@pytest.mark.parametrize("name", _SDK_IMPORTERS)
def test_polyfill_imported_before_sdk(name: str) -> None:
    """The polyfill import must appear BEFORE the @cursor/sdk import.

    ESM evaluates imported modules depth-first in source order, so the symbols are
    only defined in time if the polyfill import precedes the SDK import.
    """
    src = (_BRIDGE / name).read_text(encoding="utf-8")
    lines = src.splitlines()
    poly_idx = next(
        (i for i, ln in enumerate(lines) if "dispose-polyfill.mjs" in ln and "import" in ln),
        None,
    )
    sdk_idx = next(
        (i for i, ln in enumerate(lines) if '"@cursor/sdk"' in ln and ln.lstrip().startswith("import")),
        None,
    )
    assert sdk_idx is not None, f"{name}: no @cursor/sdk import found"
    assert poly_idx is not None, f"{name}: dispose-polyfill.mjs is not imported"
    assert poly_idx < sdk_idx, (
        f"{name}: dispose-polyfill.mjs (line {poly_idx + 1}) must be imported BEFORE "
        f"@cursor/sdk (line {sdk_idx + 1})"
    )


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_symbol_dispose_defined_after_loading_lib() -> None:
    """Functional proof: after the real lib.mjs loads, Symbol.dispose is a symbol.

    On Node <20.4 this passes ONLY because the polyfill ran; if the wiring regresses
    the import throws and Symbol.dispose stays undefined. On Node 20.4+ it is a
    trivial pass (the symbols are native) — the Node-18 leg of CI is what gives this
    teeth.
    """
    if not (_BRIDGE / "node_modules" / "@cursor" / "sdk").is_dir():
        pytest.skip("@cursor/sdk not installed (run npm install in cursor_bridge)")
    lib_url = (_BRIDGE / "lib.mjs").resolve().as_uri()
    code = (
        f"import({json.dumps(lib_url)}).then(()=>{{"
        "process.stdout.write(JSON.stringify({"
        "dispose:typeof Symbol.dispose,asyncDispose:typeof Symbol.asyncDispose})+'\\n');"
        "process.exit(0);}).catch((e)=>{"
        "process.stderr.write(String(e&&e.stack||e)+'\\n');process.exit(2);})"
    )
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", code],
        cwd=str(_BRIDGE),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr.strip()}"
    payload = next(
        (json.loads(ln) for ln in proc.stdout.splitlines() if ln.strip().startswith("{")),
        None,
    )
    assert payload is not None, f"no JSON on stdout: {proc.stdout!r} / {proc.stderr!r}"
    assert payload["dispose"] == "symbol", (
        f"Symbol.dispose is {payload['dispose']!r} after loading lib.mjs on "
        f"{sys.platform} — the polyfill did not take effect"
    )
    assert payload["asyncDispose"] == "symbol"
