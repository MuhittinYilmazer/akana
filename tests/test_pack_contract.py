"""Conformance of the shipping packs to the pack-interface contract.

Makes the "these packs are ready" claim machine-checkable: schema validity +
resolution in the akana skill registry + the security expectations each pack
advertises. akana ships two packs — browser-pack and pack-author-pack — and each
anchors a different part of the contract (external-tool preflight; the canonical
minimal, offline, full-autonomy architecture).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from akana_server.skills.registry import scan_akana_skills
from packs.contract.host import PackState
from packs.contract.manifest import load_manifest, validate_pack_dir

REPO = Path(__file__).resolve().parent.parent
PACKS = REPO / "packs"
SHIPPING = ["browser-pack", "pack-author-pack"]
BROWSER = PACKS / "browser-pack"
PACK_AUTHOR = PACKS / "pack-author-pack"


@pytest.mark.parametrize("name", SHIPPING)
def test_shipping_pack_manifest_valid(name):
    res = validate_pack_dir(PACKS / name)
    assert res.ok, res.errors


@pytest.mark.parametrize("name", SHIPPING)
def test_shipping_pack_skills_resolve_in_akana_registry(name):
    # Skills are AUTO-DISCOVERED from skills/ (no contains block) — every skill in
    # the folder resolves in the akana registry.
    ids = {e.id for e in scan_akana_skills(PACKS / name / "skills")}
    assert ids, f"{name}: no skills resolved in the registry"


def test_pack_author_is_minimal_canonical():
    # pack-author-pack is the canonical reference: a MINIMAL manifest (no
    # isolation/learning ceremony — those blocks were removed from the v0.1 schema)
    # that stays offline and writes new packs locally.
    m = load_manifest(PACK_AUTHOR / "pack.yaml")
    assert not hasattr(m, "isolation"), "isolation was removed from the pack schema"
    assert not hasattr(m, "learning"), "learning was removed from the pack schema"
    assert m.dependencies.external_tools == [], "pack-author-pack stays offline"


def test_required_external_tools_have_setup_path():
    # browser-pack anchors the dependency-preflight contract (§5.1): every required
    # external tool carries a probe (absence detection) + a real setup_skill.
    m = load_manifest(BROWSER / "pack.yaml")
    required = [t for t in m.dependencies.external_tools if t.required]
    assert required, "browser-pack must declare at least one required external tool"
    skill_ids = {e.id for e in scan_akana_skills(BROWSER / "skills")}
    for t in required:
        assert t.probe, f"{t.name} has no probe (no absence detection)"
        assert t.setup_skill in skill_ids, f"{t.name} setup_skill is not a real skill: {t.setup_skill}"


def test_pack_author_skills_have_no_approval_gate():
    # Full autonomy: the (now inert) requires_approval flag is False — the
    # scaffolding skill writes files on disk with no gate.
    entries = {e.id: e for e in scan_akana_skills(PACK_AUTHOR / "skills")}
    assert entries["pack_scaffold"].requires_approval is False


def test_lifecycle_states_are_the_enforced_subset():
    # v0.1 implements exactly enabled ⇄ disabled — the aspirational
    # discovered/quarantined/approved/uninstalled states were removed.
    assert {s.value for s in PackState} == {"enabled", "disabled"}
