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
