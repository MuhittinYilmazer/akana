"""computer-control MCP mount points at the cwd-immune launcher FILE, not `-m`.

Regression: the pack's consented MCP block used ``args: ['-m',
'akana_server.computer_mcp']`` with no cwd. ``akana_server`` is NOT pip-installed,
so ``-m`` only resolves when the child inherits cwd=repo-root — but the claude CLI
spawns with cwd=settings.workspace and the in-process bridge inherits the server's
cwd, so the child died with ModuleNotFoundError off the repo root. The fix ships a
``<AKANA_REPO>/scripts/mcp_computer.py`` launcher marker that ToolsAdapter.consent
rewrites to the absolute launcher path at mount time.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

from akana_server.orchestrator.mcp_config import CONFIG_FILENAME
from akana_server.packs.adapters import ToolsAdapter
from packs.contract.host import LoadedPack
from packs.contract.manifest import load_manifest

REPO = Path(__file__).resolve().parents[2]
PACK_DIR = REPO / "packs" / "computer-control"


def _load_computer_pack() -> LoadedPack:
    manifest = load_manifest(PACK_DIR / "pack.yaml")
    return LoadedPack(manifest=manifest, root=PACK_DIR)


def test_mounted_entry_points_at_launcher_file_not_dash_m(tmp_path: Path) -> None:
    adapter = ToolsAdapter(data_dir=tmp_path)
    adapter.register(_load_computer_pack())

    res = adapter.consent("akana/computer-control", approved=True)
    assert res["mounted"] == ["computer"], res

    raw = yaml.safe_load((tmp_path / CONFIG_FILENAME).read_text(encoding="utf-8"))
    entry = raw["servers"]["computer"]
    args = entry["args"]

    # The consented spawn must NOT be the `-m akana_server...` form.
    assert "-m" not in args
    assert not any("akana_server.computer_mcp" == a for a in args)

    # It must point at an existing launcher FILE, absolute (marker resolved).
    assert len(args) == 1
    launcher = Path(args[0])
    assert "<AKANA_REPO>" not in args[0], "the repo marker must be resolved at mount time"
    assert launcher.is_absolute()
    assert launcher.is_file()
    assert launcher.name == "mcp_computer.py"


def test_launcher_imports_from_non_repo_cwd(tmp_path: Path) -> None:
    """The launcher FILE must import akana_server from a cwd that is NOT the repo root
    (the whole point of the fix: -m would fail there)."""
    adapter = ToolsAdapter(data_dir=tmp_path)
    adapter.register(_load_computer_pack())
    adapter.consent("akana/computer-control", approved=True)
    raw = yaml.safe_load((tmp_path / CONFIG_FILENAME).read_text(encoding="utf-8"))
    launcher = raw["servers"]["computer"]["args"][0]

    # Import the launcher's target package as the child would (sys.path bootstrap
    # from the launcher's __file__), running from a NON-repo cwd. We stop before
    # starting the stdio server so no live desktop/data is touched.
    # Execute the launcher FILE's sys.path bootstrap (the `import akana_server` at
    # module top). Running it as a file, from a non-repo cwd, is exactly the child's
    # import path; it must succeed without ModuleNotFoundError. main() only runs
    # under `if __name__ == '__main__'`, so exec_module never starts the server.
    code = (
        "import sys, importlib.util\n"
        f"spec = importlib.util.spec_from_file_location('_probe', {launcher!r})\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "assert 'akana_server' in sys.modules\n"
        "print('OK')\n"
    )
    env = dict(os.environ)
    env["AKANA_DATA_DIR"] = str(tmp_path)  # never touch ~/.akana
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(tmp_path),  # deliberately NOT the repo root
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
