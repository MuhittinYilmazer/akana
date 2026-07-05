"""pack-author-pack conformance to the pack-interface contract.

The meta-pack that AUTHORS new packs: offline, LLM-only, uses the repo's own
packs.contract.cli locally. It is the canonical reference architecture — a minimal
manifest (no permissions/isolation ceremony), auto-discovered skills, and full
autonomy (no approval gate).
"""

from __future__ import annotations

from pathlib import Path

from akana_server.skills.registry import scan_akana_skills
from packs.contract.manifest import load_manifest, validate_pack_dir

REPO = Path(__file__).resolve().parent.parent
PACK = REPO / "packs" / "pack-author-pack"


def test_pack_author_manifest_valid():
    res = validate_pack_dir(PACK)
    assert res.ok, res.errors


def test_pack_author_skills_resolve_in_akana_registry():
    # Skills auto-discovered from skills/ (no contains block in the new standard).
    ids = {e.id for e in scan_akana_skills(PACK / "skills")}
    assert {"pack_author", "pack_scaffold", "pack_skill_add", "pack_validate"} <= ids


def test_pack_author_offline_no_external_tools():
    m = load_manifest(PACK / "pack.yaml")
    assert m.permissions.network == [], "offline expected"
    assert m.dependencies.external_tools == [], "uses the local CLI, no external tool"


def test_pack_author_is_minimal_canonical():
    # Canonical reference: a MINIMAL manifest — the isolation/learning ceremony was
    # removed from the v0.1 schema entirely (a legacy pack declaring them still loads,
    # the fields are just ignored).
    m = load_manifest(PACK / "pack.yaml")
    assert not hasattr(m, "isolation"), "isolation was removed from the pack schema"
    assert not hasattr(m, "learning"), "learning was removed from the pack schema"


def test_pack_author_writing_skills_have_no_approval_gate():
    # Full autonomy: requires_approval is inert; the writing skills must not gate.
    entries = {e.id: e for e in scan_akana_skills(PACK / "skills")}
    for sid in ("pack_scaffold", "pack_skill_add"):
        assert entries[sid].requires_approval is False, f"{sid} must not require approval"
