"""PR-T2 / PR-T2b — skills registry + unified GET /api/v1/skills."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from akana_server.api.app import create_app
from akana_server.skills.cursor_paths import (
    cursor_skill_roots,
    extra_skill_paths_from_env,
)
from akana_server.skills.registry import (
    akana_skills_dir,
    reload_skills,
    scan_cursor_skills,
    scan_akana_skills,
)


def _write_akana_skill(root: Path, skill_id: str, *, cursor_skills: list[str] | None = None) -> Path:
    d = root / skill_id
    d.mkdir(parents=True)
    cs = ""
    if cursor_skills:
        cs = "cursor_skills:\n" + "".join(f"  - {n}\n" for n in cursor_skills)
    (d / "manifest.yaml").write_text(
        f"""id: {skill_id}
version: 1
title: Test {skill_id}
description: Demo skill
risk: low
triggers:
  - run tests
{cs}""",
        encoding="utf-8",
    )
    (d / "SKILL.md").write_text(f"# Test {skill_id}\n\nSteps here.\n", encoding="utf-8")
    return d


def _write_cursor_skill(root: Path, skill_id: str, title: str) -> Path:
    d = root / skill_id
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"# {title}\n\nCursor procedure.\n", encoding="utf-8")
    return d


@pytest.fixture(autouse=True)
def _clear_skill_cache() -> None:
    reload_skills()
    yield
    reload_skills()


def test_scan_akana_skills(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    _write_akana_skill(root, "project_checks", cursor_skills=["shell"])
    entries = scan_akana_skills(root)
    assert len(entries) == 1
    e = entries[0]
    assert e.id == "project_checks"
    assert e.source == "akana"
    assert e.title == "Test project_checks"
    assert e.risk == "low"
    assert e.version == 1
    assert e.cursor_skills == ("shell",)
    assert e.triggers == ("run tests",)


def test_scan_akana_skips_incomplete(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    root.mkdir(parents=True)
    bad = root / "no_manifest"
    bad.mkdir()
    (bad / "SKILL.md").write_text("# x\n", encoding="utf-8")
    _write_akana_skill(root, "ok_skill")
    assert len(scan_akana_skills(root)) == 1


def test_scan_cursor_skills_dedupes_across_roots(tmp_path: Path) -> None:
    a = tmp_path / "cursor_a"
    b = tmp_path / "cursor_b"
    _write_cursor_skill(a, "shell", "Shell skill")
    _write_cursor_skill(b, "shell", "Other title")
    _write_cursor_skill(b, "babysit", "PR babysit")
    entries = scan_cursor_skills([a, b])
    ids = [e.id for e in entries]
    assert ids == ["babysit", "shell"]
    shell = next(e for e in entries if e.id == "shell")
    assert shell.source == "cursor"
    assert shell.title == "Shell skill"


def test_list_skills_akana_only_without_cursor_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "akana_server.skills.registry.cursor_skill_roots",
        lambda: [],
    )
    akana_root = akana_skills_dir(tmp_path)
    _write_akana_skill(akana_root, "project_checks")
    from akana_server.skills.registry import get_registry

    reg = get_registry(tmp_path)
    reg.reload()
    items = [e.to_dict() for e in reg.list()]
    assert len(items) == 1
    assert items[0]["source"] == "akana"


def test_extra_skill_paths_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AKANA_SKILL_PATHS", f"{tmp_path}/extra, /no/such")
    extra = tmp_path / "extra"
    extra.mkdir()
    paths = extra_skill_paths_from_env()
    assert paths == [extra.resolve()]
    _write_cursor_skill(extra, "custom_skill", "Custom")
    roots = cursor_skill_roots()
    assert extra.resolve() in roots
    found = scan_cursor_skills(roots)
    assert any(e.id == "custom_skill" for e in found)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("AKANA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AKANA_TOKEN", "")
    monkeypatch.setenv("AKANA_PORT", "8766")
    monkeypatch.setenv("CURSOR_API_KEY", "")
    # Hermetic skill API test: lifespan register_all copies the pack skills (62+)
    # into the registry → the /skills count would exceed the 2 skills this test
    # writes. This test validates ONLY its own skills; disable pack registration
    # (same pattern as test_metrics).
    monkeypatch.setattr(
        "akana_server.packs.host.AkanaPackHost.register_all", lambda self: []
    )
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_skills_api_endpoint(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    akana_root = akana_skills_dir(tmp_path)
    _write_akana_skill(akana_root, "project_checks", cursor_skills=["shell"])
    cursor_root = tmp_path / "cursor_only"
    _write_cursor_skill(cursor_root, "babysit", "PR merge-ready loop")
    monkeypatch.setattr(
        "akana_server.skills.registry.cursor_skill_roots",
        lambda: [cursor_root.resolve()],
    )

    r = client.get("/api/v1/skills")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    by_id = {s["id"]: s for s in body["skills"]}
    assert by_id["project_checks"]["source"] == "akana"
    assert by_id["project_checks"]["cursor_skills"] == ["shell"]
    assert by_id["babysit"]["source"] == "cursor"
    assert by_id["babysit"]["title"] == "PR merge-ready loop"
    assert "path" in by_id["babysit"]

    r2 = client.get("/api/v1/skills?reload=true")
    assert r2.status_code == 200
    assert r2.json()["count"] == 2


# -- duplicate-id skills (same frontmatter name in two dirs) -----------------------


def _write_akana_fm_skill(
    root: Path, dir_name: str, name: str, *, triggers: list[str], body: str
) -> Path:
    """Frontmatter-only akana skill whose ``name:`` may differ from the dir name."""
    d = root / dir_name
    d.mkdir(parents=True)
    trig = "".join(f"  - {t}\n" for t in triggers)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: demo\ntriggers:\n{trig}---\n\n{body}\n",
        encoding="utf-8",
    )
    return d


def test_duplicate_id_dedup_no_crash_on_tie(tmp_path: Path) -> None:
    """Two dirs with the same frontmatter name + equal-length triggers must not
    crash suggest_for_text (BUG-0: exact.sort() TypeError) and must appear once
    (BUG-1: _entries/_index disagreement)."""
    root = akana_skills_dir(tmp_path)
    _write_akana_fm_skill(
        root, "myskill", "myskill", triggers=["deploy site"], body="BODY-A steps"
    )
    _write_akana_fm_skill(
        root, "myskill_backup", "myskill", triggers=["deploy site"], body="BODY-B steps"
    )
    from akana_server.skills.registry import get_registry

    reg = get_registry(tmp_path)
    reg.reload()
    ids = [e.id for e in reg.list()]
    assert ids.count("myskill") == 1  # deduped, not listed twice
    # Must not raise TypeError on the tie.
    out = reg.suggest_for_text("please deploy site now")
    assert any(s["id"] == "myskill" for s in out)


def test_duplicate_id_trigger_match_loads_matched_body(tmp_path: Path) -> None:
    """BUG-1: before the fix, _entries kept BOTH dirs (so deploy_b's trigger
    'release now' matched in suggest_for_text) while load_body resolved via the
    deduped _index winner (deploy_a) → a match on deploy_b injected BODY-A.

    After the fix _entries == _index (single winner), so the only trigger that can
    match is the winner's own, and load_body always returns that winner's body —
    the matched entry and the loaded body can never diverge."""
    root = akana_skills_dir(tmp_path)
    _write_akana_fm_skill(
        root, "deploy_a", "deploy", triggers=["deploy site"], body="BODY-A steps"
    )
    _write_akana_fm_skill(
        root, "deploy_b", "deploy", triggers=["release now"], body="BODY-B steps"
    )
    from akana_server.skills.registry import get_registry

    reg = get_registry(tmp_path)
    reg.reload()
    listed = [e for e in reg.list() if e.id == "deploy"]
    assert len(listed) == 1  # deduped: the loser dir is gone
    winner = listed[0]
    # load_body resolves the SAME single winner the list surfaces (no cross-wire).
    body = reg.load_body("deploy").strip()
    on_disk = (Path(winner.path) / "SKILL.md").read_text(encoding="utf-8")
    assert body in on_disk
    # deploy_a wins by (source, id)/sorted-scan order, so it is deploy_a's trigger
    # that suggests it — and deploy_a's body that loads. The loser's trigger
    # ('release now') no longer resolves to a phantom entry that injects BODY-A.
    won_a = winner.path.replace("\\", "/").endswith("/deploy_a")
    match_text = "deploy site" if won_a else "release now"
    expected_body = "BODY-A steps" if won_a else "BODY-B steps"
    out = reg.suggest_for_text(f"please {match_text} the build")
    assert any(s["id"] == "deploy" for s in out)
    assert reg.load_body("deploy").strip() == expected_body


def test_cursor_frontmatter_skill_title_not_delimiter(tmp_path: Path) -> None:
    """A Claude-format cursor skill (frontmatter + ``##`` sections, no H1) must not
    get the literal ``---`` as its title (BUG-3)."""
    root = tmp_path / "cursor_fm"
    d = root / "pdf-tools"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\n"
        "name: PDF Tools\n"
        "description: Merge and split PDFs\n"
        "---\n"
        "\n"
        "## Instructions\n"
        "Do the thing.\n",
        encoding="utf-8",
    )
    entries = scan_cursor_skills([root])
    e = next(x for x in entries if x.id == "pdf-tools")
    assert e.title != "---"
    assert e.title == "PDF Tools"
    assert e.description == "Merge and split PDFs"


def test_cursor_frontmatter_yaml_comment_not_title(tmp_path: Path) -> None:
    """A YAML comment line inside the frontmatter must not become the title (BUG-3)."""
    root = tmp_path / "cursor_fm2"
    d = root / "notes"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\n"
        "# When to use\n"
        "name: notes\n"
        "description: take notes\n"
        "---\n"
        "\n"
        "## Steps\n",
        encoding="utf-8",
    )
    entries = scan_cursor_skills([root])
    e = next(x for x in entries if x.id == "notes")
    assert e.title == "notes"
    assert e.title != "When to use"


def test_allowed_filter_applied_before_topk_cap(tmp_path: Path) -> None:
    """BUG-2: with allowed passed in, excluded skills with LONGER triggers must not
    fill the top_k slots ahead of the selected (shorter-trigger) skill."""
    root = akana_skills_dir(tmp_path)
    # Selected skill: short trigger that occurs in the text.
    _write_akana_fm_skill(
        root, "whatsapp_send", "whatsapp_send", triggers=["send"], body="wa body"
    )
    # Three EXCLUDED skills with LONGER overlapping triggers, all in the text.
    _write_akana_fm_skill(
        root, "excl_a", "excl_a", triggers=["send a message"], body="a"
    )
    _write_akana_fm_skill(
        root, "excl_b", "excl_b", triggers=["send a report"], body="b"
    )
    _write_akana_fm_skill(
        root, "excl_c", "excl_c", triggers=["send a picture"], body="c"
    )
    from akana_server.skills.registry import get_registry

    reg = get_registry(tmp_path)
    reg.reload()
    text = "please send a message a report a picture now"
    # Without the fix, top_k=1 would be filled by the longest excluded triggers and
    # whatsapp_send would be dropped before the filter. With the fix, the allowed
    # filter runs before the cap so the selected skill survives.
    out = reg.suggest_for_text(text, 1, allowed={"whatsapp_send"})
    assert [s["id"] for s in out] == ["whatsapp_send"]
