"""WI-2 — installed capability catalog (system prompt inventory).

Contract:

* Empty (akana-source) registry → "" (system prompt unchanged, behavior-neutral).
* Only title + trigger are rendered — the SKILL.md body / description do NOT leak.
* Triggers are deduped + capped by ``_MAX_TRIGGERS``; overflow entries become "(+N more)".
* ``resolve_catalog`` combines the gate (AKANA_SKILL_CATALOG) + the registry;
  when off it returns "", when installed it returns the inventory. Cursor IDE skills are excluded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from akana_server.config import load_settings
from akana_server.skills import catalog as catalog_mod
from akana_server.skills.catalog import build_capability_catalog, resolve_catalog
from akana_server.skills.registry import SkillEntry, reload_skills


class _StubRegistry:
    """build_capability_catalog only calls ``.list(source_filter=...)``."""

    def __init__(self, entries: list[SkillEntry]) -> None:
        self._entries = entries

    def list(self, *, type_filter=None, source_filter=None):  # registry API signature
        out = self._entries
        if source_filter:
            out = [e for e in out if e.source == source_filter]
        return list(out)


def _entry(skill_id: str, title: str, triggers=(), *, source="akana", description=None) -> SkillEntry:
    return SkillEntry(
        id=skill_id,
        source=source,
        title=title,
        path=f"/tmp/{skill_id}",
        triggers=tuple(triggers),
        description=description,
    )


# -- build_capability_catalog (pure format) ------------------------------------- #


def test_empty_registry_yields_empty_string() -> None:
    assert build_capability_catalog(_StubRegistry([])) == ""


def test_title_and_triggers_rendered() -> None:
    reg = _StubRegistry([_entry("whatsapp", "WhatsApp Mesajlaşma", ["whatsapp", "mesaj gönder"])])
    out = build_capability_catalog(reg)
    assert out.startswith("[INSTALLED CAPABILITIES]")
    assert out.rstrip().endswith("[/INSTALLED CAPABILITIES]")
    assert "- WhatsApp Mesajlaşma — triggers: whatsapp, mesaj gönder" in out


def test_turkish_language_renders_turkish_header_and_labels() -> None:
    reg = _StubRegistry([_entry("whatsapp", "WhatsApp Mesajlaşma", ["whatsapp", "mesaj gönder"])])
    out = build_capability_catalog(reg, language="tr")
    assert out.startswith("[KURULU YETENEKLER]")
    assert out.rstrip().endswith("[/KURULU YETENEKLER]")
    assert "- WhatsApp Mesajlaşma — tetikleyiciler: whatsapp, mesaj gönder" in out


def test_entry_without_triggers_is_title_only() -> None:
    out = build_capability_catalog(_StubRegistry([_entry("notlar", "Notlar")]))
    assert "- Notlar" in out
    assert "triggers:" not in out  # the label "— triggers:" is absent (header prose may say "triggers")


def test_blank_title_falls_back_to_id() -> None:
    out = build_capability_catalog(_StubRegistry([_entry("ham_id", "")]))
    assert "- ham_id" in out


def test_description_and_body_never_leak() -> None:
    """User contract: only title + trigger — not the body/description."""
    reg = _StubRegistry([_entry("x", "X", ["trig"], description="GİZLİ AÇIKLAMA metni")])
    out = build_capability_catalog(reg)
    assert "GİZLİ AÇIKLAMA" not in out


def test_cursor_source_excluded() -> None:
    """Cursor IDE procedures are out of the catalog (only akana-source = installed packages)."""
    reg = _StubRegistry(
        [
            _entry("wa", "WhatsApp", ["whatsapp"], source="akana"),
            _entry("babysit", "Babysit PR", source="cursor"),
        ]
    )
    out = build_capability_catalog(reg)
    assert "WhatsApp" in out
    assert "Babysit PR" not in out


def test_triggers_deduped_and_capped() -> None:
    trigs = ["a-trig", "A-Trig", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k"]
    out = build_capability_catalog(_StubRegistry([_entry("many", "Many", trigs)]))
    line = next(ln for ln in out.splitlines() if ln.startswith("- Many"))
    shown = line.split("triggers:")[1]
    assert shown.lower().count("a-trig") == 1  # case-insensitive dedup
    assert shown.count(",") + 1 <= catalog_mod._MAX_TRIGGERS


def test_overflow_summarized_with_note_and_bounded() -> None:
    entries = [_entry(f"s{i}", f"Skill {i}", ["t"]) for i in range(catalog_mod._MAX_ENTRIES + 25)]
    out = build_capability_catalog(_StubRegistry(entries))
    assert "more capabilities)" in out  # "(+N more capabilities)" summary
    assert len(out) <= catalog_mod._MAX_CHARS + len(catalog_mod._HEADER_EN) + 256


def test_pack_grouping_groups_skills_under_pack_headers() -> None:
    entries = [
        _entry("browse", "browse", ["web"]),
        _entry("browser_setup", "browser_setup"),
        _entry("pack_author", "pack_author", ["pack"]),
        _entry("loose", "loose"),  # no owning pack → falls under "Other"
    ]
    pack_of = {
        "browse": "user/browser-pack",
        "browser_setup": "user/browser-pack",
        "pack_author": "user/pack-author-pack",
    }
    out = build_capability_catalog(_StubRegistry(entries), pack_of=pack_of)
    lines = out.splitlines()
    assert "Pack: browser-pack" in lines  # the pack id's name portion, not "user/..."
    assert "Pack: pack-author-pack" in lines
    assert "Other" in lines  # the loose skill's bucket
    bp, pa = lines.index("Pack: browser-pack"), lines.index("Pack: pack-author-pack")
    assert bp < pa  # first-seen pack order preserved
    assert any("browse" in ln for ln in lines[bp + 1 : pa])  # skills render under their pack
    assert any("- loose" in ln for ln in lines[lines.index("Other") + 1 :])


def test_pack_of_none_is_flat_and_backward_compatible() -> None:
    entries = [_entry("a", "A"), _entry("b", "B")]
    out = build_capability_catalog(_StubRegistry(entries))  # pack_of default None → flat
    assert "Pack:" not in out and "Other" not in out
    assert "- A" in out and "- B" in out


def test_pack_grouping_never_emits_dangling_header_at_char_budget() -> None:
    """A pack header must never appear with zero entries beneath it, even when the
    char budget is exhausted exactly between two pack sections (regression: a bare
    'Pack: X' header used to survive followed only by the '(+N more)' note)."""
    entries = [
        _entry("pack1.skill1", "A", ["x"]),
        _entry("pack2.skill1", "B" * 200, ["y" * 200]),  # long enough to never fit
    ]
    pack_of = {"pack1.skill1": "packs/pack1", "pack2.skill1": "packs/pack2"}
    # Budget large enough for the header + pack1's section, but not pack2's first entry.
    out = build_capability_catalog(_StubRegistry(entries), pack_of=pack_of, max_chars=330)
    lines = out.splitlines()
    assert "Pack: pack1" in lines
    # No line may be a bare "Pack: X" header with nothing but another header/footer/
    # note immediately after it.
    for i, ln in enumerate(lines):
        if ln.startswith("Pack:"):
            assert i + 1 < len(lines)
            nxt = lines[i + 1]
            assert not nxt.startswith("Pack:")
            assert not nxt.startswith("[/")
            assert "more capabilities" not in nxt


def test_pack_of_falls_back_to_directory_name_when_id_differs() -> None:
    """Provenance is keyed by skill DIRECTORY name; a skill whose manifest 'name'
    differs from its directory must still be grouped under its owning pack."""
    entries = [_entry("quick_notes", "Quick Notes", ["notes"])]
    entries[0] = SkillEntry(
        id="quick_notes",
        source="akana",
        title="Quick Notes",
        path="/data/skills/notes",  # directory name "notes" != manifest id "quick_notes"
        triggers=("notes",),
    )
    pack_of = {"notes": "user/x"}  # keyed by directory name, not manifest id
    out = build_capability_catalog(_StubRegistry(entries), pack_of=pack_of)
    lines = out.splitlines()
    assert "Pack: x" in lines
    assert "Other" not in lines


# -- resolve_catalog (gate + real registry) ------------------------------------- #


def _write_akana_skill(data_dir: Path, skill_id: str, title: str, triggers: list[str]) -> None:
    d = data_dir / "skills" / skill_id
    d.mkdir(parents=True)
    trig_yaml = "".join(f"  - {t}\n" for t in triggers)
    (d / "manifest.yaml").write_text(
        f"id: {skill_id}\nversion: 1\ntitle: {title}\nrisk: low\ntriggers:\n{trig_yaml}",
        encoding="utf-8",
    )
    (d / "SKILL.md").write_text(f"# {title}\n\nGÖVDE ADIMLARI.\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def _clear_skill_cache():
    reload_skills()
    yield
    reload_skills()


def test_resolve_catalog_reads_installed_akana_skill(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AKANA_SKILL_CATALOG", raising=False)
    _write_akana_skill(tmp_path, "whatsapp", "WhatsApp", ["whatsapp", "wa"])
    out = resolve_catalog(load_settings())
    assert "WhatsApp" in out and "whatsapp" in out
    assert "GÖVDE ADIMLARI" not in out  # body does not leak


def test_resolve_catalog_disabled_returns_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_SKILL_CATALOG", "0")
    _write_akana_skill(tmp_path, "whatsapp", "WhatsApp", ["whatsapp"])
    assert resolve_catalog(load_settings()) == ""


def test_resolve_catalog_empty_install_is_neutral(tmp_path, monkeypatch) -> None:
    """When no user package exists the catalog is "" — Cursor IDE skills do not count."""
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AKANA_SKILL_CATALOG", raising=False)
    assert resolve_catalog(load_settings()) == ""
