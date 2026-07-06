"""AkanaPackHost — discovery + ``register_all`` + enable/disable lifecycle.

``register_all`` registers the content (skills/personas) of every discovered
*enabled* pack without a gate; disabled ones (packs_state.json) are loaded but not
registered. ``enable``/``disable`` hot-reload at runtime (without touching the
source folder; the disabled state is persisted to ``data_dir/packs_state.json``).
This module exercises discovery/validation, that a shipping pack's content lands
in a clean data_dir via ``register_all``, and the enable/disable/rescan lifecycle.
``pack-author-pack`` is the reference pack (several auto-discovered skills + a
persona, offline).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from akana_server.packs.host import AkanaPackHost
from akana_server.skills.registry import akana_skills_dir, scan_akana_skills
from packs.contract.host import PackState

REPO = Path(__file__).resolve().parent.parent
PACKS = REPO / "packs"
# pack-author-pack is the host-test reference pack. Its skill set is derived from
# the folder (no magic number) so adding a skill to the pack can't break the count.
REF_DIR = "pack-author-pack"
REF_ID = "user/pack-author-pack"
REF_PERSONA = "pack_architect"
REF_PACK = PACKS / REF_DIR
REF_SKILLS = {e.id for e in scan_akana_skills(REF_PACK / "skills")}
EXPECTED_SKILL_COUNT = len(REF_SKILLS)


def _host(tmp_path: Path) -> AkanaPackHost:
    """Host pointed at the repo packs/ dir but writing skills into tmp_path."""
    return AkanaPackHost(data_dir=tmp_path, discovery_roots=[PACKS])


# --------------------------------------------------------------------------- #
# discovery + validation                                                      #
# --------------------------------------------------------------------------- #


def test_discover_finds_ref_pack(tmp_path):
    host = _host(tmp_path)
    refs = host.discover()
    ids = {r.pack_id for r in refs}
    assert REF_ID in ids
    ref = next(r for r in refs if r.pack_id == REF_ID)
    assert ref.root == REF_PACK.resolve()


def test_discover_excludes_contract(tmp_path):
    host = _host(tmp_path)
    roots = {r.root.name for r in host.discover()}
    assert "contract" not in roots


def test_validate_ok(tmp_path):
    host = _host(tmp_path)
    ref = next(r for r in host.discover() if r.pack_id == REF_ID)
    res = host.validate(ref)
    assert res.ok, res.errors


def test_load_does_not_copy_skills(tmp_path):
    host = _host(tmp_path)
    ref = next(r for r in host.discover() if r.pack_id == REF_ID)
    pack = host.load(ref)
    # load only brings the manifest into memory; content is registered by register_all.
    assert pack.registered == {}
    assert scan_akana_skills(akana_skills_dir(tmp_path)) == []


# --------------------------------------------------------------------------- #
# register_all — every enabled pack is registered                             #
# --------------------------------------------------------------------------- #


def test_register_all_activates_every_discovered_pack(tmp_path):
    host = _host(tmp_path)
    discovered = {r.pack_id for r in host.discover()}
    activated = set(host.register_all())
    assert activated == discovered
    assert REF_ID in activated
    for pid in activated:
        assert host.state(pid) is PackState.ENABLED


def test_register_all_copies_ref_pack_skills(tmp_path):
    host = _host(tmp_path)
    host.register_all()
    found = {e.id for e in scan_akana_skills(akana_skills_dir(tmp_path))}
    manifest_ids = set(host.get(REF_ID).manifest.contains.skills)
    assert len(manifest_ids) == EXPECTED_SKILL_COUNT
    # register_all copies ALL packs; the reference pack's skills are a subset.
    assert manifest_ids <= found, manifest_ids - found


def test_register_all_registers_personas(tmp_path):
    # The standard's core: persona is auto-discovered + registered. memory_schema
    # / plugins are no longer consumed (removed from the pack standard).
    host = _host(tmp_path)
    host.register_all()

    personas = {p["id"] for p in host.personas_adapter.get_active_personas()}
    assert REF_PERSONA in personas


# --------------------------------------------------------------------------- #
# enable / disable lifecycle (hot-reload, persisted)                          #
# --------------------------------------------------------------------------- #


def _minimal_pack(packs_root: Path, pack_id: str = "user/mini-pack") -> Path:
    """A minimal standard pack: one AUTO-DISCOVERED skill, no ``contains`` block."""
    name = pack_id.split("/", 1)[1]
    d = packs_root / name
    (d / "skills" / "mini").mkdir(parents=True)
    (d / "pack.yaml").write_text(
        f"pack:\n  id: {pack_id}\n  version: 0.1.0\n  title: {name}\n",
        encoding="utf-8",
    )
    (d / "skills" / "mini" / "manifest.yaml").write_text(
        "id: mini\nversion: 1\ntitle: m\n", encoding="utf-8"
    )
    (d / "skills" / "mini" / "SKILL.md").write_text("# m\n", encoding="utf-8")
    return d


def test_disable_withdraws_skills_and_personas(tmp_path):
    host = _host(tmp_path)
    host.register_all()
    ref_skills = set(host.get(REF_ID).manifest.contains.skills)

    host.disable(REF_ID)

    assert host.state(REF_ID) is PackState.DISABLED
    found = {e.id for e in scan_akana_skills(akana_skills_dir(tmp_path))}
    assert not (ref_skills & found), "disabled pack skills must be removed"
    personas = {p["id"] for p in host.personas_adapter.get_active_personas()}
    assert REF_PERSONA not in personas


def test_enable_restores_content(tmp_path):
    host = _host(tmp_path)
    host.register_all()
    ref_skills = set(host.get(REF_ID).manifest.contains.skills)
    host.disable(REF_ID)

    host.enable(REF_ID)

    assert host.state(REF_ID) is PackState.ENABLED
    found = {e.id for e in scan_akana_skills(akana_skills_dir(tmp_path))}
    assert ref_skills <= found, "re-enable must restore the skills"
    personas = {p["id"] for p in host.personas_adapter.get_active_personas()}
    assert REF_PERSONA in personas


def _mcp_pack(packs_root: Path, pack_id: str = "user/mcp-pack") -> Path:
    """A pack that declares one consented-mountable mcp_server tool."""
    name = pack_id.split("/", 1)[1]
    d = packs_root / name
    (d / "skills" / "mini").mkdir(parents=True)
    (d / "skills" / "mini" / "manifest.yaml").write_text(
        "id: mini\nversion: 1\ntitle: m\n", encoding="utf-8"
    )
    (d / "skills" / "mini" / "SKILL.md").write_text("# m\n", encoding="utf-8")
    (d / "pack.yaml").write_text(
        f"pack:\n  id: {pack_id}\n  version: 0.1.0\n  title: MCP\n"
        "  dependencies:\n"
        "    external_tools:\n"
        '      - name: "srv"\n'
        '        kind: "mcp_server"\n'
        "        required: true\n"
        "        mcp:\n"
        "          type: stdio\n"
        '          command: "node srv.js"\n',
        encoding="utf-8",
    )
    return d


def test_disable_enable_keeps_consented_mcp_mount(tmp_path):
    """BP-4: a disable/enable toggle is lossless. Once the owner consents to a pack's
    MCP server, disabling parks the yaml entry (enabled:false) rather than deleting
    it, and re-enabling restores it — so the pack is never left "enabled" with its
    server permanently gone."""
    import yaml

    from akana_server.orchestrator.mcp_config import CONFIG_FILENAME, load_external_mcp_servers

    packs_root = tmp_path / "packs_root"
    packs_root.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    host = AkanaPackHost(data_dir=data, discovery_roots=[packs_root])
    _mcp_pack(packs_root)
    host.rescan()

    # Owner grants consent → the entry is mounted + live.
    host.grant_consent("user/mcp-pack")
    cfg = data / CONFIG_FILENAME
    assert "srv" in load_external_mcp_servers(data)

    # Disable: the entry survives, parked enabled:false (skipped at runtime).
    host.disable("user/mcp-pack")
    servers = yaml.safe_load(cfg.read_text(encoding="utf-8"))["servers"]
    assert "srv" in servers, "consent must not be destroyed by a plain disable"
    assert servers["srv"]["enabled"] is False
    assert "srv" not in load_external_mcp_servers(data), "disabled tools must not reach the LLM"

    # Enable: the entry comes back live WITHOUT a fresh consent.
    host.enable("user/mcp-pack")
    assert "srv" in load_external_mcp_servers(data)
    # And the view no longer reports it as pending consent.
    assert host.pack_view("user/mcp-pack")["mcp_pending"] == []


def test_hot_delete_truly_unmounts_consented_mcp(tmp_path):
    """BP-4 boundary: a real uninstall (folder deleted → rescan) still removes the
    entry entirely — only the reversible disable parks it."""
    import yaml

    from akana_server.orchestrator.mcp_config import CONFIG_FILENAME

    packs_root = tmp_path / "packs_root"
    packs_root.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    host = AkanaPackHost(data_dir=data, discovery_roots=[packs_root])
    pack_dir = _mcp_pack(packs_root)
    host.rescan()
    host.grant_consent("user/mcp-pack")

    shutil.rmtree(pack_dir)
    host.rescan()
    servers = (yaml.safe_load((data / CONFIG_FILENAME).read_text(encoding="utf-8")) or {}).get(
        "servers", {}
    )
    assert "srv" not in servers


def test_disabled_state_persists_across_instances(tmp_path):
    host1 = _host(tmp_path)
    host1.register_all()
    host1.disable(REF_ID)

    # A fresh host over the SAME data_dir reads packs_state.json.
    host2 = _host(tmp_path)
    activated = host2.register_all()
    assert REF_ID not in activated
    assert host2.state(REF_ID) is PackState.DISABLED
    found = {e.id for e in scan_akana_skills(akana_skills_dir(tmp_path))}
    ref_skills = set(host2.get(REF_ID).manifest.contains.skills)
    assert not (ref_skills & found)


def test_rescan_adds_new_pack(tmp_path):
    packs_root = tmp_path / "packs_root"
    packs_root.mkdir()
    data = tmp_path / "data"
    host = AkanaPackHost(data_dir=data, discovery_roots=[packs_root])
    assert host.register_all() == []

    _minimal_pack(packs_root)
    delta = host.rescan()

    assert delta["added"] == ["user/mini-pack"]
    assert delta["removed"] == []
    assert host.state("user/mini-pack") is PackState.ENABLED
    assert "mini" in {e.id for e in scan_akana_skills(akana_skills_dir(data))}
    assert host.rescan() == {
        "added": [],
        "removed": [],
        "updated": [],
    }, "second rescan finds nothing new"


def test_rescan_hot_deletes_vanished_pack(tmp_path):
    """Deleting a pack's folder → rescan withdraws its skills + persona at runtime
    (no restart): the persona leaves get_active_personas immediately."""
    packs_root = tmp_path / "packs_root"
    packs_root.mkdir()
    data = tmp_path / "data"
    host = AkanaPackHost(data_dir=data, discovery_roots=[packs_root])

    pack_dir = _minimal_pack(packs_root)
    # Give the pack a persona so we can prove persona hot-delete.
    (pack_dir / "personas").mkdir()
    (pack_dir / "personas" / "mini_voice.yaml").write_text(
        "persona:\n  id: mini_voice\n  title: Mini\n  system_prompt: hi\n",
        encoding="utf-8",
    )
    host.rescan()
    assert host.state("user/mini-pack") is PackState.ENABLED
    assert "mini" in {e.id for e in scan_akana_skills(akana_skills_dir(data))}
    assert "mini_voice" in {p["id"] for p in host.personas_adapter.get_active_personas()}

    # The folder vanishes (user removed the pack from packs/).
    shutil.rmtree(pack_dir)
    delta = host.rescan()

    assert delta["removed"] == ["user/mini-pack"]
    assert delta["added"] == []
    assert host.get("user/mini-pack") is None  # forgotten from _loaded
    assert host.state("user/mini-pack") is None
    # persona + skill withdrawn WITHOUT a restart
    assert "mini_voice" not in {p["id"] for p in host.personas_adapter.get_active_personas()}
    assert "mini" not in {e.id for e in scan_akana_skills(akana_skills_dir(data))}


def test_rescan_hot_delete_clears_persisted_disabled_state(tmp_path):
    """A disabled pack later deleted from packs/ must be forgotten — its id must
    not linger in the persisted disabled set (no phantom on a fresh host)."""
    packs_root = tmp_path / "packs_root"
    packs_root.mkdir()
    data = tmp_path / "data"
    host = AkanaPackHost(data_dir=data, discovery_roots=[packs_root])
    pack_dir = _minimal_pack(packs_root)
    host.rescan()
    host.disable("user/mini-pack")

    shutil.rmtree(pack_dir)
    assert host.rescan()["removed"] == ["user/mini-pack"]

    # A fresh host over the same data_dir must not carry a phantom disabled id.
    host2 = AkanaPackHost(data_dir=data, discovery_roots=[packs_root])
    host2.register_all()
    assert host2.state("user/mini-pack") is None


def test_reconcile_prunes_orphans_keeps_user_authored(tmp_path):
    host = _host(tmp_path)
    host.register_all()

    # A hand-authored skill (no provenance entry) must survive reconcile.
    ua = akana_skills_dir(tmp_path) / "handmade"
    ua.mkdir(parents=True, exist_ok=True)
    (ua / "manifest.yaml").write_text("id: handmade\nversion: 1\ntitle: h\n", encoding="utf-8")
    (ua / "SKILL.md").write_text("# h\n", encoding="utf-8")

    prov = host.skills_adapter.provenance()
    assert "handmade" not in prov  # user-authored is never recorded
    ref_skills = set(host.get(REF_ID).manifest.contains.skills)
    assert ref_skills <= set(prov)  # pack skills ARE recorded

    # Simulate the reference pack's folder deleted → it is no longer "present".
    # Everything ELSE the repo ships stays present (derive from discovery, don't
    # hardcode — adding a new pack must not break this test).
    present = {r.pack_id for r in host.discover()} - {REF_ID}
    removed = host.skills_adapter.reconcile(present_pack_ids=present)

    assert set(removed) == ref_skills
    found = {e.id for e in scan_akana_skills(akana_skills_dir(tmp_path))}
    assert "handmade" in found  # user-authored survived
    assert not (ref_skills & found)  # orphan pruned


def test_register_all_auto_prunes_orphan_from_deleted_pack(tmp_path):
    packs_root = tmp_path / "packs_root"
    pk = packs_root / "mini-pack"
    (pk / "skills" / "mini").mkdir(parents=True)
    (pk / "pack.yaml").write_text(
        "pack:\n  id: user/mini-pack\n  version: 0.1.0\n  contains:\n    skills: [mini]\n",
        encoding="utf-8",
    )
    (pk / "skills" / "mini" / "manifest.yaml").write_text(
        "id: mini\nversion: 1\ntitle: m\n", encoding="utf-8"
    )
    (pk / "skills" / "mini" / "SKILL.md").write_text("# m\n", encoding="utf-8")

    data = tmp_path / "data"
    host1 = AkanaPackHost(data_dir=data, discovery_roots=[packs_root])
    host1.register_all()
    assert "mini" in {e.id for e in scan_akana_skills(akana_skills_dir(data))}

    # Delete the pack folder and "restart" (fresh host over the same data_dir).
    import shutil as _sh

    _sh.rmtree(pk)
    host2 = AkanaPackHost(data_dir=data, discovery_roots=[packs_root])
    host2.register_all()  # auto-reconcile prunes the now-orphan 'mini'

    assert "mini" not in {e.id for e in scan_akana_skills(akana_skills_dir(data))}
    assert "mini" not in host2.skills_adapter.provenance()


def _add_skill_dir(pack_dir: Path, skill_id: str, title: str = "extra") -> None:
    """Create an AUTO-DISCOVERED skill dir inside an existing pack's skills/."""
    d = pack_dir / "skills" / skill_id
    d.mkdir(parents=True)
    (d / "manifest.yaml").write_text(
        f"id: {skill_id}\nversion: 1\ntitle: {title}\n", encoding="utf-8"
    )
    (d / "SKILL.md").write_text(f"# {title}\n", encoding="utf-8")


def test_rescan_picks_up_skill_added_to_existing_pack(tmp_path):
    """U1: a skill added to an ALREADY-mounted pack is registered on rescan (no restart).

    Fails on the old rescan() (existing packs were skipped, so the new skill was
    never copied into data_dir/skills); passes with the content-refresh branch.
    """
    packs_root = tmp_path / "packs_root"
    packs_root.mkdir()
    data = tmp_path / "data"
    host = AkanaPackHost(data_dir=data, discovery_roots=[packs_root])

    pack_dir = _minimal_pack(packs_root)
    host.rescan()
    found = {e.id for e in scan_akana_skills(akana_skills_dir(data))}
    assert "mini" in found and "extra" not in found

    # A new skill lands in the already-mounted pack's source dir.
    _add_skill_dir(pack_dir, "extra")
    delta = host.rescan()

    assert delta["updated"] == ["user/mini-pack"]
    assert delta["added"] == [] and delta["removed"] == []
    found = {e.id for e in scan_akana_skills(akana_skills_dir(data))}
    assert "extra" in found, "the newly added skill must be discovered after rescan"
    assert host.skills_adapter.provenance().get("extra") == "user/mini-pack"
    assert "extra" in host.get("user/mini-pack").manifest.contains.skills

    # Idempotent: a second rescan with no change reports nothing updated.
    assert host.rescan()["updated"] == []


def test_rescan_drops_skill_removed_from_existing_pack(tmp_path):
    """U1 inverse: a skill deleted from a still-present pack is pruned on rescan."""
    packs_root = tmp_path / "packs_root"
    packs_root.mkdir()
    data = tmp_path / "data"
    host = AkanaPackHost(data_dir=data, discovery_roots=[packs_root])

    pack_dir = _minimal_pack(packs_root)
    _add_skill_dir(pack_dir, "extra")
    host.rescan()
    assert {"mini", "extra"} <= {e.id for e in scan_akana_skills(akana_skills_dir(data))}

    shutil.rmtree(pack_dir / "skills" / "extra")
    delta = host.rescan()

    assert delta["updated"] == ["user/mini-pack"]
    found = {e.id for e in scan_akana_skills(akana_skills_dir(data))}
    assert "extra" not in found, "the removed skill copy must be pruned"
    assert "mini" in found, "the still-shipped skill stays"
    assert "extra" not in host.skills_adapter.provenance()


def test_rescan_drop_last_skill_refreshes_registry(tmp_path):
    """U1 follow-up (stale registry): dropping a pack's LAST skill must reload the
    cached registry, not just prune the disk copy.

    SkillsAdapter.register early-returns when the pack ships no skills, skipping
    its own registry reload; without the host forcing a refresh, get_registry()
    (feeding the catalog / turn-injection) still lists the dropped skill.
    """
    from akana_server.skills.registry import get_registry

    packs_root = tmp_path / "packs_root"
    packs_root.mkdir()
    data = tmp_path / "data"
    host = AkanaPackHost(data_dir=data, discovery_roots=[packs_root])

    # A pack whose ONLY skill is 'mini'.
    pack_dir = _minimal_pack(packs_root)
    host.rescan()
    assert "mini" in {e.id for e in get_registry(data).list()}

    # Remove the pack's only skill → the pack now ships zero skills.
    shutil.rmtree(pack_dir / "skills" / "mini")
    delta = host.rescan()

    assert delta["updated"] == ["user/mini-pack"]
    # The cached registry (not just a fresh scan) must no longer list 'mini'.
    assert "mini" not in {e.id for e in get_registry(data).list()}
    assert "mini" not in {e.id for e in scan_akana_skills(akana_skills_dir(data))}
    assert "mini" not in host.skills_adapter.provenance()


def test_rescan_drop_skill_spares_colliding_user_authored(tmp_path):
    """U1 follow-up (data loss): a pack skill id can collide with a user-authored
    (skill_teach) skill; register() refuses to install over it (no provenance), so
    when the pack later drops that id from its manifest, drop_skills must NOT
    rmtree the user's skill.
    """
    packs_root = tmp_path / "packs_root"
    packs_root.mkdir()
    data = tmp_path / "data"
    host = AkanaPackHost(data_dir=data, discovery_roots=[packs_root])

    # User authors a skill 'extra' by hand (no provenance).
    ua = akana_skills_dir(data) / "extra"
    ua.mkdir(parents=True)
    (ua / "manifest.yaml").write_text("id: extra\nversion: 1\ntitle: mine\n", encoding="utf-8")
    (ua / "SKILL.md").write_text("# mine\n", encoding="utf-8")
    sentinel = ua / "USER_FILE.txt"
    sentinel.write_text("hand-authored", encoding="utf-8")

    # A pack that ALSO ships a skill id 'extra' (in addition to 'mini').
    pack_dir = _minimal_pack(packs_root)
    _add_skill_dir(pack_dir, "extra", title="pack-extra")
    host.rescan()
    # register refused to clobber the user's 'extra' → no provenance recorded.
    assert "extra" not in host.skills_adapter.provenance()
    assert sentinel.is_file(), "register must not have overwritten the user's skill"

    # The pack drops 'extra' from its manifest.
    shutil.rmtree(pack_dir / "skills" / "extra")
    host.rescan()

    # The user's hand-authored 'extra' must survive the drop.
    assert ua.is_dir(), "user-authored skill must not be deleted by a pack drop"
    assert sentinel.is_file()
    assert sentinel.read_text(encoding="utf-8") == "hand-authored"
    assert "extra" in {e.id for e in scan_akana_skills(akana_skills_dir(data))}


def test_rescan_does_not_register_content_for_disabled_pack(tmp_path):
    """A disabled pack's added skill surfaces in listings but is NOT copied/registered."""
    packs_root = tmp_path / "packs_root"
    packs_root.mkdir()
    data = tmp_path / "data"
    host = AkanaPackHost(data_dir=data, discovery_roots=[packs_root])

    pack_dir = _minimal_pack(packs_root)
    host.rescan()
    host.disable("user/mini-pack")
    assert host.state("user/mini-pack") is PackState.DISABLED
    assert "mini" not in {e.id for e in scan_akana_skills(akana_skills_dir(data))}

    _add_skill_dir(pack_dir, "extra")
    delta = host.rescan()

    assert delta["updated"] == ["user/mini-pack"]
    # Listing reflects the fresh contents...
    assert "extra" in host.get("user/mini-pack").manifest.contains.skills
    # ...but nothing is registered while disabled.
    found = {e.id for e in scan_akana_skills(akana_skills_dir(data))}
    assert "extra" not in found and "mini" not in found


def test_rescan_content_refresh_preserves_runtime_state(tmp_path):
    """Re-registering on a content change must not wipe a skill's runtime state
    (contacts.json) — register() re-copies but preserves it; the dropped-only prune
    never touches a still-shipped skill."""
    packs_root = tmp_path / "packs_root"
    packs_root.mkdir()
    data = tmp_path / "data"
    host = AkanaPackHost(data_dir=data, discovery_roots=[packs_root])

    pack_dir = _minimal_pack(packs_root)
    host.rescan()
    # User builds up runtime state inside the copied skill.
    contacts = akana_skills_dir(data) / "mini" / "contacts.json"
    contacts.write_text('{"alice": "1"}', encoding="utf-8")

    _add_skill_dir(pack_dir, "extra")
    host.rescan()

    assert contacts.is_file(), "runtime contacts.json must survive a content refresh"
    assert contacts.read_text(encoding="utf-8") == '{"alice": "1"}'


def test_enable_picks_up_skill_added_while_disabled(tmp_path):
    """U1 secondary: a skill added while the pack was disabled is registered on enable."""
    packs_root = tmp_path / "packs_root"
    packs_root.mkdir()
    data = tmp_path / "data"
    host = AkanaPackHost(data_dir=data, discovery_roots=[packs_root])

    pack_dir = _minimal_pack(packs_root)
    host.rescan()
    host.disable("user/mini-pack")

    _add_skill_dir(pack_dir, "extra")
    host.enable("user/mini-pack")

    found = {e.id for e in scan_akana_skills(akana_skills_dir(data))}
    assert {"mini", "extra"} <= found
    assert "extra" in host.get("user/mini-pack").manifest.contains.skills


def test_broken_manifest_preserves_skills_and_mount(tmp_path):
    """A transient pack.yaml parse error must NOT hot-uninstall the pack.

    Regression: discover() skips a pack whose pack.yaml fails to parse; the old
    rescan removed-branch then read 'not present' as 'folder deleted' and ran a
    full _unregister_pack (true MCP unmount + skill rmtree), destroying
    user-populated runtime state (contacts.json) and the consented MCP entry.
    Because the DIRECTORY still exists, rescan must keep the derived state.
    """
    import yaml

    from akana_server.orchestrator.mcp_config import CONFIG_FILENAME

    packs_root = tmp_path / "packs_root"
    packs_root.mkdir()
    data = tmp_path / "data"
    data.mkdir()
    host = AkanaPackHost(data_dir=data, discovery_roots=[packs_root])
    pack_dir = _mcp_pack(packs_root)
    host.rescan()
    host.grant_consent("user/mcp-pack")

    # User builds runtime state inside the copied skill.
    contacts = akana_skills_dir(data) / "mini" / "contacts.json"
    contacts.write_text('{"alice": "1"}', encoding="utf-8")
    cfg = data / CONFIG_FILENAME
    assert "srv" in yaml.safe_load(cfg.read_text(encoding="utf-8"))["servers"]

    # Owner saves pack.yaml mid-edit with a YAML syntax error.
    (pack_dir / "pack.yaml").write_text(
        "pack:\n  id: user/mcp-pack\n  version: 0.1.0\n  title: [unclosed\n",
        encoding="utf-8",
    )
    delta = host.rescan()

    # The pack must NOT be reported removed, its skill copy + runtime state must
    # survive, and the consented MCP entry must stay mounted.
    assert "user/mcp-pack" not in delta["removed"]
    assert contacts.is_file(), "runtime contacts.json must survive a broken-manifest rescan"
    assert contacts.read_text(encoding="utf-8") == '{"alice": "1"}'
    assert "mini" in {e.id for e in scan_akana_skills(akana_skills_dir(data))}
    servers = yaml.safe_load(cfg.read_text(encoding="utf-8"))["servers"]
    assert "srv" in servers, "consent must not be destroyed by a broken manifest"
    assert servers["srv"].get("managed_by") == "pack:user/mcp-pack"


def test_pack_folder_rename_updates_root(tmp_path):
    """Renaming a pack folder (same id, same content ids) must update the LoadedPack
    root on rescan so a later disable+enable does not silently strip the pack.

    Regression: _refresh_pack_content only diffed skill/persona ID sets and never
    compared roots, so after a rename the LoadedPack kept a stale/nonexistent root
    and enable() found no source dirs, deleting all skills + dropping personas.
    """
    packs_root = tmp_path / "packs_root"
    packs_root.mkdir()
    data = tmp_path / "data"
    host = AkanaPackHost(data_dir=data, discovery_roots=[packs_root])

    pack_dir = _minimal_pack(packs_root)
    (pack_dir / "personas").mkdir()
    (pack_dir / "personas" / "mini_voice.yaml").write_text(
        "persona:\n  id: mini_voice\n  title: Mini\n  system_prompt: hi\n",
        encoding="utf-8",
    )
    host.rescan()
    assert "mini" in {e.id for e in scan_akana_skills(akana_skills_dir(data))}
    assert "mini_voice" in {p["id"] for p in host.personas_adapter.get_active_personas()}

    # Rename the pack folder (id unchanged, contents unchanged).
    new_dir = packs_root / "mini-pack-v2"
    pack_dir.rename(new_dir)
    host.rescan()

    got = host.get("user/mini-pack")
    assert got is not None
    assert got.root.resolve() == new_dir.resolve(), "rescan must adopt the new root"
    assert got.root.exists()

    # A disable+enable cycle must keep the skill on disk and the persona active.
    host.disable("user/mini-pack")
    host.enable("user/mini-pack")
    assert host.state("user/mini-pack") is PackState.ENABLED
    assert "mini" in {e.id for e in scan_akana_skills(akana_skills_dir(data))}
    assert "mini_voice" in {p["id"] for p in host.personas_adapter.get_active_personas()}


def test_pack_view_shape(tmp_path):
    host = _host(tmp_path)
    host.register_all()

    view = host.pack_view(REF_ID)
    assert view is not None
    assert view["id"] == REF_ID
    assert view["state"] == "enabled"
    assert view["enabled"] is True
    assert view["counts"]["skills"] == EXPECTED_SKILL_COUNT
    assert isinstance(view["contains"]["skills"], list)

    ids = {p["id"] for p in host.list_views()}
    assert REF_ID in ids

    assert host.pack_view("user/does-not-exist") is None
