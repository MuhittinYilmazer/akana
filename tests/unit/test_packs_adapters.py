"""Adversarial / edge tests for the pack ContentAdapters (skills/tools/personas).

``akana_server/packs/adapters.py`` had ~55% line coverage and NO dedicated test
module, yet it carries security-relevant logic: untrusted pack content is used as
filesystem directory names (path-traversal surface) and is mounted into the user's
``mcp_servers.yaml`` (must never overwrite user entries). These tests probe the
consent/conflict/traversal/refcount paths where bugs hide.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from akana_server.orchestrator.mcp_config import CONFIG_FILENAME, RESERVED_SERVER_NAMES
from akana_server.packs.adapters import (
    PersonasAdapter,
    SkillsAdapter,
    ToolsAdapter,
    ToolsMountError,
)
from akana_server.skills.registry import akana_skills_dir
from packs.contract.host import LoadedPack
from packs.contract.manifest import PackManifest


def _pack(
    tmp_path: Path,
    *,
    pack_id: str = "test/pack",
    root: Path | None = None,
    external_tools: list[dict] | None = None,
    skills: list[str] | None = None,
    personas: list[str] | None = None,
) -> LoadedPack:
    contains: dict = {}
    if skills:
        contains["skills"] = skills
    if personas:
        contains["personas"] = personas
    deps: dict = {}
    if external_tools:
        deps["external_tools"] = external_tools
    manifest = PackManifest(
        id=pack_id, version="1.0.0", contains=contains, dependencies=deps
    )
    return LoadedPack(manifest=manifest, root=Path(root or tmp_path))


# --------------------------------------------------------------------------- #
# ToolsAdapter — consent-gated MCP mount (the security-critical surface).      #
# --------------------------------------------------------------------------- #


def _mcp_tool(name: str, *, command: str | None = "node srv.js", **extra) -> dict:
    t: dict = {"name": name, "kind": "mcp_server"}
    if command is not None:
        t["mcp"] = {"command": command}
    t.update(extra)
    return t


def test_consent_writes_managed_marker(tmp_path: Path) -> None:
    adapter = ToolsAdapter(data_dir=tmp_path)
    pack = _pack(tmp_path, external_tools=[_mcp_tool("srv")])
    adapter.register(pack)

    res = adapter.consent("test/pack", approved=True)
    assert res["mounted"] == ["srv"]

    raw = yaml.safe_load((tmp_path / CONFIG_FILENAME).read_text(encoding="utf-8"))
    assert raw["servers"]["srv"]["command"] == "node srv.js"
    assert raw["servers"]["srv"]["managed_by"] == "pack:test/pack"


def test_consent_never_overwrites_user_entry(tmp_path: Path) -> None:
    """Security: a pack server name clashing with a USER entry must NOT overwrite it."""
    cfg = tmp_path / CONFIG_FILENAME
    user_blob = {"servers": {"srv": {"command": "USER-OWNED", "url": "http://u"}}}
    cfg.write_text(yaml.safe_dump(user_blob), encoding="utf-8")

    adapter = ToolsAdapter(data_dir=tmp_path)
    adapter.register(_pack(tmp_path, external_tools=[_mcp_tool("srv")]))
    res = adapter.consent("test/pack", approved=True)

    assert res["conflicts"] == ["srv"]
    assert res["mounted"] == []
    after = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert after["servers"]["srv"] == {"command": "USER-OWNED", "url": "http://u"}


def test_consent_reserved_name_is_invalid(tmp_path: Path) -> None:
    reserved = sorted(RESERVED_SERVER_NAMES)[0]
    adapter = ToolsAdapter(data_dir=tmp_path)
    adapter.register(_pack(tmp_path, external_tools=[_mcp_tool(reserved)]))
    res = adapter.consent("test/pack", approved=True)
    assert res["invalid"] == [reserved]
    assert res["mounted"] == []
    # nothing written
    assert not (tmp_path / CONFIG_FILENAME).exists()


@pytest.mark.parametrize("bad", ["has space", "bad/slash", "dot.name", "a" * 65, ""])
def test_consent_rejects_malformed_server_names(tmp_path: Path, bad: str) -> None:
    adapter = ToolsAdapter(data_dir=tmp_path)
    adapter.register(_pack(tmp_path, external_tools=[_mcp_tool(bad)]))
    res = adapter.consent("test/pack", approved=True)
    assert res["mounted"] == []
    assert res["invalid"]  # rejected, not silently mounted


def test_consent_missing_command_and_url_is_invalid(tmp_path: Path) -> None:
    adapter = ToolsAdapter(data_dir=tmp_path)
    # mcp config present but has neither command nor url
    pack = _pack(tmp_path, external_tools=[_mcp_tool("srv", command=None, mcp={"args": []})])
    adapter.register(pack)
    res = adapter.consent("test/pack", approved=True)
    assert res["invalid"] == ["srv"]


def test_consent_no_config_goes_to_needs_config(tmp_path: Path) -> None:
    adapter = ToolsAdapter(data_dir=tmp_path)
    adapter.register(_pack(tmp_path, external_tools=[_mcp_tool("srv", command=None)]))
    res = adapter.consent("test/pack", approved=True)
    assert res["needs_config"] == ["srv"]
    assert res["mounted"] == []


def test_consent_server_configs_override_manifest(tmp_path: Path) -> None:
    adapter = ToolsAdapter(data_dir=tmp_path)
    adapter.register(_pack(tmp_path, external_tools=[_mcp_tool("srv", command="from-manifest")]))
    res = adapter.consent("test/pack", server_configs={"srv": {"url": "http://override"}}, approved=True)
    assert res["mounted"] == ["srv"]
    raw = yaml.safe_load((tmp_path / CONFIG_FILENAME).read_text(encoding="utf-8"))
    assert raw["servers"]["srv"]["url"] == "http://override"
    assert "command" not in raw["servers"]["srv"]


def test_consent_is_idempotent(tmp_path: Path) -> None:
    adapter = ToolsAdapter(data_dir=tmp_path)
    adapter.register(_pack(tmp_path, external_tools=[_mcp_tool("srv")]))
    adapter.consent("test/pack", approved=True)
    blob1 = (tmp_path / CONFIG_FILENAME).read_text(encoding="utf-8")
    res2 = adapter.consent("test/pack", approved=True)
    blob2 = (tmp_path / CONFIG_FILENAME).read_text(encoding="utf-8")
    assert res2["mounted"] == ["srv"]
    assert blob1 == blob2  # second consent rewrites nothing


def test_consent_non_mcp_tool_ignored(tmp_path: Path) -> None:
    adapter = ToolsAdapter(data_dir=tmp_path)
    adapter.register(_pack(tmp_path, external_tools=[_mcp_tool("cli", kind="cli")]))
    res = adapter.consent("test/pack", approved=True)
    assert res == {
        "mounted": [],
        "pending": [],
        "needs_config": [],
        "conflicts": [],
        "invalid": [],
    }


def test_consent_without_data_dir_raises(tmp_path: Path) -> None:
    adapter = ToolsAdapter(data_dir=None)
    adapter.register(_pack(tmp_path, external_tools=[_mcp_tool("srv")]))
    with pytest.raises(ToolsMountError):
        adapter.consent("test/pack", approved=True)


def test_consent_corrupt_yaml_aborts_without_writing(tmp_path: Path) -> None:
    """A corrupt mcp_servers.yaml must abort the mount (fail-closed), preserving the file."""
    cfg = tmp_path / CONFIG_FILENAME
    corrupt = "servers: {oops: [unclosed"
    cfg.write_text(corrupt, encoding="utf-8")
    adapter = ToolsAdapter(data_dir=tmp_path)
    adapter.register(_pack(tmp_path, external_tools=[_mcp_tool("srv")]))
    with pytest.raises(ToolsMountError):
        adapter.consent("test/pack", approved=True)
    assert cfg.read_text(encoding="utf-8") == corrupt


def test_read_config_rejects_non_mapping_root(tmp_path: Path) -> None:
    cfg = tmp_path / CONFIG_FILENAME
    cfg.write_text(yaml.safe_dump(["a", "b"]), encoding="utf-8")
    adapter = ToolsAdapter(data_dir=tmp_path)
    adapter.register(_pack(tmp_path, external_tools=[_mcp_tool("srv")]))
    with pytest.raises(ToolsMountError):
        adapter.consent("test/pack", approved=True)


def test_read_config_rejects_non_mapping_servers(tmp_path: Path) -> None:
    cfg = tmp_path / CONFIG_FILENAME
    cfg.write_text(yaml.safe_dump({"servers": ["not", "a", "map"]}), encoding="utf-8")
    adapter = ToolsAdapter(data_dir=tmp_path)
    adapter.register(_pack(tmp_path, external_tools=[_mcp_tool("srv")]))
    with pytest.raises(ToolsMountError):
        adapter.consent("test/pack", approved=True)


def test_unmount_only_removes_own_entries(tmp_path: Path) -> None:
    cfg = tmp_path / CONFIG_FILENAME
    seed = {
        "servers": {
            "user_srv": {"command": "u"},
            "other_pack": {"command": "o", "managed_by": "pack:other/x"},
        }
    }
    cfg.write_text(yaml.safe_dump(seed), encoding="utf-8")
    adapter = ToolsAdapter(data_dir=tmp_path)
    adapter.register(_pack(tmp_path, external_tools=[_mcp_tool("mine")]))
    adapter.consent("test/pack", approved=True)

    removed = adapter.unmount("test/pack")
    assert removed == ["mine"]
    after = yaml.safe_load(cfg.read_text(encoding="utf-8"))["servers"]
    assert set(after) == {"user_srv", "other_pack"}  # foreign entries survive


def test_pending_consent_tracks_unmounted(tmp_path: Path) -> None:
    adapter = ToolsAdapter(data_dir=tmp_path)
    adapter.register(_pack(tmp_path, external_tools=[_mcp_tool("srv")]))
    assert adapter.pending_consent("test/pack") == ["srv"]
    adapter.consent("test/pack", approved=True)
    assert adapter.pending_consent("test/pack") == []
    assert adapter.mounted_server_names("test/pack") == ["srv"]


def test_consent_without_approval_does_not_mount(tmp_path: Path) -> None:
    """Security gate: consent() without ``approved=True`` writes NOTHING and reports
    the mountable servers as ``pending`` — an agent cannot self-grant an MCP mount."""
    adapter = ToolsAdapter(data_dir=tmp_path)
    adapter.register(_pack(tmp_path, external_tools=[_mcp_tool("srv")]))

    res = adapter.consent("test/pack")  # no approval

    assert res["pending"] == ["srv"]
    assert res["mounted"] == []
    assert not (tmp_path / CONFIG_FILENAME).exists()  # nothing written
    assert adapter.pending_consent("test/pack") == ["srv"]  # still pending

    # Explicit approval mounts it.
    res2 = adapter.consent("test/pack", approved=True)
    assert res2["mounted"] == ["srv"]
    assert res2["pending"] == []
    assert (tmp_path / CONFIG_FILENAME).exists()


def test_unregister_swallows_unmount_error(tmp_path: Path) -> None:
    """disable must not blow up if mcp_servers.yaml is unreadable at unmount time."""
    adapter = ToolsAdapter(data_dir=tmp_path)
    adapter.register(_pack(tmp_path, external_tools=[_mcp_tool("srv")]))
    adapter.consent("test/pack", approved=True)
    (tmp_path / CONFIG_FILENAME).write_text("servers: [broken", encoding="utf-8")
    # must not raise
    adapter.unregister("test/pack")


def test_set_enabled_toggles_managed_entries_only(tmp_path: Path) -> None:
    """BP-4: set_enabled flips the ``enabled`` flag on the pack's own entries and
    never touches a user/foreign entry."""
    cfg = tmp_path / CONFIG_FILENAME
    seed = {
        "servers": {
            "user_srv": {"command": "u"},
            "other_pack": {"command": "o", "managed_by": "pack:other/x"},
        }
    }
    cfg.write_text(yaml.safe_dump(seed), encoding="utf-8")
    adapter = ToolsAdapter(data_dir=tmp_path)
    adapter.register(_pack(tmp_path, external_tools=[_mcp_tool("mine")]))
    adapter.consent("test/pack", approved=True)

    # Disable → only "mine" is parked; foreign/user entries are untouched.
    assert adapter.set_enabled("test/pack", False) == ["mine"]
    servers = yaml.safe_load(cfg.read_text(encoding="utf-8"))["servers"]
    assert servers["mine"]["enabled"] is False
    assert "enabled" not in servers["user_srv"]
    assert "enabled" not in servers["other_pack"]
    # Consent is NOT lost — the entry is still owned by the pack.
    assert adapter.mounted_server_names("test/pack") == ["mine"]

    # Idempotent: a second disable rewrites nothing.
    assert adapter.set_enabled("test/pack", False) == []

    # Enable → flips back to True.
    assert adapter.set_enabled("test/pack", True) == ["mine"]
    servers = yaml.safe_load(cfg.read_text(encoding="utf-8"))["servers"]
    assert servers["mine"]["enabled"] is True


def test_disable_is_lossless_reversible(tmp_path: Path) -> None:
    """BP-4: a disable/enable cycle via unregister(preserve_mount=True) keeps the
    consented entry (parked enabled:false), and a re-register + set_enabled restores
    it — the entry is never dropped from mcp_servers.yaml."""
    cfg = tmp_path / CONFIG_FILENAME
    adapter = ToolsAdapter(data_dir=tmp_path)
    pack = _pack(tmp_path, external_tools=[_mcp_tool("mine")])
    adapter.register(pack)
    adapter.consent("test/pack", approved=True)

    # Disable (reversible) — entry stays, parked off.
    adapter.unregister("test/pack", preserve_mount=True)
    servers = yaml.safe_load(cfg.read_text(encoding="utf-8"))["servers"]
    assert "mine" in servers
    assert servers["mine"]["enabled"] is False

    # Enable — re-declare, then flip back on.
    adapter.register(pack)
    assert adapter.set_enabled("test/pack", True) == ["mine"]
    servers = yaml.safe_load(cfg.read_text(encoding="utf-8"))["servers"]
    assert servers["mine"]["enabled"] is True


def test_unregister_uninstall_truly_unmounts(tmp_path: Path) -> None:
    """BP-4: the default (preserve_mount=False) path — uninstall/hot-delete —
    still removes the entry entirely."""
    cfg = tmp_path / CONFIG_FILENAME
    adapter = ToolsAdapter(data_dir=tmp_path)
    adapter.register(_pack(tmp_path, external_tools=[_mcp_tool("mine")]))
    adapter.consent("test/pack", approved=True)

    adapter.unregister("test/pack")  # default: true unmount
    servers = yaml.safe_load(cfg.read_text(encoding="utf-8"))["servers"]
    assert "mine" not in servers


def test_probe_required_absent_surfaces_in_missing(tmp_path: Path) -> None:
    adapter = ToolsAdapter(data_dir=tmp_path)
    pack = _pack(
        tmp_path,
        external_tools=[{"name": "no-such-binary", "kind": "cli", "required": True}],
    )
    adapter.register(pack)
    missing = adapter.missing_required("test/pack")
    assert [r.name for r in missing] == ["no-such-binary"]
    assert missing[0].present is False


# --------------------------------------------------------------------------- #
# SkillsAdapter — path-traversal guard + copy + teardown.                      #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def _no_skill_refresh(monkeypatch: pytest.MonkeyPatch):
    """Isolate the adapter's copy logic from the global skills registry reload."""
    import akana_server.packs.adapters as mod

    monkeypatch.setattr(mod, "reload_skills", lambda: None)

    class _Reg:
        def reload(self) -> None:
            return None

    monkeypatch.setattr(mod, "get_registry", lambda _d: _Reg())


@pytest.mark.parametrize("evil", ["../escape", "a/b", "..", "with space"])
def test_skills_rejects_traversal_ids(tmp_path: Path, _no_skill_refresh, evil: str) -> None:
    data_dir = tmp_path / "data"
    pack_root = tmp_path / "pack"
    pack_root.mkdir()
    adapter = SkillsAdapter(data_dir)
    pack = _pack(tmp_path, root=pack_root, skills=[evil])
    adapter.register(pack)
    # nothing installed, and no directory escaped the skills root
    assert pack.registered.get("skills", []) == []
    assert not (tmp_path / "escape").exists()


def test_skills_copy_and_unregister(tmp_path: Path, _no_skill_refresh) -> None:
    data_dir = tmp_path / "data"
    pack_root = tmp_path / "pack"
    src = pack_root / "skills" / "demo"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_text("# demo", encoding="utf-8")

    adapter = SkillsAdapter(data_dir)
    pack = _pack(tmp_path, root=pack_root, skills=["demo"])
    adapter.register(pack)

    dest = akana_skills_dir(data_dir) / "demo"
    assert (dest / "SKILL.md").is_file()
    assert pack.registered["skills"] == ["demo"]

    adapter.unregister("test/pack")
    assert not dest.exists()


def test_skills_missing_src_dir_skipped(tmp_path: Path, _no_skill_refresh) -> None:
    data_dir = tmp_path / "data"
    pack_root = tmp_path / "pack"
    pack_root.mkdir()
    adapter = SkillsAdapter(data_dir)
    pack = _pack(tmp_path, root=pack_root, skills=["ghost"])
    adapter.register(pack)
    assert pack.registered.get("skills", []) == []


def test_skills_copy_excludes_build_artifacts(tmp_path: Path, _no_skill_refresh) -> None:
    data_dir = tmp_path / "data"
    pack_root = tmp_path / "pack"
    src = pack_root / "skills" / "demo"
    (src / "node_modules" / "junk").mkdir(parents=True)
    (src / "node_modules" / "junk" / "x.js").write_text("//", encoding="utf-8")
    (src / "SKILL.md").write_text("# demo", encoding="utf-8")

    SkillsAdapter(data_dir).register(_pack(tmp_path, root=pack_root, skills=["demo"]))
    dest = akana_skills_dir(data_dir) / "demo"
    assert (dest / "SKILL.md").is_file()
    assert not (dest / "node_modules").exists()


# --------------------------------------------------------------------------- #
# PersonasAdapter — yaml load (.yaml/.yml), persona-key unwrap, teardown.      #
# --------------------------------------------------------------------------- #


def test_personas_load_and_unwrap(tmp_path: Path) -> None:
    pack_root = tmp_path / "pack"
    base = pack_root / "plugins" / "personas"
    base.mkdir(parents=True)
    (base / "luna.yaml").write_text(
        yaml.safe_dump({"persona": {"id": "luna", "system_prompt": "hi"}}),
        encoding="utf-8",
    )
    adapter = PersonasAdapter()
    pack = _pack(tmp_path, root=pack_root, personas=["luna"])
    adapter.register(pack)

    actives = adapter.get_active_personas()
    assert len(actives) == 1
    assert actives[0]["id"] == "luna"
    assert actives[0]["_pack_id"] == "test/pack"

    adapter.unregister("test/pack")
    assert adapter.get_active_personas() == []


def test_personas_yml_extension_fallback(tmp_path: Path) -> None:
    pack_root = tmp_path / "pack"
    base = pack_root / "plugins" / "personas"
    base.mkdir(parents=True)
    (base / "luna.yml").write_text(
        yaml.safe_dump({"id": "luna", "system_prompt": "hi"}), encoding="utf-8"
    )
    adapter = PersonasAdapter()
    adapter.register(_pack(tmp_path, root=pack_root, personas=["luna"]))
    assert [p["id"] for p in adapter.get_active_personas()] == ["luna"]


def test_personas_missing_file_skipped(tmp_path: Path) -> None:
    pack_root = tmp_path / "pack"
    (pack_root / "plugins" / "personas").mkdir(parents=True)
    adapter = PersonasAdapter()
    pack = _pack(tmp_path, root=pack_root, personas=["ghost"])
    adapter.register(pack)
    assert adapter.get_active_personas() == []
    assert pack.registered.get("personas", []) == []


def _persona_pack(tmp_path: Path, pack_id: str, dir_name: str, prompt: str) -> LoadedPack:
    """A pack shipping a single persona id 'web_op' from its own root."""
    root = tmp_path / dir_name
    base = root / "personas"
    base.mkdir(parents=True)
    (base / "web_op.yaml").write_text(
        yaml.safe_dump({"persona": {"id": "web_op", "system_prompt": prompt}}),
        encoding="utf-8",
    )
    return _pack(tmp_path, pack_id=pack_id, root=root, personas=["web_op"])


def test_same_id_persona_across_packs_no_shadow_and_disable_keeps_other(tmp_path: Path) -> None:
    """Two enabled packs shipping the same persona id must not shadow each other,
    and disabling one must not remove the persona owned by the still-enabled pack.

    Regression: PersonasAdapter keyed _active by persona id only (no ownership
    guard), so a later pack overwrote an enabled pack's persona and unregister
    unconditionally popped it — the persona vanished from prompt injection while
    the owning pack was still enabled.
    """
    adapter = PersonasAdapter()
    pack_a = _persona_pack(tmp_path, "user/pack-a", "pack-a", "A")
    pack_b = _persona_pack(tmp_path, "user/pack-b", "pack-b", "B")

    adapter.register(pack_a)
    adapter.register(pack_b)  # same id — must NOT shadow pack-a's active persona

    actives = adapter.get_active_personas()
    assert [p["id"] for p in actives] == ["web_op"]
    assert actives[0]["_pack_id"] == "user/pack-a", "incumbent must not be shadowed"
    # pack-b's copy was skipped, so it is not recorded as registered by pack-b.
    assert pack_b.registered.get("personas", []) == []

    # Disabling the later pack must leave pack-a's persona active.
    adapter.unregister("user/pack-b")
    assert [p["id"] for p in adapter.get_active_personas()] == ["web_op"]
    assert adapter.get_active_personas()[0]["_pack_id"] == "user/pack-a"

    # Disabling the owner removes it cleanly.
    adapter.unregister("user/pack-a")
    assert adapter.get_active_personas() == []


def test_mid_loop_copytree_failure_then_disable_enable(
    tmp_path: Path, _no_skill_refresh, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copytree that raises on the 2nd skill must not permanently wedge the 1st.

    Regression: register recorded per-skill provenance in-loop but only assigned
    _installed after the loop, so a mid-loop failure escaped register() with no
    _installed entry. A later disable then removed 0 dirs yet _forget_provenance
    stripped the copied skill's provenance, orphaning it (register's user-authored
    guard refused to ever reinstall it).
    """
    import akana_server.packs.adapters as mod

    data_dir = tmp_path / "data"
    pack_root = tmp_path / "pack"
    for sid in ("aaa", "bbb"):
        src = pack_root / "skills" / sid
        src.mkdir(parents=True)
        (src / "SKILL.md").write_text(f"# {sid}", encoding="utf-8")

    adapter = SkillsAdapter(data_dir)
    pack = _pack(tmp_path, pack_id="user/twopack", root=pack_root, skills=["aaa", "bbb"])

    real_copytree = mod.shutil.copytree
    calls = {"n": 0}

    def _boom(src, dst, *a, **kw):
        calls["n"] += 1
        if calls["n"] == 2:  # fail on the SECOND skill
            raise OSError("simulated Windows file lock")
        return real_copytree(src, dst, *a, **kw)

    monkeypatch.setattr(mod.shutil, "copytree", _boom)
    with pytest.raises(OSError):
        adapter.register(pack)
    monkeypatch.setattr(mod.shutil, "copytree", real_copytree)

    # The first skill was copied and MUST keep its provenance (owned by the pack).
    dest_aaa = akana_skills_dir(data_dir) / "aaa"
    assert dest_aaa.is_dir()
    assert adapter.provenance().get("aaa") == "user/twopack"

    # Disable removes exactly the copied dir; provenance is forgotten only for it.
    adapter.unregister("user/twopack")
    assert not dest_aaa.exists(), "disable must remove the skill that WAS copied"
    assert "aaa" not in adapter.provenance()

    # Re-enable must reinstall 'aaa' (NOT treat it as a user-authored collision).
    src_aaa = pack_root / "skills" / "aaa"  # still present in the pack source
    assert src_aaa.is_dir()
    adapter.register(pack)
    assert (akana_skills_dir(data_dir) / "aaa").is_dir()
    assert adapter.provenance().get("aaa") == "user/twopack"
