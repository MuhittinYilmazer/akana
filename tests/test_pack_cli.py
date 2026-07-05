"""packs.contract.cli — Studio-independent pack validator tests."""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

from packs.contract.cli import main
from packs.contract.manifest import PackManifest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCHEMA_FILE = _REPO_ROOT / "packs" / "contract" / "pack.schema.json"


def test_validate_ok_returns_zero():
    assert main(["validate", "packs/pack-author-pack"]) == 0


def test_validate_nonexistent_returns_one():
    assert main(["validate", "/nonexistent"]) == 1


def test_schema_prints_valid_json_with_expected_properties():
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["schema"])
    assert rc == 0
    obj = json.loads(buf.getvalue())
    assert "properties" in obj
    assert "id" in obj["properties"]
    assert "permissions" in obj["properties"]


def test_schema_file_on_disk_matches_canonical():
    on_disk = json.loads(_SCHEMA_FILE.read_text(encoding="utf-8"))
    assert on_disk == PackManifest.model_json_schema()


def test_info_returns_zero_for_valid_pack():
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["info", "packs/pack-author-pack"])
    assert rc == 0
    assert "id:" in buf.getvalue()
