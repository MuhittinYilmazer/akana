"""SkillEngine F0-F1 — SkillRegistry: L1/L2/L3 progressive disclosure + search + API."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app
from akana_server.skills.registry import (
    SkillRegistry,
    get_registry,
    akana_skills_dir,
    reload_skills,
    scan_akana_skills,
)

BODY = "# Demo skill\n\n1. Adım bir\n2. Adım iki\n"


def _write_skill(
    root: Path,
    skill_id: str,
    *,
    description: str = "Demo açıklama",
    skill_type: str = "skill",
    triggers: list[str] | None = None,
    tags: list[str] | None = None,
    body: str = BODY,
) -> Path:
    d = root / skill_id
    d.mkdir(parents=True)
    trig = "".join(f"  - \"{t}\"\n" for t in (triggers or []))
    tg = "".join(f"  - {t}\n" for t in (tags or []))
    fm = f"""---
name: {skill_id}
type: {skill_type}
description: {description}
"""
    if trig:
        fm += f"triggers:\n{trig}"
    if tg:
        fm += f"tags:\n{tg}"
    fm += "---\n\n"
    (d / "SKILL.md").write_text(fm + body, encoding="utf-8")
    return d


@pytest.fixture(autouse=True)
def _clear_registry_cache() -> None:
    reload_skills()
    yield
    reload_skills()


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SkillRegistry:
    monkeypatch.setattr(
        "akana_server.skills.registry.cursor_skill_roots", lambda: []
    )
    root = akana_skills_dir(tmp_path)
    _write_skill(
        root,
        "project_checks",
        description="Test ve lint çalıştırır",
        triggers=["testleri çalıştır"],
        tags=["test", "lint"],
    )
    _write_skill(root, "deploy_playbook", skill_type="playbook", description="Deploy senaryosu")
    return get_registry(tmp_path)


# -- scanning + error isolation --------------------------------------------------


def test_frontmatter_only_skill_scanned(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "fm_only", description="Sadece frontmatter")
    entries = scan_akana_skills(root)
    assert len(entries) == 1
    e = entries[0]
    assert e.id == "fm_only"
    assert e.description == "Sadece frontmatter"
    assert e.type == "skill"
    assert e.trust_tier == "user"


def test_broken_skill_does_not_block_others(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_skill(root, "good_skill")
    bad = root / "bad_skill"
    bad.mkdir()
    (bad / "SKILL.md").write_text("---\nname: [unclosed\n---\nbody\n", encoding="utf-8")
    errors: list[dict[str, str]] = []
    entries = scan_akana_skills(root, errors=errors)
    assert [e.id for e in entries] == ["good_skill"]
    assert len(errors) == 1
    assert "frontmatter YAML error" in errors[0]["error"]
    assert "bad_skill" in errors[0]["path"]


def test_registry_errors_property(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "akana_server.skills.registry.cursor_skill_roots", lambda: []
    )
    root = akana_skills_dir(tmp_path)
    _write_skill(root, "ok_skill")
    bad = root / "broken"
    bad.mkdir()
    (bad / "SKILL.md").write_text("---\ntype: skill\n---\nbody\n", encoding="utf-8")
    reg = get_registry(tmp_path)
    assert [e.id for e in reg.list()] == ["ok_skill"]
    assert len(reg.errors) == 1
    assert "missing required field" in reg.errors[0]["error"]


def test_frontmatter_overrides_manifest(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    d = root / "mixed"
    d.mkdir(parents=True)
    (d / "manifest.yaml").write_text(
        "id: mixed\ntitle: Manifest başlık\ndescription: manifest desc\nrisk: low\n",
        encoding="utf-8",
    )
    (d / "SKILL.md").write_text(
        "---\ntitle: Frontmatter başlık\nrisk: high\n---\n# Gövde\n", encoding="utf-8"
    )
    entries = scan_akana_skills(root)
    assert len(entries) == 1
    assert entries[0].title == "Frontmatter başlık"
    assert entries[0].risk == "high"
    assert entries[0].description == "manifest desc"


# -- L1/L2/L3 progressive disclosure -------------------------------------------


def test_l1_list_does_not_load_bodies(registry: SkillRegistry) -> None:
    entries = registry.list()
    assert {e.id for e in entries} == {"project_checks", "deploy_playbook"}
    for e in entries:
        assert not registry.body_loaded(e.id)


def test_l1_filters(registry: SkillRegistry) -> None:
    assert [e.id for e in registry.list(type_filter="playbook")] == ["deploy_playbook"]
    assert len(registry.list(source_filter="akana")) == 2
    assert registry.list(source_filter="cursor") == []


def test_l2_body_on_demand(registry: SkillRegistry) -> None:
    body = registry.load_body("project_checks")
    assert "Adım bir" in body
    assert "---" not in body  # frontmatter does not leak into the body
    assert registry.body_loaded("project_checks")
    assert not registry.body_loaded("deploy_playbook")
    registry.reload()
    assert not registry.body_loaded("project_checks")  # reload clears the L2 cache


def test_l2_unknown_skill(registry: SkillRegistry) -> None:
    with pytest.raises(KeyError):
        registry.load_body("yok_boyle_skill")


def test_l3_resources_listed_not_loaded(registry: SkillRegistry, tmp_path: Path) -> None:
    skill_dir = Path(registry.get("project_checks").path)
    scripts = skill_dir / "scripts"
    scripts.mkdir()
    (scripts / "run_checks.py").write_text("print('ok')\n", encoding="utf-8")
    (skill_dir / ".gizli").write_text("x", encoding="utf-8")
    res = registry.list_resources("project_checks")
    assert res == ["scripts/run_checks.py"]  # excluding SKILL.md and hidden files


# -- simple search ----------------------------------------------------------------


def test_search_trigger_exact_wins(registry: SkillRegistry) -> None:
    results = registry.search("testleri çalıştır")
    assert results[0].entry.id == "project_checks"
    assert results[0].score == 1.0
    assert results[0].match_reason == "trigger_exact"


def test_search_title_and_description(registry: SkillRegistry) -> None:
    # F2: match_reason carries the layer info — the substring reason stays first,
    # and if FTS also matches "+fts" is appended.
    by_title = registry.search("deploy")
    assert by_title[0].entry.id == "deploy_playbook"
    assert by_title[0].match_reason.startswith("title")
    by_desc = registry.search("lint çalıştırır")
    assert by_desc[0].entry.id == "project_checks"
    assert by_desc[0].match_reason.startswith("description")


def test_search_tag_and_top_k(registry: SkillRegistry) -> None:
    by_tag = registry.search("lint", top_k=1)
    assert len(by_tag) == 1
    assert registry.search("hiç eşleşmeyen sorgu xyz") == []
    assert registry.search("   ") == []


# -- REST API -------------------------------------------------------------------


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    monkeypatch.setattr(
        "akana_server.skills.registry.cursor_skill_roots", lambda: []
    )
    root = akana_skills_dir(tmp_path)
    _write_skill(
        root,
        "project_checks",
        description="Test ve lint çalıştırır",
        triggers=["testleri çalıştır"],
    )
    _write_skill(root, "deploy_playbook", skill_type="playbook", description="Deploy senaryosu")
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_api_detail_l1_only(client: TestClient) -> None:
    r = client.get("/api/v1/skills/project_checks")
    assert r.status_code == 200
    skill = r.json()["skill"]
    assert skill["id"] == "project_checks"
    assert skill["description"] == "Test ve lint çalıştırır"
    assert skill["body_loaded"] is False
    assert "body" not in skill
    assert skill["resources"] == []


def test_api_detail_include_body(client: TestClient) -> None:
    r = client.get("/api/v1/skills/project_checks?include_body=true")
    assert r.status_code == 200
    skill = r.json()["skill"]
    assert "Adım bir" in skill["body"]
    assert skill["body_loaded"] is True


def test_api_detail_404(client: TestClient) -> None:
    r = client.get("/api/v1/skills/yok_skill")
    assert r.status_code == 404
    err = r.json()["detail"]["error"]
    assert err["code"] == "SKILL_NOT_FOUND"
    assert "yok_skill" in err["message"]


def test_api_list_type_filter_and_search(client: TestClient) -> None:
    r = client.get("/api/v1/skills?type=playbook")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 1
    assert body["skills"][0]["id"] == "deploy_playbook"

    r2 = client.get("/api/v1/skills?q=testleri çalıştır")
    assert r2.status_code == 200
    res = r2.json()
    assert res["query"] == "testleri çalıştır"
    assert res["skills"][0]["id"] == "project_checks"
    assert res["skills"][0]["match_reason"] == "trigger_exact"
    assert res["skills"][0]["score"] == 1.0
